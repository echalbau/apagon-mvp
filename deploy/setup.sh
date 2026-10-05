#!/usr/bin/env bash
# Instalación en una VM Ubuntu/Debian (Oracle Always Free, Hetzner, etc.).
# Uso: sudo bash deploy/setup.sh   (desde la raíz del repo clonado en /opt/apagon)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/apagon}"
APP_USER="${APP_USER:-apagon}"

apt-get update -y
apt-get install -y python3 python3-venv sqlite3

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
mkdir -p data models backups
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

sudo -u "$APP_USER" .venv/bin/python -m apagon init-db

cp deploy/systemd/apagon-listener.service deploy/systemd/apagon-bot.service /etc/systemd/system/
systemctl daemon-reload

cat <<MSG

Listo. Pasos siguientes (en este orden):
  1. Edita $APP_DIR/.env (credenciales, sal, APAGON_DRY_RUN=0) y config/channels.csv; luego:
       sudo -u $APP_USER .venv/bin/python -m apagon init-db
  2. Login interactivo de Telethon + historial (crea data/telethon.session):
       sudo -u $APP_USER .venv/bin/python -m apagon backfill-telegram --days 120
  3. Clima histórico, entrenamiento:
       sudo -u $APP_USER .venv/bin/python -m apagon weather --backfill-days 220
       sudo -u $APP_USER .venv/bin/python -m apagon train
  4. Servicios y cron:
       systemctl enable --now apagon-listener apagon-bot
       sudo -u $APP_USER crontab deploy/crontab
MSG
