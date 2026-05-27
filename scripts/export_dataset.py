"""Экспорт датасета для обучения предиктора приглашения.

Запуск: uv run python -m scripts.export_dataset

target = 1 если last_state in (INVITATION, INTERVIEW), 0 если DISCARD*.
RESPONSE без ответа в датасет не идёт.
"""

import asyncio
import csv
import json
from pathlib import Path

from app.db import employers_repo, vacancies_repo
from app.db.db import get_db


async def main():
    db = await get_db()
    try:
        emp_map = await employers_repo.get_map(db)
        cur = await db.execute(
            """
            SELECT n.id, n.vacancy_id, n.employer_id, n.last_state, n.viewed_by_opponent,
                   n.creation_time, n.last_modified, n.applicant_sub_state, n.employer_sub_state,
                   n.conversation_messages, n.has_response_letter
              FROM negotiations n
             WHERE n.last_state IN ('INVITATION','INTERVIEW')
                OR n.last_state LIKE 'DISCARD%'
            """
        )
        neg_rows = await cur.fetchall()
        out = []
        for r in neg_rows:
            emp = emp_map.get(r["employer_id"]) if r["employer_id"] else None
            v = await vacancies_repo.get_vacancy(db, r["vacancy_id"]) if r["vacancy_id"] else None
            target = 1 if r["last_state"] in ("INVITATION", "INTERVIEW") else 0
            row = {
                "negotiation_id": r["id"],
                "vacancy_id": r["vacancy_id"],
                "employer_id": r["employer_id"],
                "last_state": r["last_state"],
                "target_invite": target,
                "viewed_by_opponent": int(r["viewed_by_opponent"] or 0),
                "conversation_messages": r["conversation_messages"] or 0,
                "has_response_letter": int(r["has_response_letter"] or 0),
                "applicant_sub_state": r["applicant_sub_state"],
                "employer_sub_state": r["employer_sub_state"],
                "creation_time": r["creation_time"],
                "emp_read_pct": (emp or {}).get("read_topic_percent"),
                "emp_reply_days": (emp or {}).get("reply_working_days"),
                "emp_all_topics": (emp or {}).get("all_topic_count"),
                "vacancy_in_db": int(v is not None),
                "salary_rub": v.get("salary_rub") if v else None,
                "is_remote": int(bool(v.get("is_remote") or v.get("is_remote_text"))) if v else None,
                "level": v.get("level") if v else None,
                "stack_count": len(v.get("parsed_stack") or []) if v else None,
                "total_responses": v.get("total_responses_count") if v else None,
            }
            out.append(row)
    finally:
        await db.close()

    if not out:
        print("Нет негоций с явным исходом")
        return

    Path("data").mkdir(exist_ok=True)
    fieldnames = sorted({k for r in out for k in r})
    with open("data/dataset.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)
    with open("data/dataset.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    positives = sum(1 for r in out if r["target_invite"] == 1)
    print(f"Сохранено {len(out)} строк, positives={positives}, файл: data/dataset.csv")
    print("Пример первой строки:")
    print(json.dumps(out[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
