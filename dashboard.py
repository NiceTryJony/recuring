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
from datetime import datetime, timedelta
from html import escape

from aiohttp import web as aioweb

import db
from stats import compute_streak, daily_series

logger = logging.getLogger(__name__)

CHART_DAYS = 14
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    # Раньше тут был тихий фолбэк на захардкоженную строку "fallback-secret" —
    # если переменная окружения вдруг не задана, подписи cookie/CSRF/Telegram
    # Login Widget стали бы подделываемы кем угодно, кто прочитал исходники.
    # bot.py и так требует BOT_TOKEN жёстко (os.environ["BOT_TOKEN"]) и
    # импортирует этот модуль раньше той строки, так что в текущей сборке
    # процесс и без этого не доедет до старта веб-сервера — но если
    # dashboard.py когда-нибудь запустят отдельно (тесты, отдельный процесс),
    # пусть падает здесь же, а не обслуживает запросы с публично известным секретом.
    raise RuntimeError("BOT_TOKEN не задан — без него SESSION_SECRET небезопасен")
SESSION_SECRET = BOT_TOKEN  # используем токен бота как секрет для HMAC
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # неделя

# ---------- rate-limit на попытки входа ----------
# In-memory, т.к. процесс один (Render free-тир); при рестарте счётчики
# обнуляются — это ок, не security-critical state. Ключ — (token, ip),
# чтобы не блокировать весь дашборд одному пользователю из-за соседа по NAT
# полностью, но и не дать перебирать пароль одного конкретного дашборда.
_LOGIN_ATTEMPTS: dict[tuple[str, str], list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 15 * 60   # считаем неудачи за последние 15 минут
_LOGIN_LOCKOUT_SECONDS = 10 * 60  # и блокируем на 10 минут после превышения


def _client_ip(request: aioweb.Request) -> str:
    # Render (и большинство PaaS) кладёт реальный IP в X-Forwarded-For;
    # первый адрес в списке — исходный клиент.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "unknown"


def _login_rate_limited(token: str, ip: str) -> bool:
    key = (token, ip)
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    _LOGIN_ATTEMPTS[key] = attempts
    if len(attempts) < _LOGIN_MAX_ATTEMPTS:
        return False
    # Уже достигли лимита — проверяем lockout от момента последней попытки,
    # а не просто окно неудач (иначе лимит снимался бы раньше времени).
    return (now - attempts[-1]) < _LOGIN_LOCKOUT_SECONDS


def _record_failed_login(token: str, ip: str):
    key = (token, ip)
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def _clear_login_attempts(token: str, ip: str):
    _LOGIN_ATTEMPTS.pop((token, ip), None)
AVATAR_CACHE_SECONDS = 3600  # getFile-ссылка живёт ~1ч, чтобы не дёргать Bot API на каждый показ
# Имя бота без @ — нужно виджету Telegram Login (data-telegram-login=...).
# Домен, на котором крутится дашборд, должен быть прописан боту через
# /setdomain в BotFather, иначе виджет откажется логинить.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")


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


def _sign_session(owner_id: int, owner_type: str, token: str, tg_user_id: int = 0, password_version: int = 0) -> str:
    """tg_user_id — личность реального Telegram-пользователя, известная только
    после входа через Telegram Login Widget (см. verify_telegram_login).
    0 значит "неизвестно" — так входят по обычному паролю на групповом
    дашборде, пока не прошли через виджет; для owner_type='user' сюда всегда
    пишется owner_id, потому что там владелец дашборда и есть тот пользователь.

    password_version зашит в подпись, чтобы смена пароля дашборда (/dashboard
    в боте) реально инвалидировала уже выданные cookie: db.set_owner_dashboard_credentials
    должен инкрементить это поле при каждой смене пароля. Без этого утёкший
    токен сессии продолжал бы работать даже после смены пароля."""
    payload = f"{owner_type}:{owner_id}:{tg_user_id}:{password_version}:{int(time.time()) + SESSION_MAX_AGE}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session(cookie_value: str, current_password_version: int | None = None) -> tuple[int, str, int] | None:
    """current_password_version, если передан, сверяется с версией в подписи —
    это и есть инвалидация старых сессий при смене пароля. None (вызовы без
    доступа к settings) пропускает эту проверку — обратная совместимость для
    мест, где version ещё не прокинута."""
    try:
        owner_type, owner_id_str, tg_user_id_str, pwd_ver_str, expiry_str, sig = cookie_value.split(":")
        payload = f"{owner_type}:{owner_id_str}:{tg_user_id_str}:{pwd_ver_str}:{expiry_str}"
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expiry_str) < time.time():
            return None
        if current_password_version is not None and int(pwd_ver_str) != current_password_version:
            return None
        return int(owner_id_str), owner_type, int(tg_user_id_str)
    except (ValueError, AttributeError):
        return None


async def _require_session(request: aioweb.Request, token: str) -> tuple[int, str, int] | None:
    """Общая проверка доступа для всех POST-действий (done/undone и т.д.): владелец
    дашборда должен существовать, а cookie — валидно расписываться именно на него.
    Возвращает (owner_id, owner_type, tg_user_id) или None, если доступ запрещён."""
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return None
    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None:
        return None
    owner_id, owner_type, tg_user_id = session
    if (owner_id, owner_type) != (settings["owner_id"], settings["owner_type"]):
        return None
    return owner_id, owner_type, tg_user_id


def _acting_user_id(owner_id: int, owner_type: str, tg_user_id: int) -> int:
    """Кого писать в log_history как исполнителя действия. На личном дашборде
    (owner_type='user') это всегда владелец — там только он и может залогиниться.
    На групповом — реальный Telegram id, если известен (вход через виджет),
    иначе деградируем на owner_id (chat_id) как и раньше: это семантически
    неверно (в истории окажется id чата, а не человека), но не ломает запись."""
    if owner_type == "user":
        return owner_id
    return tg_user_id or owner_id


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


# ---------- Telegram Login Widget ----------
# Отдельный алгоритм проверки подписи — так требует сам Telegram, не путать с
# _verify_session/csrf выше. Секрет для HMAC — не сам BOT_TOKEN, а SHA-256 от
# него (см. https://core.telegram.org/widgets/login#checking-authorization).
# BotFather должен знать домен дашборда (/setdomain), иначе виджет откажется
# работать на странице.

_TG_AUTH_MAX_AGE = 86400  # Telegram рекомендует отбрасывать auth_date старше суток


def verify_telegram_login(data: dict) -> bool:
    """data — то, что Login Widget прислал на фронтенд (id, first_name, username,
    auth_date, hash, ...). Проверяем подпись и свежесть auth_date."""
    received_hash = data.get("hash")
    if not received_hash:
        return False
    check_fields = {k: v for k, v in data.items() if k != "hash"}
    if "auth_date" not in check_fields or "id" not in check_fields:
        return False
    data_check_string = "\n".join(f"{k}={check_fields[k]}" for k in sorted(check_fields))
    secret_key = hashlib.sha256(SESSION_SECRET.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return False
    try:
        auth_date = int(check_fields["auth_date"])
    except ValueError:
        return False
    if time.time() - auth_date > _TG_AUTH_MAX_AGE:
        return False
    return True


# ---------- аватарки профиля (Telegram Bot API) ----------
# Telegram не присылает фото профиля сам по себе — ни в Login Widget, ни в
# обычных апдейтах бота. Один раз при логине через виджет: getUserProfilePhotos
# (file_id) -> getFile (file_path, живёт ~1ч) -> скачиваем байты -> сжимаем до
# AVATAR_MAX_SIDE и кладём в БД как JPEG. Дальше показ аватарки — это просто
# SELECT из БД, без единого обращения к Telegram на каждый просмотр дашборда.
AVATAR_MAX_SIDE = 128
AVATAR_JPEG_QUALITY = 82


async def _fetch_and_compress_photo(tg_user_id: int) -> tuple[bytes, str] | None:
    if not BOT_TOKEN:
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
                params={"user_id": tg_user_id, "limit": 1}, timeout=10,
            ) as resp:
                photos_data = await resp.json()
            if not photos_data.get("ok") or not photos_data["result"]["photos"]:
                return None
            file_id = photos_data["result"]["photos"][0][-1]["file_id"]  # самый крупный размер

            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id}, timeout=10,
            ) as resp:
                file_data = await resp.json()
            if not file_data.get("ok"):
                return None
            file_path = file_data["result"]["file_path"]

            async with session.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
    except Exception:
        logger.exception("Не удалось скачать фото профиля для user_id=%s", tg_user_id)
        return None

    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=AVATAR_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.exception("Не удалось сжать фото профиля для user_id=%s", tg_user_id)
        return None


# ---------- фото задач ----------
TASK_PHOTO_MAX_SIDE = 1280
TASK_PHOTO_JPEG_QUALITY = 85
TASK_PHOTO_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # лимит на входящий файл ДО сжатия


def compress_task_photo(raw: bytes) -> tuple[bytes, str] | None:
    """Сжимает присланные байты (откуда угодно — с дашборда или из бота) до
    JPEG разумного размера. Возвращает None, если это не декодируется как
    изображение (битый файл, не тот content-type) — вызывающая сторона должна
    явно отклонить загрузку в этом случае, а не падать с 500."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        img.load()  # форсируем декодирование сейчас, а не лениво при .save() —
                     # иначе битый файл всплывёт позже менее понятной ошибкой
        img = img.convert("RGB")
        img.thumbnail((TASK_PHOTO_MAX_SIDE, TASK_PHOTO_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=TASK_PHOTO_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.exception("Не удалось декодировать/сжать фото задачи")
        return None


async def handle_task_photo_upload(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/photo — загрузка фото с дашборда
    через multipart-форму. Та же сессия+CSRF защита, что и у остальных действий
    над задачей; лимит MAX_PHOTOS_PER_TASK проверяется здесь же (не в БД), так
    что при гонке двух одновременных загрузок возможен разовый перелимит на 1 —
    не считаем это проблемой для личного/семейного масштаба использования."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}", "")
    reader = await request.multipart()
    field = None
    csrf_token = ""
    file_bytes = b""
    async for part in reader:
        if part.name == "csrf":
            csrf_token = (await part.read()).decode("utf-8", errors="ignore")
        elif part.name == "photo":
            field = part
            # Читаем с ограничением, чтобы не раздувать память на огромном файле —
            # читаем по чанку и обрываем, как только превысили лимит.
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                file_bytes += chunk
                if len(file_bytes) > TASK_PHOTO_MAX_UPLOAD_BYTES:
                    return aioweb.Response(status=413)

    if not _verify_csrf(csrf_token, cookie):
        return aioweb.Response(status=403)
    if field is None or not file_bytes:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    if await db.count_task_photos(task_id) >= db.MAX_PHOTOS_PER_TASK:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    compressed = compress_task_photo(file_bytes)
    if compressed is None:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=bad_photo"})
    data, mime = compressed
    await db.add_task_photo(task_id, data, mime, uploaded_by=acting_user_id)
    await db.log_history(task_id, acting_user_id, task["title"], "photo_added")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_task_photo_get(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/tasks/{task_id}/photo/{photo_id} — отдаёт байты.
    Как и у аватарок, доступ не завязан на сессию (обычный <img src>), но
    привязан к валидному токену дашборда + проверке, что фото реально
    принадлежит задаче этого владельца (иначе можно перебирать чужие photo_id)."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(status=404)
    try:
        task_id = int(request.match_info["task_id"])
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        return aioweb.Response(status=404)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != settings["owner_id"] or task["owner_type"] != settings["owner_type"]:
        return aioweb.Response(status=404)

    photo = await db.get_task_photo(photo_id)
    if not photo or photo["task_id"] != task_id:
        return aioweb.Response(status=404)

    return aioweb.Response(
        body=photo["data"], content_type=photo["mime"],
        headers={"Cache-Control": f"public, max-age={AVATAR_CACHE_SECONDS}"},
    )


async def handle_task_photo_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/photo/{photo_id}/delete."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    await db.delete_task_photo(photo_id, task_id)
    await db.log_history(task_id, acting_user_id, task["title"], "photo_removed")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_avatar(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/avatar/{user_id} — отдаёт уже сжатую картинку из БД.
    Доступ не завязан на сессию (как и сами изображения в <img>), но токен
    дашборда должен быть валиден, иначе id участников чата можно перебирать."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(status=404)
    try:
        user_id = int(request.match_info["user_id"])
    except ValueError:
        return aioweb.Response(status=404)

    photo = await db.get_telegram_user_photo(user_id)
    if not photo:
        return aioweb.Response(status=404)
    data, mime = photo
    return aioweb.Response(
        body=data, content_type=mime,
        headers={"Cache-Control": f"public, max-age={AVATAR_CACHE_SECONDS}"},
    )


# ---------- переводы ----------

DASHBOARD_TEXTS = {
    "ru": {
        "html_lang": "ru",
        "login_title": "Вход — Task Reminder",
        "login_heading": "🔐 Вход",
        "password_placeholder": "Пароль",
        "btn_login": "Войти",
        "error_wrong_password": "Неверный пароль",
        "error_too_many_attempts": "Слишком много попыток. Подождите немного и попробуйте снова.",
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
        "tg_widget_note": "Войдите через Telegram, чтобы действия записывались под вашим именем",
        "nav_history": "📜 История",
        "nav_back": "⬅️ К задачам",
        "history_page_title": "История — Task Reminder",
        "history_heading": "📜 История событий",
        "history_empty": "Событий пока нет.",
        "event_created": "➕ создал(а) задачу",
        "event_done": "✅ выполнил(а)",
        "event_undone": "↩️ отменил(а) выполнение",
        "event_deleted": "🗑 удалил(а)",
        "event_rescheduled": "📅 перенёс(ла)",
        "btn_load_more": "Показать ещё",
        "btn_add_photo": "Добавить фото",
        "confirm_delete_photo": "Удалить фото?",
        "event_photo_added": "📷 добавил(а) фото к",
        "event_photo_removed": "🗑 удалил(а) фото у",
        "error_bad_photo": "Не удалось обработать файл как изображение",
    },
    "en": {
        "html_lang": "en",
        "login_title": "Login — Task Reminder",
        "login_heading": "🔐 Login",
        "password_placeholder": "Password",
        "btn_login": "Log in",
        "error_wrong_password": "Wrong password",
        "error_too_many_attempts": "Too many attempts. Please wait a bit and try again.",
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
        "tg_widget_note": "Sign in with Telegram so actions are recorded under your name",
        "nav_history": "📜 History",
        "nav_back": "⬅️ Back to tasks",
        "history_page_title": "History — Task Reminder",
        "history_heading": "📜 Event history",
        "history_empty": "No events yet.",
        "event_created": "➕ created the task",
        "event_done": "✅ completed",
        "event_undone": "↩️ unmarked",
        "event_deleted": "🗑 deleted",
        "event_rescheduled": "📅 rescheduled",
        "btn_load_more": "Load more",
        "btn_add_photo": "Add photo",
        "confirm_delete_photo": "Delete this photo?",
        "event_photo_added": "📷 added a photo to",
        "event_photo_removed": "🗑 removed a photo from",
        "error_bad_photo": "Couldn't process the file as an image",
    },
    "pl": {
        "html_lang": "pl",
        "login_title": "Logowanie — Task Reminder",
        "login_heading": "🔐 Logowanie",
        "password_placeholder": "Hasło",
        "btn_login": "Zaloguj się",
        "error_wrong_password": "Nieprawidłowe hasło",
        "error_too_many_attempts": "Zbyt wiele prób. Odczekaj chwilę i spróbuj ponownie.",
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
        "tg_widget_note": "Zaloguj się przez Telegram, aby działania zapisywały się pod twoim imieniem",
        "nav_history": "📜 Historia",
        "nav_back": "⬅️ Do zadań",
        "history_page_title": "Historia — Task Reminder",
        "history_heading": "📜 Historia zdarzeń",
        "history_empty": "Brak zdarzeń.",
        "event_created": "➕ utworzył(a) zadanie",
        "event_done": "✅ wykonał(a)",
        "event_undone": "↩️ cofnął(ęła) wykonanie",
        "event_deleted": "🗑 usunął(ęła)",
        "event_rescheduled": "📅 przełożył(a)",
        "btn_load_more": "Pokaż więcej",
        "btn_add_photo": "Dodaj zdjęcie",
        "confirm_delete_photo": "Usunąć zdjęcie?",
        "event_photo_added": "📷 dodał(a) zdjęcie do",
        "event_photo_removed": "🗑 usunął(ęła) zdjęcie z",
        "error_bad_photo": "Nie udało się przetworzyć pliku jako obrazu",
    },
}


def _dt(lang: str) -> dict:
    return DASHBOARD_TEXTS.get(lang, DASHBOARD_TEXTS["ru"])


# Общие дизайн-токены и база — единый визуальный язык под Telegram (тёмная
# тема, закруглённые карточки, мягкие тени, лёгкое появление карточек и
# отклик на тап), без реальной интеграции Telegram WebApp SDK. Подставляется
# как есть во все три шаблона до их .format() — поэтому фигурные скобки уже
# задвоены, как и в остальных шаблонах этого файла.
SHARED_CSS = """
:root {{
  /* "Index card on a desk" — cream card stock on a deep bottle-green desk,
     a brick-red stamp-ink accent for actions/overdue, ink-green for done.
     Picked to read as a ledger/logbook (this dashboard IS a log of who did
     what), not another dark-SaaS-with-one-accent-color screen. */
  --bg-0: #141b17;
  --bg-1: #1d2922;
  --card: #f6efdd;
  --card-edge: #e6d9b8;
  --ink: #2b2317;
  --ink-soft: #6e6250;
  --paper: #e9e3d2;
  --paper-soft: #93a093;
  --stamp: #a8392c;
  --stamp-deep: #7e2a20;
  --ok: #3f6b4e;
  --ok-deep: #2d4e38;
  --rule: rgba(43, 35, 23, 0.16);
  --rule-dark: rgba(233, 227, 210, 0.12);
  --shadow: 0 1px 0 rgba(0, 0, 0, 0.3), 0 14px 28px -16px rgba(0, 0, 0, 0.6);
  --radius: 10px;
  --radius-sm: 6px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 28px;
  --font-display: "Spectral", Georgia, "Times New Roman", serif;
  --font-body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: var(--font-body);
  background:
    radial-gradient(ellipse 900px 500px at 50% -10%, var(--bg-1) 0%, var(--bg-0) 65%);
  background-attachment: fixed;
  color: var(--paper); margin: 0;
  padding: var(--space-4);
  padding-top: max(var(--space-4), env(safe-area-inset-top));
  padding-bottom: max(var(--space-5), env(safe-area-inset-bottom));
  max-width: 640px; margin-left: auto; margin-right: auto;
  -webkit-font-smoothing: antialiased;
}}
h1 {{
  font-family: var(--font-display); font-weight: 600; font-style: italic;
  font-size: 1.55rem; margin: 0 0 var(--space-4); letter-spacing: -0.01em;
  color: var(--paper);
}}
button {{ font-family: inherit; -webkit-tap-highlight-color: transparent; transition: transform 0.1s ease; }}
button:active {{ transform: scale(0.96); }}
a {{ color: var(--stamp); }}
.nav-link {{ -webkit-tap-highlight-color: transparent; transition: opacity 0.1s ease; }}
.nav-link:active {{ opacity: 0.6; }}
button:focus-visible, input:focus-visible, a:focus-visible {{
  outline: 2px solid var(--stamp); outline-offset: 2px;
}}
@keyframes fade-up {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: translateY(0); }} }}
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; transition-duration: 0.001ms !important; }}
}}
.task, .event, .chart-block, .tg-widget-banner, .new-task-form {{
  background: var(--card); color: var(--ink);
  border-radius: var(--radius); box-shadow: var(--shadow);
  border: 1px solid var(--card-edge);
  animation: fade-up 0.3s ease both;
}}
{stagger_rules}
"""

# Лёгкий stagger для первых карточек в списке — ощущение, что список
# "выстраивается", а не просто мгновенно появляется весь разом. ВАЖНО: эти
# правила ещё не прогнаны через .format() — фигурные скобки здесь намеренно
# задвоены (как и во всём остальном SHARED_CSS), настоящее форматирование
# произойдёт один раз, позже, вместе со всей страницей (TASKS_PAGE/HISTORY_PAGE).
_stagger_rules = "\n".join(
    f".task:nth-child({i}), .event:nth-child({i}) {{{{ animation-delay: {i * 0.035:.3f}s; }}}}"
    for i in range(1, 13)
)
SHARED_CSS = SHARED_CSS.replace("{stagger_rules}", _stagger_rules)


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{login_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Spectral:ital,wght@0,500;0,600;0,700;1,400;1,500&display=swap" rel="stylesheet">
<style>""" + SHARED_CSS + """
body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
form {{
  background: var(--card); color: var(--ink); padding: 2rem 1.75rem;
  border-radius: var(--radius); box-shadow: var(--shadow); border: 1px solid var(--card-edge);
  width: 290px; animation: fade-up 0.3s ease both;
}}
input[type=password] {{
  width: 100%; padding: 10px 2px; margin: 4px 0 16px; border: none; border-bottom: 1.5px solid var(--rule);
  border-radius: 0; background: transparent; color: var(--ink); font-family: var(--font-body);
  font-size: 1rem; box-sizing: border-box;
}}
input[type=password]:focus {{ outline: none; border-bottom-color: var(--stamp); }}
button[type=submit] {{
  width: 100%; padding: 11px; border-radius: var(--radius-sm); border: none;
  background: var(--stamp); color: var(--card); font-weight: 600; cursor: pointer; font-size: 0.95rem;
}}
button[type=submit]:active {{ background: var(--stamp-deep); }}
.error {{ color: var(--stamp); font-size: 0.85rem; margin-top: 10px; font-style: italic; font-family: var(--font-display); }}
h2 {{ font-family: var(--font-display); font-style: italic; font-weight: 600; margin: 0 0 1.1rem; font-size: 1.35rem; color: var(--ink); }}
</style></head>
<body>
<form method="post">
<h2>{login_heading}</h2>
<input type="password" name="password" placeholder="{password_placeholder}" autofocus>
<button type="submit">{btn_login}</button>
{error}
</form>
</body></html>"""


# Баннер с Telegram Login Widget: не блокирует доступ (все действия и так уже
# разрешены по паролю), только предлагает уточнить identity для истории.
# JS-колбэк onTelegramAuth шлёт полученные от Telegram данные на наш же
# бэкенд (handle_telegram_auth), который сам перепроверяет подпись — сам
# виджет на фронтенде доверять нельзя, это просто источник данных.
TELEGRAM_WIDGET_BANNER = """<div class="tg-widget-banner">
<span>{note}</span>
<script async src="https://telegram.org/js/telegram-widget.js?22"
    data-telegram-login="{bot_username}" data-size="medium" data-radius="8"
    data-onauth="onTelegramAuth(user)" data-request-access="write"></script>
<script>
function onTelegramAuth(user) {{
  user._csrf = "{csrf}";
  fetch("/dashboard/{token}/telegram-auth", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(user),
  }}).then(function(r) {{ if (r.ok) location.reload(); }});
}}
</script>
</div>"""


TASKS_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Spectral:ital,wght@0,500;0,600;0,700;1,400;1,500&display=swap" rel="stylesheet">
<style>""" + SHARED_CSS + """
.task {{ padding: var(--space-4); margin-bottom: var(--space-3);
        display: flex; gap: var(--space-3); align-items: flex-start; }}
.task.done {{ background: #ece3cd; }}
.task.done .title {{ color: var(--ink-soft); text-decoration: line-through; text-decoration-color: var(--ok); text-decoration-thickness: 1.5px; }}
.task.done .meta {{ color: var(--ink-soft); opacity: 0.8; }}
.task.overdue .meta {{ color: var(--stamp); }}
.task form {{ margin: 0; line-height: 0; }}
.check-btn {{
  width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--ink-soft);
  background: transparent; cursor: pointer; flex-shrink: 0; margin-top: 2px; padding: 0;
  position: relative; transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}}
.task.overdue .check-btn {{ border-color: var(--stamp); }}
.task.done .check-btn {{ background: var(--ok); border-color: var(--ok); }}
.task.done .check-btn::after {{
  content: ""; position: absolute; left: 8px; top: 4px; width: 6px; height: 11px;
  border: solid var(--card); border-width: 0 2px 2px 0; transform: rotate(40deg);
}}
.task-body {{ flex: 1; min-width: 0; }}
.title {{ font-family: var(--font-display); font-weight: 600; font-size: 1.08rem; color: var(--ink); }}
.meta {{ color: var(--ink-soft); font-size: 0.85rem; margin-top: 4px; }}
.task-actions {{ display: flex; gap: 16px; margin-top: 10px; }}
.task-actions button {{
  background: none; border: none; color: var(--ink-soft); font-size: 0.82rem;
  font-family: var(--font-display); font-style: italic; cursor: pointer; padding: 0;
  transition: color 0.15s ease;
}}
.task-actions button:active {{ color: var(--stamp); }}
.task-actions .del-btn:active {{ color: var(--stamp-deep); }}
.tag {{ display: inline-block; color: var(--stamp-deep); font-size: 0.78rem; font-weight: 600; margin-top: 6px; }}
.task-author {{ display: flex; align-items: center; gap: 6px; margin-top: 8px;
               font-size: 0.8rem; color: var(--ink-soft); }}
.avatar {{ width: 20px; height: 20px; border-radius: 50%; object-fit: cover;
          background: var(--card-edge); border: 1.5px solid var(--card); outline: 1px solid var(--rule);
          flex-shrink: 0; }}
.avatar-placeholder {{ display: inline-flex; align-items: center; justify-content: center; font-size: 0.7rem; }}
.empty {{ color: var(--paper-soft); text-align: center; padding: 3rem 1rem; font-family: var(--font-display); font-style: italic; }}
.chart-block {{ padding: var(--space-4); margin-top: var(--space-5); }}
.chart-head {{ display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 4px; }}
.chart-title {{ font-family: var(--font-display); font-weight: 600; color: var(--ink); }}
.streak {{ color: var(--stamp-deep); font-size: 0.9rem; font-style: italic; font-family: var(--font-display); }}
.bars {{ display: flex; align-items: flex-end; gap: 4px; height: 120px; margin-top: 14px;
        border-bottom: 1px solid var(--rule); padding-bottom: 1px; }}
.col {{ flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; min-width: 0; }}
.num {{ font-size: 0.7rem; color: var(--ink-soft); height: 14px; line-height: 14px; }}
.bar {{ width: 100%; background: var(--ok); border-radius: 2px 2px 0 0; transition: height 0.3s ease; }}
.bar.zero {{ background: transparent; border: 1px dashed var(--rule); height: 2px !important; }}
.labels {{ display: flex; gap: 4px; margin-top: 4px; }}
.labels span {{ flex: 1; text-align: center; font-size: 0.65rem; color: var(--ink-soft); }}
.new-task-form {{ display: flex; flex-direction: column; gap: 10px; padding: var(--space-4); margin-bottom: var(--space-4); }}
.new-task-form input[type=text] {{
  width: 100%; padding: 8px 2px; border: none; border-bottom: 1.5px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--ink); font-family: var(--font-body); font-size: 1rem; box-sizing: border-box;
}}
.new-task-form input[type=text]::placeholder {{ color: var(--ink-soft); }}
.new-task-form input[type=datetime-local] {{
  padding: 8px 2px; border: none; border-bottom: 1.5px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--ink); font-family: var(--font-body); color-scheme: light; box-sizing: border-box;
}}
.new-task-form input:focus {{ outline: none; border-bottom-color: var(--stamp); }}
.new-task-form button {{
  padding: 10px 16px; border-radius: var(--radius-sm); border: none; background: var(--stamp);
  color: var(--card); font-weight: 600; cursor: pointer; flex-shrink: 0;
}}
.new-task-form button:active {{ background: var(--stamp-deep); }}
.new-task-error {{ color: var(--stamp); font-size: 0.85rem; margin: -0.5rem 0 0.25rem; font-style: italic; font-family: var(--font-display); }}
.tg-widget-banner {{ padding: 0.8rem 1rem; margin-bottom: var(--space-4);
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }}
.tg-widget-banner span {{ font-size: 0.85rem; color: var(--ink-soft); font-style: italic; font-family: var(--font-display); }}
.nav-link {{ display: inline-block; color: var(--paper-soft); text-decoration: none; font-size: 0.85rem; margin-bottom: var(--space-4);
    font-family: var(--font-display); font-style: italic; }}
.event {{ padding: 0.85rem 1rem; margin-bottom: 0.6rem; display: flex; gap: 0.65rem; align-items: center; }}
.event-body {{ flex: 1; min-width: 0; }}
.event-line {{ font-size: 0.92rem; color: var(--ink); }}
.event-line .ev-title {{ font-weight: 600; font-family: var(--font-display); }}
.event-time {{ color: var(--ink-soft); font-size: 0.78rem; margin-top: 2px; }}
.load-more {{ display: block; width: 100%; padding: 10px; border-radius: var(--radius-sm); border: 1px dashed var(--rule-dark);
    background: transparent; color: var(--paper-soft); text-align: center; text-decoration: none; margin-top: 0.5rem; box-sizing: border-box;
    font-family: var(--font-display); font-style: italic; }}
.load-more:active {{ border-color: var(--stamp); color: var(--stamp); }}
@media (max-width: 480px) {{ .new-task-form button {{ width: 100%; }} }}
.task-photos {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
.task-photo {{ position: relative; width: 64px; height: 64px; border-radius: var(--radius-sm); overflow: hidden;
    border: 1px solid var(--card-edge); flex-shrink: 0; }}
.task-photo img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.task-photo form {{ position: absolute; top: 2px; right: 2px; line-height: 0; }}
.photo-del-btn {{ width: 18px; height: 18px; border-radius: 50%; border: none; background: rgba(43, 35, 23, 0.65);
    color: var(--card); font-size: 11px; line-height: 1; cursor: pointer; padding: 0; }}
.photo-del-btn:active {{ background: var(--stamp-deep); }}
.photo-upload-form {{ margin-top: 8px; }}
.photo-upload-btn {{ display: inline-flex; align-items: center; gap: 4px; font-size: 0.82rem; color: var(--ink-soft);
    font-family: var(--font-display); font-style: italic; cursor: pointer; }}
.photo-upload-btn input[type=file] {{ position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }}
</style></head>
<body>
<h1>{heading}</h1>
{nav_html}
{tg_widget_html}
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


HISTORY_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{history_page_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Spectral:ital,wght@0,500;0,600;0,700;1,400;1,500&display=swap" rel="stylesheet">
<style>""" + SHARED_CSS + """
.event {{ padding: 0.85rem 1rem; margin-bottom: 0.6rem; display: flex; gap: 0.65rem; align-items: center; }}
.event-body {{ flex: 1; min-width: 0; }}
.event-line {{ font-size: 0.92rem; color: var(--ink); }}
.event-line .ev-title {{ font-weight: 600; font-family: var(--font-display); }}
.event-time {{ color: var(--ink-soft); font-size: 0.78rem; margin-top: 2px; }}
.avatar {{ width: 28px; height: 28px; border-radius: 50%; object-fit: cover;
          background: var(--card-edge); border: 1.5px solid var(--card); outline: 1px solid var(--rule); flex-shrink: 0; }}
.avatar-placeholder {{ display: inline-flex; align-items: center; justify-content: center; font-size: 0.9rem; }}
.empty {{ color: var(--paper-soft); text-align: center; padding: 3rem 1rem; font-family: var(--font-display); font-style: italic; }}
.load-more {{ display: block; width: 100%; padding: 10px; border-radius: var(--radius-sm); border: 1px dashed var(--rule-dark);
    background: transparent; color: var(--paper-soft); text-align: center; text-decoration: none; margin-top: 0.5rem; box-sizing: border-box;
    font-family: var(--font-display); font-style: italic; }}
.load-more:active {{ border-color: var(--stamp); color: var(--stamp); }}
.nav-link {{ display: inline-block; color: var(--paper-soft); text-decoration: none; font-size: 0.85rem; margin-bottom: var(--space-4);
    font-family: var(--font-display); font-style: italic; }}
</style></head>
<body>
<a class="nav-link" href="/dashboard/{token}">{nav_back}</a>
<h1>{history_heading}</h1>
{events_html}
{load_more_html}
</body></html>"""


def _render_event(ev: dict, tz, lang: str, token: str, creator: dict | None) -> str:
    texts = _dt(lang)
    local_dt = ev["event_at"].astimezone(tz)
    label = texts.get(f"event_{ev['event']}", ev["event"])
    actor_id = ev["user_id"]
    if creator and (creator.get("first_name") or creator.get("username")):
        name = creator.get("first_name") or f"@{creator['username']}"
    else:
        name = f"id{actor_id}"
    if creator and creator.get("has_photo"):
        avatar_html = f'<img class="avatar" src="/dashboard/{token}/avatar/{actor_id}" alt="">'
    else:
        avatar_html = '<span class="avatar avatar-placeholder">👤</span>'
    return f"""<div class="event">
{avatar_html}
<div class="event-body">
<div class="event-line">{escape(name)} {escape(label)} «<span class="ev-title">{escape(ev["title"])}</span>»</div>
<div class="event-time">{local_dt.strftime('%d.%m.%Y %H:%M')}</div>
</div>
</div>"""


HISTORY_PAGE_SIZE = 30


async def handle_history(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/history — пока только для группового дашборда:
    для личного автор события всегда один и тот же человек, лента не нужна."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    if owner_type != "chat":
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}")
    session = _verify_session(cookie) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        # Как и на главной странице — незалогиненный видит форму пароля.
        return aioweb.Response(text=LOGIN_PAGE.format(error="", **texts), content_type="text/html")

    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings["timezone"])
    except Exception:
        tz = ZoneInfo("UTC")

    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0

    # Берём на одну запись больше лимита — если она есть, значит дальше ещё
    # что-то осталось и нужно показать "Показать ещё".
    events = await db.get_event_feed(None, owner_id, limit=HISTORY_PAGE_SIZE + 1, offset=offset)
    has_more = len(events) > HISTORY_PAGE_SIZE
    events = events[:HISTORY_PAGE_SIZE]

    if not events:
        events_html = f'<div class="empty">{escape(texts["history_empty"])}</div>'
    else:
        actor_ids = {e["user_id"] for e in events}
        creators = await db.get_telegram_users(list(actor_ids))
        events_html = "\n".join(_render_event(e, tz, lang, token, creators.get(e["user_id"])) for e in events)

    load_more_html = ""
    if has_more:
        next_offset = offset + HISTORY_PAGE_SIZE
        load_more_html = f'<a class="load-more" href="/dashboard/{token}/history?offset={next_offset}">{escape(texts["btn_load_more"])}</a>'

    page = HISTORY_PAGE.format(
        token=token, events_html=events_html, load_more_html=load_more_html,
        **{k: v for k, v in texts.items() if k != "repeat"},
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


def _render_author(created_by: int | None, creator: dict | None, token: str) -> str:
    """Бейдж автора (аватар + имя) для группового дашборда. creator — запись из
    db.get_telegram_users (есть только у тех, кто хоть раз логинился через
    Telegram Login Widget); для остальных просто показываем raw id."""
    if not created_by:
        return ""
    if creator and (creator.get("first_name") or creator.get("username")):
        name = creator.get("first_name") or f"@{creator['username']}"
    else:
        name = f"id{created_by}"
    if creator and creator.get("has_photo"):
        avatar_html = f'<img class="avatar" src="/dashboard/{token}/avatar/{created_by}" alt="">'
    else:
        avatar_html = '<span class="avatar avatar-placeholder">👤</span>'
    return f'<div class="task-author">{avatar_html}<span>{escape(name)}</span></div>'


def _render_photos(photos: list[dict], token: str, task_id: int, csrf: str, texts: dict) -> str:
    """Галерея превьюшек под задачей. Сами байты не инлайнятся в HTML — каждая
    картинка подтягивается браузером отдельным GET на /photo/{id} (кешируется),
    здесь только теги <img> + форма удаления (крестик) поверх миниатюры."""
    if not photos:
        return ""
    items = []
    for p in photos:
        items.append(f"""<div class="task-photo">
<img src="/dashboard/{token}/tasks/{task_id}/photo/{p['id']}" alt="" loading="lazy">
<form method="post" action="/dashboard/{token}/tasks/{task_id}/photo/{p['id']}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_photo"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="photo-del-btn" aria-label="delete photo">✕</button>
</form>
</div>""")
    return f'<div class="task-photos">{"".join(items)}</div>'


def _render_upload_form(token: str, task_id: int, csrf: str, texts: dict, photo_count: int) -> str:
    """Форма прикрепления фото — скрыта, когда лимит на задачу уже достигнут
    (MAX_PHOTOS_PER_TASK), чтобы не провоцировать запрос, который сервер
    всё равно отклонит."""
    if photo_count >= db.MAX_PHOTOS_PER_TASK:
        return ""
    return f"""<form method="post" action="/dashboard/{token}/tasks/{task_id}/photo" enctype="multipart/form-data" class="photo-upload-form">
<input type="hidden" name="csrf" value="{csrf}">
<label class="photo-upload-btn">📷 {escape(texts["btn_add_photo"])}
<input type="file" name="photo" accept="image/*" onchange="this.form.submit()">
</label>
</form>"""


def _render_task(
    task: dict, tz, lang: str, token: str, csrf: str,
    creator: dict | None = None, owner_type: str = "user",
    photos: list[dict] | None = None,
) -> str:
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
    # Автора показываем только в групповом дашборде — в личном он и так всегда
    # один и тот же человек, бейдж был бы бесполезным шумом.
    author_html = _render_author(task.get("created_by"), creator, token) if owner_type == "chat" else ""

    photos = photos or []
    photos_html = _render_photos(photos, token, task["id"], csrf, texts)
    upload_html = _render_upload_form(token, task["id"], csrf, texts, len(photos))

    extra_actions = ""
    if not task["done"]:
        # «Отложить на день» двигает due_at в БД, но у повторяющихся задач due_at
        # не источник расписания (см. bot.py: schedule_task строит CronTrigger
        # один раз, а restore_jobs() при рестарте пересоберёт его уже по
        # сдвинутому due_at) — кнопка либо ничего не даёт сейчас, либо незаметно
        # съедет день/час повтора после следующего рестарта процесса. Поэтому
        # показываем её только для разовых задач (repeat == "none"); тот же
        # repeat != "none" уже отдельно обрабатывается в bot.py/snooze_task.
        postpone_html = (
            f"""<form method="post" action="/dashboard/{token}/tasks/{task['id']}/postpone">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">⏭ {escape(texts["btn_postpone"])}</button>
</form>"""
            if task["repeat"] == "none" else ""
        )
        extra_actions = f"""<div class="task-actions">
{postpone_html}
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
{author_html}
{tag_html}
{photos_html}
{upload_html}
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
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None

    if request.method == "POST":
        ip = _client_ip(request)
        if _login_rate_limited(token, ip):
            error_html = f'<div class="error">{escape(texts["error_too_many_attempts"])}</div>'
            return aioweb.Response(
                text=LOGIN_PAGE.format(error=error_html, **texts),
                content_type="text/html", status=429,
            )

        data = await request.post()
        password = data.get("password", "")
        if await asyncio.to_thread(verify_password, password, settings["dashboard_password_hash"] or ""):
            _clear_login_attempts(token, ip)
            # tg_user_id=0: кто именно ввёл общий пароль, нам неизвестно — для
            # owner_type='chat' это уточнится позже через виджет на самой
            # странице (см. handle_telegram_auth), без этого шага деградируем
            # на owner_id (chat_id) в истории, как было раньше.
            session_value = _sign_session(owner_id, owner_type, token, password_version=settings.get("dashboard_password_version", 0))
            resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
            # Render терминирует TLS на прокси — схему берём из X-Forwarded-Proto
            is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
            resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                            samesite="Strict", secure=is_https)
            return resp

        _record_failed_login(token, ip)
        error_html = f'<div class="error">{escape(texts["error_wrong_password"])}</div>'
        return aioweb.Response(
            text=LOGIN_PAGE.format(error=error_html, **texts),
            content_type="text/html", status=401,
        )

    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        return aioweb.Response(text=LOGIN_PAGE.format(error="", **texts), content_type="text/html")
    _, _, tg_user_id = session

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
        creators: dict[int, dict] = {}
        if owner_type == "chat":
            creator_ids = {t["created_by"] for t in tasks if t.get("created_by")}
            creators = await db.get_telegram_users(list(creator_ids))
        # Фото каждой задачи — отдельным запросом на задачу (метаданные без
        # байтов), не одним большим IN(...): список задач на дашборде обычно
        # небольшой (до пары десятков), а db.get_task_photos_meta и так лёгкий
        # индексированный SELECT по task_id.
        photos_by_task = {
            t["id"]: await db.get_task_photos_meta(t["id"]) for t in tasks
        }
        tasks_html = "\n".join(
            _render_task(
                t, tz, lang, token, csrf, creator=creators.get(t.get("created_by")), owner_type=owner_type,
                photos=photos_by_task.get(t["id"]),
            )
            for t in tasks
        )

    try:
        chart_html = await _build_chart(owner_id, owner_type, tz, texts)
    except Exception:
        # график — второстепенный блок: сбой БД/статистики не должен ронять весь дашборд
        logger.exception("Не удалось построить график активности owner=%s/%s", owner_type, owner_id)
        chart_html = ""

    heading = texts["heading_chat"] if owner_type == "chat" else texts["heading"]
    nav_html = (
        f'<a class="nav-link" href="/dashboard/{token}/history">{escape(texts["nav_history"])}</a>'
        if owner_type == "chat" else ""
    )
    new_task_error_html = ""
    if request.query.get("error") == "empty_title":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_empty_title"])}</div>'
    elif request.query.get("error") == "bad_photo":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_bad_photo"])}</div>'

    # Виджет показываем только на групповом дашборде и только пока не знаем,
    # кто именно из участников сейчас смотрит страницу (вошли по общему паролю,
    # через виджет ещё не проходили). На личном дашборде identity уже известна
    # (owner_id сам и есть tg_user_id), виджет там не нужен.
    tg_widget_html = ""
    if owner_type == "chat" and tg_user_id == 0 and BOT_USERNAME:
        tg_widget_html = TELEGRAM_WIDGET_BANNER.format(
            bot_username=BOT_USERNAME, token=token, note=texts["tg_widget_note"], csrf=csrf,
        )

    page = TASKS_PAGE.format(
        tasks_html=tasks_html, chart_html=chart_html, heading=heading, nav_html=nav_html,
        token=token, csrf=csrf, new_task_error=new_task_error_html, tg_widget_html=tg_widget_html,
        **{k: v for k, v in texts.items() if k not in ("heading",)},
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_telegram_auth(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/telegram-auth — вызывается из onTelegramAuth()
    после успешного входа через Login Widget. Не выдаёт новых прав (действия и
    так разрешены паролем) — только уточняет, какой именно участник сейчас за
    дашбордом, чтобы история велась под правильным tg_user_id, а не chat_id."""
    token = request.match_info["token"]

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _ = owner
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

    settings = await db.get_owner_by_dashboard_token(token)
    pwd_version = settings.get("dashboard_password_version", 0) if settings else 0
    session_value = _sign_session(owner_id, owner_type, token, tg_user_id=tg_user_id, password_version=pwd_version)
    resp = aioweb.Response(status=200)
    is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="Strict", secure=is_https)
    return resp


async def handle_task_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/new — создание задачи прямо с дашборда.
    Та же защита, что и у остальных действий: сессия + CSRF."""
    token = request.match_info["token"]

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

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
    task_id = await db.add_task(owner_id, owner_type, title, due_at, "none", "on_time", created_by=acting_user_id)
    await db.log_history(task_id, acting_user_id, title, "created")

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
    owner_id, owner_type, tg_user_id = owner
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

    if action == "done":
        await db.mark_done(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "done")
    elif action == "undone":
        await db.mark_undone(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "undone")
    elif action == "postpone":
        if task["repeat"] != "none":
            # Сдвиг due_at у повторяющейся задачи не отражается на её реальном
            # расписании (CronTrigger в bot.py строится по due_at только при
            # создании/рестарте) — см. комментарий у _render_task. Кнопка в
            # шаблоне уже скрыта для таких задач; это серверная подстраховка
            # на случай прямого POST мимо формы.
            return aioweb.Response(status=400)
        await db.update_task(task_id, due_at=task["due_at"] + timedelta(days=1))
        await db.log_history(task_id, acting_user_id, task["title"], "rescheduled")
    elif action == "delete":
        await db.delete_task(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "deleted")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


def register_dashboard_routes(app: aioweb.Application):
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}/telegram-auth", handle_telegram_auth)
    app.router.add_post("/dashboard/{token}/tasks/new", handle_task_create)
    # Важно: более специфичные /photo-маршруты регистрируются ДО общего
    # /{action} — иначе POST .../tasks/{id}/photo перехватывался бы
    # handle_task_toggle как action="photo" (не входит в его allow-list
    # done/undone/postpone/delete) и отдавал 404 вместо загрузки фото.
    # aiohttp матчит маршруты в порядке регистрации, не по специфичности.
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/photo", handle_task_photo_upload)
    app.router.add_get("/dashboard/{token}/tasks/{task_id}/photo/{photo_id}", handle_task_photo_get)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/photo/{photo_id}/delete", handle_task_photo_delete)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/{action}", handle_task_toggle)
    app.router.add_get("/dashboard/{token}/avatar/{user_id}", handle_avatar)
    app.router.add_get("/dashboard/{token}/history", handle_history)