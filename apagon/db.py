"""Conexión SQLite, inicialización del esquema y utilidades de tiempo (todo en UTC naive)."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .classify import normalize
from .config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
TS_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ts(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(TS_FMT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT)


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path or get_settings().db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def read_df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    columns = [c[0] for c in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=columns)


def _read_csv(path: Path) -> list[dict[str, str]]:
    """CSV que admite líneas de comentario con '#'."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def normalize_channel(channel: str) -> str:
    return channel.strip().lstrip("@").lower()


def init_db(conn: sqlite3.Connection, config_dir: Path | None = None) -> None:
    """Crea tablas (idempotente) y sincroniza regiones, alias y canales desde config/."""
    cfg = Path(config_dir or get_settings().config_dir)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    regions = _read_csv(cfg / "regions.csv")
    conn.executemany(
        """INSERT INTO regions(region_id, name, lat, lon, kind)
           VALUES (:region_id, :name, :lat, :lon, :kind)
           ON CONFLICT(region_id) DO UPDATE SET
             name=excluded.name, lat=excluded.lat, lon=excluded.lon, kind=excluded.kind""",
        regions,
    )
    aliases = [(normalize(r["alias"]), r["region_id"].strip()) for r in _read_csv(cfg / "aliases.csv")]
    conn.executemany("INSERT OR REPLACE INTO region_aliases(alias, region_id) VALUES (?, ?)", aliases)

    channels = [
        (normalize_channel(r["channel"]), (r.get("default_region") or "").strip() or None)
        for r in _read_csv(cfg / "channels.csv")
        if r.get("channel", "").strip()
    ]
    conn.executemany(
        """INSERT INTO channels(channel, default_region) VALUES (?, ?)
           ON CONFLICT(channel) DO UPDATE SET default_region=excluded.default_region""",
        channels,
    )
    conn.commit()


def state_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT region_id FROM regions WHERE kind='state' ORDER BY region_id")]


def region_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT region_id, name FROM regions")}


def active_channels(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {r[0]: r[1] for r in conn.execute("SELECT channel, default_region FROM channels WHERE active=1")}
