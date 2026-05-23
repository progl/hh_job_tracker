import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collector import favorites


def _state(items, has_next=False):
    page = {
        "vacancySearchResult": {
            "vacancies": items,
            "paging": {"next": {"disabled": not has_next}} if has_next else {"next": {}},
        }
    }
    body = json.dumps(page)
    return f'<template id="HH-Lux-InitialState">{body}</template>'


def _item(vid: int):
    """Item в формате, что ожидает vacancy_from_search_item."""
    return {
        "vacancyId": vid,
        "name": f"V{vid}",
        "company": {"id": 1, "name": "C"},
        "area": {"id": 1, "name": "Moscow"},
        "compensation": {},
        "links": {"desktop": f"https://hh.ru/vacancy/{vid}"},
    }


@pytest.mark.asyncio
async def test_collect_favorites_no_state(tmp_db):
    """Если страница не парсится — break сразу."""
    client = MagicMock()
    client.get_page = AsyncMock(return_value="<html></html>")
    res = await favorites.collect_favorites(client, tmp_db, max_pages=3)
    assert res == {"saved": 0, "favorite_ids": []}


@pytest.mark.asyncio
async def test_collect_favorites_empty_items(tmp_db):
    client = MagicMock()
    client.get_page = AsyncMock(return_value=_state([], has_next=False))
    res = await favorites.collect_favorites(client, tmp_db)
    assert res["saved"] == 0


@pytest.mark.asyncio
async def test_collect_favorites_saves_items_and_tags(tmp_db, monkeypatch):
    client = MagicMock()
    client.get_page = AsyncMock(side_effect=[_state([_item(100), _item(200)], has_next=False)])

    # подменим vacancy_from_search_item чтобы вернуть минимальный валидный dict
    def fake_from_search(it):
        vid = it.get("vacancyId")
        return {
            "id": vid,
            "name": f"V{vid}",
            "company_id": 1,
            "company_name": "C",
            "area_id": 1,
            "area_name": "M",
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
            "description": None,
            "raw_json": "{}",
            "url": f"https://hh.ru/vacancy/{vid}",
            "archived": False,
        }

    monkeypatch.setattr(favorites, "vacancy_from_search_item", fake_from_search)

    res = await favorites.collect_favorites(client, tmp_db)
    assert res["saved"] == 2
    assert set(res["favorite_ids"]) == {100, 200}
    # проверим что vacancy_status получил тег favorite
    cur = await tmp_db.execute("SELECT vacancy_id, tags FROM vacancy_status WHERE vacancy_id IN (100, 200)")
    rows = await cur.fetchall()
    assert len(rows) == 2
    for r in rows:
        assert "favorite" in (r["tags"] or "")


@pytest.mark.asyncio
async def test_collect_favorites_skips_item_without_id(tmp_db, monkeypatch):
    client = MagicMock()
    client.get_page = AsyncMock(return_value=_state([{"name": "no-id"}], has_next=False))
    res = await favorites.collect_favorites(client, tmp_db)
    assert res["saved"] == 0


@pytest.mark.asyncio
async def test_collect_favorites_handles_parse_exception(tmp_db, monkeypatch):
    client = MagicMock()
    client.get_page = AsyncMock(return_value=_state([_item(1)], has_next=False))

    def boom(it):
        raise ValueError("nope")

    monkeypatch.setattr(favorites, "vacancy_from_search_item", boom)
    res = await favorites.collect_favorites(client, tmp_db)
    # exception проглатывается, saved=0
    assert res["saved"] == 0
