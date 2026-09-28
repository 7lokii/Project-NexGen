# SPEC v3 — Pré-conta real

**Estado:** final para decisão humana · **não autoriza execução real**  
**Data de congelamento:** 29-08-2026  
**Fonte operacional:** `AGENTS.md`, `docs/ESTRATEGIAS.md` e os relatórios vivos da página `/auditoria`.

## 1. Limite deste documento

Este SPEC transforma os critérios técnicos numa decisão verificável. A conta principal está atualmente em **MT5 demo com execução real no broker**
(`MT5_DRY_RUN=0`), após autorização explícita do utilizador. Isto não altera o
limite deste SPEC: nenhum gate, endpoint, alerta ou resultado de estudo pode
promover a conta demo para dinheiro real sozinho.

A promoção exige uma confirmação humana explícita, registada fora de uma
inferência automática. Sem essa confirmação, o veredicto é sempre **NO-GO**.

## 2. Números congelados

| Parâmetro | Valor aprovado | Regra |
|---|---:|---|
| Saldo nominal de referência | **€1.000** | High-watermark inicial também €1.000 |
| Risco base por sinal | **1,0%** | O risco efetivo só pode reduzir; nunca aumentar acima do aprovado |
| Limite de perda diário | **3,0%** | Sempre ativo em conta real |
| Auto-kill | **Ativo** | Bloqueia novas entradas quando o limite é atingido |
| Kill switch manual | Obrigatório | Continua disponível e não pode ser contornado |
| Máximo de posições | **5** | Compatível com a estratégia N-pernas |
| Conta MT5 | Hedging (`margin_mode=2`) | Netting/desconhecida = bloqueio |
| Estratégia atualmente ativa na demo | `tp2_protect` | Não é autorização para promover uma conta real |
| Janela forward | **30 dias UTC** | Apenas `origin=shadow` |
| Mínimo forward | **N≥20 trades** | Dados ausentes = falha |
| Profit factor forward | **PF≥1,5** | Na janela congelada |
| Intervalo de confiança | **IC95% positivo** | Resultado médio estatisticamente compatível com ganho |
| Drawdown forward | **≤125%** do saldo inicial | Limite estrito |
| Validação Fase 3 | **10 primeiras trades** | Saída igual ao papel em ≥9/10 |
| Continuidade | Janela móvel de **20 trades**, revisão a cada 10 | PF≥1,5, P&L≥0, DD≤125% do backtest |

> O saldo é uma unidade de referência operacional. O broker continua a ser a
> fonte de verdade para saldo, equity, margem e permissões no momento de uma
> eventual decisão.

## 3. Gates obrigatórios (todos necessários)

### G0 — decisão e configuração

- confirmação explícita do utilizador;
- risco base 1,0%, saldo de referência €1.000 e high-watermark coerente;
- limite diário de 3%, auto-kill e `max_positions=5` ativos;
- estratégia e parâmetros aprovados sem alterações não revistas;
- conta ainda não pode mudar para real por ação automática.

### G1 — evidência forward shadow

- janela UTC de 30 dias congelada por `forward_evidence_started_at`;
- `origin=shadow`, sem misturar trades race ou MT5;
- N≥20, PF≥1,5, IC95% positivo e DD≤125%;
- comparação com a referência `nova_gestao` disponível;
- nenhum resultado `collect_more_evidence`, `insufficient_data` ou gate
  desconhecido.

### G2 — Fase 3 demo MT5

- estratégia candidata validada em conta demo hedged;
- 10 primeiras trades após promoção avaliadas;
- saída igual ao paper em pelo menos 9/10;
- zero posições sem SL, ordens duplicadas ou modifies recusados não explicados;
- retcodes de sucesso 10009/0;
- atraso sinal→abertura ≤2 minutos e P&L coerente com o backtest;
- zero rollback automático e pelo menos 30 trades demo/3–4 semanas sem
  incidente antes da decisão real.

### G3 — saúde operacional

- servidor vivo e sem reinício anómalo não explicado;
- MT5 conectado, conta hedged, AutoTrading permitido e reconciliação segura;
- listener Telegram vivo, autenticado e com heartbeat recente;
- feed de mercado disponível e timestamps UTC válidos;
- espaço livre acima do limite operacional e backup/restauração verificados;
- sem alertas `risk` não explicados, posições órfãs ou divergências de fill
  pendentes.

### G4 — alertas e observabilidade

- notificações de risco no feed configuradas;
- Telegram configurado e testado para incidentes críticos;
- journal/replay e fill-audit recolhidos quando necessários, sem usar replay
  para autorizar ordens;
- alertas de AutoTrading OFF, MT5 offline, listener offline, kill diário,
  retcodes e divergências ativos;
- export de auditoria e backup diário disponíveis.

## 4. Continuidade após uma eventual promoção

A decisão não é permanente. A cada 10 trades reais fechadas, recalcular a
janela móvel de 20 trades e rever:

- PF≥1,5;
- P&L≥€0;
- DD≤125% do backtest equivalente;
- divergências demo↔real explicadas;
- zero posições sem SL e zero duplicados;
- kill diário e auto-kill ativos;
- retcodes e alertas sem incidentes não explicados.

**Rollback obrigatório:** PF<1,0 em 10 trades consecutivas, duas ou mais
divergências estruturais, qualquer posição sem SL, ordem duplicada, AutoTrading
ou reconciliação insegura, ou qualquer quebra de um limite congelado. O rollback
volta à demo/`nova_gestao` conforme o mecanismo operacional existente e exige
nova revisão humana.

## 5. Decisão

- **GO:** somente se G0–G4 estiverem verdes, os números congelados coincidirem
  com a configuração observada e o utilizador confirmar explicitamente.
- **NO-GO:** qualquer gate vermelho, desconhecido, sem evidência, alerta não
  explicado ou ausência de confirmação humana.

A página `/auditoria` apresenta o checklist ao vivo e deve ser anexada à decisão
com data, período, valores observados e motivo de qualquer exceção. Este SPEC
não contém credenciais, não altera configuração e não é uma instrução para
ligar uma conta real.
