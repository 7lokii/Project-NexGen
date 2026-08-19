"""Risk engine.

The layer between "a signal looks good" and "money actually moves". It
answers two questions:

1. How many lots should we trade for a given risk?  (position sizing)
2. Is it safe to open this trade right now?        (the checks)

The checks are the same in manual and auto mode: a human click does not
bypass them. They are the last line of defence before execution.
"""

from typing import List

from app.config import CONTRACT_SIZE, MIN_LOTS, PIP_SIZE
from app.models import RiskDecision, Signal


def compute_lots(
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    pip_size: float = PIP_SIZE,
    contract_size: float = CONTRACT_SIZE,
) -> float:
    """Size a position so that a stop-out loses exactly `risk_pct` of equity.

    lots = (equity * risk%) / (|entry - SL| in pips * pip value per lot)

    The same risk slider therefore produces different lot sizes depending on
    how far the stop loss is: tight SL -> bigger lots, wide SL -> smaller lots.
    """
    if risk_pct <= 0:
        return MIN_LOTS
    risk_amount = equity * risk_pct / 100.0
    risk_pips = abs(entry - stop_loss) / pip_size
    if risk_pips <= 0:
        raise ValueError("stop_loss must differ from entry")
    pip_value_per_lot = contract_size * pip_size
    lots = risk_amount / (risk_pips * pip_value_per_lot)
    # round down to the standard 0.01 step, never below the minimum
    lots = max(MIN_LOTS, round(lots / MIN_LOTS) * MIN_LOTS)
    return lots


class RiskEngine:
    """Stateless checks. The caller (BotState) owns the configuration."""

    def check(
        self,
        signal: Signal,
        config: dict,
        open_trades: List,
        realized_today: float,
        balance: float,
    ) -> RiskDecision:
        reasons: List[str] = []

        if config.get("kill_switch"):
            reasons.append("kill switch ativo (pausa global)")

        if config.get("auto_kill"):
            reasons.append("bloqueio automático: limite de perda diária atingido")

        if len(open_trades) >= int(config.get("max_positions", 3)):
            reasons.append(
                f"máximo de posições abertas atingido ({len(open_trades)})"
            )

        if self.daily_loss_exceeded(balance, realized_today, config):
            reasons.append("limite de perda diária excedido")

        return RiskDecision(allowed=not reasons, reasons=reasons)

    def daily_loss_exceeded(self, balance: float, realized_today: float, config: dict) -> bool:
        limit_pct = float(config.get("daily_loss_limit_pct", 3.0))
        limit_amount = balance * limit_pct / 100.0
        return realized_today <= -limit_amount
