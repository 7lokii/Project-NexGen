"""Regression tests for analysis replay ordering and window boundaries."""

import asyncio
import os
import tempfile

from app import analysis
from app.db import DB
from app.execution.feed import YahooProvider
from app.models import Signal


def _signal(signal_id: str, symbol: str, received_at: str,
            raw_text: str = "entry") -> Signal:
    return Signal(
        id=signal_id,
        source="tg:test",
        raw_text=raw_text,
        symbol=symbol,
        direction="buy",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=101.0,
        take_profit_2=102.0,
        status="historical",
        received_at=received_at,
    )


def _result() -> dict:
    return {
        "outcome": "open",
        "pnl": 0.0,
        "commission": 0.0,
        "legs_pnl": {},
    }


def test_analysis_replays_global_chronology_and_excludes_management_after_until(
    monkeypatch,
):
    """The defensive state and causal correlation must not cross time windows.

    Insert the later symbol first to catch per-symbol bucket iteration, then
    add a management message on the exclusive `until` boundary. The replay
    must process entries by timestamp and must not attach the later message to
    the last in-window signal.
    """
    path = tempfile.mktemp(suffix=".db")
    db = DB(path)
    try:
        db.insert_signal(_signal(
            "late-gold", "XAUUSD", "2026-08-02T10:00:00+00:00",
        ))
        db.insert_signal(_signal(
            "early-eur", "EURUSD", "2026-08-01T10:00:00+00:00",
        ))
        db.insert_signal(_signal(
            "after-window-management", "", "2026-08-03T00:00:00+00:00",
            raw_text="management",
        ))

        async def fake_history(self, symbol, interval, range_):
            return [{
                "ts": 1,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            }]

        monkeypatch.setattr(YahooProvider, "history", fake_history)
        monkeypatch.setattr(
            analysis,
            "classify",
            lambda text: {"kind": "protect"} if text == "management" else None,
        )
        seen_ids = []
        seen_management = []

        def fake_managed(signal, candles, events, *args, **kwargs):
            seen_ids.append(signal.id)
            seen_management.append(list(events))
            return _result()

        monkeypatch.setattr(analysis, "simulate_signal_managed", fake_managed)

        payload = asyncio.run(analysis.run_analysis(
            db,
            days=0,
            since="2026-08-01",
            until="2026-08-03",
            manage=True,
            balance=1000.0,
            daily_stop_pct=0,
        ))

        assert seen_ids == ["early-eur", "late-gold"]
        assert seen_management == [[], []]
        assert [row["id"] for row in payload["results"]] == [
            "late-gold", "early-eur",
        ]  # response remains newest-first
    finally:
        db.close()
        if os.path.exists(path):
            os.remove(path)
