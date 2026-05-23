"""Дополнительное покрытие фильтров vacancies_repo.list_vacancies."""
import pytest

from app.db import vacancies_repo


async def _add_vac(db, vid: int, **over):
    base = {
        "id": vid,
        "name": f"Vac {vid}",
        "company_id": None,
        "company_name": "Some Co",
        "area_id": None,
        "area_name": "Moscow",
        "salary_from": None,
        "salary_to": None,
        "salary_currency": None,
        "salary_gross": None,
        "salary_rub": None,
        "work_schedule": None,
        "employment": None,
        "work_experience": None,
        "work_formats": "[]",
        "publication_time": None,
        "creation_time": None,
        "is_remote": 0,
        "is_remote_text": 0,
        "level": None,
        "key_skills": None,
        "parsed_stack": "[]",
        "responses_count": 0,
        "total_responses_count": 0,
        "online_users_count": 0,
        "description": "Some description",
        "raw_json": "{}",
        "url": f"https://hh.ru/{vid}",
        "archived": False,
    }
    base.update(over)
    await vacancies_repo.upsert(db, base)
    await db.commit()


@pytest.mark.asyncio
async def test_list_vacancies_status_filters(tmp_db):
    await _add_vac(tmp_db, 1)
    await _add_vac(tmp_db, 2)
    await vacancies_repo.set_status(tmp_db, 2, "applied")
    rows = await vacancies_repo.list_vacancies(tmp_db, statuses=["new"])
    assert {r["id"] for r in rows} == {1}
    rows = await vacancies_repo.list_vacancies(tmp_db, statuses_exclude=["new"])
    assert {r["id"] for r in rows} == {2}


@pytest.mark.asyncio
async def test_list_vacancies_neg_state_filters(tmp_db):
    await _add_vac(tmp_db, 1)
    await _add_vac(tmp_db, 2)
    # для vid=2 — добавим negotiation
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, last_state) VALUES (10, 2, 'RESPONSE')"
    )
    await tmp_db.commit()
    # neg_states=['none'] → только без переговоров
    rows = await vacancies_repo.list_vacancies(tmp_db, neg_states=["none"])
    assert {r["id"] for r in rows} == {1}
    rows = await vacancies_repo.list_vacancies(tmp_db, neg_states=["RESPONSE"])
    assert {r["id"] for r in rows} == {2}
    rows = await vacancies_repo.list_vacancies(tmp_db, neg_states_exclude=["none"])
    assert {r["id"] for r in rows} == {2}
    rows = await vacancies_repo.list_vacancies(tmp_db, neg_states_exclude=["RESPONSE"])
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_list_vacancies_text_and_stack_and_level(tmp_db):
    await _add_vac(tmp_db, 1, name="Python Senior", level="senior", parsed_stack='["python","django"]')
    await _add_vac(tmp_db, 2, name="Go Middle", level="middle", parsed_stack='["go"]')
    rows = await vacancies_repo.list_vacancies(tmp_db, text="Python")
    assert {r["id"] for r in rows} == {1}
    rows = await vacancies_repo.list_vacancies(tmp_db, stack_any=["go"])
    assert {r["id"] for r in rows} == {2}
    rows = await vacancies_repo.list_vacancies(tmp_db, level="senior")
    assert {r["id"] for r in rows} == {1}


@pytest.mark.asyncio
async def test_list_vacancies_salary_filter(tmp_db):
    await _add_vac(tmp_db, 1, salary_rub=100000)
    await _add_vac(tmp_db, 2, salary_rub=300000)
    rows = await vacancies_repo.list_vacancies(tmp_db, salary_rub_min=200000)
    assert {r["id"] for r in rows} == {2}


@pytest.mark.asyncio
async def test_list_vacancies_disappeared_archived_visibility(tmp_db):
    await _add_vac(tmp_db, 1)
    await _add_vac(tmp_db, 2)
    await _add_vac(tmp_db, 3)
    await tmp_db.execute("UPDATE vacancies SET disappeared_at='2024-01-01' WHERE id=2")
    await tmp_db.execute("UPDATE vacancies SET archived_at='2024-01-01' WHERE id=3")
    await tmp_db.commit()

    rows = await vacancies_repo.list_vacancies(tmp_db, show_disappeared="hide", show_archived="hide")
    assert {r["id"] for r in rows} == {1}
    rows = await vacancies_repo.list_vacancies(tmp_db, show_disappeared="only", show_archived="all")
    assert {r["id"] for r in rows} == {2}
    rows = await vacancies_repo.list_vacancies(tmp_db, show_disappeared="all", show_archived="only")
    assert {r["id"] for r in rows} == {3}
    rows = await vacancies_repo.list_vacancies(tmp_db, show_disappeared="all", show_archived="all")
    assert {r["id"] for r in rows} == {1, 2, 3}


@pytest.mark.asyncio
async def test_list_vacancies_sort_by_salary(tmp_db):
    await _add_vac(tmp_db, 1, salary_rub=100000)
    await _add_vac(tmp_db, 2, salary_rub=300000)
    await _add_vac(tmp_db, 3, salary_rub=200000)
    rows = await vacancies_repo.list_vacancies(tmp_db, sort_by="salary_rub", sort_dir="desc")
    assert [r["id"] for r in rows] == [2, 3, 1]
    rows = await vacancies_repo.list_vacancies(tmp_db, sort_by="salary_rub", sort_dir="asc")
    assert [r["id"] for r in rows] == [1, 3, 2]


@pytest.mark.asyncio
async def test_get_vacancy_source_from_negotiation(tmp_db):
    await _add_vac(tmp_db, 1)
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, last_state) VALUES (10, 1, 'RESPONSE')"
    )
    await tmp_db.commit()
    v = await vacancies_repo.get_vacancy(tmp_db, 1)
    assert v is not None
    assert v["source_list"] == ["из откликов"]


@pytest.mark.asyncio
async def test_get_vacancy_source_from_query(tmp_db):
    await _add_vac(tmp_db, 1)
    await tmp_db.execute(
        "INSERT INTO vacancy_collected_via(vacancy_id, query_text) VALUES (1, 'python remote')"
    )
    await tmp_db.commit()
    v = await vacancies_repo.get_vacancy(tmp_db, 1)
    assert v["source_list"] and "python remote" in v["source_list"][0]


@pytest.mark.asyncio
async def test_get_vacancy_missing_returns_none(tmp_db):
    assert await vacancies_repo.get_vacancy(tmp_db, 999) is None


@pytest.mark.asyncio
async def test_count_vacancies(tmp_db):
    await _add_vac(tmp_db, 1, is_remote=1)
    await _add_vac(tmp_db, 2)
    cnt = await vacancies_repo.count_vacancies(tmp_db)
    assert cnt["total"] == 2
    assert cnt["remote"] == 1
    assert "new" in cnt["by_status"]
