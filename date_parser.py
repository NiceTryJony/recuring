"""Парсинг даты из текста: строгий формат ДД.ММ.ГГГГ ЧЧ:ММ плюс человеческие
выражения на русском, английском и польском ('завтра 18:00', 'через 3 дня',
'in 2 hours', 'jutro 18:00', 'za 3 dni'). Возвращает naive datetime (без tzinfo) —
вызывающий код сам прикручивает часовой пояс пользователя.

Также содержит чистые функции для «тихого часа» (parse_quiet_hours,
is_quiet_time, next_quiet_end) — они без побочных эффектов и легко тестируются."""

import re
from datetime import datetime, time, timedelta

_TODAY_WORDS = r"сегодня|today|dzisiaj|dziś|dzis"
_TOMORROW_WORDS = r"завтра|tomorrow|jutro"

_UNIT_RE = (
    r"(д(?:ень|ня|ней)?|days?|dni|dzie[ńn]"
    r"|ч(?:ас|аса|асов)?|hours?|godz(?:inę|ine|iny|in|ina)?"
    r"|мин(?:ута|уты|уту|ут)?|min(?:ute|uty|utę|uta|ut)?s?"
    r"|недел[юие]|недель|weeks?|tydzie[ńn]|tygod(?:nie|ni|nia))"
)


def parse_human_date(text: str, now: datetime) -> datetime | None:
    """now — текущее время В ЛОКАЛЬНОЙ зоне пользователя (naive), чтобы
    'завтра' и 'через час' считались правильно относительно его часового пояса.
    Некорректные значения (например 'завтра 25:00') дают None, а не исключение."""
    text = re.sub(r"\s+", " ", text.strip().lower())
    try:
        return _parse(text, now)
    except ValueError:
        return None


def _parse(text: str, now: datetime) -> datetime | None:
    # 1. Строгий формат ДД.ММ.ГГГГ ЧЧ:ММ
    try:
        return datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        pass

    # 2. Строгий формат без времени — ДД.ММ.ГГГГ (ставим 09:00 по умолчанию)
    try:
        d = datetime.strptime(text, "%d.%m.%Y")
        return d.replace(hour=9, minute=0)
    except ValueError:
        pass

    # 3. "завтра [ЧЧ:ММ]" / "tomorrow [HH:MM]" / "jutro [GG:MM]"
    m = re.match(rf"^(?:{_TOMORROW_WORDS})(?:\s+(\d{{1,2}})[:\.](\d{{2}}))?$", text)
    if m:
        base = now + timedelta(days=1)
        hour = int(m.group(1)) if m.group(1) else 9
        minute = int(m.group(2)) if m.group(2) else 0
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 4. "сегодня [ЧЧ:ММ]" / "today [HH:MM]" / "dzisiaj [GG:MM]"
    m = re.match(rf"^(?:{_TODAY_WORDS})(?:\s+(\d{{1,2}})[:\.](\d{{2}}))?$", text)
    if m:
        hour = int(m.group(1)) if m.group(1) else now.hour
        minute = int(m.group(2)) if m.group(2) else now.minute
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 5. "через N <единица> [ЧЧ:ММ]" / "in N ..." / "za N ..."
    m = re.match(
        rf"^(?:через|in|za)\s+(\d+)\s*{_UNIT_RE}(?:\s+(\d{{1,2}})[:\.](\d{{2}}))?$",
        text,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        hour_override = m.group(3)
        minute_override = m.group(4)

        if unit.startswith(("д", "day", "dni", "dzie")):
            delta = timedelta(days=n)
        elif unit.startswith(("ч", "hour", "godz")):
            delta = timedelta(hours=n)
        elif unit.startswith(("мин", "min")):
            delta = timedelta(minutes=n)
        elif unit.startswith(("недел", "week", "tydz", "tygod")):
            delta = timedelta(weeks=n)
        else:
            return None

        result = now + delta
        if hour_override is not None:
            result = result.replace(hour=int(hour_override), minute=int(minute_override), second=0, microsecond=0)
        return result

    # 6. Без явного числа: "через час" / "in an hour" / "za godzinę" и т.д.
    if re.match(r"^(?:через час|in an hour|za godzinę|za godzine)$", text):
        return now + timedelta(hours=1)
    if re.match(r"^(?:через минуту|in a minute|za minutę|za minute)$", text):
        return now + timedelta(minutes=1)
    if re.match(r"^(?:через неделю|in a week|za tydzień|za tydzien)$", text):
        return now + timedelta(weeks=1)
    if re.match(r"^(?:через день|in a day|za dzień|za dzien)$", text):
        return now + timedelta(days=1)

    return None


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
