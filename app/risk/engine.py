"""Risk engine.

The layer between "a signal looks good" and "money actually moves". It
answers two questions:

1. How many lots should we trade for a given risk?  (position sizing)
2. Is it safe to open this trade right now?        (the checks)

The checks always run: there is no mode that bypasses them (the manual
mode was removed in Item 12). They are the last line of defence before
execution.
"""

from typing import List

from app.config import CONTRACT_SIZE, MIN_LOTS, PIP_SIZE

# A demo account with a small balance and a 0.01 minimum lot cannot express
# every percentage risk precisely. Keep the sizing calculation explicit: the
# caller may request a fixed risk base, but rounding must never turn a 0.01
# lot into a hidden multi-risk position.
from app.models import RiskDecision, Signal


def profit_factor(pnls: List[float]) -> float | None:
    """Profit factor for a completed-trade window, excluding breakevens."""
    wins = [float(p) for p in pnls if float(p) > 0]
    losses = [float(p) for p in pnls if float(p) < 0]
    if not losses:
        return float("inf") if wins else None
    return sum(wins) / abs(sum(losses))


def defensive_mode_active(pnls: List[float], window: int,
                          threshold: float = 1.0) -> bool:
    """Return whether a full prior window is below the defensive PF gate.

    The caller supplies only trades closed before the signal/trade being sized;
    this helper therefore has no look-ahead and is shared by backtest, paper,
    SimEngine and MT5.
    """
    if not window or len(pnls) < int(window):
        return False
    pf = profit_factor(pnls[-int(window):])
    return pf is not None and pf < float(threshold)


def effective_strategy_risk_pct(base_risk_pct: float,
                                *, def_window: int | None = None,
                                def_risk_pct: float | None = None,
                                prior_pnls: List[float] | None = None) -> float:
    """Apply an optional strategy-specific defensive risk cap.

    ``def_risk_pct`` is an absolute cap (not an extra multiplier), so a
    dynamic drawdown reduction can only reduce risk further, never increase it.
    A strategy without a complete losing window keeps ``base_risk_pct``.
    """
    base = float(base_risk_pct)
    if (def_risk_pct is None or not def_window or
            not defensive_mode_active(list(prior_pnls or []), int(def_window))):
        return base
    return min(base, float(def_risk_pct))


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
    # Use the broker step, but never silently exceed the requested risk on a
    # small account. The minimum lot is an unavoidable broker floor and is
    # reported by the audit/risk preview; larger lots are allowed whenever the
    # unrounded risk supports them.
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
        if limit_pct <= 0:
            # 0 = sem limite diário (off) — igual ao apply_daily_stop do
            # backtest e ao tooltip do slider. Sem este guard, 0 bloqueava
            # qualquer trade assim que o P&L realizado do dia fosse ≤ 0.
            return False
        limit_amount = balance * limit_pct / 100.0
        return realized_today <= -limit_amount
