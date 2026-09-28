"""Sweep read-only do candle-check dos recounts (R2).

Valida a plausibilidade da excursão/reversão reportada pelo grupo contra o
high/low REAL dos candles do dia (Yahoo 15m), para cada recount guardado em
`trade_recounts`. Responde a duas perguntas:

  * quantos recounts seriam marcados implausíveis (excursão acima do high do
    dia, reversão fora do range, etc.);
  * quantos ficam `checked=False` por falta de dados (sem símbolo associável,
    sem candles do dia) — esses nunca são declarados plausíveis nem
    implausíveis (o audit nunca inventa).

Nunca escreve na BD, nunca abre trades e não toca no servidor em produção.

Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.sweep_recount_candles \\
        [--limit 200] [--source "Golden Moves"]
"""

import argparse
import asyncio
import sys
from datetime import datetime

from app.config import DB_PATH
from app.db import DB


class _CandleChecker:
    """Holder mínimo com a superfície que `_recount_candle_check` usa
    (`self.db` para resolver o símbolo do dia e a janela por símbolo).
    Evita instanciar o BotState inteiro (que liga o market feed / MT5)."""

    def __init__(self, db: DB):
        self.db = db

    def _recount_window_hours(self, symbol: str = "") -> float:
        from app.state import BotState

        return BotState._recount_window_hours(self, symbol)

    async def check(self, audit: dict, row: dict, received_at: str) -> dict:
        # import local para não puxar o app.state inteiro no arranque
        from app.state import BotState

        return await BotState._recount_candle_check(self, audit, row, received_at)


async def _sweep(limit: int, source_like: str) -> None:
    from app.state import BotState  # noqa: F401  (valida a assinatura acima)

    db = DB(DB_PATH)
    checker = _CandleChecker(db)
    rows = db.list_trade_recounts(source_like=source_like, limit=limit)
    if not rows:
        print("Nenhum recount em `trade_recounts` — corre primeiro o backfill "
              "(app/backfill_recounts.py) ou espera por recounts novos.")
        return

    total = len(rows)
    checked = plausible = implausible = 0
    no_data = 0
    flagged = []
    for row in rows:
        audit = {
            "symbol": row.get("symbol") or "",
            "entry": row.get("entry"),
            "pips": row.get("pips"),
        }
        cc = await checker.check(audit, row, row.get("received_at") or "")
        if not cc.get("checked"):
            no_data += 1
            continue
        checked += 1
        if cc.get("plausible"):
            plausible += 1
        else:
            implausible += 1
            flagged.append((row, cc))

    print(f"Recounts analisados: {total}")
    print(f"  com candle-check: {checked} "
          f"({plausible} plausíveis, {implausible} implausíveis)")
    print(f"  sem dados (checked=False): {no_data} — nunca declarados")
    if flagged:
        print("\nImplausíveis (candidatos a falso relato do grupo):")
        for row, cc in flagged[:20]:
            rc = (row.get("raw_text") or "")[:70].replace("\n", " ")
            print(f"  [{row.get('received_at', '')[:10]}] {rc}")
            print(f"      exc_ok={cc.get('excursion_ok')} "
                  f"rev_ok={cc.get('reversal_ok')} "
                  f"day {cc.get('day_high')}/{cc.get('day_low')}")
        if len(flagged) > 20:
            print(f"  ... e mais {len(flagged) - 20}")
    if implausible:
        rate = implausible / max(checked, 1) * 100
        print(f"\nTaxa de implausibilidade: {rate:.1f}% "
              f"({implausible}/{checked}) — verificar se o relato do grupo "
              f"costuma bater com os candles reais.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200,
                        help="quantos recounts analisar (mais recentes)")
    parser.add_argument("--source", default="",
                        help="filtrar por fonte (ex.: 'Golden Moves')")
    args = parser.parse_args()
    asyncio.run(_sweep(args.limit, args.source))


if __name__ == "__main__":
    sys.exit(main())
