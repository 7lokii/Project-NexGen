#!/usr/bin/env bash
# Project NexGen — arranque completo (Git Bash / WSL / Linux)
#
#   ./start.sh              # arranca tudo (pede a senha MT5)
#   MT5_PASSWORD=... ./start.sh   # senha já definida no ambiente
#
# A senha NUNCA é gravada em ficheiro — só vive na variável de ambiente
# do processo. O terminal MT5 tem de estar aberto e com o AutoTrading
# ligado (botão "Algo Trading" verde) para ordens reais.
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/Scripts/python"
PORT="${PORT:-8000}"

# ---- credenciais Telegram (do .env, se existir) -------------------------
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# ---- senha MT5 ----------------------------------------------------------
if [ -z "${MT5_PASSWORD:-}" ]; then
  read -r -s -p "Senha do MT5 (não fica gravada): " MT5_PASSWORD
  echo
fi
export MT5_PASSWORD

# ---- verificação do terminal MT5 ----------------------------------------
if ! "$PY" -c "import MetaTrader5 as mt5" 2>/dev/null; then
  echo "⚠️  Pacote MetaTrader5 não instalado — corre: .venv/Scripts/pip install MetaTrader5"
fi

echo "🔎 A verificar ligação ao terminal MT5..."
if ! MT5_LOGIN="${MT5_LOGIN:-}" MT5_SERVER="${MT5_SERVER:-MetaQuotes-Demo}" \
     MT5_PASSWORD="$MT5_PASSWORD" "$PY" -c "
import os, MetaTrader5 as mt5
if not mt5.initialize(login=int(os.getenv('MT5_LOGIN') or 0) or None,
                      password=os.getenv('MT5_PASSWORD'), server=os.getenv('MT5_SERVER') or None):
    print('❌ initialize falhou:', mt5.last_error()); raise SystemExit(1)
ti = mt5.terminal_info(); ai = mt5.account_info()
print(f'✅ Terminal {ti.name} (build {ti.build})')
print(f'   Conta {ai.login} · {ai.currency} · saldo {ai.balance} · equity {ai.equity}')
print(f'   AutoTrading: {\"LIGADO\" if ti.trade_allowed else \"DESLIGADO ⚠️ (liga o botão Algo Trading)\"}')
mt5.shutdown()
" ; then
  echo "⚠️  Não foi possível ligar ao MT5 — o servidor continua, mas as ordens vão falhar."
fi

# ---- matar processos antigos --------------------------------------------
[ -f server.pid ]  && kill "$(cat server.pid)"  2>/dev/null || true
[ -f listener.pid ] && kill "$(cat listener.pid)" 2>/dev/null || true
sleep 1

# ---- servidor (API + dashboard) -----------------------------------------
EXECUTION_MODE="${EXECUTION_MODE:-mt5}" \
MT5_DRY_RUN="${MT5_DRY_RUN:-0}" \
MT5_LOGIN="${MT5_LOGIN:-}" MT5_SERVER="${MT5_SERVER:-MetaQuotes-Demo}" \
MT5_PASSWORD="$MT5_PASSWORD" \
nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > server.log 2>&1 &
echo $! > server.pid
echo "🚀 Servidor:  http://127.0.0.1:$PORT   (log: server.log)"

# ---- listener do Telegram -----------------------------------------------
nohup "$PY" -u -m app.telegram_listener > listener.log 2>&1 &
echo $! > listener.pid
echo "📡 Listener do Telegram a arrancar (log: listener.log)"

sleep 6
echo
echo "Estado atual:"
curl -s -m 5 "http://127.0.0.1:$PORT/api/status" | "$PY" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    e = d.get('execution', {})
    a = d.get('account', {})
    print(f'  execução: {e.get(\"mode\")} · AutoTrading: {e.get(\"trade_allowed\")} · saldo: {a.get(\"currency\", \"USD\")} {a.get(\"balance\")}')
except Exception:
    print('  (servidor ainda a arrancar — espera um momento)')
"
echo
echo "Dica: MT5_DRY_RUN=1 ./start.sh  → regista ordens sem enviar (teste seguro)"
