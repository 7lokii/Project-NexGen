# Project NexGen - arranque completo (PowerShell / Windows)
#
#   .\start.ps1                              # arranca tudo (pede a senha MT5)
#   $env:MT5_PASSWORD="..." ; .\start.ps1    # senha ja definida
#
# Idempotente: deteta se o servidor (porta 8000) e/ou o listener ja estao
# no ar e NAO lanca duplicados - correr o script com o sistema ja ligado
# so reporta o estado e sai. Para reiniciar de facto, mata os processos
# primeiro (taskkill //IM python.exe //F) e volta a correr.
#
# A senha NUNCA e gravada em ficheiro - so vive na variavel de ambiente
# do processo. O terminal MT5 tem de estar aberto e com o AutoTrading
# ligado (botao "Algo Trading" verde) para ordens reais.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = ".venv\Scripts\python.exe"
$Port = if ($env:PORT -and $env:PORT -ne "0") { $env:PORT } else { "8000" }

# ---- credenciais Telegram (do .env, se existir) -------------------------
if (Test-Path .env) {
  Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Z0-9_]+)=(.*)\s*$') {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
  }
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
