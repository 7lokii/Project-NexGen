import pytest
from types import SimpleNamespace

from app.db import DB
from app.models import Signal
from app.state import BotState


def _state(price=4638.0):
    state = object.__new__(BotState)
    state.config = {
        "entry_guard_enabled": True,
        "entry_guard_tolerance": 0.5,
        "entry_guard_max_zone_widths": 5.0,
    }
    state.sim = SimpleNamespace(
        provider=SimpleNamespace(
            connected=True,
            quote=lambda symbol: {"bid": price - 0.2, "ask": price},
        )
    )
    return state


def _signal():
    return Signal(
        id="s1", source="telegram", raw_text="", symbol="XAUUSD",
        direction="buy", entry=4584.0, stop_loss=4578.0,
        take_profit_1=4586.0, take_profit_2=4588.0,
        received_at="2026-08-21T09:00:00+00:00",
        entry_zone_low=4580.0, entry_zone_high=4584.0,
        status="pending",
    )


def test_entry_guard_blocks_price_more_than_five_zone_widths_away():
    result = _state()._entry_guard(_signal())
    assert result["status"] == "blocked"
    assert result["market_price"] == 4638.0
    assert result["max_distance"] == 20.0


def test_entry_guard_allows_in_zone_and_marks_tolerated():
    assert _state(4582.0)._entry_guard(_signal())["status"] == "in_zone"
    assert _state(4600.0)._entry_guard(_signal())["status"] == "tolerated"


def test_entry_guard_auto_tolerates_small_price_deviation():
    result = _state(4584.4)._entry_guard(_signal())
    assert result["status"] == "tolerated"
    assert result["auto_execute"] is True
    assert result["tolerance"] == 0.5
    assert result["distance"] == pytest.approx(0.4)


def test_entry_guard_requires_confirmation_above_auto_tolerance():
    result = _state(4584.6)._entry_guard(_signal())
    assert result["status"] == "tolerated"
    assert result["reason"].endswith("confirmação manual necessária")


def test_group_audit_is_idempotent_and_passive(tmp_path):
    db = DB(str(tmp_path / "audit.db"))
    try:
        common = dict(message_id="4265", source="tg:g", signal_id="s1",
                      kind="level_mention", reported_level=4600.0,
                      inferred_tp=4600.0, confidence="medium",
                      raw_text="Let's say hi to 4600", received_at="2026-08-21T09:13:35+00:00")
        first = db.record_group_audit(**common)
        second = db.record_group_audit(**common)
        rows = db.list_group_audits()
        assert first == second
        assert len(rows) == 1
        assert rows[0]["inferred_tp"] == 4600.0
    finally:
        db.close()


def _gold_signal(n=5, entry=4596.0, sl=4589.0):
    tps = {1: entry + 2, 2: entry + 4, 3: entry + 7, 4: entry + 11, 5: entry + 24}
    kwargs = dict(id="sg", source="tg", raw_text="", symbol="XAUUSD",
                  direction="buy", entry=entry, stop_loss=sl,
                  received_at="2026-08-21T10:00:00+00:00", status="pending",
                  entry_zone_low=entry - 4, entry_zone_high=entry)
    for i in range(1, n + 1):
        kwargs[f"take_profit_{i}"] = tps[i]
    return Signal(**kwargs)


def test_min_lot_risk_gate_blocks_when_min_lot_exceeds_risk():
    gate = _state()._min_lot_risk_gate(_gold_signal(5), 1000.0, 1.5)
    assert gate["exceeded"] is True
    assert gate["max_loss_est"] > gate["risk_amount"]
    assert gate["legs"] == 5
    assert gate["lots_per_leg"] == 0.01


def test_min_lot_risk_gate_allows_when_lots_are_properly_sized():
    gate = _state()._min_lot_risk_gate(_gold_signal(5), 100000.0, 1.5)
    assert gate["exceeded"] is False
    assert gate["lots_per_leg"] > 0.01


def test_min_lot_risk_gate_does_not_block_fixed_lots():
    sig = _gold_signal(5)
    sig.lots = 0.02
    gate = _state()._min_lot_risk_gate(sig, 1000.0, 1.5)
    assert gate["exceeded"] is False
    assert gate["fixed_lots"] is True


def test_fit_legs_to_risk_reduces_legs_to_budget():
    """Auto-fit: quando o lote mínimo domina, reduz o nº de pernas até o
    risco caber no orçamento (modo autónomo — nunca fica pendente)."""
    sig = _gold_signal(5, entry=4596.0, sl=4589.0)  # dist 7 → 0.01*7*100 = 7 €/perna
    st = _state()
    # 1000 € a 1.5% = 15 € → 15/7 = 2 pernas
    assert st._fit_legs_to_risk(sig, 1000.0, 1.5) == 2
    # 1000 € a 4% = 40 € → cabem as 5 pernas
    assert st._fit_legs_to_risk(sig, 1000.0, 4.0) == 5
    # 10 € a 1% = 0.1 € → nem 1 perna cabe
    assert st._fit_legs_to_risk(sig, 10.0, 1.0) == 0


def test_fit_legs_to_risk_respects_max_legs():
    sig = _gold_signal(5, entry=4596.0, sl=4589.0)
    st = _state()
    assert st._fit_legs_to_risk(sig, 1000.0, 4.0, max_legs=3) == 3
    assert st._fit_legs_to_risk(sig, 1000.0, 1.5, max_legs=3) == 2


def test_fit_legs_to_risk_needs_no_fit_when_properly_sized():
    """Com saldo grande o dimensionamento é normal: o fit devolve o nº de
    pernas real do sinal (o gate nem sequer é accionado nesse caso)."""
    sig = _gold_signal(5, entry=4596.0, sl=4589.0)
    st = _state()
    assert st._fit_legs_to_risk(sig, 100000.0, 1.5) == 5


def test_signal_dict_exposes_pending_gate_fields():
    sig = _signal()
    sig.pending_kind = "min_lot_risk"
    sig.pending_meta = {"risk_gate": {"risk_amount": 15.0, "max_loss_est": 35.0}}
    d = sig.to_dict()
    assert d["pending_kind"] == "min_lot_risk"
    assert d["pending_meta"]["risk_gate"]["max_loss_est"] == 35.0

