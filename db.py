import os
from datetime import datetime, timezone
import re
from urllib.parse import quote

import asyncpg

# DATABASE_URL должен быть строкой Supabase "Session mode" (порт 5432, хост *.pooler.supabase.com)
# Transaction mode (6543) НЕ подходит — он не держит долгоживущие сессии/prepared statements,
# которые использует asyncpg, а наш бот — persistent-процесс, а не serverless-функция.
# Прямое подключение (db.*.supabase.co) тоже не берём — требует IPv6, на Render free его может не быть.
# Взять строку: Supabase Dashboard → Project Settings → Database → Connection string → Session pooler


def _normalize_db_url(raw_url: str) -> str:
    """Перекодирует пароль в connection string на случай, если в нём есть спецсимволы
    (@ : / # % и т.д.), которые иначе ломают парсинг URL и дают 'Invalid format for user or db_name'.

    Важно: режем СЫРУЮ строку вручную по последнему '@' до вызова urlsplit — сам urlsplit
    режет netloc по первому '@', и если пароль содержит '@', host/port уезжают не туда."""
    scheme_sep = "://"
    if scheme_sep not in raw_url:
        return raw_url
    scheme, rest = raw_url.split(scheme_sep, 1)

    if "@" not in rest:
        return raw_url  # нет userinfo — нечего перекодировать

    # Пароль может содержать '@' и '/', поэтому нельзя искать границу host/path
    # по первому '/' — она может оказаться внутри пароля. Вместо этого ищем
    # host:port с конца: это единственная часть строки вида "словоcифры[/путь]",
    # где после хоста идёт ':' + только цифры порта, а дальше либо конец, либо '/'.
    m = re.search(r"@([A-Za-z0-9.\-]+:\d+)(/.*)?$", rest)
    if not m:
        return raw_url  # не смогли надёжно распознать структуру — не трогаем строку
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


async def init_db():
    global _pool
    # statement_cache_size=0 — отключаем prepared statement cache asyncpg,
    # чтобы не ловить проблемы совместимости с PgBouncer/Supavisor в сессионном режиме
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
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


async def close_db():
    if _pool:
        await _pool.close()


async def add_task(user_id: int, title: str, due_at: datetime, repeat: str, remind: str) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tasks (user_id, title, due_at, repeat, remind)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            user_id, title, due_at, repeat, remind
        )
        return row["id"]


async def get_tasks(user_id: int, include_done: bool = False) -> list[dict]:
    async with _pool.acquire() as conn:
        if include_done:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE user_id = $1 ORDER BY done, due_at", user_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE user_id = $1 AND done = FALSE ORDER BY due_at", user_id
            )
        return [dict(r) for r in rows]


async def get_task(task_id: int) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
        return dict(row) if row else None


async def get_all_active_tasks() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM tasks WHERE done = FALSE")
        return [dict(r) for r in rows]


async def mark_done(task_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET done = TRUE WHERE id = $1", task_id)


async def delete_task(task_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)


async def update_task(task_id: int, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = list(fields.values())
    async with _pool.acquire() as conn:
        await conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = $1", task_id, *values)