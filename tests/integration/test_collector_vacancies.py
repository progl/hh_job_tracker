"""Тесты сборки вакансий: vacancy_from_search_item, collect_search, backfill_from_negotiations.

hh_client.get_page мокается, БД настоящая (tmp_db).
"""

from __future__ import annotations

import html as _html
import json

import pytest

from app.collector import vacancies as col


def _exc_classes():
    """Получаем актуальные ссылки на классы — e2e conftest мог reimport app.clients.hh."""
    from app.clients.hh import (
        AntibotChallengeError,
        SessionExpiredError,
        VacancyUnavailableError,
    )

    return AntibotChallengeError, SessionExpiredError, VacancyUnavailableError


def _search_item(vid: int, name: str = "Python Dev", company: str = "Acme", **over) -> dict:
    base = {
        "vacancyId": vid,
        "name": name,
        "company": {"id": 100, "name": company},
        "area": {"id": 1, "name": "Москва"},
        "compensation": {"from": 100000, "to": 200000, "currencyCode": "RUR", "gross": False},
        "@workSchedule": "fullDay",
        "employmentForm": "FULL",
        "workFormats": [{"id": "office"}],
        "workExperience": "between1And3",
        "publicationTime": {"@timestamp": 1700000000},
        "creationTime": "2024-01-01",
        "description": "Senior Python dev. Django, FastAPI",
        "responsesCount": 5,
        "totalResponsesCount": 50,
        "online_users_count": 3,
    }
    base.update(over)
    return base


def _wrap_state(items: list[dict], paging: dict | None = None, total: int = 100) -> str:
    state = {
        "vacancySearchResult": {
            "totalResults": total,
            "vacancies": items,
            "paging": paging or {},
        }
    }
    return f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'


def _wrap_view_state(vid: int, name: str = "Detail", archived: bool = False, descr: str = "DescX") -> str:
    state = {
        "vacancyView": {
            "vacancyId": vid,
            "name": name,
            "company": {"id": 1, "name": "Co"},
            "area": {"id": 1, "name": "M"},
            "compensation": {},
            "description": descr,
            "keySkills": [{"name": "Python"}, "Django"],
            "@archived": archived,
        }
    }
    return f'<template id="HH-Lux-InitialState">{_html.escape(json.dumps(state))}</template>'


# ----- vacancy_from_search_item ----


def test_vacancy_from_search_item_basic():
    item = _search_item(42, name="Python Dev")
    v = col.vacancy_from_search_item(item)
    assert v["id"] == 42
    assert v["name"] == "Python Dev"
    assert v["company_id"] == 100
    assert v["company_name"] == "Acme"
    assert v["area_id"] == 1
    assert v["salary_from"] == 100000
    assert v["url"] == "https://hh.ru/vacancy/42"
    # parsed_stack — JSON
    stack = json.loads(v["parsed_stack"])
    assert "python" in stack or "fastapi" in stack or "django" in stack


def test_vacancy_from_search_item_visible_name_fallback():
    item = _search_item(1)
    item["company"] = {"id": 1, "visibleName": "VisCo"}
    v = col.vacancy_from_search_item(item)
    assert v["company_name"] == "VisCo"


def test_vacancy_from_search_item_remote_explicit_via_schedule():
    item = _search_item(2)
    item["@workSchedule"] = "remote"
    v = col.vacancy_from_search_item(item)
    assert v["is_remote"] == 1


def test_vacancy_from_search_item_remote_explicit_via_work_formats():
    item = _search_item(3)
    item["@workSchedule"] = "fullDay"
    item["workFormats"] = [{"id": "REMOTE"}]
    v = col.vacancy_from_search_item(item)
    assert v["is_remote"] == 1


def test_vacancy_from_search_item_non_remote():
    item = _search_item(4)
    v = col.vacancy_from_search_item(item)
    assert v["is_remote"] == 0


# ----- collect_search ----


class _FakeClient:
    """Заменяет HHClient для коллектора — реализует get_page и status."""

    def __init__(self, pages: list[str], raise_on: dict[int, Exception] | None = None):
        self.pages = pages
        self.raise_on = raise_on or {}
        self.calls: list[tuple[str, dict | None]] = []
        self.paused_now = False

    @property
    def status(self):
        return {"paused_now": self.paused_now, "paused_until": 0}

    async def get_page(self, path: str, params: dict | None = None) -> str:
        idx = len(self.calls)
        self.calls.append((path, params))
        if idx in self.raise_on:
            raise self.raise_on[idx]
        if idx >= len(self.pages):
            return ""
        return self.pages[idx]


@pytest.mark.asyncio
async def test_collect_search_saves_vacancies_single_page(tmp_db):
    html = _wrap_state([_search_item(1), _search_item(2)], paging={})
    client = _FakeClient([html])
    res = await col.collect_search(client, tmp_db, {"text": "python"}, max_pages=3)
    assert res["saved"] == 2
    assert res["pages"] == 1
    assert res["total_results"] == 100
    # вакансии в БД
    cur = await tmp_db.execute("SELECT COUNT(*) FROM vacancies")
    assert (await cur.fetchone())[0] == 2
    # vacancy_collected_via записано
    cur = await tmp_db.execute("SELECT COUNT(*) FROM vacancy_collected_via WHERE query_text='python'")
    assert (await cur.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_collect_search_stops_on_disabled_next(tmp_db):
    page1 = _wrap_state([_search_item(1)], paging={"next": {"disabled": True}, "lastPage": {"page": 0}})
    client = _FakeClient([page1, _wrap_state([_search_item(2)])])  # 2-й не должен быть запрошен
    res = await col.collect_search(client, tmp_db, {"text": "go"}, max_pages=5)
    assert res["pages"] == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_collect_search_paginates_multiple_pages(tmp_db):
    p1 = _wrap_state([_search_item(1)], paging={"next": {"disabled": False}, "lastPage": {"page": 1}})
    p2 = _wrap_state([_search_item(2)], paging={"next": {"disabled": True}, "lastPage": {"page": 1}})
    client = _FakeClient([p1, p2])
    res = await col.collect_search(client, tmp_db, {"text": "x"}, max_pages=5)
    assert res["pages"] == 2
    assert res["saved"] == 2


@pytest.mark.asyncio
async def test_collect_search_empty_items_breaks(tmp_db):
    page1 = _wrap_state([])
    client = _FakeClient([page1])
    res = await col.collect_search(client, tmp_db, {"text": "z"}, max_pages=3)
    assert res["saved"] == 0
    assert res["pages"] == 0


@pytest.mark.asyncio
async def test_collect_search_no_initial_state(tmp_db):
    client = _FakeClient(["<html>no state</html>"])
    res = await col.collect_search(client, tmp_db, {"text": "z"}, max_pages=3)
    assert res["saved"] == 0
    assert res["pages"] == 0


@pytest.mark.asyncio
async def test_collect_search_progress_callback(tmp_db):
    html = _wrap_state([_search_item(1)], paging={"next": {"disabled": True}, "lastPage": {"page": 0}})
    client = _FakeClient([html])
    calls: list[dict] = []

    def cb(**kw):
        calls.append(kw)

    await col.collect_search(client, tmp_db, {"text": "a"}, max_pages=2, progress_cb=cb)
    assert any(c.get("message", "").startswith("старт") for c in calls)
    assert any("стр " in (c.get("message") or "") for c in calls)


@pytest.mark.asyncio
async def test_collect_search_dedup_upsert_same_id(tmp_db):
    html1 = _wrap_state([_search_item(7, name="A")])
    html2 = _wrap_state([_search_item(7, name="B")])
    client = _FakeClient([html1, html2])
    await col.collect_search(client, tmp_db, {"text": "q1"}, max_pages=1)
    await col.collect_search(client, tmp_db, {"text": "q2"}, max_pages=1)
    cur = await tmp_db.execute("SELECT COUNT(*), MAX(name) FROM vacancies WHERE id=7")
    cnt, name = await cur.fetchone()
    assert cnt == 1
    assert name == "B"


@pytest.mark.asyncio
async def test_collect_search_with_resume_adds_resumelist_markers(tmp_db):
    """Если в params есть resume — collect_search добавляет from=resumelist&hhtmFrom=resume_list."""
    html = _wrap_state([_search_item(42)], paging={})
    client = _FakeClient([html])
    await col.collect_search(client, tmp_db, {"resume": "abc123"}, max_pages=1)
    assert len(client.calls) == 1
    _, params = client.calls[0]
    assert params["resume"] == "abc123"
    assert params["from"] == "resumelist"
    assert params["hhtmFrom"] == "resume_list"


@pytest.mark.asyncio
async def test_collect_search_without_resume_no_markers(tmp_db):
    """Без resume — никаких новых маркеров не добавляется."""
    html = _wrap_state([_search_item(43)], paging={})
    client = _FakeClient([html])
    await col.collect_search(client, tmp_db, {"text": "python"}, max_pages=1)
    _, params = client.calls[0]
    assert "from" not in params
    assert "hhtmFrom" not in params


@pytest.mark.asyncio
async def test_collect_search_resume_preserves_existing_from(tmp_db):
    """Если caller уже указал from — не перезатираем."""
    html = _wrap_state([_search_item(44)], paging={})
    client = _FakeClient([html])
    await col.collect_search(client, tmp_db, {"resume": "x", "from": "custom"}, max_pages=1)
    _, params = client.calls[0]
    assert params["from"] == "custom"


def test_vacancy_from_search_item_legacy_workformats():
    """Старый формат HH: [{'id': 'REMOTE'}, {'id': 'HYBRID'}]"""
    item = _search_item(1, workFormats=[{"id": "REMOTE"}, {"id": "HYBRID"}])
    v = col.vacancy_from_search_item(item)
    wf = json.loads(v["work_formats"])
    assert wf == ["REMOTE", "HYBRID"]
    assert v["is_remote"] == 1


def test_vacancy_from_search_item_new_workformats():
    """Новый формат HH: [{'workFormatsElement': ['ON_SITE', 'HYBRID']}]"""
    item = _search_item(2, workFormats=[{"workFormatsElement": ["ON_SITE", "HYBRID"]}])
    v = col.vacancy_from_search_item(item)
    wf = json.loads(v["work_formats"])
    assert wf == ["ON_SITE", "HYBRID"]
    assert v["is_remote"] == 0


def test_vacancy_from_search_item_new_workformats_with_remote():
    """Новый формат с REMOTE — is_remote=1."""
    item = _search_item(3, workFormats=[{"workFormatsElement": ["REMOTE", "HYBRID"]}])
    v = col.vacancy_from_search_item(item)
    wf = json.loads(v["work_formats"])
    assert "REMOTE" in wf and "HYBRID" in wf
    assert v["is_remote"] == 1


def test_vacancy_from_search_item_flat_workformats():
    """Плоский массив строк (на всякий случай)."""
    item = _search_item(4, workFormats=["REMOTE", "ON_SITE"])
    v = col.vacancy_from_search_item(item)
    wf = json.loads(v["work_formats"])
    assert wf == ["REMOTE", "ON_SITE"]


def test_vacancy_from_search_item_empty_workformats():
    """Пустой/отсутствующий workFormats — пустой массив, не [null]."""
    item = _search_item(5, workFormats=[])
    v = col.vacancy_from_search_item(item)
    assert json.loads(v["work_formats"]) == []


@pytest.mark.asyncio
async def test_collect_search_early_stop_consecutive_seen(tmp_db):
    """K подряд seen → stop. Не дочитываем остаток страниц, не вызываем mark_disappeared."""
    from app.db import searches_repo

    sid = await searches_repo.create_search(tmp_db, "py", {"text": "py"})
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (10, 'a'), (11, 'b'), (12, 'c'), (13, 'd')")
    await tmp_db.commit()
    await searches_repo.mark_seen(tmp_db, sid, [10, 11, 12, 13])

    # 1 новый, потом 3 подряд seen → K=3 триггерит stop, page 1 не запрашивается
    page1 = _wrap_state(
        [_search_item(99), _search_item(10), _search_item(11), _search_item(12)],
        paging={"next": {"disabled": False}, "lastPage": {"page": 5}},
    )
    page2 = _wrap_state([_search_item(100)])  # не должна запрашиваться
    client = _FakeClient([page1, page2])
    res = await col.collect_search(
        client,
        tmp_db,
        {"text": "py"},
        max_pages=5,
        search_id=sid,
        early_stop_consecutive_seen=3,
    )
    assert res["partial"] is True
    assert len(client.calls) == 1
    assert res["disappeared"] == 0


@pytest.mark.asyncio
async def test_collect_search_consecutive_resets_on_new(tmp_db):
    """seen, new, seen, seen — счётчик сбрасывается на new и K=3 не срабатывает."""
    from app.db import searches_repo

    sid = await searches_repo.create_search(tmp_db, "py", {"text": "py"})
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (10, 'a'), (11, 'b')")
    await tmp_db.commit()
    await searches_repo.mark_seen(tmp_db, sid, [10, 11])

    page1 = _wrap_state(
        [_search_item(10), _search_item(50), _search_item(11), _search_item(10)],
        paging={"next": {"disabled": True}},
    )
    client = _FakeClient([page1])
    res = await col.collect_search(
        client,
        tmp_db,
        {"text": "py"},
        max_pages=3,
        search_id=sid,
        early_stop_consecutive_seen=3,
    )
    assert res["partial"] is False
    assert res["saved"] == 4


@pytest.mark.asyncio
async def test_collect_search_skipped_counts_toward_early_stop(tmp_db):
    """Скипнутые вакансии (vacancy_status.status='skipped') тоже идут в счётчик early-stop —
    не имеет смысла снова их перебирать."""
    from app.db import searches_repo

    sid = await searches_repo.create_search(tmp_db, "py", {"text": "py"})
    # 3 вакансии: пометим как skipped (ещё НЕ были в search_vacancy_seen этого поиска)
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (20, 'a'), (21, 'b'), (22, 'c')")
    for vid in (20, 21, 22):
        await tmp_db.execute(
            "INSERT INTO vacancy_status(vacancy_id, status) VALUES (?, 'skipped')",
            (vid,),
        )
    await tmp_db.commit()

    # 1 новый + 3 скипнутых подряд → K=3 триггерит stop
    page1 = _wrap_state(
        [_search_item(999), _search_item(20), _search_item(21), _search_item(22)],
        paging={"next": {"disabled": False}, "lastPage": {"page": 5}},
    )
    page2 = _wrap_state([_search_item(100)])  # не должна запрашиваться
    client = _FakeClient([page1, page2])
    res = await col.collect_search(
        client,
        tmp_db,
        {"text": "py"},
        max_pages=5,
        search_id=sid,
        early_stop_consecutive_seen=3,
    )
    assert res["partial"] is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_collect_search_no_early_stop_when_disabled(tmp_db):
    """early_stop_consecutive_seen=0 → ничего не пропускаем, идём до конца."""
    from app.db import searches_repo

    sid = await searches_repo.create_search(tmp_db, "py", {"text": "py"})
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (10, 'a'), (11, 'b'), (12, 'c')")
    await tmp_db.commit()
    await searches_repo.mark_seen(tmp_db, sid, [10, 11, 12])

    page1 = _wrap_state(
        [_search_item(10), _search_item(11), _search_item(12)],
        paging={"next": {"disabled": True}},
    )
    client = _FakeClient([page1])
    res = await col.collect_search(
        client,
        tmp_db,
        {"text": "py"},
        max_pages=3,
        search_id=sid,
        early_stop_consecutive_seen=0,
    )
    assert res["partial"] is False  # никаких пропусков
    assert res["saved"] == 3


# ----- _detect_archived ----


def test_detect_archived_via_at_archived_bool():
    assert col._detect_archived({"@archived": True}, None) is True


def test_detect_archived_via_string():
    assert col._detect_archived({"archived": "true"}, None) is True


def test_detect_archived_via_status():
    assert col._detect_archived({"status": "archived"}, None) is True


def test_detect_archived_via_html_marker():
    assert col._detect_archived(None, "<div>Вакансия в архиве</div>") is True


def test_detect_archived_false():
    assert col._detect_archived({"archived": False}, "<div>active</div>") is False


# ----- collect_one_vacancy ----


@pytest.mark.asyncio
async def test_collect_one_vacancy_ok(tmp_db):
    page = _wrap_view_state(55)
    client = _FakeClient([page])
    ok = await col.collect_one_vacancy(client, tmp_db, 55)
    assert ok is True
    cur = await tmp_db.execute("SELECT id, name FROM vacancies WHERE id=55")
    row = await cur.fetchone()
    assert row[0] == 55


@pytest.mark.asyncio
async def test_collect_one_vacancy_archived_marked(tmp_db):
    page = _wrap_view_state(60, archived=True)
    client = _FakeClient([page])
    await col.collect_one_vacancy(client, tmp_db, 60)
    cur = await tmp_db.execute("SELECT archived_at FROM vacancies WHERE id=60")
    row = await cur.fetchone()
    assert row[0] is not None


@pytest.mark.asyncio
async def test_collect_one_vacancy_unavailable_marks_disappeared(tmp_db):
    _, _, Unav = _exc_classes()
    client = _FakeClient([""], raise_on={0: Unav("hidden")})
    ok = await col.collect_one_vacancy(client, tmp_db, 77)
    assert ok is False
    cur = await tmp_db.execute("SELECT disappeared_at FROM vacancies WHERE id=77")
    row = await cur.fetchone()
    assert row[0] is not None


@pytest.mark.asyncio
async def test_collect_one_vacancy_antibot_solo_marks_disappeared(tmp_db):
    Anti, _, _ = _exc_classes()
    # клиент НЕ в паузе → одиночный 403 → метим disappeared, возвращаем False
    client = _FakeClient([""], raise_on={0: Anti("solo 403")})
    client.paused_now = False
    ok = await col.collect_one_vacancy(client, tmp_db, 78)
    assert ok is False
    cur = await tmp_db.execute("SELECT disappeared_at FROM vacancies WHERE id=78")
    assert (await cur.fetchone())[0] is not None


@pytest.mark.asyncio
async def test_collect_one_vacancy_antibot_paused_reraises(tmp_db):
    Anti, _, _ = _exc_classes()
    client = _FakeClient([""], raise_on={0: Anti("real antibot")})
    client.paused_now = True
    with pytest.raises(Anti):
        await col.collect_one_vacancy(client, tmp_db, 79)


@pytest.mark.asyncio
async def test_collect_one_vacancy_session_expired_reraises(tmp_db):
    _, Sess, _ = _exc_classes()
    client = _FakeClient([""], raise_on={0: Sess("login")})
    with pytest.raises(Sess):
        await col.collect_one_vacancy(client, tmp_db, 80)


@pytest.mark.asyncio
async def test_collect_one_vacancy_no_state_returns_false(tmp_db):
    client = _FakeClient(["<html>no state</html>"])
    ok = await col.collect_one_vacancy(client, tmp_db, 81)
    assert ok is False


@pytest.mark.asyncio
async def test_collect_one_vacancy_generic_error_returns_false(tmp_db):
    client = _FakeClient([""], raise_on={0: ValueError("boom")})
    ok = await col.collect_one_vacancy(client, tmp_db, 82)
    assert ok is False


# ----- _resolve_query_for_vacancy ----


@pytest.mark.asyncio
async def test_resolve_query_from_collected_via(tmp_db):
    await tmp_db.execute(
        "INSERT INTO vacancy_collected_via(vacancy_id, query_text, area, schedule) VALUES (?, ?, '', '')",
        (100, "python dev"),
    )
    await tmp_db.commit()
    q = await col._resolve_query_for_vacancy(tmp_db, 100)
    assert q == "python dev"


@pytest.mark.asyncio
async def test_resolve_query_fallback_to_active_search(tmp_db):
    from app.db import searches_repo

    await searches_repo.create_search(tmp_db, name="active", params={"text": "go dev"})
    q = await col._resolve_query_for_vacancy(tmp_db, 9999)
    assert q == "go dev"


@pytest.mark.asyncio
async def test_resolve_query_returns_none(tmp_db):
    q = await col._resolve_query_for_vacancy(tmp_db, 12345)
    assert q is None


# ----- backfill_from_negotiations ----


async def _seed_negotiation(db, vid: int) -> None:
    from app.db import negotiations_repo

    item = {
        "id": vid * 10 + 1,
        "vacancyId": vid,
        "employerId": 1,
        "lastState": "RESPONSE",
        "lastModified": "2024-01-01T00:00:00",
    }
    n = negotiations_repo.from_topic_item(item)
    await negotiations_repo.upsert_and_snapshot(db, n)
    await db.commit()


@pytest.mark.asyncio
async def test_backfill_zero_pending(tmp_db):
    client = _FakeClient([])
    res = await col.backfill_from_negotiations(client, tmp_db)
    assert res["requested"] == 0
    assert res["saved"] == 0


@pytest.mark.asyncio
async def test_backfill_saves(tmp_db):
    await _seed_negotiation(tmp_db, 200)
    await _seed_negotiation(tmp_db, 201)
    pages = [_wrap_view_state(200), _wrap_view_state(201)]
    client = _FakeClient(pages)
    res = await col.backfill_from_negotiations(client, tmp_db, limit=10)
    assert res["requested"] == 2
    assert res["saved"] == 2
    assert res["paused"] is False


@pytest.mark.asyncio
async def test_backfill_antibot_paused_breaks(tmp_db):
    Anti, _, _ = _exc_classes()
    await _seed_negotiation(tmp_db, 300)
    await _seed_negotiation(tmp_db, 301)
    # первый успешный, второй — реальная пауза
    client = _FakeClient([_wrap_view_state(300), ""], raise_on={1: Anti("pause")})
    client.paused_now = True
    res = await col.backfill_from_negotiations(client, tmp_db, limit=10)
    assert res["paused"] is True
    assert res["saved"] == 1
    assert res["remaining"] >= 1
    assert res["hint"] is not None


@pytest.mark.asyncio
async def test_backfill_antibot_solo_continues_and_marks_disappeared(tmp_db):
    Anti, _, _ = _exc_classes()
    await _seed_negotiation(tmp_db, 400)
    await _seed_negotiation(tmp_db, 401)
    # 1-й вернёт хорошее HTML, 2-й — solo 403 (клиент не на паузе)
    client = _FakeClient([_wrap_view_state(400), ""], raise_on={1: Anti("solo")})
    client.paused_now = False  # коллектор смотрит на client.status в момент обработки
    res = await col.backfill_from_negotiations(client, tmp_db, limit=10)
    assert res["paused"] is False
    # одна сохранилась, одна не сохранена и помечена disappeared
    cur = await tmp_db.execute("SELECT id, disappeared_at FROM vacancies ORDER BY id")
    rows = list(await cur.fetchall())
    # должна быть запись и для 401 (placeholder с disappeared_at)
    found = {r[0]: r[1] for r in rows}
    assert 401 in found


@pytest.mark.asyncio
async def test_backfill_session_expired_breaks(tmp_db):
    _, Sess, _ = _exc_classes()
    await _seed_negotiation(tmp_db, 500)
    client = _FakeClient([""], raise_on={0: Sess("login")})
    res = await col.backfill_from_negotiations(client, tmp_db, limit=10)
    assert res["paused"] is True
    assert res["pause_reason"].startswith("session expired")


@pytest.mark.asyncio
async def test_backfill_progress_callback(tmp_db):
    await _seed_negotiation(tmp_db, 600)
    pages = [_wrap_view_state(600)]
    client = _FakeClient(pages)
    calls = []

    def cb(**kw):
        calls.append(kw)

    await col.backfill_from_negotiations(client, tmp_db, limit=5, progress_cb=cb)
    assert calls
    assert any("saved" in (c.get("message") or "") or "готово" in (c.get("message") or "") for c in calls)


# ----- backfill_descriptions ----


@pytest.mark.asyncio
async def test_backfill_descriptions_targets_short_desc_only(tmp_db):
    # 700 — без описания (попадёт), 701 — с длинным (не попадёт)
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (700, 'v700', '')")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (701, 'v701', ?)", ("x" * 200,))
    await tmp_db.commit()
    client = _FakeClient([_wrap_view_state(700)])
    res = await col.backfill_descriptions(client, tmp_db, limit=10)
    assert res["requested"] == 1  # только 700
    assert res["saved"] == 1
    assert res["paused"] is False


@pytest.mark.asyncio
async def test_backfill_descriptions_antibot_pause_breaks(tmp_db):
    Anti, _, _ = _exc_classes()
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (710, 'v710', '')")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (711, 'v711', '')")
    await tmp_db.commit()
    client = _FakeClient([_wrap_view_state(710), ""], raise_on={1: Anti("pause")})
    client.paused_now = True
    res = await col.backfill_descriptions(client, tmp_db, limit=10)
    assert res["paused"] is True
    assert res["saved"] == 1
    assert res["remaining"] >= 1


# ----- _mark_vacancy_unavailable ----


@pytest.mark.asyncio
async def test_mark_vacancy_unavailable_new_placeholder(tmp_db):
    await col._mark_vacancy_unavailable(tmp_db, 9001, "тест")
    cur = await tmp_db.execute("SELECT name, disappeared_at, url FROM vacancies WHERE id=9001")
    row = await cur.fetchone()
    assert row is not None
    assert "недоступно" in row[0]
    assert row[1] is not None


@pytest.mark.asyncio
async def test_mark_vacancy_unavailable_existing_only_sets_timestamp(tmp_db):
    # сначала кладём обычную вакансию
    from app.db import vacancies_repo
    from tests.integration.test_dedup import _v

    await vacancies_repo.upsert(tmp_db, _v(9002, "Real"))
    await tmp_db.commit()
    await col._mark_vacancy_unavailable(tmp_db, 9002, "ушла")
    cur = await tmp_db.execute("SELECT name, disappeared_at FROM vacancies WHERE id=9002")
    row = await cur.fetchone()
    # имя не перетёрто
    assert row[0] == "Real"
    assert row[1] is not None
