"""Data models.

Request/response models for the FastAPI layer live here, together with the
internal dataclasses used across the engine. Internal models store
`direction` as a plain string ("buy"/"sell") to keep serialization simple.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Mode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


# --------------------------------------------------------------------------
# API request models
# --------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    """Partial update of the bot configuration (from the dashboard)."""

    mode: Optional[Mode] = None
    risk_pct: Optional[float] = Field(None, ge=0.1, le=10.0)
    daily_loss_limit_pct: Optional[float] = Field(None, ge=0.5, le=50.0)
    max_positions: Optional[int] = Field(None, ge=1, le=50)
    sim_speed: Optional[float] = Field(None, ge=0, le=600)
    edit_window_seconds: Optional[float] = Field(None, ge=0, le=3600)
    strategy: Optional[str] = None   # id do registo em app/strategies.py
    teste_days: Optional[int] = Field(None, ge=0, le=365)  # seletor das páginas de teste (0 = tudo)
    paper_race_hours: Optional[int] = Field(None, ge=24, le=720)  # horizonte da corrida paralela (24h/7d/30d)
    fase3_target: Optional[int] = Field(None, ge=10, le=20)  # alvo da janela da Fase 3 (default 20; o feed pode baixar para 15)
    fase3_target_reason: Optional[str] = None  # motivo da alteração do alvo (regista-se no fase3_target_log)


class KillSwitchUpdate(BaseModel):
    enabled: bool


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
    buffered (`awaiting_edit`) for `edit_window_seconds`: re-sending the
    same `message_id` with new text is treated as an edit, re-parses and
    resets the window. Only after the window closes does the pipeline
    continue (manual -> pending, auto -> execute).
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
    status: str             # awaiting_edit | pending | approved | rejected | executed | failed
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
    llm_parsed: bool = False             # parsed by the LLM fallback (needs review)

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
    auto_trading_off: Optional[bool] = None  # True quando retcode 10027 (AutoTrading OFF)

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

    def to_dict(self):
        return asdict(self)


@dataclass
class Notification:
    id: int
    kind: str               # trade_open | trade_close | risk | system | summary
    text: str
    created_at: str
    action: Optional[str] = None  # ex.: "promote:<strategy_id>" (botão no feed)


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list = field(default_factory=list)
