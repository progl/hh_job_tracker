"""Миграция RAG-индекса под новую размерность эмбеддингов.

Когда меняется LLM_MODEL_EMBED → меняется EMBED_DIM (например nomic-embed-text 768d → bge-m3 1024d),
старая vec0-таблица несовместима. Скрипт: дропает vec_vacancies, чистит vacancy_embeddings —
после следующего прогона джобы _job_embed_vacancies весь корпус переэмбеддится новой моделью.

Использование:
    uv run python -m scripts.migrate_embed_dim --confirm

Без --confirm показывает план и выходит.
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import settings
from app.db.db import get_db


async def main(confirm: bool) -> None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM vacancy_embeddings")
        meta_count = (await cur.fetchone())[0]

        try:
            cur = await db.execute("SELECT COUNT(*) FROM vec_vacancies")
            vec_count = (await cur.fetchone())[0]
        except Exception:
            vec_count = "(таблицы нет)"

        print("=" * 60)
        print("Текущее состояние RAG-индекса:")
        print(f"  vacancy_embeddings (мета): {meta_count} записей")
        print(f"  vec_vacancies      (vec0): {vec_count} векторов")
        print()
        print(f"Новая модель: {settings.LLM_MODEL_EMBED}  dim={settings.EMBED_DIM}")
        print()
        print("План миграции:")
        print("  1. DROP TABLE IF EXISTS vec_vacancies")
        print("  2. DELETE FROM vacancy_embeddings")
        print("  → vec0 пересоздастся с новой размерностью при ensure_ready()")
        print("  → джоба _job_embed_vacancies переэмбеддит весь корпус")
        print("=" * 60)

        if not confirm:
            print("\nПередай --confirm чтобы выполнить.")
            return

        print("\nВыполняю…")
        await db.execute("DROP TABLE IF EXISTS vec_vacancies")
        await db.execute("DELETE FROM vacancy_embeddings")
        await db.commit()
        print("Готово. Запусти джобу embed_vacancies (или подожди следующего тика scheduler).")
    finally:
        await db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--confirm", action="store_true", help="реально выполнить, без флага — dry-run")
    args = p.parse_args()
    asyncio.run(main(args.confirm))
