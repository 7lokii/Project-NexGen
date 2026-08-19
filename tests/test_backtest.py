from datetime import datetime

from app.backtest import (aggregate, apply_daily_stop, attribute_protect_events,
                          drawdown_from_results, _roll_pf, simulate_signal,
                          simulate_signal_managed)
from app.models import Signal


def make_signal(entry=1.0850, sl=1.0820, tp1=1.0890, tp2=1.0920,
                direction="buy", received_at="2026-08-17T10:00:00"):
    return Signal(id="s1", source="tg:Test", raw_text="", symbol="EURUSD",
                  direction=direction, entry=entry, stop_loss=sl,
                  take_profit_1=tp1, take_profit_2=tp2,
                  status="historical", received_at=received_at)


BASE = int(datetime.fromisoformat("2026-08-17T10:00:00").timestamp())


def candles(highs, lows, start_ts=BASE + 60, step=900):
    out = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        out.append({"ts": start_ts + i * step, "open": (h + l) / 2,
                    "high": h, "low": l, "close": (h + l) / 2})
    return out


def test_full_win_tp1_then_tp2():
    sig = make_signal()
    # candle 1 reaches TP1 (low stays above entry so leg 2 is not stopped),
    # candle 2 reaches TP2
    cs = candles(highs=[1.0890, 1.0920], lows=[1.0860, 1.0870])
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "win"
    assert res["pnl"] > 0


def test_stop_loss_loss():
    sig = make_signal()
    cs = candles(highs=[1.0840, 1.0880], lows=[1.0820, 1.0840])
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "loss"
    assert res["pnl"] < 0


def test_tp1_then_breakeven():
    sig = make_signal()
    # TP1 then price returns to entry -> leg 2 closes at breakeven
    cs = candles(highs=[1.0890, 1.0850], lows=[1.0830, 1.0840])
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "be"


def test_delay_ignores_first_minute():
    sig = make_signal()
    # candle 1 sits at BASE (inside the first 60s) and would hit the SL, but
    # with a 60s delay it must be ignored; candle 2 (after the delay) hits
    # nothing -> trade stays open
    cs = candles(highs=[1.0840, 1.0880], lows=[1.0810, 1.0840], start_ts=BASE, step=900)
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "open"


def test_no_data_when_candles_end_before_delay():
    sig = make_signal()
    cs = candles(highs=[1.0880], lows=[1.0840], start_ts=BASE - 3600, step=900)
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "no_data"


def test_costs_reduce_pnl():
    sig = make_signal()
    cs = candles(highs=[1.0890, 1.0920], lows=[1.0860, 1.0870])
    no_cost = simulate_signal(sig, cs, 1.0, 10_000, 60,
                              spread_pips=0, slippage_pips=0, commission_per_lot=0)
    costly = simulate_signal(sig, cs, 1.0, 10_000, 60,
                             spread_pips=1.0, slippage_pips=0.5, commission_per_lot=3.5)
    assert costly["pnl"] < no_cost["pnl"]
    assert costly["commission"] > 0
    assert no_cost["commission"] == 0


def test_three_legs_tp1_then_tp2_then_tp3_win():
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1 reaches TP1 (low stays above entry), candle 2 reaches TP2,
    # candle 3 reaches TP3
    cs = candles(highs=[1.0890, 1.0920, 1.0960], lows=[1.0860, 1.0870, 1.0880])
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "win"
    assert res["pnl"] > 0
    assert res["legs_pnl"][3] > 0


def test_three_legs_stop_loss_closes_all():
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    cs = candles(highs=[1.0840], lows=[1.0820])
    res = simulate_signal(sig, cs, risk_pct=1.0, balance=10_000, delay_seconds=60)
    assert res["outcome"] == "loss"
    assert res["pnl"] < 0


def test_max_legs_limits_ladder():
    """max_legs=2 replays the old TP1/TP2-only strategy on the same signal."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    sig.take_profit_4 = 1.1000
    # price runs to TP3 but never TP4
    cs = candles(highs=[1.0890, 1.0920, 1.0960], lows=[1.0860, 1.0870, 1.0880])
    full = simulate_signal(sig, cs, 1.0, 10_000, 60)
    capped = simulate_signal(sig, cs, 1.0, 10_000, 60, max_legs=2)
    assert 3 in full["legs_pnl"]
    assert 3 not in capped["legs_pnl"]
    # the ladder pays commission on more legs, so the net P&L differs
    assert full["pnl"] != capped["pnl"]


def test_exit_at_tp2_closes_all_legs():
    """exit_at_tp=2 ("fechar tudo no TP2"): quando o TP2 bate, TODAS as
    pernas ainda abertas fecham nesse preço — a perna 3 não corre para o
    TP3 (fecha no TP2) e o outcome é win."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1 atinge TP1, candle 2 atinge TP2 (TP3 ficaria para candle 3)
    cs = candles(highs=[1.0890, 1.0920], lows=[1.0860, 1.0870])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, exit_at_tp=2)
    assert res["outcome"] == "win"
    assert res["pnl"] > 0
    # perna 3 fechou no preço do TP2, não no TP3
    assert 3 in res["legs_pnl"]
    assert res["legs_pnl"][2] == res["legs_pnl"][3], \
        "leg 3 fecha no TP2 quando exit_at_tp=2"


def test_exit_at_tp_only_triggers_at_that_tp():
    """Sem tocar no TP2, exit_at_tp=2 não muda nada (pernas correm como
    antes — ex.: TP3 sem passar por TP2? não: o TP2 está na ladder, mas
    se o SL bater primeiro, o resultado é loss igual)."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    cs = candles(highs=[1.0840], lows=[1.0820])  # SL primeiro
    base = simulate_signal(sig, cs, 1.0, 10_000, 60)
    with_exit = simulate_signal(sig, cs, 1.0, 10_000, 60, exit_at_tp=2)
    assert base["outcome"] == with_exit["outcome"] == "loss"
    assert base["pnl"] == with_exit["pnl"]


def test_drawdown_from_results():
    results = [
        {"outcome": "win", "pnl": 100.0, "received_at": "2026-08-01T10:00:00"},
        {"outcome": "loss", "pnl": -150.0, "received_at": "2026-08-02T10:00:00"},
        {"outcome": "win", "pnl": 200.0, "received_at": "2026-08-03T10:00:00"},
    ]
    dd = drawdown_from_results(results, 10_000)
    # curve: 100 -> -50 -> 150; peak-to-valley = 100 - (-50) = 150
    assert dd["amount"] == 150.0
    assert dd["pct"] == 1.5


def test_aggregate():
    results = [
        {"outcome": "win", "pnl": 100.0},
        {"outcome": "loss", "pnl": -50.0},
        {"outcome": "be", "pnl": 0.0},
        {"outcome": "no_data", "pnl": 0.0},
    ]
    a = aggregate(results)
    assert a["total"] == 4
    assert a["resolved"] == 3
    assert a["wins"] == 1
    assert a["losses"] == 1
    assert a["breakevens"] == 1
    assert a["no_data"] == 1
    assert a["win_rate"] == 33.3
    assert a["pnl"] == 50.0
    assert a["profit_factor"] == 2.0


def test_managed_exit_at_tp2_closes_all_legs():
    """'Sair no TP2 + gestão': o sim gerido respeita exit_at_tp — quando o
    TP2 bate, todas as pernas abertas fecham no TP2 (a perna 3 não corre
    para o TP3) e o outcome é win."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    cs = candles(highs=[1.0890, 1.0920], lows=[1.0860, 1.0870])
    res = simulate_signal_managed(sig, cs, [], 1.0, 10_000, 60, exit_at_tp=2)
    assert res["outcome"] == "win"
    assert res["pnl"] > 0
    assert 3 in res["legs_pnl"]
    assert res["legs_pnl"][2] == res["legs_pnl"][3], "perna 3 fecha no TP2"


def test_managed_hybrid_group_protect_cannot_worsen_mechanical_sl():
    sig = make_signal(received_at="2026-08-17T10:00:00")
    sig.take_profit_3 = 1.0950
    cs = candles(
        highs=[1.0900, 1.0910, 1.0900],
        lows=[1.0892, 1.0895, 1.0895],
    )
    result = simulate_signal_managed(
        sig, cs, [(cs[2]["ts"], "protect", False, None)],
        1.0, 10_000, 60,
        manage_kinds={"protect"}, trail_factor=2.0,
        stair_levels=3, stair_from=1,
        strategy_id="auto_escada_3_hibrida",
    )
    assert result["protect_latency"]
    row = result["protect_latency"][0]
    assert row["winner"] == "auto"
    assert row["applied_sl"] >= row["auto_sl"]
    assert row["leg1_action"] == "already_closed"


def test_managed_hybrid_protect_does_not_use_current_candle_close():
    sig = make_signal(received_at="2026-08-17T10:00:00")
    cs = [
        {"ts": BASE + 60, "open": 1.0870, "high": 1.0880,
         "low": 1.0860, "close": 1.0870},
        {"ts": BASE + 960, "open": 1.0870, "high": 1.0925,
         "low": 1.0860, "close": 1.0920},
    ]
    result = simulate_signal_managed(
        sig, cs, [(cs[1]["ts"], "protect", False, None)],
        1.0, 10_000, 60,
        manage_kinds={"protect"}, trail_factor=2.0,
        stair_levels=3, stair_from=1,
        strategy_id="auto_escada_3_hibrida",
    )
    assert result["protect_latency"]
    assert result["protect_latency"][0]["leg1_action"] == "skipped"


def test_causal_protect_attribution_identifies_harmful_intervention():
    """A protect that tightens BE before a later winner is attributed to the
    event, not hidden inside the final hybrid P&L."""
    sig = make_signal(received_at="2026-08-17T10:00:00")
    sig.take_profit_3 = 1.0960
    cs = candles(
        highs=[1.0890, 1.0885, 1.0960],
        lows=[1.0870, 1.0870, 1.0853],
    )
    report = attribute_protect_events(
        sig, cs, [(cs[1]["ts"], "protect", False, None)],
        1.0, 10_000, delay_seconds=60, spread_pips=0,
        slippage_pips=0, commission_per_lot=0,
        max_legs=3, stair_levels=3, stair_from=2,
    )
    assert report["event_count"] == 1
    assert report["harmful_event_count"] == 1
    assert report["protect_delta_pnl"] < 0
    assert report["harmful_delta_pnl"] == report["protect_delta_pnl"]
    assert report["events"][0]["incremental_pnl"] == report["protect_delta_pnl"]


def test_trace_records_events_and_path():
    """trace=True devolve os eventos cronológicos (SL/TP1/BE/fecho) e o
    caminho de preços para a página de teste mostrar o exemplo real."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    cs = candles(highs=[1.0890, 1.0920, 1.0960], lows=[1.0860, 1.0870, 1.0880])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, exit_at_tp=2, trace=True)
    kinds = [e["kind"] for e in res["events"]]
    assert "tp1" in kinds and "be" in kinds and "tp_all" in kinds
    assert res["path"] and res["path"][0]["ts"] == cs[0]["ts"]
    assert "events" not in simulate_signal(sig, cs, 1.0, 10_000, 60)


def test_apply_daily_stop_skips_rest_of_day():

    """Kill diário: depois de o dia ultrapassar o limite, as trades seguintes
    desse dia deixam de acontecer (outcome=skip, pnl=0); o dia seguinte
    recomeça do zero."""
    results = [
        {"received_at": "2026-08-17T09:00:00", "outcome": "loss", "pnl": -60.0},  # dia -60 <= -50 (limite 5% de 1000)
        {"received_at": "2026-08-17T10:00:00", "outcome": "loss", "pnl": -30.0},  # saltada (dia <= -50)
        {"received_at": "2026-08-17T11:00:00", "outcome": "win", "pnl": 40.0},    # saltada também (mesmo dia)
        {"received_at": "2026-08-18T09:00:00", "outcome": "loss", "pnl": -30.0},  # dia novo — acontece
    ]
    info = apply_daily_stop(results, balance=1000.0, stop_pct=5.0)
    assert info["skipped"] == 2
    assert results[1]["outcome"] == "skip" and results[1]["pnl"] == 0.0
    assert results[2]["outcome"] == "skip" and results[2]["pnl"] == 0.0
    assert results[0]["outcome"] == "loss"
    assert results[3]["outcome"] == "loss"


def test_apply_daily_stop_off_and_zero_limit():
    results = [{"received_at": "2026-08-17T09:00:00", "outcome": "loss", "pnl": -99.0}]
    assert apply_daily_stop(results, 1000.0, 0) == {"skipped": 0}
    assert apply_daily_stop(results, 1000.0, -1) == {"skipped": 0}
    assert results[0]["outcome"] == "loss"


def test_managed_honors_manage_kinds():
    """manage_kinds={'protect'} filtra os eventos de gestão: um close_all do
    grupo é IGNORADO na simulação (a estratégia continua a correr para os
    TPs) enquanto a variante completa o aplica."""
    sig = make_signal()
    # candle 1 não toca nada; candle 2 atinge TP1 e TP2 (win sem gestão)
    cs = candles(highs=[1.0865, 1.0925], lows=[1.0845, 1.0880])
    ev_close_all = (BASE + 60 + 900, "close_all", False)  # no instante do candle 2

    full = simulate_signal_managed(sig, cs, [ev_close_all], risk_pct=1.0,
                                   balance=10_000, delay_seconds=60)
    assert "close_all" in full["mgmt_applied"]

    only_protect = simulate_signal_managed(sig, cs, [ev_close_all], risk_pct=1.0,
                                           balance=10_000, delay_seconds=60,
                                           manage_kinds={"protect"})
    assert "close_all" not in only_protect["mgmt_applied"]
    assert only_protect["pnl"] > full["pnl"]  # correu para os TPs em vez de sair

    # manage_kinds vazio = sem gestão nenhuma (mesmo com eventos presentes)
    none = simulate_signal_managed(sig, cs, [ev_close_all], risk_pct=1.0,
                                   balance=10_000, delay_seconds=60,
                                   manage_kinds=set())
    assert none["mgmt_applied"] == []


# ---------------------------------------------------------------------------
# Auto-gestão (spec docs/SPEC-auto-gestao.md): escada de proteção, filtro de
# confirmação, trailing e time stop — buy e sell.
# ---------------------------------------------------------------------------


def test_stair_locks_tp1_profit_buy():
    """stair_from=1 (escada desde o TP1): quando o TP1 bate, o SL das pernas
    seguintes sobe para o NÍVEL do TP1 — se o preço recuar, fecham com lucro
    em vez de devolver ao BE."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1 atinge TP1 (low acima do degrau -> pernas ficam abertas);
    # candle 2 recua para baixo do TP1 mas acima da entrada -> fecham no TP1
    cs = candles(highs=[1.0900, 1.0895], lows=[1.0892, 1.0885])
    stair = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_levels=3, stair_from=1)
    assert stair["outcome"] == "win"
    assert stair["legs_pnl"][2] > 0, "perna 2 fechou com lucro no nível do TP1"
    assert stair["legs_pnl"][3] > 0, "perna 3 fechou com lucro no nível do TP1"

    # sem escada, o SL das pernas 2-3 fica na entrada -> o recuo não as fecha
    plain = simulate_signal(sig, cs, 1.0, 10_000, 60)
    assert plain["outcome"] == "open", "sem escada as pernas continuam abertas no BE"


def test_stair_moves_sl_to_tp2_buy():
    """Quando o TP2 bate, o SL das pernas seguintes sobe para o nível do TP2
    — a perna 3 fecha no TP2 com lucro se o preço recuar, não devolve nada."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1: TP1; candle 2: TP2 (low acima do novo degrau -> perna 3 fica);
    # candle 3: recuo para baixo do TP2 -> perna 3 fecha no nível do TP2
    cs = candles(highs=[1.0900, 1.0925, 1.0920], lows=[1.0892, 1.0921, 1.0915])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_levels=3, stair_from=1)
    assert res["outcome"] == "win"
    assert res["legs_pnl"][3] > 0, "perna 3 fechou no nível do TP2 com lucro"


def test_confirm_filter_closes_far_legs_buy():
    """confirm_filter: quando uma perna leva SL (aqui o BE), as pernas
    seguintes fecham ao mercado na mesma vela — a perna 3 fecha com lucro
    (market close acima da entrada) em vez de bater no BE com prejuízo."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1: TP1 (pernas 2-3 para BE); candle 2: recuo abaixo do BE
    cs = candles(highs=[1.0900, 1.0880], lows=[1.0892, 1.0845])
    with_conf = simulate_signal(sig, cs, 1.0, 10_000, 60,
                                stair_from=2, confirm_filter=True)
    assert with_conf["outcome"] == "win"
    assert with_conf["legs_pnl"][3] > 0, "perna 3 fechou ao mercado (acima da entrada)"

    without = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_from=2)
    assert without["legs_pnl"][3] < 0, "sem filtro a perna 3 fecha no BE (fill pior que a entrada)"


def test_stair_locks_tp1_profit_sell():
    """Espelho sell da escada: TP1 de venda bate e o SL das pernas seguintes
    sobe para o nível do TP1 (abaixo da entrada) — o recuo para cima fecha
    com lucro."""
    sig = make_signal(entry=1.0850, sl=1.0880, tp1=1.0810, tp2=1.0780,
                      direction="sell")
    sig.take_profit_3 = 1.0750
    # candle 1: TP1 (high abaixo do degrau -> pernas ficam); candle 2: recuo
    # para cima do nível do TP1 -> fecham com lucro
    cs = candles(highs=[1.0808, 1.0815], lows=[1.0805, 1.0800])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_levels=3, stair_from=1)
    assert res["outcome"] == "win"
    assert res["legs_pnl"][2] > 0
    assert res["legs_pnl"][3] > 0


def test_trail_follows_price_buy():
    """trail_factor: depois do TP1, o SL segue o pico — se o preço inverter
    trail_dist do pico, as pernas fecham com lucro acima do BE."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1: TP1 (pico 1.0900); candle 2: pico novo 1.0918 (sem TP2);
    # candle 3: recuo de trail_dist -> fecham no SL traiing (lucro)
    cs = candles(highs=[1.0900, 1.0918, 1.0900], lows=[1.0892, 1.0910, 1.0875])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=0.5)
    assert res["outcome"] == "win"
    assert res["legs_pnl"][2] > 0
    assert res["legs_pnl"][3] > 0
    # sem trailing, o recuo 1.0875 está acima do BE (1.0850) -> nada fecha
    plain = simulate_signal(sig, cs, 1.0, 10_000, 60)
    assert plain["outcome"] == "open"


def test_time_stop_closes_after_n_minutes_buy():
    """time_stop_minutes: se as pernas continuarem abertas N minutos após o
    TP1 (mercado estagnado), fecham ao mercado com o lucro que houver."""
    sig = make_signal()
    sig.take_profit_3 = 1.0960
    # candle 1: TP1; candle 2 (T+15m): nada; candle 3 (T+30m): time stop
    cs = candles(highs=[1.0900, 1.0880, 1.0880], lows=[1.0892, 1.0860, 1.0865])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, time_stop_minutes=30)
    assert res["outcome"] == "win"
    assert res["legs_pnl"][2] > 0, "perna 2 fechou ao mercado com lucro"
    assert res["legs_pnl"][3] > 0, "perna 3 fechou ao mercado com lucro"

    # sem time stop, as pernas continuam abertas
    plain = simulate_signal(sig, cs, 1.0, 10_000, 60)
    assert plain["outcome"] == "open"


# ---------------------------------------------------------------------------
# SPEC v2 §4 — testes de regressão da auto-gestão (look-ahead, conflitos,
# escada vs trailing, risco por sinal). Falham explicitamente se o look-ahead
# voltar ou se o trailing desfizer a escada.
# ---------------------------------------------------------------------------


def test_tp1_candle_does_not_seed_trailing_from_unseen_extreme():
    """The candle that first reaches TP1 may overshoot and retrace, but its
    unseen high/low cannot create a trailing SL and fill it in the same candle.
    The buy and sell paths must both wait for the next candle."""
    buy = make_signal(tp2=1.0950)
    buy_candles = candles(highs=[1.0945], lows=[1.0860])
    buy_result = simulate_signal(
        buy, buy_candles, 1.0, 10_000, 60, trail_factor=0.4,
    )
    assert buy_result["outcome"] == "open"

    sell = make_signal(
        entry=1.0850, sl=1.0880, tp1=1.0810, tp2=1.0780,
        direction="sell",
    )
    sell_candles = candles(highs=[1.0815], lows=[1.0785])
    sell_result = simulate_signal(
        sell, sell_candles, 1.0, 10_000, 60, trail_factor=0.4,
    )
    assert sell_result["outcome"] == "open"


def test_trailing_no_lookahead_buy():
    """O trailing usa o pico das velas ANTERIORES: uma vela cujo high cria um
    pico novo NÃO pode levantar o SL que o próprio low depois atravessa."""
    sig = make_signal(tp2=1.0950)  # entrada→TP2 = 0.0100 → trail_dist 0.0050
    # candle 1: TP1 (pico 1.0890); candle 2: high 1.0915 (pico novo, sem TP2)
    # com low 1.0865 — se o SL usasse o high da própria vela (1.0865 = 1.0915−
    # 0.0050) fechava com lucro; sem look-ahead (pico 1.0890 → SL 1.0840 < BE)
    # não fecha nada
    cs = candles(highs=[1.0890, 1.0915], lows=[1.0860, 1.0865])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=0.5)
    assert res["outcome"] == "open", "look-ahead: a vela beneficiou do próprio high"

    # vela 3: recuo real abaixo do trail legítimo (pico da vela 2 = 1.0915 →
    # SL 1.0865) — fecha no trail com lucro, já com informação passada
    cs2 = candles(highs=[1.0890, 1.0915, 1.0880], lows=[1.0860, 1.0865, 1.0840])
    res2 = simulate_signal(sig, cs2, 1.0, 10_000, 60, trail_factor=0.5)
    assert res2["outcome"] == "win" and res2["legs_pnl"][2] > 0


def test_trailing_atr_floor_widens_trail():
    """`atr_k` no backtest (espelho do MT5, §B.5.3): trail_dist =
    max(trail fixo, atr_k × ATR) — o ATR das velas ANTERIORES à atual (sem
    look-ahead). Com ATR alto, o SL fica mais LARGO (no BE) e a trade NÃO
    fecha no recuo que o trail fixo fecharia."""
    from app.backtest import _candle_atr
    sig = make_signal(tp2=1.0950)  # entrada→TP2 = 0.0100 → trail fixo 0.0050
    # 4 velas pré-sinal (TR 0.02) + 3 velas da trade: start cai no índice 4
    # (ts = BASE+60 com delay 60) — as 4 primeiras são o histórico para o ATR
    pre = [(1.0700, 1.0500), (1.0800, 1.0600), (1.0800, 1.0600), (1.0800, 1.0600)]
    # o low da vela do TP1 (1.0860) fica ACIMA do BE (1.0850) — senão a perna 2
    # fechava a BE na própria vela do TP1 e o teste não exercita o trailing
    trade_c = [(1.0890, 1.0860), (1.0940, 1.0900), (1.0920, 1.0870)]
    cs = candles([h for h, _ in pre + trade_c], [l for _, l in pre + trade_c],
                 start_ts=BASE - 3540)  # índice 4 = BASE+60 (start)
    # ATR das 2 velas antes do índice 6: TR(c0)=max(0.003,0.019,0.016)=0.019,
    # TR(c1)=max(0.004,0.0065,0.0025)=0.0065 → ATR=(0.019+0.0065)/2=0.01275
    assert abs(_candle_atr(cs, 6, 2) - 0.01275) < 1e-9
    # sem atr_k: c2 recua abaixo do trail fixo (SL 1.0890, low 1.0870) → win
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=0.5)
    assert res["outcome"] == "win"
    # com atr_k=1.0: trail_dist ≥ atr_k×ATR (≥ 0.009) → o SL fica no BE
    # (1.0850) e o low 1.0870 não cruza → a trade continua aberta
    res2 = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=0.5,
                           atr_k=1.0, atr_period=2)
    assert res2["outcome"] == "open"
    assert res2["legs_pnl"][2] == 0.0


def test_trailing_no_lookahead_sell():
    """Espelho para venda: o vale da própria vela não levanta o SL que o
    próprio high atravessaria."""
    sig = make_signal(entry=1.0850, sl=1.0880, tp1=1.0810, tp2=1.0780,
                      direction="sell")
    # candle 1: TP1 (vale 1.0810); candle 2: low 1.0785 (vale novo, sem TP2),
    # high 1.0820 — sem look-ahead o SL fica 1.0845 (1.0810+0.0035) e o high
    # 1.0820 não cruza; com look-ahead (vale 1.0785 → SL 1.0820) fechava
    cs = candles(highs=[1.0808, 1.0820], lows=[1.0810, 1.0785])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=0.5)
    assert res["outcome"] == "open", "look-ahead: a vela beneficiou do próprio low"

    # vela 3: recuo real acima do trail legítimo (vale da vela 2 = 1.0785 →
    # SL 1.0820) — fecha no trail com lucro
    cs2 = candles(highs=[1.0808, 1.0820, 1.0855], lows=[1.0810, 1.0785, 1.0790])
    res2 = simulate_signal(sig, cs2, 1.0, 10_000, 60, trail_factor=0.5)
    assert res2["outcome"] == "win" and res2["legs_pnl"][2] > 0


def test_tp_and_sl_same_candle():
    """Vela que toca TP1 E SL: o SL tem prioridade (conservador) — fecha tudo
    no SL com perda, nunca assume o lucro do TP."""
    sig = make_signal()
    cs = candles(highs=[1.0895, 1.0880], lows=[1.0815, 1.0840])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60)
    assert res["outcome"] == "loss"
    assert res["pnl"] < 0


def test_multiple_tp_same_candle():
    """Vela que toca TP2 E TP3: o TP2 fecha primeiro e a escada sobe o SL da
    perna 3 para o nível do TP2 — a perna 3 fecha no TP3 (low acima do degrau)."""
    sig = make_signal(tp2=1.0925)
    sig.take_profit_3 = 1.0950
    cs = candles(highs=[1.0890, 1.0960], lows=[1.0860, 1.0927])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_levels=3, stair_from=1)
    assert res["outcome"] == "win"
    assert res["legs_pnl"][2] > 0 and res["legs_pnl"][3] > 0


def test_defensive_mode_tightens_trail_after_pf_drop():
    """Modo defensivo (estudo 19/08): com o PF das últimas 10 trades
    resolvidas < 1.0 (`def_state` partilhado, sem look-ahead), a estratégia
    passa a `def_trail` — um recuo que o trail normal não fecha, fecha no
    trail defensivo com lucro. Sem o estado (ou com PF >= 1), nada muda."""
    sig = make_signal(received_at="2026-08-17T11:00:00")
    sig.take_profit_3 = 1.0960
    # candle 1: TP1 (pico 1.0900); candle 2: pico novo 1.0918 (sem TP2);
    # candle 3: recuo a 1.0890 (low 1.0875): com trail 2.0 (dist 0.0140) o SL
    # fica em 1.0778 → nada fecha; com trail defensivo 0.5 (dist 0.0035) o SL
    # fica em 1.0883 → a perna fecha com lucro. Velas de 15 em 15 min a
    # começar 15 min antes do sinal (sinal 11:00 + delay 60 → o start cai na
    # vela das 11:15 = TP1; 11:30 = pico novo; 11:45 = recuo).
    cs = candles(highs=[1.0860, 1.0900, 1.0918, 1.0890, 1.0890],
                 lows=[1.0840, 1.0860, 1.0870, 1.0875, 1.0870],
                 start_ts=BASE + 2700)
    def_state = {"resolved": []}
    loss_sig = make_signal(received_at="2026-08-17T10:00:00")
    for i in range(10):
        loss_sig = make_signal(entry=1.0850 + i * 0.0001,
                               received_at="2026-08-17T10:00:00")
        # o sinal perde no SL (1 candle)
        simulate_signal(loss_sig, candles(highs=[1.0840], lows=[1.0820]),
                        1.0, 10_000, 60, def_window=10, def_trail=0.5,
                        def_time_stop=60, def_state=def_state)
    assert len(def_state["resolved"]) == 10

    # com o histórico de PF < 1, o modo defensivo aplica o trail 0.5
    res_def = simulate_signal(sig, cs, 1.0, 10_000, 60,
                              trail_factor=2.0, def_window=10, def_trail=0.5,
                              def_time_stop=60, def_state=def_state)
    assert res_def["outcome"] == "win", \
        "o modo defensivo fechou a perna no trail apertado com lucro"
    assert res_def["legs_pnl"][2] > 0

    # sem modo defensivo (PF das 10 >= 1 — 10 ganhos), o trail 2.0 não fecha
    # nada (o recuo fica acima do SL largo)
    fresh = {"resolved": [(f"w{i}", 50.0) for i in range(10)]}
    assert _roll_pf([p for _, p in fresh["resolved"]]) >= 1.0
    res_plain = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=2.0,
                                def_window=10, def_trail=0.5, def_state=fresh)
    assert res_plain["outcome"] == "open", "trail 2.0: o recuo não fecha nada"


def test_defensive_mode_requires_window_of_resolved():
    """O modo defensivo só dispara com `def_window` trades resolvidas — com
    menos histórico, os parâmetros normais aplicam-se (sem falsos positivos
    no arranque da conta)."""
    sig = make_signal(received_at="2026-08-17T11:00:00")
    sig.take_profit_3 = 1.0960
    cs = candles(highs=[1.0860, 1.0900, 1.0918, 1.0890, 1.0890],
                 lows=[1.0840, 1.0860, 1.0870, 1.0875, 1.0870],
                 start_ts=BASE + 2700)
    # só 3 perdidas — menos que a janela de 10
    st = {"resolved": []}
    loss_sig = make_signal(received_at="2026-08-17T10:00:00")
    for i in range(3):
        simulate_signal(loss_sig, candles(highs=[1.0840], lows=[1.0820]),
                        1.0, 10_000, 60, def_window=10, def_trail=0.5,
                        def_state=st)
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, trail_factor=2.0,
                          def_window=10, def_trail=0.5, def_state=st)
    assert res["outcome"] == "open", "sem 10 resolvidas, nada de defensivo"


def test_stair_and_trailing_no_conflict():
    """A escada tem prioridade: o trailing nunca desfaz a proteção da escada
    (só levanta, nunca baixa o SL abaixo do degrau já conquistado)."""
    sig = make_signal(tp2=1.0925)
    sig.take_profit_3 = 1.0960
    # candle 1: TP1 (degrau 1.0890, low acima do degrau -> pernas sobrevivem);
    # candle 2: TP2 (degrau 1.0925); candle 3: pico 1.0940 cujo trail
    # (1.0940−0.00375=1.09025) fica ABAIXO do degrau — a perna 3 mantém o SL
    # no degrau (1.0925) e fecha lá com lucro no recuo da candle 4
    cs = candles(highs=[1.0900, 1.0930, 1.0940, 1.0935],
                 lows=[1.0892, 1.0927, 1.0928, 1.0910])
    res = simulate_signal(sig, cs, 1.0, 10_000, 60, stair_levels=3,
                          stair_from=1, trail_factor=0.5, trace=True)
    leg3 = [e for e in res.get("events", [])
            if e["kind"] == "leg_sl" and "perna 3" in e["detail"]]
    assert leg3 and abs(leg3[0]["price"] - 1.0925) < 1e-9, \
        f"perna 3 fechou no {leg3[0]['price'] if leg3 else '?'} — devia ser o degrau 1.0925"
    assert res["outcome"] == "win" and res["legs_pnl"][3] > 0


def test_risk_per_signal_consistent():
    """O risco por SINAL é igual independentemente do nº de pernas: o risco é
    dividido pelas pernas (2/3/5 arriscam o mesmo montante total)."""
    from app.risk.engine import compute_lots
    sig = make_signal()
    amounts = {}
    for n in (2, 3, 5):
        lots = compute_lots(100_000, 5.0 / n, sig.entry, sig.stop_loss,
                            pip_size=0.0001, contract_size=100_000)
        # risco TOTAL do sinal = soma das n pernas (o arredondamento ao passo
        # 0.01 de cada perna causa diferenças pequenas e previsíveis)
        amounts[n] = n * lots * (sig.entry - sig.stop_loss) * 100_000
    spread = max(amounts.values()) - min(amounts.values())
    assert spread < max(amounts.values()) * 0.01, amounts
