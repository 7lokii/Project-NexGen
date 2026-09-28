"""Group management-message classifier.

Signal groups don't just send entries: they send real-time management
instructions ("TP1 HIT", "close first entries and move stop loss above best
entries", "we're out at BE") and periodic summaries. This module classifies
those free-form messages into a small set of actions so the bot can act on
them deterministically (see `app/state.py::_handle_management`) instead of
dropping them as failed parses.

`classify()` returns None for anything that is not a management message
(entries, chatter, charts). The classifier is deliberately conservative: a
wrongly-triggered "close all" would close a real position, so the patterns
below were written against the actual Golden Moves message history and
prioritise the specific phrasings the group actually uses.
"""

import re
from typing import Optional

from app.messages.recounts import parse_recount

# TP1..TP5 HIT — the group announces every target it considers hit. It
# sometimes announces two at once ("TP4 and 5 HIT") — the optional second
# number lets the classifier take the highest one.
_TP_HIT = re.compile(
    r"\bTP\s*([1-5])(?:\s+(?:and|&)\s+([1-5]))?\s*HIT\b",
    re.IGNORECASE,
)
# "TP4 👀 ?" — an announced target signalled with the eyes emoji instead of
# the word HIT. Anchored to the start so "Who's with me for TP3 👀" (a
# question) never triggers an action.
_TP_EMOJI = re.compile(r"^TP\s*([1-5])\s*👀")
# "Well, we'll count that as TP2" — the group credits a target after a spike.
_COUNT_TP = re.compile(r"count\s+that\s+as\s+TP\s*([1-5])\b", re.IGNORECASE)
# "TP5 SMASHED" / "TP4 KISSED" / "TP2 tapped" / "TP4 HITTTT" — informal hit
# verbs. "TP2 anyone??" (pergunta) NÃO casa: o verbo tem de vir logo após o
# número do TP.
_TP_HIT_LOOSE = re.compile(
    r"\bTP\s*([1-6])\s*(?:(?:now\s+)?(?:HIT|SMASHED|KISSED|TAPPED)\w*)\b",
    re.IGNORECASE,
)

_SL_HIT = re.compile(r"\bSL\s*HIT\b", re.IGNORECASE)
# "SL already hit" — o grupo confirma o stop com frase mais solta.
_SL_HIT_LOOSE = re.compile(r"\bSL\s+(?:already\s+)?hit\b", re.IGNORECASE)

# "close first entries / close TP1 here" — the group's partial-profit step.
_CLOSE_FIRST = re.compile(
    r"close\s+(?:your|my|the|our|first\s+)?(?:first\s+)?(?:entr\w*|tp\s*1|tp1)|"
    r"close\s+tp\s*1\b",
    re.IGNORECASE,
)
# "Close TP1 here" with NO stop-loss instruction — partial profit only,
# the remaining legs keep their SL (no move above entry).
_CLOSE_TP1_ONLY = re.compile(
    r"close\s+tp\s*1\b|close\s+(?:the|our|my|your)?\s*first\s+(?:entr\w*|tp\s*1)\b",
    re.IGNORECASE,
)
# "Close TP2 here" / "Close TP5 when happy" — partial profit numa perna
# específica (TP1..TP6). TP1 continua a ser close_tp1; TP2..TP6 → close_tp.
_CLOSE_TP_N = re.compile(r"close\s+tp\s*([1-6])\b", re.IGNORECASE)
# The protection step also says "move the stop loss above/below best entries"
# or "risk free"/"secure some profit" — those are NOT close_tp1-only.
_MOVE_OR_RISKFREE = re.compile(
    r"move\s+(?:my|your|the|our)?\s*(?:stop\s*loss|stops?|sl)\b|"
    r"risk\s*free|secure\s+(?:some\s+)?profit",
    re.IGNORECASE,
)
# "move stop loss above/below best entries" — the risk-free protection step.
_MOVE_SL = re.compile(
    r"move\s+(?:my|your|the|our)?\s*(?:stop\s*loss|stops?|sl)\b|"
    r"move\s+(?:stop\s*loss|stops?|sl)\b",
    re.IGNORECASE,
)
_ABOVE_BELOW = re.compile(r"\b(above|below)\b", re.IGNORECASE)
# "let's make this one risk free now" — the protection step without the close.
_RISK_FREE = re.compile(r"risk\s*free", re.IGNORECASE)
# "secure some profit" (sibling of the risk-free step).
_SECURE = re.compile(r"secure\s+(?:some\s+)?profit", re.IGNORECASE)
# "set breakeven / set break even" — o grupo pede para proteger na entrada.
_SET_BE = re.compile(r"set\s+(?:the\s+)?(?:break\s*even|breakeven)\b", re.IGNORECASE)

# Full exits: "we're out after we secured TP2", "out at BE", "let's close this
# setup and wait for a better entry".
_OUT = re.compile(r"\bwe'?re?\s+out\b|\bout\s+at\s+BE\b|\bwe\s+out\b", re.IGNORECASE)
_CLOSE_SETUP = re.compile(
    r"close\s+(?:this\s+)?(?:set\s*up|trade|position|everything|all|our\s+pos)|"
    r"let'?s\s+close\s+this",
    re.IGNORECASE,
)
# "we're out at BE / after we secured TP1" — remaining legs exit at the
# ENTRY (breakeven), not at the current market price.
_OUT_BE = re.compile(r"\bout\b.{0,30}\b(BE|breakeven)\b|\bBE\b.{0,20}\bout\b|"
                     r"secured\s+TP\d.*\bout\b", re.IGNORECASE)
# "Out of this setup/trade/position" — full exit (message declarativa).
_OUT_OF = re.compile(
    r"\bout\s+of\s+(?:this\s+)?(?:set\s*up|setup|trade|position)\b",
    re.IGNORECASE,
)
# "Out at entry" (mensagem exata, sem condicional) — full exit a breakeven.
_OUT_ENTRY = re.compile(r"^\s*out\s+at\s+(?:entry|breakeven|BE)\b", re.IGNORECASE)
# "Close this at breakeven" — full exit a BE.
_CLOSE_BE = re.compile(
    r"close\s+(?:this|all|everything|it)\b.{0,30}\b(?:breakeven|break\s*even|BE)\b",
    re.IGNORECASE,
)

# "Do it now team" — the admin's GO signal: enter the trade immediately
# (finalizes an awaiting-edit signal instead of waiting the full window).
_GO = re.compile(
    r"\bdo\s+it\s+now\b|\benter\s+now\b|\bgo\s+now\b|\bjump\s+in\s+now\b",
    re.IGNORECASE,
)

# "Adjust SL to 4331" / "Move SL to 4064" — the group moves the stop loss of
# an open trade to a specific level (trailing / risk management). The level
# is optional in the message ("If you are still in trade and risk management
# allows you can adjust SL") — then it's a nudge with no concrete action.
_MOVE_SL_TO = re.compile(
    r"\b(?:adjust|move|update|change|put|set)\s+(?:the\s+|our\s+|your\s+|my\s+)?"
    r"(?:stop\s*loss|sl)\s+(?:to\s+|at\s+)?(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)
_MOVE_SL_NUDGE = re.compile(
    r"\b(?:adjust|move|update|change)\s+(?:the\s+|our\s+|your\s+|my\s+)?"
    r"(?:stop\s*loss|sl)\b",
    re.IGNORECASE,
)
# "SL adjusted / edited / corrected" (voz passiva) — o grupo mexeu no SL sem
# dar nível: nudge registado, sem ação.
_SL_ADJUSTED = re.compile(r"\bSL\s+(?:adjusted|edited|corrected)\b", re.IGNORECASE)

# Periodic summaries ("Weekly summary", "Thursday summary").
_SUMMARY = re.compile(r"\b(\w+day|\w+ly)\s+summary\b", re.IGNORECASE)
# R1: corrections must say explicitly that the summary/numbers are being
# corrected. This avoids treating ordinary summaries or discussion of a bad
# result as an instruction. The payload still needs summary numbers below.
_SUMMARY_CORRECTION = re.compile(
    r"\b(?:summary\s+)?(?:correction|corrected|amendment|erratum)\b|"
    r"\b(?:update|updated|amend|amended)\s+(?:the\s+)?summary\b|"
    r"\b(?:wrong|incorrect)\s+(?:summary|numbers)\b|"
    r"\b(?:summary|numbers)\b.{0,24}\b(?:wrong|incorrect)\b",
    re.IGNORECASE,
)

# "TP5 HIT Full target" is a tp_hit; make sure generic chatter about pips or
# the market ("Volume increased...") never triggers an action.
_SKIP_WORDS = re.compile(r"\b(volume|watch|zone|chart|ping|pong)\b", re.IGNORECASE)

# A 6-letter symbol, reused to find which symbol a management message refers
# to (most messages don't mention one — then the latest open trade wins).
_SYMBOL = re.compile(r"\bXAUUSD\b|\bXAGUSD\b|\bEURUSD\b|\bGBPUSD\b|\bUSDJPY\b|"
                     r"\bAUDUSD\b|\bUSDCAD\b|\bNZDUSD\b", re.IGNORECASE)


def symbol_in(text: str) -> Optional[str]:
    m = _SYMBOL.search(text)
    return m.group(0).upper() if m else None


def classify(text: str) -> Optional[dict]:
    """Classify a group message.

    Returns a dict with a `kind` and optional details, or None when the
    message is not a management instruction:

      {"kind": "tp_hit", "tp": 1}
      {"kind": "sl_hit"}
      {"kind": "protect"}        # close first entries + move SL above best
      {"kind": "close_tp1"}      # close leg 1 only (partial profit)
      {"kind": "close_tp", "tp": 2}  # close leg N only (Close TP2..TP6)
      {"kind": "close_all"}      # out at BE / we're out / close the setup
      {"kind": "move_sl"}        # move SL to a level (or nudge)
      {"kind": "summary"}        # periodic win-rate report
      {"kind": "summary_correction"}  # R1 correction linked to a summary
      {"kind": "recount"}        # per-trade recap ("Signal #1: Sell at 4357 … +110 pips")

    Order matters: TP/SL hits are checked before the looser exit patterns so
    "TP1 HIT +70 pips" never matches a "close" rule. Recounts are ground
    truth per-trade reports — they must be classified before the chatter
    filter discards them.
    """
    text = (text or "").strip()
    if not text:
        return None
    # the group types curly apostrophes (we’re); normalize so the \bwe're out\b
    # patterns match real messages
    text = text.replace("\u2019", "'").replace("\u2018", "'")

    # Summaries first: they may contain "volume" / "watch" words later in the
    # text, which must not suppress the ground-truth report. R1 corrections
    # have their own kind so the state layer can resolve and update a target.
    has_summary_numbers = re.search(
        r"trades?\s*sent|winning\s+trades?|win\s*rate",
        text, re.IGNORECASE,
    )
    if _SUMMARY_CORRECTION.search(text) and has_summary_numbers:
        return {"kind": "summary_correction"}
    if _SUMMARY.search(text) and has_summary_numbers:
        return {"kind": "summary"}

    # R2: per-trade recounts ("Signal #1: Sell at 4357. Price went up to
    # 4362, then dropped to 4351. +110 pips") are structured ground truth
    # for the day's real fills. Checked before the chatter filter so they
    # never land in `failed`/ignored.
    if parse_recount(text):
        return {"kind": "recount"}

    if _SKIP_WORDS.search(text) and not (_TP_HIT.search(text) or _TP_EMOJI.search(text)):
        return None

    m = _SL_HIT.search(text) or _SL_HIT_LOOSE.search(text)
    if m:
        return {"kind": "sl_hit"}

    m = _TP_HIT.search(text)
    if m:
        return {"kind": "tp_hit", "tp": max(int(m.group(1)), int(m.group(2) or 0))}
    m = _TP_EMOJI.search(text) or _COUNT_TP.search(text)
    if m:
        return {"kind": "tp_hit", "tp": int(m.group(1))}
    m = _TP_HIT_LOOSE.search(text)
    if m:
        return {"kind": "tp_hit", "tp": int(m.group(1))}

    # Full exits first (they contain "out"/"close this" too).
    if (_OUT.search(text) or _CLOSE_SETUP.search(text)
            or _OUT_OF.search(text) or _OUT_ENTRY.search(text)):
        return {"kind": "close_all",
                "be": bool(_OUT_BE.search(text) or _OUT_ENTRY.search(text))}
    if _CLOSE_BE.search(text):
        return {"kind": "close_all", "be": True}

    # Partial profit: "Close TP N here" (N>=2) closes only leg N; "Close TP1
    # here" / "close first entries" (WITHOUT a stop-loss/risk-free
    # instruction) closes only leg 1 (remaining legs keep their SL).
    m = _CLOSE_TP_N.search(text)
    if m and int(m.group(1)) >= 2:
        return {"kind": "close_tp", "tp": int(m.group(1))}
    if _CLOSE_TP1_ONLY.search(text) and not _MOVE_OR_RISKFREE.search(text):
        return {"kind": "close_tp1"}

    # Protection: close first entries + move the stop above the best entry
    # ("risk free"), or the explicit move-stop instruction.
    if (_CLOSE_FIRST.search(text) and _MOVE_SL.search(text)
            and _ABOVE_BELOW.search(text)):
        return {"kind": "protect"}
    if _CLOSE_FIRST.search(text):
        return {"kind": "protect"}
    if _RISK_FREE.search(text) or _SECURE.search(text) or _SET_BE.search(text):
        return {"kind": "protect"}
    if _MOVE_SL.search(text) and _ABOVE_BELOW.search(text):
        return {"kind": "protect"}

    # "Adjust SL to 4331" / "Move SL to 4064" — explicit stop-loss move with
    # a level. Must come AFTER the risk-free/protect patterns (which also
    # contain "move stop loss") so those stay protect.
    m = _MOVE_SL_TO.search(text)
    if m:
        return {"kind": "move_sl", "sl": float(m.group(1).replace(",", "."))}
    # "adjust SL" / "move SL" / "SL adjusted" without a level — a nudge, no
    # action (never invents a level; only registers).
    if _MOVE_SL_NUDGE.search(text) or _SL_ADJUSTED.search(text):
        return {"kind": "move_sl"}

    # GO: "Do it now team" — final entry confirmation. Checked last (nothing
    # closes/risks positions, so a false positive only finalizes an awaiting
    # signal early — which auto would execute anyway).
    if _GO.search(text):
        return {"kind": "go"}

    return None
