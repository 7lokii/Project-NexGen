"""Gestão do grupo: TP/SL hits, close/protect, move SL, pending management.

Movido verbatim de app/state.py (continuação da fase de corte dos mixins);
os métodos vivem num Mixin misturado no BotState para continuarem
acessíveis como `self.*`.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.execution.engine import _now_iso
from app.messages.manage import symbol_in
from app.messages.summaries import parse_summary
from app.models import Signal, SignalIn, Trade


class ManagementMixin:
    """Mensagens de gestão do grupo (agem na hora, sem janela de edição):
    `_handle_management`, correlação de alvos, `pending_management`,
    resumos do grupo."""

    async def _handle_management(self, body: SignalIn, action: dict, signal_id: str,
                                 received_at: str) -> dict:
        """Act on a group management message (TP HIT, SL HIT, close first
        entries, out at BE). Never waits for the edit window.

        The message is recorded as a signal (status `management`) so the
        dashboard history shows what the group said, then the action is
        applied to the trade of the signal it REPLIES TO (`reply_to_msg_id`)
        — falling back to the open trade of the same symbol, then the most
        recent open trade. If the replied signal has no trade yet (still in
        the edit window), the instruction is queued and applied when that
        signal's trade opens.
        """
        kind = action["kind"]
        if self.config.get("execution_journal_enabled"):
            self.db.record_execution_event(
                received_at or _now_iso(), "management", engine="runtime",
                signal_id=signal_id,
                payload={
                    "kind": kind, "tp": action.get("tp"),
                    "sl": action.get("sl"), "be": bool(action.get("be")),
                    "message_id": body.message_id,
                    "reply_to_msg_id": body.reply_to_msg_id,
                    "raw_text": (body.raw_text or "")[:500],
                },
            )
        if kind == "summary_correction":
            return await self._handle_summary_correction(body, signal_id, received_at)
        if kind == "summary":
            return await self._handle_summary(body, signal_id, received_at)
        if kind == "recount":
            return await self._handle_recount(body, signal_id, received_at)

        # record the management message itself (kind -> status tag)
        mgmt_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol=symbol_in(body.raw_text) or "", direction="", entry=0.0,
            stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at,
            message_id=str(body.message_id) if body.message_id else None,
            reply_to_msg_id=str(body.reply_to_msg_id) if body.reply_to_msg_id else None,
            error=f"gestão do grupo: {kind}",
        )
        self.db.insert_signal(mgmt_signal)

        if kind == "tp_hit" and int(action.get("tp") or 0) == 5:
            target_signal = self._target_signal_for(body) or self._latest_group_signal(body)
            target_trade = self._target_trade(body, self._open_trades())
            target_id = target_signal.id if target_signal else (target_trade.signal_id if target_trade else "")
            has_tp5 = bool(target_signal and target_signal.take_profit_5) or bool(
                target_trade and target_trade.take_profit_5
            )
            self.db.record_group_audit(
                message_id=str(body.message_id or signal_id), source=body.source or "",
                signal_id=target_id, kind="tp5_reported",
                reported_level=(target_signal.take_profit_5 if target_signal and has_tp5 else None),
                inferred_tp=(target_signal.take_profit_5 if target_signal and has_tp5 else None),
                confidence="confirmed" if has_tp5 else "unmatched",
                raw_text=body.raw_text or "", received_at=received_at,
            )
            if not has_tp5 and not body.historical:
                await self.notifier.send(
                    "risk",
                    f"⚠️ O grupo reportou TP5, mas o sinal {target_signal.symbol if target_signal else ''} "
                    "não tinha TP5 estruturado. Trade não reaberta nem quinta perna criada; "
                    "evento guardado apenas para auditoria.",
                )

        if body.historical:
            # history reload: register the instruction but NEVER act on live
            # trades with an old message
            return {"action": "management", "kind": kind, "historical": True,
                    "applied": False, "signal": mgmt_signal.to_dict()}

        if kind == "go":
            # "Do it now team" — o admin confirma a entrada imediatamente: se
            # o sinal ainda estiver na janela de edição, finaliza já e executa
            # (caminho único auto). Sem sinal em espera, regista apenas —
            # nunca fecha nada nem dispara risco.
            target = self._target_signal_for(body)
            if not target or target.status != "awaiting_edit":
                awaiting = self.db.awaiting_edit_signals()
                if awaiting:
                    target = max(awaiting, key=lambda s: s.received_at or "")
                else:
                    target = None
            if (target and target.status == "awaiting_edit"
                    and not target.error and not target.llm_parsed):
                res = await self._try_execute(target)
                note = ("executado já" if res.get("action") == "executed"
                        else f"finalizado ({res.get('action', '?')})")
                await self.notifier.send(
                    "system",
                    f"⚡ GO do grupo: \"{body.raw_text.strip()[:60]}\" → sinal "
                    f"{target.symbol} {note} (janela de edição encurtada)",
                )
                return {"action": "management", "kind": kind, "applied": True,
                        "target_signal": target.id, "signal": mgmt_signal.to_dict()}
            return {"action": "management", "kind": kind, "applied": False,
                    "signal": mgmt_signal.to_dict()}

        trades = self._open_trades()
        # Shadow paper follows the same exact-reply/symbol fallback even when
        # another real trade is open: the real account and the shadow account
        # are independent targets, but both must receive the group event.
        shadow_trade = self._target_paper_trade(body)
        shadow_routed = False
        if shadow_trade:
            try:
                self.paper.apply_management(
                    shadow_trade["signal_id"], kind,
                    tp=action.get("tp"), sl=action.get("sl"),
                    be=bool(action.get("be")),
                    msg_id=str(body.message_id) if body.message_id else None,
                    origin="shadow",
                )
                shadow_routed = True
            except Exception as exc:
                print(f"[shadow-paper] falha ao aplicar gestão: {exc}")
        if not trades:
            # no real trade at all: a shadow trade may still receive the
            # instruction. If the real signal is still actionable, retain a
            # separate pending row so a later real approval also receives it.
            replied = self._target_signal_for(body)
            if (replied and replied.id != signal_id
                    and replied.status in ("awaiting_edit", "pending")):
                self._queue_management(body, action, signal_id, received_at, replied)
                if shadow_routed:
                    # The queued row belongs to the real approval path too,
                    # but the shadow stream has already handled this event.
                    self.db.mark_pending_management_applied(
                        signal_id, engine="shadow"
                    )
                await self.notifier.send(
                    "system",
                    f"📨 {body.source}: \"{body.raw_text.strip()[:80]}\" "
                    f"→ resposta ao sinal {replied.symbol} (ainda sem trade) "
                    f"— aplicada quando a trade abrir"
                    + ("; shadow-paper já recebeu" if shadow_routed else ""),
                )
                return {"action": "management", "kind": kind, "applied": False,
                        "shadow_applied": shadow_routed, "queued": True,
                        "target_signal": replied.id,
                        "signal": mgmt_signal.to_dict()}
            if shadow_routed:
                # The row is still useful for a later real approval, but the
                # shadow stream has already consumed its own delivery.
                self.db.mark_pending_management_applied(
                    signal_id, engine="shadow"
                )
                await self.notifier.send(
                    "system",
                    f"👻 Shadow-paper recebeu gestão: {body.raw_text.strip()[:80]}",
                )
                return {"action": "management", "kind": kind, "applied": False,
                        "shadow_applied": True, "signal": mgmt_signal.to_dict()}
            if replied is not None and replied.status in ("awaiting_edit", "pending"):
                # This branch is normally handled above; keep it explicit for
                # a future change that may skip the queue path.
                pass
            elif replied is not None:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Gestão do grupo sem alvo: \"{body.raw_text.strip()[:60]}\" "
                    f"responde ao sinal {replied.symbol} de "
                    f"{(replied.received_at or '')[:16].replace('T', ' ')} "
                    f"(estado: {replied.status}) que já não está aberto — "
                    f"instrução ignorada. Verifica se o sinal foi rejeitado/SL "
                    f"antes da gestão chegar.",
                )
            else:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Gestão do grupo sem alvo: \"{body.raw_text.strip()[:60]}\" "
                    f"sem trade aberta e sem sinal a que responder — ignorada. "
                    f"Confirma se o sinal correspondente chegou ao bot.",
                )
            await self.notifier.send(
                "system",
                f"📨 {body.source}: \"{body.raw_text.strip()[:80]}\" "
                f"(sem trade aberta — ignorado)",
            )
            return {"action": "management", "kind": kind, "applied": False,
                    "signal": mgmt_signal.to_dict()}

        # pick the target: (1) the trades whose signal this message replies
        # to, (2) same symbol, (3) most recent — ACROSS todas as lanes. Uma
        # instrução do grupo aplica-se às trades abertas de TODAS as lanes do
        # mesmo sinal, cada uma na sua engine/estratégia.
        target_trades = self._target_trades(body, trades)

        msg_id = str(body.message_id) if body.message_id else None
        notes = []
        if not target_trades:
            notes.append("sem trade alvo (ambíguo — usa reply para desambiguar)")
        for trade in target_trades:
            lane = getattr(trade, "lane_id", None) or "main"
            trade_notes = self._apply_management_to_trade(
                trade, kind, action, msg_id,
            )
            if lane != "main":
                trade_notes = [f"{lane}: {n}" for n in trade_notes]
            notes.extend(trade_notes)

        # corrida paralela: cada estratégia de teste aplica a gestão à MESMA
        # trade em papel SE a honrar (manage_kinds) — antes a corrida paper
        # ignorava a gestão e as contas "+ gestão" comportavam-se como as
        # sem gestão até o preço bater SL/TP.
        if target_trades:
            try:
                # The paper race and shadow paper are separate origins. Even
                # when both point at the same signal, each stream must receive
                # the event exactly once; filtering by origin prevents the
                # shadow delivery above from consuming/duplicating race delivery.
                self.paper.apply_management(
                    target_trades[0].signal_id, kind,
                    tp=action.get("tp"), sl=action.get("sl"),
                    be=bool(action.get("be")), msg_id=msg_id,
                    origin="race",
                )
            except Exception as exc:
                print(f"[paper] falha ao aplicar gestão: {exc}")

        text = (f"📨 {body.source}: {body.raw_text.strip()[:80]}")
        if notes:
            text += f" → {'; '.join(notes)}"
        await self.notifier.send("system", text)
        trade_ids = [t.id for t in target_trades]
        return {"action": "management", "kind": kind,
                "applied": bool(target_trades),
                "shadow_applied": shadow_routed,
                "trade_id": trade_ids[0] if trade_ids else None,
                "trade_ids": trade_ids,
                "notes": notes, "signal": mgmt_signal.to_dict()}

    def _apply_management_to_trade(self, trade: Trade, kind: str, action: dict,
                                   msg_id: Optional[str]) -> List[str]:
        """Aplica uma instrução de gestão a UMA trade, na sua lane/engine.

        A estratégia da lane decide se honra o kind (manage_kinds); a engine
        da lane executa os fechos/moves. Devolve as notas para o feed.
        """
        notes: List[str] = []
        engine = self._engine_for_trade(trade)
        strat_id = self._lane_strategy_id(getattr(trade, "lane_id", None) or "main")
        from app.strategies import get_strategy as _get_strat
        strat = _get_strat(strat_id)
        kinds = strat.get("manage_kinds") or set()
        if kind not in kinds:
            notes.append(f"não aplica a {strat_id}")
            return notes
        if kind == "tp_hit":
            tp = action.get("tp", 1)
            if tp == 1:
                moved = self._protect_remaining(trade)
                if moved:
                    notes.append("pernas restantes movidas a breakeven")
            notes.append(f"TP{tp} confirmado pelo grupo")
        elif kind == "sl_hit":
            notes.append("SL confirmado pelo grupo — a reconciliar com o broker")
        elif kind == "close_tp1":
            # "Close TP1 here" — partial profit only: leg 1 closes at market,
            # remaining legs keep their SL untouched.
            closed = engine.close_leg_at_market(trade.id, 1,
                                                reason="close_tp1", msg_id=msg_id)
            if closed:
                notes.append(f"perna 1 fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
            else:
                notes.append("perna 1 já fechada")
        elif kind == "close_tp":
            leg = int(action.get("tp", 2))
            closed = engine.close_leg_at_market(trade.id, leg,
                                                reason="close_tp", msg_id=msg_id)
            if closed:
                notes.append(f"perna {leg} fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
            else:
                notes.append(f"perna {leg} já fechada")
        elif kind == "protect":
            # A híbrida já tem escada/trailing mecânicos. O protect do grupo
            # passa por um merge monotónico: nunca recua o SL automático nem
            # fecha a perna 1 a um preço pior que a proteção já conhecida.
            if (strat_id == "auto_escada_3_hibrida"
                    and hasattr(engine, "apply_hybrid_protect")):
                buffer = self._be_buffer(trade)
                group_sl = (trade.entry + buffer
                            if trade.direction == "buy" else trade.entry - buffer)
                hybrid_events = engine.apply_hybrid_protect(
                    trade, group_sl=group_sl, now=_now_iso(), msg_id=msg_id,
                )
                closed_events = [e for e in hybrid_events
                                 if e.get("type") == "close"]
                if closed_events:
                    total = sum(e.get("pnl", 0.0) for e in closed_events)
                    notes.append(
                        f"protect híbrido: {len(closed_events)} perna(s) fechada(s) "
                        f"(P&L {total:+.2f})"
                    )
                notes.append("SL fundido com a proteção mecânica (nunca recua)")
            else:
                closed = engine.close_leg_at_market(trade.id, 1,
                                                    reason="protect", msg_id=msg_id)
                moved = self._protect_remaining(trade, above_entry=True)
                if closed:
                    notes.append(f"perna 1 fechada @ {closed['price']} (P&L {closed['pnl']:+.2f})")
                if moved:
                    notes.append("SL das restantes movido acima da melhor entrada")
        elif kind == "move_sl":
            new_sl = action.get("sl")
            if new_sl:
                moved = 0
                for p in self.db.positions_for_trade(trade.id):
                    if p.status != "open":
                        continue
                    if engine.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                        moved += 1
                notes.append(f"SL de {moved} pernas movido para {new_sl}" if moved
                             else "SL não movido (pernas já fechadas?)")
            else:
                notes.append("nudge sem nível — registado, sem ação")
        elif kind == "close_all":
            # "out at BE": remaining legs are already protected at breakeven
            # (the update loop lets the SL close them); "we're out": close
            # everything at market now.
            if action.get("be"):
                remaining = [p for p in self.db.positions_for_trade(trade.id)
                             if p.status == "open" and p.leg != 1]
                if remaining:
                    self._protect_remaining(trade)
                    notes.append(f"out a BE confirmado — {len(remaining)} pernas "
                                 f"protegidas na entrada (SL já em breakeven)")
                else:
                    notes.append("out a BE — sem pernas restantes")
            else:
                events = engine.close_trade_at_market(trade.id,
                                                      reason="close_all", msg_id=msg_id)
                if events:
                    pnl = sum(e.get("pnl", 0.0) for e in events if e.get("type") == "close")
                    notes.append(f"trade fechada ao mercado (P&L {pnl:+.2f})")
                else:
                    notes.append("trade já sem pernas abertas")
        return notes

    async def _handle_tp_sl_update(self, body: SignalIn, signal_id: str,
                                   received_at: str) -> Optional[dict]:
        """\"TP1 4734 SL 4745\" replying to a signal: the group confirms or
        corrects TP1 and/or the stop loss of the replied signal's trade.

        Recorded as a management-style signal (status `management`) and, if
        the target trade is open, the SL of every open leg is moved to the
        new level and the DB signal's TP1/SL are updated (ground truth).
        Returns None when the message is not of this shape or has no target.
        """
        m = re.match(
            r"^\s*TP\s*1\s+([\d.]+)\s*(?:SL|stop\s*loss)\s+([\d.]+)\s*$",
            (body.raw_text or "").strip(), re.IGNORECASE,
        )
        if not m:
            return None
        tp1 = float(m.group(1)); new_sl = float(m.group(2))
        target = self._target_signal_for(body)
        if not target:
            return None
        # registar a mensagem (auditoria) — nunca como sinal de entrada
        mgmt = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol=target.symbol or symbol_in(body.raw_text) or "",
            direction="", entry=0.0, stop_loss=0.0,
            take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at,
            message_id=str(body.message_id) if body.message_id else None,
            reply_to_msg_id=str(body.reply_to_msg_id) if body.reply_to_msg_id else None,
            error=f"gestão do grupo: tp_sl_update (TP1 {tp1} · SL {new_sl})",
        )
        self.db.insert_signal(mgmt)
        if body.historical:
            return {"action": "management", "kind": "tp_sl_update",
                    "historical": True, "applied": False,
                    "signal": mgmt.to_dict()}
        notes = []
        # atualiza o TP1/SL do sinal alvo (ground truth do grupo)
        if target.take_profit_1 != tp1 or target.stop_loss != new_sl:
            self.db.update_signal_levels(target.id, tp1=tp1, sl=new_sl)
            notes.append(f"sinal {target.symbol}: TP1→{tp1} · SL→{new_sl}")
        shadow_updated = 0
        if self.config.get("shadow_paper_enabled"):
            shadow_updated = self.paper.apply_tp_sl_update(
                target.id, tp1, new_sl, origin="shadow"
            )
            if shadow_updated:
                notes.append(f"SL/TP1 atualizado em {shadow_updated} trade(s) shadow")
        race_updated = self.paper.apply_tp_sl_update(
            target.id, tp1, new_sl, origin="race"
        )
        if race_updated:
            notes.append(f"SL/TP1 atualizado em {race_updated} trade(s) paper")
        # Move o SL em TODAS as lanes abertas do sinal alvo (cada uma na sua
        # engine) — a atualização TP1/SL do grupo é ground truth partilhada.
        target_trades = [t for t in self._open_trades() if t.signal_id == target.id]
        for trade in target_trades:
            engine = self._engine_for_trade(trade)
            lane = getattr(trade, "lane_id", None) or "main"
            moved = 0
            for p in self.db.positions_for_trade(trade.id):
                if p.status != "open":
                    continue
                if engine.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                    moved += 1
            if moved:
                prefix = f"{lane}: " if lane != "main" else ""
                notes.append(f"{prefix}SL de {moved} pernas movido para {new_sl}")
        text = f"📨 {body.source}: {body.raw_text.strip()[:60]}"
        if notes:
            text += f" → {'; '.join(notes)}"
        await self.notifier.send("system", text)
        return {"action": "management", "kind": "tp_sl_update", "applied": True,
                "shadow_applied": bool(shadow_updated),
                "paper_applied": bool(race_updated),
                "target_signal": target.id, "notes": notes,
                "signal": mgmt.to_dict()}

    # ------------------------------------------- reply-based correlation

    def _target_signal_for(self, body: SignalIn) -> Optional[Signal]:
        """The signal this management message replies to (Telegram
        reply_to_msg_id), if it exists in the DB — regardless of whether its
        trade is open yet."""
        if not body.reply_to_msg_id:
            return None
        group_key = (body.group or "").strip()
        target_id = (f"tg-{group_key}-{body.reply_to_msg_id}"
                     if group_key else f"tg-{body.reply_to_msg_id}")
        return self.db.get_signal(target_id)

    def _target_trade(self, body: SignalIn, trades: List[Trade]) -> Optional[Trade]:
        """Pick the trade a management message refers to:
        1. the open trade of the signal it replies to (exact correlation),
        2. an open trade of the named symbol,
        3. the most recent open trade."""
        replied = self._target_signal_for(body)
        if replied:
            for t in trades:
                if t.signal_id == replied.id:
                    return t
        sym = symbol_in(body.raw_text or "")
        candidates = [t for t in trades if not sym or t.symbol == sym]
        if not candidates:
            candidates = trades
        # A no-reply management message must never guess between two live
        # trades. The caller emits the existing risk notification when None
        # is returned; the operator can then provide a Telegram reply.
        signal_ids = {t.signal_id for t in candidates}
        if len(signal_ids) > 1:
            return None
        return max(candidates, key=lambda t: t.opened_at or "") if candidates else None

    def _target_trades(self, body: SignalIn, trades: List[Trade]) -> List[Trade]:
        """Todas as trades abertas (em todas as lanes) a que uma mensagem de
        gestão se refere: (1) as trades do sinal a que responde, (2) mesmo
        símbolo, (3) mais recentes. Devolve [] quando ambíguo (sem reply e
        com vários signal_ids) — o chamador emite o aviso existente."""
        replied = self._target_signal_for(body)
        if replied:
            matches = [t for t in trades if t.signal_id == replied.id]
            if matches:
                return matches
        sym = symbol_in(body.raw_text or "")
        candidates = [t for t in trades if not sym or t.symbol == sym]
        if not candidates:
            candidates = trades
        signal_ids = {t.signal_id for t in candidates}
        if len(signal_ids) > 1:
            return []
        return candidates

    def _target_paper_trade(self, body: SignalIn) -> Optional[dict]:
        """Find an open shadow trade using the same reply→symbol→recent
        fallback as the real-management path, without treating unrelated
        paper accounts as a target."""
        if not self.config.get("shadow_paper_enabled"):
            return None
        allowed = set(self.config.get("shadow_paper_strategies") or [])
        if not allowed:
            return None

        def _allowed(rows: list) -> list:
            return [t for t in rows
                    if t.get("strategy_id") in allowed
                    and (t.get("origin") or "race") == "shadow"]

        # Keep the exact reply as the first choice. If that signal has no
        # shadow trade (for example it was opened before shadow was enabled),
        # fall through exactly like `_target_trade`: named symbol, then the
        # most recent open shadow trade. Every query is origin-filtered so a
        # race trade can never become the shadow target by accident.
        replied = self._target_signal_for(body)
        candidates = (_allowed(self.db.paper_open_trades_by_signal(
            replied.id, origin="shadow"
        )) if replied else [])
        # A reply to a signal that is still actionable must be queued for that
        # signal, not redirected to an unrelated same-symbol shadow trade that
        # happens to be open. The real path has the same pending-target rule.
        if (replied and not candidates
                and replied.status in ("awaiting_edit", "pending")):
            return None
        symbol = symbol_in(body.raw_text or "") or ""
        if not candidates and symbol:
            candidates = _allowed(self.db.paper_open_trades_for_symbol(
                symbol, origin="shadow"
            ))
        if not candidates:
            candidates = _allowed([
                trade
                for sid in allowed
                for trade in self.db.paper_open_trades(sid, origin="shadow")
            ])
        if len({t.get("signal_id") for t in candidates}) > 1:
            return None
        return max(candidates, key=lambda t: t.get("opened_at") or "") \
            if candidates else None

    def _queue_management(self, body: SignalIn, action: dict, signal_id: str,
                          received_at: str, target: Signal) -> None:
        """Queue a management instruction whose target signal has no open
        trade yet; `_apply_pending_management` runs it when the trade opens."""
        self.db.insert_pending_management({
            "id": signal_id,
            "signal_id": signal_id,
            "reply_to_msg_id": str(target.message_id) if target.message_id else target.id,
            "kind": action["kind"],
            "tp": action.get("tp"),
            "sl": action.get("sl"),
            "be": bool(action.get("be")),
            "raw_text": body.raw_text or "",
            "source": body.source or "",
            "received_at": received_at or "",
            "created_at": _now_iso(),
        })

    async def _apply_pending_management(self, signal: Signal, trade: Trade) -> int:
        """Apply management instructions the group posted while `signal` was
        still buffered (queued in `pending_management`). Returns how many
        were applied."""
        key = str(signal.message_id) if signal.message_id else signal.id
        lane_id = getattr(trade, "lane_id", None) or "main"
        pending_engine = "tp2" if lane_id == "tp2" else "real"
        pending = self.db.pending_management_for(key, engine=pending_engine)
        engine = self._engine_for_trade(trade)
        lane_strategy = self._lane_strategy_id(lane_id)
        from app.strategies import get_strategy
        manage_kinds = get_strategy(lane_strategy).get("manage_kinds") or set()
        applied = 0
        for rec in pending:
            kind = rec["kind"]
            if kind not in manage_kinds:
                # The instruction is still consumed for this lane, but a
                # strategy such as tp2_protect must not honour close_all,
                # move_sl or other kinds outside its declared contract.
                self.db.mark_pending_management_applied(
                    rec["id"], engine=pending_engine,
                )
                continue
            if kind == "close_tp1":
                closed = engine.close_leg_at_market(trade.id, 1, reason="close_tp1")
                if closed:
                    applied += 1
            elif kind == "close_tp":
                leg = int(rec.get("tp") or 2)
                if engine.close_leg_at_market(trade.id, leg, reason="close_tp"):
                    applied += 1
            elif kind == "protect":
                if lane_strategy == "auto_escada_3_hibrida":
                    buffer = self._be_buffer(trade)
                    group_sl = (trade.entry + buffer
                                if trade.direction == "buy" else trade.entry - buffer)
                    engine.apply_hybrid_protect(
                        trade, group_sl=group_sl, now=_now_iso(),
                        msg_id=rec.get("id"),
                    )
                else:
                    engine.close_leg_at_market(trade.id, 1, reason="protect")
                    self._protect_remaining(trade, above_entry=True)
                applied += 1
            elif kind == "close_all":
                if rec.get("be"):
                    remaining = [p for p in self.db.positions_for_trade(trade.id)
                                 if p.status == "open" and p.leg != 1]
                    if remaining:
                        self._protect_remaining(trade)
                    applied += 1
                else:
                    engine.close_trade_at_market(trade.id, reason="close_all")
                    applied += 1
            elif kind == "tp_hit":
                if int(rec.get("tp") or 1) == 1:
                    if self._protect_remaining(trade):
                        applied += 1
            elif kind == "move_sl":
                new_sl = rec.get("sl")
                if new_sl:
                    for p in self.db.positions_for_trade(trade.id):
                        if p.status == "open":
                            engine.move_leg_sl(trade.id, p.leg, new_sl)
                    applied += 1
            elif kind == "sl_hit":
                # the broker SL already protects; nothing to do on open
                applied += 1
            # corrida/shadow paper: não reaplicar uma instrução que já foi
            # entregue ao shadow antes da aprovação real. O marcador é
            # separado do `applied` da conta principal.
            if not rec.get("paper_applied"):
                try:
                    self.paper.apply_management(
                        trade.signal_id, kind,
                        tp=rec.get("tp"), sl=rec.get("sl"),
                        be=bool(rec.get("be")), msg_id=rec.get("id"),
                        origin="race",
                    )
                    self.db.mark_pending_management_applied(
                        rec["id"], engine="paper"
                    )
                except Exception as exc:
                    print(f"[paper] falha ao aplicar gestão pendente: {exc}")
            self.db.mark_pending_management_applied(rec["id"], engine=pending_engine)
        return applied

    def _protect_remaining(self, trade, above_entry: bool = False) -> int:
        """Move the SL of every remaining open leg to breakeven (default) or
        just above the entry ("risk free", locks a small profit). Returns
        the number of legs moved."""
        moved = 0
        engine = self._engine_for_trade(trade)
        for p in self.db.positions_for_trade(trade.id):
            if p.leg == 1 or p.status != "open":
                continue
            new_sl = trade.entry
            if above_entry:
                # a small buffer above entry (buy) / below (sell): lock profit
                buffer = self._be_buffer(trade)
                new_sl = trade.entry + buffer if trade.direction == "buy" \
                    else trade.entry - buffer
            if engine.move_leg_sl(trade.id, p.leg, new_sl) is not None:
                moved += 1
        return moved

    @staticmethod
    def _be_buffer(trade) -> float:
        """Small profit-lock buffer above/below entry for the group's
        \"risk free\" step: ~10% of the entry-to-SL distance, at least one
        pip, capped so it never crosses TP1."""
        risk = abs(trade.entry - trade.stop_loss)
        if risk <= 0:
            return 0.0
        buffer = min(max(risk * 0.1, risk * 0.01), abs(trade.take_profit_1 - trade.entry))
        return round(buffer, 4)

    async def _handle_summary_correction(self, body: SignalIn, signal_id: str,
                                          received_at: str) -> dict:
        """Apply an R1 correction to exactly one group's summary."""
        fields = parse_summary(body.raw_text or "") or {}
        target_info = self.db.resolve_group_summary(
            source=body.source or "",
            reply_to_msg_id=str(body.reply_to_msg_id or ""),
            period_start=fields.get("period_start") or "",
            period_end=fields.get("period_end") or "",
        ) if fields else {"summary": None, "ambiguous": False}
        target = target_info.get("summary")
        reason = "correção de summary R1"
        if not fields:
            reason += ": números não reconhecidos"
        elif target_info.get("ambiguous"):
            reason += ": alvo ambíguo"
        elif not target:
            reason += ": summary alvo não encontrado"
        summary_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol="", direction="", entry=0.0, stop_loss=0.0,
            take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at, message_id=str(body.message_id or "") or None,
            reply_to_msg_id=str(body.reply_to_msg_id or "") or None,
            error=reason,
        )
        self.db.insert_signal(summary_signal)
        if not target:
            if not body.historical:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Correção de summary sem alvo inequívoco: "
                    f"{(body.raw_text or '').strip()[:100]}",
                )
            return {"action": "management", "kind": "summary_correction",
                    "parsed": bool(fields), "applied": False,
                    "ambiguous": bool(target_info.get("ambiguous")),
                    "signal": summary_signal.to_dict()}
        corrected = self.db.apply_group_summary_correction(
            target, str(body.message_id or ""), body.source or "", fields,
            body.raw_text or "", received_at or _now_iso(),
        )
        if not body.historical:
            await self.notifier.send(
                "summary",
                f"✏️ Summary corrigido: {fields['trades_sent']} trades · "
                f"WR {fields['win_rate']:.0f}% · {fields['winning']} win / "
                f"{fields['stop_loss']} SL / {fields['breakevens']} BE · "
                f"alvo {target.get('message_id') or target.get('id')}",
            )
            await self._check_group_divergence(body, fields)
        return {"action": "management", "kind": "summary_correction",
                "parsed": True, "applied": True,
                "target_summary_id": corrected.get("id"),
                "target_message_id": corrected.get("message_id"),
                "signal": summary_signal.to_dict()}

    async def _handle_summary(self, body: SignalIn, signal_id: str,
                              received_at: str) -> dict:
        """Parse and store the group's periodic summary (ground truth)."""
        fields = parse_summary(body.raw_text or "") or {}
        if fields:
            fields["raw_text"] = body.raw_text
            self.db.insert_group_summary(body.source, fields, received_at or _now_iso(),
                                         message_id=str(body.message_id or ""))
        # also record the raw message so the history stays complete
        summary_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol="", direction="", entry=0.0, stop_loss=0.0,
            take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at, error="resumo do grupo (ground truth)",
        )
        self.db.insert_signal(summary_signal)
        # Só resumos AO VIVO notificam o feed + divergência — os do replay
        # histórico (restart do listener) ficam guardados como ground truth
        # sem inundar a dashboard com notificações repetidas.
        if fields and fields.get("trades_sent") and not body.historical:
            await self.notifier.send(
                "summary",
                f"📊 {body.source}: {fields['trades_sent']} trades, "
                f"WR {fields['win_rate']:.0f}% (grupo) | "
                f"{fields['winning']} win / {fields['stop_loss']} SL / "
                f"{fields['breakevens']} BE | +{fields['pips']} pips",
            )
            await self._check_group_divergence(body, fields)
        return {"action": "management", "kind": "summary",
                "parsed": bool(fields), "signal": summary_signal.to_dict()}

