"""Pure protection policy shared by backtest, paper and MT5.

This module deliberately contains no database, broker or clock access.  The
three execution adapters own fills and persistence; this module decides only
which protection level is valid and which legs should be affected.  Keeping
these boundaries in one place prevents the engines from drifting on monotonic
SL, ATR floor, same-timestamp time stops and confirmation semantics.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional, Tuple


Winner = Literal["auto", "group", "tie", "none"]
Leg1Action = Literal["closed", "skipped", "already_closed"]


def more_protective_sl(direction: str, left: Optional[float],
                       right: Optional[float]) -> Optional[float]:
    """Return the more protective of two SLs, preserving missing values."""
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right) if direction == "buy" else min(left, right)


def monotonic_sl(direction: str, current: Optional[float],
                 candidate: Optional[float]) -> Optional[float]:
    """Return ``candidate`` only when it improves an existing SL.

    A buy can only raise its SL and a sell can only lower it.  Equality and a
    missing candidate are no-ops.  Every engine uses this exact boundary rule
    before persisting or sending a modify to a broker.
    """
    if candidate is None:
        return None
    if current is None:
        return candidate
    if direction == "buy":
        return candidate if candidate > current + 1e-12 else None
    return candidate if candidate < current - 1e-12 else None


def confirmation_legs(trigger_leg: int, open_legs: Iterable[int]) -> Tuple[int, ...]:
    """Legs a confirmation filter closes after ``trigger_leg`` stops out."""
    return tuple(sorted(leg for leg in open_legs if leg > trigger_leg))


def time_stop_due(start: Optional[float], now: float,
                  minutes: Optional[float]) -> bool:
    """Use one inclusive time-stop boundary for numeric timestamps.

    The stop is due at exactly ``minutes``; negative/invalid durations and a
    missing start timestamp never trigger it.
    """
    if start is None or minutes is None:
        return False
    try:
        duration = float(minutes)
        if duration <= 0:
            return False
        return float(now) - float(start) >= duration * 60.0
    except (TypeError, ValueError, OverflowError):
        return False


def time_stop_due_iso(start: Optional[str], now: str,
                      minutes: Optional[float]) -> bool:
    """ISO-timestamp adapter for the MT5 engine using the same boundary."""
    if not start or not now or minutes is None:
        return False
    try:
        duration = float(minutes)
        if duration <= 0:
            return False
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        # Historical rows can predate the explicit-UTC convention. Interpret
        # a naive value as UTC so mixing old and new timestamps cannot crash
        # the MT5 management loop with an aware/naive subtraction error.
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return time_stop_due(a.timestamp(), b.timestamp(), duration)
    except (TypeError, ValueError, OverflowError):
        return False


def sl_exit_reason(direction: str, stop_level: float, entry: float,
                   epsilon: float = 1e-12) -> str:
    """Classify a stop fill consistently as ``sl``, ``be`` or ``trail``."""
    if abs(stop_level - entry) <= epsilon:
        return "be"
    protected = stop_level > entry if direction == "buy" else stop_level < entry
    return "trail" if protected else "sl"


def sl_outcome(direction: str, stop_level: float, entry: float) -> str:
    """Map a stop reason to the backtest aggregate outcome vocabulary."""
    reason = sl_exit_reason(direction, stop_level, entry)
    return "win" if reason == "trail" else reason


@dataclass(frozen=True)
class AutoProtectionPolicy:
    """Pure mechanical protection policy for one trade.

    ``stair_target`` and ``trailing_target`` return only an improving target;
    the adapters remain responsible for checking broker distance, throttling,
    sending the order and recording the applied result.
    """

    direction: str
    entry: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    trail_factor: Optional[float] = None
    stair_levels: Optional[int] = None
    stair_from: int = 1
    protect_trigger: Optional[float] = None
    protect_buffer: Optional[float] = None
    # BE+ (tp2_protect): após o TP1 o SL das restantes vai para
    # entrada ± be_buffer_pips × pip_size (cobre o spread) em vez de entrada
    # seca — nunca ultrapassa o TP1. None = BE clássico (entrada).
    be_buffer_pips: Optional[float] = None
    pip_size: float = 0.01

    def initial_floor(self, anchor: Optional[float] = None) -> float:
        """SL after TP1: TP1 for the first stair mode, otherwise breakeven
        (entry + a small positive buffer when ``be_buffer_pips`` is set).

        ``anchor`` é o preço de FILL da perna (quando o adapter o tem) e só
        se aplica no BE+ (com buffer): o chão parte do PIOR custo entre a
        entrada do sinal e o fill — um fill adversário nunca fica com SL
        abaixo do próprio preço de entrada (BE+ tem de ser sem perda).
        Sem buffer (BE clássico) ou sem ``anchor``, comporta-se exatamente
        como antes (entrada do sinal).
        """
        if self.stair_levels and self.stair_from == 1 and self.tp1:
            return float(self.tp1)
        entry = float(self.entry)
        buffer = float(self.be_buffer_pips or 0.0) * float(self.pip_size or 0.0)
        base = entry
        if buffer > 0.0 and anchor is not None:
            try:
                fill = float(anchor)
            except (TypeError, ValueError):
                fill = None
            if fill and fill > 0:
                # pior custo: o nível mais desfavorável entre sinal e fill
                base = max(entry, fill) if self.direction == "buy" \
                    else min(entry, fill)
        if buffer > 0.0 and self.tp1:
            # o SL protegido nunca cruza o TP1 (sentido invertido = sem sentido)
            room = abs(float(self.tp1) - base) - float(self.pip_size)
            buffer = min(buffer, room) if room > 0 else 0.0
        if buffer <= 0.0:
            return entry
        return base + buffer if self.direction == "buy" else base - buffer

    def trail_distance(self, *, atr: Optional[float] = None,
                       atr_k: Optional[float] = None) -> float:
        """Compute ``max(fixed trail, atr_k × ATR)`` with safe fallbacks."""
        anchor = self.tp2 if self.tp2 else self.tp1
        fixed = (
            float(self.trail_factor) * abs(float(anchor) - self.entry)
            if self.trail_factor and anchor else 0.0
        )
        atr_floor = (
            float(atr_k) * float(atr)
            if atr_k and atr and float(atr) > 0 else 0.0
        )
        return max(fixed, atr_floor)

    def stair_target(self, current: Optional[float], tp_level: float,
                     leg: int) -> Optional[float]:
        """Return an improving TP-level stair target for ``leg``."""
        if not self.stair_levels or leg > int(self.stair_levels) or not tp_level:
            return None
        return monotonic_sl(self.direction, current, float(tp_level))

    def trailing_target(self, current: Optional[float],
                        previous_extreme: Optional[float], *,
                        distance: Optional[float] = None,
                        atr: Optional[float] = None,
                        atr_k: Optional[float] = None) -> Optional[float]:
        """Return a trail target based only on the previous peak/valley.

        ``distance`` lets an adapter pass the already-calculated ATR floor;
        when omitted the policy calculates the fixed/ATR distance itself.
        """
        if previous_extreme is None:
            return None
        distance = (self.trail_distance(atr=atr, atr_k=atr_k)
                    if distance is None else float(distance))
        if distance <= 0:
            return None
        raw = (
            float(previous_extreme) - distance
            if self.direction == "buy"
            else float(previous_extreme) + distance
        )
        return monotonic_sl(self.direction, current, raw)


def hybrid_protect_eligible(auto_sl: Optional[float],
                             auto_protection_at: Optional[str]) -> bool:
    """Allow group ``protect`` to act on open legs.

    O gate original existia para não deixar o grupo fechar a perna 1 cedo
    demais antes de proteção mecânica. O incidente 15/09 11:49 mostrou o
    outro lado: um protect do grupo (“Trade is risk free now”) quando o
    preço já está acima da entrada é ESTRITAMENTE protetor — subiu o SL na
    direção do lucro, nunca o alarga — e recusá-lo deixou a perna correr
    com o SL completo até −38,58 €. Desde 15/09 o protect do grupo é
    sempre elegível: o merge monotónico (`decide_protect` →
    `more_protective_sl` + clamp de mercado) garante que nunca aplica um
    SL pior do que o atual nem fecha a perna 1 a preço desfavorável. O
    `auto_protection_at` continua registado para a telemetria de latência.
    """
    return True


def group_protect_sl(*, direction: str, entry: float,
                     stop_loss: float, tp1: float,
                     buffer_fraction: Optional[float] = None) -> float:
    """Calculate the group's protect level without side effects.

    ``buffer_fraction`` is deliberately a fraction of the original risk, not
    of the current price.  ``None`` preserves the historical group rule
    (10%, with a 1% minimum); the autonomous candidate can make the same rule
    explicit in its strategy registry and all engines then share the formula.
    """
    risk = abs(entry - stop_loss)
    fraction = 0.1 if buffer_fraction is None else max(0.0, float(buffer_fraction))
    buffer = (min(max(risk * fraction, risk * 0.01), abs(tp1 - entry))
              if risk > 0 else 0.0)
    return entry + buffer if direction == "buy" else entry - buffer


def protect_trigger_level(*, direction: str, entry: float,
                          tp1: float, fraction: Optional[float]) -> Optional[float]:
    """Return the favorable price at which autonomous protect activates.

    The trigger is a deterministic fraction of entry→TP1. Invalid/disabled
    fractions and TPs on the wrong side of entry return ``None``. No candle,
    broker or wall-clock access belongs here, keeping the rule reusable by
    backtest, paper, SimEngine and MT5.
    """
    if fraction is None:
        return None
    try:
        f = float(fraction)
    except (TypeError, ValueError):
        return None
    if not 0.0 < f <= 1.0 or not entry or not tp1:
        return None
    distance = float(tp1) - float(entry)
    if direction == "buy" and distance <= 0:
        return None
    if direction == "sell" and distance >= 0:
        return None
    return float(entry) + distance * f


def protect_trigger_hit(*, direction: str, price: float, entry: float,
                        tp1: float, fraction: Optional[float]) -> bool:
    """Whether the current known price has reached the autonomous trigger."""
    level = protect_trigger_level(
        direction=direction, entry=entry, tp1=tp1, fraction=fraction,
    )
    if level is None:
        return False
    return float(price) >= level if direction == "buy" else float(price) <= level


@dataclass(frozen=True)
class AutoProtectDecision:
    """Pure reducer output for one autonomous early-protect transition.

    Adapters apply the returned actions to their own DB/broker. The reducer
    owns the trigger, monotonic group-protect level and idempotence gate, so
    backtest, paper, dry-run and MT5 cannot silently disagree on the rule.
    """

    triggered: bool
    trigger_level: Optional[float]
    group_sl: Optional[float]
    move_sls: Tuple[Tuple[int, float], ...] = ()


def reduce_auto_protect(*, direction: str, price: float, entry: float,
                        stop_loss: float, tp1: float,
                        leg1_open: bool, remaining_sls: dict,
                        trigger_fraction: Optional[float],
                        buffer_fraction: Optional[float],
                        already_triggered: bool = False) -> AutoProtectDecision:
    """Reduce the current tick to a deterministic early-protect decision.

    ``remaining_sls`` maps leg number to its current SL. Only improving levels
    are returned. No DB, broker, clock or fill side effect belongs here.
    """
    level = protect_trigger_level(
        direction=direction, entry=entry, tp1=tp1, fraction=trigger_fraction,
    )
    if (already_triggered or not leg1_open or level is None
            or not protect_trigger_hit(
                direction=direction, price=price, entry=entry, tp1=tp1,
                fraction=trigger_fraction)):
        return AutoProtectDecision(False, level, None)
    group_sl = group_protect_sl(
        direction=direction, entry=entry, stop_loss=stop_loss, tp1=tp1,
        buffer_fraction=buffer_fraction,
    )
    moves = []
    for leg, current in sorted((remaining_sls or {}).items()):
        improved = monotonic_sl(direction, current, group_sl)
        if improved is not None:
            moves.append((int(leg), improved))
    return AutoProtectDecision(True, level, group_sl, tuple(moves))


@dataclass(frozen=True)
class ProtectDecision:
    applied_sl: Optional[float]
    winner: Winner
    delta_pips: Optional[float]
    delta_seconds: Optional[float]
    leg1_action: Leg1Action
    status: Literal["ok", "auto_not_triggered"]


def _winner(direction: str, auto_sl: Optional[float],
            group_sl: Optional[float]) -> Winner:
    if auto_sl is None and group_sl is None:
        return "none"
    if auto_sl is None:
        return "group"
    if group_sl is None:
        return "auto"
    if abs(auto_sl - group_sl) <= 1e-12:
        return "tie"
    if direction == "buy":
        return "auto" if auto_sl > group_sl else "group"
    return "auto" if auto_sl < group_sl else "group"


def _seconds_between(start: Optional[str], end: str) -> Optional[float]:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
        # Keep old naive audit rows compatible with the current explicit-UTC
        # timestamps. Without normalization, a mixed pair raises TypeError.
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return (b - a).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def decide_protect(*, direction: str, auto_sl: Optional[float],
                   group_sl: Optional[float], entry: float, tp1: float,
                   price_at_event: Optional[float], leg1_open: bool,
                   auto_protection_at: Optional[str],
                   group_received_at: str, pip_size: float) -> ProtectDecision:
    """Choose the more protective SL and conservatively gate leg-1 closure."""
    applied = more_protective_sl(direction, auto_sl, group_sl)
    winner = _winner(direction, auto_sl, group_sl)
    delta_pips = (
        abs(group_sl - auto_sl) / pip_size
        if auto_sl is not None and group_sl is not None and pip_size
        else None
    )
    if not leg1_open:
        leg1_action: Leg1Action = "already_closed"
    else:
        reached_tp1 = False
        if price_at_event is not None and tp1:
            reached_tp1 = (
                price_at_event >= tp1
                if direction == "buy"
                else price_at_event <= tp1
            )
        # Closing leg 1 at the message price must not undo a mechanical
        # protection that is already ahead.  The group may close it only when
        # its protect level wins (or ties) and TP1 is known at the event.
        can_group_close = winner in ("group", "tie")
        leg1_action = "closed" if reached_tp1 and can_group_close else "skipped"
    return ProtectDecision(
        applied_sl=applied,
        winner=winner,
        delta_pips=round(delta_pips, 4) if delta_pips is not None else None,
        delta_seconds=_seconds_between(auto_protection_at, group_received_at),
        leg1_action=leg1_action,
        status="ok" if auto_protection_at else "auto_not_triggered",
    )
