"""Vigia do event loop — stalls e fases lentas.

Quando o healthcheck regista "sem resposta HTTP" ou um login pendura, a causa
é o event loop ÚNICO congelado por I/O síncrono (MT5/worker/subprocessos).
Este módulo grava uma fita para ATRIBUIR esses episódios com dados, em vez de
investigação a posteriori:

  * kind='stall'  — a própria task de vigia acordou `duration` segundos tarde
    (o loop congelou esse tempo). A task dorme `interval` (0.5s) e mede o
    atraso real com `loop.time()` (monotónico).
  * kind='phase'  — fases on-loop instrumentadas (tick.update:<lane>,
    snapshot.build, diagnostics.on_loop, ...) que demoraram ≥ limiar — a
    CAUSA provável ao lado do sintoma.

Registos persistentes em `loop_stall_events` (bounded: purge de 14 dias no
watchdog do servidor) e expostos no `/api/diagnostics` (bloco "⏱️ Loop
stalls"). Limiar default 2.0s (`LOOP_STALL_THRESHOLD_SECONDS`); intervalo
default 0.5s (`LOOP_STALL_INTERVAL`). Read-only para execução: nunca decide
nada nem envia ordens.
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Optional

_THRESHOLD = float(os.getenv("LOOP_STALL_THRESHOLD_SECONDS", "2.0") or 2.0)
_INTERVAL = float(os.getenv("LOOP_STALL_INTERVAL", "0.5") or 0.5)
# Mínimo entre registos do MESMO tipo (kind+phase): um loop cronicamente
# lento (ex.: snapshot.build a 4-9s por tick) não pode encher a BD — mostra
# o padrão (1 a cada 30s com a duração do episódio) e a tabela fica bounded
# mesmo com um dia inteiro de stalls.
_MIN_GAP = float(os.getenv("LOOP_STALL_MIN_GAP_SECONDS", "30") or 30)


def stall_threshold_seconds() -> float:
    try:
        return float(os.getenv("LOOP_STALL_THRESHOLD_SECONDS", "2.0") or 2.0)
    except (TypeError, ValueError):
        return 2.0


class LoopStallRecorder:
    """Grava stalls do loop e fases lentas na `loop_stall_events`.

    Um registo só acontece quando `duration >= threshold` — o overhead por
    chamada é um `time.monotonic()` e a BD só é tocada nos episódios reais
    (raros). `log` é opcional (o servidor imprime no server.log).
    """

    def __init__(self, db, threshold: Optional[float] = None,
                 min_gap: Optional[float] = None,
                 log: Optional[Callable[[str], None]] = None):
        self._db = db
        self._threshold = threshold if threshold is not None \
            else stall_threshold_seconds()
        self._min_gap = min_gap if min_gap is not None else _MIN_GAP
        self._log = log or (lambda _m: None)
        # última vez que cada (kind, phase) foi gravado — rate-limit para a
        # BD não encher com um padrão crónico (1 por tipo a cada `min_gap`s)
        self._last_kind_ts: dict = {}

    @property
    def threshold(self) -> float:
        return self._threshold

    def _now_iso(self) -> str:
        from app.execution.engine import _now_iso
        return _now_iso()

    def _within_gap(self, key: str) -> bool:
        if self._min_gap <= 0:
            return False
        import time as _time
        now = _time.monotonic()
        if now - self._last_kind_ts.get(key, 0.0) < self._min_gap:
            return True
        self._last_kind_ts[key] = now
        return False

    def record_stall(self, lag: float) -> None:
        """Event loop congelado `lag` segundos (medido pelo atraso do wakeup)."""
        if lag < self._threshold or self._within_gap("stall:event_loop"):
            return
        try:
            self._db.record_loop_stall_event(
                self._now_iso(), "stall", "event_loop", lag, self._threshold)
        except Exception as exc:  # telemetria nunca pode quebrar o runtime
            self._log(f"[stallwatch] falha a gravar stall: {exc}")

    def record_phase(self, phase: str, seconds: float) -> None:
        """Fase on-loop instrumentada que demorou `seconds` — causa provável."""
        if seconds < self._threshold or self._within_gap(f"phase:{phase}"):
            return
        try:
            self._db.record_loop_stall_event(
                self._now_iso(), "phase", phase, seconds, self._threshold)
        except Exception as exc:
            self._log(f"[stallwatch] falha a gravar fase lenta ({phase}): {exc}")


async def run_loop_stall_watch(recorder: LoopStallRecorder,
                               interval: float = _INTERVAL) -> None:
    """Task do lifespan: deteta congelamentos do event loop.

    Dorme `interval` e mede o atraso real entre o wakeup agendado e o efetivo
    com `loop.time()` (relógio monotónico — imune a mudanças de relógio do
    sistema). Um loop bloqueado N segundos faz a task acordar N segundos
    tarde. Cancela-se no shutdown (task própria do lifespan).
    """
    loop = asyncio.get_running_loop()
    while True:
        scheduled = loop.time()
        await asyncio.sleep(max(0.01, interval))
        lag = max(0.0, loop.time() - scheduled - interval)
        recorder.record_stall(lag)
