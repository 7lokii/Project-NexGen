"""Paper trading forward por estratégia (corrida paralela).

Cada estratégia do registo (app/strategies.py), **incluindo a principal**,
tem uma **conta virtual própria** (`paper_accounts`) que começa com 1000 € e
evolui ao vivo: quando um sinal real executa na conta principal, as estratégias
abrem a mesma trade *em papel* com os seus parâmetros (max_legs, exit_at_tp) e
o tick do servidor atualiza as posições contra os preços do market feed
(mesma lógica SL/TP/BE do SimEngine).

Isto permite comparar as estratégias em corrida paralela durante dias com
dados novos — não é o replay do mês passado, é o futuro a acontecer em papel.

Nunca toca na conta real nem nas trades da BD principal (tabelas
paper_trades/paper_positions/paper_accounts são separadas).
"""

import time
from typing import Dict, List, Optional

from app.db import DB
from app.execution.engine import _legs, _now_iso, _new_id, _pnl, _contract, _pip_size
from app.execution.market import MarketSimulator
from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    group_protect_sl,
    hybrid_protect_eligible,
    sl_exit_reason,
    time_stop_due,
)
from app.models import Position, Signal, Trade
from app.risk.engine import compute_lots

START_BALANCE = 1000.0


class PaperRace:
    def __init__(self, db: DB, market: MarketSimulator):
        self.db = db
        self.market = market
        # estado do trailing por trade (pico/vale desde o TP1) — em memória;
        # após um restart recomeça do preço atual (aproximado, documentado)
        self._auto: Dict[str, dict] = {}
        # Ordem de fecho dentro de cada processo. `closed_at` tem resolução de
        # segundos, por isso este desempate é necessário quando uma escada/
        # confirmação fecha várias pernas no mesmo tick.
        self._close_seq = 0
        # pares sinal:auto já notificados como divergência final. Um evento
        # provisório (referência fechou, auto ainda aberta) fica separado para
        # poder ser atualizado quando o P&L final da auto existir.
        self._diverged: set = set()
        self._divergence_pending: set = set()
        # contador de sinais saltados pelo kill diário por estratégia
        # (em memória — exposto no snapshot para o painel da Fase 2)
        self._skipped_daily_stop: Dict[str, int] = {}

    # ----------------------------------------------------------------- open

    def open_for_signal(self, signal: Signal, risk_pct: float,
                        strategy_ids: Optional[List[str]] = None,
                        daily_stop_pct: Optional[float] = None) -> List[Trade]:
        """Abre a trade simulada para cada estratégia visível (com os seus
        parâmetros) quando o sinal executa na principal. Devolve as trades
        abertas (vazia se não houver estratégias).

        Cada estratégia do registo — INCLUINDO a principal — tem a sua conta
        virtual própria de 1000 €, para a corrida paralela comparar todas em
        igualdade de circunstâncias (não o saldo real da MT5). As ocultas
        (tp2/tp2_protect) não correm em papel — só no backtest.

        `strategy_ids` permite restringir as estratégias (testes/estudos);
        por omissão abre para todas as visíveis do registo.

        `daily_stop_pct` modela o kill diário da produção (auto kill) na
        corrida paper: quando o P&L realizado do dia da conta virtual da
        estratégia ≤ -saldo×pct/100, o sinal NÃO abre em papel nessa conta —
        exatamente o que aconteceria à produção se a estratégia fosse a
        principal (risk.engine.daily_loss_exceeded; espelho de
        apply_daily_stop no backtest). 0/None = sem limite."""
        from app.strategies import HIDDEN_STRATEGIES, get_strategy, STRATEGIES
        # as ocultas (tp2/tp2_protect) não correm em papel — só no backtest
        ids = strategy_ids or [s for s in STRATEGIES if s not in HIDDEN_STRATEGIES]
        trades: List[Trade] = []
        skipped: List[str] = []
        for sid in ids:
            strat = get_strategy(sid)
            balance = self.db.ensure_paper_account(sid, START_BALANCE)
            if self._daily_stop_hit(sid, balance, daily_stop_pct):
                skipped.append(sid)
                continue
            trade = self._open_one(signal, sid, strat, risk_pct, balance)
            if trade:
                trades.append(trade)
        if skipped:
            self._skipped_daily_stop = self._skipped_daily_stop or {}
            for sid in skipped:
                self._skipped_daily_stop[sid] = self._skipped_daily_stop.get(sid, 0) + 1
        return trades

    def _daily_stop_hit(self, strategy_id: str, balance: float,
                        stop_pct: Optional[float]) -> bool:
        """True quando a conta virtual já estourou o limite de perda diária
        (P&L realizado de hoje ≤ -saldo×stop_pct/100) — espelho do kill
        diário da produção (risk.engine.daily_loss_exceeded)."""
        if not stop_pct or stop_pct <= 0:
            return False
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).date().isoformat()
        realized = self.db.paper_realized_today(strategy_id, day)
        return realized <= -balance * float(stop_pct) / 100.0

    def _open_one(self, signal: Signal, strategy_id: str, strat: dict,
                  risk_pct: float, balance: float) -> Optional[Trade]:
        legs = _legs(signal, strat["max_legs"])
        if not legs:
            return None
        n = len(legs)
        per_leg = (risk_pct / n) if n else risk_pct
        lots = {
            leg_num: compute_lots(balance, per_leg, signal.entry, signal.stop_loss,
                                  pip_size=_pip_size(signal.symbol),
                                  contract_size=_contract(signal.symbol))
            for leg_num, _ in legs
        }
        now = _now_iso()
        trade = Trade(
            id="pt-" + _new_id(), signal_id=signal.id, symbol=signal.symbol,
            direction=signal.direction, entry=signal.entry,
            stop_loss=signal.stop_loss, take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2, take_profit_3=signal.take_profit_3,
            take_profit_4=signal.take_profit_4, take_profit_5=signal.take_profit_5,
            risk_pct=risk_pct,
            lots_1=lots.get(1, 0.0), lots_2=lots.get(2, 0.0),
            lots_3=lots.get(3, 0.0), lots_4=lots.get(4, 0.0),
            lots_5=lots.get(5, 0.0), status="open", opened_at=now,
            strategy_id=strategy_id,
        )
        self.db.insert_paper_trade(trade)
        # A paper account may receive the first signal for a symbol before
        # the market feed has created its price entry. Seed it exactly as the
        # SimEngine does; otherwise the first tick/management message is
        # silently ignored because `market.price()` returns None.
        self.market.ensure(signal.symbol, signal.entry)
        for leg_num, tp in legs:
            self.db.insert_paper_position(Position(
                id="pp-" + _new_id(), trade_id=trade.id, leg=leg_num,
                symbol=signal.symbol, direction=signal.direction,
                lots=lots[leg_num], entry=signal.entry,
                stop_loss=signal.stop_loss, take_profit=tp,
                status="open", opened_at=now, strategy_id=strategy_id,
            ))
        return trade

    # ---------------------------------------------------------------- tick

    def tick(self, risk_pct: float) -> List[dict]:
        """Atualiza todas as trades de papel abertas contra os preços atuais
        e devolve eventos (para notificações). Aplica a lógica SL/TP/BE de
        cada estratégia E a **auto-gestão** (escada/trailing/confirmação/
        time stop, espelho do backtest) quando a estratégia a define."""
        from app.strategies import HIDDEN_STRATEGIES, STRATEGIES, get_strategy
        events: List[dict] = []
        for sid in STRATEGIES:
            if sid in HIDDEN_STRATEGIES:
                continue
            strat = get_strategy(sid)
            for trade in self.db.paper_open_trades(sid):
                events += self._update_trade(trade, strat, risk_pct)
        return events

    def _update_trade(self, trade: dict, strat: dict,
                      risk_pct: float) -> List[dict]:
        """Atualiza uma trade de papel contra o preço atual. Espelha a
        sequência do backtest (`simulate_signal`): SL partilhado primeiro,
        TP1 → BE/escada, exit_at_tp, depois por perna (SL/TP + confirmação +
        escada) — e a auto-gestão (trailing/time stop).

        O trailing é **sem look-ahead**: o SL usa o pico dos ticks ANTERIORES
        (o pico só é atualizado no fim, depois dos fills) — um preço nunca
        beneficia do pico que ele próprio criou."""
        events: List[dict] = []
        price = self.market.price(trade["symbol"])
        if price is None:
            return events

        positions = {p.leg: p for p in self.db.paper_positions_for_trade(trade["id"])}
        open_legs = [p for p in positions.values() if p.status == "open"]
        if not open_legs:
            return events

        direction = trade["direction"]
        entry = trade["entry"]
        sl0 = trade["stop_loss"]
        tp1 = trade.get("take_profit_1") or 0.0
        exit_at_tp = strat["exit_at_tp"]
        stair_levels = strat.get("stair_levels")
        stair_from = int(strat.get("stair_from") or 1)
        confirm_filter = bool(strat.get("confirm_filter") or False)
        trail_factor = strat.get("trail_factor")
        time_stop = strat.get("time_stop_minutes")
        # modo defensivo (estudo 19/08): PF das últimas `def_window` trades
        # resolvidas da conta virtual < 1.0 → trail apertado + time stop até
        # recuperar (sem cortar pernas). A história é a da PRÓPRIA conta
        # paper (o que a estratégia realmente vê na corrida).
        def_window = strat.get("def_window")
        if def_window and self._defensive_triggered(trade["strategy_id"], def_window):
            if strat.get("def_trail") is not None:
                trail_factor = strat["def_trail"]
            if strat.get("def_time_stop") is not None:
                time_stop = strat["def_time_stop"]
        atr_k = strat.get("atr_k")
        atr_period = int(strat.get("atr_period") or 14)
        tp2 = trade.get("take_profit_2") or 0.0
        policy = AutoProtectionPolicy(
            direction=direction, entry=entry, tp1=tp1, tp2=tp2,
            trail_factor=trail_factor, stair_levels=stair_levels,
            stair_from=stair_from,
        )

        state = self._auto_state(trade, direction, price, positions)

        def _hit(level: float, kind: str) -> bool:
            if direction == "buy":
                return price <= level if kind == "sl" else price >= level
            return price >= level if kind == "sl" else price <= level

        def _close_all_at(px: float, reason: str) -> None:
            for p in open_legs:
                if p.status == "open":
                    events.append(self._close(trade, p, px, reason))

        leg1 = positions.get(1)

        # 1) stop partilhado primeiro (conservador: o SL ganha ao TP na mesma tick)
        if leg1 and leg1.status == "open" and _hit(sl0, "sl"):
            _close_all_at(sl0, "sl")
            self._auto.pop(trade["id"], None)
            events += self._finalize(trade, positions)
            return events

        # 2) TP1: perna 1 fecha; restantes vão a BE (ou ao NÍVEL do TP1 com
        #    stair_from=1 — escada: lucro do TP1 garantido em todas)
        if leg1 and leg1.status == "open" and tp1 and _hit(tp1, "tp"):
            events.append(self._close(trade, leg1, tp1, "tp1"))
            floor = policy.initial_floor()
            for p in open_legs:
                if p.leg != 1:
                    self.db.move_paper_position_sl(p.id, floor)
                    p.stop_loss = floor
                    self._record_auto_sl(state, p.leg, floor)
            state["tp1_ts"] = time.time()
            if direction == "buy":
                state["peak"] = price
            else:
                state["trough"] = price

        # 3) fechar tudo no TP configurado (exit_at_tp)
        if exit_at_tp is not None:
            tp_level = trade.get(f"take_profit_{exit_at_tp}") or 0.0
            if tp_level and _hit(tp_level, "tp"):
                _close_all_at(tp_level, f"tp{exit_at_tp}")
                events += self._finalize(trade, positions)
                return events

        # 4) time stop (auto): o mercado estagnou N min após o TP1
        if time_stop_due(state.get("tp1_ts"), time.time(), time_stop):
            for p in open_legs:
                if p.status == "open" and p.leg != 1:
                    events.append(self._close(trade, p, price, "time_stop"))
            events += self._finalize(trade, positions)
            return events

        # 5) trailing — sem look-ahead (o SL usa o pico de ticks ANTERIORES;
        #    o pico só é atualizado no passo 7, depois dos fills)
        if trail_factor and state.get("tp1_ts") is not None:
            trail_dist = policy.trail_distance()
            # trailing adaptativo por ATR (espelho do backtest/MT5, ESTRATEGIAS.md Parte X):
            # trail_dist = max(fixo, atr_k × ATR) — ATR dos MINUTOS COMPLETOS
            # anteriores ao atual (sem look-ahead: o tick atual nunca conta
            # para o ATR que ele próprio usa); sem dados → trail fixo
            if atr_k and trail_dist:
                _atr = self._paper_atr(state, price, atr_period)
                trail_dist = policy.trail_distance(atr=_atr, atr_k=atr_k)
            if trail_dist:
                for p in open_legs:
                    if p.leg == 1 or p.status != "open":
                        continue
                    extreme = (state.get("peak") if direction == "buy"
                               else state.get("trough"))
                    new_sl = policy.trailing_target(
                        p.stop_loss, extreme, distance=trail_dist,
                    )
                    if new_sl is not None:
                        self.db.move_paper_position_sl(p.id, new_sl)
                        p.stop_loss = new_sl
                        self._record_auto_sl(state, p.leg, new_sl)

        # 6) pernas restantes: SL (talvez BE/escada/trailing) ou TP próprio
        for p in open_legs:
            if p.leg == 1 or p.status != "open":
                continue
            if _hit(p.stop_loss, "sl"):
                lvl = p.stop_loss
                reason = sl_exit_reason(direction, lvl, entry)
                events.append(self._close(trade, p, lvl, reason))
                # filtro de confirmação: a perna i morreu → pernas > i fecham
                if confirm_filter:
                    for j in confirmation_legs(
                        p.leg, [candidate.leg for candidate in open_legs
                                if candidate.status == "open"],
                    ):
                        candidate = positions[j]
                        if candidate.status == "open":
                            events.append(self._close(trade, candidate, price, "confirm"))
            elif _hit(p.take_profit, "tp"):
                events.append(self._close(trade, p, p.take_profit, f"tp{p.leg}"))
                # escada: o TP conquistado vira o novo chão das pernas seguintes
                if stair_levels and p.leg <= stair_levels:
                    for j in open_legs:
                        target = policy.stair_target(j.stop_loss, p.take_profit, j.leg)
                        if j.leg > p.leg and j.status == "open" and target is not None:
                            self.db.move_paper_position_sl(j.id, target)
                            j.stop_loss = target
                            self._record_auto_sl(state, j.leg, target)

        # 7) atualizar o pico/vale para os ticks seguintes (depois dos fills)
        if trail_factor and state.get("tp1_ts") is not None:
            if direction == "buy":
                state["peak"] = max(state.get("peak") or price, price)
            else:
                state["trough"] = min(state.get("trough") or price, price)

        events += self._finalize(trade, positions)
        return events

    def _defensive_triggered(self, strategy_id: str, window: int) -> bool:
        """True quando o PF das últimas `window` trades de papel RESOLVIDAS
        da estratégia < 1.0 (win = pnl > 0, loss = pnl < 0, BE = fora da
        conta). Sem perdas → nunca dispara. Recalculado a cada tick — a
        estratégia volta ao normal quando o PF recupera."""
        rows = self.db.paper_closed_recent(strategy_id, window)
        if len(rows) < int(window):
            return False
        wins = [r for r in rows if r > 0]
        losses = [r for r in rows if r < 0]
        if not losses:
            return False
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return gross_loss > 0 and gross_win / gross_loss < 1.0

    def _auto_state(self, trade: dict, direction: str, price: float,
                    positions: dict) -> dict:
        """Estado do trailing por trade (pico/vale desde o TP1). Em memória;
        após um restart, se o TP1 já tiver acontecido, recomeça do preço
        atual (aproximado — o pico histórico perde-se, documentado)."""
        tid = trade["id"]
        if tid not in self._auto:
            st: dict = {
                "peak": None, "trough": None, "tp1_ts": None,
                "auto_sl_by_leg": {}, "auto_protection_at_by_leg": {},
            }
            leg1 = positions.get(1)
            if leg1 and leg1.status == "closed" and leg1.exit_reason == "tp1":
                if direction == "buy":
                    st["peak"] = price
                else:
                    st["trough"] = price
                # Após um restart, não há estado em memória. Repor o início
                # do trailing/time-stop a partir do fecho persistido de TP1;
                # sem isto a auto ficava com escada/SL estático para sempre.
                try:
                    from datetime import datetime, timezone
                    stamp = datetime.fromisoformat(
                        str(leg1.closed_at).replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    st["tp1_ts"] = stamp.timestamp()
                except (TypeError, ValueError, AttributeError):
                    st["tp1_ts"] = time.time()
                applied_at = leg1.closed_at or _now_iso()
                for pos in positions.values():
                    if pos.leg == 1 or pos.status != "open":
                        continue
                    if abs(float(pos.stop_loss) - float(trade["stop_loss"])) <= 1e-12:
                        continue
                    st.setdefault("auto_sl_by_leg", {}).setdefault(
                        pos.leg, pos.stop_loss,
                    )
                    st.setdefault("auto_protection_at_by_leg").setdefault(
                        pos.leg, applied_at,
                    )
            self._auto[tid] = st
        return self._auto[tid]

    @staticmethod
    def _record_auto_sl(state: dict, leg: int, new_sl: float) -> None:
        """Remember the latest mechanical SL and its first trigger time."""
        state.setdefault("auto_sl_by_leg", {})[leg] = new_sl
        state.setdefault("auto_protection_at_by_leg", {}).setdefault(leg, _now_iso())

    def _record_protect_latency(self, trade: dict, decision, *, leg: int,
                                auto_sl: Optional[float], group_sl: float,
                                msg_id: Optional[str], group_received_at: str,
                                applied_sl: Optional[float] = None) -> None:
        auto_state = self._auto.get(trade["id"], {})
        auto_at = None if decision.status == "auto_not_triggered" else auto_state.get(
            "auto_protection_at_by_leg", {}).get(leg)
        self.db.insert_protect_latency({
            "engine": "paper", "strategy_id": trade["strategy_id"],
            "signal_id": trade.get("signal_id"), "trade_id": trade["id"],
            "message_id": msg_id, "symbol": trade.get("symbol"),
            "direction": trade.get("direction"),
            "auto_protection_at": auto_at,
            "group_received_at": group_received_at,
            "auto_sl": auto_sl, "group_sl": group_sl,
            "applied_sl": decision.applied_sl if applied_sl is None else applied_sl,
            "winner": decision.winner,
            "delta_pips": decision.delta_pips,
            "delta_seconds": decision.delta_seconds,
            "leg1_action": decision.leg1_action,
            "status": decision.status, "created_at": group_received_at,
        })

    def _paper_atr(self, state: dict, price: float, period: int) -> Optional[float]:
        """ATR (média do True Range) da trade de papel a partir dos ticks,
        por baldes de minuto: `atr_buckets[minuto] = [high, low, last]` para
        os minutos COMPLETOS. O minuto atual está em progresso e NUNCA entra
        no ATR usado nesse mesmo tick (sem look-ahead, como o backtest/MT5).
        None com menos de 2 minutos completos → trail fixo."""
        import time as _t
        now_min = int(_t.time() // 60)
        buckets = state.setdefault("atr_buckets", {})
        cur = buckets.setdefault(now_min, [price, price, price])
        cur[0] = max(cur[0], price)
        cur[1] = min(cur[1], price)
        cur[2] = price
        # só minutos ANTERIORES ao atual (completos)
        mins = sorted(m for m in buckets if m < now_min)
        # poda: não deixa a memória crescer para sempre numa trade longa
        for m in list(buckets):
            if m < now_min - period - 2:
                buckets.pop(m, None)
        if len(mins) < 2:
            return None
        trs = []
        for k in range(1, len(mins)):
            prev = buckets[mins[k - 1]]
            b = buckets[mins[k]]
            trs.append(max(b[0] - b[1], abs(b[0] - prev[2]), abs(b[1] - prev[2])))
        win = trs[-period:] if period and period > 0 else trs
        if not win:
            return None
        return sum(win) / len(win)

    # ------------------------------------------------------- manage (group)

    def apply_management(self, signal_id: str, kind: str, *,
                         tp: Optional[int] = None, sl: Optional[float] = None,
                         be: bool = False, msg_id: Optional[str] = None) -> List[dict]:
        """Aplica uma instrução de gestão do grupo às trades de papel abertas
        de `signal_id` — mas só para as estratégias cujo `manage_kinds`
        inclui o kind (registo central). É o que faz a corrida paralela
        reflectir a gestão real em vez de só SL/TP/BE."""
        from app.strategies import get_strategy
        events: List[dict] = []
        for trade in self.db.paper_open_trades_by_signal(signal_id):
            sid = trade["strategy_id"]
            strat = get_strategy(sid)
            if kind not in (strat.get("manage_kinds") or ()):
                continue
            events += self._apply_one(trade, kind, tp=tp, sl=sl, be=be, msg_id=msg_id)
        return events

    def _apply_one(self, trade: dict, kind: str, *, tp: Optional[int] = None,
                   sl: Optional[float] = None, be: bool = False,
                   msg_id: Optional[str] = None) -> List[dict]:
        events: List[dict] = []
        price = self.market.price(trade["symbol"])
        if price is None:
            return events
        positions = {p.leg: p for p in self.db.paper_positions_for_trade(trade["id"])}
        open_legs = [p for p in positions.values() if p.status == "open"]
        if not open_legs:
            return events

        def _hit(level: float, k: str) -> bool:
            if trade["direction"] == "buy":
                return price <= level if k == "sl" else price >= level
            return price >= level if k == "sl" else price <= level

        if kind == "tp_hit":
            if int(tp or 1) == 1:
                for p in open_legs:
                    if p.leg != 1:
                        self.db.move_paper_position_sl(p.id, trade["entry"])
                        p.stop_loss = trade["entry"]
        elif kind == "sl_hit":
            pass  # registado — o SL do broker já protege
        elif kind == "close_tp1":
            leg1 = positions.get(1)
            if leg1 and leg1.status == "open":
                events.append(self._close(trade, leg1, price, "close_tp1", msg_id))
        elif kind == "close_tp":
            # "Close TP2..TP6 here" — fecha só a perna N ao mercado.
            leg = positions.get(int(tp or 2))
            if leg and leg.status == "open":
                events.append(self._close(trade, leg, price, "close_tp", msg_id))
        elif kind == "protect":
            if trade["strategy_id"] == "auto_escada_3_hibrida":
                state = self._auto_state(
                    trade, trade["direction"], price, positions,
                )
                group_received_at = _now_iso()
                tp1 = trade.get("take_profit_1") or trade["entry"]
                group_sl = group_protect_sl(
                    direction=trade["direction"], entry=trade["entry"],
                    stop_loss=trade["stop_loss"], tp1=tp1,
                )
                auto_by_leg = state.get("auto_sl_by_leg", {})
                remaining = [p for p in open_legs if p.leg != 1]
                representative = None
                if remaining:
                    vals = [auto_by_leg.get(p.leg) for p in remaining
                            if auto_by_leg.get(p.leg) is not None]
                    if vals:
                        representative = (max(vals) if trade["direction"] == "buy"
                                          else min(vals))
                leg1 = positions.get(1)
                leg1_decision = decide_protect(
                    direction=trade["direction"], auto_sl=representative,
                    group_sl=group_sl, entry=trade["entry"], tp1=tp1,
                    price_at_event=price,
                    leg1_open=bool(leg1 and leg1.status == "open"),
                    auto_protection_at=(
                        state.get("auto_protection_at_by_leg", {}).get(
                            remaining[0].leg) if remaining else None
                    ),
                    group_received_at=group_received_at,
                    pip_size=_pip_size(trade["symbol"]),
                )
                leg1_eligible = hybrid_protect_eligible(
                    representative,
                    (state.get("auto_protection_at_by_leg", {}).get(
                        remaining[0].leg) if remaining else None),
                )
                if leg1 and leg1.status == "open" \
                        and leg1_eligible and leg1_decision.leg1_action == "closed":
                    events.append(self._close(trade, leg1, price, "protect", msg_id))
                for p in remaining:
                    auto_sl = auto_by_leg.get(p.leg)
                    auto_at = state.get("auto_protection_at_by_leg", {}).get(p.leg)
                    decision = decide_protect(
                        direction=trade["direction"], auto_sl=auto_sl,
                        group_sl=group_sl, entry=trade["entry"], tp1=tp1,
                        price_at_event=price,
                        leg1_open=bool(leg1 and leg1.status == "open"),
                        auto_protection_at=auto_at,
                        group_received_at=group_received_at,
                        pip_size=_pip_size(trade["symbol"]),
                    )
                    eligible = hybrid_protect_eligible(auto_sl, auto_at)
                    applied_sl = decision.applied_sl if eligible else p.stop_loss
                    if applied_sl is not None:
                        applied_sl = (
                            max(p.stop_loss, applied_sl)
                            if trade["direction"] == "buy"
                            else min(p.stop_loss, applied_sl)
                        )
                        if eligible and applied_sl != p.stop_loss:
                            self.db.move_paper_position_sl(p.id, applied_sl)
                            p.stop_loss = applied_sl
                    self._record_protect_latency(
                        trade, decision, leg=p.leg, auto_sl=auto_sl,
                        group_sl=group_sl, msg_id=msg_id,
                        group_received_at=group_received_at,
                        applied_sl=applied_sl,
                    )
            else:
                leg1 = positions.get(1)
                if leg1 and leg1.status == "open":
                    events.append(self._close(trade, leg1, price, "protect", msg_id))
                risk = abs(trade["entry"] - trade["stop_loss"])
                tp1 = trade.get("take_profit_1") or trade["entry"]
                buffer = (min(max(risk * 0.1, risk * 0.01), abs(tp1 - trade["entry"]))
                          if risk > 0 else 0.0)
                for p in open_legs:
                    if p.leg != 1:
                        new_sl = (trade["entry"] + buffer) if trade["direction"] == "buy" \
                            else (trade["entry"] - buffer)
                        self.db.move_paper_position_sl(p.id, new_sl)
                        p.stop_loss = new_sl
        elif kind == "move_sl":
            if sl:
                for p in open_legs:
                    self.db.move_paper_position_sl(p.id, sl)
                    p.stop_loss = sl
        elif kind == "close_all":
            if be:
                # "out at BE": pernas restantes protegidas na entrada (o tick
                # fecha-as quando o preço voltar a BE) — igual à produção.
                for p in open_legs:
                    if p.leg != 1:
                        self.db.move_paper_position_sl(p.id, trade["entry"])
                        p.stop_loss = trade["entry"]
            else:
                for p in open_legs:
                    events.append(self._close(trade, p, price, "close_all", msg_id))

        events += self._finalize(trade, positions)
        return events

    # ------------------------------------------------------------- helpers

    def _close(self, trade: dict, pos: Position, px: float,
               reason: Optional[str] = None, msg_id: Optional[str] = None) -> dict:
        pnl = _pnl(trade["direction"], pos.entry, px, pos.lots, trade["symbol"])
        closed_at = _now_iso()
        self._close_seq += 1
        pos.status = "closed"
        pos.closed_at = closed_at
        pos.close_price = px
        pos.pnl = pnl
        pos.exit_reason = reason
        pos.exit_msg_id = msg_id
        # O atributo é apenas um desempate em memória; `closed_at` continua a
        # ser a fonte persistida para ordenar fechamentos após um restart.
        pos._paper_close_seq = self._close_seq
        self.db.close_paper_position(pos.id, px, pnl, closed_at,
                                     reason=reason, msg_id=msg_id)
        return {"type": "close", "trade_id": trade["id"], "symbol": trade["symbol"],
                "leg": pos.leg, "direction": trade["direction"], "price": px,
                "pnl": pnl, "reason": reason, "strategy_id": trade["strategy_id"]}

    def _finalize(self, trade: dict, positions: dict) -> List[dict]:
        """Fecha a trade de papel quando não restam pernas abertas e atualiza
        o saldo virtual (o P&L realizado entra na conta). Devolve os eventos
        gerados (`trade_closed` + eventuais `divergence` vs a referência)."""
        remaining = [p for p in positions.values() if p.status == "open"]
        if remaining or trade["status"] != "open":
            return []
        pnl = sum(p.pnl for p in positions.values())
        # A razão da trade é a da perna que fechou por último. Não podemos
        # assumir que é a perna de maior número: a perna 3 pode ter fechado
        # cedo e a perna 2 pode ser a que encerrou a trade por trailing,
        # protect ou close_all.
        closed_with_reason = [p for p in positions.values() if p.exit_reason]
        reason = max(
            closed_with_reason,
            key=lambda p: (
                p.closed_at or "",
                getattr(p, "_paper_close_seq", -1),
            ),
        ).exit_reason if closed_with_reason else "mixed"
        self.db.close_paper_trade(trade["id"], _now_iso(), pnl, reason=reason)
        sid = trade["strategy_id"]
        bal = self.db.paper_balance(sid) or 0.0
        self.db.update_paper_balance(sid, bal + pnl)
        self._auto.pop(trade["id"], None)
        trade["status"] = "closed"
        trade["pnl"] = pnl
        trade["exit_reason"] = reason
        out = [{"type": "trade_closed", "trade_id": trade["id"],
                "symbol": trade["symbol"], "pnl": pnl, "reason": reason,
                "strategy_id": sid}]
        out += self._divergences(trade)
        return out

    def _divergences(self, trade: dict) -> List[dict]:
        """Liga o backtest ao vivo: compara cada auto com a `nova_gestao`
        no MESMO sinal assim que há informação suficiente.

        A comparação consulta trades fechadas e abertas, por isso também
        apanha a ordem inversa (auto fecha primeiro, referência depois). O
        evento continua deduplicado por ``signal:auto_trade``; a tabela do
        Relatório permanece a fonte final dos dois P&L.
        """
        out: List[dict] = []
        signal_id = trade["signal_id"]
        sid = trade["strategy_id"]

        def _event(auto: dict, ref: dict, *, auto_open: bool) -> Optional[dict]:
            key = f"{signal_id}:{auto['id']}"
            if key in self._diverged:
                return None
            # A referência pode fechar primeiro: alerta imediato, mas não o
            # consideramos final enquanto o P&L da auto ainda é desconhecido.
            if auto_open:
                if key in self._divergence_pending:
                    return None
                self._divergence_pending.add(key)
            else:
                self._divergence_pending.discard(key)
            auto_reason = "open" if auto_open else auto.get("exit_reason")
            auto_pnl = None if auto_open else auto.get("pnl")
            ref_reason = ref.get("exit_reason")
            ref_pnl = ref.get("pnl")
            same = (
                not auto_open
                and ref_reason == auto_reason
                and ref_reason is not None
                and abs((ref_pnl or 0.0) - (auto_pnl or 0.0)) < 0.01
            )
            if same:
                return None
            if not auto_open:
                self._diverged.add(key)
            return {
                "type": "divergence", "signal_id": signal_id,
                "symbol": trade["symbol"], "strategy_id": auto["strategy_id"],
                "reason": auto_reason,
                "pnl": round(auto_pnl, 2) if auto_pnl is not None else None,
                "ref_reason": ref_reason,
                "ref_pnl": round(ref_pnl or 0.0, 2),
                "pending": auto_open,
            }

        if sid.startswith("auto_"):
            # A auto fechou primeiro: aguarda a referência fechar para poder
            # emitir a divergência com os dois P&L.
            ref = self.db.paper_trade_for_signal(signal_id, "nova_gestao")
            if ref:
                event = _event(trade, ref, auto_open=False)
                if event:
                    out.append(event)
            return out

        if sid == "nova_gestao":
            # A referência fechou. Consulta também autos já fechadas (não só
            # as abertas), cobrindo a ordem inversa que o código antigo perdia.
            for auto in self.db.paper_trades_for_signal(signal_id):
                if not auto["strategy_id"].startswith("auto_"):
                    continue
                event = _event(
                    auto, trade,
                    auto_open=auto.get("status") == "open",
                )
                if event:
                    out.append(event)
        return out
