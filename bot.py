import asyncio
import html
import logging
import os
import signal
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from dotenv import load_dotenv
load_dotenv()

from aiohttp import web as aioweb
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Chat,
    BotCommand, BotCommandScopeDefault, BufferedInputFile,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError

import csv
import io

import dashboard
import db
from date_parser import parse_human_date, parse_quiet_hours, is_quiet_time, next_quiet_end, split_title_and_date
from i18n import t
from stats import compute_streak, daily_series, totals

API_TOKEN = os.environ["BOT_TOKEN"]
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "")  # напр. https://task-reminder-bot-xxxx.onrender.com
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------- throttling: защита от спама командами ----------
# Простой rate-limit по (user_id, chat_id): не чаще 1 сообщения в
# THROTTLE_RATE_SECONDS. При превышении — молча дропаем апдейт (без ответа,
# чтобы не плодить ещё больше трафика в спам-цикле) и один раз предупреждаем,
# пока троттлинг активен, чтобы не быть totally silent для обычного юзера,
# просто торопыги.
THROTTLE_RATE_SECONDS = 0.7


class ThrottlingMiddleware:
    def __init__(self, rate: float = THROTTLE_RATE_SECONDS):
        self.rate = rate
        self._last_seen: dict[tuple[int, int], float] = {}
        self._warned: set[tuple[int, int]] = set()

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None:
            return await handler(event, data)

        key = (chat.id, user.id)
        now = asyncio.get_event_loop().time()
        last = self._last_seen.get(key, 0.0)
        self._last_seen[key] = now

        if now - last < self.rate:
            if key not in self._warned:
                self._warned.add(key)
                try:
                    lang = await owner_lang(*(await resolve_owner(chat, user.id)))
                    await bot.send_message(chat.id, t(lang, "throttled"))
                except Exception:
                    pass
            return  # дропаем апдейт, хендлер не вызываем
        self._warned.discard(key)
        return await handler(event, data)


dp.message.middleware(ThrottlingMiddleware())
dp.callback_query.middleware(ThrottlingMiddleware())
# misfire_grace_time по умолчанию = 1 сек: джоба, опоздавшая на секунду (например, после
# "засыпания" free-инстанса), молча пропускается. Даём 5 минут запаса.
scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ, job_defaults={"coalesce": True, "misfire_grace_time": 300})

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


class AddTask(StatesGroup):
    title = State()
    date = State()
    repeat = State()
    remind = State()
    nag = State()
    tag = State()


class EditTask(StatesGroup):
    value = State()


class AddSubtasks(StatesGroup):
    collecting = State()


class AddTaskPhoto(StatesGroup):
    collecting = State()


class SetTimezone(StatesGroup):
    value = State()


class SetDashboardPassword(StatesGroup):
    value = State()


class FindTask(StatesGroup):
    query = State()


class SetQuiet(StatesGroup):
    value = State()


# ---------- владелец задач: личный пользователь или групповой чат ----------
#
# owner_type='user' (owner_id=user_id) для личных чатов, owner_type='chat'
# (owner_id=chat_id) для групп/супергрупп. Все настройки (часовой пояс, язык,
# дашборд) и задачи висят на владельце, а не всегда на конкретном user_id —
# это и есть групповой режим.

async def resolve_owner(chat: Chat, from_user_id: int) -> tuple[int, str]:
    if chat.type in GROUP_CHAT_TYPES:
        await db.add_chat_member(chat.id, from_user_id)
        return chat.id, "chat"
    return from_user_id, "user"


async def _can_access_task(task: dict, chat: Chat, from_user_id: int) -> bool:
    """Может ли этот пользователь управлять этой задачей (отмечать выполненной,
    удалять, редактировать и т.п.)?

    Два легитимных случая:
    1. Нажатие в том же чате, которому принадлежит задача (обычный случай
       для личных задач, и для групповых — кнопка под сообщением в самой группе).
    2. Нажатие в личке с ботом по задаче чата (owner_type='chat') — так
       приходят уведомления о групповых задачах (_recipients рассылает их
       личными сообщениями), поэтому chat.id нажавшего будет его личным ЛС,
       а не chat_id задачи. Легитимно, только если нажавший реально состоит
       в участниках этого чата (db.chat_members)."""
    owner_id, owner_type = task["owner_id"], task["owner_type"]

    same_owner = (owner_id, owner_type) == await resolve_owner(chat, from_user_id)
    if same_owner:
        return True

    if owner_type == "chat":
        return await db.is_chat_member(owner_id, from_user_id)

    return False


async def _authorize_task(call: CallbackQuery, task_id: int) -> dict | None:
    """Общая точка входа для всех действий с задачей по кнопке: загружает
    задачу и проверяет права. Возвращает задачу при успехе; при провале сама
    отвечает call.answer() с текстом ошибки (на известном нам языке, если task
    существует — иначе на дефолтном) и возвращает None. Вызывающий код просто
    делает `if task is None: return`."""
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return None
    if not await _can_access_task(task, call.message.chat, call.from_user.id):
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        await call.answer(t(lang, "not_authorized"), show_alert=True)
        return None
    return task


async def _authorize_subtask(call: CallbackQuery, subtask_id: int) -> tuple[dict, dict] | None:
    """То же самое для подзадач: сверяет права по родительской задаче.
    Возвращает (subtask, task) при успехе, иначе None (и сама отвечает call.answer())."""
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return None
    task = await db.get_task(sub["task_id"])
    if not task:
        await call.answer()
        return None
    if not await _can_access_task(task, call.message.chat, call.from_user.id):
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        await call.answer(t(lang, "not_authorized"), show_alert=True)
        return None
    return sub, task


# ---------- helpers: настройки владельца ----------

_settings_cache: dict[tuple[str, int], dict] = {}


async def get_settings(owner_id: int, owner_type: str) -> dict:
    key = (owner_type, owner_id)
    if key not in _settings_cache:
        _settings_cache[key] = await db.get_owner_settings(owner_id, owner_type)
    return _settings_cache[key]


def invalidate_settings(owner_id: int, owner_type: str):
    _settings_cache.pop((owner_type, owner_id), None)


async def owner_tz(owner_id: int, owner_type: str) -> ZoneInfo:
    s = await get_settings(owner_id, owner_type)
    try:
        return ZoneInfo(s["timezone"])
    except ZoneInfoNotFoundError:
        return DEFAULT_TZ


async def owner_lang(owner_id: int, owner_type: str) -> str:
    s = await get_settings(owner_id, owner_type)
    return s.get("language", "ru")


async def owner_quiet(owner_id: int, owner_type: str) -> tuple[dtime, dtime] | None:
    s = await get_settings(owner_id, owner_type)
    start, end = s.get("quiet_hours_start"), s.get("quiet_hours_end")
    if start is None or end is None or start == end:
        return None
    return start, end


def to_utc(local_dt: datetime, tz: ZoneInfo) -> datetime:
    return local_dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def to_local(utc_dt: datetime, tz: ZoneInfo) -> datetime:
    return utc_dt.astimezone(tz)


def normalize_tag(text: str) -> str | None:
    """Без этого «Дом», «дом » и «ДОМ» были бы тремя разными тегами в /tags и
    в фильтре задач (get_owner_tags делает DISTINCT по сырой строке) —
    приводим к единому виду до сохранения в БД. Пустая строка/"/skip" = None
    (без тега), не пустой тег нормализованный."""
    stripped = text.strip()
    if not stripped or stripped == "/skip":
        return None
    return stripped.lower()


# ---------- клавиатуры ----------

def repeat_kb(lang: str, prefix="rep"):
    kb = [[InlineKeyboardButton(text=v[lang], callback_data=f"{prefix}_{k}")] for k, v in REPEAT_OPTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def remind_kb(lang: str, prefix="rem", selected: set[str] | None = None):
    selected = selected or set()
    kb = []
    for k, v in REMIND_OPTIONS.items():
        label = v[lang]
        if k in selected:
            label = f"✅ {label}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}_{k}")])
    kb.append([InlineKeyboardButton(text="✅ " + t(lang, "btn_done"), callback_data=f"{prefix}_done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def nag_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_nag_yes"), callback_data="nag_1"),
        InlineKeyboardButton(text=t(lang, "btn_nag_no"), callback_data="nag_0"),
    ]])


def quiet_kb(lang: str):
    kb = [[InlineKeyboardButton(text=p.replace("-", "–"), callback_data=f"quiet_set_{p}")] for p in QUIET_PRESETS]
    kb.append([InlineKeyboardButton(text=t(lang, "btn_quiet_off"), callback_data="quiet_off")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def task_kb(task_id: int, lang: str, done: bool = False):
    if done:
        status_label = t(lang, "btn_task_undone")
        status_cb = f"undone_{task_id}"
    else:
        status_label = t(lang, "btn_task_done")
        status_cb = f"done_{task_id}"
    edit_label = t(lang, "btn_edit")
    del_label = t(lang, "btn_delete")
    snooze_label = t(lang, "btn_snooze")
    sub_label = t(lang, "btn_subtasks")
    photo_label = t(lang, "btn_photo")
    kb = [
        [InlineKeyboardButton(text=status_label, callback_data=status_cb)],
        [
            InlineKeyboardButton(text=edit_label, callback_data=f"edit_{task_id}"),
            InlineKeyboardButton(text=del_label, callback_data=f"del_{task_id}"),
        ],
        [
            InlineKeyboardButton(text=snooze_label, callback_data=f"snooze_{task_id}"),
            InlineKeyboardButton(text=sub_label, callback_data=f"subs_{task_id}"),
        ],
        [InlineKeyboardButton(text=photo_label, callback_data=f"photo_{task_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def confirm_delete_kb(task_id: int, lang: str):
    yes = t(lang, "btn_confirm_delete")
    no = t(lang, "btn_cancel")
    kb = [[
        InlineKeyboardButton(text=yes, callback_data=f"delok_{task_id}"),
        InlineKeyboardButton(text=no, callback_data=f"delno_{task_id}"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def edit_field_kb(task_id: int, lang: str):
    labels = {
        "title": t(lang, "field_title"),
        "date": t(lang, "field_date"),
        "repeat": t(lang, "field_repeat"),
        "tag": t(lang, "field_tag"),
    }
    kb = [[InlineKeyboardButton(text=v, callback_data=f"ef_{k}_{task_id}")] for k, v in labels.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# Клетки 1-9 рисуются как key-cap эмодзи ("1️⃣"), 10 — отдельным символом
# "🔟"; больше 10 на страницу не бывает (PAGE_SIZE <= 10 см. ниже), так что
# набора хватает с запасом.
_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def list_open_kb(task_ids: list[int], prefix: str = "listopen"):
    """Ряд кнопок-номеров под компактным списком — тап по номеру открывает
    полную карточку конкретной задачи отдельным сообщением. prefix различает
    источник ("listopen" -> list_task_open, "findopen" -> find_task_open),
    у каждого свой кэш открытых карточек (view["open_cards"])."""
    row = [
        InlineKeyboardButton(text=_NUMBER_EMOJI[i], callback_data=f"{prefix}_{task_id}")
        for i, task_id in enumerate(task_ids)
    ]
    return row


def list_nav_kb(lang: str, filter_mode: str, sort_by: str, page: int, total_pages: int, task_ids: list[int]):
    """Клавиатура под навигационным сообщением /list: строки фильтра/сортировки,
    ряд кнопок-номеров (открыть карточку конкретной задачи), и, если страниц
    больше одной, зацикленная пагинация — ⬅️ с последней страницы уводит на
    первую и наоборот, а не упирается в край."""
    kb = list_filter_kb(lang, filter_mode, sort_by)
    rows = list(kb.inline_keyboard)
    if task_ids:
        rows.append(list_open_kb(task_ids))
    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data=f"listpage_{prev_page}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"listpage_{next_page}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_filter_kb(lang: str, filter_mode: str, sort_by: str):
    def label(key: str, active: bool) -> str:
        text = t(lang, key)
        return f"✅ {text}" if active else text

    row_filter = [
        InlineKeyboardButton(text=label("btn_filter_all", filter_mode == "all"), callback_data="listf_all"),
        InlineKeyboardButton(text=label("btn_filter_overdue", filter_mode == "overdue"), callback_data="listf_overdue"),
        InlineKeyboardButton(text=label("btn_filter_week", filter_mode == "this_week"), callback_data="listf_this_week"),
    ]
    row_sort = [
        InlineKeyboardButton(text=label("btn_sort_date", sort_by == "date"), callback_data="lists_date"),
        InlineKeyboardButton(text=label("btn_sort_tag", sort_by == "tag"), callback_data="lists_tag"),
        InlineKeyboardButton(text=label("btn_sort_title", sort_by == "title"), callback_data="lists_title"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row_filter, row_sort])


def tags_kb(tags: list[str]):
    kb = [[InlineKeyboardButton(text=tag, callback_data=f"tagf_{tag}")] for tag in tags]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def timezone_kb():
    kb = [[InlineKeyboardButton(text=tz, callback_data=f"tz_{tz}")] for tz in TZ_CHOICES]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def lang_kb(owner_id: int, owner_type: str):
    # owner зашит прямо в callback_data — выбор языка не завязан на отдельное
    # FSM-состояние, а это самый простой способ протащить owner через колбэк.
    def cb(code: str) -> str:
        return f"lang_{owner_type}_{owner_id}_{code}"

    kb = [
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data=cb("ru"))],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data=cb("en"))],
        [InlineKeyboardButton(text="🇵🇱 Polski", callback_data=cb("pl"))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def subtasks_kb(task_id: int, subtasks: list[dict], lang: str, confirm_id: int | None = None):
    kb = []
    for st in subtasks:
        if st["id"] == confirm_id:
            yes = t(lang, "btn_confirm_delete")
            no = t(lang, "btn_cancel")
            kb.append([
                InlineKeyboardButton(text=yes, callback_data=f"subdelok_{st['id']}"),
                InlineKeyboardButton(text=no, callback_data=f"subdelno_{st['id']}"),
            ])
            continue
        mark = "☑️" if st["done"] else "⬜"
        kb.append([
            InlineKeyboardButton(text=f"{mark} {st['title']}", callback_data=f"subtoggle_{st['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"subdel_{st['id']}"),
        ])
    add_label = t(lang, "btn_add_subtask")
    back_label = t(lang, "btn_back")
    kb.append([InlineKeyboardButton(text=add_label, callback_data=f"subadd_{task_id}")])
    kb.append([InlineKeyboardButton(text=back_label, callback_data=f"subback_{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ---------- форматирование ----------

def fmt_repeat(repeat: str, lang: str) -> str:
    return REPEAT_OPTIONS.get(repeat, {}).get(lang, repeat)


def fmt_remind(remind: str, lang: str) -> str:
    """remind хранится как список кодов через запятую, например '1h,1d'."""
    if not remind or remind == "0":
        return REMIND_OPTIONS["0"][lang]
    parts = [REMIND_OPTIONS[c][lang] for c in remind.split(",") if c in REMIND_OPTIONS]
    return ", ".join(parts) if parts else REMIND_OPTIONS["0"][lang]


def _task_status_icon(t_row: dict) -> str:
    """✅ выполнена, ⚠️ просрочена (есть срок в прошлом и не выполнена), иначе ⏳."""
    if t_row["done"]:
        return "✅"
    due = t_row["due_at"]
    if due and due < datetime.now(ZoneInfo("UTC")):
        return "⚠️"
    return "⏳"


async def fmt_task(t_row: dict) -> str:
    """Язык и часовой пояс берутся из владельца самой задачи (t_row['owner_id']/
    ['owner_type']), а не из того, кто сейчас смотрит карточку — так в групповом
    чате все видят задачу в едином, настроенном для чата виде."""
    lang = await owner_lang(t_row["owner_id"], t_row["owner_type"])
    tz = await owner_tz(t_row["owner_id"], t_row["owner_type"])
    local_dt = to_local(t_row["due_at"], tz)
    status = _task_status_icon(t_row)
    # parse_mode=HTML: без экранирования заголовок вида "a < b" ломает отправку (TelegramBadRequest)
    text = f"{status} <b>{html.escape(t_row['title'])}</b>\n📅 {local_dt.strftime('%d.%m.%Y %H:%M')}"
    if t_row["repeat"] != "none":
        text += f"\n🔁 {fmt_repeat(t_row['repeat'], lang)}"
    if t_row["remind"] and t_row["remind"] != "0":
        text += f"\n🔔 {fmt_remind(t_row['remind'], lang)}"
    if t_row.get("remind_until_done"):
        text += f"\n{t(lang, 'nag_line')}"
    if t_row.get("tag"):
        text += f"\n🏷 {html.escape(t_row['tag'])}"
    text += f"\n<code>#{t_row['id']}</code>"
    return text


def fmt_task_line(t_row: dict, number: int, tz: ZoneInfo) -> str:
    """Одна строка компактного списка: номер (соответствует кнопке с тем же
    номером), статус, название (обрезано, чтобы длинные заголовки не ломали
    строку), дата. Без HTML-тегов — список шлётся как HTML, поэтому экранируем."""
    status = _task_status_icon(t_row)
    local_dt = to_local(t_row["due_at"], tz)
    title = t_row["title"]
    if len(title) > 40:
        title = title[:39] + "…"
    title = html.escape(title)
    date_str = local_dt.strftime("%d.%m %H:%M")
    tag_suffix = f" 🏷{html.escape(t_row['tag'])}" if t_row.get("tag") else ""
    return f"{_NUMBER_EMOJI[number - 1]} {status} {title} — {date_str}{tag_suffix}"


# ---------- базовые команды ----------

@dp.message(CommandStart())
async def start(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    # В группе сразу объясняем, что задачи общие — иначе непонятно, почему
    # задачу, которую добавил один, видят все. Короткий welcome (3 команды)
    # вместо полного списка — развёрнутая версия теперь в /help.
    key = "welcome_group" if owner_type == "chat" else "welcome"
    await message.answer(t(lang, key))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "help_full"))


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.clear()
    await message.answer(t(lang, "cancelled"))


# ---------- /timezone ----------

@dp.message(Command("timezone"))
async def timezone_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(SetTimezone.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "choose_timezone"), reply_markup=timezone_kb())


@dp.callback_query(F.data.startswith("tz_"))
async def timezone_pick(call: CallbackQuery, state: FSMContext):
    tz_name = call.data.split("_", 1)[1]
    data = await state.get_data()
    owner_id = data.get("owner_id", call.from_user.id)
    owner_type = data.get("owner_type", "user")
    await _apply_timezone(owner_id, owner_type, tz_name, call.message)
    await state.clear()
    await call.answer()


@dp.message(SetTimezone.value)
async def timezone_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id = data.get("owner_id", message.from_user.id)
    owner_type = data.get("owner_type", "user")
    text = (message.text or "").strip()
    if text.startswith("/"):
        # любая другая команда выводит из режима ввода (иначе "/cancel" считался бы кривым поясом)
        lang = await owner_lang(owner_id, owner_type)
        await state.clear()
        await message.answer(t(lang, "cancelled"))
        return
    await _apply_timezone(owner_id, owner_type, text, message)
    await state.clear()


async def _apply_timezone(owner_id: int, owner_type: str, tz_name: str, target_message: Message):
    lang = await owner_lang(owner_id, owner_type)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await target_message.answer(t(lang, "timezone_invalid"))
        return
    await db.set_owner_timezone(owner_id, owner_type, tz_name)
    invalidate_settings(owner_id, owner_type)
    new_tz = ZoneInfo(tz_name)
    schedule_daily_summary(owner_id, owner_type, new_tz)
    # cron-джобы повторяющихся задач привязаны к поясу на момент создания — без
    # перепланирования после /timezone они продолжали бы срабатывать по старому времени.
    for task in await db.get_tasks(owner_id, owner_type):
        schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], new_tz,
                      remind_until_done=bool(task.get("remind_until_done")))
    await target_message.answer(t(lang, "timezone_set", tz=tz_name))


# ---------- /lang ----------

@dp.message(Command("lang"))
async def lang_start(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "choose_lang"), reply_markup=lang_kb(owner_id, owner_type))


@dp.callback_query(F.data.startswith("lang_"))
async def lang_pick(call: CallbackQuery):
    _, owner_type, owner_id_str, new_lang = call.data.split("_")
    owner_id = int(owner_id_str)
    await db.set_owner_language(owner_id, owner_type, new_lang)
    invalidate_settings(owner_id, owner_type)
    await call.message.edit_text(t(new_lang, "lang_set"))
    await call.answer()


# ---------- /quiet — тихий час ----------

def _fmt_quiet(q: tuple[dtime, dtime]) -> tuple[str, str]:
    return q[0].strftime("%H:%M"), q[1].strftime("%H:%M")


async def _apply_quiet(owner_id: int, owner_type: str, quiet: tuple[dtime, dtime] | None, target_message: Message):
    lang = await owner_lang(owner_id, owner_type)
    if quiet is None:
        await db.set_owner_quiet_hours(owner_id, owner_type, None, None)
        invalidate_settings(owner_id, owner_type)
        await target_message.answer(t(lang, "quiet_off_done"))
        return
    await db.set_owner_quiet_hours(owner_id, owner_type, quiet[0], quiet[1])
    invalidate_settings(owner_id, owner_type)
    start, end = _fmt_quiet(quiet)
    await target_message.answer(t(lang, "quiet_set", start=start, end=end))


@dp.message(Command("quiet"))
async def quiet_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    q = await owner_quiet(owner_id, owner_type)
    if q:
        start, end = _fmt_quiet(q)
        head = t(lang, "quiet_current", start=start, end=end)
    else:
        head = t(lang, "quiet_none")
    await state.set_state(SetQuiet.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(f"{head}\n\n{t(lang, 'quiet_prompt')}", reply_markup=quiet_kb(lang))


@dp.callback_query(F.data.startswith("quiet_"))
async def quiet_pick(call: CallbackQuery, state: FSMContext):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    arg = call.data[len("quiet_"):]
    if arg == "off":
        await _apply_quiet(owner_id, owner_type, None, call.message)
    else:
        quiet = parse_quiet_hours(arg.removeprefix("set_"))
        if quiet is None:
            await call.answer()
            return
        await _apply_quiet(owner_id, owner_type, quiet, call.message)
    await state.clear()
    await call.answer()


@dp.message(SetQuiet.value)
async def quiet_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    text = (message.text or "").strip()
    if text.startswith("/"):
        # любая другая команда выводит из режима ввода (иначе "/add" считался бы кривым временем)
        lang = await owner_lang(owner_id, owner_type)
        await state.clear()
        await message.answer(t(lang, "cancelled"))
        return
    if text.lower() in {"off", "выкл", "выключить", "wyłącz", "wylacz"}:
        await _apply_quiet(owner_id, owner_type, None, message)
        await state.clear()
        return
    quiet = parse_quiet_hours(text)
    if quiet is None:
        lang = await owner_lang(owner_id, owner_type)
        await message.answer(t(lang, "quiet_invalid"))
        return  # остаёмся в состоянии — можно повторить ввод
    await _apply_quiet(owner_id, owner_type, quiet, message)
    await state.clear()


# ---------- добавление задачи ----------

@dp.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(AddTask.title)
    await state.update_data(owner_id=owner_id, owner_type=owner_type, actor_id=message.from_user.id)
    await message.answer(t(lang, "ask_title"))


async def _accept_date_and_ask_repeat(local_dt: datetime, state: FSMContext, tz: ZoneInfo, lang: str, target_message: Message):
    """Общий хвост для двух путей: обычного пошагового (после AddTask.date)
    и быстрого однострочного (когда дата уже найдена прямо в названии)."""
    await state.update_data(due_at=to_utc(local_dt, tz).isoformat())
    await state.set_state(AddTask.repeat)
    await target_message.answer(t(lang, "ask_repeat"), reply_markup=repeat_kb(lang))


@dp.message(AddTask.title)
async def add_title(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    tz = await owner_tz(data["owner_id"], data["owner_type"])
    now_local = datetime.now(tz).replace(tzinfo=None)

    raw = message.text.strip()
    # Быстрый путь: если в названии нашлась понятная дата ("Купить молоко
    # завтра в 18:00"), сразу принимаем её и пропускаем отдельный шаг
    # ask_date — для тех, кто не хочет проходить все шаги по отдельности.
    # Если дата не найдена — ведём себя как раньше: весь текст - заголовок,
    # дату спрашиваем следующим шагом.
    title, local_dt = split_title_and_date(raw, now_local)
    await state.update_data(title=title)

    if local_dt is not None:
        await _accept_date_and_ask_repeat(local_dt, state, tz, lang, message)
        return

    await state.set_state(AddTask.date)
    await message.answer(t(lang, "ask_date"))


@dp.message(AddTask.date)
async def add_date(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    tz = await owner_tz(data["owner_id"], data["owner_type"])
    now_local = datetime.now(tz).replace(tzinfo=None)

    local_dt = parse_human_date(message.text.strip(), now_local)
    if local_dt is None:
        await message.answer(t(lang, "bad_date"))
        return

    await _accept_date_and_ask_repeat(local_dt, state, tz, lang, message)


@dp.callback_query(AddTask.repeat, F.data.startswith("rep_"))
async def add_repeat(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    repeat = call.data.split("_", 1)[1]
    await state.update_data(repeat=repeat, remind_selected=[])
    await state.set_state(AddTask.remind)
    await call.message.edit_text(t(lang, "ask_remind"), reply_markup=remind_kb(lang))
    await call.answer()


@dp.callback_query(AddTask.remind, F.data.startswith("rem_"))
async def add_remind_toggle(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    code = call.data.split("_", 1)[1]

    if code == "done":
        selected = data.get("remind_selected", [])
        remind_value = ",".join(selected) if selected else "0"
        await state.update_data(remind=remind_value)
        # «Повторять, пока не выполню» — только для разовых задач: у повторяющихся
        # (daily/weekly/...) каждое срабатывание и так приходит по расписанию.
        if data.get("repeat") == "none":
            await state.set_state(AddTask.nag)
            await call.message.edit_text(
                t(lang, "ask_nag", interval=NAG_INTERVAL_MINUTES, hours=NAG_MAX_HOURS), reply_markup=nag_kb(lang)
            )
        else:
            await state.update_data(remind_until_done=False)
            await state.set_state(AddTask.tag)
            await call.message.edit_text(t(lang, "ask_tag"))
        await call.answer()
        return

    selected = set(data.get("remind_selected", []))
    if code == "0":
        selected = set()  # "без напоминания" сбрасывает остальной выбор
    else:
        selected.discard("0")
        if code in selected:
            selected.discard(code)
        else:
            selected.add(code)
    await state.update_data(remind_selected=list(selected))
    await call.message.edit_reply_markup(reply_markup=remind_kb(lang, selected=selected))
    await call.answer()


@dp.callback_query(AddTask.nag, F.data.startswith("nag_"))
async def add_nag_pick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    await state.update_data(remind_until_done=call.data == "nag_1")
    await state.set_state(AddTask.tag)
    await call.message.edit_text(t(lang, "ask_tag"))
    await call.answer()


@dp.message(AddTask.tag)
async def add_tag(message: Message, state: FSMContext):
    tag = normalize_tag(message.text)
    await _finalize_add_task(state, tag, message)


@dp.message(Command("skip"), AddTask.tag)
async def add_tag_skip(message: Message, state: FSMContext):
    await _finalize_add_task(state, None, message)


async def _finalize_add_task(state: FSMContext, tag: str | None, target_message: Message):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    actor_id = data.get("actor_id", target_message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    due_at = datetime.fromisoformat(data["due_at"])
    nag = bool(data.get("remind_until_done", False))

    task_id = await db.add_task(
        owner_id=owner_id,
        owner_type=owner_type,
        title=data["title"],
        due_at=due_at,
        repeat=data["repeat"],
        remind=data["remind"],
        tag=tag,
        remind_until_done=nag,
        created_by=actor_id,
    )
    await db.log_history(task_id, actor_id, data["title"], "created")
    schedule_task(task_id, owner_id, owner_type, due_at, data["repeat"], data["remind"], tz, remind_until_done=nag)
    if f"dailysummary_{owner_type}_{owner_id}" not in {j.id for j in scheduler.get_jobs()}:
        schedule_daily_summary(owner_id, owner_type, tz)

    local_str = to_local(due_at, tz).strftime("%d.%m.%Y %H:%M")
    await target_message.answer(t(lang, "task_added", title=data["title"], date=local_str))
    await state.clear()


# ---------- список задач ----------

def _get_list_view(owner_id: int, owner_type: str) -> dict:
    return _list_view_cache.setdefault(
        (owner_type, owner_id),
        # nav_msg_id — сообщение со списком+кнопками; open_cards — {task_id:
        # msg_id} карточек, открытых тапом по номеру (см. list_task_open).
        # Открытые карточки переживают листание страниц/смену фильтра (если
        # задача на них ещё видна), но закрываются, если задача пропала со
        # страницы — иначе кнопка "1" на новой странице молча правила бы
        # карточку другой, уже закрытой задачи с прошлой страницы.
        {"filter_mode": "all", "sort_by": "date", "page": 0, "nav_msg_id": None, "open_cards": {}},
    )


_list_view_cache: dict[tuple[str, int], dict] = {}
_search_cache: dict[tuple[str, int], dict] = {}

# Групповой чат: несколько участников могут одновременно жать разные кнопки
# /list (фильтр/сортировка/страница) по одному и тому же view-объекту — без
# сериализации гонка на чтении/правке nav_msg_id/open_cards могла расставить
# или стереть карточки не того пользователя. Лочим по (owner_type, owner_id),
# а не глобально — чаты/юзеры друг друга не блокируют.
_list_view_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _get_list_view_lock(owner_id: int, owner_type: str) -> asyncio.Lock:
    return _list_view_locks.setdefault((owner_type, owner_id), asyncio.Lock())


def _fmt_list_body(lang: str, page_items: list[dict], tz: ZoneInfo) -> str:
    header = t(lang, "list_controls")
    lines = [fmt_task_line(task, i + 1, tz) for i, task in enumerate(page_items)]
    return header + "\n\n" + "\n".join(lines)


async def _close_stale_open_cards(chat_id: int, view: dict, visible_task_ids: set[int]):
    """Карточки, открытые тапом по номеру, но чья задача больше не видна на
    текущей странице (сменили фильтр/страницу/задачу удалили) — закрываем,
    чтобы номер на новой странице не управлял чужой, невидимой карточкой."""
    stale = [tid for tid in view["open_cards"] if tid not in visible_task_ids]
    for tid in stale:
        mid = view["open_cards"].pop(tid)
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest:
            pass


async def _render_list_page(chat_id: int, owner_id: int, owner_type: str, lang: str, view: dict):
    """Единый механизм показа страницы /list: ОДНО навигационное сообщение —
    компактный нумерованный список + фильтр/сортировка/пагинация + ряд кнопок-
    номеров. Тап по номеру открывает полную карточку задачи отдельным
    сообщением (list_task_open) — так при 10+ задачах нет шторма сообщений, но
    полный контроль (✅/✏️/🗑/⏰) остаётся доступен по одному тапу."""
    tasks = await db.get_tasks(owner_id, owner_type, filter_mode=view["filter_mode"], sort_by=view["sort_by"])

    if not tasks:
        await _close_stale_open_cards(chat_id, view, visible_task_ids=set())
        view["page"] = 0
        text = t(lang, "list_controls") + "\n\n" + t(lang, "no_tasks")
        kb = list_filter_kb(lang, view["filter_mode"], view["sort_by"])
        if view["nav_msg_id"]:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=view["nav_msg_id"], reply_markup=kb)
                return
            except TelegramBadRequest:
                pass
        msg = await bot.send_message(chat_id, text, reply_markup=kb)
        view["nav_msg_id"] = msg.message_id
        return

    total_pages = (len(tasks) + PAGE_SIZE - 1) // PAGE_SIZE
    page = view["page"] % total_pages
    view["page"] = page
    start = page * PAGE_SIZE
    page_items = tasks[start:start + PAGE_SIZE]
    task_ids = [task["id"] for task in page_items]

    await _close_stale_open_cards(chat_id, view, visible_task_ids=set(task_ids))

    tz = await owner_tz(owner_id, owner_type)
    nav_text = _fmt_list_body(lang, page_items, tz)
    nav_kb = list_nav_kb(lang, view["filter_mode"], view["sort_by"], page, total_pages, task_ids)
    if view["nav_msg_id"]:
        try:
            await bot.edit_message_text(nav_text, chat_id=chat_id, message_id=view["nav_msg_id"], reply_markup=nav_kb, parse_mode="HTML")
        except TelegramBadRequest:
            msg = await bot.send_message(chat_id, nav_text, reply_markup=nav_kb, parse_mode="HTML")
            view["nav_msg_id"] = msg.message_id
    else:
        msg = await bot.send_message(chat_id, nav_text, reply_markup=nav_kb, parse_mode="HTML")
        view["nav_msg_id"] = msg.message_id

    # Открытые карточки задач, которые остались видимы на (возможно новой)
    # странице — обновляем их текст/клавиатуру на месте (например, после
    # смены сортировки статус задачи мог измениться с прошлого рендера).
    for task in page_items:
        mid = view["open_cards"].get(task["id"])
        if mid is None:
            continue
        text = await fmt_task(task)
        kb = task_kb(task["id"], lang, done=task["done"])
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest:
            pass  # "message is not modified" или сообщение удалили руками — не критично


async def _open_task_card(call: CallbackQuery, task: dict, view: dict):
    """Общая механика для listopen_/findopen_/tagfopen_: открывает (или, если
    уже открыта на этой view, обновляет на месте) полную карточку задачи
    отдельным сообщением. view — любой из *_view_cache объектов с ключом
    "open_cards" ({task_id: msg_id})."""
    task_id = task["id"]
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    kb = task_kb(task_id, lang, done=task["done"])
    chat_id = call.message.chat.id
    existing_mid = view["open_cards"].get(task_id)
    if existing_mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=existing_mid, reply_markup=kb, parse_mode="HTML")
            await call.answer()
            return
        except TelegramBadRequest:
            pass  # карточку удалили руками — пришлём новую ниже
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    view["open_cards"][task_id] = msg.message_id
    await call.answer()


@dp.callback_query(F.data.startswith("listopen_"))
async def list_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под компактным списком /list."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_list_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)


@dp.message(Command("list"))
async def list_tasks(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["page"] = 0  # свежий /list всегда показывает список с начала
        await _render_list_page(message.chat.id, owner_id, owner_type, lang, view)


@dp.callback_query(F.data.startswith("listf_"))
async def list_filter_pick(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    mode = call.data.split("_", 1)[1]
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["filter_mode"] = mode
        view["page"] = 0  # смена фильтра — список другой, со старой страницы смысла нет
        await _render_list_page(call.message.chat.id, owner_id, owner_type, lang, view)
    await call.answer()


@dp.callback_query(F.data.startswith("lists_"))
async def list_sort_pick(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    sort_by = call.data.split("_", 1)[1]
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["sort_by"] = sort_by
        view["page"] = 0
        await _render_list_page(call.message.chat.id, owner_id, owner_type, lang, view)
    await call.answer()


@dp.callback_query(F.data.startswith("listpage_"))
async def list_page_nav(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["page"] = int(call.data.split("_", 1)[1])
        await _render_list_page(call.message.chat.id, owner_id, owner_type, lang, view)
    await call.answer()


@dp.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    # Кнопка-индикатор "2/5" в пагинации — просто гасим "часики" на тапе.
    await call.answer()


# ---------- /find — текстовый поиск ----------

@dp.message(Command("find"))
async def find_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(FindTask.query)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "ask_find_query"))


async def _render_find_page(owner_id: int, owner_type: str, lang: str, page: int, tz: ZoneInfo):
    """Возвращает (текст, клавиатура, задачи_на_странице) — компактный
    нумерованный список как в /list (см. _render_list_page), с кнопками-
    номерами для открытия полной карточки по тапу вместо шторма сообщений."""
    cache = _search_cache.get((owner_type, owner_id))
    results = cache["results"] if cache else []
    if not results:
        return t(lang, "no_results"), None, []
    start = page * PAGE_SIZE
    page_items = results[start:start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < len(results)
    header = t(lang, "find_results", total=len(results))
    lines = [fmt_task_line(task, i + 1, tz) for i, task in enumerate(page_items)]
    text = header + "\n\n" + "\n".join(lines)
    rows = []
    if page_items:
        rows.append(list_open_kb([task["id"] for task in page_items], prefix="findopen"))
    if page > 0 or has_next:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"findpage_{page-1}"))
        if has_next:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"findpage_{page+1}"))
        rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return text, kb, page_items


def _get_find_view(owner_id: int, owner_type: str) -> dict:
    # open_cards: {task_id: msg_id} карточек, открытых тапом по номеру —
    # тот же паттерн, что и _get_list_view (см. list_task_open).
    return _find_view_cache.setdefault((owner_type, owner_id), {"nav_msg_id": None, "open_cards": {}})


_find_view_cache: dict[tuple[str, int], dict] = {}


@dp.message(FindTask.query)
async def find_query(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    await state.clear()
    query_text = message.text.strip()
    results = await db.search_tasks(owner_id, owner_type, query_text)
    _search_cache[(owner_type, owner_id)] = {"query": query_text, "results": results}
    # Новый поиск — прошлые открытые карточки относятся к прошлой выдаче, забываем о них
    _find_view_cache.pop((owner_type, owner_id), None)
    view = _get_find_view(owner_id, owner_type)

    text, kb, _ = await _render_find_page(owner_id, owner_type, lang, 0, tz)
    msg = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    view["nav_msg_id"] = msg.message_id


@dp.callback_query(F.data.startswith("findpage_"))
async def find_page_nav(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    page = int(call.data.split("_", 1)[1])

    text, kb, page_items = await _render_find_page(owner_id, owner_type, lang, page, tz)
    view = _get_find_view(owner_id, owner_type)
    await _close_stale_open_cards(call.message.chat.id, view, visible_task_ids={t["id"] for t in page_items})
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("findopen_"))
async def find_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под результатами /find. Свой кэш (_find_view_cache),
    отдельный от /list — см. _open_task_card."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_find_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)


# ---------- /tags ----------

@dp.message(Command("tags"))
async def tags_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tags = await db.get_owner_tags(owner_id, owner_type)
    if not tags:
        await message.answer(t(lang, "no_tags"))
        return
    await message.answer(t(lang, "choose_tag"), reply_markup=tags_kb(tags))


def _get_tagf_view(owner_id: int, owner_type: str) -> dict:
    # tag хранится в самом view, чтобы tagf_page_N знал, по какому тегу
    # листать — в отличие от /list тут нет отдельного состояния фильтра.
    return _tagf_view_cache.setdefault((owner_type, owner_id), {"tag": None, "nav_msg_id": None, "open_cards": {}})


_tagf_view_cache: dict[tuple[str, int], dict] = {}


async def _render_tagf_page(owner_id: int, owner_type: str, lang: str, tag: str, page: int, tz: ZoneInfo):
    tasks = await db.get_tasks(owner_id, owner_type, tag=tag)
    if not tasks:
        return t(lang, "no_tasks"), None, []
    start = page * PAGE_SIZE
    page_items = tasks[start:start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < len(tasks)
    header = t(lang, "find_results", total=len(tasks))  # "Найдено: N" подходит и для фильтра по тегу
    lines = [fmt_task_line(task, i + 1, tz) for i, task in enumerate(page_items)]
    text = f"🏷 {html.escape(tag)}\n{header}\n\n" + "\n".join(lines)
    rows = []
    if page_items:
        rows.append(list_open_kb([task["id"] for task in page_items], prefix="tagfopen"))
    if page > 0 or has_next:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"tagfpage_{page-1}"))
        if has_next:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"tagfpage_{page+1}"))
        rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return text, kb, page_items


@dp.callback_query(F.data.startswith("tagf_"))
async def tags_filter(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    tag = call.data.split("_", 1)[1]

    # Новый выбор тега — прошлые открытые карточки относились к прошлому тегу
    _tagf_view_cache.pop((owner_type, owner_id), None)
    view = _get_tagf_view(owner_id, owner_type)
    view["tag"] = tag

    text, kb, _ = await _render_tagf_page(owner_id, owner_type, lang, tag, 0, tz)
    msg = await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    view["nav_msg_id"] = msg.message_id
    await call.answer()


@dp.callback_query(F.data.startswith("tagfpage_"))
async def tags_filter_page(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    page = int(call.data.split("_", 1)[1])
    view = _get_tagf_view(owner_id, owner_type)
    tag = view["tag"]
    if tag is None:
        # Состояние потеряно (рестарт процесса) — просим начать заново через /tags
        await call.answer()
        return

    text, kb, page_items = await _render_tagf_page(owner_id, owner_type, lang, tag, page, tz)
    await _close_stale_open_cards(call.message.chat.id, view, visible_task_ids={t["id"] for t in page_items})
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("tagfopen_"))
async def tagf_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под результатами /tags. Свой кэш
    (_tagf_view_cache) — см. _open_task_card."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_tagf_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)


# ---------- /history ----------
# История остаётся личной (кто что сделал), даже для общих задач чата — поэтому
# ключом остаётся message.from_user.id, а не owner_id. Три режима (inline-кнопки):
# последние события / статистика за 7 дней / статистика за 30 дней.

HISTORY_MODES = {"recent", "7", "30"}


def history_kb(lang: str, active: str = "recent"):
    def label(key: str, mode: str) -> str:
        text = t(lang, key)
        return f"✅ {text}" if mode == active else text

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label("btn_hist_recent", "recent"), callback_data="hist_recent")],
        [InlineKeyboardButton(text=label("btn_hist_week", "7"), callback_data="hist_7")],
        [InlineKeyboardButton(text=label("btn_hist_month", "30"), callback_data="hist_30")],
    ])


async def _current_streak(user_id: int, tz: ZoneInfo) -> int:
    """Серия: сколько дней подряд была хотя бы одна выполненная задача (см. stats.compute_streak)."""
    days = await db.get_done_days(user_id, tz.key)
    return compute_streak(days, datetime.now(tz).date())


async def _render_history_text(user_id: int, owner_id: int, owner_type: str, lang: str, mode: str) -> str:
    tz = await owner_tz(owner_id, owner_type)
    if mode == "recent":
        events = await db.get_history(user_id)
        if not events:
            return t(lang, "no_history")
        event_labels = {
            "created": t(lang, "history_created"),
            "done": "✅",
            "undone": "↩️",
            "deleted": "🗑",
            "rescheduled": "📅",
        }
        lines = [t(lang, "history_title")]
        for e in events:
            local_dt = to_local(e["event_at"], tz)
            icon = event_labels.get(e["event"], "•")
            lines.append(f"{icon} {e['title']} — {local_dt.strftime('%d.%m %H:%M')}")
        return "\n".join(lines)

    days = int(mode)
    rows = await db.get_history_stats(user_id, days, tz.key)
    tot = totals(rows)
    lines = [t(lang, "stats_title", days=days), "", t(lang, "stats_totals", **tot)]
    if days <= 7:
        weekdays = t(lang, "weekdays_short").split(",")
        lines.append("")
        for d, cnt in daily_series(rows, "done", days, datetime.now(tz).date()):
            bar = "▇" * min(cnt, 10) if cnt else "·"
            lines.append(f"{weekdays[d.weekday()]} {d.strftime('%d.%m')}  {bar} {cnt}")
    streak = await _current_streak(user_id, tz)
    if streak > 0:
        lines += ["", t(lang, "streak_line", n=streak).strip()]
    return "\n".join(lines)


@dp.message(Command("history"))
async def history_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    text = await _render_history_text(message.from_user.id, owner_id, owner_type, lang, "recent")
    await message.answer(text, reply_markup=history_kb(lang, "recent"))


@dp.callback_query(F.data.startswith("hist_"))
async def history_mode_pick(call: CallbackQuery):
    mode = call.data.split("_", 1)[1]
    if mode not in HISTORY_MODES:
        owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
        lang = await owner_lang(owner_id, owner_type)
        await call.answer(t(lang, "history_mode_unknown"), show_alert=True)
        return
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    text = await _render_history_text(call.from_user.id, owner_id, owner_type, lang, mode)
    try:
        await call.message.edit_text(text, reply_markup=history_kb(lang, mode))
    except TelegramBadRequest:
        pass  # "message is not modified" — нажали уже активную кнопку
    await call.answer()


# ---------- /export ----------

@dp.message(Command("export"))
async def export_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tasks = await db.get_tasks(owner_id, owner_type, include_done=True)
    tz = await owner_tz(owner_id, owner_type)

    if not tasks:
        await message.answer(t(lang, "no_tasks"))
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "due_at", "repeat", "remind", "tag", "done"])
    for task in tasks:
        local_dt = to_local(task["due_at"], tz)
        writer.writerow([
            task["id"], task["title"], local_dt.strftime("%Y-%m-%d %H:%M"),
            task["repeat"], task["remind"] or "0", task.get("tag") or "", task["done"],
        ])

    data = buf.getvalue().encode("utf-8-sig")  # BOM для корректного открытия в Excel с кириллицей
    file = BufferedInputFile(data, filename="tasks_export.csv")
    await message.answer_document(file)


# ---------- /dashboard ----------

@dp.message(Command("dashboard"))
async def dashboard_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(SetDashboardPassword.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "dashboard_ask_password"))


@dp.message(SetDashboardPassword.value)
async def dashboard_set_password(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    lang = await owner_lang(owner_id, owner_type)
    password = message.text.strip()
    if len(password) < 4:
        await message.answer(t(lang, "dashboard_password_short"))
        return

    # PBKDF2 с солью (см. dashboard.hash_password); в потоке, чтобы не блокировать event loop
    password_hash = await asyncio.to_thread(dashboard.hash_password, password)
    token = await db.set_owner_dashboard_credentials(owner_id, owner_type, password_hash)
    invalidate_settings(owner_id, owner_type)
    await state.clear()

    base = DASHBOARD_BASE_URL.rstrip("/") if DASHBOARD_BASE_URL else "https://<твой-render-url>"
    url = f"{base}/dashboard/{token}"
    await message.answer(t(lang, "dashboard_ready", url=url))

    # Пытаемся удалить сообщение с паролем из чата — минимальная гигиена,
    # но если прав на удаление нет (например, старое сообщение), просто пропускаем.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ---------- действия с задачей ----------
# Начиная с этого блока владелец (язык/часовой пояс для UI и планировщика)
# берётся из самой задачи (task["owner_id"]/["owner_type"]), а НЕ из чата, где
# нажата кнопка — потому что уведомления по общим задачам чата рассылаются
# личными сообщениями участникам, и там chat.type будет "private".
# "Кто нажал" (для истории) — это всегда call.from_user.id.

@dp.callback_query(F.data.startswith("done_"))
async def mark_done(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_done(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "done")
    _remove_task_jobs(task_id)
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await call.message.edit_text(
        text + f"\n\n{t(lang, 'task_done')}",
        reply_markup=task_kb(task_id, lang, done=True),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("undone_"))
async def mark_undone(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_undone(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "undone")

    # Задача снова активна — нужно заново запланировать уведомления,
    # которые были сняты при отметке "выполнено".
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], task["repeat"], task["remind"], tz,
                  remind_until_done=bool(task.get("remind_until_done")))

    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await call.message.edit_text(
        text + f"\n\n{t(lang, 'task_undone')}",
        reply_markup=task_kb(task_id, lang, done=False),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_confirm(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await call.message.edit_reply_markup(reply_markup=confirm_delete_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("delok_"))
async def delete_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.delete_task(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "deleted")
    _remove_task_jobs(task_id)
    # Если карточка была открыта из /list по номеру — забываем о ней, иначе
    # следующий рендер списка попытается отредактировать уже нерелевантное
    # сообщение "задача удалена".
    view = _get_list_view(task["owner_id"], task["owner_type"])
    view["open_cards"].pop(task_id, None)
    await call.message.edit_text(t(lang, "task_deleted"))
    await call.answer()


@dp.callback_query(F.data.startswith("delno_"))
async def delete_cancel(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("snooze_"))
async def snooze_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    new_due = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
    if task["repeat"] == "none":
        await db.update_task(task_id, due_at=new_due)
        _remove_task_jobs(task_id)
        tz = await owner_tz(task["owner_id"], task["owner_type"])
        schedule_task(task_id, task["owner_id"], task["owner_type"], new_due, "none", "0", tz,
                      remind_until_done=bool(task.get("remind_until_done")))
    else:
        # Повторяющуюся задачу не трогаем (раньше snooze затирал cron одноразовой джобой
        # и сдвигал due_at навсегда) — просто разовое напоминание через час.
        scheduler.add_job(
            send_due, "date", run_date=new_due,
            args=[task["owner_id"], task["owner_type"], task_id], id=f"snooze_{task_id}", replace_existing=True
        )
    await call.answer(t(lang, "snooze_1h"))


@dp.callback_query(F.data.startswith("edit_"))
async def edit_choose_field(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await call.message.edit_reply_markup(reply_markup=edit_field_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("ef_"))
async def edit_field_selected(call: CallbackQuery, state: FSMContext):
    _, field, task_id = call.data.split("_")
    task_id = int(task_id)
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])

    if field == "repeat":
        await call.message.edit_text(t(lang, "ask_repeat"), reply_markup=repeat_kb(lang, prefix=f"editrep_{task_id}"))
        await call.answer()
        return

    await state.set_state(EditTask.value)
    await state.update_data(task_id=task_id, field=field)
    prompts = {
        "title": t(lang, "new_title_prompt"),
        "date": t(lang, "new_date_prompt"),
        "tag": t(lang, "ask_tag"),
    }
    await call.message.edit_text(prompts[field])
    await call.answer()


@dp.callback_query(F.data.startswith("editrep_"))
async def edit_repeat_apply(call: CallbackQuery):
    _, task_id, new_repeat = call.data.split("_", 2)  # в коде повтора бывают "_" (monthly_nth_weekday)
    task_id = int(task_id)
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    await db.update_task(task_id, repeat=new_repeat)
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    _remove_task_jobs(task_id)
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], new_repeat, task["remind"], tz,
                  remind_until_done=bool(task.get("remind_until_done")))
    text = await fmt_task(task)
    await call.message.edit_text(text + f"\n\n{t(lang, 'repeat_updated')}", reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


@dp.message(EditTask.value)
async def edit_value_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id, field = data["task_id"], data["field"]
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"])

    if field == "title":
        await db.update_task(task_id, title=message.text.strip())
    elif field == "tag":
        tag = normalize_tag(message.text)
        await db.update_task(task_id, tag=tag)
    elif field == "date":
        tz = await owner_tz(task["owner_id"], task["owner_type"])
        now_local = datetime.now(tz).replace(tzinfo=None)
        local_dt = parse_human_date(message.text.strip(), now_local)
        if local_dt is None:
            await message.answer(t(lang, "bad_date"))
            return
        due_at = to_utc(local_dt, tz)
        await db.update_task(task_id, due_at=due_at)
        await db.log_history(task_id, message.from_user.id, task["title"], "rescheduled")
        _remove_task_jobs(task_id)
        schedule_task(task_id, task["owner_id"], task["owner_type"], due_at, task["repeat"], task["remind"], tz,
                      remind_until_done=bool(task.get("remind_until_done")))

    await state.clear()
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await message.answer(text + f"\n\n{t(lang, 'task_updated')}", reply_markup=task_kb(task_id, lang, done=updated["done"]), parse_mode="HTML")


# ---------- подзадачи ----------
# Везде ниже авторизация идёт через родительскую задачу (_authorize_task /
# _authorize_subtask) — подзадача сама по себе владельца не хранит.

@dp.callback_query(F.data.startswith("subs_"))
async def subtasks_open(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    subtasks = await db.get_subtasks(task_id)
    text = t(lang, "no_subtasks") if not subtasks else t(lang, "subtasks_title")
    await call.message.edit_text(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subtoggle_"))
async def subtask_toggle(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    authorized = await _authorize_subtask(call, subtask_id)
    if authorized is None:
        return
    sub, task = authorized
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.toggle_subtask(subtask_id)
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(sub["task_id"], subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subdel_"))
async def subtask_delete_confirm(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    authorized = await _authorize_subtask(call, subtask_id)
    if authorized is None:
        return
    sub, task = authorized
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(
        reply_markup=subtasks_kb(sub["task_id"], subtasks, lang, confirm_id=subtask_id)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("subdelok_"))
async def subtask_delete_ok(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    authorized = await _authorize_subtask(call, subtask_id)
    if authorized is None:
        return
    sub, task = authorized
    task_id = sub["task_id"]
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.delete_subtask(subtask_id)
    subtasks = await db.get_subtasks(task_id)
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subdelno_"))
async def subtask_delete_no(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    authorized = await _authorize_subtask(call, subtask_id)
    if authorized is None:
        return
    sub, task = authorized
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(sub["task_id"], subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subadd_"))
async def subtask_add_start(call: CallbackQuery, state: FSMContext):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await state.set_state(AddSubtasks.collecting)
    await state.update_data(task_id=task_id)
    await call.message.answer(t(lang, "subtask_prompt"))
    await call.answer()


@dp.message(AddSubtasks.collecting)
async def subtask_add_collect(message: Message, state: FSMContext):
    # task_id пришёл из state, установленного в subtask_add_start, который уже
    # проверил права для (chat, user) этого FSM-диалога — повторная проверка
    # тут не нужна по той же причине, что и в edit_value_apply: aiogram FSM
    # состояние изолировано по (chat_id, user_id), подменить его нельзя.
    data = await state.get_data()
    task_id = data["task_id"]
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    if message.text.strip() == "/done":
        await state.clear()
        subtasks = await db.get_subtasks(task_id)
        text = t(lang, "no_subtasks") if not subtasks else t(lang, "subtasks_title")
        await message.answer(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
        return

    await db.add_subtask(task_id, message.text.strip())
    await message.answer(t(lang, "subtask_added") + f"\n{t(lang, 'subtask_prompt')}")


@dp.callback_query(F.data.startswith("subback_"))
async def subtasks_back(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


# ---------- фото к задаче ----------
# Тот же паттерн FSM, что и у подзадач (AddSubtasks): кнопка стартует
# состояние с task_id в data, дальше просто шлём боту фото одно за другим,
# "/done" выходит. Авторизация — один раз на старте, см. комментарий в
# subtask_add_collect про изолированность FSM state по (chat_id, user_id).

@dp.callback_query(F.data.startswith("photo_"))
async def task_photo_start(call: CallbackQuery, state: FSMContext):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    count = await db.count_task_photos(task_id)
    if count >= db.MAX_PHOTOS_PER_TASK:
        await call.answer(t(lang, "photo_limit_reached", n=db.MAX_PHOTOS_PER_TASK), show_alert=True)
        return
    await state.set_state(AddTaskPhoto.collecting)
    await state.update_data(task_id=task_id)
    await call.message.answer(t(lang, "photo_prompt"))
    await call.answer()


@dp.message(AddTaskPhoto.collecting, F.photo)
async def task_photo_collect(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    if not task:
        await state.clear()
        return

    if await db.count_task_photos(task_id) >= db.MAX_PHOTOS_PER_TASK:
        await state.clear()
        await message.answer(t(lang, "photo_limit_reached", n=db.MAX_PHOTOS_PER_TASK))
        return

    # Самое большое доступное разрешение — последний элемент message.photo
    # (Telegram кладёт их по возрастанию размера), как и у аватарок дашборда.
    file_id = message.photo[-1].file_id
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    compressed = dashboard.compress_task_photo(buf.getvalue())
    if compressed is None:
        await message.answer(t(lang, "photo_bad_file"))
        return
    data_bytes, mime = compressed
    await db.add_task_photo(task_id, data_bytes, mime, uploaded_by=message.from_user.id)
    await db.log_history(task_id, message.from_user.id, task["title"], "photo_added")
    await message.answer(t(lang, "photo_saved") + f"\n{t(lang, 'photo_prompt')}")


@dp.message(AddTaskPhoto.collecting, Command("done"))
async def task_photo_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = await db.get_task(task_id)
    await state.clear()
    if not task:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    await message.answer(text, reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")


@dp.message(AddTaskPhoto.collecting)
async def task_photo_collect_wrong_type(message: Message, state: FSMContext):
    # Любой не-фото апдейт (текст, стикер, документ без фото-превью) внутри
    # этого состояния — подсказываем, не выходя из режима сбора, чтобы не
    # пришлось заново жать кнопку "📷 Фото" после случайного смайлика.
    data = await state.get_data()
    task = await db.get_task(data.get("task_id")) if data.get("task_id") else None
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    await message.answer(t(lang, "photo_wrong_type"))


# ---------- fallback — сообщение вне состояния ----------

@dp.message()
async def fallback(message: Message):
    # Раньше тут повторялся welcome — выглядело так, будто бот не узнаёт уже
    # знакомого пользователя. fallback срабатывает только на случайный текст
    # ВНЕ состояния FSM (т.е. не на середине /add и т.п.), значит это почти
    # всегда опечатка в команде или просто не-команда — явная подсказка
    # пойти в /help полезнее, чем ещё раз показывать приветствие.
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "unrecognized"))


# ---------- планировщик ----------

def _safe_remove_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass


def _remove_task_jobs(task_id: int):
    """Снимает ВСЕ джобы задачи: due, remind_<id>_<код>, отложенные тихим часом и snooze.
    (Раньше удалялся несуществующий id 'remind_<id>' — напоминания переживали done/редактирование.)"""
    exact = {f"due_{task_id}", f"quiet_due_{task_id}", f"quiet_remind_{task_id}", f"snooze_{task_id}"}
    remind_prefix = f"remind_{task_id}_"
    for job in scheduler.get_jobs():
        if job.id in exact or job.id.startswith(remind_prefix):
            _safe_remove_job(job.id)


def _nth_weekday_cron(local_due: datetime, tz: ZoneInfo) -> CronTrigger:
    """N-й день недели месяца, например 'второе воскресенье' — APScheduler поддерживает
    day='2nd sun' нативно в CronTrigger."""
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    nth = (local_due.day - 1) // 7 + 1
    nth = min(nth, 5)
    day_expr = f"{nth}{['st','nd','rd','th','th'][min(nth-1,4)]} {weekday_codes[local_due.weekday()]}"
    return CronTrigger(day=day_expr, hour=local_due.hour, minute=local_due.minute, timezone=tz)


def schedule_task(task_id: int, owner_id: int, owner_type: str, due_at_utc: datetime, repeat: str, remind: str,
                  tz: ZoneInfo, remind_until_done: bool = False):
    now_utc = datetime.now(ZoneInfo("UTC"))

    remind_codes = [c for c in (remind or "").split(",") if c in REMIND_DELTAS]
    for code in remind_codes:
        remind_time = due_at_utc - REMIND_DELTAS[code]
        if remind_time > now_utc:
            scheduler.add_job(
                send_reminder, "date", run_date=remind_time,
                args=[owner_id, owner_type, task_id], id=f"remind_{task_id}_{code}", replace_existing=True
            )

    local_due = to_local(due_at_utc, tz)
    if repeat == "none" and remind_until_done:
        # Периодическая джоба: срабатывает в due_at и далее каждые NAG_INTERVAL_MINUTES минут,
        # пока задача не выполнена, и полностью останавливается через NAG_MAX_HOURS после срока.
        # Выполнение/удаление снимает джобу (_remove_task_jobs), плюс send_due сам проверяет done.
        end_utc = due_at_utc + timedelta(hours=NAG_MAX_HOURS)
        if end_utc > now_utc:  # иначе окно уже закрыто — не создаём «мёртвую» джобу
            scheduler.add_job(
                send_due, IntervalTrigger(minutes=NAG_INTERVAL_MINUTES, start_date=due_at_utc, end_date=end_utc),
                args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
            )
    elif repeat == "none":
        if due_at_utc > now_utc:
            scheduler.add_job(
                send_due, "date", run_date=due_at_utc,
                args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
            )
    else:
        if repeat == "daily":
            trigger = CronTrigger(hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "weekly":
            trigger = CronTrigger(day_of_week=local_due.weekday(), hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "monthly":
            trigger = CronTrigger(day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "yearly":
            trigger = CronTrigger(month=local_due.month, day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "weekdays":
            trigger = CronTrigger(day_of_week="mon-fri", hour=local_due.hour, minute=local_due.minute, timezone=tz)
        elif repeat == "monthly_nth_weekday":
            trigger = _nth_weekday_cron(local_due, tz)
        else:
            trigger = CronTrigger(hour=local_due.hour, minute=local_due.minute, timezone=tz)
        scheduler.add_job(
            send_due, trigger,
            args=[owner_id, owner_type, task_id], id=f"due_{task_id}", replace_existing=True
        )


async def _recipients(owner_id: int, owner_type: str) -> list[int]:
    """Кому слать личное уведомление: самому пользователю, либо — для общей
    задачи чата — всем известным участникам этого чата (рассылка, а не
    сообщение в сам групповой чат)."""
    if owner_type == "chat":
        return await db.get_chat_members(owner_id)
    return [owner_id]


async def _quiet_end_utc(owner_id: int, owner_type: str) -> datetime | None:
    """Если у владельца СЕЙЧАС тихий час — момент его окончания (UTC), иначе None."""
    quiet = await owner_quiet(owner_id, owner_type)
    if not quiet:
        return None
    tz = await owner_tz(owner_id, owner_type)
    now_local = datetime.now(tz).replace(tzinfo=None)
    if not is_quiet_time(now_local.time(), *quiet):
        return None
    return to_utc(next_quiet_end(now_local, *quiet), tz)


async def _defer_if_quiet(owner_id: int, owner_type: str, task_id: int, kind: str) -> bool:
    """kind: 'due' | 'remind'. В тихий час не шлём, а ставим date-джобу на конец тихого часа.
    id джобы фиксирован (quiet_<kind>_<task_id>) + replace_existing: сколько бы раз за ночь ни
    сработал периодический nag, утром придёт ОДНО сообщение."""
    try:
        run_at = await _quiet_end_utc(owner_id, owner_type)
    except Exception:
        # Если в БД оказались кривые quiet_hours (например, ручная правка) — не
        # роняем всю рассылку из-за этого, а просто ведём себя как без тихого часа.
        logger.exception("Не удалось вычислить тихий час owner=%s/%s, игнорирую", owner_type, owner_id)
        return False
    if run_at is None:
        return False
    func = send_reminder if kind == "remind" else send_due
    scheduler.add_job(
        func, "date", run_date=run_at, args=[owner_id, owner_type, task_id],
        id=f"quiet_{kind}_{task_id}", replace_existing=True
    )
    logger.info("Тихий час: %s по задаче %s отложено до %s", kind, task_id, run_at.isoformat())
    return True


async def send_reminder(owner_id: int, owner_type: str, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
        return
    # Напоминание «скоро наступит» после срока (например, отложенное тихим часом) — уже ложь
    if task["due_at"] <= datetime.now(ZoneInfo("UTC")):
        return
    if await _defer_if_quiet(owner_id, owner_type, task_id, "remind"):
        return
    lang = await owner_lang(owner_id, owner_type)
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(recipient_id, t(lang, "reminder_text", title=task["title"]))
        except TelegramForbiddenError:
            logger.warning("Юзер %s заблокировал бота, пропускаю напоминание по задаче %s", recipient_id, task_id)
        except Exception:
            logger.exception("Не удалось отправить напоминание user_id=%s task_id=%s", recipient_id, task_id)


async def send_due(owner_id: int, owner_type: str, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
        return

    now = datetime.now(ZoneInfo("UTC"))
    nag = bool(task.get("remind_until_done")) and task["repeat"] == "none"

    # Окно «повторять, пока не выполню» закрыто (на случай отложенной тихим часом джобы)
    if nag and now > task["due_at"] + timedelta(hours=NAG_MAX_HOURS):
        return

    if await _defer_if_quiet(owner_id, owner_type, task_id, "due"):
        return

    # Защита от дублей: если уведомление по этой задаче уже уходило недавно
    # (например, рестарт процесса вызвал повторный catch-up) — не шлём снова.
    # Для повторяющихся напоминаний («nag») окно = интервал, чтобы не слать чаще раза в 30 минут.
    is_followup = False
    if task.get("last_notified_at"):
        last = task["last_notified_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo("UTC"))
        is_followup = nag and last >= task["due_at"]
        min_gap = timedelta(minutes=NAG_INTERVAL_MINUTES - 1) if is_followup else timedelta(minutes=5)
        if (now - last) < min_gap:
            logger.info("Пропускаю дублирующее уведомление task_id=%s (последнее было %s назад)", task_id, now - last)
            return

    lang = await owner_lang(owner_id, owner_type)
    text_key = "nag_text" if is_followup else "due_text"
    sent_any = False
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(
                recipient_id, t(lang, text_key, title=task["title"]),
                reply_markup=task_kb(task_id, lang, done=False)
            )
            sent_any = True
        except TelegramForbiddenError:
            logger.warning("Юзер %s заблокировал бота, пропускаю уведомление по задаче %s", recipient_id, task_id)
        except Exception:
            logger.exception("Не удалось отправить уведомление user_id=%s task_id=%s", recipient_id, task_id)
    if sent_any:
        await db.set_last_notified(task_id, now)


DAILY_SUMMARY_HOUR = 21
DAILY_SUMMARY_MINUTE = 0


async def send_daily_summary(owner_id: int, owner_type: str):
    """Ежедневная вечерняя сводка — заодно служит self-check: если сообщение дошло,
    значит и scheduler, и polling живы."""
    lang = await owner_lang(owner_id, owner_type)
    try:
        counts = await db.get_daily_summary_counts(owner_id, owner_type)
    except Exception:
        logger.exception("Не удалось получить статистику для daily_summary owner=%s/%s", owner_type, owner_id)
        return

    overdue_line = ""
    if counts["overdue"] > 0:
        overdue_line = t(lang, "daily_summary_overdue", overdue=counts["overdue"])
    stuck_line = ""
    if counts.get("stuck_repeats", 0) > 0:
        # Повторяющиеся задачи, похоже слетевшие с планировщика (давно не
        # напоминали, хотя срок явно уже проходил) — см. db.get_daily_summary_counts.
        stuck_line = t(lang, "daily_summary_stuck", stuck=counts["stuck_repeats"])

    tz = await owner_tz(owner_id, owner_type)
    # Сводка — self-check, откладывать её нельзя; в тихий час шлём беззвучно
    try:
        silent = await _quiet_end_utc(owner_id, owner_type) is not None
    except Exception:
        logger.exception("Не удалось вычислить тихий час owner=%s/%s для сводки, шлю не беззвучно", owner_type, owner_id)
        silent = False
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            # Серия считается персонально по каждому получателю (история привязана к тому, кто выполнил)
            streak_line = ""
            try:
                n = await _current_streak(recipient_id, tz)
                if n > 0:
                    streak_line = t(lang, "streak_line", n=n)
            except Exception:
                logger.exception("Не удалось посчитать серию user_id=%s", recipient_id)
            text = t(
                lang, "daily_summary", active=counts["active"], done_today=counts["done_today"],
                overdue_line=overdue_line, stuck_line=stuck_line, streak_line=streak_line,
            )
            await bot.send_message(recipient_id, text, parse_mode="HTML", disable_notification=silent)
        except TelegramForbiddenError:
            logger.info("Юзер %s заблокировал бота, пропускаю daily_summary", recipient_id)
        except Exception:
            logger.exception("Не удалось отправить daily_summary user_id=%s", recipient_id)


def schedule_daily_summary(owner_id: int, owner_type: str, tz: ZoneInfo):
    scheduler.add_job(
        send_daily_summary, CronTrigger(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE, timezone=tz),
        args=[owner_id, owner_type], id=f"dailysummary_{owner_type}_{owner_id}", replace_existing=True
    )


async def restore_jobs():
    """Восстанавливает джобы при старте процесса (после редеплоя/рестарта).
    Если дедлайн уже прошёл сегодня, а бот был офлайн — досылает уведомление сразу
    (если не было отправлено недавно — см. защиту в send_due)."""
    now_utc = datetime.now(ZoneInfo("UTC"))

    tasks = await db.get_all_active_tasks()
    for task in tasks:
        owner_id, owner_type = task["owner_id"], task["owner_type"]
        try:
            tz = await owner_tz(owner_id, owner_type)
        except Exception:
            tz = DEFAULT_TZ
        today_start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        due_local = to_local(task["due_at"], tz)

        missed_today = (
            task["repeat"] == "none"
            and task["due_at"] < now_utc
            and due_local >= today_start_local
        )
        if missed_today:
            await send_due(owner_id, owner_type, task["id"])
            if task.get("remind_until_done"):
                # дальше продолжаем «nag»-расписание (окно 24 ч ещё может быть открыто)
                schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                              remind_until_done=True)
        else:
            schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                          remind_until_done=bool(task.get("remind_until_done")))

    # Планируем ежедневную сводку (self-check) для всех, кто когда-либо пользовался ботом
    known_owners = await db.get_all_known_owners()
    for owner in known_owners:
        try:
            tz = await owner_tz(owner["owner_id"], owner["owner_type"])
        except Exception:
            tz = DEFAULT_TZ
        schedule_daily_summary(owner["owner_id"], owner["owner_type"], tz)


# ---------- меню команд Telegram (кнопка "/") ----------
# Telegram сам выбирает нужный набор по language_code КЛИЕНТА пользователя
# (не по языку, выбранному внутри бота через /lang) — так что человек видит
# понятные описания команд ещё до того, как вообще начал ими пользоваться,
# даже если потом выберет в боте другой язык через /lang. Набор без
# language_code (BotCommandScopeDefault без указания языка) — фолбэк для
# всех остальных языков клиента Telegram, которых нет в списке ниже.
_COMMAND_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "add": {"ru": "Добавить задачу", "en": "Add a task", "pl": "Dodaj zadanie"},
    "list": {"ru": "Список задач", "en": "List tasks", "pl": "Lista zadań"},
    "find": {"ru": "Найти задачу по названию", "en": "Find a task by title", "pl": "Znajdź zadanie po nazwie"},
    "tags": {"ru": "Задачи по тегам", "en": "Tasks by tag", "pl": "Zadania według tagów"},
    "history": {"ru": "История и статистика", "en": "History and stats", "pl": "Historia i statystyki"},
    "export": {"ru": "Выгрузить задачи в CSV", "en": "Export tasks to CSV", "pl": "Eksportuj zadania do CSV"},
    "timezone": {"ru": "Сменить часовой пояс", "en": "Change timezone", "pl": "Zmień strefę czasową"},
    "quiet": {"ru": "Тихий час (без уведомлений)", "en": "Quiet hours (no notifications)", "pl": "Cisza nocna (bez powiadomień)"},
    "lang": {"ru": "Сменить язык бота", "en": "Change bot language", "pl": "Zmień język bota"},
    "dashboard": {"ru": "Веб-страница со списком задач", "en": "Web page with your tasks", "pl": "Strona z listą zadań"},
    "cancel": {"ru": "Отменить текущее действие", "en": "Cancel current action", "pl": "Anuluj bieżącą czynność"},
    "help": {"ru": "Все команды и что они делают", "en": "All commands explained", "pl": "Wszystkie komendy — opis"},
}
# Порядок в меню — как в /help, а не алфавитный (Telegram сохраняет порядок списка)
_COMMAND_ORDER = ["add", "list", "find", "tags", "history", "export", "timezone", "quiet", "lang", "dashboard", "cancel", "help"]


async def setup_bot_commands():
    for lang in ("ru", "en", "pl"):
        commands = [
            BotCommand(command=cmd, description=_COMMAND_DESCRIPTIONS[cmd][lang])
            for cmd in _COMMAND_ORDER
        ]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code=lang)
    # Фолбэк без language_code — для клиентов Telegram на языках вне ru/en/pl
    # (например, de, fr): используем английские описания как наиболее
    # универсальный вариант.
    fallback_commands = [
        BotCommand(command=cmd, description=_COMMAND_DESCRIPTIONS[cmd]["en"])
        for cmd in _COMMAND_ORDER
    ]
    await bot.set_my_commands(fallback_commands, scope=BotCommandScopeDefault())
    logger.info("Меню команд Telegram настроено (ru/en/pl + фолбэк)")


# ---------- health-check веб-сервер (нужен Render Web Service, чтобы видеть открытый порт) ----------

async def health(request):
    return aioweb.Response(text="ok")


async def run_health_server():
    app = aioweb.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    dashboard.register_dashboard_routes(app)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = aioweb.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health server listening on port %s", port)
    return runner


# ---------- graceful shutdown ----------

_shutdown_event = asyncio.Event()


def _handle_signal(sig_name: str):
    logger.info("Получен сигнал %s, начинаю остановку...", sig_name)
    _shutdown_event.set()


# ---------- запуск ----------

async def main():
    await db.init_db()
    try:
        await setup_bot_commands()
    except Exception:
        # Не критично для работы бота — если Telegram API моргнул на старте,
        # просто остаёмся со старым меню (или без него), не блокируем запуск.
        logger.exception("Не удалось настроить меню команд Telegram")
    scheduler.start()
    await restore_jobs()
    health_runner = await run_health_server()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s.name))
        except NotImplementedError:
            pass  # Windows — сигналы не поддерживаются так, но на Render это Linux

    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    done, pending = await asyncio.wait(
        [polling_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
    )

    if shutdown_task in done:
        logger.info("Останавливаю polling...")
        await dp.stop_polling()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    logger.info("Останавливаю scheduler...")
    scheduler.shutdown(wait=False)
    logger.info("Закрываю health-сервер...")
    await health_runner.cleanup()
    logger.info("Закрываю пул БД...")
    await db.close_db()
    logger.info("Остановка завершена.")


if __name__ == "__main__":
    asyncio.run(main())