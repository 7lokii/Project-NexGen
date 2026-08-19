"""Regressão do bug do countdown inflado ("3000s em vez de 30s"):

o servidor/listener gravava timestamps naive locais (sem fuso); o browser
interpretava `edit_deadline` como hora local dele e a contagem da janela de
edição ficava deslocada pelo offset do timezone. A correção foi gravar tudo
em **UTC explícito** (`+00:00`). Estes testes garantem que nenhum produtor de
timestamps volta a emitir valores naive — tanto os produtores em memória como
as **linhas efetivamente gravadas na BD** (signals, trades, balance history).
"""

import os
import tempfile

import pytest

# Point the app at a throwaway DB *before* importing it (mesmo padrão do
# test_api.py — nunca escrever na BD de produção durante a suite).
os.environ["SIGNALBOT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["MARKET_PROVIDER"] = "none"
os.environ["EXECUTION_MODE"] = "sim"
os.environ["MT5_DRY_RUN"] = "1"
os.environ["SIGNALBOT_DASHBOARD_PASSWORD"] = ""
os.environ["SIGNALBOT_API_TOKEN"] = ""

from datetime import datetime, timezone  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, state  # noqa: E402
from app.execution.engine import _now_iso  # noqa: E402
from app.state import _now_iso_plus  # noqa: E402
from app.telegram_listener import _now_iso_short  # noqa: E402

EURUSD = "BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.post("/api/config", json={"sim_speed": 0})
        yield c


def _assert_utc(iso: str, label: str) -> None:
    """O timestamp tem de parsear com fuso explícito UTC (+00:00)."""
    assert iso, f"{label} está vazio"
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None, f"{label} é naive (sem fuso): {iso!r}"
    off = dt.utcoffset()
    assert off is not None and off.total_seconds() == 0, (
        f"{label} não está em UTC: {iso!r} -> offset {off}"
    )


# ------------------------------------------------------- produtores em memória

def test_engine_now_iso_is_explicit_utc():
    _assert_utc(_now_iso(), "_now_iso (engine)")


def test_state_now_iso_plus_is_explicit_utc():
    _assert_utc(_now_iso_plus(30), "_now_iso_plus (state, edit_deadline)")


def test_listener_now_iso_short_is_explicit_utc():
    _assert_utc(_now_iso_short(), "_now_iso_short (listener, heartbeat)")


# ------------------------------------------------------- linhas gravadas na BD

def test_ingested_signal_row_is_utc(client):
    """Fluxo real: um sinal do Telegram INCOMPLETO entra na janela de edição e
    a LINHA gravada em `signals` tem `received_at`/`edit_deadline` em UTC."""
    r = client.post("/api/signals", json={
        "raw_text": "I'm selling XAUUSD",  # incompleta → espera a edição
        "source": "tg:Group",
        "message_id": "7788", "group": "111",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "buffered"
    sid = data["signal"]["id"]

    row = state.db.get_signal(sid)
    assert row is not None
    _assert_utc(row.received_at, f"signals.received_at ({sid})")
    _assert_utc(row.edit_deadline, f"signals.edit_deadline ({sid})")


def test_trade_row_opened_at_is_utc(client):
    """Ao executar (aprovar), a LINHA em `trades` tem `opened_at` UTC."""
    r = client.post("/api/signals", json={"raw_text": EURUSD, "source": "demo"})
    sid = r.json()["signal"]["id"]
    r = client.post(f"/api/signals/{sid}/approve")
    assert r.json()["action"] == "executed"
    trade_id = r.json()["trade"]["id"]

    row = state.db.get_trade(trade_id)
    assert row is not None
    _assert_utc(row.opened_at, f"trades.opened_at ({trade_id})")


def test_paper_balance_history_ts_is_utc(client):
    """A LINHA em `paper_balance_history` (curva "quem está a ganhar") tem
    `ts` em UTC explícito."""
    # escreve um snapshot novo com um ts único para isolar o assert
    from app.execution.engine import _new_id
    sid = f"tstz-{_new_id()}"
    state.db.insert_paper_balance_history(sid, _now_iso(), 1000.0)

    rows = state.db.paper_balance_history(sid)
    assert rows, "snapshot não foi gravado"
    _assert_utc(rows[-1]["ts"], f"paper_balance_history.ts ({sid})")


def test_heartbeat_stores_utc_listener_seen(client):
    """O `last_seen` do listener (heartbeat) tem de chegar com fuso UTC."""
    r = client.post("/api/heartbeat", json={
        "groups": ["Golden Moves"], "last_msg_at": _now_iso_short(), "tick": 7,
    })
    assert r.json()["ok"] is True
    d = client.get("/api/diagnostics").json()
    _assert_utc(d["listener"]["last_seen"], "listener.last_seen")
    assert d["listener"]["last_msg_age_s"] is not None
