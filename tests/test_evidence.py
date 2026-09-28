from app.db import DB
from app.execution.evidence import (audit_csv, audit_report, compare_broker_fills,
                                    forward_gate_report, journal_quality_report)
from app.models import Position, Signal, Trade


def _signal(sid="s1", at="2026-08-20T10:00:00+00:00"):
    return Signal(
        id=sid, source="test", raw_text="BUY XAUUSD", symbol="XAUUSD",
        direction="buy", entry=4400.0, stop_loss=4396.0,
        take_profit_1=4402.0, take_profit_2=4404.0,
        status="executed", received_at=at,
    )


def _trade(tid="t1", sid="s1", status="open", closed_at=None,
           strategy_id=None, origin=None, pnl=0.0):
    return Trade(
        id=tid, signal_id=sid, symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4396.0, take_profit_1=4402.0,
        take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status=status, opened_at="2026-08-20T10:00:00+00:00",
        closed_at=closed_at, pnl=pnl, strategy_id=strategy_id,
        paper_origin=origin,
    )


def test_journal_quality_reports_counts_latency_and_missing_tape(tmp_path):
    db = DB(str(tmp_path / "quality.db"))
    try:
        db.set_config("execution_journal_enabled", "True")
        db.insert_signal(_signal())
        db.insert_trade(_trade())
        for leg, tp in ((1, 4402.0), (2, 4404.0)):
            db.insert_position(Position(
                id=f"p{leg}", trade_id="t1", leg=leg, symbol="XAUUSD",
                direction="buy", lots=0.01, entry=4400.0, stop_loss=4396.0,
                take_profit=tp, status="open", opened_at="2026-08-20T10:00:00+00:00",
            ))
        db.record_execution_event("2026-08-20T10:00:00+00:00", "fill_open",
                                  engine="mt5", trade_id="t1", signal_id="s1",
                                  payload={"leg": 1, "price": 4400.0,
                                           "signal_to_fill_seconds": 2.5})
        db.record_execution_event("2026-08-20T10:00:01+00:00", "order_result",
                                  engine="mt5", trade_id="t1", signal_id="s1",
                                  payload={"retcode": 10009, "accepted": True,
                                           "latency_ms": 4.0})
        report = journal_quality_report(db, day="2026-08-20")
        assert report["events"]["total"] == 2
        assert report["orders"]["retcodes"] == {"10009": 1}
        assert report["latency"]["signal_to_fill_seconds"]["p90"] == 2.5
        assert report["trades"]["incomplete_tape"] == 1
        assert report["trades"]["incomplete"][0]["missing_open_legs"] == [2]
    finally:
        db.close()


def test_fill_audit_reports_first_divergence_against_replay(tmp_path):
    db = DB(str(tmp_path / "fills.db"))
    try:
        db.insert_signal(_signal())
        db.insert_trade(_trade(status="open"))
        db.insert_position(Position(
            id="p1", trade_id="t1", leg=1, symbol="XAUUSD", direction="buy",
            lots=0.01, entry=4400.0, stop_loss=4396.0, take_profit=4402.0,
            status="closed", opened_at="2026-08-20T10:00:00+00:00",
            closed_at="2026-08-20T10:00:02+00:00", close_price=4401.0,
            pnl=1.0, exit_reason="manual",
        ))
        db.record_execution_event("2026-08-20T10:00:00+00:00", "fill_open",
                                  engine="mt5", trade_id="t1", signal_id="s1",
                                  payload={"leg": 1, "price": 4400.0,
                                           "source": "broker"})
        db.record_execution_event("2026-08-20T10:00:01+00:00", "quote",
                                  engine="runtime", symbol="XAUUSD",
                                  payload={"price": 4402.0})
        db.record_execution_event("2026-08-20T10:00:02+00:00", "fill_close",
                                  engine="mt5", trade_id="t1", signal_id="s1",
                                  payload={"leg": 1, "price": 4401.0,
                                           "source": "broker"})
        report = compare_broker_fills(db, start="2026-08-20T00:00:00+00:00",
                                      end="2026-08-20T23:59:59+00:00")
        assert report["count"] == 1
        assert report["divergent"] == 1
        assert report["first_divergence"]["field"] == "close_price"
        assert report["first_divergence"]["actual"] == 4401.0
        assert report["first_divergence"]["replay"] == 4402.0
    finally:
        db.close()


def test_audit_bundle_and_csv_are_exportable(tmp_path):
    db = DB(str(tmp_path / "archive.db"))
    try:
        report = audit_report(
            db, start="2026-08-20T00:00:00+00:00",
            end="2026-08-20T23:59:59+00:00",
        )
        csv_text = audit_csv(report)
        assert report["period"]["timezone"] == "UTC"
        assert "section;metric;value;details" in csv_text
        assert "forward_gate" in csv_text
        archived = db.archive_evidence(
            "forward_gate", report["period"]["start"], report["period"]["end"], report,
        )
        assert archived["kind"] == "forward_gate"
        rows = db.evidence_archives("forward_gate")
        assert len(rows) == 1
        loaded = db.evidence_archive(rows[0]["id"])
        assert loaded["payload"]["forward_gate"]["policy"]["no_auto_promotion"] is True
    finally:
        db.close()


def test_forward_gate_is_shadow_only_and_never_promotes(tmp_path):
    db = DB(str(tmp_path / "gate.db"))
    try:
        for i in range(20):
            sid = f"gate-{i}"
            db.insert_paper_trade(_trade(
                tid=f"shadow-{i}", sid=sid, status="closed",
                closed_at=f"2026-08-{20 if i < 10 else 19:02d}T12:00:00+00:00",
                strategy_id="auto_escada_3", origin="shadow", pnl=10.0,
            ))
            db.insert_paper_trade(_trade(
                tid=f"ref-{i}", sid=sid, status="closed",
                closed_at=f"2026-08-{20 if i < 10 else 19:02d}T12:00:00+00:00",
                strategy_id="nova_gestao", origin="race", pnl=5.0,
            ))
        report = forward_gate_report(
            db, start="2026-08-19T00:00:00+00:00",
            end="2026-08-20T23:59:59+00:00",
            strategies=["auto_escada_3", "auto_escada_5"],
        )
        auto3 = next(row for row in report["strategies"]
                     if row["strategy_id"] == "auto_escada_3")
        auto5 = next(row for row in report["strategies"]
                     if row["strategy_id"] == "auto_escada_5")
        assert auto3["closed"] == 20
        assert auto3["reference"]["closed_common"] == 20
        assert auto3["eligible"] is True
        assert auto5["eligible"] is False
        assert report["policy"]["no_auto_promotion"] is True
    finally:
        db.close()


def test_forward_gate_hybrid_reference_uses_shadow_when_race_empty(tmp_path):
    """Referência híbrida: quando a principal não negociou (race vazia — ex.:
    kill switch ligado), a conta shadow da nova_gestao preenche a referência
    e o gate fica comparável sem esperar 20 trades reais."""
    db = DB(str(tmp_path / "gate_hybrid.db"))
    try:
        for i in range(20):
            sid = f"ghy-{i}"
            db.insert_paper_trade(_trade(
                tid=f"sh-{i}", sid=sid, status="closed",
                closed_at=f"2026-08-20T12:00:00+00:00",
                strategy_id="auto_escada_3", origin="shadow", pnl=10.0,
            ))
        # só 12 sinais têm referência shadow da nova_gestao (sem race)
        for i in range(12):
            db.insert_paper_trade(_trade(
                tid=f"refsh-{i}", sid=f"ghy-{i}", status="closed",
                closed_at=f"2026-08-20T12:00:00+00:00",
                strategy_id="nova_gestao", origin="shadow", pnl=5.0,
            ))
        report = forward_gate_report(
            db, start="2026-08-19T00:00:00+00:00",
            end="2026-08-20T23:59:59+00:00",
            strategies=["auto_escada_3"],
        )
        auto3 = report["strategies"][0]
        # 12 sinais em comum ≥ min_reference_trades (10) → comparável via shadow
        assert auto3["reference"]["closed_common"] == 12
        assert auto3["gates"]["reference_comparable"] is True
        assert report["reference"]["race_closed"] == 0
        assert report["reference"]["shadow_closed"] == 12
        assert report["policy"]["min_reference_trades"] == 10
    finally:
        db.close()
