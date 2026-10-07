"""Юнит-тесты для date_parser.py. Чистые функции без БД/Telegram — запускать
`python3 test_date_parser.py` (pytest не обязателен, см. блок __main__)."""

from datetime import datetime, time

from date_parser import (
    parse_human_date, parse_quiet_hours, is_quiet_time, next_quiet_end, split_title_and_date,
)


# ---------- parse_quiet_hours ----------

def test_quiet_hours_valid():
    assert parse_quiet_hours("23:00-08:00") == (time(23, 0), time(8, 0))


def test_quiet_hours_accepts_dot_and_dash_variants():
    assert parse_quiet_hours("23.00 – 08.00") == (time(23, 0), time(8, 0))


def test_quiet_hours_rejects_equal_start_end():
    assert parse_quiet_hours("10:00-10:00") is None


def test_quiet_hours_rejects_out_of_range():
    assert parse_quiet_hours("25:00-08:00") is None
    assert parse_quiet_hours("10:00-08:61") is None


def test_quiet_hours_rejects_garbage():
    assert parse_quiet_hours("выкл") is None
    assert parse_quiet_hours("") is None


# ---------- is_quiet_time ----------

def test_is_quiet_time_same_day_window():
    s, e = time(13, 0), time(14, 0)
    assert is_quiet_time(time(13, 0), s, e) is True   # начало включено
    assert is_quiet_time(time(13, 30), s, e) is True
    assert is_quiet_time(time(14, 0), s, e) is False  # конец не включён
    assert is_quiet_time(time(12, 59), s, e) is False


def test_is_quiet_time_crosses_midnight():
    s, e = time(23, 0), time(8, 0)
    assert is_quiet_time(time(23, 0), s, e) is True
    assert is_quiet_time(time(23, 59), s, e) is True
    assert is_quiet_time(time(0, 0), s, e) is True
    assert is_quiet_time(time(7, 59), s, e) is True
    assert is_quiet_time(time(8, 0), s, e) is False   # конец не включён
    assert is_quiet_time(time(12, 0), s, e) is False


def test_is_quiet_time_equal_start_end_always_false():
    # defensive: parse_quiet_hours такое не пропустит, но функция всё равно
    # не должна молча говорить "весь день тихий час".
    assert is_quiet_time(time(10, 0), time(5, 0), time(5, 0)) is False


# ---------- next_quiet_end ----------

def test_next_quiet_end_same_day():
    s, e = time(13, 0), time(14, 0)
    now = datetime(2026, 10, 7, 13, 30)
    assert next_quiet_end(now, s, e) == datetime(2026, 10, 7, 14, 0)


def test_next_quiet_end_crosses_midnight_before_midnight():
    s, e = time(23, 0), time(8, 0)
    now = datetime(2026, 10, 7, 23, 30)
    assert next_quiet_end(now, s, e) == datetime(2026, 10, 8, 8, 0)


def test_next_quiet_end_crosses_midnight_after_midnight():
    s, e = time(23, 0), time(8, 0)
    now = datetime(2026, 10, 8, 2, 0)
    assert next_quiet_end(now, s, e) == datetime(2026, 10, 8, 8, 0)


# ---------- parse_human_date ----------

NOW = datetime(2026, 10, 7, 12, 0)  # вторник, 12:00


def test_strict_format_with_time():
    assert parse_human_date("15.10.2026 18:00", NOW) == datetime(2026, 10, 15, 18, 0)


def test_strict_format_date_only_defaults_to_9am():
    assert parse_human_date("15.10.2026", NOW) == datetime(2026, 10, 15, 9, 0)


def test_tomorrow_with_time():
    assert parse_human_date("завтра 18:00", NOW) == datetime(2026, 10, 8, 18, 0)
    assert parse_human_date("tomorrow at 18:00", NOW) == datetime(2026, 10, 8, 18, 0)
    assert parse_human_date("jutro o 18:00", NOW) == datetime(2026, 10, 8, 18, 0)


def test_tomorrow_without_time_defaults_to_9am():
    assert parse_human_date("завтра", NOW) == datetime(2026, 10, 8, 9, 0)


def test_today_without_time_keeps_current_time():
    assert parse_human_date("сегодня", NOW) == NOW


def test_relative_amount_with_unit():
    assert parse_human_date("через 3 дня", NOW) == NOW.replace(day=10)
    assert parse_human_date("через 2 часа", NOW) == datetime(2026, 10, 7, 14, 0)
    assert parse_human_date("in 2 hours", NOW) == datetime(2026, 10, 7, 14, 0)
    assert parse_human_date("za 3 dni", NOW) == NOW.replace(day=10)


def test_relative_amount_with_time_override():
    # "через 3 дня в 09:30" — число дней для даты, время — переопределяется явно
    result = parse_human_date("через 3 дня в 09:30", NOW)
    assert result == datetime(2026, 10, 10, 9, 30)


def test_relative_no_number_phrases():
    assert parse_human_date("через час", NOW) == datetime(2026, 10, 7, 13, 0)
    assert parse_human_date("через минуту", NOW) == datetime(2026, 10, 7, 12, 1)
    assert parse_human_date("через неделю", NOW) == datetime(2026, 10, 14, 12, 0)
    assert parse_human_date("через день", NOW) == datetime(2026, 10, 8, 12, 0)


def test_invalid_hour_returns_none_not_exception():
    # Регекс пропускает 1-2 цифры, не глядя на допустимый диапазон 0-23/0-59 —
    # .replace(hour=99) должен кинуть ValueError, пойманный внутри parse_human_date.
    assert parse_human_date("завтра в 99:99", NOW) is None
    assert parse_human_date("25.02.2026 25:00", NOW) is None


def test_invalid_calendar_date_returns_none():
    assert parse_human_date("31.02.2026 10:00", NOW) is None  # 31 февраля не существует


def test_garbage_returns_none():
    assert parse_human_date("как дела", NOW) is None
    assert parse_human_date("", NOW) is None


# ---------- split_title_and_date ----------

def test_split_title_and_date_basic():
    title, dt = split_title_and_date("Купить молоко завтра в 18:00", NOW)
    assert title == "Купить молоко"
    assert dt == datetime(2026, 10, 8, 18, 0)


def test_split_title_and_date_strips_trailing_preposition():
    title, dt = split_title_and_date("Позвонить маме на завтра", NOW)
    assert title == "Позвонить маме"
    assert dt == datetime(2026, 10, 8, 9, 0)


def test_split_title_and_date_whole_string_is_date_returns_none():
    # Вся строка — сама дата, заголовка не остаётся: не считаем это парой
    # (заголовок, дата), чтобы вызывающий код запросил дату отдельным шагом.
    title, dt = split_title_and_date("завтра", NOW)
    assert (title, dt) == ("завтра", None)


def test_split_title_and_date_no_date_in_text():
    title, dt = split_title_and_date("Просто заметка без даты", NOW)
    assert (title, dt) == ("Просто заметка без даты", None)


if __name__ == "__main__":
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
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
