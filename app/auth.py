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

COOKIE_NAME = "sb_session"
SESSION_TTL = 7 * 24 * 3600  # 7 days

_sessions: dict[str, float] = {}  # token -> expiry (unix ts)


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
