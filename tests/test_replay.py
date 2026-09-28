from app.execution.replay import normalize_tape, run_replay


BASE = "2026-08-19T10:00:00+00:00"


def signal_event(at=BASE):
    return {
        "at": at,
        "kind": "signal",
        "sequence": 1,
        "signal_id": "replay-s1",
        "symbol": "XAUUSD",
        "payload": {
            "id": "replay-s1",
            "symbol": "XAUUSD",
            "direction": "buy",
            "entry": 4400.0,
            "stop_loss": 4396.0,
            "take_profit_1": 4402.0,
            "take_profit_2": 4404.0,
            "take_profit_3": 4406.0,
            "received_at": at,
            "status": "pending",
        },
    }


def quote(at, price, sequence):
    return {
        "at": at,
        "kind": "quote",
        "sequence": sequence,
        "symbol": "XAUUSD",
        "payload": {"price": price},
    }


def test_tape_keeps_same_timestamp_sequence():
    rows = normalize_tape([
        quote(BASE, 4400.0, 2),
        signal_event(BASE),
        quote(BASE, 4402.0, 3),
    ])
    assert [row.kind for row in rows] == ["signal", "quote", "quote"]
    assert [row.sequence for row in rows] == [1, 2, 3]


def test_replay_matches_sim_paper_and_mt5_dry_run(tmp_path):
    # The path is deliberately shared: TP1 then a return to entry. It tests
    # fills, BE SL, exit reasons and P&L without touching the production DB.
    rows = [
        signal_event(),
        quote("2026-08-19T10:00:01+00:00", 4402.0, 2),
        quote("2026-08-19T10:00:02+00:00", 4400.0, 3),
    ]
    report = run_replay(rows, risk_pct=1.0, balance=1000.0)
    assert report["ok"] is True
    assert report["parity"]["equal"] is True
    assert report["by_signal"]["replay-s1"]["parity"]["equal"] is True
    for engine in ("sim", "paper", "mt5"):
        observation = report["engines"][engine]["signals"]["replay-s1"]
        assert observation["pnl"] == 2.0
        assert all(row[1] == "closed" for row in observation["decisions"])
    assert report["tape"]["pending_management"] == 0
    assert len(report["snapshots"]) == len(rows)
    assert report["snapshots"][0]["kind"] == "signal"
    assert report["snapshots"][-1]["engines"]["sim"]["positions"]


def test_replay_management_is_applied_in_same_order():
    rows = [
        signal_event(),
        quote("2026-08-19T10:00:01+00:00", 4401.0, 2),
        {
            "at": "2026-08-19T10:00:01+00:00",
            "kind": "management",
            "sequence": 3,
            "signal_id": "replay-s1",
            "payload": {"kind": "protect", "message_id": "mgmt-1"},
        },
        quote("2026-08-19T10:00:02+00:00", 4400.0, 4),
    ]
    report = run_replay(rows)
    assert report["parity"]["equal"] is True
    event = next(item for item in report["applied_events"]
                  if item["kind"] == "management")
    assert all(event["engines"][name] for name in ("sim", "paper", "mt5"))


def test_replay_preserves_broker_evidence_without_driving_the_study():
    rows = [
        signal_event(),
        {
            "at": "2026-08-19T10:00:00+00:00",
            "kind": "order_result", "sequence": 2,
            "signal_id": "replay-s1", "symbol": "XAUUSD",
            "payload": {"action": "open", "retcode": 10009,
                         "accepted": True, "latency_ms": 3.5},
        },
        quote("2026-08-19T10:00:01+00:00", 4402.0, 3),
    ]
    report = run_replay(rows)
    assert len(report["broker_observations"]) == 1
    assert report["broker_observations"][0]["payload"]["retcode"] == 10009
    assert report["parity"]["equal"] is True


def test_replay_does_not_write_the_caller_database(tmp_path):
    # The harness creates and destroys its own SQLite files; this sentinel DB
    # must remain untouched by the study.
    from app.db import DB
    db = DB(str(tmp_path / "caller.db"))
    try:
        before = db.count_trades()
        run_replay([signal_event(), quote("2026-08-19T10:00:01+00:00", 4396.0, 2)])
        assert db.count_trades() == before == 0
    finally:
        db.close()
