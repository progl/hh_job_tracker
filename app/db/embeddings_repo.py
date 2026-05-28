"""Репозиторий эмбеддингов для RAG.

Вектор хранится в vec0-таблице `vec_vacancies` (sqlite-vec), мета — в `vacancy_embeddings`.
Все функции, обращающиеся к vec0, требуют загруженного расширения — вызывай `ensure_ready(conn)`
в начале (грузит sqlite-vec и создаёт таблицу лениво). coverage/missing работают и без расширения.
"""

from __future__ import annotations

import aiosqlite

from app.config import settings
from app.llm.rag import load_vec


async def ensure_ready(conn: aiosqlite.Connection) -> None:
    """Грузит sqlite-vec в соединение и создаёт vec0-таблицу (если ещё нет)."""
    await load_vec(conn)
    await conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_vacancies "
        f"USING vec0(vacancy_id INTEGER PRIMARY KEY, embedding FLOAT[{settings.EMBED_DIM}] distance_metric=cosine)"
    )


async def upsert(
    conn: aiosqlite.Connection,
    vacancy_id: int,
    model: str,
    vector: list[float],
    source_hash: str,
) -> None:
    from sqlite_vec import serialize_float32

    blob = serialize_float32(vector)
    await conn.execute("DELETE FROM vec_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    await conn.execute("INSERT INTO vec_vacancies(vacancy_id, embedding) VALUES (?, ?)", (vacancy_id, blob))
    await conn.execute(
        """INSERT INTO vacancy_embeddings(vacancy_id, model, dim, source_hash, created_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(vacancy_id) DO UPDATE SET
             model = excluded.model, dim = excluded.dim,
             source_hash = excluded.source_hash, created_at = CURRENT_TIMESTAMP""",
        (vacancy_id, model, len(vector), source_hash),
    )
    await conn.commit()


async def get_vector_blob(conn: aiosqlite.Connection, vacancy_id: int) -> bytes | None:
    """Сериализованный вектор вакансии (как лежит в vec0) — можно сразу передать в knn()."""
    cur = await conn.execute("SELECT embedding FROM vec_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    row = await cur.fetchone()
    return row[0] if row else None


async def knn(conn: aiosqlite.Connection, query_blob: bytes, k: int) -> list[tuple[int, float]]:
    """K ближайших по косинусной/L2-дистанции. Возвращает [(vacancy_id, distance)]."""
    cur = await conn.execute(
        "SELECT vacancy_id, distance FROM vec_vacancies WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (query_blob, k),
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


async def missing_vacancy_ids(conn: aiosqlite.Connection, limit: int) -> list[int]:
    """Вакансии с описанием, у которых ещё нет эмбеддинга (для джобы индексации)."""
    cur = await conn.execute(
        """SELECT v.id FROM vacancies v
           LEFT JOIN vacancy_embeddings e ON e.vacancy_id = v.id
           WHERE v.description IS NOT NULL AND length(v.description) > 100
             AND e.vacancy_id IS NULL
           ORDER BY v.id DESC
           LIMIT ?""",
        (limit,),
    )
    return [r[0] for r in await cur.fetchall()]


async def coverage(conn: aiosqlite.Connection) -> tuple[int, int]:
    """(проиндексировано, всего вакансий с описанием). Не требует расширения."""
    cur = await conn.execute("SELECT COUNT(*) FROM vacancy_embeddings")
    embedded = (await cur.fetchone())[0]
    cur = await conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE description IS NOT NULL AND length(description) > 100"
    )
    total = (await cur.fetchone())[0]
    return embedded, total
