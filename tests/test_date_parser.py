from datetime import datetime, time

import pytest

from date_parser import is_quiet_time, next_quiet_end, parse_human_date, parse_quiet_hours

NOW = datetime(2026, 10, 4, 12, 0)  # воскресенье, 12:00


@pytest.mark.parametrize("text, expected", [
    # строгие форматы
    ("15.10.2026 18:00", datetime(2026, 10, 15, 18, 0)),
    ("15.10.2026 9:05", datetime(2026, 10, 15, 9, 5)),
    ("15.10.2026", datetime(2026, 10, 15, 9, 0)),
    # завтра / сегодня (ru, en, pl)
    ("завтра", datetime(2026, 10, 5, 9, 0)),
    ("завтра 18:00", datetime(2026, 10, 5, 18, 0)),
    ("Tomorrow 7.30", datetime(2026, 10, 5, 7, 30)),
    ("jutro 18:00", datetime(2026, 10, 5, 18, 0)),
    ("сегодня 20:15", datetime(2026, 10, 4, 20, 15)),
    ("today 20:15", datetime(2026, 10, 4, 20, 15)),
    ("dziś 20:15", datetime(2026, 10, 4, 20, 15)),
    ("сегодня", datetime(2026, 10, 4, 12, 0)),
    # через N <единица>
    ("через 3 дня", datetime(2026, 10, 7, 12, 0)),
    ("через 3 дня 10:00", datetime(2026, 10, 7, 10, 0)),
    ("через 1 день", datetime(2026, 10, 5, 12, 0)),
    ("через 2 часа", datetime(2026, 10, 4, 14, 0)),
    ("через 1 час", datetime(2026, 10, 4, 13, 0)),
    ("через 30 минут", datetime(2026, 10, 4, 12, 30)),
    ("через 1 минуту", datetime(2026, 10, 4, 12, 1)),   # регресс: раньше -> None
    ("через 30 мин", datetime(2026, 10, 4, 12, 30)),
    ("через 2 недели", datetime(2026, 10, 18, 12, 0)),
    ("через 5 недель", datetime(2026, 11, 8, 12, 0)),   # регресс: раньше -> None
    ("in 2 hours", datetime(2026, 10, 4, 14, 0)),
    ("in 5 mins", datetime(2026, 10, 4, 12, 5)),
    ("in 1 minute", datetime(2026, 10, 4, 12, 1)),
    ("in 3 days 10:00", datetime(2026, 10, 7, 10, 0)),
    ("in 2 weeks", datetime(2026, 10, 18, 12, 0)),
    ("za 3 dni 10:00", datetime(2026, 10, 7, 10, 0)),
    ("za 2 tygodnie", datetime(2026, 10, 18, 12, 0)),
    ("za 5 minut", datetime(2026, 10, 4, 12, 5)),
    ("za 2 godziny", datetime(2026, 10, 4, 14, 0)),
    # без числа
    ("через час", datetime(2026, 10, 4, 13, 0)),
    ("через   час", datetime(2026, 10, 4, 13, 0)),      # лишние пробелы
    ("in an hour", datetime(2026, 10, 4, 13, 0)),
    ("za godzinę", datetime(2026, 10, 4, 13, 0)),
    ("через минуту", datetime(2026, 10, 4, 12, 1)),
    ("через неделю", datetime(2026, 10, 11, 12, 0)),
    ("in a day", datetime(2026, 10, 5, 12, 0)),
    ("  ЗАВТРА 18:00  ", datetime(2026, 10, 5, 18, 0)),  # регистр и обрамляющие пробелы
])
def test_parse_valid(text, expected):
    assert parse_human_date(text, NOW) == expected


@pytest.mark.parametrize("text", [
    "", "мусор", "завтра 25:00", "завтра 12:75", "через 3 парсека", "32.13.2026 10:00",
    "через час 18:00", "tomorrow at 18",
])
def test_parse_invalid_returns_none_and_never_raises(text):
    assert parse_human_date(text, NOW) is None


def test_parse_result_is_naive():
    assert parse_human_date("завтра 18:00", NOW).tzinfo is None


# ---------- тихий час ----------

@pytest.mark.parametrize("text, expected", [
    ("23:00-08:00", (time(23, 0), time(8, 0))),
    ("23.00 – 8:00", (time(23, 0), time(8, 0))),
    (" 22:30—07:15 ", (time(22, 30), time(7, 15))),
    ("13:00-15:00", (time(13, 0), time(15, 0))),
])
def test_parse_quiet_hours_valid(text, expected):
    assert parse_quiet_hours(text) == expected


@pytest.mark.parametrize("text", ["10:00-10:00", "25:00-08:00", "23:00-24:00", "23:60-08:00", "23:00", "abc", ""])
def test_parse_quiet_hours_invalid(text):
    assert parse_quiet_hours(text) is None


NIGHT = (time(23, 0), time(8, 0))


@pytest.mark.parametrize("t, expected", [
    (time(22, 59), False), (time(23, 0), True), (time(0, 0), True), (time(2, 0), True),
    (time(7, 59), True), (time(8, 0), False),   # конец не включается
    (time(12, 0), False),
])
def test_is_quiet_time_overnight(t, expected):
    assert is_quiet_time(t, *NIGHT) is expected


@pytest.mark.parametrize("t, expected", [
    (time(12, 59), False), (time(13, 0), True), (time(14, 59), True), (time(15, 0), False),
])
def test_is_quiet_time_same_day_window(t, expected):
    assert is_quiet_time(t, time(13, 0), time(15, 0)) is expected


def test_is_quiet_time_equal_bounds_is_disabled():
    assert is_quiet_time(time(10, 0), time(10, 0), time(10, 0)) is False


@pytest.mark.parametrize("now, start_end, expected", [
    (datetime(2026, 10, 4, 23, 30), NIGHT, datetime(2026, 10, 5, 8, 0)),   # до полуночи -> завтра утром
    (datetime(2026, 10, 5, 2, 0), NIGHT, datetime(2026, 10, 5, 8, 0)),     # после полуночи -> сегодня утром
    (datetime(2026, 10, 5, 14, 0), (time(13), time(15)), datetime(2026, 10, 5, 15, 0)),
    (datetime(2026, 12, 31, 23, 59), NIGHT, datetime(2027, 1, 1, 8, 0)),   # переход через год
])
def test_next_quiet_end(now, start_end, expected):
    assert next_quiet_end(now, *start_end) == expected
