import os
from datetime import datetime, timezone

import asyncpg

# DATABASE_URL должен быть строкой Supabase "Session mode" (порт 5432, хост *.pooler.supabase.com)
# Transaction mode (6543) НЕ подходит — он не держит долгоживущие сессии/prepared statements,
# которые использует asyncpg, а наш бот — persistent-процесс, а не serverless-функция.
# Прямое подключение (db.*.supabase.co) тоже не берём — требует IPv6, на Render free его может не быть.
# Взять строку: Supabase Dashboard → Project Settings → Database → Connection string → Session pooler
DATABASE_URL = os.environ["DATABASE_URL"]

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