"""Small, side-effect-free helpers for cross-engine execution observations.

The engines keep their own persistence models, so reports and regression tests
need one vocabulary for the observable result of a leg.  This module does not
run a broker or mutate state; it only normalizes position-like objects.
"""

from typing import Iterable, Optional


def position_observation(positions: Iterable, trade_reason: Optional[str] = None) -> dict:
    """Return a stable decision/SL/reason/P&L snapshot for any position rows."""
    rows = sorted(positions, key=lambda item: int(item.leg))
    return {
        "decisions": tuple(
            (int(pos.leg), pos.status, round(pos.close_price or 0.0, 4),
             pos.exit_reason)
            for pos in rows
        ),
        "applied_sl": tuple(
            (int(pos.leg), round(float(pos.stop_loss), 4)) for pos in rows
        ),
        "reason": trade_reason,
        "pnl": round(sum(float(pos.pnl or 0.0) for pos in rows), 2),
    }
