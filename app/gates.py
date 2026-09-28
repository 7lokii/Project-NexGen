"""Guardas de entrada e gates de risco (extraído de app/state.py).

Decisões puras de proteção — nenhuma abre/fecha ordens: devolvem o veredicto
(`in_zone`/`tolerated`/`blocked`, lote mínimo dominado, nº de pernas que cabe
no orçamento) e o chamador decide. Corte puro num Mixin misturado no BotState.
"""
from __future__ import annotations

from typing import Optional

from app.formatting import _iso_age_seconds
from app.models import Signal


class GatesMixin:
    """Quote da lane, entry guard, gate de lote mínimo e helpers de entrada."""

    def _current_execution_quote(self, symbol: str, lane_id: str = "main") -> Optional[dict]:
        """Return the quote used by one execution lane.

        A connected MT5 provider is authoritative for that account. A dry-run
        MT5 lane without its own terminal uses the shared market feed, so the
        entry guard does not silently disappear for lane 2. Pure SimEngine
        studies keep the historical ``None`` behaviour.
        """
        engine = self._lane_engine(lane_id)
        provider = getattr(engine, "provider", None)
        if provider is not None and getattr(provider, "connected", False):
            try:
                return provider.quote(symbol)
            except Exception:
                return None
        if (provider is not None and bool(getattr(engine, "dry_run", False))
                and self.market.price(symbol) is not None):
            price = float(self.market.price(symbol))
            return {
                "symbol": symbol, "bid": price, "ask": price,
                "spread": 0.0, "spread_pips": 0.0, "source": "market-feed",
            }
        return None

    def _entry_guard(self, signal: Signal, lane_id: str = "main") -> dict:
        """Check whether the broker price is still compatible with the entry zone.

        Inside the zone -> ``in_zone``; outside but within the configured
        multiple of the zone width -> ``tolerated``. Small deviations up to
        ``entry_guard_tolerance`` are marked ``auto_execute``; larger ones
        rest as pending for explicit operator confirmation (the kept
        confirm route — not the manual mode removed in Item 12). Farther
        away -> ``blocked``. This guard never guesses a TP.
        """
        result = {
            "status": "not_applicable", "market_price": None,
            "distance": None, "zone_low": signal.entry_zone_low,
            "zone_high": signal.entry_zone_high, "width": None,
            "max_distance": None, "tolerance": float(self.config.get("entry_guard_tolerance", 0.5) or 0.0),
            "reason": "sem quote MT5 ou sem zona explícita",
        }
        if not bool(self.config.get("entry_guard_enabled", True)):
            result["status"] = "disabled"
            result["reason"] = "guard desligado na configuração"
            return result
        quote = self._current_execution_quote(signal.symbol, lane_id)
        if (quote is None or signal.entry_zone_low is None
                or signal.entry_zone_high is None):
            return result
        price = float(quote["ask"] if signal.direction == "buy" else quote["bid"])
        low, high = sorted((float(signal.entry_zone_low), float(signal.entry_zone_high)))
        width = high - low
        if width <= 0:
            result["status"] = "not_applicable"
            result["reason"] = "zona sem largura"
            return result
        outside = max(low - price, 0.0, price - high)
        tolerance = max(0.0, float(self.config.get("entry_guard_tolerance", 0.5) or 0.0))
        limit = width * float(self.config.get("entry_guard_max_zone_widths", 5.0) or 5.0)
        result.update({"market_price": price, "distance": outside,
                       "zone_low": low, "zone_high": high, "width": width,
                       "max_distance": limit, "tolerance": tolerance})
        if outside <= 0:
            result["status"] = "in_zone"
            result["reason"] = "preço dentro da zona"
        elif outside <= min(tolerance, limit):
            result["status"] = "tolerated"
            result["auto_execute"] = True
            result["reason"] = f"preço {outside:.2f} fora da zona, dentro da tolerância automática de {tolerance:.2f}"
        elif outside <= limit:
            result["status"] = "tolerated"
            result["auto_execute"] = False
            result["reason"] = f"preço fora da zona, mas dentro de {limit / width:.1f}× a largura — confirmação manual necessária"
        else:
            result["status"] = "blocked"
            result["reason"] = f"preço {outside:.2f} fora da zona (limite {limit:.2f})"
        result["lane_id"] = lane_id
        return result

    def _min_lot_risk_gate(self, signal: Signal, balance: float,
                           risk_pct: float, max_legs: Optional[int] = None) -> dict:
        """Estima a perda máxima real da trade e compara com o risco pedido.

        Com contas pequenas, o lote mínimo (0.01) por perna pode criar uma
        exposição MAIOR do que o risco percentual configurado. O gate só
        bloqueia quando o lote CALCULADO fica abaixo do mínimo (isto é, o
        mínimo é que domina o dimensionamento) — nunca por arredondamento
        normal. Expõe ambos os valores para a dashboard; não altera a ordem.
        """
        from app.config import MIN_LOTS
        from app.execution.engine import _contract, _pip_size
        from app.risk.engine import compute_lots

        legs = [i for i in range(1, 6)
                if (max_legs is None or i <= max_legs)
                and float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0) > 0]
        n = len(legs) or 1
        contract = _contract(signal.symbol)
        pip_size = _pip_size(signal.symbol)
        sl_distance = abs(float(signal.entry) - float(signal.stop_loss))
        risk_amount = balance * risk_pct / 100.0
        fixed = bool(signal.lots and float(signal.lots) > 0)
        if sl_distance <= 0 or risk_pct <= 0:
            return {"exceeded": False, "risk_amount": round(risk_amount, 2),
                    "max_loss_est": 0.0, "legs": n,
                    "lots_per_leg": float(signal.lots or 0.0),
                    "total_lots": 0.0, "sl_distance": sl_distance,
                    "contract": contract, "fixed_lots": fixed}
        if fixed:
            lots_per_leg = float(signal.lots)
            min_lot_dominated = False
        else:
            risk_pips = sl_distance / pip_size
            pip_value = contract * pip_size
            raw_lots = (risk_amount / n) / (risk_pips * pip_value)
            lots_per_leg = compute_lots(
                balance, risk_pct / n, signal.entry, signal.stop_loss,
                pip_size=pip_size, contract_size=contract,
            )
            min_lot_dominated = raw_lots < MIN_LOTS
        total_lots = lots_per_leg * n
        max_loss = total_lots * sl_distance * contract
        return {
            "exceeded": bool(min_lot_dominated) and max_loss > risk_amount + 0.01,
            "risk_amount": round(risk_amount, 2),
            "max_loss_est": round(max_loss, 2),
            "legs": n,
            "lots_per_leg": round(lots_per_leg, 4),
            "total_lots": round(total_lots, 4),
            "sl_distance": sl_distance,
            "contract": contract,
            "fixed_lots": fixed,
        }

    def _fit_legs_to_risk(self, signal: Signal, balance: float,
                          risk_pct: float, max_legs: Optional[int] = None,
                          gate: Optional[dict] = None) -> int:
        """Quantas pernas cabem no orçamento de risco com o lote mínimo?

        Quando o lote mínimo domina o dimensionamento (``gate['exceeded']``),
        reduzir o nº de pernas é a única forma de cumprir o risco configurado
        sem rejeitar o sinal (modo autónomo). Devolve o nº máximo de pernas
        (≥ 1) cuja perda máxima estimada cabe no orçamento, ou 0 se nem uma
        perna couber (aí o sinal é rejeitado — nada de sensato a executar).
        """
        from app.config import MIN_LOTS
        from app.execution.engine import _contract
        legs = [i for i in range(1, 6)
                if (max_legs is None or i <= max_legs)
                and float(getattr(signal, f"take_profit_{i}", 0.0) or 0.0) > 0]
        if not legs:
            return 0
        sl_distance = abs(float(signal.entry) - float(signal.stop_loss))
        risk_amount = balance * risk_pct / 100.0
        if sl_distance <= 0 or risk_amount <= 0:
            return len(legs)
        per_leg_loss = MIN_LOTS * sl_distance * _contract(signal.symbol)
        if per_leg_loss <= 0:
            return len(legs)
        return max(0, min(len(legs), int(risk_amount / per_leg_loss)))

    def _entry_style(self) -> str:
        """Estilo de entrada: market (preço atual) ou pending (ordem na zona).
        Fora da zona dentro do cap, pending coloca uma ordem pendente no
        nível da entrada em vez de perseguir o preço a mercado (Fase 1)."""
        style = str(self.config.get("entry_style") or "pending").strip().lower()
        return style if style in ("market", "pending") else "pending"

    def _entry_order_ttl_seconds(self) -> Optional[float]:
        """TTL das ordens pendentes da zona em segundos (None = sem expiração)."""
        hours = float(self.config.get("entry_order_ttl_hours") or 0.0)
        return hours * 3600.0 if hours > 0 else None

    async def _expire_stale_pending(self) -> int:
        """Modo autónomo: sinais `pending` antigos são rejeitados.

        Sem confirmação humana, um sinal pendente não se resolve sozinho —
        o tick rejeita-o após `pending_expire_minutes` para não acumular
        confirmações mortas. 0 na config desliga a expiração.
        """
        minutes = float(self.config.get("pending_expire_minutes") or 0.0)
        if minutes <= 0:
            return 0
        cutoff = minutes * 60.0
        expired = 0
        for sig in self.db.list_pending_signals(limit=100):
            age = _iso_age_seconds(getattr(sig, "received_at", None))
            if age is not None and age > cutoff:
                self.db.set_signal_status(
                    sig.id, "rejected",
                    error=(f"expirou após {minutes:.0f} min sem confirmação "
                           f"— modo autónomo (rejeitado automaticamente)"),
                )
                expired += 1
        if expired:
            await self.notifier.send(
                "system",
                f"⏳ {expired} sinal(is) pendente(s) expirados automaticamente "
                "(modo autónomo — sem confirmações humanas pendentes).",
            )
        return expired

