"""Тесты на app/llm/client.py — мок httpx.AsyncClient.

Покрывает: успешный JSON-режим, авто-think для qwen3, отсутствие think для других,
явный think override, http 500, network error, невалидный JSON в response."""

from __future__ import annotations

import httpx
import pytest

from app.llm import client as llm_client


def _make_response(status_code: int = 200, json_body=None, text: str = "") -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status_code, json=json_body)
    return httpx.Response(status_code, text=text)


class _FakeAsyncClient:
    """Контекстный менеджер, имитирующий httpx.AsyncClient."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.last_call: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.last_call = {"url": url, "json": json}
        if self.exc:
            raise self.exc
        return self.response


@pytest.mark.asyncio
async def test_generate_success_json(monkeypatch):
    fake = _FakeAsyncClient(
        _make_response(
            200,
            json_body={
                "response": '{"foo": "bar"}',
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )
    )
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    r = await llm_client.generate(model="llama3.1:8b", prompt="test")
    assert r.ok is True
    assert r.parsed == {"foo": "bar"}
    assert r.text == '{"foo": "bar"}'
    assert r.prompt_tokens == 10
    assert r.response_tokens == 5
    assert r.error is None
    assert r.latency_ms >= 0


@pytest.mark.asyncio
async def test_generate_qwen3_auto_sets_think_false(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": '{"ok": 1}'}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="qwen3:14b", prompt="x")
    assert fake.last_call["json"]["think"] is False


@pytest.mark.asyncio
async def test_generate_deepseek_r1_auto_sets_think_false(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": '{"ok": 1}'}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="deepseek-r1:7b", prompt="x")
    assert fake.last_call["json"]["think"] is False


@pytest.mark.asyncio
async def test_generate_non_thinking_model_no_think_field(monkeypatch):
    """Llama/qwen2.5 не поддерживают think — поле НЕ должно посылаться (иначе Ollama 400)."""
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": '{"ok": 1}'}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="llama3.1:8b", prompt="x")
    assert "think" not in fake.last_call["json"]
    await llm_client.generate(model="qwen2.5:14b", prompt="x")
    assert "think" not in fake.last_call["json"]


@pytest.mark.asyncio
async def test_generate_think_override_for_qwen3(monkeypatch):
    """Явный think=True для qwen3 — посылается как есть."""
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": '{"ok": 1}'}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="qwen3:14b", prompt="x", think=True)
    assert fake.last_call["json"]["think"] is True


@pytest.mark.asyncio
async def test_generate_passes_system_and_format(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": "{}"}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="qwen2.5:14b", prompt="user-p", system="sys-p")
    body = fake.last_call["json"]
    assert body["system"] == "sys-p"
    assert body["prompt"] == "user-p"
    assert body["format"] == "json"
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_generate_format_json_false_skips_format(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": "plain text"}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    r = await llm_client.generate(model="llama3.1:8b", prompt="x", format_json=False)
    assert "format" not in fake.last_call["json"]
    assert r.text == "plain text"
    assert r.parsed is None
    assert r.ok is True


@pytest.mark.asyncio
async def test_generate_http_500(monkeypatch):
    fake = _FakeAsyncClient(_make_response(500, text="internal error"))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    r = await llm_client.generate(model="qwen2.5:14b", prompt="x")
    assert r.ok is False
    assert "http 500" in r.error


@pytest.mark.asyncio
async def test_generate_network_error(monkeypatch):
    fake = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    r = await llm_client.generate(model="qwen2.5:14b", prompt="x")
    assert r.ok is False
    assert r.error.startswith("network:")


@pytest.mark.asyncio
async def test_generate_invalid_json_in_response(monkeypatch):
    """Модель отдала текст не-JSON в format=json режиме → error, ok=False."""
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": "not a json {"}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    r = await llm_client.generate(model="qwen2.5:14b", prompt="x")
    assert r.ok is False
    assert r.parsed is None
    assert "json parse" in r.error
    assert r.text == "not a json {"


@pytest.mark.asyncio
async def test_generate_temperature_default_from_settings(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": "{}"}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    monkeypatch.setattr(llm_client.settings, "LLM_TEMPERATURE", 0.0)
    await llm_client.generate(model="qwen2.5:14b", prompt="x")
    assert fake.last_call["json"]["options"]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_generate_temperature_override(monkeypatch):
    fake = _FakeAsyncClient(_make_response(200, json_body={"response": "{}"}))
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda timeout: fake)
    await llm_client.generate(model="qwen2.5:14b", prompt="x", temperature=0.7)
    assert fake.last_call["json"]["options"]["temperature"] == 0.7
