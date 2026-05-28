"""Тесты vec0-репозитория эмбеддингов. Требуют extra `rag` (sqlite-vec) — иначе скип."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlite_vec")

from app.config import settings
from app.db import embeddings_repo


def _vec(*head: float) -> list[float]:
    """Вектор длины EMBED_DIM с заданным началом, остальное — нули."""
    v = list(head) + [0.0] * (settings.EMBED_DIM - len(head))
    return v[: settings.EMBED_DIM]


@pytest.mark.asyncio
async def test_upsert_knn_coverage_missing(tmp_db):
    for vid in (1, 2, 3):
        await tmp_db.execute(
            "INSERT INTO vacancies(id, name, description) VALUES (?, ?, ?)", (vid, f"v{vid}", "d" * 200)
        )
    await tmp_db.commit()

    await embeddings_repo.ensure_ready(tmp_db)
    await embeddings_repo.upsert(tmp_db, 1, "m", _vec(1.0, 0.0), "h1")
    await embeddings_repo.upsert(tmp_db, 2, "m", _vec(0.9, 0.1), "h2")

    embedded, total = await embeddings_repo.coverage(tmp_db)
    assert embedded == 2
    assert total == 3
    assert await embeddings_repo.missing_vacancy_ids(tmp_db, 10) == [3]

    blob = await embeddings_repo.get_vector_blob(tmp_db, 1)
    assert blob is not None
    rows = await embeddings_repo.knn(tmp_db, blob, 5)
    # ближайшая к вакансии 1 — она сама (distance≈0), затем 2
    assert rows[0][0] == 1
    assert {vid for vid, _ in rows} == {1, 2}


@pytest.mark.asyncio
async def test_upsert_overwrites(tmp_db):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (1, 'v', ?)", ("d" * 200,))
    await tmp_db.commit()
    await embeddings_repo.ensure_ready(tmp_db)
    await embeddings_repo.upsert(tmp_db, 1, "m", _vec(1.0), "h1")
    await embeddings_repo.upsert(tmp_db, 1, "m", _vec(0.0, 1.0), "h2")  # перезапись
    embedded, _ = await embeddings_repo.coverage(tmp_db)
    assert embedded == 1
    cur = await tmp_db.execute("SELECT source_hash FROM vacancy_embeddings WHERE vacancy_id = 1")
    assert (await cur.fetchone())[0] == "h2"
