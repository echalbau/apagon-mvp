"""Comandos del bot e ingesta de Telegram con objetos falsos (sin red ni credenciales)."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from apagon import bot
from apagon.ingest.telegram import _to_raw


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def _call(handler, args, chat_id=42):
    msg = FakeMessage()
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(handler(update, SimpleNamespace(args=args)))
    return msg.replies[-1]


def test_subscription_flow(conn):
    assert "Zulia" in _call(bot.cmd_suscribir, ["zul"])
    assert "60%" in _call(bot.cmd_suscribir, ["MER", "60%"])
    assert "Umbral inválido" in _call(bot.cmd_suscribir, ["MER", "abc"])
    assert "No conozco" in _call(bot.cmd_suscribir, ["XXX"])
    rows = conn.execute("SELECT region_id, threshold FROM subscribers WHERE chat_id=42 ORDER BY region_id").fetchall()
    assert [tuple(r) for r in rows] == [("MER", 0.6), ("ZUL", None)]
    assert "sin pronóstico" in _call(bot.cmd_estado, [])
    _call(bot.cmd_desuscribir, ["todas"])
    assert conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 0


def _tg_message(**kw):
    base = dict(id=7, message="Se fue la luz", date=datetime(2026, 10, 1, tzinfo=timezone.utc),
                fwd_from=None, post=False, sender_id=555)
    return SimpleNamespace(**{**base, **kw})


def test_telegram_author_identity():
    group = _to_raw(_tg_message(), "reportes_zulia", "ZUL")
    assert group.author_key == "user:555" and group.default_region == "ZUL"
    assert group.source_msg_id == "reportes_zulia:7"

    post = _to_raw(_tg_message(post=True), "canal_nacional", None)
    assert post.author_key == "post:canal_nacional:7"

    from telethon.tl.types import MessageFwdHeader, PeerUser

    fwd = _to_raw(_tg_message(fwd_from=MessageFwdHeader(date=datetime.now(timezone.utc), from_id=PeerUser(99))), "c", None)
    assert fwd.author_key == "fwd:99"

    assert _to_raw(_tg_message(message=""), "c", None) is None
