from app.risk.engine import (
    RiskEngine,
    compute_lots,
    defensive_mode_active,
    effective_strategy_risk_pct,
)
from app.models import Signal


def make_signal(**over):
    base = dict(
        id="s1", source="test", raw_text="", symbol="EURUSD", direction="buy",
        entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890,
        take_profit_2=1.0920, status="pending", received_at="",
    )
    base.update(over)
    return Signal(**base)


def test_compute_lots_math():
    # equity 10_000, risk 1% -> $100. SL 30 pips away. Pip value $10/lot.
    # lots = 100 / (30 * 10) = 0.3333 -> rounds to 0.33.
    lots = compute_lots(10_000, 1.0, 1.0850, 1.0820)
    assert abs(lots - 0.33) < 1e-9


def test_compute_lots_tighter_sl_bigger_lots():
    tight = compute_lots(10_000, 1.0, 1.0850, 1.0830)   # 20 pips
    wide = compute_lots(10_000, 1.0, 1.0850, 1.0800)    # 50 pips
    assert tight > wide


def test_compute_lots_never_below_minimum():
    lots = compute_lots(10_000, 0.1, 1.0850, 1.0800)
    assert lots >= 0.01


def test_check_kill_switch_blocks():
    engine = RiskEngine()
    cfg = {"kill_switch": True, "auto_kill": False, "max_positions": 3,
           "daily_loss_limit_pct": 3.0}
    decision = engine.check(make_signal(), cfg, [], 0.0, 10_000)
    assert not decision.allowed
    assert any("kill switch" in r for r in decision.reasons)


def test_check_max_positions_blocks():
    engine = RiskEngine()
    cfg = {"kill_switch": False, "auto_kill": False, "max_positions": 2,
           "daily_loss_limit_pct": 3.0}
    decision = engine.check(make_signal(), cfg, ["t1", "t2"], 0.0, 10_000)
    assert not decision.allowed
    assert any("posições" in r for r in decision.reasons)


def test_daily_loss_zero_means_off():
    """O slider permite 0 = sem limite diário; o risk engine tem de tratar
    0 como off (e não como 'bloqueia assim que o P&L realizado ≤ 0')."""
    engine = RiskEngine()
    cfg = {"kill_switch": False, "auto_kill": False, "max_positions": 3,
           "daily_loss_limit_pct": 0}
    assert not engine.daily_loss_exceeded(10_000, 0.0, cfg)
    assert not engine.daily_loss_exceeded(10_000, -500.0, cfg)
    assert engine.check(make_signal(), cfg, [], -500.0, 10_000).allowed


def test_check_daily_loss_blocks_and_allows():
    engine = RiskEngine()
    cfg = {"kill_switch": False, "auto_kill": False, "max_positions": 3,
           "daily_loss_limit_pct": 3.0}
    # -350 realized on 10k account = -3.5% > 3% limit -> blocked
    decision = engine.check(make_signal(), cfg, [], -350.0, 10_000)
    assert not decision.allowed
    assert engine.daily_loss_exceeded(10_000, -350.0, cfg)
    # -200 is within the 3% limit -> allowed
    assert not engine.daily_loss_exceeded(10_000, -200.0, cfg)
    assert engine.check(make_signal(), cfg, [], -200.0, 10_000).allowed


def test_defensive_risk_cap_requires_full_losing_window_and_never_increases():
    """The defensive cap is deterministic and only uses prior closed trades."""
    losses = [-10.0, -20.0, -5.0]
    assert defensive_mode_active(losses, 3)
    assert effective_strategy_risk_pct(
        2.5, def_window=3, def_risk_pct=1.25, prior_pnls=losses,
    ) == 1.25
    assert effective_strategy_risk_pct(
        1.0, def_window=3, def_risk_pct=1.25, prior_pnls=losses,
    ) == 1.0
    assert effective_strategy_risk_pct(
        2.5, def_window=3, def_risk_pct=1.25, prior_pnls=[-10.0, -20.0],
    ) == 2.5
    assert effective_strategy_risk_pct(
        2.5, def_window=3, def_risk_pct=1.25,
        prior_pnls=[10.0, 20.0, 5.0],
    ) == 2.5


def test_check_allows_clean_signal():
    engine = RiskEngine()
    cfg = {"kill_switch": False, "auto_kill": False, "max_positions": 3,
           "daily_loss_limit_pct": 3.0}
    assert engine.check(make_signal(), cfg, [], 0.0, 10_000).allowed
