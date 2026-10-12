"""Форматирование задач и повторов для сообщений."""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

import db
from i18n import t
from stats import compute_streak

from .constants import _NUMBER_EMOJI, REMIND_OPTIONS, REPEAT_OPTIONS
from .owner import owner_lang, owner_tz, to_local

# ---------- форматирование ----------

def fmt_repeat(repeat: str, lang: str) -> str:
    return REPEAT_OPTIONS.get(repeat, {}).get(lang, repeat)

def fmt_remind(remind: str, lang: str) -> str:
    """remind хранится как список кодов через запятую, например '1h,1d'."""
    if not remind or remind == "0":
        return REMIND_OPTIONS["0"][lang]
    parts = [REMIND_OPTIONS[c][lang] for c in remind.split(",") if c in REMIND_OPTIONS]
    return ", ".join(parts) if parts else REMIND_OPTIONS["0"][lang]

def _task_status_icon(t_row: dict) -> str:
    """✅ выполнена, ⚠️ просрочена (есть срок в прошлом и не выполнена), иначе ⏳."""
    if t_row["done"]:
        return "✅"
    due = t_row["due_at"]
    if due and due < datetime.now(ZoneInfo("UTC")):
        return "⚠️"
    return "⏳"

async def fmt_task(t_row: dict) -> str:
    """Язык и часовой пояс берутся из владельца самой задачи (t_row['owner_id']/
    ['owner_type']), а не из того, кто сейчас смотрит карточку — так в групповом
    чате все видят задачу в едином, настроенном для чата виде."""
    lang = await owner_lang(t_row["owner_id"], t_row["owner_type"])
    tz = await owner_tz(t_row["owner_id"], t_row["owner_type"])
    local_dt = to_local(t_row["due_at"], tz)
    status = _task_status_icon(t_row)
    # parse_mode=HTML: без экранирования заголовок вида "a < b" ломает отправку (TelegramBadRequest)
    text = f"{status} <b>{html.escape(t_row['title'])}</b>\n📅 {local_dt.strftime('%d.%m.%Y %H:%M')}"
    if t_row["repeat"] != "none":
        text += f"\n🔁 {fmt_repeat(t_row['repeat'], lang)}"
    if t_row["remind"] and t_row["remind"] != "0":
        text += f"\n🔔 {fmt_remind(t_row['remind'], lang)}"
    if t_row.get("remind_until_done"):
        text += f"\n{t(lang, 'nag_line')}"
    if t_row.get("tag"):
        text += f"\n🏷 {html.escape(t_row['tag'])}"
    text += f"\n<code>#{t_row['id']}</code>"
    return text

def fmt_task_line(t_row: dict, number: int, tz: ZoneInfo) -> str:
    """Одна строка компактного списка: номер (соответствует кнопке с тем же
    номером), статус, название (обрезано, чтобы длинные заголовки не ломали
    строку), дата. Без HTML-тегов — список шлётся как HTML, поэтому экранируем."""
    status = _task_status_icon(t_row)
    local_dt = to_local(t_row["due_at"], tz)
    title = t_row["title"]
    if len(title) > 40:
        title = title[:39] + "…"
    title = html.escape(title)
    date_str = local_dt.strftime("%d.%m %H:%M")
    tag_suffix = f" 🏷{html.escape(t_row['tag'])}" if t_row.get("tag") else ""
    return f"{_NUMBER_EMOJI[number - 1]} {status} {title} — {date_str}{tag_suffix}"

async def _current_streak(user_id: int, tz: ZoneInfo) -> int:
    """Серия: сколько дней подряд была хотя бы одна выполненная задача (см. stats.compute_streak)."""
    days = await db.get_done_days(user_id, tz.key)
    return compute_streak(days, datetime.now(tz).date())
