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
async def test_send_telegram_returns_message_id(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")

    class _Resp:
        def json(self):
            return {"ok": True, "result": {"message_id": 77}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(notify.httpx, "AsyncClient", _Client)
    mid = await notify.send_telegram_to("1", "hi")
    assert mid == 77


@pytest.mark.asyncio
async def test_update_jobs_status_sends_then_edits(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    await notify.set_telegram_enabled(tmp_db, True)
    await notify.set_events(tmp_db, ["job_done"])
    from app.db import job_runs_repo

    rid = await job_runs_repo.start("fx_refresh")
    await job_runs_repo.finish(rid, "ok")

    sent, edited = [], []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(text)
        return 555

    async def fake_edit(mid, text):
        edited.append((mid, text))
        return True

    monkeypatch.setattr(notify, "send_telegram_to", fake_send)
    monkeypatch.setattr(notify, "edit_telegram", fake_edit)

    # первый раз — нет сохранённого id → шлём новое и сохраняем
    await notify.update_jobs_status(tmp_db)
    assert sent and "Последние задачи" in sent[0]
    assert await notify._get(tmp_db, notify._KEY_STATUS_MSG) == "555"

    # второй раз — id есть → редактируем, новое не шлём
    sent.clear()
    await notify.update_jobs_status(tmp_db)
    assert edited and edited[-1][0] == 555
    assert sent == []


@pytest.mark.asyncio
async def test_update_jobs_status_skipped_when_telegram_off(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")  # не настроен
    called = []

    async def fake_send(*a, **k):
        called.append(1)
        return 1

    monkeypatch.setattr(notify, "send_telegram_to", fake_send)
    await notify.update_jobs_status(tmp_db)
    assert called == []


@pytest.mark.asyncio
async def test_any_enabled(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    assert await notify.any_enabled(tmp_db) is False
    await notify.set_enabled(tmp_db, True)
    assert await notify.any_enabled(tmp_db) is True


@pytest.mark.asyncio
async def test_digest_hour_default_and_clamp(tmp_db):
    assert await notify.get_digest_hour(tmp_db) == 9
    await notify.set_digest_hour(tmp_db, 20)
    assert await notify.get_digest_hour(tmp_db) == 20
    await notify.set_digest_hour(tmp_db, 99)
    assert await notify.get_digest_hour(tmp_db) == 23


@pytest.mark.asyncio
async def test_run_daily_digest_hour_and_guard(tmp_db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    await notify.set_telegram_enabled(tmp_db, True)
    await notify.set_events(tmp_db, ["digest"])

    sent: list = []

    async def fake_dispatch(db, title, message, subtitle=None, event="general"):
        sent.append(event)

    monkeypatch.setattr(notify, "dispatch", fake_dispatch)
    cur_hour = datetime.now(ZoneInfo("Europe/Moscow")).hour

    # не тот час → молчит
    await notify.set_digest_hour(tmp_db, (cur_hour + 1) % 24)
    res = await notify.run_daily_digest(tmp_db)
    assert res == {"sent": False, "reason": "not_hour"}

    # час совпал → шлёт
    await notify.set_digest_hour(tmp_db, cur_hour)
    res = await notify.run_daily_digest(tmp_db)
    assert res["sent"] is True and sent == ["digest"]

    # повторно в тот же день → already_sent
    res = await notify.run_daily_digest(tmp_db)
    assert res == {"sent": False, "reason": "already_sent"}


@pytest.mark.asyncio
async def test_events_default_and_set(tmp_db):
    ev = await notify.get_events(tmp_db)
    assert "vacancies" in ev and "job_errors" in ev
    assert "job_done" not in ev  # по умолчанию выключено
    await notify.set_events(tmp_db, ["job_done", "bogus"])  # bogus отфильтруется
    ev2 = await notify.get_events(tmp_db)
    assert ev2 == {"job_done"}


@pytest.mark.asyncio
async def test_handle_start_shows_chat_id_and_hint(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")  # не настроен → подсказка
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)
    await notify._handle_command(tmp_db, 12345, "/start")
    assert sent and sent[0][0] == 12345
    assert "chat_id: 12345" in sent[0][1]
    assert "TELEGRAM_CHAT_ID=12345" in sent[0][1]


@pytest.mark.asyncio
async def test_handle_help_and_status(tmp_db, monkeypatch):
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)
    await notify._handle_command(tmp_db, 1, "/help")
    assert "/status" in sent[-1]
    # команда с @упоминанием бота тоже распознаётся
    await notify._handle_command(tmp_db, 1, "/status@my_bot")
    assert "Scheduler" in sent[-1] and "Уведомления" in sent[-1]


@pytest.mark.asyncio
async def test_sensitive_command_blocked_for_non_owner(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)
    await notify._handle_command(tmp_db, 999, "/run fx_refresh")  # чужой чат
    assert "только владельцу" in sent[-1]


@pytest.mark.asyncio
async def test_vacancies_command_owner(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    await tmp_db.execute(
        "INSERT INTO vacancies(id, name, company_name, description) VALUES (1, 'Senior Python', 'Acme', ?)",
        ("d" * 200,),
    )
    await tmp_db.commit()
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)
    await notify._handle_command(tmp_db, 111, "/vacancies")
    assert "Senior Python" in sent[-1]
    assert "hh.ru/vacancy/1" in sent[-1]


@pytest.mark.asyncio
async def test_run_command_owner(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    from app import scheduler as sched

    async def fake_run_now(job_id):
        return {"ok": True, "started": job_id}

    monkeypatch.setattr(sched, "run_now", fake_run_now)
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append((text, reply_markup))

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)

    # без аргумента — клавиатура с кнопкой на каждую джобу
    await notify._handle_command(tmp_db, 111, "/run")
    text, markup = sent[-1]
    assert "Какую задачу" in text
    btns = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "run:fx_refresh" in btns
    # с валидным job_id — запуск
    await notify._handle_command(tmp_db, 111, "/run fx_refresh")
    assert "запущено" in sent[-1][0]
    # неизвестный job_id
    await notify._handle_command(tmp_db, 111, "/run bogus")
    assert "Неизвестная" in sent[-1][0]


@pytest.mark.asyncio
async def test_find_command(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    await tmp_db.execute("INSERT INTO vacancies(id, name, company_name) VALUES (1, 'Senior Python', 'Acme')")
    await tmp_db.commit()
    from app.llm import rag

    monkeypatch.setattr(rag, "is_available", lambda: True)

    async def fake_search(db, query, limit=8):
        return [(1, 0.9)]

    monkeypatch.setattr(rag, "semantic_search", fake_search)
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)

    await notify._handle_command(tmp_db, 111, "/find python remote")
    assert "Senior Python" in sent[-1] and "hh.ru/vacancy/1" in sent[-1]
    # пустой запрос — подсказка
    await notify._handle_command(tmp_db, 111, "/find")
    assert "Укажи запрос" in sent[-1]


@pytest.mark.asyncio
async def test_callback_backfill_and_index(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    from app import scheduler as sched

    ran: list = []

    async def fake_run_now(job_id):
        ran.append(job_id)
        return {"ok": True}

    monkeypatch.setattr(sched, "run_now", fake_run_now)
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)

    await notify._handle_callback(tmp_db, 111, "cmd:backfill", "cq1")
    await notify._handle_callback(tmp_db, 111, "cmd:index", "cq2")
    assert ran == ["backfill_descriptions", "embed_vacancies"]


@pytest.mark.asyncio
async def test_handle_unknown_command_silent(tmp_db, monkeypatch):
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)
    await notify._handle_command(tmp_db, 1, "/foobar")
    assert sent == []


@pytest.mark.asyncio
async def test_callbacks_keyboard_and_owner_gate(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "111")
    from app import scheduler as sched

    async def fake_run_now(job_id):
        return {"ok": True}

    monkeypatch.setattr(sched, "run_now", fake_run_now)
    sent: list = []

    async def fake_to(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(notify, "send_telegram_to", fake_to)

    # cmd:status — открыт даже не-владельцу
    await notify._handle_callback(tmp_db, 999, "cmd:status", "cq1")
    assert "Scheduler" in sent[-1][1]
    # cmd:jobs владельцу — приходит клавиатура с кнопками run:
    await notify._handle_callback(tmp_db, 111, "cmd:jobs", "cq2")
    btns = [b["callback_data"] for row in sent[-1][2]["inline_keyboard"] for b in row]
    assert "run:fx_refresh" in btns
    # run:<id> владельцу — запуск
    await notify._handle_callback(tmp_db, 111, "run:fx_refresh", "cq3")
    assert "запущено" in sent[-1][1]
    # run:<id> из чужого чата — отказ
    await notify._handle_callback(tmp_db, 999, "run:fx_refresh", "cq4")
    assert "только владельцу" in sent[-1][1]


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
