# 🤖 Project NexGen

Copy-trading **semi-automático**: lê sinais do grupo de Telegram **Golden
Moves**, espera a janela de edição (20s por defeito) e executa a estratégia de **N pernas
(TP1..TP5 + breakeven)** no **MetaTrader 5** — atualmente numa **conta demo**
(não é dinheiro real). Tudo visível numa dashboard web estilo Cyberpunk 2077.

> ⚠️ A conta é **demo** (`MetaQuotes-Demo`). As credenciais vêm do `.env` e
> nunca devem ser guardadas no repositório. Nada de dinheiro real está em jogo,
> mas as ordens são reais no terminal demo, com o risco configurado na dashboard.

## Funcionalidades

- 📨 **Pipeline de sinais** com janela de edição de 20s e tratamento de
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
- 👻 **Shadow-paper forward**: sinais live alimentam `auto_escada_3` e
  `auto_escada_5` em contas paper separadas, mesmo com Manual/kill switch,
  com gestão e deduplicação, sem chamar o MT5
- ⏱️ **Resolução/latência auditável**: `/api/analysis/resolution` compara M1
  nativo quando disponível, fallback explícito e atrasos 0/8/60/300 s
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
.venv/Scripts/python -m pytest -q     # 337 testes
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
  notify/telegram.py   # Notificador (Telegram)
  notify/whatsapp.py   # Notificador WhatsApp (resultados de trades, bridge Baileys)
  telegram_listener.py # Listener de Telegram (Telethon)
  static/              # Dashboard (index.html, login.html)
whatsapp/              # Sidecar Node/Baileys (bridge HTTP para o WhatsApp)
tests/                 # 389+ testes (parser, gestão, API, auth, backtest, docs, shadow-paper, whatsapp)
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
| `WHATSAPP_ENABLED` | (opcional) `1` liga o push dos resultados de trades para o WhatsApp |
| `WHATSAPP_BRIDGE_URL` | (opcional) URL do sidecar Baileys (default `http://127.0.0.1:8765`) |
| `WHATSAPP_GROUP_JID` | (opcional) JID do grupo WhatsApp (ex. `1203630...@g.us`) |
| `DASHBOARD_URL` | (opcional) link público da dashboard nas mensagens (default `https://n3xg3n.duckdns.org`) |

## Resultados de trades no WhatsApp (opcional)

Quando uma **trade real da conta principal** (MT5 demo) fecha, o bot publica no
grupo configurado o resumo (símbolo, direção, entrada, **P&L por perna**, razão
de saída, P&L total) **+ link da dashboard**. A corrida paper/shadow e o modo
sim não enviam nada.

Setup (1×, na VPS):

1. Instala **Node.js LTS** e corre `cd whatsapp && npm install`.
2. O `start.ps1` arranca o bridge (porta `8765`); abre **Diagnóstico →
   WhatsApp** e pareia com o QR (telemóvel: *WhatsApp → Dispositivos ligados →
   Ligar dispositivo*).
3. No Diagnóstico copia o **JID** do grupo (secção "Grupos disponíveis") e
   define no `.env`: `WHATSAPP_ENABLED=1` e `WHATSAPP_GROUP_JID=<jid>`.
4. Reinicia o servidor. A sessão fica guardada em `whatsapp/session/` (não
   volta a pedir QR).

> ⚠️ A sessão usa o WhatsApp Web não-oficial (Baileys) — viola os termos do
> WhatsApp e a conta que pareia o QR pode ser banida. Risco baixo para poucas
> mensagens/dia, mas real.
