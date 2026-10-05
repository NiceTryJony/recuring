"""Интеграционные тесты SQL на реальном Postgres. Пропускаются, если TEST_DATABASE_URL не задан.
ВНИМАНИЕ: тесты делают TRUNCATE — поэтому имя базы обязано содержать 'test', иначе тесты отказываются
стартовать (защита от запуска против боевой БД)."""

import os
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

TEST_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL не задан"),
]

import db  # noqa: E402  (после conftest, который выставил DATABASE_URL)
from stats import compute_streak  # noqa: E402

WAW = ZoneInfo("Europe/Warsaw")


@pytest_asyncio.fixture
async def clean_db():
    assert "test" in urlparse(TEST_URL).path.lower(), "TEST_DATABASE_URL должен указывать на базу с 'test' в имени"
    await db.init_db()
    async with db._pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE tasks, task_history, user_settings, chat_settings, chat_members, subtasks RESTART IDENTITY CASCADE"
        )
    yield
    await db.close_db()


async def add_event(user_id, task_id, event, days_ago, hour=12):
    today = datetime.now(WAW).date()
    ts = (datetime.combine(today - timedelta(days=days_ago), datetime.min.time()) + timedelta(hours=hour)).replace(tzinfo=WAW)
    async with db._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_history (task_id, user_id, title, event, event_at) VALUES ($1, $2, 'x', $3, $4)",
            task_id, user_id, event, ts,
        )


async def test_init_db_is_idempotent(clean_db):
    await db.init_db()


async def test_remind_until_done_roundtrip(clean_db):
    due = datetime.now(WAW) + timedelta(days=1)
    t1 = await db.add_task(1, "user", "a", due, "none", "0", None, True)
    t2 = await db.add_task(1, "user", "b", due, "none", "0")
    assert (await db.get_task(t1))["remind_until_done"] is True
    assert (await db.get_task(t2))["remind_until_done"] is False


async def test_quiet_hours_roundtrip_user_and_chat(clean_db):
    from datetime import time
    await db.set_owner_quiet_hours(5, "user", time(23, 0), time(8, 0))
    await db.set_owner_quiet_hours(-5, "chat", time(22, 0), time(7, 30))
    u = await db.get_owner_settings(5, "user")
    c = await db.get_owner_settings(-5, "chat")
    assert (u["quiet_hours_start"], u["quiet_hours_end"]) == (time(23, 0), time(8, 0))
    assert (c["quiet_hours_start"], c["quiet_hours_end"]) == (time(22, 0), time(7, 30))
    await db.set_owner_quiet_hours(5, "user", None, None)
    u = await db.get_owner_settings(5, "user")
    assert u["quiet_hours_start"] is None and u["quiet_hours_end"] is None


async def test_search_tasks_treats_wildcards_literally(clean_db):
    due = datetime.now(WAW) + timedelta(days=1)
    await db.add_task(1, "user", "скидка 50% на всё", due, "none", "0")
    await db.add_task(1, "user", "a_b", due, "none", "0")
    await db.add_task(1, "user", "axb", due, "none", "0")
    assert [t["title"] for t in await db.search_tasks(1, "user", "50%")] == ["скидка 50% на всё"]
    assert [t["title"] for t in await db.search_tasks(1, "user", "a_b")] == ["a_b"]   # '_' — не «любой символ»


async def test_history_stats_done_days_and_streak(clean_db):
    today = datetime.now(WAW).date()
    task = await db.add_task(1, "user", "t", datetime.now(WAW) + timedelta(days=1), "none", "0")
    for days_ago, n in [(0, 2), (1, 1), (2, 1), (4, 1)]:
        for _ in range(n):
            await add_event(1, task, "done", days_ago)
    await add_event(1, task, "created", 0)
    await add_event(1, task, "deleted", 1)
    await add_event(1, task, "done", 40)          # вне окна 30 дней

    rows = await db.get_history_stats(1, 7, "Europe/Warsaw")
    by = {(r["day"], r["event"]): r["cnt"] for r in rows}
    assert by[(today, "done")] == 2 and by[(today, "created")] == 1 and by[(today - timedelta(days=1), "deleted")] == 1
    assert all(isinstance(r["cnt"], int) for r in rows)

    rows30 = await db.get_history_stats(1, 30, "Europe/Warsaw")
    assert sum(r["cnt"] for r in rows30 if r["event"] == "done") == 5     # 40-дневное не входит
    assert [r["event"] for r in await db.get_history_stats(1, 1, "Europe/Warsaw") if r["event"] == "deleted"] == []

    days = await db.get_done_days(1, "Europe/Warsaw")
    assert days[0] == today and today - timedelta(days=3) not in days
    assert compute_streak(days, today) == 3


async def test_history_day_boundary_uses_local_timezone(clean_db):
    """Событие в 00:00 по Варшаве — это ещё «вчера» по UTC; в варшавской статистике оно «сегодня»."""
    today = datetime.now(WAW).date()
    task = await db.add_task(1, "user", "t", datetime.now(WAW) + timedelta(days=1), "none", "0")
    await add_event(1, task, "done", 0, hour=0)
    assert (await db.get_done_days(1, "Europe/Warsaw")) == [today]
    assert (await db.get_done_days(1, "UTC")) == [today - timedelta(days=1)]


async def test_history_is_scoped_per_user_and_per_chat(clean_db):
    chat_task = await db.add_task(-500, "chat", "общая", datetime.now(WAW) + timedelta(days=1), "none", "0")
    personal = await db.add_task(1, "user", "личная", datetime.now(WAW) + timedelta(days=1), "none", "0")
    await add_event(222, chat_task, "done", 0)
    await add_event(1, chat_task, "done", 1)
    await add_event(1, personal, "done", 0)     # личная задача в статистику ЧАТА попасть не должна

    chat_rows = await db.get_history_stats(None, 7, "Europe/Warsaw", chat_id=-500)
    assert sum(r["cnt"] for r in chat_rows if r["event"] == "done") == 2
    user_rows = await db.get_history_stats(1, 7, "Europe/Warsaw")
    assert sum(r["cnt"] for r in user_rows if r["event"] == "done") == 2   # его чат-выполнение + личное
    assert await db.get_history_stats(999, 7, "Europe/Warsaw") == []
