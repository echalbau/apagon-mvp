-- Esquema mínimo. Todas las marcas de tiempo en UTC, formato 'YYYY-MM-DD HH:MM:SS'.

CREATE TABLE IF NOT EXISTS regions (
  region_id TEXT PRIMARY KEY,                 -- 'ZUL', 'DC', ... y 'GURI' (punto hidrológico)
  name      TEXT NOT NULL,
  lat       REAL NOT NULL,
  lon       REAL NOT NULL,
  kind      TEXT NOT NULL DEFAULT 'state'     -- 'state' | 'hydro'
);

CREATE TABLE IF NOT EXISTS region_aliases (   -- gazetteer para geocodificar texto
  alias     TEXT PRIMARY KEY,                 -- normalizado: minúsculas, sin acentos
  region_id TEXT NOT NULL REFERENCES regions(region_id)
);

CREATE TABLE IF NOT EXISTS channels (
  channel        TEXT PRIMARY KEY,            -- username sin '@', en minúsculas
  default_region TEXT REFERENCES regions(region_id),
  active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS social_reports (
  id             INTEGER PRIMARY KEY,
  source         TEXT NOT NULL,               -- 'telegram' | 'x' | 'demo'
  source_msg_id  TEXT NOT NULL,
  channel        TEXT,
  author_hash    TEXT,                        -- sha256(sal:autor)[:16]; nunca el ID crudo
  posted_at      TEXT NOT NULL,
  text           TEXT NOT NULL,
  event_type     TEXT,                        -- 'outage' | 'dip' | 'restore' | NULL
  region_id      TEXT REFERENCES regions(region_id),
  parser_version TEXT NOT NULL,               -- permite reprocesar al mejorar el clasificador
  UNIQUE (source, source_msg_id)
);
CREATE INDEX IF NOT EXISTS ix_reports_region_time ON social_reports(region_id, posted_at);
CREATE INDEX IF NOT EXISTS ix_reports_time ON social_reports(posted_at);

CREATE TABLE IF NOT EXISTS weather_hourly (
  region_id       TEXT NOT NULL REFERENCES regions(region_id),
  ts              TEXT NOT NULL,              -- hora UTC
  is_forecast     INTEGER NOT NULL,           -- 0 observado/reanálisis, 1 pronóstico
  temp_c          REAL,
  apparent_temp_c REAL,
  rh              REAL,
  precip_mm       REAL,
  gusts_kmh       REAL,
  fetched_at      TEXT NOT NULL,
  PRIMARY KEY (region_id, ts, is_forecast)
);

CREATE TABLE IF NOT EXISTS region_hour (      -- agregados por región-hora + etiqueta
  region_id TEXT NOT NULL,
  hour_ts   TEXT NOT NULL,
  n_reports INTEGER NOT NULL,                 -- mensajes con evento
  n_authors INTEGER NOT NULL,                 -- autores distintos reportando corte o bajón
  n_restore INTEGER NOT NULL,                 -- autores distintos reportando restablecimiento
  label     INTEGER NOT NULL,                 -- 1 si hubo falla observable en esa hora
  PRIMARY KEY (region_id, hour_ts)
);

CREATE TABLE IF NOT EXISTS predictions (
  region_id     TEXT NOT NULL,
  issued_at     TEXT NOT NULL,                -- hora de corte de los datos usados
  horizon_h     INTEGER NOT NULL,
  prob          REAL NOT NULL,
  model_version TEXT NOT NULL,
  PRIMARY KEY (region_id, issued_at, horizon_h)
);

CREATE TABLE IF NOT EXISTS subscribers (
  chat_id    INTEGER NOT NULL,
  region_id  TEXT NOT NULL REFERENCES regions(region_id),
  threshold  REAL,                            -- NULL = umbral sugerido por el modelo vigente
  created_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, region_id)
);

CREATE TABLE IF NOT EXISTS alerts_sent (
  id         INTEGER PRIMARY KEY,
  chat_id    INTEGER NOT NULL,
  region_id  TEXT NOT NULL,
  alert_type TEXT NOT NULL,                   -- 'detection' | 'forecast'
  prob       REAL,
  sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts_lookup ON alerts_sent(chat_id, region_id, alert_type, sent_at);
