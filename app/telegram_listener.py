"""Telegram listener — liga o bot aos grupos de sinais.

Processo separado que corre ao lado da API (uvicorn). Liga ao Telegram com a
TUA conta de utilizador (Telethon), ouve UM OU MAIS grupos e reencaminha cada
mensagem (e cada edição) para `POST /api/signals` com o `message_id` e o
`group`, para que a janela de edição se aplique e ids de mensagens de grupos
diferentes nunca colidam.

Configuração (ver docs/PUBLIC.md para o guia completo):

  SIGNALBOT_TG_API_ID     - api_id de https://my.telegram.org
  SIGNALBOT_TG_API_HASH   - api_hash de https://my.telegram.org
  SIGNALBOT_TG_PHONE      - o teu número (com indicativo, ex.: +351...)
  SIGNALBOT_TG_GROUPS     - grupos separados por vírgula (link, @username ou id)
  SIGNALBOT_TG_GROUP      - alternativa para um único grupo (backwards-compat)
  SIGNALBOT_API_URL       - URL da API (default http://127.0.0.1:8000)
  SIGNALBOT_TG_FILTER     - "0" para reencaminhar tudo; default filtra só sinais
  SIGNALBOT_TG_HISTORICAL_LIMIT - nº de mensagens a repor no arranque (0 = todas
                             desde o início do grupo; alias: SIGNALBOT_TG_HISTORY_LIMIT)

Corre com:  python -m app.telegram_listener
"""

import asyncio
import os
import re
import sys
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ImportChatInviteRequest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv é opcional
    pass

# Logs may contain emoji (✅, 🏆...) that crash on cp1252 consoles; force UTF-8
# so the process survives redirection to a file (nohup, services, etc.).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # non-reconfigurable stream: skip
    pass

from app.maintenance import configure_process_stdio
configure_process_stdio("listener")

API_ID = int(os.getenv("SIGNALBOT_TG_API_ID", "0"))
API_HASH = os.getenv("SIGNALBOT_TG_API_HASH", "")
PHONE = os.getenv("SIGNALBOT_TG_PHONE", "")
API_URL = os.getenv("SIGNALBOT_API_URL", "http://127.0.0.1:8000").rstrip("/")

# Um ou vários grupos: SIGNALBOT_TG_GROUPS="a,b" ou SIGNALBOT_TG_GROUP="a"
_GROUPS_RAW = os.getenv("SIGNALBOT_TG_GROUPS", "") or os.getenv("SIGNALBOT_TG_GROUP", "")
GROUPS = [g.strip() for g in _GROUPS_RAW.split(",") if g.strip()]

# Filtro leve: só reencaminha mensagens que pareçam sinais, para não inundar o
# bot com conversa do grupo. Põe SIGNALBOT_TG_FILTER=0 para reencaminhar tudo.
FILTER = os.getenv("SIGNALBOT_TG_FILTER", "1") != "0"
_KEYWORD = re.compile(
    r"\b(buy|sell|compr|vend|long|short|entr(?:ada)?|entry|stop\s*loss|stop|sl|"
    r"tp\s*[1-5]|tp[1-5]|alvo|target)\b",
    re.IGNORECASE,
)


def looks_like_signal(text: str) -> bool:
    return bool(_KEYWORD.search(text))


def reply_id_of(message) -> Optional[int]:
    """The Telegram message id this message replies to (None when it's not a
    reply). Telethon exposes it as `reply_to_msg_id` on the message or inside
    `reply_to` (MessageReplyHeader). This is how the bot knows a management
    message ("SL HIT") refers to a specific signal."""
    try:
        r = getattr(message, "reply_to", None)
        if r is not None:
            rid = getattr(r, "reply_to_msg_id", None) or getattr(r, "msg_id", None)
            if rid:
                return int(rid)
        rid = getattr(message, "reply_to_msg_id", None)
        return int(rid) if rid else None
    except (TypeError, ValueError):
        return None


def build_payload(text: str, message_id: int, group: str, source: str,
                  historical: bool = False, received_at: str = "",
                  reply_to_msg_id: Optional[int] = None) -> dict:
    payload = {
        "raw_text": text,
        "source": source,
        "message_id": str(message_id),
        "group": group,
    }
    if reply_to_msg_id is not None:
        payload["reply_to_msg_id"] = int(reply_to_msg_id)
    if historical:
        payload["historical"] = True
    if received_at:
        payload["received_at"] = received_at
    return payload


def signal_id_from(group: str, message_id: int) -> str:
    return f"tg-{group}-{message_id}"


def msg_date_utc(msg) -> str:
    """`msg.date` (naive local no Telethon) → ISO UTC explícito.

    O replay do histórico usa este valor como `received_at`; se chegasse
    naive, a dashboard interpretava a hora como local do browser e a
    contagem da janela de edição ficava deslocada.
    """
    if not getattr(msg, "date", None):
        return ""
    from datetime import timezone
    return msg.date.astimezone(timezone.utc).isoformat(timespec="seconds")


def _api_headers() -> dict:
    """Header de autenticação opcional (SIGNALBOT_API_TOKEN) para a API."""
    tok = os.getenv("SIGNALBOT_API_TOKEN", "")
    return {"X-API-Token": tok} if tok else {}


# Alerta no telemóvel quando o MT5 cair: o listener faz DM à própria conta
# (Saved Messages) usando a sessão Telethon já iniciada — sem bot, sem setup.
_last_offline_dm: float = 0.0
_last_mt5_state: Optional[bool] = None


async def mt5_watch(client: TelegramClient) -> None:
    """Polling ao servidor: quando o MT5 desliga (ou o AutoTrading desliga),
    envia um DM para a própria conta do Telegram. Com cooldown de 60s."""
    global _last_offline_dm, _last_mt5_state
    import time
    while True:
        await asyncio.sleep(10)
        try:
            async with httpx.AsyncClient(timeout=8) as http:
                r = await http.get(f"{API_URL}/api/diagnostics",
                                   headers=_api_headers())
                d = r.json()
            mt5 = d.get("mt5") or {}
            connected = bool(mt5.get("connected"))
            algo = bool(mt5.get("trade_allowed"))
            state = connected and algo
            prev = _last_mt5_state
            _last_mt5_state = state
            if prev is None:
                continue  # primeiro poll — só regista o estado
            if prev and not state and time.time() - _last_offline_dm > 60:
                _last_offline_dm = time.time()
                try:
                    await client.send_message(
                        "me",
                        "🚨 PROJECT NEXGEN — MT5 DESLIGADO\n"
                        "O terminal perdeu a ligação ou o AutoTrading desligou. "
                        "As trades abertas não são geridas até voltar.",
                    )
                    print("[alerta] DM enviado — MT5 offline")
                except Exception as exc:
                    print(f"[alerta] falha ao enviar DM: {exc}")
            elif not prev and state:
                try:
                    await client.send_message(
                        "me",
                        "✅ PROJECT NEXGEN — MT5 LIGADO\n"
                        "Ligação restabelecida e AutoTrading ativo.",
                    )
                except Exception:
                    pass
        except Exception:
            pass


async def forward(text: str, message_id: int, group: str, source: str,
                  historical: bool = False, received_at: str = "",
                  reply_to_msg_id: Optional[int] = None) -> Optional[dict]:
    """Reencaminha uma mensagem para a API e devolve a resposta (com o
    `action` decidido pelo backend — histórico/gestão/ignorada/...).
    None quando não há mensagem para enviar ou a API falhou."""
    if FILTER and not looks_like_signal(text):
        preview = (text or "").replace("\n", " ")[:90]
        print(f"[skip] {source} msg {message_id} não parece um sinal | {preview}")
        return None
    payload = build_payload(text, message_id, group, source, historical,
                            received_at, reply_to_msg_id)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(f"{API_URL}/api/signals", json=payload,
                                headers=_api_headers())
            data = r.json()
        print(f"[forward] {source} msg {message_id} -> {data.get('action', r.status_code)}")
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"[error] {source} msg {message_id}: {exc}")
        return None


async def notify_deleted(group: str, message_id: int, source: str) -> None:
    signal_id = signal_id_from(group, message_id)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(f"{API_URL}/api/signals/{signal_id}/delete",
                                headers=_api_headers())
            data = r.json()
        print(f"[deleted] {source} msg {message_id} -> {data.get('action', r.status_code)}")
    except Exception as exc:
        print(f"[error] delete {source} msg {message_id}: {exc}")


# ISO da última mensagem do grupo RECEBIDA pelo listener (atualizado nos
# handlers de evento e no fim do histórico) — enviado no heartbeat a cada
# segundo para a dashboard mostrar "última mensagem há Xs" e detetar o
# processo morto em silêncio (sem heartbeat há >5s = OFFLINE).
last_msg_at: str = ""


def _now_iso_short() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def report_need_code() -> None:
    """Avisa o servidor (e a dashboard) que o Telegram pede um código de
    login novo — o utilizador introduz o código na dashboard."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(f"{API_URL}/api/listener/need_code",
                            headers=_api_headers())
    except Exception:
        pass


async def poll_for_code(timeout_s: int = 900) -> str:
    """Pedir o código ao utilizador via dashboard: avisa o servidor e espera
    (polling a cada 2s) até o utilizador o introduzir."""
    await report_need_code()
    waited = 0
    while waited < timeout_s:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(f"{API_URL}/api/listener/code",
                                   headers=_api_headers())
                code = r.json().get("code")
                if code:
                    print("[auth] código do Telegram recebido da dashboard")
                    return str(code)
        except Exception:
            pass
        await asyncio.sleep(2)
        waited += 2
    raise SystemExit("sem código do Telegram (timeout) — sai")


async def heartbeat_loop() -> None:
    """Heartbeat a cada 1s: o backend marca o listener como vivo e recebe
    o momento da última mensagem do grupo (tab Diagnóstico)."""
    tick = 0
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            while True:
                tick += 1
                try:
                    await http.post(
                        f"{API_URL}/api/heartbeat",
                        json={"groups": GROUPS, "last_msg_at": last_msg_at,
                              "tick": tick},
                        headers=_api_headers(),
                    )
                except Exception:
                    pass
                await asyncio.sleep(1)
    except Exception:
        pass


def history_limit(historical_limit: str = "", history_alias: str = "") -> Optional[int]:
    """Limite do replay do histórico (SIGNALBOT_TG_HISTORICAL_LIMIT).

    0 (ou não definido) = TODAS as mensagens desde o início do grupo; >0 =
    só as últimas N. Devolve None para "sem limite" — o Telethon usa None
    para isso (0 no iter_messages devolveria ZERO mensagens).
    """
    raw = (historical_limit or history_alias or "0").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    return None if n <= 0 else n


async def load_history(client: TelegramClient, chat, limit: int = 0) -> None:
    """Envia os sinais passados do grupo para a API (histórico real).

    Por omissão percorre o grupo DESDE O INÍCIO (todas as mensagens) — o
    backtest e as páginas de teste usam o histórico completo, e mensagens
    antigas com formatos diferentes também queremos reconhecer. Configurável
    com SIGNALBOT_TG_HISTORICAL_LIMIT (nº de mensagens a percorrer; 0 = todas;
    SIGNALBOT_TG_HISTORY_LIMIT mantém-se como alias). No fim, avisa o servidor
    (POST /api/listener/history_done) para registar a notificação no feed.
    """
    if limit <= 0:
        limit = history_limit(
            os.getenv("SIGNALBOT_TG_HISTORICAL_LIMIT", ""),
            os.getenv("SIGNALBOT_TG_HISTORY_LIMIT", ""),
        )
    title = getattr(chat, "title", str(chat))
    source = f"tg:{title}"
    group = str(chat.id)
    count = 0
    counts = {"sinais": 0, "gestao": 0, "resumos": 0, "ignoradas": 0,
              "outras": 0}
    async for msg in client.iter_messages(chat, limit=limit):
        text = (msg.text or "").strip()
        if not text:
            continue
        if FILTER and not looks_like_signal(text):
            continue
        received_at = msg_date_utc(msg)
        data = await forward(text, msg.id, group, source, historical=True,
                             received_at=received_at,
                             reply_to_msg_id=reply_id_of(msg))
        if not data:
            continue
        count += 1
        action = data.get("action") or ""
        if action == "historical":
            counts["sinais"] += 1
        elif action == "management":
            counts["gestao"] += 1
        elif action == "summary":
            counts["resumos"] += 1
        elif action == "ignored":
            counts["ignoradas"] += 1
        else:
            counts["outras"] += 1
    print(f"[history] {title}: {count} mensagens do histórico reencaminhadas "
          f"(sinais {counts['sinais']} · gestão {counts['gestao']} · "
          f"ignoradas {counts['ignoradas']})")
    if count:
        await notify_history_done(title, count, counts)


async def notify_history_done(group_title: str, total: int, counts: dict) -> None:
    """Avisa o servidor que o replay do histórico terminou — o servidor
    regista uma notificação no feed com os totais reencaminhados."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                f"{API_URL}/api/listener/history_done",
                json={"group": group_title, "total": total, **counts},
                headers=_api_headers(),
            )
        print(f"[history] fim do replay notificado ({total} mensagens)")
    except Exception as exc:
        print(f"[history] falha ao notificar fim do replay: {exc}")


async def resolve_chat(client: TelegramClient, setting: str):
    """Resolve uma definição de grupo para uma entidade.

    Aceita, por ordem: link de convite (t.me/+hash ou joinchat), @username,
    id numérico, link público, ou — em último caso — procura o chat pelo
    NOME nos teus chats (ex.: "Golden Moves").
    """
    setting = setting.strip()

    # Link de convite privado: https://t.me/+<hash> ou t.me/joinchat/<hash>
    m = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", setting)
    if m:
        try:
            return await client.get_entity(setting)
        except Exception:
            result = await client(ImportChatInviteRequest(hash=m.group(1)))
            return result.chats[0]

    # @username, id numérico ou link público
    try:
        return await client.get_entity(setting)
    except Exception:
        pass

    # Fallback: procurar pelo nome nos diálogos (ex.: "Golden Moves")
    async for dialog in client.iter_dialogs():
        title = getattr(dialog.entity, "title", "") or dialog.name or ""
        if setting.lower() in title.lower():
            return dialog.entity

    raise ValueError(f"grupo '{setting}' não encontrado (link, username ou nome)")


def register_chat(client: TelegramClient, chat) -> None:
    """Regista os handlers de mensagens novas/edições/apagadas para um chat."""
    title = getattr(chat, "title", str(chat))
    source = f"tg:{title}"
    group = str(chat.id)
    print(f"✅ A ouvir: {title}")

    @client.on(events.NewMessage(chats=chat.id))
    async def on_new(event):
        global last_msg_at
        last_msg_at = _now_iso_short()
        text = (event.message.text or "").strip()
        if text:
            await forward(text, event.message.id, group, source,
                          reply_to_msg_id=reply_id_of(event.message))

    @client.on(events.MessageEdited(chats=chat.id))
    async def on_edit(event):
        global last_msg_at
        last_msg_at = _now_iso_short()
        text = (event.message.text or "").strip()
        if text:
            await forward(text, event.message.id, group, source,
                          reply_to_msg_id=reply_id_of(event.message))

    @client.on(events.MessageDeleted(chats=chat.id))
    async def on_delete(event):
        for mid in event.deleted_ids:
            await notify_deleted(group, mid, source)


async def main() -> None:
    if not (API_ID and API_HASH and PHONE):
        raise SystemExit(
            "Faltam credenciais. Define SIGNALBOT_TG_API_ID, SIGNALBOT_TG_API_HASH "
            "e SIGNALBOT_TG_PHONE (ver docs/PUBLIC.md)."
        )
    if not GROUPS:
        raise SystemExit(
            "Nenhum grupo definido. Preenche SIGNALBOT_TG_GROUPS (ou SIGNALBOT_TG_GROUP)."
        )

    # O heartbeat arranca ANTES da auth/histórico — o watchdog do servidor
    # só reinicia o listener quando o heartbeat parar, e um arranque normal
    # (auth + histórico) demora ~40s sem mensagens.
    asyncio.create_task(heartbeat_loop())

    client = TelegramClient("signalbot_session", API_ID, API_HASH)
    # Se a sessão existir e for válida, `start` nem pede código. Se o Telegram
    # exigir login novo (sessão expirada / dispositivo novo), o code_callback
    # pede o código ao utilizador através da dashboard (nunca stdin).
    await client.start(phone=PHONE, code_callback=poll_for_code)
    # Alerta no telemóvel quando o MT5 cair (DM à própria conta) — o client
    # já está ligado aqui, por isso as mensagens podem ser enviadas.
    asyncio.create_task(mt5_watch(client))

    for setting in GROUPS:
        try:
            chat = await resolve_chat(client, setting)
        except Exception as exc:
            print(f"[erro] não consegui aceder a '{setting}': {exc}")
            continue
        register_chat(client, chat)
        if os.getenv("SIGNALBOT_TG_HISTORY", "1") != "0":
            await load_history(client, chat)
        # Nota: `last_msg_at` NÃO é atualizado aqui — o replay do histórico não
        # conta como "mensagem nova". Só os handlers on_new/on_edit o atualizam,
        # para a dashboard mostrar a idade real da última mensagem do grupo e
        # não o tempo de arranque do listener.

    if not client.list_event_handlers():
        print("Nenhum grupo acessível — confirma os links e que a conta está neles.")
    else:
        print(f"A aguardar mensagens em {len(GROUPS)} grupo(s)... (Ctrl+C para parar)")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
