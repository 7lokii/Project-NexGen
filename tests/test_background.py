"""Background lifecycle tests.

These tests deliberately import only the standalone runtime, not ``app.main``.
That is the boundary that keeps engine/MT5 tests independent from FastAPI's
lifespan and prevents pending tasks from surviving a TestClient context.
"""

import asyncio

from app.background import BackgroundRuntime


def test_background_runtime_starts_once_and_cancels_owned_jobs():
    async def scenario():
        started = asyncio.Event()

        async def job():
            started.set()
            await asyncio.Event().wait()

        runtime = BackgroundRuntime(
            tick=job, watchdog=job, tick_interval=0.001,
            watchdog_interval=0.001,
        )
        first = runtime.start()
        second = runtime.start()
        assert first == second
        assert set(first) == {"market", "watchdog"}

        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert runtime.running is True
        await runtime.stop()
        assert runtime.running is False
        assert all(task.done() for task in first.values())

        # Shutdown is safe even when the lifespan calls it defensively twice.
        await runtime.stop()

    asyncio.run(scenario())


def test_background_runtime_records_loop_exceptions_without_stopping():
    async def scenario():
        errors = []
        calls = 0

        async def job():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("one bad tick")
            await asyncio.sleep(0.002)

        runtime = BackgroundRuntime(
            tick=job, watchdog=job, tick_interval=0.001,
            watchdog_interval=0.001,
            exception_recorder=lambda exc, context: errors.append((exc, context)),
        )
        runtime.start()
        await asyncio.sleep(0.02)
        await runtime.stop()
        assert errors
        assert any(isinstance(exc, RuntimeError) and context["phase"] == "tick"
                   for exc, context in errors)
        assert calls > 1

    asyncio.run(scenario())
