"""Подзадачи."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from i18n import t

from ..formatting import fmt_task
from ..keyboards import subtasks_kb, task_kb
from ..owner import _authorize_subtask, _authorize_task, owner_lang
from ..states import AddSubtasks

router = Router()


# ---------- подзадачи ----------
# Везде ниже авторизация идёт через родительскую задачу (_authorize_task /
# _authorize_subtask) — подзадача сама по себе владельца не хранит.

@router.callback_query(F.data.startswith("subs_"))
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

@router.callback_query(F.data.startswith("subtoggle_"))
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

@router.callback_query(F.data.startswith("subdel_"))
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

@router.callback_query(F.data.startswith("subdelok_"))
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

@router.callback_query(F.data.startswith("subdelno_"))
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

@router.callback_query(F.data.startswith("subadd_"))
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

@router.message(AddSubtasks.collecting, F.text)
async def subtask_add_collect(message: Message, state: FSMContext):
    # task_id пришёл из state, установленного в subtask_add_start, который уже
    # проверил права для (chat, user) этого FSM-диалога — повторная проверка
    # тут не нужна по той же причине, что и в edit_value_apply: aiogram FSM
    # состояние изолировано по (chat_id, user_id), подменить его нельзя.
    data = await state.get_data()
    task_id = data["task_id"]
    task = await db.get_task(task_id)
    if task is None:
        # задачу удалили посреди ввода — add_subtask упал бы на внешнем ключе
        await state.clear()
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    if message.text.strip() == "/done":
        await state.clear()
        subtasks = await db.get_subtasks(task_id)
        text = t(lang, "no_subtasks") if not subtasks else t(lang, "subtasks_title")
        await message.answer(text, reply_markup=subtasks_kb(task_id, subtasks, lang))
        return

    await db.add_subtask(task_id, message.text.strip())
    await message.answer(t(lang, "subtask_added") + f"\n{t(lang, 'subtask_prompt')}")

@router.callback_query(F.data.startswith("subback_"))
async def subtasks_back(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"], assign=task["owner_type"] == "chat"), parse_mode="HTML")
    await call.answer()
