import asyncio

from app.execution.feed import MarketFeed


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
