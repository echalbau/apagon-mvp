"""Entrenamiento con validación temporal, comparación contra baselines y predicción.

Regla de despliegue: el pronóstico solo se habilita si XGBoost supera a la persistencia
("si hubo fallas en las últimas 24 h, habrá otra") en PR-AUC y en Brier. Si no la supera,
el sistema sigue emitiendo alertas de detección pero no de pronóstico.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

from .config import get_settings
from .db import floor_hour, parse_ts, state_ids, to_ts, utcnow
from .features import FEATURES, build_features

log = logging.getLogger(__name__)

CATEGORICAL = ["region"]
NUMERIC = [f for f in FEATURES if f not in CATEGORICAL]
GATE_MARGIN = 1.05  # XGBoost debe mejorar al menos 5 % el PR-AUC de la persistencia
VALID_DAYS = 14   # últimos días del entrenamiento usados para early stopping
MAX_TREES = 2000
FALLBACK_TREES = 300
XGB_PARAMS = dict(
    max_depth=3,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=20,
    reg_lambda=5.0,
    tree_method="hist",
    enable_categorical=True,
    eval_metric="logloss",
    n_jobs=2,
    random_state=0,
)


def _xgb(n_estimators: int, **extra) -> XGBClassifier:
    return XGBClassifier(n_estimators=n_estimators, **XGB_PARAMS, **extra)


def _n_trees(train_df: pd.DataFrame, horizon_h: int) -> int:
    """Número de árboles por early stopping sobre los últimos VALID_DAYS del entrenamiento.
    Sin esto, con etiquetas ruidosas XGBoost sobreajusta y pierde contra una logística."""
    vsplit = train_df["hour"].max() - timedelta(days=VALID_DAYS)
    fit = train_df[train_df["hour"] <= vsplit - timedelta(hours=horizon_h)]
    valid = train_df[train_df["hour"] > vsplit]
    if fit["target"].nunique() < 2 or valid["target"].nunique() < 2:
        log.warning("Sin validación útil para early stopping; uso %d árboles.", FALLBACK_TREES)
        return FALLBACK_TREES
    model = _xgb(MAX_TREES, early_stopping_rounds=50)
    model.fit(fit[FEATURES], fit["target"], eval_set=[(valid[FEATURES], valid["target"])], verbose=False)
    return int(model.best_iteration) + 1


def _logreg():
    pre = ColumnTransformer(
        [
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL),
            ("num", make_pipeline(SimpleImputer(strategy="median"), StandardScaler()), NUMERIC),
        ]
    )
    return make_pipeline(pre, LogisticRegression(max_iter=2000))


def _scores(y, p) -> dict[str, float]:
    return {
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
    }


def _best_f1_threshold(y, p) -> float:
    precision, recall, thresholds = precision_recall_curve(y, p)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-9, None)
    return round(float(thresholds[int(np.nanargmax(f1[:-1]))]), 3)


def _at_threshold(y, p, threshold: float) -> dict[str, float]:
    """Qué significa un umbral en la práctica: precisión, recall y alertas por estado y día."""
    y = np.asarray(y)
    fired = np.asarray(p) >= threshold
    tp = int((fired & (y == 1)).sum())
    return {
        "threshold": round(float(threshold), 3),
        "precision": round(tp / fired.sum(), 3) if fired.sum() else 0.0,
        "recall": round(tp / (y == 1).sum(), 3) if (y == 1).sum() else 0.0,
        "alert_hours_share": round(float(fired.mean()), 4),
    }


def _data_start(conn: sqlite3.Connection):
    row = conn.execute("SELECT MIN(posted_at) FROM social_reports WHERE event_type IS NOT NULL").fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("No hay reportes clasificados. Corre `backfill-telegram` (o `demo`) primero.")
    return floor_hour(parse_ts(row[0]))


def train(conn: sqlite3.Connection, model_dir: Path | None = None, test_days: int = 14, warmup_days: int = 7) -> dict:
    s = get_settings()
    h = s.horizon_h
    model_dir = Path(model_dir or s.model_dir)
    end = floor_hour(utcnow()) - timedelta(hours=1)
    start = _data_start(conn) + timedelta(days=warmup_days)  # descarta días con ventanas incompletas

    df = build_features(conn, start, end, h).dropna(subset=["target"])
    df["target"] = df["target"].astype(int)
    split = df["hour"].max() - timedelta(days=test_days)
    train_df = df[df["hour"] <= split - timedelta(hours=h)]  # hueco de H horas: sin fuga del objetivo
    test_df = df[df["hour"] > split]
    for name, part in (("entrenamiento", train_df), ("prueba", test_df)):
        if part.empty or part["target"].nunique() < 2:
            raise RuntimeError(f"El conjunto de {name} no tiene ambas clases; hace falta más historial.")

    y_test = test_df["target"]
    rate = train_df.groupby(train_df["ev_24h"] > 0)["target"].mean()
    p_persist = (test_df["ev_24h"] > 0).map(rate).fillna(train_df["target"].mean()).to_numpy()
    p_logreg = _logreg().fit(train_df[FEATURES], train_df["target"]).predict_proba(test_df[FEATURES])[:, 1]
    n_trees = _n_trees(train_df, h)
    p_xgb = _xgb(n_trees).fit(train_df[FEATURES], train_df["target"]).predict_proba(test_df[FEATURES])[:, 1]

    metrics = {
        "persistencia": _scores(y_test, p_persist),
        "logistica": _scores(y_test, p_logreg),
        "xgboost": _scores(y_test, p_xgb),
    }
    enabled = (
        metrics["xgboost"]["pr_auc"] >= GATE_MARGIN * metrics["persistencia"]["pr_auc"]
        and metrics["xgboost"]["brier"] <= metrics["persistencia"]["brier"]
    )

    final = _xgb(n_trees).fit(df[FEATURES], df["target"])  # reentrena con todo el historial etiquetado
    version = utcnow().strftime("xgb-%Y%m%d-%H%M%S")
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = f"{version}.json"
    final.save_model(model_dir / model_file)

    importance = sorted(zip(FEATURES, final.feature_importances_.tolist()), key=lambda kv: -kv[1])
    meta = {
        "version": version,
        "model_file": model_file,
        "created_at": to_ts(utcnow()),
        "horizon_h": h,
        "features": FEATURES,
        "categories": state_ids(conn),
        "train_rows": int(len(train_df)),
        "test_rows": int(len(test_df)),
        "test_from": to_ts(split.to_pydatetime()),
        "base_rate": round(float(y_test.mean()), 4),
        "metrics": metrics,
        "forecast_enabled": bool(enabled),
        "n_trees": n_trees,
        "suggested_threshold": (best := _best_f1_threshold(y_test, p_xgb)),
        "operating_points": [_at_threshold(y_test, p_xgb, t) for t in sorted({best, s.default_threshold})],
        "top_features": [[name, round(v, 4)] for name, v in importance[:10]],
    }
    (model_dir / "latest.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def load_latest(model_dir: Path | None = None) -> tuple[XGBClassifier | None, dict | None]:
    model_dir = Path(model_dir or get_settings().model_dir)
    meta_path = model_dir / "latest.json"
    if not meta_path.exists():
        return None, None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    model = XGBClassifier(enable_categorical=True)
    model.load_model(model_dir / meta["model_file"])
    return model, meta


def predict_latest(conn: sqlite3.Connection, model_dir: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """P(falla en las próximas H horas) por estado, usando datos hasta la última hora completa."""
    model, meta = load_latest(model_dir)
    if model is None:
        raise RuntimeError("No hay modelo entrenado. Corre `python -m apagon train`.")
    ref = floor_hour(utcnow()) - timedelta(hours=1)
    df = build_features(conn, ref, ref, meta["horizon_h"])
    df["region"] = pd.Categorical(df["region_id"], categories=meta["categories"])
    probs = model.predict_proba(df[meta["features"]])[:, 1]

    preds = pd.DataFrame({"region_id": df["region_id"], "prob": probs}).sort_values("prob", ascending=False)
    conn.executemany(
        "INSERT OR REPLACE INTO predictions(region_id, issued_at, horizon_h, prob, model_version) VALUES (?, ?, ?, ?, ?)",
        [(r, to_ts(ref), meta["horizon_h"], float(p), meta["version"]) for r, p in zip(preds["region_id"], preds["prob"])],
    )
    conn.commit()
    return preds.reset_index(drop=True), meta
