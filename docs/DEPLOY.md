# Project NexGen — Deploy numa VPS Windows

Guia completo para correr o Project NexGen 24/7 numa VPS Windows e expor a
dashboard na web com **HTTPS + login**, sem dar acesso remoto (RDP) a mais
ninguém.

> ⚠️ O bot e o MetaTrader 5 **têm de estar na mesma máquina** (o pacote
> Python `MetaTrader5` liga ao terminal pela porta local). Por isso a VPS é
> **Windows com RDP** — não serve uma VPS Linux.

---

## 0. Contratar a VPS (Vultr)

**Vultr** é a escolha: fiabilidade 99.99%, faturação à hora (cancelas quando
quiseres), licença **Windows incluída** no preço, snapshots fáceis e
datacenters na Europa. É a opção mais estável para correr o fluxo de edição
rápida (8s por defeito) 24/7 — e mais tarde, com dinheiro real.

### Passos na Vultr

1. Cria conta em **vultr.com** (email + cartão/PayPal; carrega ~$20 de
   saldo, gasta-se só o que usares).
2. **Deploy New Server**:
   - **Type**: `Cloud Compute` → *Regular Performance* (o *High
     Performance* é opcional — o projeto não precisa);
   - **Location**: **Frankfurt (`eu-central`) ou Amesterdão (`ams`)** —
     menor latência ao Telegram e ao MetaQuotes;
   - **Image**: **Windows** — Server 2019/2022 *with Desktop Experience*
     (ou Windows 10/11). **A licença está incluída no preço** — Server
     Core NÃO serve (o MT5 precisa de ambiente gráfico);
   - **Size**: **2 vCPU · 4 GB RAM · 80 GB NVMe** (~$16/mês) — o mínimo
     confortável para Windows + MT5 + Python + Caddy. O plano 1 vCPU/1 GB
     não chega;
   - **Auto Backups**: ativa (~$1–2/mês extra) ou tira um **Snapshot**
     manual antes de cada mudança grande.
3. **Deploy** → a página do servidor mostra o **IP**, o utilizador
   **Administrator** e a **password** (anota-a — se perderes, usa
   "Reset Password" na consola).
4. (Opcional, recomendado) Cria um **Snapshot** da VPS limpa — se algo
   correr mal, restaura em 2 minutos.

**Especificação mínima:** 2 vCPU · **4 GB RAM** · 80 GB NVMe · Windows
Server 2019/2022 (Desktop Experience) · RDP habilitado.

---

## 0.1 Alternativa: testar na Azure (período gratuito)

Se tiveres a **Azure em período de teste** (conta gratuita), dá para testar
sem pagar nada — com duas limitações importantes:

| Caminho | O que é | Serve para isto? |
|---|---|---|
| **Crédito $200 (30 dias)** | Qualquer serviço Azure, incluindo VMs Windows grandes | ✅ Sim — cria uma `Standard_B2s` (2 vCPU/4 GB, ~$50/mês) e o crédito cobre os 30 dias |
| **VM gratuita 12 meses** | `B1s` Windows ou Linux, **750 h/mês** | ⚠️ A RAM de **1 GB é demasiado curta** para Windows + MT5 + Python 24/7 — serve só para um teste rápido de 30 min |

### Criar a VM Windows na Azure (com o crédito)

1. Portal → **Virtual machines** → **Create**.
2. **Image**: `Windows Server 2022 Datacenter: Azure Edition - x64 Gen2`
   (tem ambiente gráfico — o MT5 precisa).
3. **Size**: `Standard_B2s` (2 vCPU · 4 GB RAM).
4. **Region**: `West Europe` (Amesterdão).
5. Cria um utilizador administrador (username + password) — é com ele que
   entras por RDP. Ativa a porta 3389 de entrada.
6. **Networking**: cria um *Network Security Group* — regra de entrada
   **3389 só para o teu IP** e **443 aberto** (para o Caddy). Nunca 8000.
7. **Review + Create** → aguarda o deploy.

Depois é o mesmo guia: RDP → instalar Python/MT5/Caddy → copiar o projeto →
`.\start.ps1` (secções 2–7 abaixo).

> ⚠️ **O crédito acaba aos 30 dias.** Quando acabar, a VM é desalocada
> (para de cobrar compute, mas o disco continua a cobrar uns cêntimos/dia
> até a apagares). Para correr 24/7 a longo prazo, a **Vultr (~$16/mês)** é
> muito mais barata que a Azure pay-as-you-go (B2s ≈ $50/mês). A Azure é
> ótima para testar agora de graça — depois migra para a Vultr.

---

## 1. Primeiro login e firewall

1. Liga-te à VPS por **RDP** (o fornecedor envia o IP e a password do
   Windows).
2. **Muda imediatamente a password do administrador** do Windows para uma
   password forte (não a padrão do fornecedor).
3. Abre a firewall do Windows (`wf.msc`) e cria regras de entrada:
   - ✅ **HTTPS (443)** — aberto para **todos** (é a porta da dashboard);
   - ✅ **RDP (3389)** — abre **só para o teu IP** (ou usa a VPN do
     fornecedor); nunca para o mundo;
   - ❌ **Porta 8000** — **nunca** abrir ao público (o Caddy faz o proxy);
   - ❌ **22/SSH** — desnecessário (Windows).

   > 💡 Na Vultr podes reforçar com a **Cloud Firewall** da própria
   > plataforma (Network → Firewall): permite só 443 para o mundo e 3389
   > para o teu IP — assim, mesmo que o Windows Firewall falhe, a rede
   > bloqueia. Liga-a à VPS em *Server → Firewall*.
4. Ativa o Windows Update e deixa instalar tudo antes de continuar.

---

## 2. Instalar Python e git

1. Descarrega o instalador de Python 3.11/3.12 (64-bit) de python.org.
2. **No instalador, marca ✅ "Add Python to PATH"** e instala.
3. Instala o git (git-scm.com) — aceita os defaults.
4. No PowerShell, confirma:

```powershell
python --version
git --version
```

---

## 3. Instalar e configurar o MetaTrader 5

1. Descarrega o **MetaTrader 5** (da MetaQuotes ou do teu broker) e instala.
2. Faz login com a conta de trading:
   - Login: **5054644473** (demo) · Servidor: **MetaQuotes-Demo**
   - Password: a que estiver no teu `.env` (`MT5_PASSWORD`)
3. Na barra de ferramentas, liga o botão **"Algo Trading"** (tem de ficar
   **verde** — sem ele, as ordens são recusadas).
4. Em `account_info`, confirma `margin_mode=2` / **RETAIL_HEDGING**. A
   estratégia de N pernas precisa de posições independentes; o servidor
   bloqueia contas netting/desconhecidas quando `MT5_DRY_RUN=0`.
5. **Deixa o terminal aberto** e ligado — o bot precisa dele 24/7.
   (Dica: configura o Windows para **iniciar sessão automaticamente** com o
   terminal MT5 no arranque — assim basta a VPS reiniciar para tudo voltar.)

---

## 4. Copiar o projeto para a VPS

Opção A — git (recomendado para atualizações fáceis):

```powershell
cd C:\
git clone <URL-do-teu-repositorio> Project NexGen
cd Project NexGen
```

Opção B — copiar manualmente (zip do projeto, sem a pasta `.venv` nem a BD).

Depois, cria o ambiente e instala as dependências (o `requirements.txt` já
inclui o **MetaTrader5** — pacote Windows-only, liga ao terminal local):

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

---

## 5. Criar o `.env` na VPS

Copia o teu `.env` local para a VPS **ou** cria um novo (`notepad .env`)
com as mesmas chaves. O `.env` na VPS precisa de:

```ini
EXECUTION_MODE=mt5
MT5_LOGIN=5054644473
MT5_SERVER=MetaQuotes-Demo
MT5_PASSWORD=XXXXXXXX            # senha da conta demo (nunca em git!)
SIGNALBOT_TG_API_ID=...
SIGNALBOT_TG_API_HASH=...
SIGNALBOT_TG_PHONE=+351...
SIGNALBOT_TG_GROUP=Golden Moves
SIGNALBOT_DASHBOARD_PASSWORD=<password-forte> # substituir pela password local; nunca guardar no git
SIGNALBOT_API_TOKEN=...                  # token do listener (gerado)
```

> 🔒 O `.env` **nunca** entra no git. Cria-o sempre à mão na VPS.

---

## 6. Instalar o Caddy (HTTPS automático)

O Caddy gera e renova certificados HTTPS (Let's Encrypt) sozinho. Não serve
guardar passwords em HTTP puro.

1. Descarrega o Caddy para Windows em caddyserver.com (ficheiro único
   `caddy.exe`).
2. Coloca `caddy.exe` em `C:\caddy\` e cria `C:\caddy\Caddyfile`:

```
dominio.com {
    reverse_proxy 127.0.0.1:8000
}
```

   (substitui `dominio.com` pelo teu domínio — aponta o DNS do domínio para
   o IP da VPS antes de arrancar o Caddy).

3. Arranca o Caddy como **serviço do Windows** (assim sobe sozinho):

```powershell
cd C:\caddy
.\caddy.exe start
# ou, para correr como serviço sempre ligado:
sc.exe create Caddy binPath= "C:\caddy\caddy.exe run --config C:\caddy\Caddyfile" start= auto
```

4. Confirma em https://dominio.com — deve aparecer o **🔐 login** do
   Project NexGen com cadeado HTTPS válido.

---

## 6.1 Sem domínio — acesso por IP (HTTPS auto-assinado)

Se ainda não tens domínio (ex.: teste de 30 dias entre amigos), o Caddy
serve na mesma por **IP com HTTPS auto-assinado** (`tls internal`): o
tráfego fica cifrado e os browsers mostram um aviso de "certificado não
confiável" — aceitar uma vez (Avançado → Continuar) em cada dispositivo.

```
# C:\caddy\Caddyfile
https://<IP_PUBLICO_DA_VPS> {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```

1. Abre as portas 80 e 443 na firewall do Windows (nunca 8000 nem 3389):
   ```
   netsh advfirewall firewall add rule name="NexGen HTTP 80" dir=in action=allow protocol=TCP localport=80
   netsh advfirewall firewall add rule name="NexGen HTTPS 443" dir=in action=allow protocol=TCP localport=443
   ```
2. Arranca o Caddy: `cd C:\caddy && .\caddy.exe start` (após um reboot da
   VPS, repetir este comando — ou criar um serviço/Tarefa Agendada).
3. Acede a `https://<IP>` — o HTTP redireciona para HTTPS (308).
4. **Firewall da plataforma (Kamatera)**: se tiveres uma *Cloud Firewall*
   ativa na consola (Cloud → Firewalls), acrescenta regras TCP para 80/443
   com origem restrita aos teus IPs — o Windows Firewall sozinho não basta.

> Quando tiveres domínio: troca o endereço no Caddyfile e remove `tls
> internal` — o Caddy passa a emitir HTTPS automático (Let's Encrypt).

## 6.2 DuckDNS — domínio grátis (HTTPS confiável, cadeado verde)

Sem domínio comprado, o **duckdns.org** dá um subdomínio grátis
(ex.: `n3xg3n.duckdns.org`) que aponta para o IP da VPS. Com domínio, o
Caddy emite HTTPS automático da Let's Encrypt (sem aviso no browser).

1. Cria conta em duckdns.org (login Google/GitHub/Twitter/Reddit) e adiciona
   o subdomínio com o IP público da VPS.
2. `C:\caddy\Caddyfile` — troca o endereço e remove `tls internal`:
   ```
   n3xg3n.duckdns.org {
       reverse_proxy 127.0.0.1:8000
   }
   ```
3. Reinicia o Caddy: `cd C:\caddy && .\caddy.exe stop` + `.\caddy.exe start`
   (na 1ª vez demora ~30s a emitir o certificado).
4. **Update automático do IP** (rede de segurança — o IP é fixo): script
   `C:\caddy\duckdns-update.ps1` chamando
   `https://www.duckdns.org/update?domains=<NOME>&token=<TOKEN>&ip=`
   (sem `ip` o DuckDNS deteta sozinho), agendado por Tarefa Agendada.

## 7. Arrancar o bot (um comando)

O `start.ps1` já faz tudo: carrega o `.env`, verifica o MT5 e arranca
**servidor + listener**. É **idempotente** — deteta se já estão no ar e não
lança duplicados:

```powershell
cd C:\Project NexGen
.\start.ps1
```

- Se o `MT5_PASSWORD` já estiver no `.env`, não pede nada.
- Verás no terminal: Terminal ligado · conta/saldo/equity · AutoTrading ·
  Servidor em http://127.0.0.1:8000 · listener a ouvir o grupo.
- Para reiniciar de facto: `taskkill //IM python.exe //F` e volta a correr.

**Após um reboot da VPS** (Tarefa Agendada `NexGen Boot`, AtLogOn, com
privilégios máximos): o `boot.ps1` arranca o Caddy, o **MetaTrader 5**
(`terminal64.exe`) e depois o `start.ps1` — todos idempotentes. O servidor
religa-se ao MT5 automaticamente (retry no arranque + reconexão a cada 10s)
e autentica-se com `MT5_LOGIN/SERVER/PASSWORD` do `.env`; o **AutoTrading**
tem de estar ligado no terminal (fica persistido). Para tudo isto subir
sozinho, configura o **auto-login do Windows** (secção seguinte).

---

## 7.x Auto-login do Windows (reboot sem intervenção)

Sem auto-login, a VPS fica parada no ecrã de login após um reboot e a
Tarefa `NexGen Boot` (AtLogOn) nunca dispara. Para entrar sozinho:

```powershell
# uma vez, como Administrador (substitui <password> pela password real)
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name AutoAdminLogon      -Value '1'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultUserName     -Value 'Administrator'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultPassword     -Value '<password>'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' `
  -Name DefaultDomainName   -Value ''
```

> ⚠️ A password fica legível no registo (acessível a quem for admin na
> máquina). É o compromisso padrão para operação unattended numa VPS.
> Para reverter: põe `AutoAdminLogon=0` e apaga `DefaultPassword`.

Depois do auto-login + `NexGen Boot`, o fluxo completo é: Windows entra →
`boot.ps1` → Caddy + MT5 + servidor/listener → o servidor liga-se ao MT5
com as credenciais do `.env` (o AutoTrading tem de estar ON no terminal).

---

## 8. Verificação final

1. Abre https://dominio.com no telemóvel (dados móveis, sem VPN) → deve
   pedir login → entra com a password configurada → vês a dashboard com o
   saldo real do MT5 e o badge **⚡ execução real (demo MT5)**. No Diagnóstico,
   confirma `hedged=true`, AutoTrading ligado e `blocked_reason` vazio.
2. Confirma que o listener está a apanhar o grupo: `tail listener.log`
   (ou `Get-Content listener.log -Tail 20`).
3. No painel **👻 Shadow-paper**, confirma `ativo` e as estratégias
   `auto_escada_3` e `auto_escada_5` para acumular evidência forward sem
   ordens novas. O snapshot deve mostrar
   `shadow_paper.execution_isolated=true`; isto não altera `EXECUTION_MODE`,
   `MT5_DRY_RUN` nem a conta demo.
4. Manda uma mensagem de teste no grupo e vê o fluxo completo na dashboard.
5. Confirma `GET /api/window`: a janela UTC termina em `2026-08-29T00:00:00+00:00`, mostra trades reais/P&L/ritmo e o gate de promoção da auto-escada 3.
6. Se o kill switch estiver pausado para esta janela, confirma no `/api/status` que `killswitch_rearm_at` está persistido. O tick reativa a pausa no prazo, envia feed/Telegram e limpa a chave; não é necessário criar uma tarefa agendada separada.

---

## 9. Manutenção

| Ação | Comando (PowerShell) |
|---|---|
| Ver servidor | `Get-Content server.log -Tail 30` |
| Ver listener | `Get-Content listener.log -Tail 30` |
| Reiniciar tudo | `.\start.ps1` |
| Estado rápido | `curl http://127.0.0.1:8000/api/status -H "X-API-Token: <token>"` |
| Backup da BD | `.\venv\Scripts\python backup_db.py` — cópia consistente para `backups/` + envio ao Telegram (Tarefa `NexGen DB Backup` diária 06:30) |
| Restore-check | `.\venv\Scripts\python backup_db.py --restore-check` — valida o backup mais recente numa BD temporária |
| Auditoria da BD | `.\venv\Scripts\python maintenance.py --db-audit` — contagens/taxa mensal/política, read-only |
| Espaço em disco | `.\venv\Scripts\python maintenance.py --disk-check` — check manual; o servidor repete diariamente |

### 9.2 CI e métricas operacionais (Itens 10 e 17)

O ficheiro `.github/workflows/ci.yml` corre `python -m pytest -q` em cada
push e pull request, e valida novamente `tests/test_docs.py` num job separado.
O workflow usa Python 3.11, `requirements.txt` e os modos `sim`/`MARKET_PROVIDER=none`,
por isso nunca precisa de MetaTrader nem envia ordens. No GitHub Actions é
possível lançar manualmente `failure_probe=true`: o job cria um teste que falha,
confirma o exit code não-zero e limpa o ficheiro; a execução normal continua a
ser verde apenas quando a suite passa.

A saúde operacional aparece no Relatório e em `GET /api/operations?days=90`.
Não é necessário configurar credenciais novas: os dados são gravados na BD
local nas tabelas de exceções, quotes MT5, reconexões e modifies. Deixa o
servidor acumular pelo menos duas semanas antes de interpretar tendências.

O **shadow-paper** é configurado pela dashboard ou por `POST /api/config` com
`shadow_paper_enabled` e `shadow_paper_strategies`. As trades ficam nas tabelas
`paper_*`, com `origin="shadow"`, e a conta/saldo/kill diário dessa origem são
separados da corrida normal; não passam pelo `MT5Engine`. O histórico
`historical=True` e sinais `llm_parsed` são ignorados para esta amostra. A fila
`pending_management` mantém marcadores separados para a execução real, paper
normal e shadow, para uma correção/saída não desaparecer entre streams. Se o
modo for desligado, as trades existentes continuam auditáveis e só novas
aberturas são bloqueadas.

**Watchdog simples** (PowerShell, corre a cada minuto via Task Scheduler):
reinicia o servidor se `server.pid` não responder, e reabre o MT5 se o
processo `terminal64.exe` não existir. O servidor faz ainda o check diário do
volume livre (`SIGNALBOT_DISK_FREE_GB`, default 10 GiB) e envia alerta no
feed/Telegram.

### 9.1 Logs, auditoria e restauração

O `start.ps1` usa `app.server_runner` e já não redireciona stdout para um
ficheiro aberto: o servidor/listener escrevem em `server.log`,
`server.err.log`, `listener.log` e `listener.err.log` com rotação por tamanho.
Por defeito cada ficheiro fica limitado a 10 MiB com 5 cópias; configura com:

```ini
SIGNALBOT_LOG_MAX_BYTES=10485760
SIGNALBOT_LOG_BACKUP_COUNT=5
```

A auditoria read-only das tabelas (linhas, meses observados, taxa mensal e
política de retenção) é:

```powershell
.venv\Scripts\python maintenance.py --db-audit
```

O backup mensal restaura o ficheiro mais recente para uma BD temporária,
valida `PRAGMA integrity_check` e contagens das tabelas principais. Para correr
manualmente e testar uma falha, duplica um backup para uma pasta temporária,
corrompe essa cópia e chama a função `restore_check` nos testes — nunca
corrompas o único backup da VPS:

```powershell
.venv\Scripts\python backup_db.py --restore-check
```

O comando devolve código diferente de zero e envia alerta Telegram se a
restauração falhar. A Tarefa Agendada recomendada é mensal, por exemplo dia 1
às 07:00, além da tarefa diária **NexGen DB Backup** às 06:30.

**Lifecycle do servidor:** os loops de mercado e watchdog pertencem ao
`app/background.py::BackgroundRuntime`. O `lifespan` cancela e aguarda as
tasks antes de fechar o provider MT5; ao atualizar Python, o `start.ps1` deve
ser usado depois de confirmar que o processo uvicorn antigo terminou. Isto
evita que um restart deixe dois loops a gerir a mesma conta.

**Estudo Item 2 (read-only):** para repetir o sweep sem abrir trades nem
alterar a BD, usa `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m
app.study_consolidation --item2-sweep`. A ferramenta recalcula as 27
combinações da `auto_escada_5` nas três janelas e só imprime o relatório.

**Estudo Item 3 (read-only):** `PYTHONIOENCODING=utf-8 .venv/Scripts/python
-m app.study_consolidation --item3-sweep` compara `confirm_filter=True/False`
da `auto_escada_3` nas janelas atual/tudo/antiga, sem escrever na BD nem
abrir trades. O resultado aceito (PF atual 1.27→1.26; DD antiga
312.9%→278.3%) congelou `True` no registo, mas não promoveu a auto: o
servidor em produção continua a usar a estratégia configurada e a promoção
para MT5 demo exige os gates Fase 2/3.

A promoção de uma auto para a demo só deve ser feita depois dos gates do
painel Fase 2. A validação Fase 3 tem uma janela fixa das primeiras 10 trades,
com comparação paper↔MT5 (≥9/10 saídas iguais) e rollback automático para
`nova_gestao` em caso de falha. O teste de integração usa apenas um broker fake
e não deve ser substituído por uma ordem manual na conta demo.

**Atualizações**: `git pull` + `.\start.ps1` — o `.env` e a BD não são
tocados pelo git.

---

## Checklist de segurança (antes de por em produção real)

- [ ] Password do Windows forte + RDP só para o teu IP
- [ ] Só as portas **443** (web) e **3389** (RDP, restrita) abertas
- [ ] Caddy com HTTPS obrigatório (HTTP → redireciona para HTTPS)
- [ ] `SIGNALBOT_DASHBOARD_PASSWORD` definida (login ativo)
- [ ] `SIGNALBOT_API_TOKEN` definido (o listener autentica sem browser)
- [ ] Kill switch e limites de risco ativos na dashboard
- [ ] Backup diário da BD para fora da VPS
