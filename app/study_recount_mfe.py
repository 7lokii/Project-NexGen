"""Estudo read-only: recounts do grupo vs MFE real dos candles (R3).

A definição de "win" do grupo é qualquer trade que ande 25–30 pips a favor
(MFE ≥ 25 pips na convenção R3) — mesmo que depois volte e feche em loss/BE.
Este estudo mede, para cada recount guardado em `trade_recounts`, o MFE REAL
que os candles do dia mostram a partir da entrada reportada e compara com os
pips reportados pelo grupo:

  * **concordância win**: o grupo reporta pips ≥ threshold e o MFE real dos
    candles também ≥ threshold (win real);
  * **falso win**: o grupo reporta win mas o MFE real dos candles fica abaixo
    do threshold (o relato exagera);
  * **win não reportado**: o grupo reporta menos que o threshold mas o MFE
    real é win (o grupo subestima / trade perdedora que andou a favor).

Usa a janela horária à volta do sinal (quando o sinal do dia é conhecido)
para não misturar sessões. Nunca escreve na BD, nunca abre trades.

Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_recount_mfe \\
        [--threshold 25] [--limit 200] [--source "Golden Moves"]
"""

import argparse
import asyncio
import sys
from datetime import datetime

from app.backtest import _pip_size
from app.config import DB_PATH
from app.db import DB


class _Checker:
    """Holder mínimo com a superfície de `_recount_candle_check` (self.db)."""

    def __init__(self, db: DB):
        self.db = db

    async def check(self, audit: dict, row: dict, received_at: str) -> dict:
        from app.state import BotState

        return await BotState._recount_candle_check(self, audit, row, received_at)


async def _study(threshold: float, limit: int, source_like: str) -> None:
    db = DB(DB_PATH)
    checker = _Checker(db)
    rows = db.list_trade_recounts(source_like=source_like, limit=limit)
    if not rows:
        print("Sem recounts em `trade_recounts` — corre o backfill primeiro.")
        return

    agree = false_win = missed_win = no_data = 0
    details = []
    for row in rows:
        direction = row.get("direction") or ""
        entry = float(row.get("entry") or 0.0)
        reported = float(row.get("pips") or 0.0)
        if not entry or not direction:
            continue
        audit = {"symbol": row.get("symbol") or "", "entry": entry,
                 "pips": reported}
        cc = await checker.check(audit, row, row.get("received_at") or "")
        if not cc.get("checked") or not cc.get("symbol"):
            no_data += 1
            continue
        symbol = cc["symbol"]
        pip = _pip_size(symbol)
        # MFE real dos candles: excursão máxima FAVORÁVEL à direção
        high = cc.get("window_high") if cc.get("window_high") is not None \
            else cc.get("day_high")
        low = cc.get("window_low") if cc.get("window_low") is not None \
            else cc.get("day_low")
        if direction == "buy":
            mfe = (high - entry) / pip
        else:
            mfe = (entry - low) / pip
        group_win = reported >= threshold
        real_win = mfe >= threshold
        if group_win and real_win:
            agree += 1
            tag = "✓ win bate"
        elif group_win and not real_win:
            false_win += 1
            tag = "✗ falso win"
        elif not group_win and real_win:
            missed_win += 1
            tag = "△ win não reportado"
        else:
            agree += 1
            tag = "✓ ambos perdem"
        details.append((tag, row, reported, round(mfe, 1), cc))
        print(f"  {tag}: Signal #{row.get('signal_number')} "
              f"{direction.upper()} @ {entry} reporta {reported:+.0f} pips, "
              f"MFE real {mfe:+.0f} pips "
              f"(janela {cc.get('window_high')}/{cc.get('window_low')} · "
              f"dia {cc.get('day_high')}/{cc.get('day_low')})")

    total = agree + false_win + missed_win
    print(f"\nRecounts com dados: {total} (+{no_data} sem dados)")
    if total:
        print(f"  concordância: {agree}/{total} ({agree / total * 100:.0f}%)")
        print(f"  falsos win (grupo diz win, candles não): "
              f"{false_win} ({false_win / total * 100:.0f}%)")
        print(f"  wins não reportados (candles win, grupo não): "
              f"{missed_win} ({missed_win / total * 100:.0f}%)")
        print(f"\nVeredicto: a definição de win do grupo (MFE ≥ {threshold:g} "
              f"pips) bate com a excursão real em {agree}/{total} "
              f"({agree / total * 100:.0f}%) dos recounts com candles.")
        if total < 20:
            print(f"Amostra pequena ({total} recounts) — o veredicto é "
                  f"indicativo, não estatístico. Re-correr quando houver mais "
                  f"recounts (o backfill e o R2 ao vivo vão acumulando).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=25.0,
                        help="pips mínimos da convenção de win do grupo (R3)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    asyncio.run(_study(args.threshold, args.limit, args.source))


if __name__ == "__main__":
    sys.exit(main())
