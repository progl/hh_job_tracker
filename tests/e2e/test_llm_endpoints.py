"""e2e тесты на LLM-эндпоинты и страницу /llm-logs.

llm_client.generate мокается, чтобы не дёргать настоящий Ollama."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite
import pytest


@dataclass
class _FakeResp:
    ok: bool = True
    text: str = "{}"
    parsed: Any = None
    error: str | None = None
    model: str = "fake"
    latency_ms: int = 10
    prompt_tokens: int | None = 5
    response_tokens: int | None = 5


def _db_path() -> str:
    from app.config import settings

    return settings.DB_PATH


async def _insert_vacancy(vid: int, name: str = "v", desc: str = "Python нужен 3 года опыта"):
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT OR REPLACE INTO vacancies(id, name, description) VALUES (?, ?, ?)",
            (vid, name, desc),
        )
        await db.commit()


def _patch_llm(monkeypatch, response: _FakeResp):
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        return response

    monkeypatch.setattr(llm_client, "generate", _fake)


# ----- /api/vacancy/{vid}/llm-parse -----


@pytest.mark.asyncio
async def test_llm_parse_success(app_client, monkeypatch):
    client, _ = app_client
    await _insert_vacancy(101)
    _patch_llm(
        monkeypatch,
        _FakeResp(
            ok=True,
            text="{}",
            parsed={"requirements": [{"kind": "must", "category": "stack", "text": "Python"}]},
            model="qwen3:14b",
        ),
    )
    r = await client.post("/api/vacancy/101/llm-parse")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["model"] == "qwen3:14b"
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "Python"
    assert body["llm_run_id"] > 0


@pytest.mark.asyncio
async def test_llm_parse_with_model_form(app_client, monkeypatch):
    client, _ = app_client
    await _insert_vacancy(102)
    capture: dict = {}
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        capture.update(kwargs)
        return _FakeResp(ok=True, text="{}", parsed={"requirements": []}, model=kwargs["model"])

    monkeypatch.setattr(llm_client, "generate", _fake)
    r = await client.post("/api/vacancy/102/llm-parse", data={"model": "llama3.1:8b"})
    assert r.status_code == 200
    assert capture["model"] == "llama3.1:8b"


@pytest.mark.asyncio
async def test_llm_parse_vacancy_not_found(app_client, monkeypatch):
    client, _ = app_client
    _patch_llm(monkeypatch, _FakeResp(parsed={"requirements": []}))
    r = await client.post("/api/vacancy/99999/llm-parse")
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "vacancy_not_found"


# ----- /api/vacancy/{vid}/llm-parse-multi -----


@pytest.mark.asyncio
async def test_llm_parse_multi(app_client, monkeypatch):
    client, _ = app_client
    await _insert_vacancy(110)
    responses = [
        _FakeResp(
            ok=True,
            parsed={"requirements": [{"kind": "must", "category": "stack", "text": "Py-from-A"}]},
            model="A",
        ),
        _FakeResp(
            ok=True,
            parsed={"requirements": [{"kind": "must", "category": "stack", "text": "Py-from-B"}]},
            model="B",
        ),
    ]
    idx = {"n": 0}
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        i = idx["n"]
        idx["n"] += 1
        return responses[i]

    monkeypatch.setattr(llm_client, "generate", _fake)

    r = await client.post("/api/vacancy/110/llm-parse-multi", data={"models": ["A", "B"]})
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 2
    # сохраняется последний успешный → Py-from-B
    from app.db import llm_repo

    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        reqs = await llm_repo.get_requirements(db, 110)
    assert len(reqs) == 1
    assert reqs[0]["text"] == "Py-from-B"


# ----- /api/llm/parse-corpus -----


@pytest.mark.asyncio
async def test_parse_corpus_processes_unparsed(app_client, monkeypatch):
    """Корпус-режим: берёт N необработанных вакансий."""
    client, _ = app_client
    # 3 вакансии: 2 без requirements, 1 с уже распарсенными
    for vid in (201, 202, 203):
        await _insert_vacancy(vid, desc=f"описание {vid} " * 30)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO vacancy_requirements(vacancy_id, kind, text, source) VALUES (203, 'must', 'X', 'llm')"
        )
        await db.commit()

    _patch_llm(
        monkeypatch,
        _FakeResp(parsed={"requirements": [{"kind": "must", "category": "stack", "text": "AutoPy"}]}),
    )

    r = await client.post("/api/llm/parse-corpus", data={"limit": "5", "only_unparsed": "true"})
    assert r.status_code == 200
    # task запущена — ждать пока завершится
    body = r.json()
    assert body.get("ok") in (True, None) or "task_id" in body  # формат _task_response
    # дождёмся завершения
    import asyncio

    for _ in range(40):
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT COUNT(DISTINCT vacancy_id) FROM vacancy_requirements WHERE source='llm'"
            )
            cnt = (await cur.fetchone())[0]
        if cnt >= 3:
            break
        await asyncio.sleep(0.1)
    # 201 и 202 разобраны новым LLM; 203 уже была
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        for vid in (201, 202):
            cur = await db.execute(
                "SELECT text FROM vacancy_requirements WHERE vacancy_id=? AND source='llm'", (vid,)
            )
            rows = await cur.fetchall()
            assert any(r["text"] == "AutoPy" for r in rows), f"vid {vid} не разобран"


# ----- /api/settings/llm-model -----


@pytest.mark.asyncio
async def test_set_llm_model_persists(app_client):
    client, _ = app_client
    r = await client.post("/api/settings/llm-model", data={"model": "qwen3:14b"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "qwen3:14b"}
    # читаем напрямую через repo
    from app.llm import settings as llm_settings

    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        assert await llm_settings.get_requirements_model(db) == "qwen3:14b"


# ----- /api/llm/runs -----


@pytest.mark.asyncio
async def test_llm_runs_list_endpoint(app_client, monkeypatch):
    client, _ = app_client
    await _insert_vacancy(301)
    _patch_llm(
        monkeypatch, _FakeResp(parsed={"requirements": [{"kind": "must", "category": "stack", "text": "Q"}]})
    )
    await client.post("/api/vacancy/301/llm-parse")
    r = await client.get("/api/llm/runs?target_id=301")
    body = r.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["target_id"] == "301"


# ----- /llm-logs -----


@pytest.mark.asyncio
async def test_llm_logs_page_renders(app_client, monkeypatch):
    client, _ = app_client
    await _insert_vacancy(401)
    _patch_llm(
        monkeypatch, _FakeResp(parsed={"requirements": [{"kind": "must", "category": "stack", "text": "Z"}]})
    )
    res = await client.post("/api/vacancy/401/llm-parse")
    rid = res.json()["llm_run_id"]

    r = await client.get("/llm-logs")
    assert r.status_code == 200
    assert "LLM-логи" in r.text

    r = await client.get(f"/llm-logs?run={rid}")
    assert r.status_code == 200
    assert f"run #{rid}" in r.text
    # фокусированный run раскрывает prompt/response
    assert "User prompt" in r.text
