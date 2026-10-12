"""Утренняя и вечерняя сводки."""



from . import core


async def get_morning_summary_settings(user_id: int) -> dict | None:
    """None, если у пользователя вообще нет записи в user_settings (ни разу
    не писал боту) — вызывающая сторона трактует это как 'выключено'."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT morning_summary_enabled, morning_summary_hour, morning_summary_minute "
                "FROM user_settings WHERE user_id = $1", user_id,
            )
            return dict(row) if row else None
    return await core._with_retry(_run)


async def set_morning_summary(user_id: int, enabled: bool, hour: int | None = None, minute: int | None = None):
    """Апсерт — та же логика, что у остальных user_settings-полей (запись
    может ещё не существовать, если это первое касание пользователя к боту
    через именно эту команду, хотя на практике /start уже должен был её
    завести; ON CONFLICT подстраховывает от гонки/порядка вызовов)."""
    async def _run():
        async with core._pool.acquire() as conn:
            if hour is not None and minute is not None:
                await conn.execute(
                    """INSERT INTO user_settings (user_id, morning_summary_enabled, morning_summary_hour, morning_summary_minute)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (user_id) DO UPDATE SET
                           morning_summary_enabled = $2, morning_summary_hour = $3, morning_summary_minute = $4""",
                    user_id, enabled, hour, minute,
                )
            else:
                await conn.execute(
                    """INSERT INTO user_settings (user_id, morning_summary_enabled)
                       VALUES ($1, $2)
                       ON CONFLICT (user_id) DO UPDATE SET morning_summary_enabled = $2""",
                    user_id, enabled,
                )
    await core._with_retry(_run)


async def get_morning_summary_recipients() -> list[dict]:
    """user_id + час/минута для всех, у кого утренняя сводка включена —
    используется при restore_jobs() для восстановления джоб после рестарта."""
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, morning_summary_hour, morning_summary_minute "
                "FROM user_settings WHERE morning_summary_enabled = TRUE"
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_all_summary_recipients() -> list[int]:
    """Все user_id, которым нужна вечерняя сводка: личные пользователи бота
    (user_settings/owner задач) ПЛЮС участники групповых чатов (chat_members) —
    второе нужно, потому что человек мог ни разу не писать боту в личку
    (его просто добавили в группу), и тогда get_all_known_owners его бы не
    нашёл вовсе, джоба сводки для него не создалась бы при restore_jobs."""
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id FROM user_settings
                UNION
                SELECT DISTINCT owner_id FROM tasks WHERE owner_type = 'user'
                UNION
                SELECT DISTINCT user_id FROM chat_members
            """)
            return [r["user_id"] for r in rows]
    return await core._with_retry(_run)


async def get_daily_summary_counts(owner_id: int, owner_type: str = "user") -> dict:
    """Сколько активных задач и сколько выполнено за последние 24 часа — для self-check.

    overdue намеренно считает только repeat='none': у повторяющихся due_at не
    продвигается никогда (расписание целиком живёт в CronTrigger планировщика,
    см. bot.py:schedule_task), поэтому due_at < now() истинно для ЛЮБОЙ
    повторяющейся задачи почти сразу после первого срабатывания — включать их
    сюда значило бы показывать «просрочено» постоянно, даже когда всё штатно.

    stuck_repeats — отдельная, более узкая проверка именно для повторяющихся:
    задача, у которой уже должно было пройти хотя бы одно срабатывание (now() -
    due_at > 2 дня, чтобы не зацепить свежесозданные с ещё не наступившим
    первым разом), но last_notified_at либо пуст, либо давнее этого окна —
    вероятный признак слетевшей с планировщика джобы (например, исключение
    внутри send_due, которое не пересоздало job). Эвристика, не точный расчёт
    следующего срабатывания по repeat-типу — ложные срабатывания возможны для
    редких репитов (yearly), но это лучше, чем ничего не замечать вовсе."""
    async def _run():
        async with core._pool.acquire() as conn:
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND done = FALSE",
                owner_id, owner_type
            )
            done_today = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND done = TRUE AND done_at > now() - interval '24 hours'",
                owner_id, owner_type
            )
            overdue = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND done = FALSE AND due_at < now() AND repeat = 'none'",
                owner_id, owner_type
            )
            stuck_repeats = await conn.fetchval(
                """SELECT COUNT(*) FROM tasks
                   WHERE owner_id = $1 AND owner_type = $2 AND done = FALSE AND repeat != 'none'
                     AND due_at < now() - interval '2 days'
                     AND (last_notified_at IS NULL OR last_notified_at < now() - interval '2 days')""",
                owner_id, owner_type
            )
            return {"active": active, "done_today": done_today, "overdue": overdue, "stuck_repeats": stuck_repeats}
    return await core._with_retry(_run)
