"""/dashboard — ссылка и пароль веб-дашборда."""

import asyncio

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import dashboard
import db
from i18n import t

from ..constants import DASHBOARD_BASE_URL
from ..owner import invalidate_settings, owner_lang, resolve_owner
from ..states import SetDashboardPassword

router = Router()


# ---------- /dashboard ----------

@router.message(Command("dashboard"))
async def dashboard_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(SetDashboardPassword.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "dashboard_ask_password"))

@router.message(SetDashboardPassword.value)
async def dashboard_set_password(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    lang = await owner_lang(owner_id, owner_type)
    password = message.text.strip()
    if len(password) < 4:
        await message.answer(t(lang, "dashboard_password_short"))
        return

    # PBKDF2 с солью (см. dashboard.hash_password); в потоке, чтобы не блокировать event loop
    password_hash = await asyncio.to_thread(dashboard.hash_password, password)
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
