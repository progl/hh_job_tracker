"""Тонкий клиент к Ollama (/api/generate).

Принципиально без зависимости от HHClient/scheduler/etc — это отдельный пайплайн.
Возвращает структуру с временем, токенами и сырым ответом — её сохраняем в llm_runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    ok: bool
    text: str  # сырой текст ответа модели
    parsed: Any | None  # распарсенный JSON (если format=json) или None
    error: str | None
    model: str
    latency_ms: int
    prompt_tokens: int | None
    response_tokens: int | None


@dataclass
class EmbedResponse:
    ok: bool
    vectors: list[list[float]]  # по одному вектору на входной текст
    error: str | None
    model: str
    latency_ms: int


async def embed(
    texts: list[str],
    *,
    model: str,
    base_url: str | None = None,
    timeout: float | None = None,
) -> EmbedResponse:
    """Эмбеддинги через Ollama /api/embed (батч). Возвращает по вектору на каждый текст."""
    url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/") + "/api/embed"
    to = settings.LLM_TIMEOUT_SECONDS if timeout is None else timeout
    body = {"model": model, "input": texts}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=to) as cli:
            r = await cli.post(url, json=body)
    except httpx.HTTPError as e:
        dt = int((time.monotonic() - t0) * 1000)
        log.warning("embed: network error model=%s in %sms: %s", model, dt, e)
        return EmbedResponse(ok=False, vectors=[], error=f"network: {e}", model=model, latency_ms=dt)

    dt = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return EmbedResponse(
            ok=False, vectors=[], error=f"http {r.status_code}: {r.text[:200]}", model=model, latency_ms=dt
        )
    try:
        data = r.json()
    except ValueError as e:
        return EmbedResponse(ok=False, vectors=[], error=f"non-json: {e}", model=model, latency_ms=dt)

    vectors = data.get("embeddings") or []
    return EmbedResponse(
        ok=bool(vectors),
        vectors=vectors,
        error=None if vectors else "no embeddings in response",
        model=data.get("model") or model,
        latency_ms=dt,
    )


async def generate(
    *,
    model: str,
    prompt: str,
    system: str | None = None,
    format_json: bool = True,
    temperature: float | None = None,
    timeout: float | None = None,
    base_url: str | None = None,
    think: bool | None = None,
) -> LLMResponse:
    """Один вызов Ollama /api/generate.
    think=False отключает reasoning у qwen3/deepseek-r1 (иначе в format=json они часто возвращают {}).
    По умолчанию для моделей с явным reasoning (qwen3*, deepseek-r1*) отключаем.
    """
    m = model.lower()
    is_thinking_model = m.startswith(("qwen3", "deepseek-r1"))
    if think is None:
        # для thinking-моделей по умолчанию выключаем (в format=json мешает)
        think = False if is_thinking_model else None

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
            "num_ctx": settings.LLM_NUM_CTX,
        },
    }
    # шлём think только если модель его поддерживает — иначе Ollama 400
    if is_thinking_model and think is not None:
        body["think"] = think
    if system:
        body["system"] = system
    if format_json:
        body["format"] = "json"

    url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/") + "/api/generate"
    to = settings.LLM_TIMEOUT_SECONDS if timeout is None else timeout

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=to) as cli:
            r = await cli.post(url, json=body)
    except httpx.HTTPError as e:
        dt = int((time.monotonic() - t0) * 1000)
        log.warning("llm: network error model=%s in %sms: %s", model, dt, e)
        return LLMResponse(
            ok=False,
            text="",
            parsed=None,
            error=f"network: {e}",
            model=model,
            latency_ms=dt,
            prompt_tokens=None,
            response_tokens=None,
        )

    dt = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return LLMResponse(
            ok=False,
            text=r.text,
            parsed=None,
            error=f"http {r.status_code}: {r.text[:200]}",
            model=model,
            latency_ms=dt,
            prompt_tokens=None,
            response_tokens=None,
        )

    try:
        data = r.json()
    except ValueError as e:
        return LLMResponse(
            ok=False,
            text=r.text,
            parsed=None,
            error=f"non-json envelope: {e}",
            model=model,
            latency_ms=dt,
            prompt_tokens=None,
            response_tokens=None,
        )

    text = data.get("response", "")
    parsed: Any | None = None
    parse_err: str | None = None
    if format_json:
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError as e:
            parse_err = f"json parse: {e}"

    return LLMResponse(
        ok=parsed is not None if format_json else True,
        text=text,
        parsed=parsed,
        error=parse_err,
        model=model,
        latency_ms=dt,
        prompt_tokens=data.get("prompt_eval_count"),
        response_tokens=data.get("eval_count"),
    )
