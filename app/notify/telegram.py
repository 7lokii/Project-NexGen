"""Notifier.

Every event (trade opened/closed, risk blocks, daily summary) is:
  1. stored in the DB (always, so the dashboard shows a feed),
  2. broadcast over the WebSocket (always),
  3. pushed to a Telegram chat (only if TELEGRAM_BOT_TOKEN and
     TELEGRAM_CHAT_ID are configured).

Because of (3) being optional, the whole system works out of the box with
zero credentials and upgrades to real Telegram notifications when the env
variables are set.
"""

import httpx

from app.db import DB


class Notifier:
    def __init__(self, db: DB, ws, token: str = "", chat_id: str = ""):
        self.db = db
        self.ws = ws
        self.token = token
        self.chat_id = chat_id

    async def send(self, kind: str, text: str, created_at: str = "",
                   tg_text: str = "", parse_mode: str = "Markdown",
                   action: str = "", lane: str = "") -> None:
        """Regista/broadcast `text` (limpo, para o feed da dashboard) e, se
        `tg_text` for dado, envia ESSE texto formatado (Markdown) para o
        Telegram — o feed nunca mostra asteriscos/markdown.

        `action` (opcional) associa uma ação ao item do feed — ex.:
        "promote:<strategy_id>" mostra um botão "⬆️ Promover" na dashboard.
        `lane` (opcional) marca a origem do evento ("main"/"tp2") para o
        feed distinguir os eventos das duas contas."""
        from app.execution.engine import _now_iso
        created_at = created_at or _now_iso()
        self.db.insert_notification(kind, text, created_at, action or None,
                                    lane=lane or None)
        await self.ws.broadcast({
            "type": "notification",
            "kind": kind,
            "text": text,
            "created_at": created_at,
            "action": action,
            "lane": lane or "",
        })
        if self.token and self.chat_id:
            try:
                payload: dict = {"chat_id": self.chat_id, "text": tg_text or text}
                if tg_text:
                    payload["parse_mode"] = parse_mode
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json=payload,
                    )
            except Exception as exc:  # never let a notification break the bot
                try:
                    self.db.record_exception(
                        exc, context={"component": "notifier", "phase": "telegram_push"}
                    )
                except Exception:
                    pass
                print(f"[notify] telegram push failed: {exc}")
