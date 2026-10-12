"""POST-действия дашборда: задачи, подзадачи, шаблоны, уточнение личности
через Telegram Login Widget. У всех одна и та же защита: валидная сессия
владельца + CSRF-токен из формы; объект действия обязан принадлежать
владельцу дашборда."""

from datetime import datetime, time as dtime, timedelta

from aiohttp import web as aioweb

import db

from .config import SESSION_MAX_AGE, UTC, _acting_user_id, _tz_of
from .photos import _fetch_and_compress_photo
from .scheduler_hooks import _schedule_task, _unschedule_task
from .security import (
    _client_ip, _login_rate_limited, _record_failed_login, _require_session, _require_session_full,
    _sign_session, _verify_csrf, verify_telegram_login, verify_webapp_init_data,
)


def _resolve_template_due_at(time_of_day: str, day_offset: int, tz) -> datetime:
    """Та же логика, что и в bot.py:_resolve_template_due_at — датой
    отталкиваемся от момента ПРИМЕНЕНИЯ шаблона (сейчас), не от момента его
    создания. Продублировано здесь (а не импортировано из bot.py), потому что
    bot.py сам импортирует dashboard — обратный импорт создал бы цикл."""
    hour, minute = (int(p) for p in time_of_day.split(":"))
    now_local = datetime.now(tz)
    candidate_date = now_local.date() + timedelta(days=day_offset)
    candidate = datetime.combine(candidate_date, dtime(hour, minute), tzinfo=tz)
    if day_offset == 0 and candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


async def handle_template_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/new."""
    token = request.match_info["token"]
    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    title = data.get("title", "").strip()
    if not title:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates?error=empty_title"})

    if await db.count_templates(owner_id, owner_type) >= db.MAX_TEMPLATES_PER_OWNER:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates?error=limit"})

    time_of_day_raw = data.get("time_of_day", "")
    try:
        hour, minute = (int(p) for p in time_of_day_raw.split(":")[:2])
        time_of_day = f"{hour:02d}:{minute:02d}"
    except (ValueError, AttributeError):
        time_of_day = "09:00"

    try:
        day_offset = max(0, min(365, int(data.get("day_offset", "0"))))
    except ValueError:
        day_offset = 0

    await db.add_template(
        owner_id=owner_id, owner_type=owner_type, title=title[:200],
        time_of_day=time_of_day, day_offset=day_offset,
    )
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates"})


async def handle_template_apply(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/{template_id}/apply — создаёт задачу
    из шаблона прямо с дашборда. Как и у handle_task_create, задача создаётся
    без планирования в APScheduler бота (дашборд — отдельный веб-процесс без
    доступа к его scheduler'у) — ровно то же ограничение, что и у обычного
    добавления задачи с дашборда."""
    token = request.match_info["token"]
    try:
        template_id = int(request.match_info["template_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    tpl = await db.get_template(template_id)
    if not tpl or tpl["owner_id"] != owner_id or tpl["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    tz = _tz_of(settings)
    due_at = _resolve_template_due_at(tpl["time_of_day"], tpl["day_offset"], tz)
    remind_until_done = bool(tpl.get("remind_until_done", False))
    task_id = await db.add_task(
        owner_id, owner_type, tpl["title"], due_at, tpl["repeat"], tpl["remind"],
        tag=tpl.get("tag"), remind_until_done=remind_until_done, created_by=acting_user_id,
    )
    await db.log_history(task_id, acting_user_id, tpl["title"], "created")
    # Планируем сразу: иначе задача из шаблона (в т.ч. повторяющаяся) молча
    # не напоминала бы о себе до перезапуска процесса.
    _schedule_task(task_id, owner_id, owner_type, due_at, tpl["repeat"], tpl["remind"], tz,
                   remind_until_done=remind_until_done)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_template_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/{template_id}/delete."""
    token = request.match_info["token"]
    try:
        template_id = int(request.match_info["template_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    await db.delete_template(template_id, owner_id, owner_type)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates"})


async def handle_telegram_auth(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/telegram-auth — вызывается из onTelegramAuth()
    после успешного входа через Login Widget. Не выдаёт новых прав (действия и
    так разрешены паролем) — только уточняет, какой именно участник сейчас за
    дашбордом, чтобы история велась под правильным tg_user_id, а не chat_id."""
    token = request.match_info["token"]

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, _ = owner
    if owner_type != "chat":
        # На личном дашборде identity и так известна — виджет там не нужен,
        # и подменять владельца чужим Telegram-логином нельзя.
        return aioweb.Response(status=400)

    try:
        payload = await request.json()
    except Exception:
        return aioweb.Response(status=400)

    cookie = request.cookies.get(f"session_{token}", "")
    csrf_token = payload.pop("_csrf", "")
    if not _verify_csrf(csrf_token, cookie):
        return aioweb.Response(status=403)

    if not verify_telegram_login(payload):
        return aioweb.Response(status=403)

    tg_user_id = int(payload["id"])
    photo = await _fetch_and_compress_photo(tg_user_id)
    photo_data, photo_mime = photo if photo else (None, None)
    await db.upsert_telegram_user(tg_user_id, payload.get("username"), payload.get("first_name"), photo_data, photo_mime)

    pwd_version = settings.get("dashboard_password_version", 0)
    session_value = _sign_session(owner_id, owner_type, tg_user_id=tg_user_id, password_version=pwd_version)
    resp = aioweb.Response(status=200)
    is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="Strict", secure=is_https)
    return resp


async def handle_webapp_auth(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/webapp-auth — вход из Telegram Mini App по подписанному
    initData, без пароля (только личные дашборды). CSRF не нужен: сессии ещё
    нет, а защитой служит подпись. Неверные попытки считаются rate-limit'ом
    по IP, как и неверный пароль."""
    ip = _client_ip(request)
    if _login_rate_limited("webapp", ip):
        return aioweb.Response(status=429)
    try:
        payload = await request.json()
        init_data = payload["initData"]
    except (ValueError, KeyError, TypeError):
        return aioweb.Response(status=400)

    user = verify_webapp_init_data(init_data) if isinstance(init_data, str) else None
    if user is None:
        _record_failed_login("webapp", ip)
        return aioweb.Response(status=403)

    user_id = int(user["id"])
    token, pwd_version = await db.get_or_create_user_dashboard_token(user_id)
    session_value = _sign_session(user_id, "user", tg_user_id=user_id, password_version=pwd_version)
    resp = aioweb.json_response({"url": f"/dashboard/{token}"})
    is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="Strict", secure=is_https)
    return resp


def _parse_due_at(raw: str, tz) -> datetime | None:
    """Разбор значения поля due_at (<input type="datetime-local">). Пустое
    поле — это "сейчас" (поле необязательное), а вот нераспознанная строка
    возвращает None: вызывающая сторона показывает ошибку вместо того, чтобы
    молча поставить задачу на текущую минуту. Если браузер всё же прислал
    смещение (не naive-строку), оно уважается и переводится в tz владельца, а
    не затирается, как делал прежний .replace(tzinfo=tz)."""
    raw = (raw or "").strip()
    if not raw:
        return datetime.now(tz)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


async def handle_task_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/new — создание задачи прямо с дашборда.
    Та же защита, что и у остальных действий: сессия + CSRF."""
    token = request.match_info["token"]

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    title = data.get("title", "").strip()
    description = data.get("description", "").strip() or None
    if not title:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=empty_title"})

    tz = _tz_of(settings)
    due_at = _parse_due_at(data.get("due_at", ""), tz)
    if due_at is None:
        # Раньше нераспознанная дата молча превращалась в "сейчас", и задача
        # создавалась не на то время, о котором просил пользователь.
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=bad_due_at"})

    # repeat/remind — обязательные позиционные параметры add_task; с дашборда задача
    # создаётся без повтора и с дефолтным напоминанием (то же, что ожидает остальной код).
    task_id = await db.add_task(
        owner_id, owner_type, title, due_at, "none", "on_time",
        created_by=acting_user_id, description=description,
    )
    await db.log_history(task_id, acting_user_id, title, "created")
    _schedule_task(task_id, owner_id, owner_type, due_at, "none", "on_time", tz)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_task_toggle(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/{action}, action = done | undone.
    Без JS: обычная HTML-форма с redirect обратно на страницу дашборда."""
    token = request.match_info["token"]
    action = request.match_info["action"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)
    if action not in ("done", "undone", "postpone", "delete"):
        return aioweb.Response(status=404)

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    # Задача должна принадлежать именно этому владельцу — иначе валидный логин в
    # СВОЙ дашборд позволил бы менять задачи по произвольному id из чужого.
    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    # Джобы планировщика синхронизируем так же, как это делают кнопки в боте
    # (mark_done/mark_undone/snooze/delete_task в bot.py): иначе выполненная с
    # дашборда задача продолжала напоминать о себе, а удалённая оставляла
    # висеть джобу на уже несуществующий id.
    tz = _tz_of(settings)
    remind_until_done = bool(task.get("remind_until_done"))

    if action == "done":
        await db.mark_done(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "done")
        _unschedule_task(task_id)
    elif action == "undone":
        await db.mark_undone(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "undone")
        _schedule_task(task_id, owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                       remind_until_done=remind_until_done)
    elif action == "postpone":
        if task["repeat"] != "none":
            # Сдвиг due_at у повторяющейся задачи не отражается на её реальном
            # расписании (CronTrigger в bot.py строится по due_at только при
            # создании/рестарте) — см. комментарий у _render_task. Кнопка в
            # шаблоне уже скрыта для таких задач; это серверная подстраховка
            # на случай прямого POST мимо формы.
            return aioweb.Response(status=400)
        new_due = task["due_at"] + timedelta(days=1)
        await db.update_task(task_id, due_at=new_due)
        await db.log_history(task_id, acting_user_id, task["title"], "rescheduled")
        _unschedule_task(task_id)
        _schedule_task(task_id, owner_id, owner_type, new_due, "none", task["remind"], tz,
                       remind_until_done=remind_until_done)
    elif action == "delete":
        await db.delete_task(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "deleted")
        _unschedule_task(task_id)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_task_edit(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/edit — сохранение описания
    из раскрытой карточки задачи. Пустое поле очищает описание (NULL), а не
    хранит пустую строку — так meta-блок карточки снова корректно решает
    "показывать плейсхолдер или нет" по одному условию (task.get("description"))."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    description = data.get("description", "").strip()[:4000] or None
    await db.update_task(task_id, description=description)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{task_id}"})


async def _owned_subtask(subtask_id: int, owner_id: int, owner_type: str) -> dict | None:
    """Тот же принцип, что и у bot.py:_authorize_subtask — подзадача сама по
    себе владельца не хранит, поэтому владение проверяется через её task_id.
    Возвращает подзадачу при успехе, иначе None."""
    sub = await db.get_subtask(subtask_id)
    if not sub:
        return None
    task = await db.get_task(sub["task_id"])
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return None
    return sub


async def handle_subtask_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/subtasks/new."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    title = data.get("title", "").strip()
    if title:
        await db.add_subtask(task_id, title[:200])

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{task_id}"})


async def handle_subtask_toggle(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/subtasks/{subtask_id}/toggle."""
    token = request.match_info["token"]
    try:
        subtask_id = int(request.match_info["subtask_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    sub = await _owned_subtask(subtask_id, owner_id, owner_type)
    if sub is None:
        return aioweb.Response(status=404)

    await db.toggle_subtask(subtask_id)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{sub['task_id']}"})


async def handle_subtask_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/subtasks/{subtask_id}/delete."""
    token = request.match_info["token"]
    try:
        subtask_id = int(request.match_info["subtask_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    sub = await _owned_subtask(subtask_id, owner_id, owner_type)
    if sub is None:
        return aioweb.Response(status=404)

    await db.delete_subtask(subtask_id)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{sub['task_id']}"})
