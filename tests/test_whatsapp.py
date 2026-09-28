"""Testes do notifier WhatsApp (bridge Baileys).

Cobre a formatação da mensagem de fecho (resumo completo + P&L por perna +
link da dashboard), o envio para o bridge (sucesso e falha engolidos — nunca
quebra o tick) e o hook: uma trade REAL fechada (com conta MT5) dispara o
envio; sem conta MT5 (modo sim) nada é enviado. O hook só é chamado no loop
de eventos do motor principal — a corrida paper usa o seu próprio loop
(`paper.tick`), que não toca no WhatsApp.
"""

import asyncio

from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.models import Position, Trade
from app.notify.telegram import Notifier
from app.notify.whatsapp import (WhatsAppNotifier, format_dual_lane_close,
                                  format_trade_close)
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager

DASH = "https://n3xg3n.duckdns.org"


def _trade(**kw):
    base = dict(
        id="t1", signal_id="s1", symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4392.0, take_profit_1=4402.0,
        take_profit_2=4404.0, risk_pct=1.0, lots_1=0.1, lots_2=0.2,
        status="closed", opened_at="2026-08-21T12:04:00+00:00",
        closed_at="2026-08-21T14:32:00+00:00", pnl=27.0, exit_reason="tp2",
    )
    base.update(kw)
    return Trade(**base)


def _positions():
    return [
        Position(id="p1", trade_id="t1", leg=1, symbol="XAUUSD", direction="buy",
                 lots=0.1, entry=4400.0, stop_loss=4392.0, take_profit=4402.0,
                 status="closed", opened_at="2026-08-21T12:04:00+00:00",
                 closed_at="2026-08-21T13:00:00+00:00", close_price=4402.0,
                 pnl=12.0, exit_reason="tp1"),
        Position(id="p2", trade_id="t1", leg=2, symbol="XAUUSD", direction="buy",
                 lots=0.2, entry=4400.0, stop_loss=4392.0, take_profit=4404.0,
                 status="closed", opened_at="2026-08-21T12:04:00+00:00",
                 closed_at="2026-08-21T14:32:00+00:00", close_price=4404.0,
                 pnl=15.0, exit_reason="tp2"),
    ]


# ---------------------------------------------------------------- formatação


def test_format_trade_close_complete():
    msg = format_trade_close(_trade(), _positions(), DASH)
    assert "📦 XAUUSD fechada ✅ +27.00 €" in msg
    assert "➡️ Compra · entrada 4400" in msg
    assert "💸 Pernas:" in msg
    assert "L1 · 0.1 l · TP1 @ 4402 · +12.00 €" in msg
    assert "L2 · 0.2 l · TP2 @ 4404 · +15.00 €" in msg
    assert "🎯 Razão da trade: TP2 · 14:32 UTC" in msg
    assert "💰 P&L total +27.00 € · 0.3 lotes" in msg
    assert f"🔗 Detalhes: {DASH}" in msg


def test_format_trade_close_marks_mt5_dry_run_and_broker_balance():
    trade = _trade(execution_mode="mt5-dry")
    msg = format_trade_close(
        trade, _positions(), DASH,
        broker_account={"balance": 1000.0, "currency": "EUR"},
        simulation={"enabled": True, "realized_pnl": 27.0, "equity": 1027.0},
    )
    assert "🧪 MT5 DRY-RUN — resultado simulado" in msg
    assert "saldo broker 1,000.00 EUR" in msg
    assert "P&L total +27.00 €" in msg


def test_format_trade_close_loss_sell_default_url():
    trade = _trade(direction="sell", pnl=-12.5, exit_reason="sl",
                   closed_at="2026-08-21T09:05:00+00:00")
    msg = format_trade_close(trade, _positions(), "")
    assert "📦 XAUUSD fechada ❌ -12.50 €" in msg
    assert "➡️ Venda · entrada 4400" in msg
    assert "🎯 Razão da trade: SL · 09:05 UTC" in msg
    # dashboard_url vazia -> usa o default da config
    assert f"🔗 Detalhes: {DASH}" in msg


def test_format_trade_close_without_positions():
    msg = format_trade_close(_trade(), [], "")
    assert "💸 Pernas:" not in msg
    # lotes totais caem para os do trade (lots_1 + lots_2)
    assert "💰 P&L total +27.00 € · 0.3 lotes" in msg
    assert "🔗 Detalhes:" in msg


def test_format_trade_close_unknown_reason_and_bad_time():
    trade = _trade(exit_reason="mystery", closed_at="nao-e-uma-data")
    msg = format_trade_close(trade, [], "")
    assert "Razão da trade: mystery" in msg
    assert " UTC" not in msg


def test_format_trade_close_protect_and_manual_labels():
    trade = _trade(exit_reason="protect")
    assert "Razão da trade: Protect do grupo" in format_trade_close(trade, [], "")
    trade = _trade(exit_reason="killswitch")
    assert "Razão da trade: Kill switch" in format_trade_close(trade, [], "")


def test_format_trade_close_auto_management_labels():
    # razões da auto-gestão/MT5: close_leg, time_stop, confirm, auto_protect,
    # mixed e o "tp" genérico — rotulados em PT, não crus
    for raw, label in [("close_leg", "Fecho ao mercado"),
                       ("time_stop", "Time stop"),
                       ("confirm", "Confirmação"),
                       ("auto_protect", "Proteção automática"),
                       ("mixed", "Misto"),
                       ("tp", "TP")]:
        msg = format_trade_close(_trade(exit_reason=raw), [], "")
        assert f"Razão da trade: {label}" in msg, raw


def test_format_trade_close_derives_reason_from_last_leg():
    # exit_reason da trade vazio -> deriva da última perna fechada (o motor
    # faz o mesmo em `_maybe_close_trade`)
    trade = _trade(exit_reason=None)
    legs = _positions()  # L1 fecha 13:00 (tp1), L2 fecha 14:32 (tp2)
    msg = format_trade_close(trade, legs, "")
    assert "Razão da trade: TP2" in msg


# ------------------------------------------------------------------ notifier


class _FakeResponse:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_send_success(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            calls["url"] = url
            calls["json"] = json
            return _FakeResponse(200)

    monkeypatch.setattr("app.notify.whatsapp.httpx.AsyncClient", FakeClient)
    wa = WhatsAppNotifier(db=None, enabled="1", group_jid="1203630@g.us")
    assert asyncio.run(wa.send("olá grupo")) is True
    assert calls["url"] == "http://127.0.0.1:8765/send"
    assert calls["json"] == {"jid": "1203630@g.us", "text": "olá grupo"}


def test_send_failure_is_swallowed(monkeypatch):
    recorded = []

    class _DB:
        def record_exception(self, exc, context=None, **kw):
            recorded.append(exc)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            raise ConnectionError("bridge em baixo")

    monkeypatch.setattr("app.notify.whatsapp.httpx.AsyncClient", FakeClient)
    wa = WhatsAppNotifier(db=_DB(), enabled="1", group_jid="1203630@g.us",
                          max_attempts=3, retry_delay=0.0)
    # nunca levanta — o tick de execução continua; a exceção só é registada
    # UMA vez no fim (não uma por tentativa)
    assert asyncio.run(wa.send("olá")) is False
    assert len(recorded) == 1


def test_send_retries_transient_bridge_errors(monkeypatch):
    """Falhas transitórias 500/503 do bridge (janelas de religação) são
    retentadas — a mensagem não se perde por um único erro do sidecar."""
    calls = []

    class _DB:
        def record_exception(self, exc, context=None, **kw):
            raise AssertionError("não devia registar exceção")

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            calls.append(json)
            n = len(calls)
            if n < 3:
                return _FakeResponse(503 if n == 1 else 500)
            return _FakeResponse(200)

    monkeypatch.setattr("app.notify.whatsapp.httpx.AsyncClient", FakeClient)
    wa = WhatsAppNotifier(db=_DB(), enabled="1", group_jid="1203630@g.us",
                          max_attempts=3, retry_delay=0.0)
    assert asyncio.run(wa.send("mensagem crítica")) is True
    assert len(calls) == 3
    assert calls[0] == {"jid": "1203630@g.us", "text": "mensagem crítica"}


def test_whatsapp_status_endpoint_survives_bridge(monkeypatch):
    """Regressão: /api/whatsapp/status não rebenta quando o bridge devolve
    `last_error` (o KeyError('last_error') antigo abortava antes de buscar
    o QR) e o QR chega à dashboard como dataUrl."""
    import app.main as main_module

    class FakeResp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http")

        def json(self):
            return self._payload

    calls = []

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            calls.append(url)
            if url.endswith("/status"):
                return FakeResp({"connected": False, "paired": False,
                                 "phone": None, "qr": "RAW",
                                 "last_error": "408"})
            if url.endswith("/qr"):
                return FakeResp({"qr": "RAW",
                                 "dataUrl": "data:image/png;base64,xxx"})
            return FakeResp({})

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeClient)
    fake_wa = WhatsAppNotifier(db=None, enabled="1", group_jid="g@g.us")
    monkeypatch.setattr(main_module.state, "whatsapp", fake_wa)

    d = asyncio.run(main_module.whatsapp_status())
    assert d["enabled"] is True
    assert d["error"] is None          # sem KeyError('last_error')
    assert d["last_error"] == "408"
    assert d["qr"] == "data:image/png;base64,xxx"
    assert calls == [f"{fake_wa.bridge_url}/status",
                     f"{fake_wa.bridge_url}/qr"]


def test_whatsapp_restart_reconnects_when_bridge_alive(monkeypatch):
    """POST /api/whatsapp/restart com o bridge vivo pede reconnect ao
    Baileys e NÃO spawna um processo novo."""
    import app.main as main_module

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return FakeResp()

    spawned = []

    def fake_spawn():
        spawned.append(True)
        return None

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(main_module, "_spawn_whatsapp_bridge", fake_spawn)
    fake_wa = WhatsAppNotifier(db=None, enabled="1", group_jid="g@g.us")
    monkeypatch.setattr(main_module.state, "whatsapp", fake_wa)

    d = asyncio.run(main_module.whatsapp_restart())
    assert d == {"ok": True, "action": "reconnect"}
    assert spawned == []


def test_whatsapp_restart_spawns_when_bridge_down(monkeypatch):
    """Com o processo node morto (bridge não responde), o endpoint spawna-o
    — sem reiniciar o servidor."""
    import app.main as main_module

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            raise ConnectionError("bridge em baixo")

    class FakeProc:
        pid = 4242

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(main_module, "_spawn_whatsapp_bridge",
                        lambda: FakeProc())
    fake_wa = WhatsAppNotifier(db=None, enabled="1", group_jid="g@g.us")
    monkeypatch.setattr(main_module.state, "whatsapp", fake_wa)

    d = asyncio.run(main_module.whatsapp_restart())
    assert d == {"ok": True, "action": "spawned", "pid": 4242}


def test_send_not_configured_does_not_call_bridge(monkeypatch):
    called = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            called.append(url)
            return _FakeResponse(200)

    monkeypatch.setattr("app.notify.whatsapp.httpx.AsyncClient", FakeClient)
    wa = WhatsAppNotifier(db=None, enabled="", group_jid="1203630@g.us")
    assert asyncio.run(wa.send("olá")) is False
    assert called == []


# ---------------------------------------------------------------------- hook


class _FakeWa:
    configured = True
    dashboard_url = DASH

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True


class _FakeProvider:
    connected = True

    def account(self):
        return {"currency": "USD"}


class _FakeSimMT5:
    provider = _FakeProvider()


def _state(db, market, whatsapp):
    return BotState(
        db=db, market=market, risk=RiskEngine(),
        sim=SimEngine(db, market), notifier=Notifier(db, WSManager()),
        ws=WSManager(), whatsapp=whatsapp,
    )


def test_hook_sends_on_real_trade_close(tmp_path):
    """Fecho da lane TP2 → UMA mensagem só com a tp2 (formato single-lane)."""
    db = DB(str(tmp_path / "wa.db"))
    try:
        db.insert_trade(_trade(lane_id="tp2"))
        for p in _positions():
            db.insert_position(p)
        fake_wa = _FakeWa()
        state = _wa_state(db, fake_wa)
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1", "symbol": "XAUUSD",
             "pnl": 27.0, "reason": "tp2"}, lane_id="tp2"))
        assert len(fake_wa.sent) == 1
        msg = fake_wa.sent[0]
        assert "📦 XAUUSD fechada ✅ +27.00 €" in msg
        assert "L1 · 0.1 l · TP1" in msg
        assert f"🔗 Detalhes: {DASH}" in msg
    finally:
        db.close()


def test_hook_main_lane_does_not_send(tmp_path):
    """Fecho da lane MAIN → nada no WhatsApp: a mensagem é única por sinal e
    sai com o fecho da tp2 (era a causa das mensagens duplicadas)."""
    db = DB(str(tmp_path / "wa-main.db"))
    try:
        db.insert_trade(_trade())
        for p in _positions():
            db.insert_position(p)
        fake_wa = _FakeWa()
        state = _wa_state(db, fake_wa)
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1", "symbol": "XAUUSD",
             "pnl": 27.0, "reason": "tp2"}, lane_id="main"))
        assert fake_wa.sent == []
    finally:
        db.close()


def _trade2(**kw):
    """Trade da lane tp2 para o MESMO sinal (`s1`) — defaults: fechada em
    TP1 com +12 €; `kw` sobrepõe (ex.: status="open")."""
    t = _trade()
    base = {**t.to_dict(), "id": "t2", "pnl": 12.0, "status": "closed",
            "exit_reason": "tp1", "lane_id": "tp2",
            "strategy_id": "nova_gestao",
            "closed_at": "2026-08-21T13:00:00+00:00"}
    base.update(kw)
    return Trade(**base)


def _wa_state(db, fake_wa):
    """Estado com as DUAS lanes ativas (sim2 presente → `_lanes()` inclui
    tp2, como em produção com a 2ª conta ligada)."""
    sim2 = SimEngine(db, MarketSimulator())
    return BotState(
        db=db, market=MarketSimulator(), risk=RiskEngine(),
        sim=_FakeSimMT5(), notifier=Notifier(db, WSManager()),
        ws=WSManager(), whatsapp=fake_wa, second_sim=sim2,
        second_strategy="tp2_protect",
    )


def test_hook_dual_lane_ambas_fechadas_uma_mensagem(tmp_path):
    """Duas lanes fecham o mesmo sinal (mesmo tick ou ticks diferentes) → o
    hook publica só no fecho da tp2 e a mensagem mostra só a tp2."""
    db = DB(str(tmp_path / "wa5.db"))
    try:
        db.insert_trade(_trade())
        db.insert_trade(_trade2())
        for p in _positions():
            db.insert_position(p)
        fake_wa = _FakeWa()
        state = _wa_state(db, fake_wa)
        # fecho da main primeiro (tick 1): não envia
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1", "symbol": "XAUUSD",
             "pnl": 27.0, "reason": "tp2"}, lane_id="main"))
        assert fake_wa.sent == []
        # fecho da tp2 (tick 2, depois): UMA mensagem, só a tp2
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t2", "symbol": "XAUUSD",
             "pnl": 12.0, "reason": "tp1"}, lane_id="tp2"))
        assert len(fake_wa.sent) == 1
        msg = fake_wa.sent[0]
        assert "Conta principal" not in msg
        assert "nova_gestao · conta" in msg  # rótulo da lane tp2 (do trade)
        assert "+12.00 €" in msg
    finally:
        db.close()


def test_hook_dual_lane_outra_ainda_aberta(tmp_path):
    """A lane main fecha primeiro com a tp2 ainda aberta → nada é enviado;
    a mensagem sai só quando a tp2 fechar."""
    db = DB(str(tmp_path / "wa6.db"))
    try:
        db.insert_trade(_trade())
        tp2_open = _trade2(status="open", pnl=0.0, exit_reason=None,
                           closed_at=None, lane_id="tp2")
        db.insert_trade(tp2_open)
        for p in _positions():
            db.insert_position(p)
        fake_wa = _FakeWa()
        state = _wa_state(db, fake_wa)
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1", "symbol": "XAUUSD",
             "pnl": 27.0, "reason": "tp2"}, lane_id="main"))
        assert fake_wa.sent == []  # a tp2 ainda está aberta — e a main não publica
    finally:
        db.close()


def test_format_dual_lane_close_direct():
    """format_dual_lane_close continua disponível (uso histórico), mas o hook
    live usa format_trade_close single-lane."""
    from app.notify.whatsapp import format_dual_lane_close
    msg = format_dual_lane_close(
        _trade(),
        [
            {"lane_id": "main", "label": "Conta principal",
             "trade": _trade(), "positions": _positions()},
            {"lane_id": "tp2", "label": "tp2_protect · conta 5055369549",
             "trade": None, "positions": []},
        ],
        DASH, total_pnl=27.0,
    )
    assert "🤝 XAUUSD fechada — resumo das DUAS contas" in msg
    assert "🏷️ Conta principal" in msg
    assert "L1 · 0.1 l · TP1 @ 4402 · +12.00 €" in msg
    assert "tp2_protect · conta 5055369549: sem trade para este sinal" in msg
    assert "🎯 Razão do fecho: TP2" in msg
    assert f"🔗 Detalhes: {DASH}" in msg


def test_hook_skips_without_mt5_account(tmp_path):
    db = DB(str(tmp_path / "wa2.db"))
    try:
        db.insert_trade(_trade())
        fake_wa = _FakeWa()
        # SimEngine (sem provider) -> modo sim -> nunca envia
        state = _state(db, MarketSimulator(), fake_wa)
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1", "symbol": "XAUUSD",
             "pnl": 27.0, "reason": "tp2"}))
        assert fake_wa.sent == []
    finally:
        db.close()


def test_hook_noop_without_whatsapp(tmp_path):
    db = DB(str(tmp_path / "wa3.db"))
    try:
        state = _state(db, MarketSimulator(), None)
        assert state.whatsapp is None
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "t1"}))
    finally:
        db.close()


def test_hook_skips_unknown_trade(tmp_path):
    db = DB(str(tmp_path / "wa4.db"))
    try:
        fake_wa = _FakeWa()
        state = BotState(
            db=db, market=MarketSimulator(), risk=RiskEngine(),
            sim=_FakeSimMT5(), notifier=Notifier(db, WSManager()),
            ws=WSManager(), whatsapp=fake_wa,
        )
        asyncio.run(state._notify_whatsapp_trade_close(
            {"type": "trade_closed", "trade_id": "nao-existe",
             "symbol": "XAUUSD", "pnl": 0.0, "reason": "tp1"}))
        assert fake_wa.sent == []
    finally:
        db.close()
