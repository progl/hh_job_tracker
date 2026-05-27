"""e2e фикстуры: запускают FastAPI на ASGITransport, мокая внешние интеграции.

Особенности:
- lifespan не идёт в HH/ЦБ; hh_client/scheduler/ml — no-op
- БД — временный sqlite (tmp_path)
- get_db в роутах автоматически смотрит на settings.DB_PATH (мы его подменили)
"""

from __future__ import annotations

import sys

import pytest_asyncio


@pytest_asyncio.fixture
async def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Срываем все импорты app.web.app, чтобы пересоздать с новым settings.DB_PATH
    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]

    from app.config import settings

    settings.DB_PATH = str(db_path)
    import app.db.db as dbm

    dbm.DB_PATH = db_path

    # Импортируем модуль и патчим внешние вызовы перед запуском lifespan
    import app.web.app as webapp

    async def _noop(*a, **kw):
        return {}

    async def _noop_start(*a, **kw):
        return None

    async def _noop_close(*a, **kw):
        return None

    def _noop_sync(*a, **kw):
        return None

    monkeypatch.setattr(webapp.cbr_client, "refresh_salary_module", _noop)
    monkeypatch.setattr(webapp.hh_client, "start", _noop_start)
    monkeypatch.setattr(webapp.hh_client, "close", _noop_close)
    # status — это property/dict; для роутов нужен dict с базовыми полями
    monkeypatch.setattr(
        webapp.hh_client.__class__,
        "status",
        property(
            lambda self: {
                "started": True,
                "paused_until": 0.0,
                "paused_now": False,
                "challenge_count": 0,
                "base_url": "https://hh.ru",
                "last_url": "",
                "cookie_count": 0,
            }
        ),
    )
    monkeypatch.setattr(webapp.scheduler_mod, "start", _noop_sync)
    monkeypatch.setattr(webapp.scheduler_mod, "shutdown", _noop_sync)
    monkeypatch.setattr(
        webapp.scheduler_mod,
        "status",
        lambda: {"running": False, "jobs": []},
    )
    monkeypatch.setattr(webapp.ml_module, "reload_model", _noop_sync)

    import httpx
    from httpx import ASGITransport

    transport = ASGITransport(app=webapp.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # вручную поднимаем lifespan
        async with webapp.app.router.lifespan_context(webapp.app):
            yield client, webapp
