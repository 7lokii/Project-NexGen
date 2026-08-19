"""Project NexGen — FastAPI application.

Run with:  uvicorn app.main:app --reload
Dashboard: http://127.0.0.1:8000/
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager

# Load the .env file BEFORE reading EXECUTION_MODE / MT5_* — the server is
# started with nohup (no shell env) and the listener already relies on dotenv.
try:
    from dotenv import load_dotenv
    # override=False: already-set env vars (tests, CI, explicit command line)
    # keep priority over .env values.
    load_dotenv(override=False)
except ImportError:  # python-dotenv is optional
    pass

# The production runner installs this before Uvicorn starts; keeping the call
# here also protects direct `uvicorn app.main:app` launches.
from app.maintenance import configure_process_stdio
configure_process_stdio("server")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth
from app.background import BackgroundRuntime
from app.config import (APP_NAME, DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                        VERSION)
from app.db import DB
from app.execution.engine import SimEngine
from app.execution.feed import build_market_feed
from app.execution.mt5 import MT5Engine, MT5Provider, load_symbol_map
from app.models import ConfigUpdate, KillSwitchUpdate, SignalIn, SimulateIn
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager

# ------------------------------------------------------------------ wiring

db = DB(DB_PATH)
market = build_market_feed()  # preços reais (Yahoo/TwelveData) com fallback simulado
risk = RiskEngine()
ws = WSManager()

# Execution mode: "sim" (paper, default) or "mt5" (MetaTrader 5 bridge).
# MT5 starts in dry-run (MT5_DRY_RUN=1) — orders are logged, never sent.
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "sim").strip().lower()
MT5_DRY_RUN = os.getenv("MT5_DRY_RUN", "1") != "0"
mt5_provider = None
if EXECUTION_MODE == "mt5":
    mt5_login = os.getenv("MT5_LOGIN")
    mt5_provider = MT5Provider(
        login=int(mt5_login) if mt5_login and mt5_login.isdigit() else None,
        server=os.getenv("MT5_SERVER", ""),
        password=os.getenv("MT5_PASSWORD", ""),
        symbols=load_symbol_map(),
        # N legs require independent hedged positions. In dry-run the check
        # remains visible but cannot block a paper-only terminal; real mode is
        # fail-closed when margin_mode is not RETAIL_HEDGING.
        require_hedged=not MT5_DRY_RUN,
    )


def build_engine():
    """Return the execution engine for the configured mode.

    `BotState` only depends on the interface (open_trade / update /
    close_trade_at_market), so swapping sim <-> MT5 is transparent.
    """
    if EXECUTION_MODE == "mt5":
        return MT5Engine(db, mt5_provider, market)
    return SimEngine(db, market)


sim = build_engine()
notifier = Notifier(db, ws, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
state = BotState(db, market, risk, sim, notifier, ws)


_last_mt5_reconnect = 0.0


def _record_runtime_exception(exc: BaseException, context: dict) -> None:
    """Persist errors caught by the long-running runtime without masking them."""
    try:
        db.record_exception(exc, context=context)
    except Exception as telemetry_exc:
        print(f"[telemetry] não conseguiu guardar exceção: {telemetry_exc}")


async def _reconnect_mt5() -> None:
    """Reconnect the terminal with the existing ten-second throttle."""
    global _last_mt5_reconnect
    if EXECUTION_MODE != "mt5" or mt5_provider is None:
        return
    if mt5_provider.execution_block_reason:
        return
    if not mt5_provider.connected and time.time() - _last_mt5_reconnect > 10:
        _last_mt5_reconnect = time.time()
        db.record_mt5_connection_event("reconnect_attempt", "market_loop")
        try:
            ok = mt5_provider.initialize()
        except Exception as exc:
            db.record_mt5_connection_event("reconnect_failure", str(exc))
            _record_runtime_exception(exc, {"loop": "market", "phase": "mt5_reconnect"})
            return
        if ok:
            db.record_mt5_connection_event("reconnect_success", "market_loop")
            print("[mt5] religado automaticamente")
        else:
            db.record_mt5_connection_event(
                "reconnect_failure",
                mt5_provider.execution_block_reason or "initialize() devolveu False",
            )


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTENER_LOG = os.path.join(ROOT_DIR, "listener.log")


def _spawn_listener() -> subprocess.Popen:
    """Arranca o listener; o próprio processo abre os logs rotativos.

    Avoiding a parent-owned ``listener.log`` handle is essential on Windows:
    the child must be able to close/rename its active file at rotation time.
    """
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "app.telegram_listener"],
        cwd=ROOT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=os.environ, creationflags=flags,
    )


def _listener_running_anywhere() -> bool:
    """Já existe um processo do listener a correr (arrancado manualmente,
    por start.ps1 ou por um servidor anterior)? Se sim, o watchdog não deve
    spawnar um duplicado — só espera o heartbeat chegar.

    Usa Get-CimInstance (PowerShell) — o wmic foi removido em Windows
    recentes e deixava esta função a devolver False, fazendo o watchdog
    spawnar duplicados."""
    if os.name != "nt":
        return False
    try:
        ps = (
            "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*telegram_listener*' } | "
            "Measure-Object).Count"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        return out.strip().isdigit() and int(out.strip()) > 0
    except Exception:
        return False


async def _ensure_listener_alive() -> None:
    """Watchdog: se o heartbeat do listener parar (>5s sem notícias), reinicia
    o processo automaticamente (com cooldown de 60s para não entrar em loop
    se o arranque falhar)."""
    if state.listener_status().get("alive"):
        return
    proc = state.listener_proc
    if proc is not None and proc.poll() is None:
        return  # já temos um filho a tentar arrancar
    if time.time() < state.listener_next_restart_at:
        return
    # se já existe um listener a correr (ex.: arrancado à mão ou por um
    # servidor anterior), não criar duplicado — o heartbeat chega em ~1s
    if _listener_running_anywhere():
        state.listener_next_restart_at = time.time() + 60
        return
    try:
        state.listener_proc = _spawn_listener()
        state.listener_next_restart_at = time.time() + 60
        state.db.record_listener_restart("watchdog: heartbeat parado")
        print("[watchdog] listener OFFLINE — processo reiniciado")
        await notifier.send(
            "risk",
            "📨 Listener do Telegram OFFLINE — reiniciado automaticamente. "
            "Se não voltar a receber mensagens em ~1min, verifica a sessão.",
        )
    except Exception as exc:
        print(f"[watchdog] falha ao reiniciar listener: {exc}")


_last_restart_purge = 0.0


async def _listener_watchdog_tick() -> None:
    """One watchdog sample; ``BackgroundRuntime`` supplies the ten-second loop."""
    global _last_restart_purge
    try:
        state.record_uptime_sample()
    except Exception as exc:
        print(f"[watchdog] sample error: {exc}")
    try:
        await _ensure_listener_alive()
    except Exception as exc:
        print(f"[watchdog] restart error: {exc}")
    # purge periódico: `listener_restarts` não cresce sem limite
    # (registos com mais de 30 dias são apagados ~ a cada 6h)
    if time.time() - _last_restart_purge > 6 * 3600:
        try:
            n = state.db.purge_old_listener_restarts(30)
            if n:
                print(f"[watchdog] purge: {n} reinícios antigos apagados")
        except Exception as exc:
            print(f"[watchdog] purge error: {exc}")
        _last_restart_purge = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if EXECUTION_MODE == "mt5":
        ok = mt5_provider.initialize()
        # o terminal MT5 pode demorar a arrancar após reboot (o boot.ps1
        # lança-o antes do servidor) — retry curto e limitado para não ficar
        # desligado de forma definitiva.
        attempts = 0
        while not ok and attempts < 10:
            await asyncio.sleep(3)
            ok = mt5_provider.initialize()
            attempts += 1
        db.record_mt5_connection_event(
            "startup_connect" if ok else "startup_failure",
            "lifespan.initialize",
            details={"dry_run": MT5_DRY_RUN},
        )
        print(f"[mt5] terminal {'ligado' if ok else 'INACESSÍVEL — a usar market feed'}"
              f" (dry_run={MT5_DRY_RUN})")
        if mt5_provider.execution_block_reason:
            await notifier.send(
                "risk",
                "⛔ Execução MT5 bloqueada: "
                f"{mt5_provider.execution_block_reason}. "
                "Nenhuma ordem real será enviada até ligares uma conta hedged.",
            )
    try:
        n = db.purge_old_listener_restarts(30)
        if n:
            print(f"[startup] purge: {n} reinícios antigos apagados")
    except Exception as exc:
        print(f"[startup] purge error: {exc}")
    # The runtime owns and cancels both long-running tasks.  This is
    # deliberately outside the engine modules: importing/using MT5Engine in
    # a test cannot inherit an orphaned FastAPI loop.
    background = BackgroundRuntime(
        tick=state.tick,
        reconnect=_reconnect_mt5,
        watchdog=_listener_watchdog_tick,
        logger=print,
        exception_recorder=_record_runtime_exception,
    )
    background.start()
    try:
        yield
    finally:
        await background.stop()
        if mt5_provider is not None:
            mt5_provider.shutdown()


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)


# ------------------------------------------------------------ auth (login)

# The dashboard is fully open when SIGNALBOT_DASHBOARD_PASSWORD is unset
# (local use). Set it to require a password before the dashboard/API respond.
LOGIN_PATH = "/login"
LOGIN_HTML_PATH = os.path.join(STATIC_DIR := os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static"), "login.html")


def _login_page():
    """The login screen (served for / and /login when not authenticated)."""
    try:
        with open(LOGIN_HTML_PATH, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except OSError:
        return HTMLResponse("<h1>Login necessário</h1><p>Define SIGNALBOT_DASHBOARD_PASSWORD no .env.</p>", status_code=401)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not auth.enabled():
        return await call_next(request)
    path = request.url.path
    if path == LOGIN_PATH:
        # /login serves the login page; already authenticated -> dashboard
        if auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse("/", status_code=302)
        return _login_page()
    if path in ("/api/login", "/api/logout", "/favicon.ico"):
        return await call_next(request)
    # the Telegram listener authenticates with the API token (no browser)
    if path.startswith("/api/") and auth.check_api_token(
            request.headers.get("x-api-token")):
        return await call_next(request)
    if auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/ws"):
        return JSONResponse(status_code=401, content={"error": "login necessário"})
    # any page (/, static files): show the login screen instead
    return _login_page()


@app.post("/api/login")
async def login(body: dict):
    if not auth.enabled():
        return {"ok": True, "message": "autenticação desligada"}
    if not auth.check_password(body.get("password", "")):
        return JSONResponse(status_code=401, content={"error": "password incorreta"})
    token = auth.create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ------------------------------------------------------------------ páginas

# A dashboard dividiu-se em páginas HTML separadas (estilo multi-página): a
# principal em `/` (index.html), as simulações de estratégias em `/teste`, o
# diagnóstico em `/diagnostico` e a ajuda em `/help`. O middleware de auth já
# protege qualquer path não-API (sem sessão → página de login), por isso estas
# rotas só precisam de servir o HTML.


def _static_page(name: str):
    """Serve um ficheiro HTML de app/static (as páginas da dashboard)."""
    path = os.path.join(STATIC_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except OSError:
        return HTMLResponse("<h1>Página não encontrada</h1>", status_code=404)


@app.get("/teste", response_class=HTMLResponse)
async def page_teste():
    return _static_page("teste.html")


@app.get("/diagnostico", response_class=HTMLResponse)
async def page_diagnostico():
    return _static_page("diagnostico.html")


@app.get("/help", response_class=HTMLResponse)
async def page_help():
    return _static_page("help.html")


# ------------------------------------------------------------------ api

@app.get("/api/status")
async def status():
    return state.snapshot()


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    return await state.update_config(body)


@app.post("/api/killswitch")
async def killswitch(body: KillSwitchUpdate):
    return await state.set_kill_switch(body.enabled)


@app.post("/api/signals")
async def add_signal(body: SignalIn):
    return await state.ingest(body)


@app.post("/api/signals/{signal_id}/approve")
async def approve(signal_id: str):
    return await state.approve(signal_id)


@app.post("/api/signals/{signal_id}/reject")
async def reject(signal_id: str):
    return await state.reject(signal_id)


@app.post("/api/signals/{signal_id}/delete")
async def delete_signal(signal_id: str):
    return await state.delete_signal(signal_id)


@app.get("/api/report")
async def report(days: int = 7):
    return state.report(days)


@app.get("/api/operations")
async def operations(days: int = 90):
    """Item 10: exceções, spread mensal e estabilidade MT5 agregados.

    Os dados são observações persistidas; a dashboard mostra explicitamente
    quando ainda não há amostra suficiente para uma tendência.
    """
    return state.operational_metrics(days=days)


@app.get("/api/analysis")
async def analysis(group: str = "", days: int = 30,
                  risk_pct: float = 0.0, balance: float = 0.0):
    """Backtest dos sinais reais de um grupo (edge analysis) — nunca abre trades.
    `risk_pct`/`balance` opcionais (0 = config/saldo atual) para estudos de
    risco — o piso de lote mínimo domina em saldos pequenos."""
    return await state.edge_analysis(group=group, days=days,
                                     risk_pct=risk_pct or None,
                                     sim_balance=balance or None)


@app.get("/api/analysis/compare")
async def analysis_compare(group: str = "", days: int = 30, since: str = "",
                           risk_pct: float = 0.0, balance: float = 0.0):
    """A/B: TP1..TP5 (nova) vs + gestão vs fechar no TP2 — nunca abre trades.
    `since` (YYYY-MM-DD) filtra o período (ex.: só o formato atual);
    `risk_pct`/`balance` opcionais (0 = config/saldo atual) para estudos."""
    return await state.compare_analysis(group=group, days=days, since=since,
                                        risk_pct=risk_pct or None,
                                        sim_balance=balance or None)


@app.get("/api/analysis/protect_latency")
async def analysis_protect_latency(strategy: str = "auto_escada_3_hibrida",
                                   engine: str = "", limit: int = 500,
                                   days: int = 0, since: str = ""):
    """Latência do protect híbrido: decisões paper/MT5 persistidas ou
    backtest recalculado quando ``engine=backtest``."""
    return await state.protect_latency_report(
        strategy_id=strategy, engine=engine, limit=limit,
        days=days, since=since,
    )


@app.get("/api/analysis/protect_causal")
async def analysis_protect_causal(strategy: str = "auto_escada_3_hibrida",
                                  limit: int = 500, days: int = 0,
                                  since: str = ""):
    """Atribuição causal dos eventos ``protect`` da híbrida no replay
    histórico: delta contra a mesma auto sem o protect, por evento."""
    return await state.protect_causal_report(
        strategy_id=strategy, limit=limit, days=days, since=since,
    )


@app.get("/api/edits")
async def edits(group: str = "", limit: int = 500):
    """Distribuição do atraso das edições do grupo (min/méd/mediana/p90/máx)
    — para validar a janela de edição com dados reais."""
    return state.edit_delay_stats(group=group, limit=limit)


@app.get("/api/analysis/walkforward")
async def analysis_walkforward(group: str = "", days: int = 30, buckets: int = 4):
    """Walk-forward da estratégia com gestão: edge por janela temporal."""
    return await state.walk_forward(group=group, days=days, buckets=buckets)


@app.get("/api/strategies")
async def strategies():
    """Registo central de estratégias + qual é a principal."""
    return state.strategies_info()


@app.get("/api/paper/history")
async def paper_history(hours: int = 24):
    """Histórico de saldos da corrida paralela (por estratégia): as de teste
    com a conta virtual, a principal com o saldo real da conta MT5 — para a
    mini-curva "quem está a ganhar" na dashboard (principal + tab Edge)."""
    from datetime import datetime, timedelta, timezone
    from app.strategies import (HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES,
                                STRATEGIES)
    main = state.snapshot().get("config", {}).get("strategy")
    # até 30 dias (720h) para o seletor da dashboard (24h / 7d / 30d)
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat(timespec="seconds")
    out = []
    # a curva mostra só as estratégias visíveis + a real: as REMOVIDAS
    # (HIDDEN_STRATEGIES) e as ocultas do menu (MENU_HIDDEN_STRATEGIES —
    # nova/auto_5 continuam a ABRIR papel mas ficam FORA da curva, como a
    # matriz do ESTRATEGIAS.md Parte IX define) não entram.
    for sid, strat in STRATEGIES.items():
        if sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES:
            continue
        pts = db.paper_balance_history(sid, since)
        if not pts:
            continue
        stats = db.paper_trade_stats(sid)
        out.append({
            "strategy_id": sid,
            "nome": strat["nome"],
            "is_main": sid == main,
            "is_real": False,
            "closed": stats["closed"],
            "win_rate": stats["win_rate"],
            "points": pts,
        })
    # linha de referência: o saldo real da conta MT5 (estratégia reservada
    # `__real__`, gravada no tick) — a dashboard mostra-a separada das contas
    # virtuais da corrida.
    real_pts = db.paper_balance_history("__real__", since)
    if real_pts:
        out.append({
            "strategy_id": "__real__",
            "nome": "MT5 real",
            "is_main": False,
            "is_real": True,
            "closed": None,
            "win_rate": None,
            "points": real_pts,
        })
    return {"ok": True, "series": out}


@app.get("/api/paper/trades")
async def paper_trades(strategy: str = "", limit: int = 50):
    """Histórico de trades de papel de uma estratégia de teste (corrida
    paralela): fechadas com P&L + abertas — a página de teste mostra a tabela."""
    if not strategy:
        return {"ok": True, "trades": []}
    # `account` diz à dashboard se esta estratégia é a principal (corre na
    # conta real, sem corrida paper) — a página de teste mostra-o na tabela
    main = state.snapshot().get("config", {}).get("strategy")
    return {"ok": True, "strategy": strategy,
            "account": {"strategy_id": strategy, "is_main": strategy == main},
            "trades": db.paper_trades(strategy, max(1, min(limit, 200)))}


@app.get("/api/paper/divergences")
async def paper_divergences():
    """Divergências auto vs referência (`nova_gestao`) por sinal com trade
    de papel — a tabela da tab Relatório (SPEC v2 §3.4: comparação paper vs
    backtest ao vivo, sinal a sinal)."""
    return {"ok": True, "divergences": state.paper_divergences()}


@app.get("/api/analysis/real_vs_backtest")
async def analysis_real_vs_backtest(strategy: str = ""):
    """Compara as trades REAIS fechadas da conta demo com o backtest do mesmo
    sinal na estratégia principal (slippage de entrada, diferença de saída,
    timing e P&L por trade) — tabela do Relatório. SPEC v2 §3.4."""
    return await state.real_vs_backtest(strategy_id=strategy or None)


@app.get("/api/analysis/auto_sl_log")
async def analysis_auto_sl_log():
    """Auditoria dos três níveis de SL da auto-gestão no MT5 (Fase 3): por
    trade, cada tentativa de modify com target→valid→applied + retcode — para
    a janela de validação ver se algum modify foi recusado (10016) ou
    arredondado pelo broker. ESTRATEGIAS.md Parte X."""
    return state.auto_sl_log()


@app.get("/api/analysis/paper_vs_real_auto")
async def analysis_paper_vs_real_auto(strategy: str = ""):
    """Fase 3 — trade real da auto (MT5, quando promovida a principal) vs a
    trade de papel da mesma estratégia no mesmo sinal: mesma decisão de
    saída, Δ preço de fecho, Δ timing e P&L de cada lado (tabela do
    Relatório). ESTRATEGIAS.md Parte X — divergence report paper↔MT5."""
    return await state.paper_vs_real_auto(strategy_id=strategy or None)


@app.get("/api/analysis/fase3_status")
async def analysis_fase3_status():
    """Payload único do painel Fase 3 (uma chamada em vez de 4): ETA (com a
    curva do ritmo), snapshot antes/depois, veredictos + histórico, registo
    de alterações de alvo, divergences paper↔MT5, real vs backtest, DD das
    contas e auditoria de SL."""
    return await state.fase3_status()


@app.get("/api/analysis/trace")
async def analysis_trace(strategy: str = "tp2", group: str = "",
                         days: int = 0, since: str = ""):
    """Exemplo do fluxo real de uma trade (página de teste): o sinal
    resolvido mais recente da estratégia + eventos SL/TP1/BE/fecho + caminho
    de preços. Só para estratégias sem gestão (tp2, nova)."""
    return await state.trace_signal(strategy_id=strategy, group=group,
                                    days=days, since=since)


@app.get("/api/analysis/month")
async def analysis_month(group: str = "", days: int = 30, strategy: str = "",
                         since: str = "",
                         risk_pct: float = 0.0, balance: float = 0.0):
    """Simulação do último mês para uma estratégia (default = principal):
    sinais reais desde há `days` dias até hoje (ou `since=YYYY-MM-DD` para
    filtrar a partir de uma data — formato atual vs histórico completo),
    começando com o saldo atual — nunca abre trades. `risk_pct`/`balance`
    opcionais (0 = config/saldo atual) para estudos de risco."""
    return await state.month_simulation(group=group, days=days,
                                        strategy_id=strategy or None,
                                        since=since,
                                        risk_pct=risk_pct or None,
                                        sim_balance=balance or None)


@app.post("/api/simulate")
async def simulate(body: SimulateIn):
    return await state.simulate(body)


@app.post("/api/summary")
async def summary():
    return await state.daily_summary()


@app.get("/api/trades")
async def trades():
    return [t.to_dict() for t in db.list_trades(100)]


@app.get("/api/notifications")
async def notifications():
    return [
        {"id": n.id, "kind": n.kind, "text": n.text, "created_at": n.created_at}
        for n in db.list_notifications(100)
    ]


@app.get("/api/ignored")
async def ignored(limit: int = 100, q: str = "", days: int = 0):
    """Auditoria: mensagens do grupo que o bot decidiu ignorar (conversa,
    não são sinais nem instruções de gestão). Filtros opcionais: `q`
    (palavra-chave no texto) e `days` (só últimos N dias; 0 = tudo)."""
    return {"ok": True, "total": db.count_ignored(q=q, days=days),
            "items": db.recent_ignored(limit, q=q, days=days)}


@app.post("/api/heartbeat")
async def heartbeat(body: dict = {}):
    """Heartbeat do listener do Telegram (POST a cada 1s) — alimenta a tab
    Diagnóstico com o estado da ligação ao grupo e a frescura da última
    mensagem recebida."""
    if isinstance(body, dict):
        state.touch_listener(body.get("groups"),
                             body.get("last_msg_at"),
                             body.get("tick"))
    else:
        state.touch_listener()
    return {"ok": True}


@app.post("/api/listener/history_done")
async def listener_history_done(body: dict = {}):
    """O listener avisa que o replay do histórico terminou (com os totais
    reencaminhados) — o servidor regista a notificação no feed."""
    if isinstance(body, dict):
        return await state.history_done(body)
    return {"ok": True, "notified": False}


@app.post("/api/listener/need_code")
async def listener_need_code():
    """O listener precisa de um código de login do Telegram (sessão expirada /
    dispositivo novo) — avisa a dashboard para o pedir ao utilizador."""
    state.listener_need_code = True
    await notifier.send(
        "risk",
        "🔢 Código do Telegram necessário — abre a dashboard e introduz o "
        "código que o Telegram enviou para a app, para o listener voltar a ligar.",
    )
    return {"ok": True}


@app.get("/api/listener/code")
async def listener_code_get():
    """O listener busca o código (one-time) que o utilizador introduziu."""
    return {"code": state.consume_listener_code()}


@app.post("/api/listener/code")
async def listener_code_set(body: dict = {}):
    """A dashboard entrega o código do Telegram introduzido pelo utilizador."""
    if isinstance(body, dict):
        state.set_listener_code(body.get("code", ""))
    return {"ok": True}


def _downsample_uptime(hist: list, max_points: int = 300):
    """Reduz o histórico de uptime (amostras a cada 10s ≈ 8640/dia) para
    ~300 pontos para o gráfico da dashboard."""
    n = len(hist)
    if n <= max_points:
        return hist
    step = n / max_points
    return [hist[int(i * step)] for i in range(max_points)]


@app.get("/api/diagnostics")
async def diagnostics():
    """Estado do sistema para a tab Diagnóstico: MT5, listener, servidor,
    market feed, auth, base de dados e última mensagem do grupo."""
    started = state.started_at
    uptime_s = 0
    try:
        from datetime import datetime, timezone
        start = datetime.fromisoformat(started)
        if start.tzinfo is None:
            start = start.astimezone()
        uptime_s = int((datetime.now(timezone.utc) - start).total_seconds())
    except (ValueError, TypeError):
        pass
    exec_state = state._execution_state()
    acct = exec_state.get("account") or {}
    mgmt = next((s for s in state.db.list_signals(1) if s.status == "management"), None)
    return {
        "ok": True,
        "server": {
            "started_at": started,
            "uptime_s": uptime_s,
            "pid": os.getpid(),
            "app": APP_NAME,
        },
        "execution": exec_state,
        "mt5": {
            "mode": exec_state.get("mode"),
            "connected": exec_state.get("connected"),
            "trade_allowed": exec_state.get("trade_allowed"),
            "last_auto_trading_off_at": state.db.all_config().get("last_auto_trading_off_at", ""),
            "login": acct.get("login"),
            "server": acct.get("server"),
            "balance": acct.get("balance"),
            "equity": acct.get("equity"),
            "currency": acct.get("currency"),
            "leverage": acct.get("leverage"),
            "margin_mode": exec_state.get("margin_mode"),
            "hedged": exec_state.get("hedged"),
            "blocked_reason": exec_state.get("blocked_reason", ""),
        },
        "listener": state.listener_status(),
        "listener_restarts": state.db.list_listener_restarts(10),
        "replay": {
            "done_at": state.db.all_config().get("replay_done_at", ""),
            "group": state.db.all_config().get("replay_group", ""),
            "total": int(state.db.all_config().get("replay_total", "0") or 0),
            "sinais": int(state.db.all_config().get("replay_sinais", "0") or 0),
            "gestao": int(state.db.all_config().get("replay_gestao", "0") or 0),
            "resumos": int(state.db.all_config().get("replay_resumos", "0") or 0),
            "ignoradas": int(state.db.all_config().get("replay_ignoradas", "0") or 0),
        },
        "market": {
            "real_data": state.market.real_data_active,
            "sources": {sym: meta.get("source", "sim")
                         for sym, meta in state.market.meta().items()},
            "changes": {sym: meta.get("chg_pct")
                        for sym, meta in state.market.meta().items()},
        },
        "auth": {"enabled": auth.enabled()},
        "uptime_history": _downsample_uptime(list(state.uptime_history)),
        "operations": {
            "disk": dict(getattr(state, "_disk_status", {}) or {}),
            "metrics": state.operational_metrics(days=90),
        },
        "db": {
            "path": str(DB_PATH),
            "signals": state.db.count_signals(),
            "trades": state.db.count_trades(),
            "positions_open": len(state.db.open_positions()),
            "ignored": state.db.count_ignored(),
            "summaries": len(state.db.latest_group_summaries(500)),
        },
        "config": {
            "mode": state.config["mode"],
            "risk_pct": float(state.config["risk_pct"]),
            "kill_switch": bool(state.config["kill_switch"]),
            "edit_window_seconds": float(state.config["edit_window_seconds"]),
        },
        "last_group_msg": (mgmt.to_dict() if mgmt else None),
    }


@app.delete("/api/listener_restarts")
async def clear_listener_restarts():
    """Apaga TODO o histórico de reinícios do listener (ação manual da
    dashboard — a tab Diagnóstico tem um botão com confirmação)."""
    n = state.db.clear_listener_restarts()
    return {"ok": True, "deleted": n}


@app.get("/api/summaries")
async def summaries(group: str = "", limit: int = 10):
    """Resumos diários/semanais do grupo (win rate reportado = ground truth)."""
    return {"ok": True, "summaries": db.list_group_summaries(source_like=group, limit=limit)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if auth.enabled() and not auth.valid_session(
            websocket.cookies.get(auth.COOKIE_NAME)):
        await websocket.close(code=4401)
        return
    await ws.connect(websocket, user="admin")
    try:
        await websocket.send_text(
            json.dumps({"type": "snapshot", "data": state.snapshot()})
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws.disconnect(websocket)


# --------------------------------------------------------------- frontend

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
