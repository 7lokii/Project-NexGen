"""End-to-end API tests.

These exercise the full HTTP surface: ingest, manual/auto modes, the risk
checks, the kill switch, config validation and the Telegram edit window.

The TestClient is used as a context manager (module-scoped fixture) so the
FastAPI lifespan runs and the 1-second background loop is alive — that loop
is what finalizes buffered signals after their edit window closes.

The simulated market is frozen (sim_speed = 0) so trades stay open and the
tests are deterministic.
"""

import os
import tempfile
import time

import pytest

# Point the app at a throwaway DB and force simulated prices (no network)
# *before* importing it.
_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["SIGNALBOT_DB"] = _TMP_DB
os.environ["MARKET_PROVIDER"] = "none"
# the app loads .env for MT5 credentials — tests must stay on the paper
# broker (deterministic balance, no connection to the real terminal)
os.environ["EXECUTION_MODE"] = "sim"
os.environ["MT5_DRY_RUN"] = "1"
# auth off nos testes: o .env pode ter a password da dashboard definida e
# o load_dotenv(override=False) do main.py respeitaria este valor vazio.
os.environ["SIGNALBOT_DASHBOARD_PASSWORD"] = ""
os.environ["SIGNALBOT_API_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

EURUSD = "BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920"
EURUSD4 = ("BUY EURUSD @ 1.0850\nSL 1.0820\n"
           "TP1 1.0890\nTP2 1.0920\nTP3 1.0950\nTP4 1.0980")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.post("/api/config", json={"sim_speed": 0})
        yield c


@pytest.fixture(autouse=True)
def _isolated(client):
    """Isolamento por teste: o TestClient é module-scoped (um único estado),
    por isso um teste que falha a meio deixava config alterada (edit_window=0,
    mode=auto, killswitch on, trades abertas) e contaminava os seguintes — a
    cascata que tornava a suite flaky. Este setup corre SEMPRE antes de cada
    teste e repõe o baseline (mesmo que o teste anterior tenha falhado)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={
        "mode": "manual", "edit_window_seconds": 60, "max_positions": 5,
        "risk_pct": 2.0, "strategy": "nova_gestao", "sim_speed": 0,
    })
    client.post("/api/killswitch", json={"enabled": False})
    yield


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Project NexGen" in r.text


def test_status_defaults(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["config"]["mode"] == "manual"
    assert data["account"]["balance"] == 10000.0


def test_ingest_manual_is_pending(client):
    r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "pending"
    assert data["signal"]["symbol"] == "EURUSD"
    assert data["signal"]["status"] == "pending"


def test_ingest_bad_text_fails_gracefully(client):
    r = client.post("/api/signals", json={"raw_text": "ola mundo"})
    data = r.json()
    assert data["action"] == "parse_error"
    assert data["signal"]["status"] == "failed"


def test_approve_executes_and_opens_trade(client):
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    r = client.post(f"/api/signals/{sid}/approve")
    assert r.json()["action"] == "executed"
    st = client.get("/api/status").json()
    assert st["account"]["open_trades"] >= 1
    assert any(t["id"] == r.json()["trade"]["id"] for t in st["trades"])


def test_reject_marks_rejected(client):
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    r = client.post(f"/api/signals/{sid}/reject")
    assert r.json()["action"] == "rejected"
    st = client.get("/api/status").json()
    assert any(s["id"] == sid and s["status"] == "rejected" for s in st["signals"])


def test_auto_mode_executes_immediately(client):
    client.post("/api/config", json={"mode": "auto"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    client.post("/api/config", json={"mode": "manual"})


def test_four_leg_signal_not_blocked_by_max_positions(client):
    """Regressão do caso real de hoje: sinais com TP1..TP4 (4 pernas) não
    podem ser rejeitados pelo limite de posições — max_positions sobe para 5."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "max_positions": 5})
    assert client.get("/api/status").json()["config"]["max_positions"] == 5
    r = client.post("/api/signals", json={"raw_text": EURUSD4})
    sid = r.json()["signal"]["id"]
    r = client.post(f"/api/signals/{sid}/approve")
    assert r.json()["action"] == "executed"
    trade = r.json()["trade"]
    assert trade["lots_1"] > 0 and trade["lots_2"] > 0 and trade["lots_3"] > 0 and trade["lots_4"] > 0
    assert not trade["lots_5"]
    _close_all_open_trades(client)


def test_trade_open_notification_includes_risk_audit(client):
    """A notificação trade_open reporta o montante real em risco (€/USD)
    e a % de equity usada, para auditoria no feed."""
    from app.main import state
    client.post("/api/config", json={"mode": "auto", "risk_pct": 1.0})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    notes = client.get("/api/notifications").json()
    n = next((x for x in notes if x["kind"] == "trade_open"), None)
    assert n is not None
    assert "risco 1.0% da equity = " in n["text"]
    assert any(ch in n["text"] for ch in (" USD", " EUR"))
    # limpeza: fecha a posição aberta (mercado congelado) para não poluir
    # os checks de risco dos testes seguintes
    for t in state.db.open_trades():
        state.sim.close_trade_at_market(t.id)
    client.post("/api/config", json={"mode": "manual", "risk_pct": 2.0})


def test_report_includes_by_day(client):
    # independente do fluxo de execução: grava uma trade fechada diretamente
    from app.main import state
    from app.models import Trade
    now = "2026-08-17T12:00:00"
    state.db.insert_trade(Trade(
        id="byday-test-1", signal_id="byday-sig-1", symbol="EURUSD", direction="BUY",
        entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890, take_profit_2=1.0920,
        risk_pct=1.0, lots_1=0.1, lots_2=0.1, status="closed", opened_at=now,
        closed_at=now, pnl=12.5,
    ))
    rep = client.get("/api/report?days=30").json()
    assert isinstance(rep["by_day"], list)
    assert rep["by_day"], "pelo menos 1 dia com trades fechadas"
    assert all(k in rep["by_day"][0] for k in ("day", "trades", "wins", "win_rate", "pnl"))
    # o dia da trade gravada tem P&L > 0 (não o último elemento — outros
    # testes podem fechar trades com a data de hoje e pnl 0)
    day = next((d for d in rep["by_day"] if d["day"] == now[:10]), None)
    assert day is not None and day["pnl"] > 0


def test_kill_switch_blocks_auto_execution(client):
    client.post("/api/config", json={"mode": "auto"})
    client.post("/api/killswitch", json={"enabled": True})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "blocked"
    assert any("kill switch" in reason for reason in r.json()["reasons"])
    client.post("/api/killswitch", json={"enabled": False})
    client.post("/api/config", json={"mode": "manual"})


def test_config_validation_rejects_extreme_risk(client):
    r = client.post("/api/config", json={"risk_pct": 50})
    assert r.status_code == 422


def test_daily_summary_endpoint(client):
    r = client.post("/api/summary")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_telegram_edit_window_flow(client):
    """Mensagens do Telegram INCOMPLETAS ficam em buffer; edições atualizam-nas
    e só depois de a janela fechar o sinal continua (auto -> executa com o
    conteúdo editado). Sinais completos executam imediatamente (teste à parte)."""
    client.post("/api/config", json={"mode": "auto", "edit_window_seconds": 30})

    # 1) primeira versão incompleta (sem SL/TP) é buffered, NÃO executa
    r = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0850", "source": "telegram", "message_id": "777"})
    data = r.json()
    assert data["action"] == "buffered"
    assert data["signal"]["status"] == "awaiting_edit"
    st = client.get("/api/status").json()
    sig = next(s for s in st["signals"] if s["id"] == "tg-777")
    assert sig["status"] == "awaiting_edit"
    assert not any(t["signal_id"] == "tg-777" for t in st["trades"])

    # 2) uma edição chega: re-parseia e atualiza a entrada, continua em buffer
    r = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0860\nSL 1.0830\nTP1 1.0900\nTP2 1.0930",
        "source": "telegram", "message_id": "777"})
    data = r.json()
    assert data["action"] == "edited"
    assert data["signal"]["entry"] == 1.0860
    assert data["signal"]["status"] == "awaiting_edit"

    # 3) close the window and re-edit so the deadline resets to ~now
    client.post("/api/config", json={"edit_window_seconds": 0})
    client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0860\nSL 1.0830\nTP1 1.0900\nTP2 1.0930",
        "source": "telegram", "message_id": "777"})
    # poll instead of sleep: o loop de fundo de 1s finaliza o buffered e o
    # sleep fixo competia com o tick (flaky sob carga)
    st = _wait_for(client, lambda s: _sig_status(s, "tg-777") == "executed")

    sig = next(s for s in st["signals"] if s["id"] == "tg-777")
    assert sig["status"] == "executed"
    # executed with the EDITED entry (1.0860), not the original 1.0850
    tg_trades = [t for t in st["trades"] if t["signal_id"] == "tg-777"]
    assert len(tg_trades) == 1
    assert tg_trades[0]["entry"] == 1.0860

    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})


def test_failed_signal_revived_when_group_completes_edit(client):
    """A signal that failed (incomplete entry) is REVIVED when the group
    edits the message to complete it — the window reopens instead of the
    edit being ignored."""
    client.post("/api/config", json={"mode": "auto", "edit_window_seconds": 0})

    # 1) incomplete signal (no SL/TP yet) -> buffered, then fails
    r = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0860", "source": "telegram", "message_id": "999"})
    data = r.json()
    assert data["action"] == "buffered"
    st = _wait_for(client, lambda s: _sig_status(s, "tg-999") == "failed")

    sig = next(s for s in st["signals"] if s["id"] == "tg-999")
    assert sig["status"] == "failed"

    # 2) the group completes the edit -> signal revived, window reopens
    r = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0860\nSL 1.0830\nTP1 1.0900\nTP2 1.0930",
        "source": "telegram", "message_id": "999"})
    data = r.json()
    assert data["action"] == "revived"
    assert data["signal"]["status"] == "awaiting_edit"
    assert data["signal"]["entry"] == 1.0860
    assert data["signal"]["edited_at"]  # edit time recorded

    # 3) window closes again -> executes with the completed content
    st = _wait_for(client, lambda s: _sig_status(s, "tg-999") == "executed")

    sig = next(s for s in st["signals"] if s["id"] == "tg-999")
    assert sig["status"] == "executed"
    tg_trades = [t for t in st["trades"] if t["signal_id"] == "tg-999"]
    assert len(tg_trades) == 1

    # 4) the edit delay shows up in /api/edits (source-less = all groups)
    ed = client.get("/api/edits").json()
    assert ed["count"] >= 1
    assert ed["max_seconds"] >= 0
    assert "median_seconds" in ed and "examples" in ed

    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})


def test_multiple_groups_same_message_id_do_not_collide(client):
    """The same Telegram message id in two groups produces two independent
    buffered signals; editing one does not touch the other."""
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 30})

    # incompleto → buffered (o sinal completo já não passa pela janela)
    incomplete = "BUY EURUSD @ 1.0850"
    r1 = client.post("/api/signals", json={
        "raw_text": incomplete, "source": "tg:Group A", "message_id": "99", "group": "111"})
    r2 = client.post("/api/signals", json={
        "raw_text": incomplete, "source": "tg:Group B", "message_id": "99", "group": "222"})
    assert r1.json()["signal"]["id"] == "tg-111-99"
    assert r2.json()["signal"]["id"] == "tg-222-99"
    assert r1.json()["action"] == "buffered"
    assert r2.json()["action"] == "buffered"

    # edit Group A's message only
    client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0860\nSL 1.0830\nTP1 1.0900\nTP2 1.0930",
        "source": "tg:Group A", "message_id": "99", "group": "111"})
    st = client.get("/api/status").json()
    a = next(s for s in st["signals"] if s["id"] == "tg-111-99")
    b = next(s for s in st["signals"] if s["id"] == "tg-222-99")
    assert a["entry"] == 1.0860
    # B continua incompleta (entrada 0.0) — a edição de A não a tocou
    assert b["entry"] == 0.0
    client.post("/api/config", json={"edit_window_seconds": 60})


def test_historical_signal_registered_not_executed(client):
    """Past group messages (historical=True) are registered but never executed,
    even in auto mode."""
    client.post("/api/config", json={"mode": "auto"})
    r = client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "1001", "group": "111", "historical": True})
    data = r.json()
    assert data["action"] == "historical"
    assert data["signal"]["status"] == "historical"
    st = client.get("/api/status").json()
    assert not any(t["signal_id"] == "tg-111-1001" for t in st["trades"])
    client.post("/api/config", json={"mode": "manual"})


def test_delete_signal_closes_open_position(client):
    """Deleting a Telegram message marks the signal deleted and closes the
    position opened from it (at market)."""
    # earlier tests may already hold open trades; lift the cap so the approve
    # of this test's signal is not blocked by max_positions
    client.post("/api/config", json={"max_positions": 50})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")
    st = client.get("/api/status").json()
    assert st["account"]["open_trades"] >= 1

    r = client.post(f"/api/signals/{sid}/delete")
    assert r.json()["action"] == "deleted"
    assert r.json()["closed_trades"] == 1

    st = client.get("/api/status").json()
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert sig["status"] == "deleted"
    assert not any(t["signal_id"] == sid and t["status"] == "open" for t in st["trades"])


def test_analysis_compare_endpoint(client):
    """A/B backtest endpoint: strategy variants + drawdown, same signals."""
    # seed at least one Golden Moves signal so the variants have data
    # (the run_compare needs a drawdown to exist on every variant)
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "7100", "group": "111", "historical": True})
    r = client.get("/api/analysis/compare?group=Golden%20Moves&days=7")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "new" in data and "manage" in data and "tp2" in data
    assert "old" not in data
    assert "compare" in data and "winner" in data["compare"]
    assert data["new"]["max_legs"] is None
    assert "drawdown" in data["new"] and "drawdown" in data["manage"]
    assert "amount" in data["new"]["drawdown"]


def test_analysis_month_sim_endpoint(client):
    """Month simulation: same signals with management + an equity curve that
    starts at the current balance (se tivesses começado há 1 mês...)."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "7200", "group": "111", "historical": True})
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=7")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["manage"] is True
    assert "equity_curve" in data
    assert "start_balance" in data and "end_balance" in data
    assert data["start_balance"] == data["balance"]


def test_strategies_registry_endpoint(client):
    """O registo central de estratégias: lista + qual é a principal."""
    r = client.get("/api/strategies")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["main"] == "nova_gestao"
    ids = [s["id"] for s in data["strategies"]]
    assert "nova" in ids and "nova_gestao" in ids and "tp2" in ids
    assert "nova_gestao_3" in ids  # TP1..TP3 + gestão (seguro contra regime)
    ng3 = next(s for s in data["strategies"] if s["id"] == "nova_gestao_3")
    assert ng3["max_legs"] == 3 and ng3["manage"] is True
    assert "tp2_gestao" in ids  # Sair no TP2 + gestão do grupo
    t2g = next(s for s in data["strategies"] if s["id"] == "tp2_gestao")
    assert t2g["manage"] is True and t2g["exit_at_tp"] == 2
    # o tp2 foi renomeado para refletir o SL/BE (o id mantém-se)
    t2 = next(s for s in data["strategies"] if s["id"] == "tp2")
    assert "TP1→BE" in t2["nome"]
    # consolidação (19/08, spec): tp2/tp2_gestao/nova_gestao_3/auto_2 fora do
    # menu (HIDDEN), nova/auto_5 ocultas do menu mas em papel (MENU_HIDDEN) —
    # o registo mantém as 12 para o backtest/compare histórico
    hidden = {s["id"] for s in data["strategies"] if s["hidden"]}
    assert hidden == {"tp2", "tp2_gestao", "nova_gestao_3", "auto_2",
                      "nova", "auto_5"}
    vis = [s for s in data["strategies"] if not s["hidden"]]
    assert {s["id"] for s in vis} == {"nova_gestao", "tp2_protect",
                                        "auto_3", "auto_escada_3",
                                        "auto_escada_3_hibrida",
                                        "auto_escada_5"}
    assert len([s for s in data["strategies"]]) == 12
    # a estratégia principal está marcada no snapshot
    st = client.get("/api/status").json()
    assert st["config"]["strategy"] == "nova_gestao"


def test_sim_params_auto_gestao(client):
    """sim_params devolve os parâmetros de auto-gestão (escada/confirmação)
    e o registo mantém manage=False nas variantes auto_* (ignoram o grupo)."""
    from app.strategies import sim_params
    esc5 = sim_params("auto_escada_5")
    assert esc5["manage"] is False
    assert esc5["stair_levels"] == 5
    assert esc5["stair_from"] == 1
    assert esc5["confirm_filter"] is True
    # trail_factor 2.0 é o valor validado no sweep (buffer ~8 pips em gold,
    # a paciência real do grupo) — não None
    assert esc5["trail_factor"] == 2.0 and esc5["time_stop_minutes"] is None
    esc3 = sim_params("auto_escada_3")
    assert esc3["stair_levels"] == 3 and esc3["confirm_filter"] is True
    assert esc3["trail_factor"] == 2.0
    a2 = sim_params("auto_2")
    assert a2["max_legs"] == 2
    # a referência (nova_gestao) continua a gerir pelo grupo, sem escada
    ref = sim_params("nova_gestao")
    assert ref["manage"] is True and ref["stair_levels"] is None


def test_trace_endpoint_empty(client):
    """`/api/analysis/trace`: sem sinais no período devolve found=False
    sem rebentar (o exemplo de fluxo da página do TP2 fica escondido)."""
    r = client.get("/api/analysis/trace?strategy=tp2&since=2030-01-01")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["found"] is False
    assert data["strategy_id"] == "tp2"


def test_analysis_accepts_risk_and_balance_params(client):
    """Estudos de risco: `risk_pct` e `balance` opcionais nos endpoints de
    análise (None/0 = config/saldo atual)."""
    # sem sinais no período (since futuro) — payload vazio mas com os params
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=0&strategy=nova_gestao&since=2030-01-01&risk_pct=2&balance=10000")
    assert r.status_code == 200
    data = r.json()
    assert data["risk_pct"] == 2
    assert data["balance"] == 10000
    assert data["daily_stop_pct"] is not None


def test_month_sim_accepts_strategy(client):
    """run_month_sim com strategy_id: parâmetros da estratégia aplicados
    (tp2 -> exit_at_tp=2, manage=False)."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "7300", "group": "111", "historical": True})
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=7&strategy=tp2")
    assert r.status_code == 200
    data = r.json()
    assert data["strategy_id"] == "tp2"
    assert data["exit_at_tp"] == 2
    assert data["manage"] is False
    assert "equity_curve" in data


def test_config_accepts_strategy_switch(client):
    """Mudar a estratégia ativa via /api/config (validada contra o registo)."""
    r = client.post("/api/config", json={"strategy": "tp2"})
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["config"]["strategy"] == "tp2"
    r = client.post("/api/config", json={"strategy": "nao_existe"})
    assert r.status_code == 200
    assert r.json().get("error")
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_paper_forward_accounts_in_status(client):
    """Paper trading forward: o snapshot expõe as contas virtuais das
    estratégias — incluindo a principal (cada uma com a sua conta de 1000 €)."""
    st = client.get("/api/status").json()
    accts = (st.get("paper") or {}).get("accounts") or []
    ids = [a["strategy_id"] for a in accts]
    assert "nova_gestao" in ids
    main = next(a for a in accts if a["strategy_id"] == "nova_gestao")
    assert main["is_main"] is True
    assert main["balance"] == 1000.0


def test_paper_forward_opens_on_execution(client):
    """Quando um sinal executa na principal, as estratégias de teste abrem a
    mesma trade em papel (cada uma com a sua conta virtual de 1000 €)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    st = client.get("/api/status").json()
    accts = (st.get("paper") or {}).get("accounts") or []
    test_accts = {a["strategy_id"]: a for a in accts if not a["is_main"]}
    # consolidação (19/08): na corrida só as visíveis (nova/auto_5 ocultas
    # do menu continuam em papel mas não na lista; tp2/tp2_gestao/
    # nova_gestao_3/auto_2 deixaram de abrir papel)
    for sid in ("tp2_protect", "auto_3", "auto_escada_3", "auto_escada_5"):
        assert sid in test_accts and test_accts[sid]["balance"] is not None, sid
    assert "nova" not in test_accts and "auto_5" not in test_accts
    assert "tp2" not in test_accts and "auto_2" not in test_accts
    # a principal TAMBÉM tem conta virtual própria (1000 €) — a corrida
    # paralela compara todas as estratégias em igualdade de circunstâncias
    main = next(a for a in accts if a["is_main"])
    assert main["strategy_id"] == "nova_gestao"
    assert main["balance"] is not None


def test_paper_main_account_same_signal_id_open_close(client):
    """A conta de papel da PRINCIPAL abre e fecha a MESMA trade do sinal,
    com o mesmo signal_id das estratégias de teste — a corrida paralela não
    diverge da real (regressão do fix que deu conta virtual à principal)."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    # ABERTURA: as estratégias em papel — principal, Fase 2, a `nova`
    # (contrafactual, MENU_HIDDEN) e a auto_5 (comparação, MENU_HIDDEN) —
    # têm uma trade aberta com o MESMO signal_id (nada órfão)
    for sid_ in ("nova_gestao", "nova", "auto_5", "tp2_protect", "auto_escada_3"):
        pt = _paper_trade_for_signal(db, sid_, sid)
        assert pt is not None, f"{sid_} não abriu a trade de papel do sinal"
        assert pt["signal_id"] == sid
        assert pt["status"] == "open"
    # as visíveis restantes também abriram (auto_3/auto_escada_5); as
    # removidas da consolidação (tp2/tp2_gestao/nova_gestao_3/auto_2) JÁ NÃO
    # abrem papel — só ficam no backtest/compare
    for sid_ in ("auto_3", "auto_escada_5"):
        assert _paper_trade_for_signal(db, sid_, sid) is not None, sid_
    for sid_ in ("tp2", "nova_gestao_3", "tp2_gestao", "auto_2"):
        assert _paper_trade_for_signal(db, sid_, sid) is None, sid_

    # FECHO: "we're out" → close_all fecha a real E a de papel da principal
    # (manage_kinds inclui close_all) — com o mesmo signal_id
    r = client.post("/api/signals", json={
        "raw_text": "We're out", "source": "tg:Golden Moves",
        "message_id": "7210", "group": "g"})
    assert r.json()["kind"] == "close_all"
    assert r.json()["applied"] is True

    pt = _paper_trade_for_signal(db, "nova_gestao", sid, include_closed=True)
    assert pt is not None and pt["status"] == "closed", \
        "a trade de papel da principal tem de fechar com a gestão"
    assert pt["signal_id"] == sid
    assert pt["exit_reason"] == "close_all"
    # a sem gestão (nova) continua aberta — não diverge nem fecha sozinha
    assert _paper_trade_for_signal(db, "nova", sid)["status"] == "open"


def test_paper_daily_stop_skips_open(client):
    """O kill diário (daily_stop_pct) aplica-se à corrida paper: quando o P&L
    realizado de hoje da conta virtual ≤ -saldo×pct/100, o sinal NÃO abre em
    papel nessa estratégia (espelho da produção) — e abre noutras."""
    from datetime import datetime, timezone
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal, Trade

    db = DB(DB_PATH)
    mkt = MarketSimulator()
    mkt.ensure("XAUUSD", 4416.0)
    race = PaperRace(db, mkt)
    today = datetime.now(timezone.utc).date().isoformat()
    # fecha uma trade de papel HOJE com perda > 5% do saldo virtual (1000 €)
    balance = db.ensure_paper_account("nova", 1000.0)
    db.update_paper_balance("nova", balance)
    db.insert_paper_trade(Trade(
        id="ds-stop-1", strategy_id="nova", signal_id="ds-sig-0",
        symbol="XAUUSD", direction="buy", entry=4416.0, stop_loss=4408.0,
        take_profit_1=4419.0, take_profit_2=4422.0, risk_pct=5.0,
        lots_1=0.1, lots_2=0.1, status="closed",
        opened_at=today + "T09:00:00", closed_at=today + "T10:00:00",
        pnl=-60.0, exit_reason="sl",
    ))
    sig = Signal(id="ds-sig-1", source="tg:Test", raw_text="",
                 symbol="XAUUSD", direction="buy", entry=4416.0,
                 stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
                 status="historical", received_at=today + "T11:00:00")
    try:
        opened = race.open_for_signal(sig, 5.0, strategy_ids=["nova", "nova_gestao"],
                                      daily_stop_pct=5.0)
        # nova estourou o limite (-60 ≤ -50) → não abre; a outra abre
        assert all(t.strategy_id != "nova" for t in opened)
        assert any(t.strategy_id == "nova_gestao" for t in opened)
        assert race._skipped_daily_stop.get("nova") == 1
        # sem limite (pct=0) a nova abre
        opened2 = race.open_for_signal(sig, 5.0, strategy_ids=["nova"],
                                       daily_stop_pct=0)
        assert len(opened2) == 1 and opened2[0].strategy_id == "nova"
    finally:
        with db.tx() as cur:
            cur.execute("DELETE FROM paper_trades WHERE id='ds-stop-1'")
            for t in opened2 or []:
                cur.execute("DELETE FROM paper_positions WHERE trade_id=?", (t.id,))
                cur.execute("DELETE FROM paper_trades WHERE id=?", (t.id,))
            cur.execute("DELETE FROM paper_accounts WHERE strategy_id='nova'")


def test_real_vs_backtest_endpoint(client, monkeypatch):
    """`/api/analysis/real_vs_backtest`: compara a trade REAL fechada com o
    backtest do mesmo sinal — entrada real vs fill do sim, saída vs nível
    esperado, atraso e P&L (SPEC v2 §3.4)."""
    from app.main import state
    from app.models import Position, Signal, Trade

    now = "2026-08-18T10:00:00+00:00"
    state.db.insert_signal(Signal(
        id="rvb-sig-1", source="tg:Test", raw_text="", symbol="XAUUSD",
        direction="buy", entry=4416.0, stop_loss=4408.0,
        take_profit_1=4419.0, take_profit_2=4422.0,
        status="executed", received_at=now,
    ))
    state.db.insert_trade(Trade(
        id="rvb-tr-1", signal_id="rvb-sig-1", symbol="XAUUSD", direction="buy",
        entry=4416.3, stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
        risk_pct=2.3, lots_1=0.01, lots_2=0.01, status="closed",
        opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T11:00:00+00:00", pnl=25.0, exit_reason="tp2",
    ))
    state.db.insert_position(Position(
        id="rvb-pos-1", trade_id="rvb-tr-1", leg=1, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.3, stop_loss=4408.0,
        take_profit=4419.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T10:30:00+00:00", close_price=4419.2, pnl=2.9,
        exit_reason="tp1",
    ))
    state.db.insert_position(Position(
        id="rvb-pos-2", trade_id="rvb-tr-1", leg=2, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.3, stop_loss=4408.0,
        take_profit=4422.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T11:00:00+00:00", close_price=4422.0, pnl=5.7,
        exit_reason="tp2",
    ))

    async def fake_run_analysis(*a, **k):
        return {"ok": True, "results": [
            {"id": "rvb-sig-1", "pnl": 24.0, "outcome": "win"},
        ]}
    monkeypatch.setattr("app.analysis.run_analysis", fake_run_analysis)
    try:
        r = client.get("/api/analysis/real_vs_backtest")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True and data["count"] >= 1
        row = next(t for t in data["trades"] if t["trade_id"] == "rvb-tr-1")
        assert row["symbol"] == "XAUUSD"
        # entrada real 4416.30 vs fill do sim 4416.00 + spread(1 pip)/2 + slippage(0.5)
        # pip XAUUSD = 0.01 → 4416.00 + 0.005 + 0.005 = 4416.01
        assert row["bt_entry_fill"] is not None
        assert row["entry_diff"] is not None and row["entry_diff"] > 0
        assert row["entry_diff_pips"] is not None and row["entry_diff_pips"] > 0
        # saída: fecho ponderado (4419.2×0.01 + 4422×0.01)/0.02 = 4420.6;
        # diferença vs o nível esperado da razão (tp1 4419 → 0.2, tp2 4422 → 0)
        assert row["real_close"] == 4420.6
        assert abs(row["exit_diff_avg"] - 0.1) < 1e-9
        assert row["delay_seconds"] == 30
        assert row["bt_pnl"] == 24.0
    finally:
        with state.db.tx() as cur:
            cur.execute("DELETE FROM positions WHERE trade_id='rvb-tr-1'")
            cur.execute("DELETE FROM trades WHERE id='rvb-tr-1'")
            cur.execute("DELETE FROM signals WHERE id='rvb-sig-1'")


def test_paper_vs_real_auto_endpoint(client):
    """`/api/analysis/paper_vs_real_auto`: compara a trade REAL da auto (MT5,
    Fase 3) com a trade de PAPEL da mesma estratégia no mesmo sinal — mesma
    decisão de saída, Δ preço (pips), Δ timing (min) e P&L (SPEC §B.5.4)."""
    from app.main import state
    from app.models import Position, Signal, Trade

    state.db.insert_signal(Signal(
        id="pvr-sig-1", source="tg:Test", raw_text="", symbol="XAUUSD",
        direction="buy", entry=4416.0, stop_loss=4408.0,
        take_profit_1=4419.0, take_profit_2=4422.0,
        status="executed", received_at="2026-08-18T10:00:00+00:00",
    ))
    state.db.insert_trade(Trade(
        id="pvr-tr-1", signal_id="pvr-sig-1", symbol="XAUUSD", direction="buy",
        entry=4416.3, stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
        risk_pct=2.3, lots_1=0.01, lots_2=0.01, status="closed",
        opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T11:00:00+00:00", pnl=30.0, exit_reason="time_stop",
    ))
    state.db.insert_position(Position(
        id="pvr-pos-1", trade_id="pvr-tr-1", leg=1, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.3, stop_loss=4408.0,
        take_profit=4419.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T10:30:00+00:00", close_price=4419.2, pnl=2.9,
        exit_reason="tp1",
    ))
    state.db.insert_position(Position(
        id="pvr-pos-2", trade_id="pvr-tr-1", leg=2, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.3, stop_loss=4408.0,
        take_profit=4422.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T11:00:00+00:00", close_price=4422.0, pnl=5.7,
        exit_reason="tp2",
    ))
    state.db.insert_paper_trade(Trade(
        id="pvr-pt-1", strategy_id="auto_escada_3", signal_id="pvr-sig-1",
        symbol="XAUUSD", direction="buy", entry=4416.0, stop_loss=4408.0,
        take_profit_1=4419.0, take_profit_2=4422.0, risk_pct=2.3,
        lots_1=0.01, lots_2=0.01, status="closed",
        opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T10:55:00+00:00", pnl=25.0, exit_reason="tp2",
    ))
    state.db.insert_paper_position(Position(
        id="pvr-pp-1", trade_id="pvr-pt-1", leg=1, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.0, stop_loss=4408.0,
        take_profit=4419.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T10:30:00+00:00", close_price=4419.0, pnl=3.0,
        exit_reason="tp1",
    ))
    state.db.insert_paper_position(Position(
        id="pvr-pp-2", trade_id="pvr-pt-1", leg=2, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4416.0, stop_loss=4408.0,
        take_profit=4422.0, status="closed", opened_at="2026-08-18T10:00:30+00:00",
        closed_at="2026-08-18T10:55:00+00:00", close_price=4422.0, pnl=6.0,
        exit_reason="tp2",
    ))
    try:
        r = client.get("/api/analysis/paper_vs_real_auto")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True and data["count"] >= 1
        row = next(p for p in data["pairs"] if p["signal_id"] == "pvr-sig-1")
        assert row["strategy_id"] == "auto_escada_3"
        assert row["real_exit"] == "time_stop" and row["paper_exit"] == "tp2"
        assert row["same_exit"] is False and row["paper_open"] is False
        # fecho ponderado real (4419.2/4422) vs papel (4419/4422)
        assert row["real_close"] == 4420.6 and row["paper_close"] == 4420.5
        assert row["price_diff_pips"] == 10.0  # (4420.6 − 4420.5)/0.01
        assert row["timing_diff_min"] == -5.0  # papel fechou 5 min antes do real
        assert row["real_pnl"] == 30.0 and row["paper_pnl"] == 25.0
        # filtro por estratégia
        r2 = client.get("/api/analysis/paper_vs_real_auto?strategy=auto_escada_3")
        assert r2.json()["count"] >= 1
        # auto_escada_5 não tem trade de papel para o sinal do teste (não
        # pode aparecer um par fake) — outros pares podem existir de outros
        # testes (19/08: todas as auto_* visíveis abrem papel)
        r3 = client.get("/api/analysis/paper_vs_real_auto?strategy=auto_escada_5")
        assert all(p["signal_id"] != "pvr-sig-1" for p in r3.json()["pairs"])
    finally:
        with state.db.tx() as cur:
            cur.execute("DELETE FROM paper_positions WHERE trade_id='pvr-pt-1'")
            cur.execute("DELETE FROM paper_trades WHERE id='pvr-pt-1'")
            cur.execute("DELETE FROM positions WHERE trade_id='pvr-tr-1'")
            cur.execute("DELETE FROM trades WHERE id='pvr-tr-1'")
            cur.execute("DELETE FROM signals WHERE id='pvr-sig-1'")


def test_fase3_promotion_snapshot(client):
    """Ao promover uma auto com auto-gestão (Fase 3), grava na config um
    snapshot antes/depois da janela de validação: stats da corrida paper, DD,
    divergences paper↔MT5 e auditoria de SL — exposto no /api/status."""
    import json
    from app.main import state
    orig = state.config.get("strategy")
    try:
        r = client.post("/api/config", json={"strategy": "auto_escada_3"})
        assert r.status_code == 200
        raw = state.db.all_config().get("fase3_snapshot") or "{}"
        snap = json.loads(raw)
        assert snap.get("strategy_id") == "auto_escada_3"
        assert "promoted_at" in snap and "n" in snap and "pf" in snap
        assert "dd_auto" in snap and "paper_vs_real_pairs" in snap
        assert "sl_audit_entries" in snap and "real_trades_closed" in snap
        # exposto no snapshot da dashboard
        st = client.get("/api/status").json()
        assert st.get("fase3_snapshot", {}).get("strategy_id") == "auto_escada_3"
        # promover QUALQUER estratégia (feed/páginas de teste) também grava o
        # snapshot da corrida paper — o antes/depois fica para qualquer promoção
        client.post("/api/config", json={"strategy": "nova"})
        snap2 = json.loads(state.db.all_config().get("fase3_snapshot") or "{}")
        assert snap2.get("strategy_id") == "nova"
        assert snap2 != json.loads(raw)
        # a reabertura da janela de validação (limpar o veredicto atual) só
        # acontece quando uma AUTO é promovida — uma promoção de não-auto
        # deixa o veredicto intacto
        before = state.db.all_config().get("fase3_verdict") or ""
        client.post("/api/config", json={"strategy": "nova"})
        assert (state.db.all_config().get("fase3_verdict") or "") == before
        # ...e a promoção da auto volta a limpar (reabre a janela)
        client.post("/api/config", json={"strategy": "auto_escada_3"})
        assert (state.db.all_config().get("fase3_verdict") or "") == ""
    finally:
        client.post("/api/config", json={"strategy": orig})
        with state.db.tx() as cur:
            cur.execute("DELETE FROM config WHERE key='fase3_snapshot'")


def test_paper_atr_minute_buckets_no_lookahead(monkeypatch):
    """`_paper_atr` (corrida paper, espelho do backtest/MT5 §B.5.3): o ATR
    usa os MINUTOS COMPLETOS anteriores ao atual — o tick atual nunca conta
    para o ATR que ele próprio usa (sem look-ahead); sem dados → None."""
    import os
    import tempfile
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace

    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        race = PaperRace(db, MarketSimulator())
        clock = {"now": 10 * 60 + 30}  # 10:00:30 — minuto atual 10
        monkeypatch.setattr("app.execution.paper.time.time", lambda: clock["now"])
        state = {"atr_buckets": {
            8: [99.0, 89.0, 94.0],    # minuto completo
            9: [100.0, 90.0, 95.0],   # minuto completo
        }}
        # tick 96 no minuto 10 (em progresso) — não entra no ATR
        atr = race._paper_atr(state, 96.0, 14)
        # TR(9) = max(10, |100−94|=6, |90−94|=4) = 10 → ATR 10.0
        assert atr is not None and abs(atr - 10.0) < 1e-9
        # o minuto atual fica registado mas fora da janela
        assert state["atr_buckets"][10] == [96.0, 96.0, 96.0]
        # um tick violento no minuto atual NÃO altera o ATR (sem look-ahead)
        atr2 = race._paper_atr(state, 200.0, 14)
        assert abs(atr2 - 10.0) < 1e-9
        # sem minutos completos suficientes → None (trail fixo)
        assert race._paper_atr({"atr_buckets": {10: [1.0, 1.0, 1.0]}}, 1.0, 14) is None
    finally:
        db.close()
        os.remove(tmp)


def test_auto_sl_log_endpoint(client):
    """`/api/analysis/auto_sl_log`: auditoria dos três níveis de SL da auto
    (target→valid→applied + retcode 10009/10016), persistida na config por
    trade — a janela de validação da Fase 3 (SPEC §B.5.4)."""
    import json
    from app.main import state
    state.db.set_config("autosl:t-sl1", json.dumps([
        {"ts": "2026-08-18T10:00:00+00:00", "leg": 2, "kind": "stair",
         "target": 4389.0, "valid": 4389.0, "clamped": False,
         "retcode": 10009, "applied": True},
        {"ts": "2026-08-18T10:01:00+00:00", "leg": 3, "kind": "trail",
         "target": 4390.0, "valid": 4390.0, "clamped": False,
         "retcode": 10016, "applied": False},
    ]))
    try:
        r = client.get("/api/analysis/auto_sl_log")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        row = next(t for t in data["trades"] if t["trade_id"] == "t-sl1")
        assert len(row["entries"]) == 2
        assert row["entries"][0]["applied"] is True
        assert row["entries"][1]["retcode"] == 10016
        assert row["entries"][1]["applied"] is False
    finally:
        with state.db.tx() as cur:
            cur.execute("DELETE FROM config WHERE key='autosl:t-sl1'")


def test_paper_tick_auto_trail_no_lookahead(client):
    """O tick do PaperRace aplica a auto-gestão (espelho do backtest): BE
    após TP1, o trailing segue o pico e o SL levanta-se para o nível de ticks
    ANTERIORES (sem look-ahead) — o recuo fecha no SL de trail com lucro."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal
    from app.strategies import get_strategy

    db = DB(DB_PATH)
    mkt = MarketSimulator()
    mkt.ensure("XAUUSD", 4416.0)
    race = PaperRace(db, mkt)
    sig = Signal(id="s-auto-tick", source="tg:Test", raw_text="",
                 symbol="XAUUSD", direction="buy", entry=4416.0,
                 stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
                 status="historical", received_at="2026-08-18T10:00:00")
    opened = race.open_for_signal(sig, 5.0, strategy_ids=["auto_3"])
    # o tick trabalha com o dict da BD (outras trades de papel de testes
    # anteriores podem existir — usamos a nossa pela id)
    trade = next(t for t in db.paper_open_trades("auto_3") if t["id"] == opened[0].id)
    # auto_3 é a base sem escada — ligamos o trailing para testar o mecanismo
    strat = dict(get_strategy("auto_3"))
    strat["trail_factor"] = 0.5

    def _tick(px):
        mkt.prices["XAUUSD"] = px
        race._update_trade(trade, strat, 5.0)
        return db.paper_positions_for_trade(trade["id"])

    # TP1 → perna 1 fecha; restantes vão a BE (sem escada)
    pos = _tick(4419.0)
    leg2 = next(p for p in pos if p.leg == 2)
    assert leg2.stop_loss == 4416.0, "BE após TP1"
    # pico novo 4421.9 (sem TP2); o tick seguinte levanta o SL para o nível do
    # pico ANTERIOR — 4421.9 − 0.5×6 = 4418.9
    _tick(4421.9)
    _tick(4421.9)
    leg2 = next(p for p in _tick(4421.9) if p.leg == 2)
    assert abs(leg2.stop_loss - 4418.9) < 1e-9, \
        "trail: SL = pico anterior − trail_factor × dist(entrada→TP2)"
    # recuo ao SL de trail → fecha com lucro (acima da entrada)
    _tick(4418.9)
    closed = [p for p in db.paper_positions_for_trade(trade["id"])
              if p.status == "closed"]
    assert any(p.leg == 2 and p.exit_reason == "trail" and p.pnl > 0
               for p in closed), "perna 2 fechou no SL de trail com lucro"
    # limpeza (BD de testes partilhada entre testes)
    with db.tx() as cur:
        cur.execute("DELETE FROM paper_positions WHERE trade_id=?", (trade["id"],))
        cur.execute("DELETE FROM paper_trades WHERE id=?", (trade["id"],))
        cur.execute("DELETE FROM paper_accounts WHERE strategy_id='auto_3'")


def test_paper_tick_auto_stair_floor(client):
    """Escada no tick do paper: com stair_from=1 o SL das pernas restantes
    sobe para o NÍVEL do TP1 (não BE) — como no backtest."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal
    from app.strategies import get_strategy

    db = DB(DB_PATH)
    mkt = MarketSimulator()
    mkt.ensure("XAUUSD", 4416.0)
    race = PaperRace(db, mkt)
    sig = Signal(id="s-auto-stair", source="tg:Test", raw_text="",
                 symbol="XAUUSD", direction="buy", entry=4416.0,
                 stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
                 status="historical", received_at="2026-08-18T10:00:00")
    opened = race.open_for_signal(sig, 5.0, strategy_ids=["auto_escada_3"])
    trade = next(t for t in db.paper_open_trades("auto_escada_3")
                 if t["id"] == opened[0].id)
    mkt.prices["XAUUSD"] = 4419.0
    race._update_trade(trade, get_strategy("auto_escada_3"), 5.0)
    pos = db.paper_positions_for_trade(trade["id"])
    leg2 = next(p for p in pos if p.leg == 2)
    assert leg2.stop_loss == 4419.0, "escada: SL da perna 2 no nível do TP1"
    with db.tx() as cur:
        cur.execute("DELETE FROM paper_positions WHERE trade_id=?", (trade["id"],))
        cur.execute("DELETE FROM paper_trades WHERE id=?", (trade["id"],))
        cur.execute("DELETE FROM paper_accounts WHERE strategy_id='auto_escada_3'")


def test_paper_defensive_mode_tightens_trail_after_pf_drop(client):
    """Modo defensivo no PaperRace (espelho do backtest, estudo 19/08): com
    o PF das últimas 10 trades de papel resolvidas < 1.0, a estratégia passa
    a `def_trail` — o SL de trail usa a distância apertada em vez da normal."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal
    from app.strategies import get_strategy

    db = DB(DB_PATH)
    mkt = MarketSimulator()
    mkt.ensure("XAUUSD", 4416.0)
    race = PaperRace(db, mkt)
    sig = Signal(id="s-auto-def", source="tg:Test", raw_text="",
                 symbol="XAUUSD", direction="buy", entry=4416.0,
                 stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4600.0,
                 status="historical", received_at="2026-08-18T10:00:00")
    opened = race.open_for_signal(sig, 5.0, strategy_ids=["auto_escada_3"])
    trade = next(t for t in db.paper_open_trades("auto_escada_3")
                 if t["id"] == opened[0].id)
    strat = dict(get_strategy("auto_escada_3"))  # trail 2.0 · def_trail 1.5

    # 10 perdas resolvidas na conta da estratégia → PF < 1.0 → defensivo
    with db.tx() as cur:
        cur.execute("DELETE FROM paper_trades WHERE strategy_id='auto_escada_3'")
        now = "2026-08-18T00:00:00"
        for i in range(10):
            cur.execute(
                "INSERT INTO paper_trades (id, strategy_id, signal_id, symbol,"
                " direction, entry, stop_loss, take_profit_1, take_profit_2,"
                " lots_1, status, opened_at, closed_at, pnl, exit_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"pt-def-{i}", "auto_escada_3", f"s-def-{i}", "XAUUSD",
                 "buy", 4400.0, 4390.0, 4405.0, 4408.0, 0.1, "closed",
                 now, now, -20.0, "sl"))
    assert race._defensive_triggered("auto_escada_3", 10) is True

    def _tick(px):
        mkt.prices["XAUUSD"] = px
        race._update_trade(trade, strat, 5.0)
        return db.paper_positions_for_trade(trade["id"])

    # o 1º tick já vai ao pico (4490): TP1 + escada (SL 4419) no MESMO tick;
    # o seguinte levanta o SL de trail com o def_trail 1.5: dist =
    # 1.5 × (4600−4416) = 276 → SL = 4490 − 276 = 4214 (abaixo do degrau — o
    # degrau manda). Para o trail mandar, o pico tem de ultrapassar
    # degrau + trail_dist. Com pico 4700: defensivo 4700 − 276 = 4424; com
    # trail 2.0 (dist 368) SL = 4332 — ambos acima do degrau 4419? Não para
    # o 2.0 (4332 < 4419). Para separar os dois, o pico tem de ultrapassar o
    # degrau + a distância MAIOR (2.0): pico 4800 → defensivo 4800−276 =
    # 4524; normal 4800−368 = 4432.
    _tick(4800.0)
    _tick(4800.0)
    leg2 = next(p for p in _tick(4800.0) if p.leg == 2)
    assert abs(leg2.stop_loss - 4524.0) < 1e-9, \
        f"defensivo: SL de trail com def_trail 1.5 (4524), não 4432: {leg2.stop_loss}"

    # sem perdas recentes (PF alto) → trail normal 2.0 → SL 4432 (2ª trade,
    # com o histórico agora todo positivo)
    with db.tx() as cur:
        cur.execute("UPDATE paper_trades SET pnl=20.0 WHERE strategy_id='auto_escada_3'")
    sig2 = Signal(id="s-auto-def2", source="tg:Test", raw_text="",
                  symbol="XAUUSD", direction="buy", entry=4416.0,
                  stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4600.0,
                  status="historical", received_at="2026-08-18T10:05:00")
    opened2 = race.open_for_signal(sig2, 5.0, strategy_ids=["auto_escada_3"])
    trade2 = next(t for t in db.paper_open_trades("auto_escada_3")
                  if t["id"] == opened2[0].id)
    mkt.prices["XAUUSD"] = 4800.0
    race._update_trade(trade2, strat, 5.0)
    race._update_trade(trade2, strat, 5.0)
    leg2b = next(p for p in db.paper_positions_for_trade(trade2["id"]) if p.leg == 2)
    assert abs(leg2b.stop_loss - 4432.0) < 1e-9, \
        f"PF alto → trail 2.0 (4432): {leg2b.stop_loss}"

    with db.tx() as cur:
        cur.execute("DELETE FROM paper_positions WHERE trade_id=?", (trade["id"],))
        cur.execute("DELETE FROM paper_trades WHERE strategy_id='auto_escada_3'")
        cur.execute("DELETE FROM paper_accounts WHERE strategy_id='auto_escada_3'")


def test_paper_divergence_events(client):
    """Divergence report: quando a referência (nova_gestao) fecha por gestão
    do grupo e a auto ainda está aberta, emite um evento `divergence` — a
    ligação backtest↔vivo do SPEC v2 §3.4."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal
    from app.strategies import get_strategy

    db = DB(DB_PATH)
    mkt = MarketSimulator()
    mkt.ensure("XAUUSD", 4416.0)
    race = PaperRace(db, mkt)
    sig = Signal(id="s-auto-div", source="tg:Test", raw_text="",
                 symbol="XAUUSD", direction="buy", entry=4416.0,
                 stop_loss=4408.0, take_profit_1=4419.0, take_profit_2=4422.0,
                 status="historical", received_at="2026-08-18T10:00:00")
    race.open_for_signal(sig, 5.0, strategy_ids=["auto_3", "nova_gestao"])
    # TP1 em ambas (BE para a auto, BE para a referência)
    mkt.prices["XAUUSD"] = 4419.0
    strat_auto = dict(get_strategy("auto_3"))
    strat_auto["trail_factor"] = 0.5
    for t in db.paper_open_trades("auto_3"):
        race._update_trade(t, strat_auto, 5.0)
    for t in db.paper_open_trades("nova_gestao"):
        race._update_trade(t, get_strategy("nova_gestao"), 5.0)
    # o grupo sai ("we're out") → a referência fecha; a auto continua aberta
    events = race.apply_management(sig.id, "close_all")
    divs = [e for e in events if e["type"] == "divergence"]
    assert any(e["type"] == "trade_closed" for e in events), "referência fechou"
    assert divs, "devia emitir divergência (ref fechou, auto aberta)"
    assert divs[0]["reason"] == "open" and divs[0]["ref_reason"] == "close_all"
    assert divs[0]["strategy_id"].startswith("auto_")
    # a auto continua aberta (não honra close_all)
    assert db.paper_open_trades("auto_3"), "auto continua aberta"
    # limpeza (buscar ids ANTES do tx — o lock não é reentrante)
    auto_ids = [t["id"] for t in db.paper_open_trades("auto_3")]
    with db.tx() as cur:
        for tid in auto_ids:
            cur.execute("DELETE FROM paper_positions WHERE trade_id=?", (tid,))
            cur.execute("DELETE FROM paper_trades WHERE id=?", (tid,))
        cur.execute("DELETE FROM paper_accounts WHERE strategy_id='auto_3'")


def test_paper_divergences_endpoint(client):
    """GET /api/paper/divergences devolve os pares auto vs referência que
    divergiram (razão/P&L diferentes, ou a referência saiu e a auto continua)
    — a tabela do Relatório (SPEC v2 §3.4)."""
    r = client.get("/api/paper/divergences").json()
    assert r["ok"] is True
    assert isinstance(r.get("divergences"), list)
    # as divergências têm os campos lado a lado
    for d in r["divergences"]:
        assert d["signal_id"] and d["symbol"]
        assert d["strategy_id"].startswith("auto_")
        assert "ref_reason" in d and "ref_pnl" in d
        assert d["motivo"]


def test_paper_history_endpoint_returns_series(client):
    """GET /api/paper/history devolve a evolução de saldo por estratégia
    (corrida paralela) — com dados gravados, a série aparece ordenada."""
    from datetime import datetime, timedelta
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    main_id = client.get("/api/status").json()["config"]["strategy"]
    # limpa snapshots que o tick do TestClient possa ter gravado (timestamps
    # reais dentro da janela) para a asserção ser determinística
    with db.tx() as cur:
        cur.execute(
            "DELETE FROM paper_balance_history WHERE strategy_id IN ('nova', 'auto_escada_3') OR strategy_id = ?",
            (main_id,))
    # timestamps relativos (nunca hardcoded) — o endpoint filtra pelas últimas
    # 24h e datas fixas deixavam a série vazia com o passar do tempo
    now = datetime.now()
    t0 = (now - timedelta(minutes=3)).isoformat(timespec="seconds")
    t1 = (now - timedelta(minutes=2)).isoformat(timespec="seconds")
    t2 = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.insert_paper_balance_history("nova", t0, 1000.0)
    db.insert_paper_balance_history("nova", t1, 1005.0)
    db.insert_paper_balance_history("nova", t2, 1012.0)
    db.insert_paper_balance_history("auto_escada_3", t0, 1000.0)
    db.insert_paper_balance_history("auto_escada_3", t1, 1005.0)
    db.insert_paper_balance_history("auto_escada_3", t2, 1012.0)
    db.insert_paper_balance_history(main_id, t0, 11304.0)

    r = client.get("/api/paper/history?hours=24")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # a nova está oculta do menu (MENU_HIDDEN_STRATEGIES) — continua a abrir
    # papel mas fica FORA da curva da corrida (matriz do ESTRATEGIAS.md)
    assert all(s["strategy_id"] != "nova" for s in data["series"])
    auto = next(s for s in data["series"] if s["strategy_id"] == "auto_escada_3")
    assert auto["nome"]
    # ordenada por tempo e com o saldo certo
    assert [p["balance"] for p in auto["points"]] == [1000.0, 1005.0, 1012.0]
    assert auto["points"][0]["ts"] < auto["points"][-1]["ts"]
    # a principal vem marcada is_main (com a sua conta virtual própria)
    main = next((s for s in data["series"] if s["is_main"]), None)
    assert main is not None and main["strategy_id"] == main_id
    assert main["points"][0]["balance"] == 11304.0


def test_paper_trades_endpoint_returns_history(client):
    """GET /api/paper/trades devolve o histórico de trades de papel da
    estratégia (fechadas com P&L + abertas) para a página de teste."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    r = client.get("/api/paper/trades?strategy=nova")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["strategy"] == "nova"
    # account.is_main diz à dashboard se a estratégia é a principal
    # (que também tem a sua conta virtual própria na corrida paralela)
    assert data["account"]["strategy_id"] == "nova"
    assert data["account"]["is_main"] is False
    main = client.get("/api/status").json()["config"]["strategy"]
    r2 = client.get("/api/paper/trades?strategy=" + main)
    assert r2.json()["account"]["is_main"] is True
    trades = data["trades"]
    assert trades, "a trade de papel da estratégia 'nova' tem de existir"
    assert any(t["strategy_id"] == "nova" and t["status"] == "open" for t in trades)
    # sem estratégia -> lista vazia, sem erro
    assert client.get("/api/paper/trades").json()["trades"] == []


def test_strategy_change_notifies_with_params(client):
    """Promover uma estratégia regista uma notificação no feed com os
    parâmetros que passam a ser usados na execução real (max_legs/exit_at_tp/lote)."""
    client.post("/api/config", json={"strategy": "nova_gestao"})  # baseline
    r = client.post("/api/config", json={"strategy": "tp2"})
    assert r.status_code == 200 and not r.json().get("error")
    st = client.get("/api/status").json()
    promo = [n for n in st["notifications"] if "Estratégia promovida" in n["text"]]
    assert promo, "a promoção tem de aparecer no feed de notificações"
    assert "TP1→BE + sair no TP2" in promo[0]["text"]
    assert "fecha tudo no TP2" in promo[0]["text"]
    # o snapshot expõe os parâmetros ativos da execução real
    p = st["config"]["strategy_params"]
    assert p["exit_at_tp"] == 2
    assert p["max_legs"] is None
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_snapshot_exposes_strategy_params(client):
    """O snapshot mostra os parâmetros da estratégia ativa da principal
    (exit_at_tp/manage) para o badge da dashboard."""
    client.post("/api/config", json={"strategy": "tp2"})
    st = client.get("/api/status").json()
    p = st["config"]["strategy_params"]
    assert p["exit_at_tp"] == 2
    assert p["max_legs"] is None
    assert p["manage"] is False
    assert p["nome"] == "TP1→BE + sair no TP2"
    assert p["orphan"] is False
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_orphan_strategy_config_warns(client):
    """Config com id de estratégia que já não existe no registo (ex.:
    removida) fica marcada como órfã — o badge avisa e a execução usa o
    fallback (principal), nunca a estratégia errada."""
    from app.main import state
    state.config["strategy"] = "duas_pernas"  # removida do registo
    st = client.get("/api/status").json()
    assert st["config"]["strategy"] == "duas_pernas"
    p = st["config"]["strategy_params"]
    assert p["orphan"] is True
    assert p["configured_id"] == "duas_pernas"
    assert p["nome"] == "TP1..TP5 + gestão"  # fallback para a principal
    state.config["strategy"] = "nova_gestao"  # restaura


def test_month_sim_full_history_days_zero(client):
    """days=0 = todo o histórico na BD (desde o início do grupo): o replay
    das estratégias de teste não fica limitado ao último mês."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "8000", "group": "111", "historical": True})
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=0&strategy=nova_gestao")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["days"] == 0
    assert data["signals"] >= 1
    assert "equity_curve" in data and "start_balance" in data


def test_compare_full_history_days_zero(client):
    """O A/B/C também corre sobre o histórico completo (days=0) — a tab
    Edge mostra a comparação ao lado da do mês."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "8100", "group": "111", "historical": True})
    r = client.get("/api/analysis/compare?group=Golden%20Moves&days=0")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["days"] == 0
    assert "new" in data and "manage" in data and "tp2" in data
    assert data["compare"]["winner"] in ("new", "manage", "tp2", "tie")


def test_month_sim_since_filters_period(client):
    """`since=YYYY-MM-DD` no /api/analysis/month filtra os sinais a partir
    dessa data (formato atual vs histórico completo) e devolve o P&L por
    mês para o gráfico de barras."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "8200", "group": "111", "historical": True})
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=0&strategy=nova_gestao&since=2030-01-01")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["signals"] == 0
    # sem `since` (histórico completo) o sim devolve pnl_by_month + flags de falência
    r2 = client.get("/api/analysis/month?group=Golden%20Moves&days=0&strategy=nova_gestao")
    d2 = r2.json()
    assert d2["signals"] >= 1
    assert "pnl_by_month" in d2 and isinstance(d2["pnl_by_month"], list)
    assert "bankrupt" in d2 and "bankrupt_date" in d2


def test_compare_since_filters_period(client):
    """`since` no compare filtra o período (ex.: só o formato atual)."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "8300", "group": "111", "historical": True})
    r = client.get("/api/analysis/compare?group=Golden%20Moves&days=0&since=2030-01-01")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["signals"] == 0
    assert "pnl_by_month" in (data.get("manage") or {})


def test_config_persists_teste_days(client):
    """O seletor de dias das páginas de teste fica na config (7/30/90/0)
    para sobreviver a recarregamentos da dashboard."""
    r = client.post("/api/config", json={"teste_days": 0})
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["config"]["teste_days"] == 0
    r = client.post("/api/config", json={"teste_days": 90})
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["config"]["teste_days"] == 90
    # valor inválido rejeitado pelo modelo
    r = client.post("/api/config", json={"teste_days": -1})
    assert r.status_code == 422
    client.post("/api/config", json={"teste_days": 30})


def test_config_persists_paper_race_hours(client):
    """O horizonte da corrida paralela (24h/7d/30d) fica na config para
    sobreviver a recarregamentos da dashboard."""
    r = client.post("/api/config", json={"paper_race_hours": 168})
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["config"]["paper_race_hours"] == 168
    r = client.post("/api/config", json={"paper_race_hours": 720})
    assert r.status_code == 200
    assert client.get("/api/status").json()["config"]["paper_race_hours"] == 720
    # valor inválido (fora de 24..720) rejeitado pelo modelo
    r = client.post("/api/config", json={"paper_race_hours": 1})
    assert r.status_code == 422
    r = client.post("/api/config", json={"paper_race_hours": 1000})
    assert r.status_code == 422
    client.post("/api/config", json={"paper_race_hours": 24})


def test_history_done_notifies_feed(client):
    """Quando o replay do histórico do listener termina, o servidor regista
    uma notificação no feed com os totais reencaminhados e guarda o resumo
    do último replay para a tab Diagnóstico."""
    r = client.post("/api/listener/history_done", json={
        "group": "Golden Moves", "total": 1234,
        "sinais": 45, "gestao": 23, "resumos": 2, "ignoradas": 1164,
        "outras": 0})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["notified"] is True
    st = client.get("/api/status").json()
    hit = [n for n in st["notifications"] if "Replay do histórico concluído" in n["text"]]
    assert hit, "a notificação de fim de replay tem de aparecer no feed"
    assert "1234 mensagens" in hit[0]["text"]
    assert "45 sinais" in hit[0]["text"] and "23 gestão" in hit[0]["text"]
    # /api/diagnostics expõe a data + totais do último replay
    d = client.get("/api/diagnostics").json()
    rp = d.get("replay") or {}
    assert rp["done_at"], "a data do último replay tem de ficar registada"
    assert rp["total"] == 1234 and rp["sinais"] == 45 and rp["gestao"] == 23
    assert rp["ignoradas"] == 1164
    # sem mensagens -> sem notificação nem registo novo
    r = client.post("/api/listener/history_done", json={"group": "Golden Moves", "total": 0})
    assert r.json()["notified"] is False


def test_operations_endpoint_exposes_item10_metrics(client):
    r = client.get("/api/operations?days=90")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "exceptions" in data and "spread" in data and "mt5" in data
    assert "total" in data["exceptions"]
    assert "monthly" in data["spread"]
    assert "connections" in data["mt5"] and "modifies" in data["mt5"]


def test_report_endpoint(client):
    r = client.get("/api/report?days=7")
    assert r.status_code == 200
    data = r.json()
    assert "signals" in data and "trades" in data and "groups" in data
    assert "win_rate" in data["trades"]
    assert "avg_tp1" in data["risk_reward"]
    assert "equity_curve" in data
    assert "drawdown" in data
    assert "amount" in data["drawdown"] and "pct" in data["drawdown"]


def test_simulate_replays_real_signals(client, monkeypatch):
    from app.execution.feed import YahooProvider

    async def fake_history(self, symbol, interval="15m", range_="1mo"):
        from datetime import datetime
        base = int(datetime.now().timestamp())
        return [{"ts": base + 60 + i * 900, "open": 1.0850,
                 "high": 1.0890, "low": 1.0830, "close": 1.0860}
                for i in range(5)]

    monkeypatch.setattr(YahooProvider, "history", fake_history)
    r = client.post("/api/simulate", json={"risk_pct": 1.0, "days": 7, "delay_seconds": 60})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["simulated"] >= 1
    assert data["aggregate"]["total"] >= 1


def test_telegram_edit_window_invalid_at_finalize(client):
    """A buffered message that stays invalid after the window is marked failed."""
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 30})
    r = client.post("/api/signals", json={
        "raw_text": "I'm buying XAUUSD TP1 4411 TP2 4413 SL 4392",
        "source": "telegram", "message_id": "888"})
    assert r.json()["action"] == "buffered"

    client.post("/api/config", json={"edit_window_seconds": 0})
    client.post("/api/signals", json={
        "raw_text": "I'm buying XAUUSD TP1 4411 TP2 4413 SL 4392",
        "source": "telegram", "message_id": "888"})
    st = _wait_for(client, lambda s: _sig_status(s, "tg-888") == "failed")

    sig = next(s for s in st["signals"] if s["id"] == "tg-888")
    assert sig["status"] == "failed"
    client.post("/api/config", json={"edit_window_seconds": 60})


def _close_all_open_trades(client):
    """Close any trades left open by earlier tests (they share one DB)."""
    st = client.get("/api/status").json()
    for s in st["signals"]:
        if s["status"] == "executed":
            client.post(f"/api/signals/{s['id']}/delete")


def _sig_status(st, sid):
    """Estado de um sinal no snapshot (None se não existir)."""
    return next((s for s in st["signals"] if s["id"] == sid), {}).get("status")


def _wait_for(client, pred, timeout=8.0, step=0.1):
    """Espera por polling até `pred(snapshot)` ser verdadeiro.

    Os testes da janela de edição usavam time.sleep(1.3) a competir com o
    loop de fundo de 1s — sob carga o tick atrasava e o teste falhava de
    forma intermitente. Polling elimina a corrida (determinístico)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/status").json()
        if pred(st):
            return st
        time.sleep(step)
    raise AssertionError(f"_wait_for: condição não satisfeita em {timeout:.0f}s")


def test_management_message_acts_on_open_trade(client):
    """A group "close first entries" message closes leg 1 and protects the
    rest — without waiting for the edit window (acts immediately)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    # open a real trade so there is something to manage
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["status"] == "open")

    # management message (Telegram, no edit window wait)
    r = client.post("/api/signals", json={
        "raw_text": "+30 pips from best entires  Close first entires now and move stop loss above best entires",
        "source": "tg:Golden Moves", "message_id": "7001", "group": "g"})
    data = r.json()
    assert data["action"] == "management"
    assert data["kind"] == "protect"
    assert data["applied"] is True

    # invariantes determinísticos lidos da BD (o snapshot só mostra pernas
    # ABERTAS e o tick do mercado congelado fecha logo as restantes quando o
    # SL fica acima da entrada — contar pernas abertas era a fonte da flaky):
    # perna 1 fechada pelo protect + SL das restantes movido.
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    positions = db.positions_for_trade(trade_id)
    leg1 = next((p for p in positions if p.leg == 1), None)
    assert leg1 is not None and leg1.status == "closed"
    assert leg1.exit_reason == "protect"
    remaining = [p for p in positions if p.leg != 1]
    assert remaining
    assert all(p.stop_loss != 1.0820 for p in remaining)  # SL moved from original


def test_management_move_sl_updates_open_trade(client):
    """"Adjust SL to X"" move o stop loss das pernas abertas para o nível
    indicado pelo grupo (trailing/risco)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    r = client.post("/api/signals", json={
        "raw_text": "Adjust SL to 1.0790", "source": "tg:Golden Moves",
        "message_id": "7002", "group": "g"})
    data = r.json()
    assert data["action"] == "management"
    assert data["kind"] == "move_sl"
    assert data["applied"] is True

    st = client.get("/api/status").json()
    open_legs = [p for p in st["positions"] if p["status"] == "open"]
    assert open_legs, "tem de haver pernas abertas"
    assert all(abs(p["stop_loss"] - 1.0790) < 1e-9 for p in open_legs)


def test_management_tp_sl_update_reply_corrects_signal(client):
    """"TP1 1.0860 SL 1.0790"" a responder ao sinal corrige o TP1/SL do
    sinal alvo e move o SL da trade aberta (edição pós-execução)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={
        "raw_text": EURUSD, "message_id": "7100", "group": "g"})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    r = client.post("/api/signals", json={
        "raw_text": "TP1 1.0860 SL 1.0790", "source": "tg:Golden Moves",
        "message_id": "7101", "group": "g", "reply_to_msg_id": "7100"})
    data = r.json()
    assert data["action"] == "management"
    assert data["kind"] == "tp_sl_update"
    assert data["applied"] is True
    assert data["target_signal"] == sid

    sig = next(s for s in client.get("/api/status").json()["signals"] if s["id"] == sid)
    assert abs(sig["stop_loss"] - 1.0790) < 1e-9
    assert abs(sig["take_profit_1"] - 1.0860) < 1e-9
    st = client.get("/api/status").json()
    open_legs = [p for p in st["positions"] if p["status"] == "open"]
    assert all(abs(p["stop_loss"] - 1.0790) < 1e-9 for p in open_legs)


def test_management_reply_targets_exact_trade(client):
    """A management message that REPLIES to a signal must act on THAT
    signal's trade — even when another trade of the same symbol is open."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 1})

    def _open(msg_id: str) -> str:
        r = client.post("/api/signals", json={
            "raw_text": EURUSD, "message_id": msg_id, "group": "g"})
        sid = r.json()["signal"]["id"]
        # completo → pendente já (sem janela); incompleto → buffered
        assert r.json()["action"] in ("buffered", "pending")
        # wait for the signal to be pending, then approve (retry: the
        # background loop may finalize with a slight delay)
        for _ in range(30):
            sig = next(s for s in client.get("/api/status").json()["signals"]
                       if s["id"] == sid)
            if sig["status"] == "pending":
                break
            time.sleep(0.1)
        client.post(f"/api/signals/{sid}/approve")
        return sid

    sid_a = _open("8001")
    sid_b = _open("8002")  # more recent, same symbol
    st = client.get("/api/status").json()
    trade_a = next(t["id"] for t in st["trades"]
                   if t["status"] == "open" and t["signal_id"] == sid_a)
    trade_b = next(t["id"] for t in st["trades"]
                   if t["status"] == "open" and t["signal_id"] == sid_b)

    # "SL HIT" replying to signal A (message_id 8001)
    r = client.post("/api/signals", json={
        "raw_text": "SL HIT", "source": "tg:Golden Moves",
        "message_id": "8003", "group": "g", "reply_to_msg_id": 8001})
    assert r.json()["action"] == "management"
    assert r.json()["kind"] == "sl_hit"
    assert r.json()["trade_id"] == trade_a

    # "Close TP1 here" replying to signal B closes B's leg 1, not A's
    r = client.post("/api/signals", json={
        "raw_text": "Close TP1 here", "source": "tg:Golden Moves",
        "message_id": "8004", "group": "g", "reply_to_msg_id": 8002})
    assert r.json()["kind"] == "close_tp1"
    assert r.json()["trade_id"] == trade_b

    st = client.get("/api/status").json()
    legs_a = [p for p in st["positions"] if p["trade_id"] == trade_a and p["status"] == "open"]
    legs_b = [p for p in st["positions"] if p["trade_id"] == trade_b and p["status"] == "open"]
    assert len(legs_a) == 2      # trade A untouched by the reply to B
    assert len(legs_b) == 1      # trade B lost exactly its leg 1


def test_management_reply_queued_until_trade_opens(client):
    """A management reply arriving while the signal is still in the edit
    window is queued and applied when that signal's trade opens."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "auto", "edit_window_seconds": 1})

    # sinal INCOMPLETO → fica na janela de edição (um sinal completo executaria
    # já e a gestão atuaria logo, sem fila)
    r = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0850", "message_id": "8101", "group": "g"})
    assert r.json()["action"] == "buffered"

    # management reply arrives BEFORE the signal completes/executes
    r = client.post("/api/signals", json={
        "raw_text": "Close first entries now and move stop loss above best entries",
        "source": "tg:Golden Moves", "message_id": "8102", "group": "g",
        "reply_to_msg_id": 8101})
    data = r.json()
    assert data["action"] == "management"
    assert data["queued"] is True

    # completa o sinal via edição (continua em buffer) e deixa a janela expirar
    # -> auto executa -> aplica a gestão pendente.
    client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920",
        "message_id": "8101", "group": "g"})
    # Poll instead of sleeping: the background loop finalizes awaiting_edit
    # on its own tick, so a fixed sleep races with it under load.
    trade_seen = None
    for _ in range(40):
        st = client.get("/api/status").json()
        ts = [t for t in st["trades"] if t["signal_id"].startswith("tg-g-8101")]
        if ts:
            trade_seen = ts[0]
            break
        time.sleep(0.25)
    assert trade_seen is not None, "a trade do sinal 8101 chegou a abrir"
    # the queued protect ran: leg 1 closed at market and the remaining leg's
    # SL was moved above entry (in a frozen market that SL closes it at once,
    # so the whole trade is done — the key assertion is that the pending
    # instruction was APPLIED, not ignored)
    assert trade_seen["status"] == "closed"
    assert st["account"]["open_trades"] == 0
    assert any("gestão" in (s["error"] or "") for s in st["signals"])  # recorded


def test_management_tp_hit_moves_remaining_to_breakeven(client):
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    client.post(f"/api/signals/{r.json()['signal']['id']}/approve")
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["status"] == "open")

    r = client.post("/api/signals", json={
        "raw_text": "TP1 HIT", "source": "tg:Golden Moves",
        "message_id": "7002", "group": "g"})
    assert r.json()["action"] == "management"
    assert r.json()["kind"] == "tp_hit"

    # o SL das pernas 2+ foi movido a breakeven — lido da BD (o snapshot só
    # mostra pernas abertas e o tick do mercado congelado pode já as ter
    # fechado; afirma-se o nível do SL, não o estado aberto)
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    positions = db.positions_for_trade(trade_id)
    remaining = [p for p in positions if p.leg != 1]
    assert remaining
    assert all(p.stop_loss == 1.0850 for p in remaining)  # moved to breakeven


def test_management_close_all_closes_trade(client):
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    client.post(f"/api/signals/{r.json()['signal']['id']}/approve")

    # "we're out" (sem BE): fecha tudo ao mercado
    r = client.post("/api/signals", json={
        "raw_text": "**Okay, we’re out after we secured TP2.**", "source": "tg:Golden Moves",
        "message_id": "7003", "group": "g"})
    assert r.json()["action"] == "management"
    assert r.json()["kind"] == "close_all"
    assert r.json()["applied"] is True

    st = client.get("/api/status").json()
    assert st["account"]["open_trades"] == 0


def test_management_out_at_be_protects_remaining(client):
    """"Out at BE" não fecha ao mercado — as pernas restantes já estão
    protegidas na entrada (SL em breakeven) e ficam a correr para o TP."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    client.post(f"/api/signals/{r.json()['signal']['id']}/approve")
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["status"] == "open")

    r = client.post("/api/signals", json={
        "raw_text": "Out at BE after we secured TP1 🙌🏼", "source": "tg:Golden Moves",
        "message_id": "7003b", "group": "g"})
    assert r.json()["kind"] == "close_all"
    assert "protegidas na entrada" in r.json()["notes"][0]

    # pernas restantes protegidas na entrada (SL == entry), lidas da BD — o
    # snapshot só mostra pernas abertas e o tick do mercado congelado pode já
    # as ter fechado (SL na entrada dispara no tick). O invariante é o SL ter
    # sido movido para a entrada, não o estado aberto.
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    positions = db.positions_for_trade(trade_id)
    remaining = [p for p in positions if p.leg != 1]
    assert remaining
    assert all(abs(p.stop_loss - 1.0850) < 1e-9 for p in remaining)  # SL na entrada


def test_management_summary_stored_ground_truth(client):
    r = client.post("/api/signals", json={
        "raw_text": "**Weekly summary ** **(10/08/26_14/08/26)** **Gold prime Signals ** 26 **Trades Sent** 23 **winning trades ** 2 Stop loss ** 1 BE ** Win rate : 91% **Pips gained +2100**",
        "source": "tg:Golden Moves", "message_id": "7004", "group": "g"})
    assert r.json()["action"] == "management"
    assert r.json()["kind"] == "summary"
    assert r.json()["parsed"] is True

    rr = client.get("/api/summaries").json()
    assert rr["ok"] is True
    assert rr["summaries"][0]["win_rate"] == 91.0
    assert rr["summaries"][0]["trades_sent"] == 26


def test_historical_summary_does_not_spam_notifications(client):
    """Resumos do replay histórico guardam o ground truth mas não voltam a
    notificar o feed (restart do listener não inunda a dashboard).
    Imune a ruído de fundo do loop: só falha se aparecer uma notificação
    NOVA sobre este resumo (comparação por id, não por contagem)."""
    antes = client.get("/api/notifications").json()
    ids0 = {n["id"] for n in antes}
    r = client.post("/api/signals", json={
        "raw_text": "**Daily summary ** **Gold prime Signals ** 6Trades Sent 5 winning trades 0 Stop loss 1 BE Pips gained +310",
        "source": "tg:Golden Moves", "message_id": "7006", "group": "g",
        "historical": True})
    assert r.json()["action"] == "management"
    assert r.json()["parsed"] is True
    depois = client.get("/api/notifications").json()
    novas = [n for n in depois if n["id"] not in ids0]
    spam = [n for n in novas
            if "gold prime" in (n.get("text") or "").lower()
            or "daily summary" in (n.get("text") or "").lower()]
    assert not spam, "replay histórico não deve criar notificações sobre o resumo"

    rr = client.get("/api/summaries").json()
    assert any(s["trades_sent"] == 6 for s in rr["summaries"]), "resumo diário guardado como ground truth"


def test_edit_after_execution_raises_risk_alert(client):
    """Se o grupo edita uma mensagem DEPOIS de o sinal ter executado (janela
    passada), a dashboard recebe um alerta de risco — para detetar entradas
    executadas com o preço desatualizado (janelas curtas)."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "auto", "edit_window_seconds": 1})
    r = client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "telegram", "message_id": "9001"})
    # sinal completo → executa já (sem esperar a janela de edição)
    assert r.json()["action"] == "executed"
    st = client.get("/api/status").json()
    assert any(t["signal_id"] == "tg-9001" for t in st["trades"]), \
        "sinal devia ter executado imediatamente"
    # edição pós-execução com texto diferente
    r2 = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0900\nSL 1.0870\nTP1 1.0940\nTP2 1.0970",
        "source": "telegram", "message_id": "9001"})
    assert r2.json()["action"] == "ignored"
    notifs = client.get("/api/notifications").json()
    assert any("editou o sinal" in (n.get("text") or "").lower() for n in notifs), \
        "edição pós-execução deve gerar alerta de risco"


def test_management_without_open_trade_is_ignored(client):
    _close_all_open_trades(client)
    r = client.post("/api/signals", json={
        "raw_text": "TP1 HIT", "source": "tg:Golden Moves",
        "message_id": "7005", "group": "g"})
    data = r.json()
    assert data["action"] == "management"
    assert data["applied"] is False


def test_management_without_target_raises_risk_notification(client):
    """Gestão do grupo sem trade aberta nem sinal a que responder dispara
    uma notificação risk — nunca fica silenciosa."""
    _close_all_open_trades(client)
    client.post("/api/signals", json={
        "raw_text": "TP2 HIT", "source": "tg:Golden Moves",
        "message_id": "7010", "group": "g"})
    notifs = client.get("/api/notifications").json()
    risk = [n for n in notifs if n["kind"] == "risk" and "sem alvo" in n["text"]]
    assert risk, "devia existir uma notificação risk de gestão sem alvo"
    assert "TP2 HIT" in risk[0]["text"]


def test_telegram_chat_is_ignored_and_audited(client):
    """Conversa do grupo (sem keywords de sinal nem gestão) nunca entra na
    pipeline — é auditada na tab Ignoradas e devolvida como ignored."""
    r = client.post("/api/signals", json={
        "raw_text": "Okay team, waiting for New York to open.",
        "source": "tg:Golden Moves", "message_id": "7011", "group": "g"})
    data = r.json()
    assert data["action"] == "ignored"
    assert "conversa" in data["reason"]
    # não entrou na pipeline de sinais
    st = client.get("/api/status").json()
    assert all(s["message_id"] != "7011" for s in st["signals"])
    # e ficou registada na auditoria
    rr = client.get("/api/ignored").json()
    assert rr["ok"] is True
    assert rr["total"] >= 1
    assert any(it["message_id"] == "7011" for it in rr["items"])
    assert "New York" in rr["items"][0]["raw_text"]


def test_telegram_typo_signal_still_waits_edit_window(client):
    """Mensagem com keywords de sinal mas mal formatada continua a esperar a
    janela de edição (não é auditoria) — o grupo pode corrigir."""
    r = client.post("/api/signals", json={
        "raw_text": "I'm buying XAUUSD TP1 4411 TP2 4413 SL 4392",
        "source": "tg:Golden Moves", "message_id": "7012", "group": "g"})
    data = r.json()
    assert data["action"] == "buffered"
    assert data["signal"]["status"] == "awaiting_edit"
    rr = client.get("/api/ignored").json()
    assert all(it["message_id"] != "7012" for it in rr["items"])


def test_conversation_mentioning_tp_sl_without_numbers_is_ignored(client):
    """Conversa que menciona 'TP1'/'SL' soltos (sem números) não é sinal.
    Ex. real do grupo: 'Again act to fast after TP1 / So I'll registered
    that as SL'. Deve ir para a auditoria de ignoradas, não ficar failed."""
    r = client.post("/api/signals", json={
        "raw_text": "Again act to fast after TP1\n\nSo I’ll registered that as SL",
        "source": "tg:Golden Moves", "message_id": "7013", "group": "g"})
    data = r.json()
    assert data["action"] == "ignored"
    assert "conversa" in data["reason"]
    # não entrou na pipeline de sinais
    st = client.get("/api/status").json()
    assert all(s["message_id"] != "7013" for s in st["signals"])
    # e ficou na auditoria de ignoradas
    rr = client.get("/api/ignored?days=0&q=act to fast").json()
    assert any(it["message_id"] == "7013" for it in rr["items"])


def test_heartbeat_and_diagnostics(client):
    """O heartbeat do listener alimenta a tab Diagnóstico."""
    r = client.post("/api/heartbeat", json={
        "groups": ["Golden Moves"], "last_msg_at": "2026-08-17T12:00:00", "tick": 42})
    assert r.json()["ok"] is True
    d = client.get("/api/diagnostics").json()
    assert d["ok"] is True
    assert d["listener"]["groups"] == ["Golden Moves"]
    assert d["listener"]["tick"] == 42
    assert d["listener"]["last_msg_age_s"] is not None
    assert d["server"]["uptime_s"] >= 0
    assert "mt5" in d and "db" in d and "auth" in d
    assert "uptime_history" in d  # gráfico de uptime 24h


def test_listener_code_flow(client):
    """Fluxo do código do Telegram: listener pede → dashboard entrega →
    listener consome (one-time)."""
    r = client.post("/api/listener/need_code")
    assert r.json()["ok"] is True
    d = client.get("/api/diagnostics").json()
    assert d["listener"]["need_code"] is True

    # a dashboard entrega o código
    client.post("/api/listener/code", json={"code": "15096"})
    d = client.get("/api/diagnostics").json()
    assert d["listener"]["need_code"] is False

    # o listener consome uma única vez
    got = client.get("/api/listener/code").json()
    assert got["code"] == "15096"
    got2 = client.get("/api/listener/code").json()
    assert got2["code"] is None


def test_ignored_audit_filters_by_keyword_and_days(client):
    """A auditoria filtra por palavra-chave (q) e período (days)."""
    client.post("/api/signals", json={
        "raw_text": "Good morning team, the key zones are coming",
        "source": "tg:Golden Moves", "message_id": "7020", "group": "g"})
    client.post("/api/signals", json={
        "raw_text": "Zoom live em 3 minutos",
        "source": "tg:Golden Moves", "message_id": "7021", "group": "g"})
    all_items = client.get("/api/ignored?days=0").json()
    assert all_items["total"] >= 2

    zoom = client.get("/api/ignored?days=0&q=zoom").json()
    assert zoom["total"] == 1
    assert zoom["items"][0]["raw_text"].startswith("Zoom")

    recent = client.get("/api/ignored?days=1").json()
    assert recent["total"] >= 2  # ambas são de hoje
    assert all(it["received_at"][:10] >= "2026-01-01" for it in recent["items"])

    none = client.get("/api/ignored?days=1&q=naoexiste123").json()
    assert none["total"] == 0


def test_buying_selling_gerund_enters_edit_window(client):
    """'I'm selling XAUUSD' (primeira linha, sem TP/SL ainda) é sinal em
    espera de edições — o grupo envia a direção primeiro e edita com os
    detalhes dentro do minuto. Não pode ir para a auditoria de ignoradas."""
    r = client.post("/api/signals", json={
        "raw_text": "I'm selling XAUUSD",
        "source": "tg:Golden Moves", "message_id": "7410", "group": "g"})
    data = r.json()
    assert data["action"] == "buffered"
    assert data["signal"]["status"] == "awaiting_edit"
    # não fica na auditoria de ignoradas
    ig = client.get("/api/ignored?days=0").json()
    assert all(it["message_id"] != "7410" for it in ig["items"])

    # a edição com os detalhes aplica-se à mesma mensagem (formato real do
    # grupo: entrada na sua própria linha)
    r2 = client.post("/api/signals", json={
        "raw_text": "I'm selling XAUUSD\n4384-4388\nTP1 4382\nTP2 4380\nTP3 4378\nTP4 4375\nTP5 4370\nSL 4392",
        "source": "tg:Golden Moves", "message_id": "7410", "group": "g"})
    assert r2.json()["action"] == "edited"


def test_ignored_then_edited_signal_removes_audit_record(client):
    """Mensagem que começa como conversa mas é editada para sinal não pode
    ficar a duplicar: o registo de ignorada é removido quando o sinal entra."""
    client.post("/api/signals", json={
        "raw_text": "Olá pessoal bora 7411marker",
        "source": "tg:Golden Moves", "message_id": "7411", "group": "g"})
    ig = client.get("/api/ignored?days=0&q=7411marker").json()
    assert ig["total"] == 1  # ficou como conversa

    # a mesma mensagem é editada para um sinal completo → deixa a auditoria
    r = client.post("/api/signals", json={
        "raw_text": "I'm selling XAUUSD\n4384-4388\nTP1 4382\nTP2 4380\nSL 4392",
        "source": "tg:Golden Moves", "message_id": "7411", "group": "g"})
    # completo → já é sinal (executa/fica pendente, conforme o modo)
    assert r.json()["signal"]["status"] in ("pending", "executed", "awaiting_edit")
    ig2 = client.get("/api/ignored?days=0&q=7411marker").json()
    assert ig2["total"] == 0  # auditoria limpa — já é sinal


def test_online_users_tracked_via_websocket(client):
    """A lista de utilizadores ligados vem do WebSocket (header da
    dashboard): ao ligar aparece na lista; ao desligar desaparece."""
    st0 = client.get("/api/status").json()
    assert "online_users" in st0

    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        users = snap["data"]["online_users"]
        assert any(u["user"] == "admin" for u in users), \
            "o cliente ligado deve aparecer em online_users"
        assert all("since" in u for u in users)

    # depois de fechar a ligação, deixa de contar
    st1 = client.get("/api/status").json()
    assert all(u["user"] != "admin" for u in st1["online_users"]) or \
        st1["online_users"] == st0["online_users"]


def test_go_message_finalizes_awaiting_signal(client):
    """'Do it now team' encurta a janela: o sinal em espera executa já
    (modo auto) em vez de esperar a janela de edição."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "auto", "risk_pct": 1.0, "edit_window_seconds": 60})
    try:
        # 1) versão incompleta → janela de edição
        r = client.post("/api/signals", json={
            "raw_text": "I'm selling XAUUSD",
            "source": "tg:Golden Moves", "message_id": "7420", "group": "g"})
        assert r.json()["action"] == "buffered"
        sid = r.json()["signal"]["id"]

        # 2) a edição completa o sinal — continua em espera (sem executar)
        client.post("/api/signals", json={
            "raw_text": "I'm selling XAUUSD\n4384-4388\nTP1 4382\nTP2 4380\nTP3 4378\nTP4 4375\nTP5 4370\nSL 4392",
            "source": "tg:Golden Moves", "message_id": "7420", "group": "g"})

        # 3) GO do grupo (como resposta ao sinal): finaliza já
        g = client.post("/api/signals", json={
            "raw_text": "Do it now team",
            "source": "tg:Golden Moves", "message_id": "7421", "group": "g",
            "reply_to_msg_id": 7420})
        data = g.json()
        assert data["action"] == "management"
        assert data["kind"] == "go"

        st = client.get("/api/status").json()
        sig = next(s for s in st["signals"] if s["id"] == sid)
        assert sig["status"] == "executed", "GO devia executar o sinal em espera"
    finally:
        client.post("/api/config", json={"mode": "manual", "risk_pct": 2.0})


def test_go_message_without_signal_is_recorded_only(client):
    """'Do it now' sem sinal em espera regista-se como gestão, sem risco."""
    from app.main import state as st
    for s in st.db.awaiting_edit_signals():  # limpa sobras de outros testes
        st.db.set_signal_status(s.id, "failed")
    g = client.post("/api/signals", json={
        "raw_text": "Do it now team",
        "source": "tg:Golden Moves", "message_id": "7422", "group": "g"})
    data = g.json()
    assert data["action"] == "management"
    assert data["kind"] == "go"
    assert data["applied"] is False
    # não fica na auditoria de ignoradas
    ig = client.get("/api/ignored?days=0").json()
    assert all(it["message_id"] != "7422" for it in ig["items"])


def test_mt5_offline_raises_risk_notification(client, monkeypatch):
    """Queda do MT5 dispara notificação risk (feed + push se configurado)."""
    import app.main as main_mod
    st = main_mod.state
    # A suite corre em EXECUTION_MODE=sim para nunca tocar no terminal real;
    # simula explicitamente o snapshot MT5 que este teste quer exercer.
    monkeypatch.setattr(
        st, "_execution_state",
        lambda: {"mode": "mt5-real", "connected": False,
                 "trade_allowed": False},
    )

    # notifier stub: o broadcast WS exige o loop do TestClient — o método só
    # precisa do insert_notification para o teste. Usa um marcador único para
    # não depender de contagens (o tick de fundo também gera notificações).
    import time as _time
    tag = f"teste-mt5-{_time.time()}"

    class _Stub:
        async def send(self, kind, text, created_at=""):
            st.db.insert_notification(kind, text + " " + tag, created_at)
    orig = st.notifier
    st.notifier = _Stub()
    try:
        import asyncio
        st._mt5_was_connected = None
        asyncio.run(st._check_mt5_connection())  # primeiro tick: regista
        st._mt5_was_connected = True
        st._last_mt5_offline_notif = 0.0
        asyncio.run(st._check_mt5_connection())  # queda → notificação
    finally:
        st.notifier = orig

    notifs = st.db.list_notifications(500)
    new = [n for n in notifs
           if n.kind == "risk" and "MT5" in n.text and tag in n.text]
    assert new, "queda do MT5 devia gerar notificação risk"


def test_auto_trading_off_raises_immediate_risk_alert(client):
    """retcode 10027 (AutoTrading OFF) no broker dispara um alerta risk
    imediato e distinto — não só o erro genérico no log."""
    from app.main import state as st
    from app.models import Trade

    class _FakeSim:
        def __init__(self, inner):
            self._inner = inner

        def open_trade(self, signal, risk_pct, balance, **kw):
            # devolve uma trade sintética fechada SEM tocar na BD — simula o
            # MT5Engine a recusar a ordem com 10027 (sem posições fantasma).
            return Trade(
                id="t-fake-10027", signal_id=signal.id, symbol=signal.symbol,
                direction=signal.direction, entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2, risk_pct=risk_pct,
                lots_1=0.0, lots_2=0.0, status="closed",
                opened_at="2026-08-18T14:00:00+00:00",
                error="AutoTrading desligado no terminal MT5 (retcode 10027)",
                retcodes=[10027], auto_trading_off=True,
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = st.sim
    st.sim = _FakeSim(orig)
    try:
        client.post("/api/config", json={"mode": "manual"})
        r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
        sid = r.json()["signal"]["id"]
        r = client.post(f"/api/signals/{sid}/approve")
        assert r.json()["action"] == "execution_failed"
    finally:
        st.sim = orig

    notifs = st.db.list_notifications(500)
    assert any("AutoTrading DESLIGADO" in n.text for n in notifs), (
        "retcode 10027 devia gerar o alerta imediato de AutoTrading OFF"
    )


def test_listener_offline_watchdog_notifies(client):
    """O watchdog notifica quando reinicia o listener offline."""
    import app.main as main_mod
    st = main_mod.state

    import time as _time
    tag = f"teste-listener-{_time.time()}"

    class _Stub:
        async def send(self, kind, text, created_at=""):
            st.db.insert_notification(kind, text + " " + tag, created_at)
    orig = main_mod.notifier
    orig_spawn = main_mod._spawn_listener
    orig_running = main_mod._listener_running_anywhere
    main_mod.notifier = _Stub()
    main_mod._spawn_listener = lambda: None  # não criar processos reais
    main_mod._listener_running_anywhere = lambda: False
    try:
        st.listener_last_seen = "2020-01-01T00:00:00"  # heartbeat antigo → offline
        st.listener_proc = None
        st.listener_next_restart_at = 0.0

        from app.main import _ensure_listener_alive
        import asyncio
        asyncio.run(_ensure_listener_alive())
    finally:
        main_mod.notifier = orig
        main_mod._spawn_listener = orig_spawn
        main_mod._listener_running_anywhere = orig_running

    notifs = st.db.list_notifications(500)
    new = [n for n in notifs
           if n.kind == "risk" and "Listener" in n.text and tag in n.text]
    assert new, "reinício do listener offline devia notificar"


def test_watchdog_does_not_duplicate_existing_listener(client):
    """Se já houver um listener a correr (arranque manual/restart do
    servidor), o watchdog não spawna um duplicado — só espera o heartbeat."""
    import app.main as main_mod
    st = main_mod.state

    orig = main_mod.notifier
    orig_spawn = main_mod._spawn_listener
    orig_running = main_mod._listener_running_anywhere
    spawned = []
    main_mod.notifier = type("Stub", (), {"send": lambda *a, **k: None})()
    main_mod._spawn_listener = lambda: spawned.append(1)
    main_mod._listener_running_anywhere = lambda: True
    try:
        st.listener_last_seen = "2020-01-01T00:00:00"  # offline
        st.listener_proc = None
        st.listener_next_restart_at = 0.0

        from app.main import _ensure_listener_alive
        import asyncio
        asyncio.run(_ensure_listener_alive())
    finally:
        main_mod.notifier = orig
        main_mod._spawn_listener = orig_spawn
        main_mod._listener_running_anywhere = orig_running

    assert spawned == [], "não devia spawnar um segundo listener"
    assert st.listener_next_restart_at > 0, "devia pôr cooldown e esperar"


def test_watchdog_restart_with_running_listener_no_duplicate(client):
    """Regressão: um restart do servidor com o listener já ativo NÃO pode
    nascer um duplicado. Sequência real: (1) sem listener → o watchdog
    spawna 1; (2) restart do servidor (estado novo, proc desconhecido,
    heartbeat ainda não registou) com o listener anterior ainda a correr →
    o watchdog vê o processo existente e NÃO spawna outro."""
    import app.main as main_mod
    st = main_mod.state

    orig = main_mod.notifier
    orig_spawn = main_mod._spawn_listener
    orig_running = main_mod._listener_running_anywhere
    spawned = []
    main_mod.notifier = type("Stub", (), {"send": lambda *a, **k: None})()
    main_mod._spawn_listener = lambda: spawned.append(1)

    from app.main import _ensure_listener_alive
    import asyncio

    def reset():
        st.listener_last_seen = "2020-01-01T00:00:00"
        st.listener_proc = None
        st.listener_next_restart_at = 0.0

    try:
        # (1) sem listener nenhum a correr → o watchdog spawna 1
        main_mod._listener_running_anywhere = lambda: False
        reset()
        asyncio.run(_ensure_listener_alive())
        assert spawned == [1], "sem listener, o watchdog devia spawnar 1"

        # (2) restart do servidor: o listener anterior continua a correr
        # (fora do estado do servidor novo) → não pode spawnar outro
        main_mod._listener_running_anywhere = lambda: True
        reset()
        asyncio.run(_ensure_listener_alive())
        assert spawned == [1], "restart com listener ativo NÃO pode duplicar"
        assert st.listener_next_restart_at > 0, "devia pôr cooldown e esperar"
    finally:
        main_mod.notifier = orig
        main_mod._spawn_listener = orig_spawn
        main_mod._listener_running_anywhere = orig_running


def test_listener_restart_history_recorded(client):
    """Cada reinício do watchdog fica na BD (listener_restarts) e aparece
    no diagnóstico."""
    import app.main as main_mod
    st = main_mod.state
    before = len(st.db.list_listener_restarts(100))

    orig = main_mod.notifier
    orig_spawn = main_mod._spawn_listener
    orig_running = main_mod._listener_running_anywhere
    main_mod.notifier = type("Stub", (), {"send": lambda *a, **k: None})()
    main_mod._spawn_listener = lambda: None
    main_mod._listener_running_anywhere = lambda: False
    try:
        st.listener_last_seen = "2020-01-01T00:00:00"
        st.listener_proc = None
        st.listener_next_restart_at = 0.0
        from app.main import _ensure_listener_alive
        import asyncio
        asyncio.run(_ensure_listener_alive())
    finally:
        main_mod.notifier = orig
        main_mod._spawn_listener = orig_spawn
        main_mod._listener_running_anywhere = orig_running

    rests = st.db.list_listener_restarts(100)
    new = [x for x in rests[before:] if x["reason"] == "watchdog: heartbeat parado"]
    assert new, "o reinício devia ficar registado na BD"

    d = client.get("/api/diagnostics").json()
    assert any(x["reason"] == "watchdog: heartbeat parado" for x in d["listener_restarts"]), \
        "o diagnóstico devia expor o histórico de reinícios"


def test_listener_restarts_purged_after_30_days(client):
    """Registos de reinícios com mais de 30 dias são apagados pelo purge
    (a tabela listener_restarts não cresce sem limite)."""
    import app.main as main_mod
    db = main_mod.state.db
    db.record_listener_restart("teste: antigo", started_at="2020-01-01T00:00:00")
    db.record_listener_restart("teste: recente", started_at="2099-01-01T00:00:00")
    try:
        purged = db.purge_old_listener_restarts(30)
        assert purged >= 1, "o purge devia apagar pelo menos o registo antigo"
        rows = db.list_listener_restarts(1000)
        assert not any(x["started_at"].startswith("2020") for x in rows), \
            "registo com mais de 30 dias devia ser apagado"
        assert any(x["started_at"].startswith("2099") for x in rows), \
            "registo recente não pode ser apagado"
    finally:
        with db.tx() as cur:
            cur.execute("DELETE FROM listener_restarts WHERE reason LIKE 'teste:%'")


def test_clear_listener_restarts_manual(client):
    """DELETE /api/listener_restarts apaga TODO o histórico (ação manual da
    dashboard, com confirmação) — além do purge automático de 30 dias."""
    import app.main as main_mod
    db = main_mod.state.db
    db.record_listener_restart("teste: manual")
    r = client.delete("/api/listener_restarts")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["deleted"] >= 1
    assert db.list_listener_restarts(100) == [], \
        "depois do clear manual a tabela fica vazia"
    # idempotente: limpar outra vez não rebenta
    r2 = client.delete("/api/listener_restarts")
    assert r2.status_code == 200


# ----------------------------------------------------------------------
# Gestão do grupo aplicada à corrida paper + atribuição de saída
# ----------------------------------------------------------------------

def _paper_trade_for_signal(db, strategy_id: str, signal_id: str,
                            include_closed: bool = False):
    """Trade de papel de um sinal (estratégia de teste). Só as ABERTAS por
    omissão; com include_closed=True também encontra as já fechadas — a
    gestão do grupo + o tick do mercado congelado podem fechar a trade de
    papel antes de o teste a ler (fonte de flakiness)."""
    rows = (db.paper_trades(strategy_id) if include_closed
            else db.paper_open_trades(strategy_id))
    return next((t for t in rows if t["signal_id"] == signal_id), None)


def test_paper_applies_group_protect(client):
    """A corrida paper aplica a gestão do grupo às estratégias que a honram:
    'protect' fecha a perna 1 e move o SL das restantes — antes as contas
    '+ gestão' comportavam-se como as sem gestão até o preço bater SL/TP."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    def _legs(sid_: str) -> dict:
        # inclui trades de papel já fechadas: depois do protect o tick do
        # mercado congelado pode fechar as pernas restantes e finalizar a
        # trade de papel antes de o teste a ler
        pt = _paper_trade_for_signal(db, sid_, sid, include_closed=True)
        if not pt:
            return {}
        return {p.leg: p for p in db.paper_positions_for_trade(pt["id"])}

    # antes da gestão: a sem-gestão (nova) tem a perna 1 aberta
    assert _legs("nova")[1].status == "open"

    r = client.post("/api/signals", json={
        "raw_text": "+30 pips from best entires  Close first entires now and move stop loss above best entires",
        "source": "tg:Golden Moves", "message_id": "7200", "group": "g"})
    assert r.json()["kind"] == "protect"

    # estratégias com gestão: perna 1 fechada (razão protect) + SL movido
    # (a principal nova_gestao e a tp2_protect — Fase 2 — honram o protect)
    for sid_ in ("nova_gestao", "tp2_protect"):
        legs = _legs(sid_)
        assert legs.get(1) and legs[1].status == "closed", sid_
        assert legs[1].exit_reason == "protect", sid_
        assert legs[1].exit_msg_id == "7200", sid_
        assert abs(legs[2].stop_loss - 1.0820) > 1e-9, sid_  # SL movido
    # estratégias sem gestão: perna 1 continua aberta, SL intacto
    assert _legs("nova")[1].status == "open"
    assert abs(_legs("nova")[2].stop_loss - 1.0820) < 1e-9
    assert _legs("auto_escada_3")[1].status == "open", "auto ignora o grupo"
    # as removidas da consolidação (nova_gestao_3/tp2_gestao) já não abrem
    # papel — o protect não tem onde agir
    assert _paper_trade_for_signal(db, "nova_gestao_3", sid) is None
    assert _paper_trade_for_signal(db, "tp2_gestao", sid) is None
    # sem gestão: a nova (contrafactual, MENU_HIDDEN) e as auto_* continuam
    # com a perna 1 aberta (as removidas — tp2/auto_2 — já não abrem papel)
    for sid_ in ("nova", "auto_3", "auto_5", "auto_escada_5"):
        assert _legs(sid_)[1].status == "open", sid_
    assert _paper_trade_for_signal(db, "tp2", sid) is None
    assert _paper_trade_for_signal(db, "auto_2", sid) is None


def test_paper_close_all_applies_only_to_managed_visible(client):
    """close_all aplica-se em papel às estratégias que honram o kind
    (nova_gestao, nova_gestao_3, tp2_gestao — gestão completa); as sem
    gestão (nova, tp2, auto_*) continuam abertas; tp2_protect/auto_escada_3
    também (Fase 2: é exatamente o que se quer medir)."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")

    r = client.post("/api/signals", json={
        "raw_text": "We're out of this setup", "source": "tg:Golden Moves",
        "message_id": "7201", "group": "g"})
    assert r.json()["kind"] == "close_all"

    def _still_open(sid_: str) -> bool:
        return _paper_trade_for_signal(db, sid_, sid) is not None

    # gestão completa: a principal fechou (já não há trade aberta em papel)
    assert _still_open("nova_gestao") is False
    assert _still_open("nova_gestao_3") is False
    assert _still_open("tp2_gestao") is False
    # sem gestão continua: a nova (contrafactual, MENU_HIDDEN) e a auto_5
    # (comparação, MENU_HIDDEN) — continuam em papel apesar de ocultas do menu
    assert _still_open("nova") is True
    assert _still_open("auto_5") is True
    # as removidas da consolidação já não abrem papel (só backtest/compare)
    assert _paper_trade_for_signal(db, "tp2", sid) is None
    assert _paper_trade_for_signal(db, "auto_2", sid) is None
    # só protect (tp2_protect) e as auto_* restantes não honram close_all
    assert _still_open("tp2_protect") is True
    assert _still_open("auto_escada_3") is True
    assert _still_open("auto_3") is True
    assert _still_open("auto_escada_5") is True


def test_exit_reason_recorded_on_close_all(client):
    """Trades/pernas fechadas pela gestão do grupo registam exit_reason +
    exit_msg_id — a dashboard mostra a razão do fecho e o contrafactual."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["signal_id"] == sid)

    r = client.post("/api/signals", json={
        "raw_text": "We're out", "source": "tg:Golden Moves",
        "message_id": "7202", "group": "g"})
    assert r.json()["kind"] == "close_all"

    st = client.get("/api/status").json()
    closed = next(t for t in st["trades"] if t["id"] == trade_id)
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "close_all"
    rows = db.conn.execute(
        "SELECT exit_reason, exit_msg_id FROM positions WHERE trade_id = ?",
        (trade_id,)).fetchall()
    assert rows and all(rw["exit_reason"] == "close_all" for rw in rows)
    assert all(rw["exit_msg_id"] == "7202" for rw in rows)


def test_manage_kinds_filter_real_trade(client):
    """Com a estratégia tp2_protect promovida a principal, a conta real
    IGNORA close_all (não está nos manage_kinds): o grupo diz 'we're out' e
    a trade continua aberta — registado e notificado, sem fechar."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"mode": "manual", "edit_window_seconds": 60,
                                     "strategy": "tp2_protect"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    client.post(f"/api/signals/{sid}/approve")
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["signal_id"] == sid)

    r = client.post("/api/signals", json={
        "raw_text": "We're out of this setup", "source": "tg:Golden Moves",
        "message_id": "7203", "group": "g"})
    data = r.json()
    assert data["kind"] == "close_all"
    assert data["applied"] is True  # registada e notificada
    assert any("não aplica" in n for n in data.get("notes", []))

    st = client.get("/api/status").json()
    t = next(x for x in st["trades"] if x["id"] == trade_id)
    assert t["status"] == "open"  # close_all ignorado pela estratégia atual
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_paper_race_switch_suggestion_filters_hidden_and_is_consistent(client):
    """Auto-switch por regime: a sugestão dispara para o LÍDER da corrida
    paper (>= 3 trades e >= 30 min à frente) — com TODAS as estratégias no
    menu (19/08), a tp2 (saldo maior) é a sugerida e tem botão de promoção."""
    import asyncio
    import time as _time
    from app.main import state as st
    from app.db import DB
    from app.config import DB_PATH
    db = DB(DB_PATH)

    def _seed(sid, balance, n_closed):
        db.conn.execute("DELETE FROM paper_accounts WHERE strategy_id=?", (sid,))
        db.conn.execute("DELETE FROM paper_trades WHERE strategy_id=?", (sid,))
        db.ensure_paper_account(sid, 1000)
        db.update_paper_balance(sid, balance)
        now = "2026-08-18T00:00:00"
        for i in range(n_closed):
            db.conn.execute(
                "INSERT INTO paper_trades (id, strategy_id, signal_id, symbol,"
                " direction, entry, stop_loss, take_profit_1, take_profit_2,"
                " take_profit_3, take_profit_4, take_profit_5, lots_1, lots_2,"
                " lots_3, lots_4, lots_5, status, opened_at, closed_at, pnl,"
                " exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"pt-{sid}-{i}", sid, "s", "EURUSD", "buy", 1.0, 0.9, 1.1, 1.2,
                 0, 0, 0, 0.01, 0.01, 0, 0, 0, "closed", now, now, 5.0, "tp1"))
        db.conn.commit()

    tag = f"teste-switch-{_time.time()}"

    class _Stub:
        async def send(self, kind, text, created_at="", tg_text="",
                       parse_mode="Markdown", action=""):
            db.insert_notification(kind, text + " " + tag,
                                   created_at or "2026-08-18T00:00:00",
                                   action or None)

    orig = st.notifier
    orig_suggested = st._race_switch_suggested
    st.notifier = _Stub()
    try:
        princ = st._balance()
        _seed("tp2_protect", princ + 500, 3)     # líder visível (dropdown)
        _seed("auto_escada_3", princ + 200, 3)   # segunda, também consistente
        st._race_leader = "tp2_protect"
        st._race_leader_since = _time.time() - 1900
        st._race_switch_suggested = None

        asyncio.run(st._check_paper_race_leader())

        # a tp2_protect tem o saldo MAIOR e está visível → é a sugerida
        assert st._race_switch_suggested == "tp2_protect"

        notifs = db.list_notifications(500)
        hits = [n for n in notifs
                if n.kind == "race" and tag in n.text and "Sugestão de troca" in n.text]
        assert hits, "devia sugerir a troca (notificação race)"
        assert any("TP2" in h.text for h in hits)  # é a tp2_protect
        # o botão "Promover" no feed leva a action
        assert any(h.action == "promote:tp2_protect" for h in hits)
        # estratégias fora da corrida (tp2, removida) nunca são sugeridas
        assert st._race_switch_suggested != "tp2"
    finally:
        st.notifier = orig
        st._race_switch_suggested = orig_suggested
        for sid in ("tp2_protect", "nova_gestao_3", "auto_escada_3"):
            db.conn.execute("DELETE FROM paper_accounts WHERE strategy_id=?", (sid,))
            db.conn.execute("DELETE FROM paper_trades WHERE strategy_id=?", (sid,))
        db.conn.commit()


def test_dynamic_risk_scales_down_after_drawdown(client):
    """Risco dinâmico: cada -20% de drawdown desde o pico de equity corta o
    risk_pct a metade (0.5 ** (dd/20)), com piso de 0.1%."""
    from app.main import state as st
    _close_all_open_trades(client)
    orig = {k: st.config.get(k) for k in ("risk_pct", "peak_balance", "balance")}
    try:
        st.config["risk_pct"] = 2.5
        st.config["balance"] = 10000.0
        st.config["peak_balance"] = 10000.0  # no pico → sem drawdown
        assert abs(st._effective_risk_pct() - 2.5) < 1e-9

        st.config["balance"] = 8000.0       # -20% → metade
        assert abs(st._effective_risk_pct() - 1.25) < 1e-9

        st.config["balance"] = 6000.0       # -40% → um quarto
        assert abs(st._effective_risk_pct() - 0.625) < 1e-9

        st.config["balance"] = 0.0          # drawdown extremo → piso 0.1
        assert abs(st._effective_risk_pct() - 0.1) < 1e-9
    finally:
        for k, v in orig.items():
            st.config[k] = v


def test_fase3_eta(client):
    """`fase3_eta` no snapshot: ritmo de sinais/dia (trades de papel abertas
    da auto_escada_3 nos últimos 30 dias) e data estimada dos 20 sinais —
    o painel Fase 3 mostra a contagem regressiva."""
    import json
    from datetime import datetime, timedelta, timezone
    from app.main import state
    from app.models import Trade

    data = client.get("/api/status").json()
    eta = data.get("fase3_eta")
    assert eta is not None and eta["target"] == 20
    assert eta["done"] is False

    # 10 trades de papel abertas nos últimos 10 dias → ritmo 10/30 sinais/dia
    now = datetime.now(timezone.utc)
    for i in range(10):
        opened = (now - timedelta(days=i)).isoformat(timespec="seconds")
        closed = (now - timedelta(days=i, hours=-2)).isoformat(timespec="seconds")
        state.db.insert_paper_trade(Trade(
            id=f"eta-pt-{i}", strategy_id="auto_escada_3",
            signal_id=f"eta-sig-{i}", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
            take_profit_2=4405.0, risk_pct=2.0, lots_1=0.01, lots_2=0.01,
            status="closed", opened_at=opened, closed_at=closed, pnl=5.0,
        ))
    data = client.get("/api/status").json()
    eta = data["fase3_eta"]
    assert eta["done"] is False
    assert eta["pace"] is not None and eta["pace"] > 0
    assert eta["eta"], "com ritmo > 0 devia haver data estimada"
    # comparação com o mês anterior + dias até à meta (alerta do tick)
    assert "pace_prev" in eta and "eta_days" in eta
    assert eta["eta_days"] > 0
    # mini-curva do ritmo (60 dias, zero-preenchida) — painel da Fase 2
    assert len(eta["pace_daily"]) == 60
    assert all("day" in d and "n" in d and "n_ref" in d for d in eta["pace_daily"])
    assert sum(d["n"] for d in eta["pace_daily"]) >= 10  # as 10 do teste


def test_fase3_validation_rollback(client):
    """Fim da janela de validação (10 trades reais) com PF < 1 → ROLLBACK
    automático para a nova_gestao + veredicto persistido (SPEC §B.5.4)."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from app.main import state
    from app.models import Trade

    # isolamento: a BD é module-scoped — remove as trades reais de outros
    # testes (a janela é definida por closed_at >= promoted_at e o loop de
    # fundo pode fechar posições antigas após a mudança de estratégia)
    with state.db.tx() as cur:
        cur.execute("DELETE FROM trades")

    r = client.post("/api/config", json={"strategy": "auto_escada_3"})
    assert r.json().get("ok") is True

    # snapshot de promoção "antigo" — as 10 trades contam na janela
    promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.db.set_config("fase3_snapshot", json.dumps({
        "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
        "n": 0, "pf": None, "pnl": 0, "ref_pf": None, "ref_pnl": 0,
        "dd_auto": None, "dd_ref": None, "paper_vs_real_pairs": 0,
        "real_trades_closed": 0, "sl_audit_entries": 0,
    }))
    state.db.set_config("fase3_verdict", "")

    # 10 trades reais fechadas DEPOIS da promoção, todas perdedoras → PF < 1
    now = datetime.now(timezone.utc)
    for i in range(10):
        closed = (now + timedelta(hours=i + 1)).isoformat(timespec="seconds")
        state.db.insert_trade(Trade(
            id=f"fv-tr-{i}", signal_id=f"fv-sig-{i}", symbol="XAUUSD",
            direction="buy",            entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
            take_profit_2=4405.0, risk_pct=2.0, lots_1=0.01, lots_2=0.01,
            status="closed", opened_at=promoted_at,
            closed_at=closed, pnl=-5.0, exit_reason="sl",
        ))
    asyncio.run(state._check_fase3_validation())

    # rollback: config volta à nova_gestao + veredicto persistido
    assert state.config["strategy"] == "nova_gestao"
    v = json.loads(state.db.all_config()["fase3_verdict"])
    assert v["decision"] == "rollback"
    assert v["window_trades"] == 10
    assert v["pf"] is not None and v["pf"] < 1.0
    assert "PF" in v["reason"] or "1.0" in v["reason"]
    # a métrica do doc (Parte X): mesma saída que o papel ≥ 9/10 — sem
    # pares paper↔MT5 (corrida paper vazia) conta 0/10, mas o campo existe
    assert v["same_exit_n"] == 0 and v["same_exit_pct"] == 0
    # janela sem duplicados nem modifies recusados (auditoria autosl: vazia)
    assert v["duplicate_orders"] == 0 and v["refused_retcodes"] == 0

    # o veredicto fica exposto no snapshot
    data = client.get("/api/status").json()
    assert data["fase3_verdict"]["decision"] == "rollback"

    # histórico persistido: a reabertura da janela (nova promoção) limpa o
    # veredicto ATUAL mas NUNCA apaga os veredictos antigos. Nota: o loop de
    # fundo (1s) pode reavaliar a janela reaberta (a re-promoção reabre a
    # janela com as MESMAS 10 trades fechadas → novo veredicto legítimo) — o
    # contrato é "nunca apaga o histórico", não "fica exatamente com 1".
    assert len(state._fase3_verdicts()) >= 1
    # resumo diário (ESTRATEGIAS.md Parte X): com a auto destituída pelo
    # rollback HOJE, a linha da auto MT5 continua no resumo — destacando o
    # veredicto do dia
    line = state._fase3_mt5_line()
    assert "ROLLBACK" in line and "nova_gestao" in line
    r = client.post("/api/config", json={"strategy": "auto_escada_3"})
    assert r.json().get("ok") is True
    assert state.db.all_config().get("fase3_verdict") in (None, "")
    assert len(state._fase3_verdicts()) >= 1
    data = client.get("/api/status").json()
    assert len(data["fase3_verdicts"]) >= 1
    # fecha a janela para o loop de fundo não reavaliar durante o resto da
    # suite (sem trades, a janela fica vazia → veredicto atual fica limpo)
    with state.db.tx() as cur:
        cur.execute("DELETE FROM trades WHERE id LIKE 'fv-tr-%'")
    assert client.get("/api/status").json().get("fase3_verdict") is None


def test_fase3_validation_rollback_duplicate_orders(client):
    """Janela de validação com ORDENS DUPLICADAS (perna com o mesmo `leg`
    re-enviada — o comentário único `trade.id:legN` no broker) → ROLLBACK
    automático, como o doc Parte X exige ("zero ordens duplicadas")."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from app.main import state
    from app.models import Position, Trade

    with state.db.tx() as cur:
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM positions")

    r = client.post("/api/config", json={"strategy": "auto_escada_3"})
    assert r.json().get("ok") is True

    promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # 10 trades reais fechadas após a promoção, com lucro (PF ≥ 1 — o
    # rollback tem de vir das duplicadas, não do PF)
    now = datetime.now(timezone.utc)
    for i in range(10):
        closed = (now + timedelta(hours=i + 1)).isoformat(timespec="seconds")
        state.db.insert_trade(Trade(
            id=f"dup-tr-{i}", signal_id=f"dup-sig-{i}", symbol="XAUUSD",
            direction="buy", entry=4400.0, stop_loss=4392.0,
            take_profit_1=4402.0, take_profit_2=4405.0, risk_pct=2.0,
            lots_1=0.01, lots_2=0.01, status="closed", opened_at=promoted_at,
            closed_at=closed, pnl=5.0, exit_reason="tp1",
        ))
        # a trade 3 tem a perna 1 DUPLICADA (ordem re-enviada ao broker)
        # — IDs únicos obrigatórios (INSERT OR REPLACE com PK repetido
        # sobrepõe em vez de duplicar)
        if i == 3:
            for k, leg in enumerate((1, 1, 2)):
                state.db.insert_position(Position(
                    id=f"dup-pos-{i}-{k}", trade_id=f"dup-tr-{i}",
                    leg=leg, symbol="XAUUSD", direction="buy", lots=0.01,
                    entry=4400.0, stop_loss=4392.0, take_profit=4402.0,
                    status="closed", opened_at=promoted_at, closed_at=closed,
                    close_price=4402.0, pnl=2.0, exit_reason="tp1",
                ))
        else:
            for k, leg in enumerate((1, 2)):
                state.db.insert_position(Position(
                    id=f"dup-pos-{i}-{k}", trade_id=f"dup-tr-{i}",
                    leg=leg, symbol="XAUUSD", direction="buy", lots=0.01,
                    entry=4400.0, stop_loss=4392.0, take_profit=4402.0,
                    status="closed", opened_at=promoted_at, closed_at=closed,
                    close_price=4402.0, pnl=2.0, exit_reason="tp1",
                ))

    # Publica o snapshot só depois de semear trades/posições; o TestClient
    # tem um watchdog em paralelo e não deve avaliar uma janela incompleta.
    state.db.set_config("fase3_verdict", "")
    state.db.set_config("fase3_snapshot", json.dumps({
        "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
        "n": 0, "pf": None, "pnl": 0, "ref_pf": None, "ref_pnl": 0,
        "dd_auto": None, "dd_ref": None, "paper_vs_real_pairs": 0,
        "real_trades_closed": 0, "sl_audit_entries": 0,
    }))
    asyncio.run(state._check_fase3_validation())

    assert state.config["strategy"] == "nova_gestao"
    v = json.loads(state.db.all_config()["fase3_verdict"])
    assert v["decision"] == "rollback"
    assert v["duplicate_orders"] == 1
    assert "duplicadas" in v["reason"]

    # repõe a estratégia principal para não afetar os outros testes
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_fase3_validation_requires_nine_paper_matches_and_first_ten():
    """A janela usa exatamente as primeiras 10 trades e não pode manter a
    auto quando não existe evidência paper↔MT5 para os 9/10 exigidos."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from app.db import DB
    from app.models import Trade
    from app.state import _compute_fase3_verdict

    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_config("fase3_snapshot", json.dumps({
            "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
        }))
        base = datetime.fromisoformat(promoted_at)
        # A 11.ª trade existe para garantir que o veredicto não cresce para
        # além da janela fixa de validação.
        for i in range(11):
            closed = (base + timedelta(hours=i + 1)).isoformat(timespec="seconds")
            db.insert_trade(Trade(
                id=f"match-tr-{i}", signal_id=f"match-sig-{i}",
                strategy_id="auto_escada_3", symbol="XAUUSD", direction="buy",
                entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
                take_profit_2=4405.0, risk_pct=2.0, lots_1=0.01, lots_2=0.01,
                status="closed", opened_at=promoted_at, closed_at=closed,
                pnl=5.0, exit_reason="tp1",
            ))

        async def _pairs(db_, strategy_id=None):
            return {"pairs": []}

        async def _rvb(db_, strategy_id=None):
            return {"trades": []}

        with patch("app.analysis.paper_vs_real_auto", new=_pairs), \
             patch("app.analysis.real_vs_backtest", new=_rvb):
            verdict = asyncio.run(_compute_fase3_verdict(db, "auto_escada_3"))

        assert verdict is not None
        assert verdict["window_trades"] == 10
        assert verdict["same_exit_n"] == 0
        assert verdict["decision"] == "rollback"
        assert "< 9/10" in verdict["reason"]
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_fase3_validation_rollback_slow_opens():
    """Janela de validação com abertura atrasada (sinal→abertura > 2 min —
    o sim assume 60s) → ROLLBACK, como o doc Parte X exige ('atraso
    sinal→abertura ≤ ~2 min'). Chama a função standalone
    `_compute_fase3_verdict` diretamente (BD temporária + mocks nas
    funções de análise) — sem o singleton do app.main."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from app.db import DB
    from app.models import Signal, Trade
    from app.state import _compute_fase3_verdict

    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_config("fase3_snapshot", json.dumps({
            "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
        }))
        now = datetime.now(timezone.utc)
        for i in range(10):
            closed = (now + timedelta(hours=i + 1)).isoformat(timespec="seconds")
            # sinal recebido 3 min ANTES da abertura da trade 3 → atraso > 2 min;
            # as restantes abrem no instante do sinal (delay 0)
            if i == 3:
                recv = (datetime.fromisoformat(promoted_at)
                        - timedelta(minutes=3)).isoformat(timespec="seconds")
            else:
                recv = promoted_at
            db.insert_signal(Signal(
                id=f"slow-sig-{i}", source="test", raw_text="", symbol="XAUUSD",
                direction="buy", entry=4400.0, stop_loss=4392.0,
                take_profit_1=4402.0, take_profit_2=4405.0,
                status="pending", received_at=recv,
            ))
            db.insert_trade(Trade(
                id=f"slow-tr-{i}", signal_id=f"slow-sig-{i}", symbol="XAUUSD",
                direction="buy", entry=4400.0, stop_loss=4392.0,
                take_profit_1=4402.0, take_profit_2=4405.0, risk_pct=2.0,
                lots_1=0.01, lots_2=0.01, status="closed", opened_at=promoted_at,
                closed_at=closed, pnl=5.0, exit_reason="tp1",
            ))

        async def _pairs(db_, strategy_id=None):
            return {"pairs": []}

        async def _rvb(db_, strategy_id=None):
            return {"trades": [], "count": 0}

        with patch("app.analysis.paper_vs_real_auto", new=_pairs), \
             patch("app.analysis.real_vs_backtest", new=_rvb):
            v = asyncio.run(_compute_fase3_verdict(db, "auto_escada_3"))
        assert v is not None
        assert v["decision"] == "rollback"
        # PF ≥ 1 (10 vitórias) — o rollback tem de vir do atraso, não do PF
        assert v["pf"] is None or v["pf"] >= 1.0
        assert v["slow_opens"] == 1
        assert "atraso" in v["reason"] and "2 min" in v["reason"]
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_fase3_validation_rollback_pnl_incoherent():
    """Janela de validação com P&L incoerente com o backtest do mesmo sinal
    (sinal oposto — real a perder onde o sim previa ganho) → ROLLBACK, como
    o doc Parte X exige ('P&L coerente com o backtest')."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from app.db import DB
    from app.models import Signal, Trade
    from app.state import _compute_fase3_verdict

    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_config("fase3_snapshot", json.dumps({
            "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
        }))
        now = datetime.now(timezone.utc)
        for i in range(10):
            closed = (now + timedelta(hours=i + 1)).isoformat(timespec="seconds")
            db.insert_signal(Signal(
                id=f"inc-sig-{i}", source="test", raw_text="", symbol="XAUUSD",
                direction="buy", entry=4400.0, stop_loss=4392.0,
                take_profit_1=4402.0, take_profit_2=4405.0,
                status="pending", received_at=promoted_at,
            ))
            # 9 vitórias +10, 1 perda -5 → PF 18 (rollback tem de vir da
            # incoerência, não do PF)
            pnl = -5.0 if i == 3 else 10.0
            db.insert_trade(Trade(
                id=f"inc-tr-{i}", signal_id=f"inc-sig-{i}", symbol="XAUUSD",
                direction="buy", entry=4400.0, stop_loss=4392.0,
                take_profit_1=4402.0, take_profit_2=4405.0, risk_pct=2.0,
                lots_1=0.01, lots_2=0.01, status="closed", opened_at=promoted_at,
                closed_at=closed, pnl=pnl, exit_reason="sl" if i == 3 else "tp1",
            ))

        async def _pairs(db_, strategy_id=None):
            return {"pairs": []}

        async def _rvb(db_, strategy_id=None):
            # trade 3: o sim previa +40 (tp1) mas o real fechou a perder -5 →
            # sinais opostos, |real| ≥ 1 → incoerente
            return {"trades": [
                {"trade_id": f"inc-tr-{i}", "signal_id": f"inc-sig-{i}",
                 "bt_pnl": 40.0 if i == 3 else 10.0,
                 "real_pnl": -5.0 if i == 3 else 10.0}
                for i in range(10)
            ], "count": 10}

        with patch("app.analysis.paper_vs_real_auto", new=_pairs), \
             patch("app.analysis.real_vs_backtest", new=_rvb):
            v = asyncio.run(_compute_fase3_verdict(db, "auto_escada_3"))
        assert v is not None
        assert v["decision"] == "rollback"
        assert v["pf"] == 18.0  # PF alto — a reprovação vem da incoerência
        assert v["pnl_incoherent"] == 1
        assert "incoerente" in v["reason"] or "P&L" in v["reason"]
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_fase3_target_config(client):
    """Alvo da janela da Fase 3 configurável (`fase3_target`, default 20): o
    feed pode baixá-lo para 15 quando o ETA estimar > 6 semanas — o ETA e o
    painel passam a contar até ao novo alvo."""
    from app.main import state

    # default 20
    data = client.get("/api/status").json()
    assert data["config"]["fase3_target"] == 20
    assert data["fase3_eta"]["target"] == 20
    # baixa para 15 (clamp 10–20) — a decisão fica registada no histórico
    # (quem/quando/motivo) para a janela de validação ter contexto
    r = client.post("/api/config", json={"fase3_target": 15,
                                           "fase3_target_reason": "ETA > 6 semanas (botão do feed)"})
    assert r.json().get("ok") is True
    data = client.get("/api/status").json()
    assert data["config"]["fase3_target"] == 15
    assert data["fase3_eta"]["target"] == 15
    log = data["fase3_target_log"]
    assert len(log) >= 1
    last = log[-1]
    assert last["from"] == 20 and last["to"] == 15
    assert "ETA > 6 semanas" in last["reason"] and last["at"]
    # fora do intervalo (10–20) → 422, inalterado
    r = client.post("/api/config", json={"fase3_target": 99})
    assert r.status_code == 422
    data = client.get("/api/status").json()
    assert data["config"]["fase3_target"] == 15
    # volta ao default (também fica no log)
    client.post("/api/config", json={"fase3_target": 20,
                                       "fase3_target_reason": "teste: voltar ao default"})
    assert client.get("/api/status").json()["fase3_target_log"][-1]["to"] == 20


def test_fase3_status_endpoint(client):
    """`/api/analysis/fase3_status`: payload único do painel Fase 3 — ETA,
    snapshot, veredictos, target log, divergences paper↔MT5, real vs
    backtest, DD das contas e auditoria de SL (antes eram 4 chamadas)."""
    r = client.get("/api/analysis/fase3_status")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for k in ("eta", "snapshot", "verdict", "verdicts", "target_log",
              "config", "paper", "paper_vs_real", "real_vs_backtest",
              "sl_log", "dd"):
        assert k in d, f"falta {k} no fase3_status"
    assert d["config"]["fase3_target"] == 20
    assert "auto" in d["paper"] and "ref" in d["paper"]
    assert "auto_escada_3" in d["dd"] and "nova_gestao" in d["dd"]
    assert isinstance(d["paper_vs_real"].get("pairs"), list)
    assert isinstance(d["real_vs_backtest"].get("trades"), list)
    assert isinstance(d["sl_log"].get("trades"), list)
