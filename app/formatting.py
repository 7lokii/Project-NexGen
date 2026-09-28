"""Helpers de formatação e tempo (extraídos de app/state.py).

Funções puras usadas nas notificações, na dashboard e nos controles de
tempo — sem dependências do estado do bot. `app/state.py` re-exporta tudo
com os mesmos nomes (tests/ e estudos importam de `app.state`), por isso
este módulo é um corte puro, sem mudar comportamento.
"""

from typing import Optional


def cur(v) -> str:
    """Formata um valor monetário para as notificações (ex.: '1,234.56 €')."""
    try:
        return f"{float(v):,.2f} €"
    except (TypeError, ValueError):
        return str(v)


def _tps_text(obj) -> str:
    """TP1 1.0890 | TP2 1.0920 | ... — every TP present on a Signal/Trade."""
    parts = []
    for i in range(1, 6):
        tp = float(getattr(obj, f"take_profit_{i}", 0.0) or 0.0)
        if tp > 0:
            parts.append(f"TP{i} {tp}")
    return " | ".join(parts)


def _strategy_params_text(p: dict) -> str:
    """Descrição dos parâmetros de execução de uma estratégia (max_legs,
    exit_at_tp, lots_override, gestão) — usada na notificação de promoção
    e no badge da dashboard."""
    parts = []
    legs = p.get("max_legs")
    parts.append(f"{legs} pernas (TP1..TP{legs})" if legs else "todas as pernas (TP1..TP5)")
    if p.get("exit_at_tp"):
        parts.append(f"fecha tudo no TP{p['exit_at_tp']}")
    if p.get("lots_override"):
        parts.append(f"lote fixo {p['lots_override']}")
    parts.append("gestão do grupo ON" if p.get("manage") else "sem gestão do grupo")
    return " · ".join(parts)


def _lots_text(trade) -> str:
    """1.5+1.5+1.5 — lots of every sized leg of a Trade."""
    parts = []
    for i in range(1, 6):
        lots = float(getattr(trade, f"lots_{i}", 0.0) or 0.0)
        if lots > 0:
            parts.append(str(lots))
    return "+".join(parts) if parts else "0"


def _now_iso_plus(seconds: float) -> str:
    """ISO timestamp `seconds` in the future (for edit deadlines)."""
    from datetime import datetime, timedelta, timezone
    # UTC explícito — ver `_now_iso` (engine): a dashboard mostra a contagem
    # da janela de edição correta em qualquer fuso do browser.
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _iso_age_seconds(iso: Optional[str]) -> Optional[float]:
    """Idade (em segundos) de um timestamp ISO face a agora; None se inválido."""
    if not iso:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (datetime.now().astimezone() - dt).total_seconds()
