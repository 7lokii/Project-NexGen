# Changelog — Project NexGen

Registo das alterações ao programa. **Regra**: sempre que algo for
implementado/mudado, acrescenta uma entrada aqui (data + o que mudou +
nº de testes). As docs (AGENTS.md, TECHNICAL.md, PUBLIC.md) devem ficar a
par no mesmo turno.

Formato: `### YYYY-MM-DD — título` · bullets com o quê/porquê.

---

### 2026-09-16 — Boot da lane tp2 corrigido + falha de boot ≠ bloqueio runtime + WhatsApp single-lane + CLOSE ALL na tp2_protect

- **Boot da tp2 falhava SEMPRE com worker saudável** (causa raiz): o lifespan
  inicializava o `mt5_provider2` LOCAL — que tem bloqueio permanente por
  design quando `MT5_DRY_RUN=0` ("a API MetaTrader5 usa uma ligação global
  por processo") — em vez do `RemoteProvider` do worker. O feed mostrava
  "⛔ Execução MT5 bloqueada na lane tp2 ..." em todos os arranques e a lane
  só ligava depois via reconnect do market_loop. Agora `_lane2_provider()`
  (`app/main.py`) devolve o MESMO provider que o reconnect usa (worker
  quando `LANE2_WORKER_ENABLED`, local em dry-run).
- **`RemoteProvider.initialize` deixou de poluir `execution_block_reason`
  com o motivo TRANSITÓRIO de worker offline** (`app/execution/remote2.py`):
  o motivo fica em `_offline_reason` — um `execution_block_reason` truthy
  fazia o `_reconnect_mt5` saltar a lane para sempre (a tp2 falhava no boot
  e nunca mais tentava religar).
- **Falha de boot ≠ bloqueio runtime no feed**: a falha transitória de
  arranque notifica kind `system` ("⏳ [lane] MT5 não ligou no arranque —
  transitória"); o bloqueio permanente (conta não hedged, config errada)
  continua kind `risk` ("⛔"). Quando o reconnect/retry liga a lane que
  falhou no boot, emite a RECUPERAÇÃO ("✅ [lane] MT5 religado após falha de
  arranque").
- **Retry único em background no arranque**: task própria do lifespan
  (`nexgen:boot_retry`, cancelada no shutdown) re-tenta UMA vez as lanes que
  falharam no boot, passado `MT5_BOOT_RETRY_DELAY` (default 30s) — cobre a
  corrida com o arranque do worker portable pelo boot.ps1 (o worker demora
  segundos a abrir a porta/ligar o terminal e o 1º initialize perde-a).
  Sucesso → notificação de recuperação; bloqueio permanente → `risk`. Se
  falhar de novo, o reconnect do market_loop (throttle 10s) continua.
- **WhatsApp: UMA mensagem por sinal, só da lane 2** (`app/whatsapp_mixin.py`):
  antes, main e tp2 fechavam em ticks diferentes e o dedup por tick não
  cobria — o grupo recebia DUAS mensagens (dual-lane) do mesmo sinal. Agora
  o hook filtra `lane_id == "tp2"` (fechos da main não enviam) e usa o
  formato single-lane `format_trade_close` com o rótulo da conta tp2. O
  smoke-test usa a mesma regra (`notify_smoke`).
- **`tp2_protect` honra `close_all`** (`app/strategies.py`, decisão do
  utilizador): quando o grupo diz "CLOSE ALL" / "we're out", as duas lanes
  saem ao mercado como a `nova_gestao` — antes a tp2_protect ignorava e a
  lane 2 continuava a correr para o TP2 (observado ao vivo 16/09 12:08).
  Continua a ignorar `close_tp1`/`move_sl`. Nome novo: "Sair no TP2 +
  protect + close_all".
- **Diagnóstico/BD**: eventos novos `boot_retry_success`/`boot_retry_failure`
  em `mt5_connection_events`.
- testes: 631.

### 2026-09-05 — Marcador durável de revive no histórico de sinais (badge "🔄 de rejected" + motivo guardado)

- O sinal passou a guardar **de que estado veio quando a janela foi reaberta**:
  colunas novas `revived_from` / `revived_reason` / `revived_at` em `signals`
  (migração automática em `db._migrate`), preenchidas no `_revive_signal` com o
  estado anterior e o **motivo gravado** da recusa/erro ANTERIOR.
- A tabela **Sinais** da dashboard mostra agora um badge `🔄 de rejected`
  (verde tracejado) ao lado do estado, com tooltip com a hora do reaberto e o
  motivo anterior; o marcador **sobrevive à execução** — depois de o sinal
  reaberto executar (ou ser rejeitado outra vez), o histórico continua a
  mostrar que aquele sinal veio de uma rejeição, à primeira vista.
- O marcador é preservado em edições seguintes dentro da janela (`_apply_edit`
  copia `revived_*` do existente) e aparece no snapshot (`signals` → `to_dict`)
  e na resposta `POST /api/signals` (`signal.revived_from/reason/at`). O feed
  mantém o alerta kind `revived` com o motivo no texto (grupo "🔄 Revives").
- **Testes**: asserções novas no revive de `rejected` — marcador no payload,
  persistência e exposição no snapshot depois de executar.
- testes: 592.

### 2026-09-05 — Sinal `rejected` reabre a janela quando o grupo re-edita com conteúdo válido (janela de revive configurável)

- **Antes**: uma rejeição (guard de zona/cap, lote mínimo, risco global) era
  terminal — uma edição posterior do grupo caía em "sinal já finalizado
  (rejected)" e ficava muda. A recusa era sobre o conteúdo/preço ANTIGO, mas
  a intenção nova do grupo perdia-se.
- **Agora** (`app/state.py::_ingest_telegram` + `_revive_signal`): se a
  mesma mensagem Telegram for re-editada com conteúdo VÁLIDO dentro de
  `rejected_revive_minutes` (default 10, env `SIGNALBOT_REJECTED_REVIVE_MINUTES`)
  da chegada original, o sinal volta a `awaiting_edit` (`action: revived`,
  `revived_from: rejected`) e corre a pipeline de novo — com **alerta claro**
  no feed (kind `revived`): "A entrada ANTERIOR foi recusada (<motivo>).
  Janela de Ns reaberta." O motivo vem do `error` gravado na rejeição.
  Parse só via IA (LLM) continua a ser registo apenas (Item 12, nunca
  executa). Depois da janela, a recusa é reavaliada honestamente: pode
  executar ou ser rejeitada de novo.
- **Depois da janela**: uma re-edição tardia (>X min) NÃO reabre nada (não se
  perseguem entradas recusadas há demasiado tempo) — mas o operador recebe
  um aviso `system` de que o grupo voltou a mexer com conteúdo válido e a
  recusa anterior se mantém (só quando o re-edit parseia, não em edições
  parciais).
- **Config**: `rejected_revive_minutes` em DEFAULT_CONFIG (visível em
  `/api/status` → `config`), coerção float no boot. O revive de `failed`
  continua com a janela fixa de 5 min (inalterada).
- **Testes**: `test_rejected_signal_revived_when_group_reedits` (rejeição por
  cap → re-edição → revived + alerta com o motivo → executa com o conteúdo
  novo) e `test_rejected_signal_edit_after_revive_window_alerted_and_ignored`
  (janela expirada → mantém rejeitado + aviso).
- testes: 592.

### 2026-09-04 — Rejeição de sinal mostra o bloqueio REAL de cada lane (incidente 07:32 sell XAUUSD)

- **Sintoma**: sell XAUUSD (4475-4479, TP1..TP4, SL 4483) rejeitado às
  07:32:27 com "lote mínimo não cabe no risco configurado em main (16.00 >
  10.02)" — sem ordens enviadas em NENHUMA lane e sem a lane 2 aparecer no
  motivo. O grupo ainda mandou "SL HIT" 11 min depois (o preço tinha
  atravessado o SL; o shadow-paper do mesmo sinal fechou logo em SL:
  nova_gestao -32€, auto_escada_3 -24€ às 07:32:28 — executar era entrar a
  perder de certeza).
- **Causa raiz**: o gate de lote mínimo é PRÉ-computado por lane no início
  do loop (fica no dicionário `exceeded`) MESMO quando a lane é bloqueada
  ANTES pelo guard de zona (preço fora da zona entre o guard inicial e a
  execução, sem confirmação manual no modo autónomo). O branch de rejeição
  mostrava só o resumo dos gates excedidos → culpava o lote mínimo (que
  nunca foi a causa — com SL 8 e auto-fit, 1 perna de 0.01 cabe nos €10.02)
  e calava as notas reais das lanes.
- **Fix** (`app/state.py`): quando nenhuma lane abriu e há gates excedidos,
  a mensagem de rejeição mostra PRIMEIRO as notas reais por lane
  (zona/risco/exceção) e só junta o resumo do lote mínimo se alguma lane
  chegou mesmo ao gate e não coube. As notas de zona deixam de ser
  engolidas; o caso puro lote-mínimo mantém a mensagem anterior intacta.
- **Teste**: `test_reject_message_mostra_bloqueio_real_quando_zona_ganha_ao_lote`
  (guard 1 na zona → guard 2 fora da zona → rejeição mostra a zona e NÃO o
  lote mínimo).
- testes: 590.

### 2026-09-04 — Stalls do `snapshot.build` eliminados: WAL no SQLite + caches (worker, blocos informativos, lanes reutilizadas)

- **Causa raiz final dos stalls de 3.5–6.4s do `snapshot.build` encontrada e
  eliminada**: o SQLite corria em `journal_mode=delete` num ficheiro de
  647MB — com o journal de execução ligado (~3M linhas), CADA commit dos
  inserts de quotes do tick (1×/s) fsyncava o ficheiro inteiro: 15–580ms
  por commit (medido numa cópia da BD), congelando o event loop. A ligação
  agora abre em **WAL + synchronous=NORMAL** (`app/db.py`): commits ~0ms,
  leituras não bloqueiam escritas e o checkpoint é assíncrono; sem
  corrupção em crash (só perde as últimas transações num corte de energia
  — aceitável para journal/telemetria). O backup (`backup_db.py`, API do
  sqlite3) e os testes funcionam com WAL.
- **RemoteProvider (lane 2) passou a cachear `account()`/`positions()` com
  TTL** (mesma semântica do `MT5Provider` local) — antes CADA chamada era
  um HTTP ao worker (15–160ms): o snapshot consultava a conta 5–8× e as
  posições 2–3× por lane por tick. A EXECUÇÃO (engine →
  `_mt5.positions_get`) fica FORA do cache — nunca há stale para decidir
  SL/TP.
- **`_execution_state` reutiliza o snapshot de lanes já construído** em vez
  de reconstruir as lanes uma 2ª vez por snapshot (duplicava ~8 leituras
  MT5/worker por lane por tick).
- **Cache TTL para blocos informativos caros do snapshot** (`_ttl_get` em
  `BotState`): `risk_preview` por lane (5s, chamado 3×/tick),
  `paper_status`/`shadow_paper_status` (10s), `window_status` (10s —
  carregava `list_trades(200000)` por tick!) e `fase3_*` (60s). O snapshot
  do tick deixa de pagar ~20 agregações DB por segundo; os handlers que
  MUTAM o estado invalidam a cache inteira. Frescura: os painéis
  informativos atualizam em ≤ TTL (5–60s) — read-only para a execução.
- **Journal bounded + stats cacheado**: `execution_event_stats(7)` (GROUP
  BY de ~1s sobre milhões de linhas) com cache de 60s e retenção
  configurável `EXECUTION_JOURNAL_RETENTION_DAYS` (default 30) com purge
  no arranque e no watchdog (~6h) — a tabela nunca volta a crescer sem
  limite (3M linhas → o primeiro purge removeu 52k+).
- **Resultado ao vivo**: `snapshot.build` de 3.5–6.4s → ~0.5s no steady-
  state (sem fases ≥2s no stallwatch); o healthcheck não registou NENHUMA
  observação "sem resposta HTTP" desde o restart WAL (antes escalava e
  matava o servidor).
- testes: 589.

### 2026-09-04 — Vigia de stalls do event loop: quando + onde (bloco ⏱️ Loop stalls no Diagnóstico)

- **Sintoma a resolver**: o healthcheck registava "sem resposta HTTP" e os
  logins penduravam sem se saber QUANDO nem ONDE o event loop congelou.
- **Monitor de congelamentos** (`app/stallwatch.py`): task própria do
  lifespan (fora do BackgroundRuntime) que acorda a cada 0.5s
  (`LOOP_STALL_INTERVAL`) e mede o atraso real do wakeup com `loop.time()`
  (monotónico) — um loop bloqueado N segundos faz a task acordar N segundos
  tarde. Congelamento ≥ `LOOP_STALL_THRESHOLD_SECONDS` (default 2s) é
  gravado em `loop_stall_events` (nova tabela) com kind `stall`.
- **Atribuição da causa**: fases on-loop instrumentadas gravam kind `phase`
  quando demoram ≥ limiar — `tick.update:main`/`tick.update:tp2` (o
  engine.update por lane no tick), `snapshot.build` (construção do snapshot
  no fim do tick) e `diagnostics.on_loop` (parte on-loop do Diagnóstico que
  lê o terminal) — o sintoma e a causa aparecem lado a lado na BD.
- **Bloco "⏱️ Loop stalls" no Diagnóstico**: total das últimas 24h + últimos
  registos (hora · duração · evento loop vs fase) com o limiar e o intervalo
  do monitor; sem episódios mostra "monitor ativo" (confirmação de que o
  vigia está a correr).
- **Bounded**: retenção de 14 dias (`purge_old_loop_stalls`) no purge
  periódico do watchdog (~6h) e no arranque, e **rate-limit por tipo**
  (`LOOP_STALL_MIN_GAP_SECONDS`, 30s entre registos do mesmo kind+phase) —
  um padrão crónico (ex.: snapshot.build a 4-9s por tick) mostra-se no
  Diagnóstico (1 registo/30s) sem encher a BD. Read-only para execução:
  nunca decide nem envia ordens.
- testes: 584.

### 2026-09-04 — A/B com dimensionamento por lane no Diagnóstico + event loop blindado (snapshot cacheado, worker/subprocessos fora do loop, boot rápido, healthcheck com grace de boot)

- **Diagnóstico "⚖️ A/B lanes" mostra o dimensionamento de CADA conta**: o
  `GET /api/lanes/ab` junta `main.sizing`/`tp2.sizing` (risco base
  `risk_pct:<lane>`, lote fixo `lots_override:<lane>`, lots/perna, total e
  nº de pernas do preview) — a comparação de P&L lê com os tamanhos de
  posição em contexto (ex.: main 1.0% vs tp2 0.6% · fixo 0.04 l/perna).
  Read-only; o cálculo corre fora do loop (to_thread).
- **Causa raiz dos "sem resposta HTTP" do healthcheck e dos logins que
  penduram: I/O bloqueante no event loop único.** (1) `/api/status`
  reconstruía o snapshot — leituras ao broker MT5/worker — por REQUEST; o
  market_loop agora constrói o snapshot 1×/s e os endpoints read-only
  servem `BotState.last_snapshot()` (a cache é invalidada nos handlers que
  mutam config/sinal/kill switch para um GET logo a seguir ver o write);
  (2) o Diagnóstico corria `status_summary` do worker + subprocessos
  `netstat`/`schtasks` no próprio handler — `asyncio.to_thread`;
  (3) o watchdog do listener corria o subprocesso PowerShell no loop —
  `to_thread` + timeout 8s.
- **Boot rápido**: o lifespan fazia `initialize` + até 10 retries com
  sleeps (30-40s em que a porta escuta mas o HTTP ainda não serve) — o
  healthcheck via "sem resposta" e MATAVA o servidor a meio do boot
  (restart wars, bind 10048, login/dashboard partidos durante minutos).
  Agora: uma única tentativa por lane, em paralelo e num thread
  (`_initialize_mt5_lane_at_boot`); falhou → o reconnect do market_loop
  (throttle 10s) repete em background já com o servidor a servir.
- **Healthcheck com grace de boot**: um processo que segura a porta mas tem
  <5 min (`HEALTHCHECK_BOOT_GRACE`) nunca é morto (idade via
  Get-CimInstance); as falhas voltam a 1 e a passagem seguinte decide — o
  mesmo para o worker da lane 2.
- testes: 580.

### 2026-09-03 — CAUSA RAIZ dos retcodes null `'?'` na lane 2: kwargs vazio no `order_send` remoto (fix no worker)

- **Sintoma**: desde a ativação real da lane 2 (01/09) NENHUMA ordem
  preenchia — todas as pernas fechavam phantom com retcode `'?'` (resposta
  `null` do módulo), mais um `NO_TICK`, apesar de o worker/terminal/conta/
  AutoTrading estarem saudáveis e de ordens MANUAIS no terminal portable
  preencherem em 43–550 ms.
- **Isolamento empírico** (03/09, conta demo, ordens 0.01 abertas e fechadas
  logo a seguir — BD intacta):
  1. standalone (mesmo módulo/terminal/login) → `order_send` preenche (10009);
  2. worker real e réplica in-process → `None` + `last_error`
     `-2 "Unnamed arguments not allowed"`;
  3. bisect por convenção de chamada no MESMO processo: `mt5.order_send(req)`
     direto → 10009; `fn(*args)` → 10009; **`fn(*args, **{})` (kwargs dict
     vazio) → None `-2`**; `fn(request=req)` → 10013 com request a zeros.
- **Causa raiz**: o wrapper C do `order_send` deste build do módulo
  (MetaTrader5 5.0.6090, cp314) recusa QUALQUER invocação com um dict de
  kwargs presente — mesmo vazio. A conta principal chama `order_send(request)`
  direto no bytecode (sem dict) e preenche; a lane 2 passa SEMPRE por
  HTTP/JSON → `fn(*args, **kwargs)` com `kwargs={}` → `None` → retcode `'?'`
  → phantom. Nenhuma ordem chegava ao journal do terminal (por isso o log do
  terminal não mostrava nada) e o alerta/backfill de retcode null só
  registava o sintoma, nunca a causa.
- **Fix** (`app/lane2_worker.py::_mt5_call_impl`): o dict de kwargs vazio
  nunca mais chega ao módulo — `fn(*args)` quando não há kwargs; kwargs
  reais continuam a ser passados (positions_get/orders_get/order_delete com
  `symbol=`/`ticket=` continuam iguais). Mantém-se também a serialização do
  módulo numa thread única e a captura do `last_error()` quando uma ordem
  devolve `None` — um futuro `None` volta a gravar o motivo real do módulo
  em vez de só `'?'`.
- **Validação**: réplica in-process e worker REAL (8770) passam a preencher
  ordens 0.01 XAUUSD via `/mt5/call` com `kwargs:{}` — retcode 10009 open e
  close. `tests/test_lane2_worker.py`: 24 passed.

---

### 2026-09-03 — Marshaling por convenção (grelha de shapes), auto-sonda no boot do worker e smoke test guiado da lane 2

- **Marshaling POR CONVENÇÃO** (`app/lane2_worker.py`): a allowlist passou a
  grelha `MT5_CALL_SHAPES` (posicionais vs kwargs-only, com os nomes dos
  args) e o normalizador `normalize_mt5_call()` garante que NENHUMA chamada
  com convenção errada chega ao C-core — funções posicionais
  (`order_send`/`symbol_info_tick`/`symbol_info`/...) nunca recebem dict de
  kwargs (kwargs fundidos em args por nome); kwargs-only
  (`positions_get`/`orders_get`/`order_delete`/`history_deals_get`) nunca
  recebem posicionais; formas impossíveis (args a mais, kwargs
  desconhecidos, obrigatórios em falta) são recusadas com erro claro ANTES
  do módulo. O `symbol_info_tick` com kwargs ("takes no keyword
  arguments") que apareceu no /status real também fica bloqueado aqui.
- **Auto-sonda do worker** (boot e cada reconnect): um `order_send` de
  símbolo deliberadamente inválido (`__LANE2_PROBE__`, custo zero — o
  broker recusa com retcode normal) corre UMA vez por episódio de ligação
  na thread do módulo; se devolver `None` com `last_error`, o /status expõe
  `probe: {ok: false, error}` com o erro REAL do módulo e o market_loop
  alerta `risk` (evento `lane2_self_probe_failed`, cooldown 10 min) — um
  módulo/terminal morto deixa de dar phantoms `'?'` silenciosos e é
  apanhado no arranque.
- **Smoke test guiado dual-lane** (`app/smoke.py` +
  `POST /api/lanes/smoke`): abre e fecha a mercado uma trade REAL de 0.01
  (XAUUSD — EURUSD só de fallback: o broker da lane 2 recusa com 10030) em
  CADA conta demo pelas engines reais das duas lanes, com as DUAS trades a
  partilhar o MESMO sinal — escreve as rows (marcadas
  `execution_mode='mt5-smoke'`, `strategy_id='smoke_test'`,
  `exit_reason='smoke_test'` para a analytics nunca as ler como resultado
  de estratégia), emite os eventos `close`/`trade_closed` por lane e dispara
  a notificação WhatsApp dual-lane com as duas contas lado a lado e o P&L
  somado. Se uma lane falhar a abrir, a que já abriu é fechada logo
  (rollback — nunca deixa posição órfã). Guardas demo-only SEMPRE: opt-in
  `LANE2_SMOKE_ALLOW=1` no .env, as DUAS lanes reais (dry_run=False) com
  terminal/worker ligados e AutoTrading ON, contas DEMO obrigatórias
  (`trade_mode == 0` — em real recusa antes de construir o pedido) e
  cooldown (`LANE2_SMOKE_COOLDOWN`, default 120 s). As trades de teste
  ficam marcadas para a analytics nunca as ler como resultado de estratégia.
- **Testes** (`tests/test_lane2_worker.py` + `test_api.py`): regressão que
  repete TODAS as formas de chamada dos engines pelo caminho remoto
  (posicionais sem kwargs / kwargs-only com kwargs / recusas sem tocar no
  módulo), sonda ok/falha/reconnect + espelho no RemoteProvider, guardas do
  smoke (conta real recusa, dry-run/offline/AT-off/cooldown) e E2E do round
  trip DUAL (engine main em processo + worker remoto) com rollback quando
  a 2ª lane falha a abrir e notificação WhatsApp dual-lane com as duas
  trades reais.
- **Prova AO VIVO** (03/09, contas demo 5054644473 + 5055369549,
  `LANE2_SMOKE_ALLOW=1`): round trips XAUUSD 0.01 pelas engines reais —
  tickets 58300164867/… (lane 2) + 58300238944/…, P&L por corrida −0.18 a
  −0.34 € por conta (spread+slippage), WhatsApp dual-lane enviado UMA vez
  por corrida (sent_count 10→14 no bridge, grupo
  120363409845259131@g.us). Descobertas ao vivo:
  (1) o wrapper do módulo recusa comments >28 chars (`-2 Invalid "comment"
  argument` → None → `'?'`) — os ids do smoke passaram a ser curtos
  (`t-<epoch>-<rand>`, comment `:legN` ≤ 28; a 2ª lane leva sufixo `-tp2`);
  (2) o broker da lane 2 recusa EURUSD com 10030 (trade disabled) apesar
  de ter tick — XAUUSD ficou em primeiro na preferência; (3) o
  `leftover_open` do report lia o provider com cache de 2s (posição acabada
  de fechar ainda aparecia): a verificação passou a ler `positions_get`
  direto do módulo (sem cache).
- **Lote fixo por perna na 2ª conta** (`POST /api/config`,
  `lots_override_tp2`): quando definido (ex.: 0.04), cada perna da lane 2
  abre SEMPRE nesse lote em vez do dimensionamento por risco — o
  `risk_pct:tp2` deixa de variar o lote com a distância ao SL (a última
  trade da lane tinha saído a 0.06/perna). Persistido em
  `lots_override:tp2` (0 = volta ao risco); o risk_preview e o snapshot da
  lane mostram o lote fixo (`lots_override` na preview,
  `fixed_lots_per_leg` na entrada de lane). A main fica inalterada.
- **Config por lane sobrevive a restarts**: `risk_pct:tp2`,
  `lots_override:tp2` e `peak_balance:tp2` eram persistidos mas NÃO eram
  recarregados no boot (`_load_config` só restaurava chaves do
  `DEFAULT_CONFIG` + `peak_balance:tp2`) — cada restart da lane 2 caía
  silenciosamente no `risk_pct` legado e voltava ao dimensionamento por
  risco (0.06/perna em vez de 0.04). `_load_config` restaura agora todas
  as chaves por lane persistidas; `/api/lanes/status` expõe também
  `effective_risk_pct`/`risk_base_pct`/`fixed_lots_per_leg`/`risk_preview`
  por lane (antes só no snapshot de `/api/status`).

---

### 2026-09-02 — Lane 2 troca para a demo 10k (5055369549) com `tp2_protect`

- **Nova conta na lane `tp2`**: a 2ª conta demo passou de 111495351
  (`nova_gestao`) para **5055369549 (saldo 10.000 €)** com a **mesma
  estratégia da principal — `tp2_protect`** (config persistida
  `lane2_strategy`+`lane2_label` via `POST /api/config`; env `MT5_2_LOGIN`/
  `MT5_2_PASSWORD` atualizados no `.env`). O worker da lane 2 foi
  reiniciado para ligar à conta nova — `/api/diagnostics` e `/api/status`
  confirmam login 5055369549, balance 10000.0, `margin_mode=2` (hedged),
  `trade_allowed=true` (AutoTrading ON) e a lane `tp2` com
  `strategy_id=tp2_protect` (2 pernas, saída no TP2 + protect). O A/B das
  lanes passa a comparar a mesma estratégia em contas diferentes.
- A conta foi adicionada pelo utilizador ao terminal portable
  (`C:\MT5Portable2`) antes da troca; a conta antiga (111495351) fica
  disponível no terminal mas deixa de ser usada pela lane.

---

### 2026-09-02 — Worker outages no /auditoria (fita + linha temporal) e retenção bounded de exceções (530)

- **Fita de replay inclui as marcas de worker outage**: a exportação
  `GET /api/operations/replay/tape` transporta os `pending_delete_stuck` e
  os `pending_order_result` (action=delete) como eventos passivos (kind e
  payload intactos) — o replay ignora-os como observação, nunca os converte
  em ordens.
- **Linha temporal de worker outages** (`GET /api/operations/replay/outages?days=30`,
  read-only, `worker_outage_timeline` em evidence.py): junta as falhas de
  delete com as marcas de escalada/re-alerta do watchdog, cronologicamente,
  e agrupa **episódios por (trade, ticket)** — uma ordem pendente que o bot
  tentou cancelar e não conseguiu. Cada episódio expõe falhas, escaladas,
  re-alertas, tentativas máximas, intenção (TTL/gestão), lane/perna (via o
  ticket da marca) e **resolução derivada do journal** (delete aceite ou
  fill da perna depois da 1ª falha; sem isso fica aberto, ou a resolução
  não foi journaled). O /auditoria ganhou o bloco "⏳ Worker outages ·
  delete de ordens pendentes" (stats + timeline + episódios), no ciclo de
  60s da página.
- **Retenção bounded de `operational_exceptions`**: o rate-limit por
  episódio já impedia centenas de linhas idênticas, mas episódios novos
  continuavam a crescer — `db.purge_old_exceptions(365)` (política
  "archive after 365 days" de maintenance.py) apaga linhas com mais de
  365 dias no purge do watchdog (~6h) e no arranque; a tabela nunca cresce
  sem limite.
- **Testes**: a fita exporta as marcas com kind/payload intactos; o
  relatório agrupa 2 episódios (resolvido por delete ok vs aberto), conta
  escaladas/re-alertas/falhas e atribui lane/perna às falhas; purge apaga
  só as exceções antigas; frontend com o bloco/renderer/fetch do /auditoria.

### 2026-09-02 — Watchdog de pendentes persistido na BD, exceções rate-limited e contexto completo no journal (527)

- **Estado do watchdog PERSISTIDO** (nova tabela `pending_delete_states`,
  PK lane+position_id): tentativas, intenção do delete (TTL/gestão com
  reason/msg_id) e timestamps de alerta (`escalated_at`/`last_alert_utc`)
  sobrevivem a um restart do servidor. O motor restaura o estado na
  inicialização (o Diagnóstico continua a mostrar as pernas presas) e a
  intenção restaurada completa o cancelamento de gestão com a RAZÃO
  original após o reboot — nunca rebaixada a expiração TTL. Uma perna já
  escalonada antes do restart volta a alertar no 1º retry pós-reboot (o
  monotónico não sobrevive; `escalated_at` é o veredicto durável, com
  fallback independente do limiar da sessão). Linhas órfãs são limpas: por
  perna no `_check_pending_real` (deixou de estar `pending_fill`) e por
  trade no `update()` (trade encerrada) — a tabela nunca acumula lixo.
- **Exceções rate-limited por episódio**: enquanto a perna presa falha a
  cada segundo, só a PRIMEIRA falha do episódio grava
  `operational_exceptions` (contexto com `episode: true`; orders_get e
  order_delete — TTL e cancelamento de gestão — partilham o marcador por
  perna) — uma exceção concisa com trace em vez de centenas de linhas
  idênticas. O marcador reseta quando o retry acaba (delete com sucesso ou
  perna deixa de estar pendente); um episódio novo volta a gravar.
- **`pending_delete_stuck` com contexto completo no journal**: cada marca
  de escalada/re-alerta entra na fita (opt-in) com leg, position_id,
  ticket, lane, tentativas, kind/razão da intenção, `escalated`/`re_alert`
  e timestamps — com os `pending_order_result` falhados entre marcas, o
  replay reproduz uma falha prolongada do worker de forma determinística
  (a escalada e a cadência de 30 min ficam explícitas na fita).
- **Diagnóstico**: `stuck_pending_legs()` expõe ainda `intent_kind` e
  `last_alert_utc`; o bloco "⏳ Ordens pendentes em retry de delete" mostra
  o tipo de delete (TTL vs gestão) e a hora do último alerta.
- **Testes**: estado restaurado após restart (tentativas/escalonada/
  intenção TTL), re-alerta no 1º retry pós-reboot, recuperação com a razão
  original e BD sem linhas órfãs; rate-limit (2 linhas por 24 falhas;
  episódio novo volta a gravar); journal com contexto completo + marcas de
  re-alerta.

### 2026-09-02 — Watchdog de pendentes com re-alerta, retry imediato ao worker voltar e bloco no Diagnóstico (524)

- **Contagem por perna com contexto**: `_pending_delete_failures` passou a
  guardar leg/trade/símbolo/ticket/abertura por perna (não só a contagem) —
  serviu para o Diagnóstico sem consultas extra.
- **Re-alerta periódico**: `pending_delete_stuck` sai no limiar (5 falhas)
  e volta a sair a cada `PENDING_DELETE_REALERT_EVERY` (30 min) enquanto a
  perna continuar presa (worker em baixo durante horas nunca fica mudo); o
  relógio é o do motor (`self.clock.monotonic()`), determinístico nos testes.
- **Auto-recover sem esperar o TTL**: a intenção do delete falhado fica
  em `_pending_delete_intent` (kind ttl ou cancel com reason/msg_id). O
  tick re-tenta o `order_delete` em CADA update enquanto houver intenção —
  quando o worker volta, o cancelamento de gestão pendente é completado no
  mesmo tick (evento `close` com a razão/message original, `_maybe_close_trade`
  fecha a trade) e o TTL idem (reason `pending_expired`). Sem intenção nem
  TTL vencido nada muda (o fluxo normal fica igual).
- **Diagnóstico: bloco "⏳ Ordens pendentes em retry de delete"** —
  `GET /api/diagnostics` expõe `stuck_pending.main/tp2` (tentativas,
  escalonada?, ticket, idade, trade; vazio = nada preso) e a página mostra-o
  no ciclo de 10s, a vermelho quando escalonado.
- **Testes**: re-alerta periódico (uma por episódio e por intervalo),
  auto-recover de cancelamento de gestão num tick sem TTL, `stuck_pending_legs()`
  com estado/tentativas e esvaziamento após recuperação; `/api/diagnostics`
  valida a forma do bloco.

### 2026-09-02 — Delete de pendentes com retry por perna, watchdog de stuck e backfill semanal com evidência (521)

- **order_delete com tratamento por perna no caminho da expiração**
  (`_check_pending_real` TTL + `_cancel_pending_pos`): uma falha do delete
  (worker da lane caiu) já não fecha a perna na BD — antes o `try/except:
  pass` fechava a posição e deixava a ordem pendente viva no MT5 (órfã,
  podia preencher depois e virar posição fantasma). Agora grava a exceção
  (`record_exception` com leg/lane/ticket), jornala `pending_order_result`
  (delete, accepted=False, REMOTE_ERROR), mantém a perna `pending_fill` e o
  próximo tick repete o cancelamento; o delete aceite jornala accepted=True.
  `_cancel_pending_pos` devolve `None` sem fechar quando o delete falha
  (`close_trade_at_market` ignora o evento em falta).
- **Watchdog `pending_delete_stuck`**: após `PENDING_DELETE_ESCALATE_AFTER`
  (5) falhas CONSECUTIVAS de delete da mesma perna, o tick devolve o evento
  para o feed/Telegram (alerta risk "⛔ Ordem pendente não cancelada no
  MT5…") — uma vez por episódio, nunca em silêncio; o contador reseta
  quando o delete volta a funcionar ou a perna deixa de estar pendente.
  Os cancelamentos de gestão (`close_all`, etc.) partilham a mesma contagem.
- **Backfill semanal automático**: o tick ganhou o guard `_check_refusals_backfill`
  (ISO-week, config `refusals_backfill_week_last_run`) que re-corre o
  backfill uma vez por semana com `--apply` — nenhum fecho phantom futuro
  fica sem trace durável mesmo que o alerta da altura tenha sido perdido;
  notifica só quando encontrou algo para gravar (sinal de alerta perdido).
- **Evidência sem journal no backfill**: sem `execution_events`, o backfill
  mina agora as notificações risk/system da hora (±15 min) e a auditoria de
  ignoradas — um `10027` liga `auto_trading_off=1` e compõe o erro
  "AutoTrading desligado…" (como o motor ao vivo), com o texto citado da
  notificação/ignorada no trace para o auditor.
- **Testes**: delete TTL/cancel com falha mantém a perna pendente e re-tenta;
  watchdog escalona uma vez por episódio e reseta; backfill com journal
  vazio mina notificações/ignoradas e liga a flag 10027.

### 2026-09-02 — Retcodes reais nas recusas remotas + glossário no Diagnóstico + A/B com trace (516)

- **Porque é que 01/09 mostrou `'?'`**: o journal guardou os 12
  `order_result` com `accepted=False` mas `retcode=None` — o resultado do
  envio remoto da altura não trazia retcode legível (código da altura não
  recuperável; o caminho worker→proxy de hoje já o carrega — provado pelo
  roundtrip e pelos testes novos).
- **Hardening do envio perna-a-perna** (`_send_open_orders`/`_send_pending_orders`):
  exceção no `order_send` (worker caiu a meio do envio) já não desenrola o
  `open_trade` — antes deixava a trade MEIA-ABERTA na BD; agora a perna
  fecha `order_rejected` com retcode `REMOTE_ERROR` e o texto real da
  exceção entra no `trade.error` (nunca se perde o motivo). Retcodes
  ilegíveis continuam `?` (não se inventam); recusas reais (10027, …)
  chegam como números.
- **Testes de regressão 01/09**: recusa remota 10027 fim-a-fim (worker →
  RemoteProvider → MT5Engine) com trace gravado na BD, e worker a levantar
  no envio sem trade meia-aberta.
- **A/B voids com trace**: cada fecho phantom do relatório das lanes passa
  a expor `*_error`/`*_retcodes`/`*_auto_trading_off` da lane void — o
  porquê do phantom sai do próprio relatório (ver /api/lanes/ab).
- **Glossário de retcodes no Diagnóstico**: o bloco ⛔ traduz cada retcode
  (10027 AutoTrading OFF, 10014 fundos, 10018 ordem inválida, …) e marca
  AutoTrading OFF a vermelho; desconhecidos mostram-se em cru. O cartão A/B
  mostra main/tp2 phantom e remete para o bloco ⛔.

### 2026-09-02 — Backfill do trace de recusas + bloco "recusas do broker" no Diagnóstico (514)

- **Backfill das 3 trades phantom de 01/09** (`app/backfill_trade_refusals.py`,
  padrão report/`--apply` dos outros backfills): com as colunas
  `error`/`retcodes`/`auto_trading_off` agora existentes, as trades tp2
  recusadas do dia (t-d1363896494e, t-dd05e88c0bd6, t-b92a129d2cdb — 4
  pernas cada) ficaram com o trace durável gravado a partir da evidência:
  os eventos `order_result` do journal (accepted=False em todas) + as
  notificações risk da hora → `retcodes ['?','?','?','?']` (o retcode não
  era legível no worker à data — nunca se inventa o número) e
  `auto_trading_off=0`. O script volta a correr a qualquer altura (pega
  futuras trades phantom sem trace).
- **`GET /api/trades/refusals`** (`db.refused_trades`): últimas recusas do
  broker — trades com `error` gravado OU assinatura phantom (fechadas sem
  `exit_reason` a 0.0), mais recentes primeiro, com retcodes/flag
  AutoTrading OFF; inclui legados pré-colunas (error null, visíveis na
  mesma). Read-only.
- **Diagnóstico: bloco "⛔ Últimas recusas do broker"** — cartão com as
  últimas 5 recusas (lane, hora, símbolo, retcodes, erro), refrescado no
  ciclo de 10s sem se duplicar.

### 2026-09-02 — Alerta de fechos phantom, trace durável de recusas e A/B em viva com o feed (513)

- **Alerta risk distinto para fechos phantom**: quando uma lane fecha a
  trade a 0.0 SEM `exit_reason` (ordens recusadas pelo broker no envio,
  cleanup `sent==0`) o `_try_execute` emite agora um alerta próprio
  ("👻 Lane X: sinal Y SEM fill real…") com os retcodes — antes caía no
  alerta genérico com `retcodes ['?']` e só o A/B (posterior) mostrava a
  anomalia. AutoTrading OFF (10027) mantém o alerta específico existente.
- **Trace durável das recusas**: `trades` ganhou as colunas `error`,
  `retcodes` (JSON) e `auto_trading_off` (migração automática no arranque);
  `_send_open_orders` persiste-as via `db.set_trade_refusal` — antes viviam
  só em memória e a auditoria/replay não conseguia explicar o fecho.
- **Diagnóstico: A/B a refrescar com o feed**: o cartão "⚖️ A/B lanes"
  agora acompanha o ciclo de 10s do Diagnóstico (substituído, nunca
  duplicado) — a amostra atualiza sozinha sem refresh manual.
- **Flakes de calendário/ordem eliminados**: `test_observability` usava
  dias relativos que cruzavam o fim de mês (agregação mensal separava as
  amostras XAUUSD a 01/09) → stamps no mês anterior; o dia 6 do
  `test_e2e_week` dependia do realizado `tp2` acumulado por outros testes
  → `_realized_today` fixado; o lote do teste de offset diário contava
  flips de recounts de outros testes → asserções escopadas ao id do
  recount. Suite: **513 recolhidos · 512 passed + 1 skipped, 2 corridas
  consecutivas verdes sem deselect**.

### 2026-09-01 — Corrida paper focada nas estratégias em validação (509)

- **Corrida normal reduzida**: depois de as 6 estratégias perdedoras terem
  saído, a corrida paper passou a abrir **só o que está em validação** — as
  5 com menu (`nova_gestao`, `nova_gestao_fallback`, `tp2_protect`,
  `auto_escada_3`, `auto_escada_3_hibrida`) — mais a `nova`, mantida
  **silenciosamente** como contrafactual do `counterfactual_pnl` (abre
  papel mas não aparece nos cards da corrida nem no feed). As referências
  de backtest `auto_3`/`auto_5`/`auto_escada_5` deixaram de abrir papel
  normal — continuam no dropdown, no backtest/compare e no **shadow**
  (a `auto_escada_5` mantém a evidência do gate forward pelo
  `shadow_paper_strategies`, com marcadores `origin=shadow` isolados).
  `paper_strategy_ids()` (em `app/strategies.py`) é a fonte única da
  corrida; o feed conta só as visíveis (`strategy_lifecycle.menu`).
- **Guard `protect_latency`**: o endpoint validava o id só com
  `engine=backtest`; agora `get_strategy()` corre sempre — id removido
  devolve 400 em vez de 200 vazio (regra de estudos em voz alta).
- **Testes**: novo `test_removed_strategy_studies_fail_loudly` (estudos
  com ids removidos devolvem 400/ValueError, nunca rodam com a principal);
  helper comum dos testes de papel filtra `origin="race"` (o shadow
  partilha a tabela `paper_trades` — sem o filtro, uma shadow trade da
  `auto_escada_5` contaminava as asserções). Suite: **509 recolhidos**
  (507 passed + 1 skipped + 1 deselected — o flaky e2e da semana).

---

### 2026-09-01 — Estratégias removidas: apagadas do registo (remoção completa) (508)

- **Decisão do utilizador**: `tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`,
  `auto_protect_5` e `auto_escada_3_struggle` (R4) foram **apagadas do
  registo** (`app/strategies.py`) — deixam de aparecer no `/api/strategies`,
  no menu, no HELP, nas páginas de teste, no paper e no shadow.
- **O que deixa de ser reproduzível (documentado)**: o backtest/compare
  histórico destas 6 variantes (as páginas de teste e os estudos que as
  usassem passam a falhar com ValueError; os `sweep_auto`/`sweep_atr`
  deixaram de listar `auto_2`). O registo decisório (PF/DD e o porquê de
  cada remoção — 19/08 e 20/08) fica no CHANGELOG e nos capítulos
  históricos do ESTRATEGIAS.md.
- **Guardas**: `get_strategy()` levanta ValueError para ids desconhecidos
  (nenhum estudo corre silenciosamente com a estratégia principal); os
  endpoints `/api/analysis`, `/month`, `/trace`, `/resolution`,
  `/protect_latency`, `/protect_causal` devolvem 400 para ids removidos.
  O default do `/api/analysis/trace` passou de `tp2` para `nova` (o braço
  "Sair no TP2 (puro)" do gráfico mensal da página de teste foi removido).
- **Frontend**: `common.js` (STRATEGY_NAMES/RACE_COLORS) sem ids mortos +
  cores novas para `nova_gestao_fallback` e mantidas as vivas; HELP sem a
  entrada `auto_protect_5`/glossário "Protect precoce".
- **Docs**: AGENTS.md/TECHNICAL.md/PUBLIC.md/README.md/ESTRATEGIAS.md
  atualizados para o menu real (7 visíveis — 5 operacionais + referências
  auto_3/auto_escada_5) e a lista de removidas; os
  dados paper órfãos (ex.: 13 725 balanços da `auto_protect_5`) ficam na
  BD sem leitores (nunca se apagam dados). Mecânicas genéricas
  (`protect_trigger`/`struggle_*`) ficam nos motores para candidatas
  futuras. Testes atualizados (menu/registo/paper/HELP/frontend): 508
  recolhidos.

### 2026-09-01 — WhatsApp: retry no push + bridge espera a socket + bug `re` no tick (508)

- **Diagnóstico do dia**: 3 trades reais fecharam hoje (08:41 +4.04 €, 10:49 +4.63 €,
  12:40 +19.74 €) e o push WhatsApp foi tentado em todas; só a das 12:40 foi entregue
  (`sent_count` 1 no bridge). As 08:41/10:49 falharam com **500/503** — o bridge estava
  num **loop permanente de religação** (conflito de sessão `conflict replaced` ~1000×/h
  desde o arranque de 23:57, janelas de conectividade de ~1-3s) e o `send()` não tinha
  retry → mensagens perdidas em silêncio (só registadas em `operational_exceptions`).
- **Fix server** (`app/notify/whatsapp.py`): `send()` faz **3 tentativas com backoff
  curto (2s/4s)** — uma falha transitória 5xx/ligação deixa de perder a mensagem; a
  exceção só é registada na BD no fim (1 linha, não 3). Novo teste
  `test_send_retries_transient_bridge_errors`.
- **Fix bridge** (`whatsapp/bridge.js`): `/send` **espera até 8s** pela socket voltar a
  estar `open` (antes devolvia 503 instantâneo na janela de religação) e faz 1 retry
  interno quando a socket morre a meio do envio (`Connection Closed`).
- **Fix bug de tick** (`app/lanes.py`): faltava `import re` → `NameError` em
  `_mt5_positions` (93 ocorrências registadas hoje) que rebentava o fim de **cada**
  tick (o broadcast de snapshot nunca acontecia via WS).
- **Reenvio manual (pedido do utilizador)**: as 3 mensagens de hoje foram reenviadas
  ao grupo com o formato de produção (`format_trade_close`) — entregues à 1ª tentativa
  após o restart do bridge. ⚠️ Incluindo a das 12:40 (+19.74 €): o bridge tinha
  registado esse envio como `sent` (limitiu um falso positivo — a socket morreu a meio
  da entrega), mas o utilizador confirmou que NÃO chegou ao grupo; reenviada e
  confirmada (`sent_count` 3 no bridge). Lição: um ack do `sendMessage` do Baileys não
  garante entrega quando a sessão está em loop de religação — o fix duradouro é
  re-parear a sessão (apagar `whatsapp/session/` + QR novo).
- **Estado**: servidor reiniciado com o código novo (main 5054644473 · 1032.30 €,
  lane 2 worker ligado, AutoTrading ON); bridge estabilizado após o restart (0 conflitos
  desde 20:04 UTC). Se o loop voltar, re-parear: apagar `whatsapp/session/` + QR novo.

- **AutoTrading do terminal portable LIGADO a 01/09** — clique automatizado
  no último botão da toolbar Standard ("Algo Trading") via `GetWindowRect`+
  clique real de rato; validado por `terminal_info().trade_allowed = True`
  através do worker (`/mt5/call`). Continua **por-sessão** (reativar após
  reboot — como no terminal 1).
- **`/status` do worker expõe `trade_allowed`** (via `terminal_info`) e o
  `RemoteProvider` espelha-o (`trade_allowed` + `at_off` em
  `status_summary`) — o **Diagnóstico mostra ON/OFF** no bloco "Lane 2 ·
  worker MT5" e o market_loop alerta `risk` automaticamente quando o worker
  está ligado mas o AutoTrading está OFF (cooldown 10 min, evento
  `lane2_auto_trading_off`; `lane2_auto_trading_on` quando volta).
- **A/B das lanes (read-only)**: `GET /api/lanes/ab` (`lane_ab_report` em
  `app/lanes.py`) compara trades fechadas do mesmo sinal nas duas lanes
  (P&L lado a lado, win rate, profit factor, delta total) e **só sugere
  manutenção com ≥20 sinais comparáveis** (`min_comparable`); abaixo disso o
  veredicto é "amostra insuficiente" (neutro). Bloco "⚖️ A/B lanes" no
  Diagnóstico (fetch uma vez por página, refresh no botão Atualizar). Nunca
  altera config nem envia ordens.
- **Fechos phantom fora do A/B (02/09)**: `lane_ab_report` exclui do
  comparável trades fechadas **sem `exit_reason`** (o cleanup de ordens
  recusadas grava 0.0 sem razão — ex.: AutoTrading OFF) e expõe-nas em
  `voids`/`void_count` com flags por lane (`main_void`/`tp2_void`); com
  n=0 e voids, o veredicto explica a amostra em vez de contar 0.0 como
  resultado. Com a amostra real de 02/09 (as 3 trades tp2 de 01/09 eram
  phantom — o AutoTrading do terminal 2 só foi ligado nessa noite), o
  **limiar de manutenção mantém-se em 20** — sem fills reais não há base
  para o baixar; o Diagnóstico mostra "⚠️ N fecho(s) phantom sem fills
  reais". Diagnóstico e teste: a amostra real explica o n=0 sem aparecer
  como perda/ganho de nenhuma estratégia.
- **Flaky eliminado**: `test_e2e_week_autonomous_no_intervention` backdatava
  o sinal do dia 7 para 2026-08-01 (fixo) — quando o calendário passou os
  30 dias da janela do relatório de rejeições, o sinal desaparecia do
  `by_id` (KeyError). Agora backdata 2 dias atrás (relativo); a suite corre
  **100% verde sem deselect** (511 tests).
- Testes: `trade_allowed` no worker (ON e OFF), espelho no RemoteProvider
  (incl. `at_off` e broker caído), 3 cenários do A/B (amostra <20 sem
  sugestão; 20+ com tp2 à frente → sugestão; 20+ sem diferença → neutro)
  + 2 novos: phantom excluído do comparável e só-phantoms a explicar a
  amostra vazia.

### 2026-09-01 — Ativação da lane 2 real (worker + terminal portable) (501)

- Instalado o **2º terminal MetaTrader 5 portable** em `C:\MT5Portable2`
  (clone da instalação existente; `terminal64.exe` próprio e data folder
  separada `991DCC…` — a API chama-o com `path=…` + `portable=True`). O
  instalador oficial atual (`download.terminal.free/cdn/web/metaquotes.ltd/…`)
  ignora `/portable` (GUI) — a via suportada é a cópia da instalação.
- `.env` (backup `.env.bak-20260901`): `MT5_2_TERMINAL_PATH=C:\MT5Portable2\
  terminal64.exe`, `LANE2_WORKER_ENABLED=1`, `MT5_2_DRY_RUN=0`,
  `LANE2_WORKER_PORT=8770`. Verificado: a API liga à clone e faz login na
  **111495351 · 100.000 € · margin_mode 2 (hedged)** sem tocar na conta da
  principal (5054644473 confirmada intacta).
- **Produção**: worker no ar (8770) e servidor reiniciado com o novo `.env`
  → `/api/status` mostra `tp2 = nova_gestao · mt5-real · 111495351` ligada
  ao broker através do `RemoteProvider`; `main` intacta. O A/B das duas
  estratégias corre agora com fills reais do broker nas duas contas demo.
- ⚠️ **Ação manual pendente**: ligar o **AutoTrading (Algo Trading)** na janela
  do 2º MetaTrader 5 (portable). É por-sessão (reativar após reboot, como no
  terminal 1); com o botão OFF as ordens da lane 2 são recusadas com retcode
  10027 + alerta (fail-closed, nada muda nas contas).

### 2026-09-01 — Worker isolado da lane 2 (execução real da 2ª conta MT5) (500)

- **`app/lane2_worker.py`** — processo separado dono da ligação MetaTrader5
  à 2ª conta (terminal **portable**, `MT5_2_TERMINAL_PATH`), shim de I/O por
  HTTP local (`/ping /status /initialize /account /positions /quote /meta
  /copy_rates /constants /symbol_select /mt5/call` + allowlist `/mt5/call`
  para o módulo MT5 bruto), token (`LANE2_WORKER_TOKEN`), logs rotativos
  (`configure_process_stdio("lane2_worker")`). **Nunca toca na SQLite nem
  decide nada** — a invariante "todo o estado vive no event loop" mantém-se.
- **`app/execution/remote2.py`** — `RemoteProvider` com a mesma superfície
  duck-typed do `MT5Provider` (incluindo `provider._mt5`, o proxy do módulo
  MT5 com constantes + chamadas serializadas em JSON) ligado pelo `MT5Engine`
  da lane 2 sem alterar o engine. `main.py`: com `LANE2_WORKER_ENABLED=1` a
  lane 2 usa o provider remoto (`dry_run=False`); sem ele mantém o dry-run
  antigo. Só ativo com o env — a produção fica dry-run até o utilizador
  instalar o 2º terminal portable.
- **Supervisão**: `start.ps1`/`boot.ps1` arrancam o worker idempotentes por
  porta (guardiam `MT5_2_TERMINAL_PATH`); `healthcheck.py` ganhou o alvo
  lane-2 (`/ping`, 2 falhas + cooldown, chaves `worker_*`) e reinicia-o;
  o market_loop pinga-o ~1×/s e notifica no feed/Telegram as transições
  online/offline (`lane2_worker_offline`/`online`); o **Diagnóstico** tem o
  bloco "Lane 2 · worker MT5" (reachable/connected/login/last_sync/último
  erro/cauda do log + estado desligado em dry-run).
- ⚠️ **Incidente corrigido com guard rígido**: durante a validação, o worker
  sem `MT5_2_TERMINAL_PATH` fez `initialize(login=111495351)` no terminal por
  omissão e **trocou a conta do terminal da principal** (produção ficou a
  apontar para a 111495351; relogada para 5054644473/1003.89 €). O worker
  agora **recusa arrancar (exit 2)** sem um `MT5_2_TERMINAL_PATH` existente
  — teste de regressão `test_worker_recusa_arrancar_sem_terminal_portable`.
- **Testes**: `tests/test_lane2_worker.py` (9) — worker HTTP em memória com
  provider fake (auth, initialize/status/account, constants, allowlist,
  JSON-safe), roundtrip do `RemoteProvider` + proxy `_mt5` (constantes,
  `order_send`/`positions_get` com retcodes por atributo), fail-closed com
  worker morto (`open_trade` real recusa) e o guard do path. Suite **500**
  (492 → +8).

### 2026-09-01 — 2ª conta MT5 (111495351) passa a nova_gestao (492)

- A lane `tp2` (conta demo **111495351**, env `MT5_2_*`) passou de
  `tp2_protect` para **`nova_gestao`** (TP1..TP5 + gestão) por decisão do
  utilizador — config persistida (`lane2_strategy`/`lane2_label` via
  `POST /api/config`, validada contra o registo; `auto_escada_3/5` continuam
  bloqueadas na lane MT5 pelo gate da Fase 3). A principal fica em
  `tp2_protect` → as duas lanes passam a dar A/B do mesmo sinal.
- Estado da lane 2: **dry-run** (`mt5-dry`) — a API MetaTrader5 mantém uma
  ligação global por processo e a execução real da segunda conta precisa de
  um **worker isolado** (segundo processo Python com terminal portable via
  `MT5_2_TERMINAL_PATH`); sem esse worker, a lane 2 fica falha-fechada
  (`execution_block_reason` no arranque).

### 2026-09-01 — Faixa de preços do topo removida (perf) (492)

- A **faixa de preços** (GOLD..NZDUSD na linha 2 da topbar) foi removida
  por completo a pedido do utilizador: sem DOM row, sem `renderPriceStrip()`/
  `TICKER_LABEL`/`_tickPrev` no `common.js` e sem o CSS `.price-chip`/
  `.pchg`/`.pbar`/`.tb-row2` (aguardava rebuild em cada movimento de preço).
  O topbar fica estático (só changem relógio/badges/flashes reais) e a
  altura do header encolhe ~20px — `--hdr-h` já é medido ao vivo.
- Os flags `market.prices`/`changes` continuam no snapshot (o badge de
  market "preços reais"/"simulação" usa `real_data`); nenhum endpoint ou
  motor foi tocado — é só visualização. Contract test atualizado:
  `test_render_guards_previnem_rebuilds_sem_mudanca` garante que a faixa
  NÃO volta e que os restantes guards (flash de saldo, feed, tab ativa)
  permanecem.

### 2026-09-01 — Tema escuro completo (toggle persistido) + ajuste da topbar (492)

- **Tema escuro** como camada `[data-theme=dark]` sobre as variáveis do
  `style.css` (superfícies/tons escuros, semântica adaptada ao dark,
  `color-scheme: dark` para controlos nativos). Nenhum hex hardcoded fora
  das variáveis — a striping das tabelas (`--stripe`) passou a variável
  para não ficar branca no dark.
- **Toggle ☾/☀ na barra superior** (comum a todas as páginas, em
  `common.js`): alterna o atributo no `<html>`, persiste em
  `localStorage.nx_theme` e sincroniza o ícone/aria-pressed. O tema é
  aplicado **antes do paint** por um snippet no `<head>` de cada página
  (sem flash), com default de `prefers-color-scheme` quando o utilizador
  ainda não escolheu.
- **Gráficos canvas adaptativos**: `chartPalette()` em `common.js` fornece
  grelha/eixo/fundo por tema; todas as funções de desenho (equity, corrida
  paper, daily, recounts, trace, walk-forward, uptime, fills-div) usam-na —
  as cores de série (verde/vermelho/azul vividos) são iguais nos dois.
- **Topbar mais robusta**: o bloco de badges encolhe com `overflow:hidden`
  (nunca parte o layout com badges longas de lanes) e `online-users` cai no
  breakpoint 1440px — verificado sem overflow horizontal a 1440px.
- **Login** também respeita o tema (snippet + overrides inline). QR do
  WhatsApp mantém fundo branco (legibilidade de scanning).
- **Segue o SO ao vivo**: sem escolha explícita (sem `nx_theme` no storage),
  o tema acompanha o `prefers-color-scheme` do sistema em tempo real
  (`matchMedia` `change` listener). Uma escolha no botão fica persistida e
  o SO deixa de interferir; o arranque do chrome NÃO grava o tema (gravar
  mataria o follow).
- Contrato novo em `tests/test_frontend.py` (toggle + camada dark + paleta
  nos charts + snippet no head de todas as páginas + follow do SO com guard
  de escolha explícita). Suite: **492** (falha e2e pré-existente inalterada).

### 2026-09-01 — Health check no Diagnóstico, teste do recovery e guards de render (491)

- **NexGen Health no /diagnostico**: `/api/diagnostics` passa a expor
  `healthcheck` (estado do ficheiro `healthcheck.state`, cauda do log e a
  Tarefa Agendada via `schtasks`, cacheada 60s porque a página faz polling a
  cada 10s). Novo bloco na tab Diagnóstico: última corrida/resultado da
  tarefa, servidor online/offline segundo o check, falhas consecutivas,
  último restart automático e cauda do log — lado a lado do MT5/listener.
  Tolerante a ausência (servidor recém-arrancado, schtasks a falhar).
- **Teste do recovery flow** (`tests/test_healthcheck.py`, 6 testes): corre
  a lógica real do `healthcheck.py` contra uma porta isolada SEM servidor e
  controla só o restart com monkeypatch — 1 falha não age, 2 falhas
  consecutivas alertam+reiniciam, cooldown entre restarts, recuperação limpa
  o estado e notifica, `--no-restart` nunca reinicia, processo encravado na
  porta é terminado antes de subir o novo.
- **Guards de render auditados e cobertos**: a faixa de preços era
  reconstruída (`innerHTML=`) em CADA snapshot mesmo com preços parados —
  agora tem guard de assinatura (`contentChanged("pricestrip", …)`) como as
  tabelas; o flash do saldo já era condicional e ficou verificado (só
  quando o saldo muda, nunca na 1ª leitura). Contrato novo em
  `test_frontend.py::test_render_guards_previnem_rebuilds_sem_mudanca`.
- Suite: 491 passes (6 novos + 1 contrato novo; a falha e2e pré-existente
  mantém-se).

---

### 2026-09-01 — QA desktop 1400px+ e health check do servidor (NexGen Health)

- **QA visual a largura de desktop** (1440px) em todas as páginas com o
  agent-browser: corrigido overflow horizontal da barra superior (a faixa de
  preços/badges estourava ~30px — o sub-título da marca cai agora ≤1440px);
  paleta de acentos escurecida para contraste AA (badges, tags, preços);
  landmarks HTML (`<main>`/`<footer>`/h1) em todas as páginas; `aria-label`
  no filtro de grupo e no strip de preços; cabeçalho de tabela vazio com
  texto sr-only. axe-core: 0 violações (4 “incomplete” são ícones decorativos
  com gradiente, não texto).
- **`healthcheck.py` + Tarefa Agendada `NexGen Health`** (a cada 2 min):
  verifica `GET /` na porta 8000 (o `/api/status` é pesado e pode demorar
  >5s) com timeout de 15s e **2 falhas consecutivas** antes de agir — um
  servidor saudável mas lento nunca é reiniciado por engano. Quando offline:
  alerta no log rotativo (`healthcheck.log`) + Telegram (se configurado) e
  **reinicia o servidor** com o comando documentado (`server_runner`,
  `MT5_DRY_RUN=0`), terminando antes um processo encravado que segure a
  porta; cooldown de 120s entre restarts (`healthcheck.state`). Modos:
  `--once` (tarefa), `--watch` (loop), `--no-restart` (só alerta).

---

### 2026-09-01 — Redesign profissional claro da dashboard + otimização de render (frente)

- **Tema claro profissional** (estilo trading app): o tema cyberpunk neon foi
  removido por inteiro (cores neon, glows, scanlines, fontes Orbitron/Chakra
  Petch, favicon neon) e substituído por um design system claro: fundo
  `#f6f8fb`, painéis brancos, bordas subtis, verde/vermelho TradingView
  (`#089981`/`#f23645`), azul de acento `#2962ff`, números tabulares. Os
  gráficos canvas (equity, corrida, uptime, recounts) foram re-coloridos
  para o fundo claro.
- **Barra de navegação no TOPO**: as duas colunas laterais fixas (menu
  esquerdo + terminal STATE direito) foram substituídas por uma barra
  superior única (marca + navegação PRINCIPAL/ESTRATÉGIAS/DIAGNÓSTICO/
  AUDITORIA/HELP + badges + relógio + kill switch + sair) com uma faixa de
  preços por baixo — o conteúdo ganha largura total (o `--term-w`/`--term-r`
  e os overlays móveis ☰/▦ das colunas foram removidos; o ☰ abre a
  navegação em dropdown ≤960px). A ordem do conteúdo da principal foi
  reorganizada: status → KPIs → tabs → conteúdo, com os painéis analíticos
  (corrida/promoção/Fase 2/3/narrativa/shadow) depois do conteúdo.
- **Menos animações**: o boot sequence da dashboard e do login (overlay
  com linhas escritas + sons Web Audio) foi removido — a dashboard aparece
  instantaneamente; sons de notificação removidos; glitch/flicker/pulse/
  scanlines/backdrop-filter/hover-translate/glows eliminados. Ficam só
  micro-movimentos funcionais (flash de cor do balanço, sublinhado da tab,
  fade curto dos toasts) e o bloco `prefers-reduced-motion` como rede.
- **Otimização de render (o ganho de performance real)**: o snapshot chega
  ~1×/s e a página reconstruía ~20 secções de DOM por snapshot. Agora: só a
  tab ativa renderiza (`renderSignals`/`renderPositions`/`renderTrades`),
  cada renderer só reconstrói quando o conteúdo mudou (cache de assinatura
  `contentChanged` em common.js), o feed reconstruía em cada tick e agora
  só em mudança (e o `prependFeed` do WS continua incremental), e os
  painéis (promoção/Fase 2/3/shadow/auditoria) têm guards próprios. As tabs
  ocultas mantêm o DOM e são re-renderizadas ao voltar. No servidor, o
  snapshot passou a enviar 30 notificações em vez de 50.
- **Polimento profissional**: emojis decorativos removidos do chrome
  (tabs, labels dos KPIs, títulos, botões, badges); mantidos apenas no
  conteúdo do feed e nos estados semânticos. Login redesenhado (cartão
  limpo, sem glitch/boot/scanlines). Favicons atualizados para o N azul
  sobre claro.
- Suite: 484 passed (frente — nenhuma mudança de comportamento no backend
  além do cap de notificações do snapshot).

---

### 2026-08-28 — Contador 'resolvidos' persistido no /auditoria, lembrete do cap e lote semanal do histórico completo (470)

- **Bloco persistido de 'resolvidos' no /auditoria**: o re-check em lote dos
  recounts grava agora cada corrida em `recount_batch_runs` (nova tabela,
  read-only) com o contador 'resolvidos' + a lista de flips (data, recount,
  de → para, offset). A página mostra os stats do último lote (janela
  diária/semanal, re-corridos, alterados, resolvidos, erros) e o histórico
  de flips por corrida para ver a evolução das correções do offset GC=F↔spot
  ao longo do tempo. Nova rota `GET /api/recounts/batches`.
- **Lembrete no feed para decidir o cap**: enquanto o `entry_guard_auto_cap`
  estiver aplicado há mais de `cap_reminder_days` (default 7) e ainda não
  houver fills reais pós-cap, o tick (`_check_cap_reminder`) emite uma vez
  por dia uma notificação a pedir para correr o `study_cap_effect --days
  30` e decidir se o cap fica/aperta/reverte (guard por dia — o microssegundo
  era o bug do dedup).
- **Lote semanal sobre o histórico COMPLETO**: além do diário (janela
  recente), o `_check_recount_batch` corre uma vez por semana (`days=0`,
  guard por ISO-week) para os veredictos antigos ficarem todos corrigidos
  com o offset medido. O `/api/recounts/recheck-all` aceita `days=0`
  (histórico completo) nas corridas semanais.
- **Bónus (teste)**: o `test_confirm_never_bypasses_global_risk_switches`
  gravava o fecho da perda diária com a data UTC — o relógio local (+1h)
  está um dia à frente do UTC, logo `realized_today` (que usa `date.today()`
  LOCAL) não contava a trade e o confirm executava em vez de bloquear. Fix:
  usar o dia local no `closed_at` da trade. Suite: 470 passed.

---

### 2026-08-27 — Offset por recount no /auditoria, cap_applied_at + decisão do estudo, e lote diário com flips resolvidos (467)

- **Offset aplicado visível por recount no /auditoria**: a coluna do
  candle-check mostra agora um badge "offset N pips" (o `futures_shift`
  diário que o check usou — 0 sem amostras) e o tooltip inclui o valor —
  vê-se ao vivo qual correção produziu cada veredicto.
- **`cap_applied_at` gravado**: mudar o `entry_guard_auto_cap` via
  /api/config regista o momento EXATO (config, persiste na BD); o
  `study_cap_effect` usa-o automaticamente como `--since` (antes assumia
  hoje 00:00). O estudo ganhou a função pura `decide_after()`: sem fills
  pós-cap → "sem dados"; cap que corta perdedores → MANTER (poupa); cap
  que corta ganhadores → REVERTER/afrouxar (custa) — a decisão final só
  com dados reais depois do cap.
- **Lote diário com flips RESOLVIDOS**: `recheck_all_recounts` conta agora
  `resolved` (veredicto implausível/feed_divergence → plausível, que o
  `changed` não via — o `plausible` já era True na divergência de feed) e
  o `_check_recount_batch` notifica no feed quando `resolved > 0`: "N
  resolvido(s) (eram implausíveis/feed e agora plausíveis — o offset diário
  do GC=F mostrou que eram feed)".
- **Testes**: `test_recount_candle_check_daily_futures_offset` estendido
  com o lote (grava feed_divergence → fill → lote → `resolved ≥ 1` e o
  veredicto persistido passa a plausible com offset 50); novos
  `test_cap_applied_at_recorded` (grava/re-grava/persiste) e a decisão no
  `test_cap_effect_passes_guard` (decide_after: sem dados / MANTER / REVERTER).
  Suite: 467 passed.

---

### 2026-08-27 — Offset diário GC=F↔spot no candle-check dos recounts, estudo do efeito do cap e mini-gráfico da divergência (467)

- **Candle-check do GC=F com OFFSET DIÁRIO** (`state._day_futures_offset`):
  em vez de um offset fixo, o check do GC=F passa a usar a média GC=F −
  spot MEDIDA nos fills reais do dia (`market_price_at_open` do guard vs
  close do candle à hora do sinal; cache 5 min por dia+símbolo; 0 sem
  amostras). O prémio do futuro varia dia a dia (53–60 pips na BD real) e
  um offset fixo fabricava implausíveis falsos (baseline 66% no GC=F). O
  check expõe `futures_shift` por recount. Teste: um  relato plausível no spot (reversão 4351 vs low 4340) fica implausível no GC=F sem correção
  (low 4390) e volta a plausível com o offset +50 do dia. Sem fills do próprio
  dia, o offset cai na média dos últimos 30 dias do símbolo (medida, nunca
  fixa); sem nenhuma amostra, 0. Ao vivo: o recount de 08-19 passou de
  `feed_divergence` para `plausible` (shift 55.95) no re-check em lote — o
  GC=F passou a concordar com o spot.
- **`app/study_cap_effect.py` (read-only)**: compara fills reais antes/depois
  do momento do cap (`--since`) e aplica a regra real do guard como
  contrafactual (passa se ≤ tolerância ou ≤ largura × cap_widths). Veredicto
  na BD real: o cap 0.5× teria cortado 1 fill (937c65, 3.64 pips, **+8.80 €**)
  — o teto de segurança custa dinheiro no histórico (corta um ganhador);
  os outros 9/10 continuam a entrar; 0 rejeições por cap até agora.
  Re-correr `--since` depois de alguns dias para a comparação antes/depois
  real. Relatório em `reports/cap_effect_20260827.json`.
- **Mini-gráfico 🛰️ no /auditoria**: canvas `fillsDivChart` com as falsas
  perseguições (barras) vs offset médio GC=F−spot (linha) por dia — a
  evolução da divergência do feed sem abrir tabelas.
- **Testes**: `test_recount_candle_check_daily_futures_offset` (offset 0 →
  feed_divergence; com fill do dia → +50 → plausível, sem feed_divergence)
  e `test_cap_effect_passes_guard` (regra real do guard). Suite: 467 passed.

---

### 2026-08-27 — Divergência GC=F↔spot quantificada no /auditoria, cap aplicado ao vivo e notificação de rejeição por cap (464)

- **Painel 🛰️ "Divergência GC=F↔spot por dia" no /auditoria** (dentro do
  bloco 🏃): para cada fill com ambas as fontes, `feed_divergence` =
  preço GC=F (candle Yahoo) − preço spot (guard MT5) à hora do sinal;
  agrega por dia (offset médio/máx) e conta as **falsas perseguições**
  (na zona no spot — autoritativo — mas fora no GC=F) vs as **reais**
  (fora nas duas). Na BD real: offset médio **+55.95 pips** (o GC=F do
  ouro negocia ~56 pips acima do spot), **5/10 fills eram falsas
  perseguições** — os "8 fora até 65 pips" do relatório eram o feed de
  futuros, não entradas arriscadas. As tabelas de fills (dash, teste e
  /auditoria) mostram agora o preço **spot** com o GC=F + offset como
  contexto por linha.
- **`entry_guard_auto_cap` aplicado ao vivo**: 0.0 → **0.5** (2.0 pips ≈
  0.5× largura da zona — recomendação do `study_persecution_cap`). Com a
  largura mediana 4.0, o teto de perseguição desce de 20 pips (5× largura)
  para ~2.0 pips: os fills recentes (0.87–1.27 pips) continuam a entrar; a
  cauda (3.64 pips, P&L +8.80) passa a ser rejeitada — documentado como
  teto de segurança, não correção (a leitura de 65 pips era feed).
- **Notificação de rejeição por cap autónomo**: o texto passou a mostrar a
  distância real e o valor do cap — "⛔ Sinal XAUUSD rejeitado pelo cap
  autónomo: preço 36.00 pips fora da zona (cap 20.00 pips · 0.50× largura)
  — perseguição além do limite, não executada."
- **Testes**: extensões em `test_autonomous_zone_deviation_rejects_beyond_cap`
  (texto da notificação com distância+cap) e
  `test_rejection_report_zone_and_orphans` (feed_divergence, offset, falsas
  vs reais perseguições, `market_price` por fill). Suite: 464 passed.

---

### 2026-08-27 — Perseguições no /auditoria, cap honesto e notificação de fill fora da zona (464)

- **`fills_zone` agora mede com a fonte AUTORITATIVA (spot)**: a distância de
  cada fill real passa a vir do guard (`trade.entry_distance` — spot MT5 no
  momento da execução), com fallback para candles Yahoo marcado por fill
  (`distance_source` guard/candles). Na BD real os 10 fills dos últimos 30
  dias tinham 0–3.64 pips de desvio; os "8 fora até 65 pips" que o relatório
  mostrava com candles eram DIVERGÊNCIA GC=F↔spot (documentada no AGENTS.md),
  não perseguições reais.
- **`fills_zone.by_day`** — histórico por dia (UTC) de colocação: total /
  na zona / perseguições / distância máx, para ver QUANDO as entradas longe
  do mercado acontecem.
- **Bloco 🏃 "Entradas longe da zona · perseguições" no /auditoria** —
  stats + tabela por dia + detalhe por trade (fonte spot/candles visível),
  carregado no `loadAudit()` (usa o mesmo endpoint do relatório de rejeições).
- **`app/study_persecution_cap.py` (read-only)** — usa a distribuição REAL
  das distâncias dos fills (guard spot) para recomendar um
  `entry_guard_auto_cap` honesto: quantis, tabela de caps candidatos com o
  P&L do que cada cap cortaria (cortar ganhadoras é um teto de segurança,
  não uma correção), recomendação = menor cap que mantém ≥ 90% dos fills,
  convertido também em × largura da zona. Veredicto na BD real: sem
  perseguições reais (máx 3.64 pips) — cap 2.0 pips ≈ 0.5× largura como
  teto, manter o default é igualmente defensável; relatório em
  `reports/persecution_cap_20260827.json`.
- **Notificação de perseguição executada**: quando uma ordem pendente na
  zona PREENCHE com o preço fora da zona além da tolerância automática, o
  tick emite "🏃 Perseguição executada" com a distância real em pips
  (guard spot) — uma notificação por trade (agrega as pernas do mesmo
  tick), para o operador ver as entradas arriscadas em tempo real.
- **Testes**: `test_pending_fill_persecution_notifies_feed` (fill fora da
  zona → notificação com distância), `test_persecution_cap_recommendation`
  (função pura `recommend()`: sem perseguições → manter default; com cauda
  → corta mantendo ≥ 90%), e `by_day`/`distance_source` no teste de
  zona/órfãs. Suite: 464 passed.

---

### 2026-08-26 — Notificação agregada de órfãs, zona-vs-preço nos fills reais e filtros no "porquê sem trade" (462)

- **Notificação no feed/Telegram quando o relatório deteta pendentes órfãs**:
  o tick ganhou `_check_orphan_report` — a cada ~1h (guard
  `orphan_report_last_check`, cada perna só notifica uma vez) agrega por
  categoria as pendentes fechadas desde a última corrida (órfã vs cancelada
  vs expirada) e emite UMA notificação "🧨 'Porquê sem trade'" — já não é
  preciso abrir o relatório para saber que ordens da zona não entraram.
  O guard avança só depois da consulta correr bem (falha = re-tenta, nunca
  perde a notificação).
- **Zona-vs-preço no histórico de trades reais (fills)**: nova secção
  `fills_zone` no relatório — para cada trade real (mt5-real/mt5-dry) aberta
  na janela, compara a zona do sinal com o preço na hora e agrega quantos
  fills entraram DENTRO da zona vs quantos foram persecuções fora dela
  (contagem, % na zona, distância máxima + tabela por trade com P&L). As
  pendentes que nunca preencheram (pending_*) ficam de fora dos fills — são
  as órfãs do bloco 🧨. Novo helper `db.trades_opened_since`.
- **Filtros no bloco "porquê sem trade"**: seletor de símbolo + intervalo
  de distância à zona (mín/máx em pips) — isola rapidamente os padrões de
  rejeição que mais custam trades; aplicado também às órfãs (por símbolo) e
  com contador "mostrando N de M". Presente na dashboard e nas páginas de
  teste.
- Corrigido um bug de sintaxe JS pré-existente (chaveta extra no fim de
  `renderRejectionOrphans`/`renderTesteRejectionOrphans`) que partia o
  script inteiro das páginas — os ficheiros estáticos nunca tinham sido
  validados com `node --check`.
- Testes novos: `test_orphan_report_notifies_aggregated` (agregação + guard)
  e asserções de `fills_zone` no teste de zona/órfãs. Suite: 462 passed.


- **Zona do sinal vs preço real quando chegou**: cada sinal rejeitado no
  relatório expõe agora `zone_vs_price` — o preço no candle que cobre a
  hora da mensagem, a zona da entrada, se o preço estava na zona e quantos
  pips faltavam (`distance`). A dashboard e as páginas de teste mostram uma
  coluna "Zona vs preço real" (na zona / fora com distância), para perceber
  de imediato quão longe a entrada ficou do mercado na hora.
- **Pendentes órfãs/expiradas no relatório**: além dos sinais rejeitados, o
  "porquê sem trade" junta as ordens pendentes da zona que foram fechadas
  com `pending_expired`/`pending_orphan`/`pending_cancelled` (DB
  `closed_pending_positions`, junta aos trades para a zona). Para cada perna
  órfã o relatório explica **porque o preço não voltou ao nível da entrada**:
  o range de preços durante a vida da ordem, se tocou a entrada e quanto
  faltou — sub-bloco 🧨 no bloco do relatório.
- Teste novo `test_rejection_report_zone_and_orphans` e asserções de
  `zone_vs_price` no teste de categorias. Read-only: o relatório nunca abre
  trades nem escreve na BD.

- **Bloco "porquê sem trade" nas páginas de teste**: `teste.html` mostra
  agora o relatório de rejeições com o replay a usar os parâmetros da
  estratégia selecionada (`GET /api/analysis/rejections?strategy_id=<id>` —
  max_legs/gestão mudam o que o replay simula).
- **Notificação de pendentes órfãs no feed**: quando uma ordem pendente da
  zona expira (TTL), é cancelada pelo broker ou é órfã (ticket inexistente),
  o tick emite notificação no feed com texto que distingue os três casos —
  antes ficava só na BD e no Diagnóstico.
- **Conversa do grupo no relatório**: as mensagens ignoradas (tab 🚫
  Ignoradas) entram no mesmo relatório de rejeições como categoria
  `ignored` — o "porquê sem trade" distingue "rejeitei" de "nem tentou"
  (conversa pura nunca entra na pipeline).

### 2026-08-26 — "Porquê sem trade" (Fase 2), calibração data-driven das guardas (Fase 3) e reconciliação de pendentes órfãs + E2E de 7 dias (Fase 4) (458)

- **Fase 2 — bloco "porquê sem trade"**: `GET /api/analysis/rejections`
  conta os sinais rejeitados/falhados dos últimos N dias por categoria
  (`zone`, `min_lot`, `global_risk`, `pending_expired`, `broker`, `parse`,
  `llm`, `execution_failed`, `outro`) e, para cada sinal parseável, corre um
  **replay hipotético** com candles reais + gestão correlacionada (mesmo
  pipeline do `run_analysis`) — o que TERIA acontecido. Read-only: nunca
  abre trades nem escreve na BD. A dashboard (tab Relatório) mostra os
  contadores e o replay por sinal. `rejection_kind` passou a classificar
  rejeições de risco por lane (ex.: "nenhuma lane abriu a trade: ... perda
  diária ...") como `global_risk` em vez de `execution_failed`.
- **Fase 3 — sweep de calibração (`app/study_guard_calibration.py`)**: grelha
  read-only (cap 0–3× largura × tolerância 0.25–1.0 × risco 2–5%) sobre os
  sinais reais dos últimos 30 dias com candles spot MT5 (fallback GC=F),
  mesmo pipeline do backtest (kill diário incluído). Veredicto honesto:
  período perdedor (PF<1 em todas as células) — **MANTER os defaults**, sem
  calibração; relatório JSON em `reports/`.
- **Fase 4 — reconciliação de pendentes órfãs**: `reconcile_live_state`
  compara as pernas `pending_fill` da BD com `orders_get` do broker — ordem
  com ticket que já não existe no broker = óRFÃ (expirada/cancelada
  externamente ou restart antes do envio), sem ticket = órfã imediata; o
  diagnóstico expõe `pending.ok`/`pending.orphans` (read-only, o TTL fecha
  as órfãs no tick).
- **Fase 4 — E2E de 7 dias sem intervenção**: `test_e2e_week_autonomous_no_intervention`
  percorre uma semana de operação autónoma (mercado em zona, pendente que
  preenche, rejeição além do cap, auto-fit de lote mínimo, TTL de pendente,
  perda diária, pendente órfão expirado) e garante que NENHUM sinal fica
  `pending`/`awaiting_edit` no fim — cada rejeição explicada no relatório
  com contador + replay. Bug real corrigido no caminho: `_try_execute` não
  gravava `signal.error` no objeto em memória no ramo `execution_failed`
  (a BD tinha a razão, a resposta não).

### 2026-08-26 — Blindagem do Item 12: LLM nunca executa por GO/revive, confirm respeita os switches globais, métrica de edições pós-execução (442)

- **Buraco do GO fechado**: o handler `go` do grupo executava qualquer
  sinal em `awaiting_edit` sem erro — e um sinal `llm_parsed` não tem erro.
  O GO passa a ignorar sinais parseados pela IA, e `_try_execute` ganhou um
  **choke-point**: qualquer caminho (GO, revive, finalização, confirm) que
  lá chegue com `llm_parsed=True` regista + alerta e nunca abre trade.
- **Revive LLM**: um sinal falhado que o grupo completa só com parse da IA
  já não reabre a janela — fica `failed`/auditoria com o alerta (registo
  apenas, nunca executa).
- **Confirm respeita os switches globais (regressão)**: a rota
  `/api/signals/{id}/confirm` só contorna as guards de zona/lote; o kill
  switch, a perda diária e o máximo de posições voltam a correr por lane em
  `_try_execute` (verificado + 3 testes novos: kill switch, perda diária,
  max_positions).
- **Métrica contínua de edições pós-execução**: as edições a sinais que
  executaram sem janela passam a gravar `edited_at`; `db.post_execution_edit_metric`
  expõe `post_execution_edited/total/rate/pct` em `/api/edits` e no snapshot
  (`edit_metric`); o tick verifica a cada ~30 min e, com amostra ≥20 e taxa
  >5%, dispara alerta `risk` (feed + Telegram, cooldown 24h) — a regra
  "completo → executa já" do Item 12 tinha sido decidida com taxa real de
  0.0% e deve ser revista se o comportamento do grupo mudar. A página
  Diagnóstico mostra o valor face ao limiar de 5%.
- Suite: **442 testes** verdes.

### 2026-08-26 — Item 12: modo manual removido; janela de edição validada com dados reais (438)

- **Modo `manual` removido**: `config.mode` deixou de existir — o pipeline
  (`app/state.py`), o registo de configuração (`app/config.py`), o modelo
  (`app/models.py`) e o risk engine passam a ter **apenas o caminho auto**.
  O snapshot continua a expor `mode: "auto"` para a dashboard não quebrar.
- **Botões Aprovar/Rejeitar e `POST /api/signals/{id}/approve` removidos** da
  UI e do backend; os testes de rota confirmam que o endpoint devolve
  `404/405`. Ficam a rota `confirm` (só para sinais bloqueados pelas guards
  de zona de entrada / lote mínimo) e o `reject` correspondente — nunca é
  uma aprovação geral de sinais.
- **Fallback LLM sem destino manual (spec, opção b)**: um sinal
  `llm_parsed=True` nunca abre trade automaticamente e nunca fica pendente
  — é registado com status `failed` ("parseado por IA — registo apenas") e
  emite notificação `system` para auditoria. Aplicado em `_ingest_immediate`
  e em `finalize_awaiting_signals`.
- **Janela de edição — decisão com dados reais**: a spec exigia consultar a
  frequência de edições a sinais completos antes de mudar a regra. Medição
  de 26/08 sobre a BD real (342 sinais live): dos 305 que chegaram completos
  no primeiro parse, **0 foram editados depois (0.0%)** — abaixo do limiar
  de 5%. Regra mantida: completo → executa já; janela de 20s só para parses
  incompletos (documentado em TECHNICAL.md §4.1, ESTRATEGIAS.md, PUBLIC.md
  e AGENTS.md).
- **`go` do grupo**: finaliza já a janela e executa no único caminho auto
  (sem ramo "pendente manual"); o fluxo pós-janela passou a ser só risk
  checks + execução.
- **Frontend**: badges estáticos das páginas (teste/diagnóstico/help/
  auditoria) e o header (`hdr-badges`) deixaram de referir modo manual;
  o terminal lateral não mostra mais o seletor auto/manual.
- **Testes**: cobertura de modo manual removida de `test_api.py`/`test_timestamps.py`
  (sinais completos executam já, sem `/approve`); novos testes do caminho
  LLM (nunca executa, regista + alerta) e de rejeição de `mode: manual` no
  config. Suite: **438 testes** verdes.

### 2026-08-20 — R2 recounts por trade, R3 MFE win rate, R4 regra struggle

- **R2 — recount por trade como fill-audit externo**: o classificador
  reconhece `Signal #N: Sell at X … +N pips` (`kind=recount`), o parser novo
  (`app/messages/recounts.py`) extrai direção/entrada/excursão/reversão/pips,
  e o estado guarda em `trade_recounts` (dedup por message_id).
  `_recount_fill_audit` compara com as trades REAIS fechadas do mesmo dia
  (direção + entrada mais próxima, tolerância 40 pips): entrada ≥20 pips ou
  pips ≥30 de diferença disparam alerta `risk` no feed/Telegram.
  `GET /api/recounts` lista (read-only).
- **R3 — MFE win rate (convenção do grupo)**: o `simulate_signal` mede
  `mfe_pips` (excursão máxima favorável, observação pura) e o `aggregate()`
  expõe `mfe_win_rate`/`mfe_threshold_pips` (wins = MFE ≥ 25 pips por
  defeito, mesmo fechando em loss/BE). A divergência grupo↔bot compara agora
  com o `mfe_win_rate` quando disponível — menos falsos positivos para
  trades que andaram +40 pips e voltaram ao SL (win para o grupo, não para
  o bot).
- **R4 — regra "struggle" (backtest-only)**: novos parâmetros
  `struggle_ae_pips`/`struggle_fraction` no registo e no `simulate_signal`
  (antes do TP1, se o preço andou N pips contra e volta à entrada, fecha
  uma fração a BE e protege as restantes). Candidata
  `auto_escada_3_struggle` criada e HIDDEN. **Veredicto do sweep (20/08)**:
  a grelha `ae 5–25 × frac 0.25–0.75` (`app/sweep_struggle.py`, read-only)
  nas `auto_escada_3/5` não tem edge — a regra não disparou no período
  atual e só 3 sinais no histórico completo, com números idênticos à
  baseline. A candidata continua backtest-only, não promovida.
- `GET /api/recounts` passou a devolver cada recount com o seu fill-audit
  (`matched`/`alert`) e a tab Relatório ganhou a tabela "📋 Recounts do
  grupo vs fills reais" com os divergentes marcados. Um recount AO VIVO sem
  trade correspondente no dia emite notificação informativa `system`.
- **Audit enriquecido (20/08)**: o fill-audit compara também a **direção**
  (grupo SELL vs bot BUY → alerta) e o **candle-check**
  (`_recount_candle_check`): a excursão/reversão reportadas pelo grupo são
  validadas contra o high/low real dos candles Yahoo 15m do dia (sem
  símbolo no recount, resolve pelo sinal do dia com entrada próxima;
  sem dados → `checked=False`, nunca inventa). A agregação `weekly`
  (coerentes/divergentes/sem trade por semana ISO) alimenta o mini-gráfico
  da tab Relatório, e a notificação sem trade passou a apontar a **causa
  provável** cruzada com os sinais do dia (`_recount_no_trade_cause`:
  failed/rejected, pendente, direção oposta, kill switch/manual). Novo
  teste E2E do fluxo completo (tabela + audit + candle-check + notificação
  + contrato da dashboard).
- **Alerta de replay implausível (20/08)**: um recount AO VIVO cuja
  excursão/reversão reportada não cabe no range real dos candles do dia
  (`candle_check.plausible=False`) dispara notificação `risk` "Recount
  implausível vs candles do dia" para o operador investigar o relato.
- **Janela horária no candle-check (20/08)**: o high/low do dia inteiro
  mistura sessões (um pico às 22:00 não valida um relato do sinal das
  09:00). Quando a hora do sinal alvo é conhecida, o `_recount_candle_check`
  mede também o range dos candles ±3h à volta dela e usa ESSE veredicto
  como `plausible` (a sessão do sinal); o dia inteiro fica em
  `day_plausible`/`day_high`/`day_low`. A notificação implausível mostra a
  janela com as flags; o tooltip da tabela de recounts expõe dia + janela.
- **Scripts read-only (20/08)**: `app/sweep_recount_candles.py` valida a
  plausibilidade de todos os recounts guardados (plausíveis/implausíveis/
  sem dados + taxa) e `app/backfill_recounts.py` re-analisa o histórico
  (`ignored_messages`/`signals` falhadas) à procura de `Signal #N`
  descartados como conversa e insere-os em `trade_recounts` com `--apply`
  (por omissão só pré-visualiza). Executado na BD real (20/08): recuperou
  1 recount (a mensagem de correção do R1) e o sweep marcou-o implausível
  (reversão 4351 abaixo do low do dia 4378 — a entrada 4357 nem foi tocada
  pelos candles).
- **Estudo R3 vs recounts (20/08)**: `app/study_recount_mfe.py` (read-only)
  compara os pips reportados pelo grupo com o MFE real dos candles
  (concordância de win / falsos win / wins não reportados). Com 1 recount
  na BD: 0/1 concordância, 1 falso win (grupo reporta +110 pips, MFE real
  −3120) — amostra indicativa, não estatística.
- **Journal de implausíveis (20/08)**: quando o candle-check marca um
  recount implausível e o journal está ligado
  (`execution_journal_enabled`), o `_handle_recount` grava um evento
  `recount_implausible` em `execution_events` (signal_id, message_id,
  entrada/direção/excursão/reversão/pips, scope janela/dia, flags) —
  evidência passiva para o replay e o fill-audit (o replay ignora kinds
  desconhecidos, por isso nunca autoriza nem altera decisões).
- **Badge visual de plausibilidade por janela (20/08)**: a tabela de
  recounts da principal mostra agora dois badges quando a janela ±3h está
  disponível — "✓ janela ok" (verde) ou "✗ janela implausível" (vermelho)
  como veredicto da sessão, mais o contexto "dia ok"/"dia fora" — visível
  sem depender do tooltip (que mantém dia + janela + flags).
- **Estudo implausíveis vs fills (20/08)**: `app/study_recount_implausible.py`
  (read-only) cruza cada recount implausível com as trades reais do dia:
  o bot negociou (compara entrada/pips de cada lado) ou não negociou
  (causa provável por sinal do dia). Na BD real: 1/1 implausível, o bot
  NÃO negociou — o sinal do dia ficou `rejected` (formato/janela), ou seja
  quem divergiu foi a execução, não só o relato.
- **Bloco de qualidade de recounts no /auditoria (20/08)**: nova secção
  "📋 Qualidade dos recounts do grupo" — stats (total, coerentes,
  divergentes, sem trade, plausíveis, implausíveis, sem candles), tabela de
  cada recount com fill + candle-check e o estado semanal, carregada no
  `loadAudit` junto do journal-quality, fill-audit e forward-gate.
- **Fill-audit com evidência de recounts (20/08)**: o
  `compare_broker_fills` passou a devolver `recount_evidence` — os eventos
  `recount_implausible` do journal listados como divergência de evidência
  ("real" = relato do grupo, "replay" = range dos candles), com
  `bot_traded`/`bot_trade` para cruzar quem divergiu (grupo ou execução),
  sem re-consultar o Yahoo. A página /auditoria mostra-os num details do
  card de fill-audit.
- **Mini-gráfico semanal de implausibilidade (20/08)**: o bloco de
  recounts do /auditoria ganhou um canvas por semana (% plausíveis vs
  implausíveis vs sem candles) ao lado do estado semanal em tabela.
- **Re-check com candles frescos (20/08)**: `POST /api/recounts/{id}/recheck`
  refaz o fill-audit + candle-check de um recount específico (o Yahoo pode
  ter corrigido/expandido o histórico) e devolve o veredicto atualizado;
  read-only — não altera a linha guardada nem o journal. Botão "🔄
  re-check" por linha no /auditoria.
- **Veredicto agregado do estudo implausíveis-vs-fills (20/08)**: o
  `app/study_recount_implausible.py` classifica cada implausível como erro
  de relato / erro de execução / sem dados e emite o agregado + top-5 de
  cada. Na BD real: 1/1 erro de execução (sinal rejected).
- **Causa de execução perdida no fill-audit (20/08)**: o `recount_evidence`
  cruza cada implausível com `bot_traded=False` com os reinícios do
  listener (±15 min à volta do evento) e o kill switch persistido
  (`kill_switch`/`killswitch_rearm_at`) e devolve `miss_reason` — sinal
  possivelmente perdido no arranque do listener ou kill ativo no dia.
- **Toggle do journal no /auditoria (20/08)**: botão ⏸️/▶️ no card de
  qualidade do journal liga/desliga `execution_journal_enabled` com
  confirmação, sem sair da página (estado refletido no botão).
- **Linha de recounts no resumo diário (20/08)**: o resumo ganhou "📋
  Recounts do dia: N recebidos · X implausíveis · Y sem execução do bot"
  (journal quando ligado), junto das linhas de journal e Fase 3.
- **Baseline de falsos positivos do candle-check (20/08)**: novo estudo
  read-only `app/study_recount_baseline.py` — corre o mesmo filtro sobre os
  SINAIS reais dos últimos 30 dias (relato sintético exato = range real da
  janela ±3h). Resultado: 66% das entradas reais ficam fora da janela — o
  feed Yahoo serve GC=F (futuros) e o grupo negocia spot, divergência de
  dezenas de pontos; um implausível NÃO prova relato falso, tem de ser
  cruzado com os fills do bot.
- **Offset GC=F↔spot calibrado (20/08)**: o candle-check passou a usar o
  `_gold_futures_offset` — calibra o offset futuros↔spot com os fills
  REAIS de XAUUSD (mediana da diferença entre o candle GC=F na hora do
  fill e a entrada do bot; requer ≥3 amostras e |offset| ≥ 5 pontos).
  Quando aplicável, os preços reportados pelo grupo são corrigidos para o
  referencial dos candles antes de comparar (exposto como `gold_offset` no
  candle-check) — reduz os falsos positivos sem inventar plausibilidade.
  Na BD real ainda não há amostras suficientes (1 fill), por isso não
  altera os resultados atuais.
- **Atraso sinal→mercado (20/08)**: novo estudo read-only
  `app/study_signal_delay.py` — mede em minutos quando o GC=F toca a
  entrada anunciada vs a hora do Telegram. Resultado real (30d, 101
  sinais): mediana 224 min (~3.7h), p90 60h, só 28% das entradas tocadas
  ≤3h — a janela ±3h do candle-check é justa face ao atraso real entre o
  grupo e o feed de futuros; parte dos implausíveis é divergência de
  feed/hora, não relato falso.
- **Watchdog vs reinício manual no miss_reason (20/08)**: o
  `recount_evidence` classifica o reinício do listener (`restart_kind`:
  watchdog se o motivo contém "watchdog", senão manual/externo) e estima
  os  sinais na janela de arranque (`signals_lost_est`, mensagens do grupo
  nos 60s após o reinício — o replay do histórico recupera, mas a execução
  ao vivo atrasa). O /auditoria mostra a taxa de implausibilidade dos
  recounts vs o baseline (66%) para o operador ver se o relato é pior que
  o esperado ou só divergência do feed.
- **Re-baseline com janela 6h + offset (20/08)**: o `study_recount_baseline`
  aceita `--window-hours` e `--apply-offset`; com 6h a taxa cai 66.3% →
  62.4%. O offset ainda não está ativo na BD real (1 fill < mínimo de 3),
  por isso a melhoria é só da janela — o mecanismo de calibração fica
  validado para quando a demo acumular amostras.
- **Painel do offset no /auditoria (20/08)**: o `recounts_report` expõe o
  bloco `calibration` (offset estimado, amostras, ativo sim/não) e o bloco
  de recounts mostra "Offset GC=F↔spot: X (N amostras)" — o operador sabe
  quando a calibração passa a estar ativa (≥3 fills).
- **gold_offset no journal (20/08)**: o evento `recount_implausible` grava o
  `gold_offset` aplicado no payload — o replay reproduz o mesmo veredicto
  com os mesmos dados.
- **vps_reboot no miss_reason (20/08)**: quando `restart_kind=manual`, o
  `recount_evidence` cruza o reinício com o arranque do servidor
  (`server_booted_at` persistido no lifespan pelo boot.ps1) e devolve
  `vps_reboot=true` se o reinício coincide com o boot — distingue reboot
  da VPS (Tarefa NexGen Boot) de reinício manual isolado. O lifespan
  persiste `server_booted_at` na config.
- **Re-check em lote (20/08)**: novo `POST /api/recounts/recheck-all` —
  refaz o fill-audit + candle-check de TODOS os recounts com candles
  frescos (buscados uma vez por símbolo) e o offset mais recente, e
  persiste o veredicto em `trade_recounts.candle_check` (+ `checked_at`,
  migração automática no arranque). O /auditoria ganhou o botão "🗂
  re-check todos" e as linhas mostram o veredicto guardado pelo lote.
- **Janela adaptativa por símbolo (20/08)**: o candle-check usa agora
  `recount_window_hours_by_symbol` (config, JSON) por símbolo, com
  fallback para `recount_window_hours` (default 3h), limitado a [1, 24]h.
  O `study_signal_delay` mede o atraso por símbolo e recomenda a janela
  (p75 limitado a [3, 12]h; `--apply` grava). Na BD real foi aplicado
  XAUUSD=6h — a recomendação automática (12h) é contaminada pelo feed
  GC=F (p75 29h ≫ mediana 3.7h: a cauda é o futuro a tocar a entrada
  tarde); 6h é o meio-termo validado pelo re-baseline (62.4%).
- **Estudo MT5 spot vs GC=F (20/08)**: novo read-only
  `app/study_recount_mt5_feed.py` — corre o MESMO candle-check com candles
  M15 de XAUUSD do terminal (spot nativo, sem offset) lado a lado com o
  GC=F. Resultado real: o recount de 08-19 que o GC=F marcava implausível
  é PLAUSÍVEL no spot MT5 (reversão 4351 cabe no low real 4335; excursão
  4362 no high da janela 4362) — falso positivo do feed de futuros, não
  relato falso. Aproveitou-se para corrigir dois bugs latentes do
  `MT5Provider.copy_rates`: `or []` sobre a structured array do numpy
  (truthiness ambíguo → o download falhava sempre) e o acesso a campos
  por atributo (o numpy 2.x removeu-o → velas com ts=0); agora usa acesso
  por índice com fallback para atributo (fakes dos testes).
- **Notificação da calibração do offset (20/08)**: o tick verifica a cada
  ~30 min (`_check_gold_offset_calibration`) o offset GC=F↔spot e notifica
  quando passa a estar ATIVO (≥3 fills: "✅ Calibração ativa"), quando
  muda ≥ 10 pontos ("↔️ Offset mudou materialmente") e quando perde as
  amostras — estado persistido em `gold_offset_reported` (config).
- **Candle-check com spot MT5 como fonte primária (20/08)**: o check de
  produção passou a usar os candles M15 de XAUUSD do terminal (spot nativo,
  cache 5 min por símbolo) com fallback para o Yahoo GC=F quando o
  terminal está desligado — a divergência futuros↔spot deixa de fabricar
  implausíveis. Veredicto COMBINADO: quando o spot diz plausível mas o
  GC=F implausível, `feed_divergence=true` e `verdict_kind="feed_divergence"`
  ("⚠️ provável divergência de feed" no /auditoria e na dashboard, em vez
  de relato suspeito); o offset GC=F↔spot só se aplica à fonte futuros
  (spot é nativo). O `study_recount_mt5_feed --signals` recalibrou o
  baseline: com o spot real **0.0%** das entradas do grupo ficam fora da
  janela ±3h (vs 66.3% no GC=F) — os implausíveis eram TODOS divergência
  do feed de futuros.
- **Re-check em lote diário (20/08)**: o tick corre `_check_recount_batch`
  uma vez por dia UTC (guard `recount_batch_last_run` na config) — refaz o
  check de todos os recounts com as fontes combinadas e notifica quando
  algum veredicto persistido muda de regime ou há erros.
- **Offset GC=F↔spot eliminado + janela resetada (20/08)**: com o spot MT5
  como fonte primária, o offset de futuros perdeu o propósito — o GC=F é
  comparado SEM correção e só classifica a divergência do feed. Removidos
  o `_check_gold_offset_calibration` (tick), o painel "Offset GC=F↔spot" do
  /auditoria e o bloco `calibration` do `recounts_report`; a janela
  alargada XAUUSD=6h foi resetada para o default 3h. Verificação: o
  `study_signal_delay` com spot mede 101/101 entradas tocadas ≤3h (100% vs
  28% no GC=F), mediana 106 min — a janela 3h cobre tudo e os implausíveis
  do spot seriam relatos suspeitos reais.
- **Alerta de relato suspeito só com confirmação (20/08)**: a notificação
  risk "Recount implausível" passou a exigir que o spot MT5 (primária) E o
  GC=F discordem do relato (ou sem GC=F para confirmar) — se o spot diz
  implausível mas o GC=F diz plausível, não há confirmação e não dispara
  alerta. O /auditoria mostra "Implaus. confirmados" (spot+GC=F) e
  "Divergência de feed" (GC=F implausível, spot plausível) separados, com
  baseline 0% (o 66% era do feed de futuros).
- **Spot MT5 para XAGUSD/forex (20/08)**: o `_recount_candles` já era
  agnóstico ao símbolo (copy_rates por símbolo com fallback Yahoo);
  verificado ao vivo que o terminal devolve velas M15 de XAUUSD, XAGUSD e
  EURUSD — o feed spot cobre os metais menores e forex com o mesmo padrão.
- **ETA da Fase 3 conta o shadow (20/08)**: o `_fase3_eta` contava a origem
  race (vazia com o kill switch ON — a corrida só abre quando a principal
  executa) e mostrava N=0/sem ritmo, enquanto o gate já tinha 5–7 trades
  shadow. Passou a medir a MESMA origem do gate (`origin="shadow"` nas
  stats/ritmo/curva) — o painel mostra agora o ritmo real da evidência.
- **Gate forward com referência híbrida + limiar menor (20/08)**: a referência
  da `auto_escada_3` era só a corrida race da `nova_gestao` (que só enche
  quando a principal executa — com o kill switch ON ficava a 0 e o gate nunca
  ficava `reference_comparable`). Agora (a) `min_reference_trades` baixou de
  20 para **10** (a comparação de P&L/saída é descritiva e nunca promove
  sozinha); (b) a referência é **híbrida race+shadow** — a race tem
  precedência por sinal e o shadow (a `nova_gestao` foi adicionada a
  `shadow_paper_strategies`) cobre os sinais em que a principal não negociou.
  O gate deixa de depender de 20 trades reais para decidir.
- **Fix de "undefined" na legenda do comparativo (20/08)**: o
  `loadTesteCompare` buscava `nova`/`nova_gestao`/`tp2`, mas o mapa
  `NAMES`/`COLORS` só tinha `nova_gestao`/`tp2_protect`/auto-escadas — a
  legenda renderizava o literal "undefined" para `nova` e `tp2` e as curvas
  caíam na cor do grid (strokeStyle undefined). Adicionados nomes/cores para
  `nova` e `tp2` + fallback (`STRATEGY_NAMES[id]` e `#8a94a6`).
- **Slider da perda diária (20/08)**: o bloco "Limites" da dashboard ganhou um
  slider `dailyLossSlider` (0–15%, step 0.5; 0 = sem limite) que persiste
  `daily_loss_limit_pct` na config (debounced) e mostra o valor vivo
  (`dailyLossVal`); o `limitsLbl` ficou só com "máx N posições".
- **No-cache + cache-busting nos assets (20/08)**: o browser passou a servir
  `common.js` antigo (o slider ficava em 3.0% com o config em 4.5%).
  Adicionado middleware `no-cache, no-store, must-revalidate` em tudo o que
  não é `/api/`/`/ws`, e as páginas apontam agora `style.css`/`common.js` com
  `?v=<sha1 do conteúdo>` (segunda defesa contra caches que ignorem o
  no-store).
- **Slider 0 alinhado com a API (20/08)**: `daily_loss_limit_pct` aceita 0
  (`ge=0`, antes `ge=0.5` rejeitava o min do slider) e o `RiskEngine` trata 0
  como "sem limite diário" (off), igual ao `apply_daily_stop` do backtest.
- Suite completa: **389 testes passados** + 1 warning conhecido.

### 2026-08-20 — Janela de 28/08, rearme e correções de summaries

- Adicionado `GET /api/window` com trades reais fechadas/abertas, P&L, dias
  restantes, ritmo face à meta 10–20 e o gate+ETA da `auto_escada_3`.
- O resumo diário inclui a linha operacional da janela; a dashboard mostra o
  cartão compacto de progresso para promoção.
- `killswitch_rearm_at` fica persistido; o tick rearma no prazo UTC definido,
  notifica feed/Telegram e limpa a chave de forma idempotente.
- O classificador reconhece `summary_correction`, atualiza o summary alvo por
  reply/período inequívoco e reativa a divergência grupo↔bot.
- Suite completa: **368 testes passados** + 1 warning conhecido.

### 2026-08-20 — Exportação, arquivo e bloqueio do gate forward

- A página `/auditoria` passou a exportar o bundle journal + fill-audit + gate
  em JSON/CSV e a arquivar snapshots UTC na tabela `evidence_archives`, com
  histórico read-only das janelas.
- O resumo diário envia no feed/Telegram uma única mensagem com qualidade do
  journal e progresso de `auto_escada_3/5`, sem alterar a execução.
- A promoção das auto-escadas fica bloqueada visualmente e no backend até
  todos os gates objetivos da janela forward passarem.
- Regressões e documentação atualizadas; suite desta alteração: **364 testes passados** + 1 warning conhecido.

### 2026-08-20 — Auditoria diária, fill↔replay e gate forward shadow

- Adicionados `GET /api/operations/journal-quality`, `GET
  /api/analysis/fill-audit` e `GET /api/analysis/forward-gate`.
- A nova página `/auditoria` mostra a cobertura diária do journal, retcodes e
  latências, trades sem fita completa, a primeira divergência fill real vs
  replay e o gate das `auto_escada_3/5`.
- O gate usa janela UTC de 30 dias, mínimo N=20, PF≥1.5, IC95% positivo,
  DD≤125%, comparação com `nova_gestao` e promoção sempre manual.
- Regressões adicionadas para os relatórios, endpoints e frontend.
- Suite completa desta alteração: **361 testes passados** + 1 warning conhecido.

### 2026-08-20 — Evidência MT5 de fills, retcodes e latência no replay

- O `MT5Engine` passou a registar no journal opt-in pedidos/resultados de
  abertura e fecho, modifies de SL, fills simulados ou confirmados, retcodes,
  tickets, preços, P&L e latências sinal→ordem→fill.
- O replay preserva estes eventos como `broker_observations` passivas: usa-os
  para auditoria sem aceitar respostas históricas como comandos e sem tocar no
  broker/BD de produção.
- A exportação por trade mantém a fita completa e a documentação explica a
  ativação controlada de `execution_journal_enabled`.
- Regressões MT5/replay acrescentadas. Suite parcial desta alteração: **43
  testes passados**.

### 2026-08-19 — Exportação e inspeção passo a passo do replay

- `GET /api/operations/replay/tape` junta automaticamente `execution_events`
  aos sinais estruturados e resolve a correlação de gestão por reply; aceita
  filtro por sinal/trade.
- O replay devolve snapshots depois de cada evento; `GET
  /api/operations/replay/snapshots` permite seleccionar um passo sem tocar na
  BD de produção ou no MT5.
- O histórico da dashboard ganhou `🔁 replay` por trade, com relatório de
  paridade e estado dos três engines.
- Adicionadas regressões de exportação, snapshots e frontend. Suite nesta
  alteração: **353 testes**.

### 2026-08-19 — Fita de replay determinístico entre motores

- Criado `app/execution/replay.py` com contrato UTC ordenado por `sequence` para
  eventos `signal`, `quote` e `management`.
- O harness read-only alimenta a mesma fita ao SimEngine, PaperRace e MT5
  dry-run em BDs temporárias e compara decisões por perna, SL, razão e P&L.
- Adicionado `POST /api/operations/replay`; o endpoint limita a fita a 10.000
  eventos e nunca usa a BD de produção nem o broker.
- Regressões novas cobrem ordenação, TP1→BE, gestão protect e isolamento.
  Suite completa após a alteração: **350 testes passados** + 1 warning conhecido
  Starlette/httpx.

### 2026-08-19 — Journal opt-in, lifecycle explícito e roteamento fail-safe

- Adicionado `execution_events` + `app/execution/journal.py`: quotes e gestão
  podem ser recolhidos sem efeitos laterais e reproduzidos em ordem num estudo
  read-only. A captura fica desligada por defeito (`execution_journal_enabled`)
  e é exposta por `GET /api/operations/events`.
- O `PaperRace` usa o relógio injetável também para o dia do kill diário; os
  estudos deixam de depender dessa decisão do relógio civil da máquina.
- O registo de estratégias expõe `lifecycle` (`backtest`/`paper`/`shadow`/
  `menu`/`promotable`) e as contas paper passam a usar esse contrato central.
- Gestão sem reply fica fail-safe quando há várias trades de sinais diferentes
  do mesmo símbolo: não adivinha o alvo e deixa a notificação de risco para o
  operador confirmar.
- As métricas paper expõem intervalo bootstrap determinístico e um gate
  descritivo de evidência; nada promove automaticamente uma estratégia.
- A tab Diagnóstico passou a expor reconciliação read-only DB↔broker (legs em
  falta, posições desconhecidas e SL divergente), sem autocorrigir nem fechar
  posições.
- Regressões novas: journal/replay, lifecycle, evidence e ambiguidade shadow.
  Suite completa: **346 testes passados** + 1 warning conhecido Starlette/httpx.

### 2026-08-19 — Reducer comum e relógio determinístico (343 testes)

- Consolidado `reduce_auto_protect()` em `app/execution/protection.py`: o
  gatilho, a fórmula de proteção do grupo e os movimentos monotónicos passam a
  ter uma única decisão pura reutilizada pelo backtest, PaperRace, SimEngine e
  MT5 dry/real; cada adapter continua responsável pelos seus fills, broker e
  persistência.
- Adicionado `app/execution/clock.py` com `SystemClock` e `ManualClock`.
  O PaperRace e os engines aceitam relógio injetável; o estudo de polling
  avança explicitamente 1s/250ms/100ms, sem depender de `time.time()` ou de
  sleeps do processo.
- Removido o spec não rastreado duplicado `docs/SPEC-auto-gestao.md`; a fonte
  documental continua a ser `docs/ESTRATEGIAS.md`.
- A principal `nova_gestao`, kill switch, `MT5_DRY_RUN=1`, produção a ~1s e
  shadow-paper não foram alterados.
- Suite completa: **343 testes passados** + 1 warning conhecido Starlette/httpx.

### 2026-08-19 — Estudos de gestão, walk-forward e cadência paper (341 testes)

- O walk-forward calibrado foi expandido para `current`, `90`, `all` e `old`;
  a gestão completa foi positiva em 3/3 folds no formato recente, mas só
  1/3 nos últimos 90 dias e no histórico completo. O regime antigo OOS teve
  2/3 folds positivos, sem invalidar o facto de o P&L agregado ser perdedor.
- `all_fallback_60` foi incluído como candidato conservador; não alterou os
  resultados nos períodos maiores e continua desligado por defeito.

- Criado `app.study_strategies` com estudos read-only de ablation dos kinds de
  gestão, walk-forward calibrado, distribuição de risco por perna, fallback
  mecânico após TP1 e sensibilidade do `PaperRace` a 1s/250ms/100ms.
- Na amostra atual de 30 dias (107 sinais), a gestão completa ficou em
  PF 3.34 / +2 438.76 € / DD 22.7%; o `protect` isolado foi o maior
  contribuinte (PF 3.09 / +2 177.62 €), sem promoção nem alteração da principal.
- O fallback aplicou-se a 1 sinal e acrescentou apenas 0.90 €; ficou desligado
  por defeito. Com saldo 1 000 €, pesos TP3–TP5 são neutralizados pelo lote
  mínimo; o estudo de sensibilidade foi separado em saldo 10 000 €.
- O benchmark sintético mostrou que 1s pode perder spikes que 250ms/100ms
  observam, mas não é evidência de P&L mensal nem altera o loop de produção.
- O kill switch, `MT5_DRY_RUN=1`, `nova_gestao` e o shadow-paper permanecem
  inalterados.
- Suite completa: **341 testes passados** + 1 warning conhecido Starlette/httpx.

### 2026-08-19 — Preparação segura da principal após reboot (338 testes)

- Confirmado o arranque automático pós-reboot: um par lógico de servidor/listener,
  Caddy e MetaTrader 5 ativos; listener a obter heartbeat e a reenviar o
  histórico com respostas `200 OK`.
- Promovida a principal persistida para `nova_gestao`, a melhor estratégia no
  backtest comparável recente; `max_positions=5` e shadow-paper de
  `auto_escada_3`/`auto_escada_5` foram preservados.
- Recalibrados `balance`/`peak_balance` para 1.000 € e reduzido o risco base para
  1,0%, removendo a redução artificial para 0,1% causada pelo antigo high-watermark
  de 11.304 €.
- Mantidos deliberadamente `kill_switch=true` e `MT5_DRY_RUN=1`: a dashboard e
  o shadow continuam a receber sinais, mas não há ordens MT5 sem autorização
  operacional explícita. Backup local pré-alteração: `backups/signalbot-20260819-205634.db`.
- Suite completa: **338 testes passados** + 1 warning conhecido Starlette/httpx.

### 2026-08-19 — Auto-protect visível no dropdown (338 testes)

- Corrigidos os quatro menus de navegação (`/`, `/teste`, `/diagnostico` e `/help`) para exporem `auto_protect_5` como **Auto 5 TPs + protect precoce**, com link para a página de teste.
- Sincronizados `STRATEGY_NAMES` e `TERM_ORDER` no `common.js`, evitando que a estratégia exista no registo mas fique inacessível no frontend.
- Acrescentada regressão estática para exigir a entrada consistente nas quatro páginas; a suite completa terminou com **338 testes passados** e 1 warning conhecido de Starlette/httpx.

### 2026-08-19 — Risco defensivo, estado restart-safe e shadow de duas auto-escadas (337)

- Adicionado `def_risk_pct=1.25%` ao modo defensivo das auto-escadas: após uma janela completa de 10 trades com PF < 1.0, backtest, PaperRace, SimEngine e MT5 aplicam o mesmo cap absoluto, sem aumentar o risco dinâmico nem cortar pernas.
- Criada a tabela `protection_states`, com backup pré-migração local em `backups/signalbot-20260819-144647.db`; picos/vales, TP1, escadas, SLs e timestamps sobrevivem a restart de cada motor e são removidos de forma idempotente quando a trade fecha.
- O shadow-paper da VPS foi configurado para `auto_escada_3` e `auto_escada_5`, mantendo contas/origens isoladas; estratégia principal `auto_escada_5` e kill switch não foram alterados.
- Adicionadas regressões para o cap defensivo, janela completa, sizing paper e reconstrução do pico após restart.
- Suite completa validada: **337 testes passaram** + 1 warning conhecido de depreciação Starlette/httpx.

### 2026-08-19 — Auto-protect, replay seguro e análise de resolução (334)

- Adicionada a candidata `auto_protect_5`: fecha a primeira perna a 75% do caminho até TP1 e protege as restantes com a mesma fórmula monotónica do `protect` do grupo, partilhada pelo backtest, SimEngine, PaperRace e MT5; continua desligada da principal e exige sweep/paper antes de promoção.
- O replay histórico passou a transportar `reply_to_msg_id`, `message_id`, `move_sl` com nível explícito e `close_tp` por perna; replies não resolvidos são descartados em vez de contaminarem outra trade do mesmo símbolo.
- Criado `GET /api/analysis/resolution`, com M1 nativo até 7 dias, fallback explícito para horizontes maiores e sensibilidade a atrasos 0/8/60/300 s, sem writes nem look-ahead.
- Sincronizados o HELP, o painel de estratégia e o shadow-paper; a suite atual tem **334 testes**.
- Verificação read-only na BD atual: em 30 dias/107 sinais, `nova_gestao` ficou em PF 3.35 / +€2 439.75 / DD 22.7%, `auto_escada_5` em PF 1.33 / +€345.41 / DD 35.9% e `auto_protect_5` em PF 1.27 / +€281.41 / DD 38.6%; a candidata não supera a auto 5.
- O sweep da candidata (gatilho 0.25–1.0 × buffer 0–0.20) não encontrou combinação PF≥1 nos períodos atual e antigo: o melhor atual foi PF 1.96 / +€618.62, mas o melhor antigo continuou PF 0.50 / −€3 450.62.
- A análise temporal confirmou M1 nativo até 7 dias (23 sinais: PF 4.24 vs 7.72 em 15m) e fallback explícito para 15m em 30 dias; não se apresentou histórico parcial como M1.

### 2026-08-19 — HELP sincronizado com as estratégias visíveis (325)

- Atualizada a página `/help`: os sete fluxos visíveis (`nova_gestao`,
  `tp2_protect`, `auto_3`, `auto_escada_3`, a híbrida, `auto_escada_5` e
  `auto_protect_5`) agora têm mecânica explícita, e o glossário inclui
  escada, trailing, modo defensivo e shadow-paper isolado.
- Corrigido o comentário que identificava `tp2_protect` como oculta; a página
  continua a obter o registo do backend e a esconder as seis estratégias
  apenas de backtest/contrafactual.
- A regressão estática exige uma entrada `HELP_FLOW` por estratégia visível e
  mantém a regra no `AGENTS.md` para futuras alterações ao registo.
- Suite: **325 testes** (sem aumento do número de casos).

### 2026-08-19 — Auditoria final do isolamento shadow e gestão sem reply (325)

- Corrigido o fallback da gestão shadow para seguir a mesma ordem segura da
  conta real — reply exato, símbolo identificado e, por fim, a trade shadow
  aberta mais recente — sempre filtrando `origin="shadow"`.
- Corrigido o `divergence report` para comparar apenas trades da mesma origem;
  uma referência `race` já não pode gerar uma divergência falsa numa auto
  `shadow` sem referência shadow.
- Adicionada uma regressão de gestão pendente sem reply, uma de divergência
  entre origens e uma de não redirecionamento para uma trade shadow alheia.
- Suite: **325 testes passaram** + warning conhecido de depreciação do
  Starlette.

### 2026-08-19 — Shadow-paper: contas race/shadow realmente separadas (322)

- Corrigido o isolamento da corrida shadow: a deduplicação agora considera
  `signal_id + strategy_id + origin`, permitindo que o mesmo sinal seja
  observado pelas contas `race` e `shadow` sem duplicar nenhuma delas.
- Saldos virtuais, kill diário, PF e histórico de conta são separados por
  origem; a conta shadow começa em 1 000 € e nunca contamina a evidência da
  corrida normal. Adicionadas duas regressões para a abertura simultânea e o
  caminho de execução principal.
- Suite: **322 testes passaram** + warning conhecido de depreciação do
  Starlette.

### 2026-08-19 — Shadow-paper forward isolado (320)

- Criado o modo `shadow_paper_enabled` (default ativo) com seleção persistente
  de estratégias (`shadow_paper_strategies`, por defeito `auto_escada_3`). Sinais
  live completos entram numa conta paper marcada `origin="shadow"` antes das
  decisões de risco/kill da principal, mas nunca chamam o motor MT5; replays
  históricos e propostas LLM ficam excluídos.
- A deduplicação por sinal/estratégia evita trades virtuais duplicadas. O painel
  principal mostra o estado isolado e permite ligar/desligar e escolher as
  estratégias. Gestão por reply, correções TP1/SL e pendências usam marcadores
  separados para execução real, corrida paper e shadow (`applied`,
  `paper_applied`, `shadow_applied`); uma entrega shadow não consome a pendência
  que uma trade paper normal ainda precisa. `out at BE` mantém a proteção a BE
  quando a instrução fica enfileirada.
- Atualizados `AGENTS.md`, `TECHNICAL.md`, `PUBLIC.md`, `ESTRATEGIAS.md` e
  `DEPLOY.md`; adicionadas regressões de histórico/LLM, kill switch, origem,
  gestão com trade real em paralelo, TP1/SL e pendências BE.
- Suite: **320 testes passaram** + warning conhecido de depreciação do Starlette.

### 2026-08-19 — Itens 10/17: observabilidade operacional e CI (308)

- **Item 10**: exceções de runtime, observações de spread MT5, eventos de
  ligação/reconexão e modifies de SL passaram a ser persistidos em tabelas
  próprias; o cálculo de pips mantém a convenção de pip separada do tick de
  5 dígitos do forex, evitando um spread/risk distance 10x inflacionado.
  As métricas são agregadas no `GET /api/operations?days=N` e mostradas no painel
  "Saúde operacional" da tab Relatório. O MT5 também audita uma falha de
  modify BE antes de a perder no log; não há alteração de risco, estratégia
  ou abertura de trades.
- **Item 17**: criado `.github/workflows/ci.yml` para `pytest -q` em
  push/PR, validação documental separada e `failure_probe` manual que deixa o
  job vermelho com um teste deliberadamente falhado; o probe usa um nome de
  módulo pytest válido para falhar por asserção, não por erro de import, e
  limpa o ficheiro com `trap`.
- A aceitação final do Item 10 continua dependente de duas semanas de dados;
  a do Item 17 precisa do primeiro run no GitHub Actions. Os specs permanecem
  até essa evidência existir.
- Suite: **308 testes passaram** + warning conhecido de depreciação do
  Starlette.

### 2026-08-19 — Operação segura: conta hedged, logs, disco, restore e docs (302)

- **Item 15**: `MT5Provider` lê `account_info().margin_mode`, expõe
  `margin_mode`/`hedged` no status e bloqueia em modo real contas netting ou
  com modo desconhecido antes de qualquer ordem; a regressão cobre conta
  `RETAIL_NETTING` e `RETAIL_HEDGING` com módulo MT5 fake.
- **Item 7**: `app.server_runner` e o listener usam streams UTF-8 rotativos
  (10 MiB/5 cópias por defeito, configuráveis), incluindo aparagem de
  artefactos oversized já existentes; o `backup.log` usa a mesma
  política e `maintenance.py --db-audit` produz contagens, meses observados,
  taxa mensal e política de retenção por tabela.
- **Item 8**: check diário read-only de espaço livre no watchdog, com limiar e
  volume configuráveis, estado no snapshot/diagnóstico e alerta `risk` no feed
  (e Telegram quando configurado); o dia do último check/alerta persiste para
  não duplicar o aviso depois de um restart.
- **Item 9**: `backup_db.py --restore-check` restaura o backup mais recente numa
  BD temporária, valida integridade/tabelas/contagens e alerta falhas; testes
  cobrem sucesso, corrupção e mismatch de contagens.
- **Item 14**: PUBLIC/TECHNICAL/ESTRATEGIAS foram reconciliados com a janela
  real de 8s, replay histórico completo, demo MT5, autenticação, feed de
  candles reais/fallback, risco 2.5%, kill diário 3% e limites operacionais.
- Corrigida uma corrida async na validação Fase 3: um veredicto calculado para
  uma promoção antiga já não pode sobrescrever a janela acabada de promover.
- Suite: **302 testes** verdes + 1 warning de depreciação do Starlette.

### 2026-08-19 — Item 3: confirmação da auto_escada_3 (292)

- Implementado o comando read-only `--item3-sweep`, que compara
  `confirm_filter=True` vs `False` nas janelas atual/tudo/antiga, mantendo
  `stair_levels=3`, `stair_from=1`, `trail_factor=2.0`, custos e candles
  iguais. As variantes não partilham estado mutável e têm regressões de
  propagação do parâmetro, gate e ausência de leakage.
- O replay real passou o critério: PF atual **1.27→1.26** (queda relativa
  0.79%) e DD antiga **312.9%→278.3%** (redução 11.06%); tudo foi
  0.47/322.0% contra 0.53/286.1%. `auto_escada_3` ficou com
  `confirm_filter=True` por default.
- A principal continua `nova_gestao`, nenhum parâmetro foi promovido para a
  conta demo e o spec `docs/Melhorias/03-item-03.md` foi apagado só depois
  da aceitação.
- Suite focada e completa: **292 testes** verdes + 1 warning de depreciação
  do Starlette.

### 2026-08-19 — Integração Fase 3 e prontidão para o Item 3 (289)

- Acrescentada a regressão `test_fase3_promotion_paper_to_mt5_restart_and_rollback`:
  a mesma entrada percorre a corrida paper, promoção, caminho MT5 com broker
  fake, restart/reconstrução do estado, dois modifies 10016 recusados e
  rollback automático — sem enviar ordens ao terminal demo.
- Corrigido o veredicto da janela: conta apenas as **primeiras 10 trades
  abertas após a promoção**, limita a avaliação a esse lote e exige saída igual
  ao paper em ≥9/10; sem pares paper↔MT5 a janela reprova de forma conservadora.
  A mensagem dos retcodes recusados passou a mostrar os códigos reais.
- Auditoria operacional: só o par esperado do servidor/listener principal está
  ativo (porta 8000 e heartbeat); não havia listeners/servidores órfãos nas
  portas de preview 8081–8083, portanto nada seguro para terminar.
- Item 3 está desbloqueado para começar como sweep read-only de
  `confirm_filter=True` vs `False`; o spec permanece até o resultado ser
  executado e documentado.
- Suite: **289 testes verdes** + 1 warning de depreciação do Starlette.

### 2026-08-19 — Revisão adversarial do lifecycle, paridade e sweep (287)

- Corrigidas fronteiras de proteção: time stop agora ignora durações inválidas,
  normaliza timestamps naive/UTC sem crashar e mantém a fronteira inclusiva.
- Corrigido o estudo de consolidação: offsets ISO (incluindo fusos negativos)
  convertem-se para UTC, o ATR diário usa a última observação de cada dia e o
  filtro p20 consulta apenas dias anteriores; a ATR da vela pré-entrada deixou
  de ficar excluída por um off-by-one.
- Acrescentadas regressões para estas fronteiras, além da contagem atualizada
  de **287 testes**. Nenhuma ordem foi enviada à conta demo.

### 2026-08-19 — Item 1 encerrado, lifecycle/paridade e sweep do Item 2 (280)

- **Lifecycle isolado**: `app/background.py::BackgroundRuntime` passou a
  possuir explicitamente as tasks de mercado/watchdog, com start idempotente,
  cancelamento e `await` no shutdown do FastAPI. O `TestClient` e a suite MT5
  deixam de herdar loops órfãos.
- **Paridade contínua**: `tests/test_engine_parity.py` parametriza buy/sell e
  envia os mesmos eventos de preço ao backtest, SimEngine, PaperRace e MT5
  dry-run; `app/execution/parity.py` normaliza decisões, SL aplicado, razão e
  P&L para comparar os quatro caminhos.
- **Causalidade da híbrida**: `GET /api/analysis/protect_causal` faz replay
  read-only por prefixos e atribui o delta incremental de cada `protect`.
  No histórico de 19/08: 355 eventos/270 sinais, 0 harmful e delta 0 €; a
  híbrida igualou a auto pura (PF 3.20/DD 24.6% em 30d; PF 2.21/DD 32.5%
  completo). `docs/Melhorias/01-item-01.md` foi apagado só após a aceitação.
- **Item 2 iniciado depois da remoção do Item 1**: `study_consolidation.py`
  ganhou o comando reproduzível `--item2-sweep`, com grelha 3×3×3 sem
  leakage, avaliação de monotonicidade e gate PF/DD. Resultado: 0 elegíveis
  para DD antiga <50%; pico isolado no eixo `def_window` atual; defaults
  mantidos; o spec `docs/Melhorias/02-item-02.md` foi apagado após a
  aceitação.
- **Suite**: **280 testes verdes** (`pytest -q`, 73.5s) + 1 warning de
  depreciação do Starlette; nenhuma ordem foi enviada à conta demo.

### 2026-08-19 — Núcleo comum de proteção entre os três motores (272)

- **Política pura partilhada** (`app/execution/protection.py`): centraliza
  monotonicidade do SL por direção, escada, trailing com piso ATR,
  confirmação, fronteira inclusiva do time stop, classificação `sl/be/trail`
  e merge conservador do `protect` híbrido. Não acede à BD, relógio ou broker.
- **Adaptadores**: backtest, SimEngine, PaperRace e MT5Engine usam a mesma
  política para calcular níveis e fronteiras; cada motor mantém apenas fills,
  persistência, throttling e retcodes próprios. O trailing continua sem
  look-ahead e o MT5 só grava o SL depois de retcode aceite. O alerta de regime
  passou a receber `sim_params(strategy_id)`, portanto avalia a mesma escada,
  trailing, modo defensivo e ATR da estratégia ativa.
- **Regressões**: cobertura de monotonicidade buy/sell, ATR floor, empate
  TP/SL, time stop inclusivo, escada, trailing sem look-ahead, proteção híbrida
  e ciclo paper após restart.
- **Suite**: 272 testes recolhidos; a validação focada dos módulos de proteção,
  estratégia e backtest fica verde. Nenhuma ordem foi enviada à conta demo.

### 2026-08-19 — Auditoria adversarial da corrida híbrida e documentação (267)

- **PaperRace**: o `closed_at` das pernas passa a ser atualizado também no
  objeto em memória e a razão final da trade usa a perna realmente fechada por
  último; um desempate sequencial cobre escada/confirmação que fecham várias
  pernas no mesmo tick.
- **Divergence report**: cobre as duas ordens auto→referência e
  referência→auto; o alerta "auto continua" fica pendente e é atualizado com
  o P&L final dos dois lados quando a auto fecha, com a razão final correta.
- **Documentação**: AGENTS/TECHNICAL/ESTRATEGIAS/PUBLIC alinhados com 267
  testes, 12 estratégias no registo e 6 visíveis no menu (a híbrida continua
  experimental).
- **Validação focada**: `test_protection.py` (6), `test_strategy.py` (16),
  `test_backtest.py` (35) e `test_docs.py` (1) verdes; a suite completa foi
  apenas recolhida (267), não foi usada como critério desta auditoria porque
  contém testes com loops de fundo longos.

### 2026-08-19 — Limpeza de estratégias: alias REMOVED + curva só com as visíveis (255)

- **`app/strategies.py`**: alias `REMOVED_STRATEGIES` (= `HIDDEN_STRATEGIES`,
  as 4 removidas) para o código ficar literal ao plano de limpeza;
  docstrings atualizadas (a `strategies_list` distingue REMOVED — fora do
  menu E do paper — de MENU_HIDDEN — fora do menu mas em papel).
- **`GET /api/paper/history` (curva da corrida)**: passou a filtrar
  `HIDDEN_STRATEGIES` e `MENU_HIDDEN_STRATEGIES` — antes mostrava TODAS as
  estratégias com histórico (as 4 removidas + nova/auto_5 apareciam na
  curva, contra a matriz do ESTRATEGIAS.md Parte IX). Agora a curva mostra
  só as 5 visíveis + a linha ⚡ real (verificado ao vivo: nova_gestao,
  tp2_protect, auto_3, auto_escada_3, auto_escada_5, __real__).
- **Backtest das 5 visíveis no histórico completo** (579 sinais, saldo
  1000): nova_gestao PF 1.00 · +20 € (morre), tp2_protect PF 0.82 · −1 399
  € (morre), auto_3 PF 0.43 · −3 207 € (morre), auto_escada_3 PF 2.10 ·
  +6 863 € · DD 24.3%, auto_escada_5 PF 2.30 · +11 063 € · DD 25.8% —
  tabela adicionada ao ESTRATEGIAS.md Parte VIII com a leitura honesta
  (só as escada têm edge no histórico completo; a referência fica em
  break-even porque o período antigo anula o atual).
- **Testes**: `test_paper_history_endpoint_returns_series` atualizado (a
  nova sai da curva; a série usa a auto_escada_3); `test_fase3_validation_rollback`
  robusto face ao loop de fundo (a re-promoção reabre a janela e o loop
  pode reavaliar com as mesmas trades — o contrato é "nunca apaga o
  histórico", não "fica com 1").
- Suite: **255 testes verdes** · servidor reiniciado em `mt5-real`.

### 2026-08-19 — Snapshot da corrida paper em TODA a promoção (255)

- **`update_config`**: o snapshot da corrida paper (`fase3_snapshot` —
  stats paper, DD, divergences paper↔MT5, auditoria de SL) é gravado em
  **qualquer** promoção de estratégia (painel Fase 2/3, feed ou páginas de
  teste), não só da `auto_escada_3` — o antes/depois fica registado para
  qualquer estratégia promovida. A **reabertura da janela de validação**
  (limpar `fase3_verdict`) continua só para autos com auto-gestão.
- **Teste atualizado** (`test_fase3_promotion_snapshot`): promover a `nova`
  também grava snapshot (`strategy_id: nova`), o veredicto fica intacto numa
  promoção de não-auto e a promoção da auto volta a limpar (reabre a janela).
- **Screenshot verificado**: instância semeada (21 trades auto PF 4.0 · 15
  ref) — painel Fase 2 com P&L 140% · DD 3% vs 3% · N 21/20, veredicto
  "Critérios de aprovação atingidos" e botão **⬆️ Promover a principal
  (Fase 3)** visível (limpa depois: processo + BD + script).
- Suite: **255 testes verdes** · servidor reiniciado em `mt5-real`
  (nova_gestao · auto · MT5 ligado · listener vivo).

### 2026-08-19 — Painel Fase 2: botão de promoção + P&L/DD preenchidos e honestos (255)

- **⬆️ Promover a principal no painel Fase 2**: quando o gate estrito 3/3
  está cumprido (N ≥ 20 · PF ≥ 1.5 · P&L ≥ 50% · DD ≤ 125%), o painel
  mostra o mesmo botão do Fase 3 (handler partilhado) — POST /api/config
  {strategy} que, ao promover a auto, **grava o snapshot da auditoria de
  SL/divergências** (`fase3_snapshot`: dd_auto/dd_ref, pnl, pf,
  paper_vs_real_pairs, sl_audit_entries, real_trades_closed) para a janela
  de validação comparar antes/depois (verificado ao vivo: promoção via API
  na instância de verificação gravou o snapshot completo).
- **P&L/DD preenchidos e honestos (tri-state)**: com dados reais, os minis
  mostram o valor (ex.: "P&L auto vs ref 140%" com tooltip €, "DD 3% vs
  3%"); sem trades fechadas mostram "—" e o gate conta 0/3 — antes o
  painel dava "✓ P&L · ✓ DD · 2/3" com a corrida vazia. Mesma lógica no
  painel Fase 3 (consistência).
- **Verificação ao vivo**: instância de verificação com BD temporária
  semeada (21 trades auto PF 4.0 · 15 ref) — painel preencheu 140%/3% vs
  3%, veredicto "Critérios de aprovação atingidos" e botão visível;
  depois limpa (processo morto, BD e script removidos).
- Suite: **255 testes verdes** (só frontend mudou).

### 2026-08-19 — Gate estrito 3/3 no painel Fase 2 + testes da janela de validação (255)

- **Painel Fase 2 alinhado ao gate estrito 3/3** (igual ao Fase 3): a
  aprovação exige N ≥ 20 · PF ≥ 1.5 · **P&L ≥ 50% da referência** ·
  **DD ≤ 125% da referência** em simultâneo (antes só N+PF). O painel ganha
  os minis "P&L auto vs ref" e "DD auto vs ref" e o veredicto mostra o
  progresso 3/3 (PF · P&L · DD); o refresh dos instrumentos re-renderiza
  também a Fase 2 (o DD vinha de `_phase3dd`).
- **Testes novos da janela de validação** (standalone `_compute_fase3_verdict`
  com BD temporária + mocks, sem o singleton): `slow_opens` (sinal→abertura
  > 2 min → rollback, PF ≥ 1 a provar que o rollback vem do atraso) e
  `pnl_incoherent` (real a perder onde o sim previa ganho → rollback com
  PF 18).
- **test_mt5.py: +2 caminhos da auto-gestão** — `confirm_filter` (perna
  intermédia leva SL → longínquas fecham com reason `confirm`, perna 1
  intacta) e **escada em SELL** (SL das pernas 2/3 desce para o nível do
  TP1, na BD e no broker). 25 testes no ficheiro.
- **Suite: 255 testes verdes** (251 + 4). AGENTS.md e ESTRATEGIAS.md
  atualizados.

### 2026-08-19 — test_mt5.py reconstruído: 5 testes auxiliares de volta (251)

- **test_mt5.py completo com 23 testes** (18 + 5 reconstruídos): retcode 0
  aceite, perna NO_TICK fica open (a trade não é phantom), flag 10027 só
  quando **nenhuma** perna chega ao broker, retcodes parciais, AutoTrading
  ON com ordens a chegar ao broker, e `exit_at_tp` (perna 1 fecha no TP1,
  restantes no TP2) — comportamentos verificados contra o motor real.
- **Suite: 251 testes verdes** (a contagem volta ao nível pré-substituição).
  AGENTS.md e ESTRATEGIAS.md atualizados para 251.

### 2026-08-19 — Fase 3: gate estrito 3/3 + janela de validação com duplicados/retcodes

- **Gate de promoção estrito**: o painel Fase 3 voltou a exigir os **3
  critérios em simultâneo** (PF ≥ 1.5 · P&L ≥ 50% · DD ≤ 125% + N ≥ alvo) —
  a regra "comité 2 de 3" foi removida do `renderPhase3` e do
  ESTRATEGIAS.md Parte X (decisão do utilizador: com N=20 o ruído não
  justifica flexibilizar; o DD alto é o sinal de risco que o gate deve
  travar mesmo com PF/P&L bons).
- **Janela de validação com "zero ordens duplicadas e retcodes 10009"**:
  `_check_fase3_validation` agora deteta **pernas duplicadas** (mais de uma
  posição com o mesmo `leg` — o comentário único `trade.id:legN` do broker)
  e **modifies recusados** (retcodes ≠ 10009/0 na auditoria `autosl:`,
  ex.: 10016 stops inválidos) nas trades da janela — ambos disparam
  rollback automático e ficam no veredicto (`duplicate_orders` /
  `refused_retcodes`), visíveis no painel e no histórico.
- Teste novo `test_fase3_validation_rollback_duplicate_orders` (10 trades
  com lucro, perna 1 duplicada na trade 3 → rollback por duplicadas, não
  por PF).
- **Janela de validação com atraso e coerência P&L** (doc Parte X.1):
  `_compute_fase3_verdict` (extraída para função standalone, testável só
  com a BD) adiciona **`slow_opens`** (atraso sinal→abertura > 2 min) e
  **`pnl_incoherent`** (P&L real a contradizer o backtest do mesmo sinal)
  ao veredicto — ambos disparam rollback e ficam visíveis no painel.
- Teste E2E novo `test_auto_modify_refused_10016_triggers_rollback`
  (test_mt5.py): caminho real com broker fake, TP1 → escada → broker
  RECUSA o modify com 10016 → auditoria `autosl:` com applied=False →
  veredicto ROLLBACK (refused_retcodes=2). **246 testes** (a reconstrução
  do test_mt5.py após uma substituição acidental perdeu 5 casos auxiliares
  antigos; o ficheiro completo tem os 18 testes atuais).
- Referências "SPEC v2" (ficheiro consolidado no ESTRATEGIAS.md) no painel
  Fase 2 corrigidas para "ESTRATEGIAS.md Parte VIII/X" — verificadas no
  preview (painel Fase 3 a renderizar com o gate 3/3 e as novas métricas).

### 2026-08-19 — Painel Fase 3 alinhado com o ESTRATEGIAS.md Parte X (auditoria doc↔código)

- **Gate de promoção com regra do comité (2 de 3)**: o painel Fase 3 exigia
  TODOS os critérios (AND); o doc Parte X define "decisão por comité, 2 de 3
  — nunca PF isolado". Alinhado ao doc: `renderPhase3` agora marca cada
  critério (PF≥1.5 · P&L≥50% · DD≤125%) e o botão de promoção abre com
  **N ≥ alvo + 2 dos 3 critérios** (o veredicto mostra o comité lado a lado).
- **Janela de validação expõe a métrica do doc (≥ 9/10 mesma saída)**: o
  veredicto de `_check_fase3_validation` ganhou `same_exit_n`/`same_exit_pct`
  (contagem de pares paper↔MT5 com a mesma decisão de saída) e a razão passa
  a incluir "mesma decisão do papel em N/10" — visível no painel e no
  feed/Telegram. O docstring da manutenção alinhado (PF ≥ 1.0 + ≥ 9/10 +
  zero sem SL).
- **Referências órfãs do antigo spec limpas**: §B.5.x apontava para o
  SPEC-auto-gestao.md apagado — atualizadas para "ESTRATEGIAS.md Parte X"
  em `app/state.py` (×6), `app/strategies.py` (×2), `app/backtest.py` (×2),
  `app/execution/mt5.py` (×4), `app/execution/paper.py` (×1), `app/main.py`
  (×2), `app/sweep_atr.py` (×1), `app/static/index.html` (×3, incluindo o
  título do painel e o confirm de promoção) e AGENTS.md (regra do comité +
  métrica mesma-saída + referências "SPEC v2 §3.4" → Parte X/VIII). Os itens
  5/6 da checklist (paper↔MT5, real vs backtest) passaram a ser rotulados
  como critérios da **janela de validação** (avaliados nas 10 trades reais),
  não gates de promoção.
- Testes: asserção nova em `test_fase3_validation_rollback` (campo
  `same_exit_n`/`same_exit_pct` no veredicto). **250 testes verdes**.

### 2026-08-19 — Spec consolidado no ESTRATEGIAS.md (fonte única) — SPEC-auto-gestao.md apagado

- `docs/SPEC-auto-gestao.md` (892 linhas) foi **consolidado no
  `docs/ESTRATEGIAS.md`** e **apagado** — o ESTRATEGIAS.md é agora a fonte
  única sobre estratégias: a Parte V ganhou o modo defensivo, a Parte VI os
  parâmetros defensivos das escada_3/5, e a nova **Parte X** recebeu o
  protocolo da Fase 3 (§B.5: pré-requisitos N≥20 · PF≥1.5 · P&L≥50% ·
  DD≤125%, janela de validação de 10 trades com rollback automático,
  veredictos/ETA/alvo) e o **SPEC v3** (conta real, rascunho §B.7: gates de
  continuidade + linha vermelha). O histórico das fases concluídas ficou no
  CHANGELOG; a calibração detalhada nos scripts
  `app/mine_management.py`/`app/sweep_auto.py`/`app/sweep_atr.py`/
  `app/study_consolidation.py`.
- Referências atualizadas: `app/strategies.py` (×2), `app/execution/mt5.py`
  (§B.5 → Parte X), `app/mine_management.py`, `app/sweep_auto.py` e
  AGENTS.md (secção Auto-gestão + Fase 3). **250 testes**.

### 2026-08-19 — Modo defensivo implementado nas auto_escada_3/5 (backtest + paper + MT5)

- **Modo defensivo (estudo 19/08) implementado**: parâmetros novos no registo
  (`def_window=10`, `def_trail=1.5`, `def_time_stop=60`) nas `auto_escada_3/5`
  — quando o PF das últimas 10 trades resolvidas < 1.0, a estratégia passa a
  trail apertado + time stop SEM cortar pernas; volta ao normal quando o PF
  recupera. Espelhado nos 3 motores, sem look-ahead:
  - **backtest** (`simulate_signal` + `run_analysis`): `def_state` partilhado
    entre sinais (ordem cronológica); helper `_roll_pf`;
  - **paper** (`PaperRace._update_trade`): lê as últimas 10 trades fechadas
    da conta virtual (`db.paper_closed_recent`) a cada tick;
  - **MT5 real** (`_update_real`, Fase 3): lê as últimas 10 trades REAIS
    fechadas (`db.recent_closed_pnls`) — `auto_eff` com o trail/time stop
    defensivos;
  - **snapshot**: `strategy_params.defensive` expõe quando a principal está
    em modo defensivo (badge "🛡 modo defensivo" no header).
- **Sensibilidade só-ATR** (estudo): o filtro de entrada rejeita 0 sinais
  com o ATR sozinho (a rejeição era toda da sessão, que só filtra no período
  antigo) — confirma que o filtro não tem edge.
- Testes novos (5): `test_defensive_mode_tightens_trail_after_pf_drop` +
  `test_defensive_mode_requires_window_of_resolved` (backtest),
  `test_paper_defensive_mode_...` (paper), `test_real_mode_defensive_...`
  (MT5 E2E com broker fake) e `test_defensive_mode_params_in_registry`.
  **250 testes**. Docs: AGENTS/TECHNICAL/ESTRATEGIAS/spec.

### 2026-08-19 — Consolidação das estratégias (spec) + estudo filtro/modo defensivo

- **Fase 0 da consolidação**: `HIDDEN_STRATEGIES = {tp2, tp2_gestao,
  nova_gestao_3, auto_2}` (fora do menu E do paper — só backtest/compare;
  perdedoras/redundantes) e `MENU_HIDDEN_STRATEGIES = {nova, auto_5}`
  (ocultas do menu mas continuam em PAPEL — a `nova` é o contrafactual
  "sem gestão" do histórico de trades e a `auto_5` a comparação 5 pernas
  com/sem trail). Ficam 5 visíveis no menu: `nova_gestao`, `tp2_protect`,
  `auto_3`, `auto_escada_3`, `auto_escada_5`. Dropdown das 4 páginas e
  `TERM_ORDER` atualizados; as removidas continuam acessíveis por URL e no
  backtest/compare.
- **Estudo da consolidação (`app/study_consolidation.py`)**: filtro de
  entrada (sessão Londres/NY + distância entrada→TP2 ≥ 5 pips + ATR p20) e
  modo defensivo (PF(10) < 1.0 → trail 1.5 + time stop 60 min, SEM cortar
  pernas) nas 3 janelas. **Veredicto**: o filtro de entrada NÃO tem edge
  (rejeita quase só no período antigo onde tudo perde; a sessão não filtra
  nada no atual; ATR = ruído) — não implementado. O modo defensivo tem
  efeito pequeno mas consistente nas `auto_escada_3/5` (DD −5pp no
  histórico completo e antigo, P&L igual ou melhor, sem custo no atual) —
  a implementar com validação na corrida paper antes da Fase 3.
- Testes atualizados para a consolidação (paper só nas visíveis +
  contrafactual; sugestão de troca = tp2_protect). **245 testes**.
  Docs: AGENTS/TECHNICAL/ESTRATEGIAS.

### 2026-08-19 — Todas as estratégias de volta ao menu (dropdown) + kill switch reativado

- **Decisão do utilizador**: devolve TODAS as 11 estratégias ao menu,
  páginas de teste e corrida paper — `HIDDEN_STRATEGIES` e
  `MENU_HIDDEN_STRATEGIES` em `app/strategies.py` ficam **vazios** (a
  maquinaria mantém-se: ocultar = voltar a adicionar o id; nada se apaga do
  backtest). O utilizador vai retirar as piores com os dados ao vivo.
- **Dropdown ▚ ESTRATÉGIAS** no menu (todas as páginas): as 11 variantes
  vivem num painel colapsável (`.term-drop-toggle` + `.term-drop` em
  `style.css`) — toggle persistido em localStorage (default aberto), fecha
  ao clicar fora, a navegação por teclado abre-o sozinha ao chegar a um
  item lá dentro (`TERM_ORDER` em `common.js` espelha a ordem do DOM).
- **Corrida paper com 11 contas**: a curva paralela e a legenda mostram
  todas as estratégias; o auto-switch por regime passa a poder sugerir
  qualquer líder (teste atualizado: a tp2 com saldo maior é a sugerida).
- **Kill switch reativado**: estava ligado (auto_kill de sessão anterior,
  perda de papel — nada fechado hoje, 0 trades) — desligado para a execução
  real voltar a abrir (demo).
- Testes: `test_hidden_strategies_kept_in_registry_but_marked_hidden`
  reescrito (sets vazios, tudo no registo) + os testes de paper atualizados
  (todas as estratégias abrem trades de papel; close_all/protect só nas
  que honram o kind). **245 testes**. Docs: AGENTS/TECHNICAL/ESTRATEGIAS/PUBLIC.

### 2026-08-19 — Curva da referência, registo de reduções de alvo, endpoint único Fase 3

- **Mini-curva com a referência**: `fase3_eta.pace_daily` passou a incluir
  `n_ref` (sinais/dia da `nova_gestao`, mesma janela de 60 dias) — o painel
  da Fase 2 mostra as duas contas lado a lado (barra roxa auto + barra azul
  referência, com legenda) para comparar a cadência.
- **Registo das reduções de alvo**: `fase3_target_log` (config, JSON,
  últimos 20) — cada alteração ao `fase3_target` regista `{at, from, to,
  reason}` (o motivo vem do `fase3_target_reason` do POST; o botão do feed
  envia "ETA > 6 semanas (botão do feed)"). Bloco "🎯 Alterações de alvo"
  no histórico do painel + exposto em `snapshot()["fase3_target_log"]`.
- **Endpoint único** `GET /api/analysis/fase3_status` (`state.fase3_status`):
  junta ETA (curva incluída), snapshot, veredictos + histórico, target log,
  divergences paper↔MT5, real vs backtest, DD das contas (calculado no
  servidor) e auditoria de SL — o painel passou de 4 chamadas para 1
  (`refreshPhase3Instruments`).
- Testes: `test_fase3_status_endpoint` (payload completo) + alargados
  (`n_ref` na curva; `fase3_target_log` com motivo). **245 testes**.
  Docs: AGENTS/TECHNICAL.

### 2026-08-19 — Doc dedicado das estratégias

- Novo **`docs/ESTRATEGIAS.md`** — guia completo das estratégias atuais:
  o que é uma estratégia (pernas, SL-first, BE, manage_kinds, auto),
  as 3 em uso (`nova_gestao` principal · `auto_escada_3` · `tp2_protect`),
  as ocultas (backtest/contrafactual), a gestão do grupo (kinds), a
  auto-gestão (escada/trailing/confirm/time stop + ATR rejeitado), os
  resultados por janela e onde cada uma aparece. Referenciado no AGENTS.md.
  **244 testes** (só docs).

### 2026-08-19 — Mini-curva do ritmo, alvo da Fase 3 configurável (20→15), resumo com rollback

- **Mini-curva do ritmo no painel da Fase 2**: `fase3_eta.pace_daily` —
  contagem de sinais/dia da auto_escada_3 nos últimos 60 dias
  (zero-preenchida, `db.paper_trades_opened_by_day`) renderizada como barras
  com tooltip (dia · nº de sinais) — a tendência que alimenta o ETA.
- **Alvo da janela configurável** (`config fase3_target`, default 20, clamp
  10–20): quando o ETA estimar > 6 semanas, a notificação do feed ganha o
  botão "🎯 Reduzir alvo para 15" (action `fase3_target:15`, confirmação
  no frontend) → `POST /api/config {fase3_target}` → ETA e painel passam a
  contar até ao novo alvo (N/15, checklist, veredicto). Modelo
  `ConfigUpdate` + snapshot `config.fase3_target`.
- **Resumo diário com o rollback do dia**: `_fase3_mt5_line` mostra a linha
  🚀 da auto MT5 também quando a janela fechou HOJE (veredicto recente)
  mesmo com a auto já destituída — destacando o rollback (comparação em
  UTC para não falhar perto da meia-noite local).
- Testes: `test_fase3_target_config` (default/clamp/422) + alargados
  (`pace_daily` 60 dias; linha de rollback no resumo). **244 testes**.
  Docs: AGENTS/TECHNICAL/spec §B.5.4–B.5.5.

### 2026-08-19 — Resumo diário com auto MT5, alerta de ETA, histórico de veredictos

- **Resumo diário com a auto MT5** (spec §B.5.5): quando a Fase 3 está a
  correr (auto promovida + snapshot), o resumo diário passa a incluir a
  linha 🚀 com N real e P&L real das trades fechadas desde o início da
  janela e o estado (em curso x/10 ou veredicto) — `state._fase3_mt5_line`.
- **Alerta de ETA no feed** (`_check_fase3_eta_alert`, cooldown 24h): o
  `fase3_eta` passou a comparar o ritmo com o MÊS ANTERIOR (`pace_prev`,
  `pace_change_pct`, janela 30–60 dias) e a avisar quando a janela estimar
  **> 6 semanas** ou o ritmo mudar **≥ 50%** face ao mês anterior (ETA
  recalculado com o ritmo novo) — feed/Telegram.
- **Histórico de veredictos**: `fase3_verdicts` (config, JSON, últimos 20)
  — cada janela de validação APÊNDICE o seu veredicto; reabrir a janela
  (nova promoção) limpa só o veredicto ATUAL (`fase3_verdict`), o histórico
  nunca se apaga (auditoria da evolução das auto_*). Painel Fase 3 ganhou o
  bloco "📚 Histórico de janelas de validação"; exposto também em
  `snapshot()["fase3_verdicts"]`.
- Testes alargados: `test_fase3_eta` (pace_prev/eta_days) e
  `test_fase3_validation_rollback` (histórico preservado na reabertura).
  **243 testes**. Docs: AGENTS/TECHNICAL/spec §B.5.4–B.5.5.

### 2026-08-19 — ETA dos 20 sinais + veredicto automático da janela de validação

- **ETA no painel Fase 3**: `snapshot()["fase3_eta"]` calcula o ritmo de
  sinais/dia da `auto_escada_3` (trades de papel abertas nos últimos 30
  dias, `db.paper_trades_opened_since`) e a data estimada em que N chega a
  20 (`done=True` quando já lá está; `eta=None` sem ritmo ainda) — o painel
  mostra "Ritmo · ETA 20" com a data e o ritmo ao lado do contador N/20.
- **Veredicto automático da janela de validação** (§B.5.4 automatizado):
  `state._check_fase3_validation` (corre no tick) quando a auto está
  promovida e 10 trades reais fecharam desde `promoted_at` — calcula PF
  real da janela, divergências estruturais (paper↔MT5 com saída diferente
  ou papel ainda aberto) e posições sem SL; **rollback automático para a
  nova_gestao** (PF < 1.0, ≥ 2 divergências estruturais ou qualquer posição
  sem SL) ou **manutenção**, com notificação no feed/Telegram e o veredicto
  persistido em `fase3_verdict` (config) + bloco no painel Fase 3
  (antes/depois via `snapshot_before`). Corre UMA vez por janela; reabre
  quando a auto for promovida de novo (novo snapshot).
- **Testes**: `test_fase3_eta` (ritmo → data estimada) e
  `test_fase3_validation_rollback` (10 trades perdedoras → rollback +
  veredicto exposto no /api/status). **243 testes**. Docs: AGENTS/TECHNICAL/spec.

### 2026-08-18 — Sweep fino do trail, resumo ATR no painel, snapshot de promoção

- **Sweep fino do trail fixo** (`app/sweep_atr.py --trail 1.6,1.8,2.0,2.2,2.4`)
  nas escada_3/5 no histórico completo: plateau MONOTÓNICO sem pico isolado
  (escada_3: PF 2.37/2.24/2.13/2.04/2.00, +8 618 → +6 157 €, DD 35–42%; na
  janela antiga todos positivos: 1.55→1.31). **2.0 mantém-se** (interior,
  vizinhos a ~5% de PF, coincide com a paciência do grupo 1.0–2.3 distâncias
  de TP2); o melhor (1.6) está na borda da grelha e aproxima-se da zona
  inflada (< 1.0). 1.8 fica como candidato se um dia se quiser apertar.
- **Resumo do sweep ATR no painel Fase 3**: bloco "📊 Sweep do trail (estudo
  18/08)" com baseline vs atr_k (2.13/+7 014 € vs 1.42/+2 483 € vs 0.64/
  −2 053 €) e o trail fino — a decisão documentada ao lado dos gates.
- **Snapshot da janela de validação ao promover**: `POST /api/config` com
  uma auto com auto-gestão grava `fase3_snapshot` na config (stats da
  corrida paper, DD das contas via curva 30d, divergences paper↔MT5,
  trades reais fechadas e nº de entradas da auditoria SL) — exposto no
  `/api/status` e mostrado no painel; a janela compara antes/depois (§B.5.4).
- **241 testes verdes** (1 novo: snapshot de promoção) · docs atualizadas.

### 2026-08-18 — Sweep ATR (rejeitado), aviso de risco SL, botão promover Fase 3

- **Sweep da sensibilidade do ATR** (`app/sweep_atr.py`): atr_k
  1.0/1.5/2.0/3.0/5.0 × period 7/14/30 nas auto_* em 3 janelas (atual,
  histórico completo, out-of-sample antigo) — **resultado honesto e
  decisivo: o ATR é estritamente PIOR que o trail fixo 2.0 em TODOS os
  combos/janelas**. `auto_escada_3` no histórico completo: baseline PF 2.13
  (+7 014 € · DD 38%) vs atr_k=1.0/p=14 PF 1.42 (+2 483 €) e atr_k≥2 PF
  <0.7 (perde); no período antigo baseline PF 1.41 vs atr_k=1.0 PF 0.93
  (negativo). Porquê: o ATR do ouro é muito maior que o recuo de 8 pips —
  o chão ATR alarga o trail e blunts o fecho do lucro. **`atr_k` fica
  desligado** (trail fixo 2.0 congelado); o código continua disponível.
- **Aviso de risco da auditoria de SL**: modify da auto-gestão no MT5
  recusado (10016) ou arredondado (clamped) pelo broker → evento
  `auto_sl_warn` do `update` → notificação `risk` no feed/Telegram (dedup
  por trade+perna+alvo — a recusa não marca throttle, o aviso não spamma).
  Testes: recusa (1 aviso refused com retcode) e clamp (aviso clamped, SL
  aplicado = valid).
- **Botão "⬆️ Promover a principal (Fase 3)"** no painel Fase 3: aparece
  quando os gates estão verdes e a auto_escada_3 não é a principal —
  POST /api/config com confirmação (como nas páginas de teste).
- **240 testes verdes** (1 novo: clamped) · docs atualizadas
  (AGENTS/TECHNICAL/spec §B.5.3 itens 5 e 7).

### 2026-08-18 — Fase 3: ATR alinhado no backtest/paper, painel pronto-a-arrancar, auditoria de SL

- **Trailing adaptativo por ATR alinhado no backtest e na corrida paper**
  (mesma fórmula do MT5: `trail_dist = max(trail_factor × dist, atr_k ×
  ATR)`, sem look-ahead — ATR das velas/minutos ANTERIORES ao evento):
  `simulate_signal` ganhou `atr_k`/`atr_period` (`_candle_atr` em
  backtest.py), `run_analysis`/`sim_params` aceitam-nos, e o `PaperRace`
  calcula um ATR rolante por trade com baldes de minuto (`_paper_atr` — o
  minuto atual nunca conta para o ATR que ele próprio usa). Sem dados
  degrada para o trail fixo; `atr_timeframe` continua só do MT5.
- **Painel "🚀 Fase 3 — pronto a arrancar"** na página principal: checklist
  dos gates do spec (N≥20 · PF≥1.5 · P&L≥50% · DD≤125% da referência ·
  janela de validação), com os instrumentos num só sítio — counts do
  divergence paper↔MT5 e do real vs backtest, DD das contas (curva) e a
  auditoria de SL. Refresh 60s (não em cada snapshot).
- **Auditoria dos três níveis de SL da auto no MT5**: `_auto_move_sl`
  regista cada tentativa (target→valid→applied, clamped, retcode
  10009/10016) persistida na config `autosl:<trade_id>` (sobrevive a
  restarts) — endpoint `GET /api/analysis/auto_sl_log` + tabela no painel;
  é o que a janela de validação usa para auditar modifies recusados.
- **239 testes verdes** (3 novos: ATR backtest, ATR paper, auto_sl_log).

### 2026-08-18 — Fase 3: teste E2E broker fake, ATR adaptativo, auto paper↔MT5

- **Teste E2E do caminho real do `MT5Engine` com um broker fake ESTADO**
  (`dry_run=False`): abre a trade com auto_mgmt, o TP1 fecha pelo broker
  (deal de saída em `history_deals_get`) → escada sobe o SL para o nível do
  TP1 → trailing segue o pico → time stop fecha as restantes ao mercado.
  O log regista os **retcodes reais (10009)** de cada modify aceite.
- **Bug corrigido (três níveis de SL)**: `_auto_move_sl` gravava o SL na BD
  mesmo quando o broker o recusava (10016) — agora o SL só se torna
  *applied* quando o modify é confirmado (10009/0); `_modify_sl` devolve o
  retcode. Teste dedicado: recusa 10016 → BD intacta, throttle não marcado.
- **Trailing adaptativo por ATR** (spec §B.5.3, melhoria pós-validação):
  `MT5Provider.copy_rates` (M1/M5 via `copy_rates_from_pos`) + `_atr` no
  engine; `trail_dist = max(trail_factor × dist, atr_k × ATR)` — só MT5 por
  agora (backtest/paper mantêm o trail fixo). Parâmetros `atr_k`/
  `atr_period`/`atr_timeframe` no registo e no `auto` do state; sem velas
  degrada para o trail fixo. Testes de normalização, ATR e floor/fallback.
- **Divergence report auto paper vs auto MT5** (SPEC §B.5.4): endpoint
  `GET /api/analysis/paper_vs_real_auto` + tabela "🔄 Auto paper vs Auto
  MT5 (Fase 3)" no Relatório — mesma decisão de saída, Δ preço (pips),
  Δ timing (min) e P&L por sinal; vazio até a Fase 3 arrancar.
- **`insert_trade` passou a persistir `exit_reason`** (a coluna existia mas
  o INSERT dropava o valor — o `real_vs_backtest`/paper_vs_real liam None).
- **236 testes verdes** (6 novos) · docs atualizadas (AGENTS/TECHNICAL).

### 2026-08-18 — Fase 3: código da auto-gestão no MT5Engine (desligado)

- **`MT5Engine` ganhou a auto-gestão da Fase 3** (spec §B.5), **desligada**
  por omissão: `update(now, exit_at_tp, auto)` recebe os parâmetros da
  estratégia (`stair_levels`/`stair_from`/`confirm_filter`/`trail_factor`/
  `time_stop_minutes`) mas o `state.tick` só os passa quando a estratégia
  ativa os define — a `nova_gestao` não tem nenhum (auto_mgmt off), por isso
  nada muda até uma `auto_*` ser promovida. O `SimEngine.update` aceita o
  kwarg e ignora-o (em sim/paper a auto-gestão é do `PaperRace`).
- **`_update_real` com a sequência do backtest/paper**: TP_k confirma →
  **escada** (`move_leg_sl` das pernas seguintes para o NÍVEL do TP_k),
  perna k leva SL → **filtro de confirmação** (pernas > k fecham ao
  mercado), após o TP1 → **trailing** (`SL = pico − trail_factor ×
  dist(entrada,TP2)`, pico dos ticks ANTERIORES — sem look-ahead) e
  **time stop** (pernas N min após TP1 fecham ao mercado).
- **Três níveis de SL**: `_valid_sl` limita o alvo teórico pela distância
  mínima do broker (`stops_level`/`freeze_level` do símbolo) — um modify
  demasiado perto do preço seria recusado (10016). **Throttling de modify**:
  só sobe se melhorar ≥ 1 pip e ≤ 1×/min por perna (estado por trade em
  memória — após restart recomeça do preço atual, documentado).
- 4 testes novos (`test_mt5.py`): escada sobe o SL ao TP1, throttling 60s,
  trailing segue o pico sem look-ahead (e nunca desce), valid_sl respeita o
  stops_level.
- Suite: **230 testes** (+4).

### 2026-08-18 — Menu limpo (decisão das estratégias antigas) + SPEC único + SPEC v3

- **Menu/páginas de teste**: `nova`, `nova_gestao_3` e `tp2_gestao` saíram
  do menu e das páginas de teste (decisão do utilizador — 18/08). Só a
  `nova_gestao` fica como referência (compare + corrida paper).
  `HIDDEN_STRATEGIES` passou a incluir as antigas; a `nova` ficou em
  `MENU_HIDDEN_STRATEGIES` — **continua a abrir trades de papel** porque o
  contrafactual "sem gestão" do histórico de trades (`_trade_view`) lê a sua
  conta paper (escondê-la da corrida não pode cortar esse contrafactual).
  Menu agora: PRINCIPAL · Auto escada 3 · TP2 + protect · DIAGNÓSTICO ·
  HELP. O exemplo do fluxo (trace) mudou da `tp2_gestao` para a `tp2_protect`
  (a página TP2 que fica); o "✓ melhor" marca o PRINCIPAL quando a
  `nova_gestao` vence o compare.
- **SPEC único**: o `docs/SPEC-paper-fase2.md` foi **fundido** no
  `docs/SPEC-auto-gestao.md` (Parte B: config congelada, sensibilidade
  1.5–2.5, critérios revistos, instrumentos de validação, protocolo da Fase
  3) e **apagado** — este ficheiro é agora o único spec do projeto.
- **SPEC v3 (rascunho, §B.7)**: gates para conta real — Fase 3 demo aprovada
  primeiro, 3–4 semanas estáveis, decisão explícita do utilizador, revisões
  periódicas (PF ≥ 1.5 em janela deslizante, DD ≤ 125%, kill diário sempre
  ligado) e a linha vermelha do que nunca se faz em real. Sem código.
- Suite: **226 testes** (+0 — os testes existentes foram atualizados para o
  novo conjunto de estratégias visíveis).

### 2026-08-18 — SPEC atualizado antes da Fase 3 (verificação)

- **`docs/SPEC-paper-fase2.md`**: estado → Fase 2 em curso (N=0/20); kill
  diário e resumo diário 🏁 documentados na config congelada; divergence
  report persistente (§3.4) e **real vs backtest** (§3.5) como instrumentos de
  validação; **novo §7 — Protocolo da Fase 3 (MT5 demo)**: pré-requisitos
  (critérios §3.2 com N≥20), as peças do MT5Engine que já existem
  (`move_leg_sl`/`close_leg_at_market`/`_move_sl_breakeven`), o trabalho
  pequeno que falta (ligar escada/trailing ao `_update_real`, throttling de
  modify, três níveis de SL), janela de validação de 10 trades + rollback, e
  a regra "nunca real sem instrução explícita".
- **`docs/SPEC-auto-gestao.md`**: estado → Fase 1 concluída · Fase 2 em curso;
  `trail_ratio` → `trail_factor` também nas secções vivas; Fase 2 marcada
  (port do paper, páginas, kill diário, divergências); Fase 3 aponta para o
  protocolo do v2; perguntas em aberto marcadas resolvidas (stair_from,
  paralelo, trail 2.0) vs em aberto (menu das antigas).

### 2026-08-18 — Kill diário no paper + resumo diário da corrida + real vs backtest

- **Kill diário (`daily_stop_pct`) na corrida paper**: quando o P&L realizado
  do dia da conta virtual de uma estratégia ≤ −saldo×limite/100, o sinal NÃO
  abre em papel nessa conta — espelho exato da produção
  (`risk.engine.daily_loss_exceeded`; `apply_daily_stop` no backtest). Aplica-se
  a TODAS as contas virtuais (cada uma é candidata a promoção); contador
  `daily_stop_skipped` exposto no snapshot por estratégia.
- **Resumo diário com a corrida da Fase 2**: o `daily_summary` (à hora
  `summary_time`) passa a incluir o bloco 🏁 com saldo e PF da
  `auto_escada_3` vs `nova_gestao` e quantos sinais faltam para os critérios
  (N ≥ 20 · PF ≥ 1.5) — feed + Telegram.
- **`GET /api/analysis/real_vs_backtest`** + tabela "⚡ Real vs backtest" no
  Relatório: cada trade REAL fechada da demo vs o backtest do mesmo sinal na
  estratégia principal — Δ entrada (preço real vs fill do sim: entrada ±
  spread/2 ± slippage), Δ saída (fecho ponderado vs o nível da razão TP_k/SL),
  atraso sinal→abertura (sim assume 60s) e P&L de cada lado (SPEC v2 §3.4).
- Suite: **226 testes** (+1 kill diário paper, +1 real_vs_backtest).

### 2026-08-18 — Fase 2 em produção: painel de progresso, divergências no Relatório, CSV, estudo tp2, MT5 real

- **Servidor local em `mt5-real`** (`MT5_DRY_RUN=0`, como na VPS): a conta
  demo 5054644473 executa ordens reais (demo) — confirmado `connected` +
  `trade_allowed`.
- **Painel Fase 2 na página principal**: saldo/PF da `auto_escada_3` vs
  `nova_gestao`, sinais resolvidos e barra de progresso para os critérios do
  SPEC v2 (N ≥ 20 · PF ≥ 1.5), com veredicto (falência precoce PF < 1 com
  N ≥ 10 / critérios atingidos / em curso).
- **Tabela de divergências na tab Relatório** (`GET /api/paper/divergences`):
  auto vs referência por sinal — razão e P&L lado a lado + motivo ("a
  referência saiu por gestão do grupo e a auto continua", "saída diferente").
- **Botão ⬇️ CSV nas páginas de teste da Fase 2**: exporta as trades de
  papel da estratégia + a referência `nova_gestao` (preços, SL, TPs, lotes,
  timestamps, razão, P&L) para análise fora da dashboard.
- **Estudo tp2 (formato atual, 30d)**: tp2 puro PF 0.86 / −151 €; **tp2 +
  protect PF 2.65 / +1 721 €** (o protect recupera ~86% do caminho até à
  gestão completa PF 2.93 / +2 010 €); `nova_gestao` PF 3.22 / +2 309 €.
  Conclusão: sair cedo no TP2 só se justifica COM a proteção do grupo, e
  mesmo assim a gestão completa ganha mais — a `nova_gestao` continua a
  melhor escolha. (Histórico completo: tp2 puro 0.50/−3 791 € → +protect
  0.82/−1 422 € → +gestão 0.89/−834 € — o regime antigo perde para tudo.)
- Suite: **226 testes** (+1 kill diário paper, +1 real_vs_backtest).

### 2026-08-18 — Fase 2: paper com auto-gestão + layout novo (nav em cima, estado em baixo)

- **`trail_ratio` → `trail_factor`** (código, testes e docs): múltiplo
  adimensional da distância entrada→TP2 (2.0 ≈ 8 pips em gold) — o nome
  reflete a semântica multiplicativa (SPEC v2 §2.1).
- **Sweep de sensibilidade 1.5–2.5** (`auto_escada_3`, §2.2 do SPEC v2):
  plateau monotónico — PF 1.97–4.69 no atual, 1.28–1.57 no antigo, P&L
  positivo em todos, sem pico isolado (logo sem sobreajuste); **2.0
  congelado**. Resultados guardados no spec (§B.2, agora `docs/SPEC-auto-gestao.md`).
- **Testes de regressão §4** (SPEC v2): look-ahead buy/sell (a vela não
  beneficia do próprio high/low), TP/SL na mesma vela (SL primeiro),
  múltiplos TPs na mesma vela, escada vs trailing (a escada tem prioridade),
  risco por sinal (2/3/5 pernas arriscam o mesmo total) + 2 testes do tick
  do paper (trail sem look-ahead, escada no nível do TP1).
- **Auto-gestão portada para o tick do `PaperRace`**: escada, trailing,
  confirmação e time stop a funcionar ao vivo na corrida paper (espelho do
  backtest, sem look-ahead; estado do pico em memória, recomeça do preço
  atual após restart).
- **`auto_escada_3` e `tp2_protect` saíram de `HIDDEN`** (Fase 2): página de
  teste + menu + corrida paper (cores `#a3e635`/`#10b981`); o resto
  (`auto_2/3/5`, `auto_escada_5`, `tp2`) continua oculto.
- **Divergence report**: quando a auto e a referência (`nova_gestao`)
  fecham diferente no mesmo sinal — ou a referência fecha por gestão do
  grupo e a auto continua — o bot emite notificação `divergence` ⚖️ no feed
  (+ Telegram se configurado), dedup one-shot por sinal. É a comparação
  paper vs backtest do SPEC v2 §3.4 automatizada.
- **Layout novo**: a navegação subiu para uma **barra fixa no topo** (itens
  horizontais) e o terminal de estado (`▚ STATE`: badges, preços com
  **barrinha da variação % do dia** + tooltip, relógio, kill switch, Sair)
  desceu para uma **barra fixa em baixo** — sem terminais laterais (o
  conteúdo vive entre as duas, `--nav-h`/`--foot-h`, nunca coberto em
  qualquer zoom; ≤960px o ☰/▦ abrem em overlay). Aplicado às 4 páginas
  (index/teste/diagnóstico/help).
- Suite: **223 testes** (era 214 — +6 §4 backtest, +3 tick/divergence
  paper, +1 registo, e o `test_strategy` atualizado para a Fase 2).

### 2026-08-18 — auto-gestão: Fase 0.5 + Fase 1 (testes, registo, mineração, sweep)

- **Testes de regressão da auto-gestão** (6 novos em `test_backtest.py`):
  escada fecha com lucro no nível do TP (buy e sell), escada sobe o SL ao
  TP2, filtro de confirmação fecha as longínquas ao mercado, trailing segue
  o pico, time stop fecha após N minutos.
- **Bug corrigido (importante)**: o outcome de uma perna que fechava no
  degrau da escada (acima da entrada, com lucro) era marcado como `loss` —
  agora `win`. E o **trailing tinha look-ahead**: o SL era levantado com o
  high da própria vela que o atravessava (fills otimistas que inflavam o
  backtest — whipsaws convertidos em lucro). Agora o SL usa o pico das
  velas anteriores. Sem este fix, o período antigo (onde tudo perde) dava
  PF 2.
- **Registo**: `app/strategies.py` ganhou as 5 variantes `auto_*` (HIDDEN
  até aprovação) com `stair_levels`/`stair_from`/`confirm_filter`/
  `trail_ratio`/`time_stop_minutes` no `_strat`/`sim_params`; `run_analysis`
  e `run_month_sim` passam os parâmetros novos + `until` (out-of-sample).
- **`app/mine_management.py`** — mineração empírica: alinha as mensagens de
  gestão reais do grupo com o price action → o grupo tolera 1.0–2.3
  distâncias de TP2 de recuo do pico e espera p90≈15h após TP1.
- **`app/sweep_auto.py`** — sweep das 5 variantes × grelha nos 30 dias + no
  período antigo (out-of-sample), com `nova_gestao` como referência
  (validada: PF 3.28 / +2 369 € ≈ o A/B/C da dashboard). Resultado: escada/
  confirm/time stop sem edge sozinhos; o trailing é o mecanismo
  (trail_ratio 2.0 = valor honesto); `auto_escada_3` recomendada (PF 3.11
  atual / 1.41 antigo vs referência 3.28 / 0.63). Ver `docs/SPEC-auto-gestao.md` 6.4.
- Suite: 214 testes (era 207 — +6 backtest, +1 API do registo auto).

### 2026-08-18 — dashboard dividida em páginas HTML separadas + boot só na primeira visita

- **Split da dashboard** (a `index.html` tinha ~3100 linhas): CSS extraído
  para `app/static/style.css` e o JS comum (helpers, boot, WebSocket, chrome
  global — badges, statusbar, terminais, toasts, menu) para `app/static/common.js`.
- **Páginas novas servidas pelo `main.py`** (todas com auth via middleware,
  sem sessão → login): `index.html` (`/`), `teste.html` (`/teste?s=<id>`),
  `diagnostico.html` (`/diagnostico`) e `help.html` (`/help`). O menu passou
  a usar links `<a href>` reais em vez de botões — navegação com URL própria.
- **Boot animation só na primeira visita**: o overlay "INIT PROJECT NEXGEN…"
  corria em todas as páginas ao trocar de página; agora corre UMA vez por
  sessão (`sessionStorage.nexgenBootDone`), logo após o login → index, e é
  limpo no logout para voltar a animar no próximo login.
- Fix: o campo "Saldo inicial" da página de teste estava `invalid`
  (`step=500` com `min=100` → 1000 nunca era múltiplo) — agora `step=any`.
- Suite: 207 testes (era 206 — +1 contrato das páginas separadas em
  `tests/test_frontend.py`).

### 2026-08-18 — troca de conta demo MT5 + limpeza do histórico antigo

- **Nova conta demo**: `MT5_LOGIN=5054644473` (MetaQuotes-Demo, senha no
  `.env`, backup em `backups/env-20260818-204851.bak`) substitui a
  5054586550. Servidor reiniciado com `MT5_DRY_RUN=0`; `/api/status`
  confirma `login: 5054644473`, `mode: mt5-real`, saldo 1000 €.
- **Limpeza da BD** (backup `backups/signalbot-20260818-205233-pre-account-switch-clean.db`):
  apagadas as 5 trades + 21 posições fechadas da conta antiga (dashboard
  começa limpa na conta nova), os 71 pontos `__real__` de
  `paper_balance_history` (saldo real da conta antiga na corrida paralela
  — a série recomeça no saldo novo) e os 2 sinais `executed` órfãos
  (4167/4170) passaram a `historical`.
- ⚠️ AutoTrading do terminal MT5 fica OFF após troca de conta — ligar o
  botão "Algo Trading" no terminal (confirmado `trade_allowed: true`).

### 2026-08-18 — overhaul visual da dashboard (polish + zoom)

- Sistema de **design tokens** de espaçamento/raio (`--sp-1..6`, `--r-sm/md/lg`)
  aplicado às componentes base (wrap, stats, cards, controls, statusbar) —
  ritmo de espaçamento consistente em todo o layout.
- **Zoom/ecrãs pequenos**: o header (título `h1` + subtítulo) deixou de
  partir para várias linhas (nowrap; subtítulo esconde-se ≤480px, os badges
  já mostram modo/estratégia/execução); a **corrida paralela** empilha o
  canvas + legenda verticalmente ≤700px em vez de espremer (e a legenda
  ganhou `min-width` próprio no desktop).
- **Toasts persistentes** (ex.: "AutoTrading DESLIGADO") ganharam botão **×**
  de dispensar com snooze de 5 min (volta se o problema persistir) e, no
  telemóvel, passam de caixa a tapar meio ecrã para uma faixa fina em baixo.
- Limpeza visual: scanlines CRT mais subtis (0.04 → 0.028) e reticle de
  fundo mais discreto (0.13 → 0.10) — o cyberpunk mantém-se, menos ruído.
- Bug fix: `var(--fg)` inexistente no seletor do horizonte da corrida
  (ficava com a cor do texto indefinida) → `var(--text)`.

### 2026-08-18 — página HELP filtra estratégias ocultas

- O registo central marca agora cada estratégia com o flag **`hidden`**
  (`strategies_list()` — tp2/tp2_protect = True) e a página **HELP** filtra
  `!s.hidden` — deixou de mostrar as estratégias já retiradas do menu e das
  páginas de teste (que continuam só no backtest/compare histórico).
- `test_strategies_registry_endpoint` + `test_help_filtra_estrategias_ocultas`
  guardam o contrato. **206 testes verdes** (era 205).

### 2026-08-18 — corrida paralela: horizonte persiste na config

- O seletor de horizonte da corrida paralela (24h/7d/30d) agora **persiste na
  config** (`paper_race_hours`, default 24, validado 24..720) como o
  `teste_days` — sobrevive a reloads da dashboard; o boot sincroniza o
  seletor a partir da config e o onchange grava + recarrega.
- `test_config_persists_paper_race_hours` (API) + contrato estático no
  frontend. **205 testes verdes** (era 204).

### 2026-08-18 — corrida paralela: seletor de horizonte (24h/7d/30d) + stats por estratégia

- **Seletor de horizonte**: a mini-curva "🏁 Corrida paralela" ganhou um
  `<select>` (24h / 7 dias / 30 dias) — `loadPaperRaceMini` lê-o e passa
  `hours` ao `/api/paper/history`; o backend aceita agora até **720h** (era
  168h = 7 dias).
- **Stats na legenda**: cada estratégia mostra agora **trades fechadas + win
  rate** (do `paper_trade_stats`) ao lado do saldo; a linha de referência
  real (⚡ MT5 real) continua sem stats (é só o dinheiro a sério).
- `tests/test_frontend.py` ganhou o contrato do seletor/stats. **204 testes
  verdes** (era 203).

### 2026-08-18 — corrida paralela: teste da conta da principal + linha de referência real

- **Regressão**: `test_paper_main_account_same_signal_id_open_close` garante
  que a conta de papel da principal abre e fecha a MESMA trade do sinal com o
  mesmo `signal_id` das estratégias de teste (e que `close_all` fecha a de
  papel da principal, com `exit_reason` registado) — a corrida paralela nunca
  diverge da real.
- **Linha de referência real**: além das 4 contas virtuais, a mini-curva
  "🏁 Corrida paralela" mostra agora o **saldo real da MT5** como linha
  separada (tracejado claro, etiqueta "⚡ MT5 real · conta real") — o tick
  grava o saldo real sob a estratégia reservada `__real__` em
  `paper_balance_history` e `GET /api/paper/history` devolve-a com
  `is_real: true`; a dashboard distingue-a das virtuais (`is_real`).
- **203 testes verdes** (era 201; +1 API +1 frontend).

### 2026-08-18 — corrida paralela: a principal passa a ter conta virtual própria (1000 €)

- **Bug**: na mini-curva "🏁 Corrida paralela", a caixa da estratégia ativa
  ("TP1..TP5 + gestão 🧠") mostrava o **saldo real da conta MT5** (973.49 €)
  em vez da conta de testes — enquanto as outras 3 caixas mostravam a sua
  conta virtual de 1000 €. A comparação ficava maçãs-com-laranjas e a curva
  da principal saltava quando o saldo real mudava.
- **Fix**: `open_for_signal` deixou de excluir a principal — agora **todas**
  as estratégias visíveis (incluindo a ativa) abrem a mesma trade em papel
  com a sua própria conta virtual de 1000 €. `_record_balance_snapshots` e
  `paper_status` usam a conta virtual da principal (não o saldo real), com
  `ensure_paper_account` a criar as contas em falta.
- **Alerta de liderança coerente**: `_check_paper_race_leader` e
  `renderPaperRaceWarn` passaram a comparar virtual vs virtual (a principal
  agora também tem conta virtual) em vez de virtual vs saldo real.
- **Dados limpos**: apagadas as ~1200 linhas de `paper_balance_history` da
  principal que guardavam saldos reais (backup `signalbot-*-pre-main-paper-
  account.db`) para a curva recomeçar em 1000 €. 201 testes verdes.

### 2026-08-18 — suite determinística: isolamento por teste + polling (fim da flakiness)

- **Causa da flakiness**: os testes de `test_api.py` partilham um único
  `TestClient`/estado (fixture module-scoped) e usavam `time.sleep(1.3)` a
  competir com o loop de fundo de 1s (janela de edição) — sob carga o tick
  atrasava e o teste falhava de forma intermitente; uma falha a meio deixava
  config alterada (edit_window=0, mode=auto) e contaminava os seguintes.
- **Isolamento por teste**: novo fixture autouse `_isolated` em `test_api.py`
  — repõe a config base (mode manual, edit_window 60, max_positions 5,
  risk_pct 2, strategy nova_gestao, killswitch off) e fecha trades abertas
  ANTES de cada teste, mesmo que o anterior tenha falhado (sem cascata).
- **Polling em vez de sleeps**: helper `_wait_for()` + `_sig_status()` — os
  4 testes da janela de edição passaram de `time.sleep(1.3)` para polling do
  `/api/status` (sem corrida com o tick).
- **Asserts invariantes em vez de contagens transitórias**: o snapshot só
  expõe pernas ABERTAS e o tick do mercado congelado fecha logo as pernas
  cujo SL foi movido para/ acima da entrada (`protect`/`out at BE`/`TP1
  HIT`); os testes de gestão passaram a ler a BD (`db.positions_for_trade`)
  e a afirmar o invariante (perna 1 fechada com razão + SL movido) em vez do
  nº de pernas abertas.
- **Corrida paper**: `_paper_trade_for_signal` ganhou `include_closed=True`
  para o teste do protect — a trade de papel pode já ter sido fechada pelo
  tick quando o teste a lê.
- `tests/test_frontend.py` ganhou o e2e estático do fluxo da página de teste
  (loadTeste → month com testeSimQS + walkforward + onchange dos seletores
  de risco/saldo). **201 testes verdes** (era 200).

### 2026-08-18 — tab Edge removida; walk-forward + estudos de risco nas páginas de teste

- **A tab "🎯 Edge" foi removida** do menu (botão + card + JS morto): a
  simulação/comparação/P&L por mês e perna/corrida paralela já estavam
  duplicadas nas páginas de teste, e o walk-forward já tinha um substituto
  automático no alerta de regime.
- **Walk-forward** (`/api/analysis/walkforward`) moveu-se para as **páginas
  de teste** (bloco "🧪 Walk-forward").
- **Estudos de risco** (seletores risco %/saldo) moveu-se para as páginas de
  teste: o saldo default é **1000** (o saldo próprio da conta paper — nunca o
  saldo da MT5), via `testeSimQS()`; os valores persistem em `edge_risk`/
  `edge_bal` como antes.
- `tests/test_frontend.py` reescrito (4 testes): as chamadas a
  `/api/analysis/month` têm de usar `testeSimQS()`, o default de saldo é
  1000, a tab Edge não pode voltar e o walk-forward/seletores têm de existir
  na página de teste. 200 testes verdes.

### 2026-08-18 — páginas de teste usam saldo próprio (1000 €) + regressões de contract

- **Fix de UI**: as páginas de teste/HELP mostravam a simulação a começar com
  o saldo da conta MT5 (ex.: 973.49 €) em vez do saldo próprio de cada
  estratégia (1000 €) — o `/api/analysis/month` usava `balance=0` = saldo
  atual. Agora as chamadas das páginas de teste passam `balance=1000`
  (consistente com a conta paper de 1000 € e com o intro "começa com 1.000 €").
  A tab Edge mantém `balance=0` (saldo atual / seletor).
- **Limpeza**: apagada a trade fantasma `t-5659efb8fbb6` (sinal 4159, retcode
  10027 — aberta/fechada no mesmo segundo com pnl 0) + as 4 posições zero —
  o sinal 4159 continua `failed` no histórico.
- +2 testes de regressão: trade de prata (XAGUSD) usa contract 5000, e teste
  end-to-end da corrida paper que falha se o saldo virtual inflacionar. 198
  testes verdes.

### 2026-08-18 — regressões de frontend (páginas de teste vs tab Edge)

- Novo `tests/test_frontend.py` (2 testes estáticos sobre o HTML da dashboard):
  garante que **todas** as chamadas a `/api/analysis/month` das páginas de
  teste/HELP levam `balance=1000` (o bug do "saldo inicial = saldo da MT5"
  não volta) e que a tab Edge continua com o seletor de saldo (`edgeSimQS`)
  em vez de um 1000 fixo. 198 testes verdes.

### 2026-08-18 — fix do P&L da corrida paper (contract por símbolo)

- **Bug**: o SimEngine e a corrida paper calculavam o P&L de TODAS as trades
  com `CONTRACT_SIZE=100000` (forex). Em XAUUSD (contract 100) isso dava P&L
  1000× inflacionado — as contas paper `nova`/`tp2_gestao`/`nova_gestao_3`
  mostravam 66k/38k/28k em vez de ~1000 €. Ex.: um TP1 de 3 USD × 0.01 lots
  dava 3000 € em vez de 3 €.
- **Fix**: `engine._contract(symbol)`/`_pip_size(symbol)` (XAUUSD 100,
  XAGUSD 5000, forex 100000) partilhados com `paper.py` — o P&L e o
  dimensionamento das trades simuladas/de papel agora usam o contract do
  símbolo, igual ao backtest e ao MT5 engine.
- **Limpeza**: contas paper repostas a 1000 € e trades/posições/histórico de
  saldo das estratégias de teste limpos (a principal `nova_gestao` usa o
  saldo real e não foi tocada). Backup prévio em
  `backups/signalbot-*pre-paperfix-reclassify.db`.
- +2 testes de regressão (contract por símbolo + trade de ouro não inflada).
  194 testes verdes.

### 2026-08-18 — classificador de gestão estendido + reclassificação em massa

- Novos padrões de gestão reconhecidos (antes ficavam `failed`):
  `Close TP2..TP6 here/when happy` → `close_tp` (fecha só a perna N),
  `TP N SMASHED/KISSED/tapped` → `tp_hit`, `Out at entry/breakeven` /
  `Close this at breakeven` → `close_all` (BE), `SL adjusted/edited/hit` →
  `move_sl`/`sl_hit`, `Set breakeven` → `protect`.
- **Reclassificação em massa na BD**: das 465 mensagens `failed`, 61 passaram
  a `management` (depois normalizadas a `historical` pelo replay), 100
  conversas foram movidas para `ignored_messages`, e 304 sinais reais com
  erro de parse (typos, Buy/Sell Stop 💣, zonas de reentrada) continuam
  `failed`. Nenhum sinal apagado tinha trade ligada (verificação prévia).
- Testes contra a BD real em `tests/test_management.py`; suite em 194.

### 2026-08-18 — histórico: msgs 4157/4169 reclassificadas para conversa

- As mensagens "Again act to fast after TP1 / So I'll registered that as SL"
  (4169) e "Okay team… Gold is currently moving sideways" (4157) foram
  reclassificadas na BD de `failed` para **ignoradas** (auditoria "🚫
  Ignoradas") — backup prévio em `backups/signalbot-*pre-reclassify.db`.

### 2026-08-18 — sinal completo executa já + janela 8s + fix de conversa "TP1/SL" soltos

- **Executar no primeiro parse válido**: um sinal de Telegram completo (com
  entrada+TP+SL) executa imediatamente em auto (ou fica pendente em manual),
  sem esperar a janela de edição — fecha o caso do "TP1 veio em segundos"
  (o sinal das 14:33 esperou 81s e entrou 41s depois do TP1 HIT). A janela
  só é usada quando a primeira versão está incompleta.
- **Janela de edição 6s → 8s** (só para mensagens incompletas; cada edição
  volta o relógio para 8s).
- **Fix de reconhecimento**: detalhes de sinal (TP/SL/entry/target) agora só
  contam como sinal com um **número** a seguir — "after TP1"/"as SL"
  (conversa do grupo) deixam de virar `failed` e passam a ser auditadas como
  ignoradas. Caso real: "Again act to fast after TP1 / So I'll registered
  that as SL" (msg 4169).
- 186 testes verdes.

### 2026-08-18 — janela de edição do Telegram reduzida para 6s

- A espera por edições do grupo passou de **30s para 6s** (config
  `edit_window_seconds`, default em `config.py` + valor na BD atualizado
  para 6). O slider da dashboard agora permite descer até 1s (antes mínimo
  10s). Docs atualizadas (AGENTS.md, TECHNICAL.md, PUBLIC.md).

### 2026-08-18 — regressão BD + conversão UTC do listener + alerta AutoTrading OFF

- **Timestamps na BD** (`tests/test_timestamps.py`): além dos produtores em
  memória, a regressão agora verifica as **linhas gravadas** —
  `signals.received_at`/`edit_deadline`, `trades.opened_at` e
  `paper_balance_history.ts` têm de parsear com fuso UTC explícito (+00:00).
- **Conversão `msg.date` do listener** (`app/telegram_listener.py`): extraída
  para `msg_date_utc()` (naive local → UTC) + testes em `test_listener.py`
  (conversão correta e vazio quando não há `date`).
- **Alerta imediato retcode 10027** (`app/execution/mt5.py` + `state.py`):
  `_send_open_orders` recolhe os retcodes reais das pernas recusadas; quando
  10027 (AutoTrading OFF) a trade fica `auto_trading_off=True` e o
  `_try_execute` emite um alerta risk distinto ("⛔ AutoTrading DESLIGADO…")
  em vez de só o log. `Trade` ganhou `retcodes`/`auto_trading_off`
  (transientes, não persistidos). Testes no `test_mt5.py` (real-mode com
  `_mt5` fake) e `test_api.py` (notificação via approve).
- 185 testes verdes.

### 2026-08-18 — regressão: timestamps UTC explícitos

- Novo `tests/test_timestamps.py` (5 testes) que garante que **todos** os
  produtores de timestamps emitem UTC explícito (`+00:00`) — `_now_iso`
  (engine), `_now_iso_plus` (edit_deadline), `_now_iso_short` (heartbeat do
  listener) — e cobre o fluxo real: um sinal ingerido via API guarda
  `edit_deadline` com fuso UTC e o `last_seen`/idade da msg no diagnóstico
  chegam em UTC. Bloqueia a regressão do countdown inflado ("3000s").
- 178 testes verdes.

### 2026-08-18 — fix sinal não executado (AutoTrading off) + timestamps UTC + migração pending_management.sl

- **Causa do sinal não entrar (15:32)**: o broker recusou a ordem com retcode
  10027 = AutoTrading desligado no momento da execução (15:34). O utilizador
  ligou o AutoTrading depois; `trade_allowed: true` confirmado ao vivo.
- **Timestamps UTC explícito** (`+00:00`): `_now_iso`/`_now_iso_plus` (engine/state),
  listener (`_now_iso_short`, `msg.date`), paper accounts (`db.py`) e `since`
  do paper history (`main.py`). Antes eram naive locais — o browser (hora
  local diferente da VPS) interpretava `edit_deadline` como hora própria e a
  contagem da janela de edição aparecia inflada (~3000s em vez de 30s).
- **Migração `pending_management.sl`** (`db.py::_migrate`): a tabela nasceu
  sem a coluna `sl`; qualquer instrução de gestão com alvo ainda na janela
  (protect/close_all/move_sl) rebentava com `no column named sl` → 500 e a
  instrução perdia-se (8× 500 hoje). Coluna adicionada automaticamente no
  arranque (confirmado ao vivo).
- Comparações de tempo agora toleram naive e aware (`astimezone()` quando
  `tzinfo is None`) — `listener_status`, uptime, `_iso_age_seconds`.
- 173 testes verdes.

### 2026-08-18 — tp2/tp2_protect fora da corrida paper + botão Promover + risco dinâmico

- **`tp2`/`tp2_protect` deixaram de correr em papel**: `paper.open_for_signal`
  e `paper.tick` saltam `HIDDEN_STRATEGIES`, e `paper_status`/snapshots de
  saldo não as mostram — só ficam no backtest/compare histórico. (Sem dados
  paper antigos para limpar — as contas estavam vazias.)
- **Botão "⬆️ Promover" no feed**: a notificação de sugestão de troca leva
  `action="promote:<id>"` (coluna `action` nova em `notifications`, migração
  automática). `Notifier.send` aceita `action` e a dashboard renderiza o
  botão com um clique que faz `POST /api/config {strategy}` e volta à
  principal.
- **Risco dinâmico**: `risk_pct` base desceu para **2.5%** (default + valor
  na BD atualizado de 5.0). `state._effective_risk_pct()` escala o risco para
  baixo após perdas: cada **-20% de drawdown** desde o pico de equity
  (`peak_balance`, atualizado no tick) corta o risco a **metade**
  (`0.5 ** (dd/20)`, piso 0.1%). `_try_execute` usa o risco efetivo (real +
  papel) e o snapshot expõe `effective_risk_pct`/`peak_balance`.
- **Testes**: `test_dynamic_risk_scales_down_after_drawdown` + action no
  `test_paper_race_switch_suggestion_filters_hidden_and_is_consistent`;
  atualizados `test_paper_forward_opens_on_execution`,
  `test_paper_applies_group_protect` e o antigo
  `test_paper_ignores_close_all_when_not_in_manage_kinds` (agora
  `test_paper_close_all_applies_only_to_managed_visible`). Suite:
  **173 testes verdes**.

### 2026-08-18 — Corte de estratégias sem edge + auto-switch por regime

- **`HIDDEN_STRATEGIES = {"tp2", "tp2_protect"}`** em `app/strategies.py`:
  as duas variantes TP2 (sem edge no backtest — PF < 1 no histórico completo)
  saíram do **menu/páginas de teste** (`index.html`: botões, `TERM_ORDER`,
  `BEST_PAGE`, HELP), mas continuam no registo para o backtest/compare e na
  corrida paper. O menu passa de 6 para 4 páginas de simulação (o trace de
  exemplo passou a mostrar na página `tp2_gestao`).
- **Fix `_check_regime`**: passava `manage` mas não `manage_kinds` ao
  `run_analysis` — o sim de regime de uma estratégia "protect only" aplicava
  a gestão completa (errado). Agora passa `manage_kinds` também.
- **Sugestão de auto-switch por regime** (`state._check_paper_race_leader`):
  quando uma estratégia **visível** se mantém à frente da principal por
  >= 30 min com >= 3 trades fechadas, o bot emite notificação `race` de
  "🔄 Sugestão de troca" (feed + Telegram) — não troca sozinho; o
  utilizador promove com o botão "⬆️ Promover". `HIDDEN_STRATEGIES` nunca
  são sugeridas. Corrigido também o `NameError: name 'cur' is not defined`
  latente nessa notificação (helper `cur()` adicionado).
- **Testes**: `test_hidden_strategies_kept_in_registry_but_marked_hidden` +
  `test_paper_race_switch_suggestion_filters_hidden_and_is_consistent`.
  Suite: **172 testes verdes**.

### 2026-08-18 — Limpeza de posições fantasma + auto-arranque MT5 + religação automática

- **Fecho das 12 posições duplicadas** do sinal `tg-3948662131-4146`
  (3 trades fantasma). Verificado ao vivo: o broker tinha **0 posições, 0
  deals, 0 ordens e saldo exatamente 1000 €** — as posições só existiam na
  BD (criadas durante o crash do None-tick) e nunca chegaram ao MetaTrader.
  As 3 trades/12 posições foram fechadas na BD com `exit_reason="phantom"`
  (backup `signalbot.db.bak-*-pre-phantom-cleanup` antes). Dashboard voltou
  a `open_trades: 0`.
- **`boot.ps1` arranca agora o MetaTrader 5** (`terminal64.exe`, idempotente)
  entre o Caddy e o `start.ps1`, para o terminal estar no ar após reboot.
- **Servidor religa-se ao MT5 automaticamente**: retry curto no `lifespan`
  (até ~30s, para o terminal que ainda está a arrancar) + reconexão com
  throttle de 10s no `market_loop` (se o terminal cair/arrancar depois).
- **DEPLOY.md**: nova secção **auto-login do Windows** (registo `Winlogon`)
  para a Tarefa `NexGen Boot` disparar sem intervenção após reboot.
- Suite: **170 testes verdes**.

### 2026-08-18 — Fix de execução (None-tick + max_positions 5) + arranque idempotente + reboot + backup

- **Diagnóstico das 10:00–13:05**: dois sinais XAUUSD (TP1..TP4) ficaram
  `rejected` com "máximo de posições abertas atingido (3)". Causa raiz: o
  `symbol_info_tick` devolvia `None` na 1ª ordem do símbolo (fora do Market
  Watch) → `'NoneType' object has no attribute 'ask'` → o sinal ficava em
  `awaiting_edit` e o tick reabria a trade a cada segundo (3 duplicados =
  12 posições para 1 sinal) até o limite `max_positions=3` travar.
- **`max_positions` 3 → 5** (default em `config.py` + valor persistido na BD)
  — a estratégia de 5 pernas precisa de até 5 entradas.
- **`MT5Engine._send_open_orders`**: `ensure_symbol` passa a correr ANTES de
  enviar ordens; tick None/zero falha só a perna (rollback limpo quando todas
  falham). **`state._try_execute`** finaliza o sinal como `failed` em qualquer
  exceção de `open_trade` (nunca fica em `awaiting_edit` a repetir).
- **`start.ps1` idempotente** (ASCII puro, `return` em vez de `exit`): deteta
  servidor (porta 8000) e listener (processo) e não lança duplicados; o
  `$env:PORT=0/` vazio cai para 8000.
- **Watchdog `_listener_running_anywhere`**: deixou de usar `wmic` (removido
  em Windows recentes — nesta VPS não existe) e passou a usar `Get-CimInstance`.
  Era a causa raiz dos duplicados: com `wmic` em falta a função devolvia
  sempre False e o watchdog (incluindo o dos testes `pytest`) spawnava
  listeners extra.
- **Reboot da VPS**: `boot.ps1` (Caddy + start.ps1) + Tarefa Agendada
  **`NexGen Boot`** (AtLogOn).
- **Backup diário**: `backup_db.py` (cópia consistente via API backup do
  sqlite3, rotação de 10, envio `sendDocument` para o Telegram quando o bot
  estiver configurado) + Tarefa **`NexGen DB Backup`** (diária 06:30).
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` estão vazios no `.env` (backup só
  local até os preencher).
- **Teste novo**: `test_four_leg_signal_not_blocked_by_max_positions`
  (regressão do caso real). Suite: **170 testes verdes**.

### 2026-08-18 — Reorganização da UI: badges no header + terminal direito de conta/sessão

- **Badges no header** (`hdr-badges`, à direita da marca/subtítulo): modo
  (auto/manual/pausado), estratégia ativa (stack nome + parâmetros), MT5
  real (conta · saldo) e execução real/dry-run (mais o `killBadge` PAUSADO).
  Saíram do bloco STATE do menu esquerdo.
- **Menu esquerdo** (após HELP): o terminal `termSide` passou a ter só a
  lista de preços com variação % do dia (cabeçalho renomeado para ▚ MARKET).
- **Terminal direito `termRight`** (`--term-r`): utilizadores online +
  relógio + kill switch + Sair, com botão ‹ de colapsar próprio
  (`body.term-r-collapsed` encolhe para 38px). No móvel abre em overlay
  pelo botão ▦ (o header volta a ter ☰ + marca + badges + ▦).
- Conteúdo com recuo dos dois terminais (`--term-w`/`--term-r`).
- **Teste `test_paper_history_endpoint_returns_series`**: deixou de usar
  timestamps hardcoded de 2026-08-17 (com o passar das 24h a série ficava
  vazia no filtro `hours=24`) — passou a tempos relativos (`now - N min`).
- 169 testes verdes.

### 2026-08-18 — Arranque na VPS (Kamatera, Windows) — Python + venv + servidor + listener

- **Deploy na VPS**: o projeto foi movido para a VPS Kamatera (Windows) para
  30 dias de teste. O venv copiado do PC local estava quebrado
  (`pyvenv.cfg` apontava para `C:\Python314`, inexistente na VPS) — instalado
  **Python 3.14.7** (winget), recriado `.venv`, `pip install -r
  requirements.txt`. Servidor + listener no ar em `127.0.0.1:8000`,
  execução `mt5-real` na demo (saldo 1000€), estratégia `nova_gestao`.
- **`requirements.txt`**: adicionado **`MetaTrader5`** (faltava — o deploy
  novo instalava tudo menos o pacote que liga ao terminal; notado no
  DEPLOY.md).
- **Teste `test_report_includes_by_day`** deixou de depender do último dia
  do `by_day` (falhava quando testes anteriores fechavam trades com a data
  de hoje e pnl 0 — o dia da trade gravada é agora procurado por data).
- **Site público**: Caddy em `C:\caddy` (v2.11) a fazer reverse_proxy de
  `127.0.0.1:8000`; portas 80/443 abertas na firewall do Windows (regras
  `NexGen HTTP 80`/`NexGen HTTPS 443`). Primeiro por IP com HTTPS
  auto-assinado (`tls internal`) e depois migrado para o domínio grátis
  **n3xg3n.duckdns.org** com HTTPS automático da Let's Encrypt (cadeado
  verde). Script de update do IP em `C:\caddy\duckdns-update.ps1` (token
  preenchido, Tarefa Agendada diária `NexGen DuckDNS Update`).
- **Gestão como eventos tipados**: registo de estratégias passa de
  `manage: bool` para **`manage_kinds: set`** (quais kinds de gestão cada
  estratégia honra) + nova variante **`tp2_protect`** ("Sair no TP2 +
  protect only" — ignora close_all/close_tp1/move_sl) com página de teste
  e corrida paper.
- **Corrida paper corrigida**: a gestão do grupo (protect/close_all/
  close_tp1/move_sl/tp_hit) passou a ser aplicada às trades de papel das
  estratégias que a honram (antes as contas "+ gestão" comportavam-se como
  as sem gestão até o preço bater SL/TP) — correlação pelo signal_id da
  trade real alvo.
- **Atribuição de saída**: `exit_reason` + `exit_msg_id` nas pernas/trades
  (sl/tpN/protect/close_all/close_tp1/move_sl/...); a tabela de trades da
  dashboard mostra a razão do fecho e o **contrafactual "sem gestão"**
  (P&L da variante `nova` da corrida paper para o mesmo sinal). Migração
  automática da BD (ALTER TABLE), backup `signalbot.db.bak-*-pre-gestao`.
- 169 testes verdes (5 novos: gestão na corrida paper ×2, exit_reason,
  filtro manage_kinds na conta real e no backtest).
- **Revertida a reorganização de UI** (o utilizador preferiu o estilo
  anterior): o menu voltou para a **coluna fixa à esquerda** e o terminal
  de estado para a **coluna fixa à direita** (layout original restaurado a
  partir do zip `Project NexGen.zip` no desktop), incluindo a navegação
  por teclado (↑↓/Enter) e o colapso `term-r-collapsed` (38px). As
  features novas mantêm-se: página `tp2_protect` no menu e a coluna
  "Saída / sem gestão" nas trades.
- **Terminal STATE movido para o menu**: as informações do terminal direito
  (badges auto/estratégia/MT5/mercado, preços com variação % do dia,
  utilizadores + relógio, kill switch, Sair) passaram para um bloco de
  **estilo terminal em baixo do menu de navegação (após HELP)** — a coluna
  direita foi removida (`--term-r` eliminado, conteúdo só com recuo
  esquerdo) e o header perdeu o botão ▦ (o STATE abre com o ☰ no móvel).
  O colapso ‹ agora esconde só o bloco STATE (fica o cabeçalho ▚ STATE +
  ›); o hint de teclado ficou estático entre o HELP e o STATE.

### 2026-08-17 — Variação % do dia no terminal STATE + botão colapsar (‹) + estudo saída no TP2

- **Terminal STATE**: cada preço mostra agora a **variação % do dia** ao lado
  do valor (verde/vermelho) — vem do `chartPreviousClose` do Yahoo
  (`YahooProvider.changes` → `market.changes` no snapshot e no
  `/api/diagnostics`); sem dado (ex.: simulação) não aparece nada.
- **Colapsar/expandir**: novo botão **‹ ›** no cabeçalho do terminal direito
  — colapsa para uma barra de 38px e o conteúdo da página liberta o espaço
  (via `body.term-r-collapsed` → `--term-r`); preferência persistida em
  localStorage; no móvel o estado abre sempre completo.
- **Estudo (sem código)**: 3 variantes de saída no TP2 no histórico completo
  (576 sinais, 1h, risco 5%, kill 6%) — ver resposta da análise: sair cedo
  NÃO se justifica; a gestão (protect ~80% do benefício) é o que salva.
- 164 testes verdes.

### 2026-08-17 — Layout responsivo a todos os zooms + terminal direito de estado

- **Terminal direito `termSide`**: as informações que viviam no header
  (badges auto/estratégia/MT5/mercado, lista vertical de preços GOLD..NZDUSD,
  utilizadores + relógio, kill switch e Sair) passaram para um terminal
  fixo à direita (estética igual ao menu esquerdo) — o header fica só com
  ☰ + marca + ▦. ≤960px abre em overlay pelo ▦; os ids dos elementos não
  mudaram (zero JS de lógica alterado, só a localização no DOM).
- **Zoom do navegador**: em ≥961px o conteúdo afasta-se dos dois terminais
  (`--term-w`/`--term-r` partilhados por variáveis CSS) — nunca são
  cobertos em qualquer zoom; 961–1120px os terminais encolhem (largura +
  tipografia); alturas < 640px o menu passa a `flex-start` + scroll (o
  `justify-content: center` cortava itens sem forma de os alcançar); os
  itens do menu partem linha (`white-space: normal`) em vez de truncar.
- **Overflow horizontal eliminado**: tabs e `.scroll`/tabelas (Posições e
  Relatório incluídas, agora envolvidas) ganharam `overflow-x: auto`; o
  login usa `width:min(400px,92vw)`. Verificado a 350px de viewport
  (docW == viewport) e em desktop.
- 164 testes verdes (não houve mudanças Python).

### 2026-08-17 — Renome do TP2 + estratégia "Sair no TP2 + gestão" + exemplo de fluxo + página HELP

- **Renome**: a estratégia `tp2` passou a chamar-se **"TP1→BE + sair no
  TP2"** (id mantém-se) — o nome antigo escondia que o SL e o BE estão
  sempre lá; docs e testes atualizados.
- **Nova estratégia `tp2_gestao`** ("Sair no TP2 + gestão"): `exit_at_tp=2`
  com `manage=True` — o `simulate_signal_managed` passou a respeitar
  `exit_at_tp` (fecha tudo no TP2, mesmo antes da gestão do grupo fechar);
  5ª página de teste + corrida paper (cor laranja).
- **Exemplo do fluxo de uma trade real** na página do TP2: novo endpoint
  `GET /api/analysis/trace` (re-simula o sinal resolvido mais recente com
  `trace=True` — `simulate_signal` devolve `events` cronológicos SL/TP1/BE/
  fecho + `path` de candles) e a página desenha o gráfico (entrada/SL/TP1/
  TP2 + marcadores) e a linha do tempo dos eventos.
- **Página HELP** no menu (abaixo do Diagnóstico): cards interativos por
  estratégia (parâmetros + como funciona + números reais de 30 dias) e
  glossário de conceitos.
- **Ticker**: nomes dos símbolos e outlines das caixas deixaram de ser
  verde/vermelho (agora neutros: azul/âmbar) — os valores em verde/vermelho
  (direção) saltam mais à vista.
- 164 testes verdes (3 novos: managed exit_at_tp, trace, endpoint trace).

### 2026-08-17 — Kill diário no backtest + config de risco alinhada + 4ª estratégia + alerta de regime

- **A — config**: `daily_loss_limit_pct` passou de 3% para **6%** (risco
  5%): antes, qualquer 1ª perda do dia (≈4% com o piso de lote mínimo)
  disparava o auto kill e cortava os dias bons. Sweep no histórico do
  formato atual: limite 3% = P&L +2730 (salta 19 trades), 6% = **+3247**
  (salta 10), 8% = +3396 mas com DD maior (28% vs 24.7%).
- **B — kill diário no backtest**: `apply_daily_stop()` em backtest.py +
  `daily_stop_pct` em `run_analysis`/`run_compare`/`run_month_sim` (None =
  `daily_loss_limit_pct` da config, 0 = off) — as trades depois do limite
  do dia ficam `outcome=skip`/pnl 0 (`daily_stop_skipped` no payload). Os
  números da dashboard refletem o kill real.
- **C — 4ª estratégia `nova_gestao_3`** (TP1..TP3 + gestão): página de
  teste + corrida paper (cor ciano); `simulate_signal_managed` respeita
  `max_legs`. Seguro contra mudança de regime (pernas 4–5 foram
  catastróficas no regime antigo).
- **D — alerta de regime**: `_check_regime()` no tick (a cada ~30 min)
  corre o sim rolante de 30d da estratégia ativa com o kill diário; PF < 1
  com ≥ 10 trades → notificação `risk` (feed + Telegram, cooldown 6h);
  snapshot expõe `regime`.
- **Estudos de risco**: `/api/analysis`, `/compare`, `/month` aceitam
  `risk_pct` e `balance` (0 = config/atual) — seletores "Risco do sim" +
  saldo na tab Edge (persistidos em `edge_risk`/`edge_bal`). Descoberta:
  com saldo 1000 € o piso de lote mínimo domina (riscos ≤ ~4% idênticos);
  o PF não muda com o risco (estudo a 10 000 €: PF ~4.4 em 0.5–10%).
- 161 testes verdes (3 novos: apply_daily_stop × 2, params risco/saldo).

### 2026-08-17 — Corrida: principal a tracejado · ticker com direção · Telegram formatado

- **Curva da corrida paralela**: a linha da **principal (conta real)**
  desenha-se a **tracejado e mais grossa** (largura 3, dash 8/4) e com o
  rótulo de % maior — distingue-se à primeira vista das linhas de teste
  (contínuas).
- **Ticker de preços**: o preço pinta-se a **verde/vermelho conforme a
  variação vs o valor anterior** (classe `.p.up`/`.p.down` com brilho) —
  movimento ao vivo; a 1ª leitura e preços iguais ficam neutros.
- **Telegram**: `Notifier.send` ganhou `tg_text`/`parse_mode` opcionais —
  o feed guarda o texto limpo (sem markdown) e o push para o Telegram usa
  o texto formatado em negrito quando dado. O alerta da corrida (🏁) envia
  agora uma versão em Markdown com **negrito** nos valores e linhas
  separadas — consistente com os emojis das restantes mensagens.
- 158 testes verdes; servidor reiniciado (notifier/state mudaram).

### 2026-08-17 — Roxo mais claro nas curvas nova_gestão + alerta da corrida a roxo

- As curvas da estratégia nova_gestão (corrida paralela e A/B/C) usam
  agora um roxo **um pouco mais claro** (`#9A4FE8`) para contraste com o
  fundo escuro ao lado do azul (nova) e âmbar (tp2) — o accent do site
  continua `#8437C9`.
- O alerta de ultrapassagem na corrida (`_check_paper_race_leader`) passa
  a emitir uma notificação de tipo **`race`** (antes `risk`/âmbar): ícone
  🏁 a roxo no feed, grupo "🏁 Corrida", com o mesmo push para Telegram.
  O badge de aviso no header mantém o vermelho (destacado).
- 158 testes verdes; servidor reiniciado (state.py mudou).

### 2026-08-17 — Paleta roxa mais escura

- As cores rosa/roxas do site (accent `--violet` dos dois temas, badges,
  tab ativa, menu, brilhos/glows, cores das curvas nova_gestão nos gráficos
  e o rosa do terminal decorativo) passaram de `#8b5cf6`/`#c449ff`/`#ff4d6d`
  para um roxo mais escuro e sóbrio (**`#8437C9`**, com os `rgba` dos glows
  atualizados para o novo RGB) — pedido do utilizador, ligeiramente mais
  claro que o `#742CAB` inicial a seu pedido.
- Só HTML estático — sem reiniciar o servidor (recarregar com Ctrl+F5).

### 2026-08-17 — Seletor de período partilhado + P&L por perna no "desde o início"

- O seletor de período do bloco "📜 Desde o início" (todo o histórico /
  só o formato atual desde 28/07 / só o mês corrente — dinâmico) agora é
  **partilhado** entre as páginas de teste e o A/B/C da tab Edge e fica
  **persistido na config** (`all_period`, como o `teste_days`).
- Cada payload do backtest devolve agora **`pnl_by_leg`** (P&L por perna
  leg1..leg5, só trades resolvidas) e as páginas de teste mostram as chips
  "🧮 P&L por perna" — em agosto todas as 5 pernas são verdes (leg1 lidera
  com ~+5,4k no sim completo de 51 sinais); no histórico completo as pernas
  2–5 arrastam o resultado para negativo enquanto a leg1 se mantém positiva.
- 158 testes verdes.

### 2026-08-17 — Cutoff do formato atual + P&L por mês + falência no sim "desde o início"
- **Seletor de período no bloco "📜 Desde o início"** das páginas de teste:
  todo o histórico vs **só o formato atual** (`since=2026-07-28`, a data da
  primeira mensagem do formato atual). Novo parâmetro `since=YYYY-MM-DD` em
  `GET /api/analysis/month` e `GET /api/analysis/compare` (filtra os sinais
  a partir dessa data) + `run_analysis(since=...)`.
- **Gráfico de barras "📊 P&L por mês"** no bloco desde o início das páginas
  de teste (`drawMonthlyBars`) e na tab Edge (bloco compare-all, coluna da
  gestão). `run_analysis` devolve `pnl_by_month`.
- **Falência no sim**: `run_month_sim` devolve `bankrupt` + `bankrupt_date`
  quando o saldo acumulado chega a zero — o veredicto mostra
  "⚠️ falência no dia X" (o completo a 1% risco falece a 2026-07-22).
- `run_compare` trata o caso sem sinais (devolve empate em vez de KeyError).
- Testes: **156 → 158** (since no month/compare + empty compare).

---

### 2026-08-17 — Ticker GOLD/SILVER + lotes por perna no Histórico + timestamps + análise "desde o início"
- **Ticker do topo**: XAUUSD/XAGUSD mostram agora os rótulos **GOLD** e
  **SILVER** (com o símbolo no tooltip) — o ouro estava lá, mas só como
  XAUUSD, o que não era óbvio.
- **Histórico de trades**: a coluna Lotes mostra agora a **repartição por
  perna** (ex.: `0.03+0.03+0.03`, tooltip com TP1..TP5 e total) em vez de
  só o total.
- **BD**: os 2 timestamps sem timezone (caminho demo, `_now_iso()` local)
  foram normalizados para `+00:00` (0 restantes).
- **Análise "desde o início" (investigação)**: o sim completo (576 sinais)
  perde porque (1) abr–jul eram formatos antigos genuinamente perdedores no
  backtest (WR 36–54%, ouro em queda 4742→4080) e (2) o risco % fixo sobre
  saldo em queda compõe as perdas até a falência (end_balance −1153). O
  formato atual (ago, 51 sinais) é forte: nova WR 72.5 PF 2.07; + gestão
  PF 6.16. O sim de 30 dias (só formato atual) continua positivo (PF 3.12
  com gestão) — o "desde o início" puxa a média para baixo.

---

### 2026-08-17 — Lotes visíveis no risco% + rejeição de zonas impossíveis
- **Chip "Lotes ≈ do risco%"** no bloco de detalhes de risco: mostra quantos
  lotes o risco% configurado representa numa trade típica de XAUUSD (total
  e por perna), calculado com o contrato/pip reais e a distância do SL do
  último sinal válido (ex.: 5% → "0.71 total · 0.14 / perna").
  `state.risk_preview()` + `config.risk_preview` no snapshot;
  `DB.last_signal_with_sl()`. As tabelas (Posições, Histórico, trades de
  papel, posições MT5) já mostravam lotes — confirmado na revisão.
- **Parser rejeita zonas de entrada impossíveis** (typos do grupo): o span
  da zona > ~1% do preço (ex.: `4379-4775` = 396 pontos de ouro, `4317-3413`
  = 904) deixa de ser aceite — `_pick_zone_entry` devolve None. Zonas largas
  mas coerentes (ex.: `4078.50-4047`) continuam a passar. Os 2 sinais maus
  já gravados foram marcados `failed` na BD (576 sinais válidos).
- Testes: **154 → 156** (zonas impossíveis + zona larga legítima).

---

### 2026-08-17 — Reconhecimento dos 3 formatos de sinal + move_sl + edições TP1/SL + re-análise do histórico + UI
- **Parser reconhece os 3 formatos do Golden Moves**: o atual (multilinha),
  o antigo `XAU USD SELL NOW 4737-4741 🥇TP1 … 🚫SL` (símbolo com espaço,
  medalhas, SL com 🚫/invalid) e o mais antigo `Sell Gold Now … Targets …`
  (Targets = lista sem rótulo, GOLD→XAUUSD). Antes só o primeiro parseava
  → **505 sinais reais recuperados** do histórico (73 → **578**).
- **Re-análise offline do histórico**: os `failed` que agora parseiam foram
  convertidos a `historical` (nunca executam) e os que são gestão
  reconhecida a `management` — backup em `signalbot.db.bak-reparse`.
- **`move_sl`**: "Adjust/Move SL to X" move o SL de todas as pernas abertas
  (sem nível = nudge registado); guardado em `pending_management.sl`.
- **Edições TP1/SL**: "TP1 4734 SL 4745" com reply atualiza o TP1/SL do
  sinal alvo (`update_signal_levels`) e move o SL das pernas abertas
  (`_handle_tp_sl_update`) — antes ficavam em `failed`.
- **Curva da corrida com P&L absoluto**: a legenda mostra `+3.7% · +€418
  (€11304)` e há tooltip ao passar o rato (valor € + % por estratégia).
- **Alerta quando um teste ultrapassa a principal**: `_check_paper_race_leader`
  no tick emite notificação `risk` (feed + Telegram se configurado) quando
  o líder muda (mínimo 2 trades fechadas; cooldown 5 min no arranque).
- **UI mais limpa**: bloco "⚙️ Detalhes de risco" colapsável na principal;
  notificações agrupadas por tipo com contagem (`FEED_GROUP_LABEL`).
- Resultado do backtest completo (578 sinais): o período antigo (abr–jul)
  era perdedor (PF<1); o edge da gestão só existe no formato recente (30d:
  nova 1.1, **nova+gestão 2.92**, TP2 0.82). Drawdown >100% é artefacto do
  risco % fixo sobre o saldo inicial (sem falência no sim).
- Testes: **145 → 154** (parser 3 formatos, move_sl, tp_sl_update).

### 2026-08-17 — ✓ "melhor estratégia" no menu + curva da corrida paralela (principal + Edge)
- **Menu SIMULAÇÃO DE ESTRATÉGIAS marca a melhor**: o item da estratégia
  vencedora no A/B/C do histórico completo (`compare?days=0` → `winner`)
  ganha um badge verde "✓ melhor" — vês à primeira qual é a melhor das 3
  segundo o backtest de todo o histórico (não só a que está promovida).
- **Curva ao vivo "quem está a ganhar"**: novo histórico de saldos
  (`paper_balance_history` — a BD só tinha o saldo atual). O servidor grava
  um snapshot a cada ~60s no tick: as estratégias de teste com a conta
  virtual (paper), a principal com o saldo real da conta MT5.
- **Mini-curva na página principal**: bloco "🏁 Corrida paralela (paper)"
  com a evolução em % desde o início da corrida + saldo atual de cada uma
  (atualiza a cada 60s).
- **Bloco na tab Edge**: "🏁 Corrida paralela ao vivo (paper + real)" com a
  mesma curva em maior + legenda, carregado no `loadEdge` — a comparação
  fica junto do A/B/C (a curva mostra a corrida forward; o A/B/C o
  backtest).
- Endpoints novos: `GET /api/paper/history?hours=` (séries por estratégia,
  com `is_main`), tabela `paper_balance_history` (UNIQUE strategy_id+ts).
- Testes: **144 → 145** (endpoint paper/history).

### 2026-08-17 — Secção do menu "simulação de estratégias" com as 3 estratégias
- O menu terminal esquerdo passou a chamar **▚ SIMULAÇÃO DE ESTRATÉGIAS**
  (era "ESTRATÉGIAS (teste)") e tem agora as **3 páginas**: TP1..TP5 sem
  gestão, **TP1..TP5 + gestão** (nova página — era a única que não tinha
  simulação própria) e Fechar no TP2.
- A página principal usa a **melhor das 3** — a estratégia ativa na config
  (hoje `nova_gestao`). A página de simulação da estratégia ativa mostra a
  **corrida real** da conta MT5 (saldo/equity/P&L hoje/trades abertas) em
  vez da conta paper (ela não tem), e **esconde o botão "⬆️ Promover a
  principal"** (já é a principal); a tabela de trades de papel indica que a
  principal corre na conta real. `GET /api/paper/trades` passou a devolver
  `account.is_main` para a dashboard saber.
- Testes: mantêm-se **144** (alterações de frontend + um campo extra no
  endpoint de paper trades).

### 2026-08-17 — Adaptação ao zoom do navegador + correções visuais verificadas com preview
- **Layout adapta-se ao zoom**: a barra de tabs colava com `top: 52px`
  fixo — com zoom (ou quando o header envolve em ecrãs estreitos) ficava
  debaixo do header. Agora a altura do header é medida ao vivo
  (`updateHdrH()` → `--hdr-h`, atualizado no `resize`) e a barra cola logo
  abaixo do header real (verificado: a 870px de viewport o header tem
  139px e as tabs colam em 139px).
- **Overflow horizontal eliminado**: os itens da grelha `.layout`
  (main/aside) tinham `min-width: auto` e o conteúdo largo (tabelas)
  alargava a coluna além do viewport — agora `min-width: 0` (verificado:
  0px de overflow a 870px, incluindo páginas de teste/Diagnóstico).
- **Botão ☰ do menu móvel aparecia nunca** — o `style="display:none"`
  inline sobrepunha-se à media query `≤960px`; agora aparece no móvel e
  fica escondido no desktop.
- **Bug de formatação "++"**: o `money()` já antepõe "+" a valores
  positivos, mas o veredicto das páginas de teste, o P&L paper e a tabela
  de trades de papel adicionavam outro "+" manual → "++USD 418.00".
  Corrigido (só `money(pnl)`).
- Verificação visual feita com uma instância de preview temporária
  (porta 8081, BD em cópia): página principal, páginas de teste e
  Diagnóstico sem sobreposições/overflow com o viewport estreito (zoom).
- Testes: mantêm-se **144** (alterações de frontend).

### 2026-08-17 — Diagnóstico sem tabs + chrome por página + menu móvel + adeus "Se lote fixo"
- **Diagnóstico sem a barra de tabs da principal** — o `navigateTerm("diagnostico")`
  escondia mal a barra (Sinais/Posições/Histórico/Relatório/Edge/Ignoradas
  apareciam numa página de diagnóstico). Agora fica só o conteúdo da página.
- **Chrome por página**: stats/controlos/bloco de risco escondidos nas
  páginas de teste e no Diagnóstico (`body.no-chrome`); ficam só o header
  (kill switch + badges) e a statusbar. O auto-refresh do Edge também é
  parado ao entrar no Diagnóstico (como já acontecia nas páginas de teste).
- **Labels das páginas de teste a acompanhar o seletor** — o texto de
  introdução e o título da comparação já não dizem sempre "1 mês": dizem
  "há 7/90 dias" ou "desde o início do grupo" conforme o escolhido.
- **Widget "Se lote fixo (vs risco %)" removido** — nenhuma estratégia do
  registo usa lote fixo (duas_pernas saiu); o bloco de risco fica só com
  perda/SL, por perna, limite diário e P&L hoje. O parâmetro de simulação
  `lots_override` no motor mantém-se.
- **Menu acessível em ecrãs pequenos (≤960px)**: botão **☰** no header abre
  o menu terminal como overlay (fecha ao navegar, clicar fora ou Escape) —
  as páginas de teste e o Diagnóstico passam a ser alcançáveis no telemóvel.
- Testes: mantêm-se **144** (alterações de frontend/navegação).

### 2026-08-17 — Comparativo com a gestão + último replay no Diagnóstico + badge de config órfã
- **Página de teste com o P&L da gestão**: o comparativo (curva + legenda)
  passou a desenhar **3 estratégias** nos mesmos sinais — nova, **nova +
  gestão (a principal)** e fechar no TP2 — para veres o que cada uma teria
  dado (antes eram só as 2 de teste).
- **Diagnóstico mostra o último replay do histórico**: o `history_done`
  guarda na config `replay_done_at`/`replay_total`/`replay_sinais`/… e o
  `/api/diagnostics` expõe um bloco `replay` — a tab Diagnóstico mostra a
  data + totais do último replay (mensagens, sinais, gestão, resumos,
  ignoradas).
- **Badge avisa config órfã**: se a `strategy` da config apontar para um id
  que já não existe no registo, `strategy_params.orphan` fica `True` e o
  badge mostra "⚠️ … (config órfã) — a correr com <fallback>" a vermelho
  (`_strategy_params` devolve também `configured_id`).
- Testes: **143 → 144** (orphan + replay no diagnostics).

### 2026-08-17 — Removida a estratégia de teste "2 pernas · lote fixo"
- **`duas_pernas` saiu do registo** (`app/strategies.py`): o menu terminal
  ficou com **2 páginas de teste** (nova, fechar no TP2); o comparativo da
  página de teste desenha 2 curvas; a corrida paper e o `paper_status`
  passam a gerar-se só das estratégias restantes. O parâmetro de simulação
  `lots_override` continua suportado pelo motor (ninguém o usa por agora).
- Sem artefactos para limpar: a BD não tinha contas/trades de papel de
  `duas_pernas` e a config `strategy` estava em `nova_gestao`.
- Testes: mantêm-se **143** (o teste de `strategy_params` passou a usar `tp2`).

### 2026-08-17 — Edge com histórico completo + fim de replay notificado + seletor persistido
- **Tab Edge com o A/B/C do histórico completo**: novo bloco "📜 Desde o início
  do grupo" (`loadCompareAll`, `GET /api/analysis/compare?days=0`) com WR/PF/
  P&L/drawdown das 3 estratégias, ao lado da comparação do mês — para ver se
  o edge se mantém desde o início do grupo.
- **Notificação de fim de replay do histórico**: o listener conta as mensagens
  reencaminhadas por ação (sinais/gestão/resumos/ignoradas) e, no fim,
  avisa o servidor (`POST /api/listener/history_done`) que regista a
  notificação "📚 Replay do histórico concluído" no feed (`state.history_done`).
- **Seletor das páginas de teste persistido na config**: nova chave
  `teste_days` (7/30/90/0 = tudo) — `ConfigUpdate.teste_days`, snapshot
  `config.teste_days`, a dashboard restaura-o ao recarregar. O seletor ganhou
  a opção **"tudo (desde o início)"** (0), que mostra a simulação do
  histórico completo no bloco principal (o bloco dedicado "desde o início"
  esconde-se nesse caso para não duplicar).
- Testes: **140 → 143** (compare days=0, teste_days na config, history_done).

### 2026-08-17 — Promoção com notificação + histórico completo nas páginas de teste
- **Promover uma estratégia regista notificação no feed** com os parâmetros
  que passam a ser usados na execução real (max_legs/exit_at_tp/lote/gestão)
  — `state.update_config` + `_strategy_params_text`.
- **Badge da principal mostra os parâmetros reais de execução**: o snapshot
  expõe `config.strategy_params` (`state._strategy_params`) e o badge
  `♟ nome` ganhou uma segunda linha com `TP1..TP5 (todas) · gestão ON · …`
  (+ tooltip) — sabes sempre como a conta real está a correr.
- **Páginas de teste com 2 horizontes**: a simulação do último mês (seletor
  7/30/90) + o bloco **"📜 Desde o início do grupo"** — `days=0` no
  `GET /api/analysis/month` = TODO o histórico na BD, sem filtro de data
  (candles 1h/730d), com veredicto/stats/curva próprios por estratégia.
- **Tabela de trades de papel** na página de teste: novo
  `GET /api/paper/trades?strategy=<id>` (abertas + fechadas com P&L, hora,
  símbolo, direção, entrada, lotes) — o histórico da corrida paralela ao
  vivo, além do saldo atual.
- **Replay do histórico do listener desde o início do grupo**: novo
  `SIGNALBOT_TG_HISTORICAL_LIMIT` (0 = todas as mensagens; alias antigo
  `SIGNALBOT_TG_HISTORY_LIMIT`; default 0 = tudo). `history_limit()`
  devolve `None` para "sem limite" (no Telethon `iter_messages(limit=0)`
  devolveria zero mensagens). Definido no `.env`.
- Testes: **134 → 140** (paper trades endpoint, promoção com notificação,
  strategy_params no snapshot, month sim days=0, history_limit).

### 2026-08-17 — Redesenho final: principal começa hoje + promover + avisos
- **A principal (nova+gestão) começa a partir de hoje** — a simulação do mês
  ("se tivesses começado há 30 dias") saiu da tab Edge da principal e ficou
  apenas nas **3 páginas de teste** do menu (nova, fechar no TP2, 2 pernas·
  lote fixo). O menu terminal passou a ter só estas 3 estratégias de teste.
- **Botão "⬆️ Promover a principal"** em cada página de teste — `POST
  /api/config {strategy}` muda a estratégia ativa da conta real.
- **Badge da estratégia ativa** na página principal (`♟ nome`), com clarão +
  toast quando muda; **aviso vermelho** quando uma estratégia de teste está
  a ganhar à principal na corrida paper (saldo virtual > saldo real com
  trades fechadas).
- **Tab "🔧 Diagnóstico" removida da barra de tabs da principal** — tem
  página própria no menu terminal (limpeza da página principal).
- **Paper race segue a config ativa**: `open_for_signal(main_id=...)` e
  `paper_status.is_main` usam a estratégia da config, não a hardcoded —
  promover uma estratégia tira-a da corrida paper (passa a correr na real).
- Testes: 134 (sem alteração de contagem — mudanças de frontend/config).

### 2026-08-17 — Estratégias como conceito + navegação terminal + paper trading forward
- **Registo central `app/strategies.py`**: nova, nova+gestão (principal) e
  fechar no TP2, cada uma com `max_legs`/`manage`/`exit_at_tp`. Novo
  `GET /api/strategies`; config `strategy` (validada) para trocar a
  estratégia ativa da conta principal.
- **Motores multi-estratégia**: `SimEngine` e `MT5Engine` aceitam `max_legs`
  (limita pernas) e `exit_at_tp` (fecha tudo quando o TP_N bater — dry e
  real via broker). `run_month_sim` aceita `strategy_id`.
- **Navegação terminal**: o terminal esquerdo passou de decoração a **menu**
  (setas ↑↓ + Enter + rato): PRINCIPAL · páginas de teste por estratégia ·
  DIAGNÓSTICO. Página de teste = simulação de 1 mês (1000 €) + mini-curva.
- **Paper trading forward** (`app/execution/paper.py`, tabelas
  `paper_accounts/trades/positions`): cada estratégia de teste tem saldo
  virtual próprio; quando um sinal real executa, as estratégias de teste
  abrem a mesma trade em papel e o tick atualiza contra os preços — corrida
  paralela ao vivo, nunca toca a conta real. Snapshot expõe `paper.accounts`.
- +5 testes = **134 testes**.

### 2026-08-17 — Simulação do último mês na tab Edge
- Novo **`GET /api/analysis/month`** (`run_month_sim` em `analysis.py`):
  replay dos sinais reais do grupo com a **gestão completa** (TP1..TP5 +
  protect/close_all) desde há N dias até hoje, começando com o **saldo
  atual** — responde "se tivesses começado há 1 mês com X €, hoje terias
  Y €". Devolve saldo inicial/final, P&L, win rate, PF, trades e a
  **curva de equity acumulada** (por data de receção do sinal).
- **Tab Edge**: novo bloco "📅 Simulação do último mês — como seria hoje"
  com o saldo inicial → P&L → saldo final, win rate, PF, trades e uma
  mini-curva de equity (verde/vermelha consoante o resultado). Atualiza
  com a tab (60s). Nunca escreve na BD nem abre trades. +1 teste =
  **129 testes**.

### 2026-08-17 — Medição do atraso das edições + alerta de revive + reprocessamento das falhadas
- **`edited_at` na BD** (coluna nova em `signals`, com migração automática):
  regista a hora de cada edição do grupo (`_apply_edit` e revive). Novo
  `GET /api/edits` devolve a distribuição do atraso (mín/média/mediana/p90/
  máx + últimos exemplos) — para validar a janela de 30s com dados reais,
  não por achismo.
- **Tab Diagnóstico**: novo bloco "⏱️ Atraso das edições do grupo" — a
  mediana vs a janela atual (verde dentro / vermelho fora) e os últimos
  exemplos. Preenche-se automaticamente à medida que o grupo edita.
- **Alerta de revive**: notificações passam a ter o kind `revived` (🔄 no
  feed) e a dashboard mostra um **toast** quando um sinal falhado é
  recuperado pela edição do grupo.
- **Reprocessamento das 12 mensagens `failed`** (todas sem trades ligadas):
  apagadas e re-ingestadas pelo replay do histórico com o classificador
  novo. A **4141** (entrada incompleta que o grupo completou) voltou como
  sinal **historical válido** XAUUSD buy 4416.50 (SL 4408.50, TP1-5) em vez
  de failed. As 7 restantes são conversa/typos conhecidos (rejeitados de
  propósito). Backup: `signalbot.db.bak-20260817b`.
- Corrigido bug no `insert_signal` (22 placeholders para 21 colunas) que
  apareceu ao adicionar `edited_at`.

### 2026-08-17 — Revive de sinais falhados + janela 30s + estratégia antiga removida
- **Revive de sinais falhados**: quando um sinal chega incompleto (ex.:
  "I'm buying XAUUSD 4416.50-4412" sem TP/SL ainda) e falha após a janela,
  uma edição posterior do grupo que o complete **revive** o sinal
  (`action: revived`) — reabre a janela de edição com o conteúdo novo em
  vez de ignorar a edição. Limitado a edições até 5 min após a mensagem
  original; histórico nunca revive. +1 teste = **128 testes**.
- **Janela de edição de volta a 30s** (config `edit_window_seconds=30` na
  BD) — o comportamento "esperar e ver se a mensagem mudou" que o
  utilizador pediu; o listener já reportava as edições, agora o backend
  re-age sobre elas quando o sinal ainda não executou.
- **Estratégia "antiga" (TP1/TP2 only) removida do compare** da tab Edge
  (backend `run_compare` + coluna da dashboard + endpoint): ficam nova
  (TP1..TP5), nova+gestão e fechar no TP2 — a antiga era a pior (PF 1.19).
- Teste do compare agora semeia os seus próprios dados (determinístico,
  não depende da ordem da suite).

### 2026-08-17 — Alerta de edição pós-execução + máx pernas no widget + teste estável
- **Alerta de edição pós-execução**: se o grupo editar uma mensagem depois
  de o sinal já ter executado (ou ficado pendente) e a janela ter passado,
  o bot emite uma notificação `risk` na dashboard ("⚠️ O grupo editou o
  sinal ... depois de executar — a entrada pode estar desatualizada").
  Com janelas curtas (15s) é a rede de segurança para detetar entradas
  com o preço antigo. Replays históricos não disparam. +1 teste =
  **127 testes**.
- **Widget "Se lote fixo"**: nova linha com o **nº máximo de pernas** que
  cabem dentro do risco % configurado na conta atual (amarelo se o nº
  pedido exceder, vermelho se nem 1 cabe) — dimensionar o lote com
  segurança.
- **Teste do replay histórico estável**: comparação por id + filtro por
  texto do resumo (em vez de contagem global) — o flake raro do loop de
  fundo já não faz o teste falhar.

### 2026-08-17 — Lote fixo vs risco % na dashboard + variante "fechar no TP2" + isolamento dos testes
- **Widget "Se lote fixo" no bloco de risco**: mostra o risco real (€ e %)
  que N pernas de lote fixo (ex.: 0.04) dariam na conta atual, usando o
  SL do último sinal e o contrato do símbolo — verde se ≤ risco %
  configurado, vermelho se acima. Inputs de lote/pernas ao vivo.
- **Backtest "Fechar tudo no TP2"**: `simulate_signal(exit_at_tp=2)` —
  quando o TP2 bate, TODAS as pernas ainda abertas fecham nesse preço
  (nenhuma corre para TP3..TP5). Nova 4ª coluna na tab Edge
  (A/B/C/D: antiga · nova · nova+gestão · TP2 total) e no veredicto.
  +2 testes = **126 testes**.
- **Isolamento dos testes (fix)**: novo `tests/conftest.py` força o DB
  temporário e o broker de papel ANTES de qualquer import de `app.*` —
  correr ficheiros de teste fora de ordem já não escreve na BD de
  produção (incidente de hoje: os subsets escreveram 61 sinais/29
  trades/59 positions de teste na BD real; limpo + config restaurada
  do backup `signalbot.db.bak-20260817`).

### 2026-08-17 — Limpeza da BD: resíduos de simulação + reprocessar histórico
- **P&L de hoje a mostrar +3835.77 € era resíduo de simulação**: a conta
  nova (5054586550, saldo 1000 €) tem **0 deals reais** hoje (confirmado
  no histórico MT5); as 3 trades da BD (incl. uma de +3853 € com 1.23
  lots × 5 pernas) eram de papel de um servidor anterior. Apagadas as 3
  trades + 12 positions; os 2 sinais reais do grupo ligados voltaram a
  `historical`. `realized_today` agora = 0. Backup: `signalbot.db.bak-20260817`.
- **Histórico reprocessado com o classificador novo**: apagadas as 2
  mensagens que o reconhecimento antigo tinha marcado `failed`
  ("TP4 and 5 HIT" msg 4058 e "TP4 👀 ?" msg 3994) e reiniciado o
  listener — o replay do histórico re-ingestou-as como **gestão `tp_hit`**
  (`historical`, sem agir em trades). Suite: **124 testes**.

### 2026-08-17 — Reconhecimento: TP múltiplos/emoji · limpar reinícios à mão · terminais de trading
- **Classificador de gestão**: o grupo anuncia às vezes **dois targets de
  uma vez** ("TP4 and 5 HIT" → tp_hit tp=5, usa o mais alto) ou com o
  **emoji 👀** em vez da palavra HIT ("TP4 👀 ?" → tp_hit tp=4, ancorado
  ao início para "Who's with me for TP3 👀" nunca disparar). Verificado
  contra as mensagens reais da BD — 2 mensagens que se perdiam agora
  classificam, sem regressões. +1 teste = **123 testes**.
- **Limpar reinícios à mão**: novo `DELETE /api/listener_restarts`
  (`DB.clear_listener_restarts`) + botão "🧹 Limpar reinícios" na tab
  Diagnóstico com confirmação — além do purge automático de 30 dias.
  +1 teste = **124 testes**.
- **Terminais de fundo com tema de trading**: em vez de assembly genérico,
  os terminais CRT mostram agora **sinais/gestão realistas** (XAUUSD BUY
  TP1..TP5 SL, "TP1 HIT +40 pips", ticks de preço, fills, saldo/equity)
  e são **animados**: scroll contínuo + **linha de estado ao vivo**
  (ticker a mudar a cada 1.2s com cursor a piscar) + **flash ocasional**
  numa linha. Aplicado também na página de login.

### 2026-08-17 — Conta MT5 nova + purge de reinícios + dashboard roxo/vermelho
- **Troca de conta MT5**: demo **Camilo Diogo** (`MT5_LOGIN=5054586550`,
  `MetaQuotes-Demo`, senha no `.env`), substituindo a anterior
  (111209744). `.env` + rodapé da dashboard (agora **dinâmico** — lê o
  login/servidor do snapshot em vez de hardcoded) + README/AGENTS/DEPLOY
  atualizados. Servidor reiniciado; `/api/status` confirma
  `login: 5054586550`, `mode: mt5-real`. ⚠️ AutoTrading do terminal MT5
  estava OFF após a troca — ligar no terminal para as ordens executarem.
- **Purge automático de `listener_restarts`**: registos com mais de 30
  dias são apagados — no arranque e a cada ~6h no watchdog
  (`db.purge_old_listener_restarts(30)`; compara `substr(started_at,1,10)`
  imune a fusos). A tabela já não cresce sem limite. +1 teste =
  **122 testes**.
- **Dashboard cyberpunk roxo/vermelho**: paleta nova (painéis roxos,
  `--red: #ff2a4a`, `--violet: #c449ff`), gradientes/bordas/brilhos
  recoloridos (header, stats, tabs, botões primários, tab-count,
  scrollbars, scanlines, boot) e login.html a par.
- **Fundo animado CRT**: terminais de código (assembly/hex/registos,
  estilo da imagem de referência) fixos à esquerda e à direita, com
  scroll vertical contínuo, brilho roxo/vermelho e reticle central;
  `prefers-reduced-motion` e escondidos em ecrãs < 960px.

### 2026-08-17 — Slider da janela de edição + histórico de restarts
- **Slider "janela edição" no bloco Limites**: muda `edit_window_seconds`
  (10–120s, passo 5) direto da dashboard, com debounce — sem editar código
  (verificado ao vivo: 45 → 30).
- **Histórico de reinícios do listener**: nova tabela `listener_restarts`;
  o watchdog regista cada spawn (`watchdog: heartbeat parado`) e o
  `/api/diagnostics` expõe os últimos 10 — a tab Diagnóstico mostra
  "🔄 Reinícios do listener" com hora + motivo (verificado ao vivo após
  matar o listener: reiniciou e registou 15:34).
- **Teste de regressão do watchdog**: sequência real — sem listener spawna
  1; restart do servidor com listener ativo NÃO duplica. +2 testes =
  **121 testes**.

### 2026-08-17 — Watchdog: nunca duplica o listener
- O watchdog spawnava um segundo listener quando o servidor reiniciava
  (o heartbeat do listener existente ainda não tinha registado no estado
  novo). Agora `_ensure_listener_alive` verifica se **já existe** um
  processo `telegram_listener` a correr (`_listener_running_anywhere`,
  wmic) antes de spawnar — se existir, põe cooldown e espera o heartbeat.
- Fix de um `async def` acidental no helper (coroutine nunca awaitada
  crashava o watchdog em silêncio).
- Testes: +1 (não duplica quando já existe listener) = **119 testes**.

### 2026-08-17 — Janela de edição: 60s → 30s
- A espera pelas edições do Telegram baixou de **60s para 30s**
  (`edit_window_seconds` no `DEFAULT_CONFIG` + config persistida na BD):
  os sinais executam mais depressa em modo auto. Docs atualizadas
  (AGENTS.md, TECHNICAL.md, CHANGELOG). Sem novos testes — os testes de
  janela definem o valor explicitamente. **118 testes**.

### 2026-08-17 — Lista de utilizadores ligados no header
- O header da dashboard mostra agora **quem está ligado** (pill verde
  `👥 admin · admin`) mesmo antes do relógio, com tooltip e contagem.
- Backend: `WSManager` guarda metadados por ligação WebSocket
  (`{user, since}`) e o snapshot/WS expõe `online_users` — atualiza em
  tempo real (cada tab aberta = uma entrada; fechou a tab, desaparece).
- Testes: +1 (online_users via WebSocket) = **118 testes**.

### 2026-08-17 — Fix: dashboard presa no boot via túnel HTTPS
- **Bug**: ao aceder pela URL HTTPS (cloudflared/VPS), o login entrava mas a
  dashboard ficava presa no boot ("DASHBOARD ONLINE..." sem sair). Duas
  causas: (1) o WebSocket usava `ws://` — num site HTTPS o navegador
  bloqueia mixed content e o snapshot nunca chegava; (2) `finishDashBoot()`
  só escondia o overlay se `_bootSnapshotDone` fosse true, mas o fallback de
  ~3s chamava-o com a flag a false → preso para sempre.
- **Fix**: `wss://` quando `location.protocol === "https:"` (ws:// no local)
  e `finishDashBoot()` esconde sempre o overlay (com snapshot ou no
  fallback). Só ficheiro estático — sem reinício do servidor.

### 2026-08-17 — Deploy: alternativa Azure no DEPLOY.md
- Nova secção 0.1 no `docs/DEPLOY.md`: testar na **Azure gratuita** —
  crédito $200/30 dias com VM `Standard_B2s` (2 vCPU/4 GB) para teste
  real 24/7, vs VM gratuita `B1s` (1 GB — demasiado curta para MT5+bot);
  passos de criação (Windows Server 2022 Azure Edition · West Europe ·
  NSG com 3389 restrita e 443) e aviso do fim do crédito aos 30 dias
  (migrar para Vultr ~$16/mês vs Azure ~$50/mês). Docs only.

### 2026-08-17 — Deploy: guia Vultr no DEPLOY.md
- `docs/DEPLOY.md` atualizado com a **Vultr** como fornecedor escolhido:
  passos de criação (Cloud Compute · Windows com licença incluída ·
  Frankfurt/Amesterdão · 2 vCPU/4 GB/80 GB · Auto Backups), nota de
  snapshot antes de ir para real e sugestão de Cloud Firewall da Vultr
  (443 aberto, RDP só para o teu IP). Sem alteração de código — docs only.

### 2026-08-17 — "Do it now team" (GO) reconhecido como gestão
- **Bug**: "Do it now team" (o GO do admin, msg 4125) era auditada como
  conversa ignorada — não é instrução de TP/SL/close, mas é a confirmação
  de entrada imediata.
- **Fix**: novo `kind: go` no classificador (`manage.py`, padrão
  `do it now | enter now | go now | jump in now`): registado como gestão e,
  se houver um sinal em `awaiting_edit` (o da mensagem a que responde ou o
  mais recente), **finaliza já** — auto → executa agora (janela encurtada),
  manual → pendente. Sem sinal em espera regista apenas (sem fechar nada,
  sem notificação de risco).
- **Testes**: +3 (classify GO; GO finaliza sinal em espera via reply; GO sem
  sinal regista-se sem risco) = **117 testes**.

### 2026-08-17 — "I'm buying/selling" entra na janela de edição
- **Bug**: o regex `_SIGNAL_HINT` usava `\b(buy|sell)\b` — "buying"/
  "selling" não casavam (sem boundary antes de "ing"). A primeira linha do
  sinal do grupo ("I'm selling XAUUSD") ia para a auditoria de ignoradas;
  quando a edição com TP/SL chegava, entrava como sinal — a mesma mensagem
  aparecia nas Ignoradas E nos Sinais (msg 4123).
- **Fix**: `_SIGNAL_HINT` aceita agora `buying|selling|comprando|vendendo` —
  a primeira linha entra logo na janela de edição (60s) e as edições do
  Telegram atualizam-na; e quando uma mensagem ignorada é editada para
  sinal, o registo da auditoria é removido (`delete_ignored`) para não
  duplicar.
- **Testes**: +2 (gerúndio entra na janela; ignorada-editada limpa a
  auditoria) = **114 testes**.

### 2026-08-17 — "Última msg" correta + alertas OFFLINE no telemóvel + feed limpo
- **Fix "última msg há Xs" (Diagnóstico)**: o listener marcava `last_msg_at`
  com a hora de arranque após o replay do histórico, por isso a dashboard
  mostrava "última msg há 7s" logo a seguir a um restart, mesmo sem nenhuma
  mensagem nova. Agora só os handlers de mensagens reais (`on_new`/`on_edit`)
  atualizam o timestamp — o replay do histórico não conta como mensagem. A
  dashboard mostra "sem mensagens ainda" até chegar a primeira do grupo.
- **Alertas OFFLINE no telemóvel (Telegram)**:
  - MT5 desligado / AutoTrading off → o listener faz DM à própria conta
    (Saved Messages, sessão Telethon — sem bot, sem setup) e o servidor
    emite notificação risk (feed + som + push se bot configurado);
  - Listener offline → o watchdog emite notificação risk ao reiniciar;
  - Opcional `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` no `.env` para
    empurrar qualquer notificação para o telemóvel via bot (@BotFather).
- **Feed limpo**: apagadas 29 notificações duplicadas do replay antigo
  (resumos/divergências das 12:36–13:04, geradas por restarts anteriores);
  o ground truth dos resumos fica na tabela `group_summaries`.
- **Testes**: +2 (queda do MT5 notifica; watchdog do listener notifica) =
  **112 testes**.

### 2026-08-17 — Auto-restart do listener + código do Telegram + gráfico de uptime
- **Auto-restart do listener**: o servidor tem um watchdog (loop a cada 10s)
  que reinicia o processo do listener automaticamente quando o heartbeat
  parar (>5s offline) — matar o listener e ele volta sozinho (verificado).
- **Código do Telegram na dashboard**: se o listener precisar de login novo
  (sessão expirada/dispositivo novo), avisa o servidor
  (`POST /api/listener/need_code`), a dashboard mostra um toast com input e
  o utilizador introduz o código (`POST /api/listener/code`); o listener
  busca-o uma única vez (`GET /api/listener/code`) e liga. O heartbeat
  arranca ANTES da auth para o watchdog nunca reiniciar durante o arranque.
- **Toast + som quando o listener fica OFFLINE**: `watchListener()` compara o
  estado a cada snapshot — toast persistente vermelho + som de risco na
  queda, toast verde + som de sucesso ao voltar.
- **Gráfico de uptime 24h** na tab Diagnóstico: amostras a cada 10s
  (listener/MT5 ligado vs em baixo, com % de uptime), histórico em memória
  (deque 24h), downsample para ~300 pontos no endpoint.
- **Nota Windows**: cada processo python da venv aparece em par
  (launcher + interpretador real, Python 3.14) — não são duplicados.
  Matar listeners: `powershell ... CommandLine -like '*telegram_listener*'`.
- Suite: **110** testes.

### 2026-08-17 — Tab Diagnóstico + heartbeat do listener a cada 1s + polish
- **Tab "🔧 Diagnóstico"**: estado ao vivo de MT5 (ligação, conta, saldo/equity,
  AutoTrading), Telegram listener (heartbeat, última mensagem do grupo com cor:
  verde <15m, âmbar <2h, vermelho >2h), servidor (uptime, PID), market feed
  (real/sim), login, base de dados (contagens + ficheiro) e última mensagem do
  grupo. Atualiza a cada 10s enquanto a aba está aberta.
  (`GET /api/diagnostics` + `POST /api/heartbeat`).
- **Heartbeat do listener a cada 1s**: o listener envia o timestamp da última
  mensagem recebida; o servidor marca-o OFFLINE em ~5s sem notícias — deteta o
  processo morto em silêncio (que já aconteceu antes) e mostra a frescura das
  mensagens do grupo.
- **Fix**: resumos do replay histórico deixaram de notificar o feed (só os ao
  vivo notificam + divergência) — o reinício do listener já não inunda a
  dashboard com notificações repetidas.
- **Polish geral**: helper `when()` (hoje/ontem/DD-MM) em Sinais, Histórico,
  feed, Edge e Ignoradas; direções em PT (▲ compra / ▼ venda); estado das
  trades em PT; headers "Quando"; ** removidos da "Última do grupo"; janela de
  edição dinâmica no modo hint; gráfico diário em DD/MM; tooltips com data
  completa nas Ignoradas. Suite: **109** testes.

### 2026-08-17 — Dia visível nas tabelas de Sinais/Histórico
- As colunas de hora em Sinais, Histórico, feed e "Última do grupo" mostravam
  só HH:MM (e UTC cru nos históricos do Telegram). Novo helper `when()`:
  hoje → só hora; ontem → "ontem HH:MM"; mais antigo → "DD/MM HH:MM" —
  sempre em hora local. Suite: 107 testes.

### 2026-08-17 — Limpeza: um só grupo no filtro de sinais
- O filtro mostrava 2 entradas para o MESMO grupo: "tg:Golden Moves ⚜️"
  (nome real com emoji, 141 msgs do listener) e "tg:Golden Moves" (4 linhas
  antigas de testes: 3 gestão sem message_id real + 1 sinal de teste já
  apagado). Verificado que nenhuma tinha trades/posições/pending a
  referenciá-las e apagadas — o listener guarda sempre o título real.
- Suite: 107 testes.

### 2026-08-17 — Auditoria de mensagens ignoradas + listener reencaminha tudo
- **Tab "🚫 Ignoradas"** na dashboard: mostra a conversa do grupo que o bot
  não regista (hora, origem, texto completo) com badge ao vivo
  (`GET /api/ignored` + `ignored_count` no snapshot). Tabela nova
  `ignored_messages` (dedup message_id+source, mantém últimas 1000).
- **`SIGNALBOT_TG_FILTER=0`** no `.env`: o listener reencaminha TUDO; a
  decisão passou para o backend — `classify()` → gestão; `_SIGNAL_HINT`
  (mesmo regex do antigo filtro, agora em `state.py`) → sinal; senão
  conversa → auditoria + `action: ignored`. Mensagens sem keywords nunca
  entram na janela de edição.
- Testes novos: conversa ignorada+auditada, typo de sinal ainda espera a
  janela, gestão sem alvo dispara notificação risk (alerta que já existia,
  agora testado). Suite: **106** testes.

### 2026-08-17 — Log de skip do listener mostra o texto
- O log `[skip] ... não parece um sinal` do listener passa a incluir os
  primeiros 90 caracteres da mensagem ignorada — diagnóstico mais rápido
  sem ter de ir buscar a mensagem ao Telegram (mensagens ignoradas são
  conversa do grupo, não sinais). Suite: 103 testes.

### 2026-08-17 — Boot sequence + sons na dashboard
- Boot sequence cyberpunk à entrada da dashboard (após o login): linhas de
  terminal com cliques de teclado sintetizados; quando o primeiro snapshot
  chega, acrescenta o estado REAL (MT5 BRIDGE OK/OFFLINE, ALGO TRADING,
  MODO REAL (DEMO)) e faz fade-out para a dashboard já carregada.
- Sons sintetizados nas notificações do feed: trade aberta (2 bips agudos),
  trade fechada (blip), risco (2 bips graves de alerta), gestão/sumário/sistema
  (blip subtil).
- Sons de alerta quando o MT5 desliga ou o AutoTrading desliga (grave) e de
  sucesso quando voltam a ligar (agudo) — além do toast persistente já existente.
- O áudio respeita a política de autoplay: desbloqueia no primeiro clique. Suite: 103 testes.

### 2026-08-17 — Removido botão de mute do login
- O botão 🔊 de mute foi removido a pedido do utilizador — o som controla-se
  no próprio browser (mute do separador/aba).
- Mantidos: cliques de teclado no boot, beeps grave (erro) / agudo (sucesso),
  persistência da preferência deixou de ser necessária (sem estado de som).
- Limpeza do comentário obsoleto no login.html. Suite: 103 testes.

### 2026-08-17 — Boot sequence no login + favicon neon + README/scripts

- **Boot sequence no login**: terminal cyberpunk com linhas a aparecer
  (INIT PROJECT NEXGEN · MT5 BRIDGE OK · TELEGRAM LISTENER OK · RISK
  ENGINE OK · AUTH SYSTEM OK · PROCEEDING TO LOGIN) a 330ms/linha, depois
  fade-out para o form.
- **Boot pós-login**: após autenticar, o ecrã mostra "VERIFICAR ACESSO..."
  → "ACESSO PERMITIDO" antes de entrar na dashboard (a verificação de
  acesso só acontece depois do login).
- **Erro de password com efeito glitch**: a caixa sofre um glitch de
  fatias deslocadas (clip-path, sem shake), título com glitch vermelho,
  input com borda vermelha e o botão fica vermelho a dizer "Password
  incorreta" (1,8s), com contador "Tentativa N — acesso negado".
- **Favicon a piscar em vermelho** no separador quando a password falha
  (alterna vermelho/ciano durante o erro e fica vermelho; volta ao normal
  quando o erro termina) — o aviso é visível mesmo fora da página.
- **Favicon a piscar em verde** no login bem-sucedido: o botão muda para
  "Password correta ✓" em verde (visível ~1s antes do boot pós-login) e o
  favicon alterna verde/ciano, antes de entrar na dashboard.
- **Beeps sintetizados** (Web Audio): 2 bips graves no erro de password e
  2 bips agudos (880→1320 Hz) no sucesso — feedback auditivo.
- **Som de teclado no boot**: cada linha do terminal toca um clique curto
  sintetizado (~1.7–2.2 kHz, 35ms). **Botão de mute 🔊/🔇** no login
  (persiste em `localStorage`), desliga os cliques do boot e os beeps.
- **Fix (bug TDZ)**: `nextLine()` arrancava antes de `let soundOn` estar
  inicializado e o erro parava o resto do script (mute e submit nunca
  eram ligados) — o boot passou a arrancar no fim do script.
- **Favicon neon**: ícone "N" em ciano com moldura neon (SVG inline,
  data-URI) no separador da dashboard e do login.
- **README.md reescrito** para o estado atual (execução real na demo MT5,
  TP1..TP5 + gestão, auth, tema cyberpunk, docs) — estava desatualizado
  (paper, TP1/TP2).
- **start.ps1 / start.sh**: textos de arranque com o nome Project NexGen.
- Suite: **103 testes**.

### 2026-08-17 — Fix: /login depois de Sair dava 404

- O botão Sair redirecionava para `/login`, mas o middleware deixava esse
  path passar ao StaticFiles (que procura um ficheiro "login" inexistente)
  → 404. Agora `/login` serve a página de login (`_login_page()`); com
  sessão válida, redireciona para `/` (302).
- Nota no AGENTS.md: no Windows, `kill` do Git Bash não mata o uvicorn —
  usar `taskkill //PID <pid> //F` e confirmar a porta livre.
- Novo teste `test_login_page_after_logout`. Suite: **103 testes**.

### 2026-08-17 — Renomeado para Project NexGen + glitch mais vivo

- **Nome do programa**: "SignalBot" → **"Project NexGen"** (dashboard,
  login, título, footer, `APP_NAME` do backend, docs e testes).
- **Glitch do título mais frequente**: ciclo de 5s → **2.6s** com janela
  de glitch maior (3 cortes por ciclo).
- **Login no estilo Cyberpunk 2077**: fontes Orbitron/Chakra Petch, cores
  neon, scanlines CRT e glitch no título, coerente com a dashboard.
- **Glitch subtil nos preços dos tickers** (XAUUSD, EURUSD…): RGB split
  raro (~1s a cada 9s) via `data-text` nos valores.
- Suite: **102 testes**.

### 2026-08-17 — Cyberpunk 2077 como tema único

- **Tema Cyberpunk 2077 passa a ser o único e padrão**: `data-theme="cyberpunk"`
  fixo no `<html>`; seletor de tema e lógica de `localStorage` **removidos**.
- **Efeitos CRT**: scanlines subtis em todo o ecrã (overlay fixo com
  `repeating-linear-gradient`, sem bloquear cliques) e **glitch intermitente**
  no título (RGB split com `clip-path`, ~1s a cada 5s) + flicker na linha de
  gradiente do header.
- Suite: **102 testes**.

### 2026-08-17 — Site mais profissional (navegação + display)

- **Header sticky** (fixo no topo, blur de fundo) e **tabs sticky** com
  **contagens**: badge no tab Sinais (pendentes) e no tab Posições
  (trades abertas), visível quando > 0.
- **KPIs com ícones e tooltips**: 💰 Equity, 🏦 Balance, 📊 P&L aberto,
  📈 P&L hoje, 🧾 Trades abertas, ⏳ Sinais pendentes.
- **Feed de notificações colorido por tipo** (verde trade, âmbar risco,
  azul sistema/gestão, violeta resumo).
- **Tabelas com zebra** (linhas alternadas) + footer com conta/servidor.
- **Responsividade**: mobile com 2 colunas nas métricas e menos padding.
- Suite: **102 testes**.

### 2026-08-17 — Polish visual + risco por trade explícito

- **Risco por trade explícito**: o painel passa a explicar que o % é do
  **total da conta** e representa a **perda se o SL bater** (não a margem
  usada); rótulos/tooltips claros no slider, chips ("Perda se o SL bater",
  "Por perna", "Limite diário", "P&L hoje") e dica do modo ("se o SL
  bater, perdes X% da conta (€…)").
- **Polish geral**: sombras nos cards, hover com elevação em botões/stats,
  hover nas tabs, scrollbar personalizada, relógio em chip, grupo de ações
  no header.
- **Botão "Sair"** junto ao Kill switch (`.hdr-actions`, estilo ghost);
  ícone 🚪 removido (não renderizava bem no Windows).
- Suite: **102 testes**.

### 2026-08-17 — Autenticação da dashboard + deploy VPS

- **Login na dashboard** (`app/auth.py` + middleware em `main.py`): com
  `SIGNALBOT_DASHBOARD_PASSWORD` definida, `/` serve `login.html`, toda a
  API devolve 401 sem sessão, `/ws` fecha com 4401. Sessões em memória
  (cookie httpOnly, 7 dias). Login ativo com o utilizador `admin`.
- **Token do listener** (`X-API-Token` = `SIGNALBOT_API_TOKEN`): o
  listener autentica sem browser/cookie; se a password da dashboard
  mudar, o listener continua a funcionar.
- **Botão "🚪 Sair"** na dashboard (visível quando a auth está ligada);
  JS redireciona para `/login` em qualquer 401.
- **`tests/test_docs.py`** — teste anti-regressão: o AGENTS.md tem de
  mencionar o número real de testes da suite (falha se a suite crescer
  sem atualizar as docs).
- **`docs/DEPLOY.md`** — guia completo de deploy numa VPS Windows
  (Python + MT5 + Caddy HTTPS + firewall + `start.ps1`).
- **`docs/CHANGELOG.md`** — este ficheiro.
- Suite: **102 testes**.

### 2026-08-17 — Polish da dashboard (modo auto transparente)

- **Preview do risco em €**: o que o `risk_pct` realmente representa —
  € em risco por trade, por perna (TP1..TP5), limite diário e P&L de
  hoje; atualiza em tempo real ao arrastar o slider.
- **Barra de estado ao vivo** no topo: modo, execução, countdown da
  janela de edição (tick a cada segundo), última mensagem do grupo,
  estado de pausa.
- **Dica do modo** (auto/manual/pausado) com o que acontece ao próximo
  sinal e o € em risco.
- **Flash do saldo** quando muda (verde/vermelho).
- **Gráfico "P&L por dia"** na tab Relatório (barras de € + linha do win
  rate) alimentado pelo novo campo `by_day` do `/api/report`.
- **Toast persistente** quando o MT5 cai ou o AutoTrading desliga
  (fica até ser resolvido; com "✅ MT5 reconectado" ao voltar).
- **Auditoria do risco no feed**: a notificação `trade_open` mostra o €
  real em risco (`risco 2.0% da equity = 1.975,88 EUR`).
- Rótulo "Perdido hoje" → **"P&L hoje"** (fazia sentido com ganhos).
- Teste flaky corrigido (`test_management_reply_queued_until_trade_opens`
  agora faz polling em vez de `sleep` fixo). Suite: 97 → 102 testes.

### 2026-08-17 — Regra de documentação obrigatória

- AGENTS.md: nova secção **"Manter a documentação atualizada"** — toda a
  alteração tem de atualizar AGENTS.md / TECHNICAL.md / PUBLIC.md no
  mesmo turno; corrigido o nº de testes (101 → 102) e documentada a auth.

### 2026-08-17 — Correlação por reply do Telegram

- `Signal.reply_to_msg_id` guarda a relação de resposta (Telethon:
  `message.reply_to`); a dashboard mostra "📨 gestão · → responde ao
  sinal HH:MM SÍMBOLO" no histórico.
- `_handle_management` escolhe o alvo por correlação exata (reply), depois
  mesma símbolo, depois mais recente.
- `pending_management` (tabela nova na BD): instruções cujo sinal ainda
  está na janela de edição ficam em fila e são aplicadas quando a trade
  abrir (`_apply_pending_management`).
- **Alerta de gestão sem alvo**: mensagem de gestão sem trade nem sinal a
  que responder dispara notificação ⚠️ (nunca silenciosa).

### 2026-08-17 — Fix do saldo (11k → real)

- Causa: o `main.py` não carregava o `.env` → servidor arrancava em `sim`
  com saldo de papel. Fix: `load_dotenv(override=False)` no arranque +
  `EXECUTION_MODE=sim` forçado nos testes. Dashboard passou a mostrar o
  saldo real do broker (€99.097,53).

### 2026-08-17 — Execução real na demo + E2E

- Badge/subtítulo "⚡ execução real (demo MT5)" (sem "simulação").
- **Bug real corrigido**: o override de lotes (`lots: 0.01`) perdia-se —
  a tabela `signals` não tinha a coluna `lots`; adicionada (schema +
  migração + insert/read). Um teste E2E abriu 0.01 lotes × 2 pernas no
  MT5, confirmou na dashboard e fechou.

### 2026-08-17 — Walk-forward + close_tp1 + alerta de divergência

- **Walk-forward** (`/api/analysis/walkforward` + bloco no Edge): o PF da
  gestão por janelas temporais (4/4 janelas positivas, PF 2.11 → 11.00).
- **`close_tp1`** no classificador/sim/live: "Close TP1 here" fecha só a
  perna 1 sem mexer nos SLs (distinto de `protect`).
- **Alerta de divergência grupo vs bot**: resumo novo do grupo com WR
  ≥10pp afastado do backtest → notificação ⚠️.

### 2026-08-17 — Ground truth dos resumos + gestão no backtest

- Backtest com variante **manage** (protect/close_all/close_tp1 do grupo
  acoplados por fonte+janela temporal, SL-first, fills sem look-ahead):
  PF sobe de 2.70 → **4.39**.
- Histórico re-carregado com os **6 resumos** do grupo como ground truth
  (Monday 80% … Weekly **91%**); bloco no Edge compara grupo vs bot
  (−14.1pp).
- `classify` corre antes da dedup; `insert_group_summary` idempotente por
  `message_id`; gestão histórica nunca age em trades abertas.

### 2026-08-17 — 5 TPs na estratégia

- Estratégia alargada de TP1/TP2 para **TP1..TP5** (ordens 1 por TP no
  MT5, breakeven das restantes após TP1, tab Posições com cada perna e o
  seu TP individual e o sinal a que pertence).

### 2026-08-17 — Infraestrutura MT5

- **MT5Engine** com a mesma interface do SimEngine + `EXECUTION_MODE`
  (`sim` / `mt5`) + dry-run (`MT5_DRY_RUN`).
- Badge de execução (sim / mt5-dry / mt5-real, conta ligada, saldo,
  equity) no header.
- Alerta no log/feed quando o broker recusa ordens (retcode != 0) — nunca
  mais trades fantasma sem aviso.
- Botão de reinício do terminal MT5 / deteção de AutoTrading OFF.
- Posições abertas do MT5 (lotes, preço, P&L em EUR) na dashboard,
  sincronizadas a cada segundo.
- `start.ps1`/`start.sh`: arranque de tudo com um comando, sem guardar a
  senha em ficheiro.

### 2026-08-17 — Edge analysis + preços reais

- **Tab Edge** na dashboard: backtest dos sinais reais, veredito
  (edge/marginal/none), A/B/C (antiga vs nova vs nova+gestão), walk-
  forward, ground truth do grupo.
- **Preços reais** XAUUSD via Yahoo (GC=F) com fallback simulado;
  indicador real-vs-sim por símbolo no header.
- Fix dos preços fantasma: reset do saldo de papel e remoção das 3 trades
  fantasma XAUUSD que tinham lucrado +$135.000.

### 2026-08-17 — Reconhecimento de mensagens

- Classificador de gestão (`app/messages/manage.py`): TP HIT, SL HIT,
  close TP1, protect, close all (BE ou não), summaries.
- Parser de sinais com sanity checks obrigatórios; LLM fallback opcional
  (nunca executa em auto).
- Protocolo "refinar o reconhecimento" documentado no AGENTS.md.

---

## Versões anteriores (resumo)

- Listener Telegram (Telethon) a ler o grupo Golden Moves com histórico
  (~200 mensagens) e dedup por `message_id`.
- SimEngine (paper) + dashboard com tabs Sinais / Posições / Histórico /
  Relatório / Edge; autenticação por token inicialmente planeada.
- N-últimas: removida a tab do simulador (a partir do modo MT5 real os
  sinais do grupo executam na demo).
