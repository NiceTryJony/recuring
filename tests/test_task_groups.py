from datetime import date, datetime, timedelta, timezone

from task_groups import day_counts, group_tasks, select_tasks, tag_counts

TODAY = date(2026, 5, 10)


def _mk(tid, days, tag=None, done=False):
    due = datetime(2026, 5, 10, 12, tzinfo=timezone.utc) + timedelta(days=days)
    return {"id": tid, "due_at": due, "tag": tag, "done": done}


TASKS = [_mk(1, -1, "work"), _mk(2, 0, "home"), _mk(3, 2, None), _mk(4, 3, "work"),
         _mk(5, -5, "work", done=True), _mk(6, -2, None, done=True)]
LOCAL = {t["id"]: t["due_at"] for t in TASKS}


def _sel(**kw):
    args = dict(view="all", today=TODAY, selected_date=TODAY, filter_active=False, done_limit=10)
    args.update(kw)
    return select_tasks(TASKS, LOCAL, **args)


def _ids(res):
    return [t["id"] for t in res[0]]


def test_all_keeps_active_then_recent_done_and_counts_hidden():
    assert _ids(_sel()) == [1, 2, 3, 4, 6, 5]
    assert _sel(done_limit=1)[1] == 1


def test_hide_done_drops_done_from_all_view():
    assert _sel(hide_done=True) == ([TASKS[0], TASKS[1], TASKS[2], TASKS[3]], 0)
    assert _ids(_sel(view="done", hide_done=True)) == [6, 5]  # вкладка «Выполнено» не затронута


def test_today_includes_overdue_and_excludes_future_and_done():
    assert _ids(_sel(view="today")) == [1, 2]


def test_upcoming_and_done_views():
    assert _ids(_sel(view="upcoming")) == [3, 4]
    assert _ids(_sel(view="done")) == [6, 5]


def test_tag_filter_and_date_filter_override_view():
    assert _ids(_sel(tag="work", view="upcoming")) == [4]
    assert _ids(_sel(filter_active=True, selected_date=date(2026, 5, 12), view="done")) == [3]


def test_group_by_tag_sorts_alphabetically_untagged_last():
    sel = _sel()[0]
    keys = [k for k, _ in group_tasks(sel, LOCAL, "tag")]
    assert keys == ["home", "work", ""]


def test_group_by_date_and_none():
    sel = _sel(view="upcoming")[0]
    assert [k for k, _ in group_tasks(sel, LOCAL, "date")] == [date(2026, 5, 12), date(2026, 5, 13)]
    assert group_tasks(sel, LOCAL, "none") == [(None, sel)]
    assert group_tasks([], LOCAL, "none") == []


def test_tag_counts_ignore_done_and_untagged():
    assert tag_counts(TASKS) == [("home", 1), ("work", 2)]


def test_day_counts_split_overdue_and_today():
    now = datetime(2026, 5, 10, 13, tzinfo=timezone.utc)
    # id1 вчера (просрочена), id2 сегодня 12:00 (уже прошла), id3/4 позже
    assert day_counts(TASKS, LOCAL, now) == (0, 2)
    assert day_counts(TASKS, LOCAL, datetime(2026, 5, 10, 8, tzinfo=timezone.utc)) == (1, 1)


def test_today_progress():
    from task_groups import today_progress
    assert today_progress(TASKS, LOCAL, TODAY) == 0                 # на сегодня одна невыполненная
    assert today_progress(TASKS, LOCAL, date(2026, 5, 5)) == 100    # одна выполненная
    assert today_progress(TASKS, LOCAL, date(2030, 1, 1)) is None
