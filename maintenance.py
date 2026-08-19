"""Read-only operational checks for the Windows Task Scheduler.

Examples:
  .venv/Scripts/python maintenance.py --disk-check
  .venv/Scripts/python maintenance.py --db-audit

The backup restore check lives beside the backup creator as
``backup_db.py --restore-check`` so the monthly task uses the same backup
configuration.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from app.maintenance import audit_database, disk_status


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Project NexGen maintenance checks")
    parser.add_argument("--disk-check", action="store_true")
    parser.add_argument("--db-audit", action="store_true")
    parser.add_argument("--db", default=os.getenv("SIGNALBOT_DB", str(ROOT / "signalbot.db")))
    args = parser.parse_args()
    if args.disk_check:
        result = disk_status()
    elif args.db_audit:
        result = audit_database(args.db)
    else:
        parser.error("escolhe --disk-check ou --db-audit")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.get("below_threshold") else 0


if __name__ == "__main__":
    raise SystemExit(main())
