"""Действия с задачей: выполнить, удалить, отложить, изменить."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from date_parser import parse_human_date
from i18n import t

from .. import app
from ..formatting import display_name, fmt_task
from ..keyboards import assign_kb, confirm_delete_kb, edit_field_kb, repeat_kb, task_kb
from ..owner import _authorize_task, normalize_tag, owner_lang, owner_tz, to_local, to_utc
from ..scheduling import (
    _remove_task_jobs, move_to_today, overdue_before_today, schedule_task, send_due, snooze_target,
)
from ..states import EditTask
from .listing import _get_list_view

router = Router()


# ---------- действия с задачей ----------
# Начиная с этого блока владелец (язык/часовой пояс для UI и планировщика)
# берётся из самой задачи (task["owner_id"]/["owner_type"]), а НЕ из чата, где
# нажата кнопка — потому что уведомления по общим задачам чата рассылаются
# личными сообщениями участникам, и там chat.type будет "private".
# "Кто нажал" (для истории) — это всегда call.from_user.id.

@router.callback_query(F.data.startswith("done_"))
async def mark_done(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_done(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "done")
    _remove_task_jobs(task_id)
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await call.message.edit_text(
        text + f"\n\n{t(lang, 'task_done')}",
        reply_markup=task_kb(task_id, lang, done=True, assign=task["owner_type"] == "chat"),
        parse_mode="HTML",
    )
    await call.answer()

@router.callback_query(F.data.startswith("undone_"))
async def mark_undone(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.mark_undone(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "undone")

    # Задача снова активна — нужно заново запланировать уведомления,
    # которые были сняты при отметке "выполнено".
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], task["repeat"], task["remind"], tz,
                  remind_until_done=bool(task.get("remind_until_done")))

    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await call.message.edit_text(
        text + f"\n\n{t(lang, 'task_undone')}",
        reply_markup=task_kb(task_id, lang, done=False, assign=task["owner_type"] == "chat"),
        parse_mode="HTML",
    )
    await call.answer()

@router.callback_query(F.data.startswith("del_"))
async def delete_confirm(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await call.message.edit_reply_markup(reply_markup=confirm_delete_kb(task_id, lang))
    await call.answer()

@router.callback_query(F.data.startswith("delok_"))
async def delete_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await db.delete_task(task_id)
    await db.log_history(task_id, call.from_user.id, task["title"], "deleted")
    _remove_task_jobs(task_id)
    # Если карточка была открыта из /list по номеру — забываем о ней, иначе
    # следующий рендер списка попытается отредактировать уже нерелевантное
    # сообщение "задача удалена".
    view = _get_list_view(task["owner_id"], task["owner_type"])
    view["open_cards"].pop(task_id, None)
    await call.message.edit_text(t(lang, "task_deleted"))
    await call.answer()

@router.callback_query(F.data.startswith("delno_"))
async def delete_cancel(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    text = await fmt_task(task)
    await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=task["done"], assign=task["owner_type"] == "chat"), parse_mode="HTML")
    await call.answer()

async def _snooze(task: dict, new_due: datetime):
    """Разовую задачу переносим целиком (срок + джобы). Повторяющуюся не трогаем
    (раньше snooze затирал cron одноразовой джобой и сдвигал due_at навсегда) —
    только разовое напоминание к новому времени."""
    task_id = task["id"]
    if task["repeat"] == "none":
        await db.update_task(task_id, due_at=new_due)
        _remove_task_jobs(task_id)
        tz = await owner_tz(task["owner_id"], task["owner_type"])
        schedule_task(task_id, task["owner_id"], task["owner_type"], new_due, "none", "0", tz,
                      remind_until_done=bool(task.get("remind_until_done")))
    else:
        app.scheduler.add_job(
            send_due, "date", run_date=new_due,
            args=[task["owner_id"], task["owner_type"], task_id], id=f"snooze_{task_id}", replace_existing=True
        )

@router.callback_query(F.data.startswith("snooze_"))
async def snooze_task(call: CallbackQuery):
    """Старая кнопка «+1ч» — в уже отправленных сообщениях она осталась."""
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await _snooze(task, datetime.now(ZoneInfo("UTC")) + timedelta(hours=1))
    await call.answer(t(lang, "snooze_1h"))

@router.callback_query(F.data.startswith("snz_"))
async def snooze_pick(call: CallbackQuery):
    _, task_id_str, code = call.data.split("_")
    task = await _authorize_task(call, int(task_id_str))
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    new_due = snooze_target(code, datetime.now(ZoneInfo("UTC")), task["due_at"], tz)
    if new_due is None:
        await call.answer()
        return
    await _snooze(task, new_due)
    await call.answer(t(lang, "snoozed_to", when=to_local(new_due, tz).strftime("%d.%m %H:%M")))

@router.callback_query(F.data == "ovd_today")
async def overdue_to_today(call: CallbackQuery):
    """Кнопка из утренней сводки: все хвосты с прошлых дней — на сегодня (в то же
    время; уже прошедшее — через час). Только личные задачи нажавшего."""
    user_id = call.from_user.id
    lang = await owner_lang(user_id, "user")
    tz = await owner_tz(user_id, "user")
    now = datetime.now(ZoneInfo("UTC"))
    overdue = overdue_before_today(await db.get_tasks(user_id, "user"), tz, now)
    for task in overdue:
        new_due = move_to_today(task["due_at"], tz, now)
        await _snooze(task, new_due)
        await db.log_history(task["id"], user_id, task["title"], "rescheduled")
    await call.answer(t(lang, "overdue_moved", n=len(overdue)), show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)   # кнопка одноразовая
    except TelegramBadRequest:
        pass  # сообщение слишком старое для правки — перенос уже сделан, это не ошибка

@router.callback_query(F.data.startswith("edit_"))
async def edit_choose_field(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    await call.message.edit_reply_markup(reply_markup=edit_field_kb(task_id, lang))
    await call.answer()

@router.callback_query(F.data.startswith("ef_"))
async def edit_field_selected(call: CallbackQuery, state: FSMContext):
    _, field, task_id = call.data.split("_")
    task_id = int(task_id)
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])

    if field == "repeat":
        await call.message.edit_text(t(lang, "ask_repeat"), reply_markup=repeat_kb(lang, prefix=f"editrep_{task_id}"))
        await call.answer()
        return

    await state.set_state(EditTask.value)
    await state.update_data(task_id=task_id, field=field)
    prompts = {
        "title": t(lang, "new_title_prompt"),
        "date": t(lang, "new_date_prompt"),
        "tag": t(lang, "ask_tag"),
    }
    await call.message.edit_text(prompts[field])
    await call.answer()

@router.callback_query(F.data.startswith("editrep_"))
async def edit_repeat_apply(call: CallbackQuery):
    _, task_id, new_repeat = call.data.split("_", 2)  # в коде повтора бывают "_" (monthly_nth_weekday)
    task_id = int(task_id)
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    await db.update_task(task_id, repeat=new_repeat)
    task = await db.get_task(task_id)
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    tz = await owner_tz(task["owner_id"], task["owner_type"])
    _remove_task_jobs(task_id)
    schedule_task(task_id, task["owner_id"], task["owner_type"], task["due_at"], new_repeat, task["remind"], tz,
                  remind_until_done=bool(task.get("remind_until_done")))
    text = await fmt_task(task)
    await call.message.edit_text(text + f"\n\n{t(lang, 'repeat_updated')}", reply_markup=task_kb(task_id, lang, done=task["done"], assign=task["owner_type"] == "chat"), parse_mode="HTML")
    await call.answer()

@router.message(EditTask.value, F.text)
async def edit_value_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id, field = data["task_id"], data["field"]
    task = await db.get_task(task_id)
    if task is None:
        # задачу удалили, пока шла правка (в т.ч. ночная чистка выполненных)
        await state.clear()
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])

    if field == "title":
        await db.update_task(task_id, title=message.text.strip())
    elif field == "tag":
        tag = normalize_tag(message.text)
        await db.update_task(task_id, tag=tag)
    elif field == "date":
        tz = await owner_tz(task["owner_id"], task["owner_type"])
        now_local = datetime.now(tz).replace(tzinfo=None)
        local_dt = parse_human_date(message.text.strip(), now_local)
        if local_dt is None:
            await message.answer(t(lang, "bad_date"))
            return
        due_at = to_utc(local_dt, tz)
        await db.update_task(task_id, due_at=due_at)
        await db.log_history(task_id, message.from_user.id, task["title"], "rescheduled")
        _remove_task_jobs(task_id)
        schedule_task(task_id, task["owner_id"], task["owner_type"], due_at, task["repeat"], task["remind"], tz,
                      remind_until_done=bool(task.get("remind_until_done")))

    await state.clear()
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await message.answer(text + f"\n\n{t(lang, 'task_updated')}", reply_markup=task_kb(task_id, lang, done=updated["done"], assign=task["owner_type"] == "chat"), parse_mode="HTML")

# ---------- поручить задачу группы участнику ----------

@router.callback_query(F.data.startswith("assign_"))
async def assign_open(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    if task["owner_type"] != "chat":
        await call.answer()
        return
    member_ids = await db.get_chat_members(task["owner_id"])
    if not member_ids:
        await call.answer(t(lang, "assign_no_members"), show_alert=True)
        return
    users = await db.get_telegram_users(member_ids)
    # 30 — запас под лимит клавиатуры Telegram; в семейной/рабочей группе больше почти не бывает
    members = sorted(((uid, display_name(uid, users.get(uid))) for uid in member_ids), key=lambda m: m[1].casefold())[:30]
    await call.message.edit_reply_markup(reply_markup=assign_kb(task_id, members, task.get("assigned_to"), lang))
    await call.answer(t(lang, "assign_choose"))

@router.callback_query(F.data.startswith("asg_"))
async def assign_pick(call: CallbackQuery):
    _, task_id_str, user_id_str = call.data.split("_")
    task_id, user_id = int(task_id_str), int(user_id_str)
    task = await _authorize_task(call, task_id)
    if task is None:
        return
    lang = await owner_lang(task["owner_id"], task["owner_type"])
    # Поручить можно только участнику этой группы (callback_data можно подделать)
    if task["owner_type"] != "chat" or (user_id and not await db.is_chat_member(task["owner_id"], user_id)):
        await call.answer(t(lang, "not_authorized"), show_alert=True)
        return
    await db.update_task(task_id, assigned_to=user_id or None)
    updated = await db.get_task(task_id)
    text = await fmt_task(updated)
    await call.message.edit_text(text, reply_markup=task_kb(task_id, lang, done=updated["done"], assign=True),
                                 parse_mode="HTML")
    if not user_id:
        await call.answer(t(lang, "assign_cleared"))
        return
    name = display_name(user_id, (await db.get_telegram_users([user_id])).get(user_id))
    await call.answer(t(lang, "assign_done", name=name))
    if user_id != call.from_user.id:
        try:
            await app.bot.send_message(user_id, t(lang, "assigned_to_you", title=task["title"]))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass  # человек не начинал диалог с ботом — личное сообщение невозможно, это не ошибка
