"""Backfill read-only do trace de recusas do broker em trades phantom.

Na 01/09 as colunas `error`/`retcodes`/`auto_trading_off` da tabela `trades`
ainda não existiam: quando o broker recusava as ordens no envio, o cleanup
`sent == 0` fechava a trade a 0.0 SEM razão de saída e o motivo (retcodes)
vivia só em memória. Este script encontra essas trades fantasma e grava o
trace durável a partir da evidência que ficou:

- o padrão phantom em si (fechada sem `exit_reason`, pnl 0.0 — os fechos
  reais carregam sempre a razão);
- os eventos `order_result` do journal (`execution_events`, quando existem)
  para confirmar `accepted=False` por perna e colher retcodes legíveis;
- SEM journal (pré-journal/desligado), as **notificações risk/system da
  hora** ("⚠️ AutoTrading DESLIGADO… retcode 10027", "Sinal X não
  executado…") e a **auditoria de ignoradas da hora** — o 10027 confirma
  o AutoTrading OFF (flag `auto_trading_off` como no motor) e o texto
  citado dá ao auditor a causa humana legível.

É read-only para a execução: só escreve em `trades` (atributos de auditoria),
nunca abre/fecha trades, não altera sinais nem toca no MT5.

`run_refusals_backfill()` é partilhado com o guard semanal do tick
(`state._check_refusals_backfill`): uma vez por semana volta a correr o
backfill automaticamente para nenhum fecho phantom ficar sem trace durável
mesmo que o alerta da altura tenha sido perdido.

Uso (--apply grava; sem ele é só pré-visualização):
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.backfill_trade_refusals
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.backfill_trade_refusals --apply
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from app.config import DB_PATH
from app.db import DB

# Retcodes legíveis nos textos das notificações/ignoradas (10xxx do MT5 ou
# os marcadores REMOTE_ERROR/NO_TICK do motor).
_RETCODE_RE = re.compile(r"\b(10\d{3}|REMOTE_ERROR|NO_TICK)\b")

# Pistas de recusa nas notificações risk/system e nas ignoradas da hora.
_REFUSAL_HINTS = (
    "não executado", "nao executado", "recusada", "rejected",
    "autotrading", "desligado", "worker", "ordem",
)


def _phantom_candidates(db: DB):
    """Trades fechadas com assinatura phantom (sem razão de saída, pnl 0.0)
    e ainda sem `error` gravado — candidatas a backfill."""
    return db.conn.execute(
        "SELECT id, signal_id, lane_id, symbol, direction, opened_at, closed_at "
        "FROM trades WHERE status = 'closed' "
        "AND COALESCE(exit_reason, '') = '' AND pnl = 0.0 "
        "AND COALESCE(error, '') = '' ORDER BY opened_at"
    ).fetchall()


def _journal_retcodes(db: DB, trade_id: str, n_legs: int) -> list:
    """Retcodes legíveis dos `order_result` do journal para a trade.

    Devolve uma lista com um valor por perna; quando o resultado remoto não
    trazia retcode legível (caso de 01/09) fica `?` — nunca inventa o número.
    """
    rows = db.conn.execute(
        "SELECT payload_json FROM execution_events "
        "WHERE trade_id = ? AND kind = 'order_result' ORDER BY id",
        (trade_id,),
    ).fetchall()
    retcodes = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            p = {}
        rc = p.get("retcode")
        retcodes.append(rc if isinstance(rc, int) else "?")
    while len(retcodes) < n_legs:
        retcodes.append("?")
    return retcodes[:n_legs]


def _window(opened_at: str) -> tuple:
    """Janela ISO [−15 min, +15 min] em torno do open da trade (UTC)."""
    try:
        dt = datetime.fromisoformat(str(opened_at))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        lo = (dt - timedelta(minutes=15)).isoformat()
        hi = (dt + timedelta(minutes=15)).isoformat()
        return lo, hi
    except (ValueError, TypeError):
        return "", ""


def _notification_evidence(db: DB, opened_at: str) -> dict:
    """Retcodes e causa das notificações risk/system da HORA da trade.

    Sem journal (pré-journal/desligado) era isto que ficava como evidência:
    o feed avisava em tempo real ("⚠️ Sinal X não executado na lane …",
    "⛔ AutoTrading DESLIGADO … retcode 10027", "🗑️ Ordem pendente
    cancelada…"). O 10027 confirma o AutoTrading OFF — a mesma flag que o
    motor grava ao vivo.
    """
    lo, hi = _window(opened_at)
    if not lo:
        return {"retcodes": [], "quote": "", "auto_trading_off": False}
    rows = db.conn.execute(
        "SELECT kind, text FROM notifications "
        "WHERE created_at BETWEEN ? AND ? ORDER BY id",
        (lo, hi),
    ).fetchall()
    retcodes, quotes, at_off = [], [], False
    for r in rows:
        if r["kind"] not in ("risk", "system"):
            continue
        text = r["text"] or ""
        retcodes.extend(_RETCODE_RE.findall(text))
        if any(h in text.lower() for h in _REFUSAL_HINTS):
            quotes.append(text.strip()[:140])
        if "10027" in text:
            at_off = True
    return {"retcodes": retcodes, "quote": quotes[0] if quotes else "",
            "auto_trading_off": at_off}


def _ignored_evidence(db: DB, opened_at: str) -> str:
    """Corroboração nas mensagens ignoradas da hora (auditoria do grupo).

    Conversa pura era descartada, mas uma mensagem com pistas de recusa na
    janela da trade ajuda a reconstruir o contexto de um fecho phantom
    pré-journal. Melhor esforço — nunca inventa nada.
    """
    lo, hi = _window(opened_at)
    if not lo:
        return ""
    rows = db.conn.execute(
        "SELECT raw_text FROM ignored_messages "
        "WHERE received_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 20",
        (lo, hi),
    ).fetchall()
    for r in rows:
        text = (r["raw_text"] or "").strip()[:140]
        if text and any(h in text.lower() for h in _REFUSAL_HINTS):
            return text
    return ""


def _normalize_retcodes(retcodes: list, n_legs: int) -> list:
    """Normaliza para um valor por perna; `[?]` quando não há evidência."""
    out = list(retcodes) or ["?"]
    while len(out) < n_legs:
        out.append("?")
    return out[:n_legs] if n_legs else out


def run_refusals_backfill(db: DB, apply: bool = True) -> dict:
    """Corre o backfill de recusas do broker (auditoria read-only).

    Encontra as trades fechadas com assinatura phantom sem `error` gravado,
    imprime o plano e — com `apply=True` — persiste o trace durável
    (`error`/`retcodes`/`auto_trading_off`) na trade. Os retcodes vêm do
    journal quando existe; sem journal, das notificações risk/system e
    ignoradas da hora (o 10027 liga a flag AutoTrading OFF como no motor).
    Devolve `{candidates, applied, plans}` para o CLI e para o guard semanal
    do tick. Nunca abre/fecha trades nem toca no MT5.
    """
    cands = list(_phantom_candidates(db))
    if not cands:
        return {"candidates": 0, "applied": 0, "plans": []}
    n_legs_by_trade = {
        row["id"]: db.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE trade_id = ?",
            (row["id"],),
        ).fetchone()["n"]
        for row in cands
    }
    plans = []
    for row in cands:
        n_legs = n_legs_by_trade.get(row["id"]) or 0
        retcodes = _journal_retcodes(db, row["id"], n_legs)
        journal_empty = not retcodes or all(str(rc) == "?" for rc in retcodes)
        notif = {"retcodes": [], "quote": "", "auto_trading_off": False}
        ev_quote = ""
        if journal_empty:
            notif = _notification_evidence(db, row["opened_at"])
            if notif["retcodes"]:
                retcodes = notif["retcodes"]
            ev_quote = _ignored_evidence(db, row["opened_at"])
        retcodes = _normalize_retcodes(retcodes, n_legs)
        if any(str(rc) == "10027" for rc in retcodes) \
                or notif["auto_trading_off"]:
            error = "AutoTrading desligado no terminal MT5 (retcode 10027)"
            auto_trading_off = True
        else:
            error = f"ordem MT5 recusada pelo broker (retcodes {retcodes})"
            auto_trading_off = False
        extra = []
        if journal_empty and notif["quote"]:
            extra.append(f"notificações: “{notif['quote']}”")
        if journal_empty and ev_quote:
            extra.append(f"ignoradas: “{ev_quote}”")
        if extra:
            error += " — " + "; ".join(extra)
        plans.append((row["id"], error, retcodes, auto_trading_off))
        print(
            f"[{row['lane_id'] or 'main'}] {row['id']} · {row['signal_id']} · "
            f"{row['symbol']} {row['direction']} @ {row['opened_at']} · "
            f"{n_legs} perna(s) recusada(s) · retcodes {retcodes}"
            + (" (journal)" if not journal_empty else " (notificações/ignoradas)")
        )
        print(f"    → error: {error}")
    if apply:
        for trade_id, error, retcodes, auto_trading_off in plans:
            db.set_trade_refusal(trade_id, error=error, retcodes=retcodes,
                                 auto_trading_off=auto_trading_off)
    return {"candidates": len(cands),
            "applied": len(plans) if apply else 0,
            "plans": plans}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="grava na BD (sem ele é só pré-visualização)")
    args = ap.parse_args()

    db = DB(DB_PATH)
    try:
        res = run_refusals_backfill(db, apply=args.apply)
        if not res["candidates"]:
            print("sem trades phantom por backfiliar — nada a fazer.")
            return 0
        if not args.apply:
            print(f"\n{res['candidates']} trade(s) por backfiliar — corre com "
                  "--apply para gravar (trace de auditoria, nunca execução).")
            return 0
        print(f"\n✅ {res['applied']} trade(s) atualizada(s) "
              "(error/retcodes/auto_trading_off).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())