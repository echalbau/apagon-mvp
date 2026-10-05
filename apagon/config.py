"""Configuración central. Lee variables de entorno y, si existe, el archivo .env de la raíz."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Parser mínimo de .env: KEY=VALUE, ignora comentarios. No pisa variables ya definidas."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT / path


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    model_dir: Path
    config_dir: Path
    tg_api_id: int | None
    tg_api_hash: str | None
    tg_session: Path
    bot_token: str | None
    dry_run: bool
    author_salt: str
    min_authors: int
    horizon_h: int
    default_threshold: float
    alert_cooldown_h: int
    detect_window_min: int
    utc_offset_h: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    api_id = os.environ.get("TELEGRAM_API_ID")
    dry_default = "0" if bot_token else "1"
    return Settings(
        db_path=_path("APAGON_DB_PATH", ROOT / "data" / "apagon.db"),
        model_dir=_path("APAGON_MODEL_DIR", ROOT / "models"),
        config_dir=_path("APAGON_CONFIG_DIR", ROOT / "config"),
        tg_api_id=int(api_id) if api_id else None,
        tg_api_hash=os.environ.get("TELEGRAM_API_HASH") or None,
        tg_session=_path("TELEGRAM_SESSION", ROOT / "data" / "telethon"),
        bot_token=bot_token,
        dry_run=os.environ.get("APAGON_DRY_RUN", dry_default) == "1",
        author_salt=os.environ.get("APAGON_AUTHOR_SALT", "cambia-esta-sal"),
        min_authors=_int("APAGON_MIN_AUTHORS", 3),
        horizon_h=_int("APAGON_HORIZON_H", 6),
        default_threshold=_float("APAGON_DEFAULT_THRESHOLD", 0.6),
        alert_cooldown_h=_int("APAGON_ALERT_COOLDOWN_H", 3),
        detect_window_min=_int("APAGON_DETECT_WINDOW_MIN", 60),
        utc_offset_h=_int("APAGON_UTC_OFFSET_H", -4),
    )
