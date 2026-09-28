"""Project NexGen — FastAPI application.

Run with:  uvicorn app.main:app --reload
Dashboard: http://127.0.0.1:8000/
"""

import asyncio
import hashlib
import httpx
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import auth
from app.background import BackgroundRuntime
from app.config import (APP_NAME, DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                        WHATSAPP_BRIDGE_URL, WHATSAPP_ENABLED,
                        WHATSAPP_GROUP_JID, DASHBOARD_URL, VERSION)
from app.db import DB
from app.execution.engine import SimEngine
from app.execution.feed import build_market_feed
from app.execution.mt5 import MT5Engine, MT5Provider, load_symbol_map
from app.execution.replay import run_replay
from app.execution.evidence import (audit_csv, audit_report, compare_broker_fills,
                                    forward_gate_report, journal_quality_report)
from app.execution.audit import daily_execution_audit, recent_strategy_comparison
from app.models import ConfigUpdate, KillSwitchUpdate, PreRealDecisionIn, SignalIn, SimulateIn
from app.notify.telegram import Notifier
from app.notify.whatsapp import WhatsAppNotifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager, safe_json

# ------------------------------------------------------------------ wiring

db = DB(DB_PATH)
market = build_market_feed()  # preços reais (Yahoo/TwelveData) com fallback simulado
risk = RiskEngine()
ws = WSManager()

# Execution mode: "sim" (paper, default) or "mt5" (MetaTrader 5 bridge).
# MT5 starts in dry-run (MT5_DRY_RUN=1) — orders are logged, never sent.
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "sim").strip().lower()
MT5_DRY_RUN = os.getenv("MT5_DRY_RUN", "1") != "0"
MT5_2_DRY_RUN = os.getenv("MT5_2_DRY_RUN", "1") != "0"
# Worker isolado da lane 2 (app/lane2_worker.py): processo separado dono da
# ligação MetaTrader5 à 2ª conta. Opt-in — sem ele a lane 2 corre em dry-run
# (a API MetaTrader5 só permite uma ligação global por processo).
LANE2_WORKER_ENABLED = os.getenv("LANE2_WORKER_ENABLED", "0") == "1"
LANE2_WORKER_PORT = int(os.getenv("LANE2_WORKER_PORT", "8770") or 8770)
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
    # O MT5 spot é a fonte prioritária para os metais (XAUUSD/XAGUSD): o Yahoo
    # só serve os futuros GC=F/SI=F, que têm um offset de base vs o spot que o
    # broker executa (~35 $ no ouro — observado 14/09). Sem a correção, a
    # corrida paper resolve trades fantasma ("fecho instantâneo em lucro") que
    # a conta real nunca fez. Sem terminal ligado, o feed cai para o web
    # provider (failsafe).
    market.set_mt5_sources([mt5_provider])

# Segunda lane de execução (conta demo paralela, tp2_protect) — opcional.
# Ativada quando MT5_2_LOGIN está definido no .env. Em dry-run a lane 2 NÃO
# abre uma segunda ligação MT5 (o pacote MetaTrader5 mantém UMA ligação por
# processo): o motor degrada para o market feed e a conta é apresentada como
# dry-run a partir do .env. Em execução real (MT5_DRY_RUN=0), a lane 2 usa
# MT5_2_TERMINAL_PATH (instalação portable) para ligar ao segundo terminal.
mt5_provider2 = None
mt5_2_login = os.getenv("MT5_2_LOGIN")
lane2_enabled = bool(mt5_2_login and mt5_2_login.strip())
if EXECUTION_MODE == "mt5" and lane2_enabled:
    mt5_provider2 = MT5Provider(
        login=int(mt5_2_login) if mt5_2_login.isdigit() else None,
        server=os.getenv("MT5_2_SERVER", ""),
        password=os.getenv("MT5_2_PASSWORD", ""),
        symbols=load_symbol_map(),
        require_hedged=not MT5_2_DRY_RUN,
        path=os.getenv("MT5_2_TERMINAL_PATH", ""),
        portable=bool(os.getenv("MT5_2_TERMINAL_PATH", "").strip()),
    )
    if not MT5_2_DRY_RUN:
        # MetaTrader5 exposes one process-global connection. Two provider
        # objects cannot safely control two real accounts in this API process;
        # keep lane 2 fail-closed until it is moved to its own worker process.
        mt5_provider2._execution_block_reason = (
            "segunda conta MT5 real bloqueada: a API MetaTrader5 usa uma "
            "ligação global por processo; a lane 2 precisa de um worker isolado"
        )


def build_engine():
    """Return the execution engine for the configured mode.

    `BotState` only depends on the interface (open_trade / update /
    close_trade_at_market), so swapping sim <-> MT5 is transparent.
    """
    if EXECUTION_MODE == "mt5":
        return MT5Engine(db, mt5_provider, market)
    return SimEngine(db, market)


_lane2_worker_client: Any = None


def build_engine2():
    """Engine da segunda lane (conta demo paralela) — só MT5.

    Com `LANE2_WORKER_ENABLED=1`, a lane 2 liga ao worker isolado
    (app/lane2_worker.py) — execução REAL no segundo terminal
    (`dry_run=False`) via `RemoteProvider`; sem worker configurado mantém o
    comportamento antigo (dry-run: ordens registadas, nada enviado).
    """
    global _lane2_worker_client
    if mt5_provider2 is None:
        return None
    if LANE2_WORKER_ENABLED:
        from app.execution.remote2 import RemoteProvider
        _lane2_worker_client = RemoteProvider(
            base_url=f"http://127.0.0.1:{LANE2_WORKER_PORT}",
            token=os.getenv("LANE2_WORKER_TOKEN") or os.getenv("SIGNALBOT_API_TOKEN", ""),
        )
        # O worker da lane 2 também serve ticks spot: 2ª fonte para o feed
        # (a principal continua à frente; o feed usa a 1ª ligada com tick).
        market.set_mt5_sources([mt5_provider, _lane2_worker_client])
        return MT5Engine(db, _lane2_worker_client, market, dry_run=False,
                         lane_id="tp2")
    return MT5Engine(db, mt5_provider2, market, dry_run=MT5_2_DRY_RUN,
                     lane_id="tp2")


sim = build_engine()
sim2 = build_engine2()


def _stuck_pending_legs(engine) -> list:
    """Pernas `pending_fill` em retry de delete do motor (watchdog
    `pending_delete_stuck`) — read-only para o Diagnóstico."""
    fn = getattr(engine, "stuck_pending_legs", None)
    if fn is None:
        return []
    try:
        return fn() or []
    except Exception:
        return []
notifier = Notifier(db, ws, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
whatsapp = WhatsAppNotifier(db, WHATSAPP_ENABLED, WHATSAPP_BRIDGE_URL,
                            WHATSAPP_GROUP_JID, DASHBOARD_URL)
state = BotState(
    db, market, risk, sim, notifier, ws, whatsapp=whatsapp,
    second_sim=sim2,
    second_strategy=os.getenv("MT5_2_STRATEGY", "tp2_protect"),
    second_label=os.getenv("MT5_2_STRATEGY", "tp2_protect"),
)


_last_mt5_reconnect = 0.0
# Lanes cuja 1ª tentativa de ligação no BOOT falhou (transitória — o worker
# portable demora mais a ligar do que o servidor). Serve para: (a) o feed
# distinguir a falha de boot (system) do bloqueio em runtime (risk); (b) o
# reconnect/retry emitir a notificação de RECUPERAÇÃO quando ligar.
_lane_boot_failed: set = set()
_lane2_worker_watch_state = {"alive": None}
_lane2_at_state = None  # None=desconhecido, False=OFF, True=ON
_last_lane2_at_warn = 0.0
_last_lane2_quote_check = 0.0
_lane2_quote_broken_since = None  # ISO quando XAUUSD ficou sem tick no worker
_last_lane2_quote_warn = 0.0
_last_lane2_probe_warn = 0.0


def _record_runtime_exception(exc: BaseException, context: dict) -> None:
    """Persist errors caught by the long-running runtime without masking them."""
    try:
        db.record_exception(exc, context=context)
    except Exception as telemetry_exc:
        print(f"[telemetry] não conseguiu guardar exceção: {telemetry_exc}")


def _lane2_provider():
    """Provider da lane tp2 — o MESMO que o reconnect usa.

    Em execução real (LANE2_WORKER_ENABLED) é o RemoteProvider do worker
    isolado; o `mt5_provider2` local tem bloqueio PERMANENTE por design
    (a API MetaTrader5 é uma ligação global por processo) e inicializá-lo
    no boot falhava sempre, mesmo com o worker saudável — era a causa da
    "Execução MT5 bloqueada na lane tp2 ... ligação global" em todos os
    arranques com o worker de facto bom."""
    return _lane2_worker_client if LANE2_WORKER_ENABLED else mt5_provider2


async def _initialize_mt5_lane_at_boot(lane_id: str, provider, dry_run: bool) -> None:
    """Uma tentativa de ligação MT5 da lane no arranque, fora do loop
    (thread) — SEM retries. O arranque não pode ficar dezenas de segundos a
    bloquear a socket (o uvicorn escuta mas só serve depois do lifespan) —
    era isto que o healthcheck via como "sem resposta HTTP" e matava um
    servidor a meio do boot. Se falhar, o reconnect do market_loop
    (throttle 10s) repete em background assim que o servidor servir."""
    try:
        ok = bool(await asyncio.to_thread(provider.initialize))
    except Exception as exc:
        db.record_mt5_connection_event(
            "startup_failure", f"lifespan.initialize:{lane_id}",
            details={"dry_run": dry_run, "lane": lane_id, "error": str(exc)},
        )
        _lane_boot_failed.add(lane_id)
        print(f"[mt5:{lane_id}] terminal INACESSÍVEL ({exc}) — reconnect em background")
        # Falha de BOOT = TRANSITÓRIA (worker/terminal ainda a arrancar) →
        # system, não risk: o reconnect/retry trata de ligar e notificar.
        await notifier.send(
            "system",
            f"⏳ [{lane_id}] MT5 não ligou no arranque ({exc}) — transitória; "
            "retry em background e reconnect do loop tratam de ligar.",
            lane=lane_id if lane_id != "main" else "",
        )
        return
    db.record_mt5_connection_event(
        "startup_connect" if ok else "startup_failure",
        f"lifespan.initialize:{lane_id}",
        details={"dry_run": dry_run, "lane": lane_id},
    )
    print(f"[mt5:{lane_id}] terminal {'ligado' if ok else 'INACESSÍVEL'}"
          f" (dry_run={dry_run})")
    if not ok:
        _lane_boot_failed.add(lane_id)
        if provider.execution_block_reason:
            # BLOQUEIO EM RUNTIME (permanente: conta não hedged, config
            # errada...) → risk — não vai recuperar sozinho.
            await notifier.send(
                "risk",
                f"⛔ Execução MT5 bloqueada na lane {lane_id}: "
                f"{provider.execution_block_reason}. "
                "Nenhuma ordem real será enviada nesta lane.",
                lane=lane_id if lane_id != "main" else "",
            )
        else:
            await notifier.send(
                "system",
                f"⏳ [{lane_id}] MT5 não ligou no arranque — transitória "
                "(worker/terminal a arrancar?); retry em background.",
                lane=lane_id if lane_id != "main" else "",
            )


async def _reconnect_mt5() -> None:
    """Reconnect each configured MT5 lane with one shared throttle.

    A failed secondary account must not affect the primary account. In real
    mode the secondary lane requires its own terminal path; without it the
    provider remains explicitly unavailable instead of sharing a session.
    """
    global _last_mt5_reconnect
    if EXECUTION_MODE != "mt5":
        return
    providers = [
        ("main", mt5_provider),
        ("tp2", _lane2_provider()),
    ]
    for lane_id, provider in providers:
        if (provider is None or provider.execution_block_reason
                or (lane_id == "tp2" and MT5_2_DRY_RUN and not LANE2_WORKER_ENABLED)):
            continue
        if provider.connected or time.time() - _last_mt5_reconnect <= 10:
            continue
        _last_mt5_reconnect = time.time()
        db.record_mt5_connection_event("reconnect_attempt", f"market_loop:{lane_id}")
        try:
            ok = provider.initialize()
        except Exception as exc:
            db.record_mt5_connection_event("reconnect_failure", f"{lane_id}: {exc}")
            _record_runtime_exception(exc, {"loop": "market", "phase": "mt5_reconnect", "lane": lane_id})
            continue
        if ok:
            db.record_mt5_connection_event("reconnect_success", f"market_loop:{lane_id}")
            print(f"[mt5:{lane_id}] religado automaticamente")
            if lane_id in _lane_boot_failed:
                # RECUPERAÇÃO: a lane falhou no boot e o reconnect apanhou-a.
                _lane_boot_failed.discard(lane_id)
                await notifier.send(
                    "system",
                    f"✅ [{lane_id}] MT5 religado após falha de arranque — "
                    "lane recuperada.",
                    lane=lane_id if lane_id != "main" else "",
                )
        else:
            db.record_mt5_connection_event(
                "reconnect_failure", f"{lane_id}: "
                f"{provider.execution_block_reason or 'initialize() devolveu False'}",
            )
    await _lane2_worker_watch()


async def _lane2_worker_watch() -> None:
    """Transições online/offline do worker da lane 2 → notificação + evento.

    O market_loop corre ~1×/s; o ping ao worker é barato (loopback). Um
    worker que cai deixa a 2ª conta parada — o operador tem de saber já, e
    o healthcheck externo reinicia o processo.
    """
    global _lane2_worker_watch_state
    client = _lane2_worker_client
    if client is None or not LANE2_WORKER_ENABLED:
        _lane2_worker_watch_state = {"alive": None}
        return
    alive = client.ping()
    if _lane2_worker_watch_state["alive"] is None:
        _lane2_worker_watch_state["alive"] = alive
        return
    prev = _lane2_worker_watch_state["alive"]
    if prev and not alive:
        _lane2_worker_watch_state["alive"] = False
        db.record_mt5_connection_event("lane2_worker_offline", "watch")
        await notifier.send(
            "risk",
            "🛑 Lane 2 (worker MT5) OFFLINE — a 2ª conta deixa de executar. "
            "O healthcheck reinicia o worker (lane2_worker.log).",
            lane="tp2",
        )
    elif not prev and alive:
        _lane2_worker_watch_state["alive"] = True
        db.record_mt5_connection_event("lane2_worker_online", "watch")
        await notifier.send("info", "✅ Lane 2 (worker MT5) de volta ao ar.",
                            lane="tp2")
    if alive:
        await _lane2_at_check(client)
        await _lane2_quote_watch(client)
        await _lane2_probe_check(client)


async def _lane2_at_check(client) -> None:
    """AutoTrading do terminal 2 (via terminal_info do worker): alerta quando
    OFF (cooldown 10 min — o botão é por-sessão e o operador esquece-se),
    info quando volta a ON. Só observa — nunca altera o terminal."""
    global _lane2_at_state, _last_lane2_at_warn
    try:
        st = client.status_summary()
    except Exception as exc:
        db.record_mt5_connection_event("lane2_at_check_failure", f"{exc}")
        return
    at_on = bool(st.get("trade_allowed")) and bool(st.get("connected"))
    if bool(st.get("connected")) and not at_on:
        if time.time() - _last_lane2_at_warn > 600:
            _last_lane2_at_warn = time.time()
            db.record_mt5_connection_event("lane2_auto_trading_off", "watch")
            await notifier.send(
                "risk",
                f"⛔ AutoTrading DESLIGADO no terminal da lane 2 "
                f"({os.getenv('MT5_2_LOGIN', '5055369549')}) — "
                f"as ordens vão ser recusadas (retcode 10027). Liga o botão "
                f"\"Algo Trading\" na janela do terminal portable.",
                lane="tp2",
            )
        if _lane2_at_state is not False:
            _lane2_at_state = False
    elif at_on and _lane2_at_state is False:
        _lane2_at_state = True
        db.record_mt5_connection_event("lane2_auto_trading_on", "watch")
        await notifier.send(
            "info", "✅ AutoTrading da lane 2 ativo — a 2ª conta pode executar.")


async def _lane2_quote_watch(client) -> None:
    """Watchdog do símbolo na lane 2: XAUUSD sem tick no worker → re-adiciona
    ao Market Watch (`symbol_select`) e alerta.

    Causa do incidente 02/09: o terminal portable ficou sem preço de XAUUSD
    (símbolo fora do Market Watch) e as ordens da lane 2 foram recusadas com
    `NO_TICK`/retcode null sem aviso. Aqui o market_loop verifica a cada 30s
    que o worker tem quote viva; quando não tem, chama `ensure_symbol` para
    voltar a pôr o símbolo no Market Watch e notifica o operador (cooldown de
    10 min por episódio — um símbolo em manutenção não spamma o feed).
    """
    global _last_lane2_quote_check, _lane2_quote_broken_since, _last_lane2_quote_warn
    from app.execution.engine import _now_iso
    now = time.time()
    if now - _last_lane2_quote_check < 30.0:
        return
    _last_lane2_quote_check = now
    try:
        q = client.quote("XAUUSD")
    except Exception:
        q = None
    has_quote = bool(q) and (float(q.get("bid") or 0.0) > 0 or float(q.get("ask") or 0.0) > 0)
    if has_quote:
        if _lane2_quote_broken_since:
            broken_for = _lane2_quote_broken_since
            _lane2_quote_broken_since = None
            db.record_mt5_connection_event("lane2_symbol_recovered", "watch")
            await notifier.send(
                "info",
                f"✅ XAUUSD voltou a ter tick na lane 2 (estava sem quote "
                f"desde {broken_for}).",
                lane="tp2",
            )
        return
    # sem tick: marca o início do episódio e tenta re-adicionar o símbolo
    if _lane2_quote_broken_since is None:
        _lane2_quote_broken_since = _now_iso()
    try:
        client.ensure_symbol("XAUUSD")
        db.record_mt5_connection_event("lane2_symbol_reselect", "watch")
    except Exception as exc:
        db.record_mt5_connection_event("lane2_symbol_reselect_failure", f"{exc}")
    if now - _last_lane2_quote_warn < 600.0:
        return
    _last_lane2_quote_warn = now
    await notifier.send(
            "risk",
            f"⚠️ XAUUSD SEM tick no worker da lane 2 (desde "
            f"{_lane2_quote_broken_since}) — as ordens da 2ª conta vão ser "
            f"recusadas (NO_TICK/retcode null). Re-adicionei o símbolo ao "
            f"Market Watch; se persistir, verifica o terminal portable "
            f"(C:\\MT5Portable2) e o gráfico XAUUSD.",
            lane="tp2",
        )


async def _lane2_probe_check(client) -> None:
    """Alerta quando a auto-sonda do worker falha (módulo MT5 sem resposta).

    O worker corre um `order_send` de símbolo inválido no boot e em cada
    reconnect: módulo vivo ⇒ TradeResult com retcode de erro (custo zero);
    módulo/terminal morto ⇒ None + last_error — o MESMO "retcode null → '?'"
    das ordens reais, mas apanhado no arranque. Aqui o market_loop vigia o
    resultado no /status e alerta com o erro real do módulo (cooldown de 10
    min — um worker partido não spamma o feed)."""
    global _last_lane2_probe_warn
    try:
        st = client.status_summary()
    except Exception:
        return
    probe = st.get("probe") or {}
    if not isinstance(probe, dict) or probe.get("ok") is not False:
        return
    if time.time() - _last_lane2_probe_warn < 600.0:
        return
    _last_lane2_probe_warn = time.time()
    err = probe.get("error") or "sem erro do módulo"
    db.record_mt5_connection_event("lane2_self_probe_failed", "watch")
    await notifier.send(
        "risk",
        f"🧪 Lane 2: auto-sonda do worker falhou — o módulo MT5 não responde "
        f"a order_send ({err}). As ordens reais dariam retcode null ('?'). "
        f"Reinicia o worker (lane2_worker.log) e verifica o terminal portable "
        f"(C:\\MT5Portable2).",
        lane="tp2",
    )


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTENER_LOG = os.path.join(ROOT_DIR, "listener.log")

_NODE_CMD = ""


def _find_node() -> str:
    """Localiza o executável do Node (PATH ou caminho típico do Windows)."""
    global _NODE_CMD
    if _NODE_CMD:
        return _NODE_CMD
    cand = shutil.which("node") or ""
    if not cand and os.name == "nt":
        for p in (r"C:\Program Files\nodejs\node.exe",
                  r"C:\Program Files (x86)\nodejs\node.exe",
                  os.path.expandvars(
                      r"%LOCALAPPDATA%\Programs\nodejs\node.exe")):
            if os.path.exists(p):
                cand = p
                break
    _NODE_CMD = cand
    return cand


def _spawn_whatsapp_bridge() -> subprocess.Popen:
    """Arranca o sidecar Node/Baileys (whatsapp/bridge.js) com os logs em
    whatsapp/bridge.log e whatsapp/bridge.err.log. Usado pelo botão
    "Religar bridge" do Diagnóstico quando o processo node está morto."""
    node = _find_node()
    if not node:
        raise RuntimeError(
            "node.exe não encontrado — instala o Node.js LTS e reinicia"
        )
    wa_dir = os.path.join(ROOT_DIR, "whatsapp")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [node, "bridge.js"],
        cwd=wa_dir,
        stdout=open(os.path.join(wa_dir, "bridge.log"), "ab"),
        stderr=open(os.path.join(wa_dir, "bridge.err.log"), "ab"),
        env=os.environ,
        creationflags=flags,
    )


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
            capture_output=True, text=True, timeout=8,
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
    # servidor anterior), não criar duplicado — o heartbeat chega em ~1s.
    # O subprocesso PowerShell pode demorar segundos (cold start/AV) — fora
    # do event loop para o watchdog nunca congelar os requests.
    if await asyncio.to_thread(_listener_running_anywhere):
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
    # purge periódico: `listener_restarts` (30 dias) e
    # `operational_exceptions` (retenção bounded de 365 dias — o rate-limit
    # por episódio já impede spam idêntico, mas episódios novos continuam a
    # crescer) não podem crescer sem limite — apagados ~ a cada 6h
    if time.time() - _last_restart_purge > 6 * 3600:
        try:
            n = state.db.purge_old_listener_restarts(30)
            if n:
                print(f"[watchdog] purge: {n} reinícios antigos apagados")
        except Exception as exc:
            print(f"[watchdog] purge error: {exc}")
        try:
            m = state.db.purge_old_exceptions(365)
            if m:
                print(f"[watchdog] purge: {m} exceções antigas apagadas")
        except Exception as exc:
            print(f"[watchdog] exceptions purge error: {exc}")
        try:
            ls = state.db.purge_old_loop_stalls(14)
            if ls:
                print(f"[watchdog] purge: {ls} stalls antigos apagados")
        except Exception as exc:
            print(f"[watchdog] loop stalls purge error: {exc}")
        try:
            jd = int(os.getenv("EXECUTION_JOURNAL_RETENTION_DAYS", "30") or 30)
            je = state.db.purge_old_execution_events(jd)
            if je:
                print(f"[watchdog] purge: {je} eventos de execução antigos apagados")
        except Exception as exc:
            print(f"[watchdog] execution events purge error: {exc}")
        _last_restart_purge = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if EXECUTION_MODE == "mt5":
        lanes = [
            (lane_id, provider, dry_run)
            for lane_id, provider, dry_run in (
                ("main", mt5_provider, MT5_DRY_RUN),
                ("tp2", _lane2_provider(), MT5_2_DRY_RUN),
            )
            if provider is not None
        ]
        for lane_id, provider, dry_run in lanes:
            if lane_id == "tp2" and dry_run:
                db.record_mt5_connection_event(
                    "startup_dry_run", f"lifespan.initialize:{lane_id}",
                    details={"dry_run": True, "lane": lane_id},
                )
                print(f"[mt5:{lane_id}] dry-run isolado — sem segunda ligação ao terminal")
        # Boot RÁPIDO: uma única tentativa por lane, em paralelo e fora do
        # loop. O retry (até 10× com sleeps) ficava no lifespan e o uvicorn,
        # que já escuta na porta, só servia depois — janelas de 30-40s em
        # que o healthcheck via "sem resposta HTTP" e matava servidores a
        # meio do boot. Falhou → o reconnect do market_loop (throttle 10s)
        # repete em background assim que o servidor estiver a servir.
        await asyncio.gather(*[
            _initialize_mt5_lane_at_boot(lane_id, provider, dry_run)
            for lane_id, provider, dry_run in lanes
            if not (lane_id == "tp2" and dry_run)
        ])
    # Persiste o arranque do servidor (boot.ps1 arranca servidor + listener):
    # um reinício MANUAL do listener perto desta hora indica reboot da VPS.
    try:
        from app.execution.engine import _now_iso
        db.set_config("server_started_at", _now_iso())
    except Exception as exc:
        print(f"[startup] server_started_at error: {exc}")
    try:
        n = db.purge_old_listener_restarts(30)
        if n:
            print(f"[startup] purge: {n} reinícios antigos apagados")
    except Exception as exc:
        print(f"[startup] purge error: {exc}")
    try:
        m = db.purge_old_exceptions(365)
        if m:
            print(f"[startup] purge: {m} exceções antigas apagadas")
    except Exception as exc:
        print(f"[startup] exceptions purge error: {exc}")
    try:
        ls = db.purge_old_loop_stalls(14)
        if ls:
            print(f"[startup] purge: {ls} stalls antigos apagados")
    except Exception as exc:
        print(f"[startup] loop stalls purge error: {exc}")
    try:
        jd = int(os.getenv("EXECUTION_JOURNAL_RETENTION_DAYS", "30") or 30)
        je = db.purge_old_execution_events(jd)
        if je:
            print(f"[startup] purge: {je} eventos de execução antigos apagados")
    except Exception as exc:
        print(f"[startup] execution events purge error: {exc}")
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
    # RETRY ÚNICO EM BACKGROUND das lanes que falharam no boot: o worker
    # portable da lane 2 arranca pelo boot.ps1 EM PARALELO com o servidor e
    # demora segundos a abrir a porta/ligar o terminal — o 1º initialize
    # do lifespan perde essa corrida. Uma tentativa extra, fora do loop,
    # passado o atraso típico do boot; se falhar, o reconnect do market_loop
    # (throttle 10s) continua a tentar. Task própria do lifespan (cancelada
    # no shutdown, como a stall_watch).
    async def _lane_boot_retry() -> None:
        if not _lane_boot_failed:
            return
        try:
            await asyncio.sleep(float(os.getenv("MT5_BOOT_RETRY_DELAY", "30")))
            lanes = [("main", mt5_provider), ("tp2", _lane2_provider())]
            for lane_id, provider in lanes:
                if lane_id not in _lane_boot_failed or provider is None:
                    continue
                try:
                    ok = bool(await asyncio.to_thread(provider.initialize))
                except Exception as exc:
                    db.record_mt5_connection_event(
                        "boot_retry_failure", f"lifespan.boot_retry:{lane_id}",
                        details={"lane": lane_id, "error": str(exc)},
                    )
                    continue
                db.record_mt5_connection_event(
                    "boot_retry_success" if ok else "boot_retry_failure",
                    f"lifespan.boot_retry:{lane_id}", details={"lane": lane_id},
                )
                if ok:
                    _lane_boot_failed.discard(lane_id)
                    print(f"[mt5:{lane_id}] religado no retry de boot")
                    await notifier.send(
                        "system",
                        f"✅ [{lane_id}] MT5 ligou no retry de arranque — "
                        "lane recuperada.",
                        lane=lane_id if lane_id != "main" else "",
                    )
                elif provider.execution_block_reason:
                    # recuperou a resposta mas é bloqueio PERMANENTE → risk
                    _lane_boot_failed.discard(lane_id)
                    await notifier.send(
                        "risk",
                        f"⛔ Execução MT5 bloqueada na lane {lane_id}: "
                        f"{provider.execution_block_reason}.",
                        lane=lane_id if lane_id != "main" else "",
                    )
        except Exception as exc:
            _record_runtime_exception(exc, {"loop": "boot_retry"})
    boot_retry_task = asyncio.create_task(_lane_boot_retry(), name="nexgen:boot_retry")
    # Vigia do event loop: task PRÓPRIA do lifespan (fora do BackgroundRuntime
    # para não mudar o contrato das suas duas tasks market/watchdog). Deteta
    # congelamentos do loop pelo atraso de wakeup — cancela no shutdown.
    from app.stallwatch import LoopStallRecorder, run_loop_stall_watch
    stall_rec = LoopStallRecorder(db)
    stall_watch_task = asyncio.create_task(
        run_loop_stall_watch(stall_rec), name="nexgen:stall_watch")
    try:
        yield
    finally:
        if not stall_watch_task.done():
            stall_watch_task.cancel()
            try:
                await stall_watch_task
            except asyncio.CancelledError:
                pass
        if not boot_retry_task.done():
            boot_retry_task.cancel()
            try:
                await boot_retry_task
            except asyncio.CancelledError:
                pass
        await background.stop()
        for provider in (mt5_provider, mt5_provider2):
            if provider is not None:
                provider.shutdown()


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


@app.middleware("http")
async def static_no_cache(request: Request, call_next):
    """Static assets/páginas mudam com frequência (sliders, JS, CSS) e o
    browser não deve servir cópias antigas do cache — ex.: o slider da perda
    diária mostrava o valor hardcoded antigo (3.0%) depois de o config já
    estar em 4.5%. Força revalidação em tudo o que não é API/WS."""
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and not path.startswith("/ws"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.post("/api/login")
async def login(body: dict, request: Request):
    if not auth.enabled():
        return {"ok": True, "message": "autenticação desligada"}
    ip = auth.client_ip(request)
    locked = auth.login_locked(ip)
    if locked > 0:
        return JSONResponse(status_code=429, content={
            "error": f"demasiadas tentativas — tenta dentro de {locked}s",
            "retry_after": locked,
        })
    if not auth.check_password(body.get("password", "")):
        auth.record_failed_login(ip)
        remaining = max(0, auth.LOGIN_MAX_ATTEMPTS - len(auth._failed_logins.get(ip, ())))
        return JSONResponse(status_code=401, content={
            "error": "password incorreta",
            "attempts_left": remaining,
        })
    auth.clear_failed_logins(ip)
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
# diagnóstico em `/diagnostico`, auditoria em `/auditoria` e a ajuda em `/help`.
# O middleware de auth já
# protege qualquer path não-API (sem sessão → página de login), por isso estas
# rotas só precisam de servir o HTML.


_ASSET_VERSION: dict = {"token": "", "mtime": 0.0}


def _asset_version() -> str:
    """Token de cache-busting derivado do conteúdo de style.css + common.js.

    Só recalcula quando o mtime de um asset estático muda (o servidor serve os
    ficheiros do disco sem restart, por isso o token tem de acompanhar as
    edições de JS/CSS). O header no-cache continua como primeira defesa; o
    `?v=` é a segunda — cobre proxies/CDNs que ignorem o no-store."""
    try:
        newest = max(
            os.path.getmtime(os.path.join(STATIC_DIR, n))
            for n in os.listdir(STATIC_DIR)
            if n.endswith((".js", ".css", ".html"))
        )
    except (OSError, ValueError):
        newest = 0.0
    if newest != _ASSET_VERSION["mtime"]:
        digest = hashlib.sha1()
        for name in ("style.css", "common.js"):
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    digest.update(f.read())
            except OSError:
                pass
        _ASSET_VERSION["token"] = digest.hexdigest()[:8]
        _ASSET_VERSION["mtime"] = newest
    return _ASSET_VERSION["token"]


def _inject_asset_version(html: str) -> str:
    """Aponta os assets partilhados para URLs com `?v=<token>`."""
    v = _asset_version()
    return (html
            .replace('href="style.css"', f'href="style.css?v={v}"')
            .replace('src="common.js"', f'src="common.js?v={v}"'))


def _static_page(name: str):
    """Serve um ficheiro HTML de app/static (as páginas da dashboard)."""
    path = os.path.join(STATIC_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(_inject_asset_version(f.read()))
    except OSError:
        return HTMLResponse("<h1>Página não encontrada</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def page_index():
    return _static_page("index.html")


@app.get("/segundaria", response_class=HTMLResponse)
async def page_segundaria():
    """Vista da 2ª conta (lane tp2) — o MESMO dashboard do index.html em
    modo `?lane=tp2`: card de conta, posições, histórico, relatório e risco
    filtrados à lane. A página é servida com o parâmetro para o JS aplicar
    os filtros (sem HTML duplicado)."""
    html = _inject_asset_version(
        open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
    )
    # injeta o lane antes do </head> para o boot da página o apanhar sem
    # depender do histórico do browser
    return HTMLResponse(html.replace("</head>",
                                     "<script>try{history.replaceState("
                                     "{},'','?lane=tp2')}catch(e){}</script></head>",
                                     1))


@app.get("/teste", response_class=HTMLResponse)
async def page_teste():
    return _static_page("teste.html")


@app.get("/diagnostico", response_class=HTMLResponse)
async def page_diagnostico():
    return _static_page("diagnostico.html")


@app.get("/help", response_class=HTMLResponse)
async def page_help():
    return _static_page("help.html")


@app.get("/auditoria", response_class=HTMLResponse)
async def page_auditoria():
    return _static_page("auditoria.html")


# ------------------------------------------------------------------ api


def _finite_json(value):
    """Replace non-JSON floats from analytical metrics (e.g. PF=∞).

    Browsers and Starlette reject Infinity/NaN even though Python's encoder
    accepts them by default; live status must remain readable when a strategy
    has wins but no losses yet.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite_json(v) for v in value]
    if isinstance(value, tuple):
        return [_finite_json(v) for v in value]
    return value


_VALID_LANES = {"main", "tp2"}


def _lane_param(lane: str = "") -> str:
    """Normaliza o parâmetro `lane` dos endpoints (vazio = todas)."""
    lane = (lane or "").strip().lower()
    if lane and lane not in _VALID_LANES:
        raise ValueError(f"lane desconhecido: {lane!r} (esperado main|tp2)")
    return lane


@app.get("/api/status")
async def status():
    # Serve o snapshot construído pelo market_loop (~1×/s) — reconstruir o
    # estado aqui (lê o broker MT5/worker, I/O síncrono) bloquearia o event
    # loop único durante o request e congelaria a dashboard/healthcheck.
    return JSONResponse(content=_finite_json(state.last_snapshot()))


@app.get("/api/group-audits")
async def group_audits(limit: int = 100):
    """Passive group progress/level evidence; never drives execution."""
    return {"ok": True, "audits": db.list_group_audits(limit=max(1, min(int(limit), 1000)))}


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    result = await state.update_config(body)
    state._invalidate_snapshot_cache()
    return result


@app.post("/api/killswitch")
async def killswitch(body: KillSwitchUpdate):
    result = await state.set_kill_switch(body.enabled)
    state._invalidate_snapshot_cache()
    return result


@app.post("/api/signals")
async def add_signal(body: SignalIn):
    result = await state.ingest(body)
    state._invalidate_snapshot_cache()
    return result


# Item 12: o endpoint de aprovação manual foi removido — os sinais completos
# executam sozinhos após as verificações de risco (único caminho, auto).

@app.post("/api/signals/{signal_id}/confirm")
async def confirm_signal(signal_id: str, body: dict = {}):
    """Confirmação explícita de um sinal bloqueado por guard de entrada
    (fora da zona/tolerância) ou por lote mínimo acima do risco configurado.
    A confirmação é única, auditável e nunca contorna kill switch/perda diária."""
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    result = await state.confirm_signal(signal_id, reason=reason)
    state._invalidate_snapshot_cache()
    return result


@app.post("/api/signals/{signal_id}/reject")
async def reject(signal_id: str):
    """Descarta um sinal bloqueado por guard de entrada/risco (Item 12: não
    há aprovação manual geral — esta rota é só para dispensar esses sinais)."""
    result = await state.reject(signal_id)
    state._invalidate_snapshot_cache()
    return result


@app.post("/api/signals/{signal_id}/delete")
async def delete_signal(signal_id: str):
    result = await state.delete_signal(signal_id)
    state._invalidate_snapshot_cache()
    return result


@app.get("/api/report")
async def report(days: int = 7, lane: str = ""):
    """Relatório de qualidade (win rate, R:R, por grupo).

    `lane` (main|tp2) filtra as trades fechadas da conta — o drawdown e o
    P&L passam a referir-se ao saldo dessa lane.
    """
    try:
        return state.report(days, lane=_lane_param(lane))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/window")
async def window():
    """Real-trade review window through 28 August UTC plus promotion status."""
    return state.window_status()


@app.get("/api/recounts")
async def recounts(source: str = "", limit: int = 200):
    """R2: per-trade recounts from the group (read-only ground truth).

    Each row is a "Signal #N: Sell at … +N pips" message the admin posted;
    the bot compares them with its own real fills of the same day (see
    `_recount_fill_audit`) to detect divergent entries/pips, direction
    mismatches and the excursion/reversal reported vs the real candle path
    of the day. Each recount comes with its fill audit (`matched`/`alert`)
    plus a weekly summary so the dashboard can mark divergences. This
    endpoint only reads — it never opens or closes trades.
    """
    return await state.recounts_report(
        source=source, limit=min(max(1, int(limit)), 500),
    )


@app.get("/api/recounts/batches")
async def recounts_batches(limit: int = 60):
    """Historial persistido das corridas do re-check em lote (R2).

    Cada corrida (diária/semanal) guarda o contador 'resolvidos' e os flips
    (data, recount, de → para) para ver a evolução das correções do offset
    GC=F↔spot ao longo do tempo, sem abrir tabelas. Read-only.
    """
    return {"batches": state.db.list_recount_batches(limit=min(max(1, int(limit)), 200))}


@app.post("/api/recounts/recheck-all")
async def recounts_recheck_all(days: int = 90):
    """Re-corre o candle-check de TODOS os recounts em lote (R2).

    Refaz o fill-audit + candle-check de cada recount com candles frescos
    (buscados uma vez por símbolo) e o offset GC=F↔spot mais recente, e
    persiste o veredicto em `trade_recounts.candle_check` (+ `checked_at`).
    `days=0` re-corre o histórico COMPLETO (corrida semanal). Read-only para
    a execução: não abre/fecha trades, não altera o journal.
    """
    try:
        return await state.recheck_all_recounts(days=max(0, int(days)))
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)



@app.post("/api/recounts/{recount_id}/recheck")
async def recounts_recheck(recount_id: int):
    """Re-corre o candle-check de um recount com candles frescos (R2).

    O Yahoo pode ter corrigido/expandido o histórico desde a primeira
    análise; este endpoint refaz o fill-audit + candle-check e devolve o
    veredicto atualizado. Read-only para a execução: não altera a linha
    guardada nem o journal, não abre/fecha trades.
    """
    try:
        return await state.recheck_recount(recount_id)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/operations/daily-audit")
async def operations_daily_audit(day: str = "", start: str = "", end: str = "",
                                 lane: str = ""):
    """Reconcilição read-only de sinais, trades, ordens, journal e kill diário."""
    try:
        lane = _lane_param(lane)
        # Pass a broker snapshot only to this read-only report. No order or
        # account mutation is possible through the audit helper.
        broker = None
        try:
            execution = state._execution_state()
            broker = execution.get("broker_account")
            if broker is not None:
                broker = dict(broker)
                broker["positions"] = execution.get("mt5_positions") or state._mt5_positions()
                if lane:
                    broker["positions"] = [p for p in (broker["positions"] or [])
                                            if (p.get("lane_id") or "main") == lane]
        except Exception:
            broker = None
        db._audit_broker_state = broker
        try:
            audit = daily_execution_audit(db, day=day, start=start, end=end, lane=lane)
        finally:
            try:
                del db._audit_broker_state
            except AttributeError:
                pass
        # Contrafactual BE+ com velas: usa as MESMAS fontes do candle-check
        # dos recounts (spot MT5 ligado + GC=F Yahoo), cache de 5 min.
        try:
            be_plus = audit.get("be_plus") or {}
            events = be_plus.get("stop_events") or []
            if events:
                symbols = {e["symbol"] for e in events}
                candles = None
                for sym in symbols:
                    sources = await state._recount_candles(sym)
                    candles = sources.get("spot") or sources.get("futures")
                    if candles:
                        break
                if candles:
                    from app.execution.audit import _be_plus_counterfactual
                    cf = _be_plus_counterfactual(candles, events)
                    be_plus.update(cf)
                else:
                    be_plus["kept_running_to_tp2"] = None
                    be_plus["checked"] = 0
        except Exception:
            pass
        return audit
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/operations")
async def operations(days: int = 90):
    """Item 10: exceções, spread mensal e estabilidade MT5 agregados.

    Os dados são observações persistidas; a dashboard mostra explicitamente
    quando ainda não há amostra suficiente para uma tendência.
    """
    return state.operational_metrics(days=days)


@app.get("/api/operations/journal-quality")
async def operations_journal_quality(day: str = "", start: str = "", end: str = ""):
    """UTC journal-quality report: coverage, fills, retcodes and latencies."""
    try:
        return journal_quality_report(db, day=day, start=start, end=end)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/operations/events")
async def operations_events(since: str = "", until: str = "", kind: str = "", limit: int = 5000):
    """Read-only execution journal for deterministic replay/audit.

    Rows exist only when ``execution_journal_enabled`` is active; the
    endpoint never drives an engine or mutates a trade.
    """
    return {
        "ok": True,
        "enabled": bool(state.config.get("execution_journal_enabled")),
        "events": db.execution_events(since=since, until=until, kind=kind, limit=limit),
    }


@app.get("/api/operations/replay/tape")
async def operations_replay_tape(since: str = "", until: str = "",
                                 signal_id: str = "", trade_id: str = "",
                                 limit: int = 10000):
    """Export a replayable tape by joining the opt-in journal to signals.

    This is read-only. ``signal_id``/``trade_id`` narrow the export to one
    trade; without them the requested journal interval is exported.
    """
    try:
        return {"ok": True, **db.execution_tape(
            since=since, until=until, signal_id=signal_id,
            trade_id=trade_id, limit=max(1, min(int(limit), 20000)),
        )}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/operations/replay/outages")
async def operations_replay_outages(days: int = 30, start: str = "",
                                    until: str = ""):
    """Worker-outage timeline from the opt-in journal (read-only).

    Junta as falhas `pending_order_result` (action=delete) com as marcas
    `pending_delete_stuck` do watchdog e agrupa episódios por (trade, ticket)
    — uma ordem pendente que o bot tentou cancelar e não conseguiu (worker
    offline). A fita é exportada por `GET /api/operations/replay/tape`;
    este relatório é a leitura humanizada para o /auditoria. Nunca executa
    nada: é observação passiva dos eventos já gravados.
    """
    try:
        from app.execution.evidence import worker_outage_timeline
        return worker_outage_timeline(db, days=days, start=start, end=until)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/operations/replay/snapshots")
async def operations_replay_snapshots(since: str = "", until: str = "",
                                      signal_id: str = "", trade_id: str = "",
                                      step: int = -1, limit: int = 10000):
    """Replay an exported tape and expose post-event snapshots read-only.

    ``step`` selects one snapshot (zero-based); ``-1`` returns all snapshots.
    The engines always use temporary databases and MT5 dry-run.
    """
    try:
        exported = db.execution_tape(
            since=since, until=until, signal_id=signal_id,
            trade_id=trade_id, limit=max(1, min(int(limit), 10000)),
        )
        report = run_replay(exported["events"], include_snapshots=True)
        snapshots = report.get("snapshots", [])
        if step >= 0:
            snapshots = [snapshots[step]] if step < len(snapshots) else []
        return {
            "ok": True, "tape": exported["meta"],
            "snapshots": snapshots,
            "parity": report.get("parity"),
            "by_signal": report.get("by_signal", {}),
        }
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/operations/replay")
async def operations_replay(body: dict = {}):
    """Run a supplied execution tape in isolated temporary databases.

    This is a read-only study endpoint: it never uses the production DB or
    broker, and the MT5 adapter is forced to dry-run. The caller supplies
    ``{"events": [...], "risk_pct": 1.0, "balance": 1000}``.
    """
    rows = body.get("events", []) if isinstance(body, dict) else []
    if not isinstance(rows, list):
        return JSONResponse({"ok": False, "error": "events deve ser uma lista"}, status_code=400)
    if len(rows) > 10000:
        return JSONResponse({"ok": False, "error": "máximo de 10000 eventos"}, status_code=400)
    try:
        return run_replay(
            rows,
            risk_pct=float(body.get("risk_pct", 1.0)),
            balance=float(body.get("balance", 1000.0)),
            strategy_id=str(body.get("strategy_id", "nova_gestao")),
            include_snapshots=bool(body.get("include_snapshots", True)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/recent-strategies")
async def analysis_recent_strategies(since: str = "2026-08-28", group: str = "Golden Moves",
                                     balance: float = 1000.0, risk_pct: float = 1.0):
    """Comparação read-only da coorte recente entre três estratégias."""
    try:
        return await recent_strategy_comparison(
            db, since=since, group=group, balance=balance, risk_pct=risk_pct,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis")
async def analysis(group: str = "", days: int = 30,
                  risk_pct: float = 0.0, balance: float = 0.0):
    """Backtest dos sinais reais de um grupo (edge analysis) — nunca abre trades.
    `risk_pct`/`balance` opcionais (0 = config/saldo atual) para estudos de
    risco — o piso de lote mínimo domina em saldos pequenos. Estratégias
    removidas do registo devolvem 400 (nenhuma corrida silenciosa com a
    principal)."""
    try:
        return await state.edge_analysis(group=group, days=days,
                                         risk_pct=risk_pct or None,
                                         sim_balance=balance or None)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/compare")
async def analysis_compare(group: str = "", days: int = 30, since: str = "",
                           risk_pct: float = 0.0, balance: float = 0.0):
    """A/B: TP1..TP5 (nova) vs + gestão vs fechar no TP2 — nunca abre trades.
    `since` (YYYY-MM-DD) filtra o período (ex.: só o formato atual);
    `risk_pct`/`balance` opcionais (0 = config/saldo atual) para estudos."""
    return await state.compare_analysis(group=group, days=days, since=since,
                                        risk_pct=risk_pct or None,
                                        sim_balance=balance or None)


@app.get("/api/analysis/resolution")
async def analysis_resolution(group: str = "", days: int = 7,
                              strategy: str = "", interval: str = "1m",
                              delay_seconds: int = 60,
                              risk_pct: float = 0.0, balance: float = 0.0,
                              include_delays: bool = True):
    """Compara resolução temporal (M1 quando disponível) e latência.

    Yahoo só conserva M1 por ~7 dias; pedidos maiores devolvem o fallback
    efetivo e a razão no payload, sem apresentar uma amostra parcial como M1.
    """
    try:
        return await state.resolution_analysis(
            group=group, days=days, strategy_id=strategy or None,
            resolution=interval, delay=delay_seconds,
            risk_pct=risk_pct or None, sim_balance=balance or None,
            include_delays=include_delays,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/protect_latency")
async def analysis_protect_latency(strategy: str = "auto_escada_3_hibrida",
                                   engine: str = "", limit: int = 500,
                                   days: int = 0, since: str = ""):
    """Latência do protect híbrido: decisões paper/MT5 persistidas ou
    backtest recalculado quando ``engine=backtest``."""
    try:
        return await state.protect_latency_report(
            strategy_id=strategy, engine=engine, limit=limit,
            days=days, since=since,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/protect_causal")
async def analysis_protect_causal(strategy: str = "auto_escada_3_hibrida",
                                  limit: int = 500, days: int = 0,
                                  since: str = ""):
    """Atribuição causal dos eventos ``protect`` da híbrida no replay
    histórico: delta contra a mesma auto sem o protect, por evento."""
    try:
        return await state.protect_causal_report(
            strategy_id=strategy, limit=limit, days=days, since=since,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


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
    """Registo central de estratégias + qual é a principal + o MENU do chrome.

    `menu` = as estratégias visíveis (fora de HIDDEN/MENU_HIDDEN) na ordem do
    registo, com id/label/sub — o common.js re-hidrata o dropdown ▚
    ESTRATÉGIAS a partir daqui, por isso adicionar uma estratégia ao registo
    basta para ela aparecer no menu de todas as páginas (sem tocar em JS).
    """
    from app.strategies import MAIN_STRATEGY_ID, STRATEGIES
    payload = state.strategies_info()
    payload["menu"] = [
        {"id": sid, "label": s["nome"], "sub": s.get("menu_sub") or ""}
        for sid, s in STRATEGIES.items()
        if not (payload.get("strategies") and next(
            (r for r in payload["strategies"] if r["id"] == sid), {}
        ).get("hidden"))
    ]
    payload["main"] = MAIN_STRATEGY_ID
    return payload


@app.get("/api/paper/history")
async def paper_history(hours: int = 24):
    """Histórico de saldos da corrida paralela (por estratégia): as de teste
    com a conta virtual, a principal com o saldo real da conta MT5 — para a
    mini-curva "quem está a ganhar" na dashboard (principal + tab Edge)."""
    from datetime import datetime, timedelta, timezone
    from app.strategies import (HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES,
                                STRATEGIES, strategy_lifecycle)
    main = state.snapshot().get("config", {}).get("strategy")
    # até 30 dias (720h) para o seletor da dashboard (24h / 7d / 30d)
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat(timespec="seconds")
    out = []
    # a curva mostra só as estratégias visíveis + a real: as REMOVIDAS
    # (HIDDEN_STRATEGIES) e as ocultas do menu (MENU_HIDDEN_STRATEGIES —
    # nova/auto_5 continuam a ABRIR papel mas ficam FORA da curva, como a
    # matriz do ESTRATEGIAS.md Parte IX define) não entram.
    for sid, strat in STRATEGIES.items():
        if sid in MENU_HIDDEN_STRATEGIES or not strategy_lifecycle(sid)["menu"]:
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


@app.get("/api/lanes/divergences")
async def lane_divergences():
    """Per-signal divergence between execution lanes (main vs tp2).
    Shows P&L side by side and exit reasons for each signal traded in both lanes."""
    return {"ok": True, "divergences": state.lane_divergences()}


@app.get("/api/lanes/status")
async def lane_status():
    """Per-lane summary: strategy, mode, connected, total P&L, trade count."""
    return {"ok": True, "lanes": state.lane_status()}


@app.get("/api/lanes/ab")
async def lane_ab(min_comparable: int = 20):
    """A/B read-only das lanes (main vs tp2) no mesmo sinal.

    Compara trades fechadas nas DUAS lanes para o mesmo sinal (P&L lado a
    lado, win rate, PF) e só sugere manutenção com ≥ `min_comparable` sinais
    comparáveis — abaixo disso o veredicto é neutro. Junta o dimensionamento
    de cada lane (risco base/lote fixo por perna) para a comparação ler com
    os tamanhos de posição em contexto. Nunca altera estratégia, config nem
    envia ordens.
    """
    report = state.lane_ab_report(
        min_comparable=max(1, min(int(min_comparable), 100)))
    if isinstance(report, dict) and report.get("ok"):
        # O dimensionamento lê a BD + conta; fora do loop para o Diagnóstico
        # (refresh 10s) nunca congelar outros requests.
        sizings = await asyncio.gather(*[
            asyncio.to_thread(_lane_sizing_diag, lane_id)
            for lane_id in ("main", "tp2") if lane_id in report
        ])
        for lane_id, sizing in zip(
                [lane_id for lane_id in ("main", "tp2") if lane_id in report],
                sizings):
            report[lane_id]["sizing"] = sizing
    return JSONResponse(content=_finite_json(report))


@app.get("/api/lanes/ab/legs")
async def lane_ab_legs(signal_id: str = ""):
    """Confronto perna-a-perna de um sinal entre as lanes (main vs tp2):
    fills, moves de SL, timing e P&L por perna — reutiliza `/api/lanes/ab`
    (P&L por sinal) e a fita do journal para a linha temporal. Read-only.
    """
    if not signal_id:
        return JSONResponse({"ok": False, "error": "signal_id é obrigatório"},
                            status_code=400)
    try:
        from app.lanes import lane_ab_legs as _legs
        return await asyncio.to_thread(_legs, db, str(signal_id))
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/lanes/smoke")
async def lanes_smoke_test():
    """Smoke test GUIADO dual-lane — prova de execução real de 0.01 lote em
    CADA conta demo (principal + 2ª).

    Abre e fecha a mercado uma trade real de 0.01 (XAUUSD — o broker da
    lane 2 recusa EURUSD com 10030) em cada lane pelas engines reais
    (worker isolado → 2ª conta demo; processo principal → 1ª conta demo),
    com as DUAS trades a partilhar o MESMO sinal:
    escreve as rows na BD, emite os eventos `close`/`trade_closed` e dispara
    a notificação WhatsApp dual-lane com as duas contas lado a lado e o P&L
    somado — o caminho completo de um fecho real sem depender de um sinal
    do grupo.

    GUARDAS demo-only (sempre): opt-in `LANE2_SMOKE_ALLOW=1` no .env, as
    DUAS lanes em execução REAL (dry_run=False) com terminal/worker ligados
    e AutoTrading ON, e as contas TEM de ser demo (`trade_mode == 0`) — uma
    conta real/contest recusa antes de construir qualquer pedido. Cooldown
    entre corridas (`LANE2_SMOKE_COOLDOWN`, default 120 s). Read-only para
    as estratégias: nunca toca na config nem decide nada.
    """
    from app.smoke import (notify_smoke, run_smoke,
                           smoke_guard_error, smoke_report_lines)
    engines = {"main": sim, "tp2": sim2}
    # fast-fail 409 com a lane que recusou (ex.: "[main] AutoTrading OFF …")
    for lane_id, engine in engines.items():
        guard = smoke_guard_error(engine, db, label=lane_id)
        if guard:
            return JSONResponse({"ok": False, "error": guard},
                                status_code=409)
    try:
        report = await asyncio.to_thread(run_smoke, engines, db)
    except Exception as exc:
        db.record_exception(exc, context={"component": "lanes",
                                          "phase": "smoke_test"})
        return JSONResponse(
            {"ok": False, "error": f"smoke test rebentou: {exc}"},
            status_code=500)
    if report.get("ok"):
        try:
            db.set_config("lane2_smoke_last_ts", time.time())
        except Exception:
            pass
        db.record_mt5_connection_event(
            "lane2_smoke_ok",
            f"{report.get('symbol')} 0.01 2 lanes pnl "
            f"{report.get('total_pnl')}",
        )
        try:
            await notify_smoke(state, report)
        except Exception as exc:
            db.record_exception(
                exc, context={"component": "lanes", "phase": "smoke_notify"})
    else:
        db.record_mt5_connection_event(
            "lane2_smoke_failed", str(report.get("error"))[:200])
    report["summary"] = smoke_report_lines(report)
    payload = {"ok": bool(report.get("ok")), **report}
    return JSONResponse(content=_finite_json(payload))


@app.get("/api/trades/refusals")
async def trade_refusals(limit: int = 10):
    """Últimas recusas do broker (read-only): trades com `error` gravado ou
    o padrão phantom (fechadas sem `exit_reason` a 0.0 — ordens recusadas
    no envio). Devolve o trace durável: retcodes, flag AutoTrading OFF e
    o erro exato — para o Diagnóstico explicar o que aconteceu."""
    return {"ok": True, "items": state.db.refused_trades(
        limit=max(1, min(int(limit), 50)))}


@app.get("/api/analysis/fill-audit")
async def analysis_fill_audit(days: int = 30, start: str = "", end: str = "",
                              trade_id: str = "", limit: int = 50):
    """Read-only broker fills versus the isolated replay, with first mismatch."""
    try:
        return compare_broker_fills(db, days=days, start=start, end=end,
                                    trade_id=trade_id, limit=limit)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/forward-gate")
async def analysis_forward_gate(days: int = 30, start: str = "", end: str = ""):
    """Non-promoting forward gate for shadow auto strategies."""
    try:
        return forward_gate_report(db, days=days, start=start, end=end)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/audit-export")
async def analysis_audit_export(days: int = 30, start: str = "", end: str = "",
                               format: str = "json"):
    """Export a combined journal/fill/gate bundle as JSON or CSV.

    This is read-only with respect to execution; it only evaluates persisted
    journal rows, isolated replay and shadow accounts.
    """
    try:
        report = audit_report(db, days=days, start=start, end=end)
        if str(format).lower() == "csv":
            return Response(
                content=audit_csv(report), media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=nexgen-auditoria.csv"},
            )
        return JSONResponse(
            report,
            headers={"Content-Disposition": "attachment; filename=nexgen-auditoria.json"},
        )
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/analysis/forward-gate/archive")
async def archive_forward_gate(days: int = 30, start: str = "", end: str = ""):
    """Archive one frozen forward window without touching execution."""
    try:
        report = audit_report(db, days=days, start=start, end=end)
        period = report["period"]
        metadata = db.archive_evidence(
            "forward_gate", period["start"], period["end"], report,
        )
        return {"ok": True, "archive": metadata, "report": report["forward_gate"]}
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/forward-gate/archives")
async def list_forward_gate_archives(limit: int = 50):
    """List archived forward windows; metadata only by default."""
    return {"ok": True, "archives": db.evidence_archives("forward_gate", limit)}


@app.get("/api/analysis/forward-gate/archives/{archive_id}")
async def get_forward_gate_archive(archive_id: int):
    archive = db.evidence_archive(archive_id)
    if not archive or archive.get("kind") != "forward_gate":
        return JSONResponse({"ok": False, "error": "arquivo não encontrado"}, status_code=404)
    return {"ok": True, "archive": archive}


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


@app.post("/api/analysis/pre-real-decision")
async def pre_real_decision(body: PreRealDecisionIn, request: Request):
    """Regista uma decisão humana explícita sem alterar a execução."""
    if str(body.decision).strip().upper() != "GO":
        return {"ok": False, "error": "só é possível registar a decisão GO"}
    confirmation = str(body.confirmation).strip()
    if len(confirmation) < 12:
        return {"ok": False, "error": "a confirmação textual é obrigatória"}
    checklist = await pre_real_checklist()
    if checklist.get("verdict") != "GO":
        return {"ok": False, "error": "decisão bloqueada: o checklist ainda está NO-GO", "checklist": checklist}
    import json as _json
    from datetime import datetime, timezone
    entry = {
        "decision": "GO",
        "confirmation": confirmation,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": request.headers.get("x-user") or "dashboard-user",
        "checklist": checklist,
    }
    db.set_config("real_account_decision", _json.dumps(entry, ensure_ascii=False))
    return {"ok": True, "decision": entry}


@app.get("/api/analysis/pre-real-checklist")
async def pre_real_checklist():
    """Checklist read-only consolidado para decisão pré-conta-real.

    Ausência de evidência é deliberadamente NO-GO; este endpoint nunca altera
    configuração nem promove a execução.
    """
    from datetime import datetime, timezone
    from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    forward = forward_gate_report(db, days=30)
    fase3 = await state.fase3_status()
    diagnostics_payload = await diagnostics()
    ex = diagnostics_payload.get("execution") or {}
    mt5 = diagnostics_payload.get("mt5") or {}
    listener = diagnostics_payload.get("listener") or {}
    disk = (state.snapshot().get("operations") or {}).get("disk") or {}
    cfg = state.config
    strategies = forward.get("strategies") or []
    forward_ok = bool(strategies) and all(bool(x.get("eligible")) for x in strategies)
    f3v = fase3.get("verdict") or {}
    fase3_ok = bool(f3v) and f3v.get("decision") == "maintain"
    health = {
        "server": True,
        "mt5_connected": bool(mt5.get("connected")),
        "mt5_hedged": mt5.get("hedged") is True,
        "autotrading": bool(mt5.get("trade_allowed")),
        "listener": bool(listener.get("alive")),
        "reconciliation": bool((diagnostics_payload.get("reconciliation") or {}).get("safe", True)),
        "disk": not bool(disk.get("below_threshold")),
    }
    health_ok = all(health.values())
    alerts = {
        "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "risk_notifier": bool(getattr(state, "notifier", None)),
        "auto_kill": bool(cfg.get("auto_kill")),
        "daily_kill_limit": float(cfg.get("daily_loss_limit_pct", 0)) == 3.0,
        "max_positions": int(cfg.get("max_positions", 0)) == 5,
    }
    alerts_ok = all(alerts.values())
    stored_decision = cfg.get("real_account_decision", "")
    try:
        stored_payload = json.loads(stored_decision) if isinstance(stored_decision, str) else stored_decision
    except (TypeError, ValueError):
        stored_payload = {}
    human_ok = isinstance(stored_payload, dict) and str(stored_payload.get("decision", "")).upper() == "GO"
    gates = [
        {"id": "forward_gate", "label": "Forward gate shadow", "ok": forward_ok, "evidence": forward.get("verdict", "collect_more_evidence")},
        {"id": "fase3", "label": "Fase 3 MT5 demo", "ok": fase3_ok, "evidence": f3v.get("decision", "sem veredicto")},
        {"id": "operational_health", "label": "Saúde operacional", "ok": health_ok, "evidence": health},
        {"id": "alerts", "label": "Alertas e limites", "ok": alerts_ok, "evidence": alerts},
        {"id": "human_decision", "label": "Decisão explícita do utilizador", "ok": human_ok, "evidence": "GO registado" if human_ok else "não registada"},
    ]
    return {"ok": True, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "verdict": "GO" if all(g["ok"] for g in gates) else "NO-GO",
            "gates": gates, "parameters": {"balance": 1000.0, "risk_pct": 1.0,
            "daily_loss_limit_pct": cfg.get("daily_loss_limit_pct"), "max_positions": cfg.get("max_positions"),
            "kill_switch": cfg.get("kill_switch"), "auto_kill": cfg.get("auto_kill")},
            "forward_gate": forward, "fase3": {"decision": f3v.get("decision"), "verdict": f3v},
            "health": health, "alerts": alerts, "human_decision": human_ok,
            "decision": stored_payload if human_ok else None}


@app.get("/api/analysis/fase3_status")
async def analysis_fase3_status():
    """Payload único do painel Fase 3 (uma chamada em vez de 4): ETA (com a
    curva do ritmo), snapshot antes/depois, veredictos + histórico, registo
    de alterações de alvo, divergences paper↔MT5, real vs backtest, DD das
    contas e auditoria de SL."""
    return await state.fase3_status()


@app.get("/api/analysis/rejections")
async def analysis_rejections(days: int = 30, risk_pct: float = 0.0,
                              balance: float = 0.0, max_replay: int = 150,
                              strategy_id: str = ""):
    """'Porquê sem trade' (Fase 2): sinais rejeitados/falhados nos últimos
    `days` dias, contados por categoria (zona, lote mínimo, risco global,
    expiração, broker, parse…) e, para cada sinal parseável, o que TERIA
    acontecido (replay com candles reais + gestão — read-only, nunca abre
    trades nem escreve na BD). `strategy_id` muda os parâmetros do replay
    (max_legs/gestão da estratégia escolhida nas páginas de teste)."""
    from app.analysis import rejection_report
    return await rejection_report(
        state.db, days=days, risk_pct=risk_pct or None,
        balance=balance or None, max_replay=max_replay,
        strategy_id=strategy_id or None,
    )


@app.get("/api/analysis/trace")
async def analysis_trace(strategy: str = "nova", group: str = "",
                         days: int = 0, since: str = ""):
    """Exemplo do fluxo real de uma trade (página de teste): o sinal
    resolvido mais recente da estratégia + eventos SL/TP1/BE/fecho + caminho
    de preços. Só para estratégias sem gestão (nova, auto_3, auto_5)."""
    try:
        return await state.trace_signal(strategy_id=strategy, group=group,
                                        days=days, since=since)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/analysis/month")
async def analysis_month(group: str = "", days: int = 30, strategy: str = "",
                         since: str = "",
                         risk_pct: float = 0.0, balance: float = 0.0):
    """Simulação do último mês para uma estratégia (default = principal):
    sinais reais desde há `days` dias até hoje (ou `since=YYYY-MM-DD` para
    filtrar a partir de uma data — formato atual vs histórico completo),
    começando com o saldo atual — nunca abre trades. `risk_pct`/`balance`
    opcionais (0 = config/saldo atual) para estudos de risco."""
    try:
        return await state.month_simulation(group=group, days=days,
                                            strategy_id=strategy or None,
                                            since=since,
                                            risk_pct=risk_pct or None,
                                            sim_balance=balance or None)
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/simulate")
async def simulate(body: SimulateIn):
    return await state.simulate(body)


@app.post("/api/summary")
async def summary():
    return await state.daily_summary()


@app.get("/api/trades")
async def trades(lane: str = ""):
    """Histórico de trades (`lane` filtra main|tp2)."""
    try:
        lane = _lane_param(lane)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    rows = db.list_trades(100)
    if lane:
        rows = [t for t in rows if (getattr(t, "lane_id", None) or "main") == lane]
    return [t.to_dict() for t in rows]


@app.get("/api/positions")
async def positions(lane: str = ""):
    """Posições abertas (`lane` filtra main|tp2) — read-only, é o espelho
    das posições do snapshot para consumo programático."""
    try:
        lane = _lane_param(lane)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    rows = db.open_positions()
    if lane:
        rows = [p for p in rows if (getattr(p, "lane_id", None) or "main") == lane]
    return [p.to_dict() for p in rows]


@app.get("/api/notifications")
async def notifications():
    return [
        {"id": n.id, "kind": n.kind, "text": n.text,
         "created_at": n.created_at, "lane": n.lane or ""}
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


_HEALTHCHECK_CACHE: dict = {"at": 0.0, "data": None}


def _healthcheck_status() -> dict:
    """Estado do health check externo (healthcheck.py + Tarefa NexGen Health).

    Lê `healthcheck.state` (deteção offline / último restart) e a cauda de
    `healthcheck.log`. A info da Tarefa Agendada (`schtasks`) é cara e a
    Diagnóstico faz polling a cada 10s — por isso o query da tarefa fica
    cacheado ~60s. Tudo é tolerante a ausência (servidor recém-arrancado
    antes do primeiro check, ficheiros apagados, schtasks a falhar).
    """
    base = Path(__file__).resolve().parent.parent

    def _read_state() -> dict:
        try:
            return json.loads((base / "healthcheck.state").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _log_tail() -> tuple[list[str], bool]:
        try:
            lines = (base / "healthcheck.log").read_text(
                encoding="utf-8", errors="replace").splitlines()
            return lines[-6:], True
        except Exception:
            return [], False

    st = _read_state()
    tail, log_exists = _log_tail()
    now = time.time()

    cache = _HEALTHCHECK_CACHE
    if cache["data"] is None or now - cache["at"] >= 60:
        task = None
        if os.name == "nt":
            try:
                out = subprocess.run(
                    ["schtasks", "/query", "/tn", "NexGen Health", "/fo", "LIST", "/v"],
                    capture_output=True, text=True, timeout=10, check=False).stdout or ""
                bits: dict = {}
                for line in out.splitlines():
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        bits[k.strip().lower()] = v.strip()
                task = {
                    "status": bits.get("status"),
                    "last_run": bits.get("last run time"),
                    "last_result": bits.get("last result"),
                    "next_run": bits.get("next run time"),
                }
            except Exception:
                task = None
        cache.update({"at": now, "data": task})

    # last_result vem em hexadecimal ("0") ou "The operation completed successfully (0)"
    last_res = cache["data"].get("last_result") if cache["data"] else ""
    last_res_int = None
    try:
        last_res_int = int(last_res.split("(")[-1].strip(")")) if last_res else None
    except (ValueError, AttributeError, IndexError):
        last_res_int = None

    return {
        "task": cache["data"],
        "last_task_result": last_res_int,
        "log_exists": log_exists,
        "log_tail": tail,
        "down": bool(st.get("down")),
        "consecutive_failures": int(st.get("consecutive_failures", 0) or 0),
        "last_failure_at": st.get("last_failure_at"),
        "last_restart_at": st.get("last_restart_at"),
        "restarted_at": st.get("restarted_at"),
    }


def _lane_sizing_diag(lane_id: str = "main") -> dict:
    """Dimensionamento de UMA lane para o Diagnóstico/A-B (read-only): risco
    base por conta (`risk_pct:<lane>`), risco efetivo (pós-drawdown) e o
    lote fixo por perna (`lots_override:<lane>` quando definido) + preview
    por perna. Tolerante: devolve vazio se o estado não estiver construído
    ou a lane não estiver configurada."""
    try:
        preview = state.risk_preview(lane_id)
        return {
            "risk_base_pct": round(state._base_risk_for_lane(lane_id), 2),
            "effective_risk_pct": round(state._effective_risk_pct_for_lane(lane_id), 2),
            "fixed_lots_per_leg": state._fixed_lots_for_lane(lane_id),
            "lots_per_leg": round(float(preview.get("lots_per_leg") or 0.0), 4),
            "lots_total": round(float(preview.get("lots_total") or 0.0), 4),
            "n_legs": preview.get("n_legs"),
            "lots_override": preview.get("lots_override"),
            "sl_dist": preview.get("sl_dist"),
            "symbol": preview.get("symbol"),
        }
    except Exception:
        return {}


def _lane2_sizing_diag() -> dict:
    """Dimensionamento da lane 2 (wrapper do caso tp2 para o Diagnóstico)."""
    return _lane_sizing_diag("tp2")


def _lane2_worker_diag() -> dict:
    """Estado do worker da lane 2 para o Diagnóstico (read-only, tolerante).

    O worker só existe quando `LANE2_WORKER_ENABLED=1`; sem ele a lane 2
    corre em dry-run e o bloco diz isso. Quando ativo: reachable/connected/
    login/block_reason/last_sync/last_error do RemoteProvider + processo a
    escutar na porta + cauda do lane2_worker.log.
    """
    base = Path(__file__).resolve().parent.parent
    out = {
        "enabled": LANE2_WORKER_ENABLED,
        "port": LANE2_WORKER_PORT,
        "login": os.getenv("MT5_2_LOGIN", ""),
        "mode": "real" if LANE2_WORKER_ENABLED else "dry-run",
        "reachable": False,
        "connected": False,
        "block_reason": "",
        "trade_allowed": False,
        "at_off": False,
        "last_sync": None,
        "last_error": "",
        "process_alive": False,
        "log_exists": False,
        "log_tail": [],
        "sizing": _lane2_sizing_diag(),
    }
    if not LANE2_WORKER_ENABLED:
        return out
    client = _lane2_worker_client
    if client is not None:
        try:
            out.update(client.status_summary())
        except Exception as exc:
            out["last_error"] = str(exc)
    try:
        net = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=10, check=False).stdout or ""
        out["process_alive"] = any(
            f":{LANE2_WORKER_PORT}" in line and "LISTENING" in line
            for line in net.splitlines())
    except Exception:
        pass
    try:
        lines = (base / "lane2_worker.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
        out["log_tail"] = lines[-6:]
        out["log_exists"] = True
    except Exception:
        pass
    return out


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
    # A parte on-loop do Diagnóstico (exec_state lê o terminal MT5) também é
    # medida: se congelar ≥ limiar, o bloco ⏱️ Loop stalls mostra a causa.
    import time as _tm
    _diag_t0 = _tm.monotonic()
    exec_state = state._execution_state()
    reconciliation = None
    reconcile = getattr(state.sim, "reconcile_live_state", None)
    if reconcile is not None:
        try:
            reconciliation = reconcile()
        except Exception as exc:
            reconciliation = {"ok": False, "safe": False, "reason": str(exc)}
    state.stall_recorder.record_phase(
        "diagnostics.on_loop", _tm.monotonic() - _diag_t0)
    acct = exec_state.get("account") or {}
    mgmt = next((s for s in state.db.list_signals(1) if s.status == "management"), None)
    # Fita do vigia do event loop (stalls + fases lentas) — explica os "sem
    # resposta HTTP" do healthcheck com QUANDO e (nas fases) ONDE.
    from datetime import datetime, timedelta, timezone
    stall_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
        timespec="seconds")
    loop_stalls = {
        "threshold": state.stall_recorder.threshold,
        "interval": float(os.getenv("LOOP_STALL_INTERVAL", "0.5") or 0.5),
        "count_24h": db.loop_stall_count_since(stall_since),
        "recent": db.recent_loop_stalls(12),
    }
    # I/O bloqueante (HTTP ao worker, subprocessos netstat/schtasks) NUNCA
    # corre no event loop: um worker lento ou um subprocesso demorado
    # congelaria a dashboard e o healthcheck durante o request — offload.
    lane2_worker, healthcheck = await asyncio.gather(
        asyncio.to_thread(_lane2_worker_diag),
        asyncio.to_thread(_healthcheck_status),
    )
    return {
        "ok": True,
        "server": {
            "started_at": started,
            "uptime_s": uptime_s,
            "pid": os.getpid(),
            "app": APP_NAME,
        },
        "execution": exec_state,
        "reconciliation": reconciliation,
        "stuck_pending": {
            "main": _stuck_pending_legs(sim),
            "tp2": _stuck_pending_legs(sim2),
        },
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
        "healthcheck": healthcheck,
        "lane2_worker": lane2_worker,
        "loop_stalls": loop_stalls,
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
            "mode": "auto",  # único caminho desde o Item 12 (sem modo manual)
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


@app.get("/api/whatsapp/status")
async def whatsapp_status():
    """Estado do bridge WhatsApp (Baileys) para o bloco do Diagnóstico:
    ligado/pareado/telefone, QR para parear e lista de grupos (JID a copiar
    para o .env). Read-only — nunca envia nada."""
    wa = getattr(state, "whatsapp", None)
    info = {
        "enabled": bool(wa and wa.enabled),
        "configured": bool(wa and wa.configured),
        "group_jid": (wa.group_jid if wa else ""),
        "dashboard_url": (wa.dashboard_url if wa else ""),
        "connected": False, "paired": False, "phone": None,
        "last_error": None,
        "started_at": None, "sent_count": 0,
        "last_send_at": None, "last_send_jid": None,
        "qr": None, "groups": [], "error": None,
    }
    if not wa or not wa.enabled:
        return info
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{wa.bridge_url}/status")
            r.raise_for_status()
            st = r.json()
            for k in ("connected", "paired", "phone", "last_error",
                      "started_at", "sent_count", "last_send_at",
                      "last_send_jid"):
                info[k] = st.get(k, info[k])
            if info["connected"]:
                g = await client.get(f"{wa.bridge_url}/groups", timeout=8)
                if g.status_code == 200:
                    info["groups"] = g.json().get("groups", [])
            elif st.get("qr"):
                # dashboard precisa do PNG: busca o dataUrl ao bridge
                q = await client.get(f"{wa.bridge_url}/qr", timeout=5)
                if q.status_code == 200:
                    info["qr"] = q.json().get("dataUrl")
    except Exception as exc:
        info["error"] = str(exc)
    return info


@app.post("/api/whatsapp/test")
async def whatsapp_test():
    """Envia uma mensagem de teste do formato dual-lane para o grupo
    WhatsApp (valida o bridge E o texto das 2 contas sem esperar um fecho).
    Read-only para a execução de trades.
    """
    wa = getattr(state, "whatsapp", None)
    if not wa or not wa.configured:
        return {"ok": False, "error": "whatsapp não configurado (env WHATSAPP_ENABLED/WHATSAPP_GROUP_JID)"}
    from app.notify.whatsapp import format_test_message
    try:
        sent = await wa.send(format_test_message(wa.dashboard_url))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not sent:
        return {"ok": False, "error": "bridge não respondeu (ver whatsapp/bridge.log)"}
    await notifier.send("system", "🧪 Mensagem de teste WhatsApp enviada (formato dual-lane).")
    return {"ok": True, "sent": True}


@app.post("/api/whatsapp/restart")
async def whatsapp_restart():
    """Religa o bridge WhatsApp SEM reiniciar o servidor.

    Bridge vivo → pede reconnect ao Baileys (POST /reconnect — fecha a
    socket e religa em ~3s). Processo node morto → spawna-o (como o
    watchdog do listener), com os logs em whatsapp/bridge.log.
    Read-only para a execução de trades.
    """
    wa = getattr(state, "whatsapp", None)
    if not wa or not wa.enabled:
        return {"ok": False, "error": "whatsapp não configurado"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{wa.bridge_url}/reconnect")
            if r.status_code == 200:
                print("[whatsapp] reconnect pedido pela dashboard")
                return {"ok": True, "action": "reconnect"}
    except Exception:
        pass  # bridge morto -> spawna abaixo
    try:
        proc = await asyncio.to_thread(_spawn_whatsapp_bridge)
        print(f"[whatsapp] bridge reiniciado pela dashboard (pid {proc.pid})")
        return {"ok": True, "action": "spawned", "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
            safe_json({"type": "snapshot", "data": state.last_snapshot()})
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws.disconnect(websocket)


# --------------------------------------------------------------- frontend

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
