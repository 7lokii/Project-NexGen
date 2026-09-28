"""Estudo read-only: atraso sinal→mercado (quando o GC=F tocou a entrada).

O candle-check usa uma janela horária à volta da hora do sinal do Telegram.
Mas quanto tempo demora, em média, até o mercado tocar a entrada anunciada?
Este estudo mede, para cada sinal REAL nos últimos N dias:

  * o **atraso** (minutos) entre a hora do sinal no Telegram e o primeiro
    candle cujo range contém a entrada — mediana/p90/máx, POR SÍMBOLO;
  * quantas entradas são tocadas dentro de 1h/2h/3h/6h (vs nunca no dia) —
    valida a largura da janela do candle-check;
  * a **janela recomendada** por símbolo (p75 do atraso, limitada a
    [3, 12] h, mínimo 5 amostras) para a `recount_window_hours_by_symbol`
    da config — o candle-check passa a usar a janela do próprio símbolo.

Read-only: nunca escreve na BD, nunca abre trades. `--apply` grava a
recomendação em `recount_window_hours_by_symbol` (config) — o único efeito
lateral, e só com o mínimo de amostras. Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_signal_delay \\\\
        [--days 30] [--group "Golden Moves"] [--symbol ""] [--apply]
"""

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timedelta, timezone

from app.config import DB_PATH
from app.db import DB

# janela mínima (nunca abaixo do default 3h) e máxima (não abrir a sessão
# inteira por acidente) da recomendação por símbolo
MIN_WINDOW = 3
MAX_WINDOW = 12
MIN_SAMPLES = 5


def _recommend(delays_min: list) -> float:
    """Janela recomendada: p75 do atraso (min), arredondado às horas, no
    intervalo [3, 12]. p75 em vez da mediana para cobrir os casos comuns
    sem perseguir o p90 (que apanha feeds que nunca tocam a entrada)."""
    if len(delays_min) < MIN_SAMPLES:
        return 0.0
    delays_min.sort()
    p75 = delays_min[int(len(delays_min) * 0.75)] \
        if len(delays_min) > 1 else delays_min[-1]
    rec = max(MIN_WINDOW, min(MAX_WINDOW, math.ceil((p75 + 60) / 60)))
    return float(rec)


def _spot_candles(symbol: str) -> list:
    """Candles M15 spot do terminal MT5 (read-only); [] sem terminal."""
    import os
    from app.execution.mt5 import MT5Provider

    creds = {"login": None, "server": "", "password": ""}
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), ".env"), encoding="utf-8") as fh:
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
    provider = MT5Provider(login=creds["login"], server=creds["server"],
                           password=creds["password"])
    if not provider.initialize():
        return []
    try:
        rates = provider.copy_rates(symbol, timeframe=15, count=9000)
    finally:
        provider.shutdown()
    return [{"ts": int(r["time"]), "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]} for r in rates]


async def _study(days: int, group: str, symbol_filter: str,
                 apply: bool) -> None:
    from app.execution.feed import YahooProvider

    db = DB(DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
        and (s.received_at or "").startswith("20")
        and (not symbol_filter or s.symbol == symbol_filter)
    ]
    if not signals:
        print("Sem sinais no período — nada a medir.")
        return
    provider = YahooProvider()
    by_symbol: dict = {}
    for s in signals:
        by_symbol.setdefault(s.symbol, []).append(s)

    recs: dict = {}
    for symbol, sym_signals in sorted(by_symbol.items()):
        # fonte: spot MT5 quando disponível (o mercado do grupo, sem a
        # contaminação futuros↔spot do GC=F); fallback Yahoo GC=F
        candles = _spot_candles(symbol)
        source = "spot MT5" if candles else "GC=F Yahoo (sem terminal)"
        if not candles:
            try:
                candles = await provider.history(symbol, interval="15m",
                                                 range_="1mo")
            except Exception as exc:
                print(f"[{symbol}] sem candles: {exc}")
                continue
        candles.sort(key=lambda c: c["ts"])
        from app.backtest import _pip_size
        tol = _pip_size(symbol) * 40
        delays = []
        touched = {1: 0, 2: 0, 3: 0, 6: 0}
        never = 0
        total = 0
        for s in sym_signals:
            try:
                sig_ts = datetime.fromisoformat(s.received_at).timestamp()
            except (ValueError, TypeError):
                continue
            entry = float(s.entry)
            total += 1
            first = None
            for c in candles:
                if c["ts"] < sig_ts - 900:
                    continue
                if c["low"] - tol <= entry <= c["high"] + tol:
                    first = c
                    break
            if first is None:
                never += 1
                continue
            minutes = (first["ts"] - sig_ts) / 60
            delays.append(minutes)
            for h in (1, 2, 3, 6):
                if minutes <= h * 60:
                    touched[h] += 1
        if not delays:
            print(f"[{symbol}] {total} sinais — nenhuma entrada tocada pelo "
                  f"feed dentro do mês (divergência de preços ou feed sem "
                  f"liquidez).")
            continue
        delays.sort()
        median = delays[len(delays) // 2]
        p75 = delays[int(len(delays) * 0.75)] if len(delays) > 1 else delays[-1]
        p90 = delays[int(len(delays) * 0.90)] if len(delays) > 1 else delays[-1]
        rec = _recommend(delays)
        print(f"\n[{symbol}] sinais: {total} (tocados: {len(delays)}, "
              f"nunca: {never}) — fonte: {source}")
        print(f"  atraso sinal→mercado (min): min {delays[0]:.0f} · mediana "
              f"{median:.0f} · p75 {p75:.0f} · p90 {p90:.0f} · "
              f"máx {delays[-1]:.0f}")
        print(f"  entradas tocadas ≤1h: {touched[1]} · ≤2h: {touched[2]} · "
              f"≤3h: {touched[3]} · ≤6h: {touched[6]}")
        if total:
            print(f"  taxa de toque ≤3h (default do candle-check): "
                  f"{touched[3]}/{total} ({touched[3] / total * 100:.0f}%)")
        if rec:
            print(f"  JANELA RECOMENDADA: {rec:.0f}h (p75 {p75:.0f} min + "
                  f"folga, no intervalo [{MIN_WINDOW}, {MAX_WINDOW}] h)")
            if p75 > median * 4:
                print(f"  ⚠️ p75 ({p75:.0f} min) ≫ mediana ({median:.0f} min): a "
                      f"cauda é dominada pelo feed que toca a entrada muito "
                      f"tarde (ex.: GC=F futuros vs spot) — a recomendação "
                      f"abre a janela à sessão inteira e perde a distinção "
                      f"de sessões. Prefere um valor manual (ex.: 6h) ou "
                      f"re-corre com a calibração do offset ativa.")
            recs[symbol] = rec
        else:
            print(f"  janela não recomendada (< {MIN_SAMPLES} amostras de "
                  f"toque — mantém o default)")

    if apply and recs:
        cfg = db.all_config()
        try:
            cur = json.loads(cfg.get("recount_window_hours_by_symbol") or "{}")
        except (ValueError, TypeError):
            cur = {}
        cur.update({k: v for k, v in recs.items()})
        db.set_config("recount_window_hours_by_symbol",
                      json.dumps(cur, sort_keys=True))
        print(f"\n✅ `recount_window_hours_by_symbol` atualizado na config: "
              f"{json.dumps(cur, sort_keys=True)} — o candle-check passa a "
              f"usar a janela de cada símbolo (a partir do próximo tick).")
    elif recs:
        print(f"\nRecomendações (grava com --apply): "
              f"{json.dumps(recs, sort_keys=True)}")

    print("\nInterpretação: se a mediana for baixa (minutos), a janela "
          "default ±3h é folgada e os implausíveis do candle-check apontam "
          "mesmo para relatos/fills divergentes; se for alta, parte dos "
          "implausíveis pode ser apenas atraso entre o grupo e o feed — "
          "a janela recomendada cobre o atraso típico por símbolo.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--symbol", default="",
                        help="só um símbolo (ex.: XAUUSD); vazio = todos")
    parser.add_argument("--apply", action="store_true",
                        help="gravar as janelas recomendadas na config "
                             "(recount_window_hours_by_symbol)")
    args = parser.parse_args()
    asyncio.run(_study(args.days, args.group, args.symbol, args.apply))


if __name__ == "__main__":
    sys.exit(main())
