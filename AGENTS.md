# AGENTS.md — Project NexGen (Freebuff)

Guia para agentes (Codebuff/Freebuff) que trabalham neste projeto. Lê isto
antes de mexer no código.

## Replay de evidência operacional

Quando `execution_journal_enabled=true`, o servidor guarda quotes, gestão e
evidência do MT5 na `tabela execution_events`. Além de `signal`/`quote`/
`management`, a fita pode conter `order_request`, `order_result`,
`modify_request`, `modify_result`, `fill_open` e `fill_close`, com retcodes,
preços, tickets e latência sinal→ordem→fill. A fita completa pode ser exportada
sem efeitos laterais por `GET /api/operations/replay/tape?signal_id=<id>` (ou
`trade_id`), que junta os eventos aos campos estruturados de `signals`; o
relatório `POST /api/operations/replay` corre em BDs temporárias e MT5 dry-run.
Os eventos do broker são evidência passiva no replay: nunca autorizam ordens nem
alteram as decisões simuladas. `GET /api/operations/replay/snapshots?...&step=N`
permite inspeccionar o estado após cada evento. `GET
/api/operations/journal-quality` devolve a qualidade diária UTC do journal
(sinais, gestão, fills, retcodes, latências e trades sem fita completa).
`GET /api/analysis/fill-audit` compara fills MT5 com o replay isolado e expõe a
primeira divergência por trade. `GET /api/analysis/forward-gate` avalia
`auto_escada_3` e `auto_escada_5` apenas no `origin=shadow`, numa janela UTC
de 30 dias congelada por `forward_evidence_started_at`, com N≥20, PF≥1.5,
IC95% positivo, DD≤125% e comparação com a `nova_gestao`; o resultado é
informativo e nunca promove automaticamente.
A página `/auditoria` junta estes três relatórios. Também permite descarregar o
bundle diário em JSON/CSV (`GET /api/analysis/audit-export`) e arquivar
explicitamente uma janela forward (`POST /api/analysis/forward-gate/archive`;
listar em `GET /api/analysis/forward-gate/archives`). O resumo diário inclui uma
linha de qualidade do journal e o progresso de ambas as auto-escadas no feed e,
quando configurado, no Telegram — não altera execução. A promoção de
`auto_escada_3`/`auto_escada_5` fica bloqueada na UI e no backend até todos os
gates objetivos passarem. Isto é read-only para a execução: não usar o replay
para ligar a conta real.

## O que é o projeto

**Project NexGen** — sistema de copy-trading semi-automático em Python (FastAPI +
vanilla JS). Lê sinais do grupo de Telegram **Golden Moves**, espera a janela de edição (20s), executa a estratégia de **N pernas (TP1..TP5 + breakeven)**
no **MetaTrader 5** (conta **demo**, não é dinheiro real) e mostra tudo numa
dashboard web em `http://127.0.0.1:8000`.

Estado atual (2026-09-01): servidor em **MT5-real na conta demo** (`EXECUTION_MODE=mt5`,
`MT5_DRY_RUN=0`), modo **auto**, estratégia principal persistida
`tp2_protect` (2 pernas, saída no TP2) (TP1..TP5 + gestão do grupo), listener do Telegram a escutar o
Golden Moves. A **segunda conta demo MT5 (5055369549, saldo 10.000 €)** está
ligada como lane
`tp2` (env `MT5_2_*`) e **passou a execução real em 01/09** com a
`nova_gestao`; a **02/09 a lane passou para a conta 5055369549 (10k) com a
mesma estratégia da principal — `tp2_protect`** (config persistida
`lane2_strategy`), terminal **portable** em
`C:\MT5Portable2\terminal64.exe` (`MT5_2_TERMINAL_PATH`), `LANE2_WORKER_ENABLED=1`
e `MT5_2_DRY_RUN=0` no `.env` — o **worker isolado** (`app/lane2_worker.py`,
porta 8770, PID em `lane2_worker.pid`) é o dono da ligação MetaTrader5 à
5055369549 e o servidor fala-lhe via `RemoteProvider` (ver secção
Lanes/arquitetura). A conta principal **5054644473** continua no seu terminal
(Program Files) — os dois terminais/processos NUNCA partilham ligação.
✅ O **AutoTrading do terminal 2 foi LIGADO a 01/09** (clique automatizado
no botão "Algo Trading" da toolbar Standard do terminal portable — o último
botão à direita; validado por `terminal_info().trade_allowed` através do
worker). ⚠️ É **por-sessão** — reativar depois de cada reboot, como já
acontece no terminal 1. Enquanto estiver OFF, as ordens da lane 2 são
recusadas com retcode 10027 + alerta. O **Diagnóstico expõe o estado**
(`lane2_worker.trade_allowed` — ON/OFF no bloco "Lane 2") e o market_loop
emite alerta `risk` automático quando o worker está ligado mas o AutoTrading
está OFF (cooldown 10 min; evento `lane2_auto_trading_off`). **Watchdogs do
símbolo e do retcode null (02/09)**: o market_loop verifica a cada 30s que
XAUUSD tem quote no worker da lane 2 — sem tick, chama `ensure_symbol`
(`symbol_select`) para o re-adicionar ao Market Watch e emite alerta `risk`
(episódio com cooldown 10 min + notificação de recuperação; eventos
`lane2_symbol_reselect`/`lane2_symbol_recovered`); e um **retcode `null`**
(broker respondeu SEM retcode — mapeado para `?`, ex.: 14:52 de 02/09) numa
ordem da lane 2 dispara alerta `risk` distinto com o estado do worker
(`reachable`/`connected`/`trade_allowed`/login/último erro) — no `_try_execute`
para aberturas e no scan do tick (`_check_null_retcode_phantoms`, a cada 30s,
últimos 30 min, uma vez por trade) para fechos phantom fora do caminho de
abertura.
O kill switch foi **desligado a 20/08 por decisão explícita do
utilizador** (o rearm agendado de 29/08 foi limpo) — a principal `nova_gestao`
passa a abrir ordens MT5 demo na janela de 28/08 e a referência do gate
(referência híbrida race+shadow da `nova_gestao`, limiar `min_reference_trades=10`)
começa a acumular; o limite diário (3%) e o auto-kill continuam ativos. O
risco base está em
1,0% e o high-watermark foi recalibrado para 1.000 €; o risco efetivo começa
assim em 1,0%, sem herdar o pico da conta paper antiga. O **shadow-paper** está
ativo por defeito para `auto_escada_3`, `auto_escada_5`, `auto_escada_3_hibrida`
e `nova_gestao` (a última alimenta a referência híbrida do gate): recebe sinais live
completos mesmo com o kill switch ligado (o modo manual foi removido no Item
12) e usa apenas as tabelas paper, nunca o motor MT5. As pendências de gestão têm marcadores separados para real, corrida
paper e shadow, para uma entrega isolada não consumir a outra. O objetivo do
utilizador é correr isto durante dias/semanas, afinar o reconhecimento de
mensagens e **mais tarde** trocar para uma conta real.

> ⚠️ A conta é **demo** — não há dinheiro real em jogo. Nunca mudar para uma
> conta real sem instrução explícita do utilizador.

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
    janela de edição na dashboard aparece inflada ("3000s" em vez de 20s).
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
  - **Health check do servidor**: `healthcheck.py` verifica `GET /` na porta
    8000 (alvo rápido; o `/api/status` é pesado e pode demorar >5s) com
    timeout de 15s e **2 falhas consecutivas** antes de agir — um servidor
    saudável mas lento nunca é morto por engano. Quando offline: regista em
    `healthcheck.log` (rotativo), envia alerta Telegram (se configurado) e
    **reinicia o servidor** com o comando documentado (`server_runner`,
    `MT5_DRY_RUN=0`), terminando antes um processo encravado que segure a
    porta. Cooldown de 120s entre restarts (estado em `healthcheck.state`).
    **Grace de boot**: um processo que segura a porta mas tem <5 min
    (`HEALTHCHECK_BOOT_GRACE`) é tratado como "a arrancar" — nunca é morto
    (o lifespan liga o MT5 e a porta já escuta antes de o HTTP servir;
    matá-lo a meio do boot fazia o restart entrar em guerra com o próprio
    arranque e partia o login/dashboard durante minutos). O `/api/status`
    serve o snapshot do market_loop (~1×/s, `last_snapshot`) e o
    Diagnóstico corre o worker/subprocessos fora do loop (`to_thread`) —
    nenhum GET da dashboard bloqueia o event loop único por request.
    **Vigia de stalls do event loop**: uma task do lifespan (`app/stallwatch.py`)
    acorda a cada 0.5s e mede o atraso real do wakeup (`loop.time()`); um
    congelamento ≥ `LOOP_STALL_THRESHOLD_SECONDS` (2s) é gravado em
    `loop_stall_events` (kind `stall`) com as **fases on-loop instrumentadas**
    ao lado (`phase`: `tick.update:<lane>`/`snapshot.build`/
    `diagnostics.on_loop` quando ≥ limiar — a causa provável). O bloco
    "⏱️ Loop stalls" do Diagnóstico mostra os últimos + total 24h —    um "sem resposta HTTP" do healthcheck passa a ser atribuível com dados.
    Retenção bounded (14 dias, purge no watchdog ~6h + arranque) e
    rate-limit por tipo (`LOOP_STALL_MIN_GAP_SECONDS`, 30s) para um padrão
    crónico não encher a BD. **As causas provadas foram eliminadas**: o
    snapshot do tick deixa de re-ler o broker por request (cacheado por
    request + TTL nos blocos informativos — `paper`/`shadow`/`fase3`/`window`/
    `risk_preview`), o `RemoteProvider` da lane 2 cacheia conta/posições com
    TTL (cada HTTP ao worker custava 15–160ms e o snapshot fazia 10+ por
    tick), `_execution_state` reutiliza as lanes já construídas e o SQLite
    corre em **WAL** (em `journal_mode=delete` cada commit de um DB de
    600MB+ fsyncava o disco: 15–580ms por commit — `snapshot.build` 3.5–6.4s,
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
  TODO o histórico à mão — o purge automático de 30 dias continua ativo; e
  um bloco "⏱️ Atraso das edições do grupo" (`GET /api/edits`) que mostra a
  distribuição do atraso das edições (mín/média/mediana/p90/máx, a mediana
  verde/vermelha face à janela atual) — para validar os 20s com dados reais.
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
- **Resultados de trades no WhatsApp (opcional)**: quando uma trade REAL da
  **lane 2** (MT5 demo) fecha, `state.py::_notify_whatsapp_trade_close`
  (no ramo `trade_closed` do loop do motor — a corrida paper tem o
  seu próprio loop e NUNCA dispara) publica no grupo o resumo + **P&L por perna** + link da dashboard.
  **UMA mensagem por sinal, só da tp2 (16/09)**: fechos da lane main não
  enviam (antes, cada lane fechava em ticks diferentes e o grupo recebia
  DUAS mensagens do mesmo sinal). O hook filtra `lane_id == "tp2"` e usa o
  formato single-lane `format_trade_close` com o rótulo da conta tp2.
  O transporte é o sidecar Node/Baileys `whatsapp/bridge.js` (sessão
  WhatsApp Web pareada 1× por QR, persistida em `whatsapp/session/`, HTTP
  só em `127.0.0.1:8765` — `/send`, `/status`, `/qr`, `/groups`); o
  `start.ps1` arranca-o de forma idempotente (check da porta 8765). O módulo
  Python `app/notify/whatsapp.py` só formata (`format_trade_close`) e faz o
  POST — falhas são registadas na BD e nunca quebram o tick. Config no
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
  no watchdog (~6h) — a tabela nunca cresce sem limite; e o agregado
  `execution_event_stats(7)` (GROUP BY ~1s sobre milhões de linhas) corre
  com cache de 60s (a agregação por tick era uma das causas dos stalls do
  loop). ⚠️ **SQLite em WAL** (`journal_mode=wal` + `synchronous=normal`):
  em `journal_mode=delete` cada commit de um ficheiro de 647MB+ fsyncava o
  disco (15–580ms por commit — os inserts de quotes do tick congelavam o
  event loop e o healthcheck matava o servidor); WAL deixa os commits ~0ms.
  `POST /api/operations/replay` recebe uma fita JSON
  (`signal`, `quote`, `management`) e corre-a em SQLite temporária contra
  SimEngine, PaperRace e MT5 dry-run; devolve decisões, SL, razões e P&L sem
  tocar na BD de produção, no MT5 ou no intervalo de 1s.

## Arquitetura (ficheiros que importam)

| Ficheiro | Papel |
|---|---|
| `app/main.py` | FastAPI: endpoints, wiring do motor (sim vs mt5), lifespan |
| `app/background.py` | Runtime lifecycle-owned dos loops de mercado/watchdog; cancela as tasks no shutdown |
| `app/execution/parity.py` | Vocabulário comum de observação para comparar decisão, SL, razão e P&L dos motores |
| `app/execution/clock.py` | Relógio UTC/monotónico injetável; `ManualClock` torna time stops e polling determinísticos nos estudos |
| `app/execution/journal.py` | Eventos de execução imutáveis + replay read-only ordenado para estudos/reprodução |
| `app/execution/evidence.py` | Intervalos bootstrap determinísticos e gate descritivo de evidência forward (nunca promove sozinho) |
| `app/state.py` | `BotState`: pipeline de sinais, gestão do grupo, edge, resumo diário |
| `app/telegram_listener.py` | Telethon: lê o grupo e faz POST a `/api/signals` |
| `app/lane2_worker.py` | Worker da 2ª conta MT5 (processo isolado, shim de I/O por HTTP local) |
| `app/execution/remote2.py` | `RemoteProvider` + proxy do módulo MT5 (`provider._mt5`) que fala com o worker |
| `app/signals/parser.py` | Parse do sinal (entrada/SL/TP1..TP5) + `validate_signal()` |
| `app/messages/manage.py` | **Classificador das mensagens de gestão** (ver abaixo) |
| `app/messages/summaries.py` | Parse dos resumos diários/semanais (ground truth) |
| `app/signals/llm_fallback.py` | Parser LLM opcional (fallback, nunca executa em auto) |
| `app/execution/engine.py` | SimEngine (paper) — mesma interface do MT5Engine |
| `app/execution/paper.py` | PaperRace — contas virtuais, corrida normal e shadow-paper isolado |
| `app/execution/mt5.py` | MT5Engine — ordens reais/dry-run no MetaTrader 5; bloqueia conta real não-hedged |
| `app/maintenance.py` | Rotação bounded de logs, check de disco, auditoria DB e teste de restauração |
| `app/server_runner.py` | Entry point Uvicorn que instala os logs rotativos antes do servidor |
| `maintenance.py` | CLI read-only para auditoria de tabelas e espaço em disco |
| `app/execution/protection.py` | Núcleo puro/reducer de decisões de proteção comum aos motores (escada/trailing/ATR/confirm/time stop/auto-protect + merge do `protect`) |
| `app/backtest.py` | Replay de sinais reais em candles históricos (+ gestão) |
| `app/analysis.py` | Edge analysis, A/B/C compare, walk-forward |
| `app/db.py` | SQLite (sinais, trades, posições, summaries, config) |
| `app/static/style.css` + `common.js` | CSS partilhado + JS comum (helpers, boot, WS, chrome global) — carregados por todas as páginas |
| `app/static/index.html` | Dashboard principal (JS vanilla, sem framework) — tabs sinais/posições/histórico/relatório/ignoradas + feed + corrida paralela |
| `app/static/teste.html` | Página de teste de estratégias (`/teste?s=<id>`) — simulação, walk-forward, corrida paper |
| `app/static/diagnostico.html` | Página de diagnóstico (`/diagnostico`) — MT5, listener, BD |
| `app/static/help.html` | Página de ajuda (`/help`) — estratégias + glossário; `HELP_FLOW` e conceitos têm de acompanhar todas as estratégias visíveis |
| `tests/` | `test_api.py`, `test_backtest.py`, `test_management.py` |

**Modelo de concorrência:** todo o estado vive no event loop do FastAPI;
o SQLite tem um lock interno (`DB.tx`). Não há corridas.

## Lanes de execução — 2ª conta MT5 (worker isolado)

A conta principal (`MT5_LOGIN`, lane `main`) executa no processo do servidor.
A **2ª conta demo** (`MT5_2_LOGIN`, lane `tp2`) existe por omissão em
**dry-run** (`mt5-dry`: trades registadas na BD, nada enviado) e, quando
ativada, em **execução real num processo separado** — porque a API
MetaTrader5 mantém **UMA ligação global por processo** e a principal já ocupa
essa ligação.

- **Estado atual (02/09, ativado)**: a lane `tp2` (5055369549, demo 10k)
  corre `tp2_protect` — a mesma estratégia da principal, por decisão do
  utilizador para o A/B ser like-for-like — em **execução real** através do
  worker (terminal portable
  `C:\MT5Portable2`); a config `lane2_strategy`/`lane2_label` é definida por
  `POST /api/config` (a config persistida sobrepõe o env `MT5_2_STRATEGY`).
  O endpoint valida contra o registo e **bloqueia `auto_escada_3`/`auto_escada_5`
  na lane MT5** (gate da Fase 3: N≥20 · PF≥1.5 · DD≤125% antes de exposição
  MT5). O risco por trade da lane vem do `risk_pct:tp2` (base % por conta);
  para a lane abrir com **lote fixo por perna** (ex.: 0.04 em vez de variar
  com a distância ao SL) define-se `lots_override_tp2` via `POST
  /api/config` (persiste em `lots_override:tp2`; 0 = volta ao risco) — o
  risk_preview e o snapshot da lane expõem o fixo (`lots_override` na
  preview, `fixed_lots_per_leg` na entrada).
- **Worker (`app/lane2_worker.py`)**: processo isolado dono da ligação MT5 à
  2ª conta (terminal **portable**, `MT5_2_TERMINAL_PATH`) que expõe as
  operações por HTTP local (`/ping /status /initialize /account /positions
  /quote /meta /copy_rates /constants /symbol_select /mt5/call`) com token
  (`LANE2_WORKER_TOKEN`, fallback `SIGNALBOT_API_TOKEN`). O processo principal
  usa `RemoteProvider` (`app/execution/remote2.py`, mesma interface duck-typed
  do `MT5Provider`, incluindo `provider._mt5` que o `MT5Engine` usa) — **o
  worker nunca toca na SQLite nem decide nada**, preservando a invariante
  "todo o estado vive no event loop".
- **Fail-closed**: sem ligação (worker offline, terminal 2 ausente, conta não
  hedged, AutoTrading off) a lane 2 não envia ordens — `open_trade` real
  levanta erro. Um worker offline NUNCA partilha a ligação da principal.
- **Boot da lane tp2 (corrigido 16/09)**: o lifespan inicializava o
  `mt5_provider2` LOCAL (com bloqueio permanente por design quando
  `MT5_DRY_RUN=0`) em vez do `RemoteProvider` do worker — a init da tp2
  falhava em TODOS os arranques, mesmo com o worker saudável ("Execução MT5
  bloqueada na lane tp2 ... ligação global"). Agora `_lane2_provider()`
  devolve o MESMO provider que o reconnect usa. O `RemoteProvider.initialize`
  também já não polui `execution_block_reason` com o motivo TRANSITÓRIO de
  worker offline (fazia o reconnect saltar a lane para sempre).
  **Falha de boot ≠ bloqueio runtime no feed**: a falha transitória de
  arranque notifica `system` ("⏳ não ligou no arranque"); o bloqueio
  permanente (conta não hedged, config errada) notifica `risk` ("⛔"). O
  reconnect/retry emite a recuperação ("✅ religado").
  **Retry único em background**: as lanes que falharam no boot re-tentam UMA
  vez passado `MT5_BOOT_RETRY_DELAY` (default 30s, task do lifespan
  cancelada no shutdown) — cobre a corrida com o arranque do worker portable
  pelo boot.ps1; se falhar de novo, o reconnect do market_loop continua.
- **Supervisão**: `start.ps1`/`boot.ps1` arrancam o worker idempotentes por
  porta; `healthcheck.py` vigia `/ping` (mesmas 2 falhas + cooldown, chaves
  `worker_*` no `healthcheck.state`) e reinicia-o; o market_loop pinga o
  worker ~1×/s e notifica no feed/Telegram as transições offline/online
  (`lane2_worker_offline`/`online` em `execution_events`). O Diagnóstico tem
  o bloco "Lane 2 · worker MT5" (reachable/connected/login/last_sync/
  **AutoTrading ON/OFF**/último erro + cauda do log).
- **A/B das lanes (read-only)**: `GET /api/lanes/ab` compara as trades
  FECHADAS que existem nas duas lanes para o **mesmo sinal** (P&L lado a
  lado, win rate, PF, delta total) e **só sugere manutenção com ≥20 sinais
  comparáveis** (`min_comparable`, default 20; limiar mantido com a amostra
  real de 02/09: as 3 trades tp2 de 01/09 eram phantom, sem fills reais —
  com n=0 não há base para baixar o limiar) — abaixo disso o veredicto é
  neutro ("amostra insuficiente") e explica a amostra. **Fechos phantom
  (sem `exit_reason` — ordens recusadas pelo broker, ex.: AutoTrading OFF,
  fechadas a 0 no próprio tick) NUNCA entram no comparável**: ficam em
  `voids`/`void_count` (com as flags por lane) para os 0.0 não contarem
  como resultado de nenhuma estratégia — os 3 pares de 01/09 (AutoTrading
  OFF na lane 2 durante o dia) aparecem como "fechos phantom" no
  Diagnóstico, não como perda/ganho. Ao vivo, um fecho phantom dispara
  também um **alerta risk distinto** ("👻 … SEM fill real") e o motivo da
  recusa fica **gravado na trade** (`error`/`retcodes`/`auto_trading_off`,
  colunas migradas automaticamente) para a auditoria explicar o retcode.
  O bloco "⚖️ A/B lanes" do Diagnóstico **refresca com o feed (10s)** — a
  amostra atualiza sozinha, e o relatório junta o **dimensionamento de cada
  lane** (`main.sizing`/`tp2.sizing`: risco base, lote fixo por perna
  `lots_override:<lane>`, lots/perna/total do preview) para a comparação
  ler com os tamanhos de posição em contexto. Nunca altera estratégia/config
  nem envia ordens.
  **`app/backfill_trade_refusals.py`** re-corre o trace (error/retcodes/
  auto_trading_off) de fechos phantom sem motivo gravado: a evidência vem do
  journal quando existe; sem journal, das **notificações risk/system da hora
  e da auditoria de ignoradas** (um 10027 liga a flag AutoTrading OFF como o
  motor). O tick re-corre-o automaticamente 1×/semana (guard
  `refusals_backfill_week_last_run`) e notifica quando encontrou algo —
  nenhum phantom fica sem trace durável mesmo com o alerta perdido.
  **Delete de pendentes robusto**: o `order_delete` da expiração por TTL e do
  cancelamento tem tratamento por perna — falha (worker offline) mantém a
  perna `pending_fill`, grava a exceção e o próximo tick repete; após 5
  falhas seguidas o feed recebe o alerta risk `pending_delete_stuck` (uma
  vez por episódio; re-alerta a cada 30 min enquanto a perna continuar
  presa). O **retry é imediato**: a intenção do delete falhado (TTL ou
  cancelamento de gestão) fica guardada e o tick completa o cancelamento no
  próprio tick em que o worker voltar — sem esperar o TTL. O Diagnóstico
  expõe as pernas presas no bloco "⏳ Ordens pendentes em retry de delete"
  (tentativas/escalonada/ticket/intenção/último alerta) via `stuck_pending`
  em `/api/diagnostics` (read-only).
  **Estado do watchdog PERSISTIDO** (tabela `pending_delete_states`, por
  lane): tentativas, intenção do delete e timestamps de alerta sobrevivem a
  um restart do servidor — o Diagnóstico restaura o bloco e uma perna já
  escalonada volta a alertar no 1º retry pós-reboot (marca durável
  `escalated_at`); linhas órfãs (perna preenchida/fechada ou trade
  encerrada) são limpas automaticamente pelo tick. **Exceções
  rate-limited**: enquanto a perna presa falha a cada segundo, só a
  PRIMEIRA falha do episódio grava `operational_exceptions` (uma exceção
  concisa por perna, nunca centenas de linhas idênticas) — um episódio
  novo volta a gravar. **Evidência de replay**: cada marca
  `pending_delete_stuck` entra no journal (opt-in) com o contexto completo
  da perna (leg/ticket/position/lane/tentativas/intenção/razão/timestamps)
  — combinada com os `pending_order_result` falhados, a fita reproduz uma
  falha prolongada do worker de forma determinística. A **exportação da
  fita** (`GET /api/operations/replay/tape`) inclui estas marcas (passivas,
  nunca geram ordens no replay) e o relatório read-only
  `GET /api/operations/replay/outages?days=30` junta-as numa **linha
  temporal de worker outages** (timeline cronológica + episódios por
  trade/ticket: falhas, escaladas, re-alertas, intenção e resolução
  derivada do journal — delete aceite ou fill depois da 1ª falha) — bloco
  "⏳ Worker outages · delete de ordens pendentes" no /auditoria.
  **Retenção bounded de `operational_exceptions`**: além do rate-limit por
  episódio, o purge periódico do watchdog (~6h) e do arranque apaga linhas
  com mais de 365 dias (`db.purge_old_exceptions(365)`, política em
  maintenance.py) — a tabela nunca cresce sem limite.
- ⚠️ **GUARD CRÍTICO (incidente 01/09)**: sem `MT5_2_TERMINAL_PATH` o worker
  **recusa arrancar (exit 2)**. Sem uma instalação portable, `initialize(
  login=...)` liga ao **terminal por omissão** e troca a conta do terminal da
  principal (observado em produção: a conta passou a apontar para a 111495351
  até ser relogada). Ativar a lane 2 real exige, por ordem: instalar o 2º
  terminal MT5 **portable**, definir `MT5_2_TERMINAL_PATH` no `.env`,
  `LANE2_WORKER_ENABLED=1` (e `MT5_2_DRY_RUN=0` para execução real) e correr
  `start.ps1` — o `boot.ps1` já arranca o terminal portable no reboot.
- 🧪 **Marshaling do módulo MT5 POR CONVENÇÃO** (03/09): o worker recusa
  ANTES do C-core qualquer chamada remota com a convenção errada. A grelha
  `MT5_CALL_SHAPES` (`app/lane2_worker.py`) classifica cada função que o
  engine usa como **posicional** (`order_send(request)`, `symbol_info_tick`,
  `symbol_info`, `symbol_select`, sem-args) ou **kwargs-only**
  (`positions_get`/`orders_get`/`order_delete`/`history_deals_get` com
  `symbol=`/`ticket=`/`position=`). `normalize_mt5_call()` funde kwargs em
  args posicionais pelo nome (NUNCA passa um dict de kwargs a uma função
  posicional — a causa raiz 03/09 do `order_send` com `kwargs={}` → None →
  `'?'`) e rejeita formas impossíveis com erro claro, sem tocar no módulo.
  A regressão `test_marshal_replay_all_engine_call_shapes` repete todas as
  formas de chamada dos engines pelo caminho remoto completo.
- 🔎 **Auto-sonda do worker no boot/reconnect**: um `order_send` a um
  símbolo deliberadamente inválido (`__LANE2_PROBE__`, custo zero — broker
  recusa com retcode normal) corre UMA vez por episódio de ligação. Se
  devolver `None` + `last_error` real, o `/status` expõe
  `probe: {ok: false, error}` e o market_loop alerta `risk` (evento
  `lane2_self_probe_failed`, cooldown 10 min) — um módulo/terminal morto é
  apanhado no arranque, não em phantoms `'?'` silenciosos. O Diagnóstico
  expõe o bloco via `status_summary().probe`.
- 🧪 **Smoke test guiado dual-lane** (`POST /api/lanes/smoke`): abre e
  fecha a mercado uma trade REAL de 0.01 em **CADA conta demo** (principal +
  2ª; **XAUUSD** — o broker da lane 2 recusa EURUSD com 10030 "trade
disabled", apesar de ter tick; descoberto ao vivo 03/09) pelas engines
  reais de cada lane, com as DUAS trades a partilhar o **MESMO sinal** —
  prova o caminho completo (rows na BD com
  `execution_mode='mt5-smoke'`/`strategy_id='smoke_test'`/`exit_reason=
  'smoke_test'`, eventos `close`/`trade_closed`, WhatsApp single-lane com a
  mensagem só da tp2). Se uma lane falhar a abrir, a que
já abriu é fechada logo (rollback — nunca deixa posição órfã).
  **Guarda demo-only (sempre)**: opt-in `LANE2_SMOKE_ALLOW=1` no `.env`;
  as DUAS lanes em execução REAL (dry_run=False) com terminal/worker ligados
  e AutoTrading ON; as contas TEM de ser demo (`trade_mode == 0` — em conta
  real o endpoint recusa 409 antes de construir qualquer pedido); cooldown
  `LANE2_SMOKE_COOLDOWN` (default 120s, key `lane2_smoke_last_ts` na
  config). As trades de teste ficam marcadas para a analytics nunca as ler
  como resultado de estratégia. `curl -X POST
  -H "x-api-token: $TOKEN" http://127.0.0.1:8000/api/lanes/smoke`.

**Lifecycle dos loops:** `app/background.py::BackgroundRuntime` cria exatamente
uma task de mercado e uma de watchdog durante o `lifespan` e, no shutdown,
marca o stop, cancela e aguarda as duas. O isolamento é explícito: `TestClient`
e a suite MT5 não deixam `market_loop`/`listener_watchdog` pendurados num event
loop fechado; a reconexão MT5 continua no loop de mercado com throttle de 10s.
As decisões de `auto_protect` são reduzidas por `reduce_auto_protect()` em
`app/execution/protection.py`, sem side effects; os adapters aplicam depois os
fills/modifies e persistem o resultado. `app/execution/clock.py` fornece
`SystemClock` em produção e `ManualClock` nos estudos/testes, incluindo relógio
UTC e monotónico injetáveis para time stops e polling determinísticos.

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
| `close_all` (`be` true/false) | "we're out", "out at BE" | Fecha tudo (BE → à entrada). A `tp2_protect` HONRA close_all desde 16/09 (as duas lanes saem ao mercado como a `nova_gestao`) — só `close_tp1`/`move_sl` continuam a ser ignorados por ela |
| `go` | "Do it now team" | Finaliza já a janela de edição e executa (caminho único auto desde o Item 12); sem sinal em espera só regista |
| `move_sl` (`sl` opcional) | "Adjust SL to 4331", "Move SL to 4064" | Move o SL de todas as pernas abertas para o nível indicado (sem nível = nudge registado, sem ação) |
| `summary` | "**Weekly summary** ... 26 Trades Sent ..." | Guarda ground truth em `group_summaries` |
| `recount` | "Signal #1: Sell at 4357. Price went up to 4362, then dropped to 4351. +110 pips" | Guarda o recount por trade em `trade_recounts` e audita contra o fill real do mesmo dia (entrada/pips/direção → alerta; excursão/reversão vs candles; sem trade → notificação informativa com causa provável) |

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
- **Roteamento fail-safe de gestão**: reply do Telegram continua a ser a
  correlação exata; sem reply, um símbolo com trades de sinais diferentes é
  considerado ambíguo e não recebe um `close_all`/`protect` por adivinhação —
  gera alerta para o operador confirmar.
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
5. **Suite completa** — `.venv/Scripts/python -m pytest -q` (631 testes).
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
   - **sinal completo executa já** (primeiro parse válido, sem esperar) —
     decisão baseada em dados do Item 12: na BD real (26/08), dos 305
     sinais que chegaram completos no primeiro parse, **0 foram editados
     depois (0.0%)**, abaixo do limiar de 5%; só sinais **incompletos**
     entram na janela de edição (`awaiting_edit`,
     20s) — o `_SIGNAL_HINT` inclui `buying|selling|comprando|vendendo` porque
     o grupo envia primeiro "I'm selling XAUUSD" e edita a mensagem com
     entrada/TP/SL; os detalhes (TP/SL/entry/target) só contam como sinal
     quando trazem um **número** a seguir — "TP1 4396" é sinal, mas "after
     TP1"/"as SL" (conversa) não; uma mensagem que foi auditada como
     conversa mas é editada para sinal limpa o registo de ignorada
     (`delete_ignored`) para não aparecer nas duas tabelas;
   - **revive**: um sinal que falhou (ex.: entrada incompleta sem TP/SL) é
     revivido se o grupo completar a edição dentro de 5 min — reabre a
     janela com o conteúdo novo em vez de ser ignorado (`action: revived`);
     o mesmo vale para um sinal **`rejected`** (guard de zona/cap, lote
     mínimo, risco global) re-editado com conteúdo VÁLIDO dentro de
     `rejected_revive_minutes` (default 10, env
     `SIGNALBOT_REJECTED_REVIVE_MINUTES`) — com alerta claro (kind
     `revived`) de que a entrada ANTERIOR foi recusada e o motivo gravado;
     o sinal guarda um **marcador durável do revive** (`revived_from`/
     `revived_reason`/`revived_at` em `signals`, migração automática): a
     tabela Sinais mostra o badge `🔄 de rejected` (com o motivo no tooltip)
     mesmo depois de o sinal reaberto executar/rejeitar — o operador vê à
     primeira vista que aquele sinal veio de uma rejeição;
     re-edições tardias (>X min) não reabrem nada mas avisam o operador;
     edições de sinais já executados/pendentes continuam a disparar alerta
     `risk` (janela passada = entrada possivelmente desatualizada);
   - depois da janela: risk checks + execução — só existe o caminho auto
     desde o Item 12 (o modo manual foi removido); sinais bloqueados pelas
     guards de zona/lote mínimo ficam `pending` para confirmação explícita
     do operador (`confirm`), nunca por aprovação geral.
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
   - **SL de gestão de grupo com os três níveis + retry (08/09)**:
     `move_leg_sl` (kind "group": protect/move_sl/close_all BE) usa agora o
     MESMO caminho target → valid → applied do caminho híbrido (`_valid_sl`);
     um modify recusado (10016 — inclusive quando o broker aplica um
     stops_level que o `symbol_info` não reporta) ou clamped deixa o ALVO em
     fila persistida (`sl_move_pending`, por lane) e o tick re-tenta no
     nível válido até o alvo ficar aplicado — o incidente de 07/09 (protect
     recusado na lane 2, a perna rodou até ao SL completo de -38,58 €) não
     se repete. Órfãs (perna fechada/trade encerrada) são limpas no início
     e no fim de cada update; um apply em retry notifica `system`
     (`sl_move_applied`).
4. Gestão do grupo: `_handle_management()` age na trade aberta do mesmo
   símbolo (ou na mais recente).

## BE+ adaptativo e auditoria BE+ (14/09)

O buffer BE+ do `tp2_protect` é **adaptativo**: em vez dos 40 pips fixos, o tick
usa a média do spread observado do símbolo nos últimos 7 dias (`spread_observations`)
× 1.1, limitado a [10, 60] pips, com cache de 5 min — sem amostras cai no valor
estático da estratégia. `POST /api/config` com `be_buffer_adaptive=0` desliga e
fixa o estático. Os estudos podem reproduzi-lo com `be_buffer_adaptive=1` no
`GET /api/analysis`. A auditoria diária (`GET /api/operations/daily-audit`) expõe
um bloco `be_plus` (saídas protegidas com lucro/perda) e o resumo diário inclui a
linha "🛡️ BE+ do dia", com o contrafactual "BE clássico teria corrido até TP2"
calculado com velas (spot MT5 → GC=F Yahoo; sem velas, `checked=0`, nunca inventa).
Scripts one-off esgotados (`mine_management`, `sweep_atr`, `sweep_auto`,
`sweep_struggle`, `study_guard_calibration`, `study_pending_orders`) movidos para
`tools/archive/` — fora do pacote live, referências históricas nos docs intactas.

## Mixins state.py + vigia do drift do spread (14/09)

Continuação da extração de mixins: `IngestMixin` (`app/ingest_mixin.py` —
`ingest`, janela de edição, revive, `_try_execute`, confirm/reject/delete)
e `ManagementMixin` (`app/management_mixin.py` — `_handle_management`,
correlação de alvos, `pending_management`, resumos do grupo) saíram de
`state.py` verbatim, seguindo o padrão `RecountsMixin`/`GatesMixin`/
`LanesMixin`; os regex de reconhecimento (`_SIGNAL_HINT`, `_PROGRESS_PIPS`,
`_LEVEL_MENTION`) vivem nos dois ficheiros em cópia sincronizada. Testes que
fazem monkeypatch de `app.state.try_llm_parse` passaram a mirar
`app.ingest_mixin.try_llm_parse`.

O BE+ adaptativo tem vigia de drift: o tick guarda uma linha diária por
símbolo em `be_buffer_history` e emite alerta `risk` quando o spread médio
(7d) sai da banda saudável [20, 50] pips (guard diário por símbolo, respeita
`be_buffer_adaptive=0`). O /auditoria tem o bloco "🛡️ BE+" (saídas
protegidas + contrafactual TP2 do daily-audit).

## Kill diário por saldo da lane (08/09)

O auto-kill (`daily_loss_limit_pct`) avalia **cada lane MT5 contra o saldo
da PRÓPRIA conta** (`state._daily_loss_killed`), não a soma dos P&L de
todas as lanes contra o saldo da principal — 07/09: -52,36 € da tp2 (10k,
-0,52%) somados a -1,11 € da principal = -5,17% de 1.033 € disparou o kill
global com as duas contas individualmente dentro do limite. Sim puro (sem
conta MT5 em nenhuma lane) mantém o comportamento legado (P&L total vs
saldo configurado); a lane 2 em dry-run usa a conta sintética do `.env`.
O risco.check por lane no `_try_execute` já era por saldo desde antes.

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
- `GET /api/analysis/resolution` — compara M1 quando o Yahoo o conserva, explicita o fallback de resolução e mede a sensibilidade a atrasos 0/8/60/300s.- `GET /api/analysis/protect_causal` — replay read-only do histórico completo
  (`days=0`) que compara a híbrida com a mesma auto sem `protect`, atribui
  o delta incremental por evento e expõe os piores eventos.
- **Estudos read-only**: `.venv/Scripts/python -m app.study_strategies
  O recorte recente desde `2026-08-28` teve apenas 9 trades resolvidas: `nova_gestao` teve maior P&L, `tp2_protect` melhor PF/DD observado e `auto_escada_3` ficou negativa. Isto é evidência exploratória, não base suficiente para promoção automática; manter a comparação paper/real por pelo menos 20–30 trades comparáveis.
  --study {ablation,walkforward,legs,fallback,polling}` compara cada kind de
  gestão isolado, walk-forward calibrado (com `--period current|90|all|old`), pesos TP3..TP5, fallback mecânico
  após TP1 e polling paper a 1s/250ms/100ms. Usa BD temporária no benchmark
  de polling, não muda o loop de produção, não altera a config e não envia
  ordens MT5. `legs` deve ser repetido com saldo 10.000 € para ultrapassar o
  piso de lote mínimo; com 1.000 € os pesos ficam iguais por arredondamento.


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
- `GET /api/recounts` — recounts por trade do grupo (R2, read-only): cada
  "Signal #N: … +N pips" guardado em `trade_recounts` e comparado com os
  fills reais do mesmo dia (`_recount_fill_audit`) — entrada ≥20 pips,
  pips ≥30 ou direção divergente disparam alerta `risk` (fill-audit externo
  grátis). O audit inclui também a direção e o **candle-check**: a
  excursão/reversão reportadas pelo grupo são comparadas com o high/low
  real dos candles do dia (`_recount_candle_check`, Yahoo 15m, sem símbolo
  associável → `checked=False`, nunca inventa plausibilidade). O endpoint
  devolve cada recount com o seu audit (`matched`/`alert`/`candle_check`) e
  a agregação `weekly` (estado por semana: coerentes/divergentes/sem trade)
  para o mini-gráfico da tab Relatório, que mostra a tabela "📋 Recounts
  do grupo vs fills reais" com os divergentes marcados; um recount AO VIVO
  sem trade correspondente no dia emite notificação informativa `system`
  com a **causa provável** cruzada com os sinais do dia
  (  `_recount_no_trade_cause`: failed/rejected, pendente, direção oposta ou
  nada no dia); uma excursão/reversão que não cabe no range real do dia
  dispara alerta `risk` "Recount implausível". O candle-check usa uma
  **janela horária** (±3h à volta da hora do sinal, quando conhecida) como
  veredicto da sessão — o high/low do dia inteiro mistura sessões (um pico
  às 22:00 não valida um relato do sinal das 09:00); o dia inteiro fica em
  `day_plausible`/`day_*` para contexto, e o tooltip da tabela de recounts
  expõe dia + janela. Ferramentas read-only:
  `app/sweep_recount_candles.py` (plausibilidade de todos os recounts),
  `app/backfill_recounts.py` (re-analisa `ignored_messages`/`signals`
  falhadas à procura de `Signal #N`; `--apply` grava em `trade_recounts`),
  `app/study_recount_mfe.py` (recounts vs MFE real dos candles — valida a
  convenção de win do grupo R3 contra a excursão real),
  `app/study_recount_implausible.py` (implausíveis vs fills reais do bot:
  negociou ou não, com causa provável + veredicto agregado: erro de
  relato / erro de execução / sem dados, top-5 de cada),
  `app/study_recount_baseline.py` (baseline de falsos positivos do
  candle-check sobre os sinais reais — atenção: o feed Yahoo serve GC=F
  futuros vs spot do grupo, a taxa alta não prova relato falso) e
  `app/study_signal_delay.py` (atraso sinal→mercado: quando o GC=F toca a
  entrada vs a hora do Telegram — mediana 3.7h nos 30d reais, a janela
  ±3h é justa face ao feed; `study_recount_baseline.py --window-hours 6`
  re-mede o baseline com a janela alargada: 66.3% → 62.4%; com `--apply`
  grava a janela recomendada por símbolo em `recount_window_hours_by_symbol`
  — na BD real a recomendação automática de 12h é contaminada pelo feed
  GC=F, p75 29h ≫ mediana 3.7h, por isso ficou no default 3h: com o spot
  MT5 o atraso medido é mediana 106 min e 100% das entradas tocadas ≤3h
  (101/101) — a janela 3h cobre tudo). A janela do
  candle-check é adaptativa por símbolo (`_recount_window_hours`, config
  `recount_window_hours_by_symbol` → fallback `recount_window_hours` 3h,
  limitada a [1, 24]h). A FONTE do candle-check é o spot MT5 quando o
  terminal está ligado (`_recount_candles`, cache 5 min, fallback GC=F
  Yahoo, símbolos do mapeamento MT5: XAUUSD/XAGUSD/forex) — o veredicto é
  COMBINADO (`spot_plausible`/`futures_plausible`/
  `feed_divergence`/`verdict_kind`): implausível no GC=F mas plausível no
  spot marca "⚠️ provável divergência de feed" no /auditoria/dashboard,
  não relato suspeito; o GC=F é comparado SEM correção (o offset
  spot↔futuros foi eliminado — `_gold_futures_offset` ficou só no estudo
  read-only `study_recount_baseline --apply-offset`).
  `app/study_recount_mt5_feed.py` corre o MESMO
  check com candles M15 de XAUUSD spot do terminal vs o GC=F (`--signals`
  recalibra o baseline com as entradas reais: 0.0% fora da janela no spot
  vs 66.3% no GC=F — os implausíveis eram TODOS divergência do feed); o
  recount de 08-19 marcado implausível pelo GC=F é plausível no spot.
  `POST /api/recounts/recheck-all` refaz o check de TODOS os recounts
  (fontes combinadas com cache) e persiste o veredicto em
  `trade_recounts.candle_check`/`checked_at`; o /auditoria tem o botão
  "🗂 re-check todos" e o tick corre o lote automaticamente 1×/dia UTC
  (`_check_recount_batch`, guard `recount_batch_last_run`) notificando
  veredictos alterados; 1×/semana (guard ISO-week `recount_batch_week_last_run`)
  re-corre o histórico COMPLETO (`days=0`) para os veredictos antigos ficarem
  corrigidos com o offset medido. Cada corrida fica persistida em
  `recount_batch_runs` (nova tabela) com o contador `resolved` + a lista de
  flips (data, recount, de → para, offset) — `/api/recounts/batches` e bloco
  no /auditoria mostram a evolução das correções do offset. O alerta AO VIVO de relato suspeito só dispara com
  CONFIRMAÇÃO (spot E GC=F a discordar, ou sem GC=F); o /auditoria separa
  "Implaus. confirmados" de "Divergência de feed" (baseline spot 0%). Um
  implausível AO VIVO com o journal ligado
  grava o evento `recount_implausible` em `execution_events` (evidência
  passiva para o replay, com o veredicto combinado no payload); o
  fill-audit (`GET /api/analysis/fill-audit`)
  devolve `recount_evidence` com esses eventos (real = relato, replay =
  candles, `bot_traded` e `miss_reason` — listener reiniciado ±15 min
  (`restart_kind`: watchdog vs manual/externo, `signals_lost_est`,
  `vps_reboot` = reinício manual que coincide com o `server_booted_at` do
  boot.ps1) ou
  kill switch ativo quando o bot não negociou) e
  `POST /api/recounts/{id}/recheck` refaz o candle-check com candles
  frescos (read-only). O candle-check calibra o offset GC=F↔spot com os
  fills reais de XAUUSD (`_gold_futures_offset`, ≥3 amostras) para
  reduzir falsos positivos; o `recounts_report` expõe `calibration`
  (offset/amostras/ativo) e o /auditoria mostra o painel "Offset GC=F↔spot"
  para saber quando a calibração passa a estar ativa. A página `/auditoria`
  tem o bloco "📋 Qualidade
  dos recounts do grupo" (stats + tabela + semanal + mini-gráfico de
  implausibilidade + taxa vs baseline 66%) com botão de re-check por linha
  e o toggle ⏸️/▶️ do journal; o resumo diário inclui a linha "📋 Recounts
  do dia". A tabela de recounts da principal mostra badges visuais de
  janela (✓/✗ janela ok/implausível + contexto do dia).

- **Offset diário GC=F↔spot no candle-check**: `state._day_futures_offset`
  mede GC=F−spot nos fills do dia (spot do guard vs close do candle GC=F),
  prefere a média do próprio dia, cai na média dos 30 dias do símbolo sem
  fills e devolve 0 sem amostras — o valor aplicado fica exposto por recount
  (`futures_shift`/badge "offset N pips"). Com ele um recount que era
  `feed_divergence` flips a `plausible` (o prémio do futuro, não relato
  suspeito); o lote conta esses flips como `resolved`.
- **Decidir o cap autónomo com dados reais**: mudar `entry_guard_auto_cap`
  via `/api/config` grava `cap_applied_at`; o estudo read-only
  `app/study_cap_effect.py` usa-o como `--since` (sem ele assume hoje 00:00)
  e o tick (`_check_cap_reminder`) lembra no feed uma vez por dia quando o
  cap está aplicado há >`cap_reminder_days` (7) sem fills pós-cap, pedindo
  para correr `study_cap_effect --days 30` e decidir se o cap fica/aperta/
  reverte — nunca muda o cap sozinho.
- **MFE win rate (R3)**: o backtest mede `mfe_pips` (excursão máxima
  favorável) por trade e o agregado expõe `mfe_win_rate` — wins na
  convenção do grupo (MFE ≥ 25–30 pips), mesmo que a trade tenha voltado e
  fechado em loss/BE. A divergência grupo↔bot (`_check_group_divergence`)
  compara com o `mfe_win_rate` quando disponível (menos falsos positivos:
  uma trade que andou +40 pips e voltou ao SL é win para o grupo, não para
  o bot).
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
SL/ invalid") — o histórico completo tem **579 sinais reais de XAUUSD**
(abr→hoje). No período atual (30 dias, formato novo): PF nova 1.1,
nova+gestão **2.92**, fechar no TP2 0.82. O período antigo (abr–jul) era
perdedor no backtest (PF < 1) — o edge da gestão só aparece no formato
recente. Drawdown > 100% possível porque o sim usa risco % fixo sobre o
saldo inicial (sem parar em falência).

- **Auditoria histórica de 19/08:** a amostra é desigual (506 sinais antigos
  contra 73 desde 28/07). Repetições sem spread/slippage/comissão e sem o
  atraso de 60s mantiveram o período antigo negativo; não atribuir esse regime
  perdedor apenas aos custos de execução.
- Quando `protect_causal` marca eventos como `deferred`/`not eligible`, “0
  harmful” significa ausência de evidência de dano nesse replay, não prova de
  benefício causal.

## Estratégias (registo central + páginas de teste + paper trading forward)

- **`app/strategies.py`** — registo central: cada estratégia define `max_legs`,
  `manage_kinds` (quais kinds de gestão honra — `manage=True` legacy = todos),
  `exit_at_tp` e `lots_override`. `MAIN_STRATEGY_ID = "nova_gestao"` (a melhor
  segundo os dados) é a que a conta principal executa. **Remoção completa
  (01/09)**: `tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5`
  e `auto_escada_3_struggle` (R4) foram APAGADAS do registo — já não são
  backtestáveis (o histórico do porquê e PF/DD está no CHANGELOG de 01/09);
  `get_strategy()` levanta ValueError para ids mortos (um estudo com uma
  estratégia apagada falha em voz alta, nunca corre silenciosamente com a
  principal; a config/snapshot trata um id morto como órfão com badge —
  `_strategy_params`). Ficam **7 visíveis no menu/dropdown**: `nova_gestao`,
  `nova_gestao_fallback`, `tp2_protect`, `auto_3`, `auto_escada_3`,
  `auto_escada_3_hibrida` e `auto_escada_5` (as 5 primeiras operacionais; a
  híbrida é experimental: combina a auto-gestão com `protect` monotónico do
  grupo e não altera a principal; `auto_3`/`auto_escada_5` são referências
  de backtest com página de teste própria). `MENU_HIDDEN_STRATEGIES =
  {nova, auto_5}` (ocultas do menu; a `nova` continua a abrir papel
  **silenciosamente** — sem card na corrida nem feed — só para alimentar o
  contrafactual "sem gestão" do histórico de trades (`counterfactual_pnl`);
  a `auto_5` é a comparação 5 pernas com/sem trail, backtest-only).
  **Corrida paper focada (01/09)**: `paper_strategy_ids()` abre papel
  NORMAL só nas 5 em validação + a `nova` contrafactual — as referências
  `auto_3`/`auto_escada_5`/`auto_5` já não abrem corrida (backtest/compare/
  shadow continuam; o shadow tem a própria lista `shadow_paper_strategies`
  e a `auto_escada_5` mantém a evidência do gate forward isolada com
  `origin=shadow`). Os dados paper órfãos
  das removidas ficam na BD sem leitores (nunca se apagam dados). O filtro
  `manage_kinds` aplica-se à conta
  real (quando promovida), ao backtest (`simulate_signal_managed`) e à
  corrida paper. Adicionar estratégia nova = 1 entrada no dicionário + 1
  item no menu (`common.js` STRATEGY_MENU fallback + `TERM_ORDER`).
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
  ao mercado), **modo defensivo** (PF(10) < 1.0 → trail 1.5 + time stop 60,
  sem cortar pernas) e **regra "struggle"** (R4, `struggle_ae_pips`/
  `struggle_fraction`): antes do TP1, se o preço andou contra a entrada por
  N pips e volta à entrada, fecha uma fração das pernas a BE e protege as
  restantes na entrada — conselho do admin "take 50% / make risk-free" (a
  candidata `auto_escada_3_struggle` foi REMOVIDA do registo a 01/09: o
  backtest de 20/08 não encontrou edge — histórico no CHANGELOG; o mecanismo
  fica nos motores para candidatas futuras). Ferramentas: `app/mine_management.py` (minera a gestão real
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
  secção `app/strategies.py` acima) — 7 visíveis no menu, 2 ocultas do menu
  mas em papel, 4 só no backtest.
- **Layout** (dashboard): tema **claro profissional** (sem cyberpunk/neon;
  sem animações decorativas — o boot sequence e os sons foram removidos) com
  **tema escuro** opcional — botão ☾/☀ na barra superior (todas as páginas,
  `common.js`), persistido em `localStorage.nx_theme`, aplicado antes do
  paint por um snippet no `<head>` de cada página (default = preferência do
  SO; o login respeita o mesmo). Sem escolha explícita o tema **segue o SO
  ao vivo** (`prefers-color-scheme` com listener `change`); uma escolha no
  botão fica persistida e o SO deixa de interferir — o arranque do chrome
  nunca grava o tema (gravar marcaria escolha explícita e mataria o follow). O dark é uma camada `[data-theme=dark]` de
  variáveis — nenhum hex hardcoded fora das variáveis; os gráficos canvas
  usam `chartPalette()` (`common.js`) para grelha/eixo/fundo por tema. Com
  todo o chrome numa **barra superior fixa** (`.topbar`, renderizada pelo
  `common.js` num placeholder `<div id="appChrome">`): linha 1 = marca +
  navegação horizontal (PRINCIPAL · ▚ ESTRATÉGIAS dropdown · DIAGNÓSTICO ·
  AUDITORIA · HELP; setas ↑↓ + Enter + rato) + badges (modo/estratégia/MT5/
  execução) + utilizadores + relógio + **Kill switch** + **Sair**; a faixa
  de preços (GOLD..NZDUSD) foi **removida** em 09/2026 por decisão do
  utilizador (perf — o topbar fica estático; o preço ao vivo vive no
  status/market badge "preços reais"). O conteúdo usa a largura
  total (máx. 1440px) — as colunas laterais (`--term-w`/`--term-r`) e os
  overlays móveis ☰/▦ foram removidos; ≤960px o ☰ abre a navegação em
  dropdown. Na principal a ordem é status → KPIs → tabs → conteúdo, com os
  painéis analíticos (corrida/promoção/Fase 2/3/narrativa/shadow) depois do
  conteúdo (CSS `order`). **Render por diffs**: só a tab ativa reconstrói e
  só quando o conteúdo mudou (`contentChanged` — cache de assinatura em
  `common.js`); o feed reconstrói apenas em mudança; o snapshot envia 30
  notificações (antes 50). As **7 estratégias visíveis** vivem no **dropdown
  ▚ ESTRATÉGIAS**
  (fechado por defeito em cada página; fecha ao clicar fora;
  a navegação por teclado abre-o sozinha ao chegar a um item lá dentro) —
  cada uma com página de teste própria `/teste?s=<id>` (saldo simulado de 1
  mês a começar em 1000 € + mini-curva + comparação + corrida paper ao
  vivo; o **fluxo de exemplo de uma trade real** — `GET /api/analysis/trace`
  — usa `nova` por defeito). Menu atual (removidas a 01/09):
  `nova_gestao`, `nova_gestao_fallback`, `tp2_protect`, `auto_3`,
  `auto_escada_3`, `auto_escada_3_hibrida`, `auto_escada_5`; o shadow-paper
  observa `auto_escada_3` e `auto_escada_5` por defeito; `nova`/`auto_5`
  continuam em papel (ocultas do menu — contrafactual/comparação). `tp2`,
  `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5` e a candidata R4
  `auto_escada_3_struggle` foram APAGADAS do registo (ver secção
  Estratégias acima + CHANGELOG 01/09).
  A página **HELP** explica cada estratégia de forma
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
  1000 €, para comparar em igualdade de circunstâncias; além das **7
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
  badges de estado (`hdr-badges`): modo auto, estratégia ativa
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
- **Shadow-paper (forward sem ordens demo):** `config.shadow_paper_enabled`
  (default `true`) e `shadow_paper_strategies` (default
  `["auto_escada_3"]`) fazem sinais live completos abrirem uma conta virtual
  marcada `origin="shadow"` antes das decisões de risco/kill da principal.
  O caminho é idempotente por `signal_id+strategy_id+origin` (permite uma
  trade race e outra shadow do mesmo sinal, mas nunca duplicados dentro da
  mesma origem), não corre no replay `historical=True` nem em propostas
  `llm_parsed`, e a gestão do grupo
  (incluindo TP1/SL corrigidos) segue a mesma correlação por reply/símbolo sem
  consumir a fila real: `pending_management.applied` (real),
  `pending_management.paper_applied` (corrida normal) e
  `pending_management.shadow_applied` (shadow) são marcadores separados. O painel
  principal permite ligar/desligar e escolher estratégias; o snapshot expõe
  `shadow_paper` com `execution_isolated=true`; os saldos, kill diário e
  histórico da conta shadow também são separados da corrida normal. Isto
  continua paper e não autoriza qualquer ordem MT5.
  - **Regra de consultas:** `paper_trades` pode ter duas linhas com o mesmo
    `signal_id`/`strategy_id`; relatórios da corrida, sobretudo divergências,
    têm de filtrar `origin="race"` para não escolher a trade shadow como referência.
  - Gestão shadow sem reply mantém o fallback operacional reply → símbolo →
    trade mais recente, igual à conta real; não criar um caminho “só por reply”.
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
  bloqueia a abertura de novas trades virtuais no dia). Se existir
  `killswitch_rearm_at`, o tick rearma a pausa uma vez no instante UTC
  persistido, limpa a chave e avisa feed/Telegram; o rearme nunca promove
  estratégia nem altera a conta real.
- A janela operacional UTC até sexta-feira 28/08/2026 termina em
  `2026-08-29T00:00:00+00:00`. `GET /api/window` agrega trades reais fechadas/
  abertas, P&L, dias restantes, ritmo face à meta 10–20 e o painel de promoção
  da `auto_escada_3` (gate forward N/PF/IC95/DD + ETA). O resumo diário inclui
  uma linha compacta com estes mesmos números.
- Correções explícitas de summaries são classificadas como
  `summary_correction`: o alvo é resolvido por reply Telegram ou período
  explícito inequívoco, os números do summary original são regravados e a
  divergência grupo↔bot é recalculada; alvo ambíguo nunca é adivinhado.

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

## Risco defensivo e estado persistente (19/08)

As `auto_escada_3`, `auto_escada_3_hibrida` e `auto_escada_5` têm modo
 defensivo após uma janela completa de 10 trades paper/real com PF < 1.0:
 trail 1.5, time stop 60 min e **risco máximo 1.25%** (`def_risk_pct`). O
cap é absoluto e combina com o risco dinâmico de drawdown sem nunca aumentar
o risco; a janela usa apenas trades já fechadas e é igual no backtest,
PaperRace, SimEngine e MT5Engine. A principal persistida é agora
`nova_gestao`, escolhida pelo melhor resultado comparável do backtest recente;
o kill switch e o modo MT5-dry permanecem intactos até autorização operacional.

O estado vivo de escada/trailing/time stop/protect é persistido na tabela
`protection_states` por motor/origem/trade. Depois de restart, os picos,
vales, TP1 e SLs aplicados são reconstruídos de forma idempotente; apenas o
throttle monotónico do MT5 é reiniciado. O backup pré-migração desta alteração
é `backups/signalbot-20260819-144647.db`.

O shadow-paper continua isolado da conta MT5 e agora observa, por defeito, as
duas variantes `auto_escada_3` e `auto_escada_5`, cada uma com a sua conta
virtual e origem `shadow`; isto não abre ordens nem altera a corrida normal.

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
2. **Correr `pytest` antes de concluir** — a suite completa (atualmente 456 testes, atualizar se crescer) é a rede de segurança (parser, classificador, backtest, API, auth, observabilidade e estudos read-only).
3. **Não criar trades de teste sem limpar** os artefactos depois (trades,
   posições, sinais de teste na BD).
4. **Não guardar senhas em ficheiros de código** — vêm do `.env`/env vars.
5. **Nunca tocar na conta real** (a demo usa `MT5_LOGIN=5054644473` /
   `MetaQuotes-Demo`).
6. Após terminar uma tarefa: verificar processos vivos (server + listener),
   suite verde, estado da BD limpo e **documentação atualizada** (secção
   acima).
7. **GitHub local-only por agora:** mesmo existindo `origin`/workflow, não fazer
   API calls, push ou branch protection sem pedido explícito do utilizador.
8. **Credenciais coladas no chat ficam comprometidas:** revogar/rodar fora do
   repositório e nunca reutilizar ou guardar esses tokens no código.
9. **HELP sincronizada:** qualquer alteração em `app/strategies.py`, gestão do
   grupo, shadow-paper, risco ou conceitos visíveis exige atualizar
   `app/static/help.html` e `tests/test_frontend.py`; toda estratégia com
   `hidden=false` deve ter uma entrada em `HELP_FLOW` e uma explicação curta.
