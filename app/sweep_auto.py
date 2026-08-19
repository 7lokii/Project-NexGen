"""Sweep das variantes de auto-gestão (docs/ESTRATEGIAS.md, Parte V — fonte única de estratégias).

Corre as 5 variantes `auto_*` × grelha de parâmetros (trail_factor ×
time_stop_minutes × stair_from) em DUAS janelas:

  * **atual** — últimos `--days` dias (mesma janela do A/B/C da dashboard,
    para comparar com a `nova_gestao` PF 3.22);
  * **antigo (out-of-sample)** — `2026-04-01` → `2026-07-28` (formato antigo,
    onde TUDO perdia: se uma variante tiver PF > 1 aqui, é robustez real).

Inclui a `nova_gestao` como referência (com a gestão do grupo correlacionada)
nas duas janelas. Nunca escreve na BD nem abre trades.

Uso:
    .venv/Scripts/python -m app.sweep_auto [--days 30] [--trail 0,0.3,0.5] \\
        [--time-stop 0,60,120] [--stair-from 1,2] [--group "Golden Moves"]
"""

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from app.analysis import DEFAULT_COMMISSION, DEFAULT_DELAY, DEFAULT_SLIPPAGE, DEFAULT_SPREAD
from app.backtest import (aggregate, apply_daily_stop, correlate_management,
                          drawdown_from_results, simulate_signal,
                          simulate_signal_managed)
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider
from app.messages.manage import classify
from app.strategies import STRATEGIES, get_strategy

AUTO_IDS = ["auto_2", "auto_3", "auto_5", "auto_escada_3", "auto_escada_5"]
OLD_SINCE, OLD_UNTIL = "2026-04-01", "2026-07-28"
# data da 1ª mensagem do formato atual (usada no compare da dashboard)
CURRENT_SINCE = "2026-07-28"


def _to_epoch(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp())


def _load_signals(db: DB, group: str, since: str, until: str = "") -> List:
    return [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
        and (not until or (s.received_at or "")[:10] < until)
    ]


def _mgmt_by_signal(db: DB, signals: List, since: str) -> dict:
    mgmt = []
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
        mgmt.append((s.source or "", epoch, action["kind"], bool(action.get("be")), action.get("tp")))
    return correlate_management(signals, mgmt)


async def _candles_for(provider: YahooProvider, signals: List,
                        interval: str = "15m", range_: str = "1mo") -> Dict[str, List]:
    """Candles por símbolo — a janela atual usa 15m/1mo; o período antigo
    (abr–jul) precisa de 1h/6mo (Yahoo não serve 15m além de ~1 mês)."""
    out: Dict[str, List] = {}
    for symbol in {s.symbol for s in signals}:
        try:
            out[symbol] = await provider.history(symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[sweep] sem histórico {interval}/{range_} para {symbol}: {exc}")
    return out


def _run_auto(signals: List, candles: Dict[str, List], sid: str,
              trail: Optional[float], time_stop: Optional[int],
              stair_from: int, risk: float, balance: float,
              daily_stop: float) -> dict:
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
            trail_factor=trail, time_stop_minutes=time_stop,
            stair_levels=strat["stair_levels"], stair_from=stair_from,
            confirm_filter=strat["confirm_filter"])
        results.append({"id": s.id, "received_at": s.received_at, **res})
    apply_daily_stop(results, balance, daily_stop)
    agg = aggregate(results)
    dd = drawdown_from_results(results, balance)
    return {"n": agg["resolved"], "pf": agg["profit_factor"], "pnl": agg["pnl"],
            "wr": agg["win_rate"], "dd_pct": dd["pct"], "results": results}


def _run_reference(signals: List, candles: Dict[str, List], mgmt: dict,
                   risk: float, balance: float, daily_stop: float) -> dict:
    results = []
    for s in signals:
        cs = candles.get(s.symbol)
        if not cs:
            continue
        res = simulate_signal_managed(
            s, cs, mgmt.get(s.id, []), risk, balance, DEFAULT_DELAY,
            DEFAULT_SPREAD, DEFAULT_SLIPPAGE, DEFAULT_COMMISSION)
        results.append({"id": s.id, "received_at": s.received_at, **res})
    apply_daily_stop(results, balance, daily_stop)
    agg = aggregate(results)
    dd = drawdown_from_results(results, balance)
    return {"n": agg["resolved"], "pf": agg["profit_factor"], "pnl": agg["pnl"],
            "wr": agg["win_rate"], "dd_pct": dd["pct"]}


def _fmt(r: dict) -> str:
    pf = f"{r['pf']:.2f}" if r["pf"] is not None else " — "
    return f"PF {pf:>5} · {r['pnl']:>+9.2f} € · WR {r['wr']:>5}% · DD {r['dd_pct']:>5}% (n={r['n']})"


async def sweep(db: DB, group: str = "", days: int = 30,
                trail_vals: Tuple[Optional[float], ...] = (None, 0.5, 1.0, 1.5),
                time_stop_vals: Tuple[Optional[int], ...] = (None, 120, 240, 480),
                stair_from_vals: Tuple[int, ...] = (1, 2)) -> dict:
    risk = float(db.all_config().get("risk_pct") or 1.0)
    # saldo 1000 € — o mesmo das páginas de teste e do A/B/C da dashboard
    # (a config balance é o saldo da conta paper, que pode crescer; o estudo
    # compara estratégias em igualdade de circunstâncias)
    balance = float(os.environ.get("SWEEP_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)

    cur_since = (date.today() - timedelta(days=days)).isoformat()
    cur_signals = _load_signals(db, group, cur_since)
    old_signals = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    print(f"[sweep] janela atual ({cur_since} → hoje): {len(cur_signals)} sinais | "
          f"antiga ({OLD_SINCE} → {OLD_UNTIL}): {len(old_signals)} sinais | "
          f"risco {risk}% · saldo {balance:.0f} € · kill diário {daily_stop}%")

    provider = YahooProvider()
    cur_candles = await _candles_for(provider, cur_signals, "15m", "1mo")
    old_candles = await _candles_for(provider, old_signals, "1h", "6mo")
    cur_mgmt = _mgmt_by_signal(db, cur_signals, cur_since)
    old_mgmt = _mgmt_by_signal(db, old_signals, OLD_SINCE)

    # referência: nova_gestao (gestão do grupo) nas duas janelas
    ref_cur = _run_reference(cur_signals, cur_candles, cur_mgmt, risk, balance, daily_stop)
    ref_old = _run_reference(old_signals, old_candles, old_mgmt, risk, balance, daily_stop)
    print(f"\n── REFERÊNCIA nova_gestao ──")
    print(f"  atual: {_fmt(ref_cur)}")
    print(f"  antiga: {_fmt(ref_old)}")

    for sid in AUTO_IDS:
        strat = get_strategy(sid)
        static = not strat["stair_levels"]
        sf_vals = (1,) if static else stair_from_vals
        print(f"\n── {sid} · {strat['nome']} · max_legs={strat['max_legs']} · "
              f"escada={strat['stair_levels']} · confirm={strat['confirm_filter']} ──")
        print(f"  {'combo':<28} | {'ATUAL (' + str(days) + 'd)':<46} | ANTIGA (out-of-sample)")
        for sf in sf_vals:
            for ts in time_stop_vals:
                for tr in trail_vals:
                    combo = f"stair_from={sf} trail={tr or 0} time={ts or 0}"
                    c = _run_auto(cur_signals, cur_candles, sid, tr, ts, sf, risk, balance, daily_stop)
                    o = _run_auto(old_signals, old_candles, sid, tr, ts, sf, risk, balance, daily_stop)
                    print(f"  {combo:<28} | {_fmt(c):<46} | {_fmt(o)}")
    return {"ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep das variantes de auto-gestão")
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--trail", default="0,0.5,1.0,1.5", help="trail_factor separados por vírgula (0 = off) — a mineração mostra que o grupo tolera ~1–2.3 distâncias de TP2 de recuo")
    parser.add_argument("--time-stop", default="0,120,240,480", help="time_stop_minutes separados por vírgula (0 = off) — o grupo espera p90≈15h após TP1")
    parser.add_argument("--stair-from", default="1,2", help="degrau inicial da escada")
    args = parser.parse_args()
    trail = tuple((None if float(v) == 0 else float(v)) for v in args.trail.split(","))
    tstop = tuple((None if int(v) == 0 else int(v)) for v in args.time_stop.split(","))
    sfrom = tuple(int(v) for v in args.stair_from.split(","))
    db = DB(DB_PATH)
    asyncio.run(sweep(db, group=args.group, days=args.days,
                      trail_vals=trail, time_stop_vals=tstop, stair_from_vals=sfrom))
    return 0


if __name__ == "__main__":
    sys.exit(main())
