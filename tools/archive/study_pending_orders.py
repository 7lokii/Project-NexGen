"""Estudo read-only — viabilidade das ordens pendentes na zona (Fase 1.1).

Pergunta: quando o preço está FORA da zona de entrada, quantas rejeições uma
ordem pendente (BUY_LIMIT/SELL_LIMIT/STOP no nível da entrada) teria
recuperado, e com que qualidade?

Método (sem escrever na BD, sem abrir trades):
  1. Lê os sinais reais com zona + entrada + SL + TP1 da BD de produção.
  2. Busca candles M15 (spot MT5 quando ligado; fallback GC=F Yahoo — a
     divergência spot↔futuros é conhecida e marcada no relatório).
  3. Para cada sinal, classifica o preço à hora do sinal: dentro da zona
     (executaria hoje) ou fora.
  4. Fora da zona: uma ordem pendente no nível da entrada teria preenchido
     se um candle da janela tocar o nível (low ≤ entry ≤ high).
  5. Se preencheu: tempo até ao fill e primeiro toque entre TP1 e SL
     (proxy simples de qualidade — não é P&L completo da gestão).

Uso:
    .venv/Scripts/python -m app.study_pending_orders [--days 30] \
        [--window-hours 4] [--symbol XAUUSD] [--out reports/...]

--days 0 = todo o histórico. O relatório sai no stdout e num JSON em
reports/pending_orders_study_<data>.json.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from app.config import DB_PATH
from app.db import DB
from app.execution.feed import YahooProvider


def _now_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _mt5_credentials():
    """Lê as credenciais MT5 do .env (o estudo corre fora do main.py)."""
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
    return creds


async def _mt5_candles(symbol: str) -> list:
    """Candles M15 spot do terminal (read-only); [] sem terminal ligado."""
    from app.execution.mt5 import MT5Provider
    creds = _mt5_credentials()
    provider = MT5Provider(login=creds["login"], server=creds["server"],
                           password=creds["password"])
    if not provider.initialize():
        print("[estudo] terminal MT5 indisponível — só o lado Yahoo (futuros) será medido.")
        return []
    try:
        rates = provider.copy_rates(symbol, timeframe=15, count=9000)
    finally:
        provider.shutdown()
    return [{"ts": int(r["time"]), "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]} for r in rates]


def load_signals(db, days: int):
    where = ("entry_zone_low IS NOT NULL AND entry_zone_high IS NOT NULL "
             "AND entry > 0 AND stop_loss > 0 AND take_profit_1 > 0 ")
    params = []
    if days and days > 0:
        where += "AND received_at > datetime('now', ?) "
        params.append(f"-{days} days")
    where += "ORDER BY received_at"
    rows = db.conn.execute(
        f"SELECT * FROM signals WHERE {where}", params).fetchall()
    return [db._signal_from_row(r) for r in rows]


def zone_status(sig, price: float):
    low, high = sorted((float(sig.entry_zone_low), float(sig.entry_zone_high)))
    if low <= price <= high:
        return "inside", 0.0
    return "outside", max(low - price, price - high)


def would_fill(sig, candles, t0: float, window_seconds: float):
    """Ordem pendente no nível da entrada: preenche quando um candle da
    janela toca o nível (low ≤ entry ≤ high) — cobre limit e stop."""
    fill_level = float(sig.entry)
    for c in candles:
        if c["ts"] - t0 > window_seconds:
            break
        if c["low"] <= fill_level <= c["high"]:
            return True, c
    return False, None


def first_touch(sig, candles, t_fill: float, window_seconds: float):
    """Depois do fill: TP1 ou SL primeiro (proxy de qualidade)."""
    tp1 = float(sig.take_profit_1)
    sl = float(sig.stop_loss)
    for c in candles:
        if c["ts"] <= t_fill:
            continue
        if c["ts"] - t_fill > window_seconds:
            return "no_touch"
        if sig.direction == "buy":
            if c["low"] <= sl:
                return "sl_first"
            if c["high"] >= tp1:
                return "tp1_first"
        else:
            if c["high"] >= sl:
                return "sl_first"
            if c["low"] <= tp1:
                return "tp1_first"
    return "no_touch"


async def run(days: int, window_hours: float, symbol: str, out: str):
    db = DB(DB_PATH)
    try:
        signals = load_signals(db, days)
        if symbol:
            signals = [s for s in signals if s.symbol == symbol]
        # foca sinais com entrada dentro da zona (parse limpo)
        usable = []
        for s in signals:
            low, high = sorted((float(s.entry_zone_low), float(s.entry_zone_high)))
            if low <= float(s.entry) <= high:
                usable.append(s)
        signals = usable

        # Spot MT5 primeiro (a fonte real dos fills); GC=F Yahoo como fallback
        # (a divergência spot↔futuros é conhecida e marcada no relatório).
        cache: dict = {}
        candle_source = {}

        async def candles_for(sig):
            if sig.symbol not in cache:
                spot = await _mt5_candles(sig.symbol)
                if spot:
                    cache[sig.symbol] = spot
                    candle_source[sig.symbol] = "spot-mt5"
                else:
                    try:
                        cache[sig.symbol] = await YahooProvider().history(
                            sig.symbol, interval="15m", range_="1mo")
                        candle_source[sig.symbol] = "yahoo-gcf"
                    except Exception as exc:
                        print(f"[estudo] candles {sig.symbol} indisponíveis: {exc}")
                        cache[sig.symbol] = []
            return cache[sig.symbol]

        inside = 0
        outside = 0
        skipped_no_candles = 0
        rows_out = []
        window_seconds = window_hours * 3600.0

        for sig in signals:
            from datetime import datetime as _dt
            try:
                t0 = _dt.fromisoformat(sig.received_at).timestamp()
            except (ValueError, TypeError):
                continue
            candles = await candles_for(sig)
            if not candles or candles[-1]["ts"] < t0:
                skipped_no_candles += 1
                continue
            start = next((i for i, c in enumerate(candles) if c["ts"] >= t0),
                         None)
            if start is None:
                skipped_no_candles += 1
                continue
            price = float(candles[start]["open"])
            status, outside_dist = zone_status(sig, price)
            if status == "inside":
                inside += 1
                continue
            outside += 1
            filled, fill_c = would_fill(sig, candles[start:], t0, window_seconds)
            rec = {
                "signal_id": sig.id, "received_at": sig.received_at,
                "symbol": sig.symbol, "direction": sig.direction,
                "zone": [sig.entry_zone_low, sig.entry_zone_high],
                "entry": sig.entry, "stop_loss": sig.stop_loss,
                "tp1": sig.take_profit_1,
                "price_at_signal": round(price, 2),
                "outside_dist": round(outside_dist, 2),
                "filled": filled,
            }
            if filled:
                t_fill = float(fill_c["ts"])
                rec["fill_seconds"] = round(t_fill - t0, 1)
                rec["first_touch"] = first_touch(
                    sig, candles[start:], t_fill, window_seconds)
            else:
                rec["first_touch"] = "no_fill"
            rows_out.append(rec)

        filled_n = sum(1 for r in rows_out if r["filled"])
        tp1_first = sum(1 for r in rows_out if r.get("first_touch") == "tp1_first")
        sl_first = sum(1 for r in rows_out if r.get("first_touch") == "sl_first")
        no_touch = sum(1 for r in rows_out if r.get("first_touch") in ("no_touch", "no_fill"))
        fill_times = sorted(r["fill_seconds"] for r in rows_out if r["filled"])

        def pct(x, y):
            return round(100.0 * x / y, 1) if y else 0.0

        report = {
            "data": _now_iso_date(),
            "days": days, "window_hours": window_hours, "symbol": symbol or "*",
            "fonte": candle_source or "sem candles",
            "total_sinais_com_zona": len(signals),
            "sem_candles_na_janela": skipped_no_candles,
            "dentro_da_zona": inside,
            "fora_da_zona": outside,
            "preencheria_pendente": filled_n,
            "taxa_fill": pct(filled_n, outside),
            "tp1_primeiro": tp1_first,
            "sl_primeiro": sl_first,
            "sem_toque_no_horizonte": no_touch,
            "taxa_tp1_primeiro_entre_fills": pct(tp1_first, filled_n),
            "mediana_fill_segundos": (
                fill_times[len(fill_times) // 2] if fill_times else None),
            "detalhe": rows_out,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        try:
            import os
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            print(f"\n[estudo] relatório gravado em {out}")
        except Exception as exc:
            print(f"\n[estudo] não foi possível gravar o relatório: {exc}")
        return report
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--window-hours", type=float, default=4.0)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out = args.out or f"reports/pending_orders_study_{_now_iso_date()}.json"
    asyncio.run(run(args.days, args.window_hours, args.symbol, out))


if __name__ == "__main__":
    main()
