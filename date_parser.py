"""Парсинг даты из текста на русском, английском и польском. Возвращает naive
datetime (без tzinfo) — вызывающий код сам прикручивает часовой пояс пользователя.

Как устроено: текст режется на слова, и КАЖДОЕ слово должно быть опознано как
кусок даты/времени или служебное слово («в», «at», «o», «до»…); порядок любой
(«завтра в 18» == «в 18 завтра»). Хоть одно незнакомое слово — это не дата, так
«mam 2 koty» не превращается в срок. Опознанные куски складываются в состояние
(_State), а _resolve превращает его в datetime по правилам по умолчанию (см. там).
Польские диакритики и «ё» сворачиваются (_fold), поэтому «środa» == «sroda».

Также здесь: split_repeat (повтор из текста — «codziennie», «каждый понедельник»)
и чистые функции «тихого часа» (parse_quiet_hours, is_quiet_time, next_quiet_end)."""

import calendar
import re
from datetime import date, datetime, time, timedelta

_FOLD = str.maketrans("ąćęłńóśźżё", "acelnoszze")


def _fold(text: str) -> str:
    return text.lower().translate(_FOLD)


def _forms(*groups: tuple[str, int]) -> dict[str, int]:
    """('a b c', 1), ... -> {'a': 1, 'b': 1, 'c': 1} — словарь «словоформа -> значение»."""
    return {w: value for words, value in groups for w in words.split()}


# ---------- словари (все слова — уже в _fold-виде) ----------

_DAY_OFFSETS = _forms(
    ("сегодня today dzisiaj dzis", 0),
    ("завтра tomorrow jutro jutra", 1),
    ("послезавтра pojutrze", 2),
)

# sat/sun/wed и польское «sie» (= się) нарочно нет: это обычные слова, в заголовке дали бы ложную дату
# Вместе с множественным числом («по средам», «w poniedziałki», «mondays») — его оставляет split_repeat
_WEEKDAYS = _forms(
    ("понедельник понедельника понедельникам пн monday mondays mon poniedzialek poniedzialku poniedzialki pon", 0),
    ("вторник вторника вторникам вт tuesday tuesdays tue tues wtorek wtorku wtorki wt", 1),
    ("среда среду среды средам ср wednesday wednesdays sroda srode srody sr", 2),
    ("четверг четверга четвергам чт thursday thursdays thu thur thurs czwartek czwartku czwartki czw", 3),
    ("пятница пятницу пятницы пятницам пт friday fridays fri piatek piatku piatki pt", 4),
    ("суббота субботу субботы субботам сб saturday saturdays sobota sobote soboty sob", 5),
    ("воскресенье воскресенья воскресеньям вс sunday sundays niedziela niedziele niedzieli ndz nd", 6),
)
_NEXT_WORDS = {"следующий", "следующую", "следующее", "следующая", "next",
               "nastepny", "nastepna", "nastepnym", "przyszly", "przyszla", "przyszlym"}

_MONTHS = _forms(
    ("январь января янв january jan styczen stycznia sty", 1),
    ("февраль февраля фев february feb luty lutego lut", 2),
    ("март марта мар march mar marzec marca", 3),
    ("апрель апреля апр april apr kwiecien kwietnia kwi", 4),
    ("май мая may maj maja", 5),
    ("июнь июня июн june jun czerwiec czerwca", 6),
    ("июль июля июл july jul lipiec lipca lip", 7),
    ("август августа авг august aug sierpien sierpnia", 8),
    ("сентябрь сентября сен сент september sep sept wrzesien wrzesnia wrz", 9),
    ("октябрь октября окт october oct pazdziernik pazdziernika paz", 10),
    ("ноябрь ноября ноя нояб november nov listopad listopada lis", 11),
    ("декабрь декабря дек december dec grudzien grudnia gru", 12),
)

_NUMBERS = _forms(
    ("один одна одну одно a an one jeden jedna jedno", 1), ("два две пару пара two dwa dwie", 2),
    ("три three trzy", 3), ("четыре four cztery", 4), ("пять five piec", 5), ("шесть six szesc", 6),
    ("семь seven siedem", 7), ("восемь eight osiem", 8), ("девять nine dziewiec", 9),
    ("десять ten dziesiec", 10), ("пятнадцать fifteen pietnascie", 15),
    ("двадцать twenty dwadziescia", 20), ("тридцать thirty trzydziesci", 30),
    ("сорок forty czterdziesci", 40),
)
_HALF = {"пол", "half", "pol"}
_ONE_AND_HALF = {"полтора", "полторы", "poltorej"}

_UNITS = {
    **dict.fromkeys("минута минуту минуты минут мин м minute minutes min mins m minuta minuty minut".split(), "minutes"),
    **dict.fromkeys("час часа часов ч hour hours h hr hrs godzina godzine godziny godzin godz g".split(), "hours"),
    **dict.fromkeys("день дня дней д сутки суток day days d dzien dni dnia".split(), "days"),
    **dict.fromkeys("неделя неделю недели недель нед week weeks wk tydzien tygodnie tygodni tygodnia tyg".split(), "weeks"),
    **dict.fromkeys("месяц месяца месяцев мес month months miesiac miesiace miesiecy mies".split(), "months"),
    **dict.fromkeys("год года лет year years rok lata lat".split(), "years"),
}
_RELATIVE_START = {"через", "in", "za"}
_AND = {"и", "and", "i"}

# Пора дня без числа: «утром», «wieczorem», «in the evening» -> час по умолчанию
_DAYPARTS = {
    ("утром",): 9, ("morning",): 9, ("in", "the", "morning"): 9, ("rano",): 9, ("z", "rana"): 9,
    ("днем",): 14, ("afternoon",): 15, ("in", "the", "afternoon"): 15, ("po", "poludniu"): 15,
    ("в", "обед"): 13, ("at", "lunch"): 13,
    ("вечером",): 19, ("evening",): 19, ("in", "the", "evening"): 19, ("wieczorem",): 19,
    ("ночью",): 22, ("at", "night"): 22, ("w", "nocy"): 22,
    ("полдень",): 12, ("в", "полдень"): 12, ("noon",): 12, ("at", "noon"): 12,
    ("poludnie",): 12, ("w", "poludnie"): 12,
    ("полночь",): 0, ("в", "полночь"): 0, ("midnight",): 0, ("at", "midnight"): 0,
    ("polnoc",): 0, ("o", "polnocy"): 0, ("polnocy",): 0,
}
# Пометка после часа: «в 6 вечера», «o 7 rano», «6 pm»: am / pm / night (3 ночи = 3, 11 ночи = 23)
_HOUR_MARKS = {
    ("утра",): "am", ("am",): "am", ("rano",): "am",
    ("дня",): "pm", ("вечера",): "pm", ("pm",): "pm", ("wieczorem",): "pm", ("po", "poludniu"): "pm",
    ("ночи",): "night", ("w", "nocy"): "night",
}
_HOUR_WORDS = {"час", "часа", "часов", "godz", "godzina", "godzinie", "godziny", "o'clock", "h"}
_TIME_PREPS = {"в", "во", "at", "o", "к", "около", "okolo", "around"}

# Фразы-периоды: «на выходных», «w przyszłym tygodniu», «next month»
_PERIODS = {
    ("weekend",): "weekend", ("this", "weekend"): "weekend", ("на", "выходных"): "weekend",
    ("в", "выходные"): "weekend", ("на", "этих", "выходных"): "weekend", ("w", "weekend"): "weekend",
    ("next", "week"): "next_week", ("на", "следующей", "неделе"): "next_week",
    ("w", "przyszlym", "tygodniu"): "next_week", ("w", "nastepnym", "tygodniu"): "next_week",
    ("next", "month"): "next_month", ("в", "следующем", "месяце"): "next_month",
    ("w", "przyszlym", "miesiacu"): "next_month", ("w", "nastepnym", "miesiacu"): "next_month",
}
_MULTI_DAYS = {("day", "after", "tomorrow"): 2}

# Служебные слова, которые ничего не значат сами по себе
_FILLERS = {"в", "во", "на", "к", "до", "at", "on", "by", "until", "till", "the", "of", "o", "w", "we", "na", "do",
            "this", "этот", "эту", "это", "ten", "te", "ta", "г", "года", "roku", "r", "около", "okolo", "around"}

_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})(am|pm)?$")
_HOUR_SUFFIX_RE = re.compile(r"^(\d{1,2})(am|pm|h|ч)$")
_DM_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ORDINAL_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th|-го|-ого|-go|-ego)?$")
_COMPACT_RE = re.compile(r"^(\d+)(ч|h|g|godz|м|m|min|д|d)(?:(\d+)(м|m|min)?)?$")


class _State:
    def __init__(self):
        self.offset = None      # дни от сегодня (сегодня/завтра/…)
        self.date = None        # (день, месяц, год|None)
        self.weekday = None
        self.next = False       # «следующий понедельник»
        self.delta = None       # через …: timedelta
        self.months = 0         # через … месяцев/лет — календарно
        self.period = None      # weekend / next_week / next_month
        self.time = None        # (час, минута)
        self.daypart = None     # час по умолчанию от «утром/вечером»
        self.time_after_date = False  # для «12.10»: после даты это время
        self.after_prep = False       # предыдущее слово — «в/at/o»: «o 12.10» — время

    def date_sources(self) -> int:
        return sum(x is not None for x in (self.offset, self.date, self.weekday, self.delta, self.period))

    def empty(self) -> bool:
        return self.date_sources() == 0 and self.time is None and self.daypart is None


def _tokens(text: str) -> list[str]:
    words = _fold(text).replace(",", " ").split()
    out = []
    for w in words:
        w = w.strip(";!?()\"'«»")
        if not re.search(r"\d", w):
            w = w.replace(".", "")          # «p.m.» -> «pm», «r.» -> «r»
        else:
            w = w.rstrip(".")               # «20.10.» -> «20.10»
        if w:
            out.append(w)
    return out


def _phrase(tokens: list[str], i: int, table: dict) -> tuple[int, object]:
    """Самая длинная фраза из table, начинающаяся с tokens[i]: (длина, значение) или (0, None)."""
    best = (0, None)
    for key, value in table.items():
        n = len(key)
        if n > best[0] and tuple(tokens[i:i + n]) == key:
            best = (n, value)
    return best


def _set_time(st: _State, hour: int, minute: int = 0, mark: str | None = None):
    if st.time is not None:
        raise ValueError("два времени")
    if mark == "pm" and hour < 12:
        hour += 12
    elif mark == "am" and hour == 12:
        hour = 0
    elif mark == "night" and 6 <= hour < 12:
        hour += 12
    time(hour, minute)                      # ValueError на 25:00 / 10:61
    st.time = (hour, minute)


def _hour_tail(tokens: list[str], i: int) -> tuple[int, str | None]:
    """После числа-часа: необязательное «часов/godz» и пометка «вечера/pm». -> (сколько съели, пометка)."""
    used = 0
    if i < len(tokens) and tokens[i] in _HOUR_WORDS:
        used += 1
    n, mark = _phrase(tokens, i + used, _HOUR_MARKS)
    return used + n, mark


def _amount(tokens: list[str], i: int) -> tuple[int, float | None]:
    if i >= len(tokens):
        return 0, None
    t = tokens[i]
    if t.isdigit():
        return 1, int(t)
    if t in _NUMBERS:
        return 1, _NUMBERS[t]
    if t in _ONE_AND_HALF:
        return 1, 1.5
    if t in _HALF:                          # «half an hour», «пол часа», «pół godziny»
        skip = 2 if i + 1 < len(tokens) and tokens[i + 1] in ("a", "an") else 1
        return skip, 0.5
    return 0, None


def _add_duration(st: _State, amount: float, unit: str):
    st.delta = st.delta or timedelta()
    if unit == "months":
        st.months += int(amount)
    elif unit == "years":
        st.months += int(amount) * 12
    else:
        st.delta += timedelta(**{unit: amount})


def _relative(tokens: list[str], i: int, st: _State) -> int:
    """«через 1 час 30 минут», «in 2h30m», «za pół godziny», «через неделю», «полчаса»."""
    j = i + 1
    found = False
    while j < len(tokens):
        if found and tokens[j] in _AND:
            j += 1
            continue
        if tokens[j] == "полчаса":
            _add_duration(st, 30, "minutes")
            j, found = j + 1, True
            continue
        m = _COMPACT_RE.match(tokens[j])
        if m:
            _add_duration(st, int(m.group(1)), _UNITS[m.group(2)])
            if m.group(3):
                _add_duration(st, int(m.group(3)), "minutes")
            j, found = j + 1, True
            continue
        used, amount = _amount(tokens, j)
        k = j + used
        if k < len(tokens) and tokens[k] in _UNITS:
            _add_duration(st, 1 if amount is None else amount, _UNITS[tokens[k]])
            j, found = k + 1, True
            continue
        break
    if not found:
        return 0
    if st.date_sources() > 1:
        raise ValueError("две даты")
    return j - i


def _number_token(tokens: list[str], i: int, st: _State) -> int:
    t = tokens[i]
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None

    m = _TIME_RE.match(t)
    if m and (":" in t or m.group(3)):
        n, mark = _hour_tail(tokens, i + 1)
        _set_time(st, int(m.group(1)), int(m.group(2)), m.group(3) or mark)
        return 1 + n

    m = _HOUR_SUFFIX_RE.match(t)
    if m:
        n, mark = _phrase(tokens, i + 1, _HOUR_MARKS)
        suffix = m.group(2)
        _set_time(st, int(m.group(1)), 0, suffix if suffix in ("am", "pm") else mark)
        return 1 + n

    m = _DMY_RE.match(t) or _YMD_RE.match(t)
    if m:
        a, b, c = (int(x) for x in m.groups())
        day, month, year = (c, b, a) if _YMD_RE.match(t) else (a, b, c + 2000 if c < 100 else c)
        _set_date(st, day, month, year)
        return 1

    m = _DM_RE.match(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        as_time = "." in t and len(m.group(2)) == 2 and a <= 23 and b <= 59
        if as_time and (st.after_prep or st.time_after_date or not (1 <= b <= 12)):
            n, mark = _hour_tail(tokens, i + 1)
            _set_time(st, a, b, mark)
            return 1 + n
        _set_date(st, a, b, None)
        return 1

    if t.isdigit():
        # Голое число — час, только с пометкой: «6 pm», «7 rano», «5 o'clock». «10 часов»
        # без предлога — нет: «Работал 10 часов» не должно стать сроком 10:00.
        n, mark = _phrase(tokens, i + 1, _HOUR_MARKS)
        if n or nxt == "o'clock":
            _set_time(st, int(t), 0, mark)
            return 1 + (n or 1)

    m = _ORDINAL_RE.match(t)
    if m:
        k = i + 1 + (nxt == "of")
        if k < len(tokens) and tokens[k] in _MONTHS:      # «20 октября», «20th of oct 2026»
            month, k = _MONTHS[tokens[k]], k + 1
            year = None
            if k < len(tokens) and re.fullmatch(r"\d{4}", tokens[k]):
                year, k = int(tokens[k]), k + 1
            _set_date(st, int(m.group(1)), month, year)
            return k - i
    return 0


def _set_date(st: _State, day: int, month: int, year: int | None):
    date(year or 2000, month, day)          # ValueError на 31.02 (2000 — високосный: 29.02 пропускаем)
    st.date = (day, month, year)
    st.time_after_date = True
    if st.date_sources() > 1:
        raise ValueError("две даты")


def _parse_tokens(tokens: list[str], now: datetime) -> _State | None:
    st = _State()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        used = 0

        n, value = _phrase(tokens, i, _PERIODS)
        if n:
            st.period, used = value, n
        if not used:
            n, value = _phrase(tokens, i, _MULTI_DAYS)
            if n:
                st.offset, used = value, n
        if not used:
            n, value = _phrase(tokens, i, _DAYPARTS)
            if n:
                if st.daypart is not None:
                    return None
                st.daypart, used = value, n
        if not used and t == "tonight":
            st.offset, st.daypart, used = 0, 20, 1
        if not used and t in _DAY_OFFSETS:
            st.offset, used = _DAY_OFFSETS[t], 1
            st.time_after_date = True
        if not used and t in _NEXT_WORDS and i + 1 < len(tokens) and tokens[i + 1] in _WEEKDAYS:
            st.weekday, st.next, used = _WEEKDAYS[tokens[i + 1]], True, 2
            st.time_after_date = True
        if not used and t in _WEEKDAYS:
            st.weekday, used = _WEEKDAYS[t], 1
            st.time_after_date = True
        if not used and t in _MONTHS and i + 1 < len(tokens):   # «October 20th», «oct 20 2026»
            m = _ORDINAL_RE.match(tokens[i + 1])
            if m:
                used = 2
                year = None
                if i + 2 < len(tokens) and re.fullmatch(r"\d{4}", tokens[i + 2]):
                    year, used = int(tokens[i + 2]), 3
                _set_date(st, int(m.group(1)), _MONTHS[t], year)
        if not used and t in _RELATIVE_START:
            used = _relative(tokens, i, st)
        if not used and t == "полчаса":                         # «полчаса» без «через» — не дата
            return None
        if not used and t in _TIME_PREPS and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            n, mark = _hour_tail(tokens, i + 2)                  # «в 18», «o 7 rano», «at 6 pm»
            _set_time(st, int(tokens[i + 1]), 0, mark)
            used = 2 + n
        if not used and re.search(r"\d", t):
            used = _number_token(tokens, i, st)
        if not used and t in _FILLERS:
            used = 1
        if not used:
            return None
        if st.date_sources() > 1:
            return None
        st.after_prep = used == 1 and t in _TIME_PREPS
        i += used
    return None if st.empty() else st


def _add_months(d: datetime, months: int) -> datetime:
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def _resolve(st: _State, now: datetime) -> datetime:
    """Правила по умолчанию:
    - «через …» — от текущего момента; явное время заменяет часы/минуты;
    - дата без времени — 09:00 (пора дня — её час); «сегодня» без времени — сейчас;
    - только время — сегодня, а если уже прошло — завтра;
    - дата без года в прошлом — следующий год;
    - день недели — ближайший; сегодняшний — только если время ещё впереди."""
    if st.delta is not None:
        result = _add_months(now + st.delta, st.months)
        if st.time:
            result = result.replace(hour=st.time[0], minute=st.time[1], second=0, microsecond=0)
        return result

    hour_min = st.time or ((st.daypart, 0) if st.daypart is not None else None)
    today = now.date()
    day = None
    if st.offset is not None:
        if hour_min is None and st.offset == 0:
            return now
        day = today + timedelta(days=st.offset)
    elif st.date is not None:
        d, m, y = st.date
        day = date(y or today.year, m, d)
        if y is None and day < today:
            day = date(today.year + 1, m, d)
    elif st.weekday is not None:
        diff = (st.weekday - today.weekday()) % 7
        later_today = hour_min is not None and time(*hour_min) > now.time()
        if diff == 0 and (st.next or not later_today):
            diff = 7
        day = today + timedelta(days=diff)
    elif st.period == "weekend":
        day = today + timedelta(days=(5 - today.weekday()) % 7)
    elif st.period == "next_week":
        day = today + timedelta(days=7 - today.weekday())
    elif st.period == "next_month":
        day = _add_months(datetime.combine(today.replace(day=1), time()), 1).date()

    if day is None:                          # только время
        result = datetime.combine(today, time(*hour_min))
        return result if result > now else result + timedelta(days=1)
    return datetime.combine(day, time(*(hour_min or (9, 0))))


# ---------- публичные функции ----------

_TRAILING_PREPS = {"на", "в", "во", "к", "до", "at", "on", "by", "na", "do", "o", "w", "we", "the"}


def split_title_and_date(text: str, now: datetime) -> tuple[str, datetime | None]:
    """Для быстрого добавления одной строкой: 'Купить молоко завтра в 18:00' ->
    ('Купить молоко', <дата>). Ищет самый длинный непрерывный кусок слов, который
    parse_human_date понимает как дату — в конце, в начале или в середине
    ('Spotkanie w piątek o 10 z Anią' -> 'Spotkanie z Anią'). При равной длине
    предпочитает конец, потом начало. Если заголовка не остаётся (вся строка —
    дата, например 'завтра') — возвращает (text, None), чтобы вызывающий код
    спросил дату отдельным шагом, а не создал задачу без названия."""
    words = text.strip().split()
    n = len(words)
    best = None                              # (длина, приоритет, i, j, дата)
    for i in range(n):
        for j in range(n, i, -1):
            if best and j - i < best[0]:
                break
            parsed = parse_human_date(" ".join(words[i:j]), now)
            if parsed is None:
                continue
            priority = 2 if j == n else (1 if i == 0 else 0)
            if not best or (j - i, priority) > best[:2]:
                best = (j - i, priority, i, j, parsed)
            break
    if best is None:
        return text.strip(), None
    _, _, i, j, parsed = best
    head, tail = words[:i], words[j:]
    # Срезаем предлог, повисший на стыке: 'Купить подарок НА' + 'завтра' -> 'Купить подарок'
    while head and _fold(head[-1]).strip(",.") in _TRAILING_PREPS:
        head = head[:-1]
    title = " ".join(head + tail).strip(" ,.-")
    if not title:
        return text.strip(), None
    return title, parsed


def parse_human_date(text: str, now: datetime) -> datetime | None:
    """now — текущее время В ЛОКАЛЬНОЙ зоне пользователя (naive), чтобы
    'завтра' и 'через час' считались правильно относительно его часового пояса.
    Некорректные значения (например 'завтра 25:00') дают None, а не исключение."""
    try:
        st = _parse_tokens(_tokens(text), now)
        return _resolve(st, now) if st else None
    except (ValueError, OverflowError):
        return None


# ---------- повтор из текста ----------

_WD_RECUR = {
    0: r"понедельник(?:ам)?|monday|mondays|poniedzia[lł]ek|poniedzia[lł]ki",
    1: r"вторник(?:ам)?|tuesday|tuesdays|wtorek|wtorki",
    2: r"сред[уа]|средам|wednesday|wednesdays|[sś]rod[ęey]",
    3: r"четверг(?:ам)?|thursday|thursdays|czwartek|czwartki",
    4: r"пятниц[уа]|пятницам|friday|fridays|pi[aą]tek|pi[aą]tki",
    5: r"суббот[уа]|субботам|saturday|saturdays|sobot[ęey]",
    6: r"воскресень[ея]|воскресеньям|sunday|sundays|niedziel[ęe]|niedziele",
}
_WD_PREFIX = r"(кажд(?:ый|ую|ое)|по|every|each|on|co|w\s+ka[zż]d[yąae]|ka[zż]d[yąae]|w)"
# После «w / on / по» — только множественное число: «w piątek», «on friday», «по пятницу»
# («с понедельника по пятницу») — это разовая дата, а не повтор
_WD_PLURAL = r"(?:poniedzia[lł]ki|wtorki|[sś]rody|czwartki|pi[aą]tki|soboty|niedziele|\w+days|\w+(?:ам|ям))"
_REPEAT_PATTERNS = [
    ("weekdays", r"по\s+будням|в\s+будни|every\s+weekday|on\s+weekdays|weekdays|w\s+dni\s+(?:robocze|powszednie)"),
    ("daily", r"каждый\s+день|ежедневно|every\s*day|daily|codziennie|co\s*dzie[nń]|ka[zż]dego\s+dnia"),
    ("weekly", r"каждую\s+неделю|еженедельно|every\s+week|weekly|co\s+tydzie[nń]|ka[zż]dego\s+tygodnia"),
    ("monthly", r"каждый\s+месяц|ежемесячно|every\s+month|monthly|co\s+miesi[aą]c|ka[zż]dego\s+miesi[aą]ca"),
    ("yearly", r"каждый\s+год|ежегодно|every\s+year|yearly|annually|co\s+rok|corocznie|ka[zż]dego\s+roku"),
]


def _cut(text: str, start: int, end: int, insert: str = "") -> str:
    out = f"{text[:start]} {insert} {text[end:]}" if insert else f"{text[:start]} {text[end:]}"
    return re.sub(r"\s+", " ", out).strip(" ,.-")


def split_repeat(text: str) -> tuple[str, str | None]:
    """Повтор из текста задачи: ('Planerka co poniedziałek o 10') ->
    ('Planerka poniedziałek o 10', 'weekly'). Слово дня недели остаётся в тексте
    (как написал человек), чтобы дальше split_title_and_date поставил первый срок
    на ближайший такой день. Без повтора — (text, None). Код — из REPEAT_OPTIONS."""
    for forms in _WD_RECUR.values():
        for m in re.finditer(rf"\b{_WD_PREFIX}\s+({forms})\b", text, re.IGNORECASE):
            if m.group(1).lower() not in ("w", "on", "по") or re.fullmatch(_WD_PLURAL, m.group(2), re.I):
                return _cut(text, m.start(), m.end(), m.group(2)), "weekly"
    for code, pattern in _REPEAT_PATTERNS:
        m = re.search(rf"\b(?:{pattern})\b", text, re.IGNORECASE)
        if m:
            rest = _cut(text, m.start(), m.end())
            return (rest, code) if rest else (text, None)
    return text, None


# ---------- тихий час ----------

def parse_quiet_hours(text: str) -> tuple[time, time] | None:
    """'23:00-08:00' (также '23.00 – 8:00') -> (time(23,0), time(8,0)).
    None, если формат неверный, время вне диапазона или начало == концу."""
    m = re.match(r"^\s*(\d{1,2})[:\.](\d{2})\s*[-–—]\s*(\d{1,2})[:\.](\d{2})\s*$", text)
    if not m:
        return None
    sh, sm, eh, em = (int(g) for g in m.groups())
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return None
    start, end = time(sh, sm), time(eh, em)
    if start == end:
        return None
    return start, end


def is_quiet_time(t: time, start: time, end: time) -> bool:
    """Попадает ли локальное время t в тихий час [start, end). Окно может
    переходить через полночь (23:00–08:00). Конец не включается — в момент
    окончания тихого часа уведомления уже можно слать."""
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def next_quiet_end(now: datetime, start: time, end: time) -> datetime:
    """Ближайший момент окончания тихого часа (naive, локальное время).
    Предполагается, что now уже внутри окна; start оставлен в сигнатуре для
    симметрии с is_quiet_time."""
    candidate = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
