"""Vigia do event loop — `app/stallwatch.py` + tabela `loop_stall_events`.

A fita explica os "sem resposta HTTP" do healthcheck com dados: quando o loop
congelou (kind='stall', atraso do wakeup do monitor) e, nas fases
instrumentadas (kind='phase'), ONDE (tick.update:<lane>, snapshot.build,
diagnostics.on_loop). Contratos:
  - nada é gravado abaixo do limiar (overhead ~0 quando o loop está saudável);
  - ≥ limiar grava com o timestamp/duration/threshold corretos;
  - o mesmo (kind, phase) é rate-limited (`min_gap`) para um padrão crónico
    não encher a BD;
  - a task de vigia deteta um congelamento REAL do loop (atraso de wakeup);
  - recentes/contagem/purge bounded (14 dias) funcionam na BD.
"""

import asyncio
import time

from app.db import DB
from app.stallwatch import LoopStallRecorder, run_loop_stall_watch


def _recorder(tmp_path, threshold: float = 2.0, min_gap: float = 0.0):
    db = DB(str(tmp_path / "stalls.db"))
    return db, LoopStallRecorder(db, threshold=threshold, min_gap=min_gap)


def test_recorder_so_grava_acima_do_limiar(tmp_path):
    db, rec = _recorder(tmp_path, threshold=2.0, min_gap=0.0)
    try:
        rec.record_phase("snapshot.build", 1.0)   # abaixo → nada
        rec.record_stall(1.5)                     # abaixo → nada
        assert db.recent_loop_stalls(10) == []
        rec.record_stall(3.2)                     # ≥ → grava
        rec.record_phase("tick.update:tp2", 2.1)  # ≥ → grava
        rows = db.recent_loop_stalls(10)
        assert len(rows) == 2
        by_kind = {r["kind"]: r for r in rows}
        assert by_kind["stall"]["phase"] == "event_loop"
        assert by_kind["stall"]["duration"] == 3.2
        assert by_kind["stall"]["threshold"] == 2.0
        assert by_kind["phase"]["phase"] == "tick.update:tp2"
        assert by_kind["phase"]["duration"] == 2.1
    finally:
        db.close()


def test_recorder_rate_limit_same_kind(tmp_path):
    """Um padrão crónico (stall a cada poucos segundos) não enche a BD:
    o mesmo (kind, phase) só volta a gravar após `min_gap` segundos."""
    db, rec = _recorder(tmp_path, threshold=1.0, min_gap=0.2)
    try:
        rec.record_stall(2.0)
        rec.record_stall(2.5)                            # dentro do gap → não
        rec.record_phase("snapshot.build", 3.0)          # outro kind → grava
        rec.record_phase("snapshot.build", 3.5)          # gap do mesmo phase
        rows = db.recent_loop_stalls(10)
        assert len(rows) == 2, rows
        kinds = {r["kind"] for r in rows}
        assert kinds == {"stall", "phase"}
    finally:
        db.close()


def test_detecao_real_de_congelamento_do_loop(tmp_path):
    """A task de vigia acorda tarde quando o loop congela — grava o stall.

    Bloqueia o loop 0.15s (time.sleep síncrono dentro do próprio loop) com
    limiar 0.1s e intervalo 0.02s: o wakeup seguinte chega ~0.15s atrasado e
    o registo aparece. Margens largas para não flakear em CI lenta."""
    db, rec = _recorder(tmp_path, threshold=0.1, min_gap=0.0)

    async def scenario():
        task = asyncio.create_task(run_loop_stall_watch(rec, interval=0.02))
        try:
            await asyncio.sleep(0.05)          # deixa o monitor assentar
            time.sleep(0.15)                   # congela o loop (síncrono)
            await asyncio.sleep(0.05)          # o monitor acorda atrasado
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(scenario())
        rows = db.recent_loop_stalls(10)
        assert rows, "o congelamento real devia ter sido gravado"
        stall = rows[0]
        assert stall["kind"] == "stall"
        assert stall["duration"] >= 0.1  # o atraso medido ≥ limiar
    finally:
        db.close()


def test_recentes_contagem_e_purge_bounded(tmp_path):
    from datetime import datetime, timedelta, timezone

    db, rec = _recorder(tmp_path, threshold=1.0, min_gap=0.0)
    try:
        rec.record_stall(1.5)
        rec.record_phase("snapshot.build", 1.2)
        rec.record_stall(2.0)
        assert len(db.recent_loop_stalls(2)) == 2
        assert db.loop_stall_count_since(
            (datetime.now(timezone.utc) - timedelta(hours=24)
             ).isoformat(timespec="seconds")) == 3
        # purge: linhas antigas (15 dias) desaparecem, as recentes ficam
        old = (datetime.now(timezone.utc) - timedelta(days=15)
               ).isoformat(timespec="seconds")
        db.record_loop_stall_event(old, "stall", "event_loop", 5.0, 1.0)
        assert db.purge_old_loop_stalls(14) == 1
        assert len(db.recent_loop_stalls(10)) == 3
    finally:
        db.close()
