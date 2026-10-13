"""Новый вид дашборда — в настоящем браузере (headless Chromium): шторка
«Производительность», свайп по карточке на телефоне, эффекты ПК (подсветка сетки,
наклон, волна, конфетти, палитра команд).

Не входит в обычный прогон (маркер e2e). Запуск: python -m pytest -m e2e -s
"""

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from tests.e2e.test_ajax_actions import _open, env  # noqa: E402,F401

pytestmark = pytest.mark.e2e


async def test_phone_perf_sheet_switches_to_minimal_level(env):
    async with async_playwright() as pw:
        browser, page, _nav = await _open(pw, env, "phone")
        try:
            await page.click("#perf-open")
            await page.wait_for_function("document.getElementById('perf-back').classList.contains('open')")
            await page.click('.perf-lv [data-l="min"]')
            await page.wait_for_load_state("load")
            # уровень сохранён и применён детектором после перезагрузки: без mid/fx, с .min
            await page.wait_for_function("document.documentElement.classList.contains('min')")
            assert await page.evaluate("localStorage.getItem('perf')") == "min"
            assert not await page.evaluate("document.documentElement.classList.contains('mid')")
            # в «Минимуме» ни одного перехода
            assert await page.evaluate("getComputedStyle(document.querySelector('.task')).transitionDuration") in ("0s", "0s, 0s")
        finally:
            await browser.close()


async def test_phone_swipe_right_completes_task(env):
    async with async_playwright() as pw:
        browser, page, _nav = await _open(pw, env, "phone")
        try:
            cdp = await page.context.new_cdp_session(page)
            box = await page.locator("#task-1 .task-summary").bounding_box()
            x, y = box["x"] + 40, box["y"] + box["height"] / 2
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
            for k in range(1, 12):
                await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + k * 14, "y": y}]})
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await page.wait_for_function("document.querySelector('#task-1').classList.contains('done')")
            # слишком короткий жест не выполняет задачу
            box = await page.locator("#task-2 .task-summary").bounding_box()
            x, y = box["x"] + 40, box["y"] + box["height"] / 2
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
            for k in range(1, 5):
                await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + k * 8, "y": y}]})
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await page.wait_for_timeout(500)
            assert not await page.evaluate("document.querySelector('#task-2').classList.contains('done')")
        finally:
            await browser.close()


async def test_desktop_effects_without_page_errors(env):
    async with async_playwright() as pw:
        browser, page, _nav = await _open(pw, env, "desktop")
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.wait_for_function("document.documentElement.classList.contains('fx')")
            # заголовок виден (класс .sp у splitTitle не должен пересекаться с классами эффектов)
            title = await page.evaluate("(() => { const t = document.querySelector('.large-title'); return [getComputedStyle(t).position, t.getBoundingClientRect().height]; })()")
            assert title[0] != "absolute" and 0 < title[1] < 200
            # подсветка сетки и наклон карточки следуют за мышью
            box = await page.locator("#task-2").bounding_box()
            for i in range(8):
                await page.mouse.move(box["x"] + 200 + i * 12, box["y"] + box["height"] * 0.4)
                await page.wait_for_timeout(20)
            await page.wait_for_timeout(300)
            assert await page.evaluate("!!document.querySelector('.fx-hot.on')")
            assert "perspective" in await page.evaluate("document.querySelector('#task-2').style.transform")
            # палитра команд
            await page.keyboard.press("Control+k")
            await page.wait_for_selector(".fx-pal-back.open")
            await page.keyboard.type("втор")
            assert await page.evaluate("document.querySelectorAll('.fx-pal-i').length") >= 1
            await page.keyboard.press("Escape")
            # выполнение: волна по карточке; при прогрессе 100 — конфетти
            await page.evaluate("document.querySelector('.stat-today').style.setProperty('--p', '100')")
            await page.click("#task-1 .check-btn")
            await page.wait_for_selector(".fx-wave", state="attached")
            await page.wait_for_selector("canvas", state="attached", timeout=3000)
            assert not errors
        finally:
            await browser.close()
