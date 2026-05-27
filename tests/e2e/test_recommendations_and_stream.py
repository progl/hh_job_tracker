"""e2e тесты для POST /api/searches/recommendations и GET /api/status/stream."""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest


async def _insert_profile(db_path, resume_id: str | None) -> None:
    async with aiosqlite.connect(db_path) as db:
        if resume_id is None:
            await db.execute("INSERT OR REPLACE INTO profile(id) VALUES (1)")
        else:
            await db.execute(
                "INSERT OR REPLACE INTO profile(id, resume_id) VALUES (1, ?)",
                (resume_id,),
            )
        await db.commit()


def _db_path(webapp) -> str:
    from app.config import settings

    return settings.DB_PATH


# ----- POST /api/searches/recommendations -----


@pytest.mark.asyncio
async def test_recommendations_no_resume_id(app_client):
    """Без resume_id в профиле — 400 с понятным reason."""
    client, webapp = app_client
    await _insert_profile(_db_path(webapp), None)
    r = await client.post("/api/searches/recommendations")
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "no_resume_id"


@pytest.mark.asyncio
async def test_recommendations_create_new(app_client):
    """Свежий профиль с resume_id — создаём saved_search."""
    client, webapp = app_client
    await _insert_profile(_db_path(webapp), "7fbb135bff010af1f00039ed1f687571546863")
    r = await client.post("/api/searches/recommendations")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["existed"] is False
    sid = body["id"]
    # проверяем что в БД появился поиск с этим resume в params
    r = await client.get("/api/searches")
    rows = r.json()["searches"]
    s = next(s for s in rows if s["id"] == sid)
    assert s["name"] == "✨ Рекомендации"
    assert s["params"]["resume"] == "7fbb135bff010af1f00039ed1f687571546863"
    assert s["params"]["max_pages"] == 200  # «до конца» с early-stop K=5


@pytest.mark.asyncio
async def test_recommendations_idempotent(app_client):
    """Повторный вызов возвращает существующий id, не создаёт дубль."""
    client, webapp = app_client
    await _insert_profile(_db_path(webapp), "abc999")
    r1 = await client.post("/api/searches/recommendations")
    assert r1.status_code == 200
    sid1 = r1.json()["id"]
    assert r1.json()["existed"] is False

    r2 = await client.post("/api/searches/recommendations")
    assert r2.status_code == 200
    body = r2.json()
    assert body["existed"] is True
    assert body["id"] == sid1

    # в списке всего один поиск с resume
    r = await client.get("/api/searches")
    rows = [s for s in r.json()["searches"] if (s.get("params") or {}).get("resume") == "abc999"]
    assert len(rows) == 1


# ----- GET /api/status/stream -----


@pytest.mark.asyncio
async def test_bulk_max_pages_updates_only_active(app_client):
    """Bulk-апдейт меняет max_pages у активных, неактивные пропускает."""
    client, _ = app_client
    r1 = await client.post("/api/searches", data={"name": "S1", "text": "py", "max_pages": "5"})
    sid1 = r1.json()["id"]
    r2 = await client.post("/api/searches", data={"name": "S2", "text": "go", "max_pages": "5"})
    sid2 = r2.json()["id"]
    # S2 — выключаем
    await client.post(f"/api/searches/{sid2}/toggle")

    r = await client.post("/api/searches/bulk-max-pages", data={"max_pages": "200"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] == 1  # только S1 (S2 неактивен)

    # проверим что у S1 теперь 200, у S2 — 5
    r = await client.get("/api/searches")
    rows = r.json()["searches"]
    s1 = next(s for s in rows if s["id"] == sid1)
    s2 = next(s for s in rows if s["id"] == sid2)
    assert s1["params"]["max_pages"] == 200
    assert s2["params"]["max_pages"] == 5


@pytest.mark.asyncio
async def test_bulk_max_pages_only_with_resume(app_client):
    """only_with_resume=True → только рекомендации (где есть params.resume)."""
    client, webapp = app_client
    # обычный поиск
    await client.post("/api/searches", data={"name": "Plain", "text": "py", "max_pages": "5"})
    # рекомендации
    await _insert_profile(_db_path(webapp), "abc999")
    await client.post("/api/searches/recommendations")  # создаст с max_pages=200

    # сначала bulk до 50 только для рекомендаций
    r = await client.post(
        "/api/searches/bulk-max-pages",
        data={"max_pages": "50", "only_with_resume": "true"},
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 1  # только рекомендация изменилась

    rows = (await client.get("/api/searches")).json()["searches"]
    plain = next(s for s in rows if s["name"] == "Plain")
    rec = next(s for s in rows if (s.get("params") or {}).get("resume") == "abc999")
    assert plain["params"]["max_pages"] == 5
    assert rec["params"]["max_pages"] == 50


@pytest.mark.asyncio
async def test_bulk_max_pages_out_of_range(app_client):
    client, _ = app_client
    r = await client.post("/api/searches/bulk-max-pages", data={"max_pages": "0"})
    assert r.status_code == 400
    r = await client.post("/api/searches/bulk-max-pages", data={"max_pages": "9999"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bulk_early_stop_updates(app_client):
    """Bulk-апдейт early_stop_seen меняет params у всех активных."""
    client, _ = app_client
    r1 = await client.post("/api/searches", data={"name": "A", "text": "py"})
    sid1 = r1.json()["id"]
    r2 = await client.post("/api/searches", data={"name": "B", "text": "go"})
    sid2 = r2.json()["id"]

    r = await client.post("/api/searches/bulk-early-stop", data={"early_stop_seen": "10"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] == 2

    rows = (await client.get("/api/searches")).json()["searches"]
    s1 = next(s for s in rows if s["id"] == sid1)
    s2 = next(s for s in rows if s["id"] == sid2)
    assert s1["params"]["early_stop_seen"] == 10
    assert s2["params"]["early_stop_seen"] == 10


@pytest.mark.asyncio
async def test_bulk_early_stop_zero_disables(app_client):
    """early_stop_seen=0 — валидное значение (выключает early-stop)."""
    client, _ = app_client
    await client.post("/api/searches", data={"name": "X", "text": "py"})
    r = await client.post("/api/searches/bulk-early-stop", data={"early_stop_seen": "0"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    rows = (await client.get("/api/searches")).json()["searches"]
    assert rows[0]["params"]["early_stop_seen"] == 0


@pytest.mark.asyncio
async def test_bulk_early_stop_only_with_resume(app_client):
    """only_with_resume=true — только рекомендации."""
    client, webapp = app_client
    await client.post("/api/searches", data={"name": "Plain", "text": "py"})
    await _insert_profile(_db_path(webapp), "tok123")
    await client.post("/api/searches/recommendations")

    r = await client.post(
        "/api/searches/bulk-early-stop",
        data={"early_stop_seen": "7", "only_with_resume": "true"},
    )
    assert r.status_code == 200
    # рекомендация уже имеет early_stop_seen=5 → её апдейтнули. Plain не трогали.
    assert r.json()["updated"] == 1

    rows = (await client.get("/api/searches")).json()["searches"]
    plain = next(s for s in rows if s["name"] == "Plain")
    rec = next(s for s in rows if (s.get("params") or {}).get("resume") == "tok123")
    assert "early_stop_seen" not in plain["params"]
    assert rec["params"]["early_stop_seen"] == 7


@pytest.mark.asyncio
async def test_bulk_early_stop_out_of_range(app_client):
    client, _ = app_client
    r = await client.post("/api/searches/bulk-early-stop", data={"early_stop_seen": "-1"})
    assert r.status_code == 400
    r = await client.post("/api/searches/bulk-early-stop", data={"early_stop_seen": "101"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_status_stream_emits_first_event(app_client):
    """Прямой вызов генератора SSE — httpx/ASGITransport буферизует event-stream
    и зависает на aiter_text, поэтому тестируем эндпоинт через body_iterator."""
    _, webapp = app_client
    resp = await webapp.status_stream()
    assert resp.media_type == "text/event-stream"
    chunk = await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=3.0)
    text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
    assert text.startswith("data: ")
    payload = json.loads(text[len("data: ") :].split("\n\n", 1)[0])
    assert "client" in payload
    assert "scheduler" in payload
    # фикстура мокает status — проверяем что эти поля доехали
    assert payload["client"]["base_url"] == "https://hh.ru"
    assert payload["scheduler"]["running"] is False
    # закрываем генератор — иначе он зависнет в asyncio.sleep(10)
    await resp.body_iterator.aclose()
