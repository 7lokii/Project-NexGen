# 🤖 Project NexGen

Copy-trading **semi-automático**: lê sinais do grupo de Telegram **Golden
Moves**, espera a janela de edição (8s por defeito) e executa a estratégia de **N pernas
(TP1..TP5 + breakeven)** no **MetaTrader 5** — atualmente numa **conta demo**
(não é dinheiro real). Tudo visível numa dashboard web estilo Cyberpunk 2077.

> ⚠️ A conta é **demo** (`MetaQuotes-Demo`). As credenciais vêm do `.env` e
> nunca devem ser guardadas no repositório. Nada de dinheiro real está em jogo,
> mas as ordens são reais no terminal demo, com o risco configurado na dashboard.

## Funcionalidades

- 📨 **Pipeline de sinais** com janela de edição de 8s e tratamento de
  mensagens apagadas (fecha a posição ao mercado)
- 📡 **Listener de Telegram** (Telethon) que lê o Golden Moves, captura a
  relação de **resposta (reply)** entre mensagens e carrega o histórico
- 🧠 **Reconhecimento de mensagens**: parser de sinais (entrada/SL/TP1..TP5)
  + classificador de **gestão do grupo** (TP HIT → BE, SL HIT, close TP1,
  protect, close all, resumos) com fila para instruções que chegam antes da
  trade abrir
- 💹 **Execução no MetaTrader 5** (MT5Engine, dry-run opcional): 1 ordem por
  TP, SL/TP no broker, P&L em tempo real; alerta quando o broker recusa ordens
- 🖥️ **Dashboard** (tema Cyberpunk 2077): sinais, posições (cada perna com o
  seu TP), histórico, relatório (win rate, P&L por dia, drawdown), feed em
  tempo real (WebSocket) e **preview do risco** (€ em jogo por trade)
- 🎯 **Edge analysis**: backtest dos sinais reais (A/B/C: TP1..TP5 ·
  **TP1..TP5 + gestão do grupo** · fechar no TP2), walk-forward e comparação
  com o WR que o grupo
  reporta (ground truth)
- 🛡️ Motor de risco: sizing a partir do SL, limite diário, máx. posições,
  **kill switch** manual/automático, alerta de divergência grupo vs bot
- 🔐 **Login na dashboard** (password única) + token de API para o listener
- 🔔 Notificações: feed + alertas de risco (MT5 caiu, AutoTrading OFF, gestão
  sem alvo) + resumo diário
- 🩺 **Saúde operacional**: exceções, spread MT5 mensal, reconexões semanais
  e modifies de SL recusados em `/api/operations`
- ✅ **CI GitHub Actions**: suite completa e validação das regras documentais
  em cada push/pull request

## Início rápido (local)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux/macOS

# janela 1 — servidor (API + dashboard) — carrega o .env
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# janela 2 — listener do Telegram
.venv/Scripts/python -m app.telegram_listener
```

Testes:

```bash
.venv/Scripts/python -m pytest -q     # 308 testes
```

> **Windows**: para reiniciar o servidor, usa `taskkill //PID <pid> //F` (o
> `kill` do Git Bash não mata processos Windows) e confirma a porta livre
> com `netstat -ano | grep :8000` antes de arrancar de novo.

## Arranque com um comando

- Windows: `.\start.ps1` — arranca servidor + listener + verificação do MT5
  (pede a senha MT5 sem a gravar em ficheiro)
- Linux/WSL: `./start.sh`

## Autenticação da dashboard

Com `SIGNALBOT_DASHBOARD_PASSWORD` no `.env`, a dashboard pede login
(utilizador `admin`, password no `.env`). O listener autentica com
`X-API-Token` (`SIGNALBOT_API_TOKEN`). Sem a variável, tudo fica aberto
(uso local/testes).

## Documentação

- [**AGENTS.md**](AGENTS.md) — guia para agentes: processos, arquitetura,
  reconhecimento de mensagens, regras de ouro
- [**TÉCNICA**](docs/TECHNICAL.md) — arquitetura, módulos, API, BD, engines
- [**PÚBLICA**](docs/PUBLIC.md) — em linguagem simples, para não-programadores
- [**DEPLOY**](docs/DEPLOY.md) — guia de deploy numa VPS Windows (Python +
  MT5 + Caddy HTTPS + firewall)
- [**CHANGELOG**](docs/CHANGELOG.md) — histórico das alterações

## Estrutura

```
app/
  main.py              # FastAPI: endpoints, middleware de auth, WebSocket
  auth.py              # Login da dashboard + token do listener
  state.py             # BotState: orquestrador (pipeline, gestão, tick)
  config.py            # Configuração central
  models.py            # Dataclasses (Signal, Trade, Position...)
  db.py                # Persistência SQLite
  signals/parser.py    # Parser de sinais (entrada/SL/TP1..TP5)
  messages/manage.py   # Classificador das mensagens de gestão do grupo
  messages/summaries.py# Resumos diários/semanais (ground truth)
  execution/           # SimEngine + MT5Engine (mesma interface)
  backtest.py          # Backtest de sinais reais em candles históricos
  analysis.py          # Edge: A/B/C, walk-forward
  notify/telegram.py   # Notificador
  telegram_listener.py # Listener de Telegram (Telethon)
  static/              # Dashboard (index.html, login.html)
tests/                 # 308 testes (parser, gestão, API, auth, backtest, docs)
docs/                  # Documentação técnica, pública, deploy, changelog
```

## Configuração (variáveis de ambiente)

| Variável | Uso |
|---|---|
| `SIGNALBOT_DB` | Caminho do ficheiro SQLite |
| `EXECUTION_MODE` | `sim` (paper) ou `mt5` (MetaTrader) |
| `MT5_LOGIN` / `MT5_SERVER` / `MT5_PASSWORD` | Conta do terminal MT5 |
| `MT5_DRY_RUN` | `1` regista ordens sem enviar; `0` envia a sério |
| `SIGNALBOT_DASHBOARD_PASSWORD` | Password do login da dashboard |
| `SIGNALBOT_API_TOKEN` | Token do listener (`X-API-Token`) |
| `SIGNALBOT_TG_API_ID` / `SIGNALBOT_TG_API_HASH` | Credenciais API (my.telegram.org) |
| `SIGNALBOT_TG_PHONE` | Teu número (com indicativo) para o listener |
| `SIGNALBOT_TG_GROUP` | Grupo de sinais (ex.: `Golden Moves`) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (opcional) notificações via bot |
