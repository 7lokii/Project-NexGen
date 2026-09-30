---
trigger: model_decision
description: "Como o Project NexGen reconhece as mensagens do grupo Telegram Golden Moves: os 3 formatos de sinal, o classificador de gestao e os seus kinds, a correlacao por reply, o protocolo passo a passo quando o dono pedir para refinar o reconhecimento, e o script para testar um padrao novo contra as mensagens reais. Ler sempre que o dono pedir para melhorar o reconhecimento, para a perda de mensagens, para mensagens que ficam em failed ou ignoradas, ou antes de mexer em app/signals/parser.py, app/messages/manage.py ou nos regexes de reconhecimento."
---

## Reconhecimento de mensagens (onde o utilizador vai pedir melhorias)

O utilizador vai pedir ajustes ao "reconhecimento das mensagens". Dois níveis:

### 1. Sinais de entrada (`app/signals/parser.py`)
Mensagens tipo `I'm buying XAUUSD 4400-4396 TP1 4402 TP2 4404 ... SL 4392`.
O Golden Moves usou **3 formatos** ao longo do tempo e o parser reconhece
os três:
1. **Atual (multilinha)**: `I'm buying XAUUSD\n\n4400-4396\n\nTP1 4402 … SL 4392`;
2. **Antigo (uma linha, emojis)**: `XAU USD SELL NOW 4737-4741 🥇TP1 4734
   🥈TP2 4732 … 🚫SL 4745` - símbolo com espaço, medalhas antes dos TPs,
   `🚫SL`/`SL/ invalid` como stop;
3. **Mais antigo (Targets)**: `Sell Gold Now 4123.5-4128.5 Targets 4121.5
   4119.5 4117 Open SL/invalid 4131.5` - Targets é lista sem rótulo
   (TP1/TP2/TP3 por ordem), `GOLD`→XAUUSD.
- `parse_or_error()` devolve `Signal` ou um `Signal` com `.error` preenchido.
- `validate_signal()` é partilhado com o LLM fallback - **sanity checks
  obrigatórios** (ordem dos TPs, SL do lado certo, distâncias).
- Typos conhecidos já rejeitados: `TP2 421`, `TP2 4486`, `TP3 4242`, zonas
  impossíveis (`4379-4775`).
- Zonas de reentrada ("Next Buy Zone 4045-4040") NÃO são sinais - ficam
  em `failed`/ignoradas (não têm TP1/TP2 rotulados).
- "Buy Stop 💣" / "Sell Stop 💣" **NÃO são ordens executáveis** - são avisos
  do grupo ("prepara-te para comprar/vender no stop") sem símbolo, entrada,
  TP ou SL (o sinal real vem depois, ~1h mais tarde). Ficam em `failed`/
  ignoradas - não são bug.

### 2. Gestão do grupo (`app/messages/manage.py`)
Mensagens de gestão NÃO esperam a janela de edição - agem na hora. O
classificador `classify()` devolve um `kind`:

| `kind` | Exemplo do grupo | Ação |
|---|---|---|
| `tp_hit` (tp 1..5) | "TP1 HIT +70 pips" | TP1 → SL das restantes a BE; outros → registado |
| `sl_hit` | "Oh wow SL HIT" | Registado + reconciliação |
| `close_tp1` | "Close TP1 here" (sem move SL) | Fecha só a perna 1 (restantes mantêm SL) |
| `close_tp` (`tp` 2..6) | "Close TP2 here", "Close TP5 when happy" | Fecha só a perna N ao mercado (restantes mantêm SL) |
| `protect` | "close first entries and move stop loss above best entries" | Fecha perna 1 + SL das restantes acima da entrada |
| `close_all` (`be` true/false) | "we're out", "out at BE" | Fecha tudo (BE → à entrada). A `tp2_protect` HONRA close_all desde 16/09 (as duas lanes saem ao mercado como a `nova_gestao`) - só `close_tp1`/`move_sl` continuam a ser ignorados por ela |
| `go` | "Do it now team" | Finaliza já a janela de edição e executa (caminho único auto desde o Item 12); sem sinal em espera só regista |
| `move_sl` (`sl` opcional) | "Adjust SL to 4331", "Move SL to 4064" | Move o SL de todas as pernas abertas para o nível indicado (sem nível = nudge registado, sem ação) |
| `summary` | "**Weekly summary** ... 26 Trades Sent ..." | Guarda ground truth em `group_summaries` |
| `recount` | "Signal #1: Sell at 4357. Price went up to 4362, then dropped to 4351. +110 pips" | Guarda o recount por trade em `trade_recounts` e audita contra o fill real do mesmo dia (entrada/pips/direção → alerta; excursão/reversão vs candles; sem trade → notificação informativa com causa provável) |

**Edições TP1/SL do sinal alvo** (`_handle_tp_sl_update`): mensagens tipo
`TP1 4734 SL 4745` que RESPONDEM a um sinal (reply_to_msg_id) atualizam o
TP1/SL do sinal alvo (ground truth) e movem o SL das pernas abertas da trade
correspondente - antes ficavam em `failed`.

**Correlação com a mensagem original (reply do Telegram):** o listener captura
o `reply_to_msg_id` de cada mensagem (Telethon: `message.reply_to`) e envia-o
no POST. O handler `_handle_management` usa essa relação para escolher o alvo:
1. a trade do sinal a que a mensagem **responde** (correlação exata - duas
trades XAUUSD abertas não confundem o alvo); 2. trade aberta do mesmo símbolo;
3. trade aberta mais recente. Se o sinal alvo ainda estiver na janela de
edição (sem trade), a instrução vai para a tabela `pending_management` e é
aplicada automaticamente quando a trade do sinal abrir (`_apply_pending_management`
em `_try_execute`).

Padrões importantes do classificador:
- **A ordem importa**: summaries primeiro (contêm "volume"/"watch"), depois
  SL/TP hits (incl. "TP5 SMASHED/KISSED/tapped" e "SL already hit"), depois
  close_all/out at entry/breakeven, depois protect/close_tp1/close_tp ("Close
  TP2..TP6 here").
- Apóstrofos tipográficos são normalizados (`we’re` → `we're`).
- O classificador é **conservador**: um falso "close_all" fecha posições
  reais. Ao adicionar um padrão novo, testar contra as mensagens reais da BD.
- `Signal.reply_to_msg_id` guarda a relação de resposta (id do sinal alvo);
  `pending_management` (BD) fila instruções cujo alvo ainda não tem trade.
- A dashboard mostra essa relação no histórico: "📨 gestão · → responde ao
  sinal 10:53 XAUUSD".
- Mensagem de gestão **sem alvo** (sem trade aberta e sem sinal a que
  responder) dispara uma notificação `risk` (⚠️ "Gestão do grupo sem alvo") -
  nunca fica silenciosa.
- **Roteamento fail-safe de gestão**: reply do Telegram continua a ser a
  correlação exata; sem reply, um símbolo com trades de sinais diferentes é
  considerado ambíguo e não recebe um `close_all`/`protect` por adivinhação -
  gera alerta para o operador confirmar.
- **Atribuição de saída**: cada perna/trade fechada regista `exit_reason`
  (sl/tpN/protect/close_all/close_tp1/close_tp/move_sl/killswitch/manual) e
  `exit_msg_id` (id da mensagem de gestão que causou o fecho) - colunas novas
  em `positions`/`trades`/`paper_positions`/`paper_trades` (migração
  automática no arranque). A tabela de trades da dashboard mostra a razão do
  fecho e o **contrafactual "sem gestão"** (`counterfactual_pnl` = P&L da
  variante `nova` da corrida paper para o mesmo sinal) - o link ao vivo
  entre o backtest e o que a gestão realmente acrescentou.
- **`manage_kinds` na conta real**: quando a estratégia ativa não honra um
  kind (ex.: `tp2_protect` não honra `close_all`), a instrução é registada e
  notificada ("não aplica à estratégia atual") mas NÃO fecha posições - a
  corrida paper continua a aplicar nas estratégias que a honram.

### Testar um novo padrão contra as mensagens reais

As mensagens reais estão na tabela `signals`. Para ver como o classificador
trata cada uma:

```python
from app.config import DB_PATH
from app.db import DB
from app.messages.manage import classify
db = DB(DB_PATH)
for r in db.conn.execute("SELECT raw_text FROM signals WHERE status='management' OR status='historical'").fetchall():
    print(classify(r["raw_text"] or ""), "|", (r["raw_text"] or "")[:70].replace("\n", " "))
```

## Refinar o reconhecimento (protocolo do utilizador)

O utilizador vai mandar **"refinar o reconhecimento"** (ou variantes: "o grupo
mandou X e o bot não percebeu", "há mensagens que ficam failed", "melhora o
reconhecimento das mensagens"). Quando isso acontecer, executa ESTE protocolo
sem pedir esclarecimentos:

1. **Diagnóstico** - corre um script que lista as mensagens que o bot perdeu:
   ```python
   from app.config import DB_PATH
   from app.db import DB
   from app.messages.manage import classify
   from app.signals.parser import parse_or_error
   db = DB(DB_PATH)
   for r in db.conn.execute(
       "SELECT id, raw_text, status FROM signals ORDER BY received_at DESC LIMIT 200"
   ).fetchall():
       txt = r["raw_text"] or ""
       sig = parse_or_error(txt, source="x", signal_id="x", received_at="")
       if r["status"] == "failed" or (sig and sig.error) or classify(txt):
           print(f"[{r['status']}] {classify(txt)} | {sig.error or ''} | {txt[:90]}".replace("\n", " "))
   ```
   Também ver o log do listener (`tail -50 listener.log`) para mensagens
   ignoradas (o log mostra o texto) e o feed da dashboard para notificações
   "sem alvo". Mensagens que são conversa pura não são bug - estão na tab
   "🚫 Ignoradas" (auditoria).
2. **Classifica o problema**:
   - mensagem de **entrada** mal parseada → `app/signals/parser.py`;
   - mensagem de **gestão** não classificada → `app/messages/manage.py`;
   - **formato novo** que nenhum dos dois cobre → decide se é sinal ou gestão
     e adiciona o padrão ao módulo certo (o parser é regex; o classificador é
     regex por `kind` - ver a tabela acima).
3. **Implementa de forma conservadora**: um falso "close_all"/"protect" fecha
   posições reais - os padrões novos devem casar a mensagem do grupo SEM
   apanhar conversa. Testa sempre contra as mensagens reais da BD (script
   acima) antes e depois da alteração.
4. **Teste de regressão** - adiciona o caso ao ficheiro de testes certo:
   `tests/test_management.py` (classificador), `tests/test_api.py` (fluxo
   API/execução) ou `tests/test_backtest.py` (sim).
5. **Suite completa** - `.venv/Scripts/python -m pytest -q` (631 testes).
6. **Reiniciar o servidor** (código Python não recarrega em produção) e
   confirmar com `curl http://127.0.0.1:8000/api/status`.
7. **Responder ao utilizador** com: o que estava a falhar, o que mudou, e o
   resultado dos testes.

> Nota: mensagens antigas do histórico não voltam a ser reprocessadas (dedup
> por message_id). Alterações ao reconhecimento aplicam-se às mensagens
> NOVAS do grupo - se o utilizador quiser re-analisar o histórico, limpar as
> linhas antigas da BD antes de reiniciar o listener (cuidado para não apagar
> trades reais).
