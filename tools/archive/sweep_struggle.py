"""Sweep da regra "struggle" (R4) — grelha struggle_ae_pips × struggle_fraction.

A regra (conselho do admin "close the trade in profit / take 50% / make
risk-free"): ANTES de o TP1 bater, se o preço andou contra a entrada por
`struggle_ae_pips` e depois volta à entrada, a trade "sofreu" — fecha
`struggle_fraction` das pernas a breakeven e move o SL das restantes para a
entrada (fica risk-free).

Este sweep corre a grelha nas auto-escadas (`auto_escada_3` e
`auto_escada_5`) em DUAS janelas:

  * **atual** — formato atual do grupo (since `2026-07-28`, velas 15m);
  * **tudo** — TODO o histórico na BD (desde o início do grupo, velas 1h).

A baseline é a estratégia sem struggle (`struggle_ae_pips=None`). Uma
combinação só tem edge se melhorar PF/P&L/DD nas duas janelas sem sobreajuste
(pico isolado). Nunca escreve na BD nem abre trades.

Uso:
    .venv/Scripts/python -m app.sweep_struggle [--ae 5,10,15,20,25] \\
        [--fraction 0.25,0.5,0.75] [--group "Golden Moves"]
"""

import argparse
import asyncio
import os
import sys
from typing import List, Tuple

from app.analysis import DEFAULT_COMMISSION, DEFAULT_DELAY, DEFAULT_SLIPPAGE, DEFAULT_SPREAD
from app.backtest import (aggregate, apply_daily_stop, drawdown_from_results,
                          simulate_signal)
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider
from app.strategies import get_strategy

SWEEP_IDS = ["auto_escada_3", "auto_escada_5"]
CURRENT_SINCE = "2026-07-28"
DEFAULT_AE = (5.0, 10.0, 15.0, 20.0, 25.0)
DEFAULT_FRACTION = (0.25, 0.5, 0.75)


def _load_signals(db: DB, group: str, since: str) -> List:
    return [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
    ]


async def _candles_for(provider: YahooProvider, signals: List,
                       interval: str = "15m", range_: str = "1mo") -> dict:
    out = {}
    for symbol in {s.symbol for s in signals}:
        try:
            out[symbol] = await provider.history(symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[sweep-struggle] sem histórico {interval}/{range_} para {symbol}: {exc}")
    return out


def _run(signals: List, candles: dict, sid: str, ae: float, fraction: float,
         risk: float, balance: float, daily_stop: float) -> dict:
    strat = get_strategy(sid)
    results = []
    for s in signals:
        cs = candles.get(s.symbol)
        if not cs:
            continue
        res = simulate_signal(
            s, cs, risk, balance, DEFAULT_DELAY, DEFAULT_SPREAD,
            DEFAULT_SLIPPAGE, DEFAULT_COMMISSION,
            max_legs=strat["max_legs"], exit_at_tp=strat["exit_at_tp"],
            lots_override=strat["lots_override"],
            trail_factor=strat["trail_factor"],
            time_stop_minutes=strat["time_stop_minutes"],
            stair_levels=strat["stair_levels"], stair_from=strat["stair_from"],
            confirm_filter=strat["confirm_filter"],
            def_window=strat["def_window"], def_trail=strat["def_trail"],
            def_time_stop=strat["def_time_stop"], def_risk_pct=strat["def_risk_pct"],
            struggle_ae_pips=ae, struggle_fraction=fraction)
        results.append({"id": s.id, "received_at": s.received_at, **res})
    apply_daily_stop(results, balance, daily_stop)
    agg = aggregate(results)
    dd = drawdown_from_results(results, balance)
    applied = sum(1 for r in results if r.get("struggle_applied"))
    return {"n": agg["resolved"], "pf": agg["profit_factor"], "pnl": agg["pnl"],
            "wr": agg["win_rate"], "dd_pct": dd["pct"], "applied": applied}


def _fmt(r: dict) -> str:
    pf = f"{r['pf']:.2f}" if r["pf"] is not None else " — "
    return (f"PF {pf:>5} · {r['pnl']:>+9.2f} € · DD {r['dd_pct']:>5.1f}% "
            f"(n={r['n']} · struggle {r['applied']})")


async def sweep(db: DB, group: str = "", ae_vals: Tuple[float, ...] = DEFAULT_AE,
                fraction_vals: Tuple[float, ...] = DEFAULT_FRACTION) -> dict:
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("SWEEP_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)

    cur_signals = _load_signals(db, group, CURRENT_SINCE)
    all_signals = _load_signals(db, group, "")
    print(f"[sweep-struggle] atual ({CURRENT_SINCE} → hoje): {len(cur_signals)} sinais | "
          f"tudo: {len(all_signals)} | risco {risk}% · saldo {balance:.0f} € · kill {daily_stop}%")

    provider = YahooProvider()
    cur_candles = await _candles_for(provider, cur_signals, "15m", "1mo")
    all_candles = await _candles_for(provider, all_signals, "1h", "730d")

    print("\n── SWEEP STRUGGLE (baseline = sem regra) ──")
    header = f"  {'combinação':<30} | {'ATUAL':<44} | TUDO"
    for sid in SWEEP_IDS:
        strat = get_strategy(sid)
        print(f"\n── {sid} · {strat['nome']} · max_legs={strat['max_legs']} · "
              f"escada={strat['stair_levels']} · confirm={strat['confirm_filter']} ──")
        print(header)
        baseline_cur = _run(cur_signals, cur_candles, sid, None, 0.5,
                            risk, balance, daily_stop)
        baseline_all = _run(all_signals, all_candles, sid, None, 0.5,
                            risk, balance, daily_stop)
        print(f"  {'baseline (sem struggle)':<30} | {_fmt(baseline_cur):<44} | {_fmt(baseline_all)}")
        for ae in ae_vals:
            for fraction in fraction_vals:
                combo = f"ae={ae:>4.0f} · frac={fraction:.2f}"
                c = _run(cur_signals, cur_candles, sid, ae, fraction,
                         risk, balance, daily_stop)
                a = _run(all_signals, all_candles, sid, ae, fraction,
                         risk, balance, daily_stop)
                print(f"  {combo:<30} | {_fmt(c):<44} | {_fmt(a)}")
    return {"ok": True}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--ae", default=",".join(str(v) for v in DEFAULT_AE),
                        help="grelha de struggle_ae_pips")
    parser.add_argument("--fraction", default=",".join(str(v) for v in DEFAULT_FRACTION),
                        help="grelha de struggle_fraction")
    args = parser.parse_args()
    ae_vals = tuple(float(v) for v in args.ae.split(",") if v.strip())
    fraction_vals = tuple(float(v) for v in args.fraction.split(",") if v.strip())
    db = DB(DB_PATH)
    try:
        await sweep(db, group=args.group, ae_vals=ae_vals,
                    fraction_vals=fraction_vals)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
