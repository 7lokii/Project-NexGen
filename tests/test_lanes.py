"""Multi-lane de execução: duas estratégias principais em paralelo (conta
principal `nova_gestao` + segunda demo `tp2_protect`), cada uma com a sua
engine e lane_id. Estes testes cobrem a configuração das lanes, a abertura
por lane, o roteamento de gestão e a migração da coluna lane_id.
"""

import asyncio
import os

from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.models import Signal, SignalIn
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager


def _signal(sid="s1") -> Signal:
    return Signal(
        id=sid, source="tg", raw_text="", symbol="XAUUSD",
        direction="buy", entry=4400.0, stop_loss=4394.0,
        take_profit_1=4402.0, take_profit_2=4404.0,
        take_profit_3=4406.0, take_profit_4=4408.0,
        take_profit_5=4412.0, status="pending",
        received_at="2026-08-21T10:00:00+00:00",
    )


def _state(db, second_sim=None):
    market = MarketSimulator()
    sim = SimEngine(db, market)
    state = BotState(
        db=db, market=market, risk=RiskEngine(),
        sim=sim, notifier=Notifier(db, WSManager()), ws=WSManager(),
        second_sim=second_sim, second_strategy="tp2_protect",
    )
    state.config["balance"] = 100000.0  # lote bem dimensionado (sem gate mínimo)
    state.config["mode"] = "auto"
    return state


def test_lanes_defaults_to_main(tmp_path):
    db = DB(str(tmp_path / "a.db"))
    try:
        state = _state(db)
        assert state._lanes() == ["main"]
        assert state._lane_engine("main") is state.sim
        assert state._lane_strategy_id("main") == "nova_gestao"
        assert state._lanes_snapshot()[0]["lane_id"] == "main"
    finally:
        db.close()


def test_lanes_with_second_sim(tmp_path):
    db = DB(str(tmp_path / "b.db"))
    try:
        market = MarketSimulator()
        sim2 = SimEngine(db, market)
        state = _state(db, second_sim=sim2)
        assert state._lanes() == ["main", "tp2"]
        assert state._lane_engine("tp2") is sim2
        assert state._lane_strategy_id("tp2") == "tp2_protect"
        snap = {l["lane_id"]: l for l in state._lanes_snapshot()}
        assert snap["main"]["strategy_id"] == "nova_gestao"
        assert snap["main"]["max_legs"] is None
        assert snap["tp2"]["strategy_id"] == "tp2_protect"
        assert snap["tp2"]["max_legs"] == 2
    finally:
        db.close()


def test_try_execute_opens_one_trade_per_lane(tmp_path):
    db = DB(str(tmp_path / "c.db"))
    try:
        market = MarketSimulator()
        state = _state(db, second_sim=SimEngine(db, market))
        res = asyncio.run(state._try_execute(_signal()))
        assert res["action"] == "executed"
        assert len(res["trades"]) == 2
        by_lane = {t["lane_id"]: t for t in res["trades"]}
        assert set(by_lane) == {"main", "tp2"}
        assert by_lane["main"]["strategy_id"] == "nova_gestao"
        assert by_lane["tp2"]["strategy_id"] == "tp2_protect"
        # duas trades distintas, ambas abertas
        rows = db.open_trades()
        assert len(rows) == 2
        assert {t.lane_id for t in rows} == {"main", "tp2"}
    finally:
        db.close()


def test_two_leg_lane_opens_when_five_leg_lane_hits_min_lot_gate(tmp_path):
    """Modo autónomo: quando o lote mínimo bloqueia a conta de 5 pernas, a
    main faz auto-fit (menos pernas no orçamento) e a lane de 2 pernas abre
    normal — nenhuma lane fica pendente de confirmação."""
    db = DB(str(tmp_path / "independent-risk.db"))
    try:
        market = MarketSimulator()
        state = _state(db, second_sim=SimEngine(db, market))
        state.config["balance"] = 1000.0
        state.config["lane2_balance"] = 100000.0
        state.config["risk_pct"] = 2.5
        state.config["peak_balance"] = 1000.0
        state.config["peak_balance:tp2"] = 100000.0

        res = asyncio.run(state._try_execute(_signal("independent-risk")))

        assert res["action"] == "executed"
        by_lane = {t["lane_id"]: t for t in res["trades"]}
        assert set(by_lane) == {"main", "tp2"}
        # main: lote mínimo domina (0.01×6 pips×100 = 6 €/perna) → auto-fit:
        # 25 € / 6 € = 4 pernas, marcado leg_fit:4
        main_trade = by_lane["main"]
        assert "leg_fit:4" in (main_trade.get("entry_guard") or "")
        assert len(db.positions_for_trade(main_trade["id"])) == 4
        # tp2: saldo grande → dimensionamento normal (2 pernas da tp2_protect)
        tp2_trade = by_lane["tp2"]
        assert tp2_trade["strategy_id"] == "tp2_protect"
        assert len(db.positions_for_trade(tp2_trade["id"])) == 2
        assert len(db.open_trades()) == 2
    finally:
        db.close()


def test_target_trades_returns_all_lanes_for_same_signal(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    try:
        market = MarketSimulator()
        state = _state(db, second_sim=SimEngine(db, market))
        signal = _signal()
        state.db.insert_signal(signal)
        asyncio.run(state._try_execute(signal))
        # reply ao sinal -> ambas as lanes do mesmo signal_id
        body = SignalIn(raw_text="we're out", source="tg",
                        reply_to_msg_id=1, group="g")
        # reply_to_msg_id=1 -> tg-g-1; usamos o signal_id diretamente via
        # _target_signal_for precisa de reply. Testamos o fallback por símbolo
        # (uma só trade/sinal) diretamente:
        trades = state._target_trades(
            SignalIn(raw_text="we're out XAUUSD", source="tg"), db.open_trades(),
        )
        assert len(trades) == 2
        assert {t.lane_id for t in trades} == {"main", "tp2"}
    finally:
        db.close()


def test_group_protect_moves_remaining_sl_without_auto_gate(tmp_path):
    """REGRESSÃO 15/09 11:49 (−13,93 €): 'Trade is risk free now' numa trade
    tp2_protect SEM proteção auto — o antigo gate causal recusava a subida
    do SL e a perna rodou com o SL completo até bater. Agora o protect do
    grupo (sempre monotónico) põe TODAS as restantes em BE+ na hora."""
    db = DB(str(tmp_path / "regress.db"))
    try:
        market = MarketSimulator()
        market.prices["XAUUSD"] = 4285.0  # preço acima da entrada 4284
        state = _state(db, second_sim=SimEngine(db, market))
        signal = _signal("tg-4395009858-106")
        signal.entry = 4284.0
        signal.stop_loss = 4280.0
        signal.take_profit_1 = 4290.0
        signal.take_profit_2 = 4295.0
        state.db.insert_signal(signal)
        asyncio.run(state._try_execute(signal))
        tp2_trade = next(t for t in db.open_trades()
                         if t.lane_id == "tp2" and t.strategy_id == "tp2_protect")
        p2 = [p for p in db.positions_for_trade(tp2_trade.id) if p.leg == 2][0]
        assert p2.stop_loss == 4280.0, "SL original no momento do protect"
        # "Trade is risk free now" → kind protect, sem proteção auto
        notes = state._apply_management_to_trade(
            tp2_trade, "protect", {}, msg_id=None,
        )
        p2 = [p for p in db.positions_for_trade(tp2_trade.id) if p.leg == 2][0]
        assert p2.stop_loss > 4284.0, \
            f"BE+ aplicado sem gate (acima da entrada): {p2.stop_loss}"
        assert any("SL" in n or "entrada" in n for n in notes)
    finally:
        db.close()


def test_apply_management_to_trade_uses_lane_engine(tmp_path):
    db = DB(str(tmp_path / "e.db"))
    try:
        market = MarketSimulator()
        sim2 = SimEngine(db, market)
        state = _state(db, second_sim=sim2)
        signal = _signal()
        state.db.insert_signal(signal)
        asyncio.run(state._try_execute(signal))
        rows = db.open_trades()
        assert len(rows) == 2
        # close_all aplica-se a cada lane; nova_gestao honra close_all,
        # tp2_protect também (protect-only? tp2_protect honra close_all? não:
        # manage_kinds de tp2_protect = {protect}. Confirma o filtro por lane.)
        from app.strategies import get_strategy
        tp2_kinds = get_strategy("tp2_protect")["manage_kinds"]
        main_kinds = get_strategy("nova_gestao")["manage_kinds"]
        for t in rows:
            strat_id = state._lane_strategy_id(t.lane_id)
            notes = state._apply_management_to_trade(
                t, "close_all", {"be": False}, msg_id=None,
            )
            if "close_all" not in get_strategy(strat_id)["manage_kinds"]:
                assert any("não aplica" in n for n in notes)
            else:
                assert not any("não aplica" in n for n in notes)
        assert "close_all" in main_kinds
    finally:
        db.close()


def test_legacy_peak_is_rebased_to_current_lane_balance(tmp_path):
    db = DB(str(tmp_path / "peak.db"))
    try:
        state = _state(db)
        state.config["balance"] = 1000.0
        state.config["risk_pct"] = 1.5
        state.config["peak_balance"] = 100000.0
        state._normalize_lane_peaks()
        assert state.config["peak_balance"] == 1000.0
        assert db.all_config()["peak_balance"] == "1000.0"
        assert state._effective_risk_pct() == 1.5
    finally:
        db.close()


def test_lane2_synthetic_account_from_env(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "f.db"))
    try:
        market = MarketSimulator()
        state = _state(db, second_sim=SimEngine(db, market))
        monkeypatch.setenv("MT5_2_LOGIN", "111495351")
        monkeypatch.setenv("MT5_2_SERVER", "MetaQuotes-Demo")
        acct = state._lane2_synthetic_account()
        assert acct["login"] == 111495351
        assert acct["server"] == "MetaQuotes-Demo"
        assert acct["dry_run"] is True
    finally:
        db.close()


def test_lane_id_migration_defaults_main(tmp_path):
    # Cria a BD, insere uma trade sem lane_id explícito e confirma que a
    # migração/leitura devolve "main".
    db = DB(str(tmp_path / "g.db"))
    try:
        from app.models import Trade
        t = Trade(
            id="t-old", signal_id="s", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4394.0, take_profit_1=4402.0,
            take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="open", opened_at="2026-08-21T10:00:00+00:00",
        )
        db.insert_trade(t)
        row = db.get_trade("t-old")
        assert row.lane_id == "main"
    finally:
        db.close()


def test_kill_switch_no_forced_rearm(tmp_path):
    """Kill switch liga sem impor rearm agendado."""
    db = DB(str(tmp_path / "ks.db"))
    try:
        state = _state(db)
        # Simulate boot with kill_switch=True but no rearm
        state.db.set_config("kill_switch", "true")
        state.db.set_config("auto_kill", "false")
        state.db.set_config("killswitch_rearm_at", "")
        state.config = state._load_config()
        # The forced rearm "2026-08-29" must NOT be set
        assert state.config["kill_switch"] is True
        assert state.config["killswitch_rearm_at"] == ""
    finally:
        db.close()


def test_lane_divergences_shows_different_pnl(tmp_path):
    """lane_divergences returns signals where main and tp2 diverge."""
    db = DB(str(tmp_path / "ld.db"))
    try:
        market = MarketSimulator()
        sim2 = SimEngine(db, market)
        state = _state(db, second_sim=sim2)
        from app.models import Trade
        # Closed trade in main with pnl=50
        db.insert_trade(Trade(
            id="t-m1", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4394.0, take_profit_1=4402.0,
            take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed", opened_at="2026-08-21T10:00:00+00:00",
            closed_at="2026-08-21T10:05:00+00:00", pnl=50.0,
            exit_reason="tp1", lane_id="main",
        ))
        # Closed trade in tp2 with pnl=10 and different reason
        db.insert_trade(Trade(
            id="t-t1", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4394.0, take_profit_1=4402.0,
            take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed", opened_at="2026-08-21T10:00:00+00:00",
            closed_at="2026-08-21T10:05:00+00:00", pnl=10.0,
            exit_reason="tp2", lane_id="tp2",
        ))
        divs = state.lane_divergences()
        assert len(divs) == 1
        d = divs[0]
        assert d["signal_id"] == "s1"
        assert d["main_pnl"] == 50.0
        assert d["tp2_pnl"] == 10.0
        assert d["main_reason"] == "tp1"
        assert d["tp2_reason"] == "tp2"
        assert d["pnl_delta"] == -40.0
    finally:
        db.close()


def test_lane_divergences_skips_identical(tmp_path):
    """lane_divergences skips signals where both lanes have same result."""
    db = DB(str(tmp_path / "li.db"))
    try:
        market = MarketSimulator()
        sim2 = SimEngine(db, market)
        state = _state(db, second_sim=sim2)
        from app.models import Trade
        for lid, pnl_val in [("main", 30.0), ("tp2", 30.0)]:
            db.insert_trade(Trade(
                id=f"t-{lid}", signal_id="s2", symbol="XAUUSD", direction="buy",
                entry=4400.0, stop_loss=4394.0, take_profit_1=4402.0,
                take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
                status="closed", opened_at="2026-08-21T10:00:00+00:00",
                closed_at="2026-08-21T10:05:00+00:00", pnl=pnl_val,
                exit_reason="tp1", lane_id=lid,
            ))
        divs = state.lane_divergences()
        assert len(divs) == 0
    finally:
        db.close()


def test_lane_status_returns_both_lanes(tmp_path):
    """lane_status returns one entry per active lane."""
    db = DB(str(tmp_path / "ls.db"))
    try:
        market = MarketSimulator()
        sim2 = SimEngine(db, market)
        state = _state(db, second_sim=sim2)
        status = state.lane_status()
        assert len(status) == 2
        ids = {s["lane_id"] for s in status}
        assert ids == {"main", "tp2"}
        for s in status:
            assert "strategy_id" in s
            assert "total_pnl" in s
            assert "trades_total" in s
    finally:
        db.close()


def test_per_lane_dry_run_independence(tmp_path, monkeypatch):
    """MT5_2_DRY_RUN is read independently from MT5_DRY_RUN."""
    monkeypatch.setenv("MT5_DRY_RUN", "0")
    monkeypatch.setenv("MT5_2_DRY_RUN", "1")
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)
    assert main_mod.MT5_DRY_RUN is False
    assert main_mod.MT5_2_DRY_RUN is True
    monkeypatch.delenv("MT5_DRY_RUN")
    monkeypatch.delenv("MT5_2_DRY_RUN")


# ------------------------------------------------- /segundaria: lane= e risco


def test_lanes_snapshot_is_self_contained_per_lane(tmp_path):
    """A entrada `lanes[]` do snapshot leva tudo o que a vista ?lane= precisa
    (saldo, equity, P&L do dia, abertas, risco efetivo e preview de lotes)."""
    db = DB(str(tmp_path / "snap.db"))
    try:
        sim2 = SimEngine(db, MarketSimulator())
        state = _state(db, second_sim=sim2)
        state.config["balance"] = 10000.0
        state.config["lane2_balance"] = 20000.0
        lanes = state._lanes_snapshot()
        assert len(lanes) == 2
        for lane in lanes:
            assert "balance" in lane and "equity" in lane
            assert "open_pnl" in lane and "realized_today" in lane
            assert "open_trades" in lane and "effective_risk_pct" in lane
            assert "risk_preview" in lane and "strategy_name" in lane
        by_lane = {l["lane_id"]: l for l in lanes}
        # a lane tp2 usa o SALDO dela (não o da principal) no risk_preview
        assert by_lane["tp2"]["risk_preview"]["balance"] == 20000.0
        assert by_lane["main"]["risk_preview"]["balance"] == 10000.0
    finally:
        db.close()


def test_risk_preview_accepts_lane(tmp_path):
    """risk_preview(lane) dimensiona com o saldo, risco dinâmico e max_legs
    da lane — a 2ª conta (10k, tp2_protect com 2 pernas) não pode usar os
    valores da principal."""
    db = DB(str(tmp_path / "rp.db"))
    try:
        sim2 = SimEngine(db, MarketSimulator())
        state = _state(db, second_sim=sim2)
        state.config["balance"] = 1000.0
        state.config["lane2_balance"] = 10000.0
        rp_main = state.risk_preview("main")
        rp_tp2 = state.risk_preview("tp2")
        # saldo/risco por lane
        assert rp_main["balance"] == 1000.0
        assert rp_tp2["balance"] == 10000.0
        # max_legs da estratégia da lane (tp2_protect = 2 pernas)
        assert rp_tp2["n_legs"] == 2
        # com 10× o saldo e risco igual, a lane 2 arredonda a ~10× os lotes
        assert rp_tp2["lots_total"] >= rp_main["lots_total"]
        assert "lane_id" in rp_tp2 and rp_tp2["lane_id"] == "tp2"
        # sem lane -> comportamento anterior (main)
        assert state.risk_preview() == rp_main
        # a entrada de lane do snapshot leva o preview da própria lane
        lanes = {l["lane_id"]: l for l in state._lanes_snapshot()}
        assert lanes["tp2"]["risk_preview"]["n_legs"] == 2
        assert lanes["tp2"]["effective_risk_pct"] > 0
    finally:
        db.close()


def test_fixed_lots_por_perna_lane2(tmp_path):
    """`lots_override:tp2` na config fixa o lote POR PERNA da 2ª conta
    (ex.: 0.04): o risk_preview da lane passa a mostrar 0.04/perna (0.08
    total em tp2_protect de 2 pernas) em vez de dimensionar pelo risco;
    a main fica inalterada e 0/ausência = dimensionamento por risco."""
    db = DB(str(tmp_path / "fixed.db"))
    try:
        sim2 = SimEngine(db, MarketSimulator())
        state = _state(db, second_sim=sim2)
        state.config["lane2_balance"] = 10000.0
        # sem chave própria -> dimensionamento por risco (None)
        assert state._fixed_lots_for_lane("tp2") is None
        assert state._fixed_lots_for_lane("main") is None
        state.config["lots_override:tp2"] = 0.04
        assert state._fixed_lots_for_lane("tp2") == 0.04
        assert state._fixed_lots_for_lane("main") is None
        rp = state.risk_preview("tp2")
        assert rp["lots_per_leg"] == 0.04
        assert rp["lots_total"] == 0.08  # tp2_protect = 2 pernas
        assert rp.get("lots_override") == 0.04
        # a preview da main continua por risco (sem chave)
        assert "lots_override" not in state.risk_preview("main")
        # a entrada de lane do snapshot leva o lote fixo por perna
        lanes = {l["lane_id"]: l for l in state._lanes_snapshot()}
        assert lanes["tp2"]["fixed_lots_per_leg"] == 0.04
        assert lanes["main"]["fixed_lots_per_leg"] is None
        # 0 = volta ao dimensionamento por risco
        state.config["lots_override:tp2"] = 0.0
        assert state._fixed_lots_for_lane("tp2") is None
    finally:
        db.close()


def test_fixed_lots_lane2_usado_na_abertura(tmp_path):
    """Com `lots_override:tp2=0.04`, a abertura de uma trade REAL na lane 2
    (`_open_trade_on_lane`) usa 0.04 por perna (o dimensionamento por risco
    deixa de mandar); sem a chave, volta a dimensionar pelo risco/saldo."""
    db = DB(str(tmp_path / "fixedopen.db"))
    try:
        sim2 = SimEngine(db, MarketSimulator())
        state = _state(db, second_sim=sim2)
        state.config["lane2_balance"] = 10000.0
        sig = _signal("s-fixed-1")
        strat = {"id": "tp2_protect", "max_legs": 2}
        guard = {"status": "in_zone"}
        # SEM chave: dimensiona pelo risco (balance alto de 100k -> muito >0.04)
        t_risk = state._open_trade_on_lane(
            sig, 1.0, strat, sim2, "tp2", guard, False, "")
        assert t_risk is not None and t_risk.status == "open"
        lots_risk = [p.lots for p in db.positions_for_trade(t_risk.id)]
        assert len(lots_risk) == 2 and all(l > 0.04 for l in lots_risk)
        # COM a chave: cada perna abre a 0.04, fixo
        state.config["lots_override:tp2"] = 0.04
        t_fix = state._open_trade_on_lane(
            _signal("s-fixed-2"), 1.0, strat, sim2, "tp2", guard, False, "")
        assert t_fix is not None and t_fix.status == "open"
        lots_fix = [p.lots for p in db.positions_for_trade(t_fix.id)]
        assert lots_fix == [0.04, 0.04]
        # a main (sem chave) continua por risco
        assert state._fixed_lots_for_lane("main") is None
    finally:
        db.close()


def test_notifier_stamps_lane_and_snapshot_exposes_it(tmp_path):
    """Notifier.send(lane=) grava o campo na BD, no broadcast WS e no
    snapshot (a serialização que o feed da dashboard consome) — os eventos
    das duas contas ficam distinguíveis."""
    import asyncio
    from app.execution.feed import MarketFeed
    from app.notify.telegram import Notifier

    db = DB(str(tmp_path / "nf.db"))
    ws = WSManager()
    calls = []

    class _WS:
        async def broadcast(self, payload):
            calls.append(payload)

    market = MarketFeed(provider=None)
    st = BotState(db=db, market=market, risk=RiskEngine(),
                  sim=SimEngine(db, market),
                  notifier=Notifier(db, ws), ws=ws)
    st.notifier = Notifier(db, _WS())
    try:
        asyncio.run(st.notifier.send("trade_close", "fecho tp2", lane="tp2"))
        asyncio.run(st.notifier.send("system", "check diário", lane=""))
        n = st.db.list_notifications(10)
        by_text = {x.text: x for x in n}
        assert by_text["fecho tp2"].lane == "tp2"
        assert by_text["check diário"].lane == ""
        # broadcast WS carrega o lane
        assert calls[0]["lane"] == "tp2"
        assert calls[1]["lane"] == ""
        # snapshot expõe lane nas notificações
        snap = st.snapshot()
        item = next(x for x in snap["notifications"] if x["text"] == "fecho tp2")
        assert item["lane"] == "tp2"
        item2 = next(x for x in snap["notifications"] if x["text"] == "check diário")
        assert item2["lane"] == ""
    finally:
        db.close()


def test_lane_ab_legs_confronto_perna_a_perna(tmp_path):
    """O confronto de lanes junta as pernas das DUAS trades do sinal e a fita
    do journal (fills/moves com hora), atribuída à lane pelo trade_id."""
    from app.lanes import lane_ab_legs
    from app.models import Position

    db = DB(str(tmp_path / "legs.db"))
    try:
        # trade em cada lane, mesmo sinal
        _mk(db, "m-leg", "s-leg", "main", 14.0, reason="tp2")
        _mk(db, "t-leg", "s-leg", "tp2", -6.0, reason="sl")
        db.insert_position(Position(
            id="pm1", trade_id="m-leg", leg=1, symbol="XAUUSD", direction="buy",
            lots=0.1, entry=4400.0, stop_loss=4396.0, take_profit=4402.0,
            status="closed", opened_at="2026-09-01T10:00:00+00:00",
            closed_at="2026-09-01T10:20:00+00:00", close_price=4402.0,
            pnl=14.0, exit_reason="tp2", lane_id="main"))
        db.insert_position(Position(
            id="pt1", trade_id="t-leg", leg=1, symbol="XAUUSD", direction="buy",
            lots=0.1, entry=4400.0, stop_loss=4396.0, take_profit=4402.0,
            status="closed", opened_at="2026-09-01T10:00:00+00:00",
            closed_at="2026-09-01T09:55:00+00:00", close_price=4396.0,
            pnl=-6.0, exit_reason="sl", lane_id="tp2",
            order_ticket=98765))
        # fita do journal: fill open da lane tp2 e modify de SL da main
        db.record_execution_event(
            "2026-09-01T10:00:01+00:00", "fill_open", engine="mt5",
            symbol="XAUUSD", trade_id="t-leg",
            payload={"leg": 1, "price": 4400.0, "ticket": 98765})
        db.record_execution_event(
            "2026-09-01T10:05:00+00:00", "modify_result", engine="mt5",
            symbol="XAUUSD", trade_id="m-leg",
            payload={"leg": 1, "target": 4401.0, "retcode": 0})

        r = lane_ab_legs(db, "s-leg")
        assert r["found"] is True
        assert r["trades"]["main"]["pnl"] == 14.0
        assert r["trades"]["tp2"]["pnl"] == -6.0
        assert len(r["trades"]["tp2"]["legs"]) == 1
        assert r["trades"]["tp2"]["legs"][0]["order_ticket"] == 98765
        assert r["trades"]["tp2"]["legs"][0]["exit_reason"] == "sl"
        # fita: lane atribuída pelo trade_id de cada evento
        kinds = {(j["kind"], j["lane"]) for j in r["journal"]}
        assert ("fill_open", "tp2") in kinds
        assert ("modify_result", "main") in kinds
        fill = next(j for j in r["journal"] if j["kind"] == "fill_open")
        assert "Fill perna 1 @ 4400" in fill["summary"]
        assert fill["payload"]["ticket"] == 98765
    finally:
        db.close()


def test_lane_ab_legs_sem_trade_e_sem_journal(tmp_path):
    from app.lanes import lane_ab_legs

    db = DB(str(tmp_path / "legs2.db"))
    try:
        r = lane_ab_legs(db, "s-que-nao-existe")
        assert r["found"] is False and r["trades"] == {}
    finally:
        db.close()


def _mk(db, tid, sid, lane, pnl, reason="tp1"):
    from app.models import Trade
    t = Trade(id=tid, signal_id=sid, symbol="XAUUSD", direction="buy",
              entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
              take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
              status="open", opened_at="2026-09-01T10:00:00+00:00",
              strategy_id="tp2_protect" if lane == "main" else "nova_gestao",
              lane_id=lane)
    db.insert_trade(t)
    db.close_trade(tid, "2026-09-01T10:30:00+00:00", pnl, reason=reason)


# ------------------------------------------------- risco por lane + dry-run


def test_per_lane_risk_base_config(tmp_path):
    """`risk_pct:main` / `risk_pct:tp2` na config: cada lane tem a sua BASE;
    sem chave própria, a lane cai no legado `risk_pct` (arrancam iguais)."""
    from app.execution.feed import MarketFeed
    db = DB(str(tmp_path / "risk2.db"))
    try:
        market = MarketFeed(provider=None)
        sim = SimEngine(db, market)
        sim2 = SimEngine(db, market)
        state = BotState(db=db, market=market, risk=RiskEngine(),
                         sim=sim, notifier=Notifier(db, WSManager()),
                         ws=WSManager(), second_sim=sim2)
        state.config["balance"] = 1000.0
        state.config["lane2_balance"] = 10000.0
        # sem chaves próprias -> ambas usam o legado risk_pct
        state.config["risk_pct"] = 1.0
        assert state._base_risk_for_lane("main") == 1.0
        assert state._base_risk_for_lane("tp2") == 1.0
        # base própria da 2ª conta
        state.config["risk_pct:tp2"] = 2.0
        assert state._base_risk_for_lane("main") == 1.0
        assert state._base_risk_for_lane("tp2") == 2.0
        # risco efetivo sem drawdown = base da lane
        assert round(state._effective_risk_pct_for_lane("tp2"), 2) == 2.0
        assert round(state._effective_risk_pct_for_lane("main"), 2) == 1.0
        # risco efetivo da principal (legado) não muda
        assert round(state._effective_risk_pct(), 2) == 1.0
        # snapshot expõe a base por lane + o preview dimensiona com ela
        lanes = {l["lane_id"]: l for l in state._lanes_snapshot()}
        assert lanes["main"]["risk_base_pct"] == 1.0
        assert lanes["tp2"]["risk_base_pct"] == 2.0
        snap = state.snapshot()
        assert snap["config"]["risk_pct_by_lane"]["main"] == 1.0
        assert snap["config"]["risk_pct_by_lane"]["tp2"] == 2.0
        assert snap["config"]["effective_risk_by_lane"]["tp2"] == 2.0
    finally:
        db.close()


class _FakeDryProvider:
    """Provider MT5 falso para o resumo dry (conta completa como o broker)."""
    connected = True

    def __init__(self, balance=1000.0):
        self._balance = balance

    def account(self):
        return {"currency": "EUR", "balance": self._balance,
                "equity": self._balance}


def test_lanes_snapshot_includes_dry_run_summary(tmp_path):
    """A entrada de lane leva o resumo simulado (dry-run local) lado a lado
    com o saldo broker — o card mostra ambos quando a lane está em dry."""
    db = DB(str(tmp_path / "dry.db"))
    try:
        sim2 = SimEngine(db, MarketSimulator())
        state = _state(db, second_sim=sim2)
        # engines em MT5 dry-run (como MT5_2_DRY_RUN=1 na lane 2 em testes)
        sim2.provider = _FakeDryProvider(balance=10000.0)
        sim2.dry_run = True
        state.sim.provider = _FakeDryProvider(balance=100000.0)
        state.sim.dry_run = True
        from app.models import Trade
        db.insert_trade(Trade(
            id="dry-t1", signal_id="dry-s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
            take_profit_2=4404.0, risk_pct=1.0, lots_1=0.1, lots_2=0.1,
            status="closed", opened_at="2026-09-01T10:00:00+00:00",
            closed_at="2026-09-01T10:30:00+00:00", pnl=25.0,
            exit_reason="tp2", execution_mode="mt5-dry", lane_id="tp2"))
        db.insert_trade(Trade(
            id="dry-m1", signal_id="dry-s2", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
            take_profit_2=4404.0, risk_pct=1.0, lots_1=0.1, lots_2=0.1,
            status="closed", opened_at="2026-09-01T10:00:00+00:00",
            closed_at="2026-09-01T10:30:00+00:00", pnl=-5.0,
            exit_reason="sl", execution_mode="mt5-dry", lane_id="main"))
        lanes = {l["lane_id"]: l for l in state._lanes_snapshot()}
        # tp2 com engine dry-run + conta sintética -> resumo ativo com P&L 25
        assert lanes["tp2"]["dry_run"]["enabled"] is True
        assert lanes["tp2"]["dry_run"]["realized_pnl"] == 25.0
        assert lanes["tp2"]["dry_run"]["equity"] is not None
        assert lanes["main"]["dry_run"]["realized_pnl"] == -5.0
    finally:
        db.close()


def test_dry_run_realized_pnl_sql_por_lane(tmp_path):
    """O SUM SQL do dry-run separa as lanes (e as trades sim normais não
    contam para o resumo — só execution_mode='mt5-dry')."""
    db = DB(str(tmp_path / "dry2.db"))
    try:
        from app.models import Trade
        base = dict(symbol="XAUUSD", direction="buy", entry=4400.0,
                    stop_loss=4392.0, take_profit_1=4402.0, take_profit_2=4404.0,
                    risk_pct=1.0, lots_1=0.1, lots_2=0.1, status="closed",
                    opened_at="2026-09-01T10:00:00+00:00",
                    closed_at="2026-09-01T10:30:00+00:00")
        for tid, lane, pnl, mode in (
                ("a", "tp2", 10.0, "mt5-dry"), ("b", "tp2", -2.5, "mt5-dry"),
                ("c", "main", 7.0, "mt5-dry"), ("d", "main", 3.0, "sim"),
                ("e", "", 1.0, "mt5-dry")):
            db.insert_trade(Trade(id=tid, signal_id="s-" + tid, pnl=pnl,
                                  execution_mode=mode, lane_id=lane or None,
                                  **base))
        assert db.dry_run_realized_pnl("tp2") == 7.5
        assert db.dry_run_realized_pnl("main") == 8.0  # 7 + 1 (lane NULL/legado)
    finally:
        db.close()


def test_lane_config_keys_survive_restart(tmp_path):
    """`risk_pct:tp2`/`lots_override:tp2`/`peak_balance:tp2` são persistidos
    via `_persist_config` e têm de voltar da tabela config num processo novo
    — sem isso, cada restart da lane 2 cai no `risk_pct` legado e no
    dimensionamento por risco (lote diferente do definido)."""
    db = DB(str(tmp_path / "restart.db"))
    try:
        state = _state(db, second_sim=SimEngine(db, MarketSimulator()))
        state.config["risk_pct:tp2"] = 0.6
        state.config["lots_override:tp2"] = 0.04
        state.config["peak_balance:tp2"] = 10000.76
        state._persist_config(["risk_pct:tp2", "lots_override:tp2",
                               "peak_balance:tp2"])
        # novo processo: BotState recarrega a config da BD (simula restart)
        state2 = _state(db, second_sim=SimEngine(db, MarketSimulator()))
        assert state2.config["risk_pct:tp2"] == 0.6
        assert state2.config["lots_override:tp2"] == 0.04
        assert state2.config["peak_balance:tp2"] == 10000.76
        assert state2._fixed_lots_for_lane("tp2") == 0.04
        assert state2._base_risk_for_lane("tp2") == 0.6
    finally:
        db.close()


def test_last_snapshot_serves_cache_do_tick_e_snapshot_fresco(tmp_path):
    """`last_snapshot()` (endpoints read-only) devolve o snapshot que o tick
    construiu; `snapshot()` continua a construir fresco. É isto que tira as
    leituras ao broker MT5/worker do caminho de request (o tick corre ~1×/s
    e constrói UMA vez; os GET /api/status servem essa cópia em vez de
    reconstruírem por request e bloquearem o event loop)."""
    db = DB(str(tmp_path / "cache.db"))
    try:
        from app.execution.feed import MarketFeed
        market = MarketFeed(provider=None)
        state = BotState(db=db, market=market, risk=RiskEngine(),
                         sim=SimEngine(db, market),
                         notifier=Notifier(db, WSManager()), ws=WSManager())
        # sem tick corrido ainda → last_snapshot computa fresco (fallback)
        assert state._last_snapshot is None
        fresh = state.snapshot()
        assert state.last_snapshot()["config"]["mode"] == "auto"
        # simula o fim de um tick: constrói e guarda UMA vez
        built = state._build_snapshot()
        state._last_snapshot = built
        assert state.last_snapshot() is built  # servido da cache, sem rebuild
        # snapshot() continua fresco (nunca serve a cache)
        assert state.snapshot() is not built
        # a cache tem de refletir mutações quando o tick voltar a correr
        state.config["kill_switch"] = True
        state._last_snapshot = state._build_snapshot()
        assert state.last_snapshot()["config"]["kill_switch"] is True
    finally:
        db.close()


def test_ttl_cache_evita_recalcular_blocos_caros(tmp_path):
    """`_ttl_get` serve o mesmo valor dentro do TTL e recomputa depois de
    expirar; `_invalidate_snapshot_cache` limpa a cache inteira (uma mutação
    tem de aparecer no próximo snapshot). É o que tira as ~20 agregações DB
    por segundo (paper/shadow/fase3/window) do caminho do tick."""
    db = DB(str(tmp_path / "ttl.db"))
    try:
        state = _state(db)
        calls = []

        def compute():
            calls.append(1)
            return {"n": len(calls)}

        assert state._ttl_get("k", 30.0, compute) == {"n": 1}
        assert state._ttl_get("k", 30.0, compute) == {"n": 1}
        assert len(calls) == 1  # dentro do TTL: cache hit
        # TTL expirado → recomputa
        cached_at, val = state._ttl_cache["k"]
        state._ttl_cache["k"] = (cached_at - 60.0, val)
        assert state._ttl_get("k", 30.0, compute) == {"n": 2}
        assert len(calls) == 2
        # mutação → invalidação limpa a cache (o próximo read é fresco)
        state._invalidate_snapshot_cache()
        assert state._ttl_get("k", 30.0, compute) == {"n": 3}
        assert len(calls) == 3
    finally:
        db.close()


def test_ttl_cache_popula_blocos_do_snapshot(tmp_path):
    """O `_build_snapshot` usa a cache TTL para os blocos informativos
    (fase3, janela, corridas paper/shadow, risk_preview por lane) — um
    segundo snapshot no mesmo tick não volta a pagar essas queries."""
    db = DB(str(tmp_path / "ttl2.db"))
    try:
        from app.execution.feed import MarketFeed
        market = MarketFeed(provider=None)
        state = BotState(db=db, market=market, risk=RiskEngine(),
                         sim=SimEngine(db, market),
                         notifier=Notifier(db, WSManager()), ws=WSManager())
        state.config["balance"] = 100000.0
        state.config["mode"] = "auto"
        state._build_snapshot()
        for key in ("fase3_snapshot", "fase3_eta", "fase3_verdict",
                    "fase3_verdicts", "fase3_target_log", "window_status",
                    "paper_status", "shadow_paper_status", "risk_preview:main"):
            assert key in state._ttl_cache, f"falta a cache {key}"
    finally:
        db.close()


# ------------------------------------------- kill diário por saldo da lane


class _FakeAcctProvider:
    """Provider mínimo que devolve uma conta MT5 com saldo próprio."""

    def __init__(self, balance):
        self._balance = balance
        self.connected = True

    def account(self):
        return {"login": 1, "balance": self._balance, "equity": self._balance,
                "currency": "EUR"}


def _insert_closed_trade(db, tid, lane_id, pnl, day=None):
    from datetime import datetime
    from app.models import Trade
    # Dia LOCAL: a VPS tem relógio +1h vs UTC e o limite diário casa
    # `closed_at` com `date.today()` local (ver AGENTS.md) — perto da
    # meia-noite, now(utc) ficava no dia anterior e a perda não contava.
    closed_at = (day or datetime.now().isoformat())
    db.insert_trade(Trade(
        id=tid, signal_id=f"sig-{tid}", symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4394.0, take_profit_1=4402.0,
        take_profit_2=4404.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed", opened_at=closed_at, closed_at=closed_at,
        pnl=pnl, lane_id=lane_id,
    ))


def test_daily_loss_killed_per_lane_balance(tmp_path):
    """O kill diário avalia cada lane contra o saldo da PRÓPRIA conta.

    Incidente 07/09: -52,36 € da tp2 (saldo 10.000 € = -0,52%) somados a
    -1,11 € da principal (saldo 1.033 €) davam -5,17% — kill global com as
    duas contas individualmente bem dentro do limite. Agora: cada lane
    respira pelo seu saldo; a perda da tp2 NÃO dispara o kill da principal."""
    db = DB(str(tmp_path / "k.db"))
    try:
        state = _state(db, second_sim=SimEngine(db, MarketSimulator()))
        # contas MT5 por lane com saldos muito diferentes
        state.sim.provider = _FakeAcctProvider(1000.0)     # main: 1.000 €
        state.sim2.provider = _FakeAcctProvider(10_000.0)  # tp2: 10.000 €
        state.config["daily_loss_limit_pct"] = 4.5

        # a tp2 perde 52 € (-0,52% de 10k — dentro do limite) e a main
        # perde 1,11 € (-0,11% de 1k — dentro): somado dava -53,47 € contra
        # 1.000 € = -5,35% → kill ERRADO que já não acontece
        _insert_closed_trade(db, "t1", "tp2", -52.36)
        _insert_closed_trade(db, "t2", "main", -1.11)
        assert state._daily_loss_killed() is False, \
            "perda da tp2 NÃO conta contra o saldo pequeno da main"

        # a PRÓPRIA lane a passar o limite dispara o kill
        _insert_closed_trade(db, "t3", "main", -45.0)
        # main hoje: -46,11 € de 1.000 € = -4,61% > 4,5%
        assert state._daily_loss_killed() is True

        # e a tp2 sozinha no mesmo montante relativo também dispara
        db2 = DB(str(tmp_path / "k2.db"))
        try:
            state2 = _state(db2, second_sim=SimEngine(db2, MarketSimulator()))
            state2.sim.provider = _FakeAcctProvider(1000.0)
            state2.sim2.provider = _FakeAcctProvider(10_000.0)
            state2.config["daily_loss_limit_pct"] = 4.5
            _insert_closed_trade(db2, "t4", "tp2", -460.0)
            # tp2: -460 € de 10k = -4,6% > 4,5% (a main está limpa)
            assert state2._daily_loss_killed() is True
        finally:
            db2.close()
    finally:
        db.close()


def test_daily_loss_killed_without_mt5_uses_legacy_total(tmp_path):
    """Sim puro (sem conta MT5 em nenhuma lane): mantém o comportamento
    legado — P&L total do dia contra o saldo configurado."""
    db = DB(str(tmp_path / "k3.db"))
    try:
        state = _state(db, second_sim=SimEngine(db, MarketSimulator()))
        state.config["daily_loss_limit_pct"] = 4.5
        _insert_closed_trade(db, "t5", "tp2", -60.0)
        _insert_closed_trade(db, "t6", "main", -1.11)
        # total -61,11 € contra saldo configurado 100.000 € → sem kill
        assert state._daily_loss_killed() is False
        state.config["balance"] = 1000.0
        # total -61,11 € de 1.000 € = -6,1% > 4,5% → kill (legado)
        assert state._daily_loss_killed() is True
    finally:
        db.close()


def test_adaptive_be_buffer_from_observations(tmp_path):
    """BE+ adaptativo: com amostras de spread, o buffer segue a média 7d
    (×1.1, clamp [10,60]); sem amostras, usa o estático da estratégia."""
    from datetime import datetime, timezone
    db = DB(str(tmp_path / "adapt.db"))
    try:
        state = _state(db)
        strat = {"be_buffer_pips": 40.0}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # média 30 pips × 1.1 = 33
        db.insert_spread_observation(now, "XAUUSD", 4400.0, 4400.30,
                                     0.30, 30.0)
        assert state._adaptive_be_buffer("XAUUSD", strat) == 33.0
        # média 100 pips → clamp no máximo 60
        db.insert_spread_observation(now, "XAUUSD", 4400.0, 4401.7,
                                     1.70, 170.0)
        state._be_buffer_cache.clear()
        assert state._adaptive_be_buffer("XAUUSD", strat) == 60.0
        # média 2 pips → clamp no mínimo 10
        state2_db = db  # mesma BD: observa EURUSD separado
        db.insert_spread_observation(now, "EURUSD", 1.0850, 1.08502,
                                     0.00002, 0.2)
        state._be_buffer_cache.clear()
        assert state._adaptive_be_buffer("EURUSD", strat) == 10.0
        # sem amostras (símbolo desconhecido) → estático
        assert state._adaptive_be_buffer("GBPUSD", strat) == 40.0
        # switch off → estático mesmo com amostras
        state.config["be_buffer_adaptive"] = False
        state._be_buffer_cache.clear()
        assert state._adaptive_be_buffer("XAUUSD", strat) == 40.0
    finally:
        db.close()
