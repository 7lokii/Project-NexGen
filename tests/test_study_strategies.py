from datetime import datetime, timezone

from app.backtest import simulate_signal, simulate_signal_managed
from app.execution.clock import ManualClock
from app.models import Signal
from app.study_strategies import management_fallback_study, polling_sensitivity


def _signal():
    return Signal(
        id="study-signal", source="test", raw_text="",
        symbol="XAUUSD", direction="buy", entry=4400.0,
        stop_loss=4396.0, take_profit_1=4402.0,
        take_profit_2=4404.0, take_profit_3=4406.0,
        take_profit_4=4408.0, take_profit_5=4410.0,
        status="historical", received_at="2026-08-19T10:00:00+00:00",
    )


def _candles(prices):
    base = int(datetime(2026, 8, 19, 10, 0,
                       tzinfo=timezone.utc).timestamp())
    return [
        {"ts": base + i * 60, "open": p, "high": h,
         "low": l, "close": p}
        for i, (p, h, l) in enumerate(prices)
    ]


def test_leg_weights_keep_total_risk_and_change_allocation():
    signal = _signal()
    candles = _candles([
        (4400.0, 4402.1, 4400.0),
        (4402.0, 4406.1, 4401.9),
        (4406.0, 4406.0, 4406.0),
    ])
    equal = simulate_signal(
        signal, candles, 1.0, 10000.0, delay_seconds=0,
        spread_pips=0, slippage_pips=0, commission_per_lot=0,
        leg_weights=None,
    )
    long_legs = simulate_signal(
        signal, candles, 1.0, 10000.0, delay_seconds=0,
        spread_pips=0, slippage_pips=0, commission_per_lot=0,
        leg_weights={1: 0.5, 2: 0.5, 3: 1.0, 4: 1.0, 5: 1.0},
    )
    assert equal["pnl"] != long_legs["pnl"]
    assert set(equal["legs_pnl"]) == set(long_legs["legs_pnl"])


def test_management_fallback_protects_after_tp1_without_group_message():
    signal = _signal()
    candles = _candles([
        (4400.0, 4402.1, 4401.5),
        (4402.0, 4402.0, 4401.0),
        (4401.0, 4401.0, 4400.3),
    ])
    result = simulate_signal_managed(
        signal, candles, [], 1.0, 1000.0, delay_seconds=0,
        spread_pips=0, slippage_pips=0, commission_per_lot=0,
        max_legs=3, management_fallback_seconds=60,
    )
    assert result["management_fallback_applied"] is True
    assert "management_fallback" in result["mgmt_applied"]
    assert result["outcome"] in ("be", "loss", "win")
    assert result["pnl"] > 0


def test_fallback_study_recommendation_is_informational():
    assert management_fallback_study.__doc__


def test_polling_sensitivity_is_isolated_and_reports_all_rates():
    report = polling_sensitivity()
    assert report["ok"] is True
    assert report["intervals_seconds"] == [1.0, 0.25, 0.1]
    assert report["reference_seconds"] == 0.1
    assert set(report["results"]) == {"1.0", "0.25", "0.1"}
    assert set(report["results"]["1.0"]) == {"auto_escada_3", "auto_escada_5"}


def test_manual_clock_advances_wall_and_monotonic_time_together():
    clock = ManualClock(1_000.0)
    assert clock.epoch() == 1_000.0
    first = clock.iso()
    clock.advance(60)
    assert clock.epoch() == 1_060.0
    assert clock.monotonic() == 1_060.0
    assert clock.iso() != first
