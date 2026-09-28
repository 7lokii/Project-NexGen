"""Health check do servidor Project NexGen (porta 8000) + restart automático.

Corre na Tarefa Agendada "NexGen Health" (--once, a cada 2 min) ou em loop
(--watch). Verifica `GET /` (rápido: devolve a dashboard ou o login) com um
timeout generoso; se o servidor não responder:

  1. regista em `healthcheck.log` (rotativo, mesma política do backup.log),
  2. envia alerta para o Telegram (se TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
     estiverem no .env),
  3. reinicia o servidor com o comando documentado (`app.server_runner`,
     `MT5_DRY_RUN=0` sobrepõe o `.env`) e escreve o PID em `server.pid`.

**Conservador à prova de falsos positivos**: o `/api/status` é pesado
(pode demorar >5s com o MT5 a responder) — usar um timeout apertado aí
mataria um servidor saudável mas lento. O check usa `/`, timeout de 15s e
exige **2 falhas consecutivas** antes de qualquer ação (uma falha isolada
só é registada). Entre restarts há um cooldown de `--min-interval`
segundos (estado persistido em `healthcheck.state`).

Com `LANE2_WORKER_ENABLED=1` o mesmo script vigia também o **worker da
lane 2** (porta `LANE2_WORKER_PORT`, /ping): 2 falhas consecutivas ⇒
alerta + restart de `app.lane2_worker`, com cooldown próprio (`worker_*`)
no mesmo `healthcheck.state`.

Uso:
    .venv/Scripts/python healthcheck.py --once        # Tarefa Agendada
    .venv/Scripts/python healthcheck.py --watch       # loop contínuo
    .venv/Scripts/python healthcheck.py --no-restart  # só alerta (sem restart)
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # carrega o .env da cwd (o script é corrido na raiz do projeto)
except ImportError:
    pass

from app.maintenance import append_rotating_log, log_limits

BASE = Path(__file__).resolve().parent
LOG = BASE / "healthcheck.log"
STATE_FILE = BASE / "healthcheck.state"

# o .env usa PORT=0 como sinónimo de 8000 (mesma convenção do start.ps1)
_ENV_PORT = os.getenv("PORT", "8000") or "8000"
HOST = os.getenv("HEALTHCHECK_HOST", "127.0.0.1")
PORT = int(os.getenv("HEALTHCHECK_PORT", _ENV_PORT) or _ENV_PORT) or 8000

# console Windows pode ser cp1252 — nunca deixar um emoji do log rebentar o script
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# `/` é o alvo: serve sempre (dashboard ou login), rápido e sem auth
URL = f"http://{HOST}:{PORT}/"
TIMEOUT = float(os.getenv("HEALTHCHECK_TIMEOUT", "15") or 15)
REQUIRED_FAILURES = 2  # falhas consecutivas antes de alertar/restartar
DEFAULT_INTERVAL = 120  # segundos entre tentativas de restart
# Um processo que segura a porta mas tem menos que esta idade é tratado como
# "a arrancar" (lifespan: ligação MT5 + primeiras ticks) e NUNCA é morto —
# matá-lo a meio do boot faz o restart entrar em guerra com o próprio boot.
BOOT_GRACE_SECONDS = int(os.getenv("HEALTHCHECK_BOOT_GRACE", "300") or 300)

# ---- lane 2: worker MT5 isolado (app/lane2_worker.py, opcional) ---------
# Com LANE2_WORKER_ENABLED=1 a 2ª conta corre NESTE processo separado; o
# healthcheck também o vigia (mesma lógica de 2 falhas + cooldown) e
# reinicia-o quando cair. Sem o env, o bloco é inerte.
WORKER_ENABLED = os.getenv("LANE2_WORKER_ENABLED", "0") == "1"
WORKER_PORT = int(os.getenv("LANE2_WORKER_PORT", "8770") or 8770)
WORKER_URL = f"http://{HOST}:{WORKER_PORT}/ping"
WORKER_TIMEOUT = float(os.getenv("HEALTHCHECK_WORKER_TIMEOUT", "5") or 5)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{_now_iso()}] {msg}"
    print(line)
    max_bytes, backup_count = log_limits()
    append_rotating_log(LOG, line, max_bytes=max_bytes, backup_count=backup_count)


def _notify(text: str) -> None:
    """Alerta para o Telegram quando configurado; o log regista sempre."""
    log(text)
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not (tg_token and tg_chat):
        log("Telegram não configurado (TELEGRAM_BOT_TOKEN/CHAT_ID vazios no .env) "
            "— alerta só no log; preenche-os para receber no telemóvel")
        return
    try:
        import httpx
        r = httpx.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            data={"chat_id": tg_chat, "text": text}, timeout=30,
        )
        if r.status_code != 200:
            log(f"Telegram respondeu {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        log(f"ERRO ao enviar alerta para o Telegram: {exc}")


# ---------------------------------------------------------------- estado
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"ERRO ao gravar {STATE_FILE.name}: {exc}")


# ---------------------------------------------------------------- checks
def _server_ok() -> bool:
    """True se o servidor respondeu HTTP (qualquer status) dentro do timeout.

    Qualquer resposta HTTP (200/401/404) prova que o processo está vivo e a
    servir; só timeout/conexão recusada contam como offline.
    """
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as resp:
            resp.read(256)
            return True
    except urllib.error.HTTPError:
        return True  # 4xx/5xx = servidor a responder
    except (urllib.error.URLError, socket.timeout, OSError):
        return False


def _port_pid() -> int | None:
    """PID a escutar na porta (None = porta livre)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=15, check=False).stdout or ""
    except Exception:
        return None
    needle = f":{PORT}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and "LISTENING" in line and needle in line:
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def _process_age_seconds(pid: int) -> float | None:
    """Idade do processo em segundos (None = desconhecido).

    Via Get-CimInstance (PowerShell) porque o wmic foi removido em Windows
    recentes. Só corre no caminho de restart (raro), onde 1-3s de espera são
    aceitáveis. A idade é calculada no PowerShell (número puro, sem depender
    do formato de data da cultura do sistema).
    """
    if os.name != "nt":
        return None
    try:
        ps = (
            "$p = Get-CimInstance Win32_Process -Filter "
            f"'ProcessId={int(pid)}'; "
            "if ($p) { [int]((Get-Date) - $p.CreationDate).TotalSeconds } "
            "else { -1 }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        age = int(out.strip().splitlines()[-1]) if out.strip() else -1
        return age if age >= 0 else None
    except Exception:
        return None


def _port_pid_for(port: int) -> int | None:
    """PID a escutar numa porta arbitrária (worker da lane 2)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=15, check=False).stdout or ""
    except Exception:
        return None
    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and "LISTENING" in line and needle in line:
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------- restart
def _restart_server() -> bool:
    """Reinicia o servidor com o comando documentado (server_runner).

    Usa a mesma receita do start.ps1: python -u -m app.server_runner com o
    MT5_DRY_RUN=0 a sobrepor o .env (execução real na demo). O processo é
    desanexado; o server_runner abre os próprios logs rotativos.
    """
    py = BASE / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MT5_DRY_RUN"] = "0"
    try:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [str(py), "-u", "-m", "app.server_runner",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(BASE), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs,
        )
        try:
            (BASE / "server.pid").write_text(str(proc.pid), encoding="utf-8")
        except Exception:
            pass
        return True
    except Exception as exc:
        log(f"ERRO ao reiniciar o servidor: {exc}")
        return False


# ---------------------------------------------------------------- lane 2

def _worker_ok() -> bool:
    """True se o worker da lane 2 respondeu /ping (qualquer HTTP = vivo)."""
    try:
        with urllib.request.urlopen(WORKER_URL, timeout=WORKER_TIMEOUT) as resp:
            resp.read(128)
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, socket.timeout, OSError):
        return False


def _restart_worker() -> bool:
    """Reinicia o worker da lane 2 (app.lane2_worker, porta própria).

    O worker recusa arrancar sem MT5_2_TERMINAL_PATH (fail-closed: sem
    terminal portable trocaria a conta do terminal da principal). Com o path
    em falta, não vale a pena reiniciar em loop — loga e fica à espera do
    operador corrigir o .env.
    """
    if not os.getenv("MT5_2_TERMINAL_PATH", "").strip():
        log("[lane2-worker] MT5_2_TERMINAL_PATH vazio no .env — o worker "
            "recusa arrancar (fail-closed). Configura o terminal portable da "
            "2ª conta antes de ativar a lane 2 real.")
        return False
    py = BASE / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [str(py), "-u", "-m", "app.lane2_worker"],
            cwd=str(BASE), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs,
        )
        try:
            (BASE / "lane2_worker.pid").write_text(str(proc.pid), encoding="utf-8")
        except Exception:
            pass
        return True
    except Exception as exc:
        log(f"[lane2-worker] ERRO ao reiniciar: {exc}")
        return False


def _check_worker(restart: bool, min_interval: int) -> int:
    """Uma passagem completa do worker da lane 2 (alvo paralelo ao servidor).

    Mesmas regras do servidor: 2 falhas consecutivas antes de agir, cooldown
    de restart partilhado, estado em `healthcheck.state` com chaves `worker_*`.
    """
    state = _load_state()
    now = time.time()
    failures = int(state.get("worker_consecutive_failures", 0))

    if _worker_ok():
        if state.get("worker_down"):
            _notify(f"✅ Lane 2 (worker MT5) de volta (porta {WORKER_PORT})")
        _save_state({**state, "worker_down": False,
                     "worker_consecutive_failures": 0})
        return 0

    failures += 1
    state["worker_consecutive_failures"] = failures
    state["worker_last_failure_at"] = now
    if failures < REQUIRED_FAILURES:
        log(f"[lane2-worker] sem resposta em {WORKER_URL} "
            f"(falha {failures}/{REQUIRED_FAILURES} consecutiva) — a confirmar")
        _save_state(state)
        return 1

    was_down = state.get("worker_down", False)
    state["worker_down"] = True
    last_try = state.get("worker_last_restart_at", 0.0)
    if not was_down:
        _notify(f"🚨 Lane 2 (worker MT5) OFFLINE (porta {WORKER_PORT}) — restart automático")
    else:
        _notify(f"🔁 Lane 2 (worker MT5) continua offline — nova tentativa de restart")
    _save_state(state)

    if not restart:
        return 1
    if now - last_try < min_interval:
        log("[lane2-worker] cooldown de restart ativo — a tentar na próxima passagem")
        return 1

    listener = _port_pid_for(WORKER_PORT)
    if listener is not None:
        age = _process_age_seconds(listener)
        if age is not None and age < BOOT_GRACE_SECONDS:
            log(f"[lane2-worker] porta {WORKER_PORT} ocupada pelo PID "
                f"{listener} mas processo tem só {age:.0f}s (a arrancar?) — "
                "sem kill nesta passagem; volta a confirmar na próxima")
            state["worker_consecutive_failures"] = 1
            _save_state(state)
            return 1
        log(f"[lane2-worker] porta {WORKER_PORT} ocupada pelo PID {listener} sem "
            f"resposta HTTP — processo encravado; a terminar antes de reiniciar")
        try:
            subprocess.run(["taskkill", "/PID", str(listener), "/F"],
                           capture_output=True, timeout=15, check=False)
            time.sleep(2)
        except Exception as exc:
            log(f"[lane2-worker] ERRO ao terminar o PID {listener}: {exc}")

    ok = _restart_worker()
    state["worker_last_restart_at"] = now
    if ok:
        state["worker_restarted_at"] = now
    _save_state(state)
    return 1


# ---------------------------------------------------------------- action
def _check(restart: bool, min_interval: int) -> int:
    """Uma passagem completa: verifica, alerta e (se aplicável) reinicia."""
    state = _load_state()
    now = time.time()
    failures = int(state.get("consecutive_failures", 0))

    if _server_ok():
        if state.get("down"):
            _notify(f"✅ Project NexGen: servidor de volta "
                    f"(http://{HOST}:{PORT})")
        _save_state({"down": False, "consecutive_failures": 0})
        return 0

    # falha — conta as consecutivas antes de agir (à prova de falso positivo)
    failures += 1
    state["consecutive_failures"] = failures
    state["last_failure_at"] = now
    if failures < REQUIRED_FAILURES:
        log(f"sem resposta HTTP em {URL} (falha {failures}/{REQUIRED_FAILURES} "
            f"consecutiva) — a confirmar antes de agir")
        _save_state(state)
        return 1

    # confirmado offline: alerta (primeira vez) e restart com cooldown
    was_down = state.get("down", False)
    state["down"] = True
    last_try = state.get("last_restart_at", 0.0)
    if not was_down:
        _notify(f"🚨 Project NexGen: servidor OFFLINE "
                f"(http://{HOST}:{PORT})")
    else:
        _notify(f"🔁 Project NexGen: servidor continua offline — nova "
                f"tentativa de restart")
    _save_state(state)

    if not restart:
        return 1
    if now - last_try < min_interval:
        log("cooldown de restart ativo — a tentar na próxima passagem")
        return 1

    # se a porta ficou presa num processo encravado, termina-o antes de subir
    listener = _port_pid()
    if listener is not None:
        age = _process_age_seconds(listener)
        if age is not None and age < BOOT_GRACE_SECONDS:
            # boot ainda (lifespan liga MT5; a porta já escuta mas o HTTP só
            # responde depois) — matar aqui reiniciaria em loop/guerra
            log(f"porta {PORT} ocupada pelo PID {listener} mas processo tem "
                f"só {age:.0f}s (a arrancar?) — sem kill nesta passagem; "
                "volta a confirmar na próxima")
            state["consecutive_failures"] = 1
            _save_state(state)
            return 1
        log(f"porta {PORT} ocupada pelo PID {listener} sem resposta HTTP — "
            f"processo encravado; a terminar antes de reiniciar")
        try:
            subprocess.run(["taskkill", "/PID", str(listener), "/F"],
                           capture_output=True, timeout=15, check=False)
            time.sleep(2)
        except Exception as exc:
            log(f"ERRO ao terminar o PID {listener}: {exc}")

    ok = _restart_server()
    state["last_restart_at"] = now
    if ok:
        state["restarted_at"] = now
    _save_state(state)
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    watch = "--watch" in argv
    restart = "--no-restart" not in argv
    min_interval = DEFAULT_INTERVAL
    for i, a in enumerate(argv):
        if a == "--min-interval" and i + 1 < len(argv):
            try:
                min_interval = max(30, int(argv[i + 1]))
            except ValueError:
                pass

    if watch:
        while True:
            rc = _check(restart, min_interval)
            if WORKER_ENABLED:
                rc = max(rc, _check_worker(restart, min_interval))
            time.sleep(max(30, min_interval))
    rc = _check(restart, min_interval)
    if WORKER_ENABLED:
        rc = max(rc, _check_worker(restart, min_interval))
    return rc


if __name__ == "__main__":
    sys.exit(main())
