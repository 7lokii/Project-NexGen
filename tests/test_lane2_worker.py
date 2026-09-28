"""Lane-2 worker (app/lane2_worker.py) + RemoteProvider (app/execution/remote2.py).

Cobre o caminho completo da 2ª conta com execução real:

  * o worker é um shim HTTP (ThreadingHTTPServer em memória, porta 0) com um
    provider fake injetado — exercita /initialize, /status, /constants,
    /mt5/call (allowlist + JSON-safer) e a auth por token;
  * o RemoteProvider do processo principal fala com esse worker e reconstitui
    o módulo MetaTrader5 que o MT5Engine usa (`provider._mt5`): constantes,
    `order_send`, `positions_get`, retcodes legíveis por atributo;
  * **fail-closed**: worker morto ⇒ `connected=False` (reason transitória) e
    o `MT5Engine(dry_run=False)` recusa abrir — nunca chega a enviar ordem.
"""

import asyncio
import os
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.config import DB_PATH
from app.db import DB
from app.execution.engine import SimEngine
from app.execution.market import MarketSimulator
from app.execution.mt5 import MT5Engine
from app.execution.remote2 import AttrDict, RemoteProvider, WorkerUnreachable
from app.lanes import lane_ab_report
from app.smoke import (pick_smoke_symbol, run_smoke,
                       smoke_guard_error, smoke_opt_in,
                       smoke_report_lines)
from app.lane2_worker import (Lane2Worker, MT5_CALL_ALLOWLIST, MT5_CALL_SHAPES,
                              MT5_ENUM_NAMES, normalize_mt5_call)
from app.models import Signal, Trade
from app.notify.telegram import Notifier
from app.risk.engine import RiskEngine
from app.state import BotState, WSManager


# ------------------------------------------------------------------- fakes


class FakeMT5:
    """Fake do módulo MetaTrader5 (constantes + métodos usados pelo engine)."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_STATE_FILLED = 4
    ORDER_STATE_CANCELED = 6
    ORDER_STATE_EXPIRED = 7
    ORDER_STATE_REJECTED = 8
    POSITION_TYPE_BUY = 0
    TRADE_RETCODE_DONE = 10009

    def __init__(self, retcode: int = 10009):
        self.retcode = retcode
        self.received: list = []  # (name, args, kwargs)

    def symbol_info_tick(self, sym):
        self.received.append(("symbol_info_tick", [sym], {}))
        return SimpleNamespace(bid=4442.0, ask=4442.5)

    def order_send(self, request):
        self.received.append(("order_send", [request], {}))
        return SimpleNamespace(retcode=self.retcode, comment="Done",
                               order=101, deal=202, price=4442.5)

    def positions_get(self, symbol=None):
        self.received.append(("positions_get", [], {"symbol": symbol}))
        return [SimpleNamespace(ticket=111, comment="t-test:leg1",
                                price_open=4442.5, type=1, volume=0.05)]

    def orders_get(self, ticket=None):
        self.received.append(("orders_get", [], {"ticket": ticket}))
        return []

    def order_delete(self, ticket=None):
        self.received.append(("order_delete", [], {"ticket": ticket}))
        return SimpleNamespace(retcode=10009, comment="Done")

    def history_deals_get(self, position=None):
        return []

    def symbol_select(self, *a, **k):
        return True

    def symbol_info(self, sym):
        return SimpleNamespace(trade_contract_size=100.0, digits=2,
                               volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, trade_tick_size=0.01)

    def terminal_info(self):
        return SimpleNamespace(name="FakeTerminal", build=999, trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(login=111495351, server="MetaQuotes-Demo",
                               currency="EUR", balance=1000.0, equity=1000.0,
                               leverage=100, trade_mode=0, margin_mode=2)

    def last_error(self):
        return (0, "no error")


class FakeProvider:
    """Duck-type do MT5Provider injetado no worker (sem MetaTrader5 real)."""

    def __init__(self):
        self._mt5 = FakeMT5()
        self.connected = False
        self.block_reason = ""
        self.margin_mode = None
        self.hedged = None
        self._account = None

    @property
    def execution_block_reason(self):
        return self.block_reason

    @property
    def account_margin_mode(self):
        return self.margin_mode

    @property
    def account_hedged(self):
        return self.hedged

    def initialize(self):
        info = self._mt5.account_info()
        self.margin_mode = getattr(info, "margin_mode", None)
        self.hedged = int(self.margin_mode or 0) == 2
        self._account = {k: getattr(info, k) for k in
                         ("login", "server", "currency", "balance",
                          "equity", "leverage", "trade_mode")}
        self.connected = True
        return True

    def account(self, cache_seconds=5.0):
        return self._account

    def positions(self, cache_seconds=2.0):
        return []

    def meta(self, app_symbol):
        return {"mt5": app_symbol, "contract": 100.0, "pip": 0.01,
                "digits": 2, "vol_min": 0.01, "vol_max": 100.0,
                "vol_step": 0.01}

    def quote(self, app_symbol):
        return {"symbol": app_symbol, "bid": 4442.0, "ask": 4442.5,
                "spread": 0.5, "spread_pips": 0.5, "source": "mt5"}

    def copy_rates(self, app_symbol, timeframe=1, count=100):
        return []

    def ensure_symbol(self, app_symbol):
        return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def worker_server():
    prov = FakeProvider()
    wk = Lane2Worker(prov, port=0, token="test-token")
    wk.bind()
    url = f"http://127.0.0.1:{wk.port}"
    thread = threading.Thread(target=wk.serve_forever, daemon=True)
    thread.start()
    yield wk, prov, url
    wk.shutdown()
    thread.join(timeout=5)


def _auth_headers(token="test-token"):
    return {"x-api-token": token}


# ------------------------------------------------------- worker (HTTP)

def test_worker_auth_recusa_sem_token(worker_server):
    _, _, url = worker_server
    r = httpx.get(f"{url}/ping", timeout=10)
    assert r.status_code == 401
    r2 = httpx.get(f"{url}/status", timeout=10)
    assert r2.status_code == 401


def test_worker_initialize_status_e_account(worker_server):
    _, prov, url = worker_server
    h = _auth_headers()
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert st["connected"] is False and st["ok"] is True

    init = httpx.post(f"{url}/initialize", headers=h, timeout=10).json()
    assert init["connected"] is True and prov.connected is True
    assert init["account"]["login"] == 111495351

    st2 = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert st2["connected"] is True and st2["login"] == 111495351


def test_worker_constants_e_call_allowlist(worker_server):
    _, prov, url = worker_server
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)

    consts = httpx.get(f"{url}/constants", headers=h, timeout=10).json()
    assert consts["constants"]["ORDER_TYPE_BUY"] == 0
    assert consts["constants"]["TRADE_RETCODE_DONE"] == 10009
    for name in MT5_ENUM_NAMES:
        assert name in consts["constants"]

    # chamada permitida: order_send com request JSON → resultado JSON-safe
    r = httpx.post(f"{url}/mt5/call", headers=h, timeout=10,
                   json={"name": "order_send",
                         "args": [{"action": 1, "symbol": "XAUUSD",
                                   "volume": 0.05, "type": 1, "sl": 4449.5}],
                         "kwargs": {}}).json()
    assert r["ok"] is True and r["result"]["retcode"] == 10009
    assert any(name == "order_send" for name, _, _ in prov._mt5.received)

    # allowlist: nomes fora da lista são recusados (nunca executados)
    r2 = httpx.post(f"{url}/mt5/call", headers=h, timeout=10,
                    json={"name": "shutdown", "args": [], "kwargs": {}})
    assert r2.status_code == 400 and "não permitida" in r2.json()["error"]


def test_worker_sem_initialize_recusa_call(worker_server):
    _, _, url = worker_server
    h = _auth_headers()
    r = httpx.post(f"{url}/mt5/call", headers=h, timeout=10,
                   json={"name": "order_send", "args": [], "kwargs": {}})
    assert r.status_code == 503  # MT5 não ligado → fail-closed


# -------------------------------------------------- RemoteProvider (cliente)


def test_remote_provider_roundtrip(worker_server):
    _, prov, url = worker_server
    client = RemoteProvider(base_url=url, token="test-token")

    assert client.initialize() is True
    assert client.connected is True
    assert client.account()["login"] == 111495351
    assert client.tick("XAUUSD") == 4442.25
    assert client.quote("XAUUSD")["source"] == "mt5"
    meta = client.meta("XAUUSD")
    assert meta["contract"] == 100.0 and meta["mt5"] == "XAUUSD"

    # o módulo MT5 que o engine usa (provider._mt5): constantes + chamadas
    assert client._mt5.ORDER_TYPE_BUY == 0
    assert client._mt5.TRADE_RETCODE_DONE == 10009
    res = client._mt5.order_send({"action": 1, "symbol": "XAUUSD",
                                  "volume": 0.05, "type": 1, "sl": 4449.5})
    assert isinstance(res, AttrDict)
    assert getattr(res, "retcode") == 10009
    assert res.comment == "Done" and res.order == 101 and res.deal == 202

    positions = client._mt5.positions_get(symbol="XAUUSD")
    assert isinstance(positions, list) and positions[0].ticket == 111
    assert positions[0].comment == "t-test:leg1"

    tick = client._mt5.symbol_info_tick("XAUUSD")
    assert tick.ask == 4442.5 and tick.bid == 4442.0

    # registo no worker: as chamadas chegaram mesmo (proxy ≠ local)
    names = [name for name, _, _ in prov._mt5.received]
    assert "order_send" in names and "positions_get" in names


def test_remote_provider_cacheia_account_e_positions(worker_server):
    """REGRESSÃO (snapshot stalls): o `RemoteProvider` tem de cachear
    `account()`/`positions()` com TTL como o `MT5Provider` local — o
    `_build_snapshot` consulta a conta 5–8× e as posições 2–3× por lane por
    tick; sem cache são 10+ HTTP ao worker (15–160ms cada) e o loop congela
    em `snapshot.build`. A execução (engine → `_mt5.positions_get`) fica
    FORA deste cache — nunca há stale para decidir SL/TP."""
    _, _, url = worker_server
    client = RemoteProvider(base_url=url, token="test-token")
    assert client.initialize() is True

    calls: list = []
    orig_get = client._get

    def counting(path, *a, **kw):
        calls.append(path)
        return orig_get(path, *a, **kw)

    client._get = counting
    for _ in range(6):
        assert client.account()["login"] == 111495351
    for _ in range(6):
        assert client.positions() == []
    # um /account e um /positions (o /status da primeira chamada também é
    # cacheado 1s) — sem cache seriam 6+6 HTTP round-trips
    assert calls.count("/account") == 1, calls
    assert calls.count("/positions") == 1, calls
    assert calls.count("/status") == 1, calls

    # o cache expira: depois do TTL volta a ler do worker (nunca fica preso
    # em dados antigos para sempre)
    client._account_ts = 0.0
    client._positions_ts = 0.0
    client._status_ts = 0.0
    assert client.account()["login"] == 111495351
    assert client.positions() == []
    assert calls.count("/account") == 2 and calls.count("/positions") == 2

    # a execução NÃO passa por este cache: o módulo proxy volta ao worker
    prov_pos = client._mt5.positions_get(symbol="XAUUSD")
    assert isinstance(prov_pos, list)


def test_remote_provider_transiente_offline_nao_bloqueia_ordem_permanente():
    """Worker morto ⇒ fail-closed: connected False, reason TRANSITÓRIA
    (o reconnect tem de continuar a tentar) e ordens recusadas."""
    dead = f"http://127.0.0.1:{_free_port()}"
    client = RemoteProvider(base_url=dead, token="x")
    assert client.initialize() is False
    assert client.connected is False
    assert client.execution_block_reason == ""  # não é um bloqueio permanente
    assert "worker lane 2" in (client._offline_reason or "").lower()
    with pytest.raises(WorkerUnreachable):
        client._mt5.order_send({"action": 1})


def test_engine_real_recusa_abrir_com_worker_morto():
    """MT5Engine(dry_run=False) com RemoteProvider a apontar para porta morta
    não chega a enviar nada — `open_trade` levanta RuntimeError (fail-closed)."""
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        client = RemoteProvider(base_url=f"http://127.0.0.1:{_free_port()}", token="x")
        eng = MT5Engine(db, client, market=None, dry_run=False, lane_id="tp2")
        sig = Signal(id="s-worker-dead", source="test", raw_text="",
                     symbol="XAUUSD", direction="buy", entry=4385.0,
                     stop_loss=4381.0, take_profit_1=4389.0,
                     take_profit_2=4395.0, status="pending", received_at="")
        with pytest.raises(RuntimeError, match="sem ligação MT5"):
            eng.open_trade(sig, risk_pct=1.0, balance=1000.0)
        # nenhuma trade fica registada
        assert db.open_trades() == []
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_engine_remote_recusa_10027_fica_gravada(worker_server):
    """REGRESSÃO 01/09: a recusa remota tem de chegar ao engine com o
    retcode REAL (10027) — não `'?'`. O caminho completo worker →
    RemoteProvider → MT5Engine fecha a trade phantom e grava o trace
    durável na BD (error/retcodes/auto_trading_off)."""
    _, prov, url = worker_server
    prov._mt5.retcode = 10027  # AutoTrading OFF no terminal 2
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        client = RemoteProvider(base_url=url, token="test-token")
        assert client.initialize() is True
        eng = MT5Engine(db, client, market=None, dry_run=False, lane_id="tp2")
        sig = Signal(id="s-remote-10027", source="test", raw_text="",
                     symbol="XAUUSD", direction="buy", entry=4385.0,
                     stop_loss=4381.0, take_profit_1=4389.0,
                     take_profit_2=4395.0, status="pending", received_at="")
        trade = eng.open_trade(sig, risk_pct=1.0, balance=1000.0)

        assert trade.status == "closed"
        assert trade.retcodes == [10027, 10027], trade.retcodes
        assert trade.auto_trading_off is True
        assert "AutoTrading" in (trade.error or ""), trade.error
        # posições fechadas phantom (cleanup do envio recusado, sem razão)
        for p in db.positions_for_trade(trade.id):
            assert p.status == "closed" and p.exit_reason is None
        # trace durável GRAVADO (set_trade_refusal) — não só em memória
        t2 = db.get_trade(trade.id)
        assert t2.retcodes == [10027, 10027]
        assert t2.auto_trading_off is True
        assert "AutoTrading" in (t2.error or "")
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_engine_remote_erro_no_worker_nao_deixa_trade_meia_aberta(worker_server):
    """Exceção no envio (worker caiu a meio) NÃO desenrola o open_trade com
    a trade meia-aberta: a perna falha com `REMOTE_ERROR`, o texto real fica
    no error e a trade fecha phantom como esperado."""
    _, prov, url = worker_server

    def _boom(request):
        raise RuntimeError("order_send falhou: invalid filling (worker)")

    prov._mt5.order_send = _boom
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        client = RemoteProvider(base_url=url, token="test-token")
        assert client.initialize() is True
        eng = MT5Engine(db, client, market=None, dry_run=False, lane_id="tp2")
        sig = Signal(id="s-remote-boom", source="test", raw_text="",
                     symbol="XAUUSD", direction="buy", entry=4385.0,
                     stop_loss=4381.0, take_profit_1=4389.0,
                     take_profit_2=4395.0, status="pending", received_at="")
        trade = eng.open_trade(sig, risk_pct=1.0, balance=1000.0)

        assert trade.status == "closed"
        assert "REMOTE_ERROR" in (trade.retcodes or []), trade.retcodes
        # o motivo real nunca se perde no erro da trade
        assert "order_send falhou" in (trade.error or ""), trade.error
        # nada fica aberto/meio-aberto na BD
        assert db.open_trades() == []
        assert trade.auto_trading_off is not True  # não é 10027
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_worker_recusa_arrancar_sem_terminal_portable(monkeypatch, capsys):
    """GUARD CRÍTICO (incidente 01/09): sem MT5_2_TERMINAL_PATH o worker
    recusa arrancar. Sem instalação portable, `initialize(login=...)` liga ao
    TERMINAL POR OMISSÃO e troca a conta da principal — nunca pode acontecer.
    """
    from app import lane2_worker as lw
    monkeypatch.delenv("MT5_2_TERMINAL_PATH", raising=False)
    rc = lw.main(["--host", "127.0.0.1", "--port", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERRO FATAL" in err and "MT5_2_TERMINAL_PATH" in err

    # path inexistente também recusa
    monkeypatch.setenv("MT5_2_TERMINAL_PATH", r"C:\\nope\\terminal64.exe")
    rc = lw.main(["--host", "127.0.0.1", "--port", "0"])
    assert rc == 2
    assert "nao existe" in capsys.readouterr().err


def test_worker_offline_depois_de_inicializado_fica_fail_closed(worker_server):
    """O worker cai a meio: o cliente passa a desligado sem apagar o estado."""
    wk, prov, url = worker_server
    client = RemoteProvider(base_url=url, token="test-token")
    assert client.initialize() is True
    assert client._mt5.ORDER_TYPE_BUY == 0  # constantes em cache

    wk.shutdown()  # derruba o worker
    time.sleep(0.1)
    assert client.connected is False
    assert client.execution_block_reason == ""
    assert client._reachable is False
    with pytest.raises(WorkerUnreachable):
        client._mt5.positions_get(symbol="XAUUSD")


# ------------------------------------------------ AutoTrading da lane 2


def test_worker_status_expoe_trade_allowed(worker_server):
    """/status espelha o AutoTrading do terminal portátil (terminal_info),
    para o Diagnóstico e o alerta OFF funcionarem sem polling extra."""
    _, prov, url = worker_server
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert st["connected"] is True
    assert st["trade_allowed"] is True  # FakeMT5.terminal_info → trade_allowed=True


def test_worker_at_off_quando_terminal_recusa(worker_server):
    """AutoTrading OFF (10027 na lançadeira): o /status tem de o dizer."""
    _, prov, url = worker_server
    prov._mt5.terminal_info = lambda: SimpleNamespace(
        name="FakeTerminal", build=999, trade_allowed=False)
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert st["connected"] is True and st["trade_allowed"] is False


def test_remote_provider_espelha_trade_allowed(worker_server):
    """O RemoteProvider do servidor vê trade_allowed do worker (e a flag
    at_off para o Diagnóstico), desligando quando o broker cai."""
    _, prov, url = worker_server
    client = RemoteProvider(base_url=url, token="test-token")
    assert client.initialize() is True
    assert client.trade_allowed is True
    s = client.status_summary()
    assert s["trade_allowed"] is True and s["at_off"] is False

    # AutoTrading OFF no terminal → espelho OFF (alertável)
    prov._mt5.terminal_info = lambda: SimpleNamespace(
        name="F", build=1, trade_allowed=False)
    assert client.refresh_status()["connected"] is True
    assert client.trade_allowed is False
    assert client.status_summary()["at_off"] is True

    # broker cai → trade_allowed nunca fica True
    wk, _, _ = worker_server
    wk.shutdown()
    time.sleep(0.1)
    assert client.trade_allowed is False


def test_worker_calls_modulo_mt5_sempre_na_mesma_thread(worker_server):
    """REGRESSÃO 03/09: o módulo MetaTrader5 não é thread-safe — o worker
    (ThreadingHTTPServer = uma thread por pedido) tem de chamar o módulo
    SEMPRE na MESMA thread dedicada (pool de 1 worker), como o processo da
    conta principal (ordens reais passam lá). Chamadas a partir de threads
    HTTP diferentes faziam o `order_send` válido devolver `None` (retcode
    null → '?') e a lane 2 nunca preenchia."""
    import threading as _th

    _, prov, url = worker_server
    seen = []
    orig = prov._mt5.symbol_info_tick

    def spy(sym):
        seen.append(_th.get_ident())
        return orig(sym)

    prov._mt5.symbol_info_tick = spy
    h = _auth_headers()
    for _ in range(8):
        r = httpx.post(f"{url}/mt5/call", headers=h, timeout=10,
                       json={"name": "symbol_info_tick",
                             "args": ["XAUUSD"], "kwargs": {}})
        assert r.status_code == 200 and r.json()["ok"] is True
    assert len(seen) == 8
    # todas as chamadas vieram da MESMA thread do módulo
    assert len(set(seen)) == 1, f"chamadas MT5 em threads diferentes: {seen}"


def test_worker_order_send_none_expoe_erro_real_do_modulo(worker_server):
    """O `order_send` a devolver `None` mantém o contrato do retcode null
    (ok=True, result=None — o engine grava '?' como antes), mas o erro REAL
    do módulo (last_error na mesma thread) fica em `last_error` do /status —
    o alerta de retcode null deixa de ser um mistério (ex.: IPC timeout)."""
    _, prov, url = worker_server
    prov._mt5.order_send = lambda request: None
    prov._mt5.last_error = lambda: (-10005, "IPC timeout")
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)

    r = httpx.post(f"{url}/mt5/call", headers=h, timeout=10,
                   json={"name": "order_send",
                         "args": [{"action": 1, "symbol": "XAUUSD",
                                   "volume": 0.05}], "kwargs": {}}).json()
    assert r["ok"] is True and r["result"] is None  # contrato '?' mantido
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert "IPC timeout" in (st["last_error"] or "")
    assert "order_send devolveu None" in (st["last_error"] or "")


def test_worker_pool_encerra_no_shutdown(worker_server):
    """O shutdown do worker também encerra a thread única do módulo — sem
    threads fantasmas a segurar o processo (pytest/interprete)."""
    wk, _, url = worker_server
    h = _auth_headers()
    httpx.get(f"{url}/status", headers=h, timeout=10)
    assert wk._mt5_pool is not None
    wk.shutdown()
    assert wk._mt5_pool is None


# -------------------------------------------------- A/B das lanes (read-only)


def _mk_trade(db, tid, sid, lane, pnl, strategy="nova_gestao", reason="tp1"):
    t = Trade(id=tid, signal_id=sid, symbol="XAUUSD", direction="buy",
              entry=4400.0, stop_loss=4396.0, take_profit_1=4404.0,
              take_profit_2=4407.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
              status="open", opened_at="2026-09-01T00:00:00+00:00",
              strategy_id=strategy, lane_id=lane)
    db.insert_trade(t)
    db.close_trade(tid, "2026-09-01T01:00:00+00:00", pnl, reason=reason)


def _ab_db(tmp, n_signals: int, main_pnl, tp2_pnl, main_only: int = 0):
    """BD temporária com trades fechadas por sinal nas duas lanes."""
    db = DB(tmp)
    for i in range(n_signals):
        sid = f"s-{i}"
        _mk_trade(db, f"m-{i}", sid, "main", main_pnl(i),
                  strategy="tp2_protect", reason="tp2")
        _mk_trade(db, f"t-{i}", sid, "tp2", tp2_pnl(i),
                  strategy="nova_gestao", reason="tp1")
    for i in range(main_only):  # só numa lane → nunca entra no comparável
        _mk_trade(db, f"mo-{i}", f"solo-{i}", "main", 5.0,
                  strategy="tp2_protect", reason="tp2")
    return db


def test_lane_ab_report_amostra_insuficiente_sem_sugestao():
    tmp = tempfile.mktemp(suffix=".db")
    db = _ab_db(tmp, n_signals=3, main_pnl=lambda i: 2.0, tp2_pnl=lambda i: 8.0,
                main_only=1)
    try:
        r = lane_ab_report(db, min_comparable=20)
        assert r["n"] == 3  # o sinal só-main fica fora
        assert r["sufficient"] is False
        assert r["suggestion"] is None
        assert r["verdict"]["kind"] == "insufficient"
        assert "20" in r["verdict"]["message"]
    finally:
        db.close()
        os.remove(tmp)


def test_lane_ab_report_sugere_manutencao_com_20_e_tp2_a_frente():
    tmp = tempfile.mktemp(suffix=".db")
    db = _ab_db(tmp, n_signals=20, main_pnl=lambda i: 2.0, tp2_pnl=lambda i: 12.0)
    try:
        r = lane_ab_report(db, min_comparable=20)
        assert r["n"] == 20 and r["sufficient"] is True
        assert r["per_signal"]["tp2_wins"] == 20
        assert r["delta_total"] == 200.0
        assert r["suggestion"] == "maintenance_tp2_first"
        assert r["verdict"]["kind"] == "tp2_first"
        assert r["tp2"]["strategy"] == "nova_gestao"
        assert r["main"]["strategy"] == "tp2_protect"
        assert len(r["signals"]) == 20
    finally:
        db.close()
        os.remove(tmp)


def test_lane_ab_report_neutro_sem_diferenca_material():
    tmp = tempfile.mktemp(suffix=".db")
    db = _ab_db(tmp, n_signals=20,
                main_pnl=lambda i: 6.0 if i % 2 == 0 else -4.0,
                tp2_pnl=lambda i: 6.0 if i % 2 == 0 else -4.0)
    try:
        r = lane_ab_report(db, min_comparable=20)
        assert r["sufficient"] is True
        assert r["delta_total"] == 0.0
        assert r["suggestion"] is None
        assert r["verdict"]["kind"] == "neutral"
    finally:
        db.close()
        os.remove(tmp)


def test_lane_ab_report_exclui_fechos_phantom():
    """Fechos phantom (sem exit_reason — ordens recusadas, pnl 0.0) NUNCA
    entram no comparável: ficam em `voids` e o veredicto explica a amostra
    vazia em vez de contar 0.0 como resultado de uma estratégia."""
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        # 2 pares reais (com razão de saída) + 1 par onde o tp2 é phantom
        for i in range(2):
            sid = f"s-{i}"
            _mk_trade(db, f"m-{i}", sid, "main", 4.0,
                      strategy="tp2_protect", reason="tp2")
            _mk_trade(db, f"t-{i}", sid, "tp2", 6.0,
                      strategy="nova_gestao", reason="tp1")
        sid = "s-void"
        _mk_trade(db, "m-void", sid, "main", 19.74,
                  strategy="tp2_protect", reason="tp")
        v = Trade(id="t-void", signal_id=sid, symbol="XAUUSD",
                  direction="buy", entry=4400.0, stop_loss=4396.0,
                  take_profit_1=4404.0, take_profit_2=4407.0, risk_pct=1.0,
                  lots_1=0.01, lots_2=0.01, status="open",
                  opened_at="2026-09-01T12:00:00+00:00",
                  strategy_id="nova_gestao", lane_id="tp2")
        db.insert_trade(v)
        db.close_trade("t-void", "2026-09-01T12:30:00+00:00", 0.0)  # razão None

        r = lane_ab_report(db, min_comparable=20)
        assert r["n"] == 2          # o par phantom fica fora do comparável
        assert r["void_count"] == 1
        assert r["main"]["voids"] == 0 and r["tp2"]["voids"] == 1
        vd = r["voids"][0]
        assert vd["signal_id"] == sid
        assert vd["main_void"] is False and vd["tp2_void"] is True
        assert vd["main_pnl"] == 19.74 and vd["tp2_pnl"] == 0.0
        # trace durável da recusa exposto no void (legado → None; o envio
        # novo grava error/retcodes via set_trade_refusal)
        assert vd["tp2_error"] is None and vd["tp2_retcodes"] is None
        assert vd["tp2_auto_trading_off"] is None
        assert "tp2_error" in vd and "tp2_retcodes" in vd  # sempre presentes
        assert r["tp2"]["total_pnl"] == 12.0   # 6+6, sem os 0.0 phantom
        assert r["delta_total"] == 4.0          # 12-8
        assert r["sufficient"] is False
    finally:
        db.close()
        os.remove(tmp)


def test_trade_refusal_fica_gravada_na_bd():
    """O trace durável da recusa (error/retcodes/auto_trading_off) persiste
    na trade e volta a ler-se — antes vivia só em memória e o fecho phantom
    ficava inexplicável (retcodes '?' perdidos)."""
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        _mk_trade(db, "t-ref", "s-ref", "main", 0.0, reason=None)
        db.set_trade_refusal(
            "t-ref",
            error="AutoTrading desligado no terminal MT5 (retcode 10027)",
            retcodes=[10027, 10027, 10027], auto_trading_off=True,
        )
        t = db.get_trade("t-ref")
        assert t.error == "AutoTrading desligado no terminal MT5 (retcode 10027)"
        assert t.retcodes == [10027, 10027, 10027]
        assert t.auto_trading_off is True

        # retcodes '?' (sem flag 10027) também ficam gravados
        db.set_trade_refusal("t-ref",
                             error="ordem MT5 recusada pelo broker",
                             retcodes=["?", "?"], auto_trading_off=False)
        t2 = db.get_trade("t-ref")
        assert t2.retcodes == ["?", "?"]
        assert t2.auto_trading_off is False
    finally:
        db.close()
        os.remove(tmp)


def test_lane_ab_report_so_phantoms_explica_amostra_vazia():
    """TODOS os pares sem fills reais → n=0 com a mensagem a apontar os
    fechos phantom (o caso real de 01/09: AutoTrading OFF na lane 2)."""
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        sid = "s-1"
        _mk_trade(db, "m-1", sid, "main", 4.04,
                  strategy="tp2_protect", reason="sl")
        v = Trade(id="t-1", signal_id=sid, symbol="XAUUSD",
                  direction="buy", entry=4377.0, stop_loss=4369.0,
                  take_profit_1=4380.0, take_profit_2=4382.0, risk_pct=1.0,
                  lots_1=0.01, lots_2=0.01, status="open",
                  opened_at="2026-09-01T08:31:42+00:00",
                  strategy_id="nova_gestao", lane_id="tp2")
        db.insert_trade(v)
        db.close_trade("t-1", "2026-09-01T08:31:42+00:00", 0.0)

        r = lane_ab_report(db, min_comparable=20)
        assert r["n"] == 0 and r["void_count"] == 1
        assert r["sufficient"] is False
        assert r["suggestion"] is None
        assert "phantom" in r["verdict"]["message"]
        assert "ordens recusadas" in r["verdict"]["message"]
        assert r["main"]["total_pnl"] == 0.0  # sem métricas envenenadas
    finally:
        db.close()
        os.remove(tmp)

# ------------------------------------------------------------------ quote watch


def test_lane2_quote_watch_resseleciona_simbolo_sem_tick_e_alerta(monkeypatch):
    """O market_loop vigia o quote de XAUUSD no worker da lane 2: sem tick,
    re-adiciona o símbolo ao Market Watch (ensure_symbol) e emite alerta risk
    com o estado — e quando o tick volta, notifica a recuperação."""
    import asyncio
    import app.main as main_module

    calls = {"ensure": 0}
    class FakeClient:
        def __init__(self, quote_state):
            self.quote_state = quote_state  # None | dict
        def quote(self, sym):
            return self.quote_state
        def ensure_symbol(self, sym):
            calls["ensure"] += 1

    # episódio: sem tick → re-seleciona e alerta
    main_module._last_lane2_quote_check = 0.0
    main_module._last_lane2_quote_warn = 0.0
    main_module._lane2_quote_broken_since = None
    client = FakeClient(None)
    asyncio.run(main_module._lane2_quote_watch(client))
    assert calls["ensure"] == 1, "sem tick tem de re-adicionar o símbolo"
    notifs = main_module.db.list_notifications(100)
    warn = [n for n in notifs if "SEM tick" in (n.text or "")]
    assert warn, "sem tick na lane 2 tem de alertar"
    assert warn[-1].lane == "tp2"

    # o alerta tem cooldown de 10 min — passada imediata não spamma
    before = len(main_module.db.list_notifications(100))
    asyncio.run(main_module._lane2_quote_watch(client))
    after = len(main_module.db.list_notifications(100))
    assert after == before, "cooldown de 10 min entre alertas do mesmo episódio"

    # recuperação: tick volta → notifica e limpa o episódio
    client.quote_state = {"bid": 4371.13, "ask": 4371.37}
    main_module._last_lane2_quote_check = 0.0
    asyncio.run(main_module._lane2_quote_watch(client))
    notifs = main_module.db.list_notifications(100)
    assert any("voltou a ter tick" in (n.text or "") for n in notifs)
    assert main_module._lane2_quote_broken_since is None


# ------------------------------------------------- marshal por convenção (03/09)

# As formas EXATAS que os engines usam via provider._mt5 — a mesma grelha do
# worker (MT5_CALL_SHAPES). `kwargs` é o dict que o RemoteMT5Module recebeu
# do bytecode do engine (pode ser vazio — o bug era passá-lo ao módulo).
ENGINE_CALL_SHAPES = [
    # (nome, args do engine, kwargs do engine)
    ("order_send", [{"action": 1, "symbol": "XAUUSD", "volume": 0.01,
                     "comment": "t-x:leg1", "type": 0}], {}),
    ("symbol_info_tick", ["XAUUSD"], {}),
    ("symbol_info", ["XAUUSD"], {}),
    ("terminal_info", [], {}),
    ("account_info", [], {}),
    ("positions_get", [], {"symbol": "XAUUSD"}),
    ("orders_get", [], {"ticket": 12345}),
    ("order_delete", [], {"ticket": 12345}),
    ("history_deals_get", [], {"position": 987}),
]


def test_normalize_mt5_call_nunca_devolve_dict_kwargs_vazio():
    """O normalizador é a garantia da regressão 03/09: nenhuma chamada pode
    chegar ao módulo com um dict de kwargs vazio (o wrapper C deste build
    recusa `fn(*args, **{})` → None → phantom '?'). Para funções posicionais
    devolve SEMPRE call_kwargs=None; kwargs vêm fundidos em args por nome."""
    # order_send com kwargs={} (o que o engine enviava) → só args
    args, kwargs = normalize_mt5_call("order_send",
                                      [{"action": 1, "symbol": "XAUUSD"}], {})
    assert kwargs is None and len(args) == 1
    # kwargs-only com dict vazio → kwargs None também (fn() sem dict)
    args, kwargs = normalize_mt5_call("positions_get", [], {})
    assert kwargs is None and args == ()
    # kwargs-only com kwargs reais → dict presente (positions_get(symbol=...))
    args, kwargs = normalize_mt5_call("positions_get", [], {"symbol": "XAUUSD"})
    assert kwargs == {"symbol": "XAUUSD"} and args == ()
    # kwargs numa função posicional fundem por NOME (order_send(request=...))
    req = {"action": 1, "symbol": "XAUUSD"}
    args, kwargs = normalize_mt5_call("order_send", [], {"request": req})
    assert kwargs is None and args == (req,)


def test_normalize_mt5_call_recusa_convencao_errada_antes_do_modulo():
    """Formas que não casam com a convenção real do módulo são recusadas com
    ValueError — nunca chegam ao C-core (regressão '?' silenciosa)."""
    # posicionais numa função kwargs-only
    with pytest.raises(ValueError, match="não aceita argumentos posicionais"):
        normalize_mt5_call("positions_get", ["XAUUSD"], {})
    # kwargs desconhecidos numa função posicional
    with pytest.raises(ValueError, match="kwargs inválidos"):
        normalize_mt5_call("order_send", [], {"symbol": "XAUUSD"})
    # faltam argumentos obrigatórios (order_send sem request)
    with pytest.raises(ValueError, match="faltam argumentos obrigatórios"):
        normalize_mt5_call("order_send", [], {})
    # demasiados posicionais
    with pytest.raises(ValueError, match="demasiados argumentos posicionais"):
        normalize_mt5_call("symbol_select", ["XAUUSD", True, "extra"], {})
    # fora da tabela
    with pytest.raises(KeyError):
        normalize_mt5_call("shutdown", [], {})


def test_marshal_replay_all_engine_call_shapes(worker_server):
    """REGRESSÃO: repete TODAS as formas de chamada que os engines usam pelo
    caminho remoto (RemoteMT5Module → HTTP → worker → módulo) e verifica que
    o módulo (fake que imita a convenção real do C-core) as recebe sempre
    com a convenção certa — posicionais sem kwargs, kwargs-only com kwargs —
    e que nenhuma forma recusada chega a executar o módulo."""
    _, prov, url = worker_server
    client = RemoteProvider(base_url=url, token="test-token")
    assert client.initialize() is True
    calls = []
    for name in MT5_CALL_SHAPES:
        fn = getattr(prov._mt5, name, None)
        if not callable(fn):
            continue
        def wrap(*a, _fn=fn, _n=name, **k):  # captura _fn por default (não late-bind)
            calls.append((_n, list(a), dict(k)))
            return _fn(*a, **k)
        setattr(prov._mt5, name, wrap)

    for name, args, kwargs in ENGINE_CALL_SHAPES:
        fn = getattr(client._mt5, name)
        result = fn(*args, **kwargs)  # mesma convenção do bytecode do engine
        assert result is not None or name in ("history_deals_get",), name

    for name, args, kwargs in ENGINE_CALL_SHAPES:
        got = [c for c in calls if c[0] == name]
        assert got, f"{name} nunca chegou ao módulo"
        shape = MT5_CALL_SHAPES[name]
        if shape["kind"] == "positional":
            # posicionais: kwargs nunca presentes; args quando a chamada os exige
            assert all(not c[2] for c in got), f"{name} recebeu kwargs"
            if shape["min"] >= 1:
                assert got[-1][1], f"{name} sem args"
        else:
            # kwargs-only: nunca posicionais; kwargs quando enviados
            assert all(not c[1] for c in got), f"{name} recebeu posicionais"
            if kwargs:
                assert got[-1][2] == kwargs, f"{name} kwargs divergem"

    # formas recusadas: o módulo NÃO é chamado (contagem não cresce)
    before = len(calls)
    r = httpx.post(f"{url}/mt5/call", headers=_auth_headers(), timeout=10,
                   json={"name": "positions_get", "args": ["XAUUSD"],
                         "kwargs": {}})
    assert r.status_code == 503 or "posicionais" in r.json()["error"]
    assert len(calls) == before


# ------------------------------------------------------------- auto-sonda

def _probe_requests(prov):
    """Requests de order_send recebidos pelo módulo fake."""
    return [args[0] for name, args, _ in prov._mt5.received
            if name == "order_send"]


def test_worker_self_probe_ok_no_boot(worker_server):
    """Boot: depois do /initialize a sonda corre e o /status diz ok — o
    símbolo inválido é recusado com retcode normal (módulo vivo)."""
    _, prov, url = worker_server
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    probe = st.get("probe") or {}
    assert probe.get("ok") is True, st
    assert "símbolo inválido recusado" in (probe.get("detail") or "")
    sent_symbols = [r.get("symbol") for r in _probe_requests(prov)]
    assert sent_symbols.count("__LANE2_PROBE__") >= 1
    # uma sonda por episódio — não spamma ordens de teste no boot
    assert sent_symbols.count("__LANE2_PROBE__") == 1


def test_worker_self_probe_falha_expoe_erro_real_do_modulo(worker_server):
    """Módulo morto (order_send → None): a sonda falha no boot e o erro REAL
    do módulo (last_error na mesma thread) fica no /status — o '?' das ordens
    reais passa a ser apanhado no arranque, com o motivo."""
    _, prov, url = worker_server
    prov._mt5.order_send = lambda request: None
    prov._mt5.last_error = lambda: (-10005, "IPC timeout")
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    probe = st.get("probe") or {}
    assert probe.get("ok") is False
    assert "IPC timeout" in (probe.get("error") or "")
    assert "self-probe" in (st.get("last_error") or "")
    assert "order_send devolveu None" in (probe.get("detail") or "")


def test_worker_self_probe_re_corre_no_reconnect(worker_server):
    """Reconnect: a sonda volta a correr UMA vez por episódio de ligação (o
    /initialize chama _ensure_mt5 → connected=True → probe_due)."""
    wk, prov, url = worker_server
    h = _auth_headers()
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    httpx.get(f"{url}/status", headers=h, timeout=10)
    first_ts = (httpx.get(f"{url}/status", headers=h, timeout=10)
                .json()["probe"]["ts"])
    probes_after_boot = sum(1 for r in _probe_requests(prov)
                            if r.get("symbol") == "__LANE2_PROBE__")
    assert probes_after_boot == 1

    # cai e religa: probe_due volta a marcar e o próximo status sonda de novo
    prov.connected = False
    wk._last_init = 0.0  # ignora o throttle de 10s do reconnect
    httpx.post(f"{url}/initialize", headers=h, timeout=10)
    st = httpx.get(f"{url}/status", headers=h, timeout=10).json()
    assert st["connected"] is True
    assert (st["probe"]["ts"] > first_ts)
    assert sum(1 for r in _probe_requests(prov)
               if r.get("symbol") == "__LANE2_PROBE__") == 2


def test_remote_provider_espelha_sonda(worker_server):
    """O RemoteProvider do servidor vê a sonda no status_summary (o alerta do
    market_loop lê daqui)."""
    _, prov, url = worker_server
    prov._mt5.order_send = lambda request: None
    prov._mt5.last_error = lambda: (-2, "Unnamed arguments not allowed")
    client = RemoteProvider(base_url=url, token="test-token")
    client.initialize()
    s = client.status_summary()
    assert s["probe"]["ok"] is False
    assert "Unnamed arguments not allowed" in s["probe"]["error"]


# --------------------------------------------------------- smoke test (0.01)

class _BrokerProvider(FakeProvider):
    """FakeProvider do worker cujo positions() lê o módulo (como o provider
    real) — para a leitura de ticket/leftover do smoke report."""

    def positions(self, cache_seconds=2.0):
        return self._mt5.positions_get() or []


class _LiveBroker:
    """Broker fake ESTADO (abre/fecha posições + deals) com a convenção real
    do módulo: posicionais sem kwargs e kwargs-only sem posicionais."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self, ask=1.08423, bid=1.08413):
        self.ask = ask
        self.bid = bid
        self._positions = []
        self._deals = {}
        self._next_ticket = 2001

    # -- API do módulo (convenção real: posicionais / kwargs) --
    def symbol_select(self, *a, **k):
        return True

    def symbol_info_tick(self, sym):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def symbol_info(self, sym):
        return SimpleNamespace(trade_contract_size=100000.0, digits=5,
                               volume_min=0.01, volume_max=500.0,
                               volume_step=0.01, trade_tick_size=0.00001)

    def account_info(self):
        return SimpleNamespace(login=5055369549, server="MetaQuotes-Demo",
                               currency="EUR", balance=9995.96, equity=9995.96,
                               leverage=100, trade_mode=0, margin_mode=2)

    def terminal_info(self):
        return SimpleNamespace(name="FakePortable2", build=999, trade_allowed=True)

    def last_error(self):
        return (0, "no error")

    def orders_get(self, ticket=None, **k):
        return []

    def positions_get(self, symbol=None, **k):
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if p.symbol == symbol]

    def history_deals_get(self, position=None, **k):
        return list(self._deals.get(position, []))

    def order_send(self, request):
        if request.get("action") == self.TRADE_ACTION_DEAL:
            if request.get("position"):
                return self._close(request)
            return self._open(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="Done")

    def _open(self, req):
        # como o broker real, símbolos desconhecidos (ex.: a sonda) são
        # recusados com retcode de erro — nunca criam posição
        if req.get("symbol") not in ("EURUSD", "XAUUSD"):
            return SimpleNamespace(retcode=10014, comment="Unknown symbol")
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions.append(SimpleNamespace(
            ticket=ticket, symbol=req["symbol"], type=req["type"],
            volume=req["volume"], price_open=req["price"],
            sl=req.get("sl", 0.0), tp=req.get("tp", 0.0),
            comment=req.get("comment", ""), profit=0.0,
            price_current=self.ask))
        self._deals[ticket] = [SimpleNamespace(
            ticket=ticket, price=req["price"], profit=0.0,
            entry=self.DEAL_ENTRY_IN)]
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="Done")

    def _close(self, req):
        ticket = req["position"]
        pos = next((p for p in self._positions if p.ticket == ticket), None)
        if pos is None:
            return SimpleNamespace(retcode=10016, comment="Invalid position")
        price = float(req.get("price", self.ask))
        sign = 1.0 if pos.type == self.POSITION_TYPE_BUY else -1.0
        profit = round(sign * (price - pos.price_open) * pos.volume * 100000.0, 2)
        self._positions.remove(pos)
        self._deals[ticket].append(SimpleNamespace(
            ticket=ticket, price=price, profit=profit, entry=self.DEAL_ENTRY_OUT))
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="Done",
                               price=price)


def _live_broker_provider():
    prov = _BrokerProvider()
    prov._mt5 = _LiveBroker()
    return prov


@pytest.fixture()
def smoke_worker_server():
    prov = _live_broker_provider()
    wk = Lane2Worker(prov, port=0, token="test-token")
    wk.bind()
    url = f"http://127.0.0.1:{wk.port}"
    thread = threading.Thread(target=wk.serve_forever, daemon=True)
    thread.start()
    yield wk, prov, url
    wk.shutdown()
    thread.join(timeout=5)


class _FakeEngine:
    """Engine mínimo para os testes de guarda (nunca envia ordens)."""

    def __init__(self, *, provider=None, dry_run=False):
        self.provider = provider
        self.dry_run = dry_run


class _FakeSmokeProvider:
    def __init__(self, *, connected=True, trade_allowed=True, block="",
                 trade_mode=0):
        self._connected = connected
        self._trade_allowed = trade_allowed
        self._block = block
        self._trade_mode = trade_mode
        self._acct = {"login": 5055369549, "server": "MetaQuotes-Demo",
                      "currency": "EUR", "balance": 10000.0,
                      "equity": 10000.0, "trade_mode": trade_mode}

    @property
    def connected(self):
        return self._connected

    @property
    def trade_allowed(self):
        return self._trade_allowed

    @property
    def execution_block_reason(self):
        return self._block

    def account(self):
        return self._acct


def test_smoke_guard_recusa_conta_real_e_demo_ok(monkeypatch):
    """GUARDA demo-only: conta real (trade_mode 2) recusa SEMPRE; demo (0)
    passa com o resto ligado. Sem opt-in também recusa."""
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "0")
    eng = _FakeEngine(provider=_FakeSmokeProvider(trade_mode=0))
    assert "desligado" in (smoke_guard_error(eng, None) or "")

    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    eng_real = _FakeEngine(provider=_FakeSmokeProvider(trade_mode=2))
    err = smoke_guard_error(eng_real, None)
    assert err and "NÃO é demo" in err and "trade_mode=2" in err

    # trade_mode desconhecido também recusa (nunca arrisca)
    eng_unk = _FakeEngine(provider=_FakeSmokeProvider(trade_mode=None))
    assert "trade_mode desconhecido" in (smoke_guard_error(eng_unk, None) or "")

    assert smoke_guard_error(
        _FakeEngine(provider=_FakeSmokeProvider(trade_mode=0)), None) is None


def test_smoke_guard_recusa_engine_nao_real_offline_e_at_off(monkeypatch):
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    # dry-run / sem engine
    assert "dry-run" in (smoke_guard_error(
        _FakeEngine(provider=None, dry_run=True), None) or "")
    assert "sem engine" in (smoke_guard_error(None, None) or "")
    # worker offline
    err = smoke_guard_error(_FakeEngine(
        provider=_FakeSmokeProvider(connected=False)), None)
    assert err and "sem ligação" in err
    # AutoTrading OFF
    err = smoke_guard_error(_FakeEngine(
        provider=_FakeSmokeProvider(trade_allowed=False)), None)
    assert err and "AutoTrading OFF" in err
    # lane bloqueada (conta não hedged etc.)
    err = smoke_guard_error(_FakeEngine(
        provider=_FakeSmokeProvider(block="conta netting")), None)
    assert err and "bloqueada" in err


def test_smoke_guard_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    monkeypatch.setenv("LANE2_SMOKE_COOLDOWN", "600")
    db = DB(str(tmp_path / "cooldown.db"))
    try:
        eng = _FakeEngine(provider=_FakeSmokeProvider(trade_mode=0))
        assert smoke_guard_error(eng, db) is None  # nunca correu
        db.set_config("lane2_smoke_last_ts", time.time())
        err = smoke_guard_error(eng, db)
        assert err and "cooldown" in err
        # cooldown 0 desliga o throttling
        monkeypatch.setenv("LANE2_SMOKE_COOLDOWN", "0")
        assert smoke_guard_error(eng, db) is None
    finally:
        db.close()


def test_smoke_round_trip_dual_lane(smoke_worker_server, monkeypatch):
    """E2E do smoke DUAL: engine da main em processo (broker fake) + worker
    real + RemoteProvider + MT5Engine(lane tp2, dry_run=False).
    `run_smoke` abre 1 perna de 0.01 em CADA lane com o MESMO sinal, fecha
    as duas a mercado, escreve as rows (trade/posição fechadas, exit_reason
    smoke_test, strategy smoke_test, lane certa) e devolve os eventos
    close + trade_closed por lane."""
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    _, prov, url = smoke_worker_server
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        main_prov = _live_broker_provider()
        assert main_prov.initialize() is True
        main_eng = MT5Engine(db, main_prov, market=None, dry_run=False,
                             lane_id="main")
        client = RemoteProvider(base_url=url, token="test-token")
        assert client.initialize() is True
        tp2_eng = MT5Engine(db, client, market=None, dry_run=False,
                            lane_id="tp2")
        report = run_smoke({"main": main_eng, "tp2": tp2_eng}, db)

        assert report["ok"], report
        assert report["symbol"] == "XAUUSD"  # 1º da preferência com tick nas 2
        assert report["lots"] == 0.01
        assert len(report["lanes"]) == 2
        by_lane = {ln["lane_id"]: ln for ln in report["lanes"]}
        assert set(by_lane) == {"main", "tp2"}
        # as DUAS trades partilham o sinal (como um sinal real do grupo)
        assert by_lane["main"]["signal_id"] == by_lane["tp2"]["signal_id"]
        assert by_lane["main"]["trade_id"] != by_lane["tp2"]["trade_id"]
        assert report["total_pnl"] == 0.0  # broker fake fecha ao mesmo preço

        for lane_id in ("main", "tp2"):
            ln = by_lane[lane_id]
            assert ln["leftover_open"] is False
            t = db.get_trade(ln["trade_id"])
            assert t is not None and t.status == "closed"
            assert t.exit_reason == "smoke_test"
            assert t.strategy_id == "smoke_test"
            assert (t.lane_id or "main") == lane_id
            assert t.execution_mode == "mt5-smoke"
            positions = db.positions_for_trade(t.id)
            assert len(positions) == 1 and positions[0].status == "closed"
            assert positions[0].exit_reason == "smoke_test"
            assert positions[0].lots == 0.01
            types = [ev["type"] for ev in ln["events"]]
            assert "close" in types and "trade_closed" in types
            closed_ev = next(ev for ev in ln["events"]
                             if ev["type"] == "trade_closed")
            assert closed_ev["trade_id"] == ln["trade_id"]

        # nada ficou aberto nos brokers nem na BD
        assert main_prov.positions() == []
        assert client.positions() == []
        assert db.open_trades() == []
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_smoke_rollback_se_falha_na_2a_lane(smoke_worker_server, monkeypatch):
    """Se a 2ª lane falhar a ABRIR (broker recusa a ordem), a main que já
    abriu é FECHADA logo (rollback) e o report diz o que aconteceu — nunca
    deixa posição órfã."""
    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    _, prov, url = smoke_worker_server
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        main_prov = _live_broker_provider()
        assert main_prov.initialize() is True
        main_eng = MT5Engine(db, main_prov, market=None, dry_run=False,
                             lane_id="main")
        client = RemoteProvider(base_url=url, token="test-token")
        assert client.initialize() is True
        tp2_eng = MT5Engine(db, client, market=None, dry_run=False,
                            lane_id="tp2")
        # guardas passam (demo/AT ON) mas o broker da lane 2 RECUSA a ordem
        # de abertura (10030) — como um símbolo não negociável ao vivo
        worker_broker = prov._mt5
        orig_send = worker_broker.order_send

        def refuse(req):
            if req.get("action") == worker_broker.TRADE_ACTION_DEAL \
                    and not req.get("position"):
                return SimpleNamespace(retcode=10030, comment="trade disabled")
            return orig_send(req)

        worker_broker.order_send = refuse
        try:
            report = run_smoke({"main": main_eng, "tp2": tp2_eng}, db)
        finally:
            worker_broker.order_send = orig_send
        assert report["ok"] is False
        assert report["failed_lane"] == "tp2"
        # a main abriu e foi fechada no rollback — nada ficou órfão
        assert len(report["rolled_back"]) == 1
        rb = report["rolled_back"][0]
        assert rb["lane_id"] == "main" and rb["leftover_open"] is False
        assert main_prov.positions() == []
        assert client.positions() == []
        assert db.open_trades() == []
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_smoke_notifica_whatsapp_dual_lane(smoke_worker_server, monkeypatch):
    """O smoke dispara a notificação WhatsApp dual-lane (mesma função do
    tick): UMA mensagem com as DUAS contas e as DUAS trades reais do mesmo
    sinal, lado a lado, com o P&L total somado."""
    from app.smoke import notify_smoke

    class _FakeWa:
        configured = True
        dashboard_url = "https://n3xg3n.duckdns.org"

        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)
            return True

    monkeypatch.setenv("LANE2_SMOKE_ALLOW", "1")
    _, prov, url = smoke_worker_server
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    try:
        main_prov = _live_broker_provider()
        assert main_prov.initialize() is True
        main_eng = MT5Engine(db, main_prov, market=None, dry_run=False,
                             lane_id="main")
        client = RemoteProvider(base_url=url, token="test-token")
        assert client.initialize() is True
        sim2 = MT5Engine(db, client, market=None, dry_run=False, lane_id="tp2")
        report = run_smoke({"main": main_eng, "tp2": sim2}, db)
        assert report["ok"], report

        wa = _FakeWa()
        state = BotState(
            db=db, market=MarketSimulator(seed=1), risk=RiskEngine(),
            sim=main_eng, notifier=Notifier(db, WSManager()),
            ws=WSManager(), whatsapp=wa, second_sim=sim2,
            second_strategy="tp2_protect",
        )
        asyncio.run(notify_smoke(state, report))
        # 16/09: o WhatsApp publica UMA mensagem, só da lane tp2 (a main não
        # envia) — formato single-lane.
        assert len(wa.sent) == 1
        msg = wa.sent[0]
        assert "resumo das DUAS contas" not in msg
        assert "Conta principal" not in msg
        assert "smoke_test · conta 5055369549" in msg  # rótulo da 2ª conta
        assert "📦 XAUUSD fechada" in msg
        # feed também recebeu os fechos (notificações na BD do state)
        notifs = db.list_notifications(100)
        assert any("Smoke-test" in (n.text or "") for n in notifs)
        assert {n.lane for n in notifs if (n.text or "") and "Smoke-test" in n.text} \
            == {"main", "tp2"}
    finally:
        db.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_smoke_report_lines():
    ok = {"ok": True, "symbol": "XAUUSD", "lots": 0.01, "signal_id": "s-1",
          "total_pnl": -0.31, "lanes": [
              {"lane_id": "main", "login": 5054644473,
               "trade_id": "t-1", "ticket": 2001, "entry": 4492.7,
               "pnl": -0.15, "leftover_open": False, "events": []},
              {"lane_id": "tp2", "login": 5055369549,
               "trade_id": "t-1-2", "ticket": 5830, "entry": 4492.8,
               "pnl": -0.16, "leftover_open": False, "events": []},
          ]}
    lines = smoke_report_lines(ok)
    assert any("Smoke-test OK" in l for l in lines)
    assert any("[main]" in l and "P&L -0.15" in l for l in lines)
    assert any("[tp2]" in l and "P&L -0.16" in l for l in lines)
    assert smoke_report_lines({"ok": False, "error": "x"})[0].startswith("❌")
