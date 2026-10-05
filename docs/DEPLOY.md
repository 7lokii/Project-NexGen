# Project NexGen - Deploy on a Windows VPS

Complete guide to run Project NexGen 24/7 on a Windows VPS and expose the
dashboard on the web with **HTTPS + login**, without giving remote (RDP) access
to anyone else.

> ⚠️ The bot and MetaTrader 5 **must be on the same machine** (the Python
> `MetaTrader5` package connects to the terminal through the local port). So the VPS is
> **Windows with RDP** - a Linux VPS will not work.

---

## 0. Rent the VPS (Vultr)

**Vultr** is the choice: 99.99% reliability, hourly billing (cancel any time),
**Windows license included** in the price, easy snapshots and
datacenters in Europe. It is the most stable option to run the fast edit flow
(8s by default) 24/7 - and later, with real money.

### Steps on Vultr

1. Create an account at **vultr.com** (email + card/PayPal; load ~$20 of
   balance, you only spend what you use).
2. **Deploy New Server**:
   - **Type**: `Cloud Compute` -> *Regular Performance* (the *High
     Performance* plan is optional - the project does not need it);
   - **Location**: **Frankfurt (`eu-central`) or Amsterdam (`ams`)** -
     lower latency to Telegram and MetaQuotes;
   - **Image**: **Windows** - Server 2019/2022 *with Desktop Experience*
     (or Windows 10/11). **The license is included in the price** - Server
     Core does NOT work (MT5 needs a graphical environment);
   - **Size**: **2 vCPU · 4 GB RAM · 80 GB NVMe** (~$16/month) - the minimum
     comfortable for Windows + MT5 + Python + Caddy. The 1 vCPU/1 GB plan
     is not enough;
   - **Auto Backups**: enable (~$1-2/month extra) or take a manual **Snapshot**
     before each big change.
3. **Deploy** -> the server page shows the **IP**, the **Administrator** user
   and the **password** (write it down - if you lose it, use
   "Reset Password" in the console).
4. (Optional, recommended) Create a **Snapshot** of the clean VPS - if something
   goes wrong, restore in 2 minutes.

**Minimum spec:** 2 vCPU · **4 GB RAM** · 80 GB NVMe · Windows
Server 2019/2022 (Desktop Experience) · RDP enabled.

---

## 0.1 Alternative: test on Azure (free trial period)

If you have **Azure on trial** (free account), you can test
for free - with two important limits:

| Path | What it is | Works for this? |
|---|---|---|
| **$200 credit (30 days)** | Any Azure service, including large Windows VMs | ✅ Yes - create a `Standard_B2s` (2 vCPU/4 GB, ~$50/month) and the credit covers the 30 days |
| **Free VM 12 months** | `B1s` Windows or Linux, **750 h/month** | ⚠️ **1 GB RAM is too little** for Windows + MT5 + Python 24/7 - only good for a quick 30 min test |

### Create the Windows VM on Azure (with the credit)

1. Portal -> **Virtual machines** -> **Create**.
2. **Image**: `Windows Server 2022 Datacenter: Azure Edition - x64 Gen2`
   (has graphical environment - MT5 needs it).
3. **Size**: `Standard_B2s` (2 vCPU · 4 GB RAM).
4. **Region**: `West Europe` (Amsterdam).
5. Create an admin user (username + password) - you log in
   by RDP with it. Enable inbound port 3389.
6. **Networking**: create a *Network Security Group* - inbound rule
   **3389 only for your IP** and **443 open** (for Caddy). Never 8000.
7. **Review + Create** -> wait for deploy.

Then the guide is the same: RDP -> install Python/MT5/Caddy -> copy the project ->
`.\start.ps1` (sections 2-7 below).

> ⚠️ **The credit ends after 30 days.** When it ends, the VM is deallocated
> (compute billing stops, but the disk keeps billing a few cents/day
> until you delete it). To run 24/7 long term, **Vultr (~$16/month)** is
> much cheaper than Azure pay-as-you-go (B2s ≈ $50/month). Azure is
> great to test now for free - then migrate to Vultr.

---

## 1. First login and firewall

1. Connect to the VPS by **RDP** (the provider sends the IP and password of
   Windows).
2. **Immediately change the Windows administrator password** to a
   strong password (not the provider default).
3. Open the Windows firewall (`wf.msc`) and create inbound rules:
   - ✅ **HTTPS (443)** - open to **everyone** (it is the dashboard port);
   - ✅ **RDP (3389)** - open **only for your IP** (or use the provider
     VPN); never to the world;
   - ❌ **Port 8000** - **never** open to the public (Caddy proxies it);
   - ❌ **22/SSH** - not needed (Windows).

    > 💡 On Vultr you can harden with the platform
    > **Cloud Firewall** (Network -> Firewall): allow only 443 to the world and 3389
    > for your IP - so even if Windows Firewall fails, the network
    > blocks. Attach it to the VPS in *Server -> Firewall*.
4. Enable Windows Update and let it install everything before continuing.

---

## 2. Install Python and git

1. Download the Python 3.11/3.12 installer (64-bit) from python.org.
2. **In the installer, check ✅ "Add Python to PATH"** and install.
3. Install git (git-scm.com) - accept the defaults.
4. In PowerShell, confirm:

```powershell
python --version
git --version
```

---

## 3. Install and configure MetaTrader 5

1. Download **MetaTrader 5** (from MetaQuotes or your broker) and install.
2. Log in with the trading account:
   - Login: **5054644473** (demo) · Server: **MetaQuotes-Demo**
   - Password: the one in your `.env` (`MT5_PASSWORD`)
3. On the toolbar, enable the **"Algo Trading"** button (it must stay
   **green** - without it, orders are rejected).
4. In `account_info`, confirm `margin_mode=2` / **RETAIL_HEDGING**. The
   N-leg strategy needs independent positions; the server
   blocks netting/unknown accounts when `MT5_DRY_RUN=0`.
5. **Leave the terminal open** and connected - the bot needs it 24/7.
   (Tip: set Windows to **log on automatically** with the
   MT5 terminal at startup - then a VPS reboot is enough to bring all back.)

---

## 4. Copy the project to the VPS

Option A - git (recommended for easy updates):

```powershell
cd C:\
git clone <URL-do-teu-repositorio> Project NexGen
cd Project NexGen
```

Option B - manual copy (project zip, without the `.venv` folder or the DB).

Then create the environment and install dependencies (the `requirements.txt` already
includes **MetaTrader5** - Windows-only package, connects to the local terminal):

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

---

## 5. Create the `.env` on the VPS

Copy your local `.env` to the VPS **or** create a new one (`notepad .env`)
with the same keys. The `.env` on the VPS needs:

```ini
EXECUTION_MODE=mt5
MT5_LOGIN=5054644473
MT5_SERVER=MetaQuotes-Demo
MT5_PASSWORD=XXXXXXXX            # demo account password (never in git!)
SIGNALBOT_TG_API_ID=...
SIGNALBOT_TG_API_HASH=...
SIGNALBOT_TG_PHONE=+351...
SIGNALBOT_TG_GROUP=Golden Moves
SIGNALBOT_DASHBOARD_PASSWORD=<password-forte> # replace with the local password; never store in git
SIGNALBOT_API_TOKEN=...                  # listener token (generated)
```

> 🔒 The `.env` **never** goes into git. Always create it by hand on the VPS.

---

## 6. Install Caddy (automatic HTTPS)

Caddy issues and renews HTTPS certificates (Let's Encrypt) on its own. Never
store passwords over plain HTTP.

1. Download Caddy for Windows at caddyserver.com (single
   `caddy.exe` file).
2. Put `caddy.exe` in `C:\caddy\` and create `C:\caddy\Caddyfile`:

```
dominio.com {
    reverse_proxy 127.0.0.1:8000
}
```

   (replace `dominio.com` with your domain - point the domain DNS to
   the VPS IP before starting Caddy).

3. Start Caddy as a **Windows service** (so it starts on its own):

```powershell
cd C:\caddy
.\caddy.exe start
# or, to run as an always-on service:
sc.exe create Caddy binPath= "C:\caddy\caddy.exe run --config C:\caddy\Caddyfile" start= auto
```

4. Confirm at https://dominio.com - the **🔐 login** of
   Project NexGen should appear with a valid HTTPS lock.

---

## 6.1 No domain - IP access (self-signed HTTPS)

If you have no domain yet (e.g. 30-day test between friends), Caddy
still serves by **IP with self-signed HTTPS** (`tls internal`): the
traffic stays encrypted and browsers show a "certificate not
trusted" warning - accept once (Advanced -> Continue) on each device.

```
# C:\caddy\Caddyfile
https://<IP_PUBLICO_DA_VPS> {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```

1. Open ports 80 and 443 in the Windows firewall (never 8000 or 3389):
   ```
   netsh advfirewall firewall add rule name="NexGen HTTP 80" dir=in action=allow protocol=TCP localport=80
   netsh advfirewall firewall add rule name="NexGen HTTPS 443" dir=in action=allow protocol=TCP localport=443
   ```
2. Start Caddy: `cd C:\caddy && .\caddy.exe start` (after a VPS reboot,
   repeat this command - or create a service/Scheduled Task).
3. Open `https://<IP>` - HTTP redirects to HTTPS (308).
4. **Platform firewall (Kamatera)**: if you have an active *Cloud Firewall*
   in the console (Cloud -> Firewalls), add TCP rules for 80/443
   with source restricted to your IPs - Windows Firewall alone is not enough.

> When you have a domain: swap the address in the Caddyfile and remove `tls
> internal` - Caddy then issues automatic HTTPS (Let's Encrypt).

## 6.2 DuckDNS - free domain (trusted HTTPS, green lock)

Without a bought domain, **duckdns.org** gives a free subdomain
(e.g. `n3xg3n.duckdns.org`) that points to the VPS IP. With a domain, the
Caddy issues automatic HTTPS from Let's Encrypt (no browser warning).

1. Create an account at duckdns.org (Google/GitHub/Twitter/Reddit login) and add
   the subdomain with the public IP of the VPS.
2. `C:\caddy\Caddyfile` - swap the address and remove `tls internal`:
   ```
   n3xg3n.duckdns.org {
       reverse_proxy 127.0.0.1:8000
   }
   ```
3. Restart Caddy: `cd C:\caddy && .\caddy.exe stop` + `.\caddy.exe start`
   (first time takes ~30s to issue the certificate).
4. **Automatic IP update** (safety net - the IP is fixed): script
   `C:\caddy\duckdns-update.ps1` calling
   `https://www.duckdns.org/update?domains=<NOME>&token=<TOKEN>&ip=`
   (without `ip` DuckDNS detects it on its own), scheduled by Scheduled Task.

## 7. Start the bot (one command)

`start.ps1` already does all: loads `.env`, checks MT5 and starts
**server + listener**. It is **idempotent** - it detects if they are already up and does not
launch duplicates:

```powershell
cd C:\Project NexGen
.\start.ps1
```

- If `MT5_PASSWORD` is already in `.env`, it asks nothing.
- You will see in the terminal: Terminal connected · account/balance/equity · AutoTrading ·
  Server at http://127.0.0.1:8000 · listener listening on the group.
- To really restart: `taskkill //IM python.exe //F` and run again.

**After a VPS reboot** (Scheduled Task `NexGen Boot`, AtLogOn, with
highest privileges): `boot.ps1` starts Caddy, **MetaTrader 5**
(`terminal64.exe`) and then `start.ps1` - all idempotent. The server
reconnects to MT5 automatically (retry at startup + reconnect every 10s)
and logs in with `MT5_LOGIN/SERVER/PASSWORD` from `.env`; **AutoTrading**
must be on in the terminal (it persists). For all this to come up
alone, set **Windows auto-login** (next section).

---

## 7.x Windows auto-login (reboot without intervention)

Without auto-login, the VPS sits at the login screen after a reboot and the
`NexGen Boot` task (AtLogOn) never fires. To log in alone:

```powershell
# once, as Administrator (replace <password> with the real password)
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name AutoAdminLogon      -Value '1'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultUserName     -Value 'Administrator'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultPassword     -Value '<password>'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultDomainName   -Value ''
```

> ⚠️ The password stays readable in the registry (visible to anyone admin on the
> machine). It is the standard tradeoff for unattended operation on a VPS.
> To revert: set `AutoAdminLogon=0` and delete `DefaultPassword`.

After auto-login + `NexGen Boot`, the full flow is: Windows logs in ->
`boot.ps1` -> Caddy + MT5 + server/listener -> the server connects to MT5
with the `.env` credentials (AutoTrading must be ON in the terminal).

---

## 8. Final check

1. Open https://dominio.com on the phone (mobile data, no VPN) -> it must
   ask for login -> log in with the set password -> you see the dashboard with the
   real MT5 balance and the **⚡ real execution (MT5 demo)** badge. In Diagnostics,
   confirm `hedged=true`, AutoTrading on and empty `blocked_reason`.
2. Confirm the listener is catching the group: `tail listener.log`
   (or `Get-Content listener.log -Tail 20`).
3. In the **👻 Shadow-paper** panel, confirm `active` and the strategies
   `auto_escada_3` and `auto_escada_5` to collect forward evidence without
   new orders. The snapshot must show
   `shadow_paper.execution_isolated=true`; this does not change `EXECUTION_MODE`,
   `MT5_DRY_RUN` or the demo account.
4. Send a test message in the group and watch the full flow on the dashboard.
5. Confirm `GET /api/window`: the UTC window ends at `2026-08-29T00:00:00+00:00`, shows real trades/P&L/pace and the promotion gate of auto-ladder 3.
6. If the kill switch is paused for this window, confirm in `/api/status` that `killswitch_rearm_at` is persisted. The tick re-arms the pause on time, sends feed/Telegram and clears the key; no need to create a separate scheduled task.

---

## 9. Maintenance

| Action | Command (PowerShell) |
|---|---|
| View server | `Get-Content server.log -Tail 30` |
| View listener | `Get-Content listener.log -Tail 30` |
| Restart all | `.\start.ps1` |
| Quick status | `curl http://127.0.0.1:8000/api/status -H "X-API-Token: <token>"` |
| DB backup | `.\venv\Scripts\python backup_db.py` - consistent copy to `backups/` + send to Telegram (daily `NexGen DB Backup` task 06:30) |
| Restore-check | `.\venv\Scripts\python backup_db.py --restore-check` - validates the newest backup in a temp DB |
| DB audit | `.\venv\Scripts\python maintenance.py --db-audit` - counts/monthly rate/policy, read-only |
| Disk space | `.\venv\Scripts\python maintenance.py --disk-check` - manual check; the server repeats daily |

### 9.2 CI and operational metrics (Items 10 and 17)

The `.github/workflows/ci.yml` file runs `python -m pytest -q` on each
push and pull request, and re-validates `tests/test_docs.py` in a separate job.
The workflow uses Python 3.11, `requirements.txt` and `sim`/`MARKET_PROVIDER=none` modes,
so it never needs MetaTrader nor sends orders. On GitHub Actions you
can manually launch `failure_probe=true`: the job creates a failing test,
confirms the non-zero exit code and cleans the file; the normal run stays
green only when the suite passes.

Operational health shows in the Report and in `GET /api/operations?days=90`.
No need to set new credentials: data is written to the local DB
in the exceptions, MT5 quotes, reconnects and modifies tables. Let the
server collect at least two weeks before reading trends.

**shadow-paper** is set from the dashboard or by `POST /api/config` with
`shadow_paper_enabled` and `shadow_paper_strategies`. Trades stay in the
`paper_*` tables, with `origin="shadow"`, and the account/balance/daily kill of that origin are
separate from the normal run; they never pass through `MT5Engine`. The
`historical=True` history and `llm_parsed` signals are ignored for this sample. The
`pending_management` queue keeps separate markers for real execution, normal paper
and shadow, so one fix/exit does not vanish between streams. If the
mode is off, existing trades stay auditable and only new
opens are blocked.

**Simple watchdog** (PowerShell, runs each minute via Task Scheduler):
restarts the server if `server.pid` stops responding, and reopens MT5 if the
`terminal64.exe` process is missing. The server also runs the daily free volume check
(`SIGNALBOT_DISK_FREE_GB`, default 10 GiB) and sends an alert on
feed/Telegram.

### 9.1 Logs, audit and restore

`start.ps1` uses `app.server_runner` and no longer redirects stdout to an
open file: server/listener write to `server.log`,
`server.err.log`, `listener.log` and `listener.err.log` with size rotation.
By default each file is capped at 10 MiB with 5 copies; set with:

```ini
SIGNALBOT_LOG_MAX_BYTES=10485760
SIGNALBOT_LOG_BACKUP_COUNT=5
```

The read-only audit of tables (rows, months seen, monthly rate and
retention policy) is:

```powershell
.venv\Scripts\python maintenance.py --db-audit
```

The monthly backup restores the newest file to a temp DB,
checks `PRAGMA integrity_check` and counts of the main tables. To run
manually and test a failure, duplicate a backup to a temp folder,
corrupt that copy and call the `restore_check` function in tests - never
corrupt the only VPS backup:

```powershell
.venv\Scripts\python backup_db.py --restore-check
```

The command returns a non-zero code and sends a Telegram alert if the
restore fails. The recommended Scheduled Task is monthly, e.g. day 1
at 07:00, plus the daily **NexGen DB Backup** task at 06:30.

**Server lifecycle:** market and watchdog loops belong to
`app/background.py::BackgroundRuntime`. `lifespan` cancels and waits for
tasks before closing the MT5 provider; when updating Python, run
`start.ps1` after confirming the old uvicorn process ended. This
stops a restart from leaving two loops managing the same account.

**Study Item 2 (read-only):** to repeat the sweep without opening trades or
changing the DB, use `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m
app.study_consolidation --item2-sweep`. The tool recomputes the 27
combinations of `auto_escada_5` in the three windows and only prints the report.

**Study Item 3 (read-only):** `PYTHONIOENCODING=utf-8 .venv/Scripts/python
-m app.study_consolidation --item3-sweep` compares `confirm_filter=True/False`
of `auto_escada_3` in the current/all/old windows, without writing to the DB or
opening trades. The accepted result (current PF 1.27->1.26; old DD
312.9%->278.3%) froze `True` in the registry, but did not promote the auto: the
production server keeps using the set strategy and promotion
to MT5 demo needs the Phase 2/3 gates.

Promotion of an auto to demo must happen only after the Phase 2 panel gates. Phase 3 validation has a fixed window of the first 10 trades,
with paper-to-MT5 comparison (at least 9/10 same exits) and automatic rollback to
`nova_gestao` on failure. The integration test uses only a fake broker
and must not be replaced by a manual order on the demo account.

**Updates**: `git pull` + `.\start.ps1` - `.env` and DB are not
touched by git.

---

## Security checklist (before real production use)

- [ ] Strong Windows password + RDP only for your IP
- [ ] Only ports **443** (web) and **3389** (RDP, restricted) open
- [ ] Caddy with mandatory HTTPS (HTTP -> redirects to HTTPS)
- [ ] `SIGNALBOT_DASHBOARD_PASSWORD` set (login on)
- [ ] `SIGNALBOT_API_TOKEN` set (listener logs in without browser)
- [ ] Kill switch and risk limits active on the dashboard
- [ ] Daily DB backup off the VPS
