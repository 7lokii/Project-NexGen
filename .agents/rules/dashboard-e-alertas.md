---
trigger: model_decision
description: "A autenticacao por password da dashboard e do API do Project NexGen, o listener com token, como expor na web com Caddy e duckdns, e os alertas e a observabilidade: divergencia grupo contra bot, resumo diario, kill switch, alertas ao MT5 e no Telegram, e o que cada endpoint de operacoes devolve. Ler antes de mexer em app/auth.py, nas sessoes, no proxy, no Caddy, no firewall, nas notificacoes, no Telegram, ou em GET /api/operations e na observabilidade."
---

## Autenticação da dashboard (login)

A dashboard/API estão **protegidas por password** desde que `SIGNALBOT_DASHBOARD_PASSWORD`
esteja no `.env` (está: **utilizador `admin` · password no `.env`**).

- **Sem password** no `.env` → tudo aberto (uso local, testes).
- **Com password** → `GET /` serve a página de login; `POST /api/login` {password}
  devolve um cookie httpOnly de sessão (7 dias); `/api/logout` termina.
- **Toda a API e o WebSocket `/ws`** exigem sessão (401/4401 sem cookie).
- **Listener do Telegram**: autentica com header `X-API-Token` =
  `SIGNALBOT_API_TOKEN` (no `.env`, gerado) - sem browser/cookie. Se mudares
  a password da dashboard, o listener continua a funcionar (usa o token).
- `app/auth.py` tem as sessões em memória (single-process) e lê as env vars
  dinamicamente - os testes forçam auth OFF (`SIGNALBOT_DASHBOARD_PASSWORD=""`).

**Expor na web (VPS)**: a dashboard não serve HTTPS. Na VPS põe um reverse
proxy (Caddy é o mais simples - HTTPS automático com Let's Encrypt):
`dominio.com { reverse_proxy 127.0.0.1:8000 }`. Abre só a porta 443 (e 22/RDP
para ti) na firewall do Windows; **nunca** exponhas a porta 8000 nem o RDP
ao público. Sem domínio comprado, dá para usar **duckdns.org** (subdomínio
grátis - estado atual: `n3xg3n.duckdns.org` → `103.241.67.222`, Caddy em
`C:\caddy` com regras de firewall `NexGen HTTP 80`/`NexGen HTTPS 443`;
reiniciar o Caddy após reboot: `cd C:\caddy && .\caddy.exe start`).

## Alertas / notificações

- `_check_group_divergence()` em `state.py`: quando chega um resumo novo do
  grupo, compara o WR reportado com o WR do backtest do bot; se a diferença
  ≥ 10pp, envia uma notificação `risk` na dashboard.
- `_check_daily_summary()`: resumo diário à hora `summary_time` - inclui o
  bloco 🏁 da corrida da Fase 2 (`_phase2_race_line`): saldo e PF da
  `auto_escada_3` vs `nova_gestao` e quantos sinais faltam para os critérios
  do ESTRATEGIAS.md Parte VIII (N ≥ 20 · PF ≥ 1.5).
- Auto kill switch: perda diária > limite → pausa tudo (e, em papel,
  bloqueia a abertura de novas trades virtuais no dia). Se existir
  `killswitch_rearm_at`, o tick rearma a pausa uma vez no instante UTC
  persistido, limpa a chave e avisa feed/Telegram; o rearme nunca promove
  estratégia nem altera a conta real.
- A janela operacional UTC até sexta-feira 28/08/2026 termina em
  `2026-08-29T00:00:00+00:00`. `GET /api/window` agrega trades reais fechadas/
  abertas, P&L, dias restantes, ritmo face à meta 10–20 e o painel de promoção
  da `auto_escada_3` (gate forward N/PF/IC95/DD + ETA). O resumo diário inclui
  uma linha compacta com estes mesmos números.
- Correções explícitas de summaries são classificadas como
  `summary_correction`: o alvo é resolvido por reply Telegram ou período
  explícito inequívoco, os números do summary original são regravados e a
  divergência grupo↔bot é recalculada; alvo ambíguo nunca é adivinhado.

## Observabilidade operacional e CI (Itens 10 e 17)

- `GET /api/operations?days=N` agrega as métricas persistidas dos últimos
  `N` dias: exceções com stack trace/contexto, spread MT5 médio por símbolo e
  mês, eventos de ligação/reconexão por semana e tentativas de modify de SL
  aceites/recusadas por retcode. O mesmo payload aparece em
  `/api/diagnostics` e no painel "Saúde operacional" da tab Relatório.
- As tabelas SQLite são `operational_exceptions`, `spread_observations`,
  `mt5_connection_events` e `mt5_modify_events`. Um resultado vazio significa
  que ainda não houve observação, não que a taxa de falha seja zero; não se
  interpreta tendência antes de acumular pelo menos duas semanas.
- O `state.tick` amostra quotes MT5 no máximo uma vez por minuto; o loop de
  mercado/watchdog persiste exceções que escapem ao tick, reconexão ou
  watchdog, e o MT5 regista retcodes de modifies antes de alterar o SL na BD.
- `.github/workflows/ci.yml` corre `pytest -q` em cada push/PR, depois valida
  `tests/test_docs.py`. O dispatch manual `failure_probe=true` cria um teste
  deliberadamente falhado e confirma que o comando pytest devolve código
  diferente de zero, sem tornar o job normal verde artificialmente.
