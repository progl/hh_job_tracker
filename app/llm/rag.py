"""RAG-пайплайн: эмбеддинги вакансий + векторный поиск (sqlite-vec) + Q&A.

Опционально: требует extra `rag` (sqlite-vec). Если расширение не установлено/не грузится —
`is_available()` вернёт False, и все RAG-фичи аккуратно выключаются (приложение работает как раньше).

Включить: uv sync --extra rag
"""

from __future__ import annotations

import hashlib
import json
import logging

import aiosqlite

from app.config import settings

log = logging.getLogger(__name__)

_available: bool | None = None  # кеш результата детекта


def is_available() -> bool:
    """True, если sqlite-vec установлен и грузится в этой сборке sqlite. Кешируется."""
    global _available
    if _available is not None:
        return _available
    try:
        import sqlite3

        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("SELECT vec_version()")
        finally:
            conn.close()
        _available = True
    except Exception as e:
        log.info("RAG недоступен (sqlite-vec не загрузился): %s", e)
        _available = False
    return _available


async def load_vec(conn) -> None:
    """Загружает расширение sqlite-vec в aiosqlite-соединение (грузить нужно в каждое,
    которое обращается к vec0-таблице). Идемпотентно — повторный вызов на том же conn = no-op."""
    import sqlite_vec

    try:
        await conn.execute("SELECT vec_version()")
        return  # уже загружено в это соединение
    except Exception:
        pass
    await conn.enable_load_extension(True)
    await conn.load_extension(sqlite_vec.loadable_path())
    await conn.enable_load_extension(False)


# ---------- построение текста для эмбеддинга ----------


def build_embed_text(v: dict) -> str:
    """Текст вакансии для эмбеддинга: название + компания + стек + чистое описание."""
    from app.llm.tasks.requirements import clean_description

    parts = [v.get("name") or "", v.get("company_name") or ""]
    stack = v.get("parsed_stack")
    if isinstance(stack, str):
        try:
            stack = json.loads(stack)
        except Exception:
            stack = []
    if stack:
        parts.append("Стек: " + ", ".join(str(s) for s in stack))
    parts.append(clean_description(v.get("description") or ""))
    return "\n".join(p for p in parts if p).strip()


def source_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _score(distance: float) -> float:
    """Косинусная дистанция → similarity для показа (0..1)."""
    return max(0.0, min(1.0, 1.0 - distance))


# ---------- индексация ----------


async def embed_vacancy(conn: aiosqlite.Connection, vacancy_id: int, model: str | None = None) -> dict:
    """Эмбедит одну вакансию и сохраняет вектор. Логирует в llm_runs (task_kind='embed')."""
    from app.db import embeddings_repo, llm_repo, vacancies_repo
    from app.llm import client as llm_client
    from app.llm import settings as llm_settings

    v = await vacancies_repo.get_vacancy(conn, vacancy_id)
    if not v:
        return {"ok": False, "reason": "not_found"}
    text = build_embed_text(v)
    if len(text) < 30:
        return {"ok": False, "reason": "empty"}
    model = model or await llm_settings.get_embed_model(conn)
    resp = await llm_client.embed([text], model=model)
    vec = resp.vectors[0] if resp.vectors else None
    run_id = await llm_repo.insert_run(
        conn,
        task_kind="embed",
        target_kind="vacancy",
        target_id=str(vacancy_id),
        model=resp.model,
        prompt_version="embed_v1",
        system_prompt=None,
        user_prompt=text[:2000],
        response_raw=f"dim={len(vec)}" if vec else "",
        parsed_json=None,
        ok=resp.ok,
        error=resp.error,
        latency_ms=resp.latency_ms,
        prompt_tokens=None,
        response_tokens=None,
    )
    if not resp.ok or not vec:
        return {"ok": False, "reason": resp.error or "no_vector", "llm_run_id": run_id}
    await embeddings_repo.ensure_ready(conn)
    await embeddings_repo.upsert(conn, vacancy_id, resp.model, vec, source_hash(text))
    return {"ok": True, "dim": len(vec), "llm_run_id": run_id}


# ---------- поиск ----------


async def similar(
    conn: aiosqlite.Connection, vacancy_id: int, k: int | None = None
) -> list[tuple[int, float]]:
    """Похожие вакансии по вектору данной. [(vacancy_id, score)]."""
    from app.db import embeddings_repo

    k = k or settings.RAG_TOP_K
    await embeddings_repo.ensure_ready(conn)
    blob = await embeddings_repo.get_vector_blob(conn, vacancy_id)
    if blob is None:
        return []
    rows = await embeddings_repo.knn(conn, blob, k + 1)  # сама вакансия попадёт с distance≈0
    return [(vid, _score(dist)) for vid, dist in rows if vid != vacancy_id][:k]


async def semantic_search(
    conn: aiosqlite.Connection, query: str, k: int | None = None
) -> list[tuple[int, float]]:
    """Семантический поиск по корпусу. [(vacancy_id, score)]."""
    from sqlite_vec import serialize_float32

    from app.db import embeddings_repo
    from app.llm import client as llm_client
    from app.llm import settings as llm_settings

    k = k or settings.RAG_TOP_K
    model = await llm_settings.get_embed_model(conn)
    resp = await llm_client.embed([query], model=model)
    if not resp.ok or not resp.vectors:
        return []
    await embeddings_repo.ensure_ready(conn)
    rows = await embeddings_repo.knn(conn, serialize_float32(resp.vectors[0]), k)
    return [(vid, _score(dist)) for vid, dist in rows]


async def ask(conn: aiosqlite.Connection, query: str, k: int | None = None) -> dict:
    """Полный RAG: retrieval (semantic_search) + generation (LLM-ответ с ссылками на вакансии)."""
    from app.db import llm_repo, vacancies_repo
    from app.llm import client as llm_client
    from app.llm import settings as llm_settings
    from app.llm.tasks.requirements import clean_description

    k = k or settings.RAG_TOP_K
    hits = await semantic_search(conn, query, k)
    if not hits:
        return {"ok": False, "reason": "no_results", "answer": None, "sources": []}

    ctx_blocks: list[str] = []
    sources: list[dict] = []
    for vid, score in hits:
        v = await vacancies_repo.get_vacancy(conn, vid)
        if not v:
            continue
        desc = clean_description(v.get("description") or "")[:800]
        ctx_blocks.append(f"[вакансия #{vid}] {v.get('name')} — {v.get('company_name') or '?'}\n{desc}")
        sources.append(
            {
                "vacancy_id": vid,
                "name": v.get("name"),
                "company": v.get("company_name"),
                "score": score,
            }
        )

    system = (
        "Ты помощник по поиску работы. Отвечай ТОЛЬКО на основе предоставленных вакансий, "
        "ссылайся на их номера в формате [#id]. Если в вакансиях нет ответа — скажи об этом прямо. "
        "Не используй markdown."
    )
    user = (
        f"Вопрос: {query}\n\n"
        f"=== НАЙДЕННЫЕ ВАКАНСИИ ===\n" + "\n\n".join(ctx_blocks) + "\n\n"
        "Ответь кратко и по делу, ссылаясь на номера вакансий [#id]."
    )
    model = await llm_settings.get_requirements_model(conn)
    resp = await llm_client.generate(model=model, prompt=user, system=system, format_json=False)
    run_id = await llm_repo.insert_run(
        conn,
        task_kind="rag_answer",
        target_kind="query",
        target_id=None,
        model=resp.model,
        prompt_version="rag_answer_v1",
        system_prompt=system,
        user_prompt=user,
        response_raw=resp.text,
        parsed_json=None,
        ok=resp.ok,
        error=resp.error,
        latency_ms=resp.latency_ms,
        prompt_tokens=resp.prompt_tokens,
        response_tokens=resp.response_tokens,
    )
    return {
        "ok": resp.ok,
        "answer": resp.text,
        "sources": sources,
        "llm_run_id": run_id,
        "error": resp.error,
    }
