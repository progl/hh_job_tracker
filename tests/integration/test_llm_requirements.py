"""Тесты на app/llm/tasks/requirements.py — clean_description, parse_one, parse_one_multi_model.

llm_client.generate мокается, БД настоящая (tmp_db)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.llm.tasks import requirements as req_task


@dataclass
class _FakeLLMResponse:
    ok: bool
    text: str
    parsed: Any
    error: str | None = None
    model: str = "fake"
    latency_ms: int = 42
    prompt_tokens: int | None = 10
    response_tokens: int | None = 20


def _patch_generate(monkeypatch, response: _FakeLLMResponse, *, capture: dict | None = None):
    async def _fake(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return response

    monkeypatch.setattr(req_task.llm_client, "generate", _fake)


# ----- clean_description ----


def test_clean_description_removes_html():
    html = "<p>Hello <b>world</b></p>"
    assert req_task.clean_description(html) == "Hello world"


def test_clean_description_unescapes_entities():
    html = "<p>R&amp;D &mdash; 5+ лет</p>"
    out = req_task.clean_description(html)
    assert "R&D" in out


def test_clean_description_collapses_whitespace():
    html = "<p>a   b\n\t  c</p>"
    assert req_task.clean_description(html) == "a b c"


def test_clean_description_truncates(monkeypatch):
    monkeypatch.setattr(req_task.settings, "LLM_MAX_DESCRIPTION_CHARS", 10)
    out = req_task.clean_description("a" * 50)
    assert "truncated" in out
    assert len(out) < 50


def test_clean_description_empty():
    assert req_task.clean_description("") == ""
    assert req_task.clean_description(None) == ""


# ----- parse_one ----


@pytest.mark.asyncio
async def test_parse_one_vacancy_not_found(tmp_db, monkeypatch):
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=True, text="{}", parsed={}))
    res = await req_task.parse_one(tmp_db, 99999, model="m")
    assert res["ok"] is False
    assert res["reason"] == "vacancy_not_found"


@pytest.mark.asyncio
async def test_parse_one_empty_description(tmp_db, monkeypatch):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (1, 'v', NULL)")
    await tmp_db.commit()
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=True, text="{}", parsed={}))
    res = await req_task.parse_one(tmp_db, 1, model="m")
    assert res["ok"] is False
    assert res["reason"] == "empty_description"


@pytest.mark.asyncio
async def test_parse_one_success_saves_to_repo(tmp_db, monkeypatch):
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description) VALUES (2, 'v', 'нужен Python и Django')"
    )
    await tmp_db.commit()
    parsed = {
        "requirements": [
            {"kind": "must", "category": "stack", "text": "Python"},
            {"kind": "must", "category": "stack", "text": "Django"},
            {"kind": "plus", "category": "stack", "text": "Kafka"},
        ]
    }
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=True, text="{}", parsed=parsed, model="qwen3:14b"))
    res = await req_task.parse_one(tmp_db, 2, model="qwen3:14b")
    assert res["ok"] is True
    assert res["model"] == "qwen3:14b"
    assert len(res["items"]) == 3
    assert res["inserted"] == 3
    assert res["llm_run_id"] > 0

    # проверяем что записалось в обе таблицы
    from app.db import llm_repo

    run = await llm_repo.get_run(tmp_db, res["llm_run_id"])
    assert run["target_id"] == "2"
    assert run["ok"] == 1
    reqs = await llm_repo.get_requirements(tmp_db, 2)
    assert len(reqs) == 3


@pytest.mark.asyncio
async def test_parse_one_uses_runtime_model_when_none(tmp_db, monkeypatch):
    from app.llm import settings as llm_settings

    await llm_settings.set_requirements_model(tmp_db, "custom-model:7b")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (3, 'v', 'описание')")
    await tmp_db.commit()
    capture: dict = {}
    _patch_generate(
        monkeypatch,
        _FakeLLMResponse(ok=True, text="{}", parsed={"requirements": []}, model="custom-model:7b"),
        capture=capture,
    )
    res = await req_task.parse_one(tmp_db, 3, model=None)
    assert capture["model"] == "custom-model:7b"
    assert res["model"] == "custom-model:7b"


@pytest.mark.asyncio
async def test_parse_one_llm_error_still_logs_run(tmp_db, monkeypatch):
    """Ошибка LLM не должна терять прогон — всё равно пишем в llm_runs."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (4, 'v', 'desc')")
    await tmp_db.commit()
    _patch_generate(
        monkeypatch, _FakeLLMResponse(ok=False, text="", parsed=None, error="network: down", model="m")
    )
    res = await req_task.parse_one(tmp_db, 4, model="m")
    assert res["ok"] is False
    assert res["error"] == "network: down"
    assert res["items"] == []
    assert res["llm_run_id"] > 0
    from app.db import llm_repo

    run = await llm_repo.get_run(tmp_db, res["llm_run_id"])
    assert run["ok"] == 0
    assert run["error"] == "network: down"


@pytest.mark.asyncio
async def test_parse_one_invalid_items_filtered(tmp_db, monkeypatch):
    """LLM вернула странную структуру — пропускаем мусор, валидное оставляем."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (5, 'v', 'desc')")
    await tmp_db.commit()
    parsed = {
        "requirements": [
            {"kind": "must", "text": "Python"},  # категория отсутствует → "other"
            "garbage string",  # не dict → пропускаем
            {"text": ""},  # пустой text → пропускаем
            {"kind": "nice", "category": "stack", "text": "Kafka"},
        ]
    }
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=True, text="{}", parsed=parsed))
    res = await req_task.parse_one(tmp_db, 5, model="m")
    assert res["ok"] is True
    assert len(res["items"]) == 2
    assert res["items"][0]["text"] == "Python"
    assert res["items"][0]["category"] == "other"
    assert res["items"][1]["text"] == "Kafka"


@pytest.mark.asyncio
async def test_parse_one_save_requirements_false(tmp_db, monkeypatch):
    """save_requirements=False — пишем llm_run, но НЕ пишем в vacancy_requirements."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (6, 'v', 'desc')")
    await tmp_db.commit()
    parsed = {"requirements": [{"kind": "must", "category": "stack", "text": "X"}]}
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=True, text="{}", parsed=parsed))
    res = await req_task.parse_one(tmp_db, 6, model="m", save_requirements=False)
    assert res["ok"] is True
    assert res["inserted"] == 0
    from app.db import llm_repo

    assert await llm_repo.get_requirements(tmp_db, 6) == []
    assert res["llm_run_id"] > 0  # запуск всё равно залогирован


# ----- parse_one_multi_model ----


@pytest.mark.asyncio
async def test_parse_one_multi_model_saves_last_success(tmp_db, monkeypatch):
    """Сохраняем результат последнего УСПЕШНОГО прогона."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (7, 'v', 'desc')")
    await tmp_db.commit()

    call_count = {"n": 0}
    responses = [
        _FakeLLMResponse(
            ok=True,
            text="{}",
            parsed={
                "requirements": [
                    {"kind": "must", "category": "stack", "text": "FromQwen3"},
                ]
            },
            model="qwen3:14b",
        ),
        _FakeLLMResponse(ok=False, text="", parsed=None, error="oom", model="qwen2.5:14b"),
        _FakeLLMResponse(
            ok=True,
            text="{}",
            parsed={
                "requirements": [
                    {"kind": "must", "category": "stack", "text": "FromLlama"},
                ]
            },
            model="llama3.1:8b",
        ),
    ]

    async def _fake(**kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    monkeypatch.setattr(req_task.llm_client, "generate", _fake)

    out = await req_task.parse_one_multi_model(tmp_db, 7, ["qwen3:14b", "qwen2.5:14b", "llama3.1:8b"])
    assert len(out) == 3
    # сохраняется только из последнего успешного (llama3.1:8b)
    from app.db import llm_repo

    reqs = await llm_repo.get_requirements(tmp_db, 7)
    assert len(reqs) == 1
    assert reqs[0]["text"] == "FromLlama"


@pytest.mark.asyncio
async def test_parse_one_multi_model_all_fail_nothing_saved(tmp_db, monkeypatch):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (8, 'v', 'desc')")
    await tmp_db.commit()
    _patch_generate(monkeypatch, _FakeLLMResponse(ok=False, text="", parsed=None, error="x"))
    out = await req_task.parse_one_multi_model(tmp_db, 8, ["m1", "m2"])
    assert all(not r["ok"] for r in out)
    from app.db import llm_repo

    assert await llm_repo.get_requirements(tmp_db, 8) == []
    # но llm_runs всё равно созданы
    runs = await llm_repo.list_runs(tmp_db, target_id="8")
    assert len(runs) == 2
