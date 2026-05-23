import pytest

from app.db import negotiations_repo


def _neg(nid: int, vid: int, **over) -> dict:
    base = {
        "id": nid, "vacancy_id": vid, "employer_id": 1000,
        "employer_manager_id": None, "resume_id": None,
        "last_state": "RESPONSE", "last_employer_state": "RESPONSE",
        "applicant_sub_state": None, "employer_sub_state": None,
        "initial_topic_type": None, "current_topic_type": None,
        "archived": 0, "declined_by_applicant": 0, "viewed_by_opponent": 0,
        "has_new_messages": 0, "has_response_letter": 0,
        "conversation_messages": 0,
        "creation_time": None, "last_modified": "2026-05-22T10:00:00",
        "raw_json": "{}",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_upsert_creates_negotiation(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT id FROM negotiations WHERE id=1")
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_snapshot_on_state_change(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100, last_employer_state="RESPONSE"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100, last_employer_state="INVITATION"))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT COUNT(*) FROM status_snapshots WHERE negotiation_id=1")
    assert (await cur.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_no_snapshot_when_state_unchanged(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100))
    await tmp_db.commit()
    cur = await tmp_db.execute("SELECT COUNT(*) FROM status_snapshots WHERE negotiation_id=1")
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_counters_basic(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100, last_state="RESPONSE", viewed_by_opponent=1))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(2, 200, last_state="INVITATION", viewed_by_opponent=1))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(3, 300, last_state="DISCARD"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(4, 400, last_state="RESPONSE", archived=1))
    await tmp_db.commit()
    c = await negotiations_repo.counters(tmp_db)
    assert c["total"] == 4
    assert c["viewed"] == 2
    assert c["invited"] == 1
    assert c["rejected"] == 1
    assert c["archived"] == 1
    assert c["waiting"] == 1  # RESPONSE и не archived


@pytest.mark.asyncio
async def test_by_vacancy_returns_latest(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100, last_state="RESPONSE"))
    await tmp_db.commit()
    n = await negotiations_repo.by_vacancy(tmp_db, 100)
    assert n["id"] == 1


@pytest.mark.asyncio
async def test_by_vacancy_missing(tmp_db):
    assert await negotiations_repo.by_vacancy(tmp_db, 999) is None


@pytest.mark.asyncio
async def test_map_vacancy_to_state(tmp_db):
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(1, 100, last_state="RESPONSE"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, _neg(2, 200, last_state="INVITATION"))
    await tmp_db.commit()
    m = await negotiations_repo.map_vacancy_to_state(tmp_db)
    assert m[100]["last_state"] == "RESPONSE"
    assert m[200]["last_state"] == "INVITATION"


def test_from_topic_item_maps_fields():
    item = {
        "id": 42, "vacancyId": 100, "employerId": 200,
        "lastState": "INVITATION", "lastEmployerState": "INVITATION",
        "archived": True, "viewedByOpponent": True, "hasResponseLetter": True,
        "conversationMessagesCount": 3,
    }
    n = negotiations_repo.from_topic_item(item)
    assert n["id"] == 42 and n["vacancy_id"] == 100
    assert n["archived"] == 1
    assert n["viewed_by_opponent"] == 1
    assert n["last_state"] == "INVITATION"
