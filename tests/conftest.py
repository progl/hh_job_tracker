import asyncio
import os
import sys
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

# Гарантируем что settings.DB_PATH в тестах НЕ указывает на data/hh.db.
# Делаем это до любого импорта app.* — иначе pydantic-settings подцепит реальный .env.
os.environ["DB_PATH"] = "data/_pytest_unused.db"
os.environ.setdefault("HH_USER_AGENT", "pytest")
os.environ.setdefault("HH_SEC_CH_UA", "pytest")
os.environ.setdefault("HH_SEC_CH_UA_PLATFORM", "macOS")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def pytest_sessionstart(session):
    """До любого теста — глушим MODEL_PATH session-wide на /tmp/never.
    Иначе если в data/model.pkl лежит реальная обученная модель, она грузится
    через глобальный кэш _MODEL и тесты test_scoring_predict.* (которые ждут эвристику) flaky-падают."""
    try:
        from pathlib import Path

        from app.scoring import ml

        ml.MODEL_PATH = Path("/tmp/_pytest_never_exists.pkl")
        ml._MODEL = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_ml_model_path():
    """Дополнительно перед каждым тестом — сбрасываем кэш модели
    (если предыдущий тест monkeypatch'ил MODEL_PATH и что-то загрузил)."""
    try:
        from app.scoring import ml

        ml._MODEL = None
        yield
        ml._MODEL = None
    except ImportError:
        yield


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Изолированная пустая БД для каждого теста."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    from app.config import settings

    monkeypatch.setattr(settings, "DB_PATH", str(db_path))
    import app.db.db as dbm

    monkeypatch.setattr(dbm, "DB_PATH", db_path)

    await dbm.init_db()
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        yield conn
    finally:
        await conn.close()
