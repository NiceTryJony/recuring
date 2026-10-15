"""Сообщение вне состояния и без команды."""

from datetime import datetime

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from date_parser import split_repeat, split_title_and_date
from i18n import t

from ..owner import owner_lang, owner_tz, resolve_owner
from ..states import AddTask
from .add_task import take_title

router = Router()


# ---------- fallback — сообщение вне состояния ----------

@router.message()
async def fallback(message: Message, state: FSMContext):
    # Раньше тут повторялся welcome — выглядело так, будто бот не узнаёт уже
    # знакомого пользователя. fallback срабатывает только на случайный текст
    # ВНЕ состояния FSM (т.е. не на середине /add и т.п.), значит это почти
    # всегда опечатка в команде или просто не-команда — явная подсказка
    # пойти в /help полезнее, чем ещё раз показывать приветствие.
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)

    # Быстрое добавление — только в личке и только вне другого диалога (сюда же
    # падает текст посреди /add на шаге с кнопками).
    if message.chat.type == "private" and await state.get_state() is None:
        start = {"owner_id": owner_id, "owner_type": owner_type, "actor_id": message.from_user.id}
        text = (message.text or message.caption or "").strip()

        # Фото: подпись — название, само фото прикрепится к задаче; без подписи — спросим название
        if message.photo:
            await state.set_state(AddTask.title)
            await state.set_data({**start, "photo_file_id": message.photo[-1].file_id})
            if text:
                await take_title(text, message, state)
            else:
                await message.answer(t(await owner_lang(owner_id, owner_type), "ask_title"))
            return

        # Пересланное сообщение — всегда задача: первая строка — название, весь текст — описание
        if message.forward_origin is not None and text:
            first_line = text.splitlines()[0]
            await state.set_state(AddTask.title)
            await state.set_data({**start, "description": text[:4000] if text != first_line else None})
            await take_title(first_line, message, state)
            return

        # Обычный текст: «купить молоко завтра 18:00». Только если парсер нашёл
        # дату — иначе «привет» не превращается в задачу. Вопрос («Co robisz dziś?») — тоже не задача.
        if text and message.text and not text.startswith("/") and not text.endswith("?"):
            tz = await owner_tz(owner_id, owner_type)
            title, local_dt = split_title_and_date(split_repeat(text)[0], datetime.now(tz).replace(tzinfo=None))
            if local_dt is not None and title:
                await state.set_state(AddTask.title)
                await state.set_data(start)
                await take_title(text, message, state)  # дальше — обычный путь /add: повтор, напоминания, тег
                return

    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "unrecognized"))
