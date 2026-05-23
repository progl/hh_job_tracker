import pytest

from app.db import vacancies_repo


def _v(vid: int, name: str, company: str | None = "Co", **over) -> dict:
    base = {
        "id": vid, "name": name, "company_id": None, "company_name": company,
        "area_id": None, "area_name": None,
        "salary_from": None, "salary_to": None, "salary_currency": None,
        "salary_gross": False, "salary_rub": None,
        "work_schedule": None, "employment": None, "work_experience": None,
        "work_formats": "[]", "publication_time": None, "creation_time": None,
        "is_remote": 0, "is_remote_text": 0, "level": None,
        "key_skills": None, "parsed_stack": "[]",
        "responses_count": None, "total_responses_count": None, "online_users_count": None,
        "description": None, "raw_json": "{}", "url": f"https://hh.ru/vacancy/{vid}",
        "archived": False,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_find_duplicates_empty(tmp_db):
    assert await vacancies_repo.find_duplicates(tmp_db) == []


@pytest.mark.asyncio
async def test_find_duplicates_single_group(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(3, "Go Dev", "Acme"))  # не дубликат
    await tmp_db.commit()
    groups = await vacancies_repo.find_duplicates(tmp_db)
    assert len(groups) == 1
    assert groups[0]["ids"] == [1, 2]


@pytest.mark.asyncio
async def test_find_duplicates_case_and_space_normalized(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "  python DEV ", "  acme  "))
    await tmp_db.commit()
    groups = await vacancies_repo.find_duplicates(tmp_db)
    assert len(groups) == 1
    assert groups[0]["ids"] == [1, 2]


@pytest.mark.asyncio
async def test_find_duplicates_different_companies(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python Dev", "Beta"))
    await tmp_db.commit()
    groups = await vacancies_repo.find_duplicates(tmp_db)
    assert groups == []


@pytest.mark.asyncio
async def test_mark_duplicates_keeps_min_id_skips_rest(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(3, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(4, "Go Dev", "Acme"))  # одиночный
    await tmp_db.commit()
    res = await vacancies_repo.mark_duplicates_as_skipped(tmp_db)
    assert res == {"groups": 1, "marked": 2}
    cur = await tmp_db.execute(
        "SELECT vacancy_id, status FROM vacancy_status ORDER BY vacancy_id"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows[1] == "new"       # оставлен оригинал (min id)
    assert rows[2] == "skipped"
    assert rows[3] == "skipped"
    assert rows[4] == "new"       # не дубликат — не тронут


@pytest.mark.asyncio
async def test_mark_duplicates_note_references_kept_id(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "Python Dev", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python Dev", "Acme"))
    await tmp_db.commit()
    await vacancies_repo.mark_duplicates_as_skipped(tmp_db)
    cur = await tmp_db.execute("SELECT note FROM vacancy_status WHERE vacancy_id=2")
    assert "1" in (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_mark_duplicates_multiple_groups(tmp_db):
    await vacancies_repo.upsert(tmp_db, _v(1, "A", "X"))
    await vacancies_repo.upsert(tmp_db, _v(2, "A", "X"))
    await vacancies_repo.upsert(tmp_db, _v(3, "B", "Y"))
    await vacancies_repo.upsert(tmp_db, _v(4, "B", "Y"))
    await vacancies_repo.upsert(tmp_db, _v(5, "B", "Y"))
    await tmp_db.commit()
    res = await vacancies_repo.mark_duplicates_as_skipped(tmp_db)
    assert res == {"groups": 2, "marked": 3}


@pytest.mark.asyncio
async def test_mark_duplicates_empty(tmp_db):
    res = await vacancies_repo.mark_duplicates_as_skipped(tmp_db)
    assert res == {"groups": 0, "marked": 0}
