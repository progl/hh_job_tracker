"""LLM-задача: извлечение требований из описания вакансии.

Полный путь:
  1. Берём description вакансии, чистим от HTML, обрезаем до LLM_MAX_DESCRIPTION_CHARS.
  2. Зовём ollama (одна или несколько моделей подряд).
  3. Каждый прогон логируем в llm_runs (что дали / что вернула).
  4. Из лучшего ответа сохраняем requirements в vacancy_requirements.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

import aiosqlite

from app.config import settings
from app.db import llm_repo, vacancies_repo
from app.llm import client as llm_client
from app.llm import prompts
from app.llm import settings as llm_settings

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_description(html: str) -> str:
    if not html:
        return ""
    txt = _TAG_RE.sub(" ", html)
    txt = unescape(txt)
    txt = _WS_RE.sub(" ", txt).strip()
    if len(txt) > settings.LLM_MAX_DESCRIPTION_CHARS:
        txt = txt[: settings.LLM_MAX_DESCRIPTION_CHARS] + " […truncated]"
    return txt


async def parse_one(
    db: aiosqlite.Connection,
    vacancy_id: int,
    *,
    model: str | None = None,
    save_requirements: bool = True,
) -> dict[str, Any]:
    """Один прогон по одной вакансии одной моделью. Всегда пишет в llm_runs.
    Если save_requirements=True и LLM вернула валидный JSON — пишет в vacancy_requirements.
    """
    if model is None:
        model = await llm_settings.get_requirements_model(db)
    v = await vacancies_repo.get_vacancy(db, vacancy_id)
    if not v:
        return {"ok": False, "reason": "vacancy_not_found", "vacancy_id": vacancy_id}

    desc = clean_description(v.get("description") or "")
    if not desc:
        return {"ok": False, "reason": "empty_description", "vacancy_id": vacancy_id}

    version, system, user = prompts.requirements_prompt_v1(desc)
    resp = await llm_client.generate(model=model, prompt=user, system=system, format_json=True)

    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    items: list[dict] = []
    if parsed:
        raw = parsed.get("requirements")
        if isinstance(raw, list):
            for it in raw:
                if not isinstance(it, dict):
                    continue
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                items.append(
                    {
                        "kind": (it.get("kind") or "must").lower(),
                        "category": (it.get("category") or "other").lower(),
                        "text": text,
                    }
                )

    run_id = await llm_repo.insert_run(
        db,
        task_kind="requirements",
        target_kind="vacancy",
        target_id=str(vacancy_id),
        model=resp.model,
        prompt_version=version,
        system_prompt=system,
        user_prompt=user,
        response_raw=resp.text,
        parsed_json=parsed,
        ok=bool(items),
        error=resp.error,
        latency_ms=resp.latency_ms,
        prompt_tokens=resp.prompt_tokens,
        response_tokens=resp.response_tokens,
    )

    inserted = 0
    if save_requirements and items:
        inserted = await llm_repo.replace_requirements(
            db,
            vacancy_id,
            items,
            source="llm",
            llm_run_id=run_id,
        )

    return {
        "ok": bool(items),
        "vacancy_id": vacancy_id,
        "model": resp.model,
        "latency_ms": resp.latency_ms,
        "prompt_tokens": resp.prompt_tokens,
        "response_tokens": resp.response_tokens,
        "items": items,
        "inserted": inserted,
        "llm_run_id": run_id,
        "error": resp.error,
    }


async def parse_one_multi_model(
    db: aiosqlite.Connection,
    vacancy_id: int,
    models: list[str],
) -> list[dict[str, Any]]:
    """Прогоняет одну вакансию через несколько моделей подряд (для сравнения качества/скорости).
    Сохраняет requirements только из последнего успешного прогона."""
    out = []
    last_success: int | None = None
    for i, m in enumerate(models):
        res = await parse_one(db, vacancy_id, model=m, save_requirements=False)
        out.append(res)
        if res.get("ok"):
            last_success = i
    if last_success is not None:
        best = out[last_success]
        await llm_repo.replace_requirements(
            db,
            vacancy_id,
            best["items"],
            source="llm",
            llm_run_id=best["llm_run_id"],
        )
        best["inserted"] = len(best["items"])
    return out
