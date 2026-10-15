"""Тесты-инварианты: проверяют правило сразу для ВСЕХ маршрутов/хендлеров,
поэтому новый код попадает под защиту без отдельного теста. Сознательно не
смотрят на HTML и детали реализации — только на контракт."""

import inspect
import re

import pytest
from aiohttp import web

import db
import dashboard
from tests.dashboard_stubs import SHARE_CODE
from tests.test_dashboard_web import TOKEN, _login, client, settings, spy, task  # noqa: F401 — фикстуры

# Маршруты POST, которым CSRF не нужен по замыслу: вход паролем и вход из Mini App
# (сессии ещё нет, защита — пароль / подпись initData).
_NO_CSRF = {"/dashboard/{token}", "/dashboard/webapp-auth"}
_WRITE_PREFIXES = ("add_", "delete_", "mark_", "update_", "set_", "toggle_", "log_", "upsert_", "purge_", "accept_")


def _post_paths():
    app = web.Application()
    dashboard.register_dashboard_routes(app)
    fill = {"token": TOKEN, "code": SHARE_CODE, "action": "done"}
    for route in app.router.routes():
        if route.method != "POST":
            continue
        tpl = route.resource.canonical
        if tpl in _NO_CSRF:
            continue
        yield re.sub(r"\{(\w+)\}", lambda m: fill.get(m.group(1), "1"), tpl)


@pytest.mark.parametrize("path", list(_post_paths()))
async def test_every_post_rejects_missing_csrf(client, monkeypatch, path):  # noqa: F811
    await _login(client)
    writes = []
    for name in dir(db):
        if name.startswith(_WRITE_PREFIXES) and callable(getattr(db, name)):
            async def _rec(*a, _n=name, **kw):
                writes.append(_n)
            monkeypatch.setattr(db, name, _rec)
    resp = await client.post(path, data={}, allow_redirects=False)
    assert 400 <= resp.status < 500, f"{path} принял POST без CSRF: {resp.status}"
    assert not writes, f"{path} записал в БД без CSRF: {writes}"


def _state_message_handlers():
    from tgbot.handlers import (add_task, basic, dashboard_cmd, find, photos, subtasks,
                                task_actions, templates)
    for module in (add_task, basic, dashboard_cmd, find, photos, subtasks, task_actions, templates):
        for h in module.router.message.handlers:
            kinds = [type(f.callback).__name__ for f in h.filters or []]
            if "State" in kinds:
                yield pytest.param(h, kinds, id=h.callback.__name__)


@pytest.mark.parametrize("handler,kinds", list(_state_message_handlers()))
def test_state_handlers_survive_non_text_messages(handler, kinds):
    """Стикер/фото/голос посреди диалога: message.text is None. Хендлер либо
    фильтрует контент (F.text / F.photo / Command), либо не трогает
    message.text без защиты `message.text or ...`."""
    has_content_filter = "Command" in kinds or any(getattr(f, "magic", None) is not None for f in handler.filters)
    if has_content_filter:
        return
    src = inspect.getsource(handler.callback)
    assert not re.search(r"message\.text(?!\s+or\b)[.)\]]", src), \
        f"{handler.callback.__name__} обращается к message.text без F.text-фильтра"
