"""Конвертация хранимых UTC-времён в локальную таймзону (settings.TIMEZONE) для показа.

В БД всё пишется через SQLite CURRENT_TIMESTAMP — это UTC «YYYY-MM-DD HH:MM:SS».
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def to_local(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC-строка/datetime → строка в settings.TIMEZONE. Пустое/непарсируемое — без падения."""
    if not value:
        return ""
    s = str(value).replace("T", " ").strip()[:19]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return dt.astimezone(ZoneInfo(settings.TIMEZONE)).strftime(fmt)
    except (ValueError, TypeError):
        return str(value)
