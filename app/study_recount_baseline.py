"""Estudo read-only: baseline de falsos positivos do candle-check (R2).

O candle-check valida os RELATOS do grupo (recounts). Mas e se o próprio
filtro for demasiado agressivo — marcar implausível o que os candles até
explicam? Este estudo corre o MESMO `_recount_candle_check` sobre os SINAIS
REAIS do grupo (não recounts): para cada sinal dos últimos N dias com
entrada/direção/hora conhecidas, monta um \"recount sintético\" a partir do
próprio sinal (entrada real, excursão/reversão = high/low da janela real) e
mede quantos seriam marcados implausíveis:

  * um sinal REAL marcado implausível com excursão/reversão reais = o filtro
    tem falsos positivos (não devia ter marcado o que os candles mostram);
  * a taxa de implausíveis é o limite inferior esperado do filtro sobre os
    relatos do grupo (se os relatos forem tão bons quanto os próprios
    sinais, esta é a taxa de falsos positivos).

Read-only: nunca escreve, nunca abre trades. Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_recount_baseline \\
        [--days 30] [--group \"Golden Moves\"]
"""

import argparse
import asyncio
import sys

from app.config import DB_PATH
from app.db import DB


class _Checker:
    """Holder mínimo com a superfície de `_recount_candle_check` (self.db)."""

    def __init__(self, db: DB):
        self.db = db

    async def check(self, audit: dict, row: dict, received_at: str) -> dict:
        from app.state import BotState

        return await BotState._recount_candle_check(self, audit, row, received_at)


async def _study(days: int, group: str, since: str, window_hours: float,
                 apply_offset: bool) -> None:
    from datetime import datetime, timedelta, timezone

    db = DB(DB_PATH)
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    signals = [
        s for s in db.signals_since(since)
        if (group.lower() in (s.source or "").lower())
        and s.symbol and s.direction and s.entry and not s.error
        and (s.received_at or "").startswith("20")
    ]
    if not signals:
        print("Sem sinais no período — nada a medir.")
        return

    from app.execution.feed import YahooProvider
    provider = YahooProvider()
    candles_by_symbol: dict = {}
    for symbol in {s.symbol for s in signals}:
        try:
            candles_by_symbol[symbol] = await provider.history(
                symbol, interval="15m", range_="1mo")
        except Exception as exc:
            print(f"[baseline] sem histórico para {symbol}: {exc}")
            candles_by_symbol[symbol] = []

    # offset GC=F↔spot opcional (a mesma calibração que o candle-check usa)
    from app.execution.evidence import _gold_futures_offset
    offsets = {}
    for symbol, candles in candles_by_symbol.items():
        offsets[symbol] = _gold_futures_offset(db, candles, symbol) \
            if apply_offset else None

    total = checked = implausible = no_data = 0
    flagged = []
    for s in signals:
        day = (s.received_at or "")[:10]
        try:
            sig_ts = datetime.fromisoformat(s.received_at).timestamp()
        except (ValueError, TypeError):
            no_data += 1
            continue
        day_start = datetime.fromisoformat(day + "T00:00:00+00:00")
        day_end_ts = int((day_start + timedelta(days=1)).timestamp())
        day_candles = [c for c in candles_by_symbol.get(s.symbol, [])
                       if day_start.timestamp() <= c["ts"] < day_end_ts]
        if not day_candles:
            no_data += 1
            continue
        # janela à volta da hora do sinal (a mesma convenção do check,
        # configurável — 3h default, 6h para o re-baseline)
        win = [c for c in day_candles
               if abs(c["ts"] - sig_ts) <= window_hours * 3600]
        if not win:
            no_data += 1
            continue
        checked += 1
        total += 1
        win_high = max(c["high"] for c in win)
        win_low = min(c["low"] for c in win)
        entry = float(s.entry)
        # relato sintético EXATO = range real da janela (o que o grupo
        # relataria se fosse preciso): a entrada e o range têm de caber
        from app.backtest import _pip_size
        pip = _pip_size(s.symbol)
        tol = pip * 40
        shift = offsets.get(s.symbol) or 0.0
        entry_ok = win_low - tol <= entry + shift <= win_high + tol
        if not entry_ok:
            # a entrada do sinal nem tocou a janela real — se o grupo
            # relatasse exatamente isto, o candle-check marcaria implausível
            implausible += 1
            flagged.append((s, {"window_high": round(win_high, 2),
                                "window_low": round(win_low, 2),
                                "entry_ok": False, "shift": shift}))
            continue
    label = f"±{window_hours:g}h" + (" + offset" if apply_offset else "")
    print(f"Sinais com candle-check (janela {label}): {checked} "
          f"(+{no_data} sem dados)")
    print(f"  plausíveis: {checked - implausible}")
    print(f"  implausíveis (FALSOS POSITIVOS do filtro): {implausible}")
    if checked:
        print(f"  taxa de falso positivo: {implausible / checked * 100:.1f}%")
    if any(offsets.values()):
        print("  offsets aplicados: " + ", ".join(
            f"{k}={v}" for k, v in offsets.items() if v))
    if flagged:
        print(f"\nSinais reais cuja entrada não tocou a janela real {label}:")
        for s, cc in flagged[:10]:
            rc = (s.raw_text or "")[:60].replace("\n", " ")
            print(f"  [{s.received_at[:10]}] {s.direction.upper()} @ {s.entry} "
                  f"· janela {cc.get('window_high')}/{cc.get('window_low')} "
                  f"(shift={cc.get('shift')}) | {rc}")
    print("\nInterpretação: o candle-check sobre os próprios sinais reais com "
          "relato sintético EXATO devia marcar ~0 implausíveis — qualquer taxa "
          "> 0% indica ruído do filtro (tolerância/fuso/símbolo). No caso do "
          "gold, o feed Yahoo serve GC=F (futuros) enquanto o grupo negocia "
          "spot — a divergência pode ser de dezenas de pontos, por isso uma "
          "taxa alta NÃO prova relato falso: um implausível deve ser cruzado "
          "com os fills do bot antes de concluir. Compara as janelas 3h/6h "
          "e com/sem offset para ver o efeito real da correção.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--since", default="",
                        help="data inicial (YYYY-MM-DD); substitui --days")
    parser.add_argument("--group", default="Golden Moves")
    parser.add_argument("--window-hours", type=float, default=3.0,
                        help="largura da janela ±N h à volta do sinal (default 3)")
    parser.add_argument("--apply-offset", action="store_true",
                        help="aplicar o offset GC=F↔spot calibrado (como o check)")
    args = parser.parse_args()
    asyncio.run(_study(args.days, args.group, args.since,
                       args.window_hours, args.apply_offset))


if __name__ == "__main__":
    sys.exit(main())
