"""Тесты RAG-логики (app/llm/rag.py). Требуют extra `rag` (sqlite-vec) — иначе скип.

Импорты app.llm.client/rag делаем ВНУТРИ тестов: e2e-conftest удаляет и переимпортирует
app.* модули, поэтому top-level-ссылка устаревает и monkeypatch не попадает в нужный объект.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlite_vec")

from app.config import settings


def _vec(*head: float) -> list[float]:
    v = list(head) + [0.0] * (settings.EMBED_DIM - len(head))
    return v[: settings.EMBED_DIM]


def test_is_available_true_with_extra():
    from app.llm import rag

    rag._available = None  # сброс кеша
    assert rag.is_available() is True


def test_build_embed_text_and_hash():
    from app.llm import rag

    v = {
        "name": "Senior Python",
        "company_name": "Acme",
        "parsed_stack": '["python","fastapi"]',
        "description": "Описание " * 20,
    }
    text = rag.build_embed_text(v)
    assert "Senior Python" in text and "Acme" in text and "python" in text
    h1 = rag.source_hash(text)
    h2 = rag.source_hash(text)
    assert h1 == h2 and len(h1) == 40  # sha1 hex


def test_score_clamps():
    from app.llm import rag

    assert rag._score(0.0) == 1.0
    assert rag._score(2.0) == 0.0
    assert 0.0 <= rag._score(0.3) <= 1.0


@pytest.mark.asyncio
async def test_embed_vacancy_stores_vector(tmp_db, monkeypatch):
    from app.db import embeddings_repo
    from app.llm import client as llm_client
    from app.llm import rag

    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (1, 'Py', ?)", ("d" * 200,))
    await tmp_db.commit()

    async def fake_embed(texts, *, model, base_url=None, timeout=None):
        return llm_client.EmbedResponse(
            ok=True, vectors=[_vec(1.0, 0.5)], error=None, model=model, latency_ms=3
        )

    monkeypatch.setattr(llm_client, "embed", fake_embed)

    res = await rag.embed_vacancy(tmp_db, 1)
    assert res["ok"] is True
    assert res["dim"] == settings.EMBED_DIM
    embedded, _ = await embeddings_repo.coverage(tmp_db)
    assert embedded == 1


@pytest.mark.asyncio
async def test_semantic_search_and_ask(tmp_db, monkeypatch):
    from app.db import embeddings_repo
    from app.llm import client as llm_client
    from app.llm import rag

    for vid in (1, 2):
        await tmp_db.execute(
            "INSERT INTO vacancies(id, name, company_name, description) VALUES (?, ?, ?, ?)",
            (vid, f"Py {vid}", "Acme", "d" * 200),
        )
    await tmp_db.commit()
    await embeddings_repo.ensure_ready(tmp_db)
    await embeddings_repo.upsert(tmp_db, 1, "m", _vec(1.0, 0.0), "h1")
    await embeddings_repo.upsert(tmp_db, 2, "m", _vec(0.0, 1.0), "h2")

    async def fake_embed(texts, *, model, base_url=None, timeout=None):
        return llm_client.EmbedResponse(
            ok=True, vectors=[_vec(1.0, 0.0)], error=None, model=model, latency_ms=1
        )

    async def fake_generate(*, model, prompt, system=None, format_json=True, **kw):
        return llm_client.LLMResponse(
            ok=True,
            text="Подходит вакансия [#1]",
            parsed=None,
            error=None,
            model=model,
            latency_ms=2,
            prompt_tokens=10,
            response_tokens=5,
        )

    monkeypatch.setattr(llm_client, "embed", fake_embed)
    monkeypatch.setattr(llm_client, "generate", fake_generate)

    hits = await rag.semantic_search(tmp_db, "python fastapi", k=5)
    assert hits and hits[0][0] == 1  # вектор запроса ближе к вакансии 1

    res = await rag.ask(tmp_db, "какая вакансия про python?", k=5)
    assert res["ok"] is True
    assert "[#1]" in res["answer"]
    assert any(s["vacancy_id"] == 1 for s in res["sources"])


@pytest.mark.asyncio
async def test_ask_no_results(tmp_db, monkeypatch):
    from app.db import embeddings_repo
    from app.llm import client as llm_client
    from app.llm import rag

    async def fake_embed(texts, *, model, base_url=None, timeout=None):
        return llm_client.EmbedResponse(ok=True, vectors=[_vec(1.0)], error=None, model=model, latency_ms=1)

    monkeypatch.setattr(llm_client, "embed", fake_embed)
    await embeddings_repo.ensure_ready(tmp_db)  # пустой корпус
    res = await rag.ask(tmp_db, "что угодно", k=5)
    assert res["ok"] is False
    assert res["reason"] == "no_results"
