import json
from http.cookies import SimpleCookie

import aiosqlite
import httpx


def parse_cookie_header(cookie_str: str) -> list[dict[str, str]]:
    if not cookie_str:
        return []
    sc = SimpleCookie()
    sc.load(cookie_str)
    return [{"name": k, "value": v.value} for k, v in sc.items()]


def apply_cookies_to_client(client: httpx.AsyncClient, cookies: list[dict[str, str]]) -> None:
    for c in cookies:
        client.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain") or ".hh.ru",
            path=c.get("path") or "/",
        )


def dump_jar(client: httpx.AsyncClient) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for cookie in client.cookies.jar:
        items.append({
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
        })
    return items


def jar_size(client: httpx.AsyncClient) -> int:
    return sum(1 for _ in client.cookies.jar)


async def save_jar(db: aiosqlite.Connection, client: httpx.AsyncClient) -> None:
    payload = json.dumps(dump_jar(client), ensure_ascii=False)
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES('jar', ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (payload,),
    )
    await db.commit()


_ANCHOR_COOKIES = ("hhtoken", "hhuid", "_xsrf")


def _cookie_value(items: list[dict[str, str]], name: str) -> str | None:
    for c in items:
        if c.get("name") == name:
            return c.get("value")
    return None


async def load_jar(db: aiosqlite.Connection, env_cookie_header: str | None = None) -> list[dict[str, str]] | None:
    """Загружает jar из БД. Если в .env переданы куки и стабильные значения
    (hhtoken/hhuid/_xsrf) отличаются — сбрасывает БД-jar и возвращает .env (пользователь обновил)."""
    cur = await db.execute("SELECT value FROM cookie_store WHERE key='jar'")
    row = await cur.fetchone()
    db_jar = json.loads(row[0]) if row else None

    if env_cookie_header:
        env_jar = parse_cookie_header(env_cookie_header)
        if db_jar:
            diff = []
            for name in _ANCHOR_COOKIES:
                ev, dv = _cookie_value(env_jar, name), _cookie_value(db_jar, name)
                if ev and dv and ev != dv:
                    diff.append(name)
            if diff:
                # пользователь обновил .env — выбрасываем устаревший БД-jar
                await db.execute("DELETE FROM cookie_store WHERE key='jar'")
                await db.commit()
                import logging as _log
                _log.getLogger(__name__).info(
                    "cookies: .env differs from db jar on %s — using .env (db jar dropped)", diff
                )
                return env_jar
        elif env_jar:
            return env_jar
    return db_jar
