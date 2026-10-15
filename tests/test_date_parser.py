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


# ---------- rozszerzony parser: tabela przypadków ----------
# NOW = środa 2026-10-07 12:00. Nowy przypadek = jedna linijka w tabeli.

import pytest  # noqa: E402

from date_parser import split_repeat  # noqa: E402

D = datetime

PARSE_CASES = [
    # sama godzina: dziś, a jeśli minęła — jutro
    ("18:00", D(2026, 10, 7, 18)), ("8:00", D(2026, 10, 8, 8)), ("o 18", D(2026, 10, 7, 18)),
    ("в 18", D(2026, 10, 7, 18)), ("at 6 pm", D(2026, 10, 7, 18)), ("6pm", D(2026, 10, 7, 18)),
    ("6:30 pm", D(2026, 10, 7, 18, 30)), ("18h", D(2026, 10, 7, 18)), ("o 7 rano", D(2026, 10, 8, 7)),
    ("в 6 вечера", D(2026, 10, 7, 18)), ("в 3 ночи", D(2026, 10, 8, 3)), ("в 11 ночи", D(2026, 10, 7, 23)),
    ("w południe", D(2026, 10, 8, 12)), ("o północy", D(2026, 10, 8, 0)), ("midnight", D(2026, 10, 8, 0)),
    ("в 10 часов", D(2026, 10, 8, 10)), ("o 12.10", D(2026, 10, 7, 12, 10)),
    # dzień + godzina w dowolnej kolejności, pory dnia
    ("jutro o 18", D(2026, 10, 8, 18)), ("в 18 завтра", D(2026, 10, 8, 18)), ("завтра утром", D(2026, 10, 8, 9)),
    ("сегодня вечером", D(2026, 10, 7, 19)), ("tonight", D(2026, 10, 7, 20)), ("dziś wieczorem", D(2026, 10, 7, 19)),
    ("pojutrze", D(2026, 10, 9, 9)), ("послезавтра в 9:30", D(2026, 10, 9, 9, 30)),
    ("day after tomorrow at 10am", D(2026, 10, 9, 10)), ("tomorrow morning", D(2026, 10, 8, 9)),
    ("jutro 12.10", D(2026, 10, 8, 12, 10)), ("na jutro", D(2026, 10, 8, 9)),
    # dni tygodnia: najbliższy; dzisiejszy tylko gdy godzina jeszcze przed nami
    ("w piątek", D(2026, 10, 9, 9)), ("в пятницу", D(2026, 10, 9, 9)), ("до пятницы", D(2026, 10, 9, 9)),
    ("do piątku", D(2026, 10, 9, 9)), ("by friday", D(2026, 10, 9, 9)), ("next monday", D(2026, 10, 12, 9)),
    ("w przyszły wtorek", D(2026, 10, 13, 9)), ("в следующий понедельник", D(2026, 10, 12, 9)),
    ("в среду в 13:00", D(2026, 10, 7, 13)), ("w środę o 10", D(2026, 10, 14, 10)), ("wednesday", D(2026, 10, 14, 9)),
    # daty
    ("20.10", D(2026, 10, 20, 9)), ("20.10 15:00", D(2026, 10, 20, 15)), ("12.10", D(2026, 10, 12, 9)),
    ("20.01", D(2027, 1, 20, 9)), ("20/10", D(2026, 10, 20, 9)), ("2026-10-20", D(2026, 10, 20, 9)),
    ("20-10-2026", D(2026, 10, 20, 9)), ("20.10.26", D(2026, 10, 20, 9)),
    ("20 października", D(2026, 10, 20, 9)), ("20 października 2026 roku", D(2026, 10, 20, 9)),
    ("20 окт", D(2026, 10, 20, 9)), ("20 октября в 18", D(2026, 10, 20, 18)), ("october 20th", D(2026, 10, 20, 9)),
    ("oct 20 2026", D(2026, 10, 20, 9)), ("20th of october", D(2026, 10, 20, 9)),
    # względne
    ("za 2 godziny", D(2026, 10, 7, 14)), ("через 1 час 30 минут", D(2026, 10, 7, 13, 30)),
    ("in 2h30m", D(2026, 10, 7, 14, 30)), ("in 2 hours and 30 minutes", D(2026, 10, 7, 14, 30)),
    ("za pół godziny", D(2026, 10, 7, 12, 30)), ("через полчаса", D(2026, 10, 7, 12, 30)),
    ("через полтора часа", D(2026, 10, 7, 13, 30)), ("in half an hour", D(2026, 10, 7, 12, 30)),
    ("через два дня", D(2026, 10, 9, 12)), ("za miesiąc", D(2026, 11, 7, 12)), ("in a year", D(2027, 10, 7, 12)),
    ("za tydzień o 9", D(2026, 10, 14, 9)),
    # okresy
    ("w weekend", D(2026, 10, 10, 9)), ("на выходных", D(2026, 10, 10, 9)), ("next week", D(2026, 10, 12, 9)),
    ("w przyszłym tygodniu", D(2026, 10, 12, 9)), ("next month", D(2026, 11, 1, 9)),
]


@pytest.mark.parametrize("text,expected", PARSE_CASES)
def test_parse_cases(text, expected):
    assert parse_human_date(text, NOW) == expected


@pytest.mark.parametrize("text", [
    "mam 2 koty", "работал 10 часов", "kup 3 jabłka", "iPhone 15 pro", "w", "as soon as",
    "завтра в понедельник",      # dwie daty naraz
    "31.02", "о 25:00",
])
def test_not_a_date(text):
    assert parse_human_date(text, NOW) is None


@pytest.mark.parametrize("text,title,expected", [
    ("Spotkanie w piątek o 10 z Anią", "Spotkanie z Anią", D(2026, 10, 9, 10)),   # data w środku
    ("Завтра в 18 позвонить маме", "позвонить маме", D(2026, 10, 8, 18)),          # data na początku
    ("Napisać raport do piątku", "Napisać raport", D(2026, 10, 9, 9)),
    ("Wizyta u lekarza 20.10 15:30", "Wizyta u lekarza", D(2026, 10, 20, 15, 30)),
    ("Room 101 cleanup next monday", "Room 101 cleanup", D(2026, 10, 12, 9)),
])
def test_split_finds_date_anywhere(text, title, expected):
    assert split_title_and_date(text, NOW) == (title, expected)


@pytest.mark.parametrize("text,rest,repeat", [
    ("Planerka co poniedziałek o 10", "Planerka poniedziałek o 10", "weekly"),
    ("Отчёт каждую пятницу в 17", "Отчёт пятницу в 17", "weekly"),
    ("Йога по средам", "Йога средам", "weekly"),
    ("Trening on mondays", "Trening mondays", "weekly"),
    ("Bieganie w każdy wtorek", "Bieganie wtorek", "weekly"),
    ("Pić wodę codziennie", "Pić wodę", "daily"),
    ("Standup every weekday at 9:30", "Standup at 9:30", "weekdays"),
    ("Rent every month", "Rent", "monthly"),
    ("Urodziny mamy co rok 5 maja", "Urodziny mamy 5 maja", "yearly"),
    # nie powtarzanie: jednorazowy dzień, zakres dni, sam tekst bez tytułu
    ("Spotkanie w piątek", "Spotkanie w piątek", None),
    ("с понедельника по пятницу работа", "с понедельника по пятницу работа", None),
    ("codziennie", "codziennie", None),
])
def test_split_repeat(text, rest, repeat):
    assert split_repeat(text) == (rest, repeat)


def test_repeat_with_weekday_gives_first_date_on_that_day():
    rest, repeat = split_repeat("Planerka co poniedziałek o 10")
    assert repeat == "weekly"
    assert split_title_and_date(rest, NOW) == ("Planerka", D(2026, 10, 12, 10))


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
