"""Forward shadow-paper regressions.

Shadow paper is allowed to observe live signals while the principal is manual,
kill-switched, or otherwise blocked, but it must never create a real trade.
"""

import asyncio

import pytest

from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.execution.paper import PaperRace
from app.models import ConfigUpdate, Signal, SignalIn
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager


_SIGNAL_TEXT = "BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920"


def _signal(signal_id: str = "shadow-sig", message_id: str | None = None) -> Signal:
    return Signal(
        id=signal_id,
        source="shadow-test",
        raw_text=_SIGNAL_TEXT,
        symbol="EURUSD",
        direction="buy",
        entry=1.0850,
        stop_loss=1.0820,
        take_profit_1=1.0890,
        take_profit_2=1.0920,
        status="pending",
        received_at="2026-08-19T10:00:00+00:00",
        message_id=message_id,
    )


def _state(db: DB, market: MarketSimulator) -> BotState:
    # BotState snapshots expect the richer production feed interface; the
    # isolated unit tests do not need network prices, so provide its two
    # read-only metadata hooks explicitly.
    market.real_data_active = False
    market.meta = lambda: {}
    return BotState(
        db=db,
        market=market,
        risk=RiskEngine(),
        sim=SimEngine(db, market),
        notifier=Notifier(db, WSManager()),
        ws=WSManager(),
    )


def test_shadow_open_is_idempotent_and_never_duplicates(tmp_path):
    db = DB(str(tmp_path / "shadow.db"))
    try:
        market = MarketSimulator()
        race = PaperRace(db, market)
        signal = _signal()

        first = race.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], origin="shadow"
        )
        second = race.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], origin="shadow"
        )

        assert len(first) == 1
        assert second == []
        row = db.paper_trade_for_signal_any(signal.id, "auto_escada_3")
        assert row is not None
        assert row["origin"] == "shadow"
        assert len(db.paper_open_trades("auto_escada_3")) == 1
    finally:
        db.close()


def test_shadow_and_race_same_signal_have_separate_trade_and_account(tmp_path):
    db = DB(str(tmp_path / "two-origins.db"))
    try:
        race = PaperRace(db, MarketSimulator())
        signal = _signal("same-signal")

        normal = race.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], origin="race"
        )
        shadow = race.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], origin="shadow"
        )

        assert len(normal) == 1 and len(shadow) == 1
        assert normal[0].id != shadow[0].id
        assert db.paper_trade_for_signal_any(
            signal.id, "auto_escada_3", origin="race"
        )["origin"] == "race"
        assert db.paper_trade_for_signal_any(
            signal.id, "auto_escada_3", origin="shadow"
        )["origin"] == "shadow"
        assert len(db.paper_open_trades("auto_escada_3", origin="race")) == 1
        assert len(db.paper_open_trades("auto_escada_3", origin="shadow")) == 1

        db.update_paper_balance("auto_escada_3", 900.0, origin="shadow")
        assert db.paper_balance("auto_escada_3", origin="race") == 1000.0
        assert db.paper_balance("auto_escada_3", origin="shadow") == 900.0
    finally:
        db.close()


def test_live_execution_keeps_shadow_and_normal_race_for_same_strategy(tmp_path):
    db = DB(str(tmp_path / "live-two-origins.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["mode"] = "auto"
        state.config["kill_switch"] = False
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["auto_escada_3"]
        state.config["strategy"] = "auto_escada_3"

        result = asyncio.run(state.ingest(SignalIn(
            raw_text=_SIGNAL_TEXT, source="shadow-test"
        )))

        assert result["action"] == "executed"
        assert len(db.open_trades()) == 1
        assert len(db.paper_open_trades("auto_escada_3", origin="shadow")) == 1
        assert len(db.paper_open_trades("auto_escada_3", origin="race")) == 1
    finally:
        db.close()


def test_shadow_survives_principal_kill_switch_without_real_trade(tmp_path):
    db = DB(str(tmp_path / "state.db"))
    try:
        market = MarketSimulator()
        state = _state(db, market)
        state.config["mode"] = "auto"
        state.config["kill_switch"] = True
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["auto_escada_3"]

        result = asyncio.run(state.ingest(SignalIn(
            raw_text=_SIGNAL_TEXT, source="shadow-test"
        )))

        assert result["action"] == "blocked"
        assert db.open_trades() == []
        shadow = db.paper_open_trades("auto_escada_3")
        assert len(shadow) == 1
        assert shadow[0]["origin"] == "shadow"
    finally:
        db.close()


def test_shadow_config_is_persisted_and_historical_replay_is_ignored(tmp_path):
    db = DB(str(tmp_path / "config.db"))
    try:
        market = MarketSimulator()
        state = _state(db, market)
        updated = asyncio.run(state.update_config(ConfigUpdate(
            shadow_paper_enabled=True,
            shadow_paper_strategies=["auto_escada_3", "auto_escada_5"],
        )))
        assert updated["config"]["shadow_paper_enabled"] is True
        assert updated["config"]["shadow_paper_strategies"] == [
            "auto_escada_3", "auto_escada_5"
        ]

        restored = _state(db, MarketSimulator())
        assert restored.config["shadow_paper_enabled"] is True
        assert restored.config["shadow_paper_strategies"] == [
            "auto_escada_3", "auto_escada_5"
        ]

        result = asyncio.run(restored.ingest(SignalIn(
            raw_text=_SIGNAL_TEXT,
            source="shadow-test",
            message_id="history-1",
            group="group-1",
            historical=True,
        )))
        assert result["action"] == "historical"
        assert db.paper_open_trades("auto_escada_3") == []
        assert db.paper_open_trades("auto_escada_5") == []
    finally:
        db.close()


def test_shadow_pending_management_has_separate_real_marker(tmp_path):
    db = DB(str(tmp_path / "pending.db"))
    try:
        market = MarketSimulator()
        state = _state(db, market)
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        signal = _signal("tg-group-42", message_id="42")
        db.insert_signal(signal)
        db.insert_pending_management({
            "id": "tg-group-mgmt-99",
            "signal_id": "tg-group-mgmt-99",
            "reply_to_msg_id": "42",
            "kind": "close_all",
            "raw_text": "We're out",
            "source": "shadow-test",
            "received_at": "2026-08-19T10:00:01+00:00",
            "created_at": "2026-08-19T10:00:01+00:00",
        })

        opened = asyncio.run(state._open_shadow_for_signal(signal))

        assert len(opened) == 1
        paper_row = db.paper_trade_for_signal_any(signal.id, "nova_gestao")
        assert paper_row is not None and paper_row["status"] == "closed"
        real_pending = db.pending_management_for("42", engine="real")
        paper_pending = db.pending_management_for("42", engine="paper")
        shadow_pending = db.pending_management_for("42", engine="shadow")
        assert len(real_pending) == 1
        assert len(paper_pending) == 1
        assert shadow_pending == []
        assert real_pending[0]["paper_applied"] == 0
        assert real_pending[0]["shadow_applied"] == 1
        assert real_pending[0]["applied"] == 0
    finally:
        db.close()


def test_shadow_pending_management_does_not_consume_race_paper_delivery(tmp_path):
    db = DB(str(tmp_path / "pending-two-streams.db"))
    try:
        market = MarketSimulator()
        state = _state(db, market)
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        state.config["strategy"] = "nova_gestao"
        signal = _signal("tg-group-two-streams", message_id="42")
        db.insert_signal(signal)
        db.insert_pending_management({
            "id": "tg-group-two-streams-mgmt-99",
            "signal_id": "tg-group-two-streams-mgmt-99",
            "reply_to_msg_id": "42",
            "kind": "protect",
            "raw_text": "close first entries and move stop loss above best entries",
            "source": "shadow-test",
            "received_at": "2026-08-19T10:00:01+00:00",
            "created_at": "2026-08-19T10:00:01+00:00",
        })

        asyncio.run(state._open_shadow_for_signal(signal))
        shadow = db.paper_trade_for_signal_any(signal.id, "nova_gestao")
        assert shadow is not None
        shadow_positions = db.paper_positions_for_trade(shadow["id"])
        assert next(p for p in shadow_positions if p.leg == 1).status == "closed"

        # A different strategy in the normal race opens after the shadow
        # stream has consumed its own marker. The pending event must still be
        # delivered to this race trade.
        race = state.paper.open_for_signal(
            signal, 2.0, strategy_ids=["tp2_protect"], origin="race"
        )
        assert len(race) == 1
        real = state.sim.open_trade(signal, 2.0, state._balance())
        awaitable = state._apply_pending_management(signal, real)
        asyncio.run(awaitable)

        race_row = db.paper_trade_for_signal_any(signal.id, "tp2_protect")
        assert race_row is not None
        positions = db.paper_positions_for_trade(race_row["id"])
        assert next(p for p in positions if p.leg == 1).status == "closed"
        pending = db.conn.execute(
            "SELECT shadow_applied, paper_applied, applied "
            "FROM pending_management WHERE reply_to_msg_id = ?", ("42",)
        ).fetchone()
        assert pending["shadow_applied"] == 1
        assert pending["paper_applied"] == 1
        assert pending["applied"] == 1
    finally:
        db.close()


def test_config_can_clear_shadow_strategies_when_disabled(tmp_path):
    db = DB(str(tmp_path / "clear-shadow.db"))
    try:
        state = _state(db, MarketSimulator())
        result = asyncio.run(state.update_config(ConfigUpdate(
            shadow_paper_enabled=False,
            shadow_paper_strategies=[],
        )))
        assert result["ok"] is True
        assert result["config"]["shadow_paper_enabled"] is False
        assert result["config"]["shadow_paper_strategies"] == []
    finally:
        db.close()


def test_invalid_shadow_update_does_not_partially_mutate_runtime(tmp_path):
    db = DB(str(tmp_path / "invalid-shadow.db"))
    try:
        state = _state(db, MarketSimulator())
        before = list(state.config["shadow_paper_strategies"])
        result = asyncio.run(state.update_config(ConfigUpdate(
            shadow_paper_enabled=False,
            shadow_paper_strategies=["not-a-strategy"],
        )))
        assert "error" in result
        assert state.config["shadow_paper_enabled"] is True
        assert state.config["shadow_paper_strategies"] == before
    finally:
        db.close()


def test_historical_immediate_and_llm_signals_never_open_shadow(tmp_path):
    db = DB(str(tmp_path / "history.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["auto_escada_3"]

        result = asyncio.run(state.ingest(SignalIn(
            raw_text=_SIGNAL_TEXT, source="shadow-test", historical=True
        )))
        assert result["action"] == "historical"
        assert db.paper_open_trades("auto_escada_3") == []

        llm_signal = _signal("llm-sig")
        llm_signal.llm_parsed = True
        assert asyncio.run(state._open_shadow_for_signal(llm_signal)) == []
        assert db.paper_open_trades("auto_escada_3") == []

        buffered = asyncio.run(state.ingest(SignalIn(
            raw_text="BUY EURUSD @ 1.0850", source="shadow-test",
            message_id="active-edit",
        )))
        assert buffered["action"] == "buffered"
        replay = asyncio.run(state.ingest(SignalIn(
            raw_text=_SIGNAL_TEXT, source="shadow-test",
            message_id="active-edit", historical=True,
        )))
        assert replay["action"] == "ignored"
        assert db.paper_open_trades("auto_escada_3") == []
    finally:
        db.close()


@pytest.mark.skip(reason="auto_protect_5 foi removida do registo a 01/09 (ver CHANGELOG); o shadow da auto_escada_3 é coberto por outros testes")
def test_shadow_tick_applies_auto_protect_without_management_message(tmp_path):
    db = DB(str(tmp_path / "shadow-tick.db"))
    try:
        market = MarketSimulator()
        paper = PaperRace(db, market)
        signal = _signal("shadow-auto-protect")
        opened = paper.open_for_signal(
                signal, 2.0, strategy_ids=["auto_escada_3"], origin="shadow"
            )
        assert len(opened) == 1
        market.prices["EURUSD"] = 1.0880  # 75% of entry -> TP1
        events = paper.tick(2.0)
        positions = {p.leg: p for p in db.paper_positions_for_trade(opened[0].id)}
        assert any(e.get("reason") == "auto_protect" for e in events)
        assert positions[1].status == "closed"
        assert positions[1].exit_reason == "auto_protect"
        assert positions[1].close_price == 1.088
        assert positions[2].stop_loss == 1.0853
    finally:
        db.close()


def test_shadow_status_counts_only_shadow_origin(tmp_path):
    db = DB(str(tmp_path / "origins.db"))
    try:
        state = _state(db, MarketSimulator())
        state.paper.open_for_signal(
            _signal("race-sig"), 2.0,
            strategy_ids=["auto_escada_3"], origin="race",
        )
        state.paper.open_for_signal(
            _signal("shadow-sig-2"), 2.0,
            strategy_ids=["auto_escada_3"], origin="shadow",
        )
        state.config["shadow_paper_strategies"] = ["auto_escada_3"]
        assert state.shadow_paper_status()["open_trades"] == 1
    finally:
        db.close()


def test_shadow_management_is_applied_even_with_real_trade_open(tmp_path):
    db = DB(str(tmp_path / "management.db"))
    try:
        market = MarketSimulator()
        state = _state(db, market)
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        signal = _signal("tg-group-42", message_id="42")
        db.insert_signal(signal)
        state.paper.open_for_signal(
            signal, 2.0, strategy_ids=["nova_gestao"], origin="shadow"
        )
        real = state.sim.open_trade(signal, 2.0, state._balance())
        assert real.status == "open"

        result = asyncio.run(state._handle_management(
            SignalIn(raw_text="We're out", source="shadow-test", message_id="99",
                     group="group", reply_to_msg_id=42),
            {"kind": "close_all", "be": False},
            "tg-group-99", "2026-08-19T10:00:02+00:00",
        ))

        assert result["applied"] is True
        assert result["shadow_applied"] is True
        assert db.open_trades() == []
        assert db.paper_open_trades("nova_gestao", origin="shadow") == []
    finally:
        db.close()


def test_shadow_tp_sl_update_mirrors_open_levels(tmp_path):
    db = DB(str(tmp_path / "levels.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        signal = _signal("tg-group-42", message_id="42")
        db.insert_signal(signal)
        state.paper.open_for_signal(
            signal, 2.0, strategy_ids=["nova_gestao"], origin="shadow"
        )

        result = asyncio.run(state._handle_tp_sl_update(
            SignalIn(raw_text="TP1 1.0900 SL 1.0830", source="shadow-test",
                     message_id="100", group="group", reply_to_msg_id=42),
            "tg-group-100", "2026-08-19T10:00:03+00:00",
        ))

        assert result["shadow_applied"] is True
        paper = db.paper_trade_for_signal_any(signal.id, "nova_gestao")
        assert paper["take_profit_1"] == 1.09
        assert paper["stop_loss"] == 1.083
        positions = db.paper_positions_for_trade(paper["id"])
        assert all(p.stop_loss == 1.083 for p in positions if p.status == "open")
        assert next(p for p in positions if p.leg == 1).take_profit == 1.09
    finally:
        db.close()


def test_shadow_pending_be_preserves_the_breakeven_semantics(tmp_path):
    db = DB(str(tmp_path / "pending-be.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        signal = _signal("tg-group-be-42", message_id="42")
        db.insert_signal(signal)
        db.insert_pending_management({
            "id": "tg-group-be-mgmt-99",
            "signal_id": "tg-group-be-mgmt-99",
            "reply_to_msg_id": "42",
            "kind": "close_all",
            "be": True,
            "raw_text": "Out at BE",
            "source": "shadow-test",
            "received_at": "2026-08-19T10:00:01+00:00",
            "created_at": "2026-08-19T10:00:01+00:00",
        })

        opened = asyncio.run(state._open_shadow_for_signal(signal))
        assert len(opened) == 1
        paper = db.paper_trade_for_signal_any(signal.id, "nova_gestao")
        positions = db.paper_positions_for_trade(paper["id"])
        assert paper["status"] == "open"
        assert all(p.stop_loss == signal.entry for p in positions if p.leg != 1)
    finally:
        db.close()


def test_shadow_management_refuses_ambiguous_symbol_fallback(tmp_path):
    db = DB(str(tmp_path / "management-fallback.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        older = _signal("older-shadow", message_id="41")
        newer = _signal("newer-shadow", message_id="42")
        state.paper.open_for_signal(
            older, 2.0, strategy_ids=["nova_gestao"], origin="shadow"
        )
        state.paper.open_for_signal(
            newer, 2.0, strategy_ids=["nova_gestao"], origin="shadow"
        )
        # `_now_iso()` intentionally stores second precision; make the
        # recency distinction explicit rather than relying on insertion order.
        with db.tx() as cur:
            cur.execute(
                "UPDATE paper_trades SET opened_at = ? WHERE signal_id = ?",
                ("2026-08-19T10:00:01+00:00", older.id),
            )
            cur.execute(
                "UPDATE paper_trades SET opened_at = ? WHERE signal_id = ?",
                ("2026-08-19T10:00:03+00:00", newer.id),
            )

        result = asyncio.run(state._handle_management(
            SignalIn(raw_text="We're out", source="shadow-test", message_id="99",
                     group="group"),
            {"kind": "close_all", "be": False},
            "tg-group-99", "2026-08-19T10:00:02+00:00",
        ))

        # Two open trades share the symbol and the message has no reply: the
        # fail-safe router must not guess which real/shadow trade to close.
        assert result.get("shadow_applied") is not True
        assert db.paper_trade_for_signal_any(
            older.id, "nova_gestao", origin="shadow"
        )["status"] == "open"
        assert db.paper_trade_for_signal_any(
            newer.id, "nova_gestao", origin="shadow"
        )["status"] == "open"
    finally:
        db.close()


def test_shadow_pending_reply_does_not_redirect_to_unrelated_trade(tmp_path):
    db = DB(str(tmp_path / "pending-target.db"))
    try:
        state = _state(db, MarketSimulator())
        state.config["shadow_paper_enabled"] = True
        state.config["shadow_paper_strategies"] = ["nova_gestao"]
        unrelated = _signal("unrelated-shadow", message_id="41")
        state.paper.open_for_signal(
            unrelated, 2.0, strategy_ids=["nova_gestao"], origin="shadow"
        )
        target = _signal("tg-group-42", message_id="42")
        target.status = "awaiting_edit"
        target.edit_deadline = "2026-08-19T10:00:08+00:00"
        db.insert_signal(target)

        result = asyncio.run(state._handle_management(
            SignalIn(raw_text="We're out", source="shadow-test", message_id="99",
                     group="group", reply_to_msg_id=42),
            {"kind": "close_all", "be": False},
            "tg-group-99", "2026-08-19T10:00:02+00:00",
        ))

        assert result["queued"] is True
        assert result["shadow_applied"] is False
        assert db.paper_trade_for_signal_any(
            unrelated.id, "nova_gestao", origin="shadow"
        )["status"] == "open"
        pending = db.pending_management_for("42", engine="shadow")
        assert len(pending) == 1
        assert pending[0]["shadow_applied"] == 0
    finally:
        db.close()


def test_shadow_divergence_never_reads_race_reference(tmp_path):
    db = DB(str(tmp_path / "divergence-origins.db"))
    try:
        paper = PaperRace(db, MarketSimulator())
        signal = _signal("origin-divergence")
        paper.open_for_signal(
            signal, 2.0, strategy_ids=["nova_gestao"], origin="race"
        )
        paper.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], origin="shadow"
        )

        # Close only the race reference. The shadow auto has no shadow
        # reference, so it must not emit a cross-origin pending divergence.
        paper.apply_management(signal.id, "close_all", origin="race")
        shadow_auto = db.paper_trade_for_signal_any(
            signal.id, "auto_escada_3", origin="shadow"
        )
        assert shadow_auto is not None
        assert paper._divergences(shadow_auto) == []
    finally:
        db.close()


def test_shadow_daily_stop_zero_means_off_isolated_from_race(tmp_path):
    """A origem shadow-paper tem de respeitar daily_stop_pct=0 (sem limite
    diário) tal como a corrida normal: 0 = off, e a origem shadow tem de
    ser avaliada contra o P&L realizado da própria origem — não pode ser
    contaminada por perdas na race nem contaminar a race.

    Cobertura em três camadas:
      - kill > 0 e a race já estourou → shadow abre (origens isoladas);
      - kill > 0 e a shadow já estourou → shadow não abre;
      - kill == 0 → abre sempre, mesmo com P&L negativo nas duas origens.
    """
    from datetime import datetime, timezone
    db = DB(str(tmp_path / "shadow-daily-zero.db"))
    try:
        mkt = MarketSimulator()
        mkt.ensure("EURUSD", 1.0850)
        race = PaperRace(db, mkt)
        today = datetime.now(timezone.utc).date().isoformat()
        sid = "auto_escada_3"
        # saldo virtual 1000 €; -60 € de perda realizada hoje é 6% (>5%)
        db.ensure_paper_account(sid, 1000.0, origin="race")
        db.update_paper_balance(sid, 1000.0, origin="race")
        db.ensure_paper_account(sid, 1000.0, origin="shadow")
        db.update_paper_balance(sid, 1000.0, origin="shadow")

        # perda realizada HOJE só na origem shadow (a race continua limpa)
        from app.models import Trade
        db.insert_paper_trade(Trade(
            id="shadow-stoploss-1", strategy_id=sid, signal_id="prior-sig",
            symbol="EURUSD", direction="buy", entry=1.0850, stop_loss=1.0820,
            take_profit_1=1.0890, take_profit_2=1.0920, risk_pct=5.0,
            lots_1=0.1, lots_2=0.1, status="closed",
            opened_at=today + "T09:00:00", closed_at=today + "T10:00:00",
            pnl=-60.0, exit_reason="sl", paper_origin="shadow",
        ))

        sig = _signal("shadow-zero-stop")

        # kill 5% activo, só a SHADOW estourou:
        # a race (P&L=0) abre e a shadow (-60 ≤ -50) NÃO abre.
        opened_5 = race.open_for_signal(
            sig, 5.0, strategy_ids=[sid], daily_stop_pct=5.0, origin="shadow",
        )
        assert opened_5 == [], \
            "com kill > 0 e P&L negativo da shadow, o sinal não devia abrir"
        assert race._skipped_daily_stop.get(f"shadow:{sid}") == 1

        # sem limite (kill=0) a mesma shadow abre, ignorando a perda de hoje
        # (não é uma excepção só para a race — é o mesmo path).
        opened_0 = race.open_for_signal(
            sig, 5.0, strategy_ids=[sid], daily_stop_pct=0, origin="shadow",
        )
        assert len(opened_0) == 1
        assert opened_0[0].strategy_id == sid
        assert db.paper_trade_for_signal_any(
            sig.id, sid, origin="shadow",
        )["origin"] == "shadow"

        # e a race continua intacta: zero=off também lá.
        # limpa a trade aberta para não interferir com o próximo assert
        for t in opened_0:
            with db.tx() as cur:
                cur.execute(
                    "DELETE FROM paper_positions WHERE trade_id=?", (t.id,),
                )
                cur.execute("DELETE FROM paper_trades WHERE id=?", (t.id,))
        race_open = race.open_for_signal(
            sig, 5.0, strategy_ids=[sid], daily_stop_pct=0,
        )
        assert len(race_open) == 1
        assert race_open[0].strategy_id == sid
        for t in race_open:
            with db.tx() as cur:
                cur.execute(
                    "DELETE FROM paper_positions WHERE trade_id=?", (t.id,),
                )
                cur.execute("DELETE FROM paper_trades WHERE id=?", (t.id,))
    finally:
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM paper_trades WHERE id='shadow-stoploss-1'"
            )
            cur.execute(
                "DELETE FROM paper_accounts WHERE strategy_id=?", (sid,),
            )
        db.close()
