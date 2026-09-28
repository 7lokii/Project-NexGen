"""Estudo read-only — cap honesto de perseguição (`entry_guard_auto_cap`).

Pergunta: com a distribuição REAL das distâncias dos fills (zona vs preço no
momento da colocação), que `entry_guard_auto_cap` corta as piores perseguições
sem perder os fills que entram?

Método (sem escrever na BD, sem abrir trades, sem tocar na config):
  1. Lê as trades REAIS MT5 (mt5-real/mt5-dry) da janela e mede a distância de
     cada fill à zona usando a MESMA função do relatório 'porquê sem trade'
     (`app.analysis._fills_zone_analysis`):
       - fonte primária: `trade.entry_distance` — o guard com o spot MT5 no
         momento da execução (autoritativo);
       - fallback: candles Yahoo (GC=F diverge do spot para metais — offset
         conhecido, documentado no AGENTS.md).
  2. Distribuição real (quantis) + tabela de caps candidatos (em pips): para
     cada cap, quantos fills se mantêm vs quantos se cortam.
  3. Recomendação honesta: o menor cap que mantém ≥ 90% dos fills (corta só a
     cauda das piores perseguições), convertido também em × largura da zona
     (a unidade da config) quando há zonas com largura > 0.

Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_persecution_cap \
        [--days 30] [--out reports/persecution_cap_<data>.json]

Saída: distribuição + tabela + veredicto. Read-only: nunca escreve na BD.
"""

import argparse
import asyncio
import json
import math
import sys
from datetime import date, timedelta

from app.analysis import _fills_zone_analysis
from app.config import DB_PATH
from app.db import DB

CANDIDATE_CAPS = [0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0,
                  30.0, 40.0, 50.0, 75.0, 100.0]

# Config atual de produção (referência no veredicto)
CURRENT_CAP_WIDTHS = 0.0       # 0 = limite máximo (5× a largura da zona)
CURRENT_TOLERANCE = 0.5        # pips: banda de execução automática a mercado
MIN_KEEP_RATIO = 0.90          # o cap recomendado mantém ≥ 90% dos fills


def quantiles(values, ps=(0.25, 0.50, 0.75, 0.90, 0.95)) -> dict:
    """Quantis lineares da distribuição (None se vazia)."""
    if not values:
        return {f"p{int(p * 100)}": None for p in ps}
    s = sorted(float(v) for v in values)
    out = {}
    for p in ps:
        k = (len(s) - 1) * p
        f, c = math.floor(k), math.ceil(k)
        if f == c:
            out[f"p{int(p * 100)}"] = round(s[int(k)], 2)
        else:
            out[f"p{int(p * 100)}"] = round(
                s[f] + (s[c] - s[f]) * (k - f), 2)
    return out


def recommend(distances, in_zone_flags, widths, pnls=None,
              min_keep_ratio=MIN_KEEP_RATIO) -> dict:
    """Recomenda um cap honesto a partir da distribuição REAL dos fills.

    `distances`: distância de cada fill à zona (pips, fonte guard spot);
    `in_zone_flags`: True se o fill entrou dentro da zona;
    `widths`: largura da zona de cada fill (para converter em × largura);
    `pnls`: P&L real de cada fill (para medir o que cada cap corta em €).

    A honestidade é a parte difícil: cada perseguição que entrou É um fill —
    não há cap que corte perseguições SEM perder os fills que entraram. A
    escolha defensável é cortar SÓ a cauda (as piores): o menor cap que
    mantém ≥ `min_keep_ratio` dos fills, e o veredicto mostra o P&L do que
    cada cap cortaria (um cap que corta só perdedoras é honesto; um que
    corta ganhadoras é um teto de segurança, não uma correção). Sem
    perseguições reais, nada a cortar — manter o default.
    """
    total = len(distances)
    rows = []
    for cap in CANDIDATE_CAPS:
        kept = sum(1 for d in distances if d is not None and d <= cap)
        kept_in_zone = sum(1 for i, d in enumerate(distances)
                           if in_zone_flags[i] and d is not None and d <= cap)
        cut_pnl = None
        if pnls:
            cut_pnl = round(sum(
                float(pnls[i] or 0.0)
                for i, d in enumerate(distances)
                if d is not None and d > cap), 2)
        rows.append({
            "cap_pips": cap,
            "fills_kept": kept,
            "fills_cut": total - kept,
            "pct_kept": round(kept / total * 100, 1) if total else None,
            "kept_in_zone": kept_in_zone,
            "cut_pnl": cut_pnl,
        })
    if total == 0:
        return {
            "total_fills": 0, "chases": 0, "in_zone": 0,
            "quantiles": {}, "rows": rows,
            "recommended_cap_pips": None, "recommended_cap_widths": None,
            "verdict": ("Sem fills reais MT5 na janela — nada a calibrar. "
                        "Manter o default (cap 0 = 5× a largura da zona)."),
        }
    chases = sum(1 for z in in_zone_flags if not z)
    in_zone = total - chases
    dists = [float(d) for d in distances if d is not None]
    q = quantiles(dists)
    if chases == 0:
        rec_pips = None
        rec_widths = None
        verdict = (
            f"{chases} perseguições reais na janela (medidas pelo guard spot): "
            f"todos os {total} fills entraram dentro/ao lado da zona "
            f"(máx {max(dists) if dists else 0:.2f} pips). Nada a cortar — um cap "
            "mais apertado só arriscaria perder fills. Manter o default "
            "(cap 0 = 5× a largura da zona)."
        )
    else:
        # cauda: corta as piores perseguições mantendo ≥ 90% dos fills
        rec = next((r for r in rows
                    if r["pct_kept"] is not None and r["pct_kept"] >= min_keep_ratio * 100),
                   rows[-1])
        rec_pips = rec["cap_pips"]
        # conversão para × largura (unidade da config): usa a mediana das
        # larguras > 0; zonas de largura zero (formato atual de entrada única)
        # deixam o guard `not_applicable` — o cap em × largura nunca aplica.
        w_pos = [w for w in widths if w and w > 0]
        if w_pos:
            med_w = sorted(w_pos)[len(w_pos) // 2]
            rec_widths = round(rec_pips / med_w, 2)
        else:
            rec_widths = None
        cut_amt = rec.get("cut_pnl")
        cut_txt = (f"Os fills cortados somam {cut_amt:+.2f} € — cortá-los "
                   "melhora o resultado." if cut_amt is not None and cut_amt < 0
                   else f"Atenção: os fills cortados somam {cut_amt:+.2f} € — "
                        "cortá-los perde dinheiro já ganho; este cap é um teto "
                        "de segurança, não uma correção de perseguição."
                   if cut_amt is not None else "")
        verdict = (
            f"{chases} perseguição(ões) real(ais) na janela (máx "
            f"{max(dists):.2f} pips; p90 {q.get('p90')}). Recomendação honesta: "
            f"cap {rec_pips} pips — mantém {rec['pct_kept']}% dos fills "
            f"({rec['kept_in_zone']}/{in_zone} na zona) e corta só a cauda "
            f"das piores. {cut_txt} "
            + (f"Em × largura da zona (mediana {med_w:.2f}): "
               f"entry_guard_auto_cap ≈ {rec_widths}."
               if rec_widths else
               "As zonas atuais têm largura 0 (entrada única) — o guard fica "
               "not_applicable e o cap em × largura nunca aplica; um teto "
               "absoluto exigiria suportar o cap em pips na config.")
            + " Nota: distâncias medidas pelo guard (spot MT5); o GC=F do Yahoo "
              "diverge do spot e inflaciona a leitura (divergência de feed)."
        )
    return {
        "total_fills": total, "chases": chases, "in_zone": in_zone,
        "max_distance": round(max(dists), 2) if dists else None,
        "quantiles": q, "rows": rows,
        "recommended_cap_pips": rec_pips,
        "recommended_cap_widths": rec_widths,
        "verdict": verdict,
    }


async def load_candles(symbols, days):
    """Spot MT5 quando o terminal está ligado; fallback Yahoo (futuros metais)."""
    from app.execution.feed import YahooProvider
    interval, range_ = ("15m", "1mo") if days <= 30 else ("1h", "3mo")
    provider = YahooProvider()
    out, sources = {}, {}
    for symbol in symbols:
        try:
            out[symbol] = await provider.history(symbol, interval=interval,
                                                 range_=range_)
            sources[symbol] = "yahoo"
        except Exception as exc:
            print(f"[estudo] sem histórico para {symbol}: {exc}", file=sys.stderr)
    print(f"Fonte de candles por símbolo: {sources}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db = DB(DB_PATH)
    since = (date.today() - timedelta(days=max(1, args.days))).isoformat()
    all_sig = db.signals_since(since)
    symbols = {s.symbol for s in all_sig
               if s.symbol and s.direction and s.entry}
    candles = asyncio.run(load_candles(symbols, args.days))

    fz = _fills_zone_analysis(db, all_sig, since, candles)
    distances = [f["distance"] for f in fz["rows"]]
    in_zone = [f["in_zone"] for f in fz["rows"]]
    widths = []
    for f in fz["rows"]:
        z = f.get("zone")
        if isinstance(z, list) and len(z) == 2 and z[0] is not None \
                and z[1] is not None:
            widths.append(abs(float(z[1]) - float(z[0])))
        else:
            widths.append(0.0)
    rec = recommend(distances, in_zone, widths,
                    pnls=[f["pnl"] for f in fz["rows"]])

    print(f"Fills reais ({args.days}d): {rec['total_fills']} "
          f"(na zona {rec['in_zone']} · perseguições {rec['chases']} · "
          f"máx {rec['max_distance']})")
    print(f"Quantis: {rec['quantiles']}")
    print("Caps candidatos (pips):")
    for r in rec["rows"]:
        mark = "  ← recomendado" if r["cap_pips"] == rec["recommended_cap_pips"] else ""
        cut = f" · corta P&L {r['cut_pnl']:+.2f} €" if r["cut_pnl"] is not None else ""
        print(f"  cap {r['cap_pips']:>6}: mantém {r['fills_kept']:>3}/{rec['total_fills']} "
              f"fills ({r['pct_kept']}%) · corta {r['fills_cut']}{cut}{mark}")
    print(f"\nVeredicto: {rec['verdict']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({
                "days": int(args.days), "since": since,
                "current": {"cap_widths": CURRENT_CAP_WIDTHS,
                            "tolerance": CURRENT_TOLERANCE},
                "fills_zone_summary": {k: fz[k] for k in
                                       ("total_fills", "with_zone", "in_zone",
                                        "chases", "pct_in_zone", "max_distance",
                                        "source_guard", "source_candles")},
                "recommendation": rec,
            }, fh, ensure_ascii=False, indent=2)
        print(f"\nRelatório: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
