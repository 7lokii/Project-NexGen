from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    group_protect_sl,
    hybrid_protect_eligible,
    monotonic_sl,
    sl_exit_reason,
    time_stop_due,
    time_stop_due_iso,
    protect_trigger_hit,
    protect_trigger_level,
)


def test_group_protect_sl_preserves_existing_buffer_formula():
    assert group_protect_sl(
        direction="buy", entry=4400.0, stop_loss=4392.0, tp1=4402.0,
    ) == 4400.8
    assert group_protect_sl(
        direction="sell", entry=4400.0, stop_loss=4408.0, tp1=4398.0,
    ) == 4399.2


def test_buy_group_sl_cannot_lower_mechanical_sl():
    result = decide_protect(
        direction="buy",
        auto_sl=4404.0,
        group_sl=4401.0,
        entry=4400.0,
        tp1=4402.0,
        price_at_event=4403.0,
        leg1_open=True,
        auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:05:00+00:00",
        pip_size=0.01,
    )
    assert result.applied_sl == 4404.0
    assert result.winner == "auto"
    assert result.leg1_action == "skipped"
    assert result.delta_pips == 300.0
    assert result.delta_seconds == 300.0


def test_sell_group_sl_cannot_raise_mechanical_sl():
    result = decide_protect(
        direction="sell",
        auto_sl=4396.0,
        group_sl=4399.0,
        entry=4400.0,
        tp1=4398.0,
        price_at_event=4397.0,
        leg1_open=True,
        auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:02:00+00:00",
        pip_size=0.01,
    )
    assert result.applied_sl == 4396.0
    assert result.winner == "auto"
    assert result.leg1_action == "skipped"
    assert result.delta_seconds == 120.0


def test_protect_can_close_leg1_only_after_tp1_is_known():
    result = decide_protect(
        direction="buy", auto_sl=4400.0, group_sl=4401.0,
        entry=4400.0, tp1=4402.0, price_at_event=4402.0,
        leg1_open=True, auto_protection_at=None,
        group_received_at="2026-08-19T10:00:00+00:00", pip_size=0.01,
    )
    assert result.applied_sl == 4401.0
    assert result.winner == "group"
    assert result.leg1_action == "closed"
    assert result.status == "auto_not_triggered"


def test_protect_does_not_close_already_closed_leg1():
    result = decide_protect(
        direction="buy", auto_sl=4402.0, group_sl=4401.0,
        entry=4400.0, tp1=4402.0, price_at_event=4401.0,
        leg1_open=False, auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:01:00+00:00", pip_size=0.01,
    )
    assert result.leg1_action == "already_closed"
    assert result.applied_sl == 4402.0


def test_protect_latency_storage_and_stats(tmp_path):
    from app.db import DB

    db = DB(str(tmp_path / "latency.db"))
    try:
        row_id = db.insert_protect_latency({
            "engine": "paper", "strategy_id": "auto_escada_3_hibrida",
            "signal_id": "s1", "trade_id": "pt-1", "message_id": "m1",
            "symbol": "XAUUSD", "direction": "buy",
            "auto_protection_at": "2026-08-19T10:00:00+00:00",
            "group_received_at": "2026-08-19T10:00:08+00:00",
            "auto_sl": 4404.0, "group_sl": 4405.0, "applied_sl": 4405.0,
            "winner": "group", "delta_pips": 100.0,
            "delta_seconds": 8.0, "leg1_action": "already_closed",
            "status": "ok", "created_at": "2026-08-19T10:00:08+00:00",
        })
        assert row_id > 0
        rows = db.protect_latency_rows(strategy_id="auto_escada_3_hibrida")
        assert rows[0]["engine"] == "paper"
        stats = db.protect_latency_stats(strategy_id="auto_escada_3_hibrida")
        assert stats == {
            "count": 1, "auto_wins": 0, "group_wins": 1, "ties": 0,
            "auto_not_triggered": 0, "mean_seconds": 8.0,
            "median_seconds": 8.0, "p90_seconds": 8.0,
            "max_seconds": 8.0, "mean_pips": 100.0, "p90_pips": 100.0,
            "max_pips": 100.0,
        }
    finally:
        db.close()


def test_hybrid_protect_waits_for_mechanical_protection():
    """15/09: o protect do grupo é sempre elegível — é monotónico e só
    aperta. O gate causal antigo recusava-o sem proteção auto e a perna
    rodou com o SL completo (−13,93 €)."""
    assert hybrid_protect_eligible(None, None) is True
    assert hybrid_protect_eligible(4400.0, None) is True
    assert hybrid_protect_eligible(4400.0, "2026-08-19T10:00:00+00:00") is True


def test_auto_policy_is_monotonic_for_both_directions():
    buy = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=102.0, tp2=104.0,
        trail_factor=0.5, stair_levels=3, stair_from=1,
    )
    sell = AutoProtectionPolicy(
        direction="sell", entry=100.0, tp1=98.0, tp2=96.0,
        trail_factor=0.5, stair_levels=3, stair_from=1,
    )
    assert buy.initial_floor() == 102.0
    assert sell.initial_floor() == 98.0
    assert buy.stair_target(102.0, 104.0, 3) == 104.0
    assert buy.stair_target(104.0, 102.0, 3) is None
    assert sell.stair_target(98.0, 96.0, 3) == 96.0
    assert sell.stair_target(96.0, 98.0, 3) is None
    assert monotonic_sl("buy", 100.0, 100.0) is None
    assert monotonic_sl("sell", 100.0, 100.0) is None


def test_auto_policy_trailing_uses_previous_extreme_and_atr_floor():
    policy = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=102.0, tp2=104.0,
        trail_factor=0.5,
    )
    # fixed distance = 2.0; ATR floor widens it to 3.0
    assert policy.trail_distance(atr=6.0, atr_k=0.5) == 3.0
    # previous peak 110, current price is irrelevant to the target itself;
    # the adapter supplies only the already observed extreme.
    assert policy.trailing_target(102.0, 110.0, distance=3.0) == 107.0
    assert policy.trailing_target(108.0, 110.0, distance=3.0) is None


def test_common_boundaries_for_confirmation_stop_and_reason():
    assert confirmation_legs(2, [1, 2, 3, 4]) == (3, 4)
    assert confirmation_legs(4, [1, 2, 3]) == ()
    assert time_stop_due(100.0, 160.0, 1) is True
    assert time_stop_due(100.0, 159.999, 1) is False
    assert sl_exit_reason("buy", 100.0, 100.0) == "be"
    assert sl_exit_reason("buy", 101.0, 100.0) == "trail"
    assert sl_exit_reason("sell", 99.0, 100.0) == "trail"


def test_auto_protect_trigger_is_shared_for_buy_sell_and_rejects_invalid_levels():
    assert protect_trigger_level(
        direction="buy", entry=100.0, tp1=104.0, fraction=0.75,
    ) == 103.0
    assert protect_trigger_hit(
        direction="buy", price=103.0, entry=100.0, tp1=104.0,
        fraction=0.75,
    ) is True
    assert protect_trigger_level(
        direction="sell", entry=100.0, tp1=96.0, fraction=0.75,
    ) == 97.0
    assert protect_trigger_hit(
        direction="sell", price=97.0, entry=100.0, tp1=96.0,
        fraction=0.75,
    ) is True
    assert protect_trigger_level(
        direction="buy", entry=100.0, tp1=99.0, fraction=0.75,
    ) is None
    assert protect_trigger_level(
        direction="buy", entry=100.0, tp1=104.0, fraction=0.0,
    ) is None


def test_time_stop_adapters_ignore_invalid_durations_and_mixed_timestamp_styles():
    assert time_stop_due(100.0, 160.0, "not-a-number") is False
    assert time_stop_due_iso(
        "2026-08-19T10:00:00",
        "2026-08-19T10:01:00+00:00",
        1,
    ) is True
    assert time_stop_due_iso(
        "2026-08-19T10:00:00+00:00",
        "2026-08-19T10:01:00",
        "invalid",
    ) is False


def test_auto_protect_reducer_is_idempotent_and_monotonic():
    from app.execution.protection import reduce_auto_protect

    decision = reduce_auto_protect(
        direction="buy", price=103.0, entry=100.0, stop_loss=96.0,
        tp1=104.0, leg1_open=True, remaining_sls={2: 100.0, 3: 102.0},
        trigger_fraction=0.75, buffer_fraction=0.10,
    )
    assert decision.triggered is True
    assert decision.trigger_level == 103.0
    assert decision.group_sl == 100.4
    assert decision.move_sls == ((2, 100.4),)

    repeated = reduce_auto_protect(
        direction="buy", price=103.0, entry=100.0, stop_loss=96.0,
        tp1=104.0, leg1_open=False, remaining_sls={2: 100.4, 3: 102.0},
        trigger_fraction=0.75, buffer_fraction=0.10,
        already_triggered=True,
    )
    assert repeated.triggered is False
    assert repeated.move_sls == ()


def test_initial_floor_be_buffer_pips():
    """BE+ (tp2_protect): com `be_buffer_pips` o piso pós-TP1 fica em
    entrada ± buffer (cobre o spread), nunca acima/abaixo do TP1."""
    buy = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=102.0, tp2=104.0,
        be_buffer_pips=40.0, pip_size=0.01,
    )
    assert buy.initial_floor() == 100.4  # 40 pips × 0.01
    sell = AutoProtectionPolicy(
        direction="sell", entry=100.0, tp1=98.0, tp2=96.0,
        be_buffer_pips=40.0, pip_size=0.01,
    )
    assert sell.initial_floor() == 99.6  # espelho da venda
    # sem buffer = BE clássico (comportamento anterior preservado)
    plain = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=102.0, tp2=104.0,
    )
    assert plain.initial_floor() == 100.0


def test_initial_floor_fill_anchor_worst_of():
    """Âncora por perna: o chão usa o PIORE custo entre a entrada do sinal e
    o fill da perna — num buy, fill ACIMA da entrada (adversário, pagou mais)
    sobe a âncora para o fill (BE tem de ser realmente sem perda — incidente
    t-07e968709234); fill abaixo da entrada (melhor) mantém a entrada."""
    pol = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=102.0, tp2=104.0,
        be_buffer_pips=40.0, pip_size=0.01,
    )
    # fill adversário (acima da entrada num buy): âncora sobe para o fill
    assert pol.initial_floor(anchor=100.8) == 101.2  # 100.8 + 40 pips
    # fill melhor que a entrada (comprou mais barato): âncora fica na entrada
    assert pol.initial_floor(anchor=99.5) == 100.4   # 100.0 + 40 pips
    # espelho de venda: fill abaixo da entrada (adversário) é o pior
    sell = AutoProtectionPolicy(
        direction="sell", entry=100.0, tp1=98.0, tp2=96.0,
        be_buffer_pips=40.0, pip_size=0.01,
    )
    assert sell.initial_floor(anchor=99.2) == 98.8   # 99.2 - 40 pips
    assert sell.initial_floor(anchor=100.5) == 99.6  # entrada continua a ser o pior
    # anchor inválido/None = comportamento anterior (entrada do sinal)
    assert pol.initial_floor(anchor=None) == 100.4
    assert pol.initial_floor(anchor=0) == 100.4


def test_initial_floor_be_buffer_never_crosses_tp1():
    """Buffer maior que a distância entrada→TP1 fica limitado (com 1 pip
    de folga) — o SL protegido nunca cruza o TP1."""
    tight = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=100.3, tp2=101.0,
        be_buffer_pips=400.0, pip_size=0.01,
    )
    # room = |100.3-100.0| - 0.01 = 0.29 → floor = 100.0 + 0.29 < TP1
    import pytest as _pytest
    assert tight.initial_floor() == _pytest.approx(100.29)
    # distância mínima (TP1 a 1 pip): sem espaço, cai no BE clássico
    minimal = AutoProtectionPolicy(
        direction="buy", entry=100.0, tp1=100.01, tp2=100.5,
        be_buffer_pips=40.0, pip_size=0.01,
    )
    assert minimal.initial_floor() == 100.0
