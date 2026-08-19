"""Lifecycle-owned background jobs for the NexGen application.

The FastAPI module wires dependencies and callbacks; this module owns the
long-running asyncio tasks and, importantly, their cancellation.  Keeping the
runner independent from ``app.main`` lets engine tests import and exercise the
execution stack without accidentally inheriting an orphaned server loop.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional


Job = Callable[[], Awaitable[None]]
Logger = Callable[[str], None]
ExceptionRecorder = Callable[[BaseException, dict], None]


class BackgroundRuntime:
    """Run the market and watchdog jobs with an explicit application lifetime.

    ``start`` must be called from a running event loop (the FastAPI lifespan
    does this).  ``stop`` is idempotent and cancels only tasks created by this
    runtime, so a TestClient context cannot leave a market loop attached to a
    closed event loop.
    """

    def __init__(self, *, tick: Job, watchdog: Job,
                 reconnect: Optional[Job] = None,
                 tick_interval: float = 1.0,
                 watchdog_interval: float = 10.0,
                 logger: Optional[Logger] = None,
                 exception_recorder: Optional[ExceptionRecorder] = None) -> None:
        self._tick = tick
        self._watchdog = watchdog
        self._reconnect = reconnect
        self._tick_interval = max(0.0, float(tick_interval))
        self._watchdog_interval = max(0.0, float(watchdog_interval))
        self._logger = logger or (lambda _message: None)
        self._exception_recorder = exception_recorder
        self._stop: Optional[asyncio.Event] = None
        self._tasks: Dict[str, asyncio.Task] = {}

    @property
    def tasks(self) -> Dict[str, asyncio.Task]:
        """A snapshot of the tasks owned by this runtime (for diagnostics/tests)."""
        return dict(self._tasks)

    @property
    def running(self) -> bool:
        """Whether at least one owned background task is still alive."""
        return any(not task.done() for task in self._tasks.values())

    def start(self) -> Dict[str, asyncio.Task]:
        """Start exactly one market loop and one watchdog loop."""
        if self.running:
            return self.tasks
        self._stop = asyncio.Event()
        self._tasks = {
            "market": asyncio.create_task(
                self._market_loop(), name="nexgen:market_loop"
            ),
            "watchdog": asyncio.create_task(
                self._watchdog_loop(), name="nexgen:listener_watchdog"
            ),
        }
        return self.tasks

    async def stop(self) -> None:
        """Cancel and await every task created by :meth:`start`."""
        stop = self._stop
        tasks = list(self._tasks.values())
        self._tasks = {}
        if stop is not None:
            stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stop = None

    async def _wait_interval(self, seconds: float) -> bool:
        """Sleep until the next interval, returning True when stopping."""
        if self._stop is None:
            return True
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def _market_loop(self) -> None:
        while not await self._wait_interval(self._tick_interval):
            if self._reconnect is not None:
                try:
                    await self._reconnect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger(f"[mt5] reconnect error: {exc}")
                    self._record_exception(exc, {"loop": "market", "phase": "reconnect"})
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A single bad tick must not kill the 24/7 management loop.
                self._logger(f"[tick] error: {exc}")
                self._record_exception(exc, {"loop": "market", "phase": "tick"})

    async def _watchdog_loop(self) -> None:
        while not await self._wait_interval(self._watchdog_interval):
            try:
                await self._watchdog()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The watchdog must remain alive even if one health check
                # fails; the callback itself keeps its individual safeguards.
                self._logger(f"[watchdog] error: {exc}")
                self._record_exception(exc, {"loop": "watchdog", "phase": "watchdog"})

    def _record_exception(self, exc: BaseException, context: dict) -> None:
        if self._exception_recorder is None:
            return
        try:
            self._exception_recorder(exc, context)
        except Exception as recorder_exc:
            # Telemetry must never be able to kill the background loop.
            self._logger(f"[telemetry] exception recorder failed: {recorder_exc}")
