"""Backtest — replay sinais reais sobre candles históricos.

Simula a estratégia TP1..TP5/BE (N pernas) para cada sinal, usando o caminho
de preços real (candles OHLC da Yahoo) a partir do momento do sinal + atraso.

Regras (iguais às do modo live):
  * uma perna por TP disponível (TP1..TP5); perna N fecha no seu TP;
  * quando a perna 1 fecha no TP1, o SL das pernas restantes passa a
    breakeven (entrada);
  * se o SL for atingido primeiro, todas fecham no SL (perda).

Custos reais (configuráveis):
  * spread   — entrada e saída ficam piores (metade do spread em cada lado);
  * slippage — aplicado na entrada;
  * comissão — por lote, por lado (cada perna abre e fecha = 2 lados).

Convenção conservadora: se TP e SL ocorrerem no mesmo candle, assume-se que o
SL foi atingido primeiro. O tamanho das pernas vem do slider de risco (risco
dividido pelo número de pernas, igual ao live).
"""

from datetime import datetime, timezone
from typing import List, Optional, Set

from app.execution.protection import (
    AutoProtectionPolicy,
    confirmation_legs,
    decide_protect,
    group_protect_sl,
    hybrid_protect_eligible,
    sl_outcome,
    time_stop_due,
)
from app.models import Signal
from app.risk.engine import compute_lots

CONTRACT = 100_000


def _pip_size(symbol: str) -> float:
    """Pip size por símbolo (metais usam 0.01, pares forex 0.0001)."""
    return 0.01 if symbol in ("XAUUSD", "XAGUSD") else 0.0001


def _contract(symbol: str) -> float:
    """Units per lot, per symbol — same as the live MT5 engine
    (XAUUSD contract 100, XAGUSD 5000, forex 100000)."""
    if symbol == "XAUUSD":
        return 100.0
    if symbol == "XAGUSD":
        return 5000.0
    return float(CONTRACT)


def _pnl(direction: str, entry: float, close: float, lots: float,
         contract: float = CONTRACT) -> float:
    sign = 1.0 if direction == "buy" else -1.0
    return round(sign * (close - entry) * lots * contract, 2)


def _to_epoch(iso: str) -> int:
    """Local ISO timestamp -> UTC epoch seconds (safe to compare with Yahoo)."""
    return int(datetime.fromisoformat(iso).timestamp())


def _start_index(candles: List[dict], epoch: int) -> int:
    """Index of the first candle at/after `epoch` (binary search)."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["ts"] < epoch:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _exit_fill(direction: str, level: float, spread: float) -> float:
    """Fill price ao fechar: pior metade do spread (bid/ask)."""
    return level - spread / 2 if direction == "buy" else level + spread / 2


def _roll_pf(pnls: List[float]) -> Optional[float]:
    """Profit factor das trades resolvidas (pnl > 0 = win, < 0 = loss;
    pnl == 0 = BE, fora da conta). None sem trades ou sem perdas nem ganhos.
    Sem perdas → +inf (o modo defensivo nunca dispara)."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    if not losses:
        return float("inf") if wins else None
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return gross_win / gross_loss if gross_loss else float("inf")


def _candle_atr(candles: List[dict], i: int, period: int) -> Optional[float]:
    """ATR (média do True Range) das `period` velas ANTES do índice `i` — sem
    look-ahead (a vela atual nunca contribui para o ATR que ela própria usa).
    Cada vela da janela usa como referência o close da vela ANTERIOR (a que
    está imediatamente antes da janela para a primeira). None sem dados
    suficientes (o trail fixo é o fallback)."""
    lo_idx = max(0, i - period)
    if i - lo_idx < 2:
        return None
    trs = []
    for k in range(lo_idx, i):
        hi, lo = candles[k]["high"], candles[k]["low"]
        prev_close = candles[k - 1]["close"] if k > 0 else None
        if prev_close is None:
            continue
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    if not trs:
        return None
    return sum(trs) / len(trs)


def simulate_signal(signal: Signal, candles: List[dict], risk_pct: float,
                    balance: float, delay_seconds: int = 60,
                    spread_pips: float = 1.0, slippage_pips: float = 0.5,
                    commission_per_lot: float = 3.5,
                    max_legs: Optional[int] = None,
                    exit_at_tp: Optional[int] = None,
                    lots_override: Optional[float] = None,
                    trail_factor: Optional[float] = None,
                    time_stop_minutes: Optional[int] = None,
                    stair_levels: Optional[int] = None,
                    stair_from: int = 1,
                    confirm_filter: bool = False,
                    atr_k: Optional[float] = None,
                    atr_period: int = 14,
                    def_window: Optional[int] = None,
                    def_trail: Optional[float] = None,
                    def_time_stop: Optional[int] = None,
                    def_state: Optional[dict] = None,
                    trace: bool = False,
                    mgmt_events: Optional[List[tuple]] = None,
                    strategy_id: str = "") -> dict:
    """Replay one signal on the candle path, including trading costs.

    The trade is simulated as if it was made `delay_seconds` after the signal
    (the bot waits for edits), so the first `delay_seconds` of price action
    are ignored. The entry is filled at the signal's entry, worse by the
    spread and slippage; SL/TP/BE are then evaluated on the real candles.

    `max_legs` caps how many TPs are used (e.g. 2 to replay the old
    TP1/TP2-only strategy on the same signals) — the risk is split among
    the legs actually opened.

    `exit_at_tp` (2..5) = "fechar tudo": when that TP is hit, EVERY still
    open leg closes at that price (full exit) — no leg runs past it. Leg 1
    still closes at TP1 first and the remaining legs move to breakeven.

    `trail_factor` (múltiplo adimensional da distância entrada→TP2, gestão
    adaptativa) = "o bot lê o mercado e decide": depois do TP1, o SL das
    pernas restantes deixa de ser BE estático e **segue o preço** —
    SL = pico - trail_dist, onde trail_dist = trail_factor × dist(entrada,
    TP2) (adapta-se à estrutura de cada sinal; 2.0 ≈ 8 pips em gold, o valor
    honesto do sweep). Se o preço recuar trail_dist do pico, as pernas
    fecham com lucro em vez de devolver ao BE. `time_stop_minutes`: se após o TP1 as pernas restantes
    continuarem abertas passado N minutos (o mercado estagnou), fecham ao
    mercado — replica o "we're out" do grupo sem esperar pela mensagem.

    `stair_levels` (escada de proteção) = quando o TP_k bate (k <= N), o SL
    das pernas seguintes sobe para o nível do TP_k — cada TP conquistado vira
    o novo chão ("dar TP3 sem perder nada"). `stair_from` decide o degrau do
    TP1: `1` = o SL das pernas 2+ sobe para o NÍVEL do TP1 (lucro garantido
    em todas); `2` = o TP1 só sobe para a entrada (BE, comportamento atual) e
    a escada de níveis começa no TP2. `confirm_filter` (filtro de
    confirmação) = quando uma perna k leva SL (o movimento morreu), as pernas
    > k fecham também ao mercado — não esperam pelo TP seguinte (replica o
    "we're out" do grupo: "se a 3 der profit abre a 4, se a 3 morrer sai
    tudo").

    `atr_k`/`atr_period` (pós-validação, ESTRATEGIAS.md Parte X — espelho do MT5):
    trailing adaptativo por ATR — trail_dist = max(trail_factor × dist,
    atr_k × ATR), com o ATR das velas ANTERIORES à atual (sem look-ahead);
    sem atr_k ou sem velas suficientes degrada para o trail fixo.

    `lots_override` sizes every leg at a fixed volume instead of risk-based
    (parâmetro de simulação — nenhuma estratégia do registo o usa por agora).
    """
    # modo defensivo (estudo 19/08): quando o PF das últimas `def_window`
    # trades resolvidas < 1.0, a estratégia passa a trail apertado +
    # time stop SEM cortar pernas — volta ao normal quando o PF recupera.
    # `def_state` é o histórico PARTILHADO entre chamadas (o caller corre os
    # sinais por ordem cronológica); sem `def_window` ou sem histórico
    # suficiente degrada para os parâmetros normais. Sem look-ahead: a
    # decisão usa apenas trades anteriores a este sinal.
    trail_factor_eff = trail_factor
    time_stop_eff = time_stop_minutes
    if def_window and def_state is not None and def_state.get("resolved"):
        hist = [p for _, p in def_state["resolved"][-int(def_window):]]
        if len(hist) >= int(def_window):
            pf = _roll_pf(hist)
            if pf is not None and pf < 1.0:
                if def_trail is not None:
                    trail_factor_eff = def_trail
                if def_time_stop is not None:
                    time_stop_eff = def_time_stop

    start = _start_index(candles, _to_epoch(signal.received_at) + delay_seconds)
    if start >= len(candles):
        if def_state is not None:
            def_state.setdefault("resolved", [])
        return {"outcome": "no_data", "pnl": 0.0, "leg1_pnl": 0.0, "leg2_pnl": 0.0}

    entry, sl = signal.entry, signal.stop_loss
    direction = signal.direction

    tps = {}
    for i in range(1, 6):
        tp = float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0)
        if tp > 0:
            tps[i] = tp
    legs = sorted(tps)
    if max_legs is not None and max_legs >= 1:
        legs = legs[:int(max_legs)]
    if not legs:
        return {"outcome": "no_data", "pnl": 0.0, "leg1_pnl": 0.0, "leg2_pnl": 0.0}

    pip = _pip_size(signal.symbol)
    contract = _contract(signal.symbol)
    spread = spread_pips * pip
    slippage = slippage_pips * pip
    entry_fill = (entry + spread / 2 + slippage) if direction == "buy" \
        else (entry - spread / 2 - slippage)

    # risk split evenly across the legs, exactly like the live engines — with
    # the symbol's real pip/contract so gold sizes correctly (pip 0.01,
    # contract 100) instead of falling back to forex defaults
    if lots_override and lots_override > 0:
        lots = {i: float(lots_override) for i in legs}
    else:
        lots = {
            i: compute_lots(balance, risk_pct / len(legs), entry, sl,
                            pip_size=pip, contract_size=contract)
            for i in legs
        }
    commission = commission_per_lot * sum(lots.values()) * 2  # cada perna abre+fecha

    leg_open = {i: True for i in legs}
    leg_pnl = {i: 0.0 for i in legs}
    leg_sl = {i: sl for i in legs}
    outcome = "open"
    # gestão adaptativa (trailing): pico/vale do preço desde o TP1 e o SL de
    # cada perna que o segue — o bot "lê o mercado" e sobe o SL atrás do preço
    peak: Optional[float] = None          # para compras: high mais alto desde TP1
    trough: Optional[float] = None        # para vendas: low mais baixo desde TP1
    tp1_ts: Optional[int] = None          # timestamp do candle em que o TP1 fechou
    # trail fixo (base) — com `atr_k` o recuo adapta-se à volatilidade:
    # trail_dist = max(base, atr_k × ATR), o ATR das velas ANTERIORES à atual
    # (sem look-ahead) — mesma fórmula do MT5Engine (Fase 3, ESTRATEGIAS.md Parte X)
    base_trail_dist: float = 0.0
    # usa o trail EFETIVO (o modo defensivo pode ter trocado 2.0 → 1.5)
    if trail_factor_eff and 2 in tps:
        base_trail_dist = trail_factor_eff * abs(tps[2] - entry)
    elif trail_factor_eff and 1 in tps:
        base_trail_dist = trail_factor_eff * abs(tps[1] - entry)
    policy = AutoProtectionPolicy(
        direction=direction, entry=entry, tp1=tps.get(1), tp2=tps.get(2),
        trail_factor=trail_factor_eff, stair_levels=stair_levels,
        stair_from=stair_from,
    )
    trail_dist: float = policy.trail_distance()
    # trace (para a página de teste mostrar o fluxo real de uma trade):
    # eventos cronológicos + caminho de preços — só preenchido com trace=True
    events: list = []
    path: list = []
    # Hybrid strategy state.  Management events are applied in this same
    # chronological replay, never by post-processing a completed simulation.
    hybrid_events = sorted(
        [e for e in (mgmt_events or []) if len(e) > 1 and e[1] == "protect"],
        key=lambda e: e[0],
    ) if strategy_id == "auto_escada_3_hibrida" else []
    hybrid_event_idx = 0
    auto_protection_at: Optional[int] = None
    protect_latency: list = []
    mgmt_applied: list = []
    interval = max(60, candles[1]["ts"] - candles[0]["ts"]) if len(candles) > 1 else 900
    prev_close = candles[start - 1]["close"] if start > 0 else candles[start]["open"]

    def _iso_epoch(ts: Optional[int]) -> Optional[str]:
        if ts is None:
            return None
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

    def _ev(kind: str, price: float, detail: str = "") -> None:
        if trace:
            events.append({"ts": c["ts"], "kind": kind, "price": round(price, 4), "detail": detail})

    def _close_hybrid_leg(leg: int, price: float) -> None:
        if not leg_open.get(leg):
            return
        leg_pnl[leg] = _pnl(direction, entry_fill,
                            _exit_fill(direction, price, spread),
                            lots[leg], contract)
        leg_open[leg] = False

    def _apply_hybrid_protect(known_price: float) -> None:
        """Apply protect events using only the close before the event candle."""
        nonlocal hybrid_event_idx, auto_protection_at
        if not hybrid_events:
            return
        while hybrid_event_idx < len(hybrid_events):
            event = hybrid_events[hybrid_event_idx]
            event_ts = int(event[0])
            if event_ts >= c["ts"] + interval:
                break
            hybrid_event_idx += 1
            group_at = _iso_epoch(event_ts) or _iso_epoch(c["ts"]) or ""
            group_sl = group_protect_sl(
                direction=direction, entry=entry, stop_loss=sl,
                tp1=tps.get(1, entry),
            )
            remaining = [leg for leg in legs if leg != 1 and leg_open.get(leg)]
            representative = None
            if remaining:
                representative = (
                    max(leg_sl[leg] for leg in remaining)
                    if direction == "buy" else min(leg_sl[leg] for leg in remaining)
                )
            leg1_decision = decide_protect(
                direction=direction, auto_sl=representative,
                group_sl=group_sl, entry=entry, tp1=tps.get(1, entry),
                price_at_event=known_price,
                leg1_open=bool(leg_open.get(1)),
                auto_protection_at=_iso_epoch(auto_protection_at),
                group_received_at=group_at, pip_size=pip,
            )
            action = leg1_decision.leg1_action
            leg1_eligible = hybrid_protect_eligible(
                representative, _iso_epoch(auto_protection_at),
            )
            if action == "closed" and leg1_eligible:
                _close_hybrid_leg(1, known_price)
                mgmt_applied.append("protect")
            elif action == "skipped" and remaining:
                mgmt_applied.append("protect")
            elif not remaining and action == "already_closed":
                mgmt_applied.append("protect")

            for leg in remaining:
                old_sl = leg_sl[leg]
                decision = decide_protect(
                    direction=direction, auto_sl=old_sl,
                    group_sl=group_sl, entry=entry, tp1=tps.get(1, entry),
                    price_at_event=known_price,
                    leg1_open=bool(leg_open.get(1)),
                    auto_protection_at=_iso_epoch(auto_protection_at),
                    group_received_at=group_at, pip_size=pip,
                )
                eligible = hybrid_protect_eligible(
                    old_sl, _iso_epoch(auto_protection_at),
                )
                if eligible and decision.applied_sl is not None:
                    if direction == "buy":
                        leg_sl[leg] = max(leg_sl[leg], decision.applied_sl)
                    else:
                        leg_sl[leg] = min(leg_sl[leg], decision.applied_sl)
                protect_latency.append({
                    "engine": "backtest", "strategy_id": strategy_id,
                    "signal_id": signal.id, "trade_id": None,
                    "message_id": event[4] if len(event) > 4 else None,
                    "symbol": signal.symbol, "direction": direction,
                    "auto_protection_at": _iso_epoch(auto_protection_at),
                    "group_received_at": group_at,
                    "auto_sl": old_sl,
                    "group_sl": group_sl,
                    "applied_sl": leg_sl[leg], "winner": decision.winner,
                    "delta_pips": decision.delta_pips,
                    "delta_seconds": decision.delta_seconds,
                    "leg1_action": action,
                    "status": decision.status, "created_at": group_at,
                })

    for i, c in enumerate(candles[start:], start):
        high, low = c["high"], c["low"]
        if trace:
            path.append({"ts": c["ts"], "high": high, "low": low, "close": c["close"]})
        # trailing adaptativo por ATR: o recuo nunca fica mais apertado que
        # atr_k × ATR (velas ANTES desta — sem look-ahead); sem dados → fixo
        if atr_k and trail_factor_eff:
            _atr = _candle_atr(candles, i, atr_period)
            trail_dist = policy.trail_distance(atr=_atr, atr_k=atr_k)
        if direction == "buy":
            # 1) shared stop (conservative: stop first if same candle)
            if leg_open.get(1) and low <= sl:
                for i in legs:
                    if leg_open[i]:
                        leg_pnl[i] = _pnl("buy", entry_fill, _exit_fill("buy", sl, spread), lots[i], contract)
                        leg_open[i] = False
                outcome = "loss"
                _ev("sl", sl, "SL atinge — todas as pernas fecham")
                break
            # A mensagem protect atua depois de confirmar que o SL original
            # não foi cruzado.  `prev_close` é a última informação conhecida;
            # o close/high/low desta vela ainda não pode influenciar a decisão.
            _apply_hybrid_protect(prev_close)
            # 2) TP1: leg 1 closes; remaining legs move to breakeven (ou ao
            #    nível do TP1 com stair_from=1 — escada: lucro do TP1 garantido)
            if leg_open.get(1) and high >= tps[1]:
                leg_pnl[1] = _pnl("buy", entry_fill, _exit_fill("buy", tps[1], spread), lots[1], contract)
                leg_open[1] = False
                _ev("tp1", tps[1], "TP1 atinge — perna 1 fecha com lucro")
                tp1_ts = c["ts"]
                # The candle's high is not known at the instant TP1 is
                # filled.  Seed the next candle's extreme with the TP1 level;
                # the current candle's high is recorded only after all fills
                # below, so trailing cannot use its own future high.
                peak = tps[1]
                floor = policy.initial_floor()
                for i in legs:
                    if i != 1:
                        leg_sl[i] = floor
                if legs and len(legs) > 1:
                    auto_protection_at = c["ts"]
                if floor != entry:
                    _ev("stair", floor, f"escada: SL das pernas 2–5 sobe para o nível do TP1 ({floor})")
                else:
                    _ev("be", entry, "SL das pernas 2–5 sobe para a entrada (breakeven)")
            # gestão adaptativa: depois do TP1, o SL segue o pico do preço
            # (trailing) — fecha com lucro se o mercado inverter trail_dist.
            # SEM look-ahead: o SL é levantado com o pico das velas ANTERIORES
            # (o nível já existia no início desta vela quando ela o atravessa)
            # — antes usava-se o high da própria vela, o que dava fills
            # otimistas (whispsaw convertidos em lucro) e inflava o backtest.
            if trail_factor_eff and tp1_ts is not None and peak is not None:
                for i in legs:
                    if i != 1 and leg_open[i]:
                        new_sl = policy.trailing_target(
                            leg_sl[i], peak, distance=trail_dist,
                        )
                        if new_sl is not None:
                            leg_sl[i] = new_sl
                            auto_protection_at = c["ts"]
                            _ev("trail", leg_sl[i], f"perna {i} — SL segue o preço (trailing {round(trail_dist, 4)})")
            # time stop: o mercado estagnou após o TP1 — fechar ao mercado
            if time_stop_due(tp1_ts, c["ts"], time_stop_eff):
                for i in legs:
                    if i != 1 and leg_open[i]:
                        leg_pnl[i] = _pnl("buy", entry_fill, _exit_fill("buy", c["close"], spread), lots[i], contract)
                        leg_open[i] = False
                        outcome = "win" if leg_pnl[i] > 0 else ("be" if leg_pnl[i] == 0 else "loss")
                        _ev("time_stop", c["close"], f"perna {i} — mercado estagnado {time_stop_eff} min após TP1, fecha ao mercado")
            # 2b) "fechar tudo no TP": quando o TP configurado (ex.: TP2)
            # bate, TODAS as pernas ainda abertas fecham nesse preço
            if exit_at_tp and exit_at_tp in legs and leg_open.get(exit_at_tp) \
                    and high >= tps[exit_at_tp]:
                for i in legs:
                    if leg_open[i]:
                        leg_pnl[i] = _pnl("buy", entry_fill, _exit_fill("buy", tps[exit_at_tp], spread), lots[i], contract)
                        leg_open[i] = False
                outcome = "win"
                _ev("tp_all", tps[exit_at_tp], f"TP{exit_at_tp} atinge — todas as pernas abertas fecham")
                break
            # 3) remaining legs: their own SL (maybe BE/trailing/stair) or TP
            for i in legs:
                if i == 1 or not leg_open[i]:
                    continue
                if low <= leg_sl[i]:
                    leg_pnl[i] = _pnl("buy", entry_fill, _exit_fill("buy", leg_sl[i], spread), lots[i], contract)
                    leg_open[i] = False
                    # outcome pelo nível do SL relativo à entrada: acima = win
                    # (escada/trailing fecham com lucro), = entrada = BE,
                    # abaixo = loss — antes, fechar no degrau da escada
                    # (acima da entrada) era marcado como loss
                    if outcome == "open":
                        outcome = sl_outcome("buy", leg_sl[i], entry)
                    _ev("leg_sl", leg_sl[i], f"perna {i} fecha no SL" + (" (BE)" if leg_sl[i] == entry else ""))
                    # filtro de confirmação: a perna i morreu → as pernas
                    # seguintes fecham também (o movimento acabou)
                    if confirm_filter:
                        for j in confirmation_legs(i, legs):
                            if leg_open[j]:
                                leg_pnl[j] = _pnl("buy", entry_fill, _exit_fill("buy", c["close"], spread), lots[j], contract)
                                leg_open[j] = False
                                outcome = "win" if leg_pnl[j] > 0 else ("be" if leg_pnl[j] == 0 else "loss")
                                _ev("confirm", c["close"], f"perna {j} — perna {i} levou SL, fecha ao mercado (confirmação)")
                elif high >= tps[i]:
                    leg_pnl[i] = _pnl("buy", entry_fill, _exit_fill("buy", tps[i], spread), lots[i], contract)
                    leg_open[i] = False
                    outcome = "win"
                    _ev("leg_tp", tps[i], f"perna {i} fecha no TP{i}")
                    # escada: TP conquistado vira o novo chão das pernas seguintes
                    if stair_levels and i <= stair_levels:
                        for j in legs:
                            target = policy.stair_target(leg_sl[j], tps[i], j)
                            if j > i and leg_open[j] and target is not None:
                                leg_sl[j] = target
                                auto_protection_at = c["ts"]
                                _ev("stair", tps[i], f"escada: perna {j} — SL sobe para o nível do TP{i}")
            # depois dos fills: atualizar o pico para as velas seguintes
            if trail_factor_eff and tp1_ts is not None:
                peak = max(peak, high)
            if not any(leg_open.values()):
                break
            prev_close = c["close"]
        else:  # sell
            if leg_open.get(1) and high >= sl:
                for i in legs:
                    if leg_open[i]:
                        leg_pnl[i] = _pnl("sell", entry_fill, _exit_fill("sell", sl, spread), lots[i], contract)
                        leg_open[i] = False
                outcome = "loss"
                _ev("sl", sl, "SL atinge — todas as pernas fecham")
                break
            _apply_hybrid_protect(prev_close)
            if leg_open.get(1) and low <= tps[1]:
                leg_pnl[1] = _pnl("sell", entry_fill, _exit_fill("sell", tps[1], spread), lots[1], contract)
                leg_open[1] = False
                _ev("tp1", tps[1], "TP1 atinge — perna 1 fecha com lucro")
                tp1_ts = c["ts"]
                # Mirror the buy path: the current candle's low must not
                # create a trailing stop that the same candle can hit.
                trough = tps[1]
                floor = policy.initial_floor()
                for i in legs:
                    if i != 1:
                        leg_sl[i] = floor
                if legs and len(legs) > 1:
                    auto_protection_at = c["ts"]
                if floor != entry:
                    _ev("stair", floor, f"escada: SL das pernas 2–5 sobe para o nível do TP1 ({floor})")
                else:
                    _ev("be", entry, "SL das pernas 2–5 sobe para a entrada (breakeven)")
            # gestão adaptativa (sell): o SL segue o VALE do preço (trailing)
            # — sem look-ahead (o nível já existia antes desta vela)
            if trail_factor_eff and tp1_ts is not None and trough is not None:
                for i in legs:
                    if i != 1 and leg_open[i]:
                        new_sl = policy.trailing_target(
                            leg_sl[i], trough, distance=trail_dist,
                        )
                        if new_sl is not None:
                            leg_sl[i] = new_sl
                            auto_protection_at = c["ts"]
                            _ev("trail", leg_sl[i], f"perna {i} — SL segue o preço (trailing {round(trail_dist, 4)})")
            # time stop: o mercado estagnou após o TP1 — fechar ao mercado
            if time_stop_due(tp1_ts, c["ts"], time_stop_eff):
                for i in legs:
                    if i != 1 and leg_open[i]:
                        leg_pnl[i] = _pnl("sell", entry_fill, _exit_fill("sell", c["close"], spread), lots[i], contract)
                        leg_open[i] = False
                        outcome = "win" if leg_pnl[i] > 0 else ("be" if leg_pnl[i] == 0 else "loss")
                        _ev("time_stop", c["close"], f"perna {i} — mercado estagnado {time_stop_eff} min após TP1, fecha ao mercado")
            if exit_at_tp and exit_at_tp in legs and leg_open.get(exit_at_tp) \
                    and low <= tps[exit_at_tp]:
                for i in legs:
                    if leg_open[i]:
                        leg_pnl[i] = _pnl("sell", entry_fill, _exit_fill("sell", tps[exit_at_tp], spread), lots[i], contract)
                        leg_open[i] = False
                outcome = "win"
                _ev("tp_all", tps[exit_at_tp], f"TP{exit_at_tp} atinge — todas as pernas abertas fecham")
                break
            for i in legs:
                if i == 1 or not leg_open[i]:
                    continue
                if high >= leg_sl[i]:
                    leg_pnl[i] = _pnl("sell", entry_fill, _exit_fill("sell", leg_sl[i], spread), lots[i], contract)
                    leg_open[i] = False
                    # outcome pelo nível do SL relativo à entrada (espelho buy)
                    if outcome == "open":
                        outcome = sl_outcome("sell", leg_sl[i], entry)
                    _ev("leg_sl", leg_sl[i], f"perna {i} fecha no SL" + (" (BE)" if leg_sl[i] == entry else ""))
                    # filtro de confirmação: a perna i morreu → pernas seguintes
                    # fecham também (o movimento acabou)
                    if confirm_filter:
                        for j in confirmation_legs(i, legs):
                            if leg_open[j]:
                                leg_pnl[j] = _pnl("sell", entry_fill, _exit_fill("sell", c["close"], spread), lots[j], contract)
                                leg_open[j] = False
                                outcome = "win" if leg_pnl[j] > 0 else ("be" if leg_pnl[j] == 0 else "loss")
                                _ev("confirm", c["close"], f"perna {j} — perna {i} levou SL, fecha ao mercado (confirmação)")
                elif low <= tps[i]:
                    leg_pnl[i] = _pnl("sell", entry_fill, _exit_fill("sell", tps[i], spread), lots[i], contract)
                    leg_open[i] = False
                    outcome = "win"
                    _ev("leg_tp", tps[i], f"perna {i} fecha no TP{i}")
                    # escada: TP conquistado vira o novo chão das pernas seguintes
                    if stair_levels and i <= stair_levels:
                        for j in legs:
                            target = policy.stair_target(leg_sl[j], tps[i], j)
                            if j > i and leg_open[j] and target is not None:
                                leg_sl[j] = target
                                auto_protection_at = c["ts"]
                                _ev("stair", tps[i], f"escada: perna {j} — SL sobe para o nível do TP{i}")
            # depois dos fills: atualizar o vale para as velas seguintes
            if trail_factor_eff and tp1_ts is not None:
                trough = min(trough, low)
            if not any(leg_open.values()):
                break
            prev_close = c["close"]

    total = round(sum(leg_pnl.values()) - commission, 2)
    # modo defensivo: a trade resolvida entra no histórico PARTILHADO (o
    # caller corre os sinais por ordem cronológica) — a decisão do PRÓXIMO
    # sinal usa-a (nunca a própria trade, sem look-ahead)
    if def_state is not None and outcome not in ("open", "no_data", "skip"):
        def_state.setdefault("resolved", []).append((signal.id, total))
    payload = {
        "outcome": outcome,
        "pnl": total,
        "leg1_pnl": round(leg_pnl.get(1, 0.0), 2),
        "leg2_pnl": round(leg_pnl.get(2, 0.0), 2),
        "legs_pnl": {i: round(leg_pnl[i], 2) for i in legs},
        "commission": round(commission, 2),
        "delay_seconds": delay_seconds,
    }
    if trace:
        payload["events"] = events
        payload["path"] = path
    if strategy_id == "auto_escada_3_hibrida":
        payload["protect_latency"] = protect_latency
        payload["mgmt_applied"] = mgmt_applied
    return payload


def simulate_signal_managed(signal: Signal, candles: List[dict], mgmt_events: List[tuple],
                            risk_pct: float, balance: float, delay_seconds: int = 60,
                            spread_pips: float = 1.0, slippage_pips: float = 0.5,
                            commission_per_lot: float = 3.5,
                            lots_override: Optional[float] = None,
                            max_legs: Optional[int] = None,
                            exit_at_tp: Optional[int] = None,
                            manage_kinds: Optional[Set[str]] = None,
                            trail_factor: Optional[float] = None,
                            time_stop_minutes: Optional[int] = None,
                            stair_levels: Optional[int] = None,
                            stair_from: int = 1,
                            confirm_filter: bool = False,
                            atr_k: Optional[float] = None,
                            atr_period: int = 14,
                            def_window: Optional[int] = None,
                            def_trail: Optional[float] = None,
                            def_time_stop: Optional[int] = None,
                            def_state: Optional[dict] = None,
                            strategy_id: str = "") -> dict:
    """Like `simulate_signal`, but also applies the group's real-time
    management instructions when their timestamps are reached.

    `mgmt_events` is a sorted list of (epoch_seconds, kind, be, tp) for THIS
    signal (same group, between this signal and the next one):

      * "protect"   — group's "close first entries and move stop loss above
                       best entries": leg 1 closes at the market price now,
                       and every remaining leg's SL moves to entry + buffer
                       (locks a small profit, "risk free").
      * "close_all" — group's "we're out / out at BE": every open leg closes
                       now. `be=True` ("out at BE") fills at the ENTRY
                       (breakeven); `be=False` ("we're out") fills at the
                       current market price.
      * "tp_hit"    — confirmation only: price-based TP/TP1-BE rules already
                       handle it, so it changes nothing here.

    `max_legs` caps how many TPs are used (e.g. 3 = "TP1..TP3 + gestão"):
    as in `simulate_signal`, the risk is split among the legs actually
    opened and the management applies only to those legs.

    `manage_kinds` (estratégias do registo) filtra QUAIS kinds de gestão se
    aplicam — ex.: `{"protect"}` = "protect only" (close_all/close_tp1 são
    ignorados). None = aplica todos.

    Returns the same dict shape as `simulate_signal` (outcome, pnl,
    legs_pnl, commission...), plus `mgmt_applied` (kinds actually used).
    """
    from app.strategies import ALL_MGMT_KINDS
    if manage_kinds:
        mgmt_events = [e for e in mgmt_events if e[1] in manage_kinds]
    elif manage_kinds is not None:
        mgmt_events = []  # manage_kinds vazio = sem gestão

    # A híbrida precisa de uma única cronologia: a auto-gestão e o protect
    # são processados no mesmo replay, em vez de simular primeiro uma variante
    # e corrigir o resultado depois.
    if strategy_id == "auto_escada_3_hibrida":
        return simulate_signal(
            signal, candles, risk_pct, balance,
            delay_seconds=delay_seconds, spread_pips=spread_pips,
            slippage_pips=slippage_pips,
            commission_per_lot=commission_per_lot,
            lots_override=lots_override, max_legs=max_legs,
            exit_at_tp=exit_at_tp, trail_factor=trail_factor,
            time_stop_minutes=time_stop_minutes, stair_levels=stair_levels,
            stair_from=stair_from, confirm_filter=confirm_filter,
            atr_k=atr_k, atr_period=atr_period, def_window=def_window,
            def_trail=def_trail, def_time_stop=def_time_stop,
            def_state=def_state, mgmt_events=mgmt_events,
            strategy_id=strategy_id,
        )

    base = simulate_signal(signal, candles, risk_pct, balance,
                           delay_seconds=delay_seconds, spread_pips=spread_pips,
                           slippage_pips=slippage_pips,
                           commission_per_lot=commission_per_lot,
                           lots_override=lots_override, max_legs=max_legs,
                           exit_at_tp=exit_at_tp)
    if base["outcome"] == "no_data" or not mgmt_events:
        base["mgmt_applied"] = []
        return base

    start = _start_index(candles, _to_epoch(signal.received_at) + delay_seconds)
    entry, sl = signal.entry, signal.stop_loss
    direction = signal.direction
    pip = _pip_size(signal.symbol)
    contract = _contract(signal.symbol)
    spread = spread_pips * pip
    slippage = slippage_pips * pip
    entry_fill = (entry + spread / 2 + slippage) if direction == "buy" \
        else (entry - spread / 2 - slippage)

    tps = {}
    for i in range(1, 6):
        tp = float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0)
        if tp > 0:
            tps[i] = tp
    legs = sorted(tps)
    if max_legs is not None and max_legs >= 1:
        legs = legs[:int(max_legs)]
    if not legs:
        return {"outcome": "no_data", "pnl": 0.0, "leg1_pnl": 0.0, "leg2_pnl": 0.0}

    lots = {
        i: compute_lots(balance, risk_pct / len(legs), entry, sl,
                        pip_size=pip, contract_size=contract)
        for i in legs
    }
    commission = commission_per_lot * sum(lots.values()) * 2

    leg_open = {i: True for i in legs}
    leg_pnl = {i: 0.0 for i in legs}
    leg_sl = {i: sl for i in legs}
    outcome = "open"
    applied = []
    ev_idx = 0

    def _close_leg(i, price):
        leg_pnl[i] = _pnl(direction, entry_fill, _exit_fill(direction, price, spread),
                          lots[i], contract)
        leg_open[i] = False

    # candle ts is the START of the interval; an event belongs to the candle
    # that CONTAINS it (ts <= ev < ts + interval), so it acts before that
    # candle's SL/TP evaluation instead of being lost to the next candle.
    # Market fills use the LAST KNOWN close (the previous candle) — never the
    # current candle's close, which would leak information from the future.
    interval = 900
    if len(candles) > 1:
        interval = max(60, candles[1]["ts"] - candles[0]["ts"])
    prev_close = candles[start - 1]["close"] if start > 0 else candles[start]["open"]

    for c in candles[start:]:
        high, low, close = c["high"], c["low"], c["close"]

        # The broker's stop loss is automatic: if THIS candle already crossed
        # the SL, the SL filled first — a late management message must not
        # "save" the trade at a worse price. Evaluate SL/TP before the events
        # only when the candle crossed the stop (the events act on candles
        # where the stop was NOT touched).
        sl_crossed = (low <= sl) if direction == "buy" else (high >= sl)
        stop_first = sl_crossed

        # --- apply the group's management instructions whose time has come ---
        if not stop_first:
            while ev_idx < len(mgmt_events) and mgmt_events[ev_idx][0] < c["ts"] + interval:
                kind = mgmt_events[ev_idx][1]
                be = bool(mgmt_events[ev_idx][2])
                tp = mgmt_events[ev_idx][3] if len(mgmt_events[ev_idx]) > 3 else None
                if kind == "close_all":
                    # "out at BE": the group already secured TP1 (leg 1 closed
                    # at TP1) and the REMAINING legs exit at the entry
                    # (breakeven); "we're out": everything exits at the last
                    # known price.
                    for i in legs:
                        if not leg_open[i]:
                            continue
                        if be and i == 1:
                            _close_leg(i, tps[1])   # secured TP1 before walking
                        else:
                            _close_leg(i, entry if be else prev_close)
                    applied.append("close_all" + ("_be" if be else ""))
                    # the group walked away: the trade is done, P&L decides
                    total_now = round(sum(leg_pnl.values()) - commission, 2)
                    outcome = "win" if total_now > 0 else ("loss" if total_now < 0 else "be")
                    break
                if kind == "close_tp1":
                    # "Close TP1 here" — partial profit ONLY: leg 1 closes at
                    # the market, the remaining legs keep their SL (no move
                    # above entry). Isolates the effect of the partial close
                    # from the protection step.
                    if leg_open.get(1):
                        _close_leg(1, prev_close)
                        applied.append("close_tp1")
                if kind == "close_tp":
                    # "Close TP2..TP6 here" — partial profit numa perna N.
                    leg = int(tp or 2)
                    if leg_open.get(leg):
                        _close_leg(leg, prev_close)
                        applied.append("close_tp")
                if kind == "protect":
                    # close leg 1 now (partial profit) + move SL above entry
                    if leg_open.get(1):
                        _close_leg(1, prev_close)
                        applied.append("protect")
                    risk = abs(entry - sl)
                    buffer = min(max(risk * 0.1, risk * 0.01),
                                 abs(tps[1] - entry)) if tps else risk * 0.1
                    for i in legs:
                        if i != 1 and leg_open[i]:
                            leg_sl[i] = (entry + buffer) if direction == "buy" \
                                else (entry - buffer)
                ev_idx += 1
        prev_close = close
        if applied and not any(leg_open.values()):
            break

        # --- normal SL / TP / BE evaluation on this candle ---
        if direction == "buy":
            if leg_open.get(1) and low <= sl:
                for i in legs:
                    if leg_open[i]:
                        _close_leg(i, sl)
                outcome = "loss"
                break
            if leg_open.get(1) and high >= tps[1]:
                _close_leg(1, tps[1])
                for i in legs:
                    if i != 1:
                        leg_sl[i] = entry
            # "sair no TP N + gestão": quando o TP configurado bate, TODAS as
            # pernas ainda abertas fecham nesse preço (mesma regra do
            # simulate_signal) — combinável com a gestão do grupo
            if exit_at_tp and exit_at_tp in legs and leg_open.get(exit_at_tp) \
                    and high >= tps[exit_at_tp]:
                for i in legs:
                    if leg_open[i]:
                        _close_leg(i, tps[exit_at_tp])
                outcome = "win"
                break
            for i in legs:
                if i == 1 or not leg_open[i]:
                    continue
                if low <= leg_sl[i]:
                    _close_leg(i, leg_sl[i])
                    if outcome == "open":
                        outcome = "be" if leg_sl[i] == entry else "loss"
                elif high >= tps[i]:
                    _close_leg(i, tps[i])
                    outcome = "win"
            if not any(leg_open.values()):
                break
            prev_close = c["close"]
        else:  # sell
            if leg_open.get(1) and high >= sl:
                for i in legs:
                    if leg_open[i]:
                        _close_leg(i, sl)
                outcome = "loss"
                break
            if leg_open.get(1) and low <= tps[1]:
                _close_leg(1, tps[1])
                for i in legs:
                    if i != 1:
                        leg_sl[i] = entry
            if exit_at_tp and exit_at_tp in legs and leg_open.get(exit_at_tp) \
                    and low <= tps[exit_at_tp]:
                for i in legs:
                    if leg_open[i]:
                        _close_leg(i, tps[exit_at_tp])
                outcome = "win"
                break
            for i in legs:
                if i == 1 or not leg_open[i]:
                    continue
                if high >= leg_sl[i]:
                    _close_leg(i, leg_sl[i])
                    if outcome == "open":
                        outcome = "be" if leg_sl[i] == entry else "loss"
                elif low <= tps[i]:
                    _close_leg(i, tps[i])
                    outcome = "win"
            if not any(leg_open.values()):
                break
            prev_close = c["close"]

    total = round(sum(leg_pnl.values()) - commission, 2)
    return {
        "outcome": outcome,
        "pnl": total,
        "leg1_pnl": round(leg_pnl.get(1, 0.0), 2),
        "leg2_pnl": round(leg_pnl.get(2, 0.0), 2),
        "legs_pnl": {i: round(leg_pnl[i], 2) for i in legs},
        "commission": round(commission, 2),
        "delay_seconds": delay_seconds,
        "mgmt_applied": applied,
    }


def attribute_protect_events(signal: Signal, candles: List[dict],
                             mgmt_events: List[tuple], risk_pct: float,
                             balance: float, **sim_kwargs) -> dict:
    """Attribute the causal P&L effect of each hybrid ``protect`` event.

    The comparison is chronological and uses the same candles, costs and
    mechanical parameters for every replay:

    * ``baseline`` has the auto policy but no group protect;
    * each next replay adds exactly one more protect event to the prefix;
    * the event's incremental effect is ``prefix_after - prefix_before``.

    This is an intervention-style attribution, not a claim that events are
    statistically independent.  The ordered prefix makes interactions
    explicit and the increments telescope to the full hybrid-vs-auto delta.
    ``def_state`` is copied for every counterfactual so these extra replays
    never leak a future result into the real chronological state.
    """
    protects = sorted(
        [event for event in (mgmt_events or [])
         if len(event) > 1 and event[1] == "protect"],
        key=lambda event: (event[0], tuple(event[1:])),
    )
    common = dict(sim_kwargs)
    common.pop("strategy_id", None)
    common.pop("manage_kinds", None)
    common.pop("mgmt_events", None)
    source_def_state = common.pop("def_state", None)

    def _state_copy():
        if source_def_state is None:
            return None
        copied = dict(source_def_state)
        copied["resolved"] = list(source_def_state.get("resolved") or [])
        return copied

    def _run_hybrid(events: List[tuple]) -> dict:
        kwargs = dict(common)
        local_state = _state_copy()
        if local_state is not None:
            kwargs["def_state"] = local_state
        return simulate_signal_managed(
            signal, candles, events, risk_pct, balance,
            manage_kinds={"protect"},
            strategy_id="auto_escada_3_hibrida",
            **kwargs,
        )

    baseline_kwargs = dict(common)
    baseline_state = _state_copy()
    if baseline_state is not None:
        baseline_kwargs["def_state"] = baseline_state
    baseline = simulate_signal(
        signal, candles, risk_pct, balance, **baseline_kwargs,
    )

    def _compact(result: dict) -> dict:
        return {
            "outcome": result.get("outcome"),
            "pnl": round(float(result.get("pnl") or 0.0), 2),
            "legs_pnl": result.get("legs_pnl") or {},
        }

    if not protects:
        return {
            "signal_id": signal.id, "event_count": 0,
            "baseline": _compact(baseline), "hybrid": _compact(baseline),
            "protect_delta_pnl": 0.0, "harmful_event_count": 0,
            "harmful_delta_pnl": 0.0, "events": [],
        }

    prefix: List[tuple] = []
    before = baseline
    rows: List[dict] = []
    previous_latency_count = 0
    for index, event in enumerate(protects):
        prefix.append(event)
        after = _run_hybrid(prefix)
        incremental = round(
            float(after.get("pnl") or 0.0) - float(before.get("pnl") or 0.0), 2
        )
        latency_rows = after.get("protect_latency") or []
        new_latency = latency_rows[previous_latency_count:]
        winners: dict = {}
        statuses: dict = {}
        for decision in new_latency:
            winner = decision.get("winner") or "none"
            winners[winner] = winners.get(winner, 0) + 1
            status = decision.get("status") or "unknown"
            statuses[status] = statuses.get(status, 0) + 1
        rows.append({
            "index": index,
            "event_at": event[0],
            "kind": event[1],
            "before_pnl": round(float(before.get("pnl") or 0.0), 2),
            "after_pnl": round(float(after.get("pnl") or 0.0), 2),
            "incremental_pnl": incremental,
            "harmful": incremental < 0.0,
            "before_outcome": before.get("outcome"),
            "after_outcome": after.get("outcome"),
            "eligible": any(status == "ok" for status in statuses),
            "winners": winners,
            "statuses": statuses,
            "latency_rows": len(new_latency),
        })
        previous_latency_count = len(latency_rows)
        before = after

    hybrid = before
    harmful = [row for row in rows if row["harmful"]]
    return {
        "signal_id": signal.id,
        "event_count": len(protects),
        "baseline": _compact(baseline),
        "hybrid": _compact(hybrid),
        "protect_delta_pnl": round(
            float(hybrid.get("pnl") or 0.0) - float(baseline.get("pnl") or 0.0), 2
        ),
        "harmful_event_count": len(harmful),
        "harmful_delta_pnl": round(sum(row["incremental_pnl"] for row in harmful), 2),
        "events": rows,
    }


def correlate_management(signals: List[Signal], management: List[tuple]) -> dict:
    """Assign each management event to the signal it belongs to.

    `signals` is the ordered list of real signals (per group). `management`
    is a list of (source, epoch, kind, be, tp). A management event belongs
    to the most recent signal of the SAME source that arrived before it (the
    group talks about its current trade) — so "TP1 HIT" at 07:18 lands on
    the 07:11 signal, not the one at 09:15.

    Returns {signal_id: [(epoch, kind, be, tp), ...]}.
    """
    by_source: dict = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)

    out: dict = {s.id: [] for s in signals}
    for item in management:
        source, epoch, kind, be = item[0], item[1], item[2], item[3]
        tp = item[4] if len(item) > 4 else None
        pool = by_source.get(source)
        if not pool:
            continue
        latest = None
        for s in pool:
            if _to_epoch(s.received_at) <= epoch:
                latest = s
            else:
                break
        if latest is not None:
            out[latest.id].append((epoch, kind, be, tp))
    for sid, events in out.items():
        events.sort()
    return out


def drawdown_from_results(results: List[dict], balance: float) -> dict:
    """Máximo drawdown (pico-a-vale) do P&L acumulado, em moeda e % da conta.

    Ordena os resultados por `received_at` (como se fossem fechados nessa
    ordem) e percorre a curva acumulada.
    """
    ordered = sorted(
        (r for r in results if r["outcome"] in ("win", "loss", "be")),
        key=lambda r: r.get("received_at", ""),
    )
    run = peak = 0.0
    max_dd = 0.0
    for r in ordered:
        run += r["pnl"]
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
    pct = (max_dd / balance * 100) if balance > 0 else 0.0
    return {"amount": round(max_dd, 2), "pct": round(pct, 1)}


def apply_daily_stop(results: List[dict], balance: float, stop_pct: float) -> dict:
    """Modela o kill switch diário da produção (auto kill): por dia civil,
    quando o P&L realizado do dia ≤ -balance*stop_pct/100, as trades
    seguintes desse dia deixam de acontecer (outcome="skip", pnl=0).

    Mutates `results` in place (zera o pnl das trades saltadas) e devolve
    `{"skipped": n}` para a dashboard mostrar quantas foram cortadas.
    `stop_pct <= 0` = sem limite (off).
    """
    if not stop_pct or stop_pct <= 0:
        return {"skipped": 0}
    limit = balance * float(stop_pct) / 100.0
    skipped = 0
    day = None
    day_pnl = 0.0
    for r in sorted(results, key=lambda x: x.get("received_at", "")):
        d = (r.get("received_at") or "")[:10]
        if d != day:
            day, day_pnl = d, 0.0
        if day_pnl <= -limit:
            # o dia já estourou o limite — esta trade não acontece
            r["outcome"] = "skip"
            r["pnl"] = 0.0
            skipped += 1
            continue
        pnl = float(r.get("pnl") or 0.0)
        day_pnl += pnl
    return {"skipped": skipped}


def aggregate(results: List[dict]) -> dict:
    """Resumo agregado dos resultados do backtest."""
    resolved = [r for r in results if r["outcome"] in ("win", "loss", "be")]
    wins = [r for r in resolved if r["pnl"] > 0]
    losses = [r for r in resolved if r["pnl"] < 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))

    return {
        "total": len(results),
        "no_data": sum(1 for r in results if r["outcome"] == "no_data"),
        "open": sum(1 for r in results if r["outcome"] == "open"),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": sum(1 for r in resolved if r["pnl"] == 0),
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else 0.0,
        "pnl": round(sum(r["pnl"] for r in results), 2),
        "commission": round(sum(r.get("commission", 0.0) for r in results), 2),
        "avg_win": round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(r["pnl"] for r in losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
    }
