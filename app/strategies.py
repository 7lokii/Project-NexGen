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

# Estratégias retiradas do menu/páginas de teste E da corrida paper
# (mantidas no registo para o backtest/compare histórico — nunca se apagam
# dados). Decisão 19/08 (spec de consolidação): as perdedoras/redundantes
# saem do menu e do paper; a `nova` e a `auto_5` ficam só em MENU_HIDDEN
# (ver abaixo) porque são o controlo/comparação do backtest.
#
# Removidas 19/08 (spec de consolidação §3.1):
#   tp2          PF 0.86 · −151 € (perde)
#   tp2_gestao   PF ~2.93 — perde para a nova_gestao (3.22)
#   nova_gestao_3  corta as pernas 4–5 que dão o lucro; sem edge
#   auto_2       pior PF da família (2.20); 2 TPs cortam o motor do lucro
#
# Alias do plano de limpeza (19/08): `REMOVED_STRATEGIES` é o nome usado no
# spec para as 4 estratégias removidas do menu E do paper — apontar para o
# mesmo set garante que "remover" e "esconder" não divergem. Para repor uma,
# basta tirar o ID daqui (volta ao menu e ao paper).
REMOVED_STRATEGIES: Set[str] = {"tp2", "tp2_gestao", "nova_gestao_3", "auto_2"}
HIDDEN_STRATEGIES: Set[str] = REMOVED_STRATEGIES

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
           stair_levels: Optional[int] = None,
           stair_from: int = 1,
           confirm_filter: bool = False,
           trail_factor: Optional[float] = None,
           time_stop_minutes: Optional[int] = None,
           def_window: Optional[int] = None,
           def_trail: Optional[float] = None,
           def_time_stop: Optional[int] = None,
           atr_k: Optional[float] = None,
           atr_period: int = 14,
           atr_timeframe: int = 1) -> Dict:
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
    abertas N min após o TP1, fecham ao mercado. `def_window`/`def_trail`/
    `def_time_stop` (modo defensivo, estudo 19/08) = quando o PF das últimas
    `def_window` trades resolvidas < 1.0, a estratégia passa a `def_trail`
    (trail mais apertado) + `def_time_stop` (time stop ligado) SEM cortar
    pernas — volta ao normal quando o PF recupera. `atr_k`/`atr_period`/
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
        "stair_levels": stair_levels,
        "stair_from": stair_from,
        "confirm_filter": confirm_filter,
        "trail_factor": trail_factor,
        "time_stop_minutes": time_stop_minutes,
        "def_window": def_window,
        "def_trail": def_trail,
        "def_time_stop": def_time_stop,
        "atr_k": atr_k,
        "atr_period": atr_period,
        "atr_timeframe": atr_timeframe,
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
        max_legs=None, manage=True, exit_at_tp=None,
    ),
    "tp2": _strat(
        "tp2", "TP1→BE + sair no TP2",
        "SL sempre ativo (SL-first); TP1 fecha a perna 1 e move o SL das "
        "restantes para breakeven; quando o TP2 bate, todas as pernas ainda "
        "abertas fecham nesse preço. Sem gestão do grupo.",
        max_legs=None, manage=False, exit_at_tp=2,
    ),
    "tp2_gestao": _strat(
        "tp2_gestao", "Sair no TP2 + gestão",
        "Igual ao TP1→BE + sair no TP2, MAS com a gestão do grupo ao vivo "
        "(protect/close_all/close_tp1) — o grupo pode fechar antes do TP2 "
        "ou proteger as pernas com mais cedo.",
        max_legs=None, manage=True, exit_at_tp=2,
    ),
    "tp2_protect": _strat(
        "tp2_protect", "Sair no TP2 + protect only",
        "SL sempre ativo (SL-first); TP1 fecha a perna 1 e move o SL das "
        "restantes para breakeven; quando o TP2 bate, todas as pernas ainda "
        "abertas fecham nesse preço. Da gestão do grupo só aplica o "
        "'protect' (fechar perna 1 + SL acima da melhor entrada) — ignora "
        "close_all/close_tp1/move_sl: o grupo pode dizer 'we're out' que a "
        "estratégia continua a correr para o TP2.",
        max_legs=None, manage_kinds={"protect"}, exit_at_tp=2,
    ),
    "nova_gestao_3": _strat(
        "nova_gestao_3", "TP1..TP3 + gestão",
        "Só 3 pernas (TP1..TP3) + gestão do grupo (BE após TP1, "
        "protect/close_all) — menos lucro que a completa mas menos risco "
        "de cauda nas pernas 4–5 (seguro contra mudança de regime).",
        max_legs=3, manage=True, exit_at_tp=None,
    ),
    # ------------------------------------------------------------------ #
    # Auto-gestão (docs/ESTRATEGIAS.md, Parte V) — o bot gere sozinho, sem
    # mensagens do grupo (manage_kinds vazio). HIDDEN até o sweep validar.
    # ------------------------------------------------------------------ #
    "auto_2": _strat(
        "auto_2", "Auto 2 TPs",
        "2 pernas (TP1..TP2), BE após TP1 — sem gestão do grupo. "
        "Controlo: o que os amigos sugeriram (os dados dizem PF < 1 sem a "
        "escada).",
        max_legs=2, manage=False, exit_at_tp=None,
    ),
    "auto_3": _strat(
        "auto_3", "Auto 3 TPs",
        "3 pernas (TP1..TP3), BE após TP1 — sem gestão do grupo. "
        "Mínimo viável com edge (pernas 1–2 dão prejuízo; o lucro vive nas "
        "3–5).",
        max_legs=3, manage=False, exit_at_tp=None,
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
        def_window=10, def_trail=1.5, def_time_stop=60,
    ),
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
    "auto_escada_5": _strat(
        "auto_escada_5", "Auto 5 TPs + escada + confirmação",
        "5 pernas + escada de proteção + filtro de confirmação: se uma perna "
        "k levar SL (o movimento morreu), as pernas > k fecham ao mercado "
        "(dinâmico 2 do spec: \"se a 3 morrer, sai tudo\"). Sem gestão do "
        "grupo. Modo defensivo (19/08): PF(10) < 1.0 → trail 1.5 + time stop "
        "60 min.",
        max_legs=None, manage=False, stair_levels=5, stair_from=1,
        confirm_filter=True, trail_factor=2.0,
        def_window=10, def_trail=1.5, def_time_stop=60,
    ),
}


def get_strategy(strategy_id: Optional[str]) -> Dict:
    """Estratégia por id; fallback para a principal (nunca rebenta)."""
    return STRATEGIES.get(strategy_id or "", STRATEGIES[MAIN_STRATEGY_ID])


def strategies_list() -> list:
    """Registo completo, com o flag `hidden` — as ocultas ficam no registo
    para o backtest/compare histórico mas não aparecem no menu, nas páginas
    de teste, na corrida paper nem na página HELP. `REMOVED_STRATEGIES`
    (alias de `HIDDEN_STRATEGIES` — as 4 removidas do menu E do paper) e
    `MENU_HIDDEN_STRATEGIES` (nova — contrafactual "sem gestão" — e auto_5)
    aparecem como hidden para o frontend; as MENU_HIDDEN continuam a abrir
    trades de papel (contrafactual/comparação)."""
    return [{**dict(s), "hidden": sid in HIDDEN_STRATEGIES or sid in MENU_HIDDEN_STRATEGIES}
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
        # trailing adaptativo por ATR (espelho do MT5Engine, ESTRATEGIAS.md Parte X): o
        # `run_analysis`/`simulate_signal` aceitam estes kwargs — o
        # `atr_timeframe` (enum M1/M5) fica de fora (o backtest usa o
        # timeframe do próprio caminho de velas; é só do MT5 live).
        "atr_k": s.get("atr_k"),
        "atr_period": int(s.get("atr_period") or 14),
    }
