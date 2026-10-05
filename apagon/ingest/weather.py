"""Clima horario desde Open-Meteo (sin API key; gratis para uso no comercial).

- fetch_forecast(): últimas horas + pronóstico (cron horario).
- backfill(days): archivo histórico (ERA5) + relleno de los últimos días con el endpoint de pronóstico.

Las variables son las mismas en ambos endpoints para no crear diferencias entre entrenamiento
y producción.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

import httpx

from ..db import floor_hour, to_ts, utcnow

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = {
    "temperature_2m": "temp_c",
    "apparent_temperature": "apparent_temp_c",
    "relative_humidity_2m": "rh",
    "precipitation": "precip_mm",
    "wind_gusts_10m": "gusts_kmh",
}
CHUNK = 10
ARCHIVE_LAG_DAYS = 7

UPSERT_SQL = """
INSERT INTO weather_hourly
  (region_id, ts, is_forecast, temp_c, apparent_temp_c, rh, precip_mm, gusts_kmh, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(region_id, ts, is_forecast) DO UPDATE SET
  temp_c=excluded.temp_c, apparent_temp_c=excluded.apparent_temp_c, rh=excluded.rh,
  precip_mm=excluded.precip_mm, gusts_kmh=excluded.gusts_kmh, fetched_at=excluded.fetched_at
"""


def _points(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT region_id, lat, lon FROM regions ORDER BY region_id").fetchall()


def _request(url: str, points: list, extra: dict) -> list[dict]:
    params = {
        "latitude": ",".join(f"{p['lat']:.4f}" for p in points),
        "longitude": ",".join(f"{p['lon']:.4f}" for p in points),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "GMT",
        **extra,
    }
    resp = httpx.get(url, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else [data]


def parse_payloads(payloads: list[dict], region_ids: list[str], now: datetime, fetched_at: str) -> list[tuple]:
    """Convierte respuestas de Open-Meteo en filas para weather_hourly."""
    cutoff = floor_hour(now)
    rows = []
    for region_id, payload in zip(region_ids, payloads, strict=True):
        hourly = payload["hourly"]
        times = hourly["time"]
        columns = [hourly.get(var) or [None] * len(times) for var in HOURLY_VARS]
        for i, t in enumerate(times):
            values = [col[i] for col in columns]
            if all(v is None for v in values):
                continue
            ts = datetime.fromisoformat(t)
            rows.append((region_id, to_ts(ts), int(ts > cutoff), *values, fetched_at))
    return rows


def _fetch(conn: sqlite3.Connection, url: str, extra: dict) -> int:
    points = _points(conn)
    now = utcnow()
    fetched_at = to_ts(now)
    total = 0
    for i in range(0, len(points), CHUNK):
        chunk = points[i : i + CHUNK]
        payloads = _request(url, chunk, extra)
        rows = parse_payloads(payloads, [p["region_id"] for p in chunk], now, fetched_at)
        conn.executemany(UPSERT_SQL, rows)
        total += len(rows)
    conn.commit()
    return total


def fetch_forecast(conn: sqlite3.Connection, past_days: int = 2, forecast_days: int = 2) -> int:
    n = _fetch(conn, FORECAST_URL, {"past_days": past_days, "forecast_days": forecast_days})
    log.info("Clima: %d filas actualizadas", n)
    return n


def backfill(conn: sqlite3.Connection, days: int) -> int:
    today = date.today()
    start = today - timedelta(days=days)
    archive_end = today - timedelta(days=ARCHIVE_LAG_DAYS)
    total = 0
    if start <= archive_end:
        total += _fetch(
            conn,
            ARCHIVE_URL,
            {"start_date": start.isoformat(), "end_date": archive_end.isoformat()},
        )
    total += fetch_forecast(conn, past_days=min(days, ARCHIVE_LAG_DAYS + 2), forecast_days=2)
    log.info("Backfill de clima: %d filas", total)
    return total
