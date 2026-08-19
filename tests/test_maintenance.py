"""Operational maintenance checks are read-only and use temporary files/DBs."""

import os
import sqlite3
from pathlib import Path

import pytest

from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.maintenance import (RotatingTextStream, audit_database, disk_status,
                             restore_check)
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager


def _make_restore_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    for table in ("signals", "trades", "positions", "paper_trades",
                  "paper_positions", "notifications", "config"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        conn.execute(f"INSERT INTO {table} VALUES (1)")
    conn.execute("CREATE TABLE events (created_at TEXT)")
    conn.execute("INSERT INTO events VALUES ('2026-08-19T10:00:00+00:00')")
    conn.commit()
    conn.close()


def test_rotating_stream_keeps_active_and_backups_bounded(tmp_path):
    path = tmp_path / "server.log"
    stream = RotatingTextStream(path, max_bytes=1024, backup_count=2)
    try:
        stream.write("x" * 5000)
        stream.write("\nlast\n")
        stream.flush()
    finally:
        stream.close()
    files = [path, path.with_name("server.log.1"), path.with_name("server.log.2")]
    assert path.exists()
    assert all(p.stat().st_size <= 1024 for p in files if p.exists())
    assert path.with_name("server.log.1").exists()


def test_rotating_stream_trims_preexisting_oversized_artifacts(tmp_path):
    path = tmp_path / "server.log"
    path.write_bytes(b"old" * 2000)
    old_backup = path.with_name("server.log.1")
    old_backup.write_bytes(b"older" * 2000)
    stream = RotatingTextStream(path, max_bytes=1024, backup_count=2)
    stream.close()
    assert path.stat().st_size <= 1024
    assert old_backup.stat().st_size <= 1024


def test_disk_status_threshold_is_deterministic(tmp_path):
    status = disk_status(tmp_path, threshold_gb=0)
    assert status["below_threshold"] is False
    too_high = disk_status(tmp_path, threshold_gb=status["total_gb"] + 1)
    assert too_high["below_threshold"] is True
    assert too_high["free_gb"] >= 0


def test_botstate_disk_alert_can_be_forced_without_trading(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = DB(str(db_path))
    market = MarketSimulator(seed=1)
    market.ensure("XAUUSD", 4400.0)
    ws = WSManager()
    notifier = Notifier(db, ws)
    state = BotState(db, market, RiskEngine(), SimEngine(db, market), notifier, ws)
    try:
        monkeypatch.setenv("SIGNALBOT_DISK_PATH", str(tmp_path))
        monkeypatch.setenv("SIGNALBOT_DISK_FREE_GB", "999999")
        result = __import__("asyncio").run(state._check_disk_space(force=True))
        assert result["below_threshold"] is True
        notification = db.list_notifications(1)[0]
        assert notification.kind == "risk"
        assert "Espaço em disco baixo" in notification.text
        # simula restart: o dia persistido impede um segundo alerta, mas o
        # check continua a poder reconstruir o estado do snapshot.
        state._disk_status = {}
        state._disk_last_check_day = ""
        __import__("asyncio").run(state._check_disk_space(force=False))
        assert len(db.list_notifications(10)) == 1
    finally:
        db.close()


def test_database_audit_reports_counts_months_and_policy(tmp_path):
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notifications (id INTEGER, created_at TEXT)")
    conn.executemany(
        "INSERT INTO notifications VALUES (?, ?)",
        [(1, "2026-07-01T00:00:00+00:00"),
         (2, "2026-08-01T00:00:00+00:00")],
    )
    conn.execute("CREATE TABLE trades (id INTEGER)")
    conn.execute("INSERT INTO trades VALUES (1)")
    conn.commit()
    conn.close()
    report = audit_database(path)
    by_name = {row["table"]: row for row in report["tables"]}
    assert by_name["notifications"]["rows"] == 2
    assert by_name["notifications"]["monthly_rows"] == {"2026-07": 1, "2026-08": 1}
    assert "90 days" in by_name["notifications"]["policy"]
    assert by_name["trades"]["policy"].startswith("indefinite")


def test_restore_check_restores_and_counts_required_tables(tmp_path):
    path = tmp_path / "signalbot-20260819-100000.db"
    _make_restore_db(path)
    result = restore_check(path)
    assert result["ok"] is True
    assert result["integrity"] == "ok"
    assert result["counts"]["signals"] == 1
    assert result["counts"]["paper_positions"] == 1


def test_restore_check_detects_corrupt_backup(tmp_path):
    path = tmp_path / "signalbot-corrupt.db"
    path.write_bytes(b"not a sqlite database")
    result = restore_check(path)
    assert result["ok"] is False
    assert result["error"]


def test_restore_check_detects_count_mismatch(tmp_path):
    path = tmp_path / "signalbot-counts.db"
    _make_restore_db(path)
    result = restore_check(path, expected_counts={"signals": 2})
    assert result["ok"] is False
    assert result["mismatches"]["signals"] == {"expected": 2, "actual": 1}
