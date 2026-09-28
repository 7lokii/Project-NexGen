"""Tests for the group management state machine.

Covers: the management-message classifier (TP HIT / SL HIT / close first
entries / out at BE), the summary ground-truth parser, and the LLM fallback
(sanity checks + minimum confidence + never auto-executes).
"""

import os
import tempfile

import pytest

os.environ["SIGNALBOT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["MARKET_PROVIDER"] = "none"

from app.messages.manage import classify, symbol_in  # noqa: E402
from app.messages.recounts import parse_recount  # noqa: E402
from app.messages.summaries import parse_summary  # noqa: E402
from app.signals.llm_fallback import (DEFAULT_MIN_CONFIDENCE,  # noqa: E402
                                      try_llm_parse)


# ------------------------------------------------------------- classifier

def test_classify_tp_hit():
    assert classify("TP1 HIT") == {"kind": "tp_hit", "tp": 1}
    assert classify("TP2 HIT  +60 pips") == {"kind": "tp_hit", "tp": 2}
    assert classify("TP5 HIT  Full target 🎯") == {"kind": "tp_hit", "tp": 5}
    assert classify("Well, we’ll count that as TP2. I can see that spike down") == \
        {"kind": "tp_hit", "tp": 2}


def test_classify_tp_hit_multiple_and_emoji():
    """O grupo anuncia às vezes dois targets de uma vez ("TP4 and 5 HIT")
    ou com o emoji 👀 em vez da palavra HIT — ambos são tp_hit (o mais
alto). Uma pergunta com 👀 nunca é ação."""
    assert classify("Wow TP4 and 5 HIT if you are not taken out from lowest price") \
        == {"kind": "tp_hit", "tp": 5}
    assert classify("TP4 👀 ?   Typical gold, as I know we secured fully profit at TP3 "
                    "Good day so far team  👊🏼") == {"kind": "tp_hit", "tp": 4}
    assert classify("Who’s with me for TP3 👀") is None
    assert classify("TP2 anyone??  Hit the 👍🏻") is None


def test_classify_sl_hit():
    assert classify("SL HIT") == {"kind": "sl_hit"}
    assert classify("Oh wow   SL HIT") == {"kind": "sl_hit"}
    assert classify("SL HIT   THE bounce at 4326.50 was so fast") == {"kind": "sl_hit"}


def test_classify_close_tp1_partial():
    # "Close TP1 here" WITHOUT a stop-loss/risk-free instruction is a
    # partial profit step: leg 1 closes, remaining legs keep their SL.
    assert classify("Close TP1 here") == {"kind": "close_tp1"}
    assert classify("close your first entries") == {"kind": "close_tp1"}
    # with the "move stop loss" instruction it is the full protection step
    assert classify("Close first entries now and move stop loss above best entries") \
        == {"kind": "protect"}


def test_classify_close_first_protect():
    assert classify("Close first entries now and move stop loss above best entries") \
        == {"kind": "protect"}
    assert classify("+35 pips from best entires  I’m closing my first entires "
                    "and move my stop loss above my best entires now") == {"kind": "protect"}
    assert classify("Let’s make this trade risk free now, again tp1 was hit so fast") \
        == {"kind": "protect"}


def test_classify_close_all():
    # "out at BE" carries the breakeven exit flag
    assert classify("Out at BE after we secured TP1") == {"kind": "close_all", "be": True}
    assert classify("we out at BE after we secured TP2") == {"kind": "close_all", "be": True}
    assert classify("**Okay, we’re out after we secured TP2.**") == {"kind": "close_all", "be": False}
    assert classify("Let’s close this set up here and we wait and looking for better entry") \
        == {"kind": "close_all", "be": False}


def test_classify_move_sl():
    """\"Adjust SL to 4331\" / \"Move SL to 4064\" — o grupo move o stop
    loss de uma trade aberta para um nível específico (trailing/risco)."""
    assert classify("Adjust SL to 4331") == {"kind": "move_sl", "sl": 4331.0}
    assert classify("Move SL to 4064") == {"kind": "move_sl", "sl": 4064.0}
    assert classify("Move SL to 4026 as most had better entries") == \
        {"kind": "move_sl", "sl": 4026.0}
    assert classify("Let’s adjust SL to 4089") == {"kind": "move_sl", "sl": 4089.0}
    assert classify("Adjust SL 4051") == {"kind": "move_sl", "sl": 4051.0}
    assert classify("Move SL to 75") == {"kind": "move_sl", "sl": 75.0}
    # sem nível: nudge registado, sem ação
    assert classify("If you are still in trade and risk management allows you can adjust SL") \
        == {"kind": "move_sl"}


def test_classify_move_sl_not_protect():
    """'move stop loss above best entries' continua a ser protect (tem
    above/below + risco), não move_sl."""
    assert classify("Close first entries now and move stop loss above best entries") \
        == {"kind": "protect"}
    assert classify("Move stop loss above your lowest entries") == {"kind": "protect"}


def test_classify_close_tp_n_partial():
    """'Close TP2 here' / 'Close TP5 when happy' — partial profit numa perna
    específica (TP2..TP6); 'Close TP1' continua a ser close_tp1."""
    assert classify("Close TP2 here") == {"kind": "close_tp", "tp": 2}
    assert classify("Close TP5 when happy") == {"kind": "close_tp", "tp": 5}
    assert classify("Close TP6 when happy") == {"kind": "close_tp", "tp": 6}
    assert classify("+100 pips  Close TP5 when happy") == {"kind": "close_tp", "tp": 5}
    assert classify("Close TP1 here") == {"kind": "close_tp1"}


def test_classify_tp_hit_loose_verbs():
    """O grupo anuncia TP hit com verbos informais (SMASHED/KISSED/tapped)
    ou com HIT mal escrito (HITTTT) — todos tp_hit. Uma pergunta nunca é."""
    assert classify("TP5 SMASHED") == {"kind": "tp_hit", "tp": 5}
    assert classify("TP4 KISSED") == {"kind": "tp_hit", "tp": 4}
    assert classify("TP2 tapped") == {"kind": "tp_hit", "tp": 2}
    assert classify("TP5 SMASHEDDD **💵**") == {"kind": "tp_hit", "tp": 5}
    assert classify("TP4 HITTTT") == {"kind": "tp_hit", "tp": 4}
    assert classify("TP2 Now HIT") == {"kind": "tp_hit", "tp": 2}
    assert classify("HARRY KANE  TP5 SMASHED  WHAT!!!!!!") == {"kind": "tp_hit", "tp": 5}
    assert classify("TP2 anyone??  Hit the 👍🏻") is None


def test_classify_out_at_entry_and_of_setup():
    """'Out at entry' (exata) e 'Out of this setup' são close_all; o be
    só é True quando a saída é a breakeven. Condicionais não disparam."""
    assert classify("Out at entry") == {"kind": "close_all", "be": True}
    assert classify("Out of this setup after TP1 was secured.\n\nOn to the next one.") \
        == {"kind": "close_all", "be": False}
    # condicional ("if ... out at ...") NÃO é instrução
    assert classify("If you are out at breakeven you can reenter with a stop loss of 4537") is None
    assert classify("If the market reverses and we get stopped out  at entry") is None


def test_classify_close_at_breakeven():
    assert classify("Close this at breakeven now and let’s wait for a better entry") \
        == {"kind": "close_all", "be": True}


def test_classify_set_breakeven_is_protect():
    """'Set breakeven / set break even' — proteger na entrada (protect)."""
    assert classify("Set breakeven for zero risk") == {"kind": "protect"}
    assert classify("+30 pips from entry\n\nSet breakeven for zero risk and take partials when happy") \
        == {"kind": "protect"}
    assert classify("+40 pips from best entry. \n\nClose your first entires now and set break even on your higher entires.") \
        == {"kind": "protect"}


def test_classify_sl_adjusted_edited_already_hit():
    assert classify("SL already hit") == {"kind": "sl_hit"}
    assert classify("SL adjusted") == {"kind": "move_sl"}
    assert classify("SL edited") == {"kind": "move_sl"}


def test_classify_go():
    """'Do it now team' — o GO do admin (entrar já) é gestão, não conversa."""
    assert classify("Do it now team") == {"kind": "go"}
    assert classify("do it now guys") == {"kind": "go"}
    assert classify("Enter now") == {"kind": "go"}
    # conversa normal não dispara GO
    assert classify("Good morning team") is None
    assert classify("Let’s do it this weekend") is None


def test_classify_summary_correction():
    assert classify("Correction: Weekly summary 7 Trades Sent 6 winning trades "
                    "1 Stop loss Win rate: 86%") == {"kind": "summary_correction"}
    assert classify("The summary numbers were wrong, 7 Trades Sent 6 winning trades") == \
        {"kind": "summary_correction"}
    assert classify("Correction: the market moved fast, no summary numbers here") is None


def test_classify_summary():
    assert classify("**Weekly summary ** **(10/08/26_14/08/26)** **Gold prime Signals ** "
                    "26 **Trades Sent** 23 **winning trades ** Win rate : 91%") \
        == {"kind": "summary"}
    assert classify("**Thursday summary ** **Gold prime Signals ** 6Trades Sent "
                    "6 winning trades 0 Stop loss Pips gained +530") == {"kind": "summary"}


def test_classify_recount():
    """R2: per-trade recounts ("Signal #1: Sell at 4357 … +110 pips") are
    structured ground truth — classified as `recount`, never ignored."""
    assert classify("Signal #1: Sell at 4357. Price went up to 4362, then "
                    "dropped to 4351. +110 pips") == {"kind": "recount"}
    assert classify("Signal 3: Buy at 4396.50. Price went down to 4392, then "
                    "came back up to 4401. +45 pips") == {"kind": "recount"}
    # without the full structure it stays chatter (no action)
    assert classify("Signal #1 was a great trade today") is None
    assert classify("We won +110 pips on gold") is None


def test_parse_recount():
    r = parse_recount("Signal #1: Sell at 4357. Price went up to 4362, "
                      "then dropped to 4351. +110 pips")
    assert r is not None
    assert r["signal_number"] == 1
    assert r["direction"] == "sell"
    assert r["entry"] == 4357.0
    assert r["excursion_price"] == 4362.0
    assert r["excursion_dir"] == "up"
    assert r["reversal_price"] == 4351.0
    assert r["pips"] == 110.0
    assert parse_recount("Good morning team") is None
    assert parse_recount("") is None


def test_classify_ignores_chatter_and_entries():
    assert classify("Volume increased try to get the move from sell") is None
    assert classify("Buy Stop 💣") is None
    assert classify("Sell Stop 💣") is None
    assert classify("TP2 anyone??  Hit the 👍🏻") is None
    assert classify("Happy Friday, team! 🔥 What a day we’ve had!") is None
    assert classify("https://www.tradingview.com/x/bNga5KME/  Here are some key zones") is None
    # real entries (with typos) must go to the regex parser, not management
    assert classify("I’m buying XAUUSD 4417-4413 TP1 4419 TP2 421 TP3 423 SL 4409") is None
    # a real valid entry is NOT a management message either
    assert classify("I’m buying XAUUSD 4397.50-4393 TP1 4400 TP2 4402 TP3 4404 "
                    "TP4 4408 TP5 4413 SL 4389.50") is None


def test_symbol_in():
    assert symbol_in("I’m buying XAUUSD 4400-4396 TP1 4402 SL 4392") == "XAUUSD"
    assert symbol_in("TP1 HIT") is None


# ------------------------------------------------------------ summaries

def test_parse_weekly_summary():
    s = parse_summary("**Weekly summary ** **(10/08/26_14/08/26)** "
                      "**Gold prime Signals ** 26 **Trades Sent** 23 **winning trades ** "
                      "2 Stop loss ** 1 BE ** Win rate : 91% **Pips gained +2100**")
    assert s is not None
    assert s["trades_sent"] == 26
    assert s["winning"] == 23
    assert s["stop_loss"] == 2
    assert s["breakevens"] == 1
    assert s["win_rate"] == 91.0
    assert s["pips"] == 2100
    assert s["period_start"] == "2026-08-10"
    assert s["period_end"] == "2026-08-14"


def test_parse_daily_summary_no_win_rate():
    s = parse_summary("**Monday summary ** **Gold prime Signals ** 5 Trades Sent "
                      "4 winning trades 1 Stop loss Pips gained +400")
    assert s is not None
    assert s["trades_sent"] == 5
    assert s["winning"] == 4
    # win rate derived from winning/trades
    assert s["win_rate"] == 80.0
    assert s["period_start"] is None


def test_parse_summary_ignores_non_summary():
    assert parse_summary("TP1 HIT") is None
    assert parse_summary("I’m buying XAUUSD 4400-4396 TP1 4402 SL 4392") is None


# -------------------------------------------------------------- LLM fallback

def test_llm_fallback_disabled_by_default(monkeypatch):
    import asyncio
    monkeypatch.delenv("SIGNALBOT_LLM_API_KEY", raising=False)
    assert asyncio.run(try_llm_parse("whatever")) is None


def test_llm_fallback_valid_signal_flagged(monkeypatch):
    import asyncio
    monkeypatch.setenv("SIGNALBOT_LLM_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"symbol":"XAUUSD","direction":"buy","entry":4397.5,'
                '"stop_loss":4389.5,"take_profit_1":4400.0,"take_profit_2":4402.0,'
                '"take_profit_3":4404.0,"take_profit_4":4408.0,"take_profit_5":4413.0,'
                '"confidence":0.95}')}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    sig = asyncio.run(try_llm_parse("I’m buying XAUUSD 4397.50-4393 TP1 4400 TP2 4402 "
                                    "TP3 4404 TP4 4408 TP5 4413 SL 4389.50"))
    assert sig is not None
    assert not sig.error
    assert sig.llm_parsed is True
    assert sig.symbol == "XAUUSD"
    assert sig.direction == "buy"
    assert sig.take_profit_5 == 4413.0
    monkeypatch.delenv("SIGNALBOT_LLM_API_KEY")


def test_llm_fallback_low_confidence_rejected(monkeypatch):
    import asyncio
    monkeypatch.setenv("SIGNALBOT_LLM_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"symbol":"XAUUSD","direction":"buy","entry":4397.5,'
                '"stop_loss":4389.5,"take_profit_1":4400.0,"confidence":0.5}')}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    sig = asyncio.run(try_llm_parse("some unclear message"))
    assert sig is not None
    assert sig.error and "confiança" in sig.error
    monkeypatch.delenv("SIGNALBOT_LLM_API_KEY")


def test_llm_fallback_invalid_signal_rejected(monkeypatch):
    """Even a confident LLM answer must pass the same sanity checks as regex
    (TPs ordered, stop on the right side). A wrong TP sequence is rejected."""
    import asyncio
    monkeypatch.setenv("SIGNALBOT_LLM_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"symbol":"XAUUSD","direction":"buy","entry":4397.5,'
                '"stop_loss":4389.5,"take_profit_1":4400.0,"take_profit_2":421.0,'
                '"confidence":0.99}')}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    import app.signals.llm_fallback as llm_mod
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    sig = asyncio.run(try_llm_parse("typo message"))
    assert sig is not None
    assert sig.error  # rejected, not traded
    monkeypatch.delenv("SIGNALBOT_LLM_API_KEY")
