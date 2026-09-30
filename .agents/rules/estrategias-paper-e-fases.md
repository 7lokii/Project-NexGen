---
trigger: model_decision
description: "A dashboard e as fases de validacao do Project NexGen: layout e tema, promover uma estrategia, as paginas de teste com os dois horizontes, a corrida paper com contas virtuais por estrategia, o shadow-paper isolado, a comparacao real contra backtest, a Fase 3 em MT5 demo e o fase3_target_log. Ler antes de mexer em app/static/**, no layout, nas paginas de teste, no PaperRace, no shadow-paper, na promocao de estrategia, no painel da Fase 3, ou antes de expor a auto_escada_3 a MT5 real."
---

# Estratégias (dashboard, paper/shadow e fases)

- **Layout** (dashboard): tema **claro profissional** (sem cyberpunk/neon;
  sem animações decorativas - o boot sequence e os sons foram removidos) com
  **tema escuro** opcional - botão ☾/☀ na barra superior (todas as páginas,
  `common.js`), persistido em `localStorage.nx_theme`, aplicado antes do
  paint por um snippet no `<head>` de cada página (default = preferência do
  SO; o login respeita o mesmo). Sem escolha explícita o tema **segue o SO
  ao vivo** (`prefers-color-scheme` com listener `change`); uma escolha no
  botão fica persistida e o SO deixa de interferir - o arranque do chrome
  nunca grava o tema (gravar marcaria escolha explícita e mataria o follow). O dark é uma camada `[data-theme=dark]` de
  variáveis - nenhum hex hardcoded fora das variáveis; os gráficos canvas
  usam `chartPalette()` (`common.js`) para grelha/eixo/fundo por tema. Com
  todo o chrome numa **barra superior fixa** (`.topbar`, renderizada pelo
  `common.js` num placeholder `<div id="appChrome">`): linha 1 = marca +
  navegação horizontal (PRINCIPAL · ▚ ESTRATÉGIAS dropdown · DIAGNÓSTICO ·
  AUDITORIA · HELP; setas ↑↓ + Enter + rato) + badges (modo/estratégia/MT5/
  execução) + utilizadores + relógio + **Kill switch** + **Sair**; a faixa
  de preços (GOLD..NZDUSD) foi **removida** em 09/2026 por decisão do
  utilizador (perf - o topbar fica estático; o preço ao vivo vive no
  status/market badge "preços reais"). O conteúdo usa a largura
  total (máx. 1440px) - as colunas laterais (`--term-w`/`--term-r`) e os
  overlays móveis ☰/▦ foram removidos; ≤960px o ☰ abre a navegação em
  dropdown. Na principal a ordem é status → KPIs → tabs → conteúdo, com os
  painéis analíticos (corrida/promoção/Fase 2/3/narrativa/shadow) depois do
  conteúdo (CSS `order`). **Render por diffs**: só a tab ativa reconstrói e
  só quando o conteúdo mudou (`contentChanged` - cache de assinatura em
  `common.js`); o feed reconstrói apenas em mudança; o snapshot envia 30
  notificações (antes 50). As **7 estratégias visíveis** vivem no **dropdown
  ▚ ESTRATÉGIAS**
  (fechado por defeito em cada página; fecha ao clicar fora;
  a navegação por teclado abre-o sozinha ao chegar a um item lá dentro) -
  cada uma com página de teste própria `/teste?s=<id>` (saldo simulado de 1
  mês a começar em 1000 € + mini-curva + comparação + corrida paper ao
  vivo; o **fluxo de exemplo de uma trade real** - `GET /api/analysis/trace`
  - usa `nova` por defeito). Menu atual (removidas a 01/09):
  `nova_gestao`, `nova_gestao_fallback`, `tp2_protect`, `auto_3`,
  `auto_escada_3`, `auto_escada_3_hibrida`, `auto_escada_5`; o shadow-paper
  observa `auto_escada_3` e `auto_escada_5` por defeito; `nova`/`auto_5`
  continuam em papel (ocultas do menu - contrafactual/comparação). `tp2`,
  `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5` e a candidata R4
  `auto_escada_3_struggle` foram APAGADAS do registo (ver secção
  Estratégias acima + CHANGELOG 01/09).
  A página **HELP** explica cada estratégia de forma
  interativa (cards expansíveis + números reais de 30 dias) + glossário. A
  página principal usa a melhor (a estratégia ativa na config - hoje
  `nova_gestao`) e o menu marca com **"✓ melhor"** a vencedora do A/B/C do
  histórico completo (`compare?days=0` → `winner`).
- **Kill diário no backtest**: `run_analysis`/`compare`/`month` modelam o
  kill switch da produção - `daily_stop_pct` (None = `daily_loss_limit_pct`
  da config; 0 = off) - as trades depois do limite do dia ficam
  `outcome=skip`/pnl 0 e contam em `daily_stop_skipped`. Os números da
  dashboard refletem o que a produção realmente faz.
- **Estudos de risco**: `/api/analysis`, `/api/analysis/compare` e
  `/api/analysis/month` aceitam `risk_pct` e `balance` opcionais (0 =
  config/saldo atual) - as páginas de teste têm os seletores "Risco do
  sim"/saldo (persistidos em `edge_risk`/`edge_bal`). ⚠️ Com saldo ~1000 € o piso de
  lote mínimo (0.01) domina o dimensionamento - riscos ≤ ~4% dão os mesmos
  lotes; para estudar risco usa um saldo maior (ex.: 10 000).
- **Risco dinâmico**: `state._effective_risk_pct()` - o `risk_pct` base
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
  snapshots gravados a cada ~60s em `paper_balance_history` - **todas** as
  estratégias visíveis (incluindo a principal) usam a sua conta virtual
  própria de
  1000 €, para comparar em igualdade de circunstâncias; além das **7
  contas virtuais visíveis** (nova/auto_5 continuam em papel mas fora da
  curva; as 4 removidas já não abrem papel), a legenda
  mostra uma **linha de referência "⚡ MT5 real · conta
  real"** com o saldo real gravado no tick sob a estratégia reservada
  `__real__` (nunca entra na comparação - é o dinheiro a sério ao lado).
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
  volta à principal) - não troca sozinho. A tab "🔧 Diagnóstico" deixou de
  aparecer na barra de
  tabs da principal (tem página própria no menu). **Layout responsivo (zoom
  do navegador)**: a navegação vive numa **coluna fixa à esquerda**
  (`--term-w`) - o menu (PRINCIPAL, páginas de teste, Diagnóstico, HELP) e,
  **em baixo do menu (após HELP)**, o terminal **`termSide`** com a lista
  vertical de preços (**variação % do dia**, `market.changes`, do
  `chartPreviousClose` do Yahoo - verde/vermelho) e botão **‹** para
  colapsar/expandir (persistido em localStorage - colapsado fica só o
  cabeçalho ▚ MARKET + ›). O hint de teclado (↑↓ + ENTER) fica entre o
  HELP e o MARKET. No **header** (à direita da marca/subtítulo) ficam os
  badges de estado (`hdr-badges`): modo auto, estratégia ativa
  (stack nome + parâmetros), MT5 real (conta · saldo) e execução real/
  dry-run (mais o `killBadge` PAUSADO quando o kill switch está ligado).
  À **direita** há um segundo terminal fixo **`termRight`** (`--term-r`):
  utilizadores online + relógio, **kill switch** e **Sair**, com o próprio
  botão ‹ de colapsar (`body.term-r-collapsed` encolhe para 38px). O
  header tem ☰ + marca + badges + ▦. Num ecrã ≥961px o conteúdo afasta-se
  dos dois terminais (`--term-w`/`--term-r` via variáveis CSS) - nunca é
  coberto em qualquer zoom; a largura/tipografia dos terminais encolhe em
  961–1120px e o menu passa a scrollar em alturas < 640px (o flex
  centrado cortava itens). ≤960px: o menu (com o MARKET no fundo) abre em
  overlay (☰) e o terminal direito em overlay (▦), tabelas e tabs fazem
  scroll horizontal interno (`overflow-x`), sem overflow da página em
  nenhum zoom. Nas páginas de teste e no Diagnóstico o body ganha
  `no-chrome` (stats/controlos/bloco de risco escondidos - só header +
  statusbar + terminal esquerdo ficam).
- **Promover uma estratégia** (`POST /api/config` `{strategy}`) regista uma
  **notificação no feed** ("♟ Estratégia promovida a principal: … - a
  execução real passa a usar: …") e o badge do header passa a mostrar, além
  do nome, os **parâmetros reais de execução** (`max_legs`/`exit_at_tp`/lote
  fixo/gestão) - vêm de `config.strategy_params` no snapshot
  (`state._strategy_params`, registo em `app/strategies.py`).
- **Cada página de teste mostra dois horizontes**: a simulação do último mês
  (seletor 7/30/90/**tudo** - persistido na config `teste_days`) E o bloco
  "📜 Desde o início do grupo" (`days=0` = TODO o histórico na BD, sem
  filtro de data) com veredicto, stats e curva próprios - para cada
  estratégia. O bloco tem um **seletor de período** (config `all_period`):
  todo o histórico / só o formato atual
  (`since=2026-07-28`, data da 1ª mensagem do formato atual) / só o mês
  corrente - mais o **P&L por mês** (barras) e o **P&L por perna**
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
  (correlação pelo signal_id da trade real alvo - as contas "+ gestão" já
  não se comportam como as sem gestão). A corrida é paralela à conta real -
  nunca a toca.
  Snapshot expõe `paper.accounts` (saldo/win rate/PF por estratégia +
  `daily_stop_skipped`) e `GET /api/paper/trades` o histórico de trades por
  estratégia. **Kill diário na corrida paper**: `open_for_signal` recebe o
  `daily_stop_pct` da config e, quando o P&L realizado do dia da conta
  virtual ≤ −saldo×limite/100, o sinal NÃO abre em papel nessa estratégia
  (`paper_realized_today`; espelho da produção
  `risk.engine.daily_loss_exceeded`/`apply_daily_stop` do backtest) - as
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
  estratégia principal - Δ entrada (preço real vs o fill do sim: entrada ±
  spread/2 ± slippage), Δ saída (fecho ponderado das pernas vs o nível da
  razão TP_k/SL), atraso sinal→abertura (o sim assume 60s) e P&L de cada
  lado. Tabela "⚡ Real vs backtest" na tab Relatório (vazia até a demo
  fechar trades).
- **Auto paper vs Auto MT5 (Fase 3 - ESTRATEGIAS.md Parte X.1)**: `GET /api/analysis/paper_vs_real_auto`
  (o painel Fase 3 usa o **payload único** `GET /api/analysis/fase3_status` -
  ETA+curva, snapshot, veredictos+histórico, `fase3_target_log` (reduções
  de alvo com motivo), divergences, real vs backtest, DD e auditoria SL num
  só pedido em vez de 4 chamadas)
- **`fase3_target_log`**: cada alteração ao alvo da janela (`fase3_target`,
  default 20; o feed baixa para 15 quando o ETA estimar > 6 semanas)
  regista `{at, from, to, reason}` - `POST /api/config` aceita
  `fase3_target_reason`; bloco "🎯 Alterações de alvo" no histórico do
  painel Fase 3.
  compara cada trade REAL fechada de uma auto (quando promovida a principal
  na Fase 3) com a trade de PAPEL da mesma estratégia no mesmo sinal -
  mesma decisão de saída, Δ preço de fecho (pips), Δ timing (min) e P&L de
  cada lado. Tabela "🔄 Auto paper vs Auto MT5 (Fase 3)" na tab Relatório
  (vazia até a Fase 3 arrancar).
