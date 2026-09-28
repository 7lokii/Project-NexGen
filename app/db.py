"""SQLite persistence layer.

Thin, dependency-free storage for signals, trades, positions, notifications
and the key/value config table. All writes go through a single lock so the
async event loop and background tasks never race on the connection.
"""

import json
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
    applied INTEGER DEFAULT 0,
    paper_applied INTEGER DEFAULT 0,
    shadow_applied INTEGER DEFAULT 0,
    tp2_applied INTEGER DEFAULT 0,
    be INTEGER DEFAULT 0
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
    created_at TEXT,
    correction_of_message_id TEXT,
    corrected_at TEXT,
    is_correction INTEGER DEFAULT 0
);

-- R2: per-trade recounts from the group ("Signal #1: Sell at 4357 … +110
-- pips") — structured external ground truth used to audit the bot's own
-- real fills of the same day. One row per Telegram message (dedup).
CREATE TABLE IF NOT EXISTS trade_recounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    source TEXT,
    signal_number INTEGER,
    direction TEXT,
    entry REAL,
    excursion_price REAL,
    excursion_dir TEXT,
    reversal_price REAL,
    pips REAL,
    raw_text TEXT,
    received_at TEXT,
    created_at TEXT
);

-- Passive narrative from the group (progress pips, mentioned levels and
-- inferred targets). These rows never authorize execution.
CREATE TABLE IF NOT EXISTS group_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    source TEXT,
    signal_id TEXT,
    kind TEXT NOT NULL,
    reported_pips REAL,
    reported_level REAL,
    inferred_tp REAL,
    confidence TEXT,
    feed_price REAL,
    feed_delta REAL,
    feed_divergence INTEGER DEFAULT 0,
    raw_text TEXT,
    received_at TEXT,
    created_at TEXT,
    UNIQUE(message_id, source, kind)
);

CREATE INDEX IF NOT EXISTS idx_group_audits_received
    ON group_audits(received_at DESC);

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
    error TEXT,
    retcodes TEXT,
    auto_trading_off INTEGER,
    exit_reason TEXT,
    lane_id TEXT
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
    exit_msg_id TEXT,
    lane_id TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    text TEXT,
    created_at TEXT,
    action TEXT,
    lane TEXT
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
    exit_reason TEXT,
    origin TEXT NOT NULL DEFAULT 'race'
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

CREATE TABLE IF NOT EXISTS loop_stall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'stall',  -- stall (event loop) | phase (causa instrumentada)
    phase TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL,
    threshold REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_stall_ts ON loop_stall_events(ts);

CREATE TABLE IF NOT EXISTS protection_states (
    engine TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(engine, origin, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_protection_states_updated
    ON protection_states(updated_at);

CREATE TABLE IF NOT EXISTS pending_delete_states (
    lane TEXT NOT NULL,
    position_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(lane, position_id)
);

-- SL de gestão de grupo (kind "group") que o broker recusou (ex.: 10016
-- stops inválidos) ou que não pôde ser enviado (terminal sem ligação):
-- a intenção fica em fila e o tick volta a tentar nos próximos ticks
-- quando o nível se tornar válido (ver MT5Engine._retry_pending_sl_moves).
CREATE TABLE IF NOT EXISTS sl_move_pending (
    lane TEXT NOT NULL,
    position_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(lane, position_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_delete_states_updated
    ON pending_delete_states(updated_at);

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

CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '',
    signal_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_execution_events_at
    ON execution_events(at, id);CREATE INDEX IF NOT EXISTS idx_execution_events_trade
    ON execution_events(trade_id, at);

-- Resolved flips from the recount batch re-check: persisted so the
-- /auditoria can show the evolution of feed-offset corrections over time
-- (a verdict flipping implausible/feed_divergence → plausible with the
-- measured daily GC=F↔spot offset). Read-only for execution.
CREATE TABLE IF NOT EXISTS recount_batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    window TEXT NOT NULL DEFAULT 'daily',
    total INTEGER NOT NULL DEFAULT 0,
    rechecked INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    flips_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_recount_batch_runs_ran
    ON recount_batch_runs(ran_at DESC);

CREATE TABLE IF NOT EXISTS be_buffer_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    avg_spread_pips REAL NOT NULL,
    buffer_pips REAL NOT NULL,
    UNIQUE(day, symbol)
);




CREATE TABLE IF NOT EXISTS evidence_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(kind, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_evidence_archives_period
    ON evidence_archives(period_start, period_end);
"""

class DB:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL + synchronous=NORMAL: o journal DELETE fsyncava o ficheiro
            # inteiro (647MB com o journal de execução ligado) a CADA commit
            # — os inserts de quotes do tick (1×/s) levavam 15–580ms cada e
            # congelavam o event loop (`snapshot.build` 2–6s, stalls do
            # healthcheck). WAL escreve para um ficheiro aparte: commits
            # ~0ms, leituras não bloqueiam escritas e o checkpoint é
            # assíncrono. Seguro: sem corrupção em crash (só perde as
            # últimas transações num corte de energia — aceitável para o
            # journal/telemetria).
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
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
        for col in ("entry_zone_low", "entry_zone_high"):
            if col not in {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
        for col, kind in (("pending_kind", "TEXT"), ("pending_meta", "TEXT")):
            if col not in {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {kind}")
        # marcador de revive (reabertura da janela): o sinal guarda de que
        # estado veio e o motivo gravado da recusa/erro ANTERIOR — a tabela
        # de histórico mostra o marcador mesmo depois de o sinal executar.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}
        for col in ("revived_from", "revived_reason", "revived_at"):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} TEXT")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "strategy_id" not in existing:
            self.conn.execute("ALTER TABLE trades ADD COLUMN strategy_id TEXT")
        for col in ("take_profit_3", "take_profit_4", "take_profit_5",
                    "lots_3", "lots_4", "lots_5"):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT")
        for col in ("entry_zone_low", "entry_zone_high", "market_price_at_open",
                    "entry_distance", "entry_guard", "execution_mode"):
            if col not in {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}:
                kind = "REAL" if col in ("entry_zone_low", "entry_zone_high", "market_price_at_open", "entry_distance") else "TEXT"
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {kind}")
        # segunda lane de execução (conta demo paralela): as trades distinguem-se
        # por lane_id. Linhas antigas ficam "main" (comportamento atual).
        if "lane_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}:
            self.conn.execute("ALTER TABLE trades ADD COLUMN lane_id TEXT DEFAULT 'main'")
        # recusas do broker ficam duravelmente gravadas (trace de auditoria:
        # error/retcodes/auto_trading_off — ver _send_open_orders sent==0).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for col, kind in (("error", "TEXT"), ("retcodes", "TEXT"),
                          ("auto_trading_off", "INTEGER")):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {kind}")
        # positions gained exit attribution (gestão do grupo → razão do fecho).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "exit_reason" not in existing:
            self.conn.execute("ALTER TABLE positions ADD COLUMN exit_reason TEXT")
            self.conn.execute("ALTER TABLE positions ADD COLUMN exit_msg_id TEXT")
        if "lane_id" not in existing:
            self.conn.execute("ALTER TABLE positions ADD COLUMN lane_id TEXT DEFAULT 'main'")
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
        # group_summaries gained R1 correction/audit metadata. Corrections
        # keep their own Telegram message while the target row is rewritten.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(group_summaries)")}
        if "correction_of_message_id" not in existing:
            self.conn.execute("ALTER TABLE group_summaries ADD COLUMN correction_of_message_id TEXT")
        if "corrected_at" not in existing:
            self.conn.execute("ALTER TABLE group_summaries ADD COLUMN corrected_at TEXT")
        # trade_recounts gained the persisted candle-check verdict (batch
        # re-check: `POST /api/recounts/recheck-all` grava o veredicto em
        # lote para o operador ver o que já foi re-verificado e quando).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(trade_recounts)")}
        if "candle_check" not in existing:
            self.conn.execute("ALTER TABLE trade_recounts ADD COLUMN candle_check TEXT")
        if "checked_at" not in existing:
            self.conn.execute("ALTER TABLE trade_recounts ADD COLUMN checked_at TEXT")
        # group_summaries gained is_correction (summary corrections keep the
        # original row and mark it corrected instead of deleting it).
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(group_summaries)")}
        if "is_correction" not in existing:
            self.conn.execute("ALTER TABLE group_summaries ADD COLUMN is_correction INTEGER DEFAULT 0")
        # notifications gained an optional action (ex.: "promote:<strategy>")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(notifications)")}
        if "action" not in existing:
            self.conn.execute("ALTER TABLE notifications ADD COLUMN action TEXT")
        # notifications gained an optional lane ("main"/"tp2") so the feed
        # can distinguish events from each execution account. Old rows stay
        # NULL, which the UI treats as "main".
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(notifications)")}
        if "lane" not in existing:
            self.conn.execute("ALTER TABLE notifications ADD COLUMN lane TEXT")
        # pending_management gained sl (nível para kind='move_sl' quando a
        # trade ainda não abriu). A tabela nasceu sem a coluna — sem isto o
        # INSERT com `sl` rebenta com 500 e a instrução de gestão perde-se.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}
        if "sl" not in existing:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN sl REAL")
        if "tp2_applied" not in existing:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN tp2_applied INTEGER DEFAULT 0")
        if "be" not in existing:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN be INTEGER DEFAULT 0")
        if "paper_applied" not in existing:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN paper_applied INTEGER DEFAULT 0")
        if "shadow_applied" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN shadow_applied INTEGER DEFAULT 0")
        if "tp2_applied" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN tp2_applied INTEGER DEFAULT 0")
        if "be" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN be INTEGER DEFAULT 0")
        if "lane_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_management)")}:
            self.conn.execute("ALTER TABLE pending_management ADD COLUMN lane_id TEXT DEFAULT 'main'")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_trades)")}
        if "origin" not in existing:
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN origin TEXT NOT NULL DEFAULT 'race'")
        # Fase 1 — ordens pendentes na zona: cada posição guarda o ticket da
        # ordem pendente MT5 (status `pending_fill`) e a trade o entry_style.
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "order_ticket" not in existing:
            self.conn.execute("ALTER TABLE positions ADD COLUMN order_ticket INTEGER")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "entry_style" not in existing:
            self.conn.execute("ALTER TABLE trades ADD COLUMN entry_style TEXT")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_trades)")}
        if "entry_style" not in existing:
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN entry_style TEXT")
        if "market_price_at_open" not in existing:
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN market_price_at_open REAL")
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_positions)")}
        if "order_ticket" not in existing:
            self.conn.execute("ALTER TABLE paper_positions ADD COLUMN order_ticket INTEGER")

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

    # ------------------------------------------------ protection state

    def save_protection_state(self, engine: str, trade_id: str, state: dict,
                              origin: str = "", updated_at: str = "") -> None:
        """Persist the live auto-management state atomically.

        Peaks/valleys, TP1 time, protection levels and trigger timestamps are
        state, not merely cache: a restart must resume from the same monotonic
        policy instead of silently starting a new trade phase.
        """
        import json
        from datetime import datetime, timezone
        stamp = updated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(state, ensure_ascii=False, default=str, sort_keys=True)
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO protection_states "
                "(engine, origin, trade_id, state_json, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(engine, origin, trade_id) DO UPDATE SET "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (engine, origin or "", trade_id, payload, stamp),
            )

    def load_protection_state(self, engine: str, trade_id: str,
                              origin: str = "") -> Optional[dict]:
        """Load a persisted auto state and restore integer leg/time buckets."""
        import json
        with self.tx() as cur:
            row = cur.execute(
                "SELECT state_json FROM protection_states "
                "WHERE engine = ? AND origin = ? AND trade_id = ?",
                (engine, origin or "", trade_id),
            ).fetchone()
        if not row:
            return None
        try:
            state = json.loads(row["state_json"] or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        for name in ("auto_sl_by_leg", "auto_protection_at_by_leg"):
            if isinstance(state.get(name), dict):
                state[name] = {
                    int(key) if str(key).isdigit() else key: value
                    for key, value in state[name].items()
                }
        if isinstance(state.get("atr_buckets"), dict):
            state["atr_buckets"] = {
                int(key) if str(key).lstrip("-").isdigit() else key: value
                for key, value in state["atr_buckets"].items()
            }
        return state

    def delete_protection_state(self, engine: str, trade_id: str,
                                origin: str = "") -> None:
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM protection_states WHERE engine = ? AND origin = ? AND trade_id = ?",
                (engine, origin or "", trade_id),
            )

    # ------------------------------------------------- pending delete watchdog

    def save_pending_delete_state(self, lane: str, position_id: str,
                                  state: dict) -> None:
        """Persist the per-leg pending-delete watchdog state (watchdog
        `pending_delete_stuck`): attempts, delete intent and alert
        timestamps. Survives a server restart so the Diagnóstico block and
        the re-alert continue where they left off."""
        import json
        from datetime import datetime, timezone
        payload = json.dumps(state, ensure_ascii=False, default=str, sort_keys=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO pending_delete_states "
                "(lane, position_id, state_json, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(lane, position_id) DO UPDATE SET "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (lane, position_id, payload, stamp),
            )

    def load_pending_delete_states(self, lane: str) -> Dict[str, dict]:
        """All watchdog state rows for one lane, keyed by position id."""
        import json
        out: Dict[str, dict] = {}
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT position_id, state_json FROM pending_delete_states "
                "WHERE lane = ?", (lane,),
            ).fetchall()
        for row in rows:
            try:
                state = json.loads(row["state_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(state, dict):
                out[row["position_id"]] = state
        return out

    def delete_pending_delete_state(self, lane: str, position_id: str) -> None:
        """Drop the watchdog row when the leg resolves (delete ok, filled,
        cancelled or the trade closed) — never left orphaned in the table."""
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM pending_delete_states "
                "WHERE lane = ? AND position_id = ?",
                (lane, position_id),
            )

    # ------------------------------------------------- pending SL move retry

    def save_pending_sl_move(self, lane: str, position_id: str,
                             state: dict) -> None:
        """Persist a group-management SL move that the broker refused (or
        that could not be sent because the terminal was offline). The tick
        retries it until the level becomes valid — the same lifecycle as
        `pending_delete_states`, so the intent survives a server restart."""
        import json
        from datetime import datetime, timezone
        payload = json.dumps(state, ensure_ascii=False, default=str, sort_keys=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO sl_move_pending "
                "(lane, position_id, state_json, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(lane, position_id) DO UPDATE SET "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (lane, position_id, payload, stamp),
            )

    def load_pending_sl_moves(self, lane: str) -> Dict[str, dict]:
        """All staged SL-move intents for one lane, keyed by position id."""
        import json
        out: Dict[str, dict] = {}
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT position_id, state_json FROM sl_move_pending "
                "WHERE lane = ?", (lane,),
            ).fetchall()
        for row in rows:
            try:
                state = json.loads(row["state_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(state, dict):
                out[row["position_id"]] = state
        return out

    def delete_pending_sl_move(self, lane: str, position_id: str) -> None:
        """Drop the staged intent once the modify is accepted or the leg
        closes — never left orphaned in the table."""
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM sl_move_pending "
                "WHERE lane = ? AND position_id = ?",
                (lane, position_id),
            )

    def clear_pending_sl_move(self, position_id: str) -> None:
        """Lane-agnostic cleanup (position ids are globally unique)."""
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM sl_move_pending WHERE position_id = ?",
                (position_id,),
            )

    # ------------------------------------------------------- execution journal

    def archive_evidence(self, kind: str, period_start: str, period_end: str,
                         payload: dict, created_at: str = "") -> dict:
        """Persist an immutable-ish snapshot of an evidence window.

        Archiving is deliberately separate from the live journal: it stores a
        report for later export and never changes trades, config or broker
        state. Re-archiving the same kind/window replaces the snapshot so an
        operator can refresh it after the UTC window closes.
        """
        import json
        from datetime import datetime, timezone
        stamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO evidence_archives "
                "(kind, period_start, period_end, created_at, payload_json) "
                "VALUES (?,?,?,?,?) ON CONFLICT(kind, period_start, period_end) DO UPDATE SET "
                "created_at=excluded.created_at, payload_json=excluded.payload_json",
                (kind, period_start, period_end, stamp, encoded),
            )
            row = cur.execute(
                "SELECT id, kind, period_start, period_end, created_at FROM evidence_archives "
                "WHERE kind=? AND period_start=? AND period_end=?",
                (kind, period_start, period_end),
            ).fetchone()
        return dict(row)

    def evidence_archives(self, kind: str = "", limit: int = 50) -> List[dict]:
        """List archived evidence metadata, newest windows first."""
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_limit = max(1, min(int(limit), 200))
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, kind, period_start, period_end, created_at "
                "FROM evidence_archives" + where +
                " ORDER BY period_end DESC, id DESC LIMIT ?",
                (*params, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence_archive(self, archive_id: int) -> Optional[dict]:
        """Return one archived report, including its decoded payload."""
        import json
        with self.tx() as cur:
            row = cur.execute(
                "SELECT id, kind, period_start, period_end, created_at, payload_json "
                "FROM evidence_archives WHERE id=?", (int(archive_id),)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            item["payload"] = {}
            item.pop("payload_json", None)
        return item

    def record_execution_event(self, at: str, kind: str, *, engine: str = "",
                               symbol: str = "", trade_id: str = "",
                               signal_id: str = "", payload: Optional[dict] = None) -> int:
        """Persist one opt-in execution observation for deterministic replay."""
        import json
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO execution_events "
                "(at, kind, engine, symbol, trade_id, signal_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (at, kind, engine or "", symbol or "", trade_id or "",
                 signal_id or "", json.dumps(payload or {}, ensure_ascii=False,
                                              default=str, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def execution_events(self, *, since: str = "", until: str = "",
                         kind: str = "", limit: int = 5000) -> List[dict]:
        """Read journal rows in chronological order, bounded for the API."""
        import json
        clauses, params = [], []
        if since:
            clauses.append("at >= ?")
            params.append(since)
        if until:
            clauses.append("at <= ?")
            params.append(until)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        safe_limit = max(1, min(int(limit), 20000))
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, at, kind, engine, symbol, trade_id, signal_id, payload_json "
                "FROM execution_events" + where + " ORDER BY at ASC, id ASC LIMIT ?",
                (*params, safe_limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
                item.pop("payload_json", None)
            out.append(item)
        return out

    def execution_tape(self, *, since: str = "", until: str = "",
                       signal_id: str = "", trade_id: str = "",
                       limit: int = 10000) -> dict:
        """Export a replay tape from the journal plus structured signals.

        The runtime journal historically recorded quotes and management, not
        the full Signal object. The export joins those observations back to
        ``signals`` so an old capture is still replayable. A signal-specific
        export includes all quotes in that trade's lifetime and only management
        messages routed to that signal; an unrestricted export keeps all
        journal events in the requested interval.
        """
        import json
        from datetime import datetime, timezone

        requested_signal = str(signal_id or "")
        requested_trade = str(trade_id or "")
        if requested_trade and not requested_signal:
            with self.tx() as cur:
                row = cur.execute(
                    "SELECT signal_id FROM trades WHERE id = ?",
                    (requested_trade,),
                ).fetchone()
            requested_signal = str(row["signal_id"] or "") if row else ""

        signals = self.list_signals(200000)
        signal_by_id = {str(s.id): s for s in signals}
        message_to_signal = {
            str(s.message_id): str(s.id) for s in signals if s.message_id
        }
        selected = signal_by_id.get(requested_signal) if requested_signal else None
        start = since or (selected.received_at if selected else "")
        if requested_signal and not until:
            with self.tx() as cur:
                row = cur.execute(
                    "SELECT closed_at FROM trades WHERE signal_id = ? "
                    "ORDER BY closed_at DESC LIMIT 1",
                    (requested_signal,),
                ).fetchone()
            until = str(row["closed_at"] or "") if row and row["closed_at"] else ""

        journal = self.execution_events(since=start, until=until, limit=limit)
        rows = []
        target_ids = {requested_signal} if requested_signal else set()
        for row in journal:
            payload = dict(row.get("payload") or {})
            target = str(payload.get("target_signal_id") or "")
            if row.get("kind") == "management":
                reply = payload.get("reply_to_msg_id")
                if not target and reply is not None:
                    target = message_to_signal.get(str(reply), "")
                if not target and row.get("signal_id") in signal_by_id:
                    target = str(row.get("signal_id"))
                if requested_signal and target != requested_signal:
                    continue
                if target:
                    row["signal_id"] = target
                    payload["target_signal_id"] = target
            row["sequence"] = int(row.get("id") or len(rows))
            row["payload"] = payload
            rows.append(row)

        # Include the complete structured signal even when the first journal
        # event was a management message. For an unrestricted tape, include
        # valid executable signals in the requested interval; management and
        # summary rows are not sent to the replay engine as entry signals.
        signal_rows = []
        for sig in signals:
            if sig.status == "management":
                continue
            if requested_signal and sig.id != requested_signal:
                continue
            if start and (sig.received_at or "") < start:
                continue
            if until and (sig.received_at or "") > until:
                continue
            if not requested_signal and journal:
                # Avoid exporting the entire historical signal table when a
                # narrow journal capture was requested: only signals referenced
                # by management events are required for a complete tape.
                refs = {str(r.get("signal_id") or "") for r in rows}
                if refs and sig.id not in refs and not any(
                    r.get("kind") == "quote" and (sig.received_at or "") <= r.get("at", "")
                    for r in rows
                ):
                    continue
            if not (sig.symbol and sig.direction and sig.entry and sig.stop_loss
                    and sig.take_profit_1 and sig.take_profit_2):
                continue
            signal_rows.append({
                "at": sig.received_at or "", "kind": "signal",
                "engine": "runtime", "symbol": sig.symbol,
                "trade_id": "", "signal_id": sig.id,
                "payload": {
                    "id": sig.id, "source": sig.source,
                    "raw_text": sig.raw_text, "message_id": sig.message_id,
                    "reply_to_msg_id": sig.reply_to_msg_id,
                    "symbol": sig.symbol, "direction": sig.direction,
                    "entry": sig.entry, "stop_loss": sig.stop_loss,
                    "take_profit_1": sig.take_profit_1,
                    "take_profit_2": sig.take_profit_2,
                    "take_profit_3": sig.take_profit_3,
                    "take_profit_4": sig.take_profit_4,
                    "take_profit_5": sig.take_profit_5,
                    "received_at": sig.received_at,
                },
            })

        # Signal first, quote next, management last at the same timestamp. A
        # fresh sequence is preferable to SQLite ids because the joined signal
        # rows do not have an execution_events id.
        combined = signal_rows + rows
        priority = {"signal": 0, "quote": 1, "management": 2}
        combined.sort(key=lambda r: (
            str(r.get("at") or ""), priority.get(str(r.get("kind")), 9),
            int(r.get("sequence") or 0),
        ))
        tape = []
        for sequence, row in enumerate(combined, 1):
            tape.append({
                "at": row.get("at") or "", "kind": row.get("kind") or "",
                "sequence": sequence, "symbol": row.get("symbol") or "",
                "signal_id": row.get("signal_id") or "",
                "payload": row.get("payload") or {},
            })
        safe_limit = max(1, min(int(limit), 20000))
        return {
            "events": tape[:safe_limit],
            "meta": {
                "signal_id": requested_signal or None,
                "trade_id": requested_trade or None,
                "journal_events": len(rows),
                "signal_events": len(signal_rows),
                "total": len(tape),
                "truncated": len(tape) > safe_limit,
                "source": "execution_events + signals",
            },
        }

    def execution_event_stats(self, days: int = 7) -> dict:
        from collections import Counter
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT kind, engine, COUNT(*) AS n FROM execution_events "
                "WHERE at >= ? GROUP BY kind, engine ORDER BY n DESC",
                (cutoff,),
            ).fetchall()
            total = int(cur.execute(
                "SELECT COUNT(*) FROM execution_events WHERE at >= ?", (cutoff,)
            ).fetchone()[0])
        return {
            "days": int(days),
            "total": total,
            "by_kind": [{"kind": r["kind"], "engine": r["engine"], "count": int(r["n"])}
                        for r in rows],
        }

    # ---------------------------------------------------------------- signals

    def insert_signal(self, s: Signal) -> None:
        import json
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO signals "
                "(id, source, raw_text, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, status, error, received_at, message_id, "
                " reply_to_msg_id, edit_deadline, edited_at, lots, llm_parsed, "
                " entry_zone_low, entry_zone_high, pending_kind, pending_meta, "
                " revived_from, revived_reason, revived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?)",
                (s.id, s.source, s.raw_text, s.symbol, s.direction, s.entry,
                 s.stop_loss, s.take_profit_1, s.take_profit_2, s.take_profit_3,
                 s.take_profit_4, s.take_profit_5, s.status,
                 s.error, s.received_at, s.message_id, s.reply_to_msg_id,
                 s.edit_deadline, s.edited_at, s.lots, 1 if s.llm_parsed else 0,
                 s.entry_zone_low, s.entry_zone_high, s.pending_kind,
                 json.dumps(s.pending_meta, ensure_ascii=False, default=str)
                 if s.pending_meta else None,
                 s.revived_from, s.revived_reason, s.revived_at),
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

    def set_signal_pending(self, signal_id: str, error: str, kind: str,
                           meta: Optional[dict] = None) -> None:
        """Marca um sinal como pendente de confirmação explícita, com o
        motivo estruturado (entry_guard/min_lot_risk) para a dashboard
        mostrar risco configurado vs perda máxima estimada antes de aprovar."""
        import json
        with self.tx() as cur:
            cur.execute(
                "UPDATE signals SET status = 'pending', error = ?, "
                "pending_kind = ?, pending_meta = ? WHERE id = ?",
                (error, kind,
                 json.dumps(meta or {}, ensure_ascii=False, default=str),
                 signal_id),
            )

    def clear_signal_pending(self, signal_id: str) -> None:
        """Limpa o marcador de confirmação pendente quando o sinal executa."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE signals SET pending_kind = NULL, pending_meta = NULL "
                "WHERE id = ?", (signal_id,),
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

    def list_pending_signals(self, limit: int = 100) -> List[Signal]:
        """Sinais `pending` (guards de zona/lote) para a expiração automática
        do modo autónomo — nunca ficam à espera de um operador."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM signals WHERE status = 'pending' "
                "ORDER BY received_at DESC LIMIT ?", (limit,)
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
        import json
        pending_meta = None
        if "pending_meta" in row.keys() and row["pending_meta"]:
            try:
                pending_meta = json.loads(row["pending_meta"])
            except (TypeError, ValueError):
                pending_meta = None
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
            entry_zone_low=row["entry_zone_low"] if "entry_zone_low" in row.keys() else None,
            entry_zone_high=row["entry_zone_high"] if "entry_zone_high" in row.keys() else None,
            pending_kind=row["pending_kind"] if "pending_kind" in row.keys() else None,
            pending_meta=pending_meta,
            revived_from=(row["revived_from"] if "revived_from" in row.keys() else None),
            revived_reason=(row["revived_reason"] if "revived_reason" in row.keys() else None),
            revived_at=(row["revived_at"] if "revived_at" in row.keys() else None),
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
        metric = self.post_execution_edit_metric()
        return {
            "count": n,
            "min_seconds": round(delays_sorted[0], 1),
            "median_seconds": round(delays_sorted[n // 2], 1),
            "mean_seconds": round(sum(delays_sorted) / n, 1),
            "p90_seconds": round(delays_sorted[int(n * 0.9) - 1], 1),
            "max_seconds": round(delays_sorted[-1], 1),
            "examples": examples[:8],
            **metric,
        }

    def post_execution_edit_metric(self) -> dict:
        """Métrica contínua do Item 12: edições a sinais que executaram sem
        janela (primeiro parse completo) vs quantos foram editados depois.

        Só mensagens do Telegram live (com `message_id`, sem `historical`)
        que nunca entraram na janela (`edit_deadline IS NULL` = completas no
        primeiro parse → executaram já). A taxa `post_execution_edit_rate` é a
        fração dessas que recebeu uma edição posterior (`edited_at`); o alerta
        do tick dispara quando sobe acima de 5% com amostra mínima.
        """
        base = (
            "status = 'executed' AND message_id IS NOT NULL "
            "AND edit_deadline IS NULL AND status != 'historical' "
            "AND status != 'management'"
        )
        with self.tx() as cur:
            total = cur.execute(
                f"SELECT COUNT(*) FROM signals WHERE {base}"
            ).fetchone()[0]
            edited = cur.execute(
                f"SELECT COUNT(*) FROM signals WHERE {base} AND edited_at IS NOT NULL"
            ).fetchone()[0]
        rate = round(edited / total, 4) if total else 0.0
        return {
            "post_execution_edited": int(edited),
            "post_execution_total": int(total),
            "post_execution_edit_rate": rate,
            "post_execution_edit_pct": round(rate * 100.0, 2),
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

    def avg_spread_pips(self, symbol: str, days: int = 7) -> Optional[float]:
        """Média do spread (em pips) de um símbolo nos últimos N dias.

        Fonte: `spread_observations` (amostras 1×/min do MT5Engine quando
        ligado). Read-only; devolve None sem amostras (o chamador decide o
        fallback — o buffer BE+ usa o valor estático da estratégia).
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            row = cur.execute(
                "SELECT AVG(spread_pips) FROM spread_observations "
                "WHERE symbol = ? AND observed_at >= ?",
                (symbol, cutoff),
            ).fetchone()
        value = row[0] if row else None
        return float(value) if value is not None else None

    def spread_symbols(self, days: int = 7) -> list:
        """Símbolos com observações de spread nos últimos N dias (read-only)."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT DISTINCT symbol FROM spread_observations "
                "WHERE observed_at >= ? ORDER BY symbol",
                (cutoff,),
            ).fetchall()
        return [r["symbol"] for r in rows]

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
                " (id, signal_id, strategy_id, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, risk_pct, lots_1, lots_2, lots_3, lots_4, lots_5, "
                " status, opened_at, closed_at, pnl, error, retcodes, "
                " auto_trading_off, exit_reason, entry_zone_low, "
                " entry_zone_high, market_price_at_open, entry_distance, entry_guard, "
                " execution_mode, lane_id, entry_style) VALUES (?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?)",
                (t.id, t.signal_id, t.strategy_id, t.symbol, t.direction, t.entry, t.stop_loss,

                 t.take_profit_1, t.take_profit_2, t.take_profit_3, t.take_profit_4,
                 t.take_profit_5, t.risk_pct, t.lots_1, t.lots_2, t.lots_3,
                 t.lots_4, t.lots_5, t.status, t.opened_at, t.closed_at, t.pnl,
                 t.error, json.dumps(t.retcodes) if t.retcodes else None,
                 1 if t.auto_trading_off else 0
                 if t.auto_trading_off is not None else None,
                 t.exit_reason, t.entry_zone_low, t.entry_zone_high,
                 t.market_price_at_open, t.entry_distance, t.entry_guard,
                 t.execution_mode, t.lane_id, t.entry_style),
            )

    def set_trade_refusal(self, trade_id: str, error: Optional[str] = None,
                          retcodes: Optional[list] = None,
                          auto_trading_off: Optional[bool] = None) -> None:
        """Persiste o motivo de uma recusa do broker na trade (trace durável
        para auditoria/replay: error + retcodes + flag AutoTrading OFF)."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE trades SET error = ?, retcodes = ?, "
                "auto_trading_off = ? WHERE id = ?",
                (error, json.dumps(retcodes) if retcodes else None,
                 1 if auto_trading_off else 0
                 if auto_trading_off is not None else None,
                 trade_id),
            )

    def set_trade_strategy(self, trade_id: str, strategy_id: str) -> None:
        """Marca a estratégia de uma trade já inserida (ex.: smoke test da
        lane 2 grava `smoke_test` para a analytics nunca ler o teste como
        resultado de uma estratégia real)."""
        with self.tx() as cur:
            cur.execute("UPDATE trades SET strategy_id = ? WHERE id = ?",
                        (strategy_id, trade_id))

    def refused_trades(self, limit: int = 10) -> List[dict]:
        """Últimas recusas do broker: trades com `error` gravado OU com a
        assinatura phantom (fechadas sem `exit_reason`, pnl 0.0 — o cleanup
        do envio recusado). Trace de auditoria read-only para as páginas."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, signal_id, lane_id, symbol, direction, opened_at, "
                "closed_at, pnl, exit_reason, error, retcodes, "
                "auto_trading_off FROM trades WHERE status = 'closed' AND "
                "(COALESCE(error, '') != '' OR "
                " (COALESCE(exit_reason, '') = '' AND pnl = 0.0)) "
                "ORDER BY opened_at DESC LIMIT ?", (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["retcodes"] = (json.loads(d["retcodes"])
                              if d.get("retcodes") else None)
            d["auto_trading_off"] = (
                bool(d["auto_trading_off"])
                if d.get("auto_trading_off") is not None else None)
            out.append(d)
        return out

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

    def trades_for_signal(self, signal_id: str) -> List[Trade]:
        """Todas as trades reais de um sinal (uma por lane em execução
        normal) — para a mensagem WhatsApp dual e o confronto de lanes."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades WHERE signal_id = ? ORDER BY opened_at",
                (signal_id,),
            ).fetchall()
        return [self._trade_from_row(r) for r in rows]

    def dry_run_realized_pnl(self, lane_id: str) -> float:
        """P&L realizado das trades fechadas em MT5 dry-run de uma lane —
        SUM SQL (O(1)) para o resumo simulado do card da conta. Linhas sem
        lane_id (legado) pertencem à principal, não à 2ª conta."""
        lane_clause = ("lane_id = 'main' OR lane_id IS NULL OR lane_id = ''"
                       if lane_id == "main" else "lane_id = 'tp2'")
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE status = 'closed' AND execution_mode = 'mt5-dry' "
                f"AND ({lane_clause})",
            ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

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

    def recent_phantom_null_retcodes(self, minutes: int = 30, lane: str = "tp2",
                                     limit: int = 10) -> List[tuple]:
        """Trades fechadas recentes com assinatura de retcode NULL ("?"):
        fechadas sem `exit_reason` (phantom) e com pelo menos uma perna cujo
        retcode ficou a "?" (broker respondeu SEM retcode — worker/terminal
        sem resposta válida). Devolve [(trade_id, [retcodes])] para o tick
        alertar uma vez por trade (read-only)."""
        import json
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        rows = []
        try:
            with self.tx() as cur:
                q = ("SELECT id, retcodes FROM trades "
                     "WHERE status = 'closed' AND closed_at >= ? "
                     "AND (exit_reason IS NULL OR exit_reason = '') "
                     "AND retcodes IS NOT NULL AND retcodes != '' "
                     "AND lane_id = ? "
                     "ORDER BY closed_at DESC LIMIT ?")
                found = cur.execute(q, (since, lane, limit)).fetchall()
            for r in found:
                try:
                    rcs = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or [])
                except (ValueError, TypeError):
                    rcs = []
                if any(str(rc) == "?" for rc in (rcs or [])):
                    rows.append((r[0], [str(rc) for rc in (rcs or [])]))
        except Exception:
            return []
        return rows

    def trades_closed_since(self, day: str) -> List[Trade]:
        """Closed trades on or after `day` (YYYY-MM-DD), newest first."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades WHERE status = 'closed' AND closed_at >= ? "
                "ORDER BY closed_at DESC",
                (day,),
            ).fetchall()
        return [self._trade_from_row(r) for r in rows]

    def trades_opened_since(self, since: str) -> List[Trade]:
        """Trades opened on or after `since` (ISO/YYYY-MM-DD), newest first.

        Usado pelo relatório 'porquê sem trade' para a análise zona-vs-preço
        dos fills REAIS (execution_mode mt5-real/mt5-dry) — read-only.
        """
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM trades WHERE opened_at >= ? ORDER BY opened_at DESC",
                (since,),
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
            error=row["error"] if "error" in row.keys() else None,
            retcodes=(json.loads(row["retcodes"])
                      if "retcodes" in row.keys() and row["retcodes"]
                      else None),
            auto_trading_off=(bool(int(row["auto_trading_off"]))
                              if "auto_trading_off" in row.keys()
                              and row["auto_trading_off"] is not None
                              else None),
            exit_reason=row["exit_reason"] if "exit_reason" in row.keys() else None,
            entry_zone_low=row["entry_zone_low"] if "entry_zone_low" in row.keys() else None,
            entry_zone_high=row["entry_zone_high"] if "entry_zone_high" in row.keys() else None,
            market_price_at_open=row["market_price_at_open"] if "market_price_at_open" in row.keys() else None,
            entry_distance=row["entry_distance"] if "entry_distance" in row.keys() else None,
            entry_guard=row["entry_guard"] if "entry_guard" in row.keys() else None,
            execution_mode=row["execution_mode"] if "execution_mode" in row.keys() else None,
            lane_id=(row["lane_id"] or "main") if "lane_id" in row.keys() else "main",
            entry_style=row["entry_style"] if "entry_style" in row.keys() else None,
        )

    # -------------------------------------------------------------- positions

    def insert_position(self, p: Position) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO positions "
                "(id, trade_id, leg, symbol, direction, lots, entry, stop_loss, "
                " take_profit, status, opened_at, closed_at, close_price, pnl, "
                " exit_reason, exit_msg_id, lane_id, order_ticket) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.trade_id, p.leg, p.symbol, p.direction, p.lots, p.entry,
                 p.stop_loss, p.take_profit, p.status, p.opened_at, p.closed_at,
                 p.close_price, p.pnl, p.exit_reason, p.exit_msg_id, p.lane_id,
                 p.order_ticket),
            )

    def set_position_order_ticket(self, pos_id: str, ticket: int) -> None:
        """Guarda o ticket da ordem pendente MT5 de uma posição (Fase 1)."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE positions SET order_ticket = ? WHERE id = ?",
                (ticket, pos_id),
            )

    def fill_position(self, pos_id: str, opened_at: str) -> None:
        """Fase 1: ordem pendente preenchida → a posição passa a `open`."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE positions SET status = 'open', opened_at = ?, "
                "order_ticket = NULL WHERE id = ?", (opened_at, pos_id),
            )

    def fill_paper_position(self, pos_id: str, opened_at: str) -> None:
        """Fase 1: ordem pendente de papel preenchida → posição `open`."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_positions SET status = 'open', opened_at = ?, "
                "order_ticket = NULL WHERE id = ?", (opened_at, pos_id),
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

    def closed_pending_positions(self, since: str) -> list:
        """Pendentes órfãs/expiradas/canceladas: posições fechadas com a
        razão de pendente que nunca preencheu, juntas ao trade (zona,
        entrada, modo de execução) — para o relatório 'porquê sem trade'
        mostrar as ordens na zona que não entraram e porque o preço não
        voltou ao nível da entrada.

        Read-only — devolve dicts (uma por perna pendente fechada), nunca
        altera linhas.
        """
        reason_filters = (", ".join("?" for _ in (
            "pending_expired", "pending_orphan", "pending_cancelled")))
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT p.id AS pos_id, p.leg, p.symbol, p.direction, p.entry, "
                "       p.opened_at, p.closed_at, p.exit_reason, p.order_ticket, "
                "       t.signal_id, t.status AS trade_status, t.execution_mode, "
                "       t.entry_zone_low, t.entry_zone_high, t.entry_style "
                "FROM positions p JOIN trades t ON t.id = p.trade_id "
                "WHERE p.status = 'closed' AND p.exit_reason IN (" + reason_filters + ") "
                "  AND p.closed_at >= ? "
                "ORDER BY p.closed_at DESC",
                (*("pending_expired", "pending_orphan", "pending_cancelled"), since),
            ).fetchall()
        return [dict(r) for r in rows]

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
            lane_id=(row["lane_id"] or "main") if "lane_id" in row.keys() else "main",
            order_ticket=(row["order_ticket"] if "order_ticket" in row.keys() else None),
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
                " received_at, created_at, applied, paper_applied, shadow_applied, "
                " tp2_applied, be) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,0,?)",
                (rec["id"], rec["signal_id"], rec["reply_to_msg_id"],
                 rec["kind"], rec.get("tp"), rec.get("sl"),
                 rec.get("raw_text", ""), rec.get("source", ""),
                 rec.get("received_at", ""), rec.get("created_at", ""),
                 1 if rec.get("be") else 0),
            )

    def pending_management_for(self, signal_id: str, engine: str = "real") -> List[dict]:
        """Unapplied management instructions waiting for this signal.

        ``applied`` is the real-execution marker kept for compatibility;
        normal paper and shadow paper each have their own marker so delivering
        a queued instruction to one virtual stream never consumes it before
        the other stream opens.
        """
        column = {
            "paper": "paper_applied",
            "shadow": "shadow_applied",
            "tp2": "tp2_applied",
        }.get(engine, "applied")
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM pending_management "
                f"WHERE reply_to_msg_id = ? AND {column} = 0",
                (str(signal_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_pending_management_applied(self, rec_id: str, engine: str = "real") -> None:
        column = {
            "paper": "paper_applied",
            "shadow": "shadow_applied",
            "tp2": "tp2_applied",
        }.get(engine, "applied")
        with self.tx() as cur:
            cur.execute(
                f"UPDATE pending_management SET {column} = 1 WHERE id = ?", (rec_id,)
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

    def resolve_group_summary(self, source: str = "", reply_to_msg_id: str = "",
                              period_start: str = "", period_end: str = "") -> dict:
        """Resolve one summary for an R1 correction without guessing.

        A Telegram reply is exact. Without it, an explicit period is accepted
        only when it identifies one current (non-correction) row. Ambiguous or
        missing targets return ``{"summary": None, "ambiguous": ...}``.
        """
        source = source or ""
        with self.tx() as cur:
            if reply_to_msg_id:
                row = cur.execute(
                    "SELECT * FROM group_summaries WHERE message_id = ? "
                    "AND is_correction = 0 ORDER BY id DESC LIMIT 1",
                    (str(reply_to_msg_id),),
                ).fetchone()
                if row:
                    return {"summary": dict(row), "ambiguous": False, "method": "reply"}
            clauses = ["is_correction = 0"]
            params: list = []
            if source:
                clauses.append("source = ?")
                params.append(source)
            if period_start:
                clauses.append("period_start = ?")
                params.append(period_start)
            if period_end:
                clauses.append("period_end = ?")
                params.append(period_end)
            if not period_start and not period_end:
                return {"summary": None, "ambiguous": False, "method": "none"}
            rows = cur.execute(
                "SELECT * FROM group_summaries WHERE " + " AND ".join(clauses) +
                " ORDER BY id DESC", tuple(params),
            ).fetchall()
        if len(rows) == 1:
            return {"summary": dict(rows[0]), "ambiguous": False, "method": "period"}
        return {"summary": None, "ambiguous": len(rows) > 1, "method": "period"}

    def apply_group_summary_correction(self, target: dict, correction_message_id: str,
                                       source: str, fields: dict, raw_text: str,
                                       created_at: str) -> dict:
        """Persist an R1 correction and rewrite the target summary atomically."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE group_summaries SET trades_sent=?, winning=?, stop_loss=?, "
                "breakevens=?, win_rate=?, pips=?, corrected_at=? WHERE id=?",
                (fields.get("trades_sent", 0), fields.get("winning", 0),
                 fields.get("stop_loss", 0), fields.get("breakevens", 0),
                 fields.get("win_rate", 0.0), fields.get("pips", 0),
                 created_at, target["id"]),
            )
            cur.execute(
                "INSERT OR REPLACE INTO group_summaries "
                "(message_id, source, period_start, period_end, trades_sent, winning, "
                "stop_loss, breakevens, win_rate, pips, raw_text, created_at, "
                "correction_of_message_id, corrected_at, is_correction) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (str(correction_message_id or "") or None, source,
                 fields.get("period_start") or target.get("period_start"),
                 fields.get("period_end") or target.get("period_end"),
                 fields.get("trades_sent", 0), fields.get("winning", 0),
                 fields.get("stop_loss", 0), fields.get("breakevens", 0),
                 fields.get("win_rate", 0.0), fields.get("pips", 0), raw_text,
                 created_at, target.get("message_id"), created_at),
            )
            row = cur.execute("SELECT * FROM group_summaries WHERE id = ?",
                              (target["id"],)).fetchone()
        return dict(row)

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

    # ------------------------------------------------------------ recounts

    def insert_trade_recount(self, source: str, fields: dict, raw_text: str,
                             received_at: str, message_id: str = "") -> int:
        """Store a parsed per-trade recount (R2 external fill audit).

        Idempotent per Telegram `message_id` — re-processing the same message
        (history reload) updates the row instead of duplicating it. Returns
        the row id.
        """
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO trade_recounts "
                "(message_id, source, signal_number, direction, entry, "
                " excursion_price, excursion_dir, reversal_price, pips, "
                " raw_text, received_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                " source=excluded.source, signal_number=excluded.signal_number, "
                " direction=excluded.direction, entry=excluded.entry, "
                " excursion_price=excluded.excursion_price, "
                " excursion_dir=excluded.excursion_dir, "
                " reversal_price=excluded.reversal_price, pips=excluded.pips, "
                " raw_text=excluded.raw_text, received_at=excluded.received_at",
                (message_id or None, source, fields.get("signal_number"),
                 fields.get("direction"), fields.get("entry"),
                 fields.get("excursion_price"), fields.get("excursion_dir"),
                 fields.get("reversal_price"), fields.get("pips"),
                 raw_text, received_at, received_at),
            )
            return int(cur.lastrowid)

    def list_trade_recounts(self, source_like: str = "", limit: int = 200) -> List[dict]:
        """Recent recounts, newest first, optionally filtered by source."""
        with self.tx() as cur:
            if source_like:
                rows = cur.execute(
                    "SELECT * FROM trade_recounts WHERE source LIKE ? "
                    "ORDER BY received_at DESC, id DESC LIMIT ?",
                    (f"%{source_like}%", limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT * FROM trade_recounts "
                    "ORDER BY received_at DESC, id DESC LIMIT ?", (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def recount_by_message_id(self, message_id: str) -> Optional[dict]:
        with self.tx() as cur:
            row = cur.execute(
                "SELECT * FROM trade_recounts WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
        return dict(row) if row else None

    def record_group_audit(self, *, message_id: str = "", source: str = "",
                           signal_id: str = "", kind: str,
                           reported_pips: Optional[float] = None,
                           reported_level: Optional[float] = None,
                           inferred_tp: Optional[float] = None,
                           confidence: str = "",
                           feed_price: Optional[float] = None,
                           feed_delta: Optional[float] = None,
                           feed_divergence: bool = False,
                           raw_text: str = "", received_at: str = "") -> int:
        """Persist passive group narrative idempotently.

        These events are evidence only: no caller may use this row to open,
        close or modify a position automatically.
        """
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO group_audits "
                "(message_id, source, signal_id, kind, reported_pips, "
                "reported_level, inferred_tp, confidence, feed_price, feed_delta, "
                "feed_divergence, raw_text, received_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_id, source, kind) DO UPDATE SET "
                "signal_id=excluded.signal_id, reported_pips=excluded.reported_pips, "
                "reported_level=excluded.reported_level, inferred_tp=excluded.inferred_tp, "
                "confidence=excluded.confidence, feed_price=excluded.feed_price, "
                "feed_delta=excluded.feed_delta, feed_divergence=excluded.feed_divergence, "
                "raw_text=excluded.raw_text, received_at=excluded.received_at",
                (str(message_id or ""), source or "", signal_id or "", kind,
                 reported_pips, reported_level, inferred_tp, confidence or "",
                 feed_price, feed_delta, 1 if feed_divergence else 0,
                 raw_text, received_at, received_at),
            )
            row = cur.execute(
                "SELECT id FROM group_audits WHERE message_id = ? AND source = ? AND kind = ?",
                (str(message_id or ""), source or "", kind),
            ).fetchone()
            return int(row["id"])

    def list_group_audits(self, limit: int = 100) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM group_audits ORDER BY received_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- notifications

    def set_recount_verdict(self, recount_id: int, candle_check: dict,
                            checked_at: str) -> None:
        """Persiste o veredicto do candle-check de um recount (R2 batch).

        O `POST /api/recounts/recheck-all` refaz o check com candles frescos
        e o offset mais recente e grava o resultado em JSON nesta coluna;
        `checked_at` regista quando. Read-only para a execução."""
        import json
        payload = json.dumps(candle_check, ensure_ascii=False, default=str)
        with self.tx() as cur:
            cur.execute(
                "UPDATE trade_recounts SET candle_check = ?, checked_at = ? "
                "WHERE id = ?", (payload, checked_at, recount_id),
            )

    # ------------------------------------------------- recount batch history

    def record_recount_batch(self, run: dict, flips: list = None) -> None:
        """Persiste uma corrida do re-check em lote (diário/semanal) para o
        /auditoria mostrar o contador 'resolvidos' e o histórico de flips
        (data, recount, de → para) ao longo do tempo. Read-only p/ execução."""
        import json as _json
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO recount_batch_runs "
                "(ran_at, window, total, rechecked, changed, resolved, errors, "
                "flips_json) VALUES (?,?,?,?,?,?,?,?)",
                (run.get("ran_at", ""), str(run.get("window", "daily")),
                 int(run.get("total", 0)), int(run.get("rechecked", 0)),
                 int(run.get("changed", 0)), int(run.get("resolved", 0)),
                 int(run.get("errors", 0)),
                 _json.dumps(flips or [], ensure_ascii=False, default=str)),
            )

    def list_recount_batches(self, limit: int = 60) -> List[dict]:
        """Corridas do lote de recounts por ordem cronológica inversa, com os
        flips resolvidos (veredicto que mudou de implausível/feed → plausível
        graças ao offset diário do GC=F)."""
        import json as _json
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM recount_batch_runs "
                "ORDER BY ran_at DESC, id DESC LIMIT ?", (int(limit),))
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["flips"] = _json.loads((r["flips_json"] or "[]") or "[]")
                except (ValueError, TypeError):
                    d["flips"] = []
                d.pop("flips_json", None)
                out.append(d)
            return out

    # ---------------------------------------------------------- notifications

    def insert_notification(self, kind: str, text: str, created_at: str,
                            action: Optional[str] = None,
                            lane: Optional[str] = None) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO notifications (kind, text, created_at, action, lane) "
                "VALUES (?,?,?,?,?)",
                (kind, text, created_at, action, lane or ""),
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
                         action=r["action"] if "action" in r.keys() else None,
                         lane=(r["lane"] or "") if "lane" in r.keys() else "")
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

    def purge_old_exceptions(self, days: int = 365) -> int:
        """Retenção bounded de `operational_exceptions`: apaga linhas com
        mais de `days` dias (default 365, política em maintenance.py). O
        rate-limit por episódio já impede centenas de linhas idênticas por
        perna presa, mas um episódio por perna/dia pode ainda crescer sem
        limite — o purge periódico (watchdog do servidor + arranque) mantém
        a tabela bounded. Compara só a parte da data do ISO (substr), imune
        a sufixos de fuso horário."""
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM operational_exceptions WHERE substr(created_at, 1, 10) < ?",
                (cutoff,),
            )
            return cur.rowcount

    # ----------------------------------------------- event loop stalls

    def record_loop_stall_event(self, ts: str, kind: str, phase: str,
                                duration: float, threshold: float) -> None:
        """Regista um congelamento do event loop (kind='stall') ou uma fase
        on-loop que demorou ≥ limiar (kind='phase' — a causa provável ao lado
        do sintoma). Fita persistida para o Diagnóstico explicar os "sem
        resposta HTTP" do healthcheck com dados, em vez de investigação a
        posteriori."""
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO loop_stall_events "
                "(ts, kind, phase, duration, threshold) VALUES (?,?,?,?,?)",
                (ts, str(kind), str(phase), float(duration), float(threshold)),
            )

    def recent_loop_stalls(self, limit: int = 12) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT id, ts, kind, phase, duration, threshold "
                "FROM loop_stall_events ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def loop_stall_count_since(self, iso_since: str) -> int:
        """Quantos registos (stalls + fases lentas) desde `iso_since` — o
        bloco do Diagnóstico mostra o total das últimas 24h."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS n FROM loop_stall_events WHERE ts >= ?",
                (iso_since,),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def purge_old_loop_stalls(self, days: int = 14) -> int:
        """Retenção bounded de `loop_stall_events` (default 14 dias, purge
        no watchdog do servidor + arranque) — a tabela não pode crescer sem
        limite. Compara só a parte da data do ISO (substr), imune a sufixos
        de fuso horário."""
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM loop_stall_events WHERE substr(ts, 1, 10) < ?",
                (cutoff,),
            )
            return cur.rowcount

    def purge_old_execution_events(self, days: int = 30) -> int:
        """Retenção bounded do journal de execução (default 30 dias).

        Com o journal ligado a gravar quotes por tick (dezenas a centenas de
        milhar de linhas/dia) a tabela crescia sem limite — um snapshot de
        3M+ linhas. A retenção alinha com os 30 dias do relatório de outages
        (`/api/operations/replay/outages?days=30`) e as janelas forward do
        AGENTS; o journal é evidência opt-in e é suposto ser desligado/
        rotacionado depois da recolha. Purge no watchdog + arranque, como o
        `loop_stall_events`. Compara a data do ISO (substr), imune a sufixos
        de fuso horário."""
        cutoff = (date.today() - timedelta(days=max(1, int(days)))).isoformat()
        with self.tx() as cur:
            cur.execute(
                "DELETE FROM execution_events WHERE substr(at, 1, 10) < ?",
                (cutoff,),
            )
            return cur.rowcount

    # ------------------------------------------------- paper trading forward

    @staticmethod
    def _paper_account_key(strategy_id: str, origin: str = "race") -> str:
        """Internal key for a virtual account.

        The original schema keyed accounts only by strategy.  Shadow-paper
        uses the same strategy ids as the normal race, so namespace its
        account key without changing the production SQLite primary key or
        mixing balances from the two forward streams.
        """
        return (f"shadow:{strategy_id}" if origin == "shadow" else strategy_id)

    @staticmethod
    def _paper_origin_sql(origin: Optional[str]) -> tuple[str, list]:
        """SQL predicate for legacy rows whose origin was NULL (race)."""
        if origin is None:
            return "", []
        return " AND COALESCE(origin, 'race') = ?", [origin]

    def paper_balance(self, strategy_id: str, origin: str = "race") -> Optional[float]:
        """Saldo da conta virtual de uma estratégia e origem.

        ``race`` mantém a API histórica; ``shadow`` usa uma conta separada
        mesmo quando o strategy_id é igual.
        """
        key = self._paper_account_key(strategy_id, origin)
        with self.tx() as cur:
            row = cur.execute(
                "SELECT balance FROM paper_accounts WHERE strategy_id = ?",
                (key,),
            ).fetchone()
        return float(row["balance"]) if row else None

    def paper_accounts(self) -> List[dict]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_accounts ORDER BY strategy_id"
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            key = str(item.get("strategy_id") or "")
            if key.startswith("shadow:"):
                item["strategy_id"] = key.split(":", 1)[1]
                item["origin"] = "shadow"
            else:
                item["origin"] = "race"
            out.append(item)
        return out

    def ensure_paper_account(self, strategy_id: str, start_balance: float,
                             origin: str = "race") -> float:
        """Cria a conta virtual da estratégia/origem se ainda não existir.

        A conta shadow começa a sua própria evidência em `start_balance`;
        nunca herda nem altera o saldo da corrida normal.
        """
        from datetime import datetime, timezone
        key = self._paper_account_key(strategy_id, origin)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.tx() as cur:
            row = cur.execute(
                "SELECT balance FROM paper_accounts WHERE strategy_id = ?",
                (key,),
            ).fetchone()
            if row:
                return float(row["balance"])
            cur.execute(
                "INSERT INTO paper_accounts (strategy_id, balance, start_balance, "
                "opened_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, start_balance, start_balance, now, now),
            )
        return start_balance

    def update_paper_balance(self, strategy_id: str, balance: float,
                             origin: str = "race") -> None:
        from datetime import datetime, timezone
        key = self._paper_account_key(strategy_id, origin)
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_accounts SET balance = ?, updated_at = ? "
                "WHERE strategy_id = ?",
                (balance, datetime.now(timezone.utc).isoformat(timespec="seconds"), key),
            )

    def insert_paper_trade(self, t: Trade) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO paper_trades "
                "(id, strategy_id, signal_id, symbol, direction, entry, stop_loss, "
                " take_profit_1, take_profit_2, take_profit_3, take_profit_4, "
                " take_profit_5, lots_1, lots_2, lots_3, lots_4, lots_5, status, "
                " opened_at, closed_at, pnl, exit_reason, origin, entry_style, "
                " market_price_at_open) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.strategy_id, t.signal_id, t.symbol, t.direction, t.entry,
                 t.stop_loss, t.take_profit_1, t.take_profit_2, t.take_profit_3,
                 t.take_profit_4, t.take_profit_5, t.lots_1, t.lots_2, t.lots_3,
                 t.lots_4, t.lots_5, t.status, t.opened_at, t.closed_at, t.pnl,
                 t.exit_reason, t.paper_origin or "race", t.entry_style,
                 t.market_price_at_open),
            )

    def insert_paper_position(self, p: Position) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO paper_positions "
                "(id, trade_id, leg, symbol, direction, lots, entry, stop_loss, "
                " take_profit, status, opened_at, closed_at, close_price, pnl, "
                " exit_reason, exit_msg_id, order_ticket) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.trade_id, p.leg, p.symbol, p.direction, p.lots, p.entry,
                 p.stop_loss, p.take_profit, p.status, p.opened_at, p.closed_at,
                 p.close_price, p.pnl, p.exit_reason, p.exit_msg_id, p.order_ticket),
            )

    def paper_positions_for_trade(self, trade_id: str) -> List[Position]:
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_positions WHERE trade_id = ?", (trade_id,)
            ).fetchall()
        return [Position(**dict(r)) for r in rows]

    def paper_open_trades(self, strategy_id: str,
                          origin: Optional[str] = None) -> List[dict]:
        """Trades de papel abertas; ``origin`` separa corrida normal de
        shadow-paper quando o painel mostra apenas a evidência isolada."""
        sql = "SELECT * FROM paper_trades WHERE strategy_id = ? AND status = 'open'"
        args = [strategy_id]
        origin_sql, origin_args = self._paper_origin_sql(origin)
        sql += origin_sql
        args.extend(origin_args)
        with self.tx() as cur:
            rows = cur.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def paper_open_trades_by_signal(self, signal_id: str,
                                    origin: Optional[str] = None) -> List[dict]:
        """Trades de papel abertas de um sinal (qualquer estratégia),
        opcionalmente limitadas a uma origem (`race` ou `shadow`)."""
        sql = "SELECT * FROM paper_trades WHERE signal_id = ? AND status = 'open'"
        args: list = [signal_id]
        origin_sql, origin_args = self._paper_origin_sql(origin)
        sql += origin_sql
        args.extend(origin_args)
        with self.tx() as cur:
            rows = cur.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def paper_open_trades_for_symbol(self, symbol: str,
                                     origin: Optional[str] = None) -> List[dict]:
        """Trades de papel abertas de um símbolo, para fallback de gestão
        quando não existe uma trade real para servir de alvo."""
        sql = "SELECT * FROM paper_trades WHERE symbol = ? AND status = 'open'"
        args: list = [symbol]
        origin_sql, origin_args = self._paper_origin_sql(origin)
        sql += origin_sql
        args.extend(origin_args)
        sql += " ORDER BY opened_at DESC"
        with self.tx() as cur:
            rows = cur.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def paper_trade_for_signal_any(self, signal_id: str, strategy_id: str,
                                   origin: Optional[str] = None) -> Optional[dict]:
        """A trade paper mais recente do sinal/estratégia, aberta ou fechada.

        Quando `origin` é informado, a idempotência fica limitada àquela
        conta: o mesmo sinal pode ter uma trade `race` e outra `shadow`, mas
        nunca duas dentro da mesma origem.
        """
        sql = ("SELECT * FROM paper_trades WHERE signal_id = ? AND strategy_id = ? "
               "ORDER BY opened_at DESC, id DESC LIMIT 1")
        args: list = [signal_id, strategy_id]
        origin_sql, origin_args = self._paper_origin_sql(origin)
        sql = sql.replace(" ORDER BY", origin_sql + " ORDER BY")
        args.extend(origin_args)
        with self.tx() as cur:
            row = cur.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def paper_trade_for_signal(self, signal_id: str, strategy_id: str,
                               origin: str = "race") -> Optional[dict]:
        """Trade de papel fechada de um sinal/origem (contrafactual sem gestão
        usa a corrida normal por defeito)."""
        origin_sql, origin_args = self._paper_origin_sql(origin)
        with self.tx() as cur:
            row = cur.execute(
                "SELECT * FROM paper_trades WHERE signal_id = ? AND strategy_id = ? "
                "AND status = 'closed'" + origin_sql +
                " ORDER BY closed_at DESC LIMIT 1",
                (signal_id, strategy_id, *origin_args),
            ).fetchone()
        return dict(row) if row else None

    def paper_trades_for_signal(self, signal_id: str, limit: int = 100,
                                origin: str = "race") -> List[dict]:
        """Todas as trades paper de um sinal/origem, abertas ou fechadas.

        O divergence report precisa desta consulta porque a auto e a
        referência podem fechar em ordens diferentes; limitar às posições
        ainda abertas perde a divergência quando a auto sai primeiro.
        """
        origin_sql, origin_args = self._paper_origin_sql(origin)
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE signal_id = ?" + origin_sql +
                " ORDER BY opened_at DESC LIMIT ?",
                (signal_id, *origin_args, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_trades(self, strategy_id: str, limit: int = 100,
                     origin: str = "race") -> List[dict]:
        origin_sql, origin_args = self._paper_origin_sql(origin)
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT * FROM paper_trades WHERE strategy_id = ?" + origin_sql +
                " ORDER BY opened_at DESC LIMIT ?",
                (strategy_id, *origin_args, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def paper_trade_stats(self, strategy_id: str, origin: str = "race") -> dict:
        """Resumo das trades simuladas fechadas de uma estratégia/origem."""
        origin_sql, origin_args = self._paper_origin_sql(origin)
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT pnl FROM paper_trades WHERE strategy_id = ? AND status='closed'" + origin_sql,
                (strategy_id, *origin_args),
            ).fetchall()
        pnls = [float(r["pnl"] or 0.0) for r in rows]
        from app.execution.evidence import bootstrap_mean_ci, forward_evidence_gate
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
            "mean_pnl_ci95": bootstrap_mean_ci(pnls),
            "forward_evidence": forward_evidence_gate(pnls),
        }

    def paper_trades_opened_since(self, strategy_id: str, since: str,
                                  origin: str = "race") -> int:
        """Nº de trades de papel Abertas de uma estratégia desde `since` — o
        ritmo de sinais/dia (cada sinal executado abre uma trade de papel em
        cada estratégia da corrida). Usado para o ETA da janela da Fase 3."""
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COUNT(*) n FROM paper_trades "
                "WHERE strategy_id = ? AND opened_at >= ?" + self._paper_origin_sql(origin)[0],
                (strategy_id, since, *self._paper_origin_sql(origin)[1]),
            ).fetchone()
        return int(row["n"] or 0)

    def paper_trades_opened_by_day(self, strategy_id: str, since: str,
                                   origin: str = "race") -> dict:
        """Nº de trades de papel abertas por DIA (chave = data ISO) desde
        `since` — a mini-curva do ritmo de sinais/dia do painel (tendência
        que alimenta o ETA da Fase 3)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT substr(opened_at, 1, 10) AS day, COUNT(*) n "
                "FROM paper_trades WHERE strategy_id = ? AND opened_at >= ?" +
                self._paper_origin_sql(origin)[0] + " GROUP BY day ORDER BY day",
                (strategy_id, since, *self._paper_origin_sql(origin)[1]),
            ).fetchall()
        return {str(r["day"]): int(r["n"] or 0) for r in rows}

    def paper_closed_recent(self, strategy_id: str, limit: int,
                            origin: str = "race") -> List[float]:
        """P&L das últimas `limit` trades de papel RESOLVIDAS (fechadas) da
        estratégia, por ordem de fecho — alimenta o modo defensivo da corrida
        paper (PF das últimas N < 1.0 → trail apertado + time stop)."""
        with self.tx() as cur:
            rows = cur.execute(
                "SELECT pnl FROM paper_trades WHERE strategy_id = ? "
                "AND status='closed' AND pnl IS NOT NULL" + self._paper_origin_sql(origin)[0] +
                " ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
                (strategy_id, *self._paper_origin_sql(origin)[1], limit),
            ).fetchall()
        return [float(r["pnl"] or 0.0) for r in rows]

    def paper_realized_today(self, strategy_id: str, day: str,
                             origin: str = "race") -> float:
        """P&L realizado HOJE (dia civil, UTC) das trades de papel fechadas da
        estratégia/origem — para o kill diário de cada stream ser independente."""
        origin_sql, origin_args = self._paper_origin_sql(origin)
        with self.tx() as cur:
            row = cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM paper_trades "
                "WHERE strategy_id = ? AND status='closed' AND closed_at LIKE ?" + origin_sql,
                (strategy_id, day + "%", *origin_args),
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

    def update_paper_trade_levels(self, trade_id: str, tp1: float, sl: float) -> None:
        """Keep an open paper trade aligned with a group TP1/SL correction."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_trades SET take_profit_1=?, stop_loss=? WHERE id=?",
                (tp1, sl, trade_id),
            )

    def update_paper_position_tp(self, pos_id: str, tp: float) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_positions SET take_profit=? WHERE id=?", (tp, pos_id)
            )

    def close_paper_trade(self, trade_id: str, now: str, pnl: float,
                          reason: Optional[str] = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE paper_trades SET status='closed', closed_at=?, pnl=?, "
                "exit_reason=? WHERE id=?", (now, pnl, reason, trade_id),
            )

    # ------------------------------------------------- balance history

    def insert_paper_balance_history(self, strategy_id: str, ts: str, balance: float,
                                     origin: str = "race") -> None:
        """Snapshot do saldo de uma estratégia/origem.

        A origem shadow usa uma chave interna separada para não colidir com a
        curva da corrida normal.
        """
        key = self._paper_account_key(strategy_id, origin) if strategy_id != "__real__" else strategy_id
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO paper_balance_history (strategy_id, ts, balance) "
                "VALUES (?, ?, ?)", (key, ts, balance),
            )

    def paper_balance_history(self, strategy_id: str, since: str = "",
                              origin: str = "race") -> List[dict]:
        """Snapshots de saldo de uma estratégia (mais recente primeiro); com
        `since` (ISO) devolve só os posteriores a essa data (ordem ascendente)."""
        key = self._paper_account_key(strategy_id, origin) if strategy_id != "__real__" else strategy_id
        with self.tx() as cur:
            if since:
                rows = cur.execute(
                    "SELECT ts, balance FROM paper_balance_history "
                    "WHERE strategy_id = ? AND ts >= ? ORDER BY ts ASC",
                    (key, since),
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT ts, balance FROM paper_balance_history "
                    "WHERE strategy_id = ? ORDER BY ts DESC LIMIT 500",
                    (key,),
                ).fetchall()
                rows = list(reversed(rows))
        return [dict(r) for r in rows]
