# Estratégias do Project NexGen — documento de avaliação

> **Objetivo deste documento**: ser a fonte ÚNICA para avaliar as estratégias
> do bot, sem precisar de ler mais nenhum documento do projeto. Explica o
> contexto (o que o bot faz), os conceitos (TP/SL/BE/PF/DD…), as **12
> estratégias do registo central** (mecânica completa + resultados), como os números
> foram obtidos (metodologia) e como julgar se uma estratégia é boa.
>
> Estado: **2026-08-19** · suite de testes indicada em `AGENTS.md` · conta
> **demo** (nunca dinheiro real).

---

## Parte I — Contexto (o que é o sistema)

O Project NexGen é um **bot de copy-trading semi-automático**: lê sinais de
um grupo de Telegram chamado **Golden Moves**, espera ~8 segundos (janela de
edição — o grupo edita a mensagem para completar o sinal), e executa a trade
numa conta **demo** do MetaTrader 5 com uma **estratégia de N pernas**
(TP1..TP5 + SL + breakeven). Há uma dashboard web que mostra tudo ao vivo.

**Formato de um sinal** (exemplo real do grupo):

```
I'm buying XAUUSD
4400-4396
TP1 4402
TP2 4404
TP3 4406
TP4 4408
TP5 4410
SL 4392
```

Significado: comprar ouro na zona **4400–4396** (o bot entra a ~4400), TP1 em
4402, TP2 em 4404… TP5 em 4410, e **SL em 4392** (perde se cair abaixo).
Cada TP fecha **uma perna** da trade; o risco total da trade é dividido pelas
pernas.

**Gestão do grupo**: além dos sinais, o Golden Moves envia **mensagens de
gestão ao vivo** ("TP1 HIT", "Close TP1 here", "we're out", "move SL to
4331", "protect…"). O bot classifica essas mensagens e aplica-as às trades
abertas — **se a estratégia ativa honrar esse tipo de gestão** (ver Parte IV).

**Conta demo**: tudo o que está neste documento corre em dinheiro virtual
(conta demo MT5). Nunca se promove a conta real sem decisão explícita do
utilizador (SPEC v3, rascunho).

---

## Operação segura (antes de promover uma auto)

A execução multi-perna só é compatível com uma conta MT5 **RETAIL_HEDGING**
(`margin_mode=2`). Em modo real o provider verifica o tipo no arranque e
bloqueia netting/desconhecido; o painel mostra `hedged=false` e o motivo em
vez de enviar ordens que poderiam fundir as pernas. A conta principal continua
uma **demo** e a promoção para uma conta real exige instrução explícita.

A VPS também limita `server.log`/`listener.log`/erros a 10 MiB por ficheiro e
5 cópias por defeito (`SIGNALBOT_LOG_MAX_BYTES`/
`SIGNALBOT_LOG_BACKUP_COUNT`), verifica o disco diariamente (10 GiB livres por
defeito) e testa mensalmente o backup mais recente com:

```text
.venv/Scripts/python backup_db.py --restore-check
```

Estes checks são operacionais: não alteram parâmetros de estratégia, não
abrem trades e não contam como amostra forward da Fase 2/3.

### Saúde operacional e validação contínua (Itens 10 e 17)

O painel Saúde operacional do Relatório e `GET /api/operations?days=N` mostram
as exceções persistidas, o spread médio mensal por símbolo, reconexões MT5 por
semana e modifies de SL recusados por retcode. O `MT5Engine` só considera um
novo SL aplicado depois de 10009/0; uma falha fica auditada. A amostra deve
acumular pelo menos duas semanas antes de se tirar uma conclusão de tendência.

O workflow `.github/workflows/ci.yml` corre a suite completa em push/PR e,
em seguida, `tests/test_docs.py`. Um dispatch manual com `failure_probe=true`
exercita um pytest deliberadamente falhado para confirmar o comportamento de
bloqueio; não muda a configuração da conta demo nem promove estratégias.

## Parte II — Conceitos base (necessários para avaliar)

| Conceito | Explicação |
|---|---|
| **Sinal** | Mensagem do grupo com entrada, SL e TP1..TP5 |
| **Perna (leg)** | Uma parte da trade: N pernas = N entradas com o mesmo SL mas TP diferente (leg 1 → TP1, leg 2 → TP2…) |
| **TP (Take Profit)** | Nível onde a perna fecha com lucro |
| **SL (Stop Loss)** | Nível onde a perda é cortada |
| **SL-first** | Regra do sim/execução: se no mesmo instante/candle o SL e um TP forem tocados, conta o SL (conservador) |
| **BE (breakeven)** | Quando o TP1 bate, o SL das pernas restantes sobe para a ENTRADA — a trade deixa de poder dar prejuízo (no pior caso sai a 0) |
| **Pips** | Unidade de preço configurada por símbolo: no fallback XAUUSD usa 0.01, XAGUSD 0.001 e forex 0.0001; o broker MT5 pode fornecer os valores reais |
| **Lote** | Tamanho da posição (0.01 = mínimo); o risco % decide os lotes |
| **Risco por trade** | % da conta em jogo se o SL bater (default **2.5%**, antes da escala dinâmica; os lotes são calculados por símbolo e divididos pelas pernas) |
| **PF (Profit Factor)** | Lucro bruto das trades vencedoras ÷ perda bruta das perdedoras. **PF ≥ 1.5 = bom; PF ≥ 2 = muito bom; PF < 1 = perde dinheiro** |
| **WR (Win Rate)** | % de trades que fecharam com lucro |
| **P&L** | Lucro/perda total em € |
| **DD (Drawdown)** | Maior queda pico→vale do saldo, em % (ex.: DD 25% = a conta chegou a descer 25% do pico) |
| **N** | Nº de sinais/trades resolvidos numa amostra |
| **Backtest** | Replay dos sinais REAIS do grupo sobre preços históricos (velas reais), com as mesmas regras da produção |
| **Corrida paper** | Cada estratégia tem uma conta virtual de 1000 €; quando um sinal real executa na principal, a MESMA trade abre em papel em cada estratégia — vês ao vivo quem está a ganhar |
| **Look-ahead** | Erro de simulação: usar informação do futuro (ex.: o fecho da vela que ainda está a decorrer). Todas as regras do projeto são **sem look-ahead** |
| **Out-of-sample** | Validar numa janela temporal diferente da que calibrou os parâmetros (teste de robustez) |
| **Sweep (grelha)** | Correr um parâmetro em vários valores para ver se o resultado é estável ou um pico de sorte (pico isolado = sobreajuste) |

---

## Parte III — O que é uma estratégia (parâmetros)

Uma estratégia diz ao bot **como executar cada trade**: quantas pernas,
se aplica a gestão do grupo, se sai cedo num TP, e se gere sozinho
(auto-gestão). Os parâmetros do registo (`app/strategies.py`):

| Parâmetro | O que faz |
|---|---|
| `max_legs` | Nº de pernas (TP1..TPN). `None` = 5 (todas) |
| `manage` / `manage_kinds` | Gestão do grupo: `True` = honra todos os tipos; um conjunto filtra (ex. `{"protect"}` = só o protect) |
| `exit_at_tp` | Quando o TP_n bate, TODAS as pernas ainda abertas fecham nesse preço (sair cedo). `None` = corre até ao fim |
| `lots_override` | Lote fixo em vez de risco % (nenhuma estratégia atual usa) |
| `stair_levels` / `stair_from` | **Escada de proteção** (auto-gestão): quando o TP_k bate, o SL das pernas seguintes sobe para o nível do TP_k. `stair_from` = degrau onde começa (1 = sobe para o NÍVEL do TP1; 2 = TP1 só vai a BE e a escada começa no TP2) |
| `confirm_filter` | **Filtro de confirmação** (auto): se uma perna k levar SL (o movimento morreu), as pernas > k fecham ao mercado |
| `trail_factor` | **Trailing** (auto): após o TP1, o SL segue o pico com folga = `trail_factor × distância(entrada→TP2)`. 2.0 ≈ 8 pips em gold |
| `time_stop_minutes` | **Time stop** (auto): se as pernas ficarem abertas N min após o TP1, fecham ao mercado |
| `atr_k` / `atr_period` / `atr_timeframe` | Trailing adaptativo por ATR — **desligado** (o sweep provou que é pior; ver Parte VIII) |

**O que TODAS fazem igual**: SL sempre ativo (SL-first) · TP1 fecha a perna 1
 e move as restantes a BE · risco dividido pelas pernas · custos reais no
 backtest (spread, slippage, comissão) · kill diário (perda do dia > **3%** por
 defeito, configurável) → pausa.


---

## Parte IV — Gestão do grupo (mensagens ao vivo)

O classificador do bot decide o **tipo** de cada mensagem de gestão; a
estratégia ativa honra os do seu `manage_kinds`:

| kind | Exemplo real do grupo | Ação |
|---|---|---|
| `tp_hit` | "TP1 HIT +70 pips" | TP1 → SL das restantes a BE; outros TPs → registado |
| `sl_hit` | "Oh wow SL HIT" | registado + reconciliação |
| `close_tp1` | "Close TP1 here" | fecha só a perna 1 (restantes mantêm SL) |
| `close_tp` | "Close TP2 here" | fecha só a perna N ao mercado |
| `protect` | "close first entries and move stop loss above best entries" | fecha a perna 1 + sobe o SL das restantes para ACIMA da entrada |
| `close_all` | "we're out", "out at BE" | fecha tudo (BE → à entrada; senão ao mercado) |
| `move_sl` | "Adjust SL to 4331" | move o SL de todas as pernas abertas para o nível indicado |

Uma estratégia com `manage=True` (gestão completa) honra **todos**; uma com
`manage_kinds={"protect"}` honra **só o protect**; as `auto_*` mecânicas não
honram gestão do grupo, exceto a `auto_escada_3_hibrida`, que honra apenas
`protect` através do merge monotónico.

---

## Parte V — Auto-gestão (o bot a gerir sozinho)

As variantes `auto_*` substituem a gestão do grupo por **regras mecânicas**
corridas a cada tick, sempre **sem look-ahead** (só dados do passado):

1. **Escada de proteção** — o TP_k confirma → o SL das pernas seguintes sobe
   para o **nível do TP_k** (ideia "dar o TP3 sem perder nada");
2. **Trailing** — após o TP1: `SL = pico − trail_factor × dist(entrada→TP2)`.
   O pico é o máximo dos ticks **anteriores** (nunca o atual). É o
   mecanismo que replica o "we're out / move SL" do grupo — **~90% do
   edge** das auto;
3. **Filtro de confirmação** — se uma perna k levar SL, as pernas > k fecham
   ao mercado ("se a 3 morrer, sai tudo");
4. **Time stop** — pernas abertas N min após o TP1 fecham ao mercado;
5. **Modo defensivo** (19/08) — quando o PF das últimas `def_window` (10)
   trades resolvidas < 1.0, a estratégia passa a trail apertado
   (`def_trail=1.5`) + time stop (`def_time_stop=60`) **sem cortar pernas**;
   volta ao normal quando o PF recupera. A história é a da própria conta
   (paper ou real), sem look-ahead. Espelhado nos 3 motores (backtest /
   paper / MT5 Fase 3) — ver estudo em Parte VIII;
6. **Trailing adaptativo por ATR** (`max(trail_factor×dist, atr_k×ATR`) —
   implementado mas **rejeitado pelos dados**: o ATR do ouro é muito maior
   que o recuo de ~8 pips, alarga o trail e devolve o lucro ao BE (ver
   Parte VIII).
7. **Protect híbrido** — a `auto_escada_3_hibrida` mantém escada/trailing/
   modo defensivo e aceita apenas o `protect` do grupo quando este é mais
   protetor que o SL mecânico. A decisão é monotónica (compra: SL mais alto;
   venda: SL mais baixo), a perna 1 só fecha se o preço conhecido do evento
   confirmar uma saída não pior, e cada decisão fica em `protect_latency_log`
   no paper/MT5. O backtest devolve a mesma telemetria sem escrever na BD.

Descobertas honestas do estudo (ver Parte VIII): a escada, o filtro e o time stop
**não têm edge sozinhos** (PF 0.3–1.36 sem trailing); `max_legs=2` é o pior
(PF 2.20 com trail) — 2 TPs cortam o motor do lucro.

---

## Parte VI — As 12 estratégias, UMA A UMA

> Nota de resultados: "atual" = últimos 30 dias do formato atual do grupo
> (julho→hoje); "antiga" = período abr–jul (formato antigo, out-of-sample);
> "histórico completo" = todos os sinais disponíveis desde o início. Saldo
> sim 1000 € · risco base 2.5% (com escala dinâmica) · kill diário 3% por
> defeito · custos reais. Ver metodologia (Parte VII).

### 1. `nova` — "TP1..TP5" (sem gestão) — controlo

- **Mecânica**: abre as **5 pernas** com o risco dividido; SL-first; TP1
  fecha a perna 1 e move as restantes a BE; **não aplica NENHUMA mensagem
  do grupo** (nem protect, nem close_all — corre até ao fim ou até o preço
  bater SL/TPs).
- **Parâmetros**: `max_legs=None` (5) · `manage=False` · `exit_at_tp=None`.
- **Papel**: é o **controlo "o que acontece se não fizermos nada"** — a base
  de comparação para medir o valor da gestão. Também alimenta o contrafactual
  "sem gestão" do histórico de trades.
- **Resultados**: baseline sem gestão — **PF 1.25 · +261.77 €** no período
  atual (o `auto_5` sem trail é exatamente igual a esta). Ganha pouco; a
  gestão do grupo multiplica o resultado (ver `nova_gestao`).
- **Veredicto**: não é candidata a principal — é o termómetro.

### 2. `nova_gestao` — "TP1..TP5 + gestão" — **PRINCIPAL**

- **Mecânica**: igual à `nova` (5 pernas, SL-first, BE após TP1) **+ gestão
  do grupo ao vivo**: `protect` (fecha perna 1 + SL acima da entrada),
  `close_all` ("we're out" — sai tudo), `close_tp1`/`close_tp`, `move_sl`,
  TP/SL hits. O grupo decide quando proteger e quando sair; o bot obedece.
- **Parâmetros**: `manage=True` (todos os kinds) · sem auto-gestão.
- **Papel**: **a estratégia que a conta demo executa** — a melhor segundo o
  backtest (a fasquia que as outras têm de bater).
- **Resultados**: atual **PF 3.28 · +2 369 € · DD 22.7%** (a dashboard mostra
  PF 3.22 · +2 310 €, janela ligeiramente diferente). **Período antigo:
  PF 0.63 · −2 622 € (PERDE)** — o edge da gestão do grupo só existe no
  formato atual; no mercado antigo a gestão humana não salvou.
- **Veredicto**: a referência. Fraqueza honesta: depende do grupo (e o
  grupo perdeu no período antigo).

### 3. `tp2` — "TP1→BE + sair no TP2" (sem gestão)

- **Mecânica**: SL-first; **TP1 fecha a perna 1 e move as restantes a BE**;
  **quando o TP2 bate, todas as pernas ainda abertas fecham** nesse preço
  (`exit_at_tp=2`). Não aplica gestão do grupo.
- **Parâmetros**: `exit_at_tp=2` · `manage=False`.
- **Papel**: estudo "sair cedo" — fechar tudo no TP2 (lucro rápido, sem
  esperar TP3–5).
- **Resultados**: **PF 0.86 · −151 €** (estudo 30d, formato atual) — sair
  cedo SEM proteção perde (os TP3–5 são onde vive o lucro).
- **Veredicto**: perderia sozinha; só faz sentido com proteção (ver
  `tp2_protect`).

### 4. `tp2_gestao` — "Sair no TP2 + gestão"

- **Mecânica**: igual à `tp2` (sair no TP2) **mas com gestão do grupo
  completa** — o grupo pode fechar antes do TP2 (protect/close_all) ou
  proteger mais cedo.
- **Parâmetros**: `exit_at_tp=2` · `manage=True`.
- **Papel**: variante antiga de "sair cedo + gestão".
- **Resultados**: sem edge no compare histórico (perde para a `nova_gestao`
  — o estudo mostra gestão completa no TP2 ≈ PF 2.93 · +2 010 €, abaixo dos
  3.22 da `nova_gestao` que corre até TP5).
- **Veredicto**: retirada do menu e do paper por falta de edge; fica no
  registo apenas para backtest/compare histórico.

### 5. `tp2_protect` — "Sair no TP2 + protect only" — **teste**

- **Mecânica**: SL-first; TP1 fecha a perna 1 e move as restantes a BE;
  **TP2 bate → todas as pernas fecham**. Da gestão do grupo **só honra o
  `protect`** (fecha perna 1 + SL acima da entrada) — **ignora
  `close_all`/`close_tp1`/`move_sl`**: o grupo pode dizer "we're out" que a
  estratégia continua a correr para o TP2.
- **Parâmetros**: `exit_at_tp=2` · `manage_kinds={"protect"}`.
- **Papel**: teste de Fase 2 (corrida paper) — a pergunta "sair cedo só com
  a proteção do grupo justifica-se?".
- **Resultados** (estudo 30d, formato atual): **tp2 puro PF 0.86 · −151 €** ·
  **+ protect PF 2.65 · +1 721 €** · gestão completa PF 3.22 · +2 309 €. O
  protect recupera ~86% do caminho até à gestão completa.
- **Veredicto**: sair cedo SÓ vale com a proteção do grupo; mesmo assim a
  gestão completa (`nova_gestao`) continua a ganhar. Em validação ao vivo.

### 6. `nova_gestao_3` — "TP1..TP3 + gestão"

- **Mecânica**: igual à `nova_gestao` mas com **só 3 pernas** (TP1..TP3) —
  menos lucro potencial mas menos exposição às pernas 4–5 (seguro contra
  mudança de regime).
- **Parâmetros**: `max_legs=3` · `manage=True`.
- **Papel**: variante antiga conservadora.
- **Resultados**: sem edge no compare histórico (as pernas 4–5 é que dão o
  lucro; cortá-las corta o motor).
- **Veredicto**: retirada do menu e do paper; fica no registo apenas para
  backtest/compare histórico.

### 7. `auto_2` — "Auto 2 TPs"

- **Mecânica**: **2 pernas** (TP1..TP2), BE após TP1, **sem gestão do
  grupo e sem auto-gestão registada** (o registo não lhe dá trail/escada).
- **Parâmetros**: `max_legs=2` · `manage=False`.
- **Papel**: a ideia dos amigos ("só 2 TPs") — o sweep provou que é a pior.
- **Resultados**: no sweep COM trail adicionado: **PF 2.20 · +788 €** (atual)
  · PF 1.13 · +618 € (antigo) — mesmo assim o pior da família. Sem trail
  (como está registada) seria ainda pior.
- **Veredicto**: 2 TPs cortam o motor do lucro. No menu desde 19/08 para
  mostrar ao vivo.

### 8. `auto_3` — "Auto 3 TPs"

- **Mecânica**: **3 pernas**, BE após TP1, sem gestão do grupo e sem
  auto-gestão registada.
- **Parâmetros**: `max_legs=3` · `manage=False`.
- **Papel**: mínimo viável "só com 3 TPs" (as pernas 1–2 dão prejuízo; o
  lucro vive nas 3–5).
- **Resultados**: no sweep COM trail: **PF 3.03 · +1 723 € · DD 31.2%**
  (atual) · PF 1.38 · +2 162 € · DD 48.4% (antigo). Sem trail (registada)
  seria fraca.
- **Veredicto**: sem o trailing não tem edge; o trailing é o mecanismo
  (ver `auto_escada_3`, a versão com escada+trail).

### 9. `auto_5` — "Auto 5 TPs"

- **Mecânica**: **5 pernas**, BE após TP1, sem gestão do grupo e sem
  auto-gestão registada — **é exatamente a `nova`** (baseline sem gestão).
- **Parâmetros**: `max_legs=None` (5) · `manage=False`.
- **Papel**: o baseline da família auto (duplicado da `nova`).
- **Resultados**: no sweep COM trail: **PF 3.50 · +2 613 € · DD 42.6%**
  (atual) · PF 1.46 · +3 579 € · **DD 84%** (antigo) — com trail ganha muito
  mas com drawdowns enormes.
- **Veredicto**: sem trail = `nova`; com trail = muito lucro mas DD 84% no
  antigo (risco alto demais — por isso a escolha foi a `auto_escada_3`).

### 10. `auto_escada_3` — "Auto 3 TPs + escada" — **teste · Fase 2** ⭐

- **Mecânica**: **3 pernas**; BE após TP1; **escada de proteção**
  (`stair_levels=3`, `stair_from=1`: TP_k confirma → SL das seguintes sobe
  para o nível do TP_k); **trailing `trail_factor=2.0`** (≈8 pips em gold,
  o valor honesto calibrado); **modo defensivo** (PF das últimas 10 trades
  < 1.0 → trail 1.5 + time stop 60 min, sem cortar pernas); sem gestão do
  grupo.
- **Parâmetros**: `max_legs=3` · `stair_levels=3` · `stair_from=1` ·
  `confirm_filter=True` · `trail_factor=2.0` ·
  `def_window=10 · def_trail=1.5 · def_time_stop=60` · `manage=False`.
- **Papel**: **a candidata à Fase 3** (auto-gestão no MT5) — em corrida
  paper a validar (N ≥ 20 sinais; gates: PF ≥ 1.5 · P&L ≥ 50% da referência
  · DD ≤ 125%).
- **Resultados do Item 3** (sweep `--item3-sweep`, mesmas velas/custos,
  19/08): no período **atual**, `confirm=False` deu **PF 1.27 / DD 11.8%**
  e `confirm=True` **PF 1.26 / DD 11.8%** (queda relativa de PF 0.79%);
  no **histórico completo**, 0.47 / 322.0% contra **0.53 / 286.1%**; no
  **antigo**, 0.40 / 312.9% contra **0.46 / 278.3%**.
- **Veredicto**: o DD de cauda reduziu-se e o PF atual perdeu menos de 10%,
  por isso `confirm_filter=True` passou o critério e ficou congelado como
  default desta estratégia. A alteração é só da auto em paper/backtest;
  não promove nem altera a principal `nova_gestao`.

### 11. `auto_escada_5` — "Auto 5 TPs + escada + confirmação"

- **Mecânica**: **5 pernas**; escada de proteção (TP_k → SL das seguintes
  no nível do TP_k); **filtro de confirmação** (perna k leva SL → pernas > k
  fecham); trailing 2.0; **modo defensivo** (PF(10) < 1.0 → trail 1.5 +
  time stop 60 min); sem gestão do grupo.
- **Parâmetros**: `max_legs=None` (5) · `stair_levels=5` · `confirm_filter=True`
  · `trail_factor=2.0` · `def_window=10 · def_trail=1.5 · def_time_stop=60`
  · `manage=False`.
- **Papel**: a variante mais agressiva (mais pernas = mais lucro potencial,
  mais risco de cauda).
- **Resultados**: atual **PF 3.66 · +2 787 € · DD 35.9%** · antigo
  **PF 1.43 · +3 356 € · DD 86.4%** — o melhor PF absoluto mas com
  **drawdown de 86% no período antigo** (quase falência).
- **Veredicto**: o equilíbrio é pior que o escada_3 (lucro parecido, DD
  muito maior) — fica no menu para comparação ao vivo.

### 12. `auto_escada_3_hibrida` — "Auto 3 TPs + escada + protect híbrido" — **experimental**

- **Mecânica**: 3 pernas, escada desde o TP1, trailing `trail_factor=2.0`
  e modo defensivo (PF(10) < 1.0 → trail 1.5 + time stop 60 min). Quando
  chega `protect` do grupo, compara o SL do grupo com o SL mecânico por perna
  e aplica apenas o nível mais protetor; nunca recua um SL já ganho pela
  auto. A perna 1 só fecha quando o preço conhecido no evento já confirmou
  TP1 e o `protect` não é pior que a proteção automática.
- **Parâmetros**: `max_legs=3` · `stair_levels=3` · `stair_from=1` ·
  `trail_factor=2.0` · `def_window=10 · def_trail=1.5 · def_time_stop=60` ·
  `manage_kinds={"protect"}`.
- **Papel**: conta paper própria e página de teste para validar a hipótese
  "auto mecânica + timing humano". Não substitui `nova_gestao`, não é
  elegível para auto-switch e a sua telemetria deve acumular pelo menos 20
  eventos `protect` antes de tirar a conclusão.
- **Relatório**: a tabela **Protect híbrido — latência auto vs grupo** mostra
  vencedor, diferença em pips, atraso e ação da perna 1; o backtest usa a
  mesma cronologia sem look-ahead. `GET /api/analysis/protect_causal` também
  faz a atribuição por prefixos (baseline sem protect → cada evento) e mostra
  os piores deltas.
- **Item 1 validado e encerrado (19/08)**: no snapshot do histórico completo
  a híbrida ficou igual à auto pura (**PF 2.21 · DD 32.5%**); no recorte de
  30 dias, ambas ficaram em **PF 3.20 · DD 24.6%**. A causalidade encontrou
  **355 eventos em 270 sinais, 0 harmful e delta 0 €** — os `protect` não
  causaram as perdas observadas. A paridade buy/sell entre backtest,
  SimEngine, PaperRace e MT5 dry-run está em `tests/test_engine_parity.py`.
  O spec `docs/Melhorias/01-item-01.md` foi apagado só depois desta validação;
  a híbrida continua experimental e não é elegível para promoção automática.

---

## Parte VII — Metodologia dos resultados (para confiar nos números)

- **Dados**: os sinais reais disponíveis na BD do Golden Moves desde o início
  do grupo (o número exato aparece no snapshot/relatório e pode crescer quando
  chega histórico novo), com os 3 formatos de mensagem que o grupo usou ao
  longo do tempo. O histórico antigo (abr–jul) é o **out-of-sample**:
  calibrou-se no formato atual e validou-se no antigo.
- **Simulação**: replay dos sinais sobre **velas reais** (preços históricos),
  com as MESMAS regras da produção: SL-first, sem look-ahead (nunca se usa o
  fecho da vela do evento), risco por sinal **2.5% por defeito** (escala
  dinâmica possível) dividido pelas pernas, **custos reais** (spread,
  slippage, comissão), contract por símbolo (XAUUSD 100, XAGUSD 5000, forex
  100000), **kill diário** (perda do dia > **3% por defeito**, configurável →
  trades do resto do dia não abrem — espelho da produção).
- **Sweeps**: os parâmetros (trail 1.5–2.5, ATR) foram corridos em grelha
  para detetar **sobreajuste**: uma curva monotónica sem pico isolado =
  parâmetro honesto; um pico isolado = sorte. O trail 2.0 é o meio de um
  plateau monotónico (PF 4.69→1.97, sem saltos).
- **Validação estatística**: amostras pequenas (< 10–20 trades) são ruído —
  por isso os gates exigem N ≥ 20 e o veredicto automático só decide com 10
  trades reais.
- **Painel ao vivo**: a corrida paper (contas virtuais de 1000 €, todos os
  sinais reais a partir de hoje) é o teste forward — os números deste
  documento são o passado; a corrida é o futuro.

---

## Parte VIII — Como avaliar (guia de leitura)

Uma estratégia é boa se:

1. **PF ≥ 1.5 no período atual E não-perdedor no período antigo**
   (robustez — o grupo perde no antigo, as auto_* ganham);
2. **DD controlado** (DD ≤ ~40% no antigo; DD > 80% = risco de cauda);
3. **Sweep monotónico** nos parâmetros (sem pico de sorte);
4. **bate a referência `nova_gestao`** (PF 3.28 atual / 0.63 antigo) no
   backtest E na corrida paper ao vivo;
5. **N ≥ 20 sinais** na corrida antes de concluir qualquer coisa.

**Onde estão hoje (19/08, consolidação)**: `nova_gestao` executa a conta
demo; a `auto_escada_3` e a `tp2_protect` estão em corrida paper (Fase 2);
no menu ficam **6 visíveis** (`nova_gestao`, `tp2_protect`, `auto_3`,
`auto_escada_3`, `auto_escada_3_hibrida`, `auto_escada_5`); `nova` e `auto_5` continuam em PAPEL mas
ocultas do menu (contrafactual "sem gestão" + comparação 5 pernas com/sem
trail — `MENU_HIDDEN_STRATEGIES`); `tp2`, `tp2_gestao`, `nova_gestao_3` e
`auto_2` saíram do menu E do paper (`HIDDEN_STRATEGIES`) — ficam só no
backtest/compare, nunca se apagam dados. Quando a `auto_escada_3` passar os
gates (N ≥ 20 · PF ≥ 1.5 · P&L ≥ 50% · DD ≤ 125%), sobe para a Fase 3 (MT5
demo, janela de validação de 10 trades reais com veredicto automático).
Conta real: nunca sem instrução explícita (SPEC v3, rascunho).

**Estudo da consolidação (19/08, `app/study_consolidation.py`)**: testou o
filtro de entrada (sessão Londres/NY + distância entrada→TP2 ≥ 5 pips + ATR
p20) e o modo defensivo (PF das últimas 10 trades < 1.0 → trail 1.5 + time
stop 60 min, SEM cortar pernas) nas 3 janelas:

| Variante | Atual (PF/DD) | Tudo (PF/DD) | Antiga (PF/DD) |
|---|---|---|---|
| auto_escada_3 baseline | 4.95 / 11.8% | 2.13 / 38.0% | 1.41 / 35.2% |
| + filtro de entrada | 6.29 / 9.4% (rej 8) | 2.15 / 42.8% (rej 32) | 1.50 / 33.4% (rej 180) |
| + modo defensivo | 4.97 / 11.8% (5 ativos) | 2.20 / 32.5% (224) | 1.48 / 32.5% (219) |
| auto_escada_5 baseline | 5.73 / 15.8% | 2.25 / 87.1% | 1.43 / 86.4% |
| + filtro de entrada | 7.33 / 12.5% | 2.32 / 68.4% | 1.50 / 68.7% |
| + modo defensivo | 5.95 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |
| auto_3 baseline | 1.25 / 11.8% | 0.44 / 339% | 0.37 / 330% |
| + filtro de entrada | 1.53 / 9.4% | 0.44 / 327% | 0.37 / 231% |
| + modo defensivo | 1.68 / 11.8% | 0.97 / 88% | 0.86 / 87% |

**Veredicto honesto**: o **filtro de entrada NÃO tem edge** — rejeita
quase só no período antigo (onde TUDO perde, logo cortar não prova nada; o
lucro cai), a sessão não filtra nada no atual (o grupo já opera em horário
ativo) e o ATR rejeita 8 sinais no atual (ruído). A sensibilidade **só-ATR**
rejeita **0 sinais** (a rejeição do filtro completo era toda da sessão —
confirma que o ATR não filtra nada por si). **NÃO implementado.**

Na `auto_escada_3` o estudo base encontrou um efeito pequeno e consistente
(DD menor com P&L igual ou melhor). **Na `auto_escada_5` isso não se confirmou
de forma robusta**: o sweep específico do Item 2, com baseline não contaminado,
mostra DD antiga ainda acima de 84% em toda a grelha e um pico isolado de PF
no eixo `def_window`. O efeito procurado seria do time stop + trail apertado
em regimes adversos — NÃO de cortar pernas (o `max_legs=2` do spec original
foi rejeitado: o sweep já provou que 2 pernas cortam o motor do lucro).
**Implementado (19/08)**: o modo defensivo (trail 1.5 + time stop 60 min
quando PF(10) < 1.0, sem cortar pernas) está nas `auto_escada_3/5` —
parâmetros `def_window`/`def_trail`/`def_time_stop` no registo, espelhados
no backtest (`simulate_signal`), na corrida paper (`PaperRace`, conta
virtual) e no MT5 real (`_update_real`, Fase 3, trades reais fechadas), com validação na corrida paper antes da Fase 3.

### Item 2 — sweep defensivo da `auto_escada_5` (19/08)

O sweep reproduzível é executado com:

```text
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_consolidation --item2-sweep
```

O filtro de entrada do estudo mantém a promessa de não leakage: offsets com
fuso são convertidos para UTC, o ATR diário usa a última observação de cada
dia UTC e o piso p20 de cada sinal só consulta dias anteriores; os timestamps
sem fuso históricos continuam a ser tratados como hora local do bot.

A grelha completa é `def_window` **5/7/10** × `def_trail` **1.0/1.2/1.5**
× `def_time_stop` **30/45/60** (27 combinações), nas mesmas janelas
**atual / tudo / antiga**. O baseline é calculado uma vez por janela e a
história baseline é reutilizada em todas as variantes, sem leakage. Os três
time stops tiveram os mesmos números neste replay, por isso a tabela abaixo
mostra uma linha por combinação de window/trail e indica `30=45=60`.

Baseline: atual **PF 5.73 / DD 15.8%** · tudo **PF 2.25 / DD 87.1%** · antiga
**PF 1.43 / DD 86.4%**.

| `def_window` | `def_trail` | `def_time_stop` | Atual PF/DD | Tudo PF/DD | Antiga PF/DD |
|---:|---:|:---:|---:|---:|---:|
| 5 | 1.0 | 30=45=60 | 6.72 / 15.8% | 2.31 / 84.5% | 1.49 / 85.5% |
| 5 | 1.2 | 30=45=60 | 6.26 / 15.8% | 2.30 / 84.8% | 1.48 / 85.8% |
| 5 | 1.5 | 30=45=60 | 6.25 / 15.8% | 2.28 / 86.3% | 1.47 / 86.1% |
| 7 | 1.0 | 30=45=60 | 6.98 / 15.8% | 2.34 / 84.5% | 1.49 / 85.5% |
| 7 | 1.2 | 30=45=60 | 6.51 / 15.8% | 2.33 / 84.8% | 1.48 / 85.8% |
| 7 | 1.5 | 30=45=60 | 6.50 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |
| 10 | 1.0 | 30=45=60 | 6.42 / 15.8% | 2.34 / 84.5% | 1.49 / 85.5% |
| 10 | 1.2 | 30=45=60 | 5.95 / 15.8% | 2.33 / 84.8% | 1.48 / 85.8% |
| 10 | 1.5 | 30=45=60 | 5.95 / 15.8% | 2.30 / 85.1% | 1.47 / 86.1% |

**Veredicto do Item 2**: **0 combinações elegíveis**. Nenhuma reduz o DD
antigo para <50% (o melhor fica ~84.5%); a perda de PF atual não seria o
problema, mas o risco de cauda continua. A monotonicidade é verde nos eixos
`def_trail`/`def_time_stop` e nas janelas tudo/antiga; no eixo `def_window` do
período atual há um pico isolado (PF médio **6.41 → 6.66 → 6.11** para
5→7→10). Portanto reporta-se o sweep, **não se altera nenhum default** e
não se promove `auto_escada_5` para a conta MT5. O spec
`docs/Melhorias/02-item-02.md` foi apagado depois de o sweep e o veredicto
ficarem documentados.

**Item 3 encerrado (19/08)**: o sweep read-only de `auto_escada_3` comparou
`confirm_filter=True` vs `False` nas janelas atual/tudo/antiga, com
`stair_levels=3`, `stair_from=1` e `trail_factor=2.0` fixos. O critério passou:
DD antiga caiu de 312.9% para 278.3% e o PF atual caiu só de 1.27 para 1.26
(0.79%, abaixo do limite relativo de 10%). O registo foi atualizado para
`confirm_filter=True`, sem promover a conta principal; o spec
`docs/Melhorias/03-item-03.md` foi apagado depois dos testes e da
aceitação.

**A/B/C das 5 estratégias base visíveis no histórico completo** (19/08,

579 sinais, saldo
inicial 1000 €, risco/kill diário da config — mesma simulação das páginas
de teste com `days=0`):

| Estratégia | Sinais | Resolvidas | PF | P&L € | WR% | DD% | Falência |
|---|---|---|---|---|---|---|---|
| `nova_gestao` (referência) | 579 | 382 | 1.00 | +20 | 49.5 | 252 | ✅ (morre) |
| `tp2_protect` | 579 | 380 | 0.82 | −1 399 | 49.5 | 330 | ✅ (morre) |
| `auto_3` | 579 | 444 | 0.43 | −3 207 | 49.5 | 343 | ✅ (morre) |
| `auto_escada_3` ⭐ | 579 | 482 | 2.10 | +6 863 | 48.3 | 24.3 | ❌ |
| `auto_escada_5` | 579 | 405 | 2.30 | +11 063 | 46.9 | 25.8 | ❌ |

**Leitura honesta**: no histórico COMPLETO (abr→hoje, que junta o período
antigo perdedor ao formato atual) só as **auto_escada_3/5 têm edge** — as
restantes morrem em falência (DD > 100% porque o sim continua a arriscar %
fixa até o saldo chegar a zero). A `nova_gestao` fica em break-even (+20 €)
porque o período antigo perde tanto quanto o atual ganha — é por isso que
os critérios da Fase 3 avaliam a `auto_escada_3` em janelas recentes (N ≥
20 · PF ≥ 1.5 · P&L ≥ 50% da referência · DD ≤ 125%) e não no histórico
completo. A `tp2_protect` e a `auto_3` mantêm-se visíveis como testes ao
vivo (a proteção do grupo e o trail mínimo podem justificar-se em regime
recente — 30 dias: PF 2.65 e 3.03 respetivamente), mas o histórico completo
mostra que não sobrevivem a regimes adversos prolongados.

---

## Parte IX — Onde cada estratégia aparece

| Estratégia | Página | Corrida paper | Backtest/compare | Conta real |
|---|---|---|---|---|
| `nova_gestao` | PRINCIPAL | ✅ (referência) | ✅ | ✅ demo (principal) |
| `tp2_protect`, `auto_3`, `auto_escada_3`, `auto_escada_5` | dropdown ▚ ESTRATÉGIAS (`/teste?s=<id>`) | ✅ | ✅ | 🔒 só com validação |
| `auto_escada_3_hibrida` | dropdown ▚ ESTRATÉGIAS (`/teste?s=auto_escada_3_hibrida`) | ✅ experimental | ✅ | 🔒 não elegível sem validação explícita |
| `nova`, `auto_5` (contrafactual/comparação) | 🔒 ocultas do menu | ✅ em papel (não na curva) | ✅ | 🔒 |
| `tp2`, `tp2_gestao`, `nova_gestao_3`, `auto_2` (removidas) | 🔒 | ❌ | ✅ backtest/compare | 🔒 |

Todas as estratégias têm página própria com saldo simulado (1000 €), curva,
comparação e os números reais de 30 dias; a página **HELP** (`/help`)
explica-as com cartões interativos. O registo (`app/strategies.py`) é a
fonte única de verdade; os sets `HIDDEN_STRATEGIES` (tp2/tp2_gestao/
nova_gestao_3/auto_2 — fora do menu E do paper) e `MENU_HIDDEN_STRATEGIES`
(nova/auto_5 — ocultas do menu mas em papel, para o contrafactual do
histórico de trades e a comparação do backtest) controlam a visibilidade
sem apagar nada; para repor, basta tirar o id do set.

---

## Parte X — Fase 3 (MT5 demo) e SPEC v3 (conta real) — protocolo de promoção

> Consolidado de `docs/SPEC-auto-gestao.md` (apagado 19/08 — este documento
> é agora a fonte única sobre estratégias). A Fase 3 é o caminho da
> `auto_escada_3` da corrida paper para a **conta demo no MT5**; o SPEC v3 é
> apenas um rascunho de gates para a conta real — **nunca** tocar real sem
> instrução explícita do utilizador.

### Configuração congelada da `auto_escada_3` (Fase 2/3)

| Parâmetro | Valor |
|---|---|
| `max_legs` | 3 |
| `stair_levels` / `stair_from` | 3 / 1 (escada desde o TP1) |
| `confirm_filter` | true (Item 3, sweep 19/08: DD antiga 312.9% → 278.3% e PF atual 1.27 → 1.26) |
| `trail_factor` | 2.0 (meio do plateau 1.5–2.5 — monotónico, sem pico isolado) |
| `def_window` / `def_trail` / `def_time_stop` | 10 / 1.5 / 60 (modo defensivo) |
| `time_stop_minutes` | 0 (só o defensivo o liga) |
| risco / kill diário | 2.5% (config) · `daily_stop_pct` ativo (paper e real) |
| referência | `nova_gestao` em paralelo (mesma conta virtual de 1 000 €) |

O `confirm_filter=True` foi congelado como default após o Item 3; a janela
paper/MT5 continua a exigir os gates da Parte X antes de qualquer promoção.

**Não alterar durante a corrida**: parâmetros, regras de entrada/saída,
custos, filtro de sinais.

### X.1 Protocolo de promoção para MT5 (conta demo)

> Regra de ouro: conta **demo** (`5054644473 · MetaQuotes-Demo`). Nunca
> promover para real sem instrução explícita (SPEC v3, abaixo).

**Pré-requisitos (gate — não saltar):**

```text
1. Critérios de aprovação com N ≥ 20 sinais resolvidos na corrida paper:
   PF ≥ 1.5 · P&L ≥ 50% da referência · DD ≤ 125% — **os 3 em simultâneo**
   (gate estrito, sem comité 2 de 3; decisão do utilizador de 19/08: com
   N=20 o ruído não justifica flexibilizar — o DD alto é exatamente o
   sinal de risco que o gate deve travar, mesmo com PF e P&L bons);
2. Sem gates antecipados: falência precoce (PF < 1.0 com N ≥ 10 → terminar)
   ou hot streak (nunca promover antes dos N ≥ 20);
3. Divergências paper vs backtest todas explicadas (spread/slippage/timing);
4. Zero posições sem SL válido e zero ordens duplicadas no paper.
```

**O MT5Engine já tem tudo implementado** (verificado; desligado até promover):
escada/trailing/filtro de confirmação/time stop no `_update_real`, throttling
de modify (≥ 1 pip, ≤ 1×/min), três níveis de SL (`target→valid→applied` com
retcodes 10009/10016 + auditoria `autosl:<trade_id>`), modo defensivo (PF das
últimas 10 trades reais fechadas < 1.0 → trail 1.5 + time stop 60), aviso
`risk` no feed/Telegram quando um modify é recusado/clamped, teste E2E com
broker fake. O trailing por ATR (`max(trail_factor×dist, atr_k×ATR)`) está
implementado nos 3 motores mas **rejeitado pelos dados** (sweep 18/08: ATR é
estritamente pior que o trail fixo 2.0 — o ATR de ouro alarga o trail e
devolve o lucro) — fica desligado, nenhuma estratégia define `atr_k`.

**Janela de validação (primeiras 10 trades reais da auto na demo):**

```text
Aprovar a permanência se, nas 10 primeiras:
- mesma decisão de saída que o paper no mesmo sinal em ≥ 9/10;
- zero posições sem SL; zero ordens duplicadas; retcodes 10009;
- atraso sinal→abertura ≤ ~2 min; P&L coerente com o backtest.

Rollback automático (volta à nova_gestao e reabre a janela depois):
- PF < 1.0 nas primeiras 10; ≥ 2 divergências estruturais (saída diferente
  sem explicação de mercado); qualquer posição sem SL; 10027 persistente.
```

**Automatizado**: `state._check_fase3_validation` decide rollback/manutenção
uma vez por janela (nova promoção grava snapshot novo e reabre), notifica
feed/Telegram e guarda o veredicto em `fase3_verdict` (+ histórico
`fase3_verdicts`, nunca se apaga). A janela é limitada às primeiras 10 trades
abertas pela estratégia depois de `promoted_at`; a manutenção exige evidência
paper↔MT5 com a mesma saída em pelo menos 9/10 — pares em falta também
reprovam. O painel Fase 3 mostra os gates, o ETA
dos 20 sinais (`fase3_eta`, ritmo dos últimos 30 dias + alerta quando > 6
semanas ou o ritmo mudar ≥ 50%) e o botão "🎯 Reduzir alvo para 15"
(`config fase3_target`) quando o ETA estourar. Durante a Fase 3, a corrida
paper continua em paralelo (a auto paper é a referência teórica da auto
MT5) e o resumo diário mostra a linha 🚀 da auto MT5 (N real, P&L real).

### X.2 SPEC v3 — Promoção para conta REAL (RASCUNHO)

> ⚠️ Rascunho de intenção, NÃO um plano aprovado. A conta real só se toca
> com instrução explícita do utilizador, e o caminho até lá passa
> OBRIGATORIAMENTE pela Fase 3 (demo) com a janela limpa.

**Pré-requisitos absolutos (gate 0):** Fase 3 aprovada (janela de 10 trades
limpa) · 3–4 semanas de demo (≥ 30 trades) sem rollback · zero alertas de
risco não explicados · decisão EXPLÍCITA do utilizador (nenhuma automatização
promove real) · docs + spec revistos com os números reais.

**Gates de continuidade (revisão a cada 10 trades reais):** PF ≥ 1.5 e
P&L ≥ 0 na janela deslizante de 20 · DD ≤ 125% do backtest da mesma janela ·
divergências demo vs real explicadas · kill diário SEMPRE ativo · se PF < 1.0
em 10 trades consecutivas → voltar a demo e reavaliar.

**Linha vermelha (nunca em real):** arriscar mais do que o aprovado ·
desligar o kill diário ou o `max_positions` · promover outra estratégia sem
o mesmo processo · alavancagem/hedging fora do aprovado. O bot continua
SEMPRE em demo até o utilizador mandar.

---

*Fim do documento — fonte única sobre estratégias (registo, auto-gestão,
resultados, protocolo de promoção). Histórico das fases concluídas da
auto-gestão vive no CHANGELOG; a calibração detalhada (mineração, sweep)
nos scripts `app/mine_management.py` / `app/sweep_auto.py` / `app/sweep_atr.py`
/ `app/study_consolidation.py`.*
