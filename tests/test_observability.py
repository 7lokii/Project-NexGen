"""Regression tests for the persistent Item 10 operational telemetry."""

from datetime import datetime, timedelta, timezone

from app.db import DB


def _stamp(days_ago: int, hour: int = 12) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_operational_metrics_aggregate_exceptions_spread_and_mt5(tmp_path):
    db = DB(str(tmp_path / "observability.db"))
    try:
        db.record_exception(
            ValueError("quote failed"),
            context={"component": "mt5", "phase": "spread_observation"},
            signal_id="sig-1", trade_id="trade-1",
            created_at=_stamp(1),
        )
        db.record_exception(
            RuntimeError("tick failed"),
            context={"loop": "market", "phase": "tick"},
            created_at=_stamp(2),
        )

        db.insert_spread_observation(_stamp(1), "XAUUSD", 4400.0, 4400.2,
                                     0.2, 20.0)
        db.insert_spread_observation(_stamp(2), "XAUUSD", 4400.0, 4400.1,
                                     0.1, 10.0)
        db.insert_spread_observation(_stamp(1), "EURUSD", 1.1, 1.1001,
                                     0.0001, 1.0)

        db.record_mt5_connection_event("reconnect_attempt", "test", _stamp(2))
        db.record_mt5_connection_event("reconnect_success", "test", _stamp(2))
        db.record_mt5_connection_event("disconnected", "test", _stamp(2))
        db.record_mt5_connection_event("reconnect_attempt", "test", _stamp(1))
        db.record_mt5_connection_event("reconnect_failure", "test", _stamp(1))

        db.insert_mt5_modify_event(_stamp(2), trade_id="trade-1",
                                   position_id="pos-1", symbol="XAUUSD", leg=2,
                                   kind="stair", requested_sl=4401.0,
                                   valid_sl=4401.0, retcode=10009, accepted=True)
        db.insert_mt5_modify_event(_stamp(1), trade_id="trade-1",
                                   position_id="pos-1", symbol="XAUUSD", leg=2,
                                   kind="trail", requested_sl=4402.0,
                                   valid_sl=4402.0, retcode=10016, accepted=False,
                                   error="Invalid stops")

        payload = db.operational_metrics(days=90)
        assert payload["ok"] is True
        assert payload["exceptions"]["total"] == 2
        assert payload["exceptions"]["by_type"][0]["count"] == 1
        assert payload["exceptions"]["recent"][0]["context"]["phase"] in {
            "tick", "spread_observation"
        }

        monthly = payload["spread"]["monthly"]
        gold = next(row for row in monthly if row["symbol"] == "XAUUSD")
        assert gold["observations"] == 2
        assert gold["avg_spread_pips"] == 15.0
        assert payload["spread"]["observations"] == 3

        connections = payload["mt5"]["connections"]["weekly"]
        assert connections
        assert sum(row["reconnect_attempts"] for row in connections) == 2
        assert sum(row["reconnect_successes"] for row in connections) == 1
        assert sum(row["reconnect_failures"] for row in connections) == 1
        assert sum(row["disconnects"] for row in connections) == 1

        modifies = payload["mt5"]["modifies"]
        assert modifies["total_attempts"] == 2
        assert modifies["total_rejected"] == 1
        assert modifies["rejection_rate_pct"] == 50.0
        assert any(row["retcodes"].get("10016") == 1
                   for row in modifies["weekly"])
    finally:
        db.close()


def test_exception_rows_preserve_context_and_traceback(tmp_path):
    db = DB(str(tmp_path / "exceptions.db"))
    try:
        try:
            raise LookupError("missing quote")
        except LookupError as exc:
            db.record_exception(exc, context={"symbol": "XAUUSD"})
        row = db.exception_rows(1)[0]
        assert row["exception_type"].endswith("LookupError")
        assert row["message"] == "missing quote"
        assert row["context"] == {"symbol": "XAUUSD"}
        assert "LookupError" in row["traceback"]
    finally:
        db.close()
