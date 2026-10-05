from datetime import date, timedelta

from stats import compute_streak, daily_series, totals

TODAY = date(2026, 10, 4)


def d(n):
    return TODAY - timedelta(days=n)


def test_streak_continuous_including_today():
    assert compute_streak([d(0), d(1), d(2)], TODAY) == 3


def test_streak_not_broken_if_today_empty_yet():
    assert compute_streak([d(1), d(2)], TODAY) == 2


def test_streak_broken_after_a_missed_day():
    assert compute_streak([d(2), d(3)], TODAY) == 0
    assert compute_streak([d(0), d(1), d(3)], TODAY) == 2


def test_streak_empty_and_duplicates():
    assert compute_streak([], TODAY) == 0
    assert compute_streak([d(0), d(0), d(1)], TODAY) == 2


def test_streak_ignores_future_days():
    assert compute_streak([d(-1), d(0)], TODAY) == 1


def test_totals():
    rows = [
        {"day": d(0), "event": "done", "cnt": 2}, {"day": d(1), "event": "done", "cnt": 1},
        {"day": d(0), "event": "created", "cnt": 5}, {"day": d(1), "event": "weird", "cnt": 9},
    ]
    assert totals(rows) == {"created": 5, "done": 3, "deleted": 0}


def test_daily_series_fills_gaps_and_is_ordered():
    rows = [{"day": d(0), "event": "done", "cnt": 3}, {"day": d(2), "event": "done", "cnt": 1},
            {"day": d(0), "event": "created", "cnt": 9}]
    assert daily_series(rows, "done", 4, TODAY) == [(d(3), 0), (d(2), 1), (d(1), 0), (d(0), 3)]


def test_daily_series_length_and_empty():
    s = daily_series([], "done", 14, TODAY)
    assert len(s) == 14 and s[-1][0] == TODAY and all(c == 0 for _, c in s)
