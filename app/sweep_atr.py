"""Sweep da sensibilidade do trailing adaptativo por ATR (ESTRATEGIAS.md Parte X).

Corre as variantes `auto_*` × grelha de ATR (atr_k × atr_period) em TRÊS
janelas, para calibrar o `atr_k`/`atr_period` ANTES de o ligar:

  * **atual** — formato atual do grupo (since `2026-07-28`, velas 15m);
  * **tudo** — TODO o histórico na BD (desde o início do grupo, velas 1h);
  * **antigo (out-of-sample)** — `2026-04-01` → `2026-07-28` (formato antigo,
    onde TUDO perdia: robustez real se PF > 1).

A baseline (`atr_k=None`) é o trail FIXO 2.0 congelado pelo sweep original —
o ATR só alarga o recuo (trail_dist = max(fixo, atr_k × ATR)), nunca o
aperta. Nunca escreve na BD nem abre trades.

Uso:
    .venv/Scripts/python -m app.sweep_atr [--atr-k 1.0,1.5,2.0,3.0,5.0] \\
        [--period 7,14,30] [--group "Golden Moves"]

Com `--trail 1.6,1.8,2.0,2.2,2.4` (em vez do ATR) corre o **sweep fino do
`trail_factor` fixo** nas estratégias com trailing (auto_escada_3/5) no
histórico completo — para confirmar que o 2.0 continua no meio do plateau.
"""

import argparse
import asyncio
import os
import sys
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
# data da 1ª mensagem do formato atual (a mesma do compare da dashboard)
CURRENT_SINCE = "2026-07-28"
DEFAULT_ATR_K = (1.0, 1.5, 2.0, 3.0, 5.0)
DEFAULT_PERIOD = (7, 14, 30)


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
    out: Dict[str, List] = {}
    for symbol in {s.symbol for s in signals}:
        try:
            out[symbol] = await provider.history(symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[sweep-atr] sem histórico {interval}/{range_} para {symbol}: {exc}")
    return out


def _run_auto(signals: List, candles: Dict[str, List], sid: str,
              atr_k: Optional[float], atr_period: int, risk: float,
              balance: float, daily_stop: float,
              trail_override: Optional[float] = None) -> dict:
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
            trail_factor=(trail_override if trail_override is not None
                          else strat["trail_factor"]),
            time_stop_minutes=strat["time_stop_minutes"],
            stair_levels=strat["stair_levels"], stair_from=strat["stair_from"],
            confirm_filter=strat["confirm_filter"],
            atr_k=atr_k, atr_period=atr_period)
        results.append({"id": s.id, "received_at": s.received_at, **res})
    apply_daily_stop(results, balance, daily_stop)
    agg = aggregate(results)
    dd = drawdown_from_results(results, balance)
    return {"n": agg["resolved"], "pf": agg["profit_factor"], "pnl": agg["pnl"],
            "wr": agg["win_rate"], "dd_pct": dd["pct"]}


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
    return f"PF {pf:>5} · {r['pnl']:>+9.2f} € · DD {r['dd_pct']:>5.1f}% (n={r['n']})"


async def sweep(db: DB, group: str = "", atr_k_vals: Tuple[float, ...] = DEFAULT_ATR_K,
                period_vals: Tuple[int, ...] = DEFAULT_PERIOD) -> dict:
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("SWEEP_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)

    cur_signals = _load_signals(db, group, CURRENT_SINCE)
    all_signals = _load_signals(db, group, "")
    old_signals = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    print(f"[sweep-atr] atual ({CURRENT_SINCE} → hoje): {len(cur_signals)} sinais | "
          f"tudo: {len(all_signals)} | antiga ({OLD_SINCE}→{OLD_UNTIL}): {len(old_signals)} | "
          f"risco {risk}% · saldo {balance:.0f} € · kill {daily_stop}%")

    provider = YahooProvider()
    cur_candles = await _candles_for(provider, cur_signals, "15m", "1mo")
    all_candles = await _candles_for(provider, all_signals, "1h", "730d")
    old_candles = await _candles_for(provider, old_signals, "1h", "6mo")
    cur_mgmt = _mgmt_by_signal(db, cur_signals, CURRENT_SINCE)
    old_mgmt = _mgmt_by_signal(db, old_signals, OLD_SINCE)

    ref_cur = _run_reference(cur_signals, cur_candles, cur_mgmt, risk, balance, daily_stop)
    ref_old = _run_reference(old_signals, old_candles, old_mgmt, risk, balance, daily_stop)
    ref_all = _run_reference(all_signals, all_candles, cur_mgmt, risk, balance, daily_stop)
    print("\n── REFERÊNCIA nova_gestao ──")
    print(f"  atual:  {_fmt(ref_cur)}")
    print(f"  tudo:   {_fmt(ref_all)}")
    print(f"  antiga: {_fmt(ref_old)}")

    print("\n── SWEEP ATR (baseline = trail fixo 2.0) ──")
    header = f"  {'combo':<22} | {'ATUAL':<34} | {'TUDO':<34} | ANTIGA"
    for sid in AUTO_IDS:
        strat = get_strategy(sid)
        # sem trail_factor (auto_2/3/5) o ATR não tem efeito — uma linha só
        if not strat["trail_factor"]:
            combo = "trail fixo (sem trailing)"
            c = _run_auto(cur_signals, cur_candles, sid, None, 14, risk, balance, daily_stop)
            a = _run_auto(all_signals, all_candles, sid, None, 14, risk, balance, daily_stop)
            o = _run_auto(old_signals, old_candles, sid, None, 14, risk, balance, daily_stop)
            print(f"\n── {sid} · {strat['nome']} · max_legs={strat['max_legs']} · "
                  f"escada={strat['stair_levels']} · confirm={strat['confirm_filter']} ──")
            print(header)
            print(f"  {combo:<22} | {_fmt(c):<34} | {_fmt(a):<34} | {_fmt(o)}")
            continue
        rows = []
        print(f"\n── {sid} · {strat['nome']} · max_legs={strat['max_legs']} · "
              f"escada={strat['stair_levels']} · trail fixo {strat['trail_factor']} ──")
        print(header)
        combos = [(None, 0)] + [(k, p) for k in atr_k_vals for p in period_vals]
        for k, p in combos:
            combo = "baseline (fixo)" if k is None else f"atr_k={k} · p={p}"
            c = _run_auto(cur_signals, cur_candles, sid, k, p, risk, balance, daily_stop)
            a = _run_auto(all_signals, all_candles, sid, k, p, risk, balance, daily_stop)
            o = _run_auto(old_signals, old_candles, sid, k, p, risk, balance, daily_stop)
            rows.append({"combo": combo, "atr_k": k, "atr_period": p,
                         "cur": c, "all": a, "old": o})
            print(f"  {combo:<22} | {_fmt(c):<34} | {_fmt(a):<34} | {_fmt(o)}")    # recomendação: melhor (PF − DD/100) em cada janela — com o critério
    # de plateau (o vizinho a 1 passo não pode estar muito abaixo), para
    # não escolher um pico isolado (sobreajuste)
    for win, key in (("ATUAL", "cur"), ("TUDO", "all"), ("ANTIGA", "old")):
        def score(r):
            pf = r[key].get("pf")
            return (pf or 0.0) - r[key]["dd_pct"] / 100.0
        ranked = sorted(rows, key=score, reverse=True)
        best = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        plateau = (runner is None or best[key].get("pf") is None
                   or runner[key].get("pf") is None
                   or best[key]["pf"] * 0.9 <= runner[key]["pf"])
        flag = " ✓ plateau" if plateau else " ⚠ pico isolado (vizinho muito abaixo)"
        print(f"  → melhor em {win}: {best['combo']} ({_fmt(best[key])}){flag}")
    return {"ok": True}


async def sweep_trail(db: DB, group: str = "",
                      trail_vals: Tuple[float, ...] = (1.6, 1.8, 2.0, 2.2, 2.4)) -> dict:
    """Sweep fino do `trail_factor` FIXO (ATR off) nas estratégias com
    trailing, no histórico completo — o 2.0 congelado tem de continuar no
    meio do plateau (sem pico isolado) para ser honesto."""
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("SWEEP_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)

    cur_signals = _load_signals(db, group, CURRENT_SINCE)
    all_signals = _load_signals(db, group, "")
    old_signals = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    print(f"[sweep-trail] atual ({CURRENT_SINCE} → hoje): {len(cur_signals)} sinais | "
          f"tudo: {len(all_signals)} | antiga: {len(old_signals)} | "
          f"risco {risk}% · saldo {balance:.0f} € · kill {daily_stop}%")

    provider = YahooProvider()
    cur_candles = await _candles_for(provider, cur_signals, "15m", "1mo")
    all_candles = await _candles_for(provider, all_signals, "1h", "730d")
    old_candles = await _candles_for(provider, old_signals, "1h", "6mo")

    print("\n── SWEEP FINO DO TRAIL FIXO (ATR off) ──")
    for sid in AUTO_IDS:
        strat = get_strategy(sid)
        if not strat["trail_factor"]:
            continue  # sem trailing (auto_2/3/5) — nada a varrer
        print(f"\n── {sid} · {strat['nome']} · escada={strat['stair_levels']} · "
              f"confirm={strat['confirm_filter']} ──")
        header = f"  {'trail':<10} | {'ATUAL':<34} | {'TUDO':<34} | ANTIGA"
        print(header)
        rows = []
        for tr in trail_vals:
            combo = f"trail={tr}"
            c = _run_auto(cur_signals, cur_candles, sid, None, 14, risk, balance,
                          daily_stop, trail_override=tr)
            a = _run_auto(all_signals, all_candles, sid, None, 14, risk, balance,
                          daily_stop, trail_override=tr)
            o = _run_auto(old_signals, old_candles, sid, None, 14, risk, balance,
                          daily_stop, trail_override=tr)
            rows.append({"combo": combo, "cur": c, "all": a, "old": o})
            print(f"  {combo:<10} | {_fmt(c):<34} | {_fmt(a):<34} | {_fmt(o)}")
        # verificação do plateau: o 2.0 (meio da grelha) não pode ser um
        # pico isolado — vizinhos com PF dentro de 90% do dele
        for win, key in (("TUDO", "all"), ("ATUAL", "cur"), ("ANTIGA", "old")):
            best = max(rows, key=lambda r: (r[key]["pf"] or 0.0) - r[key]["dd_pct"] / 100.0)
            pf20 = next(r for r in rows if r["combo"] == "trail=2.0")[key]["pf"]
            print(f"  → melhor em {win}: {best['combo']} ({_fmt(best[key])}) · "
                  f"trail=2.0: {_fmt(next(r for r in rows if r['combo']=='trail=2.0')[key])}")
            if best["combo"] == "trail=2.0":
                print("      ✓ 2.0 é o melhor desta janela")
            elif pf20 is not None and best[key]["pf"] is not None \
                    and pf20 >= 0.9 * best[key]["pf"]:
                print(f"      ✓ 2.0 está dentro de 90% do melhor ({pf20:.2f} vs "
                      f"{best[key]['pf']:.2f}) — plateau, sem sobreajuste")
            else:
                print(f"      ⚠ 2.0 fica abaixo de 90% do melhor — reavaliar")
    return {"ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep ATR (ou trail fino) nas auto_*")
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--atr-k", default=",".join(str(v) for v in DEFAULT_ATR_K))
    parser.add_argument("--period", default=",".join(str(v) for v in DEFAULT_PERIOD))
    parser.add_argument("--trail", default="",
                        help="sweep fino do trail_factor fixo (ex.: 1.6,1.8,2.0,2.2,2.4) — "
                             "em vez do ATR")
    args = parser.parse_args()
    db = DB(DB_PATH)
    if args.trail:
        trail_vals = tuple(float(v) for v in args.trail.split(","))
        asyncio.run(sweep_trail(db, group=args.group, trail_vals=trail_vals))
        return 0
    atr_k = tuple(float(v) for v in args.atr_k.split(","))
    period = tuple(int(v) for v in args.period.split(","))
    asyncio.run(sweep(db, group=args.group, atr_k_vals=atr_k, period_vals=period))
    return 0


if __name__ == "__main__":
    sys.exit(main())
