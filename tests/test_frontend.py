"""Regressões do frontend (dashboard vanilla JS — páginas separadas em
app/static/).

A suite pytest não executa o JS, por isso estas regressões são estáticas:
leem o HTML/JS e garantem contratos que já causaram bugs reais. Se uma delas
falhar, o frontend voltou a um comportamento errado.

A dashboard dividiu-se em páginas multi-página (2026-08): `index.html` (a
principal), `teste.html` (simulações de estratégias), `diagnostico.html` e
`help.html`, com o JS partilhado em `common.js` e o CSS em `style.css`.
"""

from pathlib import Path

STATIC = Path("app") / "static"
INDEX = STATIC / "index.html"
TESTE = STATIC / "teste.html"
DIAG = STATIC / "diagnostico.html"
HELP = STATIC / "help.html"
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
    """A página HELP só mostra as estratégias em uso — as ocultas
    (tp2/tp2_protect) vêm marcadas `hidden` no backend e o `loadHelp()`
    filtra `!s.hidden` (não podem voltar a aparecer no HELP)."""
    html = _read(HELP)
    assert "filter(s => !s.hidden)" in html, \
        "loadHelp() deixou de filtrar as estratégias ocultas"
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
    """A divisão em páginas (2026-08): cada página tem o menu de navegação, o
    terminal direito (kill switch + sair) e carrega o CSS/JS partilhados —
    se uma página perder o chrome, fica sem navegação."""
    for p in (INDEX, TESTE, DIAG, HELP):
        html = _read(p)
        assert 'href="/"' in html, f"{p.name} perdeu o link para a principal"
        assert 'href="/diagnostico"' in html, f"{p.name} perdeu o link para o diagnóstico"
        assert 'href="/help"' in html, f"{p.name} perdeu o link para o HELP"
        assert 'href="/teste?s=' in html, f"{p.name} perdeu os links das páginas de teste"
        assert 'id="btnKill"' in html, f"{p.name} perdeu o kill switch"
        assert 'id="btnLogout"' in html, f"{p.name} perdeu o botão sair"
        assert 'src="common.js"' in html, f"{p.name} deixou de carregar o common.js"
        assert 'href="style.css"' in html, f"{p.name} deixou de carregar o style.css"
    # o common.js contém o chrome partilhado (render global + WS + boot)
    common = _read(COMMON)
    assert "function render(s)" in common, "render() global desapareceu do common.js"
    assert "function connect()" in common, "WebSocket desapareceu do common.js"
    assert "pageRender" in common, "o hook pageRender desapareceu do common.js"
