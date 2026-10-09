"""send_due / send_reminder / daily summary / /history: nag, тихий час, дедупликация.
БД, Telegram и планировщик подменены; время берётся относительно «сейчас»."""

from datetime import datetime, timedelta, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import bot

UTC = ZoneInfo("UTC")
WAW = ZoneInfo("Europe/Warsaw")


@pytest_asyncio.fixture
async def env(monkeypatch):
    sched = AsyncIOScheduler(timezone=UTC)
    sched.start(paused=True)   # джобы не выполняются, но replace_existing/get_job работают как в проде
    monkeypatch.setattr(bot, "scheduler", sched)
    monkeypatch.setattr(bot, "_settings_cache", {})
    sent, tasks = [], {}

    async def fake_send(chat_id, text, **kw):
        sent.append(SimpleNamespace(chat_id=chat_id, text=text, silent=kw.get("disable_notification")))

    async def get_task(task_id):
        return tasks.get(task_id)

    async def set_last_notified(task_id, when):
        tasks[task_id]["last_notified_at"] = when

    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    monkeypatch.setattr(bot.db, "get_task", get_task)
    monkeypatch.setattr(bot.db, "set_last_notified", set_last_notified)

    def settings(quiet=None):
        bot._settings_cache[("user", 1)] = {
            "timezone": "Europe/Warsaw", "language": "ru",
            "quiet_hours_start": quiet[0] if quiet else None, "quiet_hours_end": quiet[1] if quiet else None,
        }

    def add_task(task_id, due, **overrides):
        tasks[task_id] = {"id": task_id, "owner_id": 1, "owner_type": "user", "title": "Тест <b>", "due_at": due,
                          "repeat": "none", "remind": "0", "done": False, "tag": None,
                          "last_notified_at": None, "remind_until_done": False, **overrides}
        return tasks[task_id]

    def quiet_around_now(offset_start_h: float, offset_end_h: float):
        loc = datetime.now(WAW)
        start = (loc + timedelta(hours=offset_start_h)).time().replace(second=0, microsecond=0)
        end = (loc + timedelta(hours=offset_end_h)).time().replace(second=0, microsecond=0)
        return start, end

    settings()
    yield SimpleNamespace(sched=sched, sent=sent, tasks=tasks, settings=settings, add_task=add_task,
                          quiet_around_now=quiet_around_now)
    sched.shutdown(wait=False)


NOW = lambda: datetime.now(UTC)  # noqa: E731


# ---------- обычная отправка и дедупликация ----------

async def test_due_notification_sent_once_and_dedupes(env):
    env.add_task(1, NOW() - timedelta(minutes=1))
    await bot.send_due(1, "user", 1)
    await bot.send_due(1, "user", 1)   # повтор сразу — не должен уйти
    assert len(env.sent) == 1 and "Срок наступил" in env.sent[0].text
    assert env.tasks[1]["last_notified_at"] is not None


async def test_done_task_never_notifies(env):
    env.add_task(1, NOW(), done=True)
    await bot.send_due(1, "user", 1)
    await bot.send_reminder(1, "user", 1)
    assert env.sent == []


async def test_missing_task_is_ignored(env):
    await bot.send_due(1, "user", 404)
    assert env.sent == []


async def test_reminder_sent_before_due_and_dropped_after(env):
    env.add_task(1, NOW() + timedelta(minutes=30))
    await bot.send_reminder(1, "user", 1)
    assert len(env.sent) == 1 and "скоро наступит" in env.sent[0].text
    env.add_task(2, NOW() - timedelta(minutes=5))        # срок уже прошёл — напоминание устарело
    await bot.send_reminder(1, "user", 2)
    assert len(env.sent) == 1


# ---------- nag: «повторять, пока не выполню» ----------

async def test_nag_followup_text_and_30_minute_gap(env):
    t = env.add_task(1, NOW() - timedelta(hours=3), remind_until_done=True, last_notified_at=NOW() - timedelta(minutes=30))
    await bot.send_due(1, "user", 1)
    assert len(env.sent) == 1 and "Всё ещё не выполнено" in env.sent[0].text

    t["last_notified_at"] = NOW() - timedelta(minutes=10)    # чаще раза в 30 минут — нельзя
    await bot.send_due(1, "user", 1)
    assert len(env.sent) == 1


async def test_nag_first_notification_uses_regular_text(env):
    env.add_task(1, NOW() - timedelta(minutes=1), remind_until_done=True)
    await bot.send_due(1, "user", 1)
    assert "Срок наступил" in env.sent[0].text


async def test_nag_stops_after_24_hours(env):
    env.add_task(1, NOW() - timedelta(hours=bot.NAG_MAX_HOURS, minutes=1), remind_until_done=True,
                 last_notified_at=NOW() - timedelta(hours=2))
    await bot.send_due(1, "user", 1)
    assert env.sent == []


async def test_nag_new_due_date_resets_followup_regime(env):
    """Задачу отложили (due_at сдвинут вперёд, а последнее уведомление было до него) — снова обычный текст."""
    env.add_task(1, NOW() - timedelta(seconds=30), remind_until_done=True, last_notified_at=NOW() - timedelta(hours=1))
    await bot.send_due(1, "user", 1)
    assert "Срок наступил" in env.sent[0].text


# ---------- тихий час ----------

async def test_quiet_hours_defer_due_to_end_of_window(env):
    start, end = env.quiet_around_now(-1, 2)
    env.settings((start, end))
    env.add_task(1, NOW() - timedelta(minutes=1))
    await bot.send_due(1, "user", 1)
    await bot.send_due(1, "user", 1)    # второй вызов за ночь не плодит джобы
    assert env.sent == []
    jobs = [j for j in env.sched.get_jobs() if j.id == "quiet_due_1"]
    assert len(jobs) == 1
    run_local = jobs[0].trigger.run_date.astimezone(WAW)
    assert (run_local.hour, run_local.minute) == (end.hour, end.minute)
    assert jobs[0].trigger.run_date > NOW()


async def test_quiet_hours_defer_reminder(env):
    env.settings(env.quiet_around_now(-1, 2))
    env.add_task(1, NOW() + timedelta(hours=3))
    await bot.send_reminder(1, "user", 1)
    assert env.sent == [] and env.sched.get_job("quiet_remind_1") is not None


async def test_outside_quiet_hours_sends_normally(env):
    env.settings(env.quiet_around_now(3, 5))
    env.add_task(1, NOW() - timedelta(minutes=1))
    await bot.send_due(1, "user", 1)
    assert len(env.sent) == 1 and env.sched.get_jobs() == []


async def test_no_quiet_hours_configured_sends(env):
    env.add_task(1, NOW() - timedelta(minutes=1))
    await bot.send_due(1, "user", 1)
    assert len(env.sent) == 1


async def test_equal_quiet_bounds_treated_as_disabled(env):
    env.settings((time(10, 0), time(10, 0)))
    env.add_task(1, NOW() - timedelta(minutes=1))
    await bot.send_due(1, "user", 1)
    assert len(env.sent) == 1


# ---------- ежедневная сводка ----------

@pytest.fixture
def summary_env(env, monkeypatch):
    async def counts(owner_id, owner_type):
        return {"active": 2, "done_today": 1, "overdue": 0}
    monkeypatch.setattr(bot.db, "get_daily_summary_counts", counts)

    async def chats_for_user(user_id):
        return []
    monkeypatch.setattr(bot.db, "get_chats_for_user", chats_for_user)
    return env


async def test_summary_includes_streak_line(summary_env, monkeypatch):
    today = datetime.now(WAW).date()

    async def done_days(user_id, tz_name, chat_id=None, limit=400):
        return [today, today - timedelta(days=1), today - timedelta(days=2)]
    monkeypatch.setattr(bot.db, "get_done_days", done_days)
    await bot.send_daily_summary(1)
    assert "Серия: 3" in summary_env.sent[-1].text and summary_env.sent[-1].silent is False


async def test_summary_without_streak_has_no_streak_line(summary_env, monkeypatch):
    async def done_days(*a, **kw):
        return []
    monkeypatch.setattr(bot.db, "get_done_days", done_days)
    await bot.send_daily_summary(1)
    assert "Серия" not in summary_env.sent[-1].text


async def test_summary_survives_streak_failure(summary_env, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(bot.db, "get_done_days", boom)
    await bot.send_daily_summary(1)
    assert len(summary_env.sent) == 1


async def test_summary_is_silent_during_quiet_hours(summary_env, monkeypatch):
    async def done_days(*a, **kw):
        return []
    monkeypatch.setattr(bot.db, "get_done_days", done_days)
    summary_env.settings(summary_env.quiet_around_now(-1, 2))
    await bot.send_daily_summary(1)
    assert summary_env.sent[-1].silent is True


# ---------- /history ----------

async def test_history_stats_text(env, monkeypatch):
    today = datetime.now(WAW).date()
    rows = [{"day": today, "event": "done", "cnt": 2}, {"day": today, "event": "created", "cnt": 3},
            {"day": today - timedelta(days=1), "event": "deleted", "cnt": 1}]

    async def stats(user_id, days, tz_name, chat_id=None):
        return rows

    async def done_days(*a, **kw):
        return [today]
    monkeypatch.setattr(bot.db, "get_history_stats", stats)
    monkeypatch.setattr(bot.db, "get_done_days", done_days)

    week = await bot._render_history_text(1, 1, "user", "ru", "7")
    assert "Создано: 3" in week and "Выполнено: 2" in week and "Удалено: 1" in week
    assert "Серия: 1" in week and "▇▇" in week and len(week.splitlines()) >= 10   # по строке на день недели
    month = await bot._render_history_text(1, 1, "user", "en", "30")
    assert "Created: 3" in month and "▇" not in month    # помесячно — только итоги


async def test_history_recent_empty(env, monkeypatch):
    async def empty(user_id):
        return []
    monkeypatch.setattr(bot.db, "get_history", empty)
    assert await bot._render_history_text(1, 1, "user", "ru", "recent") == bot.t("ru", "no_history")
