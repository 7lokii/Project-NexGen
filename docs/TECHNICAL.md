# Project NexGen — Documentação Técnica

Sistema de **copy-trading semi-automático**: lê sinais (de grupos de Telegram ou
de uma fonte manual), aplica regras de risco e executa trades com a estratégia
de **N entradas (TP1..TP5 + proteção de breakeven)**. A execução liga ao
**MetaTrader 5** (conta demo/real) e o saldo/equity da dashboard vêm da conta
do terminal.

> ⚠️ **Aviso importante:** este esqueleto é para investigação e aprendizagem.
> Não existe "alpha" garantido em sinais de grupos; a única forma honesta de
> validar uma estratégia é backtest/simulação realista antes de ligar capital.

### Replay de uma trade real (read-only)

O journal opt-in (`execution_events`) guarda quotes, mensagens de gestão e
observações do MT5. A exportação `GET /api/operations/replay/tape` junta
automaticamente esses eventos à linha estruturada em `signals`, resolve
`reply_to_msg_id` e produz uma fita ordenada por UTC + `sequence`. Além de
`signal`/`quote`/`management`, a fita pode conter `order_request`,
`order_result`, `modify_request`, `modify_result`, `fill_open` e `fill_close`.
As observações incluem retcode, aceitação, tickets, preço, P&L e latências
quando o broker os fornece. Pode ser filtrada por `signal_id` ou `trade_id`.

`POST /api/operations/replay` corre a fita nos três adapters em BDs temporárias
(SimEngine, PaperRace e MT5 dry-run) e devolve `parity`, decisões, SL, razões,
P&L, `broker_observations` e `snapshots` pós-evento. Os eventos do broker são
passivos: servem para comparar a execução observada com a decisão simulada, mas
não alteram o estado do estudo nem provocam chamadas ao MT5. `GET
/api/operations/replay/snapshots` executa a mesma fita e aceita `step=N` para
inspeção individual. Estes endpoints nunca usam a BD de produção, inicializam
o pacote MetaTrader5 ou alteram trades.

`GET /api/operations/journal-quality` resume uma janela UTC (por defeito o dia
atual): contagem de sinais estruturados, gestão, quotes, fills de abertura e
fecho, retcodes aceites/recusados, estatísticas de latência e trades cuja fita
não tem todos os legs observados. `GET /api/analysis/fill-audit` usa as fitas
por trade e compara preço/razão de cada fill MT5 com o replay; devolve
`first_divergence`, para o operador ir diretamente ao primeiro desvio. A página
`/auditoria` mostra ambos.

O gate forward (`GET /api/analysis/forward-gate`) é deliberadamente separado do
backtest mensal. Avalia somente as contas shadow de `auto_escada_3` e
`auto_escada_5` contra uma **referência híbrida** `nova_gestao` — a corrida race
(o que a principal realmente executou) com precedência por sinal, completada
pela conta shadow da `nova_gestao` (isolada, abre em cada sinal completo mesmo
com o kill switch ligado), numa janela
UTC controlada de 30 dias (o início pode ficar congelado em
`config.forward_evidence_started_at`). Exige, por estratégia, pelo menos 20 trades fechadas,
PF≥1.5, limite inferior do IC95% da média de P&L acima de zero, drawdown≤125%
do saldo inicial, pelo menos 10 sinais comparáveis com a referência e não ficar mais de 10% atrás
da referência. O payload inclui cada sub-gate, mas `eligible` nunca chama
`POST /api/config` nem promove uma estratégia. O bundle
`GET /api/analysis/audit-export?format=json|csv` reúne qualidade, fill-audit e
gate numa janela UTC para envio/arquivo. `POST
/api/analysis/forward-gate/archive` guarda um snapshot da janela na tabela
`evidence_archives`; `GET /api/analysis/forward-gate/archives` lista os
snapshots e `/archives/{id}` devolve o JSON arquivado. O resumo diário acrescenta
as mesmas métricas ao feed/Telegram numa única notificação. Qualquer tentativa
de promover uma auto-escada antes de `eligible=true` devolve
`promotion_blocked=true` e mantém a estratégia atual.

---

## 1. Arquitetura

```
                    ┌──────────────────────────────────────────────┐
 Telegram / demo ──▶│  POST /api/signals (raw_text)                │
                    │      │                                       │
                    │      ▼                                       │
                    │  signals/parser.py  ──▶  Signal (estruturado)│
                    │      │                                       │
                    │      ▼                                       │
                    │  risk/engine.py       (lotes, limites,       │
                    │                       kill switch)           │
                    │      │                                       │
                    │      ▼                                       │
                    │  executa direto (único caminho auto —        │
                    │  Item 12; guards pedem confirmação explícita)│
                    │      │                                       │
                    │      ▼                                       │
                    │  execution/engine.py  (paper broker,         │
                    │                       TP1..TP5/BE)           │
                    │      │                                       │
                    │      ▼                                       │
                    │  notify/telegram.py   (notificações +        │
                    │                       resumo diário)         │
                    └──────────────────────────────────────────────┘
                              │
                    execution/feed.py (preços reais; fallback random walk)
```

Componentes:

| Módulo | Responsabilidade |
|---|---|
| `app/main.py` | Aplicação FastAPI: endpoints, WebSocket, wiring do motor e lifespan |
| `app/background.py` | `BackgroundRuntime`: lifecycle explícito/cancelamento do loop de mercado e watchdog |
| `app/execution/parity.py` | Normalização das observações dos três motores para regressões de paridade |
| `app/execution/clock.py` | Relógio UTC/monotónico injetável (`SystemClock`/`ManualClock`) |
| `app/execution/journal.py` | Eventos imutáveis e replay read-only ordenado para auditoria/reprodução |
| `app/execution/replay.py` | Fita determinística de sinais, mercado, gestão e evidência MT5; harness read-only SimEngine/PaperRace/MT5-dry |
| `app/execution/evidence.py` | Bootstrap determinístico, qualidade do journal, auditoria fill↔replay e gate shadow forward sem promoção |
| `app/state.py` | `BotState` — orquestrador: config, conta, pipeline, tick, resumo diário |
| `app/config.py` | Configuração central + constantes de trading |
| `app/models.py` | Modelos Pydantic (API) e dataclasses internas (Signal, Trade, Position) |
| `app/db.py` | Persistência SQLite (stdlib, thread-safe) — inclui tabela `group_summaries` (ground truth do grupo) |
| `app/signals/parser.py` | Parser tolerante de mensagens de sinais (PT/EN) + `validate_signal()` partilhado |
| `app/signals/llm_fallback.py` | Parser LLM **opcional** (fallback quando o regex falha; sanity checks + confiança mínima; nunca executa em auto) |
| `app/messages/manage.py` | Classificador determinístico das mensagens de gestão do grupo (TP HIT, SL HIT, close first, out at BE) |
| `app/messages/summaries.py` | Parser dos resumos diários/semanais do grupo (win rate reportado = ground truth) |
| `app/risk/engine.py` | Dimensionamento de lotes + verificações de risco |
| `app/execution/market.py` | Feed/fallback de preços simulado (geometric random walk) |
| `app/maintenance.py` | Logs rotativos, disk check, auditoria SQLite e restore-check read-only |
| `app/server_runner.py` | Entry point Uvicorn com rotação de stdout/stderr antes dos handlers |
| `app/execution/feed.py` | Feed real (Yahoo/TwelveData) + histórico de candles |
| `app/execution/engine.py` | SimEngine: paper broker + adaptador da política TP1..TP5/BE/auto |
| `app/execution/protection.py` | Política pura/reducer comum aos motores: SL monotónico, escada, trailing/ATR, confirmação, time stop, auto-protect e merge do `protect` |
| `app/backtest.py` | Backtest: replay de sinais reais em candles históricos + variante com gestão do grupo (`simulate_signal_managed`) |
| `app/notify/telegram.py` | Notificador: DB + WebSocket + (opcional) Telegram |
| `app/telegram_listener.py` | Listener Telethon: lê o grupo e reencaminha para a API (processo separado) |
| `app/static/style.css` + `common.js` | CSS partilhado + JS comum (helpers, boot, WS, chrome global, menu/terminais) — carregados por todas as páginas |
| `app/static/index.html` | Dashboard principal (`/`) — tabs sinais/posições/histórico/relatório/ignoradas + feed + corrida paralela |
| `app/static/teste.html` | Página de teste de estratégias (`/teste?s=<id>`) — simulação, comparativo, walk-forward, trades de papel |
| `app/static/diagnostico.html` | Página de diagnóstico (`/diagnostico`) — MT5, listener, BD |
| `app/static/help.html` | Página de ajuda (`/help`) — estratégias + glossário |

**Modelo de concorrência:** todo o estado mutável vive no event loop do FastAPI
(endpoints `async def` + tasks de background). Não há corridas entre o tick de 1s
e os pedidos HTTP. O acesso ao SQLite é serializado por um lock interno.

**Lifecycle dos loops:** `app/main.py` instancia `BackgroundRuntime` dentro do
`lifespan`. O runtime cria uma task de mercado (reconexão MT5 + `state.tick`) e
uma task de watchdog, é idempotente no `start()` e no shutdown faz
`cancel()` + `await gather(..., return_exceptions=True)`. Assim o `TestClient`
e os testes de MT5 não deixam tasks pendentes ligadas a um event loop fechado.
A comparação de adapters usa `app/execution/parity.py`, que não toca em BD,
relógio ou broker. O reducer puro `reduce_auto_protect()` concentra o gatilho
fração de entrada→TP1, o nível `group_protect_sl` e apenas os movimentos
monotónicos; backtest, PaperRace, SimEngine e MT5 aplicam essas decisões nos
seus próprios fills. Os quatro adapters aceitam um `Clock` opcional: a VPS usa
`SystemClock`, enquanto os estudos podem usar `ManualClock` sem dormir nem
consultar o relógio do sistema.

**Política de proteção comum:** `app/execution/protection.py` é side-effect-free.
`AutoProtectionPolicy` calcula a escada, o alvo de trailing e a fórmula
`max(trail_factor×dist, atr_k×ATR)`; `monotonic_sl`, `confirmation_legs`,
`time_stop_due` e `sl_exit_reason` fixam as mesmas fronteiras nos adaptadores.
Backtest, PaperRace, SimEngine e MT5Engine continuam responsáveis pelos seus
fills, relógios, throttling, broker e persistência. O endpoint `POST /api/operations/replay` recebe uma lista de eventos JSON e cria três BDs SQLite temporárias; a mesma fita é entregue a SimEngine, PaperRace e MT5Engine em dry-run. O relatório compara por sinal as decisões de cada perna, SL aplicado, razão de saída e P&L. O harness não importa a BD de produção, não inicializa o pacote MetaTrader5 e não pode promover estratégias.

O alerta de regime em
`state.py` também chama `sim_params(strategy_id)`, por isso o sim rolante usa
escada/trailing/modo defensivo/ATR da estratégia ativa em vez de avaliar uma
versão simplificada. O ATR do MT5 pede apenas velas completas
(`copy_rates_from_pos` começa na posição 1), para que a vela em formação nunca
entre numa decisão.

---

## 2. Estratégia (TP1..TP5 + breakeven)

O parser extrai **todos os TPs que o grupo enviar** (TP1 a TP5) e cada sinal
gera uma `Trade` com **uma perna por TP** (`Position`):

| Perna | Fecha em | Lotes |
|---|---|---|
| leg 1 | TP1 | `compute_lots(equity, risk/N, ...)` |
| leg 2 | TP2 | `compute_lots(equity, risk/N, ...)` |
| … | … | … |
| leg 5 | TP5 | `compute_lots(equity, risk/N, ...)` |

Gestão (em `execution/engine.py:update()`):

1. **SL atingido primeiro** → todas as pernas abertas fecham no SL (perda ≈ risco total).
2. **TP1 atingido** → perna 1 fecha no TP1 (lucro); o SL de **todas as pernas
   restantes** é movido para a entrada (**breakeven**).
3. Cada perna restante corre até ao **seu próprio TP** (vitória completa) **ou**
   é fechada em BE se o preço voltar à entrada (proteção automática).

P&L simulado: `± (close − entry) × lots × contrato` (contrato por símbolo:
XAUUSD 100, XAGUSD 5000, forex 100000 — igual ao live).

---

## 3. Motor de risco

### 3.1 Dimensionamento de lotes (`risk/engine.py:compute_lots`)

O slider de risco define a **perda máxima por trade** (% da equity). Os lotes
são calculados a partir da distância do SL:

```
risk_amount   = equity × risk% / 100
risk_pips     = |entry − SL| / pip_size          (pip_size = 0.0001)
lotes         = risk_amount / (risk_pips × pip_value_por_lote)
```

Consequência: SL apertado → lotes maiores; SL largo → lotes menores. O risco
monetário é constante. O risco total é dividido pelas N pernas (uma por TP;
cada uma arrisca `risco/N`).

### 3.2 Verificações antes de executar (`risk/engine.py:check`)

As mesmas no único caminho de execução (auto — o modo manual foi removido
no Item 12; uma confirmação humana **não** contorna o risco global):

| Verificação | Config | Efeito |
|---|---|---|
| Kill switch | `kill_switch` | Bloqueia novas entradas |
| Bloqueio automático | `auto_kill` | Ativado quando o limite diário é atingido |
| Máx. posições | `max_positions` | Bloqueia se o nº de trades abertas ≥ limite |
| Perda diária | `daily_loss_limit_pct` | Bloqueia se `realized_today ≤ −equity×%` |

### 3.3 Kill switch automático

No `tick()` (1s), se `realized_today` ultrapassar o limite diário, o bot:
1. Liga `kill_switch` e `auto_kill`;
2. Envia notificação de risco;
3. Só volta a negociar quando um humano reativa na dashboard (endpoint
   `POST /api/killswitch {enabled:false}`, que limpa `auto_kill`).

O kill switch **não** fecha posições abertas — elas continuam a ser geridas até
TP/SL/BE. Apenas bloqueia novas entradas.

---

### 3.4 Fita de replay determinístico

A fita usa timestamps UTC e `sequence` para preservar eventos no mesmo instante:

```json
{"at":"2026-08-19T10:00:00+00:00","kind":"signal","sequence":1,"signal_id":"s1","payload":{"symbol":"XAUUSD","direction":"buy","entry":4400,"stop_loss":4396,"take_profit_1":4402,"take_profit_2":4404}}
{"at":"2026-08-19T10:00:01+00:00","kind":"quote","sequence":2,"symbol":"XAUUSD","payload":{"price":4402}}
{"at":"2026-08-19T10:00:01+00:00","kind":"management","sequence":3,"signal_id":"s1","payload":{"kind":"protect"}}
```

`POST /api/operations/replay` executa apenas estudos read-only em BDs temporárias e força o adapter MT5 a dry-run. `GET /api/operations/events` continua a ser apenas a fonte de eventos recolhidos; nenhum endpoint de replay altera trades live.

## 4. Pipeline de sinais

`POST /api/signals` aceita:

- `raw_text` → passa pelo `parser.py` (mensagem real do Telegram);
- campos estruturados (`symbol`, `direction`, `entry`, ...) → usados diretamente;
- `message_id` → id da mensagem do Telegram (ver janela de edição abaixo).

O parser (`signals/parser.py`) interpreta PT/EN, vírgulas decimais e vários
layouts. Se faltar um campo obrigatório, o sinal fica `failed` com uma mensagem
legível — nunca rebenta a pipeline. Sinais com SL do lado errado (ex.: SL acima
da entrada numa compra) são rejeitados.

**Os 3 formatos históricos do grupo** (reconhecidos desde 2026-08, com a
re-análise offline do histórico):

| Formato | Exemplo | Notas |
|---|---|---|
| atual (multilinha) | `I'm buying XAUUSD 4416-4412` ⏎ `TP1 4420 … SL 4410` | zona isolada em linha própria |
| `XAU USD … NOW` | `XAU USD SELL NOW 4737-4741 🥇TP1 4734 🥈TP2 4732 🥉TP3 … 🚫SL 4745` | símbolo espaçado, emojis 🥇🥈🥉🏅 antes dos TPs, `🚫SL` |
| `Gold … Targets` | `Sell Gold Now 4123.5-4128.5 Targets 4121.5 4119.5 4117 Open SL/invalid 4131.5` | targets sem rótulo, `SL/ invalid` como stop, zona inline na linha |

Símbolos: `XAU USD` → `XAUUSD`, `Sell/Buy Gold` → `XAUUSD`. Sinais de formato
antigo são aceites apenas como histórico/backtest (`historical=True` nunca
executa) — um formato antigo a chegar **ao vivo** agora é suspeito.

**Zonas impossíveis rejeitadas**: o span da zona de entrada tem de ser
≤ ~1% do preço (relativo, funciona para ouro e forex) — `4379-4775` (396
pontos) e `4317-3413` (904) são typos e ficam `failed`; zonas largas mas
coerentes (ex.: `4078.50-4047`, 31.5 pontos) passam. Os sinais maus já
gravados foram re-marcados `failed` na BD (576 sinais válidos).

### 4.1 Janela de edição (mensagens do Telegram)

Grupos de sinais **editam mensagens** (corrigem entradas, invalidam sinais).
O trade-off é velocidade vs. correção: como o TP1 do grupo pode bater em
segundos, um sinal **completo no primeiro parse executa imediatamente** —
a janela de edição já não é usada (Item 12: os dados reais de `/api/edits`
mostraram que os atrasos medidos vêm das edições TP1/SL pós-execução, não
da entrada). Medição de 26/08 sobre a BD real (342 sinais live): dos 305
sinais que chegaram completos no primeiro parse, **0 foram editados depois
(0.0%)** — abaixo do limiar de 5% da spec, pelo que a regra se mantém
(completo → executa já; janela só para parses incompletos). Só quando a
primeira versão está **incompleta** (parse com erro) é que a mensagem com
`message_id` espera por edições (`edit_window_seconds`, default 20):

1. A mensagem chega incompleta → status `awaiting_edit`, `edit_deadline = agora + 20s`;
2. Se chegar uma edição (mesmo `message_id` com texto novo), o texto é
   re-analisado, os campos são atualizados e o relógio **reinicia** para +10s;
3. O loop de 1s (`state.finalize_awaiting_signals`) verifica os `awaiting_edit`:
   quando `edit_deadline` passa sem novas edições, o sinal continua o pipeline
   (verificações de risco + execução; um sinal parseado pela IA é registado
   para auditoria e nunca executa);
4. Se o conteúdo final continuar inválido, o sinal fica `failed` com o erro.

Edições a mensagens já finalizadas eram ignoradas (fora da janela) — exceto
**revive** (`state.py::_ingest_telegram` + `_revive_signal`): se o sinal
falhou (ex.: entrada incompleta sem TP/SL) e o grupo completar a edição
dentro de 5 min, o sinal volta a `awaiting_edit` com o conteúdo novo
(`action: revived`) em vez de ser ignorado; o mesmo aplica-se a um sinal
`rejected` (guard de zona/cap, lote mínimo, risco global) cuja mensagem o
grupo re-edita com conteúdo VÁLIDO dentro de `rejected_revive_minutes`
(default 10, env `SIGNALBOT_REJECTED_REVIVE_MINUTES`) — com alerta claro
(kind `revived`) de que a entrada ANTERIOR foi recusada e o motivo gravado
no `error` da rejeição. O revive guarda um **marcador durável** no sinal
(`revived_from`/`revived_reason`/`revived_at`, colunas novas em `signals`,
migração em `DB._migrate`) com o estado anterior e o motivo gravado; a
linha do sinal na tab Sinais mostra o badge `🔄 de rejected` (tooltip com
hora + motivo) mesmo depois de executar, e `_apply_edit` preserva o
marcador se o grupo editar outra vez dentro da janela. A janela de revive
conta desde `received_at` (chegada original). Re-edições tardias (>X min) não reabrem nada; o
operador recebe apenas um aviso `system` quando o conteúdo novo parseia.
Edições a sinais `executed`/`pending` continuam a só alertar (`risk`). O id
estável `tg-<message_id>` garante que a edição atualiza a mesma linha na BD.

Cada edição registada grava `edited_at` (coluna nova em `signals`, migração
automática em `DB._migrate`). **`GET /api/edits`** (`state.edit_delay_stats`
→ `db.edit_delay_stats`) calcula a distribuição do atraso entre a mensagem
original e a última edição (min/mean/mediana/p90/max em segundos + últimos
8 exemplos) — a tab Diagnóstico mostra a mediana face à janela atual
(`config.edit_window_seconds`) para validar os 20s com dados reais. O entry guard aceita automaticamente até `entry_guard_tolerance` unidades de preço fora da zona; desvios superiores continuam a pedir confirmação.
Notificações de revive usam o kind `revived` (ícone 🔄 no feed + toast).

**Métrica contínua de edições pós-execução (Item 12)**: as edições a sinais
que executaram **sem** janela (primeiro parse completo) passam a gravar
`edited_at` no ramo `executed`/`pending` de `_ingest_telegram` — antes só
avisavam. `db.post_execution_edit_metric` devolve
`post_execution_edited`/`post_execution_total`/`post_execution_edit_rate`
(fração de executados sem janela que receberam edição depois) e o endpoint
`/api/edits` inclui-os. O tick verifica a cada ~30 min
(`state._check_post_execution_edits`) e, com amostra ≥20 e taxa >5%, emite
alerta `risk` (feed + Telegram, cooldown 24h): a regra "completo → executa
já" foi decidida com taxa real de 0.0% e deve ser revista se o grupo mudar
de comportamento. O snapshot expõe `edit_metric` e a tab Diagnóstico mostra
o valor face ao limiar de 5%.

### 4.2 Mensagens de gestão do grupo (máquina de estados)

Os grupos não enviam só sinais — enviam **instruções de gestão em tempo real**
("TP1 HIT", "close first entries and move stop loss above best entries",
"we're out at BE"). Estas mensagens **não esperam a janela de edição**: o
classificador (`app/messages/manage.py`) age imediatamente na trade aberta do
mesmo símbolo (ou na mais recente):

| Mensagem do grupo | Ação do bot |
|---|---|
| `TP1 HIT` | SL das pernas restantes → breakeven (entrada) |
| `TP2 HIT` … `TP5 HIT` | Confirmado/registado (a perna fecha no seu TP) |
| `SL HIT` | Confirmado — a reconciliar com o broker |
| `Close TP1 here` (sem move SL) | Fecha **só a perna 1** ao mercado (restantes mantêm o SL) |
| `close first entries + move SL above best` | Fecha a **perna 1** ao mercado e move o SL das restantes para **acima da entrada** (bloqueia lucro, "risk free") |
| `we're out` / `close this setup` | Fecha **todas** as pernas ao mercado; `out at BE` move/protege as restantes na entrada sem forçar o fecho |
| `Adjust SL to X` / `Move SL to X` | **`move_sl`** — move o SL de **todas** as pernas abertas para o nível indicado (com os mesmos checks de segurança do `protect`); sem nível numérico só regista (`sl=None`). `sl` fica em `pending_management` quando a trade ainda não abriu |
| `TP1 4059 SL 4047` (com `reply_to` de um sinal) | **`tp_sl_update`** — edição/confirmação que corrige os níveis do sinal alvo na BD (`update_signal_levels`) em vez de ficar `failed` |
| `Correction: Weekly summary …` | **`summary_correction`** — liga ao summary respondido (ou ao período explícito inequívoco), regrava os números do alvo e reexecuta a divergência |
| `Signal #1: Sell at 4357 … +110 pips` | **`recount`** — recount por trade (R2): guarda em `trade_recounts` e audita contra o fill real do mesmo dia (entrada/pips divergentes → alerta `risk`) |
| Resumo diário/semanal | Guardado como **ground truth** (tabela `group_summaries`) |

A mensagem é sempre registada no histórico como sinal com status `management`
(e ícone 📨 na dashboard) e notificada no feed. A classificação é conservadora:
as ~10 mensagens não-gestão observadas (ordens pendentes "Buy/Sell Stop",
comentários, sinais com typo) nunca disparam ações.

**Conversa do grupo (auditoria).** O listener reencaminha **tudo** para o
backend (`SIGNALBOT_TG_FILTER=0` no `.env`); a decisão vive em `state.py`:
`classify()` → gestão; senão o regex `_SIGNAL_HINT` (mesmas keywords do
antigo filtro do listener) → se não casar, a mensagem é conversa pura — vai
para a tabela `ignored_messages` (dedup por message_id+source, últimas 1000)
e devolve `action: ignored`. Nunca entra na pipeline nem na janela de
edição. A tab "🚫 Ignoradas" da dashboard mostra esta auditoria
(`GET /api/ignored`); o snapshot leva `ignored_count` para o badge.

**Correlação por reply do Telegram.** O listener captura o `reply_to_msg_id`
(Telethon: `message.reply_to` → id da mensagem respondida) e envia-o no POST.
O handler usa essa relação para escolher o alvo, por ordem: (1) a trade do
sinal a que a mensagem **responde** (correlação exata — duas trades XAUUSD
abertas não confundem o alvo); (2) trade aberta do mesmo símbolo; (3) trade
aberta mais recente. Se o sinal alvo ainda estiver na janela de edição (sem
trade aberta), a instrução é guardada em `pending_management` e aplicada
automaticamente quando a trade desse sinal abrir (`_apply_pending_management`
em `_try_execute`). Colunas novas: `signals.reply_to_msg_id` e a tabela
`pending_management`.

No replay histórico (`app.backtest.correlate_management`) a mesma regra é
aplicada antes da simulação: `reply_to_msg_id → message_id` do sinal é a chave
forte; sem reply usa-se o último sinal anterior da mesma fonte; um reply
explícito que não resolve é descartado, nunca redirecionado para outra XAUUSD.
Os eventos `close_tp` (perna indicada) e `move_sl` (nível explícito) entram na
cronologia com `message_id` e `sl`, enquanto um `move_sl` sem nível fica apenas
como auditoria.

Fluxo (após a janela fechar):

- **Único caminho (auto, Item 12)**: passa pelas verificações de risco e,
  se passar, executa; se bloqueado, fica `rejected` com os motivos.
  Exceção: sinais bloqueados por guard de entrada/lote mínimo ficam
  `pending` com botões Confirmar/Descartar na dashboard (`/confirm` e
  `/reject` — nunca contornam kill switch/perda diária).
- Um sinal parseado pela IA (fallback LLM) é registado para auditoria
  (`failed` + alerta no feed) e nunca executa nem fica pendente.

Mensagens **sem** `message_id` (botão de demonstração, input estruturado)
continuam imediatamente, sem janela.

---

## 5. API Reference

| Método | Rota | Descrição |
|---|---|---|
| GET | `/api/status` | Snapshot completo (conta, config, sinais, trades, posições, notificações, `auth.enabled`, `online_users`, `execution.hedged/blocked_reason` e `operations.disk`) — exige sessão/token |
| POST | `/api/login` | `{password}` → cookie httpOnly de sessão (7 dias). 401 se errada. Desligado sem `SIGNALBOT_DASHBOARD_PASSWORD` |
| POST | `/api/logout` | Termina a sessão (limpa o cookie) |
| POST | `/api/config` | Atualização parcial: `mode`, `risk_pct`, `daily_loss_limit_pct`, `max_positions`, `sim_speed`, `strategy`, `shadow_paper_enabled`, `shadow_paper_strategies` e `execution_journal_enabled` |
| POST | `/api/killswitch` | `{enabled: true\|false}` — pausa/reativa |
| POST | `/api/signals` | Ingestão de sinal (`raw_text`, estruturado ou `message_id`) |
| POST | `/api/signals/{id}/confirm` | Confirmação explícita de sinal bloqueado por guard de entrada/lote mínimo (Item 12: única aprovação humana restante) |
| POST | `/api/signals/{id}/reject` | Descartar sinal bloqueado por guard de entrada/lote mínimo (sem veto geral) |
| POST | `/api/signals/{id}/delete` | Mensagem apagada: marca o sinal e fecha a posição ao mercado |
| GET | `/api/report?days=N` | Relatório de qualidade (win rate, R:R, por grupo, **curva de equity**, **drawdown máx**, **`by_day`** com P&L/win rate por dia para o gráfico de barras) |
| GET | `/api/operations?days=N` | Item 10: exceções persistidas (stack/contexto), spread médio mensal por símbolo, reconexões MT5 semanais e modifies de SL aceites/recusados por retcode |
| GET | `/api/operations/events?since=&until=&kind=&limit=N` | Journal read-only de quotes/gestão quando `execution_journal_enabled=true`; não executa ações |
| GET | `/api/operations/replay/tape?signal_id=&trade_id=&since=&until=` | Exporta a fita completa juntando `execution_events` + `signals`, sem executar |
| GET | `/api/operations/replay/snapshots?signal_id=&trade_id=&step=N` | Reproduz a fita em modo read-only e devolve snapshots pós-evento; `step=-1` devolve todos |
| POST | `/api/simulate` | Backtest dos sinais reais com preços históricos (`risk_pct`, `days`, `delay_seconds`, `spread_pips`, `slippage_pips`, `commission_per_lot`) |
| POST | `/api/summary` | Força o envio do resumo diário |
| GET | `/api/trades` | Histórico de trades |
| GET | `/api/notifications` | Feed de notificações |
| GET | `/api/ignored?limit=N&q=X&days=N` | Auditoria: mensagens do grupo que o bot ignorou (conversa) — hora, origem, texto completo; filtros opcionais `q` (palavra-chave) e `days` (período) |
| POST | `/api/heartbeat` | Heartbeat do listener do Telegram (a cada 1s): `{groups, last_msg_at, tick}` — alimenta a tab Diagnóstico |
| POST | `/api/listener/need_code` | O listener avisa que o Telegram pede código de login novo — a dashboard mostra o input |
| GET/POST | `/api/listener/code` | GET: o listener busca o código (one-time). POST: a dashboard entrega o código do utilizador |
| GET | `/api/diagnostics` | Estado do sistema: MT5 (inclui `margin_mode`, `hedged` e `blocked_reason`), listener (alive/tick/última msg), servidor (uptime/PID), market (sources + `changes` — variação % do dia por símbolo), auth, BD (contagens), disco (`operations.disk`), última mensagem do grupo, `uptime_history` (amostras 10s, 24h, downsample ~300), `listener_restarts` (últimos 10 reinícios com hora+motivo), `replay` (data + totais do último replay do histórico) e `reconciliation` read-only DB↔broker |
| GET | `/api/analysis/protect_latency?strategy=<id>&engine=<paper\|mt5\|backtest>` | Telemetria `protect_latency_log` da híbrida (ou replay backtest): vencedor, SL, atraso em segundos/pips e distribuição |
| GET | `/api/analysis/protect_causal?strategy=<id>&days=0` | Replay causal read-only: compara híbrida com a auto sem `protect`, delta incremental por evento e piores intervenções |
| GET | `/api/analysis/audit-export?format=json\|csv&days=N` | Bundle exportável da qualidade do journal, auditoria fill↔replay e gate forward |
| POST | `/api/analysis/forward-gate/archive` | Arquiva um snapshot read-only da janela UTC forward |
| GET | `/api/analysis/forward-gate/archives` | Lista janelas forward arquivadas |
| GET | `/api/analysis/forward-gate/archives/{id}` | Obtém um snapshot arquivado |
| GET | `/api/analysis/resolution?interval=1m&days=7&strategy=<id>` | Compara a resolução pedida (M1 nativo até 7 dias; fallback explícito depois disso) com a referência 15m e mede atrasos 0/8/60/300s, sem writes |


O snapshot `/api/status` expõe `operations.execution_journal` com o total e a distribuição dos eventos dos últimos 7 dias. O journal é opt-in e deve ser usado para recolher uma fita de quotes/gestão antes de um replay, não como substituto do broker.

O snapshot `/api/status` expõe `market.changes` (símbolo → % do dia, do
`chartPreviousClose` do Yahoo — `None`/ausente quando não há dado; usado no
terminal de preços (`termSide`, em baixo do menu esquerdo) da dashboard,
verde/vermelho).

**Layout por zonas**: o chrome global vive numa **barra superior fixa**
(`.topbar`, renderizada pelo `common.js` num placeholder `<div id="appChrome">`
— fonte única): linha 1 com a marca, a navegação horizontal
(PRINCIPAL · ▚ ESTRATÉGIAS dropdown · DIAGNÓSTICO · AUDITORIA · HELP,
navegação por setas ↑↓ + Enter), os badges de estado (modo, estratégia,
MT5 real, execução), utilizadores online, relógio, kill switch e Sair — a
faixa de preços (`priceStrip`) foi removida em 09/2026 (decisão do
utilizador — o topbar fica estático; badge "preços reais"/"simulação" cobre
o estado do feed). O conteúdo vive entre a barra superior e o fim da página
com largura total (máx. 1440px centrado) — as antigas colunas laterais
(`--term-w`/`--term-r`, `termSide`/`termRight` e os overlays móveis ☰/▦)
foram removidas. Em ecrãs ≤960px a navegação colapsa num dropdown aberto
pelo ☰. A ordem da principal é: status → KPIs → tabs → conteúdo, com os
painéis analíticos (corrida/promoção/Fase 2/3/narrativa/shadow) depois do
conteúdo (CSS `order`). Nas páginas de teste, Diagnóstico, Auditoria e
HELP o body ganha a classe `no-chrome` que esconde stats/controlos/bloco
de risco — só ficam a barra superior e a statusbar. O widget "Se lote
fixo" foi removido (nenhuma estratégia do registo usa lote fixo).

**Render por diffs (frente)**: o snapshot chega ~1×/s via WebSocket; as
páginas só reconstroem o DOM quando o conteúdo mudou (`contentChanged` em
`common.js`, cache de assinatura) e a principal só renderiza a tab ativa
(`window._activeTab`) — as tabs ocultas mantêm o DOM e re-renderizam ao
voltar. O feed reconstrói apenas em mudança (o `prependFeed` do WS continua
incremental) e o snapshot envia 30 notificações (antes 50).

**Edição pós-execução** (`state.py::_ingest_telegram`): quando o listener
re-encaminha uma edição de uma mensagem cujo sinal já está `executed`/`pending`
(a janela passou), o bot emite uma notificação `risk` — a entrada pode
estar desatualizada. `historical=True` nunca dispara (replays silenciosos).

**Isolamento dos testes**: `tests/conftest.py` define `SIGNALBOT_DB`
(temp), `MARKET_PROVIDER=none`, `EXECUTION_MODE=sim`, etc. ANTES de
qualquer import de `app.*` — correr ficheiros fora de ordem nunca escreve
na BD de produção.

**Purge da auditoria**: a tabela `listener_restarts` é limpa
automaticamente de registos com mais de 30 dias (`DB.purge_old_listener_restarts(30)`,
compara `substr(started_at, 1, 10)` imune a fusos) — no arranque do servidor
e a cada ~6h no loop do watchdog (`main.py::listener_watchdog`). Limpeza
**manual total** (com confirmação na dashboard): `DELETE /api/listener_restarts`
→ `DB.clear_listener_restarts()`.
| GET | `/api/summaries?group=X` | Resumos do grupo guardados (ground truth: WR reportado) |
| GET | `/api/recounts?source=X` | Recounts por trade do grupo (R2, read-only) — comparação com fills reais do dia em `state._recount_fill_audit` |
| WS | `/ws` | Stream em tempo real: `snapshot` e `notification` — fecha com 4401 sem sessão válida (auth ligada) |

**Autenticação** (`app/auth.py`): com `SIGNALBOT_DASHBOARD_PASSWORD` definida, a
dashboard exige login (`/` serve `login.html`; todos os `/api/*` devolvem 401
sem cookie de sessão). O **listener** autentica com o header `X-API-Token`
(`SIGNALBOT_API_TOKEN`). Sem password no ambiente, tudo fica aberto (padrão
local/testes). Sessões em memória (single-process); password e token lidos
dinamicamente do ambiente a cada pedido.

## Estratégias, navegação terminal e paper trading forward

- **`app/strategies.py`** — registo central (`STRATEGIES` + `MAIN_STRATEGY_ID`):
  cada estratégia define `max_legs`, `manage`, `exit_at_tp` e, nas autos,
  os parâmetros mecânicos de proteção. Estratégias (após a **remoção
  completa de 01/09**): `nova` (TP1..TP5), `nova_gestao` (principal),
  `nova_gestao_fallback`, `tp2_protect` (só protect + fechar no TP2),
  `auto_3`, `auto_5`, `auto_escada_3`, `auto_escada_3_hibrida`,
  `auto_escada_5`. Foram apagadas do registo a 01/09: `tp2`, `tp2_gestao`,
  `nova_gestao_3`, `auto_2`, `auto_protect_5` e `auto_escada_3_struggle`
  (histórico/PF/DD no CHANGELOG — o backtest delas deixou de ser
  reproduzível). `sim_params(id)` devolve os parâmetros para `run_analysis`;
  `get_strategy` devolve a principal para `None`/vazio e **levanta ValueError
  para ids desconhecidos** (sem fallback silencioso nos estudos; a config
  com id morto fica órfã no snapshot — `_strategy_params`).
  `GET /api/strategies` expõe o registo (já sem as removidas).
  `MENU_HIDDEN_STRATEGIES =
  {nova, auto_5}` tira só do menu/corrida visual mantendo o papel (a `nova`
  é o contrafactual "sem gestão" do histórico de trades — `_trade_view` — e
  nunca pode ser totalmente oculta; a `auto_5` é a comparação 5 pernas
  com/sem trail). Ficam **7 visíveis no menu/dropdown**: `nova_gestao`,
  `nova_gestao_fallback`, `tp2_protect`, `auto_3`, `auto_escada_3`,
  `auto_escada_3_hibrida` e `auto_escada_5` (5 operacionais + 2 referências;
  `nova`/`auto_5` abrem papel fora da curva). A `auto_escada_3_hibrida` combina a auto-gestão com o único kind
  `protect` do grupo, através do merge monotónico e da telemetria
  `protect_latency_log`; é experimental e não é elegível para promoção
  automática. A mecânica de proteção precoce (`protect_trigger`/
  `protect_buffer`) e a regra R4 (`struggle_*`) continuam nos motores por
  parâmetro, sem estratégia registada que as use.
- **Config `strategy`** — id da estratégia ativa (default `nova_gestao`).
  `state._try_execute` passa `max_legs` ao `open_trade`; `state.tick` passa
  `exit_at_tp` ao `update`. `SimEngine` e `MT5Engine` aceitam ambos:
  - `open_trade(..., max_legs=N)` limita as pernas (`_legs(obj, max_legs)`);
  - `update(..., exit_at_tp=N)` — quando o TP_N bate, todas as pernas ainda
    abertas fecham nesse preço (no MT5 real, quando a perna N fecha pelo
    broker com lucro, as restantes fecham ao mercado via `close_leg_at_market`).
- **Navegação**: o menu vive no `common.js`/`style.css` — um `<nav class="term-menu">`
  com links `data-page` (principal / teste-<id> / diagnostico / help). As 7
  estratégias visíveis vivem no **dropdown ▚ ESTRATÉGIAS** (`.term-drop-toggle` +
  `.term-drop` — toggle em `common.js`, estado persistido em localStorage,
  default aberto; a navegação por teclado abre-o sozinha ao chegar a um item
  lá dentro; fecha ao clicar fora).  `TERM_ORDER` em `common.js` espelha a
  ordem do DOM (principal, 7 estratégias, diagnostico, help). As
  páginas são **ficheiros HTML separados**: `index.html` (`/`), `teste.html`
  (`/teste?s=<id>`), `diagnostico.html` (`/diagnostico`) e `help.html`
  (`/help`) — todos servidos pelo `main.py` com auth via middleware (sem
  sessão → página de login). Cada página define `pageRender(s)` para o seu
  conteúdo; o chrome global (badges, statusbar, terminais, toasts) vem do
  `common.js`. As páginas de teste chamam
  `GET /api/analysis/month?strategy=<id>` (ver secção 4.1) e desenham a
  mini-curva no `testeChart` + o comparativo das 3 no `testeCompareChart`.
  A principal **não** mostra simulação do mês (começa a partir de hoje);
  a tab Diagnóstico saiu da barra de tabs da principal (tem página própria
  no menu). Cada página de teste tem o botão **Promover a principal**
  (`POST /api/config {strategy}`); a principal mostra o badge da estratégia
  ativa (`#strategyBadge`, clarão quando muda) e o aviso `#paperRaceWarn`
  quando uma estratégia de teste ultrapassa o saldo da principal na corrida
  paper (`renderStrategyBadge` / `renderPaperRaceWarn`).
- **Promover uma estratégia** (`state.update_config`): quando o `strategy`
  muda, o servidor regista uma **notificação no feed** com os parâmetros
  reais de execução (texto via `_strategy_params_text` — max_legs /
  exit_at_tp / lots_override / gestão). O snapshot expõe
  `config.strategy_params` (`state._strategy_params`) e o badge do header
  mostra-os (`renderStrategyBadge` usa `stratParamsText`).
- **Config órfã**: se a `strategy` da config apontar para um id fora do
  registo (ex.: estratégia removida), `strategy_params.orphan` fica `True`
  (+ `configured_id`), a execução cai no fallback (`get_strategy` →
  principal) e o badge avisa a vermelho — nunca corre a estratégia errada
  em silêncio.
- **Comparativo das páginas de teste com 3 curvas**: `loadTesteCompare`
  desenha nova, **nova+gestão (a principal)** e fechar no TP2 nos mesmos
  sinais (antes eram só as 2 de teste) — o P&L da gestão fica visível em
  cada página de teste.
- **Último replay no Diagnóstico**: `state.history_done` guarda na config
  `replay_done_at`/`replay_group`/`replay_total`/`replay_sinais`/`replay_gestao`/
  `replay_resumos`/`replay_ignoradas`; `/api/diagnostics` expõe o bloco
  `replay` e a tab mostra a data + totais.
- **Páginas de teste com 2 horizontes**: além do seletor 7/30/90 dias, cada
  página desenha o bloco "📜 Desde o início do grupo" que chama
  `GET /api/analysis/month?days=0` (todo o histórico na BD — ver 4.1) e a
  **tabela de trades de papel** via `GET /api/paper/trades?strategy=<id>`
  (abertas + fechadas com P&L; `db.paper_trades`).
- **Paper trading forward** (`app/execution/paper.py`, `PaperRace`):
  - tabelas `paper_accounts` (strategy_id PK, balance, start_balance),
    `paper_trades`, `paper_positions` — separadas da BD principal;
  - `open_for_signal(signal, risk_pct, strategy_ids=None,
    daily_stop_pct=None)` abre a trade simulada para todas as estratégias
    visíveis (INCLUINDO a principal — cada uma com a sua conta virtual de
    1000 €), com os seus `max_legs` e o saldo da conta virtual;
    `paper_status` marca `is_main` seguindo a config ativa mas a principal
    também tem `balance` próprio; **kill diário**: com `daily_stop_pct`
    (>0), quando `paper_realized_today(strategy, hoje)` (SUM pnl das
    `paper_trades` fechadas no dia civil UTC) ≤ −saldo×pct/100 o sinal não
    abre em papel nessa estratégia — `_skipped_daily_stop` conta por
    estratégia (exposto no snapshot como `daily_stop_skipped`); o `state._try_execute`
    passa o `daily_loss_limit_pct` da config;
  - `  tick(risk_pct)` atualiza posições abertas contra o market feed
    (SL/TP/BE + `exit_at_tp` + **auto-gestão**: escada, trailing,
    confirmação — `auto_escada_3` agora com `confirm_filter=True` após o
    Item 3 — e time stop — espelho da sequência do backtest, com o

    trailing **sem look-ahead** (o SL usa o pico dos ticks ANTERIORES; o
    pico só é atualizado no fim do tick, depois dos fills). O estado do
    trailing (pico/vale desde o TP1), escada e time stop é persistido em
    `protection_states` por engine/origin/trade e reconstruído após restart;
    só o throttle monotónico do MT5 começa de novo. As razões de saída da
    auto-gestão:
    `trail`/`stair`/`confirm`/`time_stop`; a razão da trade é a da ÚLTIMA
    perna a fechar (ex.: `close_all` depois do TP1, não o tp1 da perna 1);
  - **auto-protect precoce** (`auto_protect_5`): sem Telegram, o preço que
    percorre `protect_trigger=0.75` de entrada→TP1 fecha a perna 1 no nível
    do gatilho e move as restantes para `group_protect_sl` com
    `protect_buffer=0.10`; o mesmo helper é usado no backtest, PaperRace,
    SimEngine e MT5. É uma candidata experimental e não está promovida.
  - **modo defensivo** (estudo 19/08): quando o PF das últimas `def_window`
    trades resolvidas < 1.0, a estratégia (auto_escada_3/5) passa a
    `def_trail` (1.5) + `def_time_stop` (60) e limita novas entradas a
    `def_risk_pct` (1.25%) SEM cortar pernas, até o PF recuperar — sem
    look-ahead (a decisão usa só o histórico anterior ao tick). No paper a história é a da própria conta virtual
    (`db.paper_closed_recent`, origem race/shadow); no MT5 real (Fase 3) a das trades reais
    fechadas (`db.recent_closed_pnls`); no backtest o `def_state` partilhado
    entre sinais (ordem cronológica) dentro de `run_analysis`. O snapshot
    expõe `strategy_params.defensive` quando a principal está em modo
    defensivo (badge "🛡 modo defensivo");
  - **gestão do grupo em papel**: `apply_management(signal_id, kind, tp, sl,
    be, msg_id, origin)` atua nas trades de papel abertas do sinal alvo (a
    mesma correlação reply→símbolo→mais recente da conta real, via
    `signal_id`), mas só para estratégias cujo `manage_kinds` inclui o kind;
    `origin="race"` e `origin="shadow"` entregam a mesma gestão a cada stream
    sem uma entrega consumir a outra — chamado pelo `_handle_management` e
    pelos caminhos pendentes do `state.py`;
  - **atribuição de saída**: `exit_reason` (sl/tpN/protect/close_all/
    close_tp1/move_sl/killswitch/manual) + `exit_msg_id` em
    `positions`/`trades`/`paper_positions`/`paper_trades` (colunas novas,
    migração automática por ALTER TABLE no arranque — ver `db._migrate`);
    o snapshot de trades inclui `counterfactual_pnl` (P&L da variante `nova`
    da corrida paper para o mesmo sinal — o "sem gestão" ao vivo);
  - **shadow-paper (forward sem ordens demo)**: quando `shadow_paper_enabled`
    está ativo (default), `_open_shadow_for_signal` recebe sinais live completos
    antes das verificações de risco/kill da conta principal e chama
    `PaperRace.open_for_signal(..., origin="shadow")`; nunca chama
    `SimEngine.open_trade`/`MT5Engine.open_trade`. A deduplicação é por
    `signal_id+strategy_id+origin`: um retry não duplica a mesma origem, mas o
    mesmo sinal pode ter uma trade na corrida normal e outra no shadow. Cada
    origem tem também saldo, kill diário, PF e histórico de conta separados;
    `historical=True` e `llm_parsed=True` ficam fora do forward shadow. O
    `origin` persistido em `paper_trades` e o painel `shadow_paper` tornam a
    separação auditável; `paper_open_trades(..., origin="shadow")` não mistura
    a contagem com a corrida normal. Mensagens de
    gestão, incluindo atualizações `TP1 ... SL ...`, seguem o alvo por reply e
    mantêm `pending_management.applied` (execução real), `paper_applied`
    (corrida normal) e `shadow_applied` (stream isolado) separados; uma
    entrega shadow não consome a pendência que a corrida normal ainda precisa.
    Uma instrução `out at BE` mantém a semântica de proteger a entrada quando
    foi enfileirada antes da abertura.
  - **real vs backtest** (`GET /api/analysis/real_vs_backtest` →
    `analysis.real_vs_backtest`): para cada trade REAL fechada (`trades` +
    `positions` — a conta demo), o fill do sim do mesmo sinal é a entrada ±
    spread/2 ± slippage (fórmula do `simulate_signal`); compara Δ entrada
    (preço real vs fill, em pips), Δ saída (fecho ponderado das pernas vs o
    nível esperado pela razão do fecho: TP_k → `take_profit`, sl →
    `stop_loss`), atraso sinal→abertura (vs o `delay_seconds`=60 do sim) e
    P&L real vs `run_analysis` da estratégia principal no mesmo sinal
    (degrada para comparação de preços se não houver candles). Tabela na
    tab Relatório;
  - **auto paper vs auto MT5 (Fase 3)** (`GET /api/analysis/paper_vs_real_auto`
    → `analysis.paper_vs_real_auto`): para cada trade REAL fechada de uma
    estratégia `auto_*` (quando promovida a principal no MT5) com trade de
    PAPEL da mesma estratégia no mesmo sinal (a corrida paper continua em
    paralelo): mesma decisão de saída (`exit_reason`), Δ preço de fecho
    (média ponderada das pernas real vs papel, em pips), Δ timing (instante
    de fecho papel vs real, em min) e P&L de cada lado. É o divergence
    report paper↔MT5 do protocolo Fase 3 (docs/ESTRATEGIAS.md Parte X);
    vazio até a Fase 3 arrancar. Tabela
    "🔄 Auto paper vs Auto MT5 (Fase 3)" na tab Relatório;
  - **divergence report** (`PaperRace._divergences`): quando uma trade da
    corrida fecha, compara a auto (em validação, `auto_*`) com a referência
    `nova_gestao` do MESMO sinal — se saíram diferentes emite um evento
    `divergence` (feed tipo `divergence` ⚖️ + Telegram se configurado),
    incluindo o caso "a referência fechou por gestão do grupo e a auto ainda
    está aberta". Dedup one-shot por `signal:auto` (em memória `_diverged`);
    o    `state.tick` converte os eventos em notificações; a tab Relatório tem a
    tabela persistente (`GET /api/paper/divergences` → `state.paper_divergences`:
    pares auto vs `nova_gestao` por sinal com razão/P&L lado a lado + motivo,
    incluindo "a referência saiu e a auto continua");
- **Curva da corrida paralela** ("quem está a ganhar"): o servidor grava
  snapshots de saldo a cada ~60s no tick (`_record_balance_snapshots` em
  `state.py`) em `paper_balance_history` (UNIQUE strategy_id+ts) — todas as
  estratégias (incluindo a principal) com a sua conta virtual de 1000 €, para
  comparar em igualdade de circunstâncias (não o saldo real da MT5). Além
  disso, o tick grava também o **saldo real da MT5** sob a estratégia
  reservada `__real__` (sem conta paper) — `GET /api/paper/history?hours=`
  (até **720h** = 30 dias, seletor 24h/7d/30d na dashboard) devolve as séries
  por estratégia (`is_main` + `is_real` incluídos, e `closed` + `win_rate` de
  cada estratégia via `paper_trade_stats`); a
  dashboard desenha a evolução em %
  desde o primeiro ponto (mini-curva na principal),
  com **tooltip de P&L absoluto em €** ao passar o rato (`+418 € (+3.7%)`)
  e o saldo atual na legenda; a linha da **principal (conta real)**
  desenha-se a **tracejado e mais grossa** (`is_main` → dash `[8,4]`,
  largura 3) para se distinguir das linhas de teste, e a **referência real
  `__real__`** desenha-se por cima a tracejado claro (`is_real`) fora da
  comparação. O menu marca a
  vencedora do `compare?days=0` com "✓ melhor" (`markBestStrategy()` no
  frontend, `compare.winner` no backend).
- **Alerta de ultrapassagem na corrida**: `state.tick` chama
  `_check_paper_race_leader` — quando uma estratégia de teste passa a ter
  saldo **maior** que o da principal (com cooldown e só se já houver
  snapshots), emite notificação `race` no feed — ícone 🏁 a **roxo**
  (`var(--violet)`, grupo "🏁 Corrida") — e Telegram se configurado,
  além do aviso vermelho que a dashboard já mostra no header.
- **Sugestão de auto-switch por regime** (mesmo método): se uma estratégia
  **visível** (fora de `HIDDEN_STRATEGIES`) se mantém à frente da principal
  por >= 30 min seguidos com >= 3 trades fechadas, emite uma segunda
  notificação `race` de **"sugestão de troca"** (`_race_switch_suggested`
  evita repetir) com `action="promote:<id>"` — a dashboard mostra um botão
  **"⬆️ Promover"** no feed (clique → `POST /api/config {strategy}` e volta
  à principal). Não troca sozinho.
  - `Notifier.send(kind, text, tg_text="", parse_mode="Markdown")`: o feed
    guarda `text` limpo (sem markdown) e o push Telegram usa `tg_text`
    (negrito/linhas) quando dado — o alerta da corrida envia versão
    formatada em Markdown.
  - chamado em `state._try_execute` (após execução) e em `state.tick`;
  - snapshot expõe `paper.accounts` (por estratégia: balance, wins, losses,
    win_rate, pnl, profit_factor).

**MFE win rate (R3)**: o `simulate_signal` mede `mfe_pips` (excursão máxima
favorável — observação pura, nunca decisão de fill) e o `aggregate()` expõe
`mfe_win_rate`/`mfe_threshold_pips`: wins na convenção do grupo (MFE ≥ 25
pips por defeito), mesmo que a trade tenha voltado e fechado em loss/BE.
A divergência grupo↔bot (`_check_group_divergence`) compara o WR reportado
com o `mfe_win_rate` quando o backtest o mede (fallback para o win_rate
pnl em dados antigos) — uma trade que andou +40 pips e voltou ao SL é win
para o grupo, não para o bot, e deixou de gerar alertas espúrios.

**Recount por trade (R2)**: `app/messages/recounts.py::parse_recount`
extrai `Signal #N → direção → entrada → excursão → reversão → pips`; o
classificador devolve `kind="recount"` antes do filtro de conversa. O estado
persiste em `trade_recounts` (dedup por `message_id`) e `_recount_fill_audit`
compara com as trades reais fechadas do mesmo dia (direção + entrada mais
próxima, tolerância 40 pips): entrada ≥20 pips, pips ≥30 ou **direção
divergente** disparam alerta `risk` no feed/Telegram. O audit expõe também
`direction_mismatch` e o **candle-check** (`_recount_candle_check`): a
excursão/reversão reportadas pelo grupo são comparadas com o high/low dos
candles Yahoo 15m do dia; sem símbolo associável (recount não traz símbolo —
resolve pelo sinal do dia com entrada próxima) ou sem candles devolve
`checked=False` (nunca inventa plausibilidade). `GET /api/recounts`
(read-only) devolve cada recount com o seu fill-audit
(`matched`/`alert`/`candle_check`) e a agregação `weekly` (por semana ISO:
coerentes/divergentes/sem trade) — a tab Relatório mostra a tabela "📋
Recounts do grupo vs fills reais" com os divergentes marcados e o
mini-gráfico semanal. Um recount AO VIVO sem trade correspondente no dia
dispara notificação informativa `system` ("Recount sem trade correspondente")
com a **causa provável** (`_recount_no_trade_cause`): sinal do dia
failed/rejected, pendente (guard de entrada), direção oposta no dia, ou kill
switch/sinal de outro feed. Um recount AO VIVO cuja
excursão/reversão não cabe no range real do dia dispara notificação `risk`
"Recount implausível vs candles do dia" (`candle_check.plausible=False`).

O candle-check usa **janela horária** quando a hora do sinal alvo é
conhecida: o high/low do dia inteiro mistura sessões (um pico às 22:00 não
valida um relato do sinal das 09:00), por isso mede também o range dos
candles ±3h à volta da hora do sinal (`window_high`/`window_low`/
`window_excursion_ok`/`window_reversal_ok`/`window_plausible`) e usa ESSE
veredicto como `plausible`; o dia inteiro fica em `day_plausible`/
`day_high`/`day_low` para contexto. A notificação implausível mostra a
janela (ou o dia, sem hora do sinal) com as flags, e o tooltip da tabela
de recounts expõe dia + janela para o operador ver sem abrir logs.

Ferramentas read-only: `app/sweep_recount_candles.py` (sweep da
plausibilidade de todos os recounts — taxa de implausíveis/falsos
positivos), `app/backfill_recounts.py` (re-analisa `ignored_messages`/
`signals` falhadas à procura de `Signal #N` descartados como conversa;
`--apply` grava em `trade_recounts`, sem ele só pré-visualiza),
`app/study_recount_mfe.py` (compara os pips reportados com o MFE real dos
candles: concordância de win / falsos win / wins não reportados — valida a
convenção de win do grupo R3 contra a excursão real) e
`app/study_recount_implausible.py` (cruza cada recount implausível com os
fills reais do dia: o bot negociou — compara entrada/pips de cada lado —
ou não negociou — causa provável por sinal do dia).

Quando um recount AO VIVO é marcado implausível e o journal está ligado
(`execution_journal_enabled`), o `_handle_recount` grava um evento
`recount_implausible` em `execution_events` (signal_id, message_id, campos
reportados, scope janela/dia, flags `excursion_ok`/`reversal_ok` e os
ranges). É evidência passiva: o replay ignora kinds desconhecidos, por isso
nunca autoriza ordens nem altera decisões simuladas.

O `compare_broker_fills` (GET /api/analysis/fill-audit) devolve agora
`recount_evidence`: os eventos `recount_implausible` do journal listados
como divergência de evidência — "real" = relato do grupo, "replay" = range
dos candles, com `bot_traded`/`bot_trade` (trade real fechada do dia com
entrada próxima) para cruzar quem divergiu, sem re-consultar o Yahoo.
`POST /api/recounts/{id}/recheck` refaz o fill-audit + candle-check de um
recount com candles frescos (read-only: não altera a linha nem o journal).
Quando `bot_traded=False`, o `recount_evidence` cruza os reinícios do
listener (`listener_restarts`, ±15 min à volta do evento) e o kill switch
persistido (`kill_switch`/`killswitch_rearm_at`) e devolve `miss_reason`
para apontar a causa provável da execução perdida. O reinício é
classificado em `restart_kind` (watchdog se o motivo contém "watchdog",
senão manual/externo — ex.: boot.ps1) e `signals_lost_est` estima os
sinais do grupo nos 60s após o arranque (o replay do histórico recupera,
mas a execução ao vivo atrasa). Quando `restart_kind=manual`, o cruze
com `server_booted_at` (persistido na config no lifespan — o boot.ps1
arranca o servidor) devolve `vps_reboot=true` se o reinício coincide com
o boot da VPS (Tarefa NexGen Boot), distinguindo reboot de reinício
manual isolado.

O candle-check aplica o `_gold_futures_offset` quando há amostras
suficientes (≥3 fills reais de XAUUSD, |offset| ≥ 5 pontos): a mediana da
diferença entre o candle GC=F na hora do fill e a entrada spot do bot é
usada para corrigir os preços reportados para o referencial dos candles
antes de comparar (`gold_offset` no resultado; o evento
`recount_implausible` do journal grava o offset aplicado no payload, para
o replay reproduzir o mesmo veredicto) — o feed Yahoo serve GC=F
futuros enquanto o grupo negocia spot, e sem esta correção o filtro marca
implausíveis com falsos positivos (baseline medido: 66% dos sinais reais
ficam fora da janela ±3h, 62.4% com a janela alargada para 6h —
`app/study_recount_baseline.py --window-hours 6`; o estudo
`app/study_signal_delay.py` mostra mediana de 3.7h entre o sinal do
Telegram e o toque da entrada no GC=F). O `recounts_report` expõe o
bloco `calibration` (offset estimado, nº de amostras, ativo sim/não) e o
/auditoria mostra o painel "Offset GC=F↔spot" para saber quando a
calibração passa a estar ativa (≥3 fills reais).
A página `/auditoria` tem o bloco "📋 Qualidade dos recounts do grupo"
(stats + tabela + estado semanal + mini-gráfico de implausibilidade por
semana) e um details de recounts implausíveis no card de fill-audit, com
botão "🔄 re-check" por linha; tudo carregado no mesmo `loadAudit` do
journal-quality/fill-audit/forward-gate. O mesmo card tem o toggle ⏸️/▶️
do journal (`execution_journal_enabled`, com confirmação). O resumo diário
inclui a linha "📋 Recounts do dia" (recebidos/implausíveis/sem execução).
O `app/study_recount_implausible.py` emite veredicto agregado (erro de
relato / erro de execução / sem dados + top-5 de cada),
`app/study_recount_baseline.py` mede o baseline de falsos positivos do
candle-check sobre os sinais reais (importante: o feed Yahoo serve GC=F
futuros enquanto o grupo negocia spot — a divergência de dezenas de pontos
faz a taxa parecer alta sem provar relato falso) e
`app/study_signal_delay.py` mede o atraso entre a hora do sinal no
Telegram e o toque da entrada no GC=F (mediana 3.7h nos 30d reais — a
janela ±3h do check é justa face ao feed). O bloco de recounts do
/auditoria mostra a taxa de implausibilidade dos recounts vs o baseline
(66%) para o operador distinguir relato pior que o esperado de simples
divergência spot↔futuros.

**Janela adaptativa por símbolo**: o candle-check usa
`recount_window_hours_by_symbol` (config JSON) por símbolo, com fallback
para `recount_window_hours` (default 3h), limitado a [1, 24]h
(`_recount_window_hours`). O `study_signal_delay` agrega o atraso por
símbolo e recomenda a janela (p75 limitado a [3, 12]h, `--apply` grava);
na BD real aplicou-se XAUUSD=6h porque a recomendação automática (12h) é
contaminada pelo feed GC=F (p75 29h ≫ mediana 3.7h — a cauda é o futuro a
tocar a entrada tarde, não o atraso do grupo).

**Re-check em lote**: `POST /api/recounts/recheck-all` refaz o
fill-audit + candle-check de TODOS os recounts com candles frescos
(buscados UMA vez por símbolo, janela adaptativa e offset mais recente) e
persiste o veredicto em `trade_recounts.candle_check` (+ `checked_at`,
migração automática). O /auditoria tem o botão "🗂 re-check todos" e as
linhas mostram o veredicto guardado pelo lote. `_recount_resolve`
(partilhado pelo check e pelo lote) devolve símbolo + hora do sinal alvo.

**Fonte primária spot MT5 + veredicto combinado**: o candle-check de
produção usa agora os candles M15 de XAUUSD do terminal (spot nativo) como
fonte primária via `_recount_candles` (cache 5 min por símbolo), com
fallback para o Yahoo GC=F quando o terminal está desligado. O veredicto é
COMBINADO: computa `spot_plausible` (MT5) e `futures_plausible` (GC=F); o
`plausible` é o do spot quando disponível (o grupo negocia spot); quando o
spot diz plausível mas o GC=F implausível, `feed_divergence=true` e
`verdict_kind="feed_divergence"` — o /auditoria e a dashboard marcam
"⚠️ provável divergência de feed" em vez de relato suspeito. O GC=F é
comparado SEM correção de offset (o offset spot↔futuros foi eliminado — o
spot é a verdade e a calibração perdeu o propósito; o helper
`_gold_futures_offset` ficou só para o estudo read-only
`study_recount_baseline --apply-offset`). `_recount_check_source` isola o
veredicto por fonte (dia + janela). O feed spot cobre qualquer símbolo do
mapeamento MT5 (XAUUSD, XAGUSD, forex) com fallback Yahoo por símbolo.

**Feed MT5 spot vs GC=F**: `app/study_recount_mt5_feed.py` corre o MESMO
check com candles M15 de XAUUSD do terminal (spot nativo, `apply_offset=False`
— aplicar o offset de futuros a candles spot seria dupla correção) lado a
lado com o GC=F; `--signals` mede as entradas dos SINAIS reais em vez dos
recounts (re-baseline com o feed certo). Resultado real (30d, 101 sinais):
**0.0%** das entradas ficam fora da janela ±3h no spot MT5 vs **66.3%** no
GC=F — os implausíveis eram TODOS divergência do feed de futuros. Na BD
real o recount de 08-19 marcado implausível pelo GC=F é PLAUSÍVEL no spot
MT5 (reversão 4351 dentro do low real 4335; excursão 4362 no high da
janela 4362). O estudo corrigiu dois bugs latentes do
`MT5Provider.copy_rates` (`or []` sobre structured array numpy — truthiness
ambíguo — e acesso a campos por atributo que o numpy 2.x removeu → velas
com ts=0); agora usa acesso por índice com fallback para atributo.

**Alerta de relato suspeito confirmado**: o `_handle_recount` só emite a
notificação risk "Recount implausível" quando o spot MT5 (primária) E o
GC=F discordam do relato (ou sem GC=F para confirmar). Se o spot diz
implausível mas o GC=F diz plausível, não há confirmação — sem alerta
(antes da fonte spot, a divergência spot↔futuros fabricava falsos
implausíveis). O /auditoria separa "Implaus. confirmados" (spot+GC=F) de
"Divergência de feed" (GC=F implausível, spot plausível), com baseline 0%
(spot) em vez do 66% do feed de futuros.

**Re-check em lote diário**: o tick corre `_check_recount_batch` uma vez
por dia UTC (guard `recount_batch_last_run` na config) — refaz o check de
todos os recounts com as fontes combinadas (cache por símbolo) e notifica
no feed/Telegram quando algum veredicto persistido muda de regime ou há
erros. `POST /api/recounts/recheck-all` continua disponível para correr à
mão.

O dashboard é servido em `/` (static). A extinta tab **Edge** foi consolidada
nas **páginas de teste**, que mostram:

- uma **comparação A/B/C** (`GET /api/analysis/compare`): TP1..TP5 (nova) vs
  **TP1..TP5 + gestão do grupo** (backtest que simula as instruções
  `protect` / `close_all` do grupo acopladas aos sinais por fonte+tempo,
  com SL-first e fills sem look-ahead) vs **fechar tudo no TP2**
  (`exit_at_tp=2` — quando o TP2 bate, todas as pernas ainda abertas
  fecham nesse preço). A estratégia antiga (TP1/TP2 only) foi removida.
  Cada coluna mostra WR, profit factor, P&L e drawdown; o vencedor é
  destacado. A página mostra o compare do período (seletor 7/30/90) **e** o
  bloco "📜 Desde o início do grupo" sobre todo o histórico (`days=0`) — o
  edge mantém-se?
- o **walk-forward** (`GET /api/analysis/walkforward`) — o edge da gestão
  por janelas temporais;
- os **estudos de risco** (seletores risco %/saldo, default 1000).

- o compare aceita `days=0` = todo o histórico na BD (run_compare →
  run_analysis days<=0, candles 1h/730d).
- **`since=YYYY-MM-DD`** em `run_analysis`/`run_compare`/`month_sim` filtra
  os sinais a partir dessa data — o bloco "📜 Desde o início" das páginas de
  teste tem um **seletor de período** (config
  `all_period`, ver 4.5): `"0"` = todo o histórico, `2026-07-28` (primeira
  mensagem do formato atual) = só o formato atual, e o 1º dia do mês
  corrente (dinâmico) = só o mês. Cada payload devolve `pnl_by_month`
  (gráfico de barras), `pnl_by_leg` (P&L por perna leg1..leg5, só trades
  resolvidas — as páginas de teste mostram as chips "P&L por perna") e
  `month_sim` devolve também `bankrupt`/`bankrupt_date` (o sim para quando
  o saldo acumulado chega a zero — o veredicto mostra a data de falência).
- uma **simulação** (`GET /api/analysis/month`, `run_month_sim`): replay
  dos sinais reais com `manage=True` desde há `days` dias até hoje — ou
  **`days=0` = todo o histórico na BD** (desde o início do grupo, sem
  filtro de data; candles 1h/730d no Yahoo) — começando com o saldo atual
  (`start_balance`), devolvendo `end_balance`, P&L, WR/PF, `resolved` e a
  `equity_curve` (P&L acumulado por data de receção do sinal) — as
  páginas de teste desenham a mini-curva e mostram "se tivesses começado há 1 mês..." e as
  páginas de teste mostram os dois horizontes por estratégia.

### 4.3 Proteção híbrida, causalidade e paridade

A `auto_escada_3_hibrida` mantém escada/trailing/modo defensivo e honra
apenas `protect`. `decide_protect()` faz o merge monotónico: numa compra o
SL mais alto vence; numa venda o mais baixo; a perna 1 só fecha quando o
preço já conhecido no instante do evento torna a saída elegível. Paper e MT5
persistem uma linha por perna em `protect_latency_log`; o backtest devolve a
mesma telemetria sem escrever na BD.

`GET /api/analysis/protect_latency` lê paper/MT5 ou recalcula com
`engine=backtest`. `GET /api/analysis/protect_causal` faz um replay por
prefixos cronológicos: baseline mecânico → primeiro `protect` → segundo,
etc.; o `incremental_pnl` de cada prefixo explicita a contribuição do evento
e os deltas somam o efeito híbrida vs auto. No replay de 19/08 foram 355
eventos em 270 sinais, 0 harmful e delta total 0 €, pelo que a perda da
híbrida não é causada pelos `protect` observados.

A regressão `tests/test_engine_parity.py` usa o mesmo caminho de preços buy/sell
no backtest, SimEngine, PaperRace e MT5 dry-run e normaliza decisões, SL
aplicado, razão de saída e P&L com `position_observation()`.

### 4.4 Estudos read-only de gestão, risco e polling

`app/study_strategies.py` acrescenta cinco estudos sem writes e sem ordens. O
benchmark de polling injeta `ManualClock`, portanto 1s/250ms/100ms medem
realmente a cadência simulada (incluindo time stops), em vez de depender do
tempo de execução do processo:

- `--study ablation`: sem gestão, cada kind isolado (`protect`, `close_all`,
  `move_sl`, `tp_hit`) e todos os kinds;
- `--study walkforward --period current|90|all|old`: em cada fold escolhe a
  política apenas no treino e valida no bloco cronologicamente seguinte; `old`
  usa 2026-04-01 até 2026-07-28, `all` usa todo o histórico e ambos forçam
  candles 1h/730d para não truncar o período antigo;
- `--study legs`: compara pesos iguais com uma redistribuição leve para TP3–TP5;
  o risco total é normalizado, mas o piso de lote pode tornar as variantes
  iguais com saldos pequenos;
- `--study fallback`: depois de TP1, sobe o SL das pernas restantes para a
  proteção monotónica do grupo se nenhuma mensagem chegar no prazo configurado.
  `management_fallback_seconds=None` é o default e mantém o comportamento
  atual;
- `--study polling`: usa BD temporária e uma trajetória sintética para medir a
  sensibilidade do `PaperRace` a 1s/250ms/100ms. É um teste de polling, não
  um backtest mensal; o intervalo do `BackgroundRuntime` de produção continua
  1s.

Os parâmetros opcionais `leg_weights`, `management_fallback_seconds` e
`fallback_buffer_fraction` entram em `run_analysis`/`simulate_signal` apenas
para estes estudos e não fazem parte de qualquer estratégia promovida. Os
resultados de 19/08 ficam na Parte VIII-A de `docs/ESTRATEGIAS.md`.

### 4.4 Re-carregamento do histórico e resumos

Quando o listener recarrega o histórico (`SIGNALBOT_TG_HISTORY=1`), as
mensagens antigas re-entram na API. Para que os resumos do grupo guardados
como `failed` num carregamento anterior sejam recuperados:

- a classificação de gestão corre **antes** da dedup de estado final, por isso
  um resumo/mensagem de gestão histórico re-entra sempre no handler;
- mensagens de gestão **históricas** (`historical=True`) são registadas mas
  **nunca agem** sobre trades abertas;
- `insert_group_summary` é idempotente por `message_id` (coluna `UNIQUE`) —
  repetir um resumo atualiza a linha em vez de duplicar;
- `list_group_summaries` ordena pelo período coberto (mais recente primeiro),
  para mostrar sempre o resumo mais atual (ex.: o semanal).

### 5.2 Observabilidade operacional (Item 10)

A telemetria operacional é persistida na mesma SQLite e não altera a execução
nem abre trades. `GET /api/operations?days=N` (1–730 dias; default 90) devolve
um payload único para o painel do Relatório e para o bloco
`operations.metrics` de `/api/diagnostics`:

- `exceptions`: total, contagem por tipo e linhas recentes com timestamp,
  stack trace, contexto JSON, `signal_id` e `trade_id`;
- `spread`: observações bid/ask MT5 e agregação mensal por símbolo (`count`,
  média, mínimo e máximo em pips);
- `mt5.connections.weekly`: tentativas/sucessos/falhas de reconexão,
  ligações, quedas e taxa de sucesso por semana UTC;
- `mt5.modifies`: tentativas de modify de SL, aceites/recusadas e distribuição
  de retcodes por semana, incluindo 10016 e outros erros do broker.

O `BackgroundRuntime` grava exceções que escapem aos loops de mercado ou
watchdog. `BotState.tick` regista falhas de amostragem de spread, o
`_reconnect_mt5` regista cada tentativa/resultado, e `MT5Engine` grava a
operação de modify antes de considerar o novo SL aplicado. Para quotes reais,
o pip de forex (0.0001 em símbolos de 5 dígitos) fica separado do tick do
broker (0.00001), logo spread e distâncias de risco não são inflacionados. A tabela do
Relatório mostra explicitamente "sem observações" quando ainda não há dados;
a tendência só deve ser interpretada depois de pelo menos duas semanas.
As tabelas são `operational_exceptions`, `spread_observations`,
`mt5_connection_events` e `mt5_modify_events`, com índices temporais e política
de retenção documentada por `maintenance.py --db-audit`.

### 5.3 LLM fallback (opcional, desligado por defeito)

O regex é o parser principal (determinístico e testado). Quando **falha** e uma
API LLM está configurada, o texto vai ao modelo e a proposta passa pelos
**mesmos sanity checks** do regex (`validate_signal`):

- confiança < `SIGNALBOT_LLM_MIN_CONFIDENCE` (default 0.8) → **rejeitado**;
- TPs fora de ordem / SL do lado errado / TP absurdo → **rejeitado**;
- sinais parseados por IA ficam `llm_parsed=True` → **pendentes, nunca
  executam em auto** (exigem aprovação humana).

| Variável | Uso |
|---|---|
| `SIGNALBOT_LLM_API_KEY` | Chave da API (presença liga o fallback) |
| `SIGNALBOT_LLM_BASE_URL` | Endpoint OpenAI-compatible (default `https://api.openai.com/v1`) |
| `SIGNALBOT_LLM_MODEL` | Modelo (default `gpt-4o-mini`) |
| `SIGNALBOT_LLM_MIN_CONFIDENCE` | Confiança mínima 0..1 (default 0.8) |

### 5.1 Listener de Telegram

`app/telegram_listener.py` é um processo separado (roda ao lado do uvicorn) que
usa a **conta de utilizador** (Telethon) para ouvir o grupo. Cada mensagem e
cada edição são reencaminhadas para `POST /api/signals` com o `message_id`, de
modo que a janela de edição (secção 4.1) se aplica. Configuração via env vars
(ou `.env`): `SIGNALBOT_TG_API_ID`, `SIGNALBOT_TG_API_HASH`,
`SIGNALBOT_TG_PHONE`, `SIGNALBOT_TG_GROUP` (link, `@username` ou id),
`SIGNALBOT_API_URL` e `SIGNALBOT_TG_FILTER` (0 = reencaminha tudo). O
replay do histórico no arranque usa **`SIGNALBOT_TG_HISTORICAL_LIMIT`**
(nº de mensagens a percorrer; `0`/omissão = TODAS desde o início do grupo;
alias antigo `SIGNALBOT_TG_HISTORY_LIMIT`). `history_limit()` resolve o
valor — devolve `None` para "sem limite" porque `iter_messages(limit=0)`
devolveria zero mensagens no Telethon. No fim do replay, o listener conta as
mensagens reencaminhadas por ação (sinais/gestão/resumos/ignoradas) e
avisa o servidor via **`POST /api/listener/history_done`** (`state.history_done`)
— que regista a notificação "📚 Replay do histórico concluído" no feed.

---

## 6. Configuração

Chaves persistidas na tabela `config` do SQLite (editáveis pela dashboard):

| Chave | Default | Descrição |
|---|---|---|
| `mode` | `auto` | Único caminho desde o Item 12 — POST com outro valor é recusado com erro |
| `risk_pct` | `2.5` | Risco por trade (% da equity) — base do risco dinâmico (slider 0.1–10) |
| `peak_balance` | `0` | Pico de equity (high-watermark) para o risco dinâmico; 0 = ainda não inicializado |
| `daily_loss_limit_pct` | `3.0` | Limite de perda diária que dispara o auto kill |
| `max_positions` | `5` | Máximo de trades abertas em simultâneo (a estratégia de 5 pernas precisa de até 5) |
| `kill_switch` | `false` | Pausa global |
| `auto_kill` | `false` | Bloqueio automático por perda diária |
| `killswitch_rearm_at` | vazio | Timestamp ISO UTC persistido do rearme automático; por defeito é agendado para 2026-08-29 00:00 UTC quando o kill é ativado |
| `promotion_window_start_at` | `2026-08-20T00:00:00+00:00` | Início da janela real de revisão até 28/08 |
| `promotion_window_end_at` | `2026-08-29T00:00:00+00:00` | Fim exclusivo da janela UTC de 28/08 |
| `sim_speed` | `1` | Multiplicador de velocidade do mercado simulado (0 = pausa) |
| `edit_window_seconds` | `20` | Janela de espera por edições do Telegram (só quando o primeiro parse está incompleto) |
| `entry_guard_tolerance` | `0.5` | Margem automática em unidades de preço fora da zona; acima dela pede confirmação manual |
| `summary_time` | `23:59` | Hora do resumo diário |
| `balance` | `10000` | Saldo simulado/fallback; em `mt5-real` o saldo do broker é a fonte de verdade |
| `summary_sent_date` | — | Última data em que o resumo foi enviado |
| `strategy` | `tp2_protect` | Estratégia ativa da conta principal (estado operacional em 2026-09-01) |

**Configuração operacional atual da VPS (2026-09-01):** `strategy=tp2_protect`,
`risk_pct=4.0`, `peak_balance=1045.56`, `max_positions=5`, `kill_switch=false`,
conta principal demo hedged `5054644473`, saldo observado €1.003,89 e
`MT5_DRY_RUN=0`. O servidor está em `mt5-real`; ordens aprovadas são enviadas
para a conta demo. Esta configuração não autoriza uma conta real.
| `shadow_paper_enabled` | `true` | Recebe sinais live completos numa conta paper isolada, mesmo com kill switch ativo; nunca envia MT5 |
| `shadow_paper_strategies` | `["auto_escada_3", "auto_escada_5"]` | IDs visíveis que recebem sinais shadow; valida contra `STRATEGIES` e rejeita estratégias ocultas |
| `teste_days` | `30` | Seletor das páginas de teste (7/30/90; 0 = todo o histórico) |
| `all_period` | `"0"` | Seletor de período do bloco "desde o início" das páginas de teste: `"0"` = todo o histórico, `"2026-07-28"` = só o formato atual, `"YYYY-MM-01"` = só o mês corrente |
| `edge_risk` | `""` | Risco do sim nas páginas de teste (estudos): vazio = `risk_pct` da config |
| `edge_bal` | `""` | Saldo inicial do sim nas páginas de teste (estudos): vazio = default 1000 (saldo próprio da conta paper) |

**Kill diário no backtest** — `run_analysis`/`run_compare`/`run_month_sim`
aceitam `daily_stop_pct` (None = `daily_loss_limit_pct` da config; 0 =
off). `app/backtest.apply_daily_stop(results, balance, stop_pct)` percorre
os resultados por ordem cronológica; quando o P&L realizado do dia ≤
`-balance*stop_pct/100`, as trades seguintes desse dia ficam
`outcome="skip"`/pnl 0 (contadas em `daily_stop_skipped` no payload). Os
números da dashboard refletem o kill real da produção.

**Estudos de risco** — `/api/analysis`, `/api/analysis/compare`,
`/api/analysis/month` aceitam `risk_pct` e `balance` (0 = config/saldo
atual). As páginas de teste têm os seletores "Risco do sim" + saldo
(`testeSimQS()`, persistidos em `edge_risk`/`edge_bal`; saldo default 1000).
⚠️ Com saldo
~1000 € o piso `MIN_LOTS` (0.01) domina: riscos ≤ ~4% dão os mesmos lotes
— para estudar risco usa saldos maiores (o PF não muda com o risco; só o
P&L/drawdown escalam).

**Sweep reproduzível do Item 2** —
`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation --item2-sweep`
corre `auto_escada_5` em 27 combinações (`def_window` 5/7/10 ×
`def_trail` 1.0/1.2/1.5 × `def_time_stop` 30/45/60) nas janelas atual/tudo/
antiga. O baseline de cada janela é calculado uma vez e a história baseline
é reutilizada, sem leakage entre combinações. O filtro de entrada do estudo
converte timestamps com fuso para UTC, usa a última observação de ATR de cada
dia UTC e calcula o piso p20 de cada sinal apenas com dias anteriores; assim
não usa volatilidade futura. O gate só aceita DD antiga <50% e PF atual ≥85%
do baseline; o snapshot de 19/08 teve 0 elegíveis e um pico isolado no eixo
`def_window` do período atual, portanto não mudou o default.

**Sweep reproduzível do Item 3** —
`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation
--item3-sweep` corre duas variantes read-only da `auto_escada_3` nas janelas
`atual`, `tudo` e `antiga`: mantém `max_legs=3`, `stair_levels=3`,
`stair_from=1`, `trail_factor=2.0` e custos fixos, alternando apenas
`confirm_filter`. Cada variante ordena os sinais cronologicamente, reutiliza
as mesmas velas e não partilha estado mutável com a outra (sem leakage). O
gate de aceitação exige DD antiga estritamente menor e queda do PF atual ≤10%
relativa; o replay de 19/08 deu `confirm=False` 1.27/11.8% (PF/DD atual),
0.47/322.0% (tudo) e 0.40/312.9% (antiga), contra `confirm=True` 1.26/11.8%,
0.53/286.1% e 0.46/278.3%. Passou (queda PF atual 0.79%; redução DD antiga
11.06%), pelo que `auto_escada_3` usa agora `confirm_filter=True`; a principal
`nova_gestao` e a conta demo não foram promovidas/alteradas. O spec
`docs/Melhorias/03-item-03.md` foi apagado após esta aceitação.

**Alerta de regime** — `state._check_regime()` (tick, a cada ~30 min):
corre o sim rolante de 30 dias da estratégia ativa com o kill diário da
config; se PF < 1 com ≥ 10 resolvidas, notifica `risk` (feed + Telegram,
cooldown 6h). Snapshot expõe `regime` (pf/resolved/checked_at/skipped).

**Exemplo de fluxo de uma trade** — `GET /api/analysis/trace?strategy=<id>`:
corre o backtest da estratégia, pega no sinal resolvido mais recente e
re-simula com `trace=True` (`simulate_signal` devolve `events` cronológicos
— SL/TP1/BE/fecho no TP — e o `path` de candles, downsampled a ≤ 90
pontos). A página de teste do TP2 mostra o gráfico + linha do tempo
(`loadTesteFlow`/`drawTraceChart`). Só para estratégias sem gestão.

**Página HELP** — menu (abaixo do Diagnóstico): cards expansíveis por
estratégia (`/api/strategies` + sims de 30 dias em paralelo) + glossário.

O snapshot expõe ainda **`config.risk_preview`** (`state.risk_preview()`):
lotes que o risco% atual representa numa trade típica de XAUUSD (total e
por perna, com `n_legs` e a distância do SL usada — a do último sinal
válido via `DB.last_signal_with_sl()`, ou 30 pontos por omissão). A
dashboard mostra-os no chip "Lotes ≈ do risco%" do bloco de detalhes de
risco.

Variáveis de ambiente (opcionais):

| Variável | Uso |
|---|---|
| `SIGNALBOT_DB` | Caminho do ficheiro SQLite |
| `TELEGRAM_BOT_TOKEN` | Token do bot para notificações reais (opcional) |
| `TELEGRAM_CHAT_ID` | Chat/grupo para onde enviar notificações (opcional) |

Sem credenciais Telegram, as notificações continuam a funcionar (DB + dashboard).

**Alertas OFFLINE no telemóvel** (2026-08-17):

- **MT5 desligado / AutoTrading off** → o listener (`mt5_watch` em
  `telegram_listener.py`) faz polling ao `/api/diagnostics` a cada 10s e faz
  DM à própria conta do Telegram (Saved Messages) na transição ligado→
  desligado (cooldown 60s); o servidor (`_check_mt5_connection` em `state.py`,
  chamado no `tick()` a cada segundo) emite também notificação `risk` no feed
  com cooldown de 60s.
- **Listener offline** → o watchdog (`_ensure_listener_alive` em `main.py`)
  emite notificação `risk` sempre que reinicia o processo.
- Com `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` definidos, **qualquer**
  notificação (`Notifier.send`) é empurrada para o chat via Bot API.

**"Última msg do grupo"** (Diagnóstico): o listener só atualiza `last_msg_at`
nos handlers de mensagens reais — o replay do histórico no arranque **não**
conta como mensagem nova, para a dashboard não mostrar "última msg há Xs"
logo a seguir a um restart sem mensagens novas.

### Execução via MetaTrader 5 (`EXECUTION_MODE=mt5`)

O bot executa no terminal MT5 quando `EXECUTION_MODE=mt5`. Na configuração
operacional atual, a conta principal é a demo `5054644473` e
`MT5_DRY_RUN=0`; o paper/shadow continua isolado para estudos:

| Variável | Uso |
|---|---|
| `EXECUTION_MODE` | `sim` (default) ou `mt5` |
| `MT5_DRY_RUN` | `0` na demo operacional atual: envia ordens; `1` mantém simulação local |
| `MT5_LOGIN` | Número da conta demo/real (atual: `5054644473` demo) |
| `MT5_SERVER` | Servidor (ex.: `MetaQuotes-Demo`) |
| `MT5_PASSWORD` | Senha — **não guardar em ficheiros**; passar no arranque |
| `MT5_SYMBOLS` | JSON `{"XAUUSD":"XAUUSD.a"}` para símbolos cujo nome difere no broker |

Requisitos: o terminal MT5 instalado e **correndo** com a conta logada; o
pacote `MetaTrader5` no `.venv` (`pip install MetaTrader5`). Exemplo:

```bash
MT5_DRY_RUN=0 MT5_LOGIN=5054644473 MT5_SERVER='MetaQuotes-Demo' \
  MT5_PASSWORD='...' .venv/Scripts/python -m uvicorn app.main:app --reload
```

O `MT5Engine` (`execution/mt5.py`) usa a mesma interface do `SimEngine`
(`open_trade` / `update` / `close_trade_at_market`), por isso o risco, a
janela de edição e o Telegram não mudam. Particularidades:

- **Sizing por símbolo**: `compute_lots` usa o contract size e pip real do
  broker (XAUUSD ≈ contract 100 / pip 0.01; EURUSD ≈ contract 100000 /
  pip 0.0001) — o `CONTRACT_SIZE` global só vale para o paper.
- **Dry-run apenas para estudos** (`MT5_DRY_RUN=1`): as posições são registadas na BD e os
  fills simulados no tick atual do MT5; cada ordem que *seria* enviada é
  escrita no log com o prefixo `[mt5·dry]`. Nada chega ao broker.
- **Real** (`MT5_DRY_RUN=0`): ordens vão via `mt5.order_send` (1 ordem por
  perna, SL/TP server-side) e o P&L vem das posições MT5. Depois do TP1, a
  perna 2 é protegida a breakeven com um modify (`TRADE_ACTION_SLTP`).
- **Auto-gestão (Fase 3, docs/ESTRATEGIAS.md Parte X)**: `update(now,
  exit_at_tp, auto)` — o
  `auto` (escada/trailing/confirmação/time stop) só é passado quando a
  estratégia ativa os define (a `nova_gestao` não tem nenhum → inerte até
  uma `auto_*` ser promovida). No `_update_real`: TP_k confirma → `move_leg_sl`
  das pernas seguintes para o nível do TP_k (escada); perna k leva SL →
  fecho das pernas > k (confirm); após o TP1 → trailing (`SL = pico −
  trail_factor × dist(entrada,TP2)`, pico dos ticks ANTERIORES, sem
  look-ahead) e time stop. **Três níveis de SL** (`_valid_sl`): o alvo
  teórico é limitado por `stops_level`/`freeze_level` do símbolo;
  **`_modify_sl` devolve o retcode** e `_auto_move_sl` só marca o SL como
  *applied* (BD + throttle) quando o broker confirma (10009/0) — uma recusa
  10016 fica no log sem alterar a BD. **Throttling de modify**
  (`_auto_move_sl`): só sobe se melhorar ≥ 1 pip e ≤ 1×/min por perna
  (`_auto_state` em memória por trade — após restart o pico recomeça do
  preço atual, documentado).
- **Trailing adaptativo por ATR** (docs/ESTRATEGIAS.md Parte X,
  pós-validação) — a MESMA
  fórmula `trail_dist = max(trail_factor × dist, atr_k × ATR)` nos três
  motores, sempre sem look-ahead (o ATR usa só dados ANTERIORES ao evento):
  - **MT5** (`MT5Provider.copy_rates(symbol, timeframe, count)` — enum
    M1=1/M5=5 via `copy_rates_from_pos` a partir da posição 1 (velas completas)
    + `MT5Engine._atr`); `atr_timeframe`
    é só aqui (enum de velas do terminal);
  - **backtest** (`simulate_signal` + `_candle_atr` em backtest.py — ATR
    das velas anteriores à atual, do próprio caminho de velas);
  - **corrida paper** (`PaperRace._paper_atr` — ATR rolante por trade com
    baldes de minuto: o minuto atual está em progresso e NUNCA entra no ATR
    que ele próprio usa).
  `run_analysis`/`sim_params` aceitam `atr_k`/`atr_period`; sem dados
  degrada para o trail fixo. Nenhuma estratégia define `atr_k` hoje
  (desligado) — é a melhoria pós-validação.
- **Auditoria dos três níveis de SL da auto** (janela de validação,
  docs/ESTRATEGIAS.md Parte X): `_auto_move_sl` regista cada tentativa
  `{ts, leg, kind, target, valid, clamped, retcode, applied}` persistida na
  config `autosl:<trade_id>` (JSON, cap 300) — sobrevive a restarts;
  `GET /api/analysis/auto_sl_log` → `state.auto_sl_log` devolve por trade;
  tabela no painel da Fase 3 da principal. Um modify **recusado (10016) ou
  arredondado (clamped)** também devolve um evento `auto_sl_warn` do
  `update` (dedup por trade+perna+alvo em `_sl_warned`; drenado para os
  eventos no fim do update) → `state.tick` notifica `risk` no feed/Telegram
  — a recusa NÃO marca throttle (pode acertar quando o broker deixar), mas
  o aviso não spamma.
- **Sweep ATR / trail fino** (`app/sweep_atr.py` — ferramenta de estudo
  como `sweep_auto.py`): grelha atr_k × period (ou `--trail` para o trail
  fixo) nas auto_* em 3 janelas (atual, tudo, out-of-sample), com reuso de
  candles por janela. Resultados: **ATR rejeitado** (pior que o trail fixo
  2.0 — ver ESTRATEGIAS.md Parte X) e **trail fino = plateau monotónico**
  (2.0 interior, mantém-se). `atr_k` fica desligado em todas as estratégias.
- **Snapshot da janela de validação**: ao promover uma auto com auto-gestão
  (`POST /api/config`), `state._fase3_promotion_snapshot` grava na config
  `fase3_snapshot` (JSON): stats da corrida paper, DD das contas (curva
  30d), divergences paper↔MT5, trades reais fechadas e nº de entradas da
  auditoria SL — exposto em `snapshot()["fase3_snapshot"]` e mostrado no
  painel Fase 3 (comparação antes/depois da janela, ESTRATEGIAS.md
  Parte X).
- **ETA da janela** (`snapshot()["fase3_eta"]`): ritmo de sinais/dia da
  `auto_escada_3` = trades de papel abertas nos últimos 30 dias
  (`db.paper_trades_opened_since`) / 30; data estimada dos 20 sinais =
  `now + (20 − N) / ritmo`; `done=True` quando N ≥ 20, `eta=None` sem
  ritmo (corrida acabou de começar).
- **Resumo diário com a auto MT5** (ESTRATEGIAS.md Parte X):
  `state._fase3_mt5_line` —
  quando a estratégia ativa é uma auto com auto-gestão e existe snapshot,
  o resumo diário ganha a linha 🚀 com N real e P&L real (trades fechadas
  desde `promoted_at`) e o estado da janela (em curso x/10 ou veredicto).
  Se a janela fechou HOJE (veredicto recente) mesmo com a auto já
  destituída (rollback), a linha continua no resumo a destacar o veredicto
  (comparação em UTC — `evaluated_at` é `_now_iso`).
- **Alerta de ETA** (`_check_fase3_eta_alert`, corre no tick, cooldown 24h
  em `_fase3_eta_alert_at`): o `fase3_eta` também devolve `pace_prev`
  (ritmo da janela 30–60 dias) e `pace_change_pct`; o alerta dispara no
  feed/Telegram quando a janela estimar **> 42 dias** ou o ritmo mudar
  **≥ 50%** face ao mês anterior (ETA recalculado). Quando estimar
  **> 6 semanas** e o alvo ainda for 20, a notificação leva a action
  `fase3_target:15` → botão "🎯 Reduzir alvo para 15" no feed →
  `POST /api/config {fase3_target}` (clamp 10–20; `config.fase3_target`
  default 20; o ETA/checklist/painel passam a contar até ao novo alvo).
- **Mini-curva do ritmo**: `fase3_eta.pace_daily` = contagem por dia dos
  últimos 60 dias (zero-preenchida; `db.paper_trades_opened_by_day`) com a
  AUTO (`n`) e a REFERÊNCIA `nova_gestao` (`n_ref`) lado a lado — barras
  com tooltip no painel da Fase 2 (a tendência que alimenta o ETA).
- **Endpoint único do painel** `GET /api/analysis/fase3_status`
  (`state.fase3_status`): ETA (curva incluída), snapshot antes/depois,
  veredictos + histórico, `target_log`, divergences paper↔MT5, real vs
  backtest, DD das contas (curva 30d, calculado no servidor) e auditoria de
  SL — o painel passou de 4 chamadas para 1.
- **Registo de reduções de alvo** `fase3_target_log` (config, JSON, cap 20):
  cada alteração a `fase3_target` regista `{at, from, to, reason}` — o
  motivo vem de `fase3_target_reason` no `POST /api/config` (o botão do
  feed envia "ETA > 6 semanas (botão do feed)"). Bloco no histórico do
  painel + `snapshot()["fase3_target_log"]`.
- **Histórico de veredictos**: `fase3_verdicts` (config, JSON, cap 20) —
  cada avaliação apêndice o veredicto; uma nova promoção limpa apenas
  `fase3_verdict` (janela atual) para reabrir a avaliação; exposto em
  `snapshot()["fase3_verdicts"]` e no bloco "📚 Histórico" do painel.
- **Veredicto automático da janela** (`snapshot()["fase3_verdict"]`):
  `state._check_fase3_validation` corre no tick quando a estratégia ativa é
  uma auto com auto-gestão, existe `fase3_snapshot` e ≥ 10 trades reais
  abertas depois de `promoted_at` fecharam. `_fase3_window_trades` ordena-as
  por fecho e limita a janela às primeiras 10 (ignorando posições abertas
  antes da promoção e trades explicitamente de outra estratégia). Calcula PF
  real, divergências paper↔MT5 (e exige a mesma saída em ≥9/10; ausência de
  pares também reprova), posições abertas sem SL, duplicados, atraso >2 min,
  P&L incoerente e retcodes de `autosl:<trade_id>` fora de 10009/0.
  **Rollback automático** para a `nova_gestao` se qualquer gate falhar; senão
  **mantém**. Persiste `fase3_verdict` (JSON com stats +
  `snapshot_before`) na config e notifica feed/Telegram (kind `risk` no
  rollback, `system` na manutenção). Corre UMA vez por janela — uma nova
  promoção grava snapshot novo e reabre a janela.
- **Painel "🚀 Fase 3 — pronto a arrancar"** (principal): checklist dos
  gates (N≥20 · PF≥1.5 · P&L≥50% · DD≤125% da referência · janela 10
  trades), counts do divergence paper↔MT5 e do real vs backtest, DD das
  contas via `/api/paper/history` e a auditoria de SL — refresh 60s
  (`refreshPhase3Instruments`), o render do painel é síncrono do snapshot.
- **Teste E2E do caminho real** (`tests/test_mt5.py`): broker fake
  ESTADO (`_FakeBroker` — abre/fecha posições, deals de entrada/saída,
  modifies SL/TP com validação de distância, retcodes 10009/10016): TP1
  (deal) → escada → trailing → time stop com `dry_run=False`, retcodes
  reais no log + auditoria `autosl:` com os três níveis. A regressão
  `test_fase3_promotion_paper_to_mt5_restart_and_rollback` acrescenta o
  percurso paper → promoção → restart → retcode recusado → rollback sem
  tocar no terminal real.
- **Contas hedged**: o plano TP1..TP5 usa N pernas independentes —
  precisa de uma conta *hedged* (ex.: `Forex Hedged`), não `netting`.

---

## 7. Como correr

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux/macOS

.venv/Scripts/python -m uvicorn app.main:app --reload
# Dashboard: http://127.0.0.1:8000/
```

Testes:

```bash
.venv/Scripts/python -m pytest -q
```

### Correr com o grupo de Telegram

Janela 1 (API + dashboard):

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Janela 2 (listener do grupo):

```bash
.venv/Scripts/python -m app.telegram_listener
```

Na primeira execução o listener pede o código de verificação do Telegram e
cria `signalbot_session.session` (ignorado pelo git).

---

## 8. Notificações

`notify/telegram.py` envia para três destinos em paralelo:

1. Tabela `notifications` (sempre);
2. WebSocket → dashboard (sempre);
3. Telegram (só com `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`).

Eventos: `trade_open`, `trade_close`, `be` (proteção a breakeven),
`risk` (bloqueios/kill switch), `system` (config), `summary` (resumo diário),
`management` (ações da máquina de estados do grupo).

O resumo diário (`state.py:daily_summary`) agrega as trades fechadas no dia:
nº de trades, profit total e win rate. É enviado à hora `summary_time` e pode
ser forçado por `POST /api/summary`. Inclui também a linha da janela UTC até
28/08 com trades reais fechadas/abertas, P&L realizado/aberto, dias restantes e
ritmo necessário para as metas 10–20.

`GET /api/window` é read-only e devolve essa janela, as trades reais da tabela
`trades`, P&L, projeção/ritmo e `promotion`: gate forward shadow da
`auto_escada_3` (N, PF, IC95, DD, referência) + ETA da cadência. A promoção
continua manual e bloqueada no backend até todos os gates passarem.

O kill switch pode persistir `killswitch_rearm_at`. O `state.tick` compara o
instante UTC; quando chega, limpa `kill_switch`, `auto_kill` e a própria chave,
regista uma notificação `system` no feed e Telegram e não toca em posições já
abertas. Um rearme já executado é idempotente.

---

## 9. Segurança

A dashboard/API têm **login por password** quando `SIGNALBOT_DASHBOARD_PASSWORD`
está definida (`app/auth.py` + middleware em `main.py`). Sem a variável,
tudo fica aberto (uso local). Pendente antes de expor na web:

- [x] Autenticação (login) na dashboard — feita (password única, cookie httpOnly, sessões em memória);
- [x] Token de API para o listener (`X-API-Token`) — feita;
- [ ] HTTPS (a execução de trades reais nunca deve correr em HTTP puro) — usar reverse proxy (Caddy) na VPS;
- [ ] Isolar o endpoint de execução (só acessível da própria VPS/VPN);
- [ ] Limites de risco no backend como autoridade final (já existem);
- [ ] Audit log de todas as ações (quem clicou no quê, quando).

---

## 9b. Limitações da ponte MT5

- A API MetaTrader5 mantém **UMA ligação global por processo** — a 2ª conta
  (`MT5_2_*`, lane `tp2`) não pode partilhar o processo do servidor em modo
  real. A execução real da 2ª conta usa o **worker isolado**
  (`app/lane2_worker.py`): um processo separado dono da ligação ao terminal
  **portable** (`MT5_2_TERMINAL_PATH`) que expõe as operações por HTTP local
  (token `LANE2_WORKER_TOKEN`); o processo principal acede-lhe via
  `RemoteProvider` (`app/execution/remote2.py`) com a mesma superfície do
  `MT5Provider` (incluindo `provider._mt5`, o módulo MT5 bruto que o
  `MT5Engine` usa — serializado por `/mt5/call` com allowlist de métodos e
  constantes). O worker **nunca** toca na SQLite; decisões e gravações ficam
  no processo principal (invariante de concorrência preservada).
  Fail-closed: worker offline/terminal ausente/conta não hedged ⇒ a lane 2
  não envia ordens; o market_loop pinga-o ~1×/s e notifica transições; o
  `healthcheck.py` reinicia-o (`worker_*` no `healthcheck.state`),
  `start.ps1`/`boot.ps1` arrancam-no. ⚠️ Sem `MT5_2_TERMINAL_PATH` o worker
  **recusa arrancar (exit 2)** — `initialize(login=...)` sem path liga ao
  terminal por omissão e troca a conta da principal (incidente 01/09).
  O `/status` do worker inclui **`trade_allowed`** (via `terminal_info`)
  espelhado pelo `RemoteProvider` — o Diagnóstico mostra ON/OFF e o
  market_loop alerta quando o AutoTrading do terminal 2 está OFF (por-sessão;
  evento `lane2_auto_trading_off`). A/B read-only das lanes no mesmo sinal:
  `GET /api/lanes/ab` (`app/lanes.py::lane_ab_report`) — métricas lado a
  lado e sugestão de manutenção **apenas com ≥20 sinais comparáveis**
  (`min_comparable`, default 20); abaixo disso o veredicto é neutro. Fechos
  **phantom** (sem `exit_reason` — ordens recusadas, fechadas a 0 no envio)
  ficam fora do comparável em `voids`/`void_count`; com n=0 e voids a
  mensagem explica a amostra (caso real de 01/09: AutoTrading OFF na lane
  2). Nunca altera estratégia/config nem envia ordens.
- O `initialize()` exige o terminal aberto; se falhar, a dashboard continua
  acessível, mas o estado mostra `connected=false`. Em execução real também
  exige `account_info().margin_mode == 2` (`RETAIL_HEDGING`); netting ou modo
  desconhecido bloqueia a ligação e nenhuma ordem real é enviada.
- Símbolos recém-adicionados ao Market Watch podem devolver `0.0` por ~1s
  enquanto o terminal descarrega cotações — o provider ignora esses ticks.
- `open_trade` chama `ensure_symbol` ANTES de enviar ordens; `_send_open_orders`
  trata `symbol_info_tick` None/zero como falha da perna (não crasha nem
  repete a trade toda).
- O `PIP_SIZE`/`CONTRACT_SIZE` globais (`config.py`) aplicam-se só ao
  paper; o MT5 usa sempre os valores reais do broker por símbolo.

## 9c. Operação, logs e restauração

- `app.server_runner` instala `RotatingTextStream` antes de o Uvicorn criar
  handlers. `SIGNALBOT_LOG_MAX_BYTES` (default 10 MiB) e
  `SIGNALBOT_LOG_BACKUP_COUNT` (default 5) limitam `server.log`,
  `server.err.log`, `listener.log`, `listener.err.log`, `lane2_worker.log`,
  `lane2_worker.err.log`; `backup.log` usa a mesma configuração. Em Windows
  não se deve usar redireção externa para os logs, porque mantém o ficheiro
  aberto e impede `os.replace`.
- `BotState._check_disk_space()` é read-only e corre uma vez por dia. O volume
  e o limiar vêm de `SIGNALBOT_DISK_PATH`/`SIGNALBOT_DISK_FREE_GB` (default
  volume do sistema e 10 GiB). O snapshot expõe `operations.disk`; abaixo do
  limiar gera notificação `risk` no feed e, se configurado, no Telegram.
- `maintenance.py --db-audit` lista contagens, meses observados, taxa de
  linhas/mês e política de retenção. `backup_db.py --restore-check` copia o
  backup mais recente para uma BD temporária, valida `PRAGMA integrity_check`
  e as tabelas principais; envia o resultado ao Telegram quando configurado.
  A rotina nunca abre a BD de produção para escrita.

Política operacional atual (o comando `--db-audit` imprime a mesma política e
as contagens observadas):

| Tabelas | Política |
|---|---|
| `signals`, `trades`, `positions`, `paper_trades`, `paper_positions`, `group_summaries`, `config`, `paper_accounts`, `protection_states` | manter indefinidamente; são histórico analítico, auditoria ou estado atual |
| `pending_management` | arquivar linhas aplicadas após 90 dias; manter as não aplicadas |
| `notifications` | arquivar após 90 dias, preservando risco/rollback |
| `paper_balance_history` | arquivar após 180 dias, preservando agregados |
| `protect_latency_log` | arquivar após 365 dias, preservando agregados |
| `listener_restarts` | purge automático após 30 dias |
| `ignored_messages` | últimas 1000 linhas, limite aplicado no insert |

## 10. Roadmap

1. ✅ **Ingestão real de Telegram** — `app/telegram_listener.py` (Telethon)
   lê os grupos (vários), respeita a janela de edição e trata mensagens
   apagadas (fecha a posição ao mercado).
2. ✅ **Histórico do grupo** — ao ligar, o listener envia o histórico completo
   por defeito (`historical=True`), registado sem executar;
   `SIGNALBOT_TG_HISTORICAL_LIMIT` limita o replay a N mensagens (0 = todas).
3. ✅ **Dados de mercado reais** — feed ao vivo (Yahoo/TwelveData) com fallback
   simulado; histórico de candles para backtest.
4. ✅ **Backtest/Simulador** — replay dos sinais reais sobre candles históricos
   com atraso configurável (1 min por defeito) e **custos reais** (spread,
   slippage, comissão).
5. ✅ **Relatório** — win rate, profit factor, R:R, consistência por grupo,
   **curva de equity** e **drawdown máximo**.
6. ✅ **Execução real** — conector para MetaTrader 5 (Python API numa VPS Windows,
   ou EA em MQL5) com o mesmo motor de risco à frente (MT5Engine em dry-run/real).
6b. ✅ **Gestão do grupo** — máquina de estados para TP HIT / SL HIT / close
   first / out at BE, + resumos do grupo como ground truth na dashboard.
6c. ✅ **LLM fallback** — parser IA opcional com sanity checks, nunca auto.
6d. ✅ **Backtest com gestão do grupo** — simula `protect`/`close_all` nos
   mesmos sinais e compara (Edge) o WR/PF com e sem gestão vs o reportado
   pelo grupo.
7. **Modo live com limites** — arrancar com risco mínimo (0.1–0.5%) e validar
   durante semanas antes de subir.

---

## 10.1 — Risco defensivo, shadow-paper e restart-safe

As estratégias `auto_escada_3`, `auto_escada_3_hibrida` e `auto_escada_5`
partilham `effective_strategy_risk_pct`: quando existem 10 trades fechadas
anteriores com PF < 1.0, o risco de novas entradas fica limitado a
`def_risk_pct=1.25%`, além da escala normal de drawdown. O cap não aumenta um
risco base já inferior. A mesma decisão é aplicada no backtest (estado
cronológico), PaperRace por origem, SimEngine e MT5Engine.

A tabela SQLite `protection_states(engine, origin, trade_id, state_json,
updated_at)` guarda pico/vale, TP1, degraus, SL mecânico, timestamps e buckets
ATR. `PaperRace`, `SimEngine` e `MT5Engine` carregam este estado antes de
processar o tick; fechar a trade apaga a linha. Após restart, o throttle
monotónico de modify do MT5 é reiniciado, mas a proteção de preço permanece.

O shadow-paper está configurado para `auto_escada_3` e `auto_escada_5`, com
`origin="shadow"`, contas e gestão pendente separadas. Continua a correr sem
chamar o motor MT5, mesmo com a principal bloqueada, e não entra na curva
normal nem no relatório de divergências race.

## 11. Limitações conhecidas

- O random walk é apenas o fallback quando o feed externo não está disponível;
  o feed ao vivo e o backtest usam preços/candles reais quando os conseguem.
  Mesmo com candles reais, spread, slippage e latência do broker podem divergir
  dos custos modelados.
- A execução MT5 depende de um terminal aberto, AutoTrading ativo e conta
  `RETAIL_HEDGING`; o sistema bloqueia conta netting, mas não pode converter o
  tipo de conta pelo código.
- `realized_today`/limite diário seguem o dia civil UTC nas contas paper e o
  timestamp persistido do servidor na BD; confirma o fuso ao investigar um
  alerta histórico.
- A autenticação da dashboard é opcional por ambiente; em produção deve estar
  ligada e o HTTPS é responsabilidade do Caddy/reverse proxy descrito em
  `DEPLOY.md`.
- A dashboard continua vanilla JS/single-process; o SQLite tem lock interno,
  mas não é uma arquitetura para múltiplas instâncias da API a escreverem a
  mesma BD.
