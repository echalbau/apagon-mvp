"""Alertas por Telegram: detección inmediata (reglas) y pronóstico (modelo).

Los jobs de cron envían directamente vía Bot API (HTTP); el proceso del bot solo atiende comandos.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from datetime import datetime, timedelta

import httpx
import pandas as pd

from .config import get_settings
from .db import floor_hour, read_df, region_names, to_ts, utcnow

log = logging.getLogger(__name__)
BASELINE_DAYS = 28


def send_message(chat_id: int, text: str) -> bool:
    s = get_settings()
    if s.dry_run or not s.bot_token:
        log.info("[DRY-RUN → %s] %s", chat_id, text.replace("\n", " | "))
        return True
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{s.bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:  # no loguear la URL: contiene el token
        log.warning("Telegram rechazó el envío a %s (HTTP %s)", chat_id, exc.response.status_code)
    except httpx.HTTPError as exc:
        log.warning("Error de red enviando a %s: %s", chat_id, type(exc).__name__)
    return False


def _notify(
    conn: sqlite3.Connection,
    region_id: str,
    alert_type: str,
    text: str,
    now: datetime,
    prob: float | None = None,
    default_threshold: float | None = None,
) -> int:
    """Envía a los suscriptores de la región. Para pronósticos, respeta el umbral de cada
    suscriptor (o `default_threshold` si no fijó uno) y el periodo de enfriamiento."""
    s = get_settings()
    default_threshold = s.default_threshold if default_threshold is None else default_threshold
    subs = conn.execute("SELECT chat_id, threshold FROM subscribers WHERE region_id = ?", (region_id,)).fetchall()
    if not subs:
        if prob is None or prob >= default_threshold:
            log.info("[sin suscriptores · %s] %s", region_id, text.replace("\n", " | "))
        return 0
    cutoff = to_ts(now - timedelta(hours=s.alert_cooldown_h))
    sent = 0
    for sub in subs:
        threshold = default_threshold if sub["threshold"] is None else sub["threshold"]
        if prob is not None and prob < threshold:
            continue
        recent = conn.execute(
            "SELECT 1 FROM alerts_sent WHERE chat_id=? AND region_id=? AND alert_type=? AND sent_at >= ? LIMIT 1",
            (sub["chat_id"], region_id, alert_type, cutoff),
        ).fetchone()
        if recent:
            continue
        if send_message(sub["chat_id"], text):
            conn.execute(
                "INSERT INTO alerts_sent(chat_id, region_id, alert_type, prob, sent_at) VALUES (?, ?, ?, ?, ?)",
                (sub["chat_id"], region_id, alert_type, prob, to_ts(now)),
            )
            sent += 1
            time.sleep(0.05)  # holgura frente al límite de la Bot API
    conn.commit()
    return sent


def detect(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Alerta si los reportantes distintos de la última ventana superan el p95 histórico
    de esa región a esa hora local (mínimo MIN_AUTHORS)."""
    s = get_settings()
    now = now or utcnow()
    window_start = now - timedelta(minutes=s.detect_window_min)
    current = {
        r["region_id"]: r["n"]
        for r in conn.execute(
            """SELECT region_id, COUNT(DISTINCT author_hash) AS n FROM social_reports
               WHERE posted_at > ? AND posted_at <= ? AND region_id IS NOT NULL
                 AND event_type IN ('outage', 'dip')
               GROUP BY region_id""",
            (to_ts(window_start), to_ts(now)),
        )
    }
    current = {r: n for r, n in current.items() if n >= s.min_authors}
    if not current:
        return []

    hist_start = floor_hour(now) - timedelta(days=BASELINE_DAYS)
    hist = read_df(
        conn,
        "SELECT region_id, hour_ts, n_authors FROM region_hour WHERE hour_ts >= ? AND hour_ts < ?",
        (to_ts(hist_start), to_ts(floor_hour(now))),
    )
    hist["hour"] = pd.to_datetime(hist["hour_ts"])
    hours = pd.date_range(hist_start, floor_hour(now) - timedelta(hours=1), freq="h")
    same_local_hour = hours[hours.hour == floor_hour(now).hour]
    names = region_names(conn)

    detections = []
    for region_id, n in current.items():
        series = (
            hist.loc[hist["region_id"] == region_id].set_index("hour")["n_authors"]
            .reindex(same_local_hour, fill_value=0)
        )
        p95 = float(series.quantile(0.95)) if len(series) else 0.0
        threshold = max(s.min_authors, math.floor(p95) + 1)  # estrictamente por encima del p95
        if n < threshold:
            continue
        detections.append({"region_id": region_id, "n_authors": n, "threshold": threshold})
        text = (
            f"⚡ Fallas eléctricas reportadas en {names.get(region_id, region_id)}: "
            f"{n} personas distintas reportaron cortes o bajones en los últimos {s.detect_window_min} min."
        )
        _notify(conn, region_id, "detection", text, now)
    return detections


def forecast_alerts(conn: sqlite3.Connection, preds: pd.DataFrame, meta: dict, now: datetime | None = None) -> int:
    """Alertas de pronóstico. Quien no fijó umbral recibe el sugerido por el modelo (máx F1 en prueba)."""
    now = now or utcnow()
    auto_threshold = meta.get("suggested_threshold", get_settings().default_threshold)
    names = region_names(conn)
    sent = 0
    for row in preds.itertuples(index=False):
        text = (
            f"🔮 Riesgo de falla eléctrica en {names.get(row.region_id, row.region_id)}: "
            f"{row.prob:.0%} en las próximas {meta['horizon_h']} h.\n"
            f"Estimación del modelo {meta['version']} a partir de reportes en redes y clima. No es un aviso oficial."
        )
        sent += _notify(conn, row.region_id, "forecast", text, now, prob=float(row.prob), default_threshold=auto_threshold)
    return sent
