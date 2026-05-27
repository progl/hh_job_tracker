"""macOS-уведомления через `osascript display notification`.

Запускается subprocess в фоне — не блокирует scheduler.
Включается через cookie_store key 'notifications.enabled'.

Telegram-bot/другие каналы не делаем — отложили (требует bot-токен, refresh, etc).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

log = logging.getLogger(__name__)

_KEY_ENABLED = "notifications.enabled"


async def is_enabled(db: aiosqlite.Connection) -> bool:
    cur = await db.execute("SELECT value FROM cookie_store WHERE key = ?", (_KEY_ENABLED,))
    row = await cur.fetchone()
    return bool(row and row[0] == "1")


async def set_enabled(db: aiosqlite.Connection, on: bool) -> None:
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (_KEY_ENABLED, "1" if on else "0"),
    )
    await db.commit()


def _escape_apple_string(s: str) -> str:
    """Экранируем для AppleScript: " → \\\\\\", \\ → \\\\."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def send(title: str, message: str, subtitle: str | None = None) -> None:
    """Отправить macOS-уведомление. Безопасно (не падает на не-mac, не блокирует)."""
    if sys.platform != "darwin":
        log.debug("notify: skipped (not macOS)")
        return
    parts = [f'display notification "{_escape_apple_string(message)}"', f'with title "{_escape_apple_string(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_escape_apple_string(subtitle)}"')
    script = " ".join(parts)
    try:
        # subprocess в фоне — не ждём
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # не await proc.wait() — fire-and-forget
        asyncio.create_task(proc.wait())
    except Exception as e:
        log.warning("notify failed: %s", e)


async def maybe_send(db: aiosqlite.Connection, title: str, message: str, subtitle: str | None = None) -> None:
    """Проверяет включено ли — если да, шлёт."""
    if await is_enabled(db):
        await send(title, message, subtitle)
