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


# ---------- батч-выборки для дашборда ----------
# Дашборд рендерит десятки карточек за раз и раньше делал по два запроса на
# КАЖДУЮ задачу; эти функции должны отдавать ровно то же, что поштучные
# get_task_photos_meta/get_subtasks, но одним запросом.

async def test_get_task_photos_meta_bulk_matches_per_task_version(clean_db):
    due = datetime.now(WAW) + timedelta(days=1)
    t1 = await db.add_task(1, "user", "a", due, "none", "0")
    t2 = await db.add_task(1, "user", "b", due, "none", "0")
    t3 = await db.add_task(1, "user", "без фото", due, "none", "0")
    await db.add_task_photo(t1, b"\x01", "image/jpeg", uploaded_by=1)
    await db.add_task_photo(t1, b"\x02", "image/jpeg", uploaded_by=2)
    await db.add_task_photo(t2, b"\x03", "image/jpeg")

    bulk = await db.get_task_photos_meta_bulk([t1, t2, t3])
    assert bulk[t1] == await db.get_task_photos_meta(t1)
    assert bulk[t2] == await db.get_task_photos_meta(t2)
    assert t3 not in bulk                      # задачи без фото в словарь не попадают
    assert "data" not in bulk[t1][0]           # байты не тащим, как и в поштучной версии


async def test_get_subtasks_bulk_matches_per_task_version_and_order(clean_db):
    due = datetime.now(WAW) + timedelta(days=1)
    t1 = await db.add_task(1, "user", "a", due, "none", "0")
    t2 = await db.add_task(1, "user", "b", due, "none", "0")
    for title in ("первый", "второй", "третий"):
        await db.add_subtask(t1, title)
    await db.add_subtask(t2, "один")

    bulk = await db.get_subtasks_bulk([t1, t2])
    assert bulk[t1] == await db.get_subtasks(t1)
    assert [st["title"] for st in bulk[t1]] == ["первый", "второй", "третий"]  # порядок по position
    assert bulk[t2] == await db.get_subtasks(t2)


async def test_bulk_helpers_on_empty_and_unknown_ids(clean_db):
    assert await db.get_task_photos_meta_bulk([]) == {}
    assert await db.get_subtasks_bulk([]) == {}
    assert await db.get_task_photos_meta_bulk([999999]) == {}
    assert await db.get_subtasks_bulk([999999]) == {}


# ---------- выборки для страницы дашборда ----------

async def _dashboard_dataset(owner_id=7):
    """Разнообразный набор: просроченные, на сегодня, будущие, много выполненных, теги."""
    now = datetime.now(WAW)
    rows = [
        ("overdue", now - timedelta(days=1), False, None),
        ("soon", now + timedelta(minutes=1), False, "дом"),
        ("later", now + timedelta(days=3), False, "работа"),
        ("later2", now + timedelta(days=9), False, "дом"),
        ("done-today", now - timedelta(minutes=5), True, "дом"),
    ]
    rows += [(f"old-{i}", now - timedelta(days=2 + i), True, "дом" if i % 2 else "работа") for i in range(12)]
    ids = {}
    for title, due, done, tag in rows:
        ids[title] = await db.add_task(owner_id, "user", title, due, "none", "0", tag)
        if done:
            await db.mark_done(ids[title])
    return ids


async def test_dashboard_tasks_limits_done_but_keeps_all_active(clean_db):
    ids = await _dashboard_dataset()
    tasks = await db.get_dashboard_tasks(7, "user", 3)
    titles = [t["title"] for t in tasks]
    assert [t for t in titles if not t.startswith(("old-", "done-"))] == ["overdue", "soon", "later", "later2"]
    done = [t for t in tasks if t["done"]]
    assert [t["title"] for t in sorted(done, key=lambda t: t["due_at"], reverse=True)] == ["done-today", "old-0", "old-1"]
    assert all(not t["done"] for t in tasks[:4]) and ids  # порядок как у get_tasks: активные раньше выполненных


async def test_dashboard_tasks_tag_filter_and_day_window(clean_db):
    await _dashboard_dataset()
    only_home = await db.get_dashboard_tasks(7, "user", 2, tag="дом")
    done_home = [t for t in only_home if t["done"]]
    assert len(done_home) == 2 and all(t["tag"] == "дом" for t in done_home)

    old5 = datetime.now(WAW) - timedelta(days=7)          # old-5 выпадает из «последних 2», но день его покрывает
    start = old5.replace(hour=0, minute=0, second=0, microsecond=0)
    in_day = await db.get_dashboard_tasks(7, "user", 2, day_start=start, day_end=start + timedelta(days=1))
    assert "old-5" in [t["title"] for t in in_day]


async def test_dashboard_stats_match_python_helpers(clean_db):
    """SQL-агрегаты обязаны давать то же, что считал Python по всем задачам."""
    from task_groups import day_counts, tag_counts, today_progress

    await _dashboard_dataset()
    now = datetime.now(WAW)
    all_tasks = await db.get_tasks(7, "user", include_done=True)
    local_due = {t["id"]: t["due_at"].astimezone(WAW) for t in all_tasks}
    lo, hi = now.date() - timedelta(days=40), now.date() + timedelta(days=40)

    stats = await db.get_dashboard_stats(7, "user", "Europe/Warsaw", None, lo, hi)
    assert (stats["today"], stats["overdue"]) == day_counts(all_tasks, local_due, now)
    expected = today_progress(all_tasks, local_due, now.date())
    got = None if stats["today_total"] == 0 else round(100 * stats["today_done"] / stats["today_total"])
    assert got == expected
    assert stats["done_total"] == sum(1 for t in all_tasks if t["done"])
    assert stats["days"] == {d.date() for d in local_due.values() if lo <= d.date() <= hi}
    assert sorted(stats["tags"], key=lambda kv: kv[0]) == sorted(tag_counts(all_tasks), key=lambda kv: kv[0])

    tagged = await db.get_dashboard_stats(7, "user", "Europe/Warsaw", "дом", lo, hi)
    assert tagged["done_total"] == sum(1 for t in all_tasks if t["done"] and t.get("tag") == "дом")


async def test_dashboard_owner_cache_is_invalidated_on_password_change(clean_db):
    token = await db.set_owner_dashboard_credentials(11, "user", "hash-1")
    first = await db.get_owner_by_dashboard_token(token)
    assert first["dashboard_password_hash"] == "hash-1" and first["dashboard_password_version"] == 1
    assert await db.get_owner_by_dashboard_token(token) == first                      # из кэша
    new_token = await db.set_owner_dashboard_credentials(11, "user", "hash-2")
    assert await db.get_owner_by_dashboard_token(token) is None                         # старый токен мёртв сразу
    fresh = await db.get_owner_by_dashboard_token(new_token)
    assert fresh["dashboard_password_version"] == 2
    await db.set_owner_timezone(11, "user", "Asia/Almaty")
    assert (await db.get_owner_by_dashboard_token(new_token))["timezone"] == "Asia/Almaty"


# ---------- czyszczenie danych i grupy (Mini App) ----------

async def test_purge_done_tasks_removes_only_old_done_and_cascades(clean_db):
    due = datetime.now(WAW) + timedelta(days=1)
    old_done = await db.add_task(1, "user", "stare", due, "none", "0")
    new_done = await db.add_task(1, "user", "świeże", due, "none", "0")
    active = await db.add_task(1, "user", "aktywne", due, "none", "0")
    await db.add_subtask(old_done, "podzadanie")
    for tid in (old_done, new_done):
        await db.mark_done(tid)
    async with db._pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET done_at = now() - interval '8 days' WHERE id = $1", old_done)
    assert await db.purge_done_tasks(7) == 1
    assert await db.get_task(old_done) is None
    assert await db.get_task(new_done) and await db.get_task(active)
    assert await db.get_subtasks(old_done) == []                  # kaskada


async def test_purge_old_history(clean_db):
    await add_event(1, 1, "done", days_ago=100)
    await add_event(1, 1, "done", days_ago=3)
    assert await db.purge_old_history(90) == 1
    assert len(await db.get_history(1)) == 1


async def test_user_group_dashboards_membership_and_removal(clean_db):
    await db.add_chat_member(-10, 5)
    await db.add_chat_member(-20, 6)
    await db.set_chat_title(-10, "Rodzina")
    groups = await db.get_user_group_dashboards(5, create_missing=True)
    assert [(g["chat_id"], g["title"]) for g in groups] == [(-10, "Rodzina")]
    assert groups[0]["token"]
    again = await db.get_user_group_dashboards(5, create_missing=True)
    assert again[0]["token"] == groups[0]["token"]                # token stabilny
    await db.remove_chat_member(-10, 5)
    assert await db.get_user_group_dashboards(5) == []
    assert await db.is_chat_member(-20, 6)                        # cudza grupa nietknięta
    await db.forget_chat_members(-20)
    assert not await db.is_chat_member(-20, 6)


async def test_assigned_to_roundtrip(clean_db):
    tid = await db.add_task(-10, "chat", "wspólne", datetime.now(WAW) + timedelta(days=1), "none", "0")
    assert (await db.get_task(tid))["assigned_to"] is None
    await db.update_task(tid, assigned_to=5)
    assert (await db.get_task(tid))["assigned_to"] == 5
    await db.update_task(tid, assigned_to=None)
    assert (await db.get_task(tid))["assigned_to"] is None
