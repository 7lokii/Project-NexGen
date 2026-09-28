"""Conta e lanes de execução (extraído de app/state.py).

Saldo/equity por lane, risco dinâmico por drawdown, posições MT5 enriquecidas
e o snapshot resumido de cada conta. Corte puro num Mixin misturado no
BotState — nenhuma ordem é enviada aqui.
"""
from __future__ import annotations

import os
import re
from datetime import date
from typing import List, Optional

from app.models import Position, Trade


class LanesMixin:
    """Conta por lane: saldo, equity, pico, risco efetivo e posições."""

    def _dry_run_summary(self, lane_id: str = "main") -> dict:
        """Local simulated P&L/equity, kept separate from broker balance.

        O P&L fechado vem de um SUM SQL (a versão antiga varria 100k trades
        a cada snapshot — o resumo agora é O(1))."""
        broker = self._mt5_account(lane_id)
        engine = self._lane_engine(lane_id)
        is_dry = bool(getattr(engine, "dry_run", False)) and (
            broker is not None or getattr(engine, "provider", None) is not None
        )
        if not is_dry:
            return {"enabled": False, "starting_balance": None,
                    "realized_pnl": 0.0, "equity": None, "currency": self._currency()}
        start = self._balance_for_lane(lane_id)
        closed = self.db.dry_run_realized_pnl(lane_id)
        open_pnl = 0.0
        try:
            from app.execution.engine import _contract
            for pos in self.db.open_positions():
                if (getattr(pos, "lane_id", "main") or "main") != lane_id:
                    continue
                quote = self._current_execution_quote(pos.symbol, lane_id)
                price = ((quote["bid"] + quote["ask"]) / 2.0) if quote else self.market.price(pos.symbol)
                if price is not None:
                    sign = 1.0 if pos.direction == "buy" else -1.0
                    open_pnl += sign * (float(price) - float(pos.entry)) * float(pos.lots) * _contract(pos.symbol)
        except Exception:
            open_pnl = 0.0
        return {"enabled": True, "starting_balance": round(start, 2),
                "realized_pnl": round(closed, 2),
                "equity": round(start + closed + open_pnl, 2),
                "currency": (broker or {}).get("currency", self._currency())}

    def _mt5_account(self, lane_id: str = "main") -> Optional[dict]:
        """Account info from the MT5 terminal when execution runs through it.

        When EXECUTION_MODE=mt5, the broker account is the source of truth
        for balance/equity — the config balance only applies to the paper
        (sim) engine. `lane_id` seleciona a conta (main vs segunda demo).
        """
        engine = self._lane_engine(lane_id)
        provider = getattr(engine, "provider", None)
        if provider is not None and provider.connected:
            return provider.account()
        return None

    def _currency(self) -> str:
        acct = self._mt5_account()
        return (acct or {}).get("currency", "USD")

    def _lane2_synthetic_account(self) -> Optional[dict]:
        """Conta apresentada para a 2ª lane em dry-run (sem terminal 2).

        Em dry-run a lane 2 não abre uma ligação MT5 (o pacote MetaTrader5
        mantém UMA ligação por processo), por isso não há account_info real.
        Sintetizamos o login/balanço a partir do .env para a dashboard e o
        rótulo do WhatsApp — nunca envia ordens.
        """
        if getattr(self, "sim2", None) is None:
            return None
        login = os.getenv("MT5_2_LOGIN")
        if not login:
            return None
        balance = float(self.config.get("lane2_balance") or self.config.get("balance") or 0.0)
        return {
            "login": int(login) if login.isdigit() else login,
            "server": os.getenv("MT5_2_SERVER", ""),
            "currency": "EUR",
            "balance": balance,
            "equity": balance,
            "dry_run": True,
        }

    def _lanes_snapshot(self) -> List[dict]:
        """Estado resumido por lane para a dashboard/diagnóstico.

        Cada entrada é autocontida para a vista ``?lane=`` da dashboard:
        conta (balanço/equity), P&L do dia/aberto, trades abertas, risco
        efetivo e o dimensionamento de lotes da lane (risk_preview)."""
        out = []
        for lane_id in self._lanes():
            strat = self._lane_strategy(lane_id)
            engine = self._lane_engine(lane_id)
            acct = self._mt5_account(lane_id)
            if acct is None and lane_id == "tp2":
                acct = self._lane2_synthetic_account()
            dry = bool(getattr(engine, "dry_run", False))
            has_provider = getattr(engine, "provider", None) is not None
            try:
                today = self._realized_today(lane_id)
            except Exception:
                today = 0.0
            out.append({
                "lane_id": lane_id,
                "strategy_id": strat["id"],
                "strategy_name": strat["nome"],
                "max_legs": strat["max_legs"],
                "account": acct,
                "connected": bool(getattr(getattr(engine, "provider", None), "connected", False)),
                "mode": ("mt5-blocked" if getattr(getattr(engine, "provider", None), "execution_block_reason", "")
                         else "mt5-dry" if dry else
                         "mt5-real" if has_provider else "sim"),
                "enabled": True,
                "balance": round(self._balance_for_lane(lane_id), 2),
                "equity": round(self._equity_for_lane(lane_id), 2),
                "open_pnl": round(self._open_pnl(lane_id), 2),
                "realized_today": round(float(today), 2),
                "open_trades": len(self._open_trades(lane_id)),
                "effective_risk_pct": round(self._effective_risk_pct_for_lane(lane_id), 2),
                "risk_base_pct": round(self._base_risk_for_lane(lane_id), 2),
                "fixed_lots_per_leg": self._fixed_lots_for_lane(lane_id),
                "risk_preview": self.risk_preview(lane_id),
                "dry_run": self._dry_run_summary(lane_id),
            })
        return out

    def _mt5_positions(self) -> list:
        """Live MT5 positions for the dashboard (empty in sim mode).

        Enriches each broker position with the app-level context: the leg
        number and trade id (parsed from the order comment `t-xxx:legN`) and
        the linked signal (source/group) so the dashboard can show "perna 3
        de sinal X" instead of a raw ticket list.
        """
        trades = {t.id: t for t in self.db.open_trades()}
        out = []
        for lane_id in self._lanes():
            engine = self._lane_engine(lane_id)
            provider = getattr(engine, "provider", None)
            if provider is None or not provider.connected:
                continue
            try:
                broker_positions = provider.positions()
            except Exception:
                broker_positions = []
            account = provider.account() or {}
            for p in broker_positions:
                item = dict(p)
                item["lane_id"] = lane_id
                item["account_login"] = account.get("login")
                comment = item.get("comment") or ""
                if ":" in comment:
                    trade_id, leg_part = comment.split(":", 1)
                    item["trade_id"] = trade_id
                    m = re.search(r"leg(\d+)", leg_part)
                    item["leg"] = int(m.group(1)) if m else 0
                    t = trades.get(trade_id)
                    if t is not None:
                        item["signal_symbol"] = t.symbol
                        item["signal_direction"] = t.direction
                        item["strategy_id"] = t.strategy_id or self._lane_strategy_id(lane_id)
                        item["tp"] = float(getattr(t, f"take_profit_{item['leg']}", 0.0) or 0.0)
                        sig = self.db.get_signal(t.signal_id)
                        item["signal_source"] = (sig.source if sig else "") or ""
                out.append(item)
        return out

    def _balance(self) -> float:
        return self._balance_for_lane("main")

    def _open_positions(self, lane_id: Optional[str] = None) -> List[Position]:
        rows = self.db.open_positions()
        if lane_id is None:
            return rows
        return [p for p in rows
                if (getattr(p, "lane_id", None) or "main") == lane_id]

    def _open_trades(self, lane_id: Optional[str] = None) -> List[Trade]:
        rows = self.db.open_trades()
        if lane_id is None:
            return rows
        return [t for t in rows
                if (getattr(t, "lane_id", None) or "main") == lane_id]

    # -------------------------------------------------------- execution lanes

    def _lanes(self) -> List[str]:
        """Lanes de execução ativas ("main" sempre; "tp2" quando configurada)."""
        lanes = ["main"]
        if getattr(self, "sim2", None) is not None:
            lanes.append("tp2")
        return lanes

    def _lane_engine(self, lane_id: str):
        if lane_id == "main":
            return self.sim
        return getattr(self, "sim2", None)

    def _lane_strategy_id(self, lane_id: str) -> str:
        if lane_id == "main":
            return self.config.get("strategy", "nova_gestao")
        return self.lane_strategies.get(
            lane_id, self.config.get("lane2_strategy", "tp2_protect"),
        )

    def _lane_strategy(self, lane_id: str) -> dict:
        from app.strategies import MAIN_STRATEGY_ID, STRATEGIES, get_strategy
        sid = self._lane_strategy_id(lane_id)
        try:
            return get_strategy(sid)
        except ValueError:
            # config com id removido/morto (restore de BD antiga): a lane
            # executa com a principal e o snapshot marca o badge órfão — o
            # tick nunca rebenta; os ESTUDOS é que falham em voz alta
            # (get_strategy levanta ValueError fora do runtime).
            return STRATEGIES[MAIN_STRATEGY_ID]

    def _engine_for_trade(self, trade) :
        """Engine que abriu (e gere) uma trade, a partir do seu lane_id."""
        lane = getattr(trade, "lane_id", None) or "main"
        return self._lane_engine(lane)

    def _lane_worker_status_text(self, lane_id: str = "tp2") -> str:
        """Estado do worker/terminal da lane em texto curto para alertas.

        Best-effort e nunca bloqueia: sem worker, devolve uma nota genérica.
        Usado nos alertas de retcode null ("?") — o operador precisa de saber
        se o worker está alcancável/ligado/AutoTrading antes de agir.
        """
        engine = self._lane_engine(lane_id)
        provider = getattr(engine, "provider", None) if engine is not None else None
        if provider is None:
            return f"Lane {lane_id}: motor sem provider (dry-run/sim)."
        try:
            if hasattr(provider, "status_summary"):
                st = provider.status_summary()
                parts = []
                parts.append("reachable" if st.get("reachable") else "worker INALCANÇÁVEL")
                parts.append("ligado" if st.get("connected") else "worker DESLIGADO")
                parts.append("AT ON" if st.get("trade_allowed") else "AutoTrading OFF")
                if st.get("login"):
                    parts.append(f"login {st['login']}")
                err = st.get("last_error") or ""
                if err:
                    parts.append(f"último erro: {err[:120]}")
                return f"Worker lane 2: {", ".join(parts)}."
            acct = provider.account() if provider.connected else None
            login = acct.get("login") if acct else None
            state = "ligado" if provider.connected else "DESLIGADO"
            base = f"Terminal lane {lane_id}: {state}"
            if login:
                base += f" (login {login})"
            return base + "."
        except Exception:
            return f"Lane {lane_id}: estado do worker indisponível (consulta falhou)."

    def lane_ab_report(self, min_comparable: int = 20) -> dict:
        """A/B read-only das lanes no mesmo sinal (ver `lane_ab_report`)."""
        return lane_ab_report(self.db, min_comparable=max(1, int(min_comparable)))

    def _balance_for_lane(self, lane_id: str = "main") -> float:
        acct = self._mt5_account(lane_id)
        if acct:
            balance = acct.get("balance")
            if balance is not None:
                return float(balance)
        if lane_id == "tp2" and float(self.config.get("lane2_balance", 0.0) or 0.0) > 0:
            return float(self.config["lane2_balance"])
        return float(self.config["balance"])

    def _open_pnl(self, lane_id: str = "main") -> float:
        acct = self._mt5_account(lane_id)
        if acct:
            return round(float(acct["equity"]) - float(acct["balance"]), 2)
        total = 0.0
        from app.execution.engine import _contract
        for p in self._open_positions(lane_id):
            price = self.market.price(p.symbol)
            if price is not None:
                sign = 1.0 if p.direction == "buy" else -1.0
                total += sign * (price - p.entry) * p.lots * _contract(p.symbol)
        return round(total, 2)

    def _equity_for_lane(self, lane_id: str = "main") -> float:
        return round(self._balance_for_lane(lane_id) + self._open_pnl(lane_id), 2)

    def _realized_today(self, lane_id: str = "") -> float:
        day = date.today().isoformat()
        if not lane_id:
            return self.db.realized_today(day)
        return sum(float(t.pnl or 0.0) for t in self.db.trades_closed_on(day)
                   if (getattr(t, "lane_id", None) or "main") == lane_id)

    def _daily_loss_killed(self) -> bool:
        """Kill diário avaliado POR LANE contra o saldo da própria conta.

        Com várias lanes MT5 (contas com saldos muito diferentes), somar o
        P&L realizado de todas contra o saldo da principal disparava o kill
        automaticamente com cada lane individualmente bem dentro do limite
        (07/09: -52,36 € da tp2 10k + -1,11 € da principal = -5,17% do saldo
        de 1.033 €). Agora cada conta respira pelo seu próprio saldo; a
        lane principal continua a ser avaliada contra o saldo configurado
        quando não há conta MT5 (sim/dry-run).
        """
        day = date.today().isoformat()
        evaluated = False
        for lane_id in self._lanes():
            acct = self._mt5_account(lane_id)
            if acct is None and lane_id == "tp2":
                # lane 2 em dry-run sem worker: conta sintética do .env
                acct = self._lane2_synthetic_account()
            if acct and acct.get("balance") is not None:
                evaluated = True
                if self.risk.daily_loss_exceeded(
                    float(acct["balance"]),
                    self._realized_today(lane_id),
                    self.config,
                ):
                    return True
        if not evaluated:
            # sem nenhuma conta MT5 (sim puro): comportamento legado — o P&L
            # total do dia contra o saldo configurado
            return self.risk.daily_loss_exceeded(
                self._balance(), self._realized_today(), self.config)
        return False

    def equity(self, lane_id: str = "main") -> float:
        acct = self._mt5_account(lane_id)
        if acct:
            return round(float(acct["equity"]), 2)
        return self._equity_for_lane(lane_id)

    def _update_peak_balance(self) -> None:
        """Mantém o high-watermark de cada lane.

        Um pico muito acima do saldo atual é tratado como legado de uma conta
        diferente (por exemplo 100.000€ persistidos numa conta de 1.000€).
        Um drawdown plausível não é apagado; só rebases claramente absurdos.
        """
        for lane_id in self._lanes():
            eq = self.equity(lane_id)
            key = "peak_balance" if lane_id == "main" else f"peak_balance:{lane_id}"
            peak = float(self.config.get(key) or 0.0)
            if eq > 0 and peak > max(eq * 10.0, eq + 10000.0):
                peak = eq
                self.config[key] = peak
                self.db.set_config(key, peak)
            elif peak <= 0 or eq > peak:
                self.config[key] = eq
                self.db.set_config(key, eq)

    def _fixed_lots_for_lane(self, lane_id: str = "main") -> Optional[float]:
        """Lote fixo por perna da lane (`lots_override:<lane>` na config).
        Quando definido (>0), cada perna da lane abre SEMPRE nesse lote em
        vez do dimensionamento por risco (a distância ao SL deixa de mudar o
        lote — ex.: 0.04 na 2ª conta = 0.04 por perna em todas as trades).
        0/sem chave = dimensionamento por risco normal."""
        try:
            v = float(self.config.get(f"lots_override:{lane_id}") or 0.0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _base_risk_for_lane(self, lane_id: str = "main") -> float:
        """Base do risco por trade DE CADA conta: `risk_pct:main` /
        `risk_pct:tp2` na config; sem chave própria, a lane cai no legado
        `risk_pct` (as duas começam iguais e só divergem quando o operador
        definir uma base própria — ex.: 2% na 2ª conta)."""
        if lane_id == "tp2":
            return float(self.config.get("risk_pct:tp2")
                         or self.config.get("risk_pct", 1.0))
        return float(self.config.get("risk_pct:main")
                     or self.config.get("risk_pct", 1.0))

    def _effective_risk_pct_for_lane(self, lane_id: str = "main") -> float:
        """Dynamic risk based on the lane's own account/equity peak and the
        lane's own risk base (risk_pct:main / risk_pct:tp2)."""
        base = self._base_risk_for_lane(lane_id)
        balance = self._balance_for_lane(lane_id)
        equity = balance
        acct = self._mt5_account(lane_id)
        if acct:
            equity = float(acct.get("equity", balance))
        else:
            for p in self._open_positions():
                if (getattr(p, "lane_id", None) or "main") != lane_id:
                    continue
                price = self.market.price(p.symbol)
                if price is not None:
                    sign = 1.0 if p.direction == "buy" else -1.0
                    from app.execution.engine import _contract
                    equity += sign * (float(price) - float(p.entry)) * float(p.lots) * _contract(p.symbol)
        key = "peak_balance" if lane_id == "main" else f"peak_balance:{lane_id}"
        peak = float(self.config.get(key) or 0.0)
        # Corrige o high-watermark legado antes de calcular o risco efetivo.
        if equity > 0 and peak > max(equity * 10.0, equity + 10000.0):
            peak = equity
            self.config[key] = peak
            self.db.set_config(key, peak)
        if peak <= 0 or equity > peak:
            peak = equity
            self.config[key] = peak
            self.db.set_config(key, peak)
        dd = max(0.0, (peak - equity) / peak * 100.0) if peak else 0.0
        return max(0.1, base * (0.5 ** (dd / 20.0)))

    def _effective_risk_pct(self) -> float:
        """Risco dinâmico: o `risk_pct` base escala para baixo após perdas.

        Regra: cada -20% de drawdown desde o pico de equity corta o risco a
        METADE (`0.5 ** (drawdown / 20)`), com piso de 0.1%. Se ainda não há
        pico registado, devolve o base sem escala.
        """
        base = float(self.config.get("risk_pct", 1.0))
        peak = float(self.config.get("peak_balance") or 0.0)
        eq = self.equity()
        if peak <= 0:
            return base
        dd = max(0.0, (peak - eq) / peak * 100.0)
        scale = 0.5 ** (dd / 20.0)
        return max(0.1, base * scale)

    def risk_preview(self, lane_id: str = "main") -> dict:
        """Lotes que o risco% atual representa numa trade típica do grupo
        (XAUUSD): total e por perna (TP1..TP5). O lote depende da distância
        do SL — usa a distância real do último sinal válido quando existir,
        senão um SL típico de 30 pontos de ouro.

        `lane_id` dimensiona para a conta/lane pedida: usa o saldo, o risco
        dinâmico (drawdown) e o max_legs da ESTRATÉGIA da lane — a 2ª conta
        (10.000 €) não pode usar o saldo nem as pernas da principal.

        Cache TTL de 5s por lane: o snapshot chama-o 3× por tick (bloco
        config + lanes) e o `last_signal_with_sl`/balanço mudam devagar —
        sem cache cada chamada pagava uma query DB + leituras de conta."""
        return self._ttl_get(f"risk_preview:{lane_id}", 5.0,
                             lambda: self._risk_preview_compute(lane_id))

    def _risk_preview_compute(self, lane_id: str) -> dict:
        """Cálculo do risk_preview (sem cache) — ver `risk_preview`."""
        from app.execution.mt5 import FALLBACK_SYMBOLS
        meta = FALLBACK_SYMBOLS.get("XAUUSD", {})
        contract = float(meta.get("contract", 100.0))
        pip = float(meta.get("pip", 0.01))
        step = float(meta.get("vol_step", 0.01))
        vmin = float(meta.get("vol_min", 0.01))
        vmax = float(meta.get("vol_max", 100.0))
        # Para a conta MT5, o saldo é sempre o saldo/equity real do broker.
        # Em sim/dry-run, a conta operacional começa no saldo configurado;
        # nunca usar um saldo histórico fixo de €1000 para dimensionar ordens.
        balance = self._balance_for_lane(lane_id)
        risk_pct = self._effective_risk_pct_for_lane(lane_id)  # risco dinâmico
        fixed = self._fixed_lots_for_lane(lane_id)
        # distância do SL: usa a do último sinal válido (entrada+SL) se houver
        sl_dist = 30.0  # pontos de ouro (3.0 de preço) — SL típico do grupo
        sig = self.db.last_signal_with_sl()
        if sig:
            sl_dist = abs(float(sig["entry"]) - float(sig["stop_loss"]))
        n_legs = self._lane_strategy(lane_id).get("max_legs") or 5
        if fixed:
            # lote fixo por perna definido para a lane: o risco deixa de
            # dimensionar o lote — cada perna abre no lote fixo (o risco
            # monetário varia com a distância ao SL de cada sinal)
            lots_per_leg = round(max(vmin, fixed), 2)
            return {
                "symbol": "XAUUSD", "lane_id": lane_id,
                "balance": round(balance, 2),
                "risk_pct": round(risk_pct, 2),
                "sl_dist": round(sl_dist, 2),
                "lots_total": round(lots_per_leg * n_legs, 2),
                "lots_per_leg": lots_per_leg,
                "n_legs": n_legs,
                "lots_override": lots_per_leg,  # fixo por perna, não por risco
            }
        risk_amount = balance * risk_pct / 100.0
        risk_pips = sl_dist / pip
        lots_total = risk_amount / (risk_pips * contract * pip)
        lots_total = max(vmin, min(vmax, round(lots_total / step) * step))
        return {
            "symbol": "XAUUSD",
            "lane_id": lane_id,
            "balance": round(balance, 2),
            "risk_pct": round(risk_pct, 2),
            "sl_dist": round(sl_dist, 2),
            "lots_total": round(lots_total, 2),
            "lots_per_leg": round(max(vmin, lots_total / n_legs), 2),
            "n_legs": n_legs,
        }



def lane_ab_legs(db, signal_id: str) -> dict:
    """Confronto PERNA-A-PERNA de um sinal entre as lanes main e tp2.

    Read-only: junta as trades reais do sinal (uma por lane) com as pernas
    (`positions_for_trade`) e a linha temporal de execução vinda da fita do
    journal (`execution_tape`) — fills, modifies de SL e retcodes, com a
    hora exata de cada evento. Sem journal ativo, o confronto cai nas
    pernas da BD (fills/timings marcados como "sem journal").

    Nunca envia ordens nem altera estado — é observação pura para a tab
    Confronto da dashboard.
    """
    def _is_void(t) -> bool:
        return not (getattr(t, "exit_reason", None) or "")

    def _leg_view(p) -> dict:
        return {
            "leg": p.leg,
            "lots": p.lots,
            "entry": p.entry,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "status": p.status,
            "opened_at": p.opened_at,
            "closed_at": p.closed_at,
            "close_price": p.close_price,
            "pnl": round(float(p.pnl or 0.0), 2),
            "exit_reason": p.exit_reason or "",
            "order_ticket": p.order_ticket,
        }

    trades = db.trades_for_signal(signal_id)
    if not trades:
        return {"ok": True, "signal_id": signal_id, "trades": {},
                "journal": [], "journal_enabled": False, "found": False}
    main_t = next((t for t in trades if (t.lane_id or "main") == "main"), None)
    tp2_t = next((t for t in trades if (t.lane_id or "main") == "tp2"), None)

    lanes = {}
    for lane_id, t in (("main", main_t), ("tp2", tp2_t)):
        if t is None:
            lanes[lane_id] = None
            continue
        legs = sorted(db.positions_for_trade(t.id),
                      key=lambda p: (p.leg or 0))
        lanes[lane_id] = {
            "strategy_id": t.strategy_id or "",
            "status": t.status,
            "opened_at": t.opened_at,
            "closed_at": t.closed_at,
            "entry": t.entry,
            "direction": t.direction,
            "symbol": t.symbol,
            "pnl": round(float(t.pnl or 0.0), 2) if t.pnl is not None else None,
            "exit_reason": t.exit_reason or "",
            "void": _is_void(t),
            "error": t.error,
            "retcodes": t.retcodes,
            "auto_trading_off": t.auto_trading_off,
            "legs": [_leg_view(p) for p in legs],
        }

    # fita do journal do sinal: ordenada, com a lane atribuída pelo trade_id
    # (cada evento carrega o id da trade real da lane que o gerou — a
    # exportação `/api/operations/replay/tape` tira o trade_id, por isso a
    # leitura aqui é dos eventos crus na janela de vida das trades)
    trade_ids = {t.id for t in trades}
    lane_by_trade = {}
    for lane_id, t in (("main", main_t), ("tp2", tp2_t)):
        if t is not None:
            lane_by_trade[t.id] = lane_id
    journal = []
    try:
        start = min((t.opened_at or "" for t in trades), default="") or ""
        until = max((t.closed_at or "" for t in trades), default="") or ""
        for row in db.execution_events(since=start, until=until, limit=20000):
            payload = row.get("payload") or {}
            kind = row.get("kind", "")
            if kind not in ("order_request", "order_result", "modify_request",
                            "modify_result", "fill_open", "fill_close",
                            "delete_request", "order_delete"):
                continue
            tid = str(row.get("trade_id") or payload.get("trade_id") or "")
            lane = lane_by_trade.get(tid) or payload.get("lane") or ""
            if not lane or tid not in trade_ids:
                continue  # sem trade das lanes = não é desta trade (quotas etc.)
            journal.append({
                "at": row.get("at", ""),
                "kind": kind,
                "engine": row.get("engine", ""),
                "trade_id": tid,
                "lane": lane,
                "payload": payload,
                "summary": _journal_summary(kind, payload),
            })
        journal.sort(key=lambda r: r["at"])
    except Exception as exc:
        journal = [{"error": str(exc)}]

    return {"ok": True, "signal_id": signal_id, "found": True,
            "trades": lanes, "journal": journal,
            "journal_enabled": bool(json_config(db, "execution_journal_enabled")),
            "symbol": (main_t or tp2_t).symbol if (main_t or tp2_t) else "",
            "direction": (main_t or tp2_t).direction if (main_t or tp2_t) else "",
            "entry": (main_t or tp2_t).entry if (main_t or tp2_t) else None}


def json_config(db, key: str, default=None):
    """Valor bool da config (as leituras devolvem str/tipo nativo)."""
    try:
        value = (db.all_config() or {}).get(key, default)
    except Exception:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _journal_summary(kind: str, payload: dict) -> str:
    """Resumo legível de um evento da fita (fills/moves/retcodes)."""
    def _p(*keys):
        for k in keys:
            if payload.get(k) is not None:
                return payload.get(k)
        return None

    leg = _p("leg", "position_id")
    leg_txt = f"perna {leg}" if leg is not None else ""
    price = _p("price", "fill_price", "close_price", "entry")
    price_txt = f" @ {price:g}" if isinstance(price, (int, float)) else ""
    retcode = _p("retcode")
    ret_txt = f" · retcode {retcode}" if retcode is not None else ""
    if kind == "order_result":
        ok = bool(_p("deal") or _p("ticket") or _p("order")) or retcode in (0, 10009)
        return f"Ordem {('aberta' if ok else 'recusada')}{price_txt}{ret_txt}".rstrip()
    if kind == "fill_open":
        ticket = _p("ticket") or _p("order")
        return f"Fill {leg_txt}{price_txt}" + (f" · ticket {ticket}" if ticket else "") + ret_txt
    if kind == "fill_close":
        reason = _p("reason", "exit_reason")
        return f"Fecho {leg_txt}{price_txt}" + (f" · {reason}" if reason else "") + ret_txt
    if kind in ("modify_request", "modify_result"):
        target = _p("target", "sl", "stop_loss")
        valid = _p("valid")
        ok = "" if retcode is None else (" · aceite" if retcode in (0, 10009) else "")
        txt = f"Move SL {leg_txt} → {target:g}" if isinstance(target, (int, float)) else f"Modify {leg_txt}"
        txt += f"{ret_txt}{ok}"
        if valid is not None and isinstance(valid, (int, float)):
            txt += f" · valid {valid:g}"
        return txt
    if kind in ("delete_request", "order_delete"):
        return f"Cancelamento {leg_txt}{ret_txt}".rstrip()
    return f"{kind}{leg_txt}{price_txt}{ret_txt}".rstrip()


def lane_ab_report(db, min_comparable: int = 20) -> dict:
    """A/B read-only das lanes de execução (main vs tp2) no mesmo sinal.

    Compara trades FECHADAS que existem nas DUAS lanes para o mesmo sinal:
    P&L lado a lado, win rate, profit factor e quem sai por cima sinal a
    sinal. Fechos **phantom** (sem `exit_reason` — ordens que o broker
    recusou no envio, ex.: AutoTrading OFF, e que o engine fechou a 0 no
    próprio tick) NUNCA entram na comparação: ficam contados em `voids`
    para a amostra não mentir. O relatório é informativo — só sugere
    (nunca executa) e apenas com `min_comparable` sinais comparáveis
    (default 20); abaixo disso a amostra é insuficiente e o veredicto é
    neutro.
    """
    from collections import defaultdict

    def _is_void(t) -> bool:
        """Fecho sem razão de saída = phantom (cleanup de ordens recusadas).

        Todos os fechos reais carregam `exit_reason` (sl/tpN/protect/
        close_all/close_tp1/close_tp/move_sl/killswitch/manual/...); só o
        cleanup do envio recusado (`open_trade` → sent==0) grava 0.0 sem
        razão.
        """
        return not (getattr(t, "exit_reason", None) or "")

    all_trades = db.list_trades(5000)
    by_signal: dict = defaultdict(dict)
    for t in all_trades:
        if getattr(t, "status", "") != "closed":
            continue
        lane = getattr(t, "lane_id", None) or "main"
        by_signal[t.signal_id][lane] = t

    rows = []
    pairs = []
    voids = []
    main_voids = tp2_voids = 0
    for sid, lanes in by_signal.items():
        main_t = lanes.get("main")
        tp2_t = lanes.get("tp2")
        if main_t is None or tp2_t is None:
            continue
        if main_t.pnl is None or tp2_t.pnl is None:
            continue
        m_void = _is_void(main_t)
        t_void = _is_void(tp2_t)
        if m_void or t_void:
            main_voids += m_void
            tp2_voids += t_void
            voids.append({
                "signal_id": sid,
                "symbol": main_t.symbol or tp2_t.symbol or "",
                "direction": main_t.direction or "",
                "entry": main_t.entry,
                "main_strategy": main_t.strategy_id or "",
                "tp2_strategy": tp2_t.strategy_id or "",
                "main_pnl": round(float(main_t.pnl), 2),
                "tp2_pnl": round(float(tp2_t.pnl), 2),
                "main_void": m_void,
                "tp2_void": t_void,
                "closed_at": main_t.closed_at or tp2_t.closed_at or "",
                # trace durável da recusa (backfill + envio novo): o retcode
                # e o erro explicam o phantom sem sair do relatório
                "main_error": main_t.error if m_void else None,
                "main_retcodes": main_t.retcodes if m_void else None,
                "main_auto_trading_off": (
                    main_t.auto_trading_off if m_void else None),
                "tp2_error": tp2_t.error if t_void else None,
                "tp2_retcodes": tp2_t.retcodes if t_void else None,
                "tp2_auto_trading_off": (
                    tp2_t.auto_trading_off if t_void else None),
            })
            continue
        pairs.append((sid, main_t, tp2_t))
        rows.append({
            "signal_id": sid,
            "symbol": main_t.symbol or tp2_t.symbol or "",
            "direction": main_t.direction or "",
            "entry": main_t.entry,
            "main_strategy": main_t.strategy_id or "",
            "tp2_strategy": tp2_t.strategy_id or "",
            "main_pnl": round(float(main_t.pnl), 2),
            "tp2_pnl": round(float(tp2_t.pnl), 2),
            "delta": round(float(tp2_t.pnl) - float(main_t.pnl), 2),
            "main_reason": main_t.exit_reason or "",
            "tp2_reason": tp2_t.exit_reason or "",
            "closed_at": main_t.closed_at or tp2_t.closed_at or "",
        })
    rows.sort(key=lambda r: r["closed_at"] or "", reverse=True)
    voids.sort(key=lambda r: r["closed_at"] or "", reverse=True)

    def metrics(ts) -> dict:
        wins = sum(1 for t in ts if (t.pnl or 0.0) > 0)
        losses = sum(1 for t in ts if (t.pnl or 0.0) < 0)
        total = sum(float(t.pnl or 0.0) for t in ts)
        gp = sum(float(t.pnl or 0.0) for t in ts if (t.pnl or 0.0) > 0)
        gl = -sum(float(t.pnl or 0.0) for t in ts if (t.pnl or 0.0) < 0)
        pf = None
        pf_inf = False
        if gl > 0:
            pf = gp / gl
        elif gp > 0:
            pf = float("inf")
            pf_inf = True
        return {
            "n": len(ts), "wins": wins, "losses": losses,
            "be": len(ts) - wins - losses,
            "total_pnl": round(total, 2),
            "win_rate": round(100.0 * wins / len(ts), 1) if ts else None,
            "pf": None if pf is None or pf_inf else round(pf, 2),
            "pf_infinite": pf_inf,
        }

    n = len(pairs)
    main_wins_n = sum(1 for _, m, t in pairs if (m.pnl or 0.0) > (t.pnl or 0.0))
    tp2_wins_n = sum(1 for _, m, t in pairs if (t.pnl or 0.0) > (m.pnl or 0.0))
    ties_n = n - main_wins_n - tp2_wins_n
    main_m = metrics([m for _, m, _ in pairs])
    tp2_m = metrics([t for _, _, t in pairs])
    delta = round(tp2_m["total_pnl"] - main_m["total_pnl"], 2)
    main_strategy = (pairs[0][1].strategy_id or "") if pairs else ""
    tp2_strategy = (pairs[0][2].strategy_id or "") if pairs else ""

    sufficient = n >= min_comparable
    suggestion = None
    if sufficient:
        winner_share = max(main_wins_n, tp2_wins_n) / max(1, n)
        material = abs(delta) >= 5.0 and winner_share >= 0.60
        if not material:
            verdict_kind = "neutral"
            message = ("Sem diferença material no recorte comparável — manter "
                       "ambas as estratégias e continuar a recolher evidência.")
        elif delta > 0:
            verdict_kind = "tp2_first"
            message = (
                f"`nova_gestao` (lane tp2) supera `tp2_protect` (main) no recorte "
                f"comparável (+{delta:.2f} €, {tp2_wins_n}/{n} sinais). "
                "Manutenção considerada: validar com o gate antes de qualquer troca.")
            suggestion = "maintenance_tp2_first"
        else:
            verdict_kind = "main_first"
            message = (
                f"`tp2_protect` (main) supera `nova_gestao` (tp2) no recorte "
                f"comparável (+{abs(delta):.2f} € favoráveis, {main_wins_n}/{n}). "
                "Manutenção não recomendada — manter a configuração atual.")
            suggestion = "maintenance_main_first"
    else:
        verdict_kind = "insufficient"
        message = (f"Amostra insuficiente — {n}/{min_comparable} sinais comparáveis. "
                   "Sem sugestão de manutenção até a amostra acumular.")

    if n == 0 and voids:
        # Explica PORQUE é que a amostra comparável ainda está vazia: os
        # fechos phantom (ordens recusadas sem fill) contam como "sem dados",
        # não como perda/ganho de nenhuma estratégia.
        message = (
            f"Amostra insuficiente — {n}/{min_comparable} sinais comparáveis. "
            f"{len(voids)} par(es) do mesmo sinal ficaram fora por fecho "
            "phantom (ordens recusadas pelo broker sem fill, ex.: AutoTrading "
            "OFF no terminal 2 — a 01/09 a lane tp2 fechou 3 trades a 0.0 sem "
            "razão de saída). A amostra A/B começa a contar quando AMBAS as "
            "lanes fecharem sinais com ordens reais.")

    return {
        "ok": True,
        "min_comparable": min_comparable,
        "n": n,
        "sufficient": sufficient,
        "suggestion": suggestion,
        "delta_total": delta,
        "void_count": len(voids),
        "voids": voids[:50],
        "main": {"strategy": main_strategy, "voids": main_voids, **main_m},
        "tp2": {"strategy": tp2_strategy, "voids": tp2_voids, **tp2_m},
        "per_signal": {"main_wins": main_wins_n, "tp2_wins": tp2_wins_n,
                       "ties": ties_n},
        "verdict": {"kind": verdict_kind, "message": message},
        "signals": rows[:100],
    }
