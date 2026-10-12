"""Базовые команды: /start, /help, /cancel, /timezone, /lang, /morning, /quiet."""

from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from date_parser import parse_quiet_hours
from i18n import t

from ..keyboards import lang_kb, quiet_kb, timezone_kb
from ..owner import _parse_hh_mm, invalidate_settings, owner_lang, owner_quiet, owner_tz, resolve_owner
from ..scheduling import (
    schedule_daily_summary,
    schedule_morning_summary,
    schedule_task,
    unschedule_morning_summary,
)
from ..states import SetQuiet, SetTimezone

router = Router()


# ---------- базовые команды ----------

@router.message(CommandStart())
async def start(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    # В группе сразу объясняем, что задачи общие — иначе непонятно, почему
    # задачу, которую добавил один, видят все. Короткий welcome (3 команды)
    # вместо полного списка — развёрнутая версия теперь в /help.
    key = "welcome_group" if owner_type == "chat" else "welcome"
    await message.answer(t(lang, key))

@router.message(Command("help"))
async def help_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "help_full"))

@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.clear()
    await message.answer(t(lang, "cancelled"))

# ---------- /timezone ----------

@router.message(Command("timezone"))
async def timezone_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(SetTimezone.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(t(lang, "choose_timezone"), reply_markup=timezone_kb())

@router.callback_query(F.data.startswith("tz_"))
async def timezone_pick(call: CallbackQuery, state: FSMContext):
    tz_name = call.data.split("_", 1)[1]
    data = await state.get_data()
    owner_id = data.get("owner_id", call.from_user.id)
    owner_type = data.get("owner_type", "user")
    await _apply_timezone(owner_id, owner_type, tz_name, call.message)
    await state.clear()
    await call.answer()

@router.message(SetTimezone.value)
async def timezone_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id = data.get("owner_id", message.from_user.id)
    owner_type = data.get("owner_type", "user")
    text = (message.text or "").strip()
    if text.startswith("/"):
        # любая другая команда выводит из режима ввода (иначе "/cancel" считался бы кривым поясом)
        lang = await owner_lang(owner_id, owner_type)
        await state.clear()
        await message.answer(t(lang, "cancelled"))
        return
    await _apply_timezone(owner_id, owner_type, text, message)
    await state.clear()

async def _apply_timezone(owner_id: int, owner_type: str, tz_name: str, target_message: Message):
    lang = await owner_lang(owner_id, owner_type)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await target_message.answer(t(lang, "timezone_invalid"))
        return
    await db.set_owner_timezone(owner_id, owner_type, tz_name)
    invalidate_settings(owner_id, owner_type)
    new_tz = ZoneInfo(tz_name)
    # Джоба вечерней сводки теперь одна на user_id (объединяет личку + все
    # группы, см. send_daily_summary) и живёт в поясе ЛИЧНЫХ настроек
    # пользователя — смена пояса ГРУППЫ (owner_type == "chat") на неё не влияет,
    # только /timezone в личном чате с ботом переставляет её на новый пояс.
    if owner_type == "user":
        schedule_daily_summary(owner_id, new_tz)
        morning = await db.get_morning_summary_settings(owner_id)
        if morning and morning["morning_summary_enabled"]:
            schedule_morning_summary(owner_id, new_tz, morning["morning_summary_hour"], morning["morning_summary_minute"])
    # cron-джобы повторяющихся задач привязаны к поясу на момент создания — без
    # перепланирования после /timezone они продолжали бы срабатывать по старому времени.
    for task in await db.get_tasks(owner_id, owner_type):
        schedule_task(task["id"], owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], new_tz,
                      remind_until_done=bool(task.get("remind_until_done")))
    await target_message.answer(t(lang, "timezone_set", tz=tz_name))

# ---------- /lang ----------

@router.message(Command("lang"))
async def lang_start(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await message.answer(t(lang, "choose_lang"), reply_markup=lang_kb(owner_id, owner_type))

@router.callback_query(F.data.startswith("lang_"))
async def lang_pick(call: CallbackQuery):
    _, owner_type, owner_id_str, new_lang = call.data.split("_")
    owner_id = int(owner_id_str)
    await db.set_owner_language(owner_id, owner_type, new_lang)
    invalidate_settings(owner_id, owner_type)
    await call.message.edit_text(t(new_lang, "lang_set"))
    await call.answer()

# ---------- /quiet — тихий час ----------

def _fmt_quiet(q: tuple[dtime, dtime]) -> tuple[str, str]:
    return q[0].strftime("%H:%M"), q[1].strftime("%H:%M")

async def _apply_quiet(owner_id: int, owner_type: str, quiet: tuple[dtime, dtime] | None, target_message: Message):
    lang = await owner_lang(owner_id, owner_type)
    if quiet is None:
        await db.set_owner_quiet_hours(owner_id, owner_type, None, None)
        invalidate_settings(owner_id, owner_type)
        await target_message.answer(t(lang, "quiet_off_done"))
        return
    await db.set_owner_quiet_hours(owner_id, owner_type, quiet[0], quiet[1])
    invalidate_settings(owner_id, owner_type)
    start, end = _fmt_quiet(quiet)
    await target_message.answer(t(lang, "quiet_set", start=start, end=end))

@router.message(Command("morning"))
async def cmd_morning(message: Message):
    """Утренняя сводка — ВСЕГДА личная настройка человека (user_id), даже если
    команда вызвана в групповом чате: в отличие от большинства команд бота
    здесь нет resolve_owner(chat, ...) — сводка одна на пользователя и не
    привязана к тому, откуда её включили (см. send_morning_summary в
    bot.py, которая и так объединяет личку + все группы в одно сообщение)."""
    user_id = message.from_user.id
    lang = await owner_lang(user_id, "user")
    arg = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) > 1 else ""

    if not arg:
        settings = await db.get_morning_summary_settings(user_id)
        if settings and settings["morning_summary_enabled"]:
            time_str = f"{settings['morning_summary_hour']:02d}:{settings['morning_summary_minute']:02d}"
            await message.answer(t(lang, "morning_status_on", time=time_str))
        else:
            await message.answer(t(lang, "morning_status_off"))
        return

    if arg.lower() in {"off", "выкл", "выключить", "wyłącz", "wylacz"}:
        await db.set_morning_summary(user_id, enabled=False)
        unschedule_morning_summary(user_id)
        await message.answer(t(lang, "morning_off"))
        return

    parsed = _parse_hh_mm(arg)
    if parsed is None:
        await message.answer(t(lang, "morning_time_invalid"))
        return
    hour, minute = parsed
    await db.set_morning_summary(user_id, enabled=True, hour=hour, minute=minute)
    tz = await owner_tz(user_id, "user")
    schedule_morning_summary(user_id, tz, hour, minute)
    await message.answer(t(lang, "morning_on", time=f"{hour:02d}:{minute:02d}"))

@router.message(Command("quiet"))
async def quiet_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    q = await owner_quiet(owner_id, owner_type)
    if q:
        start, end = _fmt_quiet(q)
        head = t(lang, "quiet_current", start=start, end=end)
    else:
        head = t(lang, "quiet_none")
    await state.set_state(SetQuiet.value)
    await state.update_data(owner_id=owner_id, owner_type=owner_type)
    await message.answer(f"{head}\n\n{t(lang, 'quiet_prompt')}", reply_markup=quiet_kb(lang))

@router.callback_query(F.data.startswith("quiet_"))
async def quiet_pick(call: CallbackQuery, state: FSMContext):
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    arg = call.data[len("quiet_"):]
    if arg == "off":
        await _apply_quiet(owner_id, owner_type, None, call.message)
    else:
        quiet = parse_quiet_hours(arg.removeprefix("set_"))
        if quiet is None:
            await call.answer()
            return
        await _apply_quiet(owner_id, owner_type, quiet, call.message)
    await state.clear()
    await call.answer()

@router.message(SetQuiet.value)
async def quiet_custom(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    text = (message.text or "").strip()
    if text.startswith("/"):
        # любая другая команда выводит из режима ввода (иначе "/add" считался бы кривым временем)
        lang = await owner_lang(owner_id, owner_type)
        await state.clear()
        await message.answer(t(lang, "cancelled"))
        return
    if text.lower() in {"off", "выкл", "выключить", "wyłącz", "wylacz"}:
        await _apply_quiet(owner_id, owner_type, None, message)
        await state.clear()
        return
    quiet = parse_quiet_hours(text)
    if quiet is None:
        lang = await owner_lang(owner_id, owner_type)
        await message.answer(t(lang, "quiet_invalid"))
        return  # остаёмся в состоянии — можно повторить ввод
    await _apply_quiet(owner_id, owner_type, quiet, message)
    await state.clear()
