# Project NexGen — O que é isto? (para quem não é programador)

Este documento explica o projeto em linguagem simples, sem código. Se és o
Rúben ou o Miguel, começa por aqui.

---

## Em uma frase

Um programa que **lê os sinais do grupo de Telegram**, valida rapidamente se a
mensagem incompleta foi **editada** e depois — conforme o modo escolhido —
**faz a trade sozinho** ou **mostra-te o sinal para tu aprovares**.

> ⚠️ **Importante e honesto:** o projeto suporta simulação/paper e execução
> numa conta **demo MT5**. O servidor da VPS pode enviar ordens para a demo;
> isto não é dinheiro real, mas continua a exigir as mesmas precauções. Uma
> conta real nunca é ativada sem instrução explícita.

---

## O que o programa faz, passo a passo

1. **Recebe o sinal** — ou clicas num botão de demonstração na dashboard, ou
   o programa **lê o grupo de Telegram** (ver guia abaixo).
2. **Entende o sinal** — o programa "lê" a mensagem e extrai os dados:
   qual o par (ex.: EURUSD), se é compra ou venda, a entrada, o stop loss e
   os alvos (**TP1 a TP5**, conforme o grupo enviar). Funciona em português
   e inglês.
3. **Espera por edições (8 segundos por defeito)** — uma mensagem incompleta
   pode ser corrigida pelo grupo logo depois de enviada. O relógio reinicia
   quando chega uma edição. Um sinal já completo não espera: em Auto segue
   imediatamente para as verificações de risco; o valor é configurável na
   dashboard (`edit_window_seconds`).
4. **Verifica o risco** — antes de qualquer trade, confirma que está tudo
   dentro dos limites (ver abaixo).
5. **Decide** — em modo **Auto** faz a trade sozinho; em modo **Manual** aparece
   na dashboard com os botões **Aprovar** e **Rejeitar**.
6. **Reage às instruções do grupo** — além dos sinais, o programa lê as
   mensagens de **gestão** do grupo e age nelas sozinho (ver secção abaixo).

---

## A estratégia das "N entradas" (TP1 a TP5)

Cada sinal abre **uma posição por alvo** que o grupo enviar (até 5):

- A **primeira** fecha no **TP1** (lucro mais pequeno);
- As **seguintes** ficam a correr até aos seus alvos (**TP2, TP3, …**) — lucros
  maiores, mas só se o preço lá chegar.

E há uma proteção importante: **quando a primeira fecha no TP1, todas as
restantes passam a ter o stop loss na entrada** (ficam "a zeros"). Ou seja,
mesmo que o preço volte atrás, essas posições não perdem dinheiro — cada uma
corre depois para o seu próprio alvo.

O grupo também **dá instruções ao vivo** e o programa obedece:

| O grupo diz… | O programa faz… |
|---|---|
| "TP1 HIT" | Move o stop loss das posições restantes para a entrada (ficam protegidas) |
| "close first entries and move stop loss above best entries" | Fecha a **primeira posição** (bloqueia lucro) e sobe o stop das restantes para **acima da entrada** |
| "Close TP1 here" (sem "move stop loss") | Fecha só a primeira posição; as restantes mantêm o stop |
| "Adjust/Move SL to X" | Move o stop loss de **todas** as posições abertas para o nível indicado |
| "TP1 4059 SL 4047" (como resposta a um sinal) | Corrige os níveis do sinal (edição/confirmação do grupo) |
| "we're out" | Fecha **tudo** ao preço atual; "out at BE" protege as pernas restantes na entrada (não força um fecho imediato) |
| "SL HIT" | Regista e reconcilia com o broker |
| Resumo diário/semanal | Guarda o win rate que o grupo reporta (ver "páginas de teste") |

Estas mensagens são tratadas **na hora** (não esperam o minuto da janela de
edição — seria tarde demais) e aparecem no histórico com o ícone 📨.

**E o programa sabe a que sinal a mensagem responde?** Sim. Quando o grupo
responde a uma mensagem no Telegram (ex.: "SL HIT" como resposta ao sinal de
compra), o programa captura essa relação e age na trade **daquele sinal** —
mesmo que haja várias trades abertas do mesmo par. Se a mensagem chegar antes
de a trade abrir (sinal ainda a aguardar edições), a instrução fica guardada
e é aplicada automaticamente quando a trade abrir.

> Nota: o risco total por trade é dividido pelas N posições — abrir mais alvos
> não aumenta o que arriscas se o stop loss for atingido.

---

## O slider de risco

Na dashboard há um **slider de risco** (ex.: 0.1% a 5%).

Ele não escolhe o "tamanho" da trade diretamente — escolhe **quanto estás
disposto a perder se o stop loss for atingido**, em percentagem da conta.

Exemplo: com 10 000 € e risco de 1%, cada trade arrisca no máximo 100 €. Se o
stop loss estiver longe, o programa usa menos "peso"; se estiver perto, usa
mais. O risco em dinheiro é sempre o mesmo.

**Regra de ouro:** começa com 0.1–0.5% e só sobe depois de veres resultados
na simulação. O slider não te protege de sinais maus — só define o quanto te
dói quando eles forem maus.

---

## As proteções (porque é que isto não rebenta à toa)

| Proteção | O que faz |
|---|---|
| **Kill switch** | Um botão vermelho que pausa tudo na hora. As posições já abertas continuam a ser geridas (para não as largar ao acaso), mas nada de novo é aberto. |
| **Limite de perda diária** | Se hoje já perdeste mais do que o limite (ex.: 3%), o programa **pausa sozinho** e avisa. Só volta quando alguém reativar manualmente. |
| **Máximo de posições** | Limita quantas trades podem estar abertas ao mesmo tempo (default 5; configurável). |
| **Mesmas regras em ambos os modos** | Mesmo no modo manual, clicar "Aprovar" **não** contorna as verificações de risco. |

### A proteção híbrida (em teste)

Existe uma estratégia experimental chamada **Auto escada 3 híbrida**. Ela usa
primeiro a proteção automática (escada e trailing, sem esperar por uma pessoa)
e só aproveita uma mensagem `protect` do grupo quando essa mensagem melhora o
stop loss. Nunca recua uma proteção que já foi conquistada. A página de teste
e o Relatório mostram o vencedor, os pips/segundos de atraso e a atribuição
causal de cada evento; a estratégia não substitui a principal nem envia ordens
para a conta demo sem promoção explícita.

O estudo da variante mais agressiva, **Auto escada 5**, também corre uma grelha
segura de 27 combinações do modo defensivo. Como nenhuma baixou o drawdown do
período antigo para menos de 50% e apareceu um pico isolado, o bot manteve os
valores atuais — não escolheu um valor só porque ganhou num ponto do histórico.

No Item 3, testámos a **confirmação automática** da Auto escada 3 ligada e
desligada, nas três janelas históricas, sem misturar dados entre variantes.
Ligá-la reduziu o drawdown antigo de **312,9% para 278,3%** e baixou o PF
atual só de **1,27 para 1,26** (0,79%, dentro do limite de 10%). Por isso,
essa estratégia passou a usar a confirmação por defeito. Isto altera apenas
a estratégia de teste/paper — a principal `nova_gestao` e a conta demo não
foram promovidas.

Antes de uma estratégia automática poder passar da corrida paper para a conta
demo MT5, o programa percorre uma janela de 10 trades reais: compara a saída
com o paper (tem de coincidir em pelo menos 9), verifica se todas as posições
têm stop loss, se não há ordens duplicadas e se o broker aceitou os modifies.
Se um gate falhar, volta sozinho à estratégia principal `nova_gestao`. Isto é
uma proteção da conta demo; nunca promove uma conta real.

---

## Operação 24/7 e travões de segurança

- **Conta MT5 hedged**: a estratégia abre uma perna independente por TP. Em
  execução real o bot verifica `margin_mode=RETAIL_HEDGING`; uma conta netting
  ou um modo desconhecido fica bloqueado e aparece como aviso vermelho, sem
  enviar ordens.
- **Logs**: `server.log`, `listener.log` e os respetivos `.err.log` rodam
  automaticamente por tamanho (10 MiB e 5 cópias por defeito). Os limites são
  `SIGNALBOT_LOG_MAX_BYTES` e `SIGNALBOT_LOG_BACKUP_COUNT`.
- **Espaço e backups**: há um check diário de espaço livre (10 GiB por defeito)
  no feed/Telegram. O teste mensal
  `.venv/Scripts/python backup_db.py --restore-check` restaura o backup para
  uma BD temporária e valida a integridade; um backup corrompido gera alerta.

## Saúde operacional e CI

Na tab **Relatório** há o painel **🩺 Saúde operacional**. Ele reúne, sem
interferir nas trades, as exceções recentes (com o contexto), o spread MT5
médio por símbolo/mês, as reconexões por semana e os modifies de stop recusados
pelo broker. Quando ainda não há amostras, o painel diz isso explicitamente;
uma tendência só é considerada depois de pelo menos duas semanas de dados.
Os mesmos dados podem ser consultados por `GET /api/operations?days=90`.

O projeto também tem CI no GitHub: cada push ou pull request corre a suite
completa e valida as regras de documentação. Existe um teste manual opcional
que cria uma falha deliberada apenas para confirmar que o CI a assinala; ele
não é executado nas alterações normais.

## Modos de funcionamento

- **Manual** — o sinal aparece na dashboard e tu decides (Aprovar/Rejeitar).
  É o modo mais seguro para começar.
- **Auto** — o programa faz tudo sozinho, desde que passe nas verificações de
  risco. Mais rápido (importante se os sinais "batem ao início"), mas só deves
  usar quando confiares nos sinais.

---

## Onde vês tudo

A **dashboard** é uma página web (no teu PC, ou mais tarde numa VPS acessível
pelo telemóvel), organizada em separadores. **Quando o programa corre na VPS
com login ligado, a dashboard pede uma password** (ecrã de login 🔐) — só
entra quem tiver a palavra-passe, e há um botão "🚪 Sair" no topo para
terminar a sessão. O botão de **Kill switch** (pausar tudo) continua sempre
acessível depois de entrares.

A dashboard tem o **estilo Cyberpunk 2077** (cores neon **roxo e vermelho**,
fontes técnicas e um efeito de ecrã CRT subtil) — é o único tema do site.
Em ecrãs largos, o fundo mostra **terminais de código animados** (como um
monitor antigo) com o **tema do próprio bot**: sinais XAUUSD com TP1..TP5 e
SL, ticks de preço, fills e saldo/equity, com uma retícula ao centro. A
**navegação está numa barra fixa no topo** (itens horizontais; no telemóvel
abre com o ☰) e o **terminal de estado está numa barra fixa em baixo**: os
badges (modo, estratégia, MT5, execução), os **preços ao vivo** (GOLD,
SILVER, EURUSD… a verde/vermelho conforme o movimento e com uma **barrinha
da variação % do dia** — passa o rato para veres o detalhe), os
utilizadores online + relógio, o **🛑 Kill switch** e o **Sair**. Navegas
com as **setas ↑↓ + Enter** (ou com o rato) entre a **página principal**
(a conta MT5 real, com a estratégia ativa — **começa a partir de hoje**,
sem simulação do passado) e o **dropdown ▚ ESTRATÉGIAS** no topo — onde
vivem as **6 estratégias atualmente visíveis** (`nova_gestao`, `tp2_protect`,
`auto_3`, `auto_escada_3`, `auto_escada_3_hibrida` e `auto_escada_5`), cada uma
com a sua página de simulação (saldo a começar há 1 mês com 1.000 €, curva,
comparação e corrida paper ao vivo). As restantes estratégias continuam no
registo para backtest/compare ou para o contrafactual paper, sem serem
apagadas. A **TP1..TP5 + gestão** é a **referência** (a estratégia ativa na
config, que a principal executa e que aparece na corrida paper) — e o bot
mostra quem está a ganhar entre as visíveis ao vivo. Há ainda uma página
**HELP** (no
menu, logo a seguir ao diagnóstico) que explica cada estratégia com cartões
interativos
(clica
para expandir: como funciona + números reais dos últimos 30 dias) e um
glossário de conceitos (TP, SL, breakeven, gestão do grupo, kill diário,
corrida paralela). Para a versão completa e estática (incluindo as
estratégias ocultas e os resultados por janela), há o documento
**`docs/ESTRATEGIAS.md`** no projeto. Na corrida, quando a estratégia automática (Auto escada
3) e a referência saem **diferentes no mesmo sinal** — por exemplo, o grupo
mandou "sair" e a automática continuou — o feed mostra uma notificação
**⚖️ Divergência** com o P&L de cada lado. A página principal tem um
**painel da Fase 2** com o saldo e o PF da automática vs a referência ao
vivo, o contador de sinais resolvidos (N) e o progresso para os critérios
de aprovação (N ≥ 20 · PF ≥ 1.5); a tab **Relatório** mostra a tabela de
todas as divergências por sinal (razão da saída e P&L de cada lado) e a
tabela **⚡ Real vs backtest** — quando a conta demo fecha trades, cada uma
aparece comparada com o backtest do mesmo sinal (diferença de entrada,
preço real vs o fill do simulador, e de saída, fecho real vs o nível
esperado). O **resumo diário** (à hora configurada) inclui também o estado
da corrida da Fase 2: saldo e PF da automática vs a referência e quantos
sinais faltam para os critérios. Cada página de teste da Fase 2 tem ainda
um botão **"⬇️ Exportar CSV"** que descarrega as trades de papel da
estratégia e da referência (preços, timestamps, SLs, motivo de saída)
para análise fora da dashboard. A página do **TP1→BE + sair no
TP2** mostra ainda um **exemplo real de uma trade**: o gráfico com o
caminho do preço, a entrada, o SL, o TP1 e o TP2, e a linha do tempo de
onde cada evento teria acontecido.
Cada página de teste tem um botão **"⬆️ Promover a principal"** para passar
essa estratégia para a conta real (na página da própria estratégia ativa o
botão não aparece — ela já é a principal); a principal mostra um **badge com
a estratégia ativa** (com clarão quando muda) que indica também **quais os
parâmetros em uso na execução real** (nº de pernas, fechar no TP, lote fixo
e gestão do grupo) e um **aviso vermelho se alguma das estratégias de teste
estiver a ganhar à principal** na corrida paper. Quando promoves uma
estratégia, fica uma **notificação no feed** a dizer exatamente o que
mudou na execução real. Se a estratégia guardada já não existir (foi
removida), o badge avisa a **vermelho** "config órfã" e mostra qual é a
estratégia de recurso a correr — nunca engana.
Na tab **🔧 Diagnóstico** aparece também o **"📚 Último replay do
histórico"**: a data e o total de mensagens/sinais/gestão/ignoradas que o
bot reencaminhou no último arranque.
Cada página de teste mostra **dois horizontes**: a simulação do **último
mês** (seletor de 7/30/90 dias) e o bloco **"📜 Desde o início do grupo"**
— o mesmo replay sobre **todo o histórico** de sinais que o bot conhece —
mais a **tabela de trades de papel** da corrida paralela (cada trade
fechada com o seu P&L, símbolo, entrada e lotes). O **comparativo de cada
página** desenha agora 3 estratégias nos mesmos sinais — **TP1..TP5,
TP1..TP5 + gestão (a principal) e fechar no TP2** — com o P&L de cada uma
na legenda, para veres o que a gestão do grupo teria dado face às outras.
Na tab **🔧 Diagnóstico** há também um botão "🧹 Limpar reinícios" (com
confirmação) para apagar à mão o histórico de reinícios do listener, e um
bloco **"⏱️ Atraso das edições do grupo"** que mostra quanto tempo o grupo
demora a editar as mensagens (mínimo, média, mediana, p90, máximo) face à
janela atual de 8s — a mediana a verde se estiver dentro da janela e a
vermelho se não, para saberes se a janela está bem calibrada com dados
reais em vez de por tentativa.
Se o grupo **editar uma mensagem depois de o bot já ter executado** a
trade (janela passada), aparece um **alerta vermelho** na dashboard a
avisar que a entrada pode estar desatualizada. E o contrário também:
se um sinal chegar **incompleto** (ex.: entrada sem TP/SL ainda) e o
grupo completar a mensagem nos minutos seguintes, o bot **recupera o
sinal** e abre a janela de espera de novo com o conteúdo certo.
As **páginas de teste** do menu terminal mostram a comparação das estratégias
(nova, nova + gestão — a principal — e fechar no TP2) sobre os sinais reais
do grupo, do período escolhido (7/30/90 dias) **e** um bloco **"📜 Desde o
início do grupo"** sobre todo o histórico, para veres se o edge se mantém.
A **simulação do último mês** ("se começasses há 1 mês com X €, hoje terias
Y €") está nessas mesmas páginas, tal como o **walk-forward** (o edge
mantém-se ao longo do tempo?) e os **estudos de risco** (risco %/saldo do
sim — o saldo default são os 1.000 € próprios de cada estratégia). É
simulação pura: nunca abre trades reais.
Cada página de teste tem o seletor de período (7/30/90 dias ou **tudo**),
que fica **guardado** — recarregues a página e ele mantém a escolha. Quando
o bot arranca e termina de ler o histórico do grupo, aparece no feed a
notificação **"📚 Replay do histórico concluído"** com o total de mensagens
e de sinais/gestão/ignoradas reencaminhados.

Ao entrar, a dashboard faz um **arranque tipo terminal** (boot sequence):
linhas a aparecer com som de teclado, mostrando o estado real do sistema
(MT5 ligado, AutoTrading, modo de execução) antes de revelar os números.
As **notificações também têm som**: um sinal agudo quando uma trade abre,
um alerta grave quando algo corre mal (risco, MT5 offline, AutoTrading
desligado). O som respeita as regras do navegador — desbloqueia no primeiro
clique, e silencia-se no próprio navegador (mute do separador).

- **Sinais** — o histórico completo dos sinais recebidos do Telegram, com
  filtro por grupo, estado (à espera de edições, pendente, histórico, etc.)
  e botões Aprovar/Rejeitar;
- **Posições** — as trades abertas em tempo real;
- **Histórico** — todas as trades fechadas, com P&L;
- **Relatório** — win rate, profit factor, risco/recompensa, consistência
  **por grupo**, a **curva de equity** (evolução do P&L ao longo do tempo),
  o **drawdown máximo** e um **gráfico de barras com o P&L e o win rate de
  cada dia**, para o período que escolheres (7/30/90 dias);
- **🧪 Páginas de teste (menu)** — o backtest A/B/C (TP1..TP5 · **TP1..TP5
  + gestão do grupo** · fechar no TP2), o **walk-forward** (o edge mantém-se
  ao longo do tempo?) e os **estudos de risco** (risco %/saldo do sim). A
  coluna "+ gestão" simula no backtest as instruções do grupo (fechar a 1ª
  posição, subir o stop, sair tudo) para medir quanto do resultado vem da
  gestão deles;
- **🚫 Ignoradas** — auditoria das mensagens do grupo que o bot decidiu
  **não registar** (conversa: "Good morning", "+50 pips", links, avisos).
  Nenhum sinal nem instrução de gestão fica aqui — só conversa — e nunca
  entra na execução. Serve para veres tudo o que o grupo diz, mesmo o que
  o bot ignora;
- **No header** (à direita da marca) vês os badges de estado: modo
  auto/manual, a estratégia ativa, a conta MT5 real (login · saldo) e o
  modo de execução (real/dry-run). À **direita do ecrã** há um terminal
  com **quem está ligado à dashboard** (`👥 admin · admin` — cada
  separador/telemóvel aberto conta e desaparece quando fecha), o
  **relógio**, o **kill switch** e o botão **Sair**;
- **🔧 Diagnóstico** — estado do sistema num relance: ligação ao **MT5**
  (conta, saldo/equity, AutoTrading), **listener do Telegram** (vivo ou
  OFFLINE, há quanto tempo recebeu a última mensagem do grupo), servidor
  (uptime), preços reais/simulação, login, base de dados e um **gráfico de
  uptime das últimas 24h** (listener e MT5 ligados/em baixo, com % de
  uptime). Se o listener cair, fica vermelho aqui em ~5 segundos;
- **Auto-recuperação**: se o **listener do Telegram morrer**, o servidor
  **reinicia-o sozinho** em segundos. O servidor também encerra de forma
  limpa os seus loops quando é reiniciado, para não ficar com tarefas antigas
  a gerir preços em duplicado. Se o Telegram pedir um **código de
  login novo**, aparece um aviso na dashboard com um campo para introduzires
  o código que o Telegram enviou para a app — o listener volta a ligar
  sozinho depois disso;
- um feed de **notificações** sempre visível (trades abertas/fechadas,
  bloqueios, resumo diário). Quando uma instrução de gestão chega **sem
trade aberta** nem sinal a que responder, aparece um alerta ⚠️ no feed —
  nunca fica silenciosa.

Também podes receber **notificações no Telegram** (quando o bot abre ou fecha
uma trade, o resumo do dia, ou alertas de risco), se ligares um bot
(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` no `.env` — cria o bot com
@BotFather e envia-lhe `/start` para obteres o teu chat id).

**Alertas OFFLINE no telemóvel**: quando o **MT5 desliga** (ou o AutoTrading
desliga), o programa envia-te um DM no Telegram (Saved Messages) — funciona
logo de início, sem configuração, porque usa a tua própria conta. E se o
**listener do Telegram** cair e for reiniciado automaticamente, também fica
registado no feed — nunca ficas sem saber que algo esteve em baixo.

### Mensagens apagadas

Se alguém **apagar** a mensagem do sinal no grupo, o programa deteta isso,
marca o sinal como apagado e — se a trade ainda estiver aberta — **fecha-a ao
preço atual**. Não ficas com posições órfãs de sinais que já não existem.

---

## Ligar o teu grupo de Telegram (testar no teu PC, sem VPS)

Isto é tudo local — nada vai para a Internet. Precisas de 4 coisas, uma vez só:

### Passo 1 — Criar as credenciais de API (5 minutos, grátis)

1. Entra em **https://my.telegram.org** com a tua conta.
2. Clica em **API development tools**.
3. Preenche o formulário (nome da app: qualquer coisa, ex.: `signalbot`).
4. Guarda o **api_id** e o **api_hash** que aparecem.

> ⚠️ Não partilhes o api_hash com ninguém.

### Passo 2 — Entrar no grupo com a tua conta

O programa usa a **tua conta** para ler o grupo (os grupos de sinais não
aceitam bots). Abre o link que o Rúben te der no Telegram e **entra no grupo**
com a conta normal. Se ainda não estiveres lá, o programa também consegue
entrar por ti pelo link.

### Passo 3 — Configurar o programa

Na pasta do projeto, cria um ficheiro chamado `.env` com:

```
SIGNALBOT_TG_API_ID=1234567
SIGNALBOT_TG_API_HASH=escreve_aqui_o_api_hash
SIGNALBOT_TG_PHONE=+3519XXXXXXX
SIGNALBOT_TG_GROUP=https://t.me/+o_link_do_grupo
```

- `SIGNALBOT_TG_PHONE` — o teu número com indicativo (ex.: `+351...`).
- `SIGNALBOT_TG_GROUP` — o grupo. Pode ser o link de convite, o `@username`
  ou simplesmente o **nome** (ex.: `Golden Moves`) — o programa procura o chat
  nos teus chats. A tua conta tem de ser membro do grupo.

Vários grupos ao mesmo tempo? Usa `SIGNALBOT_TG_GROUPS` com os grupos
separados por vírgula, e na dashboard há um seletor para filtrar os sinais
de cada grupo.

### Passo 4 — Correr (duas janelas de terminal)

**Janela 1 — a dashboard/API:**

```
.venv/Scripts/python -m uvicorn app.main:app --reload
```

**Janela 2 — o leitor do Telegram:**

```
.venv/Scripts/python -m app.telegram_listener
```

Na primeira vez, o leitor pede-te o **código de verificação** que o Telegram
envia para o teu telemóvel. Escreve-o e confirma. A partir daí fica a ouvir o
grupo. (Se tiveres 2FA, pede também a password.)

### O que vais ver

- Uma mensagem incompleta que pareça um sinal aparece na dashboard com
  **⏳ 8s** por defeito — é a janela de edição a funcionar.
- Se o grupo **editar** a mensagem, a dashboard mostra a versão nova.
- Depois dos **8s** (ou do valor configurado), em modo **Manual** aparece com
  botões Aprovar/Rejeitar; em modo **Auto** é executado (se passar nas
  verificações de risco).
- Mensagens que não pareçam sinais (conversa normal) são ignoradas — vê no
  terminal do leitor os `[skip]`.

## Como começar a testar (sem gastar nada)

1. Corre o programa no teu PC (ver `README.md` para os passos técnicos).
2. Liga o **listener do Telegram**: ele repõe por defeito o histórico completo
   do grupo (mensagens marcadas como "histórico", sem executar trades) e
   continua a receber as novas. Se precisares de limitar o arranque, usa
   `SIGNALBOT_TG_HISTORICAL_LIMIT`.
3. Experimenta o modo manual: aprova e rejeita sinais.
4. Liga o modo **Auto** com risco baixo (0.1–0.5%) e vê o que acontece.
5. Abre uma **página de teste** (menu) ou o **Relatório** — usa os sinais
   reais do grupo com os preços históricos reais e mostra se a estratégia
   tem edge.
6. No **Relatório** vês a qualidade por grupo ao longo dos dias.
7. Numa **página de teste**, olha para a comparação de 3 colunas: se a
   coluna "+ gestão" ganhar claramente, é sinal de que seguir as instruções
   do grupo (e não só os alvos) faz a diferença — que é exatamente o que o
   programa faz automaticamente com as mensagens de gestão.

Só quando tiveres confiança (e dados reais de sinais) é que faz sentido pensar
em ligar isto a uma conta real.

---

## Perguntas frequentes

**Isto dá dinheiro garantido?**
Não. Ninguém tem isso. O programa só executa o que lhe é dado — a qualidade dos
sinais é que decide. Por isso é que se testa primeiro em simulação.

**Posso perder dinheiro?**
Nesta versão, não — é tudo simulado. Numa versão real, sim, podes perder o que
arriscas (e o slider serve precisamente para controlar isso).

**O programa "analisa o mercado com AI"?**
Não. Isso é marketing. O programa interpreta os sinais e gere o risco. Prever
preços com "AI" não é algo fiável — não construímos isso, de propósito.

**E as contas financiadas (FTMO/Stmo)?**
São um assunto separado e arriscado: têm regras duras (perda diária, drawdown,
proibição de hedging, muitos proíbem automação). Antes de pensar nisso, valida
os sinais em simulação durante semanas. Não compres contas financiadas com base
em promessas de "50% numa semana" — isso é o padrão de quem quer vender-te algo.
