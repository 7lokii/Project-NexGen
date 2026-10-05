# Project NexGen - What is this? (for non-programmers)

This document explains the project in plain language, no code. If you are
Ruben or Miguel, start here.

The evidence tool also records, when allowed, what the broker
answered: open/close fills, order retcodes and stop changes,
tickets and execution times. The **Audit** page shows the daily quality
of that data, compares each fill with a safe replay and points to the first
mismatch. It also shows a 30-day gate for the auto-ladders in
shadow-paper, with at least 20 trades and explicit statistical rules.
This improves audit, but stays off by default and does not turn
a replay into a real order nor promote a strategy alone. On the
**Audit** page you can download the report in JSON/CSV and archive the forward window
to send or check later. The daily summary shows in the feed and
on Telegram how many events are complete and how each auto-ladder advances.
While any gate rule is missing, the **Promote** button shows locked;
the server also refuses promotion, even if someone tries it by API.

---

## In one sentence

A program that **reads the signals from the Telegram group**, quickly checks if the
incomplete message was **edited** and then **trades on its own** - always
in automatic mode, with risk checks always on (there is no
manual mode).

> ⚠️ **Important and honest:** the project supports simulation/paper and execution
> on an **MT5 demo** account. The VPS server can send orders to the demo;
> this is not real money, but still needs the same care. A
> real account is never enabled without an explicit instruction.

---

## What the program does, step by step

### Technical validation without touching the account

There is also a study tool called **deterministic replay**. It
takes a sequence of signals, prices, management messages and, when present,
broker evidence (fills, retcodes, stop changes and latency). It repeats the
decision events on the three internal paths (simulation, paper and MT5 in
MT5 dry-run) and compares decisions, stops, exit reason and profit. Broker evidence
is only passive in the study; it never forces a result. It runs on temp
databases: it creates no production trades, sends no orders and does not change the
main strategy.

In trade history there is also the **replay** button. It collects the tape of
that trade from the journal (signal, prices and management), runs the same
sequence on the three engines and shows if there were step-by-step divergences. It is an
audit tool: still read-only, sends no MT5 orders and touches neither
balance nor the main strategy.

1. **Gets the signal** - either you click a demo button on the dashboard, or
   the program **reads the Telegram group** (see guide below).
2. **Understands the signal** - the program "reads" the message and pulls the data:
   which pair (e.g. EURUSD), buy or sell, entry, stop loss and
   targets (**TP1 to TP5**, as the group sends). It works in Portuguese
   and English.
3. **Waits for edits (10 seconds by default)** - an incomplete message
   may be fixed by the group right after it is sent. The clock restarts
   when an edit arrives. An already complete signal does not wait: it goes
   straight to risk checks (data-driven decision -
   see "Edit window"); the value is set on the dashboard
   (`edit_window_seconds`).
4. **Checks risk** - before any trade, it confirms all is
   within limits (see below).
5. **Decides** - it trades alone: only automatic mode exists (manual mode
   was removed). Signals blocked by entry-zone/min-lot protections stay `pending` for explicit
   operator confirmation on the dashboard.
6. **Reacts to group instructions** - besides signals, the program reads the
   **management** messages of the group and acts on them alone (see section below).

---

## The "N entries" strategy (TP1 to TP5)

Each signal opens **one position per target** the group sends (up to 5):

- The **first** closes at **TP1** (smaller profit);
- The **next ones** keep running to their targets (**TP2, TP3, ...**) - bigger
  profits, but only if price gets there.

And there is an important guard: **when the first closes at TP1, all
remaining ones move stop loss to entry** (they go "flat"). So
even if price comes back, those positions lose no money - each one
then runs to its own target.

The group also **gives live instructions** and the program obeys:

| The group says... | The program does... |
|---|---|
| "TP1 HIT" | Moves stop loss of remaining positions to entry (they stay protected) |
| "close first entries and move stop loss above best entries" | Closes the **first position** (locks profit) and moves the stop of the rest to **above entry** |
| "Close TP1 here" (without "move stop loss") | Closes only the first position; the rest keep the stop |
| "Adjust/Move SL to X" | Moves stop loss of **all** open positions to the given level |
| "TP1 4059 SL 4047" (as reply to a signal) | Fixes signal levels (group edit/confirm) |
| "we're out" | Closes **all** at current price; "out at BE" protects remaining legs at entry (does not force an immediate close) |
| "SL HIT" | Logs and reconciles with the broker |
| Daily/weekly summary | Stores the win rate the group reports (see "test pages") |
| "Correction: ... Trades Sent ..." | Updates the original summary when the message replies to it (or points to a single period) and re-compares numbers with the bot |
| "Signal #1: Sell at 4357 ... +110 pips" | Stores the **per-trade recount** and compares it with what the bot really closed that day (entry and pips) - warns you if they differ |

These messages are handled **at once** (they do not wait for the edit-window minute -
it would be too late) and show in history with the 📨 icon.

**And does the program know which signal the message answers?** Yes. When the group
replies to a message on Telegram (e.g. "SL HIT" as reply to the buy
signal), the program captures that link and acts on the trade **of that signal** -
even with several open trades of the same pair. If the message arrives before
the trade opens (signal still waiting for edits), the instruction is stored
and applied automatically when the trade opens.

> Note: total risk per trade is split across the N positions - opening more targets
> does not raise what you risk if stop loss hits.

---

## The window to 28 August and promotion

On the main page there is a card **"How much is left to promote
auto_escada_3"**. It joins how many shadow trades already closed, PF, IC95 range,
drawdown and pace/ETA. While any objective rule is missing, the
promotion button stays locked - this does not change the demo account alone.

The same info, including real closed/open trades, P&L, days left
and pace vs the minimum target 10 and goal 20, is at `GET /api/window`.
The window ends at 00:00 UTC on 29/08/2026.

## The risk slider

On the dashboard there is a **risk slider** (e.g. 0.1% to 5%).

It does not pick trade "size" directly - it picks **how much you are
willing to lose if stop loss hits**, as a percent of the account.

Example: with 10,000 EUR and 1% risk, each trade risks at most 100 EUR. If the
stop loss is far, the program uses less "weight"; if it is near, it uses
more. Risk in money is always the same.

**Golden rule:** start at 0.1-0.5% and only raise after seeing results
in simulation. The slider does not guard you from bad signals - it only sets how much it
hurts when they are bad.

---

## The guards (why this does not blow up randomly)

| Guard | What it does |
|---|---|
| **Kill switch** | A red button that pauses all at once. Already open positions keep being managed (so they are not left adrift), but nothing new opens. |
| **Daily loss limit** | If today you already lost more than the limit (e.g. 3%), the program **pauses alone** and warns. Normally it re-arms manually; when a `killswitch_rearm_at` deadline exists, it can re-arm automatically at the set UTC time and warns on feed/Telegram. |
| **Max positions** | Caps how many trades can be open at the same time (default 5; settable). |
| **Explicit confirm (not manual mode)** | Only signals blocked by guards (entry zone / min lot) stay pending for operator confirm - and even that confirm does **not** bypass the global switches (kill switch, daily limit, max positions). |

### The hybrid guard (in test)

There is a trial strategy called **Hybrid auto ladder 3**. It uses
first the automatic guard (ladder and trailing, no need to wait for a person)
and only takes a `protect` message from the group when that message improves the
stop loss. It never steps back a guard already won. The test page
and Report show the winner, delay pips/seconds and causal
credit of each event; the strategy neither replaces the main one nor sends orders
to the demo account without explicit promotion.

The study of the more aggressive variant, **Auto ladder 5**, also runs a safe
grid of 27 combos of defensive mode. As none cut the old-period drawdown
below 50% and an lone spike showed, the bot kept current
values - it did not pick a value just because it won at one point of history.

In Item 3, we tested **auto confirm** of Auto ladder 3 on and
off, on the three history windows, with no data mixed between variants.
Turning it on cut old drawdown from **312.9% to 278.3%** and cut current PF
only from **1.27 to 1.26** (0.79%, inside the 10% cap). So
that strategy now uses confirm by default. This only changes
the test/paper strategy - the `nova_gestao` reference; the demo account uses `tp2_protect` and none
were promoted.

Before an auto strategy can move from the paper run to the MT5
demo account, the program walks a 10-trade real window: it compares the exit
with paper (must match on at least 9), checks all positions
have stop loss, no duplicate orders and that the broker took the modifies.
If a gate fails, the path can roll back to the strategy set on demo. This is
a demo-account guard; it never promotes a real account.

---

## 24/7 operation and safety brakes

- **Hedged MT5 account**: the strategy opens one independent leg per TP. In
  real execution the bot checks `margin_mode=RETAIL_HEDGING`; a netting account
  or unknown mode stays blocked and shows as a red warning, with no
  orders sent.
- **Logs**: `server.log`, `listener.log` and the `.err.log` ones rotate
  automatically by size (10 MiB and 5 copies by default). The caps are
  `SIGNALBOT_LOG_MAX_BYTES` and `SIGNALBOT_LOG_BACKUP_COUNT`.
- **Space and backups**: there is a daily free-space check (10 GiB by default)
  on feed/Telegram. The monthly test
  `.venv/Scripts/python backup_db.py --restore-check` restores the backup to
  a temp DB and checks integrity; a corrupt backup raises an alert.

## Operational health and CI

On the **Report** tab there is the **🩺 Operational health** panel. It joins, with no
effect on trades, recent exceptions (with context), average MT5 spread
per symbol/month, reconnects per week and broker-rejected stop modifies.
When no samples exist yet, the panel says so plainly;
a trend only counts after at least two weeks of data.
The same data is at `GET /api/operations?days=90`.

The project also has CI on GitHub: each push or pull request runs the full
suite and checks doc rules. There is an optional manual test
that makes a deliberate failure only to confirm CI flags it; it
does not run on normal changes.

### Shadow-paper: learn from live signals without opening demo

The dashboard has a **👻 Shadow-paper** panel. When on, each full live
signal opens a copy in a virtual account of the picked strategy - by
default `auto_escada_3` - even if the main account is in **Manual** or
with **Kill switch** on. This collects forward evidence of the
auto with no single order on MT5.

The copy stays apart and tagged `shadow`: it has its own virtual balance,
daily kill and metrics, and changes neither balance, positions, risk nor
execution of the demo account or the normal paper-run account. Signal edits
and group messages like
`TP1 HIT`, `protect`, `we're out`, `move SL` and `TP1 ... SL ...` fixes
also go to shadow paper, with the same Telegram reply link.
Shadow management deliveries and the normal paper-run ones have separate markers,
so a pending instruction is not lost when only one of the accounts already opened.
History replay and AI-parsed proposals do not enter this sample.
You can turn the mode off or pick several strategies in the panel; turning it off
stops new opens, but does not erase shadow trades already stored.

There is also an **optional execution journal** for research: it stores
quotes and management messages in a read-only tape that can replay to
see why engines made a decision. It is off by default,
opens no trades and does not change the normal production range.

## Run mode

The program always runs in **automatic mode**: it does all alone, as long as
it passes risk checks. Guards (kill switch, daily cap,
max positions) are always on - there is no manual mode. Only signals
blocked by entry-zone/min-lot guards stay pending
for explicit operator **confirm** on the dashboard.

---

## Where you see all

The **dashboard** is a web page (on your PC, or later on a VPS reached
by phone), split in tabs. **When the program runs on the VPS
with login on, the dashboard asks for a password** (🔐 login screen) - only
one with the password gets in, and there is a "🚪 Exit" button at the top to
end the session. The **Kill switch** button (pause all) stays always
reachable after you log in.

The dashboard has the **Cyberpunk 2077 style** (neon **purple and red** colors,
tech fonts and a subtle CRT screen effect) - it is the only site theme.
On wide screens, the back shows **animated code terminals** (like an
old monitor) with the **bot theme**: XAUUSD signals with TP1..TP5 and
SL, price ticks, fills and balance/equity, with a reticle in the middle. The
**nav sits in a fixed top bar** (flat items; on phone it
opens with ☰) and the top status bar: badges (mode, strategy,
MT5, execution), online users + clock, **🛑 Kill switch** and
**Exit**. The **live price strip** (GOLD, SILVER, EURUSD...) was
**removed** for a cleaner and faster topbar - live price lives in the
"real prices"/"simulation" badge. You move
with **↑↓ arrows + Enter** (or mouse) between the **main page**
(the MT5 demo account with broker execution, with the active strategy (`tp2_protect`) - **starts from today**,
no past simulation) and the **▚ STRATEGIES dropdown** at the top - home of
the **7 currently visible strategies** (`nova_gestao`,
`nova_gestao_fallback`, `tp2_protect`, `auto_3`, `auto_escada_3`,
`auto_escada_3_hibrida` and `auto_escada_5`), each with its own
simulation page (balance starting 1 month ago with 1,000 EUR, curve,
compare and live paper run). Old loser strategies
(`tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5` and R4 rule
`auto_escada_3_struggle`) were removed from the program on 01/09; `nova` and
`auto_5` stay only as internal backtest refs (hidden from
menu).

On resolve analysis, the bot only calls an M1 result when Yahoo still
keeps the candles (up to about 7 days). For longer spans, the report
plainly shows fallback to 15m/1h; it also compares delays of 0, 8, 60 and
300 seconds. So we do not mix a fresh M1 sample with a full
history.
  The **TP1..TP5 + management** is the **reference** (the active strategy in
config, which the main one runs and which shows in the paper run) - and the bot
shows who is winning live among the visible ones. There is also a **HELP** page (in
menu, right after diagnostics) that explains each strategy with live
cards
(click
to expand: how it works + real numbers of the last 30 days) and a
glossary (TP, SL, breakeven, group management, daily kill,
parallel run). For the full static version (with
hidden strategies and results per window), see
**`docs/ESTRATEGIAS.md`** in the project. In the run, when the auto strategy (Auto ladder
3) and the reference exit **different on the same signal** - e.g. the group
said "exit" and the auto kept going - the feed shows a
**⚖️ Divergence** note with the P&L of each side. The main page has a
**Phase 2 panel** with live auto vs reference balance and PF, the solved-signal count (N) and progress to
approval rules (N ≥ 20 · PF ≥ 1.5); the **Report** tab shows the table of
all per-signal divergences (exit reason and P&L of each side) and the
**⚡ Real vs backtest** table - when the demo account closes trades, each one
shows compared with the same-signal backtest (entry diff,
real price vs simulator fill, and exit, real close vs expected
level). The **daily summary** (at the set hour) also has Phase 2 run state:
auto vs reference balance and PF and how many
signals are left for the rules. Each Phase 2 test page also has
an **"⬇️ Export CSV"** button that downloads the paper trades of the
strategy and reference (prices, timestamps, SLs, exit reason)
for analysis outside the dashboard. The **TP1->BE + exit at
TP2** page also shows a **real trade example**: the chart with the
price path, entry, SL, TP1 and TP2, and the timeline of
where each event would have happened.
Each test page has an **"⬆️ Promote to main"** button to move
that strategy to the real account (on the active strategy page the
button does not show - it is already main); the main one shows a **badge with
the active strategy** (with glow when it changes) which also states **which
params run in real execution** (leg count, close at TP, fixed lot
and group management) and a **red warning if any test strategy
is beating main** in the paper run. When you promote a
strategy, a **feed note** stays saying exactly what
changed in real execution. If the stored strategy no longer exists (it was
removed), the badge warns in **red** "orphan config" and shows which
fallback strategy runs - it never misleads.
On the **🔧 Diagnostics** tab there is also **"📚 Last history
replay"**: the date and total messages/signals/management/ignored the
bot forwarded at last startup.
Each test page shows **two views**: the **last
month** simulation (7/30/90-day picker) and the **"📜 Since group start"** block
- the same replay over **all** signal history the bot knows -
plus the **paper trades table** of the parallel run (each closed
trade with its P&L, symbol, entry and lots). The **compare on each
page** now draws 3 strategies on the same signals - **TP1..TP5,
TP1..TP5 + management (main) and close at TP2** - with the P&L of each
in the legend, to see what group management would have given vs the others.
On the **🔧 Diagnostics** tab there is also a "🧹 Clear restarts" button (with
confirm) to wipe by hand the listener restart history, and a
**"⏱️ Group edit delay"** block that shows how long the group
takes to edit messages (min, mean, median, p90, max) vs the
current 20s window - median in green if inside the window and in
red if not, so you know if the window is well tuned on real
data instead of by guess.
If the group **edits a message after the bot already ran** the
trade (window passed), a **red alert** shows on the dashboard
warning entry may be stale. And the reverse too:
if a signal arrives **incomplete** (e.g. entry with no TP/SL yet) and the
group fills the message in the next minutes, the bot **recovers the
signal** and reopens the wait window with the right content.
The **test pages** of the end menu show strategy compare
(new, new + management - main - and close at TP2) on real group
signals, for the picked span (7/30/90 days) **and** a **"📜 Since
group start"** block over all history, to see if edge holds.
The **last-month simulation** ("if you had started 1 month ago with X EUR, today you would have
Y EUR") sits on those same pages, as do **walk-forward** (does edge
hold over time?) and **risk studies** (sim risk %/balance - default sim balance is each strategy own 1,000 EUR). It is
pure simulation: it never opens real trades.
Each test page has the span picker (7/30/90 days or **all**),
which stays **saved** - reload the page and it keeps the pick. When
the bot starts and ends reading group history, the feed
shows the **"📚 History replay done"** note with total messages
and forwarded signals/management/ignored.

On login, the dashboard runs a **terminal-style boot** (boot sequence):
lines appearing with keyboard sound, showing real system state
(MT5 on, AutoTrading, run mode) before it shows numbers.
**Notes also have sound**: a high beep when a trade opens,
a low alert when something fails (risk, MT5 offline, AutoTrading
off). Sound follows browser rules - it unlocks on first
click, and mutes in the browser itself (tab mute).

- **Signals** - full history of signals from Telegram, with
  filter by group and state (waiting for edits, pending, history, etc.);
  signals pending on zone/lot guard have an explicit-confirm button;
- **Positions** - live open trades;
- **History** - all closed trades, with P&L;
- **Report** - win rate, profit factor, risk/reward, consistency
  **per group**, the **equity curve** (P&L path over time),
  **max drawdown** and a **bar chart with P&L and win rate of
  each day**, for the span you pick (7/30/90 days);
- **🧪 Test pages (menu)** - A/B/C backtest (TP1..TP5 · **TP1..TP5
  + group management** · close at TP2), **walk-forward** (does edge hold
  over time?) and **risk studies** (sim risk %/balance). The
  "+ management" column replays group instructions in backtest (close 1st
  position, raise stop, exit all) to measure how much result comes from
  their management;
- **🚫 Ignored** - audit of group messages the bot chose to
  **not store** (chat: "Good morning", "+50 pips", links, warnings).
  No signal or management instruction lands here - only chat - and it never
  enters execution. It shows all the group says, even what
  the bot skips;
- **In the header** (right of the brand) you see state badges: mode
  auto/manual, active strategy, real MT5 account (login · balance) and
  run mode (real/dry-run). At the **right of the screen** there is a terminal
  with **who is on the dashboard** (`👥 admin · admin` - each
  open tab/phone counts and vanishes when closed), the
  **clock**, the **kill switch** and the **Exit** button;
- **🔧 Diagnostics** - system state at a glance: **MT5** link
  (account, balance/equity, AutoTrading), **Telegram listener** (alive or
  OFFLINE, how long since the last group message), server
  (uptime), real prices/simulation, login, database and an **uptime chart of the
  last 24h** (listener and MT5 up/down, with %
  uptime). If the listener drops, it goes red here in ~5 seconds;
- **Auto-recovery**: if the **Telegram listener dies**, the server
  **restarts it alone** in seconds. The server also cleanly shuts
  its loops when restarted, so no stale tasks stay
  managing prices twice. If Telegram asks for a **new
  login code**, a warning shows on the dashboard with a field to type
  the code Telegram sent to the app - the listener connects back
  alone after that;
- an always-visible **notes** feed (open/closed trades,
  blocks, daily summary). When a management instruction arrives **with no
  open trade** or signal to answer, a ⚠️ alert shows on the feed -
  it never stays silent.

You can also get **Telegram notes** (when the bot opens or closes
a trade, the day summary, or risk alerts), if you link a bot
(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` - make the bot with
@BotFather and send it `/start` to get your chat id).

**OFFLINE alerts on the phone**: when **MT5 drops** (or AutoTrading
turns off), the program sends you a Telegram DM (Saved Messages) - it works
from the start, no setup, because it uses your own account. And if the
**Telegram listener** drops and auto-restarts, that too stays
logged on the feed - you never miss that something was down.

### Deleted messages

If someone **deletes** the signal message in the group, the program spots it,
marks the signal as deleted and - if the trade is still open - **closes it at
current price**. You never keep orphan positions of signals that no longer exist.

---

## Connect your Telegram group (test on your PC, no VPS)

This is all local - nothing goes to the Internet. You need 4 things, once:

### Step 1 - Make API credentials (5 minutes, free)

1. Go to **https://my.telegram.org** with your account.
2. Click **API development tools**.
3. Fill the form (app name: anything, e.g. `signalbot`).
4. Save the **api_id** and **api_hash** shown.

> ⚠️ Do not share api_hash with anyone.

### Step 2 - Join the group with your account

The program uses **your account** to read the group (signal groups do not
take bots). Open the link Ruben gives you in Telegram and **join the group**
with the normal account. If you are not there yet, the program can also
join for you through the link.

### Step 3 - Set up the program

In the project folder, make a file called `.env` with:

```
SIGNALBOT_TG_API_ID=1234567
SIGNALBOT_TG_API_HASH=escreve_aqui_o_api_hash
SIGNALBOT_TG_PHONE=+3519XXXXXXX
SIGNALBOT_TG_GROUP=https://t.me/+o_link_do_grupo
```

- `SIGNALBOT_TG_PHONE` - your number with prefix (e.g. `+351...`).
- `SIGNALBOT_TG_GROUP` - the group. It can be the invite link, `@username`
  or just the **name** (e.g. `Golden Moves`) - the program finds the chat
  in your chats. Your account must be a group member.

Several groups at once? Use `SIGNALBOT_TG_GROUPS` with comma-split groups,
and on the dashboard a picker filters signals
of each group.

### Step 4 - Run (two terminal windows)

**Window 1 - dashboard/API:**

```
.venv/Scripts/python -m uvicorn app.main:app --reload
```

**Window 2 - the Telegram reader:**

```
.venv/Scripts/python -m app.telegram_listener
```

On first run, the reader asks for the **check code** Telegram
sends to your phone. Type it and confirm. Then it listens to the
group. (With 2FA, it also asks for the password.)

### What you will see

- An incomplete message that looks like a signal shows on the dashboard with
  **⏳ 20s** by default - the edit window working.
- If the group **edits** the message, the dashboard shows the new version.
- After **20s** (or the set value), the signal runs (if
  it passes risk checks) - only automatic mode exists. Signals
  blocked by zone/lot guards stay pending for explicit
  operator confirm.
- Messages that do not look like signals (normal chat) are skipped - see
  `[skip]` in the reader terminal.

## How to start testing (spend nothing)

1. Run the program on your PC (see `README.md` for tech steps).
2. Turn on the **Telegram listener**: by default it replays the full
   group history (messages marked "history", with no trades run) and
   keeps getting new ones. If you must cap startup, use
   `SIGNALBOT_TG_HISTORICAL_LIMIT`.
3. Try manual mode: approve and reject signals.
4. Turn **Auto** on with low risk (0.1-0.5%) and watch what happens.
5. Open a **test page** (menu) or the **Report** - it uses real group
   signals with real history prices and shows if the strategy
   has edge.
6. On the **Report** you see quality per group over days.
7. On a **test page**, look at the 3-column compare: if the
   "+ management" column clearly wins, it means following group instructions
   (not only targets) makes the difference - which is exactly what the
   program does automatically with management messages.

Only when you trust it (and have real signal data) does it make sense to think
about linking this to a real account.

### Demo prep state

After the VPS reboot, Caddy, server, listener and MetaTrader 5 came back
up. Demo account `5054644473` is on, hedged, with
`trade_allowed=true`. The picked main is `tp2_protect` (2 legs, exit at
TP2). On 01/09/2026, seen balance was **EUR 1,003.89**. The server is in
`mt5-real` with `MT5_DRY_RUN=0`, so new approved signals can send
orders to this demo account. Paper/shadow stays apart; this does not allow
a real account.

---

## Extra guard during a loss streak

The two shadow auto-ladder strategies (`auto_escada_3` and
`auto_escada_5`) watch the same market, but send no orders to the MT5
account. If the last 10 trades of that strategy have PF below 1, the bot
goes briefly defensive: it tightens trailing, turns on time stop
and caps risk of each new entry at 1.25%. When the window recovers, it goes
back to normal risk. Each strategy virtual account is apart from the other and
from the real account.

Guard state is also stored. If the VPS reboots after TP1 or
during a trailing, the bot recovers peak, SLs and guard time
instead of starting the trade from zero. This recovery is automatic and changes neither the
main strategy nor the kill switch.

## FAQ

**Does this make guaranteed money?**
No. Nobody has that. The program only runs what it gets - signal quality
decides. That is why you test first in simulation.

**Can I lose money?**
In this version, no - it is all simulated. In a real version, yes, you can lose what
you risk (and the slider is exactly there to control that).

**Does the program "read the market with AI"?**
No. That is marketing. The program reads signals and handles risk. Predicting
prices with "AI" is not reliable - we did not build that, on purpose.

**And funded accounts (FTMO/Stmo)?**
That is a separate risky topic: they have hard rules (daily loss, drawdown,
no hedging, many ban automation). Before thinking of that, check
signals in simulation for weeks. Do not buy funded accounts on
"50% in a week" promises - that is the pattern of sellers.
