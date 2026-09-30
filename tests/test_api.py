"""End-to-end API tests.

These exercise the full HTTP surface: ingest (auto-only desde o Item 12), the risk
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


def _dias_atras(dias: int, hora: int = 12, minuto: int = 0) -> str:
    """ISO timestamp `dias` days back from now, in UTC.

    Os endpoints do relatorio (`/api/report`, `/api/analysis/rejections`)
    filtram por `date.today() - days`. Um dado semeado numa data fixa cai
    fora da janela de 30 dias assim que passam 30 dias, e o teste fica
    vermelho sozinho, sem ninguem mexer no codigo. Foi o que aconteceu a
    2026-09-16 com as datas de agosto. Semear relativo e a regra daqui.
    """
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(days=dias)
    return dt.replace(hour=hora, minute=minuto, second=0, microsecond=0).isoformat()


def _dia_atras(dias: int) -> str:
    """`YYYY-MM-DD` de `dias` dias atras, para comparar com `by_day`."""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=dias)).isoformat()


@pytest.fixture
def client():
    """Fresh SQLite/runtime wiring per test.

    The API module used to share one TestClient and one BotState across the
    whole file. Background ticks could then finish an old edit window while a
    later test was preparing its own signal. Rewire the application to a
    throwaway DB for every test so signals, pending management, paper origins
    and in-memory auto state cannot leak between cases.
    """
    import app.main as main_module
    from app.db import DB
    from app.execution.engine import SimEngine
    from app.execution.paper import PaperRace

    path = tempfile.mktemp(suffix=".db")
    db = DB(path)
    import app.config as config_module
    original = {
        "db": main_module.db,
        "state_db": main_module.state.db,
        "notifier_db": main_module.notifier.db,
        "sim": main_module.sim,
        "state_sim": main_module.state.sim,
        "paper": main_module.state.paper,
        "config": main_module.state.config,
        "config_path": config_module.DB_PATH,
        "main_path": main_module.DB_PATH,
    }
    config_module.DB_PATH = path
    main_module.DB_PATH = path
    main_module.db = db
    main_module.state.db = db
    main_module.notifier.db = db
    main_module.state.sim = SimEngine(db, main_module.market)
    main_module.sim = main_module.state.sim
    main_module.state.paper = PaperRace(db, main_module.market)
    main_module.state.config = main_module.state._load_config()
    main_module.state._last_balance_snap = 0.0
    # O snapshot cacheado pertence à BD do teste ANTERIOR (a cache sobrevive
    # ao re-wire do fixture) — sem isto, um /api/status no início do teste
    # servia dados da BD antiga (fechada) e os `next(...)` rebentavam.
    main_module.state._last_snapshot = None
    # A cache de 60s das estatísticas do journal também pertence à BD antiga
    # — re-wire limpa-a para um teste que grave eventos e leia o snapshot
    # ver os números da SUA BD, não os do teste anterior.
    main_module.state._journal_stats_cache = None
    main_module.state._journal_stats_at = 0.0
    # As caches TTL dos blocos informativos (paper/shadow/fase3/window/
    # risk_preview) também pertencem à BD/config do teste anterior.
    main_module.state._ttl_cache = {}
    try:
        with TestClient(app) as c:
            c.post("/api/config", json={"sim_speed": 0})
            yield c
    finally:
        # Restore the app-level wiring before closing the throwaway DB. Other
        # test modules import the same FastAPI singleton; leaving `state.db`
        # pointed at this closed connection makes their fixtures fail with
        # "Cannot operate on a closed database".
        db.close()
        config_module.DB_PATH = original["config_path"]
        main_module.DB_PATH = original["main_path"]
        main_module.db = original["db"]
        main_module.state.db = original["state_db"]
        main_module.notifier.db = original["notifier_db"]
        main_module.sim = original["sim"]
        main_module.state.sim = original["state_sim"]
        main_module.state.paper = original["paper"]
        main_module.state.config = original["config"]


@pytest.fixture(autouse=True)
def _isolated(client):
    """Baseline adicional por teste: limpa qualquer trade aberta e repõe
    config mesmo que o caso altere modo, janela ou kill switch. A fixture
    `client` já fornece SQLite/runtime isolados; este reset mantém os casos
    explícitos compatíveis com o contrato antigo."""
    _close_all_open_trades(client)
    client.post("/api/config", json={
        "edit_window_seconds": 60, "max_positions": 5,
        "risk_pct": 2.0, "strategy": "nova_gestao", "sim_speed": 0,
    })
    client.post("/api/killswitch", json={"enabled": False})
    yield


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Project NexGen" in r.text


def test_static_assets_sent_no_cache(client):
    """O browser não pode servir HTML/JS/CSS antigos do cache (ex.: o slider
    da perda diária a mostrar o valor hardcoded 3.0% em vez do config 4.5%).
    Tudo o que não é /api/ ou /ws deve forçar revalidação."""
    for path in ("/", "/index.html", "/common.js", "/style.css", "/teste"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} devia responder 200"
        cc = (r.headers.get("cache-control") or "").lower()
        assert "no-cache" in cc and "no-store" in cc, \
            f"{path} devia forçar no-cache/no-store, recebi {cc!r}"
    # a API continua normal (JSON não é asset do browser)
    api = client.get("/api/status")
    assert api.status_code == 200
    assert "no-store" not in (api.headers.get("cache-control") or "").lower()


def test_assets_sent_with_cache_busting_version(client):
    """As páginas devem apontar style.css/common.js para URLs `?v=<token>`
    (cache-busting): uma segunda defesa contra caches que ignorem o header
    no-cache. O token é derivado do conteúdo e coerente entre páginas."""
    import re
    tokens = set()
    for path in ("/", "/teste", "/diagnostico", "/help", "/auditoria"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} devia responder 200"
        html = r.text
        assert "style.css?v=" in html and "common.js?v=" in html, \
            f"{path} devia versionar os assets (?v=) para cache-busting"
        m = re.search(r"common\.js\?v=([a-f0-9]+)", html)
        assert m, f"{path} devia ter token hex no common.js"
        tokens.add(m.group(1))
    assert len(tokens) == 1, \
        "o token de cache-busting devia ser estável entre as páginas"


def test_replay_tape_and_snapshots_endpoints_are_read_only(client):
    import app.main as main_module
    r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "replay-test"})
    sid = r.json()["signal"]["id"]
    at = r.json()["signal"]["received_at"]
    main_module.state.db.record_execution_event(
        at, "quote", engine="runtime", symbol="EURUSD",
        payload={"price": 1.0850},
    )
    tape = client.get(f"/api/operations/replay/tape?signal_id={sid}")
    assert tape.status_code == 200
    assert [row["kind"] for row in tape.json()["events"]] == ["signal", "quote"]
    replay = client.post("/api/operations/replay", json={"events": tape.json()["events"]})
    assert replay.status_code == 200
    assert len(replay.json()["snapshots"]) == 2
    steps = client.get(f"/api/operations/replay/snapshots?signal_id={sid}&step=1")
    assert steps.status_code == 200
    assert len(steps.json()["snapshots"]) == 1
    assert steps.json()["snapshots"][0]["kind"] == "quote"


def test_replay_tape_includes_pending_delete_stuck_and_outage_report(client):
    """A fita de replay inclui as marcas `pending_delete_stuck` e as falhas
    de delete (passivas — nunca geram ordens) e o relatório read-only
    /api/operations/replay/outages agrupa os episódios por (trade, ticket):
    escalada após falhas, re-alerta periódico e resolução pelo delete ok."""
    import app.main as main_module
    db = main_module.state.db

    def rec(dt, kind, trade, ticket, **payload):
        db.record_execution_event(
            dt, kind, engine="mt5", symbol="XAUUSD",
            trade_id=trade, signal_id=f"SIG-{trade}",
            payload={"ticket": ticket, **payload},
        )

    # episódio 1 (lane main): 2 falhas + escalada + re-alerta + delete ok
    rec("2026-09-02T09:00:01+00:00", "pending_order_result", "TR1", 1001,
        action="delete", accepted=False, retcode="REMOTE_ERROR",
        comment="worker offline")
    rec("2026-09-02T09:00:02+00:00", "pending_order_result", "TR1", 1001,
        action="delete", accepted=False, retcode="REMOTE_ERROR")
    rec("2026-09-02T09:00:03+00:00", "pending_delete_stuck", "TR1", 1001,
        leg=1, position_id="P1", lane="main", attempts=2, intent_kind="ttl",
        escalated=True, re_alert=False,
        escalated_at="2026-09-02T09:00:03+00:00")
    rec("2026-09-02T09:00:04+00:00", "pending_delete_stuck", "TR1", 1001,
        leg=1, position_id="P1", lane="main", attempts=3, intent_kind="ttl",
        escalated=True, re_alert=True,
        escalated_at="2026-09-02T09:00:03+00:00")
    rec("2026-09-02T09:00:05+00:00", "pending_order_result", "TR1", 1001,
        action="delete", accepted=True, retcode=10009)
    # episódio 2 (lane tp2): falha + escalada SEM resolução no journal
    rec("2026-09-02T09:05:01+00:00", "pending_order_result", "TR2", 2002,
        action="delete", accepted=False, retcode="REMOTE_ERROR")
    rec("2026-09-02T09:05:02+00:00", "pending_delete_stuck", "TR2", 2002,
        leg=2, position_id="P2", lane="tp2", attempts=1, intent_kind="cancel",
        reason="close_all", escalated=True, re_alert=False,
        escalated_at="2026-09-02T09:05:02+00:00")

    # a exportação da fita inclui as duas marcas e as falhas (kind intacto)
    tape = db.execution_tape(since="2026-09-02T09:00:00+00:00",
                             until="2026-09-02T09:06:00+00:00")
    kinds = [row["kind"] for row in tape["events"]]
    assert kinds.count("pending_delete_stuck") == 3
    assert kinds.count("pending_order_result") == 4
    stuck = [r for r in tape["events"] if r["kind"] == "pending_delete_stuck"]
    assert stuck[0]["payload"]["leg"] == 1
    assert stuck[0]["payload"]["lane"] == "main"
    assert stuck[0]["payload"]["attempts"] == 2
    # TR1 tem escalada + re-alerta; TR2 tem só a escalada (perna 2, lane tp2)
    assert stuck[1]["payload"]["leg"] == 1 and stuck[1]["payload"]["re_alert"] is True
    assert stuck[2]["payload"]["leg"] == 2 and stuck[2]["payload"]["re_alert"] is False
    assert stuck[2]["payload"]["lane"] == "tp2"

    # relatório read-only de worker outages
    r = client.get("/api/operations/replay/outages", params={
        "start": "2026-09-02T09:00:00+00:00",
        "until": "2026-09-02T09:06:00+00:00",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    out = r.json()
    s = out["summary"]
    assert s["episodes"] == 2 and s["delete_failures"] == 3
    assert s["stuck_marks"] == 3 and s["re_alerts"] == 1
    assert s["delete_success"] == 1 and s["open_episodes"] == 1
    # timeline cronológica: falha → falha → escalada → re-alerta → delete ok
    tl = [x["kind"] for x in out["timeline"]]
    assert tl.count("delete_failed") == 3 and tl.count("pending_delete_stuck") == 3
    assert tl.count("delete_ok") == 1
    # atribuição de lane/leg às falhas via o ticket da marca da mesma perna
    for row in out["timeline"]:
        if row["ticket"] == 1001:
            assert row["lane"] == "main"
            assert row["leg"] == 1
        if row["ticket"] == 2002:
            assert row["lane"] == "tp2"
            assert row["leg"] == 2
    eps = {e["trade_id"]: e for e in out["episodes"]}
    ep1 = eps["TR1"]
    assert ep1["failed_deletes"] == 2 and ep1["marks"] == 2
    assert ep1["re_alerts"] == 1 and ep1["max_attempts"] == 3
    assert ep1["resolved"] is True and ep1["resolution"] == "delete_ok"
    assert ep1["intent_kind"] == "ttl"
    ep2 = eps["TR2"]
    assert ep2["failed_deletes"] == 1 and ep2["marks"] == 1
    assert ep2["resolved"] is False and ep2["intent_kind"] == "cancel"


def test_audit_exports_archive_and_promotion_gate(client):
    json_export = client.get("/api/analysis/audit-export?days=30&format=json")
    assert json_export.status_code == 200
    assert json_export.headers["content-disposition"].endswith("nexgen-auditoria.json")
    assert json_export.json()["forward_gate"]["policy"]["no_auto_promotion"] is True
    csv_export = client.get("/api/analysis/audit-export?days=30&format=csv")
    assert csv_export.status_code == 200
    assert "section;metric;value;details" in csv_export.text
    archived = client.post("/api/analysis/forward-gate/archive?days=30")
    assert archived.status_code == 200
    assert archived.json()["archive"]["kind"] == "forward_gate"
    archives = client.get("/api/analysis/forward-gate/archives")
    assert archives.status_code == 200
    assert archives.json()["archives"]
    import app.main as main_module
    main_module.state.db.set_config("forward_evidence_started_at", "2026-08-01T00:00:00+00:00")
    blocked = client.post("/api/config", json={"strategy": "auto_escada_3"})
    assert blocked.status_code == 200
    assert blocked.json()["promotion_blocked"] is True
    assert blocked.json()["forward_gate"]["verdict"] == "collect_more_evidence"
    assert client.get("/api/status").json()["config"]["strategy"] == "nova_gestao"


def test_quality_fill_audit_and_forward_gate_endpoints(client):
    quality = client.get("/api/operations/journal-quality")
    assert quality.status_code == 200
    assert quality.json()["ok"] is True
    fills = client.get("/api/analysis/fill-audit?days=30")
    assert fills.status_code == 200
    assert fills.json()["ok"] is True
    gate = client.get("/api/analysis/forward-gate?days=30")
    assert gate.status_code == 200
    assert gate.json()["policy"]["no_auto_promotion"] is True
    page = client.get("/auditoria")
    assert page.status_code == 200
    assert "Qualidade diária do journal" in page.text


def test_status_defaults(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    # Item 12: só existe o caminho auto — o modo manual foi removido.
    assert data["config"]["mode"] == "auto"
    assert data["account"]["balance"] == 10000.0


def test_snapshot_journal_stats_are_cached_and_fresh(client):
    """A agregação do journal (7 dias de `execution_events`, ~1s numa tabela
    com milhões de linhas) não pode correr em CADA snapshot — corre com cache
    de ~60s (`state._journal_stats`). O fixture limpa a cache por teste; aqui
    verifica-se que o snapshot serve os números da BD ATUAL e que a cache
    refresca quando expira (via `_journal_stats_at = 0.0`, sem esperar 60s).
    """
    import app.main as main_module
    from app.execution.engine import _now_iso

    def journal_total():
        return client.get("/api/status").json()["operations"]["execution_journal"]["total"]

    # 1º snapshot: sem eventos → 0 (cache fria, calculada da BD do teste)
    assert journal_total() == 0
    # grava um evento e expira a cache → o snapshot seguinte já o conta
    main_module.state.db.record_execution_event(
        _now_iso(), "quote", engine="runtime", symbol="XAUUSD",
        payload={"price": 4400.0},
    )
    main_module.state._journal_stats_at = 0.0
    assert journal_total() == 1
    # sem expirar a cache, um segundo evento NÃO é contado (cache serve)
    main_module.state.db.record_execution_event(
        _now_iso(), "quote", engine="runtime", symbol="XAUUSD",
        payload={"price": 4401.0},
    )
    assert journal_total() == 1
    # expira de novo → conta os 2
    main_module.state._journal_stats_at = 0.0
    assert journal_total() == 2


def test_ingest_executes_immediately_auto_only(client):
    """Item 12: sem modo manual, um sinal completo executa logo após as
    verificações de risco — não fica pendente à espera de aprovação."""
    r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "executed"
    assert data["signal"]["symbol"] == "EURUSD"


def test_ingest_bad_text_fails_gracefully(client):
    r = client.post("/api/signals", json={"raw_text": "ola mundo"})
    data = r.json()
    assert data["action"] == "parse_error"
    assert data["signal"]["status"] == "failed"


def test_approve_endpoint_removed_and_reject_gate_only(client):
    """Item 12: a rota de aprovação manual deixou de existir e a rota de
    rejeição só aceita descartar sinais bloqueados por guard de entrada ou
    risco de lote mínimo — nunca vetar um sinal que executaria sozinho."""
    import app.main as main_module
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    assert r.json()["action"] == "executed"

    # approve já não tem rota (405 do catch-all de páginas; 404 se não houver)
    assert client.post(f"/api/signals/{sid}/approve").status_code in (404, 405)
    assert not hasattr(main_module.state, "approve")

    # reject num sinal que já executou → recusado pelo estado
    rj = client.post(f"/api/signals/{sid}/reject")
    assert rj.status_code == 200 and "error" in rj.json()
    assert "estado atual" in rj.json()["error"]

    # reject de sinal inexistente → erro claro (rota mantida para gates)
    rj2 = client.post("/api/signals/nao-existe/reject")
    assert rj2.json() == {"error": "sinal não encontrado"}


def test_config_mode_manual_is_rejected(client):
    """Item 12: POST /api/config com mode!=auto é recusado em vez de
    silenciosamente ignorado — nenhum operador pode acreditar que pausou o
    bot quando ele continua em auto."""
    r = client.post("/api/config", json={"mode": "manual"})
    assert r.status_code == 200
    assert "error" in r.json()
    assert "auto" in r.json()["error"]
    assert client.get("/api/status").json()["config"]["mode"] == "auto"
    # mode=auto continua aceite (no-op explícito)
    ok = client.post("/api/config", json={"mode": "auto"})
    assert ok.status_code == 200 and ok.json().get("ok") is True


async def _fake_llm_parse(raw_text, source="telegram", signal_id="",
                          received_at=""):
    """Substituto determinístico do fallback LLM: devolve um sinal válido
    marcado `llm_parsed=True` com o mesmo id que o estado gerou."""
    from app.models import Signal

    return Signal(
        id=signal_id, source=source or "demo", raw_text=raw_text,
        symbol="EURUSD", direction="BUY", entry=1.0850,
        stop_loss=1.0820, take_profit_1=1.0900, take_profit_2=1.0930,
        status="pending", received_at=received_at, llm_parsed=True,
    )


def test_llm_parsed_signal_registered_never_executed(client, monkeypatch):
    """Item 12: um sinal que só a IA consegue parsear fica SÓ registado e
    alertado — nunca executa e nunca fica pendente de aprovação."""
    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr("app.ingest_mixin.try_llm_parse", _fake_llm_parse)

    # caminho imediato (botão demo): registo + alerta, sem trade
    r = client.post("/api/signals", json={
        "raw_text": "compra euro agora 1.0850 stop 1.082 alvo 1.09",
        "source": "demo"})
    data = r.json()
    assert data["action"] == "llm_registered"
    assert data["signal"]["llm_parsed"] is True
    sid = data["signal"]["id"]

    st = client.get("/api/status").json()
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert sig["status"] == "failed"  # registado apenas para auditoria
    assert "IA" in (sig["error"] or "")
    assert not any(t["signal_id"] == sid for t in st["trades"])
    notes = [n for n in st["notifications"] if "parseado por IA" in n["text"]]
    assert notes, "o alerta de registo LLM tem de aparecer no feed"


def test_llm_parsed_telegram_signal_never_waits_for_approval(client, monkeypatch):
    """No caminho Telegram um sinal LLM entra na janela de edição (o grupo
    pode corrigir o texto) mas ao fechá-la é registado como failed/auditoria
    — não executa nem fica pendente."""
    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr("app.ingest_mixin.try_llm_parse", _fake_llm_parse)
    client.post("/api/config", json={"edit_window_seconds": 0})

    r = client.post("/api/signals", json={
        "raw_text": "compra ouro agora 4400 stop 4392 alvo 4410",
        "source": "tg:Golden Moves", "message_id": "9100", "group": "g"})
    # llm_parsed nunca segue o ramo "completo → executa já"
    assert r.json()["action"] == "buffered"
    sid = r.json()["signal"]["id"]

    # ao fechar a janela: registo de auditoria (failed), nunca pendente
    st = _wait_for(client, lambda s: _sig_status(s, sid) == "failed")
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert "IA" in (sig["error"] or "")
    assert not any(t["signal_id"] == sid for t in st["trades"])

    client.post("/api/config", json={"edit_window_seconds": 60})


def test_go_never_executes_llm_parsed_signal(client, monkeypatch):
    """Item 12: um 'Do it now team' (GO) dirigido a um sinal em espera que
    só a IA consegue parsear NÃO o executa — fica registado/alertado."""
    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr("app.ingest_mixin.try_llm_parse", _fake_llm_parse)
    client.post("/api/config", json={"edit_window_seconds": 60})

    # sinal que o regex não parseia (LLM sim) → janela de edição
    r = client.post("/api/signals", json={
        "raw_text": "compra ouro agora 4400 stop 4392 alvo 4410",
        "source": "tg:Golden Moves", "message_id": "9130", "group": "g"})
    assert r.json()["action"] == "buffered"
    sid = r.json()["signal"]["id"]

    # GO do grupo dirigido a esse sinal
    g = client.post("/api/signals", json={
        "raw_text": "Do it now team",
        "source": "tg:Golden Moves", "message_id": "9131", "group": "g",
        "reply_to_msg_id": 9130})
    data = g.json()
    assert data["action"] == "management" and data["kind"] == "go"
    assert data["applied"] is False, "GO não pode executar um sinal LLM"

    # nunca executa — continua em espera (não fica executed nem pendente)
    st = client.get("/api/status").json()
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert sig["status"] == "awaiting_edit"
    assert not any(t["signal_id"] == sid for t in st["trades"])

    # ao fechar a janela (deadline reiniciada via edição com janela 0),
    # auditoria failed
    client.post("/api/config", json={"edit_window_seconds": 0})
    client.post("/api/signals", json={
        "raw_text": "compra ouro agora 4400 stop 4392 alvo 4410",
        "source": "tg:Golden Moves", "message_id": "9130", "group": "g"})
    st = _wait_for(client, lambda s: _sig_status(s, sid) == "failed")
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert "IA" in (sig["error"] or "")
    assert not any(t["signal_id"] == sid for t in st["trades"])
    client.post("/api/config", json={"edit_window_seconds": 60})


def test_revive_llm_parsed_signal_is_registered_only(client, monkeypatch):
    """Item 12: um sinal falhado que o grupo completa só com parse da IA é
    registado para auditoria — o revive não reabre a janela nem executa."""
    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr("app.ingest_mixin.try_llm_parse", _fake_llm_parse)
    client.post("/api/config", json={"edit_window_seconds": 0})

    # primeira versão: incompleta (regex falha; LLM ainda não configurado
    # para esta chamada) → janela fecha logo → failed
    r = client.post("/api/signals", json={
        "raw_text": "I'm buying XAUUSD",
        "source": "tg:Golden Moves", "message_id": "9140", "group": "g"})
    assert r.json()["action"] == "buffered"
    sid = r.json()["signal"]["id"]
    st = _wait_for(client, lambda s: _sig_status(s, sid) == "failed")

    # o grupo edita e a IA agora parseia (regex continua a falhar)
    r2 = client.post("/api/signals", json={
        "raw_text": "compra ouro agora 4400 stop 4392 alvo 4410",
        "source": "tg:Golden Moves", "message_id": "9140", "group": "g"})
    data = r2.json()
    assert data["action"] == "llm_registered", data
    assert data["llm_parsed"] is True

    st = client.get("/api/status").json()
    sig = next(s for s in st["signals"] if s["id"] == sid)
    assert sig["status"] == "failed"
    assert "IA" in (sig["error"] or "")
    assert not any(t["signal_id"] == sid for t in st["trades"])
    # a janela não foi reaberta (sem edit_deadline a aguardar)
    assert not any(s["id"] == sid and s.get("edit_deadline") for s in st["signals"])
    client.post("/api/config", json={"edit_window_seconds": 60})


def test_confirm_never_bypasses_global_risk_switches(client):
    """A confirmação explícita de um sinal bloqueado por guard (zona/lote)
    NUNCA contorna os interruptores globais: kill switch, perda diária e
    máximo de posições voltam a correr por lane em _try_execute."""
    import app.main as main_module
    from app.models import Trade
    st = main_module.state
    # preço partilhado fixo na entrada: a trade do passo 3 (max_positions)
    # não pode fechar por SL/TP num preço deixado por outro teste — senão a
    # vaga abre e o confirm deixa de ser bloqueado
    st.market.prices["EURUSD"] = 1.0850
    st.market._sim.prices["EURUSD"] = 1.0850

    def _fabricate_pending(kind="entry_guard"):
        r = client.post("/api/signals", json={"raw_text": EURUSD})
        sid = r.json()["signal"]["id"]
        st.db.set_signal_pending(sid, "preço fora da zona — confirmação",
                                 kind=kind, meta={"guards": {}})
        return sid

    # 1) kill switch ligado → confirm recusado
    client.post("/api/killswitch", json={"enabled": True})
    sid = _fabricate_pending()
    c = client.post(f"/api/signals/{sid}/confirm")
    assert c.json()["action"] == "blocked"
    assert any("kill switch" in x for x in c.json()["reasons"])
    client.post("/api/killswitch", json={"enabled": False})

    # 2) limite de perda diária atingido → confirm recusado (por lane:
    #    o pré-check do confirm passa realized_today=0; a lane vê o real)
    client.post("/api/config", json={"daily_loss_limit_pct": 1.0})
    from datetime import date as _date
    # `realized_today` usa `date.today()` LOCAL; aqui o relógio local pode
    # estar um dia à frente do UTC (offset +1) e o `closed_at` de hoje-UTC
    # ficaria fora do limiar `LIKE '<dia-local>%'` — grava no dia local para
    # o check de perda diária apanhar a trade (era um flake de fronteira de dia)
    now = _date.today().isoformat() + "T12:00:00"
    st.db.insert_trade(Trade(
        id="dl-confirm-1", signal_id="dl-sig-1", symbol="EURUSD", direction="BUY",
        entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890, take_profit_2=1.0920,
        risk_pct=1.0, lots_1=0.1, lots_2=0.1, status="closed", opened_at=now,
        closed_at=now, pnl=-200.0,
    ))
    sid = _fabricate_pending()
    c = client.post(f"/api/signals/{sid}/confirm")
    data = c.json()
    assert data["action"] == "execution_failed", data
    assert "perda diária" in data.get("error", "")
    client.post("/api/config", json={"daily_loss_limit_pct": 3.0})

    # 3) máximo de posições atingido → confirm NÃO abre trade nova
    _close_all_open_trades(client)
    client.post("/api/config", json={"max_positions": 1})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"  # ocupa a única posição
    sid = _fabricate_pending()
    c = client.post(f"/api/signals/{sid}/confirm")
    data = c.json()
    assert data["action"] == "execution_failed", data
    assert "máximo de posições" in data.get("error", "")
    client.post("/api/config", json={"max_positions": 5})
    _close_all_open_trades(client)


def test_autonomous_zone_deviation_executes_within_cap(client, monkeypatch):
    """Modo autónomo: desvio da zona dentro do cap executa a mercado com
    marca zone_deviation — nunca fica pendente de confirmação humana."""
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4608.0, "ask": 4608.0})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    # zona 4600-4604 (largura 4, limite 20); preço 4608 → fora 4 ≤ cap → executa
    assert data["action"] == "executed", data
    assert data["signal"]["status"] == "executed"
    assert data["trade"]["entry_guard"] == "zone_deviation"


def test_autonomous_zone_deviation_rejects_beyond_cap(client, monkeypatch):
    """Modo autónomo: desvio além do cap é rejeitado automaticamente (nunca
    fica pendente)."""
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4640.0, "ask": 4640.0})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    # preço 4640 → fora 36 > limite 20 → rejeitado automaticamente
    assert data["action"] == "zone_rejected", data
    assert data["signal"]["status"] == "rejected"
    # a notificação mostra a distância e o valor do cap (perseguição recusada)
    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    rej = next((t for t in texts if "rejeitado pelo cap autónomo" in t), None)
    assert rej is not None, texts[-5:]
    assert "36.00 pips fora da zona" in rej, rej
    assert "cap 20.00 pips" in rej, rej
    assert "perseguição além do limite" in rej, rej


def test_autonomous_min_lot_auto_fits_legs(client, monkeypatch):
    """Lote mínimo domina o risco → executa com menos pernas (auto-fit) em
    vez de ficar pendente de confirmação."""
    import app.main as main_module
    st = main_module.state
    # o tick pode já ter gravado peak_balance=10000 antes do monkeypatch;
    # zera o pico e congela o update para o risco efetivo ser determinístico
    monkeypatch.setattr(st, "_update_peak_balance", lambda: None)
    monkeypatch.setattr(st, "_balance_for_lane", lambda lane: 1000.0)
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)
    client.post("/api/config", json={"risk_pct": 1.5})  # 15 € de orçamento
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4598-4602\nTP1 4605\nTP2 4607\n"
                     "TP3 4609\nTP4 4611\nSL 4594")})
    data = r.json()
    assert data["action"] == "executed", data
    trade = data["trade"]
    # auto-fit: nem 1 perna com lote 0.01 cabe nas 5 → executa reduzido
    assert "leg_fit" in (trade.get("entry_guard") or "")
    assert float(trade["lots_5"]) == 0.0


def test_autonomous_min_lot_rejects_when_nothing_fits(client, monkeypatch):
    """Se nem 1 perna couber no orçamento, rejeita automaticamente (nunca
    fica pendente)."""
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_update_peak_balance", lambda: None)
    monkeypatch.setattr(st, "_balance_for_lane", lambda lane: 10.0)
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)
    client.post("/api/config", json={"risk_pct": 1.0})  # 0.10 € de orçamento
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4598-4602\nTP1 4605\nTP2 4607\n"
                     "TP3 4609\nTP4 4611\nSL 4594")})
    data = r.json()
    assert data["action"] == "min_lot_risk_rejected", data
    assert data["signal"]["status"] == "rejected"


def test_reject_message_mostra_bloqueio_real_quando_zona_ganha_ao_lote(client, monkeypatch):
    """A rejeição mostra o bloqueio REAL de cada lane em vez de culpar o lote
    mínimo quando a lane nem chegou ao gate.

    Incidente 04/09 07:32: sell XAUUSD rejeitado com "lote mínimo não cabe
    no risco configurado em main (16.00 > 10.02)" quando a causa real era o
    preço ter fugido da zona entre o guard inicial e a execução — o gate de
    lote é PRÉ-computado (fica no dicionário `exceeded`) mesmo que a lane
    seja bloqueada antes, e o branch antigo mostrava só esse resumo,
    escondendo a zona e calando as outras lanes.
    """
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_update_peak_balance", lambda: None)
    monkeypatch.setattr(st, "_balance_for_lane", lambda lane: 10.0)
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)
    client.post("/api/config", json={"risk_pct": 1.0})  # orçamento 0.10 €
    # Shadow-paper com entrada a mercado: não consome o guard fake abaixo
    st.config["entry_style"] = "market"
    st.db.set_config("entry_style", "market")
    # Guard 1 (pré-loop, decisão zona/cap): preço na zona → segue. Guard 2
    # (loop, execução): preço já fugiu da zona → lane bloqueada antes do gate.
    calls = {"n": 0}

    def fake_guard(signal, lane_id="main"):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "in_zone", "market_price": 4599.0,
                    "distance": 0.0, "reason": "preço dentro da zona",
                    "auto_execute": True, "lane_id": lane_id}
        return {"status": "tolerated", "market_price": 4620.0,
                "distance": 18.0,
                "reason": "preço fora da zona — confirmação manual necessária",
                "auto_execute": False, "lane_id": lane_id}

    monkeypatch.setattr(st, "_entry_guard", fake_guard)
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4598-4602\nTP1 4605\nTP2 4607\n"
                     "TP3 4609\nTP4 4611\nSL 4594")})
    data = r.json()
    assert data["action"] == "min_lot_risk_rejected", data
    err = data["signal"]["error"] or ""
    # a causa real (zona) aparece na mensagem...
    assert "confirmação de zona necessária" in err, err
    assert "preço fora da zona" in err, err
    # ...e o resumo enganador do lote mínimo (lane nunca chegou ao gate) não
    # é apresentado como a causa
    assert "lote mínimo não cabe" not in err, err


def test_autonomous_expires_stale_pending(client):
    """Sinais pendentes antigos são rejeitados pelo tick (modo autónomo —
    nenhuma confirmação humana fica pendente mais de N minutos)."""
    import asyncio
    import app.main as main_module
    st = main_module.state
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    sid = r.json()["signal"]["id"]
    st.db.set_signal_pending(sid, "preço fora da zona", kind="entry_guard", meta={})
    with st.db.tx() as cur:
        cur.execute("UPDATE signals SET received_at = ? WHERE id = ?",
                    ("2026-08-01T00:00:00+00:00", sid))
    asyncio.run(st._expire_stale_pending())
    sig = st.db.get_signal(sid)
    assert sig.status == "rejected"
    assert "modo autónomo" in (sig.error or "")


def test_autonomous_pending_order_fills_when_price_returns(client, monkeypatch):
    """Fase 1: fora da zona dentro do cap com entry_style=pending → a trade
    abre com pernas `pending_fill`; quando o preço volta ao nível da entrada,
    o tick preenche-as (sim) e a gestão normal continua."""
    import asyncio
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4610.0, "ask": 4610.0})
    # fixa o preço partilhado ACIMA da zona: o tick de fundo não pode
    # preencher a pendente durante o POST (um preço deixado por outro teste
    # a 4604 = entrada faria o fill disparar sozinho)
    st.market.prices["XAUUSD"] = 4610.0
    st.market._sim.prices["XAUUSD"] = 4610.0
    client.post("/api/config", json={
        "entry_style": "pending", "entry_order_ttl_hours": 4.0,
    })
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\nSL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    trade = data["trade"]
    assert trade["entry_style"] == "pending"
    pos = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "pending_fill" for p in pos)
    assert trade["entry_guard"] == "zone_deviation"

    # preço longe do nível → continua pendente
    st.market.prices["XAUUSD"] = 4610.0
    st.sim.update()
    assert all(p.status == "pending_fill" for p in st.db.positions_for_trade(trade["id"]))

    # o preço volta ao nível da entrada → preenche no próximo tick
    st.market.prices["XAUUSD"] = float(trade["entry"])
    st.sim.update()
    st.paper.tick(2.0)
    pos2 = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "open" for p in pos2)

    # paper (corrida paralela) também modela pendentes e preenche
    paper_trades = [t for t in st.db.paper_open_trades_by_signal(
        data["signal"]["id"]) if t.get("entry_style") == "pending"]
    assert paper_trades, "o paper deve espelhar o entry_style"
    pp = st.db.paper_positions_for_trade(paper_trades[0]["id"])
    assert all(p.status == "open" for p in pp)


def test_autonomous_pending_order_expires_by_ttl(client, monkeypatch):
    """Fase 1: TTL expira as pernas pendentes que nunca preencheram."""
    from app.execution.clock import ManualClock
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4610.0, "ask": 4610.0})
    # preço partilhado fixo ACIMA da zona (ver teste acima) — a pendente só
    # preenche quando o teste a fizer descer até à entrada
    st.market.prices["XAUUSD"] = 4610.0
    st.market._sim.prices["XAUUSD"] = 4610.0
    client.post("/api/config", json={"entry_style": "pending"})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\nSL 4596")})
    data = r.json()
    trade = data["trade"]
    pos = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "pending_fill" for p in pos)
    # TTL de 60s; o relógio avança 2 min depois do open (ManualClock
    # determinístico — os timestamps ISO são de segundo, um TTL < 1s nunca
    # expira dentro do mesmo segundo em wall clock).
    import time as _time
    st.sim.clock = ManualClock(epoch=_time.time() + 120.0)
    st.market.prices["XAUUSD"] = 4610.0
    st.sim.update(entry_order_ttl=60)
    pos2 = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "closed" and p.exit_reason == "pending_expired"
               for p in pos2)


def test_config_warns_when_daily_limit_below_risk(client):
    """Consistência risco↔dia: mudar o risco com limite diário baixo devolve
    um aviso informativo (e expõe as novas configs no snapshot)."""
    r = client.post("/api/config", json={"risk_pct": 8.0, "daily_loss_limit_pct": 3.0})
    data = r.json()
    assert data["ok"] is True
    assert "limite diário" in data.get("warning", "")
    assert data["config"]["autonomous_mode"] is True
    assert data["config"]["pending_expire_minutes"] == 30
    assert data["config"]["entry_guard_auto_cap"] == 0.0


def test_edits_metric_counts_post_execution_edits(client):
    """Item 12: /api/edits expõe a métrica contínua de edições a sinais que
    executaram sem janela — e o estado regista edited_at nessas edições."""
    import app.main as main_module
    st = main_module.state

    # sinal completo → executa já (sem janela: edit_deadline NULL)
    r = client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves", "message_id": "9150",
        "group": "g"})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

    # o grupo edita depois de executado → edited_at gravado
    # (texto que passa o sinal-hint mas não é gestão nem tp_sl_update)
    client.post("/api/signals", json={
        "raw_text": "buy EURUSD entry 1.0855", "source": "tg:Golden Moves",
        "message_id": "9150", "group": "g"})
    sig = st.db.get_signal(sid)
    assert sig and sig.edited_at, "a edição pós-execução tem de gravar edited_at"

    m = st.db.post_execution_edit_metric()
    assert m["post_execution_total"] >= 1
    assert m["post_execution_edited"] >= 1
    assert m["post_execution_edit_rate"] > 0.0

    # e o endpoint /api/edits devolve a métrica
    ed = client.get("/api/edits?group=g").json()
    assert "post_execution_edit_rate" in ed


def test_four_leg_signal_not_blocked_by_max_positions(client):
    """Regressão do caso real de hoje: sinais com TP1..TP4 (4 pernas) não
    podem ser rejeitados pelo limite de posições — max_positions sobe para 5."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"max_positions": 5})
    assert client.get("/api/status").json()["config"]["max_positions"] == 5
    r = client.post("/api/signals", json={"raw_text": EURUSD4})
    assert r.json()["action"] == "executed"
    trade = r.json()["trade"]
    assert trade["lots_1"] > 0 and trade["lots_2"] > 0 and trade["lots_3"] > 0 and trade["lots_4"] > 0
    assert not trade["lots_5"]
    _close_all_open_trades(client)


def test_trade_open_notification_includes_risk_audit(client):
    """A notificação trade_open reporta o montante real em risco (€/USD)
    e a % de equity usada, para auditoria no feed."""
    from app.main import state
    client.post("/api/config", json={"risk_pct": 1.0})
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
    client.post("/api/config", json={"risk_pct": 2.0})


def test_report_includes_by_day(client):
    # independente do fluxo de execução: grava uma trade fechada diretamente
    from app.main import state
    from app.models import Trade
    now = _dias_atras(2)
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


def test_config_validation_rejects_extreme_risk(client):
    r = client.post("/api/config", json={"risk_pct": 50})
    assert r.status_code == 422


def test_config_daily_loss_limit_updates_response_and_snapshot(client):
    """O limite de perda diária (`daily_loss_limit_pct`) tem de ser alterável
    via POST /api/config: a resposta devolve o novo valor em `config` e o
    `/api/status` reflete a mesma mudança no snapshot (o boot da dashboard lê
    esse campo para sincronizar o slider)."""
    before = client.get("/api/status").json()["config"]["daily_loss_limit_pct"]
    r = client.post("/api/config", json={"daily_loss_limit_pct": 4.5})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config"]["daily_loss_limit_pct"] == 4.5
    after = client.get("/api/status").json()["config"]["daily_loss_limit_pct"]
    assert after == 4.5
    assert after != before, "o valor do limite diário devia ter mudado no snapshot"


def test_config_daily_loss_limit_zero_means_off(client):
    """O slider permite arrastar até 0 (sem limite); a API tem de aceitar 0
    (não 422) e o snapshot devolver esse valor — antigamente o modelo exigia
    ge=0.5 e o slider min=0 ficava inconsistente."""
    r = client.post("/api/config", json={"daily_loss_limit_pct": 0})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["config"]["daily_loss_limit_pct"] == 0
    assert client.get("/api/status").json()["config"]["daily_loss_limit_pct"] == 0


def test_e2e_drag_daily_loss_slider_to_zero_persists_via_api(client):
    """E2E: arrastar o slider da perda diária até 0 (UI → JS → POST /api/config
    → snapshot → reload da página) tem de persistir 0 e refletir-se no
    snapshot e na próxima render.

    A interação do browser é simulada em 4 passos:
      1. A página / chega com o slider min=0 e o oninput inline (no
         index.html) que chama act('/api/config', {daily_loss_limit_pct: v});
         o common.js carrega o valor persistido para o slider no boot.
      2. POST /api/config com 0 — exactamente o que o JS faz depois do
         debounce de 250ms quando o utilizador arrasta o slider até à
         posição mais à esquerda.
      3. GET /api/status devolve config.daily_loss_limit_pct == 0
         (snapshots e tooltips leem daqui).
      4. O common.js faz dl.value = c.daily_loss_limit_pct e troca o
         label para "off" — o cabo de retorno servidor→UI sem o qual o
         slider ficaria sempre no value=3 hardcoded.

    Sem isto, a UI pode aceitar 0 visualmente (slider min=0) mas a
    config ficar a 3.0% (modelo rejeita 0) e o motor tratar 0 como
    'bloqueia assim que o P&L ≤ 0' — três bugs em camadas diferentes.
    """
    import re
    # Passo 1a: o HTML servido tem o slider e o handler inline
    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    slider = re.search(
        r'<input[^>]*id="dailyLossSlider"[^>]*>', html,
    )
    assert slider, "dailyLossSlider desapareceu da dashboard"
    slider_tag = slider.group(0)
    assert re.search(r'\bmin="0"', slider_tag), \
        "o slider devia permitir arrastar até 0 (min=0)"
    assert re.search(r'\btype="range"', slider_tag), \
        "o slider devia ser um input range"
    assert re.search(r'\bstep="0\.5"', slider_tag), \
        "o slider devia ter step 0.5 para granularidade de 0.5%"
    assert re.search(r'id="dailyLossVal"', html), \
        "o label vivo (id=dailyLossVal) tem de existir para o 'off'"
    # o cabo JS→API: o oninput inline faz POST debounced com o valor lido
    assert 'act("/api/config", { daily_loss_limit_pct: v })' in html, \
        "o oninput do slider tem de chamar POST /api/config com o valor"

    # Passo 1b: o common.js carrega o valor persistido no boot (cabo API→UI
    # após reload — sem isto o slider ficaria no value=3 hardcoded mesmo
    # com a config em 0).
    common = client.get(re.search(r'(common\.js\?v=[a-f0-9]+)', html).group(0))
    assert common.status_code == 200, \
        f"o common.js devia ser servido, recebi {common.status_code}"
    common_src = common.text
    assert "dl.value = c.daily_loss_limit_pct" in common_src, \
        "o common.js tem de sincronizar o slider com a config no boot"
    assert 'Number(c.daily_loss_limit_pct) === 0 ? "off"' in common_src, \
        "o common.js tem de mostrar 'off' quando o valor persistido é 0"

    # estado inicial: o snapshot tem o valor default carregado do .env/BD
    before = client.get("/api/status").json()["config"]["daily_loss_limit_pct"]

    # Passo 2: simula o drag até 0 (o JS faz exactamente isto no debounce)
    post = client.post("/api/config", json={"daily_loss_limit_pct": 0})
    assert post.status_code == 200, \
        f"POST /api/config {{daily_loss_limit_pct: 0}} devia aceitar 0, " \
        f"recebi {post.status_code} — o slider min=0 ficaria inconsistente"
    body = post.json()
    assert body["ok"] is True
    assert body["config"]["daily_loss_limit_pct"] == 0, \
        "a resposta do POST devolveu a config actualizada com 0"

    # Passo 3: o snapshot (lido por tooltips, panel Fase 2, header do
    # badge, etc.) reflecte 0
    after = client.get("/api/status").json()["config"]["daily_loss_limit_pct"]
    assert after == 0, \
        f"GET /api/status devia devolver 0, recebi {after}"
    assert after != before or before == 0, \
        "o valor tinha de mudar no snapshot (ou já era 0 no arranque)"

    # Passo 4: recarregar a página devolve o mesmo HTML cache-bustido
    # — o JS volta a fazer dl.value = 0 e label "off" (verificado no
    # Passo 1b pelo contrato do common.js).
    reload = client.get("/")
    assert reload.status_code == 200
    assert 'id="dailyLossSlider"' in reload.text
    # garante que o token ?v= se mantém entre reloads (não muda por
    # estarmos só a mexer na config, não nos assets)
    token_before = re.search(r"common\.js\?v=([a-f0-9]+)", page.text).group(1)
    token_after = re.search(r"common\.js\?v=([a-f0-9]+)", reload.text).group(1)
    assert token_before == token_after, \
        "o token de cache-busting não devia mudar sem editar style.css/common.js"

    # limpeza: repõe um valor não-zero para os testes a seguir não partirem
    # do zero (a config é partilhada entre testes via fixture)
    cleanup = client.post("/api/config", json={"daily_loss_limit_pct": 3.0})
    assert cleanup.status_code == 200
    assert cleanup.json()["config"]["daily_loss_limit_pct"] == 3.0


def test_daily_summary_endpoint(client):
    r = client.post("/api/summary")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "Qualidade journal" in r.json()["text"]
    assert "Gate forward" in r.json()["text"]
    assert "Janela até 28/08" in r.json()["text"]


def test_window_endpoint_and_promotion_payload(client):
    r = client.get("/api/window")
    assert r.status_code == 200
    data = r.json()
    assert data["start_at"] == "2026-08-20T00:00:00+00:00"
    assert data["end_at"] == "2026-08-29T00:00:00+00:00"
    assert data["target_min"] == 10 and data["target_max"] == 20
    assert data["trades_closed"] == 0 and data["trades_open"] == 0
    assert "promotion" in data and data["promotion"]["strategy_id"] == "auto_escada_3"
    assert "forward_gate" in data["promotion"] and "eta" in data["promotion"]
    page = client.get("/")
    assert page.status_code == 200
    assert "Quanto falta para promover a auto_escada_3" in page.text


def test_killswitch_rearms_at_persisted_deadline(client):
    import asyncio
    import app.main as main_module
    # Write the due schedule directly so the background tick cannot consume
    # it between the API calls and the assertion.
    main_module.state.config["kill_switch"] = True
    main_module.state.config["auto_kill"] = True
    main_module.state.config["killswitch_rearm_at"] = "2020-01-01T00:00:00+00:00"
    main_module.state._persist_config(["kill_switch", "auto_kill", "killswitch_rearm_at"])
    asyncio.run(main_module.state._check_killswitch_rearm())
    cfg = client.get("/api/status").json()["config"]
    assert cfg["kill_switch"] is False
    assert cfg["auto_kill"] is False
    assert cfg["killswitch_rearm_at"] == ""
    assert any("Rearme automático" in n["text"] for n in client.get("/api/notifications").json())


def test_summary_correction_updates_target_and_keeps_audit(client):
    original = ("Weekly summary (10/08/26_14/08/26) 5 Trades Sent "
                "4 winning trades 1 Stop loss Win rate: 80%")
    corrected = ("Correction: Weekly summary (10/08/26_14/08/26) 7 Trades Sent "
                 "6 winning trades 1 Stop loss Win rate: 86%")
    first = client.post("/api/signals", json={
        "raw_text": original, "source": "tg:Golden Moves", "message_id": "900", "group": "g",
    })
    assert first.json()["kind"] == "summary"
    fix = client.post("/api/signals", json={
        "raw_text": corrected, "source": "tg:Golden Moves", "message_id": "901", "group": "g",
        "reply_to_msg_id": 900,
    })
    data = fix.json()
    assert data["kind"] == "summary_correction" and data["applied"] is True
    rows = client.get("/api/summaries?limit=10").json()["summaries"]
    target = next(row for row in rows if row["message_id"] == "900")
    correction = next(row for row in rows if row["message_id"] == "901")
    assert target["trades_sent"] == 7 and target["winning"] == 6
    assert target["corrected_at"]
    assert correction["is_correction"] == 1
    assert correction["correction_of_message_id"] == "900"


def test_recount_ingested_and_fill_audit(client):
    """R2: o recount por trade é ground truth — classificado, guardado na
    tabela nova e comparado com o fill real do mesmo dia (entrada/pips)."""
    import app.main as main_module

    r = client.post("/api/signals", json={
        "raw_text": "Signal #1: Sell at 4357. Price went up to 4362, then "
                    "dropped to 4351. +110 pips",
        "source": "tg:Golden Moves", "message_id": "950", "group": "g",
    })
    data = r.json()
    assert data["kind"] == "recount" and data["parsed"] is True
    assert data["audit"]["matched"] is False  # sem trades reais no dia
    assert data["audit"]["probable_cause"]  # causa provável preenchida

    # sem trades reais fechadas, o endpoint devolve o recount + audit
    payload = client.get("/api/recounts").json()
    assert len(payload["recounts"]) == 1
    row = payload["recounts"][0]
    assert row["recount"]["signal_number"] == 1
    assert row["recount"]["direction"] == "sell" and row["recount"]["pips"] == 110.0
    assert row["audit"]["matched"] is False  # sem trades reais no dia
    # candle check sem símbolo conhecido → não inventa plausibilidade
    assert row["audit"]["candle_check"]["checked"] is False
    # weekly com o estado do recount
    weekly = payload["weekly"]
    assert weekly and weekly[0]["total"] == 1 and weekly[0]["no_trade"] == 1

    # recount AO VIVO sem trade correspondente → notificação informativa
    notes = client.get("/api/notifications").json()
    assert any("Recount sem trade correspondente" in n["text"] for n in notes)

    # um segundo POST do mesmo message_id é idempotente (não duplica)
    client.post("/api/signals", json={
        "raw_text": "Signal #1: Sell at 4357. Price went up to 4362, then "
                    "dropped to 4351. +110 pips",
        "source": "tg:Golden Moves", "message_id": "950", "group": "g",
    })
    assert len(client.get("/api/recounts").json()["recounts"]) == 1

    # recap sem a estrutura completa fica como conversa (não é recount)
    chatter = client.post("/api/signals", json={
        "raw_text": "Signal #1 was a great trade today",
        "source": "tg:Golden Moves", "message_id": "951", "group": "g",
    })
    assert chatter.json()["action"] == "ignored"

    # fill audit contra uma trade REAL fechada no mesmo dia: entrada igual,
    # pips compatíveis → sem alerta; entrada divergente → alerta risk.
    from app.models import Position, Trade
    trade = Trade(
        id="tr-recount-1", signal_id="tg-g-1", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="sell", entry=4357.0, stop_loss=4367.0,
        take_profit_1=4347.0, take_profit_2=4337.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.1, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="closed", opened_at="2026-08-20T09:00:00+00:00",
        closed_at="2026-08-20T10:00:00+00:00", pnl=11.0,
    )
    main_module.state.db.insert_trade(trade)
    # posição com close 4355.9 → 1.1 de preço = 110 pips (gold pip 0.01)
    main_module.state.db.insert_position(Position(
        id="pos-recount-1", trade_id="tr-recount-1", leg=1, symbol="XAUUSD",
        direction="sell", lots=0.1, entry=4357.0, stop_loss=4367.0,
        take_profit=4347.0, status="closed", opened_at="2026-08-20T09:00:00+00:00",
        closed_at="2026-08-20T10:00:00+00:00", close_price=4355.9, pnl=11.0,
    ))
    audit = main_module.state._recount_fill_audit(
        {"direction": "sell", "entry": 4357.0, "pips": 110.0},
        "2026-08-20T10:30:00+00:00",
    )
    assert audit["matched"] is True
    assert abs(audit["bot_pips"] - 110.0) < 1.0, audit
    assert audit["alert"] is False, audit

    # entrada real divergente (entrada 4357.3 = 30 pips do fill) → alerta
    divergent = main_module.state._recount_fill_audit(
        {"direction": "sell", "entry": 4357.3, "pips": 110.0},
        "2026-08-20T10:30:00+00:00",
    )
    assert divergent["matched"] is True and divergent["alert"] is True
    assert "entrada" in divergent["reason"]

    # pips divergentes (fill andou 110, grupo reporta 200) → alerta
    pips_bad = main_module.state._recount_fill_audit(
        {"direction": "sell", "entry": 4357.0, "pips": 200.0},
        "2026-08-20T10:30:00+00:00",
    )
    assert pips_bad["matched"] is True and pips_bad["alert"] is True
    assert "pips" in pips_bad["reason"]

    # direção divergente: um BUY no mesmo dia/entrada vs recount SELL → alerta
    buy = Trade(
        id="tr-recount-buy", signal_id="tg-g-2", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="buy", entry=4357.0, stop_loss=4347.0,
        take_profit_1=4367.0, take_profit_2=0.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.1, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="closed", opened_at="2026-08-20T11:00:00+00:00",
        closed_at="2026-08-20T12:00:00+00:00", pnl=11.0,
    )
    main_module.state.db.insert_trade(buy)
    # remove a trade SELL do dia: só sobra o BUY, e o recount SELL tem de
    # detetar a direção divergente (grupo vendeu, o bot comprou)
    main_module.state.db.conn.execute(
        "DELETE FROM positions WHERE trade_id='tr-recount-1'")
    main_module.state.db.conn.execute(
        "DELETE FROM trades WHERE id='tr-recount-1'")
    dir_bad = main_module.state._recount_fill_audit(
        {"direction": "sell", "entry": 4357.0, "pips": 110.0},
        "2026-08-20T10:30:00+00:00",
    )
    assert dir_bad["matched"] is True and dir_bad["alert"] is True
    assert dir_bad["direction_mismatch"] is True
    assert "direção" in dir_bad["reason"]


def test_recount_e2e_full_flow(client, monkeypatch):
    """E2E R2: um recount AO VIVO percorre o fluxo completo — classificação,
    persistência em `trade_recounts`, fill-audit contra as trades do dia,
    candle-check da excursão/reversão, notificação informativa quando não há
    trade e o contrato de renderização da tab Relatório."""
    import app.main as main_module
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    from app.models import Position, Trade

    # O market feed é PARTILHADO entre testes (a fixture não o rewire): fixa
    # XAUUSD na entrada do sinal do passo 0. Sem isto, um preço deixado por
    # outro teste (ex.: 4610 de um teste de zona) fazia a venda 4357/4367
    # bater o SL no primeiro tick e a trade fechava HOJE → o fill-audit
    # marcava bot_traded=True e o teste falhava consoante a ordem. O pin tem
    # de ir também ao simulador interno: o refresh copia `_sim.prices` por
    # cima de `prices` a cada tick (mesmo com sim_speed=0).
    main_module.market.prices["XAUUSD"] = 4357.0
    main_module.market._sim.prices["XAUUSD"] = 4357.0

    # Dia do cenário: 2026-08-15 (6 dias antes de hoje) — NUNCA o dia de
    # execução. O fill-audit do journal (_recount_implausible_evidence)
    # cruza os trades fechados no DIA DE PROCESSAMENTO do evento
    # (_now_iso = hoje), não no dia do recount: com o cenário a correr no
    # próprio dia, a trade real inserida no passo 4 caía na mesma janela e
    # o "bot_traded" ficava True (date-bomb — o teste falhava exatamente
    # quando corrido em 2026-08-21). "2026-08-15" é um dia sem trades de
    # nenhum outro teste da suite (os restantes usam 17/18/19/20/21).
    day = "2026-08-15"
    # candles sintéticos do dia: high 4368, low 4348 — excursão "up até
    # 4362" e reversão "para 4351" são plausíveis; high 4350 seria
    # implausível (o grupo reportaria excursão acima do high real)
    day_ts = int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())
    # candles de 15m do dia inteiro: range 4348–4368 na sessão do sinal
    # (09:00), mas um pico às 22:00 (high 4420) que NÃO pertence à sessão do
    # sinal — o high/low do dia inteiro mistura sessões e o check por janela
    # ±3h à volta da hora do sinal distingue isso
    fake_candles = [
        {"ts": day_ts + i * 900, "open": 4357.0, "high": 4368.0,
         "low": 4348.0, "close": 4355.0}
        for i in range(96)
    ]
    fake_candles.append({"ts": day_ts + 22 * 3600, "open": 4400.0,
                         "high": 4420.0, "low": 4395.0, "close": 4410.0})

    async def fake_history(self_or_symbol, *args, **kwargs):
        return fake_candles

    from app.execution import feed as feed_mod
    monkeypatch.setattr(feed_mod.YahooProvider, "history", fake_history)

    # 0) o sinal do dia que o recount refere (o recount não traz símbolo —
    #    o candle-check associa-o pelo sinal do dia com entrada próxima)
    client.post("/api/signals", json={
        "raw_text": "I'm selling XAUUSD\n\n4357-4361\n\nTP1 4347 TP2 4337 SL 4367",
        "source": "tg:Golden Moves", "message_id": "959", "group": "g",
        "symbol": "XAUUSD",
        "received_at": day + "T09:00:00+00:00",
    })

    # 1) recount AO VIVO (não histórico) com excursão/reversão
    r = client.post("/api/signals", json={
        "raw_text": "Signal #1: Sell at 4357. Price went up to 4362, then "
                    "dropped to 4351. +110 pips",
        "source": "tg:Golden Moves", "message_id": "960", "group": "g",
        "received_at": day + "T10:30:00+00:00",
    })
    data = r.json()
    assert data["action"] == "management" and data["kind"] == "recount"
    assert data["parsed"] is True and data["recount_id"]

    # 2) persistido em trade_recounts (dedup por message_id)
    rows = main_module.state.db.list_trade_recounts(limit=50)
    mine = [row for row in rows if str(row["message_id"]) == "960"]
    assert len(mine) == 1
    assert mine[0]["direction"] == "sell" and mine[0]["entry"] == 4357.0
    assert mine[0]["excursion_price"] == 4362.0
    assert mine[0]["reversal_price"] == 4351.0

    # 3) sem trade real no dia → matched=False + candle-check implausível →
    #    notificação system informativa (não é risk)
    notes = client.get("/api/notifications").json()
    info = [n for n in notes if "Recount sem trade correspondente" in n["text"]]
    assert len(info) == 1 and info[0]["kind"] == "system"
    # candle check correu e a excursão até 4362 é plausível vs high 4368
    rep = client.get("/api/recounts").json()
    mine_rep = [i for i in rep["recounts"]
                if str(i["recount"]["message_id"]) == "960"][0]
    cc = mine_rep["audit"]["candle_check"]
    assert cc["checked"] is True and cc["excursion_ok"] is True
    assert cc["reversal_ok"] is True and cc["plausible"] is True

    # 4) o bot fecha depois uma trade real no dia → fill-audit passa a
    #    matched e a divergência desaparece; semanal atualiza
    trade = Trade(
        id="tr-recount-e2e", signal_id="tg-g-3", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="sell", entry=4357.0, stop_loss=4367.0,
        take_profit_1=4347.0, take_profit_2=4337.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.1, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="closed", opened_at=day + "T09:00:00+00:00",
        closed_at=day + "T10:00:00+00:00", pnl=11.0,
    )
    main_module.state.db.insert_trade(trade)
    main_module.state.db.insert_position(Position(
        id="pos-recount-e2e", trade_id="tr-recount-e2e", leg=1,
        symbol="XAUUSD", direction="sell", lots=0.1, entry=4357.0,
        stop_loss=4367.0, take_profit=4347.0, status="closed",
        opened_at=day + "T09:00:00+00:00", closed_at=day + "T10:00:00+00:00",
        close_price=4355.9, pnl=11.0,
    ))
    rep2 = client.get("/api/recounts").json()
    mine2 = [i for i in rep2["recounts"]
             if str(i["recount"]["message_id"]) == "960"][0]
    assert mine2["audit"]["matched"] is True
    assert mine2["audit"]["alert"] is False
    assert abs(mine2["audit"]["bot_pips"] - 110.0) < 1.0
    # semanal: 1 recount, 1 coerente, 0 sem trade
    weekly = {w["week"]: w for w in rep2["weekly"]}
    week_key = f"{datetime.fromisoformat(day + 'T12:00:00+00:00').isocalendar().year}"
    cur = [w for w in rep2["weekly"] if w["total"] >= 1]
    assert cur and cur[0]["coherent"] >= 1 and cur[0]["no_trade"] == 0

    # 5) recount AO VIVO com excursão ACIMA do high real do dia (4368) →
    #    candle-check implausível → notificação risk + evento no journal
    #    (evidência estruturada para o replay/fill-audit)
    client.post("/api/config", json={"execution_journal_enabled": True})
    r3 = client.post("/api/signals", json={
        "raw_text": "Signal #2: Sell at 4357. Price went up to 4375, then "
                    "dropped to 4351. +110 pips",
        "source": "tg:Golden Moves", "message_id": "961", "group": "g",
        "received_at": day + "T11:30:00+00:00",
    })
    assert r3.json()["kind"] == "recount" and r3.json()["parsed"] is True
    notes2 = client.get("/api/notifications").json()
    impl = [n for n in notes2
            if "Recount implausível" in n["text"] and n["kind"] == "risk"]
    assert len(impl) == 1, notes2
    assert "4375" in impl[0]["text"] and "4368" in impl[0]["text"]
    # o journal (opt-in) gravou o evento estruturado recount_implausible
    events = main_module.state.db.execution_events(kind="recount_implausible")
    assert len(events) == 1, events
    ev = events[0]
    assert ev["signal_id"] == "tg-g-961" and ev["payload"]["signal_number"] == 2
    assert ev["payload"]["excursion_price"] == 4375.0
    assert ev["payload"]["window_plausible"] is False
    assert ev["payload"]["scope"].startswith("janela")
    # o candle-check do endpoint marca-o implausível também
    rep3 = client.get("/api/recounts").json()
    mine3 = [i for i in rep3["recounts"]
             if str(i["recount"]["message_id"]) == "961"][0]
    cc3 = mine3["audit"]["candle_check"]
    assert cc3["checked"] is True and cc3["plausible"] is False
    # implausível na JANELA ±3h do sinal (high 4368 < excursão 4375), mesmo
    # que o dia inteiro o cubra (o pico das 22:00 dá high 4420 — outra
    # sessão, não valida o relato do sinal das 09:00)
    assert cc3["window_plausible"] is False
    assert cc3["window_excursion_ok"] is False
    assert cc3["day_plausible"] is True  # o dia inteiro mistura a sessão
    assert "4368" in str(cc3.get("window_high"))

    # 6) o fill-audit lista o evento do journal como divergência de
    #    evidência (real = relato do grupo, replay = range dos candles)
    fa = client.get("/api/analysis/fill-audit?days=30").json()
    rev = fa.get("recount_evidence") or []
    assert len(rev) == 1 and rev[0]["kind"] == "recount_implausible"
    assert rev[0]["signal_number"] == 2 and rev[0]["excursion_price"] == 4375.0
    assert rev[0]["window_plausible"] is False
    assert rev[0]["bot_traded"] is False  # o bot não negociou o sinal
    assert rev[0]["bot_trade"] is None
    # sem reinício do listener nem kill switch → miss_reason fica None
    assert rev[0].get("miss_reason") is None

    # 6b) um reinício do listener à volta da hora do evento (o journal grava
    #     com _now_iso, não com o received_at do recount) passa a apontar a
    #     causa provável da execução perdida (sinal perdido no arranque)
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    evt_at = _dt.fromisoformat(events[0]["at"])
    main_module.state.db.record_listener_restart(
        "watchdog", started_at=(evt_at - _td(minutes=2)).isoformat())
    fa2 = client.get("/api/analysis/fill-audit?days=30").json()
    rev2 = [e for e in (fa2.get("recount_evidence") or [])
            if e["signal_number"] == 2]
    assert rev2 and "listener reiniciado" in (rev2[0].get("miss_reason") or "")
    # o miss_reason distingue watchdog (heartbeat parado) de reinício manual
    assert rev2[0].get("restart_kind") == "watchdog"
    assert rev2[0].get("restart_at")  # timestamp do reinício
    # um reinício MANUAL (boot.ps1) é classificado à parte
    main_module.state.db.record_listener_restart(
        "boot.ps1", started_at=(evt_at + _td(minutes=1)).isoformat())
    fa3 = client.get("/api/analysis/fill-audit?days=30").json()
    rev3 = [e for e in (fa3.get("recount_evidence") or [])
            if e["signal_number"] == 2]
    assert rev3 and rev3[0].get("restart_kind") == "manual"
    # reinício manual perto do arranque do servidor = provável reboot da VPS
    # (boot.ps1 arranca servidor + listener em bloco)
    main_module.state.db.set_config(
        "server_started_at", (evt_at - _td(minutes=5)).isoformat())
    fa4 = client.get("/api/analysis/fill-audit?days=30").json()
    rev4 = [e for e in (fa4.get("recount_evidence") or [])
            if e["signal_number"] == 2]
    assert rev4 and rev4[0].get("vps_reboot") is True
    assert "reboot da VPS" in (rev4[0].get("miss_reason") or "")

    # 7) re-check com candles frescos: refaz o candle-check sem alterar nada
    rid = mine3["recount"]["id"]
    rc_res = client.post(f"/api/recounts/{rid}/recheck").json()
    assert rc_res["ok"] is True and rc_res["rechecked_at"]
    cc_r = rc_res["audit"]["candle_check"]
    assert cc_r["checked"] is True and cc_r["plausible"] is False
    assert cc_r["window_plausible"] is False
    # veredicto combinado spot MT5 + GC=F: sem terminal o spot não está
    # disponível, o GC=F é a primária — os campos existem e o kind é
    # "implausible" (não há divergência de feed para classificar)
    assert cc_r.get("verdict_kind") == "implausible"
    assert "spot_plausible" in cc_r and "futures_plausible" in cc_r
    assert cc_r.get("feed_divergence") is False
    # o re-check é read-only: a linha guardada não mudou
    rep4 = client.get("/api/recounts").json()
    mine4 = [i for i in rep4["recounts"]
             if str(i["recount"]["message_id"]) == "961"][0]
    assert mine4["recount"]["id"] == rid

    # 7b) re-check em LOTE: candles frescos + offset atual, veredicto
    #     persistido em trade_recounts.candle_check (+ checked_at)
    batch = client.post("/api/recounts/recheck-all").json()
    assert batch["ok"] is True and batch["total"] >= 2
    assert batch["rechecked"] >= 2 and batch["changed"] >= 0
    row_b = [r for r in main_module.state.db.list_trade_recounts(limit=50)
             if str(r["message_id"]) == "961"][0]
    import json as _json
    stored = _json.loads(row_b["candle_check"] or "{}")
    assert stored.get("checked") is True
    assert row_b["checked_at"]  # timestamp do lote

    # 7c) janela adaptativa por símbolo: config por símbolo sobrepõe o
    #     default ±3h do candle-check
    main_module.state.db.set_config(
        "recount_window_hours_by_symbol", _json.dumps({"XAUUSD": 6}))
    rc6 = client.post(f"/api/recounts/{rid}/recheck").json()
    assert rc6["ok"] is True
    cc6 = rc6["audit"]["candle_check"]
    assert cc6.get("window_hours") == 6.0  # a janela passou a ±6h
    assert cc6.get("window_plausible") is not None
    main_module.state.db.set_config(
        "recount_window_hours_by_symbol", _json.dumps({}))  # limpa o teste

    # 7d) veredicto COMBINADO: spot MT5 disponível e plausível + GC=F
    #     implausível → feed_divergence (falso positivo do feed de futuros,
    #     não relato suspeito — o grupo negocia spot)
    async def fake_sources(symbol):
        # spot com high 4378 na janela (cobre a excursão 4375) vs GC=F com
        # high 4368 na janela (implausível) — o pico das 22:00 não conta
        return {"spot": [
            {"ts": day_ts + i * 900, "open": 4357.0, "high": 4378.0,
             "low": 4340.0, "close": 4355.0} for i in range(96)],
            "futures": fake_candles}
    orig_sources = main_module.state._recount_candles
    main_module.state._recount_candles = fake_sources
    try:
        rc7 = client.post(f"/api/recounts/{rid}/recheck").json()
    finally:
        main_module.state._recount_candles = orig_sources
    cc7 = rc7["audit"]["candle_check"]
    assert cc7["checked"] is True and cc7["plausible"] is True
    assert cc7["spot_plausible"] is True and cc7["futures_plausible"] is False
    assert cc7["feed_divergence"] is True
    assert cc7["verdict_kind"] == "feed_divergence"

    # 8) contrato de renderização: a tab Relatório tem o bloco e o loader
    index = (Path("app") / "static" / "index.html").read_text(encoding="utf-8")
    assert "Recounts do grupo vs fills reais" in index
    assert "loadRecounts" in index and "drawRecountsWeekly" in index
    assert "/api/recounts" in index
    assert "probable_cause" in index or "candle_check" in index
    # tooltip do candle-check inclui a janela ±3h do sinal (R2 20/08)
    assert "window_plausible" in index and "janela ±" in index
    # /auditoria: bloco de qualidade, gráfico semanal, re-check e o
    # fill-audit com recounts implausíveis do journal
    audit = (Path("app") / "static" / "auditoria.html").read_text(encoding="utf-8")
    assert "Qualidade dos recounts do grupo" in audit
    assert "recountsWeeklyChart" in audit and "drawAuditRecountsWeekly" in audit
    assert "recheckRecount" in audit and "/api/recounts/" in audit
    assert "recount_evidence" in audit or "recountEvidenceTable" in audit
    assert "pre-real-checklist" in audit or "Pré-conta-real" in audit


def test_pre_real_decision_requires_go_and_full_checklist(client):
    bad = client.post("/api/analysis/pre-real-decision", json={"decision":"NO-GO", "confirmation":"não aprovo"})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    blocked = client.post("/api/analysis/pre-real-decision", json={"decision":"GO", "confirmation":"Confirmo a decisão GO"})
    assert blocked.status_code == 200
    assert blocked.json()["ok"] is False
    assert "checklist" in blocked.json()


def test_recount_candle_check_daily_futures_offset(client, monkeypatch):
    """Fase 5+: o candle-check do GC=F usa o OFFSET DIÁRIO medido nos fills
    reais do dia (GC=F − spot do guard), não um offset fixo. Um relato
    plausível no spot (reversão 4351 dentro do low 4340) fica implausível no
    GC=F sem correção (low 4390 ≫ 4351) e volta a plausível com o offset +50
    do dia — o prémio do futuro não fabrica mais falsos implausíveis."""
    import app.main as main_module
    from app.models import Position, Signal, Trade
    from datetime import datetime
    st = main_module.state
    day = _dia_atras(2)
    day_ts = int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())

    # sinal do dia (para o recount resolver símbolo + hora)
    st.db.insert_signal(Signal(
        id="tg-g-970", source="tg:Golden Moves",
        raw_text="I'm selling XAUUSD", symbol="XAUUSD", direction="sell",
        entry=4357.0, stop_loss=4367.0, take_profit_1=4347.0,
        take_profit_2=4337.0, take_profit_3=0.0, take_profit_4=0.0,
        take_profit_5=0.0, status="executed",
        received_at=day + "T09:00:00+00:00", message_id=None,
        reply_to_msg_id=None, edit_deadline="", edited_at=None,
        lots=None, llm_parsed=False,
        entry_zone_low=4357.0, entry_zone_high=4361.0,
        pending_kind=None, pending_meta=None,
    ))
    # candles sintéticos: spot range 4340–4378 (plausível); GC=F +50 acima
    # (close 4400, range 4390–4428) — sem offset a reversão 4351 é impossível
    spot_candles = [{"ts": day_ts + i * 900, "open": 4357.0, "high": 4378.0,
                     "low": 4340.0, "close": 4355.0} for i in range(96)]
    futures_candles = [{"ts": day_ts + i * 900, "open": 4400.0,
                        "high": 4428.0, "low": 4390.0, "close": 4400.0}
                       for i in range(96)]

    async def fake_sources(symbol):
        return {"spot": spot_candles, "futures": futures_candles}

    orig = st._recount_candles
    st._recount_candles = fake_sources
    try:
        # recount: excursão up 4362 (cobre no high 4378 do spot) e reversão
        # down 4351 (dentro do low 4340 do spot) → plausível no SPOT; no
        # GC=F sem correção é implausível → feed_divergence (offset 0)
        r = client.post("/api/signals", json={
            "raw_text": ("Signal #1: Sell at 4357. Price went up to 4362, "
                         "then dropped to 4351. +110 pips"),
            "source": "tg:Golden Moves", "message_id": "971", "group": "g",
            "received_at": day + "T10:30:00+00:00",
        })
        assert r.json()["action"] == "management"
        rid = r.json()["recount_id"]
        # 1) SEM fills do dia → offset 0 → GC=F implausível (divergência de
        #    feed: spot plausível, futuro não); o lote GRAVA feed_divergence
        import json as _json
        batch1 = client.post("/api/recounts/recheck-all").json()
        assert batch1["ok"] is True
        # o lote re-corre TODOS os recounts (incl. de outros testes na BD
        # partilhada, contra ESTES candles fake) — o que tem de ficar estável
        # é o DESTE teste, não o contador global (resolved pode vir com flips
        # alheios): o nosso não pode mudar de veredicto sem offset
        assert rid not in {f["id"] for f in batch1.get("flips", ())}, batch1
        stored = [x for x in st.db.list_trade_recounts(limit=200)
                  if x["message_id"] == "971"][0]
        cc = _json.loads(stored["candle_check"] or "{}")
        assert cc["futures_shift"] == 0.0, cc
        assert cc["spot_plausible"] is True, cc
        assert cc["futures_plausible"] is False, cc
        assert cc["verdict_kind"] == "feed_divergence", cc

        # 2) com um fill REAL do dia (spot do guard 4350 vs close GC=F 4400)
        #    o offset diário passa a +50 e o GC=F volta a plausível
        st.db.insert_trade(Trade(
            id="tr-offset", signal_id="tg-g-970", strategy_id="nova_gestao",
            symbol="XAUUSD", direction="sell", entry=4357.0,
            stop_loss=4367.0, take_profit_1=4347.0, take_profit_2=4337.0,
            take_profit_3=0.0, take_profit_4=0.0, take_profit_5=0.0,
            risk_pct=1.0, lots_1=0.01, lots_2=0.0, lots_3=0.0,
            lots_4=0.0, lots_5=0.0, status="open",
            opened_at=day + "T09:00:00+00:00", pnl=0.0, exit_reason=None,
            entry_zone_low=4357.0, entry_zone_high=4361.0,
            market_price_at_open=4350.0, entry_distance=0.0,
            entry_guard="{}", execution_mode="mt5-real", lane_id="main",
            entry_style="pending",
        ))
        st._recount_day_offset_cache.clear()
        rc2 = client.post(f"/api/recounts/{rid}/recheck").json()
        cc2 = rc2["audit"]["candle_check"]
        assert cc2["futures_shift"] == 50.0, cc2
        assert cc2["futures_plausible"] is True, cc2
        assert cc2["plausible"] is True, cc2
        assert cc2["feed_divergence"] is False, cc2
        assert cc2["verdict_kind"] == "plausible", cc2

        # 3) o re-check em LOTE (diário) conta o flip RESOLVIDO: o veredicto
        #    persistido passou de feed_divergence para plausível com o offset
        batch2 = client.post("/api/recounts/recheck-all").json()
        assert batch2["ok"] is True
        # o flip resolvido tem de ser O DESTE recount (não basta o contador
        # global vir ≥1 com flips de outros testes)
        assert rid in {f["id"] for f in batch2.get("flips", ())}, batch2
        stored2 = [x for x in st.db.list_trade_recounts(limit=200)
                   if x["message_id"] == "971"][0]
        cc3 = _json.loads(stored2["candle_check"] or "{}")
        assert cc3["verdict_kind"] == "plausible", cc3
        assert cc3["futures_shift"] == 50.0, cc3

        # 4) o historial do lote fica PERSISTIDO com o flip (data, recount,
        #    de → para) e o contador 'resolvidos' do último lote — o
        #    /auditoria mostra a evolução das correções do offset
        batches = client.get("/api/recounts/batches").json()["batches"]
        assert batches, "lote devia estar persistido"
        latest = batches[0]
        assert latest["window"] == "daily", latest
        # o flip persistido é identificado pelo id DO recount (filtro por
        # dia/número podia casar recounts de outros testes na BD partilhada)
        flip = [f for f in (latest["flips"] or []) if f.get("id") == int(rid)]
        assert any(f.get("from") == "feed_divergence" and f.get("to") == "plausible"
                   and f.get("shift") == 50.0 for f in flip), latest
    finally:
        st._recount_candles = orig


def test_recount_batch_weekly_full_history(client, monkeypatch):
    """Fase 5+: o lote diário correr a janela recente e, UMA vez por semana,
    o histórico COMPLETO (days=0) para os veredictos antigos ficarem todos
    corrigidos com o offset medido. O guard semanal (ISO week) limita a uma
    corrida por semana; a janela da corrida fica persistida/notificada."""
    import asyncio
    import app.main as main_module
    st = main_module.state
    # força uma semana ANTERIOR no guard semanal e guard diário limpo → a
    # próxima corrida do lote é a SEMANAL (histórico completo)
    st.config["recount_batch_week_last_run"] = "1900-W01"
    st.config["recount_batch_last_run"] = ""
    st.db.set_config("recount_batch_week_last_run", "1900-W01")
    st.db.set_config("recount_batch_last_run", "")
    calls = []

    async def fake_recheck(days=90, window="daily"):
        calls.append((days, window))
        return {"ok": True, "total": 0, "rechecked": 0, "changed": 0,
                "resolved": 0, "errors": 0}

    monkeypatch.setattr(st, "recheck_all_recounts", fake_recheck)
    asyncio.run(st._check_recount_batch())
    assert calls, "o lote devia ter corrido"
    days, window = calls[-1]
    assert days == 0, (days, window)  # histórico COMPLETO
    assert window == "weekly", (days, window)
    # guard semanal avançado para a semana corrente → a corrida seguinte é diária
    st.config["recount_batch_last_run"] = ""
    asyncio.run(st._check_recount_batch())
    days2, window2 = calls[-1]
    assert days2 == 365 and window2 == "daily", (days2, window2)


def test_cap_effect_passes_guard():
    """Fase 5+: a regra real do guard usada pelo estudo do efeito do cap —
    passa dentro da tolerância (mercado) ou dentro do cap autónomo
    (largura × cap_widths); além disso o guard teria rejeitado."""
    from app.study_cap_effect import passes_guard
    # tolerância 2.0: tudo até 2.0 passa (mercado automático), mesmo com cap 0
    assert passes_guard(0.0, 4.0, 2.0, 0.5) is True
    assert passes_guard(1.5, 4.0, 2.0, 0.5) is True
    # cap 0.5× largura 4 = 2.0 pips: 2.5 corta, 2.0 passa
    assert passes_guard(2.0, 4.0, 2.0, 0.5) is True
    assert passes_guard(2.5, 4.0, 2.0, 0.5) is False
    # cap 0 = limite antigo 5× largura (20 pips): 15 passa, 25 corta
    assert passes_guard(15.0, 4.0, 2.0, 0.0) is True
    assert passes_guard(25.0, 4.0, 2.0, 0.0) is False
    # sem distância medida → passa (o estudo não corta o que não mede)
    assert passes_guard(None, 4.0, 2.0, 0.5) is True

    # decisão honesta: sem fills pós-cap → ainda não há decisão; cap que
    # corta perdedores POUPA (manter); cap que corta ganhadores CUSTA
    from app.study_cap_effect import decide_after
    d0 = decide_after([], 0.5, 2.0, "2026-08-27")
    assert "sem dados pós-cap" in d0, d0
    d_keep = decide_after([
        {"distance": 0.5, "width": 4.0, "pnl": 10.0},
        {"distance": 3.0, "width": 4.0, "pnl": -25.0},
    ], 0.5, 2.0)
    assert "MANTER" in d_keep and "poupa" in d_keep, d_keep
    d_rev = decide_after([
        {"distance": 3.0, "width": 4.0, "pnl": 8.8},
    ], 0.5, 2.0)
    assert "REVERTER" in d_rev and "custa" in d_rev, d_rev


def test_cap_applied_at_recorded(client, monkeypatch):
    """Fase 5+: mudar o entry_guard_auto_cap grava o momento EXATO
    (config `cap_applied_at`) para o study_cap_effect usar como --since;
    um POST com o mesmo valor não re-grava o momento."""
    import app.main as main_module
    st = main_module.state
    st.config.pop("cap_applied_at", None)
    r = client.post("/api/config", json={"entry_guard_auto_cap": 1.0})
    data = r.json()
    assert data["ok"] is True
    first = st.config.get("cap_applied_at")
    assert first, "cap_applied_at devia ter sido gravado"
    # repetir o MESMO valor não limpa nem muda o momento (é uma re-gravação
    # do mesmo valor — o timestamp pode ser o mesmo segundo, o que importa
    # é continuar presente)
    client.post("/api/config", json={"entry_guard_auto_cap": 1.0})
    assert st.config.get("cap_applied_at") == first
    # valor novo mantém o momento gravado (e o estudo continua a poder lê-lo)
    client.post("/api/config", json={"entry_guard_auto_cap": 0.5})
    assert st.config.get("cap_applied_at")
    # persistido na BD (o estudo lê da config table)
    assert st.db.all_config().get("cap_applied_at")


def test_cap_reminder_schedules_when_no_post_cap_fills(client, monkeypatch):
    """Fase 5+: quando o cap autónomo está à espera de dados reais pós-cap há
    mais de `cap_reminder_days`, o tick emite um LEMBRETE no feed para correr
    o `study_cap_effect --days 30` e decidir (fica/aperta/reverte). Sem fills
    pós-cap e sem dados decididos → lembra; com fill pós-cap ou dentro do
    prazo → não lembra; dentro do mesmo dia → dedup (uma vez por guard)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import app.main as main_module
    from app.models import Trade
    st = main_module.state
    now = datetime.now(timezone.utc)
    # cap aplicado há 10 dias (default `cap_reminder_days` = 7) — sem fills
    applied = (now - timedelta(days=10)).isoformat(timespec="seconds")
    st.config["cap_applied_at"] = applied
    st.db.set_config("cap_applied_at", applied)
    st.config["cap_reminder_days"] = 7
    st.config["cap_reminder_last_sent"] = ""
    st.db.set_config("cap_reminder_last_sent", "")

    asyncio.run(st._check_cap_reminder())
    texts = [n["text"] for n in client.get("/api/notifications").json()]
    rem = next((t for t in texts if "study_cap_effect --days 30" in t), None)
    assert rem is not None, texts[-5:]
    assert "Cap autónomo (0.5×) aplicado há 10 dias" in rem, rem

    # dedup: dentro do mesmo dia não repete o lembrete
    before = len(client.get("/api/notifications").json())
    asyncio.run(st._check_cap_reminder())
    assert len(client.get("/api/notifications").json()) == before


def test_cap_reminder_silent_inside_window_or_with_fill(client, monkeypatch):
    """Fase 5+: o lembrete do cap NÃO dispara dentro do prazo (cap aplicado
    há < `cap_reminder_days`) e NÃO dispara quando há um fill real pós-cap
    (há dados — o estudo decide, não o lembrete)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import app.main as main_module
    from app.models import Trade
    st = main_module.state
    now = datetime.now(timezone.utc)

    # 1) dentro do prazo → sem lembrete, mesmo sem fills
    st.config["cap_applied_at"] = (now - timedelta(days=2)).isoformat(timespec="seconds")
    st.config["cap_reminder_days"] = 7
    st.config["cap_reminder_last_sent"] = ""
    asyncio.run(st._check_cap_reminder())
    assert not any("study_cap_effect --days 30" in n["text"]
                   for n in client.get("/api/notifications").json())

    # 2) fora do prazo mas COM um fill real pós-cap → sem lembrete (há dados)
    applied = (now - timedelta(days=20)).isoformat(timespec="seconds")
    st.config["cap_applied_at"] = applied
    opened = (now - timedelta(days=1)).isoformat(timespec="seconds")
    st.db.insert_trade(Trade(
        id="tr-capfill", signal_id="sig-cap", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="buy", entry=4400.0, stop_loss=4390.0,
        take_profit_1=4410.0, take_profit_2=0.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.01, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="open", opened_at=opened, pnl=0.0, exit_reason=None,
        entry_zone_low=4398.0, entry_zone_high=4400.0,
        market_price_at_open=4400.0, entry_distance=0.5, entry_guard="{}",
        execution_mode="mt5-real", lane_id="main", entry_style="pending",
    ))
    st.config["cap_reminder_last_sent"] = ""
    asyncio.run(st._check_cap_reminder())
    assert not any("study_cap_effect --days 30" in n["text"]
                   for n in client.get("/api/notifications").json())


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

    client.post("/api/config", json={"edit_window_seconds": 60})


def test_listener_filter_allows_details_on_known_edit():
    import app.telegram_listener as listener
    listener._forwarded_live_ids.clear()
    listener._forwarded_live_order.clear()
    assert listener.looks_like_signal("I’m buying XAUUSD")
    # A details-only edit has no direction keyword. It must bypass the
    # lightweight filter even after the listener process has restarted.
    original_filter = listener.FILTER
    try:
        listener.FILTER = True
        assert listener._is_known_live_edit("group", "42") is False
        listener._forwarded_live_ids.add(("group", "42"))
        assert listener._is_known_live_edit("group", "42") is True
        assert listener._should_forward(
            "4642.50-4638", "group", "42", is_edit=True
        ) is True
        assert listener._should_forward(
            "4642.50-4638", "group", "42", is_edit=False
        ) is True  # known id also bypasses the filter
        assert listener._should_forward(
            "4642.50-4638", "other", "99", is_edit=False
        ) is False
    finally:
        listener.FILTER = original_filter


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

    client.post("/api/config", json={"edit_window_seconds": 60})


def test_rejected_signal_revived_when_group_reedits(client, monkeypatch):
    """Um sinal REJECTED (guard de zona/cap) volta a abrir a janela quando o
    grupo re-edita a mesma mensagem com conteúdo válido dentro de
    `rejected_revive_minutes` — com alerta claro de que a entrada anterior
    foi recusada. A recusa era sobre o conteúdo/preço ANTIGO; a edição nova
    é intenção fresca do grupo."""
    import app.main as main_module
    st = main_module.state
    client.post("/api/config", json={"edit_window_seconds": 0})

    # 1) sinal completo mas o preço fugiu da zona além do cap -> rejeitado
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4640.0, "ask": 4640.0})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596"),
        "source": "telegram", "message_id": "revive2"})
    data = r.json()
    assert data["action"] == "zone_rejected", data
    assert data["signal"]["status"] == "rejected"
    assert "cap autónomo" in (data["signal"]["error"] or "")

    # a janela de revive aparece na config do snapshot (default 10 min)
    snap = client.get("/api/status").json()
    assert snap["config"]["rejected_revive_minutes"] == 10.0

    # 2) o grupo volta a editar com conteúdo válido (texto diferente) e o
    #    preço já voltou à zona -> janela reaberta, alerta claro
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4602.0, "ask": 4602.0})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4608\nTP2 4609\n"
                     "TP3 4611\nSL 4596"),
        "source": "telegram", "message_id": "revive2"})
    data = r.json()
    assert data["action"] == "revived", data
    assert data["revived_from"] == "rejected", data
    assert data["signal"]["status"] == "awaiting_edit"
    assert data["signal"]["error"] is None  # novo conteúdo reavaliado
    assert data["signal"]["edited_at"]
    # marcador durável do revive: estado de origem + motivo guardado da
    # recusa ANTERIOR + instante — para o histórico o mostrar à primeira
    # vista mesmo depois de o sinal executar.
    assert data["signal"]["revived_from"] == "rejected"
    assert "cap autónomo" in (data["signal"]["revived_reason"] or "")
    assert data["signal"]["revived_at"]

    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    alert = next((t for t in texts if "ANTERIOR foi recusada" in t), None)
    assert alert is not None, texts[-5:]
    assert "cap autónomo" in alert, alert  # o motivo da recusa anterior
    assert "Janela de 0s reaberta" in alert, alert

    # 3) janela fecha (0s) -> o sinal reaberto executa com o conteúdo novo
    stt = _wait_for(client, lambda s: _sig_status(s, "tg-revive2") == "executed")
    sig = next(s for s in stt["signals"] if s["id"] == "tg-revive2")
    assert sig["status"] == "executed"
    assert sig["take_profit_1"] == 4608.0  # conteúdo da edição
    # o marcador sobrevive à execução (o operador vê no histórico que este
    # sinal foi reaberto de uma rejeição, com o motivo)
    assert sig["revived_from"] == "rejected", sig
    assert "cap autónomo" in (sig["revived_reason"] or "")
    assert sig["revived_at"]
    client.post("/api/config", json={"edit_window_seconds": 60})


def test_rejected_signal_edit_after_revive_window_alerted_and_ignored(client, monkeypatch):
    """Re-edição de um sinal rejeitado DEPOIS de `rejected_revive_minutes`
    não reabre nada (não se perseguem entradas recusadas há demasiado tempo)
    — mas o operador recebe um aviso de que o grupo voltou a mexer com
    conteúdo válido e a recusa anterior se mantém."""
    import app.main as main_module
    st = main_module.state
    client.post("/api/config", json={"edit_window_seconds": 0})
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4640.0, "ask": 4640.0})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596"),
        "source": "telegram", "message_id": "revive3"})
    data = r.json()
    assert data["action"] == "zone_rejected", data

    # envelhece o sinal além da janela de revive (10 min)
    with st.db.tx() as cur:
        cur.execute("UPDATE signals SET received_at = ? WHERE id = ?",
                    ("2026-08-01T00:00:00+00:00", "tg-revive3"))

    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4608\nTP2 4609\n"
                     "TP3 4611\nSL 4596"),
        "source": "telegram", "message_id": "revive3"})
    data = r.json()
    # mantém-se rejeitado — a edição tardia não reabre nada
    assert data["action"] == "ignored", data
    assert data["reason"] == "sinal já finalizado (rejected)"
    stt = client.get("/api/status").json()
    sig = next(s for s in stt["signals"] if s["id"] == "tg-revive3")
    assert sig["status"] == "rejected"
    # mas o operador é avisado de que o grupo voltou a mexer
    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    alert = next((t for t in texts if "janela de revive" in t), None)
    assert alert is not None, texts[-5:]
    assert "expirou" in alert and "mantém-se" in alert, alert
    client.post("/api/config", json={"edit_window_seconds": 60})


def test_multiple_groups_same_message_id_do_not_collide(client):
    """The same Telegram message id in two groups produces two independent
    buffered signals; editing one does not touch the other."""
    client.post("/api/config", json={"edit_window_seconds": 30})

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


def test_delete_signal_closes_open_position(client):
    """Deleting a Telegram message marks the signal deleted and closes the
    position opened from it (at market)."""
    # earlier tests may already hold open trades; lift the cap so the auto
    # execution of this test's signal is not blocked by max_positions
    client.post("/api/config", json={"max_positions": 50})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]
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
    assert "nova" in ids and "nova_gestao" in ids and "tp2_protect" in ids
    assert "tp2" not in ids and "nova_gestao_3" not in ids
    assert "tp2_gestao" not in ids and "auto_2" not in ids
    assert "auto_protect_5" not in ids and "auto_escada_3_struggle" not in ids
    # remoção completa (01/09): as retiradas nem aparecem no registo; as
    # únicas ocultas do menu são as referências de backtest nova/auto_5
    # (auto_3/auto_escada_5 continuam no dropdown como referências)
    hidden = {s["id"] for s in data["strategies"] if s["hidden"]}
    assert hidden == {"nova", "auto_5"}
    vis = [s for s in data["strategies"] if not s["hidden"]]
    assert {s["id"] for s in vis} == {"nova_gestao", "nova_gestao_fallback",
                                       "tp2_protect", "auto_3",
                                       "auto_escada_3", "auto_escada_3_hibrida",
                                       "auto_escada_5"}
    assert len([s for s in data["strategies"]]) == 9
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
    # as removidas (auto_protect_5/auto_2) já não devolvem parâmetros
    from app.strategies import get_strategy
    for sid in ("auto_protect_5", "auto_2"):
        try:
            get_strategy(sid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{sid} devia levantar ValueError pós-remoção")
    # a referência (nova_gestao) continua a gerir pelo grupo, sem escada
    ref = sim_params("nova_gestao")
    assert ref["manage"] is True and ref["stair_levels"] is None


def test_removed_strategy_studies_fail_loudly(client):
    """Estudos com ids removidos a 01/09 falham em VOZ ALTA — 400 nos
    endpoints de análise, nunca correm silenciosamente com a principal
    (regra pós-remoção: `get_strategy` levanta ValueError para ids mortos)."""
    paths = [
        "/api/analysis/month?days=7&strategy=tp2",
        "/api/analysis/trace?strategy=auto_protect_5&days=0",
        "/api/analysis/resolution?days=7&strategy=auto_2",
        "/api/analysis/protect_latency?strategy=tp2_gestao",
        "/api/analysis/protect_causal?strategy=nova_gestao_3&days=0",
        "/api/analysis/month?days=7&strategy=auto_escada_3_struggle",
    ]
    for path in paths:
        r = client.get(path)
        assert r.status_code == 400, f"{path} devia devolver 400 (obteve {r.status_code})"
        err = (r.json() or {}).get("error", "")
        assert "removida" in err, f"{path}: erro sem explicação de remoção: {err}"
    # um estudo direto (sim_params) também falha, não faz fallback
    from app.strategies import sim_params
    try:
        sim_params("tp2")
    except ValueError:
        pass
    else:
        raise AssertionError("sim_params('tp2') devia levantar ValueError")


def test_trace_endpoint_empty(client):
    """`/api/analysis/trace`: sem sinais no período devolve found=False
    sem rebentar (o exemplo de fluxo da página de teste fica escondido)."""
    r = client.get("/api/analysis/trace?strategy=nova&since=2030-01-01")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["found"] is False
    assert data["strategy_id"] == "nova"


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
    (tp2_protect -> exit_at_tp=2, protect-only)."""
    client.post("/api/signals", json={
        "raw_text": EURUSD, "source": "tg:Golden Moves",
        "message_id": "7300", "group": "111", "historical": True})
    r = client.get("/api/analysis/month?group=Golden%20Moves&days=7&strategy=tp2_protect")
    assert r.status_code == 200
    data = r.json()
    assert data["strategy_id"] == "tp2_protect"
    assert data["exit_at_tp"] == 2
    assert data["max_legs"] == 2
    assert "equity_curve" in data


def test_config_accepts_strategy_switch(client):
    """Mudar a estratégia ativa via /api/config (validada contra o registo);
    ids removidos (01/09) são rejeitados como inválidos."""
    r = client.post("/api/config", json={"strategy": "tp2_protect"})
    assert r.status_code == 200
    st = client.get("/api/status").json()
    assert st["config"]["strategy"] == "tp2_protect"
    r = client.post("/api/config", json={"strategy": "nao_existe"})
    assert r.status_code == 200
    assert r.json().get("error")
    r = client.post("/api/config", json={"strategy": "tp2"})  # removida
    assert r.status_code == 200 and r.json().get("error")
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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

    st = client.get("/api/status").json()
    accts = (st.get("paper") or {}).get("accounts") or []
    test_accts = {a["strategy_id"]: a for a in accts if not a["is_main"]}
    # corrida focada (01/09): só as estratégias EM VALIDAÇÃO têm card — a
    # `nova` contrafactual abre papel silenciosamente mas não aparece;
    # as referências auto_3/auto_5/auto_escada_5 e as removidas não existem
    for sid in ("nova_gestao_fallback", "tp2_protect", "auto_escada_3",
                "auto_escada_3_hibrida"):
        assert sid in test_accts and test_accts[sid]["balance"] is not None, sid
    for sid in ("nova", "auto_5", "auto_3", "auto_escada_5",
                "tp2", "auto_2", "tp2_gestao", "nova_gestao_3"):
        assert sid not in test_accts, f"{sid} não devia ter card na corrida"
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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

    # ABERTURA: a corrida foca as estratégias em validação + a `nova`
    # (contrafactual silencioso) — todas com o MESMO signal_id (nada órfão)
    for sid_ in ("nova_gestao", "nova", "nova_gestao_fallback", "tp2_protect",
                 "auto_escada_3", "auto_escada_3_hibrida"):
        pt = _paper_trade_for_signal(db, sid_, sid)
        assert pt is not None, f"{sid_} não abriu a trade de papel do sinal"
        assert pt["signal_id"] == sid
        assert pt["status"] == "open"
    # as referências auto_3/auto_5/auto_escada_5 já não abrem papel normal
    # (só backtest/compare + shadow pela própria config); as removidas JÁ
    # NÃO existem no registo
    for sid_ in ("auto_3", "auto_5", "auto_escada_5"):
        assert _paper_trade_for_signal(db, sid_, sid) is None, sid_
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
    import time as _tm
    from app.config import DB_PATH
    from app.db import DB
    from app.main import state as main_state
    # O tick do TestClient grava snapshots de saldo das estratégias paper a
    # cada ~60s (`_record_balance_snapshots`) — congela esse relógio para o
    # DELETE + inserts abaixo serem determinísticos (sem pontos extra a
    # aparecerem na série entre o DELETE e o GET).
    main_state._last_balance_snap = _tm.time() + 3600
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
    assert all(s["strategy_id"] != "auto_5" for s in data["series"])
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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

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
    r = client.post("/api/config", json={"strategy": "tp2_protect"})
    assert r.status_code == 200 and not r.json().get("error")
    st = client.get("/api/status").json()
    promo = [n for n in st["notifications"] if "Estratégia promovida" in n["text"]]
    assert promo, "a promoção tem de aparecer no feed de notificações"
    assert "Sair no TP2 + protect + close_all" in promo[0]["text"]
    assert "fecha tudo no TP2" in promo[0]["text"]
    # o snapshot expõe os parâmetros ativos da execução real
    p = st["config"]["strategy_params"]
    assert p["exit_at_tp"] == 2
    assert p["max_legs"] == 2
    client.post("/api/config", json={"strategy": "nova_gestao"})


def test_snapshot_exposes_strategy_params(client):
    """O snapshot mostra os parâmetros da estratégia ativa da principal
    (exit_at_tp/manage) para o badge da dashboard."""
    client.post("/api/config", json={"strategy": "tp2_protect"})
    st = client.get("/api/status").json()
    p = st["config"]["strategy_params"]
    assert p["exit_at_tp"] == 2
    assert p["max_legs"] == 2
    assert p["manage"] is True
    assert p["nome"] == "Sair no TP2 + protect + close_all"
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
    # This test used to depend on a signal inserted by an earlier test in the
    # module-scoped database. Seed its own input now that fixtures are isolated.
    client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
    r = client.post("/api/simulate", json={"risk_pct": 1.0, "days": 7, "delay_seconds": 60})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["simulated"] >= 1
    assert data["aggregate"]["total"] >= 1


def test_rejection_report_categories_and_replay(client, monkeypatch):
    """Fase 2: 'Porquê sem trade' — rejeições contadas por categoria e replay
    hipotético por sinal parseável (read-only, nunca abre trades)."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.feed import YahooProvider
    from app.models import Signal
    db = DB(DB_PATH)

    base = _dias_atras(2, hora=10)

    def _sig(sid, text, status="rejected", error="", entry=4600.0, sl=4592.0,
             tp1=4605.0):
        return Signal(
            id=sid, source="tg:Golden Moves", raw_text=text,
            symbol="XAUUSD", direction="buy", entry=entry, stop_loss=sl,
            take_profit_1=tp1, take_profit_2=0.0, take_profit_3=0.0,
            take_profit_4=0.0, take_profit_5=0.0, status=status,
            received_at=base, error=error, message_id=None,
            reply_to_msg_id=None, edit_deadline="", edited_at=None, lots=None,
            llm_parsed=False, entry_zone_low=4598.0, entry_zone_high=4602.0,
            pending_kind=None, pending_meta=None,
        )

    db.insert_signal(_sig("rj-zone", "I'm buying XAUUSD 4600-4604",
                          error="preço fora da zona além do cap autónomo"))
    db.insert_signal(_sig("rj-ml", "I'm buying XAUUSD 4600-4604",
                          error="lote mínimo não cabe no risco configurado"))
    db.insert_signal(_sig("rj-ks", "I'm buying XAUUSD 4600-4604",
                          error="kill switch ativo (pausa global)"))
    db.insert_signal(_sig("rj-ex", "I'm buying XAUUSD 4600-4604",
                          error="expirou após 30 min sem confirmação"))
    db.insert_signal(_sig("rj-bk", "I'm buying XAUUSD 4600-4604",
                          status="failed",
                          error="ordem MT5 recusada pelo broker (retcode 10027)"))
    db.insert_signal(_sig("rj-pa", "conversa sem sinal", status="failed",
                          error="entrada não encontrada; TP1 não encontrado",
                          entry=0.0, sl=0.0, tp1=0.0))
    db.insert_signal(_sig("rj-llm", "I'm buying XAUUSD 4600-4604",
                          status="failed",
                          error="parseado por IA (fallback LLM)"))

    async def fake_history(self, symbol, interval="15m", range_="1mo"):
        from datetime import datetime, timedelta, timezone
        start = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
        return [{"ts": start + i * 900, "open": 4600.0, "high": 4610.0,
                 "low": 4590.0, "close": 4602.0} for i in range(20)]

    monkeypatch.setattr(YahooProvider, "history", fake_history)
    r = client.get("/api/analysis/rejections?days=30&balance=10000")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 7, data
    assert data["by_kind"]["zone"] == 1
    assert data["by_kind"]["min_lot"] == 1
    assert data["by_kind"]["global_risk"] == 1
    assert data["by_kind"]["pending_expired"] == 1
    assert data["by_kind"]["broker"] == 1
    assert data["by_kind"]["parse"] == 1
    assert data["by_kind"]["llm"] == 1
    items = {it["id"]: it for it in data["items"]}
    # replay presente para os parseáveis; ausente para o incompleto
    assert items["rj-zone"]["replay"] is not None
    assert items["rj-ml"]["replay"] is not None
    assert items["rj-pa"]["replay"] is None
    # o replay corre mesmo (não pode cair no erro de assinatura do sim)
    assert items["rj-zone"]["replay"]["outcome"] in (
        "win", "loss", "be", "open", "no_data")
    assert isinstance(items["rj-zone"]["replay"]["pnl"], (int, float))
    assert items["rj-ml"]["replay"]["outcome"] in (
        "win", "loss", "be", "open", "no_data")
    # zona vs preço real na hora: com candles (close 4602) e zona 4598-4602,
    # o preço está na zona (in_zone verdadeiro); o desvio é 0.
    zvp = items["rj-zone"]["zone_vs_price"]
    assert zvp is not None, items["rj-zone"]
    assert zvp["in_zone"] is True
    assert zvp["price"] == 4602
    assert zvp["zone_low"] == 4598 and zvp["zone_high"] == 4602
    assert zvp["distance"] == 0


def test_rejection_report_zone_and_orphans(client, monkeypatch):
    """Fase 5: 'porquê sem trade' mostra a zona vs o preço real na hora E as
    pendentes órfãs/expiradas (que saíram na BD com a razão de pendente, mas
    não constam nos sinais rejeitados) com a explicação de porque o preço não
    voltou ao nível da entrada."""
    from app.config import DB_PATH
    from app.db import DB
    from app.execution.feed import YahooProvider
    from app.models import Position, Signal, Trade
    db = DB(DB_PATH)

    dia = _dia_atras(2)
    base = f"{dia}T08:00:00+00:00"
    fecha1 = f"{dia}T12:00:00+00:00"
    fecha2 = f"{dia}T13:00:00+00:00"
    # sinal fora da zona: preço real 4429, entrada 4379 → distância grande
    db.insert_signal(Signal(
        id="zp-far", source="tg:Golden Moves", raw_text="I'm buying XAUUSD",
        symbol="XAUUSD", direction="buy", entry=4379.0, stop_loss=4370.0,
        take_profit_1=4384.0, take_profit_2=0.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, status="rejected",
        received_at=base, error="preço fora da zona além do cap autónomo",
        message_id=None, reply_to_msg_id=None, edit_deadline="", edited_at=None,
        lots=None, llm_parsed=False,
        entry_zone_low=4379.0, entry_zone_high=4379.0,
        pending_kind=None, pending_meta=None,
    ))

    # trade + posições pendentes órfãs/expiradas já fechadas na BD
    trade = Trade(
        id="tr-orphan", signal_id="sig-orphan", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="buy", entry=4317.0, stop_loss=4308.0,
        take_profit_1=4322.0, take_profit_2=0.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.01, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="closed", opened_at=base, closed_at=fecha1,
        pnl=0.0, exit_reason="pending_expired",
        entry_zone_low=4317.0, entry_zone_high=4317.0,
        market_price_at_open=None, entry_distance=10.0, entry_guard="{}",
        execution_mode="mt5", lane_id="main", entry_style="pending",
    )
    db.insert_trade(trade)
    db.insert_position(Position(
        id="pos-orphan-1", trade_id="tr-orphan", leg=1, symbol="XAUUSD",
        direction="buy", lots=0.01, entry=4317.0, stop_loss=4308.0,
        take_profit=4322.0, status="closed", opened_at=base,
        closed_at=fecha1, close_price=None, pnl=0.0,
        exit_reason="pending_expired", exit_msg_id=None, lane_id="main",
        order_ticket=999001,
    ))

    # fill REAL que entrou (perseguição): o sinal zp-far executou apesar de
    # o preço real estar ~50 pips fora da zona — conta nos fills, não nos
    # órfãos (a pendente expirada nunca preencheu e fica de fora)
    db.insert_trade(Trade(
        id="tr-fill", signal_id="zp-far", strategy_id="nova_gestao",
        symbol="XAUUSD", direction="buy", entry=4379.0, stop_loss=4370.0,
        take_profit_1=4384.0, take_profit_2=0.0, take_profit_3=0.0,
        take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
        lots_1=0.01, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
        status="closed", opened_at=base,
        closed_at=fecha2, pnl=-12.5, exit_reason="sl",
        entry_zone_low=4379.0, entry_zone_high=4379.0,
        market_price_at_open=4429.0, entry_distance=50.0, entry_guard="{}",
        execution_mode="mt5-real", lane_id="main", entry_style="market",
    ))

    async def fake_history(self, symbol, interval="15m", range_="1mo"):
        from datetime import datetime, timedelta, timezone
        start = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
        # preço 4429 no arranque e depois sobe — a entrada 4317 nunca é tocada
        return [{"ts": start + i * 900, "open": 4429.0, "high": 4431.0,
                 "low": 4428.0, "close": 4429.5} for i in range(20)]

    monkeypatch.setattr(YahooProvider, "history", fake_history)
    r = client.get("/api/analysis/rejections?days=30&balance=10000")
    assert r.status_code == 200
    data = r.json()
    # zona vs preço
    zvp = {it["id"]: it["zone_vs_price"] for it in data["items"] if it["id"] == "zp-far"}["zp-far"]
    assert zvp is not None
    assert zvp["price"] == 4429.5
    assert zvp["in_zone"] is False
    # preço 4429 vs zona 4379 → faltava ~50
    assert zvp["distance"] >= 45, zvp
    # órfãs: a pendente expirada aparece com a explicação de porque o preço não voltou
    assert data["orphans_total"] >= 1, data["orphans"]
    orphan = next(o for o in data["orphans"] if o["pos_id"] == "pos-orphan-1")
    w = orphan["why"]
    assert w is not None, orphan
    assert w["entry_in_range"] is False  # 4317 fora do range 4428-4431
    assert w["verdict"].startswith("o preço nunca voltou")
    assert w["low"] == 4428.0 and w["high"] == 4431.0
    # fills REAIS que entraram: zona-vs-preço no histórico de trades (a
    # perseguição tr-fill conta; a pendente órfã — nunca preencheu — NÃO)
    fz = data["fills_zone"]
    assert fz["total_fills"] >= 1, fz
    fill = next(f for f in fz["rows"] if f["signal_id"] == "zp-far")
    assert fill["in_zone"] is False
    assert fill["distance"] >= 45, fill
    assert fz["chases"] >= 1
    assert fz["in_zone"] >= 0
    assert all(fr["signal_id"] != "sig-orphan" for fr in fz["rows"])
    assert all(fr["signal_id"] != "zp-far" or fr["execution_mode"] == "mt5-real"
               for fr in fz["rows"])
    # histórico por dia + fonte da distância: o guard (spot) é autoritativo e
    # o dia da colocação entra no by_day
    assert fz["by_day"], fz
    day = next((d for d in fz["by_day"] if d["date"] == dia), None)
    assert day is not None and day["chases"] >= 1, fz["by_day"]
    assert day["max_distance"] >= 45, day
    assert fz["source_guard"] >= 1, fz
    assert fill["distance_source"] == "guard", fill
    # divergência GC=F↔spot: o tr-fill tem spot 4429 (guard) e candle 4429.5
    # (Yahoo) → offset +0.5; fora da zona nas DUAS fontes = perseguição real
    fd = fz["feed_divergence"]
    assert fd["measured"] >= 1, fd
    assert fd["mean_offset"] is not None and abs(fd["mean_offset"] - 0.5) < 0.2, fd
    assert fd["real_persecutions"] >= 1, fd
    assert fd["false_persecutions"] == 0, fd
    assert any(d["date"] == dia for d in fd["by_day"]), fd
    assert fill["market_price"] == 4429.0, fill
    assert abs((fill["feed_divergence"] or 0.0) - 0.5) < 0.2, fill


def test_rejection_report_includes_ignored_and_strategy(client, monkeypatch):
    """Fase 2+: a conversa do grupo (tab 🚫 Ignoradas) vive no mesmo relatório
    como categoria `ignored`, e o endpoint aceita `strategy_id` para o replay
    usar os parâmetros da estratégia escolhida nas páginas de teste."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    db.record_ignored(
        message_id="ig-1", source="tg:Golden Moves", group_name="Golden Moves",
        raw_text="Good morning team ☀️", reason="conversa",
        received_at=_dias_atras(2, hora=9),
    )
    db.record_ignored(
        message_id="ig-2", source="tg:Golden Moves", group_name="Golden Moves",
        raw_text="+50 pips 🔥", reason="conversa",
        received_at=_dias_atras(2, hora=9, minuto=5),
    )
    r = client.get("/api/analysis/rejections?days=30&strategy_id=nova_gestao")
    assert r.status_code == 200
    data = r.json()
    assert data["strategy_id"] == "nova_gestao"
    assert data["by_kind"]["ignored"] >= 2, data["by_kind"]
    ig = [it for it in data["items"] if it["kind"] == "ignored"]
    assert len(ig) >= 2
    assert ig[0]["status"] == "ignored"
    assert all(i["replay"] is None for i in ig)  # conversa não é replayável
    assert any("Good morning" in i["raw_text"] for i in ig)

    # strategy_id muda os parâmetros do replay (max_legs/gestão)
    from app.analysis import rejection_report
    from app.strategies import sim_params
    from app.db import DB as _DB
    import app.main as main_module
    import asyncio
    a = asyncio.run(rejection_report(main_module.state.db, days=30,
                                     strategy_id="auto_escada_3"))
    assert a["strategy_id"] == "auto_escada_3"


def test_orphan_report_notifies_aggregated(client, monkeypatch):
    """Fase 5+: quando o relatório 'porquê sem trade' deteta pendentes
    órfãs/expiradas/canceladas, o tick emite UMA notificação AGREGADA por
    categoria — sem depender de abrir o relatório. Cada perna fechada só
    notifica uma vez (guard com o timestamp da última corrida + throttle)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import app.main as main_module
    from app.models import Position, Trade
    st = main_module.state
    now = datetime.now(timezone.utc)
    closed = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    opened = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    # guard antigo (25h) → a próxima corrida apanha as pendentes de agora
    st.config["orphan_report_last_check"] = (
        now - timedelta(hours=25)).isoformat(timespec="seconds")
    st.db.set_config("orphan_report_last_check",
                     st.config["orphan_report_last_check"])

    def _pos_trade(tid, reason, ticket):
        trade = Trade(
            id=tid, signal_id="sig-" + tid, strategy_id="nova_gestao",
            symbol="XAUUSD", direction="buy", entry=4317.0, stop_loss=4308.0,
            take_profit_1=4322.0, take_profit_2=0.0, take_profit_3=0.0,
            take_profit_4=0.0, take_profit_5=0.0, risk_pct=1.0,
            lots_1=0.01, lots_2=0.0, lots_3=0.0, lots_4=0.0, lots_5=0.0,
            status="closed", opened_at=opened, closed_at=closed, pnl=0.0,
            exit_reason=reason, entry_zone_low=4317.0, entry_zone_high=4317.0,
            market_price_at_open=None, entry_distance=10.0, entry_guard="{}",
            execution_mode="mt5", lane_id="main", entry_style="pending",
        )
        st.db.insert_trade(trade)
        st.db.insert_position(Position(
            id="pos-" + tid, trade_id=tid, leg=1, symbol="XAUUSD",
            direction="buy", lots=0.01, entry=4317.0, stop_loss=4308.0,
            take_profit=4322.0, status="closed", opened_at=opened,
            closed_at=closed, close_price=None, pnl=0.0, exit_reason=reason,
            exit_msg_id=None, lane_id="main", order_ticket=ticket,
        ))

    _pos_trade("tr-or", "pending_orphan", 990001)
    _pos_trade("tr-ex", "pending_expired", 990002)
    _pos_trade("tr-ca", "pending_cancelled", 990003)

    asyncio.run(st._check_orphan_report())
    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    agg = next((t for t in texts if "'Porquê sem trade'" in t), None)
    assert agg is not None, texts[-5:]
    assert "1 órfã(s)" in agg, agg
    assert "1 expirada(s)" in agg, agg
    assert "1 cancelada(s)" in agg, agg

    # corrida imediata: throttle de 1h — sem notificação duplicada
    notes_before = len(notes)
    asyncio.run(st._check_orphan_report())
    notes2 = client.get("/api/notifications").json()
    assert len(notes2) == notes_before


def test_pending_fill_persecution_notifies_feed(client, monkeypatch):
    """Fase 5+: quando uma ordem pendente na zona PREENCHE com o preço longe
    da zona no momento da colocação (perseguição executada), o tick notifica
    no feed com a distância real em pips — medida pelo guard (spot MT5), não
    pelos candles (GC=F diverge)."""
    import asyncio
    import app.main as main_module
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4610.0, "ask": 4610.0})
    st.market.prices["XAUUSD"] = 4610.0
    st.market._sim.prices["XAUUSD"] = 4610.0
    client.post("/api/config", json={
        "entry_style": "pending", "entry_order_ttl_hours": 4.0,
        "entry_guard_tolerance": 0.5})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "SL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    trade = data["trade"]
    # preço 4610 vs zona 4600-4604 → 6 pips fora: ordem pendente na zona
    assert trade.get("entry_style") == "pending", trade
    assert float(trade.get("entry_distance") or 0.0) > 0.5, trade
    pos = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "pending_fill" for p in pos)
    notes_before = len(client.get("/api/notifications").json())
    # o preço volta ao nível da entrada → a pendente preenche (perseguição)
    st.market.prices["XAUUSD"] = 4600.0
    st.market._sim.prices["XAUUSD"] = 4600.0
    asyncio.run(st.tick())
    pos2 = st.db.positions_for_trade(trade["id"])
    assert any(p.status == "open" for p in pos2), [p.status for p in pos2]
    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    pers = next((t for t in texts if "Perseguição executada" in t), None)
    assert pers is not None, texts[-5:]
    assert "6.00 pips fora da zona" in pers, pers
    assert "além da tolerância automática 0.50" in pers, pers


def test_persecution_cap_recommendation():
    """Fase 5+: a recomendação honesta do entry_guard_auto_cap usa a
    distribuição REAL das distâncias dos fills: sem perseguições reais →
    manter o default (nada a cortar); com uma cauda de perseguições → corta
    as piores mantendo ≥ 90% dos fills (incluindo os que entram na zona)."""
    from app.study_persecution_cap import recommend
    # cenário honesto atual (spot): 10 fills, todos dentro/ao lado da zona
    r = recommend([0.0, 0.0, 0.0, 0.4, 0.87, 0.99, 1.27, 0.0, 0.0, 3.64],
                  [True] * 10, [4.0] * 10)
    assert r["chases"] == 0
    assert r["recommended_cap_pips"] is None
    assert "Nada a cortar" in r["verdict"]
    assert r["max_distance"] == 3.64
    # com perseguições reais na cauda: corta as piores sem perder quase nada
    dists = [0.0, 0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 40.0]
    r2 = recommend(dists, [True] * 9 + [False], [4.0] * 10)
    assert r2["chases"] == 1
    assert r2["recommended_cap_pips"] is not None, r2
    rec = next(x for x in r2["rows"]
               if x["cap_pips"] == r2["recommended_cap_pips"])
    assert rec["pct_kept"] >= 90.0, r2
    assert rec["fills_cut"] >= 1  # a perseguição de 40 pips sai


def test_pending_expired_notifies_feed(client, monkeypatch):
    """Fase 4+: quando uma ordem pendente expira por TTL, o tick emite uma
    notificação no feed — não fica só na BD nem no Diagnóstico. O texto
    distingue TTL, cancelada pelo broker e órfã."""
    import asyncio
    import app.main as main_module
    from app.execution.clock import ManualClock
    import time as _time
    st = main_module.state
    monkeypatch.setattr(st, "_current_execution_quote",
                        lambda symbol, lane_id: {"bid": 4610.0, "ask": 4610.0})
    st.market.prices["XAUUSD"] = 4610.0
    st.market._sim.prices["XAUUSD"] = 4610.0
    # TTL de meia hora: o relógio avança 1h → a pendente expira no PRÓXIMO
    # tick (o evento passa pelo state.tick, que é quem notifica)
    client.post("/api/config", json={
        "entry_style": "pending", "entry_order_ttl_hours": 0.5})
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "SL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    trade = data["trade"]
    pos = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "pending_fill" for p in pos)

    st.sim.clock = ManualClock(epoch=_time.time() + 3600.0)
    st.market.prices["XAUUSD"] = 4610.0
    asyncio.run(st.tick())  # o update do tick vê a pendente com 1h de idade
    pos2 = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "closed" and p.exit_reason == "pending_expired"
               for p in pos2)

    notes = client.get("/api/notifications").json()
    texts = [n["text"] for n in notes]
    assert any("Ordem pendente expirou" in t for t in texts), texts[-5:]


def test_e2e_week_autonomous_no_intervention(client, monkeypatch):
    """Fase 4 — E2E: uma semana de operação autónoma sem intervenção humana.

    Promessa central: o utilizador pode ficar 7 dias sem abrir a dashboard —
    cada sinal resolve-se sozinho (executa a mercado em zona, abre ordem
    pendente fora da zona e preenche quando o preço volta, faz auto-fit de
    pernas quando o lote mínimo domina, rejeita além do cap, expira pendentes
    por TTL) ou é rejeitado com a razão gravada. No fim da semana: NENHUM
    sinal fica `pending`/`awaiting_edit` e o relatório "porquê sem trade"
    explica cada rejeição com contador + replay.
    """
    import asyncio
    from datetime import date, timedelta
    import app.main as main_module
    st = main_module.state

    # Configuração da semana: modo autónomo, ordens pendentes na zona, TTL,
    # cap autónomo e limite diário de perda.
    client.post("/api/config", json={
        "autonomous_mode": True,
        "entry_style": "pending",
        "entry_order_ttl_hours": 4.0,
        "entry_guard_auto_cap": 2.0,       # 2× a largura da zona
        "daily_loss_limit_pct": 1.0,
        "risk_pct": 2.0,
        "max_positions": 5,
        "pending_expire_minutes": 30,      # d7 precisa de expirar no tick
        "sim_speed": 0,
    })
    # relógio novo: um teste anterior pode ter deixado um ManualClock na BD
    # partilhada — a semana corre do zero
    from app.execution.clock import SystemClock
    st.sim.clock = SystemClock()
    # risco efetivo determinístico (sem escala dinâmica do pico)
    monkeypatch.setattr(st, "_update_peak_balance", lambda: None)
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)

    def _pin(price):
        """Preço partilhado fixo — o tick de fundo não pode preencher/fechar
        nada por engano entre os passos do teste."""
        st.market.prices["XAUUSD"] = price
        st.market._sim.prices["XAUUSD"] = price
        monkeypatch.setattr(st, "_current_execution_quote",
                            lambda symbol, lane_id: {"bid": price, "ask": price})

    # ---------- DIA 1: dentro da zona → executa a mercado --------------
    _pin(4602.0)
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    assert data["trade"]["entry_style"] == "market"
    d1_sid = data["signal"]["id"]

    # ---------- DIA 2: fora da zona (≤ cap) → pendente; preço volta → preenche
    _pin(4610.0)  # 6 pips acima da zona, cap = 2×4 = 8 → dentro do cap
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    trade = data["trade"]
    assert trade["entry_style"] == "pending"
    assert trade["entry_guard"] == "zone_deviation"
    pos = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "pending_fill" for p in pos)
    # preço volta ao nível da entrada → preenche no próximo tick
    _pin(float(trade["entry"]))
    st.sim.update()
    pos2 = st.db.positions_for_trade(trade["id"])
    assert all(p.status == "open" for p in pos2)

    # ---------- DIA 3: além do cap → rejeitado automaticamente ----------
    _pin(4616.0)  # 12 pips acima da zona > cap 8 → rejeita
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    assert data["action"] == "zone_rejected", data
    assert data["signal"]["status"] == "rejected"
    d3_sid = data["signal"]["id"]

    # ---------- DIA 4: lote mínimo domina → auto-fit de pernas ----------
    monkeypatch.setattr(st, "_balance_for_lane", lambda lane: 1000.0)
    # zera o high-watermark: as trades dos dias 1-2 gravaram um pico ~10144;
    # com o saldo monkeypatched de 1000, esse pico pareceria um drawdown de
    # 91% e cortaria o risco efetivo para o piso 0.1% (o auto-fit não
    # dispararia). Com o pico a zero, o risco efetivo = risco base. O preço é
    # fixado ANTES do reset: o tick de fundo auto-cura o pico a partir da
    # equity atual, e com o preço do dia 3 ainda no ar (4616) a cura gravaria
    # um pico errado (1576 contra equity 904 → drawdown 43% → risco 0.34%).
    _pin(4602.0)
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)
    client.post("/api/config", json={"risk_pct": 1.5})  # 15 € de orçamento
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    assert "leg_fit" in (data["trade"].get("entry_guard") or "")

    # ---------- DIA 5: pendente que nunca preenche → expira por TTL -----
    from app.execution.clock import ManualClock
    import time as _time
    monkeypatch.setattr(st, "_balance_for_lane", lambda lane: 10000.0)
    # restaura o high-watermark realista da conta (equity) para o risco
    # efetivo voltar ao base nos dias seguintes
    st.config["peak_balance"] = 0.0
    st.db.set_config("peak_balance", 0.0)
    client.post("/api/config", json={"risk_pct": 2.0})
    _pin(4610.0)
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    assert data["action"] == "executed", data
    trade5 = data["trade"]
    pos5 = st.db.positions_for_trade(trade5["id"])
    assert all(p.status == "pending_fill" for p in pos5)
    st.sim.clock = ManualClock(epoch=_time.time() + 3600.0)
    st.market.prices["XAUUSD"] = 4610.0
    st.sim.update(entry_order_ttl=60)
    pos5b = st.db.positions_for_trade(trade5["id"])
    assert all(p.status == "closed" and p.exit_reason == "pending_expired"
               for p in pos5b)

    # ---------- DIA 6: perda diária → rejeitado (risco global) ----------
    # uma perda realizada hoje de -200 € (limite diário 1% de 10000 = 100 €)
    from app.models import Trade
    today = date.today().isoformat()
    st.db.insert_trade(Trade(
        id="e2e-loss", signal_id="e2e-loss", symbol="XAUUSD",
        direction="buy", entry=4602.0, stop_loss=4596.0,
        take_profit_1=4607.0, take_profit_2=0.0, risk_pct=2.0,
        lots_1=0.4, lots_2=0.0, status="closed",
        opened_at=f"{today}T00:00:00+00:00",
        closed_at=f"{today}T12:00:00+00:00", pnl=-200.0,
        exit_reason="sl", lane_id="main",
    ))
    # O gate de perda diária lê o realizado de CADA lane (`_realized_today`);
    # trades de lanes fechadas por testes anteriores na BD partilhada faziam
    # variar o valor entre corridas (a lane tp2 ora caía abaixo ora acima do
    # limite → o dia 6 ora rejeitava ora executava). Fixa o realizado para o
    # gate ficar determinístico: -250 € ultrapassa sempre o limite 1%.
    monkeypatch.setattr(st, "_realized_today", lambda lane="": -250.0)
    _pin(4602.0)
    r = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "TP3 4611\nSL 4596")})
    data = r.json()
    # nenhuma lane abriu: a perda diária bloqueia a lane → failed com razão
    assert data["action"] == "execution_failed", data
    assert data["signal"]["status"] == "failed"
    assert "perda diária" in (data["signal"].get("error") or "")
    d6_sid = data["signal"]["id"]

    # ---------- DIA 7: pendente órfão expira; nada fica à espera ---------
    # um sinal `pending` de uma versão antiga (sem humano no ar) é
    # rejeitado pelo tick do modo autónomo
    r7 = client.post("/api/signals", json={
        "raw_text": ("I'm buying XAUUSD\n4600-4604\nTP1 4607\nTP2 4609\n"
                     "SL 4596")})
    d7_sid = r7.json()["signal"]["id"]
    st.db.set_signal_pending(d7_sid, "preço fora da zona",
                             kind="entry_guard", meta={})
    # backdate relativo (2 dias): o sinal continua "antigo" (expira com o
    # pending_expire_minutes de 30) E dentro da janela de 30 dias do
    # relatório de rejeições — uma data fixa (ex. 2026-08-01) flakeva
    # quando o calendário a empurrava para fora da janela
    old_dt = (date.today() - timedelta(days=2)).isoformat()
    with st.db.tx() as cur:
        cur.execute("UPDATE signals SET received_at = ? WHERE id = ?",
                    (f"{old_dt}T00:00:00+00:00", d7_sid))
    asyncio.run(st._expire_stale_pending())
    sig7 = st.db.get_signal(d7_sid)
    assert sig7.status == "rejected"
    assert "expirou" in (sig7.error or "")

    # ---------- Fim da semana: nada pendente; relatório explica ---------
    snap = client.get("/api/status").json()
    terminal = {s["status"] for s in snap["signals"]}
    assert not ({"pending", "awaiting_edit"} & terminal), terminal
    for sid in (d1_sid, d3_sid, d6_sid):
        assert _sig_status(snap, sid) in ("executed", "rejected", "failed")

    # o relatório "porquê sem trade" vê as rejeições com contador + replay
    from app.execution.feed import YahooProvider

    async def fake_history(self, symbol, interval="15m", range_="1mo"):
        return [{"ts": 1755000000 + i * 900, "open": 4602.0,
                 "high": 4610.0, "low": 4596.0, "close": 4602.0}
                for i in range(40)]

    monkeypatch.setattr(YahooProvider, "history", fake_history)
    rep = client.get("/api/analysis/rejections?days=30&balance=10000").json()
    assert rep["total"] >= 2, rep
    assert rep["by_kind"]["zone"] >= 1, rep["by_kind"]
    assert rep["by_kind"]["global_risk"] >= 1, rep["by_kind"]
    assert rep["by_kind"]["pending_expired"] >= 1, rep["by_kind"]
    by_id = {it["id"]: it for it in rep["items"]}
    assert by_id[d3_sid]["kind"] == "zone"
    assert by_id[d6_sid]["kind"] == "global_risk"
    assert by_id[d7_sid]["kind"] == "pending_expired"
    # replay presente nos rejeitados parseáveis (o que TERIA acontecido)
    assert by_id[d3_sid]["replay"] is not None
    assert by_id[d3_sid]["replay"]["outcome"] in (
        "win", "loss", "be", "open", "no_data")


def test_telegram_edit_window_invalid_at_finalize(client):
    """A buffered message that stays invalid after the window is marked failed."""
    client.post("/api/config", json={"edit_window_seconds": 30})
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
    # open a real trade so there is something to manage
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]
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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

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
    r = client.post("/api/signals", json={
        "raw_text": EURUSD, "message_id": "7100", "group": "g"})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

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
    client.post("/api/config", json={"edit_window_seconds": 1})

    def _open(msg_id: str) -> str:
        r = client.post("/api/signals", json={
            "raw_text": EURUSD, "message_id": msg_id, "group": "g"})
        # Item 12: um sinal completo executa logo — o único caminho é auto.
        assert r.json()["action"] == "executed"
        return r.json()["signal"]["id"]

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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"

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
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
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
    # A previous API test may have left an unrelated shadow position open;
    # disable that independent stream so this test isolates the true
    # "no target anywhere" risk path.
    client.post("/api/config", json={"shadow_paper_enabled": False})
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
    # pernas pendentes em retry de delete (watchdog) por lane, read-only
    sp = d.get("stuck_pending", {})
    assert set(sp.keys()) == {"main", "tp2"}
    assert isinstance(sp["main"], list) and isinstance(sp["tp2"], list)
    # bloco do health check externo (Tarefa NexGen Health) para o Diagnóstico
    hc = d.get("healthcheck", {})
    assert isinstance(hc.get("down"), bool)
    assert isinstance(hc.get("consecutive_failures"), int)
    assert isinstance(hc.get("log_tail"), list)
    assert "last_task_result" in hc  # pode ser None (sem schtasks no CI)
    # bloco lane2_worker com dimensionamento read-only (sizing pode ser vazio
    # em dry-run sem conta, mas tem de ser um dict e nunca rebentar o endpoint)
    w2 = d.get("lane2_worker", {})
    assert isinstance(w2.get("sizing"), dict)


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
    # completo → executa logo (único caminho auto desde o Item 12)
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
        client.post("/api/config", json={"risk_pct": 2.0})


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
        r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
        assert r.json()["action"] == "execution_failed"
    finally:
        st.sim = orig

    notifs = st.db.list_notifications(500)
    assert any("AutoTrading DESLIGADO" in n.text for n in notifs), (
        "retcode 10027 devia gerar o alerta imediato de AutoTrading OFF"
    )


def test_phantom_close_raises_distinct_risk_alert(client):
    """Fecho phantom (ordens recusadas sem fill — trade fechada a 0.0 SEM
    exit_reason e SEM flag 10027) tem alerta risc próprio e distinto, em vez
    de só o erro genérico ou a visibilidade no A/B das lanes."""
    from app.main import state as st
    from app.models import Trade

    class _FakeSimPhantom:
        def __init__(self, inner):
            self._inner = inner

        def open_trade(self, signal, risk_pct, balance, **kw):
            # MT5Engine com sent==0: trade fechada, sem razão de saída,
            # retcodes inúteis ("?") — o padrão de 01/09 na lane tp2
            return Trade(
                id="t-fake-phantom", signal_id=signal.id, symbol=signal.symbol,
                direction=signal.direction, entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2, risk_pct=risk_pct,
                lots_1=0.0, lots_2=0.0, status="closed",
                opened_at="2026-09-01T08:31:42+00:00",
                closed_at="2026-09-01T08:31:42+00:00", pnl=0.0,
                error="ordem MT5 recusada pelo broker (retcodes ['?', '?'])",
                retcodes=["?", "?"], auto_trading_off=False,
                exit_reason=None, lane_id="main",
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    orig = st.sim
    st.sim = _FakeSimPhantom(orig)
    try:
        r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
        assert r.json()["action"] == "execution_failed"
    finally:
        st.sim = orig

    notifs = st.db.list_notifications(500)
    phantom = [n for n in notifs
               if "SEM fill" in (n.text or "") and "phantom" in (n.text or "")]
    assert phantom, (
        "fecho phantom devia gerar o alerta distinto de sem-fill (viável "
        "só no feed/A-B antes)"
    )
    assert "Lane main" in phantom[0].text


def test_trades_refusals_lists_recusas_e_phantoms(client):
    """`GET /api/trades/refusals` devolve as recusas do broker com trace
    durável (error/retcodes/auto_trading_off) e também os fechos phantom
    antigos sem erro gravado — mais recentes primeiro."""
    from app.main import state as st
    from app.models import Trade
    from datetime import date
    today = date.today().isoformat()

    # recusa moderna: trace durável gravado (como o _send_open_orders faz)
    st.db.insert_trade(Trade(
        id="ref-10027", signal_id="sig-a", symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
        take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.0,
        status="closed", opened_at=f"{today}T08:00:00+00:00",
        closed_at=f"{today}T08:00:01+00:00", pnl=0.0, exit_reason=None,
        lane_id="tp2",
    ))
    st.db.set_trade_refusal(
        "ref-10027",
        error="AutoTrading desligado no terminal MT5 (retcode 10027)",
        retcodes=[10027, 10027, 10027], auto_trading_off=True,
    )
    # phantom legado (pré-colunas, 01/09): sem error — entra na lista na mesma
    st.db.insert_trade(Trade(
        id="ref-old", signal_id="sig-b", symbol="XAUUSD", direction="sell",
        entry=4377.0, stop_loss=4385.0, take_profit_1=4372.0,
        take_profit_2=4370.0, risk_pct=1.0, lots_1=0.01, lots_2=0.0,
        status="closed", opened_at=f"{today}T07:00:00+00:00",
        closed_at=f"{today}T07:00:00+00:00", pnl=0.0, exit_reason=None,
        lane_id="main",
    ))

    r = client.get("/api/trades/refusals?limit=10").json()
    assert r["ok"] is True
    by_id = {it["id"]: it for it in r["items"]}
    assert "ref-10027" in by_id and "ref-old" in by_id
    newest = by_id["ref-10027"]
    assert newest["retcodes"] == [10027, 10027, 10027]
    assert newest["auto_trading_off"] is True
    assert "AutoTrading desligado" in (newest["error"] or "")
    assert newest["lane_id"] == "tp2"
    old = by_id["ref-old"]
    assert old["error"] is None          # legado sem trace — ainda visível
    assert old["exit_reason"] is None
    # mais recente primeiro
    idx = [it["id"] for it in r["items"]]
    assert idx.index("ref-10027") < idx.index("ref-old")


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


def test_operational_exceptions_bounded_retention(client):
    """`operational_exceptions` tem retenção bounded: o purge apaga linhas
    com mais de 365 dias mas mantém as recentes — o rate-limit por episódio
    já impede spam idêntico, o purge impede a tabela de crescer sem limite."""
    import app.main as main_mod
    db = main_mod.state.db
    db.record_exception(RuntimeError("teste: antiga"),
                        created_at="2020-01-01T00:00:00+00:00")
    db.record_exception(RuntimeError("teste: recente"),
                        created_at="2099-01-01T00:00:00+00:00")
    try:
        purged = db.purge_old_exceptions(365)
        assert purged >= 1, "o purge devia apagar pelo menos a exceção antiga"
        rows = db.exception_rows(limit=500)
        assert not any("antiga" in (r["message"] or "") for r in rows), \
            "exceção com mais de 365 dias devia ser apagada"
        assert any("recente" in (r["message"] or "") for r in rows), \
            "exceção recente não pode ser apagada"
    finally:
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM operational_exceptions WHERE message LIKE 'teste:%'")


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
    # Só a corrida NORMAL (origin='race'): o shadow-paper guarda trades na
    # mesma tabela com origin='shadow' (ex.: auto_escada_5 na config shadow
    # para o gate forward) — sem filtrar, uma shadow trade contamina as
    # asserções sobre o que abre/fecha na corrida.
    rows = (db.paper_trades(strategy_id) if include_closed
            else db.paper_open_trades(strategy_id, origin="race"))
    return next((t for t in rows if t["signal_id"] == signal_id), None)


def test_paper_applies_group_protect(client):
    """A corrida paper aplica a gestão do grupo às estratégias que a honram:
    'protect' fecha a perna 1 e move o SL das restantes — antes as contas
    '+ gestão' comportavam-se como as sem gestão até o preço bater SL/TP."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

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
    # as removidas a 01/09 (nova_gestao_3/tp2_gestao/tp2/auto_2) já não
    # existem no registo — não há papel onde o protect possa agir; as
    # referências auto_3/auto_5/auto_escada_5 também já não abrem papel
    for sid_ in ("nova_gestao_3", "tp2_gestao", "tp2", "auto_2",
                 "auto_3", "auto_5", "auto_escada_5"):
        assert _paper_trade_for_signal(db, sid_, sid) is None, sid_
    # sem gestão: a nova (contrafactual silencioso) e a auto_escada_3/híbrida
    # continuam com a perna 1 aberta
    for sid_ in ("nova", "auto_escada_3", "auto_escada_3_hibrida"):
        assert _legs(sid_)[1].status == "open", sid_


def test_paper_close_all_applies_only_to_managed_visible(client):
    """close_all aplica-se em papel às estratégias que honram o kind
    (nova_gestao e, desde 16/09, tp2_protect); as sem gestão (nova, auto_*)
    continuam abertas. As removidas (01/09) não existem."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]

    r = client.post("/api/signals", json={
        "raw_text": "We're out of this setup", "source": "tg:Golden Moves",
        "message_id": "7201", "group": "g"})
    assert r.json()["kind"] == "close_all"

    def _still_open(sid_: str) -> bool:
        return _paper_trade_for_signal(db, sid_, sid) is not None

    # gestão completa: a principal fechou (já não há trade aberta em papel)
    assert _still_open("nova_gestao") is False
    # sem gestão continua: a nova (contrafactual silencioso) permanece aberta
    assert _still_open("nova") is True
    # as referências auto_3/auto_5/auto_escada_5 já não abrem papel normal;
    # as removidas a 01/09 não existem no registo
    for sid_ in ("auto_3", "auto_5", "auto_escada_5",
                 "nova_gestao_3", "tp2_gestao", "tp2", "auto_2"):
        assert _paper_trade_for_signal(db, sid_, sid) is None, sid_
    # tp2_protect honra close_all (16/09); a auto_escada_3/híbrida não
    assert _still_open("tp2_protect") is False
    assert _still_open("auto_escada_3") is True
    assert _still_open("auto_escada_3_hibrida") is True


def test_exit_reason_recorded_on_close_all(client):
    """Trades/pernas fechadas pela gestão do grupo registam exit_reason +
    exit_msg_id — a dashboard mostra a razão do fecho e o contrafactual."""
    from app.config import DB_PATH
    from app.db import DB
    db = DB(DB_PATH)
    _close_all_open_trades(client)
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]
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
    """Com a estratégia tp2_protect na principal, close_all é agora HONRADO
    (16/09): o grupo diz 'we're out' e a trade fecha ao mercado como na
    nova_gestao — continua a ignorar close_tp1/move_sl."""
    _close_all_open_trades(client)
    client.post("/api/config", json={"edit_window_seconds": 60,
                                     "strategy": "tp2_protect"})
    r = client.post("/api/signals", json={"raw_text": EURUSD})
    assert r.json()["action"] == "executed"
    sid = r.json()["signal"]["id"]
    st = client.get("/api/status").json()
    trade_id = next(t["id"] for t in st["trades"] if t["signal_id"] == sid)

    r = client.post("/api/signals", json={
        "raw_text": "We're out of this setup", "source": "tg:Golden Moves",
        "message_id": "7203", "group": "g"})
    data = r.json()
    assert data["kind"] == "close_all"
    assert data["applied"] is True
    st = client.get("/api/status").json()
    t = next(x for x in st["trades"] if x["id"] == trade_id)
    assert t["status"] == "closed"  # close_all honrado pela tp2_protect
    assert t["exit_reason"] == "close_all"
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
            paper_origin="shadow",  # o gate/ETA medem a origem shadow
        ))
    # escrita DIRETA na BD (sem handler) — invalida as caches do snapshot,
    # como os handlers mutantes fazem; sem isto o bloco fase3_eta (cache
    # TTL de 60s) serviria o valor anterior à inserção
    state._invalidate_snapshot_cache()
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


def test_group_progress_is_audited_without_becoming_a_signal(client):
    entry = client.post("/api/signals", json={
        "raw_text": "BUY XAUUSD\n4584-4580\nSL 4578\nTP1 4586\nTP2 4588",
        "source": "telegram", "message_id": "4256", "group": "g",
    })
    assert entry.status_code == 200
    progress = client.post("/api/signals", json={
        "raw_text": "+130 pips running", "source": "telegram",
        "message_id": "4266", "group": "g",
    })
    assert progress.json()["action"] == "group_audit"
    level = client.post("/api/signals", json={
        "raw_text": "Let's say hi to 4600", "source": "telegram",
        "message_id": "4267", "group": "g",
    })
    assert level.json()["action"] == "group_audit"
    audits = client.get("/api/group-audits").json()["audits"]
    assert any(a["kind"] == "progress_pips" and a["inferred_tp"] == 4597.0 for a in audits)
    assert any(a["kind"] == "level_mention" and a["inferred_tp"] == 4600.0 for a in audits)


def test_duplicate_live_management_is_not_applied_twice(client):
    client.post("/api/config", json={"mode": "auto"})
    signal = client.post("/api/signals", json={
        "raw_text": "BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920",
        "source": "telegram", "message_id": "500", "group": "g",
    })
    assert signal.status_code == 200
    first = client.post("/api/signals", json={
        "raw_text": "TP1 HIT", "source": "telegram", "message_id": "501",
        "group": "g", "reply_to_msg_id": 500,
    })
    second = client.post("/api/signals", json={
        "raw_text": "TP1 HIT", "source": "telegram", "message_id": "501",
        "group": "g", "reply_to_msg_id": 500,
    })
    assert first.json()["action"] == "management"
    assert second.json()["action"] == "duplicate"


# ------------------------------------------------- /segundaria — lane= nos endpoints


def test_report_accepts_lane_filter(client):
    """/api/report?lane=tp2 devolve só as trades fechadas da lane e o
    drawdown referenciado ao saldo da lane; sem lane, `by_lane` é exposto."""
    from app.main import state
    from app.models import Trade
    now = _dias_atras(2)
    state.db.insert_trade(Trade(
        id="lane-report-main", signal_id="lane-sig-m", symbol="EURUSD",
        direction="BUY", entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890,
        take_profit_2=1.0920, risk_pct=1.0, lots_1=0.1, lots_2=0.1,
        status="closed", opened_at=now, closed_at=now, pnl=25.0, lane_id="main"))
    state.db.insert_trade(Trade(
        id="lane-report-tp2", signal_id="lane-sig-t", symbol="EURUSD",
        direction="BUY", entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890,
        take_profit_2=1.0920, risk_pct=1.0, lots_1=0.1, lots_2=0.1,
        status="closed", opened_at=now, closed_at=now, pnl=-10.0, lane_id="tp2"))
    rep = client.get("/api/report?days=30").json()
    assert rep["period"]["lane"] == "all"
    assert rep["trades"]["by_lane"]["main"] == 25.0
    assert rep["trades"]["by_lane"]["tp2"] == -10.0
    # só tp2
    rep2 = client.get("/api/report?days=30&lane=tp2").json()
    assert rep2["period"]["lane"] == "tp2"
    assert rep2["trades"]["closed"] == 1
    assert rep2["trades"]["pnl"] == -10.0
    assert rep2["trades"]["by_lane"] is None
    # lane inválida -> 400
    bad = client.get("/api/report?days=30&lane=xxx")
    assert bad.status_code == 400


def test_trades_and_positions_accept_lane_filter(client):
    r = client.get("/api/trades?lane=main")
    assert r.status_code == 200
    assert all((t.get("lane_id") or "main") == "main" for t in r.json())
    r2 = client.get("/api/trades?lane=tp2")
    assert all((t.get("lane_id") or "main") == "tp2" for t in r2.json())
    assert client.get("/api/trades?lane=zzz").status_code == 400
    # posições (novo endpoint, espelho do snapshot)
    rp = client.get("/api/positions?lane=tp2")
    assert rp.status_code == 200
    assert all((p.get("lane_id") or "main") == "tp2" for p in rp.json())
    assert client.get("/api/positions?lane=zzz").status_code == 400


def test_daily_audit_accepts_lane_filter(client):
    from app.main import state
    from app.models import Trade
    for tid, lane in (("audit-m", "main"), ("audit-t", "tp2")):
        state.db.insert_trade(Trade(
            id=tid, signal_id="audit-sig-" + lane, symbol="EURUSD",
            direction="BUY", entry=1.0850, stop_loss=1.0820,
            take_profit_1=1.0890, take_profit_2=1.0920, risk_pct=1.0,
            lots_1=0.1, lots_2=0.1, status="closed",
            opened_at="2026-08-17T12:00:00", closed_at="2026-08-17T12:05:00",
            pnl=5.0, lane_id=lane))
    all_r = client.get("/api/operations/daily-audit?day=2026-08-17").json()
    assert all_r["trades"]["by_lane"]["main"] >= 5.0
    assert all_r["trades"]["by_lane"]["tp2"] >= 5.0
    tp2_r = client.get("/api/operations/daily-audit?day=2026-08-17&lane=tp2").json()
    assert tp2_r["period"]["lane"] == "tp2"
    assert tp2_r["trades"]["by_lane"] is None
    # só a trade da lane tp2 foi contada
    assert sum(tp2_r["trades"]["by_strategy"].values()) == 1
    assert client.get("/api/operations/daily-audit?day=2026-08-17&lane=bad").status_code == 400


def test_segundaria_serves_dashboard_with_lane_param(client):
    r = client.get("/segundaria")
    assert r.status_code == 200
    html = r.text
    assert "Project NexGen — Dashboard" in html
    # a página injeta ?lane=tp2 no URL antes do JS do index ler o parâmetro
    assert "?lane=tp2" in html


def test_lanes_ab_legs_endpoint(client):
    bad = client.get("/api/lanes/ab/legs")
    assert bad.status_code == 400
    r = client.get("/api/lanes/ab/legs?signal_id=nao-existe-xyz")
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_lanes_ab_exposes_sizing_per_lane(client):
    """O A/B junta o dimensionamento de cada lane (risco base, lote fixo por
    perna e preview) à comparação — o Diagnóstico mostra as duas contas com
    os tamanhos de posição em contexto, sem bloquear o event loop."""
    from app.main import state
    from app.models import Trade
    sid = "ab-sizing-sig"
    for lane, pnl in (("main", 10.0), ("tp2", 12.0)):
        state.db.insert_trade(Trade(
            id="ab-sizing-" + lane, signal_id=sid, symbol="XAUUSD",
            direction="BUY", entry=4400.0, stop_loss=4392.0,
            take_profit_1=4402.0, take_profit_2=4404.0, risk_pct=1.0,
            lots_1=0.1, lots_2=0.1, status="closed",
            opened_at="2026-08-20T10:00:00",
            closed_at="2026-08-20T10:30:00",
            pnl=pnl, exit_reason="tp2", lane_id=lane))
    r = client.get("/api/lanes/ab").json()
    assert r["ok"] and r["n"] == 1
    for lane in ("main", "tp2"):
        assert lane in r and "sizing" in r[lane], lane
        s = r[lane]["sizing"]
        assert s.get("risk_base_pct") is not None
        assert "fixed_lots_per_leg" in s
        assert "lots_per_leg" in s and "lots_total" in s and "n_legs" in s


def test_lane2_smoke_endpoint_guardado_sem_optin(client, monkeypatch):
    """O endpoint do smoke dual-lane existe mas está GUARDADO: sem
    LANE2_SMOKE_ALLOW devolve 409 antes de tocar no broker (e nunca em
    modo sim/sem engine real)."""
    monkeypatch.delenv("LANE2_SMOKE_ALLOW", raising=False)
    r = client.post("/api/lanes/smoke")
    assert r.status_code == 409
    assert "desligado" in r.json()["error"]
    # opt-in ligado mas sem engine real (modo sim) → recusa igual
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    r2 = client.post("/api/lanes/smoke")
    assert r2.status_code == 409
    assert "sem engine" in r2.json()["error"] or "não está em execução REAL" in r2.json()["error"]


def test_notifications_api_exposes_lane(client):
    from app.main import state
    import asyncio
    asyncio.run(state.notifier.send("system", "notif-lane-test-xyz", lane="tp2"))
    n = client.get("/api/notifications").json()
    item = next(x for x in n if x["text"] == "notif-lane-test-xyz")
    assert item["lane"] == "tp2"


def test_config_risk_por_lane_persiste_em_chaves_proprias(client):
    """O POST /api/config aceita `risk_pct_main`/`risk_pct_tp2` e persiste em
    `risk_pct:main`/`risk_pct:tp2`; o `risk_pct` legado sincroniza sempre com
    a base da principal (nunca divergem)."""
    from app.main import state
    # legado -> sincroniza risk_pct:main
    r = client.post("/api/config", json={"risk_pct": 1.5})
    assert r.status_code == 200 and not r.json().get("error")
    assert state.config["risk_pct"] == 1.5
    assert state.config["risk_pct:main"] == 1.5
    # base própria da 2ª conta
    r = client.post("/api/config", json={"risk_pct_tp2": 2.0})
    assert r.status_code == 200 and not r.json().get("error")
    assert state.config["risk_pct:tp2"] == 2.0
    assert state.config["risk_pct"] == 1.5  # a principal não muda
    # risk_pct_main também atua sobre a principal (e sincroniza o legado)
    r = client.post("/api/config", json={"risk_pct_main": 0.8})
    assert r.status_code == 200 and not r.json().get("error")
    assert state.config["risk_pct:main"] == 0.8
    assert state.config["risk_pct"] == 0.8
    assert state.config["risk_pct:tp2"] == 2.0  # a 2ª não muda
    # validação de limites
    assert client.post("/api/config", json={"risk_pct_tp2": 50}).status_code == 422
    # a base da 2ª conta fica persistida e separada da principal
    assert state.config["risk_pct:tp2"] == 2.0
    assert state._base_risk_for_lane("tp2") == 2.0
    assert state._base_risk_for_lane("main") == 0.8
    # a config na BD também guardou a chave própria da 2ª conta
    assert float(state.db.all_config().get("risk_pct:tp2") or 0) == 2.0
    # repõe o risco para não afetar outros testes
    client.post("/api/config", json={"risk_pct": 1.0})


def test_config_lots_override_tp2_persiste_e_exposto(client):
    """`lots_override_tp2` no POST /api/config fixa o lote POR PERNA da 2ª
    conta (`lots_override:tp2` na config): persiste na BD, expõe-se no
    helper/lane e na preview; a main não é afetada; 0 volta ao risco."""
    from app.main import state
    r = client.post("/api/config", json={"lots_override_tp2": 0.04})
    assert r.status_code == 200 and not r.json().get("error")
    assert float(state.config.get("lots_override:tp2") or 0) == 0.04
    assert state._fixed_lots_for_lane("tp2") == 0.04
    assert state._fixed_lots_for_lane("main") is None  # a principal não muda
    assert float(state.db.all_config().get("lots_override:tp2") or 0) == 0.04
    # a preview da lane 2 mostra o lote fixo por perna
    rp = state.risk_preview("tp2")
    assert rp.get("lots_override") == 0.04 and rp["lots_per_leg"] == 0.04
    # fora do limite → 422
    assert client.post(
        "/api/config", json={"lots_override_tp2": 50}).status_code == 422
    # repõe (0 = dimensionamento por risco) para não afetar outros testes
    client.post("/api/config", json={"lots_override_tp2": 0})
    assert state._fixed_lots_for_lane("tp2") is None


def test_phantom_null_retcode_lane2_alert_inclui_estado_worker(client):
    """Retcode NULL ('?') na lane 2 (broker respondeu SEM retcode — o caso
    das 14:52) tem alerta risk distinto com o estado do worker, e o scan do
    tick não repete o alerta para a mesma trade."""
    from app.main import state as st
    from app.models import Trade
    from app.execution.engine import _now_iso

    # 1) trade phantom da lane 2 com retcodes ['?'] — assinatura do null
    st.db.insert_trade(Trade(
        id="t-null-rc-lane2", signal_id="sig-null", symbol="XAUUSD",
        direction="buy", entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
        take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed", opened_at=_now_iso(), closed_at=_now_iso(), pnl=0.0,
        exit_reason=None,
        error="ordem MT5 recusada pelo broker (retcodes ['?', '?'])",
        retcodes=["?", "?"], lane_id="tp2",
    ))
    # 2) helper da BD devolve-a com a assinatura
    found = st.db.recent_phantom_null_retcodes(minutes=30, lane="tp2")
    assert any(tid == "t-null-rc-lane2" for tid, _ in found), found
    # 3) o scan do tick alerta uma vez e com o estado do worker no texto
    #    (reset do guard de 30s — outros testes podem já ter corrido o scan)
    st._last_null_rc_check = 0.0
    before = st.db.list_notifications(500)
    import asyncio
    asyncio.run(st._check_null_retcode_phantoms())
    after = st.db.list_notifications(500)
    new = [n for n in after if n.id not in {o.id for o in before}]
    assert new and any("SEM retcode" in (n.text or "") for n in new), new
    msg = next(n for n in new if "SEM retcode" in (n.text or ""))
    assert msg.lane == "tp2"
    assert ("worker" in msg.text.lower() or "terminal" in msg.text.lower())
    # 4) segunda passada não volta a alertar (guard _null_rc_alerted)
    st._last_null_rc_check = 0.0
    before2 = st.db.list_notifications(500)
    asyncio.run(st._check_null_retcode_phantoms())
    after2 = st.db.list_notifications(500)
    assert len(after2) == len(before2), "não pode repetir o alerta da mesma trade"


def test_recent_phantom_null_retcodes_ignora_outros_retcodes(client):
    """O scan só apanha a assinatura null ('?') — NO_TICK/10027/outros ficam
    fora (o alerta desses já existe nos caminhos normais)."""
    from app.main import state as st
    from app.models import Trade
    from app.execution.engine import _now_iso

    st.db.insert_trade(Trade(
        id="t-notick-lane2", signal_id="sig-nt", symbol="XAUUSD",
        direction="buy", entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
        take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed", opened_at=_now_iso(), closed_at=_now_iso(), pnl=0.0,
        exit_reason=None,
        error="ordem MT5 recusada pelo broker (retcodes ['NO_TICK', 'NO_TICK'])",
        retcodes=["NO_TICK", "NO_TICK"], lane_id="tp2",
    ))
    found = st.db.recent_phantom_null_retcodes(minutes=30, lane="tp2")
    assert all(tid != "t-notick-lane2" for tid, _ in found), found
    # lane main também fica fora do scan (foco lane 2)
    st.db.insert_trade(Trade(
        id="t-null-main", signal_id="sig-nm", symbol="XAUUSD",
        direction="buy", entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
        take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed", opened_at=_now_iso(), closed_at=_now_iso(), pnl=0.0,
        exit_reason=None,
        error="ordem MT5 recusada pelo broker (retcodes ['?'])",
        retcodes=["?"], lane_id="main",
    ))
    found = st.db.recent_phantom_null_retcodes(minutes=30, lane="tp2")
    assert all(tid != "t-null-main" for tid, _ in found), found
