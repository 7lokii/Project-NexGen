"""SQLite persistence layer.

Thin, dependency-free storage for signals, trades, positions, notifications
and the key/value config table. All writes go through a single lock so the
async event loop and background tasks never race on the connection.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.models import Notification, Position, Signal, Trade


def _iso_delta_seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    """Segundos entre dois timestamps ISO (None se algum for inválido)."""
    from datetime import datetime
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return None
    if a.tzinfo is None:
        a = a.astimezone()
    if b.tzinfo is None:
        b = b.astimezone()
    return (b - a).total_seconds()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    source TEXT,
    raw_text TEXT,
    symbol TEXT,
    direction TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    take_profit_4 REAL,
    take_profit_5 REAL,
    status TEXT,
    error TEXT,
    received_at TEXT,
    message_id TEXT,
    reply_to_msg_id TEXT,
    edit_deadline TEXT,
    edited_at TEXT,           -- ISO: última vez que o grupo editou a mensagem
    lots REAL,
    llm_parsed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_management (
    id TEXT PRIMARY KEY,
    signal_id TEXT,            -- management message id (tg-<group>-<msgid>)
    reply_to_msg_id TEXT,      -- the signal message it replies to
    kind TEXT,
    tp INTEGER,
    sl REAL,                   -- nível para kind='move_sl' ("Adjust SL to X")
    raw_text TEXT,
    source TEXT,
    received_at TEXT,
    created_at TEXT,
    applied INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS group_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    source TEXT,
    period_start TEXT,
    period_end TEXT,
    trades_sent INTEGER,
    winning INTEGER,
    stop_loss INTEGER,
    breakevens INTEGER,
    win_rate REAL,
    pips INTEGER,
    raw_text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    strategy_id TEXT,
    symbol TEXT,
    direction TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    take_profit_4 REAL,
    take_profit_5 REAL,
    risk_pct REAL,
    lots_1 REAL,
    lots_2 REAL,
    lots_3 REAL,
    lots_4 REAL,
    lots_5 REAL,
    status TEXT,
    opened_at TEXT,
    closed_at TEXT,
    pnl REAL,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    leg INTEGER,
    symbol TEXT,
    direction TEXT,
    lots REAL,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT,
    opened_at TEXT,
    closed_at TEXT,
    close_price REAL,
    pnl REAL,
    exit_reason TEXT,
    exit_msg_id TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    text TEXT,
    created_at TEXT,
    action TEXT
);

CREATE TABLE IF NOT EXISTS ignored_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    source TEXT,
    group_name TEXT,
    raw_text TEXT,
    reason TEXT,
    received_at TEXT,
    UNIQUE(message_id, source)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    strategy_id TEXT PRIMARY KEY,   -- id do registo em app/strategies.py
    balance REAL,                   -- saldo simulado (paper trading forward)
    start_balance REAL,             -- saldo inicial (1000 no arranque)
    opened_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_balance_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT,               -- id do registo (a principal usa o saldo real)
    ts TEXT,                        -- ISO timestamp do snapshot
    balance REAL,                   -- saldo na altura (paper ou real)
    UNIQUE(strategy_id, ts)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    strategy_id TEXT,
    signal_id TEXT,
    symbol TEXT,
    direction TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    take_profit_4 REAL,
    take_profit_5 REAL,
    lots_1 REAL,
    lots_2 REAL,
    lots_3 REAL,
    lots_4 REAL,
    lots_5 REAL,
    status TEXT,
    opened_at TEXT,
    closed_at TEXT,
    pnl REAL,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    leg INTEGER,
    symbol TEXT,
    direction TEXT,
    lots REAL,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT,
    opened_at TEXT,
    closed_at TEXT,
    close_price REAL,
    pnl REAL,
    exit_reason TEXT,
    exit_msg_id TEXT
);

CREATE TABLE IF NOT EXISTS listener_restarts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS protect_latency_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    signal_id TEXT,
    trade_id TEXT,
    message_id TEXT,
    symbol TEXT,
    direction TEXT,
    auto_protection_at TEXT,
    group_received_at TEXT NOT NULL,
    auto_sl REAL,
    group_sl REAL,
    applied_sl REAL,
    winner TEXT NOT NULL,
    delta_pips REAL,
    delta_seconds REAL,
    leg1_action TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_protect_latency_strategy
    ON protect_latency_log(strategy_id, group_received_at);
CREATE INDEX IF NOT EXISTS idx_protect_latency_engine
    ON protect_latency_log(engine, group_received_at);

CREATE TABLE IF NOT EXISTS operational_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT NOT NULL,
    context_json TEXT,
    signal_id TEXT,
    trade_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_operational_exceptions_created
    ON operational_exceptions(created_at);

CREATE TABLE IF NOT EXISTS spread_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bid REAL,
    ask REAL,
    spread REAL NOT NULL,
    spread_pips REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'mt5'
);
CREATE INDEX IF NOT EXISTS idx_spread_observations_symbol_time
    ON spread_observations(symbol, observed_at);

CREATE TABLE IF NOT EXISTS mt5_connection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event TEXT NOT NULL,
    reason TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_mt5_connection_events_created
    ON mt5_connection_events(created_at);

CREATE TABLE IF NOT EXISTS mt5_modify_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_id TEXT,
    position_id TEXT,
    symbol TEXT,
    leg INTEGER,
    kind TEXT NOT NULL,
    requested_sl REAL,
    valid_sl REAL,
    retcode INTEGER,
    accepted INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_mt5_modify_events_created
    ON mt5_modify_events(created_at);
CREATE INDEX IF NOT EXISTS idx_mt5_modify_events_trade
    ON mt5_modify_events(trade_id, created_at);
"""


class DB:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Add columns added after the original schema (SQLite has no
        ALTER TABLE ... IF NOT EXISTS, so check pragma first)."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}
        for col in ("take_profit_3", "take_profit_4", "take_profit_5", "llm_parsed"):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} "
                                  + ("INTEGER DEFAULT 0" if col == "llm_parsed" else "REAL"))
        if "reply_to_msg_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}:
            self.conn.execute("ALTER TABLE signals ADD COLUMN reply_to_msg_id TEXT")
        if "lots" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}:
            self.conn.execute("ALTER TABLE signals ADD COLUMN lots REAL")
        if "edited_at" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}:
            self.conn.execute("ALTER TABLE signals ADD COLUMN edited_at TEXT")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "strategy_id" not in existing:
            self.conn.execute("ALTER TABLE trades ADD COLUMN strategy_id TEXT")
        for col in ("take_profit_3", "take_profit_4", "take_profit_5",
                    "lots_3", "lots_4", "lots_5"):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT")
        # positions gained exit attribution (gestão do grupo → razão do fecho).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE positions ADD COLUMN exit_reason TEXT")
            self.conn.execute("ALTER TABLE positions ADD COLUMN exit_msg_id TEXT")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_trades)")}
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN exit_reason TEXT")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_positions)")}
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE paper_positions ADD COLUMN exit_reason TEXT")
            self.conn.execute("ALTER TABLE paper_positions ADD COLUMN exit_msg_id TEXT")
        # group_summaries gained message_id (dedup key for history reloads).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(group_summaries)")}
        if "message_id" not in existing:
            self.conn.execute(
                "ALTER TABLE group_summaries ADD COLUMN message_id TEXT"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_msg ON "
                "group_summaries(message_id)"
            )
        # notifications gained an optional action (ex.: "promote:<strategy>")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(notifications)")}
        if "action" not in existing:
            self.conn.execute("ALTER TABLE notifications ADD COLUMN action TEXT")
        # pending_management gained sl (nível para kind='move_sl' quando a
        # trade ainda não abriu). A tabela nasceu sem a coluna — sem isto o
        # INSERT com `sl` rebenta com 500 e a instrução de gestão perde-se.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}
        if "sl" not in existing:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN sl REAL")

    def close(self) -> None:
        """Close the underlying connection (releases the file on Windows)."""
        with self._lock:
            self.conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            cur = self.conn.cursor()
            try:
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ------------------------------------------------------------------ config

    def all_config(self) -> Dict[str, Any]:
        with self.tx() as cur:
            rows = cur.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_config(self, key: str, value: Any) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # ---------------------------------------------------------------- signals

    def insert_signal(self, s: Signal) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO signals "
                "(id, source, raw_text, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, status, error, received_at, message_id, "
                " reply_to_msg_id, edit_deadline, edited_at, lots, llm_parsed) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.id, s.source, s.raw_text, s.symbol, s.direction, s.entry,
                 s.stop_loss, s.take_profit_1, s.take_profit_2, s.take_profit_3,
                 s.take_profit_4, s.take_profit_5, s.status,
                 s.error, s.received_at, s.message_id, s.reply_to_msg_id,
                 s.edit_deadline, s.edited_at, s.lots, 1 if s.llm_parsed else 0),
            )

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        with self.tx() as cur:
            row = cur.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return self._signal_from_row(row) if row else None

    def set_signal_status(self, signal_id: str, status: str, error: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE signals SET status = ?, error = ? WHERE id = ?",
                (status, error, signal_id),
            )

    def update_signal_levels(self, signal_id: str, tp1: Optional[float] = None,
                             sl: Optional[float] = None) -> None:
        """Update the TP1 and/or stop loss of a signal (group correction:
        \"TP1 4734 SL 4745\" replying to the signal)."""
        sets, params = [], []
        if tp1 is not None:
            sets.append("take_profit_1 = ?"); params.append(tp1)
        if sl is not None:
            sets.append("stop_loss = ?"); params.append(sl)
        if not sets:
            return
        params.append(signal_id)
        with self.tx() as cur:
            cur.execute(
                f"UPDATE signals SET {', '.join(sets)} WHERE id = ?", params
            )

    def list_signals(self, limit: int = 100) -> List[Signal]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM signals ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._signal_from_row(r) for r in rows]

    def last_signal_with_sl(self):
        """Sinal válido mais recente com entrada e SL preenchidos (para
        dimensionar o exemplo de lotes do risco%)."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT entry, stop_loss FROM signals "
                "WHERE status IN ('historical','awaiting_edit','pending','executed') "
                "AND entry IS NOT NULL AND stop_loss IS NOT NULL "
                "AND entry > 0 AND stop_loss > 0 AND ABS(entry - stop_loss) > 0 "
                "ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        return row

    def count_signals(self) -> int:
        with self.tx() as cur:
            row = cur.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
        return int(row["n"])

    def count_trades(self) -> int:
        with self.tx() as cur:
            row = cur.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
        return int(row["n"])

    def signals_since(self, day: str) -> List[Signal]:
        """Signals received on or after `day` (YYYY-MM-DD), oldest first."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM signals WHERE received_at >= ? ORDER BY received_at ASC",
                (day,),
            ).fetchall()
        return [self._signal_from_row(r) for r in rows]

    def awaiting_edit_signals(self) -> List[Signal]:
        """Signals still inside their edit window (status = awaiting_edit)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM signals WHERE status = 'awaiting_edit'"
            ).fetchall()
        return [self._signal_from_row(r) for r in rows]

    @staticmethod
    def _signal_from_row(row) -> Signal:
        return Signal(
            id=row["id"], source=row["source"], raw_text=row["raw_text"] or "",
            symbol=row["symbol"], direction=row["direction"], entry=row["entry"],
            stop_loss=row["stop_loss"], take_profit_1=row["take_profit_1"],
            take_profit_2=row["take_profit_2"], take_profit_3=row["take_profit_3"],
            take_profit_4=row["take_profit_4"], take_profit_5=row["take_profit_5"],
            status=row["status"],
            received_at=row["received_at"], error=row["error"],
            message_id=row["message_id"],
            reply_to_msg_id=row["reply_to_msg_id"] if "reply_to_msg_id" in row.keys() else None,
            edit_deadline=row["edit_deadline"],
            edited_at=row["edited_at"] if "edited_at" in row.keys() else None,
            lots=row["lots"] if "lots" in row.keys() else None,
            llm_parsed=bool(row["llm_parsed"] or 0),
        )

    def set_signal_edited_at(self, signal_id: str, edited_at: str) -> None:
        """Regista a hora em que o grupo editou a mensagem (última edição)."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE signals SET edited_at = ? WHERE id = ?",
                (edited_at, signal_id),
            )

    def edit_delay_stats(self, source_like: str = "", limit: int = 500) -> dict:
        """Distribuição do atraso (em segundos) entre a mensagem original e a
        última edição do grupo — para validar a janela de edição com dados.

        Só mensagens do Telegram com `message_id` (as que esperam edições) e
        com `edited_at` preenchido. Devolve min/méd/mediana/p90/máx + os
        últimos exemplos (símbolo, atraso, texto).
        """
        import statistics
        where = "edited_at IS NOT NULL AND message_id IS NOT NULL"
        params: list = []
        if source_like:
            where += " AND source LIKE ?"
            params.append(f"%{source_like}%")
        with self.tx() as cur:
            rows = cur.execute(
                f"SELECT symbol, received_at, edited_at, raw_text FROM signals "
                f"WHERE {where} ORDER BY edited_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        delays = []
        examples = []
        for r in rows:
            age = _iso_delta_seconds(r["received_at"], r["edited_at"])
            if age is None or age < 0:
                continue
            delays.append(age)
            examples.append({
                "symbol": r["symbol"],
                "delay_seconds": round(age, 1),
                "text": (r["raw_text"] or "").strip()[:60],
            })
        if not delays:
            return {"count": 0}
        delays_sorted = sorted(delays)
        n = len(delays_sorted)
        return {
            "count": n,
            "min_seconds": round(delays_sorted[0], 1),
            "median_seconds": round(delays_sorted[n // 2], 1),
            "mean_seconds": round(sum(delays_sorted) / n, 1),
            "p90_seconds": round(delays_sorted[int(n * 0.9) - 1], 1),
            "max_seconds": round(delays_sorted[-1], 1),
            "examples": examples[:8],
        }

    # ----------------------------------------------------- protect telemetry

    def insert_protect_latency(self, entry: dict) -> int:
        """Persist one monotonic-protect decision for paper/MT5 auditing."""
        fields = (
            "engine", "strategy_id", "signal_id", "trade_id", "message_id",
            "symbol", "direction", "auto_protection_at", "group_received_at",
            "auto_sl", "group_sl", "applied_sl", "winner", "delta_pips",
            "delta_seconds", "leg1_action", "status", "created_at",
        )
        values = tuple(entry.get(name) for name in fields)
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO protect_latency_log "
                f"({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                values,
            )
            return int(cur.lastrowid)

    def protect_latency_rows(self, strategy_id: str = "", engine: str = "",
                             limit: int = 500) -> List[dict]:
        """Return recent protect decisions, optionally filtered by strategy/engine."""
        clauses = []
        params: list = []
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if engine:
            clauses.append("engine = ?")
            params.append(engine)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        safe_limit = max(1, min(int(limit), 500))
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM protect_latency_log" + where +
                " ORDER BY group_received_at DESC, id DESC LIMIT ?",
                (*params, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def protect_latency_stats(self, strategy_id: str = "", engine: str = "",
                              limit: int = 500) -> dict:
        """Summarize latency decisions with the same p90 convention as edits."""
        import statistics
        rows = self.protect_latency_rows(strategy_id, engine, limit)
        seconds = sorted(float(row["delta_seconds"]) for row in rows
                         if row["delta_seconds"] is not None)
        pips = sorted(float(row["delta_pips"]) for row in rows
                      if row["delta_pips"] is not None)

        def p90(values: list[float]) -> Optional[float]:
            if not values:
                return None
            return values[int(len(values) * 0.9) - 1]

        def rounded_mean(values: list[float]) -> Optional[float]:
            return round(sum(values) / len(values), 2) if values else None

        return {
            "count": len(rows),
            "auto_wins": sum(row["winner"] == "auto" for row in rows),
            "group_wins": sum(row["winner"] == "group" for row in rows),
            "ties": sum(row["winner"] == "tie" for row in rows),
            "auto_not_triggered": sum(
                row["status"] == "auto_not_triggered" for row in rows
            ),
            "mean_seconds": rounded_mean(seconds),
            "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
            "p90_seconds": round(p90(seconds), 2) if seconds else None,
            "max_seconds": round(seconds[-1], 2) if seconds else None,
            "mean_pips": rounded_mean(pips),
            "p90_pips": round(p90(pips), 2) if pips else None,
            "max_pips": round(pips[-1], 2) if pips else None,
        }

    # ----------------------------------------------------- operations telemetry

    def record_exception(self, exc: BaseException, context: Optional[dict] = None,
                         signal_id: Optional[str] = None,
                         trade_id: Optional[str] = None,
                         created_at: str = "") -> int:
        """Persist an exception with its execution context.

        The logger remains the primary live diagnostic, but this row makes
        recurring failures searchable in the dashboard without parsing text
        logs. ``traceback`` is captured even when the helper is called outside
        an active ``except`` block.
        """
        import json
        import traceback
        from datetime import datetime, timezone
        if not isinstance(exc, BaseException):
            exc = Exception(str(exc))
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if not trace:
            trace = f"{type(exc).__name__}: {exc}"
        context_json = json.dumps(context or {}, ensure_ascii=False, default=str)
        stamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO operational_exceptions "
                "(created_at, exception_type, message, traceback, context_json, signal_id, trade_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (stamp, f"{type(exc).__module__}.{type(exc).__name__}",
                 str(exc), trace, context_json, signal_id, trade_id),
            )
            return int(cur.lastrowid)

    def exception_rows(self, limit: int = 100, since: str = "") -> List[dict]:
        import json
        clauses, params = [], []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        safe_limit = max(1, min(int(limit), 500))
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, created_at, exception_type, message, traceback, "
                "context_json, signal_id, trade_id FROM operational_exceptions" +
                where + " ORDER BY id DESC LIMIT ?", (*params, safe_limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["context"] = json.loads(item.pop("context_json") or "{}")
            except (ValueError, TypeError):
                item["context"] = {}
                item.pop("context_json", None)
            out.append(item)
        return out

    def exception_stats(self, days: int = 90, limit: int = 20) -> dict:
        from collections import Counter
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        rows = self.exception_rows(limit=max(1, min(int(limit), 100)), since=cutoff)
        with self.tx() as cur:
            total = int(cur.execute(
                "SELECT COUNT(*) FROM operational_exceptions WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0])
            types = cur.execute(
                "SELECT exception_type, COUNT(*) AS n FROM operational_exceptions "
                "WHERE created_at >= ? GROUP BY exception_type ORDER BY n DESC",
                (cutoff,),
            ).fetchall()
        return {
            "total": total,
            "days": int(days),
            "by_type": [{"exception_type": r["exception_type"], "count": int(r["n"])}
                        for r in types],
            "recent": rows,
        }

    def insert_spread_observation(self, observed_at: str, symbol: str,
                                  bid: float, ask: float, spread: float,
                                  spread_pips: float, source: str = "mt5") -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO spread_observations "
                "(observed_at, symbol, bid, ask, spread, spread_pips, source) "
                "VALUES (?,?,?,?,?,?,?)",
                (observed_at, symbol, bid, ask, spread, spread_pips, source),
            )
            return int(cur.lastrowid)

    def spread_monthly(self, days: int = 90) -> dict:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT substr(observed_at, 1, 7) AS month, symbol, source, "
                "COUNT(*) AS observations, AVG(spread_pips) AS avg_spread_pips, "
                "MIN(spread_pips) AS min_spread_pips, MAX(spread_pips) AS max_spread_pips "
                "FROM spread_observations WHERE observed_at >= ? "
                "GROUP BY month, symbol, source ORDER BY month ASC, symbol ASC",
                (cutoff,),
            ).fetchall()
            total = int(cur.execute(
                "SELECT COUNT(*) FROM spread_observations WHERE observed_at >= ?",
                (cutoff,),
            ).fetchone()[0])
        return {
            "days": int(days), "observations": total,
            "monthly": [{
                "month": r["month"], "symbol": r["symbol"], "source": r["source"],
                "observations": int(r["observations"]),
                "avg_spread_pips": round(float(r["avg_spread_pips"]), 4),
                "min_spread_pips": round(float(r["min_spread_pips"]), 4),
                "max_spread_pips": round(float(r["max_spread_pips"]), 4),
            } for r in rows],
        }

    def record_mt5_connection_event(self, event: str, reason: str = "",
                                    created_at: str = "", details: Optional[dict] = None) -> int:
        import json
        from datetime import datetime, timezone
        stamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO mt5_connection_events "
                "(created_at, event, reason, details_json) VALUES (?,?,?,?)",
                (stamp, event, reason, json.dumps(details or {}, ensure_ascii=False, default=str)),
            )
            return int(cur.lastrowid)

    def mt5_connection_weekly(self, days: int = 90) -> dict:
        from collections import defaultdict
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT created_at, event, reason FROM mt5_connection_events "
                "WHERE created_at >= ? ORDER BY created_at ASC", (cutoff,)
            ).fetchall()
        buckets = defaultdict(lambda: {"reconnect_attempts": 0, "reconnect_successes": 0,
                                       "reconnect_failures": 0, "disconnects": 0,
                                       "connects": 0, "events": 0})
        for row in rows:
            try:
                dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                week = (dt.astimezone(timezone.utc).date() - timedelta(days=dt.weekday())).isoformat()
            except (TypeError, ValueError):
                continue
            bucket = buckets[week]
            bucket["events"] += 1
            event = row["event"]
            if event == "reconnect_attempt": bucket["reconnect_attempts"] += 1
            elif event == "reconnect_success": bucket["reconnect_successes"] += 1
            elif event == "reconnect_failure": bucket["reconnect_failures"] += 1
            elif event == "disconnected": bucket["disconnects"] += 1
            elif event == "connected": bucket["connects"] += 1
        weekly = []
        for week in sorted(buckets):
            row = {"week_start": week, **buckets[week]}
            row["success_rate_pct"] = round(
                row["reconnect_successes"] / row["reconnect_attempts"] * 100, 1
            ) if row["reconnect_attempts"] else None
            weekly.append(row)
        return {"days": int(days), "weekly": weekly}

    def insert_mt5_modify_event(self, created_at: str, trade_id: str = "",
                                position_id: str = "", symbol: str = "", leg: Optional[int] = None,
                                kind: str = "modify", requested_sl: Optional[float] = None,
                                valid_sl: Optional[float] = None, retcode: Optional[int] = None,
                                accepted: bool = False, error: str = "") -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO mt5_modify_events "
                "(created_at, trade_id, position_id, symbol, leg, kind, requested_sl, "
                "valid_sl, retcode, accepted, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (created_at, trade_id, position_id, symbol, leg, kind, requested_sl,
                 valid_sl, retcode, 1 if accepted else 0, error),
            )
            return int(cur.lastrowid)

    def mt5_modify_weekly(self, days: int = 90) -> dict:
        from collections import defaultdict, Counter
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT created_at, retcode, accepted, kind FROM mt5_modify_events "
                "WHERE created_at >= ? ORDER BY created_at ASC", (cutoff,)
            ).fetchall()
        buckets = defaultdict(lambda: {"attempts": 0, "accepted": 0, "rejected": 0,
                                       "retcodes": Counter()})
        for row in rows:
            try:
                dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                week = (dt.astimezone(timezone.utc).date() - timedelta(days=dt.weekday())).isoformat()
            except (TypeError, ValueError):
                continue
            bucket = buckets[week]
            bucket["attempts"] += 1
            accepted = bool(row["accepted"])
            bucket["accepted"] += int(accepted)
            bucket["rejected"] += int(not accepted)
            if row["retcode"] is not None:
                bucket["retcodes"][str(row["retcode"])] += 1
        weekly = []
        for week in sorted(buckets):
            row = {"week_start": week, "attempts": buckets[week]["attempts"],
                   "accepted": buckets[week]["accepted"], "rejected": buckets[week]["rejected"],
                   "retcodes": dict(sorted(buckets[week]["retcodes"].items()))}
            row["rejection_rate_pct"] = round(
                row["rejected"] / row["attempts"] * 100, 1
            ) if row["attempts"] else 0.0
            weekly.append(row)
        total_attempts = sum(r["attempts"] for r in weekly)
        total_rejected = sum(r["rejected"] for r in weekly)
        return {
            "days": int(days), "weekly": weekly,
            "total_attempts": total_attempts, "total_accepted": total_attempts - total_rejected,
            "total_rejected": total_rejected,
            "rejection_rate_pct": round(total_rejected / total_attempts * 100, 1)
            if total_attempts else None,
        }

    def operational_metrics(self, days: int = 90) -> dict:
        """Combined Item 10 payload for the dashboard/report endpoint."""
        safe_days = max(1, min(int(days), 730))
        return {
            "ok": True,
            "period": {"days": safe_days},
            "exceptions": self.exception_stats(safe_days),
            "spread": self.spread_monthly(safe_days),
            "mt5": {
                "connections": self.mt5_connection_weekly(safe_days),
                "modifies": self.mt5_modify_weekly(safe_days),
            },
        }

    # ----------------------------------------------------------------- trades

    def insert_trade(self, t: Trade) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO trades "
                "(id, signal_id, strategy_id, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, risk_pct, lots_1, lots_2, lots_3, lots_4, lots_5, "
                " status, opened_at, closed_at, pnl, exit_reason) "                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.signal_id, t.strategy_id, t.symbol, t.direction, t.entry, t.stop_loss,

                 t.take_profit_1, t.take_profit_2, t.take_profit_3, t.take_profit_4,
                 t.take_profit_5, t.risk_pct, t.lots_1, t.lots_2, t.lots_3,
                 t.lots_4, t.lots_5, t.status, t.opened_at, t.closed_at, t.pnl,
                 t.exit_reason),
            )

    def set_trade_strategy(self, trade_id: str, strategy_id: str) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE trades SET strategy_id = ? WHERE id = ?",
                (strategy_id, trade_id),
            )

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        with self.tx() as cur:
            row = cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return self._trade_from_row(row) if row else None

    def open_trades(self) -> List[Trade]:
        with self.tx() as cur:
            rows = cur.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
        return [self._trade_from_row(r) for r in rows]

    def list_trades(self, limit: int = 100) -> List[Trade]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._trade_from_row(r) for r in rows]

    def trades_closed_since(self, day: str) -> List[Trade]:
        """Closed trades on or after `day` (YYYY-MM-DD), newest first."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades WHERE status = 'closed' AND closed_at >= ? "
                "ORDER BY closed_at DESC",
                (day,),
            ).fetchall()
        return [self._trade_from_row(r) for r in rows]

    def recent_closed_pnls(self, limit: int) -> List[float]:
        """P&L das últimas `limit` trades REAIS fechadas (ordem de fecho) —
        alimenta o modo defensivo da Fase 3 (PF das últimas N < 1.0 → trail
        apertado + time stop na auto promovida)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT pnl FROM trades WHERE status = 'closed' AND pnl IS NOT NULL "
                "ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?", (limit,)
            ).fetchall()
        return [float(r["pnl"] or 0.0) for r in rows]

    def trades_closed_on(self, day: str) -> List[Trade]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades WHERE status = 'closed' AND closed_at LIKE ?",
                (day + "%",),
            ).fetchall()
        return [self._trade_from_row(r) for r in rows]

    def realized_today(self, day: str) -> float:
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
                "WHERE status = 'closed' AND closed_at LIKE ?",
                (day + "%",),
            ).fetchone()
        return float(row["total"])

    def close_trade(self, trade_id: str, closed_at: str, pnl: float,
                    reason: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE trades SET status = 'closed', closed_at = ?, pnl = ?, "
                "exit_reason = ? WHERE id = ?",
                (closed_at, pnl, reason, trade_id),
            )

    @staticmethod
    def _trade_from_row(row) -> Trade:
        return Trade(
            id=row["id"], signal_id=row["signal_id"],
            strategy_id=row["strategy_id"] if "strategy_id" in row.keys() else None,
            symbol=row["symbol"],
            direction=row["direction"], entry=row["entry"], stop_loss=row["stop_loss"],
            take_profit_1=row["take_profit_1"], take_profit_2=row["take_profit_2"],
            take_profit_3=row["take_profit_3"] or 0.0,
            take_profit_4=row["take_profit_4"] or 0.0,
            take_profit_5=row["take_profit_5"] or 0.0,
            risk_pct=row["risk_pct"], lots_1=row["lots_1"], lots_2=row["lots_2"],
            lots_3=row["lots_3"] or 0.0, lots_4=row["lots_4"] or 0.0,
            lots_5=row["lots_5"] or 0.0,
            status=row["status"], opened_at=row["opened_at"],
            closed_at=row["closed_at"], pnl=row["pnl"] or 0.0,
            exit_reason=row["exit_reason"] if "exit_reason" in row.keys() else None,
        )

    # -------------------------------------------------------------- positions

    def insert_position(self, p: Position) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.trade_id, p.leg, p.symbol, p.direction, p.lots, p.entry,
                 p.stop_loss, p.take_profit, p.status, p.opened_at, p.closed_at,
                 p.close_price, p.pnl, p.exit_reason, p.exit_msg_id),
            )

    def positions_for_trade(self, trade_id: str) -> List[Position]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM positions WHERE trade_id = ? ORDER BY leg", (trade_id,)
            ).fetchall()
        return [self._position_from_row(r) for r in rows]

    def open_positions(self) -> List[Position]:
        with self.tx() as cur:
            rows = cur.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
        return [self._position_from_row(r) for r in rows]

    def close_position(self, pos_id: str, close_price: float, pnl: float, closed_at: str,
                       reason: Optional[str] = None, msg_id: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE positions SET status = 'closed', close_price = ?, pnl = ?, "
                "closed_at = ?, exit_reason = ?, exit_msg_id = ? WHERE id = ?",
                (close_price, pnl, closed_at, reason, msg_id, pos_id),
            )

    def move_position_sl(self, pos_id: str, new_sl: float) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE positions SET stop_loss = ? WHERE id = ?", (new_sl, pos_id))

    def update_position_entry(self, pos_id: str, entry: float) -> None:
        """Record the real fill price (MT5) for a position."""
        with self.tx() as cur:
            cur.execute("UPDATE positions SET entry = ? WHERE id = ?", (entry, pos_id))

    @staticmethod
    def _position_from_row(row) -> Position:
        return Position(
            id=row["id"], trade_id=row["trade_id"], leg=row["leg"], symbol=row["symbol"],
            direction=row["direction"], lots=row["lots"], entry=row["entry"],
            stop_loss=row["stop_loss"], take_profit=row["take_profit"],
            status=row["status"], opened_at=row["opened_at"], closed_at=row["closed_at"],
            close_price=row["close_price"], pnl=row["pnl"] or 0.0,
            exit_reason=row["exit_reason"] if "exit_reason" in row.keys() else None,
            exit_msg_id=row["exit_msg_id"] if "exit_msg_id" in row.keys() else None,
        )

    # ------------------------------------------------- pending management

    def insert_pending_management(self, rec: dict) -> None:
        """Queue a management instruction whose target signal has no open
        trade yet (still in the edit window / pending). Applied when that
        signal's trade opens (see `BotState._apply_pending_management`)."""
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO pending_management "
                "(id, signal_id, reply_to_msg_id, kind, tp, sl, raw_text, source, "
                " received_at, created_at, applied) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (rec["id"], rec["signal_id"], rec["reply_to_msg_id"],
                 rec["kind"], rec.get("tp"), rec.get("sl"),
                 rec.get("raw_text", ""), rec.get("source", ""),
                 rec.get("received_at", ""), rec.get("created_at", "")),
            )

    def pending_management_for(self, signal_id: str) -> List[dict]:
        """Unapplied management instructions waiting for this signal."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM pending_management "
                "WHERE reply_to_msg_id = ? AND applied = 0",
                (str(signal_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_pending_management_applied(self, rec_id: str) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE pending_management SET applied = 1 WHERE id = ?", (rec_id,)
            )

    # --------------------------------------------------------- summaries

    def insert_group_summary(self, source: str, fields: dict, created_at: str,
                             message_id: str = "") -> None:
        """Store a parsed group summary (daily/weekly win-rate report).

        Idempotent per Telegram `message_id`: re-processing the same message
        (history reload, edit) updates the row instead of duplicating it.
        """
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO group_summaries "
                "(message_id, source, period_start, period_end, trades_sent, "
                " winning, stop_loss, breakevens, win_rate, pips, raw_text, "
                " created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                " trades_sent=excluded.trades_sent, winning=excluded.winning, "
                " stop_loss=excluded.stop_loss, breakevens=excluded.breakevens, "
                " win_rate=excluded.win_rate, pips=excluded.pips, "
                " raw_text=excluded.raw_text",
                (message_id or None, source, fields.get("period_start"),
                 fields.get("period_end"), fields.get("trades_sent", 0),
                 fields.get("winning", 0), fields.get("stop_loss", 0),
                 fields.get("breakevens", 0), fields.get("win_rate", 0.0),
                 fields.get("pips", 0), fields.get("raw_text", ""),
                 created_at),
            )

    def latest_group_summaries(self, limit: int = 10) -> List[dict]:
        """Most recent parsed group summaries (dashboard ground-truth block)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM group_summaries ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_group_summaries(self, source_like: str = "", limit: int = 20) -> List[dict]:
        """Recent summaries, optionally filtered by a source substring.

        Ordered by the covered period (most recent first), so the first row
        is the report for the latest day/week — the one the Edge tab shows
        as the group's ground truth. Rows without a period (daily reports
        with no date in the text) sort by id.
        """
        order = "COALESCE(period_end, period_start, '0000') DESC, id DESC"
        with self.tx() as cur:
            if source_like:
                rows = cur.execute(
                    f"SELECT * FROM group_summaries WHERE source LIKE ? "
                    f"ORDER BY {order} LIMIT ?", (f"%{source_like}%", limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    f"SELECT * FROM group_summaries ORDER BY {order} LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- notifications

    def insert_notification(self, kind: str, text: str, created_at: str,
                            action: Optional[str] = None) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO notifications (kind, text, created_at, action) "
                "VALUES (?,?,?,?)",
                (kind, text, created_at, action),
            )
            return int(cur.lastrowid)

    def list_notifications(self, limit: int = 100) -> List[Notification]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Notification(id=r["id"], kind=r["kind"], text=r["text"],
                         created_at=r["created_at"],
                         action=r["action"] if "action" in r.keys() else None)
            for r in rows
        ]

    # ------------------------------------------------- ignored (auditoria)

    def record_ignored(self, message_id: str, source: str, group_name: str,
                       raw_text: str, reason: str, received_at: str) -> bool:
        """Audit: store a group message the bot decided to ignore (chat,
        not a signal nor management). Deduped per (message_id, source) and
        pruned to the newest 1000 rows."""
        inserted = False
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO ignored_messages "
                "(message_id, source, group_name, raw_text, reason, received_at) "
                "VALUES (?,?,?,?,?,?)",
                (str(message_id) if message_id is not None else "", source,
                 group_name, raw_text, reason, received_at),
            )
            inserted = cur.rowcount > 0
            cur.execute(
                "DELETE FROM ignored_messages WHERE id NOT IN "
                "(SELECT id FROM ignored_messages ORDER BY id DESC LIMIT 1000)"
            )
        return inserted

    def _ignored_filters(self, q: str = "", days: int = 0):
        """WHERE clauses + args for the ignored audit (keyword e período).
        O dia compara só a parte da data do ISO (substr), imune a sufixos
        de fuso horário; days=0 = sem limite temporal."""
        conds, args = [], []
        if q:
            conds.append("raw_text LIKE ?")
            args.append(f"%{q}%")
        if days and days > 0:
            cutoff = (date.today() - timedelta(days=int(days))).isoformat()
            conds.append("substr(received_at, 1, 10) >= ?")
            args.append(cutoff)
        return (" AND ".join(conds), args) if conds else ("", [])

    def recent_ignored(self, limit: int = 100, q: str = "", days: int = 0) -> List[dict]:
        where, args = self._ignored_filters(q, days)
        sql = ("SELECT id, message_id, source, group_name, raw_text, reason, "
               "received_at FROM ignored_messages")
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id DESC LIMIT ?"
        with self.tx() as cur:
            rows = cur.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def count_ignored(self, q: str = "", days: int = 0) -> int:
        where, args = self._ignored_filters(q, days)
        sql = "SELECT COUNT(*) AS n FROM ignored_messages"
        if where:
            sql += " WHERE " + where
        with self.tx() as cur:
            row = cur.execute(sql, args).fetchone()
        return int(row["n"])

    def delete_ignored(self, message_id: str, source: str = "") -> None:
        """Remove um registo da auditoria (ex.: a mensagem foi editada e
        tornou-se sinal, já não é conversa ignorada)."""
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM ignored_messages WHERE message_id = ? AND source = ?",
                (str(message_id), source),
            )

    # ------------------------------------------------- listener restarts

    def record_listener_restart(self, reason: str, started_at: str = "") -> None:
        """Regista um (re)início do listener — o watchdog chama isto sempre
        que spawna o processo (auditoria dos reinícios automáticos)."""
        from app.execution.engine import _now_iso
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO listener_restarts (started_at, reason) VALUES (?,?)",
                (started_at or _now_iso(), reason),
            )

    def list_listener_restarts(self, limit: int = 10) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, started_at, reason FROM listener_restarts "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_listener_restarts(self) -> int:
        """Apaga TODO o histórico de reinícios (ação manual da dashboard —
        a tab Diagnóstico tem um botão com confirmação)."""
        with self.tx() as cur:
            cur.execute("DELETE FROM listener_restarts")
            return cur.rowcount

    def purge_old_listener_restarts(self, days: int = 30) -> int:
        """Apaga registos de reinícios com mais de `days` dias — a tabela
        `listener_restarts` não pode crescer sem limite (purge periódico
        no watchdog do servidor). Compara só a parte da data do ISO
        (substr), imune a sufixos de fuso horário."""
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM listener_restarts WHERE substr(started_at, 1, 10) < ?",
                (cutoff,),
            )
            return cur.rowcount

    # ------------------------------------------------- paper trading forward

    def paper_balance(self, strategy_id: str) -> Optional[float]:
        """Saldo simulado atual de uma estratégia de teste (None se nunca abriu)."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT balance FROM paper_accounts WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        return float(row["balance"]) if row else None

    def paper_accounts(self) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_accounts ORDER BY strategy_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def ensure_paper_account(self, strategy_id: str, start_balance: float) -> float:
        """Cria a conta virtual da estratégia se ainda não existir (começa com
        `start_balance` — 1000 € no arranque) e devolve o saldo atual."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            row = cur.execute(
                "SELECT balance FROM paper_accounts WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
            if row:
                return float(row["balance"])
            cur.execute(
                "INSERT INTO paper_accounts (strategy_id, balance, start_balance, "
                "opened_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (strategy_id, start_balance, start_balance, now, now),
            )
        return start_balance

    def update_paper_balance(self, strategy_id: str, balance: float) -> None:
        from datetime import datetime, timezone
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_accounts SET balance = ?, updated_at = ? "
                "WHERE strategy_id = ?",
                (balance, datetime.now(timezone.utc).isoformat(timespec="seconds"), strategy_id),
            )

    def insert_paper_trade(self, t: Trade) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO paper_trades "
                "(id, strategy_id, signal_id, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, lots_1, lots_2, lots_3, lots_4, lots_5, status, "
                " opened_at, closed_at, pnl, exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.strategy_id, t.signal_id, t.symbol, t.direction, t.entry,
                 t.stop_loss, t.take_profit_1, t.take_profit_2, t.take_profit_3,
                 t.take_profit_4, t.take_profit_5, t.lots_1, t.lots_2, t.lots_3,
                 t.lots_4, t.lots_5, t.status, t.opened_at, t.closed_at, t.pnl,
                 t.exit_reason),
            )

    def insert_paper_position(self, p: Position) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO paper_positions "
                "(id, trade_id, leg, symbol, direction, lots, entry, stop_loss, "
                " take_profit, status, opened_at, closed_at, close_price, pnl, "
                " exit_reason, exit_msg_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.trade_id, p.leg, p.symbol, p.direction, p.lots, p.entry,
                 p.stop_loss, p.take_profit, p.status, p.opened_at, p.closed_at,
                 p.close_price, p.pnl, p.exit_reason, p.exit_msg_id),
            )

    def paper_positions_for_trade(self, trade_id: str) -> List[Position]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_positions WHERE trade_id = ?", (trade_id,)
            ).fetchall()
        return [Position(**dict(r)) for r in rows]

    def paper_open_trades(self, strategy_id: str) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE strategy_id = ? AND status = 'open'",
                (strategy_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_open_trades_by_signal(self, signal_id: str) -> List[dict]:
        """Trades de papel abertas de um sinal (qualquer estratégia) — para
        aplicar a gestão do grupo à corrida paralela."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE signal_id = ? AND status = 'open'",
                (signal_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_trade_for_signal(self, signal_id: str, strategy_id: str) -> Optional[dict]:
        """Trade de papel fechada de um sinal (contrafactual sem gestão — a
        variante `nova` da corrida paralela)."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT * FROM paper_trades WHERE signal_id = ? AND strategy_id = ? "
                "AND status = 'closed' ORDER BY closed_at DESC LIMIT 1",
                (signal_id, strategy_id),
            ).fetchone()
        return dict(row) if row else None

    def paper_trades_for_signal(self, signal_id: str, limit: int = 100) -> List[dict]:
        """Todas as trades paper de um sinal, abertas ou fechadas.

        O divergence report precisa desta consulta porque a auto e a
        referência podem fechar em ordens diferentes; limitar às posições
        ainda abertas perde a divergência quando a auto sai primeiro.
        """
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE signal_id = ? "
                "ORDER BY opened_at DESC LIMIT ?", (signal_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_trades(self, strategy_id: str, limit: int = 100) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE strategy_id = ? "
                "ORDER BY opened_at DESC LIMIT ?", (strategy_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_trade_stats(self, strategy_id: str) -> dict:
        """Resumo das trades simuladas fechadas de uma estratégia."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT pnl FROM paper_trades WHERE strategy_id = ? AND status='closed'",
                (strategy_id,),
            ).fetchall()
        pnls = [float(r["pnl"] or 0.0) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        n = len(pnls)
        return {
            "closed": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "pnl": round(sum(pnls), 2),
            "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                               if losses and sum(losses) != 0 else None),
        }

    def paper_trades_opened_since(self, strategy_id: str, since: str) -> int:
        """Nº de trades de papel Abertas de uma estratégia desde `since` — o
        ritmo de sinais/dia (cada sinal executado abre uma trade de papel em
        cada estratégia da corrida). Usado para o ETA da janela da Fase 3."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COUNT(*) n FROM paper_trades "
                "WHERE strategy_id = ? AND opened_at >= ?",
                (strategy_id, since),
            ).fetchone()
        return int(row["n"] or 0)

    def paper_trades_opened_by_day(self, strategy_id: str, since: str) -> dict:
        """Nº de trades de papel abertas por DIA (chave = data ISO) desde
        `since` — a mini-curva do ritmo de sinais/dia do painel (tendência
        que alimenta o ETA da Fase 3)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT substr(opened_at, 1, 10) AS day, COUNT(*) n "
                "FROM paper_trades WHERE strategy_id = ? AND opened_at >= ? "
                "GROUP BY day ORDER BY day",
                (strategy_id, since),
            ).fetchall()
        return {str(r["day"]): int(r["n"] or 0) for r in rows}

    def paper_closed_recent(self, strategy_id: str, limit: int) -> List[float]:
        """P&L das últimas `limit` trades de papel RESOLVIDAS (fechadas) da
        estratégia, por ordem de fecho — alimenta o modo defensivo da corrida
        paper (PF das últimas N < 1.0 → trail apertado + time stop)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT pnl FROM paper_trades WHERE strategy_id = ? "
                "AND status='closed' AND pnl IS NOT NULL "
                "ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
                (strategy_id, limit),
            ).fetchall()
        return [float(r["pnl"] or 0.0) for r in rows]

    def paper_realized_today(self, strategy_id: str, day: str) -> float:
        """P&L realizado HOJE (dia civil, UTC) das trades de papel fechadas da
        estratégia — para o kill diário da corrida paper espelhar o da
        produção (risk.engine.daily_loss_exceeded)."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM paper_trades "
                "WHERE strategy_id = ? AND status='closed' AND closed_at LIKE ?",
                (strategy_id, day + "%"),
            ).fetchone()
        return float(row["total"] or 0.0)

    def close_paper_position(self, pos_id: str, price: float, pnl: float, now: str,
                             reason: Optional[str] = None,
                             msg_id: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_positions SET status='closed', close_price=?, pnl=?, "
                "closed_at=?, exit_reason=?, exit_msg_id=? WHERE id=?",
                (price, pnl, now, reason, msg_id, pos_id),
            )

    def move_paper_position_sl(self, pos_id: str, new_sl: float) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_positions SET stop_loss=? WHERE id=?", (new_sl, pos_id)
            )

    def close_paper_trade(self, trade_id: str, now: str, pnl: float,
                          reason: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_trades SET status='closed', closed_at=?, pnl=?, "
                "exit_reason=? WHERE id=?", (now, pnl, reason, trade_id),
            )

    # ------------------------------------------------- balance history

    def insert_paper_balance_history(self, strategy_id: str, ts: str, balance: float) -> None:
        """Snapshot do saldo (paper ou real) de uma estratégia na altura `ts`.
        UNIQUE(strategy_id, ts) — gravar o mesmo instante duas vezes é idempotente."""
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO paper_balance_history (strategy_id, ts, balance) "
                "VALUES (?, ?, ?)", (strategy_id, ts, balance),
            )

    def paper_balance_history(self, strategy_id: str, since: str = "") -> List[dict]:
        """Snapshots de saldo de uma estratégia (mais recente primeiro); com
        `since` (ISO) devolve só os posteriores a essa data (ordem ascendente)."""
        with self.tx() as cur:
            if since:
                rows = cur.execute(
                    "SELECT ts, balance FROM paper_balance_history "
                    "WHERE strategy_id = ? AND ts >= ? ORDER BY ts ASC",
                    (strategy_id, since),
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT ts, balance FROM paper_balance_history "
                    "WHERE strategy_id = ? ORDER BY ts DESC LIMIT 500",
                    (strategy_id,),
                ).fetchall()
                rows = list(reversed(rows))
        return [dict(r) for r in rows]
