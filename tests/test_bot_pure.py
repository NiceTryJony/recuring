"""Тесты чистых функций bot.py (без Telegram API и БД) и расписания задач."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import bot
from date_parser import parse_quiet_hours

WAW = ZoneInfo("Europe/Warsaw")
UTC = ZoneInfo("UTC")


def cron_fields(trigger: CronTrigger) -> dict:
    return {f.name: str(f) for f in trigger.fields if str(f) != "*"}


# ---------- _nth_weekday_cron ----------

@pytest.mark.parametrize("due, day_expr", [
    (datetime(2026, 10, 1, 9, 0), "1st thu"),
    (datetime(2026, 10, 4, 18, 30), "1st sun"),
    (datetime(2026, 10, 14, 9, 0), "2nd wed"),
    (datetime(2026, 10, 21, 9, 0), "3rd wed"),
    (datetime(2026, 10, 28, 9, 0), "4th wed"),
    (datetime(2026, 10, 29, 9, 0), "5th thu"),
])
def test_nth_weekday_cron_expression(due, day_expr):
    fields = cron_fields(bot._nth_weekday_cron(due, WAW))
    assert fields["day"] == day_expr
    assert fields["hour"] == str(due.hour) and fields["minute"] == str(due.minute)


def test_nth_weekday_cron_fires_on_next_matching_weekday():
    # 2-я среда октября (14.10) -> следующий раз 2-я среда ноября (11.11)
    trigger = bot._nth_weekday_cron(datetime(2026, 10, 14, 9, 0), WAW)
    nxt = trigger.get_next_fire_time(datetime(2026, 10, 14, 9, 0, tzinfo=WAW), datetime(2026, 10, 14, 9, 1, tzinfo=WAW))
    assert (nxt.month, nxt.day, nxt.hour) == (11, 11, 9)
    assert nxt.tzinfo is not None


# ---------- fmt_remind / fmt_repeat ----------

@pytest.mark.parametrize("value, lang, expected", [
    ("1h,1d", "ru", "За 1 час, За 1 день"),
    ("0", "en", "No reminder"),
    ("zzz", "pl", "Bez przypomnienia"),
    (None, "ru", "Без напоминания"),
    ("", "ru", "Без напоминания"),
])
def test_fmt_remind(value, lang, expected):
    assert bot.fmt_remind(value, lang) == expected


def test_fmt_remind_ignores_unknown_codes():
    assert bot.fmt_remind("1h,bogus", "ru") == bot.fmt_remind("1h", "ru")


def test_fmt_repeat_known_and_unknown():
    assert bot.fmt_repeat("daily", "ru") == bot.REPEAT_OPTIONS["daily"]["ru"]
    assert bot.fmt_repeat("unknown_code", "ru") == "unknown_code"


# ---------- время ----------

def test_to_utc_and_back_roundtrip():
    local = datetime(2026, 10, 4, 18, 0)
    utc = bot.to_utc(local, WAW)
    assert utc.utcoffset() == timedelta(0) and utc.hour == 16   # CEST = UTC+2
    assert bot.to_local(utc, WAW).replace(tzinfo=None) == local


# ---------- клавиатуры ----------

def _all_keyboards():
    yield from [
        bot.remind_kb("ru"), bot.remind_kb("en", selected={"1h"}), bot.repeat_kb("pl"),
        bot.repeat_kb("ru", prefix="editrep_9999999999"), bot.nag_kb("ru"), bot.quiet_kb("ru"),
        bot.task_kb(9999999999, "ru"), bot.task_kb(9999999999, "en", done=True),
        bot.history_kb("ru", "7"),
    ]


def test_callback_data_within_telegram_limit():
    for kb in _all_keyboards():
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64, btn.callback_data


def test_remind_done_button_matches_handler_prefix():
    """Регресс: кнопка «Готово» имела callback 'remdone' и не попадала под startswith('rem_')."""
    done_btn = bot.remind_kb("ru").inline_keyboard[-1][0]
    assert done_btn.callback_data == "rem_done"
    assert done_btn.callback_data.startswith("rem_")
    assert done_btn.callback_data.split("_", 1)[1] == "done"


def test_edit_repeat_callback_survives_underscores_in_code():
    """Регресс: split('_') падал на 'monthly_nth_weekday'."""
    for kb in [bot.repeat_kb("ru", prefix="editrep_7")]:
        for row in kb.inline_keyboard:
            for btn in row:
                _, task_id, code = btn.callback_data.split("_", 2)
                assert task_id == "7" and code in bot.REPEAT_OPTIONS


def test_quiet_presets_are_valid_and_roundtrip_through_callbacks():
    for row in bot.quiet_kb("ru").inline_keyboard[:-1]:
        data = row[0].callback_data
        assert data.startswith("quiet_set_")
        assert parse_quiet_hours(data.removeprefix("quiet_set_")) is not None
    assert bot.quiet_kb("ru").inline_keyboard[-1][0].callback_data == "quiet_off"


def test_history_kb_marks_only_active_mode():
    kb = bot.history_kb("ru", "30")
    marked = [row[0].callback_data for row in kb.inline_keyboard if row[0].text.startswith("✅")]
    assert marked == ["hist_30"]
    assert {row[0].callback_data for row in kb.inline_keyboard} == {"hist_recent", "hist_7", "hist_30"}


# ---------- fmt_task ----------

async def test_fmt_task_escapes_html_in_title_and_tag(monkeypatch):
    monkeypatch.setattr(bot, "_settings_cache", {("user", 1): {"timezone": "Europe/Warsaw", "language": "ru"}})
    task = {"id": 1, "owner_id": 1, "owner_type": "user", "title": "a < b & <i>c</i>", "tag": "<x>",
            "due_at": datetime(2026, 10, 4, 16, 0, tzinfo=UTC), "done": False, "repeat": "none",
            "remind": "0", "remind_until_done": True}
    text = await bot.fmt_task(task)
    assert "a &lt; b &amp; &lt;i&gt;c&lt;/i&gt;" in text and "&lt;x&gt;" in text
    assert "<i>" not in text
    assert "18:00" in text                      # 16:00 UTC = 18:00 Warsaw (CEST)
    assert bot.t("ru", "nag_line") in text      # отметка про nag


# ---------- schedule_task ----------

@pytest_asyncio.fixture
async def sched(monkeypatch):
    """Настоящий, но «на паузе» планировщик: джобы не выполняются, зато replace_existing/get_job
    работают как в проде (у незапущенного планировщика джобы лежат в pending-списке без этой семантики)."""
    s = AsyncIOScheduler(timezone=UTC)
    s.start(paused=True)
    monkeypatch.setattr(bot, "scheduler", s)
    yield s
    s.shutdown(wait=False)


async def test_schedule_plain_future_task_is_single_date_job(sched):
    due = datetime.now(UTC) + timedelta(hours=3)
    bot.schedule_task(1, 10, "user", due, "none", "0", WAW)
    assert [j.id for j in sched.get_jobs()] == ["due_1"]
    assert isinstance(sched.get_job("due_1").trigger, DateTrigger)


async def test_schedule_past_task_creates_nothing(sched):
    bot.schedule_task(1, 10, "user", datetime.now(UTC) - timedelta(hours=1), "none", "0", WAW)
    assert sched.get_jobs() == []


async def test_schedule_reminders_only_for_future_times(sched):
    due = datetime.now(UTC) + timedelta(hours=2)   # 3 часа / 1 день / 3 дня до срока уже в прошлом
    bot.schedule_task(1, 10, "user", due, "none", "1h,1d,3d", WAW)
    ids = {j.id for j in sched.get_jobs()}
    assert "remind_1_1h" in ids and "remind_1_1d" not in ids and "remind_1_3d" not in ids


async def test_schedule_nag_creates_interval_job_with_limits(sched):
    due = datetime.now(UTC) - timedelta(hours=2)
    bot.schedule_task(1, 10, "user", due, "none", "0", WAW, remind_until_done=True)
    trig = sched.get_job("due_1").trigger
    assert isinstance(trig, IntervalTrigger)
    assert trig.interval == timedelta(minutes=bot.NAG_INTERVAL_MINUTES)
    assert trig.start_date == due
    assert trig.end_date == due + timedelta(hours=bot.NAG_MAX_HOURS)


async def test_schedule_nag_in_future_starts_at_due(sched):
    due = datetime.now(UTC) + timedelta(hours=5)
    bot.schedule_task(1, 10, "user", due, "none", "0", WAW, remind_until_done=True)
    assert sched.get_job("due_1").trigger.start_date == due


async def test_schedule_nag_with_closed_window_creates_no_dead_job(sched):
    bot.schedule_task(1, 10, "user", datetime.now(UTC) - timedelta(hours=bot.NAG_MAX_HOURS + 1),
                      "none", "0", WAW, remind_until_done=True)
    assert sched.get_job("due_1") is None


async def test_schedule_nag_ignored_for_repeating_tasks(sched):
    bot.schedule_task(1, 10, "user", datetime.now(UTC) + timedelta(hours=1), "daily", "0", WAW, remind_until_done=True)
    assert isinstance(sched.get_job("due_1").trigger, CronTrigger)


async def test_schedule_replaces_existing_job(sched):
    now = datetime.now(UTC)
    bot.schedule_task(1, 10, "user", now + timedelta(hours=1), "none", "0", WAW)
    bot.schedule_task(1, 10, "user", now + timedelta(hours=9), "none", "0", WAW)
    assert len(sched.get_jobs()) == 1


async def test_remove_task_jobs_removes_everything_of_that_task_only(sched):
    due = datetime.now(UTC) + timedelta(days=5)
    bot.schedule_task(1, 10, "user", due, "none", "1h,1d,3d", WAW)
    bot.schedule_task(2, 10, "user", due, "none", "1h", WAW)
    for jid in ("quiet_due_1", "quiet_remind_1", "snooze_1"):
        sched.add_job(lambda: None, "date", run_date=due, id=jid)
    bot._remove_task_jobs(1)
    assert {j.id for j in sched.get_jobs()} == {"due_2", "remind_2_1h"}
