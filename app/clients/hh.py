import asyncio
import logging
import time

import httpx

from app.clients.cookies import apply_cookies_to_client, jar_size, parse_cookie_header
from app.clients.headers import headers_document
from app.clients.rate_limit import RateLimiter
from app.config import settings

log = logging.getLogger(__name__)


class SessionExpiredError(Exception):
    pass


class AntibotChallengeError(Exception):
    pass


class VacancyUnavailableError(Exception):
    """403 с HTML «Вам недоступна эта вакансия» — НЕ anti-bot, просто вакансия закрыта/снята."""
    pass


class HHClient:
    def __init__(self) -> None:
        self.base_url: str = settings.HH_BASE_URL
        self.rl = RateLimiter()
        self._client: httpx.AsyncClient | None = None
        self._last_url: str = self.base_url + "/"
        self._paused_until: float = 0.0
        self._challenge_count: int = 0
        self._consecutive_403: int = 0
        self._pause_threshold: int = 3  # пауза только после 3 подряд 403

    async def start(self, initial_cookies: list[dict[str, str]] | None = None) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            http2=True,
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
        )
        cookies = initial_cookies if initial_cookies is not None else parse_cookie_header(settings.HH_COOKIE)
        if cookies:
            apply_cookies_to_client(self._client, cookies)

    def unpause(self) -> dict:
        """Сбрасывает paused_until — клиент сможет делать запросы немедленно.
        НЕ влияет на challenge_count (для статистики)."""
        was = self._paused_until
        self._paused_until = 0.0
        return {"was_paused_until": was, "now": time.monotonic(), "challenge_count": self._challenge_count}

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HHClient not started")
        return self._client

    @property
    def status(self) -> dict:
        return {
            "started": self._client is not None,
            "paused_until": self._paused_until,
            "paused_now": time.monotonic() < self._paused_until,
            "challenge_count": self._challenge_count,
            "base_url": self.base_url,
            "last_url": self._last_url,
            "cookie_count": jar_size(self._client) if self._client else 0,
        }

    def _smart_referer(self, path: str) -> str:
        """Реалистичный referer для каждого типа страницы — как ходит реальный пользователь."""
        base = self.base_url
        if path.startswith("/vacancy/"):
            # если уже был запрос поиска — этот last_url и есть правильный referer
            if "/search/vacancy" in self._last_url or "/applicant/" in self._last_url:
                return self._last_url
            return f"{base}/"
        if path.startswith("/search/vacancy"):
            return f"{base}/"
        if path.startswith("/applicant/negotiations"):
            return f"{base}/"
        if path.startswith("/applicant/resumes"):
            return f"{base}/applicant/negotiations"
        if path.startswith("/resume/"):
            return f"{base}/applicant/resumes"
        return self._last_url or f"{base}/"

    async def get_page(self, path: str, params: dict | None = None) -> str:
        from app import events
        from app.db.logs_repo import log_request
        if time.monotonic() < self._paused_until:
            wait_s = int(self._paused_until - time.monotonic())
            events.emit("paused", f"клиент на паузе ещё {wait_s}с (anti-bot)", {"wait": wait_s, "path": path})
            log_request(path=path, params=params, error=f"client paused for {wait_s}s", kind="skipped_paused")
            raise AntibotChallengeError(f"client paused, retry after {wait_s}s")
        await self.rl.wait()
        events.emit("request", f"GET {path}", {"path": path, "params": params})
        ref = self._smart_referer(path)
        headers = headers_document(referer=ref)
        t0 = time.monotonic()
        try:
            r = await self.client.get(path, params=params, headers=headers)
        except httpx.RequestError as e:
            log.warning("network error on %s: %s", path, e)
            events.emit("error", f"network: {e}", {"path": path})
            log_request(path=path, params=params, referer=ref, error=f"network: {e}", kind="network")
            raise
        dt = int((time.monotonic() - t0) * 1000)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            log.info("redirect from %s -> %s", path, loc)
            events.emit("response", f"↪ {r.status_code} {path} → {loc[:80]}", {"path": path, "status": r.status_code, "duration_ms": dt, "redirect": loc})
            log_request(path=path, params=params, referer=ref, status=r.status_code, duration_ms=dt,
                        redirect_to=loc, kind="redirect")
            if "/account/login" in loc or "auth.hh.ru" in loc:
                events.emit("error", "сессия протухла, обнови HH_COOKIE в .env", {"path": path})
                raise SessionExpiredError(f"redirected to login: {loc}")
            return ""
        if r.status_code == 403:
            # различаем "вакансия скрыта" vs "anti-bot"
            body = r.text
            if (
                "Вам недоступна эта вакансия" in body
                or "HH-PageLayout-Description" in body
                or "Поиск работы" in body and len(body) > 5000
            ):
                # обычная закрытая/удалённая вакансия — НЕ anti-bot
                events.emit("info", f"вакансия скрыта: {path}", {"path": path})
                log_request(path=path, params=params, referer=ref, status=r.status_code, duration_ms=dt,
                            size_bytes=len(r.content), error="vacancy hidden by HH (not anti-bot)", kind="hidden")
                raise VacancyUnavailableError(f"vacancy unavailable: {path}")
            # настоящий anti-bot 403 — обычно маленькое тело
            self._challenge_count += 1
            self._consecutive_403 += 1
            if self._consecutive_403 >= self._pause_threshold:
                pause = 600 if self._consecutive_403 == self._pause_threshold else 1800
                self._paused_until = time.monotonic() + pause
                events.emit("paused", f"⚠ {self._consecutive_403} подряд anti-bot → пауза {pause//60}м", {"status": r.status_code, "path": path})
                log_request(path=path, params=params, referer=ref, status=r.status_code, duration_ms=dt,
                            error=f"anti-bot {r.status_code} #{self._consecutive_403}, pause {pause//60}m", kind="antibot")
                raise AntibotChallengeError(f"got {r.status_code} from hh.ru (#{self._consecutive_403}) — pausing {pause//60}m")
            # одиночный 403 — вероятно вакансия снята/недоступна, не ставим паузу
            events.emit("warn", f"403 на {path} (одиночный, #{self._consecutive_403}/{self._pause_threshold} до паузы)", {"status": r.status_code, "path": path})
            log_request(path=path, params=params, referer=ref, status=r.status_code, duration_ms=dt,
                        error=f"403 individual #{self._consecutive_403}/{self._pause_threshold}", kind="unavailable")
            raise AntibotChallengeError(f"403 on {path} (probably removed)")
        self._consecutive_403 = 0
        events.emit("response", f"{r.status_code} {path} ({dt}ms, {len(r.content)//1024}KB)", {"path": path, "status": r.status_code, "duration_ms": dt, "size": len(r.content)})
        log_request(path=path, params=params, referer=ref, status=r.status_code, duration_ms=dt,
                    size_bytes=len(r.content), kind="ok")
        r.raise_for_status()
        self._last_url = str(r.url)
        return r.text

    async def sleep_anti_burst(self) -> None:
        await asyncio.sleep(1)
