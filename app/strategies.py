"""Registo central de estratégias.

Cada estratégia define como o bot executa uma trade: quantas pernas usa
(`max_legs`), se aplica a gestão do grupo (`manage` — BE após TP1,
protect/close_all) e se fecha tudo num TP (`exit_at_tp`). É a fonte única
de verdade para:

  * a **página principal** — usa `MAIN_STRATEGY_ID` (a melhor segundo os
    dados, ver AGENTS.md) com a conta MT5 real;
  * as **páginas de teste** — uma por estratégia, cada uma com o seu saldo
    simulado (replay de 1 mês e/ou paper trading forward);
  * o backtest (`run_analysis`/`run_month_sim`) — os parâmetros são passados
    tal e qual ao motor de simulação.

Adicionar uma estratégia nova = acrescentar uma entrada aqui; a navegação,
as páginas de teste e o compare geram-se a partir deste registo.
"""

from typing import Dict, Optional, Set

# Kinds de gestão do grupo que agem em posições abertas (ver
# app/messages/manage.py::classify). `summary`/`go` não estão aqui porque
# não gerem uma trade aberta.
ALL_MGMT_KINDS: Set[str] = {
    "protect", "close_all", "close_tp1", "close_tp", "move_sl", "tp_hit", "sl_hit",
}

# Estratégias retiradas DO REGISTO a 01/09 (decisão do utilizador — remoção
# completa, já não são backtestáveis):
#   tp2          PF 0.86 · −151 € (perde)
#   tp2_gestao   PF ~2.93 — perde para a nova_gestao (3.22)
#   nova_gestao_3  corta as pernas 4–5 que dão o lucro; sem edge
#   auto_2       pior PF da família (2.20); 2 TPs cortam o motor do lucro
#   auto_protect_5  hipótese sem robustez out-of-sample (PF 1.27/DD 38.6%)
#   auto_escada_3_struggle  regra R4 sem edge no backtest de 20/08
# O registo histórico (o porquê, PF/DD de cada uma) está no CHANGELOG e nos
# capítulos removidos do ESTRATEGIAS.md — ver docs/CHANGELOG.md (01/09).
# Consequência documentada: o backtest/compare destas variantes deixa de ser
# reproduzível (ver CHANGELOG). A mecânica genérica (escada/trailing/time
# stop/protect_trigger/struggle) fica nos motores para candidatas futuras.

# Estado operacional único. Os sets abaixo continuam exportados por
# compatibilidade com estudos antigos; novas decisões devem usar este mapa.
STRATEGY_STATES = {
    "nova_gestao": "active",
    "nova_gestao_fallback": "experimental",
    "tp2_protect": "candidate",
    "auto_3": "reference",
    "auto_escada_3": "candidate",
    "auto_escada_3_hibrida": "experimental",
    "auto_escada_5": "reference",
    "nova": "reference",
    "auto_5": "reference",
}
# Retiradas do registo a 01/09: tp2, tp2_gestao, nova_gestao_3, auto_2,
# auto_protect_5, auto_escada_3_struggle — sem entrada aqui (o default
# `retired` do lifecycle cobre ids desconhecidos na BD antiga).
HIDDEN_STRATEGIES: Set[str] = {sid for sid, state in STRATEGY_STATES.items()
                               if state in {"retired", "backtest_only"}}
# Estratégias que saem do menu/páginas/HELP e da CURVA da corrida, MAS
# continuam a abrir trades de papel (contrafactual/comparação):
#   nova     — conta do contrafactual "sem gestão" do histórico de trades
#              (`counterfactual_pnl` — escondê-la da corrida visual não pode
#              cortar esse contrafactual);
#   auto_5   — = nova com trail: a comparação "5 pernas com/sem trail" no
#              backtest e no paper.
MENU_HIDDEN_STRATEGIES: Set[str] = {"nova", "auto_5"}


def _strat(id_: str, nome: str, desc: str, *, max_legs: Optional[int] = None,
           manage: bool = False, manage_kinds: Optional[Set[str]] = None,
           exit_at_tp: Optional[int] = None,
           lots_override: Optional[float] = None,
           menu_sub: str = "",
           stair_levels: Optional[int] = None,
           stair_from: int = 1,
           confirm_filter: bool = False,
           trail_factor: Optional[float] = None,
           time_stop_minutes: Optional[int] = None,
           def_window: Optional[int] = None,
           def_trail: Optional[float] = None,
           def_time_stop: Optional[int] = None,
           def_risk_pct: Optional[float] = None,
           protect_trigger: Optional[float] = None,
           protect_buffer: Optional[float] = None,
           be_buffer_pips: Optional[float] = None,
           atr_k: Optional[float] = None,
           atr_period: int = 14,
           atr_timeframe: int = 1,
           struggle_ae_pips: Optional[float] = None,
           struggle_fraction: float = 0.5) -> Dict:
    """`manage=True` (legacy) = aplica todos os kinds de gestão.
    `manage_kinds` (novo) filtra quais kinds a estratégia honra — ex.:
    `{"protect"}` = "protect only" (ignora close_all/close_tp1/move_sl).

    Parâmetros da auto-gestão (docs/ESTRATEGIAS.md, Parte V):
    `stair_levels` (escada de proteção) = quando o TP_k bate, o SL das pernas
    seguintes sobe para o nível do TP_k; `stair_from` decide o degrau do TP1
    (1 = SL sobe para o NÍVEL do TP1; 2 = TP1 só vai a BE, escada começa no
    TP2); `confirm_filter` = se uma perna k levar SL, as pernas > k fecham
    também ao mercado; `trail_factor` = múltiplo da distância entrada→TP2
    que o SL pode recuar do pico (trailing após TP1; 2.0 ≈ 8 pips em gold);
    `time_stop_minutes` = se as pernas ficarem
    abertas N min após o TP1, fecham ao mercado.
    `def_window`/`def_trail`/`def_time_stop`/`def_risk_pct` (modo defensivo,
    estudo 19/08) = quando o PF das últimas `def_window` trades resolvidas <
    1.0, a estratégia passa a `def_trail` (trail mais apertado) +
    `def_time_stop` (time stop ligado) e limita o risco a `def_risk_pct` SEM
    cortar pernas — volta ao normal quando o PF recupera. `atr_k`/`atr_period`/
    `atr_timeframe` (pós-validação, ESTRATEGIAS.md Parte X) = trailing adaptativo por ATR
    (M1/M5): trail_dist = max(trail_factor × dist, atr_k × ATR) — o recuo
    nunca fica mais apertado que k × ATR."""
    if manage and manage_kinds is None:
        manage_kinds = set(ALL_MGMT_KINDS)
    return {
        "id": id_,
        "nome": nome,
        "desc": desc,
        "max_legs": max_legs,
        "manage": bool(manage_kinds),
        "manage_kinds": set(manage_kinds or ()),
        "exit_at_tp": exit_at_tp,
        "lots_override": lots_override,
        # sub-label do dropdown da dashboard (common.js re-hidrata o menu a
        # partir de /api/strategies — o texto vive junto do registo)
        "menu_sub": menu_sub,
        "stair_levels": stair_levels,
        "stair_from": stair_from,
        "confirm_filter": confirm_filter,
        "trail_factor": trail_factor,
        "time_stop_minutes": time_stop_minutes,
        "def_window": def_window,
        "def_trail": def_trail,
        "def_time_stop": def_time_stop,
        "def_risk_pct": def_risk_pct,
        # early mechanical group-protect candidate: fraction of entry→TP1
        # reached before closing leg 1 and locking the remaining legs above
        # entry; None keeps the existing auto policies unchanged.
        "protect_trigger": protect_trigger,
        "protect_buffer": protect_buffer,
        # BE+ (tp2_protect): após o TP1, o SL das restantes vai para
        # entrada ± be_buffer_pips (cobre o spread) — BE que não devolve
        # metade do spread ao broker. None = BE clássico na entrada.
        "be_buffer_pips": be_buffer_pips,
        "atr_k": atr_k,
        "atr_period": atr_period,
        "atr_timeframe": atr_timeframe,
        # R4 "struggle" (regra do admin: "take 50% / make risk-free"):
        # antes do TP1, se o preço andou contra a entrada por N pips e volta
        # à entrada, a trade fecha uma fração a BE e protege as restantes.
        "struggle_ae_pips": struggle_ae_pips,
        "struggle_fraction": struggle_fraction,
    }


# A melhor estratégia segundo os dados reais (PF 7.07 no mês / 6.73 no
# compare) — é a que a conta principal executa.
MAIN_STRATEGY_ID = "nova_gestao"

STRATEGIES: Dict[str, Dict] = {
    "nova": _strat(
        "nova", "TP1..TP5",
        "Todas as pernas correm para os TPs (TP1..TP5), sem gestão do grupo.",
        max_legs=None, manage=False, exit_at_tp=None,
    ),
    "nova_gestao": _strat(
        "nova_gestao", "TP1..TP5 + gestão",
        "TP1..TP5 + gestão do grupo (BE após TP1, protect/close_all). "
        "É a estratégia principal atual.",
        max_legs=None, manage=True, exit_at_tp=None, menu_sub="principal · referência"
    ),
    "nova_gestao_fallback": _strat(
        "nova_gestao_fallback", "TP1..TP5 + gestão + fallback",
        "Igual à nova_gestao, mas aplica proteção mecânica se, após o TP1, "
        "não chegar gestão do grupo dentro de 5 minutos. Continua a respeitar "
        "protect/close_all humanos e só sobe o SL.",
        max_legs=None, manage=True, exit_at_tp=None,
        menu_sub="candidata · fallback 5 min"
    ),
    "tp2_protect": _strat(
        "tp2_protect", "Sair no TP2 + protect + close_all",
        "SL sempre ativo (SL-first); TP1 fecha a perna 1 e move o SL das "
        "restantes para breakeven+ (entrada + 40 pips, cobre o spread e "
        "trava lucro pequeno); quando o TP2 bate, todas as pernas ainda "
        "abertas fecham nesse preço. Da gestão do grupo aplica o 'protect' "
        "(fechar perna 1 + SL acima da melhor entrada) e o 'close_all' — "
        "quando o grupo diz CLOSE ALL / we're out, as duas lanes saem ao "
        "mercado como a nova_gestao. Ignora close_tp1/move_sl: a estratégia "
        "continua a correr para o TP2 (o protect do grupo continua a valer).",
        max_legs=2, manage_kinds={"protect", "close_all"}, exit_at_tp=2,
        be_buffer_pips=40.0,
        menu_sub="protect + close_all do grupo"
    ),
    # ------------------------------------------------------------------ #
    # Auto-gestão (docs/ESTRATEGIAS.md, Parte V) — o bot gere sozinho, sem
    # mensagens do grupo (manage_kinds vazio).
    # ------------------------------------------------------------------ #
    "auto_3": _strat(
        "auto_3", "Auto 3 TPs",
        "3 pernas (TP1..TP3), BE após TP1 — sem gestão do grupo. "
        "Mínimo viável com edge (pernas 1–2 dão prejuízo; o lucro vive nas "
        "3–5).",
        max_legs=3, manage=False, exit_at_tp=None, menu_sub="3 pernas"
    ),
    "auto_5": _strat(
        "auto_5", "Auto 5 TPs",
        "5 pernas (TP1..TP5), BE após TP1 — sem gestão do grupo. "
        "= a atual `nova` (baseline sem gestão).",
        max_legs=None, manage=False, exit_at_tp=None,
    ),
    "auto_escada_3": _strat(
        "auto_escada_3", "Auto 3 TPs + escada",
        "3 pernas + escada de proteção: quando o TP_k bate, o SL das pernas "
        "seguintes sobe para o nível do TP_k (dinâmico 1 do spec: \"dar TP3 "
        "sem perder nada\") + filtro de confirmação (se uma perna leva SL, "
        "as seguintes fecham ao mercado). Sem gestão do grupo. Modo defensivo "
        "(19/08): "
        "quando o PF das últimas 10 trades < 1.0, trail apertado (1.5) + "
        "time stop (60 min) até recuperar — sem cortar pernas.",
        max_legs=3, manage=False, stair_levels=3, stair_from=1,
        confirm_filter=True, trail_factor=2.0,
        def_window=10, def_trail=1.5, def_time_stop=60, def_risk_pct=1.25, menu_sub="Fase 2 · escada + trailing"
    ),
    "auto_escada_3_hibrida": _strat(
        "auto_escada_3_hibrida", "Auto 3 TPs + escada + protect híbrido",
        "3 pernas + escada/trailing mecânicos sempre ativos. Honra apenas "
        "protect do grupo quando o SL proposto for mais protetor que o SL "
        "mecânico; nunca piora a auto-gestão e não é candidata automática à "
        "Fase 3.",
        max_legs=3, manage_kinds={"protect"}, stair_levels=3, stair_from=1,
        confirm_filter=False, trail_factor=2.0,
        def_window=10, def_trail=1.5, def_time_stop=60, def_risk_pct=1.25, menu_sub="paper · proteção mecânica + grupo"
    ),
    "auto_escada_5": _strat(
        "auto_escada_5", "Auto 5 TPs + escada + confirmação",
        "5 pernas + escada de proteção + filtro de confirmação: se uma perna "
        "k levar SL (o movimento morreu), as pernas > k fecham ao mercado "
        "(dinâmico 2 do spec: \"se a 3 morrer, sai tudo\"). Sem gestão do "
        "grupo. Modo defensivo (19/08): PF(10) < 1.0 → trail 1.5 + time stop "
        "60 min.",
        max_legs=None, manage=False, stair_levels=5, stair_from=1,
        confirm_filter=True, trail_factor=2.0,
        def_window=10, def_trail=1.5, def_time_stop=60, def_risk_pct=1.25, menu_sub="5 pernas + confirmação"
    ),
}


def get_strategy(strategy_id: Optional[str]) -> Dict:
    """Estratégia por id; `None`/vazio = a principal (nunca rebenta em
    chamadas internas que dependem de config). Id desconhecido/retirado
    levanta ValueError — um estudo com um id morto NÃO corre silenciosamente
    com a estratégia errada (regra pós-remoção de 01/09)."""
    if not strategy_id:
        return STRATEGIES[MAIN_STRATEGY_ID]
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise ValueError(
            f"estratégia desconhecida/removida: {strategy_id!r} — ativas: "
            f"{', '.join(sorted(STRATEGIES))}"
        )


def strategy_lifecycle(strategy_id: str) -> dict:
    """Single explicit lifecycle contract for UI, paper and promotion.

    The historical sets remain backwards-compatible, but callers can now
    inspect one vocabulary instead of reimplementing hidden/menu rules.
    """
    state = STRATEGY_STATES.get(strategy_id, "retired")
    return {
        "state": state,
        "backtest": True,
        "paper": state in {"active", "candidate", "experimental", "reference"},
        "shadow": state in {"active", "candidate", "experimental", "reference"},
        "menu": state in {"active", "candidate", "experimental"},
        "promotable": state in {"active", "candidate", "experimental"},
        "hidden": state in {"retired", "backtest_only", "reference"},
    }


def paper_strategy_ids() -> list:
    """Estratégias que ABREM a corrida paper NORMAL (o shadow-paper usa a
    própria lista da config `shadow_paper_strategies` — não depende disto).

    Pós-remoção (01/09) a corrida corre só o que está EM VALIDAÇÃO — as 5
    com página/menu (active/candidate/experimental) — mais a `nova`, mantida
    SILENCIOSAMENTE como contrafactual do `counterfactual_pnl` (não aparece
    na corrida visual nem notifica no feed). As referências backtest
    (auto_3/auto_5/auto_escada_5) deixam de abrir papel normal — o backtest/
    compare e o shadow continuam disponíveis pelo registo/config.
    """
    return [sid for sid in STRATEGIES
            if sid == "nova" or strategy_lifecycle(sid)["menu"]]


def strategies_list() -> list:
    """Registo completo with one explicit lifecycle payload per strategy."""
    return [{**dict(s), "lifecycle": strategy_lifecycle(sid),
             "hidden": sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES}
            for sid, s in STRATEGIES.items()]


def sim_params(strategy_id: Optional[str]) -> Dict:
    """Parâmetros de simulação de uma estratégia (para run_analysis)."""
    s = get_strategy(strategy_id)
    return {
        "max_legs": s["max_legs"],
        "manage": s["manage"],
        "manage_kinds": set(s.get("manage_kinds") or ()),
        "exit_at_tp": s["exit_at_tp"],
        "lots_override": s["lots_override"],
        "stair_levels": s.get("stair_levels"),
        "stair_from": int(s.get("stair_from") or 1),
        "confirm_filter": bool(s.get("confirm_filter") or False),
        "trail_factor": s.get("trail_factor"),
        "time_stop_minutes": s.get("time_stop_minutes"),
        # modo defensivo (estudo 19/08): PF(10) < 1.0 → trail apertado +
        # time stop, sem cortar pernas — espelho no backtest/paper/MT5
        "def_window": s.get("def_window"),
        "def_trail": s.get("def_trail"),
        "def_time_stop": s.get("def_time_stop"),
        "def_risk_pct": s.get("def_risk_pct"),
        "protect_trigger": s.get("protect_trigger"),
        "protect_buffer": s.get("protect_buffer"),
        "be_buffer_pips": s.get("be_buffer_pips"),
        # trailing adaptativo por ATR (espelho do MT5Engine, ESTRATEGIAS.md Parte X): o
        # `run_analysis`/`simulate_signal` aceitam estes kwargs — o
        # `atr_timeframe` (enum M1/M5) fica de fora (o backtest usa o
        # timeframe do próprio caminho de velas; é só do MT5 live).
        "atr_k": s.get("atr_k"),
        "atr_period": int(s.get("atr_period") or 14),
        # R4 "struggle": backtest-only até o estudo validar.
        "struggle_ae_pips": s.get("struggle_ae_pips"),
        "struggle_fraction": float(s.get("struggle_fraction") or 0.5),
        # fallback conservador da variante nova_gestao_fallback: aplicado
        # apenas depois de TP1 e sem gestão posterior.
        "management_fallback_seconds": 300 if strategy_id == "nova_gestao_fallback" else None,
        "fallback_buffer_fraction": 0.10,
    }
