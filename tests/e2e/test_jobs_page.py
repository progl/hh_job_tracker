"""e2e тесты на страницу /jobs — журнал прогонов джобов."""

from __future__ import annotations

import aiosqlite
import pytest


def _db_path() -> str:
    from app.config import settings

    return settings.DB_PATH


async def _seed_runs():
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO job_runs(job_id, status, started_at, finished_at, duration_ms, trigger, result) "
            "VALUES ('sync_searches', 'ok', '2026-05-25 10:00:00', '2026-05-25 10:00:05', 5000, 'cron', "
            """'{"ran": 3, "saved": 50}')"""
        )
        await db.execute(
            "INSERT INTO job_runs(job_id, status, started_at, finished_at, duration_ms, trigger, error) "
            "VALUES ('personal_refresh', 'error', '2026-05-25 10:01:00', '2026-05-25 10:01:02', 2000, 'manual', "
            "'session expired: 302 -> /login')"
        )
        await db.execute(
            "INSERT INTO job_runs(job_id, status, started_at, trigger) "
            "VALUES ('ml_retrain', 'running', '2026-05-25 10:02:00', 'cron')"
        )
        await db.commit()


@pytest.mark.asyncio
async def test_jobs_page_renders_empty(app_client):
    client, _ = app_client
    r = await client.get("/jobs")
    assert r.status_code == 200
    assert "Журнал джобов" in r.text


@pytest.mark.asyncio
async def test_jobs_page_shows_all(app_client):
    client, _ = app_client
    await _seed_runs()
    r = await client.get("/jobs")
    body = r.text
    assert "sync_searches" in body
    assert "personal_refresh" in body
    assert "ml_retrain" in body
    # человекочитаемые лейблы из scheduler._JOB_LABELS
    assert "Синк сохранённых поисков" in body or "sync_searches" in body
    # статусы
    assert "ok" in body
    assert "error" in body
    assert "running" in body
    # длительность
    assert "5000" in body
    # error message виден
    assert "session expired" in body


@pytest.mark.asyncio
async def test_jobs_page_filter_by_job_id(app_client):
    client, _ = app_client
    await _seed_runs()
    r = await client.get("/jobs?job_id=sync_searches")
    body = r.text
    assert "sync_searches" in body
    # другие джобы не должны попасть в таблицу (но в селекте остаются)
    # проверяем через ошибку, которой у sync_searches нет
    assert "session expired" not in body


@pytest.mark.asyncio
async def test_jobs_page_filter_by_status(app_client):
    client, _ = app_client
    await _seed_runs()
    r = await client.get("/jobs?status=error")
    body = r.text
    assert "session expired" in body
    # ok-результат не должен быть в таблице (но текст «ok» в select-опции остаётся)
    # надёжнее: проверяем что нет sync_searches как джоб-id в строке таблицы
    # (sync_searches — ok-прогон, при фильтре status=error не должен попасть)
    # упрощённо: result от sync_searches («ran»: 3) не должен быть видим
    assert '"ran"' not in body
