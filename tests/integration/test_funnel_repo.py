import pytest

from app.db import funnel_repo


async def _add_employer(db, eid: int, name: str | None = None, read_pct: int | None = None):
    await db.execute(
        "INSERT INTO employers(id, name, read_topic_percent) VALUES (?, ?, ?)",
        (eid, name, read_pct),
    )


async def _add_neg(
    db,
    nid: int,
    *,
    employer_id: int | None = None,
    last_state: str = "RESPONSE",
    archived: int = 0,
    viewed: int = 0,
    creation_time: str | None = None,
    last_modified: str | None = None,
):
    await db.execute(
        """
        INSERT INTO negotiations(
            id, vacancy_id, employer_id, last_state, archived, viewed_by_opponent,
            creation_time, last_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (nid, nid + 1000, employer_id, last_state, archived, viewed, creation_time, last_modified),
    )


async def _add_vacancy(db, vid: int, company_id: int | None, company_name: str | None):
    await db.execute(
        "INSERT INTO vacancies(id, name, company_id, company_name) VALUES (?, 'v', ?, ?)",
        (vid, company_id, company_name),
    )


@pytest.mark.asyncio
async def test_top_employers_counts_and_buckets(tmp_db):
    await _add_employer(tmp_db, 1, name="ACME", read_pct=80)
    await _add_employer(tmp_db, 2, name="Beta", read_pct=50)
    # Employer 1: 3 neg — 1 invite, 1 discard, 1 waiting
    await _add_neg(tmp_db, 1, employer_id=1, last_state="INVITATION")
    await _add_neg(tmp_db, 2, employer_id=1, last_state="DISCARD_BY_EMPLOYER")
    await _add_neg(tmp_db, 3, employer_id=1, last_state="RESPONSE", archived=0)
    # Employer 2: 1 neg waiting
    await _add_neg(tmp_db, 4, employer_id=2, last_state="RESPONSE", archived=0)
    # без employer_id — должен игнорироваться
    await _add_neg(tmp_db, 5, employer_id=None, last_state="RESPONSE")
    await tmp_db.commit()

    rows = await funnel_repo.top_employers(tmp_db, limit=10)
    by_id = {r["employer_id"]: r for r in rows}
    assert 1 in by_id and 2 in by_id
    assert by_id[1]["n"] == 3
    assert by_id[1]["interview"] == 1
    assert by_id[1]["discard"] == 1
    assert by_id[1]["waiting"] == 1
    assert by_id[1]["name"] == "ACME"
    assert by_id[1]["read_pct"] == 80
    assert by_id[2]["n"] == 1
    assert by_id[2]["waiting"] == 1
    # сортировка по n DESC
    assert rows[0]["employer_id"] == 1


@pytest.mark.asyncio
async def test_top_employers_falls_back_to_vacancy_company_name(tmp_db):
    # employer без name — берём из vacancies.company_name
    await _add_employer(tmp_db, 10, name=None)
    await _add_vacancy(tmp_db, 500, company_id=10, company_name="FallbackInc")
    await _add_neg(tmp_db, 100, employer_id=10, last_state="RESPONSE")
    await tmp_db.commit()
    rows = await funnel_repo.top_employers(tmp_db)
    assert rows[0]["name"] == "FallbackInc"


@pytest.mark.asyncio
async def test_backfill_employer_names(tmp_db):
    await _add_employer(tmp_db, 1, name=None)
    await _add_employer(tmp_db, 2, name="Already")
    await _add_vacancy(tmp_db, 100, company_id=1, company_name="FromVac")
    await _add_vacancy(tmp_db, 200, company_id=2, company_name="Other")
    await tmp_db.commit()
    updated = await funnel_repo.backfill_employer_names(tmp_db)
    assert updated == 1
    cur = await tmp_db.execute("SELECT id, name FROM employers ORDER BY id")
    rows = await cur.fetchall()
    names = {r["id"]: r["name"] for r in rows}
    assert names[1] == "FromVac"
    assert names[2] == "Already"


@pytest.mark.asyncio
async def test_by_week_histogram(tmp_db):
    await _add_neg(tmp_db, 1, last_state="RESPONSE", viewed=1, creation_time="2026-05-01T10:00:00")
    await _add_neg(tmp_db, 2, last_state="INVITATION", viewed=1, creation_time="2026-05-02T10:00:00")
    await _add_neg(tmp_db, 3, last_state="DISCARD_BY_X", creation_time="2026-05-03T10:00:00")
    await _add_neg(tmp_db, 4, last_state="RESPONSE", creation_time="2026-04-25T10:00:00", archived=0)
    # без creation_time — отфильтруется
    await _add_neg(tmp_db, 5, last_state="RESPONSE", creation_time=None)
    await tmp_db.commit()

    rows = await funnel_repo.by_week(tmp_db)
    # как минимум 2 недели
    assert len(rows) >= 2
    total = sum(r["total"] for r in rows)
    assert total == 4
    viewed_sum = sum(r["viewed"] for r in rows)
    invite_sum = sum(r["interview"] for r in rows)
    discard_sum = sum(r["discard"] for r in rows)
    assert viewed_sum == 2
    assert invite_sum == 1
    assert discard_sum == 1


@pytest.mark.asyncio
async def test_avg_hr_response_hours_none_when_empty(tmp_db):
    assert await funnel_repo.avg_hr_response_hours(tmp_db) is None


@pytest.mark.asyncio
async def test_avg_hr_response_hours_median(tmp_db):
    # 3 viewed: дельты 1h, 2h, 4h → медиана = 2h
    await _add_neg(
        tmp_db,
        1,
        viewed=1,
        creation_time="2026-05-01T10:00:00",
        last_modified="2026-05-01T11:00:00",
    )
    await _add_neg(
        tmp_db,
        2,
        viewed=1,
        creation_time="2026-05-01T10:00:00",
        last_modified="2026-05-01T12:00:00",
    )
    await _add_neg(
        tmp_db,
        3,
        viewed=1,
        creation_time="2026-05-01T10:00:00",
        last_modified="2026-05-01T14:00:00",
    )
    # не viewed — не учитывается
    await _add_neg(
        tmp_db,
        4,
        viewed=0,
        creation_time="2026-05-01T10:00:00",
        last_modified="2026-05-01T20:00:00",
    )
    await tmp_db.commit()
    v = await funnel_repo.avg_hr_response_hours(tmp_db)
    assert v == 2.0


@pytest.mark.asyncio
async def test_avg_hr_response_hours_skips_invalid_and_negative(tmp_db):
    # invalid date — except — пропуск
    await _add_neg(
        tmp_db,
        1,
        viewed=1,
        creation_time="garbage",
        last_modified="also garbage",
    )
    # отрицательная дельта — фильтруется
    await _add_neg(
        tmp_db,
        2,
        viewed=1,
        creation_time="2026-05-01T12:00:00",
        last_modified="2026-05-01T10:00:00",
    )
    await tmp_db.commit()
    assert await funnel_repo.avg_hr_response_hours(tmp_db) is None
