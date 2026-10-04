import asyncio
import hashlib
import logging
import os
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web as aioweb
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Chat
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

import dashboard
import db
from date_parser import parse_human_date
from i18n import t

API_TOKEN = os.environ["BOT_TOKEN"]
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "")  # напр. https://task-reminder-bot-xxxx.onrender.com
DEFAULT_TZ = ZoneInfo("Europe/Warsaw")
PAGE_SIZE = 5

# Короткий список популярных поясов для кнопок; пользователь может ввести свой вручную
TZ_CHOICES = ["Europe/Warsaw", "Europe/Moscow", "Europe/Kyiv", "Asia/Almaty", "UTC"]

GROUP_CHAT_TYPES = {"group", "supergroup"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)

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
    tag = State()


class EditTask(StatesGroup):
    value = State()


class AddSubtasks(StatesGroup):
    collecting = State()


class SetTimezone(StatesGroup):
    value = State()


class SetDashboardPassword(StatesGroup):
    value = State()


class FindTask(StatesGroup):
    query = State()


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


def to_utc(local_dt: datetime, tz: ZoneInfo) -> datetime:
    return local_dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def to_local(utc_dt: datetime, tz: ZoneInfo) -> datetime:
    return utc_dt.astimezone(tz)


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
    kb.append([InlineKeyboardButton(text="✅ " + t(lang, "btn_done"), callback_data=f"{prefix}done")])
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


def pagination_kb(page: int, has_next: bool, prefix: str = "page"):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}_{page-1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}_{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


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


async def fmt_task(t_row: dict) -> str:
    """Язык и часовой пояс берутся из владельца самой задачи (t_row['owner_id']/
    ['owner_type']), а не из того, кто сейчас смотрит карточку — так в групповом
    чате все видят задачу в едином, настроенном для чата виде."""
    lang = await owner_lang(t_row["owner_id"], t_row["owner_type"])
    tz = await owner_tz(t_row["owner_id"], t_row["owner_type"])
    local_dt = to_local(t_row["due_at"], tz)
    status = "✅" if t_row["done"] else "⏳"
    text = f"{status} <b>{t_row['title']}</b>\n📅 {local_dt.strftime('%d.%m.%Y %H:%M')}"
    if t_row["repeat"] != "none":
        text += f"\n🔁 {fmt_repeat(t_row['repeat'], lang)}"
    if t_row["remind"] and t_row["remind"] != "0":
        text += f"\n🔔 {fmt_remind(t_row['remind'], lang)}"
    if t_row.get("tag"):
        text += f"\n🏷 {t_row['tag']}"
    text += f"\n<code>#{t_row['id']}</code>"
    return text


# ---------- базовые команды ----------

@dp.message(CommandStart())
async def start(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "welcome"))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start(message)


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
    await _apply_timezone(owner_id, owner_type, message.text.strip(), message)
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
    schedule_daily_summary(owner_id, owner_type, ZoneInfo(tz_name))
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


# ---------- добавление задачи ----------

@dp.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(AddTask.title)
    await state.update_data(owner_id=owner_id, owner_type=owner_type, actor_id=message.from_user.id)
    await message.answer(t(lang, "ask_title"))


@dp.message(AddTask.title)
async def add_title(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    await state.update_data(title=message.text.strip())
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

    await state.update_data(due_at=to_utc(local_dt, tz).isoformat())
    await state.set_state(AddTask.repeat)
    await message.answer(t(lang, "ask_repeat"), reply_markup=repeat_kb(lang))


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


@dp.message(AddTask.tag)
async def add_tag(message: Message, state: FSMContext):
    tag = None if message.text.strip() == "/skip" else message.text.strip()
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

    task_id = await db.add_task(
        owner_id=owner_id,
        owner_type=owner_type,
        title=data["title"],
        due_at=due_at,
        repeat=data["repeat"],
        remind=data["remind"],
        tag=tag,
    )
    await db.log_history(task_id, actor_id, data["title"], "created")
    schedule_task(task_id, owner_id, owner_type, due_at, data["repeat"], data["remind"], tz)
    if f"dailysummary_{owner_type}_{owner_id}" not in {j.id for j in scheduler.get_jobs()}:
        schedule_daily_summary(owner_id, owner_type, tz)

    local_str = to_local(due_at, tz).strftime("%d.%m.%Y %H:%M")
    await target_message.answer(t(lang, "task_added", title=data["title"], date=local_str))
    await state.clear()


# ---------- список задач ----------

def _get_list_view(owner_id: int, owner_type: str) -> dict:
    return _list_view_cache.setdefault((owner_type, owner_id), {"filter_mode": "all", "sort_by": "date"})


_list_view_cache: dict[tuple[str, int], dict] = {}
_search_cache: dict[tuple[str, int], dict] = {}


async def _render_list_tasks(answer, owner_id: int, owner_type: str, lang: str, view: dict):
    """answer — awaitable вида message.answer / call.message.answer, уже привязанное к чату."""
    tasks = await db.get_tasks(owner_id, owner_type, filter_mode=view["filter_mode"], sort_by=view["sort_by"])
    if not tasks:
        await answer(t(lang, "no_tasks"))
        return
    for task in tasks[:PAGE_SIZE]:
        text = await fmt_task(task)
        await answer(text, reply_markup=task_kb(task["id"], lang, done=task["done"]), parse_mode="HTML")
    if len(tasks) > PAGE_SIZE:
        await answer(t(lang, "showing_first", page_size=PAGE_SIZE, total=len(tasks)))


@dp.message(Command("list"))
async def list_tasks(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    view = _get_list_view(owner_id, owner_type)
    await message.answer(t(lang, "list_controls"), reply_markup=list_filter_kb(lang, view["filter_mode"], view["sort_by"]))
    await _render_list_tasks(message.answer, owner_id, owner_type, lang, view)


@dp.callback_query(F.data.startswith("listf_"))
async def list_filter_pick(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    mode = call.data.split("_", 1)[1]
    view = _get_list_view(owner_id, owner_type)
    view["filter_mode"] = mode
    await call.message.edit_reply_markup(reply_markup=list_filter_kb(lang, view["filter_mode"], view["sort_by"]))
    await _render_list_tasks(call.message.answer, owner_id, owner_type, lang, view)
    await call.answer()


@dp.callback_query(F.data.startswith("lists_"))
async def list_sort_pick(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    sort_by = call.data.split("_", 1)[1]
    view = _get_list_view(owner_id, owner_type)
    view["sort_by"] = sort_by
    await call.message.edit_reply_markup(reply_markup=list_filter_kb(lang, view["filter_mode"], view["sort_by"]))
    await _render_list_tasks(call.message.answer, owner_id, owner_type, lang, view)
    await call.answer()


# ---------- /find — текстовый поиск ----------

@dp.message(Command("find"))
async def find_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(FindTask.query)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "ask_find_query"))


async def _render_find_page(owner_id: int, owner_type: str, lang: str, page: int):
    """Возвращает (текст_заголовка, клавиатура_пагинации, задачи_на_странице)."""
    cache = _search_cache.get((owner_type, owner_id))
    results = cache["results"] if cache else []
    if not results:
        return t(lang, "no_results"), None, []
    start = page * PAGE_SIZE
    page_items = results[start:start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < len(results)
    header = t(lang, "find_results", total=len(results))
    kb = pagination_kb(page, has_next, prefix="findpage")
    return header, kb, page_items


@dp.message(FindTask.query)
async def find_query(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    lang = await owner_lang(owner_id, owner_type)
    await state.clear()
    query_text = message.text.strip()
    results = await db.search_tasks(owner_id, owner_type, query_text)
    _search_cache[(owner_type, owner_id)] = {"query": query_text, "results": results}

    header, kb, page_items = await _render_find_page(owner_id, owner_type, lang, 0)
    await message.answer(header, reply_markup=kb)
    for task in page_items:
        text = await fmt_task(task)
        await message.answer(text, reply_markup=task_kb(task["id"], lang, done=task["done"]), parse_mode="HTML")


@dp.callback_query(F.data.startswith("findpage_"))
async def find_page_nav(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    page = int(call.data.split("_", 1)[1])

    header, kb, page_items = await _render_find_page(owner_id, owner_type, lang, page)
    await call.message.edit_text(header, reply_markup=kb)
    for task in page_items:
        text = await fmt_task(task)
        await call.message.answer(text, reply_markup=task_kb(task["id"], lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


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


@dp.callback_query(F.data.startswith("tagf_"))
async def tags_filter(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tag = call.data.split("_", 1)[1]
    tasks = await db.get_tasks(owner_id, owner_type, tag=tag)
    if not tasks:
        await call.message.answer(t(lang, "no_tasks"))
        await call.answer()
        return
    for task in tasks[:PAGE_SIZE]:
        text = await fmt_task(task)
        await call.message.answer(text, reply_markup=task_kb(task["id"], lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


# ---------- /history ----------
# История остаётся личной (кто что сделал), даже для общих задач чата — поэтому
# ключом остаётся message.from_user.id, а не owner_id.

@dp.message(Command("history"))
async def history_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    events = await db.get_history(message.from_user.id)
    if not events:
        await message.answer(t(lang, "no_history"))
        return
    tz = await owner_tz(owner_id, owner_type)
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
    await message.answer("\n".join(lines))


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

    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "due_at", "repeat", "remind", "tag", "done"])
    for task in tasks:
        local_dt = to_local(task["due_at"], tz)
        writer.writerow([
            task["id"], task["title"], local_dt.strftime("%Y-%m-%d %H:%M"),
            task["repeat"], task["remind"] or "0", task.get("tag") or "", task["done"],
        ])

    from aiogram.types import BufferedInputFile
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

    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
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
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_done(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "done")
    _safe_remove_job(f"due_{task_id}")
    _safe_remove_job(f"remind_{task_id}")
    await call.message.edit_text(
        call.message.text + f"\n\n{t(lang, 'task_done')}",
        reply_markup=task_kb(task_id, lang, done=True),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("undone_"))
async def mark_undone(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_undone(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "undone")

    # Задача снова активна — нужно заново запланировать уведомления,
    # которые были сняты при отметке "выполнено".
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], task["repeat"], task["remind"], tz)

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
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    await call.message.edit_reply_markup(reply_markup=confirm_delete_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("delok_"))
async def delete_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    await db.delete_task(task_id)
    if task:
        await db.log_history(task_id, call.from_user.id, task["title"], "deleted")
    _safe_remove_job(f"due_{task_id}")
    _safe_remove_job(f"remind_{task_id}")
    await call.message.edit_text(t(lang, "task_deleted"))
    await call.answer()


@dp.callback_query(F.data.startswith("delno_"))
async def delete_cancel(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if task:
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        text = await fmt_task(task)
        await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("snooze_"))
async def snooze_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    new_due = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
    await db.update_task(task_id, due_at=new_due)
    _safe_remove_job(f"due_{task_id}")
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    schedule_task(task_id, task["owner_id"], task["owner_type"], new_due, "none", "0", tz)
    await call.answer(t(lang, "snooze_1h"))


@dp.callback_query(F.data.startswith("edit_"))
async def edit_choose_field(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    await call.message.edit_reply_markup(reply_markup=edit_field_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("ef_"))
async def edit_field_selected(call: CallbackQuery, state: FSMContext):
    _, field, task_id = call.data.split("_")
    task_id = int(task_id)
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"

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
    _, task_id, new_repeat = call.data.split("_")
    task_id = int(task_id)
    await db.update_task(task_id, repeat=new_repeat)
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    _safe_remove_job(f"due_{task_id}")
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], new_repeat, task["remind"], tz)
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
        tag = None if message.text.strip() == "/skip" else message.text.strip()
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
        _safe_remove_job(f"due_{task_id}")
        _safe_remove_job(f"remind_{task_id}")
        schedule_task(task_id, task["owner_id"], task["owner_type"], due_at, task["repeat"], task["remind"], tz)

    await state.clear()
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await message.answer(text + f"\n\n{t(lang, 'task_updated')}", reply_markup=task_kb(task_id, lang, done=updated["done"]), parse_mode="HTML")


# ---------- подзадачи ----------

async def _task_lang(task_id: int) -> str:
    task = await db.get_task(task_id)
    return await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"


@dp.callback_query(F.data.startswith("subs_"))
async def subtasks_open(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    lang = await _task_lang(task_id)
    subtasks = await db.get_subtasks(task_id)
    text = t(lang, "no_subtasks") if not subtasks else t(lang, "subtasks_title")
    await call.message.edit_text(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subtoggle_"))
async def subtask_toggle(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    lang = await _task_lang(sub["task_id"])
    await db.toggle_subtask(subtask_id)
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(sub["task_id"], subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subdel_"))
async def subtask_delete_confirm(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    lang = await _task_lang(sub["task_id"])
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(
        reply_markup=subtasks_kb(sub["task_id"], subtasks, lang, confirm_id=subtask_id)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("subdelok_"))
async def subtask_delete_ok(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    task_id = sub["task_id"]
    lang = await _task_lang(task_id)
    await db.delete_subtask(subtask_id)
    subtasks = await db.get_subtasks(task_id)
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subdelno_"))
async def subtask_delete_no(call: CallbackQuery):
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    lang = await _task_lang(sub["task_id"])
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(sub["task_id"], subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subadd_"))
async def subtask_add_start(call: CallbackQuery, state: FSMContext):
    task_id = int(call.data.split("_")[1])
    lang = await _task_lang(task_id)
    await state.set_state(AddSubtasks.collecting)
    await state.update_data(task_id=task_id)
    await call.message.answer(t(lang, "subtask_prompt"))
    await call.answer()


@dp.message(AddSubtasks.collecting)
async def subtask_add_collect(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    lang = await _task_lang(task_id)
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
    task = await db.get_task(task_id)
    if task:
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        text = await fmt_task(task)
        await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"]), parse_mode="HTML")
    await call.answer()


# ---------- fallback — сообщение вне состояния ----------

@dp.message()
async def fallback(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "welcome"))


# ---------- планировщик ----------

def _safe_remove_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass


def _nth_weekday_cron(local_due: datetime, tz: ZoneInfo) -> CronTrigger:
    """N-й день недели месяца, например 'второе воскресенье' — APScheduler поддерживает
    day='2nd sun' нативно в CronTrigger."""
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    nth = (local_due.day - 1) // 7 + 1
    nth = min(nth, 5)
    day_expr = f"{nth}{['st','nd','rd','th','th'][min(nth-1,4)]} {weekday_codes[local_due.weekday()]}"
    return CronTrigger(day=day_expr, hour=local_due.hour, minute=local_due.minute, timezone=tz)


def schedule_task(task_id: int, owner_id: int, owner_type: str, due_at_utc: datetime, repeat: str, remind: str, tz: ZoneInfo):
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
    if repeat == "none":
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


async def send_reminder(owner_id: int, owner_type: str, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
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

    # Защита от дублей: если уведомление по этой задаче уже уходило недавно
    # (например, рестарт процесса вызвал повторный catch-up) — не шлём снова.
    now = datetime.now(ZoneInfo("UTC"))
    if task.get("last_notified_at"):
        last = task["last_notified_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo("UTC"))
        if (now - last) < timedelta(minutes=5):
            logger.info("Пропускаю дублирующее уведомление task_id=%s (последнее было %s назад)", task_id, now - last)
            return

    lang = await owner_lang(owner_id, owner_type)
    sent_any = False
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(
                recipient_id, t(lang, "due_text", title=task["title"]),
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

    text = t(
        lang, "daily_summary",
        active=counts["active"], done_today=counts["done_today"], overdue_line=overdue_line
    )
    for recipient_id in await _recipients(owner_id, owner_type):
        try:
            await bot.send_message(recipient_id, text, parse_mode="Markdown")
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
        else:
            schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz)

    # Планируем ежедневную сводку (self-check) для всех, кто когда-либо пользовался ботом
    known_owners = await db.get_all_known_owners()
    for owner in known_owners:
        try:
            tz = await owner_tz(owner["owner_id"], owner["owner_type"])
        except Exception:
            tz = DEFAULT_TZ
        schedule_daily_summary(owner["owner_id"], owner["owner_type"], tz)


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
