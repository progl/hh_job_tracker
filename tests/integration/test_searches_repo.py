import pytest

from app.db import searches_repo


async def _add_vacancy(db, vid: int):
    await db.execute(
        "INSERT INTO vacancies(id, name) VALUES (?, ?)",
        (vid, f"v{vid}"),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_create_and_get(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "python remote", {"text": "python", "remote": True})
    assert isinstance(sid, int) and sid > 0
    s = await searches_repo.get(tmp_db, sid)
    assert s is not None
    assert s["name"] == "python remote"
    assert s["params"] == {"text": "python", "remote": True}
    assert s["is_active"] == 1


@pytest.mark.asyncio
async def test_get_missing(tmp_db):
    assert await searches_repo.get(tmp_db, 9999) is None


@pytest.mark.asyncio
async def test_list_searches_includes_found_count(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "s1", {"x": 1})
    await _add_vacancy(tmp_db, 100)
    await _add_vacancy(tmp_db, 200)
    await searches_repo.mark_seen(tmp_db, sid, [100, 200])

    rows = await searches_repo.list_searches(tmp_db)
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["found_count"] == 2
    assert rows[0]["params"] == {"x": 1}


@pytest.mark.asyncio
async def test_update_search(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "old", {"a": 1})
    await searches_repo.update_search(tmp_db, sid, name="new", params={"a": 2}, is_active=0)
    s = await searches_repo.get(tmp_db, sid)
    assert s["name"] == "new"
    assert s["params"] == {"a": 2}
    assert s["is_active"] == 0


@pytest.mark.asyncio
async def test_update_search_skips_unknown_fields(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    # неизвестное поле — игнор. Также проверим: пустой dict → ранний return
    await searches_repo.update_search(tmp_db, sid, garbage="zzz")
    s = await searches_repo.get(tmp_db, sid)
    assert s["name"] == "x"


@pytest.mark.asyncio
async def test_delete_search_removes_seen(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    await _add_vacancy(tmp_db, 1)
    await searches_repo.mark_seen(tmp_db, sid, [1])
    await searches_repo.delete_search(tmp_db, sid)
    assert await searches_repo.get(tmp_db, sid) is None
    cur = await tmp_db.execute("SELECT COUNT(*) FROM search_vacancy_seen WHERE search_id=?", (sid,))
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_mark_seen_resets_disappeared(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    await _add_vacancy(tmp_db, 1)
    # пометим disappeared
    await tmp_db.execute("UPDATE vacancies SET disappeared_at='2024-01-01' WHERE id=1")
    await tmp_db.commit()
    await searches_repo.mark_seen(tmp_db, sid, [1])
    cur = await tmp_db.execute("SELECT disappeared_at, last_seen_at FROM vacancies WHERE id=1")
    r = await cur.fetchone()
    assert r["disappeared_at"] is None
    assert r["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_mark_seen_empty_list_noop(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    await searches_repo.mark_seen(tmp_db, sid, [])
    cur = await tmp_db.execute("SELECT COUNT(*) FROM search_vacancy_seen")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_mark_disappeared(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    await _add_vacancy(tmp_db, 1)
    await _add_vacancy(tmp_db, 2)
    # обе вакансии были видны давно
    await tmp_db.execute(
        "INSERT INTO search_vacancy_seen(search_id, vacancy_id, last_seen_at) VALUES (?, 1, '2024-01-01')",
        (sid,),
    )
    await tmp_db.execute(
        "INSERT INTO search_vacancy_seen(search_id, vacancy_id, last_seen_at) VALUES (?, 2, '2024-01-01')",
        (sid,),
    )
    await tmp_db.commit()

    # mark_disappeared с run_started в будущем — все должны стать disappeared
    n = await searches_repo.mark_disappeared(tmp_db, sid, "2030-01-01")
    assert n == 2
    cur = await tmp_db.execute("SELECT COUNT(*) FROM vacancies WHERE disappeared_at IS NOT NULL")
    assert (await cur.fetchone())[0] == 2

    # повторный вызов — уже ничего не меняет
    n2 = await searches_repo.mark_disappeared(tmp_db, sid, "2030-01-01")
    assert n2 == 0


@pytest.mark.asyncio
async def test_update_last_run(tmp_db):
    sid = await searches_repo.create_search(tmp_db, "x", {})
    s_before = await searches_repo.get(tmp_db, sid)
    assert s_before["last_run_at"] is None
    await searches_repo.update_last_run(tmp_db, sid)
    s_after = await searches_repo.get(tmp_db, sid)
    assert s_after["last_run_at"] is not None
