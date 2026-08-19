"""Central configuration for Project NexGen.

Everything tunable lives here so the rest of the code reads from a single
source of truth. Environment variables are optional overrides (mainly for
the Telegram notifier and the SQLite path).
"""

import os

APP_NAME = "Project NexGen"
VERSION = "0.1.0"

# SQLite database file. Override with SIGNALBOT_DB to move it elsewhere.
DB_PATH = os.getenv(
    "SIGNALBOT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "signalbot.db"),
)

# Telegram notification credentials (optional).
# Without them the bot still works: notifications are stored in the DB and
# shown on the dashboard, they just are not pushed to Telegram.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Default configuration. Persisted in the DB (config table) and edited from
# the dashboard. See docs/TECHNICAL.md for what each key means.
DEFAULT_CONFIG = {
    # "manual" -> signals wait for a human click on the dashboard.
    # "auto"   -> signals that pass the risk checks are executed alone.
    "mode": "manual",
    # Risk per trade, % of account equity (the dashboard slider). Base do
    # risco dinâmico: escala para baixo após drawdown (ver state._effective_risk_pct).
    "risk_pct": 2.5,
    # Pico de equity (high-watermark) para o risco dinâmico — atualizado no
    # tick quando a equity sobe; 0 = ainda não inicializado.
    "peak_balance": 0.0,
    # Pause trading automatically if today's realized loss exceeds this %.
    "daily_loss_limit_pct": 3.0,
    # Maximum number of open trades at the same time (a estratégia de 5
    # pernas precisa de abrir até 5 entradas em simultâneo).
    "max_positions": 5,
    # Global pause: no new entries are opened (open trades are still managed).
    "kill_switch": False,
    # Set to True automatically when the daily loss limit is hit.
    "auto_kill": False,
    # Simulated market speed multiplier (0 pauses the market, 1 = real-time).
    "sim_speed": 1,
    # Edit window: after a Telegram message arrives, wait this many seconds
    # for a possible edit before continuing the pipeline. Edits reset it.
    "edit_window_seconds": 8,
    # Time (HH:MM) at which the daily summary is sent.
    "summary_time": "23:59",
    # Alvo da janela da Fase 3 (N sinais resolvidos da auto_escada_3 na
    # corrida paper). Default 20 (SPEC v2); o feed pode baixá-lo para 15
    # quando o ETA estimar > 6 semanas (menos margem estatística).
    "fase3_target": 20,
    # Simulated account balance (paper trading).
    "balance": 10000.0,
    # Date on which the daily summary was last sent (YYYY-MM-DD).
    "summary_sent_date": "",
    # Estratégia ativa na conta principal (id do registo em app/strategies.py).
    "strategy": "nova_gestao",
    # Seletor de dias das páginas de teste (7/30/90; 0 = todo o histórico).
    "teste_days": 30,
    # Horizonte da corrida paralela na principal (24h/7d/30d = 24/168/720h).
    "paper_race_hours": 24,
}

# Trading constants used by the risk engine / simulator.
PIP_SIZE = 0.0001          # pip size for the default (forex) instruments
CONTRACT_SIZE = 100_000    # units in a standard forex lot
MIN_LOTS = 0.01            # minimum tradable lot size
