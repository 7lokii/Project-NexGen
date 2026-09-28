"""BotState — the orchestrator.

Owns the configuration, the simulated account, and the signal pipeline:

    signal in -> parse -> risk checks -> execute (único caminho auto — Item 12)

Also drives the per-second market tick: advance prices, let the strategy
manager close legs at TP1/TP2/SL/breakeven, apply realized P&L to the
account, watch the daily-loss limit (auto kill switch) and schedule the
daily summary.

Everything here runs on the FastAPI event loop, so there are no races
between the API endpoints and the background tasks.
"""

import asyncio
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that converts inf/nan to None so the WS never breaks."""
    def iterencode(self, o, _one_shot=False):
        return super().iterencode(self._clean(o), _one_shot=_one_shot)

    @staticmethod
    def _clean(o):
        if isinstance(o, float):
            if math.isinf(o) or math.isnan(o):
                return None
            return o
        if isinstance(o, dict):
            return {k: _SafeEncoder._clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_SafeEncoder._clean(v) for v in o]
        return o


def safe_json(obj) -> str:
    """json.dumps that never raises on inf/nan floats."""
    return json.dumps(obj, cls=_SafeEncoder)
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

# Fase 3 (janela de validação + veredicto) vive em app/fase3.py — reexportado
# aqui com os mesmos nomes (testes/estudos importam de app.state).
from app.fase3 import _compute_fase3_verdict, _fase3_window_trades  # noqa: F401,E402



# Helpers de formatación viven en app/formatting.py — reexportados aquí.
from app.formatting import (
    _iso_age_seconds,
    _lots_text,
    _now_iso_plus,
    _strategy_params_text,
    _tps_text,
    cur,
)  # noqa: F401,E402
# Recounts (handle/audit/candle-check/batches) vivem em app/recounts.py —
# misturados no BotState para o método continuar em `self`.
from app.recounts import RecountsMixin  # noqa: E402
# Notificações WhatsApp (fecho de trades reais) vivem em app/whatsapp_mixin.py —
# misturadas no BotState.
from app.whatsapp_mixin import WhatsAppMixin  # noqa: E402
# Guardas de entrada e gates de risco (entry guard, lote mínimo, cap de
# pernas) vivem em app/gates.py — misturadas no BotState.
from app.gates import GatesMixin  # noqa: E402
# Conta/lanes (saldo, equity, risco dinâmico, posições MT5) vivem em
# app/lanes.py — misturadas no BotState.
from app.lanes import LanesMixin  # noqa: E402
# Pipeline de ingestão de sinais (janela, revive, execução) vive em
# app/ingest_mixin.py — misturada no BotState.
from app.ingest_mixin import IngestMixin  # noqa: E402
# Gestão do grupo (TP/SL hits, close/protect, pending management, resumos)
# vive em app/management_mixin.py — misturada no BotState.
from app.management_mixin import ManagementMixin  # noqa: E402

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

# Passive group narrative. These patterns deliberately do not classify a
# message as an executable management instruction: progress/levels are
# evidence for audit only.
_PROGRESS_PIPS = re.compile(r"\+\s*(\d+(?:[.,]\d+)?)\s*pips?\b", re.IGNORECASE)
_LEVEL_MENTION = re.compile(
    r"\b(?:say\s+hi\s+to|hello\s+to|target(?:ing)?|heading\s+to|at)\s+"
    r"(\d+(?:[.,]\d+)?)\b", re.IGNORECASE,
)


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
        data = safe_json(payload)
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                self.connections.discard(ws)


class BotState(RecountsMixin, WhatsAppMixin, GatesMixin, LanesMixin,
              IngestMixin, ManagementMixin):
    def __init__(self, db: DB, market: MarketSimulator, risk: RiskEngine,
                 sim: SimEngine, notifier: Notifier, ws: WSManager,
                 whatsapp: Any = None, second_sim: Any = None,
                 second_strategy: Optional[str] = None,
                 second_label: Optional[str] = None):
        self.db = db
        self.market = market
        self.risk = risk
        self.sim = sim
        self.notifier = notifier
        self.ws = ws
        # Último snapshot construído pelo tick (~1×/s): os endpoints read-only
        # servem-no em vez de reconstruírem o estado (que lê o broker MT5) por
        # request no event loop único — ver `last_snapshot()`.
        self._last_snapshot: Optional[dict] = None
        # Vigia do event loop (stalls/fases lentas) — criado lazy; ver
        # `stall_recorder` (app/stallwatch.py).
        self._stall_recorder_obj: Optional[Any] = None
        # Segunda lane de execução (estratégia principal paralela noutra conta
        # demo MT5). Lane "main" = conta principal atual (`self.sim`, sempre
        # resolvido dinamicamente para os testes poderem re-wire o motor);
        # lane "tp2" = conta paralela (tp2_protect), fixa (`self.sim2`).
        self.sim2 = second_sim
        self.lane_strategies: Dict[str, Optional[str]] = {
            "main": None, "tp2": second_strategy,
        }
        self.lane_labels: Dict[str, Optional[str]] = {
            "main": None, "tp2": second_label or "tp2_protect",
        }
        # WhatsAppNotifier opcional (resultados de trades reais num grupo
        # WhatsApp via bridge Baileys) — None/desligado = comportamento atual.
        self.whatsapp = whatsapp
        self.config: Dict[str, Any] = self._load_config()
        # Corrige high-watermarks herdados de outra conta antes do primeiro
        # cálculo de risco. O teste é conservador: só rebasa valores claramente
        # incompatíveis com o saldo/equity atualmente configurado.
        self._normalize_lane_peaks()
        # A configuração persistida é a fonte de verdade após restart; os
        # argumentos do construtor só servem como fallback para instalações
        # antigas que ainda não têm os campos da segunda lane.
        self.lane_strategies["tp2"] = (
            self.config.get("lane2_strategy")
            or self.lane_strategies.get("tp2")
            or "tp2_protect"
        )
        self.lane_labels["tp2"] = (
            self.config.get("lane2_label")
            or self.lane_labels.get("tp2")
            or "Sair no TP2 + protect"
        )
        # Cada engine só pode atualizar as trades da sua lane. Sem este
        # marcador, duas engines partilhando a BD processariam a mesma trade.
        if self.sim is not None:
            self.sim.lane_id = "main"
        if self.sim2 is not None:
            self.sim2.lane_id = "tp2"
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
        self._last_journal_monotonic: Optional[float] = None
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
        # métrica contínua do Item 12: edições pós-execução a sinais que
        # executaram sem janela — alerta quando a taxa passa de 5% com
        # amostra mínima (cooldown 24h; o número vive em /api/edits).
        self._last_edit_metric_check: float = 0.0
        self._edit_metric_alert_at: float = 0.0
        self._edit_metric_status: dict = {
            "post_execution_edited": 0, "post_execution_total": 0,
            "post_execution_edit_rate": 0.0, "checked_at": "",
        }
        # calibração do offset GC=F↔spot: avisa quando passa a estar ativa
        # (≥3 fills XAUUSD) e quando muda materialmente (≥10 pontos) —
        # verificado no tick a cada ~30 min, nunca no primeiro segundo.
        # candles do candle-check por símbolo (MT5 spot + GC=F Yahoo) com
        # cache de 5 min — o re-check em lote reutiliza sem re-buscar
        self._recount_candles_cache: dict = {}
        # offset GC=F↔spot por (dia, símbolo) medido nos fills reais com
        # ambos os preços — o candle-check do GC=F usa-o em vez do offset
        # fixo (cache 5 min, como os candles)
        self._recount_day_offset_cache: dict = {}
        # alerta do ETA da Fase 3 (janela a estimar > 6 semanas ou ritmo
        # com mudança grande vs mês anterior) — cooldown de 24h.
        self._fase3_eta_alert_at: float = 0.0
        # promotion panel cache: the forward gate is read-only but scans paper
        # rows, so do not recompute it on every one-second snapshot.
        self._promotion_cache: Optional[dict] = None
        self._promotion_cache_at: float = 0.0
        self._promotion_cache_db_id: Optional[int] = None
        # check operacional diário do volume livre: read-only, sem alterar a
        # BD nem a conta; o alerta só se repete no dia seguinte.
        self._disk_last_check_day: str = ""
        self._disk_status: dict = {}
        # estatísticas do journal de execução (linha de qualidade diária no
        # snapshot/feed): agregar os últimos 7 dias de `execution_events`
        # custa ~1s+ numa tabela com milhões de linhas (journal ligado a
        # gravar quotes por tick). Com cache de ~60s o snapshot deixa de
        # pagar essa agregação em CADA tick — a causa dos stalls de 4-6s
        # do loop atribuídos a `snapshot.build` (ver app/stallwatch.py).
        self._journal_stats_cache: Optional[dict] = None
        self._journal_stats_at: float = 0.0
        # Cache TTL genérico para blocos do snapshot que são caros de
        # computar (agregações DB sobre tabelas grandes: paper/shadow/fase3/
        # window/risk_preview) mas mudam lentamente. Sem ele, o tick somava
        # ~20 queries por snapshot e `snapshot.build` ficava perto do limiar
        # de stall (2s) mesmo com o broker cacheado. Nunca serve dados de
        # execução: apenas painéis informativos (ver `_ttl_get`).
        self._ttl_cache: dict = {}

    def _ttl_get(self, key: str, ttl: float, fn) -> Any:
        """Valor computado por `fn` com cache TTL de `ttl` segundos.

        Read-only para a execução: serve apenas blocos informativos do
        snapshot (estado das corridas paper/shadow, ETA da Fase 3, janela,
        risk_preview). O TTL é conservador (5–60s) e os handlers que MUTAM o
        estado invalidam a cache inteira (`_invalidate_snapshot_cache`); um
        erro em `fn` devolve o valor e o próximo tick volta a tentar.
        """
        import time as _time
        now = _time.monotonic()
        hit = self._ttl_cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            v = fn()
        except Exception as exc:
            print(f"[ttl:{key}] falhou: {exc}")
            if hit is not None:
                return hit[1]
            raise
        self._ttl_cache[key] = (now, v)
        return v

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
        cfg["rejected_revive_minutes"] = float(
            cfg.get("rejected_revive_minutes") or 10
        )
        cfg["balance"] = float(cfg["balance"])
        cfg["peak_balance"] = float(cfg.get("peak_balance", 0.0) or 0.0)
        # Per-lane keys are not part of DEFAULT_CONFIG; restore any that were
        # persisted so a restart preserves each account's high-watermark,
        # risk base and fixed lots instead of silently falling back to the
        # legacy `risk_pct` / risk-based sizing.
        for key in ("peak_balance:main", "peak_balance:tp2",
                    "risk_pct:main", "risk_pct:tp2",
                    "lots_override:main", "lots_override:tp2"):
            if key in stored:
                cfg[key] = float(stored[key] or 0.0)
        cfg["lane2_balance"] = float(cfg.get("lane2_balance", 0.0) or 0.0)
        cfg["lane2_strategy"] = str(
            cfg.get("lane2_strategy") or "tp2_protect"
        ).strip()
        cfg["lane2_label"] = str(
            cfg.get("lane2_label") or "Sair no TP2 + protect"
        ).strip()
        cfg["kill_switch"] = self._to_bool(cfg["kill_switch"])
        cfg["auto_kill"] = self._to_bool(cfg["auto_kill"])
        cfg["killswitch_rearm_at"] = str(cfg.get("killswitch_rearm_at") or "").strip()
        # Kill switch rearm is only set by explicit user action (dashboard/API);
        # we never impose an automatic deadline on boot.
        cfg["shadow_paper_enabled"] = self._to_bool(
            cfg.get("shadow_paper_enabled", False)
        )
        cfg["execution_journal_enabled"] = self._to_bool(
            cfg.get("execution_journal_enabled", False)
        )
        raw_shadow = cfg.get("shadow_paper_strategies", [])
        if isinstance(raw_shadow, str):
            try:
                parsed_shadow = json.loads(raw_shadow)
            except (TypeError, ValueError):
                parsed_shadow = [part.strip() for part in raw_shadow.split(",")]
        else:
            parsed_shadow = raw_shadow
        if not isinstance(parsed_shadow, list):
            parsed_shadow = []
        cfg["shadow_paper_strategies"] = list(dict.fromkeys(
            str(s).strip() for s in parsed_shadow if str(s).strip()
        ))
        return cfg

    def _normalize_lane_peaks(self) -> None:
        """Rebase high-watermarks that clearly belong to another account.

        Older paper runs persisted a 100,000 EUR peak while the active demo
        account has 1,000 EUR. Keeping that value would reduce the effective
        risk to the floor and make every minimum-lot signal look unsafe. A
        plausible drawdown is preserved; only a peak more than 10x the current
        lane balance (and at least 10,000 units above it) is rebased.
        """
        for lane_id in self._lanes():
            key = "peak_balance" if lane_id == "main" else f"peak_balance:{lane_id}"
            try:
                reference = self._balance_for_lane(lane_id)
                peak = float(self.config.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if reference <= 0 or peak <= max(reference * 10.0, reference + 10000.0):
                continue
            self.config[key] = float(reference)
            self.db.set_config(key, float(reference))

    @staticmethod
    def _to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")

    def _persist_config(self, keys: List[str]) -> None:
        for key in keys:
            value = self.config.get(key)
            if value is None:
                continue  # chave ainda não definida (ex.: risco por lane novo)
            if key == "shadow_paper_strategies":
                value = json.dumps(value, ensure_ascii=False)
            self.db.set_config(key, value)

    def shadow_paper_status(self) -> dict:
        """Runtime view of the isolated shadow-paper feed.

        Shadow trades reuse the paper tables/account machinery, but this
        status makes the safety boundary explicit: it is never the MT5
        engine and does not depend on the principal's mode or kill switch.
        """
        strategies = list(self.config.get("shadow_paper_strategies") or [])
        skipped = getattr(self.paper, "_skipped_daily_stop", {}) or {}
        accounts = []
        open_trades = 0
        for sid in strategies:
            open_count = len(self.db.paper_open_trades(sid, origin="shadow"))
            open_trades += open_count
            balance = self.db.paper_balance(sid, origin="shadow")
            stats = self.db.paper_trade_stats(sid, origin="shadow")
            accounts.append({
                "strategy_id": sid,
                "balance": balance,
                "open_trades": open_count,
                "daily_stop_skipped": skipped.get(f"shadow:{sid}", 0),
                **stats,
            })
        return {
            "enabled": bool(self.config.get("shadow_paper_enabled")),
            "strategies": strategies,
            "accounts": accounts,
            "open_trades": open_trades,
            "origin": "shadow",
            "execution_isolated": True,
        }

    # -------------------------------------------------------------- account

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        """Snapshot fresco — mesmo comportamento de sempre (uso interno /
        endpoints que mutam e devolvem o resultado)."""
        return self._build_snapshot()

    def last_snapshot(self) -> dict:
        """Snapshot mais recente computado pelo market_loop (~1×/s).

        Os endpoints read-only (`/api/status`, WS no arranque) servem ESTE
        snapshot em vez de reconstruírem o estado por request: a construção
        lê o broker MT5/worker (I/O síncrono que, no event loop único,
        congelaria a dashboard e o healthcheck por request). Entre ticks a
        idade é ≤ o intervalo do loop (~1s). Sem tick ainda corrido
        (testes, arranque), computa uma vez on-demand (semântica antiga).
        """
        if self._last_snapshot is None:
            return self._build_snapshot()
        return self._last_snapshot

    def _journal_stats(self) -> dict:
        """`execution_event_stats(7)` com cache de ~60s.

        A agregação GROUP BY sobre a janela de 7 dias de `execution_events`
        (milhões de linhas com o journal ligado) demora ~1s+ e corria dentro
        de TODOS os snapshots do tick — era a causa dos stalls de 4-6s do
        event loop ("sem resposta HTTP" do healthcheck / logins pendurados),
        atribuídos a `snapshot.build` pelo stallwatch. A linha é informativa
        (qualidade diária do journal); frescura de 60s é suficiente. Nunca
        rebenta o snapshot: em falha devolve contagens vazias e o próximo
        tick volta a tentar.
        """
        import time as _time
        if (self._journal_stats_cache is not None
                and _time.monotonic() - self._journal_stats_at < 60.0):
            return self._journal_stats_cache
        try:
            stats = self.db.execution_event_stats(7)
        except Exception as exc:
            print(f"[journal] execution_event_stats falhou: {exc}")
            stats = {"days": 7, "total": 0, "by_kind": []}
        self._journal_stats_cache = stats
        self._journal_stats_at = _time.monotonic()
        return stats

    def _build_snapshot(self) -> dict:
        lanes = self._lanes_snapshot()
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
            "lanes": lanes,
            "config": {
                # Único caminho desde o Item 12 — o modo manual foi removido.
                "mode": "auto",
                "risk_pct": round(float(self.config["risk_pct"]), 2),
                "risk_pct_by_lane": {
                    lane: round(self._base_risk_for_lane(lane), 2)
                    for lane in self._lanes()
                },
                "lots_override_by_lane": {
                    lane: self._fixed_lots_for_lane(lane)
                    for lane in self._lanes()
                },
                "effective_risk_pct": round(self._effective_risk_pct(), 2),
                "peak_balance": round(float(self.config.get("peak_balance") or 0.0), 2),
                "daily_loss_limit_pct": round(float(self.config["daily_loss_limit_pct"]), 2),
                "max_positions": int(self.config["max_positions"]),
                "kill_switch": bool(self.config["kill_switch"]),
                "auto_kill": bool(self.config["auto_kill"]),
                "killswitch_rearm_at": self.config.get("killswitch_rearm_at", ""),
                "sim_speed": round(float(self.config["sim_speed"]), 1),
                "edit_window_seconds": round(float(self.config["edit_window_seconds"]), 1),
                "rejected_revive_minutes": round(float(
                    self.config.get("rejected_revive_minutes") or 10), 1),
                "entry_guard_enabled": bool(self.config.get("entry_guard_enabled", True)),
                "entry_guard_tolerance": float(self.config.get("entry_guard_tolerance", 0.5) or 0.0),
                "entry_guard_max_zone_widths": float(self.config.get("entry_guard_max_zone_widths", 5.0)),
                "entry_guard_auto_cap": float(self.config.get("entry_guard_auto_cap") or 0.0),
                "autonomous_mode": bool(self.config.get("autonomous_mode", True)),
                "pending_expire_minutes": float(self.config.get("pending_expire_minutes") or 30),
                "entry_style": self._entry_style(),
                "entry_order_ttl_hours": float(self.config.get("entry_order_ttl_hours") or 0.0),
                "summary_time": self.config["summary_time"],
                "strategy": self.config.get("strategy", "nova_gestao"),
                "strategy_params": self._strategy_params(),
                "lane2_strategy": self._lane_strategy_id("tp2"),
                "lane2_label": self.lane_labels.get("tp2") or self.config.get("lane2_label", ""),
                "lane2_balance": round(self._balance_for_lane("tp2"), 2) if "tp2" in self._lanes() else None,
                "lane2_enabled": "tp2" in self._lanes(),
                "effective_risk_by_lane": {
                    lane: round(self._effective_risk_pct_for_lane(lane), 2)
                    for lane in self._lanes()
                },
                "shadow_paper_enabled": bool(self.config.get("shadow_paper_enabled")),
                "shadow_paper_strategies": list(self.config.get("shadow_paper_strategies") or []),
                "execution_journal_enabled": bool(self.config.get("execution_journal_enabled")),
                "risk_preview": self.risk_preview(),
                "risk_preview_by_lane": {
                    lane: self.risk_preview(lane) for lane in self._lanes()
                },
                "teste_days": int(self.config.get("teste_days", 30)),
                "paper_race_hours": int(self.config.get("paper_race_hours", 24)),
                "fase3_target": int(self.config.get("fase3_target", 20)),
            },
            # Blocos informativos caros (agregações DB sobre tabelas grandes
            # ou leituras de conta) mas de mudança lenta — cache TTL para o
            # snapshot do tick (~1×/s) não pagar ~20 queries por segundo
            # (causa dos `snapshot.build` perto do limiar de stall).
            "fase3_snapshot": self._ttl_get("fase3_snapshot", 60.0, self._fase3_snapshot),
            "fase3_eta": self._ttl_get("fase3_eta", 60.0, self._fase3_eta),
            "fase3_verdict": self._ttl_get("fase3_verdict", 60.0, self._fase3_verdict),
            "fase3_verdicts": self._ttl_get("fase3_verdicts", 60.0, self._fase3_verdicts),
            "fase3_target_log": self._ttl_get("fase3_target_log", 60.0, self._fase3_target_log),
            "window": self._ttl_get("window_status", 10.0, self.window_status),
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
            "execution": self._execution_state(lanes=lanes),
            "group_audits": self.db.list_group_audits(50),
            "operations": {
                "disk": dict(self._disk_status),
                "execution_journal": self._journal_stats(),
            },
            "shadow_paper": self._ttl_get("shadow_paper_status", 10.0,
                                           self.shadow_paper_status),
            "mt5_positions": self._mt5_positions(),
            "ignored_count": self.db.count_ignored(),
            "paper": self._ttl_get("paper_status", 10.0, self.paper_status),
            "regime": dict(self._regime_status),
            "edit_metric": dict(self._edit_metric_status),
            "listener": self.listener_status(),
            "server": {"started_at": self.started_at},
            "signals": [s.to_dict() for s in self.db.list_signals(50)],
            "trades": [self._trade_view(t) for t in self.db.list_trades(50)],
            "positions": [p.to_dict() for p in self.db.open_positions()],
            "notifications": [
                {"id": n.id, "kind": n.kind, "text": n.text,
                 "created_at": n.created_at, "lane": n.lane or ""}
                for n in self.db.list_notifications(30)
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
        # o gate forward avalia a auto_escada_3 na origem SHADOW (sinais
        # live completos abrem shadow mesmo com o kill switch ligado); a
        # corrida race só abre quando a principal executa e fica vazia com
        # o kill ON — o ETA tem de medir a MESMA origem do gate
        auto = self.db.paper_trade_stats(sid, origin="shadow")
        n = int(auto.get("closed") or 0)
        target = int(self.config.get("fase3_target") or 20)
        now = datetime.now(timezone.utc)
        if n >= target:
            return {"n": n, "target": target, "done": True,
                    "pace": None, "pace_prev": None, "pace_change_pct": None,
                    "eta": None, "eta_days": None, "pace_daily": []}
        opened = self.db.paper_trades_opened_since(
            sid, (now - timedelta(days=30)).isoformat(timespec="seconds"),
            origin="shadow")
        opened_prev = (self.db.paper_trades_opened_since(
            sid, (now - timedelta(days=60)).isoformat(timespec="seconds"),
            origin="shadow") - opened)
        pace = opened / 30.0
        pace_prev = opened_prev / 30.0
        change = None
        if pace_prev > 0:
            change = round((pace - pace_prev) / pace_prev * 100.0, 0)
        # contagem por dia dos últimos 60 dias (série zero-preenchida) — a
        # curva mostra a auto E a referência (nova_gestao) lado a lado para
        # comparar a cadência das duas contas
        by_day = self.db.paper_trades_opened_by_day(
            sid, (now - timedelta(days=59)).strftime("%Y-%m-%d"),
            origin="shadow")
        by_day_ref = self.db.paper_trades_opened_by_day(
            "nova_gestao", (now - timedelta(days=59)).strftime("%Y-%m-%d"),
            origin="shadow")
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

    @staticmethod
    def _auto_params(strat: dict) -> Optional[dict]:
        """Build the same auto-policy payload for every execution engine.

        `be_buffer_pips` conta sozinho (15/09): o BE+ autónomo precisa dele
        mesmo quando a estratégia não define NENHUM outro parâmetro auto —
        sem isto, o tp2_protect nunca recebia o buffer e o BE só acontecia
        quando o grupo dizia 'TP1 Hit' (2 min de exposição no sinal das
        15:37)."""
        keys = ("stair_levels", "stair_from", "confirm_filter", "trail_factor",
                "time_stop_minutes", "atr_k", "atr_period", "atr_timeframe",
                "def_window", "def_trail", "def_time_stop", "def_risk_pct",
                "protect_trigger", "protect_buffer", "be_buffer_pips")
        if not any(strat.get(key) for key in keys):
            return None
        return {key: strat.get(key) for key in keys}

    # BE+ adaptativo: cache {symbol: (avg_pips | None, monotonic_ts)} — a
    # média do spread move-se devagar e o tick corre ~1×/s por lane.
    _BE_BUFFER_WINDOW_DAYS = 7
    _BE_BUFFER_CACHE_SECONDS = 300.0
    _BE_BUFFER_MIN_PIPS = 10.0
    _BE_BUFFER_MAX_PIPS = 60.0
    _BE_BUFFER_SPREAD_FACTOR = 1.1

    def _adaptive_be_buffer(self, symbol: str, strat: dict) -> float:
        """Buffer BE+ do símbolo: spread médio observado (7d) × 1.1, limitado
        a [10, 60] pips. Fallback = valor estático da estratégia sem amostras
        (`spread_observations` vazia — BD fresca/símbolo nunca observado).
        Config `be_buffer_adaptive=0` desliga e fixa o valor estático."""
        static = float(strat.get("be_buffer_pips") or 0.0)
        if not symbol:
            return static
        import time as _time
        cache = getattr(self, "_be_buffer_cache", None)
        if cache is None:
            cache = self._be_buffer_cache = {}
        now = _time.monotonic()
        cached = cache.get(symbol)
        if cached is not None and now - cached[1] < self._BE_BUFFER_CACHE_SECONDS:
            avg = cached[0]
        else:
            try:
                avg = self.db.avg_spread_pips(
                    symbol, days=self._BE_BUFFER_WINDOW_DAYS)
            except Exception:
                avg = None
            cache[symbol] = (avg, now)
        if not self.config.get("be_buffer_adaptive", True):
            return static
        if avg is None or avg <= 0:
            return static
        return round(min(self._BE_BUFFER_MAX_PIPS,
                         max(self._BE_BUFFER_MIN_PIPS,
                             avg * self._BE_BUFFER_SPREAD_FACTOR)), 1)

    def _lane_open_symbols(self, lane_id: str) -> set:
        """Símbolos das trades abertas da lane (para o buffer BE+ adaptativo)."""
        try:
            return {t.symbol for t in self.db.open_trades()
                    if (getattr(t, "lane_id", None) or "main") == lane_id}
        except Exception:
            return set()

    @classmethod
    def _lane_auto_params(cls, strat: dict) -> Optional[dict]:
        """Payload base por lane (mesma política de `_auto_params`; o buffer
        BE+ adaptativo é resolvido à parte, em `_adaptive_be_buffer`)."""
        return cls._auto_params(strat)

    def _strategy_params(self) -> dict:
        """Parâmetros da estratégia ativa da conta principal (max_legs,
        exit_at_tp, lots_override, manage) — a dashboard mostra-os no badge
        para se saber exatamente como a execução real está a correr.

        `orphan` = a config aponta para um id que já não existe no registo
        (ex.: estratégia removida) — a execução cai no fallback e o badge
        avisa em vez de mostrar silenciosamente a estratégia errada."""
        from app.strategies import (MAIN_STRATEGY_ID, STRATEGIES,
                                    get_strategy)
        sid = self.config.get("strategy")
        # id removido/morto na config: o badge avisa (orphan) e o snapshot
        # usa os parâmetros da principal — a execução/tick nunca rebenta com
        # uma config antiga; os ESTUDOS é que falham em voz alta (get_strategy
        # levanta ValueError fora daqui).
        try:
            s = get_strategy(sid)
        except ValueError:
            s = STRATEGIES[MAIN_STRATEGY_ID]
        defensive = False
        if s.get("def_window") and not getattr(self, "sim", None) is None:
            try:
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
            "def_risk_pct": s.get("def_risk_pct"),
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
                                    STRATEGIES, paper_strategy_ids)
        main = self.config.get("strategy")
        skipped = getattr(self.paper, "_skipped_daily_stop", {}) or {}
        # corrida = só o que abre papel E é visível (a `nova` contrafactual
        # abre papel silenciosamente mas não entra nos cards da corrida)
        paper_ids = set(paper_strategy_ids())
        out = []
        for sid, strat in STRATEGIES.items():
            if (sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES
                    or sid not in paper_ids):
                continue
            bal = self.db.ensure_paper_account(sid, START_BALANCE)
            stats = self.db.paper_trade_stats(sid)
            out.append({
                "strategy_id": sid,
                "nome": strat["nome"],
                "desc": strat["desc"],
                "is_main": sid == main,
                "balance": bal,
                "daily_stop_skipped": skipped.get(
                    f"race:{sid}", skipped.get(sid, 0)
                ),
                **stats,
            })
        return {"accounts": out}

    def paper_divergences(self) -> list:
        """Pares auto (em validação) vs referência `nova_gestao` por sinal com
        trade de papel — razão e P&L de cada lado, para a tabela da tab
        Relatório (ligação backtest↔vivo, SPEC v2 §3.4). Só devolve os pares
        que divergiram (a referência saiu e a auto continua, ou saídas/P&L
        diferentes no mesmo sinal)."""
        from app.strategies import STRATEGIES, paper_strategy_ids
        paper = set(paper_strategy_ids())
        autos = [sid for sid in STRATEGIES
                 if sid.startswith("auto_") and sid in paper]
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

    def lane_divergences(self) -> list:
        """Per-signal divergence between execution lanes (main vs tp2).

        For each signal that has trades in BOTH lanes, compare exit_reason
        and pnl.  Only returns signals that diverge (different reason or
        pnl delta > 0.01)."""
        from collections import defaultdict
        all_trades = self.db.list_trades(5000)
        by_signal_lane: dict = defaultdict(dict)
        for t in all_trades:
            if t.status != "closed":
                continue
            lane = t.lane_id or "main"
            by_signal_lane[t.signal_id][lane] = t
        out = []
        for sid, lanes in by_signal_lane.items():
            main_t = lanes.get("main")
            tp2_t = lanes.get("tp2")
            if not main_t or not tp2_t:
                continue
            same_reason = main_t.exit_reason == tp2_t.exit_reason
            same_pnl = abs((main_t.pnl or 0.0) - (tp2_t.pnl or 0.0)) < 0.01
            if same_reason and same_pnl:
                continue
            out.append({
                "signal_id": sid,
                "symbol": main_t.symbol,
                "direction": main_t.direction,
                "entry": main_t.entry,
                "main_strategy": main_t.strategy_id,
                "tp2_strategy": tp2_t.strategy_id,
                "main_pnl": round(float(main_t.pnl or 0.0), 2),
                "tp2_pnl": round(float(tp2_t.pnl or 0.0), 2),
                "pnl_delta": round(float((tp2_t.pnl or 0.0) - (main_t.pnl or 0.0)), 2),
                "main_reason": main_t.exit_reason,
                "tp2_reason": tp2_t.exit_reason,
                "opened_at": main_t.opened_at,
                "closed_at": main_t.closed_at,
            })
        out.sort(key=lambda r: r["closed_at"] or "", reverse=True)
        return out

    def lane_status(self) -> list:
        """Per-lane summary: strategy, mode, connected, total pnl, trade count."""
        out = []
        for lane_id in self._lanes():
            strat = self._lane_strategy(lane_id)
            engine = self._lane_engine(lane_id)
            acct = self._mt5_account(lane_id)
            if acct is None and lane_id == "tp2":
                acct = self._lane2_synthetic_account()
            dry = bool(getattr(engine, "dry_run", False))
            has_provider = getattr(engine, "provider", None) is not None
            connected = bool(getattr(getattr(engine, "provider", None), "connected", False))
            all_trades = self.db.list_trades(5000)
            lane_trades = [t for t in all_trades
                           if (t.lane_id or "main") == lane_id]
            closed = [t for t in lane_trades if t.status == "closed"]
            total_pnl = round(sum(float(t.pnl or 0.0) for t in closed), 2)
            wins = len([t for t in closed if (t.pnl or 0.0) > 0])
            losses = len([t for t in closed if (t.pnl or 0.0) < 0])
            out.append({
                "lane_id": lane_id,
                "strategy_id": strat["id"],
                "strategy_name": strat["nome"],
                "mode": ("mt5-blocked" if getattr(getattr(engine, "provider", None), "execution_block_reason", "")
                         else "mt5-dry" if dry else "mt5-real" if has_provider else "sim"),
                "connected": connected,
                "account": acct,
                "effective_risk_pct": round(self._effective_risk_pct_for_lane(lane_id), 2),
                "risk_base_pct": round(self._base_risk_for_lane(lane_id), 2),
                "fixed_lots_per_leg": self._fixed_lots_for_lane(lane_id),
                "risk_preview": self.risk_preview(lane_id),
                "trades_total": len(lane_trades),
                "trades_closed": len(closed),
                "trades_open": len(lane_trades) - len(closed),
                "total_pnl": total_pnl,
                "wins": wins,
                "losses": losses,
            })
        return out

    # --------------------------------------------------------- execution

    def _execution_state(self, lanes: Optional[list] = None) -> dict:
        """How trades are executed: sim / mt5-dry / mt5-real, + MT5 account.

        Kept cheap: the provider caches account_info for a few seconds, so
        this runs on every snapshot without hammering the terminal.  A
        non-hedged real account is represented explicitly as blocked instead
        of looking like a generic connection failure.

        `lanes` (opcional) é o snapshot de lanes JÁ construído pelo
        `_build_snapshot` — reconstruí-lo aqui duplicava ~8 leituras
        MT5/worker por lane por tick (o snapshot chama as lanes primeiro e
        o estado de execução depois).
        """
        if lanes is None:
            lanes = self._lanes_snapshot()
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
                "broker_account": account,
                "simulation": self._dry_run_summary(),
                "lanes": lanes,
            }
        return {"mode": "sim", "connected": False, "trade_allowed": True,
                "account": None, "margin_mode": None, "hedged": None,
                "blocked_reason": "", "broker_account": None,
                "simulation": {"enabled": True,
                                "starting_balance": float(self.config.get("balance", 0.0) or 0.0),
                                "realized_pnl": self.db.realized_today(date.today().isoformat()),
                                "equity": self.equity(),
                                "currency": self._currency()},
                "lanes": lanes}

    # ------------------------------------------------------------ pipeline

    async def _apply_pending_shadow_management(self, signal: Signal) -> int:
        """Deliver queued group instructions to a shadow trade.

        The pending row has separate real/paper markers: shadow paper can
        receive a management message before the real account is approved,
        without consuming the instruction that the real trade may still need.
        """
        key = str(signal.message_id) if signal.message_id else signal.id
        pending = self.db.pending_management_for(key, engine="shadow")
        applied = 0
        for rec in pending:
            self.paper.apply_management(
                signal.id, rec["kind"], tp=rec.get("tp"), sl=rec.get("sl"),
                be=bool(rec.get("be")), msg_id=rec.get("id"),
                origin="shadow",
            )
            self.db.mark_pending_management_applied(rec["id"], engine="shadow")
            applied += 1
        return applied

    async def _open_shadow_for_signal(self, signal: Signal) -> List[Trade]:
        """Open configured shadow strategies without touching the real engine.

        This hook deliberately runs before risk/kill-switch decisions for the
        principal. It only accepts live, fully parsed signals; historical
        replay and LLM proposals never create forward paper evidence.
        """
        if (signal.error or signal.llm_parsed or signal.status == "historical"
                or not self.config.get("shadow_paper_enabled")):
            return []
        strategy_ids = list(self.config.get("shadow_paper_strategies") or [])
        if not strategy_ids:
            return []
        try:
            # O shadow espelha a decisão da principal: só abre pendente na
            # zona quando a guarda o justifica (desvio dentro do cap); em
            # zona/tolerância automática/sem quote abre a mercado.
            guard_mp = None
            paper_style = "market"
            if self._entry_style() == "pending":
                try:
                    guard = self._entry_guard(signal, "main")
                    guard_mp = guard.get("market_price")
                    if (guard.get("status") == "zone_deviation"
                            or (guard.get("status") == "tolerated"
                                and not guard.get("auto_execute"))):
                        paper_style = "pending"
                except Exception:
                    pass
            opened = self.paper.open_for_signal(
                signal, self._effective_risk_pct(), strategy_ids=strategy_ids,
                daily_stop_pct=float(self.config.get("daily_loss_limit_pct") or 0),
                origin="shadow",
                entry_style=paper_style,
                market_price=guard_mp,
            )
            queued = await self._apply_pending_shadow_management(signal)
            if opened:
                await self.notifier.send(
                    "system",
                    f"👻 Shadow-paper: {len(opened)} estratégia(s) receberam "
                    f"{signal.symbol} sem abrir ordens MT5 "
                    f"({', '.join(strategy_ids)})",
                )
            if queued:
                await self.notifier.send(
                    "system",
                    f"👻 Shadow-paper: aplicadas {queued} instruções pendentes "
                    f"a {signal.symbol}",
                )
            return opened
        except Exception as exc:
            self.db.record_exception(
                exc, context={"component": "paper", "phase": "shadow_open"},
                signal_id=signal.id,
            )
            print(f"[shadow-paper] falha ao abrir trade: {exc}")
            return []

    def _invalidate_snapshot_cache(self) -> None:
        """Marca o snapshot cacheado como obsoleto.

        O market_loop reconstrói o snapshot ~1×/s; quando um handler MUTA o
        estado (config, sinal, kill switch...), os clients que leem
        `/api/status` logo a seguir têm de ver a mutação — nunca uma cópia
        anterior ao write. A invalidação é barata: o próximo read reconstrói
        uma vez e o tick volta a encher a cache."""
        self._last_snapshot = None
        # As caches TTL dos blocos informativos pertencem ao estado antigo
        # (mesma BD/config) — invalida tudo: o próximo snapshot recomputa.
        self._ttl_cache = {}

    @property
    def stall_recorder(self):
        """Gravador de stalls/fases lentas do event loop (lazy — só cria
        quando uma fase é medida; ver app/stallwatch.py)."""
        if self._stall_recorder_obj is None:
            from app.stallwatch import LoopStallRecorder
            self._stall_recorder_obj = LoopStallRecorder(self.db)
        return self._stall_recorder_obj

    # ------------------------------------------------------------ analytics

    def report(self, days: int = 7, lane: str = "") -> dict:
        """Relatório de qualidade: win rate, risco/recompensa, por grupo.

        `lane` (main|tp2) filtra as trades fechadas da conta — os sinais do
        grupo são partilhados pelas duas lanes e ficam globais; o P&L,
        win rate e drawdown referem-se à lane (o drawdown usa o saldo da
        lane, não o da principal)."""
        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        signals = self.db.signals_since(since)
        all_trades = self.db.trades_closed_since(since)
        trades = all_trades
        if lane:
            trades = [t for t in all_trades
                      if (getattr(t, "lane_id", None) or "main") == lane]

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
            "period": {"days": days, "since": since, "today": date.today().isoformat(),
                       "lane": lane or "all"},
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
                "by_lane": {
                    ln: round(sum(float(t.pnl or 0.0) for t in all_trades
                                  if (getattr(t, "lane_id", None) or "main") == ln), 2)
                    for ln in ("main", "tp2")
                } if not lane else None,
            },
            "risk_reward": {
                f"avg_tp{i}": round(sum(v) / len(v), 2) if v else 0.0
                for i, v in rr.items()
            },
            "equity_curve": self._equity_curve(trades),
            "drawdown": self._max_drawdown(
                trades, self._balance_for_lane(lane) if lane else self._balance()),
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

    async def resolution_analysis(self, group: str = "", days: int = 7,
                                  strategy_id: Optional[str] = None,
                                  resolution: str = "1m",
                                  risk_pct: Optional[float] = None,
                                  sim_balance: Optional[float] = None,
                                  delay: int = 60,
                                  include_delays: bool = True) -> dict:
        """Compare native/fallback candle resolution and simulated latency."""
        from app.analysis import run_resolution_analysis
        return await run_resolution_analysis(
            self.db, group=group, days=days,
            strategy_id=strategy_id or self.config.get("strategy", "nova_gestao"),
            resolution=resolution, risk_pct=risk_pct,
            balance=sim_balance or self._balance(), delay=delay,
            include_delays=include_delays,
        )

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

    async def trace_signal(self, strategy_id: str = "nova", group: str = "",
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
        # Item 12: o modo manual foi removido — só existe o caminho auto.
        # Um POST com outro modo é recusado em vez de ignorado, para nenhum
        # operador acreditar que pausou o bot quando não pausou.
        if body.mode is not None and body.mode != "auto":
            return {"error": f"modo desconhecido: {body.mode} — só existe o "
                             f"caminho auto (modo manual removido no Item 12)"}
        if body.risk_pct is not None:
            # legado/principal: sincroniza `risk_pct` com `risk_pct:main` para
            # a base da lane main nunca divergir do valor histórico
            self.config["risk_pct"] = float(body.risk_pct)
            self.config["risk_pct:main"] = float(body.risk_pct)
        if body.risk_pct_main is not None:
            self.config["risk_pct:main"] = float(body.risk_pct_main)
            self.config["risk_pct"] = float(body.risk_pct_main)
        if body.risk_pct_tp2 is not None:
            self.config["risk_pct:tp2"] = float(body.risk_pct_tp2)
        if body.lots_override_tp2 is not None:
            # lote fixo por perna da 2ª conta (0 = volta ao dimensionamento
            # por risco; >0 = cada perna abre SEMPRE nesse lote)
            self.config["lots_override:tp2"] = float(body.lots_override_tp2)
        if body.daily_loss_limit_pct is not None:
            self.config["daily_loss_limit_pct"] = body.daily_loss_limit_pct
        if body.max_positions is not None:
            self.config["max_positions"] = body.max_positions
        if body.sim_speed is not None:
            self.config["sim_speed"] = body.sim_speed
        if body.edit_window_seconds is not None:
            self.config["edit_window_seconds"] = body.edit_window_seconds
        if body.entry_guard_enabled is not None:
            self.config["entry_guard_enabled"] = bool(body.entry_guard_enabled)
        if body.entry_guard_tolerance is not None:
            self.config["entry_guard_tolerance"] = float(body.entry_guard_tolerance)
        if body.entry_guard_max_zone_widths is not None:
            self.config["entry_guard_max_zone_widths"] = float(body.entry_guard_max_zone_widths)
        if body.entry_guard_auto_cap is not None:
            new_cap = float(body.entry_guard_auto_cap)
            # marca o momento EXATO em que o cap mudou — o study_cap_effect
            # usa-o como --since para partir os fills antes/depois do cap
            if abs(new_cap - float(self.config.get("entry_guard_auto_cap") or 0.0)) > 1e-9:
                self.config["cap_applied_at"] = _now_iso()
            self.config["entry_guard_auto_cap"] = new_cap
        if body.autonomous_mode is not None:
            self.config["autonomous_mode"] = bool(body.autonomous_mode)
        if body.pending_expire_minutes is not None:
            self.config["pending_expire_minutes"] = float(body.pending_expire_minutes)
        if body.entry_style is not None:
            style = str(body.entry_style).strip().lower()
            if style not in ("market", "pending"):
                return {"error": f"entry_style desconhecido: {body.entry_style} "
                                 f"(market | pending)"}
            self.config["entry_style"] = style
        if body.entry_order_ttl_hours is not None:
            self.config["entry_order_ttl_hours"] = float(body.entry_order_ttl_hours)
        if body.group_feed_divergence_threshold is not None:
            self.config["group_feed_divergence_threshold"] = float(body.group_feed_divergence_threshold)
        if body.lane2_balance is not None:
            self.config["lane2_balance"] = float(body.lane2_balance)
        if body.lane2_strategy is not None:
            from app.strategies import STRATEGIES
            if body.lane2_strategy not in STRATEGIES:
                return {"error": f"estratégia desconhecida para lane2: {body.lane2_strategy}"}
            if body.lane2_strategy in {"auto_escada_3", "auto_escada_5"}:
                return {"error": "auto-escadas não podem ser usadas na lane MT5 sem promoção/gate"}
            self.config["lane2_strategy"] = body.lane2_strategy
            self.lane_strategies["tp2"] = body.lane2_strategy
        if body.lane2_label is not None:
            self.config["lane2_label"] = str(body.lane2_label).strip()[:120]
            self.lane_labels["tp2"] = self.config["lane2_label"]
        strategy_changed = False
        if body.strategy is not None:
            from app.strategies import STRATEGIES
            if body.strategy not in STRATEGIES:
                return {"error": f"estratégia desconhecida: {body.strategy}"}
            strategy_changed = body.strategy != self.config.get("strategy")
            # Auto-escadas só podem ser promovidas depois de todos os gates
            # objetivos da janela forward shadow. O bloqueio é também no
            # backend (não apenas visual), para que um POST manual não contorne
            # a confirmação da dashboard. A conta principal, kill switch e
            # MT5 não são tocados quando o gate falha.
            if (strategy_changed and body.strategy in {"auto_escada_3", "auto_escada_5"}
                    and self.db.all_config().get("forward_evidence_started_at")):
                from app.execution.evidence import forward_gate_report
                gate_report = forward_gate_report(self.db, days=30)
                candidate = next(
                    (row for row in gate_report.get("strategies", [])
                     if row.get("strategy_id") == body.strategy),
                    None,
                )
                if not candidate or not candidate.get("eligible"):
                    failed = [key for key, value in (candidate or {}).get("gates", {}).items()
                              if not value]
                    return {
                        "ok": False,
                        "error": (
                            f"promoção bloqueada: {body.strategy} ainda não passou "
                            "todos os gates da janela forward"
                        ),
                        "promotion_blocked": True,
                        "strategy": body.strategy,
                        "failed_gates": failed,
                        "forward_gate": gate_report,
                    }
            self.config["strategy"] = body.strategy
        # Validate the two shadow fields before mutating either one. Without
        # this staging, `{enabled: false, strategies: [invalid]}` could leave
        # the in-memory runtime disabled even though the API returned an
        # error and nothing was persisted.
        shadow_enabled = bool(self.config.get("shadow_paper_enabled"))
        if body.shadow_paper_enabled is not None:
            shadow_enabled = bool(body.shadow_paper_enabled)
        shadow_strategies = None
        if body.shadow_paper_strategies is not None:
            from app.strategies import HIDDEN_STRATEGIES, STRATEGIES
            requested = list(dict.fromkeys(
                str(s).strip() for s in body.shadow_paper_strategies
                if str(s).strip()
            ))
            if not requested:
                # An empty selection is valid while the feature is disabled;
                # this lets an API caller clear the list atomically with
                # `{shadow_paper_enabled: false}`. An enabled stream must
                # always have at least one validated strategy.
                if shadow_enabled:
                    return {"error": "indica pelo menos uma estratégia shadow-paper "
                                    "ou desativa shadow_paper_enabled"}
                shadow_strategies = []
            else:
                invalid = [sid for sid in requested
                           if sid not in STRATEGIES or sid in HIDDEN_STRATEGIES]
                if invalid:
                    return {"error": "estratégias shadow-paper inválidas: "
                                    + ", ".join(invalid)}
                shadow_strategies = requested
        if body.shadow_paper_enabled is not None:
            self.config["shadow_paper_enabled"] = shadow_enabled
        if shadow_strategies is not None:
            self.config["shadow_paper_strategies"] = shadow_strategies
        if body.execution_journal_enabled is not None:
            self.config["execution_journal_enabled"] = bool(body.execution_journal_enabled)
        if body.killswitch_rearm_at is not None:
            raw_rearm = str(body.killswitch_rearm_at or "").strip()
            if raw_rearm:
                parsed_rearm = self._parse_utc(raw_rearm)
                if parsed_rearm is None:
                    return {"error": "killswitch_rearm_at deve ser um timestamp ISO UTC válido"}
                self.config["killswitch_rearm_at"] = parsed_rearm.isoformat(timespec="seconds")
            else:
                self.config["killswitch_rearm_at"] = ""
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
        self._persist_config(["risk_pct", "risk_pct:main", "risk_pct:tp2",
                              "lots_override:main", "lots_override:tp2",
                              "daily_loss_limit_pct",
                              "max_positions", "sim_speed", "edit_window_seconds",
                              "entry_guard_enabled", "entry_guard_tolerance", "entry_guard_max_zone_widths",
                              "entry_guard_auto_cap", "cap_applied_at",
                              "cap_reminder_days", "cap_reminder_lookback_days", "cap_reminder_last_sent",
                              "autonomous_mode", "pending_expire_minutes",
                              "entry_style", "entry_order_ttl_hours",
                              "group_feed_divergence_threshold", "lane2_balance", "lane2_strategy", "lane2_label", "strategy", "shadow_paper_enabled",
                              "shadow_paper_strategies", "execution_journal_enabled",
                              "killswitch_rearm_at", "teste_days",
                              "paper_race_hours", "fase3_target"])
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
            shadow = "ON" if self.config.get("shadow_paper_enabled") else "OFF"
            await self.notifier.send(
                "system",
                f"⚙️ Config atualizada: modo=auto, "
                f"risco={self.config['risk_pct']}% · shadow-paper={shadow}",
            )
        # Consistência risco ↔ limite diário: com risco alto por trade e
        # limite diário baixo, uma única perda trava o dia — aviso informativo.
        warning = None
        if body.risk_pct is not None:
            rp = float(self.config["risk_pct"])
            dl = float(self.config.get("daily_loss_limit_pct") or 0.0)
            if dl > 0 and dl < 1.5 * rp:
                warning = (f"limite diário ({dl:.1f}%) < 1.5× o risco por trade "
                           f"({rp:.1f}%) — uma perda pode travar o dia")
        result = {"ok": True, "config": self.snapshot()["config"]}
        if warning:
            result["warning"] = warning
        return result

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
        if enabled:
            pass  # rearm only on explicit user request
        else:
            self.config["auto_kill"] = False
            self.config["killswitch_rearm_at"] = ""
        self._persist_config(["kill_switch", "auto_kill", "killswitch_rearm_at"])
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
        # Keep the isolated forward curve available for an audit without
        # putting it in the normal race chart. Do not create accounts before a
        # configured shadow strategy has received its first live signal.
        if self.config.get("shadow_paper_enabled"):
            for sid in self.config.get("shadow_paper_strategies") or []:
                bal = self.db.paper_balance(sid, origin="shadow")
                if bal is None:
                    continue
                try:
                    self.db.insert_paper_balance_history(
                        sid, ts, bal, origin="shadow"
                    )
                except Exception as exc:
                    print(f"[balance-snap] shadow:{sid}: {exc}")

    async def tick(self) -> None:
        await self.finalize_awaiting_signals()
        speed = float(self.config["sim_speed"])
        await self.market.refresh(speed)
        if self.config.get("execution_journal_enabled"):
            import time as _time
            monotonic_now = _time.monotonic()
            poll_interval = (
                None if self._last_journal_monotonic is None
                else round(max(0.0, monotonic_now - self._last_journal_monotonic), 4)
            )
            self._last_journal_monotonic = monotonic_now
            for symbol, price in self.market.prices.items():
                self.db.record_execution_event(
                    _now_iso(), "quote", engine="runtime", symbol=symbol,
                    payload={"price": price,
                             "poll_interval_seconds": poll_interval},
                )
        else:
            self._last_journal_monotonic = None
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
        await self._check_post_execution_edits()
        await self._check_null_retcode_phantoms()

        # Cada lane de execução atualiza as suas trades com a estratégia e o
        # motor próprios — a lógica SL/TP/BE/trailing corre por lane.
        # (Fase 3: a auto-gestão só é passada quando a estratégia da lane a
        # define; `nova_gestao`/`tp2_protect` não têm — nada muda até uma
        # auto_* ser promovida.)
        all_events: List[dict] = []
        for lane_id in self._lanes():
            engine = self._lane_engine(lane_id)
            strat = self._lane_strategy(lane_id)
            auto = self._lane_auto_params(strat)
            if auto is not None and auto.get("be_buffer_pips"):
                auto = dict(auto)
                # BE+ adaptativo: o buffer segue o spread médio observado do
                # símbolo (janela 7d, cache 5 min) em vez do valor estático.
                buffer = 0.0
                for symbol in sorted(self._lane_open_symbols(lane_id)) or [""]:
                    buffer = max(buffer, self._adaptive_be_buffer(symbol, strat))
                auto["be_buffer_pips"] = buffer or strat.get("be_buffer_pips")
            import time as _tm
            _t0 = _tm.monotonic()
            try:
                events = engine.update(
                    exit_at_tp=strat["exit_at_tp"], auto=auto,
                    entry_order_ttl=self._entry_order_ttl_seconds(),
                )
            except Exception as exc:
                self.db.record_exception(
                    exc, context={"component": "execution", "phase": "update",
                                  "lane": lane_id},
                )
                print(f"[lane:{lane_id}] update falhou: {exc}")
                events = []
            # Fase on-loop que mais vezes congela o loop (I/O MT5/worker) —
            # gravada quando ≥ limiar para o Diagnóstico atribuir stalls.
            self.stall_recorder.record_phase(
                f"tick.update:{lane_id}", _tm.monotonic() - _t0)
            for ev in events:
                ev["_lane"] = lane_id
                all_events.append(ev)
        # paper trading forward: atualiza as posições de papel das estratégias
        # de teste contra os preços atuais (mesma lógica SL/TP/BE)
        try:
            paper_events = self.paper.tick(
                float(self.config["risk_pct"]),
                entry_order_ttl=self._entry_order_ttl_seconds(),
            )
            for ev in paper_events:
                if ev["type"] == "trade_closed":
                    # feed focado na corrida VISÍVEL: a `nova` (contrafactual
                    # silencioso) NÃO notifica — só as estratégias com página
                    from app.strategies import strategy_lifecycle
                    if not strategy_lifecycle(
                            ev.get("strategy_id") or "")["menu"]:
                        continue
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
        # perseguições executadas neste tick: ordens pendentes na zona que
        # preencheram — agrega por trade (várias pernas preenchem no mesmo
        # tick) e notifica uma vez por trade no fim do loop. As trades
        # fechadas de TODAS as lanes agregam por sinal para o WhatsApp
        # enviar UMA mensagem dual (ambas as contas) por sinal por tick.
        pending_fills: Dict[str, dict] = {}
        closed_signals: Dict[str, dict] = {}  # signal_id -> último ev fechado
        for ev in all_events:
            lane_id = ev.pop("_lane", "main")
            lane_tag = f"[{lane_id}] " if lane_id != "main" else ""
            if ev["type"] == "close":
                # MT5: the broker already applied the P&L to the account — the
                # config balance is only the paper (sim) source of truth.
                if lane_id == "main" and self._mt5_account("main") is None:
                    self.config["balance"] = round(self._balance() + ev["pnl"], 2)
                    self.db.set_config("balance", self.config["balance"])
                await self.notifier.send(
                    "trade_close",
                    f"{lane_tag}{'✅' if ev['pnl'] >= 0 else '❌'} Fechada perna {ev['leg']} "
                    f"{ev['symbol']} @ {ev['price']} | P&L {ev['pnl']:+.2f}",
                    lane=lane_id,
                )
            elif ev["type"] == "be":
                await self.notifier.send(
                    "system",
                    f"{lane_tag}🛡️ {ev['symbol']}: TP1 atingido — pernas restantes protegidas a breakeven",
                    lane=lane_id,
                )
            elif ev["type"] == "trade_closed":
                await self.notifier.send(
                    "trade_close",
                    f"{lane_tag}📦 Trade {ev['symbol']} fechada | P&L total {ev['pnl']:+.2f}",
                    lane=lane_id,
                )
                # dedup por sinal: se as duas lanes fecharem no MESMO tick, o
                # WhatsApp recebe uma mensagem única com as duas contas
                closed_signals[ev.get("signal_id") or ev.get("trade_id")] = (
                    ev, lane_id,)
            elif ev["type"] == "pending_expired":
                # Fase 1/4 — ordem pendente da zona que não chegou a preencher:
                # expirou por TTL, foi cancelada/expirada pelo broker ou é
                # ÓRFÃ (ticket que já não existe no broker / nunca foi
                # enviado). O tick já a fechou; notifica para o operador ver
                # que a zona não entrou e porquê — sem silêncio.
                reason = ev.get("reason") or "ttl"
                if reason == "orphan":
                    text = (f"⚠️ Ordem pendente órfã: perna {ev['leg']} "
                            f"{ev['symbol']} ({ev.get('trade_id', '')[-6:]}) — "
                            f"o ticket não existe no broker (expirada/cancelada "
                            f"externamente ou perdida num restart). Fechada na BD.")
                elif reason == "cancelled":
                    text = (f"🗑️ Ordem pendente cancelada pelo broker: perna "
                            f"{ev['leg']} {ev['symbol']} — não chegou a preencher.")
                else:
                    text = (f"⏳ Ordem pendente expirou por TTL: perna {ev['leg']} "
                            f"{ev['symbol']} — o preço não voltou ao nível da "
                            f"entrada a tempo. Fechada na BD.")
                await self.notifier.send("system", text, lane=lane_id)
            elif ev["type"] == "pending_delete_stuck":
                # watchdog Fase 1: o order_delete da ordem pendente falhou N
                # ticks seguidos (worker/terminal offline?) — a perna fica
                # `pending_fill` na BD (nunca órfã no MT5) e o tick repete,
                # mas deixa de ser silencioso
                await self.notifier.send(
                    "risk",
                    f"⛔ Ordem pendente não cancelada no MT5: perna "
                    f"{ev['leg']} {ev['symbol']} "
                    f"({ev.get('trade_id', '')[-6:]}, ticket "
                    f"{ev.get('ticket', '?')}) — {ev.get('attempts', '?')}ª "
                    f"tentativa falhada (worker/terminal offline?). A perna "
                    f"continua pendente na BD e o tick repete o cancelamento "
                    f"— verifica o worker da lane.",
                    lane=lane_id,
                )
            elif ev["type"] == "pending_fill":
                # ordem pendente na zona preencheu (Fase 1). Se o preço estava
                # longe da zona quando a ordem foi colocada, é uma PERSECUÇÃO
                # executada — a distância real vem do guard (spot MT5,
                # trade.entry_distance), não dos candles (GC=F diverge).
                pf = pending_fills.setdefault(ev["trade_id"], {
                    "trade_id": ev["trade_id"], "symbol": ev["symbol"],
                    "legs": 0, "max_distance": None, "entry": ev.get("price"),
                    "lanes": [],
                })
                pf["lanes"] = list(set(pf["lanes"] + [lane_id]))
                pf["legs"] += 1
                try:
                    t = self.db.get_trade(ev["trade_id"])
                    if t is not None:
                        if t.entry_distance is not None:
                            pf["max_distance"] = max(
                                pf["max_distance"] or 0.0, float(t.entry_distance))
                        pf["entry"] = pf.get("entry") or t.entry
                except Exception:
                    pass
            elif ev["type"] == "sl_move_applied":
                # retry de gestão de grupo: um SL que o broker tinha recusado
                # (10016) ficou válido num tick posterior e o modify passou —
                # o operador vê que a proteção pedida pelo grupo chegou, afinal
                await self.notifier.send(
                    "system",
                    f"🛡️ {lane_tag}SL do grupo aplicado em retry: perna "
                    f"{ev['leg']} {ev['symbol']} @ {ev['applied']} "
                    f"(alvo {ev['target']} tinha sido recusado — 10016).",
                    lane=lane_id,
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
                await self.notifier.send("risk", text, lane=lane_id)

        # WhatsApp: UMA mensagem por sinal fechado neste tick (com as duas
        # lanes lado a lado) — nunca uma por evento, para dois fechos no
        # mesmo tick não duplicarem no grupo.
        for ev, lane_id in closed_signals.values():
            await self._notify_whatsapp_trade_close(ev, lane_id=lane_id)

        # perseguições executadas: notifica as ordens pendentes que preencheram
        # com o preço fora da zona ALÉM da tolerância automática (o limite onde
        # a execução a mercado deixa de ser automática) — o operador vê as
        # entradas arriscadas em tempo real, com a distância real em pips.
        tolerance = float(self.config.get("entry_guard_tolerance", 0.5) or 0.0)
        for pf in pending_fills.values():
            dist = pf.get("max_distance")
            if dist is None or dist <= tolerance:
                continue
            lanes_txt = (f"[{','.join(pf['lanes'])}] ") if pf.get("lanes") else ""
            await self.notifier.send(
                "system",
                f"{lanes_txt}🏃 Perseguição executada: {pf['symbol']} {pf['legs']} perna(s) "
                f"@ {pf.get('entry')} — ordem pendente na zona preencheu com o "
                f"preço {dist:.2f} pips fora da zona (além da tolerância "
                f"automática {tolerance:.2f}). Entrada arriscada — acompanha.",
                lane=",".join(pf["lanes"]) if pf.get("lanes") else "",
            )

        await self._expire_stale_pending()
        await self._check_killswitch_rearm()
        await self._check_mt5_connection()
        await self._check_disk_space()
        await self._check_regime()
        await self._check_fase3_validation()
        await self._check_fase3_eta_alert()

        # auto kill switch: pause trading if today's loss limit is hit.
        # Cada lane MT5 é avaliada contra o SALDO DA PRÓPRIA CONTA: somar o
        # P&L realizado de todas as lanes contra o saldo da principal (pequena)
        # disparava o kill com a tp2 (10k) a perder 0.5% e a principal apenas
        # 0.1% (incidente 07/09: -53.47 € somados contra 1.033 € = -5.17%).
        # Uma lane sem saldo próprio (sim/dry-run) herda o comportamento
        # antigo: P&L total do dia contra o saldo configurado.
        if not self.config.get("auto_kill") and self._daily_loss_killed():
            self.config["kill_switch"] = True
            self.config["auto_kill"] = True
            self._persist_config(["kill_switch", "auto_kill", "killswitch_rearm_at"])
            await self.notifier.send(
                "risk",
                "🚨 Limite de perda diária atingido — trading pausado "
                "automaticamente. Reativa manualmente na dashboard.",
            )

        await self._check_recount_batch()
        await self._check_refusals_backfill()
        await self._check_cap_reminder()
        await self._check_be_buffer_drift()
        await self._check_orphan_report()
        await self._check_daily_summary()
        # Um único snapshot por tick: serve o WS e fica cacheado para os
        # endpoints read-only (`last_snapshot`) — a construção lê o broker
        # MT5/worker e não pode correr por request no event loop.
        import time as _tm
        _t0 = _tm.monotonic()
        snapshot = self._build_snapshot()
        self._last_snapshot = snapshot
        self.stall_recorder.record_phase(
            "snapshot.build", _tm.monotonic() - _t0)
        await self.ws.broadcast({"type": "snapshot", "data": snapshot})

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

    async def _check_null_retcode_phantoms(self) -> None:
        """Red-deundo do alerta de retcode null ("?") na lane 2.

        O alerta principal dispara no `_try_execute` quando uma trade abre e
        é recusada pelo broker SEM retcode — mas um fecho phantom também pode
        acontecer fora desse caminho (ex.: pernas pendentes recusadas no
        `engine.update()` do tick). Este scan percorre as trades fechadas da
        lane 2 dos últimos 30 min com assinatura phantom + retcode null e
        alerta com o estado do worker — uma vez por trade, nunca em loop.
        """
        import time as _time
        now = _time.time()
        if now - getattr(self, "_last_null_rc_check", 0.0) < 30.0:  # a cada ~30s
            return
        self._last_null_rc_check = now
        alerted = getattr(self, "_null_rc_alerted", set())
        try:
            rows = self.db.recent_phantom_null_retcodes(
                minutes=30, lane="tp2", limit=10,
            )
        except Exception:
            return
        for trade_id, rcs in rows:
            if trade_id in alerted:
                continue
            alerted.add(trade_id)
            try:
                await self.notifier.send(
                    "risk",
                    f"👻 Lane 2: trade {trade_id[-6:]} fechou phantom SEM "
                    f"retcode do broker (retcodes [{', '.join(rcs)}]) — "
                    f"worker/terminal sem resposta válida. "
                    f"{self._lane_worker_status_text('tp2')}",
                    lane="tp2",
                )
            except Exception:
                pass
        self._null_rc_alerted = alerted

    async def _check_post_execution_edits(self) -> None:
        """Métrica contínua do Item 12: edições a sinais que executaram sem
        janela (primeiro parse completo).

        Se a taxa de edições pós-execução subir acima de 5% com amostra
        mínima (20 sinais), notifica risk no feed/Telegram — a regra
        "completo → executa já" foi decidida com a taxa real de 0.0% e deve
        ser revista se o comportamento do grupo mudar. Cooldown de 24h;
        verifica a cada ~30 min no tick. O número cru fica em /api/edits.
        """
        import time as _time
        now = _time.time()
        if now - self._last_edit_metric_check < 1800:  # a cada ~30 min
            return
        self._last_edit_metric_check = now
        try:
            m = self.db.post_execution_edit_metric()
            self._edit_metric_status = dict(m)
            self._edit_metric_status["checked_at"] = _now_iso()
            total = int(m.get("post_execution_total") or 0)
            rate = float(m.get("post_execution_edit_rate") or 0.0)
            edited = int(m.get("post_execution_edited") or 0)
            if (total >= 20 and rate > 0.05
                    and now - self._edit_metric_alert_at > 86400):  # cooldown 24h
                self._edit_metric_alert_at = now
                await self.notifier.send(
                    "risk",
                    f"⚠️ Edições pós-execução em alta: {edited} de {total} sinais "
                    f"que executaram sem janela foram editados depois "
                    f"({rate * 100:.1f}% > 5%) — a regra \"completo → executa "
                    f"já\" (Item 12) pode precisar de ser revista; ver /api/edits.",
                    tg_text=f"⚠️ *Edições pós-execução em alta*: {edited} de "
                    f"{total} sinais executados sem janela foram editados depois "
                    f"({rate * 100:.1f}% > 5%). A regra 'completo → executa já' "
                    f"pode precisar de ser revista.",
                )
        except Exception as exc:
            print(f"[edit-metric] falha ao verificar: {exc}")

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

    # ------------------------------------------------------- window / promotion

    @staticmethod
    def _parse_utc(value: str) -> Optional[datetime]:
        """Parse an ISO timestamp and return an aware UTC datetime."""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _promotion_window_bounds(self) -> tuple[datetime, datetime]:
        start = self._parse_utc(self.config.get("promotion_window_start_at", ""))
        end = self._parse_utc(self.config.get("promotion_window_end_at", ""))
        start = start or datetime(2026, 8, 20, tzinfo=timezone.utc)
        end = end or datetime(2026, 8, 29, tzinfo=timezone.utc)
        if end <= start:
            end = start + timedelta(days=9)
        return start, end

    def _window_open_pnl(self, trade_ids: set[str]) -> float:
        total = 0.0
        for position in self.db.open_positions():
            if position.trade_id not in trade_ids:
                continue
            price = self.market.price(position.symbol)
            if price is None:
                continue
            sign = 1.0 if position.direction == "buy" else -1.0
            total += sign * (float(price) - float(position.entry)) * float(position.lots) * 100_000
        return round(total, 2)

    def _promotion_status(self) -> dict:
        """Join the objective forward gate and the live cadence for one card."""
        import time as _time
        now = _time.time()
        if (self._promotion_cache is not None
                and self._promotion_cache_db_id == id(self.db)
                and now - self._promotion_cache_at < 60):
            return self._promotion_cache
        from app.execution.evidence import forward_gate_report
        gate_report = forward_gate_report(self.db, days=30)
        candidate = next(
            (row for row in gate_report.get("strategies", [])
             if row.get("strategy_id") == "auto_escada_3"),
            None,
        ) or {}
        gates = candidate.get("gates") or {}
        labels = {
            "enough_trades": "N",
            "profit_factor": "PF",
            "positive_mean_ci95": "IC95",
            "drawdown": "DD",
            "reference_comparable": "referência",
            "not_more_than_10pct_behind_reference": "P&L vs referência",
        }
        missing = [labels.get(key, key) for key, passed in gates.items() if not passed]
        result = {
            "strategy_id": "auto_escada_3",
            "forward_gate": candidate,
            "forward_period": gate_report.get("period"),
            "eta": self._fase3_eta(),
            "missing": missing,
            "eligible": bool(candidate.get("eligible")),
            "promotion": "manual_only",
        }
        self._promotion_cache = result
        self._promotion_cache_at = now
        self._promotion_cache_db_id = id(self.db)
        return result

    def window_status(self, now: Optional[datetime] = None) -> dict:
        """Operational real-trade window ending at the end of 28 August UTC."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        start, end = self._promotion_window_bounds()
        real_trades = self.db.list_trades(200000)
        closed = [t for t in real_trades if t.status == "closed" and t.closed_at
                  and start.isoformat() <= t.closed_at < end.isoformat()]
        opened = [t for t in real_trades if t.status == "open" and t.opened_at
                  and start.isoformat() <= t.opened_at < end.isoformat()]
        closed_pnl = round(sum(float(t.pnl or 0.0) for t in closed), 2)
        open_ids = {t.id for t in opened}
        open_pnl = self._window_open_pnl(open_ids)
        total_seconds = max(1.0, (end - start).total_seconds())
        elapsed_seconds = min(total_seconds, max(0.0, (now - start).total_seconds()))
        elapsed_days = max(1.0 / 24.0, elapsed_seconds / 86400.0)
        remaining_seconds = max(0.0, (end - now).total_seconds())
        remaining_days = remaining_seconds / 86400.0
        closed_n = len(closed)
        pace = round(closed_n / elapsed_days, 2) if elapsed_seconds > 0 else 0.0
        required_10 = round(max(0, 10 - closed_n) / remaining_days, 2) if remaining_days else 0.0
        required_20 = round(max(0, 20 - closed_n) / remaining_days, 2) if remaining_days else 0.0
        projected = round(closed_n + pace * remaining_days, 1)
        if now >= end:
            status = "target_reached" if closed_n >= 20 else (
                "minimum_reached" if closed_n >= 10 else "expired")
        elif closed_n >= 20:
            status = "target_reached"
        elif closed_n >= 10:
            status = "minimum_reached"
        elif projected >= 10:
            status = "on_track"
        else:
            status = "below_minimum"
        return {
            "ok": True,
            "timezone": "UTC",
            "start_at": start.isoformat(timespec="seconds"),
            "end_at": end.isoformat(timespec="seconds"),
            "label": "até 28/08/2026",
            "now": now.isoformat(timespec="seconds"),
            "days_total": round(total_seconds / 86400.0, 1),
            "days_elapsed": round(elapsed_seconds / 86400.0, 2),
            "days_remaining": round(remaining_days, 2),
            "days_remaining_ceil": max(0, int((remaining_seconds + 86399) // 86400)),
            "trades_closed": closed_n,
            "trades_open": len(opened),
            "closed_pnl": closed_pnl,
            "open_pnl": open_pnl,
            "pnl": round(closed_pnl + open_pnl, 2),
            "pace_closed_per_day": pace,
            "required_pace_to_10": required_10,
            "required_pace_to_20": required_20,
            "projected_closed_at_end": projected,
            "target_min": 10,
            "target_max": 20,
            "status": status,
            "promotion": self._promotion_status(),
        }

    def _window_summary_line(self) -> str:
        window = self.window_status()
        days = window["days_remaining_ceil"]
        return (
            f"📅 Janela até 28/08 · reais: {window['trades_closed']} fechadas + "
            f"{window['trades_open']} abertas · P&L {window['pnl']:+.2f} "
            f"(realizado {window['closed_pnl']:+.2f} · aberto {window['open_pnl']:+.2f}) · "
            f"faltam {days} dias · ritmo {window['pace_closed_per_day']:.2f}/dia "
            f"(meta 10–20; necessário {window['required_pace_to_10']:.2f}–"
            f"{window['required_pace_to_20']:.2f}/dia)"
        )

    async def _check_killswitch_rearm(self) -> None:
        """Rearm a pausa global uma vez no prazo UTC persistido."""
        scheduled = self._parse_utc(self.config.get("killswitch_rearm_at", ""))
        if not scheduled or datetime.now(timezone.utc) < scheduled:
            return
        was_active = bool(self.config.get("kill_switch") or self.config.get("auto_kill"))
        self.config["kill_switch"] = False
        self.config["auto_kill"] = False
        self.config["killswitch_rearm_at"] = ""
        self._persist_config(["kill_switch", "auto_kill", "killswitch_rearm_at"])
        if was_active:
            await self.notifier.send(
                "system",
                "🔓 Rearme automático do kill switch concluído — trading reativado "
                "no fim da janela de 28/08.",
                tg_text="🔓 *Rearme automático do kill switch concluído*: trading reativado no fim da janela de 28/08.",
            )

    # ------------------------------------------------------- daily summary

    async def _check_orphan_report(self) -> None:
        """Notificação AGREGADA por categoria quando o relatório 'porquê sem
        trade' deteta pendentes órfãs/expiradas/canceladas — sem depender de
        abrir o relatório. Corre a cada ~1h; cada perna fechada é notificada
        uma única vez (guard `orphan_report_last_check` com o timestamp da
        última corrida — pernas fechadas depois disso). Read-only para a
        execução: nunca fecha nem altera ordens, só avisa.
        """
        now = datetime.now(timezone.utc)
        last = self.config.get("orphan_report_last_check")
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < 3600:
                    return
            except (ValueError, TypeError):
                pass
        since = last or (now - timedelta(hours=24)).isoformat(timespec="seconds")
        try:
            rows = self.db.closed_pending_positions(since)
        except Exception as exc:
            print(f"[rejeições] check de pendentes órfãs falhou: {exc}")
            return
        # guard avança SÓ depois da consulta correr bem — se falhar (BD/IO),
        # a próxima corrida volta a apanhar as mesmas pernas (nunca se perde
        # a notificação; no pior caso duplica-se, que é o erro seguro).
        self.config["orphan_report_last_check"] = now.isoformat(timespec="seconds")
        self._persist_config(["orphan_report_last_check"])
        if not rows:
            return
        counts: dict = {}
        for r in rows:
            key = r.get("exit_reason") or "pending_expired"
            counts[key] = counts.get(key, 0) + 1
        labels = {
            "pending_orphan": "órfã(s) — ticket perdido no broker/restart",
            "pending_cancelled": "cancelada(s) pelo broker",
            "pending_expired": "expirada(s) por TTL (preço não voltou à entrada)",
        }
        parts = [f"{counts.get(k, 0)} {labels[k]}" for k in
                 ("pending_orphan", "pending_cancelled", "pending_expired")
                 if counts.get(k)]
        await self.notifier.send(
            "system",
            "🧨 'Porquê sem trade': " + ", ".join(parts) +
            " — pendente(s) da zona que nunca preencheram. Abre o relatório "
            "para ver porquê o preço não voltou ao nível da entrada.",
        )

    async def _check_recount_batch(self) -> None:
        """Re-check em lote dos recounts (diário sobre a janela recente + uma
        vez por semana sobre o histórico COMPLETO, days=0).

        O dia corre o candle-check de todos os recounts com candles frescos
        e o offset mais recente (fontes combinadas MT5 spot + GC=F, cache por
        símbolo) e notifica quando algum veredicto persistido muda de regime
        ou quando há erros — o operador reavalia no /auditoria. A corrida
        semanal re-corre TUDO (days=0) para os veredictos antigos ficarem
        todos corrigidos com o offset medido (o offset diário só existe para
        dias com fills medidos; o semanal abrange os recortes antigos com o
        fallback da média). Cada corrida fica persistida com os flips
        resolvidos. Read-only para a execução; os guard de estado ficam em
        `recount_batch_last_run` / `recount_batch_week_last_run` (config)."""
        today = datetime.now(timezone.utc).date().isoformat()
        # guard diário (janela recente)
        if self.config.get("recount_batch_last_run") == today:
            return
        self.config["recount_batch_last_run"] = today
        self._persist_config(["recount_batch_last_run"])
        # guard SEMANAL: uma vez por semana (sempre no mesmo dia) corre o
        # histórico COMPLETO para os veredictos antigos corrigidos com o
        # offset medido
        week_key = self.config.get("recount_batch_week_last_run", "")
        iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
        current_week = f"{iso_year}-W{iso_week:02d}"
        weekly = week_key != current_week
        if weekly:
            self.config["recount_batch_week_last_run"] = current_week
            self._persist_config(["recount_batch_week_last_run"])
        try:
            res = await self.recheck_all_recounts(days=0 if weekly else 365,
                                                  window="weekly" if weekly else "daily")
            if not res.get("ok"):
                return
            flips = res.get("flips") or []
            flip_str = ""
            if flips:
                flip_list = ", ".join(
                    f"{f.get('signal_number', '?')} ({f.get('day', '')}: "
                    f"{f.get('from')} → {f.get('to')}"
                    + (f", offset {f.get('shift')}" if f.get('shift') is not None else "")
                    + ")" for f in flips[:5])
                flip_str = (f" · flips: {flip_list}"
                            + (" …" if len(flips) > 5 else ""))
            # notifica quando há veredictos ALTERADOS, RESOLVIDOS
            # (implausível/feed → plausível: eram feed, não relato falso) ou
            # erros — um flip implausível→plausível não muda `changed` (o
            # `plausible` já era True na divergência de feed), por isso o
            # `resolved` conta-o à parte
            if ((res.get("changed") or 0) > 0
                    or (res.get("resolved") or 0) > 0
                    or (res.get("errors") or 0) > 0):
                label = "semanal" if weekly else "diário"
                await self.notifier.send(
                    "system",
                    f"🗂 Re-check {label}: {res.get('rechecked', 0)}/"
                    f"{res.get('total', 0)} recounts re-corridos · "
                    f"{res.get('changed', 0)} com veredicto alterado · "
                    f"{res.get('resolved', 0)} resolvido(s) (eram "
                    f"implausíveis/feed e agora plausíveis — o offset diário "
                    f"do GC=F mostrou que eram feed) · {res.get('errors', 0)} "
                    f"erros{flip_str}. Historial no /auditoria.",
                )
        except Exception as exc:
            print(f"[recount] re-check em lote falhou: {exc}")

    async def _check_refusals_backfill(self) -> None:
        """Guard SEMANAL (ISO-week): re-corre o backfill de recusas do broker.

        Uma vez por semana (`refusals_backfill_week_last_run` na config)
        volta a varrer as trades fechadas com assinatura phantom (sem `error`
        gravado) e persiste o trace durável (error/retcodes/auto_trading_off)
        a partir da evidência do journal — para nenhum fecho phantom ficar
        sem rasto mesmo que o alerta original (retcode 10027, worker caído,
        etc.) tenha sido perdido. Audit-only: nunca abre/fecha trades nem
        toca no MT5; notifica só quando o backfill encontrou algo para
        gravar (sinal de que um alerta se perdeu)."""
        week_key = self.config.get("refusals_backfill_week_last_run", "")
        iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
        current_week = f"{iso_year}-W{iso_week:02d}"
        if week_key == current_week:
            return
        self.config["refusals_backfill_week_last_run"] = current_week
        self._persist_config(["refusals_backfill_week_last_run"])
        try:
            from app.backfill_trade_refusals import run_refusals_backfill
            res = run_refusals_backfill(self.db, apply=True)
            applied = res.get("applied") or 0
            if applied:
                await self.notifier.send(
                    "system",
                    f"🗂 Backfill semanal de recusas: {applied} trade(s) "
                    f"phantom sem trace durável — error/retcodes gravados "
                    f"a partir do journal (o alerta original não chegou a "
                    f"ficar registado?). {res.get('candidates') or 0} "
                    f"candidata(s) revista(s). Historial na auditoria.",
                )
        except Exception as exc:
            print(f"[refusals] backfill semanal falhou: {exc}")

    async def _check_be_buffer_drift(self) -> None:
        """Vigia do drift do spread: tracka o buffer BE+ adaptativo por símbolo
        (uma linha por dia em `be_buffer_history`) e alerta `risk` quando o
        spread médio (7d) sai da banda saudável [20, 50] pips — spread muito
        baixo esmaga o buffer (saídas BE+ sem cobrir custos), muito alto
        infla-o (stops antecipados). Uma vez por dia por símbolo+direção
        (guard diário), read-only para a execução."""
        if not self.config.get("be_buffer_adaptive", True):
            return
        today = datetime.now(timezone.utc).date().isoformat()
        lo, hi = 20.0, 50.0
        try:
            symbols = self.db.spread_symbols(days=self._BE_BUFFER_WINDOW_DAYS)
        except Exception:
            return
        for symbol in symbols:
            try:
                avg = self.db.avg_spread_pips(
                    symbol, days=self._BE_BUFFER_WINDOW_DAYS)
            except Exception:
                continue
            if avg is None or avg <= 0:
                continue
            # track diário (uma linha por símbolo+dia; idempotente)
            with self.db.tx() as cur:
                cur.execute(
                    "INSERT INTO be_buffer_history "
                    "(day, symbol, avg_spread_pips, buffer_pips) VALUES (?,?,?,?) "
                    "ON CONFLICT(day, symbol) DO UPDATE SET "
                    "avg_spread_pips=excluded.avg_spread_pips, "
                    "buffer_pips=excluded.buffer_pips",
                    (today, symbol, round(avg, 4),
                     round(min(self._BE_BUFFER_MAX_PIPS,
                               max(self._BE_BUFFER_MIN_PIPS,
                                   avg * self._BE_BUFFER_SPREAD_FACTOR)), 1)),
                )
            if lo <= avg <= hi:
                continue
            guard = f"be_drift_last_{symbol}"
            if self.config.get(guard) == today:
                continue
            self.config[guard] = today
            self._persist_config([guard])
            avg_r = round(avg, 1)
            await self.notifier.send(
                "risk",
                f"📏 Spread médio (7d) de {symbol}: {avg_r} pips — fora da banda "
                f"saudável [{lo:.0f}, {hi:.0f}] pips. Buffer BE+ adaptativo em "
                f"{round(min(self._BE_BUFFER_MAX_PIPS, max(self._BE_BUFFER_MIN_PIPS, avg * self._BE_BUFFER_SPREAD_FACTOR)), 1)} pips "
                f"({'esmagado — saídas BE+ podem não cobrir o custo real' if avg < lo else 'inflado — stops BE+ antecipados possíveis'}).",
            )

    async def _check_cap_reminder(self) -> None:
        """Lembrete no feed quando o cap autónomo está à espera de dados
        reais pós-cap há mais de `cap_reminder_days` (config, default 7)
        para o operador correr o `study_cap_effect --days 30` e decidir se o
        cap fica, aperta ou volta ao default.

        A decisão honesta precisa de fills reais DEPOIS do momento em que o
        cap entrou (`cap_applied_at`); enquanto `cap_applied_at` existir e
        não houver ainda decisão (nenhum fill pós-cap a validar), o lembrete
        avisa uma vez por guard (dia) até o estudo correr. Read-only para a
        execução."""
        applied = self.config.get("cap_applied_at") or ""
        if not applied:
            return  # nunca aplicado (default 0.0) — nada a decidir
        today = datetime.now(timezone.utc)
        try:
            applied_dt = datetime.fromisoformat(str(applied))
        except (ValueError, TypeError):
            return
        if today - applied_dt < timedelta(days=float(
                self.config.get("cap_reminder_days") or 7)):
            return
        # já há decisão? decisão = há fills reais pós-cap que o estudo usaria
        # (o estudo reporta decidido quando os fills pós-cap não são "0").
        # Runs persistidos de `study_cap_effect` não existem na BD — usamos
        # uma avaliação leve: algum fill real MT5 aberto DEPOIS de
        # `cap_applied_at`? (os pendentes nunca-preenchidos não contam)
        never = ("pending_expired", "pending_orphan", "pending_cancelled")
        since = (today - timedelta(days=float(
            self.config.get("cap_reminder_lookback_days") or 30))).isoformat()
        has_after = False
        try:
            for t in self.db.trades_opened_since(since):
                if not (t.execution_mode or "").startswith("mt5"):
                    continue
                if (t.exit_reason or "") in never:
                    continue
                try:
                    if datetime.fromisoformat((t.opened_at or "")).timestamp() \
                            >= applied_dt.timestamp():
                        has_after = True
                        break
                except (ValueError, TypeError):
                    continue
        except Exception as exc:
            print(f"[cap] verificação de fills pós-cap falhou: {exc}")
            return
        # sem fills pós-cap ainda? lembra de decidir (uma vez por dia — guard)
        if has_after:
            return  # já há dados — o estudo decide; não repetir o lembrete
        last = self.config.get("cap_reminder_last_sent")
        if last == today.date().isoformat():
            return
        self.config["cap_reminder_last_sent"] = today.date().isoformat()
        self._persist_config(["cap_reminder_last_sent"])
        days = (today.date() - applied_dt.date()).days
        await self.notifier.send(
            "system",
            f"⏰ Cap autónomo (0.5×) aplicado há {days} dias e ainda sem "
            f"fills reais pós-cap. Corre "
            f"`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m "
            f"app.study_cap_effect --days 30` para decidir se o cap fica, "
            f"aperta ou volta ao default com os dados reais.",
        )

    async def _check_daily_summary(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.config.get("summary_sent_date") == today:
            return
        try:
            hh, mm = str(self.config["summary_time"]).split(":")
            target = time(int(hh), int(mm))
        except (ValueError, AttributeError):
            return
        if datetime.now(timezone.utc).time() >= target:
            await self.daily_summary()

    def _journal_forward_summary_lines(self, today: str) -> list[str]:
        """Build the single daily evidence block for feed and Telegram.

        It is deliberately observational: reports read persisted journal and
        shadow rows, while the normal ``daily_summary`` notification remains
        the only send operation and no execution/config decision is made.
        """
        try:
            from app.execution.evidence import (forward_gate_report,
                                                journal_quality_report)
            quality = journal_quality_report(self.db, day=today)
            orders = quality.get("orders") or {}
            tapes = quality.get("trades") or {}
            lines = [
                "🧾 Qualidade journal (UTC): "
                f"{quality.get('events', {}).get('total', 0)} eventos · "
                f"{quality.get('signals', {}).get('received', 0)} sinais · "
                f"{quality.get('management', 0)} gestão · "
                f"{quality.get('fills', {}).get('open', 0)} fills abertura / "
                f"{quality.get('fills', {}).get('close', 0)} fecho · "
                f"ordens {orders.get('accepted', 0)} aceites / "
                f"{orders.get('rejected', 0)} recusadas · "
                f"{tapes.get('incomplete_tape', 0)} fitas incompletas",
            ]
            gate = forward_gate_report(self.db, days=30)
            status = (gate.get("period") or {}).get("status", "rolling")
            lines.append(
                "🧪 Gate forward (shadow, sem promoção automática): "
                f"{status} · janela {(gate.get('period') or {}).get('start', '')[:10]} → "
                f"{(gate.get('period') or {}).get('end', '')[:10]}"
            )
            for row in gate.get("strategies", []):
                failed = [key for key, value in (row.get("gates") or {}).items() if not value]
                lines.append(
                    f"  {row.get('strategy_id')}: N={row.get('closed', 0)}/"
                    f"{gate.get('policy', {}).get('min_trades', 20)} · "
                    f"PF={row.get('profit_factor') if row.get('profit_factor') is not None else '—'} · "
                    f"{'PASSOU' if row.get('eligible') else 'em recolha'}"
                    + (f" · falta: {', '.join(failed)}" if failed else "")
                )
            lines.append(self._recount_summary_line(today))
            return lines
        except Exception as exc:
            # A falha de telemetria nunca pode impedir o resumo operacional.
            return [f"🧾 Evidência journal/gate indisponível neste resumo: {exc}"]

    def _be_plus_summary_line(self, today: str) -> str:
        """Linha BE+ do resumo diário: saídas protegidas com lucro hoje e o
        contrafactual "BE clássico teria corrido até TP2" (quando há velas).
        Read-only; a falha nunca impede o resumo."""
        try:
            from app.execution.audit import daily_execution_audit
            audit = daily_execution_audit(
                self.db, day=today)
            bp = audit.get("be_plus") or {}
            total = int(bp.get("exits_total") or 0)
            if not total:
                return "🛡️ BE+ do dia: sem saídas protegidas"
            profit = int(bp.get("exits_with_profit") or 0)
            kept = bp.get("kept_running_to_tp2")
            checked = int(bp.get("checked") or 0)
            line = (f"🛡️ BE+ do dia: {total} saídas protegidas · "
                    f"{profit} com lucro")
            if kept is not None:
                line += f" · {kept} teriam corrido até TP2 (de {checked} verificadas)"
            return line
        except Exception:
            return ""

    def _recount_summary_line(self, today: str) -> str:
        """Linha de qualidade dos recounts do dia para o resumo diário (R2).

        Conta os recounts recebidos hoje, os implausíveis (candle-check) e
        quem divergiu (o grupo ou a execução), a partir do journal quando
        ligado. Observacional: só lê, nunca altera execução.
        """
        try:
            rows = [r for r in self.db.list_trade_recounts(limit=500)
                    if (r.get("received_at") or "")[:10] == today]
            if not rows:
                return "📋 Recounts do dia: nenhum recebido hoje"
            total = len(rows)
            # implausíveis: eventos do journal de hoje (evidência estruturada,
            # o mesmo cruzamento do fill-audit)
            from app.execution.evidence import _recount_implausible_evidence
            impl = _recount_implausible_evidence(
                self.db, f"{today}T00:00:00+00:00",
                f"{today}T23:59:59.999999+00:00",
            )
            diverged = sum(1 for e in impl if not e.get("bot_traded"))
            line = f"📋 Recounts do dia: {total} recebidos"
            if impl:
                line += f" · {len(impl)} implausíveis"
                if diverged:
                    line += f" · {diverged} sem execução do bot"
            else:
                line += " · sem implausíveis (journal desligado ou relatos ok)"
            return line
        except Exception as exc:
            return f"📋 Recounts do dia indisponíveis: {exc}"

    async def daily_summary(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
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
        text += "\n\n" + self._window_summary_line()
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
        evidence_lines = self._journal_forward_summary_lines(today)
        if evidence_lines:
            text += "\n\n" + "\n".join(evidence_lines)
        be_plus_line = self._be_plus_summary_line(today)
        if be_plus_line:
            text += "\n\n" + be_plus_line
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
