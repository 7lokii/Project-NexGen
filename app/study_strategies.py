"""Estudos read-only das estratégias e da cadência do motor paper.

Nada neste módulo altera a configuração, a BD de produção ou abre ordens MT5.
Os estudos históricos usam o mesmo ``run_analysis``/backtest da dashboard;
o benchmark de polling usa uma BD temporária e uma trajetória determinística.

Uso:
    .venv/Scripts/python -m app.study_strategies --study ablation
    .venv/Scripts/python -m app.study_strategies --study walkforward
    .venv/Scripts/python -m app.study_strategies --study legs
    .venv/Scripts/python -m app.study_strategies --study fallback
    .venv/Scripts/python -m app.study_strategies --study polling
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import tempfile
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from app.analysis import run_analysis
from app.config import DB_PATH
from app.db import DB
from app.execution.clock import ManualClock
from app.execution.paper import PaperRace
from app.execution.market import MarketSimulator
from app.models import Signal


MGMT_KINDS = ("protect", "close_all", "move_sl", "tp_hit")
POLLING_INTERVALS = (1.0, 0.25, 0.1)
LEG_WEIGHT_VARIANTS = {
    "equal": None,
    # Slightly more risk nas pernas longas, sem mudar o risco total.
    "tp345_light": {1: 0.85, 2: 0.85, 3: 1.05, 4: 1.05, 5: 1.20},
}


def _metrics(payload: dict) -> dict:
    agg = payload.get("aggregate") or {}
    dd = payload.get("drawdown") or {}
    return {
        "signals": int(payload.get("signals") or 0),
        "resolved": int(agg.get("resolved") or 0),
        "wins": int(agg.get("wins") or 0),
        "losses": int(agg.get("losses") or 0),
        "win_rate": agg.get("win_rate"),
        "profit_factor": agg.get("profit_factor"),
        "pnl": round(float(agg.get("pnl") or 0.0), 2),
        "drawdown_pct": dd.get("pct"),
        "drawdown_amount": dd.get("amount"),
        "resolution": payload.get("resolution"),
        "daily_stop_skipped": payload.get("daily_stop_skipped", 0),
    }


def _common_kwargs(db: DB, *, group: str, days: int,
                   risk_pct: Optional[float], balance: Optional[float]) -> dict:
    cfg = db.all_config()
    return {
        "db": db,
        "group": group,
        "days": days,
        "risk_pct": risk_pct,
        "balance": balance,
        "daily_stop_pct": float(cfg.get("daily_loss_limit_pct") or 0),
        "resolution": "15m" if 0 < days <= 30 else "1h",
    }


async def management_ablation(db: DB, group: str = "Golden Moves",
                              days: int = 30,
                              risk_pct: Optional[float] = None,
                              balance: Optional[float] = 1000.0) -> dict:
    """Compare gestão nenhuma, cada kind isolado e todos os kinds."""
    base = _common_kwargs(db, group=group, days=days,
                          risk_pct=risk_pct, balance=balance)
    variants = {
        "none": {"manage": False},
        "all": {"manage": True, "manage_kinds": None},
    }
    variants.update({kind: {"manage": True, "manage_kinds": {kind}}
                     for kind in MGMT_KINDS})
    out = {}
    for name, params in variants.items():
        payload = await run_analysis(**base, **params)
        out[name] = _metrics(payload)
        out[name]["management_applied_signals"] = int(
            payload.get("mgmt_applied") or 0
        )
    return {
        "ok": True,
        "study": "management_ablation",
        "group": group,
        "days": days,
        "variants": out,
        "method": "mesmos sinais, candles, custos, risco e kill diário; apenas os kinds de gestão mudam",
    }


def _valid_signals(db: DB, group: str, days: int,
                   since: str = "", until: str = "") -> list:
    since = since or ((date.today() - timedelta(days=days)).isoformat()
                      if days > 0 else "")
    return [s for s in db.signals_since(since)
            if group.lower() in (s.source or "").lower()
            and s.symbol and s.direction and s.entry and not s.error
            and (not until or (s.received_at or "")[:10] < until)]


def _chunks(items: list, n: int) -> List[list]:
    if not items:
        return []
    n = max(2, min(int(n), len(items)))
    size = max(1, math.ceil(len(items) / n))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _variant_params(name: str) -> dict:
    if name == "none":
        return {"manage": False}
    if name == "all":
        return {"manage": True, "manage_kinds": None}
    if name == "all_fallback_60":
        return {
            "manage": True, "manage_kinds": None,
            "management_fallback_seconds": 60,
            "fallback_buffer_fraction": 0.10,
        }
    return {"manage": True, "manage_kinds": {name}}


async def calibrated_walkforward(db: DB, group: str = "Golden Moves",
                                days: int = 30, buckets: int = 4,
                                risk_pct: Optional[float] = None,
                                balance: Optional[float] = 1000.0,
                                since: str = "", until: str = "") -> dict:
    """Calibrate a management policy on prior buckets and validate the next.

    The selector sees only the train interval. Ties prefer ``all`` and then
    ``none``; this avoids promoting a single management message on a tiny
    sample. This complements (rather than replaces) the existing descriptive
    ``/api/analysis/walkforward`` report.
    """
    signals = _valid_signals(db, group, days, since=since, until=until)
    chunks = _chunks(signals, buckets)
    if len(chunks) < 2:
        return {"ok": True, "study": "calibrated_walkforward",
                "signals": len(signals), "folds": [], "summary": None}

    variants = ("none", "all", "all_fallback_60", *MGMT_KINDS)
    since_all = since or (signals[0].received_at or "")[:10]
    fold_rows = []
    for i in range(1, len(chunks)):
        train_start = since_all
        train_end = (chunks[i - 1][-1].received_at or "")[:10]
        test_start = (chunks[i][0].received_at or "")[:10]
        test_end = ((chunks[i + 1][0].received_at or "")[:10]
                    if i + 1 < len(chunks) else "")
        if train_end == test_start:
            # ``until`` is date-exclusive; if both signals share a day, use
            # the next chunk boundary and preserve chronological separation.
            train_end = test_start
        train_scores = {}
        for name in variants:
            train = await run_analysis(
                **_common_kwargs(db, group=group, days=days,
                                 risk_pct=risk_pct, balance=balance),
                **_variant_params(name), since=train_start, until=test_start,
            )
            train_scores[name] = _metrics(train)
        eligible = [name for name in variants
                    if train_scores[name]["resolved"] >= 3]
        selected = max(
            eligible or list(variants),
            key=lambda name: (
                float(train_scores[name]["profit_factor"] or -1.0),
                train_scores[name]["pnl"],
                1 if name == "all" else 0,
            ),
        )
        test = await run_analysis(
            **_common_kwargs(db, group=group, days=days,
                             risk_pct=risk_pct, balance=balance),
            **_variant_params(selected), since=test_start, until=test_end,
        )
        fold_rows.append({
            "fold": i,
            "train": {"since": train_start, "until": test_start,
                      "variants": train_scores},
            "selected": selected,
            "test": {"since": test_start, "until": test_end,
                     "metrics": _metrics(test)},
        })
    test_rows = [row["test"]["metrics"] for row in fold_rows]
    profitable = sum(1 for row in test_rows if row["pnl"] > 0)
    return {
        "ok": True,
        "study": "calibrated_walkforward",
        "group": group,
        "days": days,
        "signals": len(signals),
        "folds": fold_rows,
        "summary": {
            "folds": len(fold_rows),
            "profitable_test_folds": profitable,
            "all_test_folds_profitable": bool(test_rows)
            and profitable == len(test_rows),
            "selected": [row["selected"] for row in fold_rows],
        },
        "method": "cada fold escolhe apenas no passado; a avaliação usa o bloco seguinte",
    }


async def leg_weight_study(db: DB, group: str = "Golden Moves",
                           days: int = 30,
                           risk_pct: Optional[float] = None,
                           balance: Optional[float] = 1000.0) -> dict:
    """Testa distribuição ligeiramente maior nas pernas TP3..TP5."""
    base = _common_kwargs(db, group=group, days=days,
                          risk_pct=risk_pct, balance=balance)
    out = {}
    for name, weights in LEG_WEIGHT_VARIANTS.items():
        payload = await run_analysis(
            **base, max_legs=None, manage=False, leg_weights=weights,
        )
        out[name] = _metrics(payload)
        out[name]["weights"] = weights or "equal"
    return {
        "ok": True, "study": "leg_weights", "group": group, "days": days,
        "variants": out,
        "method": "o risco total é normalizado e apenas redistribuído entre as 5 pernas",
    }


async def management_fallback_study(db: DB, group: str = "Golden Moves",
                                    days: int = 30,
                                    risk_pct: Optional[float] = None,
                                    balance: Optional[float] = 1000.0) -> dict:
    """Testa fallback mecânico após TP1 sem mensagem.

    A recomendação é deliberadamente apenas informativa: resultados idênticos
    não justificam ligar uma proteção nova em produção.
    """
    base = _common_kwargs(db, group=group, days=days,
                          risk_pct=risk_pct, balance=balance)
    out = {}
    for seconds in (None, 60, 300, 900):
        payload = await run_analysis(
            **base, manage=True, manage_kinds=None,
            management_fallback_seconds=seconds,
            fallback_buffer_fraction=0.10,
        )
        name = "human_only" if seconds is None else f"fallback_{seconds}s"
        out[name] = _metrics(payload)
        out[name]["fallback_applied"] = sum(
            1 for row in payload.get("results", [])
            if row.get("management_fallback_applied")
        )
    return {
        "ok": True, "study": "management_fallback", "group": group,
        "days": days, "variants": out,
        "method": "a proteção mecânica só sobe o SL das pernas restantes após TP1 e sem gestão posterior",
        "recommendation": {
            "id": "fallback_300s",
            "status": "study_only",
            "reason": "Não houve diferença nos resultados desta amostra; manter desligado até haver eventos sem gestão posterior.",
        },
    }


def _period_spec(period: str) -> dict:
    """Map a named validation horizon to explicit, non-leaking bounds."""
    period = (period or "current").lower()
    if period == "current":
        return {"name": "current_30d", "days": 30,
                "since": "", "until": ""}
    if period == "90":
        return {"name": "recent_90d", "days": 90,
                "since": "", "until": ""}
    if period == "old":
        return {"name": "old_oos", "days": 0,
                "since": "2026-04-01", "until": "2026-07-28"}
    if period == "all":
        return {"name": "full_history", "days": 0,
                "since": "", "until": ""}
    raise ValueError(f"período desconhecido: {period}")


def _price_path() -> List[Tuple[float, float]]:
    """Trajetória curta determinística com spike dentro de um segundo."""
    return [
        (0.00, 4400.00), (0.10, 4402.10), (0.20, 4406.20),
        (0.35, 4405.00), (0.60, 4402.00), (0.90, 4400.80),
        (1.20, 4404.20), (1.50, 4406.10), (2.00, 4399.00),
    ]


def _interpolate(path: List[Tuple[float, float]], at: float) -> float:
    if at <= path[0][0]:
        return path[0][1]
    for (t0, p0), (t1, p1) in zip(path, path[1:]):
        if at <= t1:
            fraction = (at - t0) / (t1 - t0)
            return p0 + (p1 - p0) * fraction
    return path[-1][1]


def polling_sensitivity(strategy_ids: Iterable[str] = ("auto_escada_3", "auto_escada_5")) -> dict:
    """Compare polling rates on the same deterministic price trajectory.

    This is a motor sensitivity test, not a month backtest: the paper engine
    is sampled at each cadence and therefore deliberately exposes what a
    polling-only engine can miss between observations. Broker-side SL/TP in
    MT5 remains a separate protection layer.
    """
    path = _price_path()
    duration = path[-1][0]
    signal = Signal(
        id="polling-study", source="study", raw_text="",
        symbol="XAUUSD", direction="buy", entry=4400.0,
        stop_loss=4396.0, take_profit_1=4402.0, take_profit_2=4404.0,
        take_profit_3=4406.0, take_profit_4=4408.0, take_profit_5=4410.0,
        status="historical", received_at="2026-08-19T10:00:00+00:00",
    )
    results = {}
    for interval in POLLING_INTERVALS:
        per_strategy = {}
        for sid in strategy_ids:
            path_db = tempfile.mktemp(suffix=".db")
            db = DB(path_db)
            try:
                market = MarketSimulator(seed=1)
                market.prices["XAUUSD"] = signal.entry
                start_epoch = signal.received_at
                from datetime import datetime, timezone
                clock = ManualClock(
                    datetime.fromisoformat(start_epoch).replace(
                        tzinfo=timezone.utc,
                    ).timestamp()
                )
                race = PaperRace(db, market, clock=clock)
                trades = race.open_for_signal(
                    signal, risk_pct=1.0, strategy_ids=[sid],
                    daily_stop_pct=0,
                )
                if not trades:
                    per_strategy[sid] = {"status": "not_opened"}
                    continue
                steps = int(math.ceil(duration / interval))
                previous_at = 0.0
                for step in range(1, steps + 1):
                    at = min(duration, step * interval)
                    clock.advance(at - previous_at)
                    previous_at = at
                    market.prices["XAUUSD"] = _interpolate(path, at)
                    race.tick(1.0, now=clock.iso())
                trade = db.paper_trades(sid, 20)[0]
                positions = db.paper_positions_for_trade(trade["id"])
                per_strategy[sid] = {
                    "status": trade["status"],
                    "exit_reason": trade.get("exit_reason"),
                    "pnl": round(float(trade.get("pnl") or 0.0), 2),
                    "closed_legs": sum(p.status == "closed" for p in positions),
                    "positions": len(positions),
                }
            finally:
                db.close()
                try:
                    os.remove(path_db)
                except OSError:
                    pass
        results[str(interval)] = per_strategy
    reference = results[str(min(POLLING_INTERVALS))]
    differences = []
    for sid in strategy_ids:
        rows = [results[str(interval)].get(sid, {})
                for interval in POLLING_INTERVALS]
        if any((row.get("exit_reason"), row.get("pnl"), row.get("status"))
               != (reference.get("exit_reason"), reference.get("pnl"), reference.get("status"))
               for row in rows):
            differences.append(sid)
    return {
        "ok": True,
        "study": "paper_polling",
        "intervals_seconds": list(POLLING_INTERVALS),
        "reference_seconds": min(POLLING_INTERVALS),
        "results": results,
        "different_from_reference": differences,
        "method": "trajetória determinística; não é evidência de P&L mensal nem altera o loop de produção",
    }


async def run_study(db: DB, study: str, group: str, days: int,
                    risk_pct: Optional[float], balance: Optional[float],
                    period: str = "current") -> dict:
    if study == "ablation":
        return await management_ablation(db, group, days, risk_pct, balance)
    if study == "walkforward":
        spec = _period_spec(period)
        report = await calibrated_walkforward(
            db, group, spec["days"], 4, risk_pct, balance,
            since=spec["since"], until=spec["until"],
        )
        report["period"] = spec
        return report
    if study == "legs":
        return await leg_weight_study(db, group, days, risk_pct, balance)
    if study == "fallback":
        return await management_fallback_study(db, group, days, risk_pct, balance)
    if study == "polling":
        return polling_sensitivity()
    raise ValueError(f"study desconhecido: {study}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Estudos read-only das estratégias NexGen")
    parser.add_argument("--study", choices=("ablation", "walkforward", "legs", "fallback", "polling"), required=True)
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--risk-pct", type=float, default=None)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--period", choices=("current", "90", "all", "old"),
                        default="current",
                        help="horizonte do walk-forward: atual, 90 dias, todo ou antigo OOS")
    args = parser.parse_args()
    if args.study == "polling":
        result = polling_sensitivity()
    else:
        db = DB(DB_PATH)
        try:
            result = asyncio.run(run_study(
                db, args.study, args.group, args.days,
                args.risk_pct, args.balance, args.period,
            ))
        finally:
            db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
