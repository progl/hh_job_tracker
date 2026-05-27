"""Тесты на app/llm/settings.py — runtime-настройки в cookie_store."""

from __future__ import annotations

import pytest

from app.llm import settings as llm_settings


@pytest.mark.asyncio
async def test_get_default_when_not_set(tmp_db, monkeypatch):
    monkeypatch.setattr(llm_settings.settings, "LLM_MODEL_REQUIREMENTS", "qwen3:14b")
    assert await llm_settings.get_requirements_model(tmp_db) == "qwen3:14b"


@pytest.mark.asyncio
async def test_set_then_get(tmp_db):
    await llm_settings.set_requirements_model(tmp_db, "llama3.1:8b")
    assert await llm_settings.get_requirements_model(tmp_db) == "llama3.1:8b"


@pytest.mark.asyncio
async def test_set_overwrites(tmp_db):
    await llm_settings.set_requirements_model(tmp_db, "qwen2.5:14b")
    await llm_settings.set_requirements_model(tmp_db, "qwen3:14b")
    assert await llm_settings.get_requirements_model(tmp_db) == "qwen3:14b"


@pytest.mark.asyncio
async def test_get_ignores_empty_value(tmp_db, monkeypatch):
    """Если в БД пустая строка — fallback на дефолт из settings."""
    monkeypatch.setattr(llm_settings.settings, "LLM_MODEL_REQUIREMENTS", "default-model")
    await tmp_db.execute(
        "INSERT INTO cookie_store(key, value) VALUES (?, '')",
        ("llm.model.requirements",),
    )
    await tmp_db.commit()
    assert await llm_settings.get_requirements_model(tmp_db) == "default-model"
