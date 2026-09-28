"""Recounts do grupo: ground truth externa ao bot.

Movido verbatim de app/state.py (fase de corte); os métodos vivem num Mixin
misturado no BotState para continuarem acessíveis como `self.*`.
"""
from __future__ import annotations

import json

from app.execution.engine import _now_iso
from app.messages.manage import symbol_in
from app.models import Signal, SignalIn


class RecountsMixin:
    """Handle/audit/candle-check/batches de recounts (R2/R3)."""

    async def _handle_recount(self, body: SignalIn, signal_id: str,
                              received_at: str) -> dict:
        """R2: store a per-trade recount and audit it against the bot's own
        real closed fills of the same day.

        The recount ("Signal #1: Sell at 4357 … +110 pips") is structured
        ground truth EXTERNAL to the bot. It never opens/closes trades; it is
        stored read-only and compared with the real `trades` of the same day
        (direction + closest entry). A divergent entry or pips reported vs
        the bot's own fills raises a `risk` notification so an operator can
        investigate a possible fill/execution problem.
        """
        from app.messages.recounts import parse_recount

        fields = parse_recount(body.raw_text or "") or {}
        reason = "recount do grupo (ground truth)" if fields else "recount não reconhecido"
        recount_signal = Signal(
            id=signal_id, source=body.source, raw_text=body.raw_text,
            symbol=symbol_in(body.raw_text) or "", direction="", entry=0.0,
            stop_loss=0.0, take_profit_1=0.0, take_profit_2=0.0,
            status="historical" if body.historical else "management",
            received_at=received_at,
            message_id=str(body.message_id) if body.message_id else None,
            reply_to_msg_id=str(body.reply_to_msg_id) if body.reply_to_msg_id else None,
            error=reason,
        )
        self.db.insert_signal(recount_signal)
        if not fields:
            if not body.historical:
                await self.notifier.send(
                    "risk",
                    f"⚠️ Recount do grupo não reconhecido: "
                    f"{(body.raw_text or '').strip()[:100]}",
                )
            return {"action": "management", "kind": "recount", "parsed": False,
                    "signal": recount_signal.to_dict()}

        row_id = self.db.insert_trade_recount(
            body.source or "", fields, body.raw_text or "",
            received_at or _now_iso(), str(body.message_id or ""),
        )
        audit = self._recount_fill_audit(fields, received_at or _now_iso())
        if not body.historical and row_id:
            # candle-check da excursão/reversão reportadas contra o high/low
            # real do dia: um replay implausível (excursão acima do high ou
            # reversão fora do range) merece investigação do operador.
            try:
                cc = await self._recount_candle_check(
                    audit, dict(fields), received_at or _now_iso())
            except Exception as exc:
                print(f"[recount] candle-check falhou: {exc}")
                cc = {"checked": False}
            if cc.get("checked") and cc.get("plausible") is False \
                    and cc.get("futures_plausible") is not True:
                # relato suspeito só com CONFIRMAÇÃO: o spot MT5 (primária —
                # o grupo negocia spot) discorda E o GC=F também discorda
                # (ou não há GC=F para confirmar). Se o spot diz implausível
                # mas o GC=F diz plausível, não há confirmação — registado
                # sem alerta risk (a divergência spot↔futuros já fabricou
                # falsos implausíveis antes do spot ser a fonte primária).
                if cc.get("futures_plausible") is False:
                    confirmed = " (spot MT5 + GC=F)"
                else:
                    confirmed = " (spot MT5 — sem GC=F para confirmar)"
                # o veredicto preferido é a janela horária (±3h à volta da
                # hora do sinal): o high/low do dia mistura sessões (um pico
                # às 22:00 não valida um relato do sinal das 09:00). O dia
                # inteiro fica como contexto.
                if cc.get("window_plausible") is not None:
                    lo, hi = cc.get("window_low"), cc.get("window_high")
                    exc_ok = cc.get("window_excursion_ok")
                    rev_ok = cc.get("window_reversal_ok")
                    scope = f"janela ±{cc.get('window_hours', 3)}h do sinal"
                else:
                    lo, hi = cc.get("day_low"), cc.get("day_high")
                    exc_ok = cc.get("excursion_ok")
                    rev_ok = cc.get("reversal_ok")
                    scope = "dia"
                # evidência estruturada no journal (opt-in): o replay de
                # evidência e o fill-audit podem cruzar os relatos
                # implausíveis sem re-consultar o Yahoo
                if self.config.get("execution_journal_enabled"):
                    try:
                        self.db.record_execution_event(
                            _now_iso(), "recount_implausible",
                            engine="runtime", symbol=cc.get("symbol") or "",
                            signal_id=signal_id,
                            payload={
                                "message_id": str(body.message_id or ""),
                                "signal_number": fields.get("signal_number"),
                                "direction": fields.get("direction"),
                                "entry": fields.get("entry"),
                                "excursion_price": fields.get("excursion_price"),
                                "excursion_dir": fields.get("excursion_dir"),
                                "reversal_price": fields.get("reversal_price"),
                                "pips": fields.get("pips"),
                                "scope": scope, "hi": hi, "lo": lo,
                                "excursion_ok": exc_ok, "reversal_ok": rev_ok,
                                "day_high": cc.get("day_high"),
                                "day_low": cc.get("day_low"),
                                "window_plausible": cc.get("window_plausible"),
                                "window_high": cc.get("window_high"),
                                "window_low": cc.get("window_low"),
                                "spot_plausible": cc.get("spot_plausible"),
                                "futures_plausible": cc.get("futures_plausible"),
                                "feed_divergence": cc.get("feed_divergence"),
                                "verdict_kind": cc.get("verdict_kind"),
                                "excursion_reported": cc.get("excursion_reported"),
                                "reversal_reported": cc.get("reversal_reported"),
                            },
                        )
                    except Exception as exc:
                        print(f"[recount] journal recount_implausible falhou: {exc}")
                await self.notifier.send(
                    "risk",
                    f"⚠️ Recount implausível confirmado{confirmed} vs candles "
                    f"({scope} {hi}/{lo}): Signal "
                    f"#{fields['signal_number']} {fields['direction'].upper()} "
                    f"@ {fields['entry']} reporta excursão até "
                    f"{fields.get('excursion_price')} e reversão para "
                    f"{fields.get('reversal_price')} "
                    f"(excursion_ok={exc_ok}, reversal_ok={rev_ok}) — revisita o relato.",
                )
        if audit.get("alert") and not body.historical:
            detail = audit.get("reason") or "entrada/pips divergentes"
            await self.notifier.send(
                "risk",
                f"⚠️ Recount divergente do fill real: Signal #{fields['signal_number']} "
                f"{fields['direction'].upper()} @ {fields['entry']} reporta "
                f"{fields['pips']:+.0f} pips mas o bot fechou "
                f"{audit.get('bot_pips', 0):+.0f} pips ({detail}) — "
                f"revisita a entrada/execução deste sinal.",
            )
        elif (not audit.get("matched") and not body.historical
                and audit.get("matched") is not None):
            # O grupo recountou um sinal que o bot NÃO negociou no dia (nem
            # abriu nem fechou) — informativo, não erro: cruza com os sinais
            # do dia para apontar a causa provável (failed/ignorado/kill).
            causa = audit.get("probable_cause") or "sinal não negociado"
            await self.notifier.send(
                "system",
                f"📋 Recount sem trade correspondente: Signal #{fields['signal_number']} "
                f"{fields['direction'].upper()} @ {fields['entry']} reporta "
                f"{fields['pips']:+.0f} pips mas o bot não tem trade fechada "
                f"nesse dia/entrada — {causa}.",
            )
        return {"action": "management", "kind": "recount", "parsed": True,
                "recount_id": row_id, "audit": audit,
                "signal": recount_signal.to_dict()}

    async def recounts_report(self, source: str = "", limit: int = 200) -> dict:
        """Recounts do grupo com o fill-audit do mesmo dia lado a lado (R2).

        Read-only: junta cada recount guardado em `trade_recounts` com o
        resultado de `_recount_fill_audit` (trade real correspondente, pips
        do bot, direção e divergência) + a verificação da excursão/reversão
        reportadas contra os candles reais do dia (quando o símbolo é
        conhecido) + a causa provável quando não há trade. Devolve também
        `weekly` (estado por semana) para o mini-gráfico da dashboard.
        """
        rows = self.db.list_trade_recounts(source_like=source, limit=limit)
        out = []
        for row in rows:
            audit = self._recount_fill_audit(
                {"direction": row.get("direction"), "entry": row.get("entry"),
                 "pips": row.get("pips"),
                 "excursion_price": row.get("excursion_price"),
                 "excursion_dir": row.get("excursion_dir"),
                 "reversal_price": row.get("reversal_price"),
                 "symbol": row.get("symbol")},
                row.get("received_at") or "",
            )
            audit["candle_check"] = await self._recount_candle_check(
                audit, row, row.get("received_at") or "",
            )
            out.append({"recount": row, "audit": audit})
        return {"ok": True, "recounts": out, "weekly": self._recount_weekly(out)}

    async def recheck_recount(self, recount_id: int) -> dict:
        """Re-corre o candle-check de um recount com candles FRESCOS (R2).

        O Yahoo pode ter corrigido/expandido o histórico; este re-check
        refaz o fill-audit + candle-check para o recount pedido e devolve o
        veredicto atualizado, sem alterar a linha guardada nem o journal.
        Read-only para a execução.
        """
        rows = [r for r in self.db.list_trade_recounts(limit=20000)
                if r.get("id") == int(recount_id)]
        if not rows:
            return {"ok": False, "error": "recount não encontrado"}
        row = rows[0]
        audit = self._recount_fill_audit(
            {"direction": row.get("direction"), "entry": row.get("entry"),
             "pips": row.get("pips"),
             "excursion_price": row.get("excursion_price"),
             "excursion_dir": row.get("excursion_dir"),
             "reversal_price": row.get("reversal_price"),
             "symbol": row.get("symbol")},
            row.get("received_at") or "",
        )
        audit["candle_check"] = await self._recount_candle_check(
            audit, row, row.get("received_at") or "",
        )
        return {"ok": True, "recount": row, "audit": audit,
                "rechecked_at": _now_iso()}

    async def recheck_all_recounts(self, days: int = 90,
                                   window: str = "daily") -> dict:
        """Re-corre o candle-check de TODOS os recounts com candles frescos e
        o offset mais recente, persistindo o veredicto em lote na BD.

        A Yahoo pode ter corrigido/expandido o histórico desde a primeira
        análise e a calibração do offset GC=F↔spot pode ter passado a estar
        ativa (≥3 fills); este lote refaz o fill-audit + candle-check de
        todos os recounts (candles buscados UMA vez por símbolo, com a
        janela adaptativa por símbolo) e grava o veredicto em
        `trade_recounts.candle_check` (+ `checked_at`). Cada corrida fica
        persistida em `recount_batch_runs` com os flips resolvidos
        (implausível/feed → plausível) para o /auditoria mostrar a evolução
        das correções do offset. Read-only para a execução: não abre/fecha
        trades, não altera o journal.
        """
        rows = self.db.list_trade_recounts(limit=20000)
        if not rows:
            return {"ok": True, "total": 0, "rechecked": 0, "changed": 0,
                    "errors": 0, "note": "sem recounts em trade_recounts"}
        rechecked = changed = errors = resolved = 0
        flips = []
        ran_at = _now_iso()
        for row in rows:
            try:
                day = (row.get("received_at") or "")[:10]
                symbol, sig_time = self._recount_resolve(row, day)
                audit = self._recount_fill_audit(
                    {"direction": row.get("direction"),
                     "entry": row.get("entry"), "pips": row.get("pips"),
                     "excursion_price": row.get("excursion_price"),
                     "excursion_dir": row.get("excursion_dir"),
                     "reversal_price": row.get("reversal_price"),
                     "symbol": row.get("symbol")},
                    row.get("received_at") or "",
                )
                # fontes combinadas com cache por símbolo (MT5 spot + GC=F):
                # a primeira iteração busca, as seguintes reutilizam; o GC=F
                # usa o OFFSET DIÁRIO medido nos fills do dia (_day_futures_offset)
                cc = await self._recount_candle_check(
                    audit, row, row.get("received_at") or "",
                    resolved_symbol=symbol, signal_time=sig_time or "",
                )
                self.db.set_recount_verdict(
                    int(row["id"]), cc, ran_at)
                rechecked += 1
                # `row` vem do SELECT antes de o UPDATE. O baseline é o
                # último veredicto persistido, mas tolera dados antigos que
                # só guardavam `plausible`/`feed_divergence` sem
                # `verdict_kind`. Assim um recheck individual seguido do
                # lote não perde o flip resolvido.
                stored = json.loads(
                    (row.get("candle_check") or "{}") or "{}")
                old_kind = stored.get("verdict_kind")
                if not old_kind and stored.get("checked"):
                    if stored.get("feed_divergence"):
                        old_kind = "feed_divergence"
                    elif stored.get("plausible") is False:
                        old_kind = "implausible"
                    elif stored.get("plausible") is True:
                        old_kind = "plausible"
                if stored.get("plausible") != cc.get("plausible"):
                    changed += 1
                # veredicto RESOLVIDO: implausível/feed_divergence → plausível
                # (o offset diário mostrava que era feed, não relato falso) —
                # coleciona o detalhe (data, recount, de → para) para historial
                if (old_kind in ("implausible", "feed_divergence")
                        and cc.get("verdict_kind") == "plausible"):
                    resolved += 1
                    flips.append({
                        "id": int(row.get("id") or 0),
                        "day": day,
                        "signal_number": row.get("signal_number"),
                        "from": old_kind or "implausible",
                        "to": cc.get("verdict_kind") or "plausible",
                        "shift": cc.get("futures_shift"),
                    })
            except Exception as exc:
                errors += 1
                print(f"[recount] lote: recount {row.get('id')} falhou: {exc}")
        res = {"ok": True, "total": len(rows), "rechecked": rechecked,
               "changed": changed, "resolved": resolved, "errors": errors,
               "window_hours_default": self._recount_window_hours(),
               "ran_at": ran_at, "window": window, "flips": flips,
               "last_resolved": resolved}
        try:
            self.db.record_recount_batch(res, flips)
        except Exception as exc:
            print(f"[recount] não gravou historial do lote: {exc}")
        return res

    def _recount_fill_audit(self, fields: dict, received_at: str) -> dict:
        """Compare one recount with the bot's real closed trades of the same
        day (read-only). Returns the matched trade, the bot's own pips and an
        ``alert`` flag when they diverge beyond tolerance.

        Matching is conservative: same direction + the closed trade with the
        closest entry, within ``pip * 40`` price units (0.40 in gold, 0.004
        in forex). A recount with no matching real trade sets ``matched=False``
        and ``probable_cause`` (failed/rejected signal, opposite direction,
        kill switch, or nothing at all) by cross-checking the day's signals.
        """
        from app.backtest import _contract, _pip_size

        day = (received_at or "")[:10]
        if not day:
            return {"matched": False}
        direction = fields.get("direction") or ""
        entry = float(fields.get("entry") or 0.0)
        trades = [t for t in self.db.trades_closed_on(day)]
        same_dir = [t for t in trades if t.direction == direction]
        candidates = same_dir or trades
        if not candidates:
            return {"matched": False,
                    "probable_cause": self._recount_no_trade_cause(fields, day)}
        best = min(candidates, key=lambda t: abs(t.entry - entry))
        pip = _pip_size(best.symbol)
        if abs(best.entry - entry) > pip * 40:
            return {"matched": False,
                    "probable_cause": self._recount_no_trade_cause(fields, day)}

        direction_mismatch = best.direction != direction
        # Bot pips for that trade: weighted price excursion of its legs
        # (positions are authoritative; pnl fallback includes costs).
        positions = self.db.positions_for_trade(best.id)
        sign = 1.0 if best.direction == "buy" else -1.0
        bot_pips = 0.0
        if positions:
            lots = sum(float(p.lots or 0.0) for p in positions)
            if lots > 0:
                weighted = sum(
                    float(p.lots or 0.0) * (float(p.close_price or p.entry) - float(p.entry))
                    for p in positions
                )
                bot_pips = sign * weighted / lots / pip
        else:
            lots = sum(float(getattr(best, f"lots_{i}", 0.0) or 0.0) for i in range(1, 6))
            if lots > 0:
                # pips ≈ P&L em unidades de preço: pnl / (lotes × contract × pip)
                bot_pips = float(best.pnl or 0.0) / (lots * _contract(best.symbol) * pip) * sign

        entry_delta_pips = abs(best.entry - entry) / pip
        reported = float(fields.get("pips") or 0.0)
        pips_delta = abs(reported - bot_pips)
        alert = False
        reason = ""
        if direction_mismatch:
            alert = True
            reason = (f"direção divergente (grupo {direction.upper()} mas o bot "
                      f"fechou {best.direction.upper()} @ {best.entry})")
        elif entry_delta_pips >= 20:
            alert = True
            reason = f"entrada difere {entry_delta_pips:.0f} pips"
        elif pips_delta >= 30:
            alert = True
            reason = f"pips diferem {pips_delta:.0f} pips"
        return {
            "matched": True, "trade_id": best.id, "symbol": best.symbol,
            "bot_entry": best.entry, "bot_pips": round(bot_pips, 1),
            "reported_pips": reported,
            "entry_delta_pips": round(entry_delta_pips, 1),
            "pips_delta_pips": round(pips_delta, 1),
            "direction_mismatch": direction_mismatch,
            "alert": alert, "reason": reason,
        }

    def _recount_no_trade_cause(self, fields: dict, day: str) -> str:
        """R2: sem trade fechada correspondente — cruza com os sinais do dia
        para apontar a causa provável (failed/rejected, pendente, direção
        oposta, ou simplesmente nada)."""
        from app.backtest import _pip_size

        entry = float(fields.get("entry") or 0.0)
        direction = fields.get("direction") or ""
        day_signals = [s for s in self.db.signals_since(day)
                       if (s.received_at or "")[:10] == day and s.entry]
        pip = None
        for s in day_signals:
            if pip is None:
                pip = _pip_size(s.symbol)
            if abs(s.entry - entry) <= (pip or 0.0001) * 40:
                if s.status in ("failed", "rejected"):
                    return (f"sinal do dia ficou {s.status} (formato não "
                            f"reconhecido/janela perdida) — o grupo negociou, "
                            f"o bot não")
                if s.status == "pending":
                    return ("sinal do dia ficou pendente (guard de entrada/"
                            "lote à espera de confirmação do operador)")
                if s.direction and s.direction != direction:
                    return (f"direção divergente no dia: o bot registou "
                            f"{s.direction.upper()} @ {s.entry}, o grupo "
                            f"reporta {direction.upper()}")
        return "nenhum sinal correspondente no dia (kill switch, guard de " \
               "entrada por confirmar ou sinal de outro grupo/feed)"

    def _recount_window_hours(self, symbol: str = "") -> float:
        """Janela horária do candle-check por símbolo (config-driven).

        O atraso sinal→mercado varia por símbolo (medido pelo estudo
        `app/study_signal_delay.py --apply` que grava a recomendação em
        `recount_window_hours_by_symbol`); o default é `recount_window_hours`
        (3h). Limitada a [1, 24] para nunca abrir a janela a sessões
        inteiras por acidente."""
        import json
        try:
            cfg = self.db.all_config()
            by_sym = json.loads(cfg.get("recount_window_hours_by_symbol") or "{}")
            if symbol in by_sym:
                return max(1.0, min(24.0, float(by_sym[symbol])))
            return max(1.0, min(24.0, float(cfg.get("recount_window_hours", 3.0))))
        except (ValueError, TypeError):
            return 3.0

    def _recount_resolve(self, row: dict, day: str,
                         symbol: str = "") -> tuple:
        """Resolve o símbolo e a hora do sinal alvo de um recount (R2).

        O recount não traz símbolo nem hora: usa o sinal do dia com entrada
        mais próxima (o mesmo cruzamento do fill-audit). Com `symbol` dado
        (testes/estudo/lote), procura apenas a hora do sinal nesse símbolo.
        Devolve (symbol, signal_time)."""
        from app.backtest import _pip_size

        entry = float(row.get("entry") or 0.0)
        best_sym, best_d, signal_time = "", None, None
        for s in self.db.signals_since(day):
            if (s.received_at or "")[:10] != day or not s.entry:
                continue
            pip = _pip_size(s.symbol)
            d = abs(float(s.entry) - entry)
            if best_d is None or d < best_d:
                if (not symbol or s.symbol == symbol) and s.symbol \
                        and d <= (pip or 0.0001) * 40:
                    best_sym, best_d, signal_time = s.symbol, d, s.received_at
        return (symbol or best_sym, signal_time)

    async def _recount_candles(self, symbol: str) -> dict:
        """Candles do candle-check por símbolo (cache 5 min): spot MT5 quando
        o terminal está ligado + GC=F Yahoo sempre.

        O feed Yahoo serve futuros (GC=F) e o grupo negocia spot — a
        divergência de dezenas de pontos fabrica implausíveis falsos. Quando
        o terminal MT5 está ligado, busca os candles M15 de XAUUSD spot
        nativos; o GC=F fica disponível para o veredicto combinado (que
        distingue relato suspeito de divergência do feed). Devolve
        ``{"spot": list|None, "futures": list|None}``."""
        import time as _time
        cache = getattr(self, "_recount_candles_cache", None)
        if cache:
            now = _time.time()
            cached = cache.get(symbol)
            if cached and now - cached[0] < 300:
                return cached[1]
        sources: dict = {"spot": None, "futures": None}
        provider = getattr(getattr(self, "sim", None), "provider", None)
        if provider is not None and getattr(provider, "connected", False):
            try:
                rates = provider.copy_rates(symbol, timeframe=15, count=9000)
                if rates:
                    sources["spot"] = [
                        {"ts": int(r["time"]), "open": r["open"],
                         "high": r["high"], "low": r["low"],
                         "close": r["close"]} for r in rates]
            except Exception as exc:
                print(f"[recount] candles spot indisponíveis: {exc}")
        from app.execution.feed import YahooProvider
        try:
            sources["futures"] = await YahooProvider().history(
                symbol, interval="15m", range_="1mo")
        except Exception as exc:
            print(f"[recount] candles GC=F indisponíveis: {exc}")
        if cache:
            cache[symbol] = (_time.time(), sources)
        return sources

    def _day_futures_offset(self, day: str, symbol: str,
                            futures_candles: list) -> float:
        """Offset GC=F − spot MEDIDO, por dia quando há fills do dia.

        Para cada trade real com `market_price_at_open` (spot do guard) e
        candles GC=F à hora do sinal: offset = close do candle − preço spot.
        Prefere a média do próprio dia; sem fills do dia usa a média dos
        últimos 30 dias do mesmo símbolo (medida, nunca um offset fixo); sem
        nenhuma amostra devolve 0 (comportamento anterior). O prémio do
        futuro varia dia a dia (na BD real: 53–60 pips por dia) e um offset
        fixo fabricava implausíveis falsos no GC=F.
        """
        import time as _time
        from datetime import date, datetime, timedelta
        cache = getattr(self, "_recount_day_offset_cache", None)
        if cache:
            now = _time.time()
            cached = cache.get((day, symbol))
            if cached and now - cached[0] < 300:
                return cached[1]
        since = (date.today() - timedelta(days=30)).isoformat()
        day_offsets: list = []
        all_offsets: list = []
        for t in self.db.trades_opened_since(since):
            if t.symbol != symbol or t.market_price_at_open is None:
                continue
            sig = self.db.get_signal(t.signal_id)
            if sig is None or not sig.received_at:
                continue
            try:
                epoch = datetime.fromisoformat(sig.received_at).timestamp()
            except (ValueError, TypeError):
                continue
            close = None
            for c in futures_candles:
                if c["ts"] >= epoch:
                    close = float(c["close"])
                    break
            if close is None:
                continue
            off = close - float(t.market_price_at_open)
            all_offsets.append(off)
            if (t.opened_at or "")[:10] == day:
                day_offsets.append(off)
        pool = day_offsets or all_offsets
        offset = round(sum(pool) / len(pool), 2) if pool else 0.0
        if cache:
            cache[(day, symbol)] = (_time.time(), offset)
        return offset

    def _recount_check_source(self, candles: list, day: str, sig_ts,
                              excursion_price, excursion_dir, reversal_price,
                              pip: float, tol: float, shift: float,
                              window_hours: float) -> dict:
        """Veredicto de UMA fonte de candles (dia + janela) — usado para o
        spot MT5 e o GC=F separadamente no candle-check combinado."""
        from datetime import datetime, timedelta

        day_start = datetime.fromisoformat(day + "T00:00:00+00:00")
        day_end_ts = int((day_start + timedelta(days=1)).timestamp())
        day_candles = [c for c in candles if c["ts"] < day_end_ts
                       and c["ts"] >= int(day_start.timestamp())]
        if not day_candles:
            return {"checked": False, "reason": "sem candles do dia"}
        day_high = max(c["high"] for c in day_candles)
        day_low = min(c["low"] for c in day_candles)
        exc = float(excursion_price or 0.0) + shift
        rev = float(reversal_price or 0.0) + shift
        if excursion_dir == "up":
            exc_ok = day_high >= exc - tol
        elif excursion_dir == "down":
            exc_ok = day_low <= exc + tol
        else:
            exc_ok = None
        rev_ok = day_low - tol <= rev <= day_high + tol
        day_plausible = (exc_ok is not False) and bool(rev_ok)
        out = {
            "checked": True,
            "day_high": round(day_high, 2), "day_low": round(day_low, 2),
            "excursion_ok": exc_ok, "reversal_ok": rev_ok,
            "day_plausible": day_plausible, "plausible": day_plausible,
        }
        # janela horária à volta da hora do sinal: o high/low do dia inteiro
        # mistura sessões — um pico às 22:00 não valida um relato do sinal
        # das 09:00. Quando o sinal alvo é conhecido, a janela é o veredicto
        # da sessão; o dia inteiro fica exposto como day_plausible/day_*.
        if sig_ts:
            window_secs = window_hours * 3600
            win = [c for c in day_candles
                   if abs(c["ts"] - sig_ts) <= window_secs]
            if win:
                win_high = max(c["high"] for c in win)
                win_low = min(c["low"] for c in win)
                if excursion_dir == "up":
                    w_exc = win_high >= exc - tol
                elif excursion_dir == "down":
                    w_exc = win_low <= exc + tol
                else:
                    w_exc = None
                w_rev = win_low - tol <= rev <= win_high + tol
                out["window_hours"] = window_hours
                out["window_high"] = round(win_high, 2)
                out["window_low"] = round(win_low, 2)
                out["window_excursion_ok"] = w_exc
                out["window_reversal_ok"] = w_rev
                out["window_plausible"] = (w_exc is not False) and bool(w_rev)
                # a janela é o veredicto da sessão do sinal
                out["plausible"] = out["window_plausible"]
        return out

    async def _recount_candle_check(self, audit: dict, row: dict,
                                    received_at: str, candles=None,
                                    resolved_symbol: str = "",
                                    signal_time: str = "") -> dict:
        """R2: compara a excursão/reversão reportadas pelo grupo com o range
        real dos candles do dia (read-only). Sem símbolo conhecido ou sem
        candles → ``checked=False`` (nunca inventa plausibilidade).

        Fontes (veredicto COMBINADO): spot MT5 quando o terminal está ligado
        (primária — o grupo negocia spot) + GC=F Yahoo (futuros) para
        classificar; quando o spot diz plausível mas o GC=F implausível,
        ``feed_divergence=True`` (falso positivo do feed, não relato falso).
        O GC=F é comparado com o OFFSET DIÁRIO medido nos fills reais do dia
        (``_day_futures_offset`` — GC=F − spot; 0 sem amostras): o prémio do
        futuro varia dia a dia e um offset fixo fabricava implausíveis falsos.

        `candles`/`resolved_symbol`/`signal_time` mantêm o modo
        LOTE/estudo: candles pré-buscados contam como fonte futuros (o
        estudo MT5 passa candles spot sem offset, que é o comportamento
        padrão agora)."""
        from datetime import datetime, timezone

        day = (received_at or "")[:10]
        symbol = resolved_symbol or audit.get("symbol") or row.get("symbol") or ""
        excursion_price = row.get("excursion_price")
        excursion_dir = row.get("excursion_dir")
        reversal_price = row.get("reversal_price")
        if not day or not excursion_price or not reversal_price:
            return {"checked": False}
        # o recount não traz símbolo nem hora: resolve ambos pelo sinal do
        # dia com entrada mais próxima (o mesmo cruzamento do fill-audit)
        if not signal_time:
            if not symbol:
                symbol, signal_time = self._recount_resolve(row, day)
                if not symbol:
                    return {"checked": False,
                            "reason": "sem símbolo associável ao dia"}
            else:
                _, signal_time = self._recount_resolve(row, day, symbol=symbol)
        try:
            sig_ts = datetime.fromisoformat(signal_time).timestamp() \
                if signal_time else None
        except (ValueError, TypeError):
            sig_ts = None
        from app.backtest import _pip_size
        pip = _pip_size(symbol)
        tol = pip * 40
        window_hours = self._recount_window_hours(symbol)

        if candles is not None:
            sources = {"spot": None, "futures": candles}
        else:
            sources = await self._recount_candles(symbol)
        spot_candles = sources.get("spot")
        futures_candles = sources.get("futures")
        spot_res = None
        if spot_candles:
            spot_res = self._recount_check_source(
                spot_candles, day, sig_ts, excursion_price, excursion_dir,
                reversal_price, pip, tol, 0.0, window_hours)
        # o GC=F é comparado com o OFFSET DIÁRIO medido nos fills reais do
        # dia (GC=F − spot): os preços reportados pelo grupo são spot e o
        # prémio do futuro varia dia a dia — um offset fixo fabricava
        # implausíveis falsos (baseline 66% no GC=F). Sem amostras do dia o
        # offset é 0 (comportamento anterior).
        fut_res = None
        fut_shift = 0.0
        if futures_candles:
            fut_shift = self._day_futures_offset(day, symbol, futures_candles)
            fut_res = self._recount_check_source(
                futures_candles, day, sig_ts, excursion_price, excursion_dir,
                reversal_price, pip, tol, fut_shift, window_hours)
        # primária: o spot é a verdade (o grupo negocia spot); sem spot usa
        # o GC=F (comportamento de quando o terminal está desligado)
        primary = spot_res if (spot_res and spot_res.get("checked")) \
            else fut_res
        if primary is None:
            return {"checked": False,
                    "reason": "sem candles de nenhuma fonte"}
        if not primary.get("checked"):
            return primary
        out = dict(primary)
        out.update({
            "checked": True, "symbol": symbol, "day": day,
            "spot_plausible": spot_res.get("plausible") if spot_res else None,
            "futures_plausible": fut_res.get("plausible") if fut_res else None,
            # offset diário GC=F↔spot aplicado ao GC=F (0.0 sem amostras)
            "futures_shift": round(fut_shift, 2),
            "feed_divergence": bool(
                spot_res and spot_res.get("checked")
                and spot_res.get("plausible") and fut_res
                and fut_res.get("checked") and not fut_res.get("plausible")),
        })
        # verdict_kind para a UI: relato suspeito vs divergência do feed
        if not out.get("plausible"):
            out["verdict_kind"] = "implausible"
        elif out.get("feed_divergence"):
            out["verdict_kind"] = "feed_divergence"
        else:
            out["verdict_kind"] = "plausible"
        return out

    def _recount_weekly(self, items: list) -> list:
        """R2: agrega os recounts por semana ISO (estado: total, coerentes,
        divergentes, sem trade) para o mini-gráfico da tab Relatório."""
        from datetime import datetime

        weeks: dict = {}
        for item in items:
            rc = item.get("recount") or {}
            au = item.get("audit") or {}
            received = rc.get("received_at") or ""
            try:
                dt = datetime.fromisoformat(received)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                week = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
            except (ValueError, TypeError):
                week = "desconhecido"
            w = weeks.setdefault(week, {"week": week, "total": 0, "coherent": 0,
                                        "divergent": 0, "no_trade": 0})
            w["total"] += 1
            if au.get("matched"):
                if au.get("alert"):
                    w["divergent"] += 1
                else:
                    w["coherent"] += 1
            else:
                w["no_trade"] += 1
        return [weeks[k] for k in sorted(weeks, reverse=True)]

    async def _check_group_divergence(self, body: SignalIn, fields: dict,
                                      threshold_pp: float = 10.0) -> None:
        """Compare the group's reported win rate with the bot's own backtest
        on the SAME real signals. When they drift apart by more than
        `threshold_pp`, warn in the dashboard feed — the strategy may be
        diverging from what the group claims.

        R3: the group defines a "win" as any trade that moved 25–30 pips in
        its favour (``mfe_win_rate``), not as a closed pnl>0. Comparing the
        group's WR against the bot's pnl-based WR produces spurious alerts
        for trades that ran +40 pips and came back to SL. The check now
        compares against the MFE-based WR when the backtest can measure it,
        falling back to the pnl-based WR when there is no MFE data.
        """
        if not fields.get("win_rate"):
            return
        try:
            edge = await self.edge_analysis(group=body.source or "", days=30)
        except Exception as exc:
            print(f"[divergência] edge indisponível: {exc}")
            return
        agg = edge.get("aggregate") or {}
        bot_wr = agg.get("mfe_win_rate")
        if bot_wr is None:
            bot_wr = agg.get("win_rate") or 0.0
        group_wr = float(fields["win_rate"])
        diff = group_wr - bot_wr
        if abs(diff) < threshold_pp:
            return
        direction = "acima" if diff > 0 else "abaixo"
        metric = "MFE≥25 pips" if agg.get("mfe_win_rate") is not None else "win rate pnl"
        await self.notifier.send(
            "risk",
            f"⚠️ Divergência grupo vs bot: o grupo reporta {group_wr:.0f}% WR "
            f"({fields['trades_sent']} trades) mas o backtest do bot dá "
            f"{bot_wr:.0f}% ({metric}) — o grupo está {direction} do bot por "
            f"{abs(diff):.0f}pp. Revisita a gestão/execução antes de confiar "
            f"nos números do grupo.",
        )

