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

import hashlib
import hmac
import os
import time
from html import escape

from aiohttp import web as aioweb

import db

SESSION_SECRET = os.environ.get("BOT_TOKEN", "fallback-secret")  # используем токен бота как секрет для HMAC
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # неделя


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
        "repeat": {
            "none": "",
            "daily": "Каждый день",
            "weekly": "Каждую неделю",
            "monthly": "Каждый месяц",
            "yearly": "Каждый год",
            "weekdays": "По будням",
            "monthly_nth_weekday": "N-й день недели месяца",
        },
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
        "repeat": {
            "none": "",
            "daily": "Every day",
            "weekly": "Every week",
            "monthly": "Every month",
            "yearly": "Every year",
            "weekdays": "Weekdays",
            "monthly_nth_weekday": "Nth weekday of month",
        },
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
        "repeat": {
            "none": "",
            "daily": "Codziennie",
            "weekly": "Co tydzień",
            "monthly": "Co miesiąc",
            "yearly": "Co rok",
            "weekdays": "W dni robocze",
            "monthly_nth_weekday": "N-ty dzień tygodnia miesiąca",
        },
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
         border-left: 3px solid #555; }}
.task.done {{ border-left-color: #4a9d5f; opacity: 0.6; }}
.task.overdue {{ border-left-color: #e05f5f; }}
.title {{ font-weight: 600; font-size: 1.05rem; }}
.meta {{ color: #999; font-size: 0.85rem; margin-top: 4px; }}
.tag {{ display: inline-block; background: #333; padding: 2px 8px; border-radius: 4px;
        font-size: 0.75rem; margin-top: 6px; }}
.empty {{ color: #777; text-align: center; padding: 3rem 1rem; }}
</style></head>
<body>
<h1>{heading}</h1>
{tasks_html}
</body></html>"""


def _render_task(task: dict, tz, lang: str) -> str:
    import datetime as dt
    texts = _dt(lang)
    local_due = task["due_at"].astimezone(tz)
    now = dt.datetime.now(tz)
    css_class = "task"
    if task["done"]:
        css_class += " done"
    elif local_due < now:
        css_class += " overdue"

    tag_html = f'<div class="tag">🏷 {escape(task["tag"])}</div>' if task.get("tag") else ""
    repeat_label = texts["repeat"].get(task["repeat"], task["repeat"])
    repeat_html = f" · 🔁 {escape(repeat_label)}" if task["repeat"] != "none" and repeat_label else ""

    return f"""<div class="{css_class}">
<div class="title">{"✅ " if task["done"] else ""}{escape(task["title"])}</div>
<div class="meta">📅 {local_due.strftime('%d.%m.%Y %H:%M')}{repeat_html}</div>
{tag_html}
</div>"""


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
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if hmac.compare_digest(password_hash, settings["dashboard_password_hash"] or ""):
            session_value = _sign_session(owner_id, owner_type, token)
            resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
            resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True, samesite="Strict")
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

    tasks = await db.get_tasks(owner_id, owner_type, include_done=True)
    if not tasks:
        tasks_html = f'<div class="empty">{escape(texts["empty"])}</div>'
    else:
        tasks_html = "\n".join(_render_task(t, tz, lang) for t in tasks)

    heading = texts["heading_chat"] if owner_type == "chat" else texts["heading"]
    page = TASKS_PAGE.format(tasks_html=tasks_html, heading=heading, **{k: v for k, v in texts.items() if k not in ("heading",)})
    return aioweb.Response(text=page, content_type="text/html")


def register_dashboard_routes(app: aioweb.Application):
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}", handle_dashboard)
