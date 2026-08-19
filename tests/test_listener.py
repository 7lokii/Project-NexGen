from datetime import datetime, timezone

from app.telegram_listener import (build_payload, history_limit,
                                   looks_like_signal, msg_date_utc,
                                   signal_id_from)


def test_build_payload_carries_message_id_and_group():
    p = build_payload("BUY EURUSD @ 1.0850", 12345, group="111", source="tg:Group A")
    assert p["raw_text"] == "BUY EURUSD @ 1.0850"
    assert p["message_id"] == "12345"
    assert p["group"] == "111"
    assert p["source"] == "tg:Group A"
    assert "historical" not in p


def test_build_payload_historical_flag():
    p = build_payload("BUY EURUSD @ 1.0850", 1, group="111", source="tg:Group A",
                      historical=True)
    assert p["historical"] is True


def test_signal_id_from_group_and_message():
    assert signal_id_from("111", 99) == "tg-111-99"


def test_looks_like_signal_accepts_signal_text():
    assert looks_like_signal("BUY EURUSD @ 1.0850\nSL 1.0820\nTP1 1.0890\nTP2 1.0920")
    assert looks_like_signal("COMPRA XAUUSD entrada 2450.0 alvo 2465.0")
    assert looks_like_signal("SELL GBPUSD stop 1.2735 tp2 1.2600")


def test_looks_like_signal_rejects_chatter():
    assert not looks_like_signal("bom dia pessoal")
    assert not looks_like_signal("quem tem conta na stmo?")


def test_history_limit_default_is_all_messages():
    """Sem env vars, o replay percorre o grupo desde o início (None = sem
    limite no Telethon; 0 devolveria zero mensagens)."""
    assert history_limit() is None
    assert history_limit("0", "") is None
    assert history_limit("", "0") is None


def test_history_limit_honours_historical_limit():
    assert history_limit("1000", "") == 1000
    assert history_limit("1000", "500") == 1000  # o novo nome manda
    assert history_limit("", "200") == 200       # alias antigo ainda funciona
    assert history_limit("abc", "") is None      # valor inválido -> tudo
    assert history_limit("-5", "") is None
    assert history_limit("  250  ", "") == 250


class _FakeMsg:
    def __init__(self, date):
        self.date = date


def test_msg_date_utc_converts_naive_to_utc():
    """msg.date (naive local no Telethon) → ISO UTC explícito (+00:00).

    Sem isto o replay do histórico enviava `received_at` naive e a dashboard
    interpretava a hora como local do browser (bug do countdown inflado).
    """
    naive = datetime(2026, 8, 18, 15, 30, 0)  # hora local da VPS
    iso = msg_date_utc(_FakeMsg(naive))
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None, f"timestamp naive: {iso!r}"
    assert dt.utcoffset().total_seconds() == 0, f"não está em UTC: {iso!r}"
    # o instante tem de ser o mesmo: 15:30 local == 15:30 - offset local em UTC
    expected = naive.astimezone().astimezone(timezone.utc).replace(tzinfo=None)
    assert dt.replace(tzinfo=None) == expected


def test_msg_date_utc_empty_when_no_date():
    assert msg_date_utc(_FakeMsg(None)) == ""
