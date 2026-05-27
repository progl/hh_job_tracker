import pytest

from app.db import vacancies_repo


def _vacancy(vid: int = 1, **over) -> dict:
    base = {
        "id": vid,
        "name": f"Test {vid}",
        "company_id": 100,
        "company_name": "Co",
        "area_id": None,
        "area_name": None,
        "salary_from": 100000,
        "salary_to": 200000,
        "salary_currency": "RUR",
        "salary_gross": False,
        "salary_rub": 150000,
        "work_schedule": "fullDay",
        "employment": "FULL",
        "work_experience": None,
        "work_formats": "[]",
        "publication_time": None,
        "creation_time": None,
        "is_remote": 0,
        "is_remote_text": 0,
        "level": "middle",
        "key_skills": None,
        "parsed_stack": '["python"]',
        "responses_count": 5,
        "total_responses_count": 50,
        "online_users_count": 1,
        "description": "desc",
        "raw_json": "{}",
        "url": f"https://hh.ru/vacancy/{vid}",
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


@pytest.mark.asyncio
async def test_api_health(app_client):
    client, _ = app_client
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "client" in body
    assert "scheduler" in body


@pytest.mark.asyncio
async def test_index_renders(app_client):
    client, _ = app_client
    r = await client.get("/")
    assert r.status_code == 200
    assert "HH Job Tracker" in r.text


@pytest.mark.asyncio
async def test_index_with_archived_filter(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, archived=False), _vacancy(2, archived=True))
    r = await client.get("/?archived=only")
    assert r.status_code == 200
    # вакансия 1 (не архив) не должна попасть
    assert "Test 1" not in r.text
    # вакансия 2 (архив) — должна
    assert "Test 2" in r.text


@pytest.mark.asyncio
async def test_index_archived_hide_default(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, archived=False), _vacancy(2, archived=True))
    r = await client.get("/")
    assert r.status_code == 200
    assert "Test 1" in r.text
    assert "Test 2" not in r.text


@pytest.mark.asyncio
async def test_vacancies_fragment(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1))
    r = await client.get("/api/vacancies")
    assert r.status_code == 200
    assert "Test 1" in r.text


@pytest.mark.asyncio
async def test_set_vacancy_status(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1))
    r = await client.post("/api/vacancy/1/status", data={"status": "applied", "note": "ok"})
    assert r.status_code == 200
    # проверим в БД
    from app.db.db import get_db

    db = await get_db()
    try:
        cur = await db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=1")
        assert (await cur.fetchone())[0] == "applied"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_vacancy_status_unknown_400(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1))
    r = await client.post("/api/vacancy/1/status", data={"status": "bogus"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bulk_status_updates_multiple(app_client):
    """Массовая смена статуса по списку ids."""
    client, webapp = app_client
    for vid in (10, 11, 12):
        await _seed(webapp, _vacancy(vid))
    r = await client.post(
        "/api/vacancies/bulk-status",
        data={"ids": ["10", "11", "12"], "status": "skipped"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] == 3
    # проверка в БД
    from app.db.db import get_db

    db = await get_db()
    try:
        for vid in (10, 11, 12):
            cur = await db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=?", (vid,))
            assert (await cur.fetchone())[0] == "skipped"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_bulk_status_unknown_400(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1))
    r = await client.post("/api/vacancies/bulk-status", data={"ids": ["1"], "status": "bogus"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bulk_status_empty_ids_400(app_client):
    client, _ = app_client
    r = await client.post("/api/vacancies/bulk-status", data={"status": "skipped"})
    # 422 от FastAPI если ids обязательный, либо 400 от нас
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_format_filter_remote_only(app_client):
    """Новый фильтр format=remote отдаёт только удалёнку."""
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="Remote Job", is_remote=1, is_remote_text=0))
    await _seed(webapp, _vacancy(2, name="Office Job", is_remote=0, is_remote_text=0))
    r = await client.get("/api/vacancies?format=remote")
    body = r.text
    assert "Remote Job" in body
    assert "Office Job" not in body


@pytest.mark.asyncio
async def test_format_filter_office_only(app_client):
    """format=office отдаёт только офисные."""
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="Remote Job", is_remote=1))
    await _seed(webapp, _vacancy(2, name="Office Job", is_remote=0, is_remote_text=0))
    r = await client.get("/api/vacancies?format=office")
    body = r.text
    assert "Office Job" in body
    assert "Remote Job" not in body


@pytest.mark.asyncio
async def test_format_filter_all_default(app_client):
    """format=all (или нет параметра) — все."""
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="Remote Job", is_remote=1))
    await _seed(webapp, _vacancy(2, name="Office Job", is_remote=0, is_remote_text=0))
    r = await client.get("/api/vacancies?format=all")
    body = r.text
    assert "Remote Job" in body
    assert "Office Job" in body


@pytest.mark.asyncio
async def test_vacancy_detail_page(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(42, name="Detail Vacancy"))
    r = await client.get("/vacancy/42")
    assert r.status_code == 200
    assert "Detail Vacancy" in r.text


@pytest.mark.asyncio
async def test_vacancy_detail_404(app_client):
    client, _ = app_client
    r = await client.get("/vacancy/9999999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_export_csv(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="CSV"))
    r = await client.get("/api/export.csv")
    assert r.status_code == 200
    assert "CSV" in r.text
    assert r.headers["content-type"].startswith("text/csv")


@pytest.mark.asyncio
async def test_export_json(app_client):
    client, webapp = app_client
    await _seed(webapp, _vacancy(1, name="JSON"))
    r = await client.get("/api/export.json")
    assert r.status_code == 200
    data = r.json()
    assert any(row["name"] == "JSON" for row in data)


@pytest.mark.asyncio
async def test_funnel_page(app_client):
    client, _ = app_client
    r = await client.get("/funnel")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_profile_page(app_client):
    client, _ = app_client
    r = await client.get("/profile")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_dedup_endpoint(app_client):
    client, webapp = app_client
    await _seed(
        webapp,
        _vacancy(1, name="Python Dev", company_name="Acme"),
        _vacancy(2, name="Python Dev", company_name="Acme"),
        _vacancy(3, name="Go Dev", company_name="Acme"),
    )
    r = await client.post("/api/dedup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["groups"] == 1
    assert body["marked"] == 1
