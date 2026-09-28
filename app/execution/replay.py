"""Read-only deterministic execution replay.

A replay tape is a sequence of immutable JSON-like rows:

* ``signal``: ``payload`` contains the structured Signal fields;
* ``quote``: ``symbol`` plus ``payload.price`` (or bid/ask);
* ``management``: ``signal_id`` plus ``payload.kind``/``tp``/``sl``/``be``;
* broker evidence: ``order_*``, ``modify_*``, ``fill_open`` and ``fill_close``.

The harness creates three temporary SQLite databases and feeds the same
ordered events to SimEngine, PaperRace and MT5Engine in dry-run mode. It never
opens a production DB, never calls MetaTrader5, and never changes the live
engine configuration. The report is deliberately plain JSON so studies and
API clients can store/compare it.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import tempfile
from typing import Dict, Iterable, List, Mapping, Optional

from app.db import DB
from app.execution.clock import ManualClock
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.execution.mt5 import FALLBACK_SYMBOLS, MT5Engine, MT5Provider
from app.execution.parity import position_observation
from app.execution.paper import PaperRace
from app.execution.protection import group_protect_sl
from app.models import Signal


# These events are observations from MT5, not commands for a replay adapter.
# The harness preserves them in the report so an operator can compare the
# simulated decision with the broker's actual fill/retcode without allowing a
# historical broker response to mutate the isolated study.
BROKER_EVENT_KINDS = frozenset({
    "order_request", "order_result", "modify_request", "modify_result",
    "fill_open", "fill_close",
})


@dataclass(frozen=True)
class TapeEvent:
    """Normalized tape row. ``sequence`` preserves same-timestamp order."""

    at: str
    kind: str
    sequence: int
    symbol: str = ""
    signal_id: str = ""
    payload: dict = None

    def __post_init__(self):
        if self.payload is None:
            object.__setattr__(self, "payload", {})

    def to_dict(self) -> dict:
        return asdict(self)


def _epoch(at: str) -> float:
    try:
        value = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def normalize_tape(rows: Iterable[Mapping[str, object]]) -> List[TapeEvent]:
    """Validate and stably sort public tape rows.

    Unknown kinds are retained in the normalized tape and reported as skipped;
    this makes newer journals readable by older study code.
    """
    out: List[TapeEvent] = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        sequence = row.get("sequence", row.get("id", index))
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            sequence = index
        out.append(TapeEvent(
            at=str(row.get("at") or ""),
            kind=str(row.get("kind") or "").strip().lower(),
            sequence=sequence,
            symbol=str(row.get("symbol") or payload.get("symbol") or ""),
            signal_id=str(row.get("signal_id") or payload.get("signal_id") or ""),
            payload=dict(payload),
        ))
    out.sort(key=lambda event: (_epoch(event.at), event.at, event.sequence))
    return out


def _signal_from_payload(event: TapeEvent) -> Signal:
    data = dict(event.payload)
    data.pop("symbol", None)
    data.setdefault("id", event.signal_id or data.get("signal_id") or f"replay-signal-{event.sequence}")
    data.setdefault("source", "replay")
    data.setdefault("raw_text", "")
    data.setdefault("symbol", event.symbol or "XAUUSD")
    data.setdefault("direction", "buy")
    data.setdefault("entry", 0.0)
    data.setdefault("stop_loss", 0.0)
    data.setdefault("take_profit_1", 0.0)
    data.setdefault("take_profit_2", 0.0)
    data.setdefault("take_profit_3", 0.0)
    data.setdefault("take_profit_4", 0.0)
    data.setdefault("take_profit_5", 0.0)
    data.setdefault("status", "pending")
    data.setdefault("received_at", event.at)
    data.setdefault("reply_to_msg_id", None)
    return Signal(**{key: data[key] for key in (
        "id", "source", "raw_text", "symbol", "direction", "entry",
        "stop_loss", "take_profit_1", "take_profit_2", "status",
        "received_at", "take_profit_3", "take_profit_4", "take_profit_5",
        "reply_to_msg_id",
    )})


class _TapeProvider(MT5Provider):
    """MT5 provider facade backed only by the current tape quote."""

    def __init__(self):
        super().__init__(symbols={symbol: dict(meta)
                                  for symbol, meta in FALLBACK_SYMBOLS.items()})
        self.current: Dict[str, float] = {}
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def set_price(self, symbol: str, price: float) -> None:
        self.current[symbol] = float(price)

    def meta(self, app_symbol: str) -> Optional[dict]:
        meta = self._symbols.get(app_symbol)
        return dict(meta) if meta else None

    def ensure_symbol(self, app_symbol: str) -> None:
        return None

    def tick(self, app_symbol: str) -> Optional[float]:
        return self.current.get(app_symbol)


class _EngineState:
    def __init__(self, db: DB, sim=None, paper=None, mt5=None):
        self.db, self.sim, self.paper, self.mt5 = db, sim, paper, mt5
        self.trades: Dict[str, str] = {}


def _management_sim(engine: SimEngine, signal_id: str, kind: str,
                     *, tp=None, sl=None, be=False, at="", msg_id="") -> list:
    """Apply one group instruction to the simulation adapter."""
    trade = next((row for row in engine.db.open_trades()
                  if row.signal_id == signal_id), None)
    if not trade:
        return []
    positions = engine.db.positions_for_trade(trade.id)
    open_positions = [p for p in positions if p.status == "open"]
    if kind == "tp_hit" and int(tp or 1) == 1:
        return [event for p in open_positions if p.leg != 1
                for event in [_move_sim(engine, p, trade.entry)]]
    if kind == "protect":
        events = []
        if any(p.leg == 1 and p.status == "open" for p in open_positions):
            event = engine.close_leg_at_market(trade.id, 1, now=at,
                                               reason="protect", msg_id=msg_id)
            if event:
                events.append(event)
        level = group_protect_sl(direction=trade.direction, entry=trade.entry,
                                 stop_loss=trade.stop_loss,
                                 tp1=trade.take_profit_1)
        for pos in open_positions:
            if pos.leg != 1:
                value = engine.move_leg_sl(trade.id, pos.leg, level)
                if value is not None:
                    events.append({"type": "move_sl", "trade_id": trade.id,
                                   "leg": pos.leg, "price": value,
                                   "reason": "protect"})
        return events
    if kind in ("close_tp1", "close_tp"):
        leg = 1 if kind == "close_tp1" else int(tp or 2)
        event = engine.close_leg_at_market(trade.id, leg, now=at,
                                           reason=kind, msg_id=msg_id)
        return [event] if event else []
    if kind == "move_sl" and sl is not None:
        events = []
        for pos in open_positions:
            value = engine.move_leg_sl(trade.id, pos.leg, float(sl))
            if value is not None:
                events.append({"type": "move_sl", "trade_id": trade.id,
                               "leg": pos.leg, "price": value})
        return events
    if kind == "close_all":
        if be:
            return [{"type": "move_sl", "trade_id": trade.id, "leg": pos.leg,
                     "price": engine.move_leg_sl(trade.id, pos.leg, trade.entry)}
                    for pos in open_positions if pos.leg != 1 and
                    engine.move_leg_sl(trade.id, pos.leg, trade.entry) is not None]
        return engine.close_trade_at_market(trade.id, now=at, reason="close_all",
                                            msg_id=msg_id)
    return []


def _move_sim(engine: SimEngine, pos, level: float) -> dict:
    engine.db.move_position_sl(pos.id, level)
    pos.stop_loss = level
    return {"type": "move_sl", "leg": pos.leg, "price": level}


def _management_mt5(engine: MT5Engine, signal_id: str, kind: str,
                     *, tp=None, sl=None, be=False, at="", msg_id="") -> list:
    trade = next((row for row in engine.db.open_trades()
                  if row.signal_id == signal_id), None)
    if not trade:
        return []
    positions = [p for p in engine.db.positions_for_trade(trade.id)
                 if p.status == "open"]
    if kind == "tp_hit" and int(tp or 1) == 1:
        return [event for p in positions if p.leg != 1
                for event in [_move_mt5(engine, trade, p.leg, trade.entry)]]
    if kind == "protect":
        events = []
        if any(p.leg == 1 for p in positions):
            event = engine.close_leg_at_market(trade.id, 1, now=at,
                                               reason="protect", msg_id=msg_id)
            if event:
                events.append(event)
        level = group_protect_sl(direction=trade.direction, entry=trade.entry,
                                 stop_loss=trade.stop_loss,
                                 tp1=trade.take_profit_1)
        for pos in positions:
            if pos.leg != 1:
                value = engine.move_leg_sl(trade.id, pos.leg, level)
                if value is not None:
                    events.append({"type": "move_sl", "leg": pos.leg,
                                   "price": value})
        return events
    if kind in ("close_tp1", "close_tp"):
        leg = 1 if kind == "close_tp1" else int(tp or 2)
        event = engine.close_leg_at_market(trade.id, leg, now=at,
                                           reason=kind, msg_id=msg_id)
        return [event] if event else []
    if kind == "move_sl" and sl is not None:
        events = []
        for pos in positions:
            value = engine.move_leg_sl(trade.id, pos.leg, float(sl))
            if value is not None:
                events.append({"type": "move_sl", "leg": pos.leg, "price": value})
        return events
    if kind == "close_all":
        if be:
            return [{"type": "move_sl", "leg": pos.leg,
                     "price": engine.move_leg_sl(trade.id, pos.leg, trade.entry)}
                    for pos in positions if pos.leg != 1 and
                    engine.move_leg_sl(trade.id, pos.leg, trade.entry) is not None]
        return engine.close_trade_at_market(trade.id, now=at, reason="close_all",
                                            msg_id=msg_id)
    return []


def _move_mt5(engine: MT5Engine, trade, leg: int, level: float) -> dict:
    value = engine.move_leg_sl(trade.id, leg, level)
    return {"type": "move_sl", "leg": leg, "price": value}


def _observation(db: DB, trade_id: Optional[str], paper: bool = False,
                 strategy_id: str = "nova_gestao") -> dict:
    if not trade_id:
        return {"decisions": (), "applied_sl": (), "reason": None, "pnl": 0.0}
    if paper:
        rows = db.paper_positions_for_trade(trade_id)
        trade = next((row for row in db.paper_trades(strategy_id, 100)
                      if row["id"] == trade_id), None)
        return position_observation(rows, trade.get("exit_reason") if trade else None)
    rows = db.positions_for_trade(trade_id)
    trade = db.get_trade(trade_id)
    return position_observation(rows, trade.exit_reason if trade else None)


def _report_diff(observations: dict) -> dict:
    names = list(observations)
    diffs = []
    if names:
        base = observations[names[0]]
        for name in names[1:]:
            if observations[name] != base:
                diffs.append({"engine": name, "differs_from": names[0]})
    return {"equal": not diffs, "differences": diffs}


def _replay_snapshot(states: list, event: TapeEvent, step: int,
                     strategy_id: str) -> dict:
    """Capture the smallest useful state after one tape event.

    Snapshots are intentionally derived from each temporary DB instead of
    exposing adapter internals. This makes the step-by-step report useful for
    finding the first divergence while keeping the live DB and broker outside
    the replay process.
    """
    engines = {}
    for name, state in zip(("sim", "paper", "mt5"), states):
        if name == "paper":
            trades = state.db.paper_trades(strategy_id, 100, origin="race")
            positions = []
            for trade in trades:
                positions.extend(state.db.paper_positions_for_trade(trade["id"]))
            trade_rows = [
                {"id": t.get("id"), "signal_id": t.get("signal_id"),
                 "status": t.get("status"), "pnl": round(float(t.get("pnl") or 0), 2),
                 "exit_reason": t.get("exit_reason")}
                for t in trades
            ]
        else:
            trades = state.db.list_trades(100)
            positions = [
                position for trade in trades
                for position in state.db.positions_for_trade(trade.id)
            ]
            trade_rows = [
                {"id": t.id, "signal_id": t.signal_id, "status": t.status,
                 "pnl": round(float(t.pnl or 0), 2),
                 "exit_reason": t.exit_reason}
                for t in trades
            ]
        engines[name] = {
            "trades": trade_rows,
            "positions": [
                {"trade_id": p.trade_id, "leg": p.leg, "status": p.status,
                 "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                 "close_price": p.close_price, "pnl": round(float(p.pnl or 0), 2),
                 "exit_reason": p.exit_reason}
                for p in positions
            ],
            "aggregate_pnl": round(sum(
                float(t.get("pnl") or 0) if isinstance(t, dict) else float(t.pnl or 0)
                for t in trades
                if (t.get("status") if isinstance(t, dict) else t.status) == "closed"
            ), 2),
        }
    return {
        "step": step,
        "at": event.at,
        "sequence": event.sequence,
        "kind": event.kind,
        "signal_id": event.signal_id,
        "symbol": event.symbol,
        "engines": engines,
    }


def run_replay(rows: Iterable[Mapping[str, object]], *, risk_pct: float = 1.0,
               balance: float = 1000.0, strategy_id: str = "nova_gestao",
               include_snapshots: bool = True) -> dict:
    """Run a tape through all three adapters and return a deterministic report."""
    tape = normalize_tape(rows)
    if strategy_id not in {"nova_gestao", "nova"}:
        raise ValueError("o replay comum suporta nova_gestao ou nova")
    tmp = tempfile.TemporaryDirectory(prefix="nexgen-replay-")
    dbs = [DB(f"{tmp.name}/{name}.db") for name in ("sim", "paper", "mt5")]
    clock = ManualClock(_epoch(tape[0].at) if tape else 0.0)
    markets = [MarketSimulator(seed=1) for _ in dbs]
    provider = _TapeProvider()
    sim = SimEngine(dbs[0], markets[0], clock=clock)
    paper = PaperRace(dbs[1], markets[1], clock=clock)
    mt5 = MT5Engine(dbs[2], provider, markets[2], dry_run=True, clock=clock,
                    logger=lambda _message: None)
    states = [_EngineState(dbs[0], sim=sim),
              _EngineState(dbs[1], paper=paper),
              _EngineState(dbs[2], mt5=mt5)]
    pending: List[TapeEvent] = []
    applied_events: List[dict] = []
    snapshots: List[dict] = []
    skipped = 0
    broker_observations: List[dict] = []

    def apply_management(event: TapeEvent, signal_id: str) -> None:
        nonlocal skipped
        payload = event.payload
        kind = str(payload.get("kind") or "")
        if not kind:
            skipped += 1
            return
        args = dict(tp=payload.get("tp"), sl=payload.get("sl"),
                    be=bool(payload.get("be")), at=event.at,
                    msg_id=str(payload.get("message_id") or ""))
        sim_events = (
            _management_sim(sim, signal_id, kind, **args)
            if strategy_id == "nova_gestao" else []
        )
        paper_events = (
            paper.apply_management(signal_id, kind, origin="race",
                                   **{k: v for k, v in args.items()
                                      if k in ("tp", "sl", "be", "msg_id")})
            if strategy_id == "nova_gestao" else []
        )
        mt5_events = (
            _management_mt5(mt5, signal_id, kind, **args)
            if strategy_id == "nova_gestao" else []
        )
        applied_events.append({"at": event.at, "kind": "management",
                               "management_kind": kind, "signal_id": signal_id,
                               "engines": {"sim": sim_events, "paper": paper_events,
                                           "mt5": mt5_events}})

    try:
        for event in tape:
            clock.set(event.at)
            if event.kind == "signal":
                signal = _signal_from_payload(event)
                for state in states:
                    if state.sim:
                        state.trades[signal.id] = state.sim.open_trade(
                            signal, risk_pct, balance,
                            trade_id=f"replay-{signal.id}", now=event.at,
                        ).id
                    elif state.paper:
                        trades = state.paper.open_for_signal(
                            signal, risk_pct, strategy_ids=[strategy_id],
                            daily_stop_pct=0, origin="race",
                        )
                        if trades:
                            state.trades[signal.id] = trades[0].id
                    else:
                        state.trades[signal.id] = state.mt5.open_trade(
                            signal, risk_pct, balance,
                            trade_id=f"replay-{signal.id}", now=event.at,
                        ).id
                for pending_event in list(pending):
                    if pending_event.signal_id == signal.id:
                        apply_management(pending_event, signal.id)
                        pending.remove(pending_event)
                applied_events.append({"at": event.at, "kind": "signal",
                                       "signal_id": signal.id})
            elif event.kind == "quote":
                price = event.payload.get("price")
                if price is None and event.payload.get("bid") is not None:
                    price = (float(event.payload["bid"]) + float(event.payload["ask"])) / 2
                if price is None:
                    skipped += 1
                    continue
                price = float(price)
                symbol = event.symbol or "XAUUSD"
                for market in markets:
                    market.prices[symbol] = price
                provider.set_price(symbol, price)
                # Every adapter receives the quote before evaluating it.
                sim.update(now=event.at)
                paper.tick(risk_pct, now=event.at)
                mt5.update(now=event.at)
                applied_events.append({"at": event.at, "kind": "quote",
                                       "symbol": symbol, "price": price})
            elif event.kind == "management":
                signal_id = event.signal_id or str(event.payload.get("signal_id") or "")
                if not signal_id:
                    skipped += 1
                elif signal_id not in states[0].trades:
                    pending.append(event)
                else:
                    apply_management(event, signal_id)
            elif event.kind in BROKER_EVENT_KINDS:
                # Passive evidence: broker results are compared after the
                # replay, never treated as permission to alter the simulated
                # trade or to call a broker.
                observation = {
                    "at": event.at, "kind": event.kind,
                    "sequence": event.sequence, "symbol": event.symbol,
                    "signal_id": event.signal_id,
                    "trade_id": str(event.payload.get("trade_id") or ""),
                    "payload": dict(event.payload),
                }
                broker_observations.append(observation)
                applied_events.append({"at": event.at, "kind": "broker",
                                       "broker_kind": event.kind,
                                       "signal_id": event.signal_id,
                                       "payload": dict(event.payload)})
            else:
                skipped += 1
            if include_snapshots:
                snapshots.append(_replay_snapshot(
                    states, event, len(snapshots), strategy_id,
                ))
    finally:
        observations = {}
        by_signal = {}
        for signal_id in states[0].trades:
            per_engine = {}
            for name, state in zip(("sim", "paper", "mt5"), states):
                per_engine[name] = _observation(
                    state.db, state.trades.get(signal_id),
                    paper=(name == "paper"), strategy_id=strategy_id,
                )
            by_signal[signal_id] = per_engine
        for name, state in zip(("sim", "paper", "mt5"), states):
            observations[name] = {
                "signals": {sid: report[name] for sid, report in by_signal.items()},
                "aggregate_pnl": round(sum(
                    report[name]["pnl"] for report in by_signal.values()
                ), 2),
            }
        for db in dbs:
            db.close()
        tmp.cleanup()

    return {
        "ok": True,
        "tape": {"events": len(tape), "skipped": skipped,
                 "pending_management": len(pending)},
        "engines": observations,
        "parity": _report_diff({
            name: {"signals": value["signals"]}
            for name, value in observations.items()
        }),
        "by_signal": {
            signal_id: {
                "engines": report,
                "parity": _report_diff(report),
            }
            for signal_id, report in by_signal.items()
        },
        "applied_events": applied_events,
        "broker_observations": broker_observations,
        "snapshots": snapshots if include_snapshots else [],
        "contract": {
            "events": ["signal", "quote", "management", *sorted(BROKER_EVENT_KINDS)],
            "ordering": "UTC at + sequence",
            "broker_events": "passive evidence; never drives replay decisions",
            "mode": "read-only temporary DB + MT5 dry-run",
        },
    }
