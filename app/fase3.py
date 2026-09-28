"""Fase 3 — janela de validação e veredicto (extraído de app/state.py).

Funções standalone que só precisam da BD (testáveis sem o BotState inteiro):

* `_fase3_window_trades`  — as primeiras N trades fechadas da auto após a
  promoção (o contrato de validação é a janela *inicial*, não "as últimas").
* `_compute_fase3_verdict` — veredicto da janela (PF real, mesma decisão do
  papel ≥ 9/10, zero SL em falta, zero ordens duplicadas, retcodes 10009,
  atraso sinal→abertura ≤ 2 min, P&L coerente com o backtest).

`app/state.py` re-exporta ambos com os mesmos nomes (os testes/estudos
importam de `app.state`), por isso este módulo é um corte puro, sem mudar
comportamento.
"""

import json
from typing import List, Optional

from app.db import DB
from app.execution.engine import _now_iso
from app.models import Trade


def _fase3_window_trades(db: DB, sid: str, promoted_at: str,
                         limit: int = 10) -> List[Trade]:
    """Return the first validation-window trades opened by ``sid``.

    The validation contract is the *first* ten trades after promotion, not
    every trade that happened to close after that timestamp.  Filtering by
    opening time prevents a position from the previous strategy from being
    counted after it closes; explicit strategy ids are honoured while legacy
    rows without one remain usable for historical/test data.
    """
    if not promoted_at:
        return []
    closed = [
        t for t in db.list_trades(500)
        if t.status == "closed" and t.closed_at and t.opened_at
        and t.opened_at >= promoted_at and t.closed_at >= promoted_at
        and t.strategy_id in (None, sid)
    ]
    closed.sort(key=lambda t: (t.closed_at or "", t.id))
    return closed[:max(1, int(limit))]


async def _compute_fase3_verdict(db: DB, sid: Optional[str]) -> Optional[dict]:
    """Veredicto da janela de validação da Fase 3 (ESTRATEGIAS.md Parte X)
    — standalone (só precisa da BD) para ser testável sem o BotState inteiro.

    Avalia as 10 primeiras trades reais da auto (fechadas após `promoted_at`
    do snapshot) e devolve o veredicto com as métricas do doc: PF real da
    janela, mesma decisão de saída que o papel (≥ 9/10), posições sem SL,
    ordens duplicadas (perna com o mesmo `leg`), modifies recusados pelo
    broker (retcodes ≠ 10009/0 na auditoria `autosl:`), atraso
    sinal→abertura ≤ ~2 min e P&L coerente com o backtest do mesmo sinal.

    `None` quando ainda não há janela para avaliar (estratégia não é auto,
    sem snapshot, veredicto já emitido ou < 10 trades fechadas)."""
    import json as _json
    from app.strategies import STRATEGIES as _S
    if not sid or not sid.startswith("auto_"):
        return None
    _strat = _S.get(sid, {})
    if not (_strat.get("stair_levels") or _strat.get("trail_factor")
            or _strat.get("confirm_filter") or _strat.get("time_stop_minutes")):
        return None
    try:
        snap = json.loads(db.all_config().get("fase3_snapshot") or "null")
    except (ValueError, TypeError):
        snap = None
    if not snap:
        return None
    if db.all_config().get("fase3_verdict"):
        return None
    promoted_at = snap.get("promoted_at") or ""
    closed = _fase3_window_trades(db, sid, promoted_at)
    if len(closed) < 10:
        return None
    # PF real da janela
    pnls = [t.pnl or 0.0 for t in closed]
    wins = sum(p for p in pnls if p > 0)
    loss_sum = abs(sum(p for p in pnls if p < 0))
    pf = round(wins / loss_sum, 2) if loss_sum else None
    # divergências estruturais no mesmo sinal (saída diferente / papel ainda
    # aberto enquanto o real já fechou)
    from app.analysis import paper_vs_real_auto
    try:
        pvr = await paper_vs_real_auto(db, strategy_id=sid)
        window_signal_ids = {t.signal_id for t in closed}
        pairs = [p for p in pvr.get("pairs", [])
                 if (p.get("real_closed_at") or "") >= promoted_at
                 and p.get("signal_id") in window_signal_ids]
        structural = [p for p in pairs if not p.get("same_exit") or p.get("paper_open")]
        same_exit_n = sum(1 for p in pairs if p.get("same_exit"))
    except Exception as exc:
        print(f"[fase3] divergences ao validar janela: {exc}")
        structural = []
        same_exit_n = 0
    # posições abertas sem SL (zero tolerado na janela)
    no_sl = [p for p in db.open_positions() if not p.stop_loss]

    # "zero ordens duplicadas e retcodes 10009" (doc Parte X.1):
    # - ordens duplicadas = mais de uma posição com o MESMO leg na mesma
    #   trade (o `leg` é o comentário único `trade.id:legN` no broker);
    # - retcodes recusados = modifies da auto-gestão recusados (10016/stops
    #   inválidos) na auditoria `autosl:<trade_id>` — aceite é 10009/0.
    dup_orders, refused_retcodes = [], []
    slow_opens, pnl_incoherent = [], []
    by_signal = {s.id: s for s in db.signals_since("")}
    from app.analysis import real_vs_backtest
    try:
        rvb = await real_vs_backtest(db, strategy_id=sid)
        rvb_by_trade = {r["trade_id"]: r for r in rvb.get("trades", [])}
    except Exception:
        rvb_by_trade = {}
    for t in closed:
        legs_seen = {}
        for p in db.positions_for_trade(t.id):
            if p.leg in legs_seen:
                dup_orders.append((t.id, p.leg))
            legs_seen[p.leg] = True
        try:
            for e in _json.loads(db.all_config().get(f"autosl:{t.id}") or "[]"):
                rc = e.get("retcode")
                if rc is not None and int(rc or 0) not in (10009, 0):
                    refused_retcodes.append((t.id, e.get("leg"), rc))
        except (ValueError, TypeError):
            pass
        # atraso sinal→abertura ≤ ~2 min (o sim assume 60s; acima de 120s o
        # sinal já pode estar desatualizado)
        sig = by_signal.get(t.signal_id)
        if sig and sig.received_at and t.opened_at:
            try:
                from datetime import datetime as _dt
                t0 = _dt.fromisoformat(str(sig.received_at).replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(str(t.opened_at).replace("Z", "+00:00"))
                delay = (t1 - t0).total_seconds()
                if delay > 120:
                    slow_opens.append((t.id, int(delay)))
            except (ValueError, TypeError):
                pass
        # P&L coerente com o backtest do mesmo sinal: sinal oposto =
        # incoerente; magnitudes muito desviadas também (real a perder > 150%
        # do previsto, ou a ganhar < 25% do previsto, não é ruído de custos)
        rv = rvb_by_trade.get(t.id) or {}
        bt_pnl = rv.get("bt_pnl")
        real_pnl = t.pnl or 0.0
        if bt_pnl is not None and abs(bt_pnl) >= 0.5:
            if (real_pnl > 0) != (bt_pnl > 0) and abs(real_pnl) >= 1.0:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))
            elif real_pnl < 0 and real_pnl < 1.5 * bt_pnl:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))
            elif real_pnl > 0 and real_pnl < 0.25 * bt_pnl:
                pnl_incoherent.append((t.id, real_pnl, bt_pnl))

    reasons = []
    if pf is not None and pf < 1.0:
        reasons.append(f"PF {pf:.2f} < 1.0 nas {len(closed)} trades")
    if same_exit_n < 9:
        reasons.append(f"mesma decisão do papel em {same_exit_n}/{len(closed)} "
                       "trades (< 9/10)")
    if len(structural) >= 2:
        reasons.append(f"{len(structural)} divergências estruturais "
                       "(saída diferente do papel no mesmo sinal)")
    if no_sl:
        reasons.append(f"{len(no_sl)} posições abertas sem SL")
    if dup_orders:
        reasons.append(f"{len(dup_orders)} ordens duplicadas "
                       "(perna re-enviada no mesmo leg)")
    if refused_retcodes:
        codes = sorted({r[2] for r in refused_retcodes})
        reasons.append(f"{len(refused_retcodes)} modifies recusados pelo "
                       f"broker (retcodes {codes})")
    if slow_opens:
        reasons.append(f"{len(slow_opens)} aberturas com atraso sinal→"
                       f"abertura > 2 min (máx {max(d for _, d in slow_opens)}s)")
    if pnl_incoherent:
        reasons.append(f"{len(pnl_incoherent)} trades com P&L incoerente "
                       "com o backtest do mesmo sinal")
    decision = "rollback" if reasons else "maintain"
    same_pct = (round(same_exit_n / len(closed) * 100) if closed else None)
    return {
        "evaluated_at": _now_iso(),
        "strategy_id": sid,
        "window_trades": len(closed),
        "pf": pf,
        "pnl": round(sum(pnls), 2),
        "same_exit_n": same_exit_n,
        "same_exit_pct": same_pct,
        "structural_divergences": len(structural),
        "positions_without_sl": len(no_sl),
        "duplicate_orders": len(dup_orders),
        "refused_retcodes": len(refused_retcodes),
        "slow_opens": len(slow_opens),
        "pnl_incoherent": len(pnl_incoherent),
        # Generation token: a background validation can finish after a new
        # promotion starts. `_check_fase3_validation` rechecks this timestamp
        # before persisting, so an old verdict cannot overwrite the fresh
        # window's cleared state.
        "snapshot_promoted_at": promoted_at,
        "decision": decision,
        "reason": ("; ".join(reasons) if reasons else f"janela limpa — mesma decisão "
                   f"do papel em {same_exit_n}/{len(closed)} trades "
                   f"({same_pct}%), zero posições sem SL, zero ordens "
                   f"duplicadas, retcodes 10009, atraso sinal→abertura ≤ 2 "
                   f"min, P&L coerente com o backtest, PF ≥ 1.0"),
        "snapshot_before": {
            k: snap.get(k) for k in ("promoted_at", "n", "pf", "pnl",
                                     "dd_auto", "dd_ref",
                                     "paper_vs_real_pairs",
                                     "real_trades_closed", "sl_audit_entries")
        },
    }
