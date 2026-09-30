---
trigger: model_decision
description: "Mapa dos ficheiros que importam do Project NexGen e como as duas contas MT5 estao ligadas: a lane principal no processo do servidor, a lane tp2 num worker isolado em processo separado, o RemoteProvider, o fail-closed, o boot da lane 2, o smoke test dual-lane e o lifecycle dos loops. Ler antes de mexer em app/state.py, app/execution/**, app/lane2_worker.py, no worker da 2a conta, em ordens, posicoes, threading, no event loop, ou em qualquer coisa que toque nas duas contas."
---

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
| `app/execution/engine.py` | SimEngine (paper) - mesma interface do MT5Engine |
| `app/execution/paper.py` | PaperRace - contas virtuais, corrida normal e shadow-paper isolado |
| `app/execution/mt5.py` | MT5Engine - ordens reais/dry-run no MetaTrader 5; bloqueia conta real não-hedged |
| `app/maintenance.py` | Rotação bounded de logs, check de disco, auditoria DB e teste de restauração |
| `app/server_runner.py` | Entry point Uvicorn que instala os logs rotativos antes do servidor |
| `maintenance.py` | CLI read-only para auditoria de tabelas e espaço em disco |
| `app/execution/protection.py` | Núcleo puro/reducer de decisões de proteção comum aos motores (escada/trailing/ATR/confirm/time stop/auto-protect + merge do `protect`) |
| `app/backtest.py` | Replay de sinais reais em candles históricos (+ gestão) |
| `app/analysis.py` | Edge analysis, A/B/C compare, walk-forward |
| `app/db.py` | SQLite (sinais, trades, posições, summaries, config) |
| `app/static/style.css` + `common.js` | CSS partilhado + JS comum (helpers, boot, WS, chrome global) - carregados por todas as páginas |
| `app/static/index.html` | Dashboard principal (JS vanilla, sem framework) - tabs sinais/posições/histórico/relatório/ignoradas + feed + corrida paralela |
| `app/static/teste.html` | Página de teste de estratégias (`/teste?s=<id>`) - simulação, walk-forward, corrida paper |
| `app/static/diagnostico.html` | Página de diagnóstico (`/diagnostico`) - MT5, listener, BD |
| `app/static/help.html` | Página de ajuda (`/help`) - estratégias + glossário; `HELP_FLOW` e conceitos têm de acompanhar todas as estratégias visíveis |
| `tests/` | `test_api.py`, `test_backtest.py`, `test_management.py` |

**Modelo de concorrência:** todo o estado vive no event loop do FastAPI;
o SQLite tem um lock interno (`DB.tx`). Não há corridas.

## Lanes de execução - 2ª conta MT5 (worker isolado)

A conta principal (`MT5_LOGIN`, lane `main`) executa no processo do servidor.
A **2ª conta demo** (`MT5_2_LOGIN`, lane `tp2`) existe por omissão em
**dry-run** (`mt5-dry`: trades registadas na BD, nada enviado) e, quando
ativada, em **execução real num processo separado** - porque a API
MetaTrader5 mantém **UMA ligação global por processo** e a principal já ocupa
essa ligação.

- **Estado atual (02/09, ativado)**: a lane `tp2` (5055369549, demo 10k)
  corre `tp2_protect` - a mesma estratégia da principal, por decisão do
  utilizador para o A/B ser like-for-like - em **execução real** através do
  worker (terminal portable
  `C:\MT5Portable2`); a config `lane2_strategy`/`lane2_label` é definida por
  `POST /api/config` (a config persistida sobrepõe o env `MT5_2_STRATEGY`).
  O endpoint valida contra o registo e **bloqueia `auto_escada_3`/`auto_escada_5`
  na lane MT5** (gate da Fase 3: N≥20 · PF≥1.5 · DD≤125% antes de exposição
  MT5). O risco por trade da lane vem do `risk_pct:tp2` (base % por conta);
  para a lane abrir com **lote fixo por perna** (ex.: 0.04 em vez de variar
  com a distância ao SL) define-se `lots_override_tp2` via `POST
  /api/config` (persiste em `lots_override:tp2`; 0 = volta ao risco) - o
  risk_preview e o snapshot da lane expõem o fixo (`lots_override` na
  preview, `fixed_lots_per_leg` na entrada).
- **Worker (`app/lane2_worker.py`)**: processo isolado dono da ligação MT5 à
  2ª conta (terminal **portable**, `MT5_2_TERMINAL_PATH`) que expõe as
  operações por HTTP local (`/ping /status /initialize /account /positions
  /quote /meta /copy_rates /constants /symbol_select /mt5/call`) com token
  (`LANE2_WORKER_TOKEN`, fallback `SIGNALBOT_API_TOKEN`). O processo principal
  usa `RemoteProvider` (`app/execution/remote2.py`, mesma interface duck-typed
  do `MT5Provider`, incluindo `provider._mt5` que o `MT5Engine` usa) - **o
  worker nunca toca na SQLite nem decide nada**, preservando a invariante
  "todo o estado vive no event loop".
- **Fail-closed**: sem ligação (worker offline, terminal 2 ausente, conta não
  hedged, AutoTrading off) a lane 2 não envia ordens - `open_trade` real
  levanta erro. Um worker offline NUNCA partilha a ligação da principal.
- **Boot da lane tp2 (corrigido 16/09)**: o lifespan inicializava o
  `mt5_provider2` LOCAL (com bloqueio permanente por design quando
  `MT5_DRY_RUN=0`) em vez do `RemoteProvider` do worker - a init da tp2
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
  cancelada no shutdown) - cobre a corrida com o arranque do worker portable
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
  real de 02/09: as 3 trades tp2 de 01/09 eram phantom, sem fills reais -
  com n=0 não há base para baixar o limiar) - abaixo disso o veredicto é
  neutro ("amostra insuficiente") e explica a amostra. **Fechos phantom
  (sem `exit_reason` - ordens recusadas pelo broker, ex.: AutoTrading OFF,
  fechadas a 0 no próprio tick) NUNCA entram no comparável**: ficam em
  `voids`/`void_count` (com as flags por lane) para os 0.0 não contarem
  como resultado de nenhuma estratégia - os 3 pares de 01/09 (AutoTrading
  OFF na lane 2 durante o dia) aparecem como "fechos phantom" no
  Diagnóstico, não como perda/ganho. Ao vivo, um fecho phantom dispara
  também um **alerta risk distinto** ("👻 … SEM fill real") e o motivo da
  recusa fica **gravado na trade** (`error`/`retcodes`/`auto_trading_off`,
  colunas migradas automaticamente) para a auditoria explicar o retcode.
  O bloco "⚖️ A/B lanes" do Diagnóstico **refresca com o feed (10s)** - a
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
  `refusals_backfill_week_last_run`) e notifica quando encontrou algo -
  nenhum phantom fica sem trace durável mesmo com o alerta perdido.
  **Delete de pendentes robusto**: o `order_delete` da expiração por TTL e do
  cancelamento tem tratamento por perna - falha (worker offline) mantém a
  perna `pending_fill`, grava a exceção e o próximo tick repete; após 5
  falhas seguidas o feed recebe o alerta risk `pending_delete_stuck` (uma
  vez por episódio; re-alerta a cada 30 min enquanto a perna continuar
  presa). O **retry é imediato**: a intenção do delete falhado (TTL ou
  cancelamento de gestão) fica guardada e o tick completa o cancelamento no
  próprio tick em que o worker voltar - sem esperar o TTL. O Diagnóstico
  expõe as pernas presas no bloco "⏳ Ordens pendentes em retry de delete"
  (tentativas/escalonada/ticket/intenção/último alerta) via `stuck_pending`
  em `/api/diagnostics` (read-only).
  **Estado do watchdog PERSISTIDO** (tabela `pending_delete_states`, por
  lane): tentativas, intenção do delete e timestamps de alerta sobrevivem a
  um restart do servidor - o Diagnóstico restaura o bloco e uma perna já
  escalonada volta a alertar no 1º retry pós-reboot (marca durável
  `escalated_at`); linhas órfãs (perna preenchida/fechada ou trade
  encerrada) são limpas automaticamente pelo tick. **Exceções
  rate-limited**: enquanto a perna presa falha a cada segundo, só a
  PRIMEIRA falha do episódio grava `operational_exceptions` (uma exceção
  concisa por perna, nunca centenas de linhas idênticas) - um episódio
  novo volta a gravar. **Evidência de replay**: cada marca
  `pending_delete_stuck` entra no journal (opt-in) com o contexto completo
  da perna (leg/ticket/position/lane/tentativas/intenção/razão/timestamps)
  - combinada com os `pending_order_result` falhados, a fita reproduz uma
  falha prolongada do worker de forma determinística. A **exportação da
  fita** (`GET /api/operations/replay/tape`) inclui estas marcas (passivas,
  nunca geram ordens no replay) e o relatório read-only
  `GET /api/operations/replay/outages?days=30` junta-as numa **linha
  temporal de worker outages** (timeline cronológica + episódios por
  trade/ticket: falhas, escaladas, re-alertas, intenção e resolução
  derivada do journal - delete aceite ou fill depois da 1ª falha) - bloco
  "⏳ Worker outages · delete de ordens pendentes" no /auditoria.
  **Retenção bounded de `operational_exceptions`**: além do rate-limit por
  episódio, o purge periódico do watchdog (~6h) e do arranque apaga linhas
  com mais de 365 dias (`db.purge_old_exceptions(365)`, política em
  maintenance.py) - a tabela nunca cresce sem limite.
- ⚠️ **GUARD CRÍTICO (incidente 01/09)**: sem `MT5_2_TERMINAL_PATH` o worker
  **recusa arrancar (exit 2)**. Sem uma instalação portable, `initialize(
  login=...)` liga ao **terminal por omissão** e troca a conta do terminal da
  principal (observado em produção: a conta passou a apontar para a 111495351
  até ser relogada). Ativar a lane 2 real exige, por ordem: instalar o 2º
  terminal MT5 **portable**, definir `MT5_2_TERMINAL_PATH` no `.env`,
  `LANE2_WORKER_ENABLED=1` (e `MT5_2_DRY_RUN=0` para execução real) e correr
  `start.ps1` - o `boot.ps1` já arranca o terminal portable no reboot.
- 🧪 **Marshaling do módulo MT5 POR CONVENÇÃO** (03/09): o worker recusa
  ANTES do C-core qualquer chamada remota com a convenção errada. A grelha
  `MT5_CALL_SHAPES` (`app/lane2_worker.py`) classifica cada função que o
  engine usa como **posicional** (`order_send(request)`, `symbol_info_tick`,
  `symbol_info`, `symbol_select`, sem-args) ou **kwargs-only**
  (`positions_get`/`orders_get`/`order_delete`/`history_deals_get` com
  `symbol=`/`ticket=`/`position=`). `normalize_mt5_call()` funde kwargs em
  args posicionais pelo nome (NUNCA passa um dict de kwargs a uma função
  posicional - a causa raiz 03/09 do `order_send` com `kwargs={}` → None →
  `'?'`) e rejeita formas impossíveis com erro claro, sem tocar no módulo.
  A regressão `test_marshal_replay_all_engine_call_shapes` repete todas as
  formas de chamada dos engines pelo caminho remoto completo.
- 🔎 **Auto-sonda do worker no boot/reconnect**: um `order_send` a um
  símbolo deliberadamente inválido (`__LANE2_PROBE__`, custo zero - broker
  recusa com retcode normal) corre UMA vez por episódio de ligação. Se
  devolver `None` + `last_error` real, o `/status` expõe
  `probe: {ok: false, error}` e o market_loop alerta `risk` (evento
  `lane2_self_probe_failed`, cooldown 10 min) - um módulo/terminal morto é
  apanhado no arranque, não em phantoms `'?'` silenciosos. O Diagnóstico
  expõe o bloco via `status_summary().probe`.
- 🧪 **Smoke test guiado dual-lane** (`POST /api/lanes/smoke`): abre e
  fecha a mercado uma trade REAL de 0.01 em **CADA conta demo** (principal +
  2ª; **XAUUSD** - o broker da lane 2 recusa EURUSD com 10030 "trade
disabled", apesar de ter tick; descoberto ao vivo 03/09) pelas engines
  reais de cada lane, com as DUAS trades a partilhar o **MESMO sinal** -
  prova o caminho completo (rows na BD com
  `execution_mode='mt5-smoke'`/`strategy_id='smoke_test'`/`exit_reason=
  'smoke_test'`, eventos `close`/`trade_closed`, WhatsApp single-lane com a
  mensagem só da tp2). Se uma lane falhar a abrir, a que
já abriu é fechada logo (rollback - nunca deixa posição órfã).
  **Guarda demo-only (sempre)**: opt-in `LANE2_SMOKE_ALLOW=1` no `.env`;
  as DUAS lanes em execução REAL (dry_run=False) com terminal/worker ligados
  e AutoTrading ON; as contas TEM de ser demo (`trade_mode == 0` - em conta
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
