"""Планировщик: напоминания, повторы, тихий час, утренняя и вечерняя сводки."""

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
from date_parser import is_quiet_time, next_quiet_end
from i18n import t

from . import app
from .app import bot, logger
from .constants import DEFAULT_TZ, NAG_INTERVAL_MINUTES, NAG_MAX_HOURS, REMIND_DELTAS
from .formatting import _current_streak
from .keyboards import task_kb
from .owner import owner_lang, owner_quiet, owner_tz, to_local, to_utc

# ---------- планировщик ----------

def _safe_remove_job(job_id: str):
    try:
        app.scheduler.remove_job(job_id)
    except JobLookupError:
        pass

def _remove_task_jobs(task_id: int):
    """Снимает ВСЕ джобы задачи: due, remind_<id>_<код>, отложенные тихим часом и snooze.
    (Раньше удалялся несуществующий id 'remind_<id>' — напоминания переживали done/редактирование.)"""
    exact = {f"due_{task_id}", f"quiet_due_{task_id}", f"quiet_remind_{task_id}", f"snooze_{task_id}"}
    remind_prefix = f"remind_{task_id}_"
    for job in app.scheduler.get_jobs():
        if job.id in exact or job.id.startswith(remind_prefix):
            _safe_remove_job(job.id)

def _nth_weekday_cron(local_due: datetime, tz: ZoneInfo) -> CronTrigger:
    """N-й день недели месяца, например 'второе воскресенье' — APScheduler поддерживает
    day='2nd sun' нативно в CronTrigger."""
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    nth = (local_due.day - 1) // 7 + 1
    nth = min(nth, 5)
    day_expr = f"{nth}{['st','nd','rd','th','th'][min(nth-1,4)]} {weekday_codes[local_due.weekday()]}"
    return CronTrigger(day=day_expr, hour=local_due.hour, minute=local_due.minute, timezone=tz)

def schedule_task(task_id: int, owner_id: int, owner_type: str, due_at_utc: datetime, repeat: str, remind: str,
                  tz: ZoneInfo, remind_until_done: bool = False):
    now_utc = datetime.now(ZoneInfo("UTC"))

    remind_codes = [c for c in (remind or "").split(",") if c in REMIND_DELTAS]
    for code in remind_codes:
        remind_time = due_at_utc - REMIND_DELTAS[code]
        if remind_time > now_utc:
            app.scheduler.add_job(
                send_reminder, "date", run_date=remind_time,
                args=[owner_id, owner_type, task_id], id=f"remind_{task_id}_{code}", replace_existing=True
            )

    local_due = to_local(due_at_utc, tz)
    if repeat == "none" and remind_until_done:
        # Периодическая джоба: срабатывает в due_at и далее каждые NAG_INTERVAL_MINUTES минут,
        # пока задача не выполнена, и полностью останавливается через NAG_MAX_HOURS после срока.
        # Выполнение/удаление снимает джобу (_remove_task_jobs), плюс send_due сам проверяет done.
        end_utc = due_at_utc + timedelta(hours=NAG_MAX_HOURS)
        if end_utc > now_utc:  # иначе окно уже закрыто — не создаём «мёртвую» джобу
            app.scheduler.add_job(
                send_due, IntervalTrigger(minutes=NAG_INTERVAL_MINUTES, start_date=due_at_utc, end_date=end_utc),
                args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
            )
    elif repeat == "none":
        if due_at_utc > now_utc:
            app.scheduler.add_job(
                send_due, "date", run_date=due_at_utc,
                args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
            )
    else:
        if repeat == "daily":
            trigger = CronTrigger(hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "weekly":
            trigger = CronTrigger(day_of_week=local_due.weekday(), hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "monthly":
            trigger = CronTrigger(day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "yearly":
            trigger = CronTrigger(month=local_due.month, day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "weekdays":
            trigger = CronTrigger(day_of_week="mon-fri", hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "monthly_nth_weekday":
            trigger = _nth_weekday_cron(local_due, tz)
        else:
            trigger = CronTrigger(hour=local_due.hour, minute=local_due.minute, timezone=tz)
        app.scheduler.add_job(
            send_due, trigger,
            args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
        )

async def _recipients(owner_id: int, owner_type: str) -> list[int]:
    """Кому слать личное уведомление: самому пользователю, либо — для общей
    задачи чата — всем известным участникам этого чата (рассылка, а не
    сообщение в сам групповой чат)."""
    if owner_type == "chat":
        return await db.get_chat_members(owner_id)
    return [owner_id]

async def _quiet_end_utc(owner_id: int, owner_type: str) -> datetime | None:
    """Если у владельца СЕЙЧАС тихий час — момент его окончания (UTC), иначе None."""
    quiet = await owner_quiet(owner_id, owner_type)
    if not quiet:
        return None
    tz = await owner_tz(owner_id, owner_type)
    now_local = datetime.now(tz).replace(tzinfo=None)
    if not is_quiet_time(now_local.time(), *quiet):
        return None
    return to_utc(next_quiet_end(now_local, *quiet), tz)

async def _defer_if_quiet(owner_id: int, owner_type: str, task_id: int, kind: str) -> bool:
    """kind: 'due' | 'remind'. В тихий час не шлём, а ставим date-джобу на конец тихого часа.
    id джобы фиксирован (quiet_<kind>_<task_id>) + replace_existing: сколько бы раз за ночь ни
    сработал периодический nag, утром придёт ОДНО сообщение."""
    try:
        run_at = await _quiet_end_utc(owner_id, owner_type)
    except Exception:
        # Если в БД оказались кривые quiet_hours (например, ручная правка) — не
        # роняем всю рассылку из-за этого, а просто ведём себя как без тихого часа.
        logger.exception("Не удалось вычислить тихий час owner=%s/%s, игнорирую", owner_type, owner_id)
        return False
    if run_at is None:
        return False
    func = send_reminder if kind == "remind" else send_due
    app.scheduler.add_job(
        func, "date", run_date=run_at, args=[owner_id, owner_type, task_id],
        id=f"quiet_{kind}_{task_id}", replace_existing=True
    )
    logger.info("Тихий час: %s по задаче %s отложено до %s", kind, task_id, run_at.isoformat())
    return True

async def send_reminder(owner_id: int, owner_type: str, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
        return
    # Напоминание «скоро наступит» после срока (например, отложенное тихим часом) — уже ложь
    if task["due_at"] <= datetime.now(ZoneInfo("UTC")):
        return
    if await _defer_if_quiet(owner_id, owner_type, task_id, "remind"):
        return
    lang = await owner_lang(owner_id, owner_type)
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(recipient_id, t(lang, "reminder_text", title=task["title"]))
        except TelegramForbiddenError:
            logger.warning("Юзер %s заблокировал бота, пропускаю напоминание по задаче %s", recipient_id, task_id)
        except Exception:
            logger.exception("Не удалось отправить напоминание user_id=%s task_id=%s", recipient_id, task_id)

async def send_due(owner_id: int, owner_type: str, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
        return

    now = datetime.now(ZoneInfo("UTC"))
    nag = bool(task.get("remind_until_done")) and task["repeat"] == "none"

    # Окно «повторять, пока не выполню» закрыто (на случай отложенной тихим часом джобы)
    if nag and now > task["due_at"] + timedelta(hours=NAG_MAX_HOURS):
        return

    if await _defer_if_quiet(owner_id, owner_type, task_id, "due"):
        return

    # Защита от дублей: если уведомление по этой задаче уже уходило недавно
    # (например, рестарт процесса вызвал повторный catch-up) — не шлём снова.
    # Для повторяющихся напоминаний («nag») окно = интервал, чтобы не слать чаще раза в 30 минут.
    is_followup = False
    if task.get("last_notified_at"):
        last = task["last_notified_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo("UTC"))
        is_followup = nag and last >= task["due_at"]
        min_gap = timedelta(minutes=NAG_INTERVAL_MINUTES - 1) if is_followup else timedelta(minutes=5)
        if (now - last) < min_gap:
            logger.info("Пропускаю дублирующее уведомление task_id=%s (последнее было %s назад)", task_id, now - last)
            return

    lang = await owner_lang(owner_id, owner_type)
    text_key = "nag_text" if is_followup else "due_text"
    sent_any = False
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(
                recipient_id, t(lang, text_key, title=task["title"]),
                reply_markup=task_kb(task_id, lang, done=False)
            )
            sent_any = True
        except TelegramForbiddenError:
            logger.warning("Юзер %s заблокировал бота, пропускаю уведомление по задаче %s", recipient_id, task_id)
        except Exception:
            logger.exception("Не удалось отправить уведомление user_id=%s task_id=%s", recipient_id, task_id)
    if sent_any:
        await db.set_last_notified(task_id, now)

DAILY_SUMMARY_HOUR = 21

DAILY_SUMMARY_MINUTE = 0

async def _summary_section_counts(owner_id: int, owner_type: str, lang: str) -> tuple[str, str] | None:
    """Считает active/done_today/overdue/stuck для одного контекста (личного
    или одной группы) и собирает готовые overdue_line/stuck_line. Возвращает
    None при ошибке — вызывающая сторона просто пропускает секцию, не валит
    всю сводку из-за проблемы в одном из нескольких контекстов пользователя."""
    try:
        counts = await db.get_daily_summary_counts(owner_id, owner_type)
    except Exception:
        logger.exception("Не удалось получить статистику для daily_summary owner=%s/%s", owner_type, owner_id)
        return None
    overdue_line = t(lang, "daily_summary_overdue", overdue=counts["overdue"]) if counts["overdue"] > 0 else ""
    stuck_line = (
        t(lang, "daily_summary_stuck", stuck=counts["stuck_repeats"])
        if counts.get("stuck_repeats", 0) > 0 else ""
    )
    return counts, overdue_line, stuck_line

async def send_daily_summary(user_id: int):
    """Ежедневная вечерняя сводка — ОДНО сообщение на пользователя, объединяющее
    его личные задачи и задачи всех групп, в которых он состоит. Раньше джоба
    шла по owner (личка и каждая группа отдельно), и человек, состоящий в
    нескольких группах, получал по сообщению на каждую — отсюда «три разных
    сводки» за вечер. Теперь джоба одна на user_id, сама собирает контексты
    через db.get_chats_for_user и строит секции внутри одного текста.

    Заодно служит self-check: если сообщение дошло, значит и scheduler, и
    polling живы."""
    lang = await owner_lang(user_id, "user")

    personal = await _summary_section_counts(user_id, "user", lang)
    sections = []
    if personal is not None:
        counts, overdue_line, stuck_line = personal
        sections.append(t(
            lang, "daily_summary_section_personal",
            active=counts["active"], done_today=counts["done_today"],
            overdue_line=overdue_line, stuck_line=stuck_line,
        ))

    try:
        chat_ids = await db.get_chats_for_user(user_id)
    except Exception:
        logger.exception("Не удалось получить список групп user_id=%s для daily_summary", user_id)
        chat_ids = []

    for chat_id in chat_ids:
        section = await _summary_section_counts(chat_id, "chat", lang)
        if section is None:
            continue
        counts, overdue_line, stuck_line = section
        try:
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or t(lang, "daily_summary_chat_fallback")
        except Exception:
            # Бот мог быть удалён из группы, но участник в chat_members ещё
            # остался — не роняем всю сводку из-за одной недоступной группы.
            logger.info("Не удалось получить title чата %s для daily_summary user_id=%s", chat_id, user_id)
            chat_title = t(lang, "daily_summary_chat_fallback")
        sections.append(t(
            lang, "daily_summary_section_chat", chat_title=html.escape(chat_title),
            active=counts["active"], done_today=counts["done_today"],
            overdue_line=overdue_line, stuck_line=stuck_line,
        ))

    if not sections:
        return  # ни одного доступного контекста — нечего слать

    streak_line = ""
    try:
        tz = await owner_tz(user_id, "user")
        n = await _current_streak(user_id, tz)
        if n > 0:
            streak_line = t(lang, "streak_line", n=n)
    except Exception:
        logger.exception("Не удалось посчитать серию user_id=%s", user_id)

    text = (
        t(lang, "daily_summary_combined_header")
        + "".join(sections)
        + "\n"
        + t(lang, "daily_summary_combined_footer", streak_line=streak_line)
    )

    try:
        silent = await _quiet_end_utc(user_id, "user") is not None
    except Exception:
        logger.exception("Не удалось вычислить тихий час user_id=%s для сводки, шлю не беззвучно", user_id)
        silent = False

    try:
        await bot.send_message(user_id, text, parse_mode="HTML", disable_notification=silent)
    except TelegramForbiddenError:
        logger.info("Юзер %s заблокировал бота, пропускаю daily_summary", user_id)
    except Exception:
        logger.exception("Не удалось отправить daily_summary user_id=%s", user_id)

def schedule_daily_summary(user_id: int, tz: ZoneInfo):
    app.scheduler.add_job(
        send_daily_summary, CronTrigger(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE, timezone=tz),
        args=[user_id], id=f"dailysummary_{user_id}", replace_existing=True
    )

# ---------- утренняя сводка ----------
DEFAULT_MORNING_HOUR = 8

DEFAULT_MORNING_MINUTE = 0

async def _morning_section_tasks(owner_id: int, owner_type: str, tz: ZoneInfo, lang: str) -> str:
    """Список задач на СЕГОДНЯ (по локальной дате owner'а) одной готовой
    HTML-строкой (уже с переводом строки на каждой), отсортированных по
    времени. Используется и для личного контекста, и для каждой группы."""
    try:
        tasks = await db.get_tasks(owner_id, owner_type, include_done=False)
    except Exception:
        logger.exception("Не удалось получить задачи для morning_summary owner=%s/%s", owner_type, owner_id)
        return t(lang, "morning_summary_empty_section")

    today = datetime.now(tz).date()
    today_tasks = [task for task in tasks if to_local(task["due_at"], tz).date() == today]
    today_tasks.sort(key=lambda task: task["due_at"])

    if not today_tasks:
        return t(lang, "morning_summary_empty_section")

    lines = []
    for task in today_tasks:
        local_time = to_local(task["due_at"], tz).strftime("%H:%M")
        lines.append(t(lang, "morning_summary_task_line", time=local_time, title=html.escape(task["title"])))
    return "".join(lines)

async def send_morning_summary(user_id: int):
    """Утренняя сводка — список задач на сегодня (не статистика, в отличие от
    вечерней), той же объединённой структурой: личные задачи + секция на
    каждую группу, где состоит пользователь. Независимая от вечерней джоба —
    своё время, своя настройка morning_summary_enabled в user_settings."""
    lang = await owner_lang(user_id, "user")
    tz = await owner_tz(user_id, "user")

    sections = []
    personal_tasks = await _morning_section_tasks(user_id, "user", tz, lang)
    sections.append(t(lang, "morning_summary_section_personal", tasks_list=personal_tasks))

    try:
        chat_ids = await db.get_chats_for_user(user_id)
    except Exception:
        logger.exception("Не удалось получить список групп user_id=%s для morning_summary", user_id)
        chat_ids = []

    any_chat_tasks = False
    for chat_id in chat_ids:
        chat_tasks = await _morning_section_tasks(chat_id, "chat", tz, lang)
        if chat_tasks != t(lang, "morning_summary_empty_section"):
            any_chat_tasks = True
        try:
            chat = await bot.get_chat(chat_id)
            chat_title = chat.title or t(lang, "daily_summary_chat_fallback")
        except Exception:
            logger.info("Не удалось получить title чата %s для morning_summary user_id=%s", chat_id, user_id)
            chat_title = t(lang, "daily_summary_chat_fallback")
        sections.append(t(lang, "morning_summary_section_chat", chat_title=html.escape(chat_title), tasks_list=chat_tasks))

    # Если вообще нигде ничего нет на сегодня — одно короткое сообщение вместо
    # пустых секций подряд (меньше шума в 8 утра, когда и так всё свободно).
    personal_empty = personal_tasks == t(lang, "morning_summary_empty_section")
    if personal_empty and not any_chat_tasks:
        text = t(lang, "morning_summary_nothing")
    else:
        text = t(lang, "morning_summary_header") + "".join(sections)

    # Тихий час может легитимно перекрывать утреннее время (например, тихий
    # час "22:00-09:00", а сводка настроена на 8:00) — та же логика, что и у
    # вечерней: не откладываем (сводка полезна именно утром), но шлём беззвучно.
    try:
        silent = await _quiet_end_utc(user_id, "user") is not None
    except Exception:
        logger.exception("Не удалось вычислить тихий час user_id=%s для утренней сводки, шлю не беззвучно", user_id)
        silent = False

    try:
        await bot.send_message(user_id, text, parse_mode="HTML", disable_notification=silent)
    except TelegramForbiddenError:
        logger.info("Юзер %s заблокировал бота, пропускаю morning_summary", user_id)
    except Exception:
        logger.exception("Не удалось отправить morning_summary user_id=%s", user_id)

def schedule_morning_summary(user_id: int, tz: ZoneInfo, hour: int, minute: int):
    app.scheduler.add_job(
        send_morning_summary, CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[user_id], id=f"morningsummary_{user_id}", replace_existing=True
    )

def unschedule_morning_summary(user_id: int):
    try:
        app.scheduler.remove_job(f"morningsummary_{user_id}")
    except JobLookupError:
        pass  # джобы и не было (ещё не включали) — не ошибка

async def purge_done_tasks():
    """Раз в сутки чистит выполненные задачи старше недели и историю старше 90 дней
    (экономия места в БД). 90 дней истории — предел стрика."""
    try:
        n = await db.purge_done_tasks(7)
        h = await db.purge_old_history(90)
    except Exception:
        logger.exception("Не удалось почистить выполненные задачи/историю")
        return
    if n or h:
        logger.info("Удалено: выполненных задач старше 7 дней — %s, событий истории старше 90 дней — %s", n, h)

async def restore_jobs():
    """Восстанавливает джобы при старте процесса (после редеплоя/рестарта).
    Если дедлайн уже прошёл сегодня, а бот был офлайн — досылает уведомление сразу
    (если не было отправлено недавно — см. защиту в send_due)."""
    now_utc = datetime.now(ZoneInfo("UTC"))

    tasks = await db.get_all_active_tasks()
    for task in tasks:
        owner_id, owner_type = task["owner_id"], task["owner_type"]
        try:
            tz = await owner_tz(owner_id, owner_type)
        except Exception:
            tz = DEFAULT_TZ
        today_start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        due_local = to_local(task["due_at"], tz)

        missed_today = (
            task["repeat"] == "none"
            and task["due_at"] < now_utc
            and due_local >= today_start_local
        )
        if missed_today:
            await send_due(owner_id, owner_type, task["id"])
            if task.get("remind_until_done"):
                # дальше продолжаем «nag»-расписание (окно 24 ч ещё может быть открыто)
                schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                              remind_until_done=True)
        else:
            schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                          remind_until_done=bool(task.get("remind_until_done")))

    app.scheduler.add_job(
        purge_done_tasks, CronTrigger(hour=4, minute=0, timezone=ZoneInfo("UTC")),
        id="purge_done_tasks", replace_existing=True,
    )
    await purge_done_tasks()  # и сразу при старте: процесс на Render может не доживать до 04:00

    # Планируем ежедневную сводку (self-check) для всех людей, которым она
    # может быть нужна — личные пользователи бота И участники групп (человек
    # мог ни разу не писать боту лично, только состоять в группе). Групповые
    # owner'ы отдельной джобы больше не получают: каждая группа подхватывается
    # объединённой сводкой её участников через db.get_chats_for_user
    # (bot.py:send_daily_summary) — одна джоба на человека, а не на owner.
    for user_id in await db.get_all_summary_recipients():
        try:
            tz = await owner_tz(user_id, "user")
        except Exception:
            tz = DEFAULT_TZ
        schedule_daily_summary(user_id, tz)

    # Утренняя сводка — отдельный, более узкий список: только те, кто явно
    # включил её через /morning (morning_summary_enabled = TRUE), а не все
    # известные пользователи, в отличие от вечерней.
    for row in await db.get_morning_summary_recipients():
        try:
            tz = await owner_tz(row["user_id"], "user")
        except Exception:
            tz = DEFAULT_TZ
        schedule_morning_summary(row["user_id"], tz, row["morning_summary_hour"], row["morning_summary_minute"])
