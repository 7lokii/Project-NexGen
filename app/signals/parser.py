"""Signal parser.

Turns raw, free-form Telegram messages into structured signals. Signal
groups never agree on a format, so the parser is deliberately tolerant:
it understands Portuguese and English keywords, several layouts and
comma decimals.

If a required field is missing the message is returned as a failed signal
with a human-readable error, so bad messages never crash the pipeline.
"""

import re
from typing import Optional, Tuple

from app.models import Signal

# Direction words. `buy(?:ing)?` also matches "buying" (used by Golden Moves);
# `compr(?:o|ando|ar)` covers compro/comprando/comprar. `now` helps the old
# one-line format ("XAU USD SELL NOW 4737-4741") when buy/sell is unclear.
_DIR_BUY = re.compile(
    r"\b(buy(?:ing)?|compr(?:o|a|ando|ar)|long|call|alta|subir)\b", re.IGNORECASE
)
_DIR_SELL = re.compile(
    r"\b(sell(?:ing)?|vend(?:o|a|endo|er)|short|put|baixa|descer)\b", re.IGNORECASE
)
# A 6-letter symbol like XAUUSD is indistinguishable from a word like COMPRA,
# so we scan all candidates and skip known words.
_SYMBOL_PATTERN = re.compile(r"\b[A-Z]{3}\s?/\s?[A-Z]{3}\b|\b[A-Z]{6}\b", re.IGNORECASE)
# Symbols we know about, checked before the generic 6-letter scan (avoids
# picking up "BUYING"/"TRADES" as a symbol).
_KNOWN_SYMBOLS = (
    "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "NZDUSD", "USDCHF", "EURJPY", "GBPJPY", "EURGBP", "BTCUSD", "ETHUSD",
)
# Old Golden Moves formats wrote the symbol with a space ("XAU USD SELL NOW")
# or the metal name ("Sell Gold Now") — normalize them before symbol scan.
_SYMBOL_SPACED = {
    "XAU USD": "XAUUSD", "XAG USD": "XAGUSD",
    "XAU/USD": "XAUUSD", "XAG/USD": "XAGUSD",
}
_SYMBOL_ALIASES = {
    "GOLD": "XAUUSD", "SILVER": "XAGUSD",
}
_SYMBOL_STOPWORDS = {
    "BUY", "SELL", "COMPRA", "VENDA", "LONG", "SHORT", "CALL", "PUT",
    "ENTRADA", "ENTRY", "STOP", "STOPLOSS", "PRICE", "ALVO", "TARGET",
    "TAKE", "PROFIT", "TP1", "TP2", "TP3", "SL", "ORDER", "ORDEM",
    "BUYING", "SELLING", "COMPRANDO", "VENDENDO", "WINNING", "TRADES",
    "SIGNALS", "PIPS", "MARKET", "WEEKLY", "FRIDAY", "THURSDAY", "DAILY",
    "MONDAY", "TUESDAY", "WEDNESDAY", "SUNDAY", "SATURDAY", "BREAKEVEN",
}

# A price may be an integer (4381) or have decimals (1.0850 / 1,0850).
_NUM = r"\d+(?:[.,]\d+)?"

_ENTRY = re.compile(
    rf"(?:entr(?:ada)?|entry|pre[çc]o|price|@)\s*[:=]?\s*({_NUM})\b", re.IGNORECASE
)
_STOP = re.compile(
    rf"(?:stop\s*loss|stop|sl|perda)\b\s*/?\s*(?:invalid\b)?\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
_TP1 = re.compile(
    rf"(?:tp\s*1|tp1|take\s*profit\s*1|alvo\s*1|target\s*1)\b\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
_TP2 = re.compile(
    rf"(?:tp\s*2|tp2|take\s*profit\s*2|alvo\s*2|target\s*2)\b\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
_TP3 = re.compile(
    rf"(?:tp\s*3|tp3|take\s*profit\s*3|alvo\s*3|target\s*3)\b\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
_TP4 = re.compile(
    rf"(?:tp\s*4|tp4|take\s*profit\s*4|alvo\s*4|target\s*4)\b\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
_TP5 = re.compile(
    rf"(?:tp\s*5|tp5|take\s*profit\s*5|alvo\s*5|target\s*5)\b\s*[:=]?\s*({_NUM})\b",
    re.IGNORECASE,
)
# Entry given as an unlabeled zone on its own line: "4389-4385", "4351.50-4347".
# The line may start with an entry label ("Entry: 4389-4385"), but never with
# TP/SL prefixes (those lines carry labels and would misparse as zones).
_ENTRY_ZONE = re.compile(
    rf"^\s*(?:entr(?:ada)?|entry|pre[çc]o|price|@)?\s*[:=]?\s*"
    rf"({_NUM})\s*[-–—]\s*({_NUM})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Old Golden Moves one-line format: "XAU USD SELL NOW 4737-4741 🥇TP1 …",
# "Sell Gold Now 4123.5 - 4128.5 Targets …" — the zone sits in the middle of
# the line, after the direction word. Anchored on the direction word so a
# random "10-15 pips" never becomes an entry.
_ENTRY_ZONE_INLINE = re.compile(
    rf"\b(buy(?:ing)?|sell(?:ing)?|compr\w*|vend\w*|now|gold|silver)\b"
    rf"[^\n]*?({_NUM})\s*[-–—]\s*({_NUM})\b",
    re.IGNORECASE,
)
# Golden Circle format: a single entry price right after the symbol, with no
# label and no zone — "I'm buying XAUUSD  4331  TP1 4336 TP2 4341 SL 4325".
# Only known symbols count (a bare "BUYING 4" can never become an entry) and
# a trailing dash means it is a zone, which the zone patterns above handle.
_ENTRY_SYMBOL = re.compile(
    rf"\b({'|'.join(_KNOWN_SYMBOLS)})\s*[:=]?\s*({_NUM})\b(?!\s*[-–—]\s*\d)",
    re.IGNORECASE,
)
# The oldest Golden Moves format listed targets WITHOUT per-target labels:
# "Sell Gold Now 4123.5 - 4128.5  Targets  4121.5 4119.5 4117  Open  SL/ invalid 4131.5"
# — the numbers after "Targets" are TP1/TP2/TP3 in order. Also handles the
# emoji-medal variant where TP labels exist (those fall through to _TP1.._TP5).
_TARGETS_LIST = re.compile(
    rf"\btargets?\b\s*[:=]?\s*({_NUM})\s+({_NUM})\s+({_NUM})\b(?:(?:\s+)({_NUM}))?(?:(?:\s+)({_NUM}))?",
    re.IGNORECASE,
)


class ParseError(Exception):
    """Raised when a raw message cannot be turned into a valid signal.

    ``signal`` carries the fields that were successfully extracted before a
    required field failed. The edit-window pipeline uses those partial fields
    (especially symbol, direction and entry zone) to audit and revive a later
    Telegram edit without guessing missing values.
    """

    def __init__(self, message: str, signal: Optional[Signal] = None):
        super().__init__(message)
        self.signal = signal


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def _direction(text: str) -> Optional[str]:
    if _DIR_BUY.search(text):
        return "buy"
    if _DIR_SELL.search(text):
        return "sell"
    return None


def _symbol(text: str) -> Optional[str]:
    upper = text.upper()
    # old formats: "XAU USD", "Sell Gold Now" -> XAUUSD
    for spaced, canonical in _SYMBOL_SPACED.items():
        if re.search(rf"\b{re.escape(spaced)}\b", upper):
            return canonical
    for alias, canonical in _SYMBOL_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", upper):
            return canonical
    for sym in _KNOWN_SYMBOLS:
        if re.search(rf"\b{re.escape(sym)}\b", upper):
            return sym
    for m in _SYMBOL_PATTERN.finditer(text):
        sym = m.group(0).upper().replace("/", "").replace(" ", "")
        if sym not in _SYMBOL_STOPWORDS:
            return sym
    return None


def _pick_zone_entry(a: float, b: float, direction: Optional[str],
                     tp1: Optional[float]) -> Optional[float]:
    """Pick a single entry from a zone like "4389-4385".

    Conservative rule: for a buy we fill at the TOP of the zone (worst
    price), for a sell at the BOTTOM. If only one end keeps TP1 on the
    right side of the entry (e.g. a typo in the zone), that end wins.

    Zones wider than ~1% of the price are typos ("4379-4775" is 396 points
    of gold — nobody trades a zone that wide) and are rejected: the two
    ends cannot both be "the entry" of one signal.
    """
    lo, hi = min(a, b), max(a, b)
    mid = (lo + hi) / 2
    if mid > 0 and (hi - lo) > 0.01 * mid:
        return None
    if direction == "buy":
        valid = [c for c in (lo, hi) if tp1 is None or tp1 > c]
        return max(valid) if valid else None
    if direction == "sell":
        valid = [c for c in (lo, hi) if tp1 is None or tp1 < c]
        return min(valid) if valid else None
    return None


def parse(raw_text: str, source: str = "telegram", signal_id: str = "", received_at: str = "") -> Signal:
    """Parse a raw message into a Signal.

    Raises ParseError when a mandatory field (direction, symbol, entry,
    stop loss, TP1, TP2) is missing or invalid.
    """
    text = raw_text.strip()
    errors = []

    direction = _direction(text)
    if direction is None:
        errors.append("direção (buy/sell) não encontrada")

    symbol = _symbol(text)
    if symbol is None:
        errors.append("símbolo (ex.: EURUSD) não encontrado")

    tp1 = _optional_float(_TP1.search(text))
    tp2 = _optional_float(_TP2.search(text))
    tp3 = _optional_float(_TP3.search(text))
    tp4 = _optional_float(_TP4.search(text))
    tp5 = _optional_float(_TP5.search(text))
    # oldest format: "Targets 4121.5 4119.5 4117 Open" — targets are an
    # unlabelled list after the word "Targets" (TP1/TP2/TP3 in order); only
    # used when the labelled TP1..TP3 above did not match
    if tp1 is None or tp2 is None:
        tgt = _TARGETS_LIST.search(text)
        if tgt:
            if tp1 is None:
                tp1 = _num(tgt.group(1))
            if tp2 is None:
                tp2 = _num(tgt.group(2))
            if tp3 is None:
                tp3 = _num(tgt.group(3))
            if tp4 is None and tgt.group(4):
                tp4 = _num(tgt.group(4))
            if tp5 is None and tgt.group(5):
                tp5 = _num(tgt.group(5))
    stop = _optional_float(_STOP.search(text))
    entry = _optional_float(_ENTRY.search(text))
    entry_zone_low = None
    entry_zone_high = None
    zone = _ENTRY_ZONE.search(text)
    if zone is None and entry is None:
        zone = _ENTRY_ZONE_INLINE.search(text)
    if zone is None and entry is None:
        # Golden Circle single-entry format: "I'm buying XAUUSD  4331  TP1 …"
        single = _ENTRY_SYMBOL.search(text)
        if single:
            entry = _num(single.group(2))
    if zone:
        # Preserve the zone even when required details are still missing. The
        # first Telegram version may contain only direction + zone; keeping it
        # on the failed/buffered row is required for a later edit/revive and
        # for the entry guard audit.
        # inline format has the direction word in group(1); the zone in
        # groups (2,3) — _ENTRY_ZONE keeps the zone in groups (1,2)
        if zone.lastindex >= 3:
            a, b = _num(zone.group(2)), _num(zone.group(3))
        else:
            a, b = _num(zone.group(1)), _num(zone.group(2))
        entry_zone_low, entry_zone_high = min(a, b), max(a, b)
        if entry is None:
            entry = _pick_zone_entry(a, b, direction, tp1)

    if entry is None:
        errors.append("entrada não encontrada")
    if stop is None:
        errors.append("stop loss não encontrado")
    if tp1 is None:
        errors.append("TP1 não encontrado")
    if tp2 is None:
        errors.append("TP2 não encontrado")

    # An isolated labelled entry ("@ 1.0850") is only a fragment of a
    # Telegram message. Keep explicit zones for revive/entry-guard auditing,
    # but do not expose a standalone price as an operational entry until the
    # stop and target fields arrive. This prevents an incomplete row from
    # looking executable while preserving the completed-edit path.
    entry_value = entry
    if errors and zone is None:
        entry_value = None

    signal = Signal(
        id=signal_id,
        source=source,
        raw_text=raw_text,
        symbol=symbol or "",
        direction=direction or "",
        entry=entry_value or 0.0,
        stop_loss=stop or 0.0,
        take_profit_1=tp1 or 0.0,
        take_profit_2=tp2 or 0.0,
        take_profit_3=tp3 or 0.0,
        take_profit_4=tp4 or 0.0,
        take_profit_5=tp5 or 0.0,
        status="pending",
        received_at=received_at,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
    )

    if errors:
        raise ParseError("; ".join(errors), signal=signal)

    problem = validate_signal(signal)
    if problem:
        raise ParseError(problem, signal=signal)
    return signal


def validate_signal(signal) -> Optional[str]:
    """Sanity-check a Signal (regex- or LLM-parsed). Returns an error message
    or None when the signal is tradable.

    Shared by `parse()` and the LLM fallback so AI output passes the exact
    same mandatory checks (never trust a model blindly): stop loss on the
    right side of entry, TPs ordered and each further from entry than the
    last, and no TP absurdly far from TP1 (typo in the source message).
    """
    direction = signal.direction
    entry = float(signal.entry or 0.0)
    stop = float(signal.stop_loss or 0.0)
    tps = [float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0)
           for i in range(1, 6)]
    tps = [tp for tp in tps if tp > 0]
    if not tps:
        return "sem take profits"

    if direction == "buy" and stop >= entry:
        return "stop loss deve ser inferior à entrada (compra)"
    if direction == "sell" and stop <= entry:
        return "stop loss deve ser superior à entrada (venda)"
    if direction == "buy" and any(tp <= entry for tp in tps):
        return "TP deve ser superior à entrada (compra)"
    if direction == "sell" and any(tp >= entry for tp in tps):
        return "TP deve ser inferior à entrada (venda)"
    # TPs iguais são permitidos: o grupo já enviou "TP1 4280 / TP2 4280" a
    # sério (ambas as pernas saem ao mesmo nível — 15/09). Só rejeita TPs
    # verdadeiramente fora de ordem (invertem a direção da entrada).
    if direction == "buy" and any(b < a for a, b in zip(tps, tps[1:])):
        return "TPs fora de ordem (devem afastar-se da entrada progressivamente)"
    if direction == "sell" and any(b > a for a, b in zip(tps, tps[1:])):
        return "TPs fora de ordem (devem afastar-se da entrada progressivamente)"
    distance = abs(tps[0] - entry)
    if distance > 0 and any(abs(tp - entry) > 20 * distance for tp in tps[1:]):
        return "TP suspeito (distância muito maior que TP1 — provável typo)"
    return None


def parse_or_error(raw_text: str, source: str = "telegram", signal_id: str = "",
                   received_at: str = "") -> Signal:
    """Like `parse`, but returns a failed Signal instead of raising.

    Used by the edit-window flow, where a message may be temporarily invalid
    and later fixed by an edit.
    """
    try:
        return parse(raw_text, source=source, signal_id=signal_id, received_at=received_at)
    except ParseError as exc:
        signal = exc.signal or Signal(
            id=signal_id, source=source, raw_text=raw_text, symbol="",
            direction="", entry=0.0, stop_loss=0.0, take_profit_1=0.0,
            take_profit_2=0.0, take_profit_3=0.0, take_profit_4=0.0,
            take_profit_5=0.0, status="pending", received_at=received_at,
        )
        signal.error = str(exc)
        return signal


def _optional_float(match: Optional[re.Match]) -> Optional[float]:
    if not match:
        return None
    try:
        return _num(match.group(1))
    except ValueError:
        return None
