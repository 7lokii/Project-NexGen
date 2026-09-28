"""Estudo read-only: candle-check dos recounts com candles MT5 (spot) vs Yahoo GC=F.

O candle-check de produção usa o feed Yahoo (GC=F, futuros de ouro) porque o
servidor não depende do terminal; mas o grupo negocia spot e a divergência
futuros↔spot (dezenas de pontos) fabrica implausíveis. O MT5 da demo tem
candles de XAUUSD SPOT nativos — este estudo corre o MESMO check com os dois
feeds lado a lado e mede quantos implausíveis do Yahoo seriam plausíveis com
o spot real (falsos positivos do feed de futuros).

Veredictos por recount (idênticos aos do /auditoria):

  * Yahoo  : o check de produção (GC=F, com o offset calibrado se ativo);
  * MT5    : o mesmo check com candles M15 de XAUUSD do terminal (spot, sem
             offset — aplicar o offset de futuros a candles spot seria
             dupla correção).

Aggregado: implausíveis-no-Yahoo-que-passam-no-MT5 (falsos positivos do
feed), implausíveis em ambos, plausíveis em ambos, sem dados de um dos lados.
Read-only: nunca escreve na BD, nunca abre trades, não altera o servidor.
Requer o terminal MT5 a correr (senão degrada e só mostra o lado Yahoo).

Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_recount_mt5_feed \
        [--days 90] [--source "Golden Moves"]
"""

import argparse
import asyncio
import os
import sys

from app.config import DB_PATH
from app.db import DB


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


class _Holder:
    """Holder mínimo com a superfície que o candle-check usa (`self.db` e a
    janela por símbolo). Evita instanciar o BotState inteiro."""

    def __init__(self, db: DB):
        self.db = db

    def _recount_window_hours(self, symbol: str = "") -> float:
        from app.state import BotState

        return BotState._recount_window_hours(self, symbol)


async def _mt5_candles() -> list:
    """Candles M15 de XAUUSD spot do terminal (read-only). Devolve a mesma
    forma do Yahoo {ts, open, high, low, close}; [] sem terminal."""
    from app.execution.mt5 import MT5Provider

    creds = _mt5_credentials()
    provider = MT5Provider(login=creds["login"], server=creds["server"],
                           password=creds["password"])
    if not provider.initialize():
        print("[mt5] terminal indisponível — só o lado Yahoo será medido.")
        return []
    try:
        rates = provider.copy_rates("XAUUSD", timeframe=15, count=9000)
    finally:
        provider.shutdown()
    return [{"ts": int(r["time"]), "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]} for r in rates]


async def _study(days: int, source_like: str) -> None:
    from datetime import datetime, timedelta, timezone

    from app.execution.feed import YahooProvider
    from app.state import BotState  # noqa: F401  (métodos usados unbound)

    db = DB(DB_PATH)
    holder = _Holder(db)
    rows = db.list_trade_recounts(source_like=source_like, limit=20000)
    if not rows:
        print("Nenhum recount em `trade_recounts` — corre primeiro o backfill.")
        return
    # filtra por janela (recounts antigos sem candles em ambos os feeds)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = [r for r in rows if (r.get("received_at") or "")[:10] >= since]
    if not rows:
        print(f"Sem recounts nos últimos {days} dias.")
        return

    provider = YahooProvider()
    try:
        yahoo = await provider.history("XAUUSD", interval="15m", range_="3mo")
    except Exception:
        # a Yahoo limita o intervalo 15m a ~60 dias; cai para 1mo
        try:
            yahoo = await provider.history("XAUUSD", interval="15m",
                                           range_="1mo")
        except Exception as exc:
            print(f"sem candles GC=F: {exc}")
            return
    mt5 = await _mt5_candles()
    if not mt5:
        print("Sem candles MT5 — o estudo precisa do terminal a correr "
              "(ou o símbolo XAUUSD no Market Watch).")

    stats = {"both_plausible": 0, "both_implausible": 0,
             "yahoo_implausible_mt5_plausible": 0,  # falso positivo do feed
             "mt5_implausible_yahoo_plausible": 0,
             "yahoo_only_checked": 0, "no_data": 0}
    print(f"\nRecounts XAUUSD analisados: {len(rows)} "
          f"(candles: Yahoo GC=F {len(yahoo)} · MT5 spot {len(mt5)})\n")
    for row in rows:
        day = (row.get("received_at") or "")[:10]
        symbol, sig_time = BotState._recount_resolve(holder, row, day)
        if symbol != "XAUUSD":
            stats["no_data"] += 1
            continue
        audit = {"symbol": "XAUUSD", "entry": row.get("entry"),
                 "pips": row.get("pips")}
        cc_y = await BotState._recount_candle_check(
            holder, audit, row, row.get("received_at") or "",
            candles=yahoo, resolved_symbol=symbol, signal_time=sig_time or "",
        )
        cc_m = await BotState._recount_candle_check(
            holder, audit, row, row.get("received_at") or "",
            candles=mt5, resolved_symbol=symbol, signal_time=sig_time or "",
        )
        y_ok = cc_y.get("checked") and cc_y.get("plausible")
        m_ok = cc_m.get("checked") and cc_m.get("plausible")
        y_chk = bool(cc_y.get("checked"))
        m_chk = bool(cc_m.get("checked"))
        if not y_chk or not m_chk:
            stats["no_data"] += 1
            flag = f"  (Yahoo {'sem dados' if not y_chk else 'ok'} · " \
                   f"MT5 {'sem dados' if not m_chk else 'ok'})"
        elif y_ok and m_ok:
            stats["both_plausible"] += 1
            flag = "  ✓ ambos plausíveis"
        elif (not y_ok) and (not m_ok):
            stats["both_implausible"] += 1
            flag = "  ✗ implausível em AMBOS (relato suspeito mesmo em spot)"
        elif (not y_ok) and m_ok:
            stats["yahoo_implausible_mt5_plausible"] += 1
            flag = "  ⚠️ implausível no GC=F mas plausível no spot MT5 " \
                   "(falso positivo do feed de futuros)"
        else:
            stats["mt5_implausible_yahoo_plausible"] += 1
            flag = "  ? plausível no GC=F mas implausível no spot MT5"
        print(f"[{day}] #{row.get('signal_number')} {flag}")
        if y_chk and m_chk:
            w_y = cc_y.get("window_hours")
            print(f"      Yahoo: dia {cc_y.get('day_high')}/{cc_y.get('day_low')}"
                  f" · janela ±{w_y or 3}h {cc_y.get('window_high')}/"
                  f"{cc_y.get('window_low')} · exc {cc_y.get('excursion_ok')} · "
                  f"rev {cc_y.get('reversal_ok')}")
            print(f"      MT5  : dia {cc_m.get('day_high')}/{cc_m.get('day_low')}"
                  f" · janela ±{cc_m.get('window_hours') or 3}h "
                  f"{cc_m.get('window_high')}/{cc_m.get('window_low')} · exc "
                  f"{cc_m.get('excursion_ok')} · rev {cc_m.get('reversal_ok')}")
    print("\nAgregado:")
    for key, label in (
        ("both_plausible", "plausíveis em ambos os feeds"),
        ("both_implausible", "implausíveis em ambos (relato suspeito mesmo em spot)"),
        ("yahoo_implausible_mt5_plausible", "implausíveis no GC=F mas plausíveis no MT5 (falsos positivos do feed de futuros)"),
        ("mt5_implausible_yahoo_plausible", "plausíveis no GC=F mas implausíveis no MT5"),
        ("no_data", "sem dados de um dos feeds"),
    ):
        print(f"  {stats[key]:>3}  {label}")
    fp = stats["yahoo_implausible_mt5_plausible"]
    checked = stats["both_plausible"] + stats["both_implausible"] + fp \
        + stats["mt5_implausible_yahoo_plausible"]
    if checked and fp:
        print(f"\nFalsos positivos do feed GC=F: {fp}/{checked} "
              f"({fp / checked * 100:.0f}%) — a divergência futuros↔spot "
              f"marca implausíveis que o spot real explica.")
    elif checked:
        print("\nNenhum falso positivo do feed GC=F na amostra.")


async def _study_signals(days: int, group: str) -> None:
    """Re-baseline de falsos positivos com o feed certo: quantas entradas
    dos SINAIS reais cabem na janela do spot MT5 vs do GC=F.

    O baseline do candle-check (66% das entradas fora da janela ±3h no
    GC=F) mede o feed de futuros, não o mercado do grupo — este modo mede
    o MESMO conceito com o spot nativo do MT5: quantas entradas ficam
    dentro da    janela horária de cada feed e quantas divergem (entram no
    spot mas não no GC=F e vice-versa)."""
    from datetime import datetime, timedelta, timezone

    from app.backtest import _pip_size
    from app.execution.feed import YahooProvider

    db = DB(DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol == "XAUUSD" and s.direction and s.entry and not s.error
        and (s.received_at or "").startswith("20")
    ]
    if not signals:
        print("Sem sinais XAUUSD no período — nada a medir.")
        return
    provider = YahooProvider()
    try:
        yahoo = await provider.history("XAUUSD", interval="15m", range_="1mo")
    except Exception as exc:
        print(f"sem candles GC=F: {exc}")
        return
    mt5 = await _mt5_candles()
    if not mt5:
        print("Sem candles MT5 — o estudo precisa do terminal a correr.")
        return
    tol = _pip_size("XAUUSD") * 40
    window_h = 3  # default do candle-check (o mesmo do baseline original)
    win = window_h * 3600

    def _entry_fits(candles: list, sig_ts: float, entry: float) -> bool:
        return any(
            c["low"] - tol <= entry <= c["high"] + tol
            and abs(c["ts"] - sig_ts) <= win for c in candles)

    stats = {"both": 0, "spot_only": 0, "futures_only": 0, "neither": 0}
    print(f"\nSinais XAUUSD analisados: {len(signals)} "
          f"(janela ±{window_h}h, candles: GC=F {len(yahoo)} · spot {len(mt5)})\n")
    for s in signals:
        try:
            sig_ts = datetime.fromisoformat(s.received_at).timestamp()
        except (ValueError, TypeError):
            continue
        entry = float(s.entry)
        in_spot = _entry_fits(mt5, sig_ts, entry)
        in_fut = _entry_fits(yahoo, sig_ts, entry)
        if in_spot and in_fut:
            stats["both"] += 1
            flag = "✓ em ambos"
        elif in_spot:
            stats["spot_only"] += 1
            flag = "⚡ só no spot MT5 (o GC=F não toca a entrada — divergência do feed)"
        elif in_fut:
            stats["futures_only"] += 1
            flag = "? só no GC=F"
        else:
            stats["neither"] += 1
            flag = "✗ fora de ambos"
        print(f"  [{s.received_at[:16]}] {s.direction.upper()} @ {entry} {flag}")
    total = max(1, sum(stats.values()))
    print("\nAgregado (entradas dentro da janela ±3h):")
    for key, label in (
        ("both", "cabem no spot E no GC=F"),
        ("spot_only", "só no spot MT5 (falso positivo do GC=F)"),
        ("futures_only", "só no GC=F (falso negativo do spot)"),
        ("neither", "fora de ambos (verdadeiros implausíveis)"),
    ):
        print(f"  {stats[key]:>3}  {label}")
    outside_fut = stats["neither"] + stats["spot_only"]
    outside_spot = stats["neither"] + stats["futures_only"]
    print(f"\nBaseline com o GC=F (feed de produção): "
          f"{outside_fut / total * 100:.1f}% das entradas fora da janela")
    print(f"Baseline com o spot MT5: {outside_spot / total * 100:.1f}% "
          f"(falsos positivos eliminados: "
          f"{stats['spot_only'] / total * 100:.1f}% — entradas que só o spot "
          f"explica)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="janela de dias a analisar")
    parser.add_argument("--source", default="",
                        help="filtrar por fonte (ex.: 'Golden Moves')")
    parser.add_argument("--signals", action="store_true",
                        help="medir os SINAIS reais (re-baseline spot vs GC=F) "
                             "em vez dos recounts")
    parser.add_argument("--group", default="Golden Moves",
                        help="grupo a filtrar no modo --signals")
    args = parser.parse_args()
    if args.signals:
        asyncio.run(_study_signals(args.days, args.group))
    else:
        asyncio.run(_study(args.days, args.source))


if __name__ == "__main__":
    sys.exit(main())
