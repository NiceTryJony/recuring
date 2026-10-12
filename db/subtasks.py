"""Подзадачи."""



from . import core

# ---------- подзадачи ----------

async def add_subtask(task_id: int, title: str) -> int:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO subtasks (task_id, title, position)
                   VALUES ($1, $2, COALESCE((SELECT MAX(position)+1 FROM subtasks WHERE task_id=$1), 0))
                   RETURNING id""",
                task_id, title
            )
            return row["id"]
    return await core._with_retry(_run)


async def get_subtasks(task_id: int) -> list[dict]:
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM subtasks WHERE task_id = $1 ORDER BY position", task_id
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_subtasks_bulk(task_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-версия get_subtasks — тем же принципом, что get_task_photos_meta_bulk."""
    if not task_ids:
        return {}

    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM subtasks WHERE task_id = ANY($1::int[]) ORDER BY task_id, position",
                list(task_ids),
            )
            grouped: dict[int, list[dict]] = {}
            for r in rows:
                grouped.setdefault(r["task_id"], []).append(dict(r))
            return grouped
    return await core._with_retry(_run)


async def toggle_subtask(subtask_id: int) -> bool:
    """Переключает done и возвращает новое значение."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE subtasks SET done = NOT done WHERE id = $1 RETURNING done", subtask_id
            )
            return row["done"]
    return await core._with_retry(_run)


async def delete_subtask(subtask_id: int):
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute("DELETE FROM subtasks WHERE id = $1", subtask_id)
    await core._with_retry(_run)


async def get_subtask(subtask_id: int) -> dict | None:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM subtasks WHERE id = $1", subtask_id)
            return dict(row) if row else None
    return await core._with_retry(_run)
