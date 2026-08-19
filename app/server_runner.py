"""Production Uvicorn entry point with bounded stdout/stderr logs.

Using this module instead of shell redirection lets the rotating stream close
its active file before Windows renames it during rotation.
"""

from __future__ import annotations

import argparse
import os

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from app.maintenance import configure_process_stdio


# Install before importing Uvicorn: its logging handlers then see the bounded
# streams rather than a shell-owned, never-rotated redirection.
configure_process_stdio("server")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Project NexGen API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
