"""Dashboard authentication.

The dashboard is fully open when no password is configured (local use). Set
`SIGNALBOT_DASHBOARD_PASSWORD` (in .env or the environment) to require a
login before anyone can see the dashboard or touch the API.

Two ways in:
- Browser: POST /api/login with the password -> httpOnly session cookie.
- The Telegram listener (no browser): sends the `X-API-Token` header with
  `SIGNALBOT_API_TOKEN` (when configured).

Sessions live in memory (single-process server). The password is read on
every check so tests can toggle it with monkeypatch.
"""

import os
import secrets
import time
from collections import deque

COOKIE_NAME = "sb_session"
SESSION_TTL = 7 * 24 * 3600  # 7 days

_sessions: dict[str, float] = {}  # token -> expiry (unix ts)

# ---------------------------------------------------------------- rate limit
# Lockout por IP no /api/login: janela deslizante em memória (processo único).
# Após LOGIN_MAX_ATTEMPTS falhas dentro de LOGIN_WINDOW_SECONDS o IP fica
# bloqueado até a janela expirar. Config por env (0 desliga o limite).
LOGIN_WINDOW_SECONDS = int(os.getenv("SIGNALBOT_LOGIN_WINDOW", "300") or 300)
LOGIN_MAX_ATTEMPTS = int(os.getenv("SIGNALBOT_LOGIN_MAX_ATTEMPTS", "5") or 5)

_failed_logins: dict[str, deque] = {}  # ip -> timestamps de falhas


def client_ip(request) -> str:
    """IP real do cliente (atrás do Caddy/túnel respeita X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for") if request is not None else None
    if xff:
        # primeiro salto da cadeia é o cliente original
        return xff.split(",", 1)[0].strip() or "?"
    if request is not None and request.client is not None:
        return request.client.host or "?"
    return "?"


def login_locked(ip: str) -> int:
    """Segundos de bloqueio restantes para o IP (0 = livre)."""
    if LOGIN_MAX_ATTEMPTS <= 0 or LOGIN_WINDOW_SECONDS <= 0:
        return 0
    now = time.time()
    dq = _failed_logins.get(ip)
    if not dq:
        return 0
    cutoff = now - LOGIN_WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) < LOGIN_MAX_ATTEMPTS:
        return 0
    return max(1, int(dq[0] + LOGIN_WINDOW_SECONDS - now))


def record_failed_login(ip: str) -> None:
    """Regista uma falha de login do IP (janela deslizante, anti brute-force)."""
    if LOGIN_MAX_ATTEMPTS <= 0 or LOGIN_WINDOW_SECONDS <= 0:
        return
    now = time.time()
    dq = _failed_logins.setdefault(ip, deque())
    dq.append(now)
    cutoff = now - LOGIN_WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()


def clear_failed_logins(ip: str) -> None:
    """Login com sucesso limpa o histórico do IP."""
    _failed_logins.pop(ip, None)


def reset_login_limit_state() -> None:
    """Limpa todo o estado do lockout (testes)."""
    _failed_logins.clear()


def dashboard_password() -> str:
    return os.getenv("SIGNALBOT_DASHBOARD_PASSWORD", "")


def api_token() -> str:
    return os.getenv("SIGNALBOT_API_TOKEN", "")


def enabled() -> bool:
    return bool(dashboard_password())


def check_password(password: str) -> bool:
    pwd = dashboard_password()
    return bool(pwd) and secrets.compare_digest(password or "", pwd)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def check_api_token(header: str | None) -> bool:
    tok = api_token()
    return bool(tok) and header is not None and secrets.compare_digest(header, tok)
