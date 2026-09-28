"""Deterministic execution journal primitives.

The journal is deliberately transport-agnostic: production may persist events
in SQLite, while a study can pass the same records to ``replay_events``. It is
opt-in so the normal one-second runtime does not grow the database until the
operator enables evidence capture.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class ExecutionEvent:
    """One observed input or execution result in chronological order."""

    at: str
    kind: str
    engine: str = ""
    symbol: str = ""
    trade_id: str = ""
    signal_id: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "kind": self.kind,
            "engine": self.engine,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "signal_id": self.signal_id,
            "payload": dict(self.payload),
        }


def replay_events(events: Iterable[Mapping[str, object]],
                  handlers: Mapping[str, Callable[[ExecutionEvent], None]]) -> int:
    """Replay events in timestamp/order order into pure study handlers.

    Unknown event kinds are ignored rather than making an old journal
    impossible to read. Handlers receive immutable ``ExecutionEvent`` values
    and therefore cannot mutate the journal or production database.
    """
    normalized: List[ExecutionEvent] = []
    for row in events:
        if not isinstance(row, Mapping):
            continue
        normalized.append(ExecutionEvent(
            at=str(row.get("at") or ""),
            kind=str(row.get("kind") or ""),
            engine=str(row.get("engine") or ""),
            symbol=str(row.get("symbol") or ""),
            trade_id=str(row.get("trade_id") or ""),
            signal_id=str(row.get("signal_id") or ""),
            payload=dict(row.get("payload") or {}),
        ))
    normalized.sort(key=lambda event: (event.at, event.kind, event.trade_id))
    count = 0
    for event in normalized:
        handler = handlers.get(event.kind)
        if handler is None:
            continue
        handler(event)
        count += 1
    return count
