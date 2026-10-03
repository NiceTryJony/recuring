import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web as aioweb
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db

API_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Europe/Warsaw")
PAGE_SIZE = 5

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)

REPEAT_OPTIONS = {
    "none": "Без повтора",
    "daily": "Каждый день",
    "weekly": "Каждую неделю",
    "monthly": "Каждый месяц",
    "yearly": "Каждый год",
}
REMIND_OPTIONS = {"0": "Без напоминания", "1h": "За 1 час", "1d": "За 1 день", "3d": "За 3 дня"}


class AddTask(StatesGroup):
    title = State()
    date = State()
    repeat = State()
    remind = State()


class EditTask(StatesGroup):
    field = State()
    value = State()


# ---------- клавиатуры ----------

def repeat_kb(prefix="rep"):
    kb = [[InlineKeyboardButton(text=v, callback_data=f"{prefix}_{k}")] for k, v in REPEAT_OPTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def remind_kb(prefix="rem"):
    kb = [[InlineKeyboardButton(text=v, callback_data=f"{prefix}_{k}")] for k, v in REMIND_OPTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def task_kb(task_id: int):
    kb = [
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_{task_id}")],
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_{task_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{task_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def confirm_delete_kb(task_id: int):
    kb = [[
        InlineKeyboardButton(text="Да, удалить", callback_data=f"delok_{task_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"delno_{task_id}"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def edit_field_kb(task_id: int):
    kb = [
        [InlineKeyboardButton(text="Название", callback_data=f"ef_title_{task_id}")],
        [InlineKeyboardButton(text="Дату", callback_data=f"ef_date_{task_id}")],
        [InlineKeyboardButton(text="Повтор", callback_data=f"ef_repeat_{task_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def pagination_kb(page: int, has_next: bool):
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


# ---------- helpers ----------

def to_utc(local_dt: datetime) -> datetime:
    return local_dt.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC"))


def to_local(utc_dt: datetime) -> datetime:
    return utc_dt.astimezone(TZ)


def fmt_task(t: dict) -> str:
    local_dt = to_local(t["due_at"])
    status = "✅" if t["done"] else "⏳"
    text = f"{status} <b>{t['title']}</b>\n📅 {local_dt.strftime('%d.%m.%Y %H:%M')}"
    if t["repeat"] != "none":
        text += f"\n🔁 {REPEAT_OPTIONS[t['repeat']]}"
    if t["remind"] != "0":
        text += f"\n🔔 {REMIND_OPTIONS[t['remind']]}"
    text += f"\n<code>#{t['id']}</code>"
    return text


# ---------- базовые команды ----------

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я слежу за твоими задачами и сроками.\n\n"
        "/add — добавить задачу\n"
        "/list — список задач\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start(message)


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


# ---------- добавление задачи ----------

@dp.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    await state.set_state(AddTask.title)
    await message.answer("Название задачи/события:\n(/cancel — отменить)")


@dp.message(AddTask.title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddTask.date)
    await message.answer("Дата и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nнапример: 15.10.2026 18:00")


@dp.message(AddTask.date)
async def add_date(message: Message, state: FSMContext):
    try:
        local_dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("Неверный формат. Пример: 15.10.2026 18:00")
        return
    await state.update_data(due_at=to_utc(local_dt).isoformat())
    await state.set_state(AddTask.repeat)
    await message.answer("Повторять?", reply_markup=repeat_kb())


@dp.callback_query(AddTask.repeat, F.data.startswith("rep_"))
async def add_repeat(call: CallbackQuery, state: FSMContext):
    repeat = call.data.split("_", 1)[1]
    await state.update_data(repeat=repeat)
    await state.set_state(AddTask.remind)
    await call.message.edit_text("Напомнить заранее?", reply_markup=remind_kb())
    await call.answer()


@dp.callback_query(AddTask.remind, F.data.startswith("rem_"))
async def add_remind(call: CallbackQuery, state: FSMContext):
    remind = call.data.split("_", 1)[1]
    data = await state.get_data()
    due_at = datetime.fromisoformat(data["due_at"])

    task_id = await db.add_task(
        user_id=call.from_user.id,
        title=data["title"],
        due_at=due_at,
        repeat=data["repeat"],
        remind=remind,
    )
    schedule_task(task_id, call.from_user.id, due_at, data["repeat"], remind)

    local_str = to_local(due_at).strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(f"✅ Задача «{data['title']}» добавлена на {local_str}")
    await state.clear()
    await call.answer()


# ---------- список задач ----------

async def render_list(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    tasks = await db.get_tasks(user_id)
    if not tasks:
        return "Задач нет. Добавь через /add", None
    start_i = page * PAGE_SIZE
    chunk = tasks[start_i:start_i + PAGE_SIZE]
    has_next = len(tasks) > start_i + PAGE_SIZE
    text = "\n\n".join(fmt_task(t) for t in chunk)
    text += f"\n\nСтраница {page+1}"
    return text, pagination_kb(page, has_next)


@dp.message(Command("list"))
async def list_tasks(message: Message):
    tasks = await db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Задач нет. Добавь через /add")
        return
    for t in tasks[:PAGE_SIZE]:
        await message.answer(fmt_task(t), reply_markup=task_kb(t["id"]), parse_mode="HTML")
    if len(tasks) > PAGE_SIZE:
        await message.answer(f"Показаны первые {PAGE_SIZE} из {len(tasks)}. Используй /list ещё раз позже или отметь текущие выполненными.")


# ---------- действия с задачей ----------

@dp.callback_query(F.data.startswith("done_"))
async def mark_done(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    await db.mark_done(task_id)
    try:
        scheduler.remove_job(f"due_{task_id}")
    except Exception:
        pass
    try:
        scheduler.remove_job(f"remind_{task_id}")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n✅ Отмечено выполненным")
    await call.answer("Готово!")


@dp.callback_query(F.data.startswith("del_"))
async def delete_confirm(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    await call.message.edit_reply_markup(reply_markup=confirm_delete_kb(task_id))
    await call.answer()


@dp.callback_query(F.data.startswith("delok_"))
async def delete_task(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    await db.delete_task(task_id)
    for prefix in ("due", "remind"):
        try:
            scheduler.remove_job(f"{prefix}_{task_id}")
        except Exception:
            pass
    await call.message.edit_text("🗑 Задача удалена.")
    await call.answer()


@dp.callback_query(F.data.startswith("delno_"))
async def delete_cancel(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    task = await db.get_task(task_id)
    if task:
        await call.message.edit_text(fmt_task(task), reply_markup=task_kb(task_id), parse_mode="HTML")
    await call.answer("Отменено")


@dp.callback_query(F.data.startswith("edit_"))
async def edit_choose_field(call: CallbackQuery):
    task_id = int(call.data.split("_")[1])
    await call.message.edit_reply_markup(reply_markup=edit_field_kb(task_id))
    await call.answer()


@dp.callback_query(F.data.startswith("ef_"))
async def edit_field_selected(call: CallbackQuery, state: FSMContext):
    _, field, task_id = call.data.split("_")
    task_id = int(task_id)

    if field == "repeat":
        await call.message.edit_text(
            "Выбери новый повтор:", reply_markup=repeat_kb(prefix=f"editrep_{task_id}")
        )
        await call.answer()
        return

    await state.set_state(EditTask.value)
    await state.update_data(task_id=task_id, field=field)
    prompt = "Новое название:" if field == "title" else "Новая дата ДД.ММ.ГГГГ ЧЧ:ММ:"
    await call.message.edit_text(prompt)
    await call.answer()


@dp.callback_query(F.data.startswith("editrep_"))
async def edit_repeat_apply(call: CallbackQuery):
    _, task_id, new_repeat = call.data.split("_")
    task_id = int(task_id)
    await db.update_task(task_id, repeat=new_repeat)
    task = await db.get_task(task_id)
    try:
        scheduler.remove_job(f"due_{task_id}")
    except Exception:
        pass
    schedule_task(task_id, task["user_id"], task["due_at"], new_repeat, task["remind"])
    await call.message.edit_text(fmt_task(task) + "\n\n✅ Повтор обновлён", reply_markup=task_kb(task_id), parse_mode="HTML")
    await call.answer()


@dp.message(EditTask.value)
async def edit_value_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id, field = data["task_id"], data["field"]

    if field == "title":
        await db.update_task(task_id, title=message.text.strip())
    elif field == "date":
        try:
            local_dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        except ValueError:
            await message.answer("Неверный формат. Пример: 15.10.2026 18:00")
            return
        due_at = to_utc(local_dt)
        await db.update_task(task_id, due_at=due_at)
        task = await db.get_task(task_id)
        try:
            scheduler.remove_job(f"due_{task_id}")
        except Exception:
            pass
        try:
            scheduler.remove_job(f"remind_{task_id}")
        except Exception:
            pass
        schedule_task(task_id, task["user_id"], due_at, task["repeat"], task["remind"])

    await state.clear()
    task = await db.get_task(task_id)
    await message.answer(fmt_task(task) + "\n\n✅ Обновлено", reply_markup=task_kb(task_id), parse_mode="HTML")


# ---------- планировщик ----------

def schedule_task(task_id: int, user_id: int, due_at_utc: datetime, repeat: str, remind: str):
    now_utc = datetime.now(ZoneInfo("UTC"))

    if remind != "0":
        delta = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "3d": timedelta(days=3)}[remind]
        remind_time = due_at_utc - delta
        if remind_time > now_utc:
            scheduler.add_job(
                send_reminder, "date", run_date=remind_time,
                args=[user_id, task_id], id=f"remind_{task_id}", replace_existing=True
            )

    local_due = to_local(due_at_utc)
    if repeat == "none":
        if due_at_utc > now_utc:
            scheduler.add_job(
                send_due, "date", run_date=due_at_utc,
                args=[user_id, task_id], id=f"due_{task_id}", replace_existing=True
            )
    else:
        cron_map = {
            "daily": CronTrigger(hour=local_due.hour, minute=local_due.minute, timezone=TZ),
            "weekly": CronTrigger(day_of_week=local_due.weekday(), hour=local_due.hour, minute=local_due.minute, timezone=TZ),
            "monthly": CronTrigger(day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=TZ),
            "yearly": CronTrigger(month=local_due.month, day=local_due.day, hour=local_due.hour, minute=local_due.minute, timezone=TZ),
        }
        scheduler.add_job(
            send_due, cron_map[repeat],
            args=[user_id, task_id], id=f"due_{task_id}", replace_existing=True
        )


async def send_reminder(user_id: int, task_id: int):
    task = await db.get_task(task_id)
    if task and not task["done"]:
        await bot.send_message(user_id, f"⏰ Напоминание: «{task['title']}» скоро наступит!")


async def send_due(user_id: int, task_id: int):
    task = await db.get_task(task_id)
    if task and not task["done"]:
        await bot.send_message(user_id, f"🔔 Срок наступил: «{task['title']}»",
                                reply_markup=task_kb(task_id))


async def restore_jobs():
    """Восстанавливает джобы при старте процесса (после редеплоя/рестарта).
    Если дедлайн уже прошёл сегодня, а бот был офлайн — досылает уведомление сразу,
    чтобы не потерять его (вместо планирования в прошлое, что APScheduler просто пропустит)."""
    now_utc = datetime.now(ZoneInfo("UTC"))
    today_start_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    tasks = await db.get_all_active_tasks()
    for t in tasks:
        due_local = to_local(t["due_at"])
        missed_today = (
            t["repeat"] == "none"
            and t["due_at"] < now_utc
            and due_local >= today_start_local
        )
        if missed_today:
            await send_due(t["user_id"], t["id"])
        else:
            schedule_task(t["id"], t["user_id"], t["due_at"], t["repeat"], t["remind"])


# ---------- health-check веб-сервер (нужен Render Web Service, чтобы видеть открытый порт) ----------
# Render требует, чтобы процесс слушал $PORT — иначе деплой считается "упавшим".
# Сам по себе этот эндпоинт ничего не делает, кроме как отвечает 200 OK.
# Чтобы сервис не "засыпал" на free tier — настрой внешний пинг (UptimeRobot и т.п.)
# на URL твоего сервиса раз в 5-10 минут.

async def health(request):
    return aioweb.Response(text="ok")


async def run_health_server():
    app = aioweb.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = aioweb.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info(f"Health server listening on port {port}")


# ---------- запуск ----------

async def main():
    await db.init_db()
    scheduler.start()
    await restore_jobs()
    await run_health_server()
    try:
        await dp.start_polling(bot)
    finally:
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())