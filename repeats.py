"""Правила повторов задач — чистая логика без Telegram, HTTP и БД.

Два вида повторов:
* по календарю (daily / weekly / monthly / yearly / weekdays / monthly_nth_weekday) —
  CronTrigger планировщика, время берётся из due_at;
* «через N дней после выполнения» (after_<N>d) — следующий срок отсчитывается
  от момента выполнения, расписания нет, только разовая джоба на due_at.

due_at повторяющейся задачи — это ближайший ещё не закрытый раз. «Готово» и
«Пропустить» переносят его на следующий раз (next_due), задача остаётся активной.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

UTC = ZoneInfo("UTC")
_AFTER_RE = re.compile(r"after_(\d{1,3})d")
_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def after_days(repeat: str | None) -> int | None:
    """'after_3d' -> 3; всё остальное (в т.ч. after_0d) -> None."""
    m = _AFTER_RE.fullmatch(repeat or "")
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def is_one_shot(repeat: str | None) -> bool:
    """Планируется одной date-джобой на due_at (без cron): разовые и after_<N>d."""
    return repeat in (None, "", "none") or after_days(repeat) is not None


def nth_weekday_cron(local_due: datetime, tz: ZoneInfo) -> CronTrigger:
    """N-й день недели месяца, например 'второе воскресенье' — APScheduler поддерживает
    day='2nd sun' нативно в CronTrigger."""
    nth = min((local_due.day - 1) // 7 + 1, 5)
    day_expr = f"{nth}{['st', 'nd', 'rd', 'th', 'th'][nth - 1]} {_WEEKDAY_CODES[local_due.weekday()]}"
    return CronTrigger(day=day_expr, hour=local_due.hour, minute=local_due.minute, timezone=tz)


def repeat_trigger(repeat: str, local_due: datetime, tz: ZoneInfo) -> CronTrigger:
    """Расписание повтора по календарю. Неизвестный код — ежедневно (как было всегда)."""
    h, m = local_due.hour, local_due.minute
    if repeat == "weekly":
        return CronTrigger(day_of_week=local_due.weekday(), hour=h, minute=m, timezone=tz)
    if repeat == "monthly":
        return CronTrigger(day=local_due.day, hour=h, minute=m, timezone=tz)
    if repeat == "yearly":
        return CronTrigger(month=local_due.month, day=local_due.day, hour=h, minute=m, timezone=tz)
    if repeat == "weekdays":
        return CronTrigger(day_of_week="mon-fri", hour=h, minute=m, timezone=tz)
    if repeat == "monthly_nth_weekday":
        return nth_weekday_cron(local_due, tz)
    return CronTrigger(hour=h, minute=m, timezone=tz)


def next_due(repeat: str, due_utc: datetime, now_utc: datetime, tz: ZoneInfo,
             completed: bool = True) -> datetime | None:
    """Следующий срок повторяющейся задачи (UTC) после того, как текущий раз
    (due_utc) закрыт: completed=True — выполнен, False — пропущен.

    Результат всегда позже и due_utc, и now_utc: закрыть сразу несколько
    пропущенных раз одним нажатием — нормально, следующий будет в будущем.
    after_<N>d: выполнено — N дней от сегодня (по местному календарю), в то же
    местное время; пропущено — шагами по N дней от прежнего срока.
    None — у задачи нет повтора."""
    if repeat in (None, "", "none"):
        return None
    local_due = due_utc.astimezone(tz)
    days = after_days(repeat)
    if days is not None:
        if completed:
            day = now_utc.astimezone(tz).date() + timedelta(days=days)
            nxt = datetime.combine(day, local_due.time().replace(tzinfo=None)).replace(tzinfo=tz)
        else:
            nxt = local_due + timedelta(days=days)
        while nxt.astimezone(UTC) <= max(due_utc, now_utc):
            nxt += timedelta(days=days)
        return nxt.astimezone(UTC)
    after = max(due_utc, now_utc) + timedelta(seconds=1)
    fire = repeat_trigger(repeat, local_due, tz).get_next_fire_time(None, after.astimezone(tz))
    return fire.astimezone(UTC) if fire else None
