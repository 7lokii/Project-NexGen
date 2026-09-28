import pytest

from app.signals.parser import ParseError, parse


def test_parse_entry_zone_is_preserved_for_freshness_guard():
    s = parse("BUY XAUUSD\n4584-4580\nSL 4578\nTP1 4586\nTP2 4588")
    assert s.entry == 4584.0
    assert s.entry_zone_low == 4580.0
    assert s.entry_zone_high == 4584.0


def test_parse_or_error_preserves_partial_signal_fields():
    """A primeira edição do grupo pode conter apenas direção + zona; esses
    campos têm de sobreviver ao erro para o revive e a auditoria."""
    from app.signals.parser import parse_or_error

    s = parse_or_error("I’m buying XAUUSD\n\n4642.50-4638")
    assert s.error
    assert s.symbol == "XAUUSD"
    assert s.direction == "buy"
    assert s.entry == 4642.5
    assert (s.entry_zone_low, s.entry_zone_high) == (4638.0, 4642.5)


def test_parse_labeled_entry_zone_is_preserved():
    s = parse("BUY XAUUSD\nEntry: 4584-4580\nSL 4578\nTP1 4586\nTP2 4588")
    assert s.entry == 4584.0
    assert (s.entry_zone_low, s.entry_zone_high) == (4580.0, 4584.0)


def test_parse_english_layout():
    s = parse("BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920")
    assert s.symbol == "EURUSD"
    assert s.direction == "buy"
    assert s.entry == 1.0850
    assert s.stop_loss == 1.0820
    assert s.take_profit_1 == 1.0890
    assert s.take_profit_2 == 1.0920
    assert s.status == "pending"


def test_parse_portuguese_layout_and_comma_decimals():
    s = parse(
        "COMPRA GBP/USD\n"
        "Entrada: 1,2700\n"
        "Stop loss: 1,2665\n"
        "Alvo 1: 1,2735\n"
        "Alvo 2: 1,2770"
    )
    assert s.symbol == "GBPUSD"
    assert s.direction == "buy"
    assert s.entry == 1.2700
    assert s.stop_loss == 1.2665
    assert s.take_profit_1 == 1.2735
    assert s.take_profit_2 == 1.2770


def test_parse_symbol_without_slash():
    s = parse("SELL XAUUSD @ 2450.0 SL 2458.0 TP1 2435.0 TP2 2420.0")
    assert s.symbol == "XAUUSD"
    assert s.direction == "sell"
    assert s.entry == 2450.0
    assert s.stop_loss == 2458.0


def test_parse_missing_field_raises():
    with pytest.raises(ParseError):
        parse("BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890")  # no TP2


def test_parse_bad_direction_raises():
    with pytest.raises(ParseError):
        parse("EURUSD @ 1.0850 SL 1.0820 TP1 1.0890 TP2 1.0920")


def test_parse_invalid_sl_side_raises():
    with pytest.raises(ParseError):
        parse("BUY EURUSD @ 1.0850 SL 1.0900 TP1 1.0890 TP2 1.0920")


def test_parse_tp1_to_tp5():
    s = parse(
        "BUY XAUUSD @ 4385.0\nSL 4381.0\nTP1 4389.0\nTP2 4395.0\n"
        "TP3 4402.0\nTP4 4410.0\nTP5 4420.0"
    )
    assert s.take_profit_1 == 4389.0
    assert s.take_profit_2 == 4395.0
    assert s.take_profit_3 == 4402.0
    assert s.take_profit_4 == 4410.0
    assert s.take_profit_5 == 4420.0


def test_parse_tp3_optional_and_partial():
    s = parse("BUY EURUSD @ 1.0850 SL 1.0820 TP1 1.0890 TP2 1.0920 TP3 1.0960")
    assert s.take_profit_3 == 1.0960
    assert s.take_profit_4 == 0.0
    assert s.take_profit_5 == 0.0


def test_parse_tp_out_of_order_raises():
    # TP3 closer than TP2 (buy) is a typo — never trade it
    with pytest.raises(ParseError):
        parse("BUY XAUUSD @ 4385.0 SL 4381.0 TP1 4389.0 TP2 4395.0 TP3 4392.0")


def test_parse_equal_tps_are_valid():
    """TPs iguais são válidos: o grupo enviou a sério "TP1 4280 / TP2 4280"
    (15/09 12:26 UTC) — ambas as pernas saem ao mesmo nível. Só TPs que
    INVERTEM a direção (ex.: TP3 mais perto que TP2) continuam rejeitados."""
    s = parse("BUY XAUUSD @ 4275.0 SL 4270.0 TP1 4280.0 TP2 4280.0")
    assert s.take_profit_1 == 4280.0
    assert s.take_profit_2 == 4280.0
    # sell igual também passa
    s2 = parse("SELL XAUUSD @ 2450.0 SL 2458.0 TP1 2435.0 TP2 2435.0")
    assert s2.take_profit_2 == 2435.0


def test_parse_sell_tps_ordered_descending():
    s = parse("SELL XAUUSD @ 2450.0 SL 2458.0 TP1 2435.0 TP2 2420.0 TP3 2400.0")
    assert s.take_profit_3 == 2400.0


# --------------------------------------------- Golden Moves old formats


def test_parse_old_xau_usd_now_format():
    """Formato antigo (abr–jul 2026): "XAU USD SELL NOW 4737-4741 🥇TP1 …
    🚫SL …" — símbolo com espaço, emojis de medalha, SL com 🚫."""
    s = parse("XAU USD SELL NOW  4737-4741  🥇TP1 4734 🥈TP2 4732 🥉TP3 4730 "
              "🏅TP4 4728       TP5 4723  🚫SL 4745")
    assert s.symbol == "XAUUSD"
    assert s.direction == "sell"
    assert s.entry == 4737.0
    assert s.stop_loss == 4745.0
    assert s.take_profit_1 == 4734.0
    assert s.take_profit_2 == 4732.0
    assert s.take_profit_3 == 4730.0
    assert s.take_profit_4 == 4728.0
    assert s.take_profit_5 == 4723.0


def test_parse_old_xau_usd_buy_now_format():
    s = parse("XAU USD BUY NOW  4710.5-4705.5  🥇TP1 4713.50 🥈TP2 4715 "
              "🥉TP3 4717 🏅TP4 4719       TP5 4724  🚫SL 4702")
    assert s.symbol == "XAUUSD"
    assert s.direction == "buy"
    assert s.entry == 4710.5
    assert s.stop_loss == 4702.0
    assert s.take_profit_5 == 4724.0


def test_parse_old_gold_now_targets_format():
    """Formato mais antigo: "Sell Gold Now 4123.5-4128.5 Targets 4121.5
    4119.5 4117 Open SL/invalid 4131.5" — Targets é uma lista sem rótulo
    por target; o SL tem "invalid" a seguir."""
    s = parse("Sell Gold Now  4123.5 - 4128.5  Targets  4121.5 4119.5 4117 "
              "Open  SL/ invalid 4131.5")
    assert s.symbol == "XAUUSD"
    assert s.direction == "sell"
    assert s.entry == 4123.5
    assert s.stop_loss == 4131.5
    assert s.take_profit_1 == 4121.5
    assert s.take_profit_2 == 4119.5
    assert s.take_profit_3 == 4117.0
    assert s.take_profit_4 == 0.0


def test_parse_old_buy_gold_now_targets_4():
    """Versão com 4 targets na lista (o 4º é TP4)."""
    s = parse("Buy Gold Now  4098 - 4093  Targets   4101 4103 4105 4107 Open  "
              "SL/ invalid 4089")
    assert s.symbol == "XAUUSD"
    assert s.direction == "buy"
    assert s.entry == 4098.0
    assert s.stop_loss == 4089.0
    assert s.take_profit_1 == 4101.0
    assert s.take_profit_2 == 4103.0
    assert s.take_profit_3 == 4105.0
    assert s.take_profit_4 == 4107.0
    assert s.take_profit_5 == 0.0


def test_parse_single_line_current_format():
    """O formato atual também aparece sem quebras de linha; a zona de
    entrada fica no meio da linha após a direção."""
    s = parse("I’m buying XAUUSD 4416-4412 TP1 4419 TP2 4421 TP3 4423 "
              "TP4 4426 TP5 4432 SL 4408.50")
    assert s.symbol == "XAUUSD"
    assert s.direction == "buy"
    assert s.entry == 4416.0
    assert s.stop_loss == 4408.5
    assert s.take_profit_5 == 4432.0


def test_parse_rejects_impossible_zone_typos():
    """Zonas impossíveis (typos do grupo) são rejeitadas: "4379-4775"
    são 396 pontos de ouro — ninguém negocia uma zona assim. O span máximo
    aceite é ~1% do preço (mantém zonas largas legítimas como 4078.50-4047)."""
    with pytest.raises(ParseError):
        parse("I’m buying XAUUSD 4379-4775 TP1 4381 TP2 4383 TP3 4385 "
              "TP4 4388 SL 4371")
    with pytest.raises(ParseError):
        parse("I’m buying XAUUSD 4317-3413 TP1 4320 TP2 4322 TP3 4324 "
              "TP4 4326 TP5 4330 SL 4312")


def test_parse_accepts_wide_but_legit_zone():
    """Uma zona larga mas coerente (31.5 pontos, TPs e SL alinhados) é
    aceite — o span check é relativo ao preço, não fixo."""
    s = parse("I’m buying XAUUSD 4078.50-4047 TP1 4081 TP2 4083 TP3 4085 "
              "TP4 4089 SL 4070")
    assert s.entry == 4078.5
    assert s.take_profit_1 == 4081.0


def test_parse_golden_circle_single_entry():
    """Golden Circle format: a single (unlabelled) entry price right after
    the symbol — "I'm buying XAUUSD  4331  TP1 4336 TP2 4341 SL 4325"."""
    s = parse("I’m buying XAUUSD  4331  TP1 4336 TP2 4341 SL 4325")
    assert s.symbol == "XAUUSD"
    assert s.direction == "buy"
    assert s.entry == 4331.0
    assert s.stop_loss == 4325.0
    assert s.take_profit_1 == 4336.0
    assert s.take_profit_2 == 4341.0


def test_parse_golden_circle_single_entry_sell():
    s = parse("I’m Selling XAUUSD  4390  TP1 4380 TP2 4370  SL 4400")
    assert s.symbol == "XAUUSD"
    assert s.direction == "sell"
    assert s.entry == 4390.0
    assert s.stop_loss == 4400.0
    assert s.take_profit_1 == 4380.0
    assert s.take_profit_2 == 4370.0


def test_parse_symbol_followed_by_zone_still_prefers_zone():
    """A symbol followed by a range is a zone, not a single entry — the zone
    path must win (the (?!zone) guard in the single-entry pattern)."""
    s = parse("I’m selling XAUUSD  4323-4327   TP1 4320 TP2 4318 TP3 4316 TP4 4312  SL 4331")
    assert s.entry == 4323.0
    assert s.entry_zone_low == 4323.0
    assert s.entry_zone_high == 4327.0


def test_parse_single_entry_not_triggered_by_conversation():
    """Conversa com números perto do símbolo não vira sinal — falta TP1/TP2
    e o regex de entrada isolada nunca cria uma entrada operacional."""
    with pytest.raises(ParseError):
        parse("XAUUSD at 4331 right now, watching closely")
    with pytest.raises(ParseError):
        parse("BTC to watch: 65000 zone chart https://tradingview.com/x/abc")
