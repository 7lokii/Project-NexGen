"""BotState — the orchestrator.

Owns the configuration, the simulated account, and the signal pipeline:

    signal in -> parse -> risk checks -> (auto) execute / (manual) pending

Also drives the per-second market tick: advance prices, let the strategy
manager close legs at TP1/TP2/SL/breakeven, apply realized P&L to the
account, watch the daily-loss limit (auto kill switch) and schedule the
daily summary.

Everything here runs on the FastAPI event loop, so there are no races
between the API endpoints and the background tasks.
"""

import asyncio
import json
import os
import re
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from app.config import DEFAULT_CONFIG
from app.db import DB
from app.execution.engine import SimEngine, _now_iso, _new_id
from app.execution.market import MarketSimulator
from app.models import (ConfigUpdate, KillSwitchUpdate, Position, RiskDecision,
                        Signal, SignalIn, SimulateIn, Trade)
from app.messages.manage import classify, symbol_in
from app.messages.summaries import parse_summary
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.signals.llm_fallback import try_llm_parse
from app.signals.parser import parse_or_error


def cur(v) -> str:
    """Formata um valor monetário para as notificações (ex.: '1,234.56 €')."""
    try:
        return f"{float(v):,.2f} €"
    except (TypeError, ValueError):
        return str(v)

# Conversa do grupo ("Okay team, waiting for New York...") nunca entra na
# pipeline — é auditada e ignorada. Mesmo regex do filtro do listener
# (`looks_like_signal`), para a decisão viver no backend e funcionar mesmo
# com SIGNALBOT_TG_FILTER=0.
# Log das ignoradas no server.log: SIGNALBOT_LOG_IGNORED=0 desliga,
# 1 (default) mostra os primeiros 80 caracteres, 2 mostra o texto completo.
_LOG_IGNORED = int(os.getenv("SIGNALBOT_LOG_IGNORED", "1") or "1")

# Keywords de direção ("I'm selling XAUUSD") casam sozinhas — o grupo envia
# a direção primeiro e edita com os detalhes. Já os detalhes (TP/SL/entry/...)
# só contam como sinal quando trazem um NÚMERO a seguir: "TP1 4396" é sinal,
# mas "after TP1" / "as SL" (conversa do grupo) não.
_SIGNAL_HINT = re.compile(
    r"\b(buying|selling|comprando|vendendo|buy|sell|compr|vend|long|short)\b"
    r"|\b(entr(?:ada)?|entry|stop\s*loss|stop|sl|tp\s*[1-5]|tp[1-5]|alvo|target)\b"
    r"\s*[0-9]",
    re.IGNORECASE,
)


def _tps_text(obj) -> str:
    """TP1 1.0890 | TP2 1.0920 | ... — every TP present on a Signal/Trade."""
    parts = []
    for i in range(1, 6):
        tp = float(getattr(obj, f"take_profit_{i}", 0.0) or 0.0)
        if tp > 0:
            parts.append(f"TP{i} {tp}")
    return " | ".join(parts)


def _strategy_params_text(p: dict) -> str:
    """Descrição dos parâmetros de execução de uma estratégia (max_legs,
    exit_at_tp, lots_override, gestão) — usada na notificação de promoção
    e no badge da dashboard."""
    parts = []
    legs = p.get("max_legs")
    parts.append(f"{legs} pernas (TP1..TP{legs})" if legs else "todas as pernas (TP1..TP5)")
    if p.get("exit_at_tp"):
        parts.append(f"fecha tudo no TP{p['exit_at_tp']}")
    if p.get("lots_override"):
        parts.append(f"lote fixo {p['lots_override']}")
    parts.append("gestão do grupo ON" if p.get("manage") else "sem gestão do grupo")
    return " · ".join(parts)


def _lots_text(trade) -> str:
    """1.5+1.5+1.5 — lots of every sized leg of a Trade."""
    parts = []
    for i in range(1, 6):
        lots = float(getattr(trade, f"lots_{i}", 0.0) or 0.0)
        if lots > 0:
            parts.append(str(lots))
    return "+".join(parts) if parts else "0"


def _now_iso_plus(seconds: float) -> str:
    """ISO timestamp `seconds` in the future (for edit deadlines)."""
    from datetime import datetime, timedelta, timezone
    # UTC explícito — ver `_now_iso` (engine): a dashboard mostra a contagem
    # da janela de edição correta em qualquer fuso do browser.
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _iso_age_seconds(iso: Optional[str]) -> Optional[float]:
    """Idade (em segundos) de um timestamp ISO face a agora; None se inválido."""
    if not iso:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (datetime.now().astimezone() - dt).total_seconds()


class WSManager:
    def __init__(self):
        self.connections = set()
        # websocket -> {user, since} — quem está ligado à dashboard agora
        self.meta: Dict[Any, Dict[str, str]] = {}

    async def connect(self, websocket, user: str = ""):
        await websocket.accept()
        self.connections.add(websocket)
        self.meta[websocket] = {"user": user or "?", "since": _now_iso()}

    def disconnect(self, websocket):
        self.connections.discard(websocket)
        self.meta.pop(websocket, None)

    def online_users(self) -> List[dict]:
        """Lista de utilizadores com a dashboard aberta neste momento."""
        return [{"user": m["user"], "since": m["since"]} for m in self.meta.values()]

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload)
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                self.connections.discard(ws)


def _fase3_window_trades(db: DB, sid: str, promoted_at: str,
                         limit: int = 10) -> List[Trade]:
    """Return the first validation-window trades opened by ``sid``.

    The validation contract is the *first* ten trades after promotion, not
    every trade that happened to close after that timestamp.  Filtering by
    opening time prevents a position from the previous strategy from being
    counted after it closes; explicit strategy ids are honoured while legacy
    rows without one remain usable for historical/test data.
    """
    if not promoted_at:
        return []
    closed = [
        t for t in db.list_trades(500)
        if t.status == "closed" and t.closed_at and t.opened_at
        and t.opened_at >= promoted_at and t.closed_at >= promoted_at
        and t.strategy_id in (None, sid)
    ]
    closed.sort(key=lambda t: (t.closed_at or "", t.id))
    return closed[:max(1, int(limit))]


async def _compute_fase3_verdict(db: DB, sid: Optional[str]) -> Optional[dict]:
    """Veredicto da janela de validação da Fase 3 (ESTRATEGIAS.md Parte X)
    — standalone (só precisa da BD) para ser testável sem o BotState inteiro.

    Avalia as 10 primeiras trades reais da auto (fechadas após `promoted_at`
    do snapshot) e devolve o veredicto com as métricas do doc: PF real da
    janela, mesma decisão de saída que o papel (≥ 9/10), posições sem SL,
    ordens duplicadas (perna com o mesmo `leg`), modifies recusados pelo
    broker (retcodes ≠ 10009/0 na auditoria `autosl:`), atraso
    sinal→abertura ≤ ~2 min e P&L coerente com o backtest do mesmo sinal.

    `None` quando ainda não há janela para avaliar (estratégia não é auto,
    sem snapshot, veredicto já emitido ou < 10 trades fechadas)."""
    import json as _json
    from app.strategies import STRATEGIES as _S
    if not sid or not sid.startswith("auto_"):
        return None
    _strat = _S.get(sid, {})
    if not (_strat.get("stair_levels") or _strat.get("trail_factor")
            or _strat.get("confirm_filter") or _strat.get("time_stop_minutes")):
        return None
    try:
        snap = json.loads(db.all_config().get("fase3_snapshot") or "null")
    except (ValueError, TypeError):
        snap = None
    if not snap:
        return None
    if db.all_config().get("fase3_verdict"):
        return None
    promoted_at = snap.get("promoted_at") or ""
    closed = _fase3_window_trades(db, sid, promoted_at)
    if len(closed) < 10:
        return None
    # PF real da janela
    pnls = [t.pnl or 0.0 for t in closed]
    wins = sum(p for p in pnls if p > 0)
    loss_sum = abs(sum(p for p in pnls if p < 0))
    pf = round(wins / loss_sum, 2) if loss_sum else None
    # divergências estruturais no mesmo sinal (saída diferente / papel ainda
    # aberto enquanto o real já fechou)
    from app.analysis import paper_vs_real_auto
    try:
        pvr = await paper_vs_real_auto(db, strategy_id=sid)
        window_signal_ids = {t.signal_id for t in closed}
        pairs = [p for p in pvr.get("pairs", [])
                 if (p.get("real_closed_at") or "") >= promoted_at
                 and p.get("signal_id") in window_signal_ids]
        structural = [p for p in pairs if not p.get("same_exit") or p.get("paper_open")]
        same_exit_n = sum(1 for p in pairs if p.get("same_exit"))
    except Exception as exc:
        print(f"[fase3] divergences ao validar janela: {exc}")
        structural = []
        same_exit_n = 0
    # posições abertas sem SL (zero tolerado na janela)
    no_sl = [p for p in db.open_positions() if not p.stop_loss]

    # "zero ordens duplicadas e retcodes 10009" (doc Parte X.1):
    # - ordens duplicadas = mais de uma posição com o MESMO leg na mesma
    #   trade (o `leg` é o comentário único `trade.id:legN` no broker);
    # - retcodes recusados = modifies da auto-gestão recusados (10016/stops
    #   inválidos) na auditoria `autosl:<trade_id>` — aceite é 10009/0.
    dup_orders, refused_retcodes = [], []
    slow_opens, pnl_incoherent = [], []
    by_signal = {s.id: s for s in db.signals_since("")}
    from app.analysis import real_vs_backtest
    try:
        rvb = await real_vs_backtest(db, strategy_id=sid)
        rvb_by_trade = {r["trade_id"]: r for r in rvb.get("trades", [])}
    except Exception:
        rvb_by_trade = {}
    for t in closed:
        legs_seen = {}
        for p in db.positions_for_trade(t.id):
            if p.leg in legs_seen:
                dup_orders.append((t.id, p.leg))
            legs_seen[p.leg] = True
        try:
            for e in _json.loads(db.all_config().get(f"autosl:{t.id}") or "[]"):
                rc = e.get("retcode")
                if rc is not None and int(rc or 0) not in (10009, 0):
                    refused_retcodes.append((t.id, e.get("leg"), rc))
        except (ValueError, TypeError):
            pass
        # atraso sinal→abertura ≤ ~2 min (o sim assume 60s; acima de 120s o
        # sinal já pode estar desatualizado)
        sig = by_signal.get(t.signal_id)
        if sig and sig.received_at and t.opened_at:
            try:
                from datetime import datetime as _dt
                t0 = _dt.fromisoformat(str(sig.received_at).replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(str(t.opened_at).replace("Z", "+00:00"))
                delay = (t1 - t0).total_seconds()
                if delay > 120:
                    slow_opens.append((t.id, int(delay)))
            except (ValueError, TypeError):
                pass
        # P&L coerente com o backtest do mesmo sinal: sinal oposto =
        # incoerente; magnitudes muito desviadas também (real a perder > 150%
        # do previsto, ou a ganhar < 25% do previsto, não é ruído de custos)
        rv = rvb_by_trade.get(t.id) or {}
        bt_pnl = rv.get("bt_pnl")
        real_pnl = t.pnl or 0.0
        if bt_pnl is not None and abs(bt_pnl) >= 0.5:
            if (real_pnl > 0) != (bt_pnl > 0) and abs(real_pnl) >= 1.0:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))
            elif real_pnl < 0 and real_pnl < 1.5 * bt_pnl:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))
            elif real_pnl > 0 and real_pnl < 0.25 * bt_pnl:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))

    reasons = []
    if pf is not None and pf < 1.0:
        reasons.append(f"PF {pf:.2f} < 1.0 nas {len(closed)} trades")
    if same_exit_n < 9:
        reasons.append(f"mesma decisão do papel em {same_exit_n}/{len(closed)} "
                       "trades (< 9/10)")
    if len(structural) >= 2:
        reasons.append(f"{len(structural)} divergências estruturais "
                       "(saída diferente do papel no mesmo sinal)")
    if no_sl:
        reasons.append(f"{len(no_sl)} posições abertas sem SL")
    if dup_orders:
        reasons.append(f"{len(dup_orders)} ordens duplicadas "
                       "(perna re-enviada no mesmo leg)")
    if refused_retcodes:
        codes = sorted({r[2] for r in refused_retcodes})
        reasons.append(f"{len(refused_retcodes)} modifies recusados pelo "
                       f"broker (retcodes {codes})")
    if slow_opens:
        reasons.append(f"{len(slow_opens)} aberturas com atraso sinal→"
                       f"abertura > 2 min (máx {max(d for _, d in slow_opens)}s)")
    if pnl_incoherent:
        reasons.append(f"{len(pnl_incoherent)} trades com P&L incoerente "
                       "com o backtest do mesmo sinal")
    decision = "rollback" if reasons else "maintain"
    same_pct = (round(same_exit_n / len(closed) * 100) if closed else None)
    return {
        "evaluated_at": _now_iso(),
        "strategy_id": sid,
        "window_trades": len(closed),
        "pf": pf,
        "pnl": round(sum(pnls), 2),
        "same_exit_n": same_exit_n,
        "same_exit_pct": same_pct,
        "structural_divergences": len(structural),
        "positions_without_sl": len(no_sl),
        "duplicate_orders": len(dup_orders),
        "refused_retcodes": len(refused_retcodes),
        "slow_opens": len(slow_opens),
        "pnl_incoherent": len(pnl_incoherent),
        # Generation token: a background validation can finish after a new
        # promotion starts. `_check_fase3_validation` rechecks this timestamp
        # before persisting, so an old verdict cannot overwrite the fresh
        # window's cleared state.
        "snapshot_promoted_at": promoted_at,
        "decision": decision,
        "reason": ("; ".join(reasons) if reasons else f"janela limpa — mesma decisão "
                   f"do papel em {same_exit_n}/{len(closed)} trades "
                   f"({same_pct}%), zero posições sem SL, zero ordens "
                   f"duplicadas, retcodes 10009, atraso sinal→abertura ≤ 2 "
                   f"min, P&L coerente com o backtest, PF ≥ 1.0"),
        "snapshot_before": {
            k: snap.get(k) for k in ("promoted_at", "n", "pf", "pnl",
                                     "dd_auto", "dd_ref",
                                     "paper_vs_real_pairs",
                                     "real_trades_closed", "sl_audit_entries")
        },
    }


class BotState:
    def __init__(self, db: DB, market: MarketSimulator, risk: RiskEngine,
                 sim: SimEngine, notifier: Notifier, ws: WSManager):
        self.db = db
        self.market = market
        self.risk = risk
        self.sim = sim
        self.notifier = notifier
        self.ws = ws
        self.config: Dict[str, Any] = self._load_config()
        # paper trading forward: cada estratégia de teste tem a sua conta
        # virtual (1000 €) e recebe os sinais reais ao vivo (ver execution/paper.py)
        from app.execution.paper import PaperRace
        self.paper = PaperRace(db, market)
        # heartbeat do listener do Telegram (tab Diagnóstico): o listener
        # faz POST /api/heartbeat a cada 1s; sem notícias há >5s consideramos
        # o processo em baixo (deteta o listener morto em silêncio).
        self.listener_last_seen: Optional[str] = None
        self.listener_last_msg: Optional[str] = None
        self.listener_tick: int = 0
        self.listener_groups: List[str] = []
        # auto-restart: o watchdog do servidor (main.py) reinicia o processo
        # quando o heartbeat parar; se o Telegram pedir código de login novo,
        # o listener avisa aqui e a dashboard pede o código ao utilizador.
        self.listener_need_code: bool = False
        self.listener_pending_code: Optional[str] = None
        self.listener_proc: Any = None
        self.listener_next_restart_at: float = 0.0
        # histórico de uptime (listener/MT5) — amostrado pelo watchdog a cada
        # 10s, mantido 24h, para o gráfico da tab Diagnóstico.
        from collections import deque
        self.uptime_history = deque()
        self.started_at = _now_iso()
        # snapshots de saldo da corrida paralela (paper + real da principal)
        # — gravados no tick a cada ~60s para a curva "quem está a ganhar".
        self._last_balance_snap: float = 0.0
        # alerta "um teste passou a ganhar à principal" — com cooldown para
        # não spammar (só volta a disparar quando o líder muda).
        self._race_leader: Optional[str] = None
        self._race_leader_since: float = 0.0
        self._race_switch_suggested: Optional[str] = None
        # deteção de transições para alertas (Telegram + dashboard):
        # estado anterior do MT5 e instante da última notificação offline
        # (para não spammar se a ligação ficar a flutuar).
        self._mt5_was_connected: Optional[bool] = None
        self._mt5_was_trade_allowed: Optional[bool] = None
        self._last_mt5_offline_notif: float = 0.0
        # alerta de regime: sim rolante (30d) da estratégia ativa abaixo de
        # PF 1 — verificado no tick a cada ~30 min, cooldown de 6h.
        self._last_regime_check: float = 0.0
        self._regime_alert_at: float = 0.0
        self._regime_status: dict = {"pf": None, "resolved": 0, "checked_at": ""}
        # alerta do ETA da Fase 3 (janela a estimar > 6 semanas ou ritmo
        # com mudança grande vs mês anterior) — cooldown de 24h.
        self._fase3_eta_alert_at: float = 0.0
        # check operacional diário do volume livre: read-only, sem alterar a
        # BD nem a conta; o alerta só se repete no dia seguinte.
        self._disk_last_check_day: str = ""
        self._disk_status: dict = {}

    # ------------------------------------------------------- listener health

    def touch_listener(self, groups: Optional[List[str]] = None,
                       last_msg_at: Optional[str] = None,
                       tick: Optional[int] = None) -> None:
        self.listener_last_seen = _now_iso()
        if groups:
            self.listener_groups = [g for g in groups if g]
        if last_msg_at:
            self.listener_last_msg = last_msg_at
        if tick is not None:
            self.listener_tick = int(tick)

    def listener_status(self) -> dict:
        from datetime import timezone
        alive = False
        if self.listener_last_seen:
            try:
                last = datetime.fromisoformat(self.listener_last_seen)
                if last.tzinfo is None:
                    last = last.astimezone()
                alive = (datetime.now(timezone.utc) - last).total_seconds() < 5
            except (ValueError, TypeError):
                alive = False
        last_msg_age_s = None
        if self.listener_last_msg:
            try:
                last = datetime.fromisoformat(self.listener_last_msg)
                if last.tzinfo is None:
                    last = last.astimezone()
                last_msg_age_s = int(
                    (datetime.now(timezone.utc) - last).total_seconds()
                )
            except (ValueError, TypeError):
                last_msg_age_s = None
        return {
            "alive": alive,
            "last_seen": self.listener_last_seen,
            "last_msg": self.listener_last_msg,
            "last_msg_age_s": last_msg_age_s,
            "tick": self.listener_tick,
            "groups": self.listener_groups,
            "need_code": self.listener_need_code,
        }

    def set_listener_code(self, code: str) -> None:
        code = str(code or "").strip()
        if not code:
            return
        self.listener_pending_code = code
        self.listener_need_code = False

    def consume_listener_code(self) -> Optional[str]:
        code = self.listener_pending_code
        self.listener_pending_code = None
        return code

    def record_uptime_sample(self) -> None:
        """Amostra o estado (listener/MT5) para o gráfico de uptime 24h."""
        import time
        lis = self.listener_status()
        ex = self._execution_state()
        self.uptime_history.append({
            "t": time.time(),
            "listener": 1 if lis["alive"] else 0,
            "mt5": 1 if (ex.get("connected") and str(ex.get("mode", "")).startswith("mt5")) else 0,
            "algo": 1 if ex.get("trade_allowed") else 0,
        })
        cutoff = time.time() - 86400
        while self.uptime_history and self.uptime_history[0]["t"] < cutoff:
            self.uptime_history.popleft()

    # -------------------------------------------------------------- config

    def _load_config(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        stored = self.db.all_config()
        for key in cfg:
            if key in stored:
                cfg[key] = stored[key]
        # coerce types (SQLite stores everything as text)
        cfg["risk_pct"] = float(cfg["risk_pct"])
        cfg["daily_loss_limit_pct"] = float(cfg["daily_loss_limit_pct"])
        cfg["max_positions"] = int(cfg["max_positions"])
        cfg["sim_speed"] = float(cfg["sim_speed"])
        cfg["edit_window_seconds"] = float(cfg["edit_window_seconds"])
        cfg["balance"] = float(cfg["balance"])
        cfg["kill_switch"] = self._to_bool(cfg["kill_switch"])
        cfg["auto_kill"] = self._to_bool(cfg["auto_kill"])
        return cfg

    @staticmethod
    def _to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")

    def _persist_config(self, keys: List[str]) -> None:
        for key in keys:
            self.db.set_config(key, self.config[key])

    # -------------------------------------------------------------- account

    def _mt5_account(self) -> Optional[dict]:
        """Account info from the MT5 terminal when execution runs through it.

        When EXECUTION_MODE=mt5, the broker account is the source of truth
        for balance/equity — the config balance only applies to the paper
        (sim) engine.
        """
        provider = getattr(self.sim, "provider", None)
        if provider is not None and provider.connected:
            return provider.account()
        return None

    def _currency(self) -> str:
        acct = self._mt5_account()
        return (acct or {}).get("currency", "USD")

    def _mt5_positions(self) -> list:
        """Live MT5 positions for the dashboard (empty in sim mode).

        Enriches each broker position with the app-level context: the leg
        number and trade id (parsed from the order comment `t-xxx:legN`) and
        the linked signal (source/group) so the dashboard can show "perna 3
        de sinal X" instead of a raw ticket list.
        """
        provider = getattr(self.sim, "provider", None)
        if provider is None or not provider.connected:
            return []
        trades = {t.id: t for t in self.db.open_trades()}
        out = []
        for p in provider.positions():
            item = dict(p)
            comment = item.get("comment") or ""
            if ":" in comment:
                trade_id, leg_part = comment.split(":", 1)
                item["trade_id"] = trade_id
                m = re.search(r"leg(\d+)", leg_part)
                item["leg"] = int(m.group(1)) if m else 0
                t = trades.get(trade_id)
                if t is not None:
                    item["signal_symbol"] = t.symbol
                    item["signal_direction"] = t.direction
                    item["tp"] = float(getattr(t, f"take_profit_{item['leg']}", 0.0) or 0.0)
                    sig = self.db.get_signal(t.signal_id)
                    item["signal_source"] = (sig.source if sig else "") or ""
            out.append(item)
        return out

    def _balance(self) -> float:
        acct = self._mt5_account()
        if acct:
            return float(acct["balance"])
        return float(self.config["balance"])

    def _open_positions(self) -> List[Position]:
        return self.db.open_positions()

    def _open_trades(self) -> List[Trade]:
        return self.db.open_trades()

    def _open_pnl(self) -> float:
        acct = self._mt5_account()
        if acct:
            return round(float(acct["equity"]) - float(acct["balance"]), 2)
        total = 0.0
        for p in self._open_positions():
            price = self.market.price(p.symbol)
            if price is not None:
                sign = 1.0 if p.direction == "buy" else -1.0
                total += sign * (price - p.entry) * p.lots * 100_000
        return round(total, 2)

    def _realized_today(self) -> float:
        return self.db.realized_today(date.today().isoformat())

    def equity(self) -> float:
        acct = self._mt5_account()
        if acct:
            return round(float(acct["equity"]), 2)
        return round(self._balance() + self._open_pnl(), 2)

    def _update_peak_balance(self) -> None:
        """Mantém o high-watermark da equity (`peak_balance`) — base do risco
        dinâmico. Só escreve quando a equity sobe (barato no tick)."""
        eq = self.equity()
        peak = float(self.config.get("peak_balance") or 0.0)
        if peak <= 0 or eq > peak:
            self.config["peak_balance"] = eq
            self.db.set_config("peak_balance", eq)

    def _effective_risk_pct(self) -> float:
        """Risco dinâmico: o `risk_pct` base escala para baixo após perdas.

        Regra: cada -20% de drawdown desde o pico de equity corta o risco a
        METADE (`0.5 ** (drawdown / 20)`), com piso de 0.1%. Se ainda não há
        pico registado, devolve o base sem escala.
        """
        base = float(self.config.get("risk_pct", 1.0))
        peak = float(self.config.get("peak_balance") or 0.0)
        eq = self.equity()
        if peak <= 0:
            return base
        dd = max(0.0, (peak - eq) / peak * 100.0)
        scale = 0.5 ** (dd / 20.0)
        return max(0.1, base * scale)

    def risk_preview(self) -> dict:
        """Lotes que o risco% atual representa numa trade típica do grupo
        (XAUUSD): total e por perna (TP1..TP5). O lote depende da distância
        do SL — usa a distância real do último sinal válido quando existir,
        senão um SL típico de 30 pontos de ouro."""
        from app.execution.mt5 import FALLBACK_SYMBOLS
        meta = FALLBACK_SYMBOLS.get("XAUUSD", {})
        contract = float(meta.get("contract", 100.0))
        pip = float(meta.get("pip", 0.01))
        step = float(meta.get("vol_step", 0.01))
        vmin = float(meta.get("vol_min", 0.01))
        vmax = float(meta.get("vol_max", 100.0))
        balance = self._balance()
        risk_pct = self._effective_risk_pct()  # risco dinâmico (drawdown)
        # distância do SL: usa a do último sinal válido (entrada+SL) se houver
        sl_dist = 30.0  # pontos de ouro (3.0 de preço) — SL típico do grupo
        sig = self.db.last_signal_with_sl()
        if sig:
            sl_dist = abs(float(sig["entry"]) - float(sig["stop_loss"]))
        risk_amount = balance * risk_pct / 100.0
        risk_pips = sl_dist / pip
        lots_total = risk_amount / (risk_pips * contract * pip)
        lots_total = max(vmin, min(vmax, round(lots_total / step) * step))
        n_legs = self._strategy_params().get("max_legs") or 5
        return {
            "symbol": "XAUUSD",
            "sl_dist": round(sl_dist, 2),
            "lots_total": round(lots_total, 2),
            "lots_per_leg": round(max(vmin, lots_total / n_legs), 2),
            "n_legs": n_legs,
        }

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        return {
            "auth": {"enabled": bool(os.getenv("SIGNALBOT_DASHBOARD_PASSWORD"))},
            "online_users": self.ws.online_users(),
            "account": {
                "balance": round(self._balance(), 2),
                "equity": self.equity(),
                "open_pnl": self._open_pnl(),
                "realized_today": self._realized_today(),
                "open_trades": len(self._open_trades()),
                "currency": self._currency(),
            },
            "config": {
                "mode": self.config["mode"],
                "risk_pct": round(float(self.config["risk_pct"]), 2),
                "effective_risk_pct": round(self._effective_risk_pct(), 2),
                "peak_balance": round(float(self.config.get("peak_balance") or 0.0), 2),
                "daily_loss_limit_pct": round(float(self.config["daily_loss_limit_pct"]), 2),
                "max_positions": int(self.config["max_positions"]),
                "kill_switch": bool(self.config["kill_switch"]),
                "auto_kill": bool(self.config["auto_kill"]),
                "sim_speed": round(float(self.config["sim_speed"]), 1),
                "edit_window_seconds": round(float(self.config["edit_window_seconds"]), 1),
                "summary_time": self.config["summary_time"],
                "strategy": self.config.get("strategy", "nova_gestao"),
                "strategy_params": self._strategy_params(),
                "risk_preview": self.risk_preview(),
                "teste_days": int(self.config.get("teste_days", 30)),
                "paper_race_hours": int(self.config.get("paper_race_hours", 24)),
                "fase3_target": int(self.config.get("fase3_target", 20)),
            },
            "fase3_snapshot": self._fase3_snapshot(),
            "fase3_eta": self._fase3_eta(),
            "fase3_verdict": self._fase3_verdict(),
            "fase3_verdicts": self._fase3_verdicts(),
            "fase3_target_log": self._fase3_target_log(),
            "market": {
                "real_data": self.market.real_data_active,
                "prices": dict(self.market.prices),
                "sources": {
                    sym: meta.get("source", "sim")
                    for sym, meta in self.market.meta().items()
                },
                "changes": {
                    sym: meta.get("chg_pct")
                    for sym, meta in self.market.meta().items()
                },
            },
            "execution": self._execution_state(),
            "operations": {"disk": dict(self._disk_status)},
            "mt5_positions": self._mt5_positions(),
            "ignored_count": self.db.count_ignored(),
            "paper": self.paper_status(),
            "regime": dict(self._regime_status),
            "listener": self.listener_status(),
            "server": {"started_at": self.started_at},
            "signals": [s.to_dict() for s in self.db.list_signals(50)],
            "trades": [self._trade_view(t) for t in self.db.list_trades(50)],
            "positions": [p.to_dict() for p in self.db.open_positions()],
            "notifications": [
                {"id": n.id, "kind": n.kind, "text": n.text, "created_at": n.created_at}
                for n in self.db.list_notifications(50)
            ],
        }

    def _fase3_eta(self) -> Optional[dict]:
        """ETA da janela da Fase 3: ritmo de sinais/dia da `auto_escada_3`
        (trades de papel abertas nos últimos 30 dias — cada sinal executado
        abre papel em cada estratégia) e a data estimada em que N chega ao
        ALVO (`fase3_target`, default 20; o feed permite baixar para 15
        quando o ETA estimar > 6 semanas). `done=True` quando já está;
        `eta=None` sem ritmo ainda (a corrida acabou de começar).

        Também compara o ritmo com o MÊS ANTERIOR (janela 30–60 dias):
        `pace_prev` + `pace_change_pct` — o alerta do tick avisa no feed se
        a janela passar a estimar mais de 6 semanas ou o ritmo mudar muito.
        `pace_daily` = contagem por dia dos últimos 60 dias (mini-curva)."""
        from datetime import datetime, timedelta, timezone
        sid = "auto_escada_3"
        auto = self.db.paper_trade_stats(sid)
        n = int(auto.get("closed") or 0)
        target = int(self.config.get("fase3_target") or 20)
        now = datetime.now(timezone.utc)
        if n >= target:
            return {"n": n, "target": target, "done": True,
                    "pace": None, "pace_prev": None, "pace_change_pct": None,
                    "eta": None, "eta_days": None, "pace_daily": []}
        opened = self.db.paper_trades_opened_since(
            sid, (now - timedelta(days=30)).isoformat(timespec="seconds"))
        opened_prev = (self.db.paper_trades_opened_since(
            sid, (now - timedelta(days=60)).isoformat(timespec="seconds")) - opened)
        pace = opened / 30.0
        pace_prev = opened_prev / 30.0
        change = None
        if pace_prev > 0:
            change = round((pace - pace_prev) / pace_prev * 100.0, 0)
        # contagem por dia dos últimos 60 dias (série zero-preenchida) — a
        # curva mostra a auto E a referência (nova_gestao) lado a lado para
        # comparar a cadência das duas contas
        by_day = self.db.paper_trades_opened_by_day(
            sid, (now - timedelta(days=59)).strftime("%Y-%m-%d"))
        by_day_ref = self.db.paper_trades_opened_by_day(
            "nova_gestao", (now - timedelta(days=59)).strftime("%Y-%m-%d"))
        pace_daily = []
        for i in range(60):
            day = (now - timedelta(days=59 - i)).strftime("%Y-%m-%d")
            pace_daily.append({"day": day, "n": by_day.get(day, 0),
                               "n_ref": by_day_ref.get(day, 0)})
        if pace <= 0:
            return {"n": n, "target": target, "done": False,
                    "pace": 0.0, "pace_prev": round(pace_prev, 2),
                    "pace_change_pct": change, "eta": None, "eta_days": None,
                    "pace_daily": pace_daily}
        eta_days = (target - n) / pace
        eta = now + timedelta(days=eta_days)
        return {
            "n": n, "target": target, "done": False,
            "pace": round(pace, 2), "pace_prev": round(pace_prev, 2),
            "pace_change_pct": change,
            "eta": eta.isoformat(timespec="seconds"),
            "eta_days": round(eta_days, 1),
            "pace_daily": pace_daily,
        }

    def _fase3_verdicts(self) -> list:
        """Histórico dos veredictos das janelas de validação da Fase 3
        (persistido em `fase3_verdicts` — nunca se apaga ao reabrir uma
        janela; mantém os últimos 20)."""
        import json
        raw = self.db.all_config().get("fase3_verdicts")
        if not raw:
            return []
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def _fase3_target_log(self) -> list:
        """Registo das alterações ao alvo da janela da Fase 3 (quem/quando/
        motivo) — `fase3_target_log` na config (JSON, últimos 20). A janela
        de validação usa-o para contexto quando o alvo foi reduzido."""
        import json
        raw = self.db.all_config().get("fase3_target_log")
        if not raw:
            return []
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def _fase3_verdict(self) -> Optional[dict]:
        """Veredicto da janela de validação da Fase 3 (ESTRATEGIAS.md Parte X) — gravado na
        config quando as primeiras 10 trades reais da auto fecham."""
        import json
        raw = self.db.all_config().get("fase3_verdict")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _fase3_snapshot(self) -> Optional[dict]:
        """Snapshot da janela de validação (gravado ao promover uma auto com
        auto-gestão) — o painel Fase 3 mostra-o para comparar antes/depois."""
        import json
        raw = self.db.all_config().get("fase3_snapshot")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _strategy_params(self) -> dict:
        """Parâmetros da estratégia ativa da conta principal (max_legs,
        exit_at_tp, lots_override, manage) — a dashboard mostra-os no badge
        para se saber exatamente como a execução real está a correr.

        `orphan` = a config aponta para um id que já não existe no registo
        (ex.: estratégia removida) — a execução cai no fallback e o badge
        avisa em vez de mostrar silenciosamente a estratégia errada."""
        from app.strategies import STRATEGIES, get_strategy
        sid = self.config.get("strategy")
        s = get_strategy(sid)
        defensive = False
        if s.get("def_window") and not getattr(self, "sim", None) is None:
            try:
                if self.config["mode"] == "auto":
                    pnls = self.db.recent_closed_pnls(int(s["def_window"]))
                    if len(pnls) >= int(s["def_window"]):
                        wins = [p for p in pnls if p > 0]
                        losses = [p for p in pnls if p < 0]
                        if losses:
                            gross_loss = -sum(losses)
                            defensive = gross_loss > 0 and sum(wins) / gross_loss < 1.0
            except Exception:
                pass
        return {
            "nome": s["nome"],
            "max_legs": s["max_legs"],
            "exit_at_tp": s["exit_at_tp"],
            "lots_override": s["lots_override"],
            "defensive": defensive,
            "def_window": s.get("def_window"),
            "def_trail": s.get("def_trail"),
            "def_time_stop": s.get("def_time_stop"),
            "manage": s["manage"],
            "manage_kinds": sorted(s.get("manage_kinds") or ()),
            "orphan": sid not in STRATEGIES,
            "configured_id": sid,
        }

    def _trade_view(self, t: Trade) -> dict:
        """Trade pronta para a dashboard: inclui o contrafactual "sem gestão"
        (P&L da variante `nova` da corrida paper para o MESMO sinal) quando a
        trade real já fechou — liga o backtest ao vivo: "o que teria dado se
        a estratégia não tivesse gestão" (lotes ≈ os mesmos: mesma conta 1000€
        e mesmo risk_pct)."""
        d = t.to_dict()
        if t.status == "closed":
            cf = self.db.paper_trade_for_signal(t.signal_id, "nova")
            if cf:
                d["counterfactual_pnl"] = round(float(cf.get("pnl") or 0.0), 2)
                d["counterfactual_reason"] = cf.get("exit_reason")
        return d

    def paper_status(self) -> dict:
        """Estado das contas virtuais das estratégias (corrida paralela):
        saldo atual vs inicial + stats das trades fechadas. A estratégia ativa
        da principal (config strategy) é `is_main` — mas TODAS (incluindo a
        principal) têm conta virtual própria de 1000 €, para a corrida
        comparar em igualdade de circunstâncias."""
        from app.execution.paper import START_BALANCE
        from app.strategies import (HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES,
                                    STRATEGIES)
        main = self.config.get("strategy")
        skipped = getattr(self.paper, "_skipped_daily_stop", {}) or {}
        out = []
        for sid, strat in STRATEGIES.items():
            if sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES:
                continue
            bal = self.db.ensure_paper_account(sid, START_BALANCE)
            stats = self.db.paper_trade_stats(sid)
            out.append({
                "strategy_id": sid,
                "nome": strat["nome"],
                "desc": strat["desc"],
                "is_main": sid == main,
                "balance": bal,
                "daily_stop_skipped": skipped.get(sid, 0),
                **stats,
            })
        return {"accounts": out}

    def paper_divergences(self) -> list:
        """Pares auto (em validação) vs referência `nova_gestao` por sinal com
        trade de papel — razão e P&L de cada lado, para a tabela da tab
        Relatório (ligação backtest↔vivo, SPEC v2 §3.4). Só devolve os pares
        que divergiram (a referência saiu e a auto continua, ou saídas/P&L
        diferentes no mesmo sinal)."""
        from app.strategies import HIDDEN_STRATEGIES, STRATEGIES
        autos = [sid for sid in STRATEGIES
                 if sid.startswith("auto_") and sid not in HIDDEN_STRATEGIES]
        out = []
        for sid in autos:
            for t in self.db.paper_trades(sid, 300):
                ref = self.db.paper_trade_for_signal(t["signal_id"], "nova_gestao")
                if not ref:
                    continue
                auto_reason = t.get("exit_reason")
                ref_reason = ref.get("exit_reason")
                auto_pnl = t.get("pnl")
                ref_pnl = ref.get("pnl")
                same = (ref_reason == auto_reason and ref_reason is not None
                        and abs((ref_pnl or 0.0) - (auto_pnl or 0.0)) < 0.01)
                auto_open = t.get("status") == "open"
                if auto_open:
                    motivo = "a referência saiu por gestão do grupo e a auto continua a correr"
                elif not same:
                    motivo = (f"saída diferente: {auto_reason or '?'} (auto) vs "
                              f"{ref_reason or '?'} (referência)")
                else:
                    continue  # não divergiu — fica fora da tabela
                out.append({
                    "signal_id": t["signal_id"], "symbol": t["symbol"],
                    "strategy_id": sid, "auto_open": auto_open,
                    "auto_reason": auto_reason, "auto_pnl": auto_pnl,
                    "ref_reason": ref_reason, "ref_pnl": ref_pnl,
                    "motivo": motivo,
                    "auto_at": t.get("closed_at") or t.get("opened_at"),
                })
        out.sort(key=lambda r: r["auto_at"] or "", reverse=True)
        return out

    # --------------------------------------------------------- execution

    def _execution_state(self) -> dict:
        """How trades are executed: sim / mt5-dry / mt5-real, + MT5 account.

        Kept cheap: the provider caches account_info for a few seconds, so
        this runs on every snapshot without hammering the terminal.  A
        non-hedged real account is represented explicitly as blocked instead
        of looking like a generic connection failure.
        """
        engine = self.sim
        provider = getattr(engine, "provider", None)
        if provider is not None:
            dry = bool(getattr(engine, "dry_run", True))
            account = provider.account()
            trade_allowed = False
            if provider.connected:
                try:
                    ti = provider._mt5.terminal_info()
                    trade_allowed = bool(ti and ti.trade_allowed)
                except Exception:
                    pass
            return {
                "mode": "mt5-dry" if dry else "mt5-real",
                "connected": bool(provider.connected),
                "trade_allowed": trade_allowed,
                "account": account,
                "margin_mode": getattr(provider, "account_margin_mode", None),
                "hedged": getattr(provider, "account_hedged", None),
                "blocked_reason": getattr(provider, "execution_block_reason", ""),
            }
        return {"mode": "sim", "connected": False, "trade_allowed": True,
                "account": None, "margin_mode": None, "hedged": None,
                "blocked_reason": ""}

    # ------------------------------------------------------------ pipeline

    async def ingest(self, body: SignalIn) -> dict:
        """Receive a signal and route it through the pipeline.

        Telegram messages (which carry a `message_id`) are buffered in an
        edit window: they only continue once the window closes without edits.
        Everything else (demo button, structured input) continues immediately.
        """
        # Historical messages carry their original Telegram timestamp, so the
        # backtest replays them at the right moment instead of at load time.
        received_at = body.received_at or _now_iso()
        window = float(self.config.get("edit_window_seconds", 60))

        if body.message_id:
            return await self._ingest_telegram(body, received_at, window)

        return await self._ingest_immediate(body, received_at)

    async def _ingest_telegram(self, body: SignalIn, received_at: str, window: float) -> dict:
        """Buffer a Telegram message and wait for possible edits.

        The signal id is `tg-<group>-<message_id>` so that the same Telegram
        message id from different groups never collides, and edits from the
        same group hit the same row.

        Group MANAGEMENT messages ("TP1 HIT", "close first entries",
        "we're out at BE") never wait for the edit window — they act on the
        open trade immediately (see `_handle_management`).
        """
        group_key = (body.group or "").strip()
        signal_id = f"tg-{group_key}-{body.message_id}" if group_key else f"tg-{body.message_id}"
        existing = self.db.get_signal(signal_id)
        if existing and existing.status == "awaiting_edit":
            return await self._apply_edit(existing, body, window)

        # Management messages ("TP1 HIT", "we're out", "Weekly summary") act
        # immediately — classify BEFORE the final-state dedup so re-processing
        # a historical message still routes to the management handler.
        if body.raw_text:
            action = classify(body.raw_text)
            if action:
                return await self._handle_management(body, action, signal_id, received_at)
            # "TP1 4734 SL 4745" a responder ao sinal — o grupo confirma/corrige
            # o TP1/SL da trade do sinal alvo (edição pós-execução).
            edited = await self._handle_tp_sl_update(body, signal_id, received_at)
            if edited:
                return edited
            # Conversa do grupo (sem keywords de sinal nem gestão): audita e
            # ignora — nunca entra na pipeline nem na janela de edição.
            if not _SIGNAL_HINT.search(body.raw_text):
                self.db.record_ignored(
                    body.message_id, body.source or "", body.group or "",
                    body.raw_text,
                    "conversa do grupo (sem keywords de sinal)",
                    received_at,
                )
                if _LOG_IGNORED:
                    preview = body.raw_text.replace("\n", " ").strip()
                    if _LOG_IGNORED >= 2:
                        print(f"[ignored] {body.source} msg {body.message_id} | {body.raw_text}")
                    else:
                        print(f"[ignored] {body.source} msg {body.message_id} | {preview[:80]}")
                return {"action": "ignored", "reason": "conversa do grupo (audit)"}

        if existing:
            # O grupo editou a mensagem DEPOIS de a janela fechar e o sinal
            # já ter executado — a entrada/trade pode estar desatualizada.
            # Alerta de risco para detetar entradas erradas (janelas curtas).
            if (not body.historical and existing.status in ("executed", "pending")
                    and (body.raw_text or "").strip() != (existing.raw_text or "").strip()):
                await self.notifier.send(
                    "risk",
                    f"⚠️ O grupo editou o sinal {existing.symbol or ''} depois de "
                    f"{('executar' if existing.status == 'executed' else 'ficar pendente')} "
                    f"(janela de {int(window)}s já passou) — a entrada pode estar "
                    f"desatualizada: \"{(body.raw_text or '').strip()[:70]}\"",
                )
            # REVIVE: o sinal tinha falhado (ex.: entrada incompleta, sem TP/SL
            # ainda) e o grupo agora completou a edição — reabre a janela com o
            # conteúdo novo em vez de ignorar. Só dentro de 5 min da mensagem
            # original; histórico nunca revive (não age em trades abertas).
            age = _iso_age_seconds(existing.received_at)
            if (not body.historical and existing.status == "failed"
                    and (body.raw_text or "").strip() != (existing.raw_text or "").strip()
                    and age is not None and age <= 300):
                revived = await self._build_signal(body, existing.id, existing.received_at)
                if not revived.error:
                    revived.status = "awaiting_edit"
                    revived.message_id = existing.message_id
                    revived.edit_deadline = _now_iso_plus(window)
                    revived.edited_at = _now_iso()
                    self.db.insert_signal(revived)
                    self.db.delete_ignored(str(body.message_id), body.source or "")
                    await self.notifier.send(
                        "revived",
                        f"🔄 Sinal {revived.symbol or ''} recuperado — o grupo completou "
                        f"a edição (janela de {int(window)}s reaberta)",
                    )
                    return {"action": "revived", "signal": revived.to_dict()}
            return {"action": "ignored", "reason": f"sinal já finalizado ({existing.status})"}

        signal = await self._build_signal(body, signal_id, received_at)
        signal.reply_to_msg_id = str(body.reply_to_msg_id) if body.reply_to_msg_id else None
        if body.historical:
            # past messages from the group's history: register only, never
            # wait for edits or execute
            signal.status = "historical" if not signal.error else "failed"
            signal.message_id = str(body.message_id)
            self.db.insert_signal(signal)
            return {"action": "historical", "signal": signal.to_dict()}

        signal.message_id = str(body.message_id)
        # se a primeira versão da mensagem tinha sido auditada como conversa
        # (ex.: "I'm selling XAUUSD" sem TP/SL ainda) e agora é sinal, remove
        # o registo de ignorada para a auditoria não contradizer os sinais.
        self.db.delete_ignored(str(body.message_id), body.source or "")
        # Parse completo → executa já (o TP1 do grupo pode bater em segundos).
        # A janela de edição só é usada quando a primeira versão está
        # incompleta (parse com erro) ou veio da IA (llm_parsed → aprovação).
        if not signal.error and not signal.llm_parsed:
            self.db.insert_signal(signal)
            return await self._continue_complete(signal)

        signal.status = "awaiting_edit"
        signal.edit_deadline = _now_iso_plus(window)
        self.db.insert_signal(signal)
        return {"action": "buffered", "edit_window_seconds": window, "signal": signal.to_dict()}

    async def _continue_complete(self, signal: Signal) -> dict:
        """Roteia um sinal com parse completo para a pipeline — auto executa
        já, manual fica pendente — sem esperar a janela de edição."""
        if self.config["mode"] == "auto":
            return await self._try_execute(signal)
        signal.status = "pending"
        self.db.set_signal_status(signal.id, "pending")
        return {"action": "pending", "signal": signal.to_dict()}

    async def _apply_edit(self, existing: Signal, body: SignalIn, window: float) -> dict:
        """A Telegram edit for a buffered message: re-parse and reset the clock."""
        signal = await self._build_signal(body, existing.id, existing.received_at)
        signal.status = "awaiting_edit"
        signal.message_id = existing.message_id
        signal.edit_deadline = _now_iso_plus(window)
        signal.edited_at = _now_iso()
        self.db.insert_signal(signal)
        if signal.error:
            return {"action": "edited_invalid", "error": signal.error, "signal": signal.to_dict()}
        return {"action": "edited", "signal": signal.to_dict()}

    async def _ingest_immediate(self, body: SignalIn, received_at: str) -> dict:
        """Messages without a message_id (demo button, structured input)."""
        signal_id = "s-" + _new_id()
        signal = await self._build_signal(body, signal_id, received_at)
        if signal.error:
            signal.status = "failed"
            self.db.insert_signal(signal)
            await self.notifier.send("risk", f"⚠️ Sinal não reconhecido: {signal.error}")
            return {"action": "parse_error", "error": signal.error, "signal": signal.to_dict()}
        self.db.insert_signal(signal)
        if signal.llm_parsed:
            # LLM-parsed signals always need a human: never auto-execute
            self.db.set_signal_status(signal.id, "pending")
            await self.notifier.send(
                "system",
                f"🤖 Sinal {signal.symbol} {signal.direction} parseado por IA "
                f"— aguarda aprovação manual (nunca executa em auto)",
            )
            return {"action": "pending", "llm_parsed": True, "signal": signal.to_dict()}
        if self.config["mode"] == "auto":
            return await self._try_execute(signal)
        return {"action": "pending", "signal": signal.to_dict()}

    async def _build_signal(self, body: SignalIn, signal_id: str, received_at: str) -> Signal:
        """Build a Signal from an API body.

        Failed parses carry an `error` instead of raising, so the edit window
        can wait for the group to fix the message. When the regex parser
        fails and an LLM endpoint is configured, `try_llm_parse` offers a
        second chance — flagged `llm_parsed`, which forces manual approval.
        """
        if body.raw_text:
            signal = parse_or_error(body.raw_text, source=body.source,
                                    signal_id=signal_id, received_at=received_at)
            if signal.error:
                from app.signals.llm_fallback import llm_configured
                if llm_configured():
                    llm = await try_llm_parse(body.raw_text, source=body.source,
                                              signal_id=signal_id, received_at=received_at)
                    if llm is not None and not llm.error:
                        return llm
            return signal
        signal = Signal(
            id=signal_id, source=body.source, raw_text="(estruturado)",
            symbol=(body.symbol or "").upper().replace("/", ""),
            direction=body.direction.value if body.direction else "",
            entry=body.entry or 0.0,            stop_loss=body.stop_loss or 0.0,
            take_profit_1=body.take_profit_1 or 0.0,
            take_profit_2=body.take_profit_2 or 0.0,
            take_profit_3=body.take_profit_3 or 0.0,
            take_profit_4=body.take_profit_4 or 0.0,
            take_profit_5=body.take_profit_5 or 0.0,
            status="pending", received_at=received_at, lots=body.lots,
        )
        if not (signal.symbol and signal.direction and signal.entry
                and signal.stop_loss and signal.take_profit_1 and signal.take_profit_2):
            signal.status = "failed"
            signal.error = "campos estruturados incompletos"
        return signal

    async def finalize_awaiting_signals(self, now: Optional[str] = None) -> None:
        """Continue any signal whose edit window has closed.

        Valid signals go to the normal pipeline (manual -> pending,
        auto -> risk checks + execute). Invalid ones are marked failed.
        """
        now = now or _now_iso()
        for sig in self.db.awaiting_edit_signals():
            if not sig.edit_deadline or sig.edit_deadline > now:
                continue
            if sig.error:
                self.db.set_signal_status(sig.id, "failed")
                await self.notifier.send(
                    "risk",
                    f"⚠️ Sinal {sig.symbol or '(desconhecido)'} inválido após a "
                    f"janela de edição: {sig.error}",
                )
                continue
            if sig.llm_parsed:
                # LLM-parsed signals always need a human: never auto-execute
                self.db.set_signal_status(sig.id, "pending")
                await self.notifier.send(
                    "system",
                    f"🤖 Sinal {sig.symbol} {sig.direction} parseado por IA "
                    f"— aguarda aprovação manual (nunca executa em auto)",
                )
                continue
            if self.config["mode"] == "auto":
                await self._try_execute(sig)
            else:
                self.db.set_signal_status(sig.id, "pending")

    async def _try_execute(self, signal: Signal) -> dict:
        decision: RiskDecision = self.risk.check(
            signal, self.config, self._open_trades(), self._realized_today(), self._balance()
        )
        if not decision.allowed:
            signal.status = "rejected"
            self.db.set_signal_status(signal.id, "rejected", error="; ".join(decision.reasons))
            await self.notifier.send(
                "risk",
                f"⛔ Sinal {signal.symbol} bloqueado: {'; '.join(decision.reasons)}",
            )
            return {"action": "blocked", "reasons": decision.reasons, "signal": signal.to_dict()}

        from app.strategies import get_strategy
        strat = get_strategy(self.config.get("strategy"))
        risk_pct = self._effective_risk_pct()  # risco dinâmico (drawdown)
        try:
            trade = self.sim.open_trade(
                signal, risk_pct, self._balance(),
                lots_override=signal.lots, max_legs=strat["max_legs"],
            )
            # Persistir a estratégia que abriu a trade real permite encaminhar
            # protect/pending management corretamente após um restart.
            trade.strategy_id = strat["id"]
            self.db.set_trade_strategy(trade.id, strat["id"])
        except Exception as exc:
            # nunca deixar o sinal em awaiting_edit após uma exceção: o tick
            # reabriria a trade a cada segundo (duplicados). Regista a falha.
            self.db.record_exception(
                exc, context={"component": "execution", "phase": "open_trade"},
                signal_id=signal.id,
            )
            signal.status = "failed"
            error = f"exceção ao abrir trade: {exc}"
            self.db.set_signal_status(signal.id, "failed", error=error)
            await self.notifier.send(
                "risk", f"⚠️ Sinal {signal.symbol} falhou ao abrir: {exc}"
            )
            return {"action": "execution_failed", "error": error, "signal": signal.to_dict()}
        if trade.status != "open":
            # broker refused the orders (e.g. AutoTrading off): no phantom trade
            signal.status = "failed"
            error = trade.error or "ordem recusada pelo broker"
            self.db.set_signal_status(signal.id, "failed", error=error)
            if getattr(trade, "auto_trading_off", None):
                # retcode 10027 = trading desligado no terminal — alerta imediato
                # para o utilizador ligar o AutoTrading antes do próximo sinal.
                self.db.set_config("last_auto_trading_off_at", _now_iso())
                await self.notifier.send(
                    "risk",
                    f"⛔ AutoTrading DESLIGADO no MT5: sinal {signal.symbol} "
                    f"NÃO executado (retcode 10027). Liga o AutoTrading no "
                    f"terminal antes do próximo sinal.",
                )
            else:
                await self.notifier.send(
                    "risk", f"⚠️ Sinal {signal.symbol} não executado: {error}"
                )
            return {"action": "execution_failed", "error": error, "signal": signal.to_dict()}

        signal.status = "executed"
        self.db.set_signal_status(signal.id, "executed")
        # paper trading forward: as estratégias de teste abrem a MESMA trade
        # em papel com os seus parâmetros, cada uma com o seu saldo virtual.
        # O kill diário da produção (daily_loss_limit_pct) também se aplica à
        # corrida paper — as contas virtuais refletem o que a produção real
        # faria se a estratégia fosse a principal.
        try:
            opened = self.paper.open_for_signal(
                signal, risk_pct,
                daily_stop_pct=float(self.config.get("daily_loss_limit_pct") or 0),
            )
            if opened:
                await self.notifier.send(
                    "system",
                    f"🧪 {len(opened)} estratégias abriram a trade "
                    f"{signal.symbol} em papel (corrida paralela)",
                )
        except Exception as exc:
            self.db.record_exception(
                exc, context={"component": "paper", "phase": "open_for_signal"},
                signal_id=signal.id,
            )
            print(f"[paper] falha ao abrir trade de teste: {exc}")
        # auditoria: quanto dinheiro real está em risco nesta trade
        eq = self.equity()
        risk_amt = round(eq * risk_pct / 100.0, 2)
        risk_currency = self._currency()
        await self.notifier.send(
            "trade_open",
            f"🚀 Trade aberta ({self.config['mode']}) {signal.symbol} "
            f"{signal.direction.upper()} @ {signal.entry} | "
            f"SL {signal.stop_loss} | {_tps_text(signal)} | "
            f"risco {risk_pct}% da equity = {risk_amt:,.2f} {risk_currency} | "
            f"lotes {_lots_text(trade)}",
        )
        # management instructions the group posted while this signal was
        # still in the edit window ("SL HIT" replied before the trade opened)
        applied = await self._apply_pending_management(signal, trade)
        if applied:
            await self.notifier.send(
                "system",
                f"📨 Aplicadas {applied} instruções de gestão pendentes do grupo "
                f"à trade {signal.symbol} acabada de abrir",
            )
        return {"action": "executed", "trade": trade.to_dict(), "signal": signal.to_dict()}

    async def approve(self, signal_id: str) -> dict:
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"error": "sinal não encontrado"}
        if signal.status != "pending":
            return {"error": f"estado atual: {signal.status}"}
        return await self._try_execute(signal)

    async def reject(self, signal_id: str) -> dict:
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"error": "sinal não encontrado"}
        if signal.status != "pending":
            return {"error": f"estado atual: {signal.status}"}
        self.db.set_signal_status(signal_id, "rejected")
        await self.notifier.send("system", f"❌ Sinal {signal.symbol} rejeitado manualmente")
        return {"action": "rejected"}

    async def delete_signal(self, signal_id: str) -> dict:
        """A Telegram message was deleted: mark the signal deleted and close
        any open position that was opened from it (at market price)."""
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"ok": True, "action": "not_found"}

        closed = 0
        if signal.status == "executed":
            for trade in self.db.open_trades():
                if trade.signal_id != signal.id:
                    continue
                events = self.sim.close_trade_at_market(trade.id)
                if events:
                    closed += 1
                    # MT5: the broker already moved the money; the config
                    # balance is only the paper (sim) source of truth.
                    if self._mt5_account() is None:
                        updated = self.db.get_trade(trade.id)
                        pnl = updated.pnl if updated else 0.0
                        self.config["balance"] = round(self._balance() + pnl, 2)
                        self.db.set_config("balance", self.config["balance"])

        self.db.set_signal_status(signal.id, "deleted", error="mensagem apagada no Telegram")
        note = " — posição fechada ao mercado" if closed else ""
        await self.notifier.send(
            "risk", f"🗑️ Sinal {signal.symbol or signal.id} apagado no Telegram{note}"
        )
        return {"ok": True, "action": "deleted", "closed_trades": closed}

    # ------------------------------------------------- group management

    async def _handle_management(self, body: SignalIn, action: dict, signal_id: str,
                                 received_at: str) -> dict:
        """Act on a group management message (TP HIT, SL HIT, close first
        entries, out at BE). Never waits for the edit window.

        The message is recorded as a signal (status `management`) so the
        dashboard history shows what the group said, then the action is
        applied to the trade of the signal it REPLIES TO (`reply_to_msg_id`)
        — falling back to the open trade of the same symbol, then the most
        recent open trade. If the replied signal has no trade yet (still in
        the edit window), the instruction is queued and applied when that
        signal's trade opens.
        """
        kind = action["kind"]
        if kind == "summary":
            return await self._handle_summary(body, signal_id, received_at)

        # record the management message itself (kind -> status tag)
        mgmt_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol=symbol_in(body.raw_text) or "", direction="", entry=0.0,
            stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at,
            message_id=str(body.message_id) if body.message_id else None,
            reply_to_msg_id=str(body.reply_to_msg_id) if body.reply_to_msg_id else None,
            error=f"gestão do grupo: {kind}",
        )
        self.db.insert_signal(mgmt_signal)

        if body.historical:
            # history reload: register the instruction but NEVER act on live
            # trades with an old message
            return {"action": "management", "kind": kind, "historical": True,
                    "applied": False, "signal": mgmt_signal.to_dict()}

        if kind == "go":
            # "Do it now team" — o admin confirma a entrada imediatamente: se
            # o sinal ainda estiver na janela de edição, finaliza já (auto →
            # executa agora; manual → pendente de aprovação). Sem sinal em
            # espera, regista apenas — nunca fecha nada nem dispara risco.
            target = self._target_signal_for(body)
            if not target or target.status != "awaiting_edit":
                awaiting = self.db.awaiting_edit_signals()
                if awaiting:
                    target = max(awaiting, key=lambda s: s.received_at or "")
                else:
                    target = None
            if target and target.status == "awaiting_edit" and not target.error:
                if self.config["mode"] == "auto":
                    res = await self._try_execute(target)
                    note = ("executado já" if res.get("action") == "executed"
                            else f"finalizado ({res.get('action', '?')})")
                else:
                    self.db.set_signal_status(target.id, "pending")
                    note = "pendente de aprovação (modo manual)"
                await self.notifier.send(
                    "system",
                    f"⚡ GO do grupo: \"{body.raw_text.strip()[:60]}\" → sinal "
                    f"{target.symbol} {note} (janela de edição encurtada)",
                )
                return {"action": "management", "kind": kind, "applied": True,
                        "target_signal": target.id, "signal": mgmt_signal.to_dict()}
            return {"action": "management", "kind": kind, "applied": False,
                    "signal": mgmt_signal.to_dict()}

        trades = self._open_trades()
        if not trades:
            # no open trade at all: if the message replies to a signal that
            # is still buffered (edit window) or pending, queue it so it
            # applies when that signal executes — otherwise register + ignore
            replied = self._target_signal_for(body)
            if replied and replied.id != signal_id:
                self._queue_management(body, action, signal_id, received_at, replied)
                await self.notifier.send(
                    "system",
                    f"📨 {body.source}: \"{body.raw_text.strip()[:80]}\" "
                    f"→ resposta ao sinal {replied.symbol} (ainda sem trade) "
                    f"— aplicada quando a trade abrir",
                )
                return {"action": "management", "kind": kind, "applied": False,
                        "queued": True, "target_signal": replied.id,
                        "signal": mgmt_signal.to_dict()}
            if replied is not None and replied.status in ("awaiting_edit", "pending", "executed"):
                # the replied signal exists and is still alive: queue (the
                # trade opens later) — handled above; this branch is only
                # reached when the signal is no longer actionable
                pass
            elif replied is not None:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Gestão do grupo sem alvo: \"{body.raw_text.strip()[:60]}\" "
                    f"responde ao sinal {replied.symbol} de "
                    f"{(replied.received_at or '')[:16].replace('T', ' ')} "
                    f"(estado: {replied.status}) que já não está aberto — "
                    f"instrução ignorada. Verifica se o sinal foi rejeitado/SL "
                    f"antes da gestão chegar.",
                )
            else:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Gestão do grupo sem alvo: \"{body.raw_text.strip()[:60]}\" "
                    f"sem trade aberta e sem sinal a que responder — ignorada. "
                    f"Confirma se o sinal correspondente chegou ao bot.",
                )
            await self.notifier.send(
                "system",
                f"📨 {body.source}: \"{body.raw_text.strip()[:80]}\" "
                f"(sem trade aberta — ignorado)",
            )
            return {"action": "management", "kind": kind, "applied": False,
                    "signal": mgmt_signal.to_dict()}

        # pick the target: (1) the trade whose signal this message replies to,
        # (2) same symbol, (3) most recent open trade
        trade = self._target_trade(body, trades)

        notes = []
        from app.strategies import get_strategy as _get_strat
        main_strat = _get_strat(self.config.get("strategy"))
        main_kinds = main_strat.get("manage_kinds") or set()
        applies_real = kind in main_kinds
        msg_id = str(body.message_id) if body.message_id else None
        if not applies_real:
            notes.append(f"não aplica à estratégia atual ({main_strat['nome']})")
        elif kind == "tp_hit":
            tp = action.get("tp", 1)
            if tp == 1:
                moved = self._protect_remaining(trade)
                if moved:
                    notes.append("pernas restantes movidas a breakeven")
            notes.append(f"TP{tp} confirmado pelo grupo")
        elif kind == "sl_hit":
            notes.append("SL confirmado pelo grupo — a reconciliar com o broker")
        elif kind == "close_tp1":
            # "Close TP1 here" — partial profit only: leg 1 closes at market,
            # remaining legs keep their SL untouched.
            closed = self.sim.close_leg_at_market(trade.id, 1,
                                                  reason="close_tp1", msg_id=msg_id)
            if closed:
                notes.append(f"perna 1 fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
            else:
                notes.append("perna 1 já fechada")
        elif kind == "close_tp":
            # "Close TP2 here" / "Close TP5 when happy" — partial profit numa
            # perna específica: fecha só a perna N ao mercado, as restantes
            # mantêm o SL.
            leg = int(action.get("tp", 2))
            closed = self.sim.close_leg_at_market(trade.id, leg,
                                                  reason="close_tp", msg_id=msg_id)
            if closed:
                notes.append(f"perna {leg} fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
            else:
                notes.append(f"perna {leg} já fechada")
        elif kind == "protect":
            # A híbrida já tem escada/trailing mecânicos. O protect do grupo
            # passa por um merge monotónico: nunca recua o SL automático nem
            # fecha a perna 1 a um preço pior que a proteção já conhecida.
            if (main_strat.get("id") == "auto_escada_3_hibrida"
                    and hasattr(self.sim, "apply_hybrid_protect")):
                buffer = self._be_buffer(trade)
                group_sl = (trade.entry + buffer
                            if trade.direction == "buy" else trade.entry - buffer)
                hybrid_events = self.sim.apply_hybrid_protect(
                    trade, group_sl=group_sl, now=_now_iso(), msg_id=msg_id,
                )
                closed_events = [e for e in hybrid_events
                                 if e.get("type") == "close"]
                if closed_events:
                    total = sum(e.get("pnl", 0.0) for e in closed_events)
                    notes.append(
                        f"protect híbrido: {len(closed_events)} perna(s) fechada(s) "
                        f"(P&L {total:+.2f})"
                    )
                notes.append("SL fundido com a proteção mecânica (nunca recua)")
            else:
                # comportamento das estratégias com gestão normal
                closed = self.sim.close_leg_at_market(trade.id, 1,
                                                      reason="protect", msg_id=msg_id)
                moved = self._protect_remaining(trade, above_entry=True)
                if closed:
                    notes.append(f"perna 1 fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
                if moved:
                    notes.append("SL das restantes movido acima da melhor entrada")
        elif kind == "move_sl":
            # "Adjust SL to 4331" — move the stop loss of EVERY open leg to
            # the level the group indicates (trailing / risk management).
            new_sl = action.get("sl")
            if new_sl:
                moved = 0
                for p in self.db.positions_for_trade(trade.id):
                    if p.status != "open":
                        continue
                    if self.sim.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                        moved += 1
                notes.append(f"SL de {moved} pernas movido para {new_sl}" if moved
                             else "SL não movido (pernas já fechadas?)")
            else:
                notes.append("nudge sem nível — registado, sem ação")
        elif kind == "close_all":
            # "out at BE": remaining legs are already protected at breakeven
            # (the update loop lets the SL close them); "we're out": close
            # everything at market now.
            if action.get("be"):
                remaining = [p for p in self.db.positions_for_trade(trade.id)
                             if p.status == "open" and p.leg != 1]
                if remaining:
                    self._protect_remaining(trade)
                    notes.append(f"out a BE confirmado — {len(remaining)} pernas "
                                 f"protegidas na entrada (SL já em breakeven)")
                else:
                    notes.append("out a BE — sem pernas restantes")
            else:
                events = self.sim.close_trade_at_market(trade.id,
                                                        reason="close_all", msg_id=msg_id)
                if events:
                    pnl = sum(e.get("pnl", 0.0) for e in events if e.get("type") == "close")
                    notes.append(f"trade fechada ao mercado (P&L {pnl:+.2f})")
                else:
                    notes.append("trade já sem pernas abertas")

        # corrida paralela: cada estratégia de teste aplica a gestão à MESMA
        # trade em papel SE a honrar (manage_kinds) — antes a corrida paper
        # ignorava a gestão e as contas "+ gestão" comportavam-se como as
        # sem gestão até o preço bater SL/TP.
        try:
            self.paper.apply_management(
                trade.signal_id, kind,
                tp=action.get("tp"), sl=action.get("sl"),
                be=bool(action.get("be")), msg_id=msg_id,
            )
        except Exception as exc:
            print(f"[paper] falha ao aplicar gestão: {exc}")

        text = (f"📨 {body.source}: {body.raw_text.strip()[:80]}")
        if notes:
            text += f" → {'; '.join(notes)}"
        await self.notifier.send("system", text)
        return {"action": "management", "kind": kind, "applied": True,
                "trade_id": trade.id, "notes": notes, "signal": mgmt_signal.to_dict()}

    async def _handle_tp_sl_update(self, body: SignalIn, signal_id: str,
                                   received_at: str) -> Optional[dict]:
        """\"TP1 4734 SL 4745\" replying to a signal: the group confirms or
        corrects TP1 and/or the stop loss of the replied signal's trade.

        Recorded as a management-style signal (status `management`) and, if
        the target trade is open, the SL of every open leg is moved to the
        new level and the DB signal's TP1/SL are updated (ground truth).
        Returns None when the message is not of this shape or has no target.
        """
        m = re.match(
            r"^\s*TP\s*1\s+([\d.]+)\s*(?:SL|stop\s*loss)\s+([\d.]+)\s*$",
            (body.raw_text or "").strip(), re.IGNORECASE,
        )
        if not m:
            return None
        tp1 = float(m.group(1)); new_sl = float(m.group(2))
        target = self._target_signal_for(body)
        if not target:
            return None
        # registar a mensagem (auditoria) — nunca como sinal de entrada
        mgmt = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol=target.symbol or symbol_in(body.raw_text) or "",
            direction="", entry=0.0, stop_loss=0.0,
            take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at,
            message_id=str(body.message_id) if body.message_id else None,
            reply_to_msg_id=str(body.reply_to_msg_id) if body.reply_to_msg_id else None,
            error=f"gestão do grupo: tp_sl_update (TP1 {tp1} · SL {new_sl})",
        )
        self.db.insert_signal(mgmt)
        if body.historical:
            return {"action": "management", "kind": "tp_sl_update",
                    "historical": True, "applied": False,
                    "signal": mgmt.to_dict()}
        notes = []
        # atualiza o TP1/SL do sinal alvo (ground truth do grupo)
        if target.take_profit_1 != tp1 or target.stop_loss != new_sl:
            self.db.update_signal_levels(target.id, tp1=tp1, sl=new_sl)
            notes.append(f"sinal {target.symbol}: TP1→{tp1} · SL→{new_sl}")
        trade = self._target_trade(body, self._open_trades())
        if trade and trade.signal_id == target.id:
            moved = 0
            for p in self.db.positions_for_trade(trade.id):
                if p.status != "open":
                    continue
                if self.sim.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                    moved += 1
            if moved:
                notes.append(f"SL de {moved} pernas movido para {new_sl}")
        text = f"📨 {body.source}: {body.raw_text.strip()[:60]}"
        if notes:
            text += f" → {'; '.join(notes)}"
        await self.notifier.send("system", text)
        return {"action": "management", "kind": "tp_sl_update", "applied": True,
                "target_signal": target.id, "notes": notes,
                "signal": mgmt.to_dict()}

    # ------------------------------------------- reply-based correlation

    def _target_signal_for(self, body: SignalIn) -> Optional[Signal]:
        """The signal this management message replies to (Telegram
        reply_to_msg_id), if it exists in the DB — regardless of whether its
        trade is open yet."""
        if not body.reply_to_msg_id:
            return None
        group_key = (body.group or "").strip()
        target_id = (f"tg-{group_key}-{body.reply_to_msg_id}"
                     if group_key else f"tg-{body.reply_to_msg_id}")
        return self.db.get_signal(target_id)

    def _target_trade(self, body: SignalIn, trades: List[Trade]) -> Optional[Trade]:
        """Pick the trade a management message refers to:
        1. the open trade of the signal it replies to (exact correlation),
        2. an open trade of the named symbol,
        3. the most recent open trade."""
        replied = self._target_signal_for(body)
        if replied:
            for t in trades:
                if t.signal_id == replied.id:
                    return t
        sym = symbol_in(body.raw_text or "")
        candidates = [t for t in trades if not sym or t.symbol == sym]
        if not candidates:
            candidates = trades
        return max(candidates, key=lambda t: t.opened_at or "") if candidates else None

    def _queue_management(self, body: SignalIn, action: dict, signal_id: str,
                          received_at: str, target: Signal) -> None:
        """Queue a management instruction whose target signal has no open
        trade yet; `_apply_pending_management` runs it when the trade opens."""
        self.db.insert_pending_management({
            "id": signal_id,
            "signal_id": signal_id,
            "reply_to_msg_id": str(target.message_id) if target.message_id else target.id,
            "kind": action["kind"],
            "tp": action.get("tp"),
            "sl": action.get("sl"),
            "raw_text": body.raw_text or "",
            "source": body.source or "",
            "received_at": received_at or "",
            "created_at": _now_iso(),
        })

    async def _apply_pending_management(self, signal: Signal, trade: Trade) -> int:
        """Apply management instructions the group posted while `signal` was
        still buffered (queued in `pending_management`). Returns how many
        were applied."""
        key = str(signal.message_id) if signal.message_id else signal.id
        pending = self.db.pending_management_for(key)
        applied = 0
        for rec in pending:
            kind = rec["kind"]
            if kind == "close_tp1":
                closed = self.sim.close_leg_at_market(trade.id, 1, reason="close_tp1")
                if closed:
                    applied += 1
            elif kind == "close_tp":
                leg = int(rec.get("tp") or 2)
                if self.sim.close_leg_at_market(trade.id, leg, reason="close_tp"):
                    applied += 1
            elif kind == "protect":
                if self.config.get("strategy") == "auto_escada_3_hibrida":
                    buffer = self._be_buffer(trade)
                    group_sl = (trade.entry + buffer
                                if trade.direction == "buy" else trade.entry - buffer)
                    self.sim.apply_hybrid_protect(
                        trade, group_sl=group_sl, now=_now_iso(),
                        msg_id=rec.get("id"),
                    )
                else:
                    self.sim.close_leg_at_market(trade.id, 1, reason="protect")
                    self._protect_remaining(trade, above_entry=True)
                applied += 1
            elif kind == "close_all":
                self.sim.close_trade_at_market(trade.id, reason="close_all")
                applied += 1
            elif kind == "tp_hit":
                if int(rec.get("tp") or 1) == 1:
                    if self._protect_remaining(trade):
                        applied += 1
            elif kind == "move_sl":
                new_sl = rec.get("sl")
                if new_sl:
                    for p in self.db.positions_for_trade(trade.id):
                        if p.status == "open":
                            self.sim.move_leg_sl(trade.id, p.leg, new_sl)
                    applied += 1
            elif kind == "sl_hit":
                # the broker SL already protects; nothing to do on open
                applied += 1
            # corrida paralela: aplica também às trades de papel do sinal
            try:
                self.paper.apply_management(
                    trade.signal_id, kind,
                    tp=rec.get("tp"), sl=rec.get("sl"),
                    be=False, msg_id=rec.get("id"),
                )
            except Exception as exc:
                print(f"[paper] falha ao aplicar gestão pendente: {exc}")
            self.db.mark_pending_management_applied(rec["id"])
        return applied

    def _protect_remaining(self, trade, above_entry: bool = False) -> int:
        """Move the SL of every remaining open leg to breakeven (default) or
        just above the entry ("risk free", locks a small profit). Returns
        the number of legs moved."""
        moved = 0
        for p in self.db.positions_for_trade(trade.id):
            if p.leg == 1 or p.status != "open":
                continue
            new_sl = trade.entry
            if above_entry:
                # a small buffer above entry (buy) / below (sell): lock profit
                buffer = self._be_buffer(trade)
                new_sl = trade.entry + buffer if trade.direction == "buy" \
                    else trade.entry - buffer
            if self.sim.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                moved += 1
        return moved

    @staticmethod
    def _be_buffer(trade) -> float:
        """Small profit-lock buffer above/below entry for the group's
        \"risk free\" step: ~10% of the entry-to-SL distance, at least one
        pip, capped so it never crosses TP1."""
        risk = abs(trade.entry - trade.stop_loss)
        if risk <= 0:
            return 0.0
        buffer = min(max(risk * 0.1, risk * 0.01), abs(trade.take_profit_1 - trade.entry))
        return round(buffer, 4)

    async def _handle_summary(self, body: SignalIn, signal_id: str,
                              received_at: str) -> dict:
        """Parse and store the group's periodic summary (ground truth)."""
        fields = parse_summary(body.raw_text or "") or {}
        if fields:
            fields["raw_text"] = body.raw_text
            self.db.insert_group_summary(body.source, fields, received_at or _now_iso(),
                                         message_id=str(body.message_id or ""))
        # also record the raw message so the history stays complete
        summary_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol="", direction="", entry=0.0, stop_loss=0.0,
            take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at, error="resumo do grupo (ground truth)",
        )
        self.db.insert_signal(summary_signal)
        # Só resumos AO VIVO notificam o feed + divergência — os do replay
        # histórico (restart do listener) ficam guardados como ground truth
        # sem inundar a dashboard com notificações repetidas.
        if fields and fields.get("trades_sent") and not body.historical:
            await self.notifier.send(
                "summary",
                f"📊 {body.source}: {fields['trades_sent']} trades, "
                f"WR {fields['win_rate']:.0f}% (grupo) | "
                f"{fields['winning']} win / {fields['stop_loss']} SL / "
                f"{fields['breakevens']} BE | +{fields['pips']} pips",
            )
            await self._check_group_divergence(body, fields)
        return {"action": "management", "kind": "summary",
                "parsed": bool(fields), "signal": summary_signal.to_dict()}

    async def _check_group_divergence(self, body: SignalIn, fields: dict,
                                      threshold_pp: float = 10.0) -> None:
        """Compare the group's reported win rate with the bot's own backtest
        on the SAME real signals. When they drift apart by more than
        `threshold_pp`, warn in the dashboard feed — the strategy may be
        diverging from what the group claims."""
        if not fields.get("win_rate"):
            return
        try:
            edge = await self.edge_analysis(group=body.source or "", days=30)
        except Exception as exc:
            print(f"[divergência] edge indisponível: {exc}")
            return
        bot_wr = (edge.get("aggregate") or {}).get("win_rate") or 0.0
        group_wr = float(fields["win_rate"])
        diff = group_wr - bot_wr
        if abs(diff) < threshold_pp:
            return
        direction = "acima" if diff > 0 else "abaixo"
        await self.notifier.send(
            "risk",
            f"⚠️ Divergência grupo vs bot: o grupo reporta {group_wr:.0f}% WR "
            f"({fields['trades_sent']} trades) mas o backtest do bot dá "
            f"{bot_wr:.0f}% — o grupo está {direction} do bot por "
            f"{abs(diff):.0f}pp. Revisita a gestão/execução antes de confiar "
            f"nos números do grupo.",
        )

    # ------------------------------------------------------------ analytics

    def report(self, days: int = 7) -> dict:
        """Relatório de qualidade: win rate, risco/recompensa, por grupo."""
        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        signals = self.db.signals_since(since)
        trades = self.db.trades_closed_since(since)

        groups = {}
        for s in signals:
            g = s.source or "desconhecido"
            row = groups.setdefault(g, {"signals": 0, "trades": 0, "wins": 0, "pnl": 0.0})
            row["signals"] += 1
        for t in trades:
            sig = self.db.get_signal(t.signal_id)
            g = (sig.source if sig else "") or "desconhecido"
            row = groups.setdefault(g, {"signals": 0, "trades": 0, "wins": 0, "pnl": 0.0})
            row["trades"] += 1
            row["pnl"] = round(row["pnl"] + t.pnl, 2)
            if t.pnl > 0:
                row["wins"] += 1

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))

        rr = {i: [] for i in range(1, 6)}
        for s in signals:
            risk = abs(s.entry - s.stop_loss)
            if risk > 0:
                for i in range(1, 6):
                    tp = float(getattr(s, f"take_profit_{i}", 0.0) or 0.0)
                    if tp > 0:
                        rr[i].append(abs(tp - s.entry) / risk)

        return {
            "period": {"days": days, "since": since, "today": date.today().isoformat()},
            "signals": {
                "total": len(signals),
                "by_group": {g: v["signals"] for g, v in groups.items()},
            },
            "trades": {
                "closed": len(trades),
                "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
                "pnl": round(sum(t.pnl for t in trades), 2),
                "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0.0,
                "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0.0,
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            },
            "risk_reward": {
                f"avg_tp{i}": round(sum(v) / len(v), 2) if v else 0.0
                for i, v in rr.items()
            },
            "equity_curve": self._equity_curve(trades),
            "drawdown": self._max_drawdown(trades, self._balance()),
            "by_day": self._by_day(trades),
            "groups": [
                {
                    "group": g,
                    "signals": v["signals"],
                    "trades": v["trades"],
                    "wins": v["wins"],
                    "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0,
                    "pnl": v["pnl"],
                }
                for g, v in sorted(groups.items())
            ],
        }

    def operational_metrics(self, days: int = 90) -> dict:
        """Aggregated Item 10 telemetry for the report and diagnostics.

        The counters are intentionally independent from the live account: an
        empty result means no evidence was collected yet, not zero failures.
        Interpret spread/reconnect/modify trends only after the documented
        two-week observation window.
        """
        return self.db.operational_metrics(days=days)

    @staticmethod
    def _by_day(trades) -> list:
        """P&L, trades e win rate por dia (para o mini-gráfico do Relatório)."""
        days: dict[str, dict] = {}
        for t in sorted(trades, key=lambda x: x.closed_at or ""):
            d = (t.closed_at or "")[:10]
            if not d:
                continue
            row = days.setdefault(d, {"day": d, "trades": 0, "wins": 0, "pnl": 0.0})
            row["trades"] += 1
            row["pnl"] = round(row["pnl"] + t.pnl, 2)
            if t.pnl > 0:
                row["wins"] += 1
        out = []
        for d, row in sorted(days.items()):
            out.append({
                "day": d,
                "trades": row["trades"],
                "wins": row["wins"],
                "win_rate": round(row["wins"] / row["trades"] * 100, 1) if row["trades"] else 0.0,
                "pnl": row["pnl"],
            })
        return out

    @staticmethod
    def _equity_curve(trades) -> list:
        """Curva de equity: P&L acumulado ao longo do tempo (começa em 0)."""
        curve = []
        run = 0.0
        for t in sorted(trades, key=lambda x: x.closed_at or ""):
            run += t.pnl
            curve.append({"t": t.closed_at, "pnl": round(run, 2)})
        return curve

    @staticmethod
    def _max_drawdown(trades, balance: float) -> dict:
        """Drawdown máximo (pico-a-vale) do P&L acumulado, em $ e % da conta."""
        run = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted(trades, key=lambda x: x.closed_at or ""):
            run += t.pnl
            peak = max(peak, run)
            max_dd = max(max_dd, peak - run)
        pct = (max_dd / balance * 100) if balance > 0 else 0.0
        return {"amount": round(max_dd, 2), "pct": round(pct, 1)}

    async def simulate(self, body: SimulateIn) -> dict:
        """Backtest: replay os sinais reais do histórico sobre candles reais,
        como se a trade fosse feita `delay_seconds` depois do sinal."""
        from datetime import timedelta
        from app.backtest import aggregate, simulate_signal
        from app.execution.feed import YahooProvider

        since = (date.today() - timedelta(days=body.days)).isoformat()
        signals = [
            s for s in self.db.signals_since(since)
            if s.symbol and s.direction and s.entry and not s.error
        ]
        if not signals:
            return {"ok": True, "simulated": 0, "aggregate": aggregate([]),
                    "results": [], "message": "sem sinais no período"}

        provider = YahooProvider()
        by_symbol = {}
        for s in signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        results = []
        for symbol, sym_signals in by_symbol.items():
            try:
                candles = await provider.history(symbol)
            except Exception as exc:
                print(f"[simulate] sem histórico para {symbol}: {exc}")
                continue
            if not candles:
                continue
            for s in sym_signals:
                res = simulate_signal(s, candles, body.risk_pct, self._balance(),
                                      body.delay_seconds, body.spread_pips,
                                      body.slippage_pips, body.commission_per_lot)
                results.append({
                    "id": s.id, "source": s.source, "symbol": s.symbol,
                    "direction": s.direction, "entry": s.entry,
                    "received_at": s.received_at, **res,
                })

        results.sort(key=lambda r: r["received_at"], reverse=True)
        return {
            "ok": True,
            "simulated": len(results),
            "risk_pct": body.risk_pct,
            "delay_seconds": body.delay_seconds,
            "days": body.days,
            "spread_pips": body.spread_pips,
            "slippage_pips": body.slippage_pips,
            "commission_per_lot": body.commission_per_lot,
            "aggregate": aggregate(results),
            "results": results,
        }

    # -------------------------------------------------------- edge analysis

    async def edge_analysis(self, group: str = "", days: int = 30,
                            risk_pct: Optional[float] = None,
                            sim_balance: Optional[float] = None) -> dict:
        """Replay the group's real signals on real candles and return the verdict.

        Thin wrapper over `app.analysis.run_analysis` so the dashboard tab
        shows the same numbers as the CLI, refreshing live as new signals land.
        `risk_pct`/`sim_balance` opcionais — para estudos de risco (None =
        config/valor real)."""
        from app.analysis import run_analysis
        payload = await run_analysis(self.db, group=group, days=days,
                                     risk_pct=risk_pct,
                                     balance=sim_balance or self._balance())
        # keep the response light for the dashboard (drop raw texts/results detail)
        for r in payload.get("results", []):
            r.pop("id", None)
        # ground truth: the group's own reported win rate for comparison
        payload["group_summaries"] = self.db.list_group_summaries(
            source_like=group, limit=3)
        return payload

    async def compare_analysis(self, group: str = "", days: int = 30,
                               since: str = "",
                               risk_pct: Optional[float] = None,
                               sim_balance: Optional[float] = None) -> dict:
        """A/B backtest: TP1..TP5 (nova) vs + gestão vs fechar no TP2, lado a
        lado, nos MESMOS sinais reais do grupo. Nunca abre trades.
        `since` (YYYY-MM-DD) filtra o período (ex.: só o formato atual).
        `risk_pct`/`sim_balance` opcionais — para estudos de risco."""
        from app.analysis import run_compare
        payload = await run_compare(self.db, group=group, days=days,
                                    balance=sim_balance or self._balance(),
                                    since=since, risk_pct=risk_pct)
        for side in ("new", "manage", "tp2"):
            for r in payload[side].get("results", []):
                r.pop("id", None)
        return payload

    def edit_delay_stats(self, group: str = "", limit: int = 500) -> dict:
        """Distribuição do atraso das edições do grupo (para validar a janela)."""
        return self.db.edit_delay_stats(source_like=group, limit=limit)

    async def protect_latency_report(self, strategy_id: str = "auto_escada_3_hibrida",
                                     engine: str = "", limit: int = 500,
                                     days: int = 0, since: str = "") -> dict:
        """Relatório da decisão protect da híbrida (paper/MT5 persistido ou
        backtest recalculado sem efeitos laterais)."""
        from app.analysis import protect_latency_report
        return await protect_latency_report(
            self.db, strategy_id=strategy_id, engine=engine, limit=limit,
            days=days, since=since,
        )

    async def protect_causal_report(self, strategy_id: str = "auto_escada_3_hibrida",
                                     limit: int = 500, days: int = 0,
                                     since: str = "") -> dict:
        """Atribuição causal, por ordem temporal, dos ``protect`` da híbrida.

        Compara cada prefixo de eventos com a mesma auto-gestão sem protect;
        é um replay read-only e não escreve na BD.
        """
        from app.analysis import protect_causal_report
        return await protect_causal_report(
            self.db, strategy_id=strategy_id, limit=limit,
            days=days, since=since,
        )

    async def month_simulation(self, group: str = "", days: int = 30,
                               strategy_id: Optional[str] = None,
                               since: str = "",
                               risk_pct: Optional[float] = None,
                               sim_balance: Optional[float] = None) -> dict:
        """Simulação do último mês para UMA estratégia + curva de equity.
        `since` (YYYY-MM-DD) filtra os sinais a partir dessa data — o bloco
        "desde o início" usa-o para comparar o formato atual vs tudo.
        `risk_pct`/`sim_balance` opcionais — para estudos de risco."""
        from app.analysis import run_month_sim
        return await run_month_sim(self.db, group=group, days=days,
                                   risk_pct=risk_pct,
                                   balance=sim_balance or self._balance(),
                                   strategy_id=strategy_id, since=since)

    def strategies_info(self) -> dict:
        """Registo central de estratégias + qual é a principal."""
        from app.strategies import MAIN_STRATEGY_ID, strategies_list
        return {
            "ok": True,
            "main": MAIN_STRATEGY_ID,
            "strategies": strategies_list(),
        }

    async def trace_signal(self, strategy_id: str = "tp2", group: str = "",
                           days: int = 0, since: str = "") -> dict:
        """Exemplo do fluxo real de uma trade (página de teste): o sinal
        resolvido mais recente da estratégia, re-simulado com trace — eventos
        SL/TP1/BE/fecho + caminho de preços."""
        from app.analysis import trace_signal
        return await trace_signal(self.db, strategy_id=strategy_id, group=group,
                                  days=days, since=since)

    async def real_vs_backtest(self, strategy_id: Optional[str] = None) -> dict:
        """Compara as trades REAIS fechadas da conta demo com o backtest do
        mesmo sinal na estratégia principal (slippage de entrada, diferença de
        saída, timing e P&L por trade) — tabela do Relatório (SPEC v2 §3.4)."""
        from app.analysis import real_vs_backtest
        return await real_vs_backtest(self.db, strategy_id=strategy_id)

    async def _fase3_promotion_snapshot(self, sid: str) -> dict:
        """Snapshot gravado quando uma auto com auto-gestão é promovida a
        principal (início da janela de validação): stats da corrida paper,
        DD das contas (curva 30d), divergences paper↔MT5 e a auditoria de SL
        até ao momento — para comparar com o estado no fim da janela
        (ESTRATEGIAS.md Parte X, antes/depois)."""
        import json
        from datetime import datetime, timedelta, timezone
        auto = self.db.paper_trade_stats(sid)
        ref = self.db.paper_trade_stats("nova_gestao")
        since = (datetime.now(timezone.utc) - timedelta(hours=720)).isoformat(timespec="seconds")

        def _dd(s: str) -> Optional[float]:
            pts = self.db.paper_balance_history(s, since)
            peak = None
            mx = 0.0
            for p in pts:
                v = float(p.get("balance") or 0)
                if peak is None or v > peak:
                    peak = v
                if peak and peak > 0:
                    mx = max(mx, (peak - v) / peak * 100)
            return round(mx, 1) if pts else None

        sl_entries = sum(
            len(json.loads(v)) for k, v in self.db.all_config().items()
            if k.startswith("autosl:") and v)
        pvr = await self.paper_vs_real_auto(strategy_id=sid)
        real_closed = len([t for t in self.db.list_trades(500)
                           if t.status == "closed"])
        return {
            "promoted_at": _now_iso(),
            "strategy_id": sid,
            "n": auto["closed"], "pf": auto["profit_factor"],
            "pnl": auto["pnl"],
            "ref_pf": ref["profit_factor"], "ref_pnl": ref["pnl"],
            "dd_auto": _dd(sid), "dd_ref": _dd("nova_gestao"),
            "paper_vs_real_pairs": len(pvr.get("pairs", [])),
            "real_trades_closed": real_closed,
            "sl_audit_entries": sl_entries,
        }

    async def paper_vs_real_auto(self, strategy_id: Optional[str] = None) -> dict:
        """Fase 3 — trade REAL de uma auto estratégia (quando promovida a
        principal no MT5) vs a trade de PAPEL da mesma estratégia no        mesmo sinal: mesma decisão de saída, Δ preço de fecho, Δ timing e P&L de
        cada lado (tabela do Relatório; ESTRATEGIAS.md Parte X — divergence
        report paper↔MT5)."""
        from app.analysis import paper_vs_real_auto
        return await paper_vs_real_auto(self.db, strategy_id=strategy_id)

    def auto_sl_log(self) -> dict:
        """Auditoria dos três níveis de SL da auto-gestão no MT5 (Fase 3,
        ESTRATEGIAS.md Parte X): por trade, cada tentativa de modify com target→valid→
        applied + retcode (10009 aceite / 10016 recusado) e se foi
        arredondado/clamped pelo broker. Persistida na config (`autosl:<id>`)
        — sobrevive a restarts. Vazio até uma auto ser promovida."""
        import json
        rows = []
        for key, val in self.db.all_config().items():
            if not key.startswith("autosl:") or not val:
                continue
            try:
                entries = json.loads(val)
            except (ValueError, TypeError):
                continue
            if not isinstance(entries, list):
                continue
            trade_id = key.split(":", 1)[1]
            trade = self.db.get_trade(trade_id)
            rows.append({
                "trade_id": trade_id,
                "signal_id": trade.signal_id if trade else None,
                "symbol": trade.symbol if trade else None,
                "closed_at": trade.closed_at if trade else None,
                "entries": entries,
            })
        rows.sort(key=lambda r: (r.get("closed_at") or r.get("entries", [{}])[-1].get("ts") or ""),
                  reverse=True)
        return {"ok": True, "trades": rows}

    async def fase3_status(self) -> dict:
        """Payload ÚNICO do painel Fase 3 (substitui as 4 chamadas
        separadas): ETA (com a curva do ritmo), snapshot antes/depois,
        veredictos + histórico, registo de alterações de alvo, divergences
        paper↔MT5, real vs backtest, DD das contas (curva 30d) e a auditoria
        de SL — `GET /api/analysis/fase3_status`."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=720)).isoformat(timespec="seconds")

        def _dd(sid: str) -> Optional[float]:
            pts = self.db.paper_balance_history(sid, since)
            peak = None
            mx = 0.0
            for p in pts:
                v = float(p.get("balance") or 0)
                if peak is None or v > peak:
                    peak = v
                if peak and peak > 0:
                    mx = max(mx, (peak - v) / peak * 100)
            return round(mx, 1) if pts else None

        pvr = await self.paper_vs_real_auto(strategy_id="auto_escada_3")
        rvb = await self.real_vs_backtest()
        return {
            "ok": True,
            "eta": self._fase3_eta(),
            "snapshot": self._fase3_snapshot(),
            "verdict": self._fase3_verdict(),
            "verdicts": self._fase3_verdicts(),
            "target_log": self._fase3_target_log(),
            "config": {"fase3_target": int(self.config.get("fase3_target") or 20)},
            "paper": {
                "auto": self.db.paper_trade_stats("auto_escada_3"),
                "hybrid": self.db.paper_trade_stats("auto_escada_3_hibrida"),
                "ref": self.db.paper_trade_stats("nova_gestao"),
            },
            "paper_vs_real": pvr,
            "real_vs_backtest": rvb,
            "sl_log": self.auto_sl_log(),
            "dd": {"auto_escada_3": _dd("auto_escada_3"),
                   "auto_escada_3_hibrida": _dd("auto_escada_3_hibrida"),
                   "nova_gestao": _dd("nova_gestao")},
        }

    async def walk_forward(self, group: str = "", days: int = 30,
                           buckets: int = 4) -> dict:
        """Walk-forward da estratégia com gestão: métricas por janela
        temporal, para ver se o edge se mantém ao longo do tempo."""
        from app.analysis import run_walk_forward
        return await run_walk_forward(self.db, group=group, days=days,
                                      balance=self._balance(), buckets_n=buckets)

    # -------------------------------------------------------------- config

    async def update_config(self, body: ConfigUpdate) -> dict:
        if body.mode is not None:
            if body.mode.value == "auto" and self.config.get("auto_kill"):
                return {"error": "bloqueio automático ativo — reativa o trading primeiro"}
            self.config["mode"] = body.mode.value
        if body.risk_pct is not None:
            self.config["risk_pct"] = body.risk_pct
        if body.daily_loss_limit_pct is not None:
            self.config["daily_loss_limit_pct"] = body.daily_loss_limit_pct
        if body.max_positions is not None:
            self.config["max_positions"] = body.max_positions
        if body.sim_speed is not None:
            self.config["sim_speed"] = body.sim_speed
        if body.edit_window_seconds is not None:
            self.config["edit_window_seconds"] = body.edit_window_seconds
        strategy_changed = False
        if body.strategy is not None:
            from app.strategies import STRATEGIES
            if body.strategy not in STRATEGIES:
                return {"error": f"estratégia desconhecida: {body.strategy}"}
            strategy_changed = body.strategy != self.config.get("strategy")
            self.config["strategy"] = body.strategy
        if body.teste_days is not None:
            self.config["teste_days"] = int(body.teste_days)
        if body.paper_race_hours is not None:
            self.config["paper_race_hours"] = int(body.paper_race_hours)
        if body.fase3_target is not None:
            # alvo da janela da Fase 3 (default 20; o feed permite baixar para
            # 15 quando o ETA estimar > 6 semanas) — clamp 10–20. A mudança é
            # registada no histórico (`fase3_target_log`) com quem/quando/
            # motivo — a janela de validação usa-o para contexto.
            novo = max(10, min(20, int(body.fase3_target)))
            antigo = int(self.config.get("fase3_target") or 20)
            if novo != antigo:
                import json as _j
                entry = {
                    "at": _now_iso(),
                    "from": antigo, "to": novo,
                    "reason": (body.fase3_target_reason or "").strip()
                    or (f"redução manual (POST /api/config) — "
                        f"{antigo} → {novo}"),
                }
                log = self._fase3_target_log()
                log.append(entry)
                self.db.set_config("fase3_target_log",
                                   _j.dumps(log[-20:], ensure_ascii=False))
            self.config["fase3_target"] = novo
        self._persist_config(["mode", "risk_pct", "daily_loss_limit_pct",
                              "max_positions", "sim_speed", "edit_window_seconds",
                              "strategy", "teste_days", "paper_race_hours",
                              "fase3_target"])
        if strategy_changed:
            # promoção de estratégia: notificação no feed com os parâmetros
            # que passam a ser usados na execução real (max_legs/exit_at_tp/lote)
            s = self._strategy_params()
            await self.notifier.send(
                "system",
                f"♟ Estratégia promovida a principal: {s['nome']} — a execução "
                f"real passa a usar: {_strategy_params_text(s)}",
            )
            # Snapshot da corrida paper em TODA a promoção (painel Fase 2/3,
            # feed ou páginas de teste): grava o estado da corrida paper, DD,
            # divergences e auditoria de SL até ao momento — o antes/depois
            # fica registado para QUALQUER estratégia promovida (Parte X)
            try:
                import json as _json
                snap = await self._fase3_promotion_snapshot(body.strategy)
                self.db.set_config("fase3_snapshot", _json.dumps(snap))
            except Exception as exc:
                print(f"[fase3] snapshot de promoção falhou: {exc}")
            # janela de validação da Fase 3: só reabre quando uma AUTO com
            # auto-gestão é promovida (o veredicto atual é limpo; o histórico
            # `fase3_verdicts` fica para auditoria)
            from app.strategies import STRATEGIES as _S
            _strat = _S.get(body.strategy, {})
            if (_strat.get("stair_levels") or _strat.get("trail_factor")
                    or _strat.get("confirm_filter") or _strat.get("time_stop_minutes")):
                self.db.set_config("fase3_verdict", "")
        else:
            await self.notifier.send(
                "system",
                f"⚙️ Config atualizada: modo={self.config['mode']}, "
                f"risco={self.config['risk_pct']}%",
            )
        return {"ok": True, "config": self.snapshot()["config"]}

    async def history_done(self, body: dict) -> dict:
        """Fim do replay do histórico do listener: notificação no feed com os
        totais reencaminhados (mensagens, sinais, gestão, resumos, ignoradas)
        e guarda o resumo do último replay na config (tab Diagnóstico)."""
        total = int(body.get("total") or 0)
        if total <= 0:
            return {"ok": True, "notified": False}
        sinais = int(body.get("sinais") or 0)
        gestao = int(body.get("gestao") or 0)
        resumos = int(body.get("resumos") or 0)
        ignoradas = int(body.get("ignoradas") or 0)
        grupo = (body.get("group") or "").replace("tg:", "") or "grupo"
        # último replay (data + totais) para a tab Diagnóstico
        self.db.set_config("replay_done_at", _now_iso())
        self.db.set_config("replay_group", grupo)
        self.db.set_config("replay_total", str(total))
        self.db.set_config("replay_sinais", str(sinais))
        self.db.set_config("replay_gestao", str(gestao))
        self.db.set_config("replay_resumos", str(resumos))
        self.db.set_config("replay_ignoradas", str(ignoradas))
        parts = [f"{sinais} sinais", f"{gestao} gestão"]
        if resumos:
            parts.append(f"{resumos} resumos")
        parts.append(f"{ignoradas} ignoradas/já conhecidas")
        await self.notifier.send(
            "system",
            f"📚 Replay do histórico concluído ({grupo}): {total} mensagens "
            f"· {' · '.join(parts)}",
        )
        return {"ok": True, "notified": True}

    async def set_kill_switch(self, enabled: bool) -> dict:
        self.config["kill_switch"] = enabled
        if not enabled:
            self.config["auto_kill"] = False
        self._persist_config(["kill_switch", "auto_kill"])
        verb = "PAUSADO" if enabled else "REATIVADO"
        await self.notifier.send("system", f"🛑 Trading {verb} (kill switch)")
        return {"ok": True, "config": self.snapshot()["config"]}

    # ------------------------------------------------------------- tick

    def _record_balance_snapshots(self) -> None:
        """Grava o saldo de cada estratégia da corrida paralela a cada ~60s:
        cada uma (incluindo a principal) usa a sua conta virtual própria de
        1000 € — a curva "quem está a ganhar" compara todas em igualdade de
        circunstâncias (não o saldo real da MT5)."""
        import time as _time
        now = _time.time()
        if now - self._last_balance_snap < 60:
            return
        self._last_balance_snap = now
        from app.execution.paper import START_BALANCE
        from app.strategies import (HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES,
                                    STRATEGIES)
        ts = _now_iso()
        for sid in STRATEGIES:
            if sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES:
                continue
            bal = self.db.ensure_paper_account(sid, START_BALANCE)
            try:
                self.db.insert_paper_balance_history(sid, ts, bal)
            except Exception as exc:
                print(f"[balance-snap] {sid}: {exc}")
        # linha de referência da conta real (MT5): não entra na corrida
        # virtual — é só o "dinheiro a sério" ao lado das contas de teste
        # (estratégia reservada `__real__`, sem conta paper).
        try:
            self.db.insert_paper_balance_history("__real__", ts, self._balance())
        except Exception as exc:
            print(f"[balance-snap] __real__: {exc}")

    async def tick(self) -> None:
        await self.finalize_awaiting_signals()
        speed = float(self.config["sim_speed"])
        await self.market.refresh(speed)
        # O motor MT5 observa bid/ask reais a uma cadência limitada (não um
        # INSERT por segundo). Em simulação este hook não faz nada.
        try:
            observe_spreads = getattr(self.sim, "record_spread_observations", None)
            if observe_spreads is not None:
                observe_spreads(_now_iso())
        except Exception as exc:
            self.db.record_exception(
                exc, context={"component": "mt5", "phase": "spread_observation"}
            )
            print(f"[mt5] spread observation falhou: {exc}")
        self._update_peak_balance()
        self._record_balance_snapshots()
        await self._check_paper_race_leader()

        from app.strategies import get_strategy
        strat = get_strategy(self.config.get("strategy"))
        # Fase 3 (MT5 demo): passa os parâmetros da auto-gestão da estratégia
        # ao motor — a `nova_gestao` não os tem (auto_mgmt desligada: sem
        # stair_levels/trail_factor/confirm/time_stop), por isso nada muda
        # até uma estratégia auto_* ser promovida a principal.
        auto = None
        if strat.get("stair_levels") or strat.get("trail_factor") or \
                strat.get("confirm_filter") or strat.get("time_stop_minutes") \
                or strat.get("def_window"):
            auto = {k: strat[k] for k in ("stair_levels", "stair_from",
                                          "confirm_filter", "trail_factor",
                                          "time_stop_minutes", "atr_k",
                                          "atr_period", "atr_timeframe",
                                          "def_window", "def_trail",
                                          "def_time_stop")}
        events = self.sim.update(exit_at_tp=strat["exit_at_tp"], auto=auto)
        # paper trading forward: atualiza as posições de papel das estratégias
        # de teste contra os preços atuais (mesma lógica SL/TP/BE)
        try:
            paper_events = self.paper.tick(float(self.config["risk_pct"]))
            for ev in paper_events:
                if ev["type"] == "trade_closed":
                    await self.notifier.send(
                        "system",
                        f"🧪 [teste] {ev['strategy_id']}: trade {ev['symbol']} fechada "
                        f"em papel | P&L {ev['pnl']:+.2f}",
                    )
                elif ev["type"] == "divergence":
                    # ligação backtest↔vivo: a auto (Fase 2) saiu diferente da
                    # referência no mesmo sinal (ou a referência saiu e a auto
                    # continua) — notifica para investigar porquê
                    sid = ev["strategy_id"]
                    if ev.get("reason") == "open":
                        text = (f"⚖️ {ev['symbol']}: a referência nova_gestao fechou "
                                f"({ev['ref_reason']} · P&L {ev['ref_pnl']:+.2f}) mas a "
                                f"{sid} continua aberta — o grupo saiu, a auto não")
                    else:
                        text = (f"⚖️ {ev['symbol']}: {sid} saiu por {ev['reason']} "
                                f"(P&L {ev['pnl']:+.2f}) vs nova_gestao {ev['ref_reason']} "
                                f"(P&L {ev['ref_pnl']:+.2f}) — divergência no mesmo sinal")
                    await self.notifier.send("divergence", text)
        except Exception as exc:
            print(f"[paper] falha ao atualizar trades de teste: {exc}")
        for ev in events:
            if ev["type"] == "close":
                # MT5: the broker already applied the P&L to the account — the
                # config balance is only the paper (sim) source of truth.
                if self._mt5_account() is None:
                    self.config["balance"] = round(self._balance() + ev["pnl"], 2)
                    self.db.set_config("balance", self.config["balance"])
                await self.notifier.send(
                    "trade_close",
                    f"{'✅' if ev['pnl'] >= 0 else '❌'} Fechada perna {ev['leg']} "
                    f"{ev['symbol']} @ {ev['price']} | P&L {ev['pnl']:+.2f}",
                )
            elif ev["type"] == "be":
                await self.notifier.send(
                    "system",
                    f"🛡️ {ev['symbol']}: TP1 atingido — pernas restantes protegidas a breakeven",
                )
            elif ev["type"] == "trade_closed":
                await self.notifier.send(
                    "trade_close",
                    f"📦 Trade {ev['symbol']} fechada | P&L total {ev['pnl']:+.2f}",
                )
            elif ev["type"] == "auto_sl_warn":
                # Fase 3 — janela de validação: um modify de SL da auto-gestão
                # foi RECUSADO pelo broker (10016) ou ARREDONDADO (clamped) —
                # o SL real pode não ser o alvo teórico (auditoria autosl:)
                if ev.get("reason") == "clamped":
                    text = (f"⚠️ SL da auto-gestão arredondado pelo broker: perna "
                            f"{ev['leg']} {ev['symbol']} ({ev['kind']}) — target "
                            f"{ev['target']} → valid {ev['valid']} "
                            f"(stops_level/freeze do broker). Ver auditoria no painel Fase 3.")
                else:
                    text = (f"⛔ Modify de SL recusado pelo broker: perna {ev['leg']} "
                            f"{ev['symbol']} ({ev['kind']}) — target {ev['target']} → "
                            f"retcode {ev['retcode']} (10016 = stops inválidos). O SL fica "
                            f"no nível anterior (auditoria autosl: no painel Fase 3).")
                await self.notifier.send("risk", text)

        await self._check_mt5_connection()
        await self._check_disk_space()
        await self._check_regime()
        await self._check_fase3_validation()
        await self._check_fase3_eta_alert()

        # auto kill switch: pause trading if today's loss limit is hit
        if not self.config.get("auto_kill") and self.risk.daily_loss_exceeded(
            self._balance(), self._realized_today(), self.config
        ):
            self.config["kill_switch"] = True
            self.config["auto_kill"] = True
            self._persist_config(["kill_switch", "auto_kill"])
            await self.notifier.send(
                "risk",
                "🚨 Limite de perda diária atingido — trading pausado "
                "automaticamente. Reativa manualmente na dashboard.",
            )

        await self._check_daily_summary()
        await self.ws.broadcast({"type": "snapshot", "data": self.snapshot()})

    async def _check_regime(self) -> None:
        """Alerta de regime: corre o sim rolante (30 dias, estratégia ativa,
        com o kill diário da config) e, se o profit factor cair abaixo de 1
        com ≥ 10 trades resolvidas, notifica (risk no feed + Telegram) —
        com cooldown de 6h para não spammar. Expõe `regime` no snapshot."""
        import time as _time
        now = _time.time()
        if now - self._last_regime_check < 1800:  # a cada ~30 min
            return
        self._last_regime_check = now
        try:
            from app.analysis import run_analysis
            from app.strategies import get_strategy, sim_params
            sid = self.config.get("strategy")
            strat = get_strategy(sid)
            # O alerta de regime tem de correr a MESMA configuração que a
            # estratégia ativa nos outros motores. Antes passava apenas
            # max_legs/gestão/exit_at_tp e ignorava escada, trailing, modo
            # defensivo e ATR — podia declarar PF<1 numa auto que a produção
            # realmente não estava a simular.
            payload = await run_analysis(
                self.db, group="", days=30, balance=self._balance(),
                strategy_id=sid, **sim_params(sid),
            )
            agg = payload.get("aggregate") or {}
            pf = agg.get("profit_factor")
            self._regime_status = {
                "pf": pf, "resolved": agg.get("resolved", 0),
                "checked_at": _now_iso(), "skipped": payload.get("daily_stop_skipped", 0),
            }
            nome = strat["nome"]
            if pf is not None and pf < 1.0 and (agg.get("resolved") or 0) >= 10 \
                    and now - self._regime_alert_at > 21600:  # cooldown 6h
                self._regime_alert_at = now
                await self.notifier.send(
                    "risk",
                    f"⚠️ Regime em degradação: a estratégia {nome} está com "
                    f"profit factor {pf:.2f} no sim rolante de 30 dias "
                    f"({agg.get('resolved')} trades resolvidas, kill diário "
                    f"a {self.config.get('daily_loss_limit_pct', 3)}%). "
                    f"Considera rever o risco ou a estratégia ativa.",
                    tg_text=f"⚠️ *Regime em degradação*: a estratégia *{nome}* "
                    f"está com profit factor *{pf:.2f}* no sim rolante de 30 "
                    f"dias ({agg.get('resolved')} trades). Considera rever o "
                    f"risco ou a estratégia ativa na dashboard.",
                )
        except Exception as exc:
            print(f"[regime] falha ao verificar: {exc}")

    async def _check_fase3_eta_alert(self) -> None:
        """Alerta do ETA da Fase 3 (cooldown 24h): avisa no feed/Telegram
        quando a janela dos 20 sinais passar a estimar MAIS DE 6 SEMANAS ao
        ritmo atual, e/ou quando o ritmo de sinais mudar ≥ 50% face ao mês
        anterior (o ETA foi recalculado com o ritmo novo)."""
        import time as _time
        eta = self._fase3_eta()
        if not eta or eta.get("done") or not eta.get("eta"):
            return
        now = _time.time()
        if now - self._fase3_eta_alert_at < 86400:  # cooldown 24h
            return
        eta_days = eta.get("eta_days") or 0
        pace = eta.get("pace") or 0
        pace_prev = eta.get("pace_prev")
        change = eta.get("pace_change_pct")
        target = eta.get("target") or 20
        msgs = []
        if eta_days > 42:
            msgs.append(f"a janela dos {target} sinais está a estimar "
                        f"{eta_days:.0f} dias (> 6 semanas) ao ritmo atual de "
                        f"{pace:.2f} sinais/dia — ETA {eta.get('eta', '')[:10]}")
        if change is not None and abs(change) >= 50:
            dir_ = "subiu" if change > 0 else "desceu"
            prev_txt = f"{pace_prev:.2f}" if pace_prev else "—"
            msgs.append(f"o ritmo de sinais {dir_} {abs(change):.0f}% face ao "
                        f"mês anterior ({prev_txt} → {pace:.2f} sinais/dia) — "
                        f"ETA recalculado para {eta.get('eta', '')[:10]}")
        if not msgs:
            return
        self._fase3_eta_alert_at = now
        # quando a janela estimar > 6 semanas, o feed ganha um botão para
        # BAIXAR o alvo de 20 para 15 (com confirmação no frontend) — o
        # Telegram não tem botão, só o texto
        action = "fase3_target:15" if (eta_days > 42 and target > 15) else ""
        await self.notifier.send(
            "system",
            "📅 ETA da Fase 3: " + "; ".join(msgs) + ".",
            tg_text="📅 *ETA da Fase 3*: " + "; ".join(msgs) + ".",
            action=action,
        )

    async def _check_fase3_validation(self) -> None:
        """Fim da janela de validação da Fase 3 (ESTRATEGIAS.md Parte X):
        delega o cálculo do veredicto na função standalone
        `_compute_fase3_verdict` (testável só com a BD) e aplica a decisão:
        rollback → volta à `nova_gestao`; manter → continua. Notifica
        feed/Telegram uma vez por janela (o veredicto fica persistido)."""
        import json as _json
        sid = self.config.get("strategy")
        verdict = await _compute_fase3_verdict(self.db, sid)
        if verdict is None:
            return
        # Promotion can replace the snapshot while the async analysis above
        # is still running. Never persist a verdict computed for that stale
        # generation (otherwise a newly opened window is immediately closed
        # again by the old background task).
        try:
            current_snapshot = _json.loads(
                self.db.all_config().get("fase3_snapshot") or "null"
            )
        except (ValueError, TypeError):
            current_snapshot = None
        if not current_snapshot or current_snapshot.get("promoted_at") != \
                verdict.get("snapshot_promoted_at"):
            return
        decision = verdict["decision"]
        self.db.set_config("fase3_verdict", _json.dumps(verdict, ensure_ascii=False))
        # histórico persistido (últimos 20) — a reabertura da janela NÃO apaga
        # os veredictos antigos (auditoria da evolução das auto_* ao longo do
        # tempo; exposto em snapshot()["fase3_verdicts"])
        hist = self._fase3_verdicts()
        hist.append(verdict)
        self.db.set_config("fase3_verdicts",
                           _json.dumps(hist[-20:], ensure_ascii=False))
        if decision == "rollback":
            self.config["strategy"] = "nova_gestao"
            self._persist_config(["strategy"])
            await self.notifier.send(
                "risk",
                f"↩️ Janela de validação fechada com rollback: a {sid} reprovou "
                f"({verdict['reason']}) — a execução real voltou à nova_gestao. "
                f"A janela reabre se voltares a promover a auto depois de "
                f"investigar as divergências (auditoria de SL no painel Fase 3).",
                tg_text=f"↩️ *Janela de validação fechada — rollback*: a *{sid}* "
                f"reprovou ({verdict['reason']}). A execução real voltou à "
                f"*nova_gestao*. A janela reabre se a promoveres de novo.",
            )
        else:
            pf = verdict.get("pf")
            await self.notifier.send(
                "system",
                f"🏁 Janela de validação da Fase 3 fechada: {sid} MANTIDA "
                f"(PF {pf if pf is not None else '—'} · mesma saída que o papel "
                f"em {verdict['same_exit_n']}/{verdict['window_trades']} · "
                f"{verdict['structural_divergences']} divergências · "
                f"{verdict['positions_without_sl']} sem SL · "
                f"{verdict['slow_opens']} aberturas lentas · "
                f"{verdict['pnl_incoherent']} P&L incoerentes). "
                f"Continua na principal — o SPEC v3 (conta real) pode ser "
                f"preparado quando a demo acumular mais estabilidade.",
                tg_text=f"🏁 *Janela de validação fechada*: a *{sid}* foi "
                f"*MANTIDA* (PF {pf if pf is not None else '—'} · mesma saída "
                f"{verdict['same_exit_n']}/{verdict['window_trades']} · "
                f"{verdict['structural_divergences']} divergências · "
                f"{verdict['slow_opens']} lentas · "
                f"{verdict['pnl_incoherent']} P&L incoer.).",
            )

    async def _check_disk_space(self, force: bool = False) -> dict:
        """Check diário de espaço livre e alerta antes de a VPS encher.

        ``SIGNALBOT_DISK_PATH``/``SIGNALBOT_DISK_FREE_GB`` permitem um teste
        manual sem alterar o disco.  O check é read-only e o estado fica no
        snapshot/diagnóstico para o alerta ser auditável.
        """
        from app.maintenance import disk_status
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        stored_config = self.db.all_config()
        already_checked = (today == self._disk_last_check_day
                           or stored_config.get("disk_last_check_day") == today)
        if not force and already_checked and self._disk_status:
            return self._disk_status
        path = os.getenv("SIGNALBOT_DISK_PATH") or None
        raw_threshold = os.getenv("SIGNALBOT_DISK_FREE_GB", "10")
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            threshold = 10.0
        try:
            status = disk_status(path=path, threshold_gb=threshold)
        except (OSError, ValueError) as exc:
            status = {"path": str(path or ""), "error": str(exc),
                      "checked_at": _now_iso(), "below_threshold": False}
        self._disk_status = status
        self._disk_last_check_day = today
        self.db.set_config("disk_last_check_day", today)
        if status.get("below_threshold") and (force or
                stored_config.get("disk_alert_day") != today):
            await self.notifier.send(
                "risk",
                f"💽 Espaço em disco baixo: {status.get('free_gb', 0):.2f} GB livres "
                f"em {status.get('path')} (limiar {status.get('threshold_gb')} GB). "
                "Rotaciona logs e confirma os backups.",
            )
            self.db.set_config("disk_alert_day", today)
        return status

    async def _check_mt5_connection(self) -> None:
        """Alerta quando a ligação ao MT5 cair (ou o AutoTrading desligar):
        notificação na dashboard (com som) + push para o Telegram do
        utilizador se TELEGRAM_BOT_TOKEN estiver configurado. Com cooldown
        de 60s para não spammar se a ligação flutuar."""
        ex = self._execution_state()
        connected = bool(ex.get("connected"))
        algo = bool(ex.get("trade_allowed"))
        prev = self._mt5_was_connected
        prev_algo = self._mt5_was_trade_allowed
        self._mt5_was_connected = connected
        self._mt5_was_trade_allowed = algo
        if not str(ex.get("mode", "")).startswith("mt5"):
            return
        if prev is None:
            self.db.record_mt5_connection_event(
                "connected" if connected else "disconnected", "initial_state"
            )
            return  # primeiro tick — só regista o estado inicial
        if prev != connected:
            self.db.record_mt5_connection_event(
                "connected" if connected else "disconnected", "state_transition"
            )
        if prev_algo is not None and prev_algo != algo:
            self.db.record_mt5_connection_event(
                "autotrading_on" if algo else "autotrading_off", "state_transition"
            )
        import time
        now = time.time()
        if prev and not connected and now - self._last_mt5_offline_notif > 60:
            self._last_mt5_offline_notif = now
            await self.notifier.send(
                "risk",
                "🔌 MT5 DESLIGADO — o terminal perdeu a ligação. As trades "
                "abertas não são geridas até voltar. Verifica o terminal.",
            )
        elif connected and not algo and now - self._last_mt5_offline_notif > 60:
            self._last_mt5_offline_notif = now
            await self.notifier.send(
                "risk",
                "⚙️ AutoTrading DESLIGADO no MT5 — sem AutoTrading o bot não "
                "consegue abrir/fechar trades. Liga-o no terminal.",
            )

    async def _check_paper_race_leader(self) -> None:
        """Alerta quando uma estratégia de teste passa a ganhar à principal
        na corrida paralela e sugere a troca quando a liderança se mantém
        consistente (>= 30 min seguidos à frente). Notificação `race` no feed
        (+ Telegram, a roxo). Só dispara com um mínimo de trades fechadas
        para o teste não "ganhar" por ter 1 trade de sorte."""
        import time as _time
        from app.strategies import HIDDEN_STRATEGIES
        accts = self.paper_status().get("accounts", [])
        main_acct = next((a for a in accts if a.get("is_main")), None)
        princ = (float(main_acct["balance"]) if main_acct
                 and main_acct.get("balance") is not None else self._balance())
        # só sugere troca para estratégias visíveis (tp2/tp2_protect saíram
        # do menu — não há página para promovê-las)
        tests = [a for a in accts if not a.get("is_main")
                 and a.get("balance") is not None
                 and a.get("strategy_id") not in HIDDEN_STRATEGIES]
        tests = [a for a in tests if (a.get("closed") or 0) >= 3]  # mínimo de trades
        if not tests:
            return
        best = max(tests, key=lambda a: a["balance"])
        leader_id = best["strategy_id"] if best["balance"] > princ else None
        now = _time.time()
        if leader_id and leader_id != self._race_leader:
            # muda o líder: notifica (com cooldown mínimo para não spammar
            # em cada arranque do servidor)
            if now - self._race_leader_since > 300 or self._race_leader is None:
                from app.strategies import get_strategy
                nome = get_strategy(leader_id)["nome"]
                await self.notifier.send(
                    "race",
                    f"🏁 {nome} (teste) ultrapassou a principal na corrida "
                    f"paralela: {cur(best['balance'])} vs {cur(princ)} "
                    f"(conta virtual) — {best.get('closed') or 0} trades "
                    f"fechadas. Vê a página de teste para comparar.",
                    tg_text=(
                        f"🏁 *{nome}* (teste) ultrapassou a *principal* na "
                        f"corrida paralela!\n"
                        f"💰 Teste: *{cur(best['balance'])}* vs principal: "
                        f"*{cur(princ)}*\n"
                        f"📊 {best.get('closed') or 0} trades fechadas — "
                        f"vê a página de teste na dashboard para comparar."
                    ),
                )
            self._race_leader = leader_id
            self._race_leader_since = now
            self._race_switch_suggested = None  # novo líder — pode sugerir de novo
        elif not leader_id and self._race_leader is not None:
            self._race_leader = None  # a principal voltou a ganhar — pode
            self._race_switch_suggested = None
            # voltar a disparar quando um teste a ultrapassar de novo

        # Sugestão de auto-switch: o líder está à frente há tempo suficiente
        # (consistência) e ainda não sugerimos esta troca.
        if leader_id and leader_id == self._race_leader:
            held = now - self._race_leader_since
            if held >= 1800 and leader_id != self._race_switch_suggested:
                from app.strategies import get_strategy
                self._race_switch_suggested = leader_id
                nome = get_strategy(leader_id)["nome"]
                await self.notifier.send(
                    "race",
                    f"🔄 Sugestão de troca: {nome} está a ganhar à principal "
                    f"consistentemente há {int(held // 60)} min na corrida "
                    f"paper ({cur(best['balance'])} vs {cur(princ)}).",
                    tg_text=(
                        f"🔄 *Sugestão de troca de estratégia*\n"
                        f"🏆 *{nome}* está a ganhar à principal há "
                        f"*{int(held // 60)} min* na corrida paper\n"
                        f"💰 Teste: *{cur(best['balance'])}* vs principal: "
                        f"*{cur(princ)}*\n"
                        f"Usa o botão *Promover* na página de teste para "
                        f"mudar a estratégia ativa."
                    ),
                    action=f"promote:{leader_id}",
                )

    # ------------------------------------------------------- daily summary

    async def _check_daily_summary(self) -> None:
        today = date.today().isoformat()
        if self.config.get("summary_sent_date") == today:
            return
        try:
            hh, mm = str(self.config["summary_time"]).split(":")
            target = time(int(hh), int(mm))
        except (ValueError, AttributeError):
            return
        if datetime.now().time() >= target:
            await self.daily_summary()

    async def daily_summary(self) -> dict:
        today = date.today().isoformat()
        trades = self.db.trades_closed_on(today)
        pnl = round(sum(t.pnl for t in trades), 2)
        wins = [t for t in trades if t.pnl > 0]
        if trades:
            text = (
                f"📊 Resumo diário ({today})\n"
                f"Trades fechadas: {len(trades)}\n"
                f"Profit: {pnl:+.2f}\n"
                f"Win rate: {len(wins) / len(trades) * 100:.0f}%"
            )
        else:
            text = f"📊 Resumo diário ({today}): sem trades fechadas hoje."
        # 🏁 corrida da Fase 2: saldo/PF da auto_escada_3 vs referência e o
        # progresso para os critérios do SPEC v2 (N ≥ 20 · PF ≥ 1.5)
        race = self._phase2_race_line()
        if race:
            text += "\n\n🏁 " + race
        # 🚀 Fase 3 (ESTRATEGIAS.md Parte X): quando a auto está na principal, mostra o estado
        # da auto MT5 — N real e P&L real das trades fechadas da janela
        f3 = self._fase3_mt5_line()
        if f3:
            text += "\n\n🚀 " + f3
        self.config["summary_sent_date"] = today
        self.db.set_config("summary_sent_date", today)
        await self.notifier.send("summary", text)
        return {"ok": True, "text": text}

    def _fase3_mt5_line(self) -> str:
        """Linha da auto MT5 para o resumo diário (ESTRATEGIAS.md Parte X): quando a
        Fase 3 está a correr (auto com auto-gestão promovida + snapshot),
        mostra N real e P&L real das trades fechadas desde o início da janela
        e o estado da janela (em curso x/10 ou veredicto).

        Se a janela fechou HOJE (veredicto recente) mesmo com a auto já
        destituída (rollback automático voltou à nova_gestao), o resumo do
        dia continua a mostrar a linha — destacando o veredicto."""
        from datetime import datetime, timezone
        from app.strategies import STRATEGIES as _S
        sid = self.config.get("strategy")
        snap = self._fase3_snapshot()
        # janela fechada hoje (veredicto do dia — inclui rollback); compara em
        # UTC (o evaluated_at é _now_iso, UTC explícito — date.today() local
        # daria falsos negativos perto da meia-noite)
        verdicts = self._fase3_verdicts()
        today = datetime.now(timezone.utc).date().isoformat()
        if verdicts and (verdicts[-1].get("evaluated_at") or "")[:10] == today:
            v = verdicts[-1]
            keep = v.get("decision") == "maintain"
            if keep:
                return (f"Auto MT5 (Fase 3) · janela fechada hoje: auto MANTIDA "
                        f"· PF {v.get('pf') if v.get('pf') is not None else '—'} "
                        f"· {v.get('window_trades')} trades · "
                        f"{v.get('structural_divergences')} divergências")
            return (f"Auto MT5 (Fase 3) · ⚠️ janela fechada hoje com ROLLBACK: "
                    f"a {v.get('strategy_id')} reprovou ({v.get('reason')}) — "
                    f"a principal voltou à nova_gestao")
        # Fase 3 a correr: auto na principal com snapshot
        if not sid or not sid.startswith("auto_"):
            return ""
        strat = _S.get(sid, {})
        if not (strat.get("stair_levels") or strat.get("trail_factor")
                or strat.get("confirm_filter") or strat.get("time_stop_minutes")):
            return ""
        if not snap:
            return ""
        promoted_at = snap.get("promoted_at") or ""
        closed = _fase3_window_trades(self.db, sid, promoted_at)
        pnl = round(sum(t.pnl or 0 for t in closed), 2)
        v = self._fase3_verdict()
        if v:
            keep = "MANTIDA" if v.get("decision") == "maintain" else "ROLLBACK"
            verdict_txt = f" · janela fechada ({keep})"
        else:
            verdict_txt = f" · janela de validação {len(closed)}/10"
        return (f"Auto MT5 (Fase 3) · {sid} na principal · {len(closed)} trades "
                f"reais fechadas · P&L {pnl:+.2f}{verdict_txt}")

    def _phase2_race_line(self) -> str:
        """Linha de estado da corrida da Fase 2 para o resumo diário (e para
        reutilizar na dashboard): auto_escada_3 vs nova_gestao — saldo, PF e
        quantos sinais faltam para os critérios do SPEC v2 (N ≥ 20 · PF ≥
        1.5 · P&L ≥ 50% da referência · DD ≤ 125%)."""
        accts = (self.paper_status() or {}).get("accounts", [])
        auto = next((a for a in accts if a["strategy_id"] == "auto_escada_3"), None)
        ref = next((a for a in accts if a["strategy_id"] == "nova_gestao"), None)
        if not auto or not ref:
            return ""
        n = int(auto.get("closed") or 0)
        falta = max(0, 20 - n)
        pf = auto.get("profit_factor")
        pf_txt = f"{pf:.2f}" if pf is not None else "—"
        ref_pf = ref.get("profit_factor")
        ref_txt = f"{ref_pf:.2f}" if ref_pf is not None else "—"
        return (
            f"Corrida Fase 2 · auto_escada_3 {auto.get('balance', 0):.2f} € "
            f"(PF {pf_txt}) vs referência nova_gestao {ref.get('balance', 0):.2f} € "
            f"(PF {ref_txt}) · {n}/20 sinais resolvidos — faltam {falta} "
            f"para os critérios (N≥20 · PF≥1.5)"
        )
