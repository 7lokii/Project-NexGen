"""Edge analysis for the real signals of a Telegram group.

Reads the parsed signals stored by the listener (history + live), replays
them over real candles (Yahoo) with the same TP1/TP2/BE strategy and costs
used in live mode, and produces an aggregate + verdict — meant to be checked
BEFORE enabling auto mode.

The same core is used by:
  * the CLI:  .venv/Scripts/python -m app.analysis [--group ...] [--days 30]
  * the dashboard tab ("🎯 Edge") via GET /api/analysis

The script/endpoint never writes to the DB and never opens trades.
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional

from app.backtest import (aggregate, apply_daily_stop, attribute_protect_events,
                          correlate_management, drawdown_from_results,
                          simulate_signal, simulate_signal_managed)
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider
from app.messages.manage import classify
from app.messages.summaries import parse_summary
from app.signals.parser import ParseError


def _to_epoch(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp())

# Same defaults as the dashboard simulator (SimulateIn).
DEFAULT_RISK_PCT = 1.0
DEFAULT_DELAY = 60
DEFAULT_SPREAD = 1.0
DEFAULT_SLIPPAGE = 0.5
DEFAULT_COMMISSION = 3.5


def verdict_data(agg: dict) -> dict:
    """Verdict levels: edge / marginal / none / small_sample."""
    n = agg["resolved"]
    if n < 10:
        return {
            "level": "small_sample",
            "text": (f"Amostra demasiado pequena ({n} trades resolvidas) — "
                     "ainda não dá para concluir edge. Precisas de mais sinais reais."),
        }
    wr = agg["win_rate"]
    pf = agg["profit_factor"] or 0.0
    if pf >= 1.2 and wr >= 40:
        return {
            "level": "edge",
            "text": (f"Edge provável: WR {wr}% com PF {pf} em {n} trades reais — "
                     "candidata a auto (com kill switch)."),
        }
    if pf >= 1.0:
        return {
            "level": "marginal",
            "text": (f"Edge marginal: WR {wr}% / PF {pf} em {n} trades — "
                     "aproximar com cautela, mais dados antes de auto."),
        }
    return {
        "level": "none",
        "text": (f"Sem edge visível: WR {wr}% / PF {pf} em {n} trades — "
                 "não ligar auto com esta estratégia."),
    }


def _signal_buckets(signals: List, n: int = 4) -> List[tuple]:
    """Split an ordered signal list into `n` chronological buckets (same
    order as they happened — walk-forward style). Returns (label, signals).
    """
    ordered = sorted(signals, key=lambda s: s.received_at or "")
    if not ordered:
        return []
    n = max(1, min(n, len(ordered)))
    size = max(1, len(ordered) // n)
    buckets = []
    for i in range(0, len(ordered), size):
        chunk = ordered[i:i + size]
        first = (chunk[0].received_at or "")[:10]
        last = (chunk[-1].received_at or "")[:10]
        label = first if first == last else f"{first} → {last}"
        buckets.append((label, chunk))
    return buckets


async def run_walk_forward(db: DB, group: str = "", days: int = 30,
                           risk_pct: float = DEFAULT_RISK_PCT,
                           delay: int = DEFAULT_DELAY,
                           spread: float = DEFAULT_SPREAD,
                           slippage: float = DEFAULT_SLIPPAGE,
                           commission: float = DEFAULT_COMMISSION,
                           balance: Optional[float] = None,
                           buckets_n: int = 4) -> dict:
    """Walk-forward check of the managed strategy.

    Splits the real signals into `buckets_n` chronological windows and runs
    the managed simulation on each, so the Edge tab can show whether the
    edge (PF 4.39) holds across time or comes from a single lucky window.
    Never writes to the DB.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
    ]
    if balance is None:
        balance = float(db.all_config().get("balance", 10000) or 10000)
    if not signals:
        return {"ok": True, "signals": 0, "buckets": [], "summary": None}

    interval, range_ = ("15m", "1mo") if days <= 30 else ("1h", "3mo")
    provider = YahooProvider()

    # candles + management events fetched ONCE, reused across buckets
    mgmt_events = []
    for s in db.signals_since(since):
        try:
            action = classify(s.raw_text or "")
        except Exception:
            action = None
        if not action or action["kind"] not in ("protect", "close_all", "close_tp1", "close_tp", "tp_hit"):
            continue
        try:
            epoch = _to_epoch(s.received_at)
        except (ValueError, TypeError):
            continue
        mgmt_events.append((s.source or "", epoch, action["kind"],
                            bool(action.get("be")), action.get("tp")))
    mgmt_all = correlate_management(signals, mgmt_events)

    candles_by_symbol: Dict[str, List] = {}
    for symbol in {s.symbol for s in signals}:
        try:
            candles_by_symbol[symbol] = await provider.history(
                symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[walk-forward] sem histórico para {symbol}: {exc}")

    def _simulate(bucket_signals: List) -> List[dict]:
        out = []
        for s in bucket_signals:
            candles = candles_by_symbol.get(s.symbol)
            if not candles:
                continue
            res = simulate_signal_managed(
                s, candles, mgmt_all.get(s.id, []),
                risk_pct, balance, delay, spread, slippage, commission)
            out.append({"id": s.id, "source": s.source, "symbol": s.symbol,
                        "direction": s.direction, "entry": s.entry,
                        "received_at": s.received_at, **res})
        return out

    buckets = []
    for label, chunk in _signal_buckets(signals, buckets_n):
        results = _simulate(chunk)
        agg = aggregate(results)
        buckets.append({
            "label": label, "signals": len(chunk), "resolved": agg["resolved"],
            "wins": agg["wins"], "losses": agg["losses"],
            "win_rate": agg["win_rate"], "pnl": agg["pnl"],
            "profit_factor": agg["profit_factor"],
            "drawdown": drawdown_from_results(results, balance),
        })

    resolved_buckets = [b for b in buckets if b["resolved"] >= 3]
    pfs = [b["profit_factor"] for b in resolved_buckets if b["profit_factor"] is not None]
    profitable = sum(1 for b in resolved_buckets if b["pnl"] > 0)
    summary = {
        "buckets": len(buckets),
        "resolved_buckets": len(resolved_buckets),
        "profitable": profitable,
        "all_profitable": len(resolved_buckets) > 0 and profitable == len(resolved_buckets),
        "pf_range": [min(pfs), max(pfs)] if pfs else None,
        "worst_pnl": min((b["pnl"] for b in resolved_buckets), default=0.0),
    }
    return {"ok": True, "signals": len(signals), "buckets": buckets,
            "summary": summary}


async def run_analysis(db: DB, group: str = "", days: int = 30,
                       risk_pct: Optional[float] = None,
                       delay: int = DEFAULT_DELAY, spread: float = DEFAULT_SPREAD,
                       slippage: float = DEFAULT_SLIPPAGE,
                       commission: float = DEFAULT_COMMISSION,
                       balance: Optional[float] = None,
                       max_legs: Optional[int] = None,
                       manage: bool = False,
                       manage_kinds: Optional[set] = None,
                       exit_at_tp: Optional[int] = None,
                       lots_override: Optional[float] = None,
                       since: str = "",
                       until: str = "",
                       daily_stop_pct: Optional[float] = None,
                       trail_factor: Optional[float] = None,
                       time_stop_minutes: Optional[int] = None,
                       stair_levels: Optional[int] = None,
                       stair_from: int = 1,
                       confirm_filter: bool = False,
                       def_window: Optional[int] = None,
                       def_trail: Optional[float] = None,
                       def_time_stop: Optional[int] = None,
                       atr_k: Optional[float] = None,
                       atr_period: int = 14,
                       strategy_id: str = "",
                       include_causal: bool = False) -> dict:
    """Backtest the group's real signals on real candles and return a payload.

    `group` is a substring filter on the source (e.g. "Golden Moves"); empty
    means all groups. `max_legs` caps the TPs used per signal (2 = old
    strategy, None = all TPs). `days <= 0` = TODO o histórico na BD (desde o
    início do grupo, sem filtro de data). `since` (YYYY-MM-DD, opcional)
    filtra os sinais a partir dessa data; `until` (YYYY-MM-DD, opcional)
    filtra até essa data (exclusiva) — juntos isolam um período (o
    out-of-sample antigo: since=2026-04-01, until=2026-07-28).
    `daily_stop_pct` modela o kill switch diário da produção (None = usa
    `daily_loss_limit_pct` da config; 0 = off). `risk_pct` None = usa
    `risk_pct` da config (o slider).

    Auto-gestão (spec): `trail_factor`/`time_stop_minutes`/`stair_levels`/
    `stair_from`/`confirm_filter` são os parâmetros adaptativos do
    `simulate_signal`. As variantes `auto_*` usam o ramo sem gestão do grupo;
    a `auto_escada_3_hibrida` é a exceção: combina estes parâmetros com
    `manage_kinds={"protect"}` num único replay cronológico.
    Never writes to the DB.
    """
    since = since or ((date.today() - timedelta(days=days)).isoformat() if (days and days > 0) else "")
    if risk_pct is None:
        risk_pct = float(db.all_config().get("risk_pct") or DEFAULT_RISK_PCT)
    if daily_stop_pct is None:
        daily_stop_pct = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
        and (not until or (s.received_at or "")[:10] < until)
    ]
    if balance is None:
        balance = float(db.all_config().get("balance", 10000) or 10000)

    if not signals:
        return {
            "ok": True, "signals": 0, "group": group, "days": days,
            "balance": balance, "risk_pct": risk_pct,
            "aggregate": aggregate([]), "results": [],
            "pnl_by_month": [], "pnl_by_leg": [], "since": since,
            "daily_stop_pct": daily_stop_pct, "daily_stop_skipped": 0,
            "strategy_id": strategy_id, "protect_latency": [],
            "protect_causal": {
                "signals": 0, "events": 0, "harmful_events": 0,
                "delta_pnl": 0.0, "harmful_delta_pnl": 0.0, "rows": [],
            },
            "verdict": verdict_data(aggregate([])),
        }

    # Yahoo only serves 15m candles for ~1 month; use 1h candles for longer
    # windows so the whole signal period is covered. Histórico completo
    # (days<=0) usa 1h/730d para cobrir o grupo desde o início.
    if days and days > 0:
        interval, range_ = ("15m", "1mo") if days <= 30 else ("1h", "3mo")
    else:
        interval, range_ = ("1h", "730d")

    provider = YahooProvider()
    by_symbol: Dict[str, List] = {}
    for s in signals:
        by_symbol.setdefault(s.symbol, []).append(s)

    # group management instructions (TP HIT / protect / close_all / close_tp1)
    # assigned to the signal they refer to — only used when `manage=True`
    mgmt_by_signal: dict = {}
    if manage:
        mgmt_events = []
        for s in db.signals_since(since):
            # `until` is exclusive for both entries and the management that
            # belongs to them.  Without this guard, a protect/close_all
            # posted after an out-of-sample window could be correlated to the
            # last signal before `until` and leak future information into the
            # replay.
            if until and (s.received_at or "")[:10] >= until:
                continue
            try:
                action = classify(s.raw_text or "")
            except Exception:
                action = None
            if not action or action["kind"] not in ("protect", "close_all", "close_tp1", "tp_hit"):
                continue
            try:
                epoch = _to_epoch(s.received_at)
            except (ValueError, TypeError):
                continue
            mgmt_events.append((s.source or "", epoch, action["kind"],
                                bool(action.get("be")), action.get("tp")))
        mgmt_by_signal = correlate_management(signals, mgmt_events)

    # modo defensivo (estudo 19/08): estado PARTILHADO entre sinais — o PF
    # das últimas N trades resolvidas decide se a estratégia passa a trail
    # apertado + time stop (sem look-ahead: só trades anteriores ao sinal).
    # Os sinais são corridos por símbolo; o histórico é global por janela.
    def_state = {"resolved": []} if def_window else None
    # Fetch each symbol once, then replay the signals in one global
    # chronological stream.  The defensive PF state is shared across symbols;
    # iterating the symbol buckets independently would let a later signal in
    # one symbol influence an earlier signal in another symbol.
    candles_by_symbol: Dict[str, List] = {}
    for symbol in by_symbol:
        try:
            candles_by_symbol[symbol] = await provider.history(
                symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[análise] sem histórico para {symbol}: {exc}")
    causal_rows = []
    results = []
    ordered_signals = sorted(signals, key=lambda x: (x.received_at or "", x.id))
    for s in ordered_signals:
        candles = candles_by_symbol.get(s.symbol)
        if not candles:
            continue
        if include_causal and strategy_id == "auto_escada_3_hibrida":
            causal = attribute_protect_events(
                s, candles, mgmt_by_signal.get(s.id, []),
                risk_pct, balance,
                delay_seconds=delay, spread_pips=spread,
                slippage_pips=slippage, commission_per_lot=commission,
                lots_override=lots_override, max_legs=max_legs,
                exit_at_tp=exit_at_tp, trail_factor=trail_factor,
                time_stop_minutes=time_stop_minutes,
                stair_levels=stair_levels, stair_from=stair_from,
                confirm_filter=confirm_filter, atr_k=atr_k,
                atr_period=atr_period, def_window=def_window,
                def_trail=def_trail, def_time_stop=def_time_stop,
                def_state=def_state,
            )
            if causal.get("event_count"):
                causal_rows.append({
                    "id": s.id, "source": s.source, "symbol": s.symbol,
                    "direction": s.direction, "received_at": s.received_at,
                    **causal,
                })
        if manage:
            res = simulate_signal_managed(
                s, candles, mgmt_by_signal.get(s.id, []),
                risk_pct, balance, delay, spread, slippage, commission,
                lots_override=lots_override, max_legs=max_legs,
                exit_at_tp=exit_at_tp, manage_kinds=manage_kinds,
                trail_factor=trail_factor,
                time_stop_minutes=time_stop_minutes,
                stair_levels=stair_levels, stair_from=stair_from,
                confirm_filter=confirm_filter, atr_k=atr_k,
                atr_period=atr_period, def_window=def_window,
                def_trail=def_trail, def_time_stop=def_time_stop,
                def_state=def_state, strategy_id=strategy_id)
        else:
            res = simulate_signal(s, candles, risk_pct, balance, delay,
                                  spread, slippage, commission,
                                  max_legs=max_legs, exit_at_tp=exit_at_tp,
                                  lots_override=lots_override,
                                  trail_factor=trail_factor,
                                  time_stop_minutes=time_stop_minutes,
                                  stair_levels=stair_levels,
                                  stair_from=stair_from,
                                  confirm_filter=confirm_filter,
                                  def_window=def_window,
                                  def_trail=def_trail,
                                  def_time_stop=def_time_stop,
                                  def_state=def_state,
                                  atr_k=atr_k, atr_period=atr_period)
        results.append({
            "id": s.id, "source": s.source, "symbol": s.symbol,
            "direction": s.direction, "entry": s.entry,
            "received_at": s.received_at, **res,
        })

    # eventos de telemetria do replay (não são persistidos pelo backtest)
    protect_latency = []
    for result in results:
        protect_latency.extend(result.get("protect_latency") or [])

    # kill switch diário da produção: trades depois do limite do dia não
    # acontecem (outcome="skip", pnl=0) — os números refletem a realidade
    stop_info = apply_daily_stop(results, balance, daily_stop_pct)

    results.sort(key=lambda r: r["received_at"], reverse=True)
    agg = aggregate(results)
    # P&L acumulado por mês (para o gráfico de barras do bloco "desde o início")
    by_month: dict = {}
    for r in sorted(results, key=lambda x: x.get("received_at", "")):
        m = (r.get("received_at") or "")[:7]
        if m:
            by_month[m] = by_month.get(m, 0.0) + float(r.get("pnl") or 0.0)
    # P&L por perna (leg1..leg5) — onde o lucro vem, só trades resolvidas
    by_leg: dict = {}
    for r in results:
        if r.get("outcome") not in ("win", "loss", "be"):
            continue
        for i, v in (r.get("legs_pnl") or {}).items():
            by_leg[i] = by_leg.get(i, 0.0) + float(v)

    payload = {
        "ok": True, "signals": len(signals), "simulated": len(results),
        "group": group, "days": days, "balance": balance,
        "risk_pct": risk_pct, "delay_seconds": delay,
        "spread_pips": spread, "slippage_pips": slippage,
        "commission_per_lot": commission, "max_legs": max_legs,
        "manage": manage, "exit_at_tp": exit_at_tp,
        "lots_override": lots_override, "since": since, "until": until,
        "trail_factor": trail_factor, "time_stop_minutes": time_stop_minutes,
        "stair_levels": stair_levels, "stair_from": stair_from,
        "confirm_filter": confirm_filter,
        "strategy_id": strategy_id,
        "daily_stop_pct": daily_stop_pct,
        "daily_stop_skipped": stop_info["skipped"],
        "pnl_by_month": [{"month": m, "pnl": round(v, 2)} for m, v in sorted(by_month.items())],
        "pnl_by_leg": [{"leg": i, "pnl": round(v, 2)} for i, v in sorted(by_leg.items())],
        "drawdown": drawdown_from_results(results, balance),
        "aggregate": agg, "results": results, "verdict": verdict_data(agg),
        "protect_latency": protect_latency,
        "protect_causal": {
            "signals": len(causal_rows),
            "events": sum(row.get("event_count", 0) for row in causal_rows),
            "harmful_events": sum(row.get("harmful_event_count", 0)
                                   for row in causal_rows),
            "delta_pnl": round(sum(row.get("protect_delta_pnl", 0.0)
                                    for row in causal_rows), 2),
            "harmful_delta_pnl": round(sum(row.get("harmful_delta_pnl", 0.0)
                                           for row in causal_rows), 2),
            "rows": causal_rows,
        },
    }
    if manage:
        applied = sum(1 for r in results if r.get("mgmt_applied"))
        payload["mgmt_applied"] = applied
    return payload


def summarize_latency(rows: List[dict]) -> dict:
    """Summarize hybrid protect decisions without persisting them."""
    import statistics

    seconds = sorted(float(r["delta_seconds"]) for r in rows
                     if r.get("delta_seconds") is not None)
    pips = sorted(float(r["delta_pips"]) for r in rows
                  if r.get("delta_pips") is not None)

    def p90(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return values[max(0, min(len(values) - 1, int(len(values) * 0.9) - 1))]

    return {
        "count": len(rows),
        "auto_wins": sum(r.get("winner") == "auto" for r in rows),
        "group_wins": sum(r.get("winner") == "group" for r in rows),
        "ties": sum(r.get("winner") == "tie" for r in rows),
        "auto_not_triggered": sum(
            r.get("status") == "auto_not_triggered" for r in rows
        ),
        "mean_seconds": round(sum(seconds) / len(seconds), 2) if seconds else None,
        "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
        "p90_seconds": round(p90(seconds), 2) if seconds else None,
        "max_seconds": round(seconds[-1], 2) if seconds else None,
        "mean_pips": round(sum(pips) / len(pips), 2) if pips else None,
        "p90_pips": round(p90(pips), 2) if pips else None,
        "max_pips": round(pips[-1], 2) if pips else None,
    }


async def protect_latency_report(db: DB,
                                 strategy_id: str = "auto_escada_3_hibrida",
                                 engine: str = "", limit: int = 500,
                                 days: int = 0, since: str = "") -> dict:
    """Return persisted paper/MT5 protect telemetry or a fresh backtest.

    Backtest rows are deliberately kept out of SQLite; paper and MT5 rows are
    operational audit data and are read from ``protect_latency_log``.
    """
    safe_limit = max(1, min(int(limit), 500))
    if engine == "backtest":
        from app.strategies import sim_params
        payload = await run_analysis(
            db, group="", days=days, since=since, daily_stop_pct=0,
            strategy_id=strategy_id, **sim_params(strategy_id),
        )
        rows = (payload.get("protect_latency") or [])[:safe_limit]
    else:
        rows = db.protect_latency_rows(
            strategy_id=strategy_id, engine=engine, limit=safe_limit,
        )
        if days and days > 0:
            cutoff = (date.today() - timedelta(days=int(days))).isoformat()
            rows = [r for r in rows
                    if (r.get("group_received_at") or "")[:10] >= cutoff]
        if since:
            rows = [r for r in rows
                    if (r.get("group_received_at") or "") >= since]
        rows = rows[:safe_limit]
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "engine": engine,
        "stats": summarize_latency(rows),
        "rows": rows,
    }


async def protect_causal_report(db: DB,
                                strategy_id: str = "auto_escada_3_hibrida",
                                limit: int = 500, days: int = 0,
                                since: str = "") -> dict:
    """Replay the hybrid and attribute each protect event chronologically.

    Unlike ``protect_latency_report`` this endpoint is a read-only historical
    calculation: no causal rows are persisted, and the report explicitly
    compares the hybrid against the same mechanical auto without group
    protects.  ``days=0`` means the complete signal history in the DB.
    """
    from app.strategies import sim_params

    safe_limit = max(1, min(int(limit), 500))
    params = sim_params(strategy_id)
    payload = await run_analysis(
        db, group="", days=days, since=since, daily_stop_pct=0,
        strategy_id=strategy_id, include_causal=True, **params,
    )
    report = payload.get("protect_causal") or {
        "signals": 0, "events": 0, "harmful_events": 0,
        "delta_pnl": 0.0, "harmful_delta_pnl": 0.0, "rows": [],
    }
    rows = report.get("rows") or []
    # Keep the signal-level rows for audit, but expose the worst event-level
    # interventions directly so a human can inspect what made the hybrid lose.
    worst = []
    for row in rows:
        for event in row.get("events") or []:
            worst.append({
                "signal_id": row.get("id"),
                "symbol": row.get("symbol"),
                "received_at": row.get("received_at"),
                **event,
            })
    worst.sort(key=lambda event: event.get("incremental_pnl", 0.0))
    flat_events = [event for row in rows for event in (row.get("events") or [])]
    eligible_events = sum(1 for event in flat_events if event.get("eligible"))
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "days": days,
        "since": since,
        "summary": {
            "signals": report.get("signals", 0),
            "events": report.get("events", 0),
            "eligible_events": eligible_events,
            "deferred_events": len(flat_events) - eligible_events,
            "harmful_events": report.get("harmful_events", 0),
            "delta_pnl": report.get("delta_pnl", 0.0),
            "harmful_delta_pnl": report.get("harmful_delta_pnl", 0.0),
        },
        "rows": rows[:safe_limit],
        "worst_events": worst[:safe_limit],
    }


async def run_compare(db: DB, group: str = "", days: int = 30,
                      risk_pct: float = DEFAULT_RISK_PCT,
                      delay: int = DEFAULT_DELAY, spread: float = DEFAULT_SPREAD,
                      slippage: float = DEFAULT_SLIPPAGE,
                      commission: float = DEFAULT_COMMISSION,
                      balance: Optional[float] = None,
                      since: str = "",
                      daily_stop_pct: Optional[float] = None) -> dict:
    """A/B: full ladder (TP1..TP5) vs full ladder + group management vs
    "fechar tudo no TP2". Returns all payloads plus a winner verdict.

    Runs the analysis multiple times on the same signals/candles — never
    writes to the DB, never opens trades. `since` (YYYY-MM-DD) filtra o
    período (ex.: só o formato atual); `daily_stop_pct` modela o kill
    switch diário (None = config).
    """
    new = await run_analysis(db, group=group, days=days, risk_pct=risk_pct,
                             delay=delay, spread=spread, slippage=slippage,
                             commission=commission, balance=balance, max_legs=None,
                             since=since, daily_stop_pct=daily_stop_pct)
    managed = await run_analysis(db, group=group, days=days, risk_pct=risk_pct,
                                 delay=delay, spread=spread, slippage=slippage,
                                 commission=commission, balance=balance,
                                 max_legs=None, manage=True, since=since,
                                 daily_stop_pct=daily_stop_pct)
    tp2 = await run_analysis(db, group=group, days=days, risk_pct=risk_pct,
                             delay=delay, spread=spread, slippage=slippage,
                             commission=commission, balance=balance,
                             max_legs=None, exit_at_tp=2, since=since,
                             daily_stop_pct=daily_stop_pct)

    if not new.get("signals"):
        return {
            "ok": True, "signals": 0, "group": group, "days": days,
            "since": since, "compare": {"winner": "tie", "text": "🤝 Empate — sem sinais no período"},
            "new": new, "manage": managed, "tp2": tp2,
        }

    def _pick(name: str):
        """Which strategy wins on `name` (new / manage / tp2 / None on tie)."""
        vals = {"new": new["aggregate"].get(name),
                "manage": managed["aggregate"].get(name),
                "tp2": tp2["aggregate"].get(name)}
        present = {k: v for k, v in vals.items() if v is not None}
        if not present or len(set(present.values())) == 1:
            return None
        return max(present, key=present.get)

    keys = ["win_rate", "profit_factor", "pnl"]
    scores = {"new": 0, "manage": 0, "tp2": 0, "tie": 0}
    for k in keys:
        winner_k = _pick(k)
        scores[winner_k if winner_k else "tie"] += 1
    dds = {k: v["drawdown"]["amount"] for k, v in
           (("new", new), ("manage", managed), ("tp2", tp2))}
    dd_winner = min(dds, key=dds.get) if len(set(dds.values())) > 1 else "tie"
    scores["drawdown"] = dd_winner
    # winner = highest-scoring non-tie strategy; "manage"/"tp2" break ties
    best = max([k for k in ("manage", "tp2", "new")],
               key=lambda k: (scores[k], k in ("manage", "tp2"), k == "new"))
    winner = best if scores[best] > scores["tie"] else "tie"

    text = {
        "manage": "🏆 TP1..TP5 + gestão do grupo ganha",
        "tp2": "🏆 Fechar tudo no TP2 ganha",
        "new": "🏆 TP1..TP5 (nova) ganha",
        "tie": "🤝 Empate — estratégias equivalentes",
    }[winner]

    return {
        "ok": True, "group": group, "days": days, "signals": new.get("signals", 0),
        "new": new, "manage": managed, "tp2": tp2,
        "compare": {
            "keys": keys,
            "scores": scores,
            "winner": winner,
            "text": text,
        },
    }


def _print_payload(p: dict) -> None:
    agg = p["aggregate"]
    print(f"Grupo: {p['group'] or '(todos)'} | sinais válidos: {p['signals']} | "
          f"saldo: {p['balance']:.0f}")
    print(f"Parâmetros: risco {p['risk_pct']}% | delay {p['delay_seconds']}s | "
          f"spread {p['spread_pips']} pip | slippage {p['slippage_pips']} pip | "
          f"comissão {p['commission_per_lot']}/lote")
    if not p["signals"]:
        print("Sem sinais válidos no período. Confirma que o listener terminou "
              "de carregar o histórico?")
        return
    print("\n==================== RESULTADO ====================")
    print(f"Total: {agg['total']} | sem dados: {agg['no_data']} | "
          f"ainda abertos: {agg['open']} | resolvidos: {agg['resolved']}")
    print(f"Wins: {agg['wins']} | Losses: {agg['losses']} | "
          f"Breakevens: {agg['breakevens']}")
    print(f"Win rate: {agg['win_rate']}%")
    print(f"P&L total: {agg['pnl']:+.2f} (comissão {agg['commission']:.2f})")
    print(f"Avg win: {agg['avg_win']:.2f} | Avg loss: {agg['avg_loss']:.2f} | "
          f"Profit factor: {agg['profit_factor']}")
    print("===================================================")
    print(f"\n{'✅' if p['verdict']['level'] == 'edge' else ('🟡' if p['verdict']['level'] == 'marginal' else ('⚠️' if p['verdict']['level'] == 'small_sample' else '❌'))} "
          f"{p['verdict']['text']}")

async def run_month_sim(db: DB, group: str = "", days: int = 30,
                        risk_pct: float = DEFAULT_RISK_PCT,
                        delay: int = DEFAULT_DELAY,
                        spread: float = DEFAULT_SPREAD,
                        slippage: float = DEFAULT_SLIPPAGE,
                        commission: float = DEFAULT_COMMISSION,
                        balance: Optional[float] = None,
                        strategy_id: Optional[str] = None,
                        since: str = "",
                        until: str = "",
                        daily_stop_pct: Optional[float] = None) -> dict:
    """Simulação completa do último mês (ou N dias): replay dos sinais reais
    desde há `days` dias até hoje, começando com o saldo atual — mostra "se
    tivesses começado há 1 mês com X €, hoje terias Y €" para UMA estratégia.

    `strategy_id` vem do registo central (`app.strategies`): define max_legs,
    manage, exit_at_tp e os parâmetros de auto-gestão (escada/trailing/time
    stop). Default = a estratégia principal. `days <= 0` = histórico
    completo (desde o início do grupo); `since` (YYYY-MM-DD) filtra a partir
    de uma data; `until` (YYYY-MM-DD) filtra até uma data (exclusiva) —
    juntos isolam um período. Reusa `run_analysis` e acrescenta a curva de
    equity acumulada (ordenada por data de receção) para a dashboard a
    desenhar, mais `bankrupt`/`bankrupt_date` (o sim para quando o saldo
    acumulado chega a zero). Nunca escreve na BD.
    """
    from app.strategies import MAIN_STRATEGY_ID, get_strategy, sim_params
    sid = strategy_id or MAIN_STRATEGY_ID
    strat = get_strategy(sid)
    payload = await run_analysis(
        db, group=group, days=days, risk_pct=risk_pct, delay=delay,
        spread=spread, slippage=slippage, commission=commission,
        balance=balance, since=since, until=until,
        daily_stop_pct=daily_stop_pct, strategy_id=sid,
        **sim_params(sid),
    )
    payload["strategy_id"] = sid
    payload["strategy_name"] = strat["nome"]
    if not payload.get("results"):
        payload["equity_curve"] = []
        payload["start_balance"] = payload["balance"]
        payload["end_balance"] = payload["balance"]
        payload["bankrupt"] = False
        return payload

    ordered = sorted(payload["results"], key=lambda r: r.get("received_at", ""))
    run = 0.0
    curve = []
    bankrupt = False
    bankrupt_date = ""
    start_bal = float(payload["balance"])
    for r in ordered:
        if r["outcome"] not in ("win", "loss", "be"):
            continue
        run += r["pnl"]
        curve.append({
            "date": (r.get("received_at") or "")[:10],
            "symbol": r.get("symbol", ""),
            "pnl": round(run, 2),
        })
        # falência: o saldo acumulado chegou a zero — o sim "morre" aqui
        if start_bal + run <= 0 and not bankrupt:
            bankrupt = True
            bankrupt_date = (r.get("received_at") or "")[:10]
    payload["equity_curve"] = curve
    payload["start_balance"] = round(start_bal, 2)
    payload["end_balance"] = round(start_bal + (payload.get("aggregate") or {}).get("pnl", 0.0), 2)
    payload["bankrupt"] = bankrupt
    payload["bankrupt_date"] = bankrupt_date
    return payload


async def trace_signal(db: DB, strategy_id: str = "tp2", group: str = "",
                        days: int = 0, since: str = "") -> dict:
    """Exemplo do fluxo real de uma trade para a página de teste: corre o
    backtest da estratégia, pega no sinal resolvido MAIS RECENTE e re-simula
    esse sinal com trace — devolve os eventos cronológicos (SL / TP1 / BE /
    fecho no TP) + o caminho de preços, para a dashboard desenhar o gráfico
    e a linha do tempo. Nunca escreve na BD.

    Só faz sentido para estratégias sem gestão do grupo (o trace usa o
    `simulate_signal` puro — tp2, nova).
    """
    from app.strategies import get_strategy, sim_params
    strat = get_strategy(strategy_id)
    balance = float(db.all_config().get("balance", 10000) or 10000)
    payload = await run_analysis(
        db, group=group, days=days, since=since, balance=balance,
        daily_stop_pct=0, **sim_params(strategy_id),
    )
    resolved = sorted(
        [r for r in payload.get("results", [])
         if r.get("outcome") in ("win", "loss", "be")],
        key=lambda r: r.get("received_at", ""), reverse=True)
    if not resolved:
        return {"ok": True, "found": False, "strategy_id": strategy_id}

    interval, range_ = ("15m", "1mo") if (days and days > 0) else ("1h", "730d")
    provider = YahooProvider()
    candles = None  # fetch único por símbolo (todos os sinais são XAUUSD)
    by_id = {s.id: s for s in db.signals_since(payload.get("since") or "")}
    chosen = None
    fallback = None
    # procura o sinal resolvido mais recente com um caminho rico (≥ 4 candles)
    # para o exemplo mostrar a sequência completa (SL/TP1/BE/TP2) — o sinal
    # de hoje costuma resolver-se no próprio candle e não ensina nada
    for r in resolved:
        signal = by_id.get(r.get("id"))
        if signal is None:
            continue
        if candles is None:
            candles = await provider.history(signal.symbol, interval=interval, range_=range_)
        res = simulate_signal(
            signal, candles, payload["risk_pct"], balance,
            delay_seconds=payload["delay_seconds"], spread_pips=payload["spread_pips"],
            slippage_pips=payload["slippage_pips"],
            commission_per_lot=payload["commission_per_lot"],
            max_legs=strat["max_legs"], exit_at_tp=strat["exit_at_tp"],
            lots_override=strat["lots_override"], trace=True,
            atr_k=strat.get("atr_k"), atr_period=int(strat.get("atr_period") or 14),
        )
        if fallback is None:
            fallback = (signal, res)
        if len(res.get("path", [])) >= 4:
            chosen = (signal, res)
            break
    else:
        chosen = fallback
    if chosen is None:
        return {"ok": True, "found": False, "strategy_id": strategy_id}
    signal, res = chosen
    path = res.get("path", [])
    # downsample para a dashboard (≤ 90 pontos, mantém o primeiro e o último)
    step = max(1, len(path) // 90)
    path = path[::step]
    if path and path[-1] != res.get("path", [])[-1]:
        path.append(res["path"][-1])
    tps = {i: float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0)
           for i in range(1, 6) if float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0) > 0}
    return {
        "ok": True, "found": True, "strategy_id": strategy_id,
        "strategy_name": strat["nome"],
        "signal": {
            "symbol": signal.symbol, "direction": signal.direction,
            "entry": signal.entry, "stop_loss": signal.stop_loss,
            "take_profits": tps, "received_at": signal.received_at,
        },
        "outcome": res["outcome"], "pnl": res["pnl"],
        "events": res.get("events", []), "path": path,
    }


async def real_vs_backtest(db: DB, strategy_id: Optional[str] = None) -> dict:
    """Compara cada trade REAL fechada da conta demo com o backtest do mesmo
    sinal na estratégia principal (SPEC v2 §3.4, ligação backtest↔vivo):

    - **slippage de entrada**: preço real de abertura vs o fill do sim
      (entrada do sinal ± spread/2 ± slippage — a mesma fórmula do
      `simulate_signal`);
    - **diferença de saída**: fecho real (média ponderada das pernas) vs o
      nível esperado pela razão do fecho (TP_k ou SL da perna);
    - **timing**: atraso real sinal→abertura vs o `delay_seconds` do sim;
    - **P&L**: real vs o backtest do mesmo sinal (mesma estratégia).

    Só devolve trades fechadas reais (tabela `trades` — a conta demo); vazio
    enquanto não houver. Nunca escreve na BD."""
    from app.backtest import _pip_size
    from app.strategies import get_strategy, sim_params
    from datetime import datetime

    main = strategy_id or str(db.all_config().get("strategy", "nova_gestao"))
    strat = get_strategy(main)
    params = sim_params(main)
    trades = [t for t in db.list_trades(500) if t.status == "closed"]
    if not trades:
        return {"ok": True, "trades": [], "count": 0, "strategy_id": main}

    balance = float(db.all_config().get("balance", 10000) or 10000)
    risk_pct = float(db.all_config().get("risk_pct") or DEFAULT_RISK_PCT)
    delay = DEFAULT_DELAY
    spread = DEFAULT_SPREAD
    slippage = DEFAULT_SLIPPAGE
    commission = DEFAULT_COMMISSION
    # backtest da estratégia principal no histórico completo (sem kill diário
    # — aqui comparamos sinal a sinal, não o agregado) para o P&L do sim
    bt_by_id: Dict = {}
    try:
        payload = await run_analysis(
            db, days=0, risk_pct=risk_pct, balance=balance, delay=delay,
            spread=spread, slippage=slippage, commission=commission,
            daily_stop_pct=0, **params,
        )
        bt_by_id = {r.get("id"): r for r in payload.get("results", [])}
    except Exception:
        bt_by_id = {}  # sem candles/rede — degrada para comparação de preços

    by_signal = {s.id: s for s in db.signals_since("")}
    out: List[dict] = []
    for t in trades:
        signal = by_signal.get(t.signal_id)
        pip = _pip_size(t.symbol)
        # fill do backtest: entrada do sinal piorada por spread/2 + slippage
        if signal:
            s_fill = (
                signal.entry + spread * pip / 2 + slippage * pip
                if signal.direction == "buy"
                else signal.entry - spread * pip / 2 - slippage * pip
            )
        else:
            s_fill = None
        entry_diff = round(t.entry - s_fill, 4) if s_fill is not None else None
        # saída real: média ponderada dos close_price das pernas
        positions = db.positions_for_trade(t.id)
        closed_pos = [p for p in positions if p.close_price]
        real_close = None
        if closed_pos:
            lots = sum(p.lots for p in closed_pos)
            real_close = (round(sum(p.close_price * p.lots for p in closed_pos) / lots, 4)
                          if lots else None)
        # diferença de saída por perna vs o nível esperado pela razão do fecho
        exit_diffs = []
        for p in positions:
            expected = None
            if p.exit_reason and p.exit_reason.startswith("tp"):
                expected = p.take_profit
            elif p.exit_reason == "sl":
                expected = p.stop_loss
            if expected and p.close_price:
                exit_diffs.append(round(p.close_price - expected, 4))
        # atraso real sinal→abertura vs o delay do sim
        delay_sec = None
        if signal and signal.received_at and t.opened_at:
            try:
                t0 = datetime.fromisoformat(signal.received_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(t.opened_at.replace("Z", "+00:00"))
                delay_sec = int((t1 - t0).total_seconds())
            except (ValueError, TypeError):
                pass
        bt = bt_by_id.get(t.signal_id) if t.signal_id else None
        out.append({
            "trade_id": t.id, "signal_id": t.signal_id,
            "symbol": t.symbol, "direction": t.direction,
            "received_at": signal.received_at if signal else None,
            "opened_at": t.opened_at, "closed_at": t.closed_at,
            "real_entry": t.entry, "bt_entry_fill": s_fill,
            "entry_diff": entry_diff,
            "entry_diff_pips": round(entry_diff / pip, 2) if entry_diff is not None else None,
            "real_close": real_close,
            "exit_diff_avg": (round(sum(exit_diffs) / len(exit_diffs), 4)
                              if exit_diffs else None),
            "exit_reason": t.exit_reason,
            "real_pnl": round(t.pnl or 0.0, 2),
            "bt_pnl": round(float(bt.get("pnl") or 0.0), 2) if bt else None,
            "bt_outcome": bt.get("outcome") if bt else None,
            "delay_seconds": delay_sec, "bt_delay_seconds": delay,
            "delay_diff": (delay_sec - delay) if delay_sec is not None else None,
        })
    out.sort(key=lambda r: (r.get("closed_at") or ""), reverse=True)
    return {"ok": True, "trades": out[:50], "count": len(out), "strategy_id": main}


def _weighted_close(positions) -> Optional[float]:
    """Fecho médio ponderado pelos lotes das pernas já fechadas."""
    closed = [p for p in positions if getattr(p, "close_price", 0.0)]
    if not closed:
        return None
    lots = sum(p.lots for p in closed)
    if not lots:
        return None
    return round(sum(p.close_price * p.lots for p in closed) / lots, 4)


async def paper_vs_real_auto(db: DB, strategy_id: Optional[str] = None) -> dict:
    """Fase 3 — compara a trade REAL de uma estratégia `auto_*` (quando
    promovida a principal e a correr no MT5) com a trade de PAPEL da MESMA
    estratégia no mesmo sinal (a corrida paper continua a correr em paralelo):

    - **mesma decisão de saída** (exit_reason de cada lado);
    - **Δ preço de fecho** (média ponderada das pernas real vs papel, em
      pips — fills reais vs sim);
    - **Δ timing** (instante de fecho real vs papel, em minutos);
    - **P&L** de cada lado.

    Vazio até a conta demo fechar trades de uma auto (Fase 3 arrancada).
    Nunca escreve na BD."""
    from app.backtest import _pip_size
    from app.strategies import HIDDEN_STRATEGIES, STRATEGIES

    autos = [sid for sid in STRATEGIES if sid.startswith("auto_")
             and sid not in HIDDEN_STRATEGIES]
    if strategy_id:
        autos = [s for s in autos if s == strategy_id]
    if not autos:
        return {"ok": True, "pairs": [], "count": 0}

    real_trades = [t for t in db.list_trades(500)
                   if t.status == "closed" and t.signal_id]
    if not real_trades:
        return {"ok": True, "pairs": [], "count": 0}

    by_signal = {s.id: s for s in db.signals_since("")}
    out: List[dict] = []
    for sid in autos:
        closed_paper = {pt["signal_id"]: pt for pt in db.paper_trades(sid, 500)
                        if pt.get("status") == "closed" and pt.get("signal_id")}
        open_paper = {pt["signal_id"]: pt for pt in db.paper_open_trades(sid)
                      if pt.get("signal_id")}
        for t in real_trades:
            pt = closed_paper.get(t.signal_id) or open_paper.get(t.signal_id)
            if not pt:
                continue
            paper_open = pt.get("status") == "open"
            pip = _pip_size(t.symbol)
            real_close = _weighted_close(db.positions_for_trade(t.id))
            paper_close = (_weighted_close(db.paper_positions_for_trade(pt["id"]))
                           if not paper_open else None)
            price_diff = ((real_close - paper_close) / pip if real_close is not None
                          and paper_close is not None else None)
            # Δ timing: papel vs real (min) — positivo = o papel fechou depois
            timing_diff = None
            if pt.get("closed_at") and t.closed_at:
                try:
                    from datetime import datetime
                    t_real = datetime.fromisoformat(str(t.closed_at).replace("Z", "+00:00"))
                    t_paper = datetime.fromisoformat(str(pt["closed_at"]).replace("Z", "+00:00"))
                    timing_diff = round((t_paper - t_real).total_seconds() / 60.0, 1)
                except (ValueError, TypeError):
                    pass
            signal = by_signal.get(t.signal_id)
            real_pnl = round(t.pnl or 0.0, 2)
            paper_pnl = round(float(pt.get("pnl") or 0.0), 2)
            same_exit = (not paper_open
                         and (pt.get("exit_reason") or None) == (t.exit_reason or None))
            out.append({
                "signal_id": t.signal_id, "symbol": t.symbol,
                "direction": t.direction, "strategy_id": sid,
                "received_at": signal.received_at if signal else None,
                "real_closed_at": t.closed_at,
                "paper_closed_at": pt.get("closed_at"),
                "real_exit": t.exit_reason, "paper_exit": pt.get("exit_reason"),
                "paper_open": paper_open, "same_exit": same_exit,
                "real_close": real_close, "paper_close": paper_close,
                "price_diff_pips": round(price_diff, 2) if price_diff is not None else None,
                "real_pnl": real_pnl, "paper_pnl": paper_pnl,
                "pnl_diff": round(real_pnl - paper_pnl, 2),
                "timing_diff_min": timing_diff,
            })
    out.sort(key=lambda r: (r.get("real_closed_at") or ""), reverse=True)
    return {"ok": True, "pairs": out[:50], "count": len(out)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Analisa o edge dos sinais reais")
    parser.add_argument("--group", default="Golden Moves",
                        help="substring do nome do grupo (default: Golden Moves)")
    parser.add_argument("--days", type=int, default=30, help="janela em dias")
    parser.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY)
    parser.add_argument("--spread", type=float, default=DEFAULT_SPREAD)
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    parser.add_argument("--commission", type=float, default=DEFAULT_COMMISSION)
    args = parser.parse_args()

    db = DB(DB_PATH)
    payload = asyncio.run(run_analysis(
        db, group=args.group, days=args.days, risk_pct=args.risk_pct,
        delay=args.delay, spread=args.spread, slippage=args.slippage,
        commission=args.commission,
    ))
    _print_payload(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
