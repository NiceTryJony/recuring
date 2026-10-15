"""История событий и статистика."""

from datetime import date

from . import core

# ---------- история ----------

async def log_history(task_id: int, user_id: int, title: str, event: str):
    """event: 'created' | 'done' | 'undone' | 'deleted' | 'rescheduled'.
    user_id — тот, кто реально выполнил действие (для общих задач чата это
    конкретный участник, а не chat_id), отдельно от владельца самой задачи."""
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_history (task_id, user_id, title, event) VALUES ($1, $2, $3, $4)",
                task_id, user_id, title, event
            )
    await core._with_retry(_run)


async def purge_old_history(days: int = 90) -> int:
    """Удаляет события истории старше `days` дней. Возвращает число удалённых строк."""
    async def _run():
        async with core._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM task_history WHERE event_at < now() - make_interval(days => $1)", days
            )
            return int(status.split()[-1])  # "DELETE <n>"
    return await core._with_retry(_run)


async def get_history(user_id: int, limit: int = 20) -> list[dict]:
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_history WHERE user_id = $1 ORDER BY event_at DESC LIMIT $2",
                user_id, limit
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


def _history_scope(user_id: int | None, chat_id: int | None, idx: int) -> tuple[str, int]:
    """Чьи события считаем. user_id — личная история (кто что сделал). chat_id — события по
    ОБЩИМ задачам этого чата (для группового дашборда: личные задачи участников туда не попадают)."""
    if chat_id is not None:
        return f"task_id IN (SELECT id FROM tasks WHERE owner_id = ${idx} AND owner_type = 'chat')", chat_id
    return f"user_id = ${idx}", user_id


async def get_history_stats(user_id: int | None, days: int, tz_name: str, chat_id: int | None = None) -> list[dict]:
    """События (created/done/deleted/...) по дням за последние `days` календарных дней
    В ЛОКАЛЬНОМ часовом поясе (включая сегодняшний). [{'day': date, 'event': str, 'cnt': int}]."""
    scope, scope_val = _history_scope(user_id, chat_id, 3)

    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT date_trunc('day', event_at AT TIME ZONE $1::text)::date AS day, event, COUNT(*) AS cnt
                    FROM task_history
                    WHERE {scope}
                      AND event_at >= (date_trunc('day', now() AT TIME ZONE $1::text)
                                       - ($2::int - 1) * interval '1 day') AT TIME ZONE $1::text
                    GROUP BY 1, 2
                    ORDER BY 1""",
                tz_name, days, scope_val
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_completion_rate(owner_id: int, owner_type: str, days: int, tz_name: str) -> dict:
    """Честный % выполнения за период: доля задач, у которых due_at попал в
    последние `days` календарных дней (в ЛОКАЛЬНОМ поясе), которые отмечены
    done. В отличие от get_history_stats (считает СОБЫТИЯ created/done за
    период — задача, созданная месяц назад и выполненная вчера, не попала бы
    в 'created' вчерашнего дня, искажая процент), здесь смотрим на состояние
    самих задач по их сроку, а не на историю событий — так "78% задач за
    месяц" действительно означает "из задач со сроком в этом месяце 78%
    закрыты", что и ожидает увидеть пользователь.
    Повторяющиеся задачи (repeat != 'none') исключены из знаменателя: у них
    одна строка в tasks с постоянно сдвигающимся due_at, а done неизменно
    сбрасывается в FALSE после каждого цикла (см. планировщик в bot.py) — их
    включение считало бы почти любую повторяющуюся задачу «невыполненной»
    на момент снятия среза, искажая процент не в её пользу."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE repeat = 'none') AS total,
                       COUNT(*) FILTER (WHERE repeat = 'none' AND done) AS done_count
                   FROM tasks
                   WHERE owner_id = $1 AND owner_type = $2
                     AND due_at >= (date_trunc('day', now() AT TIME ZONE $3::text)
                                    - ($4::int - 1) * interval '1 day') AT TIME ZONE $3::text
                     AND due_at < (date_trunc('day', now() AT TIME ZONE $3::text)
                                   + interval '1 day') AT TIME ZONE $3::text""",
                owner_id, owner_type, tz_name, days,
            )
            total = row["total"] or 0
            done_count = row["done_count"] or 0
            percent = round(done_count / total * 100) if total > 0 else None
            return {"total": total, "done": done_count, "percent": percent}
    return await core._with_retry(_run)


async def get_event_feed(
    user_id: int | None, chat_id: int | None, limit: int = 30, offset: int = 0,
) -> list[dict]:
    """Постраничная лента событий (created/done/undone/deleted/rescheduled) для
    дашборда — в отличие от get_history (которая отдаёт только последние 20 без
    пагинации и только для личного user_id), эта поддерживает offset и chat_id."""
    scope, scope_val = _history_scope(user_id, chat_id, 3)

    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM task_history WHERE {scope} ORDER BY event_at DESC LIMIT $1 OFFSET $2",
                limit, offset, scope_val
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_done_days(user_id: int | None, tz_name: str, chat_id: int | None = None, limit: int = 400) -> list[date]:
    """Различные локальные дни, в которые было хотя бы одно выполнение (для стрика), новые -> старые."""
    scope, scope_val = _history_scope(user_id, chat_id, 2)

    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT DISTINCT date_trunc('day', event_at AT TIME ZONE $1::text)::date AS day
                    FROM task_history
                    WHERE event = 'done' AND {scope}
                    ORDER BY day DESC LIMIT $3""",
                tz_name, scope_val, limit
            )
            return [r["day"] for r in rows]
    return await core._with_retry(_run)
