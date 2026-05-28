"""e2e тесты RAG: страница /search, /api/rag/search, ветка rag_disabled."""

from __future__ import annotations

import aiosqlite
import pytest


def _db_path() -> str:
    from app.config import settings

    return settings.DB_PATH


@pytest.mark.asyncio
async def test_search_page_renders(app_client):
    client, _ = app_client
    r = await client.get("/search")
    assert r.status_code == 200
    assert "Семантический поиск" in r.text


@pytest.mark.asyncio
async def test_rag_search_disabled(app_client, monkeypatch):
    client, _ = app_client
    from app.llm import rag

    monkeypatch.setattr(rag, "is_available", lambda: False)
    r = await client.post("/api/rag/search", data={"q": "python"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "rag_disabled"}


@pytest.mark.asyncio
async def test_rag_search_returns_results(app_client, monkeypatch):
    pytest.importorskip("sqlite_vec")
    client, _ = app_client
    from app.config import settings
    from app.db import embeddings_repo
    from app.llm import client as llm_client
    from app.llm import rag

    monkeypatch.setattr(rag, "is_available", lambda: True)

    def _vec(*head):
        v = list(head) + [0.0] * (settings.EMBED_DIM - len(head))
        return v[: settings.EMBED_DIM]

    async with aiosqlite.connect(_db_path()) as db:
        for vid in (1, 2):
            await db.execute(
                "INSERT INTO vacancies(id, name, company_name, description) VALUES (?, ?, ?, ?)",
                (vid, f"Py {vid}", "Acme", "d" * 200),
            )
        await db.commit()
        await embeddings_repo.ensure_ready(db)
        await embeddings_repo.upsert(db, 1, "m", _vec(1.0, 0.0), "h1")
        await embeddings_repo.upsert(db, 2, "m", _vec(0.0, 1.0), "h2")

    async def fake_embed(texts, *, model, base_url=None, timeout=None):
        return llm_client.EmbedResponse(
            ok=True, vectors=[_vec(1.0, 0.0)], error=None, model=model, latency_ms=1
        )

    monkeypatch.setattr(llm_client, "embed", fake_embed)

    r = await client.post("/api/rag/search", data={"q": "python"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["results"]
    assert body["results"][0]["id"] == 1  # ближайший к вектору запроса
