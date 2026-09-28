"""WhatsAppNotifier — publica resultados de trades reais num grupo WhatsApp.

Transporte: sidecar Node/Baileys em ``whatsapp/bridge.js`` (sessão WhatsApp
Web pareada 1x por QR code) que aceita ``POST /send {jid, text}`` em
``127.0.0.1:8765``. Este módulo só formata a mensagem e faz o POST — uma
falha do bridge NUNCA quebra o bot (mesma filosofia do notifier Telegram:
regista a exceção na BD e segue).

O bridge é um sidecar com religação própria: quando o telemóvel/sessão
está instável há janelas de conectividade de ~1-3s (500/503 intermitentes).
``send()`` faz retry curto (3 tentativas, backoff 2s/4s) para uma falha
transitória não perder a mensagem; só desiste e regista na BD no fim.

Configuração (.env):
    WHATSAPP_ENABLED    - "1" liga o push para o WhatsApp
    WHATSAPP_BRIDGE_URL - default http://127.0.0.1:8765
    WHATSAPP_GROUP_JID  - JID do grupo (ex. 120363012345678901@g.us)
    DASHBOARD_URL       - link público da dashboard (default n3xg3n.duckdns.org)
"""

import asyncio
import httpx

from app.config import (DASHBOARD_URL, WHATSAPP_BRIDGE_URL,
                        WHATSAPP_ENABLED, WHATSAPP_GROUP_JID)

# Rótulos PT para a razão de saída (trades/positions). Fallback = razão crua.
REASON_LABELS = {
    "tp1": "TP1", "tp2": "TP2", "tp3": "TP3", "tp4": "TP4", "tp5": "TP5",
    "tp": "TP", "sl": "SL", "be": "Breakeven", "protect": "Protect do grupo",
    "close_all": "Close all", "close_tp1": "Close TP1",
    "close_tp": "Close TP", "move_sl": "Move SL",
    "killswitch": "Kill switch", "manual": "Manual",
    "close_leg": "Fecho ao mercado", "auto_protect": "Proteção automática",
    "time_stop": "Time stop", "confirm": "Confirmação", "mixed": "Misto",
}

DIRECTION_LABELS = {"buy": "Compra", "sell": "Venda"}


def reason_label(reason) -> str:
    if not reason:
        return "—"
    return REASON_LABELS.get(reason, str(reason))


def _fmt_pnl(pnl: float) -> str:
    return f"{pnl:+.2f} €"


def _close_time_utc(closed_at) -> str:
    """HH:MM UTC a partir do ISO com fuso; vazio quando não dá para parsear."""
    if not closed_at:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(closed_at))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def format_trade_close(trade, positions, dashboard_url: str = "",
                       broker_account: dict = None, simulation: dict = None,
                       lane_label: str = "") -> str:
    """Mensagem final para o grupo quando uma trade REAL fecha.

    `trade` — row da tabela `trades` (get_trade); `positions` — lista de
    `Position` da trade (positions_for_trade), para o P&L por perna.
    `lane_label` — rótulo de conta/estratégia da segunda lane (vazio na
    principal). Devolve o texto com o resumo completo + link da dashboard.
    """
    url = dashboard_url or DASHBOARD_URL
    if lane_label:
        lines_head = [f"🏷️ {lane_label}"]
    else:
        lines_head = []
    symbol = getattr(trade, "symbol", "?")
    pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
    direction = DIRECTION_LABELS.get(
        getattr(trade, "direction", ""), getattr(trade, "direction", "")
    )
    entry = getattr(trade, "entry", None)

    total_lots = 0.0
    legs = sorted(positions, key=lambda p: getattr(p, "leg", 0) or 0) \
        if positions else []
    # Razão da trade: a do trade; quando vazia, deriva da última perna
    # fechada (o motor faz o mesmo em `_maybe_close_trade`).
    exit_reason_raw = getattr(trade, "exit_reason", None) or ""
    if not exit_reason_raw and legs:
        last = max(
            legs,
            key=lambda p: (getattr(p, "closed_at", "") or "",
                           getattr(p, "exit_reason", "") or ""),
        )
        exit_reason_raw = getattr(last, "exit_reason", None) or ""
    exit_reason = reason_label(exit_reason_raw)

    lines = []
    if getattr(trade, "execution_mode", "") == "mt5-dry":
        sim = simulation or {}
        broker = broker_account or {}
        currency = sim.get("currency") or broker.get("currency") or ""
        lines.append(
            f"🧪 MT5 DRY-RUN — resultado simulado · saldo broker "
            f"{float(broker.get('balance', 0.0) or 0.0):,.2f} {currency}".strip()
        )
    lines.append(f"📦 {symbol} fechada {'✅' if pnl >= 0 else '❌'} {_fmt_pnl(pnl)}")
    if direction:
        lines.append(f"➡️ {direction}" + (f" · entrada {entry:g}" if entry else ""))

    if legs:
        lines.append("💸 Pernas:")
        for p in legs:
            lots = float(getattr(p, "lots", 0.0) or 0.0)
            total_lots += lots
            price = getattr(p, "close_price", None)
            at = f" @ {price:g}" if price is not None else ""
            lines.append(
                f"  L{getattr(p, 'leg', '?')} · {lots:g} l · "
                f"{reason_label(getattr(p, 'exit_reason', None))}{at} · "
                f"{_fmt_pnl(float(getattr(p, 'pnl', 0.0) or 0.0))}"
            )
    else:
        total_lots = sum(
            float(getattr(trade, f"lots_{i}", 0.0) or 0.0) for i in range(1, 6)
        )

    time_str = _close_time_utc(getattr(trade, "closed_at", None))
    lines.append(
        f"🎯 Razão da trade: {exit_reason}"
        + (f" · {time_str} UTC" if time_str else "")
    )
    lines.append(f"💰 P&L total {_fmt_pnl(pnl)} · {total_lots:g} lotes")
    lines.append(f"🔗 Detalhes: {url}")
    return "\n".join(lines_head + lines)


def _lane_block(lines: list, label: str, trade, positions) -> None:
    """Bloco de UMA lane para a mensagem dual: título, status e pernas."""
    if label:
        lines.append(f"\n🏷️ {label}")
    status = getattr(trade, "status", "")
    pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
    lines.append(
        f"📦 {getattr(trade, 'symbol', '?')} "
        f"{'ABERTA' if status != 'closed' else 'fechada ✅' if pnl >= 0 else 'fechada ❌'} "
        f"· {_fmt_pnl(pnl)}"
    )
    legs = sorted(positions, key=lambda p: getattr(p, "leg", 0) or 0) \
        if positions else []
    if legs:
        for p in legs:
            lots = float(getattr(p, "lots", 0.0) or 0.0)
            price = getattr(p, "close_price", None)
            at = f" @ {price:g}" if price is not None else ""
            state = ("aberta" if getattr(p, "status", "") != "closed"
                     else reason_label(getattr(p, "exit_reason", None)))
            lines.append(
                f"  L{getattr(p, 'leg', '?')} · {lots:g} l · {state}{at} · "
                f"{_fmt_pnl(float(getattr(p, 'pnl', 0.0) or 0.0))}"
            )
    else:
        lines.append(
            f"  sem pernas registadas · "
            f"{'ainda aberta' if status != 'closed' else 'sem detalhe de pernas'}"
        )


def format_dual_lane_close(closed_trade, lanes_data, dashboard_url: str = "",
                           total_pnl: float = 0.0) -> str:
    """Mensagem única para o grupo quando uma trade REAL fecha: mostra as
    DUAS lanes (a que fechou e a outra do mesmo sinal, aberta ou fechada).

    `closed_trade` — a trade que disparou a notificação; `lanes_data` —
    lista de dicts por lane: `{"lane_id", "label", "trade", "positions"}`
    (a lane sem trade leva `trade=None`); `total_pnl` — soma do P&L de
    todas as lanes apresentadas. O link da dashboard aparece uma vez no fim.
    """
    url = dashboard_url or DASHBOARD_URL
    symbol = getattr(closed_trade, "symbol", "?")
    pnl = float(getattr(closed_trade, "pnl", 0.0) or 0.0)
    direction = DIRECTION_LABELS.get(
        getattr(closed_trade, "direction", ""), getattr(closed_trade, "direction", "")
    )
    entry = getattr(closed_trade, "entry", None)
    exit_reason = reason_label(
        getattr(closed_trade, "exit_reason", None) or ""
    ) or "—"
    time_str = _close_time_utc(getattr(closed_trade, "closed_at", None))

    lines = [
        f"🤝 {symbol} fechada — resumo das DUAS contas",
        f"➡️ {direction}" + (f" · entrada {entry:g}" if entry else ""),
    ]
    closing_lane = getattr(closed_trade, "lane_id", None) or "main"
    presented = 0
    for ld in lanes_data:
        t = ld.get("trade")
        if t is None:
            lines.append(
                f"\n🏷️ {ld.get('label') or (ld.get('lane_id') or '?')}: sem trade "
                "para este sinal"
            )
            continue
        label = ld.get("label") or (ld.get("lane_id") or "main")
        if ld.get("lane_id") == closing_lane:
            label = f"{label} · fechou agora"
        _lane_block(lines, label, t, ld.get("positions") or [])
        presented += 1
    lines.append(
        f"\n💰 P&L total das duas contas {_fmt_pnl(total_pnl)}"
        + (f" · {time_str} UTC" if time_str else "")
    )
    lines.append(f"🎯 Razão do fecho: {exit_reason}")
    lines.append(f"🔗 Detalhes: {url}")
    return "\n".join(lines)


def format_test_message(dashboard_url: str = "") -> str:
    """Mensagem de teste do formato dual (2 contas) — enviada pelo botão de
    teste do Diagnóstico/API para validar o bridge sem esperar um fecho."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    return (
        "🧪 Teste de notificação dual-lane\n"
        f"Enviada a {now}. Quando uma trade fechar, a mensagem mostra as "
        "duas contas (principal + 2ª) do mesmo sinal, lado a lado.\n"
        f"🔗 Dashboard: {dashboard_url or DASHBOARD_URL}"
    )


class WhatsAppNotifier:
    """Envia o texto para o grupo via bridge Baileys (127.0.0.1:8765).

    `send()` nunca levanta: falha de rede/bridge é registada na BD
    (`record_exception`) e devolve False — o tick de execução continua.
    """

    def __init__(self, db, enabled: str = WHATSAPP_ENABLED,
                 bridge_url: str = WHATSAPP_BRIDGE_URL,
                 group_jid: str = WHATSAPP_GROUP_JID,
                 dashboard_url: str = DASHBOARD_URL,
                 max_attempts: int = 3, retry_delay: float = 2.0):
        self.db = db
        self.enabled = bool(enabled)
        self.bridge_url = (bridge_url or WHATSAPP_BRIDGE_URL).rstrip("/")
        self.group_jid = group_jid
        self.dashboard_url = dashboard_url or DASHBOARD_URL
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = max(0.0, float(retry_delay))

    @property
    def configured(self) -> bool:
        """Push ativo = ligado no .env E com bridge e grupo preenchidos."""
        return self.enabled and bool(self.group_jid)

    async def send(self, text: str) -> bool:
        if not self.configured:
            return False
        # O bridge religa sozinho em segundos (janelas de conectividade do
        # WhatsApp); um 5xx/erro de rede transitório não é ordem para
        # desistir — repete com backoff curto. Só registra a exceção no fim.
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post(
                        f"{self.bridge_url}/send",
                        json={"jid": self.group_jid, "text": text},
                    )
                    r.raise_for_status()
                    return True
            except Exception as exc:  # never let a notification break the bot
                last_exc = exc
                if attempt < self.max_attempts - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        try:
            self.db.record_exception(
                last_exc, context={"component": "whatsapp", "phase": "bridge_push"}
            )
        except Exception:
            pass
        print(f"[whatsapp] bridge push failed: {last_exc}")
        return False
