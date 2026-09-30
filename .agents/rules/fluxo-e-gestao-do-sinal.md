---
trigger: model_decision
description: "O caminho de um sinal desde o listener do Telegram ate a ordem no MT5 e a gestao do grupo: janela de edicao, revive, guards de entrada, retcodes, BE+ adaptativo, kill diario por saldo da lane, e o journal de execucao com replay de evidencia. Ler antes de mexer na pipeline de sinais, em guards de entrada, no estado de uma trade, em manages, no BE+, no kill switch, ou no que aparece no audit de execucao."
---

## Fluxo de um sinal

1. Listener → `POST /api/signals` com `message_id` (e `historical=True` para
   o histórico). O listener reencaminha **tudo** (`SIGNALBOT_TG_FILTER=0` no
   `.env`) - quem decide é o backend.
2. `state.ingest()` → `_ingest_telegram()`:
   - **classify corre ANTES da dedup** - gestão/resumos históricos re-entram
     no handler (e `historical=True` nunca age em trades abertas);
   - **conversa do grupo** (sem keywords de sinal nem gestão, regex
     `_SIGNAL_HINT` em `state.py`, igual ao antigo filtro do listener) →
     registada na tabela `ignored_messages` e devolvida como `ignored` -
     nunca entra na pipeline nem na janela de edição;
   - **sinal completo executa já** (primeiro parse válido, sem esperar) -
     decisão baseada em dados do Item 12: na BD real (26/08), dos 305
     sinais que chegaram completos no primeiro parse, **0 foram editados
     depois (0.0%)**, abaixo do limiar de 5%; só sinais **incompletos**
     entram na janela de edição (`awaiting_edit`,
     20s) - o `_SIGNAL_HINT` inclui `buying|selling|comprando|vendendo` porque
     o grupo envia primeiro "I'm selling XAUUSD" e edita a mensagem com
     entrada/TP/SL; os detalhes (TP/SL/entry/target) só contam como sinal
     quando trazem um **número** a seguir - "TP1 4396" é sinal, mas "after
     TP1"/"as SL" (conversa) não; uma mensagem que foi auditada como
     conversa mas é editada para sinal limpa o registo de ignorada
     (`delete_ignored`) para não aparecer nas duas tabelas;
   - **revive**: um sinal que falhou (ex.: entrada incompleta sem TP/SL) é
     revivido se o grupo completar a edição dentro de 5 min - reabre a
     janela com o conteúdo novo em vez de ser ignorado (`action: revived`);
     o mesmo vale para um sinal **`rejected`** (guard de zona/cap, lote
     mínimo, risco global) re-editado com conteúdo VÁLIDO dentro de
     `rejected_revive_minutes` (default 10, env
     `SIGNALBOT_REJECTED_REVIVE_MINUTES`) - com alerta claro (kind
     `revived`) de que a entrada ANTERIOR foi recusada e o motivo gravado;
     o sinal guarda um **marcador durável do revive** (`revived_from`/
     `revived_reason`/`revived_at` em `signals`, migração automática): a
     tabela Sinais mostra o badge `🔄 de rejected` (com o motivo no tooltip)
     mesmo depois de o sinal reaberto executar/rejeitar - o operador vê à
     primeira vista que aquele sinal veio de uma rejeição;
     re-edições tardias (>X min) não reabrem nada mas avisam o operador;
     edições de sinais já executados/pendentes continuam a disparar alerta
     `risk` (janela passada = entrada possivelmente desatualizada);
   - depois da janela: risk checks + execução - só existe o caminho auto
     desde o Item 12 (o modo manual foi removido); sinais bloqueados pelas
     guards de zona/lote mínimo ficam `pending` para confirmação explícita
     do operador (`confirm`), nunca por aprovação geral.
3. Execução: `SimEngine` ou `MT5Engine` (`open_trade` com N pernas; `update`
   gere TP1→BE, SL, TPs individuais).
   - **`max_positions` = 5** (default; antes 3): a estratégia de 5 pernas
     precisa de até 5 entradas em simultâneo - o limite 3 rejeitava sinais
     com "máximo de posições abertas atingido (3)".
   - **`MT5Engine._send_open_orders`** já não crasha quando
     `symbol_info_tick` devolve None (símbolo fora do Market Watch) - o
     `ensure_symbol` é chamado ANTES de enviar ordens e o tick None falha só
     a perna (não repete a trade). O `_try_execute` também finaliza o sinal
     como `failed` em qualquer exceção de `open_trade` (antes ficava em
     `awaiting_edit` e o tick reabria a trade a cada segundo - duplicados).
   - **Retcode 10027 (AutoTrading OFF)**: `_send_open_orders` recolhe os
     retcodes reais de cada perna recusada; quando 10027 aparece, a trade
     fica `auto_trading_off=True` (transiente) e o `_try_execute` emite um
     alerta risk **imediato e distinto** ("⛔ AutoTrading DESLIGADO…") - não
     só o erro no log. Retcodes diferentes só registam o erro normal.
   - **SL de gestão de grupo com os três níveis + retry (08/09)**:
     `move_leg_sl` (kind "group": protect/move_sl/close_all BE) usa agora o
     MESMO caminho target → valid → applied do caminho híbrido (`_valid_sl`);
     um modify recusado (10016 - inclusive quando o broker aplica um
     stops_level que o `symbol_info` não reporta) ou clamped deixa o ALVO em
     fila persistida (`sl_move_pending`, por lane) e o tick re-tenta no
     nível válido até o alvo ficar aplicado - o incidente de 07/09 (protect
     recusado na lane 2, a perna rodou até ao SL completo de -38,58 €) não
     se repete. Órfãs (perna fechada/trade encerrada) são limpas no início
     e no fim de cada update; um apply em retry notifica `system`
     (`sl_move_applied`).
4. Gestão do grupo: `_handle_management()` age na trade aberta do mesmo
   símbolo (ou na mais recente).

## BE+ adaptativo e auditoria BE+ (14/09)

O buffer BE+ do `tp2_protect` é **adaptativo**: em vez dos 40 pips fixos, o tick
usa a média do spread observado do símbolo nos últimos 7 dias (`spread_observations`)
× 1.1, limitado a [10, 60] pips, com cache de 5 min - sem amostras cai no valor
estático da estratégia. `POST /api/config` com `be_buffer_adaptive=0` desliga e
fixa o estático. Os estudos podem reproduzi-lo com `be_buffer_adaptive=1` no
`GET /api/analysis`. A auditoria diária (`GET /api/operations/daily-audit`) expõe
um bloco `be_plus` (saídas protegidas com lucro/perda) e o resumo diário inclui a
linha "🛡️ BE+ do dia", com o contrafactual "BE clássico teria corrido até TP2"
calculado com velas (spot MT5 → GC=F Yahoo; sem velas, `checked=0`, nunca inventa).
Scripts one-off esgotados (`mine_management`, `sweep_atr`, `sweep_auto`,
`sweep_struggle`, `study_guard_calibration`, `study_pending_orders`) movidos para
`tools/archive/` - fora do pacote live, referências históricas nos docs intactas.

## Mixins state.py + vigia do drift do spread (14/09)

Continuação da extração de mixins: `IngestMixin` (`app/ingest_mixin.py` -
`ingest`, janela de edição, revive, `_try_execute`, confirm/reject/delete)
e `ManagementMixin` (`app/management_mixin.py` - `_handle_management`,
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
todas as lanes contra o saldo da principal - 07/09: -52,36 € da tp2 (10k,
-0,52%) somados a -1,11 € da principal = -5,17% de 1.033 € disparou o kill
global com as duas contas individualmente dentro do limite. Sim puro (sem
conta MT5 em nenhuma lane) mantém o comportamento legado (P&L total vs
saldo configurado); a lane 2 em dry-run usa a conta sintética do `.env`.
O risco.check por lane no `_try_execute` já era por saldo desde antes.

## Auditoria de mensagens ignoradas (dashboard → tab "🚫 Ignoradas")

- `GET /api/ignored` - últimas mensagens que o bot decidiu **não registar**
  (conversa do grupo: "Good morning", "+50 pips", links, etc.), com hora,
  origem e texto completo. Tabela `ignored_messages` (dedup por
  message_id+source, mantém as últimas 1000).
- O snapshot inclui `ignored_count` para o badge da tab atualizar ao vivo.
- O log do listener `[skip]` mostra o texto das mensagens ignoradas.

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
quando configurado, no Telegram - não altera execução. A promoção de
`auto_escada_3`/`auto_escada_5` fica bloqueada na UI e no backend até todos os
gates objetivos passarem. Isto é read-only para a execução: não usar o replay
para ligar a conta real.
