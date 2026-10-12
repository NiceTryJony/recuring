"""Встроенные шаблоны задач (данные)."""

from datetime import datetime
from datetime import time as dtime
from datetime import timedelta
from zoneinfo import ZoneInfo

# ---------- шаблоны задач ----------
# Встроенные шаблоны — hardcode с переводом по языку (НЕ хранятся в БД, в
# отличие от пользовательских — см. db.task_templates): один шаблон = одна
# задача (title + время дня + смещение в днях от момента применения +
# repeat/remind/tag). day_offset: 0 = сегодня, 1 = завтра. Если время дня уже
# прошло для day_offset=0, apply_template сам сдвигает на следующий день —
# так "Полить цветы 19:00" не создаёт задачу в прошлом, если применили в 20:00.
BUILTIN_TEMPLATES = [
    {
        "key": "wake_up",
        "title": {"ru": "Подъём", "en": "Wake up", "pl": "Pobudka"},
        "time_of_day": "07:00", "day_offset": 0, "repeat": "daily", "remind": "0",
        "tag": {"ru": "рутина", "en": "routine", "pl": "rutyna"},
    },
    {
        "key": "workout",
        "title": {"ru": "Тренировка", "en": "Workout", "pl": "Trening"},
        "time_of_day": "19:00", "day_offset": 0, "repeat": "weekdays", "remind": "1h",
        "tag": {"ru": "спорт", "en": "fitness", "pl": "sport"},
    },
    {
        "key": "study_session",
        "title": {"ru": "Сессия учёбы", "en": "Study session", "pl": "Sesja nauki"},
        "time_of_day": "10:00", "day_offset": 0, "repeat": "none", "remind": "1h",
        "tag": {"ru": "учёба", "en": "study", "pl": "nauka"},
    },
    {
        "key": "homework_deadline",
        "title": {"ru": "Сдать домашнее задание", "en": "Submit homework", "pl": "Oddać pracę domową"},
        "time_of_day": "23:00", "day_offset": 1, "repeat": "none", "remind": "1d",
        "tag": {"ru": "учёба", "en": "study", "pl": "nauka"},
    },
    {
        "key": "exam_prep",
        "title": {"ru": "Начать подготовку к экзамену", "en": "Start exam prep", "pl": "Zacząć naukę do egzaminu"},
        "time_of_day": "09:00", "day_offset": 0, "repeat": "none", "remind": "3d",
        "tag": {"ru": "учёба", "en": "study", "pl": "nauka"},
    },
    {
        "key": "weekly_cleaning",
        "title": {"ru": "Уборка по дому", "en": "House cleaning", "pl": "Sprzątanie domu"},
        "time_of_day": "11:00", "day_offset": 0, "repeat": "weekly", "remind": "0",
        "tag": {"ru": "дом", "en": "home", "pl": "dom"},
    },
    {
        "key": "groceries",
        "title": {"ru": "Купить продукты", "en": "Buy groceries", "pl": "Zrobić zakupy spożywcze"},
        "time_of_day": "18:00", "day_offset": 0, "repeat": "weekly", "remind": "0",
        "tag": {"ru": "дом", "en": "home", "pl": "dom"},
    },
    {
        "key": "pay_bills",
        "title": {"ru": "Оплатить счета", "en": "Pay bills", "pl": "Opłacić rachunki"},
        "time_of_day": "10:00", "day_offset": 0, "repeat": "monthly", "remind": "1d",
        "tag": {"ru": "финансы", "en": "finance", "pl": "finanse"},
    },
    {
        "key": "water_plants",
        "title": {"ru": "Полить цветы", "en": "Water the plants", "pl": "Podlać kwiaty"},
        "time_of_day": "19:00", "day_offset": 0, "repeat": "weekly", "remind": "0",
        "tag": {"ru": "дом", "en": "home", "pl": "dom"},
    },
    {
        "key": "family_call",
        "title": {"ru": "Позвонить семье", "en": "Call family", "pl": "Zadzwonić do rodziny"},
        "time_of_day": "20:00", "day_offset": 0, "repeat": "weekly", "remind": "0",
        "tag": {"ru": "семья", "en": "family", "pl": "rodzina"},
    },
]

def builtin_templates_for_lang(lang: str) -> list[dict]:
    """Разворачивает BUILTIN_TEMPLATES под конкретный язык — title/tag
    подставляются по lang (с фолбэком на ru, если вдруг для языка не
    заведён перевод конкретного шаблона), остальные поля общие."""
    out = []
    for tpl in BUILTIN_TEMPLATES:
        out.append({
            "key": tpl["key"],
            "title": tpl["title"].get(lang, tpl["title"]["ru"]),
            "time_of_day": tpl["time_of_day"],
            "day_offset": tpl["day_offset"],
            "repeat": tpl["repeat"],
            "remind": tpl["remind"],
            "tag": tpl["tag"].get(lang, tpl["tag"]["ru"]) if tpl.get("tag") else None,
        })
    return out

def _resolve_template_due_at(time_of_day: str, day_offset: int, tz: ZoneInfo) -> datetime:
    """Превращает 'HH:MM' + смещение в днях в конкретный due_at (UTC-aware
    datetime), относительно МОМЕНТА ПРИМЕНЕНИЯ шаблона, не момента его
    создания — тот же шаблон, применённый сегодня и через неделю, должен
    каждый раз дать актуальную дату. Если day_offset=0 и время уже прошло
    сегодня, сдвигаем на завтра — иначе кнопка "применить" создавала бы
    задачу в прошлом, которая либо тут же считалась просроченной, либо (для
    repeat='none') вообще не имела смысла."""
    hour, minute = (int(p) for p in time_of_day.split(":"))
    now_local = datetime.now(tz)
    candidate_date = now_local.date() + timedelta(days=day_offset)
    candidate = datetime.combine(candidate_date, dtime(hour, minute), tzinfo=tz)
    if day_offset == 0 and candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(ZoneInfo("UTC"))
