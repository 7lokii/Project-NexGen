---
trigger: model_decision
description: "Como correr, reiniciar e vigiar o servidor do Project NexGen: processos e PIDs, logs rotativos, health check, restart no Windows, a tab de diagnostico, backup e restauracao da BD, e as regras de seguranca da VPS. Ler antes de arrancar, parar ou reiniciar qualquer processo, mexer em logs, em healthcheck, no Caddy, no firewall, no AutoTrading do MT5, ou expor a porta 8000."
---

## Processos em execução (verificar estado primeiro)

```
cat server.pid    # PID do uvicorn (porta 8000)
cat listener.pid  # PID do listener do Telegram
cat lane2_worker.pid  # PID do worker da 2ª conta MT5 (porta 8770, opcional)
tail -20 server.log     # log da API (rotação automática)
tail -20 listener.log   # log do listener (rotação automática)
tail -20 healthcheck.log  # log do health check (Tarefa NexGen Health, 2 min)
tail -20 lane2_worker.log  # log do worker da lane 2 (rotação automática)
```

- **Servidor (uvicorn):** `MT5_DRY_RUN=0 PYTHONIOENCODING=utf-8 nohup .venv/Scripts/python -u -m app.server_runner --host 127.0.0.1 --port 8000 >/dev/null 2>&1 &`
  - `app.server_runner` abre `server.log`/`server.err.log` com rotação por tamanho
    (`SIGNALBOT_LOG_MAX_BYTES`, default 10 MiB; `SIGNALBOT_LOG_BACKUP_COUNT`,
    default 5). Não redireciones o processo para `server.log`: isso mantém um
    handle externo aberto e impede a rotação no Windows.
  - O `main.py` carrega o `.env` (`load_dotenv`, sem override) - `EXECUTION_MODE`,
    `MT5_LOGIN`, `MT5_SERVER`, `MT5_PASSWORD` e `SIGNALBOT_API_TOKEN` vêm do `.env`.
    `MT5_DRY_RUN=0` no arranque sobrepõe o `MT5_DRY_RUN=1` do `.env` (execução
    real na demo); sem isso o servidor fica em dry-run.
  - **Reiniciar depois de qualquer alteração** ao código Python - os ficheiros
    não são recarregados em produção (sem `--reload`).
  - ⚠️ **No Windows, `kill` (Git Bash) não mata o processo uvicorn** - usa
    `taskkill //PID <pid> //F` (ou `taskkill /PID` via cmd). Confirma sempre
    com `netstat -ano | grep :8000` que a porta ficou livre antes de
    arrancar de novo; o novo processo só arranca se a porta estiver livre.
    O `server.pid` pode apontar para o subshell (`$!`) - o PID real do
    uvicorn é o que está a LISTEN na porta 8000.
  - **`start.ps1` é idempotente** (PowerShell): deteta se o servidor (porta
    8000) e/ou o listener já estão no ar e NÃO lança duplicados - com o
    sistema já ligado só reporta e sai. Usa `return` (não `exit`) para poder
    ser chamado a partir do `boot.ps1`. Para reiniciar de facto:
    `taskkill //IM python.exe //F` e volta a correr.
  - **Timestamps**: todos os timestamps do servidor/listener são **UTC
    explícito** (`+00:00`) - o browser converte para a hora local do
    utilizador. Se algum timestamp ficar naive (sem fuso), a contagem da
    janela de edição na dashboard aparece inflada ("3000s" em vez de 20s).
  - **Reinício automático (reboot da VPS)**: `boot.ps1` (raiz do projeto)
    arranca o Caddy (`C:\caddy\caddy.exe run`), o **MetaTrader 5**
    (`terminal64.exe`, idempotente) e depois o `start.ps1` (servidor +
    listener), todos idempotentes. A Tarefa Agendada **`NexGen Boot`**
    (AtLogOn, RunLevel Highest, sessão interativa) corre-o ao iniciar
    sessão. O servidor **religa-se ao MT5 automaticamente** (retry no
    arranque + reconexão com throttle de 10s no `market_loop`), e o bot
    autentica-se programaticamente via `MT5_LOGIN/SERVER/PASSWORD` do
    `.env` - o terminal só precisa de estar a correr com o AutoTrading ON.
    Para o `boot.ps1` disparar sozinho é preciso o **auto-login do Windows**
    (registo `Winlogon`); sem ele a VPS fica no ecrã de login e nada arranca.
  - **Backup diário da BD**: `backup_db.py` copia `signalbot.db` (API backup
    do sqlite3, consistente) para `backups/signalbot-<timestamp>.db`, roda a
    rotação (últimas 10) e envia para o Telegram via `sendDocument` quando
    `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` estão preenchidos (hoje estão
    VAZIOS - backup só local até os preencher). Tarefa **`NexGen DB Backup`**
    diária às 06:30. O teste mensal de restauração é
    `.venv/Scripts/python backup_db.py --restore-check`: restaura para uma BD
    temporária, corre `PRAGMA integrity_check` e valida as tabelas/contagens;
    uma falha é registada e enviada como alerta Telegram quando configurado.
  - **Health check do servidor**: `healthcheck.py` verifica `GET /` na porta
    8000 (alvo rápido; o `/api/status` é pesado e pode demorar >5s) com
    timeout de 15s e **2 falhas consecutivas** antes de agir - um servidor
    saudável mas lento nunca é morto por engano. Quando offline: regista em
    `healthcheck.log` (rotativo), envia alerta Telegram (se configurado) e
    **reinicia o servidor** com o comando documentado (`server_runner`,
    `MT5_DRY_RUN=0`), terminando antes um processo encravado que segure a
    porta. Cooldown de 120s entre restarts (estado em `healthcheck.state`).
    **Grace de boot**: um processo que segura a porta mas tem <5 min
    (`HEALTHCHECK_BOOT_GRACE`) é tratado como "a arrancar" - nunca é morto
    (o lifespan liga o MT5 e a porta já escuta antes de o HTTP servir;
    matá-lo a meio do boot fazia o restart entrar em guerra com o próprio
    arranque e partia o login/dashboard durante minutos). O `/api/status`
    serve o snapshot do market_loop (~1×/s, `last_snapshot`) e o
    Diagnóstico corre o worker/subprocessos fora do loop (`to_thread`) -
    nenhum GET da dashboard bloqueia o event loop único por request.
    **Vigia de stalls do event loop**: uma task do lifespan (`app/stallwatch.py`)
    acorda a cada 0.5s e mede o atraso real do wakeup (`loop.time()`); um
    congelamento ≥ `LOOP_STALL_THRESHOLD_SECONDS` (2s) é gravado em
    `loop_stall_events` (kind `stall`) com as **fases on-loop instrumentadas**
    ao lado (`phase`: `tick.update:<lane>`/`snapshot.build`/
    `diagnostics.on_loop` quando ≥ limiar - a causa provável). O bloco
    "⏱️ Loop stalls" do Diagnóstico mostra os últimos + total 24h -    um "sem resposta HTTP" do healthcheck passa a ser atribuível com dados.
    Retenção bounded (14 dias, purge no watchdog ~6h + arranque) e
    rate-limit por tipo (`LOOP_STALL_MIN_GAP_SECONDS`, 30s) para um padrão
    crónico não encher a BD. **As causas provadas foram eliminadas**: o
    snapshot do tick deixa de re-ler o broker por request (cacheado por
    request + TTL nos blocos informativos - `paper`/`shadow`/`fase3`/`window`/
    `risk_preview`), o `RemoteProvider` da lane 2 cacheia conta/posições com
    TTL (cada HTTP ao worker custava 15–160ms e o snapshot fazia 10+ por
    tick), `_execution_state` reutiliza as lanes já construídas e o SQLite
    corre em **WAL** (em `journal_mode=delete` cada commit de um DB de
    600MB+ fsyncava o disco: 15–580ms por commit - `snapshot.build` 3.5–6.4s,
    "sem resposta HTTP", logins pendurados).
    Corre na Tarefa Agendada **`NexGen Health`** (a cada 2 min, `--once`);
    loop contínuo com `--watch`; só alerta com `--no-restart`.
  - **Operação**: o watchdog faz um check diário de disco (`SIGNALBOT_DISK_PATH`,
    `SIGNALBOT_DISK_FREE_GB`, default 10 GiB) e avisa no feed/Telegram antes de
    o volume encher. Para auditoria read-only das tabelas usa
    `.venv/Scripts/python maintenance.py --db-audit`; para uma verificação
    manual do volume usa `maintenance.py --disk-check`.
- Se o `/api/status` mostrar `mode: sim` no execution → o servidor não
  carregou o `.env` (ou o MT5 não está a correr) e o saldo mostrado é o de
  papel, não o do broker. Em `mt5-real`, `execution.hedged` tem de ser `true`;
  O saldo MT5 só muda com ordens broker: `MT5_DRY_RUN=1` pode atualizar trades locais/paper sem alterar a conta demo; confirmar sempre `mode`, `dry_run`, saldo/equity e tickets em `/api/status`, `/api/diagnostics` e na auditoria diária.
  uma conta netting aparece como `blocked_reason` e o servidor não envia ordens.
- **Listener (Telethon):** `PYTHONIOENCODING=utf-8 nohup .venv/Scripts/python -u -m app.telegram_listener >/dev/null 2>&1 &`
  - O listener abre e roda `listener.log`/`listener.err.log` sozinho; não uses
    redireção externa para permitir a rotação no Windows.
  - Ao arrancar, repõe o histórico do grupo (`historical=True`) - por omissão
    **todas** as mensagens desde o início; `SIGNALBOT_TG_HISTORICAL_LIMIT`
    limita a N (`0` = todas; alias antigo `SIGNALBOT_TG_HISTORY_LIMIT`).
    Já não são só as últimas ~1000/200 - as páginas de teste usam o
    histórico completo (days=0). No fim do replay, o listener conta as
    mensagens por ação (sinais/gestão/resumos/ignoradas) e avisa o servidor
    (`POST /api/listener/history_done`) - o feed mostra
    "📚 Replay do histórico concluído" com os totais.
  - Credenciais vêm do `.env` (`SIGNALBOT_TG_API_ID/HASH/PHONE/GROUP`).
- **Estado rápido:** `curl -s http://127.0.0.1:8000/api/status | python -m json.tool`
- **Auditoria operacional diária:** `GET /api/operations/daily-audit?day=YYYY-MM-DD` reúne numa resposta o estado do broker, trades/realized P&L da BD, ordens rejeitadas, sinais falhados/pendentes/expirados, mensagens ignoradas e kill diário. `GET /api/analysis/recent-strategies?since=YYYY-MM-DD` é um estudo read-only; não altera a estratégia persistida nem a execução.

## Verificação rápida de saúde

```
.venv/Scripts/python -m pytest -q          # 631 testes (atualizar quando a suite crescer)

curl -s http://127.0.0.1:8000/api/status   # mode, balance, open_trades
curl -s http://127.0.0.1:8000/api/diagnostics   # MT5, listener, uptime, BD
```

⚠️ **Fronteira de dia nos testes**: a VPS tem relógio local +1h (um dia à
frente do UTC perto da meia-noite). `realized_today`/`_realized_today` usam
`date.today()` LOCAL mas o `closed_at` de uma trade inserida com UTC fica no
dia anterior → o `LIKE '<dia-local>%'` não casa e o limite de perda diária
não conta a trade (flakes de perda diária/kill). Testes ao limite diário
`daily_loss_limit_pct` devem gravar o `closed_at` com o dia LOCAL
(`date.today().isoformat()`), não `datetime.now(timezone.utc)`.

- A tab **"🔧 Diagnóstico"** da dashboard mostra o estado ao vivo: MT5
  (ligação/conta/AutoTrading/**tipo hedged**), **listener do Telegram** (heartbeat a cada 1s;
  sem notícias há >5s = OFFLINE; última mensagem do grupo com idade; gráfico
  de uptime 24h), servidor (uptime/PID), market feed, auth e contagens da BD.
  `GET /api/diagnostics` + `POST /api/heartbeat` (enviado pelo listener com
  `{groups, last_msg_at, tick}`). A tab tem também um botão "🧹 Limpar
  reinícios" (`DELETE /api/listener_restarts`, com confirmação) para apagar
  TODO o histórico à mão - o purge automático de 30 dias continua ativo; e
  um bloco "⏱️ Atraso das edições do grupo" (`GET /api/edits`) que mostra a
  distribuição do atraso das edições (mín/média/mediana/p90/máx, a mediana
  verde/vermelha face à janela atual) - para validar os 20s com dados reais.
  A coluna `edited_at` em `signals` regista a hora de cada edição.
- **Auto-restart do listener**: o servidor tem um watchdog (loop a cada 10s
  em `main.py::listener_watchdog`) que reinicia o processo do listener
  quando o heartbeat parar. `_listener_running_anywhere` usa
  `Get-CimInstance` (NÃO `wmic`, removido em Windows recentes) para não
  spawnar duplicados - o `wmic` em falta era a causa raiz dos listeners
  duplicados. O mesmo loop faz **purge automático** dos
  registos de `listener_restarts` com mais de 30 dias (no arranque e a
  cada ~6h - a tabela não cresce sem limite). Se o Telegram pedir código de login novo, o
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
  mensagens reais - o replay do histórico no arranque NÃO conta (a
  dashboard mostra "sem mensagens ainda" até chegar a primeira do grupo).
- ⚠️ **Windows/Python 3.14**: cada processo python da venv aparece em PAR
  (launcher + interpretador real) - não são duplicados. Para matar só os
  listeners: `powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*telegram_listener*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"`.
  Para matar tudo: `taskkill //IM python.exe //F`. O `kill` do Git Bash não
  mata processos Windows.
- **Resultados de trades no WhatsApp (opcional)**: quando uma trade REAL da
  **lane 2** (MT5 demo) fecha, `state.py::_notify_whatsapp_trade_close`
  (no ramo `trade_closed` do loop do motor - a corrida paper tem o
  seu próprio loop e NUNCA dispara) publica no grupo o resumo + **P&L por perna** + link da dashboard.
  **UMA mensagem por sinal, só da tp2 (16/09)**: fechos da lane main não
  enviam (antes, cada lane fechava em ticks diferentes e o grupo recebia
  DUAS mensagens do mesmo sinal). O hook filtra `lane_id == "tp2"` e usa o
  formato single-lane `format_trade_close` com o rótulo da conta tp2.
  O transporte é o sidecar Node/Baileys `whatsapp/bridge.js` (sessão
  WhatsApp Web pareada 1× por QR, persistida em `whatsapp/session/`, HTTP
  só em `127.0.0.1:8765` - `/send`, `/status`, `/qr`, `/groups`); o
  `start.ps1` arranca-o de forma idempotente (check da porta 8765). O módulo
  Python `app/notify/whatsapp.py` só formata (`format_trade_close`) e faz o
  POST - falhas são registadas na BD e nunca quebram o tick. Config no
  `.env`: `WHATSAPP_ENABLED`/`WHATSAPP_GROUP_JID` (obrigatórios),
  `WHATSAPP_BRIDGE_URL`/`DASHBOARD_URL` (defaults). O Diagnóstico tem o
  bloco "💬 WhatsApp" (estado + QR + grupos para copiar o JID). ⚠️ Sessão
  não-oficial: a conta que pareia o QR pode ser banida. Reiniciar o servidor
  depois de qualquer alteração.
- Se o servidor responder mas `/api/status` devolver dados antigos/errados →
  **reiniciar o servidor** (código antigo em memória).
- Se a dashboard mostrar "simulação" no badge de execução → o servidor pode
  ter caído para paper (`EXECUTION_MODE` não setado) ou o MT5 desligou.
- **Nunca** correr `git push` / commits sem o utilizador pedir.

## Operação e segurança da VPS

- **Conta MT5**: a conta demo principal está atualmente em execução broker (`MT5_DRY_RUN=0`) com `tp2_protect`; a estratégia N-pernas exige `margin_mode=2` (`RETAIL_HEDGING`). A troca para `tp2_protect` foi uma alteração de configuração separada da saída de dry-run: mudar a estratégia não envia ordens, mas reiniciar após mudar `MT5_DRY_RUN` é obrigatório para o processo carregar o modo novo.
  Em `MT5_DRY_RUN=0`, `MT5Provider.initialize()` falha fechado se o modo for
  netting/desconhecido, fecha a ligação e expõe `execution.blocked_reason`;
  nenhuma ordem é enviada. `GET /api/diagnostics` mostra `margin_mode`,
  `hedged` e o motivo do bloqueio. A mesma tab expõe `reconciliation` read-only
  com legs em falta, posições desconhecidas e SL divergente entre DB e broker;
  uma divergência não é auto-corrigida nem fecha posições.
- **Logs**: o entry point `app.server_runner` e o listener usam streams UTF-8
  rotativos. O limite é configurável por `SIGNALBOT_LOG_MAX_BYTES` (default
  10 MiB) e `SIGNALBOT_LOG_BACKUP_COUNT` (default 5), incluindo os ficheiros
  `.err.log`; `backup.log` usa a mesma política. A manutenção não apaga o
  histórico analítico de sinais/trades/positions/paper.
- **Disco/BD**: o watchdog verifica diariamente o volume configurado e o
  snapshot expõe `operations.disk`; `maintenance.py --db-audit` mostra linhas,
  meses observados e política por tabela. `backup_db.py --restore-check` usa
  uma BD temporária e nunca altera a BD de produção.
- **Auditoria diária**: a contagem de trades da BD não é uma reconciliação automática do broker; dry-run pode produzir P&L local sem efeito no saldo MT5 e um journal com quotes em excesso pode não conter fills úteis. Tratar divergências como alertas para investigação, nunca como autorização de correção automática ou de novas ordens.
- **Dimensionamento em contas pequenas**: com saldo perto de €1000, o lote mínimo `0.01` pode dominar o arredondamento de XAUUSD; aumentar o risco configurado não implica necessariamente aumentar o lote e pode quebrar o limite de risco. Validar lote, distância ao SL, margem e risco monetário antes de alterar risco.
- **Journal de execução opt-in**: `execution_journal_enabled=false` por defeito.
  Quando ligado, `state.tick` guarda quotes e mensagens de gestão em
  `execution_events`; `GET /api/operations/events` expõe os eventos em modo
  read-only para replay determinístico. O journal não abre/fecha trades nem
  muda o intervalo de produção e deve ser desligado/rotacionado depois de uma
  recolha de evidência. **Bounded**: a retenção é configurável por
  `EXECUTION_JOURNAL_RETENTION_DAYS` (default 30) com purge no arranque e
  no watchdog (~6h) - a tabela nunca cresce sem limite; e o agregado
  `execution_event_stats(7)` (GROUP BY ~1s sobre milhões de linhas) corre
  com cache de 60s (a agregação por tick era uma das causas dos stalls do
  loop). ⚠️ **SQLite em WAL** (`journal_mode=wal` + `synchronous=normal`):
  em `journal_mode=delete` cada commit de um ficheiro de 647MB+ fsyncava o
  disco (15–580ms por commit - os inserts de quotes do tick congelavam o
  event loop e o healthcheck matava o servidor); WAL deixa os commits ~0ms.
  `POST /api/operations/replay` recebe uma fita JSON
  (`signal`, `quote`, `management`) e corre-a em SQLite temporária contra
  SimEngine, PaperRace e MT5 dry-run; devolve decisões, SL, razões e P&L sem
  tocar na BD de produção, no MT5 ou no intervalo de 1s.
