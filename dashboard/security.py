"""Безопасность дашборда: пароли, подписанные cookie-сессии, CSRF, проверка
Telegram Login Widget и rate-limit попыток входа.

Сессия хранится в подписанной cookie (без внешних зависимостей — просто HMAC
поверх owner_type:owner_id + expiry, валидируется на каждый запрос)."""

import hashlib
import hmac
import os
import time

from aiohttp import web as aioweb

import db

from .config import SESSION_MAX_AGE, SESSION_SECRET

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


def _reset_login_limiter():
    """Полный сброс состояния rate-limit (счётчики и таймер уборки) — для
    тестов. Присваивать dashboard._last_login_prune снаружи бесполезно: пакет
    реэкспортирует лишь значение, а читает его код этого модуля."""
    global _last_login_prune
    _LOGIN_ATTEMPTS.clear()
    _last_login_prune = 0.0
