"""Тесты на app/llm/registry.py: реестр анализаторов, analyze_one, enabled-настройки."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.llm import registry as reg


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


def _patch_generate(monkeypatch, response_map: dict | None = None, default: _FakeResp | None = None):
    """response_map: prompt_version → _FakeResp. Иначе default."""
    response_map = response_map or {}
    default = default or _FakeResp(ok=True, parsed={"ok": True})

    async def _fake(**kwargs):
        # роутим по содержанию system_prompt — у каждого analyzer уникальный
        system = kwargs.get("system") or ""
        for marker, resp in response_map.items():
            if marker in system:
                return resp
        return default

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    # registry импортирует client как llm_client — патчим оба места на всякий случай
    monkeypatch.setattr(reg.llm_client, "generate", _fake)


@pytest.mark.asyncio
async def test_registry_has_expected_analyzers():
    assert "requirements" in reg.ANALYZERS
    assert "salary" in reg.ANALYZERS
    assert "company_kind" in reg.ANALYZERS
    assert "summary" in reg.ANALYZERS
    assert "match_essay" in reg.ANALYZERS
    assert "interview_prep" in reg.ANALYZERS
    assert "soft_skills_employer" in reg.ANALYZERS
    assert "cover_letter" in reg.ANALYZERS
    assert reg.ANALYZERS["requirements"].default_enabled is True
    assert reg.ANALYZERS["salary"].default_enabled is False
    assert reg.ANALYZERS["match_essay"].default_enabled is False
    assert reg.ANALYZERS["interview_prep"].default_enabled is False
    assert reg.ANALYZERS["soft_skills_employer"].default_enabled is False
    assert reg.ANALYZERS["cover_letter"].default_enabled is False
    # cover_letter — тяжёлая задача, fast=False
    assert reg.ANALYZERS["cover_letter"].fast is False


@pytest.mark.asyncio
async def test_get_enabled_default_uses_default_enabled(tmp_db):
    enabled = await reg.get_enabled_analyzers(tmp_db)
    # дефолт — только requirements
    assert enabled == ["requirements"]


@pytest.mark.asyncio
async def test_set_enabled_persists(tmp_db):
    await reg.set_enabled_analyzers(tmp_db, ["requirements", "summary", "unknown_kind"])
    enabled = await reg.get_enabled_analyzers(tmp_db)
    # unknown_kind отфильтрован
    assert sorted(enabled) == ["requirements", "summary"]


@pytest.mark.asyncio
async def test_set_enabled_empty_means_nothing(tmp_db):
    await reg.set_enabled_analyzers(tmp_db, [])
    assert await reg.get_enabled_analyzers(tmp_db) == []


@pytest.mark.asyncio
async def test_analyze_one_unknown_kind(tmp_db, monkeypatch):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (1, 'v', 'desc')")
    await tmp_db.commit()
    _patch_generate(monkeypatch, default=_FakeResp(ok=True, parsed={}))
    res = await reg.analyze_one(tmp_db, 1, ["nonexistent_analyzer"])
    assert len(res) == 1
    assert res[0].ok is False
    assert res[0].error == "unknown_analyzer"


@pytest.mark.asyncio
async def test_analyze_summary_saves_to_vacancy_analysis(tmp_db, monkeypatch):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (2, 'v', 'Python backend dev')")
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "короткое резюме": _FakeResp(parsed={"summary": "Py backend, удалёнка"}),
        },
    )
    res = await reg.analyze_one(tmp_db, 2, ["summary"])
    assert len(res) == 1
    assert res[0].ok is True
    assert res[0].kind == "summary"
    assert res[0].data == {"summary": "Py backend, удалёнка"}
    # проверяем хранилище
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 2, "summary")
    assert a is not None
    assert a["data"]["summary"] == "Py backend, удалёнка"


@pytest.mark.asyncio
async def test_analyze_salary(tmp_db, monkeypatch):
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description) VALUES (3, 'v', 'зп от 200 до 350 тыс')"
    )
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "зарплатные ожидания": _FakeResp(
                parsed={
                    "amount_from": 200000,
                    "amount_to": 350000,
                    "currency": "RUR",
                    "gross": True,
                    "period": "month",
                    "note": "от 200 до 350 тыс",
                }
            ),
        },
    )
    res = await reg.analyze_one(tmp_db, 3, ["salary"])
    assert res[0].ok is True
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 3, "salary")
    assert a["data"]["amount_from"] == 200000


@pytest.mark.asyncio
async def test_analyze_company_kind(tmp_db, monkeypatch):
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description, company_name) "
        "VALUES (4, 'Senior', 'описание длинное', 'Yandex')"
    )
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "классифицируешь работодателя": _FakeResp(
                parsed={
                    "kind": "product",
                    "confidence": 0.9,
                    "reasoning": "yandex — продуктовая",
                }
            ),
        },
    )
    res = await reg.analyze_one(tmp_db, 4, ["company_kind"])
    assert res[0].ok is True
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 4, "company_kind")
    assert a["data"]["kind"] == "product"


@pytest.mark.asyncio
async def test_analyze_multiple_kinds_at_once(tmp_db, monkeypatch):
    """Несколько анализов за один вызов analyze_one."""
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description, company_name) VALUES (5, 'X', 'описание тест', 'Co')"
    )
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "короткое резюме": _FakeResp(parsed={"summary": "S"}),
            "зарплатные ожидания": _FakeResp(
                parsed={
                    "amount_from": None,
                    "amount_to": None,
                    "currency": None,
                    "gross": None,
                    "period": None,
                    "note": None,
                }
            ),
            "классифицируешь работодателя": _FakeResp(
                parsed={
                    "kind": "other",
                    "confidence": 0.3,
                    "reasoning": "не ясно",
                }
            ),
        },
    )
    res = await reg.analyze_one(tmp_db, 5, ["summary", "salary", "company_kind"])
    assert len(res) == 3
    assert all(r.ok for r in res)
    from app.db import llm_repo

    all_a = await llm_repo.get_all_analysis(tmp_db, 5)
    assert set(all_a.keys()) == {"summary", "salary", "company_kind"}


@pytest.mark.asyncio
async def test_analyze_requirements_delegates_to_existing(tmp_db, monkeypatch):
    """Анализатор 'requirements' использует существующую parse_one и пишет в vacancy_requirements."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (6, 'v', 'нужен Python')")
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "строгий парсер": _FakeResp(
                parsed={
                    "requirements": [
                        {"kind": "must", "category": "stack", "text": "Python"},
                    ]
                }
            ),
        },
    )
    res = await reg.analyze_one(tmp_db, 6, ["requirements"])
    assert res[0].ok is True
    from app.db import llm_repo

    reqs = await llm_repo.get_requirements(tmp_db, 6)
    assert len(reqs) == 1
    assert reqs[0]["text"] == "Python"


@pytest.mark.asyncio
async def test_analyze_one_uses_runtime_model_for_slow(tmp_db, monkeypatch):
    """analyze_one с model=None для НЕ-fast анализатора (requirements) берёт requirements_model."""
    from app.llm import settings as llm_settings

    await llm_settings.set_requirements_model(tmp_db, "slow-custom:7b")
    await llm_settings.set_fast_model(tmp_db, "fast-custom:3b")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (7, 'v', 'desc')")
    await tmp_db.commit()

    captured: dict = {}
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        captured["model"] = kwargs["model"]
        return _FakeResp(
            parsed={"requirements": [{"kind": "must", "category": "stack", "text": "X"}]},
            model=kwargs["model"],
        )

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    # requirements — fast=False → должен взять slow модель
    await reg.analyze_one(tmp_db, 7, ["requirements"], model=None)
    assert captured["model"] == "slow-custom:7b"


@pytest.mark.asyncio
async def test_analyze_one_uses_fast_model_for_fast(tmp_db, monkeypatch):
    """analyze_one с model=None для fast анализатора (summary) берёт fast_model."""
    from app.llm import settings as llm_settings

    await llm_settings.set_requirements_model(tmp_db, "slow-custom:7b")
    await llm_settings.set_fast_model(tmp_db, "fast-custom:3b")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (8, 'v', 'desc')")
    await tmp_db.commit()

    captured: dict = {}
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        captured["model"] = kwargs["model"]
        return _FakeResp(parsed={"summary": "x"}, model=kwargs["model"])

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    # summary — fast=True → должен взять fast модель
    await reg.analyze_one(tmp_db, 8, ["summary"], model=None)
    assert captured["model"] == "fast-custom:3b"


@pytest.mark.asyncio
async def test_analyze_one_explicit_model_overrides_fast(tmp_db, monkeypatch):
    """Если model передан явно — используется для всех, fast не применяется."""
    from app.llm import settings as llm_settings

    await llm_settings.set_fast_model(tmp_db, "fast-custom:3b")
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (9, 'v', 'desc')")
    await tmp_db.commit()

    captured: dict = {}
    from app.llm import client as llm_client

    async def _fake(**kwargs):
        captured["model"] = kwargs["model"]
        return _FakeResp(parsed={"summary": "x"}, model=kwargs["model"])

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    await reg.analyze_one(tmp_db, 9, ["summary"], model="explicit:5b")
    assert captured["model"] == "explicit:5b"


@pytest.mark.asyncio
async def test_analyze_match_essay_uses_profile(tmp_db, monkeypatch):
    """match_essay подмешивает profile.skills в промпт."""
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description, company_name, salary_rub) "
        "VALUES (10, 'Senior Py', 'Python+Django senior', 'Acme', 250000)"
    )
    await tmp_db.execute(
        "INSERT INTO profile(id, title, years_experience, salary_expected_from, salary_currency, skills) "
        "VALUES (1, 'Senior Python', 7.5, 200000, 'RUR', '[\"Python\",\"Django\",\"PostgreSQL\"]')"
    )
    await tmp_db.commit()

    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeResp(
            parsed={"score": 85, "verdict": "match", "matches": ["Python"], "gaps": [], "reasoning": "OK"}
        )

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 10, ["match_essay"])
    assert res[0].ok is True
    # промпт содержит профиль
    user = captured["prompt"]
    assert "Python" in user and "Django" in user
    assert "Senior Python" in user  # title
    assert "7.5 лет" in user  # years_experience
    # данные сохранены
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 10, "match_essay")
    assert a["data"]["score"] == 85
    assert a["data"]["verdict"] == "match"


@pytest.mark.asyncio
async def test_analyze_interview_prep_includes_history(tmp_db, monkeypatch):
    """interview_prep подмешивает прошлые отклики в эту же компанию."""
    # текущая вакансия
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, company_id, description) "
        "VALUES (20, 'Backend', 500, 'Python нужен опыт')"
    )
    # 2 вакансии той же компании + отклики
    await tmp_db.execute("INSERT INTO vacancies(id, name, company_id) VALUES (21, 'Old role', 500)")
    await tmp_db.execute("INSERT INTO vacancies(id, name, company_id) VALUES (22, 'Earlier role', 500)")
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, last_state, last_modified) "
        "VALUES (1, 21, 'INVITATION', '2024-10-01T12:00:00')"
    )
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, last_state, last_modified) "
        "VALUES (2, 22, 'DISCARD_BY_EMPLOYER', '2024-08-15T12:00:00')"
    )
    # вакансия другой компании — НЕ должна попасть
    await tmp_db.execute("INSERT INTO vacancies(id, name, company_id) VALUES (99, 'Other co', 999)")
    await tmp_db.execute(
        "INSERT INTO negotiations(id, vacancy_id, last_state, last_modified) "
        "VALUES (3, 99, 'INVITATION', '2024-09-01T12:00:00')"
    )
    await tmp_db.commit()

    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeResp(
            parsed={
                "topics": ["Python", "highload"],
                "likely_questions": [{"q": "GIL?", "why": "Python"}],
                "code_tasks": ["LRU cache"],
                "red_flags": [],
            }
        )

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 20, ["interview_prep"])
    assert res[0].ok is True
    user = captured["prompt"]
    # история компании в промпте
    assert "Old role" in user
    assert "Earlier role" in user
    assert "INVITATION" in user
    # чужая компания НЕ в промпте
    assert "Other co" not in user


@pytest.mark.asyncio
async def test_analyze_interview_prep_no_history(tmp_db, monkeypatch):
    """Если нет прошлых откликов в эту компанию — секция истории пуста, но не падаем."""
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, company_id, description) VALUES (30, 'X', 700, 'описание')"
    )
    await tmp_db.commit()

    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeResp(parsed={"topics": [], "likely_questions": [], "code_tasks": [], "red_flags": []})

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 30, ["interview_prep"])
    assert res[0].ok is True
    user = captured["prompt"]
    assert "МОИ ПРОШЛЫЕ ОТКЛИКИ" not in user  # секция не появляется


@pytest.mark.asyncio
async def test_analyze_soft_skills_employer(tmp_db, monkeypatch):
    """soft_skills_employer оценивает работодателя по тону описания."""
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description, company_name) "
        "VALUES (50, 'Senior Py', 'Дружная команда, гибкий график, забота о сотрудниках', 'Cozy Co')"
    )
    await tmp_db.commit()
    _patch_generate(
        monkeypatch,
        response_map={
            "HR-консультант": _FakeResp(
                parsed={
                    "tone": "warm",
                    "wlb_score": 8,
                    "team_culture": "modern",
                    "growth_opportunities": 7,
                    "red_flags": [],
                    "green_flags": ["гибкий график", "забота о сотрудниках"],
                    "summary": "Тёплый тон, ценят WLB и команду.",
                }
            ),
        },
    )
    res = await reg.analyze_one(tmp_db, 50, ["soft_skills_employer"])
    assert len(res) == 1
    assert res[0].ok is True
    assert res[0].kind == "soft_skills_employer"
    assert res[0].data["tone"] == "warm"
    assert res[0].data["wlb_score"] == 8
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 50, "soft_skills_employer")
    assert a is not None
    assert a["data"]["team_culture"] == "modern"
    assert a["data"]["green_flags"] == ["гибкий график", "забота о сотрудниках"]


@pytest.mark.asyncio
async def test_analyze_match_essay_without_profile(tmp_db, monkeypatch):
    """Если профиля нет — match_essay всё равно работает (с пустыми скиллами)."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (40, 'v', 'описание')")
    await tmp_db.commit()

    async def _fake(**kwargs):
        return _FakeResp(
            parsed={"score": 0, "verdict": "skip", "matches": [], "gaps": [], "reasoning": "no data"}
        )

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 40, ["match_essay"])
    assert res[0].ok is True


@pytest.mark.asyncio
async def test_analyze_cover_letter_uses_resume(tmp_db, monkeypatch):
    """cover_letter подмешивает в промпт profile.raw_resume и описание вакансии."""
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, description, company_name, salary_rub) "
        "VALUES (50, 'Senior Py', 'Python+Django, опыт highload', 'Acme', 250000)"
    )
    await tmp_db.execute(
        "INSERT INTO profile(id, title, years_experience, salary_expected_from, salary_currency, "
        "skills, raw_resume) VALUES (1, 'Senior Python', 7.0, 200000, 'RUR', "
        '\'["Python","Django","PostgreSQL"]\', \'{"work": [{"company": "Acme prev", "tech": "Python+Django"}]}\')'
    )
    await tmp_db.commit()

    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeResp(
            parsed={
                "letter": "Здравствуйте, ...",
                "highlights": ["7 лет Python", "Django в проде"],
                "tone_note": "уверенный",
            }
        )

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 50, ["cover_letter"])
    assert res[0].ok is True
    user_prompt = captured["prompt"]
    # резюме попало в контекст
    assert "Acme prev" in user_prompt
    assert "Python+Django" in user_prompt
    # профиль тоже
    assert "Senior Python" in user_prompt
    assert "7" in user_prompt
    # вакансия
    assert "Senior Py" in user_prompt
    assert "highload" in user_prompt
    # данные сохранены
    from app.db import llm_repo

    a = await llm_repo.get_analysis(tmp_db, 50, "cover_letter")
    assert a["data"]["letter"].startswith("Здравствуйте")
    assert "7 лет Python" in a["data"]["highlights"]


@pytest.mark.asyncio
async def test_analyze_cover_letter_no_resume(tmp_db, monkeypatch):
    """Если в profile нет raw_resume — возвращаем no_resume error, БЕЗ вызова LLM."""
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (51, 'v', 'desc')")
    # профиль без raw_resume
    await tmp_db.execute("INSERT INTO profile(id, title) VALUES (1, 'Junior')")
    await tmp_db.commit()

    called: dict = {"n": 0}

    async def _fake(**kwargs):
        called["n"] += 1
        return _FakeResp(parsed={"letter": "should-not-happen"})

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "generate", _fake)
    monkeypatch.setattr(reg.llm_client, "generate", _fake)

    res = await reg.analyze_one(tmp_db, 51, ["cover_letter"])
    assert res[0].ok is False
    assert res[0].error == "no_resume"
    # LLM не должен был вызываться
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_missing_analysis_kinds(tmp_db):
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (90, 'v', 'd')")
    # есть requirements + company_kind, нет soft_skills_employer
    await tmp_db.execute(
        "INSERT INTO vacancy_requirements(vacancy_id, kind, text, source) VALUES (90, 'must', 'python', 'llm')"
    )
    await tmp_db.execute(
        "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) VALUES (90, 'company_kind', '{}')"
    )
    await tmp_db.commit()

    enabled = ["requirements", "company_kind", "soft_skills_employer", "summary"]
    missing = await reg.missing_analysis_kinds(tmp_db, 90, enabled)
    assert set(missing) == {"soft_skills_employer", "summary"}
    # порядок сохранён (как в enabled)
    assert missing == ["soft_skills_employer", "summary"]

    # у вакансии без анализов — все kinds недостающие
    await tmp_db.execute("INSERT INTO vacancies(id, name, description) VALUES (91, 'v', 'd')")
    await tmp_db.commit()
    assert await reg.missing_analysis_kinds(tmp_db, 91, enabled) == enabled
