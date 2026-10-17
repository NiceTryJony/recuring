"""repeats.py: следующий срок повторяющейся задачи после «Готово» / «Пропустить»."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from repeats import after_days, is_one_shot, next_due

WAW = ZoneInfo("Europe/Warsaw")
UTC = ZoneInfo("UTC")


def _utc(*a):
    return datetime(*a, tzinfo=WAW).astimezone(UTC)


@pytest.mark.parametrize("code, n", [("after_3d", 3), ("after_14d", 14), ("after_0d", None), ("daily", None),
                                     ("none", None), (None, None), ("after_xd", None)])
def test_after_days(code, n):
    assert after_days(code) == n


def test_one_shot_kinds():
    assert is_one_shot("none") and is_one_shot("after_3d")
    assert not is_one_shot("daily") and not is_one_shot("monthly_nth_weekday")


def test_none_has_no_next():
    assert next_due("none", _utc(2026, 10, 1, 9), _utc(2026, 10, 1, 8), WAW) is None


def test_daily_done_early_moves_to_tomorrow_same_time():
    # закрыли сегодняшний раз утром, до срока — следующий завтра в 18:00, а не сегодня
    assert next_due("daily", _utc(2026, 10, 7, 18), _utc(2026, 10, 7, 10), WAW) == _utc(2026, 10, 8, 18)


def test_daily_long_missed_jumps_to_future_not_past():
    # неделю не отмечали: одно нажатие — следующий раз в будущем
    nxt = next_due("daily", _utc(2026, 10, 1, 9), _utc(2026, 10, 8, 12), WAW)
    assert nxt == _utc(2026, 10, 9, 9)


def test_weekly_and_monthly_keep_calendar():
    assert next_due("weekly", _utc(2026, 10, 6, 9), _utc(2026, 10, 6, 8), WAW) == _utc(2026, 10, 13, 9)
    assert next_due("monthly", _utc(2026, 10, 15, 9), _utc(2026, 10, 15, 8), WAW) == _utc(2026, 11, 15, 9)


def test_local_time_survives_dst_change():
    # 25.10.2026 Польша переходит на зимнее время: местные 9:00 остаются 9:00
    nxt = next_due("daily", _utc(2026, 10, 24, 9), _utc(2026, 10, 24, 8), WAW)
    assert nxt.astimezone(WAW).hour == 9 and nxt.astimezone(WAW).day == 25


def test_after_days_done_counts_from_today():
    # срок был 5-го, полили 7-го — следующий полив 10-го в то же время
    assert next_due("after_3d", _utc(2026, 10, 5, 19), _utc(2026, 10, 7, 12), WAW) == _utc(2026, 10, 10, 19)


def test_after_days_skip_counts_from_old_due():
    assert next_due("after_3d", _utc(2026, 10, 7, 19), _utc(2026, 10, 7, 12), WAW,
                    completed=False) == _utc(2026, 10, 10, 19)


@pytest.mark.parametrize("repeat", ["daily", "weekly", "monthly", "yearly", "weekdays", "monthly_nth_weekday",
                                    "after_2d", "after_7d"])
@pytest.mark.parametrize("completed", [True, False])
def test_next_is_always_in_future(repeat, completed):
    due, now = _utc(2026, 1, 31, 23, 30), _utc(2026, 3, 1, 0, 15)
    nxt = next_due(repeat, due, now, WAW, completed=completed)
    assert nxt > now and nxt > due
    assert nxt - now < timedelta(days=370)
