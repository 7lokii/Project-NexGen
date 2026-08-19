"""Optional LLM fallback for signal parsing.

The regex parser (`app/signals/parser.py`) handles the templated group
formats deterministically and is the ONLY parser used on the happy path.
This module is a safety net for messages the regex cannot parse: when
`parse_or_error` fails and an LLM endpoint is configured, the raw text is
sent to the model and the proposed fields go through the exact same sanity
checks as a regex parse (`validate_signal`).

Safety rules (never trade blindly):
  * The LLM result is rejected unless `confidence >= SIGNALBOT_LLM_MIN_CONFIDENCE`
    (default 0.8) — a low-confidence parse is a failed signal, not a trade.
  * The proposed signal must pass `validate_signal` (stop on the right side,
    TPs ordered, no absurd TP distance). Anything invalid -> failed signal.
  * Signals parsed this way are flagged `llm_parsed=True`, which forces them
    to PENDING (manual approval) — auto mode never executes them.

Disabled by default: no API key / base URL configured -> `try_llm_parse`
returns None and the pipeline behaves exactly as before (regex only).

Environment:
  SIGNALBOT_LLM_API_KEY         - API key (required to enable)
  SIGNALBOT_LLM_BASE_URL        - OpenAI-compatible endpoint (default https://api.openai.com/v1)
  SIGNALBOT_LLM_MODEL           - model id (default gpt-4o-mini)
  SIGNALBOT_LLM_MIN_CONFIDENCE  - 0..1 (default 0.8)
"""

import json
import os
from typing import Optional

import httpx

from app.models import Signal
from app.signals.parser import validate_signal

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MIN_CONFIDENCE = 0.8


def llm_configured() -> bool:
    """True when an API key is present (the module is opt-in)."""
    return bool(os.getenv("SIGNALBOT_LLM_API_KEY", "").strip())


def _min_confidence() -> float:
    try:
        return float(os.getenv("SIGNALBOT_LLM_MIN_CONFIDENCE", str(DEFAULT_MIN_CONFIDENCE)))
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE


_SYSTEM_PROMPT = """You extract trading signals from a Telegram message. \
Return ONLY JSON with these fields (no markdown, no prose):
{"symbol": "XAUUSD", "direction": "buy", "entry": 4397.5,
 "stop_loss": 4389.5, "take_profit_1": 4400.0, "take_profit_2": 4402.0,
 "take_profit_3": 4404.0, "take_profit_4": 4408.0, "take_profit_5": 4413.0,
 "confidence": 0.95}
Rules: symbol is a 6-letter code (XAUUSD, EURUSD...); direction is buy or
sell; entry/stop_loss/TPs are numbers (a TP you cannot read = 0); confidence
is 0..1 reflecting how sure you are the message is a real signal with those
exact numbers. If the message is NOT a trading signal, return
{"signal": false}."""


async def try_llm_parse(raw_text: str, source: str = "telegram",
                        signal_id: str = "", received_at: str = "") -> Optional[Signal]:
    """Ask the LLM to parse a message the regex could not.

    Returns a validated Signal flagged `llm_parsed=True`, a failed Signal
    (invalid / low confidence), or None when the LLM is not configured or
    the call fails (the caller keeps the original failed parse). Never
    raises.
    """
    if not llm_configured():
        return None

    base_url = os.getenv("SIGNALBOT_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.getenv("SIGNALBOT_LLM_MODEL", DEFAULT_MODEL)
    api_key = os.getenv("SIGNALBOT_LLM_API_KEY", "")

    def _failed(error: str) -> Signal:
        return Signal(
            id=signal_id, source=source, raw_text=raw_text, symbol="",
            direction="", entry=0.0, stop_loss=0.0, take_profit_1=0.0,
            take_profit_2=0.0, take_profit_3=0.0, take_profit_4=0.0,
            take_profit_5=0.0, status="pending", received_at=received_at,
            error=error,
        )

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.0,
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        print(f"[llm] falha na chamada: {exc}")
        return _failed(f"LLM indisponível ({exc.__class__.__name__})")

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return _failed("LLM devolveu JSON inválido")

    if data.get("signal") is False:
        return _failed("LLM: não é um sinal de trading")

    confidence = float(data.get("confidence", 0.0))
    if confidence < _min_confidence():
        return _failed(f"LLM: confiança baixa ({confidence:.0%} < "
                       f"{_min_confidence():.0%}) — rejeitado")

    def _num(key: str) -> float:
        try:
            return float(data.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    symbol = str(data.get("symbol") or "").upper().replace("/", "").replace(" ", "")
    direction = str(data.get("direction") or "").lower().strip()
    if direction not in ("buy", "sell"):
        return _failed("LLM: direção inválida")
    if len(symbol) != 6 or not symbol.isalpha():
        return _failed("LLM: símbolo inválido")

    signal = Signal(
        id=signal_id, source=source, raw_text=raw_text, symbol=symbol,
        direction=direction, entry=_num("entry"), stop_loss=_num("stop_loss"),
        take_profit_1=_num("take_profit_1"), take_profit_2=_num("take_profit_2"),
        take_profit_3=_num("take_profit_3"), take_profit_4=_num("take_profit_4"),
        take_profit_5=_num("take_profit_5"), status="pending",
        received_at=received_at, llm_parsed=True,
    )

    problem = validate_signal(signal)
    if problem:
        return _failed(f"LLM: {problem}")
    if signal.entry <= 0 or signal.stop_loss <= 0:
        return _failed("LLM: entrada/stop loss em falta")

    print(f"[llm] parse OK ({signal.symbol} {signal.direction} @ "
          f"{signal.entry} conf {confidence:.2f}) — requer aprovação manual")
    return signal
