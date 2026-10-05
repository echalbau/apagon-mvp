"""Punta a punta con datos sintéticos: generar → etiquetar → entrenar → predecir → alertar."""

from apagon.alerts import detect, forecast_alerts
from apagon.db import utcnow
from apagon.demo import generate
from apagon.features import aggregate_region_hour
from apagon.model import load_latest, predict_latest, train


def test_end_to_end(conn):
    stats = generate(conn, days=60, seed=1)
    assert stats["messages"] > 1000
    assert aggregate_region_hour(conn) > 0

    meta = train(conn, test_days=10)
    assert set(meta["metrics"]) == {"persistencia", "logistica", "xgboost"}
    assert meta["metrics"]["xgboost"]["pr_auc"] > meta["base_rate"]  # mejor que azar
    model, loaded = load_latest()
    assert model is not None and loaded["version"] == meta["version"]

    preds, meta = predict_latest(conn)
    assert len(preds) == 24 and preds["prob"].between(0, 1).all()
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 24

    # Un suscriptor con umbral 0 recibe pronóstico de su estado; el enfriamiento evita duplicados.
    conn.execute("INSERT INTO subscribers VALUES (123, 'ZUL', 0.0, '2026-01-01 00:00:00')")
    now = utcnow()
    assert forecast_alerts(conn, preds, meta, now) == 1
    assert forecast_alerts(conn, preds, meta, now) == 0
    detect(conn)
