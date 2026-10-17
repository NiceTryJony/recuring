"""JSON-ответы для fetch-действий дашборда (без перезагрузки страницы).

Формы по-прежнему работают без JS: обычный POST отвечает редиректом. JS
(static/dashboard.js) шлёт тот же POST с `Accept: application/json` и
контекстом страницы в поле `ctx` (query-строка, на которой стоит человек:
вкладка, группировка, тег, день) — сервер отвечает готовыми HTML-фрагментами,
которые клиент подставляет на место. Фрагменты строятся теми же функциями,
что и полная страница, поэтому вид не расходится."""

import datetime as dt
from urllib.parse import parse_qs

from aiohttp import web as aioweb

import db
from task_groups import GROUPS, VIEWS

from .config import DONE_TASKS_PAGE_SIZE, _tz_of
from .render import _render_description, _render_subtasks, _render_task, render_photos_block
from .security import _csrf_token, _verify_session
from .texts import _dt
from .views import _more_done_html, _page_state, _safe_chart, _view_controls


def wants_json(request: aioweb.Request) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def parse_ctx(raw: str) -> dict:
    """Разбор query-строки страницы, на которой стоит человек. Мусор в
    параметрах молча заменяется значениями по умолчанию (как в handle_dashboard)."""
    q = {k: v[0] for k, v in parse_qs((raw or "").lstrip("?"), keep_blank_values=False).items()}
    view = q.get("view") if q.get("view") in VIEWS else "upcoming"
    group = q.get("group") if q.get("group") in GROUPS else "date"
    selected = None
    if q.get("date"):
        try:
            selected = dt.date.fromisoformat(q["date"])
        except ValueError:
            selected = None
    try:
        done_limit = max(DONE_TASKS_PAGE_SIZE, int(q.get("done_limit", DONE_TASKS_PAGE_SIZE)))
    except ValueError:
        done_limit = DONE_TASKS_PAGE_SIZE
    return {"view": view, "group": group, "tag": q.get("tag") or None, "date": selected,
            "done_limit": done_limit, "nojs": q.get("nojs") == "1"}


async def state_payload(request: aioweb.Request, token: str, settings: dict, owner_id: int, owner_type: str,
                        data, *, task_id: int | None = None, with_chart: bool = False) -> dict:
    """Свежее состояние после действия: карточка задачи (или null, если в
    текущем виде её больше нет), цифры сводки и панель вкладок/тегов."""
    lang = settings.get("language", "ru")
    texts = _dt(lang)
    tz = _tz_of(settings)
    ctx = parse_ctx(data.get("ctx", ""))
    now = dt.datetime.now(tz)

    state = await _page_state(
        owner_id, owner_type, tz, now, view=ctx["view"], tag=ctx["tag"], done_limit=ctx["done_limit"],
        selected_date=ctx["date"], filter_active=ctx["date"] is not None, group=ctx["group"],
    )
    tasks, local_due, stats = state["tasks"], state["local_due"], state["stats"]
    payload: dict = {
        "task_id": task_id,
        "stats": {"today": stats["today"], "overdue": stats["overdue"], "progress": state["progress"]},
        "controls": _view_controls(token, texts, ctx["view"], ctx["group"], ctx["tag"], state["tags"], ctx["nojs"]),
        "more_done": _more_done_html(token, texts, state["hidden_done"], ctx["done_limit"], ctx["nojs"], ctx["view"]),
        "empty": texts["empty"] if not tasks else None,
        "card": None,
    }
    if with_chart:
        chart_html, streak = await _safe_chart(owner_id, owner_type, tz, texts)
        payload["chart"] = chart_html
        if streak is not None:
            payload["stats"]["streak"] = streak

    task = next((t for t in tasks if t["id"] == task_id), None) if task_id is not None else None
    if task is not None:
        cookie = request.cookies.get(f"session_{token}", "")
        session = _verify_session(cookie)    # подпись уже проверена вызывающим действием; нужен только tg_user_id
        creators = {}
        people = [task[k] for k in ("created_by", "assigned_to") if task.get(k)]
        if owner_type == "chat" and people:
            creators = await db.get_telegram_users(people)
        photos = await db.get_task_photos_meta_bulk([task["id"]])
        subtasks = await db.get_subtasks_bulk([task["id"]])
        payload["card"] = _render_task(
            task, tz, lang, token, _csrf_token(cookie), creator=creators.get(task.get("created_by")),
            owner_type=owner_type, photos=photos.get(task["id"]), subtasks=subtasks.get(task["id"]),
            now=now, local_due=local_due[task["id"]], lazy=not ctx["nojs"],
            assignee=creators.get(task.get("assigned_to")), viewer_id=session[2] if session else 0,
        )
    return payload


async def subtasks_payload(request: aioweb.Request, token: str, settings: dict, task_id: int) -> dict:
    texts = _dt(settings.get("language", "ru"))
    cookie = request.cookies.get(f"session_{token}", "")
    subtasks = await db.get_subtasks(task_id)
    return {"task_id": task_id, "subtasks": _render_subtasks(subtasks, token, task_id, _csrf_token(cookie), texts)}


def description_payload(request: aioweb.Request, token: str, settings: dict, task: dict) -> dict:
    texts = _dt(settings.get("language", "ru"))
    cookie = request.cookies.get(f"session_{token}", "")
    return {"task_id": task["id"], "description": _render_description(task, token, _csrf_token(cookie), texts)}


async def photos_payload(request: aioweb.Request, token: str, task_id: int, error: str | None = None) -> dict:
    settings = await db.get_owner_by_dashboard_token(token)
    texts = _dt((settings or {}).get("language", "ru"))
    cookie = request.cookies.get(f"session_{token}", "")
    photos = await db.get_task_photos_meta(task_id)
    payload = {"task_id": task_id, "photos": render_photos_block(photos, token, task_id, _csrf_token(cookie), texts)}
    if error:
        payload["error"] = texts[f"error_{error}"]
    return payload
