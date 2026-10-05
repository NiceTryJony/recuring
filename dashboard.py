"""Веб-дашборд: простая HTML-страница со списком задач. Доступ по уникальному
токену в URL (/dashboard/<token>) + пароль в форме логина. Токен может
принадлежать как личному пользователю, так и групповому чату (общие задачи
чата) — db.get_owner_by_dashboard_token() ищет среди обоих и возвращает
owner_id/owner_type, дальше вся логика страницы работает с ними одинаково.

Сессия хранится в подписанной cookie (без внешних зависимостей — просто HMAC
поверх owner_type:owner_id + expiry, валидируется на каждый запрос).

Язык страницы берётся из настроек владельца (user_settings.language для
личного дашборда или chat_settings.language для группового — то, что задано
в боте через /lang). Переводы не завязаны на i18n.py бота, чтобы не тащить
aiogram-зависимости в веб-слой — здесь свой маленький словарь DASHBOARD_TEXTS."""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from html import escape

from aiohttp import web as aioweb

import db
from stats import compute_streak, daily_series

logger = logging.getLogger(__name__)

CHART_DAYS = 14
SESSION_SECRET = os.environ.get("BOT_TOKEN", "fallback-secret")  # используем токен бота как секрет для HMAC
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # неделя


# ---------- пароли ----------
# Раньше пароль хранился как незасоленный SHA-256 (подбирается по радужным таблицам за секунды
# при утечке БД). Теперь — PBKDF2-HMAC-SHA256 с солью; старые хеши (64 hex-символа) продолжают
# проверяться, так что уже созданные дашборды не ломаются.
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        except ValueError:
            return False
        return hmac.compare_digest(dk.hex(), hash_hex)
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()  # старый формат
    return hmac.compare_digest(legacy, stored)


def _sign_session(owner_id: int, owner_type: str, token: str) -> str:
    payload = f"{owner_type}:{owner_id}:{int(time.time()) + SESSION_MAX_AGE}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session(cookie_value: str) -> tuple[int, str] | None:
    try:
        owner_type, owner_id_str, expiry_str, sig = cookie_value.split(":")
        payload = f"{owner_type}:{owner_id_str}:{expiry_str}"
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expiry_str) < time.time():
            return None
        return int(owner_id_str), owner_type
    except (ValueError, AttributeError):
        return None


async def _require_session(request: aioweb.Request, token: str) -> tuple[int, str] | None:
    """Общая проверка доступа для всех POST-действий (done/undone и т.д.): владелец
    дашборда должен существовать, а cookie — валидно расписываться именно на него.
    Возвращает (owner_id, owner_type) или None, если доступ запрещён."""
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return None
    cookie = request.cookies.get(f"session_{token}")
    session = _verify_session(cookie) if cookie else None
    expected = (settings["owner_id"], settings["owner_type"])
    if session != expected:
        return None
    return expected


# ---------- CSRF ----------
# Токен — HMAC от значения сессионной cookie: привязан к конкретной сессии, но
# не требует отдельного хранилища на сервере (stateless, как и сама сессия).

def _csrf_token(session_cookie_value: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), session_cookie_value.encode(), hashlib.sha256).hexdigest()


def _verify_csrf(request_token: str, session_cookie_value: str) -> bool:
    if not request_token or not session_cookie_value:
        return False
    expected = _csrf_token(session_cookie_value)
    return hmac.compare_digest(request_token, expected)


# ---------- переводы ----------

DASHBOARD_TEXTS = {
    "ru": {
        "html_lang": "ru",
        "login_title": "Вход — Task Reminder",
        "login_heading": "🔐 Вход",
        "password_placeholder": "Пароль",
        "btn_login": "Войти",
        "error_wrong_password": "Неверный пароль",
        "page_title": "Мои задачи",
        "heading": "📋 Мои задачи",
        "heading_chat": "📋 Общие задачи чата",
        "empty": "Задач пока нет.",
        "not_found": "Дашборд не найден. Проверь ссылку.",
        "chart_title": "Выполнено за {days} дн.",
        "streak": "🔥 Серия: {n} дн.",
        "repeat": {
            "none": "",
            "daily": "Каждый день",
            "weekly": "Каждую неделю",
            "monthly": "Каждый месяц",
            "yearly": "Каждый год",
            "weekdays": "По будням",
            "monthly_nth_weekday": "N-й день недели месяца",
        },
        "btn_postpone": "Отложить на день",
        "btn_delete": "Удалить",
        "confirm_delete": "Удалить задачу?",
        "new_title_placeholder": "Новая задача…",
        "btn_add": "Добавить",
        "error_empty_title": "Введите название задачи",
    },
    "en": {
        "html_lang": "en",
        "login_title": "Login — Task Reminder",
        "login_heading": "🔐 Login",
        "password_placeholder": "Password",
        "btn_login": "Log in",
        "error_wrong_password": "Wrong password",
        "page_title": "My Tasks",
        "heading": "📋 My Tasks",
        "heading_chat": "📋 Shared Chat Tasks",
        "empty": "No tasks yet.",
        "not_found": "Dashboard not found. Check the link.",
        "chart_title": "Completed in the last {days} days",
        "streak": "🔥 Streak: {n} days",
        "repeat": {
            "none": "",
            "daily": "Every day",
            "weekly": "Every week",
            "monthly": "Every month",
            "yearly": "Every year",
            "weekdays": "Weekdays",
            "monthly_nth_weekday": "Nth weekday of month",
        },
        "btn_postpone": "Postpone 1 day",
        "btn_delete": "Delete",
        "confirm_delete": "Delete this task?",
        "new_title_placeholder": "New task…",
        "btn_add": "Add",
        "error_empty_title": "Enter a task title",
    },
    "pl": {
        "html_lang": "pl",
        "login_title": "Logowanie — Task Reminder",
        "login_heading": "🔐 Logowanie",
        "password_placeholder": "Hasło",
        "btn_login": "Zaloguj się",
        "error_wrong_password": "Nieprawidłowe hasło",
        "page_title": "Moje zadania",
        "heading": "📋 Moje zadania",
        "heading_chat": "📋 Wspólne zadania czatu",
        "empty": "Brak zadań.",
        "not_found": "Nie znaleziono panelu. Sprawdź link.",
        "chart_title": "Wykonane w ostatnich {days} dniach",
        "streak": "🔥 Seria: {n} dni",
        "repeat": {
            "none": "",
            "daily": "Codziennie",
            "weekly": "Co tydzień",
            "monthly": "Co miesiąc",
            "yearly": "Co rok",
            "weekdays": "W dni robocze",
            "monthly_nth_weekday": "N-ty dzień tygodnia miesiąca",
        },
        "btn_postpone": "Przełóż o dzień",
        "btn_delete": "Usuń",
        "confirm_delete": "Usunąć zadanie?",
        "new_title_placeholder": "Nowe zadanie…",
        "btn_add": "Dodaj",
        "error_empty_title": "Wpisz nazwę zadania",
    },
}


def _dt(lang: str) -> dict:
    return DASHBOARD_TEXTS.get(lang, DASHBOARD_TEXTS["ru"])


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{login_title}</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #0f0f0f; color: #eee;
       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
form {{ background: #1a1a1a; padding: 2rem; border-radius: 12px; width: 280px; }}
input {{ width: 100%; padding: 10px; margin: 8px 0; border-radius: 6px; border: 1px solid #333;
         background: #0f0f0f; color: #eee; box-sizing: border-box; }}
button {{ width: 100%; padding: 10px; border-radius: 6px; border: none; background: #e07a3f;
          color: #fff; font-weight: 600; cursor: pointer; }}
.error {{ color: #e05f5f; font-size: 0.9rem; margin-top: 8px; }}
h2 {{ margin-top: 0; }}
</style></head>
<body>
<form method="post">
<h2>{login_heading}</h2>
<input type="password" name="password" placeholder="{password_placeholder}" autofocus>
<button type="submit">{btn_login}</button>
{error}
</form>
</body></html>"""


TASKS_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #0f0f0f; color: #eee;
       margin: 0; padding: 1.5rem; max-width: 640px; margin: 0 auto; }}
h1 {{ font-size: 1.4rem; }}
.task {{ background: #1a1a1a; border-radius: 10px; padding: 1rem; margin-bottom: 0.75rem;
         border-left: 3px solid #555; display: flex; gap: 0.75rem; align-items: flex-start; }}
.task.done {{ border-left-color: #4a9d5f; opacity: 0.6; }}
.task.overdue {{ border-left-color: #e05f5f; }}
.task form {{ margin: 0; line-height: 0; }}
.check-btn {{ width: 22px; height: 22px; border-radius: 50%; border: 2px solid #666;
              background: transparent; cursor: pointer; flex-shrink: 0; margin-top: 2px; padding: 0; }}
.task.done .check-btn {{ background: #4a9d5f; border-color: #4a9d5f; }}
.task-body {{ flex: 1; min-width: 0; }}
.title {{ font-weight: 600; font-size: 1.05rem; }}
.meta {{ color: #999; font-size: 0.85rem; margin-top: 4px; }}
.task-actions {{ display: flex; gap: 10px; margin-top: 8px; }}
.task-actions button {{ background: none; border: none; color: #888; font-size: 0.8rem;
                         cursor: pointer; padding: 0; }}
.task-actions button:hover {{ color: #e07a3f; }}
.task-actions .del-btn:hover {{ color: #e05f5f; }}
.tag {{ display: inline-block; background: #333; padding: 2px 8px; border-radius: 4px;
        font-size: 0.75rem; margin-top: 6px; }}
.empty {{ color: #777; text-align: center; padding: 3rem 1rem; }}
.chart-block {{ background: #1a1a1a; border-radius: 10px; padding: 1rem; margin-top: 1.5rem; }}
.chart-head {{ display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 4px; }}
.chart-title {{ font-weight: 600; }}
.streak {{ color: #e07a3f; font-size: 0.9rem; }}
.bars {{ display: flex; align-items: flex-end; gap: 4px; height: 120px; margin-top: 12px; }}
.col {{ flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; min-width: 0; }}
.num {{ font-size: 0.7rem; color: #bbb; height: 14px; line-height: 14px; }}
.bar {{ width: 100%; background: #e07a3f; border-radius: 3px 3px 0 0; }}
.bar.zero {{ background: #333; }}
.labels {{ display: flex; gap: 4px; margin-top: 4px; }}
.labels span {{ flex: 1; text-align: center; font-size: 0.65rem; color: #777; }}
.new-task-form {{ display: flex; gap: 8px; margin-bottom: 1rem; }}
.new-task-form input[type=text] {{ flex: 1; min-width: 0; padding: 10px; border-radius: 6px;
    border: 1px solid #333; background: #1a1a1a; color: #eee; box-sizing: border-box; }}
.new-task-form input[type=datetime-local] {{ padding: 10px; border-radius: 6px; border: 1px solid #333;
    background: #1a1a1a; color: #eee; color-scheme: dark; box-sizing: border-box; }}
.new-task-form button {{ padding: 10px 16px; border-radius: 6px; border: none; background: #e07a3f;
    color: #fff; font-weight: 600; cursor: pointer; flex-shrink: 0; }}
.new-task-error {{ color: #e05f5f; font-size: 0.85rem; margin: -0.5rem 0 1rem; }}
@media (max-width: 480px) {{ .new-task-form {{ flex-wrap: wrap; }} .new-task-form button {{ width: 100%; }} }}
</style></head>
<body>
<h1>{heading}</h1>
{new_task_error}
<form class="new-task-form" method="post" action="/dashboard/{token}/tasks/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{new_title_placeholder}" maxlength="200" required>
<input type="datetime-local" name="due_at" required>
<button type="submit">{btn_add}</button>
</form>
{tasks_html}
{chart_html}
</body></html>"""


def _render_task(task: dict, tz, lang: str, token: str, csrf: str) -> str:
    import datetime as dt
    texts = _dt(lang)
    local_due = task["due_at"].astimezone(tz)
    now = dt.datetime.now(tz)
    css_class = "task"
    if task["done"]:
        css_class += " done"
    elif local_due < now and task["repeat"] == "none":
        css_class += " overdue"

    tag_html = f'<div class="tag">🏷 {escape(task["tag"])}</div>' if task.get("tag") else ""
    repeat_label = texts["repeat"].get(task["repeat"], task["repeat"])
    repeat_html = f" · 🔁 {escape(repeat_label)}" if task["repeat"] != "none" and repeat_label else ""
    toggle_action = "undone" if task["done"] else "done"

    extra_actions = ""
    if not task["done"]:
        extra_actions = f"""<div class="task-actions">
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/postpone">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">⏭ {escape(texts["btn_postpone"])}</button>
</form>
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/delete" onsubmit="return confirm('{escape(texts["confirm_delete"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="del-btn">🗑 {escape(texts["btn_delete"])}</button>
</form>
</div>"""

    return f"""<div class="{css_class}">
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/{toggle_action}">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="check-btn" aria-label="toggle"></button>
</form>
<div class="task-body">
<div class="title">{escape(task["title"])}</div>
<div class="meta">📅 {local_due.strftime('%d.%m.%Y %H:%M')}{repeat_html}</div>
{tag_html}
{extra_actions}
</div>
</div>"""


def _render_chart(series: list, streak: int, texts: dict) -> str:
    """Столбчатый график на чистом HTML/CSS: высота столбца в px, без JS."""
    max_px = 90
    peak = max((c for _, c in series), default=0)
    cols, labels = [], []
    for d, c in series:
        px = round(c / peak * max_px) if peak else 0
        zero = " zero" if c == 0 else ""
        cols.append(
            f'<div class="col" title="{d.strftime("%d.%m")}: {c}">'
            f'<div class="num">{c if c else ""}</div><div class="bar{zero}" style="height:{max(px, 2)}px"></div></div>'
        )
        labels.append(f"<span>{d.day}</span>")
    streak_html = f'<div class="streak">{escape(texts["streak"].format(n=streak))}</div>' if streak > 0 else ""
    title = escape(texts["chart_title"].format(days=len(series)))
    return (
        f'<div class="chart-block"><div class="chart-head"><div class="chart-title">{title}</div>{streak_html}</div>'
        f'<div class="bars">{"".join(cols)}</div><div class="labels">{"".join(labels)}</div></div>'
    )


async def _build_chart(owner_id: int, owner_type: str, tz, texts: dict) -> str:
    # личный дашборд — история самого пользователя; групповой — события по общим задачам чата
    user_id, chat_id = (owner_id, None) if owner_type == "user" else (None, owner_id)
    rows = await db.get_history_stats(user_id, CHART_DAYS, tz.key, chat_id=chat_id)
    done_days = await db.get_done_days(user_id, tz.key, chat_id=chat_id)
    today = datetime.now(tz).date()
    series = daily_series(rows, "done", CHART_DAYS, today)
    return _render_chart(series, compute_streak(done_days, today), texts)


async def handle_dashboard(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        # До того, как мы знаем настройки конкретного владельца, язык
        # страницы с ошибкой определить нечем — используем дефолт (ru).
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    cookie = request.cookies.get(f"session_{token}")
    session = _verify_session(cookie) if cookie else None

    if request.method == "POST":
        data = await request.post()
        password = data.get("password", "")
        if await asyncio.to_thread(verify_password, password, settings["dashboard_password_hash"] or ""):
            session_value = _sign_session(owner_id, owner_type, token)
            resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
            # Render терминирует TLS на прокси — схему берём из X-Forwarded-Proto
            is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
            resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                            samesite="Strict", secure=is_https)
            return resp
        error_html = f'<div class="error">{escape(texts["error_wrong_password"])}</div>'
        return aioweb.Response(
            text=LOGIN_PAGE.format(error=error_html, **texts),
            content_type="text/html", status=401,
        )

    if session != (owner_id, owner_type):
        return aioweb.Response(text=LOGIN_PAGE.format(error="", **texts), content_type="text/html")

    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings["timezone"])
    except Exception:
        tz = ZoneInfo("UTC")

    csrf = _csrf_token(cookie)

    tasks = await db.get_tasks(owner_id, owner_type, include_done=True)
    if not tasks:
        tasks_html = f'<div class="empty">{escape(texts["empty"])}</div>'
    else:
        tasks_html = "\n".join(_render_task(t, tz, lang, token, csrf) for t in tasks)

    try:
        chart_html = await _build_chart(owner_id, owner_type, tz, texts)
    except Exception:
        # график — второстепенный блок: сбой БД/статистики не должен ронять весь дашборд
        logger.exception("Не удалось построить график активности owner=%s/%s", owner_type, owner_id)
        chart_html = ""

    heading = texts["heading_chat"] if owner_type == "chat" else texts["heading"]
    new_task_error_html = ""
    if request.query.get("error") == "empty_title":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_empty_title"])}</div>'
    page = TASKS_PAGE.format(
        tasks_html=tasks_html, chart_html=chart_html, heading=heading,
        token=token, csrf=csrf, new_task_error=new_task_error_html,
        **{k: v for k, v in texts.items() if k not in ("heading",)},
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_task_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/new — создание задачи прямо с дашборда.
    Та же защита, что и у остальных действий: сессия + CSRF."""
    token = request.match_info["token"]

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    title = data.get("title", "").strip()
    due_at_raw = data.get("due_at", "")
    if not title:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=empty_title"})

    settings = await db.get_owner_by_dashboard_token(token)
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings["timezone"])
    except Exception:
        tz = ZoneInfo("UTC")

    try:
        # datetime-local отдаёт naive строку вида "2026-10-05T14:30" — трактуем её
        # как локальное время владельца (та же tz, в которой рендерится дашборд).
        due_at = datetime.fromisoformat(due_at_raw).replace(tzinfo=tz)
    except ValueError:
        due_at = datetime.now(tz)

    # repeat/remind — обязательные позиционные параметры add_task; с дашборда задача
    # создаётся без повтора и с дефолтным напоминанием (то же, что ожидает остальной код).
    task_id = await db.add_task(owner_id, owner_type, title, due_at, "none", "on_time")
    await db.log_history(task_id, owner_id, title, "created")

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

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    # Задача должна принадлежать именно этому владельцу — иначе валидный логин в
    # СВОЙ дашборд позволил бы менять задачи по произвольному id из чужого.
    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    if action == "done":
        await db.mark_done(task_id)
        await db.log_history(task_id, owner_id, task["title"], "done")
    elif action == "undone":
        await db.mark_undone(task_id)
        await db.log_history(task_id, owner_id, task["title"], "undone")
    elif action == "postpone":
        from datetime import timedelta
        await db.update_task(task_id, due_at=task["due_at"] + timedelta(days=1))
        await db.log_history(task_id, owner_id, task["title"], "rescheduled")
    elif action == "delete":
        await db.delete_task(task_id)
        await db.log_history(task_id, owner_id, task["title"], "deleted")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


def register_dashboard_routes(app: aioweb.Application):
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}/tasks/new", handle_task_create)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/{action}", handle_task_toggle)
