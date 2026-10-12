"""Теги: /tags и фильтр по тегу."""

import html
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from i18n import t

from ..constants import PAGE_SIZE
from ..formatting import fmt_task_line
from ..keyboards import list_open_kb, tags_kb
from ..owner import _authorize_task, owner_lang, owner_tz, resolve_owner
from .listing import _close_stale_open_cards, _open_task_card

router = Router()


# ---------- /tags ----------

@router.message(Command("tags"))
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

@router.callback_query(F.data.startswith("tagf_"))
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

@router.callback_query(F.data.startswith("tagfpage_"))
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

@router.callback_query(F.data.startswith("tagfopen_"))
async def tagf_task_open(call: CallbackQuery):
    """Тап по кнопке-номеру под результатами /tags. Свой кэш
    (_tagf_view_cache) — см. _open_task_card."""
    task_id = int(call.data.split("_", 1)[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    view = _get_tagf_view(task["owner_id"], task["owner_type"])
    await _open_task_card(call, task, view)
