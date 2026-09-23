"""Единая временная модель проекта.

Внутренние даты работают в часовом поясе Черновцов. Это важно для GPS:
перевозчик отдаёт локальное время, а часы процесса внутри минимального
Docker-образа могут быть UTC.

Старые naive-значения (тесты, модельные сценарии, старые callers) считаем
киевским локальным временем. aware-значения сначала переводим в Europe/Kyiv,
чтобы арифметика board_time - snapshot_at никогда не зависела от timezone
контейнера.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Europe/Kyiv")


def now_kyiv() -> datetime:
    """Текущее время в часовом поясе приложения."""
    return datetime.now(APP_TIMEZONE)


def as_kyiv(value: Optional[datetime]) -> datetime:
    """Привести дату к aware Europe/Kyiv; None означает текущее время."""
    if value is None:
        return now_kyiv()
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=APP_TIMEZONE)
    return value.astimezone(APP_TIMEZONE)


def format_kyiv(value: Optional[datetime] = None) -> str:
    """Стабильный формат времени API/логов без offset, в локальном времени."""
    return as_kyiv(value).strftime("%Y-%m-%d %H:%M:%S")
