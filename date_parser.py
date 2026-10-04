"""Парсинг даты из текста: строгий формат ДД.ММ.ГГГГ ЧЧ:ММ плюс несколько
человеческих выражений на русском и английском ('завтра 18:00', 'через 3 дня',
'in 2 hours'). Возвращает naive datetime (без tzinfo) — вызывающий код сам
прикручивает часовой пояс пользователя."""

import re
from datetime import datetime, timedelta


def parse_human_date(text: str, now: datetime) -> datetime | None:
    """now — текущее время В ЛОКАЛЬНОЙ зоне пользователя (naive), чтобы
    'завтра' и 'через час' считались правильно относительно его часового пояса."""
    text = text.strip().lower()

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

    # 3. "завтра [ЧЧ:ММ]" / "tomorrow [HH:MM]"
    m = re.match(r"^(завтра|tomorrow)(?:\s+(\d{1,2})[:\.](\d{2}))?$", text)
    if m:
        base = now + timedelta(days=1)
        hour = int(m.group(2)) if m.group(2) else 9
        minute = int(m.group(3)) if m.group(3) else 0
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 4. "сегодня [ЧЧ:ММ]" / "today [HH:MM]"
    m = re.match(r"^(сегодня|today)(?:\s+(\d{1,2})[:\.](\d{2}))?$", text)
    if m:
        hour = int(m.group(2)) if m.group(2) else now.hour
        minute = int(m.group(3)) if m.group(3) else now.minute
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 5. "через N дней/часов/минут/недель [ЧЧ:ММ]" / "in N days/hours/..."
    m = re.match(
        r"^(?:через|in)\s+(\d+)\s*"
        r"(д(?:ень|ня|ней)?|days?|ч(?:ас|аса|асов)?|hours?|мин(?:ута|уты|ут)?|min(?:ute)?s?|недел[юие]|weeks?)"
        r"(?:\s+(\d{1,2})[:\.](\d{2}))?$",
        text
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        hour_override = m.group(3)
        minute_override = m.group(4)

        if unit.startswith(("д", "day")):
            delta = timedelta(days=n)
        elif unit.startswith(("ч", "hour")):
            delta = timedelta(hours=n)
        elif unit.startswith(("мин", "min")):
            delta = timedelta(minutes=n)
        elif unit.startswith(("недел", "week")):
            delta = timedelta(weeks=n)
        else:
            return None

        result = now + delta
        if hour_override is not None:
            result = result.replace(hour=int(hour_override), minute=int(minute_override), second=0, microsecond=0)
        return result

    # 6. "через час" / "in an hour" / "через минуту" / "через неделю" — без явного числа
    m = re.match(r"^(?:через час|in an hour)$", text)
    if m:
        return now + timedelta(hours=1)
    m = re.match(r"^(?:через минуту|in a minute)$", text)
    if m:
        return now + timedelta(minutes=1)
    m = re.match(r"^(?:через неделю|in a week)$", text)
    if m:
        return now + timedelta(weeks=1)
    m = re.match(r"^(?:через день|in a day)$", text)
    if m:
        return now + timedelta(days=1)

    return None
