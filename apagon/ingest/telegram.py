"""Ingesta desde canales/grupos públicos de Telegram con Telethon (cuenta de usuario).

- backfill(days): descarga el historial y lo guarda (permite entrenar desde el día 1).
- listen(): escucha mensajes nuevos en tiempo real (proceso largo, systemd).

La primera ejecución pide el número de teléfono y el código de login por consola y crea
el archivo de sesión. Hazla de forma interactiva antes de activar el servicio.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..classify import Geocoder
from ..config import get_settings
from ..db import active_channels, connect, normalize_channel
from .store import RawMessage, store_messages

log = logging.getLogger(__name__)
BATCH = 500


def _client():
    from telethon import TelegramClient

    s = get_settings()
    if not s.tg_api_id or not s.tg_api_hash:
        raise SystemExit("Faltan TELEGRAM_API_ID / TELEGRAM_API_HASH en .env (obtenlos en my.telegram.org).")
    s.tg_session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(s.tg_session), s.tg_api_id, s.tg_api_hash)


def _author_key(msg, channel: str) -> str:
    """Identidad del autor para contar reportantes distintos.

    - Reenvío: el autor original (así diez reenvíos del mismo reporte cuentan como uno).
    - Post de canal tipo broadcast: cada post cuenta como fuente independiente, porque el
      remitente es siempre el propio canal (los admins suelen republicar reportes de terceros).
    - Grupo: el usuario que escribe.
    """
    from telethon import utils

    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None and getattr(fwd, "from_id", None) is not None:
        return f"fwd:{utils.get_peer_id(fwd.from_id)}"
    if getattr(msg, "post", False):
        return f"post:{channel}:{msg.id}"
    return f"user:{msg.sender_id}"


def _to_raw(msg, channel: str, default_region: str | None) -> RawMessage | None:
    text = getattr(msg, "message", None) or ""
    if not text.strip():
        return None
    return RawMessage(
        source="telegram",
        source_msg_id=f"{channel}:{msg.id}",
        channel=channel,
        author_key=_author_key(msg, channel),
        posted_at=msg.date,
        text=text,
        default_region=default_region,
    )


async def _backfill(days: int) -> int:
    conn = connect()
    geocoder = Geocoder.from_db(conn)
    channels = active_channels(conn)
    if not channels:
        raise SystemExit("No hay canales en config/channels.csv. Agrega al menos uno y corre init-db.")
    since = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    async with _client() as client:
        for channel, default_region in channels.items():
            batch: list[RawMessage] = []
            n_channel = 0
            try:
                async for msg in client.iter_messages(channel, offset_date=since, reverse=True):
                    raw = _to_raw(msg, channel, default_region)
                    if raw:
                        batch.append(raw)
                    if len(batch) >= BATCH:
                        n_channel += store_messages(conn, batch, geocoder)
                        batch.clear()
                n_channel += store_messages(conn, batch, geocoder)
            except Exception as exc:  # un canal caído no debe tumbar el backfill completo
                log.warning("Canal %s omitido: %s", channel, exc)
                continue
            log.info("Canal %s: %d mensajes nuevos", channel, n_channel)
            total += n_channel
    conn.close()
    return total


def backfill(days: int = 90) -> int:
    return asyncio.run(_backfill(days))


async def _listen() -> None:
    from telethon import events

    conn = connect()
    geocoder = Geocoder.from_db(conn)
    channels = active_channels(conn)
    if not channels:
        raise SystemExit("No hay canales en config/channels.csv. Agrega al menos uno y corre init-db.")
    client = _client()

    @client.on(events.NewMessage(chats=list(channels)))
    async def handler(event) -> None:
        chat = await event.get_chat()
        channel = normalize_channel(getattr(chat, "username", None) or str(event.chat_id))
        raw = _to_raw(event.message, channel, channels.get(channel))
        if raw and store_messages(conn, [raw], geocoder):
            log.debug("Nuevo mensaje de %s", channel)

    async with client:
        log.info("Escuchando %d canales…", len(channels))
        await client.run_until_disconnected()


def listen() -> None:
    asyncio.run(_listen())
