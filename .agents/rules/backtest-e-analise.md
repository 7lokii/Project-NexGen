---
trigger: model_decision
description: "Os endpoints de analise e backtest do Project NexGen e o modo defensivo: edge do grupo, compare A/B/C, walk-forward, protect causal, recounts contra candles, MFE, offset GC=F contra spot, simulacao mensal, e as garantias de risco defensivo e estado persistente. Ler antes de mexer em app/analysis.py, app/backtest.py, nos estudos, no MFE, no offset do feed, no alerta de regime, ou antes de promover, ajustar ou comparar estrategias com numeros."
---

## Backtest / análise (sem tab Edge - vive nas páginas de teste e no alerta de regime)

- `GET /api/analysis` - edge do grupo (variante nova, TP1..TP5); o alerta de
  regime usa-o por trás, já não tem tab própria na dashboard.
- `GET /api/analysis/compare` - A/B/C: nova (TP1..TP5) vs **nova + gestão**
  vs fechar tudo no TP2 (simula `protect`/`close_all`/`close_tp1` acoplados
  por fonte+tempo, com SL-first e fills sem look-ahead). A estratégia antiga
  (TP1/TP2 only) foi removida do compare.
- `GET /api/analysis/walkforward` - edge da gestão por janelas temporais
  (estabilidade do PF ao longo do tempo) - mostrado nas **páginas de teste**.
- `GET /api/analysis/protect_latency` - telemetria persistida de `protect` da
  híbrida (paper/MT5) ou replay fresco (`engine=backtest`): vencedor, pips e
  segundos, com distribuição.
- `GET /api/analysis/resolution` - compara M1 quando o Yahoo o conserva, explicita o fallback de resolução e mede a sensibilidade a atrasos 0/8/60/300s.- `GET /api/analysis/protect_causal` - replay read-only do histórico completo
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


- `GET /api/analysis/month` - **simulação**: replay dos sinais reais com
  gestão desde há N dias até hoje (ou `days=0` = **todo o histórico na BD**,
  desde o início do grupo), com a curva de equity acumulada (`run_month_sim`
  em analysis.py). As **páginas de teste** chamam-no com `balance=1000` (o
  saldo próprio da conta paper de cada estratégia - não o saldo da conta
  MT5); o saldo default é **1000** (o saldo próprio da conta paper da
  estratégia, via `testeSimQS()`, com seletor de risco/saldo para estudos).
  Mostra "se tivesses começado há 1 mês com X €, hoje terias Y €" +
  mini-curva - nunca abre trades.
- `GET /api/summaries` - resumos do grupo (ground truth).
- `GET /api/recounts` - recounts por trade do grupo (R2, read-only): cada
  "Signal #N: … +N pips" guardado em `trade_recounts` e comparado com os
  fills reais do mesmo dia (`_recount_fill_audit`) - entrada ≥20 pips,
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
  veredicto da sessão - o high/low do dia inteiro mistura sessões (um pico
  às 22:00 não valida um relato do sinal das 09:00); o dia inteiro fica em
  `day_plausible`/`day_*` para contexto, e o tooltip da tabela de recounts
  expõe dia + janela. Ferramentas read-only:
  `app/sweep_recount_candles.py` (plausibilidade de todos os recounts),
  `app/backfill_recounts.py` (re-analisa `ignored_messages`/`signals`
  falhadas à procura de `Signal #N`; `--apply` grava em `trade_recounts`),
  `app/study_recount_mfe.py` (recounts vs MFE real dos candles - valida a
  convenção de win do grupo R3 contra a excursão real),
  `app/study_recount_implausible.py` (implausíveis vs fills reais do bot:
  negociou ou não, com causa provável + veredicto agregado: erro de
  relato / erro de execução / sem dados, top-5 de cada),
  `app/study_recount_baseline.py` (baseline de falsos positivos do
  candle-check sobre os sinais reais - atenção: o feed Yahoo serve GC=F
  futuros vs spot do grupo, a taxa alta não prova relato falso) e
  `app/study_signal_delay.py` (atraso sinal→mercado: quando o GC=F toca a
  entrada vs a hora do Telegram - mediana 3.7h nos 30d reais, a janela
  ±3h é justa face ao feed; `study_recount_baseline.py --window-hours 6`
  re-mede o baseline com a janela alargada: 66.3% → 62.4%; com `--apply`
  grava a janela recomendada por símbolo em `recount_window_hours_by_symbol`
  - na BD real a recomendação automática de 12h é contaminada pelo feed
  GC=F, p75 29h ≫ mediana 3.7h, por isso ficou no default 3h: com o spot
  MT5 o atraso medido é mediana 106 min e 100% das entradas tocadas ≤3h
  (101/101) - a janela 3h cobre tudo). A janela do
  candle-check é adaptativa por símbolo (`_recount_window_hours`, config
  `recount_window_hours_by_symbol` → fallback `recount_window_hours` 3h,
  limitada a [1, 24]h). A FONTE do candle-check é o spot MT5 quando o
  terminal está ligado (`_recount_candles`, cache 5 min, fallback GC=F
  Yahoo, símbolos do mapeamento MT5: XAUUSD/XAGUSD/forex) - o veredicto é
  COMBINADO (`spot_plausible`/`futures_plausible`/
  `feed_divergence`/`verdict_kind`): implausível no GC=F mas plausível no
  spot marca "⚠️ provável divergência de feed" no /auditoria/dashboard,
  não relato suspeito; o GC=F é comparado SEM correção (o offset
  spot↔futuros foi eliminado - `_gold_futures_offset` ficou só no estudo
  read-only `study_recount_baseline --apply-offset`).
  `app/study_recount_mt5_feed.py` corre o MESMO
  check com candles M15 de XAUUSD spot do terminal vs o GC=F (`--signals`
  recalibra o baseline com as entradas reais: 0.0% fora da janela no spot
  vs 66.3% no GC=F - os implausíveis eram TODOS divergência do feed); o
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
  flips (data, recount, de → para, offset) - `/api/recounts/batches` e bloco
  no /auditoria mostram a evolução das correções do offset. O alerta AO VIVO de relato suspeito só dispara com
  CONFIRMAÇÃO (spot E GC=F a discordar, ou sem GC=F); o /auditoria separa
  "Implaus. confirmados" de "Divergência de feed" (baseline spot 0%). Um
  implausível AO VIVO com o journal ligado
  grava o evento `recount_implausible` em `execution_events` (evidência
  passiva para o replay, com o veredicto combinado no payload); o
  fill-audit (`GET /api/analysis/fill-audit`)
  devolve `recount_evidence` com esses eventos (real = relato, replay =
  candles, `bot_traded` e `miss_reason` - listener reiniciado ±15 min
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
  fills e devolve 0 sem amostras - o valor aplicado fica exposto por recount
  (`futures_shift`/badge "offset N pips"). Com ele um recount que era
  `feed_divergence` flips a `plausible` (o prémio do futuro, não relato
  suspeito); o lote conta esses flips como `resolved`.
- **Decidir o cap autónomo com dados reais**: mudar `entry_guard_auto_cap`
  via `/api/config` grava `cap_applied_at`; o estudo read-only
  `app/study_cap_effect.py` usa-o como `--since` (sem ele assume hoje 00:00)
  e o tick (`_check_cap_reminder`) lembra no feed uma vez por dia quando o
  cap está aplicado há >`cap_reminder_days` (7) sem fills pós-cap, pedindo
  para correr `study_cap_effect --days 30` e decidir se o cap fica/aperta/
  reverte - nunca muda o cap sozinho.
- **MFE win rate (R3)**: o backtest mede `mfe_pips` (excursão máxima
  favorável) por trade e o agregado expõe `mfe_win_rate` - wins na
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
  e 100000 para forex - antes usavam 100000 para tudo, o que inflacionava o
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
- **Item 2 - sweep defensivo da `auto_escada_5`**: repetir com
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
SL/ invalid") - o histórico completo tem **579 sinais reais de XAUUSD**
(abr→hoje). No período atual (30 dias, formato novo): PF nova 1.1,
nova+gestão **2.92**, fechar no TP2 0.82. O período antigo (abr–jul) era
perdedor no backtest (PF < 1) - o edge da gestão só aparece no formato
recente. Drawdown > 100% possível porque o sim usa risco % fixo sobre o
saldo inicial (sem parar em falência).

- **Auditoria histórica de 19/08:** a amostra é desigual (506 sinais antigos
  contra 73 desde 28/07). Repetições sem spread/slippage/comissão e sem o
  atraso de 60s mantiveram o período antigo negativo; não atribuir esse regime
  perdedor apenas aos custos de execução.
- Quando `protect_causal` marca eventos como `deferred`/`not eligible`, “0
  harmful” significa ausência de evidência de dano nesse replay, não prova de
  benefício causal.

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
