import pytest

from app.db import vacancies_repo


def _vacancy(vid: int = 1, **over) -> dict:
    base = {
        "id": vid,
        "name": f"Vacancy {vid}",
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


@pytest.mark.asyncio
async def test_upsert_inserts_new_row(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, name="первая"))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT name FROM vacancies WHERE id=1")
    assert (await cur.fetchone())[0] == "первая"


@pytest.mark.asyncio
async def test_upsert_updates_on_conflict(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, name="старое"))
    await vacancies_repo.upsert(tmp_db, _vacancy(1, name="новое"))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT name FROM vacancies WHERE id=1")
    assert (await cur.fetchone())[0] == "новое"


@pytest.mark.asyncio
async def test_archived_at_set_when_archived_true(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=True))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT archived_at FROM vacancies WHERE id=1")
    assert (await cur.fetchone())[0] is not None


@pytest.mark.asyncio
async def test_archived_at_not_reset_on_non_archived_upsert(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=True))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT archived_at FROM vacancies WHERE id=1")
    before = (await cur.fetchone())[0]
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=False))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT archived_at FROM vacancies WHERE id=1")
    after = (await cur.fetchone())[0]
    assert before == after  # не сбрасываем


@pytest.mark.asyncio
async def test_archived_at_null_when_never_archived(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=False))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT archived_at FROM vacancies WHERE id=1")
    assert (await cur.fetchone())[0] is None


@pytest.mark.asyncio
async def test_list_show_archived_hide(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=False))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, archived=True))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, show_archived="hide")
    ids = [r["id"] for r in rows]
    assert 1 in ids and 2 not in ids


@pytest.mark.asyncio
async def test_list_show_archived_only(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=False))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, archived=True))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, show_archived="only")
    ids = [r["id"] for r in rows]
    assert 2 in ids and 1 not in ids


@pytest.mark.asyncio
async def test_list_show_archived_all(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, archived=False))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, archived=True))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, show_archived="all")
    assert {r["id"] for r in rows} >= {1, 2}


@pytest.mark.asyncio
async def test_list_filter_only_remote(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, is_remote=1))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, is_remote=0))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, only_remote=True)
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_list_filter_text(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, name="Python Senior"))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, name="Go Backend"))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, text="Python")
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_list_filter_level(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, level="senior"))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, level="junior"))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, level="senior")
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_list_filter_salary_min(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, salary_rub=300000))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, salary_rub=100000))
    await tmp_db.commit()
    rows = await vacancies_repo.list_vacancies(tmp_db, salary_rub_min=200000)
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_count_vacancies(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1, is_remote=1))
    await vacancies_repo.upsert(tmp_db, _vacancy(2, is_remote=0))
    await tmp_db.commit()
    out = await vacancies_repo.count_vacancies(tmp_db)
    assert out["total"] == 2
    assert out["remote"] == 1


@pytest.mark.asyncio
async def test_set_status_creates_and_updates(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(1))
    await tmp_db.commit()
    await vacancies_repo.set_status(tmp_db, 1, "applied", "тест-нота")
    cur = await tmp_db.execute("SELECT status, note FROM vacancy_status WHERE vacancy_id=1")
    row = await cur.fetchone()
    assert row[0] == "applied"
    assert row[1] == "тест-нота"


@pytest.mark.asyncio
async def test_get_vacancy_returns_dict(tmp_db):
    await vacancies_repo.upsert(tmp_db, _vacancy(42, name="X"))
    await tmp_db.commit()
    v = await vacancies_repo.get_vacancy(tmp_db, 42)
    assert v["name"] == "X"
    assert v["parsed_stack"] == ["python"]


@pytest.mark.asyncio
async def test_get_vacancy_missing(tmp_db):
    assert await vacancies_repo.get_vacancy(tmp_db, 999) is None
