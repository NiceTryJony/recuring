"""Пул соединений, URL базы и повтор запросов при временных сбоях."""

import logging
import os
import re
from urllib.parse import quote, unquote

import asyncpg

logger = logging.getLogger("db")


# DATABASE_URL должен быть строкой Supabase "Session mode" (порт 5432, хост *.pooler.supabase.com)
# Transaction mode (6543) НЕ подходит — он не держит долгоживущие сессии/prepared statements,
# которые использует asyncpg, а наш бот — persistent-процесс, а не serverless-функция.
# Прямое подключение (db.*.supabase.co) тоже не берём — требует IPv6, на Render free его может не быть.
# Взять строку: Supabase Dashboard → Project Settings → Database → Connection string → Session pooler


def _normalize_db_url(raw_url: str) -> str:
    """Перекодирует пароль в connection string на случай спецсимволов (@ : / # % и т.д.),
    которые иначе ломают парсинг URL и дают 'Invalid format for user or db_name'."""
    scheme_sep = "://"
    if scheme_sep not in raw_url:
        return raw_url
    scheme, rest = raw_url.split(scheme_sep, 1)

    if "@" not in rest:
        return raw_url

    m = re.search(r"@([A-Za-z0-9.\-]+:\d+)(/.*)?$", rest)
    if not m:
        return raw_url
    hostinfo = m.group(1)
    tail = m.group(2) or ""
    userinfo = rest[: m.start()]

    if ":" in userinfo:
        user, _, password = userinfo.partition(":")
    else:
        user, password = userinfo, ""

    # unquote перед quote делает функцию идемпотентной: уже закодированный пароль ('p%40x')
    # раньше кодировался второй раз ('p%2540x') и подключение падало с ошибкой аутентификации.
    safe_user = quote(unquote(user), safe="")
    safe_password = quote(unquote(password), safe="")
    new_userinfo = f"{safe_user}:{safe_password}" if password else safe_user

    return f"{scheme}{scheme_sep}{new_userinfo}@{hostinfo}{tail}"


DATABASE_URL = _normalize_db_url(os.environ["DATABASE_URL"])

_pool: asyncpg.Pool | None = None

# retry-параметры для временных сбоев соединения (Supabase может моргнуть на free tier)
_MAX_RETRIES = 3
_RETRY_DELAY_SEC = 2


async def _with_retry(coro_fn, *args, **kwargs):
    """Оборачивает запрос к БД ретраями на случай временной недоступности
    (например, Supabase free tier приостановил проект или сетевой сбой)."""
    import asyncio
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (asyncpg.PostgresConnectionError, OSError) as e:
            last_exc = e
            logger.warning("DB запрос не удался (попытка %d/%d): %r", attempt, _MAX_RETRIES, e)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY_SEC * attempt)
    logger.error("DB запрос не удался после %d попыток", _MAX_RETRIES)
    raise last_exc


async def close_db():
    if _pool:
        await _pool.close()
