"""Notificações WhatsApp (extraído de app/state.py).

Publica o resultado de trades reais no grupo WhatsApp via bridge Baileys
(`app/notify/whatsapp.py`).

Desde 16/09 a mensagem mostra APENAS a lane 2 (tp2) e é enviada UMA vez por
sinal, no fecho da trade da tp2: fechos da lane main são ignorados (antes,
cada lane fechava em ticks diferentes e o grupo recebia DUAS mensagens do
mesmo sinal). O tick chama `_notify_whatsapp_trade_close` UMA vez por sinal
por tick (dedup no caller) e o hook filtra por lane — dois fechos no mesmo
tick ou em ticks diferentes nunca duplicam.
"""

from __future__ import annotations


class WhatsAppMixin:
    """_notify_whatsapp_trade_close + rótulo de lane para a mensagem."""

    async def _notify_whatsapp_trade_close(self, ev: dict, lane_id: str = "main") -> None:
        """Publica o resultado da trade real no grupo WhatsApp (se configurado).

        SOMENTE a lane 2 (tp2) publica: uma mensagem por sinal, no fecho da
        trade da tp2 (a main não envia — era a causa das mensagens duplicadas
        quando as lanes fechavam em ticks diferentes). Mostra só a trade da
        tp2 com o seu P&L e razão.

        Só trades REAIS (MT5 demo): em modo sim não há conta MT5 e a corrida
        paper dispara os seus próprios eventos (loop separado) — nada a
        enviar. Uma falha do WhatsApp nunca interrompe o tick: registada na
        BD e seguida.
        """
        if self.whatsapp is None or not self.whatsapp.configured:
            return
        if lane_id != "tp2":
            # A lane main não publica no WhatsApp: a mensagem é única por
            # sinal e sai com o fecho da tp2.
            return
        acct = self._mt5_account(lane_id)
        if acct is None:
            acct = self._lane2_synthetic_account()
        if acct is None:
            return
        try:
            from app.notify.whatsapp import format_trade_close
            closed = self.db.get_trade(ev.get("trade_id") or "")
            if closed is None:
                return
            positions = self.db.positions_for_trade(closed.id)
            label = self._lane_whatsapp_label("tp2", closed)
            text = format_trade_close(
                closed, positions, self.whatsapp.dashboard_url,
                lane_label=label,
            )
            await self.whatsapp.send(text)
        except Exception as exc:
            try:
                self.db.record_exception(
                    exc, context={"component": "whatsapp", "phase": "trade_close"}
                )
            except Exception:
                pass
            print(f"[whatsapp] falha ao notificar trade fechada: {exc}")

    def _lane_whatsapp_label(self, lane_id: str, trade) -> str:
        """Rótulo de conta/estratégia para a mensagem WhatsApp por lane."""
        if lane_id == "main":
            return "Conta principal"
        acct = self._mt5_account(lane_id) or {}
        login = acct.get("login")
        if not login:
            from app.config import DEFAULT_CONFIG
            # lane sem terminal ligado (dry-run): usa o login do .env
            import os as _os
            login = _os.getenv("MT5_2_LOGIN") or "tp2"
        strat_id = getattr(trade, "strategy_id", None) if trade is not None \
            else None
        if not strat_id:
            strat_id = self._lane_strategy_id(lane_id)
        return f"{strat_id} · conta {login}"
