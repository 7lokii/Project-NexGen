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

# WhatsApp bridge notifications (optional) — resultados de trades reais num
# grupo WhatsApp, via sidecar Node/Baileys em whatsapp/ (ver notify/whatsapp.py).
# Sem estes valores o bot funciona na mesma: só não há push para o WhatsApp.
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "")
WHATSAPP_BRIDGE_URL = os.getenv(
    "WHATSAPP_BRIDGE_URL", "http://127.0.0.1:8765"
)
WHATSAPP_GROUP_JID = os.getenv("WHATSAPP_GROUP_JID", "")  # ex. 1203630...@g.us
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL", "https://n3xg3n.duckdns.org"
)

# Default configuration. Persisted in the DB (config table) and edited from
# the dashboard. See docs/TECHNICAL.md for what each key means.
DEFAULT_CONFIG = {
    # Item 12: o modo "manual" foi removido — só existe o caminho auto:
    # sinais que passam nas verificações de risco executam sozinhos.
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
    # UTC instant at which an explicitly scheduled global pause may rearm.
    # Empty means no automatic rearm is scheduled.
    "killswitch_rearm_at": "",
    # Fixed operational observation window requested for the 28 August review.
    "promotion_window_start_at": "2026-08-20T00:00:00+00:00",
    "promotion_window_end_at": "2026-08-29T00:00:00+00:00",
    # Simulated market speed multiplier (0 pauses the market, 1 = real-time).
    "sim_speed": 1,
    # Edit window: after a Telegram message arrives, wait this many seconds
    # for a possible edit before continuing the pipeline. Edits reset it.
    "edit_window_seconds": 20,
    # Revive window (minutes): a REJECTED signal (entry guard, min-lot risk,
    # global risk) whose Telegram message the group re-edits with valid
    # content within this many minutes reopens the edit window instead of
    # staying silently rejected. 0 disables the revive. Env-tunable.
    "rejected_revive_minutes": float(os.getenv("SIGNALBOT_REJECTED_REVIVE_MINUTES", "10") or 10),
    # Time (HH:MM) at which the daily summary is sent.
    "summary_time": "23:59",
    # Automatic entry freshness guard. A price may be outside the declared
    # zone by at most N zone widths; beyond that the signal is blocked.
    "entry_guard_enabled": True,
    # Pequena margem automática fora da zona (em unidades de preço):
    # XAUUSD aceita até 0,50, mas desvios maiores continuam a pedir confirmação.
    "entry_guard_tolerance": 0.5,
    "entry_guard_max_zone_widths": 5.0,
    # Group pips are observational only; XAUUSD convention is 0.10 price
    # units per reported pip (never used to authorize an order).
    "group_gold_pip_size": 0.1,
    "group_feed_divergence_threshold": 20.0,
    # Alvo da janela da Fase 3 (N sinais resolvidos da auto_escada_3 na
    # corrida paper). Default 20 (SPEC v2); o feed pode baixá-lo para 15
    # quando o ETA estimar > 6 semanas (menos margem estatística).
    "fase3_target": 20,
    # Simulated account balance (paper trading).
    # Main lane fallback balance. In MT5 mode the broker account is the source of truth.
    "balance": 10000.0,
    # Independent fallback balance for lane 2; 0 inherits the main balance.
    "lane2_balance": 0.0,
    # Date on which the daily summary was last sent (YYYY-MM-DD).
    "summary_sent_date": "",
    # Estratégia ativa na conta principal (id do registo em app/strategies.py).
    "strategy": "nova_gestao",
    # Segunda lane de execução (estratégia principal paralela noutra conta
    # demo MT5). A credencial/conta vem do .env (MT5_2_*); aqui fica a
    # estratégia e o rótulo para a dashboard/WhatsApp. `lane2_enabled` é
    # derivado no arranque (main.py) da presença de MT5_2_LOGIN no .env.
    "lane2_strategy": "tp2_protect",
    "lane2_label": "Sair no TP2 + protect",
    # Corrida paper shadow: recebe sinais válidos mesmo quando a execução
    # principal está manual/bloqueada, mas nunca chama o motor MT5.
    "shadow_paper_enabled": True,
    # Evidência forward isolada das duas auto-escadas: 3 pernas (equilíbrio)
    # e 5 pernas (cauda/risco). Cada uma tem conta shadow própria.
    "shadow_paper_strategies": ["auto_escada_3", "auto_escada_5"],
    # Journal de quotes/gestão para replay determinístico. Desligado por
    # defeito para não aumentar a BD em produção sem autorização operacional.
    "execution_journal_enabled": False,
    # Início congelado da janela forward controlada (UTC). Vazio = o relatório
    # usa os últimos 30 dias; quando definido, a recolha não recua no tempo.
    "forward_evidence_started_at": "",
    # Seletor de dias das páginas de teste (7/30/90; 0 = todo o histórico).
    "teste_days": 30,
    # Horizonte da corrida paralela na principal (24h/7d/30d = 24/168/720h).
    "paper_race_hours": 24,
    # Modo autónomo: nenhum sinal fica pendente de confirmação humana.
    # Guardas de zona/lote mínimo ou executam sozinhas (com marca e
    # notificação) ou rejeitam com notificação — nunca esperam por um
    # operador. As proteções globais (kill switch, limite diário,
    # max_positions, llm_parsed) continuam a valer sempre.
    "autonomous_mode": True,
    # Cap autónomo da guarda de zona, em múltiplos da largura da zona
    # (0 = usa o limite máximo `entry_guard_max_zone_widths`). Desvios até
    # este cap executam a mercado com marca `zone_deviation` + notificação;
    # acima dele o sinal é rejeitado automaticamente.
    "entry_guard_auto_cap": 0.0,
    # Momento EXATO em que o cap autónomo mudou (ISO) — o study_cap_effect
    # usa-o como --since para partir os fills antes/depois do cap.
    "cap_applied_at": "",
    # Lembrete no feed quando o cap está à espera de dados reais pós-cap há
    # mais de N dias (o study_cap_effect decide com os fills reais).
    "cap_reminder_days": 7,
    "cap_reminder_lookback_days": 30,
    "cap_reminder_last_sent": "",
    # Idade máxima (minutos) de um sinal `pending` antes de ser rejeitado
    # automaticamente pelo tick (0 = sem expiração).
    "pending_expire_minutes": 30,
    # Estilo de entrada: market (preço atual) ou pending (ordem na zona).
    # Com pending, um preço fora da zona (dentro do cap) coloca ordens
    # pendentes no nível da entrada — o mercado volta ao nível em vez de o
    # bot perseguir o preço (Fase 1, validada pelo estudo de viabilidade).
    "entry_style": "pending",
    # TTL das ordens pendentes da zona (horas; 0 = sem expiração).
    "entry_order_ttl_hours": 4.0,
}

# Trading constants used by the risk engine / simulator.
PIP_SIZE = 0.0001          # pip size for the default (forex) instruments
CONTRACT_SIZE = 100_000    # units in a standard forex lot
MIN_LOTS = 0.01            # minimum tradable lot size
