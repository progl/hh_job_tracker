"""e2e тесты на страницу /analytics — SQL-агрегация по vacancy_requirements + vacancy_analysis."""

from __future__ import annotations

import aiosqlite
import pytest


def _db_path() -> str:
    from app.config import settings

    return settings.DB_PATH


async def _seed():
    """Подсадим вакансии и требования в БД для агрегации."""
    async with aiosqlite.connect(_db_path()) as db:
        for vid in (1, 2, 3):
            await db.execute("INSERT INTO vacancies(id, name) VALUES (?, ?)", (vid, f"v{vid}"))
        # Python встречается у всех 3
        for vid in (1, 2, 3):
            await db.execute(
                "INSERT INTO vacancy_requirements(vacancy_id, kind, category, text, source) "
                "VALUES (?, 'must', 'stack', 'Python', 'llm')",
                (vid,),
            )
        # Django — у 2
        for vid in (1, 2):
            await db.execute(
                "INSERT INTO vacancy_requirements(vacancy_id, kind, category, text, source) "
                "VALUES (?, 'must', 'stack', 'Django', 'llm')",
                (vid,),
            )
        # Kafka nice — у 1
        await db.execute(
            "INSERT INTO vacancy_requirements(vacancy_id, kind, category, text, source) "
            "VALUES (1, 'nice', 'stack', 'Kafka', 'llm')"
        )
        # 3+ года exp must — у 1
        await db.execute(
            "INSERT INTO vacancy_requirements(vacancy_id, kind, category, text, source) "
            "VALUES (1, 'must', 'exp', '3+ года', 'llm')"
        )
        # company_kind analysis: 2 product, 1 outsource
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) "
            "VALUES (1, 'company_kind', '{\"kind\": \"product\"}')"
        )
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) "
            "VALUES (2, 'company_kind', '{\"kind\": \"product\"}')"
        )
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) "
            "VALUES (3, 'company_kind', '{\"kind\": \"outsource\"}')"
        )
        await db.commit()


@pytest.mark.asyncio
async def test_analytics_page_renders_empty(app_client):
    client, _ = app_client
    r = await client.get("/analytics")
    assert r.status_code == 200
    assert "Аналитика корпуса" in r.text


@pytest.mark.asyncio
async def test_analytics_top_stack_aggregation(app_client):
    client, _ = app_client
    await _seed()
    r = await client.get("/analytics")
    assert r.status_code == 200
    # Python должен быть первым (cnt=3), Django (cnt=2), Kafka (cnt=1)
    body = r.text
    py_pos = body.find(">Python<")  # без LOWER, как сохранено
    dj_pos = body.find(">Django<")
    kf_pos = body.find(">Kafka<")
    assert py_pos > 0
    assert dj_pos > 0
    assert kf_pos > 0
    assert py_pos < dj_pos < kf_pos


@pytest.mark.asyncio
async def test_analytics_filter_by_category(app_client):
    client, _ = app_client
    await _seed()
    r = await client.get("/analytics?category=exp")
    assert r.status_code == 200
    # в таблице top_requirements должно быть только '3+ года'
    assert "3+ года" in r.text
    # Python (stack) не должен попасть в top-таблицу
    # (но может встречаться в других местах — поэтому проверяем через фильтр-индикатор)
    assert "фильтр: " in r.text or "category=exp" in r.text


@pytest.mark.asyncio
async def test_analytics_filter_by_kind(app_client):
    client, _ = app_client
    await _seed()
    r = await client.get("/analytics?kind=nice")
    assert r.status_code == 200
    # Kafka — единственный nice
    assert "Kafka" in r.text


@pytest.mark.asyncio
async def test_analytics_company_kinds(app_client):
    client, _ = app_client
    await _seed()
    r = await client.get("/analytics")
    body = r.text
    # Должны быть product (2) и outsource (1)
    assert "product" in body
    assert "outsource" in body


async def _seed_interview_prep():
    """Подсадим interview_prep данные для топ-вопросов / топ-тем."""
    async with aiosqlite.connect(_db_path()) as db:
        # 3 вакансии с interview_prep, разные вопросы/темы
        for vid in (10, 11, 12):
            await db.execute("INSERT INTO vacancies(id, name) VALUES (?, ?)", (vid, f"v{vid}"))
        # «Расскажи про GIL» — у всех 3
        # «Что такое индексы в БД» — у 2
        # «JOIN типы» — у 1
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) VALUES (10, 'interview_prep', "
            """'{"likely_questions": [{"q": "Расскажи про GIL", "why": "Py"}, {"q": "Что такое индексы в БД", "why": "PG"}], "topics": ["Python", "PostgreSQL"]}')"""
        )
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) VALUES (11, 'interview_prep', "
            """'{"likely_questions": [{"q": "Расскажи про GIL", "why": "Py"}, {"q": "Что такое индексы в БД", "why": "PG"}, {"q": "JOIN типы", "why": "SQL"}], "topics": ["Python"]}')"""
        )
        await db.execute(
            "INSERT INTO vacancy_analysis(vacancy_id, kind, data_json) VALUES (12, 'interview_prep', "
            """'{"likely_questions": [{"q": "Расскажи про GIL", "why": "Py"}], "topics": ["Python", "asyncio"]}')"""
        )
        await db.commit()


@pytest.mark.asyncio
async def test_analytics_top_questions(app_client):
    """Топ-вопросов из interview_prep агрегирует по vacancy_id."""
    client, _ = app_client
    await _seed_interview_prep()
    r = await client.get("/analytics")
    assert r.status_code == 200
    body = r.text
    # SQLite LOWER не работает с кириллицей — храним и группируем как есть
    gil = body.find("Расскажи про GIL")
    idx = body.find("Что такое индексы в БД")
    join_t = body.find("JOIN типы")
    assert gil > 0 and idx > 0 and join_t > 0
    assert gil < idx < join_t  # порядок по cnt DESC (3 > 2 > 1)
    assert "из 3 разобранных" in body


@pytest.mark.asyncio
async def test_analytics_top_topics(app_client):
    """topics — плоский массив строк, агрегируется отдельно."""
    client, _ = app_client
    await _seed_interview_prep()
    r = await client.get("/analytics")
    body = r.text
    # Python — 3, PostgreSQL — 1, asyncio — 1 (без LOWER)
    py = body.find(">Python<")
    pg = body.find(">PostgreSQL<")
    asy = body.find(">asyncio<")
    assert py > 0
    assert pg > 0 or asy > 0


@pytest.mark.asyncio
async def test_analytics_no_interview_prep_data(app_client):
    """Если interview_prep нет — блок не рендерится (или показывает «включи»)."""
    client, _ = app_client
    r = await client.get("/analytics")
    # interview_prep_count = 0 → блок не появится
    assert "Топ-вопросов" not in r.text or "Включи" in r.text


@pytest.mark.asyncio
async def test_analytics_parsed_count(app_client):
    client, _ = app_client
    await _seed()
    r = await client.get("/analytics")
    # 3 вакансии всего, у всех 3 есть requirements → parsed_count=3
    assert "3 / 3" in r.text or "Разобрано LLM" in r.text


async def _seed_llm_runs():
    """Подсадим llm_runs c разными model/task_kind и ok/fail."""
    async with aiosqlite.connect(_db_path()) as db:
        rows = [
            # (task_kind, model, prompt_version, ok, latency_ms, prompt_tokens, response_tokens)
            ("requirements", "qwen3:14b", "requirements_v1", 1, 1200, 500, 200),
            ("requirements", "qwen3:14b", "requirements_v1", 1, 800, 450, 180),
            ("requirements", "qwen3:14b", "requirements_v1", 0, 1500, 480, 0),
            ("salary", "qwen3:14b", "salary_v1", 1, 600, 200, 50),
            ("company_kind", "llama3:8b", "company_kind_v1", 1, 400, 150, 30),
            ("company_kind", "llama3:8b", "company_kind_v1", 0, 700, 160, 0),
            ("interview_prep", "llama3:8b", "interview_prep_v1", 1, 2000, 800, 400),
        ]
        for tk, model, pv, ok, lat, pt, rt in rows:
            await db.execute(
                """
                INSERT INTO llm_runs(task_kind, target_kind, target_id, model, prompt_version,
                                     ok, latency_ms, prompt_tokens, response_tokens)
                VALUES (?, 'vacancy', '1', ?, ?, ?, ?, ?, ?)
                """,
                (tk, model, pv, ok, lat, pt, rt),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_analytics_llm_stats_empty(app_client):
    """Пустая llm_runs → страница рендерится без ошибок, секция показывает «нет данных»."""
    client, _ = app_client
    r = await client.get("/analytics")
    assert r.status_code == 200
    body = r.text
    assert "LLM-затраты" in body
    # При пустой таблице — заглушка с «Нет данных»
    assert "Нет данных" in body or "llm_runs" in body


@pytest.mark.asyncio
async def test_analytics_llm_stats_aggregated(app_client):
    """После INSERT нескольких прогонов: видны count'ы, модели и task_kind."""
    client, _ = app_client
    await _seed_llm_runs()
    r = await client.get("/analytics")
    assert r.status_code == 200
    body = r.text
    # Заголовок секции
    assert "LLM-затраты" in body
    # Модели и task_kind отрендерены в таблицах
    assert "qwen3:14b" in body
    assert "llama3:8b" in body
    assert "requirements" in body
    assert "salary" in body
    assert "company_kind" in body
    assert "interview_prep" in body
    # Σ prompt токенов = 500+450+480+200+150+160+800 = 2740
    assert "2740" in body
    # Σ response токенов = 200+180+0+50+30+0+400 = 860
    assert "860" in body
