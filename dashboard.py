"""Веб-дашборд: простая HTML-страница со списком задач пользователя.
Доступ по уникальному токену в URL (/dashboard/<token>) + пароль в форме логина.
Сессия хранится в подписанной cookie (без внешних зависимостей для сессий —
просто HMAC поверх user_id + expiry, валидируется на каждый запрос)."""

import hashlib
import hmac
import os
import time
from html import escape

from aiohttp import web as aioweb

import db

SESSION_SECRET = os.environ.get("BOT_TOKEN", "fallback-secret")  # используем токен бота как секрет для HMAC
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # неделя


def _sign_session(user_id: int, token: str) -> str:
    payload = f"{user_id}:{int(time.time()) + SESSION_MAX_AGE}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session(cookie_value: str, expected_token: str) -> int | None:
    try:
        user_id_str, expiry_str, sig = cookie_value.split(":")
        payload = f"{user_id_str}:{expiry_str}"
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expiry_str) < time.time():
            return None
        return int(user_id_str)
    except (ValueError, AttributeError):
        return None


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Task Reminder</title>
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
<h2>🔐 Вход</h2>
<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">Войти</button>
{error}
</form>
</body></html>"""


TASKS_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мои задачи</title>
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
<h1>📋 Мои задачи</h1>
{tasks_html}
</body></html>"""


def _render_task(task: dict, tz) -> str:
    import datetime as dt
    local_due = task["due_at"].astimezone(tz)
    now = dt.datetime.now(tz)
    css_class = "task"
    if task["done"]:
        css_class += " done"
    elif local_due < now:
        css_class += " overdue"

    tag_html = f'<div class="tag">🏷 {escape(task["tag"])}</div>' if task.get("tag") else ""
    repeat_html = f' · 🔁 {escape(task["repeat"])}' if task["repeat"] != "none" else ""

    return f"""<div class="{css_class}">
<div class="title">{"✅ " if task["done"] else ""}{escape(task["title"])}</div>
<div class="meta">📅 {local_due.strftime('%d.%m.%Y %H:%M')}{repeat_html}</div>
{tag_html}
</div>"""


async def handle_dashboard(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    settings = await db.get_user_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(text="Дашборд не найден. Проверь ссылку.", status=404)

    cookie = request.cookies.get(f"session_{token}")
    user_id = _verify_session(cookie, token) if cookie else None

    if request.method == "POST":
        data = await request.post()
        password = data.get("password", "")
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if hmac.compare_digest(password_hash, settings["dashboard_password_hash"] or ""):
            session_value = _sign_session(settings["user_id"], token)
            resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
            resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True, samesite="Strict")
            return resp
        return aioweb.Response(text=LOGIN_PAGE.format(error='<div class="error">Неверный пароль</div>'),
                                content_type="text/html", status=401)

    if user_id != settings["user_id"]:
        return aioweb.Response(text=LOGIN_PAGE.format(error=""), content_type="text/html")

    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings["timezone"])
    except Exception:
        tz = ZoneInfo("UTC")

    tasks = await db.get_tasks(user_id, include_done=True)
    if not tasks:
        tasks_html = '<div class="empty">Задач пока нет.</div>'
    else:
        tasks_html = "\n".join(_render_task(t, tz) for t in tasks)

    return aioweb.Response(text=TASKS_PAGE.format(tasks_html=tasks_html), content_type="text/html")


def register_dashboard_routes(app: aioweb.Application):
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}", handle_dashboard)
