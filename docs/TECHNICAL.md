# Project NexGen - Technical Documentation

A **semi-automatic copy-trading** system: it reads signals (from Telegram groups or
a manual source), applies risk rules and runs trades with the
**N-entry strategy (TP1..TP5 + breakeven guard)**. Execution connects to
**MetaTrader 5** (demo/real account) and dashboard balance/equity comes from the terminal
account.

> ⚠️ **Important warning:** this skeleton is for research and learning.
> There is no guaranteed "alpha" in group signals; the only honest way to
> check a strategy is realistic backtest/simulation before risking capital.

### Replay of a real trade (read-only)

The opt-in journal (`execution_events`) stores quotes, management messages and
MT5 notes. The `GET /api/operations/replay/tape` export auto-joins
those events to the structured row in `signals`, resolves
`reply_to_msg_id` and builds a tape sorted by UTC + `sequence`. Besides
`signal`/`quote`/`management`, the tape can hold `order_request`,
`order_result`, `modify_request`, `modify_result`, `fill_open` and `fill_close`.
Notes hold retcode, accept, tickets, price, P&L and latencies
when the broker gives them. It can filter by `signal_id` or `trade_id`.

`POST /api/operations/replay` runs the tape on the three adapters in temp DBs
(SimEngine, PaperRace and MT5 dry-run) and returns `parity`, calls, SL, reasons,
P&L, `broker_observations` and post-event `snapshots`. Broker events are
passive: they compare seen run with the replayed call, but
do not change study state nor call MT5. `GET
/api/operations/replay/snapshots` runs the same tape and takes `step=N` for
single-step view. These endpoints never use the production DB, never start
the MetaTrader5 package and never change trades.

`GET /api/operations/journal-quality` sums a UTC window (default today):
structured signal count, management, quotes, open and
close fills, accepted/rejected retcodes, latency stats and trades whose tape
misses some seen legs. `GET /api/analysis/fill-audit` uses per-trade tapes
and compares each MT5 fill price/reason with replay; it returns
`first_divergence`, so the operator jumps straight to the first gap. The
`/auditoria` page shows both.

The forward gate (`GET /api/analysis/forward-gate`) stays apart from monthly
backtest. It only scores shadow accounts of `auto_escada_3` and
`auto_escada_5` vs a **hybrid reference** `nova_gestao` - the race run
(what main really ran) with per-signal priority, filled
by the `nova_gestao` shadow account (apart, opens on each full signal even
with kill switch on), in a controlled
30-day UTC window (start can freeze at
`config.forward_evidence_started_at`). It needs, per strategy, at least 20 closed trades,
PF>=1.5, lower IC95% bound of mean P&L above zero, drawdown<=125%
of start balance, at least 10 comparable signals with reference and no more than 10% behind
reference. The payload holds each sub-gate, but `eligible` never calls
`POST /api/config` nor promotes a strategy. The
`GET /api/analysis/audit-export?format=json|csv` bundle joins quality, fill-audit and
gate in one UTC window for send/archive. `POST
/api/analysis/forward-gate/archive` stores a window snapshot in the
`evidence_archives` table; `GET /api/analysis/forward-gate/archives` lists
snapshots and `/archives/{id}` returns the archived JSON. The daily summary adds
the same metrics to the feed/Telegram in one note. Any try
to promote an auto-ladder before `eligible=true` returns
`promotion_blocked=true` and keeps the current strategy.

---

## 1. Architecture

```
                    ┌──────────────────────────────────────────────┐
 Telegram / demo ──▶│  POST /api/signals (raw_text)                │
                    │      │                                       │
                    │      ▼                                       │
                    │  signals/parser.py  ──▶  Signal (structured) │
                    │      │                                       │
                    │      ▼                                       │
                    │  risk/engine.py       (lots, limits,         │
                    │                       kill switch)           │
                    │      │                                       │
                    │      ▼                                       │
                    │  runs direct (single auto path -            │
                    │  Item 12; guards ask explicit confirm)       │
                    │      │                                       │
                    │      ▼                                       │
                    │  execution/engine.py  (paper broker,         │
                    │                       TP1..TP5/BE)           │
                    │      │                                       │
                    │      ▼                                       │
                    │  notify/telegram.py   (notes +              │
                    │                       daily summary)         │
                    └──────────────────────────────────────────────┘
                              │
                    execution/feed.py (real prices; random-walk fallback)
```

Components:

| Module | Duty |
|---|---|
| `app/main.py` | FastAPI app: endpoints, WebSocket, engine wiring and lifespan |
| `app/background.py` | `BackgroundRuntime`: explicit lifecycle/cancel of market loop and watchdog |
| `app/execution/parity.py` | Normalize notes of the three engines for parity checks |
| `app/execution/clock.py` | Injectable UTC/monotonic clock (`SystemClock`/`ManualClock`) |
| `app/execution/journal.py` | Fixed events and sorted read-only replay for audit/replay |
| `app/execution/replay.py` | Fixed tape of signals, market, management and MT5 evidence; read-only SimEngine/PaperRace/MT5-dry harness |
| `app/execution/evidence.py` | Fixed bootstrap, journal quality, fill-to-replay audit and shadow forward gate with no promotion |
| `app/state.py` | `BotState` - runner: config, account, pipeline, tick, daily summary |
| `app/config.py` | Central setup + trading constants |
| `app/models.py` | Pydantic models (API) and inner dataclasses (Signal, Trade, Position) |
| `app/db.py` | SQLite store (stdlib, thread-safe) - holds `group_summaries` table (group ground truth) |
| `app/signals/parser.py` | Tolerant signal-message parser (PT/EN) + shared `validate_signal()` |
| `app/signals/llm_fallback.py` | **Optional** LLM parser (fallback when regex fails; sanity checks + min confidence; never runs in auto) |
| `app/messages/manage.py` | Fixed classifier of group management messages (TP HIT, SL HIT, close first, out at BE) |
| `app/messages/summaries.py` | Parser of group daily/weekly summaries (reported win rate = ground truth) |
| `app/risk/engine.py` | Lot sizing + risk checks |
| `app/execution/market.py` | Mock price feed/fallback (geometric random walk) |
| `app/maintenance.py` | Rotating logs, disk check, SQLite audit and read-only restore-check |
| `app/server_runner.py` | Uvicorn entry with stdout/stderr rotation before handlers |
| `app/execution/feed.py` | Real feed (Yahoo/TwelveData) + candle history |
| `app/execution/engine.py` | SimEngine: paper broker + TP1..TP5/BE/auto policy adapter |
| `app/execution/protection.py` | Pure policy/common reducer for engines: monotonic SL, ladder, trailing/ATR, confirm, time stop, auto-protect and `protect` merge |
| `app/backtest.py` | Backtest: replay of real signals on history candles + variant with group management (`simulate_signal_managed`) |
| `app/notify/telegram.py` | Notifier: DB + WebSocket + (optional) Telegram |
| `app/telegram_listener.py` | Telethon listener: reads the group and forwards to the API (split process) |
| `app/static/style.css` + `common.js` | Shared CSS + common JS (helpers, boot, WS, global chrome, menu/terminals) - loaded by all pages |
| `app/static/index.html` | Main dashboard (`/`) - signals/positions/history/report/ignored tabs + feed + parallel run |
| `app/static/teste.html` | Strategy test page (`/teste?s=<id>`) - sim, compare, walk-forward, paper trades |
| `app/static/diagnostico.html` | Diagnostics page (`/diagnostico`) - MT5, listener, DB |
| `app/static/help.html` | Help page (`/help`) - strategies + glossary |

**Concurrency model:** all changeable state lives in the FastAPI event loop
(`async def` endpoints + background tasks). No races between the 1s tick
and HTTP calls. SQLite access is set by an inner lock.

**Loop lifecycle:** `app/main.py` builds `BackgroundRuntime` inside
`lifespan`. The runtime makes one market task (MT5 reconnect + `state.tick`) and
one watchdog task, is idempotent on `start()` and on shutdown runs
`cancel()` + `await gather(..., return_exceptions=True)`. So `TestClient`
and MT5 tests leave no pending tasks tied to a closed event loop.
Adapter compare uses `app/execution/parity.py`, which touches no DB,
clock or broker. The pure `reduce_auto_protect()` reducer holds the trigger,
entry-to-TP1 fraction, `group_protect_sl` level and only monotonic
moves; backtest, PaperRace, SimEngine and MT5 apply those calls on
their own fills. The four adapters take an optional `Clock`: VPS uses
`SystemClock`, while studies can use `ManualClock` with no sleep and no
system-clock read.

**Common guard policy:** `app/execution/protection.py` is side-effect free.
`AutoProtectionPolicy` computes ladder, trailing target and the
`max(trail_factorxdist, atr_kxATR)` formula; `monotonic_sl`, `confirmation_legs`,
`time_stop_due` and `sl_exit_reason` set the same edges on adapters.
Backtest, PaperRace, SimEngine and MT5Engine stay in charge of their
fills, clocks, throttle, broker and store. `POST /api/operations/replay` takes a JSON event list and makes three temp SQLite DBs; the same tape goes to SimEngine, PaperRace and dry-run MT5Engine. The report compares per-signal leg calls, applied SL, exit reason and P&L. The harness does not load the production DB, does not start the MetaTrader5 package and cannot promote strategies.

The regime alert in
`state.py` also calls `sim_params(strategy_id)`, so the rolling sim uses
ladder/trailing/defensive-mode/ATR of the active strategy instead of scoring a
cut-down version. MT5 ATR only asks full candles
(`copy_rates_from_pos` starts at pos 1), so the forming candle never
enters a call.

---

## 2. Strategy (TP1..TP5 + breakeven)

The parser pulls **all TPs the group sends** (TP1 to TP5) and each signal
makes one `Trade` with **one leg per TP** (`Position`):

| Leg | Closes at | Lots |
|---|---|---|
| leg 1 | TP1 | `compute_lots(equity, risk/N, ...)` |
| leg 2 | TP2 | `compute_lots(equity, risk/N, ...)` |
| ... | ... | ... |
| leg 5 | TP5 | `compute_lots(equity, risk/N, ...)` |

Manage (in `execution/engine.py:update()`):

1. **SL hit first** -> all open legs close at SL (loss about total risk).
2. **TP1 hit** -> leg 1 closes at TP1 (profit); SL of **all
   remaining legs** moves to entry (**breakeven**).
3. Each left leg runs to **its own TP** (full win) **or**
   closes at BE if price comes back to entry (auto guard).

Mock P&L: `+/- (close - entry) x lots x contract` (contract per symbol:
XAUUSD 100, XAGUSD 5000, forex 100000 - same as live).

---

## 3. Risk engine

### 3.1 Lot sizing (`risk/engine.py:compute_lots`)

The risk slider sets **max loss per trade** (% of equity). Lots
come from SL gap:

```
risk_amount   = equity x risk% / 100
risk_pips     = |entry - SL| / pip_size          (pip_size = 0.0001)
lots          = risk_amount / (risk_pips x pip_value_per_lot)
```

Result: tight SL -> bigger lots; wide SL -> smaller lots. Money
risk stays flat. Total risk splits across N legs (one per TP;
each risks `risk/N`).

### 3.2 Pre-run checks (`risk/engine.py:check`)

The same on the single run path (auto - manual mode was removed
in Item 12; a human confirm does **not** bypass global risk):

| Check | Config | Effect |
|---|---|---|
| Kill switch | `kill_switch` | Blocks new entries |
| Auto block | `auto_kill` | Set when daily cap hits |
| Max positions | `max_positions` | Blocks if open-trade count >= cap |
| Daily loss | `daily_loss_limit_pct` | Blocks if `realized_today <= -equityx%` |

### 3.3 Auto kill switch

On `tick()` (1s), if `realized_today` passes the daily cap, the bot:
1. Turns on `kill_switch` and `auto_kill`;
2. Sends a risk note;
3. Only trades again when a human re-arms on the dashboard (`POST /api/killswitch {enabled:false}`, which clears `auto_kill`).

The kill switch does **not** close open spots - they keep running to
TP/SL/BE. It only blocks new entries.

---

### 3.4 Deterministic replay tape

The tape uses UTC stamps and `sequence` to keep same-time events in order:

```json
{"at":"2026-08-19T10:00:00+00:00","kind":"signal","sequence":1,"signal_id":"s1","payload":{"symbol":"XAUUSD","direction":"buy","entry":4400,"stop_loss":4396,"take_profit_1":4402,"take_profit_2":4404}}
{"at":"2026-08-19T10:00:01+00:00","kind":"quote","sequence":2,"symbol":"XAUUSD","payload":{"price":4402}}
{"at":"2026-08-19T10:00:01+00:00","kind":"management","sequence":3,"signal_id":"s1","payload":{"kind":"protect"}}
```

`POST /api/operations/replay` only runs read-only studies in temp DBs and forces the MT5 adapter to dry-run. `GET /api/operations/events` stays only the source of stored events; no replay endpoint changes live trades.

## 4. Signal pipeline

`POST /api/signals` takes:

- `raw_text` -> goes through `parser.py` (real Telegram message);
- structured fields (`symbol`, `direction`, `entry`, ...) -> used directly;
- `message_id` -> Telegram message id (see edit window below).

The parser (`signals/parser.py`) reads PT/EN, decimal commas and many
layouts. If a needed field is missing, the signal goes `failed` with a readable
message - the pipeline never breaks. Signals with wrong-side SL (e.g. SL above
entry on a buy) are rejected.

**The 3 historic group formats** (seen since 2026-08, with
offline history re-parse):

| Format | Example | Notes |
|---|---|---|
| current (multiline) | `I'm buying XAUUSD 4416-4412` ⏎ `TP1 4420 … SL 4410` | apart zone on its own line |
| `XAU USD … NOW` | `XAU USD SELL NOW 4737-4741 🥇TP1 4734 🥈TP2 4732 🥉TP3 … 🚫SL 4745` | spaced symbol, 🥇🥈🥉🏅 emojis before TPs, `🚫SL` |
| `Gold … Targets` | `Sell Gold Now 4123.5-4128.5 Targets 4121.5 4119.5 4117 Open SL/invalid 4131.5` | unlabeled targets, `SL/ invalid` as stop, inline zone on the line |

Symbols: `XAU USD` -> `XAUUSD`, `Sell/Buy Gold` -> `XAUUSD`. Old-format
signals only pass as history/backtest (`historical=True` never
runs) - an old format arriving **live** now is suspect.

**Bad zones rejected**: entry-zone span must be
<= ~1% of price (relative, works for gold and forex) - `4379-4775` (396
points) and `4317-3413` (904) are typos and go `failed`; wide but
sound zones (e.g. `4078.50-4047`, 31.5 points) pass. Bad stored
signals were re-marked `failed` in DB (576 valid signals).

### 4.1 Edit window (Telegram messages)

Signal groups **edit messages** (fix entries, void signals).
The trade is speed vs rightness: as group TP1 can hit in
seconds, a signal **complete at first parse runs at once** -
the edit window no longer applies (Item 12: real `/api/edits` data
showed measured lags come from post-run TP1/SL edits, not
entry). Measure of 26/08 on real DB (342 live signals): of 305
signals complete at first parse, **0 were edited later
(0.0%)** - under the 5% spec bar, so the rule stays
(complete -> runs now; window only for incomplete parses). Only when the
first version is **incomplete** (parse error) does the message with
`message_id` wait for edits (`edit_window_seconds`, default 20):

1. Message arrives incomplete -> `awaiting_edit` status, `edit_deadline = now + 20s`;
2. If an edit arrives (same `message_id` with new text), text is
   re-parsed, fields update and the clock **restarts** to +10s;
3. The 1s loop (`state.finalize_awaiting_signals`) checks `awaiting_edit`:
   when `edit_deadline` passes with no new edits, the signal moves on
   (risk checks + run; an AI-parsed signal is logged
   for audit and never runs);
4. If end content is still bad, the signal goes `failed` with the error.

Edits to already done messages were skipped (past window) - except
**revive** (`state.py::_ingest_telegram` + `_revive_signal`): if the signal
failed (e.g. incomplete entry with no TP/SL) and the group fills the edit
in 5 min, the signal goes back to `awaiting_edit` with the new content
(`action: revived`) instead of skipped; the same fits a
`rejected` signal (zone/cap guard, min lot, global risk) whose message the
group re-edits with VALID content in `rejected_revive_minutes`
(default 10, env `SIGNALBOT_REJECTED_REVIVE_MINUTES`) - with a plain alert
(kind `revived`) that the PRIOR entry was refused and the stored
`error` reason. Revive stores a **lasting marker** on the signal
(`revived_from`/`revived_reason`/`revived_at`, new `signals` columns,
migrate in `DB._migrate`) with prior state and stored reason; the
signal row on the Signals tab shows the `🔄 de rejected` badge (tooltip with
time + reason) even after it runs, and `_apply_edit` keeps the
marker if the group edits again in-window. The revive window
counts from `received_at` (first arrival). Late re-edits (>X min) reopen nothing; the
operator only gets a `system` warn when new content parses.
Edits to `executed`/`pending` signals only alert (`risk`). The stable
`tg-<message_id>` id makes the edit update the same DB row.

Each stored edit writes `edited_at` (new `signals` column, auto
migrate in `DB._migrate`). **`GET /api/edits`** (`state.edit_delay_stats`
-> `db.edit_delay_stats`) computes lag spread between first
message and last edit (min/mean/median/p90/max in seconds + last
8 samples) - the Diagnostics tab shows median vs current window
(`config.edit_window_seconds`) to check 20s on real data. The entry guard auto-takes up to `entry_guard_tolerance` price units outside zone; bigger gaps still ask manual confirm.
Revive notes use kind `revived` (🔄 icon on feed + toast).

**Ongoing post-run edit metric (Item 12)**: edits to signals
that ran **with no** window (complete first parse) now write
`edited_at` on the `executed`/`pending` branch of `_ingest_telegram` - before they only
warned. `db.post_execution_edit_metric` returns
`post_execution_edited`/`post_execution_total`/`post_execution_edit_rate`
(share of no-window runs that got a later edit) and the
`/api/edits` endpoint adds them. The tick checks about each ~30 min
(`state._check_post_execution_edits`) and, with sample >=20 and rate >5%, sends
a `risk` alert (feed + Telegram, 24h cooldown): the "complete -> runs
now" rule was set on a real 0.0% rate and must be reviewed if group habits
change. The snapshot shows `edit_metric` and the Diagnostics tab shows
the value vs the 5% bar.

### 4.2 Group management messages (state machine)

Groups send not only signals - they send **live manage notes**
("TP1 HIT", "close first entries and move stop loss above best entries",
"we're out at BE"). These messages **do not wait for the edit window**: the
classifier (`app/messages/manage.py`) acts at once on the open trade of the
same symbol (or the newest one):

| Group message | Bot action |
|---|---|
| `TP1 HIT` | SL of left legs -> breakeven (entry) |
| `TP2 HIT` … `TP5 HIT` | Confirmed/logged (the leg closes at its TP) |
| `SL HIT` | Confirmed - to reconcile with broker |
| `Close TP1 here` (no move SL) | Closes **only leg 1** at market (rest keep SL) |
| `close first entries + move SL above best` | Closes **leg 1** at market and moves SL of rest to **above entry** (locks profit, "risk free") |
| `we're out` / `close this setup` | Closes **all** legs at market; `out at BE` moves/guards rest at entry with no forced close |
| `Adjust SL to X` / `Move SL to X` | **`move_sl`** - moves SL of **all** open legs to the given level (with same safety checks as `protect`); with no numeric level only logs (`sl=None`). `sl` stays in `pending_management` when the trade has not opened yet |
| `TP1 4059 SL 4047` (with `reply_to` of a signal) | **`tp_sl_update`** - edit/confirm that fixes target signal levels in DB (`update_signal_levels`) instead of going `failed` |
| `Correction: Weekly summary …` | **`summary_correction`** - links to the answered summary (or the clear explicit span), rewrites target numbers and re-runs the split |
| `Signal #1: Sell at 4357 … +110 pips` | **`recount`** - per-trade recount (R2): stores in `trade_recounts` and audits vs same-day real fill (divergent entry/pips -> `risk` alert) |
| Daily/weekly summary | Stored as **ground truth** (`group_summaries` table) |

The message is always stored in history as a signal with `management` status
(and 📨 icon on dashboard) and noted on the feed. Classify is conservative:
the ~10 seen non-manage messages (pending "Buy/Sell Stop" orders,
comments, typo signals) never fire actions.

**Group chat (audit).** The listener forwards **all** to the
backend (`SIGNALBOT_TG_FILTER=0` in `.env`); the call lives in `state.py`:
`classify()` -> management; else the `_SIGNAL_HINT` regex (same keywords as the
old listener filter) -> if no match, the message is pure chat - it goes
to the `ignored_messages` table (dedup by message_id+source, last 1000)
and returns `action: ignored`. It never enters the pipeline or the edit
window. The dashboard "🚫 Ignored" tab shows this audit
(`GET /api/ignored`); the snapshot carries `ignored_count` for the badge.

**Telegram reply link.** The listener captures `reply_to_msg_id`
(Telethon: `message.reply_to` -> answered message id) and sends it in POST.
The handler uses that link to pick the target, in order: (1) the trade of the
signal the message **answers** (exact link - two open XAUUSD
trades do not mix the target); (2) open trade of same symbol; (3) newest
open trade. If the target signal is still in the edit window (no
open trade), the note is stored in `pending_management` and applied
automatically when that signal trade opens (`_apply_pending_management`
in `_try_execute`). New columns: `signals.reply_to_msg_id` and the
`pending_management` table.

In history replay (`app.backtest.correlate_management`) the same rule runs
before sim: `reply_to_msg_id -> message_id` of the signal is the strong
key; with no reply it uses the last prior signal of the same source; an explicit reply
that does not resolve is dropped, never sent to another XAUUSD.
`close_tp` events (named leg) and `move_sl` events (explicit level) enter the
timeline with `message_id` and `sl`, while a level-free `move_sl` stays only
as audit.

Flow (after the window closes):

- **Single path (auto, Item 12)**: passes risk checks and,
  if it passes, runs; if blocked, it goes `rejected` with reasons.
  Exception: signals blocked by entry/lot-min guard go
  `pending` with Confirm/Drop buttons on the dashboard (`/confirm` and
  `/reject` - never bypass kill switch/daily loss).
- An AI-parsed signal (LLM fallback) is logged for audit
  (`failed` + feed alert) and never runs nor goes pending.

Messages **with no** `message_id` (demo button, structured input)
move on at once, with no window.

---

## 5. API Reference

| Method | Route | What |
|---|---|---|
| GET | `/api/status` | Full snapshot (account, config, signals, trades, spots, notes, `auth.enabled`, `online_users`, `execution.hedged/blocked_reason` and `operations.disk`) - needs session/token |
| POST | `/api/login` | `{password}` -> httpOnly session cookie (7 days). 401 if wrong. Off with no `SIGNALBOT_DASHBOARD_PASSWORD` |
| POST | `/api/logout` | Ends session (clears cookie) |
| POST | `/api/config` | Partial update: `mode`, `risk_pct`, `daily_loss_limit_pct`, `max_positions`, `sim_speed`, `strategy`, `shadow_paper_enabled`, `shadow_paper_strategies` and `execution_journal_enabled` |
| POST | `/api/killswitch` | `{enabled: true|false}` - pause/re-arm |
| POST | `/api/signals` | Signal ingest (`raw_text`, structured or `message_id`) |
| POST | `/api/signals/{id}/confirm` | Explicit confirm of zone/lot-min-guard-blocked signal (Item 12: only human approval left) |
| POST | `/api/signals/{id}/reject` | Drop zone/lot-min-guard-blocked signal (no global veto) |
| POST | `/api/signals/{id}/delete` | Deleted message: marks the signal and closes the spot at market |
| GET | `/api/report?days=N` | Quality report (win rate, R:R, per group, **equity curve**, **max drawdown**, **`by_day`** with P&L/win rate per day for the bar chart) |
| GET | `/api/operations?days=N` | Item 10: stored exceptions (stack/context), mean monthly spread per symbol, weekly MT5 reconnects and taken/rejected SL modifies per retcode |
| GET | `/api/operations/events?since=&until=&kind=&limit=N` | Read-only journal of quotes/management when `execution_journal_enabled=true`; runs no actions |
| GET | `/api/operations/replay/tape?signal_id=&trade_id=&since=&until=` | Exports the full tape joining `execution_events` + `signals`, with no run |
| GET | `/api/operations/replay/snapshots?signal_id=&trade_id=&step=N` | Replays the tape read-only and returns post-event snapshots; `step=-1` returns all |
| POST | `/api/simulate` | Backtest of real signals with history prices (`risk_pct`, `days`, `delay_seconds`, `spread_pips`, `slippage_pips`, `commission_per_lot`) |
| POST | `/api/summary` | Forces the daily summary send |
| GET | `/api/trades` | Trade history |
| GET | `/api/notifications` | Notes feed |
| GET | `/api/ignored?limit=N&q=X&days=N` | Audit: group messages the bot skipped (chat) - time, source, full text; optional `q` (keyword) and `days` (span) filters |
| POST | `/api/heartbeat` | Telegram listener heartbeat (each 1s): `{groups, last_msg_at, tick}` - feeds the Diagnostics tab |
| POST | `/api/listener/need_code` | Listener warns Telegram asks new login code - dashboard shows the input |
| GET/POST | `/api/listener/code` | GET: listener fetches the code (one-time). POST: dashboard gives the user code |
| GET | `/api/diagnostics` | System state: MT5 (has `margin_mode`, `hedged` and `blocked_reason`), listener (alive/tick/last msg), server (uptime/PID), market (sources + `changes` - day % move per symbol), auth, DB (counts), disk (`operations.disk`), last group message, `uptime_history` (10s samples, 24h, downsample ~300), `listener_restarts` (last 10 restarts with time+reason), `replay` (date + totals of last history replay) and read-only DB-to-broker `reconciliation` |
| GET | `/api/analysis/protect_latency?strategy=<id>&engine=<paper\|mt5\|backtest>` | Hybrid `protect_latency_log` telemetry (or backtest replay): winner, SL, lag in seconds/pips and spread |
| GET | `/api/analysis/protect_causal?strategy=<id>&days=0` | Read-only causal replay: compares hybrid with auto with no `protect`, step delta per event and worst hits |
| GET | `/api/analysis/audit-export?format=json\|csv&days=N` | Exportable bundle of journal quality, fill-to-replay audit and forward gate |
| POST | `/api/analysis/forward-gate/archive` | Archives a read-only snapshot of the forward UTC window |
| GET | `/api/analysis/forward-gate/archives` | Lists archived forward windows |
| GET | `/api/analysis/forward-gate/archives/{id}` | Gets one archived snapshot |
| GET | `/api/analysis/resolution?interval=1m&days=7&strategy=<id>` | Compares asked size (native M1 to 7 days; plain fallback after that) with 15m ref and measures 0/8/60/300s lags, no writes |


The `/api/status` snapshot shows `operations.execution_journal` with total and spread of last-7-day events. The journal is opt-in and must be used to collect a quote/management tape before a replay, not as a broker stand-in.

The `/api/status` snapshot shows `market.changes` (symbol -> day %, from
Yahoo `chartPreviousClose` - `None`/missing when no data; used on the
price terminal (`termSide`, under the left menu) of the dashboard,
green/red).

**Zone layout**: global chrome lives in a **fixed top bar**
(`.topbar`, drawn by `common.js` in an `<div id="appChrome">` placeholder
- single source): row 1 with brand, flat nav
(MAIN · STRATEGIES dropdown · DIAGNOSTICS · AUDIT · HELP,
arrow-key nav ↑↓ + Enter), state badges (mode, strategy,
real MT5, run), online users, clock, kill switch and Exit - the
price strip (`priceStrip`) was removed in 09/2026 (user
call - topbar stays static; "real prices"/"simulation" badge covers
feed state). Content lives between top bar and page end
at full width (max 1440px centered) - old side columns
(`--term-w`/`--term-r`, `termSide`/`termRight` and ☰/▦ mobile overlays)
are gone. On screens <=960px nav folds in a dropdown opened
by ☰. Main order is: status -> KPIs -> tabs -> content, with
run panels (run/promotion/Phase 2/3/story/shadow) after
content (CSS `order`). On test, Diagnostics, Audit and
HELP pages the body gains `no-chrome` class that hides stats/controls/risk
block - only top bar and statusbar stay. The "If fixed
lot" widget is gone (no registry strategy uses fixed lot).

**Diff render (front)**: snapshot comes ~1x/s by WebSocket; pages
only rebuild DOM when content changed (`contentChanged` in
`common.js`, signature cache) and main only draws the active tab
(`window._activeTab`) - hidden tabs keep DOM and re-draw on
return. Feed rebuilds only on change (WS `prependFeed` stays
stepwise) and snapshot sends 30 notes (was 50).

**Post-run edit** (`state.py::_ingest_telegram`): when the listener
forwards an edit of a message whose signal is already `executed`/`pending`
(window passed), the bot sends a `risk` note - entry may
be stale. `historical=True` never fires (quiet replays).

**Test split**: `tests/conftest.py` sets `SIGNALBOT_DB`
(temp), `MARKET_PROVIDER=none`, `EXECUTION_MODE=sim`, etc. BEFORE
any `app.*` import - running files out of order never writes
to the production DB.

**Audit purge**: the `listener_restarts` table auto-clears
rows older than 30 days (`DB.purge_old_listener_restarts(30)`,
compares `substr(started_at, 1, 10)` tz-proof) - at server start
and each ~6h on the watchdog loop (`main.py::listener_watchdog`). Full
**manual clear** (with dashboard confirm): `DELETE /api/listener_restarts`
-> `DB.clear_listener_restarts()`.
| GET | `/api/summaries?group=X` | Stored group summaries (ground truth: reported WR) |
| GET | `/api/recounts?source=X` | Per-trade group recounts (R2, read-only) - compare with same-day real fills in `state._recount_fill_audit` |
| WS | `/ws` | Live stream: `snapshot` and `notification` - closes with 4401 with no valid session (auth on) |

**Auth** (`app/auth.py`): with `SIGNALBOT_DASHBOARD_PASSWORD` set, the
dashboard needs login (`/` serves `login.html`; all `/api/*` return 401
with no session cookie). The **listener** logs in with the `X-API-Token` header
(`SIGNALBOT_API_TOKEN`). With no password in env, all stays open (default
local/tests). In-memory sessions (single-process); password and token read
live from env on each call.

## Strategies, terminal nav and forward paper trading

- **`app/strategies.py`** - central registry (`STRATEGIES` + `MAIN_STRATEGY_ID`):
  each strategy sets `max_legs`, `manage`, `exit_at_tp` and, on autos,
  mechanical guard params. Strategies (after **full removal
  of 01/09**): `nova` (TP1..TP5), `nova_gestao` (main),
  `nova_gestao_fallback`, `tp2_protect` (protect-only + close at TP2),
  `auto_3`, `auto_5`, `auto_escada_3`, `auto_escada_3_hibrida`,
  `auto_escada_5`. Erased from registry on 01/09: `tp2`, `tp2_gestao`,
  `nova_gestao_3`, `auto_2`, `auto_protect_5` and `auto_escada_3_struggle`
  (history/PF/DD in CHANGELOG - their backtest is no longer
  reproducible). `sim_params(id)` returns params for `run_analysis`;
  `get_strategy` returns main for `None`/empty and **raises ValueError
  for unknown ids** (no quiet fallback in studies; config
  with a dead id goes orphan in snapshot - `_strategy_params`).
  `GET /api/strategies` shows the registry (minus removed).
  `MENU_HIDDEN_STRATEGIES =
  {nova, auto_5}` only hides from menu/visual run but keeps paper (the `nova`
  is the "no management" counterfactual of trade history - `_trade_view` - and
  can never fully hide; `auto_5` is the 5-leg with/without
  trail compare). **7 stay visible on menu/dropdown**: `nova_gestao`,
  `nova_gestao_fallback`, `tp2_protect`, `auto_3`, `auto_escada_3`,
  `auto_escada_3_hibrida` and `auto_escada_5` (5 run + 2 refs;
  `nova`/`auto_5` open paper off-curve). `auto_escada_3_hibrida` joins auto-manage with the single
  `protect` group kind, through monotonic merge and
  `protect_latency_log` telemetry; it is trial and not eligible for auto
  promotion. Early-guard path (`protect_trigger`/
  `protect_buffer`) and R4 rule (`struggle_*`) stay in engines by
  param, with no set strategy using them.
- **Config `strategy`** - active strategy id (default `nova_gestao`).
  `state._try_execute` passes `max_legs` to `open_trade`; `state.tick` passes
  `exit_at_tp` to `update`. `SimEngine` and `MT5Engine` take both:
  - `open_trade(..., max_legs=N)` caps legs (`_legs(obj, max_legs)`);
  - `update(..., exit_at_tp=N)` - when TP_N hits, all still-open
    legs close at that price (on real MT5, when leg N closes by
    broker in profit, rest close at market by `close_leg_at_market`).
- **Nav**: the menu lives in `common.js`/`style.css` - a `<nav class="term-menu">`
  with `data-page` links (main / test-<id> / diagnostics / help). The 7
  visible strategies live in the **STRATEGIES dropdown** (`.term-drop-toggle` +
  `.term-drop` - toggle in `common.js`, state kept in localStorage,
  default open; keyboard nav opens it alone when reaching an item
  inside; closes on outside click). `TERM_ORDER` in `common.js` mirrors the
  DOM order (main, 7 strategies, diagnostics, help). Pages
  are **split HTML files**: `index.html` (`/`), `teste.html`
  (`/teste?s=<id>`), `diagnostico.html` (`/diagnostico`) and `help.html`
  (`/help`) - all served by `main.py` with auth by middleware (no
  session -> login page). Each page sets `pageRender(s)` for its
  content; global chrome (badges, statusbar, terminals, toasts) comes from
  `common.js`. Test pages call
  `GET /api/analysis/month?strategy=<id>` (see 4.1) and draw the
  mini-curve on `testeChart` + the 3-way compare on `testeCompareChart`.
  Main **does not** show month sim (starts from today);
  the Diagnostics tab left the main tab bar (it has its own page
  on the menu). Each test page has the **Promote to main** button
  (`POST /api/config {strategy}`); main shows the active-strategy badge
  (`#strategyBadge`, glow when it changes) and the `#paperRaceWarn` warn
  when a test strategy passes main balance in the paper
  run (`renderStrategyBadge` / `renderPaperRaceWarn`).
- **Promote a strategy** (`state.update_config`): when `strategy`
  changes, the server logs a **feed note** with real run
  params (text by `_strategy_params_text` - max_legs /
  exit_at_tp / lots_override / management). The snapshot shows
  `config.strategy_params` (`state._strategy_params`) and the header badge
  shows them (`renderStrategyBadge` uses `stratParamsText`).
- **Orphan config**: if config `strategy` points to an id outside the
  registry (e.g. removed strategy), `strategy_params.orphan` goes `True`
  (+ `configured_id`), run falls back (`get_strategy` ->
  main) and the badge warns red - it never quietly runs the wrong strategy.
- **3-curve test-page compare**: `loadTesteCompare`
  draws nova, **nova+management (main)** and close at TP2 on the same
  signals (was only the 2 test ones) - management P&L shows on
  each test page.
- **Last replay on Diagnostics**: `state.history_done` stores in config
  `replay_done_at`/`replay_group`/`replay_total`/`replay_sinais`/`replay_gestao`/
  `replay_resumos`/`replay_ignoradas`; `/api/diagnostics` shows the
  `replay` block and the tab shows date + totals.
- **Test pages with 2 views**: besides the 7/30/90-day picker, each
  page draws the "📜 Since group start" block that calls
  `GET /api/analysis/month?days=0` (all history in DB - see 4.1) and the
  **paper-trades table** by `GET /api/paper/trades?strategy=<id>`
  (open + closed with P&L; `db.paper_trades`).
- **Forward paper trading** (`app/execution/paper.py`, `PaperRace`):
  - `paper_accounts` tables (strategy_id PK, balance, start_balance),
    `paper_trades`, `paper_positions` - apart from the main DB;
  - `open_for_signal(signal, risk_pct, strategy_ids=None,
    daily_stop_pct=None)` opens the mock trade for all visible strategies
    (WITH main - each with its own virtual 1,000 EUR account), with their own `max_legs` and virtual-account balance;
    `paper_status` marks `is_main` from active config but main
    also has its own `balance`; **daily kill**: with `daily_stop_pct`
    (>0), when `paper_realized_today(strategy, today)` (SUM pnl of
    `paper_trades` closed on UTC civil day) <= -balancexpct/100 the signal does not
    open on paper for that strategy - `_skipped_daily_stop` counts per
    strategy (shown in snapshot as `daily_stop_skipped`); `state._try_execute`
    passes config `daily_loss_limit_pct`;
  - `  tick(risk_pct)` moves open spots vs the market feed
    (SL/TP/BE + `exit_at_tp` + **auto-manage**: ladder, trailing,
    confirm - `auto_escada_3` now with `confirm_filter=True` after
    Item 3 - and time stop - mirror of backtest order, with

    look-ahead-free **trailing** (SL uses the peak of PRIOR ticks; the
    peak only updates at tick end, after fills). Trailing
    state (peak/valley since TP1), ladder and time stop persist in
    `protection_states` per engine/origin/trade and rebuild after restart;
    only MT5 monotonic modify throttle starts over. Auto-manage exit
    reasons:
    `trail`/`stair`/`confirm`/`time_stop`; trade reason is the LAST
    leg to close (e.g. `close_all` after TP1, not leg-1 tp1);
  - **early auto-protect** (`auto_protect_5`): with no Telegram, price that
    walks `protect_trigger=0.75` from entry to TP1 closes leg 1 at the trigger
    level and moves rest to `group_protect_sl` with
    `protect_buffer=0.10`; same helper runs in backtest, PaperRace,
    SimEngine and MT5. It is a trial candidate and not promoted.
  - **defensive mode** (19/08 study): when PF of last `def_window`
    solved trades < 1.0, the strategy (auto_escada_3/5) moves to
    `def_trail` (1.5) + `def_time_stop` (60) and caps new entries at
    `def_risk_pct` (1.25%) with NO leg cuts, until PF recovers - no
    look-ahead (the call uses only history before the tick). On paper history is its own virtual account
    (`db.paper_closed_recent`, race/shadow origin); on real MT5 (Phase 3) from closed real
    trades (`db.recent_closed_pnls`); in backtest the shared `def_state`
    across signals (time order) inside `run_analysis`. The snapshot
    shows `strategy_params.defensive` when main is in defensive
    mode ("🛡 defensive mode" badge);
  - **group management on paper**: `apply_management(signal_id, kind, tp, sl,
    be, msg_id, origin)` hits open paper trades of the target signal (the
    same reply->symbol->newest link of the real account, by
    `signal_id`), but only for strategies whose `manage_kinds` holds the kind;
    `origin="race"` and `origin="shadow"` give the same management to each stream
    with no delivery eating the other - called by `_handle_management` and
    the pending paths of `state.py`;
  - **exit credit**: `exit_reason` (sl/tpN/protect/close_all/
    close_tp1/move_sl/killswitch/manual) + `exit_msg_id` in
    `positions`/`trades`/`paper_positions`/`paper_trades` (new columns,
    auto ALTER TABLE migrate at start - see `db._migrate`);
    trade snapshot holds `counterfactual_pnl` (P&L of the `nova` variant
    of the paper run for the same signal - the live "no management");
  - **shadow-paper (forward with no demo orders)**: when `shadow_paper_enabled`
    is on (default), `_open_shadow_for_signal` takes full live signals
    before main-account risk/kill checks and calls
    `PaperRace.open_for_signal(..., origin="shadow")`; it never calls
    `SimEngine.open_trade`/`MT5Engine.open_trade`. Dedup is by
    `signal_id+strategy_id+origin`: a retry does not copy the same origin, but the
    same signal can have one trade on the normal run and one on shadow. Each
    origin also has apart balance, daily kill, PF and account history;
    `historical=True` and `llm_parsed=True` stay out of forward shadow. The
    `origin` stored in `paper_trades` and the `shadow_paper` panel make the
    split auditable; `paper_open_trades(..., origin="shadow")` does not mix
    count with the normal run. Management
    messages, with `TP1 ... SL ...` updates, follow target by reply and
    keep `pending_management.applied` (real run), `paper_applied`
    (normal run) and `shadow_applied` (apart stream) split; a
    shadow delivery does not eat the pending item the normal run still needs.
    An `out at BE` note keeps entry-guard meaning when
    queued before open.
  - **real vs backtest** (`GET /api/analysis/real_vs_backtest` ->
    `analysis.real_vs_backtest`): for each closed REAL trade (`trades` +
    `positions` - the demo account), the sim fill of the same signal is entry +/-
    spread/2 +/- slippage (`simulate_signal` formula); it compares entry gap
    (real price vs fill, in pips), exit gap (leg-weighted close vs the
    expected level by close reason: TP_k -> `take_profit`, sl ->
    `stop_loss`), signal-to-open lag (vs sim `delay_seconds`=60) and
    real P&L vs `run_analysis` of main strategy on the same signal
    (falls back to price compare with no candles). Table on
    Report tab;
  - **auto paper vs auto MT5 (Phase 3)** (`GET /api/analysis/paper_vs_real_auto`
    -> `analysis.paper_vs_real_auto`): for each closed REAL trade of an
    `auto_*` strategy (when promoted to main on MT5) with a PAPER
    trade of the same strategy on the same signal (paper run keeps going in
    parallel): same exit call (`exit_reason`), close-price gap
    (real vs paper leg-weighted mean, in pips), timing gap (paper
    vs real close time, in min) and P&L of each side. It is the paper-to-MT5 split
    report of the Phase 3 path (docs/ESTRATEGIAS.md Part X);
    empty until Phase 3 starts. Table
    "🔄 Auto paper vs Auto MT5 (Phase 3)" on Report tab;
  - **divergence report** (`PaperRace._divergences`): when a run
    trade closes, it compares the auto (in check, `auto_*`) with reference
    `nova_gestao` of the SAME signal - if they exited different it sends a
    `divergence` event (feed kind `divergence` ⚖️ + Telegram if set),
    with the case "reference closed by group management and auto still
    open". One-shot dedup per `signal:auto` (in-memory `_diverged`);
    `state.tick` turns events into notes; the Report tab has the
    stored table (`GET /api/paper/divergences` -> `state.paper_divergences`:
    auto vs `nova_gestao` pairs per signal with reason/P&L side by side + motive,
    with "reference exited and auto still on");
- **Parallel-run curve** ("who is winning"): the server writes
  balance snapshots each ~60s on tick (`_record_balance_snapshots` in
  `state.py`) to `paper_balance_history` (UNIQUE strategy_id+ts) - all
  strategies (with main) with their own virtual 1,000 EUR account, to
  compare on equal terms (not real MT5 balance). Also, the tick also writes the **real MT5 balance** under the
  reserved `__real__` strategy (no paper account) - `GET /api/paper/history?hours=`
  (to **720h** = 30 days, 24h/7d/30d picker on dashboard) returns series
  per strategy (`is_main` + `is_real` set, and `closed` + `win_rate` of
  each strategy by `paper_trade_stats`); the
  dashboard draws growth in %
  from the first point (mini-curve on main),
  with **absolute P&L in EUR on hover** (`+418 EUR (+3.7%)`)
  and current balance in legend; the **main (real account)** line
  draws **dashed and thicker** (`is_main` -> dash `[8,4]`,
  width 3) to stand out from test lines, and the **real
  `__real__` reference** draws over in a light dash (`is_real`) outside
  compare. The menu marks the
  `compare?days=0` winner with "✓ best" (`markBestStrategy()` on
  frontend, `compare.winner` on backend).
- **Lead flip alert on the run**: `state.tick` calls
  `_check_paper_race_leader` - when a test strategy moves to
  a balance **above** main (with cooldown and only if snapshots
  already exist), it sends a `race` note on the feed - 🏁 icon in **purple**
  (`var(--violet)`, "🏁 Race" group) - and Telegram if set,
  plus the red warn the dashboard already shows in the header.
- **Regime auto-switch hint** (same path): if a **visible** strategy
  (outside `HIDDEN_STRATEGIES`) stays ahead of main
  for >= 30 straight min with >= 3 closed trades, it sends a second
  `race` note of **"switch hint"** (`_race_switch_suggested`
  stops repeats) with `action="promote:<id>"` - the dashboard shows a
  **"⬆️ Promote"** button on the feed (click -> `POST /api/config {strategy}` and back
  to main). It never switches alone.
  - `Notifier.send(kind, text, tg_text="", parse_mode="Markdown")`: feed
    stores plain `text` (no markdown) and Telegram push uses `tg_text`
    (bold/lines) when given - the run alert sends a
    Markdown-shaped version.
  - called in `state._try_execute` (after run) and in `state.tick`;
  - snapshot shows `paper.accounts` (per strategy: balance, wins, losses,
    win_rate, pnl, profit_factor).

**MFE win rate (R3)**: `simulate_signal` measures `mfe_pips` (max favorable
run - pure watch, never a fill call) and `aggregate()` shows
`mfe_win_rate`/`mfe_threshold_pips`: wins in group convention (MFE >= 25
pips by default), even if the trade came back and closed at loss/BE.
Group-to-bot split (`_check_group_divergence`) compares reported WR
with `mfe_win_rate` when backtest measures it (falls back to pnl
win_rate on old data) - a trade that ran +40 pips and came back to SL is a win
for the group, not for the bot, and no longer makes false alerts.

**Per-trade recount (R2)**: `app/messages/recounts.py::parse_recount`
pulls `Signal #N -> direction -> entry -> run -> reverse -> pips`; the
classifier returns `kind="recount"` before the chat filter. State
stores in `trade_recounts` (dedup by `message_id`) and `_recount_fill_audit`
compares with closed real trades of the same day (direction + nearest
entry, 40 pip tolerance): entry >=20 pips, pips >=30 or **divergent
direction** fire a `risk` alert on feed/Telegram. The audit also shows
`direction_mismatch` and the **candle-check** (`_recount_candle_check`): the
group-reported run/reverse compare with high/low of the
day Yahoo 15m candles; with no linkable symbol (recount has no symbol -
it links by near-entry day signal) or no candles it returns
`checked=False` (never makes up a pass). `GET /api/recounts`
(read-only) returns each recount with its fill-audit
(`matched`/`alert`/`candle_check`) and the `weekly` set (per ISO week:
matching/divergent/no trade) - the Report tab shows the "📋
Group recounts vs real fills" table with divergent ones marked and the
weekly mini-chart. A LIVE recount with no matching trade on the day
fires an informative `system` note ("Recount with no matching trade")
with the **likely cause** (`_recount_no_trade_cause`): day signal
failed/rejected, pending (entry guard), opposite direction on the day, or kill
switch/other-feed signal. A LIVE recount whose
run/reverse does not fit the real day range fires a `risk` note
"Recount not plausible vs day candles" (`candle_check.plausible=False`).

The candle-check uses an **hour window** when target signal time is
known: full-day high/low mixes sessions (a 22:00 peak does not
prove a 09:00 signal claim), so it also measures the range of
candles +-3h around signal time (`window_high`/`window_low`/
`window_excursion_ok`/`window_reversal_ok`/`window_plausible`) and uses THAT
verdict as `plausible`; the full day stays in `day_plausible`/
`day_high`/`day_low` for context. The not-plausible note shows the
window (or the day, with no signal time) with flags, and the recount
table tooltip shows day + window so the operator sees it with no logs.

Read-only tools: `app/sweep_recount_candles.py` (sweep of
plausibility of all recounts - not-plausible/false-positive
rate), `app/backfill_recounts.py` (re-scans `ignored_messages`/
failed `signals` for `Signal #N` dropped as chat;
`--apply` writes to `trade_recounts`, with no flag only previews),
`app/study_recount_mfe.py` (compares reported pips with real MFE of
candles: win match / false win / unreported wins - checks the
R3 group win convention vs real run) and
`app/study_recount_implausible.py` (crosses each not-plausible recount with same-day
real fills: the bot traded - compares each side entry/pips -
or did not trade - likely cause by day signal).

When a LIVE recount is marked not-plausible and the journal is on
(`execution_journal_enabled`), `_handle_recount` writes a
`recount_implausible` event in `execution_events` (signal_id, message_id, reported
fields, window/day scope, `excursion_ok`/`reversal_ok` flags and the
ranges). It is passive evidence: replay skips unknown kinds, so
it never allows orders nor changes replayed calls.

`compare_broker_fills` (GET /api/analysis/fill-audit) now returns
`recount_evidence`: journal `recount_implausible` events listed
as evidence split - "real" = group claim, "replay" = candle
range, with `bot_traded`/`bot_trade` (closed real day trade with
near entry) to cross who split, with no fresh Yahoo read.
`POST /api/recounts/{id}/recheck` remakes fill-audit + candle-check of one
recount with fresh candles (read-only: does not change row or journal).
When `bot_traded=False`, `recount_evidence` crosses listener restarts
(`listener_restarts`, +-15 min around the event) and the stored
kill switch (`kill_switch`/`killswitch_rearm_at`) and returns `miss_reason`
to point the likely cause of the missed run. The restart is
classed as `restart_kind` (watchdog if reason holds "watchdog",
else manual/outside - e.g. boot.ps1) and `signals_lost_est` counts
group signals in the 60s after start (history replay recovers,
but live run lags). When `restart_kind=manual`, the cross
with `server_booted_at` (stored in config at lifespan - boot.ps1
starts the server) returns `vps_reboot=true` if restart matches
VPS boot (NexGen Boot task), apart from a lone manual
restart.

The candle-check applies `_gold_futures_offset` when samples are
enough (>=3 real XAUUSD fills, |offset| >= 5 points): the median gap
between the GC=F candle at fill time and bot spot entry is
used to shift reported prices to the candle frame
before compare (`gold_offset` in result; the
`recount_implausible` journal event stores applied offset in payload, so
replay gives the same verdict) - the Yahoo feed serves GC=F
futures while the group trades spot, and with no fix the filter marks
not-plausible with false positives (measured baseline: 66% of real signals
fall outside the +-3h window, 62.4% with the window widened to 6h -
`app/study_recount_baseline.py --window-hours 6`; the
`app/study_signal_delay.py` study shows 3.7h median between group
Telegram signal and entry touch on GC=F). `recounts_report` shows the
`calibration` block (counted offset, sample count, on yes/no) and
/auditoria shows the "GC=F-to-spot Offset" panel to know when
tuning goes live (>=3 real fills).
The `/auditoria` page has the "📋 Group recount quality" block
(stats + table + weekly state + weekly not-plausible mini-chart) and an implausible-recount details on the fill-audit card, with
a "🔄 re-check" button per row; all loads in the same `loadAudit` as
journal-quality/fill-audit/forward-gate. The same card has the journal ⏸️/▶️
toggle (`execution_journal_enabled`, with confirm). The daily summary
adds the "📋 Day recounts" line (got/not-plausible/no run).
`app/study_recount_implausible.py` gives a set verdict (claim
error / run error / no data + top-5 of each),
`app/study_recount_baseline.py` measures the candle-check false-positive baseline on real
signals (note: Yahoo feed serves GC=F
futures while the group trades spot - the tens-of-points gap
makes the rate look high with no proof of a false claim) and
`app/study_signal_delay.py` measures lag between signal time on
Telegram and entry touch on GC=F (3.7h median on real 30d - the
+-3h check window is fair vs the feed). The recount block of
/auditoria shows the recount not-plausible rate vs baseline
(66%) so the operator can tell worse-than-expected claims from plain
spot-to-futures split.

**Per-symbol adaptive window**: candle-check uses
`recount_window_hours_by_symbol` (JSON config) per symbol, falling back
to `recount_window_hours` (default 3h), capped at [1, 24]h
(`_recount_window_hours`). `study_signal_delay` sets lag per
symbol and advises the window (p75 capped at [3, 12]h, `--apply` writes);
on the real DB XAUUSD=6h was applied because the auto advice (12h) is
tainted by GC=F feed (p75 29h is far above 3.7h median - the tail is futures
touching entry late, not group lag).

**Batch re-check**: `POST /api/recounts/recheck-all` remakes
fill-audit + candle-check of ALL recounts with fresh candles
(fetched ONCE per symbol, adaptive window and newest offset) and
stores the verdict in `trade_recounts.candle_check` (+ `checked_at`,
auto migrate). /auditoria has the "re-check all" button and rows
show the batch-stored verdict. `_recount_resolve`
(shared by check and batch) returns target signal symbol + time.

**Primary MT5 spot feed + joined verdict**: production candle-check now uses
terminal XAUUSD M15 candles (native spot) as
primary source by `_recount_candles` (5 min cache per symbol), with
fallback to Yahoo GC=F when the terminal is off. The verdict is
JOINED: it computes `spot_plausible` (MT5) and `futures_plausible` (GC=F); the
`plausible` is the spot one when present (the group trades spot); when the
spot says plausible but GC=F not-plausible, `feed_divergence=true` and
`verdict_kind="feed_divergence"` - /auditoria and dashboard mark
"⚠️ likely feed split" instead of a suspect claim. GC=F is
compared with NO offset fix (the spot-to-futures offset is gone - the
spot is truth and tuning lost its use; the
`_gold_futures_offset` helper stays only for the read-only
`study_recount_baseline --apply-offset` study). `_recount_check_source` splits the
verdict per source (day + window). The spot feed covers any mapped MT5 symbol
(XAUUSD, XAGUSD, forex) with per-symbol Yahoo fallback.

**MT5 spot feed vs GC=F**: `app/study_recount_mt5_feed.py` runs the SAME
check with terminal XAUUSD M15 candles (native spot, `apply_offset=False`
- applying the futures offset to spot candles would double-fix) side by
side with GC=F; `--signals` measures REAL signal entries instead of
recounts (re-baseline with the right feed). Real result (30d, 101 signals):
**0.0%** of entries fall outside the +-3h window on MT5 spot vs **66.3%** on
GC=F - the not-plausible ones were ALL futures-feed split. On the real
DB the 08-19 recount marked not-plausible by GC=F is PLAUSIBLE on spot
MT5 (4351 reverse inside real 4335 low; 4362 run at window
4362 high). The study fixed two stored
`MT5Provider.copy_rates` bugs (`or []` on numpy structured array - unclear
truth - and field read by attr that numpy 2.x dropped -> candles
with ts=0); it now uses index read with attr fallback.

**Confirmed suspect-claim alert**: `_handle_recount` only sends the
"Not-plausible recount" risk note when MT5 spot (primary) AND
GC=F both reject the claim (or with no GC=F to confirm). If spot says
not-plausible but GC=F says plausible, there is no confirm - no alert
(before the spot source, spot-to-futures split made false
not-plausible ones). /auditoria splits "Confirmed not-plaus." (spot+GC=F) from
"Feed split" (GC=F not-plausible, spot plausible), with 0%
baseline (spot) instead of the 66% futures feed.

**Daily batch re-check**: the tick runs `_check_recount_batch` once
per UTC day (guard `recount_batch_last_run` in config) - remakes the check of
all recounts with joined sources (per-symbol cache) and notes
on feed/Telegram when a stored verdict changes regime or there are
errors. `POST /api/recounts/recheck-all` stays to run by
hand.

The dashboard is served at `/` (static). The old **Edge** tab was folded
in the **test pages**, which show:

- an **A/B/C compare** (`GET /api/analysis/compare`): TP1..TP5 (nova) vs
  **TP1..TP5 + group management** (backtest that replays
  `protect` / `close_all` group notes linked to signals by source+time,
  with SL-first and look-ahead-free fills) vs **close all at TP2**
  (`exit_at_tp=2` - when TP2 hits, all still-open legs
  close at that price). The old strategy (TP1/TP2 only) is gone.
  Each column shows WR, profit factor, P&L and drawdown; the winner is
  marked. The page shows the span compare (7/30/90 picker) **and** the
  "📜 Since group start" block over all history (`days=0`) - does
  edge hold?
- **walk-forward** (`GET /api/analysis/walkforward`) - management edge
  by time windows;
- **risk studies** (risk %/balance pickers, default 1000).

- compare takes `days=0` = all history in DB (run_compare ->
  run_analysis days<=0, 1h/730d candles).
- **`since=YYYY-MM-DD`** in `run_analysis`/`run_compare`/`month_sim` filters
  signals from that date - the "📜 Since start" block of test
  pages has a **span picker** (config
  `all_period`, see 4.5): `"0"` = all history, `2026-07-28` (first
  message of current format) = current format only, and the 1st of the
  current month (moving) = month only. Each payload returns `pnl_by_month`
  (bar chart), `pnl_by_leg` (P&L per leg leg1..leg5, only solved trades - test
  pages show the "P&L per leg" chips) and
  `month_sim` also returns `bankrupt`/`bankrupt_date` (sim stops when
  stored balance hits zero - the verdict shows ruin date).
- a **sim** (`GET /api/analysis/month`, `run_month_sim`): replay
  of real signals with `manage=True` from `days` ago to today - or
  **`days=0` = all history in DB** (from group start, no
  date filter; 1h/730d candles on Yahoo) - starting with current balance
  (`start_balance`), returning `end_balance`, P&L, WR/PF, `resolved` and the
  `equity_curve` (stored P&L by signal date) - the
  test pages draw the mini-curve and show "if you had started 1 month ago..." and the
  test pages show both views per strategy.

### 4.3 Hybrid guard, causal proof and parity

`auto_escada_3_hibrida` keeps ladder/trailing/defensive mode and honors
only `protect`. `decide_protect()` makes the monotonic merge: on a buy the
higher SL wins; on a sell the lower; leg 1 only closes when the
price already known at event time makes the exit valid. Paper and MT5
store one row per leg in `protect_latency_log`; backtest returns the
same telemetry with no DB write.

`GET /api/analysis/protect_latency` reads paper/MT5 or recomputes with
`engine=backtest`. `GET /api/analysis/protect_causal` makes a replay by
time-ordered prefixes: mechanical baseline -> first `protect` -> second,
etc.; each prefix `incremental_pnl` states the event share
and deltas sum the hybrid-vs-auto effect. On the 19/08 replay there were 355
events on 270 signals, 0 harmful and 0 EUR total delta, so the loss of the
hybrid is not caused by seen `protect` calls.

The `tests/test_engine_parity.py` check uses the same buy/sell price path
on backtest, SimEngine, PaperRace and MT5 dry-run and normalizes calls, SL
applied, exit reason and P&L with `position_observation()`.

### 4.4 Read-only management, risk and polling studies

`app/study_strategies.py` adds five studies with no writes and no orders. The
polling benchmark injects `ManualClock`, so 1s/250ms/100ms really
measure the replayed pace (with time stops), instead of hanging on
process run time:

- `--study ablation`: no management, each lone kind (`protect`, `close_all`,
  `move_sl`, `tp_hit`) and all kinds;
- `--study walkforward --period current|90|all|old`: on each fold it picks the
  policy only on train and checks on the next time-ordered block; `old`
  uses 2026-04-01 to 2026-07-28, `all` uses all history and both force
  1h/730d candles so the old span is not cut;
- `--study legs`: compares equal weights with a light shift to TP3-TP5;
  total risk is normalized, but the lot floor can make variants
  same on small balances;
- `--study fallback`: after TP1, it moves SL of left legs to the
  group monotonic guard if no message arrives in the set time.
  `management_fallback_seconds=None` is default and keeps current
  behavior;
- `--study polling`: uses a temp DB and a synthetic path to measure
  `PaperRace` sensitivity at 1s/250ms/100ms. It is a polling test, not
  a monthly backtest; the production `BackgroundRuntime` gap stays
  1s.

Optional `leg_weights`, `management_fallback_seconds` and
`fallback_buffer_fraction` params enter `run_analysis`/`simulate_signal` only
for these studies and are in no promoted strategy. 19/08 results stay in Part VIII-A of `docs/ESTRATEGIAS.md`.

### 4.4 History reload and summaries

When the listener reloads history (`SIGNALBOT_TG_HISTORY=1`), old
messages re-enter the API. So group summaries stored
as `failed` on a prior load get recovered:

- management classify runs **before** end-state dedup, so
  a historic summary/management message always re-enters the handler;
- **historic** management messages (`historical=True`) are logged but
  **never act** on open trades;
- `insert_group_summary` is idempotent by `message_id` (`UNIQUE` column) -
  repeating a summary updates the row instead of copying;
- `list_group_summaries` sorts by covered span (newest first),
  to always show the newest summary (e.g. weekly).

### 5.2 Operational watch (Item 10)

Run telemetry is stored in the same SQLite and does not change run
nor open trades. `GET /api/operations?days=N` (1-730 days; default 90) returns
one payload for the Report panel and for the
`operations.metrics` block of `/api/diagnostics`:

- `exceptions`: total, count per kind and fresh rows with timestamp,
  stack trace, JSON context, `signal_id` and `trade_id`;
- `spread`: MT5 bid/ask notes and monthly set per symbol (`count`,
  mean, min and max in pips);
- `mt5.connections.weekly`: reconnect tries/wins/fails,
  connects, drops and win rate per UTC week;
- `mt5.modifies`: SL modify tries, taken/rejected and retcode
  spread per week, with 10016 and other broker errors.

`BackgroundRuntime` logs exceptions that leak from market or
watchdog loops. `BotState.tick` logs spread-sample fails, the
`_reconnect_mt5` logs each try/result, and `MT5Engine` writes the
modify op before taking the new SL as applied. For real quotes,
forex pip (0.0001 on 5-digit symbols) stays apart from broker
tick (0.00001), so spread and risk gaps are not puffed. The Report
table plainly shows "no notes" when no data exists yet;
a trend must only be read after at least two weeks.
Tables are `operational_exceptions`, `spread_observations`,
`mt5_connection_events` and `mt5_modify_events`, with time indexes and retention
policy filed by `maintenance.py --db-audit`.

### 5.3 LLM fallback (optional, off by default)

Regex is the main parser (fixed and tested). When it **fails** and an
LLM API is set, the text goes to the model and the proposal passes the
**same sanity checks** as regex (`validate_signal`):

- confidence < `SIGNALBOT_LLM_MIN_CONFIDENCE` (default 0.8) -> **rejected**;
- out-of-order TPs / wrong-side SL / wild TP -> **rejected**;
- AI-parsed signals go `llm_parsed=True` -> **pending, never
  run in auto** (need human approval).

| Variable | Use |
|---|---|
| `SIGNALBOT_LLM_API_KEY` | API key (presence turns fallback on) |
| `SIGNALBOT_LLM_BASE_URL` | OpenAI-ready endpoint (default `https://api.openai.com/v1`) |
| `SIGNALBOT_LLM_MODEL` | Model (default `gpt-4o-mini`) |
| `SIGNALBOT_LLM_MIN_CONFIDENCE` | Min confidence 0..1 (default 0.8) |

### 5.1 Telegram listener

`app/telegram_listener.py` is a split process (runs next to uvicorn) that
uses the **user account** (Telethon) to hear the group. Each message and
each edit forwards to `POST /api/signals` with `message_id`, so
the edit window (4.1) applies. Setup by env vars
(or `.env`): `SIGNALBOT_TG_API_ID`, `SIGNALBOT_TG_API_HASH`,
`SIGNALBOT_TG_PHONE`, `SIGNALBOT_TG_GROUP` (link, `@username` or id),
`SIGNALBOT_API_URL` and `SIGNALBOT_TG_FILTER` (0 = forwards all). The
startup history replay uses **`SIGNALBOT_TG_HISTORICAL_LIMIT`**
(message count to walk; `0`/missing = ALL from group start;
old alias `SIGNALBOT_TG_HISTORY_LIMIT`). `history_limit()` resolves the
value - returns `None` for "no cap" because `iter_messages(limit=0)`
would return zero messages on Telethon. At replay end, the listener counts
forwarded messages per action (signals/management/summaries/ignored) and
tells the server by **`POST /api/listener/history_done`** (`state.history_done`)
- which logs the "📚 History replay done" note on the feed.

---

## 6. Setup

Keys stored in the SQLite `config` table (set on dashboard):

| Key | Default | What |
|---|---|---|
| `mode` | `auto` | Single path since Item 12 - POST with another value is refused with error |
| `risk_pct` | `2.5` | Risk per trade (% of equity) - base of moving risk (slider 0.1-10) |
| `peak_balance` | `0` | Equity peak (high-water mark) for moving risk; 0 = not yet set |
| `daily_loss_limit_pct` | `3.0` | Daily loss cap that fires auto kill |
| `max_positions` | `5` | Max open trades at once (the 5-leg strategy needs up to 5) |
| `kill_switch` | `false` | Global pause |
| `auto_kill` | `false` | Auto block by daily loss |
| `killswitch_rearm_at` | empty | Stored ISO UTC stamp of auto re-arm; by default set for 2026-08-29 00:00 UTC when kill turns on |
| `promotion_window_start_at` | `2026-08-20T00:00:00+00:00` | Start of the real review window to 28/08 |
| `promotion_window_end_at` | `2026-08-29T00:00:00+00:00` | End of the 28/08 UTC window |
| `sim_speed` | `1` | Mock market speed factor (0 = pause) |
| `edit_window_seconds` | `20` | Wait window for Telegram edits (only when first parse is incomplete) |
| `entry_guard_tolerance` | `0.5` | Auto margin in price units outside zone; above it asks manual confirm |
| `summary_time` | `23:59` | Daily summary time |
| `balance` | `10000` | Mock/fallback balance; in `mt5-real` the broker balance is truth |
| `summary_sent_date` | - | Last date the summary was sent |
| `strategy` | `tp2_protect` | Main-account active strategy (run state on 2026-09-01) |

**Current VPS run setup (2026-09-01):** `strategy=tp2_protect`,
`risk_pct=4.0`, `peak_balance=1045.56`, `max_positions=5`, `kill_switch=false`,
main hedged demo account `5054644473`, seen balance EUR 1,003.89 and
`MT5_DRY_RUN=0`. The server is in `mt5-real`; approved orders go
to the demo account. This setup does not allow a real account.
| `shadow_paper_enabled` | `true` | Takes full live signals in an apart paper account, even with kill switch on; never sends MT5 |
| `shadow_paper_strategies` | `["auto_escada_3", "auto_escada_5"]` | Visible IDs that take shadow signals; checks vs `STRATEGIES` and rejects hidden strategies |
| `teste_days` | `30` | Test-page picker (7/30/90; 0 = all history) |
| `all_period` | `"0"` | Span picker of the "from start" block of test pages: `"0"` = all history, `"2026-07-28"` = current format only, `"YYYY-MM-01"` = current month only |
| `edge_risk` | `""` | Sim risk on test pages (studies): empty = config `risk_pct` |
| `edge_bal` | `""` | Sim start balance on test pages (studies): empty = default 1000 (own paper-account balance) |

**Daily kill in backtest** - `run_analysis`/`run_compare`/`run_month_sim`
take `daily_stop_pct` (None = config `daily_loss_limit_pct`; 0 =
off). `app/backtest.apply_daily_stop(results, balance, stop_pct)` walks
results in time order; when stored day P&L <=
`-balance*stop_pct/100`, later trades of that day go
`outcome="skip"`/pnl 0 (counted as `daily_stop_skipped` in payload). The
dashboard numbers mirror real production kill.

**Risk studies** - `/api/analysis`, `/api/analysis/compare`,
`/api/analysis/month` take `risk_pct` and `balance` (0 = config/current
balance). Test pages have "Sim risk" + balance pickers
(`testeSimQS()`, stored in `edge_risk`/`edge_bal`; default balance 1000).
⚠️ With balance
~1,000 EUR the `MIN_LOTS` floor (0.01) rules: risks <= ~4% give the same lots
- to study risk use bigger balances (PF does not move with risk; only
P&L/drawdown scale).

**Item 2 reproducible sweep** -
`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation --item2-sweep`
runs `auto_escada_5` on 27 combos (`def_window` 5/7/10 x
`def_trail` 1.0/1.2/1.5 x `def_time_stop` 30/45/60) on current/all/
old windows. Each window baseline is set once and baseline history
is reused, with no leak across combos. The study entry filter
turns tz stamps to UTC, uses the last ATR reading of each
UTC day and sets each signal p20 floor only on prior days; so
it uses no future swings. The gate only takes old DD <50% and current PF >=85%
of baseline; the 19/08 snapshot had 0 eligible and a lone spike on the
current-span `def_window` axis, so no default changed.

**Item 3 reproducible sweep** -
`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation
--item3-sweep` runs two read-only variants of `auto_escada_3` on
`current`, `all` and `old` windows: keeps `max_legs=3`, `stair_levels=3`,
`stair_from=1`, `trail_factor=2.0` and fixed costs, toggling only
`confirm_filter`. Each variant sorts signals in time order, reuses
the same candles and shares no changeable state with the other (no leakage). The
accept gate needs strictly lower old DD and current-PF drop <=10%
relative; the 19/08 replay gave `confirm=False` 1.27/11.8% (current PF/DD),
0.47/322.0% (all) and 0.40/312.9% (old), vs `confirm=True` 1.26/11.8%,
0.53/286.1% and 0.46/278.3%. It passed (current PF drop 0.79%; old DD cut
11.06%), so `auto_escada_3` now uses `confirm_filter=True`; main
`nova_gestao` and the demo account were not promoted/changed. Spec
`docs/Melhorias/03-item-03.md` was deleted after this accept.

**Regime alert** - `state._check_regime()` (tick, each ~30 min):
runs the rolling 30-day sim of the active strategy with config daily kill;
if PF < 1 with >= 10 solved, it sends a `risk` note (feed + Telegram,
6h cooldown). Snapshot shows `regime` (pf/resolved/checked_at/skipped).

**Trade-flow sample** - `GET /api/analysis/trace?strategy=<id>`:
runs the strategy backtest, takes the newest solved signal and
replays with `trace=True` (`simulate_signal` returns time-ordered `events`
- SL/TP1/BE/close at TP - and the candle `path`, downsampled to <= 90
points). The TP2 test page shows chart + timeline
(`loadTesteFlow`/`drawTraceChart`). Only for no-management strategies.

**HELP page** - menu (under Diagnostics): expandable cards per
strategy (`/api/strategies` + parallel 30-day sims) + glossary.

The snapshot also shows **`config.risk_preview`** (`state.risk_preview()`):
lots the current risk% means on a normal XAUUSD trade (total and
per leg, with `n_legs` and the SL gap used - from the last valid
signal by `DB.last_signal_with_sl()`, or 30 points by default). The
dashboard shows them on the "Lots from risk%" chip of the risk
details block.

Optional env vars:

| Variable | Use |
|---|---|
| `SIGNALBOT_DB` | SQLite file path |
| `TELEGRAM_BOT_TOKEN` | Bot token for real notes (optional) |
| `TELEGRAM_CHAT_ID` | Chat/group where notes go (optional) |

With no Telegram creds, notes still work (DB + dashboard).

**OFFLINE phone alerts** (2026-08-17):

- **MT5 off / AutoTrading off** -> the listener (`mt5_watch` in
  `telegram_listener.py`) polls `/api/diagnostics` each 10s and DMs
  the Telegram account itself (Saved Messages) on the on->
  off flip (60s cooldown); the server (`_check_mt5_connection` in `state.py`,
  called on 1s `tick()`) also sends a `risk` feed note
  with 60s cooldown.
- **Listener offline** -> the watchdog (`_ensure_listener_alive` in `main.py`)
  sends a `risk` note each time it restarts the process.
- With `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set, **any**
  note (`Notifier.send`) is pushed to the chat by Bot API.

**"Last group msg"** (Diagnostics): the listener only moves `last_msg_at`
on real message handlers - startup history replay does **not**
count as a new message, so the dashboard never shows "last msg Xs ago"
right after a restart with no new messages.

### Run by MetaTrader 5 (`EXECUTION_MODE=mt5`)

The bot runs on the MT5 terminal when `EXECUTION_MODE=mt5`. In the current
run setup, the main account is demo `5054644473` and
`MT5_DRY_RUN=0`; paper/shadow stays apart for studies:

| Variable | Use |
|---|---|
| `EXECUTION_MODE` | `sim` (default) or `mt5` |
| `MT5_DRY_RUN` | `0` on the current run demo: sends orders; `1` keeps local sim |
| `MT5_LOGIN` | Demo/real account number (current: `5054644473` demo) |
| `MT5_SERVER` | Server (e.g. `MetaQuotes-Demo`) |
| `MT5_PASSWORD` | Password - **never store in files**; pass at start |
| `MT5_SYMBOLS` | JSON `{"XAUUSD":"XAUUSD.a"}` for symbols whose broker name differs |

Needs: MT5 terminal installed and **running** with the account logged in; the
`MetaTrader5` package in `.venv` (`pip install MetaTrader5`). Example:

```bash
MT5_DRY_RUN=0 MT5_LOGIN=5054644473 MT5_SERVER='MetaQuotes-Demo' \
  MT5_PASSWORD='...' .venv/Scripts/python -m uvicorn app.main:app --reload
```

`MT5Engine` (`execution/mt5.py`) uses the same face as `SimEngine`
(`open_trade` / `update` / `close_trade_at_market`), so risk, the
edit window and Telegram do not change. Notes:

- **Per-symbol sizing**: `compute_lots` uses the broker real contract size and pip
  (XAUUSD about contract 100 / pip 0.01; EURUSD about contract 100000 /
  pip 0.0001) - the global `CONTRACT_SIZE` only fits paper.
- **Dry-run only for studies** (`MT5_DRY_RUN=1`): spots are stored in DB and
  fills replayed on the current MT5 tick; each order that *would* go out is
  logged with the `[mt5·dry]` prefix. Nothing hits the broker.
- **Real** (`MT5_DRY_RUN=0`): orders go by `mt5.order_send` (1 order per
  leg, server-side SL/TP) and P&L comes from MT5 spots. After TP1, leg
  2 is guarded to breakeven with a modify (`TRADE_ACTION_SLTP`).
- **Auto-manage (Phase 3, docs/ESTRATEGIAS.md Part X)**: `update(now,
  exit_at_tp, auto)` - the
  `auto` (ladder/trailing/confirm/time stop) only passes when the
  active strategy sets it (plain `nova_gestao` has none -> dead until
  an `auto_*` is promoted). In `_update_real`: TP_k confirms -> `move_leg_sl`
  of later legs to TP_k level (ladder); leg k takes SL ->
  close of legs > k (confirm); after TP1 -> trailing (`SL = peak -
  trail_factor x dist(entry,TP2)`, peak of PRIOR ticks, no
  look-ahead) and time stop. **Three SL levels** (`_valid_sl`): the theory
  target is capped by symbol `stops_level`/`freeze_level`;
  **`_modify_sl` returns the retcode** and `_auto_move_sl` only marks SL as
  *applied* (DB + throttle) when the broker confirms (10009/0) - a 10016
  refuse stays in log with no DB change. **Modify throttle**
  (`_auto_move_sl`): only moves if better by >= 1 pip and <= 1x/min per leg
  (in-memory `_auto_state` per trade - after restart the peak restarts from
  current price, filed).
- **ATR-adaptive trailing** (docs/ESTRATEGIAS.md Part X,
  post-check) - the SAME
  `trail_dist = max(trail_factor x dist, atr_k x ATR)` formula on all three
  engines, always look-ahead free (ATR only uses data BEFORE the event):
  - **MT5** (`MT5Provider.copy_rates(symbol, timeframe, count)` - M1=1/M5=5 enum
    by `copy_rates_from_pos` from pos 1 (full candles)
    + `MT5Engine._atr`); `atr_timeframe`
    only lives here (terminal candle enum);
  - **backtest** (`simulate_signal` + `_candle_atr` in backtest.py - ATR
    of candles before the current one, from the same candle path);
  - **paper run** (`PaperRace._paper_atr` - rolling ATR per trade with
    minute buckets: the current minute is in flight and NEVER enters the ATR
    it uses itself).
  `run_analysis`/`sim_params` take `atr_k`/`atr_period`; with no data
  it falls back to fixed trail. No strategy sets `atr_k` today
  (off) - it is the post-check upgrade.
- **Audit of the three auto SL levels** (check window,
  docs/ESTRATEGIAS.md Part X): `_auto_move_sl` logs each try
  `{ts, leg, kind, target, valid, clamped, retcode, applied}` stored in
  config `autosl:<trade_id>` (JSON, cap 300) - lives through restarts;
  `GET /api/analysis/auto_sl_log` -> `state.auto_sl_log` returns per trade;
  table on the Phase 3 panel of main. A **refused (10016) or
  rounded (clamped)** modify also returns an `auto_sl_warn` event from
  `update` (dedup by trade+leg+target in `_sl_warned`; drained to
  events at update end) -> `state.tick` sends a `risk` note on feed/Telegram
  - the refuse does NOT set throttle (it can land when the broker allows), but
  the warn does not spam.
- **ATR / fine-trail sweep** (`app/sweep_atr.py` - study tool
  like `sweep_auto.py`): atr_k x period grid (or `--trail` for fixed
  trail) on auto_* on 3 windows (current, all, out-of-sample), with candle
  reuse per window. Results: **ATR rejected** (worse than fixed trail
  2.0 - see ESTRATEGIAS.md Part X) and **fine trail = smooth plateau**
  (2.0 inside, stays). `atr_k` stays off on all strategies.
- **Check-window snapshot**: when promoting an auto with auto-manage
  (`POST /api/config`), `state._fase3_promotion_snapshot` writes config
  `fase3_snapshot` (JSON): paper-run stats, account DD (30d
  curve), paper-to-MT5 splits, closed real trades and entry count of the
  SL audit - shown in `snapshot()["fase3_snapshot"]` and on the
  Phase 3 panel (before/after window compare, ESTRATEGIAS.md
  Part X).
- **Window ETA** (`snapshot()["fase3_eta"]`): signal pace/day of
  `auto_escada_3` = paper trades opened in last 30 days
  (`db.paper_trades_opened_since`) / 30; due date of 20 signals =
  `now + (20 - N) / pace`; `done=True` when N >= 20, `eta=None` with no
  pace (run just started).
- **Daily summary with auto MT5** (ESTRATEGIAS.md Part X):
  `state._fase3_mt5_line` -
  when the active strategy is an auto with auto-manage and a snapshot exists,
  the daily summary gains the 🚀 row with real N and real P&L (closed trades
  since `promoted_at`) and window state (running x/10 or verdict).
  If the window closed TODAY (fresh verdict) even with the auto already
  removed (rollback), the row stays in the summary to mark the verdict
  (UTC compare - `evaluated_at` is `_now_iso`).
- **ETA alert** (`_check_fase3_eta_alert`, runs on tick, 24h cooldown
  in `_fase3_eta_alert_at`): `fase3_eta` also returns `pace_prev`
  (30-60-day window pace) and `pace_change_pct`; the alert fires on
  feed/Telegram when the window counts **> 42 days** or pace moves
  **>= 50%** vs the prior month (rebuilt ETA). When it counts
  **> 6 weeks** and the target is still 20, the note carries action
  `fase3_target:15` -> "🎯 Cut target to 15" button on feed ->
  `POST /api/config {fase3_target}` (clamp 10-20; `config.fase3_target`
  default 20; ETA/checklist/panel then count to the new target).
- **Pace mini-curve**: `fase3_eta.pace_daily` = per-day count of
  last 60 days (zero-filled; `db.paper_trades_opened_by_day`) with the
  AUTO (`n`) and `nova_gestao` REFERENCE (`n_ref`) side by side - bars
  with tooltip on the Phase 2 panel (the trend feeding ETA).
- **Single panel endpoint** `GET /api/analysis/fase3_status`
  (`state.fase3_status`): ETA (curve set), before/after snapshot,
  verdicts + history, `target_log`, paper-to-MT5 splits, real vs
  backtest, account DD (30d curve, set on server) and SL
  audit - the panel went from 4 calls to 1.
- **Target-cut log** `fase3_target_log` (config, JSON, cap 20):
  each `fase3_target` change logs `{at, from, to, reason}` - the
  reason comes from `fase3_target_reason` in `POST /api/config` (the feed
  button sends "ETA > 6 weeks (feed button)"). Block on panel history
  + `snapshot()["fase3_target_log"]`.
- **Verdict history**: `fase3_verdicts` (config, JSON, cap 20) -
  each review appends the verdict; a new promotion only clears
  `fase3_verdict` (current window) to reopen review; shown in
  `snapshot()["fase3_verdicts"]` and the panel "📚 History" block.
- **Auto window verdict** (`snapshot()["fase3_verdict"]`):
  `state._check_fase3_validation` runs on tick when the active strategy is
  an auto with auto-manage, `fase3_snapshot` exists and >= 10 real
  trades opened after `promoted_at` closed. `_fase3_window_trades` sorts them
  by close and caps the window at the first 10 (skips open spots
  from before promotion and trades plainly of another strategy). It sets real
  PF, paper-to-MT5 splits (and needs the same exit on >=9/10; missing
  pairs also fail), open spots with no SL, copies, >2 min lag,
  off P&L and `autosl:<trade_id>` retcodes outside 10009/0.
  **Auto rollback** to `nova_gestao` if any gate fails; else
  **keep**. Stores `fase3_verdict` (JSON with stats +
  `snapshot_before`) in config and notes feed/Telegram (`risk` kind on
  rollback, `system` on keep). Runs ONCE per window - a new
  promotion writes a fresh snapshot and reopens the window.
- **"🚀 Phase 3 - ready to start" panel** (main): gate checklist
  (N>=20 · PF>=1.5 · P&L>=50% · DD<=125% of reference · 10-trade
  window), paper-to-MT5 and real-vs-backtest split counts, account DD
  by `/api/paper/history` and SL audit - 60s refresh
  (`refreshPhase3Instruments`), panel render is sync from snapshot.
- **E2E test of the real path** (`tests/test_mt5.py`): STATE fake
  broker (`_FakeBroker` - opens/closes spots, entry/exit deals,
  SL/TP modifies with gap check, retcodes 10009/10016): TP1
  (deal) -> ladder -> trailing -> time stop with `dry_run=False`, real
  retcodes in log + `autosl:` audit with all three levels. The
  `test_fase3_promotion_paper_to_mt5_restart_and_rollback` check adds the
  paper -> promote -> restart -> refused-retcode -> rollback path with no
  touch of the real terminal.
- **Hedged accounts**: the TP1..TP5 plan uses N standalone legs -
  it needs a *hedged* account (e.g. `Forex Hedged`), not `netting`.

---

## 7. How to run

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux/macOS

.venv/Scripts/python -m uvicorn app.main:app --reload
# Dashboard: http://127.0.0.1:8000/
```

Tests:

```bash
.venv/Scripts/python -m pytest -q
```

### Run with the Telegram group

Window 1 (API + dashboard):

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Window 2 (group listener):

```bash
.venv/Scripts/python -m app.telegram_listener
```

On first run the listener asks the Telegram check code and
makes `signalbot_session.session` (skipped by git).

---

## 8. Notes

`notify/telegram.py` sends to three targets in parallel:

1. `notifications` table (always);
2. WebSocket -> dashboard (always);
3. Telegram (only with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`).

Events: `trade_open`, `trade_close`, `be` (breakeven guard),
`risk` (blocks/kill switch), `system` (setup), `summary` (daily summary),
`management` (group state-machine actions).

The daily summary (`state.py:daily_summary`) sums closed trades of the day:
trade count, total profit and win rate. It is sent at `summary_time` and can
be forced by `POST /api/summary`. It also adds the UTC-window row to
28/08 with closed/open real trades, made/open P&L, days left and
pace needed for the 10-20 targets.

`GET /api/window` is read-only and returns that window, real trades of the
`trades` table, P&L, forecast/pace and `promotion`: shadow forward gate of
`auto_escada_3` (N, PF, IC95, DD, reference) + pace ETA. Promotion
stays manual and backend-blocked until all gates pass.

The kill switch can store `killswitch_rearm_at`. `state.tick` compares the
UTC time; when due, it clears `kill_switch`, `auto_kill` and the key itself,
logs a `system` note on feed and Telegram and does not touch already-open
spots. An already-run re-arm is idempotent.

---

## 9. Safety

The dashboard/API have **password login** when `SIGNALBOT_DASHBOARD_PASSWORD`
is set (`app/auth.py` + middleware in `main.py`). With no var,
all stays open (local use). Still open before web show:

- [x] Dashboard login - done (single password, httpOnly cookie, in-memory sessions);
- [x] Listener API token (`X-API-Token`) - done;
- [ ] HTTPS (real trade run must never run over plain HTTP) - use reverse proxy (Caddy) on VPS;
- [ ] Split the run endpoint (only reached from the VPS/VPN itself);
- [ ] Backend risk caps as end authority (already there);
- [ ] Audit log of all actions (who clicked what, when).

---

## 9b. MT5 bridge limits

- The MetaTrader5 API keeps **ONE global link per process** - the 2nd account
  (`MT5_2_*`, `tp2` lane) cannot share the server process in real
  mode. Real run of the 2nd account uses the **apart worker**
  (`app/lane2_worker.py`): a split process owning the link to the **portable** terminal
  (`MT5_2_TERMINAL_PATH`) that shows ops over local HTTP
  (token `LANE2_WORKER_TOKEN`); the main process reaches it by
  `RemoteProvider` (`app/execution/remote2.py`) with the same face as
  `MT5Provider` (with `provider._mt5`, the raw MT5 module that
  `MT5Engine` uses - set by `/mt5/call` with method allowlist and
  constants). The worker **never** touches SQLite; calls and writes stay
  in the main process (concurrency rule kept).
  Fail-closed: offline worker/missing terminal/non-hedged account means lane 2
  sends no orders; the market_loop pings it ~1x/s and notes flips; the
  `healthcheck.py` restarts it (`worker_*` in `healthcheck.state`),
  `start.ps1`/`boot.ps1` start it. ⚠️ With no `MT5_2_TERMINAL_PATH` the worker
  **refuses to start (exit 2)** - `initialize(login=...)` with no path links to the
  default terminal and swaps the main account (01/09 event).
  Worker `/status` has **`trade_allowed`** (by `terminal_info`)
  mirrored by `RemoteProvider` - Diagnostics shows ON/OFF and the
  market_loop warns when terminal 2 AutoTrading is OFF (per-session;
  `lane2_auto_trading_off` event). Read-only A/B of lanes on the same signal:
  `GET /api/lanes/ab` (`app/lanes.py::lane_ab_report`) - side-by-side
  metrics and keep hint **only with >=20 comparable signals**
  (`min_comparable`, default 20); under that the verdict is neutral. **phantom** closes
  (with no `exit_reason` - refused orders, closed at 0 on send)
  stay out of the comparable set in `voids`/`void_count`; with n=0 and voids the
  message explains the sample (real 01/09 case: AutoTrading OFF on lane
  2). It never changes strategy/setup nor sends orders.
- `initialize()` needs the terminal open; if it fails, the dashboard stays
  reachable, but state shows `connected=false`. In real run it also
  needs `account_info().margin_mode == 2` (`RETAIL_HEDGING`); netting or unknown
  mode blocks the link and no real order is sent.
- Fresh Market Watch symbols can return `0.0` for ~1s
  while the terminal pulls quotes - the provider skips those ticks.
- `open_trade` calls `ensure_symbol` BEFORE sending orders; `_send_open_orders`
  takes a None/zero `symbol_info_tick` as a leg fail (does not crash and does not
  repeat the whole trade).
- Global `PIP_SIZE`/`CONTRACT_SIZE` (`config.py`) only fit
  paper; MT5 always uses the real broker values per symbol.

## 9c. Run, logs and restore

- `app.server_runner` sets `RotatingTextStream` before Uvicorn makes
  handlers. `SIGNALBOT_LOG_MAX_BYTES` (default 10 MiB) and
  `SIGNALBOT_LOG_BACKUP_COUNT` (default 5) cap `server.log`,
  `server.err.log`, `listener.log`, `listener.err.log`, `lane2_worker.log`,
  `lane2_worker.err.log`; `backup.log` uses the same setup. On Windows
  do not use outside redirect for logs, because it keeps the file
  open and blocks `os.replace`.
- `BotState._check_disk_space()` is read-only and runs once a day. Volume
  and bar come from `SIGNALBOT_DISK_PATH`/`SIGNALBOT_DISK_FREE_GB` (default
  system volume and 10 GiB). The snapshot shows `operations.disk`; under the
  bar it makes a `risk` note on feed and, if set, on Telegram.
- `maintenance.py --db-audit` lists counts, months seen, rows/month
  rate and hold policy. `backup_db.py --restore-check` copies the
  newest backup to a temp DB, checks `PRAGMA integrity_check`
  and the main tables; it sends the result to Telegram when set.
  The routine never opens the production DB for write.

Current run policy (the `--db-audit` command prints the same policy and
seen counts):

| Tables | Policy |
|---|---|
| `signals`, `trades`, `positions`, `paper_trades`, `paper_positions`, `group_summaries`, `config`, `paper_accounts`, `protection_states` | keep forever; they are run history, audit or current state |
| `pending_management` | archive applied rows after 90 days; keep unapplied ones |
| `notifications` | archive after 90 days, keep risk/rollback |
| `paper_balance_history` | archive after 180 days, keep sets |
| `protect_latency_log` | archive after 365 days, keep sets |
| `listener_restarts` | auto purge after 30 days |
| `ignored_messages` | last 1000 rows, cap applied on insert |

## 10. Roadmap

1. ✅ **Real Telegram ingest** - `app/telegram_listener.py` (Telethon)
   reads groups (many), honors the edit window and handles deleted
   messages (closes the spot at market).
2. ✅ **Group history** - on connect, the listener sends the full history
   by default (`historical=True`), stored with no run;
   `SIGNALBOT_TG_HISTORICAL_LIMIT` caps replay at N messages (0 = all).
3. ✅ **Real market data** - live feed (Yahoo/TwelveData) with mock
   fallback; candle history for backtest.
4. ✅ **Backtest/Sim** - replay of real signals on history candles
   with set lag (1 min by default) and **real costs** (spread,
   slippage, commission).
5. ✅ **Report** - win rate, profit factor, R:R, per-group shape,
   **equity curve** and **max drawdown**.
6. ✅ **Real run** - link to MetaTrader 5 (Python API on a Windows VPS,
   or MQL5 EA) with the same risk engine in front (MT5Engine in dry-run/real).
6b. ✅ **Group management** - state machine for TP HIT / SL HIT / close
   first / out at BE, + group summaries as dashboard ground truth.
6c. ✅ **LLM fallback** - optional AI parser with sanity checks, never auto.
6d. ✅ **Backtest with group management** - replays `protect`/`close_all` on the
   same signals and compares (Edge) WR/PF with and with no management vs the group
   reported.
7. **Live mode with caps** - start at min risk (0.1-0.5%) and check
   for weeks before raising.

---

## 10.1 - Defensive risk, shadow-paper and restart-safe

`auto_escada_3`, `auto_escada_3_hibrida` and `auto_escada_5`
share `effective_strategy_risk_pct`: when 10 prior closed
trades have PF < 1.0, new-entry risk is capped at
`def_risk_pct=1.25%`, besides normal drawdown scale. The cap never raises an
already lower base risk. The same call runs in backtest (time-order
state), PaperRace per origin, SimEngine and MT5Engine.

The SQLite `protection_states(engine, origin, trade_id, state_json,
updated_at)` table stores peak/valley, TP1, steps, mechanical SL, stamps and ATR
buckets. `PaperRace`, `SimEngine` and `MT5Engine` load this state before
each tick; closing the trade erases the row. After restart, the MT5
monotonic modify throttle restarts, but price guard stays.

Shadow-paper is set for `auto_escada_3` and `auto_escada_5`, with
`origin="shadow"`, apart accounts and pending management. It keeps running with no
call to the MT5 engine, even with main blocked, and does not enter the normal
curve or the race split report.

## 11. Known limits

- The random walk is only the fallback when the outside feed is down;
  live feed and backtest use real prices/candles when they can get them.
  Even with real candles, broker spread, slippage and latency can split
  from modeled costs.
- MT5 run needs an open terminal, active AutoTrading and a
  `RETAIL_HEDGING` account; the system blocks a netting account, but cannot change the
  account kind by code.
- `realized_today`/daily cap follow the UTC civil day on paper accounts and the
  stored server stamp in DB; confirm tz when digging a
  historic alert.
- Dashboard login is env-optional; in production it must be
  on and HTTPS is the job of the Caddy/reverse proxy in
  `DEPLOY.md`.
- The dashboard stays vanilla JS/single-process; SQLite has an inner lock,
  but it is no setup for many API copies writing the
  same DB.
