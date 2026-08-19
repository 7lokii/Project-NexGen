"""Golden Moves periodic-summary parser (ground truth).

The group posts daily/weekly summaries with their own reported results:
"26 Trades Sent 23 winning trades 2 Stop loss 1 BE Win rate : 91% Pips gained
+2100". Those numbers are the group's ground truth for the same signals the
bot trades — comparing them with the bot's own backtest shows how much of the
gap (if any) comes from execution/management vs the signal itself.

`parse_summary()` extracts the structured fields; it returns None when the
text is not a summary. The raw text is stored alongside so nothing is lost.
"""

import re
from typing import Optional

# Markdown asterisks may sit between the number and the label
# ("26 **Trades Sent**", "** 2  Stop loss **"), so allow `**` after the number.
_STARS = r"\s*\*{0,2}\s*"
# Period inside the weekly summary: **(10/08/26_14/08/26)**
_PERIOD = re.compile(r"\((\d{1,2}/\d{1,2}/\d{2})_(\d{1,2}/\d{1,2}/\d{2})\)")
# "26 Trades Sent" / "6Trades Sent" (no space in one sample) / "26 **Trades Sent**"
_TRADES_SENT = re.compile(rf"(\d+){_STARS}Trades?\s*Sent", re.IGNORECASE)
_WINNING = re.compile(rf"(\d+){_STARS}winning\s*trades?", re.IGNORECASE)
# Formato antigo do grupo: "5 Trades Sent 4 winning" (winning no fim, sem a
# palavra "trades" a seguir) — o número imediatamente antes de "winning".
_WINNING_OLD = re.compile(rf"Trades?\s*Sent{_STARS}(\d+)\s*winning\b", re.IGNORECASE)
_STOP_LOSS = re.compile(rf"(\d+){_STARS}Stop\s*loss", re.IGNORECASE)
_BE = re.compile(rf"(\d+){_STARS}\bBE\b", re.IGNORECASE)
_WIN_RATE = re.compile(rf"Win\s*rate{_STARS}:?\s*(\d+)%", re.IGNORECASE)
_PIPS = re.compile(rf"Pips?\s*gained{_STARS}\+?(\d+)", re.IGNORECASE)


def _ddmmyy_to_iso(value: str) -> str:
    """Convert 10/08/26 -> 2026-08-10 (assume 20xx).

    Pads day/month to two digits so ISO dates sort chronologically
    (03/08/26 must become 2026-08-03, not 2026-08-3)."""
    d, m, y = value.split("/")
    return f"20{y}-{int(m):02d}-{int(d):02d}"


def parse_summary(text: str) -> Optional[dict]:
    """Extract the group's reported stats from a summary message.

    Returns a dict or None when the text is not a summary. `period_end` is
    the day the report covers (single day for daily summaries, the end of the
    range for weekly ones); `period_start` is the beginning of the range.
    """
    text = (text or "").strip()
    m_trades = _TRADES_SENT.search(text)
    if not m_trades:
        return None

    trades_sent = int(m_trades.group(1))
    m_win = _WINNING.search(text)
    if not m_win:
        m_win = _WINNING_OLD.search(text)
    winning = int(m_win.group(1)) if m_win else 0
    stop_loss = int(_STOP_LOSS.search(text).group(1)) if _STOP_LOSS.search(text) else 0
    breakevens = int(_BE.search(text).group(1)) if _BE.search(text) else 0
    pips = int(_PIPS.search(text).group(1)) if _PIPS.search(text) else 0

    m_rate = _WIN_RATE.search(text)
    if m_rate:
        win_rate = float(m_rate.group(1))
    elif trades_sent:
        win_rate = round(winning / trades_sent * 100, 1)
    else:
        win_rate = 0.0

    period = {"start": None, "end": None}
    m_period = _PERIOD.search(text)
    if m_period:
        period["start"] = _ddmmyy_to_iso(m_period.group(1))
        period["end"] = _ddmmyy_to_iso(m_period.group(2))

    return {
        "trades_sent": trades_sent,
        "winning": winning,
        "stop_loss": stop_loss,
        "breakevens": breakevens,
        "win_rate": win_rate,
        "pips": pips,
        "period_start": period["start"],
        "period_end": period["end"],
    }
