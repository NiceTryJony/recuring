"""Niezawodność: alerty do admina, /health/deep, eksport JSON. Sprawdzają kontrakt
(co dociera do admina, jaki status HTTP), nie szczegóły implementacji."""

import asyncio
import logging

import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

import db
from tgbot import alerts, app, server


def _logger_with_handler(sent):
    async def send(text):
        sent.append(text)
    handler = alerts.AdminAlertHandler(send)
    log = logging.getLogger("test-alerts-" + str(id(sent)))
    log.propagate = False
    log.addHandler(handler)
    return log


async def test_error_log_reaches_admin_but_info_and_repeats_do_not():
    sent = []
    log = _logger_with_handler(sent)
    log.info("zwykła informacja")
    log.error("awaria A")
    log.error("awaria A")                       # ten sam błąd — nie spamujemy
    await asyncio.sleep(0)
    assert len(sent) == 1 and "awaria A" in sent[0]


async def test_alert_burst_is_capped():
    sent = []
    log = _logger_with_handler(sent)
    for i in range(20):
        log.error("inna awaria %s", i)
    await asyncio.sleep(0)
    assert len(sent) == alerts.ALERT_BURST


async def test_exception_traceback_is_included():
    sent = []
    log = _logger_with_handler(sent)
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("dzielenie")
    await asyncio.sleep(0)
    assert "ZeroDivisionError" in sent[0]


def test_error_without_running_loop_is_ignored_not_crashing():
    sent = []
    log = _logger_with_handler(sent)
    log.error("poza pętlą zdarzeń")             # brak event loop — nic się nie stanie
    assert sent == []


def test_install_is_noop_without_admin_id():
    assert alerts.install(object(), None) is None


@pytest.mark.parametrize("db_ok,scheduler_running,status", [(True, True, 200), (False, True, 503), (True, False, 503)])
async def test_health_deep_reports_failures(monkeypatch, db_ok, scheduler_running, status):
    async def ping():
        if not db_ok:
            raise ConnectionError("baza leży")
    monkeypatch.setattr(db, "ping", ping)
    monkeypatch.setattr(app.scheduler, "get_jobs", lambda: [], raising=False)
    monkeypatch.setattr(type(app.scheduler), "running", property(lambda self: scheduler_running))
    web_app = aioweb.Application()
    web_app.router.add_get("/health/deep", server.health_deep)
    async with TestClient(TestServer(web_app)) as client:
        resp = await client.get("/health/deep")
        assert resp.status == status
        body = await resp.json()
        assert body["db"] is db_ok and body["scheduler"] is scheduler_running
