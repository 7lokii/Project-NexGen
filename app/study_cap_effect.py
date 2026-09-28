"""Estudo read-only — efeito do `entry_guard_auto_cap` aplicado ao vivo.

Pergunta: depois de aplicar o cap autónomo (0.5× largura ≈ 2.0 pips com a
largura mediana 4.0), os fills reais mostram que o teto de segurança custa
ou poupa dinheiro? (Re-correr depois de alguns dias: `--since` parte os
fills em antes/depois do momento em que o cap entrou.)

Método (sem escrever na BD, sem abrir trades, sem tocar na config):
  1. Lê as trades reais MT5 (mt5-real/mt5-dry) da janela com a distância à
     zona do guard (spot — autoritativa) e a largura da zona.
  2. Parte os fills em antes/depois de `--since` (ISO do momento do cap).
  3. Para cada fill aplica a REGRA REAL do guard com o cap: passa se
     `distance ≤ tolerance` (mercado automático) ou
     `distance ≤ largura × cap_widths` (pendente na zona); além disso o
     guard teria REJEITADO (contrafactual — o fill não teria existido).
  4. Conta as rejeições novas (sinais rejeitados com "cap autónomo" na
     razão), partidas antes/depois de `--since`.
  5. Veredicto: custo (P&L dos fills que o cap teria cortado) vs poupança
     (rejeições novas), e se os fills restantes continuam a entrar.

Uso:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.study_cap_effect \
        [--days 30] [--since <ISO>] [--cap-widths 0.5] [--tolerance 2.0] \
        [--out reports/cap_effect_<data>.json]

Saída: tabela antes/depois + contrafactual + veredicto. Read-only.
"""

import argparse
import json
from datetime import date, datetime, timedelta

from app.config import DB_PATH
from app.db import DB

DEFAULT_CAP_WIDTHS = 0.5      # aplicado ao vivo a 2026-08-27
DEFAULT_TOLERANCE = 2.0       # config ao vivo (banda de mercado automático)
DEFAULT_MAX_WIDTHS = 5.0      # limite antigo quando cap_widths = 0


def passes_guard(distance, width, tolerance, cap_widths,
                 max_widths=DEFAULT_MAX_WIDTHS):
    """A regra REAL do guard: passa se dentro da tolerância (mercado
    automático) ou dentro do cap autónomo (ordem pendente na zona). Sem
    distância medida → passa (o estudo não corta o que não mede)."""
    if distance is None:
        return True
    if float(distance) <= tolerance:
        return True
    cap = (width * cap_widths if (cap_widths or 0.0) > 0
           else width * max_widths)
    return float(distance) <= cap


def decide_after(after_fills, cap_widths, tolerance, since_date="") -> str:
    """Decisão honesta a partir dos fills reais DEPOIS do cap.

    Um cap que corta fills com P&L negativo poupa dinheiro (MANTER, ou até
    apertar); um cap que corta ganhadores custa (REVERTER/afrouxar); sem
    fills pós-cap ainda não há decisão — só o contrafactual."""
    cut = [f for f in after_fills if not passes_guard(
        f["distance"], f["width"] or 1.0, tolerance, cap_widths)]
    cut_pnl = round(sum(f["pnl"] for f in cut), 2)
    if not after_fills:
        return (f"sem dados pós-cap ainda (fills depois de {since_date or '—'}: "
                f"0) — re-correr o estudo quando houver; entretanto o "
                f"contrafactual é a única leitura")
    if cut_pnl < 0:
        return (f"MANTER (o cap cortaria {len(cut)} fill(s) com "
                f"{cut_pnl:+.2f} € de perda evitada — o teto poupa)")
    if cut_pnl > 0:
        return (f"REVERTER/afrouxar (o cap cortaria {len(cut)} fill(s) com "
                f"{cut_pnl:+.2f} € de ganho perdido — o teto custa)")
    return "MANTER (sem fills além do cap no período pós-cap)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since", default="",
                    help="ISO do momento em que o cap entrou (parte antes/depois)")
    ap.add_argument("--cap-widths", type=float, default=DEFAULT_CAP_WIDTHS)
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    db = DB(DB_PATH)

    if not args.since:
        # sem marca explícita: usa o MOMENTO EXATO gravado quando o cap foi
        # alterado via /api/config (config `cap_applied_at`); sem ele, hoje
        # às 00:00 UTC (tudo antes)
        applied = (db.all_config() or {}).get("cap_applied_at")
        if applied:
            args.since = applied
        else:
            args.since = date.today().isoformat() + "T00:00:00+00:00"
    since_cap = datetime.fromisoformat(args.since)
    print(f"Momento do cap: {since_cap.isoformat()}")
    since = (date.today() - timedelta(days=max(1, args.days))).isoformat()
    never = ("pending_expired", "pending_orphan", "pending_cancelled")
    fills = []
    for t in db.trades_opened_since(since):
        if not (t.execution_mode or "").startswith("mt5"):
            continue
        if (t.exit_reason or "") in never:
            continue
        low = t.entry_zone_low
        high = t.entry_zone_high
        width = abs(float(high) - float(low)) if (low and high) else 0.0
        fills.append({
            "id": t.id, "symbol": t.symbol, "direction": t.direction,
            "entry": t.entry, "distance": t.entry_distance,
            "width": round(width, 2), "pnl": round(float(t.pnl or 0.0), 2),
            "opened_at": t.opened_at, "exit_reason": t.exit_reason,
            "style": t.entry_style,
        })
    fills.sort(key=lambda f: f["opened_at"] or "")

    before = [f for f in fills if _to_epoch(f["opened_at"]) < since_cap.timestamp()]
    after = [f for f in fills if _to_epoch(f["opened_at"]) >= since_cap.timestamp()]

    def _split_verdict(flist):
        passed = [f for f in flist if passes_guard(
            f["distance"], f["width"] or 1.0, args.tolerance, args.cap_widths)]
        cut = [f for f in flist if not passes_guard(
            f["distance"], f["width"] or 1.0, args.tolerance, args.cap_widths)]
        return {
            "fills": len(flist),
            "pass": len(passed),
            "cut": len(cut),
            "cut_pnl": round(sum(f["pnl"] for f in cut), 2),
            "pass_pnl": round(sum(f["pnl"] for f in passed), 2),
            "max_distance": round(max((f["distance"] or 0.0) for f in flist), 2)
            if flist else None,
        }

    verdict_before = _split_verdict(before)
    verdict_after = _split_verdict(after)
    # contrafactual TOTAL: se o cap tivesse estado ativo desde sempre
    counterfactual = _split_verdict(fills)

    # rejeições novas por cap (sinais rejeitados com a razão do cap)
    rej_before = rej_after = 0
    for s in db.signals_since(since):
        if s.status != "rejected" or not (s.error or ""):
            continue
        if "cap autónomo" not in s.error:
            continue
        if _to_epoch(s.received_at) < since_cap.timestamp():
            rej_before += 1
        else:
            rej_after += 1

    # decisão (a partir dos fills DEPOIS do cap — antes disso é contrafactual)
    decision = decide_after(after, args.cap_widths, args.tolerance,
                            since_cap.date().isoformat())

    # veredicto honesto
    cut_fills = [f for f in fills if not passes_guard(
        f["distance"], f["width"] or 1.0, args.tolerance, args.cap_widths)]
    if cut_fills:
        cut_lst = ", ".join(
            f"{f['id'][-6:]} {f['distance']:.2f}p {f['pnl']:+.2f}€"
            for f in cut_fills)
        verdict = (
            f"Contrafactual: o cap {args.cap_widths}× largura teria cortado "
            f"{len(cut_fills)} fill(s) ({counterfactual['cut_pnl']:+.2f} € em "
            f"P&L) — {cut_lst}. Rejeições novas por cap: {rej_after} "
            f"(antes: {rej_before}). Os fills restantes "
            f"({counterfactual['pass']}/{counterfactual['fills']}) continuam "
            "a entrar."
        )
    else:
        verdict = (
            f"Nenhum fill real da janela excede o cap {args.cap_widths}× "
            f"largura (tolerância {args.tolerance}) — nada a cortar; todos os "
            f"fills continuam a entrar. Rejeições novas por cap: {rej_after} "
            f"(antes: {rej_before})."
        )

    out = {
        "days": int(args.days), "since_cap": since_cap.isoformat(),
        "params": {"cap_widths": args.cap_widths,
                   "tolerance": args.tolerance,
                   "max_widths": DEFAULT_MAX_WIDTHS},
        "before": verdict_before, "after": verdict_after,
        "counterfactual": counterfactual,
        "rejections_by_cap": {"before": rej_before, "after": rej_after},
        "cut_fills": cut_fills,
        "decision": decision,
        "verdict": verdict,
    }

    print(f"Fills reais ({args.days}d): {len(fills)} "
          f"(antes do cap: {len(before)} · depois: {len(after)})")
    print(f"  antes:  {verdict_before}")
    print(f"  depois: {verdict_after}")
    print(f"  contrafactual (cap ativo desde sempre): {counterfactual}")
    print(f"Rejeições por cap: {rej_before} antes · {rej_after} depois")
    print(f"\nDecisão: {decision}")
    print(f"Veredicto: {verdict}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"\nRelatório: {args.out}")
    return 0


def _to_epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
