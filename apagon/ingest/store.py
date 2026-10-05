"""Persistencia de mensajes sociales: clasifica, geocodifica, anonimiza autor e inserta."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..classify import PARSER_VERSION, Geocoder, parse_message
from ..config import get_settings
from ..db import active_channels, to_ts


@dataclass
class RawMessage:
    source: str
    source_msg_id: str
    channel: str | None
    author_key: str | None
    posted_at: datetime
    text: str
    default_region: str | None = None


def hash_author(author_key: str | None, salt: str) -> str | None:
    if not author_key:
        return None
    return hashlib.sha256(f"{salt}:{author_key}".encode()).hexdigest()[:16]


INSERT_SQL = """
INSERT OR IGNORE INTO social_reports
  (source, source_msg_id, channel, author_hash, posted_at, text, event_type, region_id, parser_version)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def store_messages(conn: sqlite3.Connection, messages: Iterable[RawMessage], geocoder: Geocoder) -> int:
    """Inserta mensajes (idempotente por source+source_msg_id). Devuelve cuántos son nuevos."""
    salt = get_settings().author_salt
    rows = []
    for m in messages:
        if not m.text or not m.text.strip():
            continue
        event_type, region_id = parse_message(m.text, geocoder, m.default_region)
        rows.append(
            (
                m.source,
                m.source_msg_id,
                m.channel,
                hash_author(m.author_key, salt),
                to_ts(m.posted_at),
                m.text,
                event_type,
                region_id,
                PARSER_VERSION,
            )
        )
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(INSERT_SQL, rows)
    conn.commit()
    return conn.total_changes - before


def reprocess(conn: sqlite3.Connection, only_stale: bool = True) -> int:
    """Reclasifica mensajes guardados con el parser y el gazetteer actuales."""
    geocoder = Geocoder.from_db(conn)
    defaults = active_channels(conn)
    sql = "SELECT id, channel, text FROM social_reports"
    params: tuple = ()
    if only_stale:
        sql += " WHERE parser_version != ?"
        params = (PARSER_VERSION,)
    updates = []
    for row in conn.execute(sql, params).fetchall():
        event_type, region_id = parse_message(row["text"], geocoder, defaults.get(row["channel"]))
        updates.append((event_type, region_id, PARSER_VERSION, row["id"]))
    conn.executemany(
        "UPDATE social_reports SET event_type=?, region_id=?, parser_version=? WHERE id=?", updates
    )
    conn.commit()
    return len(updates)
