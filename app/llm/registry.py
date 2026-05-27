"""Реестр LLM-анализаторов вакансий.

Идея: одна вакансия = N независимых анализов (требования, зарплата, тип компании, резюме, ...).
Каждый анализатор:
  - имеет уникальный kind ('requirements', 'salary', 'company_kind', 'summary', ...)
  - сам зовёт LLM и сам пишет результат в нужную таблицу (requirements → отдельная,
    остальные — универсальная vacancy_analysis)
  - умеет работать в составе батча: «прогони на вакансии X эти 4 анализа»

Включение/отключение — флаг `default_enabled` + runtime overrides через llm_settings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.db import llm_repo, profile_repo, vacancies_repo
from app.llm import client as llm_client
from app.llm import settings as llm_settings
from app.llm.tasks.requirements import clean_description
from app.llm.tasks.requirements import parse_one as parse_requirements_one

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    ok: bool
    kind: str
    data: Any  # для requirements — list[dict], для остальных — dict
    llm_run_id: int | None
    model: str
    latency_ms: int | None
    error: str | None = None


def _error_result(kind: str, model: str | None, code: str) -> AnalysisResult:
    """Унифицированный «ошибочный» результат — не загромождать analyzer'ы повторяющимся boilerplate."""
    return AnalysisResult(
        ok=False,
        kind=kind,
        data=None,
        llm_run_id=None,
        model=model or "",
        latency_ms=None,
        error=code,
    )


async def _load_vacancy_for_analysis(
    db: aiosqlite.Connection,
    vacancy_id: int,
    kind: str,
    model: str | None,
) -> tuple[dict, str] | AnalysisResult:
    """Достаём вакансию + чистое описание. Возвращает (v, desc) или AnalysisResult-ошибку.
    Анализаторы используют:
        loaded = await _load_vacancy_for_analysis(...)
        if isinstance(loaded, AnalysisResult): return loaded
        v, desc = loaded
    """
    v = await vacancies_repo.get_vacancy(db, vacancy_id)
    if not v:
        return _error_result(kind, model, "vacancy_not_found")
    desc = clean_description(v.get("description") or "")
    if not desc:
        return _error_result(kind, model, "empty_description")
    return v, desc


# ---------- helper: универсальный путь «promp → LLM → upsert в vacancy_analysis» ----------


async def _run_simple_analysis(
    db: aiosqlite.Connection,
    vacancy_id: int,
    kind: str,
    model: str,
    prompt_version: str,
    system_prompt: str,
    user_prompt: str,
) -> AnalysisResult:
    """Универсальный путь для «однообъектных» анализов (salary/company_kind/summary).
    Зовёт LLM, парсит JSON, логирует в llm_runs, upsert'ит в vacancy_analysis."""
    resp = await llm_client.generate(
        model=model,
        prompt=user_prompt,
        system=system_prompt,
        format_json=True,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    ok = parsed is not None and not resp.error
    run_id = await llm_repo.insert_run(
        db,
        task_kind=kind,
        target_kind="vacancy",
        target_id=str(vacancy_id),
        model=resp.model,
        prompt_version=prompt_version,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_raw=resp.text,
        parsed_json=parsed,
        ok=ok,
        error=resp.error,
        latency_ms=resp.latency_ms,
        prompt_tokens=resp.prompt_tokens,
        response_tokens=resp.response_tokens,
    )
    if ok:
        await llm_repo.upsert_analysis(db, vacancy_id, kind, parsed, llm_run_id=run_id)
    return AnalysisResult(
        ok=ok,
        kind=kind,
        data=parsed,
        llm_run_id=run_id,
        model=resp.model,
        latency_ms=resp.latency_ms,
        error=resp.error,
    )


# ---------- analyzers ----------


async def _analyzer_requirements(db, vacancy_id, model) -> AnalysisResult:
    """Делегируем существующей parse_one (она уже пишет vacancy_requirements)."""
    res = await parse_requirements_one(db, vacancy_id, model=model)
    return AnalysisResult(
        ok=bool(res.get("ok")),
        kind="requirements",
        data=res.get("items") or [],
        llm_run_id=res.get("llm_run_id"),
        model=res.get("model") or model or "",
        latency_ms=res.get("latency_ms"),
        error=res.get("error") or res.get("reason"),
    )


async def _analyzer_salary(db, vacancy_id, model) -> AnalysisResult:
    loaded = await _load_vacancy_for_analysis(db, vacancy_id, "salary", model)
    if isinstance(loaded, AnalysisResult):
        return loaded
    _, desc = loaded
    system = (
        "Ты извлекаешь зарплатные ожидания из текста IT-вакансии. "
        "Возвращай ТОЛЬКО валидный JSON без пояснений."
    )
    user = (
        "Извлеки зарплатную вилку из описания. Если в тексте нет конкретных цифр — верни null поля.\n"
        "Формат:\n"
        "{\n"
        '  "amount_from": 200000 | null,\n'
        '  "amount_to": 350000 | null,\n'
        '  "currency": "RUR" | "USD" | "EUR" | null,\n'
        '  "gross": true | false | null,\n'
        '  "period": "month" | "year" | "hour" | null,\n'
        '  "note": "цитата из текста где упомянута зарплата" | null\n'
        "}\n\n"
        "Описание:\n---\n" + desc + "\n---"
    )
    return await _run_simple_analysis(
        db,
        vacancy_id,
        kind="salary",
        model=model,
        prompt_version="salary_v1",
        system_prompt=system,
        user_prompt=user,
    )


async def _analyzer_company_kind(db, vacancy_id, model) -> AnalysisResult:
    loaded = await _load_vacancy_for_analysis(db, vacancy_id, "company_kind", model)
    if isinstance(loaded, AnalysisResult):
        return loaded
    v, desc = loaded
    name = v.get("name") or ""
    company = v.get("company_name") or ""
    system = "Ты классифицируешь работодателя по типу. Возвращай ТОЛЬКО валидный JSON без пояснений."
    user = (
        "По названию компании и описанию вакансии определи тип работодателя.\n"
        "Категории (выбери ОДНУ наиболее подходящую):\n"
        "- 'product' — продуктовая (создаёт собственный продукт/сервис)\n"
        "- 'outsource' — аутсорс/аутстаф/студия разработки\n"
        "- 'bank_fintech' — банк, страховая, финтех\n"
        "- 'ecommerce_retail' — e-commerce, retail, маркетплейс\n"
        "- 'gamedev' — игры\n"
        "- 'startup' — стартап (мало людей, ранняя стадия)\n"
        "- 'enterprise' — крупный энтерпрайз (телеком, нефтегаз, металлургия)\n"
        "- 'government' — государство, госкорпорация, бюджет\n"
        "- 'edu_science' — образование/наука\n"
        "- 'other' — не подходит ни одна\n\n"
        "Формат:\n"
        "{\n"
        '  "kind": "product",\n'
        '  "confidence": 0.85,\n'
        '  "reasoning": "1-2 предложения почему"\n'
        "}\n\n"
        f"Компания: {company}\nВакансия: {name}\n\nОписание:\n---\n{desc}\n---"
    )
    return await _run_simple_analysis(
        db,
        vacancy_id,
        kind="company_kind",
        model=model,
        prompt_version="company_kind_v1",
        system_prompt=system,
        user_prompt=user,
    )


async def _analyzer_summary(db, vacancy_id, model) -> AnalysisResult:
    loaded = await _load_vacancy_for_analysis(db, vacancy_id, "summary", model)
    if isinstance(loaded, AnalysisResult):
        return loaded
    _, desc = loaded
    system = (
        "Ты делаешь короткое резюме IT-вакансии для быстрого скана. "
        "Возвращай ТОЛЬКО валидный JSON без пояснений."
    )
    user = (
        "Сделай очень короткое резюме (1-2 предложения, ≤200 символов) — что за позиция, "
        "ключевой стек, формат, отличительная особенность если есть.\n\n"
        "Формат:\n"
        "{\n"
        '  "summary": "Senior Python+FastAPI бэкенд в финтехе, удалёнка, акцент на highload."\n'
        "}\n\n"
        "Описание:\n---\n" + desc + "\n---"
    )
    return await _run_simple_analysis(
        db,
        vacancy_id,
        kind="summary",
        model=model,
        prompt_version="summary_v1",
        system_prompt=system,
        user_prompt=user,
    )


async def _analyzer_match_essay(db, vacancy_id, model) -> AnalysisResult:
    """Эссе «почему мне подходит / не подходит» с учётом моего профиля.
    Использует profile.skills, profile.years_experience, salary_expected — даёт LLM-у мой контекст."""
    loaded = await _load_vacancy_for_analysis(db, vacancy_id, "match_essay", model)
    if isinstance(loaded, AnalysisResult):
        return loaded
    v, desc = loaded
    profile = await profile_repo.get_profile(db) or {}
    skills = profile.get("skills") or []
    years = profile.get("years_experience")
    title = profile.get("title") or ""
    salary_from = profile.get("salary_expected_from")

    profile_block = (
        f"Должность: {title}\n"
        f"Опыт: {years} лет\n"
        f"Зарплатные ожидания: от {salary_from} {profile.get('salary_currency') or ''}\n"
        f"Скиллы: {', '.join(skills[:30])}"
    )
    system = (
        "Ты карьерный консультант. Анализируешь подходит ли IT-вакансия конкретному кандидату. "
        "Возвращай ТОЛЬКО валидный JSON без пояснений."
    )
    user = (
        "Сравни требования вакансии с профилем кандидата. Сделай честный анализ:\n"
        "что СОВПАДАЕТ, что НЕ ХВАТАЕТ, и общая оценка подходимости.\n\n"
        "Формат:\n"
        "{\n"
        '  "score": 0..100,        // насколько подходит\n'
        '  "verdict": "match" | "stretch" | "skip",\n'
        '  "matches": ["конкретные совпадения, до 5 пунктов"],\n'
        '  "gaps": ["конкретные пробелы, до 5 пунктов"],\n'
        '  "reasoning": "1-3 предложения почему такой verdict"\n'
        "}\n\n"
        f"=== ПРОФИЛЬ КАНДИДАТА ===\n{profile_block}\n\n"
        f"=== ВАКАНСИЯ ===\nКомпания: {v.get('company_name') or '?'}\nДолжность: {v.get('name')}\n"
        f"ЗП в вакансии (₽): {v.get('salary_rub') or '?'}\n\n"
        f"Описание:\n---\n{desc}\n---"
    )
    return await _run_simple_analysis(
        db,
        vacancy_id,
        kind="match_essay",
        model=model,
        prompt_version="match_essay_v1",
        system_prompt=system,
        user_prompt=user,
    )


async def _analyzer_interview_prep(db, vacancy_id, model) -> AnalysisResult:
    """Подготовка к собесу: вероятные вопросы по требованиям вакансии.
    Подмешивает в контекст прошлые отклики на эту же компанию (если есть) — без RAG, через SQL."""
    loaded = await _load_vacancy_for_analysis(db, vacancy_id, "interview_prep", model)
    if isinstance(loaded, AnalysisResult):
        return loaded
    v, desc = loaded

    # история откликов на ту же компанию
    history_block = ""
    cid = v.get("company_id")
    if cid:
        cur = await db.execute(
            """
            SELECT v.name, n.last_state, n.last_modified
              FROM negotiations n
              JOIN vacancies v ON v.id = n.vacancy_id
             WHERE v.company_id = ? AND v.id != ?
          ORDER BY n.last_modified DESC
             LIMIT 5
            """,
            (cid, vacancy_id),
        )
        rows = await cur.fetchall()
        if rows:
            hl = [
                f"  - «{r['name']}» (last_state={r['last_state']}, {r['last_modified'][:10] if r['last_modified'] else '?'})"
                for r in rows
            ]
            history_block = "\n=== МОИ ПРОШЛЫЕ ОТКЛИКИ В ЭТУ КОМПАНИЮ ===\n" + "\n".join(hl) + "\n"

    system = (
        "Ты опытный IT-интервьюер. По описанию вакансии генерируешь вероятные вопросы "
        "и темы к собеседованию. Возвращай ТОЛЬКО валидный JSON без пояснений."
    )
    user = (
        "На основе требований и стека вакансии составь подготовку к собесу. "
        "Если есть история прошлых откликов в эту компанию — учти её (могут спросить о ней).\n\n"
        "Формат:\n"
        "{\n"
        '  "topics": ["главные темы которые надо повторить"],\n'
        '  "likely_questions": [\n'
        '    {"q": "вопрос", "why": "почему вероятен"}\n'
        "  ],   // до 10 вопросов, по убыванию вероятности\n"
        '  "code_tasks": ["типы задач которые могут дать", ...],  // до 5\n'
        '  "red_flags": ["что я должен спросить у работодателя", ...] // до 5\n'
        "}\n\n"
        f"=== ВАКАНСИЯ ===\nДолжность: {v.get('name')}\nКомпания: {v.get('company_name') or '?'}\n"
        f"{history_block}\n"
        f"Описание:\n---\n{desc}\n---"
    )
    return await _run_simple_analysis(
        db,
        vacancy_id,
        kind="interview_prep",
        model=model,
        prompt_version="interview_prep_v1",
        system_prompt=system,
        user_prompt=user,
    )


@dataclass
class Analyzer:
    kind: str
    label: str
    description: str
    default_enabled: bool
    fn: Callable[[aiosqlite.Connection, int, str], Awaitable[AnalysisResult]]


ANALYZERS: dict[str, Analyzer] = {
    "requirements": Analyzer(
        kind="requirements",
        label="Требования",
        description="must / nice / plus × stack / exp / soft / edu / other",
        default_enabled=True,
        fn=_analyzer_requirements,
    ),
    "salary": Analyzer(
        kind="salary",
        label="Зарплата из текста",
        description="Если HH не отдал salary в полях, ищем в описании",
        default_enabled=False,
        fn=_analyzer_salary,
    ),
    "company_kind": Analyzer(
        kind="company_kind",
        label="Тип компании",
        description="product / outsource / bank / ecommerce / gamedev / startup / enterprise / gov / edu / other",
        default_enabled=False,
        fn=_analyzer_company_kind,
    ),
    "summary": Analyzer(
        kind="summary",
        label="Резюме 1-2 предл.",
        description="Очень короткое описание для быстрого скана таблицы",
        default_enabled=False,
        fn=_analyzer_summary,
    ),
    "match_essay": Analyzer(
        kind="match_essay",
        label="Match-эссе",
        description="Подходит ли вакансия мне (score + matches/gaps + verdict) на основе profile.skills",
        default_enabled=False,
        fn=_analyzer_match_essay,
    ),
    "interview_prep": Analyzer(
        kind="interview_prep",
        label="Подготовка к собесу",
        description="Вероятные вопросы и темы к интервью + история откликов в эту компанию",
        default_enabled=False,
        fn=_analyzer_interview_prep,
    ),
}


async def analyze_one(
    db: aiosqlite.Connection,
    vacancy_id: int,
    kinds: list[str],
    model: str | None = None,
) -> list[AnalysisResult]:
    """Прогоняет выбранные анализаторы по одной вакансии. Каждый — независимо."""
    if model is None:
        model = await llm_settings.get_requirements_model(db)
    out: list[AnalysisResult] = []
    for kind in kinds:
        a = ANALYZERS.get(kind)
        if not a:
            out.append(
                AnalysisResult(
                    ok=False,
                    kind=kind,
                    data=None,
                    llm_run_id=None,
                    model=model,
                    latency_ms=None,
                    error="unknown_analyzer",
                )
            )
            continue
        try:
            res = await a.fn(db, vacancy_id, model)
        except Exception as e:
            log.warning("analyzer %s vid=%s failed: %s", kind, vacancy_id, e)
            res = AnalysisResult(
                ok=False,
                kind=kind,
                data=None,
                llm_run_id=None,
                model=model,
                latency_ms=None,
                error=str(e),
            )
        out.append(res)
    return out


# ---------- runtime: какие анализаторы включены глобально ----------


_ENABLED_KEY = "llm.analyzers.enabled"


async def get_enabled_analyzers(db: aiosqlite.Connection) -> list[str]:
    """Список включённых анализаторов (используется cron-джобом и UI по умолчанию).
    Хранится в cookie_store как JSON-массив. Если не задано — берём default_enabled."""
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (_ENABLED_KEY,))
    row = await cur.fetchone()
    if row and row[0]:
        try:
            stored = json.loads(row[0])
            if isinstance(stored, list):
                # фильтруем по реально существующим анализаторам
                return [k for k in stored if k in ANALYZERS]
        except Exception:
            pass
    return [k for k, a in ANALYZERS.items() if a.default_enabled]


async def set_enabled_analyzers(db: aiosqlite.Connection, kinds: list[str]) -> None:
    valid = [k for k in kinds if k in ANALYZERS]
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (_ENABLED_KEY, json.dumps(valid)),
    )
    await db.commit()
