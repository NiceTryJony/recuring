"""Фото к задаче."""

import io

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import dashboard
import db
from i18n import t

from ..app import bot
from ..formatting import fmt_task
from ..keyboards import task_kb
from ..owner import _authorize_task, owner_lang
from ..states import AddTaskPhoto

router = Router()


# ---------- фото к задаче ----------
# Тот же паттерн FSM, что и у подзадач (AddSubtasks): кнопка стартует
# состояние с task_id в data, дальше просто шлём боту фото одно за другим,
# "/done" выходит. Авторизация — один раз на старте, см. комментарий в
# subtask_add_collect про изолированность FSM state по (chat_id, user_id).

@router.callback_query(F.data.startswith("photo_"))
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

@router.message(AddTaskPhoto.collecting, F.photo)
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

@router.message(AddTaskPhoto.collecting, Command("done"))
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

@router.message(AddTaskPhoto.collecting)
async def task_photo_collect_wrong_type(message: Message, state: FSMContext):
    # Любой не-фото апдейт (текст, стикер, документ без фото-превью) внутри
    # этого состояния — подсказываем, не выходя из режима сбора, чтобы не
    # пришлось заново жать кнопку "📷 Фото" после случайного смайлика.
    data = await state.get_data()
    task = await db.get_task(data.get("task_id")) if data.get("task_id") else None
    lang = await owner_lang(task["owner_id"], task["owner_type"]) if task else "ru"
    await message.answer(t(lang, "photo_wrong_type"))
