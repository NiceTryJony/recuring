"""Константы бота."""

import os
from datetime import timedelta
from zoneinfo import ZoneInfo

API_TOKEN = os.environ["BOT_TOKEN"]

DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "")  # напр. https://task-reminder-bot-xxxx.onrender.com

# Чат (ваш user id), куда приходят алерты о сбоях; без переменной алертов нет.
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.environ.get("ADMIN_CHAT_ID", "").lstrip("-").isdigit() else None

DEFAULT_TZ = ZoneInfo("Europe/Warsaw")

PAGE_SIZE = 8  # до 10 — ограничено набором _NUMBER_EMOJI и шириной ряда кнопок

# «Повторять напоминание, пока не выполню»: дефолты, чтобы не спамить бесконечно.
NAG_INTERVAL_MINUTES = 30   # не чаще раза в 30 минут

NAG_MAX_HOURS = 24          # полностью останавливаемся через 24 ч после срока

# Пресеты тихого часа для кнопок /quiet (можно ввести и свой)
QUIET_PRESETS = ["22:00-07:00", "23:00-08:00", "00:00-09:00"]

# Короткий список популярных поясов для кнопок; пользователь может ввести свой вручную
TZ_CHOICES = ["Europe/Warsaw", "Europe/Moscow", "Europe/Kyiv", "Asia/Almaty", "UTC"]

GROUP_CHAT_TYPES = {"group", "supergroup"}

REPEAT_OPTIONS = {
    "none": {"ru": "Без повтора", "en": "No repeat", "pl": "Bez powtarzania"},
    "daily": {"ru": "Каждый день", "en": "Every day", "pl": "Codziennie"},
    "weekly": {"ru": "Каждую неделю", "en": "Every week", "pl": "Co tydzień"},
    "monthly": {"ru": "Каждый месяц", "en": "Every month", "pl": "Co miesiąc"},
    "yearly": {"ru": "Каждый год", "en": "Every year", "pl": "Co rok"},
    "weekdays": {"ru": "По будням", "en": "Weekdays", "pl": "W dni robocze"},
    "monthly_nth_weekday": {"ru": "N-й день недели месяца", "en": "Nth weekday of month", "pl": "N-ty dzień tygodnia miesiąca"},
}

REMIND_OPTIONS = {
    "0": {"ru": "Без напоминания", "en": "No reminder", "pl": "Bez przypomnienia"},
    "1h": {"ru": "За 1 час", "en": "1 hour before", "pl": "1 godzinę wcześniej"},
    "1d": {"ru": "За 1 день", "en": "1 day before", "pl": "1 dzień wcześniej"},
    "3d": {"ru": "За 3 дня", "en": "3 days before", "pl": "3 dni wcześniej"},
}

REMIND_DELTAS = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "3d": timedelta(days=3)}

ORDINAL_WORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}

WEEKDAY_NAMES_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# Клетки 1-9 рисуются как key-cap эмодзи ("1️⃣"), 10 — отдельным символом
# "🔟"; больше 10 на страницу не бывает (PAGE_SIZE <= 10 см. ниже), так что
# набора хватает с запасом.
_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# ---------- /history ----------
# История остаётся личной (кто что сделал), даже для общих задач чата — поэтому
# ключом остаётся message.from_user.id, а не owner_id. Три режима (inline-кнопки):
# последние события / статистика за 7 дней / статистика за 30 дней.

HISTORY_MODES = {"recent", "7", "30"}
