"""Действия дашборда без перезагрузки — в настоящем браузере (headless Chromium).

Не входит в обычный прогон (маркер e2e исключён в pytest.ini). Запуск:

    python -m playwright install chromium        # один раз
    python -m pytest -m e2e -s

Реальное aiohttp-приложение с изменяемой заглушкой БД: действие должно
отработать без единой навигации главного фрейма, карточки — переехать на место,
сбой сервера — откатить оптимистичное состояние и показать тост.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright.async_api")
from aiohttp import web as aioweb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import dashboard  # noqa: E402
import db  # noqa: E402
from tests.dashboard_stubs import install_db_stubs  # noqa: E402

pytestmark = pytest.mark.e2e

UTC = ZoneInfo("UTC")
TOKEN = "e2e-token"
OWNER_ID = -100900

PROFILES = {
    "desktop": {"viewport": {"width": 1280, "height": 900}, "mobile": False},
    "phone": {"viewport": {"width": 390, "height": 844}, "mobile": True},
}


def _task(task_id: int, days: int, title: str) -> dict:
    return {
        "id": task_id, "owner_id": OWNER_ID, "owner_type": "chat", "title": title,
        "due_at": datetime.now(UTC) + timedelta(days=days), "repeat": "none", "remind": "on_time",
        "done": False, "tag": None, "created_by": None, "description": None, "remind_until_done": False,
    }


@pytest.fixture
async def env(monkeypatch):
    tasks = [_task(1, 1, "первая"), _task(2, 2, "вторая"), _task(3, 3, "третья")]
    settings = {
        "owner_id": OWNER_ID, "owner_type": "chat", "language": "ru", "timezone": "Europe/Warsaw",
        "dashboard_password_hash": dashboard.hash_password("pw"), "dashboard_password_version": 1,
    }
    install_db_stubs(monkeypatch, TOKEN, settings, tasks)

    async def get_tasks(*a, **kw):
        return sorted(tasks, key=lambda t: (t["done"], t["due_at"]))

    async def mark_done(task_id):
        next(t for t in tasks if t["id"] == task_id)["done"] = True

    async def mark_undone(task_id):
        next(t for t in tasks if t["id"] == task_id)["done"] = False

    async def delete_task(task_id):
        tasks[:] = [t for t in tasks if t["id"] != task_id]

    async def add_task(owner_id, owner_type, title, due_at, repeat, remind, **kw):
        new_id = max(t["id"] for t in tasks) + 1
        tasks.append({**_task(new_id, 0, title), "due_at": due_at, "description": kw.get("description")})
        return new_id

    subs: list[dict] = []

    async def add_subtask(task_id, title):
        subs.append({"id": len(subs) + 1, "task_id": task_id, "title": title, "done": False, "position": len(subs)})
        return len(subs)

    async def get_subtasks(task_id):
        return [x for x in subs if x["task_id"] == task_id]

    async def get_subtasks_bulk(ids):
        return {i: [x for x in subs if x["task_id"] == i] for i in ids if any(x["task_id"] == i for x in subs)}

    async def get_subtask(subtask_id):
        return next((x for x in subs if x["id"] == subtask_id), None)

    async def toggle_subtask(subtask_id):
        sub = next(x for x in subs if x["id"] == subtask_id)
        sub["done"] = not sub["done"]
        return sub["done"]

    async def update_task(task_id, **fields):
        next(t for t in tasks if t["id"] == task_id).update(fields)

    for name, impl in {"add_subtask": add_subtask, "get_subtasks": get_subtasks, "get_subtasks_bulk": get_subtasks_bulk,
                       "get_subtask": get_subtask, "toggle_subtask": toggle_subtask, "update_task": update_task,
                       "get_tasks": get_tasks, "mark_done": mark_done, "mark_undone": mark_undone,
                       "delete_task": delete_task, "add_task": add_task}.items():
        monkeypatch.setattr(db, name, impl)

    app = aioweb.Application()
    dashboard.register_dashboard_routes(app)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    cookie = dashboard._sign_session(OWNER_ID, "chat", password_version=1)
    yield client, cookie, monkeypatch
    await client.close()


async def _open(pw, env, profile: str, *, javascript: bool = True, path: str = "?view=all"):
    """По умолчанию — вкладка «Все»: на «Ближайших» (вкладка по умолчанию) выполненная
    задача исчезает из вида, а тесты проверяют именно поведение карточки."""
    client, cookie, _ = env
    spec = PROFILES[profile]
    base = str(client.make_url("")).rstrip("/")
    browser = await pw.chromium.launch()
    context = await browser.new_context(
        viewport=spec["viewport"], is_mobile=spec["mobile"], has_touch=spec["mobile"],
        java_script_enabled=javascript,
    )
    await context.add_cookies([{"name": f"session_{TOKEN}", "value": cookie, "url": base}])
    page = await context.new_page()
    # Внешний скрипт Telegram недоступен в тесте и не нужен.
    await page.route("**/*", lambda route: route.abort()
                     if not route.request.url.startswith(base) else route.continue_())
    navigations: list[str] = []
    page.on("framenavigated", lambda f: navigations.append(f.url) if f == page.main_frame else None)
    await page.goto(f"{base}/dashboard/{TOKEN}{path}")
    return browser, page, navigations


# Карточки в порядке на экране — и в плоском списке, и внутри групп по дате
ORDER = "[...document.querySelectorAll('.list .task')].map(e => e.id).join(',')"
DONE = "document.querySelector('#task-1').classList.contains('done')"


@pytest.mark.parametrize("profile", list(PROFILES))
async def test_actions_do_not_reload_the_page(env, profile):
    async with async_playwright() as pw:
        browser, page, nav = await _open(pw, env, profile)
        try:
            base_nav = len(nav)
            assert await page.evaluate(ORDER) == "task-1,task-2,task-3"
            assert await page.evaluate("document.querySelectorAll('.list .task-group').length") == 3  # по дню на задачу

            # выполнить: в группировке по дате карточка отмечается и остаётся в своём дне
            await page.click("#task-1 .check-btn")
            await page.wait_for_function(DONE)
            assert await page.evaluate(ORDER) == "task-1,task-2,task-3"

            # вернуть
            await page.click("#task-1 .check-btn")
            await page.wait_for_function(f"!{DONE}")

            # создать: шторка закрывается, карточка встаёт в новую группу своего дня — последней
            await page.click(".fab")
            await page.fill(".add-sheet input[name=title]", "новая из браузера")
            await page.fill(".add-sheet input[name=due_at]", "2031-01-01T10:00")
            await page.click(".add-sheet button[type=submit]")
            await page.wait_for_function(f"{ORDER}.split(',').length === 4")
            assert "новая из браузера" in await page.inner_text(".list .task-group:last-of-type .task")
            assert await page.inner_text(".list .task-group:last-of-type .group-title") == "01.01 1"
            assert not await page.evaluate("document.querySelector('.add-sheet').open")

            # удалить: без confirm(), карточка прячется сразу, «Отменить» возвращает её без запроса
            dialogs, deletes = [], []
            page.on("dialog", lambda d: (dialogs.append(d), d.dismiss()))
            page.on("request", lambda r: deletes.append(r.url) if r.url.endswith("/tasks/2/delete") else None)
            await page.click("#task-2 .task-summary")
            await page.click("#task-2 .del-btn")
            await page.wait_for_selector("#task-2", state="hidden")
            await page.click(".toast .toast-btn")
            await page.wait_for_selector("#task-2", state="visible")
            await page.wait_for_timeout(5500)
            assert not deletes, "после «Отменить» запрос на удаление уходить не должен"
            # без отмены — через несколько секунд уходит POST, а с ним и опустевшая группа дня
            await page.click("#task-2 .del-btn")
            await page.wait_for_function("!document.getElementById('task-2')", timeout=10000)
            assert not dialogs
            await page.wait_for_function("document.querySelectorAll('.list .task-group').length === 3")

            assert len(nav) == base_nav, f"страница перезагружалась: {nav[base_nav:]}"
        finally:
            await browser.close()


async def test_failed_action_rolls_back_and_shows_toast(env):
    _, _, monkeypatch = env

    async def boom(task_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "mark_done", boom)
    async with async_playwright() as pw:
        browser, page, nav = await _open(pw, env, "phone")
        try:
            base_nav = len(nav)
            await page.click("#task-1 .check-btn")
            await page.wait_for_selector(".toast")
            assert not await page.evaluate("document.querySelector('#task-1').classList.contains('done')")
            assert len(nav) == base_nav
        finally:
            await browser.close()


async def test_without_javascript_forms_still_work(env):
    async with async_playwright() as pw:
        browser, page, nav = await _open(pw, env, "phone", javascript=False)
        try:
            base_nav = len(nav)
            await page.click("#task-1 .check-btn", force=True)   # входная CSS-анимация карточки не даёт «стабильности»
            await page.wait_for_function("document.querySelector('#task-1').classList.contains('done')")
            assert len(nav) > base_nav     # без JS — обычная отправка формы и редирект
        finally:
            await browser.close()


async def test_subtasks_and_description_update_in_place(env):
    async with async_playwright() as pw:
        browser, page, nav = await _open(pw, env, "phone")
        try:
            base_nav = len(nav)
            await page.click("#task-1 .task-summary")

            await page.fill("#task-1 .subtask-add-form input[name=title]", "шаг один")
            await page.click("#task-1 .subtask-add-form button[type=submit]")
            await page.wait_for_selector("#task-1 .subtask-row")
            await page.click("#task-1 .subtask-check")
            await page.wait_for_selector("#task-1 .subtask-row.done")

            await page.click("#task-1 .edit-toggle summary")
            await page.fill("#task-1 .desc-form textarea", "подробности")
            await page.click("#task-1 .desc-form button[type=submit]")
            await page.wait_for_selector("#task-1 .description-text")
            assert await page.inner_text("#task-1 .description-text") == "подробности"

            assert len(nav) == base_nav
        finally:
            await browser.close()
