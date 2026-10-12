"""Сквозные тесты веб-слоя дашборда: реальное aiohttp-приложение с маршрутами
из register_dashboard_routes, но с БД-заглушкой (db.* подменяется monkeypatch'ем).
Проверяют то, что юнит-тестами не ловится: формы, редиректы, cookie, 403/404 и
синхронизацию с планировщиком.
"""

import io
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

import dashboard
import db
from tests.dashboard_stubs import SHARE_CODE, install_db_stubs, make_spy

UTC = ZoneInfo("UTC")
TOKEN = "tok-abc"
OWNER_ID = -100500


@pytest.fixture
def settings():
    return {
        "owner_id": OWNER_ID, "owner_type": "chat", "language": "ru",
        "timezone": "Europe/Warsaw",
        "dashboard_password_hash": dashboard.hash_password("pw"),
        "dashboard_password_version": 3,
    }


@pytest.fixture
def task():
    return {
        "id": 1, "owner_id": OWNER_ID, "owner_type": "chat", "title": "общая задача",
        "due_at": datetime.now(UTC) + timedelta(days=1), "repeat": "none", "remind": "on_time",
        "done": False, "tag": None, "created_by": 777, "description": None,
        "remind_until_done": False,
    }


@pytest.fixture
def spy():
    """Счётчики обращений к БД и планировщику — ими проверяем, что страница
    берёт фото/подзадачи батчем, а действия доходят до планировщика."""
    return make_spy()


@pytest_asyncio.fixture
async def client(monkeypatch, settings, task, spy):
    install_db_stubs(monkeypatch, TOKEN, settings, [task], spy=spy)

    dashboard.set_scheduler_hooks(
        schedule=lambda task_id, *a, **kw: spy["scheduled"].append(task_id),
        unschedule=lambda task_id: spy["unscheduled"].append(task_id),
    )

    app = aioweb.Application()
    dashboard.register_dashboard_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()
    dashboard.set_scheduler_hooks(None, None)


async def _login(client) -> str:
    """Логинится паролем и возвращает CSRF-токен со страницы задач."""
    resp = await client.post(f"/dashboard/{TOKEN}", data={"password": "pw"}, allow_redirects=False)
    assert resp.status == 302
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    return re.search(r'name="csrf" value="([0-9a-f]+)"', body).group(1)


# ---------- доступ ----------

async def test_unknown_token_is_404(client):
    assert (await client.get("/dashboard/nope")).status == 404


@pytest.mark.parametrize("path", ["", "/history", "/templates"])
async def test_login_form_on_every_page_posts_to_dashboard_root(client, path):
    """Регресс: у формы входа не было action, а POST-маршрута на /history и
    /templates нет — ввод пароля на этих страницах давал 405 вместо входа."""
    resp = await client.get(f"/dashboard/{TOKEN}{path}")
    assert resp.status == 200
    assert f'action="/dashboard/{TOKEN}"' in await resp.text()


async def test_wrong_password_is_401_and_right_one_sets_cookie(client):
    resp = await client.post(f"/dashboard/{TOKEN}", data={"password": "wrong"}, allow_redirects=False)
    assert resp.status == 401
    resp = await client.post(f"/dashboard/{TOKEN}", data={"password": "pw"}, allow_redirects=False)
    assert resp.status == 302 and f"session_{TOKEN}" in resp.cookies


@pytest.mark.parametrize("path", ["", "/history", "/templates"])
async def test_pages_render_for_logged_in_owner(client, path):
    await _login(client)
    assert (await client.get(f"/dashboard/{TOKEN}{path}")).status == 200


@pytest.mark.parametrize("path", ["", "/history", "/templates"])
async def test_password_change_invalidates_session_everywhere(client, settings, path):
    """Регресс: /history не проверяла dashboard_password_version, и утёкшая
    cookie продолжала читать ленту событий после смены пароля."""
    await _login(client)
    settings["dashboard_password_version"] += 1
    body = await (await client.get(f"/dashboard/{TOKEN}{path}")).text()
    assert "login-form" in body


async def test_password_change_blocks_actions(client, settings):
    csrf = await _login(client)
    settings["dashboard_password_version"] += 1
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/1/done", data={"csrf": csrf}, allow_redirects=False)
    assert resp.status == 403


# ---------- CSRF и действия ----------

async def test_action_without_valid_csrf_is_403(client):
    await _login(client)
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/1/done", data={"csrf": "bad"}, allow_redirects=False)
    assert resp.status == 403


async def test_action_on_foreign_task_is_404(client):
    csrf = await _login(client)
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/999/done", data={"csrf": csrf}, allow_redirects=False)
    assert resp.status == 404


# ---------- синхронизация с планировщиком ----------

async def test_done_removes_scheduler_jobs(client, spy):
    """Регресс: выполненная с дашборда задача продолжала напоминать о себе."""
    csrf = await _login(client)
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/1/done", data={"csrf": csrf}, allow_redirects=False)
    assert resp.status == 302
    assert spy["unscheduled"] == [1]


async def test_delete_removes_scheduler_jobs(client, spy):
    csrf = await _login(client)
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/1/delete", data={"csrf": csrf}, allow_redirects=False)
    assert resp.status == 302
    assert spy["unscheduled"] == [1]


async def test_new_task_is_scheduled_immediately(client, spy):
    """Регресс: задача с дашборда не попадала в APScheduler, и напоминание по
    ней не приходило до перезапуска процесса."""
    csrf = await _login(client)
    resp = await client.post(
        f"/dashboard/{TOKEN}/tasks/new",
        data={"csrf": csrf, "title": "новая", "due_at": "2027-01-02T08:15"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert spy["scheduled"] == [42]
    due = spy["added"][0]["due_at"]
    assert (due.hour, due.minute) == (8, 15)
    assert due.tzinfo.key == "Europe/Warsaw"   # время понято как локальное для владельца


async def test_unparseable_due_at_shows_error_instead_of_now(client, spy):
    """Регресс: битая дата молча превращалась в "сейчас"."""
    csrf = await _login(client)
    resp = await client.post(
        f"/dashboard/{TOKEN}/tasks/new",
        data={"csrf": csrf, "title": "x", "due_at": "05.10.2026 14:30"},
        allow_redirects=False,
    )
    assert resp.status == 302 and resp.headers["Location"].endswith("error=bad_due_at")
    assert spy["added"] == []
    body = await (await client.get(f"/dashboard/{TOKEN}?error=bad_due_at")).text()
    assert "разобрать дату" in body


async def test_empty_title_is_rejected(client, spy):
    csrf = await _login(client)
    resp = await client.post(f"/dashboard/{TOKEN}/tasks/new",
                             data={"csrf": csrf, "title": "   "}, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"].endswith("error=empty_title")
    assert spy["added"] == []


# ---------- оптимизация выборок ----------

async def test_tasks_page_uses_bulk_queries(client, spy):
    """Фото и подзадачи берутся одним запросом на страницу, а не на задачу."""
    await _login(client)  # сам делает один GET страницы — считаем со следующего
    spy["photos_bulk"] = spy["subtasks_bulk"] = 0
    resp = await client.get(f"/dashboard/{TOKEN}")
    assert resp.status == 200 and "общая задача" in await resp.text()
    assert (spy["photos_bulk"], spy["subtasks_bulk"]) == (1, 1)
    assert (spy["photos_single"], spy["subtasks_single"]) == (0, 0)


# ---------- приватные картинки ----------

async def test_avatar_is_not_publicly_cacheable(client, monkeypatch):
    async def _photo(user_id):
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr(db, "get_telegram_user_photo", _photo)
    resp = await client.get(f"/dashboard/{TOKEN}/avatar/777")
    assert resp.status == 200
    # Доступ держится на секретности токена — промежуточный прокси не должен
    # кешировать картинку и отдавать её кому-то ещё.
    assert resp.headers["Cache-Control"].startswith("private")


# ---------- вес страницы: сжатие и внешний CSS ----------

async def test_html_is_gzipped_for_clients_that_accept_it(client):
    await _login(client)
    resp = await client.get(f"/dashboard/{TOKEN}", headers={"Accept-Encoding": "gzip, deflate"})
    assert resp.status == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in resp.headers["Vary"]
    assert "задача" in await resp.text()      # тело распаковывается корректно


async def test_html_is_not_compressed_for_clients_without_support(client):
    await _login(client)
    resp = await client.get(f"/dashboard/{TOKEN}", headers={"Accept-Encoding": "identity"})
    assert resp.status == 200
    assert "Content-Encoding" not in resp.headers


async def test_css_is_external_and_not_inlined(client):
    """CSS больше не инлайнится в каждый HTML — он отдельным файлом с хешем."""
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert "<style>" not in body
    links = re.findall(r'href="(/dashboard/static/[^"]+\.css)"', body)
    assert len(links) == 2            # общая база + стили страницы задач


async def test_static_css_is_immutable_and_revalidates_with_etag(client):
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    url = re.search(r'href="(/dashboard/static/[^"]+\.css)"', body).group(1)

    resp = await client.get(url)
    assert resp.status == 200
    assert resp.content_type == "text/css"
    cache = resp.headers["Cache-Control"]
    assert "immutable" in cache and "max-age=31536000" in cache
    css = await resp.text()
    assert "/*" not in css            # комментарии вырезаны минификатором
    assert ".task" in css

    etag = resp.headers["ETag"]
    again = await client.get(url, headers={"If-None-Match": etag})
    assert again.status == 304
    assert await again.read() == b""


async def test_unknown_static_file_is_404(client):
    assert (await client.get("/dashboard/static/nope.css")).status == 404


async def test_static_route_does_not_shadow_dashboard_token(client):
    """Путь статики по форме совпадает с /dashboard/{token}/... — убеждаемся,
    что обычные страницы дашборда по-прежнему доступны."""
    await _login(client)
    assert (await client.get(f"/dashboard/{TOKEN}")).status == 200
    assert (await client.get(f"/dashboard/{TOKEN}/templates")).status == 200


async def test_no_third_party_fonts(client):
    """Шрифт системный: ни одного стороннего запроса перед первой отрисовкой."""
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert "fonts.googleapis" not in body and "fonts.gstatic" not in body
    assert 'content="light dark"' in body


# ---------- фото: превью вместо полного кадра ----------

def _jpeg(side: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (side, side), (120, 90, 60)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def photo_rows(monkeypatch, spy):
    """Одно фото в БД: полный кадр 1280px и ещё не сделанное превью."""
    full = _jpeg(1280)
    row = {"id": 7, "task_id": 1, "data": full, "mime": "image/jpeg", "thumb": None}
    spy["thumb_saved"] = []

    async def _get_photo(photo_id):
        return row if photo_id == row["id"] else None

    async def _get_thumb(photo_id):
        return (row["thumb"], row["task_id"]) if photo_id == row["id"] else None

    async def _set_thumb(photo_id, thumb):
        row["thumb"] = thumb
        spy["thumb_saved"].append(photo_id)

    async def _photos_bulk(ids):
        return {1: [{"id": row["id"], "mime": "image/jpeg"}]}

    monkeypatch.setattr(db, "get_task_photo", _get_photo)
    monkeypatch.setattr(db, "get_task_photo_thumb", _get_thumb)
    monkeypatch.setattr(db, "set_task_photo_thumb", _set_thumb)
    monkeypatch.setattr(db, "get_task_photos_meta_bulk", _photos_bulk)
    return row


async def test_feed_requests_thumbnails_not_full_frames(client, photo_rows):
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert 'src="/dashboard/tok-abc/tasks/1/photo/7?size=thumb"' in body
    # полный кадр в ленте не запрашивается — только в data-full для лайтбокса
    assert 'data-full="/dashboard/tok-abc/tasks/1/photo/7"' in body
    assert 'loading="lazy"' in body and 'decoding="async"' in body


async def test_thumb_is_much_smaller_than_full_frame(client, photo_rows):
    full = await client.get(f"/dashboard/{TOKEN}/tasks/1/photo/7")
    thumb = await client.get(f"/dashboard/{TOKEN}/tasks/1/photo/7?size=thumb")
    assert full.status == thumb.status == 200
    full_bytes, thumb_bytes = len(await full.read()), len(await thumb.read())
    assert thumb_bytes < full_bytes / 3, (thumb_bytes, full_bytes)


async def test_missing_thumb_is_generated_once_and_persisted(client, photo_rows, spy):
    """Фото, загруженные до появления превью (и всё, что приходит из бота),
    дорендериваются лениво — но только один раз."""
    assert photo_rows["thumb"] is None
    await client.get(f"/dashboard/{TOKEN}/tasks/1/photo/7?size=thumb")
    assert spy["thumb_saved"] == [7] and photo_rows["thumb"] is not None
    await client.get(f"/dashboard/{TOKEN}/tasks/1/photo/7?size=thumb")
    assert spy["thumb_saved"] == [7]   # второй раз не пересжимается


async def test_photo_and_thumb_revalidate_with_etag(client, photo_rows):
    for url in (f"/dashboard/{TOKEN}/tasks/1/photo/7", f"/dashboard/{TOKEN}/tasks/1/photo/7?size=thumb"):
        first = await client.get(url)
        assert first.headers["Cache-Control"].startswith("private")
        again = await client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        assert again.status == 304 and await again.read() == b""


async def test_thumb_of_foreign_task_is_404(client, photo_rows):
    """Превью тоже нельзя вытащить по чужому task_id."""
    assert (await client.get(f"/dashboard/{TOKEN}/tasks/999/photo/7?size=thumb")).status == 404


async def test_single_lightbox_per_page(client, monkeypatch, task):
    """Раньше оверлей рендерился на каждую задачу с фото; теперь он один."""
    many = []
    for i in range(1, 6):
        t = dict(task)
        t["id"] = i
        many.append(t)

    async def _tasks(*a, **kw):
        return many

    async def _photos_bulk(ids):
        return {i: [{"id": i * 10, "mime": "image/jpeg"}] for i in ids}

    monkeypatch.setattr(db, "get_tasks", _tasks)
    monkeypatch.setattr(db, "get_task_photos_meta_bulk", _photos_bulk)
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert body.count('class="photo-lightbox"') == 1
    assert body.count('id="ph-lb"') == 1
    assert body.count("data-full=") == 5


# ---------- лёгкость страницы: лимит выполненных, ленивые карточки, JS ----------

@pytest.fixture
def many_done(monkeypatch, task):
    """Одна активная задача и 30 выполненных (в порядке ORDER BY done, due_at)."""
    tasks = [task]
    for i in range(30):
        t = dict(task)
        t.update(id=100 + i, done=True, title=f"готово {i}",
                 due_at=datetime.now(UTC) - timedelta(days=30 - i))
        tasks.append(t)

    async def _tasks(*a, **kw):
        return tasks

    monkeypatch.setattr(db, "get_tasks", _tasks)
    return tasks


async def test_done_tasks_are_limited_with_load_more(client, many_done):
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert body.count('class="task done"') == dashboard.config.DONE_TASKS_PAGE_SIZE
    assert "общая задача" in body                      # активные — все
    assert "готово 29" in body and "готово 0" not in body   # выполненные — самые свежие
    assert f"done_limit={2 * dashboard.config.DONE_TASKS_PAGE_SIZE}" in body

    body = await (await client.get(f"/dashboard/{TOKEN}?done_limit=40")).text()
    assert body.count('class="task done"') == 30
    assert "done_limit=" not in body


async def test_day_filter_shows_whole_day_without_limit(client, many_done):
    await _login(client)
    day = many_done[5]["due_at"].astimezone(ZoneInfo("Europe/Warsaw")).date()
    body = await (await client.get(f"/dashboard/{TOKEN}?date={day.isoformat()}")).text()
    assert "готово 4" in body and "done_limit=" not in body


async def test_expanded_card_is_lazy_template_with_nojs_fallback(client):
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    # раскрытая часть — инертный <template>, его достаёт dashboard.js
    assert '<template class="task-x"><div class="task-expanded">' in body
    assert f'href="/dashboard/{TOKEN}?nojs=1"' in body and "<noscript>" in body

    body = await (await client.get(f"/dashboard/{TOKEN}?nojs=1")).text()
    assert "<template" not in body and '<div class="task-expanded">' in body


async def test_page_js_is_external_cached_file(client):
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert "onclick=" not in body                      # стрелки карусели — через dashboard.js
    url = re.search(r'<script defer src="(/dashboard/static/dashboard\.[0-9a-f]+\.js)"', body).group(1)
    resp = await client.get(url)
    assert resp.status == 200 and resp.content_type == "application/javascript"
    assert "immutable" in resp.headers["Cache-Control"]
    js = await resp.text()
    assert "task-x" in js and "onTelegramAuth" in js


async def test_heavy_effects_are_opt_in(client):
    """Дорогие эффекты (блюр) — только под .mid/.fx, которые ставит детектор
    среднего и мощного устройства; по умолчанию страница облегчённая."""
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert "c.add('mid')" in body and "c.add('fx')" in body
    css_urls = re.findall(r'href="(/dashboard/static/[^"]+\.css)"', body)
    css = "".join([await (await client.get(u)).text() for u in css_urls])
    blur_rules = re.findall(r"([^{}]*)\{[^{}]*backdrop-filter:\s*(?!none)[^;{}]+", css)
    assert blur_rules
    for rule in blur_rules:
        assert ".mid" in rule or ".fx" in rule, rule
    # анимировать filter нельзя ни в каком режиме — он пересчитывается каждый кадр
    for name, body_ in re.findall(r"@keyframes\s*([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css):
        assert "filter" not in body_, name


async def test_pc_effects_are_loaded_only_by_fx_detector(client):
    """Анимации для ПК лежат в fx.css/fx.js: в HTML их ссылок нет (телефоны не
    качают ни байта), их подгружает детектор вместе с классом fx, а в самом
    fx.css каждое правило начинается с .fx и ни один keyframes не трогает filter."""
    await _login(client)
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert not re.search(r'(href|src)="/dashboard/static/fx\.', body)
    m = re.search(r"c\.add\('fx'\);.*?l\.href='(/dashboard/static/fx\.[0-9a-f]+\.css)';s\.src='(/dashboard/static/fx\.[0-9a-f]+\.js)'", body)
    assert m, "детектор должен подгружать fx.css и fx.js вместе с классом fx"

    css_resp = await client.get(m.group(1))
    assert css_resp.status == 200 and "immutable" in css_resp.headers["Cache-Control"]
    css = await css_resp.text()
    js_resp = await client.get(m.group(2))
    assert js_resp.status == 200 and js_resp.content_type == "application/javascript"
    js = await js_resp.text()
    assert "classList.contains('fx')" in js     # без класса скрипт ничего не делает

    keyframes = re.findall(r"@keyframes\s*([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)
    assert keyframes
    for name, body_ in keyframes:
        assert "filter" not in body_, name
    rest = re.sub(r"@keyframes\s*[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)
    for selectors in re.findall(r"([^{}]+)\{", rest):
        selectors = selectors.strip()
        if selectors.startswith("@"):            # @media / @supports — обёртка, не правило
            continue
        for sel in selectors.split(","):
            assert sel.strip().startswith(".fx"), sel


# ---------- Telegram Mini App ----------

def _init_data(user_id: int, auth_date: int | None = None, bad_hash: bool = False,
               start_param: str | None = None) -> str:
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps({"id": user_id})}
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", dashboard.SESSION_SECRET.encode(), hashlib.sha256).digest()
    fields["hash"] = "0" * 64 if bad_hash else hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_webapp_entry_is_not_swallowed_by_token_route(client):
    resp = await client.get("/dashboard/app")
    assert resp.status == 200
    assert "telegram-web-app.js" in await resp.text()


async def test_webapp_auth_logs_user_in_without_password(client, settings):
    settings.update(owner_id=999, owner_type="user")
    resp = await client.post("/dashboard/webapp-auth", json={"initData": _init_data(999)})
    assert resp.status == 200
    assert (await resp.json())["url"] == f"/dashboard/{TOKEN}"
    page = await client.get(f"/dashboard/{TOKEN}")
    assert 'name="csrf"' in await page.text()


@pytest.mark.parametrize("init_data", [
    _init_data(999, bad_hash=True), _init_data(999, auth_date=1), "garbage", "",
])
async def test_webapp_auth_rejects_bad_init_data(client, settings, init_data):
    settings.update(owner_id=999, owner_type="user")
    resp = await client.post("/dashboard/webapp-auth", json={"initData": init_data})
    assert resp.status == 403
    assert not resp.cookies


# ---------- «Поделиться задачей» ----------

@pytest_asyncio.fixture
async def share_client(monkeypatch, settings, task, spy):
    """Как client, но у задачи есть подзадача и включён BOT_USERNAME."""
    monkeypatch.setattr("dashboard.actions.BOT_USERNAME", "mybot")
    monkeypatch.setattr("dashboard.render.BOT_USERNAME", "mybot")
    task.update(tag="дом", description="описание")
    install_db_stubs(monkeypatch, TOKEN, settings, [task], spy=spy,
                     subtasks={1: [{"id": 5, "task_id": 1, "title": "шаг 1", "done": False, "position": 0}]})
    dashboard.set_scheduler_hooks(schedule=lambda task_id, *a, **kw: spy["scheduled"].append(task_id))
    app = aioweb.Application()
    dashboard.register_dashboard_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()
    dashboard.set_scheduler_hooks(None, None)


async def _login_recipient(client, settings) -> tuple[str, str]:
    """Получатель открывает Mini App по ссылке с кодом: (url экрана, csrf)."""
    settings.update(owner_id=999, owner_type="user")
    resp = await client.post("/dashboard/webapp-auth", json={"initData": _init_data(999, start_param=SHARE_CODE)})
    url = (await resp.json())["url"]
    body = await (await client.get(url)).text()
    return url, re.search(r'name="csrf" value="([0-9a-f]+)"', body).group(1)


async def test_webapp_auth_with_share_start_param_opens_share_screen(share_client, settings):
    settings.update(owner_id=999, owner_type="user")
    resp = await share_client.post("/dashboard/webapp-auth", json={"initData": _init_data(999, start_param=SHARE_CODE)})
    assert (await resp.json())["url"] == f"/dashboard/{TOKEN}/share/{SHARE_CODE}"


async def test_webapp_auth_ignores_malformed_start_param(share_client, settings):
    settings.update(owner_id=999, owner_type="user")
    resp = await share_client.post("/dashboard/webapp-auth", json={"initData": _init_data(999, start_param="../x")})
    assert (await resp.json())["url"] == f"/dashboard/{TOKEN}"


async def test_share_requires_session_and_csrf(share_client):
    url = f"/dashboard/{TOKEN}/tasks/1/share"
    assert (await share_client.post(url, data={"csrf": "x"})).status == 403
    await _login(share_client)
    assert (await share_client.post(url, data={"csrf": "bad"})).status == 403


async def test_share_returns_link_and_code(share_client):
    csrf = await _login(share_client)
    resp = await share_client.post(f"/dashboard/{TOKEN}/tasks/1/share", data={"csrf": csrf})
    data = await resp.json()
    assert data["link"] == f"https://t.me/mybot?startapp={SHARE_CODE}"
    assert data["prepared_id"] is None  # у группового дашборда личность Telegram неизвестна
    assert "общая задача" in data["text"]


async def test_share_foreign_task_is_404(share_client, task):
    csrf = await _login(share_client)
    task["owner_id"] = 1
    assert (await share_client.post(f"/dashboard/{TOKEN}/tasks/1/share", data={"csrf": csrf})).status == 404


async def test_share_button_rendered_with_bot_username(share_client):
    await _login(share_client)
    assert 'class="share-btn"' in await (await share_client.get(f"/dashboard/{TOKEN}")).text()


async def test_share_button_hidden_without_bot_username(client):
    await _login(client)
    assert 'class="share-btn"' not in await (await client.get(f"/dashboard/{TOKEN}")).text()


async def test_share_preview_shows_task(share_client, settings):
    url, _ = await _login_recipient(share_client, settings)
    page = await (await share_client.get(url)).text()
    assert "общая задача" in page and "шаг 1" in page and "описание" in page and "#дом" in page
    assert "Добавить к своим задачам" in page


async def test_share_preview_dead_code(share_client, settings):
    await _login_recipient(share_client, settings)
    page = await (await share_client.get(f"/dashboard/{TOKEN}/share/s_BBBBBBBBBBBB")).text()
    assert "больше недоступна" in page


async def test_share_accept_copies_snapshot(share_client, settings, spy):
    url, csrf = await _login_recipient(share_client, settings)
    due = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    resp = await share_client.post(f"{url}/accept", data={"csrf": csrf, "due_at": due}, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == f"/dashboard/{TOKEN}"
    added = spy["added"][0]
    assert (added["title"], added["owner_id"], added["owner_type"], added["repeat"]) == ("общая задача", 999, "user", "none")
    assert (added["tag"], added["description"]) == ("дом", "описание")
    assert spy["subtasks_added"] == [(42, "шаг 1")]
    assert spy["scheduled"] == [42]


async def test_share_accept_rejects_bad_csrf_bad_date_and_group_owner(share_client, settings, spy):
    url, csrf = await _login_recipient(share_client, settings)
    assert (await share_client.post(f"{url}/accept", data={"csrf": "bad", "due_at": "2030-01-01T10:00"})).status == 403
    bad = await share_client.post(f"{url}/accept", data={"csrf": csrf, "due_at": "nope"}, allow_redirects=False)
    assert bad.status == 302 and "error=bad_due_at" in bad.headers["Location"]
    assert not spy["added"]
    settings.update(owner_type="chat")  # групповой дашборд не может принимать чужие задачи
    assert (await share_client.post(f"{url}/accept", data={"csrf": csrf, "due_at": "2030-01-01T10:00"})).status in (403, 404)


async def test_prepare_share_message_degrades_to_none():
    from dashboard.share_hooks import _prepare_share_message
    dashboard.set_share_hooks(None)
    assert await _prepare_share_message(5, "t", "x", "b", "l") is None

    async def ok(*a):
        return "pid"

    async def boom(*a):
        raise RuntimeError("api")

    try:
        dashboard.set_share_hooks(ok)
        assert await _prepare_share_message(5, "t", "x", "b", "l") == "pid"
        assert await _prepare_share_message(0, "t", "x", "b", "l") is None
        dashboard.set_share_hooks(boom)
        assert await _prepare_share_message(5, "t", "x", "b", "l") is None
    finally:
        dashboard.set_share_hooks(None)


# ---------- вкладки и группировка ----------

async def test_view_tabs_and_grouping_params(client, task):
    await _login(client)
    page = await (await client.get(f"/dashboard/{TOKEN}?group=tag")).text()
    assert 'class="view-tab active"' in page and 'class="task-group"' in page and "Без тега" in page
    done_page = await (await client.get(f"/dashboard/{TOKEN}?view=done")).text()
    assert task["title"] not in done_page
    junk = await client.get(f"/dashboard/{TOKEN}?view=zzz&group=zzz")
    assert junk.status == 200 and task["title"] in await junk.text()


async def test_page_has_summary_add_sheet_and_tabbar(client):
    await _login(client)
    page = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert 'class="summary"' in page and 'class="add-sheet"' in page and 'class="tabbar"' in page
    assert page.count('action="/dashboard/tok-abc/tasks/new"') == 1
    err = await (await client.get(f"/dashboard/{TOKEN}?error=empty_title")).text()
    assert 'class="add-sheet" open' in err


async def test_tasks_page_has_side_panel_before_list(client):
    await _login(client)
    page = await (await client.get(f"/dashboard/{TOKEN}")).text()
    side, lst = page.index('<aside class="side">'), page.index('<main class="list">')
    assert side < page.index('class="summary"') < page.index('class="bars"') < page.index("</aside>") < lst
    assert page.index("общая задача") > lst
