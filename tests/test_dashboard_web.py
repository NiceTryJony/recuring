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
    return {"photos_bulk": 0, "subtasks_bulk": 0, "photos_single": 0, "subtasks_single": 0,
            "scheduled": [], "unscheduled": [], "added": []}


@pytest_asyncio.fixture
async def client(monkeypatch, settings, task, spy):
    async def _owner(token):
        return settings if token == TOKEN else None

    async def _tasks(*a, **kw):
        return [task]

    async def _task(task_id):
        return task if task_id == task["id"] else None

    async def _photos_bulk(ids):
        spy["photos_bulk"] += 1
        return {}

    async def _subtasks_bulk(ids):
        spy["subtasks_bulk"] += 1
        return {}

    async def _photos_single(task_id):
        spy["photos_single"] += 1
        return []

    async def _subtasks_single(task_id):
        spy["subtasks_single"] += 1
        return []

    async def _add_task(owner_id, owner_type, title, due_at, repeat, remind, **kw):
        spy["added"].append({"title": title, "due_at": due_at, "repeat": repeat})
        return 42

    async def _noop(*a, **kw):
        return None

    async def _empty_list(*a, **kw):
        return []

    async def _empty_dict(*a, **kw):
        return {}

    for name, impl in {
        "get_owner_by_dashboard_token": _owner,
        "get_tasks": _tasks,
        "get_task": _task,
        "get_task_photos_meta_bulk": _photos_bulk,
        "get_subtasks_bulk": _subtasks_bulk,
        "get_task_photos_meta": _photos_single,
        "get_subtasks": _subtasks_single,
        "add_task": _add_task,
        "get_telegram_users": _empty_dict,
        "get_history_stats": _empty_list,
        "get_done_days": _empty_list,
        "get_event_feed": _empty_list,
        "get_templates": _empty_list,
        "log_history": _noop,
        "mark_done": _noop,
        "mark_undone": _noop,
        "update_task": _noop,
        "delete_task": _noop,
    }.items():
        monkeypatch.setattr(db, name, impl)

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


async def test_fonts_do_not_block_first_paint(client):
    """Таблица стилей шрифтов грузится неблокирующе (media=print + onload),
    а начертаний запрашивается ровно столько, сколько используется."""
    body = await (await client.get(f"/dashboard/{TOKEN}")).text()
    assert 'media="print" onload="this.media=' in body
    assert "<noscript>" in body
    assert "wght@0,600..700;1,400..600" in body and "Inter:wght@400..600" in body


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
