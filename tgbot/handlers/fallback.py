"""Сообщение вне состояния и без команды."""

from aiogram import Router
from aiogram.types import Message

from i18n import t

from ..owner import owner_lang, resolve_owner

router = Router()


# ---------- fallback — сообщение вне состояния ----------

@router.message()
async def fallback(message: Message):
    # Раньше тут повторялся welcome — выглядело так, будто бот не узнаёт уже
    # знакомого пользователя. fallback срабатывает только на случайный текст
    # ВНЕ состояния FSM (т.е. не на середине /add и т.п.), значит это почти
    # всегда опечатка в команде или просто не-команда — явная подсказка
    # пойти в /help полезнее, чем ещё раз показывать приветствие.
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "unrecognized"))
