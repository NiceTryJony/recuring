"""Коды ссылок «Поделиться задачей»."""

import secrets

from . import core

# ---------- «Поделиться задачей» ----------

async def get_or_create_task_share(task_id: int, created_by: int | None) -> str:
    """Код ссылки на задачу; повторный вызов отдаёт тот же код. Префикс 's_'
    отличает его от других значений start_param Mini App."""
    code = "s_" + secrets.token_urlsafe(9)

    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO task_shares (code, task_id, created_by) VALUES ($1, $2, $3)
                   ON CONFLICT (task_id) DO UPDATE SET task_id = EXCLUDED.task_id
                   RETURNING code""",
                code, task_id, created_by
            )
            return row["code"]
    return await core._with_retry(_run)


async def get_task_by_share_code(code: str) -> dict | None:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.* FROM task_shares s JOIN tasks t ON t.id = s.task_id WHERE s.code = $1", code
            )
            return dict(row) if row else None
    return await core._with_retry(_run)
