"""Тесты уведомлений: пороги, Telegram-канал, dispatch по каналам."""

from __future__ import annotations

import pytest

from app import notify
from app.config import settings


@pytest.mark.asyncio
async def test_threshold_default_and_set(tmp_db):
    assert await notify.get_match_threshold(tmp_db) == 75
    await notify.set_match_threshold(tmp_db, 60)
    assert await notify.get_match_threshold(tmp_db) == 60
    # кламп 0..100
    await notify.set_match_threshold(tmp_db, 150)
    assert await notify.get_match_threshold(tmp_db) == 100


@pytest.mark.asyncio
async def test_telegram_toggle(tmp_db):
    assert await notify.is_telegram_enabled(tmp_db) is False
    await notify.set_telegram_enabled(tmp_db, True)
    assert await notify.is_telegram_enabled(tmp_db) is True


@pytest.mark.asyncio
async def test_send_telegram_noop_without_token(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
    called = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            called["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return None

    monkeypatch.setattr(notify.httpx, "AsyncClient", _Client)
    await notify.send_telegram("hi")
    assert called["n"] == 0  # без токена httpx даже не создаётся


@pytest.mark.asyncio
async def test_send_telegram_posts(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "TOKEN123")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "42")
    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json

    monkeypatch.setattr(notify.httpx, "AsyncClient", _Client)
    await notify.send_telegram("привет")
    assert "TOKEN123" in captured["url"] and captured["url"].endswith("/sendMessage")
    assert captured["json"] == {"chat_id": "42", "text": "привет"}


@pytest.mark.asyncio
async def test_dispatch_routes_to_enabled_channels(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    sent = {"mac": [], "tg": []}

    async def fake_send(title, message, subtitle=None):
        sent["mac"].append((title, message))

    async def fake_tg(text):
        sent["tg"].append(text)

    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(notify, "send_telegram", fake_tg)

    # ничего не включено → молчим
    await notify.dispatch(tmp_db, "T", "M")
    assert sent == {"mac": [], "tg": []}

    await notify.set_enabled(tmp_db, True)
    await notify.set_telegram_enabled(tmp_db, True)
    await notify.dispatch(tmp_db, "Заголовок", "Текст")
    assert sent["mac"] == [("Заголовок", "Текст")]
    assert sent["tg"] and "Заголовок" in sent["tg"][0] and "Текст" in sent["tg"][0]


@pytest.mark.asyncio
async def test_any_enabled(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    assert await notify.any_enabled(tmp_db) is False
    await notify.set_enabled(tmp_db, True)
    assert await notify.any_enabled(tmp_db) is True


@pytest.mark.asyncio
async def test_events_default_and_set(tmp_db):
    ev = await notify.get_events(tmp_db)
    assert "vacancies" in ev and "job_errors" in ev
    assert "job_done" not in ev  # по умолчанию выключено
    await notify.set_events(tmp_db, ["job_done", "bogus"])  # bogus отфильтруется
    ev2 = await notify.get_events(tmp_db)
    assert ev2 == {"job_done"}


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_event(tmp_db, monkeypatch):
    sent = []

    async def fake_send(title, message, subtitle=None):
        sent.append(title)

    monkeypatch.setattr(notify, "send", fake_send)
    await notify.set_enabled(tmp_db, True)
    await notify.set_events(tmp_db, ["vacancies"])  # job_done выключено

    await notify.dispatch(tmp_db, "X", "Y", event="job_done")
    assert sent == []  # категория выключена → молчим
    await notify.dispatch(tmp_db, "X", "Y", event="vacancies")
    assert sent == ["X"]
