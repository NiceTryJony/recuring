"""Шаблоны задач: /templates и создание своего шаблона."""

from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from i18n import t

from ..keyboards import templates_kb
from ..owner import _parse_hh_mm, owner_lang, owner_tz, resolve_owner
from ..scheduling import schedule_task
from ..states import SaveTemplate
from ..templates import _resolve_template_due_at, builtin_templates_for_lang

router = Router()


async def apply_template(
    tpl: dict, owner_id: int, owner_type: str, tz: ZoneInfo, actor_id: int | None = None,
) -> int:
    """Единая точка применения ЛЮБОГО шаблона (built-in или пользовательского —
    оба приводятся к одной форме словаря с title/time_of_day/day_offset/
    repeat/remind/tag до вызова) — создаёт реальную задачу и планирует её
    так же, как обычное /add. Возвращает task_id."""
    due_at = _resolve_template_due_at(tpl["time_of_day"], tpl["day_offset"], tz)
    task_id = await db.add_task(
        owner_id=owner_id, owner_type=owner_type, title=tpl["title"], due_at=due_at,
        repeat=tpl["repeat"], remind=tpl["remind"], tag=tpl.get("tag"),
        remind_until_done=tpl.get("remind_until_done", False), created_by=actor_id,
    )
    await db.log_history(task_id, actor_id, tpl["title"], "created")
    schedule_task(task_id, owner_id, owner_type, due_at, tpl["repeat"], tpl["remind"], tz,
                  remind_until_done=tpl.get("remind_until_done", False))
    return task_id

@router.message(Command("templates"))
async def templates_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    custom = await db.get_templates(owner_id, owner_type)
    text = f"{t(lang, 'templates_title')}\n\n{t(lang, 'templates_hint')}"
    await message.answer(text, reply_markup=templates_kb(lang, custom))

@router.callback_query(F.data.startswith("tplapply_builtin_"))
async def template_apply_builtin(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    key = call.data[len("tplapply_builtin_"):]
    tpl = next((t_ for t_ in builtin_templates_for_lang(lang) if t_["key"] == key), None)
    if tpl is None:
        await call.answer()
        return
    await apply_template(tpl, owner_id, owner_type, tz, actor_id=call.from_user.id)
    local_str = _resolve_template_due_at(tpl["time_of_day"], tpl["day_offset"], tz).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    await call.message.answer(t(lang, "template_applied", title=tpl["title"], date=local_str))
    await call.answer()

@router.callback_query(F.data.startswith("tplapply_custom_"))
async def template_apply_custom(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    template_id = int(call.data[len("tplapply_custom_"):])
    tpl_row = await db.get_template(template_id)
    # Проверка владения — тот же принцип, что и у задач: шаблон должен
    # принадлежать именно этому owner (личка или эта группа), иначе кто-то
    # угадавший/подобравший id мог бы применить чужой приватный шаблон.
    if tpl_row is None or tpl_row["owner_id"] != owner_id or tpl_row["owner_type"] != owner_type:
        await call.answer()
        return
    await apply_template(tpl_row, owner_id, owner_type, tz, actor_id=call.from_user.id)
    local_str = _resolve_template_due_at(tpl_row["time_of_day"], tpl_row["day_offset"], tz).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    await call.message.answer(t(lang, "template_applied", title=tpl_row["title"], date=local_str))
    await call.answer()

@router.callback_query(F.data.startswith("tpldel_"))
async def template_delete(call: CallbackQuery):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    template_id = int(call.data[len("tpldel_"):])
    await db.delete_template(template_id, owner_id, owner_type)
    custom = await db.get_templates(owner_id, owner_type)
    text = f"{t(lang, 'templates_title')}\n\n{t(lang, 'templates_hint')}"
    try:
        await call.message.edit_text(text, reply_markup=templates_kb(lang, custom))
    except TelegramBadRequest:
        pass
    await call.answer(t(lang, "template_deleted"))

@router.callback_query(F.data == "tplnew")
async def template_new_start(call: CallbackQuery, state: FSMContext):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    count = await db.count_templates(owner_id, owner_type)
    if count >= db.MAX_TEMPLATES_PER_OWNER:
        await call.answer(t(lang, "template_limit_reached", n=db.MAX_TEMPLATES_PER_OWNER), show_alert=True)
        return
    await state.set_state(SaveTemplate.title)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await call.message.answer(t(lang, "template_save_prompt"))
    await call.answer()

@router.message(SaveTemplate.title)
async def template_new_title(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    title = (message.text or "").strip()
    if not title or title.startswith("/"):
        await message.answer(t(lang, "cancelled"))
        await state.clear()
        return
    await state.update_data(title=title[:200])
    await state.set_state(SaveTemplate.time_of_day)
    await message.answer(t(lang, "template_save_time_prompt"))

@router.message(SaveTemplate.time_of_day)
async def template_new_time(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer(t(lang, "cancelled"))
        await state.clear()
        return
    parsed = _parse_hh_mm(text)
    if parsed is None:
        await message.answer(t(lang, "morning_time_invalid"))  # тот же формат ЧЧ:ММ, тот же текст подсказки
        return
    hour, minute = parsed
    await state.update_data(time_of_day=f"{hour:02d}:{minute:02d}")
    await state.set_state(SaveTemplate.day_offset)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_offset_today"), callback_data="tploffset_0"),
        InlineKeyboardButton(text=t(lang, "btn_offset_tomorrow"), callback_data="tploffset_1"),
    ]])
    await message.answer(t(lang, "template_save_offset_prompt"), reply_markup=kb)

@router.callback_query(SaveTemplate.day_offset, F.data.startswith("tploffset_"))
async def template_new_offset(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    lang = await owner_lang(owner_id, owner_type)
    day_offset = int(call.data[len("tploffset_"):])
    await db.add_template(
        owner_id=owner_id, owner_type=owner_type, title=data["title"],
        time_of_day=data["time_of_day"], day_offset=day_offset, created_by=call.from_user.id,
    )
    await state.clear()
    await call.message.edit_text(t(lang, "template_saved", title=data["title"]))
    await call.answer()
