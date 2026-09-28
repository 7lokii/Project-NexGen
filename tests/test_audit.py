from app.db import DB
from app.backfill_trade_refusals import run_refusals_backfill
from app.execution.audit import daily_execution_audit
from app.models import Position, Signal, Trade


def test_daily_execution_audit_reconciles_db_rows_without_writing(tmp_path):
    db = DB(str(tmp_path / "audit.db"))
    try:
        db.set_config("balance", 1000.0)
        db.set_config("daily_loss_limit_pct", 3.0)
        db.insert_signal(Signal(
            id="s1", source="Golden Moves", raw_text="signal",
            symbol="XAUUSD", direction="buy", entry=4400.0,
            stop_loss=4390.0, take_profit_1=4403.0, take_profit_2=4406.0,
            status="executed", received_at="2026-08-31T10:00:00+00:00",
        ))
        db.insert_trade(Trade(
            id="t1", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
            take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed",
            opened_at="2026-08-31T10:01:00+00:00",
            closed_at="2026-08-31T10:02:00+00:00", pnl=-35.0,
            execution_mode="mt5-dry", strategy_id="nova_gestao",
        ))
        report = daily_execution_audit(db, day="2026-08-31")
        assert report["signals"]["executed"] == 1
        assert report["signals"]["ignored_messages"] == 0
        assert report["trades"]["closed"] == 1
        assert report["trades"]["pnl"] == -35.0
        assert report["daily_kill"]["triggered"] is True
        assert report["reconciliation"]["status"] == "db_only"
    finally:
        db.close()


def test_refusals_backfill_finds_phantom_and_applies_durable_trace(tmp_path):
    """O backfill semanal encontra fechos phantom (sem exit_reason/pnl 0.0,
    sem error) e grava o trace durável — idempotente, audit-only."""
    db = DB(str(tmp_path / "refusals.db"))
    try:
        db.insert_signal(Signal(
            id="s1", source="Golden Moves", raw_text="signal",
            symbol="XAUUSD", direction="buy", entry=4400.0,
            stop_loss=4390.0, take_profit_1=4403.0, take_profit_2=4406.0,
            status="executed", received_at="2026-08-31T10:00:00+00:00",
        ))
        # phantom: 2 pernas, fechada sem razão e pnl 0.0, sem error
        db.insert_trade(Trade(
            id="t-ph", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
            take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed",
            opened_at="2026-08-31T10:01:00+00:00",
            closed_at="2026-08-31T10:02:00+00:00", pnl=0.0,
            execution_mode="mt5-real", strategy_id="nova_gestao",
        ))
        for leg in (1, 2):
            db.insert_position(Position(
                id=f"p-ph-{leg}", trade_id="t-ph", leg=leg, symbol="XAUUSD",
                direction="buy", lots=0.01, entry=4400.0, stop_loss=4390.0,
                take_profit=4403.0 if leg == 1 else 4406.0,
                status="closed", opened_at="2026-08-31T10:01:00+00:00",
            ))
        # trade já com trace (error gravado) NUNCA é re-tocada
        db.insert_trade(Trade(
            id="t-ok", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
            take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed",
            opened_at="2026-08-31T10:03:00+00:00",
            closed_at="2026-08-31T10:04:00+00:00", pnl=0.0,
            error="ordem MT5 recusada pelo broker (retcodes [10027])",
            retcodes=[10027], execution_mode="mt5-real",
            strategy_id="nova_gestao", lane_id="tp2",
        ))
        res = run_refusals_backfill(db, apply=True)
        assert res["candidates"] == 1
        assert res["applied"] == 1
        row = db.conn.execute(
            "SELECT error, retcodes, auto_trading_off FROM trades WHERE id = 't-ph'"
        ).fetchone()
        assert "ordem MT5 recusada pelo broker" in row["error"]
        assert row["auto_trading_off"] == 0
        # idempotente: segunda corrida (o guard semanal seguinte) não encontra nada
        res2 = run_refusals_backfill(db, apply=True)
        assert res2["candidates"] == 0
        assert res2["applied"] == 0
    finally:
        db.close()


def test_refusals_backfill_mines_notifications_and_ignored_without_journal(tmp_path):
    """SEM journal (pré-journal/desligado): o backfill mina as notificações
    risk/system e a auditoria de ignoradas da hora — o 10027 liga a flag
    AutoTrading OFF (como o motor ao vivo) e a causa humana fica citada."""
    db = DB(str(tmp_path / "refusals-evidence.db"))
    try:
        db.insert_signal(Signal(
            id="s1", source="Golden Moves", raw_text="signal",
            symbol="XAUUSD", direction="buy", entry=4400.0,
            stop_loss=4390.0, take_profit_1=4403.0, take_profit_2=4406.0,
            status="executed", received_at="2026-08-31T10:00:00+00:00",
        ))
        db.insert_trade(Trade(
            id="t-ph", signal_id="s1", symbol="XAUUSD", direction="buy",
            entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
            take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
            status="closed",
            opened_at="2026-08-31T10:01:00+00:00",
            closed_at="2026-08-31T10:02:00+00:00", pnl=0.0,
            execution_mode="mt5-real", strategy_id="nova_gestao",
        ))
        for leg in (1, 2):
            db.insert_position(Position(
                id=f"p-ph-{leg}", trade_id="t-ph", leg=leg, symbol="XAUUSD",
                direction="buy", lots=0.01, entry=4400.0, stop_loss=4390.0,
                take_profit=4403.0 if leg == 1 else 4406.0,
                status="closed", opened_at="2026-08-31T10:01:00+00:00",
            ))
        # evidência da hora: notificações risk + auditoria de ignoradas
        db.insert_notification(
            "risk", "⚠️ Sinal s1 não executado na lane tp2 …",
            "2026-08-31T10:01:20+00:00",
        )
        db.insert_notification(
            "risk", "⛔ AutoTrading DESLIGADO no terminal? retcode 10027 …",
            "2026-08-31T10:01:30+00:00",
        )
        # sem pistas de recusa → nunca entra na evidência
        db.insert_notification(
            "system", "+50 pips hoje, bom dia",
            "2026-08-31T10:01:40+00:00",
        )
        db.record_ignored(
            "999", "Golden Moves", "Golden Moves",
            "ordem recusada na lane 2? vamos ver", "no_signal",
            "2026-08-31T10:01:35+00:00",
        )
        db.record_ignored(
            "1000", "Golden Moves", "Golden Moves", "bom dia pessoal",
            "no_signal", "2026-08-31T10:01:36+00:00",
        )
        res = run_refusals_backfill(db, apply=True)
        assert res["candidates"] == 1
        assert res["applied"] == 1
        row = db.conn.execute(
            "SELECT error, retcodes, auto_trading_off FROM trades WHERE id = 't-ph'"
        ).fetchone()
        assert "AutoTrading desligado" in row["error"]
        assert "10027" in row["retcodes"]
        assert row["auto_trading_off"] == 1
        # a causa humana (notificações/ignoradas) fica citada no trace
        assert "notificações:" in row["error"]
        assert "ignoradas:" in row["error"]
        assert "bom dia pessoal" not in row["error"]
    finally:
        db.close()


def _be_plus_fixture(db):
    """tp2_protect trade com BE+: leg 1 fecha no TP1, leg 2 no stop protegido
    (trail acima da entrada, lucro pequeno) — e uma trade de outra estratégia
    com exit_reason trail que NÃO deve contar."""
    db.insert_signal(Signal(
        id="sb1", source="Golden Circle", raw_text="sig",
        symbol="XAUUSD", direction="buy", entry=4400.0,
        stop_loss=4390.0, take_profit_1=4403.0, take_profit_2=4406.0,
        status="executed", received_at="2026-08-31T10:00:00+00:00",
    ))
    db.insert_trade(Trade(
        id="tb1", signal_id="sb1", symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
        take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed",
        opened_at="2026-08-31T10:01:00+00:00",
        closed_at="2026-08-31T10:40:00+00:00", pnl=2.4,
        execution_mode="mt5-real", strategy_id="tp2_protect", lane_id="main",
    ))
    db.insert_position(Position(
        id="p1", trade_id="tb1", leg=1, symbol="XAUUSD", direction="buy",
        lots=0.01, entry=4400.0, stop_loss=4390.0, take_profit=4403.0,
        status="closed", opened_at="2026-08-31T10:01:00+00:00",
        closed_at="2026-08-31T10:20:00+00:00", close_price=4403.0,
        pnl=3.0, exit_reason="tp1", strategy_id="tp2_protect",
        lane_id="main",
    ))
    db.insert_position(Position(
        id="p2", trade_id="tb1", leg=2, symbol="XAUUSD", direction="buy",
        lots=0.01, entry=4400.0, stop_loss=4400.4, take_profit=4406.0,
        status="closed", opened_at="2026-08-31T10:01:00+00:00",
        closed_at="2026-08-31T10:30:00+00:00", close_price=4400.4,
        pnl=0.4, exit_reason="trail", strategy_id="tp2_protect",
        lane_id="main",
    ))
    # ruído: trail noutra estratégia não conta
    db.insert_signal(Signal(
        id="sb2", source="Golden Circle", raw_text="sig",
        symbol="XAUUSD", direction="buy", entry=4400.0,
        stop_loss=4390.0, take_profit_1=4403.0, take_profit_2=4406.0,
        status="executed", received_at="2026-08-31T11:00:00+00:00",
    ))
    db.insert_trade(Trade(
        id="tb2", signal_id="sb2", symbol="XAUUSD", direction="buy",
        entry=4400.0, stop_loss=4390.0, take_profit_1=4403.0,
        take_profit_2=4406.0, risk_pct=1.0, lots_1=0.01, lots_2=0.01,
        status="closed",
        opened_at="2026-08-31T11:01:00+00:00",
        closed_at="2026-08-31T11:40:00+00:00", pnl=2.0,
        execution_mode="mt5-real", strategy_id="nova_gestao", lane_id="main",
    ))
    db.insert_position(Position(
        id="p3", trade_id="tb2", leg=2, symbol="XAUUSD", direction="buy",
        lots=0.01, entry=4400.0, stop_loss=4400.4, take_profit=4406.0,
        status="closed", opened_at="2026-08-31T11:01:00+00:00",
        closed_at="2026-08-31T11:30:00+00:00", close_price=4400.4,
        pnl=0.4, exit_reason="trail", strategy_id="nova_gestao",
        lane_id="main",
    ))


def test_be_plus_block_counts_protected_profit_exits(tmp_path):
    """BE+: só trails de tp2_protect com SL além da entrada contam —
    'trail' noutra estratégia é ruído."""
    db = DB(str(tmp_path / "bep.db"))
    try:
        _be_plus_fixture(db)
        report = daily_execution_audit(db, day="2026-08-31")
        bp = report["be_plus"]
        assert bp["exits_total"] == 1
        assert bp["exits_with_profit"] == 1
        assert bp["exits_with_loss"] == 0
        assert len(bp["stop_events"]) == 1
        assert bp["stop_events"][0]["trade_id"] == "tb1"
        assert bp["stop_events"][0]["stop_loss"] == 4400.4
    finally:
        db.close()


def test_be_plus_counterfactual_detects_tp2_reached_after_stop(tmp_path):
    """Com velas em que o TP2 é tocado DEPOIS do stop-out BE+, o
    contrafactual conta a trade como "teria corrido até TP2"."""
    from app.execution.audit import _be_plus_counterfactual
    db = DB(str(tmp_path / "bep2.db"))
    try:
        _be_plus_fixture(db)
        report = daily_execution_audit(db, day="2026-08-31")
        events = report["be_plus"]["stop_events"]
        assert events, "fixture deve produzir um stop BE+"
        # trade tb1: entrada 4400, TP2 4406. Stop-out às 10:30, trade fecha
        # 10:40. Velas M15: 10:15 (passa pelo TP2 DEPOIS do stop), 10:30, 10:45.
        # 10:15, 10:30, 10:45 UTC de 2026-08-31 (o TP2 é tocado às 10:30,
        # DEPOIS do stop-out à mesma hora — a janela [10:30, 10:45] apanha-o)
        base_ts = 1788172200 - 900
        candles = [
            {"ts": base_ts, "open": 4401, "high": 4402, "low": 4399, "close": 4401},
            {"ts": base_ts + 900, "open": 4402, "high": 4407, "low": 4401, "close": 4406},
            {"ts": base_ts + 1800, "open": 4406, "high": 4406, "low": 4400, "close": 4400},
        ]
        cf = _be_plus_counterfactual(candles, events)
        assert cf["checked"] == 1
        assert cf["kept_running_to_tp2"] == 1
        # sem velas: nunca inventa
        cf_empty = _be_plus_counterfactual(None, events)
        assert cf_empty == {"kept_running_to_tp2": None, "checked": 0}
    finally:
        db.close()


def test_avg_spread_pips_empty_and_filled(tmp_path):
    from datetime import datetime, timezone
    db = DB(str(tmp_path / "spr.db"))
    try:
        assert db.avg_spread_pips("XAUUSD") is None  # sem amostras
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.insert_spread_observation(now, "XAUUSD", 4400.0, 4400.34,
                                     0.34, 34.0)
        db.insert_spread_observation(now, "XAUUSD", 4400.0, 4400.44,
                                     0.44, 44.0)
        assert db.avg_spread_pips("XAUUSD", days=7) == 39.0
        assert db.avg_spread_pips("EURUSD") is None  # outro símbolo
    finally:
        db.close()


def test_be_buffer_drift_check_tracks_and_alerts(tmp_path):
    """Vigia do drift do spread: grava o histórico diário e alerta uma vez
    por dia quando a média 7d sai da banda [20, 50] pips."""
    import asyncio
    from datetime import datetime, timezone
    from app.execution.market import MarketSimulator
    from app.risk.engine import RiskEngine
    from app.state import BotState

    db = DB(str(tmp_path / "drift.db"))
    try:
        st = BotState(db, MarketSimulator(), RiskEngine(), None, None, None)
        sent = []

        class _N:
            async def send(self, kind, msg):
                sent.append((kind, msg))

        st.notifier = _N()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # média 34 pips — dentro da banda: track sem alerta
        db.insert_spread_observation(now, "XAUUSD", 4400.0, 4400.34, 0.34, 34.0)
        asyncio.run(st._check_be_buffer_drift())
        assert sent == []
        rows = db.conn.execute(
            "SELECT symbol, avg_spread_pips, buffer_pips FROM be_buffer_history"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["symbol"] == "XAUUSD"
        assert rows[0]["buffer_pips"] == 37.4  # 34 × 1.1

        # média 67 pips — acima da banda: um alerta, guard diário evita eco
        db.insert_spread_observation(now, "EURUSD2", 4400.0, 4401.67, 1.67, 67.0)
        asyncio.run(st._check_be_buffer_drift())
        assert len(sent) == 1 and sent[0][0] == "risk"
        assert "XAUUSD" not in sent[0][1]  # só o símbolo fora da banda
        asyncio.run(st._check_be_buffer_drift())
        assert len(sent) == 1  # guard diário: sem duplicado

        # switch off: nenhuma atividade
        st.config["be_buffer_adaptive"] = 0
        db.set_config("be_buffer_adaptive", 0)
        db.insert_spread_observation(now, "GBPUSD2", 1.2, 1.3, 0.1, 90.0)
        asyncio.run(st._check_be_buffer_drift())
        assert len(sent) == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM be_buffer_history WHERE symbol='GBPUSD2'"
        ).fetchone()[0] == 0
    finally:
        db.close()

    # spread_symbols: só símbolos com observações na janela
    db2 = DB(str(tmp_path / "spr2.db"))
    try:
        assert db2.spread_symbols(days=7) == []
        db2.insert_spread_observation(now, "XAUUSD", 4400.0, 4400.34, 0.34, 34.0)
        assert db2.spread_symbols(days=7) == ["XAUUSD"]
    finally:
        db2.close()
