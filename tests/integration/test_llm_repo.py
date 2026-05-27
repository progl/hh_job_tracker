"""Тесты на app/db/llm_repo.py — БД integration через tmp_db."""

from __future__ import annotations

import pytest

from app.db import llm_repo


@pytest.mark.asyncio
async def test_insert_run_minimal(tmp_db):
    rid = await llm_repo.insert_run(
        tmp_db,
        task_kind="requirements",
        target_kind="vacancy",
        target_id="123",
        model="qwen2.5:14b",
        prompt_version="v1",
        system_prompt="s",
        user_prompt="u",
        response_raw='{"x":1}',
        parsed_json={"x": 1},
        ok=True,
        error=None,
        latency_ms=100,
        prompt_tokens=10,
        response_tokens=20,
    )
    assert rid > 0
    got = await llm_repo.get_run(tmp_db, rid)
    assert got["task_kind"] == "requirements"
    assert got["model"] == "qwen2.5:14b"
    assert got["ok"] == 1
    # parsed_json раскрывается в dict при чтении
    assert got["parsed_json"] == {"x": 1}


@pytest.mark.asyncio
async def test_insert_run_error_path(tmp_db):
    rid = await llm_repo.insert_run(
        tmp_db,
        task_kind="requirements",
        target_kind="vacancy",
        target_id="42",
        model="llama3.1:8b",
        prompt_version="v1",
        system_prompt="s",
        user_prompt="u",
        response_raw="garbage",
        parsed_json=None,
        ok=False,
        error="json parse fail",
        latency_ms=50,
        prompt_tokens=None,
        response_tokens=None,
    )
    got = await llm_repo.get_run(tmp_db, rid)
    assert got["ok"] == 0
    assert got["error"] == "json parse fail"
    assert got["parsed_json"] is None


@pytest.mark.asyncio
async def test_list_runs_filters_by_target_and_task(tmp_db):
    for i in range(3):
        await llm_repo.insert_run(
            tmp_db,
            task_kind="requirements",
            target_kind="vacancy",
            target_id="100",
            model="m",
            prompt_version="v1",
            system_prompt=None,
            user_prompt=None,
            response_raw=None,
            parsed_json=None,
            ok=True,
            error=None,
            latency_ms=i,
            prompt_tokens=None,
            response_tokens=None,
        )
    await llm_repo.insert_run(
        tmp_db,
        task_kind="summary",
        target_kind="vacancy",
        target_id="100",
        model="m",
        prompt_version="v1",
        system_prompt=None,
        user_prompt=None,
        response_raw=None,
        parsed_json=None,
        ok=True,
        error=None,
        latency_ms=999,
        prompt_tokens=None,
        response_tokens=None,
    )
    await llm_repo.insert_run(
        tmp_db,
        task_kind="requirements",
        target_kind="vacancy",
        target_id="999",
        model="m",
        prompt_version="v1",
        system_prompt=None,
        user_prompt=None,
        response_raw=None,
        parsed_json=None,
        ok=True,
        error=None,
        latency_ms=42,
        prompt_tokens=None,
        response_tokens=None,
    )

    by_target = await llm_repo.list_runs(tmp_db, target_kind="vacancy", target_id="100")
    assert len(by_target) == 4
    by_task = await llm_repo.list_runs(tmp_db, task_kind="requirements")
    assert len(by_task) == 4  # 3 + 1
    both = await llm_repo.list_runs(tmp_db, task_kind="requirements", target_id="100")
    assert len(both) == 3
    # сортировка id DESC
    assert both[0]["id"] > both[-1]["id"]


@pytest.mark.asyncio
async def test_list_runs_limit(tmp_db):
    for i in range(10):
        await llm_repo.insert_run(
            tmp_db,
            task_kind="requirements",
            target_kind=None,
            target_id=None,
            model="m",
            prompt_version="v1",
            system_prompt=None,
            user_prompt=None,
            response_raw=None,
            parsed_json=None,
            ok=True,
            error=None,
            latency_ms=i,
            prompt_tokens=None,
            response_tokens=None,
        )
    rows = await llm_repo.list_runs(tmp_db, limit=3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_get_run_not_found(tmp_db):
    assert await llm_repo.get_run(tmp_db, 99999) is None


@pytest.mark.asyncio
async def test_replace_requirements_inserts(tmp_db):
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (10, 'v')")
    await tmp_db.commit()
    items = [
        {"kind": "must", "category": "stack", "text": "Python"},
        {"kind": "must", "category": "exp", "text": "3+ года"},
        {"kind": "plus", "category": "stack", "text": "Kafka"},
    ]
    n = await llm_repo.replace_requirements(tmp_db, 10, items, source="llm")
    assert n == 3
    got = await llm_repo.get_requirements(tmp_db, 10)
    assert len(got) == 3
    assert {g["text"] for g in got} == {"Python", "3+ года", "Kafka"}


@pytest.mark.asyncio
async def test_replace_requirements_clears_same_source(tmp_db):
    """replace_requirements удаляет старые записи того же source — это идемпотентный апдейт."""
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (11, 'v')")
    await tmp_db.commit()
    await llm_repo.replace_requirements(
        tmp_db,
        11,
        [
            {"kind": "must", "category": "stack", "text": "Old1"},
            {"kind": "must", "category": "stack", "text": "Old2"},
        ],
        source="llm",
    )
    await llm_repo.replace_requirements(
        tmp_db,
        11,
        [
            {"kind": "must", "category": "stack", "text": "New"},
        ],
        source="llm",
    )
    got = await llm_repo.get_requirements(tmp_db, 11)
    assert len(got) == 1
    assert got[0]["text"] == "New"


@pytest.mark.asyncio
async def test_replace_requirements_keeps_different_source(tmp_db):
    """source='regex' и source='llm' не конфликтуют."""
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (12, 'v')")
    await tmp_db.commit()
    await llm_repo.replace_requirements(
        tmp_db,
        12,
        [
            {"kind": "must", "category": "stack", "text": "FromRegex"},
        ],
        source="regex",
    )
    await llm_repo.replace_requirements(
        tmp_db,
        12,
        [
            {"kind": "must", "category": "stack", "text": "FromLLM"},
        ],
        source="llm",
    )
    got = await llm_repo.get_requirements(tmp_db, 12)
    assert len(got) == 2
    sources = {g["source"] for g in got}
    assert sources == {"regex", "llm"}


@pytest.mark.asyncio
async def test_replace_requirements_skips_empty_text(tmp_db):
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (13, 'v')")
    await tmp_db.commit()
    n = await llm_repo.replace_requirements(
        tmp_db,
        13,
        [
            {"kind": "must", "category": "stack", "text": "   "},
            {"kind": "must", "category": "stack", "text": ""},
            {"kind": "must", "category": "stack", "text": "OK"},
        ],
        source="llm",
    )
    assert n == 1


@pytest.mark.asyncio
async def test_replace_requirements_dedup_within_batch(tmp_db):
    """UNIQUE(vacancy_id, kind, text) — дубликаты в одном батче игнорируются."""
    await tmp_db.execute("INSERT INTO vacancies(id, name) VALUES (14, 'v')")
    await tmp_db.commit()
    await llm_repo.replace_requirements(
        tmp_db,
        14,
        [
            {"kind": "must", "category": "stack", "text": "Python"},
            {"kind": "must", "category": "stack", "text": "Python"},  # дубль
        ],
        source="llm",
    )
    got = await llm_repo.get_requirements(tmp_db, 14)
    assert len(got) == 1


@pytest.mark.asyncio
async def test_get_requirements_empty(tmp_db):
    assert await llm_repo.get_requirements(tmp_db, 99999) == []
