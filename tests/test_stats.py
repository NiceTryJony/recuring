"""Юнит-тесты для stats.py. Чистые функции без БД/Telegram — запускать просто
`python3 -m pytest test_stats.py` (или `python3 test_stats.py` без pytest,
см. блок __main__ внизу)."""

from datetime import date, timedelta

from stats import compute_streak, totals, daily_series


TODAY = date(2026, 10, 7)  # фиксированная "сегодня" для воспроизводимости тестов


# ---------- compute_streak ----------

def test_streak_empty_history():
    assert compute_streak([], TODAY) == 0


def test_streak_done_today_continues_run():
    days = {TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=2)}
    assert compute_streak(days, TODAY) == 3


def test_streak_not_done_today_counts_from_yesterday():
    # Сегодня ещё пусто, но вчера/позавчера было — серия не должна обнуляться
    # раньше времени (пользователю ещё есть время выполнить что-то сегодня).
    days = {TODAY - timedelta(days=1), TODAY - timedelta(days=2)}
    assert compute_streak(days, TODAY) == 2


def test_streak_broken_by_gap():
    # Разрыв 2 дня назад — серия должна остановиться на вчера, не доходя до
    # более старых дней по ту сторону разрыва.
    days = {TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=3)}
    assert compute_streak(days, TODAY) == 2


def test_streak_single_old_day_no_gap_to_today():
    # Последнее выполнение было 3 дня назад — ни сегодня, ни вчера пусто,
    # серия должна быть 0 (разрыв сразу от "вчера").
    days = {TODAY - timedelta(days=3)}
    assert compute_streak(days, TODAY) == 0


# ---------- totals ----------

def test_totals_sums_by_event_ignoring_unknown():
    rows = [
        {"event": "created", "cnt": 5},
        {"event": "done", "cnt": 3},
        {"event": "done", "cnt": 2},
        {"event": "deleted", "cnt": 1},
        {"event": "rescheduled", "cnt": 99},  # не в EVENTS — должен игнорироваться
    ]
    result = totals(rows)
    assert result == {"created": 5, "done": 5, "deleted": 1}


def test_totals_empty_rows():
    assert totals([]) == {"created": 0, "done": 0, "deleted": 0}


# ---------- daily_series ----------

def test_daily_series_fills_gaps_with_zero():
    rows = [
        {"day": TODAY, "event": "done", "cnt": 2},
        {"day": TODAY - timedelta(days=2), "event": "done", "cnt": 1},
    ]
    series = daily_series(rows, "done", days=3, today=TODAY)
    assert series == [
        (TODAY - timedelta(days=2), 1),
        (TODAY - timedelta(days=1), 0),
        (TODAY, 2),
    ]


def test_daily_series_filters_by_event():
    rows = [
        {"day": TODAY, "event": "created", "cnt": 10},
        {"day": TODAY, "event": "done", "cnt": 4},
    ]
    series = daily_series(rows, "done", days=1, today=TODAY)
    assert series == [(TODAY, 4)]


def test_daily_series_sums_duplicate_day_rows():
    # get_history_stats группирует по (day, event) на стороне SQL, так что на
    # практике дублей по одному дню/событию быть не должно — но функция не
    # должна молча терять данные, если они всё же пришли.
    rows = [
        {"day": TODAY, "event": "done", "cnt": 1},
        {"day": TODAY, "event": "done", "cnt": 1},
    ]
    series = daily_series(rows, "done", days=1, today=TODAY)
    assert series == [(TODAY, 2)]


if __name__ == "__main__":
    # Без pytest: просто вызываем все test_* функции модуля по очереди.
    import sys
    failures = 0
    tests = {name: fn for name, fn in list(globals().items()) if name.startswith("test_")}
    for name, fn in tests.items():
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
