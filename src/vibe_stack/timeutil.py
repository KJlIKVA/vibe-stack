from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 или RFC 2822 → aware datetime (UTC). None, если не разобрать."""
    if not value:
        return None
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def local_date(dt: datetime, tz: str) -> date:
    return local(dt, tz).date()


def week_start(d: date) -> date:
    """Понедельник недели, в которую попадает d."""
    return d - timedelta(days=d.weekday())


def report_day(now: datetime, tz: str) -> date:
    """День, за который отчитывается запуск по расписанию (решение 73): GitHub задерживает запуски на часы, поэтому
    сводка на 23:45, вышедшая в 04:11, и итоги воскресенья, вышедшие в понедельник в 01:39, — за прошедший день."""
    return local_date(now - timedelta(hours=12), tz)


def week_sunday(d: date) -> date:
    """Воскресенье недели, в которую попадает d: по нему запоминаем, что итоги этой недели уже вышли."""
    return week_start(d) + timedelta(days=6)


def age_days(dt: datetime | None, now: datetime) -> float | None:
    return (now - dt).total_seconds() / 86400 if dt else None
