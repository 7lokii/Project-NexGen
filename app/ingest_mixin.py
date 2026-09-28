"""Signal ingestion: pipeline do sinal do grupo (window, revive, execução).

Movido verbatim de app/state.py (continuação da fase de corte dos mixins);
os métodos vivem num Mixin misturado no BotState para continuarem
acessíveis como `self.*`.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from app.execution.engine import _new_id, _now_iso
from app.formatting import _now_iso_plus
from app.formatting import _iso_age_seconds, _lots_text, _tps_text
from app.messages.manage import classify, symbol_in
from app.models import RiskDecision, Signal, SignalIn
from app.signals.parser import parse_or_error
from app.signals.llm_fallback import try_llm_parse
from app.strategies import strategy_lifecycle

# Mesmos regex do app/state.py (fonte única lá; aqui só a cópia usada pelo
# mixin — manter em sincronia é verificado pelos testes de reconhecimento).
_LOG_IGNORED = int(os.getenv("SIGNALBOT_LOG_IGNORED", "1") or "1")

# Keywords de direção ("I'm selling XAUUSD") casam sozinhas — o grupo envia
# a direção primeiro e edita com os detalhes. Já os detalhes (TP/SL/entry/...)
# só contam como sinal quando trazem um NÚMERO a seguir: "TP1 4396" é sinal,
# mas "after TP1" / "as SL" (conversa do grupo) não.
_SIGNAL_HINT = re.compile(
    r"\b(buying|selling|comprando|vendendo|buy|sell|compr|vend|long|short)\b"
    r"|\b(entr(?:ada)?|entry|stop\s*loss|stop|sl|tp\s*[1-5]|tp[1-5]|alvo|target)\b"
    r"\s*[0-9]",
    re.IGNORECASE,
)

# Passive group narrative. These patterns deliberately do not classify a
# message as an executable management instruction: progress/levels are
# evidence for audit only.
_PROGRESS_PIPS = re.compile(r"\+\s*(\d+(?:[.,]\d+)?)\s*pips?\b", re.IGNORECASE)
_LEVEL_MENTION = re.compile(
    r"\b(?:say\s+hi\s+to|hello\s+to|target(?:ing)?|heading\s+to|at)\s+"
    r"(\d+(?:[.,]\d+)?)\b", re.IGNORECASE,
)


class IngestMixin:
    """Pipeline de ingestão de sinais: ingest, janela de edição, revive,
    execução (`_try_execute`), confirm/reject/delete."""

    async def ingest(self, body: SignalIn) -> dict:
        """Receive a signal and route it through the pipeline.

        Telegram messages (which carry a `message_id`) are buffered in an
        edit window: they only continue once the window closes without edits.
        Everything else (demo button, structured input) continues immediately.
        """
        # Historical messages carry their original Telegram timestamp, so the
        # backtest replays them at the right moment instead of at load time.
        received_at = body.received_at or _now_iso()
        window = float(self.config.get("edit_window_seconds", 10))

        if body.message_id:
            return await self._ingest_telegram(body, received_at, window)

        return await self._ingest_immediate(body, received_at)

    def _latest_group_signal(self, body: SignalIn) -> Optional[Signal]:
        """Best observational target for a progress message.

        Reply correlation wins; without a reply use the latest complete signal
        for the named symbol (or latest complete signal). This is only for
        audit metadata and never routes an order.
        """
        replied = self._target_signal_for(body)
        if replied:
            return replied
        sym = symbol_in(body.raw_text or "")
        for sig in self.db.list_signals(100):
            if sig.direction not in ("buy", "sell") or not sig.entry:
                continue
            if sym and sig.symbol != sym:
                continue
            if sig.status in ("management", "historical", "failed", "ignored"):
                continue
            return sig
        return None

    async def _audit_group_progress(self, body: SignalIn, message_id: str,
                                    received_at: str) -> bool:
        """Store `+N pips`/level narrative as passive evidence.

        A pips-derived level is explicitly labelled as an inference. A level
        such as ``Let's say hi to 4600`` becomes a candidate TP5 only when the
        target signal has no TP5; it is never added to the signal or trade.
        """
        text = body.raw_text or ""
        pips_match = _PROGRESS_PIPS.search(text)
        level_match = _LEVEL_MENTION.search(text)
        if not pips_match and not level_match:
            return False
        target = self._latest_group_signal(body)
        pips = float(pips_match.group(1).replace(",", ".")) if pips_match else None
        level = float(level_match.group(1).replace(",", ".")) if level_match else None
        inferred = None
        confidence = "low"
        if target and pips is not None:
            pip_size = (float(self.config.get("group_gold_pip_size", 0.1) or 0.1)
                        if target.symbol == "XAUUSD" else 0.0001)
            sign = 1.0 if target.direction == "buy" else -1.0
            inferred = target.entry + sign * pips * pip_size
        if level is not None:
            confidence = "medium" if target else "unlinked"
            if target and target.take_profit_5:
                confidence = "confirmed_match" if abs(target.take_profit_5 - level) < 1e-6 else "medium"
            inferred = level
        quote = self._current_execution_quote(target.symbol) if target else None
        feed_price = None
        feed_delta = None
        divergence = False
        if quote:
            feed_price = float((quote["bid"] + quote["ask"]) / 2.0)
            if level is not None:
                feed_delta = abs(feed_price - level)
                divergence = feed_delta >= float(
                    self.config.get("group_feed_divergence_threshold", 20.0) or 20.0
                )
        common = {
            "source": body.source or "",
            "signal_id": target.id if target else "",
            "reported_pips": pips,
            "reported_level": level,
            "inferred_tp": inferred,
            "confidence": confidence,
            "feed_price": feed_price,
            "feed_delta": feed_delta,
            "feed_divergence": divergence,
            "raw_text": text,
            "received_at": received_at,
        }
        if pips_match:
            self.db.record_group_audit(message_id=str(message_id), kind="progress_pips", **common)
        if level_match:
            self.db.record_group_audit(message_id=str(message_id), kind="level_mention", **common)
        if not body.historical and level is not None and target and not target.take_profit_5:
            await self.notifier.send(
                "system",
                f"🧭 Possível TP5 reportado pelo grupo: {target.symbol} @ {level:g} "
                f"(confiança {confidence}; não foi adicionado ao sinal nem à trade)."
                + (f" ⚠️ Divergência feed MT5 {feed_price:.2f} vs grupo {level:g}." if divergence else ""),
            )
        elif not body.historical and divergence:
            await self.notifier.send(
                "risk",
                f"⚠️ Divergência de feed: grupo mencionou {level:g}, "
                f"mas MT5 estava em {feed_price:.2f} ({target.symbol if target else 'símbolo desconhecido'}). "
                f"Evento apenas auditado; não usado para execução.",
            )
        return True

    async def _ingest_telegram(self, body: SignalIn, received_at: str, window: float) -> dict:
        """Buffer a Telegram message and wait for possible edits.

        The signal id is `tg-<group>-<message_id>` so that the same Telegram
        message id from different groups never collides, and edits from the
        same group hit the same row.

        Group MANAGEMENT messages ("TP1 HIT", "close first entries",
        "we're out at BE") never wait for the edit window — they act on the
        open trade immediately (see `_handle_management`).
        """
        group_key = (body.group or "").strip()
        signal_id = f"tg-{group_key}-{body.message_id}" if group_key else f"tg-{body.message_id}"
        existing = self.db.get_signal(signal_id)
        if (existing and existing.status == "awaiting_edit"
                and not body.historical):
            return await self._apply_edit(existing, body, window)

        # A failed/rejected parse is still the same Telegram signal while it
        # is fresh. Try the new full message BEFORE management/conversation
        # filtering: a valid completion must be able to revive even when its
        # text has no standalone keyword that the conservative classifier
        # recognizes. A REJECTED signal (entry guard, min-lot risk, global
        # risk) reopens its edit window when the group re-edits with valid
        # content within `rejected_revive_minutes` — the rejection was about
        # the OLD content/price, and the edit is fresh group intent; the
        # alert states explicitly that the previous entry was refused.
        if (existing and not body.historical
                and (body.raw_text or "").strip() != (existing.raw_text or "").strip()):
            age = _iso_age_seconds(existing.received_at)
            if existing.status == "failed":
                if age is not None and 0 <= age <= 300:
                    res = await self._revive_signal(
                        existing, body, window, 300,
                        f"🔄 Sinal {existing.symbol or ''} recuperado — o grupo "
                        f"completou a edição (janela de {int(window)}s reaberta)",
                    )
                    if res:
                        return res
            elif existing.status == "rejected":
                revive_minutes = float(self.config.get("rejected_revive_minutes") or 10)
                if revive_minutes > 0:
                    revive_window = max(60.0, revive_minutes * 60.0)
                    if age is not None and 0 <= age <= revive_window:
                        reason = (existing.error or "").strip()[:160] or "recusado pelo operador"
                        res = await self._revive_signal(
                            existing, body, window, revive_window,
                            f"🔄 Sinal {existing.symbol or ''} reaberto — o grupo voltou "
                            f"a editar a mensagem com conteúdo válido. ⚠️ A entrada "
                            f"ANTERIOR foi recusada ({reason}). Janela de "
                            f"{int(window)}s reaberta.",
                        )
                        if res:
                            return res
                    elif age is not None and age > revive_window:
                        # A janela de revive expirou, mas o grupo voltou a mexer
                        # na mensagem — se o conteúdo novo for um sinal válido
                        # completo, o operador precisa de saber que ficou ignorado
                        # (não se perseguem entradas recusadas há >X min). Só
                        # alerta quando o re-edit parseia (não em edições parciais).
                        recheck = await self._build_signal(body, existing.id, existing.received_at)
                        if not recheck.error and not recheck.llm_parsed:
                            await self.notifier.send(
                                "system",
                                f"🗓️ O grupo voltou a editar o sinal "
                                f"{existing.symbol or ''} rejeitado, mas a janela de "
                                f"revive ({int(revive_window / 60)} min) já expirou — "
                                f"a recusa anterior mantém-se; o conteúdo novo foi "
                                f"ignorado (janela fechada há demasiado tempo).",
                            )

        # Telegram retries/reconnects can deliver the same live management
        # message more than once. Its signal id is the idempotency key; do not
        # reapply the action or emit a second notification. Historical replay
        # remains observational and is intentionally allowed through.
        if (existing and not body.historical
                and existing.status in ("management", "historical")
                and existing.message_id == str(body.message_id)):
            return {"action": "duplicate", "message_id": str(body.message_id)}

        # Management messages ("TP1 HIT", "we're out", "Weekly summary") act
        # immediately — classify BEFORE the final-state dedup so re-processing
        # a historical message still routes to the management handler.
        if body.raw_text:
            action = classify(body.raw_text)
            if action:
                return await self._handle_management(body, action, signal_id, received_at)
            audited = await self._audit_group_progress(body, signal_id, received_at)
            if audited:
                return {"action": "group_audit", "signal_id": signal_id,
                        "message_id": str(body.message_id)}
            # "TP1 4734 SL 4745" a responder ao sinal — o grupo confirma/corrige
            # o TP1/SL da trade do sinal alvo (edição pós-execução).
            edited = await self._handle_tp_sl_update(body, signal_id, received_at)
            if edited:
                return edited
            # Conversa do grupo (sem keywords de sinal nem gestão): audita e
            # ignora — nunca entra na pipeline nem na janela de edição.
            if not _SIGNAL_HINT.search(body.raw_text):
                self.db.record_ignored(
                    body.message_id, body.source or "", body.group or "",
                    body.raw_text,
                    "conversa do grupo (sem keywords de sinal)",
                    received_at,
                )
                if _LOG_IGNORED:
                    preview = body.raw_text.replace("\n", " ").strip()
                    if _LOG_IGNORED >= 2:
                        print(f"[ignored] {body.source} msg {body.message_id} | {body.raw_text}")
                    else:
                        print(f"[ignored] {body.source} msg {body.message_id} | {preview[:80]}")
                return {"action": "ignored", "reason": "conversa do grupo (audit)"}

        if existing:
            # O grupo editou a mensagem DEPOIS de a janela fechar e o sinal
            # já ter executado — a entrada/trade pode estar desatualizada.
            # Alerta de risco para detetar entradas erradas (janelas curtas).
            if (not body.historical and existing.status in ("executed", "pending")
                    and (body.raw_text or "").strip() != (existing.raw_text or "").strip()):
                # Regista a edição pós-execução (Item 12): alimenta a métrica
                # contínua de /api/edits — edições a sinais que já executaram
                # deviam ser raras; a taxa >5% dispara alerta no tick.
                self.db.set_signal_edited_at(existing.id, _now_iso())
                await self.notifier.send(
                    "risk",
                    f"⚠️ O grupo editou o sinal {existing.symbol or ''} depois de "
                    f"{('executar' if existing.status == 'executed' else 'ficar pendente')} "
                    f"(janela de {int(window)}s já passou) — a entrada pode estar "
                    f"desatualizada: \"{(body.raw_text or '').strip()[:70]}\"",
                )
            return {"action": "ignored", "reason": f"sinal já finalizado ({existing.status})"}

        signal = await self._build_signal(body, signal_id, received_at)
        signal.reply_to_msg_id = str(body.reply_to_msg_id) if body.reply_to_msg_id else None
        if body.historical:
            # past messages from the group's history: register only, never
            # wait for edits or execute
            signal.status = "historical" if not signal.error else "failed"
            signal.message_id = str(body.message_id)
            self.db.insert_signal(signal)
            return {"action": "historical", "signal": signal.to_dict()}

        signal.message_id = str(body.message_id)
        # se a primeira versão da mensagem tinha sido auditada como conversa
        # (ex.: "I'm selling XAUUSD" sem TP/SL ainda) e agora é sinal, remove
        # o registo de ignorada para a auditoria não contradizer os sinais.
        self.db.delete_ignored(str(body.message_id), body.source or "")
        # Parse completo → executa já (o TP1 do grupo pode bater em segundos).
        # A janela de edição só é usada quando a primeira versão está
        # incompleta (parse com erro) ou veio da IA (llm_parsed → registo
        # apenas, nunca executa — Item 12).
        if not signal.error and not signal.llm_parsed:
            self.db.insert_signal(signal)
            await self._open_shadow_for_signal(signal)
            return await self._continue_complete(signal)

        signal.status = "awaiting_edit"
        signal.edit_deadline = _now_iso_plus(window)
        self.db.insert_signal(signal)
        return {"action": "buffered", "edit_window_seconds": window, "signal": signal.to_dict()}

    async def _revive_signal(self, existing: Signal, body: SignalIn, window: float,
                             revive_window_seconds: float, alert_text: str) -> Optional[dict]:
        """Re-open the edit window of a previously failed/rejected signal when
        the group re-edits the same Telegram message with valid content within
        `revive_window_seconds` of the original arrival.

        Returns the action dict when the revive applied; None when the edit is
        not eligible (too old, still invalid, or only LLM-parsed — which is
        registered for audit but never re-opens the window). Shared by the
        failed-signal revive (5 min) and the rejected-signal revive
        (`rejected_revive_minutes`).
        """
        age = _iso_age_seconds(existing.received_at)
        if age is None or not (0 <= age <= revive_window_seconds):
            return None
        revived = await self._build_signal(body, existing.id, existing.received_at)
        if revived.error:
            return None
        if revived.llm_parsed:
            # Item 12: um revive só com parse da IA não reabre a janela — fica
            # registado/alertado, nunca executa.
            revived.status = "failed"
            revived.message_id = existing.message_id or str(body.message_id)
            self.db.insert_signal(revived)
            self.db.set_signal_status(
                revived.id, "failed",
                error="parseado por IA (fallback LLM) — registo apenas; "
                      "nunca executa automaticamente",
            )
            await self.notifier.send(
                "system",
                f"🤖 Sinal {revived.symbol} {revived.direction} parseado por IA "
                f"— guardado apenas para auditoria (nunca executa sem o "
                f"parser regex)",
            )
            return {"action": "llm_registered", "llm_parsed": True,
                    "signal": revived.to_dict()}
        revived.status = "awaiting_edit"
        revived.message_id = existing.message_id or str(body.message_id)
        revived.reply_to_msg_id = (
            str(body.reply_to_msg_id) if body.reply_to_msg_id
            else existing.reply_to_msg_id
        )
        revived.edit_deadline = _now_iso_plus(window)
        revived.edited_at = _now_iso()
        revived.error = None
        # marcador durável do revive: de que estado o sinal veio e o motivo
        # gravado da recusa/erro ANTERIOR — sobrevive à execução/rejeição
        # seguinte e alimenta o histórico (badge "🔄 de rejected").
        revived.revived_from = existing.status
        revived.revived_reason = (existing.error or "").strip()[:300] or None
        revived.revived_at = _now_iso()
        self.db.insert_signal(revived)
        self.db.delete_ignored(str(body.message_id), body.source or "")
        await self.notifier.send("revived", alert_text)
        return {"action": "revived", "signal": revived.to_dict(),
                "revived_from": existing.status}

    async def _continue_complete(self, signal: Signal) -> dict:
        """Roteia um sinal com parse completo para a pipeline — executa já,
        sem esperar a janela de edição. Só existe o caminho auto: o modo
        manual foi removido no Item 12."""
        return await self._try_execute(signal)

    async def _apply_edit(self, existing: Signal, body: SignalIn, window: float) -> dict:
        """Reparse an edit while retaining partial signal identity."""
        signal = await self._build_signal(body, existing.id, existing.received_at)
        if signal.error:
            signal.symbol = signal.symbol or existing.symbol
            signal.direction = signal.direction or existing.direction
            if signal.entry_zone_low is None and signal.entry_zone_high is None:
                signal.entry_zone_low = existing.entry_zone_low
                signal.entry_zone_high = existing.entry_zone_high
            if signal.entry == existing.entry and existing.entry:
                signal.entry = existing.entry
        signal.status = "awaiting_edit"
        signal.message_id = existing.message_id
        signal.edit_deadline = _now_iso_plus(window)
        signal.edited_at = _now_iso()
        # preserva o marcador de revive se o sinal reaberto for editado outra
        # vez dentro da janela — o histórico não perde a origem do reaberto.
        signal.revived_from = signal.revived_from or existing.revived_from
        signal.revived_reason = signal.revived_reason or existing.revived_reason
        signal.revived_at = signal.revived_at or existing.revived_at
        self.db.insert_signal(signal)
        if signal.error:
            return {"action": "edited_invalid", "error": signal.error, "signal": signal.to_dict()}
        return {"action": "edited", "signal": signal.to_dict()}

    async def _ingest_immediate(self, body: SignalIn, received_at: str) -> dict:
        """Messages without a message_id (demo button, structured input)."""
        signal_id = "s-" + _new_id()
        signal = await self._build_signal(body, signal_id, received_at)
        if body.historical:
            # Immediate/history API calls have no Telegram message_id to route
            # through `_ingest_telegram`; keep them observational as well.
            signal.status = "historical" if not signal.error else "failed"
            self.db.insert_signal(signal)
            return {"action": "historical", "signal": signal.to_dict()}
        if signal.error:
            signal.status = "failed"
            self.db.insert_signal(signal)
            await self.notifier.send("risk", f"⚠️ Sinal não reconhecido: {signal.error}")
            return {"action": "parse_error", "error": signal.error, "signal": signal.to_dict()}
        self.db.insert_signal(signal)
        if signal.llm_parsed:
            # Fallback LLM (Item 12): sem modo manual, um sinal parseado por
            # IA fica SÓ registado/alertado — nunca executa nem fica pendente.
            self.db.set_signal_status(
                signal.id, "failed",
                error="parseado por IA (fallback LLM) — registo apenas; "
                      "nunca executa automaticamente",
            )
            await self.notifier.send(
                "system",
                f"🤖 Sinal {signal.symbol} {signal.direction} parseado por IA "
                f"— guardado apenas para auditoria (nunca executa sem o "
                f"parser regex)",
            )
            return {"action": "llm_registered", "llm_parsed": True,
                    "signal": signal.to_dict()}
        await self._open_shadow_for_signal(signal)
        return await self._try_execute(signal)

    async def _build_signal(self, body: SignalIn, signal_id: str, received_at: str) -> Signal:
        """Build a Signal from an API body.

        Failed parses carry an `error` instead of raising, so the edit window
        can wait for the group to fix the message. When the regex parser
        fails and an LLM endpoint is configured, `try_llm_parse` offers a
        second chance — flagged `llm_parsed`, which keeps it registration-
        only (audit + alert; never executed since Item 12 removed manual
        approval).
        """
        if body.raw_text:
            signal = parse_or_error(body.raw_text, source=body.source,
                                    signal_id=signal_id, received_at=received_at)
            if signal.error:
                from app.signals.llm_fallback import llm_configured
                if llm_configured():
                    llm = await try_llm_parse(body.raw_text, source=body.source,
                                              signal_id=signal_id, received_at=received_at)
                    if llm is not None and not llm.error:
                        return llm
            return signal
        signal = Signal(
            id=signal_id, source=body.source, raw_text="(estruturado)",
            symbol=(body.symbol or "").upper().replace("/", ""),
            direction=body.direction.value if body.direction else "",
            entry=body.entry or 0.0,            stop_loss=body.stop_loss or 0.0,
            take_profit_1=body.take_profit_1 or 0.0,
            take_profit_2=body.take_profit_2 or 0.0,
            take_profit_3=body.take_profit_3 or 0.0,
            take_profit_4=body.take_profit_4 or 0.0,
            take_profit_5=body.take_profit_5 or 0.0,
            status="pending", received_at=received_at, lots=body.lots,
            entry_zone_low=body.entry_zone_low,
            entry_zone_high=body.entry_zone_high,
        )
        if not (signal.symbol and signal.direction and signal.entry
                and signal.stop_loss and signal.take_profit_1 and signal.take_profit_2):
            signal.status = "failed"
            signal.error = "campos estruturados incompletos"
        return signal

    async def finalize_awaiting_signals(self, now: Optional[str] = None) -> None:
        """Continue any signal whose edit window has closed.

        Valid signals go straight to the risk checks + execution (the only
        path since Item 12 removed the manual mode). Invalid ones are marked
        failed; LLM-parsed ones are registered for audit only — never
        executed.
        """
        now = now or _now_iso()
        for sig in self.db.awaiting_edit_signals():
            if not sig.edit_deadline or sig.edit_deadline > now:
                continue
            if sig.error:
                self.db.set_signal_status(sig.id, "failed")
                await self.notifier.send(
                    "risk",
                    f"⚠️ Sinal {sig.symbol or '(desconhecido)'} inválido após a "
                    f"janela de edição: {sig.error}",
                )
                continue
            if sig.llm_parsed:
                # Fallback LLM (Item 12): registo apenas — nunca executa.
                self.db.set_signal_status(
                    sig.id, "failed",
                    error="parseado por IA (fallback LLM) — registo apenas; "
                          "nunca executa automaticamente",
                )
                await self.notifier.send(
                    "system",
                    f"🤖 Sinal {sig.symbol} {sig.direction} parseado por IA "
                    f"— guardado apenas para auditoria (nunca executa sem o "
                    f"parser regex)",
                )
                continue
            await self._open_shadow_for_signal(sig)
            await self._try_execute(sig)

    async def _try_execute(self, signal: Signal, confirm_override: bool = False,
                           override_reason: str = "", override_kind: str = "") -> dict:
        # Shared entry point: window-finalized signals, GO messages and the
        # demo path all land here. Normal live paths already opened shadow
        # paper earlier; the DB dedup keeps this safe if both paths race
        # `confirm_override` bypasses ONLY the entry-zone and min-lot gates
        # after an explicit operator confirmation; it never bypasses the
        # global risk switches (kill switch, daily loss, max positions).
        # Choke-point do Item 12: um sinal parseado pela IA nunca executa,
        # mesmo que chegue aqui por GO do grupo, revive ou finalização —
        # fica registado para auditoria (registo + alerta, nunca trade).
        if signal.llm_parsed:
            self.db.set_signal_status(
                signal.id, "failed",
                error="parseado por IA (fallback LLM) — registo apenas; "
                      "nunca executa automaticamente",
            )
            await self.notifier.send(
                "system",
                f"🤖 Sinal {signal.symbol} {signal.direction} parseado por IA "
                f"— guardado apenas para auditoria (nunca executa sem o "
                f"parser regex)",
            )
            return {"action": "llm_registered", "llm_parsed": True,
                    "signal": signal.to_dict()}
        await self._open_shadow_for_signal(signal)
        # Global kill/auto-kill remain global. Position count, daily P&L and
        # account balance are checked separately for each execution lane.
        global_decision: RiskDecision = self.risk.check(
            signal, self.config, [], 0.0, self._balance()
        )
        if not global_decision.allowed:
            decision = global_decision
        else:
            decision = None
        if decision is not None and not decision.allowed:
            signal.status = "rejected"
            self.db.set_signal_status(signal.id, "rejected", error="; ".join(decision.reasons))
            await self.notifier.send(
                "risk",
                f"⛔ Sinal {signal.symbol} bloqueado: {'; '.join(decision.reasons)}",
            )
            return {"action": "blocked", "reasons": decision.reasons, "signal": signal.to_dict()}

        # A zona é validada contra cada conta. Em instalações sem segundo
        # provider, a lane 2 usa o mesmo preço de mercado em dry-run.
        risk_pct = self._effective_risk_pct()  # fallback/telemetria da conta principal

        zone_deviation = False
        if not confirm_override:
            # Tolerated entries used to open with a warning. They now wait for
            # explicit confirmation too: an entry outside the declared zone
            # (even within the tolerance) is never auto-executed silently.
            guards = {lane: self._entry_guard(signal, lane) for lane in self._lanes()}
            blocked_guards = {
                lane: guard for lane, guard in guards.items()
                if guard["status"] == "blocked"
                or (guard["status"] == "tolerated" and not guard.get("auto_execute"))
            }
            if blocked_guards and len(blocked_guards) == len(guards):
                # Modo autónomo: nenhum sinal fica pendente de humano.
                # Desvios até ao cap autónomo executam a mercado com marca
                # `zone_deviation` + notificação; acima dele, rejeita.
                worst = max(blocked_guards.values(),
                            key=lambda g: float(g.get("distance") or 0.0))
                width = float(worst.get("width") or 0.0)
                limit = float(worst.get("max_distance") or 0.0)
                cap_widths = float(self.config.get("entry_guard_auto_cap") or 0.0)
                auto_cap = limit if cap_widths <= 0 else width * cap_widths
                distance = float(worst.get("distance") or 0.0)
                if auto_cap > 0 and distance <= auto_cap:
                    zone_deviation = True
                    await self.notifier.send(
                        "system",
                        f"🎯 Sinal {signal.symbol}: preço {distance:.2f} fora da "
                        f"zona (≤ cap autónomo {auto_cap:.2f}) — executa com "
                        "marca zone_deviation",
                    )
                else:
                    error = (f"preço fora da zona além do cap autónomo "
                             f"({worst['reason']})")
                    self.db.set_signal_status(signal.id, "rejected", error=error)
                    signal.status = "rejected"
                    signal.error = error
                    cap_lbl = (f"{cap_widths:.2f}× largura"
                               if cap_widths > 0
                               else f"limite {worst.get('max_distance') or auto_cap:.2f}")
                    await self.notifier.send(
                        "risk",
                        f"⛔ Sinal {signal.symbol} rejeitado pelo cap autónomo: "
                        f"preço {distance:.2f} pips fora da zona (cap "
                        f"{auto_cap:.2f} pips · {cap_lbl}) — perseguição além "
                        f"do limite, não executada.",
                    )
                    return {"action": "zone_rejected", "guards": guards,
                            "signal": signal.to_dict()}

            # Minimum lot dominates the configured risk: the smallest tradable
            # size already exceeds the risk budget. Block auto entries so the
            # operator sees the real exposure before approving.
            # The minimum-lot gate is evaluated below per lane, because the
            # two strategies intentionally use different numbers of legs.

        # Abre a trade em CADA lane de execução (conta principal + segunda
        # demo paralela), cada uma com a sua estratégia e engine próprias.
        # O sinal só fica "executed" se PELO MENOS uma lane abrir; uma lane
        # que falhe não bloqueia as outras.
        opened_trades = []
        lane_notes = []
        lane_risk_gates = {}
        for lane_id in self._lanes():
            strat = self._lane_strategy(lane_id)
            engine = self._lane_engine(lane_id)
            lane_balance = self._balance_for_lane(lane_id)
            lane_risk_pct = self._effective_risk_pct_for_lane(lane_id)
            lane_risk_gate = self._min_lot_risk_gate(
                signal, lane_balance, lane_risk_pct, strat.get("max_legs")
            )
            lane_risk_gates[lane_id] = lane_risk_gate
            lane_guard = self._entry_guard(signal, lane_id)
            # O guard é restritivo apenas para `blocked` e para `tolerated`
            # sem auto_execute; `not_applicable` (sem quote/zona), `disabled`
            # e `in_zone` passam sempre.
            lane_zone_ok = (lane_guard["status"] == "in_zone"
                            or lane_guard["status"] in ("not_applicable", "disabled")
                            or bool(lane_guard.get("auto_execute")))
            if zone_deviation and not lane_zone_ok:
                # desvio dentro do cap autónomo: a lane executa mesmo fora
                # da zona, mas a trade fica marcada para auditoria
                lane_guard["status"] = "zone_deviation"
                lane_zone_ok = True
            if not confirm_override and not lane_zone_ok:
                lane_notes.append(f"{lane_id}: confirmação de zona necessária ({lane_guard['reason']})")
                continue
            lane_open = [t for t in self._open_trades()
                         if (getattr(t, "lane_id", None) or "main") == lane_id]
            lane_decision = self.risk.check(
                signal, self.config, lane_open,
                self._realized_today(lane_id), lane_balance,
            )
            if not lane_decision.allowed:
                lane_notes.append(f"{lane_id}: {'; '.join(lane_decision.reasons)}")
                continue
            max_legs_fit = None
            if lane_risk_gate["exceeded"] and not confirm_override:
                # Modo autónomo: em vez de rejeitar, tenta reduzir o número
                # de pernas até o risco caber no orçamento configurado.
                fit = self._fit_legs_to_risk(
                    signal, lane_balance, lane_risk_pct,
                    strat.get("max_legs"), lane_risk_gate,
                )
                if fit >= 1:
                    max_legs_fit = fit
                    lane_notes.append(
                        f"{lane_id}: lote mínimo domina — executa com "
                        f"{fit} perna(s) (auto-fit, risco "
                        f"{lane_risk_gate['risk_amount']:.2f} €)"
                    )
                else:
                    lane_notes.append(
                        f"{lane_id}: lote mínimo não cabe nem com 1 perna "
                        f"({lane_risk_gate['max_loss_est']:.2f} > "
                        f"{lane_risk_gate['risk_amount']:.2f})"
                    )
                    continue
            trade = self._open_trade_on_lane(

                signal, lane_risk_pct, strat, engine, lane_id, lane_guard,
                confirm_override, override_kind,
                max_legs_override=max_legs_fit,
            )
            if trade is None:
                lane_notes.append(f"{lane_id}: exceção ao abrir")
                continue
            if trade.status != "open":
                # broker refused the orders (e.g. AutoTrading off)
                error = trade.error or "ordem recusada pelo broker"
                if getattr(trade, "auto_trading_off", None):
                    self.db.set_config("last_auto_trading_off_at", _now_iso())
                    await self.notifier.send(
                        "risk",
                        f"⛔ AutoTrading DESLIGADO no MT5 ({lane_id}): sinal "
                        f"{signal.symbol} NÃO executado (retcode 10027).",
                    )
                elif not (trade.exit_reason or ""):
                    # fecho phantom: ordens recusadas SEM fill real — a trade
                    # fechou a 0.0 sem razão de saída (cleanup do envio). Não
                    # pode passar despercebido no feed: também não conta para
                    # o A/B das lanes e o retcode é a pista do motivo.
                    rcs = ", ".join(str(r) for r in (trade.retcodes or []))
                    null_rc = any(str(r) == "?" for r in (trade.retcodes or []))
                    if null_rc:
                        # retcode NULL devolvido pelo worker/terminal (ex.:
                        # 14:52 na lane 2 — order_send respondeu sem retcode,
                        # mapeado para "?"). O broker não disse NADA: pista
                        # de worker caído/ligado mas sem autorização ou IPC
                        # partido. Estado do worker no alerta para nunca
                        # passar despercebido.
                        ws = self._lane_worker_status_text(lane_id)
                        await self.notifier.send(
                            "risk",
                            f"👻 Lane {lane_id}: sinal {signal.symbol} SEM fill "
                            f"real — o broker respondeu SEM retcode (null, "
                            f"retcodes [{rcs}]), trade fechada phantom a 0.0. "
                            f"Geralmente worker/terminal sem ligação válida. "
                            f"{ws}",
                            lane=lane_id,
                        )
                    else:
                        await self.notifier.send(
                            "risk",
                            f"👻 Lane {lane_id}: sinal {signal.symbol} SEM fill "
                            f"real — ordens recusadas pelo broker (retcodes "
                            f"[{rcs}]), trade fechada phantom a 0.0 (sem razão de "
                            "saída). Não conta para o A/B das lanes; verifica o "
                            "terminal/AutoTrading.",
                        )
                else:
                    await self.notifier.send(
                        "risk",
                        f"⚠️ Sinal {signal.symbol} não executado na lane "
                        f"{lane_id}: {error}",
                    )
                lane_notes.append(f"{lane_id}: {error}")
                continue
            opened_trades.append(trade)
            lane_notes.append(f"{lane_id}: {strat['id']} (trade {trade.id})")

        if not opened_trades:
            # If every active lane is blocked only because its own minimum lot
            # exceeds its own risk budget, reject the signal (autonomous mode:
            # nothing waits for an operator; each lane already tried the
            # auto-fit above). A two-leg account may still pass while the
            # five-leg account does not, so this branch is reached only when
            # no lane opened.
            exceeded = {
                lane: gate for lane, gate in lane_risk_gates.items()
                if gate.get("exceeded")
            }
            if exceeded and not confirm_override:
                exceeded_msg = (
                    "lote mínimo não cabe no risco configurado em " + "; ".join(
                        f"{lane} ({gate['max_loss_est']:.2f} > "
                        f"{gate['risk_amount']:.2f})"
                        for lane, gate in exceeded.items()
                    )
                )
                # O gate de lote é PRÉ-computado por lane (antes do guard de
                # zona); uma lane pode ter sido bloqueada antes de lá chegar
                # (ex.: o preço fugiu da zona entre o guard e a execução —
                # incidente 04/09 07:32, sell XAUUSD rejeitado com "lote
                # mínimo" quando a causa real era a zona em AMBAS as lanes).
                # Se há notas reais não-lote, mostrá-las primeiro: o resumo
                # antigo escondia o motivo verdadeiro e nunca dizia o que
                # bloqueou as outras lanes.
                lot_notes = [n for n in lane_notes if "lote mínimo" in n]
                other_notes = [n for n in lane_notes if "lote mínimo" not in n]
                if other_notes:
                    error = "; ".join(other_notes)
                    if lot_notes:
                        error += " · " + exceeded_msg
                else:
                    error = exceeded_msg
                self.db.set_signal_status(signal.id, "rejected", error=error)
                signal.status = "rejected"
                signal.error = error
                await self.notifier.send(
                    "risk",
                    f"⛔ Sinal {signal.symbol} rejeitado automaticamente "
                    f"(modo autónomo): {error}",
                )
                return {"action": "min_lot_risk_rejected", "lanes": lane_risk_gates,
                        "signal": signal.to_dict()}
            signal.status = "failed"
            error = "nenhuma lane abriu a trade: " + "; ".join(lane_notes)
            signal.error = error
            self.db.set_signal_status(signal.id, "failed", error=error)
            await self.notifier.send(
                "risk", f"⚠️ Sinal {signal.symbol} não executado: {error}",
            )
            return {"action": "execution_failed", "error": error,
                    "signal": signal.to_dict()}

        signal.status = "executed"
        self.db.set_signal_status(signal.id, "executed")
        self.db.clear_signal_pending(signal.id)
        # paper trading forward: as estratégias de teste abrem a MESMA trade
        # em papel com os seus parâmetros, cada uma com o seu saldo virtual.
        # O kill diário da produção (daily_loss_limit_pct) também se aplica à
        # corrida paper — as contas virtuais refletem o que a produção real
        # faria se a estratégia fosse a principal.
        try:
            # O paper espelha a trade REAL que acabou de abrir: a mesma
            # entrada (preço de colocação) e o mesmo estilo (market vs
            # pendente na zona) — não a config às cegas, senão uma ordem a
            # mercado abriria `pending_fill` sem o preço alguma vez cruzar.
            opened = self.paper.open_for_signal(
                signal, risk_pct,
                daily_stop_pct=float(self.config.get("daily_loss_limit_pct") or 0),
                entry_style=(opened_trades[0].entry_style
                             if opened_trades else "market"),
                market_price=(opened_trades[0].market_price_at_open
                              if opened_trades else None),
            )
            if opened:
                # conta só as VISÍVEIS no feed (a `nova` contrafactual abre
                # papel mas não enche a notificação)
                from app.strategies import strategy_lifecycle
                vis = [t for t in opened
                       if strategy_lifecycle(
                           getattr(t, "strategy_id", "") or "")["menu"]]
                await self.notifier.send(
                    "system",
                    f"🧪 {len(vis)} estratégias abriram a trade "
                    f"{signal.symbol} em papel (corrida paralela)",
                )
        except Exception as exc:
            self.db.record_exception(
                exc, context={"component": "paper", "phase": "open_for_signal"},
                signal_id=signal.id,
            )
            print(f"[paper] falha ao abrir trade de teste: {exc}")
        # auditoria: quanto dinheiro real está em risco nesta trade
        eq = self.equity()
        risk_amt = round(eq * risk_pct / 100.0, 2)
        risk_currency = self._currency()
        lots_text = " · ".join(
            f"[{t.lane_id}] {_lots_text(t)}" for t in opened_trades
        ) if len(opened_trades) > 1 else _lots_text(opened_trades[0])
        lanes_text = " · ".join(
            f"{t.lane_id}:{t.strategy_id}" for t in opened_trades
        ) if len(opened_trades) > 1 else ""
        await self.notifier.send(
            "trade_open",
            f"🚀 Trade aberta (auto) {signal.symbol} "
            f"{signal.direction.upper()} @ {signal.entry} | "
            f"SL {signal.stop_loss} | {_tps_text(signal)} | "
            f"risco {risk_pct}% da equity = {risk_amt:,.2f} {risk_currency} | "
            f"lotes {lots_text}"
            + (f" | lanes {lanes_text}" if lanes_text else ""),
        )
        # management instructions the group posted while this signal was
        # still in the edit window ("SL HIT" replied before the trade opened)
        total_applied = 0
        for trade in opened_trades:
            total_applied += await self._apply_pending_management(signal, trade)
        if total_applied:
            await self.notifier.send(
                "system",
                f"📨 Aplicadas {total_applied} instruções de gestão pendentes "
                f"do grupo às trades {signal.symbol} acabadas de abrir",
            )
        return {"action": "executed",
                "trades": [t.to_dict() for t in opened_trades],
                "trade": opened_trades[0].to_dict(),
                "signal": signal.to_dict()}

    def _open_trade_on_lane(self, signal: Signal, risk_pct: float, strat: dict,
                            engine, lane_id: str, guard: dict,
                            confirm_override: bool, override_kind: str,
                            max_legs_override: Optional[int] = None):
        """Abre a trade do sinal numa lane; devolve o Trade (mesmo recusado)
        ou None em caso de exceção (a falha fica registada na BD).

        `max_legs_override` (auto-fit do lote mínimo) reduz o número de
        pernas para o risco caber no orçamento — nunca aumenta além do
        registo da estratégia."""
        guard_label = guard.get("status") or "n/a"
        if confirm_override:
            guard_label = f"manual_override:{override_kind or 'unknown'}:{guard_label}"
        if max_legs_override:
            guard_label = f"leg_fit:{max_legs_override}:{guard_label}"
        execution_mode = (
            "mt5-dry" if bool(getattr(engine, "dry_run", False))
            else "mt5-real" if getattr(engine, "provider", None) is not None
            else "sim"
        )
        # Fase 1: fora da zona (dentro do cap) → ordem pendente no nível da
        # entrada em vez de perseguir o preço a mercado. Dentro da zona ou na
        # tolerância automática → mercado normal.
        zone_pending = (guard.get("status") == "zone_deviation"
                        or (guard.get("status") == "tolerated"
                            and not guard.get("auto_execute")))
        entry_style = ("pending"
                       if self._entry_style() == "pending" and zone_pending
                       else "market")
        # lote por perna: o sinal pode trazer lote explícito (raro); senão,
        # a lane pode ter um lote FIXO por perna (`lots_override:<lane>` na
        # config — ex.: 0.04 na 2ª conta); sem nenhum, dimensiona por risco.
        lane_lots = signal.lots or self._fixed_lots_for_lane(lane_id)
        try:
            trade = engine.open_trade(
                signal, risk_pct, self._balance_for_lane(lane_id),
                lots_override=lane_lots,
                max_legs=max_legs_override or strat["max_legs"],
                auto=self._auto_params(strat),
                execution_meta={
                    "zone_low": guard.get("zone_low"),
                    "zone_high": guard.get("zone_high"),
                    "market_price": guard.get("market_price"),
                    "distance": guard.get("distance"),
                    "guard": guard_label,
                    "execution_mode": execution_mode,
                    "lane_id": lane_id,
                    "leg_fit": max_legs_override,
                    "entry_style": entry_style,
                },
            )
            # Persistir a estratégia que abriu a trade real permite encaminhar
            # protect/pending management corretamente após um restart.
            trade.strategy_id = strat["id"]
            self.db.set_trade_strategy(trade.id, strat["id"])
            return trade
        except Exception as exc:
            # nunca deixar o sinal em awaiting_edit após uma exceção: o tick
            # reabriria a trade a cada segundo (duplicados). Regista a falha.
            self.db.record_exception(
                exc, context={"component": "execution", "phase": "open_trade",
                              "lane": lane_id},
                signal_id=signal.id,
            )
            return None

    async def confirm_signal(self, signal_id: str, reason: str = "") -> dict:
        """Confirmação explícita e única de um sinal bloqueado por guard de
        entrada (fora da zona/tolerância) ou por lote mínimo acima do risco.

        A confirmação contorna APENAS esses dois gates para ESTE sinal; as
        verificações globais de risco (kill switch, perda diária, máximo de
        posições) voltam a correr e nunca podem ser contornadas. O motivo é
        registado no feed e o trade fica marcado como `manual_override`.
        """
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"error": "sinal não encontrado"}
        if signal.status != "pending":
            return {"error": f"estado atual: {signal.status}"}
        if signal.pending_kind not in ("entry_guard", "min_lot_risk"):
            return {"error": "este sinal não está bloqueado por guard de entrada/risco"}
        reason_text = (reason or "").strip()
        # Only global switches are checked here. Open-trade count and daily
        # realized P&L are evaluated independently inside _try_execute for
        # each lane, so confirming one account cannot consume the other one's
        # position/risk budget.
        decision: RiskDecision = self.risk.check(
            signal, self.config, [], 0.0, self._balance(),
        )
        if not decision.allowed:
            self.db.set_signal_status(signal.id, "rejected", error="; ".join(decision.reasons))
            await self.notifier.send(
                "risk",
                f"⛔ Confirmação recusada para {signal.symbol}: "
                f"{'; '.join(decision.reasons)}",
            )
            return {"action": "blocked", "reasons": decision.reasons, "signal": signal.to_dict()}
        guards = {lane: self._entry_guard(signal, lane) for lane in self._lanes()}
        guard = guards.get("main", {"status": "not_applicable"})
        risk_gate = self._min_lot_risk_gate(
            signal, self._balance_for_lane("main"), self._effective_risk_pct_for_lane("main"),
            self._lane_strategy("main").get("max_legs"),
        )
        await self.notifier.send(
            "risk",
            f"⚠️ Confirmação manual de {signal.symbol}: guard {guard.get('status')} · "
            f"preço {guard.get('market_price')} · risco {risk_gate['risk_amount']:.2f} € vs "
            f"perda máxima estimada {risk_gate['max_loss_est']:.2f} € · motivo: "
            f"{reason_text[:120] or '—'}",
        )
        return await self._try_execute(
            signal, confirm_override=True, override_reason=reason_text,
            override_kind=signal.pending_kind or "",
        )

    async def reject(self, signal_id: str) -> dict:
        """Descarta um sinal bloqueado por guard de entrada/risco de lote.

        Sem modo manual (Item 12) não há aprovação humana geral: esta rota
        existe apenas para o operador dispensar sinais à espera de
        confirmação de gate — nunca para vetar sinais que executariam
        sozinhos (esses passam pelas verificações de risco e pronto).
        """
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"error": "sinal não encontrado"}
        if signal.status != "pending":
            return {"error": f"estado atual: {signal.status}"}
        if signal.pending_kind not in ("entry_guard", "min_lot_risk"):
            return {"error": "sem modo manual: só sinais bloqueados por guard "
                             "de entrada/risco podem ser descartados"}
        self.db.set_signal_status(signal_id, "rejected")
        await self.notifier.send(
            "system", f"❌ Sinal {signal.symbol} descartado pelo operador (guard/risco)"
        )
        return {"action": "rejected"}

    async def delete_signal(self, signal_id: str) -> dict:
        """A Telegram message was deleted: mark the signal deleted and close
        any open position that was opened from it (at market price)."""
        signal = self.db.get_signal(signal_id)
        if not signal:
            return {"ok": True, "action": "not_found"}

        closed = 0
        if signal.status == "executed":
            for trade in self.db.open_trades():
                if trade.signal_id != signal.id:
                    continue
                events = self._engine_for_trade(trade).close_trade_at_market(trade.id)
                if events:
                    closed += 1
                    # MT5: the broker already moved the money; the config
                    # balance is only the paper (sim) source of truth.
                    if self._mt5_account() is None:
                        updated = self.db.get_trade(trade.id)
                        pnl = updated.pnl if updated else 0.0
                        self.config["balance"] = round(self._balance() + pnl, 2)
                        self.db.set_config("balance", self.config["balance"])

        self.db.set_signal_status(signal.id, "deleted", error="mensagem apagada no Telegram")
        note = " — posição fechada ao mercado" if closed else ""
        await self.notifier.send(
            "risk", f"🗑️ Sinal {signal.symbol or signal.id} apagado no Telegram{note}"
        )
        return {"ok": True, "action": "deleted", "closed_trades": closed}

    # ------------------------------------------------- group management

