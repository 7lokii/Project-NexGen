# AGENTS.md - Project NexGen (Freebuff)

Guia para agentes (Codebuff/Freebuff) que trabalham neste projeto. Lê isto
antes de mexer no código.

## O que é o projeto

**Project NexGen** - sistema de copy-trading semi-automático em Python (FastAPI +
vanilla JS). Lê sinais do grupo de Telegram **Golden Moves**, espera a janela de edição (20s), executa a estratégia de **N pernas (TP1..TP5 + breakeven)**
no **MetaTrader 5** (conta **demo**, não é dinheiro real) e mostra tudo numa
dashboard web em `http://127.0.0.1:8000`.

Estado atual (2026-09-01): servidor em **MT5-real na conta demo** (`EXECUTION_MODE=mt5`,
`MT5_DRY_RUN=0`), modo **auto**, estratégia principal persistida
`tp2_protect` (2 pernas, saída no TP2) (TP1..TP5 + gestão do grupo), listener do Telegram a escutar o
Golden Moves. A **segunda conta demo MT5 (5055369549, saldo 10.000 €)** está
ligada como lane
`tp2` (env `MT5_2_*`) e **passou a execução real em 01/09** com a
`nova_gestao`; a **02/09 a lane passou para a conta 5055369549 (10k) com a
mesma estratégia da principal - `tp2_protect`** (config persistida
`lane2_strategy`), terminal **portable** em
`C:\MT5Portable2\terminal64.exe` (`MT5_2_TERMINAL_PATH`), `LANE2_WORKER_ENABLED=1`
e `MT5_2_DRY_RUN=0` no `.env` - o **worker isolado** (`app/lane2_worker.py`,
porta 8770, PID em `lane2_worker.pid`) é o dono da ligação MetaTrader5 à
5055369549 e o servidor fala-lhe via `RemoteProvider` (ver secção
Lanes/arquitetura). A conta principal **5054644473** continua no seu terminal
(Program Files) - os dois terminais/processos NUNCA partilham ligação.
✅ O **AutoTrading do terminal 2 foi LIGADO a 01/09** (clique automatizado
no botão "Algo Trading" da toolbar Standard do terminal portable - o último
botão à direita; validado por `terminal_info().trade_allowed` através do
worker). ⚠️ É **por-sessão** - reativar depois de cada reboot, como já
acontece no terminal 1. Enquanto estiver OFF, as ordens da lane 2 são
recusadas com retcode 10027 + alerta. O **Diagnóstico expõe o estado**
(`lane2_worker.trade_allowed` - ON/OFF no bloco "Lane 2") e o market_loop
emite alerta `risk` automático quando o worker está ligado mas o AutoTrading
está OFF (cooldown 10 min; evento `lane2_auto_trading_off`). **Watchdogs do
símbolo e do retcode null (02/09)**: o market_loop verifica a cada 30s que
XAUUSD tem quote no worker da lane 2 - sem tick, chama `ensure_symbol`
(`symbol_select`) para o re-adicionar ao Market Watch e emite alerta `risk`
(episódio com cooldown 10 min + notificação de recuperação; eventos
`lane2_symbol_reselect`/`lane2_symbol_recovered`); e um **retcode `null`**
(broker respondeu SEM retcode - mapeado para `?`, ex.: 14:52 de 02/09) numa
ordem da lane 2 dispara alerta `risk` distinto com o estado do worker
(`reachable`/`connected`/`trade_allowed`/login/último erro) - no `_try_execute`
para aberturas e no scan do tick (`_check_null_retcode_phantoms`, a cada 30s,
últimos 30 min, uma vez por trade) para fechos phantom fora do caminho de
abertura.
O kill switch foi **desligado a 20/08 por decisão explícita do
utilizador** (o rearm agendado de 29/08 foi limpo) - a principal `nova_gestao`
passa a abrir ordens MT5 demo na janela de 28/08 e a referência do gate
(referência híbrida race+shadow da `nova_gestao`, limiar `min_reference_trades=10`)
começa a acumular; o limite diário (3%) e o auto-kill continuam ativos. O
risco base está em
1,0% e o high-watermark foi recalibrado para 1.000 €; o risco efetivo começa
assim em 1,0%, sem herdar o pico da conta paper antiga. O **shadow-paper** está
ativo por defeito para `auto_escada_3`, `auto_escada_5`, `auto_escada_3_hibrida`
e `nova_gestao` (a última alimenta a referência híbrida do gate): recebe sinais live
completos mesmo com o kill switch ligado (o modo manual foi removido no Item
12) e usa apenas as tabelas paper, nunca o motor MT5. As pendências de gestão têm marcadores separados para real, corrida
paper e shadow, para uma entrega isolada não consumir a outra. O objetivo do
utilizador é correr isto durante dias/semanas, afinar o reconhecimento de
mensagens e **mais tarde** trocar para uma conta real.

> ⚠️ A conta é **demo** - não há dinheiro real em jogo. Nunca mudar para uma
> conta real sem instrução explícita do utilizador.

## Manter a documentação atualizada (obrigatório)

**Toda a alteração/implementação que toque no comportamento do programa tem
que atualizar a documentação no MESMO turno - nunca concluir uma tarefa com
docs desatualizadas.** A documentação é o mapa que permite manter este
projeto vivo durante semanas e migrar para a VPS/conta real sem quebras.

- **AGENTS.md** - fluxos, processos em execução, arquitetura, reconhecimento
  de mensagens, autenticação, endpoints, variáveis de ambiente, número de
  testes da suite. Qualquer mudança de comportamento → atualizar aqui.
- **docs/TECHNICAL.md** - detalhe técnico: endpoints, BD, engine, backtest,
  convenções. Novas funcionalidades e mudanças de API → atualizar aqui.
- **docs/ESTRATEGIAS.md** - guia dedicado das estratégias atuais (registo,
  gestão do grupo, auto-gestão, resultados, onde aparecem). Mudanças ao
  registo/resultados → atualizar aqui.
- **docs/PUBLIC.md** - visão para não-programadores (utilizador final).
  Funcionalidades novas visíveis na dashboard → atualizar aqui.
- **docs/DEPLOY.md** - guia de deploy na VPS Windows (Python + MT5 + Caddy
  HTTPS + firewall). Mudanças ao arranque/deploy → atualizar aqui.
- **docs/CHANGELOG.md** - registo cronológico das alterações. **Toda** a
  alteração acrescenta uma entrada (data + o quê + nº de testes).
- **Endpoints/env vars novos** → registar sempre em AGENTS.md + TECHNICAL.md.
- **Número de testes**: quando a suite crescer/mudar, corrigir o número em
  AGENTS.md (e em qualquer doc que o cite). `tests/test_docs.py` falha se
  o AGENTS.md não mencionar o número real da suite.
- Checklist de fim de tarefa (ver regras de ouro): processos vivos + suite
  verde + BD limpa + **docs atualizadas**.

## Regras de ouro para o agente

1. **Reiniciar o servidor depois de mexer em Python** e confirmar com
   `curl /api/status`.
2. **Correr `pytest` antes de concluir** - a suite completa (atualmente 631 testes, atualizar se crescer) é a rede de segurança (parser, classificador, backtest, API, auth, observabilidade e estudos read-only).
3. **Não criar trades de teste sem limpar** os artefactos depois (trades,
   posições, sinais de teste na BD).
4. **Não guardar senhas em ficheiros de código** - vêm do `.env`/env vars.
5. **Nunca tocar na conta real** (a demo usa `MT5_LOGIN=5054644473` /
   `MetaQuotes-Demo`).
6. Após terminar uma tarefa: verificar processos vivos (server + listener),
   suite verde, estado da BD limpo e **documentação atualizada** (secção
   acima).
7. **GitHub local-only por agora:** mesmo existindo `origin`/workflow, não fazer
   API calls, push ou branch protection sem pedido explícito do utilizador.
8. **Credenciais coladas no chat ficam comprometidas:** revogar/rodar fora do
   repositório e nunca reutilizar ou guardar esses tokens no código.
9. **HELP sincronizada:** qualquer alteração em `app/strategies.py`, gestão do
   grupo, shadow-paper, risco ou conceitos visíveis exige atualizar
   `app/static/help.html` e `tests/test_frontend.py`; toda estratégia com
   `hidden=false` deve ter uma entrada em `HELP_FLOW` e uma explicação curta.

## Workspace e vault

Este projeto faz parte de `E:\Workspace`. As regras do workspace valem aqui e
prevalece sobre estas quando houver conflito: `E:\Workspace\AGENTS.md`.

**Lê a nota do vault antes de propor ou fazer seja o que for neste projeto.**
É lá que o dono anota o que quer a seguir e o que já ficou feito:
`E:\Workspace\Workspace-Vault\02 Projetos\Project NexGen.md`. Confirma a data no
topo da nota antes de lhe dares crédito: pode estar desatualizada, e nesse caso
perguntas em vez de inventar.

Os próximos passos de cada projeto estão anotados nas notas do vault, uma por
projeto, todas em `E:\Workspace\Workspace-Vault\02 Projetos\`.

## Mapa das regras detalhadas

O que está acima é o essencial e está sempre ativo. O resto deste ficheiro
antigo está em `.agents/rules/`, e o Antigravity carrega cada ficheiro por
conta própria quando a tarefa bate com a descrição. **Lê o que for preciso
antes de mexer**, e não adivinhes o comportamento de uma parte que não
tenhas lido.

| Ficheiro | Quando ler |
|---|---|
| `.agents/rules/operacao-e-vps.md` | arrancar, parar, reiniciar, logs, health check, backup da BD, Caddy, firewall, AutoTrading do MT5 |
| `.agents/rules/arquitetura-e-lanes.md` | `app/state.py`, `app/execution/**`, worker da 2ª conta, ordens, posições, event loop |
| `.agents/rules/reconhecimento-de-mensagens.md` | refinar reconhecimento, mensagens em `failed` ou ignoradas, `parser.py`, `manage.py` |
| `.agents/rules/fluxo-e-gestao-do-sinal.md` | pipeline de sinais, janela de edição, guards, gestão do grupo, BE+, kill switch, journal |
| `.agents/rules/backtest-e-analise.md` | `analysis.py`, `backtest.py`, estudos, MFE, offset do feed, risco defensivo |
| `.agents/rules/estrategias-registro.md` | `app/strategies.py`, menu, `manage_kinds`, risco, escolher que estratégia promover |
| `.agents/rules/estrategias-paper-e-fases.md` | `app/static/**`, páginas de teste, PaperRace, shadow-paper, painel da Fase 3 |
| `.agents/rules/dashboard-e-alertas.md` | `app/auth.py`, sessões, Caddy, firewall, Telegram, observabilidade |
