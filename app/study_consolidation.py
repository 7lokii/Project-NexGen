"""Estudo da consolidação (spec 19/08) — filtros, modo defensivo e Item 3.

Compara, para cada estratégia auto, o baseline com duas variantes nas MESMAS
janelas do sweep (atual · tudo · antiga out-of-sample):

  * **baseline** — parâmetros do registo (ex.: auto_escada_3, trail 2.0);
  * **entrada (filtro)** — só sinais que passam o filtro: sessão Londres/NY
    (07:00–20:00 UTC), distância entrada→TP2 >= 5 pips (gold) / 25 pips
    (forex), e ATR(14) do dia >= p20 do ATR da janela anterior (sem
    look-ahead: o ATR usa velas ANTES do sinal);
  * **defensivo** — quando o PF das últimas 10 trades resolvidas do baseline
    < 1.0, a estratégia passa a trail apertado (1.5) + time stop (60 min),
    SEM cortar pernas (o `max_legs=2` do spec foi rejeitado — o sweep provou
    que 2 pernas cortam o motor do lucro).

O modo defensivo é sequencial (cada sinal decide com as 10 anteriores) e a
história que o alimenta é a do BASELINE (o que a estratégia veria em
produção). Nunca escreve na BD nem abre trades.

Uso:
    .venv/Scripts/python -m app.study_consolidation [--group "Golden Moves"]
"""

import argparse
import asyncio
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.analysis import DEFAULT_COMMISSION, DEFAULT_DELAY, DEFAULT_SLIPPAGE, DEFAULT_SPREAD
from app.backtest import (aggregate, apply_daily_stop, drawdown_from_results,
                          simulate_signal)
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider
from app.strategies import get_strategy

OLD_SINCE, OLD_UNTIL = "2026-04-01", "2026-07-28"
CURRENT_SINCE = "2026-07-28"

# estratégias com auto-gestão (trailing/escada) — as que o spec quer melhorar
STUDY_IDS = ["auto_escada_3", "auto_escada_5", "auto_3"]

# Grelha congelada do Item 2: pequena o suficiente para repetir numa VPS,
# larga o suficiente para distinguir tendência de pico isolado.
DEF_SWEEP_WINDOWS = (5, 7, 10)
DEF_SWEEP_TRAILS = (1.0, 1.2, 1.5)
DEF_SWEEP_TIME_STOPS = (30, 45, 60)

# filtro de entrada — parâmetros honestos (não calibrados sobre os dados:
# valores de senso comum, validados depois pelas duas janelas)
SESSION_UTC_START, SESSION_UTC_END = 7, 20          # Londres+NY ≈ 07:00–20:00 UTC
MIN_TP2_PIPS = 5                                     # gold (0.05); forex = 5×0.0001
ATR_PERCENTILE = 0.20                                # p20 do ATR(14) diário
DEF_WINDOW = 10                                      # últimas 10 trades resolvidas
DEF_MAX_PF = 1.0                                     # PF < 1.0 → modo defensivo
DEF_TRAIL = 1.5                                      # trail apertado
DEF_TIME_STOP = 60                                   # time stop em minutos


def _load_signals(db: DB, group: str, since: str, until: str = "") -> List:
    return [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
        and (not until or (s.received_at or "")[:10] < until)
    ]


def _utc_hour(received_at: str) -> Optional[int]:
    """Hora UTC do sinal.

    Timestamps da BD sem fuso são a hora local histórica do bot (UTC+1 no
    período estudado); timestamps com fuso são convertidos de facto para UTC,
    incluindo offsets negativos e a passagem pela meia-noite.
    """
    try:
        raw = (received_at or "").strip()
        if len(raw) < 13 or raw[10] not in ("T", " "):
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            return int(dt.astimezone(timezone.utc).hour)
        return (dt.hour - 1) % 24  # local (UTC+1 verão) → UTC
    except (ValueError, TypeError, IndexError, OverflowError):
        return None


def _candle_day(ts) -> Optional[str]:
    """UTC calendar day for a Yahoo epoch or ISO candle timestamp."""
    try:
        if isinstance(ts, str):
            raw = ts.strip()
            if not raw:
                return None
            if "T" in raw or ":" in raw or ("-" in raw and len(raw) > 8):
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                value = float(raw)
                if abs(value) > 10_000_000_000:
                    value /= 1000.0
                dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            value = float(ts)
            if abs(value) > 10_000_000_000:
                value /= 1000.0
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _daily_atr_map(candles: List[dict]) -> Dict[str, float]:
    """Last available ATR(14) for each UTC day, in chronological candles."""
    from app.backtest import _candle_atr
    days: Dict[str, float] = {}
    for i, c in enumerate(candles):
        atr = _candle_atr(candles, i, 14)
        day = _candle_day(c.get("ts"))
        if atr is not None and day is not None:
            # The last completed observation of a day represents that day;
            # setdefault would incorrectly retain the first intraday ATR.
            days[day] = atr
    return days


def _daily_atrs(candles: List[dict]) -> List[float]:
    """ATR(14) por dia (última vela disponível do dia), em UTC."""
    daily = _daily_atr_map(candles)
    return [daily[day] for day in sorted(daily)]


def _atr_floor_before_signal(signal, candles: List[dict]) -> Optional[float]:
    """P20 ATR floor using only complete calendar days before this signal."""
    from app.backtest import _start_index, _to_epoch
    try:
        start = _start_index(
            candles, _to_epoch(signal.received_at or "") + DEFAULT_DELAY,
        )
    except (ValueError, TypeError, OverflowError):
        return None
    if start <= 0:
        return None
    cutoff_day = _candle_day(candles[start - 1].get("ts"))
    if cutoff_day is None:
        return None
    history = [value for day, value in _daily_atr_map(candles).items()
               if day < cutoff_day]
    if not history:
        return None
    history.sort()
    return history[max(0, int(len(history) * ATR_PERCENTILE) - 1)]


def _passes_entry_filter(s, candles: List[dict], atr_floor: float,
                         only_atr: bool = False) -> tuple:
    """(passa?, motivo de rejeição). Filtros: sessão, distância ao TP2, ATR.
    `only_atr=True` (sensibilidade 19/08) = só o filtro de ATR — para
    confirmar que a sessão/distância ao TP2 não mudam o veredicto."""
    if not only_atr:
        hh = _utc_hour(s.received_at or "")
        if hh is None or not (SESSION_UTC_START <= hh < SESSION_UTC_END):
            return False, "fora_sessao"
        pip = 0.01 if s.symbol in ("XAUUSD", "XAGUSD") else 0.0001
        tp2 = float(getattr(s, "take_profit_2", 0.0) or 0.0)
        if not tp2:
            return False, "sem_tp2"
        dist = abs(s.entry - tp2) / pip
        if dist < MIN_TP2_PIPS:
            return False, "tp2_curto"
    if atr_floor is not None and candles:
        # ATR(14) na vela mais próxima ANTES do sinal (sem look-ahead)
        from app.backtest import _candle_atr, _start_index, _to_epoch
        try:
            i = _start_index(candles, _to_epoch(s.received_at or "") + DEFAULT_DELAY)
        except (ValueError, TypeError, OverflowError):
            return True, ""
        # `_candle_atr(candles, i)` already excludes candle i and includes the
        # last completed candle i-1. Passing i-1 introduced an off-by-one gap.
        atr = _candle_atr(candles, i, 14) if i > 0 else None
        if atr is not None and atr < atr_floor:
            return False, "atr_baixo"
    return True, ""


def _roll_pf(last: List[float]) -> Optional[float]:
    """PF das últimas trades resolvidas (pnl > 0 win, < 0 loss; 0 = BE fora)."""
    wins = sum(1 for p in last if p > 0)
    losses = sum(1 for p in last if p < 0)
    if not losses:
        return float("inf") if wins else None
    gross_win = sum(p for p in last if p > 0)
    gross_loss = -sum(p for p in last if p < 0)
    return gross_win / gross_loss if gross_loss else float("inf")


def _run(signals: List, candles: Dict[str, List], sid: str, risk: float,
         balance: float, daily_stop: float, mode: str,
         baseline_history: Optional[Dict[str, float]] = None,
         def_window: int = DEF_WINDOW,
         def_trail: float = DEF_TRAIL,
         def_time_stop: int = DEF_TIME_STOP,
         confirm_override: Optional[bool] = None) -> dict:
    """`mode` = baseline | entrada | entrada_atr | defensivo.
    `baseline_history` (opcional) = pnl por id do baseline da mesma janela —
    alimenta o modo defensivo com o que a estratégia REALMENTE veria (não
    com a variante filtrada). Os três parâmetros defensivos são argumentos
    para o sweep do Item 2; os defaults reproduzem o registo atual."""
    strat = get_strategy(sid)
    confirm = (strat["confirm_filter"] if confirm_override is None
               else bool(confirm_override))
    # The ATR floor is computed separately for each signal from days already
    # elapsed. A single floor calculated from the whole candle range would
    # leak future volatility into the entry decision.

    ordered = sorted(signals, key=lambda s: (s.received_at or "", s.id))
    results, rolled = [], []
    # Nunca alterar o mapa recebido: no sweep todas as combinações têm de
    # consultar exactamente o mesmo histórico baseline, sem contaminar a
    # próxima variante com o P&L da anterior.
    history = dict(baseline_history or {})
    rej: Dict[str, int] = {}
    n_def = 0
    for s in ordered:
        cs = candles.get(s.symbol)
        if not cs:
            continue
        tf, ts = strat["trail_factor"], strat["time_stop_minutes"]
        if mode == "defensivo":
            hist = [history[i] for i in rolled if i in history]
            hist = hist[-int(def_window):] if def_window else []
            pf = _roll_pf(hist) if len(hist) >= int(def_window) else None
            if pf is not None and pf < DEF_MAX_PF:
                tf, ts = def_trail, def_time_stop
                n_def += 1
        if mode in ("entrada", "entrada_atr"):
            atr_floor = _atr_floor_before_signal(s, cs)
            ok, why = _passes_entry_filter(s, cs, atr_floor,
                                           only_atr=(mode == "entrada_atr"))
            if not ok:
                rej[why] = rej.get(why, 0) + 1
                continue
        res = simulate_signal(
            s, cs, risk, balance, DEFAULT_DELAY, DEFAULT_SPREAD,
            DEFAULT_SLIPPAGE, DEFAULT_COMMISSION,
            max_legs=strat["max_legs"], exit_at_tp=strat["exit_at_tp"],
            lots_override=strat["lots_override"],
            trail_factor=tf, time_stop_minutes=ts,
            stair_levels=strat["stair_levels"], stair_from=strat["stair_from"],
            confirm_filter=confirm)
        results.append({"id": s.id, "received_at": s.received_at, **res})
        if res.get("outcome") not in (None, "open", "no_data", "skip"):
            rolled.append(s.id)
            # Quando o mapa veio do baseline, conserva-o intacto. Se o caller
            # não forneceu histórico (uso genérico do helper), a própria
            # variante torna-se a fonte de contexto para o próximo sinal.
            if baseline_history is None or mode != "defensivo":
                history[s.id] = float(res.get("pnl") or 0.0)
    apply_daily_stop(results, balance, daily_stop)
    agg = aggregate(results)
    dd = drawdown_from_results(results, balance)
    out = {"n": agg["resolved"], "pf": agg["profit_factor"], "pnl": agg["pnl"],
           "wr": agg["win_rate"], "dd_pct": dd["pct"], "sinal_total": len(results)}
    if mode in ("entrada", "entrada_atr"):
        out["rejeitados"] = rej
    if mode == "defensivo":
        out["n_defensivo"] = n_def
    return out


def _metric(result: dict) -> dict:
    """Métricas pequenas e estáveis para a tabela/JSON do sweep."""
    agg = result.get("aggregate") or {}
    return {
        "pf": (agg.get("profit_factor") if agg else result.get("pf")),
        "pnl": round(float((agg.get("pnl") if agg else result.get("pnl")) or 0.0), 2),
        "dd_pct": float(result.get("dd_pct") or (result.get("drawdown") or {}).get("pct") or 0.0),
        "resolved": int((agg.get("resolved") if agg else result.get("n")) or 0),
        "signals": int(result.get("sinal_total") or agg.get("total") or 0),
        "win_rate": agg.get("win_rate") if agg else result.get("wr"),
    }


def _no_isolated_peak(values: List[Optional[float]]) -> bool:
    """Aceita plateau/monotonicidade e rejeita um pico interior isolado."""
    nums = [v for v in values if v is not None]
    if len(nums) < 3:
        return True
    for i in range(1, len(values) - 1):
        value = values[i]
        if value is None:
            continue
        left, right = values[i - 1], values[i + 1]
        if left is not None and right is not None and value > max(left, right):
            return False
    return True


def _no_isolated_valley(values: List[Optional[float]]) -> bool:
    """Espelho do critério de DD: não congela um mínimo isolado."""
    nums = [v for v in values if v is not None]
    if len(nums) < 3:
        return True
    for i in range(1, len(values) - 1):
        value = values[i]
        if value is None:
            continue
        left, right = values[i - 1], values[i + 1]
        if left is not None and right is not None and value < min(left, right):
            return False
    return True


def _axis_average(rows: List[dict], axis: str, value, window: str,
                  metric: str) -> Optional[float]:
    vals = [row["windows"][window].get(metric)
            for row in rows if row[axis] == value
            and row["windows"].get(window, {}).get(metric) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def evaluate_defensive_sweep(rows: List[dict], baseline: Dict[str, dict],
                             axes: Optional[Dict[str, List]] = None) -> dict:
    """Evaluate Item 2 without rerunning candles.

    The gate is deliberately strict: an option is eligible only when the
    *current* PF keeps at least 85% of the current baseline and the *old*
    drawdown falls below 50%. Monotonicity is reported independently for all
    three axes and all three windows; it never silently promotes an isolated
    winner.
    """
    axes = axes or {
        "def_window": list(DEF_SWEEP_WINDOWS),
        "def_trail": list(DEF_SWEEP_TRAILS),
        "def_time_stop": list(DEF_SWEEP_TIME_STOPS),
    }
    baseline_current_pf = baseline.get("atual", {}).get("pf")
    eligible = []
    for row in rows:
        current = row["windows"].get("atual", {})
        old = row["windows"].get("antiga", {})
        if (baseline_current_pf is not None and current.get("pf") is not None
                and current["pf"] >= baseline_current_pf * 0.85
                and old.get("dd_pct") is not None and old["dd_pct"] < 50.0):
            eligible.append({
                "def_window": row["def_window"],
                "def_trail": row["def_trail"],
                "def_time_stop": row["def_time_stop"],
                "current_pf": current["pf"],
                "old_dd_pct": old["dd_pct"],
            })

    monotonicity = {}
    for axis, values in axes.items():
        monotonicity[axis] = {}
        for window in ("atual", "tudo", "antiga"):
            pfs = [_axis_average(rows, axis, value, window, "pf") for value in values]
            dds = [_axis_average(rows, axis, value, window, "dd_pct") for value in values]
            monotonicity[axis][window] = {
                "pf_no_isolated_peak": _no_isolated_peak(pfs),
                "dd_no_isolated_valley": _no_isolated_valley(dds),
                "pf_by_value": pfs,
                "dd_by_value": dds,
            }
    return {
        "eligible": eligible,
        "eligible_count": len(eligible),
        "monotonicity": monotonicity,
        "verdict": ("alterar_default" if eligible else
                    "não alterar o default (DD antiga >= 50% ou PF atual "
                    "caiu mais de 15%)"),
    }


def run_defensive_sweep(
    windows: Dict[str, tuple], sid: str = "auto_escada_5", risk: float = 1.0,
    balance: float = 1000.0, daily_stop: float = 0.0,
    def_windows: Optional[List[int]] = None,
    def_trails: Optional[List[float]] = None,
    def_time_stops: Optional[List[int]] = None,
) -> dict:
    """Run the complete 3×3×3 Item 2 grid on current/tudo/old windows.

    `windows` maps ``atual``, ``tudo`` and ``antiga`` to ``(signals, candles)``.
    The baseline history is computed once per window and reused by every
    combination, so the sweep is reproducible and has no parameter leakage.
    """
    axes = {
        "def_window": list(def_windows or DEF_SWEEP_WINDOWS),
        "def_trail": list(def_trails or DEF_SWEEP_TRAILS),
        "def_time_stop": list(def_time_stops or DEF_SWEEP_TIME_STOPS),
    }
    cache = {}
    baseline = {}
    for name, (signals, candles) in windows.items():
        base = _run(signals, candles, sid, risk, balance, daily_stop, "baseline")
        raw = _run_results(signals, candles, sid, risk, balance, daily_stop)
        history = {
            row["id"]: float(row.get("pnl") or 0.0)
            for row in raw
            if row.get("outcome") in ("win", "loss", "be")
        }
        cache[name] = (signals, candles, history)
        baseline[name] = _metric(base)

    rows = []
    for window in axes["def_window"]:
        for trail in axes["def_trail"]:
            for time_stop in axes["def_time_stop"]:
                row = {
                    "def_window": window, "def_trail": trail,
                    "def_time_stop": time_stop, "windows": {},
                }
                for name, (signals, candles, history) in cache.items():
                    result = _run(
                        signals, candles, sid, risk, balance, daily_stop,
                        "defensivo", baseline_history=history,
                        def_window=window, def_trail=trail,
                        def_time_stop=time_stop,
                    )
                    row["windows"][name] = _metric(result)
                rows.append(row)
    verdict = evaluate_defensive_sweep(rows, baseline, axes)
    return {
        "strategy_id": sid, "grid": axes, "baseline": baseline,
        "rows": rows, **verdict,
    }


def _confirm_relative_metrics(variants: Dict[str, Dict[str, dict]]) -> dict:
    """Apply the Item 3 gate to two already-computed variants.

    ``confirm_true`` is eligible only when the old-window DD strictly falls
    and the current-window PF loses no more than 10% relative to the exact
    ``confirm_false`` replay. Missing/zero PF or DD is not evidence for a
    promotion.
    """
    plain = variants.get("confirm_false", {})
    candidate = variants.get("confirm_true", {})
    plain_current = plain.get("atual", {})
    candidate_current = candidate.get("atual", {})
    plain_old = plain.get("antiga", {})
    candidate_old = candidate.get("antiga", {})

    base_pf = plain_current.get("pf")
    candidate_pf = candidate_current.get("pf")
    pf_drop = None
    if base_pf is not None and candidate_pf is not None and float(base_pf) > 0:
        pf_drop = round((float(base_pf) - float(candidate_pf))
                        / float(base_pf) * 100.0, 2)
    pf_ok = pf_drop is not None and pf_drop <= 10.0

    base_dd = plain_old.get("dd_pct")
    candidate_dd = candidate_old.get("dd_pct")
    dd_reduction = None
    if base_dd is not None and candidate_dd is not None and float(base_dd) > 0:
        dd_reduction = round((float(base_dd) - float(candidate_dd))
                             / float(base_dd) * 100.0, 2)
    old_dd_reduced = (base_dd is not None and candidate_dd is not None
                      and float(candidate_dd) < float(base_dd))
    eligible = bool(pf_ok and old_dd_reduced)
    return {
        "current_pf_drop_pct": pf_drop,
        "old_dd_reduction_pct": dd_reduction,
        "current_pf_ok": pf_ok,
        "old_dd_reduced": old_dd_reduced,
        "eligible": eligible,
        "verdict": ("alterar_default" if eligible else
                    "não alterar o default (DD antiga não reduziu ou "
                    "PF atual caiu mais de 10%)"),
    }


def run_confirm_sweep(
    windows: Dict[str, tuple], sid: str = "auto_escada_3", risk: float = 1.0,
    balance: float = 1000.0, daily_stop: float = 0.0,
) -> dict:
    """Run the Item 3 binary sweep without writing to the DB.

    ``windows`` maps ``atual``, ``tudo`` and ``antiga`` to ``(signals,
    candles)``. Every replay uses the registered auto parameters except for
    ``confirm_filter``; stair=3/from=1 and trail=2.0 therefore stay fixed,
    and both variants consume the same candles, costs and chronological
    signal set. The defensive mode is deliberately not applied: Item 3
    isolates the requested stair+trail interaction rather than mixing in the
    separate Item 2 defensive intervention.
    """
    strat = get_strategy(sid)
    variants = {"confirm_false": {}, "confirm_true": {}}
    for name, (signals, candles) in windows.items():
        for flag, key in ((False, "confirm_false"), (True, "confirm_true")):
            result = _run(
                signals, candles, sid, risk, balance, daily_stop, "baseline",
                confirm_override=flag,
            )
            variants[key][name] = _metric(result)
    comparison = _confirm_relative_metrics(variants)
    return {
        "strategy_id": sid,
        "fixed": {
            "max_legs": strat["max_legs"],
            "stair_levels": strat["stair_levels"],
            "stair_from": strat["stair_from"],
            "trail_factor": strat["trail_factor"],
            "time_stop_minutes": strat["time_stop_minutes"],
            "defensive_mode": False,
        },
        "variants": variants,
        "comparison": comparison,
        "verdict": comparison["verdict"],
    }


def _fmt(r: dict, label: str = "") -> str:
    pf = f"{r['pf']:.2f}" if r["pf"] is not None else " — "
    extra = ""
    if "rejeitados" in r:
        extra = " | rej: " + ", ".join(f"{k}={v}" for k, v in r["rejeitados"].items())
    if "n_defensivo" in r:
        extra = f" | em modo defensivo: {r['n_defensivo']}"
    return (f"{label:<26} PF {pf:>5} · {r['pnl']:>+9.2f} € · DD {r['dd_pct']:>5.1f}% "
            f"(n={r['n']}/{r['sinal_total']}){extra}")


async def study(db: DB, group: str = "") -> dict:
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("STUDY_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)

    cur = _load_signals(db, group, CURRENT_SINCE)
    old = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    all_s = _load_signals(db, group, "")
    print(f"[study-consolidação] atual ({CURRENT_SINCE}→hoje): {len(cur)} | "
          f"antiga ({OLD_SINCE}→{OLD_UNTIL}): {len(old)} | tudo: {len(all_s)} | "
          f"risco {risk}% · saldo {balance:.0f} € · kill {daily_stop}%")

    provider = YahooProvider()
    cur_c = await _candles(provider, cur, "15m", "1mo")
    old_c = await _candles(provider, old, "1h", "6mo")
    all_c = await _candles(provider, all_s, "1h", "730d")

    for sid in STUDY_IDS:
        strat = get_strategy(sid)
        print(f"\n── {sid} · {strat['nome']} · max_legs={strat['max_legs']} · "
              f"escada={strat['stair_levels']} · trail {strat['trail_factor']} ──")
        for win, signals, candles in (("ATUAL", cur, cur_c), ("TUDO", all_s, all_c),
                                      ("ANTIGA", old, old_c)):
            base = _run(signals, candles, sid, risk, balance, daily_stop, "baseline")
            # história do baseline para o defensivo (mesma janela): o modo
            # defensivo decide com o que a estratégia REALMENTE vê em produção
            hist = {
                r["id"]: float(r.get("pnl") or 0.0)
                for r in _run_results(signals, candles, sid, risk, balance, daily_stop)
                if r.get("outcome") in ("win", "loss", "be")
            }
            ent = _run(signals, candles, sid, risk, balance, daily_stop, "entrada")
            atr_only = _run(signals, candles, sid, risk, balance, daily_stop,
                            "entrada_atr")
            dfn = _run(signals, candles, sid, risk, balance, daily_stop, "defensivo",
                       baseline_history=hist)
            print(f"  {win:<7}")
            print(f"    {_fmt(base, 'baseline')}")
            print(f"    {_fmt(ent, 'filtro entrada')}")
            print(f"    {_fmt(atr_only, 'só ATR (sensibilidade)')}")
            print(f"    {_fmt(dfn, 'defensivo')}")
    return {"ok": True}


def _run_results(signals: List, candles: Dict[str, List], sid: str, risk: float,
                 balance: float, daily_stop: float) -> List[dict]:
    """Resultados do baseline (para alimentar o modo defensivo sem duplicar
    a simulação — mesma lógica de `_run` mas devolve os resultados crus)."""
    strat = get_strategy(sid)
    ordered = sorted(signals, key=lambda s: (s.received_at or "", s.id))
    results = []
    for s in ordered:
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
            confirm_filter=strat["confirm_filter"])
        results.append({"id": s.id, "received_at": s.received_at, **res})
    apply_daily_stop(results, balance, daily_stop)
    return results


async def _candles(provider: YahooProvider, signals: List, interval: str,
                   range_: str) -> Dict[str, List]:
    out: Dict[str, List] = {}
    for symbol in {s.symbol for s in signals}:
        try:
            out[symbol] = await provider.history(symbol, interval=interval, range_=range_)
        except Exception as exc:
            print(f"[study] sem histórico {interval}/{range_} para {symbol}: {exc}")
    return out



def _print_item2_sweep(report: dict) -> None:
    """Print the compact table recorded in the Item 2 acceptance evidence."""
    print("\n── ITEM 2 · auto_escada_5 · sweep defensivo ──")
    print("  baseline: " + " | ".join(
        f"{name} PF {metric.get('pf')} / DD {metric.get('dd_pct')}%"
        for name, metric in report["baseline"].items()
    ))
    print("  window trail time | atual PF/DD | tudo PF/DD | antiga PF/DD")
    for row in report["rows"]:
        cells = []
        for name in ("atual", "tudo", "antiga"):
            metric = row["windows"].get(name, {})
            pf = "—" if metric.get("pf") is None else f"{metric['pf']:.2f}"
            cells.append(f"{pf}/{metric.get('dd_pct', 0):.1f}%")
        print(f"  {row['def_window']:>6} {row['def_trail']:>5} {row['def_time_stop']:>4} | "
              f"{cells[0]:>11} | {cells[1]:>10} | {cells[2]:>11}")
    print(f"  monotonicidade: {report['monotonicity']}")
    print(f"  combinações elegíveis: {report['eligible_count']}")
    print(f"  veredicto: {report['verdict']}")


def _print_item3_sweep(report: dict) -> None:
    """Print the compact Item 3 acceptance report."""
    print("\n── ITEM 3 · auto_escada_3 · confirm_filter ──")
    fixed = report["fixed"]
    print("  fixos: " + ", ".join(
        f"{key}={value}" for key, value in fixed.items()
    ))
    print("  janela | confirm=False PF/DD | confirm=True PF/DD")
    for name in ("atual", "tudo", "antiga"):
        cells = []
        for key in ("confirm_false", "confirm_true"):
            metric = report["variants"][key].get(name, {})
            pf = "—" if metric.get("pf") is None else f"{metric['pf']:.2f}"
            cells.append(f"{pf}/{metric.get('dd_pct', 0):.1f}%")
        print(f"  {name:<7} | {cells[0]:>20} | {cells[1]:>19}")
    print(f"  comparação: {report['comparison']}")
    print(f"  veredicto: {report['verdict']}")


async def item3_sweep(db: DB, group: str = "") -> dict:
    """Run only the reproducible Item 3 sweep (no writes, no paper trades)."""
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("STUDY_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)
    cur = _load_signals(db, group, CURRENT_SINCE)
    old = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    all_s = _load_signals(db, group, "")
    provider = YahooProvider()
    cur_c = await _candles(provider, cur, "15m", "1mo")
    old_c = await _candles(provider, old, "1h", "6mo")
    all_c = await _candles(provider, all_s, "1h", "730d")
    report = run_confirm_sweep(
        {"atual": (cur, cur_c), "tudo": (all_s, all_c),
         "antiga": (old, old_c)},
        sid="auto_escada_3", risk=risk, balance=balance,
        daily_stop=daily_stop,
    )
    _print_item3_sweep(report)
    return report


async def item2_sweep(db: DB, group: str = "") -> dict:
    """Run only the reproducible Item 2 sweep (no writes, no paper trades)."""
    risk = float(db.all_config().get("risk_pct") or 1.0)
    balance = float(os.environ.get("STUDY_BALANCE", "1000") or 1000)
    daily_stop = float(db.all_config().get("daily_loss_limit_pct", 0) or 0)
    cur = _load_signals(db, group, CURRENT_SINCE)
    old = _load_signals(db, group, OLD_SINCE, OLD_UNTIL)
    all_s = _load_signals(db, group, "")
    provider = YahooProvider()
    cur_c = await _candles(provider, cur, "15m", "1mo")
    old_c = await _candles(provider, old, "1h", "6mo")
    all_c = await _candles(provider, all_s, "1h", "730d")
    report = run_defensive_sweep(
        {"atual": (cur, cur_c), "tudo": (all_s, all_c),
         "antiga": (old, old_c)},
        sid="auto_escada_5", risk=risk, balance=balance,
        daily_stop=daily_stop,
    )
    _print_item2_sweep(report)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="Golden Moves")
    ap.add_argument("--item2-sweep", action="store_true",
                    help="corre a grelha 3×3×3 da auto_escada_5 e termina")
    ap.add_argument("--item3-sweep", action="store_true",
                    help="compara confirm_filter=True/False na auto_escada_3")
    args = ap.parse_args()
    db = DB(DB_PATH)
    if args.item3_sweep:
        asyncio.run(item3_sweep(db, group=args.group))
    elif args.item2_sweep:
        asyncio.run(item2_sweep(db, group=args.group))
    else:
        asyncio.run(study(db, group=args.group))
