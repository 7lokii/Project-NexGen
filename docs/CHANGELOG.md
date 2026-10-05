# Changelog - Project NexGen

Log of program changes. **Rule**: whenever something is
built/changed, add an entry here (date + what changed +
test count). Docs (AGENTS.md, TECHNICAL.md, PUBLIC.md) must stay in
step in the same turn.

Format: `### YYYY-MM-DD - title` · bullets with what/why.

---

### 2026-09-30 - Test env set up and test count fixed

- **`.venv` made in the project** (Python 3.14.2, the only Python on this machine).
  All `requirements.txt` deps installed, including
  `MetaTrader5 5.0.6231`, which has a wheel for 3.14. With no such env there was
  no way to run the suite on this PC.
- **`AGENTS.md` said 456 tests; the suite has 631.** The real count was measured
  with `pytest --collect-only -q` once the env existed, and it matches what
  `operacao-e-vps.md` and this changelog already said. `tests/test_docs.py`
  counts the suite and needs `AGENTS.md` to state that count, so the
  test failed and CI stayed red. Fixed.
- **6 `test_api.py` tests are red, and not from this change.**
  `test_report_includes_by_day`, `test_recount_candle_check_daily_futures_offset`,
  `test_rejection_report_categories_and_replay`,
  `test_rejection_report_zone_and_orphans`,
  `test_rejection_report_includes_ignored_and_strategy` and
  `test_report_accepts_lane_filter`. One proven cause: they seed signals and
  trades on fixed August 2026 dates and call endpoints with `days=30`,
  which filter by `date.today() - days` (`app/state.py:1258` and
  `app/analysis.py:385`). Past day 30 after the seeded date, data
  falls out of window. With the clock frozen at 2026-08-22, 2026-08-25 or
  2026-09-15, all 6 pass; today, 2026-09-30, all 6 fail. No code
  change: `git diff` shows `app/` untouched. **Fixed**: the 6 now seed
  relative dates, with two helpers at the top of `test_api.py`, `_dias_atras(n,
  hora, minuto)` and `_dia_atras(n)`. It follows the lead the project already used
  (relative backdate at line 3801 and the `_stamp` helper of
  `test_observability.py`); the global clock was not frozen so the
  other 625 tests stay put. Full suite: **630 pass, 1 skips, 0 fail**. The 6
  pass with the clock frozen at 2027-09-30, 2028-02-29 and 2030-12-31, so
  the time bombs stay disarmed.
- No behavior change in the program: env and doc sync only.

### 2026-09-16 - Fixed tp2 lane boot + boot fail is not runtime block + WhatsApp single-lane + CLOSE ALL on tp2_protect

- **tp2 boot ALWAYS failed with a healthy worker** (root cause): lifespan
  set up the LOCAL `mt5_provider2` - which has a permanent block by
  design when `MT5_DRY_RUN=0` ("the MetaTrader5 API uses one global link
  per process") - instead of the worker `RemoteProvider`. The feed showed
  "⛔ MT5 run blocked on lane tp2 ..." at every start and the lane
  only came on later by market_loop reconnect. Now `_lane2_provider()`
  (`app/main.py`) returns the SAME provider reconnect uses (worker
  when `LANE2_WORKER_ENABLED`, local in dry-run).
- **`RemoteProvider.initialize` no longer pollutes `execution_block_reason`
  with the PASSING worker-offline reason** (`app/execution/remote2.py`):
  the reason stays in `_offline_reason` - a truthy `execution_block_reason`
  made `_reconnect_mt5` skip the lane forever (tp2 failed at boot
  and never tried to reconnect).
- **Boot fail is not runtime block on the feed**: the passing start
  failure notes kind `system` ("⏳ [lane] MT5 did not connect at startup -
  passing"); the lasting block (non-hedged account, wrong config)
  stays kind `risk` ("⛔"). When reconnect/retry links the lane that
  failed at boot, it sends RECOVERY ("✅ [lane] MT5 reconnected after startup
  failure").
- **One background retry at startup**: its own lifespan task
  (`nexgen:boot_retry`, cut at shutdown) retries ONCE lanes that
  failed at boot, after `MT5_BOOT_RETRY_DELAY` (default 30s) - it covers the
  race with the portable-worker start by boot.ps1 (the worker takes
  seconds to open the port/link the terminal and the 1st initialize misses it).
  Win -> recovery note; lasting block -> `risk`. If
  it fails again, market_loop reconnect (10s throttle) goes on.
- **WhatsApp: ONE message per signal, only from lane 2** (`app/whatsapp_mixin.py`):
  before, main and tp2 closed on different ticks and per-tick dedup did not
  cover - the group got TWO messages (dual-lane) for the same signal. Now
  the hook filters `lane_id == "tp2"` (main closes send none) and uses the
  single-lane `format_trade_close` shape with the tp2 account tag. The
  smoke test uses the same rule (`notify_smoke`).
- **`tp2_protect` honors `close_all`** (`app/strategies.py`, user
  call): when the group says "CLOSE ALL" / "we're out", both lanes
  exit at market like `nova_gestao` - before tp2_protect skipped it and lane
  2 kept running to TP2 (seen live 16/09 12:08).
  Still skips `close_tp1`/`move_sl`. New name: "Exit at TP2 +
  protect + close_all".
- **Diagnostics/DB**: new `boot_retry_success`/`boot_retry_failure` events
  in `mt5_connection_events`.
- tests: 631.

### 2026-09-05 - Lasting revive marker in signal history ("🔄 de rejected" badge + stored reason)

- The signal now stores **which state it came from when the window reopened**:
  new `revived_from` / `revived_reason` / `revived_at` columns on `signals`
  (auto migrate in `db._migrate`), filled in `_revive_signal` with the
  prior state and the stored **reason** of the PRIOR refuse/error.
- The dashboard **Signals** table now shows a `🔄 de rejected` badge
  (dashed green) next to state, with a tooltip holding reopen time and
  prior reason; the marker **lives through run** - after the reopened
  signal runs (or is rejected again), history still shows
  that signal came from a rejection, at first sight.
- The marker lives through later in-window edits (`_apply_edit`
  copies `revived_*` from the stored one) and shows in the snapshot (`signals` -> `to_dict`)
  and in the `POST /api/signals` answer (`signal.revived_from/reason/at`). The feed
  keeps the kind `revived` alert with the reason in text ("🔄 Revives" group).
- **Tests**: new asserts on `rejected` revive - marker in payload,
  store and snapshot show after run.
- tests: 592.

### 2026-09-05 - `rejected` signal reopens the window when the group re-edits with valid content (settable revive window)

- **Before**: a rejection (zone/cap guard, min lot, global risk) was
  end - a later group edit fell on "signal already done
  (rejected)" and stayed mute. The refuse was about the OLD content/price, but
  the new group intent was lost.
- **Now** (`app/state.py::_ingest_telegram` + `_revive_signal`): if the
  same Telegram message is re-edited with VALID content in
  `rejected_revive_minutes` (default 10, env `SIGNALBOT_REJECTED_REVIVE_MINUTES`)
  from first arrival, the signal goes back to `awaiting_edit` (`action: revived`,
  `revived_from: rejected`) and runs the pipeline again - with a **plain alert**
  on the feed (kind `revived`): "The PRIOR entry was refused (<reason>).
  Ns window reopened." The reason comes from the stored `error` of the rejection.
  AI-only parse (LLM) stays log-only (Item 12, never
  runs). After the window, the refuse is reviewed honestly: it can
  run or be rejected again.
- **After the window**: a late re-edit (>X min) reopens NOTHING (we do not
  chase entries refused long ago) - but the operator gets
  a `system` warn that the group touched it again with valid content and the
  prior refuse stands (only when the re-edit parses, not on partial
  edits).
- **Setup**: `rejected_revive_minutes` in DEFAULT_CONFIG (seen in
  `/api/status` -> `config`), float cast at boot. The `failed` revive
  keeps its fixed 5 min window (same).
- **Tests**: `test_rejected_signal_revived_when_group_reedits` (cap rejection
  -> re-edit -> revived + alert with reason -> runs with new
  content) and `test_rejected_signal_edit_after_revive_window_alerted_and_ignored`
  (window out -> stays rejected + warn).
- tests: 592.

### 2026-09-04 - Signal rejection shows the REAL block of each lane (07:32 XAUUSD sell event)

- **Symptom**: XAUUSD sell (4475-4479, TP1..TP4, SL 4483) rejected at
  07:32:27 with "min lot does not fit set risk on main (16.00 >
  10.02)" - no orders sent on EITHER lane and lane 2 missing from the
  reason. The group still sent "SL HIT" 11 min later (price had
  crossed SL; the same-signal shadow-paper closed straight at SL:
  nova_gestao -32 EUR, auto_escada_3 -24 EUR at 07:32:28 - running it meant entering
  sure to lose).
- **Root cause**: the min-lot gate is PRE-set per lane at loop start
  (stays in the `exceeded` dict) EVEN when the lane is blocked
  BEFORE by the zone guard (price outside zone between first guard and
  run, with no manual confirm in standalone mode). The reject branch
  only showed the over-gate sum -> blamed min lot (which
  was never the cause - with SL 8 and auto-fit, 1 leg of 0.01 fits in EUR 10.02)
  and muted the real lane notes.
- **Fix** (`app/state.py`): when no lane opened and gates are over,
  the reject message shows FIRST the real per-lane notes
  (zone/risk/exception) and only adds the min-lot sum if some lane
  really hit the gate and did not fit. Zone notes are no longer
  swallowed; the pure min-lot case keeps the prior message whole.
- **Test**: `test_reject_message_mostra_bloqueio_real_quando_zona_ganha_ao_lote`
  (guard 1 in zone -> guard 2 outside zone -> rejection shows zone and NOT
  min lot).
- tests: 590.

### 2026-09-04 - `snapshot.build` stalls gone: SQLite WAL + caches (worker, info blocks, reused lanes)

- **End root cause of the 3.5-6.4s `snapshot.build` stalls found and
  gone**: SQLite ran in `journal_mode=delete` on a 647MB
  file - with the run journal on (~3M rows), EACH commit of the
  per-tick quote inserts (1x/s) fsynced the whole file: 15-580ms
  per commit (measured on a DB copy), freezing the event loop. The link
  now opens in **WAL + synchronous=NORMAL** (`app/db.py`): ~0ms commits,
  reads do not block writes and checkpoint is async; no
  crash corruption (only loses last transactions on a power cut
  - fine for journal/telemetry). Backup (`backup_db.py`, sqlite3
  API) and tests work with WAL.
- **RemoteProvider (lane 2) now caches `account()`/`positions()` with
  TTL** (same meaning as local `MT5Provider`) - before EACH call was
  one HTTP to the worker (15-160ms): the snapshot read the account 5-8x and
  spots 2-3x per lane per tick. RUN (engine ->
  `_mt5.positions_get`) stays OUT of cache - never stale for SL/TP
  calls.
- **`_execution_state` reuses the already-built lane snapshot** instead
  of building lanes a 2nd time per snapshot (it copied ~8 MT5/worker
  reads per lane per tick).
- **TTL cache for costly snapshot info blocks** (`_ttl_get` on
  `BotState`): per-lane `risk_preview` (5s, called 3x/tick),
  `paper_status`/`shadow_paper_status` (10s), `window_status` (10s -
  it loaded `list_trades(200000)` per tick!) and `fase3_*` (60s). The per-tick
  snapshot no longer pays ~20 DB sets per second; handlers that
  CHANGE state clear the whole cache. Freshness: info
  panels update in <= TTL (5-60s) - read-only for run.
- **Bounded journal + cached stats**: `execution_event_stats(7)` (GROUP
  BY of ~1s over millions of rows) with 60s cache and set
  `EXECUTION_JOURNAL_RETENTION_DAYS` hold (default 30) with purge
  at start and on watchdog (~6h) - the table never grows with no
  cap again (3M rows -> first purge cut 52k+).
- **Live result**: `snapshot.build` from 3.5-6.4s to ~0.5s in steady
  state (no >=2s steps on stallwatch); healthcheck logged NO
  "no HTTP answer" note since the WAL restart (before it ramped and
  killed the server).
- tests: 589.

### 2026-09-04 - Event-loop stall watch: when + where (⏱️ Loop stalls block on Diagnostics)

- **Symptom to fix**: healthcheck logged "no HTTP answer" and
  logins hung with no word WHEN or WHERE the event loop froze.
- **Freeze monitor** (`app/stallwatch.py`): its own lifespan task
  (outside BackgroundRuntime) that wakes each 0.5s
  (`LOOP_STALL_INTERVAL`) and measures real wakeup lag with `loop.time()`
  (monotonic) - a loop blocked N seconds makes the task wake N seconds
  late. A freeze >= `LOOP_STALL_THRESHOLD_SECONDS` (default 2s) is
  stored in `loop_stall_events` (new table) with kind `stall`.
- **Cause credit**: instrumented on-loop steps write kind `phase`
  when they take >= bar - `tick.update:main`/`tick.update:tp2` (the
  per-lane engine.update on tick), `snapshot.build` (snapshot build
  at tick end) and `diagnostics.on_loop` (on-loop part of Diagnostics that
  reads the terminal) - symptom and cause show side by side in DB.
- **"⏱️ Loop stalls" block on Diagnostics**: last-24h total + latest
  rows (time · length · loop vs step event) with monitor bar and gap;
  with no events it shows "monitor on" (proof the
  watch runs).
- **Bounded**: 14-day hold (`purge_old_loop_stalls`) on the
  watchdog periodic purge (~6h) and at start, and **per-kind rate cap**
  (`LOOP_STALL_MIN_GAP_SECONDS`, 30s between rows of same kind+phase) -
  a chronic shape (e.g. snapshot.build at 4-9s per tick) shows on
  Diagnostics (1 row/30s) with no DB fill. Read-only for run:
  never calls nor sends orders.
- tests: 584.

### 2026-09-04 - A/B with per-lane sizing on Diagnostics + shielded event loop (cached snapshot, worker/subs outside loop, fast boot, healthcheck with boot grace)

- **"⚖️ Lane A/B" Diagnostics shows EACH account sizing**: the
  `GET /api/lanes/ab` call joins `main.sizing`/`tp2.sizing` (base risk
  `risk_pct:<lane>`, fixed lot `lots_override:<lane>`, lots/leg, total and
  preview leg count) - the P&L compare reads with set
  spots in context (e.g. main 1.0% vs tp2 0.6% · fixed 0.04 lots/leg).
  Read-only; math runs off-loop (to_thread).
- **Root cause of healthcheck "no HTTP answer" and hanging logins:
  blocking I/O on the single event loop.** (1) `/api/status`
  rebuilt the snapshot - MT5/worker broker reads - per REQUEST; the
  market_loop now builds the snapshot 1x/s and read-only endpoints
  serve `BotState.last_snapshot()` (cache clears on handlers that
  change setup/signal/kill switch so a GET right after sees the write);
  (2) Diagnostics ran worker `status_summary` + `netstat`/`schtasks`
  subs in the handler itself - `asyncio.to_thread`;
  (3) the listener watchdog ran the PowerShell sub on the loop -
  `to_thread` + 8s timeout.
- **Fast boot**: lifespan ran `initialize` + up to 10 retries with
  sleeps (30-40s where the port hears but HTTP still does not serve) - the
  healthcheck saw "no answer" and KILLED the server mid-boot
  (restart wars, bind 10048, login/dashboard broken for minutes).
  Now: one try per lane, in parallel and in a thread
  (`_initialize_mt5_lane_at_boot`); on fail the market_loop reconnect
  (10s throttle) repeats in background with the server already serving.
- **Healthcheck with boot grace**: a process holding the port but under
  5 min old (`HEALTHCHECK_BOOT_GRACE`) is never killed (age by
  Get-CimInstance); fails go back to 1 and the next pass calls - the
  same for the lane 2 worker.
- tests: 580.

### 2026-09-03 - ROOT CAUSE of null `'?'` retcodes on lane 2: empty kwargs on remote `order_send` (worker fix)

- **Symptom**: since lane 2 went real (01/09) NO order
  filled - all legs closed phantom with `'?'` retcode (`null` module
  answer), plus one `NO_TICK`, though worker/terminal/account/
  AutoTrading were healthy and MANUAL orders on the portable terminal
  filled in 43-550 ms.
- **Trial split** (03/09, demo account, 0.01 orders opened and closed
  right after - DB whole):
  1. standalone (same module/terminal/login) -> `order_send` fills (10009);
  2. real worker and in-process copy -> `None` + `last_error`
     `-2 "Unnamed arguments not allowed"`;
  3. call-shape split in the SAME process: `mt5.order_send(req)`
     direct -> 10009; `fn(*args)` -> 10009; **`fn(*args, **{})` (empty kwargs
     dict) -> None `-2`**; `fn(request=req)` -> 10013 with zeroed request.
- **Root cause**: this module build C wrapper of `order_send`
  (MetaTrader5 5.0.6090, cp314) refuses ANY call with a kwargs
  dict present - even empty. The main account calls `order_send(request)`
  straight in bytecode (no dict) and fills; lane 2 always goes by
  HTTP/JSON -> `fn(*args, **kwargs)` with `kwargs={}` -> `None` -> `'?'`
  retcode -> phantom. No order hit the terminal journal (so the terminal
  log showed nothing) and the null-retcode alert/backfill only
  logged the symptom, never the cause.
- **Fix** (`app/lane2_worker.py::_mt5_call_impl`): the empty kwargs dict
  never hits the module again - `fn(*args)` when no kwargs exist; real
  kwargs still pass (positions_get/orders_get/order_delete with
  `symbol=`/`ticket=` stay same). Module calls stay set on
  one thread and `last_error()` capture when an order
  returns `None` stays too - a future `None` again writes the real module reason
  instead of only `'?'`.
- **Check**: in-process copy and REAL worker (8770) now fill
  0.01 XAUUSD orders by `/mt5/call` with `kwargs:{}` - open and close
  retcode 10009. `tests/test_lane2_worker.py`: 24 passed.

---

### 2026-09-03 - Shape marshaling (shape grid), worker boot self-probe and guided lane 2 smoke test

- **Marshaling BY SHAPE** (`app/lane2_worker.py`): the allowlist is now
  an `MT5_CALL_SHAPES` grid (positional vs kwargs-only, with arg
  names) and the `normalize_mt5_call()` normalizer makes sure NO call
  with wrong shape hits the C core - positional funcs
  (`order_send`/`symbol_info_tick`/`symbol_info`/...) never take a
  kwargs dict (kwargs merged into args by name); kwargs-only
  (`positions_get`/`orders_get`/`order_delete`/`history_deals_get`) never
  take positionals; bad shapes (too many args, unknown
  kwargs, missing musts) are refused with a plain error BEFORE
  the module. The kwargs `symbol_info_tick` ("takes no keyword
  arguments") seen on real /status is also blocked here.
- **Worker self-probe** (boot and each reconnect): an `order_send` of a
  deliberately bad symbol (`__LANE2_PROBE__`, zero cost - the
  broker refuses with a normal retcode) runs ONCE per link episode
  on the module thread; if it returns `None` with `last_error`, /status shows
  `probe: {ok: false, error}` with the REAL module error and market_loop
  alerts `risk` (`lane2_self_probe_failed` event, 10 min cooldown) - a
  dead module/terminal no longer gives quiet `'?'` phantoms and is
  caught at start.
- **Guided dual-lane smoke test** (`app/smoke.py` +
  `POST /api/lanes/smoke`): opens and closes at market one REAL 0.01 trade
  (XAUUSD - EURUSD only as fallback: the lane 2 broker refuses with 10030) on
  EACH demo account by the real engines of both lanes, with BOTH trades
  sharing the SAME signal - it writes rows (tagged
  `execution_mode='mt5-smoke'`, `strategy_id='smoke_test'`,
  `exit_reason='smoke_test'` so analytics never reads them as strategy
  output), sends per-lane `close`/`trade_closed` events and fires
  the dual-lane WhatsApp note with both accounts side by side and summed P&L.
  If one lane fails to open, the one that opened closes at once
  (rollback - never leaves an orphan spot). Demo-only guards ALWAYS: opt-in
  `LANE2_SMOKE_ALLOW=1` in .env, BOTH lanes real (dry_run=False) with
  linked terminal/worker and AutoTrading ON, DEMO accounts must
  (`trade_mode == 0` - on real it refuses before building the call) and
  cooldown (`LANE2_SMOKE_COOLDOWN`, default 120 s). Test trades
  stay tagged so analytics never reads them as strategy output.
- **Tests** (`tests/test_lane2_worker.py` + `test_api.py`): check that
  repeats ALL engine call shapes by the remote path
  (positionals with no kwargs / kwargs-only with kwargs / refuses with no module
  touch), probe ok/fail/reconnect + mirror on RemoteProvider, smoke guards
  (real account refuses, dry-run/offline/AT-off/cooldown) and E2E of the DUAL
  round trip (in-process main engine + remote worker) with rollback when
  the 2nd lane fails to open and dual-lane WhatsApp note with both
  real trades.
- **LIVE proof** (03/09, demo accounts 5054644473 + 5055369549,
  `LANE2_SMOKE_ALLOW=1`): XAUUSD 0.01 round trips by real engines -
  tickets 58300164867/... (lane 2) + 58300238944/..., P&L per run -0.18 to
  -0.34 EUR per account (spread+slippage), dual-lane WhatsApp sent ONCE
  per run (sent_count 10->14 on the bridge, group
  120363409845259131@g.us). Live finds:
  (1) the module wrapper refuses comments >28 chars (`-2 Invalid "comment"
  argument` -> None -> `'?'`) - smoke ids are now short
  (`t-<epoch>-<rand>`, `:legN` comment <= 28; the 2nd lane takes `-tp2` suffix);
  (2) the lane 2 broker refuses EURUSD with 10030 (trade disabled) though
  it has a tick - XAUUSD stays first in choice; (3) the
  `leftover_open` of the report read the provider with a 2s cache (a just
  closed spot still showed): the check now reads `positions_get`
  straight from the module (no cache).
- **Fixed lot per leg on the 2nd account** (`POST /api/config`,
  `lots_override_tp2`): when set (e.g. 0.04), each lane 2 leg
  always opens at that lot instead of risk sizing - the
  `risk_pct:tp2` no longer moves lots with SL gap (the last
  lane trade had come out at 0.06/leg). Stored in
  `lots_override:tp2` (0 = back to risk); risk_preview and lane snapshot
  show the fixed lot (`lots_override` in preview,
  `fixed_lots_per_leg` in the lane entry). Main stays same.
- **Per-lane setup lives through restarts**: `risk_pct:tp2`,
  `lots_override:tp2` and `peak_balance:tp2` were stored but NOT
  reloaded at boot (`_load_config` only brought back
  `DEFAULT_CONFIG` keys + `peak_balance:tp2`) - each lane 2 restart quietly fell
  to legacy `risk_pct` and went back to risk sizing
  (0.06/leg instead of 0.04). `_load_config` now brings back all
  stored per-lane keys; `/api/lanes/status` also shows
  `effective_risk_pct`/`risk_base_pct`/`fixed_lots_per_leg`/`risk_preview`
  per lane (before only in the `/api/status` snapshot).

---

### 2026-09-02 - Lane 2 moves to the 10k demo (5055369549) with `tp2_protect`

- **New account on lane `tp2`**: the 2nd demo account went from 111495351
  (`nova_gestao`) to **5055369549 (10,000 EUR balance)** with the **same
  main strategy - `tp2_protect`** (stored `lane2_strategy`+`lane2_label` setup
  by `POST /api/config`; `MT5_2_LOGIN`/
  `MT5_2_PASSWORD` envs updated in `.env`). The lane 2 worker
  restarted to link the new account - `/api/diagnostics` and `/api/status`
  confirm login 5055369549, balance 10000.0, `margin_mode=2` (hedged),
  `trade_allowed=true` (AutoTrading ON) and lane `tp2` with
  `strategy_id=tp2_protect` (2 legs, exit at TP2 + protect). The lane A/B
  now compares the same strategy on different accounts.
- The account was added by the user to the portable terminal
  (`C:\MT5Portable2`) before the swap; the old account (111495351) stays
  on the terminal but lane no longer uses it.

---

### 2026-09-02 - Worker outages on /auditoria (tape + timeline) and bounded exception hold (530)

- **Replay tape holds worker-outage marks**: the
  `GET /api/operations/replay/tape` export carries `pending_delete_stuck` and
  `pending_order_result` (action=delete) marks as passive events (kind and
  payload whole) - replay skips them as watch, never turns them
  into orders.
- **Worker-outage timeline** (`GET /api/operations/replay/outages?days=30`,
  read-only, `worker_outage_timeline` in evidence.py): joins delete fails
  with watchdog ramp/re-alert marks, in time order,
  and groups **episodes by (trade, ticket)** - a pending order the bot
  tried to cancel and could not. Each episode shows fails, ramps,
  re-alerts, max tries, intent (TTL/management), lane/leg (by the
  mark ticket) and **journal-derived outcome** (taken delete or
  leg fill after the 1st fail; with no such it stays open, or outcome
  was not journaled). /auditoria got the "⏳ Worker outages ·
  pending-order delete" block (stats + timeline + episodes), on the
  60s page cycle.
- **Bounded hold of `operational_exceptions`**: per-episode rate cap
  already stopped hundreds of same rows, but new episodes
  kept growing - `db.purge_old_exceptions(365)` ("archive after 365 days"
  policy of maintenance.py) erases rows older than
  365 days on the watchdog purge (~6h) and at start; the table never grows
  with no cap.
- **Tests**: the tape exports marks with whole kind/payload; the
  report groups 2 episodes (solved by ok delete vs open), counts
  ramps/re-alerts/fails and credits lane/leg to fails; purge erases
  only old exceptions; frontend with the /auditoria block/renderer/fetch.

### 2026-09-02 - Pending watchdog stored in DB, rate-limited exceptions and full context on journal (527)

- **Watchdog state STORED** (new `pending_delete_states` table,
  lane+position_id PK): tries, delete intent (TTL/management with
  reason/msg_id) and alert stamps (`escalated_at`/`last_alert_utc`)
  live through a server restart. The engine brings state back at
  start (Diagnostics still shows stuck legs) and the brought-back
  intent finishes the management cancel with the ORIGINAL
  reason after reboot - never cut to TTL expiry. Orphan rows are wiped: per
  leg in `_check_pending_real` (no longer `pending_fill`) and per
  trade in `update()` (closed trade) - the table never piles junk.
- **Per-episode rate-limited exceptions**: while the stuck leg fails
  each second, only the FIRST fail of the episode writes
  `operational_exceptions` (context with `episode: true`; orders_get and
  order_delete - TTL and management cancel - share the per-leg
  marker) - one short exception with trace instead of hundreds of same
  rows. The marker resets when retry ends (good delete or
  leg no longer pending); a new episode writes again.
- **`pending_delete_stuck` with full context on journal**: each ramp/re-alert
  mark enters the tape (opt-in) with leg, position_id,
  ticket, lane, tries, intent kind/reason, `escalated`/`re_alert`
  and stamps - with the failed `pending_order_result` marks between, the
  replay replays a long worker fail on its own
  (ramp and 30 min pace plain on tape).
- **Diagnostics**: `stuck_pending_legs()` also shows `intent_kind` and
  `last_alert_utc`; the "⏳ Pending orders in delete retry" block shows
  the delete kind (TTL vs management) and last alert time.
- **Tests**: state back after restart (tries/ramped/
  TTL intent), re-alert on 1st post-reboot retry, recover with the original
  reason and DB with no orphan rows; rate cap (2 rows per 24 fails;
  new episode writes again); journal with full context + re-alert
  marks.

### 2026-09-02 - Pending watchdog with re-alert, at-once retry when worker returns and Diagnostics block (524)

- **Per-leg count with context**: `_pending_delete_failures` now
  stores leg/trade/symbol/ticket/open per leg (not only the count) -
  it fed Diagnostics with no extra reads.
- **Periodic re-alert**: `pending_delete_stuck` fires at the bar (5 fails)
  and fires again each `PENDING_DELETE_REALERT_EVERY` (30 min) while the
  leg stays stuck (a worker down for hours never goes mute); the
  clock is the engine one (`self.clock.monotonic()`), fixed in tests.
- **Auto-recover with no TTL wait**: the failed delete intent stays
  in `_pending_delete_intent` (ttl or cancel kind with reason/msg_id). The
  tick retries `order_delete` on EACH update while intent lasts -
  when the worker returns, the pending management cancel finishes on the
  same tick (`close` event with the original reason/message, `_maybe_close_trade`
  closes the trade) and TTL the same (reason `pending_expired`). With no intent and no
  out TTL nothing changes (normal flow stays same).
- **Diagnostics: "⏳ Pending orders in delete retry" block** -
  `GET /api/diagnostics` shows `stuck_pending.main/tp2` (tries,
  ramped?, ticket, age, trade; empty = nothing stuck) and the page shows it
  on the 10s cycle, red when ramped.
- **Tests**: periodic re-alert (one per episode and per gap),
  auto-recover of a management cancel in one tick with no TTL, `stuck_pending_legs()`
  with state/tries and drain after recover; `/api/diagnostics`
  checks the block shape.

### 2026-09-02 - Pending delete with per-leg retry, stuck watchdog and weekly backfill with evidence (521)

- **Per-leg `order_delete` handling on the expiry path**
  (`_check_pending_real` TTL + `_cancel_pending_pos`): a failed delete
  (lane worker fell) no longer closes the leg in DB - before `try/except:
  pass` closed the spot and left the pending order live on MT5 (orphan,
  could fill later and turn into a ghost spot). Now it writes the exception
  (`record_exception` with leg/lane/ticket), journals `pending_order_result`
  (delete, accepted=False, REMOTE_ERROR), keeps the leg `pending_fill` and the
  next tick repeats the cancel; the taken delete journals accepted=True.
  `_cancel_pending_pos` returns `None` with no close when delete fails
  (`close_trade_at_market` skips the missing event).
- **`pending_delete_stuck` watchdog**: after `PENDING_DELETE_ESCALATE_AFTER`
  (5) BACK-TO-BACK delete fails of the same leg, the tick returns the event
  to feed/Telegram (risk alert "⛔ Pending order not canceled on
  MT5...") - once per episode, never quiet; the counter resets
  when delete works again or the leg stops pending.
  Management cancels (`close_all`, etc.) share the same count.
- **Weekly auto backfill**: the tick got the `_check_refusals_backfill` guard
  (ISO week, `refusals_backfill_week_last_run` config) that re-runs the
  backfill once a week with `--apply` - no future phantom close
  stays with no lasting trace even if the alert of the time was missed;
  it notes only when it found something to write (missed-alert sign).
- **Evidence with no journal on backfill**: with no `execution_events`, the backfill
  now mines the hour risk/system notes (+-15 min) and the skipped
  audit - a `10027` sets `auto_trading_off=1` and builds the
  "AutoTrading off..." error (like the live engine), with the quoted note/skipped
  text in trace for the auditor.
- **Tests**: TTL/cancel delete with fail keeps the leg pending and retries;
  watchdog ramps once per episode and resets; backfill with empty journal
  mines notes/skipped and sets the 10027 flag.

### 2026-09-02 - Real retcodes on remote refusals + glossary on Diagnostics + A/B with trace (516)

- **Why 01/09 showed `'?'`**: the journal stored the 12
  `order_result` with `accepted=False` but `retcode=None` - the remote-send
  result of the time had no readable retcode (code of the time not
  recoverable; the worker-to-proxy path of today already carries it - proved by the
  roundtrip and the new tests).
- **Leg-by-leg send hardening** (`_send_open_orders`/`_send_pending_orders`):
  an `order_send` exception (worker fell mid-send) no longer unwinds
  `open_trade` - before it left the trade HALF-OPEN in DB; now the leg
  closes `order_rejected` with `REMOTE_ERROR` retcode and the real
  exception text enters `trade.error` (the reason is never lost). Unreadable
  retcodes stay `?` (never made up); real refusals (10027, ...)
  arrive as numbers.
- **01/09 regression tests**: remote 10027 refusal end-to-end (worker ->
  RemoteProvider -> MT5Engine) with trace stored in DB, and worker raising
  on send with no half-open trade.
- **A/B voids with trace**: each phantom close of the lane report now
  shows the lane void `*_error`/`*_retcodes`/`*_auto_trading_off` - the
  phantom why comes from the report itself (see /api/lanes/ab).
- **Retcode glossary on Diagnostics**: the ⛔ block explains each retcode
  (10027 AutoTrading OFF, 10014 funds, 10018 bad order, ...) and marks
  AutoTrading OFF red; unknown ones show raw. The A/B card
  shows main/tp2 phantom and points to the ⛔ block.

### 2026-09-02 - Refusal-trace backfill + "broker refusals" block on Diagnostics (514)

- **Backfill of the 3 phantom trades of 01/09** (`app/backfill_trade_refusals.py`,
  report/`--apply` shape of other backfills): with the
  `error`/`retcodes`/`auto_trading_off` columns now there, the refused tp2
  trades of the day (t-d1363896494e, t-dd05e88c0bd6, t-b92a129d2cdb - 4
  legs each) got lasting trace written from evidence:
  journal `order_result` events (accepted=False on all) + the
  hour risk notes -> `retcodes ['?','?','?','?']` (retcode was not
  readable on the worker at the time - the number is never made up) and
  `auto_trading_off=0`. The script re-runs any time (catches
  future phantom trades with no trace).
- **`GET /api/trades/refusals`** (`db.refused_trades`): latest broker refusals
  - trades with stored `error` OR phantom mark (closed with no
  `exit_reason` at 0.0), newest first, with retcodes/AutoTrading-OFF
  flag; has pre-column legacy rows (null error, seen the
  same). Read-only.
- **Diagnostics: "⛔ Latest broker refusals" block** - card with the
  latest 5 refusals (lane, time, symbol, retcodes, error), refreshed on
  the 10s cycle with no copies.

### 2026-09-02 - Phantom-close alert, lasting refusal trace and live A/B on the feed (513)

- **Own risk alert for phantom closes**: when a lane closes the
  trade at 0.0 with NO `exit_reason` (broker-refused orders on send,
  `sent==0` cleanup) `_try_execute` now sends its own alert
  ("👻 Lane X: signal Y with NO real fill...") with retcodes - before it fell in the
  generic alert with `retcodes ['?']` and only A/B (later) showed the
  oddity. AutoTrading OFF (10027) keeps the stored own alert.
- **Lasting trace of refusals**: `trades` got `error`,
  `retcodes` (JSON) and `auto_trading_off` columns (auto migrate at start);
  `_send_open_orders` stores them by `db.set_trade_refusal` - before they lived
  only in memory and audit/replay could not explain the close.
- **Diagnostics: A/B refreshing with the feed**: the "⚖️ Lane A/B" card
  now follows the 10s Diagnostics cycle (replaced, never
  copied) - the sample updates alone with no manual refresh.
- **Calendar/order flakes gone**: `test_observability` used
  relative days that crossed month end (monthly set split the
  XAUUSD samples at 01/09) -> stamps in the prior month; day 6 of
  `test_e2e_week` hung on stored `tp2` realized by other tests
  -> fixed `_realized_today`; the daily-offset test lot counted
  recount flips of other tests -> asserts scoped to the
  recount id. Suite: **513 collected · 512 passed + 1 skipped, 2 back-to-back
  green runs with no deselect**.

### 2026-09-01 - Paper run focused on strategies in check (509)

- **Normal run cut**: after the 6 loser strategies
  left, the paper run now opens **only what is in check** - the 5
  with a menu (`nova_gestao`, `nova_gestao_fallback`, `tp2_protect`,
  `auto_escada_3`, `auto_escada_3_hibrida`) - plus `nova`, kept
  **quietly** as the `counterfactual_pnl` counterfactual (opens
  paper but shows on neither run cards nor feed). Backtest refs
  `auto_3`/`auto_5`/`auto_escada_5` no longer open normal paper
  - they stay on dropdown, backtest/compare and **shadow**
  (`auto_escada_5` keeps forward-gate evidence by
  `shadow_paper_strategies`, with apart `origin=shadow` markers).
  `paper_strategy_ids()` (in `app/strategies.py`) is the single source of the
  run; the feed only counts visible ones (`strategy_lifecycle.menu`).
- **`protect_latency` guard**: the endpoint only checked the id with
  `engine=backtest`; now `get_strategy()` always runs - a removed id
  returns 400 instead of an empty 200 (loud-study rule).
- **Tests**: new `test_removed_strategy_studies_fail_loudly` (studies
  with removed ids return 400/ValueError, never run on main);
  common paper-test helper filters `origin="race"` (shadow
  shares the `paper_trades` table - with no filter, an `auto_escada_5` shadow trade
  tainted asserts). Suite: **509 collected**
  (507 passed + 1 skipped + 1 deselected - the flaky week e2e).

---

### 2026-09-01 - Removed strategies: erased from registry (full removal) (508)

- **User call**: `tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`,
  `auto_protect_5` and `auto_escada_3_struggle` (R4) were **erased from the
  registry** (`app/strategies.py`) - they no longer show on `/api/strategies`,
  menu, HELP, test pages, paper or shadow.
- **What is no longer reproducible (filed)**: history backtest/compare
  of these 6 variants (test pages and studies using them now fail with ValueError; `sweep_auto`/`sweep_atr`
  no longer list `auto_2`). The decision log (PF/DD and why of
  each removal - 19/08 and 20/08) stays in CHANGELOG and the
  history chapters of ESTRATEGIAS.md.
- **Guards**: `get_strategy()` raises ValueError for unknown ids
  (no study quietly runs on the main strategy); the
  `/api/analysis`, `/month`, `/trace`, `/resolution`,
  `/protect_latency`, `/protect_causal` endpoints return 400 for removed ids.
  The `/api/analysis/trace` default moved from `tp2` to `nova` (the
  "Exit at TP2 (pure)" arm of the test-page month chart is gone).
- **Frontend**: `common.js` (STRATEGY_NAMES/RACE_COLORS) with no dead ids +
  new colors for `nova_gestao_fallback` and kept live ones; HELP with no
  `auto_protect_5` entry/"Early protect" glossary.
- **Docs**: AGENTS.md/TECHNICAL.md/PUBLIC.md/README.md/ESTRATEGIAS.md
  updated for the real menu (7 visible - 5 run + auto_3/auto_escada_5 refs)
  and the removed list; orphan paper
  data (e.g. 13,725 `auto_protect_5` balances) stays in
  DB with no readers (data is never erased). Generic paths
  (`protect_trigger`/`struggle_*`) stay in engines for future
  candidates. Updated tests (menu/registry/paper/HELP/frontend): 508
  collected.

### 2026-09-01 - WhatsApp: push retry + bridge waits for socket + `re` bug on tick (508)

- **Day diagnosis**: 3 real trades closed today (08:41 +4.04 EUR, 10:49 +4.63 EUR,
  12:40 +19.74 EUR) and the WhatsApp push was tried on all; only the 12:40 one went through
  (`sent_count` 1 on the bridge). 08:41/10:49 failed with **500/503** - the bridge was
  in a **lasting reconnect loop** (`conflict replaced` session clash ~1000x/h
  since the 23:57 start, ~1-3s connect windows) and `send()` had
  no retry -> messages lost quietly (only logged in `operational_exceptions`).
- **Server fix** (`app/notify/whatsapp.py`): `send()` makes **3 tries with short
  backoff (2s/4s)** - a passing 5xx/link fail no longer loses the message; the
  exception only logs to DB at the end (1 row, not 3). New
  `test_send_retries_transient_bridge_errors` test.
- **Bridge fix** (`whatsapp/bridge.js`): `/send` **waits up to 8s** for the socket to
  turn `open` (before it returned an instant 503 in the reconnect window) and makes 1 inner
  retry when the socket dies mid-send (`Connection Closed`).
- **Tick bug fix** (`app/lanes.py`): missing `import re` -> `NameError` in
  `_mt5_positions` (93 stored hits today) that broke the end of **each**
  tick (snapshot broadcast never happened by WS).
- **Manual resend (user asked)**: the 3 messages of today were resent
  to the group in production shape (`format_trade_close`) - through at 1st try
  after the bridge restart. ⚠️ With the 12:40 one (+19.74 EUR): the bridge had
  logged that send as `sent` (a false positive - the socket died mid
  send), but the user confirmed it NEVER hit the group; resent and
  confirmed (`sent_count` 3 on the bridge). Lesson: a Baileys `sendMessage` ack does not
  prove send when the session is in a reconnect loop - the lasting fix is
  to re-pair the session (wipe `whatsapp/session/` + new QR).
- **State**: server restarted with new code (main 5054644473 · 1032.30 EUR,
  lane 2 worker on, AutoTrading ON); bridge steady after restart (0 clashes
  since 20:04 UTC). If the loop returns, re-pair: wipe `whatsapp/session/` + new QR.

- **Portable-terminal AutoTrading TURNED ON on 01/09** - auto click
  on the last Standard-toolbar button ("Algo Trading") by `GetWindowRect`+
  real mouse click; checked by `terminal_info().trade_allowed = True`
  through the worker (`/mt5/call`). Still **per-session** (re-arm after
  reboot - like terminal 1).
- **Worker `/status` shows `trade_allowed`** (by `terminal_info`) and
  `RemoteProvider` mirrors it (`trade_allowed` + `at_off` in
  `status_summary`) - **Diagnostics shows ON/OFF** on the "Lane 2 ·
  MT5 worker" block and market_loop auto-alerts `risk` when the worker
  is on but AutoTrading is OFF (10 min cooldown,
  `lane2_auto_trading_off` event; `lane2_auto_trading_on` when back).
- **Lane A/B (read-only)**: `GET /api/lanes/ab` (`lane_ab_report` in
  `app/lanes.py`) compares closed trades of the same signal on both lanes
  (side-by-side P&L, win rate, profit factor, total delta) and **only hints
  keep with >=20 comparable signals** (`min_comparable`); under that the
  verdict is "thin sample" (neutral). "⚖️ Lane A/B" block on
  Diagnostics (fetch once per page, refresh on Update button). Never
  changes setup nor sends orders.
- **Phantom closes out of A/B (02/09)**: `lane_ab_report` drops from the
  comparable set trades closed **with no `exit_reason`** (refused-order
  cleanup writes 0.0 with no reason - e.g. AutoTrading OFF) and shows them in
  `voids`/`void_count` with per-lane flags (`main_void`/`tp2_void`); with
  n=0 and voids, the verdict explains the sample instead of counting 0.0 as
  output. With the real 02/09 sample (the 3 tp2 trades of 01/09 were
  phantom - terminal 2 AutoTrading only came on that night), the
  **keep bar stays at 20** - with no real fills there is no base
  to cut it; Diagnostics shows "⚠️ N phantom close(s) with no real
  fills". Diagnostics and test: the real sample explains n=0 with no show
  as loss/gain of any strategy.
- **Flaky gone**: `test_e2e_week_autonomous_no_intervention` backdated
  the day-7 signal to 2026-08-01 (fixed) - when the calendar passed the
  30-day span of the reject report, the signal left
  `by_id` (KeyError). Now backdates 2 days back (relative); the suite runs
  **100% green with no deselect** (511 tests).
- Tests: `trade_allowed` on the worker (ON and OFF), mirror on RemoteProvider
  (with `at_off` and dead broker), 3 A/B scenes (sample <20 with no
  hint; 20+ with tp2 ahead -> hint; 20+ with no gap -> neutral)
  + 2 new: phantom dropped from comparable and phantom-only explaining the
  empty sample.

### 2026-09-01 - Lane 2 real start (worker + portable terminal) (501)

- Set up the **2nd portable MetaTrader 5 terminal** at `C:\MT5Portable2`
  (clone of the stored install; its own `terminal64.exe` and split data
  folder `991DCC…` - the API calls it with `path=…` + `portable=True`). The current
  official setup (`download.terminal.free/cdn/web/metaquotes.ltd/…`)
  skips `/portable` (GUI) - the backed path is the install copy.
- `.env` (backup `.env.bak-20260901`): `MT5_2_TERMINAL_PATH=C:\MT5Portable2\
  terminal64.exe`, `LANE2_WORKER_ENABLED=1`, `MT5_2_DRY_RUN=0`,
  `LANE2_WORKER_PORT=8770`. Checked: the API links the clone and logs in to
  **111495351 · 100,000 EUR · margin_mode 2 (hedged)** with no touch of the main
  account (5054644473 confirmed whole).
- **Production**: worker up (8770) and server restarted with the new `.env`
  -> `/api/status` shows `tp2 = nova_gestao · mt5-real · 111495351` linked
  to the broker through `RemoteProvider`; `main` whole. A/B of the two
  strategies now runs with real broker fills on both demo accounts.
- ⚠️ **Manual step left**: turn on **AutoTrading (Algo Trading)** on the
  2nd portable MetaTrader 5 window. It is per-session (re-arm after reboot, like
  terminal 1); with the button OFF lane 2 orders refuse with retcode
  10027 + alert (fail-closed, nothing changes on accounts).

### 2026-09-01 - Apart lane 2 worker (real run of the 2nd MT5 account) (500)

- **`app/lane2_worker.py`** - split process owning the MetaTrader5 link
  to the 2nd account (portable terminal, `MT5_2_TERMINAL_PATH`), I/O shim by
  local HTTP (`/ping /status /initialize /account /positions /quote /meta
  /copy_rates /constants /symbol_select /mt5/call` + `/mt5/call` allowlist
  for the raw MT5 module), token (`LANE2_WORKER_TOKEN`), rotating logs
  (`configure_process_stdio("lane2_worker")`). **Never touches SQLite and
  calls nothing** - the "all state lives in the event loop" rule stays.
- **`app/execution/remote2.py`** - `RemoteProvider` with the same duck-typed
  face as `MT5Provider` (with `provider._mt5`, the MT5-module proxy
  with constants + JSON-set calls) linked by the lane 2 `MT5Engine`
  with no engine change. `main.py`: with `LANE2_WORKER_ENABLED=1` lane
  2 uses the remote provider (`dry_run=False`); with no flag it keeps the old
  dry-run. Only on with the env - production stays dry-run until the user
  sets up the 2nd portable terminal.
- **Watch**: `start.ps1`/`boot.ps1` start the worker idempotent by
  port (they guard `MT5_2_TERMINAL_PATH`); `healthcheck.py` got the lane-2
  target (`/ping`, 2 fails + cooldown, `worker_*` keys) and restarts it;
  market_loop pings it ~1x/s and notes online/offline flips on
  feed/Telegram (`lane2_worker_offline`/`online`); **Diagnostics** has the
  "Lane 2 · MT5 worker" block (reachable/connected/login/last_sync/last
  error/log tail + off state in dry-run).
- ⚠️ **Event fixed with a hard guard**: during checks, the worker
  with no `MT5_2_TERMINAL_PATH` ran `initialize(login=111495351)` on the default
  terminal and **swapped the main-terminal account** (production pointed at
  111495351; logged back to 5054644473/1003.89 EUR). The worker
  now **refuses to start (exit 2)** with no stored `MT5_2_TERMINAL_PATH`
  - regression test `test_worker_recusa_arrancar_sem_terminal_portable`.
- **Tests**: `tests/test_lane2_worker.py` (9) - in-memory HTTP worker with
  fake provider (auth, initialize/status/account, constants, allowlist,
  JSON-safe), `RemoteProvider` roundtrip + `_mt5` proxy (constants,
  `order_send`/`positions_get` with attr retcodes), fail-closed with a
  dead worker (real `open_trade` refuses) and the path guard. Suite **500**
  (492 -> +8).

### 2026-09-01 - 2nd MT5 account (111495351) moves to nova_gestao (492)

- Lane `tp2` (demo account **111495351**, `MT5_2_*` env) moved from
  `tp2_protect` to **`nova_gestao`** (TP1..TP5 + management) by user
  call - stored setup (`lane2_strategy`/`lane2_label` by
  `POST /api/config`, checked vs the registry; `auto_escada_3/5` stay
  blocked on the MT5 lane by the Phase 3 gate). Main stays at
  `tp2_protect` -> the two lanes now A/B the same signal.
- Lane 2 state: **dry-run** (`mt5-dry`) - the MetaTrader5 API keeps one
  global link per process and real run of the second account needs an
  **apart worker** (second Python process with portable terminal by
  `MT5_2_TERMINAL_PATH`); with no such worker, lane 2 stays fail-closed
  (`execution_block_reason` at start).

### 2026-09-01 - Top price strip removed (perf) (492)

- The **price strip** (GOLD..NZDUSD on topbar row 2) is fully gone
  by user ask: no DOM row, no `renderPriceStrip()`/
  `TICKER_LABEL`/`_tickPrev` in `common.js` and no `.price-chip`/
  `.pchg`/`.pbar`/`.tb-row2` CSS (it waited for a rebuild on each price move).
  The topbar stays static (only clock/badges/real flashes change) and
  header height drops ~20px - `--hdr-h` is already live-measured.
- `market.prices`/`changes` flags stay in the snapshot (the "real
  prices"/"simulation" market badge uses `real_data`); no endpoint or
  engine was touched - view only. Contract test updated:
  `test_render_guards_previnem_rebuilds_sem_mudanca` makes sure the strip
  does NOT come back and the other guards (balance flash, feed, active tab)
  stay.

### 2026-09-01 - Full dark theme (stored toggle) + topbar tune (492)

- **Dark theme** as a `[data-theme=dark]` layer over `style.css` vars
  (dark surfaces/tones, dark-fit meaning, native controls get
  `color-scheme: dark`). No hardcoded hex outside
  vars - table striping (`--stripe`) is now a var
  so it does not stay white in dark.
- **☾/☀ toggle on the top bar** (shared by all pages, in
  `common.js`): flips the attr on `<html>`, stores in
  `localStorage.nx_theme` and syncs icon/aria-pressed. The theme
  applies **before paint** by a snippet in each page `<head>`
  (no flash), defaulting to `prefers-color-scheme` when the user
  has not picked yet.
- **Theme-aware canvas charts**: `chartPalette()` in `common.js` gives
  grid/axis/back per theme; all draw funcs (equity, paper
  run, daily, recounts, trace, walk-forward, uptime, fills-div) use it -
  series colors (bright green/red/blue) are same on both.
- **Sturdier topbar**: the badge block shrinks with `overflow:hidden`
  (never breaks layout with long lane badges) and `online-users` drops at the
  1440px breakpoint - checked with no flat overflow at 1440px.
- **Login** also honors the theme (snippet + inline overrides). WhatsApp
  QR keeps a white back (scan readability).
- **Follows the OS live**: with no explicit pick (no `nx_theme` in storage),
  the theme tracks the system `prefers-color-scheme` in real time
  (`matchMedia` `change` hearer). A button pick stays stored and
  the OS no longer steps in; chrome start does NOT write the theme (writing
  would kill follow).
- New contract in `tests/test_frontend.py` (toggle + dark layer + palette
  on charts + head snippet on all pages + OS follow with explicit-pick
  guard). Suite: **492** (stored e2e fail same).

### 2026-09-01 - Health check on Diagnostics, recovery-flow test and render guards (491)

- **NexGen Health on /diagnostico**: `/api/diagnostics` now shows
  `healthcheck` (the `healthcheck.state` file state, log tail and the
  Scheduled Task by `schtasks`, 60s-cached because the page polls
  each 10s). New block on the Diagnostics tab: last task run/result,
  server online/offline by the check, back-to-back fails,
  last auto restart and log tail - next to MT5/listener.
  Tolerant of missing (freshly started server, failing schtasks).
- **Recovery-flow test** (`tests/test_healthcheck.py`, 6 tests): runs the
  real `healthcheck.py` logic vs an apart port with NO server and
  only controls restart by monkeypatch - 1 fail does nothing, 2 back-to-back
  fails alert+restart, cooldown between restarts, recover clears
  state and notes, `--no-restart` never restarts, a port-holding stuck process
  is killed before the new one rises.
- **Audited and covered render guards**: the price strip
  rebuilt (`innerHTML=`) on EACH snapshot even with flat prices -
  now it has a signature guard (`contentChanged("pricestrip", …)`) like
  tables; the balance flash was already conditional and stayed checked (only
  when balance moves, never on 1st read). New contract in
  `test_frontend.py::test_render_guards_previnem_rebuilds_sem_mudanca`.
- Suite: 491 passes (6 new + 1 new contract; the stored e2e fail
  stays).

---

### 2026-09-01 - Desktop 1400px+ QA and server health check (NexGen Health)

- **Wide-desktop visual QA** (1440px) on all pages with
  agent-browser: fixed flat overflow of the top bar (price
  strip/badges spilled ~30px - the brand sub-title now drops <=1440px);
  accent palette darkened for AA contrast (badges, tags, prices);
  HTML landmarks (`<main>`/`<footer>`/h1) on all pages; `aria-label`
  on the group filter and price strip; empty table header with
  sr-only text. axe-core: 0 fails (4 "incomplete" are decorative
  gradient icons, not text).
- **`healthcheck.py` + `NexGen Health` Scheduled Task** (each 2 min):
  checks `GET /` on port 8000 (the `/api/status` is heavy and can take
  >5s) with a 15s timeout and **2 back-to-back fails** before acting - a
  healthy but slow server is never wrongly restarted. When offline:
  alert on the rotating log (`healthcheck.log`) + Telegram (if set) and
  **server restart** with the filed command (`server_runner`,
  `MT5_DRY_RUN=0`), first killing a port-holding stuck
  process; 120s cooldown between restarts (`healthcheck.state`). Modes:
  `--once` (task), `--watch` (loop), `--no-restart` (alert only).

---

### 2026-09-01 - Clean pro redesign of the dashboard + render tuning (front)

- **Clean pro light theme** (trading-app style): the neon cyberpunk theme is
  fully gone (neon colors, glows, scanlines, Orbitron/Chakra
  Petch fonts, neon favicon) and replaced by a light design system: back
  `#f6f8fb`, white panels, soft borders, TradingView green/red
  (`#089981`/`#f23645`), `#2962ff` accent blue, tabular numbers. The
  canvas charts (equity, run, uptime, recounts) were re-toned
  for the light back.
- **TOP nav bar**: the two fixed side columns (left menu
  + STATE terminal right) were replaced by one single top
  bar (brand + MAIN/STRATEGIES/DIAGNOSTICS/
  AUDIT/HELP nav + badges + clock + kill switch + exit) with a price
  strip under - content gains full width (`--term-w`/`--term-r`
  and ☰/▦ mobile overlays of the columns are gone; ☰ opens
  dropdown nav <=960px). Main content order was
  reshaped: status -> KPIs -> tabs -> content, with run panels
  (run/promotion/Phase 2/3/story/shadow) after content.
- **Fewer moves**: the dashboard and login boot sequence (overlay
  with typed lines + Web Audio sounds) is gone - the dashboard shows
  at once; note sounds gone; glitch/flicker/pulse/
  scanlines/backdrop-filter/hover-shift/glows all gone. Only
  working micro-moves stay (balance color flash, tab underline,
  short toast fade) and the `prefers-reduced-motion` block as net.
- **Render tuning (the real perf gain)**: the snapshot comes
  ~1x/s and the page rebuilt ~20 DOM parts per snapshot. Now: only the
  active tab draws (`renderSignals`/`renderPositions`/`renderTrades`),
  each renderer only rebuilds when content moved (signature cache
  `contentChanged` in common.js), the feed rebuilt each tick and now
  only on change (and WS `prependFeed` stays stepwise), and the
  panels (promotion/Phase 2/3/shadow/audit) have their own guards. Hidden
  tabs keep DOM and re-draw on return. On the server, the
  snapshot now sends 30 notes instead of 50.
- **Pro polish**: decorative emojis off the chrome
  (tabs, KPI labels, titles, buttons, badges); kept only in
  feed content and meaning states. Login redrawn (clean card,
  no glitch/boot/scanlines). Favicons moved to blue N
  on light.
- Suite: 484 passed (front - no backend behavior change
  besides the snapshot note cap).

---

### 2026-08-28 - Stored 'solved' count on /auditoria, cap-decision reminder and full-history weekly batch (470)

- **Stored 'solved' block on /auditoria**: the recount batch re-check now
  writes each run to `recount_batch_runs` (new table,
  read-only) with the 'solved' count + flip list (date, recount,
  from -> to, offset). The page shows last-batch stats (daily/weekly
  window, re-run, changed, solved, errors) and flip history
  per run to see how GC=F-to-spot offset fixes move
  over time. New `GET /api/recounts/batches` route.
- **Feed reminder to call the cap**: while `entry_guard_auto_cap`
  has been on for more than `cap_reminder_days` (default 7) and no
  real post-cap fills exist yet, the tick (`_check_cap_reminder`) sends once
  a day a note asking to run `study_cap_effect --days
  30` and call if the cap stays/tightens/reverts (per-day guard - the microsecond
  was the dedup bug).
- **Weekly batch over FULL history**: besides daily (fresh
  window), `_check_recount_batch` runs once a week (`days=0`,
  ISO-week guard) so old verdicts all get fixed
  with the measured offset. `/api/recounts/recheck-all` takes `days=0`
  (full history) on weekly runs.
- **Bonus (test)**: `test_confirm_never_bypasses_global_risk_switches`
  wrote the daily-loss close with the UTC date - the local clock (+1h)
  is one day ahead of UTC, so `realized_today` (which uses LOCAL
  `date.today()`) missed the trade and confirm ran instead of blocking. Fix:
  use the local day in the trade `closed_at`. Suite: 470 passed.

---

### 2026-08-27 - Per-recount offset on /auditoria, cap_applied_at + study call, and daily batch with solved flips (467)

- **Applied offset seen per recount on /auditoria**: the
  candle-check column now shows an "offset N pips" badge (the daily
  `futures_shift` the check used - 0 with no samples) and the tooltip holds the value -
  you see live which fix gave each verdict.
- **`cap_applied_at` stored**: changing `entry_guard_auto_cap` by
  /api/config logs the EXACT time (config, stays in DB); the
  `study_cap_effect` uses it alone as `--since` (before it took
  today 00:00). The study got the pure `decide_after()` func: no post-cap
  fills -> "no data"; cap cutting losers -> KEEP (saves); cap
  cutting winners -> REVERT/loosen (costs) - the end call only
  with real post-cap data.
- **Daily batch with SOLVED flips**: `recheck_all_recounts` now counts
  `resolved` (not-plausible/feed_divergence -> plausible verdict, which
  `changed` missed - `plausible` was already True on feed split) and
  `_check_recount_batch` notes on the feed when `resolved > 0`: "N
  solved (were not-plausible/feed and now plausible - the daily
  GC=F offset showed they were feed)".
- **Tests**: longer `test_recount_candle_check_daily_futures_offset`
  with the batch (writes feed_divergence -> fill -> batch -> `resolved >= 1` and the
  stored verdict turns plausible with offset 50); new
  `test_cap_applied_at_recorded` (writes/re-writes/stays) and the call in
  `test_cap_effect_passes_guard` (decide_after: no data / KEEP / REVERT).
  Suite: 467 passed.

---

### 2026-08-27 - Daily GC=F-to-spot offset on recount candle-check, cap-effect study and split mini-chart (467)

- **GC=F candle-check with DAILY OFFSET** (`state._day_futures_offset`):
  instead of a fixed offset, the GC=F check now uses the MEASURED GC=F -
  spot mean on same-day real fills (`market_price_at_open` of the guard vs
  candle close at signal time; 5 min cache per day+symbol; 0 with no
  samples). The futures premium moves day to day (53-60 pips on the real DB) and
  a fixed offset made false not-plausible ones (66% baseline on GC=F). The
  check shows `futures_shift` per recount. Test: a spot-plausible claim (4351 reverse vs 4340 low) goes not-plausible on raw GC=F
  (4390 low) and back to plausible with the +50 day offset. With no fills of that
  day, the offset falls to the last-30-day mean of the symbol (measured, never
  fixed); with no samples at all, 0. Live: the 08-19 recount went from
  `feed_divergence` to `plausible` (55.95 shift) on batch re-check - the
  GC=F now agrees with spot.
- **`app/study_cap_effect.py` (read-only)**: compares real fills before/after
  the cap time (`--since`) and applies the real guard rule as
  counterfactual (passes if <= tolerance or <= width x cap_widths). Verdict
  on the real DB: the 0.5x cap would have cut 1 fill (937c65, 3.64 pips, **+8.80 EUR**)
  - the safety lid costs money on history (cuts a winner);
  the other 9/10 still enter; 0 cap rejections so far.
  Re-run `--since` after some days for the real before/after
  compare. Report at `reports/cap_effect_20260827.json`.
- **🛰️ mini-chart on /auditoria**: `fillsDivChart` canvas with false
  chases (bars) vs mean GC=F-spot offset (line) per day - the
  feed-split trend with no tables open.
- **Tests**: `test_recount_candle_check_daily_futures_offset` (0 offset ->
  feed_divergence; with same-day fill -> +50 -> plausible, no feed_divergence)
  and `test_cap_effect_passes_guard` (real guard rule). Suite: 467 passed.

---

### 2026-08-27 - GC=F-to-spot split measured on /auditoria, cap live and cap-reject note (464)

- **🛰️ "GC=F-to-spot split per day" panel on /auditoria** (inside the
  🏃 block): for each fill with both sources, `feed_divergence` =
  GC=F price (Yahoo candle) - spot price (MT5 guard) at signal time;
  sets per day (mean/max offset) and counts **false chases**
  (in zone on spot - binding - but out on GC=F) vs **real** ones
  (out on both). On the real DB: mean offset **+55.95 pips** (gold GC=F
  trades ~56 pips over spot), **5/10 fills were false
  chases** - the "8 out up to 65 pips" of the report were the futures
  feed, not risky entries. Fill tables (dash, test and
  /auditoria) now show the **spot** price with GC=F + offset as
  per-row context.
- **`entry_guard_auto_cap` live**: 0.0 -> **0.5** (2.0 pips is about
  0.5x zone width - `study_persecution_cap` advice). With
  median width 4.0, the chase lid drops from 20 pips (5x width)
  to ~2.0 pips: fresh fills (0.87-1.27 pips) still enter; the
  tail (3.64 pips, +8.80 P&L) is now rejected - filed as
  a safety lid, not a fix (the 65 pip read was feed).
- **Standalone cap-reject note**: the text now shows the
  real gap and cap value - "⛔ XAUUSD signal rejected by standalone
  cap: price 36.00 pips outside zone (cap 20.00 pips · 0.50x width)
  - chase past the cap, not run."
- **Tests**: longer `test_autonomous_zone_deviation_rejects_beyond_cap`
  (note text with gap+cap) and
  `test_rejection_report_zone_and_orphans` (feed_divergence, offset, false
  vs real chases, per-fill `market_price`). Suite: 464 passed.

---

### 2026-08-27 - Chases on /auditoria, honest cap and off-zone-fill note (464)

- **`fills_zone` now measures with the BINDING source (spot)**: each
  real-fill gap now comes from the guard (`trade.entry_distance` - MT5 spot at
  run time), falling back to Yahoo candles marked per fill
  (`distance_source` guard/candles). On the real DB the 10 fills of the last 30
  days had 0-3.64 pips of gap; the "8 out up to 65 pips" the report
  showed on candles were GC=F-to-spot SPLIT (filed in AGENTS.md),
  not real chases.
- **`fills_zone.by_day`** - per-day (UTC) place history: total /
  in zone / chases / max gap, to see WHEN far-market entries
  happen.
- **🏃 "Far-from-zone entries · chases" block on /auditoria** -
  stats + per-day table + per-trade detail (spot/candle source seen),
  loaded in `loadAudit()` (uses the same reject-report endpoint).
- **`app/study_persecution_cap.py` (read-only)** - uses the REAL
  spread of fill gaps (spot guard) to advise an
  honest `entry_guard_auto_cap`: quantiles, candidate-cap table with the
  P&L each cap would cut (cutting winners is a safety lid,
  not a fix), advice = smallest cap that keeps >= 90% of fills,
  also turned into x zone width. Verdict on the real DB: no
  real chases (max 3.64 pips) - 2.0 pip cap is about 0.5x width as
  lid, keeping default is just as sound; report at
  `reports/persecution_cap_20260827.json`.
- **Ran-chase note**: when a zone pending order FILLS with price outside zone past auto tolerance, the
  tick sends "🏃 Ran chase" with the real pip gap
  (spot guard) - one note per trade (sets legs of the same
  tick), so the operator sees risky entries live.
- **Tests**: `test_pending_fill_persecution_notifies_feed` (off-zone fill ->
  note with gap), `test_persecution_cap_recommendation`
  (pure `recommend()` func: no chases -> keep default; with tail
  -> cuts but keeps >= 90%), and `by_day`/`distance_source` in the
  zone/orphan test. Suite: 464 passed.

---

### 2026-08-26 - Set orphan note, zone-vs-price on real fills and filters on "why no trade" (462)

- **Feed/Telegram note when the report spots orphan pendings**:
  the tick got `_check_orphan_report` - each ~1h (guard
  `orphan_report_last_check`, each leg only notes once) it sets per-category
  pendings closed since last run (orphan vs canceled
  vs out) and sends ONE "🧨 'Why no trade'" note - no need to
  open the report to know which zone orders did not enter.
  The guard only moves after the read runs well (fail = retries, never
  loses the note).
- **Zone-vs-price on real trade history (fills)**: new
  `fills_zone` part on the report - for each real trade (mt5-real/mt5-dry) opened
  in window, it compares signal zone with price at the time and sets how many
  fills went INSIDE zone vs how many were chases outside it
  (count, in-zone %, max gap + per-trade table with P&L). Pendings
  that never filled (pending_*) stay out of fills - they are
  the 🧨 orphans. New `db.trades_opened_since` helper.
- **Filters on the "why no trade" block**: symbol picker + zone-gap
  range (min/max in pips) - quickly split the reject
  shapes that cost most trades; also on orphans (per symbol) and
  with a "showing N of M" count. On dashboard and test
  pages.
- Fixed a stored JS syntax bug (extra brace at the end of
  `renderRejectionOrphans`/`renderTesteRejectionOrphans`) that broke the
  whole page script - static files had never been checked with
  `node --check`.
- New tests: `test_orphan_report_notifies_aggregated` (set + guard)
  and `fills_zone` asserts in the zone/orphan test. Suite: 462 passed.


- **Signal zone vs real price when it came**: each rejected signal on the
  report now shows `zone_vs_price` - the price on the candle covering
  message time, the entry zone, if price was in zone and how many
  pips missed (`distance`). Dashboard and test pages show a
  "Zone vs real price" column (in zone / out with gap), to see
  at once how far entry stood from market at the time.
- **Orphan/out pendings on the report**: besides rejected signals, the
  "why no trade" joins zone pending orders closed
  with `pending_expired`/`pending_orphan`/`pending_cancelled` (DB
  `closed_pending_positions`, joined to trades for the zone). For each orphan
  leg the report explains **why price did not come back to entry level**:
  price range over order life, if it touched entry and how much
  missed - 🧨 sub-block in the report block.
- New `test_rejection_report_zone_and_orphans` test and `zone_vs_price`
  asserts in the category test. Read-only: the report never opens
  trades nor writes to DB.

- **"Why no trade" block on test pages**: `teste.html` now
  shows the reject report with replay using the picked
  strategy params (`GET /api/analysis/rejections?strategy_id=<id>` -
  max_legs/management change what replay replays).
- **Orphan pending notes on the feed**: when a zone pending order
  outs (TTL), is canceled by broker or is orphan (missing ticket),
  the tick sends a feed note with text telling the three apart -
  before it stayed only in DB and Diagnostics.
- **Group chat on the report**: skipped messages (🚫
  Ignored tab) enter the same reject report as an
  `ignored` category - the "why no trade" tells "I rejected" from "never tried"
  (pure chat never enters the pipeline).

### 2026-08-26 - "Why no trade" (Phase 2), data-driven guard tune (Phase 3) and orphan-pending reconcile + 7-day E2E (Phase 4) (458)

- **Phase 2 - "why no trade" block**: `GET /api/analysis/rejections`
  counts rejected/failed signals of the last N days per category
  (`zone`, `min_lot`, `global_risk`, `pending_expired`, `broker`, `parse`,
  `llm`, `execution_failed`, `outro`) and, for each parsable signal, runs a
  **would-be replay** with real candles + linked management (same
  `run_analysis` pipeline) - what WOULD have happened. Read-only: never
  opens trades nor writes to DB. The dashboard (Report tab) shows the
  counts and per-signal replay. `rejection_kind` now classes
  per-lane risk rejects (e.g. "no lane opened the trade: ... daily
  loss ...") as `global_risk` instead of `execution_failed`.
- **Phase 3 - tune sweep (`app/study_guard_calibration.py`)**: read-only
  grid (cap 0-3x width x tolerance 0.25-1.0 x risk 2-5%) over real
  last-30-day signals with MT5 spot candles (GC=F fallback),
  same backtest pipeline (daily kill set). Honest verdict:
  loser span (PF<1 on all cells) - **KEEP defaults**, no
  tune; JSON report in `reports/`.
- **Phase 4 - orphan pending reconcile**: `reconcile_live_state`
  compares `pending_fill` BD legs with broker `orders_get` - an order
  with a ticket no longer on the broker = orphan (out/canceled
  outside or restart before send), no ticket = at-once orphan; the
  diagnostics shows `pending.ok`/`pending.orphans` (read-only, TTL closes
  orphans on tick).
- **Phase 4 - 7-day no-touch E2E**: `test_e2e_week_autonomous_no_intervention`
  walks a week of standalone run (in-zone market, pending that
  fills, past-cap reject, min-lot auto-fit, pending TTL,
  daily loss, out orphan pending) and makes sure NO signal stays
  `pending`/`awaiting_edit` at the end - each reject explained on the report
  with count + replay. Real bug fixed on the way: `_try_execute` did not
  write `signal.error` on the in-memory object on the `execution_failed` branch
  (DB had the reason, the answer did not).

### 2026-08-26 - Item 12 shield: LLM never runs by GO/revive, confirm honors global switches, post-run edit metric (442)

- **GO hole closed**: the group `go` handler ran any
  `awaiting_edit` signal with no error - and an `llm_parsed` signal has no error.
  GO now skips AI-parsed signals, and `_try_execute` got a
  **choke point**: any path (GO, revive, finalize, confirm) that
  gets there with `llm_parsed=True` logs + alerts and never opens a trade.
- **LLM revive**: a failed signal the group fills only with AI parse
  no longer reopens the window - it stays `failed`/audit with the alert (log
  only, never runs).
- **Confirm honors global switches (regression)**: the
  `/api/signals/{id}/confirm` route only skips zone/lot guards; kill
  switch, daily loss and max positions run again per lane in
  `_try_execute` (checked + 3 new tests: kill switch, daily loss,
  max_positions).
- **Ongoing post-run edit metric**: edits to signals that
  ran with no window now write `edited_at`; `db.post_execution_edit_metric`
  shows `post_execution_edited/total/rate/pct` on `/api/edits` and in the snapshot
  (`edit_metric`); the tick checks each ~30 min and, with sample >=20 and rate
  >5%, fires a `risk` alert (feed + Telegram, 24h cooldown) - the
  "complete -> runs now" rule of Item 12 was set on a real 0.0%
  rate and must be reviewed if group habits move. The
  Diagnostics page shows the value vs the 5% bar.
- Suite: **442 green tests**.

### 2026-08-26 - Item 12: manual mode cut; edit window checked on real data (438)

- **Manual-mode approval removed**: `config.mode` is gone - the pipeline
  (`app/state.py`), setup registry (`app/config.py`), model
  (`app/models.py`) and risk engine now have **only the auto path**.
  The snapshot still shows `mode: "auto"` so the dashboard does not break.
- **Approve/Reject buttons and `POST /api/signals/{id}/approve` cut** from
  UI and backend; route tests confirm the endpoint returns
  `404/405`. The `confirm` route stays (only for signals blocked by
  entry-zone / min-lot guards) and the matching `reject` - it is never
  a general signal approval.
- **LLM fallback with no manual end (spec, option b)**: an
  `llm_parsed=True` signal never auto-opens a trade and never goes pending
  - it is logged with `failed` status ("AI-parsed - log only") and
  sends a `system` note for audit. Applied in `_ingest_immediate`
  and in `finalize_awaiting_signals`.
- **Edit window - call on real data**: the spec needed to read
  full-signal edit rate before changing the rule. Measure
  of 26/08 on the real DB (342 live signals): of 305 that arrived complete
  at first parse, **0 were edited later (0.0%)** - under the 5%
  bar. Rule stays: complete -> runs now; 20s window only for incomplete
  parses (filed in TECHNICAL.md 4.1, ESTRATEGIAS.md, PUBLIC.md
  and AGENTS.md).
- **Group `go`**: ends the window at once and runs on the single auto path
  (no "manual pending" branch); post-window flow is now only risk
  checks + run.
- **Frontend**: static page badges (test/diagnostics/help/
  audit) and the header (`hdr-badges`) no longer name manual mode;
  the side terminal no longer shows the auto/manual picker.
- **Tests**: manual-mode cover cut from `test_api.py`/`test_timestamps.py`
  (complete signals run now, with no `/approve`); new path tests
  for LLM (never runs, logs + alerts) and `mode: manual` reject on
  config. Suite: **438 green tests**.

### 2026-08-20 - R2 per-trade recounts, R3 MFE win rate, R4 struggle rule

- **R2 - per-trade recount as outside fill-audit**: the classifier
  knows `Signal #N: Sell at X … +N pips` (`kind=recount`), the new parser
  (`app/messages/recounts.py`) pulls direction/entry/run/reverse/pips,
  and state stores in `trade_recounts` (dedup by message_id).
  `_recount_fill_audit` compares with closed REAL trades of the same day
  (direction + nearest entry, 40 pip tolerance): entry >=20 pips or
  pips >=30 apart fire a `risk` alert on feed/Telegram.
  `GET /api/recounts` lists (read-only).
- **R3 - MFE win rate (group convention)**: `simulate_signal` measures
  `mfe_pips` (max favorable run, pure watch) and `aggregate()`
  shows `mfe_win_rate`/`mfe_threshold_pips` (wins = MFE >= 25 pips by
  default, even closing at loss/BE). Group-to-bot split now compares
  with `mfe_win_rate` when on hand - fewer false positives for
  trades that ran +40 pips and came back to SL (win for the group, not for
  the bot).
- **R4 - "struggle" rule (backtest-only)**: new
  `struggle_ae_pips`/`struggle_fraction` params in the registry and in `simulate_signal`
  (before TP1, if price ran N pips against and comes back to entry, it closes
  a share at BE and guards the rest). Candidate
  `auto_escada_3_struggle` made and HIDDEN. **Sweep verdict (20/08)**:
  the `ae 5-25 x frac 0.25-0.75` grid (`app/sweep_struggle.py`, read-only)
  on `auto_escada_3/5` has no edge - the rule did not fire in the current
  span and only 3 signals on full history, with same-as-baseline
  numbers. The candidate stays backtest-only, not promoted.
- `GET /api/recounts` now returns each recount with its fill-audit
  (`matched`/`alert`) and the Report tab got the "📋 Group
  recounts vs real fills" table with divergent ones marked. A LIVE recount with no
  matching trade on the day sends an informative `system` note.
- **Richer audit (20/08)**: fill-audit also compares **direction**
  (SELL group vs BUY bot -> alert) and the **candle-check**
  (`_recount_candle_check`): group-reported run/reverse are
  checked vs the real high/low of the day Yahoo 15m candles (no
  symbol on the recount, links by near-entry day signal;
  no data -> `checked=False`, never made up). The `weekly` set
  (matching/divergent/no-trade per ISO week) feeds the Report-tab mini-chart,
  and the no-trade note now points the **likely
  cause** crossed with day signals (`_recount_no_trade_cause`:
  failed/rejected, pending, opposite direction, kill switch/manual). New
  E2E test of the full flow (table + audit + candle-check + note
  + dashboard contract).
- **Not-plausible-replay alert (20/08)**: a LIVE recount whose
  reported run/reverse does not fit the real day-candle range
  (`candle_check.plausible=False`) fires a "Not-plausible
  recount vs day candles" `risk` note for the operator to check the claim.
- **Hour window on candle-check (20/08)**: full-day high/low
  mixes sessions (a 22:00 peak does not prove a 09:00 signal
  claim). When target signal time is known, `_recount_candle_check`
  also measures the +-3h candle range around it and uses THAT verdict
  as `plausible` (the signal session); the whole day stays in
  `day_plausible`/`day_high`/`day_low`. The not-plausible note shows the
  window with flags; the recount-table tooltip shows day + window.
- **Read-only scripts (20/08)**: `app/sweep_recount_candles.py` checks
  plausibility of all stored recounts (plausible/not-plausible/
  no-data + rate) and `app/backfill_recounts.py` re-scans history
  (`ignored_messages`/failed `signals`) for `Signal #N`
  dropped as chat and puts them in `trade_recounts` with `--apply`
  (by default only previews). Ran on the real DB (20/08): it got back
  1 recount (the R1 fix message) and the sweep marked it not-plausible
  (4351 reverse under the 4378 day low - the 4357 entry was never even touched
  by candles).
- **R3 vs recounts study (20/08)**: `app/study_recount_mfe.py` (read-only)
  compares group-reported pips with real candle MFE
  (win match / false win / unreported wins). With 1 recount
  in DB: 0/1 match, 1 false win (group claims +110 pips, real MFE
  -3120) - pointing sample, not stats.
- **Not-plausible journal (20/08)**: when candle-check marks a
  not-plausible recount and the journal is on
  (`execution_journal_enabled`), `_handle_recount` writes a
  `recount_implausible` event in `execution_events` (signal_id, message_id,
  entry/direction/run/reverse/pips, window/day scope, flags) -
  passive evidence for replay and fill-audit (replay skips unknown
  kinds, so it never allows nor changes calls).
- **Per-window plausibility look badges (20/08)**: the main recount
  table now shows two badges when the +-3h window is
  on hand - green "✓ window ok" or red "✗ window not-plausible"
  as the session verdict, plus "day ok"/"day out" context - seen
  with no need for the tooltip (which keeps day + window + flags).
- **Not-plausible-vs-fills study (20/08)**: `app/study_recount_implausible.py`
  (read-only) crosses each not-plausible recount with same-day real trades:
  the bot traded (compares each side entry/pips) or did not trade
  (likely cause by day signal). On the real DB: 1/1 not-plausible, the bot
  did NOT trade - the day signal stayed `rejected` (shape/window), so
  what split was run, not only the claim.
- **Recount-quality block on /auditoria (20/08)**: new
  "📋 Group recount quality" part - stats (total, matching,
  divergent, no-trade, plausible, not-plausible, no candles), table of
  each recount with fill + candle-check and weekly state, loaded in
  `loadAudit` with journal-quality, fill-audit and forward-gate.
- **Fill-audit with recount evidence (20/08)**: the
  `compare_broker_fills` now returns `recount_evidence` - journal
  `recount_implausible` events listed as evidence split
  ("real" = group claim, "replay" = candle range), with
  `bot_traded`/`bot_trade` to cross who split (group or run),
  with no fresh Yahoo read. The /auditoria page shows them in a details of the
  fill-audit card.
- **Weekly not-plausible mini-chart (20/08)**: the /auditoria recount
  block got a per-week canvas (% plausible vs
  not-plausible vs no candles) next to tabular weekly state.
- **Re-check with fresh candles (20/08)**: `POST /api/recounts/{id}/recheck`
  remakes fill-audit + candle-check of one recount (Yahoo may
  have fixed/grown history) and returns the updated verdict;
  read-only - does not change the stored row or journal. "🔄
  re-check" button per row on /auditoria.
- **Set verdict of the not-plausible-vs-fills study (20/08)**: the
  `app/study_recount_implausible.py` classes each not-plausible as claim
  error / run error / no data and sends the set + top-5 of
  each. On the real DB: 1/1 run error (rejected signal).
- **Missed-run cause on fill-audit (20/08)**: `recount_evidence`
  crosses each not-plausible with `bot_traded=False` with listener
  restarts (+-15 min around the event) and the stored kill switch
  (`kill_switch`/`killswitch_rearm_at`) and returns `miss_reason` - a signal
  likely missed at listener start or kill on on the day.
- **Journal toggle on /auditoria (20/08)**: ⏸️/▶️ button on the journal-quality
  card turns `execution_journal_enabled` on/off with
  confirm, with no page leave (state mirrored on the button).
- **Recount row on the daily summary (20/08)**: the summary got "📋
  Day recounts: N got · X not-plausible · Y with no bot run"
  (journal when on), next to journal and Phase 3 rows.
- **Candle-check false-positive baseline (20/08)**: new read-only
  `app/study_recount_baseline.py` study - runs the same filter over real
  last-30-day SIGNALS (exact synthetic claim = real +-3h window
  range). Result: 66% of real entries fall outside the window - the
  Yahoo feed serves GC=F (futures) and the group trades spot, tens-of-points
  split; a not-plausible one does NOT prove a false claim, it must be
  crossed with bot fills.
- **Tuned GC=F-to-spot offset (20/08)**: candle-check now uses
  `_gold_futures_offset` - tunes the futures-to-spot offset on REAL
  XAUUSD fills (median gap between the GC=F candle at
  fill time and bot entry; needs >=3 samples and |offset| >= 5 points).
  When it fits, group-reported prices are shifted to the
  candle frame before compare (shown as `gold_offset` on
  candle-check) - fewer false positives with no made-up plausibility.
  On the real DB samples are still short (1 fill), so it does not
  change current results.
- **Signal-to-market lag (20/08)**: new read-only
  `app/study_signal_delay.py` study - measures in minutes when GC=F touches the
  posted entry vs Telegram time. Real result (30d, 101
  signals): 224 min median (~3.7h), p90 60h, only 28% of entries touched
  within 3h - the +-3h candle-check window is fair vs the real lag between the
  group and the futures feed; some not-plausible ones are feed/time
  split, not false claims.
- **Watchdog vs manual restart on miss_reason (20/08)**: the
  `recount_evidence` classes the listener restart (`restart_kind`:
  watchdog if reason holds "watchdog", else manual/outside) and counts
  signals in the start window (`signals_lost_est`, group messages
  in the 60s after restart - history replay recovers, but live
  run lags). /auditoria shows the recount not-plausible rate
  vs baseline (66%) so the operator sees if the claim is worse than
  expected or only feed split.
- **Re-baseline with 6h window + offset (20/08)**: `study_recount_baseline`
  takes `--window-hours` and `--apply-offset`; at 6h the rate drops 66.3% ->
  62.4%. Offset is still off on the real DB (1 fill < 3 min),
  so the gain is only window - the tune path stays
  proved for when the demo piles samples.
- **Offset panel on /auditoria (20/08)**: `recounts_report` shows the
  `calibration` block (counted offset, samples, on yes/no) and the recount
  block shows "GC=F-to-spot Offset: X (N samples)" - the operator knows
  when tuning goes live (>=3 fills).
- **gold_offset on journal (20/08)**: the `recount_implausible` event stores the
  applied `gold_offset` in payload - replay gives the same verdict
  with the same data.
- **vps_reboot on miss_reason (20/08)**: when `restart_kind=manual`, the
  `recount_evidence` crosses the restart with server start
  (`server_booted_at` stored in lifespan by boot.ps1) and returns
  `vps_reboot=true` if the restart matches boot - tells VPS reboot apart
  (NexGen Boot task) from a lone manual restart. Lifespan
  stores `server_booted_at` in config.
- **Batch re-check (20/08)**: new `POST /api/recounts/recheck-all` -
  remakes fill-audit + candle-check of ALL recounts with fresh
  candles (fetched once per symbol) and the newest offset, and
  stores the verdict in `trade_recounts.candle_check` (+ `checked_at`,
  auto migrate at start). /auditoria got the "re-check
  all" button and rows show the batch-stored verdict.
- **Per-symbol adaptive window (20/08)**: candle-check now uses
  `recount_window_hours_by_symbol` (config, JSON) per symbol, falling
  back to `recount_window_hours` (default 3h), capped at [1, 24]h.
  `study_signal_delay` measures lag per symbol and advises the window
  (p75 capped at [3, 12]h; `--apply` writes). On the real DB
  XAUUSD=6h was applied - the auto advice (12h) is tainted by the GC=F
  feed (p75 29h is far above 3.7h median: the tail is futures touching entry
  late); 6h is the re-baseline-checked middle (62.4%).
- **MT5 spot vs GC=F study (20/08)**: new read-only
  `app/study_recount_mt5_feed.py` - runs the SAME candle-check with XAUUSD
  terminal candles (native spot, no offset) side by side with
  GC=F. Real result: the 08-19 recount GC=F marked not-plausible
  is PLAUSIBLE on MT5 spot (4351 reverse fits real 4335 low; 4362
  run at window 4362 high) - a futures-feed false positive, not
  a false claim. It also fixed two stored
  `MT5Provider.copy_rates` bugs: `or []` on the numpy structured
  array (unclear truth -> download always failed) and field read
  by attr (numpy 2.x dropped it -> ts=0 candles); now index read
  with attr fallback (test fakes).
- **Offset-tune note (20/08)**: the tick checks each
  ~30 min (`_check_gold_offset_calibration`) the GC=F-to-spot offset and notes
  when it turns ON (>=3 fills: "✅ Tuning on"), when it
  moves >= 10 points ("↔️ Offset moved a lot") and when it loses
  samples - state stored in `gold_offset_reported` (config).
- **Candle-check with MT5 spot as primary source (20/08)**: production check
  moved to terminal XAUUSD M15 candles (native spot,
  5 min cache per symbol) with Yahoo GC=F fallback when the
  terminal is off - the futures-to-spot split no longer makes
  not-plausible ones. JOINED verdict: when spot says plausible but
  GC=F not-plausible, `feed_divergence=true` and `verdict_kind="feed_divergence"`
  ("⚠️ likely feed split" on /auditoria and dashboard, instead
  of a suspect claim); the GC=F-to-spot offset only fits the futures source
  (spot is native). `study_recount_mt5_feed --signals` re-based
  the baseline: with real spot **0.0%** of group entries fall outside the
  +-3h window (vs 66.3% on GC=F) - the not-plausible ones were ALL futures-feed
  split.
- **Daily batch re-check (20/08)**: the tick runs `_check_recount_batch`
  once per UTC day (guard `recount_batch_last_run` in config) - remakes the
  check of all recounts with joined sources and notes when
  a stored verdict changes regime or there are errors.
- **GC=F-to-spot offset gone + window reset (20/08)**: with MT5 spot
  as primary source, the futures offset lost its use - GC=F is
  compared with NO fix and only classes feed split. Gone:
  `_check_gold_offset_calibration` (tick), the "GC=F-to-spot Offset" panel of
  /auditoria and the `calibration` block of `recounts_report`; the widened
  XAUUSD=6h window reset to the 3h default. Check: the
  `study_signal_delay` on spot measures 101/101 entries touched within 3h (100% vs
  28% on GC=F), 106 min median - the 3h window covers all and spot
  not-plausible ones would be real suspect claims.
- **Suspect-claim alert only on confirm (20/08)**: the "Not-plausible
  recount" risk note now needs MT5 spot (primary) AND
  GC=F to both reject the claim (or no GC=F to confirm) - if spot says
  not-plausible but GC=F says plausible, there is no confirm and no
  alert fires. /auditoria shows "Confirmed not-plaus." (spot+GC=F) and
  "Feed split" (GC=F not-plausible, spot plausible) apart, with
  0% baseline (the 66% was the futures feed).
- **MT5 spot for XAGUSD/forex (20/08)**: `_recount_candles` was already
  symbol-blind (per-symbol copy_rates with Yahoo fallback);
  checked live that the terminal returns XAUUSD, XAGUSD and
  EURUSD M15 candles - the spot feed covers smaller metals and forex the same way.
- **Phase 3 ETA counts shadow (20/08)**: `_fase3_eta` counted the race
  origin (empty with kill switch ON - the run only opens when main
  runs) and showed N=0/no pace, while the gate already held 5-7 shadow
  trades. It now measures the SAME origin as the gate (`origin="shadow"` on
  stats/pace/curve) - the panel now shows the real pace of evidence.
- **Forward gate with hybrid ref + lower bar (20/08)**: the
  `auto_escada_3` ref was only the `nova_gestao` race run (which only fills
  when main runs - with kill switch ON it sat at 0 and the gate never
  went `reference_comparable`). Now (a) `min_reference_trades` dropped from
  20 to **10** (the P&L/exit compare is descriptive and never promotes
  alone); (b) the ref is **hybrid race+shadow** - race leads
  per signal and shadow (`nova_gestao` joined
  `shadow_paper_strategies`) covers signals where main did not trade.
  The gate no longer needs 20 real trades to call.
- **"undefined" fix on compare legend (20/08)**: the
  `loadTesteCompare` read `nova`/`nova_gestao`/`tp2`, but the
  `NAMES`/`COLORS` map only held `nova_gestao`/`tp2_protect`/auto-ladders - the
  legend drew the plain "undefined" for `nova` and `tp2` and curves
  fell on grid color (undefined strokeStyle). Added names/colors for
  `nova` and `tp2` + fallback (`STRATEGY_NAMES[id]` and `#8a94a6`).
- **Daily-loss slider (20/08)**: the dashboard "Caps" block got a
  `dailyLossSlider` (0-15%, 0.5 step; 0 = no cap) that stores
  `daily_loss_limit_pct` in config (debounced) and shows the live value
  (`dailyLossVal`); `limitsLbl` keeps only "max N spots".
- **No-cache + cache-bust on assets (20/08)**: the browser kept serving
  stale `common.js` (the slider sat at 3.0% with config at 4.5%).
  Added a `no-cache, no-store, must-revalidate` middleware on all not
  `/api/`/`/ws`, and pages now point `style.css`/`common.js` with
  `?v=<content sha1>` (second guard vs caches that skip
  no-store).
- **Slider 0 lined with the API (20/08)**: `daily_loss_limit_pct` takes 0
  (`ge=0`, before `ge=0.5` refused the slider min) and `RiskEngine` reads 0
  as "no daily cap" (off), like backtest `apply_daily_stop`.
- Full suite: **389 passed tests** + 1 known warning.

### 2026-08-20 - 28/08 window, re-arm and summary fixes

- Added `GET /api/window` with closed/open real trades, P&L, days
  left, pace vs the 10-20 target and the `auto_escada_3` gate+ETA.
- The daily summary adds the window run row; the dashboard shows the
  short promotion-progress card.
- `killswitch_rearm_at` stays stored; the tick re-arms at the set UTC time,
  notes feed/Telegram and clears the key idempotently.
- The classifier knows `summary_correction`, updates the target summary by
  clear reply/span and re-fires group-to-bot split.
- Full suite: **368 passed tests** + 1 known warning.

### 2026-08-20 - Forward-gate export, archive and block

- The `/auditoria` page now exports the journal + fill-audit + gate bundle
  in JSON/CSV and archives UTC snapshots in the `evidence_archives` table, with
  read-only history of windows.
- The daily summary sends on feed/Telegram one message with journal quality
  and `auto_escada_3/5` progress, with no run change.
- Auto-ladder promotion stays blocked in view and in backend until
  all objective forward-window gates pass.
- Checks and docs updated; suite of this change: **364 passed tests** + 1 known warning.

### 2026-08-20 - Daily audit, fill-to-replay and shadow forward gate

- Added `GET /api/operations/journal-quality`, `GET
  /api/analysis/fill-audit` and `GET /api/analysis/forward-gate`.
- The new `/auditoria` page shows daily journal cover, retcodes and
  latencies, trades with no full tape, the first real-fill vs
  replay split and the `auto_escada_3/5` gate.
- The gate uses a 30-day UTC window, min N=20, PF>=1.5, positive IC95%,
  DD<=125%, compare with `nova_gestao` and always-manual promotion.
- New checks for reports, endpoints and frontend.
- Full suite of this change: **361 passed tests** + 1 known warning.

### 2026-08-20 - MT5 fill, retcode and latency evidence on replay

- `MT5Engine` now logs on the opt-in journal open/close calls/results,
  SL modifies, replayed or confirmed fills, retcodes,
  tickets, prices, P&L and signal-to-order-to-fill latencies.
- Replay keeps these events as passive `broker_observations`: uses them
  for audit with no take of historic answers as orders and no touch of the
  production broker/DB.
- Per-trade export keeps the full tape and docs explain the controlled
  `execution_journal_enabled` turn-on.
- New MT5/replay checks. Partial suite of this change: **43
  passed tests**.

### 2026-08-19 - Replay export and step-by-step view

- `GET /api/operations/replay/tape` auto-joins `execution_events`
  to structured signals and links reply-based management; takes
  signal/trade filter.
- Replay returns snapshots after each event; `GET
  /api/operations/replay/snapshots` picks one step with no touch of the
  production DB or MT5.
- The dashboard history got per-trade `🔁 replay`, with parity report
  and three-engine state.
- New export, snapshot and frontend checks. Suite at this
  change: **353 tests**.

### 2026-08-19 - Fixed replay tape across engines

- Made `app/execution/replay.py` with a `sequence`-sorted UTC contract for
  `signal`, `quote` and `management` events.
- The read-only harness feeds the same tape to SimEngine, PaperRace and MT5
  dry-run in temp DBs and compares per-leg calls, SL, reason and P&L.
- Added `POST /api/operations/replay`; the endpoint caps the tape at 10,000
  events and never uses the production DB or broker.
- New checks cover order, TP1->BE, protect management and split.
  Full suite after the change: **350 passed tests** + 1 known
  Starlette/httpx warning.

### 2026-08-19 - Opt-in journal, explicit lifecycle and fail-safe routing

- Added `execution_events` + `app/execution/journal.py`: quotes and management
  can collect with no side effects and replay in order in a read-only
  study. Capture stays off by default (`execution_journal_enabled`)
  and is shown by `GET /api/operations/events`.
- `PaperRace` uses the injectable clock also for the daily-kill day; studies
  no longer hang on that civil-clock call of the machine.
- The strategy registry shows `lifecycle` (`backtest`/`paper`/`shadow`/
  `menu`/`promotable`) and paper accounts now use that central contract.
- No-reply management goes fail-safe with many trades of different signals
  of the same symbol: it does not guess the target and leaves the risk note for the
  operator to confirm.
- Paper metrics show a fixed bootstrap range and a descriptive
  evidence gate; nothing auto-promotes a strategy.
- The Diagnostics tab now shows read-only DB-to-broker reconcile (missing
  legs, unknown spots and split SL), with no auto-fix and no
  spot close.
- New checks: journal/replay, lifecycle, evidence and shadow split.
  Full suite: **346 passed tests** + 1 known Starlette/httpx warning.

### 2026-08-19 - Shared reducer and fixed clock (343 tests)

- Folded `reduce_auto_protect()` into `app/execution/protection.py`: the
  trigger, group-guard formula and monotonic moves now have
  one pure call reused by backtest, PaperRace, SimEngine and
  MT5 dry/real; each adapter stays in charge of its fills, broker and
  store.
- Added `app/execution/clock.py` with `SystemClock` and `ManualClock`.
  PaperRace and engines take an injectable clock; the polling study
  plainly moves 1s/250ms/100ms, with no need for `time.time()` or
  process sleeps.
- Removed the untracked double spec `docs/SPEC-auto-gestao.md`; the source

  docs stay `docs/ESTRATEGIAS.md`.
- Main `nova_gestao`, kill switch, `MT5_DRY_RUN=1`, ~1s production and
  shadow-paper did not change.
- Full suite: **343 passed tests** + 1 known Starlette/httpx warning.

### 2026-08-19 - Management, walk-forward and paper-pace studies (341 tests)

- Tuned walk-forward was widened to `current`, `90`, `all` and `old`;
  full management was green on 3/3 folds on the fresh shape, but only
  1/3 on the last 90 days and full history. The old OOS regime had
  2/3 green folds, with no denial that set P&L is a loser.
- `all_fallback_60` joined as a careful candidate; it did not move results
  on bigger spans and stays off by default.

- Made `app.study_strategies` with read-only studies of management-kind
  ablation, tuned walk-forward, per-leg risk split, mechanical
  fallback after TP1 and `PaperRace` sensitivity at 1s/250ms/100ms.
- On the current 30-day sample (107 signals), full management stood at
  PF 3.34 / +2,438.76 EUR / DD 22.7%; lone `protect` was the biggest
  giver (PF 3.09 / +2,177.62 EUR), with no promotion and no main change.
- Fallback hit 1 signal and added only 0.90 EUR; it stayed off
  by default. On a 1,000 EUR balance, TP3-TP5 weights are killed by min
  lot; the sensitivity study was split to a 10,000 EUR balance.
- The synthetic benchmark showed 1s can miss spikes that 250ms/100ms
  see, but it is no monthly-P&L evidence and does not change the production loop.
- Kill switch, `MT5_DRY_RUN=1`, `nova_gestao` and shadow-paper stay
  same.
- Full suite: **341 passed tests** + 1 known Starlette/httpx warning.

### 2026-08-19 - Safe main prep after reboot (338 tests)

- Post-reboot auto start confirmed: one matching server/listener pair,
  Caddy and MetaTrader 5 up; listener getting heartbeat and resending
  history with `200 OK` answers.
- Stored main promoted to `nova_gestao`, the best strategy on the
  fresh comparable backtest; `max_positions=5` and `auto_escada_3`/`auto_escada_5`
  shadow-paper were kept.
- `balance`/`peak_balance` re-based to 1,000 EUR and base risk cut to
  1.0%, killing the fake 0.1% cut from the old 11,304 EUR
  high-water mark.
- Left on purpose `kill_switch=true` and `MT5_DRY_RUN=1`: dashboard and
  shadow still take signals, but no MT5 orders with no explicit
  run permit. Pre-change local backup: `backups/signalbot-20260819-205634.db`.
- Full suite: **338 passed tests** + 1 known Starlette/httpx warning.

### 2026-08-19 - Auto-protect seen on dropdown (338 tests)

- Fixed the four nav menus (`/`, `/teste`, `/diagnostico` and `/help`) to show
  `auto_protect_5` as **Auto 5 TPs + early protect**, with a link to the test page.
- Synced `STRATEGY_NAMES` and `TERM_ORDER` in `common.js`, so the strategy
  exists in the registry but never hides from frontend.
- Added a static check to need the steady entry on all four pages; the full
  suite ended with **338 passed tests** and 1 known Starlette/httpx warning.

### 2026-08-19 - Defensive risk, restart-safe state and two-auto-ladder shadow (337)

- Added `def_risk_pct=1.25%` to the auto-ladder defensive mode: after one full
  10-trade window with PF < 1.0, backtest, PaperRace, SimEngine and MT5 apply the
  same absolute cap, with no push of moving risk and no leg cuts.
- Made the `protection_states` table, with a local pre-migrate backup at
  `backups/signalbot-20260819-144647.db`; peaks/valleys, TP1, ladders, SLs and stamps
  live through each engine restart and are erased idempotently when the trade closes.
- The VPS shadow-paper was set for `auto_escada_3` and `auto_escada_5`, keeping
  apart accounts/origins; main `auto_escada_5` strategy and kill switch did not change.
- Added checks for the defensive cap, full window, paper sizing and post-restart
  peak rebuild.
- Full suite checked: **337 tests passed** + 1 known Starlette/httpx deprecation
  warning.

### 2026-08-19 - Auto-protect, safe replay and size analysis (334)

- Added candidate `auto_protect_5`: it closes the first leg 75% of the way to
  TP1 and guards the rest with the same monotonic formula as group `protect`,
  shared by backtest, SimEngine, PaperRace and MT5; it stays off main and needs
  sweep/paper before promotion.
- History replay now carries `reply_to_msg_id`, `message_id`, leveled `move_sl`
  and per-leg `close_tp`; unlinked replies are dropped instead of tainting another
  same-symbol trade.
- Made `GET /api/analysis/resolution`, with native M1 to 7 days, plain fallback
  for longer views and 0/8/60/300 s lag sensitivity, with no writes and no look-ahead.
- Synced HELP, the strategy panel and shadow-paper; the current suite has **334 tests**.
- Read-only check on the current DB: on 30 days/107 signals, `nova_gestao` stood at
  PF 3.35 / +EUR 2,439.75 / DD 22.7%, `auto_escada_5` at PF 1.33 / +EUR 345.41 /
  DD 35.9% and `auto_protect_5` at PF 1.27 / +EUR 281.41 / DD 38.6%; the candidate
  does not beat auto 5.
- The candidate sweep (0.25-1.0 trigger x 0-0.20 buffer) found no combo with PF>=1
  on current and old spans: best current was PF 1.96 / +EUR 618.62, but best old
  stayed PF 0.50 / -EUR 3,450.62.
- Time analysis confirmed native M1 to 7 days (23 signals: PF 4.24 vs 7.72 on
  15m) and plain fallback to 15m on 30 days; no partial history was shown as M1.

### 2026-08-19 - HELP synced with seen strategies (325)

- Updated the `/help` page: the seven seen flows (`nova_gestao`,
  `tp2_protect`, `auto_3`, `auto_escada_3`, the hybrid, `auto_escada_5` and
  `auto_protect_5`) now have plain mechanics, and the glossary holds
  ladder, trailing, defensive mode and apart shadow-paper.
- Fixed the comment naming `tp2_protect` as hidden; the page
  still pulls the registry from backend and hides the six
  backtest/counterfactual-only strategies.
- The static check needs one `HELP_FLOW` entry per seen strategy and
  keeps the rule in `AGENTS.md` for future registry changes.
- Suite: **325 tests** (no new case count).

### 2026-08-19 - End audit of shadow split and no-reply management (325)

- Fixed the shadow-management fallback to follow the same safe order as the
  real account - exact reply, named symbol and, last, the newest
  open shadow trade - always filtering `origin="shadow"`.
- Fixed the `divergence report` to only compare same-origin trades;
  a `race` reference can no longer make a false split on a
  `shadow` auto with no shadow reference.
- Added a no-reply pending-management check, one cross-origin split
  check and one no-reroute-to-foreign-shadow-trade check.
- Suite: **325 tests passed** + known Starlette
  deprecation warning.

### 2026-08-19 - Shadow-paper: truly split race/shadow accounts (322)

- Fixed shadow-run split: dedup now counts
  `signal_id + strategy_id + origin`, letting the same signal be
  watched by `race` and `shadow` accounts with no copy on either.
- Virtual balances, daily kill, PF and account history are split per
  origin; the shadow account starts at 1,000 EUR and never taints the
  normal-run evidence. Added two checks for side-by-side open and the
  main run path.
- Suite: **322 tests passed** + known Starlette deprecation
  warning.

### 2026-08-19 - Apart forward shadow-paper (320)

- Made `shadow_paper_enabled` mode (default on) with stored
  strategy pick (`shadow_paper_strategies`, default `auto_escada_3`). Full
  live signals enter a paper account tagged `origin="shadow"` before
  main risk/kill calls, but never call the MT5 engine; history
  replays and LLM proposals stay out.
- Per-signal/strategy dedup stops copied virtual trades. The main
  panel shows the apart state and turns on/off and picks
  strategies. Reply-based management, TP1/SL fixes and pendings use apart
  markers for real run, paper run and shadow (`applied`,
  `paper_applied`, `shadow_applied`); a shadow delivery does not eat the pending
  item a normal paper trade still needs. `out at BE` keeps BE guard
  when the note is queued.
- Updated `AGENTS.md`, `TECHNICAL.md`, `PUBLIC.md`, `ESTRATEGIAS.md` and
  `DEPLOY.md`; added history/LLM, kill switch, origin,
  management-with-parallel-real-trade, TP1/SL and BE-pending checks.
- Suite: **320 tests passed** + known Starlette deprecation warning.

### 2026-08-19 - Items 10/17: run watch and CI (308)

- **Item 10**: runtime exceptions, MT5 spread notes, link/reconnect
  events and SL modifies now store in their own
  tables; pip math keeps the pip convention apart from the 5-digit
  forex tick, stopping a 10x-puffed spread/risk gap.
  Metrics set in `GET /api/operations?days=N` and show on the
  "Operational health" panel of the Report tab. MT5 also audits a
  failed BE modify before losing it in the log; no risk, strategy
  or trade-open change.
- **Item 17**: made `.github/workflows/ci.yml` for `pytest -q` on
  push/PR, split doc check and manual `failure_probe` that turns the
  job red with a deliberately failing test; the probe uses a valid
  pytest module name to fail by assert, not import error, and
  clears the file with `trap`.
- End take of Item 10 still needs two weeks of data;
  Item 17 needs the first GitHub Actions run. Specs stay
  until that evidence exists.
- Suite: **308 tests passed** + known Starlette deprecation
  warning.

### 2026-08-19 - Safe run: hedged account, logs, disk, restore and docs (302)

- **Item 15**: `MT5Provider` reads `account_info().margin_mode`, shows
  `margin_mode`/`hedged` in status and blocks netting or
  unknown-mode accounts in real mode before any order; the check covers a
  `RETAIL_NETTING` and `RETAIL_HEDGING` account with a fake MT5 module.
- **Item 7**: `app.server_runner` and the listener use rotating UTF-8 streams
  (10 MiB/5 copies by default, settable), with trim of
  stored oversized bits; `backup.log` uses the same
  policy and `maintenance.py --db-audit` gives counts, months seen,
  monthly rate and hold policy per table.
- **Item 8**: daily read-only free-space check on the watchdog, with settable bar and
  volume, state in snapshot/diagnostics and a `risk` alert on the feed
  (and Telegram when set); the last check/alert day stays so it never
  doubles the warn after a restart.
- **Item 9**: `backup_db.py --restore-check` restores the newest backup in a
  temp DB, checks integrity/tables/counts and alerts on fails; tests
  cover win, corruption and count mismatch.
- **Item 14**: PUBLIC/TECHNICAL/ESTRATEGIAS were lined with the real
  8s window, full history replay, MT5 demo, login, real-candle
  feed/fallback, 2.5% risk, 3% daily kill and run caps.
- Fixed an async race on Phase 3 check: a verdict set for
  an old promotion can no longer overwrite the just-promoted window.
- Suite: **302 green tests** + 1 Starlette deprecation warning.

### 2026-08-19 - Item 3: auto_escada_3 confirm (292)

- Built the read-only `--item3-sweep` command, which compares
  `confirm_filter=True` vs `False` on current/all/old windows, keeping
  `stair_levels=3`, `stair_from=1`, `trail_factor=2.0`, costs and candles
  same. Variants share no changeable state and have checks for
  param spread, gate and no leakage.
- The real replay passed the bar: current PF **1.27->1.26** (relative
  0.79% drop) and old DD **312.9%->278.3%** (11.06% cut); all was
  0.47/322.0% vs 0.53/286.1%. `auto_escada_3` kept
  `confirm_filter=True` by default.
- Main stays `nova_gestao`, no param went to the
  demo account and spec `docs/Melhorias/03-item-03.md` was erased only after
  the take.
- Focused and full suite: **292 green tests** + 1 Starlette deprecation
  warning.

### 2026-08-19 - Phase 3 link and ready for Item 3 (289)

- Added check `test_fase3_promotion_paper_to_mt5_restart_and_rollback`:
  the same entry walks the paper run, promotion, MT5 path with a fake
  broker, restart/state rebuild, two refused 10016 modifies and
  auto rollback - with no orders to the demo terminal.
- Fixed the window verdict: it only counts the **first 10 trades
  opened after promotion**, caps review at that lot and needs the same exit
  as paper on >=9/10; with no paper-to-MT5 pairs the window fails safe.
  The refused-retcode message now shows real codes.
- Run audit: only the expected main server/listener pair is
  on (port 8000 and heartbeat); no orphan listeners/servers sat on
  preview ports 8081-8083, so nothing safe to kill.
- Item 3 is unblocked to start as a read-only sweep of
  `confirm_filter=True` vs `False`; the spec stays until the result runs
  and is filed.
- Suite: **289 green tests** + 1 Starlette deprecation warning.

### 2026-08-19 - Hard review of lifecycle, parity and sweep edges (287)

- Fixed guard edges: time stop now skips bad lengths,
  sorts naive/UTC stamps with no crash and keeps the set edge.
- Fixed the consolidation study: ISO offsets (with negative zones)
  turn to UTC, daily ATR uses the last reading of each day and the
  p20 filter only reads prior days; the pre-entry candle ATR no longer
  drops from an off-by-one.
- Added checks for these edges, plus the updated count of
  **287 tests**. No order went to the demo account.

### 2026-08-19 - Item 1 closed, lifecycle/parity and Item 2 sweep (280)

- **Apart lifecycle**: `app/background.py::BackgroundRuntime` now
  plainly owns market/watchdog tasks, with idempotent start,
  cancel and `await` on FastAPI shutdown. `TestClient` and the MT5 suite
  no longer inherit orphan loops.
- **Ongoing parity**: `tests/test_engine_parity.py` splits buy/sell and
  sends the same price events to backtest, SimEngine, PaperRace and MT5
  dry-run; `app/execution/parity.py` normalizes calls, applied SL, reason and
  P&L to compare the four paths.
- **Hybrid causal proof**: `GET /api/analysis/protect_causal` replays read-only
  by prefixes and credits the step delta of each `protect`.
  On 19/08 history: 355 events/270 signals, 0 harmful and 0 EUR delta; the
  hybrid tied pure auto (PF 3.20/DD 24.6% on 30d; PF 2.21/DD 32.5%
  full). `docs/Melhorias/01-item-01.md` was erased only after the take.
- **Item 2 started after Item 1 cut**: `study_consolidation.py`
  got the reproducible `--item2-sweep` command, with a no-leakage
  3x3x3 grid, smoothness review and PF/DD gate. Result: 0 eligible
  for old DD <50%; lone spike on the current `def_window` axis; defaults
  kept; spec `docs/Melhorias/02-item-02.md` was erased after
  the take.
- **Suite**: **280 green tests** (`pytest -q`, 73.5s) + 1 Starlette deprecation
  warning; no order went to the demo account.

### 2026-08-19 - Shared guard core across the three engines (272)

- **Shared pure policy** (`app/execution/protection.py`): holds
  per-direction SL monotonicity, ladder, ATR-floor trailing,
  confirm, set time-stop edge, `sl/be/trail` class
  and careful hybrid `protect` merge. It touches no DB, clock or broker.
- **Adapters**: backtest, SimEngine, PaperRace and MT5Engine use the same
  policy to set levels and edges; each engine keeps only its fills,
  store, throttle and retcodes. Trailing stays look-ahead
  free and MT5 only writes SL after a taken retcode. The regime alert
  now takes `sim_params(strategy_id)`, so it scores the same ladder,
  trailing, defensive mode and ATR of the active strategy.
- **Checks**: buy/sell monotonicity cover, ATR floor, TP/SL tie,
  set time stop, ladder, look-ahead-free trailing, hybrid guard
  and post-restart paper cycle.
- **Suite**: 272 collected tests; focused check of the guard,
  strategy and backtest modules is green. No order went to the demo account.

### 2026-08-19 - Hard audit of the hybrid run and docs (267)

- **PaperRace**: leg `closed_at` now also updates on the
  in-memory object and the trade end reason uses the leg really closed
  last; a sequence tie-break covers ladder/confirm closing many
  legs on the same tick.
- **Divergence report**: covers both auto->reference and
  reference->auto orders; the "auto still on" alert stays pending and updates with
  the end P&L of both sides when the auto closes, with the right end reason.
- **Docs**: AGENTS/TECHNICAL/ESTRATEGIAS/PUBLIC lined with 267
  tests, 12 strategies in the registry and 6 seen on the menu (hybrid stays
  trial).
- **Focused check**: `test_protection.py` (6), `test_strategy.py` (16),
  `test_backtest.py` (35) and `test_docs.py` (1) green; the full suite was
  only collected (267), not used as the bar of this audit because
  it holds long-background-loop tests.

### 2026-08-19 - Strategy cleanup: REMOVED alias + curve of seen only (255)

- **`app/strategies.py`**: `REMOVED_STRATEGIES` alias (= `HIDDEN_STRATEGIES`,
  the 4 removed) so code reads plain to the cleanup plan;
  docstrings updated (the `strategies_list` tells REMOVED - off
  menu AND paper - from MENU_HIDDEN - off menu but on paper).
- **`GET /api/paper/history` (run curve)**: now filters
  `HIDDEN_STRATEGIES` and `MENU_HIDDEN_STRATEGIES` - before it showed ALL
  strategies with history (the 4 removed + nova/auto_5 showed on
  curve, vs the ESTRATEGIAS.md Part IX matrix). Now the curve only shows
  the 5 seen + the ⚡ real line (checked live: nova_gestao,
  tp2_protect, auto_3, auto_escada_3, auto_escada_5, __real__).
- **Backtest of the 5 seen on full history** (579 signals, 1000
  balance): nova_gestao PF 1.00 · +20 EUR (dies), tp2_protect PF 0.82 · -1,399
  EUR (dies), auto_3 PF 0.43 · -3,207 EUR (dies), auto_escada_3 PF 2.10 ·
  +6,863 EUR · DD 24.3%, auto_escada_5 PF 2.30 · +11,063 EUR · DD 25.8% -
  table added to ESTRATEGIAS.md Part VIII with the honest read
  (only ladders have edge on full history; the reference sits at
  break-even because the old span wipes the current).
- **Tests**: updated `test_paper_history_endpoint_returns_series` (nova
  leaves the curve; the series uses auto_escada_3); `test_fase3_validation_rollback`
  hard vs the back loop (re-promotion reopens the window and the loop
  can re-score with the same trades - the contract is "never erases
  history", not "stays at 1").
- Suite: **255 green tests** · server restarted in `mt5-real`.

### 2026-08-19 - Paper-run snapshot on EVERY promotion (255)

- **`update_config`**: the paper-run snapshot (`fase3_snapshot` -
  paper stats, DD, paper-to-MT5 splits, SL audit) is written on
  **any** strategy promotion (Phase 2/3 panel, feed or test
  pages), not only `auto_escada_3` - before/after stays filed for
  any promoted strategy. **Validation-window reopen**
  (clearing `fase3_verdict`) stays only for autos with auto-manage.
- **Updated test** (`test_fase3_promotion_snapshot`): promoting `nova`
  also writes a snapshot (`strategy_id: nova`), the verdict stays whole on a
  non-auto promotion and auto promotion clears again (reopens the window).
- **Checked screenshot**: seeded box (21 auto trades PF 4.0 · 15
  ref) - Phase 2 panel with 140% P&L · 3% vs 3% DD · N 21/20, "Approval
  bars met" verdict and **⬆️ Promote to main
  (Phase 3)** button seen (cleaned after: process + DB + script).
- Suite: **255 green tests** · server restarted in `mt5-real`
  (nova_gestao · auto · MT5 on · listener live).

### 2026-08-19 - Phase 2 panel: promotion button + full honest P&L/DD (255)

- **⬆️ Promote to main on the Phase 2 panel**: when the strict 3/3 gate
  passes (N >= 20 · PF >= 1.5 · P&L >= 50% · DD <= 125%), the panel
  shows the same button as Phase 3 (shared handler) - POST /api/config
  {strategy} which, on promoting the auto, **writes the SL/split
  audit snapshot** (`fase3_snapshot`: dd_auto/dd_ref, pnl, pf,
  paper_vs_real_pairs, sl_audit_entries, real_trades_closed) for the check
  window to compare before/after (checked live: API promotion
  on the check box wrote the full snapshot).
- **Full honest P&L/DD (tri-state)**: with real data, minis
  show the value (e.g. "auto vs ref P&L 140%" with EUR tooltip, "DD 3% vs
  3%"); with no closed trades they show "-" and the gate counts 0/3 - before the
  panel gave "✓ P&L · ✓ DD · 2/3" on an empty run. Same logic on the
  Phase 3 panel (same).
- **Live check**: check box with a seeded temp DB
  (21 auto trades PF 4.0 · 15 ref) - panel filled 140%/3% vs
  3%, "Approval bars met" verdict and button seen;
  then clean (dead process, DB and script gone).
- Suite: **255 green tests** (only frontend moved).

### 2026-08-19 - Strict 3/3 gate on Phase 2 panel + check-window tests (255)

- **Phase 2 panel lined to the strict 3/3 gate** (same as Phase 3): take
  needs N >= 20 · PF >= 1.5 · **P&L >= 50% of reference** ·
  **DD <= 125% of reference** at once (before only N+PF). The panel gets
  the "auto vs ref P&L" and "auto vs ref DD" minis and the verdict shows
  3/3 progress (PF · P&L · DD); the instruments refresh also re-draws
  Phase 2 (DD came from `_phase3dd`).
- **New check-window tests** (standalone `_compute_fase3_verdict`
  with temp DB + mocks, no singleton): `slow_opens` (signal-to-open
  > 2 min -> rollback, PF >= 1 proving rollback comes from lag) and
  `pnl_incoherent` (real losing where sim called gain -> rollback with
  PF 18).
- **test_mt5.py: +2 auto-manage paths** - `confirm_filter` (mid
  leg takes SL -> far legs close with `confirm` reason, leg 1
  whole) and **SELL ladder** (legs 2/3 SL down to the
  TP1 level, in DB and on broker). 25 tests in the file.
- **Suite: 255 green tests** (251 + 4). AGENTS.md and ESTRATEGIAS.md
  updated.

### 2026-08-19 - test_mt5.py rebuilt: 5 helper tests back (251)

- **Full test_mt5.py with 23 tests** (18 + 5 rebuilt): taken 0 retcode,
  NO_TICK leg stays open (the trade is not phantom), 10027 flag only
  when **no** leg hits the broker, partial retcodes, AutoTrading
  ON with orders hitting the broker, and `exit_at_tp` (leg 1 closes at TP1,
  rest at TP2) - behaviors checked vs the real engine.
- **Suite: 251 green tests** (count back to pre-replace level).
  AGENTS.md and ESTRATEGIAS.md updated to 251.

### 2026-08-19 - Phase 3: strict 3/3 gate + check window with copies/retcodes

- **Promotion gate with board rule (2 of 3)**: the Phase 3 panel needed
  ALL bars (AND); doc Part X says "board call, 2 of 3
  - never lone PF". Lined to doc: `renderPhase3` now marks each
  bar (PF>=1.5 · P&L>=50% · DD<=125%) and the promote button opens with
  **N >= target + 2 of 3 bars** (verdict shows the board side by side).
- **Check window shows the doc metric (>= 9/10 same exit)**: the
  `_check_fase3_validation` verdict got `same_exit_n`/`same_exit_pct`
  (paper-to-MT5 pair count with the same exit call) and the reason now
  has "same call as paper on N/10" - seen on panel and
  feed/Telegram. The maintenance docstring lined (PF >= 1.0 + >= 9/10 +
  zero with no SL).
- **Old-spec orphan refs cleaned**: §B.5.x pointed at the
  erased SPEC-auto-gestao.md - updated to "ESTRATEGIAS.md Part X"
  in `app/state.py` (x6), `app/strategies.py` (x2), `app/backtest.py` (x2),
  `app/execution/mt5.py` (x4), `app/execution/paper.py` (x1), `app/main.py`
  (x2), `app/sweep_atr.py` (x1), `app/static/index.html` (x3, with the
  panel title and promote confirm) and AGENTS.md (board rule +
  same-exit metric + "SPEC v2 §3.4" refs -> Part X/VIII). Items
  5/6 of the checklist (paper-to-MT5, real vs backtest) are now tagged
  as **check-window** bars (scored on the 10 real trades),
  not promotion gates.
- old ones; the full file has the current 18 tests).
- "SPEC v2" refs (file folded into ESTRATEGIAS.md) on the Phase
  2 panel fixed to "ESTRATEGIAS.md Part VIII/X" - checked on
  preview (Phase 3 panel drawing with the 3/3 gate and new metrics).

### 2026-08-19 - Phase 3 panel lined with ESTRATEGIAS.md Part X (doc-to-code audit)

- Tests: new assert in `test_fase3_validation_rollback` (`same_exit_n`/
  `same_exit_pct` field on verdict). **250 green tests**.

### 2026-08-19 - Spec folded into ESTRATEGIAS.md (single source) - SPEC-auto-gestao.md erased

- `docs/SPEC-auto-gestao.md` (892 lines) was **folded into
  `docs/ESTRATEGIAS.md`** and **erased** - ESTRATEGIAS.md is now the single
  source on strategies: Part V got defensive mode, Part VI the
  escada_3/5 defensive params, and the new **Part X** got the
  Phase 3 path (§B.5: N>=20 · PF>=1.5 · P&L>=50% ·
  DD<=125% pre-reqs, 10-trade check window with auto rollback,
  verdicts/ETA/target) and **SPEC v3** (real account, §B.7 draft: ongoing
  gates + red line). Done-phase history stayed in
  CHANGELOG; fine tune in scripts
  `app/mine_management.py`/`app/sweep_auto.py`/`app/sweep_atr.py`/
  `app/study_consolidation.py`.
- Refs updated: `app/strategies.py` (x2), `app/execution/mt5.py`
  (§B.5 -> Part X), `app/mine_management.py`, `app/sweep_auto.py` and
  AGENTS.md (Auto-manage + Phase 3 part). **250 tests**.

### 2026-08-19 - Defensive mode built on auto_escada_3/5 (backtest + paper + MT5)

- **Defensive mode (19/08 study) built**: new registry params
  (`def_window=10`, `def_trail=1.5`, `def_time_stop=60`) on `auto_escada_3/5`
  - when PF of the last 10 solved trades < 1.0, the strategy moves to
  tight trail + time stop with NO leg cuts; back to normal when PF
  recovers. Mirrored on 3 engines, no look-ahead:
  - **backtest** (`simulate_signal` + `run_analysis`): shared `def_state`
    across signals (time order); `_roll_pf` helper;
  - **paper** (`PaperRace._update_trade`): reads the last 10 closed trades
    of the virtual account (`db.paper_closed_recent`) each tick;
  - **real MT5** (`_update_real`, Phase 3): reads the last 10 closed REAL
    trades (`db.recent_closed_pnls`) - `auto_eff` with defensive
    trail/time stop;
  - **snapshot**: `strategy_params.defensive` shows when main is
    in defensive mode ("🛡 defensive mode" header badge).
- **ATR-only sensitivity** (study): the entry filter rejects 0 signals
  on ATR alone (rejects were all session, which only filters on the old
  span) - proof the filter has no edge.
- 5 new tests: `test_defensive_mode_tightens_trail_after_pf_drop` +
  `test_defensive_mode_requires_window_of_resolved` (backtest),
  `test_paper_defensive_mode_...` (paper), `test_real_mode_defensive_...`
  (MT5 E2E with fake broker) and `test_defensive_mode_params_in_registry`.
  **250 tests**. Docs: AGENTS/TECHNICAL/ESTRATEGIAS/spec.

### 2026-08-19 - Strategy consolidation (spec) + filter/defensive-mode study

- **Consolidation phase 0**: `HIDDEN_STRATEGIES = {tp2, tp2_gestao,
  nova_gestao_3, auto_2}` (off menu AND paper - only backtest/compare;
  loser/spare) and `MENU_HIDDEN_STRATEGIES = {nova, auto_5}`
  (hidden from menu but still on PAPER - `nova` is the "no
  management" counterfactual of trade history and `auto_5` the 5-leg
  with/without-trail compare). 5 stay seen on menu: `nova_gestao`, `tp2_protect`,
  `auto_3`, `auto_escada_3`, `auto_escada_5`. Dropdown of all 4 pages and
  `TERM_ORDER` updated; removed ones stay reachable by URL and on
  backtest/compare.
- **Consolidation study (`app/study_consolidation.py`)**: entry
  filter (London/NY session + entry-to-TP2 gap >= 5 pips + ATR p20) and
  defensive mode (PF(10) < 1.0 -> trail 1.5 + 60 min time stop, with NO leg
  cuts) on 3 windows. **Verdict**: the entry filter has NO edge
  (rejects almost only on the old span where all loses; session filters
  nothing on current; ATR = noise) - not built. Defensive mode has
  a small but steady effect on `auto_escada_3/5` (-5pp DD on
  full and old history, same or better P&L, no cost on current) -
  to build with paper-run check before Phase 3.
- Tests updated for consolidation (paper only on seen +
  counterfactual; switch hint = tp2_protect). **245 tests**.
  Docs: AGENTS/TECHNICAL/ESTRATEGIAS.

### 2026-08-19 - All strategies back on menu (dropdown) + kill switch re-armed

- **User call**: bring ALL 11 strategies back to the menu,
  test pages and paper run - `HIDDEN_STRATEGIES` and
  `MENU_HIDDEN_STRATEGIES` in `app/strategies.py` stay **empty** (the
  machine stays: hide = add the id back; nothing leaves
  backtest). The user will cut the worst with live data.
- **STRATEGIES dropdown** on menu (all pages): all 11 variants
  live in a folding panel (`.term-drop-toggle` + `.term-drop` in
  `style.css`) - localStorage-stored toggle (default open), closes
  on outside click, keyboard nav opens it alone when reaching an
  item inside (`TERM_ORDER` in `common.js` mirrors DOM order).
- **11-account paper run**: the parallel curve and legend show
  all strategies; regime auto-switch can now hint
  any leader (updated test: tp2 with bigger balance is the hint).
- **Kill switch re-armed**: it was on (prior-session auto_kill,
  paper loss - nothing closed today, 0 trades) - turned off so real
  run opens again (demo).
- Tests: `test_hidden_strategies_kept_in_registry_but_marked_hidden`
  rewritten (empty sets, all in registry) + paper tests updated
  (all strategies open paper trades; close_all/protect only on those
  honoring the kind). **245 tests**. Docs: AGENTS/TECHNICAL/ESTRATEGIAS/PUBLIC.

### 2026-08-19 - Ref curve, target-cut log, single Phase 3 endpoint

- **Mini-curve with the ref**: `fase3_eta.pace_daily` now has
  `n_ref` (per-day signals of `nova_gestao`, same 60-day window) - the Phase
  2 panel shows both accounts side by side (purple auto bar + blue
  ref bar, with legend) to compare pace.
- **Target-cut log**: `fase3_target_log` (config, JSON,
  last 20) - each `fase3_target` change logs `{at, from, to,
  reason}` (reason comes from `fase3_target_reason` of POST; the feed button
  sends "ETA > 6 weeks (feed button)"). "🎯 Target changes"
  block on panel history + shown in `snapshot()["fase3_target_log"]`.
- **Single endpoint** `GET /api/analysis/fase3_status` (`state.fase3_status`):
  joins ETA (curve set), snapshot, verdicts + history, target log,
  paper-to-MT5 splits, real vs backtest, account DD (set on
  server) and SL audit - the panel went from 4 calls to 1
  (`refreshPhase3Instruments`).
- Tests: `test_fase3_status_endpoint` (full payload) + widened
  (`n_ref` on curve; `fase3_target_log` with reason). **245 tests**.
  Docs: AGENTS/TECHNICAL.

### 2026-08-19 - Dedicated strategy doc

- New **`docs/ESTRATEGIAS.md`** - full guide of current strategies:
  what a strategy is (legs, SL-first, BE, manage_kinds, auto),
  the 3 in use (`nova_gestao` main · `auto_escada_3` · `tp2_protect`),
  hidden ones (backtest/counterfactual), group management (kinds),
  auto-manage (ladder/trailing/confirm/time stop + rejected ATR),
  per-window results and where each shows. Linked in AGENTS.md.
  **244 tests** (docs only).

### 2026-08-19 - Pace mini-curve, settable Phase 3 target (20->15), summary with rollback

- **Pace mini-curve on the Phase 2 panel**: `fase3_eta.pace_daily` -
  per-day signal count of auto_escada_3 over last 60 days
  (zero-filled, `db.paper_trades_opened_by_day`) drawn as bars
  with tooltip (day · signal count) - the trend feeding ETA.
- **Settable window target** (`fase3_target` config, default 20, clamp
  10-20): when ETA counts > 6 weeks, the feed note gets the
  "🎯 Cut target to 15" button (action `fase3_target:15`, confirm
  on frontend) -> `POST /api/config {fase3_target}` -> ETA and panel now
  count to the new target (N/15, checklist, verdict). `ConfigUpdate`
  model + `config.fase3_target` snapshot.
- **Daily summary with same-day rollback**: `_fase3_mt5_line` shows the
  🚀 auto-MT5 row also when the window closed TODAY (fresh verdict)
  even with the auto already removed - marking rollback (UTC
  compare so it never misses near local midnight).
- Tests: `test_fase3_target_config` (default/clamp/422) + widened
  (`pace_daily` 60 days; rollback row on summary). **244 tests**.
  Docs: AGENTS/TECHNICAL/spec §B.5.4-§B.5.5.

### 2026-08-19 - Daily summary with auto MT5, ETA alert, verdict history

- **Daily summary with auto MT5** (spec §B.5.5): when Phase 3 runs
  (promoted auto + snapshot), the daily summary now has the
  🚀 row with real N and real P&L of trades closed since window
  start and state (running x/10 or verdict) - `state._fase3_mt5_line`.
- **ETA alert on feed** (`_check_fase3_eta_alert`, 24h cooldown): the
  `fase3_eta` now compares pace with the PRIOR MONTH (`pace_prev`,
  `pace_change_pct`, 30-60-day window) and warns when the window counts
  **> 6 weeks** or pace moves **>= 50%** vs the prior month (ETA
  rebuilt with the new pace) - feed/Telegram.
- **Verdict history**: `fase3_verdicts` (config, JSON, last 20)
  - each check window APPENDS its verdict; reopening the window
  (new promotion) only clears the CURRENT verdict (`fase3_verdict`), history
  never erases (auto_* growth audit). Phase 3 panel got the
  "📚 Check-window history" block; also shown in
  `snapshot()["fase3_verdicts"]`.
- Widened tests: `test_fase3_eta` (pace_prev/eta_days) and
  `test_fase3_validation_rollback` (history kept on reopen).
  **243 tests**. Docs: AGENTS/TECHNICAL/spec §B.5.4-§B.5.5.

### 2026-08-19 - ETA of 20 signals + auto verdict of the check window

- **ETA on Phase 3 panel**: `snapshot()["fase3_eta"]` sets the
  `auto_escada_3` signals/day pace (paper trades opened in last 30
  days, `db.paper_trades_opened_since`) and the due date when N hits
  20 (`done=True` when already there; `eta=None` with no pace yet) - the panel
  shows "Pace · ETA 20" with date and pace next to the N/20 count.
- **Auto verdict of the check window** (§B.5.4 automated):
  `state._check_fase3_validation` (runs on tick) when the auto is
  promoted and 10 real trades closed since `promoted_at` - sets real
  window PF, structural splits (paper-to-MT5 with different exit
  or paper still open) and spots with no SL; **auto rollback to
  nova_gestao** (PF < 1.0, >= 2 structural splits or any spot
  with no SL) or **keep**, with feed/Telegram note and verdict
  stored in `fase3_verdict` (config) + block on Phase 3 panel
  (before/after by `snapshot_before`). Runs ONCE per window; reopens
  when the auto is promoted again (new snapshot).
- **Tests**: `test_fase3_eta` (pace -> due date) and
  `test_fase3_validation_rollback` (10 losing trades -> rollback +
  verdict shown on /api/status). **243 tests**. Docs: AGENTS/TECHNICAL/spec.

### 2026-08-18 - Fine trail sweep, ATR summary on panel, promotion snapshot

- **Fine fixed-trail sweep** (`app/sweep_atr.py --trail 1.6,1.8,2.0,2.2,2.4`)
  on escada_3/5 on full history: SMOOTH plateau with no lone spike
  (escada_3: PF 2.37/2.24/2.13/2.04/2.00, +8,618 -> +6,157 EUR, DD 35-42%; on
  the old window all green: 1.55->1.31). **2.0 stays** (inside,
  neighbors within ~5% PF, matches group patience of 1.0-2.3 TP2
  gaps); the best (1.6) sits on grid edge and nears the
  puffed zone (< 1.0). 1.8 stays a candidate if tighter is ever wanted.
- **ATR sweep summary on Phase 3 panel**: "📊 Trail sweep (18/08
  study)" block with baseline vs atr_k (2.13/+7,014 EUR vs 1.42/+2,483 EUR vs 0.64/
  -2,053 EUR) and fine trail - the filed call next to gates.
- **Check-window snapshot at promote**: `POST /api/config` with
  an auto-manage auto writes `fase3_snapshot` in config (paper-run
  stats, account DD by 30d curve, paper-to-MT5 splits,
  closed real trades and SL-audit entry count) - shown on
  `/api/status` and on the panel; the window compares before/after (§B.5.4).
- **241 green tests** (1 new: promote snapshot) · updated docs.

### 2026-08-18 - ATR sweep (rejected), SL risk warn, Phase 3 promote button

- **ATR sensitivity sweep** (`app/sweep_atr.py`): atr_k
  1.0/1.5/2.0/3.0/5.0 x period 7/14/30 on auto_* on 3 windows (current,
  full history, old out-of-sample) - **honest and final
  result: ATR is strictly WORSE than fixed 2.0 trail on ALL
  combos/windows**. `auto_escada_3` on full history: PF 2.13
  baseline (+7,014 EUR · DD 38%) vs atr_k=1.0/p=14 PF 1.42 (+2,483 EUR) and atr_k>=2 PF
  <0.7 (loses); on the old span PF 1.41 baseline vs atr_k=1.0 PF 0.93
  (negative). Why: gold ATR is far bigger than the 8 pip pullback -
  the ATR floor widens trail and blunts profit lock. **`atr_k` stays
  off** (fixed 2.0 trail frozen); code stays on hand.
- **Risk warn from SL audit**: auto-manage MT5 modify
  refused (10016) or broker-rounded (clamped) -> `auto_sl_warn`
  event from `update` -> `risk` note on feed/Telegram (per-trade+leg+target
  dedup - the refuse does not set throttle, the warn does not spam).
  Tests: refuse (1 refused warn with retcode) and clamp (clamped warn, SL
  applied = valid).
- **"⬆️ Promote to main (Phase 3)" button** on the Phase 3 panel: shows
  when gates are green and auto_escada_3 is not main -
  POST /api/config with confirm (like test pages).
- **240 green tests** (1 new: clamped) · updated docs
  (AGENTS/TECHNICAL/spec §B.5.3 items 5 and 7).

### 2026-08-18 - Phase 3: ATR lined on backtest/paper, ready panel, SL audit

- **ATR-adaptive trailing lined on backtest and paper run**
  (same MT5 formula: `trail_dist = max(trail_factor x dist, atr_k x
  ATR)`, no look-ahead - ATR of candles/minutes BEFORE the event):
  `simulate_signal` got `atr_k`/`atr_period` (`_candle_atr` in
  backtest.py), `run_analysis`/`sim_params` take them, and `PaperRace`
  sets a rolling per-trade ATR with minute buckets (`_paper_atr` - the
  current minute never counts for the ATR it uses itself). With no data
  it falls back to fixed trail; `atr_timeframe` stays MT5-only.
- **"🚀 Phase 3 - ready to start" panel** on the main page: checklist
  of spec gates (N>=20 · PF>=1.5 · P&L>=50% · DD<=125% of reference ·
  check window), with tools in one spot - paper-to-MT5 and
  real-vs-backtest split counts, account DD (curve) and the
  SL audit. 60s refresh (not on each snapshot).
- **Audit of the three auto SL levels on MT5**: `_auto_move_sl`
  logs each try (target->valid->applied, clamped, 10009/10016
  retcode) stored in `autosl:<trade_id>` config (lives through
  restarts) - `GET /api/analysis/auto_sl_log` endpoint + table on panel;
  it is what the check window uses to audit refused modifies.
- **239 green tests** (3 new: backtest ATR, paper ATR, auto_sl_log).

### 2026-08-18 - Phase 3: fake-broker E2E test, adaptive ATR, auto paper-to-MT5

- **E2E test of the real `MT5Engine` path with a STATE fake broker**
  (`dry_run=False`): opens the trade with auto_mgmt, TP1 closes by broker
  (exit deal in `history_deals_get`) -> ladder moves SL to the
  TP1 level -> trailing follows the peak -> time stop closes the rest at market.
  The log holds the **real (10009) retcodes** of each taken modify.
- **Fixed bug (three SL levels)**: `_auto_move_sl` wrote SL to DB
  even when the broker refused it (10016) - now SL only turns
  *applied* when the modify confirms (10009/0); `_modify_sl` returns the
  retcode. Own test: 10016 refuse -> whole DB, throttle not set.
- **ATR-adaptive trailing** (spec §B.5.3, post-check upgrade):
  `MT5Provider.copy_rates` (M1/M5 by `copy_rates_from_pos`) + `_atr` on the
  engine; `trail_dist = max(trail_factor x dist, atr_k x ATR)` - MT5 only for
  now (backtest/paper keep fixed trail). `atr_k`/
  `atr_period`/`atr_timeframe` params in the registry and in state `auto`; with no candles
  it falls back to fixed trail. Normalize, ATR and floor/fallback tests.
- **Auto paper vs auto MT5 split report** (SPEC §B.5.4): endpoint
  `GET /api/analysis/paper_vs_real_auto` + "🔄 Auto paper vs Auto
  MT5 (Phase 3)" table on Report - same exit call, price gap (pips),
  timing gap (min) and P&L per signal; empty until Phase 3 starts.
- **`insert_trade` now stores `exit_reason`** (the column was there but
  the INSERT dropped the value - `real_vs_backtest`/paper_vs_real read None).
- **236 green tests** (6 new) · updated docs (AGENTS/TECHNICAL).

### 2026-08-18 - Phase 3: auto-manage code on MT5Engine (off)

- **`MT5Engine` got Phase 3 auto-manage** (spec §B.5), **off**
  by default: `update(now, exit_at_tp, auto)` takes the strategy
  params (`stair_levels`/`stair_from`/`confirm_filter`/`trail_factor`/
  `time_stop_minutes`) but `state.tick` only passes them when the active
  strategy sets them - `nova_gestao` has none (auto_mgmt off), so
  nothing moves until an `auto_*` is promoted. `SimEngine.update` takes the
  kwarg and skips it (on sim/paper auto-manage is `PaperRace`).
- **`_update_real` with the backtest/paper order**: TP_k confirms ->
  **ladder** (`move_leg_sl` of later legs to TP_k LEVEL),
  leg k takes SL -> **confirm filter** (legs > k close at
  market), after TP1 -> **trailing** (`SL = peak - trail_factor x
  dist(entry,TP2)`, peak of PRIOR ticks - no look-ahead) and
  **time stop** (legs N min after TP1 close at market).
- **Three SL levels**: `_valid_sl` caps the theory target by broker
  min gap (`stops_level`/`freeze_level` of the symbol) - a modify
  too near price would refuse (10016). **Modify throttle**:
  only moves if better by >= 1 pip and <= 1x/min per leg (per-trade state in
  memory - after restart it restarts from current price, filed).
- 4 new tests (`test_mt5.py`): ladder moves SL to TP1, 60s throttle,
  trailing follows the peak with no look-ahead (and never drops), valid_sl honors
  stops_level.
- Suite: **230 tests** (+4).

### 2026-08-18 - Clean menu (old-strategy call) + single SPEC + SPEC v3

- **Menu/test pages**: `nova`, `nova_gestao_3` and `tp2_gestao` left
  the menu and test pages (user call - 18/08). Only
  `nova_gestao` stays as reference (compare + paper run).
  `HIDDEN_STRATEGIES` now holds the old ones; `nova` went to
  `MENU_HIDDEN_STRATEGIES` - **still opens paper trades** because the
  "no management" counterfactual of trade history (`_trade_view`) reads its
  paper account (hiding it from the run must not cut that counterfactual).
  Menu now: MAIN · Auto ladder 3 · TP2 + protect · DIAGNOSTICS ·
  HELP. The flow sample (trace) moved from `tp2_gestao` to `tp2_protect`
  (the TP2 page that stays); the "✓ best" marks MAIN when
  `nova_gestao` wins compare.
- **Single SPEC**: `docs/SPEC-paper-fase2.md` was **merged** into
  `docs/SPEC-auto-gestao.md` (Part B: frozen setup, 1.5-2.5
  sensitivity, reviewed bars, check tools, Phase
  3 path) and **erased** - this file is now the only project spec.
- **SPEC v3 (draft, §B.7)**: gates for a real account - approved Phase 3 demo
  first, 3-4 steady weeks, explicit user call, periodic
  reviews (PF >= 1.5 on rolling window, DD <= 125%, daily kill always
  on) and the red line of what never happens on real. No code.
- Suite: **226 tests** (+0 - stored tests were updated for the
  new seen-strategy set).

### 2026-08-18 - SPEC updated before Phase 3 (check)

- **`docs/SPEC-paper-fase2.md`**: state -> Phase 2 running (N=0/20); daily
  kill and 🏁 daily summary filed in frozen setup; lasting
  divergence report (§3.4) and **real vs backtest** (§3.5) as check
  tools; **new §7 - Phase 3 path (MT5 demo)**: pre-reqs
  (§3.2 bars with N>=20), the MT5Engine parts already there
  (`move_leg_sl`/`close_leg_at_market`/`_move_sl_breakeven`), the small
  work left (hook ladder/trailing to `_update_real`, modify
  throttle, three SL levels), 10-trade check window + rollback, and
  the "never real with no explicit instruction" rule.
- **`docs/SPEC-auto-gestao.md`**: state -> Phase 1 done · Phase 2 running;
  `trail_ratio` -> `trail_factor` also on live parts; Phase 2 marked
  (paper port, pages, daily kill, splits); Phase 3 points at the
  v2 path; open asks marked solved (stair_from,
  parallel, trail 2.0) vs open (menu of old ones).

### 2026-08-18 - Daily kill on paper + run daily summary + real vs backtest

- **Daily kill (`daily_stop_pct`) on the paper run**: when stored day
  P&L of a strategy virtual account <= -balancexcap/100, the signal does NOT
  open on paper for that account - exact mirror of production
  (`risk.engine.daily_loss_exceeded`; `apply_daily_stop` on backtest). Fits
  ALL virtual accounts (each can be promoted); the
  `daily_stop_skipped` counter shows per strategy in snapshot.
- **Daily summary with the Phase 2 run**: `daily_summary` (at
  `summary_time`) now has the 🏁 block with balance and PF of
  `auto_escada_3` vs `nova_gestao` and how many signals miss the bars
  (N >= 20 · PF >= 1.5) - feed + Telegram.
- **`GET /api/analysis/real_vs_backtest`** + "⚡ Real vs backtest" table on
  Report: each closed REAL demo trade vs the same-signal backtest on the
  main strategy - entry gap (real price vs sim fill: entry +/-
  spread/2 +/- slippage), exit gap (weighted close vs the TP_k/SL reason
  level), signal-to-open lag (sim takes 60s) and P&L of each side (SPEC v2 §3.4).
- Suite: **226 tests** (+1 paper daily kill, +1 real_vs_backtest).

### 2026-08-18 - Phase 2 live: progress panel, Report splits, CSV, tp2 study, real MT5

- **Paper trades of the strategy + the `nova_gestao` reference** (prices, SL, TPs,
  lots, stamps, reason, P&L) for analysis outside the dashboard.
- **tp2 study (current shape, 30d)**: pure tp2 PF 0.86 / -151 EUR; **tp2 +
  protect PF 2.65 / +1,721 EUR** (protect gets back ~86% of the way to
  full management PF 2.93 / +2,010 EUR); `nova_gestao` PF 3.22 / +2,309 EUR.
  Call: early exit at TP2 only pays WITH group guard, and
  even so full management wins more - `nova_gestao` stays the
  best pick. (Full history: pure tp2 0.50/-3,791 EUR -> +protect
  0.82/-1,422 EUR -> +management 0.89/-834 EUR - the old regime loses on all.)
- Suite: **226 tests** (+1 paper daily kill, +1 real_vs_backtest).

### 2026-08-18 - Phase 2: paper with auto-manage + new layout (nav up, state down)

- **`trail_ratio` -> `trail_factor`** (code, tests and docs): plain
  multiple of the entry-to-TP2 gap (2.0 is about 8 pips on gold) - the name
  mirrors multiply meaning (SPEC v2 §2.1).
- **1.5-2.5 sensitivity sweep** (`auto_escada_3`, SPEC v2 §2.2):
  smooth plateau - PF 1.97-4.69 on current, 1.28-1.57 on old, P&L
  green on all, no lone spike (so no overfit); **2.0
  frozen**. Results stored in spec (§B.2, now `docs/SPEC-auto-gestao.md`).
- **§4 regression tests** (SPEC v2): buy/sell look-ahead (the candle never
  gains from its own high/low), TP/SL on same candle (SL first),
  many TPs on same candle, ladder vs trailing (ladder leads),
  per-signal risk (2/3/5 legs risk the same total) + 2 paper-tick
  tests (look-ahead-free trail, ladder at TP1 level).
- **Auto-manage moved to the `PaperRace` tick**: ladder, trailing,
  confirm and time stop working live on the paper run (backtest
  mirror, no look-ahead; peak state in memory, restarts from current
  price after reboot).
- **`auto_escada_3` and `tp2_protect` left `HIDDEN`** (Phase 2): test
  page + menu + paper run (`#a3e635`/`#10b981` colors); the rest
  (`auto_2/3/5`, `auto_escada_5`, `tp2`) stays hidden.
- **Divergence report**: when the auto and the reference (`nova_gestao`)
  close different on the same signal - or the reference closes by group
  management and the auto stays on - the bot sends a ⚖️ `divergence` note on
  feed (+ Telegram if set), one-shot dedup per signal. It is the SPEC
  v2 §3.4 paper-vs-backtest compare, automated.
- **New layout**: nav moved to a **fixed top bar** (flat
  items) and the state terminal (`▚ STATE`: badges, prices with
  **day-%-move mini-bar** + tooltip, clock, kill switch, Exit)
  moved to a **fixed bottom bar** - no side terminals (content
  lives between the two, `--nav-h`/`--foot-h`, never covered at
  any zoom; <=960px ☰/▦ open as overlay). On all 4 pages
  (index/test/diagnostics/help).
- Suite: **223 tests** (was 214 - +6 §4 backtest, +3 paper tick/divergence,
  +1 registry, and updated `test_strategy` for Phase 2).

### 2026-08-18 - auto-manage: Phase 0.5 + Phase 1 (tests, registry, mining, sweep)

- **Auto-manage regression tests** (6 new in `test_backtest.py`):
  ladder closes in profit at TP level (buy and sell), ladder moves SL to
  TP2, confirm filter closes far legs at market, trailing follows
  the peak, time stop closes after N minutes.
- **Fixed bug (big)**: the outcome of a leg closing on the
  ladder step (above entry, in profit) was marked `loss` -
  now `win`. And **trailing had look-ahead**: SL was raised with the
  high of the same candle crossing it (hopeful fills puffing
  backtest - whipsaws turned into profit). Now SL uses the peak of
  prior candles. With no such fix, the old span (where all loses) gave
  PF 2.
- **Registry**: `app/strategies.py` got the 5 `auto_*` variants (HIDDEN
  until approved) with `stair_levels`/`stair_from`/`confirm_filter`/
  `trail_ratio`/`time_stop_minutes` in `_strat`/`sim_params`; `run_analysis`
  and `run_month_sim` pass the new params + `until` (out-of-sample).
- **`app/mine_management.py`** - trial mining: lines real group
  management messages with price action -> the group bears 1.0-2.3
  TP2 gaps of peak pullback and waits p90 about 15h after TP1.
- **`app/sweep_auto.py`** - sweep of the 5 variants x grid on 30 days + the
  old span (out-of-sample), with `nova_gestao` as reference
  (checked: PF 3.28 / +2,369 EUR is about the dashboard A/B/C). Result: ladder/
  confirm/time stop with no edge alone; trailing is the path
  (trail_ratio 2.0 = honest value); `auto_escada_3` advised (PF 3.11
  current / 1.41 old vs reference 3.28 / 0.63). See `docs/SPEC-auto-gestao.md` 6.4.
- Suite: 214 tests (was 207 - +6 backtest, +1 auto-registry API).

### 2026-08-18 - dashboard split into split HTML pages + boot only on first visit

- **Dashboard split** (`index.html` was ~3100 lines): CSS pulled
  to `app/static/style.css` and shared JS (helpers, boot, WebSocket, global
  chrome - badges, statusbar, terminals, toasts, menu) to `app/static/common.js`.
- **New pages served by `main.py`** (all with middleware auth,
  no session -> login): `index.html` (`/`), `teste.html` (`/teste?s=<id>`),
  `diagnostico.html` (`/diagnostico`) and `help.html` (`/help`). The menu now
  uses real `<a href>` links instead of buttons - each nav has its own URL.
- **Boot move only on first visit**: the "INIT PROJECT NEXGEN..."
  overlay ran on all pages on each page swap; now it runs ONCE per
  session (`sessionStorage.nexgenBootDone`), right after login -> index, and is
  cleared on logout to move again at next login.
- Fix: the test-page "Start balance" field was `invalid`
  (`step=500` with `min=100` -> 1000 was never a multiple) - now `step=any`.
- Suite: 207 tests (was 206 - +1 split-page contract in
  `tests/test_frontend.py`).

### 2026-08-18 - MT5 demo account swap + old history clean

- **New demo account**: `MT5_LOGIN=5054644473` (MetaQuotes-Demo, password in
  `.env`, backup in `backups/env-20260818-204851.bak`) replaces
  5054586550. Server restarted with `MT5_DRY_RUN=0`; `/api/status`
  confirms `login: 5054644473`, `mode: mt5-real`, 1000 EUR balance.
- **DB clean** (backup `backups/signalbot-20260818-205233-pre-account-switch-clean.db`):
  erased the 5 closed trades + 21 spots of the old account (dashboard
  starts clean on the new account), the 71 `__real__` points of
  `paper_balance_history` (real balance of the old account on the parallel
  run - the series restarts on the new balance) and the 2 orphan `executed`
  signals (4167/4170) turned `historical`.
- ⚠️ Terminal MT5 AutoTrading stays OFF after an account swap - turn on the
  "Algo Trading" button on the terminal (confirmed `trade_allowed: true`).

### 2026-08-18 - dashboard visual overhaul (polish + zoom)

- Spacing/radius **design tokens** (`--sp-1..6`, `--r-sm/md/lg`)
  on base parts (wrap, stats, cards, controls, statusbar) -
  steady spacing pace over the whole layout.
- **Zoom/small screens**: the header (`h1` title + sub) no longer
  breaks to many lines (nowrap; sub hides <=480px, badges
  already show mode/strategy/run); the **parallel run** stacks the
  canvas + legend upright <=700px instead of squeezing (and the legend
  got its own desktop `min-width`).
- **Sticky toasts** (e.g. "AutoTrading OFF") got a ×
  dismiss button with 5 min snooze (back if the issue stays) and, on
  phone, move from a half-screen box to a thin bottom strip.
- Visual clean: subtler CRT scanlines (0.04 -> 0.028) and dimmer back
  reticle (0.13 -> 0.10) - cyberpunk stays, less noise.
- Bug fix: missing `var(--fg)` on the run-view picker
  (text color stayed unset) -> `var(--text)`.

### 2026-08-18 - HELP page filters hidden strategies

- The central registry now tags each strategy with the **`hidden`** flag
  (`strategies_list()` - tp2/tp2_protect = True) and the **HELP** page filters
  `!s.hidden` - it no longer shows strategies already off the menu and
  test pages (which stay only on historic backtest/compare).
- `test_strategies_registry_endpoint` + `test_help_filtra_estrategias_ocultas`
  hold the contract. **206 green tests** (was 205).

### 2026-08-18 - parallel run: view stays in config

- The parallel-run view picker (24h/7d/30d) now **stays in
  config** (`paper_race_hours`, default 24, checked 24..720) like
  `teste_days` - lives through dashboard reloads; boot syncs the
  picker from config and onchange writes + reloads.
- `test_config_persists_paper_race_hours` (API) + static contract on
  frontend. **205 green tests** (was 204).

### 2026-08-18 - parallel run: view picker (24h/7d/30d) + per-strategy stats

- **View picker**: the "🏁 Parallel run" mini-curve got a
  `<select>` (24h / 7 days / 30 days) - `loadPaperRaceMini` reads it and passes
  `hours` to `/api/paper/history`; backend now takes up to **720h** (was
  168h = 7 days).
- **Legend stats**: each strategy now shows **closed trades + win
  rate** (from `paper_trade_stats`) next to balance; the real
  reference line (⚡ real MT5) stays with no stats (it is only the serious
  money).
- `tests/test_frontend.py` got the picker/stats contract. **204 green
  tests** (was 203).

### 2026-08-18 - parallel run: main-account test + real reference line

- **Check**: `test_paper_main_account_same_signal_id_open_close` makes
  sure the main paper account opens and closes the SAME signal trade with the
  same `signal_id` as test strategies (and that `close_all` closes the
  main paper one, with `exit_reason` stored) - the parallel run never
  splits from real.
- **Real reference line**: besides the 4 virtual accounts, the
  "🏁 Parallel run" mini-curve now shows **real MT5 balance** as a split
  line (light dash, "⚡ real MT5 · real account" tag) - the tick
  writes real balance under the reserved `__real__` strategy in
  `paper_balance_history` and `GET /api/paper/history` returns it with
  `is_real: true`; the dashboard tells it from virtual ones (`is_real`).
- **203 green tests** (was 201; +1 API +1 frontend).

### 2026-08-18 - parallel run: main gets its own virtual account (1000 EUR)

- **Bug**: on the "🏁 Parallel run" mini-curve, the active-strategy box
  ("TP1..TP5 + management 🧠") showed the **real MT5 account balance** (973.49 EUR)
  instead of the test account - while the other 3 boxes showed their
  1000 EUR virtual account. Compare was apples-to-oranges and the
  main curve jumped when real balance moved.
- **Fix**: `open_for_signal` no longer skips main - now **all**
  seen strategies (with the active one) open the same paper trade
  with their own 1000 EUR virtual account. `_record_balance_snapshots` and
  `paper_status` use main virtual account (not real balance), with
  `ensure_paper_account` making missing accounts.
- **Steady lead alert**: `_check_paper_race_leader` and
  `renderPaperRaceWarn` now compare virtual vs virtual (main
  now also has a virtual account) instead of virtual vs real balance.
- **Clean data**: erased the ~1200 `paper_balance_history` rows of
  main that stored real balances (backup `signalbot-*-pre-main-paper-
  account.db`) so the curve restarts at 1000 EUR. 201 green tests.

### 2026-08-18 - fixed suite: per-test split + polling (flakiness over)

- **Flakiness cause**: `test_api.py` tests share one
  `TestClient`/state (module-scoped fixture) and used `time.sleep(1.3)`
  racing the 1s back loop (edit window) - under load the tick
  lagged and the test failed at random; a mid-fail left
  changed setup (edit_window=0, mode=auto) and tainted the next ones.
- **Per-test split**: new autouse `_isolated` fixture in `test_api.py`
  - brings back base setup (manual mode, edit_window 60, max_positions 5,
  risk_pct 2, nova_gestao strategy, killswitch off) and closes open trades
  BEFORE each test, even if the prior one failed (no chain).
- **Polling not sleeps**: `_wait_for()` + `_sig_status()` helper - the
  4 edit-window tests moved from `time.sleep(1.3)` to `/api/status` polling
  (no race with the tick).
- **Set asserts not passing counts**: the snapshot only
  shows OPEN legs and the frozen-market tick at once closes legs
  whose SL moved to/above entry (`protect`/`out at BE`/`TP1
  HIT`); management tests now read the DB (`db.positions_for_trade`)
  and assert the set fact (leg 1 closed with reason + moved SL) instead of
  open-leg count.
- **Paper run**: `_paper_trade_for_signal` got `include_closed=True`
  for the protect test - the paper trade may already be closed by the
  tick when the test reads it.
- `tests/test_frontend.py` got the static e2e of the test-page flow
  (loadTeste -> month with testeSimQS + walkforward + risk/balance picker
  onchange). **201 green tests** (was 200).

### 2026-08-18 - Edge tab cut; walk-forward + risk studies on test pages

- **The "🎯 Edge" tab is gone** from the menu (button + card + dead JS): the
  sim/compare/monthly and per-leg P&L/parallel run were already
  copied on test pages, and walk-forward already had an automatic
  stand-in on the regime alert.
- **Walk-forward** (`/api/analysis/walkforward`) moved to **test
  pages** ("🧪 Walk-forward" block).
- **Risk studies** (risk %/balance pickers) moved to test pages:
  default balance is **1000** (the paper account own balance - never the
  MT5 balance), by `testeSimQS()`; values stay in `edge_risk`/
  `edge_bal` as before.
- `tests/test_frontend.py` rewritten (4 tests): calls to
  `/api/analysis/month` must use `testeSimQS()`, balance default is
  1000, the Edge tab must not return and walk-forward/pickers must exist
  on the test page. 200 green tests.

### 2026-08-18 - test pages use own balance (1,000 EUR) + contract checks

- **UI fix**: test/HELP pages showed the sim starting with
  the MT5 account balance (e.g. 973.49 EUR) instead of each
  strategy own balance (1000 EUR) - `/api/analysis/month` used `balance=0` =
  current balance. Now test-page calls pass `balance=1000`
  (in line with the 1000 EUR paper account and the "starts with 1,000 EUR" intro).
  The Edge tab keeps `balance=0` (current balance / picker).
- **Clean**: erased ghost trade `t-5659efb8fbb6` (signal 4159, 10027
  retcode - opened/closed in the same second with 0 pnl) + the 4 zero spots -
  signal 4159 stays `failed` in history.
- +2 regression tests: silver trade (XAGUSD) uses 5000 contract, and an
  end-to-end paper-run test that fails if virtual balance puffs. 198
  green tests.

### 2026-08-18 - frontend checks (test pages vs Edge tab)

- New `tests/test_frontend.py` (2 static tests on dashboard HTML):
  makes sure **all** calls to `/api/analysis/month` from test/HELP
  pages carry `balance=1000` (the "start balance = MT5 balance" bug
  never returns) and the Edge tab keeps the balance picker (`edgeSimQS`)
  instead of a fixed 1000. 198 green tests.

### 2026-08-18 - paper-run P&L fix (per-symbol contract)

- **Bug**: SimEngine and the paper run set P&L of ALL trades
  with `CONTRACT_SIZE=100000` (forex). On XAUUSD (100 contract) that gave 1000x
  puffed P&L - `nova`/`tp2_gestao`/`nova_gestao_3` paper accounts
  showed 66k/38k/28k instead of ~1000 EUR. E.g. a 3 USD TP1 x 0.01 lots
  gave 3000 EUR instead of 3 EUR.
- **Fix**: `engine._contract(symbol)`/`_pip_size(symbol)` (XAUUSD 100,
  XAGUSD 5000, forex 100000) shared with `paper.py` - P&L and
  sizing of replayed/paper trades now use the symbol
  contract, like backtest and the MT5 engine.
- **Clean**: paper accounts back to 1000 EUR and test-strategy
  trades/spots/balance history wiped (main `nova_gestao` uses
  real balance and was not touched). Prior backup in
  `backups/signalbot-*pre-paperfix-reclassify.db`.
- +2 regression tests (per-symbol contract + unpuffed gold trade).
  194 green tests.

### 2026-08-18 - longer management classifier + mass reclass

- New management shapes known (before they went `failed`):
  `Close TP2..TP6 here/when happy` -> `close_tp` (closes only leg N),
  `TP N SMASHED/KISSED/tapped` -> `tp_hit`, `Out at entry/breakeven` /
  `Close this at breakeven` -> `close_all` (BE), `SL adjusted/edited/hit` ->
  `move_sl`/`sl_hit`, `Set breakeven` -> `protect`.
- **Mass reclass in DB**: of 465 `failed` messages, 61 turned
  `management` (then set `historical` by replay), 100
  chats moved to `ignored_messages`, and 304 real signals with
  parse error (typos, Buy/Sell Stop 💣, re-entry zones) stay
  `failed`. No erased signal had a linked trade (pre-check).
- Tests vs the real DB in `tests/test_management.py`; suite at 194.

### 2026-08-18 - history: msgs 4157/4169 reclassed as chat

- The "Again act to fast after TP1 / So I'll registered that as SL"
  (4169) and "Okay team… Gold is currently moving sideways" (4157) messages
  were reclassed in DB from `failed` to **skipped** ("🚫
  Ignored" audit) - prior backup in `backups/signalbot-*pre-reclassify.db`.

### 2026-08-18 - full signal runs now + 8s window + loose "TP1/SL" chat fix

- **Run on first valid parse**: a full Telegram signal (with
  entry+TP+SL) runs at once in auto (or stays pending in manual),
  with no wait for the edit window - closes the "TP1 came in seconds"
  case (the 14:33 signal waited 81s and entered 41s after TP1 HIT). The window
  is only used when the first version is incomplete.
- **6s -> 8s edit window** (only for incomplete messages; each edit
  turns the clock back to 8s).
- **Match fix**: signal details (TP/SL/entry/target) now only
  count as a signal with a **number** after - "after TP1"/"as SL"
  (group chat) no longer turn `failed` and go to audit as
  skipped. Real case: "Again act to fast after TP1 / So I'll registered
  that as SL" (msg 4169).
- 186 green tests.

### 2026-08-18 - Telegram edit window cut to 6s

- The group-edit wait went from **30s to 6s** (config
  `edit_window_seconds`, default in `config.py` + DB value updated
  to 6). The dashboard slider now goes down to 1s (was 10s
  min). Updated docs (AGENTS.md, TECHNICAL.md, PUBLIC.md).

### 2026-08-18 - DB check + listener UTC turn + AutoTrading OFF alert

- **DB stamps** (`tests/test_timestamps.py`): besides in-memory
  makers, the check now reads **stored rows** -
  `signals.received_at`/`edit_deadline`, `trades.opened_at` and
  `paper_balance_history.ts` must parse with plain UTC zone (+00:00).
- **Listener `msg.date` turn** (`app/telegram_listener.py`): pulled
  to `msg_date_utc()` (naive local -> UTC) + tests in `test_listener.py`
  (right turn and empty when no `date`).
- **At-once 10027 retcode alert** (`app/execution/mt5.py` + `state.py`):
  `_send_open_orders` gathers the real retcodes of refused legs; when
  10027 (AutoTrading OFF) the trade goes `auto_trading_off=True` and
  `_try_execute` sends its own risk alert ("⛔ AutoTrading OFF...")
  instead of only the log. `Trade` got `retcodes`/`auto_trading_off`
  (passing, not stored). Tests in `test_mt5.py` (real-mode with
  fake `_mt5`) and `test_api.py` (note by approve).
- 185 green tests.

### 2026-08-18 - check: plain UTC stamps

- New `tests/test_timestamps.py` (5 tests) that makes sure **all**
  stamp makers send plain UTC (`+00:00`) - `_now_iso`
  (engine), `_now_iso_plus` (edit_deadline), `_now_iso_short` (listener
  heartbeat) - and covers the real flow: an API-ingested signal stores
  `edit_deadline` with UTC zone and the diagnostics `last_seen`/msg age
  arrive in UTC. Blocks the puffed-countdown ("3000s") regression.
- 178 green tests.

### 2026-08-18 - unrun-signal fix (AutoTrading off) + UTC stamps + pending_management.sl migrate

- **Why the signal did not enter (15:32)**: the broker refused the order with
  10027 retcode = AutoTrading off at run time (15:34). The user
  turned AutoTrading on after; `trade_allowed: true` confirmed live.
- **Plain UTC stamps** (`+00:00`): `_now_iso`/`_now_iso_plus` (engine/state),
  listener (`_now_iso_short`, `msg.date`), paper accounts (`db.py`) and `since`
  of paper history (`main.py`). Before they were naive local - the browser (own
  local time apart from VPS) read `edit_deadline` as its own time and the
  edit-window count showed puffed (~3000s instead of 30s).
- **`pending_management.sl` migrate** (`db.py::_migrate`): the table was born
  with no `sl` column; any management note with a still-in-window target
  (protect/close_all/move_sl) broke with `no column named sl` -> 500 and the
  note was lost (8x 500 today). Column auto-added at
  start (confirmed live).
- Time compares now bear naive and aware (`astimezone()` when
  `tzinfo is None`) - `listener_status`, uptime, `_iso_age_seconds`.
- 173 green tests.

### 2026-08-18 - tp2/tp2_protect off the paper run + Promote button + moving risk

- **`tp2`/`tp2_protect` no longer run on paper**: `paper.open_for_signal`
  and `paper.tick` skip `HIDDEN_STRATEGIES`, and `paper_status`/balance
  snapshots do not show them - they stay only on historic backtest/compare. (No old paper
  data to clean - the accounts were empty.)
- **"⬆️ Promote" button on the feed**: the switch-hint note carries
  `action="promote:<id>"` (new `action` column on `notifications`, auto
  migrate). `Notifier.send` takes `action` and the dashboard draws the
  button with one click doing `POST /api/config {strategy}` and back to
  main.
- **Moving risk**: base `risk_pct` dropped to **2.5%** (default + DB value
  updated from 5.0). `state._effective_risk_pct()` scales risk
  down after losses: each **-20% drawdown** from equity peak
  (`peak_balance`, updated on tick) halves risk
  (`0.5 ** (dd/20)`, 0.1% floor). `_try_execute` uses working risk (real +
  paper) and the snapshot shows `effective_risk_pct`/`peak_balance`.
- **Tests**: `test_dynamic_risk_scales_down_after_drawdown` + action on
  `test_paper_race_switch_suggestion_filters_hidden_and_is_consistent`;
  updated `test_paper_forward_opens_on_execution`,
  `test_paper_applies_group_protect` and the old
  `test_paper_ignores_close_all_when_not_in_manage_kinds` (now
  `test_paper_close_all_applies_only_to_managed_visible`). Suite:
  **173 green tests**.

### 2026-08-18 - Cut of no-edge strategies + regime auto-switch

- **`HIDDEN_STRATEGIES = {"tp2", "tp2_protect"}`** in `app/strategies.py`:
  the two TP2 variants (no edge on backtest - PF < 1 on full history)
  left the **menu/test pages** (`index.html`: buttons, `TERM_ORDER`,
  `BEST_PAGE`, HELP), but stay in the registry for backtest/compare and the
  paper run. The menu goes from 6 to 4 sim pages (the sample
  trace moved to show on the `tp2_gestao` page).
- **Fixed `_check_regime`**: it passed `manage` but not `manage_kinds` to
  `run_analysis` - a "protect only" strategy regime sim applied
  full management (wrong). Now it also passes `manage_kinds`.
- **Regime auto-switch hint** (`state._check_paper_race_leader`):
  when a **seen** strategy stays ahead of main for
  >= 30 min with >= 3 closed trades, the bot sends a `race` note of
  "🔄 Switch hint" (feed + Telegram) - it never switches alone; the
  user promotes with the "⬆️ Promote" button. `HIDDEN_STRATEGIES` are never
  hinted. Also fixed the stored `NameError: name 'cur' is not defined`
  in that note (added `cur()` helper).
- **Tests**: `test_hidden_strategies_kept_in_registry_but_marked_hidden` +
  `test_paper_race_switch_suggestion_filters_hidden_and_is_consistent`.
  Suite: **172 green tests**.

### 2026-08-18 - Ghost-spot cleanup + MT5 auto-start + auto reconnect

- **Close of the 12 copied spots** of signal `tg-3948662131-4146`
  (3 ghost trades). Checked live: the broker held **0 spots, 0
  deals, 0 orders and exactly 1000 EUR balance** - the spots only lived in
  DB (made during the None-tick crash) and never hit MetaTrader.
  The 3 trades/12 spots were closed in DB with `exit_reason="phantom"`
  (backup `signalbot.db.bak-*-pre-phantom-cleanup` before). Dashboard back
  to `open_trades: 0`.
- **`boot.ps1` now starts MetaTrader 5** (`terminal64.exe`, idempotent)
  between Caddy and `start.ps1`, so the terminal is up after reboot.
- **Server reconnects to MT5 alone**: short retry on `lifespan`
  (to ~30s, for the still-starting terminal) + 10s-throttle reconnect on
  `market_loop` (if the terminal drops/starts later).
- **DEPLOY.md**: new **Windows auto-login** part (`Winlogon` registry)
  so the `NexGen Boot` task fires with no touch after reboot.
- Suite: **170 green tests**.

### 2026-08-18 - Run fix (None-tick + max_positions 5) + idempotent start + reboot + backup

- **10:00-13:05 diagnosis**: two XAUUSD signals (TP1..TP4) stayed
  `rejected` with "max open spots hit (3)". Root cause: the
  `symbol_info_tick` returned `None` on the 1st order of the symbol (outside
  Market Watch) -> `'NoneType' object has no attribute 'ask'` -> the signal stayed
  `awaiting_edit` and the tick reopened the trade each second (3 copies =
  12 spots for 1 signal) until the `max_positions=3` cap stopped it.
- **`max_positions` 3 -> 5** (default in `config.py` + stored DB value)
  - the 5-leg strategy needs up to 5 entries.
- **`MT5Engine._send_open_orders`**: `ensure_symbol` now runs BEFORE
  sending orders; a None/zero tick only fails the leg (clean rollback when all
  fail). **`state._try_execute`** finalizes the signal as `failed` on any
  `open_trade` exception (never stays `awaiting_edit` repeating).
- **Idempotent `start.ps1`** (pure ASCII, `return` not `exit`): spots
  the server (port 8000) and listener (process) and launches no copies; the
  empty `$env:PORT=0/` falls to 8000.
- **`_listener_running_anywhere` watchdog**: no longer uses `wmic` (gone
  on fresh Windows - missing on this VPS) and now uses `Get-CimInstance`.
  It was the root cause of copies: with missing `wmic` the func always
  returned False and the watchdog (with the `pytest` test one) spawned
  extra listeners.
- **VPS reboot**: `boot.ps1` (Caddy + start.ps1) + **`NexGen Boot`**
  Scheduled Task (AtLogOn).
- **Daily backup**: `backup_db.py` (steady copy by sqlite3 backup
  API, 10-deep turn, `sendDocument` send to Telegram when the bot
  is set) + **`NexGen DB Backup`** task (daily 06:30).
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are empty in `.env` (local-only
  backup until filled).
- **New test**: `test_four_leg_signal_not_blocked_by_max_positions`
  (regression of the real case). Suite: **170 green tests**.

### 2026-08-18 - UI reshuffle: header badges + right account/session terminal

- **Header badges** (`hdr-badges`, right of brand/sub): mode
  (auto/manual/paused), active strategy (stacked name + params), real MT5
  (account · balance) and real/dry-run run (plus the PAUSED `killBadge`).
  Left the STATE block of the left menu.
- **Left menu** (after HELP): the `termSide` terminal now only holds the
  price list with day % move (header renamed to ▚ MARKET).
- **Right `termRight` terminal** (`--term-r`): online users +
  clock + kill switch + Exit, with its own ‹ fold button
  (`body.term-r-collapsed` shrinks to 38px). On phone it opens as overlay
  by the ▦ button (header back with ☰ + brand + badges + ▦).
- Content inset from both terminals (`--term-w`/`--term-r`).
- **`test_paper_history_endpoint_returns_series` test**: no longer uses
  hardcoded 2026-08-17 stamps (past 24h the series went
  empty on the `hours=24` filter) - now relative times (`now - N min`).
- 169 green tests.

### 2026-08-18 - VPS start (Kamatera, Windows) - Python + venv + server + listener

- **VPS deploy**: the project moved to the Kamatera VPS (Windows) for
  30 test days. The venv copied from the local PC was broken
  (`pyvenv.cfg` pointed at `C:\Python314`, missing on the VPS) - installed
  **Python 3.14.7** (winget), rebuilt `.venv`, `pip install -r
  requirements.txt`. Server + listener up at `127.0.0.1:8000`,
  `mt5-real` run on demo (1000 EUR balance), `nova_gestao` strategy.
- **`requirements.txt`**: added **`MetaTrader5`** (missing - the fresh
  deploy installed all but the package linking the terminal; noted in
  DEPLOY.md).
- **`test_report_includes_by_day` test** no longer hangs on the last
  `by_day` day (it failed when prior tests closed trades with today
  date and 0 pnl - the stored-trade day is now found by date).
- **Public site**: Caddy at `C:\caddy` (v2.11) reverse-proxying
  `127.0.0.1:8000`; ports 80/443 open on Windows firewall (`NexGen HTTP 80`/
  `NexGen HTTPS 443` rules). First by IP with self-signed
  HTTPS (`tls internal`) then moved to the free
  **n3xg3n.duckdns.org** domain with automatic Let's Encrypt HTTPS (green
  lock). IP-update script at `C:\caddy\duckdns-update.ps1` (token
  filled, daily `NexGen DuckDNS Update` Scheduled Task).
- **Management as typed events**: strategy registry moves from
  `manage: bool` to **`manage_kinds: set`** (which management kinds each
  strategy honors) + new **`tp2_protect`** variant ("Exit at TP2 +
  protect only" - skips close_all/close_tp1/move_sl) with test page
  and paper run.
- **Fixed paper run**: group management (protect/close_all/
  close_tp1/move_sl/tp_hit) now applies to paper trades of
  strategies honoring it (before "+ management" accounts behaved like
  no-management ones until price hit SL/TP) - linked by the target
  real-trade signal_id.
- **Exit credit**: `exit_reason` + `exit_msg_id` on legs/trades
  (sl/tpN/protect/close_all/close_tp1/move_sl/...); the dashboard trades
  table shows the close reason and the **"no management"
  counterfactual** (P&L of the `nova` paper variant for the same signal). Auto
  DB migrate (ALTER TABLE), `signalbot.db.bak-*-pre-gestao` backup.
- 169 green tests (5 new: paper-run management x2, exit_reason,
  manage_kinds filter on real account and backtest).
- **UI reshuffle reverted** (the user liked the prior style):
  the menu went back to the **fixed left column** and the state
  terminal to the **fixed right column** (first layout brought back
  from the `Project NexGen.zip` zip on the desktop), with keyboard
  nav (up/down/Enter) and `term-r-collapsed` fold (38px). The
  new features stay: `tp2_protect` page on menu and the
  "Exit / no management" column on trades.
- **STATE terminal moved to the menu**: the right-terminal facts
  (auto/strategy/MT5/market badges, prices with day % move,
  users + clock, kill switch, Exit) moved to a
  **terminal-style block under the nav menu (after HELP)** - the right
  column is gone (`--term-r` cut, content only with left
  inset) and the header lost the ▦ button (STATE opens with ☰ on phone).
  Fold ‹ now only hides the STATE block (the ▚ STATE header stays +
  ›); the keyboard hint sits static between HELP and STATE.

### 2026-08-17 - Day % move on STATE terminal + fold (‹) button + TP2-exit study

- **STATE terminal**: each price now shows the **day % move** next
  to the value (green/red) - from Yahoo `chartPreviousClose`
  (`YahooProvider.changes` -> `market.changes` in snapshot and
  `/api/diagnostics`); with no data (e.g. sim) nothing shows.
- **Fold/unfold**: new **‹ ›** button on the right-terminal header
  - folds to a 38px bar and page content frees the room
  (by `body.term-r-collapsed` -> `--term-r`); choice stored in
  localStorage; on phone state always opens full.
- **Study (no code)**: 3 TP2-exit variants on full history
  (576 signals, 1h, 5% risk, 6% kill) - see analysis answer: early exit
  does NOT pay; management (protect ~80% of the gain) is what saves.
- 164 green tests.

### 2026-08-17 - All-zoom responsive layout + right state terminal

- **Right `termSide` terminal**: facts that lived in the header
  (auto/strategy/MT5/market badges, upright GOLD..NZDUSD price list,
  users + clock, kill switch and Exit) moved to a fixed
  right terminal (same look as the left menu) - the header keeps only
  ☰ + brand + ▦. <=960px it opens as overlay by ▦; element ids did not
  move (zero logic JS changed, only DOM spot).
- **Browser zoom**: at >=961px content steps back from both terminals
  (`--term-w`/`--term-r` shared by CSS vars) - never
  covered at any zoom; 961-1120px terminals shrink (width +
  type); heights < 640px the menu goes `flex-start` + scroll (the
  `justify-content: center` cut items with no way to reach them); menu
  items wrap (`white-space: normal`) instead of cutting.
- **Flat overflow gone**: tabs and `.scroll`/tables (Positions and
  Report set, now wrapped) got `overflow-x: auto`; the
  login uses `width:min(400px,92vw)`. Checked at 350px viewport
  (docW == viewport) and on desktop.
- 164 green tests (no Python changes).

### 2026-08-17 - TP2 rename + "Exit at TP2 + management" strategy + flow sample + HELP page

- **Rename**: strategy `tp2` is now called **"TP1->BE + exit at
  TP2"** (id stays) - the old name hid that SL and BE are always
  there; docs and tests updated.
- **New `tp2_gestao` strategy** ("Exit at TP2 + management"): `exit_at_tp=2`
  with `manage=True` - `simulate_signal_managed` now honors
  `exit_at_tp` (closes all at TP2, even before group management closes);
  5th test page + paper run (orange).
- **Real-trade flow sample** on the TP2 page: new
  `GET /api/analysis/trace` endpoint (replays the newest solved signal with
  `trace=True` - `simulate_signal` returns time-ordered SL/TP1/BE/
  close `events` + candle `path`) and the page draws the chart (entry/SL/TP1/
  TP2 + markers) and the event timeline.
- **HELP page** on menu (under Diagnostics): live cards per
  strategy (params + how it works + real 30-day numbers) and
  concept glossary.
- **Ticker**: symbol names and box outlines are no longer
  green/red (now neutral: blue/amber) - green/red values
  (direction) jump out more.
- 164 green tests (3 new: managed exit_at_tp, trace, trace endpoint).

### 2026-08-17 - Daily kill on backtest + lined risk setup + 4th strategy + regime alert

- **A - setup**: `daily_loss_limit_pct` went from 3% to **6%** (5%
  risk): before, any 1st day loss (~4% with the min-lot floor)
  fired auto kill and cut good days. Sweep on current-shape history:
  3% cap = +2730 P&L (skips 19 trades), 6% = **+3247**
  (skips 10), 8% = +3396 but with bigger DD (28% vs 24.7%).
- **B - daily kill on backtest**: `apply_daily_stop()` in backtest.py +
  `daily_stop_pct` in `run_analysis`/`run_compare`/`run_month_sim` (None =
  config `daily_loss_limit_pct`, 0 = off) - post-cap day trades
  go `outcome=skip`/0 pnl (`daily_stop_skipped` in payload). The
  dashboard numbers mirror the real kill.
- **C - 4th `nova_gestao_3` strategy** (TP1..TP3 + management): test
  page + paper run (cyan); `simulate_signal_managed` honors
  `max_legs`. Safe vs regime change (legs 4-5 were
  fatal on the old regime).
- **D - regime alert**: `_check_regime()` on tick (each ~30 min)
  runs the rolling 30d sim of the active strategy with daily kill; PF < 1
  with >= 10 trades -> `risk` note (feed + Telegram, 6h cooldown);
  snapshot shows `regime`.
- **Risk studies**: `/api/analysis`, `/compare`, `/month` take
  `risk_pct` and `balance` (0 = config/current) - "Sim risk" pickers +
  balance on the Edge tab (stored in `edge_risk`/`edge_bal`). Find:
  with 1000 EUR balance the min-lot floor rules (<= ~4% risks same);
  PF does not move with risk (10,000 EUR study: PF ~4.4 at 0.5-10%).
- 161 green tests (3 new: apply_daily_stop x 2, risk/balance params).

### 2026-08-17 - Run: dashed main · direction ticker · shaped Telegram

- **Parallel-run curve**: the **main (real account)** line
  now draws **dashed and thicker** (width 3, 8/4 dash) and with the
  bigger % tag - stands out at once from test lines
  (solid).
- **Price ticker**: the price paints **green/red by
  move vs the prior value** (`.p.up`/`.p.down` class with glow) -
  live move; 1st read and same prices stay neutral.
- **Telegram**: `Notifier.send` got optional `tg_text`/`parse_mode` -
  the feed stores clean text (no markdown) and the Telegram push uses
  bold shaped text when given. The run (🏁) alert now sends
  a Markdown version with **bold** values and split
  lines - in line with the emojis of other messages.
- 158 green tests; server restarted (notifier/state moved).

### 2026-08-17 - Lighter purple on nova_gestao curves + purple run alert

- The nova_gestao strategy curves (parallel run and A/B/C) now use
  a **slightly lighter** purple (`#9A4FE8`) for contrast with the
  dark back next to blue (nova) and amber (tp2) - the site accent
  stays `#8437C9`.
- The run lead-flip alert (`_check_paper_race_leader`) now
  sends a **`race`**-kind note (was `risk`/amber): 🏁 icon
  purple on feed, "🏁 Race" group, with the same Telegram push.
  The header warn badge stays red (stands out).
- 158 green tests; server restarted (state.py moved).

### 2026-08-17 - Darker purple palette

- The pink/purple site colors (both-theme `--violet` accent, badges,
  active tab, menu, glows, nova_gestao curve colors on charts
  and the decorative terminal pink) moved from `#8b5cf6`/`#c449ff`/`#ff4d6d`
  to a darker, calmer purple (**`#8437C9`**, with glow `rgba`
  updated to the new RGB) - user asked, slightly lighter
  than the first `#742CAB` by ask.
- Static HTML only - no server restart (reload with Ctrl+F5).

### 2026-08-17 - Shared span picker + per-leg P&L on "from start"

- The "📜 From start" block span picker (all history /
  current shape only since 28/07 / current month only - moving) is now
  **shared** between test pages and the Edge-tab A/B/C and stays
  **stored in config** (`all_period`, like `teste_days`).
- Each backtest payload now returns **`pnl_by_leg`** (P&L per leg
  leg1..leg5, only solved trades) and test pages show the
  "🧮 P&L per leg" chips - in August all 5 legs are green (leg1 leads
  with ~+5.4k on the full 51-signal sim); on full history legs
  2-5 drag the result negative while leg1 stays green.
- 158 green tests.

### 2026-08-17 - Current-shape cutoff + monthly P&L + ruin on "from start" sim
- **Span picker on the "📜 From start" block** of test pages:
  all history vs **current shape only** (`since=2026-07-28`, the date of
  the first current-shape message). New `since=YYYY-MM-DD` param on
  `GET /api/analysis/month` and `GET /api/analysis/compare` (filters signals
  from that date) + `run_analysis(since=...)`.
- **"📊 Monthly P&L" bar chart** on the from-start block of test
  pages (`drawMonthlyBars`) and on the Edge tab (compare-all block, management
  column). `run_analysis` returns `pnl_by_month`.
- **Ruin on sim**: `run_month_sim` returns `bankrupt` + `bankrupt_date`
  when stored balance hits zero - the verdict shows
  "⚠️ ruin on day X" (the full one at 1% risk dies on 2026-07-22).
- `run_compare` handles the no-signal case (returns a tie instead of KeyError).
- Tests: **156 -> 158** (since on month/compare + empty compare).

---

### 2026-08-17 - GOLD/SILVER ticker + per-leg lots on History + stamps + "from start" analysis
- **Top ticker**: XAUUSD/XAGUSD now show **GOLD** and **SILVER** tags (symbol in tooltip) - gold was there, but only as XAUUSD, which was not obvious.
- **Trade history**: the Lots column now shows the **per-leg split** (e.g. `0.03+0.03+0.03`, tooltip with TP1..TP5 and total) instead of only the total.
- **DB**: the 2 timezone-free stamps (demo path, local `_now_iso()`) were set to `+00:00` (0 left).
- **"From start" analysis (research)**: the full sim (576 signals) loses because (1) Apr-Jul were truly loser old shapes on backtest (WR 36-54%, gold falling 4742->4080) and (2) fixed % risk on a falling balance mixes losses down to ruin (end_balance -1153). The current shape (Aug, 51 signals) is strong: nova WR 72.5 PF 2.07; + management PF 6.16. The 30-day sim (current shape only) stays green (PF 3.12 with management) - "from start" drags the mean down.

---

### 2026-08-17 - Seen lots on risk% + bad-zone reject
- **"Lots from risk%" chip** on the risk details block: shows how many lots the set risk% means on a normal XAUUSD trade (total and per leg), set with real contract/pip and the last valid signal SL gap (e.g. 5% -> "0.71 total · 0.14 / leg"). `state.risk_preview()` + `config.risk_preview` in snapshot; `DB.last_signal_with_sl()`. Tables (Positions, History, paper trades, MT5 spots) already showed lots - confirmed in review.
- **Parser rejects bad entry zones** (group typos): zone span > ~1% of price (e.g. `4379-4775` = 396 gold points, `4317-3413` = 904) no longer passes - `_pick_zone_entry` returns None. Wide but sound zones (e.g. `4078.50-4047`) still pass. The 2 bad stored signals were marked `failed` in DB (576 valid signals).
- Tests: **154 -> 156** (bad zones + fair wide zone).

---

### 2026-08-17 - 3 signal shapes + move_sl + TP1/SL edits + history rework + UI
- **Parser knows the 3 Golden Moves shapes**: current (multiline), old `XAU USD SELL NOW 4737-4741 TP1 ... SL` (spaced symbol, medals, 🚫/invalid SL) and oldest `Sell Gold Now ... Targets ...` (Targets = unlabeled list, GOLD->XAUUSD). Before only the first parsed -> **505 real signals got back** from history (73 -> **578**).
- **Offline history rework**: `failed` ones that now parse turned `historical` (never run) and known management turned `management` - backup in `signalbot.db.bak-reparse`.
- **`move_sl`**: "Adjust/Move SL to X" moves SL of all open legs (no level = logged nudge); stored in `pending_management.sl`.
- **TP1/SL edits**: "TP1 4734 SL 4745" with reply updates target signal TP1/SL (`update_signal_levels`) and moves SL of open legs (`_handle_tp_sl_update`) - before they went `failed`.
- **Run curve with absolute P&L**: legend shows `+3.7% · +EUR 418 (EUR 11304)` with hover tooltip (EUR value + % per strategy).
- **Alert when a test beats main**: `_check_paper_race_leader` on tick sends a `risk` note (feed + Telegram if set) when the leader moves (min 2 closed trades; 5 min cooldown at start).
- **Cleaner UI**: folding risk-details block on main; notes grouped by kind with count (`FEED_GROUP_LABEL`).
- Full-backtest result (578 signals): the old span (Apr-Jul) was a loser (PF<1); management edge only lives on the fresh shape (30d: nova 1.1, **nova+management 2.92**, TP2 0.82). >100% drawdown is fixed-%-risk-on-start-balance artifact (no ruin in sim).
- Tests: **145 -> 154** (3-shape parser, move_sl, tp_sl_update).

### 2026-08-17 - "Best strategy" tick on menu + parallel-run curve (main + Edge)
- **SIMULATION menu marks the best**: the winning strategy item on full-history A/B/C (`compare?days=0` -> `winner`) gets a green "best" badge - you see at once which is best of the 3 by whole-history backtest (not only the promoted one).
- **Live "who is winning" curve**: new balance history (`paper_balance_history` - DB only held current balance). The server writes a snapshot each ~60s on tick: test strategies with the virtual (paper) account, main with the real MT5 account balance.
- **Mini-curve on the main page**: "Parallel run (paper)" block with % growth since run start + current balance of each (updates each 60s).
- **Edge-tab block**: "Live parallel run (paper + real)" with the same bigger curve + legend, loaded in `loadEdge` - compare sits next to A/B/C (curve shows the forward run; A/B/C the backtest).
- New endpoints: `GET /api/paper/history?hours=` (per-strategy series, with `is_main`), `paper_balance_history` table (UNIQUE strategy_id+ts).
- Tests: **144 -> 145** (paper/history endpoint).

### 2026-08-17 - Menu "strategy sim" part with the 3 strategies
- The left terminal menu is now called **STRATEGY SIM** (was "TEST strategies") with the **3 pages**: TP1..TP5 no management, **TP1..TP5 + management** (new page - the only one with no own sim) and Close at TP2.
- The main page uses the **best of the 3** - the active strategy in config (today `nova_gestao`). The active-strategy sim page shows the **real run** of the MT5 account (balance/equity/today P&L/open trades) instead of the paper account (it has none), and **hides the "Promote to main" button** (already main); the paper-trades table notes main runs on the real account. `GET /api/paper/trades` now returns `account.is_main` so the dashboard knows.
- Tests: still **144** (frontend changes + one extra field on paper trades endpoint).

### 2026-08-17 - Browser-zoom fit + preview-checked visual fixes
- **Zoom-fit layout**: the tab bar stuck with a fixed `top: 52px` - on zoom (or when the header wraps on narrow screens) it sat under the header. Now header height is live-measured (`updateHdrH()` -> `--hdr-h`, updated on `resize`) and the bar sticks right under the real header (checked: at 870px viewport the header is 139px and tabs stick at 139px).
- **Flat overflow gone**: `.layout` grid items (main/aside) had `min-width: auto` and wide content (tables) pushed the column past viewport - now `min-width: 0` (checked: 0px overflow at 870px, with test/Diagnostics pages).
- **Mobile-menu ☰ button never showed** - inline `style="display:none"` beat the `<=960px` media query; now it shows on phone and hides on desktop.
- **"++" shape fix**: `money()` already puts "+" on green values, but the test-page verdict, paper P&L and paper-trades table added another manual "+" -> "++USD 418.00". Fixed (only `money(pnl)`).
- Visual check made with a temp preview box
  (port 8081, copy DB): main page, test pages and
  Diagnostics with no overlap/overflow at narrow viewport (zoom).
- Tests: still **144** (frontend changes).

### 2026-08-17 - Diagnostics with no tabs + per-page chrome + mobile menu + goodbye "If fixed lot"
- **Diagnostics with no main tab bar** - `navigateTerm("diagnostico")`
  badly hid the bar (Signals/Positions/History/Report/Edge/Ignored
  showed on a diagnostics page). Now only the page content stays.
- **Per-page chrome**: stats/controls/risk block hidden on
  test pages and Diagnostics (`body.no-chrome`); only the header
  (kill switch + badges) and statusbar stay. Edge auto-refresh also
  stops on entering Diagnostics (as it already did on test pages).
- **Test-page labels track the picker** - intro text and
  compare title no longer always say "1 month": they say
  "7/90 days ago" or "since group start" as picked.
- **"If fixed lot (vs risk %)" widget gone** - no registry strategy
  uses fixed lots (duas_pernas left); the risk block only keeps
  loss/SL, per leg, daily cap and today P&L. The `lots_override`
  sim param on the engine stays.
- **Menu reachable on small screens (<=960px)**: **☰** header button
  opens the terminal menu as overlay (closes on nav, outside click or Escape) -
  test pages and Diagnostics are now reachable on phone.
- Tests: still **144** (frontend/nav changes).

### 2026-08-17 - Compare with management + last replay on Diagnostics + orphan-setup badge
- **Test page with management P&L**: the compare (curve + legend)
  now draws **3 strategies** on the same signals - nova, **nova +
  management (main)** and close at TP2 - to see what each would have
  given (before only the 2 test ones).
- **Diagnostics shows the last history replay**: `history_done`
  stores in config `replay_done_at`/`replay_total`/`replay_sinais`/… and
  `/api/diagnostics` shows a `replay` block - the Diagnostics tab shows the
  date + totals of the last replay (messages, signals, management, summaries,
  skipped).
- **Badge warns orphan setup**: if config `strategy` points to an id
  no longer in the registry, `strategy_params.orphan` goes `True` and the
  badge shows "⚠️ … (orphan setup) - running on <fallback>" in red
  (`_strategy_params` also returns `configured_id`).
- Tests: **143 -> 144** (orphan + replay on diagnostics).

### 2026-08-17 - "2 legs · fixed lot" test strategy cut
- **`duas_pernas` left the registry** (`app/strategies.py`): the terminal menu
  keeps **2 test pages** (nova, close at TP2); the test-page compare
  draws 2 curves; the paper run and `paper_status`
  now only build from left strategies. The `lots_override` sim param
  stays backed by the engine (nobody uses it for now).
- No bits to clean: DB held no `duas_pernas` paper accounts/trades
  and config `strategy` was at `nova_gestao`.
- Tests: still **143** (the `strategy_params` test now uses `tp2`).

### 2026-08-17 - Edge with full history + replay-end note + stored picker
- **Edge tab with full-history A/B/C**: new "📜 Since group
  start" block (`loadCompareAll`, `GET /api/analysis/compare?days=0`) with WR/PF/
  P&L/drawdown of the 3 strategies, next to the month compare - to see if
  edge holds since group start.
- **History-replay-end note**: the listener counts forwarded messages
  by action (signals/management/summaries/skipped) and, at the end,
  tells the server (`POST /api/listener/history_done`) which logs the
  "📚 History replay done" note on the feed (`state.history_done`).
- **Test-page picker stored in config**: new
  `teste_days` key (7/30/90/0 = all) - `ConfigUpdate.teste_days`, snapshot
  `config.teste_days`, the dashboard brings it back on reload. The picker got
  the **"all (from start)"** (0) option, which shows the full-history
  sim on the main block (the set "from start" block
  hides then so it never copies).
- Tests: **140 -> 143** (compare days=0, teste_days in config, history_done).

### 2026-08-17 - Promotion with note + full history on test pages
- **Promoting a strategy logs a feed note** with the params
  now used in real run (max_legs/exit_at_tp/lot/management)
  - `state.update_config` + `_strategy_params_text`.
- **Main badge shows real run params**: the snapshot
  shows `config.strategy_params` (`state._strategy_params`) and the
  `♟ name` badge got a second row with `TP1..TP5 (all) · management ON · …`
  (+ tooltip) - you always know how the real account runs.
- **Test pages with 2 views**: last-month sim (7/30/90
  picker) + the **"📜 Since group start"** block - `days=0` on
  `GET /api/analysis/month` = ALL history in DB, no date filter
  (1h/730d candles), with its own verdict/stats/curve per strategy.
- **Paper-trades table** on the test page: new
  `GET /api/paper/trades?strategy=<id>` (open + closed with P&L, time,
  symbol, direction, entry, lots) - the live parallel-run history,
  besides current balance.
- **Listener history replay from group start**: new
  `SIGNALBOT_TG_HISTORICAL_LIMIT` (0 = all messages; old alias
  `SIGNALBOT_TG_HISTORY_LIMIT`; default 0 = all). `history_limit()`
  returns `None` for "no cap" (on Telethon `iter_messages(limit=0)`
  would return zero messages). Set in `.env`.
- Tests: **134 -> 140** (paper trades endpoint, promotion with note,
  strategy_params in snapshot, month sim days=0, history_limit).

### 2026-08-17 - End redesign: main starts today + promote + warns
- **Main (nova+management) starts from today** - the month sim
  ("if you had started 30 days ago") left the main Edge tab and stays
  only on the **3 menu test pages** (nova, close at TP2, 2-leg
  fixed lot). The terminal menu now only holds these 3 test strategies.
- **"⬆️ Promote to main" button** on each test page - `POST
  /api/config {strategy}` swaps the real-account active strategy.
- **Active-strategy badge** on the main page (`♟ name`), with glow +
  toast when it moves; **red warn** when a test strategy beats
  main on the paper run (virtual balance > real balance with
  closed trades).
- **"🔧 Diagnostics" tab cut from the main tab bar** - it has
  its own page on the terminal menu (main-page cleanup).
- **Paper race follows active config**: `open_for_signal(main_id=...)` and
  `paper_status.is_main` use the config strategy, not hardcoded -
  promoting a strategy takes it out of the paper run (it now runs on real).
- Tests: 134 (no count change - frontend/config moves).

### 2026-08-17 - Strategies as concept + terminal nav + forward paper trading
- **Central `app/strategies.py` registry**: nova, nova+management (main) and
  close at TP2, each with `max_legs`/`manage`/`exit_at_tp`. New
  `GET /api/strategies`; `strategy` config (checked) to swap the
  main-account active strategy.
- **Multi-strategy engines**: `SimEngine` and `MT5Engine` take `max_legs`
  (caps legs) and `exit_at_tp` (closes all when TP_N hits - dry and
  real by broker). `run_month_sim` takes `strategy_id`.
- **Terminal nav**: the left terminal went from decor to **menu**
  (up/down arrows + Enter + mouse): MAIN · per-strategy test pages ·
  DIAGNOSTICS. Test page = 1-month sim (1000 EUR) + mini-curve.
- **Forward paper trading** (`app/execution/paper.py`, `paper_accounts/trades/positions`
  tables): each test strategy has its own virtual
  balance; when a real signal runs, test strategies
  open the same paper trade and the tick moves it vs prices - live
  parallel run, never touches the real account. Snapshot shows `paper.accounts`.
- +5 tests = **134 tests**.

### 2026-08-17 - Last-month sim on Edge tab
- New **`GET /api/analysis/month`** (`run_month_sim` in `analysis.py`):
  replay of real group signals with **full management** (TP1..TP5 +
  protect/close_all) from N days ago to today, starting with the **current
  balance** - answers "if you had started 1 month ago with X EUR, today you would
  have Y EUR". Returns start/end balance, P&L, win rate, PF, trades and the
  **stored equity curve** (by signal date).
- **Edge tab**: new "📅 Last month sim - how today would look"
  block with start balance -> P&L -> end balance, win rate, PF, trades and a
  mini equity curve (green/red by result). Refreshes
  with the tab (60s). Never writes to DB nor opens trades. +1 test =
  **129 tests**.

### 2026-08-17 - Edit-lag measure + revive alert + failed-message rework
- **`edited_at` in DB** (new `signals` column, auto migrate):
  logs each group-edit time (`_apply_edit` and revive). New
  `GET /api/edits` returns the lag spread (min/mean/median/p90/
  max + latest samples) - to check the 30s window on real data,
  not by guess.
- **Diagnostics tab**: new "⏱️ Group edit lag" block - the
  median vs current window (green inside / red outside) and the latest
  samples. It fills alone as the group edits.
- **Revive alert**: notes now have kind `revived` (🔄 on
  feed) and the dashboard shows a **toast** when a failed signal is
  brought back by the group edit.
- **Rework of the 12 `failed` messages** (all with no linked trades):
  erased and re-ingested by history replay with the new
  classifier. **4141** (incomplete entry the group filled) came back as a
  valid **historical** XAUUSD buy 4416.50 signal (SL 4408.50, TP1-5) instead
  of failed. The other 7 are known chat/typos (refused
  on purpose). Backup: `signalbot.db.bak-20260817b`.
- Fixed a bug in `insert_signal` (22 placeholders for 21 columns) that
  showed when `edited_at` was added.

### 2026-08-17 - Failed-signal revive + 30s window + old strategy cut
- **Failed-signal revive**: when a signal arrives incomplete (e.g.
  "I'm buying XAUUSD 4416.50-4412" with no TP/SL yet) and fails after the window,
  a later group edit filling it **revives** the signal
  (`action: revived`) - reopens the edit window with the new content
  instead of skipping the edit. Capped at edits up to 5 min after the first
  message; history never revives. +1 test = **128 tests**.
- **Edit window back to 30s** (`edit_window_seconds=30` setup in
  DB) - the "wait and see if the message moved" behavior the
  user asked; the listener already reported edits, now the backend
  re-acts on them while the signal has not run yet.
- **"Old" (TP1/TP2 only) strategy cut from Edge** compare (backend
  `run_compare` + dashboard column + endpoint): nova
  (TP1..TP5), nova+management and close at TP2 stay - the old one was worst (PF
  1.19).
- Compare test now seeds its own data (fixed,
  not suite-order-bound).

### 2026-08-17 - Post-run edit alert + max legs on widget + steady test
- **Post-run edit alert**: if the group edits a message after
  the signal already ran (or went pending) and the window passed,
  the bot sends a `risk` note on the dashboard ("⚠️ The group edited the
  signal ... after run - entry may be stale").
  With short windows (15s) it is the safety net to spot old-price
  entries. History replays do not fire. +1 test =
  **127 tests**.
- **"If fixed lot" widget**: new row with the **max leg count**
  fitting in the set risk % on the current account (yellow if asked
  count outs, red if not even 1 fits) - safe lot sizing.
- **Steady history-replay test**: compare by id + summary-text filter
  (instead of global count) - the rare back-loop flake no longer
  fails the test.

### 2026-08-17 - Fixed lot vs risk % on dashboard + "close at TP2" variant + test split
- **"If fixed lot" widget on the risk block**: shows the real risk (EUR and %)
  N fixed-lot legs (e.g. 0.04) would give on the current account, using the
  last-signal SL and symbol contract - green if <= set risk %,
  red if over. Live lot/leg inputs.
- **"Close all at TP2" backtest**: `simulate_signal(exit_at_tp=2)` -
  when TP2 hits, ALL still-open legs close at that price
  (none runs to TP3..TP5). New 4th column on the Edge tab
  (A/B/C/D: old · nova · nova+management · full TP2) and on verdict.
  +2 tests = **126 tests**.
- **Test split (fix)**: new `tests/conftest.py` forces the temp
  DB and paper broker BEFORE any `app.*` import -
  running test files out of order no longer writes to the
  production DB (today event: subsets wrote 61 signals/29
  trades/59 test spots to the real DB; cleaned + setup brought back
  from the `signalbot.db.bak-20260817` backup).

### 2026-08-17 - DB cleanup: sim leftovers + rework history
- **Today P&L showing +3835.77 EUR was sim leftover**: the new
  account (5054586550, 1000 EUR balance) has **0 real deals** today (confirmed
  on MT5 history); the 3 DB trades (with one +3853 EUR on 1.23
  lots x 5 legs) were paper from a prior server. Erased the 3
  trades + 12 spots; the 2 linked real group signals went back to
  `historical`. `realized_today` now = 0. Backup: `signalbot.db.bak-20260817`.
- **History reworked with the new classifier**: erased the 2
  messages the old match had marked `failed`
  ("TP4 and 5 HIT" msg 4058 and "TP4 👀 ?" msg 3994) and restarted the
  listener - history replay re-ingested them as **`tp_hit` management**
  (`historical`, with no trade action). Suite: **124 tests**.

### 2026-08-17 - Match: multi TPs/emoji · clear restarts by hand · trading terminals
- **Management classifier**: the group sometimes posts **two targets at
  once** ("TP4 and 5 HIT" -> tp_hit tp=5, takes the highest) or with the
  **👀 emoji** instead of HIT ("TP4 👀 ?" -> tp_hit tp=4, start-
  anchored so "Who's with me for TP3 👀" never fires). Checked
  vs real DB messages - 2 missed messages now
  class, with no regressions. +1 test = **123 tests**.
- **Clear restarts by hand**: new `DELETE /api/listener_restarts`
  (`DB.clear_listener_restarts`) + "🧹 Clear restarts" button on
  Diagnostics tab with confirm - besides the auto 30-day purge.
  +1 test = **124 tests**.
- **Themed trading back terminals**: instead of generic assembly,
  CRT terminals now show **lifelike signals/management** (XAUUSD BUY
  TP1..TP5 SL, "TP1 HIT +40 pips", price ticks, fills, balance/equity)
  and are **moving**: steady scroll + **live status row**
  (ticker moving each 1.2s with blinking cursor) + **rare flash**
  on a row. Also on the login page.

### 2026-08-17 - New MT5 account + restart purge + purple/red dashboard
- **MT5 account swap**: demo **Camilo Diogo** (`MT5_LOGIN=5054586550`,
  `MetaQuotes-Demo`, password in `.env`), replacing the prior
  (111209744). `.env` + dashboard footer (now **moving** - reads
  login/server from snapshot instead of hardcoded) + README/AGENTS/DEPLOY
  updated. Server restarted; `/api/status` confirms
  `login: 5054586550`, `mode: mt5-real`. ⚠️ Terminal MT5 AutoTrading
  was OFF after the swap - turn it on in the terminal so orders run.
- **Auto purge of `listener_restarts`**: rows older than 30
  days erase - at start and each ~6h on watchdog
  (`db.purge_old_listener_restarts(30)`; compares `substr(started_at,1,10)`
  zone-proof). The table no longer grows with no cap. +1 test =
  **122 tests**.
- **Purple/red cyberpunk dashboard**: new palette (purple panels,
  `--red: #ff2a4a`, `--violet: #c449ff`), retoned gradients/borders/glows
  (header, stats, tabs, primary buttons, tab-count,
  scrollbars, scanlines, boot) and matching login.html.
- **Moving CRT back**: code terminals (assembly/hex/registers,
  ref-image style) fixed left and right, with
  steady upright scroll, purple/red glow and center reticle;
  `prefers-reduced-motion` and hidden on < 960px screens.

### 2026-08-17 - Edit-window slider + restart history
- **"Edit window" slider on the Caps block**: moves `edit_window_seconds`
  (10-120s, 5 step) straight from the dashboard, with debounce - no code edit
  (checked live: 45 -> 30).
- **Listener restart history**: new `listener_restarts` table;
  the watchdog logs each spawn (`watchdog: dead heartbeat`) and
  `/api/diagnostics` shows the last 10 - the Diagnostics tab shows
  "🔄 Listener restarts" with time + reason (checked live after
  killing the listener: it restarted and logged 15:34).
- **Watchdog regression test**: real order - no listener spawns
  1; server restart with a live listener does NOT copy. +2 tests =
  **121 tests**.

### 2026-08-17 - Watchdog: never copies the listener
- The watchdog spawned a second listener when the server restarted
  (the stored listener heartbeat had not yet logged in the new
  state). Now `_ensure_listener_alive` checks if **one** already
  `telegram_listener` process runs (`_listener_running_anywhere`,
  wmic) before spawning - if one exists, it cools down and waits for heartbeat.
- Fixed an accidental `async def` on the helper (never-awaited
  coroutine quietly crashed the watchdog).
- Tests: +1 (no copy when a listener already exists) = **119 tests**.

### 2026-08-17 - Edit window: 60s -> 30s
- The Telegram-edit wait dropped from **60s to 30s**
  (`edit_window_seconds` in `DEFAULT_CONFIG` + stored setup in DB):
  signals run faster in auto mode. Updated docs
  (AGENTS.md, TECHNICAL.md, CHANGELOG). No new tests - window
  tests set the value plainly. **118 tests**.

### 2026-08-17 - Online-user list on header
- The dashboard header now shows **who is on** (green
  `👥 admin · admin` pill) even before the clock, with tooltip and count.
- Backend: `WSManager` stores per-link WebSocket metadata
  (`{user, since}`) and snapshot/WS shows `online_users` - live updates
  (each open tab = one entry; tab closed, gone).
- Tests: +1 (online_users by WebSocket) = **118 tests**.

### 2026-08-17 - Fix: dashboard stuck at boot by HTTPS tunnel
- **Bug**: on the HTTPS URL (cloudflared/VPS), login went in but the
  dashboard stayed stuck at boot ("DASHBOARD ONLINE..." never left). Two
  causes: (1) WebSocket used `ws://` - on an HTTPS site the browser
  blocks mixed content and the snapshot never came; (2) `finishDashBoot()`
  only hid the overlay if `_bootSnapshotDone` was true, but the ~3s
  fallback called it with the flag false -> stuck forever.
- **Fix**: `wss://` when `location.protocol === "https:"` (ws:// local)
  and `finishDashBoot()` always hides the overlay (with snapshot or on
  fallback). Static file only - no server restart.

### 2026-08-17 - Deploy: Azure option in DEPLOY.md
- New 0.1 part in `docs/DEPLOY.md`: test on free **Azure** -
  $200/30-day credit with a `Standard_B2s` VM (2 vCPU/4 GB) for real
  24/7 test, vs free `B1s` VM (1 GB - too short for MT5+bot);
  make steps (Windows Server 2022 Azure Edition · West Europe ·
  NSG with tight 3389 and 443) and 30-day credit-end warn
  (move to Vultr ~$16/month vs Azure ~$50/month). Docs only.

### 2026-08-17 - Deploy: Vultr guide in DEPLOY.md
- `docs/DEPLOY.md` updated with **Vultr** as picked provider:
  make steps (Cloud Compute · Windows with included license ·
  Frankfurt/Amsterdam · 2 vCPU/4 GB/80 GB · Auto Backups), snapshot note
  before going real and Vultr Cloud Firewall hint
  (open 443, RDP only for your IP). No code change - docs only.

### 2026-08-17 - "Do it now team" (GO) known as management
- **Bug**: "Do it now team" (the admin GO, msg 4125) was audited as
  skipped chat - it is no TP/SL/close order, but it is the at-once
  entry confirm.
- **Fix**: new `kind: go` on the classifier (`manage.py`, `do it now | enter now | go now | jump in now`
  shape): logged as management and,
  if a signal sits in `awaiting_edit` (the one the message answers or the
  newest), **it ends now** - auto -> runs now (short window),
  manual -> pending. With no waiting signal it only logs (closes nothing,
  no risk note).
- **Tests**: +3 (GO class; GO ends a waiting signal by reply; GO with no
  signal only logs with no risk) = **117 tests**.

### 2026-08-17 - "I'm buying/selling" enters the edit window
- **Bug**: the `_SIGNAL_HINT` regex used `\b(buy|sell)\b` - "buying"/
  "selling" never matched (no boundary before "ing"). The first line of the
  group signal ("I'm selling XAUUSD") went to the skipped audit;
  when the TP/SL edit came, it entered as a signal - the same message
  showed in Ignored AND Signals (msg 4123).
- **Fix**: `_SIGNAL_HINT` now takes `buying|selling|comprando|vendendo` -
  the first line enters the edit window at once (60s) and Telegram
  edits update it; and when a skipped message is edited into a
  signal, the audit row is cut (`delete_ignored`) so it never
  copies.
- **Tests**: +2 (gerund enters window; edited-skipped clears
  audit) = **114 tests**.

### 2026-08-17 - Right "Last msg" + OFFLINE phone alerts + clean feed
- **"Last msg Xs ago" fix (Diagnostics)**: the listener stamped `last_msg_at`
  with start time after history replay, so the dashboard
  showed "last msg 7s ago" right after a restart, even with no
  new message. Now only real-message handlers (`on_new`/`on_edit`)
  move the stamp - history replay does not count as a message. The
  dashboard shows "no messages yet" until the first group one comes.
- **OFFLINE phone alerts (Telegram)**:
  - MT5 off / AutoTrading off -> the listener DMs its own account
    (Saved Messages, Telethon session - no bot, no setup) and the server
    sends a risk note (feed + sound + push if a bot is set);
  - Listener offline -> the watchdog sends a risk note at restart;
  - Optional `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` to
    push any note to the phone by bot (@BotFather).
- **Clean feed**: erased 29 copied notes of the old replay
  (12:36-13:04 summaries/splits, made by prior restarts);
  summary ground truth stays in the `group_summaries` table.
- **Tests**: +2 (MT5 drop notes; listener watchdog notes) =
  **112 tests**.

### 2026-08-17 - Listener auto-restart + Telegram code + uptime chart
- **Listener auto-restart**: the server has a watchdog (10s loop)
  that restarts the listener process alone when heartbeat
  stops (>5s offline) - kill the listener and it comes back alone (checked).
- **Telegram code on the dashboard**: if the listener needs a fresh login
  (out session/new device), it tells the server
  (`POST /api/listener/need_code`), the dashboard shows a toast with input and
  the user types the code (`POST /api/listener/code`); the listener
  fetches it only once (`GET /api/listener/code`) and links. Heartbeat
  starts BEFORE auth so the watchdog never restarts during start.
- **Toast + sound when the listener goes OFFLINE**: `watchListener()` compares
  state on each snapshot - sticky red toast + risk sound on the
  drop, green toast + win sound on return.
- **24h uptime chart** on Diagnostics tab: 10s samples
  (listener/MT5 up vs down, with uptime %), in-memory history
  (24h deque), downsampled to ~300 points on the endpoint.
- **Windows note**: each venv python process shows as a pair
  (launcher + real runner, Python 3.14) - not copies.
  Kill listeners: `powershell ... CommandLine -like '*telegram_listener*'`.
- Suite: **110 tests**.

### 2026-08-17 - Diagnostics tab + 1s listener heartbeat + polish
- **"🔧 Diagnostics" tab**: live MT5 state (link, account, balance/equity,
  AutoTrading), Telegram listener (heartbeat, last group message with color:
  green <15m, amber <2h, red >2h), server (uptime, PID), market feed
  (real/sim), login, database (counts + file) and last group
  message. Refreshes each 10s while the tab is open.
  (`GET /api/diagnostics` + `POST /api/heartbeat`).
- **1s listener heartbeat**: the listener sends the last-message
  stamp; the server marks it OFFLINE in ~5s with no news - spots the
  quietly dead process (which happened before) and shows group-message
  freshness.
- **Fix**: history-replay summaries no longer note the feed (only live
  ones note + split) - listener restart no longer floods the
  dashboard with repeat notes.
- **General polish**: `when()` helper (today/yesterday/DD-MM) on Signals, History,
  feed, Edge and Ignored; PT directions (▲ buy / ▼ sell); PT trade
  states; "When" headers; ** cut from "Last of the group"; moving
  edit-window countdown on hint mode; daily chart on DD/MM; full-date
  tooltips on Ignored. Suite: **109 tests**.

### 2026-08-17 - Day seen on Signals/History tables
- Time columns on Signals, History, feed and "Last of the group" only
  showed HH:MM (and raw UTC on Telegram histories). New `when()` helper:
  today -> time only; yesterday -> "ontem HH:MM"; older -> "DD/MM HH:MM" -
  always local time. Suite: 107 tests.

### 2026-08-17 - Cleanup: one group on the signal filter
- The filter showed 2 rows for the SAME group: "tg:Golden Moves ⚜️"
  (real name with emoji, 141 listener msgs) and "tg:Golden Moves" (4 old
  test rows: 3 management with no real message_id + 1 erased test signal).
  Checked none had trades/spots/pending
  pointing at them and erased - the listener always stores the real title.
- Suite: 107 tests.

### 2026-08-17 - Skipped-message audit + listener forwards all
- **"🚫 Ignored" tab** on dashboard: shows group chat the bot
  does not store (time, source, full text) with a live badge
  (`GET /api/ignored` + `ignored_count` in snapshot). New
  `ignored_messages` table (message_id+source dedup, keeps last 1000).
- **`SIGNALBOT_TG_FILTER=0`** in `.env`: the listener forwards ALL; the
  call moved to backend - `classify()` -> management; `_SIGNAL_HINT`
  (same regex as the old filter, now in `state.py`) -> signal; else
  chat -> audit + `action: ignored`. Messages with no keywords never
  enter the edit window.
- New tests: skipped-and-audited chat, signal typo still waits for the
  window, management with no target fires a risk note (the alert already
  was there, now tested). Suite: **106 tests**.

### 2026-08-17 - Listener skip log shows the text
- The listener `[skip] ... not a signal` log now has the
  first 90 chars of the skipped message - faster diagnosis
  with no need to fetch the message from Telegram (skipped messages are
  group chat, not signals). Suite: 103 tests.

### 2026-08-17 - Boot sequence + dashboard sounds
- Cyberpunk boot order at dashboard entry (after login): terminal
  lines with made keyboard clicks; when the first snapshot
  comes, it adds the REAL state (MT5 BRIDGE OK/OFFLINE, ALGO TRADING,
  REAL (DEMO) MODE) and fades to the loaded dashboard.
- Made sounds on feed notes: open trade (2 sharp beeps),
  closed trade (blip), risk (2 deep alert beeps), management/summary/system
  (soft blip).
- Alert sounds when MT5 drops or AutoTrading drops (deep) and
  win sounds when they come back (sharp) - besides the already-stored sticky
  toast.
- Audio honors autoplay policy: unlocks on first click. Suite: 103 tests.

### 2026-08-17 - Login mute button cut
- The 🔊 mute button is gone by user ask - sound is handled
  in the browser itself (tab mute).
- Kept: boot keyboard clicks, deep (error) / sharp (win) beeps,
  choice store no longer needed (no sound state).
- Stale comment cleanup in login.html. Suite: 103 tests.

### 2026-08-17 - Login boot sequence + neon favicon + README/scripts

- **Login boot sequence**: cyberpunk terminal with lines showing
  (INIT PROJECT NEXGEN · MT5 BRIDGE OK · TELEGRAM LISTENER OK · RISK
  ENGINE OK · AUTH SYSTEM OK · PROCEEDING TO LOGIN) at 330ms/row, then
  fade to the form.
- **Post-login boot**: after login, the screen shows "CHECKING ACCESS..."
  -> "ACCESS GRANTED" before entering the dashboard (the access
  check only happens after login).
- **Glitchy password error**: the box glitches with shifted
  slices (clip-path, no shake), red glitch title,
  red-border input and the button turns red saying "Wrong
  password" (1.8s), with an "Try N - access denied" count.
- **Red flashing favicon** on the tab when the password fails
  (red/cyan swap during the error and stays red; back to normal
  when the error ends) - the warn shows even off-page.
- **Green flashing favicon** on good login: the button turns
  "Right password ✓" green (seen ~1s before post-login boot) and the
  favicon swaps green/cyan, before entering the dashboard.
- **Made beeps** (Web Audio): 2 deep beeps on password error and
  2 sharp beeps (880->1320 Hz) on win - audio feedback.
- **Boot keyboard sound**: each terminal row plays a short made
  click (~1.7-2.2 kHz, 35ms). **🔊/🔇 mute button** on login
  (stays in `localStorage`), mutes boot clicks and beeps.
- **Fix (TDZ bug)**: `nextLine()` started before `let soundOn` was
  set and the error stopped the rest of the script (mute and submit never
  wired) - boot now starts at script end.
- **Neon favicon**: cyan "N" icon with neon frame (inline SVG,
  data-URI) on the dashboard and login tab.
- **README.md rewritten** for the current state (real run on MT5 demo,
  TP1..TP5 + management, auth, cyberpunk theme, docs) - it was stale
  (paper, TP1/TP2).
- **start.ps1 / start.sh**: start texts with the Project NexGen name.
- Suite: **103 tests**.

### 2026-08-17 - Fix: /login after Exit gave 404

- The Exit button went to `/login`, but middleware let that
  path pass to StaticFiles (which looks for a missing "login" file)
  -> 404. Now `/login` serves the login page (`_login_page()`); with a
  valid session, it goes to `/` (302).
- Note in AGENTS.md: on Windows, Git Bash `kill` does not kill uvicorn -
  use `taskkill //PID <pid> //F` and confirm the free port.
- New `test_login_page_after_logout` test. Suite: **103 tests**.

### 2026-08-17 - Renamed to Project NexGen + livelier glitch

- **Program name**: "SignalBot" -> **"Project NexGen"** (dashboard,
  login, title, footer, backend `APP_NAME`, docs and tests).
- **More frequent title glitch**: 5s cycle -> **2.6s** with a bigger
  glitch window (3 cuts per cycle).
- **Cyberpunk 2077 login**: Orbitron/Chakra Petch fonts, neon
  colors, CRT scanlines and title glitch, in line with the dashboard.
- **Soft glitch on ticker prices** (XAUUSD, EURUSD…): rare RGB split
  (~1s each 9s) by `data-text` on values.
- Suite: **102 tests**.

### 2026-08-17 - Cyberpunk 2077 as the single theme

- **Cyberpunk 2077 theme is now the one and default**: `data-theme="cyberpunk"`
  fixed on `<html>`; theme picker and `localStorage` logic **cut**.
- **CRT effects**: soft scanlines over the whole screen (fixed overlay with
  `repeating-linear-gradient`, no click block) and **stepped**
  title glitch (RGB split with `clip-path`, ~1s each 5s) + flicker on the
  header gradient row.
- Suite: **102 tests**.

### 2026-08-17 - More pro site (nav + display)

- **Sticky header** (fixed top, back blur) and **sticky tabs** with
  **counts**: badge on Signals tab (pending) and Positions tab
  (open trades), seen when > 0.
- **KPIs with icons and tooltips**: 💰 Equity, 🏦 Balance, 📊 Open P&L,
  📈 Today P&L, 🧾 Open trades, ⏳ Pending signals.
- **Type-colored notes feed** (green trade, amber risk,
  blue system/management, violet summary).
- **Zebra tables** (split rows) + footer with account/server.
- **Small screens**: 2-column phone metrics and less padding.
- Suite: **102 tests**.

### 2026-08-17 - Visual polish + plain per-trade risk

- **Plain per-trade risk**: the panel now explains the % is of the
  **whole account** and means the **loss if SL hits** (not the margin
  used); plain slider tags/tooltips, chips ("Loss if SL hits",
  "Per leg", "Daily cap", "Today P&L") and mode tip ("if SL
  hits, you lose X% of the account (EUR …)").
- **General polish**: card shadows, hover lift on buttons/stats,
  hover on tabs, custom scrollbar, clock chip, action group
  on header.
- **"Exit" button** next to Kill switch (`.hdr-actions`, ghost style);
  🚪 icon cut (it drew badly on Windows).
- Suite: **102 tests**.

### 2026-08-17 - Dashboard login + VPS deploy

- **Dashboard login** (`app/auth.py` + middleware in `main.py`): with
  `SIGNALBOT_DASHBOARD_PASSWORD` set, `/` serves `login.html`, the whole
  API returns 401 with no session, `/ws` closes with 4401. In-memory sessions
  (httpOnly cookie, 7 days). Login live with user `admin`.
- **Listener token** (`X-API-Token` = `SIGNALBOT_API_TOKEN`): the
  listener logs in with no browser/cookie; if the dashboard password
  moves, the listener keeps working.
- **"🚪 Exit" button** on dashboard (seen when auth is on);
  JS goes to `/login` on any 401.
- **`tests/test_docs.py`** - anti-regression test: AGENTS.md must
  state the real suite test count (fails if the suite grows
  with no doc update).
- **`docs/DEPLOY.md`** - full deploy guide on a Windows VPS
  (Python + MT5 + Caddy HTTPS + firewall + `start.ps1`).
- **`docs/CHANGELOG.md`** - this file.
- Suite: **102 tests**.

### 2026-08-17 - Dashboard polish (clear auto mode)

- **€ risk preview**: what `risk_pct` really means -
  € at risk per trade, per leg (TP1..TP5), daily cap and today P&L;
  live updates as the slider drags.
- **Live status bar** on top: mode, run, edit-window
  countdown (1s tick), last group message,
  pause state.
- **Mode tip** (auto/manual/paused) with what the next
  signal does and the € at risk.
- **Balance flash** when it moves (green/red).
- **"P&L per day" chart** on Report tab (€ bars + win
  rate line) fed by the new `by_day` field of `/api/report`.
- **Sticky toast** when MT5 drops or AutoTrading drops
  (stays until fixed; with "✅ MT5 reconnected" on return).
- **Risk audit on feed**: the `trade_open` note shows the real
  € at risk (`risk 2.0% of equity = 1,975.88 EUR`).
- "Lost today" tag -> **"Today P&L"** (it read wrong with gains).
- Fixed flaky test (`test_management_reply_queued_until_trade_opens`
  now polls instead of a fixed `sleep`). Suite: 97 -> 102 tests.

### 2026-08-17 - Must doc rule

- AGENTS.md: new **"Keep docs updated"** part - every
  change must update AGENTS.md / TECHNICAL.md / PUBLIC.md in the
  same turn; fixed test count (101 -> 102) and filed auth.

### 2026-08-17 - Telegram reply link

- `Signal.reply_to_msg_id` stores the answer link (Telethon:
  `message.reply_to`); the dashboard shows "📨 management · → answers
  HH:MM SYMBOL signal" in history.
- `_handle_management` picks the target by exact link (reply), then
  same symbol, then newest.
- `pending_management` (new DB table): notes whose signal is still
  in the edit window queue and apply when the trade
  opens (`_apply_pending_management`).
- **No-target management alert**: a management message with no trade or signal to
  answer fires a ⚠️ note (never quiet).

### 2026-08-17 - Balance fix (11k -> real)

- Cause: `main.py` did not load `.env` -> server started in `sim`
  with paper balance. Fix: `load_dotenv(override=False)` at start +
  `EXECUTION_MODE=sim` forced in tests. Dashboard now shows the
  real broker balance (€99,097.53).

### 2026-08-17 - Real run on demo + E2E

- "⚡ real run (MT5 demo)" badge/sub (no "simulation").
- **Real bug fixed**: the lot override (`lots: 0.01`) was lost -
  the `signals` table missed the `lots` column; added (schema +
  migrate + insert/read). An E2E test opened 0.01 lots x 2 legs on
  MT5, confirmed on dashboard and closed.

### 2026-08-17 - Walk-forward + close_tp1 + split alert

- **Walk-forward** (`/api/analysis/walkforward` + Edge block): group-management
  PF by time windows (4/4 green windows, PF 2.11 -> 11.00).
- **`close_tp1`** on classifier/sim/live: "Close TP1 here" only closes
  leg 1 with no SL touch (apart from `protect`).
- **Group-vs-bot split alert**: fresh group summary with WR
  >=10pp off backtest -> ⚠️ note.

### 2026-08-17 - Summary ground truth + management on backtest

- Backtest with a **manage** variant (group protect/close_all/close_tp1
  linked by source+time window, SL-first, look-ahead-free fills):
  PF climbs 2.70 -> **4.39**.
- History reloaded with the **6** group summaries as ground truth
  (Monday 80% … Weekly **91%**); Edge block compares group vs bot
  (-14.1pp).
- `classify` runs before dedup; idempotent `insert_group_summary` by
  `message_id`; historic management never hits open trades.

### 2026-08-17 - 5 TPs on strategy

- Strategy widened from TP1/TP2 to **TP1..TP5** (1 order per TP on
  MT5, breakeven of rest after TP1, Positions tab with each leg and its
  own TP and the signal it fits).

### 2026-08-17 - MT5 setup

- **MT5Engine** with the same face as SimEngine + `EXECUTION_MODE`
  (`sim` / `mt5`) + dry-run (`MT5_DRY_RUN`).
- Run badge (sim / mt5-dry / mt5-real, linked account, balance,
  equity) on header.
- Log/feed alert when the broker refuses orders (non-zero retcode) - never
  more ghost trades with no warn.
- MT5 terminal restart button / AutoTrading OFF spot.
- Open MT5 spots (lots, price, EUR P&L) on dashboard,
  synced each second.
- `start.ps1`/`start.sh`: start all with one command, with no password
  stored in file.

### 2026-08-17 - Edge analysis + real prices

- **Edge tab** on dashboard: backtest of real signals, verdict
  (edge/marginal/none), A/B/C (old vs nova vs nova+management), walk-
  forward, group ground truth.
- **Real XAUUSD prices** by Yahoo (GC=F) with mock fallback;
  real-vs-sim sign per symbol on header.
- Fixed ghost prices: paper-balance reset and cut of the 3 ghost
  XAUUSD trades that had made +$135,000.

### 2026-08-17 - Message match

- Management classifier (`app/messages/manage.py`): TP HIT, SL HIT,
  close TP1, protect, close all (BE or not), summaries.
- Signal parser with must sanity checks; optional LLM fallback
  (never runs in auto).
- "Tune the match" protocol filed in AGENTS.md.

---

## Older versions (summary)

- Telegram listener (Telethon) reading the Golden Moves group with history
  (~200 messages) and `message_id` dedup.
- SimEngine (paper) + dashboard with Signals / Positions / History /
  Report / Edge tabs; token login first planned.
- N-latest: sim tab cut (from real MT5 mode on, group
  signals run on demo).
