from app.execution.journal import replay_events
from app.db import DB
from app.models import Signal
from app.strategies import strategy_lifecycle


def test_execution_journal_round_trip(tmp_path):
    from datetime import datetime, timezone
    db = DB(str(tmp_path / "journal.db"))
    try:
        # timestamp ATUAL: as stats são uma janela rolante de N dias — uma
        # data fixa sairia da janela à medida que o calendário avança.
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.record_execution_event(
            at, "quote", engine="runtime",
            symbol="XAUUSD", payload={"price": 4400.0},
        )
        rows = db.execution_events(kind="quote")
        assert len(rows) == 1
        assert rows[0]["payload"]["price"] == 4400.0
        assert db.execution_event_stats()["total"] == 1
    finally:
        db.close()


def test_execution_tape_joins_signals_and_routes_reply_management(tmp_path):
    db = DB(str(tmp_path / "tape.db"))
    try:
        signal = Signal(
            id="s1", source="Golden Moves", raw_text="buy XAUUSD",
            symbol="XAUUSD", direction="buy", entry=4400.0,
            stop_loss=4396.0, take_profit_1=4402.0, take_profit_2=4404.0,
            received_at="2026-08-19T10:00:00+00:00", status="executed",
            message_id="42",
        )
        db.insert_signal(signal)
        db.record_execution_event(
            "2026-08-19T10:00:01+00:00", "quote", engine="runtime",
            symbol="XAUUSD", payload={"price": 4401.0},
        )
        db.record_execution_event(
            "2026-08-19T10:00:02+00:00", "management", engine="runtime",
            payload={"kind": "protect", "reply_to_msg_id": "42"},
        )
        tape = db.execution_tape(signal_id="s1")
        assert [row["kind"] for row in tape["events"]] == [
            "signal", "quote", "management",
        ]
        assert tape["events"][2]["signal_id"] == "s1"
        assert tape["meta"]["source"] == "execution_events + signals"
    finally:
        db.close()


def test_replay_events_is_chronological_and_ignores_unknown():
    seen = []
    count = replay_events([
        {"at": "2026-08-19T10:00:02+00:00", "kind": "quote",
         "payload": {"price": 2}},
        {"at": "2026-08-19T10:00:01+00:00", "kind": "quote",
         "payload": {"price": 1}},
        {"at": "2026-08-19T10:00:00+00:00", "kind": "future"},
    ], {"quote": lambda event: seen.append(event.payload["price"])})
    assert count == 2
    assert seen == [1, 2]


def test_strategy_lifecycle_is_explicit():
    assert strategy_lifecycle("nova_gestao")["paper"] is True
    assert strategy_lifecycle("nova_gestao_fallback")["state"] == "experimental"
    assert strategy_lifecycle("nova_gestao_fallback")["paper"] is True
    assert strategy_lifecycle("nova_gestao")["promotable"] is True
    assert strategy_lifecycle("tp2")["paper"] is False
    assert strategy_lifecycle("nova")["paper"] is True
    assert strategy_lifecycle("nova")["menu"] is False


def test_execution_events_retention_purge_bounded(tmp_path):
    """O journal (dezenas a centenas de milhar de linhas/dia com o journal
    ligado) tem retenção bounded — linhas além de N dias são apagadas no
    watchdog/arranque; as recentes ficam intactas."""
    from datetime import datetime, timedelta, timezone
    db = DB(str(tmp_path / "retention.db"))
    try:
        db.record_execution_event(
            "2020-01-01T00:00:00+00:00", "quote", engine="runtime",
            symbol="XAUUSD", payload={"price": 1.0},
        )
        recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.record_execution_event(
            recent, "quote", engine="runtime",
            symbol="XAUUSD", payload={"price": 2.0},
        )
        assert len(db.execution_events()) == 2
        # purge com retenção de 30 dias: a linha de 2020 sai, a recente fica
        assert db.purge_old_execution_events(30) == 1
        rows = db.execution_events()
        assert len(rows) == 1
        assert rows[0]["payload"]["price"] == 2.0
    finally:
        db.close()
