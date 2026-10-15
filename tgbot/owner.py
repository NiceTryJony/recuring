"""Владелец задач (личный пользователь / групповой чат), его настройки и время."""

from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import CallbackQuery, Chat

import db
from i18n import t

from .constants import DEFAULT_TZ, GROUP_CHAT_TYPES

# ---------- владелец задач: личный пользователь или групповой чат ----------
#
# owner_type='user' (owner_id=user_id) для личных чатов, owner_type='chat'
# (owner_id=chat_id) для групп/супергрупп. Все настройки (часовой пояс, язык,
# дашборд) и задачи висят на владельце, а не всегда на конкретном user_id —
# это и есть групповой режим.

# Последнее записанное в БД название группы: пишем только при изменении,
# а не на каждое сообщение.
_chat_titles: dict[int, str | None] = {}


async def resolve_owner(chat: Chat, from_user_id: int) -> tuple[int, str]:
    if chat.type in GROUP_CHAT_TYPES:
        await db.add_chat_member(chat.id, from_user_id)
        if chat.id not in _chat_titles or _chat_titles[chat.id] != chat.title:
            await db.set_chat_title(chat.id, chat.title)
            _chat_titles[chat.id] = chat.title
        return chat.id, "chat"
    return from_user_id, "user"

async def _can_access_task(task: dict, chat: Chat, from_user_id: int) -> bool:
    """Может ли этот пользователь управлять этой задачей (отмечать выполненной,
    удалять, редактировать и т.п.)?

    Два легитимных случая:
    1. Нажатие в том же чате, которому принадлежит задача (обычный случай
       для личных задач, и для групповых — кнопка под сообщением в самой группе).
    2. Нажатие в личке с ботом по задаче чата (owner_type='chat') — так
       приходят уведомления о групповых задачах (_recipients рассылает их
       личными сообщениями), поэтому chat.id нажавшего будет его личным ЛС,
       а не chat_id задачи. Легитимно, только если нажавший реально состоит
       в участниках этого чата (db.chat_members)."""
    owner_id, owner_type = task["owner_id"], task["owner_type"]

    same_owner = (owner_id, owner_type) == await resolve_owner(chat, from_user_id)
    if same_owner:
        return True

    if owner_type == "chat":
        return await db.is_chat_member(owner_id, from_user_id)

    return False

async def _authorize_task(call: CallbackQuery, task_id: int) -> dict | None:
    """Общая точка входа для всех действий с задачей по кнопке: загружает
    задачу и проверяет права. Возвращает задачу при успехе; при провале сама
    отвечает call.answer() с текстом ошибки (на известном нам языке, если task
    существует — иначе на дефолтном) и возвращает None. Вызывающий код просто
    делает `if task is None: return`."""
    task = await db.get_task(task_id)
    if not task:
        await call.answer()
        return None
    if not await _can_access_task(task, call.message.chat, call.from_user.id):
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        await call.answer(t(lang, "not_authorized"), show_alert=True)
        return None
    return task

async def _authorize_subtask(call: CallbackQuery, subtask_id: int) -> tuple[dict, dict] | None:
    """То же самое для подзадач: сверяет права по родительской задаче.
    Возвращает (subtask, task) при успехе, иначе None (и сама отвечает call.answer())."""
    sub = await db.get_subtask(subtask_id)
    if not sub:
        await call.answer()
        return None
    task = await db.get_task(sub["task_id"])
    if not task:
        await call.answer()
        return None
    if not await _can_access_task(task, call.message.chat, call.from_user.id):
        lang = await owner_lang(task["owner_id"], task["owner_type"])
        await call.answer(t(lang, "not_authorized"), show_alert=True)
        return None
    return sub, task

# ---------- helpers: настройки владельца ----------

_settings_cache: dict[tuple[str, int], dict] = {}

async def get_settings(owner_id: int, owner_type: str) -> dict:
    key = (owner_type, owner_id)
    if key not in _settings_cache:
        _settings_cache[key] = await db.get_owner_settings(owner_id, owner_type)
    return _settings_cache[key]

def invalidate_settings(owner_id: int, owner_type: str):
    _settings_cache.pop((owner_type, owner_id), None)

async def owner_tz(owner_id: int, owner_type: str) -> ZoneInfo:
    s = await get_settings(owner_id, owner_type)
    try:
        return ZoneInfo(s["timezone"])
    except ZoneInfoNotFoundError:
        return DEFAULT_TZ

async def owner_lang(owner_id: int, owner_type: str) -> str:
    s = await get_settings(owner_id, owner_type)
    return s.get("language", "ru")

async def owner_quiet(owner_id: int, owner_type: str) -> tuple[dtime, dtime] | None:
    s = await get_settings(owner_id, owner_type)
    start, end = s.get("quiet_hours_start"), s.get("quiet_hours_end")
    if start is None or end is None or start == end:
        return None
    return start, end

def to_utc(local_dt: datetime, tz: ZoneInfo) -> datetime:
    return local_dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))

def to_local(utc_dt: datetime, tz: ZoneInfo) -> datetime:
    return utc_dt.astimezone(tz)

def normalize_tag(text: str) -> str | None:
    """Без этого «Дом», «дом » и «ДОМ» были бы тремя разными тегами в /tags и
    в фильтре задач (get_owner_tags делает DISTINCT по сырой строке) —
    приводим к единому виду до сохранения в БД. Пустая строка/"/skip" = None
    (без тега), не пустой тег нормализованный."""
    stripped = text.strip()
    if not stripped or stripped == "/skip":
        return None
    return stripped.lower()

def _parse_hh_mm(text: str) -> tuple[int, int] | None:
    """Разбор 'ЧЧ:ММ' в (hour, minute) или None при невалидном вводе."""
    text = text.strip()
    if ":" not in text:
        return None
    h_str, _, m_str = text.partition(":")
    if not (h_str.isdigit() and m_str.isdigit()):
        return None
    hour, minute = int(h_str), int(m_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute
