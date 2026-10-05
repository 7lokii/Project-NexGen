# AGENTS.md - Project NexGen (Freebuff)

Guide for agents (Codebuff/Freebuff) working in this project. Read this
before touching code.

## What the project is

**Project NexGen** - semi-automatic copy-trading system in Python (FastAPI +
vanilla JS). It reads signals from the **Golden Moves** Telegram group, waits out the edit window (20s), runs the **N-leg strategy (TP1..TP5 + breakeven)**
on **MetaTrader 5** (a **demo** account, no real money), and shows everything on a
web dashboard at `http://127.0.0.1:8000`.

Current state (2026-09-01): server on **MT5-live on the demo account** (`EXECUTION_MODE=mt5`,
`MT5_DRY_RUN=0`), **auto** mode, main strategy persisted as
`tp2_protect` (2 legs, exit at TP2) (TP1..TP5 + group management), Telegram listener
listening to Golden Moves. The **second MT5 demo account (5055369549, 10,000 EUR balance)** is
connected as lane
`tp2` (`MT5_2_*` env) and **went live on 01/09** with
`nova_gestao`; on **02/09 the lane moved to account 5055369549 (10k) with the
same strategy as the main one - `tp2_protect`** (persisted config
`lane2_strategy`), **portable** terminal at
`C:\MT5Portable2\terminal64.exe` (`MT5_2_TERMINAL_PATH`), `LANE2_WORKER_ENABLED=1`
and `MT5_2_DRY_RUN=0` in `.env` - the **isolated worker** (`app/lane2_worker.py`,
port 8770, PID in `lane2_worker.pid`) owns the MetaTrader5 connection to
5055369549 and the server talks to it via `RemoteProvider` (see
Lanes/architecture section). Main account **5054644473** stays on its own terminal
(Program Files) - the two terminals/processes NEVER share a connection.
The **terminal 2 AutoTrading was turned ON on 01/09** (automated click
on the portable terminal "Algo Trading" toolbar button - last
button on the right; validated through `terminal_info().trade_allowed` via the
worker). It is **per-session** - re-enable after every reboot, same as
terminal 1. While OFF, lane 2 orders are
rejected with retcode 10027 + alert. **Diagnostics expose the state**
(`lane2_worker.trade_allowed` - ON/OFF in the "Lane 2" block) and the market_loop
emits an automatic `risk` alert when the worker is up but AutoTrading
is OFF (10 min cooldown; `lane2_auto_trading_off` event). **Symbol and null-retcode watchdogs (02/09)**: market_loop checks every 30s that
XAUUSD has a quote on the lane 2 worker - with no tick, it calls `ensure_symbol`
(`symbol_select`) to re-add it to Market Watch and emits a `risk` alert
(episode with 10 min cooldown + recovery notification;
`lane2_symbol_reselect`/`lane2_symbol_recovered` events); and a **null `retcode`**
(broker answered with NO retcode - mapped to `?`, e.g. 14:52 on 02/09) on a
lane 2 order fires a distinct `risk` alert with worker state
(`reachable`/`connected`/`trade_allowed`/login/last error) - in `_try_execute`
for opens and in the tick scan (`_check_null_retcode_phantoms`, every 30s,
last 30 min, once per trade) for phantom closes off the open path.
The kill switch was **turned off on 20/08 by explicit user
decision** (the scheduled 29/08 re-arm was cleared) - main `nova_gestao`
now opens MT5 demo orders in the 28/08 window and the gate reference
(hybrid race+shadow reference of `nova_gestao`, `min_reference_trades=10`
threshold) starts accumulating; the daily limit (3%) and auto-kill stay on. Base risk is
1.0% and the high-watermark was recalibrated to 1,000 EUR; effective risk thus starts
at 1.0%, without inheriting the old paper account peak. **shadow-paper** is
on by default for `auto_escada_3`, `auto_escada_5`, `auto_escada_3_hibrida`
and `nova_gestao` (the last feeds the gate hybrid reference): it receives full live
signals even with the kill switch on (manual mode was removed in Item
12) and only uses paper tables, never the MT5 engine. Management pendings carry separate markers for real, paper
race, and shadow, so one isolated fill cannot consume another. The user
goal is to run this for days/weeks, tune message recognition, and **later** switch to a live account.

> ⚠️ The account is **demo** - no real money involved. Never switch to a
> live account without the user's explicit instruction.

## Keep docs updated (mandatory)

**Every change/implementation that touches program behavior must update the
docs in the SAME turn - never finish a task with stale docs.** Docs are the map that keeps this
project alive for weeks and migrates to the VPS/live account without breaks.

- **AGENTS.md** - flows, running processes, architecture, message
  recognition, auth, endpoints, env vars, suite test count. Any behavior change -> update here.
- **docs/TECHNICAL.md** - technical detail: endpoints, DB, engine, backtest,
  conventions. New features and API changes -> update here.
- **docs/ESTRATEGIAS.md** - dedicated guide to current strategies (register,
  group management, self-management, results, where they show). Register/result changes -> update here.
- **docs/PUBLIC.md** - non-programmer view (end user).
  New dashboard-visible features -> update here.
- **docs/DEPLOY.md** - Windows VPS deploy guide (Python + MT5 + Caddy
  HTTPS + firewall). Startup/deploy changes -> update here.
- **docs/CHANGELOG.md** - chronological change log. **Every**
  change adds an entry (date + what + test count).
- **New endpoints/env vars** -> always register in AGENTS.md + TECHNICAL.md.
- **Test count**: when the suite grows/changes, fix the number in
  AGENTS.md (and any doc quoting it). `tests/test_docs.py` fails when
  AGENTS.md does not state the real suite count.
- End-of-task checklist (see golden rules): live processes + green suite
  + clean DB + **updated docs**.

## Golden rules for the agent

1. **Restart the server after touching Python** and confirm with
   `curl /api/status`.
2. **Run `pytest` before finishing** - the full suite (currently 631 tests, update on growth) is the safety net (parser, classifier, backtest, API, auth, observability, and read-only studies).
3. **Do not create test trades without cleaning up** the artifacts after (trades,
   positions, test signals in the DB).
4. **Do not store passwords in code files** - they come from `.env`/env vars.
5. **Never touch the live account** (demo uses `MT5_LOGIN=5054644473` /
   `MetaQuotes-Demo`).
6. After a task: check live processes (server + listener),
   green suite, clean DB state, and **updated docs** (section
   above).
7. **Local-only GitHub for now:** even with `origin`/workflow present, no
   API calls, push, or branch protection without the user's explicit request.
8. **Credentials pasted in chat are compromised:** revoke/rotate outside the
   repo and never reuse or store those tokens in code.
9. **HELP in sync:** any change in `app/strategies.py`, group management, shadow-paper, risk, or visible concepts requires updating
   `app/static/help.html` and `tests/test_frontend.py`; every strategy with
   `hidden=false` must have a `HELP_FLOW` entry plus a short explanation.

## Workspace and vault

This project is part of `E:\Workspace`. The workspace rules apply here and win
on conflict: `E:\Workspace\AGENTS.md`.

**Read the vault note before proposing or doing anything in this project.**
It holds what the owner wants next:
`E:\Workspace\Workspace-Vault\02 Projetos\Project NexGen.md`. Check the date at the
top before trusting it: it may be stale, and then ask instead of inventing.

History and fixes live in the repo (`docs/CHANGELOG.md` and git), not in the
vault. Update the vault note's state and next step only when they actually change.

## Map of detailed rules

The above is the essential, always-on core. The rest of this old file lives in
`.agents/rules/`, and Antigravity loads each file on its own when the task
matches the description. **Read what is needed before touching**, and never guess the behavior of a part you
have not read.

| File | When to read |
|---|---|
| `.agents/rules/operacao-e-vps.md` | start, stop, restart, logs, health check, DB backup, Caddy, firewall, MT5 AutoTrading |
| `.agents/rules/arquitetura-e-lanes.md` | `app/state.py`, `app/execution/**`, 2nd-account worker, orders, positions, event loop |
| `.agents/rules/reconhecimento-de-mensagens.md` | refine recognition, `failed` or ignored messages, `parser.py`, `manage.py` |
| `.agents/rules/fluxo-e-gestao-do-sinal.md` | signal pipeline, edit window, guards, group management, BE+, kill switch, journal |
| `.agents/rules/backtest-e-analise.md` | `analysis.py`, `backtest.py`, studies, MFE, feed offset, defensive risk |
| `.agents/rules/estrategias-registro.md` | `app/strategies.py`, menu, `manage_kinds`, risk, choosing which strategy to promote |
| `.agents/rules/estrategias-paper-e-fases.md` | `app/static/**`, test pages, PaperRace, shadow-paper, Phase 3 panel |
| `.agents/rules/dashboard-e-alertas.md` | `app/auth.py`, sessions, Caddy, firewall, Telegram, observability |
