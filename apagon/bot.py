"""Bot de Telegram: gestiona suscripciones y consultas. Las alertas las envían los jobs de cron."""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import get_settings
from .db import connect, region_names, state_ids, to_ts, utcnow

log = logging.getLogger(__name__)


def _help() -> str:
    return (
        "⚡ Alertas de fallas eléctricas (MVP)\n\n"
        "/regiones — códigos de estado\n"
        "/suscribir ZUL [umbral] — alertas de un estado (umbral 0-1 o %; sin umbral usa el del modelo)\n"
        "/desuscribir ZUL | todas — dejar de recibir alertas\n"
        "/estado [ZUL] — riesgo actual y reportes recientes\n\n"
        "Las alertas combinan reportes en redes y clima. No son avisos oficiales."
    )


def _parse_threshold(raw: str) -> float:
    value = float(raw.replace(",", ".").rstrip("%"))
    value = value / 100 if value > 1 else value
    if not 0 < value < 1:
        raise ValueError
    return value


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_help())


async def cmd_regiones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with closing(connect()) as conn:
        names = region_names(conn)
        lines = [f"{rid} — {names[rid]}" for rid in state_ids(conn)]
    await update.message.reply_text("\n".join(lines))


async def cmd_suscribir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /suscribir ZUL [umbral]. Mira los códigos con /regiones")
        return
    region_id = context.args[0].upper()
    try:
        threshold = _parse_threshold(context.args[1]) if len(context.args) > 1 else None
    except ValueError:
        await update.message.reply_text("Umbral inválido. Ejemplos: 0.6 o 60%")
        return
    with closing(connect()) as conn:
        if region_id not in state_ids(conn):
            await update.message.reply_text(f"No conozco el código {region_id}. Mira /regiones")
            return
        conn.execute(
            "INSERT OR REPLACE INTO subscribers(chat_id, region_id, threshold, created_at) VALUES (?, ?, ?, ?)",
            (update.effective_chat.id, region_id, threshold, to_ts(utcnow())),
        )
        conn.commit()
        name = region_names(conn)[region_id]
    rule = f"umbral de pronóstico {threshold:.0%}" if threshold is not None else "umbral automático del modelo"
    await update.message.reply_text(f"Listo: recibirás alertas de {name} ({rule}).")


async def cmd_desuscribir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = (context.args[0].upper() if context.args else "TODAS")
    with closing(connect()) as conn:
        if target == "TODAS":
            conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (update.effective_chat.id,))
        else:
            conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ? AND region_id = ?", (update.effective_chat.id, target)
            )
        conn.commit()
    await update.message.reply_text("Suscripción eliminada.")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings()
    since = to_ts(utcnow() - timedelta(minutes=s.detect_window_min))
    with closing(connect()) as conn:
        names = region_names(conn)
        if context.args:
            regions = [context.args[0].upper()]
        else:
            regions = [
                r[0]
                for r in conn.execute("SELECT region_id FROM subscribers WHERE chat_id = ?", (update.effective_chat.id,))
            ]
        if not regions:
            await update.message.reply_text("Indica un estado (/estado ZUL) o suscríbete primero.")
            return
        lines = []
        for region_id in regions:
            if region_id not in names:
                lines.append(f"{region_id}: código desconocido")
                continue
            pred = conn.execute(
                "SELECT prob, horizon_h FROM predictions WHERE region_id = ? ORDER BY issued_at DESC LIMIT 1",
                (region_id,),
            ).fetchone()
            n = conn.execute(
                """SELECT COUNT(DISTINCT author_hash) FROM social_reports
                   WHERE region_id = ? AND event_type IN ('outage', 'dip') AND posted_at >= ?""",
                (region_id, since),
            ).fetchone()[0]
            risk = f"riesgo {pred['prob']:.0%} próximas {pred['horizon_h']} h" if pred else "sin pronóstico aún"
            lines.append(f"{names[region_id]}: {risk} · {n} reportantes en {s.detect_window_min} min")
    await update.message.reply_text("\n".join(lines))


def run() -> None:
    s = get_settings()
    if not s.bot_token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env (créalo con @BotFather).")
    app = Application.builder().token(s.bot_token).build()
    for name, handler in (
        ("start", cmd_start),
        ("ayuda", cmd_start),
        ("regiones", cmd_regiones),
        ("suscribir", cmd_suscribir),
        ("desuscribir", cmd_desuscribir),
        ("estado", cmd_estado),
    ):
        app.add_handler(CommandHandler(name, handler))
    log.info("Bot en marcha (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
