# Changelog — Project NexGen

Registo das alterações ao programa. **Regra**: sempre que algo for
implementado/mudado, acrescenta uma entrada aqui (data + o que mudou +
nº de testes). As docs (AGENTS.md, TECHNICAL.md, PUBLIC.md) devem ficar a
par no mesmo turno.

Formato: `### YYYY-MM-DD — título` · bullets com o quê/porquê.

---

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
