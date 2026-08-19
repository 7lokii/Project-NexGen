"""Simulated market feed.

In paper-trading mode there is no real broker, so prices have to come from
somewhere. This is a simple geometric random walk per symbol: good enough to
see the strategy (TP1/TP2/breakeven) resolve, clearly not a substitute for
real market data.

The `sim_speed` config multiplies how many steps each tick performs, so the
demo can be fast-forwarded. Setting it to 0 freezes the market.
"""

import math
import random
from typing import Dict, Optional

# Volatility per step. 0.0004 is ~4 pips per step on a 1.0 price, a reasonable
# "demo pace" for a 1-second tick.
SIGMA = 0.0004


class MarketSimulator:
    def __init__(self, seed: Optional[int] = None):
        self.prices: Dict[str, float] = {}
        self._rng = random.Random(seed)

    def ensure(self, symbol: str, seed_price: float) -> None:
        """Make sure a symbol has a price, seeding it if unknown."""
        if symbol not in self.prices:
            self.prices[symbol] = seed_price

    def price(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol)

    def tick(self, steps: int = 1) -> None:
        """Advance the market `steps` times (geometric random walk)."""
        for _ in range(int(steps)):
            for symbol in list(self.prices):
                shock = math.exp(SIGMA * self._rng.gauss(0.0, 1.0))
                self.prices[symbol] = self.prices[symbol] * shock
