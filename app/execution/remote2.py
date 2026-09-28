"""Cliente do worker da lane 2 — `RemoteProvider`, mesma superfície duck-typed
do `MT5Provider` (app/execution/mt5.py).

O processo principal mantém TODAS as decisões e a BD; este provider faz proxy
das operações para o worker (`app/lane2_worker.py`, processo isolado dono da
ligação MetaTrader5 à 2ª conta). O `MT5Engine` usa `provider._mt5` diretamente
(módulo MetaTrader5 bruto) — por isso o `RemoteProvider` expõe também um
`_RemoteMT5Module` que serializa chamadas/constantes para o worker.

Fail-closed:
  * worker offline ⇒ `connected=False` e `execution_block_reason` a explicar
    — o `MT5Engine` real recusa abrir ordens (`open_trade` levanta RuntimeError);
  * block_reason do broker (conta não hedged, etc.) é espelhado do worker;
  * nunca existe fallback para partilhar a ligação da conta principal.
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from app.execution.mt5 import load_symbol_map


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


WORKER_TIMEOUT = _env_float("LANE2_WORKER_TIMEOUT", 10.0)
_STATUS_TTL = 1.0  # segundos de cache do /status (o engine consulta connected~1/s)

# Constantes do módulo MetaTrader5 usadas pelo engine antes do 1º /constants
# (valores estáveis da API MT5). O /constants do worker sobrepõe-se quando
# disponível — evita um round-trip extra por request e cobre testes sem worker.
MT5_CONSTANT_FALLBACK: Dict[str, int] = {
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2,
    "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_TYPE_BUY_STOP": 4,
    "ORDER_TYPE_SELL_STOP": 5,
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_SLTP": 6,
    "ORDER_TIME_GTC": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    "ORDER_STATE_FILLED": 4,
    "ORDER_STATE_CANCELED": 6,
    "ORDER_STATE_EXPIRED": 7,
    "ORDER_STATE_REJECTED": 8,
    "POSITION_TYPE_BUY": 0,
    "TRADE_RETCODE_DONE": 10009,
}


class WorkerUnreachable(RuntimeError):
    """O worker da lane 2 não respondeu (processo em baixo / porta fechada)."""


class AttrDict(dict):
    """dict com acesso por atributo — reconstrói os namedtuples do MT5
    (result.retcode, p.ticket, order.state, ...) a partir do JSON do worker."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _from_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return AttrDict({k: _from_json(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_from_json(x) for x in obj]
    return obj


class RemoteMT5Module:
    """Proxy do módulo MetaTrader5 (`provider._mt5`) que o MT5Engine usa.

    Atributos de constantes (ORDER_TYPE_BUY, TRADE_RETCODE_DONE, ...) devolvem
    ints (fetch ao worker na 1ª resolução, fallback local); restantes atributos
    devolvem um callable que POSTa `/mt5/call` ao worker.
    """

    def __init__(self, client: "RemoteProvider"):
        self._client = client
        self._consts: Dict[str, int] = dict(MT5_CONSTANT_FALLBACK)
        self._consts_loaded = False

    def _load_constants(self) -> None:
        if self._consts_loaded:
            return
        self._consts_loaded = True
        try:
            data = self._client._get("/constants")
            fetched = data.get("constants") or {}
            for k, v in fetched.items():
                if isinstance(v, int):
                    self._consts[k] = v
        except Exception:
            pass  # fallback local basta até o worker voltar

    def __getattr__(self, name: str) -> Any:
        if name in MT5_CONSTANT_FALLBACK:
            try:
                self._load_constants()
            except Exception:
                pass
            return self._consts.get(name, MT5_CONSTANT_FALLBACK[name])
        if name.startswith("_"):
            raise AttributeError(name)
        return _RemoteCall(self._client, name)


class _RemoteCall:
    def __init__(self, client: "RemoteProvider", name: str):
        self._client = client
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._client._mt5_call(self._name, list(args), kwargs)


class RemoteProvider:
    """Duck-type do MT5Provider que fala com o worker da lane 2."""

    is_remote = True  # distinguir do provider local (diagnóstico/reconnect)

    def __init__(self, base_url: str = "", token: str = "",
                 port: Optional[int] = None, timeout: float = WORKER_TIMEOUT,
                 symbols: Optional[Dict[str, Dict]] = None):
        if port is not None:
            base_url = base_url or f"http://127.0.0.1:{port}"
        self._base_url = (base_url or f"http://127.0.0.1:8770").rstrip("/")
        self._token = token or ""
        self._timeout = timeout
        self._symbols = symbols if symbols is not None else load_symbol_map()
        self._meta_cache: Dict[str, Dict] = {}
        self._status_cache: Optional[dict] = None
        self._status_ts = 0.0
        self._positions_cache: Optional[list] = None
        self._positions_ts = 0.0
        self._account_cache: Optional[dict] = None
        self._account_ts = 0.0
        self._execution_block_reason = ""
        self._account_margin_mode = None
        self._account_hedged: Optional[bool] = None
        self._trade_allowed = False
        self._last_sync: Optional[str] = None
        self._last_error = ""
        self._offline_reason = ""
        self._reachable = True
        self._probe: dict = {}
        self._last_probe_ts = 0.0
        self._mt5 = RemoteMT5Module(self)
        self._now_iso = lambda: datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------ http

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["x-api-token"] = self._token
        return h

    def _http(self) -> httpx.Client:
        return httpx.Client(base_url=self._base_url, timeout=self._timeout,
                            headers=self._headers())

    def _get(self, path: str) -> dict:
        try:
            with self._http() as c:
                r = c.get(path)
                if r.status_code != 200:
                    raise WorkerUnreachable(
                        f"worker {path} → HTTP {r.status_code}: {r.text[:160]}")
                return r.json()
        except WorkerUnreachable:
            raise
        except Exception as exc:
            raise WorkerUnreachable(f"worker lane 2 offline: {exc}") from exc

    def _post(self, path: str, payload: Optional[dict] = None) -> dict:
        try:
            with self._http() as c:
                r = c.post(path, json=payload or {})
                if r.status_code != 200:
                    raise WorkerUnreachable(
                        f"worker {path} → HTTP {r.status_code}: {r.text[:160]}")
                return r.json()
        except WorkerUnreachable:
            raise
        except Exception as exc:
            raise WorkerUnreachable(f"worker lane 2 offline: {exc}") from exc

    # ------------------------------------------------------------ estado

    def _mirror_status(self, st: dict) -> None:
        """Espelha o estado do worker (conta, block_reason, hedged).

        `execution_block_reason` só espelha o bloqueio PERMANENTE do broker
        (conta não hedged, etc.) — "worker offline" é transitório e o loop de
        reconnect tem de continuar a tentar (fica em `_offline_reason` para o
        diagnóstico; o fail-closed de ordens usa `connected`)."""
        self._reachable = True
        self._account_margin_mode = st.get("margin_mode")
        self._account_hedged = st.get("hedged")
        self._trade_allowed = bool(st.get("trade_allowed"))
        # auto-sonda do worker (boot/reconnect) — o servidor alerta se falhar
        probe = st.get("probe")
        if isinstance(probe, dict):
            self._probe = probe
        else:
            self._probe = {}
        self._last_probe_ts = self._probe.get("ts", 0.0)
        broker_block = st.get("block_reason") or ""
        self._execution_block_reason = broker_block
        if st.get("connected"):
            self._offline_reason = ""
        else:
            self._trade_allowed = False
        self._last_sync = self._now_iso()
        err = st.get("last_error") or ""
        if err:
            self._last_error = err

    def _status(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._status_cache is not None \
                and now - self._status_ts < _STATUS_TTL:
            return self._status_cache
        try:
            st = self._get("/status")
            self._status_cache = st
            self._status_ts = now
            self._mirror_status(st)
            return st
        except WorkerUnreachable as exc:
            self._reachable = False
            self._offline_reason = str(exc)
            self._last_error = str(exc)
            self._status_cache = {"connected": False,
                                  "block_reason": self._execution_block_reason}
            self._status_ts = now
            return self._status_cache

    def refresh_status(self) -> dict:
        return self._status(force=True)

    @property
    def execution_block_reason(self) -> str:
        return self._execution_block_reason

    @property
    def account_margin_mode(self):
        return self._account_margin_mode

    @property
    def account_hedged(self) -> Optional[bool]:
        return self._account_hedged

    @property
    def trade_allowed(self) -> bool:
        """AutoTrading ON no terminal portátil 2 (espelho do worker)."""
        return self._trade_allowed and bool(self._status().get("connected"))

    @property
    def connected(self) -> bool:
        return bool(self._status().get("connected"))

    def ping(self, timeout: Optional[float] = None) -> bool:
        """Ping barato ao worker (default 2s — o watch do market_loop é
        bloqueante e um worker encravado não pode travar o loop)."""
        if timeout is None:
            timeout = 2.0
        try:
            with httpx.Client(base_url=self._base_url, timeout=timeout,
                              headers=self._headers()) as c:
                r = c.get("/ping")
                return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------ provider

    def initialize(self) -> bool:
        try:
            data = self._post("/initialize")
            st = {k: data.get(k) for k in
                  ("connected", "block_reason", "margin_mode", "hedged",
                   "login", "account", "last_error", "trade_allowed")}
            st["connected"] = bool(data.get("connected"))
            self._mirror_status(st)
            if not data.get("connected"):
                # TRANSITÓRIO: worker/terminal ainda não ligou. O motivo fica
                # em `_offline_reason` — NÃO poluir `execution_block_reason`,
                # que é só para bloqueios PERMANENTES (conta não hedged, etc.):
                # um reason truthy fazia o reconnect do market_loop saltar a
                # lane para sempre (a tp2 falhava no boot e nunca mais tentava).
                self._offline_reason = data.get("error") or "worker não ligado"
            return bool(data.get("connected"))
        except WorkerUnreachable as exc:
            self._reachable = False
            self._offline_reason = str(exc)
            self._last_error = str(exc)
            return False

    def shutdown(self) -> None:
        # O worker é supervisionado fora do processo — nada a fechar aqui.
        pass

    def ensure_symbol(self, app_symbol: str) -> None:
        if not self.connected:
            return
        try:
            self._post("/symbol_select", {"symbol": app_symbol})
        except WorkerUnreachable:
            pass

    def meta(self, app_symbol: str) -> Optional[Dict]:
        if app_symbol not in self._symbols:
            return None
        if app_symbol in self._meta_cache:
            return self._meta_cache[app_symbol]
        base = dict(self._symbols[app_symbol])
        try:
            data = self._get(f"/meta?sym={app_symbol}")
            m = data.get("meta")
            if isinstance(m, dict):
                base.update(m)
        except WorkerUnreachable:
            pass  # fallback do mapa estático (dry-run/falha do worker)
        self._meta_cache[app_symbol] = base
        return base

    def quote(self, app_symbol: str) -> Optional[dict]:
        if not self.connected:
            return None
        try:
            data = self._get(f"/quote?sym={app_symbol}")
            return data.get("quote")
        except WorkerUnreachable:
            return None

    def tick(self, app_symbol: str) -> Optional[float]:
        q = self.quote(app_symbol)
        if not q:
            return None
        digits = int((q.get("digits") or 0) if isinstance(q.get("digits"), (int, float)) else 0)
        return round((float(q["bid"]) + float(q["ask"])) / 2.0, digits or 2)

    def copy_rates(self, app_symbol: str, timeframe: int = 1,
                   count: int = 100) -> list:
        if not self.connected:
            return []
        try:
            data = self._get(
                f"/copy_rates?sym={app_symbol}&tf={timeframe}&count={count}")
            return data.get("rates") or []
        except WorkerUnreachable:
            return []

    def positions(self, cache_seconds: float = 2.0) -> list:
        """Posições do broker, com cache TTL (mesma semântica do
        `MT5Provider` local). O snapshot constrói as lanes várias vezes por
        tick e cada HTTP ao worker custa 15–160ms — sem cache, uma
        iteração da lane 2 faz ~8 round-trips e congela o loop."""
        now = time.monotonic()
        if (self._positions_cache is not None
                and now - self._positions_ts < cache_seconds):
            return self._positions_cache
        if not self.connected:
            return self._positions_cache or []
        try:
            data = self._get("/positions")
            self._positions_cache = data.get("positions") or []
            self._positions_ts = time.monotonic()
            return self._positions_cache
        except WorkerUnreachable:
            return self._positions_cache or []

    def account(self, cache_seconds: float = 5.0) -> Optional[dict]:
        """Conta do broker, com cache TTL (mesma semântica do `MT5Provider`
        local) — o `_build_snapshot` consulta a conta 5–8 vezes por lane por
        tick e cada HTTP ao worker custa 15–160ms."""
        now = time.monotonic()
        if self._account_cache is not None \
                and now - self._account_ts < cache_seconds:
            return self._account_cache
        if not self.connected:
            return self._account_cache
        try:
            data = self._get("/account")
        except WorkerUnreachable:
            return self._account_cache
        acct = data.get("account")
        if not acct:
            return self._account_cache
        self._account_cache = acct
        self._account_ts = time.monotonic()
        return acct

    # --------------------------------------------- módulo MT5 (engine)

    def _mt5_call(self, name: str, args: List[Any],
                  kwargs: Dict[str, Any]) -> Any:
        try:
            data = self._post("/mt5/call", {"name": name, "args": args,
                                            "kwargs": kwargs})
        except WorkerUnreachable as exc:
            self._reachable = False
            self._offline_reason = str(exc)
            self._last_error = str(exc)
            raise
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or f"{name} recusada pelo worker")
        self._last_error = ""
        self._last_sync = self._now_iso()
        return _from_json(data.get("result"))

    # ------------------------------------------------------- diagnóstico

    def status_summary(self) -> dict:
        # status com cache curto (≤1s) — o Diagnóstico faz polling a cada 10s
        # e o market_loop pinga o worker ~1×/s; nunca bloquear um endpoint
        # da dashboard com um timeout longo.
        st = self._status()
        return {
            "reachable": self._reachable,
            "connected": bool(st.get("connected")),
            "login": st.get("login"),
            "block_reason": self._execution_block_reason,
            "trade_allowed": self._trade_allowed,
            "at_off": bool(st.get("connected")) and not self._trade_allowed,
            "last_sync": self._last_sync,
            "last_error": self._last_error,
            "probe": self._probe,
        }