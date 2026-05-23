import json
from typing import Any

import aiosqlite


def _unwrap(field: Any) -> Any:
    """HH-style: field is list of {string|amount|title} dicts. Returns scalar or list."""
    if not isinstance(field, list) or not field:
        return None
    if len(field) == 1:
        item = field[0]
        if isinstance(item, dict):
            if "string" in item:
                return item["string"]
            if "amount" in item:
                return {"amount": item.get("amount"), "currency": item.get("currency")}
            return item
        return item
    # list of items
    return [(it.get("string") if isinstance(it, dict) else it) for it in field]


def _unwrap_list(field: Any) -> list[str]:
    if not isinstance(field, list):
        return []
    out = []
    for it in field:
        if isinstance(it, dict):
            v = it.get("string") or it.get("title") or it.get("name")
            if v:
                out.append(str(v))
        elif it:
            out.append(str(it))
    return out


async def upsert_from_state(db: aiosqlite.Connection, state: dict[str, Any]) -> None:
    account = state.get("account") or {}
    full_name = " ".join(x for x in [account.get("firstName"), account.get("lastName")] if x).strip()
    await db.execute(
        """
        INSERT INTO profile(id, hhid, full_name, updated_at)
        VALUES (1, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            hhid = excluded.hhid,
            full_name = excluded.full_name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (state.get("hhid"), full_name or None),
    )
    await db.commit()


async def set_from_resume(db: aiosqlite.Connection, resume: dict[str, Any]) -> dict[str, Any]:
    attrs = resume.get("_attributes") or {}
    resume_id = attrs.get("id") or resume.get("id")
    title = _unwrap(resume.get("title"))
    skills = _unwrap_list(resume.get("keySkills"))

    salary = _unwrap(resume.get("salary")) or {}
    if isinstance(salary, dict):
        sal_amount = salary.get("amount")
        sal_cur = salary.get("currency")
    else:
        sal_amount = sal_cur = None

    months = _unwrap(resume.get("totalExperience"))
    if isinstance(months, dict):
        months = months.get("string") or months.get("amount")
    years_exp = round(months / 12.0, 1) if isinstance(months, (int, float)) else None

    formats = _unwrap_list(resume.get("workFormats"))

    await db.execute(
        """
        INSERT INTO profile(id, resume_id, title, years_experience,
                            salary_expected_from, salary_currency,
                            skills, formats, raw_resume, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            resume_id = excluded.resume_id,
            title = COALESCE(excluded.title, profile.title),
            years_experience = COALESCE(excluded.years_experience, profile.years_experience),
            salary_expected_from = COALESCE(excluded.salary_expected_from, profile.salary_expected_from),
            salary_currency = COALESCE(excluded.salary_currency, profile.salary_currency),
            skills = excluded.skills,
            formats = excluded.formats,
            raw_resume = excluded.raw_resume,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            str(resume_id or ""),
            title if isinstance(title, str) else None,
            years_exp,
            sal_amount,
            sal_cur,
            json.dumps(skills, ensure_ascii=False),
            json.dumps(formats, ensure_ascii=False),
            json.dumps(resume, ensure_ascii=False),
        ),
    )
    await db.commit()
    return {
        "resume_id": resume_id,
        "title": title,
        "years_experience": years_exp,
        "salary_amount": sal_amount,
        "salary_currency": sal_cur,
        "skills_count": len(skills),
        "formats": formats,
    }


async def get_profile(db: aiosqlite.Connection) -> dict | None:
    cur = await db.execute("SELECT * FROM profile WHERE id=1")
    row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["skills"] = json.loads(d["skills"] or "[]")
    d["formats"] = json.loads(d["formats"] or "[]")
    return d


async def update_manual(db: aiosqlite.Connection, fields: dict[str, Any]) -> None:
    allowed = {"title", "years_experience", "salary_expected_from", "salary_currency", "skills", "formats"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("skills", "formats") and not isinstance(v, str):
            v = json.dumps(v or [], ensure_ascii=False)
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return
    args.append(1)
    await db.execute(
        f"UPDATE profile SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        args,
    )
    await db.commit()
