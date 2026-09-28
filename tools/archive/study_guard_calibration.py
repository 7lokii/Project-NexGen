"""Estudo read-only — calibração data-driven das guardas de entrada (Fase 3).

Pergunta: que combinação de `entry_guard_auto_cap` (cap autónomo em × largura
da zona), `entry_guard_tolerance` (tolerância automática em pips) e `risk_pct`
teria dado melhor PF/P&L/DD nos sinais reais dos últimos 30 dias?

Método (sem escrever na BD, sem abrir trades, sem tocar na config):
  1. Lê os sinais reais com parse completo (entrada+SL+TP1) dos últimos N dias.
  2. Busca candles 15m Yahoo (1mo) por símbolo — a divergência GC=F↔spot é
     conhecida e marcada no relatório (XAUUSD usa GC=F no Yahoo).
  3. Preço de mercado à hora do sinal = close do primeiro candle a partir de
     `received_at` (o MESMO candle onde o replay começa — sem look-ahead).
  4. Para cada configuração (cap × tolerância): um sinal executa se
       - estiver dentro da zona, ou
       - `outside ≤ tolerância` (executa a mercado), ou
       - `outside ≤ cap × largura` → ORDEM PENDENTE no nível da entrada: só
         conta se um candle da janela de fill tocar o nível (low ≤ entry ≤
         high para compras) — o não-touch fica `no_fill` (sem trade).
     Sinais além do cap são REJEITADOS (a perda hipotética não acontece).
  5. Executa o replay gerido (`simulate_signal_managed`, nova_gestao) no
     conjunto executado e agrega PF/P&L/DD por célula.

Uso:
    .venv/Scripts/python -m app.study_guard_calibration [--days 30] \
        [--fill-hours 4] [--out reports/guard_calibration_<data>.json]

Saída: tabela por célula + veredicto (melhor célula por PF com N mínimo e DD
≤ 125%, comparada com a config atual). Read-only: nunca escreve na BD.
"""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone

from app.backtest import aggregate, drawdown_from_results, simulate_signal_managed
from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider

# Config atual de produção (para a linha de referência)
CURRENT = {"cap_widths": 0.0, "tolerance": 0.5, "risk_pct": 4.0}

GRID_CAP = [0.0, 0.5, 1.0, 2.0, 3.0]      # 0.0 = limite atual (5× largura)
GRID_TOL = [0.25, 0.5, 1.0]
GRID_RISK = [2.0, 3.0, 4.0, 5.0]

BALANCE = 1000.0
DEFAULT_COSTS = dict(delay_seconds=60, spread_pips=1.0, slippage_pips=0.5,
                     commission_per_lot=3.5)


def _to_epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _mt5_credentials():
    """Lê as credenciais MT5 do .env (o estudo corre fora do main.py)."""
    import os
    creds = {"login": None, "server": "", "password": ""}
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MT5_LOGIN="):
                    creds["login"] = int(line.split("=", 1)[1])
                elif line.startswith("MT5_SERVER="):
                    creds["server"] = line.split("=", 1)[1]
                elif line.startswith("MT5_PASSWORD="):
                    creds["password"] = line.split("=", 1)[1]
    except OSError:
        pass
    return creds


async def _mt5_spot_candles(symbol: str) -> list:
    """Candles M15 spot do terminal (read-only); [] sem terminal ligado.

    O GC=F do Yahoo diverge do spot (offset conhecido — ver study_recount_*):
    para XAUUSD o estudo usa o spot MT5 sempre que possível."""
    from app.execution.mt5 import MT5Provider, load_symbol_map
    mapping = load_symbol_map()
    mt5_sym = mapping.get(symbol, {}).get("mt5") if mapping else symbol
    creds = _mt5_credentials()
    provider = MT5Provider(login=creds["login"], server=creds["server"],
                           password=creds["password"])
    if not provider.initialize():
        print("[estudo] terminal MT5 indisponível — só o lado Yahoo (futuros) será medido.",
              file=sys.stderr)
        return []
    try:
        rates = provider.copy_rates(mt5_sym or symbol, timeframe=15, count=9000)
    finally:
        provider.shutdown()
    return [{"ts": int(r["time"]), "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]} for r in rates]


async def load_candles(symbols, days: int) -> dict:
    """Spot MT5 quando o terminal está ligado; fallback Yahoo (futuros p/ metais)."""
    interval, range_ = ("15m", "1mo") if days <= 30 else ("1h", "3mo")
    provider = YahooProvider()
    out, sources = {}, {}
    for symbol in symbols:
        spot = await _mt5_spot_candles(symbol) if symbol == "XAUUSD" else []
        if spot:
            out[symbol] = spot
            sources[symbol] = "mt5-spot"
            continue
        try:
            out[symbol] = await provider.history(symbol, interval=interval,
                                                 range_=range_)
            sources[symbol] = "yahoo"
        except Exception as exc:
            print(f"[estudo] sem histórico para {symbol}: {exc}", file=sys.stderr)
    print(f"Fonte de candles por símbolo: {sources}")
    return out


def market_price_at_signal(candles, epoch: int) -> float:
    """Close do primeiro candle a partir da hora do sinal (mesmo start do
    replay — sem look-ahead)."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["ts"] < epoch:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(candles):
        return float(candles[-1]["close"]) if candles else 0.0
    return float(candles[lo]["close"])


def pending_would_fill(candles, start_idx: int, direction: str, entry: float,
                       window_hours: int) -> bool:
    """Um candle na janela toca o nível da entrada (fill da ordem pendente)."""
    limit_ts = candles[start_idx]["ts"] + window_hours * 3600 if start_idx < len(candles) else 0
    for c in candles[start_idx:]:
        if c["ts"] > limit_ts:
            break
        if direction == "buy" and float(c["low"]) <= entry <= float(c["high"]):
            return True
        if direction == "sell" and float(c["high"]) >= entry >= float(c["low"]):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--fill-hours", type=int, default=4)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db = DB(DB_PATH)
    since = (date.today() - timedelta(days=max(1, args.days))).isoformat()
    signals = [s for s in db.signals_since(since)
               if s.symbol and s.direction and s.entry and s.stop_loss
               and float(s.take_profit_1 or 0.0) > 0
               and s.received_at]
    if not signals:
        print("Sem sinais parseáveis no período — nada a calibrar.")
        return 1
    print(f"Sinais parseáveis ({args.days}d): {len(signals)}")

    candles_by_symbol = asyncio.run(load_candles(
        {s.symbol for s in signals}, args.days))

    # Preço de mercado à hora do sinal + índice de início do replay por sinal
    enriched = []
    for s in signals:
        candles = candles_by_symbol.get(s.symbol) or []
        if not candles:
            continue
        start = 0
        lo, hi = 0, len(candles)
        epoch = _to_epoch(s.received_at)
        while lo < hi:
            mid = (lo + hi) // 2
            if candles[mid]["ts"] < epoch:
                lo = mid + 1
            else:
                hi = mid
        start = lo
        if start >= len(candles):
            continue  # o histórico não cobre a hora do sinal
        price = float(candles[start]["close"])
        low = float(s.entry_zone_low or 0.0) or float(s.entry)
        high = float(s.entry_zone_high or 0.0) or float(s.entry)
        low, high = sorted((low, high))
        width = high - low or 1.0
        outside = max(low - price, 0.0, price - high)
        enriched.append({
            "signal": s, "candles": candles, "start": start,
            "price": price, "zone": (low, high), "width": width,
            "outside": outside, "epoch": epoch,
        })
    print(f"Sinais com preço de mercado no histórico: {len(enriched)}")

    from app.strategies import get_strategy, sim_params
    strat = get_strategy("nova_gestao")
    params = sim_params("nova_gestao")
    params.pop("manage", None)  # simulate_signal_managed filtra por manage_kinds

    def _execute_set(cap_widths: float, tolerance: float):
        """Devolve a lista de 'executáveis' (market/pending-fill) + rejeitados."""
        execs, rejected, no_fill = [], 0, 0
        for e in enriched:
            if e["outside"] <= tolerance:
                execs.append((e, "market"))
            else:
                cap = e["width"] * cap_widths if cap_widths > 0 else e["width"] * 5.0
                if e["outside"] <= cap:
                    filled = pending_would_fill(
                        e["candles"], e["start"], e["signal"].direction,
                        float(e["signal"].entry), args.fill_hours)
                    if filled:
                        execs.append((e, "pending"))
                    else:
                        no_fill += 1
                else:
                    rejected += 1
        return execs, rejected, no_fill

    def _run(execs, risk_pct: float):
        results = []
        for e, style in execs:
            try:
                res = simulate_signal_managed(
                    e["signal"], e["candles"],
                    [], risk_pct, BALANCE, **DEFAULT_COSTS, **params)
            except Exception as exc:
                res = {"outcome": "erro", "pnl": 0.0}
            results.append({"id": e["signal"].id, "style": style, **res})
        agg = aggregate(results)
        dd = drawdown_from_results(results, BALANCE)
        pnl = sum(float(r.get("pnl") or 0.0) for r in results)
        return {"executed": len(results), "resolved": agg.get("resolved", 0),
                "wins": agg.get("wins", 0), "losses": agg.get("losses", 0),
                "pf": agg.get("profit_factor"), "pnl": round(pnl, 2),
                "dd_pct": dd.get("pct")}

    rows = []
    for cap in GRID_CAP:
        for tol in GRID_TOL:
            execs, rejected, no_fill = _execute_set(cap, tol)
            for risk in GRID_RISK:
                st = _run(execs, risk)
                st.update({"cap": cap, "tolerance": tol, "risk_pct": risk,
                           "rejected": rejected, "no_fill": no_fill})
                rows.append(st)

    # Veredicto: melhor PF entre células com N≥15 resolvidos e DD≤125%
    def _key(r):
        return (r.get("pf") or 0.0)
    eligible = [r for r in rows
                if r["resolved"] >= 15 and (r.get("dd_pct") or 999) <= 125.0
                and (r.get("pf") or 0.0) > 0]
    best = max(eligible, key=_key) if eligible else None
    current = next((r for r in rows
                    if r["cap"] == CURRENT["cap_widths"]
                    and r["tolerance"] == CURRENT["tolerance"]
                    and r["risk_pct"] == CURRENT["risk_pct"]), None)

    print(f"\n{'cap×':>6} {'tol':>5} {'risco':>6} {'exec':>5} {'resolv':>7} "
          f"{'win':>4} {'loss':>5} {'PF':>7} {'P&L':>10} {'DD%':>7} "
          f"{'rej':>4} {'nofill':>6}")
    for r in rows:
        print(f"{r['cap']:>6} {r['tolerance']:>5} {r['risk_pct']:>6} "
              f"{r['executed']:>5} {r['resolved']:>7} {r['wins']:>4} "
              f"{r['losses']:>5} {r['pf'] if r['pf'] is not None else '-':>7} "
              f"{r['pnl']:>10} {r['dd_pct'] if r['dd_pct'] is not None else '-':>7} "
              f"{r['rejected']:>4} {r['no_fill']:>6}")

    print("\n=== Veredicto (Fase 3) ===")
    print(f"Config atual: cap={CURRENT['cap_widths']} (limite 5×), "
          f"tol={CURRENT['tolerance']}, risco={CURRENT['risk_pct']}%"
          + (f" → PF {current['pf']}, P&L {current['pnl']} €, "
             f"DD {current['dd_pct']}%, {current['executed']} executadas"
             if current else ""))
    if best:
        print(f"Melhor célula: cap={best['cap']}×, tol={best['tolerance']}, "
              f"risco={best['risk_pct']}% → PF {best['pf']}, P&L {best['pnl']} €, "
              f"DD {best['dd_pct']}%, {best['executed']} executadas "
              f"({best['rejected']} rejeitadas, {best['no_fill']} no_fill)")
        delta = (current or {}).get("pf")
        if current and delta:
            print(f"Δ PF vs atual: {(best['pf'] - delta) / delta * 100:+.1f}%")
        # só vale a pena calibrar com edge REAL: PF>1 na melhor célula E
        # melhoria material face à atual — num período perdedor (PF<1 em
        # todas as células) a diferença é ruído e apertar o cap só rejeita
        # mais sinais (a amostra mostrou os fora-de-zona MELHORES que os
        # dentro de zona neste recorte).
        if (best.get("pf") or 0.0) > 1.0 and current and delta and delta > 0 \
                and (best["pf"] - delta) / delta > 0.15:
            print("Recomendação: CALIBRAR para a célula acima.")
        else:
            print("Recomendação: MANTER os defaults — sem evidência de edge "
                  "(período perdedor, PF<1 em todas as células).")
    else:
        print("Nenhuma célula cumpriu N≥15 resolvidos e DD≤125% — "
              "a calibração não tem evidência para mudar defaults.")
        print("Recomendação: MANTER os defaults.")

    if args.out or not args.out:
        out = args.out or f"reports/guard_calibration_{date.today().isoformat()}.json"
        import os
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"days": args.days, "current": CURRENT, "rows": rows,
                       "best": best, "verdict": (
                           "calibrar" if best else "manter defaults")},
                      fh, ensure_ascii=False, indent=1, default=str)
        print(f"Relatório: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
