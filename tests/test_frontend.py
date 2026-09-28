"""Regressões do frontend (dashboard vanilla JS — páginas separadas em
app/static/).

A suite pytest não executa o JS, por isso estas regressões são estáticas:
leem o HTML/JS e garantem contratos que já causaram bugs reais. Se uma delas
falhar, o frontend voltou a um comportamento errado.

A dashboard dividiu-se em páginas multi-página (2026-08): `index.html` (a
principal), `teste.html` (simulações de estratégias),`diagnostico.html`, `auditoria.html` e `help.html`, com o JS partilhado em `common.js` e o CSS em `style.css`.
"""

from pathlib import Path

STATIC = Path("app") / "static"
INDEX = STATIC / "index.html"
TESTE = STATIC / "teste.html"
DIAG = STATIC / "diagnostico.html"
HELP = STATIC / "help.html"
AUDIT = STATIC / "auditoria.html"
COMMON = STATIC / "common.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_test_pages_sim_usam_saldo_proprio():
    """As páginas de teste/HELP têm de pedir `/api/analysis/month` através do
    `testeSimQS()`, que aplica o saldo default 1000 (o saldo próprio da conta
    paper) em vez do saldo da conta MT5. O bug: sem `balance`, o backend usava
    `balance=0` = saldo atual da MT5 e o \"Saldo inicial\" mostrava 973.49 €
    em vez de 1.000 €."""
    html = _read(TESTE) + "\n" + _read(HELP) + "\n" + _read(COMMON)
    lines = [ln for ln in html.splitlines() if "/api/analysis/month" in ln]
    assert lines, "nenhuma chamada a /api/analysis/month encontrada no frontend"
    for ln in lines:
        assert "testeSimQS()" in ln, (
            "chamada a /api/analysis/month sem testeSimQS() "
            f"(pode usar o saldo da MT5 em vez do saldo próprio): {ln.strip()[:110]}"
        )


def test_teste_sim_qs_default_saldo_1000():
    """O `testeSimQS()` tem de aplicar `&balance=` com fallback para 1000 —
    o saldo próprio da conta paper, nunca vazio (que seria saldo da MT5)."""
    html = _read(TESTE)
    assert 'value="1000"' in html, \
        "input do saldo do sim perdeu o default 1000"
    common = _read(COMMON)
    assert "&balance=" in common and "1000" in common, \
        "testeSimQS perdeu o balance default 1000"


def test_dashboard_has_operator_analyst_view_switch():
    html = _read(INDEX) + _read(COMMON)
    assert 'id="viewOperator"' in html and 'id="viewAnalyst"' in html
    assert "nexgenDashboardView" in html
    assert "dashboard-operator" in html
    assert "analyst-only" in html


def test_auditoria_has_human_pre_real_decision_control():
    html = _read(AUDIT)
    main = _read(Path("app") / "main.py")
    assert "preRealConfirmation" in html
    assert "/api/analysis/pre-real-decision" in html
    assert "/api/analysis/pre-real-decision" in main
    assert "PreRealDecisionIn" in main


def test_dashboard_content_has_breathing_room_and_collapsible_controls():
    html = _read(INDEX)
    assert 'class="dashboard-overview"' in html
    assert 'class="advanced-controls"' in html  # operacional: visível nas duas vistas, colapsável
    css = _read(STATIC / "style.css")
    assert ".overview-heading" in css
    assert ".advanced-controls" in css
    assert "padding: var(--sp-4)" in css
    # a ordem de leitura coloca o foco operacional antes dos KPIs (flex order)
    assert ".wrap > .operator-focus { order: 5;" in css
    assert ".wrap > .dashboard-overview { order: 6;" in css


def test_operator_view_exposes_minimal_operational_focus():
    html = _read(INDEX)
    assert 'class="operator-focus"' in html  # foco operacional visível na vista Operador
    for element in ("operatorExecState", "operatorOpenState", "operatorLastSignal", "operatorActionState"):
        assert f'id="{element}"' in html
    assert "operatorExecState" in html and "operatorActionState" in html


def test_secondary_pages_have_global_context_and_health_support():
    for page in (AUDIT, DIAG, TESTE, HELP):
        html = _read(page)
        assert 'id="statusBar"' in html
        assert 'id="pageUpdatedAt"' in html
    audit = _read(AUDIT)
    teste = _read(TESTE)
    assert 'id="globalOriginFilter"' in audit
    assert 'id="globalOriginFilter"' in teste
    common = _read(COMMON)
    assert "initGlobalOriginFilter" in common
    assert "nexgenOriginFilter" in common
    assert "relativeAge" in common


def test_dashboard_has_global_operational_health_banner():
    html = _read(INDEX) + _read(COMMON)
    css = _read(STATIC / "style.css")
    assert 'id="healthBanner"' in html
    assert "renderHealthBanner" in html
    assert "healthTitle" in html and "healthDetail" in html
    assert "health-banner" in css
    assert "Operação normal" in html or "Operação normal" in _read(COMMON)


def test_shared_dashboard_css_has_accessible_polish_contracts():
    css = _read(STATIC / "style.css")
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "::selection" in css
    # contrato de performance: sem backdrop-filter (camadas de blur caras no
    # scroll), sem animações decorativas infinitas e sem mix-blend-mode
    assert "backdrop-filter" not in css
    assert "will-change" not in css
    assert "mix-blend-mode" not in css


def test_auditoria_page_exposes_quality_fill_and_forward_reports():
    html = _read(AUDIT)
    assert "journal-quality" in html
    assert "fill-audit" in html
    assert "forward-gate" in html
    assert "Primeira divergência" in html
    assert "NUNCA" in html
    assert "exportAuditJson" in html and "exportAuditCsv" in html
    assert "archiveForward" in html and "forward-gate/archive" in html
    assert "archiveTable" in html
    main = _read(Path("app") / "main.py")
    assert "/api/analysis/audit-export" in main
    assert "/api/analysis/forward-gate/archives" in main


def test_auditoria_worker_outage_timeline_block():
    """O /auditoria mostra a linha temporal das falhas do worker (delete de
    ordens pendentes): o bloco, o renderer e o fetch de
    /api/operations/replay/outages no loadAudit."""
    html = _read(AUDIT)
    assert "Worker outages" in html
    assert "outageStats" in html and "outageTable" in html
    assert "outageEpisodes" in html and "renderOutages" in html
    assert "/api/operations/replay/outages?days=30" in html
    main_py = (Path("app") / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/operations/replay/outages")' in main_py
    evidence_py = (Path("app") / "execution" / "evidence.py").read_text(
        encoding="utf-8")
    assert "def worker_outage_timeline" in evidence_py


def test_auditoria_page_exposes_recount_quality_block():
    """A página /auditoria mostra a qualidade dos recounts do grupo (R2):
    o bloco, o renderer e o fetch de /api/recounts no loadAudit."""
    html = _read(AUDIT)
    assert "Qualidade dos recounts do grupo" in html
    assert "recountQualityStats" in html and "recountQualityTable" in html
    assert "renderRecounts" in html
    assert "/api/recounts?limit=200" in html
    assert "Implausíveis" in html and "Sem candles" in html


def test_auditoria_recount_evidence_recheck_and_weekly_chart():
    """O /auditoria cruza os recounts implausíveis do journal com o
    fill-audit (recount_evidence), permite re-correr o candle-check de um
    recount específico e mostra a taxa semanal de implausibilidade."""
    html = _read(AUDIT)
    assert "recountEvidenceTable" in html and "recountEvidenceCount" in html
    assert "recount_evidence" in html or "recounts implausíveis" in html
    assert "recheckRecount" in html and "/api/recounts/${id}/recheck" in html
    assert "recountsWeeklyChart" in html and "drawAuditRecountsWeekly" in html
    assert "plausível" in html and "implausível" in html
    # toggle do journal (ligar/desligar a recolha de evidência) com confirmação
    assert "toggleJournal" in html and "toggleJournal" in html
    assert "execution_journal_enabled" in html and "confirm(" in html
    # taxa de implausibilidade vs baseline (66% sobre sinais reais)
    assert "recountBaselineTip" in html and "baseline" in html
    # re-check em lote: re-corre todos os recounts com candles frescos e o
    # offset mais recente, persiste o veredicto em trade_recounts
    assert "recheckAllRecounts" in html and "/api/recounts/recheck-all" in html
    assert "re-check todos" in html
    # veredicto combinado spot MT5 + GC=F: implausível no GC=F mas plausível
    # no spot marca "provável divergência de feed" em vez de relato suspeito
    assert "feed_divergence" in html or "divergência de feed" in html
    assert "spot_plausible" in html and "futures_plausible" in html
    # baseline com o feed certo: o spot MT5 é a fonte primária e o tip
    # referencia o estudo que o mediu (o baseline do GC=F era 66%)
    assert "study_recount_mt5_feed.py" in html and "baseline" in html
    # implausíveis confirmados (spot+GC=F a discordar) vs divergência de feed
    assert "Implaus. confirmados" in html and "Divergência de feed" in html
    main = _read(Path("app") / "main.py")
    assert "/api/recounts/{recount_id}/recheck" in main


def test_render_guards_previnem_rebuilds_sem_mudanca():
    """Contrato de render: o snapshot chega ~1×/s mas o DOM só reconstrói
    quando o conteúdo mudou. Sem estes guards, as tabelas eram reconstruídas
    em cada snapshot (perf) e as animações disparavam sem movimento real
    (UX). A faixa de preços foi removida por completo (decisão de 09/2026) —
    o DOM da topbar fica estático; só mudam alas, relógio e flashes reais."""
    common = _read(COMMON)
    html = _read(INDEX)
    # a faixa de preços (símbolos em cima) foi removida por completo
    assert "priceStrip" not in common and "renderPriceStrip" not in common
    assert "price-chip" not in common and "TICKER_LABEL" not in common
    # flash do saldo: só quando o saldo mudou de facto (nunca na 1ª leitura)
    assert "_prevBalance !== null && a.balance !== _prevBalance" in html
    # feed: só reconstrói quando as notificações mudaram
    assert 'contentChanged("feed"' in html
    # só a tab ativa reconstrói (as outras mantêm o DOM parado)
    assert 'window._activeTab || "sinais"' in html


def test_auto_promotion_is_visually_gated_by_forward_evidence():
    index = _read(INDEX)
    teste = _read(TESTE)
    state = (Path("app") / "state.py").read_text(encoding="utf-8")
    assert "refreshForwardPromotionGate" in index
    assert "phase2PromotionGate" in index and "phase3PromotionGate" in index
    assert "window._forwardPromotionGate" in index
    assert "refreshPromotionGate" in teste
    assert "window._promotionGateEligible" in teste
    assert "promoção bloqueada" in teste
    assert "promotion_blocked" in state
    assert "forward_gate_report" in state


def test_edge_tab_removida():
    """A tab Edge foi removida (não pode voltar a aparecer com o botão
    `data-tab=\"edge\"` nem com o card `#tab-edge`)."""
    for p in (INDEX, TESTE, DIAG, HELP):
        html = _read(p)
        assert 'data-tab="edge"' not in html, f"botão da tab Edge voltou ao {p.name}"
        assert 'id="tab-edge"' not in html, f"card #tab-edge voltou ao {p.name}"


def test_walkforward_e_estudos_risco_nas_paginas_de_teste():
    """O walk-forward e os seletores de risco/saldo moveram-se para a página de
    teste (não podem desaparecer com a Edge)."""
    html = _read(TESTE)
    assert 'id="testeWalkforward"' in html, "walk-forward desapareceu da página de teste"
    assert 'id="testeRisk"' in html, "seletor de risco desapareceu da página de teste"
    assert 'id="testeBalance"' in html, "seletor de saldo desapareceu da página de teste"
    assert 'id="wfBuckets"' in html, "buckets do walk-forward desapareceram"


def test_corrida_paralela_tem_referencia_da_conta_real():
    """A mini-curva da corrida paralela mostra o saldo real da MT5 como linha
    de referência SEPARADA (flag `is_real` no frontend + série reservada
    `__real__` no backend), ao lado das contas virtuais das estratégias."""
    html = _read(INDEX)
    assert "s.is_real" in html, "o frontend perdeu o tratamento da linha real (is_real)"
    assert "MT5 real" in html, "a etiqueta 'MT5 real' desapareceu da legenda"
    assert "conta real" in html, "o rótulo 'conta real' desapareceu da legenda"
    # o backend expõe a série real em /api/paper/history
    main_py = (Path("app") / "main.py").read_text(encoding="utf-8")
    assert '"__real__"' in main_py, "o backend deixou de gravar/expor a série __real__"
    assert '"is_real": True' in main_py, "a série real perdeu o flag is_real"
    # e o snapshot da corrida continua a gravar o saldo real no tick
    state_py = (Path("app") / "state.py").read_text(encoding="utf-8")
    assert '__real__' in state_py, "o tick deixou de gravar o saldo real (__real__)"


def test_corrida_paralela_seletor_horizonte_e_stats():
    """A mini-curva da corrida paralela tem seletor de horizonte (24h/7d/30d)
    e a legenda mostra trades fechadas + win rate por estratégia (lidos do
    backend `/api/paper/history`)."""
    html = _read(INDEX)
    # seletor com os 3 horizontes
    assert 'id="paperRaceHours"' in html, "seletor de horizonte desapareceu"
    for v in ('value="24"', 'value="168"', 'value="720"'):
        assert v in html, f"opção {v} do seletor de horizonte desapareceu"
    # loadPaperRaceMini lê o seletor e passa o horizonte ao endpoint
    assert '($("paperRaceHours") || {}).value' in html, \
        "loadPaperRaceMini deixou de ler o seletor de horizonte"
    assert '"/api/paper/history?hours=" + hours' in html, \
        "a corrida deixou de passar hours ao /api/paper/history"
    assert '$("paperRaceHours").onchange' in html, \
        "o seletor de horizonte perdeu o onchange"
    # a legenda mostra stats (fechadas + win rate) por estratégia
    assert "fechadas · WR" in html, "a legenda deixou de mostrar trades fechadas + win rate"
    # o backend devolve closed/win_rate na série
    main_py = (Path("app") / "main.py").read_text(encoding="utf-8")
    assert '"win_rate": stats["win_rate"]' in main_py, \
        "o backend deixou de expor win_rate na série da corrida"
    assert 'min(hours, 720)' in main_py, \
        "o backend não aceita 30 dias (720h) de histórico"
    # o horizonte persiste na config (sobrevive a reloads) — boot sincroniza
    # o seletor a partir da config e o onchange grava
    common = _read(COMMON)
    assert 'c.paper_race_hours ?? 24' in common, \
        "o boot deixou de sincronizar o seletor com a config (paper_race_hours)"
    assert 'paper_race_hours: parseInt($("paperRaceHours").value, 10)' in html, \
        "o onchange do seletor deixou de gravar paper_race_hours na config"


def test_help_filtra_estrategias_ocultas():
    """A página HELP só mostra as estratégias em uso — as ocultas do menu
    (nova/auto_5 — referências de backtest) vêm marcadas `hidden` no backend
    e o `loadHelp()` filtra `!s.hidden`. Cada estratégia com página própria
    tem de manter uma explicação mecânica, incluindo o shadow-paper; as
    removidas a 01/09 não podem ter entrada no HELP."""
    html = _read(HELP)
    assert "filter(s => !s.hidden)" in html, \
        "loadHelp() deixou de filtrar as estratégias ocultas"
    for strategy_id in (
        "nova_gestao", "tp2_protect", "auto_escada_3",
        "auto_escada_3_hibrida", "auto_escada_5",
    ):
        assert f"  {strategy_id}: [" in html, \
            f"HELP perdeu o fluxo da estratégia visível {strategy_id}"
    assert "Shadow-paper" in html and "origin=shadow" in html, \
        "HELP perdeu a explicação do shadow-paper isolado"
    assert "modo defensivo" in html and "trailing" in html, \
        "HELP perdeu os conceitos de auto-gestão"
    # o backend marca as ocultas no registo
    strat_py = (Path("app") / "strategies.py").read_text(encoding="utf-8")
    assert '"hidden": sid in HIDDEN_STRATEGIES' in strat_py, \
        "o registo deixou de marcar as estratégias ocultas (hidden)"


def test_pagina_de_teste_fluxo_sim_walkforward_risco_saldo():
    """E2E estático do fluxo da página de teste (sem browser): o `loadTeste()`
    dispara a simulação do mês com `testeSimQS()` (risco + saldo 1000), carrega
    o walk-forward no mesmo fluxo, e os seletores de risco/saldo recarregam a
    simulação ao mudar. Se um elo do fluxo partir, este teste falha."""
    html = _read(TESTE)
    # 1) o carregador da página de teste existe e chama a simulação com os
    #    parâmetros de risco/saldo do testeSimQS()
    assert "async function loadTeste()" in html, "loadTeste() desapareceu"
    assert ('"/api/analysis/month?group=Golden%20Moves&days=" + days + "&strategy=" '
            "+ testeStrategy + testeSimQS()") in html, \
        "loadTeste() deixou de usar testeSimQS() na simulação"
    # 2) o walk-forward é carregado no mesmo fluxo
    assert "loadWalkforward(days)" in html, "loadTeste() deixou de carregar o walk-forward"
    assert ('"/api/analysis/walkforward?group=Golden%20Moves&days=" + days' in html), \
        "loadWalkforward() deixou de chamar /api/analysis/walkforward"
    # 3) os seletores de risco/saldo alimentam testeSimQS (default 1000) e
    #    recarregam a simulação ao mudar
    assert 'testeSimQS()' in html, "testeSimQS() desapareceu"
    assert '$("testeRisk").onchange' in html and '$("testeBalance").onchange' in html, \
        "seletores de risco/saldo perderam o onchange"
    assert 'edge_risk: $("testeRisk").value' in html, "risco deixou de persistir na config"
    assert 'edge_bal: $("testeBalance").value' in html, "saldo deixou de persistir na config"


def test_paginas_separadas_tem_menu_e_chrome_partilhado():
    """A divisão em páginas (2026-08, barra superior em 2026-09): o chrome
    (barra superior: navegação + estado + preços) é renderizado UMA vez no
    common.js — as páginas só trazem o placeholder vazio #appChrome e
    carregam o CSS/JS partilhados. Se o common.js perder o chrome, TODAS as
    páginas ficam sem navegação; se uma página perder o placeholder, fica
    sem menu."""
    for p in (INDEX, TESTE, DIAG, HELP, AUDIT):
        html = _read(p)
        assert 'id="appChrome"' in html, f"{p.name} perdeu o placeholder do chrome global"
        assert 'src="common.js"' in html, f"{p.name} deixou de carregar o common.js"
        assert 'href="style.css"' in html, f"{p.name} deixou de carregar o style.css"
    # o chrome partilhado vive no common.js (render + links + ações globais)
    common = _read(COMMON)
    for frag in ("\"principal\"", "\"/diagnostico\"", "\"/auditoria\"",
                 "\"/help\"", "\"/teste?s=\"",                 'id="btnKill"', 'id="btnLogout"',
                 'id="dropEstrategias"'):
        assert frag in common, f"o common.js perdeu o chrome partilhado ({frag})"
    # o common.js contém o render global + WS + hook de página
    assert "function render(s)" in common, "render() global desapareceu do common.js"
    assert "function connect()" in common, "WebSocket desapareceu do common.js"
    assert "pageRender" in common, "o hook pageRender desapareceu do common.js"
    # a página ativa é derivada do URL (deixa de estar copiada no HTML)
    assert "_currentPageId" in common, "a deteção da página ativa desapareceu"
    for p in (INDEX, TESTE, DIAG, HELP, AUDIT):
        assert 'class="term-menu-item active"' not in _read(p), \
            f"{p.name} ainda marca a página ativa no HTML (é derivada do URL)"


def test_replay_da_trade_exporta_fita_e_mostra_snapshots():
    html = _read(INDEX)
    assert 'id="tradeReplayPanel"' in html
    assert 'data-replay-signal' in html
    assert '/api/operations/replay/tape?signal_id=' in html
    assert 'include_snapshots: true' in html
    assert 'tradeReplayStep' in html
    main_py = (Path("app") / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/operations/replay/tape")' in main_py
    assert '@app.get("/api/operations/replay/snapshots")' in main_py
    db_py = (Path("app") / "db.py").read_text(encoding="utf-8")
    assert "def execution_tape" in db_py


def test_pagina_teste_comparativo_sem_undefined_na_legenda():
    """O comparativo da página de teste (loadTesteCompare) desenha a legenda
    com NAMES/COLORS para os ids que busca (`nova`, `nova_gestao` — o braço
    `tp2` puro foi removido com a estratégia a 01/09). Se um id faltar no
    mapa, o innerHTML renderiza o literal "undefined" na legenda."""
    html = _read(TESTE)
    assert 'nova: "TP1..TP5 sem gestão"' in html, \
        "a legenda do comparativo perdeu o nome de 'nova' (renderiza undefined)"
    assert 'nova_gestao: "TP1..TP5 + gestão"' in html, \
        "a legenda do comparativo perdeu o nome de 'nova_gestao'"
    assert 'tp2: "Sair no TP2 (puro)"' not in html, \
        "o braço tp2 removido (01/09) ainda está no comparativo"
    assert "NAMES[id] || STRATEGY_NAMES[id] || id" in html, \
        "a legenda do comparativo perdeu o fallback para ids desconhecidos"
    assert 'COLORS[id] || pal.axis' in html, \
        "a cor da legenda/curva perdeu o fallback (strokeStyle undefined)"


def test_janela_edicao_default_e_de_20_segundos():
    """A janela operacional por defeito deve dar 20s ao grupo para editar.
    O valor visível e o default do backend têm de permanecer alinhados."""
    html = _read(INDEX)
    config = _read(Path("app") / "config.py")
    assert 'id="editWinSlider"' in html
    assert 'id="editWinSlider" min="1" max="120" step="1" value="20"' in html
    assert 'id="editWinVal">20s</span>' in html
    assert '"edit_window_seconds": 20' in config


def test_slider_perda_diaria_persiste_limite():
    """O limite de perda diária (`daily_loss_limit_pct`) tem de ser editável
    por slider na dashboard: o input existe, o oninput escreve na config via
    POST /api/config (debounced) e o boot sincroniza o valor do config."""
    html = _read(INDEX)
    assert 'id="dailyLossSlider"' in html, \
        "o slider da perda diária desapareceu da dashboard"
    assert 'id="dailyLossVal"' in html, \
        "o valor vivo do slider desapareceu"
    assert "daily_loss_limit_pct: v" in html, \
        "o oninput deixou de persistir daily_loss_limit_pct na config"
    common = _read(COMMON)
    assert "dailyLossSlider" in common and "dailyLossVal" in common, \
        "o boot deixou de sincronizar o slider da perda diária com o config"


def test_tema_escuro_toggle_persistido_e_aplicado_antes_do_paint():
    """O tema escuro é uma camada `[data-theme=dark]` sobre as variáveis; o
    toggle vive no chrome (common.js) e persiste em localStorage. O atributo
    tem de estar definido antes do paint por um snippet no <head> de cada
    página (senão flash de tema claro) e o CSS tem de conter os overrides.
    Bug real evitado: um `--stripe: <lixo>` ou fundos fixos #f8fafc deixam
    linhas brancas no dark; a tabela usa a variável."""
    css = _read(Path("app") / "static" / "style.css")
    # camada dark apenas com variáveis (nenhum hex hardcoded fora das variáveis)
    assert '[data-theme="dark"]' in css
    assert "--stripe" in css, "a variável de striping da tabela desapareceu"
    assert "background: var(--stripe)" in css, \
        "a striping da tabela voltou a cor fixa clara (#f8fafc) e quebra no dark"
    # o chrome tem o botão + a lógica do toggle
    common = _read(COMMON)
    assert 'id="btnTheme"' in common, "o botão de tema desapareceu do chrome"
    assert "localStorage.setItem" in common and 'THEME_KEY = "nx_theme"' in common, \
        "o toggle perdeu a persistência em localStorage"
    assert "function chartPalette()" in common, \
        "a paleta adaptativa para os gráficos canvas desapareceu"
    # o tema segue o SO ao vivo enquanto NÃO houver escolha explícita
    assert 'matchMedia("(prefers-color-scheme: dark)")' in common, \
        "o follow do SO (prefers-color-scheme) desapareceu"
    assert 'mq.addEventListener("change", followSystemTheme)' in common, \
        "o listener de mudança de tema do SO desapareceu"
    assert "function followSystemTheme()" in common and "hasExplicitTheme" in common, \
        "o follow do SO perdeu o guard de escolha explícita"
    assert 'localStorage.getItem(THEME_KEY) !== null' in common, \
        "o follow do SO deixou de respeitar a escolha explícita (nx_theme guard)"
    # o arranque do chrome NÃO pode gravar o tema (gravar mataria o follow do SO)
    assert "_syncThemeButton(currentTheme())" in common, \
        "o arranque voltou a gravar o tema (applyTheme) e mata o follow do SO"
    # todas as páginas (incl. login) aplicam o tema antes do paint no <head>
    for p in (INDEX, TESTE, DIAG, HELP, AUDIT, Path("app") / "static" / "login.html"):
        html = _read(p)
        assert "data-theme=\"dark\"" in html or "document.documentElement.dataset.theme" in html, \
            f"{p.name} não aplica o tema escuro antes do paint (head snippet)"
        sec = html.split("<head>", 1)[1].split("</head>", 1)[0]
        assert "nx_theme" in sec, f"{p.name}: o snippet de tema do <head> desapareceu"
    # os gráficos canvas das páginas usam a paleta (nada de cinzas fixos claros)
    for p in (TESTE, DIAG, AUDIT):
        html = _read(p)
        assert 'chartPalette()' in html, \
            f"{p.name}: os charts deixaram de usar a paleta adaptativa ao tema"
    index = _read(INDEX)
    assert index.count('chartPalette()') >= 3, \
        "a principal deixou de adaptar os charts (equity/recounts/corrida) ao tema"


def test_estrategias_removidas_nao_existem_no_chrome_partilhado():
    """Remoção completa (01/09): tp2/tp2_gestao/nova_gestao_3/auto_2/
    auto_protect_5/auto_escada_3_struggle não podem aparecer no chrome
    partilhado (menus/mapas do common.js) — zero referências a estratégias
    que já não existem no registo backend."""
    import re as _re
    common = _read(COMMON)
    for sid in ("tp2_gestao", "nova_gestao_3", "auto_2",
                "auto_protect_5", "auto_escada_3_struggle"):
        assert not _re.search(rf"\b{_re.escape(sid)}\b", common), \
            f"{sid} ainda referenciada no common.js após a remoção de 01/09"
    # `tp2` como ESTRATÉGIA (id removido) não aparece: o menu/estados só
    # usam o `tp2` como ID DA LANE (2ª conta demo — ver /segundaria); um
    # item de estratégia `teste-tp2` ou um rótulo de estratégia voltaria a
    # violar a remoção
    assert "teste-tp2" not in common, \
        "a estratégia removida tp2 voltou ao menu de estratégias"
    assert not _re.search(r"(?:auto_|nova_|tp2_)[a-z0-9_]*tp2[a-z0-9_]*", common), \
        "tp2 ainda referenciada como id/parte de id de estratégia no common.js"
    # as referências de backtest (nova/auto_5) continuam nos mapas de nomes
    assert "nova_gestao_fallback" in common and "auto_escada_3_hibrida" in common
    # TERM_ORDER deriva do STRATEGY_MENU (fonte única): "teste-" + id por item
    assert 'STRATEGY_MENU.map(s => "teste-" + s.id)' in common, \
        "TERM_ORDER deixou de derivar do STRATEGY_MENU"
    assert "STRATEGY_MENU" in common and "TERM_ORDER" in common, \
        "o menu deixou de ter fonte única (STRATEGY_MENU/TERM_ORDER no common.js)"
    # e todas as páginas carregam o chrome partilhado (menu renderizado)
    for p in (INDEX, TESTE, DIAG, HELP, AUDIT):
        assert 'src="common.js"' in _read(p), f"{p.name} deixou de carregar o common.js"


def test_segundaria_menu_chips_feed_e_confronto():
    """Regressões da página /segundaria (lane tp2) e do feed dual-lane:

    - o menu do chrome tem a entrada SEGUNDA CONTA com href /segundaria e o
      router de página reconhece o path;
    - o index.html lê ?lane= (LANE_VIEW) e aplica-o ao relatório/render;
    - o feed tem os chips (tudo | main | tp2 | risco) e o campo lane aparece
      no render e no prepend;
    - existe a tab ⚔️ Confronto ligada ao /api/lanes/ab/legs;
    - o backend serve a rota /segundaria com o ?lane=tp2 injetado.
    """
    common = _read(COMMON)
    index = _read(INDEX)

    # menu
    assert 'href: "/segundaria"' in common, "o menu perdeu a entrada /segundaria"
    assert 'SEGUNDA CONTA' in common, "o rótulo SEGUNDA CONTA saiu do menu"
    assert 'startsWith("/segundaria")' in common, \
        "o router de página deixou de reconhecer /segundaria"

    # vista de lane no index
    assert "LANE_VIEW" in index and '"lane"' in index, \
        "o index deixou de ler o parâmetro ?lane="
    assert '"/api/report?days=" + days + (LANE_VIEW' in index, \
        "o relatório deixou de passar lane= na vista de lane"
    assert 'data-feed-filter="main"' in index and \
        'data-feed-filter="tp2"' in index and \
        'data-feed-filter="risco"' in index, \
        "os chips de filtro do feed (main/tp2/risco) desapareceram"
    assert 'feedPasses(' in index and 'n.lane' in index, \
        "o filtro do feed deixou de olhar para o campo lane"
    assert 'feed-lane' in index, \
        "a badge de lane nos itens do feed desapareceu"

    # tab Confronto
    assert 'data-tab="confronto"' in index, "a tab Confronto saiu dos tabs"
    assert 'loadConfronto(' in index and '/api/lanes/ab/legs' in index, \
        "a tab Confronto deixou de ligar ao confronto perna-a-perna"

    # risco por lane na dashboard
    assert "risk_preview_by_lane" in index, \
        "o card de risco deixou de usar o preview por lane"
    assert 'id="riskLane2"' in index, "o card de dimensionamento da lane 2 desapareceu"
    # o feedback IMEDIATO do slider também é por lane (bug real: ao mexer no
    # slider na /segundaria os chips voltavam a valores da conta primária)
    assert "laneAccount(last) || last.account" in index, \
        "o preview local do slider voltou a usar a conta primária"
    assert "risk_preview_by_lane || {})[LANE_VIEW]" in index, \
        "o preview local do slider deixou de usar as pernas da lane"
    assert "laneAccount(s) || s.account" in index, \
        "o modeHint voltou a usar o equity da conta primária"
    # e o rodapé (conta) na vista de lane usa o login da lane
    assert 'new URLSearchParams(location.search).get("lane")' in common, \
        "o rodapé deixou de distinguir a vista de lane"


def test_rota_segundaria_no_backend():
    """A rota /segundaria existe no backend e injeta ?lane=tp2 para o JS."""
    main_py = Path("app") / "main.py"
    src = main_py.read_text(encoding="utf-8")
    assert '"/segundaria"' in src and 'page_segundaria' in src, \
        "a rota /segundaria desapareceu do main.py"
    assert "?lane=tp2" in src, "a rota /segundaria deixou de injetar ?lane=tp2"


def test_risco_por_lane_no_slider_e_dry_run_no_card():
    """Risco por LANE (risk_pct:main/risk_pct:tp2) + resumo dry no card:

    - o slider usa a base da lane (`risk_pct_by_lane`) e, na /segundaria, o
      label mostra o risco EFETIVO (pós-drawdown) da lane;
    - o POST do slider na vista tp2 altera só `risk_pct_tp2`;
    - o card da lane mostra o resumo simulado (dry-run) ao lado do saldo."""
    common = _read(COMMON)
    index = _read(INDEX)

    # base por lane no slider (posição = base da lane)
    assert "risk_pct_by_lane" in common, \
        "o slider deixou de ler a base por lane (risk_pct_by_lane)"
    assert '"% · efetivo"' in common, \
        "o label do slider deixou de mostrar o risco efetivo na vista de lane"
    assert "function _laneViewId()" in common, \
        "o helper de vista de lane saiu do common.js"

    # POST por lane no drag do slider
    assert "{ risk_pct_tp2: v }" in index, \
        "o slider da /segundaria deixou de guardar risk_pct:tp2"
    assert "{ risk_pct_main: v }" in index, \
        "o slider da vista main deixou de guardar risk_pct:main"

    # resumo dry por lane no card (stats da /segundaria + card lane 2)
    assert "dry_run" in index and "leSim" in index, \
        "o card da lane deixou de mostrar o resumo dry-run"
    assert 'id="rc2SimBox"' in index, \
        "o card da lane 2 perdeu o chip do resumo simulado (dry)"
    assert "running_total" not in index  # sanity: sem artefactos do edit


def test_risk_slider_local_updates_lots():
    """O slider recalcula os LOTES localmente (não só perTrade/perLeg) para a
    vista atual e para o card da lane tp2 — antes os lotes ficavam no valor do
    snapshot até ao próximo tick."""
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "function previewLots" in html
    assert '"rcLots"' in html and "previewLots" in html
    # o feedback local tem de usar a conta/sl_dist/legs da vista, não fixos
    assert "function lpView()" in html
    assert 'LANE_VIEW !== "tp2"' in html  # card tp2 responde ao slider na principal
