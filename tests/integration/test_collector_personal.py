"""Тесты сборки откликов (personal): collect_negotiations, _sync_local_statuses,
last_sync маркер, sync state mapping."""
from __future__ import annotations

import html as _html
import json

import pytest

from app.collector import personal as personal_col


def _topic(nid: int, vid: int, state: str = "RESPONSE", last_modified: str = "2024-05-01T10:00:00",
           viewed: bool = False, archived: bool = False) -> dict:
    return {
        "id": nid, "vacancyId": vid, "employerId": 1, "resumeId": 555,
        "lastState": state, "lastEmployerState": state,
        "viewedByOpponent": viewed, "archived": archived,
        "lastModified": last_modified, "creationTime": "2024-01-01T00:00:00",
    }


def _state_html(topics: list[dict], paging: dict | None = None, account: dict | None = None,
                politeness: dict | None = None) -> str:
    state = {
        "applicantNegotiations": {"topicList": topics, "paging": paging or {}},
        "account": account or {"firstName": "Иван", "lastName": "Иванов"},
        "applicantEmployerPoliteness": {"employerPolitenessIndexes": politeness or {}},
    }
    return f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'


class _FakeClient:
    def __init__(self, pages: list[str]):
        self.pages = pages
        self.calls: list[tuple[str, dict | None]] = []

    @property
    def status(self):
        return {"paused_now": False, "paused_until": 0}

    async def get_page(self, path: str, params: dict | None = None) -> str:
        idx = len(self.calls)
        self.calls.append((path, params))
        return self.pages[idx] if idx < len(self.pages) else ""


# ----- collect_negotiations ----


@pytest.mark.asyncio
async def test_collect_negotiations_basic_one_page(tmp_db):
    html = _state_html([_topic(1, 100), _topic(2, 101)])
    client = _FakeClient([html])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=3)
    assert res["saved_negotiations"] == 2
    assert res["pages"] == 1
    assert res["resume_id"] == 555
    cur = await tmp_db.execute("SELECT COUNT(*) FROM negotiations")
    assert (await cur.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_collect_negotiations_empty_topic_list_breaks(tmp_db):
    html = _state_html([])
    client = _FakeClient([html])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=3)
    assert res["saved_negotiations"] == 0
    assert res["pages"] == 0


@pytest.mark.asyncio
async def test_collect_negotiations_no_initial_state(tmp_db):
    client = _FakeClient(["<html>nothing</html>"])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=2)
    assert res["pages"] == 0


@pytest.mark.asyncio
async def test_collect_negotiations_smart_stop_on_old_items_incremental(tmp_db):
    # сначала кладём первый sync с lastModified
    await personal_col._save_last_sync(tmp_db, "2024-06-01T00:00:00")
    await tmp_db.commit()
    # инкрементальный sync: все items старее last_sync_iso → smart-stop после 1й страницы
    p1 = _state_html(
        [_topic(1, 100, last_modified="2024-05-01T00:00:00")],
        paging={"next": {"disabled": False}},
    )
    p2 = _state_html([_topic(2, 200)])
    client = _FakeClient([p1, p2])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=3, full=False)
    assert res["stopped_early"] is True
    assert res["mode"] == "incremental"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_collect_negotiations_full_ignores_last_sync(tmp_db):
    await personal_col._save_last_sync(tmp_db, "2099-01-01T00:00:00")
    await tmp_db.commit()
    p1 = _state_html(
        [_topic(1, 100, last_modified="2024-05-01T00:00:00")],
        paging={"next": {"disabled": True}},
    )
    client = _FakeClient([p1])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=2, full=True)
    assert res["mode"] == "full"
    assert res["stopped_early"] is False
    assert res["saved_negotiations"] == 1


@pytest.mark.asyncio
async def test_collect_negotiations_paginates(tmp_db):
    p1 = _state_html([_topic(1, 100)], paging={"next": {"disabled": False}})
    p2 = _state_html([_topic(2, 200)], paging={"next": {"disabled": True}})
    client = _FakeClient([p1, p2])
    res = await personal_col.collect_negotiations(client, tmp_db, max_pages=5, full=True)
    assert res["pages"] == 2
    assert res["saved_negotiations"] == 2


@pytest.mark.asyncio
async def test_collect_negotiations_progress_cb(tmp_db):
    html = _state_html([_topic(1, 100)], paging={"next": {"disabled": True}})
    client = _FakeClient([html])
    calls = []
    await personal_col.collect_negotiations(
        client, tmp_db, max_pages=2, full=True,
        progress_cb=lambda **kw: calls.append(kw),
    )
    assert calls
    assert any("стр" in (c.get("message") or "") for c in calls)


@pytest.mark.asyncio
async def test_collect_negotiations_writes_profile(tmp_db):
    from app.db import profile_repo
    html = _state_html([_topic(1, 100)], account={"firstName": "Тест", "lastName": "Тестов"})
    client = _FakeClient([html])
    await personal_col.collect_negotiations(client, tmp_db, max_pages=1, full=True)
    p = await profile_repo.get_profile(tmp_db)
    assert p is not None
    assert "Тест" in (p.get("full_name") or "")


@pytest.mark.asyncio
async def test_collect_negotiations_saves_last_sync_marker(tmp_db):
    html = _state_html([
        _topic(1, 100, last_modified="2024-05-01T00:00:00"),
        _topic(2, 101, last_modified="2024-08-15T00:00:00"),
    ], paging={"next": {"disabled": True}})
    client = _FakeClient([html])
    await personal_col.collect_negotiations(client, tmp_db, max_pages=1, full=True)
    marker = await personal_col._load_last_sync(tmp_db)
    # должен быть самый свежий lastModified
    assert marker == "2024-08-15T00:00:00"


# ----- last_sync helpers ----


@pytest.mark.asyncio
async def test_load_last_sync_empty(tmp_db):
    assert await personal_col._load_last_sync(tmp_db) is None


@pytest.mark.asyncio
async def test_save_then_load_last_sync(tmp_db):
    await personal_col._save_last_sync(tmp_db, "2024-12-01T00:00:00")
    await tmp_db.commit()
    assert await personal_col._load_last_sync(tmp_db) == "2024-12-01T00:00:00"


@pytest.mark.asyncio
async def test_save_last_sync_overwrites(tmp_db):
    await personal_col._save_last_sync(tmp_db, "2024-01-01T00:00:00")
    await personal_col._save_last_sync(tmp_db, "2024-12-01T00:00:00")
    await tmp_db.commit()
    assert await personal_col._load_last_sync(tmp_db) == "2024-12-01T00:00:00"


# ----- _sync_local_statuses ----


@pytest.mark.asyncio
async def test_sync_local_statuses_discard_to_rejected(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(10, "X"))
    n = negotiations_repo.from_topic_item(_topic(101, 10, state="DISCARD"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.commit()
    updated = await personal_col._sync_local_statuses(tmp_db)
    assert updated == 1
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=10")
    assert (await cur.fetchone())[0] == "rejected"


@pytest.mark.asyncio
async def test_sync_local_statuses_invitation_to_interview(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(11, "X"))
    n = negotiations_repo.from_topic_item(_topic(102, 11, state="INVITATION"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.commit()
    await personal_col._sync_local_statuses(tmp_db)
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=11")
    assert (await cur.fetchone())[0] == "interview"


@pytest.mark.asyncio
async def test_sync_local_statuses_hired_to_offer(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(12, "X"))
    n = negotiations_repo.from_topic_item(_topic(103, 12, state="HIRED"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.commit()
    await personal_col._sync_local_statuses(tmp_db)
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=12")
    assert (await cur.fetchone())[0] == "offer"


@pytest.mark.asyncio
async def test_sync_local_statuses_response_to_applied(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(13, "X"))
    n = negotiations_repo.from_topic_item(_topic(104, 13, state="RESPONSE"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.commit()
    await personal_col._sync_local_statuses(tmp_db)
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=13")
    assert (await cur.fetchone())[0] == "applied"


@pytest.mark.asyncio
async def test_sync_local_statuses_skipped_not_overridden(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(14, "X"))
    n = negotiations_repo.from_topic_item(_topic(105, 14, state="INVITATION"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.execute(
        "INSERT OR REPLACE INTO vacancy_status(vacancy_id, status) VALUES (?, 'skipped')",
        (14,),
    )
    await tmp_db.commit()
    updated = await personal_col._sync_local_statuses(tmp_db)
    assert updated == 0
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=14")
    assert (await cur.fetchone())[0] == "skipped"


@pytest.mark.asyncio
async def test_sync_local_statuses_no_downgrade(tmp_db):
    # текущий status='offer', новый mapped 'rejected' (DISCARD): не должен опускаться
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(15, "X"))
    n = negotiations_repo.from_topic_item(_topic(106, 15, state="DISCARD"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.execute(
        "INSERT OR REPLACE INTO vacancy_status(vacancy_id, status) VALUES (?, 'offer')",
        (15,),
    )
    await tmp_db.commit()
    await personal_col._sync_local_statuses(tmp_db)
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=15")
    # offer (order=5) > rejected (order=4) → не меняем
    assert (await cur.fetchone())[0] == "offer"


@pytest.mark.asyncio
async def test_sync_local_statuses_unknown_state_skipped(tmp_db):
    from app.db import vacancies_repo, negotiations_repo
    from tests.integration.test_dedup import _v
    await vacancies_repo.upsert(tmp_db, _v(16, "X"))
    n = negotiations_repo.from_topic_item(_topic(107, 16, state="WEIRD_STATE"))
    await negotiations_repo.upsert_and_snapshot(tmp_db, n)
    await tmp_db.commit()
    updated = await personal_col._sync_local_statuses(tmp_db)
    assert updated == 0


# ----- collect_resume ----


@pytest.mark.asyncio
async def test_collect_resume_picks_first_when_no_id(tmp_db):
    state = {
        "applicantResumes": [
            {"_attributes": {"id": "r1"}, "title": [{"string": "Pythonist"}]},
            {"_attributes": {"id": "r2"}, "title": [{"string": "Other"}]},
        ]
    }
    html = f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'
    client = _FakeClient([html])
    res = await personal_col.collect_resume(client, tmp_db)
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_collect_resume_picks_by_id(tmp_db):
    state = {
        "applicantResumes": [
            {"_attributes": {"id": "r1"}, "title": [{"string": "Pythonist"}]},
            {"_attributes": {"id": "r2"}, "title": [{"string": "Java dev"}]},
        ]
    }
    html = f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'
    client = _FakeClient([html])
    res = await personal_col.collect_resume(client, tmp_db, resume_id="r2")
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_collect_resume_no_state(tmp_db):
    client = _FakeClient(["<html>nope</html>"])
    res = await personal_col.collect_resume(client, tmp_db)
    assert res["ok"] is False
    assert res["reason"] == "no_state"


@pytest.mark.asyncio
async def test_collect_resume_no_resumes_block(tmp_db):
    state = {"applicantResumes": []}
    html = f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'
    client = _FakeClient([html])
    res = await personal_col.collect_resume(client, tmp_db)
    assert res["ok"] is False
    assert res["reason"] == "no_resumes_block"
