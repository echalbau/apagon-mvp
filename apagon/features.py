"""Etiquetas y features. Una sola función (build_features) sirve para entrenar y para predecir,
así no hay diferencias entre lo que ve el modelo en entrenamiento y en producción.

Unidad de predicción: (estado, hora UTC). Objetivo: ¿habrá una falla observable en las
próximas H horas (h+1 … h+H)?
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .config import get_settings
from .db import floor_hour, read_df, state_ids, to_ts

HYDRO_REGION = "GURI"
WARMUP_DAYS = 95  # historia previa necesaria para ventanas de 90 días (Guri)
MAX_GAP_H = 720.0

SOCIAL_FEATURES = [
    "n_authors",
    "n_authors_3h",
    "n_restore",
    "ev_3h",
    "ev_24h",
    "ev_168h",
    "lag24",
    "hours_since_event",
    "nat_regions_now",
    "nat_ev_24h",
]
WEATHER_COLUMNS = ["temp_c", "apparent_temp_c", "rh", "precip_mm", "gusts_kmh"]
WEATHER_FEATURES = [*WEATHER_COLUMNS, "app_temp_next_max", "app_temp_anom"]
HYDRO_FEATURES = ["guri_precip_30d", "guri_precip_90d"]
FEATURES = ["region", "local_hour", "dow", *SOCIAL_FEATURES, *WEATHER_FEATURES, *HYDRO_FEATURES]


def aggregate_region_hour(conn: sqlite3.Connection, since: datetime | None = None) -> int:
    """Recalcula region_hour (completo, o desde `since`). Etiqueta = 1 si al menos
    MIN_AUTHORS autores distintos reportaron corte/bajón, o restablecimiento, en esa hora."""
    k = get_settings().min_authors
    where = "WHERE region_id IS NOT NULL AND event_type IS NOT NULL"
    params: list = [k, k]
    if since is None:
        conn.execute("DELETE FROM region_hour")
    else:
        since_ts = to_ts(floor_hour(since))
        conn.execute("DELETE FROM region_hour WHERE hour_ts >= ?", (since_ts,))
        where += " AND posted_at >= ?"
        params.append(since_ts)
    cur = conn.execute(
        f"""
        INSERT INTO region_hour (region_id, hour_ts, n_reports, n_authors, n_restore, label)
        SELECT region_id, hour_ts, n_reports, n_authors, n_restore,
               CASE WHEN n_authors >= ? OR n_restore >= ? THEN 1 ELSE 0 END
        FROM (
          SELECT region_id,
                 strftime('%Y-%m-%d %H:00:00', posted_at) AS hour_ts,
                 COUNT(*) AS n_reports,
                 COUNT(DISTINCT CASE WHEN event_type IN ('outage', 'dip') THEN author_hash END) AS n_authors,
                 COUNT(DISTINCT CASE WHEN event_type = 'restore' THEN author_hash END) AS n_restore
          FROM social_reports
          {where}
          GROUP BY region_id, hour_ts
        )
        """,
        params,
    )
    conn.commit()
    return cur.rowcount


def _grid(region_ids: list[str], start: datetime, end: datetime) -> pd.DataFrame:
    hours = pd.date_range(start, end, freq="h")
    index = pd.MultiIndex.from_product([region_ids, hours], names=["region_id", "hour"])
    return index.to_frame(index=False)


def load_region_hour(conn: sqlite3.Connection, start: datetime, end: datetime) -> pd.DataFrame:
    df = read_df(
        conn,
        "SELECT region_id, hour_ts, n_authors, n_restore, label FROM region_hour WHERE hour_ts BETWEEN ? AND ?",
        (to_ts(start), to_ts(end)),
    )
    df["hour"] = pd.to_datetime(df.pop("hour_ts"))
    return df


def load_weather(conn: sqlite3.Connection, start: datetime, end: datetime) -> pd.DataFrame:
    """Clima por región-hora; si hay dato observado y pronosticado para la misma hora, gana el observado."""
    df = read_df(
        conn,
        f"SELECT region_id, ts, is_forecast, {', '.join(WEATHER_COLUMNS)} FROM weather_hourly WHERE ts BETWEEN ? AND ?",
        (to_ts(start), to_ts(end)),
    )
    df["hour"] = pd.to_datetime(df.pop("ts"))
    df = df.sort_values(["region_id", "hour", "is_forecast"]).drop_duplicates(["region_id", "hour"], keep="first")
    return df.drop(columns="is_forecast")


def _forward_max(x: pd.Series, h: int, min_periods: int) -> pd.Series:
    """max(x[t+1 … t+h]) por posición."""
    return x[::-1].rolling(h, min_periods=min_periods).max()[::-1].shift(-1)


def _social_features(conn, states: list[str], warm: datetime, end: datetime, horizon_h: int) -> pd.DataFrame:
    df = _grid(states, warm, end).merge(load_region_hour(conn, warm, end), on=["region_id", "hour"], how="left")
    df[["n_authors", "n_restore", "label"]] = df[["n_authors", "n_restore", "label"]].fillna(0)
    df = df.sort_values(["region_id", "hour"], ignore_index=True)
    g = df.groupby("region_id", sort=False)

    def rolling_sum(col: str, window: int) -> pd.Series:
        return g[col].transform(lambda x: x.rolling(window, min_periods=1).sum())

    df["n_authors_3h"] = rolling_sum("n_authors", 3)
    df["ev_3h"] = rolling_sum("label", 3)
    df["ev_24h"] = rolling_sum("label", 24)
    df["ev_168h"] = rolling_sum("label", 168)
    df["lag24"] = g["label"].shift(24)

    t_h = (df["hour"] - warm) / pd.Timedelta(hours=1)
    last_event = t_h.where(df["label"] > 0).groupby(df["region_id"]).ffill()
    df["hours_since_event"] = (t_h - last_event).fillna(MAX_GAP_H).clip(upper=MAX_GAP_H)

    # Objetivo: falla observable en (t, t+H]. NaN cuando el futuro aún no está completo.
    df["target"] = g["label"].transform(lambda x: _forward_max(x, horizon_h, horizon_h))

    national = df.groupby("hour")["label"].sum().sort_index()
    nat = pd.DataFrame(
        {
            "hour": national.index,
            "nat_regions_now": national.to_numpy(),
            "nat_ev_24h": national.rolling(24, min_periods=1).sum().to_numpy(),
        }
    )
    return df.merge(nat, on="hour", how="left")


def _weather_features(conn, states: list[str], warm: datetime, end_ext: datetime, horizon_h: int):
    regions = [*states, HYDRO_REGION]
    w = _grid(regions, warm, end_ext).merge(load_weather(conn, warm, end_ext), on=["region_id", "hour"], how="left")
    w = w.sort_values(["region_id", "hour"], ignore_index=True)
    g = w.groupby("region_id", sort=False)["apparent_temp_c"]
    w["app_temp_next_max"] = g.transform(lambda x: _forward_max(x, horizon_h, 1))
    w["app_temp_anom"] = w["apparent_temp_c"] - g.transform(lambda x: x.rolling(168, min_periods=24).mean())

    guri = w.loc[w["region_id"] == HYDRO_REGION].set_index("hour")["precip_mm"].sort_index()
    hydro = pd.DataFrame(
        {
            "hour": guri.index,
            "guri_precip_30d": guri.rolling(30 * 24, min_periods=24).sum().to_numpy(),
            "guri_precip_90d": guri.rolling(90 * 24, min_periods=24).sum().to_numpy(),
        }
    )
    states_w = w.loc[w["region_id"] != HYDRO_REGION, ["region_id", "hour", *WEATHER_FEATURES]]
    return states_w, hydro


def build_features(
    conn: sqlite3.Connection, start: datetime, end: datetime, horizon_h: int | None = None
) -> pd.DataFrame:
    """Matriz de features para cada (estado, hora) en [start, end] + columna `target`."""
    s = get_settings()
    h = horizon_h or s.horizon_h
    start, end = floor_hour(start), floor_hour(end)
    warm = start - timedelta(days=WARMUP_DAYS)
    states = state_ids(conn)

    social = _social_features(conn, states, warm, end, h)
    weather, hydro = _weather_features(conn, states, warm, end + timedelta(hours=h), h)

    df = social.merge(weather, on=["region_id", "hour"], how="left").merge(hydro, on="hour", how="left")
    local = df["hour"] + pd.Timedelta(hours=s.utc_offset_h)
    df["local_hour"] = local.dt.hour.astype(np.int16)
    df["dow"] = local.dt.dayofweek.astype(np.int16)
    df["region"] = pd.Categorical(df["region_id"], categories=states)

    df = df.loc[df["hour"] >= start].reset_index(drop=True)
    return df[["region_id", "hour", *FEATURES, "target"]]
