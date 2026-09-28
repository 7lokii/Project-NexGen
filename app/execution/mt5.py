"""MetaTrader 5 execution bridge.

Two pieces, mirroring the rest of the execution layer:

  * `MT5Provider` — talks to the running MT5 terminal (MetaQuotes API):
    validates symbol mappings, returns live ticks and per-symbol trading
    metadata (contract size, pip size, volume step / min / max). MT5 must be
    running and logged in; credentials can be passed via env vars
    (MT5_LOGIN / MT5_SERVER / MT5_PASSWORD) or come from the logged-in
    terminal.

  * `MT5Engine` — same interface as `SimEngine` (open_trade / update /
    close_trade_at_market) so `BotState` and the API don't care where the
    order goes. Two modes:

      * dry-run (default, `MT5_DRY_RUN=1`): positions are recorded in the DB
        exactly like paper trades, but fills are simulated at the current MT5
        tick and every order the engine *would* send is logged. Nothing ever
        reaches the broker.
      * real (`MT5_DRY_RUN=0`): orders go to the broker through
        `mt5.order_send`. P&L comes from MT5 positions themselves.

    Symbol mapping: the app uses 6-letter names (XAUUSD, EURUSD, ...). The
    broker may call them differently (XAUUSD.a, GOLD, ...). `MT5_SYMBOLS`
    is a JSON dict of overrides; by default the app name is used as-is and
    validated at startup via `mt5.symbol_info()`.
"""

import json
import os
from typing import Dict, List, Optional

from app.config import CONTRACT_SIZE, MIN_LOTS, PIP_SIZE
from app.db import DB
from app.execution.clock import Clock, SystemClock
from app.execution.engine import _legs, _new_id, _now_iso
from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    hybrid_protect_eligible,
    monotonic_sl,
    group_protect_sl,
    reduce_auto_protect,
    sl_exit_reason,
    time_stop_due_iso,
)
from app.models import Position, Signal, Trade
from app.risk.engine import effective_strategy_risk_pct

MT5_DRY_RUN = os.getenv("MT5_DRY_RUN", "1") != "0"

# MetaTrader 5 account margin modes: 0 = retail netting, 1 = exchange,
# 2 = retail hedging.  The multi-leg strategy requires the last one so each
# TP can remain an independent broker position.
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


def is_hedged_margin_mode(mode) -> bool:
    """Return whether an MT5 ``margin_mode`` identifies a hedged account."""
    if isinstance(mode, str):
        return "hedg" in mode.lower()
    try:
        return int(mode) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    except (TypeError, ValueError):
        return False


# Default symbol metadata, used only when MT5 is unreachable (dry-run still
# needs contract/pip to size lots and compute P&L). Overridden by the real
# terminal values whenever they are available.
FALLBACK_SYMBOLS = {
    "XAUUSD": {"mt5": "XAUUSD", "contract": 100.0, "pip": 0.01, "digits": 2,
               "vol_min": 0.01, "vol_max": 100.0, "vol_step": 0.01},
    "XAGUSD": {"mt5": "XAGUSD", "contract": 5000.0, "pip": 0.001, "digits": 3,
               "vol_min": 0.01, "vol_max": 100.0, "vol_step": 0.01},
    "EURUSD": {"mt5": "EURUSD", "contract": 100_000.0, "pip": 0.0001, "digits": 5,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
    "GBPUSD": {"mt5": "GBPUSD", "contract": 100_000.0, "pip": 0.0001, "digits": 5,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
    "USDJPY": {"mt5": "USDJPY", "contract": 100_000.0, "pip": 0.01, "digits": 3,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
    "AUDUSD": {"mt5": "AUDUSD", "contract": 100_000.0, "pip": 0.0001, "digits": 5,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
    "USDCAD": {"mt5": "USDCAD", "contract": 100_000.0, "pip": 0.0001, "digits": 5,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
    "NZDUSD": {"mt5": "NZDUSD", "contract": 100_000.0, "pip": 0.0001, "digits": 5,
               "vol_min": 0.01, "vol_max": 500.0, "vol_step": 0.01},
}


def load_symbol_map() -> Dict[str, Dict]:
    """App symbol -> MT5 symbol mapping (env JSON override, else identity)."""
    raw = os.getenv("MT5_SYMBOLS", "")
    if raw:
        try:
            override = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[mt5] MT5_SYMBOLS inválido (ignorado): {raw!r}")
            override = {}
    else:
        override = {}
    out = {}
    for app_sym, meta in FALLBACK_SYMBOLS.items():
        entry = dict(meta)
        if app_sym in override:
            entry["mt5"] = override[app_sym]
        out[app_sym] = entry
    return out


class MT5Provider:
    """Thin wrapper over the MetaQuotes Python API.

    `initialize()` connects to the running terminal (explicit login when
    MT5_LOGIN / MT5_SERVER / MT5_PASSWORD are set). `ensure_symbol()` adds a
    symbol to the Market Watch so ticks start streaming. `meta()` merges the
    terminal's real values (contract size, digits, volume constraints) over
    the fallback table.
    """

    def __init__(self, login: Optional[int] = None, server: str = "",
                 password: str = "", symbols: Optional[Dict[str, Dict]] = None,
                 require_hedged: bool = True, path: str = "",
                 portable: bool = False):
        self._mt5 = None
        self._login = login
        self._server = server
        self._password = password
        self._require_hedged = bool(require_hedged)
        self._path = (path or "").strip()
        self._portable = bool(portable)
        self._symbols = symbols if symbols is not None else load_symbol_map()
        self._meta_cache: Dict[str, Dict] = {}
        self._account_cache: Optional[dict] = None
        self._account_ts = 0.0
        self._account_margin_mode = None
        self._account_hedged: Optional[bool] = None
        self._execution_block_reason = ""
        self._positions_cache: Optional[list] = None
        self._positions_ts = 0.0

    # ------------------------------------------------------------ connect

    def initialize(self) -> bool:
        # A conta não muda de netting para hedging during a reconnect. Once
        # blocked, require a process restart (or a new provider) rather than
        # repeatedly logging the same dangerous condition every 10 seconds.
        if self._execution_block_reason:
            return False
        try:
            import MetaTrader5 as mt5
        except ImportError:
            print("[mt5] pacote MetaTrader5 não instalado (pip install MetaTrader5)")
            return False
        self._mt5 = mt5
        try:
            if self._login:
                kwargs = {"login": self._login, "password": self._password,
                          "server": self._server}
                # Uma instalação portable / segundo terminal pode ser endereçada
                # pelo caminho do terminal64.exe. Sem path, liga ao terminal
                # por omissão (comportamento atual).
                if self._path:
                    kwargs["path"] = self._path
                    kwargs["portable"] = self._portable
                ok = mt5.initialize(**kwargs)
            else:
                ok = mt5.initialize()
        except Exception as exc:
            print(f"[mt5] initialize() falhou: {exc}")
            self._mt5 = None
            return False
        if not ok:
            print(f"[mt5] initialize() falhou: {mt5.last_error()}")
            self._mt5 = None
            return False

        try:
            info = mt5.account_info()
        except Exception as exc:
            info = None
            print(f"[mt5] account_info() falhou: {exc}")
        margin_mode = getattr(info, "margin_mode", None) if info is not None else None
        self._account_margin_mode = margin_mode
        self._account_hedged = is_hedged_margin_mode(margin_mode)
        if info is not None:
            try:
                self._account_cache = {
                    "login": info.login, "server": info.server,
                    "currency": info.currency, "balance": float(info.balance),
                    "equity": float(info.equity), "leverage": int(info.leverage),
                    "trade_mode": int(info.trade_mode),
                    "margin_mode": margin_mode, "hedged": self._account_hedged,
                }
                self._account_ts = __import__("time").monotonic()
            except (AttributeError, TypeError, ValueError):
                self._account_cache = None
        if self._require_hedged and not self._account_hedged:
            mode_text = "desconhecido" if margin_mode is None else str(margin_mode)
            self._execution_block_reason = (
                "conta MT5 não é hedged (margin_mode=" + mode_text + ") — "
                "execução real bloqueada; usa uma conta RETAIL_HEDGING"
            )
            print(f"[mt5] BLOQUEADO: {self._execution_block_reason}")
            try:
                mt5.shutdown()
            except Exception:
                pass
            self._mt5 = None
            return False
        return True

    @property
    def connected(self) -> bool:
        if self._execution_block_reason:
            return False
        try:
            return self._mt5 is not None and bool(self._mt5.terminal_info())
        except Exception:
            return False

    @property
    def execution_block_reason(self) -> str:
        return self._execution_block_reason

    @property
    def account_margin_mode(self):
        return self._account_margin_mode

    @property
    def account_hedged(self) -> Optional[bool]:
        return self._account_hedged

    def shutdown(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
            self._mt5 = None

    # ----------------------------------------------------------- symbols

    def ensure_symbol(self, app_symbol: str) -> None:
        """Add the MT5 symbol to the Market Watch so ticks stream."""
        if not self.connected:
            return
        meta = self._symbols.get(app_symbol)
        if meta is None:
            return
        try:
            self._mt5.symbol_select(meta["mt5"], True)
        except Exception as exc:
            print(f"[mt5] symbol_select({app_symbol}) falhou: {exc}")

    def meta(self, app_symbol: str) -> Optional[Dict]:
        """Per-symbol trading metadata, real values from the terminal when up.

        Returns None when the symbol has no mapping at all.
        """
        if app_symbol not in self._symbols:
            return None
        if app_symbol in self._meta_cache:
            return self._meta_cache[app_symbol]
        meta = dict(self._symbols[app_symbol])
        if self.connected:
            mt5 = self._mt5
            mt5_sym = meta["mt5"]
            try:
                info = mt5.symbol_info(mt5_sym)
                if info is not None:
                    meta["contract"] = float(info.trade_contract_size)
                    meta["digits"] = int(info.digits)
                    meta["vol_min"] = float(info.volume_min)
                    meta["vol_max"] = float(info.volume_max)
                    meta["vol_step"] = float(info.volume_step)
                    # Keep the project's *pip* convention distinct from the
                    # broker's point/tick. A 5-digit EURUSD quotes in
                    # 0.00001 ticks but one pip is 0.0001; using the tick here
                    # would inflate spread and risk distances by 10x. The
                    # fallback map already carries the correct pip for each
                    # supported app symbol; retain it and expose the broker
                    # tick separately for price rounding/audit.
                    meta["tick_size"] = float(info.trade_tick_size)
                    meta["pip"] = float(meta.get("pip") or info.trade_tick_size)
                else:
                    print(f"[mt5] símbolo {mt5_sym} não existe no broker "
                          f"(check MT5_SYMBOLS)")
            except Exception as exc:
                print(f"[mt5] symbol_info({mt5_sym}) falhou: {exc}")
        self._meta_cache[app_symbol] = meta
        return meta

    def quote(self, app_symbol: str) -> Optional[dict]:
        """Return bid/ask and the instantaneous spread for an app symbol."""
        meta = self.meta(app_symbol)
        if meta is None or not self.connected or self._mt5 is None:
            return None
        try:
            tick = self._mt5.symbol_info_tick(meta["mt5"])
        except Exception as exc:
            print(f"[mt5] quote({app_symbol}) falhou: {exc}")
            return None
        if tick is None or not (tick.bid > 0) or not (tick.ask > 0):
            return None
        bid, ask = float(tick.bid), float(tick.ask)
        spread = ask - bid
        pip = float(meta.get("pip") or 0.0)
        if spread < 0 or pip <= 0 or spread != spread:
            return None
        return {
            "symbol": app_symbol,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pips": spread / pip,
            "source": "mt5",
        }

    def tick(self, app_symbol: str) -> Optional[float]:
        """Current mid price for an app symbol (bid/ask midpoint)."""
        quote = self.quote(app_symbol)
        if quote is None:
            return None
        meta = self.meta(app_symbol)
        return round((quote["bid"] + quote["ask"]) / 2.0, meta["digits"])

    def copy_rates(self, app_symbol: str, timeframe: int = 1,
                   count: int = 100) -> list:
        """Últimas `count` velas do símbolo (`mt5.copy_rates_from_pos`) — para
        o trailing adaptativo por ATR (Fase 3, ESTRATEGIAS.md Parte X). `timeframe` é o
        enum do MT5 (M1=1, M5=5). Devolve uma lista de dicts
        {time, open, high, low, close, tick_volume}; vazia sem terminal ou
        se o download falhar (o trail fixo é o fallback)."""
        meta = self.meta(app_symbol)
        if meta is None or not self.connected:
            return []
        try:
            # position 0 is the currently forming candle; use position 1 so
            # ATR decisions see completed bars only (no look-ahead).
            # NB: `or []` sobre o resultado rebentaria — o MT5 devolve uma
            # numpy structured array e o truthiness de arrays >1 elemento
            # é ambíguo (o download de velas falhava sempre em silêncio).
            rates = self._mt5.copy_rates_from_pos(meta["mt5"], timeframe, 1, count)
            if rates is None:
                rates = []
        except Exception as exc:
            print(f"[mt5] copy_rates({app_symbol}) falhou: {exc}")
            return []
        def _field(r, name, default):
            # acesso por índice (structured arrays do numpy — o numpy 2.x
            # removeu o acesso por atributo, que devolvia sempre 0) com
            # fallback para atributo (fakes/API antiga nos testes)
            try:
                return r[name]
            except (TypeError, KeyError, IndexError):
                return getattr(r, name, default)

        out = []
        for r in rates:
            try:
                out.append({
                    "time": int(_field(r, "time", 0)),
                    "open": float(_field(r, "open", 0.0)),
                    "high": float(_field(r, "high", 0.0)),
                    "low": float(_field(r, "low", 0.0)),
                    "close": float(_field(r, "close", 0.0)),
                    "tick_volume": float(_field(r, "tick_volume", 0.0)),
                })
            except (TypeError, ValueError):
                # linha malformada: ignora em vez de rebentar o download
                continue
        return out

    # ------------------------------------------------------- positions

    def positions(self, cache_seconds: float = 2.0) -> list:
        """Live open positions from the broker (for the dashboard)."""
        import time
        now = time.monotonic()
        if (self._positions_cache is not None
                and now - self._positions_ts < cache_seconds):
            return self._positions_cache
        out = []
        if self.connected:
            try:
                for p in (self._mt5.positions_get() or []):
                    out.append({
                        "ticket": p.ticket,
                        "symbol": p.symbol,
                        "type": "buy" if p.type == 0 else "sell",
                        "volume": float(p.volume),
                        "price_open": float(p.price_open),
                        "sl": float(p.sl or 0.0),
                        "tp": float(p.tp or 0.0),
                        "profit": round(float(p.profit or 0.0), 2),
                        "comment": getattr(p, "comment", "") or "",
                    })
            except Exception:
                out = []
        self._positions_cache = out
        self._positions_ts = now
        return out

    # ------------------------------------------------------- account info

    def account(self, cache_seconds: float = 5.0) -> Optional[dict]:
        """Account info, cached `cache_seconds` (the tick loop calls this a lot).

        The last snapshot remains available after a hedging-policy block so
        diagnostics can explain which account was refused.
        """
        import time
        now = time.monotonic()
        if self._account_cache is not None and now - self._account_ts < cache_seconds:
            return self._account_cache
        if not self.connected:
            return self._account_cache
        try:
            info = self._mt5.account_info()
        except Exception:
            return self._account_cache
        if info is None:
            return self._account_cache
        self._account_margin_mode = getattr(info, "margin_mode", self._account_margin_mode)
        self._account_hedged = is_hedged_margin_mode(self._account_margin_mode)
        self._account_cache = {
            "login": info.login, "server": info.server, "currency": info.currency,
            "balance": float(info.balance), "equity": float(info.equity),
            "leverage": int(info.leverage), "trade_mode": int(info.trade_mode),
            "margin_mode": self._account_margin_mode,
            "hedged": self._account_hedged,
        }
        self._account_ts = now
        return self._account_cache


def _compute_lots_sym(equity: float, risk_pct: float, entry: float, stop_loss: float,
                      meta: Dict) -> float:
    """Position size so a stop-out loses `risk_pct` of equity, per symbol.

    Uses the symbol's real contract size and pip size (gold: contract 100,
    pip 0.01; forex: contract 100000, pip 0.0001).
    """
    if risk_pct <= 0:
        return float(meta.get("vol_min", MIN_LOTS))
    contract = float(meta.get("contract", CONTRACT_SIZE))
    pip = float(meta.get("pip", PIP_SIZE))
    step = float(meta.get("vol_step", 0.01))
    vmin = float(meta.get("vol_min", MIN_LOTS))
    vmax = float(meta.get("vol_max", 1_000_000.0))
    risk_amount = equity * risk_pct / 100.0
    risk_pips = abs(entry - stop_loss) / pip
    if risk_pips <= 0:
        raise ValueError("stop_loss must differ from entry")
    lots = risk_amount / (risk_pips * contract * pip)
    lots = max(vmin, min(vmax, round(lots / step) * step))
    return round(lots, 4)


class MT5Engine:
    # watchdog das ordens pendentes: depois de N falhas CONSECUTIVAS de
    # order_delete (ex.: worker da lane offline) o tick devolve o evento
    # `pending_delete_stuck` para o feed/Telegram alertar — a perna deixa de
    # ser cancelada em silêncio, sem nunca perder o rasto no MT5. Enquanto
    # presa, o alerta repete a cada `PENDING_DELETE_REALERT_EVERY` segundos
    # para uma falha prolongada do worker continuar visível.
    PENDING_DELETE_ESCALATE_AFTER = 5
    PENDING_DELETE_REALERT_EVERY = 1800.0  # 30 min entre re-alertas
    """Broker execution engine, same interface as SimEngine.

    dry_run=True (default) logs every order and simulates fills at the MT5
    tick — nothing reaches the broker. dry_run=False sends real orders via
    mt5.order_send and reads P&L from MT5 positions.
    """

    def __init__(self, db: DB, provider: MT5Provider, market,
                 dry_run: bool = MT5_DRY_RUN, logger=None,
                 clock: Optional[Clock] = None, lane_id: str = "main"):
        self.db = db
        self.provider = provider
        self.lane_id = lane_id
        self.market = market          # used for the fallback price in dry-run
        self.dry_run = dry_run
        self.logger = logger or print
        self.clock = clock or SystemClock()
        # live MT5 position ticket -> app position id (real mode). Also
        # persisted in the DB config table (`mt5ticket:<ticket>`) so the
        # mapping survives a server restart (the broker keeps managing SL/TP
        # while the server is down).
        self._ticket_pos: Dict[int, str] = {}
        try:
            for key, val in db.all_config().items():
                if key.startswith("mt5ticket:") and val:
                    self._ticket_pos[int(key.split(":", 1)[1])] = val
        except Exception:
            pass
        # Fase 3 — estado da auto-gestão por trade (em memória, como o paper):
        #   tp1_ts   — quando o TP1 fechou (início do trailing/time stop)
        #   peak     — pico do preço desde o TP1 (buy) / vale (sell)
        #   last_mod — último modify de SL por perna (throttling 1×/min)
        # Após um restart o pico recomeça do preço atual (aproximado,
        # documentado no ESTRATEGIAS.md Parte X) — nunca bloqueia a execução.
        self._auto_state: Dict[str, dict] = {}
        # avisos de SL recusado/clamped durante o update (drenados para os
        # eventos) — dedup por (trade, perna, alvo): um modify recusado não
        # marca throttle (para poder acertar quando o broker deixar), mas o
        # aviso não se repete a cada tick
        self._sl_warnings: List[dict] = []
        self._sl_warned: set = set()
        import time as _t
        self._auto_clock = self.clock
        # Spread is sampled once per minute per symbol; the raw quote remains
        # available on every tick, but the DB stores a bounded time series.
        self._last_spread_observation = 0.0
        self._spread_observation_interval = 60.0
        # estado do retry de delete de pernas pendentes (watchdog
        # `pending_delete_stuck`): falhas consecutivas por perna (com o
        # contexto para o Diagnóstico), monotónico do último alerta
        # (re-alerta periódico) e a INTENÇÃO do delete que ainda não
        # conseguiu (TTL ou cancelamento de gestão) — o tick re-tenta assim
        # que o worker voltar, sem esperar o TTL. Reset quando o delete
        # consegue ou a perna deixa de estar pendente. PERSISTIDO na BD
        # (`pending_delete_states`) para o Diagnóstico e o re-alerta
        # sobreviverem a um restart do servidor: o estado real é a BD, a
        # memória é só o cache da sessão.
        self._pending_delete_failures: Dict[str, dict] = {}
        self._pending_delete_alerted_at: Dict[str, float] = {}
        self._pending_delete_intent: Dict[str, dict] = {}
        # pernas que já gravaram a exceção do retry (rate-limit: enquanto
        # presas, o tick falha todos os segundos mas só a PRIMEIRA falha do
        # episódio vai para `operational_exceptions` — uma exceção concisa
        # em vez de centenas de linhas idênticas)
        self._pending_delete_exception_recorded: set = set()
        try:
            for pos_id, entry in self.db.load_pending_delete_states(
                    self.lane_id).items():
                self._pending_delete_failures[pos_id] = entry
                intent = entry.get("intent")
                if isinstance(intent, dict) and intent:
                    self._pending_delete_intent[pos_id] = intent
                # já escalonada antes do restart → o 1º retry pós-reboot
                # volta a alertar (o monotónico não sobrevive ao restart)
                if int(entry.get("attempts") or 0) \
                        >= self.PENDING_DELETE_ESCALATE_AFTER:
                    self._pending_delete_alerted_at[pos_id] = \
                        self.clock.monotonic() - self.PENDING_DELETE_REALERT_EVERY
        except Exception:
            pass
        # Broker evidence is opt-in with the same switch as quote/management
        # journaling. These keys prevent a single update loop from writing the
        # same close observation twice while the broker history settles.
        self._journaled_close_events = set()

    def _journal_enabled(self) -> bool:
        """Read the opt-in evidence flag without coupling MT5 to BotState."""
        try:
            value = self.db.all_config().get("execution_journal_enabled", "false")
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            return False

    def _journal_event(self, kind: str, *, at: str = "", trade: Optional[Trade] = None,
                       pos: Optional[Position] = None, symbol: str = "",
                       payload: Optional[dict] = None) -> None:
        """Persist a broker observation in the common replay vocabulary.

        This helper is deliberately best-effort: evidence must never block an
        order or turn a broker response into a trading failure. The payload is
        JSON-safe and includes the app identifiers needed to correlate a fill
        after a restart.
        """
        if not self._journal_enabled():
            return
        try:
            self.db.record_execution_event(
                at or self.clock.iso(), kind, engine="mt5",
                symbol=symbol or (trade.symbol if trade else (pos.symbol if pos else "")),
                trade_id=trade.id if trade else (pos.trade_id if pos else ""),
                signal_id=trade.signal_id if trade else "",
                payload=payload or {},
            )
        except Exception as exc:
            # Evidence is observability, never part of the order critical path.
            self.logger(f"[mt5] journal event failed ({kind}): {exc}")

    @staticmethod
    def _retcode(result) -> Optional[int]:
        try:
            return int(getattr(result, "retcode"))
        except (TypeError, ValueError, AttributeError):
            return None

    def _signal_received_at(self, trade: Trade) -> str:
        try:
            signal = self.db.get_signal(trade.signal_id)
            return signal.received_at if signal else trade.opened_at
        except Exception:
            return trade.opened_at

    def _ticket_for(self, pos_id: str) -> Optional[int]:
        """MT5 position ticket for an app position id (memory or persisted)."""
        for t, pid in self._ticket_pos.items():
            if pid == pos_id:
                return t
        try:
            for key, val in self.db.all_config().items():
                if key.startswith("mt5ticket:") and val == pos_id:
                    return int(key.split(":", 1)[1])
        except Exception:
            pass
        return None

    # -------------------------------------------------------------- helpers

    def _log_order(self, action: str, detail: dict) -> None:
        self.logger(f"[mt5-{'dry' if self.dry_run else 'real'}] {action}: {detail}")

    def _price(self, app_symbol: str):
        """Live price: MT5 tick when connected, else the market feed."""
        if self.provider.connected:
            return self.provider.tick(app_symbol)
        return self.market.price(app_symbol) if self.market else None

    def record_spread_observations(self, observed_at: str = "", force: bool = False) -> int:
        """Persist one bid/ask sample per mapped symbol at most once/minute."""
        if not self.provider.connected or not hasattr(self.provider, "quote"):
            return 0
        now = self._auto_clock.monotonic()
        if not force and now - self._last_spread_observation < self._spread_observation_interval:
            return 0
        self._last_spread_observation = now
        stamp = observed_at or _now_iso()
        written = 0
        for symbol in getattr(self.provider, "_symbols", {}):
            quote = self.provider.quote(symbol)
            if not quote:
                continue
            self.db.insert_spread_observation(
                stamp, symbol, quote["bid"], quote["ask"],
                quote["spread"], quote["spread_pips"], quote.get("source", "mt5"),
            )
            written += 1
        return written

    def _sym_meta(self, app_symbol: str) -> Optional[Dict]:
        return self.provider.meta(app_symbol)

    # ---------------------------------------------------------------- open

    def open_trade(self, signal: Signal, risk_pct: float, balance: float,
                   trade_id: str = "", now: str = "", lots_override: Optional[float] = None,
                   max_legs: Optional[int] = None, auto: Optional[dict] = None,
                   execution_meta: Optional[dict] = None) -> Trade:
        """Open a one-leg-per-TP trade for a signal, splitting the risk evenly.

        `lots_override` (when set) sizes every leg at that fixed volume
        instead of the risk-based calculation — useful for demo tests.
        `max_legs` caps the number of legs (estratégias do registo central).
        """
        trade_id = trade_id or "t-" + _new_id()
        now = now or self.clock.iso()

        if not self.dry_run and (
                self.provider is None or not self.provider.connected
        ):
            reason = getattr(self.provider, "execution_block_reason", "") or "terminal MT5 offline"
            raise RuntimeError(f"lane {self.lane_id} sem ligação MT5: {reason}")

        meta = self._sym_meta(signal.symbol) or self.provider._symbols.get(signal.symbol)
        if meta is None:
            raise ValueError(f"símbolo {signal.symbol} não mapeado para MT5 "
                             f"(adiciona em MT5_SYMBOLS)")
        legs = _legs(signal, max_legs)
        n = len(legs)
        if auto and auto.get("def_window"):
            risk_pct = effective_strategy_risk_pct(
                risk_pct,
                def_window=auto.get("def_window"),
                def_risk_pct=auto.get("def_risk_pct"),
                prior_pnls=self.db.recent_closed_pnls(int(auto["def_window"])),
            )
        per_leg = (risk_pct / n) if n else risk_pct

        if lots_override and lots_override > 0:
            lots = {leg_num: float(lots_override) for leg_num, _ in legs}
        else:
            lots = {
                leg_num: _compute_lots_sym(balance, per_leg, signal.entry,
                                           signal.stop_loss, meta)
                for leg_num, _ in legs
            }

        trade = Trade(
            id=trade_id, signal_id=signal.id, symbol=signal.symbol,
            direction=signal.direction, entry=signal.entry,
            stop_loss=signal.stop_loss, take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2, take_profit_3=signal.take_profit_3,
            take_profit_4=signal.take_profit_4, take_profit_5=signal.take_profit_5,
            risk_pct=risk_pct,
            lots_1=lots.get(1, 0.0), lots_2=lots.get(2, 0.0),
            lots_3=lots.get(3, 0.0), lots_4=lots.get(4, 0.0),
            lots_5=lots.get(5, 0.0), status="open", opened_at=now,
            entry_zone_low=(execution_meta or {}).get("zone_low"),
            entry_zone_high=(execution_meta or {}).get("zone_high"),
            market_price_at_open=(execution_meta or {}).get("market_price"),
            entry_distance=(execution_meta or {}).get("distance"),
            entry_guard=(execution_meta or {}).get("guard"),
            execution_mode=(execution_meta or {}).get("execution_mode", "mt5-real"),
            lane_id=(execution_meta or {}).get("lane_id"),
            entry_style=(execution_meta or {}).get("entry_style"),
        )
        self.db.insert_trade(trade)

        pending_style = (execution_meta or {}).get("entry_style") == "pending"
        positions = []
        for leg_num, tp in legs:
            pos = Position(
                id=f"p-{_new_id()}", trade_id=trade.id, leg=leg_num,
                symbol=signal.symbol, direction=signal.direction,
                lots=lots[leg_num], entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit=tp,
                status="pending_fill" if pending_style else "open",
                opened_at=now,
                lane_id=(execution_meta or {}).get("lane_id"),
            )
            self.db.insert_position(pos)
            positions.append(pos)

        mt5_sym = meta["mt5"]
        # seleciona o símbolo no Market Watch ANTES de enviar ordens — sem
        # isto o symbol_info_tick devolve None na primeira trade do símbolo
        # (crash 'NoneType' .ask) e o sinal ficava em awaiting_edit a repetir
        self.provider.ensure_symbol(signal.symbol)
        if not self.dry_run:
            if pending_style:
                sent, retcodes, failures = self._send_pending_orders(
                    trade, positions, mt5_sym)
            else:
                sent, retcodes, failures = self._send_open_orders(
                    trade, positions, mt5_sym)
            if sent == 0:
                # nothing reached the broker: don't leave phantom positions
                for pos in positions:
                    self.db.close_position(pos.id, 0.0, 0.0, now)
                    pos.status = "closed"
                self.db.close_trade(trade.id, now, 0.0)
                trade.status = "closed"
                trade.retcodes = retcodes
                if 10027 in retcodes:
                    trade.error = "AutoTrading desligado no terminal MT5 (retcode 10027)"
                    trade.auto_trading_off = True
                else:
                    trade.error = f"ordem MT5 recusada pelo broker (retcodes {retcodes})"
                    trade.auto_trading_off = False
                if failures:
                    # a falha real (ex.: worker caiu no envio) nunca se perde
                    trade.error += " — " + "; ".join(failures[:3])
                # trace durável: error/retcodes/auto_trading_off ficam na trade
                # (auditoria/replay — no passado só viviam em memória e o
                # Diagnóstico/A-B não conseguia explicar o fecho phantom)
                self.db.set_trade_refusal(
                    trade.id, trade.error, retcodes,
                    bool(trade.auto_trading_off),
                )
        else:
            self._log_order("open", {
                "trade": trade.id, "symbol": mt5_sym, "direction": signal.direction,
                "lots": {leg_num: lots[leg_num] for leg_num, _ in legs},
                "entry": signal.entry, "sl": signal.stop_loss,
                "tps": {leg_num: tp for leg_num, tp in legs},
                "note": "dry-run: ordem registada, não enviada ao broker",
            })
            # Dry-run has no broker retcode, but it still has a deterministic
            # simulated fill that must be visible in a forward evidence tape.
            for pos in positions:
                self._journal_event(
                    "fill_open", at=now, trade=trade, pos=pos,
                    payload={"source": "mt5_dry_run", "status": "simulated",
                             "price": pos.entry, "volume": pos.lots,
                             "leg": pos.leg, "stop_loss": pos.stop_loss,
                             "take_profit": pos.take_profit,
                             "signal_to_fill_seconds": _elapsed_seconds(
                                 self._signal_received_at(trade), now)},
                )
        return trade

    def _send_pending_orders(self, trade: Trade, positions: List[Position],
                             mt5_sym: str) -> tuple:
        """Fase 1 — ordens pendentes na zona: uma ordem pendente por perna
        no nível da entrada (BUY_LIMIT/SELL_LIMIT ou BUY_STOP/SELL_STOP,
        consoante o preço atual vs o nível), SL/TP anexados, GTC. O ticket de
        cada ordem fica na posição (order_ticket) para o tick reconhecer o
        fill/cancelamento. Devolve (sent, retcodes, failures) — `failures`
        carrega o texto real de exceções de envio (worker caiu, etc.)."""
        mt5 = self.provider._mt5
        entry = float(trade.entry)
        tick = mt5.symbol_info_tick(mt5_sym)
        if tick is None or not (tick.ask > 0 and tick.bid > 0):
            # sem tick não há como escolher limit vs stop — falha a trade
            for pos in positions:
                self.db.close_position(pos.id, 0.0, 0.0, self.clock.iso(),
                                       reason="order_rejected")
                pos.status = "closed"
            return 0, ["NO_TICK"], []
        sent = 0
        retcodes: List = []
        failures: List = []
        for pos in positions:
            request_at = self.clock.iso()
            self._journal_event(
                "pending_order_request", at=request_at, trade=trade, pos=pos,
                payload={"action": "open_pending", "leg": pos.leg,
                         "volume": pos.lots, "price": entry,
                         "stop_loss": pos.stop_loss,
                         "take_profit": pos.take_profit,
                         "signal_to_order_seconds": _elapsed_seconds(
                             self._signal_received_at(trade), request_at)},
            )
            if trade.direction == "buy":
                order_type = (mt5.ORDER_TYPE_BUY_LIMIT if tick.ask > entry
                              else mt5.ORDER_TYPE_BUY_STOP)
            else:
                order_type = (mt5.ORDER_TYPE_SELL_LIMIT if tick.bid < entry
                              else mt5.ORDER_TYPE_SELL_STOP)
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": mt5_sym,
                "volume": float(pos.lots),
                "type": order_type,
                "price": entry,
                "sl": float(pos.stop_loss),
                "tp": float(pos.take_profit),
                "deviation": 10,
                "magic": 36450341,
                "comment": f"{trade.id}:leg{pos.leg}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            try:
                result = mt5.order_send(request)
            except Exception as exc:
                # ordem que NUNCA chegou ao broker (ex.: worker lane 2 caiu
                # a meio do envio): grava a falha real, fecha só ESTA perna
                # e segue para as outras — sem exceção a desenrolar o
                # open_trade (que deixava a trade meia-aberta na BD)
                self.db.record_exception(
                    exc, context={"component": "mt5", "phase": "order_send",
                                  "leg": pos.leg, "lane": self.lane_id,
                                  "trade_id": trade.id},
                    trade_id=trade.id,
                )
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": "REMOTE_ERROR", "comment": str(exc)[:140],
                })
                self._journal_event(
                    "pending_order_result", at=self.clock.iso(),
                    trade=trade, pos=pos,
                    payload={"action": "open_pending", "accepted": False,
                             "retcode": "REMOTE_ERROR", "type": order_type,
                             "comment": str(exc)[:140]},
                )
                failures.append(f"leg{pos.leg}: {exc}")
                retcodes.append("REMOTE_ERROR")
                self.db.close_position(pos.id, 0.0, 0.0, self.clock.iso(),
                                       reason="order_rejected")
                pos.status = "closed"
                continue
            retcode = self._retcode(result)
            accepted = _order_ok(result, mt5)
            self._journal_event(
                "pending_order_result", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"action": "open_pending", "accepted": accepted,
                         "retcode": retcode, "type": order_type,
                         "order_ticket": getattr(result, "order", None),
                         "comment": getattr(result, "comment", "") or ""},
            )
            if not accepted:
                rc = retcode if retcode is not None else "?"
                retcodes.append(rc)
                self.db.close_position(pos.id, 0.0, 0.0, self.clock.iso(),
                                       reason="order_rejected")
                pos.status = "closed"
                continue
            sent += 1
            order_ticket = getattr(result, "order", None)
            if order_ticket:
                self.db.set_position_order_ticket(pos.id, int(order_ticket))
                pos.order_ticket = int(order_ticket)
        return sent, retcodes, failures

    def _send_open_orders(self, trade: Trade, positions: List[Position],
                          mt5_sym: str) -> tuple:
        """Real orders: one market order per leg, SL/TP set server-side.

        After each fill we resolve the position ticket by the unique comment
        (`trade.id:legN`) — the deal ticket returned by order_send is an
        *order* ticket, useless for position lookups later. Returns
        `(sent, retcodes, failures)` — the number of orders accepted, the
        retcode of every leg the broker refused (para o erro/alerta exato
        upstream) e o texto real de exceções de envio perna a perna.
        """
        mt5 = self.provider._mt5
        order_type = mt5.ORDER_TYPE_BUY if trade.direction == "buy" else mt5.ORDER_TYPE_SELL
        sent = 0
        retcodes: List = []
        failures: List = []
        for pos in positions:
            request_started = self._auto_clock.monotonic()
            request_at = self.clock.iso()
            self._journal_event(
                "order_request", at=request_at, trade=trade, pos=pos,
                payload={"action": "open", "source": "broker",
                         "leg": pos.leg, "volume": pos.lots,
                         "requested_price": None, "stop_loss": pos.stop_loss,
                         "take_profit": pos.take_profit,
                         "signal_to_order_seconds": _elapsed_seconds(
                             self._signal_received_at(trade), request_at)},
            )
            tick = mt5.symbol_info_tick(mt5_sym)
            if tick is None or not (tick.ask > 0 and tick.bid > 0):
                # símbolo ainda sem tick (fora do Market Watch / a carregar)
                # — falha esta perna em vez de crashar e repetir a trade toda
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": "NO_TICK",
                    "comment": "symbol_info_tick sem preço (símbolo fora do Market Watch?)",
                })
                self._journal_event(
                    "order_result", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "open", "accepted": False,
                             "retcode": "NO_TICK", "comment": "sem tick",
                             "latency_ms": round((self._auto_clock.monotonic() - request_started) * 1000, 3)},
                )
                retcodes.append("NO_TICK")
                self.db.close_position(
                    pos.id, 0.0, 0.0, self.clock.iso(), reason="order_rejected",
                )
                pos.status = "closed"
                continue
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": mt5_sym,
                "volume": float(pos.lots),
                "type": order_type,
                "price": tick.ask if trade.direction == "buy" else tick.bid,
                "sl": float(pos.stop_loss),
                "tp": float(pos.take_profit),
                "deviation": 10,
                "magic": 36450341,
                "comment": f"{trade.id}:leg{pos.leg}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            try:
                result = mt5.order_send(request)
            except Exception as exc:
                # ordem que NUNCA chegou ao broker (ex.: worker lane 2 caiu a
                # meio do envio): grava a falha REAL e fecha só esta perna —
                # sem embrulhar o open_trade (deixava a trade meia-aberta).
                self.db.record_exception(
                    exc, context={"component": "mt5", "phase": "order_send",
                                  "leg": pos.leg, "lane": self.lane_id,
                                  "trade_id": trade.id},
                    trade_id=trade.id,
                )
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": "REMOTE_ERROR", "comment": str(exc)[:140],
                })
                self._journal_event(
                    "order_result", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "open", "accepted": False,
                             "retcode": "REMOTE_ERROR",
                             "comment": str(exc)[:140],
                             "signal_to_order_seconds": _elapsed_seconds(
                                 self._signal_received_at(trade), request_at),
                             "latency_ms": round((self._auto_clock.monotonic() - request_started) * 1000, 3)},
                )
                failures.append(f"leg{pos.leg}: {exc}")
                retcodes.append("REMOTE_ERROR")
                self.db.close_position(
                    pos.id, 0.0, 0.0, self.clock.iso(), reason="order_rejected",
                )
                pos.status = "closed"
                continue
            retcode = self._retcode(result)
            accepted = _order_ok(result, mt5)
            self._journal_event(
                "order_result", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"action": "open", "accepted": accepted,
                         "retcode": retcode,
                         "comment": getattr(result, "comment", "") or "",
                         "order_ticket": getattr(result, "order", None),
                         "deal_ticket": getattr(result, "deal", None),
                         "broker_price": getattr(result, "price", None),
                         "signal_to_order_seconds": _elapsed_seconds(
                             self._signal_received_at(trade), request_at),
                         "latency_ms": round((self._auto_clock.monotonic() - request_started) * 1000, 3)},
            )
            if not accepted:
                rc = retcode if retcode is not None else "?"
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": rc,
                    "comment": getattr(result, "comment", ""),
                })
                retcodes.append(rc)
                self.db.close_position(
                    pos.id, 0.0, 0.0, self.clock.iso(), reason="order_rejected",
                )
                pos.status = "closed"
                continue
            sent += 1  # the broker accepted the order — a real fill happened
            # resolve the position ticket from the just-filled order
            ticket = None
            try:
                for p in (mt5.positions_get(symbol=mt5_sym) or []):
                    if f"{trade.id}:leg{pos.leg}" == (getattr(p, "comment", "") or ""):
                        ticket = p.ticket
                        break
            except Exception:
                pass
            if ticket:
                self._ticket_pos[ticket] = pos.id
                self.db.set_config(f"mt5ticket:{ticket}", pos.id)
                # record the real fill price so entries/P&L match the broker
                try:
                    for p in (mt5.positions_get(symbol=mt5_sym) or []):
                        if p.ticket == ticket:
                            self.db.update_position_entry(pos.id, float(p.price_open))
                            pos.entry = float(p.price_open)
                            break
                except Exception:
                    pass
            self._log_order("sent", {
                "trade": trade.id, "leg": pos.leg, "ticket": ticket,
                "symbol": mt5_sym, "volume": pos.lots, "sl": pos.stop_loss,
                "tp": pos.take_profit,
            })
            fill_price = pos.entry
            if ticket:
                try:
                    live_pos = next((p for p in (mt5.positions_get(symbol=mt5_sym) or [])
                                     if p.ticket == ticket), None)
                    if live_pos is not None:
                        fill_price = float(live_pos.price_open)
                except Exception:
                    pass
            self._journal_event(
                "fill_open", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"source": "broker", "status": "filled", "leg": pos.leg,
                         "ticket": ticket, "price": fill_price, "volume": pos.lots,
                         "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                         "order_to_fill_ms": round((self._auto_clock.monotonic() - request_started) * 1000, 3),
                         "signal_to_fill_seconds": _elapsed_seconds(
                             self._signal_received_at(trade), self.clock.iso())},
            )
        return sent, retcodes, failures

    # --------------------------------------------------------------- update

    def update(self, now: Optional[str] = None,
               exit_at_tp: Optional[int] = None,
               auto: Optional[dict] = None,
               entry_order_ttl: Optional[float] = None) -> List[dict]:
        """Evaluate every open trade against live prices / MT5 positions.

        `exit_at_tp` (estratégias do registo): quando o TP com esse número
        bater, TODAS as pernas ainda abertas fecham nesse preço.

        `auto` (Fase 3 — auto-gestão, docs/ESTRATEGIAS.md Parte X):
        dicionário com os parâmetros da estratégia (`stair_levels`,
        `stair_from`, `confirm_filter`, `trail_factor`, `time_stop_minutes`).
        Só é passado quando a estratégia ativa os define — a `nova_gestao`
        não tem nenhum (auto_mgmt desligada), por isso este código fica
        inerte até uma estratégia auto_* ser promovida. Em dry-run a
        auto-gestão é a do `PaperRace` (corrida paper) — aqui só o modo real
        (broker) executa escada/trailing.

        `entry_order_ttl` (segundos): expira ordens pendentes da zona (Fase 1)
        que não preencheram.

        Same event contract as SimEngine: {"type": "close"|"be"|"trade_closed"}.
        """
        now = now or self.clock.iso()
        events: List[dict] = []

        # SLs de gestão de grupo em fila (modify recusado antes): re-tenta
        # ANTES da reconciliação — o nível pode ter ficado válido desde o
        # último tick (o preço afastou-se do nível do protect)
        if not self.dry_run:
            events += self._retry_pending_sl_moves(now)

        trades = self.db.open_trades()
        for trade in trades:
            if (getattr(trade, "lane_id", None) or "main") != self.lane_id:
                continue
            if not self.dry_run:
                events += self._update_real(
                    trade, now, exit_at_tp, auto, entry_order_ttl)
            else:
                events += self._update_dry(
                    trade, now, exit_at_tp, auto, entry_order_ttl)
        # o tick por trade só corre para trades abertas → limpa o estado
        # persistido do watchdog de pernas de trades já encerradas (a BD
        # nunca guarda linhas órfãs de episódios resolvidos)
        if self._pending_delete_failures:
            open_ids = {t.id for t in trades}
            for pos_id in list(self._pending_delete_failures):
                trade_id = self._pending_delete_failures[pos_id].get("trade_id")
                if trade_id and trade_id not in open_ids:
                    self._clear_pending_delete_retry(pos_id)
        # drena os avisos de SL recusado/clamped para os eventos do update
        events += self._sl_warnings
        self._sl_warnings = []
        # pernas que fecharam NESTE tick limpam as suas intenções de SL em
        # fila (a verificação do início do tick corre antes da reconciliação)
        if not self.dry_run:
            self._clear_stale_sl_moves()
        return events

    def _update_dry(self, trade: Trade, now: str,
                    exit_at_tp: Optional[int] = None,
                    auto: Optional[dict] = None,
                    entry_order_ttl: Optional[float] = None) -> List[dict]:
        """Dry-run: simulate fills at the current MT5 tick (or feed price)."""
        events: List[dict] = []
        trade_reason: Optional[str] = None
        price = self._price(trade.symbol)
        if price is None:
            return events

        # Fase 1 — ordens pendentes na zona (dry-run simula o crossing)
        events += self._process_pending_dry(trade, price, now, entry_order_ttl)

        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        legs = [positions[n] for n in range(1, 6) if n in positions]
        open_legs = [p for p in legs if p.status == "open"]
        if not open_legs:
            return events

        leg1 = positions.get(1)
        auto_state = self._restore_auto_state(trade, positions, now) if auto else {}
        if leg1 and leg1.status == "open":
            if self._hit(trade, price, trade.stop_loss, "sl"):

                trade_reason = "sl"
                for p in open_legs:
                    events.append(self._close_dry(
                        trade, p, trade.stop_loss, now, reason="sl",
                    ))
            elif self._hit(trade, price, trade.take_profit_1, "tp"):
                trade_reason = "tp1"
                events.append(self._close_dry(
                    trade, leg1, trade.take_profit_1, now, reason="tp1",
                ))
                moved = False
                be_buffer = float((auto or {}).get("be_buffer_pips") or 0.0)
                for p in open_legs:
                    if p.leg != 1:
                        be_level = self._be_level(
                            trade, be_buffer, fill=p.entry)
                        self.db.move_position_sl(p.id, be_level)
                        p.stop_loss = be_level
                        moved = True
                if moved:
                    events.append({
                        "type": "be", "trade_id": trade.id,
                        "symbol": trade.symbol, "price": trade.entry,
                    })

        # Autonomous early protect (generic policy for future candidates;
        # auto_protect_5 was removed from the registry on 01/09): the same
        # pure reducer as the backtest and paper path. Dry-run applies
        # accepted decisions to SQLite immediately because there is no broker
        # modify to confirm.
        if auto and leg1:
            decision = reduce_auto_protect(
                direction=trade.direction, price=price, entry=trade.entry,
                stop_loss=trade.stop_loss, tp1=trade.take_profit_1,
                leg1_open=leg1.status == "open",
                remaining_sls={p.leg: p.stop_loss for p in open_legs if p.leg != 1},
                trigger_fraction=auto.get("protect_trigger"),
                buffer_fraction=auto.get("protect_buffer"),
                already_triggered=bool(auto_state.get("auto_protected")),
            )
            if decision.triggered:
                events.append(self._close_dry(
                    trade, leg1, decision.trigger_level, now,
                    reason="auto_protect",
                ))
                for leg, improved in decision.move_sls:
                    pos = positions.get(leg)
                    if pos and pos.status == "open":
                        self.db.move_position_sl(pos.id, improved)
                        pos.stop_loss = improved
                auto_state["auto_protected"] = True
                auto_state["auto_protection_at"] = now

        # exit_at_tp: quando o TP_N bater, fecha TUDO nesse preço
        if exit_at_tp is not None:
            tp_level = getattr(trade, f"take_profit_{exit_at_tp}", 0.0) or 0.0
            if tp_level and self._hit(trade, price, tp_level, "tp"):
                trade_reason = f"tp{exit_at_tp}"
                for p in open_legs:
                    if p.status == "open":
                        events.append(self._close_dry(
                            trade, p, tp_level, now, reason=trade_reason,
                        ))

        for p in open_legs:
            if p.leg == 1 or p.status != "open":
                continue
            if self._hit(trade, price, p.stop_loss, "sl"):
                trade_reason = sl_exit_reason(trade.direction, p.stop_loss, trade.entry)
                events.append(self._close_dry(
                    trade, p, p.stop_loss, now, reason=trade_reason,
                ))
            elif self._hit(trade, price, p.take_profit, "tp"):
                trade_reason = f"tp{p.leg}"
                events.append(self._close_dry(
                    trade, p, p.take_profit, now, reason=trade_reason,
                ))

        events += self._maybe_close_trade(trade, now, reason=trade_reason)
        if auto and trade.status == "open":
            self.db.save_protection_state("mt5", trade.id, auto_state, updated_at=now)
        return events

    def _process_pending_dry(self, trade: Trade, price: float, now: str,
                             entry_order_ttl: Optional[float]) -> List[dict]:
        """Fase 1 — ordens pendentes em dry-run: fill por crossing do preço
        no nível da entrada (mesmo critério do SimEngine); expira por TTL."""
        events: List[dict] = []
        placement = float(trade.market_price_at_open or trade.entry)
        entry = float(trade.entry)
        for pos in self.db.positions_for_trade(trade.id):
            if pos.status != "pending_fill":
                continue
            filled = False
            if trade.direction == "buy":
                filled = (price <= entry and placement > entry) \
                    or (price >= entry and placement < entry)
            else:
                filled = (price >= entry and placement < entry) \
                    or (price <= entry and placement > entry)
            if filled:
                self.db.fill_position(pos.id, now)
                pos.status = "open"
                pos.opened_at = now
                self._journal_event(
                    "fill_open", at=now, trade=trade, pos=pos,
                    payload={"source": "mt5_dry_run_pending", "status": "simulated",
                             "price": entry, "volume": pos.lots,
                             "leg": pos.leg, "stop_loss": pos.stop_loss,
                             "take_profit": pos.take_profit,
                             "signal_to_fill_seconds": _elapsed_seconds(
                                 self._signal_received_at(trade), now)},
                )
                events.append({
                    "type": "pending_fill", "trade_id": trade.id,
                    "symbol": trade.symbol, "leg": pos.leg, "price": entry,
                    "lots": pos.lots,
                })
            elif entry_order_ttl and entry_order_ttl > 0:
                age = _elapsed_seconds(pos.opened_at, now)
                if age is not None and age > entry_order_ttl:
                    self.db.close_position(pos.id, 0.0, 0.0, now,
                                           reason="pending_expired")
                    pos.status = "closed"
                    events.append({
                        "type": "pending_expired", "trade_id": trade.id,
                        "symbol": trade.symbol, "leg": pos.leg,
                    })
        return events

    # ------------------------------------------------- pending delete retry

    def _bump_pending_delete_failure(self, pos: Position) -> int:
        """Soma uma falha de `order_delete` à perna (watchdog
        `pending_delete_stuck`). Guarda o contexto (leg/trade/símbolo/
        ticket/open) para o Diagnóstico (`stuck_pending_legs`) e devolve o
        total de falhas CONSECUTIVAS. Persiste na BD — o estado do watchdog
        sobrevive a restart."""
        entry = self._pending_delete_failures.get(pos.id) or {}
        entry.update({
            "leg": pos.leg, "trade_id": pos.trade_id, "symbol": pos.symbol,
            "ticket": pos.order_ticket,
            "opened_at": (pos.opened_at or ""),
        })
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        self._pending_delete_failures[pos.id] = entry
        self._persist_pending_delete_state(pos.id)
        return int(entry["attempts"])

    def _persist_pending_delete_state(self, pos_id: str) -> None:
        """Serializa o estado do watchdog da perna para a BD
        (tentativas/intenção/alertas). Best-effort: falhas de escrita nunca
        bloqueiam o tick nem mudam o comportamento do retry."""
        entry = self._pending_delete_failures.get(pos_id)
        if not entry:
            return
        row = dict(entry)
        row["intent"] = self._pending_delete_intent.get(pos_id) or None
        try:
            self.db.save_pending_delete_state(self.lane_id, pos_id, row)
        except Exception:
            pass

    def _clear_pending_delete_retry(self, pos_id: str) -> None:
        """Fim do retry da perna: o delete conseguiu ou a perna deixou de
        estar pendente — esvazia o estado do watchdog (memória + BD)."""
        self._pending_delete_failures.pop(pos_id, None)
        self._pending_delete_alerted_at.pop(pos_id, None)
        self._pending_delete_intent.pop(pos_id, None)
        self._pending_delete_exception_recorded.discard(pos_id)
        try:
            self.db.delete_pending_delete_state(self.lane_id, pos_id)
        except Exception:
            pass

    def stuck_pending_legs(self) -> List[dict]:
        """Pernas `pending_fill` em retry de delete (worker offline?) —
        read-only para o Diagnóstico: tentativas, escalonada?, ticket,
        idade e trade. Vazio = nada preso."""
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        out: List[dict] = []
        for pos_id, entry in self._pending_delete_failures.items():
            attempts = int(entry.get("attempts") or 0)
            age = None
            try:
                opened = datetime.fromisoformat(str(entry.get("opened_at") or ""))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age = int((now_utc - opened).total_seconds())
            except (ValueError, TypeError):
                pass
            out.append({
                "position_id": pos_id,
                "trade_id": entry.get("trade_id") or "",
                "symbol": entry.get("symbol") or "",
                "leg": entry.get("leg"),
                "ticket": entry.get("ticket"),
                "attempts": attempts,
                "escalated": attempts >= self.PENDING_DELETE_ESCALATE_AFTER,
                "age_seconds": age,
                "lane": self.lane_id,
                "intent_kind": (self._pending_delete_intent.get(pos_id) or {}).get("kind"),
                "last_alert_utc": entry.get("last_alert_utc") or "",
            })
        out.sort(key=lambda d: -(d["attempts"] or 0))
        return out

    def _check_pending_real(self, trade: Trade, positions: dict, mt5, now: str,
                            entry_order_ttl: Optional[float]) -> List[dict]:
        """Fase 1 — reconcilia as ordens pendentes reais com o broker.

        Por perna `pending_fill`: preenchida (ORDER_STATE_FILLED) → a posição
        passa a `open` com o fill real; cancelada/expirada pelo broker →
        fecha na BD; ainda ativa além do TTL → cancela (order_delete). Uma
        ordem sem ticket (restart antes do envio) expira como órfã. Pernas
        com delete FALHADO antes (intenção guardada) re-tentam o cancelamento
        em cada tick — assim que o worker voltar, o delete é completado no
        mesmo tick, sem esperar o TTL.
        """
        events: List[dict] = []
        pending = [p for p in positions.values() if p.status == "pending_fill"]
        # perna escalonada ANTES de um restart volta a alertar no 1º retry
        # pós-reboot (o monotónico não sobrevive ao restart, e o limiar de
        # escalada pode ter mudado entre sessões — a marca `escalated_at` é
        # o veredicto durável)
        if self._pending_delete_failures:
            for pos_id in list(self._pending_delete_failures):
                entry = self._pending_delete_failures[pos_id]
                if (entry.get("escalated_at")
                        and pos_id not in self._pending_delete_alerted_at
                        and int(entry.get("attempts") or 0)
                        > self.PENDING_DELETE_ESCALATE_AFTER):
                    self._pending_delete_alerted_at[pos_id] = 0.0

        # limpa o estado persistido do watchdog de pernas que já não estão
        # pendentes (fill/fecho/cancelamento enquanto o servidor esteve em
        # baixo) — nada fica órfão na BD nem no Diagnóstico
        if self._pending_delete_failures:
            by_id = {p.id: p for p in positions.values()}
            for pos_id in list(self._pending_delete_failures):
                pos = by_id.get(pos_id)
                if pos is None or pos.status != "pending_fill":
                    self._clear_pending_delete_retry(pos_id)
        if not pending:
            return events
        mt5_sym = self.provider.meta(trade.symbol)["mt5"]
        for pos in pending:
            if not pos.order_ticket:
                self._clear_pending_delete_retry(pos.id)
                self.db.close_position(pos.id, 0.0, 0.0, now,
                                       reason="pending_orphan")
                pos.status = "closed"
                events.append({"type": "pending_expired", "trade_id": trade.id,
                                "symbol": trade.symbol, "leg": pos.leg,
                                "reason": "orphan"})
                continue
            try:
                orders = mt5.orders_get(ticket=pos.order_ticket) or []
            except Exception as exc:
                # rate-limit por perna: o worker caído faz falhar TODOS os
                # ticks — só a primeira falha do episódio grava a exceção
                if pos.id not in self._pending_delete_exception_recorded:
                    self._pending_delete_exception_recorded.add(pos.id)
                    self.db.record_exception(
                        exc, context={"component": "mt5", "phase": "orders_get",
                                      "trade_id": trade.id},
                        trade_id=trade.id,
                    )
                orders = []
            order = orders[0] if orders else None
            state = getattr(order, "state", None) if order else None
            retry_intent = self._pending_delete_intent.get(pos.id)
            if state == getattr(mt5, "ORDER_STATE_FILLED", None):
                # preencheu: sincroniza com a posição real do broker
                live = mt5.positions_get(symbol=mt5_sym) or []
                for lp in live:
                    comment = getattr(lp, "comment", "") or ""
                    if f"{trade.id}:leg{pos.leg}" == comment:
                        self._clear_pending_delete_retry(pos.id)
                        self.db.fill_position(pos.id, now)
                        pos.status = "open"
                        pos.opened_at = now
                        events.append({"type": "pending_fill",
                                        "trade_id": trade.id,
                                        "symbol": trade.symbol, "leg": pos.leg,
                                        "price": float(pos.entry),
                                        "lots": pos.lots})
                        break
            elif state in (getattr(mt5, "ORDER_STATE_CANCELED", None),
                           getattr(mt5, "ORDER_STATE_EXPIRED", None),
                           getattr(mt5, "ORDER_STATE_REJECTED", None)):
                self._clear_pending_delete_retry(pos.id)
                self.db.close_position(pos.id, 0.0, 0.0, now,
                                       reason="pending_cancelled")
                pos.status = "closed"
                events.append({"type": "pending_expired", "trade_id": trade.id,
                                "symbol": trade.symbol, "leg": pos.leg,
                                "reason": "cancelled"})
            elif retry_intent is not None or (entry_order_ttl and entry_order_ttl > 0):
                age = _elapsed_seconds(pos.opened_at, now)
                over_ttl = bool(entry_order_ttl and entry_order_ttl > 0
                                and age is not None and age > entry_order_ttl)
                if over_ttl or retry_intent is not None:
                    try:
                        mt5.order_delete(ticket=pos.order_ticket)
                    except Exception as exc:
                        # delete sem sucesso (ex.: worker da lane caiu no
                        # caminho): fechar aqui na BD deixaria a ordem
                        # pendente ÓRFÃ no MT5 — mantém `pending_fill` e o
                        # próximo tick repete o cancelamento (mesmo
                        # tratamento por perna do envio em
                        # `_send_pending_orders`). A intenção do delete fica
                        # GUARDADA E PERSISTIDA — o tick re-tenta assim que
                        # o worker voltar e a intenção sobrevive a restart
                        # (a gestão falhada completa-se com a razão original,
                        # não como expiração TTL).
                        if not self._pending_delete_intent.get(pos.id):
                            self._pending_delete_intent[pos.id] = {"kind": "ttl"}
                        self._persist_pending_delete_state(pos.id)
                        # rate-limit por perna: o tick falha a cada segundo
                        # enquanto o worker estiver em baixo — só a PRIMEIRA
                        # falha do episódio grava a exceção (concisa, com o
                        # trace); as restantes ficam só no log/feed/journal
                        if pos.id not in self._pending_delete_exception_recorded:
                            self._pending_delete_exception_recorded.add(pos.id)
                            self.db.record_exception(
                                exc,
                                context={"component": "mt5",
                                         "phase": "order_delete",
                                         "kind": "pending_ttl",
                                         "leg": pos.leg, "lane": self.lane_id,
                                         "trade_id": trade.id,
                                         "ticket": pos.order_ticket,
                                         "episode": True},
                                trade_id=trade.id,
                            )
                        self._log_order("pending_delete_failed", {
                            "trade": trade.id, "leg": pos.leg,
                            "ticket": pos.order_ticket,
                            "retcode": "REMOTE_ERROR",
                            "comment": str(exc)[:140],
                        })
                        self._journal_event(
                            "pending_order_result", at=self.clock.iso(),
                            trade=trade, pos=pos,
                            payload={"action": "delete", "accepted": False,
                                     "retcode": "REMOTE_ERROR",
                                     "ticket": pos.order_ticket,
                                     "comment": str(exc)[:140]},
                        )
                        # watchdog: após N falhas seguidas o tick deixa de
                        # tentar em silêncio — devolve o evento para o feed
                        # alertar o operador (worker offline?); enquanto
                        # presa, re-alerta a cada REALERT_EVERY segundos
                        self._bump_pending_delete_failure(pos)
                        n = int(self._pending_delete_failures[pos.id]["attempts"])
                        now_mono = self.clock.monotonic()
                        last_alert = self._pending_delete_alerted_at.get(pos.id)
                        first_alert = n == self.PENDING_DELETE_ESCALATE_AFTER
                        re_alert = (n > self.PENDING_DELETE_ESCALATE_AFTER
                                    and last_alert is not None
                                    and (now_mono - last_alert)
                                    >= self.PENDING_DELETE_REALERT_EVERY)
                        if first_alert or re_alert:
                            self._pending_delete_alerted_at[pos.id] = now_mono
                            entry = self._pending_delete_failures[pos.id]
                            if not entry.get("escalated_at"):
                                entry["escalated_at"] = now
                            entry["last_alert_utc"] = now
                            self._persist_pending_delete_state(pos.id)
                            intent = self._pending_delete_intent.get(pos.id) or {}
                            events.append({
                                "type": "pending_delete_stuck",
                                "trade_id": trade.id,
                                "symbol": trade.symbol, "leg": pos.leg,
                                "ticket": pos.order_ticket,
                                "attempts": n,
                            })
                            # evidência completa no journal: a sequência de
                            # `pending_order_result` (falhas do delete) + a
                            # marca `pending_delete_stuck` com o contexto
                            # da perna reproduzem uma falha prolongada do
                            # worker de forma determinística no replay
                            self._journal_event(
                                "pending_delete_stuck", at=now,
                                trade=trade, pos=pos,
                                payload={"leg": pos.leg,
                                         "position_id": pos.id,
                                         "ticket": pos.order_ticket,
                                         "lane": self.lane_id,
                                         "attempts": n,
                                         "intent_kind": intent.get("kind") or "ttl",
                                         "reason": intent.get("reason"),
                                         "escalated": True,
                                         "re_alert": bool(re_alert),
                                         "escalated_at": entry.get("escalated_at"),
                                         "last_alert_utc": entry.get("last_alert_utc"),
                                         "opened_at": pos.opened_at or "",
                                         "comment": str(exc)[:200]},
                            )
                        continue
                    self._journal_event(
                        "pending_order_result", at=self.clock.iso(),
                        trade=trade, pos=pos,
                        payload={"action": "delete", "accepted": True,
                                 "retcode": 10009,
                                 "ticket": pos.order_ticket},
                    )
                    self._clear_pending_delete_retry(pos.id)
                    if retry_intent is not None \
                            and retry_intent.get("kind") == "cancel":
                        # delete de GESTÃO que tinha falhado — completa-o
                        # agora (mesmo evento do `_cancel_pending_pos`)
                        reason = retry_intent.get("reason") or "close_all"
                        self.db.close_position(pos.id, pos.entry, 0.0, now,
                                               reason=reason,
                                               msg_id=retry_intent.get("msg_id"))
                        pos.status = "closed"
                        pos.exit_reason = reason
                        pos.exit_msg_id = retry_intent.get("msg_id")
                        events.append({
                            "type": "close", "trade_id": trade.id,
                            "symbol": trade.symbol, "leg": pos.leg,
                            "direction": pos.direction, "price": pos.entry,
                            "pnl": 0.0, "reason": reason,
                        })
                        continue
                    self.db.close_position(pos.id, 0.0, 0.0, now,
                                           reason="pending_expired")
                    pos.status = "closed"
                    events.append({"type": "pending_expired", "trade_id": trade.id,
                                    "symbol": trade.symbol, "leg": pos.leg,
                                    "reason": "ttl"})
        return events

    def _update_real(self, trade: Trade, now: str,
                     exit_at_tp: Optional[int] = None,
                     auto: Optional[dict] = None,
                     entry_order_ttl: Optional[float] = None) -> List[dict]:
        """Real mode: reconcile DB legs with live MT5 positions.

        Positions carry server-side SL/TP, so the broker manages the exits;
        this code only records what happened, protects leg 2 at breakeven
        after leg 1 hits TP1, and (Fase 3) aplica a **auto-gestão** quando a
        estratégia ativa a define (`auto`): escada de proteção, trailing sem
        look-ahead, filtro de confirmação e time stop — espelho do backtest
        e do PaperRace, com throttling de modify e os três níveis de SL.
        """
        events: List[dict] = []
        mt5 = self.provider._mt5
        if not self.provider.connected:
            return events

        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        if not positions:
            return events

        # Fase 1 — ordens pendentes na zona: fill/cancelamento/expiração
        # ANTES da reconciliação das posições abertas.
        events += self._check_pending_real(trade, positions, mt5, now,
                                           entry_order_ttl)

        legs = [positions[n] for n in range(1, 6) if n in positions]
        open_legs = [p for p in legs if p.status == "open"]
        leg1 = positions.get(1)
        auto_policy = None
        if auto:
            auto_policy = AutoProtectionPolicy(
                direction=trade.direction, entry=trade.entry,
                tp1=getattr(trade, "take_profit_1", 0.0) or 0.0,
                tp2=getattr(trade, "take_profit_2", 0.0) or 0.0,
                trail_factor=auto.get("trail_factor"),
                stair_levels=auto.get("stair_levels"),
                stair_from=int(auto.get("stair_from") or 1),
                be_buffer_pips=auto.get("be_buffer_pips"),
                pip_size=float((self._sym_meta(trade.symbol) or {}).get("pip", 0.01)),
            )

        try:
            live = mt5.positions_get(symbol=self.provider.meta(trade.symbol)["mt5"])
        except Exception as exc:
            self.db.record_exception(
                exc,
                context={"component": "mt5", "phase": "positions_get", "trade_id": trade.id},
                trade_id=trade.id,
            )
            print(f"[mt5] positions_get falhou: {exc}")
            return events
        live = live or []

        # match MT5 tickets to our legs
        ticket_to_leg = {}
        for p in live:
            comment = getattr(p, "comment", "") or ""
            if trade.id in comment:
                leg = int(comment.split("leg")[-1]) if "leg" in comment else 0
                ticket_to_leg[p.ticket] = leg

        # a leg is missing from the broker: only close it in the DB when the
        # broker *proves* it closed it (an exit deal exists). A transient
        # empty positions_get (right after the fill) must not fake a close.
        closed_legs = {}
        for pos in legs:
            if pos.status == "open" and pos.leg not in ticket_to_leg.values():
                res = self._broker_close_via_deals(pos, mt5)
                if res is not None:
                    closed_legs[pos.leg] = res

        auto_state = self._restore_auto_state(trade, positions, now)
        # BE autónomo (15/09): quando o PREÇO toca o TP1, as pernas restantes
        # vão logo para BE+ — sem esperar pelo fill server-side do TP nem pela
        # mensagem "TP1 Hit" do grupo (no sinal das 15:37 o grupo soube do
        # TP1 43s antes do tick e o BE só aconteceu no fill). É monotónico:
        # `_auto_move_sl` só aplica se melhorar o SL atual, portanto corre em
        # todos os ticks sem repetir modifies. Requer `be_buffer_pips` na
        # estratégia (tp2_protect) — estratégias sem BE+ ficam inalteradas.
        if auto and auto.get("be_buffer_pips") and open_legs:
            tp1_level = getattr(trade, "take_profit_1", 0.0) or 0.0
            current_price = self._price(trade.symbol)
            tp1_touched = (
                tp1_level and current_price is not None
                and ((trade.direction == "buy" and current_price >= tp1_level)
                     or (trade.direction == "sell" and current_price <= tp1_level))
            )
            if tp1_touched:
                be_buffer = float(auto.get("be_buffer_pips") or 0.0)
                for pos in open_legs:
                    if pos.leg == 1:
                        continue
                    be_level = self._be_level(trade, be_buffer, fill=pos.entry)
                    self._auto_move_sl(trade, pos, mt5, be_level, kind="be")
        # Early autonomous protect: close leg 1 at the current broker price,
        # then apply the shared group-protect SL to the remaining legs. The
        # broker confirmation path in `_auto_move_sl` is the source of truth;
        # a refused modify never becomes an applied DB level.
        if auto and leg1 and leg1.status == "open":
            current_price = self._price(trade.symbol)
            decision = reduce_auto_protect(
                direction=trade.direction, price=current_price or trade.entry,
                entry=trade.entry, stop_loss=trade.stop_loss,
                tp1=trade.take_profit_1,
                leg1_open=leg1.status == "open",
                remaining_sls={p.leg: p.stop_loss for p in open_legs if p.leg != 1},
                trigger_fraction=auto.get("protect_trigger"),
                buffer_fraction=auto.get("protect_buffer"),
                already_triggered=bool(auto_state.get("auto_protected")),
            ) if current_price is not None else None
            if (decision and decision.triggered
                    and leg1.leg in ticket_to_leg.values()):
                close_event = self.close_leg_at_market(
                    trade.id, 1, now=now, reason="auto_protect",
                )
                if close_event:
                    events.append(close_event)
                    for leg, target in decision.move_sls:
                        other = positions.get(leg)
                        if other and other.status == "open":
                            self._auto_move_sl(
                                trade, other, mt5, target,
                                kind="auto_protect",
                            )
                    auto_state["auto_protected"] = True
                    auto_state["auto_protection_at"] = now
        tp1_ts = auto_state.get("tp1_ts")
        for leg_num, (price, pnl) in closed_legs.items():
            pos = positions[leg_num]
            reason = "tp" if pnl >= 0 else "sl"
            events.append(self._record_close(trade, pos, price, pnl, now,
                                             reason=reason))
            if leg_num == 1 and pnl >= 0:
                # leg 1 closed at TP1 (profit): protect every remaining leg
                # at breakeven+ (entry ± buffer quando a estratégia define
                # `be_buffer_pips` — cobre o spread; entrada seca sem buffer)
                moved = False
                be_buffer = float((auto or {}).get("be_buffer_pips") or 0.0)
                for other in open_legs:
                    if other.leg != 1:
                        self._move_sl_breakeven(trade, other, mt5,
                                                buffer_pips=be_buffer)
                        moved = True
                if moved:
                    events.append({
                        "type": "be", "trade_id": trade.id,
                        "symbol": trade.symbol, "price": trade.entry,
                    })
                # Fase 3: TP1 marca o início da gestão adaptativa (trailing
                # começa a seguir o preço a partir daqui)
                if auto:
                    self._auto_state.setdefault(trade.id, {})["tp1_ts"] = now
                    tp1_ts = now
            # Fase 3 — escada de proteção: TP_k conquistado vira o novo chão
            # das pernas seguintes (SL sobe para o NÍVEL do TP_k)
            if auto and pnl >= 0 and leg_num <= int(auto.get("stair_levels") or 0):
                tp_level = getattr(trade, f"take_profit_{leg_num}", 0.0) or 0.0
                for other in open_legs:
                    if other.leg > leg_num and tp_level and auto_policy:
                        target = auto_policy.stair_target(
                            other.stop_loss, tp_level, other.leg,
                        )
                        if target is not None:
                            self._auto_move_sl(trade, other, mt5, level=target,
                                               stair=True)
            # Fase 3 — filtro de confirmação: perna k levou SL (movimento
            # morreu) → as pernas > k fecham ao mercado
            if auto and pnl < 0 and auto.get("confirm_filter"):
                open_leg_numbers = [p.leg for p in open_legs
                                    if p.status == "open"]
                for other_leg in confirmation_legs(leg_num, open_leg_numbers):
                    other = positions[other_leg]
                    if other.status == "open":
                        ev = self.close_leg_at_market(trade.id, other_leg, now,
                                                      reason="confirm")
                        if ev:
                            events.append(ev)
            # exit_at_tp: quando a perna do TP_N fecha (pelo broker) com lucro,
            # fecha também as pernas ainda abertas ao mercado
            if exit_at_tp is not None and leg_num == exit_at_tp and pnl >= 0:
                for other in open_legs:
                    if other.status == "open" and other.leg != exit_at_tp:
                        ev = self.close_leg_at_market(trade.id, other.leg, now)
                        if ev:
                            events.append(ev)

        # Fase 3 — modo defensivo (estudo 19/08): quando o PF das últimas
        # `def_window` trades REAIS fechadas < 1.0, a estratégia passa a trail
        # apertado + time stop SEM cortar pernas (a história é a da própria
        # conta real — o que a Fase 3 realmente vê). Recalculado a cada tick.
        auto_eff = auto
        if auto and auto.get("def_window"):
            pnls = self.db.recent_closed_pnls(int(auto["def_window"]))
            if len(pnls) >= int(auto["def_window"]) and self._pf_below(pnls, 1.0):
                auto_eff = dict(auto)
                if auto.get("def_trail") is not None:
                    auto_eff["trail_factor"] = auto["def_trail"]
                if auto.get("def_time_stop") is not None:
                    auto_eff["time_stop_minutes"] = auto["def_time_stop"]

        # Fase 3 — trailing: depois do TP1, o SL das pernas restantes segue o
        # pico do preço (ticks ANTERIORES — sem look-ahead), com throttling
        if auto_eff and tp1_ts and auto_eff.get("trail_factor") and open_legs:
            self._auto_trailing(trade, open_legs, mt5, auto_eff)

        # Fase 3 — time stop: pernas abertas N min após o TP1 fecham ao
        # mercado (o mercado estagnou)
        if auto_eff and tp1_ts and auto_eff.get("time_stop_minutes"):
            self._auto_time_stop(trade, open_legs, mt5, auto_eff, tp1_ts, now)

        events += self._maybe_close_trade(trade, now)
        if auto and trade.status == "open":
            self.db.save_protection_state("mt5", trade.id, auto_state, updated_at=now)
        return events

    def reconcile_live_state(self) -> dict:
        """Read-only reconciliation of DB open legs against broker positions.

        It never closes, opens or modifies an order. A missing broker position
        is only reported as a discrepancy because an exit deal may still be
        arriving; the normal `_update_real` path remains the authority that
        records a close after it sees that deal.
        """
        if self.dry_run:
            return {"ok": True, "mode": "dry-run", "safe": True,
                    "checked": 0, "missing": [], "unknown": [],
                    "sl_mismatches": []}
        if not self.provider.connected:
            return {"ok": False, "mode": "mt5-real", "safe": False,
                    "reason": "MT5 desligado", "checked": 0,
                    "missing": [], "unknown": [], "sl_mismatches": []}
        missing, unknown, sl_mismatches = [], [], []
        pending_ok, pending_orphans = [], []
        checked = 0
        for trade in self.db.open_trades():
            try:
                live = self.provider._mt5.positions_get(
                    symbol=self.provider.meta(trade.symbol)["mt5"]
                ) or []
            except Exception as exc:
                return {"ok": False, "mode": "mt5-real", "safe": False,
                        "reason": f"positions_get falhou: {exc}",
                        "checked": checked, "missing": missing,
                        "unknown": unknown, "sl_mismatches": sl_mismatches}
            app_legs = {p.leg: p for p in self.db.positions_for_trade(trade.id)
                        if p.status == "open"}
            live_legs = {}
            for broker_pos in live:
                comment = getattr(broker_pos, "comment", "") or ""
                if trade.id in comment and ":leg" in comment:
                    try:
                        leg = int(comment.rsplit(":leg", 1)[1])
                    except (TypeError, ValueError):
                        continue
                    live_legs[leg] = broker_pos
            checked += len(app_legs)
            for leg, app_pos in app_legs.items():
                broker_pos = live_legs.get(leg)
                if broker_pos is None:
                    missing.append({"trade_id": trade.id, "leg": leg,
                                    "position_id": app_pos.id})
                    continue
                broker_sl = float(getattr(broker_pos, "sl", 0.0) or 0.0)
                if broker_sl and abs(broker_sl - float(app_pos.stop_loss)) > 1e-9:
                    sl_mismatches.append({
                        "trade_id": trade.id, "leg": leg,
                        "position_id": app_pos.id,
                        "db_sl": app_pos.stop_loss, "broker_sl": broker_sl,
                    })
            for leg, broker_pos in live_legs.items():
                if leg not in app_legs:
                    unknown.append({"trade_id": trade.id, "leg": leg,
                                    "ticket": getattr(broker_pos, "ticket", None)})
            # Fase 4 — ordens pendentes da zona (status `pending_fill`): a
            # BD tem a ordem com ticket; o broker devolve-a em `orders_get`.
            # Uma perna cujo ticket já não existe no broker é um óRFÃO
            # (expirada/cancelada externamente ou perdida num restart) — o
            # tick fecha-a por TTL, mas o diagnóstico tem de a expor. A ordem
            # sem ticket é órfã imediata (restart antes do envio). Read-only.
            for pos in self.db.positions_for_trade(trade.id):
                if pos.status != "pending_fill":
                    continue
                if not pos.order_ticket:
                    pending_orphans.append({
                        "trade_id": trade.id, "leg": pos.leg,
                        "position_id": pos.id, "ticket": None,
                        "reason": "sem ticket (restart antes do envio)",
                    })
                    continue
                try:
                    orders = self.provider._mt5.orders_get(
                        ticket=pos.order_ticket) or []
                except Exception as exc:
                    self.db.record_exception(
                        exc, context={"component": "mt5",
                                      "phase": "reconcile.orders_get"},
                        trade_id=trade.id,
                    )
                    orders = []
                if orders:
                    pending_ok.append({
                        "trade_id": trade.id, "leg": pos.leg,
                        "ticket": pos.order_ticket,
                        "state": getattr(orders[0], "state", None),
                    })
                else:
                    pending_orphans.append({
                        "trade_id": trade.id, "leg": pos.leg,
                        "position_id": pos.id, "ticket": pos.order_ticket,
                        "reason": "ordem já não existe no broker",
                    })
        safe = (not missing and not unknown and not sl_mismatches
                and not pending_orphans)
        return {"ok": True, "mode": "mt5-real", "safe": safe,
                "checked": checked, "missing": missing, "unknown": unknown,
                "sl_mismatches": sl_mismatches,
                "pending": {"ok": pending_ok, "orphans": pending_orphans}}

    def _closed_by_broker(self, trade: Trade, pos: Position, mt5) -> tuple:
        """Find the close price + P&L the broker booked for a position."""
        return self._broker_close_via_deals(pos, mt5)

    def _broker_close_via_deals(self, pos: Position, mt5) -> Optional[tuple]:
        """Close price + P&L the broker booked for a position, or None when
        the broker has NOT closed it (position still open / no exit deal).

        We resolve the position ticket from our open-time mapping (comment
        `trade:id:legN`) and take the *last* deal for that ticket — for a
        closed position that is the exit deal with the real price/profit.
        """
        ticket = self._ticket_for(pos.id)
        if ticket is None:
            return None
        try:
            deals = mt5.history_deals_get(position=ticket) or []
        except Exception:
            return None
        if not deals:
            return None
        last = deals[-1]
        # The last deal for a still-open position is the ENTRY (entry=0 =
        # DEAL_ENTRY_IN). An exit deal has entry=1 (OUT), 2 (INOUT) or 3
        # (OUT_BY) — including a breakeven exit whose profit is 0. Only skip
        # the opening deal, never a real exit.
        if int(getattr(last, "entry", 1)) == 0:
            return None
        close_key = (pos.id, getattr(last, "ticket", None),
                     float(getattr(last, "price", 0.0)),
                     float(getattr(last, "profit", 0.0) or 0.0))
        if close_key not in self._journaled_close_events:
            self._journaled_close_events.add(close_key)
            trade = self.db.get_trade(pos.trade_id)
            self._journal_event(
                "fill_close", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"source": "broker", "status": "filled",
                         "leg": pos.leg, "ticket": ticket,
                         "deal_ticket": getattr(last, "ticket", None),
                         "price": float(last.price),
                         "pnl": float(last.profit or 0.0),
                         "entry": getattr(last, "entry", None)},
            )
        return (float(last.price), float(last.profit or 0.0))

    def _be_level(self, trade: Trade, buffer_pips: float = 0.0,
                  fill: Optional[float] = None) -> float:
        """Nível do SL pós-TP1: entrada seca, ou entrada ± buffer (BE+ —
        cobre o spread) quando `buffer_pips` > 0. Nunca cruza o TP1
        (fica limitado com 1 pip de folga).

        ``fill`` é o preço REAL de fill da perna e só se aplica no BE+
        (com buffer): o âncora é o PIOR custo entre a entrada do sinal e o
        fill — um fill adversário nunca fica com SL abaixo do próprio preço
        de entrada (BE+ tem de ser sem perda). Sem buffer (BE clássico) ou
        sem ``fill``, comporta-se exatamente como antes.
        """
        entry = float(trade.entry)
        buffer_pips = float(buffer_pips or 0.0)
        base = entry
        if buffer_pips > 0.0 and fill is not None:
            try:
                fill = float(fill)
            except (TypeError, ValueError):
                fill = None
            if fill and fill > 0:
                base = max(entry, fill) if trade.direction == "buy" \
                    else min(entry, fill)
        if buffer_pips <= 0.0:
            return entry
        pip = float((self._sym_meta(trade.symbol) or {}).get("pip", 0.01))
        buffer = buffer_pips * pip
        if trade.take_profit_1:
            room = abs(float(trade.take_profit_1) - base) - pip
            buffer = min(buffer, room) if room > 0 else 0.0
        if buffer <= 0.0:
            return base
        return base + buffer if trade.direction == "buy" else base - buffer

    def _move_sl_breakeven(self, trade: Trade, pos: Position, mt5,
                           buffer_pips: float = 0.0) -> None:
        """Modify leg 2's SL to breakeven (entry, or entry ± buffer when
        ``buffer_pips`` is set — BE+ cobre o spread) after TP1 hit. O âncora
        é o pior entre a entrada do sinal e o FILL REAL da perna — um fill
        com slippage nunca fica com SL abaixo do próprio preço de entrada."""
        be_level = self._be_level(trade, buffer_pips, fill=pos.entry)
        mt5_sym = self.provider.meta(trade.symbol)["mt5"]
        ticket = self._ticket_for(pos.id)
        if ticket is None:
            # find by comment
            try:
                for p in mt5.positions_get(symbol=mt5_sym) or []:
                    if f"{trade.id}:leg{pos.leg}" in (getattr(p, "comment", "") or ""):
                        ticket = p.ticket
                        break
            except Exception:
                return
        if ticket is None:
            return
        try:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": mt5_sym,
                "position": ticket,
                "sl": be_level,
                "tp": float(pos.take_profit),
            }
            started = self._auto_clock.monotonic()
            self._journal_event(
                "modify_request", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"kind": "breakeven", "ticket": ticket,
                         "requested_sl": be_level,
                         "take_profit": pos.take_profit},
            )
            result = mt5.order_send(request)
            retcode = getattr(result, "retcode", "?")
            accepted = _order_ok(result, mt5)
            self._journal_event(
                "modify_result", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"kind": "breakeven", "ticket": ticket,
                         "requested_sl": be_level, "retcode": self._retcode(result),
                         "accepted": accepted,
                         "comment": getattr(result, "comment", "") or "",
                         "latency_ms": round((self._auto_clock.monotonic() - started) * 1000, 3)},
            )
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind="breakeven",
                requested_sl=be_level, valid_sl=be_level,
                retcode=int(retcode) if str(retcode).lstrip("-").isdigit() else None,
                accepted=accepted, error=getattr(result, "comment", "") or "",
            )
            self._log_order("modify_sl_be", {
                "trade": trade.id, "leg": pos.leg, "ticket": ticket,
                "sl": be_level, "retcode": retcode,
            })
            # A BD só pode refletir o BE depois de o broker confirmar o
            # modify. Sem isto, o caminho real continuava a mostrar o SL
            # original apesar de o MT5 já estar protegido, e o trailing
            # calculava o próximo nível a partir de um estado falso.
            if _order_ok(result, mt5):
                self.db.move_position_sl(pos.id, be_level)
                pos.stop_loss = be_level
        except Exception as exc:
            # A falha antes de receber um retcode continua a ser uma tentativa
            # operacional observável; não pode desaparecer apenas no log.
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind="breakeven",
                requested_sl=be_level, valid_sl=be_level,
                retcode=None, accepted=False, error=str(exc),
            )
            self.db.record_exception(
                exc, context={"component": "mt5", "phase": "modify_sl", "kind": "breakeven"},
                trade_id=trade.id,
            )
            print(f"[mt5] modify SL BE falhou: {exc}")

    def _restore_auto_state(self, trade: Trade, positions: dict,
                            now: str) -> dict:
        """Reconstruct the in-memory auto trigger after a server restart.

        TP1 and the applied SLs are persisted, while the peak itself is
        intentionally restarted from the next live price by
        ``_auto_trailing``.  Without restoring ``tp1_ts`` the trailing and
        time-stop branches would remain disabled for the rest of the trade.
        """
        if trade.id not in self._auto_state:
            persisted = self.db.load_protection_state("mt5", trade.id) or {}
            defaults = {
                "tp1_ts": None, "peak": None, "trough": None,
                "last_mod": {}, "auto_protected": False,
                "auto_protection_at": None, "auto_sl_by_leg": {},
                "auto_protection_at_by_leg": {},
            }
            defaults.update(persisted)
            # monotonic timestamps cannot survive a process restart; reset
            # only the throttle, preserving price/protection state.
            defaults["last_mod"] = {}
            self._auto_state[trade.id] = defaults
        state = self._auto_state[trade.id]
        self.db.save_protection_state("mt5", trade.id, state, updated_at=now)
        leg1 = positions.get(1)
        if not leg1 or leg1.status != "closed":
            return state
        if leg1.exit_reason == "auto_protect":
            state["auto_protected"] = True
            state.setdefault("auto_protection_at", leg1.closed_at or now)
            return state
        if leg1.exit_reason not in ("tp", "tp1"):
            return state
        if state.get("tp1_ts") is None:
            # MT5's time-stop adapter consumes an ISO string (unlike the
            # numeric timestamp used by SimEngine/PaperRace).
            state["tp1_ts"] = leg1.closed_at or now
        applied_at = leg1.closed_at or now
        for pos in positions.values():
            if pos.leg == 1 or pos.status != "open":
                continue
            if abs(float(pos.stop_loss) - float(trade.stop_loss)) <= 1e-12:
                continue
            state.setdefault("auto_sl_by_leg", {}).setdefault(
                pos.leg, pos.stop_loss,
            )
            state.setdefault("auto_protection_at_by_leg", {}).setdefault(
                pos.leg, applied_at,
            )
        return state

    def _record_close(self, trade: Trade, pos: Position, price: float, pnl: float,
                      now: str, reason: Optional[str] = None,
                      msg_id: Optional[str] = None) -> dict:
        pos.status = "closed"
        pos.close_price = price
        pos.pnl = pnl
        pos.closed_at = now
        pos.exit_reason = reason
        pos.exit_msg_id = msg_id
        self.db.close_position(pos.id, price, pnl, now, reason=reason, msg_id=msg_id)
        return {
            "type": "close", "trade_id": trade.id, "symbol": trade.symbol,
            "leg": pos.leg, "direction": trade.direction, "price": price,
            "pnl": pnl, "reason": reason,
        }

    def _close_dry(self, trade: Trade, pos: Position, price: float, now: str,
                   reason: Optional[str] = None,
                   msg_id: Optional[str] = None) -> dict:
        pnl = self._pnl_sym(pos, price)
        self._log_order("close", {
            "trade": trade.id, "leg": pos.leg, "symbol": pos.symbol,
            "price": price, "pnl": round(pnl, 2),
            "note": "dry-run: simulado no tick atual",
        })
        self._journal_event(
            "fill_close", at=now, trade=trade, pos=pos,
            payload={"source": "mt5_dry_run", "status": "simulated",
                     "leg": pos.leg, "price": price, "pnl": pnl,
                     "reason": reason, "message_id": msg_id},
        )
        return self._record_close(trade, pos, price, pnl, now, reason=reason,
                                  msg_id=msg_id)

    def _pnl_sym(self, pos: Position, close_price: float) -> float:
        meta = self._sym_meta(pos.symbol) or self.provider._symbols.get(pos.symbol, {})
        contract = float(meta.get("contract", CONTRACT_SIZE))
        sign = 1.0 if pos.direction == "buy" else -1.0
        return round(sign * (close_price - pos.entry) * pos.lots * contract, 2)

    def _maybe_close_trade(self, trade: Trade, now: str,
                           reason: Optional[str] = None) -> List[dict]:
        positions = self.db.positions_for_trade(trade.id)
        remaining = [p for p in positions
                     if p.status in ("open", "pending_fill")]
        if not remaining and trade.status == "open":
            # Auto time-stop/confirmation closes legs through
            # ``close_leg_at_market`` before this reconciliation call. When
            # no explicit trade-level reason was passed, preserve the reason
            # of the last leg closed instead of writing NULL to the trade.
            if reason is None:
                closed = [p for p in positions if p.exit_reason]
                if closed:
                    reason = max(
                        closed,
                        key=lambda p: (p.closed_at or "", p.exit_reason or ""),
                    ).exit_reason
            pnl = sum(p.pnl for p in positions)
            self.db.close_trade(trade.id, now, pnl, reason=reason)
            # Remove the durable row; keep the in-process snapshot available
            # for the validation panel and post-close audit.
            self.db.delete_protection_state("mt5", trade.id)
            return [{
                "type": "trade_closed", "trade_id": trade.id,
                "symbol": trade.symbol, "pnl": pnl, "reason": reason,
            }]
        return []

    # ------------------------------------------------------- manage (group)

    def move_leg_sl(self, trade_id: str, leg: int, new_sl: float) -> Optional[float]:
        """Move a single leg's SL (group "move stop loss above best entries").

        O nível vai pelos MESMOS três níveis (target → valid → applied) do
        caminho híbrido/auto: o alvo teórico é limitado por `_valid_sl` às
        regras do broker (stops_level/freeze + arredondamento ao tick) e o
        SL só se torna *applied* quando o broker confirma (10009/0). Quando
        o modify é recusado (ex.: 10016 "invalid stops" — incluindo o caso
        em que o broker aplica um stops_level que o `symbol_info` não
        reporta) ou ficou clamped abaixo do alvo, o ALVO original fica em
        fila (`sl_move_pending`) e o tick volta a tentar nos próximos
        ticks — o incidente de 07/09 (protect recusado na lane 2, a perna
        rodou até ao SL completo) não se repete.
        """
        trade = self.db.get_trade(trade_id)
        if not trade:
            return None
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status == "open"), None)
        if pos is None:
            return None
        if self.dry_run:
            self._log_order("modify_sl", {
                "trade": trade.id, "leg": pos.leg, "sl": new_sl,
                "note": "dry-run: SL atualizado na BD apenas",
            })
            self.db.move_position_sl(pos.id, new_sl)
            pos.stop_loss = new_sl
            return new_sl
        # alvo pior/igual ao SL atual (movimento anti-monotónico) é no-op:
        # nunca em fila — o grupo não pediu proteção nova
        if monotonic_sl(trade.direction, pos.stop_loss, float(new_sl)) is None:
            return None
        return self._attempt_group_sl(trade, pos, float(new_sl))

    def _market_clamped_sl(self, trade: Trade, target: float) -> float:
        """Clamp do alvo de proteção ao LADO VÁLIDO do preço de mercado.

        Um BE+ ancorado na entrada do sinal pode ficar do lado errado do
        preço atual (incidente 15/09 11:49: entrada 4285, bid 4285.42 → alvo
        4285.5 ACIMA do bid num buy — o broker recusou 280 modifies com
        10016 'Invalid stops' durante 9 min e a perna morreu no SL original).
        O clamp é restritivo: num buy o SL nunca passa do preço (fica
        marketable-side); num sell nunca fica abaixo. Sem preço, devolve o
        alvo tal como está.
        """
        tick = self._price(trade.symbol)
        if tick is None or tick <= 0:
            return target
        meta = self._sym_meta(trade.symbol) or {}
        digits = int(meta.get("digits", 2))
        pip = float(meta.get("pip", 0.01))
        # 1 pip de folga para o nível ficar aceitável pelo broker
        if trade.direction == "buy":
            ceiling = round(tick - pip, digits)
            return min(target, ceiling)
        floor = round(tick + pip, digits)
        return max(target, floor)

    def _attempt_group_sl(self, trade: Trade, pos: Position,
                          target: float) -> Optional[float]:
        """Um ciclo target → valid → applied para o SL de gestão de grupo.

        O alvo é primeiro clampado ao lado válido do preço de mercado
        (`_market_clamped_sl` — o BE+ nunca fica no lado proibido do preço,
        causa real dos 10016 em série de 15/09). Depois envia o modify do
        nível válido do momento; aceito → aplica na BD e, se ficou clamped
        abaixo do alvo, mantém o alvo EM FILA para o tick completar o
        movimento; recusado → mantém o SL anterior (nunca aplicado sem
        confirmação do broker) e filaa o alvo. Devolve o nível aplicado
        deste ciclo, ou None.
        """
        original_target = target
        target = self._market_clamped_sl(trade, target)
        valid = self._valid_sl(trade, target)
        if monotonic_sl(trade.direction, pos.stop_loss, valid) is None:
            # nem o melhor nível válido melhora o SL atual → fica só a
            # intenção em fila (o retry volta a verificar no próximo tick)
            self._stage_group_sl(trade, pos, target, None, "refused")
            return None
        retcode = self._modify_sl(trade, pos, valid, kind="group")
        if int(retcode or -1) in (10009, 0):
            self.db.clear_pending_sl_move(pos.id)
            self.db.move_position_sl(pos.id, valid)
            pos.stop_loss = valid
            if abs(round(valid, 4) - round(original_target, 4)) > 1e-9:
                # clamped: o broker aceitou um nível ainda longe do alvo —
                # o ALVO ORIGINAL do grupo fica em fila e o tick completa o
                # movimento quando o alvo ficar válido
                self._stage_group_sl(trade, pos, original_target, retcode,
                                     "clamped")
            return valid
        self._stage_group_sl(trade, pos, original_target, retcode,
                             "refused" if retcode is not None else "not_sent")
        return None

    def _stage_group_sl(self, trade: Trade, pos: Position, target: float,
                        retcode: Optional[int], reason: str) -> None:
        self.db.save_pending_sl_move(self.lane_id or "main", pos.id, {
            "trade_id": trade.id, "leg": pos.leg,
            "target_sl": float(target),
            "retcode": retcode,
            "staged_at": _now_iso(),
            "reason": reason,
        })

    def _retry_pending_sl_moves(self, now: str) -> List[dict]:
        """Re-tenta SLs de gestão de grupo que o broker recusou/clampou.

        Corre no update() (1×/tick, antes da reconciliação): carrega a fila
        persistida, reverifica a validade do alvo com o preço ATUAL e envia
        o modify quando o nível passar a caber. Limpa as entradas resolvidas
        (aceites no alvo exato) e as órfãs (perna fechada / trade encerrada).
        """
        if self.dry_run:
            return []
        events: List[dict] = []
        lane = self.lane_id or "main"
        try:
            staged = self.db.load_pending_sl_moves(lane)
        except Exception:
            return events
        if not staged:
            return events
        open_trades = {t.id: t for t in self.db.open_trades()
                       if (getattr(t, "lane_id", None) or "main") == lane}
        for pos_id, entry in staged.items():
            trade = open_trades.get(entry.get("trade_id") or "")
            if trade is None:
                # trade encerrada → intenção órfã
                self.db.clear_pending_sl_move(pos_id)
                continue
            pos = next((p for p in self.db.positions_for_trade(trade.id)
                        if p.id == pos_id and p.status == "open"), None)
            if pos is None:
                self.db.clear_pending_sl_move(pos_id)
                continue
            target = entry.get("target_sl")
            if target is None:
                self.db.clear_pending_sl_move(pos_id)
                continue
            target = float(target)
            clamped = self._market_clamped_sl(trade, target)
            valid = self._valid_sl(trade, clamped)
            if monotonic_sl(trade.direction, pos.stop_loss, valid) is None:
                # ainda não melhora / ainda inválido — fica em fila
                continue
            retcode = self._modify_sl(trade, pos, valid, kind="group_retry")
            if int(retcode or -1) in (10009, 0):
                self.db.clear_pending_sl_move(pos.id)
                self.db.move_position_sl(pos.id, valid)
                pos.stop_loss = valid
                if abs(round(valid, 4) - round(target, 4)) > 1e-9:
                    # ainda clamped: mantém o alvo para o próximo tick
                    self._stage_group_sl(trade, pos, target, retcode, "clamped")
                events.append({
                    "type": "sl_move_applied", "trade_id": trade.id,
                    "symbol": trade.symbol, "leg": pos.leg,
                    "target": target, "applied": valid,
                    "retcode": retcode,
                })
        return events

    def _clear_stale_sl_moves(self) -> None:
        """Fim do update(): limpa intenções cuja perna fechou NESTE tick (a
        verificação do início do tick corre antes da reconciliação)."""
        lane = self.lane_id or "main"
        try:
            staged = self.db.load_pending_sl_moves(lane)
        except Exception:
            return
        for pos_id, entry in staged.items():
            trade = self.db.get_trade(entry.get("trade_id") or "")
            pos = next((p for p in self.db.positions_for_trade(trade.id)
                        if p.id == pos_id), None) if trade else None
            if pos is None or pos.status != "open" or trade.status != "open":
                self.db.clear_pending_sl_move(pos_id)

    def close_leg_at_market(self, trade_id: str, leg: int,
                            now: Optional[str] = None,
                            reason: str = "close_leg",
                            msg_id: Optional[str] = None) -> Optional[dict]:
        """Close a single leg at market (group "close first entries").
        Uma perna `pending_fill` (ordem na zona) é cancelada sem P&L."""
        now = now or _now_iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return None
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status in ("open", "pending_fill")), None)
        if pos is None:
            return None
        if pos.status == "pending_fill":
            return self._cancel_pending_pos(pos, now, reason=reason, msg_id=msg_id)
        if not self.dry_run:
            mt5 = self.provider._mt5
            if not self.provider.connected:
                return None
            mt5_sym = self.provider.meta(trade.symbol)["mt5"]
            try:
                live = mt5.positions_get(symbol=mt5_sym) or []
            except Exception:
                live = []
            for p in live:
                comment = getattr(p, "comment", "") or ""
                if trade.id not in comment or f":leg{leg}" not in comment:
                    continue
                price = p.price_current if hasattr(p, "price_current") else p.price_open
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": getattr(p, "symbol", ""),
                    "volume": float(p.volume),
                    "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY
                    else mt5.ORDER_TYPE_BUY,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 10,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                started = self._auto_clock.monotonic()
                self._journal_event(
                    "order_request", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "close", "kind": reason,
                             "ticket": p.ticket, "volume": p.volume,
                             "requested_price": price},
                )
                result = mt5.order_send(request)
                accepted = _order_ok(result, mt5)
                self._journal_event(
                    "order_result", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "close", "kind": reason,
                             "ticket": p.ticket, "accepted": accepted,
                             "retcode": self._retcode(result),
                             "comment": getattr(result, "comment", "") or "",
                             "deal_ticket": getattr(result, "deal", None),
                             "latency_ms": round((self._auto_clock.monotonic() - started) * 1000, 3)},
                )
                if accepted:
                    close_pnl = self._close_pnl_from_deals(p.ticket, mt5)
                    if close_pnl == 0:
                        close_pnl = float(getattr(p, "profit", 0) or 0.0)
                    fill_price = float(getattr(result, "price", price) or price)
                    self._journal_event(
                        "fill_close", at=now, trade=trade, pos=pos,
                        payload={"source": "broker", "status": "filled",
                                 "leg": pos.leg, "ticket": p.ticket,
                                 "deal_ticket": getattr(result, "deal", None),
                                 "price": fill_price, "pnl": close_pnl,
                                 "reason": reason, "message_id": msg_id},
                    )
                    return self._record_close(trade, pos, fill_price,
                                              close_pnl, now, reason=reason,
                                              msg_id=msg_id)
                self._log_order("close_leg_failed", {
                    "trade": trade_id, "leg": leg, "ticket": p.ticket,
                    "retcode": getattr(result, "retcode", "?"),
                    "comment": getattr(result, "comment", ""),
                })
                return None
            return None
        # dry-run: close at the current tick / feed price
        price = self._price(trade.symbol)
        if price is None:
            return None
        return self._close_dry(trade, pos, price, now, reason=reason, msg_id=msg_id)

    def auto_protection_state(self, trade_id: str, leg: int) -> dict:
        state = self._auto_state.get(trade_id, {})
        return {
            "auto_sl": state.get("auto_sl_by_leg", {}).get(leg),
            "auto_protection_at": state.get(
                "auto_protection_at_by_leg", {}).get(leg),
        }

    def apply_hybrid_protect(self, trade: Trade, group_sl: float,
                             now: Optional[str] = None,
                             msg_id: Optional[str] = None) -> List[dict]:
        """Apply a monotonic group protect to a promoted hybrid trade.

        The main strategy is never routed here unless the user explicitly
        promotes the hybrid. In real mode a position SL is written to SQLite
        only after the broker accepts the modify (10009/0).
        """
        now = now or _now_iso()
        if trade.strategy_id != "auto_escada_3_hibrida":
            return []
        if trade.status != "open":
            return []
        price = self._price(trade.symbol)
        if price is None:
            return []
        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        open_legs = [p for p in positions.values() if p.status == "open"]
        if not open_legs:
            return []
        state = self._restore_auto_state(trade, positions, now)
        auto_by_leg = state.get("auto_sl_by_leg", {})
        auto_at_by_leg = state.get("auto_protection_at_by_leg", {})
        remaining = [p for p in open_legs if p.leg != 1]
        vals = [auto_by_leg.get(p.leg) for p in remaining
                if auto_by_leg.get(p.leg) is not None]
        representative = ((max(vals) if trade.direction == "buy" else min(vals))
                          if vals else None)
        tp1 = trade.take_profit_1 or trade.entry
        leg1 = positions.get(1)
        pip = float((self._sym_meta(trade.symbol) or {}).get("pip", 0.0001))
        leg1_decision = decide_protect(
            direction=trade.direction, auto_sl=representative,
            group_sl=group_sl, entry=trade.entry, tp1=tp1,
            price_at_event=price,
            leg1_open=bool(leg1 and leg1.status == "open"),
            auto_protection_at=(auto_at_by_leg.get(remaining[0].leg)
                                if remaining else None),
            group_received_at=now, pip_size=pip,
        )
        events: List[dict] = []
        leg1_eligible = hybrid_protect_eligible(
            representative,
            (auto_at_by_leg.get(remaining[0].leg) if remaining else None),
        )
        if leg1 and leg1.status == "open" \
                and leg1_eligible and leg1_decision.leg1_action == "closed":
            ev = self.close_leg_at_market(trade.id, 1, now=now,
                                          reason="protect", msg_id=msg_id)
            if ev:
                events.append(ev)
        for pos in remaining:
            auto_sl = auto_by_leg.get(pos.leg)
            decision = decide_protect(
                direction=trade.direction, auto_sl=auto_sl,
                group_sl=group_sl, entry=trade.entry, tp1=tp1,
                price_at_event=price,
                leg1_open=bool(leg1 and leg1.status == "open"),
                auto_protection_at=auto_at_by_leg.get(pos.leg),
                group_received_at=now, pip_size=pip,
            )
            eligible = hybrid_protect_eligible(auto_sl, auto_at_by_leg.get(pos.leg))
            target = decision.applied_sl if eligible else None
            valid = target
            retcode = None
            accepted = False
            status = decision.status
            if target is not None:
                valid = (self._valid_sl(trade, target)
                         if (not self.dry_run and self.provider.connected)
                         else target)
                improves = ((valid > pos.stop_loss) if trade.direction == "buy"
                            else (valid < pos.stop_loss))
                if improves:
                    if self.dry_run or not self.provider.connected:
                        accepted = True
                    else:
                        retcode = self._modify_sl(trade, pos, valid, kind="protect")
                        accepted = int(retcode or -1) in (10009, 0)
                    if accepted:
                        self.db.move_position_sl(pos.id, valid)
                        pos.stop_loss = valid
                    else:
                        status = f"retcode_{retcode}" if retcode is not None else "modify_refused"
                        warn = {
                            "type": "auto_sl_warn", "trade_id": trade.id,
                            "symbol": trade.symbol, "leg": pos.leg,
                            "kind": "protect", "target": target,
                            "valid": valid, "retcode": retcode,
                            "applied": False, "reason": "refused",
                        }
                        self._sl_warnings.append(warn)
                else:
                    status = "clamped_not_improving"
            applied = pos.stop_loss if not accepted else valid
            self._log_auto_sl(trade.id, {
                "ts": now, "leg": pos.leg, "kind": "protect",
                "target": target, "valid": valid,
                "clamped": target is not None and valid != target,
                "retcode": retcode, "applied": accepted,
            })
            self.db.insert_protect_latency({
                "engine": "mt5", "strategy_id": trade.strategy_id or "",
                "signal_id": trade.signal_id, "trade_id": trade.id,
                "message_id": msg_id, "symbol": trade.symbol,
                "direction": trade.direction,
                "auto_protection_at": auto_at_by_leg.get(pos.leg),
                "group_received_at": now, "auto_sl": auto_sl,
                "group_sl": group_sl, "applied_sl": applied,
                "winner": decision.winner, "delta_pips": decision.delta_pips,
                "delta_seconds": decision.delta_seconds,
                "leg1_action": leg1_decision.leg1_action,
                "status": status, "created_at": now,
            })
        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        if not [p for p in positions.values() if p.status == "open"]:
            pnl = sum(p.pnl for p in positions.values())
            self.db.close_trade(trade.id, now, pnl, reason="protect")
            events.append({"type": "trade_closed", "trade_id": trade.id,
                           "symbol": trade.symbol, "pnl": pnl,
                           "reason": "protect"})
        return events

    def _modify_sl(self, trade: Trade, pos: Position, new_sl: float,
                   kind: str = "modify") -> Optional[int]:
        """Server-side SL modify for a single position (real mode). Devolve o
        retcode do broker quando a ordem foi enviada (10009/0 = aceite;
        10016 = stops inválidos), None quando nada foi enviado (sem ticket /
        sem terminal) — o chamador decide se o SL se torna *applied*."""
        mt5 = self.provider._mt5
        mt5_sym = self.provider.meta(trade.symbol)["mt5"]
        ticket = self._ticket_for(pos.id)
        if ticket is None:
            try:
                for p in mt5.positions_get(symbol=mt5_sym) or []:
                    if f"{trade.id}:leg{pos.leg}" in (getattr(p, "comment", "") or ""):
                        ticket = p.ticket
                        break
            except Exception:
                return None
        if ticket is None:
            return None
        try:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": mt5_sym,
                "position": ticket,
                "sl": float(new_sl),
                "tp": float(pos.take_profit),
            }
            started = self._auto_clock.monotonic()
            self._journal_event(
                "modify_request", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"kind": kind, "ticket": ticket,
                         "requested_sl": new_sl, "take_profit": pos.take_profit},
            )
            result = mt5.order_send(request)
            retcode = int(getattr(result, "retcode", -1))
            accepted = retcode in (10009, 0)
            self._journal_event(
                "modify_result", at=self.clock.iso(), trade=trade, pos=pos,
                payload={"kind": kind, "ticket": ticket,
                         "requested_sl": new_sl, "retcode": retcode,
                         "accepted": accepted,
                         "comment": getattr(result, "comment", "") or "",
                         "latency_ms": round((self._auto_clock.monotonic() - started) * 1000, 3)},
            )
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind=kind,
                requested_sl=new_sl, valid_sl=new_sl, retcode=retcode,
                accepted=accepted, error=getattr(result, "comment", "") or "",
            )
            self._log_order("modify_sl", {
                "trade": trade.id, "leg": pos.leg, "ticket": ticket,
                "sl": new_sl, "retcode": retcode, "kind": kind,
            })
            return retcode
        except Exception as exc:
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind=kind,
                requested_sl=new_sl, valid_sl=new_sl, retcode=None,
                accepted=False, error=str(exc),
            )
            self.db.record_exception(
                exc, context={"component": "mt5", "phase": "modify_sl", "kind": kind},
                trade_id=trade.id,
            )
            print(f"[mt5] modify SL falhou: {exc}")
            return None

    # ------------------------------------------------ Fase 3: auto-gestão

    def _valid_sl(self, trade: Trade, target_sl: float) -> float:
        """Três níveis de SL (target → valid → applied).

        `target_sl` é o nível teórico (escada/trailing). `valid_sl` respeita
        os limites do broker — `stops_level`/`freeze_level` do símbolo
        (distância mínima do preço atual) e o arredondamento ao tick. O
        `applied_sl` é o que o `_modify_sl` envia e confirma (retcode 10009).
        """
        meta = self._sym_meta(trade.symbol)
        if meta is None:
            return target_sl
        mt5 = self.provider._mt5
        try:
            info = mt5.symbol_info(meta["mt5"])
        except Exception:
            info = None
        tick = self._price(trade.symbol)
        if info is None or tick is None:
            return target_sl
        stops_level = float(getattr(info, "stops_level", 0) or 0)
        freeze_level = float(getattr(info, "freeze_level", 0) or 0)
        min_dist = max(stops_level, freeze_level) * float(meta.get("pip", 0.01))
        # o SL não pode ficar mais perto do preço atual do que a distância
        # mínima do broker (em pips) — senão o modify é recusado (10016)
        if trade.direction == "buy":
            if target_sl > tick - min_dist:
                return round(tick - min_dist, int(meta.get("digits", 2)))
        else:
            if target_sl < tick + min_dist:
                return round(tick + min_dist, int(meta.get("digits", 2)))
        return round(target_sl, int(meta.get("digits", 2)))

    def _auto_move_sl(self, trade: Trade, pos: Position, mt5, level: float,
                      stair: bool = False, kind: Optional[str] = None) -> bool:
        """Move o SL de uma perna para `level` (escada/trailing) com os três
        níveis (target→valid→applied) e throttling: só modifica se o novo SL
        melhorar ≥ 1 pip e ≤ 1×/min por perna — o trailing não spammeia
        ordens SLTP. `stair=True` marca a razão `stair` (escada); trailing
        marca `trail`."""
        if pos.status != "open":
            return False
        current = pos.stop_loss or 0.0
        kind = kind or ("stair" if stair else "trail")
        # decisão pura e comum aos três motores: só melhora
        candidate = monotonic_sl(trade.direction, current, level)
        if candidate is None:
            return False
        level = candidate
        # throttling: ≥ 1 pip de melhoria e ≤ 1×/min por perna
        pip = float((self._sym_meta(trade.symbol) or {}).get("pip", 0.0001))
        if abs(level - current) < pip:
            return False
        state = self._auto_state.setdefault(trade.id, {})
        last = state.get("last_mod", {}).get(pos.id, 0.0)
        if self._auto_clock.monotonic() - last < 60:
            return False
        valid = self._valid_sl(trade, level)
        # só envia se o valid ainda melhorar o atual
        if trade.direction == "buy":
            if valid <= current + 1e-12:
                return False
        else:
            if valid >= current - 1e-12:
                return False
        if self.dry_run or not self.provider.connected:
            # dry-run / sem terminal: o SL aplica-se na BD (como o PaperRace)
            # — não há broker para confirmar
            retcode = None
            accepted = True
        else:
            # modo real: o SL só se torna *applied* quando o broker o
            # confirma (retcode 10009/0) — um modify recusado (ex.: 10016
            # stops inválidos) NÃO altera a BD nem o throttle
            retcode = self._modify_sl(trade, pos, valid, kind=kind)
            accepted = int(retcode or -1) in (10009, 0)
        # auditoria dos três níveis (janela de validação, ESTRATEGIAS.md Parte X): cada
        # tentativa fica registada com target→valid→applied + retcode, para
        # se ver se algum modify foi recusado (10016) ou arredondado pelo
        # broker (valid != target) — persistida na config (sobrevive a restarts)
        clamped = abs(round(valid, 4) - round(level, 4)) > 1e-9
        self._log_auto_sl(trade.id, {
            "ts": _now_iso(), "leg": pos.leg, "kind": kind,
            "target": round(level, 4), "valid": round(valid, 4),
            "clamped": clamped,
            "retcode": retcode, "applied": accepted,
        })
        # aviso de risco (janela de validação): um modify RECUSADO (10016) ou
        # ARREDONDADO pelo broker não pode passar despercebido — notifica o
        # feed/Telegram (dedup por trade+perna+alvo)
        warn_key = (trade.id, pos.leg, round(level, 4))
        if warn_key not in self._sl_warned and (not accepted or clamped):
            self._sl_warned.add(warn_key)
            self._sl_warnings.append({
                "type": "auto_sl_warn",
                "trade_id": trade.id, "symbol": trade.symbol,
                "leg": pos.leg, "kind": kind,
                "target": round(level, 4), "valid": round(valid, 4),
                "retcode": retcode, "applied": accepted,
                "reason": "clamped" if (accepted and clamped) else "refused",
            })
        if not accepted:
            return False
        self.db.move_position_sl(pos.id, valid)
        pos.stop_loss = valid
        state.setdefault("auto_sl_by_leg", {})[pos.leg] = valid
        state.setdefault("auto_protection_at_by_leg", {}).setdefault(pos.leg, _now_iso())
        state.setdefault("last_mod", {})[pos.id] = self._auto_clock.monotonic()
        self.db.save_protection_state("mt5", trade.id, state)
        self._log_order("auto_move_sl", {
            "trade": trade.id, "leg": pos.leg, "sl": valid,
            "kind": kind,
        })
        return True

    def _log_auto_sl(self, trade_id: str, entry: dict) -> None:
        """Persiste a auditoria dos três níveis de SL de uma trade da auto
        (config `autosl:<trade_id>` = lista JSON, cap 300) — a janela de
        validação lê-a via `GET /api/analysis/auto_sl_log`."""
        import json
        key = f"autosl:{trade_id}"
        try:
            current = json.loads(self.db.all_config().get(key) or "[]")
        except (ValueError, TypeError):
            current = []
        current.append(entry)
        self.db.set_config(key, json.dumps(current[-300:]))

    def _auto_trailing(self, trade: Trade, open_legs: List[Position],
                       mt5, auto: dict) -> None:
        """Trailing: depois do TP1, o SL das pernas restantes segue o pico
        dos ticks ANTERIORES (sem look-ahead — o pico só se atualiza no fim,
        depois dos fills, como no backtest/paper):
        `SL = pico − trail_factor × dist(entrada, TP2)` (buy) / pico +
        trail_dist (sell).
        """
        trail_factor = float(auto.get("trail_factor") or 0.0)
        tp1 = getattr(trade, "take_profit_1", 0.0) or 0.0
        tp2 = getattr(trade, "take_profit_2", 0.0) or 0.0
        policy = AutoProtectionPolicy(
            direction=trade.direction, entry=trade.entry, tp1=tp1, tp2=tp2,
            trail_factor=trail_factor,
        )
        if not trail_factor or not tp2:
            return
        trail_dist = policy.trail_distance()
        # Fase 3 pós-validação (ESTRATEGIAS.md Parte X): trailing adaptativo por ATR — o
        # recuo nunca fica mais apertado que `atr_k × ATR` (M1/M5). Mercado
        # volátil → o SL dá mais espaço (menos whipsaws); calmo → o trail
        # fixo domina. Sem dados de velas degrada para o trail fixo.
        atr_k = float(auto.get("atr_k") or 0.0)
        if atr_k > 0:
            atr = self._atr(trade.symbol,
                            period=int(auto.get("atr_period") or 14),
                            timeframe=int(auto.get("atr_timeframe") or 1))
            if atr and atr > 0:
                trail_dist = policy.trail_distance(atr=atr, atr_k=atr_k)
        price = self._price(trade.symbol)
        if price is None:
            return
        state = self._auto_state.setdefault(trade.id, {})
        previous_peak = state.get("peak")
        # O nível usado nesta chamada vem apenas do pico observado antes do
        # tick atual. O preço atual pode criar o próximo pico, mas nunca pode
        # criar o SL que ele próprio atravessa (sem look-ahead).
        if previous_peak is None:
            state["peak"] = price
            return
        target = policy.trailing_target(None, previous_peak, distance=trail_dist)
        if trade.direction == "buy":
            state["peak"] = max(previous_peak, price)
        else:
            state["peak"] = min(previous_peak, price)
        for pos in open_legs:
            if pos.leg != 1 and target is not None:
                self._auto_move_sl(trade, pos, mt5, target, stair=False)
        self.db.save_protection_state("mt5", trade.id, state)

    @staticmethod
    def _pf_below(pnls: List[float], threshold: float) -> bool:
        """True quando o PF dos pnls < threshold (win = > 0, loss = < 0,
        BE = fora da conta). Sem perdas → False (nunca dispara)."""
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        if not losses:
            return False
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return gross_loss > 0 and gross_win / gross_loss < threshold

    def _atr(self, app_symbol: str, period: int = 14,
             timeframe: int = 1) -> Optional[float]:
        """ATR (média do True Range) do símbolo nas últimas `period` velas do
        `timeframe` (enum MT5: M1=1, M5=5). None sem dados suficientes — o
        chamador degrada para o trail fixo."""
        rates = self.provider.copy_rates(app_symbol, timeframe, period + 2)
        if len(rates) < 2:
            return None
        trs = []
        for i in range(max(1, len(rates) - period), len(rates)):
            prev_close = rates[i - 1]["close"]
            hi = rates[i]["high"]
            lo = rates[i]["low"]
            trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        if not trs:
            return None
        return sum(trs) / len(trs)

    def _auto_time_stop(self, trade: Trade, open_legs: List[Position],
                        mt5, auto: dict, tp1_ts: str, now: str) -> None:
        """Time stop: pernas abertas N min após o TP1 (mercado estagnado)
        fecham ao mercado — espelho do backtest/paper. One-shot por trade."""
        minutes = float(auto.get("time_stop_minutes") or 0.0)
        if minutes <= 0:
            return
        if not time_stop_due_iso(tp1_ts, now, minutes):
            return
        try:
            from datetime import datetime
            t1 = datetime.fromisoformat(str(tp1_ts).replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
            elapsed = (t2 - t1).total_seconds() / 60.0
        except (ValueError, TypeError):
            return
        for pos in open_legs:
            if pos.leg != 1 and pos.status == "open":
                ev = self.close_leg_at_market(trade.id, pos.leg, now,
                                              reason="time_stop")
                if ev:
                    self._log_order("auto_time_stop", {
                        "trade": trade.id, "leg": pos.leg,
                        "minutes": round(elapsed, 1),
                    })

    # -------------------------------------------------------- close at market

    def _cancel_pending_pos(self, pos: Position, now: str, reason: str,
                            msg_id: Optional[str] = None) -> Optional[dict]:
        """Cancela uma perna pendente (ordem na zona): order_delete no real,
        fecho na BD sem P&L em ambos os modos.

        Devolve o evento de fecho; `None` quando o cancelamento no broker
        falhou (worker caído) — a perna fica `pending_fill` e a próxima
        tentativa repete o delete, em vez de fechar na BD e deixar a ordem
        órfã no MT5."""
        if not self.dry_run and pos.order_ticket:
            try:
                mt5 = self.provider._mt5
                if self.provider.connected:
                    mt5.order_delete(ticket=pos.order_ticket)
            except Exception as exc:
                # rate-limit por perna (mesmo watchdog do TTL): só a primeira
                # falha do episódio grava a exceção — o retry do tick não
                # enche `operational_exceptions` com linhas idênticas
                if pos.id not in self._pending_delete_exception_recorded:
                    self._pending_delete_exception_recorded.add(pos.id)
                    self.db.record_exception(
                        exc,
                        context={"component": "mt5", "phase": "order_delete",
                                 "kind": "cancel_pending", "leg": pos.leg,
                                 "lane": self.lane_id, "trade_id": pos.trade_id,
                                 "ticket": pos.order_ticket, "episode": True},
                        trade_id=pos.trade_id,
                    )
                self._log_order("pending_delete_failed", {
                    "trade": pos.trade_id, "leg": pos.leg,
                    "ticket": pos.order_ticket,
                    "retcode": "REMOTE_ERROR", "comment": str(exc)[:140],
                })
                self._journal_event(
                    "pending_order_result", at=now, pos=pos,
                    payload={"action": "delete", "accepted": False,
                             "retcode": "REMOTE_ERROR",
                             "ticket": pos.order_ticket,
                             "comment": str(exc)[:140]},
                )
                # intenção de cancelamento guardada + mesma contagem do
                # watchdog da expiração por TTL: o ciclo de retry do tick
                # re-tenta o delete (sem esperar o TTL) e escalona
                self._pending_delete_intent[pos.id] = {
                    "kind": "cancel", "reason": reason or "close_all",
                    "msg_id": msg_id,
                }
                self._bump_pending_delete_failure(pos)
                return None
        self._clear_pending_delete_retry(pos.id)
        self.db.close_position(pos.id, pos.entry, 0.0, now, reason=reason,
                               msg_id=msg_id)
        pos.status = "closed"
        pos.exit_reason = reason
        pos.exit_msg_id = msg_id
        return {
            "type": "close", "trade_id": pos.trade_id,
            "symbol": pos.symbol, "leg": pos.leg,
            "direction": pos.direction, "price": pos.entry,
            "pnl": 0.0, "reason": reason,
        }

    def close_trade_at_market(self, trade_id: str, now: Optional[str] = None,
                              reason: str = "close_all",
                              msg_id: Optional[str] = None) -> List[dict]:
        """Close every open leg at the current market price (signal deleted).
        Pernas `pending_fill` são canceladas (ordem pendente na zona)."""
        now = now or _now_iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return []

        events: List[dict] = []
        for pos in self.db.positions_for_trade(trade_id):
            if pos.status == "pending_fill":
                ev = self._cancel_pending_pos(pos, now, reason=reason,
                                              msg_id=msg_id)
                if ev:
                    events.append(ev)
        if self.dry_run:
            price = self._price(trade.symbol)
            if price is None:
                return []
            for pos in self.db.positions_for_trade(trade_id):
                if pos.status == "open":
                    events.append(self._close_dry(trade, pos, price, now,
                                                  reason=reason, msg_id=msg_id))
        else:
            mt5 = self.provider._mt5
            if not self.provider.connected:
                return []
            try:
                live = mt5.positions_get(symbol=self.provider.meta(trade.symbol)["mt5"]) or []
            except Exception:
                live = []
            for p in live:
                comment = getattr(p, "comment", "") or ""
                if trade.id not in comment:
                    continue
                pos = next((x for x in self.db.positions_for_trade(trade_id)
                            if f":leg{x.leg}" in comment and x.status == "open"), None)
                price = p.price_current if hasattr(p, "price_current") else p.price_open
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": getattr(p, "symbol", ""),
                    "volume": float(p.volume),
                    "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY
                    else mt5.ORDER_TYPE_BUY,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 10,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                started = self._auto_clock.monotonic()
                self._journal_event(
                    "order_request", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "close", "kind": reason,
                             "ticket": p.ticket, "volume": p.volume,
                             "requested_price": price},
                )
                result = mt5.order_send(request)
                accepted = _order_ok(result, mt5)
                self._journal_event(
                    "order_result", at=self.clock.iso(), trade=trade, pos=pos,
                    payload={"action": "close", "kind": reason,
                             "ticket": p.ticket, "accepted": accepted,
                             "retcode": self._retcode(result),
                             "comment": getattr(result, "comment", "") or "",
                             "deal_ticket": getattr(result, "deal", None),
                             "latency_ms": round((self._auto_clock.monotonic() - started) * 1000, 3)},
                )
                if accepted and pos:
                    # prefer the broker's booked P&L from the exit deal; fall
                    # back to the position's floating profit at close time
                    close_pnl = self._close_pnl_from_deals(p.ticket, mt5)
                    if close_pnl == 0:
                        close_pnl = float(getattr(p, "profit", 0) or 0.0)
                    fill_price = float(getattr(result, "price", price) or price)
                    self._journal_event(
                        "fill_close", at=now, trade=trade, pos=pos,
                        payload={"source": "broker", "status": "filled",
                                 "leg": pos.leg, "ticket": p.ticket,
                                 "deal_ticket": getattr(result, "deal", None),
                                 "price": fill_price, "pnl": close_pnl,
                                 "reason": reason, "message_id": msg_id},
                    )
                    events.append(self._record_close(
                        trade, pos, fill_price, close_pnl, now,
                        reason=reason, msg_id=msg_id))
                else:
                    self._log_order("close_failed", {
                        "trade": trade_id, "ticket": p.ticket,
                        "retcode": getattr(result, "retcode", "?"),
                        "comment": getattr(result, "comment", ""),
                    })

        events += self._maybe_close_trade(trade, now, reason=reason)
        return events

    def _close_pnl_from_deals(self, ticket: int, mt5) -> float:
        """Real P&L of the just-closed position (last deal for the ticket)."""
        try:
            deals = mt5.history_deals_get(position=ticket) or []
        except Exception:
            return 0.0
        if not deals:
            return 0.0
        last = deals[-1]
        if int(getattr(last, "entry", 1)) == 0:
            return 0.0
        return float(getattr(last, "profit", 0) or 0.0)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _hit(trade: Trade, price: float, level: float, kind: str) -> bool:
        if trade.direction == "buy":
            return price <= level if kind == "sl" else price >= level
        return price >= level if kind == "sl" else price <= level


def _elapsed_seconds(start: str, end: str) -> Optional[float]:
    """Return UTC ISO latency in seconds, or None for incomplete timestamps."""
    from datetime import datetime, timezone
    try:
        left = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        if left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        if right.tzinfo is None:
            right = right.replace(tzinfo=timezone.utc)
        return round(max(0.0, (right - left).total_seconds()), 6)
    except (TypeError, ValueError, OverflowError):
        return None


def _order_ok(result, mt5) -> bool:
    """True when the broker accepted the order.

    MetaQuotes docs say success is TRADE_RETCODE_DONE (10009), but the
    MetaQuotes-Demo server replies `retcode 0 / comment "Done"` — accept
    both so a real fill is never mistaken for a failure (and vice-versa).
    """
    if result is None:
        return False
    retcode = int(getattr(result, "retcode", -1))
    return retcode in (mt5.TRADE_RETCODE_DONE, 0)
