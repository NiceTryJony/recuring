import logging
import os
import re
import secrets
from datetime import datetime
from urllib.parse import quote

import asyncpg

logger = logging.getLogger(__name__)

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

    safe_user = quote(user, safe="")
    safe_password = quote(password, safe="")
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


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                due_at TIMESTAMPTZ NOT NULL,
                repeat TEXT NOT NULL DEFAULT 'none',
                remind TEXT NOT NULL DEFAULT '0',
                done BOOLEAN NOT NULL DEFAULT FALSE,
                tag TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                done_at TIMESTAMPTZ,
                last_notified_at TIMESTAMPTZ
            )
        """)
        # Миграция для существующих баз (добавление колонок, если их ещё нет)
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tag TEXT")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS done_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMPTZ")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subtasks (
                id SERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                done BOOLEAN NOT NULL DEFAULT FALSE,
                position INTEGER NOT NULL DEFAULT 0
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',
                language TEXT NOT NULL DEFAULT 'ru',
                dashboard_token TEXT UNIQUE,
                dashboard_password_hash TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_history (
                id SERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                event TEXT NOT NULL,
                event_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_subtasks_task_id ON subtasks(task_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user_id ON task_history(user_id)")


async def close_db():
    if _pool:
        await _pool.close()


# ---------- задачи ----------

async def add_task(user_id: int, title: str, due_at: datetime, repeat: str, remind: str, tag: str | None = None) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks (user_id, title, due_at, repeat, remind, tag)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                user_id, title, due_at, repeat, remind, tag
            )
            return row["id"]
    return await _with_retry(_run)


async def get_tasks(user_id: int, include_done: bool = False, tag: str | None = None) -> list[dict]:
    async def _run():
        async with _pool.acquire() as conn:
            query = "SELECT * FROM tasks WHERE user_id = $1"
            params = [user_id]
            if not include_done:
                query += " AND done = FALSE"
            if tag:
                params.append(tag)
                query += f" AND tag = ${len(params)}"
            query += " ORDER BY done, due_at"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_task(task_id: int) -> dict | None:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
            return dict(row) if row else None
    return await _with_retry(_run)


async def get_all_active_tasks() -> list[dict]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tasks WHERE done = FALSE")
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_user_tags(user_id: int) -> list[str]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT tag FROM tasks WHERE user_id = $1 AND tag IS NOT NULL ORDER BY tag",
                user_id
            )
            return [r["tag"] for r in rows]
    return await _with_retry(_run)


async def mark_done(task_id: int):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET done = TRUE, done_at = now() WHERE id = $1", task_id
            )
    await _with_retry(_run)


async def mark_undone(task_id: int):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET done = FALSE, done_at = NULL WHERE id = $1", task_id
            )
    await _with_retry(_run)


async def set_last_notified(task_id: int, when: datetime):
    """Фиксирует момент последней отправки уведомления по задаче — защита от дублей
    при быстрых рестартах процесса (catch-up логика в restore_jobs не шлёт повторно
    в течение короткого окна после последнего уведомления)."""
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET last_notified_at = $2 WHERE id = $1", task_id, when
            )
    await _with_retry(_run)


async def delete_task(task_id: int):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
    await _with_retry(_run)


async def update_task(task_id: int, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = list(fields.values())

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = $1", task_id, *values)
    await _with_retry(_run)


# ---------- подзадачи ----------

async def add_subtask(task_id: int, title: str) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO subtasks (task_id, title, position)
                   VALUES ($1, $2, COALESCE((SELECT MAX(position)+1 FROM subtasks WHERE task_id=$1), 0))
                   RETURNING id""",
                task_id, title
            )
            return row["id"]
    return await _with_retry(_run)


async def get_subtasks(task_id: int) -> list[dict]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM subtasks WHERE task_id = $1 ORDER BY position", task_id
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def toggle_subtask(subtask_id: int) -> bool:
    """Переключает done и возвращает новое значение."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE subtasks SET done = NOT done WHERE id = $1 RETURNING done", subtask_id
            )
            return row["done"]
    return await _with_retry(_run)


async def delete_subtask(subtask_id: int):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute("DELETE FROM subtasks WHERE id = $1", subtask_id)
    await _with_retry(_run)


async def get_subtask(subtask_id: int) -> dict | None:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM subtasks WHERE id = $1", subtask_id)
            return dict(row) if row else None
    return await _with_retry(_run)


# ---------- история ----------

async def log_history(task_id: int, user_id: int, title: str, event: str):
    """event: 'created' | 'done' | 'undone' | 'deleted' | 'rescheduled'"""
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_history (task_id, user_id, title, event) VALUES ($1, $2, $3, $4)",
                task_id, user_id, title, event
            )
    await _with_retry(_run)


async def get_history(user_id: int, limit: int = 20) -> list[dict]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_history WHERE user_id = $1 ORDER BY event_at DESC LIMIT $2",
                user_id, limit
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


# ---------- настройки пользователя (часовой пояс, язык, доступ к дашборду) ----------

async def get_user_settings(user_id: int) -> dict:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id = $1", user_id)
            if row:
                return dict(row)
            await conn.execute(
                "INSERT INTO user_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
            )
            row = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id = $1", user_id)
            return dict(row)
    return await _with_retry(_run)


async def set_user_timezone(user_id: int, tz_name: str):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_settings (user_id, timezone) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET timezone = $2""",
                user_id, tz_name
            )
    await _with_retry(_run)


async def set_user_language(user_id: int, lang: str):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_settings (user_id, language) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET language = $2""",
                user_id, lang
            )
    await _with_retry(_run)


async def set_dashboard_credentials(user_id: int, password_hash: str) -> str:
    """Генерирует уникальный токен (часть URL) и сохраняет хеш пароля. Возвращает токен."""
    token = secrets.token_urlsafe(16)

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_settings (user_id, dashboard_token, dashboard_password_hash)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id) DO UPDATE SET dashboard_token = $2, dashboard_password_hash = $3""",
                user_id, token, password_hash
            )
    await _with_retry(_run)
    return token


async def get_user_by_dashboard_token(token: str) -> dict | None:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_settings WHERE dashboard_token = $1", token
            )
            return dict(row) if row else None
    return await _with_retry(_run)
