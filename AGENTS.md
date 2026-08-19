# AGENTS.md — Project NexGen (Freebuff)

Guia para agentes (Codebuff/Freebuff) que trabalham neste projeto. Lê isto
antes de mexer no código.

## O que é o projeto

**Project NexGen** — sistema de copy-trading semi-automático em Python (FastAPI +
vanilla JS). Lê sinais do grupo de Telegram **Golden Moves**, espera a janela
de edição (8s), executa a estratégia de **N pernas (TP1..TP5 + breakeven)**
no **MetaTrader 5** (conta **demo**, não é dinheiro real) e mostra tudo numa
dashboard web em `http://127.0.0.1:8000`.

Estado atual (2026-08): servidor em **MT5 real na conta demo** (`EXECUTION_MODE=mt5`,
`MT5_DRY_RUN=0` no arranque do servidor — o `.env` tem `MT5_DRY_RUN=1` mas o
servidor é iniciado com override), modo **auto**, listener do Telegram a
escutar o Golden Moves. O objetivo do utilizador é correr isto durante
dias/semanas, afinar o reconhecimento de mensagens e **mais tarde** trocar
para uma conta real.

> ⚠️ A conta é **demo** — não há dinheiro real em jogo. Nunca mudar para uma
> conta real sem instrução explícita do utilizador.

## Processos em execução (verificar estado primeiro)

```
cat server.pid    # PID do uvicorn (porta 8000)
cat listener.pid  # PID do listener do Telegram
tail -20 server.log     # log da API (rotação automática)
tail -20 listener.log   # log do listener (rotação automática)
```

- **Servidor (uvicorn):** `MT5_DRY_RUN=0 PYTHONIOENCODING=utf-8 nohup .venv/Scripts/python -u -m app.server_runner --host 127.0.0.1 --port 8000 >/dev/null 2>&1 &`
  - `app.server_runner` abre `server.log`/`server.err.log` com rotação por tamanho
    (`SIGNALBOT_LOG_MAX_BYTES`, default 10 MiB; `SIGNALBOT_LOG_BACKUP_COUNT`,
    default 5). Não redireciones o processo para `server.log`: isso mantém um
    handle externo aberto e impede a rotação no Windows.
  - O `main.py` carrega o `.env` (`load_dotenv`, sem override) — `EXECUTION_MODE`,
    `MT5_LOGIN`, `MT5_SERVER`, `MT5_PASSWORD` e `SIGNALBOT_API_TOKEN` vêm do `.env`.
    `MT5_DRY_RUN=0` no arranque sobrepõe o `MT5_DRY_RUN=1` do `.env` (execução
    real na demo); sem isso o servidor fica em dry-run.
  - **Reiniciar depois de qualquer alteração** ao código Python — os ficheiros
    não são recarregados em produção (sem `--reload`).
  - ⚠️ **No Windows, `kill` (Git Bash) não mata o processo uvicorn** — usa
    `taskkill //PID <pid> //F` (ou `taskkill /PID` via cmd). Confirma sempre
    com `netstat -ano | grep :8000` que a porta ficou livre antes de
    arrancar de novo; o novo processo só arranca se a porta estiver livre.
    O `server.pid` pode apontar para o subshell (`$!`) — o PID real do
    uvicorn é o que está a LISTEN na porta 8000.
  - **`start.ps1` é idempotente** (PowerShell): deteta se o servidor (porta
    8000) e/ou o listener já estão no ar e NÃO lança duplicados — com o
    sistema já ligado só reporta e sai. Usa `return` (não `exit`) para poder
    ser chamado a partir do `boot.ps1`. Para reiniciar de facto:
    `taskkill //IM python.exe //F` e volta a correr.
  - **Timestamps**: todos os timestamps do servidor/listener são **UTC
    explícito** (`+00:00`) — o browser converte para a hora local do
    utilizador. Se algum timestamp ficar naive (sem fuso), a contagem da
    janela de edição na dashboard aparece inflada ("3000s" em vez de 8s).
  - **Reinício automático (reboot da VPS)**: `boot.ps1` (raiz do projeto)
    arranca o Caddy (`C:\caddy\caddy.exe run`), o **MetaTrader 5**
    (`terminal64.exe`, idempotente) e depois o `start.ps1` (servidor +
    listener), todos idempotentes. A Tarefa Agendada **`NexGen Boot`**
    (AtLogOn, RunLevel Highest, sessão interativa) corre-o ao iniciar
    sessão. O servidor **religa-se ao MT5 automaticamente** (retry no
    arranque + reconexão com throttle de 10s no `market_loop`), e o bot
    autentica-se programaticamente via `MT5_LOGIN/SERVER/PASSWORD` do
    `.env` — o terminal só precisa de estar a correr com o AutoTrading ON.
    Para o `boot.ps1` disparar sozinho é preciso o **auto-login do Windows**
    (registo `Winlogon`); sem ele a VPS fica no ecrã de login e nada arranca.
  - **Backup diário da BD**: `backup_db.py` copia `signalbot.db` (API backup
    do sqlite3, consistente) para `backups/signalbot-<timestamp>.db`, roda a
    rotação (últimas 10) e envia para o Telegram via `sendDocument` quando
    `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` estão preenchidos (hoje estão
    VAZIOS — backup só local até os preencher). Tarefa **`NexGen DB Backup`**
    diária às 06:30. O teste mensal de restauração é
    `.venv/Scripts/python backup_db.py --restore-check`: restaura para uma BD
    temporária, corre `PRAGMA integrity_check` e valida as tabelas/contagens;
    uma falha é registada e enviada como alerta Telegram quando configurado.
  - **Operação**: o watchdog faz um check diário de disco (`SIGNALBOT_DISK_PATH`,
    `SIGNALBOT_DISK_FREE_GB`, default 10 GiB) e avisa no feed/Telegram antes de
    o volume encher. Para auditoria read-only das tabelas usa
    `.venv/Scripts/python maintenance.py --db-audit`; para uma verificação
    manual do volume usa `maintenance.py --disk-check`.
- Se o `/api/status` mostrar `mode: sim` no execution → o servidor não
  carregou o `.env` (ou o MT5 não está a correr) e o saldo mostrado é o de
  papel, não o do broker. Em `mt5-real`, `execution.hedged` tem de ser `true`;
  uma conta netting aparece como `blocked_reason` e o servidor não envia ordens.
- **Listener (Telethon):** `PYTHONIOENCODING=utf-8 nohup .venv/Scripts/python -u -m app.telegram_listener >/dev/null 2>&1 &`
  - O listener abre e roda `listener.log`/`listener.err.log` sozinho; não uses
    redireção externa para permitir a rotação no Windows.
  - Ao arrancar, repõe o histórico do grupo (`historical=True`) — por omissão
    **todas** as mensagens desde o início; `SIGNALBOT_TG_HISTORICAL_LIMIT`
    limita a N (`0` = todas; alias antigo `SIGNALBOT_TG_HISTORY_LIMIT`).
    Já não são só as últimas ~1000/200 — as páginas de teste usam o
    histórico completo (days=0). No fim do replay, o listener conta as
    mensagens por ação (sinais/gestão/resumos/ignoradas) e avisa o servidor
    (`POST /api/listener/history_done`) — o feed mostra
    "📚 Replay do histórico concluído" com os totais.
  - Credenciais vêm do `.env` (`SIGNALBOT_TG_API_ID/HASH/PHONE/GROUP`).
- **Estado rápido:** `curl -s http://127.0.0.1:8000/api/status | python -m json.tool`

## Verificação rápida de saúde

```
.venv/Scripts/python -m pytest -q          # 308 testes (atualizar quando a suite crescer)

curl -s http://127.0.0.1:8000/api/status   # mode, balance, open_trades
curl -s http://127.0.0.1:8000/api/diagnostics   # MT5, listener, uptime, BD
```

- A tab **"🔧 Diagnóstico"** da dashboard mostra o estado ao vivo: MT5
  (ligação/conta/AutoTrading/**tipo hedged**), **listener do Telegram** (heartbeat a cada 1s;
  sem notícias há >5s = OFFLINE; última mensagem do grupo com idade; gráfico
  de uptime 24h), servidor (uptime/PID), market feed, auth e contagens da BD.
  `GET /api/diagnostics` + `POST /api/heartbeat` (enviado pelo listener com
  `{groups, last_msg_at, tick}`). A tab tem também um botão "🧹 Limpar
  reinícios" (`DELETE /api/listener_restarts`, com confirmação) para apagar
  TODO o histórico à mão — o purge automático de 30 dias continua ativo; e
  um bloco "⏱️ Atraso das edições do grupo" (`GET /api/edits`) que mostra a
  distribuição do atraso das edições (mín/média/mediana/p90/máx, a mediana
  verde/vermelha face à janela atual) — para validar os 8s com dados reais.
  A coluna `edited_at` em `signals` regista a hora de cada edição.
- **Auto-restart do listener**: o servidor tem um watchdog (loop a cada 10s
  em `main.py::listener_watchdog`) que reinicia o processo do listener
  quando o heartbeat parar. `_listener_running_anywhere` usa
  `Get-CimInstance` (NÃO `wmic`, removido em Windows recentes) para não
  spawnar duplicados — o `wmic` em falta era a causa raiz dos listeners
  duplicados. O mesmo loop faz **purge automático** dos
  registos de `listener_restarts` com mais de 30 dias (no arranque e a
  cada ~6h — a tabela não cresce sem limite). Se o Telegram pedir código de login novo, o
  listener avisa (`POST /api/listener/need_code`), a dashboard mostra um
  toast com input e o código vai por `POST /api/listener/code`; o listener
  busca-o one-time em `GET /api/listener/code`. O heartbeat arranca antes
  da auth para o watchdog não reiniciar durante o arranque.
- **Alertas OFFLINE no telemóvel (Telegram)**: o listener faz DM à própria
  conta (Saved Messages) quando o MT5 desliga ou o AutoTrading off
  (`mt5_watch`, polling ao `/api/diagnostics` a cada 10s, cooldown 60s); o
  servidor emite notificação `risk` no feed na queda do MT5 (`state.py::
  _check_mt5_connection`) e quando o watchdog reinicia o listener offline.
  Com `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` no `.env`, **todas** as
  notificações são empurradas para o telemóvel via bot (@BotFather).
- **"Última msg do grupo"**: o listener só atualiza `last_msg_at` em
  mensagens reais — o replay do histórico no arranque NÃO conta (a
  dashboard mostra "sem mensagens ainda" até chegar a primeira do grupo).
- ⚠️ **Windows/Python 3.14**: cada processo python da venv aparece em PAR
  (launcher + interpretador real) — não são duplicados. Para matar só os
  listeners: `powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*telegram_listener*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"`.
  Para matar tudo: `taskkill //IM python.exe //F`. O `kill` do Git Bash não
  mata processos Windows.
- Se o servidor responder mas `/api/status` devolver dados antigos/errados →
  **reiniciar o servidor** (código antigo em memória).
- Se a dashboard mostrar "simulação" no badge de execução → o servidor pode
  ter caído para paper (`EXECUTION_MODE` não setado) ou o MT5 desligou.
- **Nunca** correr `git push` / commits sem o utilizador pedir.

## Operação e segurança da VPS

- **Conta MT5**: a estratégia N-pernas exige `margin_mode=2` (`RETAIL_HEDGING`).
  Em `MT5_DRY_RUN=0`, `MT5Provider.initialize()` falha fechado se o modo for
  netting/desconhecido, fecha a ligação e expõe `execution.blocked_reason`;
  nenhuma ordem é enviada. `GET /api/diagnostics` mostra `margin_mode`,
  `hedged` e o motivo do bloqueio.
- **Logs**: o entry point `app.server_runner` e o listener usam streams UTF-8
  rotativos. O limite é configurável por `SIGNALBOT_LOG_MAX_BYTES` (default
  10 MiB) e `SIGNALBOT_LOG_BACKUP_COUNT` (default 5), incluindo os ficheiros
  `.err.log`; `backup.log` usa a mesma política. A manutenção não apaga o
  histórico analítico de sinais/trades/positions/paper.
- **Disco/BD**: o watchdog verifica diariamente o volume configurado e o
  snapshot expõe `operations.disk`; `maintenance.py --db-audit` mostra linhas,
  meses observados e política por tabela. `backup_db.py --restore-check` usa
  uma BD temporária e nunca altera a BD de produção.

## Arquitetura (ficheiros que importam)

| Ficheiro | Papel |
|---|---|
| `app/main.py` | FastAPI: endpoints, wiring do motor (sim vs mt5), lifespan |
| `app/background.py` | Runtime lifecycle-owned dos loops de mercado/watchdog; cancela as tasks no shutdown |
| `app/execution/parity.py` | Vocabulário comum de observação para comparar decisão, SL, razão e P&L dos motores |
| `app/state.py` | `BotState`: pipeline de sinais, gestão do grupo, edge, resumo diário |
| `app/telegram_listener.py` | Telethon: lê o grupo e faz POST a `/api/signals` |
| `app/signals/parser.py` | Parse do sinal (entrada/SL/TP1..TP5) + `validate_signal()` |
| `app/messages/manage.py` | **Classificador das mensagens de gestão** (ver abaixo) |
| `app/messages/summaries.py` | Parse dos resumos diários/semanais (ground truth) |
| `app/signals/llm_fallback.py` | Parser LLM opcional (fallback, nunca executa em auto) |
| `app/execution/engine.py` | SimEngine (paper) — mesma interface do MT5Engine |
| `app/execution/mt5.py` | MT5Engine — ordens reais/dry-run no MetaTrader 5; bloqueia conta real não-hedged |
| `app/maintenance.py` | Rotação bounded de logs, check de disco, auditoria DB e teste de restauração |
| `app/server_runner.py` | Entry point Uvicorn que instala os logs rotativos antes do servidor |
| `maintenance.py` | CLI read-only para auditoria de tabelas e espaço em disco |
| `app/execution/protection.py` | Núcleo puro de decisões de proteção comum aos motores (escada/trailing/ATR/confirm/time stop + merge do `protect`) |
| `app/backtest.py` | Replay de sinais reais em candles históricos (+ gestão) |
| `app/analysis.py` | Edge analysis, A/B/C compare, walk-forward |
| `app/db.py` | SQLite (sinais, trades, posições, summaries, config) |
| `app/static/style.css` + `common.js` | CSS partilhado + JS comum (helpers, boot, WS, chrome global) — carregados por todas as páginas |
| `app/static/index.html` | Dashboard principal (JS vanilla, sem framework) — tabs sinais/posições/histórico/relatório/ignoradas + feed + corrida paralela |
| `app/static/teste.html` | Página de teste de estratégias (`/teste?s=<id>`) — simulação, walk-forward, corrida paper |
| `app/static/diagnostico.html` | Página de diagnóstico (`/diagnostico`) — MT5, listener, BD |
| `app/static/help.html` | Página de ajuda (`/help`) — estratégias + glossário |
| `tests/` | `test_api.py`, `test_backtest.py`, `test_management.py` |

**Modelo de concorrência:** todo o estado vive no event loop do FastAPI;
o SQLite tem um lock interno (`DB.tx`). Não há corridas.

**Lifecycle dos loops:** `app/background.py::BackgroundRuntime` cria exatamente
uma task de mercado e uma de watchdog durante o `lifespan` e, no shutdown,
marca o stop, cancela e aguarda as duas. O isolamento é explícito: `TestClient`
e a suite MT5 não deixam `market_loop`/`listener_watchdog` pendurados num event
loop fechado; a reconexão MT5 continua no loop de mercado com throttle de 10s.

## Reconhecimento de mensagens (onde o utilizador vai pedir melhorias)

O utilizador vai pedir ajustes ao "reconhecimento das mensagens". Dois níveis:

### 1. Sinais de entrada (`app/signals/parser.py`)
Mensagens tipo `I'm buying XAUUSD 4400-4396 TP1 4402 TP2 4404 ... SL 4392`.
O Golden Moves usou **3 formatos** ao longo do tempo e o parser reconhece
os três:
1. **Atual (multilinha)**: `I'm buying XAUUSD\n\n4400-4396\n\nTP1 4402 … SL 4392`;
2. **Antigo (uma linha, emojis)**: `XAU USD SELL NOW 4737-4741 🥇TP1 4734
   🥈TP2 4732 … 🚫SL 4745` — símbolo com espaço, medalhas antes dos TPs,
   `🚫SL`/`SL/ invalid` como stop;
3. **Mais antigo (Targets)**: `Sell Gold Now 4123.5-4128.5 Targets 4121.5
   4119.5 4117 Open SL/invalid 4131.5` — Targets é lista sem rótulo
   (TP1/TP2/TP3 por ordem), `GOLD`→XAUUSD.
- `parse_or_error()` devolve `Signal` ou um `Signal` com `.error` preenchido.
- `validate_signal()` é partilhado com o LLM fallback — **sanity checks
  obrigatórios** (ordem dos TPs, SL do lado certo, distâncias).
- Typos conhecidos já rejeitados: `TP2 421`, `TP2 4486`, `TP3 4242`, zonas
  impossíveis (`4379-4775`).
- Zonas de reentrada ("Next Buy Zone 4045-4040") NÃO são sinais — ficam
  em `failed`/ignoradas (não têm TP1/TP2 rotulados).
- "Buy Stop 💣" / "Sell Stop 💣" **NÃO são ordens executáveis** — são avisos
  do grupo ("prepara-te para comprar/vender no stop") sem símbolo, entrada,
  TP ou SL (o sinal real vem depois, ~1h mais tarde). Ficam em `failed`/
  ignoradas — não são bug.

### 2. Gestão do grupo (`app/messages/manage.py`)
Mensagens de gestão NÃO esperam a janela de edição — agem na hora. O
classificador `classify()` devolve um `kind`:

| `kind` | Exemplo do grupo | Ação |
|---|---|---|
| `tp_hit` (tp 1..5) | "TP1 HIT +70 pips" | TP1 → SL das restantes a BE; outros → registado |
| `sl_hit` | "Oh wow SL HIT" | Registado + reconciliação |
| `close_tp1` | "Close TP1 here" (sem move SL) | Fecha só a perna 1 (restantes mantêm SL) |
| `close_tp` (`tp` 2..6) | "Close TP2 here", "Close TP5 when happy" | Fecha só a perna N ao mercado (restantes mantêm SL) |
| `protect` | "close first entries and move stop loss above best entries" | Fecha perna 1 + SL das restantes acima da entrada |
| `close_all` (`be` true/false) | "we're out", "out at BE" | Fecha tudo (BE → à entrada) |
| `go` | "Do it now team" | Confirma entrada: sinal em `awaiting_edit` executa já (auto) / pendente (manual); sem sinal em espera só regista |
| `move_sl` (`sl` opcional) | "Adjust SL to 4331", "Move SL to 4064" | Move o SL de todas as pernas abertas para o nível indicado (sem nível = nudge registado, sem ação) |
| `summary` | "**Weekly summary** ... 26 Trades Sent ..." | Guarda ground truth em `group_summaries` |

**Edições TP1/SL do sinal alvo** (`_handle_tp_sl_update`): mensagens tipo
`TP1 4734 SL 4745` que RESPONDEM a um sinal (reply_to_msg_id) atualizam o
TP1/SL do sinal alvo (ground truth) e movem o SL das pernas abertas da trade
correspondente — antes ficavam em `failed`.

**Correlação com a mensagem original (reply do Telegram):** o listener captura
o `reply_to_msg_id` de cada mensagem (Telethon: `message.reply_to`) e envia-o
no POST. O handler `_handle_management` usa essa relação para escolher o alvo:
1. a trade do sinal a que a mensagem **responde** (correlação exata — duas
trades XAUUSD abertas não confundem o alvo); 2. trade aberta do mesmo símbolo;
3. trade aberta mais recente. Se o sinal alvo ainda estiver na janela de
edição (sem trade), a instrução vai para a tabela `pending_management` e é
aplicada automaticamente quando a trade do sinal abrir (`_apply_pending_management`
em `_try_execute`).

Padrões importantes do classificador:
- **A ordem importa**: summaries primeiro (contêm "volume"/"watch"), depois
  SL/TP hits (incl. "TP5 SMASHED/KISSED/tapped" e "SL already hit"), depois
  close_all/out at entry/breakeven, depois protect/close_tp1/close_tp ("Close
  TP2..TP6 here").
- Apóstrofos tipográficos são normalizados (`we’re` → `we're`).
- O classificador é **conservador**: um falso "close_all" fecha posições
  reais. Ao adicionar um padrão novo, testar contra as mensagens reais da BD.
- `Signal.reply_to_msg_id` guarda a relação de resposta (id do sinal alvo);
  `pending_management` (BD) fila instruções cujo alvo ainda não tem trade.
- A dashboard mostra essa relação no histórico: "📨 gestão · → responde ao
  sinal 10:53 XAUUSD".
- Mensagem de gestão **sem alvo** (sem trade aberta e sem sinal a que
  responder) dispara uma notificação `risk` (⚠️ "Gestão do grupo sem alvo") —
  nunca fica silenciosa.
- **Atribuição de saída**: cada perna/trade fechada regista `exit_reason`
  (sl/tpN/protect/close_all/close_tp1/close_tp/move_sl/killswitch/manual) e
  `exit_msg_id` (id da mensagem de gestão que causou o fecho) — colunas novas
  em `positions`/`trades`/`paper_positions`/`paper_trades` (migração
  automática no arranque). A tabela de trades da dashboard mostra a razão do
  fecho e o **contrafactual "sem gestão"** (`counterfactual_pnl` = P&L da
  variante `nova` da corrida paper para o mesmo sinal) — o link ao vivo
  entre o backtest e o que a gestão realmente acrescentou.
- **`manage_kinds` na conta real**: quando a estratégia ativa não honra um
  kind (ex.: `tp2_protect` não honra `close_all`), a instrução é registada e
  notificada ("não aplica à estratégia atual") mas NÃO fecha posições — a
  corrida paper continua a aplicar nas estratégias que a honram.

### Testar um novo padrão contra as mensagens reais

As mensagens reais estão na tabela `signals`. Para ver como o classificador
trata cada uma:

```python
from app.config import DB_PATH
from app.db import DB
from app.messages.manage import classify
db = DB(DB_PATH)
for r in db.conn.execute("SELECT raw_text FROM signals WHERE status='management' OR status='historical'").fetchall():
    print(classify(r["raw_text"] or ""), "|", (r["raw_text"] or "")[:70].replace("\n", " "))
```

## Refinar o reconhecimento (protocolo do utilizador)

O utilizador vai mandar **"refinar o reconhecimento"** (ou variantes: "o grupo
mandou X e o bot não percebeu", "há mensagens que ficam failed", "melhora o
reconhecimento das mensagens"). Quando isso acontecer, executa ESTE protocolo
sem pedir esclarecimentos:

1. **Diagnóstico** — corre um script que lista as mensagens que o bot perdeu:
   ```python
   from app.config import DB_PATH
   from app.db import DB
   from app.messages.manage import classify
   from app.signals.parser import parse_or_error
   db = DB(DB_PATH)
   for r in db.conn.execute(
       "SELECT id, raw_text, status FROM signals ORDER BY received_at DESC LIMIT 200"
   ).fetchall():
       txt = r["raw_text"] or ""
       sig = parse_or_error(txt, source="x", signal_id="x", received_at="")
       if r["status"] == "failed" or (sig and sig.error) or classify(txt):
           print(f"[{r['status']}] {classify(txt)} | {sig.error or ''} | {txt[:90]}".replace("\n", " "))
   ```
   Também ver o log do listener (`tail -50 listener.log`) para mensagens
   ignoradas (o log mostra o texto) e o feed da dashboard para notificações
   "sem alvo". Mensagens que são conversa pura não são bug — estão na tab
   "🚫 Ignoradas" (auditoria).
2. **Classifica o problema**:
   - mensagem de **entrada** mal parseada → `app/signals/parser.py`;
   - mensagem de **gestão** não classificada → `app/messages/manage.py`;
   - **formato novo** que nenhum dos dois cobre → decide se é sinal ou gestão
     e adiciona o padrão ao módulo certo (o parser é regex; o classificador é
     regex por `kind` — ver a tabela acima).
3. **Implementa de forma conservadora**: um falso "close_all"/"protect" fecha
   posições reais — os padrões novos devem casar a mensagem do grupo SEM
   apanhar conversa. Testa sempre contra as mensagens reais da BD (script
   acima) antes e depois da alteração.
4. **Teste de regressão** — adiciona o caso ao ficheiro de testes certo:
   `tests/test_management.py` (classificador), `tests/test_api.py` (fluxo
   API/execução) ou `tests/test_backtest.py` (sim).
5. **Suite completa** — `.venv/Scripts/python -m pytest -q` (308 testes).
6. **Reiniciar o servidor** (código Python não recarrega em produção) e
   confirmar com `curl http://127.0.0.1:8000/api/status`.
7. **Responder ao utilizador** com: o que estava a falhar, o que mudou, e o
   resultado dos testes.

> Nota: mensagens antigas do histórico não voltam a ser reprocessadas (dedup
> por message_id). Alterações ao reconhecimento aplicam-se às mensagens
> NOVAS do grupo — se o utilizador quiser re-analisar o histórico, limpar as
> linhas antigas da BD antes de reiniciar o listener (cuidado para não apagar
> trades reais).

## Fluxo de um sinal

1. Listener → `POST /api/signals` com `message_id` (e `historical=True` para
   o histórico). O listener reencaminha **tudo** (`SIGNALBOT_TG_FILTER=0` no
   `.env`) — quem decide é o backend.
2. `state.ingest()` → `_ingest_telegram()`:
   - **classify corre ANTES da dedup** — gestão/resumos históricos re-entram
     no handler (e `historical=True` nunca age em trades abertas);
   - **conversa do grupo** (sem keywords de sinal nem gestão, regex
     `_SIGNAL_HINT` em `state.py`, igual ao antigo filtro do listener) →
     registada na tabela `ignored_messages` e devolvida como `ignored` —
     nunca entra na pipeline nem na janela de edição;
   - **sinal completo executa já** (primeiro parse válido, sem esperar);
     só sinais **incompletos** entram na janela de edição (`awaiting_edit`,
     8s) — o `_SIGNAL_HINT` inclui `buying|selling|comprando|vendendo` porque
     o grupo envia primeiro "I'm selling XAUUSD" e edita a mensagem com
     entrada/TP/SL; os detalhes (TP/SL/entry/target) só contam como sinal
     quando trazem um **número** a seguir — "TP1 4396" é sinal, mas "after
     TP1"/"as SL" (conversa) não; uma mensagem que foi auditada como
     conversa mas é editada para sinal limpa o registo de ignorada
     (`delete_ignored`) para não aparecer nas duas tabelas;
   - **revive**: um sinal que falhou (ex.: entrada incompleta sem TP/SL) é
     revivido se o grupo completar a edição dentro de 5 min — reabre a
     janela com o conteúdo novo em vez de ser ignorado (`action: revived`);
     edições de sinais já executados/pendentes continuam a disparar alerta
     `risk` (janela passada = entrada possivelmente desatualizada);
   - depois da janela: `manual` → pending; `auto` → risk checks + execução.
3. Execução: `SimEngine` ou `MT5Engine` (`open_trade` com N pernas; `update`
   gere TP1→BE, SL, TPs individuais).
   - **`max_positions` = 5** (default; antes 3): a estratégia de 5 pernas
     precisa de até 5 entradas em simultâneo — o limite 3 rejeitava sinais
     com "máximo de posições abertas atingido (3)".
   - **`MT5Engine._send_open_orders`** já não crasha quando
     `symbol_info_tick` devolve None (símbolo fora do Market Watch) — o
     `ensure_symbol` é chamado ANTES de enviar ordens e o tick None falha só
     a perna (não repete a trade). O `_try_execute` também finaliza o sinal
     como `failed` em qualquer exceção de `open_trade` (antes ficava em
     `awaiting_edit` e o tick reabria a trade a cada segundo — duplicados).
   - **Retcode 10027 (AutoTrading OFF)**: `_send_open_orders` recolhe os
     retcodes reais de cada perna recusada; quando 10027 aparece, a trade
     fica `auto_trading_off=True` (transiente) e o `_try_execute` emite um
     alerta risk **imediato e distinto** ("⛔ AutoTrading DESLIGADO…") — não
     só o erro no log. Retcodes diferentes só registam o erro normal.
4. Gestão do grupo: `_handle_management()` age na trade aberta do mesmo
   símbolo (ou na mais recente).

## Auditoria de mensagens ignoradas (dashboard → tab "🚫 Ignoradas")

- `GET /api/ignored` — últimas mensagens que o bot decidiu **não registar**
  (conversa do grupo: "Good morning", "+50 pips", links, etc.), com hora,
  origem e texto completo. Tabela `ignored_messages` (dedup por
  message_id+source, mantém as últimas 1000).
- O snapshot inclui `ignored_count` para o badge da tab atualizar ao vivo.
- O log do listener `[skip]` mostra o texto das mensagens ignoradas.

## Backtest / análise (sem tab Edge — vive nas páginas de teste e no alerta de regime)

- `GET /api/analysis` — edge do grupo (variante nova, TP1..TP5); o alerta de
  regime usa-o por trás, já não tem tab própria na dashboard.
- `GET /api/analysis/compare` — A/B/C: nova (TP1..TP5) vs **nova + gestão**
  vs fechar tudo no TP2 (simula `protect`/`close_all`/`close_tp1` acoplados
  por fonte+tempo, com SL-first e fills sem look-ahead). A estratégia antiga
  (TP1/TP2 only) foi removida do compare.
- `GET /api/analysis/walkforward` — edge da gestão por janelas temporais
  (estabilidade do PF ao longo do tempo) — mostrado nas **páginas de teste**.
- `GET /api/analysis/protect_latency` — telemetria persistida de `protect` da
  híbrida (paper/MT5) ou replay fresco (`engine=backtest`): vencedor, pips e
  segundos, com distribuição.
- `GET /api/analysis/protect_causal` — replay read-only do histórico completo
  (`days=0`) que compara a híbrida com a mesma auto sem `protect`, atribui o
  delta incremental por evento e expõe os piores eventos.

- `GET /api/analysis/month` — **simulação**: replay dos sinais reais com
  gestão desde há N dias até hoje (ou `days=0` = **todo o histórico na BD**,
  desde o início do grupo), com a curva de equity acumulada (`run_month_sim`
  em analysis.py). As **páginas de teste** chamam-no com `balance=1000` (o
  saldo próprio da conta paper de cada estratégia — não o saldo da conta
  MT5); o saldo default é **1000** (o saldo próprio da conta paper da
  estratégia, via `testeSimQS()`, com seletor de risco/saldo para estudos).
  Mostra "se tivesses começado há 1 mês com X €, hoje terias Y €" +
  mini-curva — nunca abre trades.
- `GET /api/summaries` — resumos do grupo (ground truth).
- Convenções do sim: risco dividido por N pernas; contract por símbolo
  (XAUUSD 100, XAGUSD 5000, forex 100000); custos reais (spread, slippage,
  comissão); fills sem look-ahead (nunca usar o close do candle do evento).
- **SimEngine/PaperRace também usam o contract por símbolo** (`engine._contract`/
  `_pip_size`, partilhados com o `paper.py`): o P&L e o dimensionamento das
  trades simuladas/de papel passam a usar 100 para XAUUSD, 5000 para XAGUSD
  e 100000 para forex — antes usavam 100000 para tudo, o que inflacionava o
  P&L de ouro 1000× ("a corrida paper tinha muito dinheiro").
- **Item 1 fechado antes do Item 2**: a `auto_escada_3_hibrida` foi comparada
  com a auto pura nos 30 dias e no histórico completo; os resultados atuais
  são iguais (PF 3.20/DD 24.6% no recorte de 30 dias e PF 2.21/DD 32.5%
  no replay completo). A atribuição causal (`protect_causal.json`, 355
  eventos em 270 sinais) encontrou **0 eventos harmful e delta 0 €**: os
  `protect` reais não pioraram a auto; as perdas são do caminho mecânico/base.
  A paridade parametrizada em `tests/test_engine_parity.py` envia os mesmos
  eventos buy/sell ao backtest, SimEngine, PaperRace e MT5 dry-run e compara
  decisões, SL aplicado, razão e P&L. Depois desta validação, o spec
  `docs/Melhorias/01-item-01.md` foi apagado antes de começar o Item 2.
- **Item 2 — sweep defensivo da `auto_escada_5`**: repetir com
  `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation
  --item2-sweep`. A grelha 3×3×3 (`def_window` 5/7/10 × `def_trail`
  1.0/1.2/1.5 × `def_time_stop` 30/45/60) reutiliza o baseline por janela e
  não tem leakage. O resultado de 19/08 não encontrou combinação com DD
  antiga <50% (0 elegíveis); há um pico isolado de PF no eixo window no
  período atual, por isso nenhum parâmetro default foi congelado.
- **Revisão de prontidão da Fase 3 (19/08)**: a integração
  `tests/test_mt5.py::test_fase3_promotion_paper_to_mt5_restart_and_rollback`
  cobre paper → promoção → MT5 com broker fake, restart/reconstrução do
  estado, retcodes recusados e rollback. O veredicto usa exatamente as
  primeiras 10 trades abertas após a promoção, exige saída igual ao paper em
  ≥9/10 e reprova se a evidência paper↔MT5 faltar; a demo continua sem
  ordens de teste.
- **Item 3 encerrado (19/08)**: `PYTHONIOENCODING=utf-8 .venv/Scripts/python
  -m app.study_consolidation --item3-sweep` compara `confirm_filter=True` e
  `False` nas janelas atual/tudo/antiga, mantendo escada 3/1 e trail 2.0.
  O resultado passou o gate: PF atual 1.27→1.26 (queda 0.79%) e DD antiga
  312.9%→278.3%. `auto_escada_3` ficou com `confirm_filter=True` por default;
  a principal continua `nova_gestao` e não houve promoção MT5. O spec
  `docs/Melhorias/03-item-03.md` foi apagado depois da aceitação.

**Resultado atual:** o reconhecimento cobre os **3 formatos de sinal** que o
Golden Moves usou (atual "I'm buying XAUUSD …" multilinha, antigo "XAU USD
SELL NOW …" com emojis 🥇 e 🚫SL, e o mais antigo "Sell Gold Now … Targets …
SL/ invalid") — o histórico completo tem **578 sinais reais de XAUUSD**
(abr→hoje). No período atual (30 dias, formato novo): PF nova 1.1,
nova+gestão **2.92**, fechar no TP2 0.82. O período antigo (abr–jul) era
perdedor no backtest (PF < 1) — o edge da gestão só aparece no formato
recente. Drawdown > 100% possível porque o sim usa risco % fixo sobre o
saldo inicial (sem parar em falência).

## Estratégias (registo central + páginas de teste + paper trading forward)

- **`app/strategies.py`** — registo central: cada estratégia define `max_legs`,
  `manage_kinds` (quais kinds de gestão honra — `manage=True` legacy = todos),
  `exit_at_tp` e `lots_override`. `MAIN_STRATEGY_ID = "nova_gestao"` (a melhor
  segundo os dados) é a que a conta principal executa. Consolidação (19/08,
  spec): `HIDDEN_STRATEGIES = {tp2, tp2_gestao, nova_gestao_3, auto_2}`
  (fora do menu E do paper — só backtest/compare; perdedoras/redundantes) e
  `MENU_HIDDEN_STRATEGIES = {nova, auto_5}` (ocultas do menu mas continuam em
  PAPEL — a `nova` é o contrafactual "sem gestão" do histórico de trades
  (`counterfactual_pnl`) e a `auto_5` a comparação 5 pernas com/sem trail).
  Ficam **6 visíveis no menu**: `nova_gestao`, `tp2_protect`, `auto_3`,
  `auto_escada_3`, `auto_escada_3_hibrida`, `auto_escada_5`. A híbrida é
  experimental: combina a auto-gestão com `protect` monotónico do grupo e
  não altera a principal `nova_gestao`. Para repor basta tirar o id do set (a
  maquinaria mantém-se, nada se apaga do backtest). O filtro
  `manage_kinds` aplica-se à conta
  real (quando promovida), ao backtest (`simulate_signal_managed`) e à
  corrida paper. Adicionar estratégia nova = 1 entrada no dicionário + 1
  item no menu (`index.html`, `TERM_ORDER` e `BEST_PAGE`).
- **Estudo da consolidação (`app/study_consolidation.py`)** — filtro de
  entrada (sessão Londres/NY + distância entrada→TP2 ≥ 5 pips + ATR p20) e
  modo defensivo (PF das últimas 10 trades < 1.0 → trail 1.5 + time stop 60
  min, SEM cortar pernas) nas 3 janelas. Veredicto (19/08): o filtro de
  entrada NÃO tem edge (rejeita quase só no período antigo onde tudo perde,
  a sessão não filtra nada no atual, ATR = ruído; a sensibilidade só-ATR
  rejeita 0) — não implementado; o **modo defensivo foi IMPLEMENTADO** nas
  `auto_escada_3/5` (parâmetros `def_window`/`def_trail`/`def_time_stop` no
  registo) — espelho nos 3 motores sem look-ahead (backtest `def_state`
  partilhado; paper `paper_closed_recent`; MT5 real `recent_closed_pnls`),
  com validação na corrida paper antes da Fase 3.
- **Item 3 — confirmação na `auto_escada_3`**: o sweep binário
  `--item3-sweep` corre os mesmos sinais/candles/custos com apenas
  `confirm_filter` alternado; o gate exige DD antiga estritamente menor e
  perda de PF atual ≤10% relativa. O resultado de 19/08 passou (1.27→1.26;
  312.9%→278.3%), por isso o default da estratégia é agora `True` — isto
  não muda a principal nem promove a estratégia para MT5.
- **Auto-gestão (docs/ESTRATEGIAS.md, Parte V — fonte única de estratégias;
  o antigo `docs/SPEC-auto-gestao.md` foi consolidado e apagado em 19/08)**
  — variantes que substituem a gestão do grupo
  por regras mecânicas: **escada de proteção** (quando o TP_k bate, o SL das
  pernas seguintes sobe para o nível do TP_k), **trailing** (`trail_factor` =
  múltiplo da distância entrada→TP2 que o SL pode recuar do pico; 2.0 ≈ 8
  pips em gold), **filtro de confirmação** (perna intermédia leva SL →
  longínquas fecham), **time stop** (pernas abertas N min após TP1 → fecham
  ao mercado) e **modo defensivo** (PF(10) < 1.0 → trail 1.5 + time stop 60,
  sem cortar pernas). Ferramentas: `app/mine_management.py` (minera a gestão real
  do grupo para calibrar parâmetros) e  `app/sweep_auto.py` (sweep das
  variantes no período atual + antigo out-of-sample). `app/study_consolidation.py
  --item2-sweep` faz também a grelha reproduzível do modo defensivo da
  `auto_escada_5`; resultado do Item 2: DD antiga permanece ~85.5–87.4%,
  portanto não há promoção de default. Resultado do sweep

  (18/08): a escada/confirm/time stop NÃO têm edge sozinhos; o **trailing é
  o mecanismo** (trail_factor 2.0 ≈ 8 pips em gold é o valor honesto — 0.5 é
  top-picking irrealista). **Fase 2 (paper)**: a `auto_escada_3` (PF 3.11
  atual / 1.41 antigo vs referência 3.28 / 0.63) saiu de `HIDDEN` — página
  de teste + **corrida paper** com a auto-gestão portada para o tick do
  `PaperRace` (mesma sequência do backtest, sem look-ahead) e a `nova_gestao`
  como referência paralela; o **divergence report** (`⚖️` no feed, dedup por
  sinal) compara a saída da auto vs a referência no mesmo sinal (incluindo
  "o grupo saiu, a auto continua"); `tp2_protect` também saiu (só `protect`
  do grupo, validação do estudo). O resto (`auto_2/3/5`, `auto_escada_5`)
  continua `HIDDEN` (só backtest/compare). Sensibilidade do trail 1.5–2.5:
  plateau monotónico (PF 1.97–4.69 atual, 1.28–1.57 antigo — sem pico
  isolado, logo sem sobreajuste); 2.0 congelado como meio do plateau.
  **Fase 3 (MT5 demo)**: protocolo pronto no ESTRATEGIAS.md (Parte X) —
  pré-requisitos N≥20, janela de validação de
  10 trades, rollback. O **código da Fase 3 está implementado e desligado**:
  escada/trailing/confirmação/time stop ligados ao `_update_real` (só correm
  quando a estratégia ativa os define — a `nova_gestao` não tem nenhum),
  throttling de modify (≥1 pip, 1×/min), três níveis de SL
  (  `_valid_sl` — um modify recusado 10016 NÃO grava o SL na BD;
  `_modify_sl` devolve o retcode), **trailing adaptativo por ATR** com a
  MESMA fórmula `max(trail_factor×dist, atr_k×ATR)` nos três motores
  (`MT5Provider.copy_rates` M1/M5 + `_atr`; `_candle_atr` no backtest;
  `PaperRace._paper_atr` na corrida — baldes de minuto, sem look-ahead;
  **rejeitado pelo sweep** (`app/sweep_atr.py`: atr_k 1–5 × period 7/14/30
  em 3 janelas — o ATR é estritamente pior que o trail fixo 2.0; o ATR do
  ouro alarga o trail e blunts o fecho do lucro; fica desligado — ver
  ESTRATEGIAS.md Parte X (protocolo Fase 3); o `--trail` confirmou o plateau
  monotónico 1.6–2.4 e o 2.0
  interior), a **auditoria dos três
  níveis de SL** (`autosl:<trade_id>` na config, target→valid→applied +
  retcode, `GET /api/analysis/auto_sl_log` — o que a janela de validação
  audita; um modify recusado 10016/clamped dispara um aviso `risk` no
  feed/Telegram via evento `auto_sl_warn`) e um **teste E2E do caminho
  real com broker fake** (`tests/test_mt5.py`: TP1 por deal → escada →
  trailing → time stop, retcodes 10009/10016 no log + auditoria). A principal tem um **painel
  "🚀 Fase 3 — pronto a arrancar"** (checklist dos gates: N≥20 · **3/3**
  (PF≥1.5 · P&L≥50% · DD≤125% — gate estrito, sem comité 2 de 3),
  com os counts do divergence paper↔MT5, real vs backtest, DD e a
  auditoria de SL; a janela de 10 trades avalia-se na validação). O painel mostra
  também o **ETA dos 20 sinais** (`snapshot()["fase3_eta"]`: ritmo de
  sinais/dia = trades de papel abertas da auto_escada_3 nos últimos 30
  dias, data = `now + (20−N)/ritmo`; `done` quando N≥20) e o **veredicto da
  janela de validação** (`fase3_verdict`): `state._check_fase3_validation`
  corre no tick quando a auto está promovida e 10 trades reais fecharam
  desde `promoted_at` — PF real da janela + mesma saída que o papel
  (`same_exit_n`, o ≥ 9/10 do doc) + divergências estruturais
  (paper↔MT5 com saída diferente/papel aberto) + posições sem SL →
  **rollback automático para a nova_gestao** (PF<1 · ≥2 divergências · sem
  SL · ordens duplicadas · retcodes recusados ≠ 10009/0 na auditoria
  `autosl:`) ou **manutenção**, notificado no feed/Telegram, uma vez por janela
  (nova promoção grava snapshot novo e reabre; o **histórico de veredictos**
  fica em `fase3_verdicts`, nunca se apaga — bloco "📚 Histórico" no painel).
  O ETA também compara o ritmo com o mês anterior (`pace_prev`/
  `pace_change_pct`) e alerta no feed quando a janela estimar **> 6 semanas**
  ou o ritmo mudar **≥ 50%** (cooldown 24h), e o **resumo diário** ganha a
  linha 🚀 da auto MT5 (N real, P&L real da janela) durante a Fase 3
  (ESTRATEGIAS.md Parte X.1). O **SPEC v3 (conta real)** é um rascunho de
  gates na Parte X.2 — nunca promover a conta real sem instrução explícita
  do utilizador.
  A página principal tem um **painel Fase 2** (saldo/PF da auto vs a
  referência, sinais resolvidos e progresso para os critérios: N ≥ 20 ·
  PF ≥ 1.5 · P&L ≥ 50% · DD ≤ 125% da referência) e a tab Relatório uma
  **tabela de divergências** (`GET /api/paper/divergences` — auto vs
  referência por sinal, razão/P&L lado a lado + motivo). As páginas de
  teste da Fase 2 têm botão **⬇️ CSV** (trades de papel da auto + referência).
  Estudo tp2 (30d, formato atual): tp2 puro PF 0.86/−151 €; **+ protect
  PF 2.65/+1 721 €** (o protect recupera ~86% do caminho até à gestão
  completa PF 2.93/+2 010 €); nova_gestao PF 3.22/+2 309 € — sair cedo só
  vale com a proteção do grupo, e a gestão completa continua a ganhar.
- **Config `strategy`** (`GET/POST /api/config`, validada contra o registo) —
  a estratégia ativa da conta principal. `_try_execute` passa `max_legs` ao
  motor e o tick passa `exit_at_tp` (SimEngine e MT5Engine suportam ambos;
  `lots_override` já existia). Consolidação 19/08:
  `HIDDEN_STRATEGIES`/`MENU_HIDDEN_STRATEGIES` já não estão vazios (ver
  secção `app/strategies.py` acima) — 6 visíveis no menu, 2 ocultas do menu
  mas em papel, 4 só no backtest.
- **Layout** (dashboard): a **navegação vive numa barra fixa no TOPO** (itens
  horizontais: PRINCIPAL · ▚ ESTRATÉGIAS (dropdown) · DIAGNÓSTICO · HELP;
  setas ↑↓ + Enter + rato) e o **terminal de estado numa barra fixa em
  BAIXO** (`▚ STATE`): badges (modo/estratégia/MT5/execução), o **pacote de
  preços** (GOLD..NZDUSD, verde/vermelho conforme o movimento + **barrinha
  da variação % do dia** com tooltip), 👥 relógio, **🛑 Kill switch** e
  **Sair**. O conteúdo vive entre as duas barras (`--nav-h`/`--foot-h`,
  variáveis CSS únicas — nunca é coberto em qualquer zoom; ≤960px o ☰ abre o
  menu e o ▦ o estado em overlay; cada barra tem o seu botão ‹ de colapso
  persistido). As **6 estratégias visíveis** vivem no **dropdown
  ▚ ESTRATÉGIAS**
  (toggle persistido em localStorage, default aberto; fecha ao clicar fora;
  a navegação por teclado abre-o sozinha ao chegar a um item lá dentro) —
  cada uma com página de teste própria `/teste?s=<id>` (saldo simulado de 1
  mês a começar em 1000 € + mini-curva + comparação + corrida paper ao
  vivo; a página do TP2 mostra também o **fluxo de exemplo de uma trade
  real** — `GET /api/analysis/trace`). Consolidação do utilizador (19/08):
  no menu ficam `nova_gestao`, `tp2_protect`, `auto_3`, `auto_escada_3` e
  `auto_escada_5`; `nova`/`auto_5` continuam em papel (ocultas do menu —
  contrafactual/comparação) e `tp2`/`tp2_gestao`/`nova_gestao_3`/`auto_2`
  saíram do menu e do paper (só backtest). A página **HELP** explica cada estratégia de forma
  interativa (cards expansíveis + números reais de 30 dias) + glossário. A
  página principal usa a melhor (a estratégia ativa na config — hoje
  `nova_gestao`) e o menu marca com **"✓ melhor"** a vencedora do A/B/C do
  histórico completo (`compare?days=0` → `winner`).
- **Kill diário no backtest**: `run_analysis`/`compare`/`month` modelam o
  kill switch da produção — `daily_stop_pct` (None = `daily_loss_limit_pct`
  da config; 0 = off) — as trades depois do limite do dia ficam
  `outcome=skip`/pnl 0 e contam em `daily_stop_skipped`. Os números da
  dashboard refletem o que a produção realmente faz.
- **Estudos de risco**: `/api/analysis`, `/api/analysis/compare` e
  `/api/analysis/month` aceitam `risk_pct` e `balance` opcionais (0 =
  config/saldo atual) — as páginas de teste têm os seletores "Risco do
  sim"/saldo (persistidos em `edge_risk`/`edge_bal`). ⚠️ Com saldo ~1000 € o piso de
  lote mínimo (0.01) domina o dimensionamento — riscos ≤ ~4% dão os mesmos
  lotes; para estudar risco usa um saldo maior (ex.: 10 000).
- **Risco dinâmico**: `state._effective_risk_pct()` — o `risk_pct` base
  (default **2.5%**, config `risk_pct`) escala para baixo após perdas:
  cada **-20% de drawdown** desde o pico de equity (`config peak_balance`,
  atualizado no tick via `_update_peak_balance`) corta o risco a **metade**
  (`0.5 ** (dd/20)`, piso 0.1%). `_try_execute` usa o risco efetivo (real +
  papel) e o snapshot expõe `effective_risk_pct` + `peak_balance`.
- **Alerta de regime**: `state._check_regime()` (a cada ~30 min no tick)
  corre o sim rolante de 30 dias da estratégia ativa com o kill diário e,
  se PF < 1 com ≥ 10 trades, notifica `risk` (feed + Telegram, cooldown
  6h); expõe `regime` no snapshot (pf/resolved/checked_at/skipped).
  A principal mostra também a **mini-curva "🏁 Corrida paralela"** (quem
  está a ganhar ao vivo: evolução em % do saldo de cada estratégia, com
  snapshots gravados a cada ~60s em `paper_balance_history` — **todas** as
  estratégias visíveis (incluindo a principal) usam a sua conta virtual
  própria de
  1000 €, para comparar em igualdade de circunstâncias; além das **6
  contas virtuais visíveis** (nova/auto_5 continuam em papel mas fora da
  curva; as 4 removidas já não abrem papel), a legenda
  mostra uma **linha de referência "⚡ MT5 real · conta
  real"** com o saldo real gravado no tick sob a estratégia reservada
  `__real__` (nunca entra na comparação — é o dinheiro a sério ao lado).
  Cada página de teste tem um
  botão **"⬆️ Promover a principal"** (POST `/api/config` `{strategy}`) e a
  principal mostra um **badge com a estratégia ativa** (clarão + toast quando
  muda) e um **aviso se alguma estratégia de teste está a ganhar à principal**
  na corrida paper. **Auto-switch por regime** (`state._check_paper_race_leader`):
  quando uma estratégia **visível** (fora de `HIDDEN_STRATEGIES`) se mantém à
  frente da principal por >= 30 min seguidos e com >= 3 trades fechadas,
  o bot envia uma **notificação `race` de "sugestão de troca"** (feed +
  Telegram) com um **botão "⬆️ Promover" no feed** (a notificação leva
  `action="promote:<id>"`; um clique faz `POST /api/config {strategy}` e
  volta à principal) — não troca sozinho. A tab "🔧 Diagnóstico" deixou de
  aparecer na barra de
  tabs da principal (tem página própria no menu). **Layout responsivo (zoom
  do navegador)**: a navegação vive numa **coluna fixa à esquerda**
  (`--term-w`) — o menu (PRINCIPAL, páginas de teste, Diagnóstico, HELP) e,
  **em baixo do menu (após HELP)**, o terminal **`termSide`** com a lista
  vertical de preços (**variação % do dia**, `market.changes`, do
  `chartPreviousClose` do Yahoo — verde/vermelho) e botão **‹** para
  colapsar/expandir (persistido em localStorage — colapsado fica só o
  cabeçalho ▚ MARKET + ›). O hint de teclado (↑↓ + ENTER) fica entre o
  HELP e o MARKET. No **header** (à direita da marca/subtítulo) ficam os
  badges de estado (`hdr-badges`): modo auto/manual, estratégia ativa
  (stack nome + parâmetros), MT5 real (conta · saldo) e execução real/
  dry-run (mais o `killBadge` PAUSADO quando o kill switch está ligado).
  À **direita** há um segundo terminal fixo **`termRight`** (`--term-r`):
  utilizadores online + relógio, **kill switch** e **Sair**, com o próprio
  botão ‹ de colapsar (`body.term-r-collapsed` encolhe para 38px). O
  header tem ☰ + marca + badges + ▦. Num ecrã ≥961px o conteúdo afasta-se
  dos dois terminais (`--term-w`/`--term-r` via variáveis CSS) — nunca é
  coberto em qualquer zoom; a largura/tipografia dos terminais encolhe em
  961–1120px e o menu passa a scrollar em alturas < 640px (o flex
  centrado cortava itens). ≤960px: o menu (com o MARKET no fundo) abre em
  overlay (☰) e o terminal direito em overlay (▦), tabelas e tabs fazem
  scroll horizontal interno (`overflow-x`), sem overflow da página em
  nenhum zoom. Nas páginas de teste e no Diagnóstico o body ganha
  `no-chrome` (stats/controlos/bloco de risco escondidos — só header +
  statusbar + terminal esquerdo ficam).
- **Promover uma estratégia** (`POST /api/config` `{strategy}`) regista uma
  **notificação no feed** ("♟ Estratégia promovida a principal: … — a
  execução real passa a usar: …") e o badge do header passa a mostrar, além
  do nome, os **parâmetros reais de execução** (`max_legs`/`exit_at_tp`/lote
  fixo/gestão) — vêm de `config.strategy_params` no snapshot
  (`state._strategy_params`, registo em `app/strategies.py`).
- **Cada página de teste mostra dois horizontes**: a simulação do último mês
  (seletor 7/30/90/**tudo** — persistido na config `teste_days`) E o bloco
  "📜 Desde o início do grupo" (`days=0` = TODO o histórico na BD, sem
  filtro de data) com veredicto, stats e curva próprios — para cada
  estratégia. O bloco tem um **seletor de período** (config `all_period`):
  todo o histórico / só o formato atual
  (`since=2026-07-28`, data da 1ª mensagem do formato atual) / só o mês
  corrente — mais o **P&L por mês** (barras) e o **P&L por perna**
  (chips leg1..leg5, só trades resolvidas, `pnl_by_leg`). Mais a **tabela
  de trades de papel**
  (`GET /api/paper/trades?strategy=<id>`) com o histórico da corrida
  paralela (abertas + fechadas com P&L, hora, símbolo, direção, entrada,
  lotes).
- **Paper trading forward** (`app/execution/paper.py` + tabelas
  `paper_accounts`/`paper_trades`/`paper_positions`): cada estratégia
  (**incluindo a principal**) tem a sua **conta virtual própria** (1000 €) e,
  quando um sinal real executa na principal, abre a MESMA trade em papel com
  os seus parâmetros;
  o tick atualiza contra os preços do market (SL/TP/BE + `exit_at_tp`) e o
  saldo virtual evolui. **A gestão do grupo também é aplicada em papel**: ao
  chegar uma instrução (protect/close_all/close_tp1/move_sl/tp_hit), a
  `PaperRace.apply_management(signal_id, kind, ...)` atua nas trades de papel
  do sinal alvo, mas SÓ nas estratégias cujo `manage_kinds` inclui o kind
  (correlação pelo signal_id da trade real alvo — as contas "+ gestão" já
  não se comportam como as sem gestão). A corrida é paralela à conta real —
  nunca a toca.
  Snapshot expõe `paper.accounts` (saldo/win rate/PF por estratégia +
  `daily_stop_skipped`) e `GET /api/paper/trades` o histórico de trades por
  estratégia. **Kill diário na corrida paper**: `open_for_signal` recebe o
  `daily_stop_pct` da config e, quando o P&L realizado do dia da conta
  virtual ≤ −saldo×limite/100, o sinal NÃO abre em papel nessa estratégia
  (`paper_realized_today`; espelho da produção
  `risk.engine.daily_loss_exceeded`/`apply_daily_stop` do backtest) — as
  contas virtuais refletem o que a produção real faria se a estratégia
  fosse a principal.
- **Real vs backtest (ESTRATEGIAS.md Parte X)**: `GET /api/analysis/real_vs_backtest`
  compara cada trade REAL fechada da demo com o backtest do mesmo sinal na
  estratégia principal — Δ entrada (preço real vs o fill do sim: entrada ±
  spread/2 ± slippage), Δ saída (fecho ponderado das pernas vs o nível da
  razão TP_k/SL), atraso sinal→abertura (o sim assume 60s) e P&L de cada
  lado. Tabela "⚡ Real vs backtest" na tab Relatório (vazia até a demo
  fechar trades).
- **Auto paper vs Auto MT5 (Fase 3 — ESTRATEGIAS.md Parte X.1)**: `GET /api/analysis/paper_vs_real_auto`
  (o painel Fase 3 usa o **payload único** `GET /api/analysis/fase3_status` —
  ETA+curva, snapshot, veredictos+histórico, `fase3_target_log` (reduções
  de alvo com motivo), divergences, real vs backtest, DD e auditoria SL num
  só pedido em vez de 4 chamadas)
- **`fase3_target_log`**: cada alteração ao alvo da janela (`fase3_target`,
  default 20; o feed baixa para 15 quando o ETA estimar > 6 semanas)
  regista `{at, from, to, reason}` — `POST /api/config` aceita
  `fase3_target_reason`; bloco "🎯 Alterações de alvo" no histórico do
  painel Fase 3.
  compara cada trade REAL fechada de uma auto (quando promovida a principal
  na Fase 3) com a trade de PAPEL da mesma estratégia no mesmo sinal —
  mesma decisão de saída, Δ preço de fecho (pips), Δ timing (min) e P&L de
  cada lado. Tabela "🔄 Auto paper vs Auto MT5 (Fase 3)" na tab Relatório
  (vazia até a Fase 3 arrancar).

## Autenticação da dashboard (login)

A dashboard/API estão **protegidas por password** desde que `SIGNALBOT_DASHBOARD_PASSWORD`
esteja no `.env` (está: **utilizador `admin` · password no `.env`**).

- **Sem password** no `.env` → tudo aberto (uso local, testes).
- **Com password** → `GET /` serve a página de login; `POST /api/login` {password}
  devolve um cookie httpOnly de sessão (7 dias); `/api/logout` termina.
- **Toda a API e o WebSocket `/ws`** exigem sessão (401/4401 sem cookie).
- **Listener do Telegram**: autentica com header `X-API-Token` =
  `SIGNALBOT_API_TOKEN` (no `.env`, gerado) — sem browser/cookie. Se mudares
  a password da dashboard, o listener continua a funcionar (usa o token).
- `app/auth.py` tem as sessões em memória (single-process) e lê as env vars
  dinamicamente — os testes forçam auth OFF (`SIGNALBOT_DASHBOARD_PASSWORD=""`).

**Expor na web (VPS)**: a dashboard não serve HTTPS. Na VPS põe um reverse
proxy (Caddy é o mais simples — HTTPS automático com Let's Encrypt):
`dominio.com { reverse_proxy 127.0.0.1:8000 }`. Abre só a porta 443 (e 22/RDP
para ti) na firewall do Windows; **nunca** exponhas a porta 8000 nem o RDP
ao público. Sem domínio comprado, dá para usar **duckdns.org** (subdomínio
grátis — estado atual: `n3xg3n.duckdns.org` → `103.241.67.222`, Caddy em
`C:\caddy` com regras de firewall `NexGen HTTP 80`/`NexGen HTTPS 443`;
reiniciar o Caddy após reboot: `cd C:\caddy && .\caddy.exe start`).

## Alertas / notificações

- `_check_group_divergence()` em `state.py`: quando chega um resumo novo do
  grupo, compara o WR reportado com o WR do backtest do bot; se a diferença
  ≥ 10pp, envia uma notificação `risk` na dashboard.
- `_check_daily_summary()`: resumo diário à hora `summary_time` — inclui o
  bloco 🏁 da corrida da Fase 2 (`_phase2_race_line`): saldo e PF da
  `auto_escada_3` vs `nova_gestao` e quantos sinais faltam para os critérios
  do ESTRATEGIAS.md Parte VIII (N ≥ 20 · PF ≥ 1.5).
- Auto kill switch: perda diária > limite → pausa tudo (e, em papel,
  bloqueia a abertura de novas trades virtuais no dia).

## Observabilidade operacional e CI (Itens 10 e 17)

- `GET /api/operations?days=N` agrega as métricas persistidas dos últimos
  `N` dias: exceções com stack trace/contexto, spread MT5 médio por símbolo e
  mês, eventos de ligação/reconexão por semana e tentativas de modify de SL
  aceites/recusadas por retcode. O mesmo payload aparece em
  `/api/diagnostics` e no painel "Saúde operacional" da tab Relatório.
- As tabelas SQLite são `operational_exceptions`, `spread_observations`,
  `mt5_connection_events` e `mt5_modify_events`. Um resultado vazio significa
  que ainda não houve observação, não que a taxa de falha seja zero; não se
  interpreta tendência antes de acumular pelo menos duas semanas.
- O `state.tick` amostra quotes MT5 no máximo uma vez por minuto; o loop de
  mercado/watchdog persiste exceções que escapem ao tick, reconexão ou
  watchdog, e o MT5 regista retcodes de modifies antes de alterar o SL na BD.
- `.github/workflows/ci.yml` corre `pytest -q` em cada push/PR, depois valida
  `tests/test_docs.py`. O dispatch manual `failure_probe=true` cria um teste
  deliberadamente falhado e confirma que o comando pytest devolve código
  diferente de zero, sem tornar o job normal verde artificialmente.

## Manter a documentação atualizada (obrigatório)

**Toda a alteração/implementação que toque no comportamento do programa tem
que atualizar a documentação no MESMO turno — nunca concluir uma tarefa com
docs desatualizadas.** A documentação é o mapa que permite manter este
projeto vivo durante semanas e migrar para a VPS/conta real sem quebras.

- **AGENTS.md** — fluxos, processos em execução, arquitetura, reconhecimento
  de mensagens, autenticação, endpoints, variáveis de ambiente, número de
  testes da suite. Qualquer mudança de comportamento → atualizar aqui.
- **docs/TECHNICAL.md** — detalhe técnico: endpoints, BD, engine, backtest,
  convenções. Novas funcionalidades e mudanças de API → atualizar aqui.
- **docs/ESTRATEGIAS.md** — guia dedicado das estratégias atuais (registo,
  gestão do grupo, auto-gestão, resultados, onde aparecem). Mudanças ao
  registo/resultados → atualizar aqui.
- **docs/PUBLIC.md** — visão para não-programadores (utilizador final).
  Funcionalidades novas visíveis na dashboard → atualizar aqui.
- **docs/DEPLOY.md** — guia de deploy na VPS Windows (Python + MT5 + Caddy
  HTTPS + firewall). Mudanças ao arranque/deploy → atualizar aqui.
- **docs/CHANGELOG.md** — registo cronológico das alterações. **Toda** a
  alteração acrescenta uma entrada (data + o quê + nº de testes).
- **Endpoints/env vars novos** → registar sempre em AGENTS.md + TECHNICAL.md.
- **Número de testes**: quando a suite crescer/mudar, corrigir o número em
  AGENTS.md (e em qualquer doc que o cite). `tests/test_docs.py` falha se
  o AGENTS.md não mencionar o número real da suite.
- Checklist de fim de tarefa (ver regras de ouro): processos vivos + suite
  verde + BD limpa + **docs atualizadas**.

## Regras de ouro para o agente

1. **Reiniciar o servidor depois de mexer em Python** e confirmar com
   `curl /api/status`.
2. **Correr `pytest` antes de concluir** — a suite completa (308 testes) é a
   rede de segurança (parser, classificador, backtest, API, auth, observabilidade).
3. **Não criar trades de teste sem limpar** os artefactos depois (trades,
   posições, sinais de teste na BD).
4. **Não guardar senhas em ficheiros de código** — vêm do `.env`/env vars.
5. **Nunca tocar na conta real** (a demo usa `MT5_LOGIN=5054644473` /
   `MetaQuotes-Demo`).
6. Após terminar uma tarefa: verificar processos vivos (server + listener),
   suite verde, estado da BD limpo e **documentação atualizada** (secção
   acima).
