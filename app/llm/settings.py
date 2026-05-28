"""Runtime-настройки LLM-пайплайна (хранятся в cookie_store как key/value).

cookie_store используется как универсальный key-value для мелких настроек,
которые не оправдывают отдельной таблицы. Если значения нет — fallback на settings.LLM_*.
"""

from __future__ import annotations

import aiosqlite

from app.config import settings

_KEY_REQUIREMENTS_MODEL = "llm.model.requirements"
_KEY_FAST_MODEL = "llm.model.fast"
_KEY_EMBED_MODEL = "llm.model.embed"


async def get_requirements_model(db: aiosqlite.Connection) -> str:
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (_KEY_REQUIREMENTS_MODEL,))
    row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    return settings.LLM_MODEL_REQUIREMENTS


async def set_requirements_model(db: aiosqlite.Connection, model: str) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (_KEY_REQUIREMENTS_MODEL, model),
    )
    await db.commit()


async def get_fast_model(db: aiosqlite.Connection) -> str:
    """Быстрая модель для лёгких задач (summary/salary/company_kind/soft_skills)."""
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (_KEY_FAST_MODEL,))
    row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    return settings.LLM_MODEL_FAST


async def set_fast_model(db: aiosqlite.Connection, model: str) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (_KEY_FAST_MODEL, model),
    )
    await db.commit()


async def get_embed_model(db: aiosqlite.Connection) -> str:
    """Модель эмбеддингов для RAG (по умолчанию nomic-embed-text)."""
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (_KEY_EMBED_MODEL,))
    row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    return settings.LLM_MODEL_EMBED


async def set_embed_model(db: aiosqlite.Connection, model: str) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (_KEY_EMBED_MODEL, model),
    )
    await db.commit()
