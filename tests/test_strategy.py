import os
import tempfile
import pytest

from app.db import DB
from app.execution.engine import SimEngine, _contract, _pnl
from app.execution.market import MarketSimulator
from app.models import Signal


def make_signal(**over):
    base = dict(
        id="s1", source="test", raw_text="", symbol="EURUSD", direction="buy",
        entry=1.0850, stop_loss=1.0820, take_profit_1=1.0890,
        take_profit_2=1.0920, status="pending", received_at="",
    )
    base.update(over)
    return Signal(**base)


def setup():
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    market = MarketSimulator(seed=42)
    sim = SimEngine(db, market)
    return db, market, sim, tmp


def cleanup(db, tmp):
    db.close()  # release the file handle (required on Windows)
    if os.path.exists(tmp):
        os.remove(tmp)


def test_tp1_then_breakeven_then_tp2():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal()
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        # price reaches TP1 -> leg 1 closes at TP1, leg 2 moves to BE
        market.prices["EURUSD"] = 1.0890
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 1 and e["price"] == 1.0890 for e in closes)
        be = [e for e in events if e["type"] == "be"]
        assert be, "leg 2 should be protected at breakeven"

        leg2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert leg2.stop_loss == 1.0850  # moved to entry (BE)

        # price comes back to entry -> leg 2 closes at BE (no loss)
        market.prices["EURUSD"] = 1.0850
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 2 and e["price"] == 1.0850 for e in closes)

        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        # leg 1 closed at TP1 (profit), leg 2 closed at breakeven (0)
        assert closed.pnl > 0
    finally:
        cleanup(db, tmp)


def test_tp1_then_tp2_full_win():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal()
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        market.prices["EURUSD"] = 1.0890  # TP1
        sim.update()

        market.prices["EURUSD"] = 1.0920  # TP2
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 2 and e["price"] == 1.0920 for e in closes)

        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl > 0
    finally:
        cleanup(db, tmp)


def test_stop_loss_closes_both_legs():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal()
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        market.prices["EURUSD"] = 1.0820  # SL
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        legs = {e["leg"] for e in closes}
        assert legs == {1, 2}
        assert all(e["price"] == 1.0820 for e in closes)

        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl < 0
    finally:
        cleanup(db, tmp)


def test_sell_direction_hits_tp():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(direction="sell", entry=1.2700, stop_loss=1.2735,
                             take_profit_1=1.2650, take_profit_2=1.2600)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        market.prices["EURUSD"] = 1.2650  # TP1 for a sell
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 1 and e["price"] == 1.2650 for e in closes)
        assert any(e["type"] == "be" for e in events)
    finally:
        cleanup(db, tmp)


def test_three_legs_tp1_be_then_tp2_then_tp3():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(take_profit_3=1.0950)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)
        assert {p.leg for p in db.positions_for_trade(trade.id)} == {1, 2, 3}
        assert trade.lots_3 > 0

        market.prices["EURUSD"] = 1.0890  # TP1
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 1 and e["price"] == 1.0890 for e in closes)
        assert any(e["type"] == "be" for e in events)
        # every remaining leg moved to breakeven
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 1.0850

        market.prices["EURUSD"] = 1.0920  # TP2
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 2 and e["price"] == 1.0920 for e in closes)

        market.prices["EURUSD"] = 1.0950  # TP3
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert any(e["leg"] == 3 and e["price"] == 1.0950 for e in closes)

        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl > 0
    finally:
        cleanup(db, tmp)


def test_stop_loss_closes_all_three_legs():
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(take_profit_3=1.0950)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        market.prices["EURUSD"] = 1.0820  # SL
        events = sim.update()
        closes = [e for e in events if e["type"] == "close"]
        assert {e["leg"] for e in closes} == {1, 2, 3}
        assert all(e["price"] == 1.0820 for e in closes)

        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl < 0
    finally:
        cleanup(db, tmp)


def test_contract_per_symbol():
    """P&L do simulador/paper tem de usar o contract por símbolo — gold 100,
    silver 5000, forex 100000. O bug do 'paper com muito dinheiro' era gold a
    ser calculado com contract de forex (100000), 1000x inflacionado."""
    assert _contract("XAUUSD") == 100.0
    assert _contract("XAGUSD") == 5000.0
    assert _contract("EURUSD") == 100_000.0
    # gold: 3 USD * 0.01 lots * 100 units = 3.0 (não 3000)
    assert _pnl("buy", 4353.0, 4356.0, 0.01, "XAUUSD") == 3.0
    # silver: 0.50 USD * 0.01 lots * 5000 units = 25.0 (não 2500)
    assert _pnl("buy", 66.50, 67.00, 0.01, "XAGUSD") == 25.0
    # forex: 40 pips * 0.01 lots * 100000 = 4.0
    assert _pnl("buy", 1.0850, 1.0890, 0.01, "EURUSD") == 4.0


def test_silver_trade_pnl_uses_contract_5000():
    """Uma trade de prata no SimEngine tem de fechar com P&L na escala das
    dezenas/centenas de euros (contract 5000), não milhares (contract 100000)."""
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(symbol="XAGUSD", entry=66.50, stop_loss=66.00,
                             take_profit_1=67.00, take_profit_2=67.50)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)
        market.prices["XAGUSD"] = 67.00  # TP1
        sim.update()
        market.prices["XAGUSD"] = 67.50  # TP2
        sim.update()
        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl > 0
        # correcto ≈ 150 € (contract 5000); o bug dava ≈ 1500 € (contract 100000)
        assert closed.pnl < 500.0, f"P&L de prata inflacionado: {closed.pnl}"
    finally:
        cleanup(db, tmp)


def test_paper_balance_no_inflation():
    """End-to-end da corrida paper: uma trade de ouro fechada nos TPs não pode
    fazer o saldo virtual saltar para milhares de euros. Trava qualquer
    inflação futura de P&L (contract errado)."""
    from app.execution.paper import PaperRace
    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        signal = make_signal(symbol="XAUUSD", entry=4353.0, stop_loss=4345.0,
                             take_profit_1=4356.0, take_profit_2=4358.0,
                             take_profit_3=4360.0)
        trades = race.open_for_signal(signal, risk_pct=1.0, strategy_ids=["nova"])
        assert len(trades) == 1
        market.prices["XAUUSD"] = 4356.0  # TP1
        race.tick(1.0)
        market.prices["XAUUSD"] = 4358.0  # TP2
        race.tick(1.0)
        market.prices["XAUUSD"] = 4360.0  # TP3
        race.tick(1.0)
        bal = db.paper_balance("nova")
        assert bal is not None
        # 3 pernas a 0.01 lots, contract 100 → P&L ≈ 15 € → saldo ≈ 1015 €
        assert 1000.0 < bal < 1100.0, f"saldo paper inflacionado: {bal}"
    finally:
        cleanup(db, tmp)


def test_gold_trade_pnl_not_inflated():
    """Uma trade de ouro no SimEngine fecha no TP1 e o P&L tem de ser de
    euros (contract 100), não milhares de euros (contract 100000)."""
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(symbol="XAUUSD", entry=4353.0, stop_loss=4345.0,
                             take_profit_1=4356.0, take_profit_2=4358.0)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)
        market.prices["XAUUSD"] = 4356.0  # TP1
        sim.update()
        market.prices["XAUUSD"] = 4358.0  # TP2
        sim.update()
        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        assert closed.pnl > 0
        # 2 pernas, 0.01 lots cada, contract 100: bem abaixo de 100 €
        assert closed.pnl < 50.0, f"P&L inflacionado: {closed.pnl}"
    finally:
        cleanup(db, tmp)


def test_removed_strategies_gone_from_registry_and_get_strategy_raises():
    """Remoção completa (01/09): tp2/tp2_gestao/nova_gestao_3/auto_2/
    auto_protect_5/auto_escada_3_struggle saíram do registo — o backtest
    histórico dessas variantes deixou de ser reproduzível (CHANGELOG 01/09).
    `nova`/`auto_5` ficam (contrafactual/comparação, MENU_HIDDEN); a máquina
    de menu deriva do registo e o get_strategy levanta para ids mortos."""
    from app.strategies import (HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES,
                                STRATEGIES, get_strategy)
    removed = {"tp2", "tp2_gestao", "nova_gestao_3", "auto_2",
               "auto_protect_5", "auto_escada_3_struggle"}
    for sid in removed:
        assert sid not in STRATEGIES, f"{sid} devia ter sido removida a 01/09"
    assert "tp2_protect" in STRATEGIES and "nova" in STRATEGIES
    assert "nova_gestao" in STRATEGIES
    # já não há estratégias "retired"/"backtest_only" no registo; as únicas
    # ocultas do menu são as referências de backtest nova/auto_5
    assert HIDDEN_STRATEGIES == set()
    assert MENU_HIDDEN_STRATEGIES == {"nova", "auto_5"}
    # id morto => ValueError ruidoso (sem fallback silencioso para a main)
    for sid in removed:
        try:
            get_strategy(sid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"get_strategy({sid}) devia levantar ValueError")
    # um estudo típico (sim_params/backtest) também falha em voz alta
    from app.strategies import sim_params
    for sid in ("tp2", "auto_2"):
        try:
            sim_params(sid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"sim_params({sid}) devia levantar ValueError")
    # ids vivos continuam a devolver a estratégia certa + parâmetros
    assert get_strategy("tp2_protect")["id"] == "tp2_protect"
    # 16/09: o grupo pediu para a tp2_protect honrar CLOSE ALL como a
    # nova_gestao — as duas lanes saem ao mercado na mesma instrução.
    assert get_strategy("tp2_protect")["manage_kinds"] == {"protect", "close_all"}
    assert get_strategy("nova")["manage"] is False
    assert get_strategy("nova")["id"] == "nova"
    assert get_strategy(None)["id"] == "nova_gestao"


def test_hybrid_strategy_is_visible_and_configured():
    from app.strategies import HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES, get_strategy

    s = get_strategy("auto_escada_3_hibrida")
    assert "auto_escada_3_hibrida" not in HIDDEN_STRATEGIES
    assert "auto_escada_3_hibrida" not in MENU_HIDDEN_STRATEGIES
    assert s["max_legs"] == 3
    assert s["stair_levels"] == 3
    assert s["stair_from"] == 1
    assert s["trail_factor"] == 2.0
    assert s["def_window"] == 10
    assert s["def_trail"] == 1.5
    assert s["def_time_stop"] == 60
    assert s["manage_kinds"] == {"protect"}


def test_hybrid_gets_own_paper_account():
    from app.execution.paper import PaperRace

    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        opened = race.open_for_signal(
            make_signal(id="s-hybrid", take_profit_3=1.0950),
            risk_pct=1.0,
            strategy_ids=["auto_escada_3_hibrida"],
            daily_stop_pct=0,
        )
        assert len(opened) == 1
        assert opened[0].strategy_id == "auto_escada_3_hibrida"
        assert db.paper_balance("auto_escada_3_hibrida") == 1000.0
    finally:
        cleanup(db, tmp)


def test_paper_hybrid_protect_is_monotonic_and_logged():
    from app.execution.paper import PaperRace

    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        signal = make_signal(
            id="s-paper-hybrid", take_profit_3=1.0950,
            received_at="2026-08-19T10:00:00+00:00",
        )
        trades = race.open_for_signal(
            signal, 1.0, strategy_ids=["auto_escada_3_hibrida"], daily_stop_pct=0,
        )
        assert len(trades) == 1
        # Slightly above TP1: the tick records TP1/escada but does not
        # immediately cross the new stair SL at exactly TP1.
        market.prices[signal.symbol] = signal.take_profit_1 + 0.0005
        race.tick(1.0)
        # Um restart perde o dicionário em memória, mas o trailing/time-stop
        # deve continuar a partir do TP1 persistido, não ficar desligado.
        restarted = PaperRace(db, market)
        trade_row = db.paper_open_trades("auto_escada_3_hibrida")[0]
        restarted_state = restarted._auto_state(
            trade_row, trade_row["direction"], market.price(signal.symbol),
            {p.leg: p for p in db.paper_positions_for_trade(trade_row["id"])},
        )
        assert restarted_state["tp1_ts"] is not None
        assert restarted_state["auto_sl_by_leg"], \
            "o restart deve reconstruir também os SLs mecânicos aplicados"
        before = db.paper_positions_for_trade(trades[0].id)
        before_sl = min(p.stop_loss for p in before if p.leg != 1)
        race.apply_management(signal.id, "protect", msg_id="protect-1")
        after = db.paper_positions_for_trade(trades[0].id)
        assert min(p.stop_loss for p in after if p.leg != 1) >= before_sl
        rows = db.protect_latency_rows(
            strategy_id="auto_escada_3_hibrida", engine="paper"
        )
        assert len(rows) == 2
        assert all(r["engine"] == "paper" for r in rows)
    finally:
        cleanup(db, tmp)


def test_divergence_report_catches_auto_closing_before_reference():
    """A divergência não pode depender da ordem de fecho.

    A auto pode sair primeiro por trailing e a referência só fechar depois
    quando chega a gestão do grupo; nesse caso o feed ainda tem de comparar
    os dois P&L.
    """
    from app.execution.paper import PaperRace

    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        signal = make_signal(id="s-auto-first", take_profit_3=1.0950)
        race.open_for_signal(
            signal, 1.0, strategy_ids=["auto_3", "nova_gestao"],
            daily_stop_pct=0,
        )
        auto = next(t for t in db.paper_open_trades("auto_3")
                    if t["signal_id"] == signal.id)
        # A auto fecha primeiro; a referência continua aberta. Usa razões
        # diferentes no mesmo tick para garantir que o trade fica com a razão
        # da perna realmente fechada por último, não a de maior número.
        positions = db.paper_positions_for_trade(auto["id"])
        for pos in positions[:-1]:
            race._close(auto, pos, 1.0880, "early")
        race._close(auto, positions[-1], 1.0880, "trail")
        race._finalize(auto, {p.leg: p for p in positions})
        auto_row = next(t for t in db.paper_trades("auto_3")
                        if t["signal_id"] == signal.id)
        assert auto_row["exit_reason"] == "trail"
        assert all(p.closed_at for p in positions)

        # A gestão fecha a referência depois. O evento tem os dois P&L.
        events = race.apply_management(signal.id, "close_all")
        divergences = [e for e in events if e["type"] == "divergence"]
        assert divergences
        assert divergences[0]["reason"] == "trail"
        assert divergences[0]["pnl"] is not None
        assert divergences[0]["ref_pnl"] is not None

        # Ordem inversa: a referência fecha primeiro e o feed avisa que a
        # auto continua; quando a auto fecha, chega uma atualização com os
        # dois P&L finais em vez de ficar deduplicada para sempre.
        late_signal = make_signal(id="s-ref-first")
        race.open_for_signal(
            late_signal, 1.0, strategy_ids=["auto_3", "nova_gestao"],
            daily_stop_pct=0,
        )
        provisional = race.apply_management(late_signal.id, "close_all")
        pending = [e for e in provisional if e["type"] == "divergence"]
        assert pending and pending[0]["pending"] is True
        assert pending[0]["pnl"] is None
        late_auto = next(t for t in db.paper_open_trades("auto_3")
                         if t["signal_id"] == late_signal.id)
        late_positions = db.paper_positions_for_trade(late_auto["id"])
        for pos in late_positions:
            race._close(late_auto, pos, 1.0880, "trail")
        final_events = race._finalize(
            late_auto, {p.leg: p for p in late_positions}
        )
        final_divergences = [e for e in final_events if e["type"] == "divergence"]
        assert final_divergences
        assert final_divergences[0]["pending"] is False
        assert final_divergences[0]["pnl"] is not None
        assert final_divergences[0]["ref_pnl"] is not None
    finally:
        cleanup(db, tmp)


def test_defensive_mode_params_in_registry():
    """Modo defensivo (estudo 19/08): as `auto_escada_3/5` têm
    `def_window`/`def_trail`/`def_time_stop` no registo (PF(10) < 1.0 →
    trail 1.5 + time stop 60 min, sem cortar pernas); as restantes não
    (a maquinaria fica inerte). O `sim_params` expõe os parâmetros para o
    backtest/paper/MT5."""
    from app.strategies import STRATEGIES, get_strategy, sim_params
    for sid in ("auto_escada_3", "auto_escada_5"):
        s = get_strategy(sid)
        assert s["def_window"] == 10
        assert s["def_trail"] == 1.5
        assert s["def_time_stop"] == 60
        assert s["def_risk_pct"] == 1.25
        p = sim_params(sid)
        assert p["def_window"] == 10 and p["def_trail"] == 1.5
        assert p["def_time_stop"] == 60
        assert p["def_risk_pct"] == 1.25
    # a nova_gestao (principal) e as sem auto-gestão não definem modo defensivo
    for sid in ("nova_gestao", "auto_3", "tp2_protect"):
        assert not get_strategy(sid).get("def_window")
        assert not sim_params(sid).get("def_window")
    assert "def_window" in sim_params("auto_escada_3")


def test_paper_defensive_risk_cap_is_applied_after_ten_losses():
    from app.execution.paper import PaperRace

    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        for i in range(10):
            signal = make_signal(id=f"def-loss-{i}")
            race.open_for_signal(
                signal, 2.5, strategy_ids=["auto_escada_3"], daily_stop_pct=0,
            )
            row = db.paper_trade_for_signal_any(signal.id, "auto_escada_3")
            positions = db.paper_positions_for_trade(row["id"])
            for pos in positions:
                race._close(row, pos, signal.stop_loss, "sl")
            race._finalize(row, {p.leg: p for p in positions})

        next_trade = race.open_for_signal(
            make_signal(id="def-next"), 2.5,
            strategy_ids=["auto_escada_3"], daily_stop_pct=0,
        )[0]
        assert next_trade.risk_pct == 1.25
    finally:
        cleanup(db, tmp)


def test_paper_protection_peak_survives_restart():
    """A restart must preserve the peak used by trailing, not re-seed it from
    the current price and silently give back already observed protection."""
    from app.execution.paper import PaperRace

    db, market, sim, tmp = setup()
    try:
        race = PaperRace(db, market)
        signal = make_signal(id="persisted-peak", take_profit_3=1.0950)
        trade = race.open_for_signal(
            signal, 2.0, strategy_ids=["auto_escada_3"], daily_stop_pct=0,
        )[0]
        peak = signal.take_profit_1 + 0.0010
        market.prices[signal.symbol] = peak
        race.tick(2.0)
        persisted = db.load_protection_state("paper", trade.id, origin="race")
        assert persisted and persisted["peak"] == peak

        restarted = PaperRace(db, market)
        row = db.paper_trade_for_signal_any(signal.id, "auto_escada_3")
        positions = {p.leg: p for p in db.paper_positions_for_trade(row["id"])}
        state = restarted._auto_state(row, row["direction"], signal.entry, positions)
        assert state["peak"] == peak
    finally:
        cleanup(db, tmp)


def test_sim_engine_uses_shared_auto_policy():
    """O modo sim não pode ignorar uma promoção auto: TP1 sobe o degrau e
    um recuo posterior fecha as pernas restantes no lucro protegido."""
    db, market, sim, tmp = setup()
    try:
        signal = make_signal(take_profit_3=1.0950)
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)
        auto = {
            "stair_levels": 3, "stair_from": 1,
            "confirm_filter": False, "trail_factor": 0.0,
            "time_stop_minutes": 0,
        }
        # Overshoot TP1 so the newly-created stair SL at TP1 is not hit in
        # the same point tick (the shared SL-first tie rule is conservative).
        market.prices[signal.symbol] = signal.take_profit_1 + 0.0005
        first = sim.update(now="2026-08-19T10:00:00+00:00", auto=auto)
        assert any(event["type"] == "close" and event["leg"] == 1
                   for event in first)
        remaining = [p for p in db.positions_for_trade(trade.id) if p.leg != 1]
        assert all(p.stop_loss == signal.take_profit_1 for p in remaining)

        market.prices[signal.symbol] = signal.take_profit_1 - 0.0005
        final = sim.update(now="2026-08-19T10:01:00+00:00", auto=auto)
        assert {event["leg"] for event in final if event["type"] == "close"} == {2, 3}
        assert db.get_trade(trade.id).status == "closed"
        assert db.get_trade(trade.id).pnl > 0
    finally:
        cleanup(db, tmp)


def test_tp1_breakeven_plus_buffer_sim():
    """BE+ (tp2_protect): SimEngine com `be_buffer_pips` move o SL da perna
    2 para entrada+buffer após o TP1 — a reversão fecha com lucro, não a 0."""
    db, market, sim, tmp = setup()
    try:
        signal = make_signal()
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)
        auto = {"be_buffer_pips": 4.0}  # 4 pips × 0.0001 = 0.0004

        market.prices["EURUSD"] = 1.0890  # TP1
        sim.update(auto=auto)
        leg2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert leg2.status == "open"
        assert leg2.stop_loss == pytest.approx(1.0854)  # entry + buffer

        # reversão: o preço toca a entrada (1.0850), abaixo do BE+ → fecha
        market.prices["EURUSD"] = 1.0850
        sim.update(auto=auto)
        leg2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert leg2.status == "closed"
        assert leg2.exit_reason == "trail"  # SL acima da entrada = protegido
        assert leg2.pnl > 0
    finally:
        cleanup(db, tmp)


def test_tp1_breakeven_classic_without_buffer_sim():
    """Sem `be_buffer_pips` (auto=None), o BE clássico mantém-se — a perna 2
    fecha exatamente na entrada (regressão do comportamento anterior)."""
    db, market, sim, tmp = setup()
    try:
        signal = make_signal()
        trade = sim.open_trade(signal, risk_pct=1.0, balance=10_000)

        market.prices["EURUSD"] = 1.0890  # TP1
        sim.update()
        leg2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert leg2.stop_loss == 1.0850  # BE clássico: entrada seca
    finally:
        cleanup(db, tmp)
