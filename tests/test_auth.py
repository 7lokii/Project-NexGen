"""Dashboard auth tests.

Default: authentication is OFF (SIGNALBOT_DASHBOARD_PASSWORD unset) — the
dashboard and API are open, exactly as before. When the env var is set, a
login is required for the browser, and the Telegram listener can use
`X-API-Token` (SIGNALBOT_API_TOKEN).

The same env prep as test_api.py runs here BEFORE importing app.main, so the
app is built against a throwaway DB no matter which test file runs first.
"""

import os
import tempfile

import pytest

_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["SIGNALBOT_DB"] = _TMP_DB
os.environ["MARKET_PROVIDER"] = "none"
os.environ["EXECUTION_MODE"] = "sim"
os.environ["MT5_DRY_RUN"] = "1"
# auth off por defeito nos testes (o .env pode ter a password definida)
os.environ["SIGNALBOT_DASHBOARD_PASSWORD"] = ""
os.environ["SIGNALBOT_API_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.post("/api/config", json={"sim_speed": 0})
        yield c


def test_auth_off_by_default(client):
    """Sem SIGNALBOT_DASHBOARD_PASSWORD a dashboard e a API estão abertas."""
    assert os.getenv("SIGNALBOT_DASHBOARD_PASSWORD") in (None, "")
    r = client.get("/")
    assert r.status_code == 200
    assert "Project NexGen" in r.text
    assert client.get("/api/status").status_code == 200


def test_login_required_when_enabled(client, monkeypatch):
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    try:
        # página web -> login.html, nunca a dashboard
        r = client.get("/")
        assert r.status_code == 200
        assert "Dashboard protegida" in r.text
        assert "loginForm" in r.text

        # API sem cookie -> 401
        assert client.get("/api/status").status_code == 401

        # password errada -> 401
        r = client.post("/api/login", json={"password": "errada"})
        assert r.status_code == 401

        # password certa -> cookie de sessão + acesso
        r = client.post("/api/login", json={"password": "segredo-123"})
        assert r.status_code == 200
        assert r.cookies.get("sb_session")
        assert client.get("/api/status").status_code == 200

        # logout -> volta a exigir login
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/status").status_code == 401
    finally:
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_login_page_after_logout(client, monkeypatch):
    """/login (sem extensão) carrega a página de login — o botão Sair
    redireciona para /login e não pode dar 404."""
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    try:
        # sem sessão, /login serve a página de login
        r = client.get("/login")
        assert r.status_code == 200
        assert "loginForm" in r.text
        # autenticado, /login redireciona para a dashboard
        client.post("/api/login", json={"password": "segredo-123"})
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        # logout limpa a sessão; /login volta a servir a página
        client.post("/api/logout")
        r = client.get("/login")
        assert r.status_code == 200
        assert "loginForm" in r.text
    finally:
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_api_token_for_listener(client, monkeypatch):
    """O listener autentica com X-API-Token (sem browser/cookie)."""
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    monkeypatch.setenv("SIGNALBOT_API_TOKEN", "token-do-listener")
    try:
        # sem token -> 401
        assert client.get("/api/status").status_code == 401
        # token errado -> 401
        assert client.get("/api/status", headers={"X-API-Token": "nope"}).status_code == 401
        # token certo -> 200
        assert client.get("/api/status", headers={"X-API-Token": "token-do-listener"}).status_code == 200
    finally:
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")
        monkeypatch.delenv("SIGNALBOT_API_TOKEN")


def test_websocket_requires_login(client, monkeypatch):
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    try:
        # WS sem sessão -> fechado com 4401
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws"):
                pass
        assert exc.value.code == 4401

        # com sessão -> aceite
        client.post("/api/login", json={"password": "segredo-123"})
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "snapshot"
    finally:
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_login_rate_limit_lockout(client, monkeypatch):
    """Lockout por IP: após LOGIN_MAX_ATTEMPTS falhas na janela, /api/login
    devolve 429 mesmo com a password certa; o sucesso limpa o histórico."""
    from app import auth
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    auth.reset_login_limit_state()
    try:
        for i in range(auth.LOGIN_MAX_ATTEMPTS):
            r = client.post("/api/login", json={"password": "errada"})
            assert r.status_code == 401
        # bloqueado: mesmo a password certa dá 429
        r = client.post("/api/login", json={"password": "segredo-123"})
        assert r.status_code == 429, r.text
        assert r.json()["retry_after"] > 0
        # e a password errada continua 429 (não vaza informação)
        assert client.post("/api/login", json={"password": "x"}).status_code == 429
    finally:
        auth.reset_login_limit_state()
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_login_rate_limit_success_clears(client, monkeypatch):
    """Um login com sucesso limpa as falhas do IP (não pune o dono)."""
    from app import auth
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    auth.reset_login_limit_state()
    try:
        client.post("/api/login", json={"password": "errada"})
        client.post("/api/login", json={"password": "errada"})
        r = client.post("/api/login", json={"password": "segredo-123"})
        assert r.status_code == 200
        assert auth.login_locked("testclient") == 0
        assert not auth._failed_logins
    finally:
        auth.reset_login_limit_state()
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_login_rate_limit_window_expiry(client, monkeypatch):
    """Falhas saem da janela deslizante: passado o cutoff, o IP desbloqueia."""
    from app import auth
    monkeypatch.setenv("SIGNALBOT_DASHBOARD_PASSWORD", "segredo-123")
    auth.reset_login_limit_state()
    try:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            client.post("/api/login", json={"password": "errada"})
        assert client.post("/api/login", json={"password": "segredo-123"}).status_code == 429
        # simula a expiração da janela
        old = 0.0 - auth.LOGIN_WINDOW_SECONDS - 1
        for ip, dq in auth._failed_logins.items():
            dq.clear()
            dq.append(old)
        assert auth.login_locked(next(iter(auth._failed_logins), "x")) == 0
        r = client.post("/api/login", json={"password": "segredo-123"})
        assert r.status_code == 200
    finally:
        auth.reset_login_limit_state()
        monkeypatch.delenv("SIGNALBOT_DASHBOARD_PASSWORD")


def test_client_ip_forwarded_header(client):
    """Atrás do túnel/Caddy o IP vem do X-Forwarded-For."""
    from app.auth import client_ip

    class _Req:
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
        client = None

    assert client_ip(_Req()) == "203.0.113.7"
