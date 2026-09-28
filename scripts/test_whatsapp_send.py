"""Teste manual do caminho REAL de produção do WhatsApp.

Envia uma mensagem UTF-8 (emoji + acentos) para o grupo configurado via
`WhatsAppNotifier` (httpx -> bridge Baileys) — o MESMO caminho que o hook de
fecho de trade usa em `state.py`. O objetivo é validar que o texto chega ao
grupo sem corrupção de encoding.

Uso:
    .venv/Scripts/python scripts/test_whatsapp_send.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB  # noqa: E402
from app.notify.whatsapp import WhatsAppNotifier  # noqa: E402

JID = os.getenv("WHATSAPP_GROUP_JID", "120363409845259131@g.us")
TEXT = (
    "🧪 Project NexGen — teste UTF-8 do pipeline (ç ã é õ í) 🚀\n"
    "Se vires esta linha sem caracteres estranhos, está tudo OK."
)


async def main() -> bool:
    wa = WhatsAppNotifier(DB(":memory:"), enabled="1", group_jid=JID)
    ok = await wa.send(TEXT)
    print("enviado:", ok)
    print("texto:", repr(TEXT))
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
