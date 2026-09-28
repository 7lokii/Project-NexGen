"""Golden Moves per-trade recount parser (external fill audit).

The admin posts a structured recap of the day's trades, one per line/message:

    "Signal #1: Sell at 4357. Price went up to 4362, then dropped to 4351. +110 pips"

Each recount carries: the group's own signal number, direction, entry,
favourable excursion (where price went first), the reversal price and the
reported pips.  This is ground truth EXTERNAL to the bot: comparing it with
the real closed trades of the same day detects divergent entries/pips (a
free fill-audit while the demo account trades the same signals).

`parse_recount()` is deliberately conservative — it returns None unless the
message has the full Signal #N → Sell/Buy at X → went … to Y → then … to Z →
±N pips structure, so ordinary chatter about a trade never parses.
"""

import re
from typing import Optional

# "Signal #1:" / "Signal 3" / "#2"
_SIGNAL_N = re.compile(r"\b(?:signal\s*)?#?\s*(\d+)\b", re.IGNORECASE)
# "Sell at 4357" / "Buy @ 4396.50"
_DIR_ENTRY = re.compile(
    r"\b(Sell|Buy)\b.{0,20}?\b(?:at|@)\s*(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)
# "Price went up to 4362" / "went down to X" / "moved up to"
_EXCURSION = re.compile(
    r"\b(?:went|moved|rose|climbed|pushed|ran|bounced|shot|jumped)\s+"
    r"(up|down)\s+to\s+(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)
# "then dropped to 4351" / "then came back down to X" / "reversed to X"
_REVERSAL = re.compile(
    r"\b(?:then\s+)?(?:dropped|fell|reversed|came\s+back|pulled\s+back|"
    r"retraced|went\s+back)\s+(?:back\s+)?(?:down|up|to)?\s*"
    r"(?:to\s+)?(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)
# "+110 pips" / "-25 pips" / "110 pips"
_PIPS = re.compile(r"([+-]?\d+(?:[.,]\d+)?)\s*pips?\b", re.IGNORECASE)


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def parse_recount(text: str) -> Optional[dict]:
    """Extract a structured per-trade recount, or None.

    Returns::

        {
          "signal_number": 1,
          "direction": "sell",
          "entry": 4357.0,
          "excursion_price": 4362.0,   # first move (favourable or adverse)
          "excursion_dir": "up",
          "reversal_price": 4351.0,    # price after the reversal
          "pips": 110.0,
        }

    ``direction`` is normalised to the bot's "buy"/"sell" vocabulary.
    """
    text = (text or "").strip()
    if not text:
        return None
    m_n = _SIGNAL_N.search(text)
    m_de = _DIR_ENTRY.search(text)
    m_ex = _EXCURSION.search(text)
    m_rev = _REVERSAL.search(text)
    m_pips = _PIPS.search(text)
    if not (m_n and m_de and m_ex and m_rev and m_pips):
        return None
    return {
        "signal_number": int(m_n.group(1)),
        "direction": m_de.group(1).lower(),
        "entry": _num(m_de.group(2)),
        "excursion_price": _num(m_ex.group(2)),
        "excursion_dir": m_ex.group(1).lower(),
        "reversal_price": _num(m_rev.group(1)),
        "pips": _num(m_pips.group(1)),
    }
