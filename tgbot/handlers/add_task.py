"""Добавление задачи (/add, FSM) и шаблон из задачи."""

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from date_parser import parse_human_date, split_repeat, split_title_and_date
from i18n import t

from .. import app
from ..constants import NAG_INTERVAL_MINUTES, NAG_MAX_HOURS
from ..keyboards import nag_kb, remind_kb, repeat_kb
from ..owner import normalize_tag, owner_lang, owner_tz, resolve_owner, to_local, to_utc
from ..photo_store import attach_photo
from ..scheduling import schedule_daily_summary, schedule_task
from ..states import AddTask

router = Router()


# ---------- добавление задачи ----------

@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    await state.set_state(AddTask.title)
    # set_data, а не update_data: фото/описание от брошенного быстрого добавления не должны перейти в новую задачу
    await state.set_data({"owner_id": owner_id, "owner_type": owner_type, "actor_id": message.from_user.id})
    await message.answer(t(lang, "ask_title"))

async def _accept_date_and_ask_repeat(local_dt: datetime, state: FSMContext, tz: ZoneInfo, lang: str, target_message: Message):
    """Общий хвост для двух путей: обычного пошагового (после AddTask.date)
    и быстрого однострочного (когда дата уже найдена прямо в названии).
    Повтор, найденный в тексте («codziennie», «каждый понедельник»), уже
    лежит в repeat — тогда вопрос о повторе пропускаем."""
    await state.update_data(due_at=to_utc(local_dt, tz).isoformat())
    if (await state.get_data()).get("repeat"):
        await state.update_data(remind_selected=[])
        await state.set_state(AddTask.remind)
        await target_message.answer(t(lang, "ask_remind"), reply_markup=remind_kb(lang))
        return
    await state.set_state(AddTask.repeat)
    await target_message.answer(t(lang, "ask_repeat"), reply_markup=repeat_kb(lang))

@router.message(AddTask.title, F.text)  # стикер/фото без текста — к fallback, а не AttributeError
async def add_title(message: Message, state: FSMContext):
    await take_title(message.text, message, state)

async def take_title(text: str, message: Message, state: FSMContext):
    """Шаг «название»: и для /add, и для быстрого добавления (текст, пересланное
    сообщение, подпись к фото — там text не обязательно message.text)."""
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    tz = await owner_tz(data["owner_id"], data["owner_type"])
    now_local = datetime.now(tz).replace(tzinfo=None)

    # Быстрый путь: если в названии нашлась понятная дата ("Купить молоко
    # завтра в 18:00"), сразу принимаем её и пропускаем отдельный шаг
    # ask_date — для тех, кто не хочет проходить все шаги по отдельности.
    # Если дата не найдена — ведём себя как раньше: весь текст - заголовок,
    # дату спрашиваем следующим шагом. Повтор из текста — в repeat.
    raw, repeat = split_repeat(text.strip())
    title, local_dt = split_title_and_date(raw, now_local)
    await state.update_data(title=title[:200], repeat=repeat)

    if local_dt is not None:
        await _accept_date_and_ask_repeat(local_dt, state, tz, lang, message)
        return

    await state.set_state(AddTask.date)
    await message.answer(t(lang, "ask_date"))

@router.message(AddTask.date, F.text)
async def add_date(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    tz = await owner_tz(data["owner_id"], data["owner_type"])
    now_local = datetime.now(tz).replace(tzinfo=None)

    text, repeat = split_repeat(message.text.strip())   # «каждый день в 9» на шаге даты
    local_dt = parse_human_date(text, now_local)
    if local_dt is None:
        await message.answer(t(lang, "bad_date"))
        return
    if repeat:
        await state.update_data(repeat=repeat)

    await _accept_date_and_ask_repeat(local_dt, state, tz, lang, message)

@router.callback_query(AddTask.repeat, F.data.startswith("rep_"))
async def add_repeat(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    repeat = call.data.split("_", 1)[1]
    await state.update_data(repeat=repeat, remind_selected=[])
    await state.set_state(AddTask.remind)
    await call.message.edit_text(t(lang, "ask_remind"), reply_markup=remind_kb(lang))
    await call.answer()

@router.callback_query(AddTask.remind, F.data.startswith("rem_"))
async def add_remind_toggle(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    code = call.data.split("_", 1)[1]

    if code == "done":
        selected = data.get("remind_selected", [])
        remind_value = ",".join(selected) if selected else "0"
        await state.update_data(remind=remind_value)
        # «Повторять, пока не выполню» — только для разовых задач: у повторяющихся
        # (daily/weekly/...) каждое срабатывание и так приходит по расписанию.
        if data.get("repeat") == "none":
            await state.set_state(AddTask.nag)
            await call.message.edit_text(
                t(lang, "ask_nag", interval=NAG_INTERVAL_MINUTES, hours=NAG_MAX_HOURS), reply_markup=nag_kb(lang)
            )
        else:
            await state.update_data(remind_until_done=False)
            await state.set_state(AddTask.tag)
            await call.message.edit_text(t(lang, "ask_tag"))
        await call.answer()
        return

    selected = set(data.get("remind_selected", []))
    if code == "0":
        selected = set()  # "без напоминания" сбрасывает остальной выбор
    else:
        selected.discard("0")
        if code in selected:
            selected.discard(code)
        else:
            selected.add(code)
    await state.update_data(remind_selected=list(selected))
    await call.message.edit_reply_markup(reply_markup=remind_kb(lang, selected=selected))
    await call.answer()

@router.callback_query(AddTask.nag, F.data.startswith("nag_"))
async def add_nag_pick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = await owner_lang(data["owner_id"], data["owner_type"])
    await state.update_data(remind_until_done=call.data == "nag_1")
    await state.set_state(AddTask.tag)
    await call.message.edit_text(t(lang, "ask_tag"))
    await call.answer()

@router.message(AddTask.tag, F.text)
async def add_tag(message: Message, state: FSMContext):
    tag = normalize_tag(message.text)
    await _finalize_add_task(state, tag, message)

@router.message(Command("skip"), AddTask.tag)
async def add_tag_skip(message: Message, state: FSMContext):
    await _finalize_add_task(state, None, message)

async def _finalize_add_task(state: FSMContext, tag: str | None, target_message: Message):
    data = await state.get_data()
    owner_id, owner_type = data["owner_id"], data["owner_type"]
    actor_id = data.get("actor_id", target_message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tz = await owner_tz(owner_id, owner_type)
    due_at = datetime.fromisoformat(data["due_at"])
    nag = bool(data.get("remind_until_done", False))

    task_id = await db.add_task(
        owner_id=owner_id,
        owner_type=owner_type,
        title=data["title"],
        due_at=due_at,
        repeat=data["repeat"],
        remind=data["remind"],
        tag=tag,
        remind_until_done=nag,
        created_by=actor_id,
        description=data.get("description"),
    )
    await db.log_history(task_id, actor_id, data["title"], "created")
    # Задача из фото — само фото прикрепляем сразу после создания
    if data.get("photo_file_id"):
        await attach_photo(task_id, data["photo_file_id"], actor_id, data["title"])
    schedule_task(task_id, owner_id, owner_type, due_at, data["repeat"], data["remind"], tz, remind_until_done=nag)
    # Джоба вечерней сводки — одна на РЕАЛЬНОГО ЧЕЛОВЕКА (actor_id), который
    # создал задачу, а не на owner: создание задачи в группе не должно
    # заводить отдельную джобу "для группы" — объединённая сводка и так
    # подхватит эту группу через db.get_chats_for_user на следующем прогоне
    # (bot.py:send_daily_summary), достаточно гарантировать, что у автора
    # вообще есть джоба на его личный пояс.
    if f"dailysummary_{actor_id}" not in {j.id for j in app.scheduler.get_jobs()}:
        actor_tz = await owner_tz(actor_id, "user")
        schedule_daily_summary(actor_id, actor_tz)

    local_str = to_local(due_at, tz).strftime("%d.%m.%Y %H:%M")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_save_as_template"), callback_data=f"tplfromtask_{task_id}"),
    ]])
    await target_message.answer(t(lang, "task_added", title=data["title"], date=local_str), reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.startswith("tplfromtask_"))
async def template_from_task(call: CallbackQuery):
    """Быстрое сохранение только что созданной задачи как шаблона — без
    повторного ввода title/time/offset вручную через /templates. time_of_day
    и day_offset считаем от due_at задачи ОТНОСИТЕЛЬНО МОМЕНТА НАЖАТИЯ кнопки
    (а не момента создания задачи) — так шаблон честно описывает "через
    сколько дней от сегодня", если человек нажал кнопку не сразу."""
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    task_id = int(call.data[len("tplfromtask_"):])
    task = await db.get_task(task_id)
    # Та же проверка владения, что и у tplapply_custom/tpldel — угаданный
    # чужой task_id не должен давать сохранить его как свой шаблон.
    if task is None or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        await call.answer()
        return
    count = await db.count_templates(owner_id, owner_type)
    if count >= db.MAX_TEMPLATES_PER_OWNER:
        await call.answer(t(lang, "template_limit_reached", n=db.MAX_TEMPLATES_PER_OWNER), show_alert=True)
        return
    tz = await owner_tz(owner_id, owner_type)
    due_local = to_local(task["due_at"], tz)
    day_offset = max(0, (due_local.date() - datetime.now(tz).date()).days)
    time_of_day = due_local.strftime("%H:%M")
    await db.add_template(
        owner_id=owner_id, owner_type=owner_type, title=task["title"],
        time_of_day=time_of_day, day_offset=day_offset,
        repeat=task["repeat"], remind=task["remind"], tag=task["tag"],
        remind_until_done=task["remind_until_done"], created_by=call.from_user.id,
    )
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.answer(t(lang, "template_saved_from_task", title=task["title"]))
    await call.answer()
