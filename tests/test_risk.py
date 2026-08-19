from app.risk.engine import RiskEngine, compute_lots
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


def test_check_allows_clean_signal():
    engine = RiskEngine()
    cfg = {"kill_switch": False, "auto_kill": False, "max_positions": 3,
           "daily_loss_limit_pct": 3.0}
    assert engine.check(make_signal(), cfg, [], 0.0, 10_000).allowed
