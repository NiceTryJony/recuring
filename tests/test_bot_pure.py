"""Тесты чистых функций bot.py (без Telegram API и БД) и расписания задач."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tgbot import app, constants, formatting, keyboards, owner, scheduling
from i18n import t
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
    fields = cron_fields(scheduling._nth_weekday_cron(due, WAW))
    assert fields["day"] == day_expr
    assert fields["hour"] == str(due.hour) and fields["minute"] == str(due.minute)


def test_nth_weekday_cron_fires_on_next_matching_weekday():
    # 2-я среда октября (14.10) -> следующий раз 2-я среда ноября (11.11)
    trigger = scheduling._nth_weekday_cron(datetime(2026, 10, 14, 9, 0), WAW)
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
    assert formatting.fmt_remind(value, lang) == expected


def test_fmt_remind_ignores_unknown_codes():
    assert formatting.fmt_remind("1h,bogus", "ru") == formatting.fmt_remind("1h", "ru")


def test_fmt_repeat_known_and_unknown():
    assert formatting.fmt_repeat("daily", "ru") == constants.REPEAT_OPTIONS["daily"]["ru"]
    assert formatting.fmt_repeat("unknown_code", "ru") == "unknown_code"


# ---------- время ----------

def test_to_utc_and_back_roundtrip():
    local = datetime(2026, 10, 4, 18, 0)
    utc = owner.to_utc(local, WAW)
    assert utc.utcoffset() == timedelta(0) and utc.hour == 16   # CEST = UTC+2
    assert owner.to_local(utc, WAW).replace(tzinfo=None) == local


# ---------- клавиатуры ----------

def _all_keyboards():
    yield from [
        keyboards.remind_kb("ru"), keyboards.remind_kb("en", selected={"1h"}), keyboards.repeat_kb("pl"),
        keyboards.repeat_kb("ru", prefix="editrep_9999999999"), keyboards.nag_kb("ru"), keyboards.quiet_kb("ru"),
        keyboards.task_kb(9999999999, "ru"), keyboards.task_kb(9999999999, "en", done=True),
        keyboards.task_kb(9999999999, "pl", open_url="https://x.example/dashboard/app?task=9999999999"),
        keyboards.history_kb("ru", "7"),
    ]


def test_callback_data_within_telegram_limit():
    for kb in _all_keyboards():
        for row in kb.inline_keyboard:
            for btn in row:
                assert btn.callback_data or btn.web_app, btn   # у кнопки есть действие
                if btn.callback_data:
                    assert len(btn.callback_data.encode()) <= 64, btn.callback_data


def test_task_kb_open_button_only_when_url_given():
    def web_apps(kb):
        return [b.web_app.url for row in kb.inline_keyboard for b in row if b.web_app]
    assert web_apps(keyboards.task_kb(5, "ru")) == []
    assert web_apps(keyboards.task_kb(5, "ru", open_url="https://h/dashboard/app?task=5")) == ["https://h/dashboard/app?task=5"]


async def _run_fallback(monkeypatch, *, chat_type="private", text=None, caption=None, photo=None,
                        forward=None, state=None):
    """Prowadzi wiadomość przez fallback z podmienionym otoczeniem. -> (tytuły do take_title, dane stanu, odpowiedzi)."""
    from types import SimpleNamespace
    from tgbot.handlers import fallback
    titles, answers = [], []

    async def _resolve(chat, uid):
        return uid, "user"

    async def _tz(*a):
        return WAW

    async def _lang(*a):
        return "pl"

    async def _take_title(title_text, message, st):
        titles.append(title_text)

    class _State:
        data: dict = {}

        async def get_state(self):
            return state

        async def set_state(self, s):
            pass

        async def set_data(self, d):
            self.data = d

    async def _answer(txt, **kw):
        answers.append(txt)

    for name, impl in {"resolve_owner": _resolve, "owner_tz": _tz, "owner_lang": _lang, "take_title": _take_title}.items():
        monkeypatch.setattr(fallback, name, impl)
    msg = SimpleNamespace(text=text, caption=caption, photo=photo, forward_origin=forward,
                          chat=SimpleNamespace(type=chat_type, id=1), from_user=SimpleNamespace(id=5), answer=_answer)
    st = _State()
    await fallback.fallback(msg, st)
    return titles, st.data, answers


@pytest.mark.parametrize("chat_type,text,state,expect_task", [
    ("private", "kup mleko jutro 18:00", None, True),
    ("private", "hej", None, False),                          # без даты — не задача
    ("private", "Co robisz jutro?", None, False),             # вопрос — не задача
    ("private", "kup mleko jutro 18:00", "AddTask:repeat", False),  # посреди другого диалога
    ("group", "kup mleko jutro 18:00", None, False),          # в группе не перехватываем
])
async def test_quick_add_from_plain_message(monkeypatch, chat_type, text, state, expect_task):
    titles, _, answers = await _run_fallback(monkeypatch, chat_type=chat_type, text=text, state=state)
    assert bool(titles) is expect_task
    assert bool(answers) is not expect_task                   # иначе — подсказка /help


async def test_forwarded_message_becomes_task_with_description(monkeypatch):
    from types import SimpleNamespace
    titles, data, _ = await _run_fallback(monkeypatch, text="Zapłacić czynsz\nKonto: 123\ndo 10-go",
                                          forward=SimpleNamespace(type="user"))
    assert titles == ["Zapłacić czynsz"]                       # data nie jest wymagana — forward to zawsze zadanie
    assert data["description"].startswith("Zapłacić czynsz\nKonto")


@pytest.mark.parametrize("caption,asks_title", [("Paragon do zwrotu jutro", False), (None, True)])
async def test_photo_becomes_task_with_photo_attached(monkeypatch, caption, asks_title):
    from types import SimpleNamespace
    photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]
    titles, data, answers = await _run_fallback(monkeypatch, caption=caption, photo=photo)
    assert data["photo_file_id"] == "big"                      # największy rozmiar
    assert titles == ([] if asks_title else [caption])
    assert answers == ([t("pl", "ask_title")] if asks_title else [])


@pytest.mark.parametrize("repeat,asked", [("daily", "ask_remind"), (None, "ask_repeat")])
async def test_repeat_from_text_skips_repeat_question(repeat, asked):
    from types import SimpleNamespace
    from tgbot.handlers import add_task
    answers = []

    class _State:
        def __init__(self):
            self.data = {"repeat": repeat}

        async def get_data(self):
            return self.data

        async def update_data(self, **kw):
            self.data.update(kw)

        async def set_state(self, s):
            self.state = s

    async def _answer(txt, **kw):
        answers.append(txt)

    st = _State()
    await add_task._accept_date_and_ask_repeat(datetime(2026, 10, 8, 9), st, WAW, "pl", SimpleNamespace(answer=_answer))
    assert answers == [t("pl", asked)]
    assert "due_at" in st.data


async def test_member_leaving_group_loses_access(monkeypatch):
    from types import SimpleNamespace
    from tgbot.handlers import basic
    removed = []

    async def _remove(chat_id, user_id):
        removed.append((chat_id, user_id))
    monkeypatch.setattr(owner.db, "remove_chat_member", _remove)
    bot = SimpleNamespace(id=1)
    msg = SimpleNamespace(chat=SimpleNamespace(id=-50), bot=bot, left_chat_member=SimpleNamespace(id=77))
    await basic.group_membership_changed(msg)
    msg.left_chat_member = SimpleNamespace(id=1)          # ушёл сам бот — не «участник»
    await basic.group_membership_changed(msg)
    msg.left_chat_member = None                           # кто-то пришёл — ничего не трогаем
    await basic.group_membership_changed(msg)
    assert removed == [(-50, 77)]


def test_remind_done_button_matches_handler_prefix():
    """Регресс: кнопка «Готово» имела callback 'remdone' и не попадала под startswith('rem_')."""
    done_btn = keyboards.remind_kb("ru").inline_keyboard[-1][0]
    assert done_btn.callback_data == "rem_done"
    assert done_btn.callback_data.startswith("rem_")
    assert done_btn.callback_data.split("_", 1)[1] == "done"


def test_edit_repeat_callback_survives_underscores_in_code():
    """Регресс: split('_') падал на 'monthly_nth_weekday'."""
    for kb in [keyboards.repeat_kb("ru", prefix="editrep_7")]:
        for row in kb.inline_keyboard:
            for btn in row:
                _, task_id, code = btn.callback_data.split("_", 2)
                assert task_id == "7" and code in constants.REPEAT_OPTIONS


def test_quiet_presets_are_valid_and_roundtrip_through_callbacks():
    for row in keyboards.quiet_kb("ru").inline_keyboard[:-1]:
        data = row[0].callback_data
        assert data.startswith("quiet_set_")
        assert parse_quiet_hours(data.removeprefix("quiet_set_")) is not None
    assert keyboards.quiet_kb("ru").inline_keyboard[-1][0].callback_data == "quiet_off"


def test_history_kb_marks_only_active_mode():
    kb = keyboards.history_kb("ru", "30")
    marked = [row[0].callback_data for row in kb.inline_keyboard if row[0].text.startswith("✅")]
    assert marked == ["hist_30"]
    assert {row[0].callback_data for row in kb.inline_keyboard} == {"hist_recent", "hist_7", "hist_30"}


# ---------- fmt_task ----------

async def test_fmt_task_escapes_html_in_title_and_tag(monkeypatch):
    monkeypatch.setattr(owner, "_settings_cache", {("user", 1): {"timezone": "Europe/Warsaw", "language": "ru"}})
    task = {"id": 1, "owner_id": 1, "owner_type": "user", "title": "a < b & <i>c</i>", "tag": "<x>",
            "due_at": datetime(2026, 10, 4, 16, 0, tzinfo=UTC), "done": False, "repeat": "none",
            "remind": "0", "remind_until_done": True}
    text = await formatting.fmt_task(task)
    assert "a &lt; b &amp; &lt;i&gt;c&lt;/i&gt;" in text and "&lt;x&gt;" in text
    assert "<i>" not in text
    assert "18:00" in text                      # 16:00 UTC = 18:00 Warsaw (CEST)
    assert t("ru", "nag_line") in text      # отметка про nag


# ---------- schedule_task ----------

@pytest_asyncio.fixture
async def sched(monkeypatch):
    """Настоящий, но «на паузе» планировщик: джобы не выполняются, зато replace_existing/get_job
    работают как в проде (у незапущенного планировщика джобы лежат в pending-списке без этой семантики)."""
    s = AsyncIOScheduler(timezone=UTC)
    s.start(paused=True)
    monkeypatch.setattr(app, "scheduler", s)
    yield s
    s.shutdown(wait=False)


async def test_schedule_plain_future_task_is_single_date_job(sched):
    due = datetime.now(UTC) + timedelta(hours=3)
    scheduling.schedule_task(1, 10, "user", due, "none", "0", WAW)
    assert [j.id for j in sched.get_jobs()] == ["due_1"]
    assert isinstance(sched.get_job("due_1").trigger, DateTrigger)


async def test_schedule_past_task_creates_nothing(sched):
    scheduling.schedule_task(1, 10, "user", datetime.now(UTC) - timedelta(hours=1), "none", "0", WAW)
    assert sched.get_jobs() == []


async def test_schedule_reminders_only_for_future_times(sched):
    due = datetime.now(UTC) + timedelta(hours=2)   # 3 часа / 1 день / 3 дня до срока уже в прошлом
    scheduling.schedule_task(1, 10, "user", due, "none", "1h,1d,3d", WAW)
    ids = {j.id for j in sched.get_jobs()}
    assert "remind_1_1h" in ids and "remind_1_1d" not in ids and "remind_1_3d" not in ids


async def test_schedule_nag_creates_interval_job_with_limits(sched):
    due = datetime.now(UTC) - timedelta(hours=2)
    scheduling.schedule_task(1, 10, "user", due, "none", "0", WAW, remind_until_done=True)
    trig = sched.get_job("due_1").trigger
    assert isinstance(trig, IntervalTrigger)
    assert trig.interval == timedelta(minutes=constants.NAG_INTERVAL_MINUTES)
    assert trig.start_date == due
    assert trig.end_date == due + timedelta(hours=constants.NAG_MAX_HOURS)


async def test_schedule_nag_in_future_starts_at_due(sched):
    due = datetime.now(UTC) + timedelta(hours=5)
    scheduling.schedule_task(1, 10, "user", due, "none", "0", WAW, remind_until_done=True)
    assert sched.get_job("due_1").trigger.start_date == due


async def test_schedule_nag_with_closed_window_creates_no_dead_job(sched):
    scheduling.schedule_task(1, 10, "user", datetime.now(UTC) - timedelta(hours=constants.NAG_MAX_HOURS + 1),
                      "none", "0", WAW, remind_until_done=True)
    assert sched.get_job("due_1") is None


async def test_schedule_nag_ignored_for_repeating_tasks(sched):
    scheduling.schedule_task(1, 10, "user", datetime.now(UTC) + timedelta(hours=1), "daily", "0", WAW, remind_until_done=True)
    assert isinstance(sched.get_job("due_1").trigger, CronTrigger)


async def test_schedule_replaces_existing_job(sched):
    now = datetime.now(UTC)
    scheduling.schedule_task(1, 10, "user", now + timedelta(hours=1), "none", "0", WAW)
    scheduling.schedule_task(1, 10, "user", now + timedelta(hours=9), "none", "0", WAW)
    assert len(sched.get_jobs()) == 1


async def test_remove_task_jobs_removes_everything_of_that_task_only(sched):
    due = datetime.now(UTC) + timedelta(days=5)
    scheduling.schedule_task(1, 10, "user", due, "none", "1h,1d,3d", WAW)
    scheduling.schedule_task(2, 10, "user", due, "none", "1h", WAW)
    for jid in ("quiet_due_1", "quiet_remind_1", "snooze_1"):
        sched.add_job(lambda: None, "date", run_date=due, id=jid)
    scheduling._remove_task_jobs(1)
    assert {j.id for j in sched.get_jobs()} == {"due_2", "remind_2_1h"}


async def test_apply_timezone_reschedules_tasks_and_rejects_bad_zone(monkeypatch):
    from tgbot import settings_apply
    calls = []

    async def _set_tz(owner_id, owner_type, tz):
        calls.append(("db", tz))

    async def _tasks(owner_id, owner_type):
        return [{"id": 7, "due_at": datetime(2026, 10, 8, 9, tzinfo=ZoneInfo("UTC")), "repeat": "daily", "remind": "0"}]

    async def _morning(user_id):
        return None

    monkeypatch.setattr(settings_apply.db, "set_owner_timezone", _set_tz)
    monkeypatch.setattr(settings_apply.db, "get_tasks", _tasks)
    monkeypatch.setattr(settings_apply.db, "get_morning_summary_settings", _morning)
    monkeypatch.setattr(settings_apply, "schedule_task", lambda task_id, *a, **kw: calls.append(("task", task_id, a[-1].key)))
    monkeypatch.setattr(settings_apply, "schedule_daily_summary", lambda uid, tz: calls.append(("summary", tz.key)))

    assert await settings_apply.apply_timezone(1, "user", "Mars/Olympus") is False
    assert calls == []
    assert await settings_apply.apply_timezone(1, "user", "Asia/Almaty") is True
    assert calls == [("db", "Asia/Almaty"), ("summary", "Asia/Almaty"), ("task", 7, "Asia/Almaty")]


async def test_assigned_group_task_notifies_only_assignee(monkeypatch):
    async def _members(chat_id):
        return [1, 2, 3]
    monkeypatch.setattr(scheduling.db, "get_chat_members", _members)
    assert await scheduling._recipients(-5, "chat", {"assigned_to": 2}) == [2]
    assert await scheduling._recipients(-5, "chat", {"assigned_to": None}) == [1, 2, 3]
    assert await scheduling._recipients(9, "user", {"assigned_to": 2}) == [9]   # osobiste — bez zmian


async def test_assign_pick_rejects_non_member(monkeypatch):
    from types import SimpleNamespace
    from tgbot.handlers import task_actions
    alerts_shown, updates = [], []

    async def _authorize(call, task_id):
        return {"id": task_id, "owner_id": -5, "owner_type": "chat", "title": "x"}

    async def _lang(*a):
        return "pl"

    async def _is_member(chat_id, user_id):
        return user_id == 2

    async def _update(task_id, **kw):
        updates.append(kw)

    async def _answer(text=None, show_alert=False):
        alerts_shown.append(text)

    monkeypatch.setattr(task_actions, "_authorize_task", _authorize)
    monkeypatch.setattr(task_actions, "owner_lang", _lang)
    monkeypatch.setattr(task_actions.db, "is_chat_member", _is_member)
    monkeypatch.setattr(task_actions.db, "update_task", _update)
    call = SimpleNamespace(data="asg_10_666", answer=_answer, from_user=SimpleNamespace(id=1))
    await task_actions.assign_pick(call)
    assert updates == [] and alerts_shown == [t("pl", "not_authorized")]


# ---------- przekładanie (punkt 7) ----------

UTC_ = ZoneInfo("UTC")


def test_snooze_target_keeps_local_clock_time_across_dst():
    # 24.10.2026 w Warszawie: dzień przed zmianą czasu na zimowy (25.10)
    now = datetime(2026, 10, 24, 10, 0, tzinfo=UTC_)
    due = datetime(2026, 10, 24, 16, 0, tzinfo=UTC_)                 # 18:00 czasu letniego
    assert scheduling.snooze_target("1h", now, due, WAW) == now + timedelta(hours=1)
    tomorrow = scheduling.snooze_target("tom", now, due, WAW)
    assert tomorrow.astimezone(WAW).strftime("%d.%m %H:%M") == "25.10 18:00"   # nadal 18:00, choć offset inny
    week = scheduling.snooze_target("1w", now, due, WAW)
    assert week.astimezone(WAW).strftime("%d.%m %H:%M") == "31.10 18:00"
    assert scheduling.snooze_target("xyz", now, due, WAW) is None


def test_overdue_before_today_ignores_today_done_and_repeating():
    now = datetime(2026, 10, 15, 10, 0, tzinfo=UTC_)

    def task(days, done=False, repeat="none"):
        return {"due_at": now + timedelta(days=days), "done": done, "repeat": repeat}
    tasks = [task(-2), task(-1, done=True), task(-3, repeat="daily"), task(0), task(1)]
    assert scheduling.overdue_before_today(tasks, WAW, now) == [tasks[0]]


def test_move_to_today_same_clock_time_or_in_an_hour():
    now = datetime(2026, 10, 15, 10, 0, tzinfo=UTC_)                  # 12:00 w Warszawie
    later = scheduling.move_to_today(datetime(2026, 10, 12, 16, 0, tzinfo=UTC_), WAW, now)
    assert later.astimezone(WAW).strftime("%d.%m %H:%M") == "15.10 18:00"
    passed = scheduling.move_to_today(datetime(2026, 10, 12, 6, 0, tzinfo=UTC_), WAW, now)
    assert passed == now + timedelta(hours=1)
