"""Дополнительные e2e тесты для покрытия app/web/app.py.

Покрывают: /api/status, /api/tasks, /api/searches, /api/cleanup*, /api/scheduler/*,
/api/logs*, /api/profile, /compare, /api/client/unpause, /api/vacancy/{vid}/refresh,
/api/vacancies (с фильтрами), /api/ml/train, /api/fx/refresh, /api/probe.
"""
from __future__ import annotations

import pytest

from app.db import vacancies_repo


def _vacancy(vid: int = 1, **over) -> dict:
    base = {
        "id": vid, "name": f"Test {vid}", "company_id": 100, "company_name": "Co",
        "area_id": None, "area_name": None,
        "salary_from": 100000, "salary_to": 200000, "salary_currency": "RUR",
        "salary_gross": False, "salary_rub": 150000,
        "work_schedule": "fullDay", "employment": "FULL", "work_experience": None,
        "work_formats": "[]",
        "publication_time": None, "creation_time": None,
        "is_remote": 0, "is_remote_text": 0, "level": "middle",
        "key_skills": None, "parsed_stack": '["python"]',
        "responses_count": 5, "total_responses_count": 50, "online_users_count": 1,
        "description": "desc", "raw_json": "{}", "url": f"https://hh.ru/vacancy/{vid}",
        "archived": False,
    }
    base.update(over)
    return base


async def _seed(webapp, *vs: dict) -> None:
    from app.db.db import get_db
    db = await get_db()
    try:
        for v in vs:
            await vacancies_repo.upsert(db, v)
        await db.commit()
    finally:
        await db.close()


# ----- /api/status, /api/tasks -----


@pytest.mark.asyncio
async def test_api_status(app_client):
    client, _ = app_client
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "client" in body
    assert "scheduler" in body
    assert "tasks" in body


@pytest.mark.asyncio
async def test_api_tasks(app_client):
    client, _ = app_client
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    body = r.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)


@pytest.mark.asyncio
async def test_api_tasks_cancel_nonexistent(app_client):
    client, _ = app_client
    r = await client.post("/api/tasks/some_kind/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "some_kind"


# ----- /api/searches -----


@pytest.mark.asyncio
async def test_searches_crud(app_client):
    client, _ = app_client
    # пусто
    r = await client.get("/api/searches")
    assert r.status_code == 200
    assert r.json()["searches"] == []
    # создать
    r = await client.post("/api/searches", data={"name": "MySearch", "text": "python"})
    assert r.status_code == 200
    sid = r.json()["id"]
    # список
    r = await client.get("/api/searches")
    assert any(s["name"] == "MySearch" for s in r.json()["searches"])
    # toggle
    r = await client.post(f"/api/searches/{sid}/toggle")
    assert r.status_code == 200
    # delete
    r = await client.delete(f"/api/searches/{sid}")
    assert r.status_code == 200
    r = await client.get("/api/searches")
    assert all(s["id"] != sid for s in r.json()["searches"])


@pytest.mark.asyncio
async def test_searches_toggle_not_found(app_client):
    client, _ = app_client
    r = await client.post("/api/searches/9999/toggle")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_searches_create_with_remote_and_area(app_client):
    client, _ = app_client
    r = await client.post(
        "/api/searches",
        data={"name": "Remote", "text": "py", "area": "1", "only_remote": "true", "max_pages": "3"},
    )
    assert r.status_code == 200


# ----- /api/cleanup -----


@pytest.mark.asyncio
async def test_cleanup_preview_empty(app_client):
    client, _ = app_client
    r = await client.get("/api/cleanup/preview")
    assert r.status_code == 200
    body = r.json()
    assert "will_delete" in body and "total" in body and "keep" in body


@pytest.mark.asyncio
async def test_cleanup_preview_counts_garbage(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="orphan"))
    r = await client.get("/api/cleanup/preview")
    body = r.json()
    assert body["will_delete"] >= 1


@pytest.mark.asyncio
async def test_cleanup_deletes_orphans(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="orphan"))
    r = await client.post("/api/cleanup", data={"also_resync": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] >= 1


# ----- /api/scheduler/{job_id}/run-now -----


@pytest.mark.asyncio
async def test_scheduler_run_now_not_started(app_client):
    client, _ = app_client
    r = await client.post("/api/scheduler/personal_refresh/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "scheduler" in body["reason"]


# ----- /api/client/unpause -----


@pytest.mark.asyncio
async def test_client_unpause(app_client):
    client, _ = app_client
    r = await client.post("/api/client/unpause")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "was_paused_until" in body


# ----- /api/logs -----


@pytest.mark.asyncio
async def test_logs_api(app_client):
    client, _ = app_client
    r = await client.get("/api/logs")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert "stats" in body


@pytest.mark.asyncio
async def test_logs_api_with_filters(app_client):
    client, _ = app_client
    r = await client.get("/api/logs?status=ok&path=/x&only_errors=true&limit=10")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_logs_page_html(app_client):
    client, _ = app_client
    r = await client.get("/logs")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_logs_cleanup(app_client):
    client, _ = app_client
    r = await client.post("/api/logs/cleanup", data={"keep": "100"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["kept"] == 100


# ----- /api/profile -----


@pytest.mark.asyncio
async def test_update_profile(app_client):
    client, _ = app_client
    r = await client.post(
        "/api/profile",
        data={
            "title": "Senior Python", "years_experience": "5",
            "salary_expected_from": "300000", "salary_currency": "RUR",
            "skills_csv": "python, django, fastapi", "formats_csv": "remote, hybrid",
        },
    )
    assert r.status_code == 200
    assert "сохранено" in r.text


# ----- /compare -----


@pytest.mark.asyncio
async def test_compare_page_empty(app_client):
    client, _ = app_client
    r = await client.get("/compare")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_compare_page_with_ids(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="Aaa"), _vacancy(2, name="Bbb"))
    r = await client.get("/compare?ids=1&ids=2")
    assert r.status_code == 200
    assert "Aaa" in r.text
    assert "Bbb" in r.text


# ----- /api/vacancies (с фильтрами) -----


@pytest.mark.asyncio
async def test_vacancies_fragment_with_filters(app_client):
    client, webapp = app_client
    await _seed(
        webapp,
        _vacancy(1, name="Python Dev", level="middle", is_remote=1),
        _vacancy(2, name="Go Dev", level="senior", is_remote=0),
    )
    r = await client.get("/api/vacancies?only_remote=true")
    assert r.status_code == 200
    assert "Python Dev" in r.text


@pytest.mark.asyncio
async def test_vacancies_fragment_with_text_query(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="Java Dev"))
    r = await client.get("/api/vacancies?q=Java")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_vacancies_fragment_sort_by_salary(app_client):
    client, webapp = app_client
    await _seed(
        webapp,
        _vacancy(1, salary_rub=100000),
        _vacancy(2, salary_rub=300000),
    )
    r = await client.get("/api/vacancies?sort=salary_rub&dir=desc")
    assert r.status_code == 200


# ----- /api/vacancy/{vid}/refresh — мокается коллектор -----


@pytest.mark.asyncio
async def test_vacancy_refresh(app_client, monkeypatch):
    client, webapp = app_client
    await _seed(webapp, _vacancy(42))

    async def fake_collect_one(hh_client, db, vid):
        return True
    async def _noop_save_jar(*a, **kw):
        return None
    monkeypatch.setattr(webapp.collector, "collect_one_vacancy", fake_collect_one)
    monkeypatch.setattr(webapp, "save_jar", _noop_save_jar)
    # hh_client.client — property, бросает если не start'нут; ставим заглушку
    monkeypatch.setattr(
        webapp.hh_client.__class__, "client",
        property(lambda self: object()),
    )
    r = await client.post("/api/vacancy/42/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["id"] == 42


# ----- /api/probe -----


@pytest.mark.asyncio
async def test_probe_session_expired(app_client, monkeypatch):
    client, webapp = app_client

    async def boom(*a, **kw):
        from app.clients.hh import SessionExpiredError
        raise SessionExpiredError("login")
    monkeypatch.setattr(webapp.hh_client, "get_page", boom)
    r = await client.get("/api/probe")
    assert r.status_code == 401
    assert r.json()["reason"] == "session_expired"


@pytest.mark.asyncio
async def test_probe_antibot(app_client, monkeypatch):
    client, webapp = app_client

    async def boom(*a, **kw):
        from app.clients.hh import AntibotChallengeError
        raise AntibotChallengeError("paused")
    monkeypatch.setattr(webapp.hh_client, "get_page", boom)
    r = await client.get("/api/probe")
    assert r.status_code == 429
    assert r.json()["reason"] == "antibot"


@pytest.mark.asyncio
async def test_probe_no_initial_state(app_client, monkeypatch):
    client, webapp = app_client

    async def returns_empty(*a, **kw):
        return "<html>no state</html>"
    monkeypatch.setattr(webapp.hh_client, "get_page", returns_empty)
    r = await client.get("/api/probe")
    assert r.status_code == 500
    assert r.json()["reason"] == "no_initial_state"


# ----- /api/dedup empty -----


@pytest.mark.asyncio
async def test_dedup_empty(app_client):
    client, _ = app_client
    r = await client.post("/api/dedup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["groups"] == 0


# ----- /api/fx/refresh, /api/ml/train -----


async def _async_noop(*a, **kw):
    return None


@pytest.mark.asyncio
async def test_fx_refresh(app_client, monkeypatch):
    client, webapp = app_client

    async def fake_refresh(db):
        return {"ok": True}
    monkeypatch.setattr(webapp.cbr_client, "refresh_salary_module", fake_refresh)
    r = await client.post("/api/fx/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_ml_train(app_client, monkeypatch):
    client, webapp = app_client

    async def fake_train():
        return {"trained": False, "reason": "not_enough"}
    monkeypatch.setattr(webapp.ml_module, "train_if_enough_data", fake_train)
    monkeypatch.setattr(webapp.ml_module, "reload_model", lambda *a, **kw: None)
    r = await client.post("/api/ml/train")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
