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
from app.execution.engine import _legs, _new_id, _now_iso
from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    hybrid_protect_eligible,
    monotonic_sl,
    sl_exit_reason,
    time_stop_due_iso,
)
from app.models import Position, Signal, Trade

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
                 require_hedged: bool = True):
        self._mt5 = None
        self._login = login
        self._server = server
        self._password = password
        self._require_hedged = bool(require_hedged)
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
                ok = mt5.initialize(login=self._login, password=self._password,
                                    server=self._server)
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
            rates = self._mt5.copy_rates_from_pos(meta["mt5"], timeframe, 1, count) or []
        except Exception as exc:
            print(f"[mt5] copy_rates({app_symbol}) falhou: {exc}")
            return []
        out = []
        for r in rates:
            out.append({
                "time": int(getattr(r, "time", 0)),
                "open": float(getattr(r, "open", 0.0)),
                "high": float(getattr(r, "high", 0.0)),
                "low": float(getattr(r, "low", 0.0)),
                "close": float(getattr(r, "close", 0.0)),
                "tick_volume": float(getattr(r, "tick_volume", 0.0)),
            })
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
    """Broker execution engine, same interface as SimEngine.

    dry_run=True (default) logs every order and simulates fills at the MT5
    tick — nothing reaches the broker. dry_run=False sends real orders via
    mt5.order_send and reads P&L from MT5 positions.
    """

    def __init__(self, db: DB, provider: MT5Provider, market, dry_run: bool = MT5_DRY_RUN,
                 logger=None):
        self.db = db
        self.provider = provider
        self.market = market          # used for the fallback price in dry-run
        self.dry_run = dry_run
        self.logger = logger or print
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
        self._auto_clock = _t
        # Spread is sampled once per minute per symbol; the raw quote remains
        # available on every tick, but the DB stores a bounded time series.
        self._last_spread_observation = 0.0
        self._spread_observation_interval = 60.0

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
                   max_legs: Optional[int] = None) -> Trade:
        """Open a one-leg-per-TP trade for a signal, splitting the risk evenly.

        `lots_override` (when set) sizes every leg at that fixed volume
        instead of the risk-based calculation — useful for demo tests.
        `max_legs` caps the number of legs (estratégias do registo central).
        """
        trade_id = trade_id or "t-" + _new_id()
        now = now or _now_iso()

        meta = self._sym_meta(signal.symbol) or self.provider._symbols.get(signal.symbol)
        if meta is None:
            raise ValueError(f"símbolo {signal.symbol} não mapeado para MT5 "
                             f"(adiciona em MT5_SYMBOLS)")
        legs = _legs(signal, max_legs)
        n = len(legs)
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
        )
        self.db.insert_trade(trade)

        positions = []
        for leg_num, tp in legs:
            pos = Position(
                id=f"p-{_new_id()}", trade_id=trade.id, leg=leg_num,
                symbol=signal.symbol, direction=signal.direction,
                lots=lots[leg_num], entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit=tp,
                status="open", opened_at=now,
            )
            self.db.insert_position(pos)
            positions.append(pos)

        mt5_sym = meta["mt5"]
        # seleciona o símbolo no Market Watch ANTES de enviar ordens — sem
        # isto o symbol_info_tick devolve None na primeira trade do símbolo
        # (crash 'NoneType' .ask) e o sinal ficava em awaiting_edit a repetir
        self.provider.ensure_symbol(signal.symbol)
        if not self.dry_run:
            sent, retcodes = self._send_open_orders(trade, positions, mt5_sym)
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
        else:
            self._log_order("open", {
                "trade": trade.id, "symbol": mt5_sym, "direction": signal.direction,
                "lots": {leg_num: lots[leg_num] for leg_num, _ in legs},
                "entry": signal.entry, "sl": signal.stop_loss,
                "tps": {leg_num: tp for leg_num, tp in legs},
                "note": "dry-run: ordem registada, não enviada ao broker",
            })
        return trade

    def _send_open_orders(self, trade: Trade, positions: List[Position],
                          mt5_sym: str) -> tuple:
        """Real orders: one market order per leg, SL/TP set server-side.

        After each fill we resolve the position ticket by the unique comment
        (`trade.id:legN`) — the deal ticket returned by order_send is an
        *order* ticket, useless for position lookups later. Returns
        `(sent, retcodes)` — the number of orders accepted and the retcode of
        every leg the broker refused (for a precise error/alert upstream).
        """
        mt5 = self.provider._mt5
        order_type = mt5.ORDER_TYPE_BUY if trade.direction == "buy" else mt5.ORDER_TYPE_SELL
        sent = 0
        retcodes: List = []
        for pos in positions:
            tick = mt5.symbol_info_tick(mt5_sym)
            if tick is None or not (tick.ask > 0 and tick.bid > 0):
                # símbolo ainda sem tick (fora do Market Watch / a carregar)
                # — falha esta perna em vez de crashar e repetir a trade toda
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": "NO_TICK",
                    "comment": "symbol_info_tick sem preço (símbolo fora do Market Watch?)",
                })
                retcodes.append("NO_TICK")
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
            result = mt5.order_send(request)
            if not _order_ok(result, mt5):
                rc = getattr(result, "retcode", "?")
                self._log_order("open_failed", {
                    "trade": trade.id, "leg": pos.leg,
                    "retcode": rc,
                    "comment": getattr(result, "comment", ""),
                })
                retcodes.append(rc)
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
        return sent, retcodes

    # --------------------------------------------------------------- update

    def update(self, now: Optional[str] = None,
               exit_at_tp: Optional[int] = None,
               auto: Optional[dict] = None) -> List[dict]:
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

        Same event contract as SimEngine: {"type": "close"|"be"|"trade_closed"}.
        """
        now = now or _now_iso()
        events: List[dict] = []

        for trade in self.db.open_trades():
            if not self.dry_run:
                events += self._update_real(trade, now, exit_at_tp, auto)
            else:
                events += self._update_dry(trade, now, exit_at_tp)
        # drena os avisos de SL recusado/clamped para os eventos do update
        events += self._sl_warnings
        self._sl_warnings = []
        return events

    def _update_dry(self, trade: Trade, now: str,
                    exit_at_tp: Optional[int] = None) -> List[dict]:
        """Dry-run: simulate fills at the current MT5 tick (or feed price)."""
        events: List[dict] = []
        trade_reason: Optional[str] = None
        price = self._price(trade.symbol)
        if price is None:
            return events

        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        legs = [positions[n] for n in range(1, 6) if n in positions]
        open_legs = [p for p in legs if p.status == "open"]
        if not open_legs:
            return events

        leg1 = positions.get(1)
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
                for p in open_legs:
                    if p.leg != 1:
                        self.db.move_position_sl(p.id, trade.entry)
                        p.stop_loss = trade.entry
                        moved = True
                if moved:
                    events.append({
                        "type": "be", "trade_id": trade.id,
                        "symbol": trade.symbol, "price": trade.entry,
                    })

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
        return events

    def _update_real(self, trade: Trade, now: str,
                     exit_at_tp: Optional[int] = None,
                     auto: Optional[dict] = None) -> List[dict]:
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
        legs = [positions[n] for n in range(1, 6) if n in positions]
        open_legs = [p for p in legs if p.status == "open"]
        auto_policy = None
        if auto:
            auto_policy = AutoProtectionPolicy(
                direction=trade.direction, entry=trade.entry,
                tp1=getattr(trade, "take_profit_1", 0.0) or 0.0,
                tp2=getattr(trade, "take_profit_2", 0.0) or 0.0,
                trail_factor=auto.get("trail_factor"),
                stair_levels=auto.get("stair_levels"),
                stair_from=int(auto.get("stair_from") or 1),
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

        self._restore_auto_state(trade, positions, now)
        tp1_ts = self._auto_state.get(trade.id, {}).get("tp1_ts")
        for leg_num, (price, pnl) in closed_legs.items():
            pos = positions[leg_num]
            reason = "tp" if pnl >= 0 else "sl"
            events.append(self._record_close(trade, pos, price, pnl, now,
                                             reason=reason))
            if leg_num == 1 and pnl >= 0:
                # leg 1 closed at TP1 (profit): protect every remaining leg
                # at breakeven
                moved = False
                for other in open_legs:
                    if other.leg != 1:
                        self._move_sl_breakeven(trade, other, mt5)
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
        return events

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
        return (float(last.price), float(last.profit or 0.0))

    def _move_sl_breakeven(self, trade: Trade, pos: Position, mt5) -> None:
        """Modify leg 2's SL to entry (breakeven) after TP1 hit."""
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
                "sl": float(trade.entry),
                "tp": float(pos.take_profit),
            }
            result = mt5.order_send(request)
            retcode = getattr(result, "retcode", "?")
            accepted = _order_ok(result, mt5)
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind="breakeven",
                requested_sl=trade.entry, valid_sl=trade.entry,
                retcode=int(retcode) if str(retcode).lstrip("-").isdigit() else None,
                accepted=accepted, error=getattr(result, "comment", "") or "",
            )
            self._log_order("modify_sl_be", {
                "trade": trade.id, "leg": pos.leg, "ticket": ticket,
                "sl": trade.entry, "retcode": retcode,
            })
            # A BD só pode refletir o BE depois de o broker confirmar o
            # modify. Sem isto, o caminho real continuava a mostrar o SL
            # original apesar de o MT5 já estar protegido, e o trailing
            # calculava o próximo nível a partir de um estado falso.
            if _order_ok(result, mt5):
                self.db.move_position_sl(pos.id, trade.entry)
                pos.stop_loss = trade.entry
        except Exception as exc:
            # A falha antes de receber um retcode continua a ser uma tentativa
            # operacional observável; não pode desaparecer apenas no log.
            self.db.insert_mt5_modify_event(
                _now_iso(), trade_id=trade.id, position_id=pos.id,
                symbol=trade.symbol, leg=pos.leg, kind="breakeven",
                requested_sl=trade.entry, valid_sl=trade.entry,
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
        state = self._auto_state.setdefault(trade.id, {
            "tp1_ts": None, "peak": None, "last_mod": {},
            "auto_sl_by_leg": {}, "auto_protection_at_by_leg": {},
        })
        leg1 = positions.get(1)
        if not leg1 or leg1.status != "closed" \
                or leg1.exit_reason not in ("tp", "tp1"):
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
        remaining = [p for p in positions if p.status == "open"]
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
            return [{
                "type": "trade_closed", "trade_id": trade.id,
                "symbol": trade.symbol, "pnl": pnl, "reason": reason,
            }]
        return []

    # ------------------------------------------------------- manage (group)

    def move_leg_sl(self, trade_id: str, leg: int, new_sl: float) -> Optional[float]:
        """Move a single leg's SL (group "move stop loss above best entries")."""
        trade = self.db.get_trade(trade_id)
        if not trade:
            return None
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status == "open"), None)
        if pos is None:
            return None
        if not self.dry_run:
            retcode = self._modify_sl(trade, pos, new_sl, kind="group")
            if retcode not in (10009, 0):
                return None
        else:
            self._log_order("modify_sl", {
                "trade": trade.id, "leg": pos.leg, "sl": new_sl,
                "note": "dry-run: SL atualizado na BD apenas",
            })
        self.db.move_position_sl(pos.id, new_sl)
        pos.stop_loss = new_sl
        return new_sl

    def close_leg_at_market(self, trade_id: str, leg: int,
                            now: Optional[str] = None,
                            reason: str = "close_leg",
                            msg_id: Optional[str] = None) -> Optional[dict]:
        """Close a single leg at market (group "close first entries")."""
        now = now or _now_iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return None
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status == "open"), None)
        if pos is None:
            return None
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
                result = mt5.order_send(request)
                if _order_ok(result, mt5):
                    close_pnl = self._close_pnl_from_deals(p.ticket, mt5)
                    if close_pnl == 0:
                        close_pnl = float(getattr(p, "profit", 0) or 0.0)
                    return self._record_close(trade, pos, float(result.price),
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
            result = mt5.order_send(request)
            retcode = int(getattr(result, "retcode", -1))
            accepted = retcode in (10009, 0)
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
                      stair: bool = False) -> bool:
        """Move o SL de uma perna para `level` (escada/trailing) com os três
        níveis (target→valid→applied) e throttling: só modifica se o novo SL
        melhorar ≥ 1 pip e ≤ 1×/min por perna — o trailing não spammeia
        ordens SLTP. `stair=True` marca a razão `stair` (escada); trailing
        marca `trail`."""
        if pos.status != "open":
            return False
        current = pos.stop_loss or 0.0
        kind = "stair" if stair else "trail"
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

    def close_trade_at_market(self, trade_id: str, now: Optional[str] = None,
                              reason: str = "close_all",
                              msg_id: Optional[str] = None) -> List[dict]:
        """Close every open leg at the current market price (signal deleted)."""
        now = now or _now_iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return []

        events: List[dict] = []
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
                result = mt5.order_send(request)
                if _order_ok(result, mt5) and pos:
                    # prefer the broker's booked P&L from the exit deal; fall
                    # back to the position's floating profit at close time
                    close_pnl = self._close_pnl_from_deals(p.ticket, mt5)
                    if close_pnl == 0:
                        close_pnl = float(getattr(p, "profit", 0) or 0.0)
                    events.append(self._record_close(
                        trade, pos, float(result.price), close_pnl, now,
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
