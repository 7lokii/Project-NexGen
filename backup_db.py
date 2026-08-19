"""Backup diário da base de dados para o Telegram (Tarefa Agendada).

Copia `signalbot.db` de forma consistente (API backup do sqlite3, segura
mesmo com o servidor a escrever) para `backups/signalbot-<timestamp>.db` e
envia-a para o Telegram via `sendDocument` (se TELEGRAM_BOT_TOKEN e
TELEGRAM_CHAT_ID estiverem no .env) — assim o historico/ground truth sai da
VPS todos os dias. Mantem as ultimas 10 copias locais (rotacao).

Uso: `.venv/Scripts/python backup_db.py`
"""

import datetime
import glob
import os
import sqlite3
import sys
from pathlib import Path

from app.maintenance import (append_rotating_log, latest_backup, log_limits,
                             restore_check)

try:
    from dotenv import load_dotenv
    load_dotenv()  # carrega o .env da cwd (o script é corrido na raiz do projeto)
except ImportError:
    pass

BASE = Path(__file__).resolve().parent
DB = os.getenv("SIGNALBOT_DB", str(BASE / "signalbot.db"))
BACKUP_DIR = BASE / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
LOG = BASE / "backup.log"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
KEEP = 10


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    max_bytes, backup_count = log_limits()
    append_rotating_log(LOG, line, max_bytes=max_bytes,
                        backup_count=backup_count)


def _notify_restore(result: dict) -> None:
    """Send the monthly restore result to Telegram when configured."""
    if not (TOKEN and CHAT_ID):
        return
    try:
        import httpx
        if result.get("ok"):
            text = ("✅ Project NexGen: teste mensal de restauração OK — "
                    f"{Path(result['backup']).name}; contagens "
                    f"{result.get('counts', {})}")
        else:
            text = ("🚨 Project NexGen: FALHA no teste mensal de restauração — "
                    f"{result.get('error', 'erro desconhecido')} · "
                    f"backup {result.get('backup', '—')}")
        response = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}, timeout=30,
        )
        if response.status_code != 200:
            log(f"Telegram respondeu {response.status_code}: {response.text[:200]}")
    except Exception as exc:
        log(f"ERRO ao enviar resultado da restauração para o Telegram: {exc}")


def _restore_check() -> int:
    backup = latest_backup(BACKUP_DIR)
    if backup is None:
        result = {"ok": False, "error": "não existe backup local para testar"}
    else:
        result = restore_check(backup)
    if result.get("ok"):
        log(f"restore-check OK: {Path(result['backup']).name} "
            f"integrity={result.get('integrity')} counts={result.get('counts')}")
    else:
        log(f"restore-check FALHOU: {result.get('error', 'erro desconhecido')}")
    _notify_restore(result)
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    if argv is not None and "--restore-check" in argv:
        return _restore_check()
    if "--restore-check" in sys.argv[1:]:
        return _restore_check()
    if not os.path.exists(DB):
        log(f"ERRO: BD nao encontrada em {DB}")
        return 1

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"signalbot-{ts}.db"

    # copia consistente (online backup API — nao corre o risco de apanhar
    # a BD a meio de uma escrita, como aconteceria com shutil.copy)
    try:
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as exc:
        log(f"ERRO ao copiar a BD: {exc}")
        return 1

    size_kb = dest.stat().st_size / 1024
    log(f"backup local: {dest.name} ({size_kb:.1f} KB)")

    # envio para o Telegram (fora da VPS)
    if TOKEN and CHAT_ID:
        try:
            import httpx
            with open(dest, "rb") as fh:
                r = httpx.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                    data={"chat_id": CHAT_ID,
                          "caption": f"Project NexGen backup {ts}"},
                    files={"document": (dest.name, fh, "application/octet-stream")},
                    timeout=120,
                )
            if r.status_code == 200:
                log("backup enviado para o Telegram (OK)")
            else:
                log(f"Telegram respondeu {r.status_code}: {r.text[:200]}")
        except Exception as exc:
            log(f"ERRO ao enviar para o Telegram: {exc}")
    else:
        log("Telegram nao configurado (TELEGRAM_BOT_TOKEN/CHAT_ID vazios no .env) "
            "— backup so local; preenche-os para a copia sair da VPS")

    # rotacao: mantem as ultimas KEEP copias locais
    try:
        files = sorted(glob.glob(str(BACKUP_DIR / "signalbot-*.db")))
        for old in files[:-KEEP]:
            os.remove(old)
            log(f"rotacao: removido {Path(old).name}")
    except Exception as exc:
        log(f"ERRO na rotacao: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
