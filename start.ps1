# Project NexGen - arranque completo (PowerShell / Windows)
#
#   .\start.ps1                              # arranca tudo (pede a senha MT5)
#   $env:MT5_PASSWORD="..." ; .\start.ps1    # senha ja definida
#
# Idempotente: deteta se o servidor (porta 8000), o listener e o Caddy ja
# estao no ar e NAO lanca duplicados - correr o script com o sistema ja
# ligado so reporta o estado e sai (e garante o Caddy, para o dominio nunca
# ficar fora so porque o servidor foi reiniciado). Para reiniciar de facto,
# mata os processos primeiro (taskkill //IM python.exe //F) e volta a correr.
#
# A senha NUNCA e gravada em ficheiro - so vive na variavel de ambiente
# do processo. O terminal MT5 tem de estar aberto e com o AutoTrading
# ligado (botao "Algo Trading" verde) para ordens reais.
param()
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = ".venv\Scripts\python.exe"
$Port = if ($env:PORT -and $env:PORT -ne "0") { $env:PORT } else { "8000" }

# ---- credenciais (do .env, se existir) ----------------------------------
# Carrega o .env para o ambiente do processo SEM sobrepor variáveis já
# definidas (mesma semântica do load_dotenv(override=False) do main.py): um
# `MT5_DRY_RUN=0` passado explicitamente tem de chegar ao servidor, senão o
# .env (MT5_DRY_RUN=1) forçava sempre dry-run e o arranque documentado
# (execução real na demo) nunca funcionava.
if (Test-Path .env) {
  Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Z0-9_]+)=(.*)\s*$') {
      $name = $matches[1]
      if ($null -eq [Environment]::GetEnvironmentVariable($name, "Process")) {
        [Environment]::SetEnvironmentVariable($name, $matches[2], "Process")
      }
    }
  }
}

# ---- 0. Caddy (reverse proxy HTTPS: n3xg3n.duckdns.org) -----------------
# O dominio publico so responde enquanto o Caddy servir as portas 80/443.
# Garante-o SEMPRE, mesmo quando o servidor ja esta no ar (era o caso em
# que um reinicio do servidor deixava o link fora de servico). Idempotente:
# se a porta 443 ja estiver a escutar nao lanca outro processo.
$caddyUp = $false
try {
  $caddyListen = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
  if ($caddyListen) { $caddyUp = $true }
} catch { }
if ($caddyUp) {
  Write-Host "[OK] Caddy ja no ar (porta 443 - n3xg3n.duckdns.org)"
} elseif (Test-Path "C:\caddy\caddy.exe") {
  try {
    Start-Process -FilePath "C:\caddy\caddy.exe" `
      -ArgumentList "run","--config","C:\caddy\Caddyfile" `
      -WorkingDirectory "C:\caddy" -WindowStyle Hidden | Out-Null
    Write-Host "[OK] Caddy arrancado (https://n3xg3n.duckdns.org)"
    Start-Sleep -Seconds 2
  } catch {
    Write-Host "[!] Falha ao arrancar o Caddy: $_"
  }
} else {
  Write-Host "[i]  Caddy nao instalado (C:\caddy\caddy.exe) - o dominio publico fica fora."
}

# ---- detetar o que ja esta no ar (idempotente) --------------------------
$serverUp = $false
try {
  $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($listening) { $serverUp = $true }
} catch { }

$listenerProcs = @()
try {
  $listenerProcs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*telegram_listener*' }
} catch { }
$listenerUp = ($listenerProcs.Count -gt 0)

if ($serverUp) {
  Write-Host "[OK] Servidor ja no ar na porta $Port - a saltar arranque."
  if ($listenerUp) {
    Write-Host "[OK] Listener do Telegram ja no ar - nada a fazer."
  } else {
    Write-Host "[i]  Listener OFFLINE - o watchdog do servidor arranca-o em ~10s."
  }
  $waUp = $false
  try {
    $waListen = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($waListen) { $waUp = $true }
  } catch { }
  if ($waUp) { Write-Host "[OK] Bridge WhatsApp no ar (porta 8765)" }
  else { Write-Host "[i]  Bridge WhatsApp OFFLINE - reinicia o servidor para o arrancar." }
  $wkPort = if ($env:LANE2_WORKER_PORT -and $env:LANE2_WORKER_PORT -ne "0") { $env:LANE2_WORKER_PORT } else { "8770" }
  $wkUp = $false
  try {
    $wkListen = Get-NetTCPConnection -LocalPort $wkPort -State Listen -ErrorAction SilentlyContinue
    if ($wkListen) { $wkUp = $true }
  } catch { }
  if ($env:LANE2_WORKER_ENABLED -eq "1") {
    if ($wkUp) { Write-Host "[OK] Worker lane 2 no ar (porta $wkPort)" }
    else { Write-Host "[i]  Worker lane 2 OFFLINE - o healthcheck reinicia-o." }
  } elseif ($wkUp) {
    Write-Host "[i]  Worker lane 2 NO AR mas LANE2_WORKER_ENABLED!=1 no .env (o servidor nao o usa)."
  }
  Write-Host "     Para reiniciar de facto: taskkill //IM python.exe //F e volta a correr."
  return
}

# ---- senha MT5 (so quando vamos arrancar o servidor) --------------------
if (-not $env:MT5_PASSWORD) {
  $secure = Read-Host -AsSecureString "Senha do MT5 (nao fica gravada)"
  $env:MT5_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

# ---- verificacao do terminal MT5 ----------------------------------------
Write-Host "[?] A verificar ligacao ao terminal MT5..."
$env:MT5_LOGIN = if ($env:MT5_LOGIN) { $env:MT5_LOGIN } else { "" }
$env:MT5_SERVER = if ($env:MT5_SERVER) { $env:MT5_SERVER } else { "MetaQuotes-Demo" }
$env:MT5_PASSWORD = $env:MT5_PASSWORD
& $Py -c @"
import os, MetaTrader5 as mt5
login = int(os.getenv('MT5_LOGIN') or 0) or None
ok = mt5.initialize(login=login, password=os.getenv('MT5_PASSWORD'),
                    server=os.getenv('MT5_SERVER') or None)
if not ok:
    print('[x] initialize falhou:', mt5.last_error()); raise SystemExit(1)
ti = mt5.terminal_info(); ai = mt5.account_info()
print('[OK] Terminal', ti.name, '(build', ti.build, ')')
print('     Conta', ai.login, ai.currency, 'saldo', ai.balance, 'equity', ai.equity)
print('     AutoTrading:', 'LIGADO' if ti.trade_allowed else 'DESLIGADO (liga o botao Algo Trading)')
mt5.shutdown()
"@
if ($LASTEXITCODE -ne 0) {
  Write-Host "[!] Nao foi possivel ligar ao MT5 - o servidor continua, mas as ordens vao falhar."
}

# ---- limpar listeners orfaos (de um servidor antigo que morreu) ---------
foreach ($p in $listenerProcs) {
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

# ---- servidor (API + dashboard) -----------------------------------------
$env:EXECUTION_MODE = if ($env:EXECUTION_MODE) { $env:EXECUTION_MODE } else { "mt5" }
$env:MT5_DRY_RUN = if ($null -ne $env:MT5_DRY_RUN) { $env:MT5_DRY_RUN } else { "0" }
$srv = Start-Process -FilePath $Py -ArgumentList "-u -m app.server_runner --host 127.0.0.1 --port $Port" `
  -PassThru -WindowStyle Hidden
$srv.Id | Out-File server.pid
Write-Host "[OK] Project NexGen:  http://127.0.0.1:$Port   (log: server.log)"

# ---- listener do Telegram -------------------------------------------------
# O watchdog do servidor tambem gere o listener (e nao cria duplicados por
# causa de _listener_running_anywhere), mas arrancamo-lo ja para a dashboard
# ficar verde sem esperar o ciclo de 10s do watchdog.
$lst = Start-Process -FilePath $Py -ArgumentList "-u -m app.telegram_listener" `
  -PassThru -WindowStyle Hidden
$lst.Id | Out-File listener.pid
Write-Host "[OK] Listener do Telegram a arrancar (log: listener.log)"

# ---- bridge WhatsApp (Baileys) ---------------------------------------------
# Publica resultados de trades reais no grupo configurado (WHATSAPP_GROUP_JID
# no .env). Node tem de estar instalado; pareia 1x por QR no Diagnóstico.
$waUp = $false
try {
  $waListen = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
  if ($waListen) { $waUp = $true }
} catch { }
if ($waUp) {
  Write-Host "[OK] Bridge WhatsApp ja no ar (porta 8765)"
} elseif (Test-Path "whatsapp\node_modules") {
  try {
    Start-Process -FilePath "node" -ArgumentList "whatsapp/bridge.js" `
      -WorkingDirectory "$PSScriptRoot\whatsapp" -WindowStyle Hidden `
      -RedirectStandardOutput "$PSScriptRoot\whatsapp\bridge.log" `
      -RedirectStandardError "$PSScriptRoot\whatsapp\bridge.err.log" | Out-Null
    Write-Host "[OK] Bridge WhatsApp a arrancar (porta 8765 - pareia com QR no Diagnostico)"
  } catch {
    Write-Host "[i]  Node nao encontrado - instala Node.js LTS e corre 'npm install' em whatsapp/"
  }
} else {
  Write-Host "[i]  Bridge WhatsApp nao instalado - corre: cd whatsapp && npm install"
}

# ---- 2a lane MT5: worker isolado (execucao real da 2.a conta, opcional) --
# A API MetaTrader5 so permite UMA ligacao por processo: a 2.a conta precisa
# do worker (processo separado, app/lane2_worker.py) + 2.o terminal portable
# (MT5_2_TERMINAL_PATH, arrancado pelo boot.ps1). Idempotente por porta.
$wkUp = $false
$wkPort = if ($env:LANE2_WORKER_PORT -and $env:LANE2_WORKER_PORT -ne "0") { $env:LANE2_WORKER_PORT } else { "8770" }
try {
  $wkListen = Get-NetTCPConnection -LocalPort $wkPort -State Listen -ErrorAction SilentlyContinue
  if ($wkListen) { $wkUp = $true }
} catch { }
if ($env:LANE2_WORKER_ENABLED -eq "1") {
  if (-not $env:MT5_2_TERMINAL_PATH) {
    Write-Host "[!] Worker lane 2 NAO arranca: MT5_2_TERMINAL_PATH vazio no .env (fail-closed - sem terminal portable o worker trocaria a conta do terminal principal)."
  } elseif ($wkUp) {
    Write-Host "[OK] Worker lane 2 ja no ar (porta $wkPort)"
  } else {
    $wk = Start-Process -FilePath $Py -ArgumentList "-u -m app.lane2_worker" `
      -PassThru -WindowStyle Hidden
    $wk.Id | Out-File lane2_worker.pid
    Write-Host "[OK] Worker lane 2 a arrancar (porta $wkPort, log: lane2_worker.log)"
  }
} elseif ($wkUp) {
  Write-Host "[i]  Worker lane 2 NO AR mas LANE2_WORKER_ENABLED!=1 no .env (o servidor nao o usa - lane 2 em dry-run)"
} else {
  Write-Host "[i]  Worker lane 2 desligado (LANE2_WORKER_ENABLED!=1) - a 2.a conta corre em dry-run"
}

Start-Sleep -Seconds 6
Write-Host ""
Write-Host "Estado atual:"
try {
  $d = Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 5
  Write-Host "  execucao: $($d.execution.mode) | AutoTrading: $($d.execution.trade_allowed) | saldo: $($d.account.currency) $($d.account.balance)"
} catch {
  Write-Host "  (servidor ainda a arrancar - espera um momento)"
}
Write-Host ""
Write-Host "Dica: define `$env:MT5_DRY_RUN='1' antes de correr -> regista ordens sem enviar (teste seguro)"
