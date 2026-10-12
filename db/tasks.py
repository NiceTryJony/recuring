"""Задачи: CRUD, выборки, поиск."""

from datetime import datetime

from . import core

# ---------- задачи ----------
# owner_type: 'user' (личная задача, owner_id = user_id) | 'chat' (общая задача
# группы, owner_id = chat_id). Одна и та же таблица обслуживает оба случая без
# дублирования схемы.

async def add_task(
    owner_id: int, owner_type: str, title: str, due_at: datetime, repeat: str, remind: str,
    tag: str | None = None,
    remind_until_done: bool = False,
    created_by: int | None = None,
    description: str | None = None,
) -> int:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks (owner_id, owner_type, title, due_at, repeat, remind, tag, remind_until_done, created_by, description)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
                owner_id, owner_type, title, due_at, repeat, remind, tag, remind_until_done, created_by, description
            )
            return row["id"]
    return await core._with_retry(_run)


async def get_tasks(
    owner_id: int,
    owner_type: str = "user",
    include_done: bool = False,
    tag: str | None = None,
    filter_mode: str = "all",
    sort_by: str = "date",
) -> list[dict]:
    """filter_mode: 'all' | 'overdue' | 'this_week'. sort_by: 'date' | 'tag' | 'title'."""
    async def _run():
        async with core._pool.acquire() as conn:
            query = "SELECT * FROM tasks WHERE owner_id = $1 AND owner_type = $2"
            params = [owner_id, owner_type]
            if not include_done:
                query += " AND done = FALSE"
            if tag:
                params.append(tag)
                query += f" AND tag = ${len(params)}"
            if filter_mode == "overdue":
                query += " AND due_at < now()"
            elif filter_mode == "this_week":
                query += " AND due_at BETWEEN now() AND now() + interval '7 days'"

            order_map = {
                "date": "done, due_at",
                "tag": "done, tag NULLS LAST, due_at",
                "title": "done, title",
            }
            query += f" ORDER BY {order_map.get(sort_by, order_map['date'])}"

            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_task(task_id: int) -> dict | None:
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
            return dict(row) if row else None
    return await core._with_retry(_run)


async def get_all_active_tasks() -> list[dict]:
    """Все активные задачи системы вне зависимости от владельца — для восстановления
    джоб планировщика при рестарте процесса (restore_jobs сам разберёт owner_id/owner_type)."""
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tasks WHERE done = FALSE")
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_owner_tags(owner_id: int, owner_type: str = "user") -> list[str]:
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT tag FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND tag IS NOT NULL ORDER BY tag",
                owner_id, owner_type
            )
            return [r["tag"] for r in rows]
    return await core._with_retry(_run)


async def search_tasks(owner_id: int, owner_type: str, query_text: str, include_done: bool = False) -> list[dict]:
    """Регистронезависимый поиск по названию задачи (ILIKE %query%), в рамках одного владельца."""
    async def _run():
        async with core._pool.acquire() as conn:
            # экранируем \\, % и _, чтобы поиск "50%" или "a_b" не превращался в wildcard
            escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql = "SELECT * FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND title ILIKE $3 ESCAPE '\\'"
            params = [owner_id, owner_type, f"%{escaped}%"]
            if not include_done:
                sql += " AND done = FALSE"
            sql += " ORDER BY done, due_at"
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def mark_done(task_id: int):
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET done = TRUE, done_at = now() WHERE id = $1", task_id
            )
    await core._with_retry(_run)


async def mark_undone(task_id: int):
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET done = FALSE, done_at = NULL WHERE id = $1", task_id
            )
    await core._with_retry(_run)


async def set_last_notified(task_id: int, when: datetime):
    """Фиксирует момент последней отправки уведомления по задаче — защита от дублей
    при быстрых рестартах процесса (catch-up логика в restore_jobs не шлёт повторно
    в течение короткого окна после последнего уведомления)."""
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET last_notified_at = $2 WHERE id = $1", task_id, when
            )
    await core._with_retry(_run)


async def delete_task(task_id: int):
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
    await core._with_retry(_run)


# Поля tasks, которые реально обновляются через update_task (bot.py: snooze/
# смена повтора/названия/тега/даты; dashboard.py: postpone). Ключи **fields
# сейчас везде захардкожены на вызывающей стороне, так что инъекции имени
# колонки сегодня нет — но f-string с именами полей (ниже) означает, что
# стоит кому-то в будущем собрать fields из пользовательского ввода
# (например, динамическая форма редактирования), и это станет SQL-инъекцией
# через имя колонки. Белый список — дешёвая страховка от этого класса багов.
_UPDATABLE_TASK_FIELDS = {
    "title", "due_at", "repeat", "remind", "tag", "done", "done_at",
    "last_notified_at", "remind_until_done", "description",
}


async def update_task(task_id: int, **fields):
    if not fields:
        return
    unknown = fields.keys() - _UPDATABLE_TASK_FIELDS
    if unknown:
        raise ValueError(f"update_task: недопустимые поля {sorted(unknown)}")
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = list(fields.values())

    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = $1", task_id, *values)
    await core._with_retry(_run)
