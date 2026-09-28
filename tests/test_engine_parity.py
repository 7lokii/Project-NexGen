"""Cross-engine execution parity.

The three adapters have different persistence/broker boundaries, but a simple
price path must still produce the same observable trading decision.  This
suite feeds the identical event prices to the backtest, PaperRace and MT5
dry-run adapter and compares the applied SL, exit reason and P&L.
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

from app.backtest import simulate_signal
from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.execution.mt5 import FALLBACK_SYMBOLS, MT5Engine, MT5Provider
from app.execution.parity import position_observation
from app.execution.paper import PaperRace
from app.models import Signal


class _ParityProvider(MT5Provider):
    """Connected provider with a single deterministic tick stream."""

    def __init__(self, price: float):
        super().__init__(symbols={s: dict(m) for s, m in FALLBACK_SYMBOLS.items()})
        self._fake_ticks = {"XAUUSD": price}

    @property
    def connected(self):
        return True

    def tick(self, app_symbol):
        return self._fake_ticks.get(app_symbol)

    def meta(self, app_symbol):
        return dict(self._symbols[app_symbol]) if app_symbol in self._symbols else None


def _signal(direction: str, entry: float, stop: float,
           tp1: float, tp2: float, tp3: float) -> Signal:
    return Signal(
        id=f"parity-{direction}", source="test", raw_text="",
        symbol="XAUUSD", direction=direction, entry=entry,
        stop_loss=stop, take_profit_1=tp1, take_profit_2=tp2,
        take_profit_3=tp3, status="pending",
        received_at="2026-08-19T10:00:00+00:00",
    )


def _snapshot(db: DB, trade_id: str, *, paper: bool = False) -> dict:
    if paper:
        positions = db.paper_positions_for_trade(trade_id)
        trade = next((row for row in db.paper_trades("nova", 100)
                      if row["id"] == trade_id), None)
        reason = trade.get("exit_reason") if trade else None
    else:
        positions = db.positions_for_trade(trade_id)
        trade = db.get_trade(trade_id)
        reason = trade.exit_reason if trade else None
    return position_observation(positions, reason)


def _backtest_snapshot(result: dict, *, entry: float, stop: float) -> dict:
    """Normalize the backtest trace to the same observable contract."""
    sl = {1: stop, 2: stop, 3: stop}
    reasons = {}
    closes = {}
    import re

    for event in result.get("events", []):
        kind = event.get("kind")
        price = float(event.get("price", 0.0))
        if kind == "be":
            for leg in (2, 3):
                sl[leg] = entry
        elif kind == "leg_sl":
            match = re.search(r"perna (\d+)", event.get("detail", ""))
            if match:
                leg = int(match.group(1))
                closes[leg] = price
                reasons[leg] = "be" if abs(price - entry) < 1e-9 else "sl"
        elif kind == "tp1":
            closes[1] = price
            reasons[1] = "tp1"

    return {
        "decisions": tuple(
            (leg, "closed", round(closes[leg], 4), reasons[leg])
            for leg in (1, 2, 3)
        ),
        "applied_sl": tuple((leg, round(sl[leg], 4)) for leg in (1, 2, 3)),
        "reason": "be",
        "pnl": round(result["pnl"], 2),
    }


@pytest.mark.parametrize(
    "direction,prices,entry,stop,tp1,tp2,tp3",
    [
        ("buy", [4400.0, 4402.0, 4400.0], 4400.0, 4396.0,
         4402.0, 4404.0, 4406.0),
        ("sell", [4400.0, 4398.0, 4400.0], 4400.0, 4404.0,
         4398.0, 4396.0, 4394.0),
    ],
)
def test_same_price_events_have_same_decision_sl_reason_and_pnl(
    direction, prices, entry, stop, tp1, tp2, tp3,
):
    signal = _signal(direction, entry, stop, tp1, tp2, tp3)
    now = [
        "2026-08-19T10:01:00+00:00",
        "2026-08-19T10:02:00+00:00",
        "2026-08-19T10:03:00+00:00",
    ]
    live = []
    temp_paths = []

    # SimEngine --------------------------------------------------------------
    sim_path = tempfile.mktemp(suffix=".db")
    temp_paths.append(sim_path)
    sim_db = DB(sim_path)
    sim_market = MarketSimulator(seed=1)
    sim_market.prices["XAUUSD"] = entry
    sim = SimEngine(sim_db, sim_market)
    sim_trade = sim.open_trade(
        signal, risk_pct=1.0, balance=1000.0,
        trade_id="sim-parity", now="2026-08-19T10:00:00+00:00",
    )
    for price, timestamp in zip(prices, now):
        sim_market.prices["XAUUSD"] = price
        sim.update(now=timestamp)
    live.append(_snapshot(sim_db, sim_trade.id))

    # PaperRace --------------------------------------------------------------
    paper_path = tempfile.mktemp(suffix=".db")
    temp_paths.append(paper_path)
    paper_db = DB(paper_path)
    paper_market = MarketSimulator(seed=1)
    paper_market.prices["XAUUSD"] = entry
    paper = PaperRace(paper_db, paper_market)
    paper_trade = paper.open_for_signal(
        signal, risk_pct=1.0, strategy_ids=["nova"], daily_stop_pct=0,
    )[0]
    for price in prices:
        paper_market.prices["XAUUSD"] = price
        paper.tick(1.0)
    live.append(_snapshot(paper_db, paper_trade.id, paper=True))

    # MT5 dry-run ------------------------------------------------------------
    mt5_path = tempfile.mktemp(suffix=".db")
    temp_paths.append(mt5_path)
    mt5_db = DB(mt5_path)
    provider = _ParityProvider(entry)
    mt5_market = MarketSimulator(seed=1)
    mt5_market.prices["XAUUSD"] = entry
    mt5 = MT5Engine(mt5_db, provider, mt5_market, dry_run=True)
    mt5_trade = mt5.open_trade(
        signal, risk_pct=1.0, balance=1000.0,
        trade_id="mt5-parity", now="2026-08-19T10:00:00+00:00",
    )
    for price, timestamp in zip(prices, now):
        provider._fake_ticks["XAUUSD"] = price
        mt5.update(now=timestamp)
    live.append(_snapshot(mt5_db, mt5_trade.id))

    try:
        # Every adapter must agree with every other adapter, not just with a
        # hand-written expected P&L.
        assert live[0] == live[1] == live[2]

        # Backtest receives the very same three prices as one-point candles.
        base = int(datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc).timestamp())
        candles = [
            {"ts": base + (i + 1) * 60, "open": price,
             "high": price, "low": price, "close": price}
            for i, price in enumerate(prices)
        ]
        result = simulate_signal(
            signal, candles, risk_pct=1.0, balance=1000.0,
            delay_seconds=60, spread_pips=0.0, slippage_pips=0.0,
            commission_per_lot=0.0, max_legs=3, trace=True,
        )
        backtest = _backtest_snapshot(result, entry=entry, stop=stop)
        assert backtest == live[0]
        assert result["outcome"] == "be"
    finally:
        for db in (sim_db, paper_db, mt5_db):
            db.close()
        for path in temp_paths:
            if os.path.exists(path):
                os.remove(path)


def test_sim_engine_auto_protect_matches_shared_trigger_and_group_floor():
    path = tempfile.mktemp(suffix=".db")
    db = DB(path)
    try:
        market = MarketSimulator(seed=2)
        market.prices["XAUUSD"] = 4400.0
        engine = SimEngine(db, market)
        signal = _signal("buy", 4400.0, 4396.0, 4402.0, 4404.0, 4406.0)
        trade = engine.open_trade(
            signal, risk_pct=1.0, balance=1000.0,
            trade_id="sim-auto-protect", now="2026-08-19T10:00:00+00:00",
        )
        market.prices["XAUUSD"] = 4401.5
        events = engine.update(
            now="2026-08-19T10:01:00+00:00",
            auto={"protect_trigger": 0.75, "protect_buffer": 0.10},
        )
        positions = {p.leg: p for p in db.positions_for_trade(trade.id)}
        assert any(e.get("reason") == "auto_protect" for e in events)
        assert positions[1].status == "closed"
        assert positions[1].exit_reason == "auto_protect"
        assert positions[1].close_price == 4401.5
        assert positions[2].stop_loss == 4400.4
        assert positions[3].stop_loss == 4400.4
    finally:
        db.close()
        if os.path.exists(path):
            os.remove(path)
