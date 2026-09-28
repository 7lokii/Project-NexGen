import asyncio

import pytest

from app.execution.feed import MT5_PRIORITY_SYMBOLS, MarketFeed


class FakeProvider:
    def __init__(self, quotes=None, fail=False):
        self.quotes = quotes or {}
        self.fail = fail
        self.calls = 0

    async def fetch(self, symbols):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


class FakeMt5Source:
    """Connected MT5 provider shape: .connected + .tick(app_symbol)."""

    def __init__(self, ticks=None, connected=True, fail=False):
        self.ticks = ticks or {}
        self.connected = connected
        self.fail = fail
        self.calls = []

    def tick(self, app_symbol):
        self.calls.append(app_symbol)
        if self.fail:
            raise RuntimeError("terminal gone")
        return self.ticks.get(app_symbol)


def test_sim_mode_walks_prices():
    feed = MarketFeed(provider=None, seed=1)
    feed.ensure("EURUSD", 1.0850)
    asyncio.run(feed.refresh(5))
    assert feed.price("EURUSD") != 1.0850  # random walk moved
    assert feed.meta()["EURUSD"]["source"] == "sim"
    assert not feed.real_data_active


def test_sim_mode_frozen_at_speed_zero():
    feed = MarketFeed(provider=None, seed=1)
    feed.ensure("EURUSD", 1.0850)
    asyncio.run(feed.refresh(0))
    assert feed.price("EURUSD") == 1.0850


def test_real_mode_uses_provider_prices():
    async def scenario():
        feed = MarketFeed(provider=FakeProvider(quotes={"EURUSD": 1.0900}),
                          refresh_seconds=0, seed=1)
        feed.ensure("EURUSD", 1.0850)
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    assert feed.price("EURUSD") == 1.0900
    assert feed.meta()["EURUSD"]["source"] == "real"
    assert feed.real_data_active


def test_real_mode_activates_without_open_trades():
    """A configured provider must price the default watchlist on refresh,
    even when no trade has opened yet (fresh start)."""
    quotes = {s: 1.0 for s in ["XAUUSD", "EURUSD", "GBPUSD"]}

    async def scenario():
        feed = MarketFeed(provider=FakeProvider(quotes=quotes),
                          refresh_seconds=0, seed=1)
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 1.0
    assert feed.meta()["XAUUSD"]["source"] == "real"
    assert feed.real_data_active


def test_holds_last_price_when_provider_fails():
    async def scenario():
        feed = MarketFeed(provider=FakeProvider(fail=True), refresh_seconds=0, seed=1)
        feed.ensure("EURUSD", 1.0850)
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    # real price is never fabricated: the last known price is held
    assert feed.price("EURUSD") == 1.0850
    assert feed.meta()["EURUSD"]["source"] == "sim"


# ------------------------------------------------------------------------
# MT5 spot priority for the futures-served metals (GC=F/SI=F basis offset).


def test_mt5_spot_wins_over_web_provider_for_metals():
    """With a connected terminal, XAUUSD is priced from MT5 spot even though
    the web provider serves the futures price — the basis offset (~$35 on
    gold) must never leak into the paper race."""

    async def scenario():
        feed = MarketFeed(
            provider=FakeProvider(quotes={"XAUUSD": 4310.0, "EURUSD": 1.0900}),
            refresh_seconds=0, seed=1)
        mt5 = FakeMt5Source(ticks={"XAUUSD": 4275.7})
        feed.set_mt5_sources([mt5])
        await feed.refresh(5)
        return feed, mt5

    feed, mt5 = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 4275.7          # spot, not GC=F 4310
    assert feed.meta()["XAUUSD"]["source"] == "mt5"
    assert feed.price("EURUSD") == 1.0900          # forex stays on the web feed
    assert feed.meta()["EURUSD"]["source"] == "real"
    assert "XAUUSD" in mt5.calls                   # metals go through MT5
    assert "EURUSD" not in mt5.calls               # forex never costs an MT5 call
    assert feed.real_data_active


def test_mt5_spot_skipped_when_terminal_offline():
    """Failsafe: without a connected terminal the web provider keeps serving
    the metals (the pre-fix behaviour), prices never stall."""

    async def scenario():
        feed = MarketFeed(
            provider=FakeProvider(quotes={"XAUUSD": 4310.0}),
            refresh_seconds=0, seed=1)
        feed.set_mt5_sources([FakeMt5Source(ticks={"XAUUSD": 4275.7}, connected=False)])
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 4310.0
    assert feed.meta()["XAUUSD"]["source"] == "real"


def test_mt5_spot_error_falls_back_to_web_provider():
    """A dead terminal raising inside tick() must never take the feed down."""

    async def scenario():
        feed = MarketFeed(
            provider=FakeProvider(quotes={"XAUUSD": 4310.0}),
            refresh_seconds=0, seed=1)
        feed.set_mt5_sources([FakeMt5Source(fail=True)])
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 4310.0
    assert feed.meta()["XAUUSD"]["source"] == "real"


def test_mt5_first_connected_source_with_tick_wins():
    """Main provider first; the lane 2 worker is only asked for symbols the
    main provider could not price."""

    async def scenario():
        feed = MarketFeed(
            provider=FakeProvider(quotes={}), refresh_seconds=0, seed=1)
        main = FakeMt5Source(ticks={"XAUUSD": 4275.7})
        worker = FakeMt5Source(ticks={"XAUUSD": 9999.0, "XAGUSD": 63.4})
        feed.set_mt5_sources([main, worker])
        await feed.refresh(5)
        return feed, main, worker

    feed, main, worker = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 4275.7
    assert feed.price("XAGUSD") == 63.4
    assert main.calls == ["XAUUSD", "XAGUSD"]
    assert worker.calls == ["XAGUSD"]


def test_no_mt5_sources_no_crash():
    """set_mt5_sources never called (sim mode / old wiring): unchanged."""

    async def scenario():
        feed = MarketFeed(
            provider=FakeProvider(quotes={"XAUUSD": 4310.0}),
            refresh_seconds=0, seed=1)
        await feed.refresh(5)
        return feed

    feed = asyncio.run(scenario())
    assert feed.price("XAUUSD") == 4310.0


def test_mt5_sources_none_entries_filtered():
    feed = MarketFeed(provider=None, seed=1)
    feed.set_mt5_sources([None, FakeMt5Source(ticks={"XAUUSD": 4275.7})])
    assert len(feed._mt5_sources) == 1


def test_pricing_basics_of_the_phantom_close_regression():
    """The 14/09 incident: buy 4275 spot, TP2 4285, while the feed said 4310.
    Spot pricing keeps the entry inside the TP range; the futures feed made
    the sim think the trade was +$35 in profit and closed it instantly."""
    signal = {"entry": 4275.0, "tp1": 4280.0, "tp2": 4285.0}
    assert MT5_PRIORITY_SYMBOLS == ("XAUUSD", "XAGUSD")
    # with spot pricing, entry < tp1 < tp2 -> trade cannot resolve instantly
    assert signal["entry"] < signal["tp1"] < signal["tp2"]
