"""Тесты на refresh_resume_search_token и sync_resume_token_into_searches."""

from __future__ import annotations

import pytest

from app.collector import personal as personal_col
from app.db import searches_repo


class _FakeClient:
    def __init__(self, html: str):
        self.html = html
        self.calls: list[tuple[str, dict | None]] = []

    @property
    def status(self):
        return {"paused_now": False, "paused_until": 0}

    async def get_page(self, path: str, params=None) -> str:
        self.calls.append((path, params))
        return self.html


_HTML_WITH_TOKEN = """
<html><body>
  <a href="https://krasnodar.hh.ru/search/vacancy?resume=7fbb135bff010af1f00039ed1f687571546863&from=resumelist&hhtmFrom=resume_list" class="btn">1443 вакансии</a>
</body></html>
"""

_HTML_WITHOUT_TOKEN = "<html><body>Нет резюме</body></html>"


@pytest.mark.asyncio
async def test_refresh_token_extracts_hash(tmp_db):
    client = _FakeClient(_HTML_WITH_TOKEN)
    token = await personal_col.refresh_resume_search_token(client, tmp_db)
    assert token == "7fbb135bff010af1f00039ed1f687571546863"
    cur = await tmp_db.execute("SELECT resume_id FROM profile WHERE id=1")
    row = await cur.fetchone()
    assert row["resume_id"] == token


@pytest.mark.asyncio
async def test_refresh_token_none_when_no_match(tmp_db):
    client = _FakeClient(_HTML_WITHOUT_TOKEN)
    token = await personal_col.refresh_resume_search_token(client, tmp_db)
    assert token is None


@pytest.mark.asyncio
async def test_sync_resume_token_updates_searches(tmp_db):
    # 2 поиска: один с rec-токеном, второй обычный
    sid_rec = await searches_repo.create_search(
        tmp_db, "✨ Рекомендации", {"resume": "OLD_TOKEN", "max_pages": 10}
    )
    sid_plain = await searches_repo.create_search(tmp_db, "python", {"text": "python", "max_pages": 5})

    client = _FakeClient(_HTML_WITH_TOKEN)
    res = await personal_col.sync_resume_token_into_searches(client, tmp_db)
    assert res["refreshed"] is True
    assert res["token"] == "7fbb135bff010af1f00039ed1f687571546863"
    assert res["searches_updated"] == 1

    # rec-поиск получил новый токен
    s = await searches_repo.get(tmp_db, sid_rec)
    assert s["params"]["resume"] == "7fbb135bff010af1f00039ed1f687571546863"
    # обычный — не тронут
    s = await searches_repo.get(tmp_db, sid_plain)
    assert "resume" not in s["params"]


@pytest.mark.asyncio
async def test_sync_resume_token_no_searches(tmp_db):
    """Нет поисков с resume — не дёргаем HH."""
    client = _FakeClient(_HTML_WITH_TOKEN)
    res = await personal_col.sync_resume_token_into_searches(client, tmp_db)
    assert res["refreshed"] is False
    assert res["reason"] == "no_recommendation_searches"
    assert client.calls == []  # HH не дёргали


@pytest.mark.asyncio
async def test_sync_resume_token_idempotent(tmp_db):
    """Если токен не менялся — поиск не апдейтим."""
    await searches_repo.create_search(
        tmp_db,
        "✨ Рекомендации",
        {"resume": "7fbb135bff010af1f00039ed1f687571546863", "max_pages": 10},
    )
    client = _FakeClient(_HTML_WITH_TOKEN)
    res = await personal_col.sync_resume_token_into_searches(client, tmp_db)
    assert res["refreshed"] is True
    assert res["searches_updated"] == 0  # ничего не поменялось
