"""Smoke test de execução REAL dual-lane — prova de custo ~zero das DUAS contas.

Abre e fecha a mercado uma trade de 0.01 lote em CADA conta MT5 demo (a
principal + a 2ª) pelas engines reais de cada lane — worker isolado → 2ª
conta; processo principal → 1ª conta. As DUAS trades partilham o MESMO sinal
sintético (`s-<ts>-<rand>`), como um sinal real do grupo, para a notificação
WhatsApp dual-lane sair com as duas contas lado a lado e o P&L somado.

O caminho é o MESMO de uma trade do grupo em cada lane: `open_trade` escreve
as rows de trade/posição na BD, `close_trade_at_market` fecha e emite os
eventos `close`/`trade_closed`, e a notificação WhatsApp dual-lane dispara
pelo hook habitual — sem depender de um sinal do Telegram.

Guarda demo-only (corre ANTES de qualquer ordem):
  * opt-in explícito (`LANE2_SMOKE_ALLOW=1` no .env — o endpoint nunca fica
    ativo só por existir);
  * as DUAS lanes em execução REAL (`dry_run=False`, terminal/worker ligados,
    AutoTrading ON) — uma lane em dry-run/paper recusa o smoke inteiro;
  * contas DEMO obrigatórias (`trade_mode == 0` do account_info MT5) — uma
    conta real/contest recusa SEMPRE;
  * cooldown entre corridas (`lane2_smoke_last_ts` na config, intervalo
    `LANE2_SMOKE_COOLDOWN`, default 120 s) para não spammar o broker.

O módulo é usado pelo endpoint `POST /api/lanes/smoke` (app/main.py) e pelos
testes — quem decide é sempre o processo principal (invariante do worker);
aqui só se executa o que a guarda deixou passar.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from app.execution.engine import _new_id, _now_iso
from app.models import Signal

# Marcações das trades smoke na BD: `execution_mode='mt5-smoke'` e
# `strategy_id='smoke_test'` para a analytics/dashboard nunca ler o teste
# como resultado de uma estratégia (A/B, realized pnl por estratégia, etc.).
SMOKE_STRATEGY_ID = "smoke_test"
SMOKE_EXECUTION_MODE = "mt5-smoke"
SMOKE_EXIT_REASON = "smoke_test"
SMOKE_LOTS = 0.01
SMOKE_SL_DISTANCE_PIPS = 100.0  # distância do SL/TP (nunca devem ser tocados)
# Símbolos preferidos: XAUUSD primeiro (o instrumento do grupo, comprovado
# nas duas lanes). EURUSD fica como fallback — descoberto ao vivo (03/09) que
# o broker da lane 2 recusa EURUSD com retcode 10030 ("trade disabled for
# symbol") apesar de ter tick: preferir forex de spread pequeno aqui só
# fazia o smoke falhar no primeiro try.
SMOKE_SYMBOLS = ("XAUUSD", "EURUSD")


def smoke_opt_in() -> bool:
    """Opt-in explícito: sem `LANE2_SMOKE_ALLOW=1` o smoke nunca corre."""
    return os.getenv("LANE2_SMOKE_ALLOW", "0") == "1"


def smoke_cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("LANE2_SMOKE_COOLDOWN", "120") or 120))
    except (TypeError, ValueError):
        return 120.0


def _provider_trade_allowed(provider) -> bool:
    """AutoTrading do terminal da lane, por provider duck-typed.

    O RemoteProvider (lane 2) expõe `provider.trade_allowed`; o MT5Provider
    em processo (lane principal) não — lê do módulo (`terminal_info`), que é
    o estado real do terminal que vai aceitar/recusar as ordens.
    """
    ta = getattr(provider, "trade_allowed", None)
    if ta is not None:
        return bool(ta)
    try:
        mt5mod = getattr(provider, "_mt5", None)
        if mt5mod is None:
            return False
        ti = mt5mod.terminal_info()
        return bool(getattr(ti, "trade_allowed", False))
    except Exception:
        return False


def smoke_guard_error(engine, db=None, *, allow: Optional[bool] = None,
                      label: str = "") -> Optional[str]:
    """Guarda demo-only de UMA lane. Devolve None (pode correr) ou a razão.

    A ordem das verificações é deliberada: primeiro o opt-in, depois o modo
    de execução/ligação e por fim a conta DEMO (o teste nunca chega sequer a
    construir um pedido quando a conta é real). `db` é opcional — sem BD não
    há cooldown (scripts/tests podem forçar). `allow` sobrepõe o env.
    `label` (ex.: "main"/"tp2") prefixa a razão para o erro dizer que lane
    recusou.
    """
    def _msg(text: str) -> str:
        return f"[{label}] {text}" if label else text

    if allow is None:
        allow = smoke_opt_in()
    if not allow:
        return _msg("smoke test desligado — define LANE2_SMOKE_ALLOW=1 no .env "
                    "para ativar (só em contas demo)")
    if engine is None:
        return _msg("sem engine MT5 na lane (conta não configurada)")
    if getattr(engine, "dry_run", True):
        return _msg("lane não está em execução REAL (dry-run) — o smoke test "
                    "precisa do broker")
    provider = getattr(engine, "provider", None)
    if provider is None:
        return _msg("sem provider MT5 na lane")
    if getattr(provider, "execution_block_reason", ""):
        return _msg(f"lane bloqueada: {provider.execution_block_reason}")
    if not getattr(provider, "connected", False):
        return _msg("sem ligação ao terminal MT5 na lane")
    if not _provider_trade_allowed(provider):
        return _msg("AutoTrading OFF no terminal da lane — as ordens seriam "
                    "recusadas (retcode 10027)")
    try:
        acct = provider.account()
    except Exception as exc:
        return _msg(f"sem account_info da lane ({exc}) — não arrisca a conta")
    trade_mode = (acct or {}).get("trade_mode")
    # MT5: 0 = ACCOUNT_TRADE_MODE_DEMO, 1 = contest, 2 = real. Só demo passa.
    if trade_mode is None:
        return _msg("trade_mode desconhecido na conta da lane — smoke test é "
                    "demo-only e recusa sem confirmação de demo")
    if int(trade_mode) != 0:
        return _msg(f"conta da lane NÃO é demo (trade_mode={int(trade_mode)}) "
                    "— smoke test é demo-only")
    if db is not None:
        cooldown = smoke_cooldown_seconds()
        if cooldown > 0:
            try:
                last = float(db.all_config().get("lane2_smoke_last_ts", 0.0) or 0.0)
            except Exception:
                last = 0.0
            if last and time.time() - last < cooldown:
                wait = int(cooldown - (time.time() - last)) + 1
                return _msg(f"smoke test em cooldown — tenta dentro de {wait}s")
    return None


def _tick_live(engine, symbol: str) -> bool:
    """Tick bid/ask vivo para o símbolo NA lane (re-adiciona ao Market Watch
    e espera 1–2 segundos quando acabou de ser selecionado)."""
    provider = engine.provider
    try:
        meta = provider.meta(symbol)
    except Exception:
        meta = None
    if not meta:
        return False
    for attempt in range(2):
        try:
            provider.ensure_symbol(symbol)
            q = provider.quote(symbol)
        except Exception:
            q = None
        if q and float(q.get("bid") or 0.0) > 0 and float(q.get("ask") or 0.0) > 0:
            return True
        if attempt == 0:
            time.sleep(1.0)  # Market Watch acabou de o adicionar — dá tempo ao tick
    return False


def pick_smoke_symbol(engine) -> Optional[str]:
    """Primeiro símbolo da preferência com tick vivo numa lane (compat:
    smoke de lane única / diagnóstico)."""
    for symbol in SMOKE_SYMBOLS:
        if _tick_live(engine, symbol):
            return symbol
    return None


def pick_common_symbol(lanes: List[dict]) -> Optional[str]:
    """Símbolo com tick vivo em TODAS as lanes (XAUUSD primeiro — o único
    comprovadamente negociável nas duas; EURUSD só como fallback)."""
    for symbol in SMOKE_SYMBOLS:
        if all(_tick_live(ln["engine"], symbol) for ln in lanes):
            return symbol
    return None


def _make_smoke_signal(symbol: str, quote: dict, meta: dict, ts: int) -> Signal:
    """Sinal de 1 perna (TP1) centrado no preço atual, SL/TP a ±100 pips —
    níveis válidos para o broker mas longe o suficiente para nunca serem
    tocados entre a abertura e o fecho a mercado. Ids compactos
    (`s-<ts>-<rand>` / `t-<ts>-<rand>`): o wrapper do módulo MT5 recusa
    comments >28 chars (`-2 Invalid "comment" argument`) e o comment da
    ordem é `<trade_id>:leg1` — com 19 chars de id sobram 5 para o `:legN`.
    (Descoberto ao vivo 03/09: um `t-smoke-<epoch>-<rand>` de 30 chars dava
    None/`'?'` na ordem real — o smoke serviu mesmo de prova.)"""
    now = _now_iso()
    pip = float(meta.get("pip") or 0.0) or 0.01
    digits = int(meta.get("digits") or 0) or 2
    price = round(float(quote["ask"]), digits)   # buy a mercado no ask
    dist = round(100.0 * pip, digits)
    return Signal(
        id=f"s-{ts}-{_new_id()[:6]}",
        source="smoke-test",
        raw_text="",
        symbol=symbol,
        direction="buy",
        entry=price,
        stop_loss=round(price - dist, digits),
        take_profit_1=round(price + dist, digits),
        take_profit_2=0.0,
        status="pending",
        received_at=now,
        lots=SMOKE_LOTS,
    )


def _broker_open_positions(engine, trade, symbol: str) -> list:
    """Posições abertas no broker cujo comment pertence à trade — lidas
    DIRETAMENTE do módulo (`positions_get` sem cache).

    A leitura pós-fecho tem de ver o estado real do broker JÁ, senão acusa
    leftover fantasma: o `provider.positions()` do worker mantém uma cache de
    2s e devolve a posição que acabou de fechar (observado ao vivo 03/09)."""
    try:
        meta = engine.provider.meta(symbol)
        if not meta:
            return []
        live = engine.provider._mt5.positions_get(symbol=meta["mt5"]) or []
        return [p for p in live
                if trade.id in (getattr(p, "comment", "") or "")]
    except Exception:
        return []


def _broker_open_ticket(engine, trade, symbol: str) -> Optional[int]:
    """Ticket real da posição aberta no broker (lida por comment)."""
    try:
        pos = _broker_open_positions(engine, trade, symbol)[0]
        return int(getattr(pos, "ticket", 0) or 0) or None
    except (IndexError, TypeError, ValueError):
        return None


def _open_lane(engine, db, signal: Signal, lots: float) -> dict:
    """Abre a perna smoke numa lane. Devolve dict com `ok` + trade/ticket ou
    o erro — nunca levanta para o caller sem report."""
    acct = engine.provider.account() or {}
    login = acct.get("login")
    balance = float(acct.get("balance") or 10000.0)
    # s-<ts>-<rand> → t-<ts>-<rand> (curto); a 2ª lane leva sufixo próprio
    # (`-tp2`) para as DUAS trades do mesmo sinal terem ids distintos na BD
    # (o id entra no comment da ordem broker: ≤28 chars com o `:legN`)
    lane_suffix = "" if (engine.lane_id or "main") == "main" \
        else f"-{engine.lane_id}"
    trade_id = "t" + signal.id[1:] + lane_suffix
    try:
        trade = engine.open_trade(
            signal, risk_pct=1.0, balance=balance,
            trade_id=trade_id,
            lots_override=float(lots), max_legs=1,
            execution_meta={
                "execution_mode": SMOKE_EXECUTION_MODE,
                "lane_id": engine.lane_id or "main",
                "entry_style": "market",
                "market_price": signal.entry,
            },
        )
    except Exception as exc:
        return {"ok": False, "error": f"open_trade falhou: {exc}"}
    if trade.status != "open":
        # tudo o que o broker recusou no envio (10027, NO_TICK, worker caiu…)
        return {"ok": False, "error": trade.error or "ordem recusada na "
                "abertura (sem retcode)", "trade": trade}
    # marca a estratégia para a analytics nunca ler o teste como resultado
    # de uma estratégia real (A/B, realized pnl, resumos)
    try:
        db.set_trade_strategy(trade.id, SMOKE_STRATEGY_ID)
    except Exception:
        pass
    return {
        "ok": True, "trade": trade,
        "ticket": _broker_open_ticket(engine, trade, signal.symbol),
        "login": login,
    }


def _close_lane(engine, db, trade, symbol: str, lots: float) -> dict:
    """Fecha a perna smoke numa lane a mercado. Devolve dict com `ok`, os
    eventos e o estado final (row + leftover real no broker)."""
    try:
        events = engine.close_trade_at_market(trade.id, reason=SMOKE_EXIT_REASON)
    except Exception as exc:
        return {"ok": False, "error": f"close falhou: {exc}",
                "events": [], "leftover_open": True}
    closed = db.get_trade(trade.id)
    positions = db.positions_for_trade(trade.id)
    # confirma que não sobrou nada aberto no broker (segurança)
    leftover = bool(_broker_open_positions(engine, trade, symbol))
    return {
        "ok": not leftover,
        "events": events or [],
        "pnl": round(float(closed.pnl if closed else 0.0), 2),
        "close_price": (positions[0].close_price if positions else None),
        "retcodes": closed.retcodes if closed else None,
        "leftover_open": leftover,
    }


def run_smoke(engines: Dict[str, Any], db, *, symbol: Optional[str] = None,
              lots: float = SMOKE_LOTS) -> dict:
    """Abre e fecha uma trade real de 0.01 em CADA lane (guardas validadas
    pelo caller — esta função também as repete por segurança).

    As trades das lanes partilham o MESMO sinal (como um sinal real do
    grupo), por isso a mensagem WhatsApp dual-lane sai com as duas contas
    lado a lado. Se uma lane abrir e outra falhar, a aberta é fechada logo
    (rollback) — nunca deixa posição órfã. Nunca lança para o caller sem
    report: recusas do broker, worker caído ou símbolo sem tick voltam num
    dict com `ok=False` e o trace (retcodes/erro) exato.
    """
    lanes = [{"lane_id": lid, "engine": eng}
             for lid, eng in engines.items() if eng is not None]
    if not lanes:
        return {"ok": False, "error": "sem lanes configuradas para o smoke"}
    # guarda demo-only de CADA lane (com a label: o erro diz que lane recusou)
    for ln in lanes:
        guard = smoke_guard_error(ln["engine"], db, label=ln["lane_id"])
        if guard:
            return {"ok": False, "error": guard}
    symbol = symbol or pick_common_symbol(lanes)
    if not symbol:
        return {"ok": False,
                "error": "sem símbolo com tick nas duas lanes (EURUSD/XAUUSD "
                         "indisponíveis)"}
    try:
        # sinal partilhado, centrado na quote da 1ª lane (ordem das lanes)
        first = lanes[0]["engine"]
        meta = first.provider.meta(symbol)
        quote = first.provider.quote(symbol)
    except Exception as exc:
        return {"ok": False, "error": f"meta/quote de {symbol} falhou: {exc}"}
    if not meta or not quote:
        return {"ok": False, "error": f"sem meta/quote de {symbol} na lane"}
    signal = _make_smoke_signal(symbol, quote, meta, ts=int(time.time()))

    # 1) abre em todas as lanes; se uma falhar, rollback das já abertas
    opened: List[dict] = []
    for ln in lanes:
        res = _open_lane(ln["engine"], db, signal, lots)
        if not res.get("ok"):
            detail = {
                "lane_id": ln["lane_id"],
                "error": res.get("error"),
                "signal_id": signal.id,
                "retcodes": getattr(res.get("trade"), "retcodes", None),
            }
            return _rollback_report(opened, detail, symbol, db)
        opened.append({"lane_id": ln["lane_id"], "engine": ln["engine"],
                       "trade": res["trade"], "ticket": res["ticket"],
                       "login": res["login"]})

    # 2) fecha em todas; falha de fecho é reportada por lane (nunca silenciosa)
    lanes_result = []
    close_fail = None
    for item in opened:
        close = _close_lane(item["engine"], db, item["trade"], symbol, lots)
        entry = float(item["trade"].entry or 0.0)
        lanes_result.append({
            "lane_id": item["lane_id"],
            "login": item["login"],
            "trade_id": item["trade"].id,
            "signal_id": signal.id,
            "ticket": item["ticket"],
            "entry": entry,
            "pnl": close.get("pnl", 0.0),
            "close_price": close.get("close_price"),
            "reason": SMOKE_EXIT_REASON,
            "leftover_open": bool(close.get("leftover_open")),
            "events": close.get("events") or [],
            "retcodes": close.get("retcodes"),
        })
        if close.get("ok") is False:
            close_fail = item["lane_id"]
    total_pnl = round(sum(float(ln["pnl"] or 0.0) for ln in lanes_result), 2)
    report = {
        "ok": close_fail is None,
        "symbol": symbol, "direction": "buy", "lots": float(lots),
        "signal_id": signal.id,
        "lanes": lanes_result,
        "total_pnl": total_pnl,
        "cost_note": ("P&L ≈ custo real da volta (spread/slippage) a 0.01 lote "
                      "em cada conta — o valor fica em total_pnl"),
    }
    if close_fail:
        report["error"] = (f"fecho falhou na lane {close_fail} — verificar "
                           "posição no broker (leftover_open)")
    return report


def _rollback_report(opened: List[dict], failure: dict, symbol: str,
                     db=None) -> dict:
    """Uma lane abriu e outra falhou: fecha as abertas já (rollback) e reporta
    o que aconteceu — nunca deixa a posição órfã sem avisar."""
    rolled = []
    for item in opened:
        try:
            close = _close_lane(item["engine"], db, item["trade"], symbol,
                                SMOKE_LOTS)
            rolled.append({
                "lane_id": item["lane_id"], "trade_id": item["trade"].id,
                "pnl": close.get("pnl", 0.0),
                "leftover_open": bool(close.get("leftover_open")),
            })
        except Exception as exc:
            rolled.append({"lane_id": item["lane_id"],
                           "trade_id": item["trade"].id,
                           "error": f"rollback falhou: {exc}",
                           "leftover_open": True})
    return {
        "ok": False, "error": failure.get("error"),
        "failed_lane": failure.get("lane_id"),
        "signal_id": failure.get("signal_id"),
        "retcodes": failure.get("retcodes"),
        "rolled_back": rolled,
    }


async def notify_smoke(state, report: dict) -> None:
    """Dispacha os eventos do smoke como o tick faria: feed de `close`/
    `trade_closed` por lane + UMA notificação WhatsApp da lane 2 (o hook só
    publica no fecho da tp2 — mesma regra do tick).

    Espelha o ramo de eventos do `state.tick` (a dedup por sinal e a chamada
    ao hook são as mesmas) — só que para trades que o endpoint acabou de
    abrir/fechar, sem esperar pelo loop de mercado.
    """
    whatsapp_ev = None
    whatsapp_lane = None
    for lane in report.get("lanes") or []:
        lane_id = lane.get("lane_id")
        evs = lane.get("events") or []
        for ev in evs:
            if ev.get("type") == "close":
                pnl = float(ev.get("pnl") or 0.0)
                await state.notifier.send(
                    "trade_close",
                    f"[{lane_id}] {'✅' if pnl >= 0 else '❌'} Smoke: fechada "
                    f"perna {ev.get('leg')} {ev.get('symbol')} @ {ev.get('price')} "
                    f"| P&L {pnl:+.2f}",
                    lane=lane_id,
                )
            elif ev.get("type") == "trade_closed":
                pnl = float(ev.get("pnl") or 0.0)
                await state.notifier.send(
                    "trade_close",
                    f"[{lane_id}] 🧪 Smoke-test: trade "
                    f"{ev.get('symbol')} fechada | P&L total {pnl:+.2f} "
                    f"(0.01 lote, id {ev.get('trade_id', '')[-8:]})",
                    lane=lane_id,
                )
                whatsapp_ev, whatsapp_lane = ev, lane_id
    if whatsapp_ev is not None:
        # WhatsApp: o mesmo hook do tick (só a lane tp2 publica — se o smoke
        # da tp2 falhou, não há mensagem)
        await state._notify_whatsapp_trade_close(whatsapp_ev, lane_id=whatsapp_lane)


def smoke_report_lines(report: dict) -> List[str]:
    """Resumo legível do report (resposta ao operador / log do endpoint)."""
    if not report.get("ok"):
        return [f"❌ Smoke-test falhou: {report.get('error')}"]
    lanes = report.get("lanes") or []
    lines = [f"✅ Smoke-test OK — {report.get('symbol')} buy "
             f"{report.get('lots'):g} l por conta "
             f"({len(lanes)} lanes, P&L total {float(report.get('total_pnl') or 0.0):+.2f} €)"]
    for ln in lanes:
        status = "aberta! ⚠️" if ln.get("leftover_open") else "fechada"
        lines.append(
            f"  [{ln.get('lane_id')}] trade {ln.get('trade_id')} · ticket "
            f"{ln.get('ticket')} @ {ln.get('entry')} · {status} · "
            f"P&L {float(ln.get('pnl') or 0.0):+.2f} €"
        )
    return lines
