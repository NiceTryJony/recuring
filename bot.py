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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)

REPEAT_OPTIONS = {
    "none": {"ru": "Без повтора", "en": "No repeat"},
    "daily": {"ru": "Каждый день", "en": "Every day"},
    "weekly": {"ru": "Каждую неделю", "en": "Every week"},
    "monthly": {"ru": "Каждый месяц", "en": "Every month"},
    "yearly": {"ru": "Каждый год", "en": "Every year"},
    "weekdays": {"ru": "По будням", "en": "Weekdays"},
    "monthly_nth_weekday": {"ru": "N-й день недели месяца", "en": "Nth weekday of month"},
}
REMIND_OPTIONS = {
    "0": {"ru": "Без напоминания", "en": "No reminder"},
    "1h": {"ru": "За 1 час", "en": "1 hour before"},
    "1d": {"ru": "За 1 день", "en": "1 day before"},
    "3d": {"ru": "За 3 дня", "en": "3 days before"},
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


# ---------- helpers: пользовательские настройки ----------

_settings_cache: dict[int, dict] = {}


async def get_settings(user_id: int) -> dict:
    if user_id not in _settings_cache:
        _settings_cache[user_id] = await db.get_user_settings(user_id)
    return _settings_cache[user_id]


def invalidate_settings(user_id: int):
    _settings_cache.pop(user_id, None)


async def user_tz(user_id: int) -> ZoneInfo:
    s = await get_settings(user_id)
    try:
        return ZoneInfo(s["timezone"])
    except ZoneInfoNotFoundError:
        return DEFAULT_TZ


async def user_lang(user_id: int) -> str:
    s = await get_settings(user_id)
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
    kb.append([InlineKeyboardButton(text="✅ " + ("Готово" if lang == "ru" else "Done"), callback_data=f"{prefix}done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def task_kb(task_id: int, lang: str):
    done_label = "✅ Выполнено" if lang == "ru" else "✅ Done"
    edit_label = "✏️ Изменить" if lang == "ru" else "✏️ Edit"
    del_label = "🗑 Удалить" if lang == "ru" else "🗑 Delete"
    snooze_label = "⏰ +1ч" if lang == "ru" else "⏰ +1h"
    sub_label = "📋 Подпункты" if lang == "ru" else "📋 Subtasks"
    kb = [
        [InlineKeyboardButton(text=done_label, callback_data=f"done_{task_id}")],
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
    yes = "Да, удалить" if lang == "ru" else "Yes, delete"
    no = "Отмена" if lang == "ru" else "Cancel"
    kb = [[
        InlineKeyboardButton(text=yes, callback_data=f"delok_{task_id}"),
        InlineKeyboardButton(text=no, callback_data=f"delno_{task_id}"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def edit_field_kb(task_id: int, lang: str):
    labels = {
        "title": "Название" if lang == "ru" else "Title",
        "date": "Дату" if lang == "ru" else "Date",
        "repeat": "Повтор" if lang == "ru" else "Repeat",
        "tag": "Тег" if lang == "ru" else "Tag",
    }
    kb = [[InlineKeyboardButton(text=v, callback_data=f"ef_{k}_{task_id}")] for k, v in labels.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def pagination_kb(page: int, has_next: bool):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def tags_kb(tags: list[str]):
    kb = [[InlineKeyboardButton(text=tag, callback_data=f"tagf_{tag}")] for tag in tags]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def timezone_kb():
    kb = [[InlineKeyboardButton(text=tz, callback_data=f"tz_{tz}")] for tz in TZ_CHOICES]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def lang_kb():
    kb = [
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def subtasks_kb(task_id: int, subtasks: list[dict], lang: str):
    kb = []
    for st in subtasks:
        mark = "☑️" if st["done"] else "⬜"
        kb.append([
            InlineKeyboardButton(text=f"{mark} {st['title']}", callback_data=f"subtoggle_{st['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"subdel_{st['id']}"),
        ])
    add_label = "➕ Добавить подпункт" if lang == "ru" else "➕ Add subtask"
    back_label = "⬅️ Назад" if lang == "ru" else "⬅️ Back"
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


async def fmt_task(t_row: dict, user_id: int) -> str:
    lang = await user_lang(user_id)
    tz = await user_tz(user_id)
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
    lang = await user_lang(message.from_user.id)
    await message.answer(t(lang, "welcome"))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start(message)


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    await state.clear()
    await message.answer(t(lang, "cancelled"))


# ---------- /timezone ----------

@dp.message(Command("timezone"))
async def timezone_start(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    await state.set_state(SetTimezone.value)
    await message.answer(t(lang, "choose_timezone"), reply_markup=timezone_kb())


@dp.callback_query(F.data.startswith("tz_"))
async def timezone_pick(call: CallbackQuery, state: FSMContext):
    tz_name = call.data.split("_", 1)[1]
    await _apply_timezone(call.from_user.id, tz_name, call.message)
    await state.clear()
    await call.answer()


@dp.message(SetTimezone.value)
async def timezone_custom(message: Message, state: FSMContext):
    await _apply_timezone(message.from_user.id, message.text.strip(), message)
    await state.clear()


async def _apply_timezone(user_id: int, tz_name: str, target_message: Message):
    lang = await user_lang(user_id)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await target_message.answer(t(lang, "timezone_invalid"))
        return
    await db.set_user_timezone(user_id, tz_name)
    invalidate_settings(user_id)
    await target_message.answer(t(lang, "timezone_set", tz=tz_name))


# ---------- /lang ----------

@dp.message(Command("lang"))
async def lang_start(message: Message):
    lang = await user_lang(message.from_user.id)
    await message.answer(t(lang, "choose_lang"), reply_markup=lang_kb())


@dp.callback_query(F.data.startswith("lang_"))
async def lang_pick(call: CallbackQuery):
    new_lang = call.data.split("_", 1)[1]
    await db.set_user_language(call.from_user.id, new_lang)
    invalidate_settings(call.from_user.id)
    await call.message.edit_text(t(new_lang, "lang_set"))
    await call.answer()


# ---------- добавление задачи ----------

@dp.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    await state.set_state(AddTask.title)
    await message.answer(t(lang, "ask_title"))


@dp.message(AddTask.title)
async def add_title(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    await state.update_data(title=message.text.strip())
    await state.set_state(AddTask.date)
    await message.answer(t(lang, "ask_date"))


@dp.message(AddTask.date)
async def add_date(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    tz = await user_tz(message.from_user.id)
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
    lang = await user_lang(call.from_user.id)
    repeat = call.data.split("_", 1)[1]
    await state.update_data(repeat=repeat, remind_selected=[])
    await state.set_state(AddTask.remind)
    await call.message.edit_text(t(lang, "ask_remind"), reply_markup=remind_kb(lang))
    await call.answer()


@dp.callback_query(AddTask.remind, F.data.startswith("rem_"))
async def add_remind_toggle(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    code = call.data.split("_", 1)[1]

    if code == "done":
        data = await state.get_data()
        selected = data.get("remind_selected", [])
        remind_value = ",".join(selected) if selected else "0"
        await state.update_data(remind=remind_value)
        await state.set_state(AddTask.tag)
        await call.message.edit_text(t(lang, "ask_tag"))
        await call.answer()
        return

    data = await state.get_data()
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
    await _finalize_add_task(message.from_user.id, state, tag, message)


@dp.message(Command("skip"), AddTask.tag)
async def add_tag_skip(message: Message, state: FSMContext):
    await _finalize_add_task(message.from_user.id, state, None, message)


async def _finalize_add_task(user_id: int, state: FSMContext, tag: str | None, target_message: Message):
    lang = await user_lang(user_id)
    tz = await user_tz(user_id)
    data = await state.get_data()
    due_at = datetime.fromisoformat(data["due_at"])

    task_id = await db.add_task(
        user_id=user_id,
        title=data["title"],
        due_at=due_at,
        repeat=data["repeat"],
        remind=data["remind"],
        tag=tag,
    )
    await db.log_history(task_id, user_id, data["title"], "created")
    schedule_task(task_id, user_id, due_at, data["repeat"], data["remind"], tz)

    local_str = to_local(due_at, tz).strftime("%d.%m.%Y %H:%M")
    await target_message.answer(t(lang, "task_added", title=data["title"], date=local_str))
    await state.clear()


# ---------- список задач ----------

@dp.message(Command("list"))
async def list_tasks(message: Message):
    lang = await user_lang(message.from_user.id)
    tasks = await db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer(t(lang, "no_tasks"))
        return
    for task in tasks[:PAGE_SIZE]:
        text = await fmt_task(task, message.from_user.id)
        await message.answer(text, reply_markup=task_kb(task["id"], lang), parse_mode="HTML")
    if len(tasks) > PAGE_SIZE:
        more = f"Показаны первые {PAGE_SIZE} из {len(tasks)}." if lang == "ru" else f"Showing first {PAGE_SIZE} of {len(tasks)}."
        await message.answer(more)


# ---------- /tags ----------

@dp.message(Command("tags"))
async def tags_cmd(message: Message):
    lang = await user_lang(message.from_user.id)
    tags = await db.get_user_tags(message.from_user.id)
    if not tags:
        await message.answer(t(lang, "no_tags"))
        return
    await message.answer(t(lang, "choose_tag"), reply_markup=tags_kb(tags))


@dp.callback_query(F.data.startswith("tagf_"))
async def tags_filter(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    tag = call.data.split("_", 1)[1]
    tasks = await db.get_tasks(call.from_user.id, tag=tag)
    if not tasks:
        await call.message.answer(t(lang, "no_tasks"))
        await call.answer()
        return
    for task in tasks[:PAGE_SIZE]:
        text = await fmt_task(task, call.from_user.id)
        await call.message.answer(text, reply_markup=task_kb(task["id"], lang), parse_mode="HTML")
    await call.answer()


# ---------- /history ----------

@dp.message(Command("history"))
async def history_cmd(message: Message):
    lang = await user_lang(message.from_user.id)
    events = await db.get_history(message.from_user.id)
    if not events:
        await message.answer(t(lang, "no_history"))
        return
    tz = await user_tz(message.from_user.id)
    event_labels = {
        "created": "➕" if lang == "ru" else "➕",
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
    lang = await user_lang(message.from_user.id)
    tasks = await db.get_tasks(message.from_user.id, include_done=True)
    tz = await user_tz(message.from_user.id)

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
    lang = await user_lang(message.from_user.id)
    await state.set_state(SetDashboardPassword.value)
    await message.answer(t(lang, "dashboard_ask_password"))


@dp.message(SetDashboardPassword.value)
async def dashboard_set_password(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    password = message.text.strip()
    if len(password) < 4:
        await message.answer(t(lang, "dashboard_password_short"))
        return

    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    token = await db.set_dashboard_credentials(message.from_user.id, password_hash)
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

@dp.callback_query(F.data.startswith("done_"))
async def mark_done(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    await db.mark_done(task_id)
    if task:
        await db.log_history(task_id, call.from_user.id, task["title"], "done")
    _safe_remove_job(f"due_{task_id}")
    _safe_remove_job(f"remind_{task_id}")
    await call.message.edit_text(call.message.text + f"\n\n{t(lang, 'task_done')}")
    await call.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_confirm(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    await call.message.edit_reply_markup(reply_markup=confirm_delete_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("delok_"))
async def delete_task(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    await db.delete_task(task_id)
    if task:
        await db.log_history(task_id, call.from_user.id, task["title"], "deleted")
    _safe_remove_job(f"due_{task_id}")
    _safe_remove_job(f"remind_{task_id}")
    await call.message.edit_text(t(lang, "task_deleted"))
    await call.answer()


@dp.callback_query(F.data.startswith("delno_"))
async def delete_cancel(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if task:
        text = await fmt_task(task, call.from_user.id)
        await call.message.edit_text(text, reply_markup=task_kb(task_id, lang), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("snooze_"))
async def snooze_task(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return
    new_due = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
    await db.update_task(task_id, due_at=new_due)
    _safe_remove_job(f"due_{task_id}")
    tz = await user_tz(call.from_user.id)
    schedule_task(task_id, call.from_user.id, new_due, "none", "0", tz)
    await call.answer(t(lang, "snooze_1h"))


@dp.callback_query(F.data.startswith("edit_"))
async def edit_choose_field(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    await call.message.edit_reply_markup(reply_markup=edit_field_kb(task_id, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("ef_"))
async def edit_field_selected(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    _, field, task_id = call.data.split("_")
    task_id = int(task_id)

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
    lang = await user_lang(call.from_user.id)
    _, task_id, new_repeat = call.data.split("_")
    task_id = int(task_id)
    await db.update_task(task_id, repeat=new_repeat)
    task = await db.get_task(task_id)
    tz = await user_tz(call.from_user.id)
    _safe_remove_job(f"due_{task_id}")
    schedule_task(task_id, task["user_id"], task["due_at"], new_repeat, task["remind"], tz)
    text = await fmt_task(task, call.from_user.id)
    await call.message.edit_text(text + f"\n\n{t(lang, 'repeat_updated')}", reply_markup=task_kb(task_id, lang), parse_mode="HTML")
    await call.answer()


@dp.message(EditTask.value)
async def edit_value_apply(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    data = await state.get_data()
    task_id, field = data["task_id"], data["field"]

    if field == "title":
        await db.update_task(task_id, title=message.text.strip())
    elif field == "tag":
        tag = None if message.text.strip() == "/skip" else message.text.strip()
        await db.update_task(task_id, tag=tag)
    elif field == "date":
        tz = await user_tz(message.from_user.id)
        now_local = datetime.now(tz).replace(tzinfo=None)
        local_dt = parse_human_date(message.text.strip(), now_local)
        if local_dt is None:
            await message.answer(t(lang, "bad_date"))
            return
        due_at = to_utc(local_dt, tz)
        await db.update_task(task_id, due_at=due_at)
        task = await db.get_task(task_id)
        await db.log_history(task_id, message.from_user.id, task["title"], "rescheduled")
        _safe_remove_job(f"due_{task_id}")
        _safe_remove_job(f"remind_{task_id}")
        schedule_task(task_id, task["user_id"], due_at, task["repeat"], task["remind"], tz)

    await state.clear()
    task = await db.get_task(task_id)
    text = await fmt_task(task, message.from_user.id)
    await message.answer(text + f"\n\n{t(lang, 'task_updated')}", reply_markup=task_kb(task_id, lang), parse_mode="HTML")


# ---------- подзадачи ----------

@dp.callback_query(F.data.startswith("subs_"))
async def subtasks_open(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    subtasks = await db.get_subtasks(task_id)
    text = t(lang, "no_subtasks") if not subtasks else ("📋 Подпункты:" if lang == "ru" else "📋 Subtasks:")
    await call.message.edit_text(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subtoggle_"))
async def subtask_toggle(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    await db.toggle_subtask(subtask_id)
    subtasks = await db.get_subtasks(sub["task_id"])
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(sub["task_id"], subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subdel_"))
async def subtask_delete(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    subtask_id = int(call.data.split("_")[1])
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return
    task_id = sub["task_id"]
    await db.delete_subtask(subtask_id)
    subtasks = await db.get_subtasks(task_id)
    await call.message.edit_reply_markup(reply_markup=subtasks_kb(task_id, subtasks, lang))
    await call.answer()


@dp.callback_query(F.data.startswith("subadd_"))
async def subtask_add_start(call: CallbackQuery, state: FSMContext):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    await state.set_state(AddSubtasks.collecting)
    await state.update_data(task_id=task_id)
    await call.message.answer(t(lang, "subtask_prompt"))
    await call.answer()


@dp.message(AddSubtasks.collecting)
async def subtask_add_collect(message: Message, state: FSMContext):
    lang = await user_lang(message.from_user.id)
    if message.text.strip() == "/done":
        data = await state.get_data()
        task_id = data["task_id"]
        await state.clear()
        subtasks = await db.get_subtasks(task_id)
        text = t(lang, "no_subtasks") if not subtasks else ("📋 Подпункты:" if lang == "ru" else "📋 Subtasks:")
        await message.answer(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
        return

    data = await state.get_data()
    task_id = data["task_id"]
    await db.add_subtask(task_id, message.text.strip())
    await message.answer(t(lang, "subtask_added") + f"\n{t(lang, 'subtask_prompt')}")


@dp.callback_query(F.data.startswith("subback_"))
async def subtasks_back(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if task:
        text = await fmt_task(task, call.from_user.id)
        await call.message.edit_text(text, reply_markup=task_kb(task_id, lang), parse_mode="HTML")
    await call.answer()


# ---------- fallback — сообщение вне состояния ----------

@dp.message()
async def fallback(message: Message):
    lang = await user_lang(message.from_user.id)
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


def schedule_task(task_id: int, user_id: int, due_at_utc: datetime, repeat: str, remind: str, tz: ZoneInfo):
    now_utc = datetime.now(ZoneInfo("UTC"))

    remind_codes = [c for c in (remind or "").split(",") if c in REMIND_DELTAS]
    for code in remind_codes:
        remind_time = due_at_utc - REMIND_DELTAS[code]
        if remind_time > now_utc:
            scheduler.add_job(
                send_reminder, "date", run_date=remind_time,
                args=[user_id, task_id], id=f"remind_{task_id}_{code}", replace_existing=True
            )

    local_due = to_local(due_at_utc, tz)
    if repeat == "none":
        if due_at_utc > now_utc:
            scheduler.add_job(
                send_due, "date", run_date=due_at_utc,
                args=[user_id, task_id], id=f"due_{task_id}", replace_existing=True
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
            args=[user_id, task_id], id=f"due_{task_id}", replace_existing=True
        )


async def send_reminder(user_id: int, task_id: int):
    task = await db.get_task(task_id)
    if not task or task["done"]:
        return
    lang = await user_lang(user_id)
    try:
        await bot.send_message(user_id, t(lang, "reminder_text", title=task["title"]))
    except TelegramForbiddenError:
        logger.warning("Юзер %s заблокировал бота, пропускаю напоминание по задаче %s", user_id, task_id)
    except Exception:
        logger.exception("Не удалось отправить напоминание user_id=%s task_id=%s", user_id, task_id)


async def send_due(user_id: int, task_id: int):
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

    lang = await user_lang(user_id)
    try:
        await bot.send_message(
            user_id, t(lang, "due_text", title=task["title"]),
            reply_markup=task_kb(task_id, lang)
        )
        await db.set_last_notified(task_id, now)
    except TelegramForbiddenError:
        logger.warning("Юзер %s заблокировал бота, пропускаю уведомление по задаче %s", user_id, task_id)
    except Exception:
        logger.exception("Не удалось отправить уведомление user_id=%s task_id=%s", user_id, task_id)


async def restore_jobs():
    """Восстанавливает джобы при старте процесса (после редеплоя/рестарта).
    Если дедлайн уже прошёл сегодня, а бот был офлайн — досылает уведомление сразу
    (если не было отправлено недавно — см. защиту в send_due)."""
    now_utc = datetime.now(ZoneInfo("UTC"))

    tasks = await db.get_all_active_tasks()
    for task in tasks:
        try:
            tz = await user_tz(task["user_id"])
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
            await send_due(task["user_id"], task["id"])
        else:
            schedule_task(task["id"], task["user_id"], task["due_at"], task["repeat"], task["remind"], tz)


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
