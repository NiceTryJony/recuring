"""Применение настроек владельца со всеми побочными эффектами (сброс кэша,
перепланирование джоб). Одна точка для команд бота (/timezone, /lang, /quiet,
/morning) и страницы настроек дашборда — та получает эти функции через
dashboard.set_settings_hooks (импортировать tgbot дашборду нельзя)."""

from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db

from .owner import invalidate_settings, owner_tz
from .scheduling import schedule_daily_summary, schedule_morning_summary, schedule_task, unschedule_morning_summary

LANGUAGES = ("ru", "en", "pl")


def valid_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):   # ValueError — «../x» и прочие кривые ключи
        return False
    return True


async def apply_timezone(owner_id: int, owner_type: str, tz_name: str) -> bool:
    """False — пояс не существует, ничего не меняли."""
    if not valid_timezone(tz_name):
        return False
    await db.set_owner_timezone(owner_id, owner_type, tz_name)
    invalidate_settings(owner_id, owner_type)
    new_tz = ZoneInfo(tz_name)
    # Джоба вечерней сводки теперь одна на user_id (объединяет личку + все
    # группы, см. send_daily_summary) и живёт в поясе ЛИЧНЫХ настроек
    # пользователя — смена пояса ГРУППЫ (owner_type == "chat") на неё не влияет.
    if owner_type == "user":
        schedule_daily_summary(owner_id, new_tz)
        morning = await db.get_morning_summary_settings(owner_id)
        if morning and morning["morning_summary_enabled"]:
            schedule_morning_summary(owner_id, new_tz, morning["morning_summary_hour"], morning["morning_summary_minute"])
    # cron-джобы повторяющихся задач привязаны к поясу на момент создания — без
    # перепланирования они продолжали бы срабатывать по старому времени.
    for task in await db.get_tasks(owner_id, owner_type):
        schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], new_tz,
                      remind_until_done=bool(task.get("remind_until_done")))
    return True


async def apply_language(owner_id: int, owner_type: str, lang: str) -> bool:
    if lang not in LANGUAGES:
        return False
    await db.set_owner_language(owner_id, owner_type, lang)
    invalidate_settings(owner_id, owner_type)
    return True


async def apply_quiet(owner_id: int, owner_type: str, quiet: tuple[dtime, dtime] | None):
    """None выключает тихий час."""
    start, end = quiet if quiet else (None, None)
    await db.set_owner_quiet_hours(owner_id, owner_type, start, end)
    invalidate_settings(owner_id, owner_type)


async def apply_morning(user_id: int, hour_minute: tuple[int, int] | None):
    """Утренняя сводка — всегда личная настройка человека. None выключает."""
    if hour_minute is None:
        await db.set_morning_summary(user_id, enabled=False)
        unschedule_morning_summary(user_id)
        return
    hour, minute = hour_minute
    await db.set_morning_summary(user_id, enabled=True, hour=hour, minute=minute)
    schedule_morning_summary(user_id, await owner_tz(user_id, "user"), hour, minute)
