"""Punto de entrada: python -m apagon <comando>."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import timedelta
from pathlib import Path

from .config import ROOT, get_settings


def _print_metrics(meta: dict) -> None:
    print(f"\nModelo {meta['version']} · horizonte {meta['horizon_h']} h · prueba desde {meta['test_from']} UTC")
    print(f"{'modelo':<14}{'PR-AUC':>9}{'Brier':>9}")
    for name, m in meta["metrics"].items():
        print(f"{name:<14}{m['pr_auc']:>9.3f}{m['brier']:>9.3f}")
    enabled = "SÍ" if meta["forecast_enabled"] else "NO (solo detección)"
    print(f"tasa base: {meta['base_rate']:.3f} · pronóstico habilitado: {enabled} · umbral sugerido (máx F1): {meta['suggested_threshold']}")
    for op in meta.get("operating_points", []):
        print(
            f"  umbral {op['threshold']:.2f}: precisión {op['precision']:.0%} · recall {op['recall']:.0%}"
            f" · alerta en {op['alert_hours_share']:.1%} de las horas-estado"
        )
    m = meta["metrics"]
    if m["logistica"]["pr_auc"] > m["xgboost"]["pr_auc"]:
        print("  nota: la logística supera a XGBoost en este periodo; revisa features o regularización.")
    print("features más importantes:", ", ".join(f"{n} ({v:.3f})" for n, v in meta["top_features"][:5]))


def _print_preds(preds, top: int = 8) -> None:
    print(f"\nRiesgo de falla en las próximas horas (top {top}):")
    for row in preds.head(top).itertuples(index=False):
        bar = "█" * int(round(row.prob * 20))
        print(f"  {row.region_id:<4} {row.prob:6.1%} {bar}")


def _cmd_init_db(args) -> None:
    from .db import connect, init_db

    with connect() as conn:
        init_db(conn)
    print(f"Base de datos lista: {get_settings().db_path}")


def _cmd_demo(args) -> None:
    from .demo import run_demo

    result = run_demo(days=args.days, seed=args.seed, reset=not args.keep)
    print(f"\nDatos sintéticos: {result['stats']}")
    _print_metrics(result["meta"])
    _print_preds(result["preds"])
    print(f"\nDetecciones activas ahora: {result['detections'] or 'ninguna'}")
    print(f"\nDB demo: {get_settings().db_path} · modelos: {get_settings().model_dir}")


def _cmd_listen(args) -> None:
    from .ingest.telegram import listen

    listen()


def _cmd_backfill_telegram(args) -> None:
    from .features import aggregate_region_hour
    from .db import connect
    from .ingest.telegram import backfill

    n = backfill(days=args.days)
    with connect() as conn:
        aggregate_region_hour(conn)
    print(f"Mensajes nuevos: {n}. Etiquetas recalculadas.")


def _cmd_weather(args) -> None:
    from .db import connect
    from .ingest import weather

    with connect() as conn:
        if args.backfill_days:
            weather.backfill(conn, args.backfill_days)
        else:
            weather.fetch_forecast(conn)


def _cmd_aggregate(args) -> None:
    from .db import connect, utcnow
    from .features import aggregate_region_hour

    with connect() as conn:
        since = None if args.full else utcnow() - timedelta(hours=args.hours)
        n = aggregate_region_hour(conn, since)
    print(f"region_hour: {n} filas recalculadas")


def _cmd_detect(args) -> None:
    from .alerts import detect
    from .db import connect

    with connect() as conn:
        detections = detect(conn)
    print(f"Detecciones: {detections or 'ninguna'}")


def _cmd_train(args) -> None:
    from .db import connect
    from .model import train

    with connect() as conn:
        meta = train(conn, test_days=args.test_days)
    _print_metrics(meta)


def _cmd_predict(args) -> None:
    from .alerts import forecast_alerts
    from .db import connect
    from .model import predict_latest

    with connect() as conn:
        preds, meta = predict_latest(conn)
        _print_preds(preds)
        if args.no_alert:
            return
        if meta["forecast_enabled"]:
            forecast_alerts(conn, preds, meta)
        else:
            logging.warning("Pronóstico deshabilitado: el modelo no supera a la persistencia. No se envían alertas de pronóstico.")


def _cmd_reprocess(args) -> None:
    from .db import connect
    from .features import aggregate_region_hour
    from .ingest.store import reprocess

    with connect() as conn:
        n = reprocess(conn, only_stale=not args.all)
        aggregate_region_hour(conn)
    print(f"Mensajes reclasificados: {n}. Etiquetas recalculadas.")


def _cmd_bot(args) -> None:
    from .bot import run

    run()


def _cmd_status(args) -> None:
    import json

    from .db import connect

    s = get_settings()
    with connect() as conn:
        for table in ("social_reports", "weather_hourly", "region_hour", "predictions", "subscribers", "alerts_sent"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:<16}{n:>10}")
        row = conn.execute(
            "SELECT MIN(posted_at), MAX(posted_at), SUM(event_type IS NOT NULL) FROM social_reports"
        ).fetchone()
        print(f"reportes: {row[0]} → {row[1]} · clasificados: {row[2] or 0}")
    meta_path = s.model_dir / "latest.json"
    if meta_path.exists():
        _print_metrics(json.loads(meta_path.read_text(encoding="utf-8")))
    else:
        print("Sin modelo entrenado todavía.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apagon", description="MVP de predicción de fallas eléctricas en Venezuela")
    p.add_argument("--db", type=Path, help="ruta de la base SQLite (sobrescribe APAGON_DB_PATH)")
    p.add_argument("--model-dir", type=Path, help="carpeta de modelos (sobrescribe APAGON_MODEL_DIR)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="crea tablas y carga config/").set_defaults(func=_cmd_init_db)

    d = sub.add_parser("demo", help="pipeline completo con datos sintéticos (data/demo.db)")
    d.add_argument("--days", type=int, default=180)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--keep", action="store_true", help="no borrar la DB demo previa")
    d.set_defaults(func=_cmd_demo)

    sub.add_parser("listen", help="escucha Telegram en tiempo real").set_defaults(func=_cmd_listen)

    b = sub.add_parser("backfill-telegram", help="descarga historial de los canales")
    b.add_argument("--days", type=int, default=90)
    b.set_defaults(func=_cmd_backfill_telegram)

    w = sub.add_parser("weather", help="actualiza clima (o backfill con --backfill-days)")
    w.add_argument("--backfill-days", type=int, default=0)
    w.set_defaults(func=_cmd_weather)

    a = sub.add_parser("aggregate", help="recalcula etiquetas región-hora")
    a.add_argument("--full", action="store_true")
    a.add_argument("--hours", type=int, default=3, help="horas hacia atrás a recalcular (incremental)")
    a.set_defaults(func=_cmd_aggregate)

    sub.add_parser("detect", help="detección inmediata y alertas").set_defaults(func=_cmd_detect)

    t = sub.add_parser("train", help="entrena y evalúa contra baselines")
    t.add_argument("--test-days", type=int, default=14)
    t.set_defaults(func=_cmd_train)

    pr = sub.add_parser("predict", help="pronóstico por estado y alertas")
    pr.add_argument("--no-alert", action="store_true")
    pr.set_defaults(func=_cmd_predict)

    r = sub.add_parser("reprocess", help="reclasifica mensajes con el parser actual")
    r.add_argument("--all", action="store_true", help="reprocesar todo, no solo versiones viejas")
    r.set_defaults(func=_cmd_reprocess)

    sub.add_parser("bot", help="bot de Telegram (comandos)").set_defaults(func=_cmd_bot)
    sub.add_parser("status", help="resumen de datos y último modelo").set_defaults(func=_cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "demo":  # la demo borra su DB al empezar: nunca debe apuntar a la base real
        os.environ["APAGON_DB_PATH"] = str(ROOT / "data" / "demo.db")
        os.environ["APAGON_MODEL_DIR"] = str(ROOT / "models" / "demo")
        os.environ["APAGON_DRY_RUN"] = "1"
    if args.db:
        os.environ["APAGON_DB_PATH"] = str(args.db)
    if args.model_dir:
        os.environ["APAGON_MODEL_DIR"] = str(args.model_dir)
    get_settings.cache_clear()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    import httpx

    try:
        args.func(args)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    except httpx.HTTPError as exc:
        logging.error("Error de red (%s). Revisa la conexión o los límites de la API.", type(exc).__name__)
        return 2
    return 0
