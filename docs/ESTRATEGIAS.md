# Project NexGen strategies - review document

> **Goal of this document**: be the SINGLE source to review the bot
> strategies, with no need to read any other project document. It explains the
> context (what the bot does), concepts (TP/SL/BE/PF/DD...), the **13
> strategies of the central registry** (full mechanics + results), how numbers
> were found (method) and how to judge if a strategy is good.
>
> State: **2026-08-31** · test suite stated in `AGENTS.md` · **demo** account
> (never real money).

---

## Part I - Context (what the system is)

Project NexGen is a **semi-automatic copy-trading bot**: it reads signals from
a Telegram group called **Golden Moves**, waits ~10 seconds (edit window
- the group edits the message to fill the signal), and runs the trade
on an **MT5 demo** account with an **N-leg strategy**
(TP1..TP5 + SL + breakeven). A web dashboard shows all live.

### Per-trade replayable evidence

When the opt-in journal is on, trade history shows a replay button.
The button auto-exports a full tape of the trade - structured signal,
quotes and reply-linked management - and compares SimEngine, PaperRace and
MT5 dry-run. The result has a snapshot after each event to find
where engines diverge. It is a read-only audit tool; it does not change the
main `nova_gestao`, shadow-paper or the kill switch. The `/auditoria` page
also exports the daily bundle in JSON/CSV and archives forward-window snapshots
for later send and compare. The daily summary adds journal quality
and `auto_escada_3/5` progress on feed/Telegram. The UI shows promotion
only as a blocked action until all objective gates
(N>=20, PF, IC95%, DD and reference) pass; the backend applies the same guard.

**Signal format** (real group example):

```
I'm buying XAUUSD
4400-4396
TP1 4402
TP2 4404
TP3 4406
TP4 4408
TP5 4410
SL 4392
```

Meaning: buy gold in zone **4400-4396** (bot enters near 4400), TP1 at
4402, TP2 at 4404... TP5 at 4410, and **SL at 4392** (loses if it drops below).
Each TP closes **one leg** of the trade; total trade risk is split across
legs.

**Group management**: besides signals, Golden Moves sends **live management
messages** ("TP1 HIT", "Close TP1 here", "we're out", "move SL to
4331", "protect..."). The bot classifies those messages and applies them to open
trades - **if the active strategy honors that management kind** (see Part IV).

**Demo account**: all in this document runs on virtual money
(MT5 demo account). Never promote to a real account without an explicit user
decision (SPEC v3, draft).

**Shadow-paper (forward)**: the main account stays apart from forward
evidence. The VPS setup now watches `auto_escada_3` and `auto_escada_5`,
with two standalone virtual accounts at `origin="shadow"`; this does not change
the main strategy nor send orders to MT5. By default, full live signals feed `auto_escada_3` and
`auto_escada_5`, each in a paper account tagged `origin="shadow"`, even
in Manual or with kill switch on;
history and `llm_parsed` proposals stay out. Group management and TP1/SL fixes
mirror with no use of the real management queue or normal paper-run
delivery: `pending_management` keeps split markers for
`applied`, `paper_applied` and `shadow_applied`. Dedup adds origin
and virtual account, daily kill and shadow metrics share no balance
with the normal run. This is not Phase 3 and never sends orders to MT5.

### Deterministic replay across engines

To check the strategy does not win only from an accidental gap across
adapters, there is a read-only tape with `signal`, `quote` and `management` events,
plus `order_request`, `order_result`,
`modify_request`, `modify_result`, `fill_open` and `fill_close` evidence from MT5. The journal
logs retcodes, tickets, prices, P&L and signal-to-order-to-fill latency when
execution provides them. `POST /api/operations/replay` feeds the same sequence
to SimEngine, PaperRace and MT5 dry-run, returns per-signal decisions,
applied SL, reason, P&L and `broker_observations`. Broker answers are
passive in the study: they allow no orders and never overwrite simulated decisions.
Event order with the same timestamp comes from `sequence`. This is a
validation tool, it does not change main `nova_gestao`, does not turn on real MT5
and is not forward evidence alone.

### Controlled forward gate of auto-ladders

The `/auditoria` page and `GET /api/analysis/forward-gate` split forward
evidence from monthly backtest P&L. The default window is 30-day UTC and uses
only closed trades of `origin="shadow"` for `auto_escada_3` and
`auto_escada_5`; `nova_gestao` of `origin="race"` is the reference by
`signal_id`. A strategy only goes `eligible` with N>=20, PF>=1.5, the
lower IC95% bound of mean P&L positive, DD<=125% of start balance, at least
20 comparable signals and P&L no more than 10% behind reference.
The result is a descriptive gate with `promotion="manual_only"`: it does not change main
nor allow MT5.

The main panel **"How much is left to promote auto_escada_3"** joins that
result with pace ETA (`fase3_eta`) and plainly shows N/PF/IC95/DD
and missing gates. `GET /api/window` returns the same block for audit.
The real daily-summary run window runs in UTC from 20/08/2026 to
29/08/2026 00:00, with min target 10 and goal 20 trades; this is watching, not
an auto promotion.

### Monday review - 31/08/2026

The read-only battery ran again after new signals arrived (no
change to config, paper, shadow or MT5). In the current 30-day cut, the
ablation found `all` with PF **5.96** and +**EUR 3,472.29**, vs PF **2.19** and
+**EUR 833.90** with no management; applied management hit 43 signals. This is
in line with keeping `nova_gestao` as main, but is no evidence to
promote an auto-ladder.

The `auto_escada_3` sweep confirmed the `confirm_filter`: current PF
**1.17** in both, while on old history DD dropped from **284.8%** to
**242.8%**; gate passes. The defensive sweep of `auto_escada_5` found no
eligible combo: current PF **1.85**, but old-history PF **0.64** and DD
**219.0%**. So no default changed.

90-day walk-forward picked full management in all three folds, but
only **1/3** of test blocks made money; it is regime-change evidence,
not a license to extrapolate the fresh result. The TP3-TP5 weight test
cut current PF from **2.19** to **1.94** and raised DD from **21.9%** to
**27.6%**, so equal split stays. The mechanical fallback gave
same results as human in this sample (zero seen triggers), so
it stays study-only and off.

The polling study still shows 1s can miss intrabar events vs
100 ms; this is sensitivity diagnosis, not monthly P&L. There were
also symbols with no Yahoo history (`AROUND`), out of replay; numbers
above must not be read as full cover of that symbol.

**Current run state (2026-09-01):** main is set to
`tp2_protect`, with 2 legs and exit at TP2. The account is hedged demo
`5054644473` (`MetaQuotes-Demo`), seen balance **EUR 1,003.89**,
`max_positions=5`, AutoTrading on and `MT5_DRY_RUN=0`: new approved signals
can send orders to the demo account. This is no permit for a
real account; any promotion to real money stays blocked on an explicit
human decision.

---

## Safe operation (before promoting an auto)

Multi-leg run only fits an **RETAIL_HEDGING** MT5 account
(`margin_mode=2`). In real mode the provider checks type at startup and
blocks netting/unknown; the panel shows `hedged=false` and the reason
instead of sending orders that could merge legs. The main account stays
a **demo** and promotion to a real account needs an explicit instruction.

The VPS also caps `server.log`/`listener.log`/errors at 10 MiB per file and
5 copies by default (`SIGNALBOT_LOG_MAX_BYTES`/
`SIGNALBOT_LOG_BACKUP_COUNT`), checks disk daily (10 GiB free by
default) and monthly tests the newest backup with:

```text
.venv/Scripts/python backup_db.py --restore-check
```

These checks are run ops: they change no strategy params, open
no trades and do not count as Phase 2/3 forward sample.

### Operational health and ongoing checks (Items 10 and 17)

The Report health panel and `GET /api/operations?days=N` show
stored exceptions, mean monthly spread per symbol, MT5 reconnects per
week and SL modifies rejected per retcode. `MT5Engine` only takes a new
SL as applied after 10009/0; a fail stays audited. The sample must
collect at least two weeks before any trend call.

The central registry also shows an explicit per-strategy lifecycle:
`backtest`, `paper`, `shadow`, `menu` and `promotable`. Old hidden-strategy
lists stay compatible, but dashboard/API need not
rebuild those rules. Forward metrics also show a bootstrap P&L range
(`mean_pnl_ci95`) and a descriptive gate; this gate never
promotes a strategy alone and needs enough evidence before any
run change.

When `execution_journal_enabled` is on, quotes and management messages
can collect in `execution_events` and read by
`GET /api/operations/events`. It is a read-only audit/replay tape; it does not
change the demo account nor allow promotion.

The `.github/workflows/ci.yml` workflow runs the full suite on push/PR and
then `tests/test_docs.py`. A manual dispatch with `failure_probe=true`
runs a deliberately failing pytest to confirm blocking
behavior; it does not change demo account setup nor promote strategies.

## Part II - Base concepts (needed to review)

| Concept | Meaning |
|---|---|
| **Signal** | Group message with entry, SL and TP1..TP5 |
| **Leg** | One part of the trade: N legs = N entries with same SL but different TP (leg 1 -> TP1, leg 2 -> TP2...) |
| **TP (Take Profit)** | Level where the leg closes in profit |
| **SL (Stop Loss)** | Level where the loss is cut |
| **SL-first** | Sim/run rule: if at the same time/candle SL and a TP both hit, SL counts (conservative) |
| **BE (breakeven)** | When TP1 hits, SL of remaining legs moves to ENTRY - the trade can no longer lose (worst case exits at 0) |
| **Pips** | Symbol-set price unit: fallback XAUUSD uses 0.01, XAGUSD 0.001 and forex 0.0001; the MT5 broker can give real values |
| **Lot** | Position size (0.01 = min); risk % sets lots |
| **Risk per trade** | % of account at stake if SL hits (default **1.0%**, before dynamic scale; lots are set per symbol and split across legs) |
| **PF (Profit Factor)** | Gross profit of winning trades / gross loss of losing ones. **PF >= 1.5 = good; PF >= 2 = very good; PF < 1 = loses money** |
| **WR (Win Rate)** | % of trades closed in profit |
| **P&L** | Total profit/loss in EUR |
| **DD (Drawdown)** | Worst peak-to-valley balance drop, in % (e.g. DD 25% = account once fell 25% from peak) |
| **N** | Count of solved signals/trades in a sample |
| **Backtest** | Replay of REAL group signals on history prices (real candles), with same production rules |
| **Paper run** | Each strategy has a virtual 1,000 EUR account; the normal run follows signals that run on main, while **shadow-paper** can also take live signals with main on kill switch (manual mode was removed - Item 12) - always with no MT5 |
| **Evidence gate** | Bootstrap range and min trade count to review a candidate; it is statistical evidence, not auto promotion |
| **Journal/replay** | Opt-in quotes and management tape to replay decisions with no production change |
| **Look-ahead** | Sim bug: using future info (e.g. the close of the still-open candle). All project rules are **look-ahead free** |
| **Out-of-sample** | Check on a different time window than the one that set params (robustness test) |
| **Sweep (grid)** | Run one param on many values to see if result is stable or a luck spike (lone spike = overfit) |

---

## Part III - What a strategy is (params)

A strategy tells the bot **how to run each trade**: how many legs,
if it applies group management, if it exits early at a TP, and if it manages alone
(auto-manage). Registry params (`app/strategies.py`):

| Param | What it does |
|---|---|
| `max_legs` | Leg count (TP1..TPN). `None` = 5 (all) |
| `manage` / `manage_kinds` | Group management: `True` = honors all kinds; a set filters (e.g. `{"protect"}` = protect only) |
| `exit_at_tp` | When TP_n hits, ALL still-open legs close at that price (early exit). `None` = runs to the end |
| `lots_override` | Fixed lot instead of risk % (no current strategy uses it) |
| `stair_levels` / `stair_from` | **Protection ladder** (auto-manage): when TP_k hits, SL of later legs moves to TP_k level. `stair_from` = step where it starts (1 = moves to TP1 LEVEL; 2 = TP1 only goes to BE and ladder starts at TP2) |
| `confirm_filter` | **Confirm filter** (auto): if leg k takes SL (move died), legs > k close at market |
| `trail_factor` | **Trailing** (auto): after TP1, SL follows peak with slack = `trail_factor x dist(entry->TP2)`. 2.0 is about 8 pips on gold |
| `time_stop_minutes` | **Time stop** (auto): if legs stay open N min after TP1, they close at market |
| `def_window` / `def_trail` / `def_time_stop` | Defensive mode after PF < 1.0 on last N trades: tighter trail/time stop, no leg cuts |
| `def_risk_pct` | Absolute risk cap during defensive mode (1.25% on auto-ladders; never raises dynamic risk) |
| `atr_k` / `atr_period` / `atr_timeframe` | ATR-adaptive trailing - **off** (sweep proved it worse; see Part VIII) |
| `struggle_ae_pips` / `struggle_fraction` | **"Struggle" rule** (R4): before TP1, if price went N pips against entry and comes back to entry, it closes `fraction` of legs at BE and guards the rest at entry (admin tip "take 50% / make risk-free"). Backtest-only - see R4 study |

**What ALL do the same**: SL always on (SL-first) · TP1 closes leg 1
 and moves rest to BE · risk split across legs · real costs in
 backtest (spread, slippage, commission) · daily kill (day loss > **3%** by
 default, settable) -> pause. On auto-ladders, defensive mode also
caps new entries at `def_risk_pct=1.25%` when PF(10) < 1.0.


---

## Part IV - Group management (live messages)

The bot classifier picks the **kind** of each management message; the
active strategy honors those in its `manage_kinds`:

| kind | Real group example | Action |
|---|---|---|
| `tp_hit` | "TP1 HIT +70 pips" | TP1 -> SL of rest to BE; other TPs -> logged |
| `sl_hit` | "Oh wow SL HIT" | logged + reconcile |
| `close_tp1` | "Close TP1 here" | closes only leg 1 (rest keep SL) |
| `close_tp` | "Close TP2 here" | closes only leg N at market |
| `protect` | "close first entries and move stop loss above best entries" | closes leg 1 + moves SL of rest to ABOVE entry |
| `close_all` | "we're out", "out at BE" | closes all (BE -> at entry; else at market) |
| `move_sl` | "Adjust SL to 4331" | moves SL of all open legs to the given level |
| `summary_correction` | "Correction: Weekly summary ... Trades Sent ..." | fixes numbers of target summary by reply/period and re-arms group-to-bot divergence |
| `recount` | "Signal #1: Sell at 4357 ... +110 pips" | stores per-trade recount (R2) in `trade_recounts` and audits vs same-day real fill (divergent entry/pips -> alert) |

A strategy with `manage=True` (full management) honors **all**; one with
`manage_kinds={"protect"}` honors **only protect**; mechanical `auto_*` do not
honor group management, except `auto_escada_3_hibrida`, which honors only
`protect` through monotonic merge.

---

## Part IV-A - MFE win rate (R3) and split from group convention

The group counts as a **win** any trade that ran **25-30 pips in
favor** (max favorable run), even if it later comes back and closes at
loss/BE. The bot, until now, compared reported WR with its own
`win_rate` (pnl>0) - different conventions made false >=10pp divergence
alerts for trades that ran +40 pips and came back to SL.

Backtest now measures `mfe_pips` per trade (pure watch, never a
fill decision) and the set shows `mfe_win_rate` (wins in group
convention, MFE >= 25 pips by default). Group-to-bot divergence
(`_check_group_divergence`) compares with `mfe_win_rate` when present;
it only alerts if even with group convention the gap stays >= 10pp.

## Part V - Auto-manage (bot managing alone)

`auto_*` variants replace group management with **mechanical rules**
run each tick, always **look-ahead free** (past data only):

1. **Protection ladder** - TP_k confirms -> SL of later legs moves
   to **TP_k level** (idea "take TP3 with nothing to lose");
2. **Trailing** - after TP1: `SL = peak - trail_factor x dist(entry->TP2)`.
   Peak is the max of **prior** ticks (never current). It is
   the path that mirrors group "we're out / move SL" - **~90% of
   edge** of autos;
3. **Confirm filter** - if leg k takes SL, legs > k close
   at market ("if 3 dies, exit all");
4. **Time stop** - legs open N min after TP1 close at market;
5. **Defensive mode** (19/08) - when PF of last `def_window` (10)
   solved trades < 1.0, the strategy moves to tight trail
   (`def_trail=1.5`) + time stop (`def_time_stop=60`) **with no leg cuts**;
   back to normal when PF recovers. History is from its own account
   (paper or real), no look-ahead. Mirrored on 3 engines (backtest /
   paper / MT5 Phase 3) - see study in Part VIII;
6. **ATR-adaptive trailing** (`max(trail_factorxdist, atr_kxATR`) -
   built but **rejected by data**: gold ATR is far bigger
   than the ~8 pip pullback, widens trail and gives profit back to BE (see
   Part VIII).
7. **Hybrid protect** - `auto_escada_3_hibrida` keeps ladder/trailing/
   defensive mode and takes only group `protect` when it is more
   protective than mechanical SL. The call is monotonic (buy: higher SL;
   sell: lower SL), leg 1 only closes if known event price
   already confirmed a no-worse exit, and each call stays in `protect_latency_log`
   on paper/MT5. Backtest returns the same telemetry with no DB write.

Honest study finds (see Part VIII): ladder, filter and time stop
**have no edge alone** (PF 0.3-1.36 with no trailing); `max_legs=2` is worst
(PF 2.20 with trail) - 2 TPs cut the profit engine.

---

## Part VI - Registry strategies, ONE BY ONE

> Result note: "current" = last 30 days of current group format
> (July->today); "old" = Apr-Jul span (old format, out-of-sample);
> "full history" = all signals on hand since start. Sim
> balance 1,000 EUR · base risk 1.0% (with dynamic scale) · daily kill 3% by
> default · real costs. See method (Part VII).

### Strategies removed from registry on 01/09 (full removal)

`tp2` (PF 0.86 · -151 EUR), `tp2_gestao` (loses to `nova_gestao` in
compare), `nova_gestao_3` (cuts legs 4-5 that give profit) and `auto_2`
(worst PF of family in sweep) left menu AND paper on 19/08
(consolidation); on 01/09 they were **erased from the registry** by user
call - they can no longer run nor backtest. The
decision history (PF/DD and why) is in CHANGELOG; old docs that
name them are historic non-run record. Orphan paper data
stays in DB with no readers (data is never erased).

### 1. `nova` - "TP1..TP5" (no management) - control

- **Mechanics**: opens **5 legs** with split risk; SL-first; TP1
  closes leg 1 and moves rest to BE; **applies NO group
  message** (no protect, no close_all - runs to the end or until price
  hits SL/TPs).
- **Params**: `max_legs=None` (5) · `manage=False` · `exit_at_tp=None`.
- **Role**: it is the **control "what if we do nothing"** - the base
  to measure management value. It also feeds the "no management" counterfactual
  of trade history.
- **Results**: no-management baseline - **PF 1.25 · +261.77 EUR** in current
  span (`auto_5` with no trail is exactly the same). Wins little; group
  management multiplies the result (see `nova_gestao`).
- **Verdict**: not a main candidate - it is the thermometer.

### 2. `nova_gestao` - "TP1..TP5 + management" - **MAIN**

- **Mechanics**: same as `nova` (5 legs, SL-first, BE after TP1) **+ live group
  management**: `protect` (closes leg 1 + SL above entry),
  `close_all` ("we're out" - exits all), `close_tp1`/`close_tp`, `move_sl`,
  TP/SL hits. The group picks when to guard and when to exit; the bot obeys.
- **Params**: `manage=True` (all kinds) · no auto-manage.
- **Role**: **the strategy the demo account runs** - the best by
  backtest (the bar others must beat).
- **Results**: current **PF 3.28 · +2,369 EUR · DD 22.7%** (dashboard shows
  PF 3.22 · +2,310 EUR, slightly different window). **Old span:
  PF 0.63 · -2,622 EUR (LOSES)** - group-management edge only exists in
  current format; in the old market human management did not save it.
- **Verdict**: the reference. Honest weak point: it needs the group (and the
  group lost in the old span).

### 3. `tp2_protect` - "Exit at TP2 + protect only" - **test**

- **Mechanics**: SL-first; TP1 closes leg 1 and moves rest to BE;
  **TP2 hits -> all legs close**. Of group management it **only honors
  `protect`** (closes leg 1 + SL above entry) - **ignores
  `close_all`/`close_tp1`/`move_sl`**: the group can say "we're out" and the
  strategy still runs to TP2.
- **Params**: `exit_at_tp=2` · `manage_kinds={"protect"}`.
- **Role**: Phase 2 test (paper run) - the ask "is early exit with only
  group guard worth it?".
- **Results** (30d study, current format): **pure tp2 PF 0.86 · -151 EUR** ·
  **+ protect PF 2.65 · +1,721 EUR** · full management PF 3.22 · +2,309 EUR. The
  protect recovers ~86% of the path to full management.
- **Verdict**: early exit ONLY pays with group guard; even so full
  management (`nova_gestao`) still wins. In live check.

### 4. `auto_3` - "Auto 3 TPs"

- **Mechanics**: **3 legs**, BE after TP1, no group management and no
  set auto-manage.
- **Params**: `max_legs=3` · `manage=False`.
- **Role**: bare min "only 3 TPs" (legs 1-2 lose; profit
  lives in 3-5).
- **Results**: in sweep WITH trail: **PF 3.03 · +1,723 EUR · DD 31.2%**
  (current) · PF 1.38 · +2,162 EUR · DD 48.4% (old). With no trail (set)
  it would be weak.
- **Verdict**: with no trailing it has no edge; trailing is the path
  (see `auto_escada_3`, the ladder+trail version).

### 5. `auto_5` - "Auto 5 TPs"

- **Mechanics**: **5 legs**, BE after TP1, no group management and no
  set auto-manage - **it is exactly `nova`** (no-management baseline).
- **Params**: `max_legs=None` (5) · `manage=False`.
- **Role**: the auto-family baseline (copy of `nova`).
- **Results**: in sweep WITH trail: **PF 3.50 · +2,613 EUR · DD 42.6%**
  (current) · PF 1.46 · +3,579 EUR · **DD 84%** (old) - with trail it wins big
  but with huge drawdowns.
- **Verdict**: no trail = `nova`; with trail = big profit but 84% DD on
  old (too much tail risk - so the pick was `auto_escada_3`).

### 6. `auto_escada_3` - "Auto 3 TPs + ladder" - **test · Phase 2** ⭐

- **Mechanics**: **3 legs**; BE after TP1; **protection ladder**
  (`stair_levels=3`, `stair_from=1`: TP_k confirms -> SL of next ones moves
  to TP_k level); **trailing `trail_factor=2.0`** (about 8 pips on gold,
  the honest tuned value); **defensive mode** (PF of last 10 trades
  < 1.0 -> trail 1.5 + 60 min time stop + max 1.25% risk, with no
  leg cuts); no group management.
- **Params**: `max_legs=3` · `stair_levels=3` · `stair_from=1` ·
  `confirm_filter=True` · `trail_factor=2.0` ·
  `def_window=10 · def_trail=1.5 · def_time_stop=60 · def_risk_pct=1.25` ·
  `manage=False`.
- **Role**: **the Phase 3 candidate** (auto-manage on MT5) - in paper
  run for check (N >= 20 signals; gates: PF >= 1.5 · P&L >= 50% of reference
  · DD <= 125%).
- **Results of Item 3** (sweep `--item3-sweep`, same candles/costs,
  19/08): in **current** span, `confirm=False` gave **PF 1.27 / DD 11.8%**
  and `confirm=True` **PF 1.26 / DD 11.8%** (relative PF drop 0.79%);
  on **full history**, 0.47 / 322.0% vs **0.53 / 286.1%**; on
  **old**, 0.40 / 312.9% vs **0.46 / 278.3%**.
- **Verdict**: tail DD fell and current PF lost under 10%,
  so `confirm_filter=True` passed the bar and froze as
  default of this strategy. The change is only the auto on paper/backtest;
  it neither promotes nor changes main `nova_gestao`.

### 7. `auto_escada_5` - "Auto 5 TPs + ladder + confirm"

- **Mechanics**: **5 legs**; protection ladder (TP_k -> SL of next ones
  at TP_k level); **confirm filter** (leg k takes SL -> legs > k
  close); trailing 2.0; **defensive mode** (PF(10) < 1.0 -> trail 1.5 +
  60 min time stop + max 1.25% risk); no group management.
- **Params**: `max_legs=None` (5) · `stair_levels=5` · `confirm_filter=True`
  · `trail_factor=2.0` · `def_window=10 · def_trail=1.5 · def_time_stop=60 ·
  def_risk_pct=1.25` · `manage=False`.
- **Role**: the more aggressive variant (more legs = more profit room,
  more tail risk).
- **Results**: current **PF 3.66 · +2,787 EUR · DD 35.9%** · old
  **PF 1.43 · +3,356 EUR · DD 86.4%** - best absolute PF but with
  **86% drawdown on the old span** (near ruin).
- **Verdict**: worse trade than escada_3 (like profit, far
  bigger DD) - stays on menu for live compare.

### 8. `auto_escada_3_hibrida` - "Auto 3 TPs + ladder + hybrid protect" - **trial**

- **Mechanics**: 3 legs, ladder from TP1, trailing `trail_factor=2.0`
  and defensive mode (PF(10) < 1.0 -> trail 1.5 + 60 min time stop + max
  1.25% risk). When group `protect` arrives, it compares group SL with mechanical SL per leg
  and only applies the more protective level; it never walks back an SL already won by
  auto. Leg 1 only closes when known event price already confirmed
  TP1 and `protect` is no worse than auto guard.
- **Params**: `max_legs=3` · `stair_levels=3` · `stair_from=1` ·
  `trail_factor=2.0` · `def_window=10 · def_trail=1.5 · def_time_stop=60` ·
  `manage_kinds={"protect"}`.
- **Role**: own paper account and test page to check the
  "mechanical auto + human timing" claim. It does not replace `nova_gestao`, is not
  eligible for auto-switch and its telemetry must collect at least 20
  `protect` events before a call.
- **Report**: the **Hybrid protect - auto vs group latency** table shows
  winner, pip gap, delay and leg-1 action; backtest uses the
  same timeline with no look-ahead. `GET /api/analysis/protect_causal` also
  credits by prefixes (no-protect baseline -> each event) and shows
  worst deltas.
- **Item 1 checked and closed (19/08)**: on the full-history snapshot
  hybrid tied pure auto (**PF 2.21 · DD 32.5%**); on the 30-day
  cut, both hit **PF 3.20 · DD 24.6%**. Causal check found
  **355 events on 270 signals, 0 harmful and 0 EUR delta** - `protect` calls did not
  cause seen losses. Buy/sell parity across backtest,
  SimEngine, PaperRace and MT5 dry-run is in `tests/test_engine_parity.py`.
  Spec `docs/Melhorias/01-item-01.md` was deleted only after this check;
  hybrid stays trial and is not eligible for auto promotion.

### ~~9. `auto_protect_5` - removed on 01/09~~ (history in CHANGELOG)

> **Removed from registry on 01/09** (user call - full removal).
> Early-guard claim with no out-of-sample strength: **PF 1.27 ·
> +EUR 281.41 · DD 38.6%** vs `auto_escada_5` PF 1.33 · best current of sweep
> (1.0/0.20) had PF 1.96 but best old PF 0.50 · -EUR 3,450.62 - no
> robust combo. The path (`protect_trigger`/`protect_buffer`) stays in
> engines for future candidates; backtest of this variant is no longer
> reproducible.

---

## Part VII - Result method (so numbers can be trusted)

- **Data**: real on-hand Golden Moves signals in DB since group
  start (exact count shows in snapshot/report and can grow when
  new history arrives), with the 3 message formats the group used over
  time. Old history (Apr-Jul) is **out-of-sample**:
  tuned on current format and checked on old.
- **Simulation**: replay of signals on **real candles** (history prices),
  with SAME production rules: SL-first, look-ahead free (never uses
  event-candle close), risk per signal **1.0% by default** (dynamic
  scale possible) split across legs, **real costs** (spread,
  slippage, commission), contract per symbol (XAUUSD 100, XAGUSD 5000, forex
  100000), **daily kill** (day loss > **3% by default**, settable ->
  rest-of-day trades do not open - production mirror).
- **Sweeps**: params (trail 1.5-2.5, ATR) ran in grid
  to spot **overfit**: a smooth curve with no lone spike =
  honest param; a lone spike = luck. Trail 2.0 sits mid
  smooth plateau (PF 4.69->1.97, no jumps).
- **Stats check**: small samples (< 10-20 trades) are noise -
  so gates need N >= 20 and the auto verdict only calls with 10
  real trades.
- **Live panel**: the paper run (virtual 1,000 EUR accounts, all
  real signals from today) is the forward test - numbers in this
  document are the past; the run is the future.

---

## Part VIII - How to review (read guide)

A strategy is good if:

1. **PF >= 1.5 on current span AND not a loser on old span**
   (robustness - the group loses on old, auto_* win);
2. **Capped DD** (DD <= ~40% on old; DD > 80% = tail risk);
3. **Smooth sweep** on params (no luck spike);
4. **beats reference `nova_gestao`** (PF 3.28 current / 0.63 old) in
   backtest AND live paper run;
5. **N >= 20 signals** in the run before any call.

**Where they stand today (19/08, consolidation)**: `nova_gestao` runs the demo
account; `auto_escada_3` and `tp2_protect` are in paper run (Phase 2);
menu keeps **6 visible** (`nova_gestao`, `tp2_protect`,
`auto_3`, `auto_escada_3`, `auto_escada_3_hibrida`, `auto_escada_5`,
`auto_protect_5`); `nova` and `auto_5` stay on PAPER but
hidden from menu ("no management" counterfactual + 5-leg with/without
trail compare - `MENU_HIDDEN_STRATEGIES`); `tp2`, `tp2_gestao`, `nova_gestao_3` and
`auto_2` left menu AND paper (`HIDDEN_STRATEGIES`) - stay only in
backtest/compare, data is never erased. When `auto_escada_3` passes gates
(N >= 20 · PF >= 1.5 · P&L >= 50% · DD <= 125%), it moves to Phase 3 (MT5
demo, 10-real-trade check window with auto verdict).
Real account: never with no explicit instruction (SPEC v3, draft).

**Consolidation study (19/08, `app/study_consolidation.py`)**: tested the
entry filter (London/NY session + entry-to-TP2 gap >= 5 pips + ATR
p20) and defensive mode (PF of last 10 trades < 1.0 -> trail 1.5 + time
stop 60 min, with NO leg cuts) on 3 windows:

| Variant | Current (PF/DD) | All (PF/DD) | Old (PF/DD) |
|---|---|---|---|
| auto_escada_3 baseline | 4.95 / 11.8% | 2.13 / 38.0% | 1.41 / 35.2% |
| + entry filter | 6.29 / 9.4% (rej 8) | 2.15 / 42.8% (rej 32) | 1.50 / 33.4% (rej 180) |
| + defensive mode | 4.97 / 11.8% (5 on) | 2.20 / 32.5% (224) | 1.48 / 32.5% (219) |
| auto_escada_5 baseline | 5.73 / 15.8% | 2.25 / 87.1% | 1.43 / 86.4% |
| + entry filter | 7.33 / 12.5% | 2.32 / 68.4% | 1.50 / 68.7% |
| + defensive mode | 5.95 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |
| auto_3 baseline | 1.25 / 11.8% | 0.44 / 339% | 0.37 / 330% |
| + entry filter | 1.53 / 9.4% | 0.44 / 327% | 0.37 / 231% |
| + defensive mode | 1.68 / 11.8% | 0.97 / 88% | 0.86 / 87% |

**Honest verdict**: the **entry filter has NO edge** - it rejects
almost only on the old span (where ALL loses, so cutting proves nothing; the
profit drops), session filters nothing on current (the group already trades in active
hours) and ATR rejects 8 signals on current (noise). **ATR-only** sensitivity
rejects **0 signals** (full-filter rejects were all session -
proof ATR filters nothing alone). **NOT built.**

On `auto_escada_3` the base study found a small steady effect
(lower DD with same or better P&L). **On `auto_escada_5` this did not hold
robustly**: the Item 2 sweep, with clean baseline,
shows old DD still over 84% on the whole grid and a lone PF spike
on the `def_window` axis. The wanted effect would be time stop + tight trail
in bad regimes - NOT leg cuts (`max_legs=2` of the first spec
was rejected: the sweep already proved 2 legs cut the profit engine).
**Built (19/08)**: defensive mode (trail 1.5 + 60 min time stop
when PF(10) < 1.0, with no leg cuts) is on `auto_escada_3/5` -
`def_window`/`def_trail`/`def_time_stop` params in registry, mirrored
in backtest (`simulate_signal`), paper run (`PaperRace`, virtual
account) and real MT5 (`_update_real`, Phase 3, closed real trades), with paper-run check before Phase 3.

### Item 2 - defensive sweep of `auto_escada_5` (19/08)

The reproducible sweep runs with:

```text
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation --item2-sweep
```

The study entry filter keeps the no-leak promise: tz offsets convert to UTC, daily ATR uses the last reading of each
UTC day and each signal p20 floor only reads prior days; historic
tz-free stamps still read as bot local time.

The full grid is `def_window` **5/7/10** x `def_trail` **1.0/1.2/1.5**
x `def_time_stop` **30/45/60** (27 combos), on the same
**current / all / old** windows. Baseline is set once per window and the
baseline history is reused on all variants, no leakage. The three
time stops gave the same numbers in this replay, so the table below
shows one row per window/trail combo and marks `30=45=60`.

Baseline: current **PF 5.73 / DD 15.8%** · all **PF 2.25 / DD 87.1%** · old
**PF 1.43 / DD 86.4%**.

| `def_window` | `def_trail` | `def_time_stop` | Current PF/DD | All PF/DD | Old PF/DD |
|---:|---:|:---:|---:|---:|---:|
| 5 | 1.0 | 30=45=60 | 6.72 / 15.8% | 2.31 / 84.5% | 1.49 / 85.5% |
| 5 | 1.2 | 30=45=60 | 6.26 / 15.8% | 2.30 / 84.8% | 1.48 / 85.8% |
| 5 | 1.5 | 30=45=60 | 6.25 / 15.8% | 2.28 / 86.3% | 1.47 / 86.1% |
| 7 | 1.0 | 30=45=60 | 6.98 / 15.8% | 2.34 / 84.5% | 1.49 / 85.5% |
| 7 | 1.2 | 30=45=60 | 6.51 / 15.8% | 2.33 / 84.8% | 1.48 / 85.8% |
| 7 | 1.5 | 30=45=60 | 6.50 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |
| 10 | 1.0 | 30=45=60 | 6.42 / 15.8% | 2.34 / 84.5% | 1.49 / 85.5% |
| 10 | 1.2 | 30=45=60 | 5.95 / 15.8% | 2.33 / 84.8% | 1.48 / 85.8% |
| 10 | 1.5 | 30=45=60 | 5.95 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |

**Item 2 verdict**: **0 eligible combos**. None cuts old DD
below 50% (best stays ~84.5%); current PF loss would not be the
issue, but tail risk stays. Smoothness is green on
`def_trail`/`def_time_stop` axes and on all/old windows; on the current-span `def_window` axis
there is a lone spike (mean PF **6.41 -> 6.66 -> 6.11** for
5->7->10). So report the sweep, **change no default** and
do not promote `auto_escada_5` to the MT5 account. Spec
`docs/Melhorias/02-item-02.md` was deleted after the sweep and verdict
were filed.

**Read-only rerun of 19/08**: with current DB, `auto_escada_5` baseline
hit **PF 1.95 / DD 15.8%** on the current window, **PF 0.70 / DD 310.9%** on
full history and **PF 0.58 / DD 302.4%** on the old span. The 27
replayed combos tied these values and the verdict kept
**0 eligible**; this reading only replaces snapshot numbers above,
it changes neither defaults nor account setup.

**Item 3 closed (19/08)**: the read-only sweep of `auto_escada_3` compared
`confirm_filter=True` vs `False` on current/all/old windows, with
`stair_levels=3`, `stair_from=1` and `trail_factor=2.0` fixed. The bar passed:
old DD fell from 312.9% to 278.3% and current PF only fell from 1.27 to 1.26
(0.79%, under the 10% relative cap). The registry moved to
`confirm_filter=True`, with no main-account promotion; spec
`docs/Melhorias/03-item-03.md` was deleted after tests and
accept.

**A/B/C of the 5 visible base strategies on full history** (19/08,

579 signals, start
balance 1,000 EUR, config risk/daily kill - same run as test pages
with `days=0`):

| Strategy | Signals | Solved | PF | P&L EUR | WR% | DD% | Ruin |
|---|---|---|---|---|---|---|---|
| `nova_gestao` (reference) | 579 | 382 | 1.00 | +20 | 49.5 | 252 | yes (dies) |
| `tp2_protect` | 579 | 380 | 0.82 | -1,399 | 49.5 | 330 | yes (dies) |
| `auto_3` | 579 | 444 | 0.43 | -3,207 | 49.5 | 343 | yes (dies) |
| `auto_escada_3` ⭐ | 579 | 482 | 2.10 | +6,863 | 48.3 | 24.3 | no |
| `auto_escada_5` | 579 | 405 | 2.30 | +11,063 | 46.9 | 25.8 | no |

**Honest read**: on FULL history (Apr->today, which joins the
old loser span to the current format) only **auto_escada_3/5 have edge** - the
rest die in ruin (DD > 100% because sim keeps risking a fixed %
down to zero balance). `nova_gestao` ends flat (+20 EUR)
because the old span loses as much as the current wins - that is why
Phase 3 rules review `auto_escada_3` on fresh windows (N >=
20 · PF >= 1.5 · P&L >= 50% of reference · DD <= 125%) and not on full
history. `tp2_protect` and `auto_3` stay visible as live
tests (group guard and min trail may pay in a fresh
regime - 30 days: PF 2.65 and 3.03 each), but full history
shows they do not live through long bad regimes.

---

## Part VIII-A - Extra decision studies (19/08)

Reproducible studies live in `app/study_strategies.py` and are read-only:

```text
.venv/Scripts/python -m app.study_strategies --study ablation
.venv/Scripts/python -m app.study_strategies --study walkforward
.venv/Scripts/python -m app.study_strategies --study legs --balance 10000
.venv/Scripts/python -m app.study_strategies --study fallback
.venv/Scripts/python -m app.study_strategies --study polling
```

On current DB (30 days, 107 signals, 1,000 EUR balance, 1% risk, 6% daily kill,
15m candles), ablation found:

| Variant | PF | P&L EUR | DD |
|---|---:|---:|---:|
| no management | 1.25 | +261.77 | 42.8% |
| full management | **3.34** | **+2,438.76** | **22.7%** |
| `protect` only | 3.09 | +2,177.62 | 22.7% |
| `close_all` only | 1.54 | +569.21 | 42.8% |
| `move_sl` only | 1.25 | +261.77 | 42.8% |
| `tp_hit` only | 1.25 | +261.77 | 42.8% |

The read is causally useful, but proves no statistical split: the
`protect` is the biggest lone path; `close_all` helps little with no `protect`,
and `move_sl`/`tp_hit` did not move this sample. Full-management value is
mainly the mix of events and group time link.

Tuned walk-forward (3 folds) picked `all -> protect -> all_fallback_60`
only on the prior block; the next three blocks were all green
(+61.27 EUR, +821.29 EUR and +1,513.26 EUR), but the last has only 26 signals. Fallback
had exactly the same result as `all` on the block where it was picked.
This backs more research, it does not allow swapping main.

The wider check, always picking on train and testing on the next
block, read:

| Span | Signals | Picks | Green test folds | Result |
|---|---:|---|---:|---|
| current 30d | 107 | all -> protect -> all_fallback_60 | 3/3 | steady only in the fresh regime |
| last 90d | 397 | all -> all -> all | 1/3 | not robust; old span weighs against |
| full history | 578 | all -> all -> all | 1/3 | not robust |
| old OOS | 506 | all -> all -> all | 2/3 | old group still a loser |

On 90-day, full and old spans, `all_fallback_60` did not move
numbers vs `all`. Sound management today is thus
**sound inside the fresh format**, but is no cure-all for
the old regime; main stays put.

Light risk shift to TP3-TP5 changes nothing on a 1,000 EUR balance:
0.01 min lot rules all legs. In a split study with 10,000 EUR
balance, `tp345_light` gave PF 1.23 / +875.96 EUR / DD 13.5%, vs PF 1.17 /
+598.06 EUR / DD 12.7% on equal weights. It is a live claim, but not
directly comparable to the current account and was not promoted.

Mechanical fallback after TP1, if no management arrives in 30/60/120s, applied
on only 1 of 107 signals: P&L +2,438.76 EUR with no fallback vs +2,439.66 EUR
with 30s and +2,439.66 EUR with 60/120s. The 0.90 EUR gain is noise; it stays off
by default.

The lone paper-engine benchmark ran the same fixed path at
1s, 250ms and 100ms. On both `auto_escada_3` and `auto_escada_5`, 1s missed the
spike and left the trade open; 250ms and 100ms closed, but with different
reasons/P&L. This proves polling sensitivity, not monthly edge: the
path is synthetic, uses a temp DB and does not touch the production loop.
Since this check, the benchmark uses `ManualClock`: each sample moves
the clock plainly by the picked gap, with time stops and
exit stamps. Production stays near 1s and uses `SystemClock`; there is
no auto promotion from this test.

The call edge was also hardened: `reduce_auto_protect()` in
`app/execution/protection.py` computes trigger, group
guard and monotonic SLs once. Backtest, paper, SimEngine and MT5 apply the same
result on their own fills/broker. So a future threshold change
can run in parity with no formula copied in four
engines.

**R4 study - "struggle" rule (20/08)**: candidate `auto_escada_3_struggle`
(= auto_escada_3 + `struggle_ae_pips=15` + `struggle_fraction=0.5` - before
TP1, if price ran 15 pips against entry and comes back to entry, it closes 50%
of legs at BE and guards the rest) ran on the same signals/candles
as base. The **full sweep** (`app/sweep_struggle.py`, read-only) ran
grid `struggle_ae_pips` 5/10/15/20/25 x `struggle_fraction` 0.25/0.5/0.75
on `auto_escada_3` and `auto_escada_5`, on current and full windows. The rule
never fired in the current span (0 signals) and only on 3 signals of full
history; all combos read near same as baseline
(current PF 1.26->1.26 and same 11.8% DD; on full PF 0.53->0.53-0.54 and DD
within 3 pp, P&L within 77 EUR). **No edge in any combo - the candidate was
removed from the registry on 01/09** (not promoted and never back with no new signals
where the "ran against then came back" shape is more common). The path
(`struggle_ae_pips`/`struggle_fraction`) stays on in engines for
future candidates.

---

## Part IX - Where each strategy shows

| Strategy | Page | Paper run | Backtest/compare | Real account |
|---|---|---|---|---|
| `nova_gestao` | MAIN | ✅ (reference) | ✅ | ✅ demo (main) |
| `nova_gestao_fallback` | dropdown menu (`/teste?s=nova_gestao_fallback`) | ✅ trial | ✅ | 🔒 only with check |
| `tp2_protect` | dropdown (`/teste?s=tp2_protect`) | ✅ | ✅ | 🔒 only with check |
| `auto_escada_3` | dropdown (`/teste?s=auto_escada_3`) | ✅ | ✅ | 🔒 only with check |
| `auto_escada_3_hibrida` | dropdown (`/teste?s=auto_escada_3_hibrida`) | ✅ trial | ✅ | 🔒 not eligible with no explicit check |
| `auto_3`, `auto_escada_5` (backtest refs) | dropdown (own test page) | ✅ on paper (not on curve) | ✅ | 🔒 |
| `nova` (counterfactual), `auto_5` | 🔒 hidden from menu | ✅ on paper (not on curve) | ✅ | 🔒 |
| ~~`tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5`, `auto_escada_3_struggle`~~ | ❌ removed from registry on 01/09 | ❌ (orphan data in DB with no readers) | ❌ no longer reproducible | 🔒 |

Strategies with their own page have sim balance (1,000 EUR), curve,
compare and real 30-day numbers; the **HELP** page (`/help`)
explains them with live cards. The main **👻 Shadow-paper** panel
lets you pick visible IDs that take forward signals; the
`shadow_paper` snapshot shows apart open trades (`origin="shadow"`) and the
balance/PF of that origin never mixes with the normal run; the mode can turn
off with no paper-history erase. Evidence bar
stays N>=20 solved signals - no call on thin data.
The registry (`app/strategies.py`) is the single source of truth: those removed on
01/09 no longer exist in it (`get_strategy` raises ValueError for dead ids
- a study with an erased strategy fails loud, never runs
quietly on main); `MENU_HIDDEN_STRATEGIES` = `nova`/`auto_5`
(hidden from menu but on paper, for trade-history counterfactual and
backtest compare).

---

## Part X - Phase 3 (MT5 demo) and SPEC v3 (real account) - promotion path

> Folded from `docs/SPEC-auto-gestao.md` (deleted 19/08 - this document
> is now the single source on strategies). Phase 3 is the path of
> `auto_escada_3` from paper run to the **demo MT5 account**; SPEC v3 is
> only a draft of gates for the real account - **never** touch real with no
> explicit user instruction.

### Frozen setup of `auto_escada_3` (Phase 2/3)

| Param | Value |
|---|---|
| `max_legs` | 3 |
| `stair_levels` / `stair_from` | 3 / 1 (ladder from TP1) |
| `confirm_filter` | true (Item 3, sweep 19/08: old DD 312.9% -> 278.3% and current PF 1.27 -> 1.26) |
| `trail_factor` | 2.0 (mid of 1.5-2.5 plateau - smooth, no lone spike) |
| `def_window` / `def_trail` / `def_time_stop` | 10 / 1.5 / 60 (defensive mode) |
| `time_stop_minutes` | 0 (only defensive turns it on) |
| risk / daily kill | 1.0% (config) · `daily_stop_pct` on (paper and real) |
| reference | `nova_gestao` in parallel (same virtual 1,000 EUR account) |

`confirm_filter=True` froze as default after Item 3; paper/MT5 window still needs Part X gates before any promotion.

**Do not change during the run**: params, entry/exit rules,
costs, signal filter.

### X.1 Promotion path to MT5 (demo account)

> Golden rule: **demo** account (`5054644473 · MetaQuotes-Demo`). Never
> promote to real with no explicit instruction (SPEC v3, below).

**Pre-reqs (gate - do not skip):**

```text
1. Approval bars with N >= 20 solved signals in the paper run:
   PF >= 1.5 · P&L >= 50% of reference · DD <= 125% - **all 3 at once**
   (strict gate, no 2-of-3 board; user call of 19/08: with
   N=20 noise does not warrant slack - high DD is exactly the
   risk sign the gate must stop, even with good PF and P&L);
2. No early gates: early ruin (PF < 1.0 with N >= 10 -> end)
   or hot streak (never promote before N >= 20);
3. All paper vs backtest splits explained (spread/slippage/timing);
4. Zero spots with no valid SL and zero duplicate orders in paper.
```

**MT5Engine already has all built** (checked; off until promotion):
ladder/trailing/confirm-filter/time stop in `_update_real`, modify
throttle (>= 1 pip, <= 1x/min), three SL levels (`target->valid->applied` with
retcodes 10009/10016 + `autosl:<trade_id>` audit), defensive mode (PF of last
10 closed real trades < 1.0 -> trail 1.5 + 60 time stop), `risk`
warn on feed/Telegram when a modify is refused/clamped, E2E test with
fake broker. ATR trailing (`max(trail_factorxdist, atr_kxATR)`) is
built in 3 engines but **rejected by data** (sweep 18/08: ATR is
strictly worse than fixed trail 2.0 - gold ATR widens trail and
gives profit back) - stays off, no strategy sets `atr_k`.

**Check window (first 10 real trades of the auto on demo):**

```text
Keep it if, on the first 10:
- same exit call as paper on same signal on >= 9/10;
- zero spots with no SL; zero duplicate orders; retcodes 10009;
- signal-to-open lag <= ~2 min; P&L in line with backtest.

Auto rollback (back to nova_gestao and reopen window later):
- PF < 1.0 on first 10; >= 2 structural splits (different exit
  with no market reason); any spot with no SL; steady 10027.
```

**Automated**: `state._check_fase3_validation` calls rollback/keep
once per window (new promotion writes a fresh snapshot and reopens), notes
feed/Telegram and stores verdict in `fase3_verdict` (+ history
`fase3_verdicts`, never erased). The window covers only the first 10 trades
opened by the strategy after `promoted_at`; keep needs paper-to-MT5 evidence with same exit on at least 9/10 - missing
pairs also fail. The Phase 3 panel shows gates, ETA
of 20 signals (`fase3_eta`, last-30-day pace + alert when > 6
weeks or pace moves >= 50%) and the "🎯 Cut target to 15"
button (`config fase3_target`) when ETA blows. During Phase 3, paper
run keeps going in parallel (auto paper is the theory ref of auto
MT5) and the daily summary shows the 🚀 line of auto MT5 (real N, real P&L).

### X.2 SPEC v3 - Promotion to REAL account (DRAFT)

> ⚠️ Draft of intent, NOT an approved plan. Only touch the real account
> with an explicit user instruction, and the path there runs
> WITHOUT EXCEPTION through Phase 3 (demo) with a clean window.

**Absolute pre-reqs (gate 0):** Phase 3 passed (clean 10-trade window) · 3-4 demo weeks (>= 30 trades) with no rollback · zero unexplained
risk alerts · EXPLICIT user call (no automation
promotes real) · docs + spec reviewed with real numbers.

**Ongoing gates (review each 10 real trades):** PF >= 1.5 and
P&L >= 0 on rolling 20 window · DD <= 125% of same-window backtest ·
demo vs real splits explained · daily kill ALWAYS on · if PF < 1.0
on 10 straight trades -> back to demo and review.

**Red line (never on real):** risk more than approved ·
turn off daily kill or `max_positions` · promote another strategy with no
same process · leverage/hedging off approved. The bot stays
ALWAYS on demo until the user orders.

---

*End of document - single source on strategies (registry, auto-manage,
results, promotion path). History of done auto-manage phases
lives in CHANGELOG; fine tune (mining, sweep)
in scripts `app/mine_management.py` / `app/sweep_auto.py` / `app/sweep_atr.py`
/ `app/study_consolidation.py`.*
