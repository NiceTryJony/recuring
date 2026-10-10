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
import calendar as cal
import datetime as dt
import gzip
import hashlib
import hmac
import io
import logging
import os
import re
import time
from datetime import datetime, time as dtime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web as aioweb
from PIL import Image

import db
from stats import compute_streak, daily_series

UTC = ZoneInfo("UTC")

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


# Сколько ПОСЛЕДНИХ адресов в X-Forwarded-For добавлено нашими собственными
# прокси. Для Render это 1. Брать адрес нельзя из начала списка: прокси
# ДОПИСЫВАЕТ адрес своего клиента в конец, поэтому левые элементы — это то,
# что прислал сам клиент, т.е. полностью подконтрольные ему значения. Раньше
# тут был fwd.split(",")[0] — и rate-limit на вход обходился простым
# "X-Forwarded-For: <случайный IP>" в каждом запросе (ключ (token, ip) всякий
# раз новый, лимит никогда не срабатывал).
_TRUSTED_PROXY_COUNT = max(1, int(os.environ.get("TRUSTED_PROXY_COUNT", "1")))


def _client_ip(request: aioweb.Request) -> str:
    chain = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if chain:
        # len-N — адрес, который дописал самый внешний ДОВЕРЕННЫЙ прокси;
        # max(..., 0) — если цепочка короче ожидаемой (запрос пришёл напрямую).
        return chain[max(len(chain) - _TRUSTED_PROXY_COUNT, 0)]
    return request.remote or "unknown"


_LOGIN_PRUNE_INTERVAL = 5 * 60
_last_login_prune = 0.0


def _prune_login_attempts(now: float):
    """Раньше запись (token, ip) создавалась на КАЖДЫЙ POST и удалялась только
    при успешном входе — словарь рос бесконечно (с подделкой X-Forwarded-For
    это был дешёвый способ раздуть память процесса). Теперь устаревшие ключи
    выметаются целиком, не чаще раза в _LOGIN_PRUNE_INTERVAL."""
    global _last_login_prune
    if now - _last_login_prune < _LOGIN_PRUNE_INTERVAL:
        return
    _last_login_prune = now
    horizon = max(_LOGIN_WINDOW_SECONDS, _LOGIN_LOCKOUT_SECONDS)
    for key, attempts in list(_LOGIN_ATTEMPTS.items()):
        if not attempts or now - attempts[-1] > horizon:
            del _LOGIN_ATTEMPTS[key]


def _login_rate_limited(token: str, ip: str) -> bool:
    key = (token, ip)
    now = time.time()
    _prune_login_attempts(now)
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if not attempts:
        # Пустой список не храним вовсе — иначе один запрос с любым паролем
        # оставлял после себя вечную запись в словаре.
        _LOGIN_ATTEMPTS.pop(key, None)
        return False
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


def _sign_session(owner_id: int, owner_type: str, tg_user_id: int = 0, password_version: int = 0) -> str:
    """Привязка cookie к конкретному дашборду обеспечивается именем cookie
    (session_<token>) и сверкой owner_id/owner_type с владельцем токена в
    _require_session — поэтому сам токен в подпись не входит (раньше он был
    параметром функции, но в payload не попадал, что только путало).

    tg_user_id — личность реального Telegram-пользователя, известная только
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


async def _require_session_full(request: aioweb.Request, token: str) -> tuple[dict, int, str, int] | None:
    """Общая проверка доступа для всех POST-действий (done/undone и т.д.): владелец
    дашборда должен существовать, а cookie — валидно расписываться именно на него.
    Возвращает (settings, owner_id, owner_type, tg_user_id) или None при отказе.

    settings отдаём наружу, потому что половине обработчиков он нужен сразу
    после проверки (timezone для due_at) — раньше они делали второй такой же
    get_owner_by_dashboard_token, т.е. лишний SELECT на каждое действие."""
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
    return settings, owner_id, owner_type, tg_user_id


async def _require_session(request: aioweb.Request, token: str) -> tuple[int, str, int] | None:
    """Вариант для обработчиков, которым settings не нужен."""
    full = await _require_session_full(request, token)
    return None if full is None else full[1:]


def _tz_of(settings: dict | None):
    """Часовой пояс владельца с фолбэком на UTC — одна точка вместо четырёх
    одинаковых try/except по файлу. ZoneInfo кеширует объекты по имени зоны,
    так что повторные вызовы ничего не пересчитывают."""
    try:
        return ZoneInfo(settings["timezone"])
    except Exception:
        return UTC


def _acting_user_id(owner_id: int, owner_type: str, tg_user_id: int) -> int:
    """Кого писать в log_history как исполнителя действия. На личном дашборде
    (owner_type='user') это всегда владелец — там только он и может залогиниться.
    На групповом — реальный Telegram id, если известен (вход через виджет),
    иначе деградируем на owner_id (chat_id) как и раньше: это семантически
    неверно (в истории окажется id чата, а не человека), но не ломает запись."""
    if owner_type == "user":
        return owner_id
    return tg_user_id or owner_id


# ---------- связь с планировщиком бота ----------
# Дашборд работает в том же процессе, что и бот (bot.py вызывает
# register_dashboard_routes на своём aiohttp-приложении), но импортировать
# bot.py отсюда нельзя — он сам импортирует этот модуль, получился бы цикл.
# Поэтому bot.py при старте отдаёт сюда две свои функции (schedule_task и
# _remove_task_jobs) через set_scheduler_hooks. Без этого задача, созданная с
# дашборда, вообще не попадала в APScheduler — напоминание по ней не
# приходило до перезапуска процесса (restore_jobs), а удалённая с дашборда
# задача наоборот оставляла после себя живые джобы.
_schedule_task_hook = None
_unschedule_task_hook = None


def set_scheduler_hooks(schedule=None, unschedule=None):
    global _schedule_task_hook, _unschedule_task_hook
    _schedule_task_hook = schedule
    _unschedule_task_hook = unschedule


def _schedule_task(task_id: int, owner_id: int, owner_type: str, due_at: datetime,
                   repeat: str, remind: str, tz, remind_until_done: bool = False):
    """Хуки намеренно необязательны: тесты и запуск дашборда отдельным
    процессом работают и без планировщика — тогда это no-op, как было раньше.
    Сбой планирования не должен ронять уже выполненное действие в БД, поэтому
    исключение только логируется."""
    if _schedule_task_hook is None:
        return
    try:
        _schedule_task_hook(task_id, owner_id, owner_type, due_at.astimezone(UTC),
                            repeat, remind, tz, remind_until_done=remind_until_done)
    except Exception:
        logger.exception("Не удалось запланировать задачу %s, созданную с дашборда", task_id)


def _unschedule_task(task_id: int):
    if _unschedule_task_hook is None:
        return
    try:
        _unschedule_task_hook(task_id)
    except Exception:
        logger.exception("Не удалось снять джобы задачи %s с дашборда", task_id)


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
# Превью. В ленте одно фото показывается во всю ширину карточки (на телефоне
# это ~340 CSS-пикселей), а в карусели — вообще в 118px. Полный кадр на 1280px
# весит в среднем 250-400 КБ: пять фото в одной задаче — это под два мегабайта
# трафика ради картинок, которые физически не могут показать такую детализацию.
# 560px по длинной стороне хватает и для крупного показа на retina-телефоне, а
# весит такое превью примерно в пять раз меньше. Полный кадр остаётся и
# отдаётся только по тапу — в лайтбоксе.
TASK_PHOTO_THUMB_MAX_SIDE = 560
TASK_PHOTO_THUMB_QUALITY = 76


def compress_task_photo(raw: bytes) -> tuple[bytes, str] | None:
    """Сжимает присланные байты (откуда угодно — с дашборда или из бота) до
    JPEG разумного размера. Возвращает None, если это не декодируется как
    изображение (битый файл, не тот content-type) — вызывающая сторона должна
    явно отклонить загрузку в этом случае, а не падать с 500."""
    try:
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


def make_task_photo_thumb(data: bytes) -> bytes | None:
    """Делает превью из уже сжатого кадра (дешевле, чем из исходника). None —
    если кадр не декодируется; вызывающая сторона тогда просто живёт без
    превью и отдаёт полный файл, как раньше."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        img.thumbnail((TASK_PHOTO_THUMB_MAX_SIDE, TASK_PHOTO_THUMB_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=TASK_PHOTO_THUMB_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Не удалось сделать превью фото задачи")
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
    got_file = False
    csrf_token = ""
    file_bytes = b""
    # CSRF проверяется ПОСЛЕ цикла — порядок частей в multipart роли не играет;
    # читать байты до проверки безопасно, потому что сессия уже проверена выше,
    # а объём ограничен TASK_PHOTO_MAX_UPLOAD_BYTES.
    async for part in reader:
        if part.name == "csrf":
            csrf_token = (await part.read()).decode("utf-8", errors="ignore")
        elif part.name == "photo":
            got_file = True
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
    if not got_file or not file_bytes:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    if await db.count_task_photos(task_id) >= db.MAX_PHOTOS_PER_TASK:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    compressed = compress_task_photo(file_bytes)
    if compressed is None:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=bad_photo"})
    data, mime = compressed
    # Превью считаем здесь же, а не при первом показе: сжатие уже произошло,
    # картинка в памяти, лишних миллисекунд это почти не стоит.
    thumb = await asyncio.to_thread(make_task_photo_thumb, data)
    await db.add_task_photo(task_id, data, mime, uploaded_by=acting_user_id, thumb=thumb)
    await db.log_history(task_id, acting_user_id, task["title"], "photo_added")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


def _not_modified(request: aioweb.Request, etag: str) -> aioweb.Response | None:
    """304 вместо повторной отдачи тех же байтов. max-age у картинок — час, и
    раньше по его истечении телефон качал каждое фото целиком заново; теперь
    при неизменившемся ETag ответ — пустой 304."""
    if request.headers.get("If-None-Match") == etag:
        return aioweb.Response(status=304, headers={"ETag": etag})
    return None


def _image_headers(etag: str) -> dict:
    # private, а не public: доступ к картинке держится только на секретности
    # токена дашборда, и промежуточному прокси кешировать её и отдавать
    # кому-то ещё нельзя.
    return {"Cache-Control": f"private, max-age={AVATAR_CACHE_SECONDS}", "ETag": etag}


async def handle_task_photo_get(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/tasks/{task_id}/photo/{photo_id} — отдаёт байты.
    ?size=thumb отдаёт превью (его и просит лента), без параметра — полный
    кадр (его просит лайтбокс по тапу).

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

    want_thumb = request.query.get("size") == "thumb"
    etag = f'"{photo_id}-{"t" if want_thumb else "f"}"'
    cached = _not_modified(request, etag)
    if cached is not None:
        return cached

    if want_thumb:
        # Отдельный запрос без колонки data: превью весит десятки килобайт, а
        # полный кадр — сотни, и тащить его из БД, чтобы отдать превью, — это
        # ровно та же лишняя работа, только на стороне сервера.
        row = await db.get_task_photo_thumb(photo_id)
        if row is None or row[1] != task_id:
            return aioweb.Response(status=404)
        thumb = row[0]
        if thumb is None:
            # Фото загружено до появления превью (или пришло из бота) — делаем
            # превью сейчас и сохраняем, чтобы следующий показ обошёлся без
            # пересжатия. Только в этом случае и нужен полный кадр.
            full = await db.get_task_photo(photo_id)
            if not full or full["task_id"] != task_id:
                return aioweb.Response(status=404)
            thumb = await asyncio.to_thread(make_task_photo_thumb, full["data"])
            if thumb:
                await db.set_task_photo_thumb(photo_id, thumb)
            else:
                # Превью не сделалось (битый кадр) — отдаём как есть, лишь бы
                # картинка показалась.
                return aioweb.Response(body=full["data"], content_type=full["mime"],
                                       headers=_image_headers(etag))
        return aioweb.Response(body=thumb, content_type="image/jpeg", headers=_image_headers(etag))

    photo = await db.get_task_photo(photo_id)
    if not photo or photo["task_id"] != task_id:
        return aioweb.Response(status=404)

    return aioweb.Response(body=photo["data"], content_type=photo["mime"], headers=_image_headers(etag))


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

    etag = f'"av{user_id}"'
    cached = _not_modified(request, etag)
    if cached is not None:
        return cached

    photo = await db.get_telegram_user_photo(user_id)
    if not photo:
        return aioweb.Response(status=404)
    data, mime = photo
    return aioweb.Response(body=data, content_type=mime, headers=_image_headers(etag))


# ---------- статика: CSS отдельным кешируемым файлом ----------
# Раньше весь CSS (десять с лишним килобайт на каждой странице, а на странице
# задач — больше тридцати) инлайнился в HTML и качался заново при КАЖДОМ
# открытии и каждом редиректе после действия. Теперь он отдаётся отдельным
# файлом с хешем в имени и годовым immutable-кешем: телефон скачивает его
# один раз за всё время, а HTML страницы задач становится в разы меньше.
_CSS_CACHE_SECONDS = 365 * 24 * 3600
# имя файла -> (тело, ETag, предсжатое gzip-тело)
_STATIC_CSS: dict[str, tuple[bytes, str, bytes]] = {}


def _minify_css(css: str) -> str:
    """Срезает комментарии и лишние пробелы. Комментарии в этом файле —
    подробные и на русском (это ~3.5 КБ только в SHARED_CSS), читателю
    исходника они нужны, телефону — нет."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,])\s*", r"\1", css)
    css = re.sub(r"(?<=[{;:,])\s+", "", css)
    css = re.sub(r";}", "}", css)
    return css.strip()


def _publish_css(name: str, css: str) -> str:
    """Регистрирует CSS как статический файл и возвращает его URL. Хеш в имени
    делает кеш самоинвалидирующимся: правка стилей меняет URL, старый файл
    браузеру можно держать вечно."""
    # В исходнике фигурные скобки задвоены (CSS шёл через .format() вместе со
    # страницей) — для отдельного файла раздваиваем их обратно.
    body = _minify_css(css.replace("{{", "{").replace("}}", "}")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()[:12]
    filename = f"{name}.{digest}.css"
    # Содержимое статики не меняется за всё время жизни процесса, поэтому gzip
    # считаем ровно один раз здесь, при импорте, с максимальным уровнем — а не
    # заново на каждый запрос, как это делал бы общий middleware.
    _STATIC_CSS[filename] = (body, f'"{digest}"', gzip.compress(body, 9))
    return f"/dashboard/static/{filename}"


def _stylesheets(name: str, page_css: str) -> str:
    """Две таблицы стилей на страницу: общая база (одна для всех страниц — её
    телефон скачивает единожды и переиспользует при переходах) и небольшой
    блок конкретной страницы."""
    return (f'<link rel="stylesheet" href="{_BASE_CSS_URL}">\n'
            f'<link rel="stylesheet" href="{_publish_css(name, page_css)}">')


async def handle_static_css(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/static/{filename} — CSS не приватен (ни одной крупицы
    данных владельца в нём нет), поэтому отдаётся с public-кешем и без токена.
    ETag — чтобы даже при принудительном обновлении страницы возвращался 304
    с пустым телом, а не файл целиком."""
    entry = _STATIC_CSS.get(request.match_info["filename"])
    if entry is None:
        return aioweb.Response(status=404)
    body, etag, gz_body = entry
    if request.headers.get("If-None-Match") == etag:
        return aioweb.Response(status=304, headers={"ETag": etag})
    headers = {"Cache-Control": f"public, max-age={_CSS_CACHE_SECONDS}, immutable",
               "ETag": etag, "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("Accept-Encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        body = gz_body
    return aioweb.Response(body=body, content_type="text/css", charset="utf-8", headers=headers)


# ---------- сжатие ----------
# aiohttp по умолчанию не сжимает ничего. HTML дашборда — это текст с огромной
# долей повторов (однотипные карточки, одинаковые формы), он жмётся в 8-10 раз:
# 48 КБ -> ~12 КБ на одной задаче и 116 КБ -> ~13 КБ на тридцати. На мобильной
# сети это самая крупная экономия из всех возможных.
_COMPRESS_MIN_BYTES = 700
_COMPRESSIBLE_TYPES = ("text/html", "text/css", "application/json", "text/plain")


@aioweb.middleware
async def compression_middleware(request: aioweb.Request, handler):
    resp = await handler(request)
    try:
        if (isinstance(resp, aioweb.Response)
                and "Content-Encoding" not in resp.headers     # уже предсжато (статика)
                and resp.content_type in _COMPRESSIBLE_TYPES
                and (resp.content_length or 0) >= _COMPRESS_MIN_BYTES):
            # Кодировку выбираем явно. Сам enable_compression() без аргумента
            # перебирает ContentCoding в порядке объявления и выбрал бы
            # "deflate" даже тому клиенту, который просит gzip; gzip же
            # понимают абсолютно все и никакие прокси его не путают с
            # raw-deflate.
            accept = request.headers.get("Accept-Encoding", "").lower()
            if "gzip" in accept:
                resp.enable_compression(aioweb.ContentCoding.gzip)
            elif "deflate" in accept:
                resp.enable_compression(aioweb.ContentCoding.deflate)
            else:
                return resp
            vary = resp.headers.get("Vary")
            if not vary:
                resp.headers["Vary"] = "Accept-Encoding"
            elif "accept-encoding" not in vary.lower():
                resp.headers["Vary"] = f"{vary}, Accept-Encoding"
    except (AttributeError, TypeError):
        # Потоковые/нестандартные ответы просто не сжимаем — отдать ответ
        # важнее, чем сэкономить на нём байты.
        pass
    return resp


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
        "error_bad_due_at": "Не удалось разобрать дату и время",
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
        "nav_templates": "📑 Шаблоны",
        "templates_page_title": "Шаблоны — Task Reminder",
        "templates_heading": "📑 Шаблоны задач",
        "templates_empty": "Шаблонов пока нет.",
        "btn_apply_template": "Создать задачу",
        "btn_delete_template": "Удалить",
        "confirm_delete_template": "Удалить шаблон?",
        "template_offset_today": "сегодня",
        "template_offset_tomorrow": "завтра",
        "template_offset_days": "через {n} дн.",
        "new_template_title_placeholder": "Название шаблона…",
        "new_template_offset_label": "Смещение в днях (0 = сегодня)",
        "btn_save_template": "Сохранить шаблон",
        "error_empty_template_title": "Введите название шаблона",
        "template_limit_reached": "Достигнут лимит шаблонов ({n}).",
        "new_description_placeholder": "Описание (необязательно)…",
        "description_placeholder": "Добавить описание…",
        "btn_save_description": "Сохранить",
        "edit_description_label": "✏️ Изменить описание",
        "add_description_label": "✏️ Добавить описание",
        "repeats_label": "🔁 Повторяется: {label}",
        "no_repeat_label": "Не повторяется",
        "new_subtask_placeholder": "Новый пункт…",
        "btn_add_subtask": "Добавить",
        "confirm_delete_subtask": "Удалить пункт?",
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
        "error_bad_due_at": "Couldn't parse the date and time",
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
        "nav_templates": "📑 Templates",
        "templates_page_title": "Templates — Task Reminder",
        "templates_heading": "📑 Task templates",
        "templates_empty": "No templates yet.",
        "btn_apply_template": "Create task",
        "btn_delete_template": "Delete",
        "confirm_delete_template": "Delete this template?",
        "template_offset_today": "today",
        "template_offset_tomorrow": "tomorrow",
        "template_offset_days": "in {n} days",
        "new_template_title_placeholder": "Template title…",
        "new_template_offset_label": "Days offset (0 = today)",
        "btn_save_template": "Save template",
        "error_empty_template_title": "Enter a template title",
        "template_limit_reached": "Template limit reached ({n}).",
        "new_description_placeholder": "Description (optional)…",
        "description_placeholder": "Add a description…",
        "btn_save_description": "Save",
        "edit_description_label": "✏️ Edit description",
        "add_description_label": "✏️ Add description",
        "repeats_label": "🔁 Repeats: {label}",
        "no_repeat_label": "Doesn't repeat",
        "new_subtask_placeholder": "New item…",
        "btn_add_subtask": "Add",
        "confirm_delete_subtask": "Delete this item?",
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
        "error_bad_due_at": "Nie udało się odczytać daty i godziny",
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
        "nav_templates": "📑 Szablony",
        "templates_page_title": "Szablony — Task Reminder",
        "templates_heading": "📑 Szablony zadań",
        "templates_empty": "Brak szablonów.",
        "btn_apply_template": "Utwórz zadanie",
        "btn_delete_template": "Usuń",
        "confirm_delete_template": "Usunąć szablon?",
        "template_offset_today": "dzisiaj",
        "template_offset_tomorrow": "jutro",
        "template_offset_days": "za {n} dni",
        "new_template_title_placeholder": "Nazwa szablonu…",
        "new_template_offset_label": "Przesunięcie w dniach (0 = dzisiaj)",
        "btn_save_template": "Zapisz szablon",
        "error_empty_template_title": "Wpisz nazwę szablonu",
        "template_limit_reached": "Osiągnięto limit szablonów ({n}).",
        "new_description_placeholder": "Opis (opcjonalnie)…",
        "description_placeholder": "Dodaj opis…",
        "btn_save_description": "Zapisz",
        "edit_description_label": "✏️ Edytuj opis",
        "add_description_label": "✏️ Dodaj opis",
        "repeats_label": "🔁 Powtarza się: {label}",
        "no_repeat_label": "Nie powtarza się",
        "new_subtask_placeholder": "Nowy punkt…",
        "btn_add_subtask": "Dodaj",
        "confirm_delete_subtask": "Usunąć punkt?",
    },
}


def _dt(lang: str) -> dict:
    return DASHBOARD_TEXTS.get(lang, DASHBOARD_TEXTS["ru"])


# Наборы текстов, пригодные для прямой передачи в str.format(**...): без
# вложенного словаря "repeat" (он нужен только рендеру задач) и, во втором
# варианте, без заголовков, которые handle_dashboard передаёт отдельным
# аргументом. Раньше эти словари пересобирались на каждый рендер страницы —
# теперь считаются один раз при импорте модуля.
_TEXTS_FLAT = {
    lang: {k: v for k, v in texts.items() if k != "repeat"}
    for lang, texts in DASHBOARD_TEXTS.items()
}
_TEXTS_TASKS_PAGE = {
    lang: {k: v for k, v in flat.items() if k not in ("heading", "heading_chat")}
    for lang, flat in _TEXTS_FLAT.items()
}


def _dt_flat(lang: str) -> dict:
    return _TEXTS_FLAT.get(lang, _TEXTS_FLAT["ru"])


def _dt_tasks_page(lang: str) -> dict:
    return _TEXTS_TASKS_PAGE.get(lang, _TEXTS_TASKS_PAGE["ru"])


# Шрифты: было 11 отдельных начертаний (Fraunces 6 + Inter 5) — 11 файлов и
# render-blocking CSS-запрос на сторонний домен перед первой отрисовкой. Стало
# три вариативных файла по реально используемым диапазонам (Fraunces 600-700
# прямой + 400-600 курсив, Inter 400-600), а сама таблица стилей грузится
# неблокирующе: media="print" + onload переключает её на all, когда она
# приехала. Текст виден сразу системным шрифтом и подменяется по display=swap,
# не задерживая первый кадр.
_FONT_CSS_URL = ("https://fonts.googleapis.com/css2?"
                 "family=Fraunces:ital,wght@0,600..700;1,400..600&family=Inter:wght@400..600&display=swap")
_FONT_HEAD = f"""<meta name="color-scheme" content="dark">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{_FONT_CSS_URL}" media="print" onload="this.media='all'">
<noscript><link rel="stylesheet" href="{_FONT_CSS_URL}"></noscript>"""


# Общие дизайн-токены и база — единый визуальный язык под Telegram (тёмная
# тема, закруглённые карточки, мягкие тени, лёгкое появление карточек и
# отклик на тап), без реальной интеграции Telegram WebApp SDK. Подставляется
# как есть во все три шаблона до их .format() — поэтому фигурные скобки уже
# задвоены, как и в остальных шаблонах этого файла.
SHARED_CSS = """
:root {{
  /* "Тёмное стекло" — матовые полупрозрачные панели (backdrop-filter) над
     почти чёрным фоном с двумя приглушёнными цветными пятнами (медно-янтарным
     и глубоким изумрудным — НЕ фиолетово-голубой градиент, это сочетание
     сейчас слишком узнаваемо как "дефолт ИИ-лендинга"). Заголовки — Fraunces
     (курсивная антиква с характерными засечками) вместо геометрического
     гротеска на каждом углу — ещё один сознательный уход от шаблонного вида.
     backdrop-filter навешен ТОЛЬКО на крупные панели-контейнеры (карточки
     задач/событий, форма логина, панель дней) — на мелких элементах (кружки
     чекбоксов, стрелки фото, аватары) его нет: это и держит рендер лёгким на
     телефоне (у каждого blur-слоя своя цена для композитора), и визуально
     мелкий элемент всё равно не читается как "стекло" на таком размере.
     Токены (имена переменных) не менялись — поменялись их значения, плюс
     один новый --on-accent (непрозрачный цвет текста/иконок поверх залитых
     акцентом элементов — var(--card) для этой роли больше не годится, он
     теперь полупрозрачный). */
  --bg-0: #0b0a0d;
  --bg-1: #1a140f;
  --card: rgba(255, 255, 255, 0.055);
  --card-edge: rgba(255, 255, 255, 0.12);
  --ink: #f4efe6;
  --ink-soft: rgba(244, 239, 230, 0.6);
  --paper: #f4efe6;
  --paper-soft: rgba(244, 239, 230, 0.56);
  --stamp: #d97a26;
  --stamp-deep: #b8631a;
  --ok: #14a87e;
  --ok-deep: #0d8565;
  --on-accent: #fbf5ea;
  --rule: rgba(255, 255, 255, 0.14);
  --rule-dark: rgba(255, 255, 255, 0.22);
  --shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1), 0 24px 48px -26px rgba(0, 0, 0, 0.75);
  --glass-blur: blur(14px) saturate(130%);
  --radius: 20px;
  --radius-sm: 12px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 28px;
  --font-display: "Fraunces", Georgia, "Times New Roman", serif;
  --font-body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
* {{ box-sizing: border-box; }}
html {{ background: var(--bg-0); }}
body {{
  font-family: var(--font-body);
  /* background-attachment: fixed было главным убийцей производительности
     в паре с backdrop-filter — на каждый кадр скролла браузер обязан
     пересчитывать блюр заново, потому что "фиксированный" фон формально
     движется относительно скроллящегося контента. Заменено статичным
     градиентом (не двигается на коротких страницах вроде этой, а на
     длинных просто мягко уезжает вместе с контентом — не критично визуально,
     зато дешёво для рендера). */
  background:
    radial-gradient(640px 420px at 12% -6%, rgba(217, 122, 38, 0.22), transparent 60%),
    radial-gradient(560px 460px at 108% 18%, rgba(20, 168, 126, 0.16), transparent 62%),
    radial-gradient(900px 700px at 50% 115%, rgba(217, 122, 38, 0.08), transparent 70%),
    var(--bg-0);
  color: var(--paper); margin: 0;
  padding: var(--space-4);
  padding-top: max(var(--space-4), env(safe-area-inset-top));
  padding-bottom: max(var(--space-5), env(safe-area-inset-bottom));
  max-width: 640px; margin-left: auto; margin-right: auto;
  -webkit-font-smoothing: antialiased;
  position: relative;
}}
/* Тонкое зерно поверх фона — убирает "идеально гладкий цифровой градиент",
   из-за которого плоские тёмные интерфейсы и читаются как сгенерированные.
   Один statичный SVG-шум через data-URI, без JS и без перерисовки. */
body::before {{
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  opacity: 0.045; mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}}
body > * {{ position: relative; z-index: 1; }}
h1 {{
  font-family: var(--font-display); font-weight: 600; font-style: italic;
  font-size: 1.6rem; margin: 0 0 var(--space-4); letter-spacing: -0.01em;
  color: var(--paper);
}}
/* touch-action: manipulation отключает ожидание возможного двойного тапа
   (double-tap to zoom) — на мобильных браузерах это снимает знаменитую
   задержку ~300 мс между касанием и реакцией кнопки. */
button, .task-summary, .day-picker-summary, a {{ touch-action: manipulation; }}
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
  transition: transform 0.25s cubic-bezier(.22,1,.36,1), box-shadow 0.25s ease, border-color 0.2s ease;
}}
/* content-visibility: браузер пропускает layout/paint карточек, которых
   сейчас нет на экране (длинный список задач/истории) — почти бесплатное
   ускорение первой отрисовки и скролла в Chrome/Edge; там, где свойство не
   поддерживается (Safari), просто игнорируется, ничего не ломая. */
.task, .event {{ content-visibility: auto; contain-intrinsic-size: 0 120px; }}
/* Живой backdrop-filter — дорогая штука: на каждый кадр скролла браузер
   пересчитывает блюр под элементом заново, а у .task/.event таких
   элементов может быть десятки одновременно на экране — именно это и
   тормозило страницу. Поэтому настоящее стекло (var(--glass-blur))
   оставлено только на одиночных "херо" панелях — форма входа, форма
   добавления задачи, график, баннер Telegram, панель дней: их на странице
   всегда ровно одна-две штуки, цена блюра там фиксированная и небольшая.
   У карточек списка — просто полупрозрачная заливка без блюра: на глаз,
   в вертикальном списке разница почти незаметна (фон за карточкой и так
   малоконтрастный), а разница в производительности на длинных списках —
   огромная. */
.chart-block, .tg-widget-banner, .new-task-form, .day-picker, #login-form {{
  backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
}}
/* Браузеры без backdrop-filter (редкость, но есть) получают сплошную тёмную
   панель вместо почти прозрачного фона — иначе текст на --ink (светлый)
   лёг бы прямо на фоновые пятна без разделения. */
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {{
  .task, .event, .chart-block, .tg-widget-banner, .new-task-form, .day-picker, #login-form {{
    background: #1c1812;
  }}
}}
/* Вся эта страница раньше жила только на :active (тач) — на мышке/трекпаде
   ничего не реагировало на наведение и выглядело "мёртво". (hover: hover)
   and (pointer: fine) — чтобы эти эффекты включались именно на ПК, а не
   залипали на телефоне, где :hover срабатывает на тап и не снимается
   сама собой до следующего касания. */
@media (hover: hover) and (pointer: fine) {{
  .task:hover, .event:hover {{
    transform: translateY(-3px);
    border-color: rgba(217, 122, 38, 0.35);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.12), 0 28px 54px -24px rgba(0, 0, 0, 0.8);
  }}
  .task.done:hover, .event.done:hover {{ border-color: rgba(20, 168, 126, 0.4); }}
  /* Блик, скользящий по карточке при наведении — узнаваемая "жидкостная"
     деталь стекла, но дешёвая: это просто анимация background-position
     одной уже существующей карточки под курсором, а не постоянный эффект
     на всех карточках сразу. */
  .task, .event {{
    background-image: linear-gradient(115deg, transparent 20%, rgba(255, 255, 255, 0.07) 36%, transparent 52%);
    background-size: 220% 100%; background-position: 120% 0; background-repeat: no-repeat;
    transition: transform 0.25s cubic-bezier(.22,1,.36,1), box-shadow 0.25s ease, border-color 0.2s ease,
                background-position 0.6s ease;
  }}
  .task:hover, .event:hover {{ background-position: -20% 0; }}
  .task-summary:hover .title {{ color: var(--stamp); transition: color 0.15s ease; }}
  .edit-toggle summary:hover {{ color: var(--stamp); }}
  .task-photo:hover img {{ transform: scale(1.08); }}
  .photo-nav:hover {{ border-color: var(--stamp); color: var(--stamp); }}
  .photo-lightbox-close:hover {{ background: var(--stamp); }}
  .check-btn:hover {{ border-color: var(--stamp); transform: scale(1.1); }}
  .task.done .check-btn:hover, .task.overdue .check-btn:hover {{ transform: scale(1.1); }}
  .subtask-check:hover {{ border-color: var(--stamp); }}
  .nav-link:hover {{ opacity: 0.75; transform: translateX(2px); transition: opacity 0.15s ease, transform 0.15s ease; }}
  button[type=submit]:hover, .desc-form button:hover, .subtask-add-form button:hover,
  .new-task-form button:hover {{ filter: brightness(1.12); transition: filter 0.15s ease; }}
  .task-actions button:hover {{ color: var(--stamp); }}
  .task-actions .del-btn:hover {{ color: var(--stamp-deep); }}
  .task-summary:hover::after {{ border-color: var(--stamp); }}
  .chart-block:hover, .day-picker:hover {{ border-color: var(--card-edge); box-shadow: var(--shadow); }}
  a.nav-link, button, .task-summary, .check-btn, .subtask-check {{ cursor: pointer; }}
}}
{stagger_rules}
/* ---------- мобильный бюджет рендера ----------
   Всё, что ниже, адресовано именно телефону. Три самые дорогие для
   мобильного композитора вещи на этой странице:
   1) backdrop-filter — каждый такой слой заставляет браузер заново читать
      то, что под ним, и блюрить это на каждый кадр скролла. Радиус 14px на
      мобильном GPU стоит заметно дороже 7px (цена блюра растёт с радиусом),
      а на экране 360px разницы на глаз почти нет.
   2) mix-blend-mode у зернового оверлея — блендинг фиксированного слоя во
      всю высоту экрана пересчитывается при каждом кадре скролла. На телефоне
      зерно и так неразличимо, поэтому там оно просто выключается.
   3) staggered-анимация появления карточек — на слабом CPU 12 отложенных
      анимаций заметно растягивают первую отрисовку; оставляем короткое
      общее появление без каскада.
   Ничего из этого не меняет вид страницы на настольном браузере. */
@media (max-width: 700px) {{
  :root {{ --glass-blur: blur(7px) saturate(120%); }}
  body::before {{ display: none; }}
  .task, .event, .chart-block, .tg-widget-banner, .new-task-form {{ animation-duration: 0.18s; }}
  .task:nth-child(n), .event:nth-child(n) {{ animation-delay: 0s; }}
  /* Тени дешевле считать с меньшим радиусом размытия: тень в 48px на
     каждой карточке — это большая область перерисовки при скролле. */
  :root {{ --shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1), 0 14px 26px -18px rgba(0, 0, 0, 0.8); }}
}}
/* Экономия батареи и кадров для тех, кто попросил систему меньше двигать
   картинку: блюр полностью снимаем (prefers-reduced-motion часто включают
   как раз на слабых устройствах). */
@media (prefers-reduced-motion: reduce) {{
  :root {{ --glass-blur: none; }}
  body::before {{ display: none; }}
}}
/* Лайтбокс блюрит фон во ВСЮ площадь экрана с радиусом 22px — на телефоне это
   самый дорогой отдельный эффект на странице (и единственный, который виден
   сразу после тапа, когда важна отзывчивость). Радиус режем, фон за счёт
   этого делаем чуть плотнее — на глаз результат тот же. */
@media (max-width: 700px) {{
  .photo-lightbox {{
    background: rgba(7, 6, 8, 0.94);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  }}
}}
@media (prefers-reduced-motion: reduce) {{
  .photo-lightbox {{ background: rgba(7, 6, 8, 0.97); backdrop-filter: none; -webkit-backdrop-filter: none; }}
}}
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

# Базовая таблица стилей — одна для всех страниц, поэтому при переходах
# задачи -> история -> шаблоны она берётся из кеша, а не качается снова.
_BASE_CSS_URL = _publish_css("base", SHARED_CSS)


LOGIN_CSS = """
body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
form {{
  background: var(--card); color: var(--ink); padding: 2rem 1.75rem;
  border-radius: var(--radius); box-shadow: var(--shadow); border: 1px solid var(--card-edge);
  backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
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
  background: var(--stamp); color: var(--on-accent); font-weight: 600; cursor: pointer; font-size: 0.95rem;
}}
button[type=submit]:active {{ background: var(--stamp-deep); }}
.error {{ color: var(--stamp); font-size: 0.85rem; margin-top: 10px; font-family: var(--font-body); }}
h2 {{ font-family: var(--font-display); font-style: italic; font-weight: 600; margin: 0 0 1.1rem; font-size: 1.35rem; color: var(--ink); }}
"""


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{login_title}</title>
""" + _FONT_HEAD + """
<!--stylesheets-->
</head>
<body>
<form method="post" action="{login_action}" id="login-form">
<h2>{login_heading}</h2>
<input type="password" name="password" placeholder="{password_placeholder}" autofocus>
<button type="submit">{btn_login}</button>
{error}
</form>
</body></html>"""


def _login_response(token: str, lang: str, error_html: str = "", status: int = 200) -> aioweb.Response:
    """Единая точка отдачи формы входа. Раньше четыре места собирали её
    вручную, и у формы не было action — POST уходил на текущий URL, а для
    /history и /templates POST-маршрута нет: пользователь, попавший сразу на
    эти страницы, вводил пароль и получал 405 вместо входа."""
    page = LOGIN_PAGE.format(
        error=error_html, login_action=f"/dashboard/{token}", **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", status=status,
                           headers={"Cache-Control": "no-store"})


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


TASKS_CSS = """
.task {{ padding: var(--space-4); margin-bottom: var(--space-3);
        display: flex; gap: var(--space-3); align-items: flex-start; }}
.task.done {{ background: rgba(20, 168, 126, 0.1); border-color: rgba(20, 168, 126, 0.3); }}
.task.done .meta {{ color: var(--ink-soft); opacity: 0.8; }}
.task.overdue .meta {{ color: var(--stamp); }}
.task form {{ margin: 0; line-height: 0; }}
/* Хит-область чекбокса увеличена невидимым ::before на ~44px (минимум для
   уверенного тапа пальцем) без изменения видимого диаметра кружка — чистый
   мобильный UX приём, 26px было бы мелковато под палец. */
.check-btn {{
  width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--ink-soft);
  background: transparent; cursor: pointer; flex-shrink: 0; margin-top: 2px; padding: 0;
  position: relative; transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}}
.check-btn::before {{ content: ""; position: absolute; inset: -9px; }}
.task.overdue .check-btn {{ border-color: var(--stamp); }}
.task.done .check-btn {{ background: var(--ok); border-color: var(--ok); }}
.task.done .check-btn::after {{
  content: ""; position: absolute; left: 8px; top: 4px; width: 6px; height: 11px;
  border: solid var(--on-accent); border-width: 0 2px 2px 0;
  transform: rotate(40deg) scale(0); transform-origin: bottom left;
  animation: check-draw 0.3s ease forwards 0.05s;
}}
@keyframes check-draw {{ to {{ transform: rotate(40deg) scale(1); }} }}
/* Карточка-раскрывашка: <details>/<summary> без единой строчки JS — тап по
   заголовку/мете (summary) раскрывает описание и действия. Чекбокс живёт
   вне <details> как отдельная форма, чтобы клик по нему не дублировал
   toggle раскрытия. */
.task-details {{ flex: 1; min-width: 0; }}
.task-summary {{
  list-style: none; cursor: pointer; display: block; position: relative;
  padding-right: 22px; -webkit-tap-highlight-color: transparent;
}}
.task-summary::-webkit-details-marker {{ display: none; }}
.task-summary::after {{
  content: ""; position: absolute; right: 2px; top: 6px; width: 8px; height: 8px;
  border-right: 2px solid var(--ink-soft); border-bottom: 2px solid var(--ink-soft);
  transform: rotate(45deg); transition: transform 0.2s ease; transform-origin: center;
}}
.task-details[open] > .task-summary::after {{ transform: rotate(-135deg); top: 9px; }}
.task-summary:focus-visible {{ outline: 2px solid var(--stamp); outline-offset: 2px; }}
.title {{ font-family: var(--font-display); font-weight: 700; font-size: 1.15rem; line-height: 1.35; color: var(--ink);
         position: relative; display: inline-block; }}
.task.done .title {{ color: var(--ink-soft); }}
.task.done .title::after {{
  content: ""; position: absolute; left: 0; top: 50%; height: 1.5px; width: 100%;
  background: var(--ok); transform: scaleX(0); transform-origin: left;
  animation: strike 0.35s ease forwards 0.1s;
}}
@keyframes strike {{ to {{ transform: scaleX(1); }} }}
.meta {{ color: var(--ink-soft); font-size: 0.85rem; margin-top: 4px; }}
/* Разделитель — мягкий градиент-растворение вместо пунктира: пунктир
   читался как "бухгалтерская книга", а не как стекло. */
.task-expanded {{
  margin-top: 12px; padding-top: 13px; position: relative; transform-origin: top center;
}}
.task-expanded::before {{
  content: ""; position: absolute; top: 0; left: -2px; right: -2px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--rule) 15%, var(--rule) 85%, transparent);
}}
/* Долгое, многослойное появление содержимого карточки при раскрытии —
   специально небыстрое (0.6с) и "дорогое" на вид: стекло "материализуется"
   из блюра с лёгким приседанием по Y, а не просто мгновенно появляется.
   Триггерится само по себе в поддерживающих браузерах, потому что контент
   нативного <details> физически не существует в дереве рендера, пока он
   закрыт, — в момент открытия браузер видит элемент как "только что
   вставленный" и честно проигрывает его entrance-animation. Там, где это
   не так (очень старые браузеры) — контент просто появляется мгновенно,
   деградация без потери функциональности. */
@keyframes glass-materialize {{
  0%   {{ opacity: 0; transform: translateY(-16px) scale(0.95); filter: blur(12px); }}
  55%  {{ opacity: 1; filter: blur(0); }}
  100% {{ opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }}
}}
.task-expanded {{ animation: glass-materialize 0.65s cubic-bezier(.16,1,.3,1) both; }}
/* Лёгкий каскад: содержимое внутри не вываливается одним куском, а
   "досыпается" друг за другом с небольшой задержкой — тот самый
   ощутимо-небыстрый, многоступенчатый эффект. */
.task-expanded > * {{
  animation: fade-up 0.45s cubic-bezier(.16,1,.3,1) both;
  animation-delay: 0.12s;
}}
.task-expanded > *:nth-child(2) {{ animation-delay: 0.17s; }}
.task-expanded > *:nth-child(3) {{ animation-delay: 0.22s; }}
.task-expanded > *:nth-child(4) {{ animation-delay: 0.27s; }}
.task-expanded > *:nth-child(5) {{ animation-delay: 0.32s; }}
.task-expanded > *:nth-child(6) {{ animation-delay: 0.37s; }}
.task-expanded > *:nth-child(n+7) {{ animation-delay: 0.4s; }}
.repeat-chip, .tag {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 11px; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  font-family: var(--font-body); font-style: normal; line-height: 1.4;
}}
.repeat-chip {{ color: var(--ink-soft); background: rgba(255, 255, 255, 0.05); border: 1px solid var(--card-edge); margin-bottom: 10px; }}
.description-text {{ color: var(--ink); font-size: 0.92rem; line-height: 1.45; margin: 0 0 10px; white-space: pre-wrap; word-break: break-word; }}
.edit-toggle {{ margin-bottom: 10px; }}
.edit-toggle summary {{
  list-style: none; cursor: pointer; display: inline-block; -webkit-tap-highlight-color: transparent;
  color: var(--ink-soft); font-size: 0.82rem; font-family: var(--font-body);
}}
.edit-toggle summary::-webkit-details-marker {{ display: none; }}
.edit-toggle[open] summary {{ color: var(--stamp); margin-bottom: 8px; }}
.desc-form {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 4px;
    animation: fade-up 0.35s cubic-bezier(.16,1,.3,1) both; }}
.desc-form textarea {{
  width: 100%; padding: 8px; border: 1px solid var(--rule); border-radius: var(--radius-sm);
  background: rgba(0, 0, 0, 0.22); color: var(--ink); font-family: var(--font-body); font-size: 0.9rem;
  box-sizing: border-box; resize: vertical; min-height: 44px;
}}
.desc-form textarea:focus {{ outline: none; border-color: var(--stamp); }}
.desc-form button {{
  align-self: flex-start; padding: 7px 14px; border-radius: var(--radius-sm); border: none;
  background: var(--card-edge); color: var(--ink); font-size: 0.82rem; font-weight: 600; cursor: pointer;
}}
.desc-form button:active {{ background: var(--card-edge); }}
/* Подзадачи — тот же язык анимации, что и у главного чекбокса задачи
   (вырисовывающаяся галочка + бегущая линия-зачёркивание), только компактнее,
   раз пунктов в списке обычно несколько. */
.subtasks {{ display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }}
.subtask-row {{ display: flex; align-items: center; gap: 8px; }}
.subtask-row form {{ margin: 0; line-height: 0; }}
.subtask-check {{
  width: 18px; height: 18px; border-radius: 50%; border: 1.5px solid var(--ink-soft);
  background: transparent; cursor: pointer; flex-shrink: 0; padding: 0; position: relative;
  transition: background 0.15s ease, border-color 0.15s ease;
}}
.subtask-check::before {{ content: ""; position: absolute; inset: -11px; }}
.subtask-row.done .subtask-check {{ background: var(--ok); border-color: var(--ok); }}
.subtask-row.done .subtask-check::after {{
  content: ""; position: absolute; left: 5px; top: 2px; width: 4px; height: 8px;
  border: solid var(--on-accent); border-width: 0 1.5px 1.5px 0;
  transform: rotate(40deg) scale(0); transform-origin: bottom left;
  animation: check-draw 0.3s ease forwards 0.05s;
}}
.subtask-title {{
  flex: 1; min-width: 0; font-size: 0.9rem; color: var(--ink); position: relative;
  overflow-wrap: break-word;
}}
.subtask-row.done .subtask-title {{ color: var(--ink-soft); }}
.subtask-row.done .subtask-title::after {{
  content: ""; position: absolute; left: 0; top: 50%; height: 1.5px; width: 100%;
  background: var(--ok); transform: scaleX(0); transform-origin: left;
  animation: strike 0.35s ease forwards 0.1s;
}}
.subtask-del-btn {{
  background: none; border: none; color: var(--ink-soft); font-size: 0.8rem; line-height: 1;
  cursor: pointer; padding: 4px; flex-shrink: 0;
}}
.subtask-del-btn:active {{ color: var(--stamp-deep); }}
.subtask-add-form {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }}
.subtask-add-form input[type=text] {{
  flex: 1; min-width: 0; padding: 6px 2px; border: none; border-bottom: 1.5px solid var(--rule);
  background: transparent; color: var(--ink); font-family: var(--font-body); font-size: 0.88rem;
}}
.subtask-add-form input[type=text]:focus {{ outline: none; border-bottom-color: var(--stamp); }}
.subtask-add-form input[type=text]::placeholder {{ color: var(--ink-soft); }}
.subtask-add-form button {{
  flex-shrink: 0; padding: 6px 12px; border-radius: var(--radius-sm); border: none;
  background: var(--card-edge); color: var(--ink); font-size: 0.8rem; font-weight: 600; cursor: pointer;
}}
.subtask-add-form button:active {{ background: var(--card-edge); }}
.task-actions {{ display: flex; gap: 16px; margin-top: 10px; }}
.task-actions button {{
  background: none; border: none; color: var(--ink-soft); font-size: 0.82rem;
  font-family: var(--font-body); cursor: pointer; padding: 0;
  transition: color 0.15s ease;
}}
.task-actions button:active {{ color: var(--stamp); }}
.task-actions .del-btn:active {{ color: var(--stamp-deep); }}
.tag {{ color: var(--stamp-deep); background: rgba(217, 122, 38, 0.14); border: 1px solid rgba(217, 122, 38, 0.3); margin-top: 6px; }}
.task-author {{ display: flex; align-items: center; gap: 6px; margin-top: 8px;
               font-size: 0.8rem; color: var(--ink-soft); }}
.avatar {{ width: 20px; height: 20px; border-radius: 50%; object-fit: cover;
          background: var(--card-edge); border: 1.5px solid var(--on-accent); outline: 1px solid var(--rule);
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
.new-task-form input[type=text]::placeholder, .new-task-form textarea::placeholder {{ color: var(--ink-soft); }}
.new-task-form textarea {{
  width: 100%; padding: 8px 2px; border: none; border-bottom: 1.5px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--ink); font-family: var(--font-body); font-size: 0.95rem;
  box-sizing: border-box; resize: vertical; min-height: 44px;
}}
.new-task-form textarea:focus {{ outline: none; border-bottom-color: var(--stamp); }}
.new-task-form input[type=datetime-local] {{
  padding: 8px 2px; border: none; border-bottom: 1.5px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--ink); font-family: var(--font-body); color-scheme: light; box-sizing: border-box;
}}
.new-task-form input:focus {{ outline: none; border-bottom-color: var(--stamp); }}
.new-task-form button {{
  padding: 10px 16px; border-radius: var(--radius-sm); border: none; background: var(--stamp);
  color: var(--on-accent); font-weight: 600; cursor: pointer; flex-shrink: 0;
}}
.new-task-form button:active {{ background: var(--stamp-deep); }}
.new-task-error {{ color: var(--stamp); font-size: 0.85rem; margin: -0.5rem 0 0.25rem; font-family: var(--font-body); }}
.tg-widget-banner {{ padding: 0.8rem 1rem; margin-bottom: var(--space-4);
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }}
.tg-widget-banner span {{ font-size: 0.85rem; color: var(--ink-soft); font-family: var(--font-body); }}
.nav-link {{ display: inline-block; color: var(--paper-soft); text-decoration: none; font-size: 0.85rem; margin-bottom: var(--space-4);
    font-family: var(--font-body); }}
.event {{ padding: 0.85rem 1rem; margin-bottom: 0.6rem; display: flex; gap: 0.65rem; align-items: center; }}
.event-body {{ flex: 1; min-width: 0; }}
.event-line {{ font-size: 0.92rem; color: var(--ink); }}
.event-line .ev-title {{ font-weight: 600; font-family: var(--font-display); }}
.event-time {{ color: var(--ink-soft); font-size: 0.78rem; margin-top: 2px; }}
.load-more {{ display: block; width: 100%; padding: 10px; border-radius: var(--radius-sm); border: 1px dashed var(--rule-dark);
    background: transparent; color: var(--paper-soft); text-align: center; text-decoration: none; margin-top: 0.5rem; box-sizing: border-box;
    font-family: var(--font-body); }}
.load-more:active {{ border-color: var(--stamp); color: var(--stamp); }}
@media (max-width: 480px) {{ .new-task-form button {{ width: 100%; }} }}
/* Карусель фото: viewport шириной ровно 3 кадра, остальные уезжают за
   overflow-x и достаются scroll-snap'ом по клику на стрелки — без JS-таймера
   и зацикливания, пользователь листает сам. */
.task-photos-wrap {{
  position: relative; margin-top: 12px; margin-bottom: 14px; padding-top: 10px;
  padding-left: 30px; padding-right: 30px; border-top: 1px solid var(--rule);
}}
/* Одно фото: нет карусели — нет и стрелок, боковые поля под них не нужны,
   а сам кадр растягивается на всю ширину карточки (см. .task-photo-hero). */
.task-photos-wrap.hero {{ padding-left: 0; padding-right: 0; }}
.task-photos-wrap.hero .task-photos {{ width: 100%; }}
.task-photos {{
  display: flex; gap: 8px; overflow-x: auto; scroll-snap-type: x mandatory;
  /* Чтобы горизонтальный свайп по карусели не "перетекал" в скролл всей
     страницы, когда карусель доехала до края — на телефоне это главный
     источник ощущения "залипающего" скролла. */
  overscroll-behavior-x: contain;
  width: calc(3 * 118px + 2 * 8px); max-width: 100%;
  scrollbar-width: none; -ms-overflow-style: none;
}}
.task-photos::-webkit-scrollbar {{ display: none; }}
.task-photo {{ position: relative; width: 118px; height: 118px; border-radius: var(--radius-sm); overflow: hidden;
    border: 1px solid var(--card-edge); flex-shrink: 0; scroll-snap-align: start; cursor: pointer; }}
.task-photo img {{ width: 100%; height: 100%; object-fit: cover; display: block; transition: transform 0.3s ease; }}
/* Одно фото — не мельчим в квадратик карусели, а показываем крупно во всю
   ширину карточки: это и есть главный выигрыш в "насколько хорошо видно
   фото" для самого частого случая (одно фото на задачу). */
.task-photo.task-photo-hero {{ width: 100%; height: auto; aspect-ratio: 16 / 10; }}
.task-photo form {{ position: absolute; top: 4px; right: 4px; line-height: 0; }}
.photo-del-btn {{ width: 20px; height: 20px; border-radius: 50%; border: none; background: rgba(43, 35, 23, 0.65);
    color: var(--on-accent); font-size: 12px; line-height: 1; cursor: pointer; padding: 0; }}
.photo-del-btn:active {{ background: var(--stamp-deep); }}
/* Стрелки вынесены в собственные 30px-поля слева/справа (padding на .task-photos-wrap
   выше) — не наезжают на крайние фото и на крестики удаления. */
.photo-nav {{
  position: absolute; top: 50%; transform: translateY(-50%); width: 26px; height: 26px; border-radius: 50%;
  border: 1px solid var(--card-edge); background: var(--card); color: var(--ink); font-size: 16px;
  line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center;
  box-shadow: var(--shadow); padding: 0;
}}
.photo-nav-prev {{ left: 0; }}
.photo-nav-next {{ right: 0; }}
.photo-nav:active {{ background: var(--card-edge); }}
/* Полноэкранный просмотр фото — скрыт (display:none) до .open, тап по фону
   или крестику закрывает. Картинка вписывается в экран с сохранением пропорций,
   никакого внешнего JS/библиотек. */
.photo-lightbox {{
  display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(7, 6, 8, 0.86);
  backdrop-filter: blur(22px) saturate(120%); -webkit-backdrop-filter: blur(22px) saturate(120%);
  align-items: center; justify-content: center; padding: var(--space-4);
}}
/* Лайтбокс один на экране в любой момент — в отличие от блюра на карточках
   списка, здесь backdrop-filter ничего не стоит с точки зрения
   производительности (один элемент, не десятки). */
.photo-lightbox.open {{ display: flex; }}
@keyframes lightbox-in {{ from {{ opacity: 0; transform: scale(0.94); }} to {{ opacity: 1; transform: scale(1); }} }}
.photo-lightbox.open img {{ animation: lightbox-in 0.35s cubic-bezier(.16,1,.3,1) both; }}
.photo-lightbox img {{ max-width: 100%; max-height: 100%; border-radius: var(--radius);
    border: 1px solid var(--card-edge); box-shadow: 0 30px 70px -20px rgba(0,0,0,0.7); }}
.photo-lightbox-close {{
  position: absolute; top: max(var(--space-4), env(safe-area-inset-top)); right: var(--space-4);
  width: 38px; height: 38px; border-radius: 50%; border: 1px solid var(--card-edge); background: var(--card-edge);
  color: var(--on-accent); font-size: 18px; cursor: pointer; line-height: 1; transition: background 0.15s ease;
}}
.photo-lightbox-close:active {{ background: var(--stamp-deep); }}
.photo-upload-form {{ margin-top: 10px; }}
.photo-upload-btn {{ display: inline-flex; align-items: center; gap: 4px; font-size: 0.82rem; color: var(--ink-soft);
    font-family: var(--font-body); cursor: pointer; }}
.photo-upload-btn input[type=file] {{ position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }}

/* Панель выбора дня — адаптация карточки "Upcoming Meetings" с Uiverse под
   крафт-палитру страницы. Раскрытие до месяца — нативный <details>, стрелка
   поворачивается чистым CSS от [open], без единой строчки JS. */
.day-picker {{ background: var(--card); border: 1px solid var(--card-edge); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: var(--space-3) var(--space-4); margin-bottom: var(--space-3);
    backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); }}
.day-picker-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
.day-picker-month {{ font-family: var(--font-display); font-weight: 600; font-size: 1.05rem; color: var(--ink);
    text-transform: capitalize; }}
.day-today-link {{ color: var(--ink-soft); text-decoration: none; font-size: 0.85rem; border: 1px solid var(--card-edge);
    border-radius: 999px; padding: 2px 9px; }}
.day-today-link:active {{ background: rgba(0, 0, 0, 0.22); }}

.day-picker-summary {{ list-style: none; cursor: pointer; display: block; }}
.day-picker-summary::-webkit-details-marker {{ display: none; }}
.date-nav-container {{ background: rgba(0, 0, 0, 0.2); border-radius: var(--radius-sm); padding: 10px 6px;
    display: flex; justify-content: space-between; gap: 2px; }}
.day-item {{ display: flex; flex-direction: column; align-items: center; text-decoration: none;
    color: var(--ink); flex: 1; position: relative; padding-bottom: 6px; border-radius: var(--radius-sm); }}
.day-number {{ font-size: 1.05rem; font-weight: 600; width: 34px; height: 26px; display: flex;
    align-items: center; justify-content: center; border-radius: 13px; }}
.day-name {{ font-size: 0.65rem; color: var(--ink-soft); margin-top: 2px; }}
.day-item.day-active .day-number {{ background: var(--stamp); color: var(--on-accent); }}
.day-item.day-today:not(.day-active) .day-number {{ border: 1px solid var(--stamp); }}
.day-dot {{ width: 4px; height: 4px; border-radius: 50%; background: var(--stamp); margin-top: 3px; }}
.day-item.day-active .day-dot {{ background: transparent; }} /* подсветка и так есть — точка лишняя */

/* Стрелка-индикатор раскрытия — отдельный декоративный треугольник под
   рядом недели, поворачивается через [open] на <details>. */
.day-picker-expand {{ position: relative; }}
.day-picker-expand > summary {{ padding-bottom: 14px; }}
.day-picker-expand > summary::after {{
  content: ""; position: absolute; left: 50%; bottom: -2px; transform: translateX(-50%) rotate(0deg);
  width: 9px; height: 9px; border-right: 2px solid var(--ink-soft); border-bottom: 2px solid var(--ink-soft);
  transform-origin: center; transition: transform 0.2s ease;
}}
.day-picker-expand[open] > summary::after {{ transform: translateX(-50%) rotate(-135deg); bottom: 2px; }}

.day-picker-month-grid {{ margin-top: 10px; }}
.day-picker-dow-row, .day-picker-month-cells {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }}
.day-dow-label {{ text-align: center; font-size: 0.62rem; color: var(--ink-soft); padding-bottom: 4px; }}
.day-item-month .day-number {{ width: 28px; height: 24px; font-size: 0.9rem; }}
.day-item-month .day-name {{ display: none; }}
.day-empty {{ visibility: hidden; }}

.indicator-container {{ display: flex; justify-content: space-between; position: relative; padding: 0 20px;
    margin-top: 10px; }}
.indicator-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--card-edge); position: relative; z-index: 2; }}
.indicator-dot.indicator-active {{ background: var(--stamp); }}
.indicator-line {{ position: absolute; top: 50%; left: 20px; right: 20px; height: 1px;
    border-top: 1.5px dashed var(--card-edge); z-index: 1; }}
"""


TASKS_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
""" + _FONT_HEAD + """
<!--stylesheets-->
</head>
<body>
<h1>{heading}</h1>
{nav_html}
{tg_widget_html}
{new_task_error}
<form class="new-task-form" method="post" action="/dashboard/{token}/tasks/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{new_title_placeholder}" maxlength="200" required>
<textarea name="description" placeholder="{new_description_placeholder}" maxlength="4000" rows="2"></textarea>
<input type="datetime-local" name="due_at" required>
<button type="submit">{btn_add}</button>
</form>
{day_picker_html}
{tasks_html}
{chart_html}
<!-- Один оверлей на всю страницу вместо отдельного на каждую задачу с фото,
     и один делегированный обработчик вместо inline-onclick на каждом кадре:
     меньше DOM-узлов, меньше HTML, меньше работы парсеру на телефоне.
     Полный кадр (без ?size=thumb) запрашивается только здесь, по тапу. -->
<div class="photo-lightbox" id="ph-lb">
<button type="button" class="photo-lightbox-close" aria-label="close">✕</button>
<!-- без src: пустой src="" некоторые браузеры трактуют как ссылку на саму
     страницу и уходят за ней лишним запросом -->
<img alt="">
</div>
<script>
(function () {{
  var lb = document.getElementById('ph-lb'), img = lb.querySelector('img');
  function close() {{ lb.classList.remove('open'); img.removeAttribute('src'); }}
  document.addEventListener('click', function (e) {{
    var frame = e.target.closest('.task-photo');
    // Клик по крестику удаления живёт внутри своей формы — лайтбокс не трогаем.
    if (frame && !e.target.closest('form')) {{
      img.src = frame.dataset.full;
      lb.classList.add('open');
    }} else if (e.target === lb || e.target.closest('.photo-lightbox-close')) {{
      close();
    }}
  }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape') close();
  }});
}})();
</script>
</body></html>"""


HISTORY_CSS = """
.event {{ padding: 0.85rem 1rem; margin-bottom: 0.6rem; display: flex; gap: 0.65rem; align-items: center; }}
.event-body {{ flex: 1; min-width: 0; }}
.event-line {{ font-size: 0.92rem; color: var(--ink); }}
.event-line .ev-title {{ font-weight: 600; font-family: var(--font-display); }}
.event-time {{ color: var(--ink-soft); font-size: 0.78rem; margin-top: 2px; }}
.avatar {{ width: 28px; height: 28px; border-radius: 50%; object-fit: cover;
          background: var(--card-edge); border: 1.5px solid var(--on-accent); outline: 1px solid var(--rule); flex-shrink: 0; }}
.avatar-placeholder {{ display: inline-flex; align-items: center; justify-content: center; font-size: 0.9rem; }}
.empty {{ color: var(--paper-soft); text-align: center; padding: 3rem 1rem; font-family: var(--font-display); font-style: italic; }}
.load-more {{ display: block; width: 100%; padding: 10px; border-radius: var(--radius-sm); border: 1px dashed var(--rule-dark);
    background: transparent; color: var(--paper-soft); text-align: center; text-decoration: none; margin-top: 0.5rem; box-sizing: border-box;
    font-family: var(--font-body); }}
.load-more:active {{ border-color: var(--stamp); color: var(--stamp); }}
.nav-link {{ display: inline-block; color: var(--paper-soft); text-decoration: none; font-size: 0.85rem; margin-bottom: var(--space-4);
    font-family: var(--font-body); }}
"""


HISTORY_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{history_page_title}</title>
""" + _FONT_HEAD + """
<!--stylesheets-->
</head>
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
        avatar_html = (f'<img class="avatar" src="/dashboard/{token}/avatar/{actor_id}" '
                       f'alt="" loading="lazy" decoding="async">')
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
    # current_password_version обязателен: без него смена пароля дашборда
    # инвалидировала сессию на всех страницах, кроме этой, и утёкшая cookie
    # продолжала читать ленту событий чата.
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        # Как и на главной странице — незалогиненный видит форму пароля.
        return _login_response(token, lang)

    tz = _tz_of(settings)

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
        token=token, events_html=events_html, load_more_html=load_more_html, **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


TEMPLATES_CSS = """
.task {{ padding: var(--space-4); margin-bottom: var(--space-3);
        display: flex; gap: var(--space-3); align-items: flex-start; }}
.task-body {{ flex: 1; min-width: 0; }}
.title {{ font-family: var(--font-display); font-weight: 700; font-size: 1.15rem; line-height: 1.35; color: var(--ink); }}
.meta {{ color: var(--ink-soft); font-size: 0.85rem; margin-top: 4px; }}
.task-actions {{ display: flex; gap: 16px; margin-top: 10px; }}
.task-actions button {{
  background: none; border: none; color: var(--ink-soft); font-size: 0.82rem;
  font-family: var(--font-body); cursor: pointer; padding: 0;
  transition: color 0.15s ease;
}}
.task-actions button:active {{ color: var(--stamp); }}
.task-actions .del-btn:active {{ color: var(--stamp-deep); }}
.empty {{ color: var(--paper-soft); text-align: center; padding: 3rem 1rem; font-family: var(--font-display); font-style: italic; }}
.nav-link {{ display: inline-block; color: var(--paper-soft); text-decoration: none; font-size: 0.85rem; margin-bottom: var(--space-4);
    font-family: var(--font-body); margin-right: var(--space-3); }}
.new-task-form {{ display: flex; flex-direction: column; gap: 10px; padding: var(--space-4); margin-top: var(--space-4); }}
.new-task-form input[type=text], .new-task-form input[type=time], .new-task-form input[type=number] {{
  width: 100%; padding: 8px 2px; border: none; border-bottom: 1.5px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--ink); font-family: var(--font-body); font-size: 1rem; box-sizing: border-box;
}}
.new-task-form input::placeholder {{ color: var(--ink-soft); }}
.new-task-form label {{ font-size: 0.8rem; color: var(--ink-soft); }}
.new-task-form input:focus {{ outline: none; border-bottom-color: var(--stamp); }}
.new-task-form button {{
  padding: 10px 16px; border-radius: var(--radius-sm); border: none; background: var(--stamp);
  color: var(--on-accent); font-weight: 600; cursor: pointer; flex-shrink: 0;
}}
.new-task-form button:active {{ background: var(--stamp-deep); }}
.new-task-error {{ color: var(--stamp); font-size: 0.85rem; margin: -0.5rem 0 0.25rem; font-family: var(--font-body); }}
@media (max-width: 480px) {{ .new-task-form button {{ width: 100%; }} }}
"""


TEMPLATES_PAGE = """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{templates_page_title}</title>
""" + _FONT_HEAD + """
<!--stylesheets-->
</head>
<body>
<a class="nav-link" href="/dashboard/{token}">{nav_back}</a>
<h1>{templates_heading}</h1>
{new_template_error}
{templates_html}
<form class="new-task-form" method="post" action="/dashboard/{token}/templates/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{new_template_title_placeholder}" maxlength="200" required>
<label>{new_template_offset_label}</label>
<input type="time" name="time_of_day" required>
<input type="number" name="day_offset" min="0" max="365" value="0" required>
<button type="submit">{btn_save_template}</button>
</form>
</body></html>"""


# Маркер <!--stylesheets--> в <head> каждого шаблона заменяем на реальные
# <link>'и уже после того, как все блоки CSS определены: имя файла зависит от
# хеша его содержимого, поэтому собрать ссылку раньше нельзя. Обработчики
# трогают шаблоны только во время запроса, так что порядок здесь безопасен.
LOGIN_PAGE = LOGIN_PAGE.replace("<!--stylesheets-->", _stylesheets("login", LOGIN_CSS))
TASKS_PAGE = TASKS_PAGE.replace("<!--stylesheets-->", _stylesheets("tasks", TASKS_CSS))
HISTORY_PAGE = HISTORY_PAGE.replace("<!--stylesheets-->", _stylesheets("history", HISTORY_CSS))
TEMPLATES_PAGE = TEMPLATES_PAGE.replace("<!--stylesheets-->", _stylesheets("templates", TEMPLATES_CSS))
assert "<!--stylesheets-->" not in TASKS_PAGE


def _fmt_template_offset(day_offset: int, lang: str) -> str:
    texts = _dt(lang)
    if day_offset == 0:
        return texts["template_offset_today"]
    if day_offset == 1:
        return texts["template_offset_tomorrow"]
    return texts["template_offset_days"].format(n=day_offset)


def _render_template_row(tpl: dict, lang: str, token: str, csrf: str) -> str:
    texts = _dt(lang)
    meta = f'{escape(tpl["time_of_day"])} · {escape(_fmt_template_offset(tpl["day_offset"], lang))}'
    return f"""<div class="task">
<div class="task-body">
<div class="title">{escape(tpl["title"])}</div>
<div class="meta">{meta}</div>
<div class="task-actions">
<form method="post" action="/dashboard/{token}/templates/{tpl['id']}/apply">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">{escape(texts["btn_apply_template"])}</button>
</form>
<form method="post" action="/dashboard/{token}/templates/{tpl['id']}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_template"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="del-btn">{escape(texts["btn_delete_template"])}</button>
</form>
</div>
</div>
</div>"""


def _resolve_template_due_at(time_of_day: str, day_offset: int, tz) -> datetime:
    """Та же логика, что и в bot.py:_resolve_template_due_at — датой
    отталкиваемся от момента ПРИМЕНЕНИЯ шаблона (сейчас), не от момента его
    создания. Продублировано здесь (а не импортировано из bot.py), потому что
    bot.py сам импортирует dashboard — обратный импорт создал бы цикл."""
    hour, minute = (int(p) for p in time_of_day.split(":"))
    now_local = datetime.now(tz)
    candidate_date = now_local.date() + timedelta(days=day_offset)
    candidate = datetime.combine(candidate_date, dtime(hour, minute), tzinfo=tz)
    if day_offset == 0 and candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


async def handle_templates_page(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/templates — список своих шаблонов + форма
    создания нового. Доступно и личному, и групповому владельцу (в отличие
    от /history) — см. комментарий у nav_links в handle_dashboard."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        return _login_response(token, lang)

    csrf = _csrf_token(cookie)
    templates = await db.get_templates(owner_id, owner_type)
    if not templates:
        templates_html = f'<div class="empty">{escape(texts["templates_empty"])}</div>'
    else:
        templates_html = "\n".join(_render_template_row(tpl, lang, token, csrf) for tpl in templates)

    new_template_error = ""
    err = request.query.get("error")
    if err == "empty_title":
        new_template_error = f'<div class="new-task-error">{escape(texts["error_empty_template_title"])}</div>'
    elif err == "limit":
        new_template_error = (
            f'<div class="new-task-error">'
            f'{escape(texts["template_limit_reached"].format(n=db.MAX_TEMPLATES_PER_OWNER))}</div>'
        )

    page = TEMPLATES_PAGE.format(
        token=token, csrf=csrf, templates_html=templates_html,
        new_template_error=new_template_error, **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_template_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/new."""
    token = request.match_info["token"]
    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    title = data.get("title", "").strip()
    if not title:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates?error=empty_title"})

    if await db.count_templates(owner_id, owner_type) >= db.MAX_TEMPLATES_PER_OWNER:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates?error=limit"})

    time_of_day_raw = data.get("time_of_day", "")
    try:
        hour, minute = (int(p) for p in time_of_day_raw.split(":")[:2])
        time_of_day = f"{hour:02d}:{minute:02d}"
    except (ValueError, AttributeError):
        time_of_day = "09:00"

    try:
        day_offset = max(0, min(365, int(data.get("day_offset", "0"))))
    except ValueError:
        day_offset = 0

    await db.add_template(
        owner_id=owner_id, owner_type=owner_type, title=title[:200],
        time_of_day=time_of_day, day_offset=day_offset,
    )
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates"})


async def handle_template_apply(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/{template_id}/apply — создаёт задачу
    из шаблона прямо с дашборда. Как и у handle_task_create, задача создаётся
    без планирования в APScheduler бота (дашборд — отдельный веб-процесс без
    доступа к его scheduler'у) — ровно то же ограничение, что и у обычного
    добавления задачи с дашборда."""
    token = request.match_info["token"]
    try:
        template_id = int(request.match_info["template_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    tpl = await db.get_template(template_id)
    if not tpl or tpl["owner_id"] != owner_id or tpl["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    tz = _tz_of(settings)
    due_at = _resolve_template_due_at(tpl["time_of_day"], tpl["day_offset"], tz)
    remind_until_done = bool(tpl.get("remind_until_done", False))
    task_id = await db.add_task(
        owner_id, owner_type, tpl["title"], due_at, tpl["repeat"], tpl["remind"],
        tag=tpl.get("tag"), remind_until_done=remind_until_done, created_by=acting_user_id,
    )
    await db.log_history(task_id, acting_user_id, tpl["title"], "created")
    # Планируем сразу: иначе задача из шаблона (в т.ч. повторяющаяся) молча
    # не напоминала бы о себе до перезапуска процесса.
    _schedule_task(task_id, owner_id, owner_type, due_at, tpl["repeat"], tpl["remind"], tz,
                   remind_until_done=remind_until_done)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_template_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/templates/{template_id}/delete."""
    token = request.match_info["token"]
    try:
        template_id = int(request.match_info["template_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    await db.delete_template(template_id, owner_id, owner_type)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}/templates"})


_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
_PL_MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
              "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]
_MONTH_NAMES = {"ru": _RU_MONTHS, "en": _EN_MONTHS, "pl": _PL_MONTHS}

_RU_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_PL_DOW = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]
_DOW_NAMES = {"ru": _RU_DOW, "en": _EN_DOW, "pl": _PL_DOW}


def _months(lang: str) -> list[str]:
    """Фолбэк на ru, как в _dt(): раньше незнакомый язык в БД давал KeyError
    и 500 на главной странице вместо просто русских названий месяцев."""
    return _MONTH_NAMES.get(lang, _RU_MONTHS)


def _dows(lang: str) -> list[str]:
    return _DOW_NAMES.get(lang, _RU_DOW)


def _render_day_picker(selected: "dt.date", today: "dt.date", lang: str, token: str, task_days: set,
                        filter_active: bool = True) -> str:
    """Панель выбора дня дашборда (фильтр по due_at), в стиле карточки
    'Upcoming Meetings' с Uiverse: неделя вокруг selected + стрелка вниз,
    раскрывающая блок до полного месяца. Раскрытие — чистый HTML
    <details>/<summary> (без JS): браузер сам переключает видимость и
    поворот стрелки по CSS от атрибута [open], сервер ничего не считает
    по клику — вся навигация это обычные ссылки с ?date=YYYY-MM-DD.
    task_days — set дат (date), на которые есть хотя бы одна задача, для
    точек-индикаторов под числами (как у dot-индикаторов в оригинале)."""
    month_name = _months(lang)[selected.month - 1]
    dow_names = _dows(lang)

    # Неделя (Пн-Вс), в которую попадает selected — тот же принцип, что и в
    # оригинальном компоненте (ряд дней вокруг активного).
    week_start = selected - dt.timedelta(days=selected.weekday())
    week_days = [week_start + dt.timedelta(days=i) for i in range(7)]

    def _day_link(d: "dt.date", extra_class: str = "") -> str:
        cls = "day-item"
        if filter_active and d == selected:
            cls += " day-active"
        if d == today and not (filter_active and d == selected):
            cls += " day-today"
        dot = '<span class="day-dot"></span>' if d in task_days else ""
        return f"""<a class="{cls} {extra_class}" href="/dashboard/{token}?date={d.isoformat()}">
<div class="day-number">{d.day}</div>
<div class="day-name">{dow_names[d.weekday()]}</div>
{dot}
</a>"""

    week_html = "".join(_day_link(d) for d in week_days)
    week_dots = "".join(
        f'<div class="indicator-dot{" indicator-active" if (filter_active and d == selected) else ""}"></div>'
        for d in week_days
    )

    # Полный месяц selected — те же ссылки-дни, сеткой 7 колонок, с пустыми
    # ячейками в начале/конце для выравнивания по дню недели.
    _, days_in_month = cal.monthrange(selected.year, selected.month)
    first_of_month = selected.replace(day=1)
    lead_empty = first_of_month.weekday()  # 0=Пн
    month_cells = ['<div class="day-empty"></div>'] * lead_empty
    for day_num in range(1, days_in_month + 1):
        month_cells.append(_day_link(first_of_month.replace(day=day_num), "day-item-month"))
    month_html = "".join(month_cells)

    # Ссылка сброса видна всегда, когда фильтр активен (вернуться к полному
    # списку задач) — а не только когда выбранный день не сегодня.
    reset_html = f'<a class="day-today-link" href="/dashboard/{token}">↺</a>' if filter_active else ""

    return f"""<div class="day-picker">
<div class="day-picker-header">
<span class="day-picker-month">{month_name} {selected.year}</span>
{reset_html}
</div>
<details class="day-picker-expand">
<summary class="day-picker-summary">
<div class="date-nav-container">{week_html}</div>
</summary>
<div class="day-picker-month-grid">
<div class="day-picker-dow-row">{"".join(f'<div class="day-dow-label">{n}</div>' for n in dow_names)}</div>
<div class="day-picker-month-cells">{month_html}</div>
</div>
</details>
<div class="indicator-container"><div class="indicator-line"></div>{week_dots}</div>
</div>"""


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
        avatar_html = (f'<img class="avatar" src="/dashboard/{token}/avatar/{created_by}" '
                       f'alt="" loading="lazy" decoding="async">')
    else:
        avatar_html = '<span class="avatar avatar-placeholder">👤</span>'
    return f'<div class="task-author">{avatar_html}<span>{escape(name)}</span></div>'


PHOTO_SLOT_WIDTH = 118  # px — ширина одного кадра карусели, используется и в CSS, и в JS-прокрутке стрелками (было 96 — фото было мелковато различимо, укрупнили)


def _render_photos(photos: list[dict], token: str, task_id: int, csrf: str, texts: dict) -> str:
    """Карусель фото под задачей. Сначала самый частый случай — ОДНО фото —
    рендерится крупно на всю ширину карточки (.task-photo-hero), а не
    мелким квадратиком 118×118 из общей сетки: именно одно фото теряло
    больше всего в читаемости при сжатии в квадрат. 2+ фото — прежняя
    карусель (до 3 кадров разом, scroll-snap, стрелки только если фото
    больше 3 — незачем рисовать управление, которым нечего листать).

    В ленту идут ПРЕВЬЮ (?size=thumb) — полный кадр подтягивается только в
    лайтбоксе по тапу. Лайтбокс на страницу один (см. PHOTO_LIGHTBOX в
    TASKS_PAGE) и открывается делегированным обработчиком по data-full:
    раньше на каждую задачу с фото рендерился свой оверлей плюс по
    inline-onclick на каждом кадре."""
    if not photos:
        return ""
    track_id = f"ph-track-{task_id}"
    is_hero = len(photos) == 1
    items = []
    for p in photos:
        src = f"/dashboard/{token}/tasks/{task_id}/photo/{p['id']}"
        photo_class = "task-photo task-photo-hero" if is_hero else "task-photo"
        # loading=lazy здесь особенно уместно: карточка свёрнута (<details>
        # закрыт), и браузер вообще не тронет эти картинки, пока её не
        # раскроют. decoding=async — чтобы декодирование JPEG не занимало
        # главный поток в момент раскрытия.
        items.append(f"""<div class="{photo_class}" data-full="{src}">
<img src="{src}?size=thumb" alt="" loading="lazy" decoding="async" fetchpriority="low">
<form method="post" action="{src}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_photo"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="photo-del-btn" aria-label="delete photo">✕</button>
</form>
</div>""")

    arrows_html = ""
    if len(photos) > 3:
        # scrollBy на фиксированный шаг (один кадр) — явное, предсказуемое
        # перемещение по клику, никакого авто-таймера/зацикливания.
        arrows_html = f"""<button type="button" class="photo-nav photo-nav-prev" aria-label="prev"
  onclick="document.getElementById('{track_id}').scrollBy({{left:-{PHOTO_SLOT_WIDTH},behavior:'smooth'}})">‹</button>
<button type="button" class="photo-nav photo-nav-next" aria-label="next"
  onclick="document.getElementById('{track_id}').scrollBy({{left:{PHOTO_SLOT_WIDTH},behavior:'smooth'}})">›</button>"""

    wrap_class = "task-photos-wrap hero" if is_hero else "task-photos-wrap"
    return f"""<div class="{wrap_class}">
<div class="task-photos" id="{track_id}">{"".join(items)}</div>
{arrows_html}
</div>"""


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


def _render_subtasks(subtasks: list[dict], token: str, task_id: int, csrf: str, texts: dict) -> str:
    """Чек-лист подзадач внутри раскрытой карточки — раньше заводился только
    из чата (FSM-диалог /subtasks в bot.py), теперь то же самое доступно и
    с дашборда: свой toggle/delete/add на отдельных маршрутах, та же модель
    данных (subtasks таблица, которую /subtasks в Telegram читает и пишет)."""
    rows = []
    for st in subtasks:
        row_class = "subtask-row done" if st["done"] else "subtask-row"
        rows.append(f"""<div class="{row_class}">
<form method="post" action="/dashboard/{token}/subtasks/{st['id']}/toggle">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="subtask-check" aria-label="toggle"></button>
</form>
<span class="subtask-title">{escape(st["title"])}</span>
<form method="post" action="/dashboard/{token}/subtasks/{st['id']}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_subtask"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="subtask-del-btn" aria-label="delete">✕</button>
</form>
</div>""")
    list_html = f'<div class="subtasks">{"".join(rows)}</div>' if rows else ""
    add_form = f"""<form class="subtask-add-form" method="post" action="/dashboard/{token}/tasks/{task_id}/subtasks/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{escape(texts["new_subtask_placeholder"])}" maxlength="200" required>
<button type="submit">{escape(texts["btn_add_subtask"])}</button>
</form>"""
    return list_html + add_form


def _render_task(
    task: dict, tz, lang: str, token: str, csrf: str,
    creator: dict | None = None, owner_type: str = "user",
    photos: list[dict] | None = None,
    subtasks: list[dict] | None = None,
) -> str:
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
    # Короткий значок повтора — виден уже в свёрнутой шапке карточки (summary);
    # полная формулировка ("🔁 Повторяется: раз в месяц") — только внутри,
    # в .repeat-info, чтобы не раздувать и так плотную строку меты.
    repeat_html = " · 🔁" if task["repeat"] != "none" and repeat_label else ""
    repeat_info_html = (
        f'<div class="repeat-chip">{escape(texts["repeats_label"].format(label=repeat_label))}</div>'
        if task["repeat"] != "none" and repeat_label
        else f'<div class="repeat-chip">{escape(texts["no_repeat_label"])}</div>'
    )
    toggle_action = "undone" if task["done"] else "done"
    # Автора показываем только в групповом дашборде — в личном он и так всегда
    # один и тот же человек, бейдж был бы бесполезным шумом.
    author_html = _render_author(task.get("created_by"), creator, token) if owner_type == "chat" else ""

    photos = photos or []
    photos_html = _render_photos(photos, token, task["id"], csrf, texts)
    upload_html = _render_upload_form(token, task["id"], csrf, texts, len(photos))

    # Раньше текст описания дублировался: один раз как read-only абзац,
    # сразу под ним — та же строка ещё раз в textarea формы редактирования.
    # Теперь textarea спрятана во вложенный <details> ("✏️ Изменить
    # описание") — по умолчанию виден только чистый текст, форма
    # редактирования — отдельное осознанное действие, а не вечно открытая
    # дублирующая копия.
    description = task.get("description") or ""
    description_text_html = f'<p class="description-text">{escape(description)}</p>' if description else ""
    edit_label = texts["edit_description_label"] if description else texts["add_description_label"]
    desc_form_html = f"""<details class="edit-toggle">
<summary>{escape(edit_label)}</summary>
<form class="desc-form" method="post" action="/dashboard/{token}/tasks/{task['id']}/edit">
<input type="hidden" name="csrf" value="{csrf}">
<textarea name="description" placeholder="{escape(texts["description_placeholder"])}" maxlength="4000" rows="2">{escape(description)}</textarea>
<button type="submit">{escape(texts["btn_save_description"])}</button>
</form>
</details>"""
    subtasks_html = _render_subtasks(subtasks or [], token, task["id"], csrf, texts)

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

    return f"""<div class="{css_class}" id="task-{task['id']}">
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/{toggle_action}">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="check-btn" aria-label="toggle"></button>
</form>
<details class="task-details">
<summary class="task-summary">
<div class="title">{escape(task["title"])}</div>
<div class="meta">📅 {local_due.strftime('%d.%m.%Y %H:%M')}{repeat_html}</div>
{tag_html}
</summary>
<div class="task-expanded">
{repeat_info_html}
{author_html}
{description_text_html}
{desc_form_html}
{subtasks_html}
{photos_html}
{upload_html}
{extra_actions}
</div>
</details>
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
            return _login_response(token, lang, error_html, status=429)

        data = await request.post()
        password = data.get("password", "")
        if await asyncio.to_thread(verify_password, password, settings["dashboard_password_hash"] or ""):
            _clear_login_attempts(token, ip)
            # tg_user_id=0: кто именно ввёл общий пароль, нам неизвестно — для
            # owner_type='chat' это уточнится позже через виджет на самой
            # странице (см. handle_telegram_auth), без этого шага деградируем
            # на owner_id (chat_id) в истории, как было раньше.
            session_value = _sign_session(owner_id, owner_type,
                                          password_version=settings.get("dashboard_password_version", 0))
            resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
            # Render терминирует TLS на прокси — схему берём из X-Forwarded-Proto
            is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
            resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                            samesite="Strict", secure=is_https)
            return resp

        _record_failed_login(token, ip)
        error_html = f'<div class="error">{escape(texts["error_wrong_password"])}</div>'
        return _login_response(token, lang, error_html, status=401)

    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        return _login_response(token, lang)
    _, _, tg_user_id = session

    tz = _tz_of(settings)
    csrf = _csrf_token(cookie)

    today = dt.datetime.now(tz).date()
    raw_date = request.query.get("date", "")
    selected_date = today
    filter_active = bool(raw_date)
    if raw_date:
        try:
            selected_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            selected_date, filter_active = today, False  # битый параметр — тихо откатываемся на "все задачи"

    all_tasks = await db.get_tasks(owner_id, owner_type, include_done=True)
    # task_days — для точек-индикаторов под числами панели: на какие дни
    # вообще есть задачи. Считаем по полному списку, иначе индикаторы видели
    # бы только дни внутри уже отфильтрованной недели/месяца.
    task_days = {t["due_at"].astimezone(tz).date() for t in all_tasks}
    # Панель — это ФИЛЬТР, не обязательный режим: без ?date= в URL дашборд
    # ведёт себя как раньше (показывает все задачи), а выбор дня в панели
    # добавляет ?date=... и сужает список до одного дня. Так ссылка дашборда
    # без параметров (например, уже сохранённая в Telegram) не меняет
    # поведения для тех, кто панелью не пользуется.
    tasks = [t for t in all_tasks if t["due_at"].astimezone(tz).date() == selected_date] if filter_active else all_tasks

    day_picker_html = _render_day_picker(
        selected_date if filter_active else today, today, lang, token, task_days, filter_active=filter_active
    )

    if not tasks:
        tasks_html = f'<div class="empty">{escape(texts["empty"])}</div>'
    else:
        creators: dict[int, dict] = {}
        if owner_type == "chat":
            creator_ids = {t["created_by"] for t in tasks if t.get("created_by")}
            creators = await db.get_telegram_users(list(creator_ids))
        # Фото и подзадачи — ОДИН запрос на всю страницу каждый (WHERE task_id
        # = ANY(...)), а не по запросу на задачу: раньше рендер стоил 2*N
        # round-trip'ов к БД, и на двух десятках задач это была самая дорогая
        # часть страницы.
        task_ids = [t["id"] for t in tasks]
        photos_by_task = await db.get_task_photos_meta_bulk(task_ids)
        subtasks_by_task = await db.get_subtasks_bulk(task_ids)
        tasks_html = "\n".join(
            _render_task(
                t, tz, lang, token, csrf, creator=creators.get(t.get("created_by")), owner_type=owner_type,
                photos=photos_by_task.get(t["id"]), subtasks=subtasks_by_task.get(t["id"]),
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
    # В отличие от истории (nav_history) — лента событий с авторами нужна
    # только групповому дашборду (см. комментарий у handle_history) — шаблоны
    # полезны и личному владельцу, поэтому ссылка показывается всегда.
    nav_links = [f'<a class="nav-link" href="/dashboard/{token}/templates">{escape(texts["nav_templates"])}</a>']
    if owner_type == "chat":
        nav_links.append(f'<a class="nav-link" href="/dashboard/{token}/history">{escape(texts["nav_history"])}</a>')
    nav_html = " ".join(nav_links)
    new_task_error_html = ""
    if request.query.get("error") == "empty_title":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_empty_title"])}</div>'
    elif request.query.get("error") == "bad_photo":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_bad_photo"])}</div>'
    elif request.query.get("error") == "bad_due_at":
        new_task_error_html = f'<div class="new-task-error">{escape(texts["error_bad_due_at"])}</div>'

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
        day_picker_html=day_picker_html, **_dt_tasks_page(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_telegram_auth(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/telegram-auth — вызывается из onTelegramAuth()
    после успешного входа через Login Widget. Не выдаёт новых прав (действия и
    так разрешены паролем) — только уточняет, какой именно участник сейчас за
    дашбордом, чтобы история велась под правильным tg_user_id, а не chat_id."""
    token = request.match_info["token"]

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, _ = owner
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

    pwd_version = settings.get("dashboard_password_version", 0)
    session_value = _sign_session(owner_id, owner_type, tg_user_id=tg_user_id, password_version=pwd_version)
    resp = aioweb.Response(status=200)
    is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="Strict", secure=is_https)
    return resp


def _parse_due_at(raw: str, tz) -> datetime | None:
    """Разбор значения поля due_at (<input type="datetime-local">). Пустое
    поле — это "сейчас" (поле необязательное), а вот нераспознанная строка
    возвращает None: вызывающая сторона показывает ошибку вместо того, чтобы
    молча поставить задачу на текущую минуту. Если браузер всё же прислал
    смещение (не naive-строку), оно уважается и переводится в tz владельца, а
    не затирается, как делал прежний .replace(tzinfo=tz)."""
    raw = (raw or "").strip()
    if not raw:
        return datetime.now(tz)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


async def handle_task_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/new — создание задачи прямо с дашборда.
    Та же защита, что и у остальных действий: сессия + CSRF."""
    token = request.match_info["token"]

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    title = data.get("title", "").strip()
    description = data.get("description", "").strip() or None
    if not title:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=empty_title"})

    tz = _tz_of(settings)
    due_at = _parse_due_at(data.get("due_at", ""), tz)
    if due_at is None:
        # Раньше нераспознанная дата молча превращалась в "сейчас", и задача
        # создавалась не на то время, о котором просил пользователь.
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=bad_due_at"})

    # repeat/remind — обязательные позиционные параметры add_task; с дашборда задача
    # создаётся без повтора и с дефолтным напоминанием (то же, что ожидает остальной код).
    task_id = await db.add_task(
        owner_id, owner_type, title, due_at, "none", "on_time",
        created_by=acting_user_id, description=description,
    )
    await db.log_history(task_id, acting_user_id, title, "created")
    _schedule_task(task_id, owner_id, owner_type, due_at, "none", "on_time", tz)

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

    owner = await _require_session_full(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    settings, owner_id, owner_type, tg_user_id = owner
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

    # Джобы планировщика синхронизируем так же, как это делают кнопки в боте
    # (mark_done/mark_undone/snooze/delete_task в bot.py): иначе выполненная с
    # дашборда задача продолжала напоминать о себе, а удалённая оставляла
    # висеть джобу на уже несуществующий id.
    tz = _tz_of(settings)
    remind_until_done = bool(task.get("remind_until_done"))

    if action == "done":
        await db.mark_done(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "done")
        _unschedule_task(task_id)
    elif action == "undone":
        await db.mark_undone(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "undone")
        _schedule_task(task_id, owner_id, owner_type, task["due_at"], task["repeat"], task["remind"], tz,
                       remind_until_done=remind_until_done)
    elif action == "postpone":
        if task["repeat"] != "none":
            # Сдвиг due_at у повторяющейся задачи не отражается на её реальном
            # расписании (CronTrigger в bot.py строится по due_at только при
            # создании/рестарте) — см. комментарий у _render_task. Кнопка в
            # шаблоне уже скрыта для таких задач; это серверная подстраховка
            # на случай прямого POST мимо формы.
            return aioweb.Response(status=400)
        new_due = task["due_at"] + timedelta(days=1)
        await db.update_task(task_id, due_at=new_due)
        await db.log_history(task_id, acting_user_id, task["title"], "rescheduled")
        _unschedule_task(task_id)
        _schedule_task(task_id, owner_id, owner_type, new_due, "none", task["remind"], tz,
                       remind_until_done=remind_until_done)
    elif action == "delete":
        await db.delete_task(task_id)
        await db.log_history(task_id, acting_user_id, task["title"], "deleted")
        _unschedule_task(task_id)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_task_edit(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/edit — сохранение описания
    из раскрытой карточки задачи. Пустое поле очищает описание (NULL), а не
    хранит пустую строку — так meta-блок карточки снова корректно решает
    "показывать плейсхолдер или нет" по одному условию (task.get("description"))."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    description = data.get("description", "").strip()[:4000] or None
    await db.update_task(task_id, description=description)

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{task_id}"})


async def _owned_subtask(subtask_id: int, owner_id: int, owner_type: str) -> dict | None:
    """Тот же принцип, что и у bot.py:_authorize_subtask — подзадача сама по
    себе владельца не хранит, поэтому владение проверяется через её task_id.
    Возвращает подзадачу при успехе, иначе None."""
    sub = await db.get_subtask(subtask_id)
    if not sub:
        return None
    task = await db.get_task(sub["task_id"])
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return None
    return sub


async def handle_subtask_create(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/subtasks/new."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    title = data.get("title", "").strip()
    if title:
        await db.add_subtask(task_id, title[:200])

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{task_id}"})


async def handle_subtask_toggle(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/subtasks/{subtask_id}/toggle."""
    token = request.match_info["token"]
    try:
        subtask_id = int(request.match_info["subtask_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    sub = await _owned_subtask(subtask_id, owner_id, owner_type)
    if sub is None:
        return aioweb.Response(status=404)

    await db.toggle_subtask(subtask_id)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{sub['task_id']}"})


async def handle_subtask_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/subtasks/{subtask_id}/delete."""
    token = request.match_info["token"]
    try:
        subtask_id = int(request.match_info["subtask_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, _tg_user_id = owner

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    sub = await _owned_subtask(subtask_id, owner_id, owner_type)
    if sub is None:
        return aioweb.Response(status=404)

    await db.delete_subtask(subtask_id)
    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}#task-{sub['task_id']}"})


def register_dashboard_routes(app: aioweb.Application):
    # Сжатие — на все ответы приложения (для не-текстовых и мелких это no-op).
    if compression_middleware not in app.middlewares:
        app.middlewares.append(compression_middleware)
    # Статика регистрируется первой: её путь /dashboard/static/... по форме
    # совпадает с /dashboard/{token}/..., а aiohttp матчит в порядке регистрации.
    app.router.add_get("/dashboard/static/{filename}", handle_static_css)
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
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/edit", handle_task_edit)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/subtasks/new", handle_subtask_create)
    app.router.add_post("/dashboard/{token}/subtasks/{subtask_id}/toggle", handle_subtask_toggle)
    app.router.add_post("/dashboard/{token}/subtasks/{subtask_id}/delete", handle_subtask_delete)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/{action}", handle_task_toggle)
    app.router.add_get("/dashboard/{token}/avatar/{user_id}", handle_avatar)
    app.router.add_get("/dashboard/{token}/history", handle_history)
    app.router.add_get("/dashboard/{token}/templates", handle_templates_page)
    app.router.add_post("/dashboard/{token}/templates/new", handle_template_create)
    app.router.add_post("/dashboard/{token}/templates/{template_id}/apply", handle_template_apply)
    app.router.add_post("/dashboard/{token}/templates/{template_id}/delete", handle_template_delete)