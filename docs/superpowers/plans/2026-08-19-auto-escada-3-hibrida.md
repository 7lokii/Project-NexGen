# Auto-escada 3 híbrida — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar `auto_escada_3_hibrida` à corrida paper, ao backtest e à preparação MT5, fazendo com que a escada/trailing mecânicos nunca sejam piorados por um `protect` do grupo e produzindo um relatório de latência com pelo menos 20 eventos paper.

**Architecture:** Criar uma política pura de decisão de proteção em `app/execution/protection.py`, usada pelos três motores sem reescrever toda a máquina de SL/TP. O backtest devolve eventos de latência sem escrever na BD; paper e MT5 persistem os eventos em `protect_latency_log`. A nova estratégia é visível no dropdown, curva e painel da Fase 2, mas não substitui `nova_gestao` nem fica elegível para promoção automática à Fase 3.

**Tech Stack:** Python 3.14, FastAPI, SQLite via `DB.tx`, dataclasses da stdlib, backtest OHLC existente, PaperRace, MT5Engine, JavaScript vanilla existente e pytest.

## Global Constraints

- Implementar apenas o **Item 1**; os Itens 2–6 são planos separados.
- A híbrida entra imediatamente na corrida paper com sinais reais, mas não altera a principal `nova_gestao` e não envia ordens MT5 enquanto não for explicitamente promovida.
- **Sem look-ahead:** a decisão usa somente o estado de ticks/candles já processados; a vela atual nunca pode criar o pico/SL que ela própria atravessa.
- Um `protect` nunca pode piorar um SL mecânico já existente; para compras escolhe-se o SL mais alto e para vendas o mais baixo.
- A perna 1 só é fechada pelo `protect` se a saída no preço conhecido do evento não for pior que a proteção automática; caso contrário, a ação fica `leg1_action="skipped"`.
- Não apagar sinais, trades, estratégias ou dados existentes; a nova estratégia não entra em `HIDDEN_STRATEGIES` nem em `MENU_HIDDEN_STRATEGIES`.
- O backtest continua sem efeitos laterais na BD; o paper/MT5 podem persistir telemetria operacional.
- Usar apenas bibliotecas já existentes; não adicionar dependências.
- Atualizar `AGENTS.md`, `docs/TECHNICAL.md`, `docs/ESTRATEGIAS.md`, `docs/PUBLIC.md` e `docs/CHANGELOG.md` no mesmo turno.
- A suite de referência atual é de 255 testes; atualizar a contagem apenas depois de a suite final passar.
- O ficheiro `docs/Melhorias/01-item-01.md` só pode ser removido depois de existirem pelo menos 20 eventos `protect` paper e todos os critérios de aceitação estarem verificados.

## File Map

- **Create:** `app/execution/protection.py` — decisão pura de merge do SL, política de fecho da perna 1 e cálculo da telemetria.
- **Modify:** `app/strategies.py` — registo de `auto_escada_3_hibrida`; não alterar a assinatura de `_strat` porque os parâmetros atuais já cobrem a híbrida.
- **Modify:** `app/db.py` — tabela, migração e consultas da telemetria `protect_latency_log`.
- **Modify:** `app/backtest.py` — replay gerido com auto-gestão e retorno de eventos de latência sem escrita.
- **Modify:** `app/analysis.py` — passagem dos parâmetros auto ao ramo gerido e relatório de latência de backtest/live.
- **Modify:** `app/execution/paper.py` — proteção monotónica, timestamps de auto-proteção e persistência dos eventos paper.
- **Modify:** `app/execution/mt5.py` — exposição do estado auto e aplicação segura de `protect` quando a híbrida for promovida.
- **Modify:** `app/execution/engine.py` — caminho equivalente para a execução simulada da principal, usado pelos testes/API sem broker.
- **Modify:** `app/state.py` — encaminhamento do `protect` híbrido, snapshot/painel da corrida e endpoint report wrapper.
- **Modify:** `app/main.py` — `GET /api/analysis/protect_latency`.
- **Modify:** `app/static/index.html` — item do dropdown, candidato híbrido no painel Fase 2 e bloco do relatório.
- **Modify:** `app/static/teste.html` — item do dropdown da página de teste.
- **Modify:** `app/static/common.js` — ordem de navegação, cores/legenda e refresh do relatório.
- **Modify:** `tests/test_protection.py` — testes unitários da política pura.
- **Modify:** `tests/test_backtest.py` — combinação auto + gestão e proteção sem look-ahead.
- **Modify:** `tests/test_strategy.py` — registo/visibilidade/conta paper da híbrida.
- **Modify:** `tests/test_api.py` — endpoint, payload e curva da estratégia híbrida.
- **Modify:** `tests/test_mt5.py` — caminho MT5 fake quando uma híbrida é promovida em teste isolado.
- **Modify:** `tests/test_frontend.py` — contrato estático do dropdown/painel/relatório.
- **Modify:** `docs/ESTRATEGIAS.md`, `AGENTS.md`, `docs/TECHNICAL.md`, `docs/PUBLIC.md`, `docs/CHANGELOG.md` — documentação e resultados.
- **Keep:** `docs/Melhorias/01-item-01.md` até à validação paper de 20 eventos.

---

### Task 1: Criar a política pura de proteção monotónica

**Files:**
- Create: `app/execution/protection.py`
- Create: `tests/test_protection.py`

**Interfaces:**
- Produces `group_protect_sl(direction: str, entry: float, stop_loss: float, tp1: float) -> float` com a fórmula atual do grupo.
- Produces `ProtectDecision` e `decide_protect(...)` para os três motores.
- `group_protect_sl`/`decide_protect` não acessam DB, mercado, relógio global nem rede.

- [ ] **Step 1: Escrever os testes falhando para compra, venda, leg 1 e latência**

```python
from app.execution.protection import decide_protect, group_protect_sl


def test_group_protect_sl_preserves_existing_buffer_formula():
    assert group_protect_sl(
        direction="buy", entry=4400.0, stop_loss=4392.0, tp1=4402.0,
    ) == 4400.8
    assert group_protect_sl(
        direction="sell", entry=4400.0, stop_loss=4408.0, tp1=4398.0,
    ) == 4399.2


def test_buy_group_sl_cannot_lower_mechanical_sl():
    result = decide_protect(
        direction="buy",
        auto_sl=4404.0,
        group_sl=4401.0,
        entry=4400.0,
        tp1=4402.0,
        price_at_event=4403.0,
        leg1_open=True,
        auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:05:00+00:00",
        pip_size=0.01,
    )
    assert result.applied_sl == 4404.0
    assert result.winner == "auto"
    assert result.leg1_action == "skipped"
    assert result.delta_pips == 300.0
    assert result.delta_seconds == 300.0


def test_sell_group_sl_cannot_raise_mechanical_sl():
    result = decide_protect(
        direction="sell",
        auto_sl=4396.0,
        group_sl=4399.0,
        entry=4400.0,
        tp1=4398.0,
        price_at_event=4397.0,
        leg1_open=True,
        auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:02:00+00:00",
        pip_size=0.01,
    )
    assert result.applied_sl == 4396.0
    assert result.winner == "auto"
    assert result.leg1_action == "skipped"
    assert result.delta_seconds == 120.0


def test_protect_can_close_leg1_only_after_tp1_is_known():
    result = decide_protect(
        direction="buy", auto_sl=4400.0, group_sl=4401.0,
        entry=4400.0, tp1=4402.0, price_at_event=4402.0,
        leg1_open=True, auto_protection_at=None,
        group_received_at="2026-08-19T10:00:00+00:00", pip_size=0.01,
    )
    assert result.applied_sl == 4401.0
    assert result.winner == "group"
    assert result.leg1_action == "closed"
    assert result.status == "auto_not_triggered"


def test_protect_does_not_close_already_closed_leg1():
    result = decide_protect(
        direction="buy", auto_sl=4402.0, group_sl=4401.0,
        entry=4400.0, tp1=4402.0, price_at_event=4401.0,
        leg1_open=False, auto_protection_at="2026-08-19T10:00:00+00:00",
        group_received_at="2026-08-19T10:01:00+00:00", pip_size=0.01,
    )
    assert result.leg1_action == "already_closed"
    assert result.applied_sl == 4402.0
```

- [ ] **Step 2: Executar apenas os testes novos para confirmar que falham**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_protection.py -q
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` because `app/execution/protection.py` ainda não existe.

- [ ] **Step 3: Criar a implementação mínima da política**

`app/execution/protection.py` deve conter esta interface e comportamento:

```python
"""Pure, side-effect-free protect merge policy shared by backtest/paper/MT5."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


Winner = Literal["auto", "group", "tie", "none"]
Leg1Action = Literal["closed", "skipped", "already_closed"]


def group_protect_sl(*, direction: str, entry: float,
                     stop_loss: float, tp1: float) -> float:
    risk = abs(entry - stop_loss)
    buffer = (min(max(risk * 0.1, risk * 0.01), abs(tp1 - entry))
              if risk > 0 else 0.0)
    return entry + buffer if direction == "buy" else entry - buffer


@dataclass(frozen=True)
class ProtectDecision:
    applied_sl: Optional[float]
    winner: Winner
    delta_pips: Optional[float]
    delta_seconds: Optional[float]
    leg1_action: Leg1Action
    status: Literal["ok", "auto_not_triggered"]


def _more_protective(direction: str, left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right) if direction == "buy" else min(left, right)


def _winner(direction: str, auto_sl: Optional[float], group_sl: Optional[float]) -> Winner:
    if auto_sl is None and group_sl is None:
        return "none"
    if auto_sl is None:
        return "group"
    if group_sl is None:
        return "auto"
    if abs(auto_sl - group_sl) <= 1e-12:
        return "tie"
    if direction == "buy":
        return "auto" if auto_sl > group_sl else "group"
    return "auto" if auto_sl < group_sl else "group"


def _seconds_between(start: Optional[str], end: str) -> Optional[float]:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def decide_protect(*, direction: str, auto_sl: Optional[float],
                   group_sl: Optional[float], entry: float, tp1: float,
                   price_at_event: Optional[float], leg1_open: bool,
                   auto_protection_at: Optional[str],
                   group_received_at: str, pip_size: float) -> ProtectDecision:
    applied = _more_protective(direction, auto_sl, group_sl)
    winner = _winner(direction, auto_sl, group_sl)
    delta_pips = (abs(group_sl - auto_sl) / pip_size
                  if auto_sl is not None and group_sl is not None and pip_size else None)
    leg1_action: Leg1Action
    if not leg1_open:
        leg1_action = "already_closed"
    else:
        reached_tp1 = False
        if price_at_event is not None and tp1:
            reached_tp1 = price_at_event >= tp1 if direction == "buy" else price_at_event <= tp1
        leg1_action = "closed" if reached_tp1 else "skipped"
    return ProtectDecision(
        applied_sl=applied,
        winner=winner,
        delta_pips=round(delta_pips, 4) if delta_pips is not None else None,
        delta_seconds=_seconds_between(auto_protection_at, group_received_at),
        leg1_action=leg1_action,
        status="ok" if auto_protection_at else "auto_not_triggered",
    )
```

- [ ] **Step 4: Executar os testes unitários e confirmar PASS**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_protection.py -q
```

Expected: `5 passed`.

- [ ] **Step 5: Checkpoint de revisão**

Rever o diff de `app/execution/protection.py` e `tests/test_protection.py`. Não fazer commit neste checkout sem autorização explícita; num checkout Git normal, o checkpoint sugerido é:

```bash
git add app/execution/protection.py tests/test_protection.py
git commit -m "feat: add monotonic protect policy"
```

---

### Task 2: Persistir e consultar a telemetria de latência

**Files:**
- Modify: `app/db.py:SCHEMA`, `DB._migrate` e métodos de persistência
- Modify: `tests/test_api.py` ou criar `tests/test_protection.py` com uma DB temporária

**Interfaces:**
- Produces `DB.insert_protect_latency(entry: dict) -> int`.
- Produces `DB.protect_latency_rows(strategy_id: str = "", engine: str = "", limit: int = 500) -> list[dict]`.
- Produces `DB.protect_latency_stats(strategy_id: str = "", engine: str = "", limit: int = 500) -> dict`.

- [ ] **Step 1: Escrever o teste falhando da tabela e das estatísticas**

```python
from app.db import DB


def test_protect_latency_storage_and_stats(tmp_path):
    db = DB(str(tmp_path / "latency.db"))
    try:
        row_id = db.insert_protect_latency({
            "engine": "paper", "strategy_id": "auto_escada_3_hibrida",
            "signal_id": "s1", "trade_id": "pt-1", "message_id": "m1",
            "symbol": "XAUUSD", "direction": "buy",
            "auto_protection_at": "2026-08-19T10:00:00+00:00",
            "group_received_at": "2026-08-19T10:00:08+00:00",
            "auto_sl": 4404.0, "group_sl": 4405.0, "applied_sl": 4405.0,
            "winner": "group", "delta_pips": 100.0,
            "delta_seconds": 8.0, "leg1_action": "already_closed",
            "status": "ok", "created_at": "2026-08-19T10:00:08+00:00",
        })
        assert row_id > 0
        rows = db.protect_latency_rows(strategy_id="auto_escada_3_hibrida")
        assert rows[0]["engine"] == "paper"
        stats = db.protect_latency_stats(strategy_id="auto_escada_3_hibrida")
        assert stats == {
            "count": 1, "auto_wins": 0, "group_wins": 1, "ties": 0,
            "auto_not_triggered": 0, "mean_seconds": 8.0,
            "median_seconds": 8.0, "p90_seconds": 8.0,
            "max_seconds": 8.0, "mean_pips": 100.0, "p90_pips": 100.0,
            "max_pips": 100.0,
        }
    finally:
        db.close()
```

- [ ] **Step 2: Confirmar que o teste falha antes da tabela/métodos**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_protection.py::test_protect_latency_storage_and_stats -q
```

Expected: FAIL because `insert_protect_latency` is undefined.

- [ ] **Step 3: Adicionar o schema, índice e métodos SQLite**

Adicionar ao `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS protect_latency_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    signal_id TEXT,
    trade_id TEXT,
    message_id TEXT,
    symbol TEXT,
    direction TEXT,
    auto_protection_at TEXT,
    group_received_at TEXT NOT NULL,
    auto_sl REAL,
    group_sl REAL,
    applied_sl REAL,
    winner TEXT NOT NULL,
    delta_pips REAL,
    delta_seconds REAL,
    leg1_action TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_protect_latency_strategy
    ON protect_latency_log(strategy_id, group_received_at);
CREATE INDEX IF NOT EXISTS idx_protect_latency_engine
    ON protect_latency_log(engine, group_received_at);
```

`insert_protect_latency` deve usar `INSERT` parametrizado dentro de `DB.tx`. `protect_latency_rows` deve filtrar apenas pelos parâmetros preenchidos e ordenar por `group_received_at DESC`. `protect_latency_stats` deve ignorar valores `NULL` nos quantis, usar `statistics.median`, calcular p90 com o mesmo índice usado por `edit_delay_stats` e devolver sempre as chaves do teste, com zeros quando não houver linhas.

- [ ] **Step 4: Executar o teste de persistência**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_protection.py::test_protect_latency_storage_and_stats -q
```

Expected: `1 passed`.

---

### Task 3: Registar a híbrida e abrir a conta paper imediatamente

**Files:**
- Modify: `app/strategies.py` na função `_strat` apenas se for necessário para documentação; adicionar entrada em `STRATEGIES` depois de `auto_escada_3`.
- Modify: `tests/test_strategy.py`
- Modify: `app/static/index.html`, `app/static/teste.html`, `app/static/common.js`

**Interfaces:**
- `get_strategy("auto_escada_3_hibrida")` devolve os parâmetros congelados.
- `state.paper_status()` e `GET /api/paper/history` incluem a híbrida sem mudanças especiais, porque ela não pertence aos sets hidden.

- [ ] **Step 1: Escrever testes falhando de registo e paper**

```python
from app.strategies import HIDDEN_STRATEGIES, MENU_HIDDEN_STRATEGIES, get_strategy


def test_hybrid_strategy_is_visible_and_configured():
    s = get_strategy("auto_escada_3_hibrida")
    assert "auto_escada_3_hibrida" not in HIDDEN_STRATEGIES
    assert "auto_escada_3_hibrida" not in MENU_HIDDEN_STRATEGIES
    assert s["max_legs"] == 3
    assert s["stair_levels"] == 3
    assert s["stair_from"] == 1
    assert s["trail_factor"] == 2.0
    assert s["def_window"] == 10
    assert s["def_trail"] == 1.5
    assert s["def_time_stop"] == 60
    assert s["manage_kinds"] == {"protect"}


def test_hybrid_gets_own_paper_account(tmp_path):
    from app.db import DB
    from app.execution.market import MarketSimulator
    from app.execution.paper import PaperRace
    from app.models import Signal
    db = DB(str(tmp_path / "paper.db"))
    try:
        race = PaperRace(db, MarketSimulator(seed=1))
        signal = Signal(
            id="s-hybrid", source="test", raw_text="", symbol="EURUSD",
            direction="buy", entry=1.085, stop_loss=1.082,
            take_profit_1=1.089, take_profit_2=1.092,
            status="executed", received_at="2026-08-19T10:00:00+00:00",
        )
        opened = race.open_for_signal(signal, 1.0,
                                      strategy_ids=["auto_escada_3_hibrida"],
                                      daily_stop_pct=0)
        assert len(opened) == 1
        assert db.paper_balance("auto_escada_3_hibrida") == 1000.0
    finally:
        db.close()
```

- [ ] **Step 2: Confirmar a falha de registo**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_strategy.py::test_hybrid_strategy_is_visible_and_configured -q
```

Expected: FAIL because the strategy id is absent.

- [ ] **Step 3: Adicionar a entrada do registo**

Adicionar a `STRATEGIES`:

```python
"auto_escada_3_hibrida": _strat(
    "auto_escada_3_hibrida", "Auto 3 TPs + escada + protect híbrido",
    "3 pernas + escada/trailing mecânicos sempre ativos. Honra apenas "
    "protect do grupo quando o SL proposto for mais protetor que o SL "
    "mecânico; nunca piora a auto-gestão e não é candidata automática à "
    "Fase 3.",
    max_legs=3, manage_kinds={"protect"}, stair_levels=3, stair_from=1,
    confirm_filter=False, trail_factor=2.0,
    def_window=10, def_trail=1.5, def_time_stop=60,
),
```

Não adicionar o id aos sets `REMOVED_STRATEGIES`, `HIDDEN_STRATEGIES` ou `MENU_HIDDEN_STRATEGIES`.

- [ ] **Step 4: Atualizar navegação visível sem hardcode de PF**

Adicionar o item imediatamente depois de `auto_escada_3` em ambos `index.html` e `teste.html`:

```html
<a class="term-menu-item" data-page="teste-auto_escada_3_hibrida"
   href="/teste?s=auto_escada_3_hibrida">
  <span class="cur">▸</span>Auto 3 TPs + escada + protect híbrido
  <span class="sub">paper · proteção mecânica + grupo</span>
  <span class="best"></span>
</a>
```

Adicionar `"teste-auto_escada_3_hibrida"` ao `TERM_ORDER` em `common.js`. A página de teste deve receber os dados do `/api/strategies`, sem duplicar parâmetros no frontend. O PF histórico será adicionado no item de dashboard específico da estratégia apenas depois de o endpoint de comparação das visíveis ser tratado num plano separado; este item não deve inventar uma segunda fonte de números.

- [ ] **Step 5: Confirmar a conta paper e a suite de estratégia**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_strategy.py::test_hybrid_strategy_is_visible_and_configured tests/test_strategy.py::test_hybrid_gets_own_paper_account -q
```

Expected: `2 passed`.

---

### Task 4: Fazer o backtest gerido suportar auto-gestão + protect

**Files:**
- Modify: `app/backtest.py:simulate_signal_managed`
- Modify: `app/analysis.py:run_analysis` e `run_month_sim`
- Modify: `tests/test_backtest.py`

**Interfaces:**
- Expandir `simulate_signal_managed` sem quebrar os chamadores existentes:

```python
def simulate_signal_managed(
    signal: Signal,
    candles: List[dict],
    mgmt_events: List[tuple],
    risk_pct: float,
    balance: float,
    delay_seconds: int = 60,
    spread_pips: float = 1.0,
    slippage_pips: float = 0.5,
    commission_per_lot: float = 3.5,
    lots_override: Optional[float] = None,
    max_legs: Optional[int] = None,
    exit_at_tp: Optional[int] = None,
    manage_kinds: Optional[Set[str]] = None,
    trail_factor: Optional[float] = None,
    time_stop_minutes: Optional[int] = None,
    stair_levels: Optional[int] = None,
    stair_from: int = 1,
    confirm_filter: bool = False,
    atr_k: Optional[float] = None,
    atr_period: int = 14,
    def_window: Optional[int] = None,
    def_trail: Optional[float] = None,
    def_time_stop: Optional[int] = None,
    def_state: Optional[dict] = None,
    strategy_id: str = "",
) -> dict:
```

O retorno mantém `outcome`, `pnl`, `legs_pnl`, `mgmt_applied` e acrescenta `protect_latency: list[dict]`. A função deve usar o mesmo loop cronológico da auto-gestão; não pode simular primeiro a auto e depois aplicar gestão num segundo replay.

- [ ] **Step 1: Escrever testes falhando de proteção híbrida no backtest**

```python
from app.backtest import simulate_signal_managed


def test_managed_hybrid_group_protect_cannot_worsen_trail():
    sig = make_signal(received_at="2026-08-17T10:00:00+00:00")
    cs = candles(
        highs=[1.0890, 1.0910, 1.0900],
        lows=[1.0860, 1.0895, 1.0895],
        start_ts=BASE + 60,
        step=900,
    )
    # TP1/escada na primeira vela cria SL 1.0890; o evento chega na terceira
    # vela, cujo low não atravessa esse SL. O protect propõe um nível inferior.
    event_ts = cs[2]["ts"]
    result = simulate_signal_managed(
        sig, cs,
        [(event_ts, "protect", False, None)],
        1.0, 10_000, 60,
        manage_kinds={"protect"}, trail_factor=2.0,
        stair_levels=3, stair_from=1,
        strategy_id="auto_escada_3_hibrida",
    )
    assert result["protect_latency"]
    assert result["protect_latency"][0]["winner"] == "auto"
    assert result["protect_latency"][0]["applied_sl"] >= result["protect_latency"][0]["auto_sl"]


def test_managed_hybrid_does_not_use_current_candle_close_for_protect():
    sig = make_signal(received_at="2026-08-17T10:00:00+00:00")
    cs = [
        {"ts": BASE + 60, "open": 1.0870, "high": 1.0880,
         "low": 1.0860, "close": 1.0870},
        {"ts": BASE + 960, "open": 1.0870, "high": 1.0925,
         "low": 1.0860, "close": 1.0920},
    ]
    # O high/close da vela do evento é informação futura nesse instante;
    # prev_close continua abaixo do TP1, logo a perna 1 não é fechada pelo
    # protect antes da vela ser processada.
    result = simulate_signal_managed(
        sig, cs, [(cs[1]["ts"], "protect", False, None)],
        1.0, 10_000, 60, manage_kinds={"protect"},
        trail_factor=2.0, stair_levels=3, stair_from=1,
        strategy_id="auto_escada_3_hibrida",
    )
    assert result["protect_latency"][0]["leg1_action"] == "skipped"
```

- [ ] **Step 2: Confirmar falha antes de integrar o loop combinado**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_backtest.py -k "hybrid" -q
```

Expected: FAIL because the managed function does not accept auto parameters or return `protect_latency`.

- [ ] **Step 3: Refatorar o loop gerido para manter uma única cronologia**

Usar a implementação existente de `simulate_signal_managed` como base, mas mover para o mesmo loop as operações que hoje existem em `simulate_signal`: TP1/BE, escada, trailing, ATR, confirmação e time stop. Em cada candle/evento:

```python
# ordem obrigatória dentro do replay:
# 1. verificar SL já cruzado com o estado anterior;
# 2. processar mensagens cujo timestamp já chegou usando prev_close;
# 3. aplicar TP/BE/escada/trailing com o pico anterior;
# 4. atualizar o pico/vale apenas no fim da vela.
```

Quando `kind == "protect"` e `strategy_id == "auto_escada_3_hibrida"`:

1. calcular o SL do grupo com a fórmula existente de `protect`;
2. obter `auto_sl` da perna aberta e `auto_protection_at` do estado mecânico;
3. chamar `decide_protect` com `price_at_event=prev_close`;
4. aplicar somente `decision.applied_sl` às pernas restantes;
5. fechar a perna 1 somente quando `decision.leg1_action == "closed"`;
6. anexar ao retorno um dicionário com os campos da `ProtectDecision`, `signal_id`, `strategy_id`, `engine="backtest"`, `trade_id=None` e `message_id=None`.

Quando a estratégia não for híbrida, preservar o comportamento atual de `simulate_signal_managed`.

- [ ] **Step 4: Passar os parâmetros da estratégia pelo `run_analysis`**

No ramo `manage=True` de `run_analysis`, passar explicitamente `trail_factor`, `time_stop_minutes`, `stair_levels`, `stair_from`, `confirm_filter`, `atr_k`, `atr_period`, `def_window`, `def_trail`, `def_time_stop`, `def_state` e `strategy_id`. O ramo `manage=False` continua usando `simulate_signal`. Ao montar o payload final, agregar os eventos retornados:

```python
protect_latency = []
for result in results:
    protect_latency.extend(result.get("protect_latency") or [])
payload["protect_latency"] = protect_latency
```

Atualizar a docstring que hoje diz que auto-gestão só é usada quando `manage=False`; a híbrida é a exceção documentada porque combina gestão filtrada com auto-gestão.

- [ ] **Step 5: Executar os testes de backtest existentes e novos**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_backtest.py -q
```

Expected: todos os testes existentes e os 2 testes híbridos PASS; nenhum teste antigo de `simulate_signal_managed` muda de resultado.

---

### Task 5: Aplicar a proteção e telemetria na corrida paper

**Files:**
- Modify: `app/execution/paper.py`
- Modify: `tests/test_strategy.py` ou `tests/test_api.py`

**Interfaces:**
- `PaperRace._auto_state` passa a guardar `auto_sl_by_leg` e `auto_protection_at_by_leg`.
- `PaperRace._record_protect_latency(trade, decision, ...)` persiste uma linha em `DB` com `engine="paper"`.

- [ ] **Step 1: Escrever teste paper falhando para protect melhor/pior**

```python
import os

from app.db import DB
from app.execution.market import MarketSimulator
from app.execution.paper import PaperRace
from app.models import Signal


def _paper_hybrid_fixture(tmp_path):
    db = DB(str(tmp_path / "paper-hybrid.db"))
    market = MarketSimulator(seed=1)
    market.prices["EURUSD"] = 1.0850
    race = PaperRace(db, market)
    signal = Signal(
        id="s-paper-hybrid", source="test", raw_text="", symbol="EURUSD",
        direction="buy", entry=1.0850, stop_loss=1.0820,
        take_profit_1=1.0890, take_profit_2=1.0920,
        take_profit_3=1.0950, status="executed",
        received_at="2026-08-19T10:00:00+00:00",
    )
    return db, market, race, signal


def test_paper_hybrid_protect_is_monotonic_and_logged(tmp_path):
    db, market, race, signal = _paper_hybrid_fixture(tmp_path)
    try:
        trades = race.open_for_signal(
            signal, 1.0, strategy_ids=["auto_escada_3_hibrida"], daily_stop_pct=0,
        )
        assert len(trades) == 1
        market.prices[signal.symbol] = signal.take_profit_1
        race.tick(1.0)  # TP1, escada e estado auto
        before = db.paper_positions_for_trade(trades[0].id)
        before_sl = min(p.stop_loss for p in before if p.leg != 1)
        race.apply_management(signal.id, "protect", msg_id="protect-1")
        after = db.paper_positions_for_trade(trades[0].id)
        assert min(p.stop_loss for p in after if p.leg != 1) >= before_sl
        rows = db.protect_latency_rows(
            strategy_id="auto_escada_3_hibrida", engine="paper"
        )
        assert len(rows) == 2
        assert all(r["engine"] == "paper" for r in rows)
    finally:
        db.close()
        try:
            os.remove(str(tmp_path / "paper-hybrid.db"))
        except FileNotFoundError:
            pass
```

- [ ] **Step 2: Confirmar falha**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_strategy.py -k "paper_hybrid" -q
```

Expected: FAIL because the paper handler does not write latency rows and does not merge the SL.

- [ ] **Step 3: Registar o estado mecânico quando escada/trailing alterarem o SL**

No caminho de TP1, escada e trailing de `PaperRace._update_trade`, depois de `move_paper_position_sl`, guardar o primeiro timestamp ISO e o SL por perna:

```python
state.setdefault("auto_sl_by_leg", {})[p.leg] = p.stop_loss
state.setdefault("auto_protection_at_by_leg", {}).setdefault(p.leg, _now_iso())
```

Não atualizar esses timestamps com o preço da mesma tick antes dos fills; o estado deve ser atualizado depois do bloco de fills, tal como o pico do trailing.

- [ ] **Step 4: Integrar `decide_protect` apenas na híbrida**

Em `PaperRace._apply_one`, quando `kind == "protect"` e `trade["strategy_id"] == "auto_escada_3_hibrida"`, o bloco deve seguir esta forma:

```python
group_sl = group_protect_sl(
    direction=trade["direction"], entry=trade["entry"],
    stop_loss=trade["stop_loss"], tp1=trade.get("take_profit_1") or trade["entry"],
)
state = self._auto.get(trade["id"], {})
group_received_at = _now_iso()
leg1 = positions.get(1)
leg1_decision = decide_protect(
    direction=trade["direction"],
    auto_sl=leg1.stop_loss if leg1 else None,
    group_sl=group_sl, entry=trade["entry"],
    tp1=trade.get("take_profit_1") or 0.0,
    price_at_event=price, leg1_open=bool(leg1 and leg1.status == "open"),
    auto_protection_at=state.get("auto_protection_at_by_leg", {}).get(1),
    group_received_at=group_received_at, pip_size=_pip_size(trade["symbol"]),
)
for pos in open_legs:
    if pos.leg == 1:
        continue
    decision = decide_protect(
        direction=trade["direction"],
        auto_sl=state.get("auto_sl_by_leg", {}).get(pos.leg, pos.stop_loss),
        group_sl=group_sl, entry=trade["entry"],
        tp1=trade.get("take_profit_1") or 0.0,
        price_at_event=price, leg1_open=bool(leg1 and leg1.status == "open"),
        auto_protection_at=state.get("auto_protection_at_by_leg", {}).get(pos.leg),
        group_received_at=group_received_at, pip_size=_pip_size(trade["symbol"]),
    )
    if decision.applied_sl is not None and decision.applied_sl != pos.stop_loss:
        self.db.move_paper_position_sl(pos.id, decision.applied_sl)
        pos.stop_loss = decision.applied_sl
    self._record_protect_latency(trade, pos, decision, msg_id)
if leg1 and leg1.status == "open" and leg1_decision.leg1_action == "closed":
    events.append(self._close(trade, leg1, price, "protect", msg_id))
```

O código real deve calcular `group_received_at` uma única vez antes do loop e usar essa mesma string em todas as linhas. `leg1_action` deve ser calculada separadamente quando não existirem pernas restantes. As estratégias existentes (`nova_gestao`, `tp2_protect`) mantêm exatamente o comportamento atual.

As estratégias existentes (`nova_gestao`, `tp2_protect`) mantêm exatamente o comportamento atual.

- [ ] **Step 5: Executar os testes paper e a suite de estratégia**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_strategy.py -q
```

Expected: todos os testes anteriores + o teste híbrido PASS.

---

### Task 6: Preparar o caminho MT5 sem ativar a conta principal

**Files:**
- Modify: `app/execution/mt5.py`
- Modify: `app/execution/engine.py`
- Modify: `app/state.py`
- Modify: `tests/test_mt5.py`, usando o broker fake existente

**Interfaces:**
- Adicionar `MT5Engine.auto_protection_state(trade_id: str, leg: int) -> dict`.
- Adicionar `MT5Engine.apply_hybrid_protect(trade: Trade, group_sl: float, now: str, msg_id: Optional[str]) -> list[dict]`.
- Adicionar `SimEngine.apply_hybrid_protect(trade: Trade, group_sl: float, now: str, msg_id: Optional[str]) -> list[dict]` com a mesma decisão, `engine="sim"` e sem chamada ao broker.
- Ambos usam `decide_protect`, `_modify_sl`/`move_position_sl`, `_record_close` e `DB.insert_protect_latency`; não executam quando `trade.strategy_id` não é `auto_escada_3_hibrida`.

- [ ] **Step 1: Escrever teste fake falhando para merge MT5**

Usar os helpers concretos já existentes no topo de `tests/test_mt5.py`: `setup`, `make_signal` e `cleanup`. O teste fica em dry-run para exercitar o mesmo método sem enviar ordens para nenhum terminal.

```python
def test_mt5_hybrid_protect_keeps_more_protective_sl():
    db, provider, market, eng, tmp = setup(price=4385.0)
    try:
        sig = make_signal(take_profit_3=4397.0)
        trade = eng.open_trade(sig, risk_pct=1.0, balance=10_000,
                               max_legs=3)
        trade.strategy_id = "auto_escada_3_hibrida"
        eng._auto_state[trade.id] = {
            "auto_sl_by_leg": {2: 4387.0, 3: 4387.0},
            "auto_protection_at_by_leg": {
                2: "2026-08-19T10:00:00+00:00",
                3: "2026-08-19T10:00:00+00:00",
            },
        }
        eng.apply_hybrid_protect(
            trade, group_sl=4385.3,
            now="2026-08-19T10:05:00+00:00", msg_id="m1",
        )
        positions = db.positions_for_trade(trade.id)
        assert all(p.stop_loss == 4387.0
                   for p in positions if p.leg in (2, 3))
        rows = db.protect_latency_rows(engine="mt5")
        assert len(rows) == 2
        assert all(row["winner"] == "auto" for row in rows)
    finally:
        cleanup(db, tmp)
```

- [ ] **Step 2: Confirmar falha**

Run:

```bash
.venv/Scripts/python -m pytest tests/test_mt5.py -k "hybrid_protect" -q
```

Expected: FAIL because the MT5 adapter method is absent.

- [ ] **Step 3: Expor estado auto e aplicar merge com retcodes reais**

Guardar `auto_sl_by_leg`/`auto_protection_at_by_leg` no mesmo `_auto_state` usado pelo trailing. O método de merge deve seguir esta sequência:

```python
decision = decide_protect(
    direction=trade.direction, auto_sl=pos.stop_loss,
    group_sl=group_sl, entry=trade.entry,
    tp1=trade.take_profit_1, price_at_event=self._price(trade.symbol),
    leg1_open=positions[1].status == "open",
    auto_protection_at=state.get("auto_protection_at_by_leg", {}).get(pos.leg),
    group_received_at=now, pip_size=self._sym_meta(trade.symbol)["pip"],
)
valid = self._valid_sl(trade, decision.applied_sl)
retcode = self._modify_sl(trade, pos, valid)
if self.dry_run:
    accepted = True
else:
    accepted = int(retcode or -1) in (10009, 0)
self._log_hybrid_latency(trade, pos, decision, valid, retcode, msg_id, now)
if accepted:
    self.db.move_position_sl(pos.id, valid)
    pos.stop_loss = valid
```

Em modo real, só atualizar a BD quando `_modify_sl` retornar `10009` ou `0`; em dry-run, o modify é aceite sem broker. Manter a regra existente de não gravar SL quando houver `10016`.

Para a perna 1, usar o preço atual do broker já obtido pelo tick; nunca usar `price_close` futuro ou o close da vela.

- [ ] **Step 4: Encaminhar `protect` no `BotState`**

No ramo `kind == "protect"` de `BotState._handle_management`, detetar a estratégia ativa pelo id configurado. Para `auto_escada_3_hibrida`, chamar o método `apply_hybrid_protect` do motor atual (`MT5Engine` ou `SimEngine`) e não executar o caminho genérico que fecha sempre a perna 1 e move o SL sem comparação. O `PaperRace.apply_management` continua a ser chamado para a conta híbrida e para as outras estratégias que honram `protect`.

- [ ] **Step 5: Executar os testes MT5 relevantes**

Run:

```bash
PYTHONIOENCODING=utf-8 timeout 120 .venv/Scripts/python -m pytest tests/test_mt5.py -k "hybrid_protect or auto_modify_refused_10016" -q
```

Expected: PASS sem arrancar o singleton do servidor e sem criar trades na BD real.

---

### Task 7: Expor relatório de latência na API e dashboard

**Files:**
- Modify: `app/analysis.py`
- Modify: `app/state.py`
- Modify: `app/main.py`
- Modify: `app/static/index.html`, `app/static/common.js`
- Modify: `tests/test_api.py`, `tests/test_frontend.py`

**Interfaces:**
- Add `summarize_latency(rows: list[dict]) -> dict` in `app/analysis.py` for non-persisted backtest rows.
- Add `async analysis.protect_latency_report(db, strategy_id="auto_escada_3_hibrida", engine="", limit=500, days=0, since="") -> dict`.
- Add `async BotState.protect_latency_report(...) -> dict`.
- Add endpoint:

```python
@app.get("/api/analysis/protect_latency")
async def analysis_protect_latency(strategy: str = "auto_escada_3_hibrida",
                                   engine: str = "", limit: int = 500,
                                   days: int = 0, since: str = ""):
    return await state.protect_latency_report(
        strategy_id=strategy, engine=engine, limit=limit,
        days=days, since=since,
    )
```

Response shape:

```json
{
  "ok": true,
  "strategy_id": "auto_escada_3_hibrida",
  "engine": "",
  "stats": {"count": 0, "auto_wins": 0, "group_wins": 0, "ties": 0},
  "rows": []
}
```

- [ ] **Step 1: Escrever teste API falhando**

```python
def test_protect_latency_endpoint(client):
    response = client.get(
        "/api/analysis/protect_latency?strategy=auto_escada_3_hibrida"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy_id"] == "auto_escada_3_hibrida"
    assert "stats" in payload and "rows" in payload
```

- [ ] **Step 2: Implementar relatório DB + rota FastAPI**

O relatório deve combinar as linhas persistidas de `paper`/`mt5`. Para `engine="backtest"`, `analysis.protect_latency_report` chama `run_analysis(..., days=days, since=since, **sim_params(strategy_id))`, lê `payload["protect_latency"]` e não persiste nada. A implementação do resumo comum e do relatório deve ser:

```python
def summarize_latency(rows: list[dict]) -> dict:
    import statistics
    seconds = sorted(float(r["delta_seconds"]) for r in rows
                     if r.get("delta_seconds") is not None)
    pips = sorted(float(r["delta_pips"]) for r in rows
                  if r.get("delta_pips") is not None)
    def q(values):
        return values[max(0, min(len(values) - 1, int(len(values) * 0.9) - 1))] if values else None
    return {
        "count": len(rows),
        "auto_wins": sum(r.get("winner") == "auto" for r in rows),
        "group_wins": sum(r.get("winner") == "group" for r in rows),
        "ties": sum(r.get("winner") == "tie" for r in rows),
        "auto_not_triggered": sum(r.get("status") == "auto_not_triggered" for r in rows),
        "mean_seconds": round(sum(seconds) / len(seconds), 2) if seconds else None,
        "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
        "p90_seconds": round(q(seconds), 2) if seconds else None,
        "max_seconds": round(seconds[-1], 2) if seconds else None,
        "mean_pips": round(sum(pips) / len(pips), 2) if pips else None,
        "p90_pips": round(q(pips), 2) if pips else None,
        "max_pips": round(pips[-1], 2) if pips else None,
    }


async def protect_latency_report(db, strategy_id="auto_escada_3_hibrida",
                                 engine="", limit=500, days=0, since=""):
    if engine == "backtest":
        from app.strategies import sim_params
        payload = await run_analysis(
            db, days=days, since=since, daily_stop_pct=0,
            **sim_params(strategy_id),
        )
        rows = payload.get("protect_latency", [])[:limit]
        return {"ok": True, "strategy_id": strategy_id,
                "engine": engine, "stats": summarize_latency(rows),
                "rows": rows}
    rows = db.protect_latency_rows(strategy_id=strategy_id,
                                   engine=engine, limit=limit)
    return {"ok": True, "strategy_id": strategy_id,
            "engine": engine, "stats": db.protect_latency_stats(
                strategy_id=strategy_id, engine=engine, limit=limit),
            "rows": rows}
```

O endpoint deve limitar `limit` a `1..500` e não devolver credenciais, texto completo do Telegram ou dados de conta.

- [ ] **Step 3: Adicionar bloco visual ao Relatório**

Adicionar ao bloco existente de Relatório em `index.html`:

```html
<section id="protectLatencyPanel" class="card" style="display:none">
  <div class="lbl">⏱️ Protect híbrido — latência auto vs grupo</div>
  <div id="protectLatencyStats" class="mini-stats"></div>
  <div class="scroll">
    <table id="protectLatencyTable">
      <thead><tr><th>Data</th><th>Sinal</th><th>Motor</th><th>Vencedor</th>
        <th>Δ pips</th><th>Δ segundos</th><th>Perna 1</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>
```

No carregamento do Relatório, chamar `/api/analysis/protect_latency?strategy=auto_escada_3_hibrida`; renderizar `—` quando não houver dados e mostrar claramente `N eventos` para não sugerir conclusão antes dos 20 eventos paper.

- [ ] **Step 4: Adicionar contrato estático e testar API**

`tests/test_frontend.py` deve verificar que `index.html` contém `protectLatencyPanel`, `protectLatencyTable`, `/api/analysis/protect_latency` e o id `auto_escada_3_hibrida`. Executar:

```bash
.venv/Scripts/python -m pytest tests/test_api.py -k "protect_latency" tests/test_frontend.py -q
```

Expected: todos PASS.

---

### Task 8: Mostrar a híbrida no painel Fase 2 sem permitir promoção indevida

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/common.js`
- Modify: `app/state.py` para expor DD/stats da híbrida no payload da Fase 2
- Modify: `tests/test_frontend.py`

**Interfaces:**
- `renderPhase2(snapshot)` passa a renderizar dois candidatos: `auto_escada_3` e `auto_escada_3_hibrida`, ambos contra `nova_gestao`.
- O botão de promoção continua ligado apenas à `auto_escada_3`, exatamente como o gate da Parte X.

- [ ] **Step 1: Escrever o contrato frontend falhando**

```python
from pathlib import Path


def test_phase2_contains_hybrid_candidate_and_keeps_phase3_target():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "auto_escada_3_hibrida" in html
    assert "btnPhase2Promote" in html
    phase2 = html[html.index("function renderPhase2"):html.index("function renderPhase3")]
    assert "auto_escada_3_hibrida" in phase2
    assert 'strategy !== "auto_escada_3"' in phase2
```

- [ ] **Step 2: Renderizar uma linha/card própria da híbrida**

Criar a função JS `phase2CandidateStats(account, reference, dd, label, color, promotable)` para não duplicar os oito minis:

```javascript
function phase2CandidateStats(account, reference, dd, label, color, promotable) {
  const autoPnl = Number(account.pnl || 0);
  const refPnl = Number(reference.pnl || 0);
  const pnlPct = refPnl ? autoPnl / refPnl * 100 : null;
  const autoDd = dd[account.strategy_id];
  const refDd = dd.nova_gestao;
  const gate = account.closed >= 20 && Number(account.profit_factor) >= 1.5
    && pnlPct !== null && autoPnl >= refPnl * 0.5
    && autoDd != null && refDd != null && autoDd <= refDd * 1.25;
  return `<div class="phase2-candidate" data-strategy="${account.strategy_id}">` +
    `<b style="color:${color}">${esc(label)}</b>` +
    ` · N ${account.closed || 0} · PF ${account.profit_factor ?? "—"}` +
    ` · P&L ${pnlPct == null ? "—" : fmt(pnlPct, 0) + "%"}` +
    ` · DD ${autoDd == null ? "—" : fmt(autoDd, 1) + "%"}` +
    `${promotable && gate ? " · ✅ elegível para revisão" : " · validação paper — não elegível para promoção automática"}` +
    `</div>`;
}
```

O candidato híbrido deve mostrar saldo, PF, P&L relativo, DD relativo, N e WR; `promotable` deve ser `true` apenas para `auto_escada_3`.

- [ ] **Step 3: Atualizar a legenda da curva e o refresh**

A curva de ritmo da Fase 2 deve continuar mostrando auto pura e referência; acrescentar a série híbrida quando o payload de `state._fase3_eta()` trouxer `n_hybrid`, sem inventar zero como trade real. `state.fase3_status()`/snapshot deve expor também `paper.hybrid` e `dd.auto_escada_3_hibrida`; `renderPhase2` deve continuar a ser chamado no refresh de instrumentos e no snapshot inicial.

- [ ] **Step 4: Testar que a promoção híbrida fica ocultada**

Com um snapshot sintético onde os três critérios da híbrida passam, verificar no DOM que os stats aparecem mas o botão de promoção não fica visível para a híbrida. Executar:

```bash
.venv/Scripts/python -m pytest tests/test_frontend.py -q
```

Expected: PASS.

---

### Task 9: Backtest, divergências, documentação e validação operacional

**Files:**
- Modify: `docs/ESTRATEGIAS.md`
- Modify: `AGENTS.md`
- Modify: `docs/TECHNICAL.md`
- Modify: `docs/PUBLIC.md`
- Modify: `docs/CHANGELOG.md`
- Keep: `docs/Melhorias/01-item-01.md` until the 20-event gate

**Interfaces:**
- Nenhuma alteração adicional de API; esta tarefa documenta os contratos produzidos nas tarefas anteriores.

- [ ] **Step 1: Executar o backtest atual e completo da híbrida**

Usar a rota de análise, sem abrir trades:

```bash
curl -s "http://127.0.0.1:8000/api/analysis/month?group=Golden%20Moves&days=30&strategy=auto_escada_3_hibrida&balance=1000" | python -m json.tool
curl -s "http://127.0.0.1:8000/api/analysis/month?group=Golden%20Moves&days=0&strategy=auto_escada_3_hibrida&balance=1000" | python -m json.tool
```

Comparar `profit_factor`, `pnl`, `drawdown` e `bankrupt` com as mesmas respostas de `auto_escada_3`. Não declarar o critério PF/DD cumprido só por falta de dados; guardar os resultados com a data e os parâmetros.

- [ ] **Step 2: Confirmar que a corrida paper abre a conta híbrida**

Depois de reiniciar o servidor Python com o mesmo modo atual e sem promover a híbrida:

```bash
curl -s "http://127.0.0.1:8000/api/status" | python -m json.tool
curl -s "http://127.0.0.1:8000/api/paper/history?hours=720" | python -m json.tool
curl -s "http://127.0.0.1:8000/api/analysis/protect_latency?strategy=auto_escada_3_hibrida" | python -m json.tool
```

Confirmar que a híbrida aparece em `paper.accounts`, na curva e no dropdown, enquanto `config.strategy` continua `nova_gestao`.

- [ ] **Step 3: Atualizar a documentação completa**

`docs/ESTRATEGIAS.md` deve acrescentar a híbrida à Parte VI, aos resultados da Parte VIII e à matriz da Parte IX, explicando a regra de merge, a salvaguarda da perna 1, a telemetria e o facto de não ser elegível para Fase 3 sem gates próprios.

`AGENTS.md` e `docs/TECHNICAL.md` devem documentar o registo, a tabela `protect_latency_log`, `GET /api/analysis/protect_latency`, os motores envolvidos e o filtro de promoção.

`docs/PUBLIC.md` deve explicar em linguagem simples que a híbrida usa a proteção automática e só aproveita uma instrução `protect` do grupo quando ela melhora o stop.

`docs/CHANGELOG.md` deve incluir a data, a implementação, os testes e o estado do gate de 20 eventos. Não afirmar que o item está concluído se ainda não houver 20 eventos paper.

- [ ] **Step 4: Executar a suite completa e validar o frontend**

Run:

```bash
.venv/Scripts/python -m pytest -q
```

Expected: todos os testes PASS e a contagem documentada atualizada para o número real.

Iniciar/usar o servidor de preview existente e validar:

```bash
curl -s http://127.0.0.1:8081/ >/dev/null
```

No navegador, confirmar visualmente: dropdown com a híbrida, curva com a híbrida, painel Fase 2 com a linha híbrida, e Relatório com `N eventos`/`—` quando ainda não houver protects.

- [ ] **Step 5: Aguardar e validar os 20 eventos paper**

Não gerar eventos artificiais na BD real. Após pelo menos 20 `protect` genuínos recebidos enquanto a híbrida estiver aberta em paper:

```bash
curl -s "http://127.0.0.1:8000/api/analysis/protect_latency?strategy=auto_escada_3_hibrida&engine=paper&limit=500" | python -m json.tool
```

Confirmar `stats.count >= 20`, distribuição de vencedor/latência e que cada linha contém timestamps UTC explícitos. Só então marcar `docs/Melhorias/01-item-01.md` como encerrado e removê-lo, se todos os critérios PF/DD/look-ahead também estiverem aprovados.

- [ ] **Step 6: Checkpoint final**

Rever `git diff`/estado dos ficheiros e não incluir `.env`, bases reais, credenciais ou logs com tokens. Num checkout Git normal, criar commits lógicos separados para política/DB, motores, UI e docs; neste checkout não executar `git commit` sem pedido explícito do utilizador.

---

## Self-review do plano

### Cobertura da especificação

- Registo com parâmetros auto e `manage_kinds={"protect"}`: Task 3.
- Merge de SL mais protetor para buy/sell: Tasks 1, 4, 5 e 6.
- Salvaguarda do fecho da perna 1: Task 1 e integrações 4–6.
- Instrumentação de timestamps, vencedor, pips e segundos: Tasks 1–2 e 4–6.
- Espelho nos três motores: Tasks 4–6.
- Dropdown, corrida paper e página própria: Task 3.
- Relatório com pelo menos 20 eventos paper: Tasks 2, 7 e 9.
- Backtest atual/completo e critério contra `auto_escada_3`: Task 9.
- Sem look-ahead, incluindo evento na mesma vela: Tasks 1 e 4.
- Não promoção automática/Fase 3: Tasks 3 e 8.
- Documentação e changelog: Task 9.

### Verificação de placeholders

Não há marcadores de trabalho pendente nem instruções vagas no plano; todas as tarefas têm ficheiros, interfaces, comandos e resultados esperados.

### Consistência de interfaces

- `decide_protect` recebe timestamps ISO e devolve `ProtectDecision` usado pelo backtest, paper e MT5.
- `DB.insert_protect_latency` recebe o dicionário produzido pelos três motores e `protect_latency_rows/stats` alimentam o relatório.
- `simulate_signal_managed` mantém os argumentos antigos e apenas acrescenta kwargs opcionais.
- `GET /api/analysis/protect_latency` usa o mesmo `strategy_id` da entrada do registo e do frontend.
- O botão de promoção continua explicitamente limitado a `auto_escada_3`, enquanto a híbrida aparece para validação.

### Fora do escopo deste plano

- Sweep de `auto_escada_5` defensiva.
- `confirm_filter` combinado como estudo separado.
- `def_risk_pct`.
- Recalibração mensal de `trail_factor`.
- Comparação `tp2_protect` vs híbrida com N≥20.
- Endpoint A/B/C genérico das cinco estratégias visíveis e PF histórico no dropdown.

Estes itens devem ter planos próprios depois do Item 1, para que cada um produza software testável sem misturar alterações de risco e de telemetria.
