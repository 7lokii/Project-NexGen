# AGENTS.md - Project NexGen (Freebuff)

Guide for agents (Codebuff/Freebuff) working in this project. Read this
before touching code.

## Subagents

Toda a chamada a `subagent` leva `background: true`. Sem excepcao em trabalho
que demore mais do que alguns segundos.

- `background: true` e o equivalente de agente ao keybind `session.background`.
  O keybind e `ctrl+b` e e humano: um agente nao o pode carregar. O parametro
  pode. Nunca pedir `ctrl+b`.
- Varios subagentes: lanca todos na mesma resposta, todos com `background: true`.
  Varios em foreground bloqueiam em serie.
- Depois de os lancar, continua a fazer trabalho e so recolhe o resultado quando
  a notificacao chegar. Nao fiques em polls nem a dormir a vigiar.
- Unica excecao: leitura curta de um ficheiro, ou verificacao de tres linhas.
- Um subagent em foreground e invisivel: o Dispatch continua `in_progress`, o
  `liveness` continua `live`, nao ha `worker_done` nem erro. Um worker bloqueado
  e um worker saudavel mostram o mesmo sinal. Nao confies em nenhum dos dois.

## Workspace context

These rules are layered, outer to inner: `E:\Workspace\AGENTS.md`, then the
reference material in `E:\Workspace\.agents\rules\`, then this file. On
conflict, the more specific file wins **inside this project**, but the outer
rules still govern everything outside it.

Two absolute references, and you need both:

- `@E:\Workspace\.agents\rules\workspace-map.md` - where everything lives, which
  project a request belongs to, one issue per repo, the Orca commands, and the
  worktree traps. Read it before opening paths, backups, venvs, MCPs or git.
- `@E:\Workspace\AI-Resources\AI-Vault\index.md` - the compiled knowledge about
  the agent setup. Start at the index, never at a search, for any question about
  the harnesses, enforcement, past decisions, or the limits on rule files.

The `@` form resolves the path without inlining the file, so it costs nothing
until you actually open it.

**This file is not enough on its own.** An Orca worker runs in a standalone clone
under `C:\Users\imlok\orca\workspaces\`, a tree that never passes
`E:\Workspace\Projects\`. Nothing from the workspace reaches it: not this file,
not the root `AGENTS.md`, not the rules. In that context you have only what the
repo's own `AGENTS.md` says, so read it there.

## What the project is

**Project NexGen** - semi-automatic copy-trading system in Python (FastAPI +
vanilla JS). It reads signals from the **Golden Moves** Telegram group, waits out the edit window (20s), runs the **N-leg strategy (TP1..TP5 + breakeven)**
on **MetaTrader 5** (a **demo** account, no real money), and shows everything on a
web dashboard at `http://127.0.0.1:8000`.

Current state (2026-10-05): **this desktop runs PAPER ONLY**
(`EXECUTION_MODE=sim`, `MT5_DRY_RUN=1`), **auto** mode, Telegram listener
listening to Golden Moves. The bot reads the group, parses signals, runs the
paper race and fills the dashboard — **it sends no orders and MetaTrader 5 is
never opened on this machine.**

**Owner decision (05/10): MetaTrader 5 goes to the NoxSec homelab; this
desktop stays clean.** `mt5.initialize()` *starts* the terminal when it is not
running, so "don't open MetaQuotes here" is enforced by four independent
guards, each covered by a test
(`test_scripts_de_arranque_nao_abrem_o_metatrader`,
`test_env_local_nao_executa_contra_o_broker`,
`test_check_terminal_nao_arranca_sem_optin`):
- `EXECUTION_MODE=sim` in `.env` — the server never opens an MT5 link;
- `boot.ps1` only starts the terminal under `MT5_AUTOSTART=1` (not set here);
- `healthcheck.py`'s terminal watchdog only *restarts* the terminal under
  `MT5_AUTOSTART=1`; without it a missing terminal is logged once and left
  alone, which is the expected state on this desktop;
- `start.ps1` has no `mt5.initialize()` at all. `MT5_CHECK=1` validates a
  terminal that is *already* running and never starts one.

**ONE lane, ONE strategy.** On 05/10 the two lanes were consolidated by owner
decision: there is a single execution lane (`main`), it runs `tp2_protect`
(2 legs, exits at TP2, honours group `protect` + `close_all`), and it uses
account **5055369549** (10,000 EUR, the account that used to be lane `tp2`) on
the **portable** terminal `C:\MT5Portable2\terminal64.exe` (`MT5_TERMINAL_PATH`).
The old main account **5054644473** is **out of execution** — its history stays in
the DB (`account_label` says which account each row came from) but nothing sends
orders there. Everything that existed only for the second lane was removed:
`app/lane2_worker.py`, `app/execution/remote2.py`, the `MT5_2_*`/`LANE2_*` env
vars, `/api/lanes/ab`, `/api/lanes/ab/legs`, the `/segundaria` page and the
`lane2_*` config keys. `healthcheck.py` now watches the **terminal process**
instead of the worker.

**Open work: the homelab.** Real execution against 5055369549 needs MetaTrader 5
running somewhere, and the homelab (NoxSec) is Linux/Proxmox, where MT5 has no
native build. That is the owner's stated next step and **nothing is implemented
yet**. Until it is, the honest state is: **paper on the desktop, no broker
execution anywhere.**

`MAIN_STRATEGY_ID` is `tp2_protect` and the registry has **no other entry**: the
eight other strategies were removed from the registry on 05/10 (the six from
01/09 were already gone). See `RETIRED_STRATEGY_IDS` in `app/strategies.py` and
docs/CHANGELOG.md — `get_strategy` raises `ValueError` for a retired id, so
studies never silently run with the wrong strategy.

**AutoTrading is per-session**: re-enable "Algo Trading" after every reboot,
otherwise orders are rejected with retcode 10027. Diagnostics exposes the state
(`terminal.trade_allowed`) and the market_loop emits a `risk` alert when the
terminal is up but AutoTrading is OFF (10 min cooldown;
`terminal_auto_trading_off` event). **Symbol and null-retcode watchdogs**: the
market_loop checks every 30s that XAUUSD has a quote — with no tick it calls
`ensure_symbol` (`symbol_select`) to re-add it to Market Watch and emits a `risk`
alert (10 min cooldown + recovery notification); and a **null `retcode`**
(broker answered with NO retcode, mapped to `?`) fires a distinct `risk` alert
with the terminal state — in `_try_execute` for opens and in the tick scan
(`_check_null_retcode_phantoms`, every 30s, last 30 min, once per trade) for
phantom closes off the open path.

**shadow-paper is OFF** (`shadow_paper_enabled=0`, empty strategy list): the
strategies that fed it left the registry with the consolidation. The kill switch
was turned off on 20/08 by explicit user decision; the daily loss limit and
auto-kill stay on. Base risk is **1.0%** and the high-watermark is recalibrated
to the real account balance at boot. The user goal is to run this for
days/weeks, tune message recognition, and **later** switch to a live account.

**Two accounts, one `lane_id` — keep them apart.** The consolidation merged the
old `tp2` rows into `main`, so `lane_id` no longer tells you which account a row
came from. `account_label` / `source_account` are the separator:
`SimEngine.open_trade` stamps the linked login on every trade and position
(`DB.stamp_source_account`), `lane_status` reports the live account's P&L as
`total_pnl` with the old account's as `pnl_outras_contas`, and
`_realized_today()` filters the daily kill by the same login. **Never sum P&L by
`lane_id` alone** — that is what mixed +98,43 EUR of a dead account into a
10.000 EUR card. Reads that must not assume a single account should go through
`db.pnl_by_source_account()` (it returns `None` when the split is impossible).

**Wrong-account guard.** `MT5Provider.initialize()` refuses when
`account_info().login != MT5_LOGIN` (sets `execution_block_reason` and drops the
link). Do not remove it: with `MT5_TERMINAL_PATH` empty the MT5 API links to the
*default* terminal, which may hold another session, and the hedge check does not
catch it (both demo accounts are hedged). Diagnostics shows
`terminal.login_ligado` / `login_bate`.

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
2. **Run `pytest` before finishing** - the full suite (currently 600 tests, update on growth) is the safety net (parser, classifier, backtest, API, auth, observability, and read-only studies).
3. **Do not create test trades without cleaning up** the artifacts after (trades,
   positions, test signals in the DB).
4. **Do not store passwords in code files** - they come from `.env`/env vars.
5. **Never touch the live account** (demo uses `MT5_LOGIN=5055369549` /
   `MetaQuotes-Demo`, terminal `C:\MT5Portable2`). Account 5054644473 is
   historical only: never reintroduce it as an execution account.
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
| `.agents/rules/arquitetura-e-lanes.md` | `app/state.py`, `app/execution/**`, orders, positions, event loop |
| `.agents/rules/reconhecimento-de-mensagens.md` | refine recognition, `failed` or ignored messages, `parser.py`, `manage.py` |
| `.agents/rules/fluxo-e-gestao-do-sinal.md` | signal pipeline, edit window, guards, group management, BE+, kill switch, journal |
| `.agents/rules/backtest-e-analise.md` | `analysis.py`, `backtest.py`, studies, MFE, feed offset, defensive risk |
| `.agents/rules/estrategias-registro.md` | `app/strategies.py`, menu, `manage_kinds`, risk, choosing which strategy to promote |
| `.agents/rules/estrategias-paper-e-fases.md` | `app/static/**`, test pages, PaperRace, shadow-paper, Phase 3 panel |
| `.agents/rules/dashboard-e-alertas.md` | `app/auth.py`, sessions, Caddy, firewall, Telegram, observability |
