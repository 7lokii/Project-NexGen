"""Mineração empírica da gestão do grupo — calibração dos parâmetros da
auto-gestão (docs/ESTRATEGIAS.md, Parte V — fonte única de estratégias).

Para cada sinal real dos últimos N dias, alinha as mensagens de gestão do
grupo ("we're out", "close first entries", "out at BE", "protect") com o
price action real (candles Yahoo) e mede, no momento em que o grupo decidiu:

  * retracement desde o pico/vale, em % da distância entrada→TP2
    → calibra `trail_factor` (quanto o preço recua até o grupo sair);
  * minutos desde o TP1 (se o TP1 já tinha batido)
    → calibra `time_stop_minutes` (quanto tempo o grupo aguenta parado);
  * distância do preço à entrada (se já estava "risk free" / acima do BE)
    → informa `stair_from` (escada desde TP1 vs só BE).

Nunca escreve na BD nem abre trades — é uma ferramenta de análise.

Uso:
    .venv/Scripts/python -m app.mine_management [--days 30] [--group "Golden Moves"]
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional

from app.analysis import DEFAULT_DELAY, DEFAULT_RISK_PCT
from app.backtest import _start_index, _to_epoch, correlate_management
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider
from app.messages.manage import classify

MGMT_KINDS = ("protect", "close_all", "close_tp1", "close_tp")


def _pct(vals: List[float], q: float) -> Optional[float]:
    """Quantile simples (sem numpy)."""
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(q * len(s)))
    return round(s[idx], 3)


def _stats(vals: List[float], label: str) -> None:
    if not vals:
        print(f"  {label:<34} — (sem amostras)")
        return
    lo, hi = min(vals), max(vals)
    print(f"  {label:<34} n={len(vals):>3}  min {lo:+.3f}  p25 {(_pct(vals, .25) or 0):+.3f}  "
          f"p50 {(_pct(vals, .50) or 0):+.3f}  p75 {(_pct(vals, .75) or 0):+.3f}  "
          f"p90 {(_pct(vals, .90) or 0):+.3f}  max {hi:+.3f}")


async def mine(db: DB, group: str = "", days: int = 30) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
    ]
    if not signals:
        return {"ok": True, "signals": 0, "events": 0}

    # mensagens de gestão classificadas (mesmo critério do run_analysis)
    mgmt_events = []
    for s in db.signals_since(since):
        try:
            action = classify(s.raw_text or "")
        except Exception:
            action = None
        if not action or action["kind"] not in MGMT_KINDS:
            continue
        try:
            epoch = _to_epoch(s.received_at)
        except (ValueError, TypeError):
            continue
        mgmt_events.append((s.source or "", epoch, action["kind"],
                            bool(action.get("be")), action.get("tp")))
    by_signal = correlate_management(signals, mgmt_events)

    provider = YahooProvider()
    candles_by_symbol: Dict[str, List] = {}
    for symbol in {s.symbol for s in signals}:
        try:
            candles_by_symbol[symbol] = await provider.history(symbol, interval="15m", range_="1mo")
        except Exception as exc:
            print(f"[mining] sem histórico para {symbol}: {exc}")

    # métricas por kind: (retrace_pct_tp2, minutos_desde_tp1, dist_entrada_pct)
    metrics: Dict[str, List] = {k: [] for k in MGMT_KINDS}
    metrics_min: Dict[str, List] = {k: [] for k in MGMT_KINDS}
    metrics_dist: Dict[str, List] = {k: [] for k in MGMT_KINDS}

    for sig in signals:
        candles = candles_by_symbol.get(sig.symbol)
        if not candles:
            continue
        events = by_signal.get(sig.id) or []
        if not events:
            continue
        start = _start_index(candles, _to_epoch(sig.received_at) + DEFAULT_DELAY)
        if start >= len(candles):
            continue
        tps = {i: float(getattr(sig, f"take_profit_{i}", 0.0) or 0.0)
               for i in range(1, 6)
               if float(getattr(sig, f"take_profit_{i}", 0.0) or 0.0) > 0}
        if 2 not in tps:
            continue  # precisa do TP2 para normalizar o retracement
        dist_tp2 = abs(tps[2] - sig.entry)
        if dist_tp2 <= 0:
            continue
        buy = sig.direction == "buy"
        tp1_level = tps.get(1)
        tp1_ts: Optional[int] = None
        best = sig.entry  # pico (buy) / vale (sell) desde a entrada

        for ev in events:
            epoch = ev[0]
            idx = _start_index(candles, epoch)
            if idx >= len(candles):
                idx = len(candles) - 1
            if idx < start:
                continue
            # pico/vale desde a entrada até à vela do evento (informação real
            # até ao momento da decisão do grupo — sem look-ahead)
            for c in candles[start:idx + 1]:
                best = max(best, c["high"]) if buy else min(best, c["low"])
                if tp1_ts is None and tp1_level is not None:
                    hit = c["high"] >= tp1_level if buy else c["low"] <= tp1_level
                    if hit:
                        tp1_ts = c["ts"]
            price = candles[idx]["close"]
            retrace = (best - price) if buy else (price - best)
            retrace_pct = retrace / dist_tp2
            dist_entry = (price - sig.entry) if buy else (sig.entry - price)
            dist_entry_pct = dist_entry / dist_tp2
            mins_since_tp1 = None
            if tp1_ts is not None:
                mins_since_tp1 = (epoch - tp1_ts) / 60.0
            kind = ev[1]
            metrics[kind].append(retrace_pct)
            metrics_dist[kind].append(dist_entry_pct)
            if mins_since_tp1 is not None:
                metrics_min[kind].append(mins_since_tp1)

    print(f"\n=== MINERAÇÃO DA GESTÃO DO GRUPO ({days} dias, {len(signals)} sinais) ===")
    print("Métrica principal — retracement desde o pico/vale no momento da decisão,")
    print("em % da distância entrada→TP2 (0 = no pico; 1 = devolveu o TP2 inteiro).")
    for kind in MGMT_KINDS:
        print(f"\n[{kind}]")
        _stats(metrics[kind], "retrace_pct (do pico ao preço)")
        _stats(metrics_dist[kind], "distância à entrada (em TP2s)")
        _stats(metrics_min[kind], "minutos desde o TP1")

    # sugestões de calibração (p50 conservador; p90 = o grupo aguentou mais)
    def _suggest(kind: str, metric: str) -> Optional[float]:
        vals = {"retrace": metrics[kind], "min": metrics_min[kind]}.get(metric)
        return _pct(vals or [], 0.5)

    print("\n=== SUGESTÕES DE CALIBRAÇÃO (interpretação) ===")
    for kind in ("close_all", "close_tp1", "protect"):
        r = _suggest(kind, "retrace")
        if r is not None:
            # trail_factor define SL = pico − factor × dist(TP2). O grupo deixa o
            # preço recuar ~p50 distâncias de TP2 do pico antes de agir — um
            # trail mecânico com factor ~p50 replica essa paciência (valores
            # 0.3–0.5 são MUITO mais agressivos que o grupo).
            print(f"  {kind:<10} retrace p50 = {r:.3f} distâncias de TP2 → "
                  f"trail_factor ≈ {r:.2f} replica a paciência do grupo "
                  f"(0.3–0.5 fecha muito mais cedo)")
    mins = [v for kind in ("close_tp1", "protect") for v in metrics_min[kind]]
    if mins:
        p50 = _pct(mins, 0.5) or 0
        p90 = _pct(mins, 0.9) or 0
        print(f"  time_stop_minutes: o grupo espera p50={p50:.0f} min após TP1 (muitos eventos são "
              f"pré-TP1, p50 negativo) e p90={p90:.0f} min → time stop de 30–60 min fecha "
              f"MUITO antes do grupo; testar 120–480 min")
    return {"ok": True, "signals": len(signals),
            "events": sum(len(v) for v in by_signal.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description="Minera a gestão real do grupo para calibrar a auto-gestão")
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    db = DB(DB_PATH)
    asyncio.run(mine(db, group=args.group, days=args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
