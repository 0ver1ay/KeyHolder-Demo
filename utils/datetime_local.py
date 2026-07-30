"""Локальное время для отображения и фильтров (Москва, UTC+3).

PostgreSQL хранит TIMESTAMPTZ в UTC; в UI показываем и фильтруем по локальному
календарному дню приложения.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Часовой пояс шкафа/админки. При необходимости можно вынести в config.cfg.
APP_TZ = ZoneInfo("Europe/Moscow")


def now_local() -> datetime:
    return datetime.now(APP_TZ)


def local_day_start(year: int, month: int, day: int) -> datetime:
    """Полночь выбранного календарного дня в локальном часовом поясе."""
    return datetime(year, month, day, 0, 0, 0, tzinfo=APP_TZ)


def local_day_end_exclusive(day_start: datetime) -> datetime:
    """Начало следующего локального дня (для фильтра ``< until``)."""
    local = to_local(day_start)
    if local is None:
        raise ValueError("day_start is required")
    return local_day_start(local.year, local.month, local.day) + timedelta(days=1)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ)


def format_local_datetime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if dt is None:
        return ""
    try:
        return to_local(dt).strftime(fmt)
    except Exception:
        return str(dt)


def format_local_date(dt: datetime | None, fmt: str = "%d.%m.%Y") -> str:
    if dt is None:
        return ""
    try:
        return to_local(dt).strftime(fmt)
    except Exception:
        return str(dt)
