"""Lane-2 MT5 worker — dono exclusivo da ligação à 2ª conta MT5 (processo isolado).

A API MetaTrader5 mantém **UMA ligação global por processo** — a da conta
principal já está presa no processo do servidor. A execução real da 2ª conta
(env `MT5_2_*`) precisa por isso de um processo próprio que ligue ao segundo
terminal (instalação portable, `MT5_2_TERMINAL_PATH`) e exponha as operações
por HTTP local. Este módulo é esse processo:

  * dono do `MT5Provider` da lane 2 (`initialize`/`connected`/`account`/
    `positions`/`meta`/`quote`/`copy_rates` + o módulo MetaTrader5 bruto por
    `/mt5/call` — o `MT5Engine` do processo principal usa `provider._mt5`);
  * **nunca toca na SQLite nem decide nada** — é um shim de I/O; toda a
    decisão e gravação continuam no processo principal, preservando a
    invariante "todo o estado vive no event loop" e o lock da BD;
  * token no header (`x-api-token` ou `Authorization: Bearer`, valor de
    `LANE2_WORKER_TOKEN`, fallback `SIGNALBOT_API_TOKEN`);
  * logs rotativos `lane2_worker.log`/`lane2_worker.err.log` (mesma política
    do `server_runner` via `app.maintenance.configure_process_stdio`);
  * **fail-closed**: sem ligação (terminal ausente, AutoTrading off, conta
    netting, worker offline) fica bloqueado — nunca partilha a ligação da
    conta principal.

Arranque (após instalar o 2º terminal portable):
    LANE2_WORKER_PORT=8770 .venv/Scripts/python -m app.lane2_worker

O `start.ps1`/`boot.ps1` (idempotentes por porta) e o `healthcheck.py`
(reinício + alerta, Tarefa NexGen Health) supervisionam este processo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from app.execution.mt5 import MT5Provider, load_symbol_map
from app.maintenance import configure_process_stdio

# Convenção de invocação do módulo MetaTrader5 por chamada — o inventário
# exato que o MT5Engine usa via `provider._mt5`. O wrapper C DESTE BUILD
# (MetaTrader5 5.0.6090, cp314) distingue `fn(*args)` de `fn(*args, **{})`:
# funções POSICIONAIS recusam qualquer dict de kwargs (o `order_send` com
# kwargs={} devolve None com last_error (-2, "Unnamed arguments not allowed")
# → retcode null → phantom '?' na lane 2 desde 01/09; `symbol_info_tick`
# levanta "takes no keyword arguments"). O marshaling é por isso POR
# CONVENÇÃO: funções posicionais NUNCA recebem um dict de kwargs (vazio ou
# não — kwargs são fundidos em args posicionais pelo nome); as de kwargs só
# recebem kwargs. Qualquer forma que não case com a tabela é recusada ANTES
# de chegar ao módulo — nenhuma chamada de convenção errada volta a tocar no
# C-core.
MT5_CALL_SHAPES: Dict[str, dict] = {
    # --- posicionais: `fn(*args)` SEM kwargs (kwargs fundidos por nome) ---
    "order_send": {"kind": "positional", "names": ["request"], "min": 1},
    "symbol_info_tick": {"kind": "positional", "names": ["symbol"], "min": 1},
    "symbol_info": {"kind": "positional", "names": ["symbol"], "min": 1},
    "symbol_select": {"kind": "positional", "names": ["symbol", "visible"], "min": 1},
    "account_info": {"kind": "positional", "names": [], "min": 0},
    "terminal_info": {"kind": "positional", "names": [], "min": 0},
    "last_error": {"kind": "positional", "names": [], "min": 0},
    # --- kwargs: `fn(**kwargs)` (assinatura Python do pacote) ---
    "positions_get": {"kind": "keyword", "names": ["symbol", "group", "ticket"], "min": 0},
    "orders_get": {"kind": "keyword", "names": ["symbol", "group", "ticket"], "min": 0},
    "order_delete": {"kind": "keyword", "names": ["ticket"], "min": 0},
    "history_deals_get": {"kind": "keyword", "names": ["position", "from", "to", "ticket", "group"], "min": 0},
}

# Nomes aceites no `/mt5/call` (qualquer outro é recusado — um proxy genérico
# que aceita tudo seria uma porta de entrada de RCE).
MT5_CALL_ALLOWLIST = set(MT5_CALL_SHAPES)


def normalize_mt5_call(name: str, args, kwargs) -> tuple:
    """Normaliza (args, kwargs) de uma chamada remota para a convenção real
    do módulo MetaTrader5. Devolve `(call_args, call_kwargs)` com
    `call_kwargs=None` a significar "chama SEM kwargs" — nunca um dict vazio.

    Levanta ValueError para qualquer forma que não case com a tabela
    (`MT5_CALL_SHAPES`) — a convenção errada fica bloqueada AQUI, antes de
    tocar no C-core (regressão 03/09: o `order_send` com kwargs={} devolvia
    None → retcode null → phantom '?'; `symbol_info_tick` com kwargs
    levantava "takes no keyword arguments")."""
    shape = MT5_CALL_SHAPES[name]
    args = list(args or [])
    kwargs = dict(kwargs or {})
    if shape["kind"] == "positional":
        if len(args) > len(shape["names"]):
            raise ValueError(
                f"{name}: demasiados argumentos posicionais "
                f"({len(args)} > {len(shape['names'])})")
        extra = sorted(set(kwargs) - set(shape["names"]))
        if extra:
            raise ValueError(f"{name}: kwargs inválidos: {extra}")
        for key in shape["names"][len(args):]:
            if key in kwargs:
                args.append(kwargs[key])
        if len(args) < shape["min"]:
            raise ValueError(
                f"{name}: faltam argumentos obrigatórios "
                f"({len(args)} < {shape['min']})")
        return tuple(args), None
    # keyword — a API Python do pacote é kwargs-only (positions_get etc.)
    if args:
        raise ValueError(f"{name}: não aceita argumentos posicionais")
    return (), dict(kwargs) if kwargs else None

# Constantes do módulo MT5 que o engine lê como valores (não chamadas).
MT5_ENUM_NAMES = [
    "ORDER_TYPE_BUY", "ORDER_TYPE_SELL",
    "ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT",
    "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_SELL_STOP",
    "TRADE_ACTION_DEAL", "TRADE_ACTION_PENDING", "TRADE_ACTION_SLTP",
    "ORDER_TIME_GTC", "ORDER_FILLING_IOC", "ORDER_FILLING_RETURN",
    "ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED",
    "ORDER_STATE_REJECTED", "POSITION_TYPE_BUY", "TRADE_RETCODE_DONE",
]


def _jsonable(obj: Any) -> Any:
    """Converte resultados do MetaTrader5 em JSON puro.

    namedtuples (TradeResult, positions, orders, ticks) → dict/list; escalares
    numpy (int64/float64) → números Python; tudo o resto passa.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "_asdict"):  # namedtuple (TradeResult, Position, ...)
        return _jsonable(obj._asdict())
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    # numpy escalares/arrays (numpy 1.x/2.x)
    try:
        import numpy as np  # type: ignore
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            if obj.dtype.names:  # structured array (copy_rates_from_pos)
                return [_jsonable(dict(zip(obj.dtype.names, row))) for row in obj]
            return [_jsonable(float(x)) for x in obj.tolist()]
    except ImportError:
        pass
    # outros objetos com atributos (SimpleNamespace, dataclasses, ...) — os
    # namedtuples reais do MT5 caem no ramo _asdict acima; isto cobre os
    # fakes dos testes e qualquer resultado estruturado futuro
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        try:
            return {str(k): _jsonable(v) for k, v in vars(obj).items()}
        except TypeError:
            pass
    return str(obj)


class Lane2Worker:
    """Servidor HTTP do worker (loopback) sobre um MT5Provider real.

    Aceita um provider injetado nos testes (dependency injection) — o
    arranque de produção constrói o MT5Provider da lane 2 real.
    """

    def __init__(self, provider: MT5Provider, port: int = 8770,
                 host: str = "127.0.0.1", token: str = ""):
        self.provider = provider
        self.host = host
        self.port = port
        self.token = (token or "").strip()
        self._last_init = 0.0
        self._last_error = ""
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        self._mt5_pool: Optional[ThreadPoolExecutor] = None
        self._mt5_pool_lock = threading.Lock()
        # auto-diagnóstico (ver _run_probe): ok=None = ainda não sondado;
        # True/False depois de cada boot/reconnect. `_probe_due` marca que a
        # ligação acabou de (re)estabelecer e falta sondar o módulo.
        self._probe = {"ok": None, "ts": 0.0, "error": "", "detail": ""}
        self._probe_due = False

    # -------------------------------------------- thread única do módulo MT5

    def _mt5_run(self, fn: Any, timeout: float = 90.0) -> Any:
        """Executa `fn` na thread ÚNICA dedicada ao módulo MetaTrader5.

        O módulo MetaTrader5 (C-core) mantém UMA ligação global por processo
        e não é thread-safe — o processo principal só o usa a partir da
        thread do event loop, enquanto o worker (ThreadingHTTPServer = uma
        thread por pedido) chamava o módulo de threads diferentes. Todas as
        chamadas passam por aqui e correm sempre na MESMA thread (pool de 1
        worker), como na principal. (A causa do `order_send` devolver None
        na lane 2 NÃO era esta — era o kwargs={} vazio passado ao wrapper C;
        ver `_mt5_call_impl`. A serialização fica na mesma: evita corridas.)
        """
        with self._mt5_pool_lock:
            if self._mt5_pool is None:
                self._mt5_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="lane2-mt5")
            pool = self._mt5_pool
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except _FutTimeout:
            fut.cancel()
            raise TimeoutError(
                f"módulo MT5 sem resposta em {timeout:.0f}s") from None

    # ------------------------------------------------------------ ligação

    def _ensure_mt5(self) -> bool:
        """Liga (ou religa, com throttle) o MetaTrader5 ao 2º terminal."""
        was_connected = bool(self.provider.connected)
        if was_connected:
            return True
        if self.provider.execution_block_reason:
            return False
        now = time.monotonic()
        if now - self._last_init < 10.0:
            return False
        self._last_init = now
        try:
            ok = self.provider.initialize()
        except Exception as exc:  # a ligação nunca pode rebentar o worker
            self._last_error = f"initialize falhou: {exc}"
            ok = False
        if ok and not self.provider.connected:
            self._last_error = "initialize ok mas terminal não confirma"
            ok = False
        if not ok:
            self._last_error = (
                self.provider.execution_block_reason
                or self._last_error or "sem ligação ao terminal MT5"
            )
        elif not was_connected:
            # (re)ligação nova — a sonda corre UMA vez neste episódio (o
            # primeiro /status, /constants ou /mt5/call que passar aqui)
            self._probe_due = True
        return ok

    # -------------------------------------------------- auto-diagnóstico

    # Símbolo deliberadamente INVÁLIDO para a sonda: um order_send a um
    # símbolo que não existe devolve sempre um TradeResult com retcode de
    # erro (o pedido nem chega ao broker — custo zero). O ÚNICO caso em que
    # devolve None é o módulo/terminal morto (IPC timeout, wrapper partido)
    # com o erro real em last_error — exatamente o "retcode null → '?'" das
    # ordens reais, mas apanhado no boot em vez de numa phantom silenciosa.
    PROBE_SYMBOL = "__LANE2_PROBE__"

    def _maybe_probe(self) -> None:
        """Corre a sonda UMA vez por episódio de ligação, na thread do módulo.

        `_ensure_mt5` marca `_probe_due` quando (re)liga; a primeira chamada
        útil que passa por aqui (status/constants/mt5_call) executa-a antes
        de continuar. A sonda nunca rebenta o worker nem bloqueia pedidos
        seguintes (é um order_send local barato)."""
        if not self._probe_due or not self.provider.connected:
            return
        self._probe_due = False
        try:
            self._probe = self._run_probe()
        except Exception as exc:
            self._probe = {"ok": False, "ts": time.time(),
                           "error": f"sonda rebentou: {exc}", "detail": ""}
            self._last_error = self._probe["error"]
            print(f"[lane2_worker] AUTO-PROBE falhou: {exc}")

    def _run_probe(self) -> dict:
        """Sonda o módulo com um order_send de símbolo inválido (0.01 lote)."""
        mt5 = self.provider._mt5
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.PROBE_SYMBOL,
            "volume": 0.01,
            "type": mt5.ORDER_TYPE_BUY,
            "price": 0.0,
            "deviation": 10,
            "magic": 36450341,
            "comment": "lane2:selfprobe",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        now = time.time()
        # chamada SEM kwargs (convenção real do módulo — ver MT5_CALL_SHAPES)
        result = mt5.order_send(request)
        if result is not None:
            code = getattr(result, "retcode", None)
            self._last_error = ""
            return {"ok": True, "ts": now, "error": "",
                    "detail": "símbolo inválido recusado"
                    + (f" (retcode {code})" if code is not None else "")}
        try:
            code, text = mt5.last_error()
        except Exception:
            code, text = None, ""
        error = f"{code} {text}".strip() if code is not None else "sem erro do módulo"
        # módulo/terminal morto: as ordens reais dariam retcode null ('?')
        self._last_error = (
            f"self-probe falhou — módulo MT5 sem resposta a order_send: {error}")
        print(f"[lane2_worker] AUTO-PROBE: módulo MT5 NÃO responde a order_send "
              f"({error}) — as ordens reais dariam retcode null ('?'). "
              f"Terminal parado ou wrapper partido.")
        return {"ok": False, "ts": now, "error": error,
                "detail": "order_send devolveu None"}

    # ------------------------------------------------------------ estado

    def status(self) -> dict:
        return self._mt5_run(self._status_impl)

    def _status_impl(self) -> dict:
        # primeira chamada útil pós-(re)ligação: corre a sonda antes do resto
        self._maybe_probe()
        acct = None
        trade_allowed = False
        try:
            if self.provider.connected:
                acct = self.provider.account()
                ti = self.provider._mt5.terminal_info()
                trade_allowed = bool(getattr(ti, "trade_allowed", False))
        except Exception:
            acct = None
            trade_allowed = False
        return {
            "ok": True,
            "connected": bool(self.provider.connected),
            "block_reason": self.provider.execution_block_reason or "",
            "margin_mode": self.provider.account_margin_mode,
            "hedged": self.provider.account_hedged,
            "login": (acct or {}).get("login"),
            "account": acct,
            "trade_allowed": trade_allowed,
            "probe": self._probe,
            "last_error": self._last_error,
            "ts": time.time(),
        }

    def constants(self) -> dict:
        return self._mt5_run(self._constants_impl)

    def _constants_impl(self) -> dict:
        if not self._ensure_mt5():
            return {"ok": False, "error": self._last_error or "MT5 não ligado"}
        self._maybe_probe()
        mt5 = self.provider._mt5
        out = {}
        for name in MT5_ENUM_NAMES:
            try:
                out[name] = int(getattr(mt5, name))
            except (TypeError, ValueError, AttributeError):
                out[name] = None
        return {"ok": True, "constants": out}

    def initialize(self) -> tuple:
        """Liga ao terminal na thread do módulo e devolve (ok, status)."""
        return self._mt5_run(self._initialize_impl)

    def _initialize_impl(self) -> tuple:
        ok = self._ensure_mt5()
        st = self._status_impl()
        st["ok"] = ok
        st["error"] = "" if ok else (self._last_error or "sem ligação")
        return ok, st

    # ------------------------------------------- despacho do módulo MT5

    def mt5_call(self, name: str, args: List[Any],
                 kwargs: Dict[str, Any]) -> tuple[bool, Any, str]:
        """Executa `getattr(module, name)(*args, **kwargs)` na thread do módulo.

        Permitido apenas para a allowlist do engine. Devolve
        (ok, resultado JSON-safe, erro).
        """
        return self._mt5_run(
            lambda: self._mt5_call_impl(name, args, kwargs))

    def _mt5_call_impl(self, name: str, args: List[Any],
                       kwargs: Dict[str, Any]) -> tuple[bool, Any, str]:
        if name not in MT5_CALL_SHAPES:
            return False, None, f"chamada MT5 não permitida: {name}"
        if not self._ensure_mt5():
            return False, None, self._last_error or "MT5 não ligado"
        self._maybe_probe()
        mt5 = self.provider._mt5
        fn = getattr(mt5, name, None)
        if fn is None:
            return False, None, f"módulo MT5 sem {name}"
        # --------------------------------------------------------------
        # CAUSA RAIZ (03/09, reproduzida e isolada): o wrapper C do
        # order_send DESTE BUILD (MetaTrader5 5.0.6090, cp314) recusa
        # qualquer invocação que chegue com um dict de kwargs presente —
        # mesmo VAZIO. `fn(*args, **{})` devolve None com last_error (-2,
        # "Unnamed arguments not allowed") e a ordem nunca sai (retcode
        # null → '?' → phantom na lane 2 desde 01/09). A conta principal
        # chama `order_send(request)` direto no bytecode (sem dict) e
        # preenche — por isso só a lane 2 falhava. `symbol_info_tick` é
        # igual: rejeita kwargs ("takes no keyword arguments").
        #
        # O marshaling é POR CONVENÇÃO (MT5_CALL_SHAPES →
        # `normalize_mt5_call`): posicionais NUNCA recebem dict de kwargs
        # (kwargs vão fundidos em args pelo nome); kwargs-only recebem
        # kwargs (vazio → fn() sem dict). Uma forma que não case (args a
        # mais, kwargs desconhecidos, posicionais numa função de kwargs) é
        # recusada ANTES do módulo — a convenção errada nunca volta a tocar
        # no C-core.
        # --------------------------------------------------------------
        try:
            call_args, call_kwargs = normalize_mt5_call(name, args, kwargs)
        except ValueError as exc:
            self._last_error = f"{name}: {exc}"
            return False, None, self._last_error
        try:
            # SEMPRE sem dict de kwargs quando a convenção é posicional;
            # nas kwargs-only, kwargs presentes (nunca um dict vazio)
            if call_kwargs:
                result = fn(*call_args, **call_kwargs)
            else:
                result = fn(*call_args)
        except Exception as exc:
            self._last_error = f"{name} falhou: {exc}"
            return False, None, f"{name} falhou: {exc}"
        if result is None and name in ("order_send", "order_delete"):
            # Ordem SEM resposta do módulo (retcode null upstream → '?'): o
            # erro REAL fica gravado na MESMA thread para o alerta/backfill
            # de retcode null deixar de ser um mistério (ex.: IPC timeout,
            # auto-trading off — até agora aparecia só '?').
            try:
                code, text = mt5.last_error()
            except Exception:
                code, text = None, ""
            if code != 1:  # RES_S_OK — sem erro real
                self._last_error = (
                    f"{name} devolveu None — módulo MT5: "
                    f"{code} {text}".strip())
            else:
                self._last_error = f"{name} devolveu None (sem erro do módulo)"
        else:
            self._last_error = ""
        return True, _jsonable(result), ""

    # --------------------------------------------------------------- http

    def _bind(self) -> None:
        """Cria e bind o socket (porta 0 = aleatória, para testes)."""
        if self._httpd is None:
            handler = self._make_handler()
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
            if self.port == 0:
                self.port = int(self._httpd.server_address[1])

    def serve_forever(self) -> None:
        self._bind()
        self._httpd.serve_forever()

    def bind(self) -> None:
        """Bind o socket sem servir ainda (testes: porta 0 = aleatória)."""
        self._bind()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        with self._mt5_pool_lock:
            pool, self._mt5_pool = self._mt5_pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def _make_handler(self):
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # ruído silencioso (rotativo no stdout)
                pass

            # ---- helpers
            def _unauth(self, why: str = ""):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"ok": False, "error": "token lane2_worker inválido"}).encode())

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                if not worker.token:
                    return True
                hdr = self.headers.get("x-api-token", "") or ""
                authz = self.headers.get("Authorization", "") or ""
                if hdr == worker.token:
                    return True
                if authz.startswith("Bearer ") and authz[7:] == worker.token:
                    return True
                return False

            def _get_str(self, name: str, default: str = "") -> str:
                q = parse_qs(urlparse(self.path).query)
                vals = q.get(name)
                return vals[0] if vals else default

            # ---- rotas
            def do_GET(self):
                if not self._authed():
                    return self._unauth()
                path = urlparse(self.path).path
                try:
                    if path == "/ping":
                        return self._reply(200, {"ok": True, "ts": time.time()})
                    if path == "/status":
                        return self._reply(200, worker.status())
                    if path == "/constants":
                        return self._reply(200, worker.constants())
                    if path == "/account":
                        acct = worker._mt5_run(
                            lambda: (worker.provider.account()
                                     if worker.provider.connected else None))
                        return self._reply(200, {"ok": True, "account": _jsonable(acct)})
                    if path == "/positions":
                        try:
                            pos = worker._mt5_run(
                                lambda: (worker.provider.positions()
                                         if worker.provider.connected else []))
                        except Exception as exc:
                            pos = []
                            worker._last_error = f"positions falhou: {exc}"
                        return self._reply(200, {"ok": True, "positions": _jsonable(pos)})
                    if path == "/quote":
                        sym = self._get_str("sym")
                        q = worker._mt5_run(
                            lambda: (worker.provider.quote(sym)
                                     if sym and worker.provider.connected else None))
                        return self._reply(200, {"ok": True, "quote": _jsonable(q)})
                    if path == "/meta":
                        sym = self._get_str("sym")
                        m = worker._mt5_run(
                            lambda: worker.provider.meta(sym) if sym else None)
                        return self._reply(200, {"ok": True, "meta": _jsonable(m)})
                    if path == "/copy_rates":
                        sym = self._get_str("sym")
                        try:
                            tf = int(self._get_str("tf", "1"))
                            count = int(self._get_str("count", "100"))
                        except ValueError:
                            tf, count = 1, 100
                        rates = worker._mt5_run(
                            lambda: (worker.provider.copy_rates(sym, tf, count)
                                     if sym else []))
                        return self._reply(200, {"ok": True, "rates": _jsonable(rates)})
                    return self._reply(404, {"ok": False, "error": f"rota desconhecida: {path}"})
                except Exception as exc:
                    worker._last_error = f"GET {path} falhou: {exc}"
                    return self._reply(500, {"ok": False, "error": str(exc)})

            def do_POST(self):
                if not self._authed():
                    return self._unauth()
                path = urlparse(self.path).path
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw.decode("utf-8") or "{}")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return self._reply(400, {"ok": False, "error": "body JSON inválido"})
                    if path == "/initialize":
                        ok, st = worker.initialize()
                        return self._reply(200 if ok else 503, st)
                    if path == "/symbol_select":
                        sym = str(body.get("symbol", ""))
                        if sym:
                            worker._mt5_run(
                                lambda: worker.provider.ensure_symbol(sym))
                        return self._reply(200, {"ok": True})
                    if path == "/mt5/call":
                        name = str(body.get("name", ""))
                        args = list(body.get("args") or [])
                        kwargs = dict(body.get("kwargs") or {})
                        ok, result, error = worker.mt5_call(name, args, kwargs)
                        if ok:
                            return self._reply(200, {"ok": True, "result": result, "name": name})
                        code = 400 if "não permitida" in error else 503
                        return self._reply(code, {"ok": False, "error": error, "name": name})
                    return self._reply(404, {"ok": False, "error": f"rota desconhecida: {path}"})
                except Exception as exc:
                    worker._last_error = f"POST {path} falhou: {exc}"
                    return self._reply(500, {"ok": False, "error": str(exc)})

        return Handler


def _default_port() -> int:
    try:
        return int(os.getenv("LANE2_WORKER_PORT", "8770") or 8770)
    except ValueError:
        return 8770


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Worker MT5 da lane 2 (2ª conta demo)")
    parser.add_argument("--host", default=os.getenv("LANE2_WORKER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_default_port())
    args = parser.parse_args(argv)

    configure_process_stdio("lane2_worker")

    # console Windows pode ser cp1252 — nunca deixar um acento/emoji rebentar
    # o arranque em modo sem rotação (validação); com rotação o stream é UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ---------------------------------------------------------------- guard
    # FAIL-CLOSED OBRIGATÓRIO: sem uma instalação PORTABLE da 2ª conta, o
    # initialize(login=...) liga ao TERMINAL POR OMISSÃO e faz login na conta
    # passada — o que troca a conta do terminal da principal (observado a
    # 01/09: a produção ficou a apontar para a 111495351 até ser relogada).
    # O worker NUNCA pode arrancar sem o path portable: sai já com erro.
    terminal_path = os.getenv("MT5_2_TERMINAL_PATH", "").strip()
    if not terminal_path:
        print("[lane2_worker] ERRO FATAL: MT5_2_TERMINAL_PATH vazio no .env. "
              "O worker nunca pode ligar ao terminal por omissao (troca a conta "
              "da principal). Instala a 2a conta em modo portable, define "
              "MT5_2_TERMINAL_PATH=...\\terminal64.exe e volta a correr.",
              file=sys.stderr)
        return 2
    if not os.path.exists(terminal_path):
        print(f"[lane2_worker] ERRO FATAL: MT5_2_TERMINAL_PATH nao existe: "
              f"{terminal_path}", file=sys.stderr)
        return 2

    login_raw = os.getenv("MT5_2_LOGIN", "")
    # O worker serve execução real (require_hedged=True, falha-fechado se a
    # conta não for RETAIL_HEDGING — mesma regra da conta principal). A lane 2
    # em dry-run continua a ser o caminho antigo (sem worker).
    provider = MT5Provider(
        login=int(login_raw) if login_raw and login_raw.isdigit() else None,
        server=os.getenv("MT5_2_SERVER", ""),
        password=os.getenv("MT5_2_PASSWORD", ""),
        symbols=load_symbol_map(),
        require_hedged=True,
        path=terminal_path,
        portable=True,
    )
    token = os.getenv("LANE2_WORKER_TOKEN", "") or os.getenv("SIGNALBOT_API_TOKEN", "")
    worker = Lane2Worker(provider, port=args.port, host=args.host, token=token)

    login_txt = f"login {login_raw}" if login_raw else "sem login (terminal já autenticado)"
    print(f"[lane2_worker] a escutar em {args.host}:{args.port} - {login_txt}")
    print(f"[lane2_worker] terminal portable: {terminal_path}")
    try:
        worker.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())