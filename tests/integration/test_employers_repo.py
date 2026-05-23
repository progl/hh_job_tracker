import pytest

from app.db import employers_repo


@pytest.mark.asyncio
async def test_upsert_politeness_inserts(tmp_db):
    pmap = {
        "1001": {"employerId": 1001, "allTopicCount": 100, "readTopicPercent": 85, "replyTotalWorkingTimeDays": 2.0},
        "1002": {"employerId": 1002, "allTopicCount": 50, "readTopicPercent": 60, "replyTotalWorkingTimeDays": 4.5},
    }
    saved = await employers_repo.upsert_politeness(tmp_db, pmap)
    await tmp_db.commit()
    assert saved == 2
    m = await employers_repo.get_map(tmp_db)
    assert m[1001]["read_topic_percent"] == 85
    assert m[1002]["reply_working_days"] == 4.5


@pytest.mark.asyncio
async def test_upsert_politeness_updates(tmp_db):
    pmap = {"1001": {"employerId": 1001, "allTopicCount": 100, "readTopicPercent": 50, "replyTotalWorkingTimeDays": 3.0}}
    await employers_repo.upsert_politeness(tmp_db, pmap)
    pmap = {"1001": {"employerId": 1001, "allTopicCount": 150, "readTopicPercent": 80, "replyTotalWorkingTimeDays": 1.0}}
    await employers_repo.upsert_politeness(tmp_db, pmap)
    await tmp_db.commit()
    m = await employers_repo.get_map(tmp_db)
    assert m[1001]["read_topic_percent"] == 80
    assert m[1001]["reply_working_days"] == 1.0


@pytest.mark.asyncio
async def test_upsert_politeness_skips_non_dict(tmp_db):
    pmap = {"1001": "garbage", "1002": {"employerId": 1002, "readTopicPercent": 70}}
    saved = await employers_repo.upsert_politeness(tmp_db, pmap)
    await tmp_db.commit()
    assert saved == 1


@pytest.mark.asyncio
async def test_get_map_empty(tmp_db):
    assert await employers_repo.get_map(tmp_db) == {}
