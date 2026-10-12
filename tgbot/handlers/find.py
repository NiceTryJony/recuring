"""Текстовый поиск /find."""

from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from i18n import t

from ..constants import PAGE_SIZE
from ..formatting import fmt_task_line
from ..keyboards import list_open_kb
from ..owner import _authorize_task, owner_lang, owner_tz, resolve_owner
from ..states import FindTask
from .listing import _close_stale_open_cards, _open_task_card, _search_cache

router = Router()


# ---------- /find — текстовый поиск ----------

@router.message(Command("find"))
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

@router.message(FindTask.query)
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

@router.callback_query(F.data.startswith("findpage_"))
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

@router.callback_query(F.data.startswith("findopen_"))
async def find_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под результатами /find. Свой кэш (_find_view_cache),
    отдельный от /list — см. _open_task_card."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_find_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)
