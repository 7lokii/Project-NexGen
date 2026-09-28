"""Deterministic evidence reports for forward strategy validation.

The functions in this module are read-only: they inspect a DB and return plain
JSON-compatible dictionaries. They never promote a strategy, change config, or
send an order.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import csv
import io
import math
import random
from typing import Iterable, Optional


FORWARD_STRATEGIES = ("auto_escada_3", "auto_escada_5")
FORWARD_GATE_DEFAULTS = {
    "period_days": 30,
    "min_trades": 20,
    "min_profit_factor": 1.5,
    "max_drawdown_pct": 125.0,
    # amostra mínima da referência para o gate ser comparável: 10 sinais em
    # comum bastam para a comparação de P&L/saída (descritiva, nunca promove
    # sozinha) — 20 atrasava a decisão sem acrescentar robustez ao gate
    "min_reference_trades": 10,
    "max_pnl_lag_pct": 10.0,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: str) -> Optional[datetime]:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _period_bounds(days: int = 30, start: str = "", end: str = "") -> tuple[str, str]:
    right = _iso(end) or _utc_now()
    left = _iso(start) or (right - timedelta(days=max(1, int(days))))
    return left.isoformat(timespec="seconds"), right.isoformat(timespec="seconds")


def bootstrap_mean_ci(values: Iterable[float], *, iterations: int = 1000,
                      seed: int = 0) -> Optional[dict]:
    """Return a reproducible percentile CI for mean P&L, or None for <2 rows."""
    data = [float(value) for value in values]
    if len(data) < 2:
        return None
    rng = random.Random(seed)
    means = []
    n = len(data)
    for _ in range(max(100, int(iterations))):
        means.append(sum(data[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    low = means[max(0, int(len(means) * 0.025) - 1)]
    high = means[min(len(means) - 1, int(len(means) * 0.975))]
    return {"mean": round(sum(data) / n, 4),
            "low": round(low, 4), "high": round(high, 4),
            "confidence": 0.95, "n": n}


def forward_evidence_gate(pnls: Iterable[float], *, min_trades: int = 20,
                          max_drawdown_pct: Optional[float] = None) -> dict:
    """Conservative, descriptive gate; never promotes a strategy itself."""
    data = [float(value) for value in pnls]
    equity = peak = 0.0
    drawdown = 0.0
    for pnl in data:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    ci = bootstrap_mean_ci(data)
    enough = len(data) >= int(min_trades)
    positive_ci = bool(ci and ci["low"] > 0)
    dd_ok = max_drawdown_pct is None or (
        peak > 0 and drawdown / peak * 100.0 <= float(max_drawdown_pct)
    )
    return {
        "n": len(data),
        "min_trades": int(min_trades),
        "enough_trades": enough,
        "mean_ci95": ci,
        "positive_mean_ci95": positive_ci,
        "drawdown_amount": round(drawdown, 4),
        "drawdown_limit_ok": dd_ok,
        "eligible": enough and positive_ci and dd_ok,
    }


def _profit_factor(pnls: list[float]) -> Optional[float]:
    wins = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    return round(wins / losses, 4) if losses else (None if not wins else math.inf)


def _drawdown(pnls: list[float], starting_balance: float = 1000.0) -> dict:
    equity = float(starting_balance)
    peak = equity
    amount = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        amount = max(amount, peak - equity)
    return {
        "amount": round(amount, 2),
        "pct_start": round(amount / starting_balance * 100.0, 2)
        if starting_balance > 0 else None,
        "ending_balance": round(equity, 2),
    }


def _event_payloads(db, start: str, end: str) -> list[dict]:
    return db.execution_events(since=start, until=end, limit=20000)


def journal_quality_report(db, *, day: str = "", start: str = "", end: str = "") -> dict:
    """Summarize one UTC day of journal completeness and broker telemetry.

    A trade is complete when the journal has an opening fill for every DB leg
    and, for every leg that is already closed, a closing fill. Signal fields are
    joined from ``signals`` by the tape exporter, so a separate signal event is
    not required in the runtime journal.
    """
    if day:
        start = f"{day}T00:00:00+00:00"
        end = f"{day}T23:59:59.999999+00:00"
    else:
        start, end = _period_bounds(1, start, end)
    events = _event_payloads(db, start, end)
    by_kind = Counter(str(row.get("kind") or "") for row in events)
    structured_signals = [signal for signal in db.list_signals(200000)
                          if start <= (signal.received_at or "") <= end
                          and signal.status not in {"management", "historical"}]
    journal_signal_ids = {str(row.get("signal_id") or "") for row in events
                          if row.get("signal_id")}
    retcodes = Counter()
    accepted = rejected = 0
    latencies = {"signal_to_order_seconds": [], "signal_to_fill_seconds": [],
                 "order_to_fill_ms": [], "latency_ms": []}
    for row in events:
        payload = row.get("payload") or {}
        if row.get("kind") in ("order_result", "modify_result"):
            code = payload.get("retcode")
            if code is not None:
                retcodes[str(code)] += 1
            if payload.get("accepted") is True:
                accepted += 1
            elif payload.get("accepted") is False:
                rejected += 1
        for key in latencies:
            value = payload.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                latencies[key].append(float(value))

    def latency_stats(values: list[float]) -> dict:
        if not values:
            return {"count": 0, "mean": None, "median": None, "p90": None, "max": None}
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "mean": round(sum(ordered) / len(ordered), 4),
            "median": round(ordered[len(ordered) // 2], 4),
            "p90": round(ordered[max(0, int(len(ordered) * 0.9) - 1)], 4),
            "max": round(ordered[-1], 4),
        }

    event_trade_ids = {str(row.get("trade_id") or "") for row in events}
    trades = [trade for trade in db.list_trades(200000)
              if (start <= (trade.opened_at or "") <= end
                  or (trade.closed_at and start <= trade.closed_at <= end)
                  or trade.id in event_trade_ids)]
    event_by_trade = {}
    for row in events:
        tid = str(row.get("trade_id") or "")
        if tid:
            event_by_trade.setdefault(tid, []).append(row)
    incomplete = []
    complete = 0
    for trade in trades:
        positions = db.positions_for_trade(trade.id)
        rows = event_by_trade.get(trade.id, [])
        opened = {int((r.get("payload") or {}).get("leg") or 0)
                  for r in rows if r.get("kind") == "fill_open"}
        closed = {int((r.get("payload") or {}).get("leg") or 0)
                  for r in rows if r.get("kind") == "fill_close"}
        expected_open = {int(pos.leg) for pos in positions}
        expected_close = {int(pos.leg) for pos in positions if pos.status == "closed"}
        missing_open = sorted(expected_open - opened)
        missing_close = sorted(expected_close - closed)
        if missing_open or missing_close:
            incomplete.append({
                "trade_id": trade.id, "signal_id": trade.signal_id,
                "symbol": trade.symbol, "status": trade.status,
                "missing_open_legs": missing_open,
                "missing_close_legs": missing_close,
            })
        else:
            complete += 1
    return {
        "ok": True,
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "enabled": str(db.all_config().get("execution_journal_enabled", "false")).lower() in {"1", "true", "yes", "on"},
        "events": {"total": len(events), "by_kind": dict(by_kind)},
        "signals": {"received": len(structured_signals),
                    "structured": sum(1 for signal in structured_signals
                                      if signal.entry and signal.stop_loss and signal.take_profit_1 and signal.take_profit_2),
                    "referenced_by_journal": sum(1 for signal in structured_signals
                                                   if signal.id in journal_signal_ids)},
        "management": by_kind.get("management", 0),
        "fills": {"open": by_kind.get("fill_open", 0), "close": by_kind.get("fill_close", 0)},
        "orders": {"accepted": accepted, "rejected": rejected,
                   "retcodes": dict(retcodes)},
        "latency": {key: latency_stats(value) for key, value in latencies.items()},
        "trades": {"observed": len(trades), "complete_tape": complete,
                   "incomplete_tape": len(incomplete), "incomplete": incomplete[:100]},
    }


def worker_outage_timeline(db, *, days: int = 30, start: str = "",
                            end: str = "") -> dict:
    """Worker-outage timeline derived from the opt-in journal (read-only).

    Uma falha prolongada do worker da lane (delete de ordem pendente a falhar
    tick após tick) deixa na fita duas marcas passivas: as falhas
    `pending_order_result` (action=delete, accepted=false) a cada retry e o
    `pending_delete_stuck` na escalada/re-alerta (a cada 30 min enquanto
    presa). Este relatório junta-as cronologicamente e agrupa episódios por
    (trade, ticket) — uma ordem pendente que o bot tentou cancelar e não
    conseguiu — para o operador ver a linha temporal da falha sem re-consultar
    a BD. Nunca participa no replay nem autoriza nada: é observação passiva.
    Resolução do episódio: um `pending_order_result` delete aceite ou um
    `fill_open` da mesma perna DEPOIS da 1ª falha (a ordem preencheu em vez de
    ser cancelada); sem isso o episódio fica aberto (ou a resolução não foi
    journaled — ex.: cancelamento de gestão completado no `_cancel_pending_pos`
    sem falha prévia)."""
    start, end = _period_bounds(days, start, end)
    events = _event_payloads(db, start, end)
    enabled = str(db.all_config().get(
        "execution_journal_enabled", "false")).lower() in {"1", "true", "yes", "on"}
    timeline = []
    fills_by_leg = {}
    # 1ª passada: marcas e fills (para atribuir ticket/leg/lane)
    for row in events:
        kind = str(row.get("kind") or "")
        payload = row.get("payload") or {}
        at = str(row.get("at") or "")
        if kind == "pending_delete_stuck":
            timeline.append({
                "at": at, "kind": "pending_delete_stuck",
                "lane": payload.get("lane") or "",
                "trade_id": str(row.get("trade_id") or ""),
                "signal_id": str(row.get("signal_id") or ""),
                "symbol": payload.get("symbol") or row.get("symbol") or "",
                "leg": payload.get("leg"), "ticket": payload.get("ticket"),
                "attempts": payload.get("attempts"),
                "intent_kind": payload.get("intent_kind"),
                "reason": payload.get("reason"),
                "re_alert": bool(payload.get("re_alert")),
                "escalated": bool(payload.get("escalated")),
                "escalated_at": payload.get("escalated_at"),
                "last_alert_utc": payload.get("last_alert_utc"),
                "comment": payload.get("comment"),
            })
        elif kind == "pending_order_result" and payload.get("action") == "delete":
            accepted = payload.get("accepted") is True
            timeline.append({
                "at": at,
                "kind": "delete_ok" if accepted else "delete_failed",
                "lane": "",
                "trade_id": str(row.get("trade_id") or ""),
                "signal_id": str(row.get("signal_id") or ""),
                "symbol": row.get("symbol") or "",
                "leg": None, "ticket": payload.get("ticket"),
                "attempts": payload.get("attempts"),
                "intent_kind": payload.get("intent_kind"),
                "reason": payload.get("reason"),
                "re_alert": False, "escalated": False,
                "escalated_at": None, "last_alert_utc": None,
                "comment": payload.get("comment"),
            })
        elif kind == "fill_open":
            leg = payload.get("leg")
            tid = str(row.get("trade_id") or "")
            if leg is not None and tid:
                fills_by_leg.setdefault(tid, {})[int(leg)] = at
    if not timeline:
        return {
            "ok": True, "period": {"start": start, "end": end, "timezone": "UTC"},
            "enabled": enabled, "summary": {"episodes": 0, "stuck_marks": 0,
                                              "re_alerts": 0, "delete_failures": 0,
                                              "delete_success": 0, "open_episodes": 0},
            "timeline": [], "episodes": [],
        }
    timeline.sort(key=lambda r: (r["at"], r["kind"]))

    # atribui lane/leg às falhas via o ticket da marca da mesma perna
    ticket_ctx = {}
    for row in timeline:
        ticket = row.get("ticket")
        tid = row.get("trade_id")
        if not tid or not ticket:
            continue
        key = (tid, str(ticket))
        if row["kind"] == "pending_delete_stuck":
            ticket_ctx[key] = {
                "lane": row["lane"], "leg": row["leg"],
                "symbol": row["symbol"] or ticket_ctx.get(key, {}).get("symbol", ""),
            }
    for row in timeline:
        ticket = row.get("ticket")
        tid = row.get("trade_id")
        ctx = ticket_ctx.get((tid, str(ticket))) if (tid and ticket) else None
        if ctx and row["kind"].startswith("delete"):
            row["lane"] = row["lane"] or ctx["lane"]
            row["leg"] = row["leg"] if row["leg"] is not None else ctx["leg"]
            row["symbol"] = row["symbol"] or ctx["symbol"]

    # episódios por (trade, ticket): só os que têm >=1 falha ou marca
    episodes = {}
    for row in timeline:
        if row["kind"] not in ("pending_delete_stuck", "delete_failed"):
            continue
        tid = row.get("trade_id") or "?"
        key = (tid, str(row.get("ticket") or "?"))
        ep = episodes.get(key)
        if ep is None:
            ep = {
                "trade_id": row.get("trade_id") or "",
                "signal_id": row.get("signal_id") or "",
                "symbol": row.get("symbol") or "",
                "lane": row.get("lane") or "",
                "leg": row.get("leg"), "ticket": row.get("ticket"),
                "started_at": row["at"], "last_event_at": row["at"],
                "failed_deletes": 0, "marks": 0, "re_alerts": 0,
                "escalated_at": None, "max_attempts": 0,
                "intent_kind": row.get("intent_kind"),
                "resolved": False, "resolution": None,
                "_fill_leg": None,
            }
            episodes[key] = ep
        ep["last_event_at"] = max(ep["last_event_at"], row["at"])
        ep["lane"] = ep["lane"] or row.get("lane") or ""
        ep["symbol"] = ep["symbol"] or row.get("symbol") or ""
        ep["leg"] = ep["leg"] if ep["leg"] is not None else row.get("leg")
        ep["intent_kind"] = row.get("intent_kind") or ep["intent_kind"]
        if row["kind"] == "delete_failed":
            ep["failed_deletes"] += 1
        else:
            ep["marks"] += 1
            if row.get("re_alert"):
                ep["re_alerts"] += 1
            if not ep["escalated_at"]:
                ep["escalated_at"] = row["at"]
            ep["_fill_leg"] = row.get("leg")
        try:
            ep["max_attempts"] = max(ep["max_attempts"],
                                      int(row.get("attempts") or 0))
        except (TypeError, ValueError):
            pass

    # resolução: delete aceite ou fill da perna DEPOIS da 1ª falha/marca
    for row in timeline:
        if row["kind"] not in ("delete_ok",):
            continue
        tid = row.get("trade_id") or "?"
        ep = episodes.get((tid, str(row.get("ticket") or "?")))
        if ep and row["at"] >= ep["started_at"] and not ep["resolved"]:
            ep["resolved"] = True
            ep["resolution"] = "delete_ok"
    for key, ep in episodes.items():
        if ep["resolved"]:
            continue
        leg = ep["_fill_leg"]
        fills = fills_by_leg.get(ep["trade_id"], {})
        fill_at = fills.get(int(leg)) if leg is not None else None
        if fill_at and fill_at >= ep["started_at"]:
            ep["resolved"] = True
            ep["resolution"] = "fill"
    out_eps = []
    for ep in episodes.values():
        row = {k: v for k, v in ep.items() if not k.startswith("_")}
        row["intent_kind"] = row.get("intent_kind") or None
        out_eps.append(row)
    out_eps.sort(key=lambda ep: ep["last_event_at"], reverse=True)
    summary = {
        "episodes": len(out_eps),
        "stuck_marks": sum(1 for r in timeline if r["kind"] == "pending_delete_stuck"),
        "re_alerts": sum(1 for r in timeline
                          if r["kind"] == "pending_delete_stuck" and r["re_alert"]),
        "delete_failures": sum(1 for r in timeline if r["kind"] == "delete_failed"),
        "delete_success": sum(1 for r in timeline if r["kind"] == "delete_ok"),
        "open_episodes": sum(1 for ep in out_eps if not ep["resolved"]),
    }
    return {
        "ok": True, "period": {"start": start, "end": end, "timezone": "UTC"},
        "enabled": enabled, "summary": summary,
        "timeline": timeline, "episodes": out_eps,
    }


def _actual_fill_map(events: list[dict], kind: str) -> dict[int, dict]:
    result = {}
    for row in events:
        if row.get("kind") != kind:
            continue
        payload = row.get("payload") or {}
        try:
            leg = int(payload.get("leg"))
        except (TypeError, ValueError):
            continue
        result[leg] = {"at": row.get("at"), "payload": payload,
                       "sequence": row.get("sequence") or row.get("id")}
    return result


def _recount_implausible_evidence(db, start: str, end: str) -> list[dict]:
    """Recounts implausíveis gravados no journal como evidência externa.

    Cada evento ``recount_implausible`` é um relato do grupo (Signal #N) que
    o candle-check não conseguiu encaixar nos candles reais. Devolve-os no
    mesmo formato do fill-audit ("real" = relato do grupo, "replay" = range
    dos candles) para o operador cruzar quem divergiu sem re-consultar o
    Yahoo. Read-only; nunca participa no replay determinístico.
    """
    events = db.execution_events(since=start, until=end, limit=20000)
    out = []
    for row in events:
        if row.get("kind") != "recount_implausible":
            continue
        payload = row.get("payload") or {}
        signal_id = row.get("signal_id") or ""
        trade_id = row.get("trade_id") or ""
        # cruza com uma trade real fechada do mesmo dia (fill do bot) para
        # distinguir erro de relato vs erro de execução
        day = (row.get("at") or "")[:10]
        entry = float(payload.get("entry") or 0.0)
        direction = payload.get("direction") or ""
        from app.backtest import _pip_size
        symbol = payload.get("symbol") or row.get("symbol") or ""
        pip = _pip_size(symbol) if symbol else 0.0001
        tol = pip * 40
        best = None
        if day:
            trades = [t for t in db.trades_closed_on(day)
                      if t.direction == direction]
            if trades:
                best = min(trades, key=lambda t: abs(t.entry - entry))
                if abs(best.entry - entry) > tol:
                    best = None
        bot_traded = best is not None
        # quando o bot NÃO negociou o sinal, cruza com reinícios do listener
        # e o kill switch à volta da hora do recount para apontar a causa
        # provável da execução perdida (além da divergência do relato)
        miss_reason = None
        restart_kind = None
        restart_at = None
        signals_lost_est = None
        vps_reboot = False
        if not bot_traded and day:
            at_ts = _parse_ts(row.get("at") or "")
            if at_ts:
                # reinício do listener numa janela de ±15 min à volta do evento
                restarts = db.list_listener_restarts(limit=100)
                near = [r for r in restarts
                        if _ts_distance(r.get("started_at") or "", at_ts) <= 900]
                if near:
                    restart = near[0]
                    restart_at = restart.get("started_at") or ""
                    reason = restart.get("reason") or ""
                    # watchdog grava reason com "watchdog"; qualquer outro
                    # motivo (boot.ps1, manual) é reinício externo
                    if "watchdog" in reason.lower():
                        restart_kind = "watchdog"
                    else:
                        restart_kind = "manual"
                    # reinício MANUAL perto do arranque do servidor = reboot
                    # da VPS (boot.ps1 arranca Caddy + MT5 + servidor +
                    # listener em bloco) — confirma com a hora persistida
                    vps_reboot = False
                    if restart_kind == "manual":
                        boot_ts = _parse_ts(_config_str(db, "server_started_at"))
                        r_ts = _parse_ts(restart_at)
                        if boot_ts and r_ts and abs(r_ts - boot_ts) <= 1800:
                            vps_reboot = True
                    # estimativa de sinais perdidos: mensagens do grupo no
                    # intervalo [restart, restart + 60s] — durante o arranque
                    # o listener ainda não está a receber (o heartbeat só
                    # começa depois da auth; o replay do histórico recupera
                    # os sinais, mas a execução ao vivo fica atrasada)
                    try:
                        r_ts = _parse_ts(restart_at)
                        lost = 0
                        if r_ts:
                            for s in db.signals_since(
                                    restart_at[:10] or ""):
                                s_ts = _parse_ts(s.received_at or "")
                                if s_ts is not None and r_ts <= s_ts <= r_ts + 60:
                                    lost += 1
                        signals_lost_est = lost
                    except Exception:
                        signals_lost_est = None
                    kind_label = "watchdog (heartbeat parado)" \
                        if restart_kind == "watchdog" else "manual/externo"
                    reboot_note = (
                        " — provável reboot da VPS (boot.ps1 arrancou o "
                        "servidor na mesma janela)" if vps_reboot else ""
                    )
                    miss_reason = (
                        f"listener reiniciado {kind_label}{reboot_note} à volta "
                        f"da hora do recount ({_fmt_ts(restart_at)}: {reason or '—'})"
                        + (f" — até {signals_lost_est} sinal(is) na janela do "
                           "arranque" if signals_lost_est else "")
                        + " (o replay do histórico recupera as mensagens, "
                          "mas a execução ao vivo atrasa)"
                    )
                else:
                    # kill switch ativo no dia (config persistida)
                    kill_switch = _config_bool(db, "kill_switch")
                    if kill_switch:
                        rearm = _config_str(db, "killswitch_rearm_at")
                        miss_reason = (f"kill switch ATIVO no dia{(' até ' + rearm) if rearm else ''} "
                                       "— o bot não abre ordens novas")
        out.append({
            "at": row.get("at"), "signal_id": signal_id, "trade_id": trade_id,
            "kind": "recount_implausible",
            "message_id": payload.get("message_id"),
            "signal_number": payload.get("signal_number"),
            "direction": direction, "entry": entry,
            "excursion_price": payload.get("excursion_price"),
            "reversal_price": payload.get("reversal_price"),
            "pips": payload.get("pips"),
            "scope": payload.get("scope"),
            "hi": payload.get("hi"), "lo": payload.get("lo"),
            "excursion_ok": payload.get("excursion_ok"),
            "reversal_ok": payload.get("reversal_ok"),
            "window_plausible": payload.get("window_plausible"),
            "bot_trade": {"trade_id": best.id, "entry": best.entry,
                           "opened_at": best.opened_at,
                           "closed_at": best.closed_at}
            if best else None,
            "bot_traded": bot_traded,
            "miss_reason": miss_reason,
            "restart_kind": restart_kind,
            "restart_at": restart_at,
            "signals_lost_est": signals_lost_est,
            "vps_reboot": vps_reboot,
        })
    return out


def _gold_futures_offset(db, candles: list, symbol: str) -> float | None:
    """Offset GC=F↔spot calibrado com os fills reais do bot (R2).

    O feed Yahoo serve GC=F (futuros de ouro) enquanto o grupo negocia spot
    XAUUSD — a divergência é sistemática (dezenas de pontos) e faz o
    candle-check marcar implausíveis com falsos positivos. Este helper
    calibra o offset com as trades REAIS fechadas de XAUUSD: para cada fill
    com entrada e hora conhecidas, compara o preço do candle GC=F nesse
    momento com a entrada spot do bot; a mediana é o offset. Requer >=3
    amostras e |offset| >= 5 pontos para aplicar (evita sobreajuste com
    amostras pequenas); devolve None caso contrário.
    """
    if symbol != "XAUUSD" or not candles:
        return None
    try:
        ts_list = sorted(c["ts"] for c in candles)
        offsets = []
        for trade in db.list_trades(200000):
            if trade.symbol != "XAUUSD" or not trade.entry \
                    or not trade.opened_at:
                continue
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(trade.opened_at).timestamp()
            except (ValueError, TypeError):
                continue
            if ts < ts_list[0] - 900 or ts > ts_list[-1] + 900:
                continue
            # candle que contém a hora do fill
            close = None
            for c in candles:
                if c["ts"] <= ts < c["ts"] + 900:
                    close = c.get("close") or c.get("open")
                    break
            if close:
                offsets.append(float(close) - float(trade.entry))
        if len(offsets) < 3:
            return None
        offsets.sort()
        median = offsets[len(offsets) // 2]
        if abs(median) < 5.0:
            return None
        return round(median, 2)
    except Exception as exc:
        print(f"[recount] calibração do offset gold falhou: {exc}")
        return None


def _parse_ts(value: str):
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(value)
        return dt.timestamp() if dt.tzinfo else dt.replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _ts_distance(a: str, b_ts: float):
    ats = _parse_ts(a)
    return abs(ats - b_ts) if ats is not None else float("inf")


def _fmt_ts(value: str) -> str:
    return (value or "")[:19].replace("T", " ")


def _config_value(db, key: str) -> str:
    with db.tx() as cur:
        row = cur.execute(
            "SELECT value FROM config WHERE key = ?", (key,),
        ).fetchone()
    return str(row["value"] or "") if row else ""


def _config_bool(db, key: str) -> bool:
    return _config_value(db, key).strip().lower() in ("1", "true", "yes")


def _config_str(db, key: str) -> str:
    return _config_value(db, key)


def compare_broker_fills(db, *, days: int = 30, start: str = "", end: str = "",
                         trade_id: str = "", limit: int = 50) -> dict:
    """Compare journaled broker fills with the isolated deterministic replay.

    The first mismatch is reported in chronological tape order. This function
    only reads production rows and calls ``run_replay`` with temporary DBs;
    broker observations never become replay commands.
    """
    from app.execution.replay import run_replay

    start, end = _period_bounds(days, start, end)
    all_events = _event_payloads(db, start, end)
    trade_ids = {str(row.get("trade_id") or "") for row in all_events if row.get("trade_id")}
    if trade_id:
        trade_ids = {trade_id}
    rows = []
    trades = {trade.id: trade for trade in db.list_trades(200000)}
    for tid in sorted(trade_ids):
        trade = trades.get(tid)
        if not trade:
            continue
        tape = db.execution_tape(trade_id=tid, limit=20000)
        events = tape.get("events", [])
        if not any(row.get("kind") in ("fill_open", "fill_close") for row in events):
            continue
        strategy_id = trade.strategy_id if trade.strategy_id in {"nova", "nova_gestao"} else "nova_gestao"
        replay = run_replay(events, strategy_id=strategy_id, include_snapshots=False)
        signal_id = trade.signal_id
        expected = ((replay.get("engines") or {}).get("mt5", {}).get("signals") or {}).get(signal_id, {})
        decisions = {int(row[0]): row for row in expected.get("decisions", [])}
        actual_events = [row for row in events if row.get("kind") in ("fill_open", "fill_close")]
        actual_open = _actual_fill_map(actual_events, "fill_open")
        actual_close = _actual_fill_map(actual_events, "fill_close")
        first = None
        comparisons = []
        actual_source = set()
        signal_data = db.get_signal(signal_id)
        replay_entry = float(signal_data.entry) if signal_data else float(trade.entry)
        for pos in sorted(db.positions_for_trade(tid), key=lambda item: item.leg):
            open_fill = actual_open.get(pos.leg)
            close_fill = actual_close.get(pos.leg)
            for fill in (open_fill, close_fill):
                if fill:
                    actual_source.add(str(fill["payload"].get("source") or "unknown"))
            expected_decision = decisions.get(int(pos.leg))
            expected_close = expected_decision[2] if expected_decision else None
            expected_reason = expected_decision[3] if expected_decision else None
            actual_open_price = (open_fill or {}).get("payload", {}).get("price")
            actual_close_price = (close_fill or {}).get("payload", {}).get("price")
            diffs = []
            if open_fill is None:
                diffs.append(("fill_open", None, replay_entry, "missing_broker_fill"))
            elif actual_open_price is not None and abs(float(actual_open_price) - replay_entry) > 1e-9:
                diffs.append(("open_price", actual_open_price, replay_entry, "price"))
            if pos.status == "closed":
                if close_fill is None:
                    diffs.append(("fill_close", None, expected_close, "missing_broker_fill"))
                elif expected_close is not None and actual_close_price is not None and abs(float(actual_close_price) - float(expected_close)) > 1e-9:
                    diffs.append(("close_price", actual_close_price, expected_close, "price"))
                if close_fill and expected_reason and pos.exit_reason and pos.exit_reason != expected_reason:
                    diffs.append(("exit_reason", pos.exit_reason, expected_reason, "reason"))
            for field, actual, expected_value, kind in diffs:
                if first is None:
                    first = {"trade_id": tid, "signal_id": signal_id, "leg": pos.leg,
                             "field": field, "kind": kind, "actual": actual,
                             "replay": expected_value,
                             "sequence": (close_fill or open_fill or {}).get("sequence")}
            comparisons.append({
                "leg": pos.leg, "actual_open": actual_open_price,
                "replay_open": replay_entry, "actual_close": actual_close_price,
                "replay_close": expected_close, "actual_reason": pos.exit_reason,
                "replay_reason": expected_reason, "differences": [
                    {"field": field, "actual": actual, "replay": expected_value, "kind": kind}
                    for field, actual, expected_value, kind in diffs
                ],
            })
        rows.append({
            "trade_id": tid, "signal_id": signal_id, "symbol": trade.symbol,
            "opened_at": trade.opened_at, "closed_at": trade.closed_at,
            "source": sorted(actual_source), "equal": first is None,
            "first_divergence": first, "legs": comparisons,
            "replay_parity": replay.get("parity"),
        })
        if len(rows) >= max(1, min(int(limit), 200)):
            break
    divergences = [row for row in rows if not row["equal"]]
    # Divergências de EVIDÊNCIA externa: os recounts implausíveis gravados no
    # journal (kind=recount_implausible) são relatos do grupo que não cabem
    # nos candles reais — listá-los aqui cruza o relato com a fita do bot
    # (quem divergiu: o grupo ou a execução), sem re-consultar o Yahoo.
    recount_evidence = _recount_implausible_evidence(db, start, end)
    return {
        "ok": True,
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "trades": rows, "count": len(rows),
        "divergent": len(divergences),
        "equal": len(rows) - len(divergences),
        "first_divergence": next((row["first_divergence"] for row in rows if row["first_divergence"]), None),
        "recount_evidence": recount_evidence,
        "recount_evidence_count": len(recount_evidence),
        "mode": "read-only production journal + temporary replay DB",
    }


def _shadow_rows(db, strategy_id: str, start: str, end: str) -> list[dict]:
    return [row for row in db.paper_trades(strategy_id, 100000, origin="shadow")
            if row.get("status") == "closed" and start <= (row.get("closed_at") or row.get("opened_at") or "") <= end]


def _gate_strategy(rows: list[dict], reference_by_signal: dict[str, dict], *, strategy_id: str) -> dict:
    pnls = [float(row.get("pnl") or 0.0) for row in rows]
    pf = _profit_factor(pnls)
    dd = _drawdown(pnls)
    ci = bootstrap_mean_ci(pnls)
    common = [row for row in rows if row.get("signal_id") in reference_by_signal]
    ref_pnls = [float(reference_by_signal[row["signal_id"]].get("pnl") or 0.0) for row in common]
    ref_pf = _profit_factor(ref_pnls)
    ref_total = sum(ref_pnls)
    pnl_total = sum(pnls)
    same_exit = sum(
        1 for row in common
        if row.get("exit_reason") == reference_by_signal[row["signal_id"]].get("exit_reason")
    )
    pnl_lag_limit = abs(ref_total) * FORWARD_GATE_DEFAULTS["max_pnl_lag_pct"] / 100.0
    gates = {
        "enough_trades": len(rows) >= FORWARD_GATE_DEFAULTS["min_trades"],
        "profit_factor": pf is not None and pf >= FORWARD_GATE_DEFAULTS["min_profit_factor"],
        "positive_mean_ci95": bool(ci and ci["low"] > 0),
        "drawdown": dd["pct_start"] is not None and dd["pct_start"] <= FORWARD_GATE_DEFAULTS["max_drawdown_pct"],
        "reference_comparable": len(common) >= FORWARD_GATE_DEFAULTS["min_reference_trades"],
        "not_more_than_10pct_behind_reference": pnl_total >= ref_total - pnl_lag_limit,
    }
    return {
        "strategy_id": strategy_id, "closed": len(rows), "pnl": round(pnl_total, 2),
        "profit_factor": None if pf == math.inf else pf,
        "wins": sum(1 for pnl in pnls if pnl > 0),
        "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100, 2) if pnls else 0.0,
        "mean_pnl_ci95": ci, "drawdown": dd,
        "reference": {"closed_common": len(common), "pnl": round(ref_total, 2),
                      "profit_factor": None if ref_pf == math.inf else ref_pf,
                      "same_exit": same_exit, "same_exit_rate": round(same_exit / len(common) * 100, 2) if common else None},
        "gates": gates, "eligible": all(gates.values()),
        "verdict": "pass" if all(gates.values()) else "insufficient_or_fail",
        "promotion": "manual_only",
    }


def forward_gate_report(db, *, days: int = 30, start: str = "", end: str = "",
                        strategies: Iterable[str] = FORWARD_STRATEGIES) -> dict:
    """Evaluate shadow auto strategies in a frozen period; never promotes."""
    configured_start = str(db.all_config().get("forward_evidence_started_at") or "")
    if not start and configured_start:
        start = configured_start
    start, end = _period_bounds(days, start, end)
    target_end = None
    collection_status = "rolling"
    if configured_start:
        started = _iso(start)
        if started:
            target_end = (started + timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
            now_iso = _utc_now().isoformat(timespec="seconds")
            if end > target_end:
                end = target_end
            collection_status = "complete" if now_iso >= target_end else "collecting"
    # Referência HÍBRIDA da nova_gestao: a corrida race (a principal real,
    # só enche quando ela executa) + a conta shadow (isolada, abre em cada
    # sinal completo mesmo com o kill switch ligado). Por sinal a race tem
    # precedência (é o que a principal realmente fez); o shadow cobre os
    # sinais em que a principal não negociou. Isto desacopla o gate do kill
    # switch e não atrasa a decisão à espera de 20 trades reais.
    ref_race = {str(row.get("signal_id")): row
                for row in db.paper_trades("nova_gestao", 100000, origin="race")
                if row.get("status") == "closed"
                and start <= (row.get("closed_at") or row.get("opened_at") or "") <= end}
    ref_shadow = {str(row.get("signal_id")): row
                  for row in db.paper_trades("nova_gestao", 100000, origin="shadow")
                  if row.get("status") == "closed"
                  and start <= (row.get("closed_at") or row.get("opened_at") or "") <= end}
    reference_by_signal = {**ref_shadow, **ref_race}  # race ganha por sinal
    reports = [_gate_strategy(_shadow_rows(db, sid, start, end), reference_by_signal, strategy_id=sid)
               for sid in strategies]
    return {
        "ok": True, "period": {"start": start, "end": end,
                                  "target_end": target_end,
                                  "status": collection_status,
                                  "timezone": "UTC"},
        "policy": {**FORWARD_GATE_DEFAULTS,
                    "configured_start": configured_start or None,
                    "no_auto_promotion": True,
                    "shadow_origin": "shadow",
                    "reference_origin": "race+shadow (híbrida, race ganha por sinal)",
                    "reference_strategy": "nova_gestao"},
        "reference": {"strategy_id": "nova_gestao",
                      "closed": len(reference_by_signal),
                      "race_closed": len(ref_race),
                      "shadow_closed": len(ref_shadow),
                      "pnl": round(sum(float(row.get("pnl") or 0)
                                       for row in reference_by_signal.values()), 2)},
        "strategies": reports,
        "verdict": "pass" if reports and any(row["eligible"] for row in reports) else "collect_more_evidence",
    }


def audit_report(db, *, days: int = 30, start: str = "", end: str = "") -> dict:
    """Build one exportable, self-contained audit bundle for a UTC window."""
    start, end = _period_bounds(days, start, end)
    return {
        "ok": True,
        "generated_at": _utc_now().isoformat(timespec="seconds"),
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "journal_quality": journal_quality_report(db, start=start, end=end),
        "fill_audit": compare_broker_fills(db, start=start, end=end),
        "forward_gate": forward_gate_report(db, days=days, start=start, end=end),
    }


def audit_csv(report: dict) -> str:
    """Flatten the audit bundle into a portable semicolon CSV."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    period = report.get("period") or {}
    writer.writerow(["section", "metric", "value", "details"])
    q = report.get("journal_quality") or {}
    writer.writerow(["journal", "period_start", period.get("start", ""), ""])
    writer.writerow(["journal", "period_end", period.get("end", ""), ""])
    writer.writerow(["journal", "events_total", (q.get("events") or {}).get("total", 0), ""])
    writer.writerow(["journal", "signals_received", (q.get("signals") or {}).get("received", 0), ""])
    writer.writerow(["journal", "management", q.get("management", 0), ""])
    writer.writerow(["journal", "fill_open", (q.get("fills") or {}).get("open", 0), ""])
    writer.writerow(["journal", "fill_close", (q.get("fills") or {}).get("close", 0), ""])
    writer.writerow(["journal", "tapes_incomplete", (q.get("trades") or {}).get("incomplete_tape", 0), ""])
    orders = q.get("orders") or {}
    writer.writerow(["journal", "orders_accepted", orders.get("accepted", 0), ""])
    writer.writerow(["journal", "orders_rejected", orders.get("rejected", 0), ""])
    for row in (report.get("fill_audit") or {}).get("trades", []):
        first = row.get("first_divergence") or {}
        writer.writerow(["fill_audit", row.get("trade_id", ""),
                         "equal" if row.get("equal") else "divergent",
                         f"{first.get('field', '')}: actual={first.get('actual', '')}; replay={first.get('replay', '')}"])
    for row in (report.get("forward_gate") or {}).get("strategies", []):
        gates = row.get("gates") or {}
        failed = ",".join(key for key, value in gates.items() if not value)
        writer.writerow(["forward_gate", row.get("strategy_id", ""),
                         row.get("verdict", ""), f"N={row.get('closed', 0)}; PF={row.get('profit_factor', '')}; failed={failed}"])
    return out.getvalue()
