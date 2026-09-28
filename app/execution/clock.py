"""Clock adapters used by execution engines.

Production uses :class:`SystemClock`; studies and engine tests can inject
:class:`ManualClock` so time stops, timestamps and polling decisions do not
depend on how fast the test process happens to run.
"""

from datetime import datetime, timezone
import time
from typing import Optional, Protocol


class Clock(Protocol):
    """Minimal clock contract required by the execution layer."""

    def epoch(self) -> float:
        ...

    def monotonic(self) -> float:
        ...

    def iso(self) -> str:
        ...


class SystemClock:
    """UTC wall clock plus the process monotonic clock."""

    def epoch(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ManualClock:
    """Deterministic clock for tests and synthetic paper studies.

    ``advance`` moves both wall and monotonic time by the same amount. The
    class deliberately does not monkey-patch global time functions.
    """

    def __init__(self, epoch: float, monotonic: Optional[float] = None):
        self._epoch = float(epoch)
        self._monotonic = float(epoch if monotonic is None else monotonic)

    def epoch(self) -> float:
        return self._epoch

    def monotonic(self) -> float:
        return self._monotonic

    def iso(self) -> str:
        return datetime.fromtimestamp(
            self._epoch, tz=timezone.utc,
        ).isoformat(timespec="seconds")

    def set(self, at: str) -> None:
        """Set wall and monotonic time to an ISO timestamp for a replay."""
        value = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._epoch = value.timestamp()
        self._monotonic = self._epoch

    def advance(self, seconds: float) -> None:
        seconds = float(seconds)
        self._epoch += seconds
        self._monotonic += seconds
