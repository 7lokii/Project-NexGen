"""Data models.

Request/response models for the FastAPI layer live here, together with the
internal dataclasses used across the engine. Internal models store
`direction` as a plain string ("buy"/"sell") to keep serialization simple.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


# --------------------------------------------------------------------------
# API request models
# --------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    """Partial update of the bot configuration (from the dashboard)."""

    mode: Optional[str] = None  # legado do Item 12: só "auto" é aceite (manual removido)
    risk_pct: Optional[float] = Field(None, ge=0.1, le=10.0)
    # Risco por lane (base): `risk_pct` continua a ser o da principal (legado
    # e sincronizado com `risk_pct:main`); `risk_pct_tp2` é a base da 2ª conta.
    risk_pct_main: Optional[float] = Field(None, ge=0.1, le=10.0)
    risk_pct_tp2: Optional[float] = Field(None, ge=0.1, le=10.0)
    # Lote fixo por perna da 2ª conta (`lots_override:tp2` na config): quando
    # definido (>0), cada perna da lane 2 abre SEMPRE nesse lote em vez do
    # dimensionamento por risco (0 = volta ao dimensionamento por risco).
    lots_override_tp2: Optional[float] = Field(None, ge=0.0, le=10.0)
    daily_loss_limit_pct: Optional[float] = Field(None, ge=0, le=50.0)
    max_positions: Optional[int] = Field(None, ge=1, le=50)
    sim_speed: Optional[float] = Field(None, ge=0, le=600)
    edit_window_seconds: Optional[float] = Field(None, ge=0, le=3600)
    entry_guard_enabled: Optional[bool] = None
    entry_guard_tolerance: Optional[float] = Field(None, ge=0, le=20)
    entry_guard_max_zone_widths: Optional[float] = Field(None, ge=0, le=20)
    entry_guard_auto_cap: Optional[float] = Field(None, ge=0, le=20)  # múltiplos da largura da zona; 0 = limite máximo
    autonomous_mode: Optional[bool] = None  # sem confirmações humanas: guardas auto-executam ou rejeitam
    pending_expire_minutes: Optional[float] = Field(None, ge=0, le=10080)  # expiração automática de pendentes
    entry_style: Optional[str] = None  # market | pending (ordem na zona — Fase 1)
    entry_order_ttl_hours: Optional[float] = Field(None, ge=0, le=336)  # TTL das ordens pendentes (horas; 0 = sem)
    group_feed_divergence_threshold: Optional[float] = Field(None, ge=0, le=10000)
    lane2_balance: Optional[float] = Field(None, ge=0, le=100000000)
    lane2_strategy: Optional[str] = None  # estratégia da segunda conta/lane
    lane2_label: Optional[str] = None  # rótulo operacional da segunda lane
    strategy: Optional[str] = None   # id do registo em app/strategies.py
    teste_days: Optional[int] = Field(None, ge=0, le=365)  # seletor das páginas de teste (0 = tudo)
    paper_race_hours: Optional[int] = Field(None, ge=24, le=720)  # horizonte da corrida paralela (24h/7d/30d)
    fase3_target: Optional[int] = Field(None, ge=10, le=20)  # alvo da janela da Fase 3 (default 20; o feed pode baixar para 15)
    fase3_target_reason: Optional[str] = None  # motivo da alteração do alvo (regista-se no fase3_target_log)
    shadow_paper_enabled: Optional[bool] = None  # corrida paper isolada da execução principal
    shadow_paper_strategies: Optional[List[str]] = None  # ids que recebem sinais shadow
    execution_journal_enabled: Optional[bool] = None  # captura opt-in para replay determinístico
    killswitch_rearm_at: Optional[str] = None  # instante UTC persistido do rearme automático


class KillSwitchUpdate(BaseModel):
    enabled: bool


class PreRealDecisionIn(BaseModel):
    decision: str = Field(..., min_length=2, max_length=20)
    confirmation: str = Field(..., min_length=1, max_length=1000)


class SimulateIn(BaseModel):
    """Body for the backtest/simulator: replay real signals on historical prices."""

    risk_pct: float = Field(1.0, ge=0.1, le=10.0)
    days: int = Field(7, ge=1, le=90)
    delay_seconds: int = Field(60, ge=0, le=3600)  # trade opened N seconds after the signal
    spread_pips: float = Field(1.0, ge=0, le=100)         # spread em pips
    slippage_pips: float = Field(0.5, ge=0, le=100)       # slippage na entrada (pips)
    commission_per_lot: float = Field(3.5, ge=0, le=100)  # USD por lote, por lado


class SignalIn(BaseModel):
    """A signal to ingest.

    Either provide `raw_text` (parsed like a Telegram message) or the
    structured fields directly.

    `message_id` is the Telegram message id. When present, the signal is
    buffered (`awaiting_edit`) for `edit_window_seconds` ONLY when the first
    parse comes incomplete; re-sending the same `message_id` with new text is
    treated as an edit, re-parses and resets the window. A complete parse
    executes immediately after the window-less risk checks.
    """

    raw_text: Optional[str] = None
    source: str = "manual"
    message_id: Optional[str] = None
    group: Optional[str] = None  # stable per-group id (chat id); disambiguates message ids
    reply_to_msg_id: Optional[int] = None  # Telegram msg this one replies to (management correlation)
    historical: bool = False  # True -> signal from the group's past history (no execution)
    received_at: Optional[str] = None  # original message timestamp (history load)
    symbol: Optional[str] = None
    direction: Optional[Direction] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    take_profit_4: Optional[float] = None
    take_profit_5: Optional[float] = None
    lots: Optional[float] = None  # fixed size (all legs); None = risk-based sizing
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None


# --------------------------------------------------------------------------
# Internal models
# --------------------------------------------------------------------------

@dataclass
class Signal:
    id: str
    source: str
    raw_text: str
    symbol: str
    direction: str          # "buy" | "sell"
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    status: str             # awaiting_edit | pending | rejected | executed | failed
    received_at: str        # ISO timestamp
    take_profit_3: float = 0.0
    take_profit_4: float = 0.0
    take_profit_5: float = 0.0
    error: Optional[str] = None
    message_id: Optional[str] = None     # Telegram message id (edit window)
    reply_to_msg_id: Optional[str] = None  # msg this one replies to (management correlation)
    edit_deadline: Optional[str] = None  # ISO timestamp; continue after this
    edited_at: Optional[str] = None     # ISO: última edição do grupo (medir atraso)
    lots: Optional[float] = None         # fixed size; None = risk-based sizing
    llm_parsed: bool = False             # parsed by the LLM fallback (audit only — never executes)
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    pending_kind: Optional[str] = None   # entry_guard | min_lot_risk (aguarda confirmação)
    pending_meta: Optional[dict] = None  # guard/risco p/ mostrar antes de aprovar
    revived_from: Optional[str] = None   # estado anterior quando reaberto (ex.: "rejected")
    revived_reason: Optional[str] = None  # motivo da recusa/erro ANTERIOR (guardado no revive)
    revived_at: Optional[str] = None     # ISO: instante em que o grupo reabriu a janela

    def to_dict(self):
        return asdict(self)


@dataclass
class Trade:
    id: str
    signal_id: str
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_pct: float
    lots_1: float
    lots_2: float
    status: str             # open | closed
    opened_at: str
    strategy_id: Optional[str] = None  # paper trading forward (registo estratégias)
    take_profit_3: float = 0.0
    take_profit_4: float = 0.0
    take_profit_5: float = 0.0
    lots_3: float = 0.0
    lots_4: float = 0.0
    lots_5: float = 0.0
    closed_at: Optional[str] = None
    pnl: float = 0.0
    error: Optional[str] = None  # set when the broker refused the orders
    exit_reason: Optional[str] = None  # razão dominante do fecho (ver Position.exit_reason)
    retcodes: Optional[list] = None  # retcodes das pernas recusadas (MT5Engine)
    entry_style: Optional[str] = None  # market | pending (ordem na zona — Fase 1)
    auto_trading_off: Optional[bool] = None  # True quando retcode 10027 (AutoTrading OFF)
    paper_origin: Optional[str] = None  # apenas paper_trades: race | shadow
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    market_price_at_open: Optional[float] = None
    entry_distance: Optional[float] = None
    entry_guard: Optional[str] = None
    execution_mode: Optional[str] = None
    lane_id: Optional[str] = None  # "main" (conta principal) | "tp2" (2ª conta demo)

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    id: str
    trade_id: str
    leg: int                # 1..5 (closes at TP1..TP5)
    symbol: str
    direction: str
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    status: str             # open | closed
    opened_at: str
    strategy_id: Optional[str] = None  # paper trading forward
    closed_at: Optional[str] = None
    close_price: Optional[float] = None
    pnl: float = 0.0
    exit_reason: Optional[str] = None  # sl | tpN | be | protect | close_all | close_tp1 | close_tp | move_sl | killswitch | manual
    exit_msg_id: Optional[str] = None  # id da mensagem de gestão do grupo que causou o fecho
    lane_id: Optional[str] = None  # "main" | "tp2"
    order_ticket: Optional[int] = None  # ticket da ordem pendente MT5 (status pending_fill)

    def to_dict(self):
        return asdict(self)


@dataclass
class Notification:
    id: int
    kind: str               # trade_open | trade_close | risk | system | summary
    text: str
    created_at: str
    action: Optional[str] = None  # ex.: "promote:<strategy_id>" (botão no feed)
    lane: Optional[str] = None  # "main" | "tp2" (vazio = global/conta principal)
    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "text": self.text,
                "created_at": self.created_at, "action": self.action,
                "lane": self.lane or ""}


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list = field(default_factory=list)
