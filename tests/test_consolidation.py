"""Tests for the reproducible auto_escada_5 defensive-mode sweep."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.backtest import _candle_atr
from app.study_consolidation import (
    _atr_floor_before_signal,
    _daily_atrs,
    _passes_entry_filter,
    _utc_hour,
    evaluate_defensive_sweep,
    run_defensive_sweep,
)


def test_utc_hour_normalizes_offsets_and_wraps_naive_local_midnight():
    assert _utc_hour("2026-08-19T01:30:00+02:00") == 23
    assert _utc_hour("2026-08-19T12:30:00-05:00") == 17
    assert _utc_hour("2026-08-19T00:30:00+00:00") == 0
    assert _utc_hour("2026-08-19T00:30:00") == 23
    assert _utc_hour("2026-08-19") is None


def test_daily_atrs_uses_one_last_observation_per_utc_day():
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(48):
        ts = int((base + timedelta(hours=i)).timestamp())
        candles.append({
            "ts": ts, "open": 100.0, "high": 101.0 + (i % 5),
            "low": 99.0 - (i % 3), "close": 100.0,
        })

    values = _daily_atrs(candles)
    assert len(values) == 2
    assert values == [
        _candle_atr(candles, 23, 14),
        _candle_atr(candles, 47, 14),
    ]


def test_entry_atr_floor_uses_only_prior_days_and_last_completed_candle():
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(72):
        ts = int((base + timedelta(hours=i)).timestamp())
        candles.append({
            "ts": ts, "open": 100.0, "high": 101.0 + (i % 7),
            "low": 99.0 - (i % 5), "close": 100.0,
        })
    signal = SimpleNamespace(
        received_at="2026-08-03T10:00:00+00:00", symbol="XAUUSD",
        entry=100.0, take_profit_2=100.2,
    )
    floor = _atr_floor_before_signal(signal, candles)
    assert floor == _daily_atrs(candles[:58])[0]
    # The ATR from the signal's current day is not allowed to set the floor.
    assert floor != _daily_atrs(candles)[-1]
    # The current pre-entry ATR is deliberately just below this floor; the
    # filter must reject it. The old i-1 call incorrectly used the prior ATR
    # and would have accepted the same signal.
    assert _passes_entry_filter(signal, candles, 6.9) == (False, "atr_baixo")


def test_item2_sweep_runs_the_complete_grid_without_market_data():
    report = run_defensive_sweep(
        {"atual": ([], {}), "tudo": ([], {}), "antiga": ([], {})},
        sid="auto_escada_5", risk=1.0, balance=1000.0, daily_stop=0,
    )

    assert len(report["rows"]) == 27  # 3 windows × 3 trails × 3 time stops
    assert {row["def_window"] for row in report["rows"]} == {5, 7, 10}
    assert {row["def_trail"] for row in report["rows"]} == {1.0, 1.2, 1.5}
    assert {row["def_time_stop"] for row in report["rows"]} == {30, 45, 60}
    assert all(set(row["windows"]) == {"atual", "tudo", "antiga"}
               for row in report["rows"])
    assert report["eligible_count"] == 0
    assert report["verdict"] == (
        "não alterar o default (DD antiga >= 50% ou PF atual caiu mais de 15%)"
    )


def test_item2_gate_requires_both_old_drawdown_and_current_pf():
    baseline = {
        "atual": {"pf": 2.0}, "tudo": {"pf": 1.5}, "antiga": {"pf": 1.0},
    }
    def row(window, trail, stop, current_pf, old_dd):
        return {
            "def_window": window, "def_trail": trail,
            "def_time_stop": stop,
            "windows": {
                "atual": {"pf": current_pf, "dd_pct": 10.0},
                "tudo": {"pf": 1.0, "dd_pct": 20.0},
                "antiga": {"pf": 1.0, "dd_pct": old_dd},
            },
        }

    report = evaluate_defensive_sweep([
        row(5, 1.0, 30, 1.70, 49.9),  # exactly the 85% PF floor: eligible
        row(7, 1.2, 45, 1.69, 20.0),  # PF sacrifice > 15%: reject
        row(10, 1.5, 60, 1.90, 50.0),  # DD threshold is strict: reject
    ], baseline)

    assert report["eligible_count"] == 1
    assert report["eligible"][0]["def_window"] == 5
    assert report["eligible"][0]["old_dd_pct"] == 49.9
    assert report["verdict"] == "alterar_default"


def test_item2_monotonicity_is_reported_per_axis_and_window():
    baseline = {name: {"pf": 2.0} for name in ("atual", "tudo", "antiga")}
    rows = []
    for window in (5, 7, 10):
        rows.append({
            "def_window": window, "def_trail": 1.0,
            "def_time_stop": 30,
            "windows": {
                "atual": {"pf": 2.0, "dd_pct": 20.0},
                "tudo": {"pf": 2.0, "dd_pct": 30.0},
                "antiga": {"pf": 2.0, "dd_pct": 40.0},
            },
        })
    report = evaluate_defensive_sweep(rows, baseline)

    for axis in ("def_window", "def_trail", "def_time_stop"):
        for window in ("atual", "tudo", "antiga"):
            checks = report["monotonicity"][axis][window]
            assert checks["pf_no_isolated_peak"] is True
            assert checks["dd_no_isolated_valley"] is True


def _confirm_signal():
    from app.models import Signal

    return Signal(
        id="confirm-1", source="tg:Test", raw_text="", symbol="EURUSD",
        direction="buy", entry=1.0850, stop_loss=1.0820,
        take_profit_1=1.0890, take_profit_2=1.0920,
        take_profit_3=1.0960, status="historical",
        received_at="2026-08-17T10:00:00",
    )


def _confirm_candles():
    return [
        {"ts": 1786960860, "open": 1.0895, "high": 1.0900,
         "low": 1.0892, "close": 1.0895},  # TP1; stair floor = 1.0890
        {"ts": 1786961760, "open": 1.0890, "high": 1.0900,
         "low": 1.0880, "close": 1.0885},  # leg 2 SL; leg 3 confirms
        {"ts": 1786962660, "open": 1.0900, "high": 1.0960,
         "low": 1.0895, "close": 1.0950},  # only the unconfirmed leg 3 survives
    ]


def test_item3_sweep_changes_only_confirm_filter_and_uses_all_windows():
    from app.study_consolidation import run_confirm_sweep

    signal = _confirm_signal()
    candles = {"EURUSD": _confirm_candles()}
    report = run_confirm_sweep(
        {name: ([signal], candles) for name in ("atual", "tudo", "antiga")},
        risk=1.0, balance=10_000.0, daily_stop=0,
    )

    assert set(report["variants"]) == {"confirm_false", "confirm_true"}
    assert all(set(report["variants"][variant]) == {"atual", "tudo", "antiga"}
               for variant in report["variants"])
    assert report["fixed"]["stair_levels"] == 3
    assert report["fixed"]["stair_from"] == 1
    assert report["fixed"]["trail_factor"] == 2.0
    plain = report["variants"]["confirm_false"]["atual"]
    confirmed = report["variants"]["confirm_true"]["atual"]
    assert plain["pnl"] > confirmed["pnl"]
    assert plain["signals"] == confirmed["signals"] == 1


def test_item3_gate_accepts_only_strict_old_dd_reduction_and_ten_percent_pf():
    from app.study_consolidation import _confirm_relative_metrics

    eligible = _confirm_relative_metrics({
        "confirm_false": {
            "atual": {"pf": 2.0}, "antiga": {"dd_pct": 50.0},
        },
        "confirm_true": {
            "atual": {"pf": 1.8}, "antiga": {"dd_pct": 49.9},
        },
    })
    assert eligible["eligible"] is True
    assert eligible["current_pf_drop_pct"] == 10.0
    assert eligible["old_dd_reduction_pct"] == 0.2

    equal_dd = _confirm_relative_metrics({
        "confirm_false": {"atual": {"pf": 2.0}, "antiga": {"dd_pct": 50.0}},
        "confirm_true": {"atual": {"pf": 1.8}, "antiga": {"dd_pct": 50.0}},
    })
    too_much_pf = _confirm_relative_metrics({
        "confirm_false": {"atual": {"pf": 2.0}, "antiga": {"dd_pct": 50.0}},
        "confirm_true": {"atual": {"pf": 1.799}, "antiga": {"dd_pct": 49.9}},
    })
    assert equal_dd["eligible"] is False
    assert too_much_pf["eligible"] is False


def test_item3_variants_are_isolated_and_future_window_does_not_change_result():
    from app.study_consolidation import _run, run_confirm_sweep

    signal = _confirm_signal()
    candles = {"EURUSD": _confirm_candles()}
    windows = {"atual": ([signal], candles), "tudo": ([], {}), "antiga": ([], {})}
    report = run_confirm_sweep(windows, risk=1.0, balance=10_000.0, daily_stop=0)
    direct_false = _run(
        [signal], candles, "auto_escada_3", 1.0, 10_000.0, 0, "baseline",
        confirm_override=False,
    )
    assert report["variants"]["confirm_false"]["atual"]["pnl"] == direct_false["pnl"]

    # A candle appended after the confirmed/unconfirmed exits is not part of
    # either decision; it must not leak into the earlier result.
    extended = {"EURUSD": candles["EURUSD"] + [
        {"ts": 1786963560, "open": 1.0950, "high": 1.1000,
         "low": 1.0800, "close": 1.0900},
    ]}
    extended_report = run_confirm_sweep(
        {"atual": ([signal], extended), "tudo": ([], {}), "antiga": ([], {})},
        risk=1.0, balance=10_000.0, daily_stop=0,
    )
    assert extended_report["variants"]["confirm_false"]["atual"]["pnl"] == \
        report["variants"]["confirm_false"]["atual"]["pnl"]
    assert extended_report["variants"]["confirm_true"]["atual"]["pnl"] == \
        report["variants"]["confirm_true"]["atual"]["pnl"]
