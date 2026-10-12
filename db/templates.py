"""Шаблоны задач."""



from . import core

# ---------- шаблоны задач ----------
MAX_TEMPLATES_PER_OWNER = 30  # защита от бесконтрольного роста — с запасом для семьи/группы


async def add_template(
    owner_id: int, owner_type: str, title: str, time_of_day: str, day_offset: int,
    repeat: str = "none", remind: str = "0", tag: str | None = None,
    remind_until_done: bool = False, created_by: int | None = None,
) -> int:
    async def _run():
        async with core._pool.acquire() as conn:
            next_order = await conn.fetchval(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM task_templates WHERE owner_id = $1 AND owner_type = $2",
                owner_id, owner_type,
            )
            row = await conn.fetchrow(
                """INSERT INTO task_templates
                       (owner_id, owner_type, title, time_of_day, day_offset, repeat, remind, tag,
                        remind_until_done, created_by, sort_order)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id""",
                owner_id, owner_type, title, time_of_day, day_offset, repeat, remind, tag,
                remind_until_done, created_by, next_order,
            )
            return row["id"]
    return await core._with_retry(_run)


async def count_templates(owner_id: int, owner_type: str) -> int:
    async def _run():
        async with core._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM task_templates WHERE owner_id = $1 AND owner_type = $2", owner_id, owner_type
            )
    return await core._with_retry(_run)


async def get_templates(owner_id: int, owner_type: str) -> list[dict]:
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_templates WHERE owner_id = $1 AND owner_type = $2 ORDER BY sort_order, id",
                owner_id, owner_type,
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_template(template_id: int) -> dict | None:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM task_templates WHERE id = $1", template_id)
            return dict(row) if row else None
    return await core._with_retry(_run)


async def delete_template(template_id: int, owner_id: int, owner_type: str) -> bool:
    """owner_id/owner_type в WHERE — та же страховка от удаления чужого шаблона
    по угаданному id, что и у delete_task_photo."""
    async def _run():
        async with core._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM task_templates WHERE id = $1 AND owner_id = $2 AND owner_type = $3",
                template_id, owner_id, owner_type,
            )
            return result.endswith(" 1")
    return await core._with_retry(_run)
