"""Paper broker + strategy manager.

Every signal becomes a Trade with one leg per take-profit the group sent
(TP1..TP5):

    leg 1  -> closes at TP1
    leg 2  -> closes at TP2
    ...    -> up to leg 5 (TP5)

Management rules (the "N entradas" strategy):
  * If price hits the stop loss first, every open leg closes at the SL (loss).
  * If price hits TP1 first, leg 1 closes at TP1 (profit) and every remaining
    leg's SL is moved to breakeven (entry) — the "protege automaticamente BE"
    step.
  * Each remaining leg then either runs to its own TP (full win) or gets
    closed at breakeven.

In simulation mode nothing touches a real broker: fills happen at the
simulated market price and P&L is computed directly from price difference.
"""

from typing import Dict, List, Optional

from app.config import CONTRACT_SIZE, PIP_SIZE
from app.db import DB
from app.models import Position, Signal, Trade
from app.risk.engine import compute_lots, effective_strategy_risk_pct
from app.execution.clock import Clock, SystemClock
from app.execution.market import MarketSimulator
from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    hybrid_protect_eligible,
    reduce_auto_protect,
    sl_exit_reason,
    time_stop_due,
)


def _contract(symbol: str) -> float:
    """Units per lot, per symbol — same as the live MT5 engine and backtest
    (XAUUSD contract 100, XAGUSD 5000, forex 100000)."""
    if symbol == "XAUUSD":
        return 100.0
    if symbol == "XAGUSD":
        return 5000.0
    return float(CONTRACT_SIZE)


def _pip_size(symbol: str) -> float:
    """Pip size per symbol (metals 0.01, forex 0.0001)."""
    return 0.01 if symbol in ("XAUUSD", "XAGUSD") else PIP_SIZE


def _pnl(direction: str, entry: float, close: float, lots: float,
         symbol: str = "") -> float:
    sign = 1.0 if direction == "buy" else -1.0
    return round(sign * (close - entry) * lots * _contract(symbol), 2)


def _legs(obj, max_legs: Optional[int] = None) -> list:
    """(leg_number, tp) pairs for every TP present on a Signal/Trade (> 0).

    `max_legs` caps how many legs are used (e.g. 2 = só TP1+TP2) — a base
    do registo de estratégias (app/strategies.py)."""
    out = []
    for i in range(1, 6):
        if max_legs is not None and i > max_legs:
            break
        tp = float(getattr(obj, f"take_profit_{i}", 0.0) or 0.0)
        if tp > 0:
            out.append((i, tp))
    return out


class SimEngine:
    def __init__(self, db: DB, market: MarketSimulator,
                 clock: Optional[Clock] = None, lane_id: str = "main"):
        self.db = db
        self.market = market
        self.lane_id = lane_id
        self.clock = clock or SystemClock()
        # Estado opcional da auto-gestão para a estratégia híbrida. A
        # estratégia principal continua a usar o caminho normal até ser
        # explicitamente promovida.
        self._auto_state: Dict[str, dict] = {}

    # ------------------------------------------------------------------ open

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
                leg_num: compute_lots(balance, per_leg, signal.entry, signal.stop_loss,
                                      pip_size=_pip_size(signal.symbol),
                                      contract_size=_contract(signal.symbol))
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
            execution_mode=(execution_meta or {}).get("execution_mode", "sim"),
            lane_id=(execution_meta or {}).get("lane_id"),
            entry_style=(execution_meta or {}).get("entry_style"),
        )
        self.db.insert_trade(trade)

        pending_style = (execution_meta or {}).get("entry_style") == "pending"
        for leg_num, tp in legs:
            self.db.insert_position(Position(
                id=f"p-{_new_id()}", trade_id=trade.id, leg=leg_num,
                symbol=signal.symbol, direction=signal.direction,
                lots=lots[leg_num], entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit=tp,
                status="pending_fill" if pending_style else "open",
                opened_at=now,
                lane_id=(execution_meta or {}).get("lane_id"),
            ))

        self.market.ensure(signal.symbol, signal.entry)
        return trade

    # ----------------------------------------------------------------- update

    def update(self, now: Optional[str] = None,
               exit_at_tp: Optional[int] = None,
               auto: Optional[dict] = None,
               entry_order_ttl: Optional[float] = None) -> List[dict]:
        """Evaluate every open trade against current prices.

        `exit_at_tp` (estratégias do registo): quando o TP com esse número
        bater, TODAS as pernas ainda abertas fecham nesse preço (ex.:
        "fechar tudo no TP2"). `auto` ativa a mesma política pura de
        escada/trailing/confirmação/time stop usada pelos outros motores;
        como o SimEngine só tem um preço pontual, ATR sem candles degrada
        para o trail fixo. `entry_order_ttl` (segundos) expira ordens
        pendentes da zona que não preencheram (Fase 1).

        Returns a list of events, e.g.:
          {"type": "close", "trade_id":..., "symbol":..., "leg":1,
           "direction":..., "price":..., "pnl":...}
          {"type": "be", "trade_id":..., "symbol":..., "price":...}
          {"type": "trade_closed", "trade_id":..., "pnl":...}
        """
        now = now or self.clock.iso()
        events: List[dict] = []

        for trade in self.db.open_trades():
            if (getattr(trade, "lane_id", None) or "main") != self.lane_id:
                continue
            price = self.market.price(trade.symbol)
            if price is None:
                continue

            # Fase 1 — ordens pendentes na zona: preenche quando o preço toca
            # o nível da entrada; expira após o TTL.
            events.extend(self._process_pending(
                trade, price, now, entry_order_ttl,
            ))

            if auto:
                events.extend(self._update_auto_trade(
                    trade, price, now, exit_at_tp, auto,
                ))
                continue

            positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
            legs = [positions[n] for n in range(1, 6) if n in positions]
            open_legs = [p for p in legs if p.status == "open"]
            if not open_legs:
                continue

            # --- leg 1: manages TP1 and the shared stop loss -----------------
            trade_reason = "mixed"
            leg1 = positions.get(1)
            if leg1 and leg1.status == "open":
                if self._hit(trade, price, trade.stop_loss, "sl"):
                    # stop-out: every open leg closes at the SL
                    trade_reason = "sl"
                    for p in open_legs:
                        events.append(self._close(trade, p, trade.stop_loss, now,
                                                  reason="sl"))
                elif self._hit(trade, price, trade.take_profit_1, "tp"):
                    trade_reason = "tp1"
                    events.append(self._close(trade, leg1, trade.take_profit_1, now,
                                              reason="tp1"))
                    moved = False
                    for p in open_legs:
                        if p.leg != 1:
                            # protect every remaining leg at breakeven
                            self.db.move_position_sl(p.id, trade.entry)
                            p.stop_loss = trade.entry
                            moved = True
                    if moved:
                        events.append({
                            "type": "be", "trade_id": trade.id,
                            "symbol": trade.symbol, "price": trade.entry,
                        })

            # --- exit_at_tp: quando o TP_N bater, fecha TUDO nesse preço ---
            if exit_at_tp is not None:
                tp_level = getattr(trade, f"take_profit_{exit_at_tp}", 0.0) or 0.0
                if tp_level and self._hit(trade, price, tp_level, "tp"):
                    # "fechar tudo no TP_N": qualquer perna ainda aberta fecha
                    # nesse preço (as que já fecharam no próprio TP ficam).
                    trade_reason = f"tp{exit_at_tp}"
                    for p in open_legs:
                        if p.status == "open":
                            events.append(self._close(trade, p, tp_level, now,
                                                      reason=f"tp{exit_at_tp}"))

            # --- remaining legs: their own TP or the (possibly moved) stop ---
            for p in open_legs:
                if p.leg == 1 or p.status != "open":
                    continue
                if self._hit(trade, price, p.stop_loss, "sl"):
                    trade_reason = sl_exit_reason(trade.direction, p.stop_loss, trade.entry)
                    events.append(self._close(
                        trade, p, p.stop_loss, now, reason=trade_reason,
                    ))
                elif self._hit(trade, price, p.take_profit, "tp"):
                    trade_reason = f"tp{p.leg}"
                    events.append(self._close(trade, p, p.take_profit, now,
                                              reason=f"tp{p.leg}"))

            # --- if no legs remain open, close the trade ---------------------
            remaining = [p for p in positions.values()
                         if p.status in ("open", "pending_fill")]
            if not remaining and trade.status == "open":
                pnl = sum(pos.pnl for pos in positions.values())
                self.db.close_trade(trade.id, now, pnl, reason=trade_reason)
                events.append({
                    "type": "trade_closed", "trade_id": trade.id,
                    "symbol": trade.symbol, "pnl": pnl, "reason": trade_reason,
                })

        return events

    def _process_pending(self, trade: Trade, price: float, now: str,
                         entry_order_ttl: Optional[float]) -> List[dict]:
        """Fase 1 — ordens pendentes na zona (entry_style=pending).

        Com um preço pontual (sem candles), o fill acontece quando o preço
        cruza o nível da entrada a partir do lado em que a ordem foi
        colocada (placement = market_price_at_open): BUY_LIMIT acima do
        nível preenche quando o preço desce até ele; BUY_STOP abaixo
        preenche quando sobe até ele. Espelho do par sell. Posições que não
        preencheram em `entry_order_ttl` segundos expiram.
        """
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
                events.append({
                    "type": "pending_fill", "trade_id": trade.id,
                    "symbol": trade.symbol, "leg": pos.leg, "price": entry,
                    "lots": pos.lots,
                })
            elif entry_order_ttl and entry_order_ttl > 0:
                age = _iso_elapsed_seconds(now, pos.opened_at)
                if age is not None and age > entry_order_ttl:
                    self.db.close_position(
                        pos.id, 0.0, 0.0, now, reason="pending_expired",
                    )
                    pos.status = "closed"
                    pos.exit_reason = "pending_expired"
                    events.append({
                        "type": "pending_expired", "trade_id": trade.id,
                        "symbol": trade.symbol, "leg": pos.leg,
                    })
        return events

    def _update_auto_trade(self, trade: Trade, price: float, now: str,
                           exit_at_tp: Optional[int], auto: dict) -> List[dict]:
        """Apply the shared mechanical policy in the SimEngine adapter.

        Fills remain SimEngine/SQLite concerns; every SL target, stair, trail,
        confirmation and time-stop boundary comes from ``protection.py`` so a
        simulated promotion cannot silently ignore auto-management.
        """
        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        open_legs = [p for p in positions.values() if p.status == "open"]
        if not open_legs:
            return []
        direction = trade.direction
        policy = AutoProtectionPolicy(
            direction=direction, entry=trade.entry,
            tp1=trade.take_profit_1, tp2=trade.take_profit_2,
            trail_factor=auto.get("trail_factor"),
            stair_levels=auto.get("stair_levels"),
            stair_from=int(auto.get("stair_from") or 1),
            be_buffer_pips=auto.get("be_buffer_pips"),
            pip_size=self._pip_size(trade.symbol),
        )
        persisted = self.db.load_protection_state("sim", trade.id) or {}
        defaults = {
            "peak": None, "trough": None, "tp1_ts": None,
            "auto_protected": False, "auto_protection_at": None,
            "auto_sl_by_leg": {}, "auto_protection_at_by_leg": {},
        }
        defaults.update(persisted)
        state = self._auto_state.setdefault(trade.id, defaults)
        # Rebuild the in-memory trigger after a server restart.  The broker/DB
        # already know that TP1 closed and which SL was applied; losing that
        # fact would silently disable trailing, time-stop and hybrid protect.
        self._restore_auto_state(trade, positions, state, now)
        events: List[dict] = []

        def remember_sl(pos: Position, level: float) -> None:
            state["auto_sl_by_leg"][pos.leg] = level
            state["auto_protection_at_by_leg"].setdefault(pos.leg, now)

        def hit(level: float, kind: str) -> bool:
            if direction == "buy":
                return price <= level if kind == "sl" else price >= level
            return price >= level if kind == "sl" else price <= level

        def close_all(level: float, reason: str) -> None:
            for pos in open_legs:
                if pos.status == "open":
                    events.append(self._close(trade, pos, level, now, reason=reason))

        leg1 = positions.get(1)
        if leg1 and leg1.status == "open" and hit(trade.stop_loss, "sl"):
            close_all(trade.stop_loss, "sl")
            events.extend(self._finish_auto_trade(trade, positions, now, "sl"))
            return events

        # Autonomous early protect (generic policy for future candidates;
        # auto_protect_5 was removed from the registry on 01/09): no Telegram
        # message is needed. The trigger is reached at the known tick price;
        # the close level and group-protect SL formula are shared with the
        # backtest.
        decision = reduce_auto_protect(
            direction=direction, price=price, entry=trade.entry,
            stop_loss=trade.stop_loss, tp1=trade.take_profit_1,
            leg1_open=bool(leg1 and leg1.status == "open"),
            remaining_sls={p.leg: p.stop_loss for p in open_legs if p.leg != 1},
            trigger_fraction=auto.get("protect_trigger"),
            buffer_fraction=auto.get("protect_buffer"),
            already_triggered=bool(state.get("auto_protected")),
        )
        if decision.triggered and leg1:
            events.append(self._close(
                trade, leg1, decision.trigger_level, now, reason="auto_protect",
            ))
            for leg, improved in decision.move_sls:
                pos = positions.get(leg)
                if pos and pos.status == "open":
                    self.db.move_position_sl(pos.id, improved)
                    pos.stop_loss = improved
                    remember_sl(pos, improved)
            state["auto_protected"] = True
            state["auto_protection_at"] = now

        if leg1 and leg1.status == "open" and trade.take_profit_1 \
                and hit(trade.take_profit_1, "tp"):
            events.append(self._close(
                trade, leg1, trade.take_profit_1, now, reason="tp1",
            ))
            floor = policy.initial_floor()
            for pos in open_legs:
                if pos.leg != 1:
                    leg_floor = policy.initial_floor(
                        anchor=getattr(pos, "entry", None))
                    self.db.move_position_sl(pos.id, leg_floor)
                    pos.stop_loss = leg_floor
                    remember_sl(pos, leg_floor)
            state["tp1_ts"] = self._epoch(now)
            if direction == "buy":
                state["peak"] = price
            else:
                state["trough"] = price

        if exit_at_tp is not None:
            level = getattr(trade, f"take_profit_{exit_at_tp}", 0.0) or 0.0
            if level and hit(level, "tp"):
                close_all(level, f"tp{exit_at_tp}")
                events.extend(self._finish_auto_trade(
                    trade, positions, now, f"tp{exit_at_tp}"
                ))
                return events

        if time_stop_due(state.get("tp1_ts"), self._epoch(now),
                         auto.get("time_stop_minutes")):
            for pos in open_legs:
                if pos.leg != 1 and pos.status == "open":
                    events.append(self._close(trade, pos, price, now,
                                              reason="time_stop"))
            events.extend(self._finish_auto_trade(trade, positions, now,
                                                   "time_stop"))
            return events

        if state.get("tp1_ts") is not None:
            extreme = state.get("peak") if direction == "buy" else state.get("trough")
            target = policy.trailing_target(
                None, extreme, distance=policy.trail_distance(),
            )
            if target is not None:
                for pos in open_legs:
                    if pos.leg != 1 and pos.status == "open":
                        improved = policy.trailing_target(
                            pos.stop_loss, extreme, distance=policy.trail_distance(),
                        )
                        if improved is not None:
                            self.db.move_position_sl(pos.id, improved)
                            pos.stop_loss = improved
                            remember_sl(pos, improved)

        for pos in open_legs:
            if pos.leg == 1 or pos.status != "open":
                continue
            if hit(pos.stop_loss, "sl"):
                reason = sl_exit_reason(direction, pos.stop_loss, trade.entry)
                events.append(self._close(trade, pos, pos.stop_loss, now,
                                          reason=reason))
                if auto.get("confirm_filter"):
                    open_numbers = [p.leg for p in open_legs if p.status == "open"]
                    for leg in confirmation_legs(pos.leg, open_numbers):
                        candidate = positions[leg]
                        if candidate.status == "open":
                            events.append(self._close(
                                trade, candidate, price, now, reason="confirm",
                            ))
            elif hit(pos.take_profit, "tp"):
                events.append(self._close(
                    trade, pos, pos.take_profit, now, reason=f"tp{pos.leg}",
                ))
                for candidate in open_legs:
                    if candidate.leg > pos.leg and candidate.status == "open":
                        stair = policy.stair_target(
                            candidate.stop_loss, pos.take_profit, candidate.leg,
                        )
                        if stair is not None:
                            self.db.move_position_sl(candidate.id, stair)
                            candidate.stop_loss = stair
                            remember_sl(candidate, stair)

        if state.get("tp1_ts") is not None:
            if direction == "buy":
                state["peak"] = max(state.get("peak") or price, price)
            else:
                state["trough"] = min(state.get("trough") or price, price)
        if any(pos.status == "open" for pos in positions.values()):
            self.db.save_protection_state("sim", trade.id, state, updated_at=now)
        events.extend(self._finish_auto_trade(trade, positions, now, None))
        return events

    @staticmethod
    def _restore_auto_state(trade: Trade, positions: dict,
                            state: dict, now: str) -> None:
        leg1 = positions.get(1)
        if not leg1 or leg1.status != "closed":
            return
        if leg1.exit_reason == "auto_protect":
            state["auto_protected"] = True
            state.setdefault("auto_protection_at", leg1.closed_at or now)
            return
        if leg1.exit_reason not in ("tp", "tp1"):
            return
        if state.get("tp1_ts") is None:
            state["tp1_ts"] = SimEngine._epoch(leg1.closed_at or now)
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

    @staticmethod
    def _epoch(iso: str) -> float:
        from datetime import datetime
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _finish_auto_trade(self, trade: Trade, positions: dict, now: str,
                           reason: Optional[str]) -> List[dict]:
        if any(pos.status == "open" for pos in positions.values()):
            return []
        pnl = sum(pos.pnl for pos in positions.values())
        if reason is None:
            closed = [pos for pos in positions.values() if pos.exit_reason]
            reason = (max(closed, key=lambda pos: pos.closed_at or "").exit_reason
                      if closed else "mixed")
        self.db.close_trade(trade.id, now, pnl, reason=reason)
        self._auto_state.pop(trade.id, None)
        self.db.delete_protection_state("sim", trade.id)
        return [{"type": "trade_closed", "trade_id": trade.id,
                 "symbol": trade.symbol, "pnl": pnl, "reason": reason}]

    # ------------------------------------------------------- hybrid protect

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
        """Merge a group protect with the current mechanical SL in simulation."""
        now = now or self.clock.iso()
        if trade.strategy_id != "auto_escada_3_hibrida":
            return []
        if trade.status != "open":
            return []
        price = self.market.price(trade.symbol)
        if price is None:
            return []
        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        open_legs = [p for p in positions.values() if p.status == "open"]
        if not open_legs:
            return []
        persisted = self.db.load_protection_state("sim", trade.id) or {}
        defaults = {
            "peak": None, "trough": None, "tp1_ts": None,
            "auto_protected": False, "auto_protection_at": None,
            "auto_sl_by_leg": {}, "auto_protection_at_by_leg": {},
        }
        defaults.update(persisted)
        state = self._auto_state.setdefault(trade.id, defaults)
        self._restore_auto_state(trade, positions, state, now)
        auto_by_leg = state.get("auto_sl_by_leg", {})
        auto_at_by_leg = state.get("auto_protection_at_by_leg", {})
        remaining = [p for p in open_legs if p.leg != 1]
        representative = None
        if remaining:
            vals = [auto_by_leg.get(p.leg) for p in remaining
                    if auto_by_leg.get(p.leg) is not None]
            if vals:
                representative = max(vals) if trade.direction == "buy" else min(vals)
        tp1 = trade.take_profit_1 or trade.entry
        leg1 = positions.get(1)
        leg1_decision = decide_protect(
            direction=trade.direction, auto_sl=representative,
            group_sl=group_sl, entry=trade.entry, tp1=tp1,
            price_at_event=price,
            leg1_open=bool(leg1 and leg1.status == "open"),
            auto_protection_at=(auto_at_by_leg.get(remaining[0].leg)
                                if remaining else None),
            group_received_at=now, pip_size=self._pip_size(trade.symbol),
        )
        events: List[dict] = []
        leg1_eligible = hybrid_protect_eligible(
            representative,
            (auto_at_by_leg.get(remaining[0].leg) if remaining else None),
        )
        if leg1 and leg1.status == "open" \
                and leg1_eligible and leg1_decision.leg1_action == "closed":
            events.append(self.close_leg_at_market(
                trade.id, 1, now=now, reason="protect", msg_id=msg_id
            ) or {})
        for pos in remaining:
            auto_sl = auto_by_leg.get(pos.leg)
            decision = decide_protect(
                direction=trade.direction, auto_sl=auto_sl,
                group_sl=group_sl, entry=trade.entry, tp1=tp1,
                price_at_event=price,
                leg1_open=bool(leg1 and leg1.status == "open"),
                auto_protection_at=auto_at_by_leg.get(pos.leg),
                group_received_at=now, pip_size=self._pip_size(trade.symbol),
            )
            eligible = hybrid_protect_eligible(auto_sl, auto_at_by_leg.get(pos.leg))
            applied = decision.applied_sl if eligible else pos.stop_loss
            if applied is not None:
                applied = (max(pos.stop_loss, applied)
                           if trade.direction == "buy"
                           else min(pos.stop_loss, applied))
                if eligible and applied != pos.stop_loss:
                    self.db.move_position_sl(pos.id, applied)
                    pos.stop_loss = applied
            self.db.insert_protect_latency({
                "engine": "sim", "strategy_id": trade.strategy_id or "",
                "signal_id": trade.signal_id, "trade_id": trade.id,
                "message_id": msg_id, "symbol": trade.symbol,
                "direction": trade.direction,
                "auto_protection_at": auto_at_by_leg.get(pos.leg),
                "group_received_at": now, "auto_sl": auto_sl,
                "group_sl": group_sl, "applied_sl": applied,
                "winner": decision.winner, "delta_pips": decision.delta_pips,
                "delta_seconds": decision.delta_seconds,
                "leg1_action": leg1_decision.leg1_action,
                "status": decision.status, "created_at": now,
            })
        positions = {p.leg: p for p in self.db.positions_for_trade(trade.id)}
        if not [p for p in positions.values() if p.status == "open"]:
            pnl = sum(p.pnl for p in positions.values())
            self.db.close_trade(trade.id, now, pnl, reason="protect")
            events.append({"type": "trade_closed", "trade_id": trade.id,
                           "symbol": trade.symbol, "pnl": pnl,
                           "reason": "protect"})
        return [e for e in events if e]

    def _pip_size(self, symbol: str) -> float:
        return 0.01 if symbol in ("XAUUSD", "XAGUSD") else PIP_SIZE

    # ------------------------------------------------------- manage (group)

    def move_leg_sl(self, trade_id: str, leg: int, new_sl: float) -> Optional[float]:
        """Move a single leg's stop loss (group "move SL above best entries").

        Returns the new SL, or None when the leg is not open.
        """
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status == "open"), None)
        if pos is None:
            return None
        self.db.move_position_sl(pos.id, new_sl)
        pos.stop_loss = new_sl
        return new_sl

    def close_leg_at_market(self, trade_id: str, leg: int,
                            now: Optional[str] = None,
                            reason: str = "close_leg",
                            msg_id: Optional[str] = None) -> Optional[dict]:
        """Close a single leg at the current market price (group "close first
        entries"). Returns the close event, or None when already closed.
        Pernas `pending_fill` (ordem na zona) são canceladas sem P&L."""
        now = now or self.clock.iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return None
        pos = next((p for p in self.db.positions_for_trade(trade_id)
                    if p.leg == leg and p.status in ("open", "pending_fill")), None)
        if pos is None:
            return None
        if pos.status == "pending_fill":
            return self._cancel_pending(pos, now, reason=reason, msg_id=msg_id)
        price = self.market.price(trade.symbol)
        if price is None:
            return None
        return self._close(trade, pos, price, now, reason=reason, msg_id=msg_id)

    def _cancel_pending(self, pos: Position, now: str, reason: str,
                        msg_id: Optional[str] = None) -> dict:
        """Cancela uma perna pendente (ordem na zona) sem P&L."""
        pos.status = "closed"
        pos.close_price = pos.entry
        pos.pnl = 0.0
        pos.closed_at = now
        pos.exit_reason = reason
        pos.exit_msg_id = msg_id
        self.db.close_position(pos.id, pos.entry, 0.0, now, reason=reason,
                               msg_id=msg_id)
        return {
            "type": "close", "trade_id": pos.trade_id,
            "symbol": pos.symbol, "leg": pos.leg,
            "direction": pos.direction, "price": pos.entry,
            "pnl": 0.0, "reason": reason,
        }

    # ------------------------------------------------------------ close all

    def close_trade_at_market(self, trade_id: str, now: Optional[str] = None,
                              reason: str = "close_all",
                              msg_id: Optional[str] = None) -> List[dict]:
        """Close every open leg of a trade at the current market price.

        Used when a Telegram signal is deleted: no point keeping a position
        whose signal no longer exists.
        """
        now = now or self.clock.iso()
        trade = self.db.get_trade(trade_id)
        if not trade or trade.status != "open":
            return []
        price = self.market.price(trade.symbol)
        if price is None:
            return []

        events = []
        for pos in self.db.positions_for_trade(trade_id):
            if pos.status == "open":
                events.append(self._close(trade, pos, price, now,
                                          reason=reason, msg_id=msg_id))
            elif pos.status == "pending_fill":
                events.append(self._cancel_pending(
                    pos, now, reason=reason, msg_id=msg_id))

        pnl = sum(pos.pnl for pos in self.db.positions_for_trade(trade_id))
        self.db.close_trade(trade_id, now, pnl, reason=reason)
        events.append({
            "type": "trade_closed", "trade_id": trade_id,
            "symbol": trade.symbol, "pnl": pnl, "reason": reason,
        })
        return events

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _hit(trade: Trade, price: float, level: float, kind: str) -> bool:
        """True when the current price has crossed `level` for this trade."""
        if trade.direction == "buy":
            return price <= level if kind == "sl" else price >= level
        return price >= level if kind == "sl" else price <= level

    def _close(self, trade: Trade, pos: Position, price: float, now: str,
               reason: Optional[str] = None, msg_id: Optional[str] = None) -> dict:
        pnl = _pnl(trade.direction, pos.entry, price, pos.lots, trade.symbol)
        # mutate the in-memory object too, so the trade-close detection below
        # sees the updated status (we hold the same objects in `positions`)
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    # UTC explícito (+00:00) — o browser (dashboard) interpreta o timestamp
    # como UTC e converte para a hora local do utilizador. Timestamps naive
    # locais faziam a contagem da janela de edição aparecer inflada ("3000s").
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_elapsed_seconds(now: str, then: str) -> Optional[float]:
    """Segundos entre dois timestamps ISO (UTC-aware); None se inválido."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(now)
        b = datetime.fromisoformat(then)
    except (ValueError, TypeError):
        return None
    if a.tzinfo is None:
        a = a.astimezone()
    if b.tzinfo is None:
        b = b.astimezone()
    return (a - b).total_seconds()


def _new_id() -> str:
    import secrets
    return secrets.token_hex(6)
