---
trigger: model_decision
description: "O registo central de estrategias do Project NexGen em app/strategies.py: as 7 visiveis no menu, as ocultas, as apagadas, os parametros (max_legs, manage_kinds, exit_at_tp, lots_override), o estudo da consolidação, a confirmacao na auto_escada_3, a auto-gestao, a config strategy, o kill diario no backtest, o risco dinamico e o alerta de regime. Ler antes de adicionar, remover ou alterar uma estrategia, mexer em app/strategies.py, no menu da dashboard, em manage_kinds, no risco, ou antes de escolher qual estrategia promover."
---

# Estratégias (registo central, parâmetros e risco)

## Estratégias (registo central + páginas de teste + paper trading forward)

- **`app/strategies.py`** - registo central: cada estratégia define `max_legs`,
  `manage_kinds` (quais kinds de gestão honra - `manage=True` legacy = todos),
  `exit_at_tp` e `lots_override`. `MAIN_STRATEGY_ID = "nova_gestao"` (a melhor
  segundo os dados) é a que a conta principal executa. **Remoção completa
  (01/09)**: `tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2`, `auto_protect_5`
  e `auto_escada_3_struggle` (R4) foram APAGADAS do registo - já não são
  backtestáveis (o histórico do porquê e PF/DD está no CHANGELOG de 01/09);
  `get_strategy()` levanta ValueError para ids mortos (um estudo com uma
  estratégia apagada falha em voz alta, nunca corre silenciosamente com a
  principal; a config/snapshot trata um id morto como órfão com badge -
  `_strategy_params`). Ficam **7 visíveis no menu/dropdown**: `nova_gestao`,
  `nova_gestao_fallback`, `tp2_protect`, `auto_3`, `auto_escada_3`,
  `auto_escada_3_hibrida` e `auto_escada_5` (as 5 primeiras operacionais; a
  híbrida é experimental: combina a auto-gestão com `protect` monotónico do
  grupo e não altera a principal; `auto_3`/`auto_escada_5` são referências
  de backtest com página de teste própria). `MENU_HIDDEN_STRATEGIES =
  {nova, auto_5}` (ocultas do menu; a `nova` continua a abrir papel
  **silenciosamente** - sem card na corrida nem feed - só para alimentar o
  contrafactual "sem gestão" do histórico de trades (`counterfactual_pnl`);
  a `auto_5` é a comparação 5 pernas com/sem trail, backtest-only).
  **Corrida paper focada (01/09)**: `paper_strategy_ids()` abre papel
  NORMAL só nas 5 em validação + a `nova` contrafactual - as referências
  `auto_3`/`auto_escada_5`/`auto_5` já não abrem corrida (backtest/compare/
  shadow continuam; o shadow tem a própria lista `shadow_paper_strategies`
  e a `auto_escada_5` mantém a evidência do gate forward isolada com
  `origin=shadow`). Os dados paper órfãos
  das removidas ficam na BD sem leitores (nunca se apagam dados). O filtro
  `manage_kinds` aplica-se à conta
  real (quando promovida), ao backtest (`simulate_signal_managed`) e à
  corrida paper. Adicionar estratégia nova = 1 entrada no dicionário + 1
  item no menu (`common.js` STRATEGY_MENU fallback + `TERM_ORDER`).
- **Estudo da consolidação (`app/study_consolidation.py`)** - filtro de
  entrada (sessão Londres/NY + distância entrada→TP2 ≥ 5 pips + ATR p20) e
  modo defensivo (PF das últimas 10 trades < 1.0 → trail 1.5 + time stop 60
  min, SEM cortar pernas) nas 3 janelas. Veredicto (19/08): o filtro de
  entrada NÃO tem edge (rejeita quase só no período antigo onde tudo perde,
  a sessão não filtra nada no atual, ATR = ruído; a sensibilidade só-ATR
  rejeita 0) - não implementado; o **modo defensivo foi IMPLEMENTADO** nas
  `auto_escada_3/5` (parâmetros `def_window`/`def_trail`/`def_time_stop` no
  registo) - espelho nos 3 motores sem look-ahead (backtest `def_state`
  partilhado; paper `paper_closed_recent`; MT5 real `recent_closed_pnls`),
  com validação na corrida paper antes da Fase 3.
- **Item 3 - confirmação na `auto_escada_3`**: o sweep binário
  `--item3-sweep` corre os mesmos sinais/candles/custos com apenas
  `confirm_filter` alternado; o gate exige DD antiga estritamente menor e
  perda de PF atual ≤10% relativa. O resultado de 19/08 passou (1.27→1.26;
  312.9%→278.3%), por isso o default da estratégia é agora `True` - isto
  não muda a principal nem promove a estratégia para MT5.
- **Auto-gestão (docs/ESTRATEGIAS.md, Parte V - fonte única de estratégias;
  o antigo `docs/SPEC-auto-gestao.md` foi consolidado e apagado em 19/08)**
  - variantes que substituem a gestão do grupo
  por regras mecânicas: **escada de proteção** (quando o TP_k bate, o SL das
  pernas seguintes sobe para o nível do TP_k), **trailing** (`trail_factor` =
  múltiplo da distância entrada→TP2 que o SL pode recuar do pico; 2.0 ≈ 8
  pips em gold), **filtro de confirmação** (perna intermédia leva SL →
  longínquas fecham), **time stop** (pernas abertas N min após TP1 → fecham
  ao mercado), **modo defensivo** (PF(10) < 1.0 → trail 1.5 + time stop 60,
  sem cortar pernas) e **regra "struggle"** (R4, `struggle_ae_pips`/
  `struggle_fraction`): antes do TP1, se o preço andou contra a entrada por
  N pips e volta à entrada, fecha uma fração das pernas a BE e protege as
  restantes na entrada - conselho do admin "take 50% / make risk-free" (a
  candidata `auto_escada_3_struggle` foi REMOVIDA do registo a 01/09: o
  backtest de 20/08 não encontrou edge - histórico no CHANGELOG; o mecanismo
  fica nos motores para candidatas futuras). Ferramentas: `app/mine_management.py` (minera a gestão real
  do grupo para calibrar parâmetros) e  `app/sweep_auto.py` (sweep das
  variantes no período atual + antigo out-of-sample). `app/study_consolidation.py
  --item2-sweep` faz também a grelha reproduzível do modo defensivo da
  `auto_escada_5`; resultado do Item 2: DD antiga permanece ~85.5–87.4%,
  portanto não há promoção de default. Resultado do sweep

  (18/08): a escada/confirm/time stop NÃO têm edge sozinhos; o **trailing é
  o mecanismo** (trail_factor 2.0 ≈ 8 pips em gold é o valor honesto - 0.5 é
  top-picking irrealista). **Fase 2 (paper)**: a `auto_escada_3` (PF 3.11
  atual / 1.41 antigo vs referência 3.28 / 0.63) saiu de `HIDDEN` - página
  de teste + **corrida paper** com a auto-gestão portada para o tick do
  `PaperRace` (mesma sequência do backtest, sem look-ahead) e a `nova_gestao`
  como referência paralela; o **divergence report** (`⚖️` no feed, dedup por
  sinal) compara a saída da auto vs a referência no mesmo sinal (incluindo
  "o grupo saiu, a auto continua"); `tp2_protect` também saiu (só `protect`
  do grupo, validação do estudo). O resto (`auto_2/3/5`, `auto_escada_5`)
  continua `HIDDEN` (só backtest/compare). Sensibilidade do trail 1.5–2.5:
  plateau monotónico (PF 1.97–4.69 atual, 1.28–1.57 antigo - sem pico
  isolado, logo sem sobreajuste); 2.0 congelado como meio do plateau.
  **Fase 3 (MT5 demo)**: protocolo pronto no ESTRATEGIAS.md (Parte X) -
  pré-requisitos N≥20, janela de validação de
  10 trades, rollback. O **código da Fase 3 está implementado e desligado**:
  escada/trailing/confirmação/time stop ligados ao `_update_real` (só correm
  quando a estratégia ativa os define - a `nova_gestao` não tem nenhum),
  throttling de modify (≥1 pip, 1×/min), três níveis de SL
  (  `_valid_sl` - um modify recusado 10016 NÃO grava o SL na BD;
  `_modify_sl` devolve o retcode), **trailing adaptativo por ATR** com a
  MESMA fórmula `max(trail_factor×dist, atr_k×ATR)` nos três motores
  (`MT5Provider.copy_rates` M1/M5 + `_atr`; `_candle_atr` no backtest;
  `PaperRace._paper_atr` na corrida - baldes de minuto, sem look-ahead;
  **rejeitado pelo sweep** (`app/sweep_atr.py`: atr_k 1–5 × period 7/14/30
  em 3 janelas - o ATR é estritamente pior que o trail fixo 2.0; o ATR do
  ouro alarga o trail e blunts o fecho do lucro; fica desligado - ver
  ESTRATEGIAS.md Parte X (protocolo Fase 3); o `--trail` confirmou o plateau
  monotónico 1.6–2.4 e o 2.0
  interior), a **auditoria dos três
  níveis de SL** (`autosl:<trade_id>` na config, target→valid→applied +
  retcode, `GET /api/analysis/auto_sl_log` - o que a janela de validação
  audita; um modify recusado 10016/clamped dispara um aviso `risk` no
  feed/Telegram via evento `auto_sl_warn`) e um **teste E2E do caminho
  real com broker fake** (`tests/test_mt5.py`: TP1 por deal → escada →
  trailing → time stop, retcodes 10009/10016 no log + auditoria). A principal tem um **painel
  "🚀 Fase 3 - pronto a arrancar"** (checklist dos gates: N≥20 · **3/3**
  (PF≥1.5 · P&L≥50% · DD≤125% - gate estrito, sem comité 2 de 3),
  com os counts do divergence paper↔MT5, real vs backtest, DD e a
  auditoria de SL; a janela de 10 trades avalia-se na validação). O painel mostra
  também o **ETA dos 20 sinais** (`snapshot()["fase3_eta"]`: ritmo de
  sinais/dia = trades de papel abertas da auto_escada_3 nos últimos 30
  dias, data = `now + (20−N)/ritmo`; `done` quando N≥20) e o **veredicto da
  janela de validação** (`fase3_verdict`): `state._check_fase3_validation`
  corre no tick quando a auto está promovida e 10 trades reais fecharam
  desde `promoted_at` - PF real da janela + mesma saída que o papel
  (`same_exit_n`, o ≥ 9/10 do doc) + divergências estruturais
  (paper↔MT5 com saída diferente/papel aberto) + posições sem SL →
  **rollback automático para a nova_gestao** (PF<1 · ≥2 divergências · sem
  SL · ordens duplicadas · retcodes recusados ≠ 10009/0 na auditoria
  `autosl:`) ou **manutenção**, notificado no feed/Telegram, uma vez por janela
  (nova promoção grava snapshot novo e reabre; o **histórico de veredictos**
  fica em `fase3_verdicts`, nunca se apaga - bloco "📚 Histórico" no painel).
  O ETA também compara o ritmo com o mês anterior (`pace_prev`/
  `pace_change_pct`) e alerta no feed quando a janela estimar **> 6 semanas**
  ou o ritmo mudar **≥ 50%** (cooldown 24h), e o **resumo diário** ganha a
  linha 🚀 da auto MT5 (N real, P&L real da janela) durante a Fase 3
  (ESTRATEGIAS.md Parte X.1). O **SPEC v3 (conta real)** é um rascunho de
  gates na Parte X.2 - nunca promover a conta real sem instrução explícita
  do utilizador.
  A página principal tem um **painel Fase 2** (saldo/PF da auto vs a
  referência, sinais resolvidos e progresso para os critérios: N ≥ 20 ·
  PF ≥ 1.5 · P&L ≥ 50% · DD ≤ 125% da referência) e a tab Relatório uma
  **tabela de divergências** (`GET /api/paper/divergences` - auto vs
  referência por sinal, razão/P&L lado a lado + motivo). As páginas de
  teste da Fase 2 têm botão **⬇️ CSV** (trades de papel da auto + referência).
  Estudo tp2 (30d, formato atual): tp2 puro PF 0.86/−151 €; **+ protect
  PF 2.65/+1 721 €** (o protect recupera ~86% do caminho até à gestão
  completa PF 2.93/+2 010 €); nova_gestao PF 3.22/+2 309 € - sair cedo só
  vale com a proteção do grupo, e a gestão completa continua a ganhar.
- **Config `strategy`** (`GET/POST /api/config`, validada contra o registo) -
  a estratégia ativa da conta principal. `_try_execute` passa `max_legs` ao
  motor e o tick passa `exit_at_tp` (SimEngine e MT5Engine suportam ambos;
  `lots_override` já existia). Consolidação 19/08:
  `HIDDEN_STRATEGIES`/`MENU_HIDDEN_STRATEGIES` já não estão vazios (ver
  secção `app/strategies.py` acima) - 7 visíveis no menu, 2 ocultas do menu
  mas em papel, 4 só no backtest.
