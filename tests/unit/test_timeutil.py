"""Тесты конвертации UTC → локальная таймзона для отображения."""

from __future__ import annotations

from app.config import settings
from app.timeutil import to_local


def test_utc_to_moscow(monkeypatch):
    monkeypatch.setattr(settings, "TIMEZONE", "Europe/Moscow")
    # Москва = UTC+3 (без DST)
    assert to_local("2026-05-29 08:00:00") == "2026-05-29 11:00"
    assert to_local("2026-05-29 08:00:00", "%H:%M") == "11:00"
    # ISO с 'T' тоже принимаем
    assert to_local("2026-05-29T08:00:00") == "2026-05-29 11:00"


def test_empty_and_garbage():
    assert to_local("") == ""
    assert to_local(None) == ""
    assert to_local("никогда") == "никогда"  # непарсируемое — без падения


def test_other_timezone(monkeypatch):
    monkeypatch.setattr(settings, "TIMEZONE", "UTC")
    assert to_local("2026-05-29 08:00:00", "%H:%M") == "08:00"
