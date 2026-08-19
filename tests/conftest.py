"""Isolamento da suite: força um DB temporário e o broker de papel ANTES de
qualquer teste importar `app.*` (que lê as env vars no import).

Sem isto, correr um ficheiro de testes que importe `app.backtest` antes de
`test_api.py` fazia `app.config` apontar para a BD de produção e os testes
escreviam nela (trades/positions/sinais de teste na BD real). Este conftest
garante que a ordem de execução nunca afeta o alvo.
"""

import os
import tempfile

os.environ.setdefault("SIGNALBOT_DB", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("MARKET_PROVIDER", "none")
os.environ.setdefault("EXECUTION_MODE", "sim")
os.environ.setdefault("MT5_DRY_RUN", "1")
os.environ.setdefault("SIGNALBOT_DASHBOARD_PASSWORD", "")
os.environ.setdefault("SIGNALBOT_API_TOKEN", "")
os.environ.setdefault("SIGNALBOT_LOG_ROTATE", "0")
