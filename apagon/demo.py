"""Mundo sintético para probar el pipeline de punta a punta sin credenciales ni red.

Simula clima por estado, un embalse (Guri) con una sequía, fallas por calor, tormentas,
racionamientos en el occidente, eventos nacionales en cascada y bajones precursores. Luego
genera mensajes de Telegram en español venezolano que pasan por el MISMO clasificador y
geocodificador que los datos reales. Los mensajes se publican con más frecuencia al volver
la luz que durante el corte, como ocurre en la realidad.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from .classify import Geocoder
from .config import get_settings
from .db import TS_FMT, floor_hour, state_ids, to_ts, utcnow
from .ingest.store import RawMessage, store_messages
from .ingest.weather import UPSERT_SQL

log = logging.getLogger(__name__)

# (riesgo base de falla por hora, factor de reporte en redes, temperatura media °C)
PROFILE = {
    "ZUL": (0.020, 1.3, 29.5), "TAC": (0.012, 0.9, 22.0), "MER": (0.012, 0.8, 20.5),
    "TRU": (0.010, 0.6, 23.0), "BAR": (0.010, 0.7, 27.5), "LAR": (0.008, 1.0, 25.5),
    "FAL": (0.008, 0.7, 28.5), "POR": (0.008, 0.6, 27.5), "YAR": (0.007, 0.6, 26.5),
    "CAR": (0.006, 1.1, 26.0), "ARA": (0.006, 1.0, 25.5), "MON": (0.007, 0.7, 27.0),
    "ANZ": (0.006, 0.8, 27.5), "SUC": (0.006, 0.6, 27.5), "NES": (0.006, 0.6, 28.0),
    "BOL": (0.005, 0.7, 28.0), "GUA": (0.006, 0.5, 28.0), "COJ": (0.006, 0.4, 27.5),
    "APU": (0.007, 0.4, 28.5), "AMA": (0.008, 0.3, 27.5), "DAM": (0.008, 0.3, 27.5),
    "MIR": (0.003, 1.2, 24.0), "LGU": (0.003, 0.7, 28.0), "DC": (0.002, 1.5, 23.0),
}
DEFAULT_PROFILE = (0.006, 0.6, 27.0)
RATIONED = ("ZUL", "TAC", "MER", "TRU", "BAR")

TEXTS = {
    "outage": [
        "Se fue la luz en {place}",
        "{place} sin luz desde hace rato",
        "Apagón en {place} otra vez",
        "Otra vez nos quitaron la luz aquí en {place}",
        "Corte eléctrico en {place}, ¿alguien más?",
        "Se fue la luz",
        "Sin luz por aquí también",
    ],
    "dip": [
        "Bajón fuertísimo en {place}",
        "Fluctuaciones de voltaje en {place}, desconecten los equipos",
        "Puros bajones en {place} hoy",
        "Qué bajón tan feo acaba de dar",
        "Parpadea la luz en {place}, ojalá no se vaya",
    ],
    "restore": [
        "Llegó la luz en {place}",
        "Volvió la luz en {place} después de {h} horas",
        "Ya pusieron la luz en {place}",
        "Llegó la luz por fin",
        "Ya hay luz aquí, {h} horas sin servicio",
    ],
    "noise": [
        "¿Alguien sabe si hay gasolina en {place}?",
        "Buenos días {place}",
        "Hoy hace un calor terrible en {place}",
        "Cola larguísima en el banco de {place}",
        "¿Dónde venden hielo en {place}?",
    ],
}


def _ar1(rng: np.random.Generator, n: int, phi: float, sd: float) -> np.ndarray:
    eps = rng.normal(0.0, sd, n)
    x = np.empty(n)
    x[0] = eps[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + eps[i]
    return x


def _synth_weather(rng, local_hour, doy, month, base_temp, rain_mult) -> dict[str, np.ndarray]:
    n = len(local_hour)
    diurnal = np.sin(2 * np.pi * (local_hour - 9) / 24)
    temp = base_temp + 4.5 * diurnal + 1.2 * np.sin(2 * np.pi * (doy - 100) / 365) + _ar1(rng, n, 0.97, 0.35)
    rh = np.clip(74 - 15 * diurnal + _ar1(rng, n, 0.9, 2.5), 25, 100)
    rainy = (month >= 5) & (month <= 11)
    p_rain = np.where(rainy, 0.07, 0.015) * (1 + 1.5 * np.exp(-(((local_hour - 16) / 3) ** 2))) * rain_mult
    precip = np.where(rng.random(n) < p_rain, rng.exponential(4.0, n), 0.0)
    gusts = 16 + 6 * np.clip(diurnal, 0, None) + rng.gamma(2.0, 4.0, n) + 2.5 * precip
    vapour = rh / 100 * 6.105 * np.exp(17.27 * temp / (237.7 + temp))
    apparent = temp + 0.33 * vapour - 0.7 * (gusts / 3.6 / 1.6) - 4.0
    return {"temp_c": temp, "apparent_temp_c": apparent, "rh": rh, "precip_mm": precip, "gusts_kmh": gusts}


def _weather_rows(rng, region_id, ts_str, observed, wx, fetched_at) -> list[tuple]:
    cols = ["temp_c", "apparent_temp_c", "rh", "precip_mm", "gusts_kmh"]
    values = np.column_stack([wx[c] for c in cols])
    forecast_noise = rng.normal(0, [1.0, 1.2, 4.0, 0.5, 3.0], values.shape)
    rows = []
    for i, ts in enumerate(ts_str):
        if observed[i]:
            rows.append((region_id, ts, 0, *map(float, values[i]), fetched_at))
        else:
            v = values[i] + forecast_noise[i]
            v[3] = max(v[3], 0.0)
            rows.append((region_id, ts, 1, *map(float, v), fetched_at))
    return rows


def _intervals(mask: np.ndarray) -> list[tuple[int, int]]:
    """Tramos contiguos True como (inicio, fin_exclusivo)."""
    padded = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return list(zip(edges[::2], edges[1::2]))


def generate(conn, days: int = 180, seed: int = 7) -> dict[str, int]:
    s = get_settings()
    rng = np.random.default_rng(seed)
    now = utcnow()
    end = floor_hour(now)
    social_start = end - timedelta(days=days)
    weather_start = social_start - timedelta(days=95)
    hours = pd.date_range(weather_start, end + timedelta(hours=s.horizon_h), freq="h")
    n = len(hours)
    ts_str = hours.strftime(TS_FMT).tolist()
    local = hours + pd.Timedelta(hours=s.utc_offset_h)
    lh, doy, month = local.hour.to_numpy(), local.dayofyear.to_numpy(), local.month.to_numpy()
    observed = np.asarray(hours <= pd.Timestamp(end))
    social_window = observed & np.asarray(hours >= pd.Timestamp(social_start))
    fetched_at = to_ts(now)

    # Embalse de Guri: una sequía de 50 días a mitad de la serie dispara el estrés del sistema.
    drought = np.zeros(n, dtype=bool)
    d0 = int(n * 0.55)
    drought[d0 : d0 + 50 * 24] = True
    guri = _synth_weather(rng, lh, doy, month, 27.0, np.where(drought, 0.15, 1.6))
    p60 = pd.Series(guri["precip_mm"]).rolling(60 * 24, min_periods=24).sum()
    z = ((p60 - p60.mean()) / p60.std()).fillna(0).to_numpy()
    hydro = np.clip(np.exp(-0.6 * z), 0.6, 2.5)
    weather_rows = _weather_rows(rng, "GURI", ts_str, observed, guri, fetched_at)

    national_starts = np.flatnonzero(rng.random(n) < hydro**1.5 / (24 * 30))
    hour_factor = 1 + 1.2 * np.exp(-(((lh - 20) / 2.5) ** 2)) + 0.6 * np.exp(-(((lh - 14) / 2) ** 2))
    evening_blocks = np.flatnonzero(lh == 18)

    geocoder = Geocoder.from_db(conn)
    aliases: dict[str, list[str]] = {}
    for alias, region_id in geocoder.aliases.items():
        aliases.setdefault(region_id, []).append(alias)

    messages: list[RawMessage] = []
    counter = 0

    def add(region_id: str, kind: str, when: pd.Timestamp, rep: float, hours_out: int = 2) -> None:
        nonlocal counter
        if when > now:
            return
        regional = rng.random() < 0.7
        templates = TEXTS[kind]
        if not regional:  # el canal nacional siempre menciona el lugar
            templates = [t for t in templates if "{place}" in t]
        place = str(rng.choice(aliases[region_id])).title()
        text = str(rng.choice(templates)).format(place=place, h=hours_out)
        counter += 1
        messages.append(
            RawMessage(
                source="demo",
                source_msg_id=str(counter),
                channel=f"demo_{region_id.lower()}" if regional else "demo_nacional",
                author_key=f"demo:{region_id}:{rng.integers(max(50, int(400 * rep)))}",
                posted_at=when.floor("s").to_pydatetime(),
                text=text,
                default_region=region_id if regional else None,
            )
        )

    def minutes(lo: float, hi: float) -> pd.Timedelta:
        return pd.Timedelta(minutes=float(rng.uniform(lo, hi)))

    n_outages = 0
    for region_id in state_ids(conn):
        base, rep, base_temp = PROFILE.get(region_id, DEFAULT_PROFILE)
        wx = _synth_weather(rng, lh, doy, month, base_temp, 1.0)
        weather_rows += _weather_rows(rng, region_id, ts_str, observed, wx, fetched_at)

        app = wx["apparent_temp_c"]
        storm = np.where((wx["precip_mm"] > 5) | (wx["gusts_kmh"] > 45), 3.0, 1.0)
        hazard = np.clip(base * np.exp(0.15 * (app - app.mean())) * hour_factor * hydro * storm, 0, 0.5)

        out = np.zeros(n, dtype=bool)
        starts: list[int] = []
        draws = rng.random(n)
        t = 0
        while t < n:
            if draws[t] < hazard[t]:
                dur = max(1, int(round(rng.lognormal(np.log(2.5), 0.6))))
                out[t : t + dur] = True
                starts.append(t)
                t += dur + 1
            else:
                t += 1
        for t0 in national_starts:
            if rng.random() < 0.6:
                out[t0 : t0 + max(2, int(round(rng.lognormal(np.log(6), 0.5))))] = True
                starts.append(int(t0))
        if region_id in RATIONED:  # racionamiento de 18:00 a 22:00 cuando el embalse está bajo
            for t0 in evening_blocks:
                if hydro[t0] > 1.3 and rng.random() < 0.6:
                    out[t0 : t0 + 4] = True

        dips = rng.random(n) < hazard * 1.5
        for t0 in starts:
            if rng.random() < 0.5:
                dips[max(0, t0 - int(rng.integers(1, 3)))] = True
        dips &= ~out

        out &= social_window
        dips &= social_window
        for a, b in _intervals(out):
            n_outages += 1
            for _ in range(rng.poisson(rep * 3.0)):  # durante el corte se publica poco
                add(region_id, "outage", hours[a] + minutes(0, 60), rep)
            if b < n and observed[b]:  # al volver la luz se publica mucho más
                for _ in range(rng.poisson(rep * 5.0)):
                    add(region_id, "restore", hours[b] + minutes(0, 40), rep, int(b - a))
        for t0 in np.flatnonzero(dips):
            for _ in range(rng.poisson(rep * 3.5)):
                add(region_id, "dip", hours[t0] + minutes(0, 60), rep)
        for day in pd.date_range(social_start, end, freq="D"):
            for _ in range(rng.poisson(3 * rep)):
                add(region_id, "noise", day + minutes(0, 24 * 60), rep)
            for _ in range(rng.poisson(0.3)):  # falsos positivos aislados
                add(region_id, "outage", day + minutes(0, 24 * 60), rep)

    conn.executemany(UPSERT_SQL, weather_rows)
    channels = {m.channel: m.default_region for m in messages}
    conn.executemany("INSERT OR IGNORE INTO channels(channel, default_region) VALUES (?, ?)", list(channels.items()))
    conn.commit()
    stored = store_messages(conn, messages, geocoder)
    return {"weather_rows": len(weather_rows), "messages": stored, "simulated_outages": n_outages}


def run_demo(days: int = 180, seed: int = 7, reset: bool = True) -> dict:
    """demo completa: init → datos sintéticos → etiquetas → entrenamiento → predicción → alertas."""
    from .alerts import detect, forecast_alerts
    from .db import connect, init_db
    from .features import aggregate_region_hour
    from .model import predict_latest, train

    s = get_settings()
    if reset:
        for suffix in ("", "-wal", "-shm"):
            path = s.db_path.with_name(s.db_path.name + suffix)
            path.unlink(missing_ok=True)
    conn = connect()
    init_db(conn)
    stats = generate(conn, days=days, seed=seed)
    log.info("Datos sintéticos: %s", stats)
    stats["region_hours"] = aggregate_region_hour(conn)
    meta = train(conn)
    preds, meta = predict_latest(conn)
    if meta["forecast_enabled"]:
        forecast_alerts(conn, preds, meta)
    detections = detect(conn)
    conn.close()
    return {"stats": stats, "meta": meta, "preds": preds, "detections": detections}
