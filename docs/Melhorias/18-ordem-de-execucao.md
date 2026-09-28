# SPEC — Ordem de execução e encerramento

> Contexto e regras comuns: consultar [00-contexto-e-regras.md](00-contexto-e-regras.md).

## Estado atual (2026-09-01)

**Encerrados e aceites**: Itens 1, 2, 3, 7, 8, 9, 14 e 15. Os respetivos
specs foram apagados depois dos testes e da verificação operacional. A conta
principal continua demo, mas agora está persistida como `tp2_protect` (2 pernas,
saída no TP2), com saldo observado de €1.003,89; `MT5_DRY_RUN=0` está ativo para
enviar ordens broker na demo. Não há autorização para operar uma conta real nem
promoção automática.

**Implementados, mas ainda em observação/evidência**: o Item 10 já tem
instrumentação persistente, endpoint e painel, mas o critério de tendência
continua pendente até haver pelo menos duas semanas de dados reais. O Item 17
já tem workflow push/PR, validação documental e failure-probe manual; a
execução remota no GitHub foi confirmada neste ciclo, mas a branch protection
fica dependente do plano GitHub e não é necessária para o runtime local.
Os dois specs permanecem até a evidência final, conforme as regras comuns.

**Shadow-paper forward**: implementação operacional concluída fora da ordem dos
Itens 10/17. A `auto_escada_3` pode acumular sinais live numa conta virtual
`origin="shadow"` sem depender da execução principal e sem abrir ordens demo;
histórico/LLM ficam excluídos e a gestão mantém marcadores separados para
real (`applied`), corrida paper (`paper_applied`) e shadow (`shadow_applied`). A evidência continua a exigir N≥20 sinais resolvidos.

**Próxima ordem recomendada**: confirmar o primeiro run do Item 17, deixar o
Item 10 acumular duas semanas, e só depois iniciar Item 11 (relatório
semanal/deriva). Os Itens 4–6/12–13 continuam dependentes de dados reais
suficientes. Item 16 pode avançar em paralelo quando existirem credenciais de
um canal secundário; não se deve remover o modo manual (Item 12) enquanto o
fallback LLM e a frequência de edições de sinais completos não estiverem
decididos com denominador real.

## Ordem de execução sugerida

1. Itens 1–3 estão encerrados; não reabrir os specs sem dados que contradigam
   os veredictos aceites.
2. Itens 7–9, 14 e 15 estão encerrados: a infraestrutura operacional deve
   agora acumular histórico em vez de ser recalibrada sem necessidade.
3. Item 17 está implementado; o primeiro run normal e o failure-probe foram
   confirmados no GitHub. A branch protection privada continua bloqueada pelo
   plano Free, por isso o spec permanece para decisão/aceitação documental.
4. Item 10 está instrumentado: deixa acumular exceções, spread, reconexões e
   modifies recusados; o critério final exige duas semanas de dados, portanto
   implementação não equivale ainda a conclusão.
5. Item 11 depois de 10: o relatório semanal/deriva precisa dos agregados de
   saúde e deve ser testado sem esperar semanas para o código, mas só terá
   tendência interpretável com dados acumulados.
6. Item 16 é independente e pode correr em paralelo quando houver credenciais
   SMTP/webhook e um teste controlado de falha do Telegram.
7. Item 4 depois de uma janela forward suficiente e sem alterar o risco da
   principal enquanto a `auto_escada_3` ainda não tem N≥20.
8. Item 5 é independente tecnicamente, mas a recalibração mensal deve manter
   os defaults congelados até existir uma janela nova out-of-sample.
9. Item 12 só depois de medir a frequência de edições corretivas a sinais
   completos e decidir explicitamente o destino dos sinais `llm_parsed`.
10. Item 13 depois de 11 para o resumo semanal; os papéis de classificação e
    triagem podem ser prototipados em modo sugestão, nunca ação.
11. Item 6 só depois de a corrida `tp2_protect`/híbrida ter pelo menos N≥20
    sinais resolvidos comparáveis.

Nenhum item avança para Fase 3 (MT5 demo) ou conta real sem passar primeiro
pelos gates já definidos em `docs/ESTRATEGIAS.md` Parte X.

> **Nota**: o heartbeat externo (dead man's switch, monitorização por
> serviço fora da VPS a confirmar que o sistema está vivo) foi identificado
> como lacuna mas fica **fora desta spec por agora**, por decisão explícita.
> Pode ser adicionado como item futuro quando fizer sentido retomar.

**Nota final sobre o kill switch**: nesta revisão o kill switch persistido
permanece **ligado** e não foi desativado para criar amostra artificial. Antes
de qualquer validação forward/Fase 2 ou promoção, confirmar explicitamente o
modo, a estratégia, o tipo de conta e a intenção de abrir novas trades.

**Encerramento**: quando todos os itens (1–17) estiverem implementados,
validados e documentados no `CHANGELOG.md`/`docs/ESTRATEGIAS.md`/`DEPLOY.md`
(conforme aplicável), este ficheiro (`SPEC_MELHORIAS.md`) deixa de ser
necessário — apagar do repositório. Não apagar antes disso, e nunca apagar
`CHANGELOG.md`, `docs/ESTRATEGIAS.md` ou `DEPLOY.md`, que continuam a ser a
fonte permanente de registo.
