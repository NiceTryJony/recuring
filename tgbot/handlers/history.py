"""/history и /export."""

import csv
import io
import json
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
from i18n import t
from stats import daily_series, totals

from ..app import logger
from ..constants import HISTORY_MODES
from ..formatting import _current_streak
from ..keyboards import history_kb
from ..owner import owner_lang, owner_tz, resolve_owner, to_local

router = Router()


async def _render_history_text(user_id: int, owner_id: int, owner_type: str, lang: str, mode: str) -> str:
    tz = await owner_tz(owner_id, owner_type)
    if mode == "recent":
        events = await db.get_history(user_id)
        if not events:
            return t(lang, "no_history")
        event_labels = {
            "created": t(lang, "history_created"),
            "done": "✅",
            "undone": "↩️",
            "deleted": "🗑",
            "rescheduled": "📅",
        }
        lines = [t(lang, "history_title")]
        for e in events:
            local_dt = to_local(e["event_at"], tz)
            icon = event_labels.get(e["event"], "•")
            lines.append(f"{icon} {e['title']} — {local_dt.strftime('%d.%m %H:%M')}")
        return "\n".join(lines)

    days = int(mode)
    rows = await db.get_history_stats(user_id, days, tz.key)
    tot = totals(rows)
    lines = [t(lang, "stats_title", days=days), "", t(lang, "stats_totals", **tot)]

    # Только за ≤7 дней: выполненные задачи старше недели удаляются (purge_done_tasks),
    # за больший период процент был бы занижен.
    if days <= 7:
        try:
            rate = await db.get_completion_rate(owner_id, owner_type, days, tz.key)
            if rate["total"] > 0:
                lines.append(t(lang, "stats_completion_rate", **rate))
        except Exception:
            logger.exception("Не удалось посчитать %% выполнения owner=%s/%s", owner_type, owner_id)

    if days <= 7:
        weekdays = t(lang, "weekdays_short").split(",")
        lines.append("")
        for d, cnt in daily_series(rows, "done", days, datetime.now(tz).date()):
            bar = "▇" * min(cnt, 10) if cnt else "·"
            lines.append(f"{weekdays[d.weekday()]} {d.strftime('%d.%m')}  {bar} {cnt}")
    streak = await _current_streak(user_id, tz)
    if streak > 0:
        lines += ["", t(lang, "streak_line", n=streak).strip()]
    return "\n".join(lines)

@router.message(Command("history"))
async def history_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    text = await _render_history_text(message.from_user.id, owner_id, owner_type, lang, "recent")
    await message.answer(text, reply_markup=history_kb(lang, "recent"))

@router.callback_query(F.data.startswith("hist_"))
async def history_mode_pick(call: CallbackQuery):
    mode = call.data.split("_", 1)[1]
    if mode not in HISTORY_MODES:
        owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
        lang = await owner_lang(owner_id, owner_type)
        await call.answer(t(lang, "history_mode_unknown"), show_alert=True)
        return
    owner_id, owner_type = await resolve_owner(call.message.chat, call.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    text = await _render_history_text(call.from_user.id, owner_id, owner_type, lang, mode)
    try:
        await call.message.edit_text(text, reply_markup=history_kb(lang, mode))
    except TelegramBadRequest:
        pass  # "message is not modified" — нажали уже активную кнопку
    await call.answer()

# ---------- /export ----------

@router.message(Command("export"))
async def export_cmd(message: Message):
    owner_id, owner_type = await resolve_owner(message.chat, message.from_user.id)
    lang = await owner_lang(owner_id, owner_type)
    tasks = await db.get_tasks(owner_id, owner_type, include_done=True)
    tz = await owner_tz(owner_id, owner_type)

    if not tasks:
        await message.answer(t(lang, "no_tasks"))
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "due_at", "repeat", "remind", "tag", "done"])
    for task in tasks:
        local_dt = to_local(task["due_at"], tz)
        writer.writerow([
            task["id"], task["title"], local_dt.strftime("%Y-%m-%d %H:%M"),
            task["repeat"], task["remind"] or "0", task.get("tag") or "", task["done"],
        ])

    data = buf.getvalue().encode("utf-8-sig")  # BOM для корректного открытия в Excel с кириллицей
    file = BufferedInputFile(data, filename="tasks_export.csv")
    await message.answer_document(file)

    # JSON — полнее CSV (описание, подзадачи, tz-aware сроки): годится как резервная копия
    subtasks = await db.get_subtasks_bulk([task["id"] for task in tasks])
    backup = [
        {
            "title": task["title"], "description": task.get("description"), "tag": task.get("tag"),
            "due_at": task["due_at"].isoformat(), "repeat": task["repeat"], "remind": task["remind"],
            "done": task["done"], "done_at": task["done_at"].isoformat() if task.get("done_at") else None,
            "subtasks": [{"title": s["title"], "done": s["done"]} for s in subtasks.get(task["id"], [])],
        }
        for task in tasks
    ]
    payload = json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")
    await message.answer_document(BufferedInputFile(payload, filename="tasks_backup.json"))
