"""Read-only operational audits and short-horizon strategy studies."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.execution.evidence import _period_bounds


def _iso(value: str) -> Optional[datetime]:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _in_period(value: str, start: str, end: str) -> bool:
    return bool(value) and start <= value <= end


def _signal_counts(db, start: str, end: str) -> dict:
    counts = Counter()
    total = 0
    for signal in db.list_signals(200000):
        if not _in_period(signal.received_at or "", start, end):
            continue
        total += 1
        status = signal.status or "unknown"
        if status in {"management", "historical"}:
            continue
        if signal.error:
            counts["parse_failed"] += 1
        elif status in {"executed", "closed"}:
            counts["executed"] += 1
        elif status == "pending":
            counts["pending"] += 1
        elif status in {"failed", "rejected"}:
            counts[status] += 1
        elif status == "ignored":
            counts["ignored"] += 1
        else:
            counts["other"] += 1
    return {"received": total, **dict(counts)}


def _trade_rows(db, start: str, end: str, lane: str = "") -> list:
    rows = []
    for trade in db.list_trades(200000):
        if not (_in_period(trade.opened_at or "", start, end)
                 or _in_period(trade.closed_at or "", start, end)):
            continue
        if lane and (getattr(trade, "lane_id", None) or "main") != lane:
            continue
        rows.append(trade)
    return rows


def _be_plus_block(db, closed) -> dict:
    """BE+ (tp2_protect): saídas protegidas (stop acima/abaixo da entrada)
    do período + contrafactual honesto "o BE clássico teria corrido até TP2?".

    O counterfactual usa velas (spot MT5 quando ligado, GC=F Yahoo como
    fallback) no intervalo [stop_out, fecho da trade]: `kept_running_to_tp2`
    conta as trades cujo TP2 foi tocado DEPOIS da saída BE+ — i.e. o BE
    clássico (na entrada, mais atrás) teria sobrevivido e colhido o TP2.
    `checked=False` quando não há velas: a auditoria nunca inventa
    plausibilidade (mesma convenção dos recounts).
    """
    exits_total = 0
    exits_with_profit = 0
    stop_events = []
    for trade in closed:
        if (trade.strategy_id or "") != "tp2_protect":
            continue
        entry = float(trade.entry or 0.0)
        if entry <= 0:
            continue
        positions = _closed_positions(db, trade.id)
        for pos in positions:
            reason = pos.exit_reason or ""
            if reason != "trail":
                continue
            sl = float(pos.stop_loss or 0.0)
            if sl <= 0 or abs(sl - entry) <= 1e-9:
                continue  # BE clássico, não BE+
            protected = (sl > entry if trade.direction == "buy" else sl < entry)
            if not protected:
                continue
            exits_total += 1
            pnl = float(pos.pnl or 0.0)
            if pnl > 0:
                exits_with_profit += 1
            stop_events.append({
                "trade_id": trade.id, "symbol": trade.symbol,
                "leg": pos.leg, "pnl": round(pnl, 2),
                "stop_loss": sl, "entry": entry,
                "closed_at": pos.closed_at or trade.closed_at or "",
                "trade_closed_at": trade.closed_at or "",
                "_trade": trade,  # consumido pelo candle-check, nunca serializado
            })

    return {
        "exits_total": exits_total,
        "exits_with_profit": exits_with_profit,
        "exits_with_loss": exits_total - exits_with_profit,
        "kept_running_to_tp2": None,  # candle-check (async) preenche a seguir
        "checked": 0,
        "stop_events": stop_events[:20],
    }


def _be_plus_counterfactual(candles: Optional[list], stop_events: list) -> dict:
    """Contrafactual do BE+ com velas: para cada saída protegida, o TP2 da
    trade foi tocado DEPOIS do stop-out (dentro da vida da trade)?"""
    kept = 0
    checked = 0
    if not candles:
        return {"kept_running_to_tp2": None, "checked": 0}
    from app.analysis import _to_epoch
    ordered = sorted(candles, key=lambda c: c["ts"])
    for ev in stop_events:
        trade = ev.get("_trade")
        if trade is None:
            continue
        tp2 = float(getattr(trade, "take_profit_2", 0.0) or 0.0)
        start = _iso(ev.get("closed_at") or "")
        end = _iso(ev.get("trade_closed_at") or "")
        if not tp2 or start is None or end is None or end <= start:
            continue
        start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
        window = [c for c in ordered
                  if start_ts <= c["ts"] <= end_ts + 900]  # +1 vela M15
        if not window:
            continue
        checked += 1
        high = max(c["high"] for c in window)
        low = min(c["low"] for c in window)
        touched = high >= tp2 if trade.direction == "buy" else low <= tp2
        if touched:
            kept += 1
    return {"kept_running_to_tp2": kept, "checked": checked}


def _closed_positions(db, trade_id: str) -> list:
    try:
        return [p for p in db.positions_for_trade(trade_id)
                if p.status == "closed"]
    except Exception:
        return []


def daily_execution_audit(db, *, day: str = "", start: str = "", end: str = "",
                          lane: str = "") -> dict:
    """Return one reconciled view of signals, trades, journal and broker state.

    ``lane`` (main|tp2) narrows the trade rows to one execution account; the
    signal/journal sections stay global (signals are shared by both lanes).
    """
    if day:
        start = f"{day}T00:00:00+00:00"
        end = f"{day}T23:59:59.999999+00:00"
    else:
        start, end = _period_bounds(1, start, end)

    trades = _trade_rows(db, start, end, lane=lane)
    closed = [trade for trade in trades if trade.status == "closed"]
    open_trades = [trade for trade in trades if trade.status == "open"]
    pnl = round(sum(float(trade.pnl or 0.0) for trade in closed), 2)
    by_reason = Counter(trade.exit_reason or "unknown" for trade in closed)
    by_mode = Counter(trade.execution_mode or "unknown" for trade in trades)
    by_strategy = Counter(trade.strategy_id or "unknown" for trade in trades)
    by_lane = Counter()
    for trade in trades:
        if trade.status == "closed":
            by_lane[(getattr(trade, "lane_id", None) or "main")] += \
                round(float(trade.pnl or 0.0), 2)

    period = {"start": start, "end": end, "timezone": "UTC", "lane": lane or "all"}

    be_plus = _be_plus_block(db, closed)

    ignored = 0
    with db.tx() as cur:
        try:
            ignored = int(cur.execute(
                "SELECT COUNT(*) FROM ignored_messages WHERE received_at >= ? AND received_at <= ?",
                (start, end),
            ).fetchone()[0])
        except Exception:
            ignored = 0

    journal = {}
    try:
        from app.execution.evidence import journal_quality_report
        journal = journal_quality_report(db, start=start, end=end)
    except Exception as exc:
        journal = {"ok": False, "error": str(exc)}

    # The endpoint is also used while the server is in MT5 dry-run. When the
    # caller provides the live state, broker positions can be reconciled here;
    # otherwise keep the result honest instead of pretending DB rows are MT5.
    broker = None
    if broker_state := getattr(db, "_audit_broker_state", None):
        broker = broker_state

    cfg = db.all_config()
    daily_limit_pct = float(cfg.get("daily_loss_limit_pct") or 0.0)
    # The configured operational balance is the stable risk reference. It is
    # intentionally separate from the live broker balance, which is supplied
    # by the API layer when available.
    reference_balance = float(cfg.get("balance") or cfg.get("initial_balance") or 1000.0)
    limit_amount = reference_balance * daily_limit_pct / 100.0 if daily_limit_pct > 0 else 0.0
    realized = round(sum(float(t.pnl or 0.0) for t in closed), 2)

    broker_reconciliation = {"status": "not_available", "account": broker}
    if broker:
        broker_positions = broker.get("positions") or []
        db_open = {trade.id for trade in open_trades}
        broker_reconciliation = {
            "status": "observed",
            "account": {key: broker.get(key) for key in
                        ("login", "server", "currency", "balance", "equity")},
            "broker_open_positions": len(broker_positions),
            "db_open_trades": len(db_open),
            "unmatched_broker_positions": len(broker_positions) if broker_positions else 0,
        }

    return {
        "ok": True,
        "period": period,
        "signals": {**_signal_counts(db, start, end), "ignored_messages": ignored},
        "be_plus": be_plus,
        "trades": {
            "total": len(trades), "closed": len(closed), "open": len(open_trades),
            "pnl": pnl, "by_exit_reason": dict(by_reason),
            "by_execution_mode": dict(by_mode), "by_strategy": dict(by_strategy),
            "by_lane": None if lane else dict(by_lane),
        },
        "orders": journal.get("orders", {}),
        "journal": {
            "enabled": journal.get("enabled", False),
            "events": journal.get("events", {}),
            "fills": journal.get("fills", {}),
            "incomplete_tape": (journal.get("trades") or {}).get("incomplete_tape", 0),
        },
        "daily_kill": {
            "limit_pct": daily_limit_pct,
            "reference_balance": round(reference_balance, 2),
            "limit_amount": round(limit_amount, 2),
            "realized_pnl": realized,
            "triggered": bool(limit_amount and realized <= -limit_amount),
            "auto_kill": str(cfg.get("auto_kill", "")).lower() in {"1", "true", "yes", "on"},
            "note": "reference_balance vem da config; em MT5 o saldo/equity do broker é a fonte de verdade",
        },
        "reconciliation": {
            "db_trades": len(trades),
            "closed_pnl": pnl,
            "unmatched_broker_positions": broker_reconciliation.get("unmatched_broker_positions"),
            "broker": broker_reconciliation,

            "status": "broker_observed" if broker else ("broker_check_required" if any(t.execution_mode == "mt5-real" for t in trades) else "db_only"),
            "warning": None if broker else "MT5 não é consultado por este helper; use /api/diagnostics para saldo/equity broker",
        },
    }


async def recent_strategy_comparison(db, *, since: str = "2026-08-28", group: str = "Golden Moves", balance: float = 1000.0, risk_pct: float = 1.0) -> dict:
    """Run the existing read-only backtest for the recent signal cohort."""
    from app.analysis import run_analysis
    from app.study_strategies import _metrics

    until = ""
    variants = {
        "nova_gestao": {"manage": True, "manage_kinds": None, "max_legs": None},
        "tp2_protect": {"manage": True, "manage_kinds": {"protect"}, "max_legs": 2},
        "auto_escada_3": {"manage": False, "max_legs": 3},
    }
    rows = {}
    for name, params in variants.items():
        payload = await run_analysis(
            db, group=group, days=0, since=since, until=until,
            risk_pct=risk_pct, balance=balance, daily_stop_pct=0,
            resolution="15m", **params,
        )
        rows[name] = _metrics(payload)
    return {
        "ok": True, "study": "recent_strategy_comparison",
        "period": {"since": since, "until": until or None, "timezone": "UTC"},
        "starting_balance": balance, "risk_pct": risk_pct,
        "variants": rows,
        "method": "replay read-only dos mesmos sinais/candles/custos; não altera config nem abre ordens",
    }
