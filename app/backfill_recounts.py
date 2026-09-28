"""Backfill read-only dos recounts antigos (R2).

Antes do R2, mensagens `Signal #N: Sell at X … +N pips` eram descartadas como
conversa (entravam em `ignored_messages` ou `failed`). Este script re-analisa
o histórico e insere em `trade_recounts` (com dedup por message_id) tudo o que
o `parse_recount` reconhecer.

É read-only para a EXECUÇÃO: só escreve na tabela de auditoria
`trade_recounts` (nunca abre/fecha trades, não altera sinais, não toca no MT5).

Uso (--apply grava; sem ele é só pré-visualização):
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m app.backfill_recounts \\
        [--apply] [--source "Golden Moves"]
"""

import argparse
import sys

from app.config import DB_PATH
from app.db import DB
from app.messages.recounts import parse_recount


def _candidates(db: DB, source_like: str):
    """Mensagens que parecem recounts mas ficaram fora de `trade_recounts`:
    as `ignored_messages` (conversa) e as `signals` falhadas/descartadas."""
    seen = set()
    if source_like:
        ignored = db.conn.execute(
            "SELECT * FROM ignored_messages WHERE source LIKE ? "
            "ORDER BY received_at ASC", (f"%{source_like}%",),
        ).fetchall()
        failed = db.conn.execute(
            "SELECT id, source, raw_text, message_id, received_at FROM signals "
            "WHERE source LIKE ? AND (status='failed' OR status='ignored' OR "
            "status='rejected') ORDER BY received_at ASC",
            (f"%{source_like}%",),
        ).fetchall()
    else:
        ignored = db.conn.execute(
            "SELECT * FROM ignored_messages ORDER BY received_at ASC").fetchall()
        failed = db.conn.execute(
            "SELECT id, source, raw_text, message_id, received_at FROM signals "
            "WHERE (status='failed' OR status='ignored' OR status='rejected') "
            "ORDER BY received_at ASC").fetchall()

    out = []
    for row in ignored:
        mid = str(row["message_id"] or "")
        key = mid or (row["source"] or "") + "|" + (row["raw_text"] or "")[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append({"message_id": mid, "source": row["source"] or "",
                    "raw_text": row["raw_text"] or "",
                    "received_at": row["received_at"] or ""})
    for row in failed:
        mid = str(row["message_id"] or "")
        key = mid or (row["source"] or "") + "|" + (row["raw_text"] or "")[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append({"message_id": mid, "source": row["source"] or "",
                    "raw_text": row["raw_text"] or "",
                    "received_at": row["received_at"] or ""})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="gravar em trade_recounts (sem ele: só mostra)")
    parser.add_argument("--source", default="",
                        help="filtrar por fonte (ex.: 'Golden Moves')")
    args = parser.parse_args()

    db = DB(DB_PATH)
    candidates = _candidates(db, args.source)
    matched = 0
    already = 0
    for cand in candidates:
        fields = parse_recount(cand["raw_text"]) or {}
        if not fields:
            continue
        if cand["message_id"] and db.recount_by_message_id(cand["message_id"]):
            already += 1
            continue
        matched += 1
        line = cand["raw_text"][:90].replace("\n", " ")
        print(f"  [{'GRAVAR' if args.apply else 'pode-gravar'}] "
              f"{cand['received_at'][:10]} {line}")
        if args.apply:
            db.insert_trade_recount(
                cand["source"], fields, cand["raw_text"],
                cand["received_at"], cand["message_id"],
            )
    print(f"\nCandidatos 'Signal #N' reconhecidos: {matched} "
          f"({already} já em trade_recounts)")
    if matched and not args.apply:
        print("Sem --apply nada foi gravado. Repete com --apply para inserir.")


if __name__ == "__main__":
    sys.exit(main())
