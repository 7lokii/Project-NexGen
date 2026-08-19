"""Real market feed with a simulated fallback.

Replaces the pure random walk: prices now come from a real provider when
available, so the paper-trading simulation reflects true market prices.

Providers:
  * YahooProvider      — free, no API key (default). Works for 6-letter forex
                         pairs (EURUSD, GBPUSD, XAUUSD, ...).
  * TwelveDataProvider — free tier, needs MARKET_API_KEY (more reliable).

Design:
  * `refresh()` fetches fresh quotes every `refresh_seconds` (async). With a
    provider configured it prices a default watchlist from the start, so real
    data is active even before the first trade opens.
  * If the provider is unreachable, the last known real prices are held
    (never fabricated); with no provider at all, the random walk runs.
  * Each symbol tracks its source ("real" vs "sim") so the dashboard can show
    whether prices are real or simulated.
"""

import os
import time

import httpx

from app.execution.market import MarketSimulator

DEFAULT_REFRESH_SECONDS = 10.0

# Symbols the feed always prices when a real provider is configured, so the
# market activates on startup even before the first trade opens (and the
# dashboard badge shows "preços reais"). XAUUSD/XAGUSD map to the futures.
DEFAULT_SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY",
                   "AUDUSD", "USDCAD", "NZDUSD")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _now_ts() -> float:
    return time.time()


def yahoo_symbol(symbol: str) -> str:
    """Map an internal symbol to the Yahoo ticker that serves it.

    Yahoo does not serve spot XAUUSD=X / XAGUSD=X; metals must use the
    futures contracts (GC=F gold, SI=F silver). 6-letter pairs get the
    forex =X suffix; anything else is assumed to already be a ticker.
    """
    s = symbol.upper()
    if s == "XAUUSD":
        return "GC=F"
    if s == "XAGUSD":
        return "SI=F"
    if "=" in s or len(s) != 6:
        return s
    return f"{s}=X"


class YahooProvider:
    """Free quotes via Yahoo Finance (no API key)."""

    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"

    def __init__(self):
        self.changes: dict = {}  # symbol -> variação % do dia (após o último fetch)

    async def fetch(self, symbols):
        out = {}
        self.changes = {}
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            for sym in symbols:
                ticker = yahoo_symbol(sym)
                try:
                    r = await client.get(self.BASE.format(sym=ticker))
                    r.raise_for_status()
                    meta = r.json()["chart"]["result"][0]["meta"]
                    price = meta.get("regularMarketPrice")
                    if price and price > 0:
                        out[sym] = float(price)
                        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                        if prev:
                            self.changes[sym] = round((float(price) - float(prev)) / float(prev) * 100, 2)
                    else:
                        print(f"[market] sem preço para {sym}")
                except Exception as exc:
                    print(f"[market] sem preço para {sym}: {exc}")
        return out

    async def history(self, symbol: str, interval: str = "15m", range_: str = "1mo") -> list:
        """Historical OHLC candles for the backtest.

        Returns a list of dicts {ts, open, high, low, close} sorted by time,
        or raises if the symbol/range is unavailable.
        """
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(symbol)}"
            f"?interval={interval}&range={range_}"
        )
        async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()["chart"]["result"][0]

        ts = data.get("timestamp") or []
        quote = data["indicators"]["quote"][0]
        candles = []
        for i, t in enumerate(ts):
            high = quote["high"][i]
            low = quote["low"][i]
            if high is None or low is None:
                continue
            candles.append({
                "ts": int(t),
                "open": quote["open"][i] or 0.0,
                "high": float(high),
                "low": float(low),
                "close": quote["close"][i] or 0.0,
            })
        return candles


class TwelveDataProvider:
    """Quotes via Twelve Data (free tier; requires an API key)."""

    URL = "https://api.twelvedata.com/price"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, symbols):
        pairs = ",".join(f"{s[:3]}/{s[3:]}" for s in symbols if len(s) == 6)
        if not pairs:
            return {}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                self.URL, params={"symbol": pairs, "apikey": self.api_key}
            )
            r.raise_for_status()
            data = r.json()
        out = {}
        for pair, value in data.items():
            if isinstance(value, dict) and value.get("price"):
                out[pair.replace("/", "")] = float(value["price"])
        return out


class MarketFeed:
    """Owns the current prices used by the paper broker."""

    def __init__(self, provider=None, refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
                 seed: int = None):
        self.provider = provider
        self.refresh_seconds = refresh_seconds
        self._sim = MarketSimulator(seed)
        self.prices: dict = {}
        self._meta: dict = {}  # symbol -> {"source": "real"|"sim", "updated": ts}
        self._last_refresh = 0.0

    # ------------------------------------------------------------- interface

    def ensure(self, symbol: str, seed_price: float) -> None:
        """Make sure a symbol has a price (called when a trade opens)."""
        if symbol not in self.prices:
            self._sim.ensure(symbol, seed_price)
            self.prices[symbol] = seed_price
            self._meta[symbol] = {"source": "sim", "updated": _now_ts()}

    def price(self, symbol: str):
        return self.prices.get(symbol)

    def meta(self) -> dict:
        return dict(self._meta)

    @property
    def real_data_active(self) -> bool:
        """True when at least one symbol has a fresh real quote (< 5 min)."""
        now = _now_ts()
        return any(
            m.get("source") == "real" and (now - m.get("updated", 0)) < 300
            for m in self._meta.values()
        )

    # -------------------------------------------------------------- refresh

    async def refresh(self, speed: float = 1) -> None:
        """Update prices: real quotes when available, otherwise hold or walk."""
        now = time.monotonic()
        if self.provider:
            if now - self._last_refresh >= self.refresh_seconds:
                self._last_refresh = now
                # With no symbols priced yet (fresh start, no open trade), fetch
                # the default watchlist so real quotes activate immediately.
                targets = list(self.prices.keys()) or list(DEFAULT_SYMBOLS)
                try:
                    quotes = await self.provider.fetch(targets)
                    if quotes:
                        changes = getattr(self.provider, "changes", {}) or {}
                        for sym, price in quotes.items():
                            self.prices[sym] = price
                            self._sim.prices[sym] = price  # keep fallback aligned
                            self._meta[sym] = {"source": "real", "updated": _now_ts(),
                                               "chg_pct": changes.get(sym)}
                        return
                except Exception as exc:
                    print(f"[market] feed indisponível ({exc}); a manter últimos preços")
            return  # provider configured: hold last real prices between fetches

        # no provider: simulated random walk (speed 0 freezes the market)
        self._sim.tick(int(speed))
        for sym in self._sim.prices:
            self.prices[sym] = self._sim.prices[sym]
            self._meta[sym] = {"source": "sim", "updated": _now_ts()}


def build_market_feed() -> MarketFeed:
    """Build the feed from environment variables (see .env / docs)."""
    provider_name = os.getenv("MARKET_PROVIDER", "yahoo").strip().lower()
    refresh = float(os.getenv("MARKET_REFRESH_SECONDS", str(DEFAULT_REFRESH_SECONDS)))

    if provider_name == "twelvedata":
        api_key = os.getenv("MARKET_API_KEY", "")
        provider = TwelveDataProvider(api_key) if api_key else None
    elif provider_name == "yahoo":
        provider = YahooProvider()
    else:  # "none" -> simulation only
        provider = None

    return MarketFeed(provider=provider, refresh_seconds=refresh)
