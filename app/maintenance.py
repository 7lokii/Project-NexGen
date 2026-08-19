"""Operational maintenance helpers used by the server and scheduled tasks.

The helpers in this module are deliberately side-effect-light: disk checks and
DB audits are read-only, while the rotating stream is the only long-lived
writer.  They are shared by the API process, the Telegram listener, and the
Windows maintenance commands so the limits cannot silently diverge.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_DISK_THRESHOLD_GB = 10.0

# Analytical history is never deleted by routine maintenance.  Operational
# tables have bounded policies documented in TECHNICAL.md and can be archived
# later without changing this audit command.
TABLE_POLICIES = {
    "signals": "indefinite; analytical/source history",
    "trades": "indefinite; real execution audit",
    "positions": "indefinite; real execution audit",
    "paper_trades": "indefinite; forward comparison",
    "paper_positions": "indefinite; forward comparison",
    "group_summaries": "indefinite; group ground truth",
    "protect_latency_log": "archive after 365 days; retain aggregate metrics",
    "paper_balance_history": "archive after 180 days; retain aggregate metrics",
    "notifications": "archive after 90 days; retain risk/rollback events",
    "listener_restarts": "purge after 30 days",
    "ignored_messages": "latest 1000 rows (bounded at insert time)",
    "config": "indefinite; current state and audit snapshots",
    "paper_accounts": "indefinite; current virtual-account state",
    "pending_management": "archive applied rows after 90 days; retain unapplied rows",
    "operational_exceptions": "archive after 365 days; retain aggregate metrics",
    "spread_observations": "archive after 365 days; retain monthly averages",
    "mt5_connection_events": "archive after 365 days; retain weekly aggregates",
    "mt5_modify_events": "archive after 365 days; retain weekly aggregates",
}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def log_limits() -> tuple[int, int]:
    """Return the configured byte limit and number of rotated log files."""
    return (
        _env_int("SIGNALBOT_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, 1024),
        _env_int("SIGNALBOT_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT, 0),
    )


def _cap_file(path: Path, max_bytes: Optional[int]) -> None:
    """Keep a rotated artifact bounded, preserving its newest tail."""
    if not max_bytes or not path.exists() or path.stat().st_size <= max_bytes:
        return
    with open(path, "rb") as fh:
        fh.seek(-int(max_bytes), os.SEEK_END)
        tail = fh.read(int(max_bytes))
    tmp = path.with_name(path.name + ".trim")
    with open(tmp, "wb") as fh:
        fh.write(tail)
    os.replace(tmp, path)


def _cap_rotated_files(path: Path, backup_count: int,
                       max_bytes: Optional[int]) -> None:
    if not max_bytes:
        return
    for index in range(1, max(0, int(backup_count)) + 1):
        _cap_file(path.with_name(f"{path.name}.{index}"), max_bytes)


def _rotate_closed_file(path: Path, backup_count: int,
                        max_bytes: Optional[int] = None) -> None:
    """Rotate a *closed* file, best-effort and safe when no file exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup_count <= 0:
        path.write_bytes(b"")
        return
    for index in range(backup_count - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        if older.exists():
            os.replace(older, newer)
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))
    _cap_rotated_files(path, backup_count, max_bytes)


class RotatingTextStream:
    """Small UTF-8 text stream with deterministic size-based rotation.

    It closes the active file before renaming it, which is important on
    Windows where an open file cannot be replaced.  A write larger than the
    remaining capacity is split, so the active file does not grow past the
    configured limit merely because one log line is large.
    """

    def __init__(self, path: os.PathLike[str] | str,
                 max_bytes: int = DEFAULT_LOG_MAX_BYTES,
                 backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
                 encoding: str = "utf-8") -> None:
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.encoding = encoding
        self._lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding=encoding,
                          errors="replace", buffering=1)
        self._nexgen_rotating = True
        if self._size() > self.max_bytes:
            self._rotate()
        else:
            _cap_rotated_files(self.path, self.backup_count, self.max_bytes)

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _rotate(self) -> None:
        self._file.flush()
        self._file.close()
        try:
            _rotate_closed_file(self.path, self.backup_count, self.max_bytes)
        finally:
            self._file = open(self.path, "a", encoding=self.encoding,
                              errors="replace", buffering=1)

    def write(self, text: str) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed log stream")
        if not text:
            return 0
        with self._lock:
            remaining = str(text)
            while remaining:
                used = self._size()
                if used >= self.max_bytes:
                    self._rotate()
                    used = 0
                available = self.max_bytes - used
                encoded_len = len(remaining.encode(self.encoding, errors="replace"))
                if encoded_len <= available:
                    self._file.write(remaining)
                    self._file.flush()
                    break
                # Find the largest character prefix that fits.  Log lines are
                # normally short; binary search keeps an unusually large line
                # bounded without quadratic behaviour.
                low, high = 1, len(remaining)
                best = 0
                while low <= high:
                    mid = (low + high) // 2
                    size = len(remaining[:mid].encode(self.encoding, errors="replace"))
                    if size <= available:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                if best == 0:
                    self._rotate()
                    continue
                self._file.write(remaining[:best])
                self._file.flush()
                remaining = remaining[best:]
                if remaining:
                    self._rotate()
        return len(text)

    def flush(self) -> None:
        if not self._closed:
            with self._lock:
                self._file.flush()

    def close(self) -> None:
        if not self._closed:
            with self._lock:
                self._file.close()
                self._closed = True

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return not self._closed

    def fileno(self) -> int:
        return self._file.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def configure_process_stdio(prefix: str, root_dir: Optional[os.PathLike[str] | str] = None) -> bool:
    """Install bounded stdout/stderr files for a long-running process.

    ``SIGNALBOT_LOG_ROTATE=0`` is used by tests and one-off local debugging.
    Production starts leave it enabled (the default).  The server runner calls
    this before Uvicorn creates its logging handlers; the Telegram listener
    calls it immediately after loading dotenv.
    """
    if os.getenv("SIGNALBOT_LOG_ROTATE", "1").strip().lower() in {"0", "false", "off"}:
        return False
    root = Path(root_dir or os.getenv("SIGNALBOT_LOG_DIR", str(ROOT_DIR)))
    max_bytes, backup_count = log_limits()
    old_out, old_err = sys.stdout, sys.stderr
    if getattr(old_out, "_nexgen_rotating", False):
        return True
    # Flush inherited redirection before replacing it.  The official runner
    # starts without shell redirection; this also keeps direct uvicorn starts
    # from losing their last console line.
    for stream in (old_out, old_err):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    sys.stdout = RotatingTextStream(root / f"{prefix}.log", max_bytes, backup_count)
    sys.stderr = RotatingTextStream(root / f"{prefix}.err.log", max_bytes, backup_count)
    return True


def append_rotating_log(path: os.PathLike[str] | str, line: str,
                        max_bytes: Optional[int] = None,
                        backup_count: Optional[int] = None) -> None:
    """Append one line to a bounded log (used by the backup task)."""
    if max_bytes is None or backup_count is None:
        configured_max, configured_backups = log_limits()
        max_bytes = configured_max if max_bytes is None else max_bytes
        backup_count = configured_backups if backup_count is None else backup_count
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (line + "\n").encode("utf-8", errors="replace")
    if target.exists() and target.stat().st_size + len(encoded) > max(1024, int(max_bytes)):
        _rotate_closed_file(target, int(backup_count), int(max_bytes))
    with open(target, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(line + "\n")


def disk_status(path: Optional[os.PathLike[str] | str] = None,
                threshold_gb: Optional[float] = None) -> dict:
    """Read-only disk usage status for the configured Windows/system volume."""
    if path is None or str(path).strip() == "":
        if os.name == "nt":
            path = os.getenv("SystemDrive", "C:") + "\\"
        else:
            path = Path.cwd().anchor or "/"
    if threshold_gb is None:
        try:
            threshold_gb = float(os.getenv("SIGNALBOT_DISK_FREE_GB", str(DEFAULT_DISK_THRESHOLD_GB)))
        except (TypeError, ValueError):
            threshold_gb = DEFAULT_DISK_THRESHOLD_GB
    threshold_gb = max(0.0, float(threshold_gb))
    usage = shutil.disk_usage(path)
    gb = 1024 ** 3
    free_gb = usage.free / gb
    total_gb = usage.total / gb
    return {
        "path": str(path),
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
        "used_pct": round((usage.used / usage.total) * 100, 2) if usage.total else 0.0,
        "threshold_gb": threshold_gb,
        "below_threshold": free_gb < threshold_gb,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _timestamp_column(columns: Iterable[str]) -> Optional[str]:
    for candidate in ("created_at", "received_at", "opened_at", "ts", "started_at", "updated_at"):
        if candidate in columns:
            return candidate
    return None


def audit_database(path: os.PathLike[str] | str) -> dict:
    """Return table counts and observed monthly write rates without writing DB."""
    db_path = Path(path)
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = []
        for row in table_rows:
            table = str(row["name"])
            columns = [r["name"] for r in conn.execute(
                f"PRAGMA table_info({_quote_identifier(table)})").fetchall()]
            count = int(conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0])
            ts_col = _timestamp_column(columns)
            monthly: Dict[str, int] = {}
            oldest = newest = None
            if ts_col:
                qtable, qcol = _quote_identifier(table), _quote_identifier(ts_col)
                values = conn.execute(
                    f"SELECT substr(CAST({qcol} AS TEXT),1,7) AS month, COUNT(*) AS n "
                    f"FROM {qtable} WHERE {qcol} IS NOT NULL GROUP BY month ORDER BY month"
                ).fetchall()
                monthly = {str(v["month"]): int(v["n"]) for v in values if v["month"]}
                bounds = conn.execute(
                    f"SELECT MIN(CAST({qcol} AS TEXT)), MAX(CAST({qcol} AS TEXT)) FROM {qtable} "
                    f"WHERE {qcol} IS NOT NULL"
                ).fetchone()
                oldest, newest = bounds[0], bounds[1]
            span_days = None
            rate = None
            if oldest and newest:
                try:
                    a = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
                    b = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
                    span_days = max(1.0, (b - a).total_seconds() / 86400.0)
                    rate = round(count / (span_days / 30.0), 2)
                except (TypeError, ValueError):
                    pass
            tables.append({
                "table": table,
                "rows": count,
                "timestamp_column": ts_col,
                "oldest": oldest,
                "newest": newest,
                "monthly_rows": monthly,
                "observed_rows_per_month": rate,
                "policy": TABLE_POLICIES.get(table, "review before adding retention"),
            })
        return {
            "ok": True,
            "path": str(db_path),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tables": tables,
        }
    finally:
        conn.close()


REQUIRED_RESTORE_TABLES = (
    "signals", "trades", "positions", "paper_trades", "paper_positions",
    "notifications", "config",
)


def restore_check(backup_path: os.PathLike[str] | str,
                  required_tables: Iterable[str] = REQUIRED_RESTORE_TABLES,
                  expected_counts: Optional[Dict[str, int]] = None) -> dict:
    """Restore a SQLite backup into a temp DB and validate integrity/counts."""
    source_path = Path(backup_path)
    if not source_path.exists():
        return {"ok": False, "backup": str(source_path), "error": "backup não encontrado"}
    required = tuple(required_tables)
    try:
        with tempfile.TemporaryDirectory(prefix="nexgen-restore-") as tmp:
            restored_path = Path(tmp) / "restored.db"
            source = sqlite3.connect(str(source_path))
            restored = sqlite3.connect(str(restored_path))
            try:
                source.backup(restored)
                restored.row_factory = sqlite3.Row
                integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    return {"ok": False, "backup": str(source_path),
                            "error": f"integrity_check: {integrity}"}
                present = {r[0] for r in restored.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                missing = [name for name in required if name not in present]
                if missing:
                    return {"ok": False, "backup": str(source_path),
                            "error": f"tabelas em falta: {', '.join(missing)}"}
                counts = {
                    name: int(restored.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()[0])
                    for name in required
                }
                mismatches = {
                    name: {"expected": int(expected_counts[name]), "actual": counts[name]}
                    for name in counts
                    if expected_counts is not None and name in expected_counts
                    and counts[name] != int(expected_counts[name])
                }
                if mismatches:
                    return {"ok": False, "backup": str(source_path),
                            "error": "contagens divergentes", "counts": counts,
                            "mismatches": mismatches}
                return {"ok": True, "backup": str(source_path),
                        "integrity": integrity, "counts": counts}
            finally:
                restored.close()
                source.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        return {"ok": False, "backup": str(source_path), "error": str(exc)}


def latest_backup(directory: os.PathLike[str] | str) -> Optional[Path]:
    files = sorted(Path(directory).glob("signalbot-*.db"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    print("Use maintenance.py na raiz para operações agendadas.")
