import asyncio
import random
import time

from app.config import settings


class RateLimiter:
    """Имитирует поведение пользователя в браузере: задержки + jitter, минутный лимит,
    периодический "rest" как при чтении страницы."""

    def __init__(self) -> None:
        self._last_req: float = 0.0
        self._req_in_minute: int = 0
        self._minute_start: float = time.monotonic()
        self._req_since_rest: int = 0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._minute_start > 60:
                self._req_in_minute = 0
                self._minute_start = now
            if self._req_in_minute >= settings.HH_REQUESTS_PER_MIN_LIMIT:
                wait = 60 - (now - self._minute_start) + random.uniform(0.5, 2.0)
                await asyncio.sleep(max(wait, 0))
                self._req_in_minute = 0
                self._minute_start = time.monotonic()
            if self._req_since_rest >= settings.HH_REST_AFTER_REQUESTS:
                rest = settings.HH_REST_DURATION_SEC + random.uniform(-5, 10)
                await asyncio.sleep(max(rest, 1))
                self._req_since_rest = 0
            delay = random.uniform(settings.HH_MIN_DELAY_SEC, settings.HH_MAX_DELAY_SEC)
            elapsed = time.monotonic() - self._last_req
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_req = time.monotonic()
            self._req_in_minute += 1
            self._req_since_rest += 1
