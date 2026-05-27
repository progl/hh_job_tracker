"""CLI: разобрать требования вакансии через LLM (отладка/сравнение моделей).

Примеры:
    python -m scripts.llm_parse 109876543
    python -m scripts.llm_parse 109876543 --model llama3.1:8b
    python -m scripts.llm_parse 109876543 --models qwen3:14b qwen2.5:14b llama3.1:8b

Вывод: для каждой модели — латентность, токены, кол-во распарсенных требований,
короткая таблица items. Все прогоны сохраняются в llm_runs, запросы можно посмотреть
через repo (см. llm_repo.list_runs)."""

from __future__ import annotations

import argparse
import asyncio

from app.db.db import get_db, init_db
from app.llm.tasks.requirements import parse_one, parse_one_multi_model


def _fmt_items(items: list[dict], limit: int = 12) -> str:
    lines = []
    for it in items[:limit]:
        lines.append(f"  [{it['kind']:<4}|{it.get('category', 'other'):<5}] {it['text']}")
    if len(items) > limit:
        lines.append(f"  ... +{len(items) - limit}")
    return "\n".join(lines) or "  (пусто)"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vacancy_id", type=int)
    ap.add_argument("--model", default=None, help="одна модель (по умолчанию из settings)")
    ap.add_argument("--models", nargs="+", help="несколько моделей подряд для сравнения")
    args = ap.parse_args()

    await init_db()
    db = await get_db()
    try:
        if args.models:
            results = await parse_one_multi_model(db, args.vacancy_id, args.models)
        else:
            results = [await parse_one(db, args.vacancy_id, model=args.model)]
    finally:
        await db.close()

    print()
    print("=" * 78)
    print(f"VACANCY {args.vacancy_id}")
    print("=" * 78)
    for r in results:
        print()
        print(f"MODEL : {r.get('model')}")
        if not r.get("ok"):
            print(f"  ✗ FAIL reason={r.get('reason') or r.get('error')}")
            continue
        toks = f"prompt={r.get('prompt_tokens')} resp={r.get('response_tokens')}"
        print(
            f"  latency: {r['latency_ms']} ms   tokens: {toks}   "
            f"items: {len(r['items'])}   llm_run_id: {r['llm_run_id']}"
        )
        print(_fmt_items(r["items"]))


if __name__ == "__main__":
    asyncio.run(main())
