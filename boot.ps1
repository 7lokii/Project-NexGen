# Project NexGen — arranque pós-reboot (Tarefa Agendada "NexGen Boot")
#
# Sobe o Caddy (reverse proxy HTTPS) e depois o servidor + listener.
# Ambos os passos são idempotentes: não criam duplicados se já estiverem
# no ar. O terminal MT5 tem de estar aberto e ligado para ordens reais.
param()
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# ---- 1. Caddy (reverse proxy HTTPS, em C:\caddy) -------------------------
try {
  $caddy = Get-Process caddy -ErrorAction SilentlyContinue
  if ($caddy) {
    Write-Host "[OK] Caddy ja no ar (PID $($caddy.Id))"
  } else {
    Start-Process -FilePath "C:\caddy\caddy.exe" `
      -ArgumentList "run","--config","C:\caddy\Caddyfile" `
      -WorkingDirectory "C:\caddy" -WindowStyle Hidden
    Write-Host "[OK] Caddy arrancado"
  }
} catch {
  Write-Host "[!] Falha ao arrancar o Caddy: $_"
}

# ---- 2. MetaTrader 5 (tem de estar aberto para ordens reais) -------------
# O bot autentica-se programaticamente via MT5_LOGIN/MT5_SERVER/MT5_PASSWORD
# do .env (mt5.initialize(login=...)), mas o terminal tem de estar a correr.
$mt5Exe = "C:\Program Files\MetaTrader 5\terminal64.exe"
try {
  $mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
  if ($mt5) {
    Write-Host "[OK] MT5 ja no ar (PID $($mt5.Id))"
  } elseif (Test-Path $mt5Exe) {
    Start-Process -FilePath $mt5Exe
    Write-Host "[OK] MT5 arrancado (aguardar o terminal ligar...)"
    Start-Sleep -Seconds 10
  } else {
    Write-Host "[!] MT5 nao encontrado em $mt5Exe"
  }
} catch {
  Write-Host "[!] Falha ao arrancar o MT5: $_"
}

# ---- 2b. Segundo terminal MT5 (conta demo paralela, opcional) -----------
# Lê MT5_2_TERMINAL_PATH do .env. Se estiver preenchido, arranca o terminal
# portable da 2ª conta; caso contrário salta (a lane 2 corre em dry-run com
# o market feed até a instalação portable existir).
$mt5_2_path = ""
if (Test-Path "$PSScriptRoot\.env") {
  $mt5_2_path = (Select-String -Path "$PSScriptRoot\.env" -Pattern '^MT5_2_TERMINAL_PATH=').Line
  if ($mt5_2_path) { $mt5_2_path = $mt5_2_path.Substring(20).Trim() }
}
if ($mt5_2_path -and (Test-Path $mt5_2_path)) {
  try {
    $mt5_2_proc = Get-Process -Name (Split-Path $mt5_2_path -Leaf).Replace(".exe","") -ErrorAction SilentlyContinue
    if ($mt5_2_proc) {
      Write-Host "[OK] MT5 (2a conta) ja no ar (PID $($mt5_2_proc.Id))"
    } else {
      Start-Process -FilePath $mt5_2_path
      Write-Host "[OK] MT5 (2a conta) arrancado de $mt5_2_path"
    }
  } catch {
    Write-Host "[!] Falha ao arrancar o 2o MT5: $_"
  }
} else {
  Write-Host "[ ] 2o terminal MT5 nao configurado (MT5_2_TERMINAL_PATH vazio/inexistente) — lane 2 em dry-run"
}

# ---- 3. Servidor + listener (start.ps1 é idempotente) --------------------
& "$PSScriptRoot\start.ps1"
