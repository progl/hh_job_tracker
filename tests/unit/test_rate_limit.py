import time

import pytest

from app.clients import rate_limit
from app.config import settings


@pytest.mark.asyncio
async def test_first_wait_is_almost_instant(monkeypatch):
    # уменьшим задержки до близких к нулю
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1000)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1000)
    rl = rate_limit.RateLimiter()
    t0 = time.monotonic()
    await rl.wait()
    assert (time.monotonic() - t0) < 0.5
    assert rl._req_in_minute == 1
    assert rl._req_since_rest == 1


@pytest.mark.asyncio
async def test_wait_increments_counters(monkeypatch):
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1000)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1000)
    rl = rate_limit.RateLimiter()
    for _ in range(3):
        await rl.wait()
    assert rl._req_in_minute == 3
    assert rl._req_since_rest == 3


@pytest.mark.asyncio
async def test_minute_window_resets(monkeypatch):
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1000)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1000)
    rl = rate_limit.RateLimiter()
    rl._req_in_minute = 100
    rl._minute_start = time.monotonic() - 120  # 2 минуты назад
    await rl.wait()
    # _req_in_minute должен сброситься до 1 (после инкремента)
    assert rl._req_in_minute == 1


@pytest.mark.asyncio
async def test_minute_limit_triggers_sleep(monkeypatch):
    # настроим маленькие задержки, малый лимит
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1000)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1)

    rl = rate_limit.RateLimiter()
    # симулируем: уже сделали 1 запрос в текущую минуту, но окно почти истекло
    rl._req_in_minute = 5
    rl._minute_start = time.monotonic() - 59.5  # 0.5s до конца окна

    # подменим asyncio.sleep на мгновенный, фиксируя что вызван
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rate_limit.asyncio, "sleep", fake_sleep)
    await rl.wait()
    # должен спать хотя бы один раз
    assert len(slept) >= 1


@pytest.mark.asyncio
async def test_rest_triggers_sleep(monkeypatch):
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1)
    monkeypatch.setattr(settings, "HH_REST_DURATION_SEC", 0)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1000)

    rl = rate_limit.RateLimiter()
    rl._req_since_rest = 5  # выше порога

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rate_limit.asyncio, "sleep", fake_sleep)
    await rl.wait()
    assert len(slept) >= 1
    # _req_since_rest сброшен и потом увеличен до 1
    assert rl._req_since_rest == 1


@pytest.mark.asyncio
async def test_min_delay_enforced(monkeypatch):
    """Если elapsed < delay, должен await sleep(delay - elapsed)."""
    monkeypatch.setattr(settings, "HH_MIN_DELAY_SEC", 1.0)
    monkeypatch.setattr(settings, "HH_MAX_DELAY_SEC", 1.0)
    monkeypatch.setattr(settings, "HH_REST_AFTER_REQUESTS", 1000)
    monkeypatch.setattr(settings, "HH_REQUESTS_PER_MIN_LIMIT", 1000)
    rl = rate_limit.RateLimiter()
    rl._last_req = time.monotonic() - 0.1  # 0.1s назад

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rate_limit.asyncio, "sleep", fake_sleep)
    await rl.wait()
    assert len(slept) == 1
    assert 0.5 < slept[0] <= 1.0
