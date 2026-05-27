"""Тесты HHClient — мокаем httpx.AsyncClient через AsyncMock, БД-логи отключаем."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.clients.hh import (
    AntibotChallengeError,
    HHClient,
    SessionExpiredError,
    VacancyUnavailableError,
)


def _mk_response(status: int, *, text: str = "", headers: dict | None = None, url: str = "https://hh.ru/p"):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = text
    r.content = text.encode("utf-8")
    r.headers = headers or {}
    r.url = url
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def _silence_logs(monkeypatch):
    """log_request пишет в БД через asyncio.create_task — в тестах БД нет, делаем no-op."""
    import app.clients.hh as hh_mod

    def _noop_log(**kwargs):
        return None

    monkeypatch.setattr(hh_mod, "log_request", _noop_log, raising=False)
    # log_request импортируется внутри get_page по месту
    import app.db.logs_repo as logs_repo

    monkeypatch.setattr(logs_repo, "log_request", _noop_log)


@pytest.fixture(autouse=True)
def _fast_rate_limit(monkeypatch):
    """Чтобы RateLimiter.wait не задерживал тесты."""

    async def _noop(self):
        return None

    from app.clients.rate_limit import RateLimiter

    monkeypatch.setattr(RateLimiter, "wait", _noop)


@pytest.mark.asyncio
async def test_start_creates_client_and_idempotent():
    cli = HHClient()
    assert cli._client is None
    await cli.start(initial_cookies=[])
    assert cli._client is not None
    # повторный start — без эффекта
    inner = cli._client
    await cli.start()
    assert cli._client is inner
    await cli.close()
    assert cli._client is None


@pytest.mark.asyncio
async def test_status_before_start():
    cli = HHClient()
    s = cli.status
    assert s["started"] is False
    assert s["paused_now"] is False
    assert s["cookie_count"] == 0
    assert s["base_url"].startswith("http")


@pytest.mark.asyncio
async def test_status_after_start_with_cookies():
    cli = HHClient()
    await cli.start(initial_cookies=[{"name": "a", "value": "b"}])
    s = cli.status
    assert s["started"] is True
    assert s["cookie_count"] >= 1
    await cli.close()


def test_unpause_resets_until():
    cli = HHClient()
    cli._paused_until = time.monotonic() + 1000
    cli._challenge_count = 7
    res = cli.unpause()
    assert cli._paused_until == 0.0
    assert res["was_paused_until"] > 0
    assert res["challenge_count"] == 7


def test_client_property_raises_when_not_started():
    cli = HHClient()
    with pytest.raises(RuntimeError):
        _ = cli.client


def test_smart_referer_for_vacancy_without_prior():
    cli = HHClient()
    ref = cli._smart_referer("/vacancy/123")
    assert ref.endswith("/")


def test_smart_referer_for_vacancy_uses_last_url_if_search():
    cli = HHClient()
    cli._last_url = "https://hh.ru/search/vacancy?text=python"
    ref = cli._smart_referer("/vacancy/1")
    assert "search/vacancy" in ref


def test_smart_referer_for_search():
    cli = HHClient()
    ref = cli._smart_referer("/search/vacancy")
    assert ref.endswith("/")


def test_smart_referer_for_resumes():
    cli = HHClient()
    ref = cli._smart_referer("/applicant/resumes")
    assert "negotiations" in ref


def test_smart_referer_for_resume_detail():
    cli = HHClient()
    ref = cli._smart_referer("/resume/abc")
    assert "resumes" in ref


def test_smart_referer_default_uses_last_url():
    cli = HHClient()
    cli._last_url = "https://hh.ru/somepage"
    ref = cli._smart_referer("/unknown/path")
    assert ref == "https://hh.ru/somepage"


@pytest.mark.asyncio
async def test_get_page_returns_text_on_200():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    fake = _mk_response(200, text="<html>OK</html>", url="https://hh.ru/x")
    cli._client.get = AsyncMock(return_value=fake)
    html = await cli.get_page("/x")
    assert html == "<html>OK</html>"
    assert cli._consecutive_403 == 0
    assert cli._last_url == "https://hh.ru/x"
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_paused_raises_immediately():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    cli._paused_until = time.monotonic() + 100
    cli._client.get = AsyncMock()
    with pytest.raises(AntibotChallengeError):
        await cli.get_page("/x")
    cli._client.get.assert_not_called()
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_redirect_to_login_raises_session_expired():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    fake = _mk_response(302, headers={"location": "https://auth.hh.ru/account/login"})
    cli._client.get = AsyncMock(return_value=fake)
    with pytest.raises(SessionExpiredError):
        await cli.get_page("/foo")
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_redirect_non_login_returns_empty():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    fake = _mk_response(302, headers={"location": "/somewhere"})
    cli._client.get = AsyncMock(return_value=fake)
    html = await cli.get_page("/foo")
    assert html == ""
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_403_hidden_vacancy_raises_unavailable():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    body = "<html>Вам недоступна эта вакансия</html>"
    fake = _mk_response(403, text=body)
    cli._client.get = AsyncMock(return_value=fake)
    with pytest.raises(VacancyUnavailableError):
        await cli.get_page("/vacancy/1")
    # 403 hidden НЕ инкрементит счётчик anti-bot
    assert cli._consecutive_403 == 0
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_403_individual_does_not_pause():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    fake = _mk_response(403, text="tiny")
    cli._client.get = AsyncMock(return_value=fake)
    with pytest.raises(AntibotChallengeError):
        await cli.get_page("/x")
    assert cli._consecutive_403 == 1
    assert cli._challenge_count == 1
    assert cli._paused_until == 0.0
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_403_triple_pauses_client():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    fake = _mk_response(403, text="tiny")
    cli._client.get = AsyncMock(return_value=fake)
    for _ in range(3):
        with pytest.raises(AntibotChallengeError):
            await cli.get_page("/x")
    assert cli._consecutive_403 == 3
    assert cli._paused_until > time.monotonic()
    s = cli.status
    assert s["paused_now"] is True
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_network_error_reraised():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    cli._client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(httpx.RequestError):
        await cli.get_page("/x")
    await cli.close()


@pytest.mark.asyncio
async def test_get_page_resets_consecutive_403_on_success():
    cli = HHClient()
    await cli.start(initial_cookies=[])
    cli._consecutive_403 = 2
    fake = _mk_response(200, text="ok", url="https://hh.ru/ok")
    cli._client.get = AsyncMock(return_value=fake)
    await cli.get_page("/ok")
    assert cli._consecutive_403 == 0
    await cli.close()


@pytest.mark.asyncio
async def test_sleep_anti_burst_does_not_raise():
    cli = HHClient()
    # реально не спим — патчим asyncio.sleep
    with patch("app.clients.hh.asyncio.sleep", new=AsyncMock(return_value=None)) as sl:
        await cli.sleep_anti_burst()
        sl.assert_awaited_once_with(1)
