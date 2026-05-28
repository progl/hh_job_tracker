"""e2e: страница /searches и inline-обновление сохранённого поиска (merge params)."""

from __future__ import annotations

import json

import aiosqlite
import pytest


def _db_path() -> str:
    from app.config import settings

    return settings.DB_PATH


@pytest.mark.asyncio
async def test_searches_page_renders(app_client):
    client, _ = app_client
    r = await client.get("/searches")
    assert r.status_code == 200
    assert "Сохранённые поиски" in r.text


@pytest.mark.asyncio
async def test_searches_update_merges_params(app_client):
    client, _ = app_client
    # сеем поиск с «лишним» ключом resume — он не должен потеряться при update
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO searches(id, name, params, is_active) VALUES (1, 'old', ?, 1)",
            (json.dumps({"text": "python", "resume": "abc", "max_pages": 5}),),
        )
        await db.commit()

    r = await client.post(
        "/api/searches/1/update",
        data={"name": "new name", "text": "go", "max_pages": "10", "is_active": "false"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute("SELECT name, params, is_active FROM searches WHERE id = 1")
        name, params_raw, is_active = await cur.fetchone()
    params = json.loads(params_raw)
    assert name == "new name"
    assert params["text"] == "go"
    assert params["max_pages"] == 10
    assert params["resume"] == "abc"  # ключ сохранён (merge, не перезапись)
    assert is_active == 0
