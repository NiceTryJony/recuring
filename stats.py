"""Чистые функции для статистики и стриков (без БД и без Telegram) — легко тестируются.

Определение стрика (метрика привычки, а не «вовремя»): сколько дней подряд в
каждый день была хотя бы одна выполненная задача (task_history, event='done').
Если сегодня выполненных ещё нет, серия не обрывается — считаем от вчера: у
пользователя ещё есть остаток дня, чтобы её продолжить."""

from datetime import date, timedelta
from typing import Iterable

EVENTS = ("created", "done", "deleted")


def compute_streak(done_days: Iterable[date], today: date) -> int:
    days = set(done_days)
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def totals(rows: Iterable[dict]) -> dict[str, int]:
    """rows — результат db.get_history_stats: [{'day': date, 'event': str, 'cnt': int}]."""
    result = {e: 0 for e in EVENTS}
    for r in rows:
        if r["event"] in result:
            result[r["event"]] += int(r["cnt"])
    return result


def daily_series(rows: Iterable[dict], event: str, days: int, today: date) -> list[tuple[date, int]]:
    """Ровно `days` точек (старые -> новые, последняя = today), дни без событий = 0."""
    by_day: dict[date, int] = {}
    for r in rows:
        if r["event"] == event:
            by_day[r["day"]] = by_day.get(r["day"], 0) + int(r["cnt"])
    return [(d, by_day.get(d, 0)) for d in (today - timedelta(days=i) for i in range(days - 1, -1, -1))]
