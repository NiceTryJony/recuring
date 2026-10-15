"""Список задач (/list): фильтры, сортировка, страницы, карточка задачи."""

import asyncio
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import db
from i18n import t

from ..app import bot
from ..constants import PAGE_SIZE
from ..formatting import fmt_task, fmt_task_line
from ..keyboards import list_filter_kb, list_nav_kb, task_kb
from ..owner import _authorize_task, owner_lang, owner_tz, resolve_owner

router = Router()


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
        kb = task_kb(task["id"], lang, done=task["done"], assign=task["owner_type"] == "chat")
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
    kb = task_kb(task_id, lang, done=task["done"], assign=task["owner_type"] == "chat")
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

@router.callback_query(F.data.startswith("listopen_"))
async def list_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под компактным списком /list."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_list_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)

@router.message(Command("list"))
async def list_tasks(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["page"] = 0  # свежий /list всегда показывает список с начала
        await _render_list_page(message.chat.id, owner_id, owner_type, lang, view)

@router.callback_query(F.data.startswith("listf_"))
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

@router.callback_query(F.data.startswith("lists_"))
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

@router.callback_query(F.data.startswith("listpage_"))
async def list_page_nav(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    view = _get_list_view(owner_id, owner_type)
    async with _get_list_view_lock(owner_id, owner_type):
        view["page"] = int(call.data.split("_", 1)[1])
        await _render_list_page(call.message.chat.id, owner_id, owner_type, lang, view)
    await call.answer()

@router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    # Кнопка-индикатор "2/5" в пагинации — просто гасим "часики" на тапе.
    await call.answer()
