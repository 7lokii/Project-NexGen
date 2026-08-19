# SPEC — Melhorias ao Project NexGen (estratégias + operação)

> Especificação técnica de 17 itens de trabalho, independentes entre si
> (podem ser implementados em paralelo), com contexto, critérios de
> aceitação e ordem de prioridade. Dividida em duas partes: **Parte A —
> Estratégias** (itens 1–6) e **Parte B — Operação e observabilidade**
> (itens 7–17), focada em manter o projeto a correr por longos períodos com
> o mínimo de intervenção manual. Nenhum item toca conta real — tudo corre
> em backtest → paper → (só depois) Fase 3 MT5 demo, seguindo o protocolo já
> existente em `docs/ESTRATEGIAS.md` Parte X.

---

## Contexto (ler primeiro)

O Project NexGen é um bot de copy-trading que replica sinais de Telegram no
MetaTrader 5 (conta demo). Tem 3 motores que correm a MESMA lógica de
execução em paralelo: **backtest** (`simulate_signal`, replay histórico),
**paper** (`app/execution/paper.py`, `PaperRace`, contas virtuais de 1000 €
em tempo real) e **MT5 real** (`_update_real`, conta demo). As estratégias
vivem como registo central em `app/strategies.py` — qualquer parâmetro novo
tem de existir e comportar-se **igual nos 3 motores**, ou os relatórios de
divergência (`real_vs_backtest`, `paper_vs_real_auto`) acusam-no.

### Regras fixas

- **Sem look-ahead**: nenhuma decisão pode usar dados futuros (ex.: fecho da
  vela em curso). O trailing usa sempre o pico dos ticks **anteriores**.
- **Nunca tocar conta real.** Toda a implementação corre em backtest/paper;
  promoção a MT5 demo só depois de gates explícitos (Fase 2/3, já existentes).
- **Kill switch diário está DESATIVADO neste momento, de propósito.** Não
  reativar enquanto o trabalho for só código/backtest/sweeps/paper isolado.
  Só reativar o kill switch (`daily_stop_pct` / `daily_loss_limit_pct`,
  conforme a config) **imediatamente antes** de começar a testar qualquer
  estratégia com os sinais reais do grupo a correr na estratégia
  **Principal** (nova_gestao ou quem a substituir) — ou seja, no arranque de
  qualquer janela de validação ao vivo (Fase 2 paper com sinais reais,
  Fase 3 MT5 demo, ou qualquer teste que mexa na Principal). Antes disso,
  deixar desligado. Em caso de dúvida sobre se um item envolve sinais reais
  a abrir na Principal, confirmar antes de mexer no kill switch — nunca
  assumir.
- **Nunca apagar dados nem estratégias do registo** — usar os sets
  `HIDDEN_STRATEGIES` / `MENU_HIDDEN_STRATEGIES` para visibilidade.
- **Sweep antes de aceitar qualquer parâmetro novo**: correr em grelha e
  confirmar curva monotónica (sem pico isolado) antes de o congelar.
- Documentar cada mudança no `CHANGELOG.md` e atualizar
  `docs/ESTRATEGIAS.md` (é a fonte única sobre estratégias).

O item 1 é o mais valioso — priorizar se só houver capacidade para um.

---

---

## Como usar estes ficheiros

Os itens 01–17 são independentes salvo quando indicado no próprio ficheiro. A implementação deve respeitar sempre as regras fixas acima e atualizar `CHANGELOG.md` e `docs/ESTRATEGIAS.md` quando aplicável. A ordem sugerida e as dependências entre itens estão em `18-ordem-de-execucao.md`.
