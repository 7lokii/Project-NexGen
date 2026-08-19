"""MT5Engine tests — dry-run mode only (nothing touches the real broker).

The fake provider returns per-symbol metadata from the fallback table and
scriptable ticks, so the engine's dry-run path (record in DB, simulate fills
at the tick) is exercised without a terminal.
"""

import os
import sys
import tempfile
from types import SimpleNamespace

from app.db import DB
from app.execution.mt5 import (FALLBACK_SYMBOLS, MT5Engine, MT5Provider,
                               _compute_lots_sym)
from app.execution.market import MarketSimulator
from app.models import Signal


class FakeProvider(MT5Provider):
    """Provider without a terminal: connected, scriptable ticks."""

    def __init__(self, ticks=None):
        super().__init__(symbols={s: dict(m) for s, m in FALLBACK_SYMBOLS.items()})
        self._fake_ticks = ticks or {}

    @property
    def connected(self):
        return True

    def tick(self, app_symbol):
        return self._fake_ticks.get(app_symbol)

    def meta(self, app_symbol):
        if app_symbol not in self._symbols:
            return None
        return dict(self._symbols[app_symbol])


class _FakeMT5:
    """Fake `mt5` module para exercitar o caminho real (dry_run=False)
    sem terminal: retcodes scriptáveis via `retcode`."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, retcode=10009, ask=4385.0, bid=4384.9):
        self.retcode = retcode
        self.ask = ask
        self.bid = bid
        self.orders = []

    def symbol_select(self, *a, **k):
        return True

    def symbol_info_tick(self, sym):
        return _Tick(self.ask, self.bid)

    def order_send(self, request):
        self.orders.append(request)
        return _Result(self.retcode, "AutoTrading disabled" if self.retcode == 10027 else "Done")

    def positions_get(self, **k):
        return []


class _Tick:
    def __init__(self, ask, bid):
        self.ask = ask
        self.bid = bid


class _QuoteMT5:
    """Minimal connected terminal exposing a 5-digit FX quote."""

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def symbol_info(self, _symbol):
        return SimpleNamespace(
            trade_contract_size=100_000.0,
            digits=5,
            volume_min=0.01,
            volume_max=500.0,
            volume_step=0.01,
            trade_tick_size=0.00001,
        )

    def symbol_info_tick(self, _symbol):
        return _Tick(1.10020, 1.10010)


class _Result:
    def __init__(self, retcode, comment):
        self.retcode = retcode
        self.comment = comment


class _NoTickFirstBroker(_FakeMT5):
    """`symbol_info_tick` devolve None na 1ª perna (símbolo ainda sem
    preço) e o tick normal nas seguintes — para testar o NO_TICK."""

    def __init__(self):
        super().__init__()
        self._calls = 0

    def symbol_info_tick(self, sym):
        self._calls += 1
        if self._calls == 1:
            return None
        return _Tick(self.ask, self.bid)


class _PartialRetcodeBroker(_FakeMT5):
    """Devolve retcodes diferentes por ordem enviada (lista) — para testar
    retcodes parciais (uma perna aceite, outra recusada)."""

    def __init__(self, retcodes):
        super().__init__(retcode=retcodes[0])
        self._retcodes = list(retcodes)
        self._i = 0

    def order_send(self, request):
        rc = self._retcodes[min(self._i, len(self._retcodes) - 1)]
        self._i += 1
        self.orders.append(request)
        return _Result(rc, "AutoTrading disabled" if rc == 10027 else "Done")


def make_signal(**over):
    base = dict(
        id="s1", source="test", raw_text="", symbol="XAUUSD", direction="buy",
        entry=4385.0, stop_loss=4381.0, take_profit_1=4389.0,
        take_profit_2=4395.0, status="pending", received_at="",
    )
    base.update(over)
    return Signal(**base)


def setup(price=4385.0):
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    provider = FakeProvider(ticks={"XAUUSD": price})
    market = MarketSimulator(seed=42)
    market.prices["XAUUSD"] = price
    eng = MT5Engine(db, provider, market, dry_run=True)
    return db, provider, market, eng, tmp


def cleanup(db, tmp):
    db.close()
    if os.path.exists(tmp):
        os.remove(tmp)


# --------------------------------------------------------- account policy


class _InitAccountMT5:
    """Minimal MetaTrader5 module for the hedging gate."""

    def __init__(self, margin_mode):
        self.margin_mode = margin_mode
        self.shutdown_called = False

    def initialize(self, **_kwargs):
        return True

    def account_info(self):
        return SimpleNamespace(
            login=5054644473, server="MetaQuotes-Demo", currency="USD",
            balance=1000.0, equity=1000.0, leverage=100,
            trade_mode=0, margin_mode=self.margin_mode,
        )

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "")


def test_real_quote_reports_forex_spread_in_pips():
    """A 5-digit FX point is not a pip: 0.00010 must report as 1 pip."""
    provider = MT5Provider(symbols=FALLBACK_SYMBOLS)
    provider._mt5 = _QuoteMT5()
    quote = provider.quote("EURUSD")
    assert quote is not None
    assert round(quote["spread"], 8) == 0.0001
    assert round(quote["spread_pips"], 6) == 1.0


def test_initialize_blocks_non_hedged_real_account(monkeypatch):
    fake = _InitAccountMT5(margin_mode=0)  # ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    provider = MT5Provider(symbols=FALLBACK_SYMBOLS, require_hedged=True)
    assert provider.initialize() is False
    assert provider.connected is False
    assert provider.account_margin_mode == 0
    assert provider.account_hedged is False
    assert "não é hedged" in provider.execution_block_reason
    assert fake.shutdown_called is True
    assert provider.account()["hedged"] is False


def test_initialize_accepts_hedged_account(monkeypatch):
    fake = _InitAccountMT5(margin_mode=2)  # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    provider = MT5Provider(symbols=FALLBACK_SYMBOLS, require_hedged=True)
    assert provider.initialize() is True
    assert provider.connected is True
    assert provider.account()["margin_mode"] == 2
    assert provider.account()["hedged"] is True
    provider.shutdown()


# ------------------------------------------------------------- sizing


def test_compute_lots_gold_contract():
    """XAUUSD usa o contract 100 (não 100000) — o lote do risco real."""
    meta = dict(FALLBACK_SYMBOLS["XAUUSD"])
    lots = _compute_lots_sym(equity=10_000, risk_pct=1.0, entry=4400.0,
                             stop_loss=4392.0, meta=meta)
    assert 0.12 <= lots <= 0.13, f"gold contract 100: lots={lots}"


def test_compute_lots_forex_contract():
    """Forex usa contract 100000 — o lote do risco real."""
    meta = dict(FALLBACK_SYMBOLS["EURUSD"])
    lots = _compute_lots_sym(equity=10_000, risk_pct=1.0, entry=1.15,
                             stop_loss=1.1470, meta=meta)
    assert 0.33 <= lots <= 0.34, f"forex contract 100000: lots={lots}"


def test_open_trade_records_two_legs():
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open"
        pos = db.positions_for_trade(trade.id)
        assert len(pos) == 2
        assert {p.leg for p in pos} == {1, 2}
        assert all(p.stop_loss == 4381.0 for p in pos)
    finally:
        cleanup(db, tmp)


def test_tp1_then_breakeven_then_tp2():
    """TP1 fecha (lucro) → perna 2 vai a BE → TP2 fecha a perna 2."""
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        # TP1 (4389) bate → perna 1 fecha com lucro
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update()
        p1 = [p for p in db.positions_for_trade(trade.id) if p.leg == 1][0]
        assert p1.status == "closed" and p1.pnl > 0
        p2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert p2.status == "open"
        assert p2.stop_loss == trade.entry  # BE após TP1
        # TP2 (4395) → perna 2 fecha com lucro
        provider._fake_ticks["XAUUSD"] = 4395.0
        eng.update()
        p2 = [p for p in db.positions_for_trade(trade.id) if p.leg == 2][0]
        assert p2.status == "closed" and p2.pnl > 0
        t = db.get_trade(trade.id)
        assert t.status == "closed"
    finally:
        cleanup(db, tmp)


def test_tp1_then_tp2_full_win():
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        provider._fake_ticks["XAUUSD"] = 4395.0
        eng.update()
        t = db.get_trade(trade.id)
        assert t.status == "closed"
        assert t.pnl > 0
        pos = db.positions_for_trade(trade.id)
        assert all(p.status == "closed" and p.pnl > 0 for p in pos)
    finally:
        cleanup(db, tmp)


def test_stop_loss_closes_both_legs():
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        provider._fake_ticks["XAUUSD"] = 4381.0
        eng.update()
        t = db.get_trade(trade.id)
        assert t.status == "closed"
        assert t.pnl < 0
        pos = db.positions_for_trade(trade.id)
        # dry-run: as pernas fecham no preço do SL (sem exit_reason — o
        # motivo é inferido pelo pnl < 0 no fecho)
        assert all(p.status == "closed" and p.close_price == 4381.0 for p in pos)
    finally:
        cleanup(db, tmp)


def test_sell_direction_hits_tp():
    db, provider, market, eng, tmp = setup(price=4385.0)
    try:
        sig = make_signal(direction="sell", entry=4385.0, stop_loss=4390.0,
                          take_profit_1=4382.0, take_profit_2=4378.0)
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        provider._fake_ticks["XAUUSD"] = 4378.0
        eng.update()
        t = db.get_trade(trade.id)
        assert t.status == "closed" and t.pnl > 0
    finally:
        cleanup(db, tmp)


def test_close_trade_at_market_dry_run():
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        provider._fake_ticks["XAUUSD"] = 4390.0
        eng.close_trade_at_market(trade.id, reason="close_all")
        t = db.get_trade(trade.id)
        assert t.status == "closed"
        assert all(p.exit_reason == "close_all" for p in db.positions_for_trade(trade.id))
    finally:
        cleanup(db, tmp)


def test_open_trade_three_legs_dry_run():
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal(take_profit_3=4402.0)
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert len(db.positions_for_trade(trade.id)) == 3
    finally:
        cleanup(db, tmp)


def test_open_trade_real_mode_captures_retcode_10027():
    """dry_run=False com o broker a responder 10027 (AutoTrading off): a
    trade é marcada `auto_trading_off=True` e o error explica o retcode."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _FakeMT5(retcode=10027)
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "closed"  # nada chegou ao broker
        assert trade.auto_trading_off is True
        assert "10027" in (trade.error or "")
    finally:
        db.close()


def test_open_trade_real_mode_retcode_0_accepted():
    """O servidor MetaQuotes-Demo responde `retcode 0 / Done` — o motor
    aceita 0 como fill real (o _order_ok aceita 10009 e 0)."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _FakeMT5(retcode=0)
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open", "retcode 0 = fill aceite (Demo)"
        assert trade.auto_trading_off is not True
        assert len(db.positions_for_trade(trade.id)) == 2
    finally:
        db.close()


def test_open_trade_real_mode_no_tick_fails_leg_only():
    """`symbol_info_tick` sem preço (símbolo fora do Market Watch): a perna
    falha com NO_TICK mas NÃO crasha nem repete a trade — as restantes
    abrem normalmente e a trade fica aberta (não phantom)."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _NoTickFirstBroker()
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        # a 1ª perna ficou sem tick (NO_TICK → falha só essa perna, fica
        # `open` na BD como perna sem fill); a 2ª abriu no broker
        pos = db.positions_for_trade(trade.id)
        assert len(pos) == 2
        legs_by_leg = {p.leg: p for p in pos}
        assert legs_by_leg[1].status == "open"  # sem fill do broker
        assert legs_by_leg[2].status == "open"
        assert trade.status == "open"
    finally:
        db.close()


def test_open_trade_real_mode_partial_retcodes():
    """Retcodes parciais (uma perna aceite 10009, outra recusada 10027): a
    trade fica ABERTA (algo chegou ao broker) e o flag `auto_trading_off`
    NÃO é marcado — só quando NENHUMA perna chega ao broker (sent==0) a
    trade é fechada com o flag."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _PartialRetcodeBroker([10009, 10027])
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open", "1 perna aceite → trade aberta"
        assert not trade.auto_trading_off, "flag só com sent==0 (nenhuma perna)"
    finally:
        db.close()


def test_open_trade_real_mode_autotrading_on_no_flag():
    """AutoTrading ON (retcodes 10009 em todas as pernas): o flag
    `auto_trading_off` NÃO é marcado — só o 10027 o ativa."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _FakeMT5(retcode=10009)
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open"
        assert trade.auto_trading_off is False or trade.auto_trading_off is None
        assert not (trade.retcodes or [])
    finally:
        db.close()


def test_open_trade_real_mode_other_retcode_no_auto_trading_flag():
    """Retcode 10015 (ordem inválida) NÃO marca `auto_trading_off` — só o
    10027 (AutoTrading OFF) o faz; o error explica o retcode."""
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _FakeMT5(retcode=10015)
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "closed"
        assert trade.auto_trading_off is False
        assert "10015" in (trade.error or "")
    finally:
        db.close()


def test_exit_at_tp2_closes_all_at_tp2_level():
    """`exit_at_tp=2` (estratégias do registo, ex.: tp2_protect): quando o
    TP2 bate, as pernas ainda abertas fecham nesse preço — a perna 1 fecha
    no TP1 (regra normal) e as restantes (2/3) fecham no nível do TP2."""
    db, provider, market, eng, tmp = setup()
    try:
        sig = make_signal(take_profit_3=4402.0)
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        provider._fake_ticks["XAUUSD"] = 4395.0  # TP1 e TP2 batem
        eng.update(exit_at_tp=2)
        t = db.get_trade(trade.id)
        assert t.status == "closed"
        closes = {p.leg: p for p in db.positions_for_trade(trade.id)}
        assert closes[1].close_price == 4389.0  # TP1 (regra normal)
        assert closes[2].close_price == 4395.0  # TP2 (exit_at_tp)
        assert closes[3].close_price == 4395.0  # TP2 (exit_at_tp)
    finally:
        cleanup(db, tmp)


def test_auto_stair_raises_sl_to_tp1_level():
    """Real mode (broker fake): TP1 fecha pelo broker → escada sobe o SL das
    pernas 2/3 para o NÍVEL do TP1 (o `leg` é o comentário `trade.id:legN`)."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(take_profit_3=4402.0)
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 0.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)  # TP1 bate (server-side)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0, f"escada: perna {p.leg} SL = TP1"
    finally:
        db.close()
        os.remove(tmp)


def test_auto_confirm_filter_closes_far_legs_on_sl():
    """Real mode: filtro de confirmação — a perna intermédia (2) leva SL
    (o movimento morreu) → as pernas > k (3) fecham ao mercado com reason
    `confirm`; a perna com SL fica `sl` e a perna 1 intacta mantém a trade
    aberta (a confirmação não fecha pernas que ainda não confirmaram nada)."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(take_profit_3=4402.0)  # entry 4385, SL 4381, TP1 4389
        auto = {"stair_levels": 0, "stair_from": 0, "confirm_filter": True,
                "trail_factor": 0.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        # perna 2 leva SL: fecha a 4380 (abaixo do SL 4381) → pnl < 0
        broker.simulate_close(tickets[2], 4380.0)
        provider._fake_ticks["XAUUSD"] = 4380.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        by_leg = {p.leg: p for p in db.positions_for_trade(trade.id)}
        assert by_leg[2].status == "closed" and by_leg[2].exit_reason == "sl"
        assert by_leg[3].status == "closed", "perna 3 fechou pelo filtro"
        assert by_leg[3].exit_reason == "confirm"
        assert by_leg[1].status == "open", "perna 1 intacta"
        assert db.get_trade(trade.id).status == "open"
        assert broker._positions == [] or len(broker._positions) == 1
    finally:
        db.close()
        os.remove(tmp)


def test_auto_stair_raises_sl_to_tp1_level_sell():
    """Real mode: escada em SELL — TP1 fecha com lucro pelo broker → o SL
    das pernas 2/3 DESCE para o nível do TP1 (4381, abaixo da entrada 4385
    — para sell o SL protetor move-se para baixo, espelho do caso buy)."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(direction="sell", entry=4385.0, stop_loss=4389.0,
                          take_profit_1=4381.0, take_profit_2=4375.0,
                          take_profit_3=4368.0)
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 0.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4381.0)  # TP1 de sell (abaixo da entrada)
        provider._fake_ticks["XAUUSD"] = 4381.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        leg1 = [p for p in db.positions_for_trade(trade.id) if p.leg == 1][0]
        assert leg1.exit_reason == "tp" and leg1.pnl > 0
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4381.0, f"escada sell: perna {p.leg} SL = TP1"
                bp = next(x for x in broker._positions if x.ticket == tickets[p.leg])
                assert bp.sl == 4381.0, "SL também no broker"
    finally:
        db.close()
        os.remove(tmp)


def test_auto_move_sl_throttled_60s():
    """Real mode: o throttle (≥1 pip e ≤1×/min por perna) bloqueia uma
    segunda subida de SL imediata — a escada só move uma vez."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(take_profit_3=4402.0)
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 0.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0
        # novo tick imediato (mesmo minuto): throttle bloqueia outro modify
        provider._fake_ticks["XAUUSD"] = 4391.0
        eng.update(now="2026-08-18T10:00:30+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0, "sem novo modify no throttle"
    finally:
        db.close()
        os.remove(tmp)


def test_auto_trailing_follows_peak_without_lookahead():
    """Real mode: depois do TP1, o trailing segue o pico dos ticks
    ANTERIORES (sem look-ahead) — SL = pico − trail_factor × dist(entrada→TP2)."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(take_profit_3=4402.0)  # entry 4385, TP2 4395
        auto = {"stair_levels": 0, "stair_from": 0, "confirm_filter": False,
                "trail_factor": 2.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)  # TP1 → tp1_ts
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        # Primeiro observa o pico 4410. O trailing só pode usar esse pico no
        # tick seguinte (sem look-ahead); a própria observação não pode criar
        # o SL que ela depois atravessaria.
        state = eng._auto_state.setdefault(trade.id, {})
        last_mod = state.setdefault("last_mod", {})
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                last_mod[p.id] = eng._auto_clock.monotonic() - 61
        provider._fake_ticks["XAUUSD"] = 4410.0
        broker.ask = 4410.05
        broker.bid = 4409.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:01:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4385.0, "o pico atual ainda não pode levantar o SL"
                last_mod[p.id] = eng._auto_clock.monotonic() - 61

        # No tick seguinte, o pico anterior 4410 já é conhecido:
        # SL = 4410 − 2×(4395−4385) = 4390.
        provider._fake_ticks["XAUUSD"] = 4411.0
        broker.ask = 4411.05
        broker.bid = 4410.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:02:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4390.0, f"trail: perna {p.leg} SL = pico − trail"
    finally:
        db.close()
        os.remove(tmp)


def test_auto_valid_sl_respects_stops_level():
    db = DB(tempfile.mktemp(suffix=".db"))
    try:
        provider = FakeProvider(ticks={"XAUUSD": 4385.0})
        provider._mt5 = _FakeMT5()
        market = MarketSimulator(seed=42)
        market.prices["XAUUSD"] = 4385.0
        eng = MT5Engine(db, provider, market, dry_run=False)
        sig = make_signal()
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        pos = db.positions_for_trade(trade.id)[0]
        # target acima da distância mínima do broker → clamp ao nível válido
        valid = eng._valid_sl(trade, 4395.0)
        assert valid <= 4395.0
    finally:
        db.close()


class _Rate:
    def __init__(self, high, low, close, time=0):
        self.time = time
        self.open = low
        self.high = high
        self.low = low
        self.close = close
        self.tick_volume = 100


class _Pos:
    def __init__(self, ticket, symbol, type_, volume, price_open, sl, tp,
                 comment, price_current):
        self.ticket = ticket
        self.symbol = symbol
        self.type = type_
        self.volume = volume
        self.price_open = price_open
        self.sl = sl
        self.tp = tp
        self.comment = comment
        self.profit = 0.0
        self.price_current = price_current


class _Deal:
    def __init__(self, ticket, price, profit, entry):
        self.ticket = ticket
        self.price = price
        self.profit = profit
        self.entry = entry


class _SymInfo:
    def __init__(self, stops_level, freeze_level):
        self.stops_level = stops_level
        self.freeze_level = freeze_level


class _FakeBroker:
    """Broker fake ESTADO (dry_run=False): abre/fecha posições, regista
    deals de entrada/saída, aceita modifies SL/TP com validação da distância
    mínima (stops/freeze) — retcodes 10009 (Done) / 10016 (invalid stops).
    `copy_rates_from_pos` devolve velas para o trailing por ATR."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_STOPS = 10016
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self, ask=4385.0, bid=4384.9, info_stops_level=0.0,
                 enforce_stops_level=0.0):
        self.ask = ask
        self.bid = bid
        self.info_stops_level = info_stops_level
        self.enforce_stops_level = enforce_stops_level
        self._positions = []
        self._deals = {}
        self._next_ticket = 1001
        self._next_deal = 5001
        self.orders = []
        self.rates = []
        self.rate_calls = []

    # ---- API do módulo mt5 ----
    def symbol_select(self, *a, **k):
        return True

    def symbol_info_tick(self, sym):
        return _Tick(self.ask, self.bid)

    def symbol_info(self, sym):
        return _SymInfo(self.info_stops_level, 0.0)

    def positions_get(self, symbol=None, **k):
        if symbol is None:
            return [p for p in self._positions]
        return [p for p in self._positions if p.symbol == symbol]

    def history_deals_get(self, position=None, **k):
        return list(self._deals.get(position, []))

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.rate_calls.append((symbol, timeframe, start, count))
        return list(self.rates[:count])

    def order_send(self, request):
        self.orders.append(request)
        action = request.get("action")
        if action == self.TRADE_ACTION_DEAL:
            if not request.get("position"):
                return self._open(request)
            return self._close(request)
        if action == self.TRADE_ACTION_SLTP:
            return self._modify(request)
        return _Result(self.TRADE_RETCODE_DONE, "Done")

    # ---- internals ----
    def _open(self, req):
        ticket = self._next_ticket
        self._next_ticket += 1
        pos = _Pos(ticket, req["symbol"], req["type"], req["volume"],
                   req["price"], req.get("sl", 0.0), req.get("tp", 0.0),
                   req.get("comment", ""), self.ask)
        self._positions.append(pos)
        self._deals[ticket] = [_Deal(ticket, pos.price_open, 0.0,
                                     self.DEAL_ENTRY_IN)]
        return _Result(self.TRADE_RETCODE_DONE, "Done")

    def _close(self, req):
        ticket = req["position"]
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return _Result(self.TRADE_RETCODE_INVALID_STOPS, "Invalid position")
        price = float(req.get("price", self.ask))
        sign = 1.0 if pos.type == self.POSITION_TYPE_BUY else -1.0
        contract = float(FALLBACK_SYMBOLS.get(pos.symbol, {}).get("contract", 100.0))
        profit = round(sign * (price - pos.price_open) * pos.volume * contract, 2)
        self._positions.remove(pos)
        self._deals[ticket].append(_Deal(ticket, price, profit, self.DEAL_ENTRY_OUT))
        return _Result(self.TRADE_RETCODE_DONE, "Done", price)

    def _modify(self, req):
        ticket = req["position"]
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return _Result(self.TRADE_RETCODE_INVALID_STOPS, "Invalid position")
        new_sl = float(req.get("sl", 0.0))
        if new_sl:
            pip = float(FALLBACK_SYMBOLS.get(pos.symbol, {}).get("pip", 0.01))
            min_dist = self.enforce_stops_level * pip
            if abs(self.ask - new_sl) < min_dist:
                return _Result(self.TRADE_RETCODE_INVALID_STOPS, "Invalid stops")
            pos.sl = new_sl
        if req.get("tp"):
            pos.tp = req["tp"]
        return _Result(self.TRADE_RETCODE_DONE, "Done")

    def simulate_close(self, ticket, price):
        """O broker fecha a posição `ticket` ao preço dado (simula o TP/SL
        server-side a bater) e devolve o P&L registado."""
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return None
        sign = 1.0 if pos.type == self.POSITION_TYPE_BUY else -1.0
        contract = float(FALLBACK_SYMBOLS.get(pos.symbol, {}).get("contract", 100.0))
        profit = round(sign * (price - pos.price_open) * pos.volume * contract, 2)
        self._positions.remove(pos)
        self._deals[ticket].append(_Deal(ticket, price, profit, self.DEAL_ENTRY_OUT))
        return profit


class _Result:
    def __init__(self, retcode, comment, price=None):
        self.retcode = retcode
        self.comment = comment
        self.price = price


def setup_real(ask=4385.0, bid=4384.9, info_stops_level=0.0,
               enforce_stops_level=0.0, logs=None):
    """Motor em modo REAL (dry_run=False) com o broker fake stateful."""
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    provider = FakeProvider(ticks={"XAUUSD": (ask + bid) / 2})
    broker = _FakeBroker(ask=ask, bid=bid, info_stops_level=info_stops_level,
                         enforce_stops_level=enforce_stops_level)
    provider._mt5 = broker
    market = MarketSimulator(seed=42)
    market.prices["XAUUSD"] = (ask + bid) / 2
    eng = MT5Engine(db, provider, market, dry_run=False,
                    logger=(logs.append if logs is not None else print))
    return db, provider, broker, market, eng, tmp


def test_real_mode_auto_gestao_full_flow():
    """E2E do caminho real (dry_run=False) com broker fake: abre a trade com
    auto_mgmt, o TP1 fecha pelo broker (deal de saída) → escada sobe o SL
    para o nível do TP1 → trailing segue o pico (sem look-ahead) → time stop
    fecha as restantes ao mercado. O log regista os retcodes reais (10009)
    de cada modify aceite e a BD reflete os SLs *applied*."""
    logs: list = []
    db, provider, broker, market, eng, tmp = setup_real(logs=logs)
    try:
        sig = make_signal(take_profit_3=4402.0)  # entry 4385, SL 4381, TP1 4389, TP2 4395, TP3 4402
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 2.0, "time_stop_minutes": 5}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open"
        assert len(broker._positions) == 3  # 3 pernas no broker

        # ---- TP1 bate (server-side) → deal de saída no broker ----
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)
        leg1 = [p for p in db.positions_for_trade(trade.id) if p.leg == 1][0]
        assert leg1.status == "closed" and leg1.exit_reason == "tp"
        assert leg1.pnl > 0
        # escada: SL das pernas 2/3 para o NÍVEL do TP1 (4389), via broker
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0, f"perna {p.leg} SL = TP1 (escada)"
                bp = next(x for x in broker._positions if x.ticket == tickets[p.leg])
                assert bp.sl == 4389.0, f"perna {p.leg} SL também no broker"

        # ---- trailing segue o pico sem look-ahead -------------------------
        # O tick que observa 4410 só atualiza o pico; o SL correspondente
        # torna-se elegível no tick seguinte.
        state = eng._auto_state[trade.id]
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                state["last_mod"][p.id] = eng._auto_clock.monotonic() - 61
        provider._fake_ticks["XAUUSD"] = 4410.0
        broker.ask = 4410.05
        broker.bid = 4409.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:01:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                # A escada em 4389 continua a ser a proteção vencedora; o
                # trailing nunca pode recuar para o nível largo 4369.
                assert p.stop_loss == 4389.0
                state["last_mod"][p.id] = eng._auto_clock.monotonic() - 61

        provider._fake_ticks["XAUUSD"] = 4411.0
        broker.ask = 4411.05
        broker.bid = 4410.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:02:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4390.0, f"perna {p.leg} SL = pico − trail (4390)"
                bp = next(x for x in broker._positions if x.ticket == tickets[p.leg])
                assert bp.sl == 4390.0

        # ---- time stop: 10 min após o TP1 → fecham ao mercado ----
        eng.update(now="2026-08-18T10:10:00+00:00", auto=auto)
        closed = db.get_trade(trade.id)
        assert closed.status == "closed"
        reasons = {p.leg: p.exit_reason for p in db.positions_for_trade(trade.id)}
        assert reasons[2] == "time_stop" and reasons[3] == "time_stop"
        assert closed.pnl > 0
        assert broker._positions == []  # tudo fechado no broker

        # retcodes reais no log: os modifies SLTP foram aceites (10009)
        modify_lines = [l for l in logs if "modify_sl" in l or "auto_move_sl" in l]
        assert len(modify_lines) >= 4  # BE (2) + escada (2) + trailing (2)
        assert any("'retcode': 10009" in l for l in logs), \
            "modify aceite deve aparecer no log com retcode 10009"
        # o SL aplicado na BD == o que o broker confirmou (três níveis)
        assert any("4390.0" in l and "auto_move_sl" in l for l in logs)
        # auditoria dos três níveis (target→valid→applied) persistida
        import json
        sl_log = json.loads(db.all_config().get(f"autosl:{trade.id}") or "[]")
        assert sl_log, "a auditoria de SL da auto deve estar persistida"
        assert all(e["applied"] for e in sl_log), "todos os modifies foram aceites"
        assert all(e["retcode"] == 10009 for e in sl_log)
        assert any(e["kind"] == "stair" and e["valid"] == 4389.0 for e in sl_log)
        assert any(e["kind"] == "trail" and e["valid"] == 4390.0 for e in sl_log)
        assert all(e["clamped"] is False for e in sl_log)
        modify_stats = db.mt5_modify_weekly(days=90)
        assert modify_stats["total_attempts"] >= 6
        assert modify_stats["total_rejected"] == 0
    finally:
        db.close()
        os.remove(tmp)


def test_real_mode_reconstructs_auto_state_after_restart():
    """Se o servidor reiniciar depois do TP1, o MT5 real reconstrói o
    timestamp da proteção e o time stop não fica desligado para sempre."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        sig = make_signal(take_profit_3=4402.0)
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 2.0, "time_stop_minutes": 5}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)

        # Novo objeto = processo reiniciado; a BD/broker são os únicos estados
        # que sobrevivem.
        restarted = MT5Engine(db, provider, market, dry_run=False)
        restarted.update(now="2026-08-18T10:06:00+00:00", auto=auto)

        state = restarted._auto_state[trade.id]
        assert state["tp1_ts"] is not None
        positions = db.positions_for_trade(trade.id)
        assert all(p.status == "closed" for p in positions if p.leg != 1)
        assert all(p.exit_reason == "time_stop" for p in positions if p.leg != 1)
    finally:
        db.close()
        os.remove(tmp)


def test_real_mode_defensive_tightens_trail_after_pf_drop():
    """Modo defensivo no MT5 real (dry_run=False): com o PF das últimas 10
    trades reais fechadas < 1.0 (`def_window`), a auto passa a `def_trail` —
    o trailing usa a distância apertada em vez da normal (espelho do
    backtest/paper). Sem perdas recentes, o trail normal aplica-se."""
    logs: list = []
    db, provider, broker, market, eng, tmp = setup_real(logs=logs)
    try:
        # 10 trades reais fechadas com perda → PF < 1.0 → modo defensivo
        now = "2026-08-18T09:00:00+00:00"
        for i in range(10):
            with db.tx() as cur:
                cur.execute(
                    "INSERT INTO trades (id, signal_id, symbol, direction, entry,"
                    " stop_loss, take_profit_1, take_profit_2, status, opened_at,"
                    " closed_at, pnl, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"t-def-{i}", f"s-def-{i}", "XAUUSD", "buy", 4400.0, 4390.0,
                     4405.0, 4408.0, "closed", now, now, -20.0, "sl"))

        sig = make_signal(take_profit_3=4402.0)  # entry 4385, TP1 4389, TP2 4395
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 2.0, "time_stop_minutes": 0,
                "def_window": 10, "def_trail": 1.5, "def_time_stop": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000)
        assert trade.status == "open"

        # TP1 fecha pelo broker → escada (SL 4389)
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T10:00:00+00:00", auto=auto)

        # trailing defensivo: pico 4410 → dist = 1.5 × (4395−4385) = 15 →
        # SL = 4410 − 15 = 4395 (com trail 2.0 seria 4390)
        state = eng._auto_state[trade.id]
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                state["last_mod"][p.id] = eng._auto_clock.monotonic() - 61
        # Observa 4410 primeiro; só o tick seguinte pode usar esse pico.
        provider._fake_ticks["XAUUSD"] = 4410.0
        broker.ask = 4410.05
        broker.bid = 4409.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:01:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0
                state["last_mod"][p.id] = eng._auto_clock.monotonic() - 61
        provider._fake_ticks["XAUUSD"] = 4411.0
        broker.ask = 4411.05
        broker.bid = 4410.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T10:02:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4395.0, \
                    f"defensivo: SL = pico − 1.5×dist (4395), perna {p.leg}: {p.stop_loss}"

        # com o histórico agora TODO positivo (PF alto) → trail normal 2.0 →
        # SL = 4410 − 20 = 4390 (numa 2ª trade, com o preço de volta à entrada)
        with db.tx() as cur:
            cur.execute("UPDATE trades SET pnl=20.0 WHERE id LIKE 't-def-%'")
        broker.ask = 4385.0
        broker.bid = 4384.9
        provider._fake_ticks["XAUUSD"] = 4385.0
        sig2 = make_signal(id="s2-def", take_profit_3=4402.0)
        trade2 = eng.open_trade(sig2, risk_pct=1.0, balance=10_000)
        # matricio pelos comentários (a 1ª trade ainda tem pernas abertas no
        # broker — positions_get devolve todas)
        tickets2 = {}
        for bp in broker.positions_get():
            if bp.comment and trade2.id in bp.comment:
                leg = int(bp.comment.split("leg")[-1])
                tickets2[leg] = bp.ticket
        broker.simulate_close(tickets2[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        eng.update(now="2026-08-18T11:00:00+00:00", auto=auto)
        state2 = eng._auto_state[trade2.id]
        for p in db.positions_for_trade(trade2.id):
            if p.leg in (2, 3):
                state2["last_mod"][p.id] = eng._auto_clock.monotonic() - 61
        provider._fake_ticks["XAUUSD"] = 4410.0
        broker.ask = 4410.05
        broker.bid = 4409.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T11:01:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade2.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4389.0
                state2["last_mod"][p.id] = eng._auto_clock.monotonic() - 61
        provider._fake_ticks["XAUUSD"] = 4411.0
        broker.ask = 4411.05
        broker.bid = 4410.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T11:02:00+00:00", auto=auto)
        for p in db.positions_for_trade(trade2.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4390.0, \
                    f"PF alto → trail 2.0 (4390), perna {p.leg}: {p.stop_loss}"
    finally:
        db.close()
        os.remove(tmp)


def test_auto_modify_refused_10016_triggers_rollback():
    """E2E do caminho real com broker fake: um modify da auto-gestão
    RECUSADO pelo broker (retcode 10016 — stops inválidos) fica registado na
    auditoria `autosl:` com applied=False, e a janela de validação da Fase 3
    (`_compute_fase3_verdict`) deteta-o e decide ROLLBACK (o doc
    ESTRATEGIAS.md Parte X exige "retcodes 10009" — zero tolerado).

    Usa BD temporária própria e a função standalone (NÃO importa `app.main`
    — o singleton arranca o loop de fundo e prende o processo). Abre a trade
    real no broker fake, TP1 bate → escada tenta subir o SL → broker recusa
    com 10016."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from app.state import _compute_fase3_verdict
    from app.models import Trade

    db, provider, broker, market, eng, tmp = setup_real(enforce_stops_level=10000.0)
    try:
        # sinal REAL (received_at preenchido — evita falso "abertura lenta")
        sig = make_signal(
            id="e2e-10016-sig", source="test",
            received_at="2026-08-18T09:00:00+00:00",
            entry=4385.0, stop_loss=4381.0, take_profit_1=4389.0,
            take_profit_2=4395.0, take_profit_3=4402.0,
        )
        db.insert_signal(sig)
        auto = {"stair_levels": 3, "stair_from": 1, "confirm_filter": False,
                "trail_factor": 2.0, "time_stop_minutes": 0}
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000,
                               now="2026-08-18T09:00:30+00:00")
        assert trade.status == "open"
        assert len(broker._positions) == 3

        # ---- TP1 bate (broker fecha) → escada tenta subir o SL das pernas
        # 2/3 para 4389 (nível do TP1) → broker RECUSA (10016) ----
        tickets = {p.leg: broker.positions_get()[i].ticket
                   for i, p in enumerate(sorted(db.positions_for_trade(trade.id),
                                                key=lambda x: x.leg))}
        broker.simulate_close(tickets[1], 4389.0)
        provider._fake_ticks["XAUUSD"] = 4389.0
        broker.ask = 4389.05
        broker.bid = 4388.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now="2026-08-18T09:01:00+00:00", auto=auto)

        leg1 = [p for p in db.positions_for_trade(trade.id) if p.leg == 1][0]
        assert leg1.status == "closed" and leg1.pnl > 0
        # o SL das pernas 2/3 NÃO subiu (modify recusado — o SL só se torna
        # *applied* quando o broker confirma; ficou no SL original 4381)
        for p in db.positions_for_trade(trade.id):
            if p.leg in (2, 3):
                assert p.stop_loss == 4381.0, \
                    f"perna {p.leg} SL não mudou (modify recusado): {p.stop_loss}"
                bp = next(x for x in broker._positions if x.ticket == tickets[p.leg])
                assert bp.sl == 4381.0, "broker também manteve o SL original"

        # auditoria persistida: o modify recusado fica com retcode 10016 e
        # applied=False (os três níveis target→valid→applied)
        sl_log = json.loads(db.all_config().get(f"autosl:{trade.id}") or "[]")
        assert sl_log, "a auditoria de SL deve estar persistida"
        refused = [e for e in sl_log if e.get("retcode") == 10016]
        assert refused and not refused[0]["applied"], \
            "o modify recusado fica na auditoria com retcode 10016 / applied=False"

        # ---- fecha o resto da trade (legs 2/3 ao mercado); o tick seguinte
        # finaliza a trade (como em produção) ----
        for leg in (2, 3):
            eng.close_leg_at_market(trade.id, leg, now="2026-08-18T09:05:00+00:00",
                                    reason="close_leg")
        eng.update(now="2026-08-18T09:05:30+00:00")
        closed = db.get_trade(trade.id)
        assert closed.status == "closed" and closed.closed_at

        # ---- janela de validação (função standalone): promove + 10 trades
        # fechadas após promoted_at (a e2e + 9 falsas com lucro → PF ≥ 1; o
        # rollback tem de vir do retcode 10016, não do PF) ----
        promoted_at = "2026-08-18T08:00:00+00:00"
        db.set_config("fase3_snapshot", json.dumps({
            "promoted_at": promoted_at, "strategy_id": "auto_escada_3",
            "n": 0, "pf": None, "pnl": 0, "ref_pf": None, "ref_pnl": 0,
            "dd_auto": None, "dd_ref": None, "paper_vs_real_pairs": 0,
            "real_trades_closed": 0, "sl_audit_entries": 0,
        }))
        db.set_config("fase3_verdict", "")
        now = datetime.now(timezone.utc)
        for i in range(9):
            c = (now + timedelta(hours=i + 1)).isoformat(timespec="seconds")
            db.insert_trade(Trade(
                id=f"e2e-aux-{i}", signal_id=f"e2e-aux-sig-{i}", symbol="XAUUSD",
                direction="buy", entry=4385.0, stop_loss=4381.0,
                take_profit_1=4389.0, take_profit_2=4395.0, risk_pct=2.0,
                lots_1=0.01, lots_2=0.01, status="closed", opened_at=promoted_at,
                closed_at=c, pnl=5.0, exit_reason="tp1",
            ))
        v = asyncio.run(_compute_fase3_verdict(db, "auto_escada_3"))

        assert v is not None and v["decision"] == "rollback", \
            "modify recusado (10016) → rollback no veredicto"
        # as 2 pernas (2/3) tiveram o modify recusado → 2 na auditoria
        assert v["refused_retcodes"] == 2
        modify_stats = db.mt5_modify_weekly(days=90)
        # BE (2) e escada (2) foram tentados; o veredicto conta apenas a
        # auditoria auto-SL, enquanto a telemetria agrega todos os modifies.
        assert modify_stats["total_rejected"] == 4
        assert modify_stats["rejection_rate_pct"] > 0
        assert "recusados" in v["reason"] or "10016" in v["reason"]
    finally:
        db.close()
        os.remove(tmp)


def test_fase3_promotion_paper_to_mt5_restart_and_rollback():
    """Integra a promoção Fase 3 sem tocar num terminal real.

    A mesma entrada começa na corrida paper, é promovida, percorre o caminho
    MT5 real com broker fake, sobrevive a um restart (estado de TP1/time-stop
    reconstruído), regista dois modifies 10016 recusados e termina em rollback
    automático quando a janela chega a dez trades.
    """
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from app.models import ConfigUpdate, Trade
    from app.notify.telegram import Notifier
    from app.risk.engine import RiskEngine
    from app.state import BotState, WSManager

    db, provider, broker, market, eng, tmp = setup_real(
        enforce_stops_level=10000.0,
    )
    try:
        base = datetime.now(timezone.utc).replace(microsecond=0)
        signal = make_signal(
            id="fase3-e2e-sig", source="test",
            received_at=base.isoformat(), take_profit_3=4402.0,
        )
        db.insert_signal(signal)
        # BotState normally receives the feed adapter, which exposes this
        # dashboard-only flag; the deterministic MarketSimulator does not.
        market.real_data_active = False
        market.meta = lambda: {
            symbol: {"source": "test", "chg_pct": None}
            for symbol in market.prices
        }

        # Fase 2: a auto vê o mesmo sinal em paper e aplica a escada.
        bot = BotState(
            db, market, RiskEngine(), eng,
            Notifier(db, WSManager()), WSManager(),
        )
        paper = bot.paper.open_for_signal(
            signal, 1.0, strategy_ids=["auto_escada_3"], daily_stop_pct=0,
        )
        assert len(paper) == 1
        market.prices[signal.symbol] = signal.take_profit_1 + 0.0005
        bot.paper.tick(1.0)
        paper_positions = db.paper_positions_for_trade(paper[0].id)
        assert paper_positions[1].stop_loss == signal.take_profit_1
        assert paper_positions[1].status == "open"

        # Promoção real: usa o mesmo caminho de configuração da dashboard e
        # grava o snapshot que abre a janela de validação.
        promoted = asyncio.run(
            bot.update_config(ConfigUpdate(strategy="auto_escada_3"))
        )
        assert promoted["ok"] is True
        snapshot = json.loads(db.all_config()["fase3_snapshot"])
        assert snapshot["strategy_id"] == "auto_escada_3"
        promoted_at = datetime.fromisoformat(snapshot["promoted_at"])

        auto = {
            "stair_levels": 3, "stair_from": 1,
            "confirm_filter": False, "trail_factor": 2.0,
            "time_stop_minutes": 60,
            "def_window": 10, "def_trail": 1.5,
            "def_time_stop": 60,
        }
        opened_at = (promoted_at + timedelta(seconds=30)).isoformat()
        real_trade = eng.open_trade(
            signal, risk_pct=1.0, balance=10_000, now=opened_at,
        )
        db.set_trade_strategy(real_trade.id, "auto_escada_3")
        tickets = {
            p.leg: next(bp.ticket for bp in broker.positions_get()
                        if bp.comment == f"{real_trade.id}:leg{p.leg}")
            for p in db.positions_for_trade(real_trade.id)
        }

        # TP1 chega ao broker; a escada tenta elevar os SLs, mas o broker
        # recusa ambos os modifies com 10016. Nada recusado fica aplicado.
        tp1_at = promoted_at + timedelta(minutes=1)
        broker.simulate_close(tickets[1], signal.take_profit_1)
        provider._fake_ticks[signal.symbol] = signal.take_profit_1
        broker.ask = signal.take_profit_1 + 0.05
        broker.bid = signal.take_profit_1 - 0.05
        for bp in broker._positions:
            bp.price_current = broker.ask
        eng.update(now=tp1_at.isoformat(), auto=auto)

        refused = json.loads(db.all_config()[f"autosl:{real_trade.id}"])
        assert len([e for e in refused if e["retcode"] == 10016]) == 2
        assert all(e["applied"] is False for e in refused)
        assert all(
            p.stop_loss == signal.stop_loss
            for p in db.positions_for_trade(real_trade.id) if p.leg != 1
        )

        # Restart: só a BD e o broker sobrevivem. O novo motor reconstrói o
        # TP1 timestamp e dispara o time-stop sem depender do dicionário antigo.
        restarted = MT5Engine(db, provider, market, dry_run=False)
        restart_at = promoted_at + timedelta(minutes=63)
        provider._fake_ticks[signal.symbol] = signal.entry + 1.0
        broker.ask = signal.entry + 1.05
        broker.bid = signal.entry + 0.95
        for bp in broker._positions:
            bp.price_current = broker.ask
        restarted.update(now=restart_at.isoformat(), auto=auto)
        positions = db.positions_for_trade(real_trade.id)
        assert all(p.status == "closed" for p in positions)
        assert all(p.exit_reason == "time_stop" for p in positions if p.leg != 1)
        assert db.get_trade(real_trade.id).status == "closed"
        assert db.get_trade(real_trade.id).exit_reason == "time_stop"

        # Completa a janela com nove trades vencedoras. Os patches mantêm o
        # teste read-only quanto a Yahoo/backtest e isolam o veredicto nos
        # sinais desta pequena janela.
        for i in range(9):
            closed_at = restart_at + timedelta(minutes=10 + i)
            db.insert_trade(Trade(
                id=f"fase3-e2e-aux-{i}",
                signal_id=f"fase3-e2e-aux-sig-{i}",
                strategy_id="auto_escada_3", symbol="XAUUSD", direction="buy",
                entry=4385.0, stop_loss=4381.0, take_profit_1=4389.0,
                take_profit_2=4395.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
                status="closed", opened_at=promoted_at.isoformat(),
                closed_at=closed_at.isoformat(), pnl=5.0, exit_reason="tp1",
            ))

        async def empty_pairs(_db, strategy_id=None):
            return {"pairs": []}

        async def empty_backtest(_db, strategy_id=None):
            return {"trades": []}

        with patch("app.analysis.paper_vs_real_auto", new=empty_pairs), \
             patch("app.analysis.real_vs_backtest", new=empty_backtest):
            asyncio.run(bot._check_fase3_validation())

        assert bot.config["strategy"] == "nova_gestao"
        verdict = json.loads(db.all_config()["fase3_verdict"])
        assert verdict["decision"] == "rollback"
        assert verdict["window_trades"] == 10
        assert verdict["refused_retcodes"] == 2
        assert "10016" in verdict["reason"] or "recusados" in verdict["reason"]
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_mt5_atr_requests_completed_candles_only():
    """O ATR do MT5 não pode ler a vela em formação: o provider pede a
    posição 1 de `copy_rates_from_pos` (a posição 0 é a vela atual)."""
    db, provider, broker, market, eng, tmp = setup_real()
    try:
        broker.rates = [
            _Rate(101.0, 99.0, 100.0, time=1),
            _Rate(103.0, 100.0, 102.0, time=2),
            _Rate(104.0, 101.0, 103.0, time=3),
        ]
        atr = eng._atr("XAUUSD", period=2, timeframe=1)
        assert atr is not None
        assert broker.rate_calls and broker.rate_calls[-1][2] == 1
    finally:
        db.close()
        os.remove(tmp)
