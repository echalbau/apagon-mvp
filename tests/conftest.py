import pytest

from apagon import config


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APAGON_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("APAGON_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("APAGON_DRY_RUN", "1")
    monkeypatch.setenv("APAGON_AUTHOR_SALT", "test")
    config.get_settings.cache_clear()
    from apagon.db import connect, init_db

    c = connect()
    init_db(c)
    yield c
    c.close()
    config.get_settings.cache_clear()
