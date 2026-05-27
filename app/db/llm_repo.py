"""Repo для llm_runs и vacancy_requirements."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite


async def insert_run(
    db: aiosqlite.Connection,
    *,
    task_kind: str,
    target_kind: str | None,
    target_id: str | None,
    model: str,
    prompt_version: str,
    system_prompt: str | None,
    user_prompt: str | None,
    response_raw: str | None,
    parsed_json: Any | None,
    ok: bool,
    error: str | None,
    latency_ms: int | None,
    prompt_tokens: int | None,
    response_tokens: int | None,
) -> int:
    cur = await db.execute(
        """
        INSERT INTO llm_runs(task_kind, target_kind, target_id, model, prompt_version,
                             system_prompt, user_prompt, response_raw, parsed_json,
                             ok, error, latency_ms, prompt_tokens, response_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_kind,
            target_kind,
            target_id,
            model,
            prompt_version,
            system_prompt,
            user_prompt,
            response_raw,
            json.dumps(parsed_json, ensure_ascii=False) if parsed_json is not None else None,
            1 if ok else 0,
            error,
            latency_ms,
            prompt_tokens,
            response_tokens,
        ),
    )
    await db.commit()
    return cur.lastrowid


async def list_runs(
    db: aiosqlite.Connection,
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    task_kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    where: list[str] = []
    args: list[Any] = []
    if target_kind:
        where.append("target_kind = ?")
        args.append(target_kind)
    if target_id is not None:
        where.append("target_id = ?")
        args.append(str(target_id))
    if task_kind:
        where.append("task_kind = ?")
        args.append(task_kind)
    sql = "SELECT * FROM llm_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    cur = await db.execute(sql, args)
    rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("parsed_json"):
            try:
                d["parsed_json"] = json.loads(d["parsed_json"])
            except Exception:
                pass
        out.append(d)
    return out


async def get_run(db: aiosqlite.Connection, run_id: int) -> dict | None:
    cur = await db.execute("SELECT * FROM llm_runs WHERE id = ?", (run_id,))
    r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("parsed_json"):
        try:
            d["parsed_json"] = json.loads(d["parsed_json"])
        except Exception:
            pass
    return d


async def replace_requirements(
    db: aiosqlite.Connection,
    vacancy_id: int,
    items: list[dict],
    *,
    source: str = "llm",
    llm_run_id: int | None = None,
) -> int:
    """Удаляет старые requirements того же source и вставляет новые. Возвращает кол-во вставленных."""
    await db.execute(
        "DELETE FROM vacancy_requirements WHERE vacancy_id = ? AND source = ?",
        (vacancy_id, source),
    )
    inserted = 0
    for it in items:
        kind = it.get("kind") or "must"
        category = it.get("category")
        text = (it.get("text") or "").strip()
        if not text:
            continue
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO vacancy_requirements(vacancy_id, kind, category, text, source, llm_run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (vacancy_id, kind, category, text, source, llm_run_id),
            )
            inserted += 1
        except Exception:
            pass
    await db.commit()
    return inserted


async def get_requirements(db: aiosqlite.Connection, vacancy_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM vacancy_requirements WHERE vacancy_id = ? ORDER BY kind, category, text",
        (vacancy_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# --- vacancy_analysis: универсальное хранилище для «однообъектных» анализов ---


async def upsert_analysis(
    db: aiosqlite.Connection,
    vacancy_id: int,
    kind: str,
    data: Any,
    llm_run_id: int | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO vacancy_analysis(vacancy_id, kind, data_json, llm_run_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(vacancy_id, kind) DO UPDATE SET
            data_json = excluded.data_json,
            llm_run_id = excluded.llm_run_id,
            created_at = CURRENT_TIMESTAMP
        """,
        (vacancy_id, kind, json.dumps(data, ensure_ascii=False), llm_run_id),
    )
    await db.commit()


async def get_analysis(db: aiosqlite.Connection, vacancy_id: int, kind: str) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM vacancy_analysis WHERE vacancy_id = ? AND kind = ?",
        (vacancy_id, kind),
    )
    r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["data"] = json.loads(d.pop("data_json"))
    except Exception:
        d["data"] = None
    return d


async def get_all_analysis(db: aiosqlite.Connection, vacancy_id: int) -> dict[str, dict]:
    """Возвращает {kind: {data, llm_run_id, created_at}}."""
    cur = await db.execute(
        "SELECT * FROM vacancy_analysis WHERE vacancy_id = ?",
        (vacancy_id,),
    )
    rows = await cur.fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d.pop("data_json"))
        except Exception:
            d["data"] = None
        out[d["kind"]] = d
    return out
