"""Estudo read-only: recounts implausíveis vs fills reais do bot (R2).

Quando o candle-check marca um recount como implausível (a excursão/reversão
reportadas não cabem nos candles reais do dia/janela do sinal), há duas
hipóteses: o relato do grupo está errado, OU a execução do bot divergiu (o
bot negociou outro preço/símbolo/dia). Este estudo cruza cada recount
implausível com as trades reais fechadas do mesmo dia para responder quem
diverge:

  * **bot negociou o sinal** (fill real com entrada próxima) → o relato do
    grupo não bate com os candles, mas o bot tem fill: compara a entrada do
    bot com a reportada e os pips de cada lado;
  * **bot não negociou** → cruza com os sinais do dia (failed/pendente/kill)
    para apontar a causa provável;
  * **sem sinais nem trades no dia** → o relato referencia um dia que o bot
    não tem de todo.

Nunca escreve na BD, nunca abre trades. Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_recount_implausible \\
        [--limit 200] [--source "Golden Moves"]
"""

import argparse
import asyncio
import sys

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


def _no_trade_cause(db, day: str, direction: str, entry: float, tol: float) -> str:
    for s in db.signals_since(day):
        if (s.received_at or "")[:10] != day or not s.entry:
            continue
        if abs(float(s.entry) - entry) <= tol:
            if s.status in ("failed", "rejected"):
                return f"sinal do dia ficou {s.status} (formato/janela)"
            if s.status == "pending":
                return "sinal do dia ficou pendente (modo manual)"
            if s.direction and s.direction != direction:
                return (f"direção divergente no dia ({s.direction.upper()} vs "
                        f"{direction.upper()})")
    return "sem sinais nem trades do dia (kill switch/manual/outro feed)"


async def _study(limit: int, source_like: str) -> None:
    from app.state import BotState  # noqa: F401 (valida a assinatura)

    db = DB(DB_PATH)
    checker = _Checker(db)
    rows = db.list_trade_recounts(source_like=source_like, limit=limit)
    implausible = []
    for row in rows:
        audit = {"symbol": row.get("symbol") or "", "entry": row.get("entry"),
                 "pips": row.get("pips")}
        cc = await checker.check(audit, row, row.get("received_at") or "")
        if cc.get("checked") and cc.get("plausible") is False:
            implausible.append((row, cc))
    if not implausible:
        print("Sem recounts implausíveis — nada a cruzar com os fills.")
        return

    print(f"Recounts implausíveis: {len(implausible)}\n")
    traded = 0
    buckets = {"erro_relato": 0, "erro_execucao": 0, "sem_dados": 0}
    buckets_detail = {"erro_relato": [], "erro_execucao": [], "sem_dados": []}
    for row, cc in implausible:
        day = (row.get("received_at") or "")[:10]
        direction = row.get("direction") or ""
        entry = float(row.get("entry") or 0.0)
        reported = float(row.get("pips") or 0.0)
        symbol = cc.get("symbol") or ""
        pip = _pip_size(symbol) if symbol else 0.0001
        tol = pip * 40

        # 1) o bot fechou trades reais no dia com entrada próxima?
        trades = [t for t in db.trades_closed_on(day)
                  if t.direction == direction]
        best = min(trades, key=lambda t: abs(t.entry - entry)) if trades else None
        if best and abs(best.entry - entry) <= tol:
            traded += 1
            positions = db.positions_for_trade(best.id)
            sign = 1.0 if best.direction == "buy" else -1.0
            if positions and sum(float(p.lots or 0.0) for p in positions) > 0:
                lots = sum(float(p.lots or 0.0) for p in positions)
                weighted = sum(
                    float(p.lots or 0.0)
                    * (float(p.close_price or p.entry) - float(p.entry))
                    for p in positions
                )
                bot_pips = sign * weighted / lots / pip
            else:
                bot_pips = 0.0
            # o bot executou o MESMO sinal que o grupo relatou, mas o
            # candle-check não encaixa o relato → o relato do grupo diverge
            # dos candles (e o fill do bot pode também divergir)
            bucket = "erro_relato"
            detail = (f"bot negociou @ {best.entry} ({best.id}) — "
                      f"bot {bot_pips:+.0f} pips vs grupo {reported:+.0f} "
                      f"pips, dia {cc.get('day_high')}/{cc.get('day_low')}")
            verdict = (f"relato do grupo NÃO bate com os candles, mas o bot "
                       f"negociou @ {best.entry} ({best.id}) — "
                       f"bot {bot_pips:+.0f} pips vs grupo "
                       f"{reported:+.0f} pips, dia {cc.get('day_high')}/"
                       f"{cc.get('day_low')}")
        else:
            cause = _no_trade_cause(db, day, direction, entry, tol)
            verdict = f"bot NÃO negociou no dia — {cause}"
            if "sinal do dia ficou" in cause or "pendente" in cause \
                    or "direção divergente" in cause:
                # o bot tinha o sinal mas não o executou (rejected/pendente/
                # direção oposta) → a EXECUÇÃO é quem divergiu
                bucket = "erro_execucao"
                detail = cause
            else:
                # sem sinais nem trades no dia: não há como atribuir
                bucket = "sem_dados"
                detail = cause
        if cc.get("window_plausible") is not None:
            scope = (f"janela ±{cc.get('window_hours', 3)}h "
                     f"{cc.get('window_high')}/{cc.get('window_low')}")
        else:
            scope = f"dia {cc.get('day_high')}/{cc.get('day_low')}"
        buckets[bucket] += 1
        rc = (row.get("raw_text") or "")[:70].replace("\n", " ")
        buckets_detail[bucket].append((day, rc, scope, verdict))
        print(f"  [{day}] {rc}")
        print(f"      {scope} · {verdict}")
    print(f"\nCom fills reais do bot: {traded}/{len(implausible)}")
    total = sum(buckets.values())
    print(f"\nVeredicto agregado ({total} implausíveis):")
    for key, label in (("erro_relato", "erro de relato do grupo"),
                       ("erro_execucao", "erro de execução do bot"),
                       ("sem_dados", "sem dados para atribuir")):
        n = buckets[key]
        pct = n / total * 100 if total else 0.0
        print(f"  • {label}: {n} ({pct:.0f}%)")
    for key, label in (("erro_relato", "Erro de relato"),
                       ("erro_execucao", "Erro de execução"),
                       ("sem_dados", "Sem dados")):
        items = buckets_detail[key]
        if not items:
            continue
        print(f"\n  Top {label}:")
        for day, rc, scope, _v in items[:5]:
            print(f"    [{day}] {rc} · {scope}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    asyncio.run(_study(args.limit, args.source))


if __name__ == "__main__":
    sys.exit(main())
