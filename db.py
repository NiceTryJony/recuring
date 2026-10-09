import logging
import os
import re
import secrets
from datetime import date, datetime, time
from urllib.parse import quote, unquote

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


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                owner_type TEXT NOT NULL DEFAULT 'user',
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

        # Миграция для баз, созданных до группового режима: раньше у tasks была
        # колонка user_id, теперь — owner_id + owner_type ('user' | 'chat').
        # Переименовываем колонку (если она ещё старая) и бэкафиллим owner_type,
        # чтобы все существующие личные задачи остались личными.
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
        )
        colnames = {r["column_name"] for r in cols}
        if "owner_id" not in colnames and "user_id" in colnames:
            await conn.execute("ALTER TABLE tasks RENAME COLUMN user_id TO owner_id")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_type TEXT")
        await conn.execute("UPDATE tasks SET owner_type = 'user' WHERE owner_type IS NULL")
        await conn.execute("ALTER TABLE tasks ALTER COLUMN owner_type SET DEFAULT 'user'")
        await conn.execute("ALTER TABLE tasks ALTER COLUMN owner_type SET NOT NULL")

        # Миграция для существующих баз (добавление колонок, если их ещё нет)
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by BIGINT")
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

        # Настройки группового чата — отдельная таблица, а не записи в
        # user_settings с chat_id вместо user_id: у группы нет одного
        # "хозяина" настроек, так что отдельный неймспейс честнее и проще
        # для ON CONFLICT-апсертов.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id BIGINT PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',
                language TEXT NOT NULL DEFAULT 'ru',
                dashboard_token TEXT UNIQUE,
                dashboard_password_hash TEXT
            )
        """)

        # Участники групповых чатов — заполняется по мере активности (Telegram
        # не отдаёт список участников группы без прав администратора боту), чтобы
        # бот знал, кому из участников слать личные уведомления по общим задачам.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        # Тихий час (локальное время владельца) — у user_settings и chat_settings,
        # т.к. get_owner_settings/_owner_table обслуживают обе таблицы единообразно.
        for tbl in ("user_settings", "chat_settings"):
            await conn.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS quiet_hours_start TIME")
            await conn.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS quiet_hours_end TIME")

        # Версия пароля дашборда — инкрементится при каждой смене пароля
        # (см. set_owner_dashboard_credentials) и зашита в подпись сессионной
        # cookie (dashboard._sign_session). Это и есть инвалидация старых
        # сессий: сменил пароль -> версия выросла -> все ранее выданные cookie
        # с старой версией больше не проходят _verify_session, даже если TTL
        # (неделя) ещё не истёк.
        for tbl in ("user_settings", "chat_settings"):
            await conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS dashboard_password_version INTEGER NOT NULL DEFAULT 0"
            )

        # «Повторять напоминание, пока не выполню»
        await conn.execute(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS remind_until_done BOOLEAN NOT NULL DEFAULT FALSE"
        )

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

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, owner_type)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_subtasks_task_id ON subtasks(task_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user_id ON task_history(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user_event_at ON task_history(user_id, event_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_members_chat_id ON chat_members(chat_id)")

        # Имена для отображения в истории/на дашборде — Telegram не хранится нигде
        # больше в схеме (только user_id). Заполняется при входе через Telegram
        # Login Widget на дашборде; данные могут устаревать (человек сменил имя) —
        # ON CONFLICT обновляет их на каждый новый вход.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Храним уже сжатую картинку (до ~128px, JPEG) прямо в БД как bytea —
        # для масштаба "семья из нескольких человек" это десятки-сотни КБ
        # суммарно, а показ аватарки не требует обращения к Bot API каждый раз.
        await conn.execute("ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS photo_data BYTEA")
        await conn.execute("ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS photo_mime TEXT")

        # Пользовательские шаблоны задач — один шаблон = одна задача (title +
        # время дня + смещение в днях + repeat/remind/tag), применяется кнопкой,
        # создавая реальную задачу с due_at, вычисленным от момента применения.
        # Встроенные (built-in) шаблоны НЕ хранятся здесь — они hardcode в
        # bot.py (templates.py) с переводом на лету по языку пользователя, см.
        # комментарий у BUILTIN_TEMPLATES. owner_id/owner_type — те же, что у
        # задач: личные шаблоны пользователя или общие шаблоны группы.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_templates (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                owner_type TEXT NOT NULL DEFAULT 'user',
                title TEXT NOT NULL,
                time_of_day TEXT NOT NULL,
                day_offset SMALLINT NOT NULL DEFAULT 0,
                repeat TEXT NOT NULL DEFAULT 'none',
                remind TEXT NOT NULL DEFAULT '0',
                tag TEXT,
                remind_until_done BOOLEAN NOT NULL DEFAULT FALSE,
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_templates_owner ON task_templates(owner_id, owner_type)")

        # Утренняя сводка — отдельное от вечерней время, личная настройка
        # человека (не группы: сводка теперь всегда идёт по user_id, см.
        # bot.py:send_daily_summary/send_morning_summary). Выключена по
        # умолчанию — включается явно командой /morning, чтобы не начать
        # неожиданно слать новый тип сообщений всем существующим пользователям
        # бота сразу после деплоя этой фичи.
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS morning_summary_enabled BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS morning_summary_hour SMALLINT NOT NULL DEFAULT 8")
        await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS morning_summary_minute SMALLINT NOT NULL DEFAULT 0")

        # Фото, прикреплённые к задачам (не аватарки) — отдельная таблица
        # "один-ко-многим" на случай нескольких фото на задачу. Байты хранятся
        # прямо в БД (как и telegram_users.photo_data) — показ не требует
        # похода на Bot API/диск, а масштаб (до нескольких МБ на задачу,
        # сжатых до JPEG) комфортен для Supabase free tier.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_photos (
                id SERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                data BYTEA NOT NULL,
                mime TEXT NOT NULL,
                uploaded_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_photos_task_id ON task_photos(task_id)")


async def close_db():
    if _pool:
        await _pool.close()


# ---------- задачи ----------
# owner_type: 'user' (личная задача, owner_id = user_id) | 'chat' (общая задача
# группы, owner_id = chat_id). Одна и та же таблица обслуживает оба случая без
# дублирования схемы.

async def add_task(
    owner_id: int, owner_type: str, title: str, due_at: datetime, repeat: str, remind: str,
    tag: str | None = None,
    remind_until_done: bool = False,
    created_by: int | None = None,
) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks (owner_id, owner_type, title, due_at, repeat, remind, tag, remind_until_done, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
                owner_id, owner_type, title, due_at, repeat, remind, tag, remind_until_done, created_by
            )
            return row["id"]
    return await _with_retry(_run)


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
        async with _pool.acquire() as conn:
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
    return await _with_retry(_run)


async def get_task(task_id: int) -> dict | None:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
            return dict(row) if row else None
    return await _with_retry(_run)


async def get_all_active_tasks() -> list[dict]:
    """Все активные задачи системы вне зависимости от владельца — для восстановления
    джоб планировщика при рестарте процесса (restore_jobs сам разберёт owner_id/owner_type)."""
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tasks WHERE done = FALSE")
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_owner_tags(owner_id: int, owner_type: str = "user") -> list[str]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT tag FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND tag IS NOT NULL ORDER BY tag",
                owner_id, owner_type
            )
            return [r["tag"] for r in rows]
    return await _with_retry(_run)


async def search_tasks(owner_id: int, owner_type: str, query_text: str, include_done: bool = False) -> list[dict]:
    """Регистронезависимый поиск по названию задачи (ILIKE %query%), в рамках одного владельца."""
    async def _run():
        async with _pool.acquire() as conn:
            # экранируем \\, % и _, чтобы поиск "50%" или "a_b" не превращался в wildcard
            escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql = "SELECT * FROM tasks WHERE owner_id = $1 AND owner_type = $2 AND title ILIKE $3 ESCAPE '\\'"
            params = [owner_id, owner_type, f"%{escaped}%"]
            if not include_done:
                sql += " AND done = FALSE"
            sql += " ORDER BY done, due_at"
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]
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


# Поля tasks, которые реально обновляются через update_task (bot.py: snooze/
# смена повтора/названия/тега/даты; dashboard.py: postpone). Ключи **fields
# сейчас везде захардкожены на вызывающей стороне, так что инъекции имени
# колонки сегодня нет — но f-string с именами полей (ниже) означает, что
# стоит кому-то в будущем собрать fields из пользовательского ввода
# (например, динамическая форма редактирования), и это станет SQL-инъекцией
# через имя колонки. Белый список — дешёвая страховка от этого класса багов.
_UPDATABLE_TASK_FIELDS = {
    "title", "due_at", "repeat", "remind", "tag", "done", "done_at",
    "last_notified_at", "remind_until_done",
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
        async with _pool.acquire() as conn:
            await conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = $1", task_id, *values)
    await _with_retry(_run)


# ---------- фото задач ----------
# Лимит числа фото на задачу — защита от раздувания БД через форму на
# дашборде/бота; проверяется на вызывающей стороне (add_task_photo сам
# лимит не знает, чтобы не плодить гонки — см. komментарий в dashboard.py/bot.py).
MAX_PHOTOS_PER_TASK = 5


async def add_task_photo(task_id: int, data: bytes, mime: str, uploaded_by: int | None = None) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO task_photos (task_id, data, mime, uploaded_by) VALUES ($1, $2, $3, $4) RETURNING id",
                task_id, data, mime, uploaded_by,
            )
            return row["id"]
    return await _with_retry(_run)


async def get_task_photo(photo_id: int) -> dict | None:
    """Возвращает одно фото с байтами (для отдачи по GET) вместе с task_id —
    чтобы вызывающая сторона могла проверить принадлежность задаче/владельцу
    перед отдачей, не делая второй запрос."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM task_photos WHERE id = $1", photo_id)
            return dict(row) if row else None
    return await _with_retry(_run)


async def get_task_photos_meta(task_id: int) -> list[dict]:
    """Список фото задачи БЕЗ байтов (id, mime, created_at) — для рендера
    превьюшек/галереи, где сами байты отдаются отдельным запросом на каждую
    картинку через <img src=".../photo/{id}">, а не инлайнятся все разом."""
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, mime, uploaded_by, created_at FROM task_photos WHERE task_id = $1 ORDER BY created_at",
                task_id,
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def count_task_photos(task_id: int) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM task_photos WHERE task_id = $1", task_id)
    return await _with_retry(_run)


async def delete_task_photo(photo_id: int, task_id: int) -> bool:
    """task_id передаётся явно и входит в WHERE — страховка от удаления чужого
    фото по угаданному/перебранному id (вызывающая сторона и так должна
    проверить владельца задачи, но лишняя защита в самом запросе не мешает)."""
    async def _run():
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM task_photos WHERE id = $1 AND task_id = $2", photo_id, task_id
            )
            return result.endswith(" 1")
    return await _with_retry(_run)


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
    """event: 'created' | 'done' | 'undone' | 'deleted' | 'rescheduled'.
    user_id — тот, кто реально выполнил действие (для общих задач чата это
    конкретный участник, а не chat_id), отдельно от владельца самой задачи."""
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
        async with _pool.acquire() as conn:
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
    return await _with_retry(_run)


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
        async with _pool.acquire() as conn:
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
    return await _with_retry(_run)


async def get_event_feed(
    user_id: int | None, chat_id: int | None, limit: int = 30, offset: int = 0,
) -> list[dict]:
    """Постраничная лента событий (created/done/undone/deleted/rescheduled) для
    дашборда — в отличие от get_history (которая отдаёт только последние 20 без
    пагинации и только для личного user_id), эта поддерживает offset и chat_id."""
    scope, scope_val = _history_scope(user_id, chat_id, 3)

    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM task_history WHERE {scope} ORDER BY event_at DESC LIMIT $1 OFFSET $2",
                limit, offset, scope_val
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_done_days(user_id: int | None, tz_name: str, chat_id: int | None = None, limit: int = 400) -> list[date]:
    """Различные локальные дни, в которые было хотя бы одно выполнение (для стрика), новые -> старые."""
    scope, scope_val = _history_scope(user_id, chat_id, 2)

    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT DISTINCT date_trunc('day', event_at AT TIME ZONE $1::text)::date AS day
                    FROM task_history
                    WHERE event = 'done' AND {scope}
                    ORDER BY day DESC LIMIT $3""",
                tz_name, scope_val, limit
            )
            return [r["day"] for r in rows]
    return await _with_retry(_run)


# ---------- участники групповых чатов ----------

async def add_chat_member(chat_id: int, user_id: int):
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_members (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                chat_id, user_id
            )
    await _with_retry(_run)


async def get_chat_members(chat_id: int) -> list[int]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM chat_members WHERE chat_id = $1", chat_id)
            return [r["user_id"] for r in rows]
    return await _with_retry(_run)


async def get_chats_for_user(user_id: int) -> list[int]:
    """Обратный запрос к get_chat_members — в каких групповых чатах состоит
    этот пользователь. Нужен для объединённой вечерней сводки: один человек
    может быть участником нескольких групп + иметь личные задачи, и раньше
    каждый owner (личка и каждая группа отдельно) слал СВОЮ сводку этому же
    user_id — см. send_daily_summary в bot.py, где теперь джоба идёт по
    user_id, а не по owner, и сама собирает все его контексты через эту
    функцию."""
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT chat_id FROM chat_members WHERE user_id = $1", user_id)
            return [r["chat_id"] for r in rows]
    return await _with_retry(_run)


async def is_chat_member(chat_id: int, user_id: int) -> bool:
    """Используется для авторизации действий с задачами чата: нажатие кнопки
    приходит личным сообщением (бот рассылает уведомления в личку), поэтому
    нельзя просто сравнить chat.id нажавшего — нужно явно спросить БД, состоит
    ли этот user_id в участниках chat_id."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM chat_members WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
            )
            return row is not None
    return await _with_retry(_run)


# ---------- telegram_users: имена для отображения ----------

async def upsert_telegram_user(
    user_id: int, username: str | None, first_name: str | None,
    photo_data: bytes | None = None, photo_mime: str | None = None,
):
    """Вызывается при каждом успешном входе через Telegram Login Widget на
    дашборде — данные могут устареть (смена имени/username/фото), поэтому просто
    перезаписываем при каждом логине, а не только при первом появлении.
    photo_data: передавай None только если фото не получилось скачать в этот
    раз (сетевой сбой и т.п.) — COALESCE сохранит прежнее значение, а не сотрёт его."""
    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO telegram_users (user_id, username, first_name, photo_data, photo_mime, updated_at)
                   VALUES ($1, $2, $3, $4, $5, now())
                   ON CONFLICT (user_id) DO UPDATE
                   SET username = $2, first_name = $3,
                       photo_data = COALESCE($4, telegram_users.photo_data),
                       photo_mime = COALESCE($5, telegram_users.photo_mime),
                       updated_at = now()""",
                user_id, username, first_name, photo_data, photo_mime
            )
    await _with_retry(_run)


async def get_telegram_users(user_ids: list[int]) -> dict[int, dict]:
    """Батч-подгрузка имён/признака наличия фото по списку id — для рендера
    истории/дашборда без N+1 запросов. Специально НЕ тянем сами байты фото
    здесь (список задач может содержать десяток разных авторов — незачем
    гонять картинки в каждом SELECT'е списка), только has_photo — сами байты
    отдаёт get_telegram_user_photo() по одному id в сам момент показа картинки.
    Возвращает {user_id: {"username":..., "first_name":..., "has_photo": bool}};
    id без записи в таблице (человек ещё не логинился через виджет) просто
    отсутствуют в результате — вызывающий код сам решает, что показать (например,
    сырой id)."""
    if not user_ids:
        return {}
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name, (photo_data IS NOT NULL) AS has_photo "
                "FROM telegram_users WHERE user_id = ANY($1::bigint[])",
                user_ids
            )
            return {
                r["user_id"]: {"username": r["username"], "first_name": r["first_name"], "has_photo": r["has_photo"]}
                for r in rows
            }
    return await _with_retry(_run)


async def get_telegram_user_photo(user_id: int) -> tuple[bytes, str] | None:
    """Сами байты аватарки — отдельный лёгкий запрос, только для роута показа
    картинки (/dashboard/{token}/avatar/{user_id}), не для батч-списков."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT photo_data, photo_mime FROM telegram_users WHERE user_id = $1", user_id
            )
            if not row or row["photo_data"] is None:
                return None
            return row["photo_data"], row["photo_mime"] or "image/jpeg"
    return await _with_retry(_run)


# ---------- настройки владельца (личные user_settings или групповые chat_settings) ----------
# Единый интерфейс поверх двух таблиц: owner_type выбирает, с какой из них работать.
# Возвращаемый/ожидаемый owner_id — это user_id для 'user' и chat_id для 'chat'.

def _owner_table(owner_type: str) -> tuple[str, str]:
    if owner_type == "chat":
        return "chat_settings", "chat_id"
    return "user_settings", "user_id"


async def get_owner_settings(owner_id: int, owner_type: str = "user") -> dict:
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE {id_col} = $1", owner_id)
            if not row:
                await conn.execute(
                    f"INSERT INTO {table} ({id_col}) VALUES ($1) ON CONFLICT DO NOTHING", owner_id
                )
                row = await conn.fetchrow(f"SELECT * FROM {table} WHERE {id_col} = $1", owner_id)
            result = dict(row)
            result["owner_id"] = owner_id
            result["owner_type"] = owner_type
            return result
    return await _with_retry(_run)


async def set_owner_timezone(owner_id: int, owner_type: str, tz_name: str):
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, timezone) VALUES ($1, $2)
                    ON CONFLICT ({id_col}) DO UPDATE SET timezone = $2""",
                owner_id, tz_name
            )
    await _with_retry(_run)


async def set_owner_language(owner_id: int, owner_type: str, lang: str):
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, language) VALUES ($1, $2)
                    ON CONFLICT ({id_col}) DO UPDATE SET language = $2""",
                owner_id, lang
            )
    await _with_retry(_run)


async def set_owner_quiet_hours(owner_id: int, owner_type: str, start: time | None, end: time | None):
    """start/end = None выключают тихий час."""
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, quiet_hours_start, quiet_hours_end) VALUES ($1, $2, $3)
                    ON CONFLICT ({id_col}) DO UPDATE SET quiet_hours_start = $2, quiet_hours_end = $3""",
                owner_id, start, end
            )
    await _with_retry(_run)


async def set_owner_dashboard_credentials(owner_id: int, owner_type: str, password_hash: str) -> str:
    """Генерирует уникальный токен (часть URL) и сохраняет хеш пароля. Возвращает токен.

    dashboard_password_version инкрементится при КАЖДОЙ смене пароля (включая
    первое создание — там рост с дефолтного 0 до 1 безвреден, сессий ещё нет).
    Это то, на чём держится инвалидация старых cookie при смене пароля — см.
    миграцию в init_db и dashboard._sign_session/_verify_session."""
    table, id_col = _owner_table(owner_type)
    token = secrets.token_urlsafe(16)

    async def _run():
        async with _pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, dashboard_token, dashboard_password_hash, dashboard_password_version)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT ({id_col}) DO UPDATE
                    SET dashboard_token = $2, dashboard_password_hash = $3,
                        dashboard_password_version = {table}.dashboard_password_version + 1""",
                owner_id, token, password_hash
            )
    await _with_retry(_run)
    return token


async def get_owner_by_dashboard_token(token: str) -> dict | None:
    """Ищет токен сначала среди личных дашбордов, потом среди групповых."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM user_settings WHERE dashboard_token = $1", token)
            if row:
                result = dict(row)
                result["owner_id"] = result["user_id"]
                result["owner_type"] = "user"
                return result
            row = await conn.fetchrow("SELECT * FROM chat_settings WHERE dashboard_token = $1", token)
            if row:
                result = dict(row)
                result["owner_id"] = result["chat_id"]
                result["owner_type"] = "chat"
                return result
            return None
    return await _with_retry(_run)


async def get_all_known_owners() -> list[dict]:
    """Все владельцы (личные пользователи и групповые чаты), которые когда-либо
    пользовались ботом — используется для рассылки ежедневной сводки (self-check)."""
    async def _run():
        async with _pool.acquire() as conn:
            user_rows = await conn.fetch("""
                SELECT user_id FROM user_settings
                UNION
                SELECT DISTINCT owner_id FROM tasks WHERE owner_type = 'user'
            """)
            chat_rows = await conn.fetch("""
                SELECT chat_id FROM chat_settings
                UNION
                SELECT DISTINCT owner_id FROM tasks WHERE owner_type = 'chat'
            """)
            owners = [{"owner_id": r["user_id"], "owner_type": "user"} for r in user_rows]
            owners += [{"owner_id": r["chat_id"], "owner_type": "chat"} for r in chat_rows]
            return owners
    return await _with_retry(_run)


# ---------- шаблоны задач ----------
MAX_TEMPLATES_PER_OWNER = 30  # защита от бесконтрольного роста — с запасом для семьи/группы


async def add_template(
    owner_id: int, owner_type: str, title: str, time_of_day: str, day_offset: int,
    repeat: str = "none", remind: str = "0", tag: str | None = None,
    remind_until_done: bool = False, created_by: int | None = None,
) -> int:
    async def _run():
        async with _pool.acquire() as conn:
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
    return await _with_retry(_run)


async def count_templates(owner_id: int, owner_type: str) -> int:
    async def _run():
        async with _pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM task_templates WHERE owner_id = $1 AND owner_type = $2", owner_id, owner_type
            )
    return await _with_retry(_run)


async def get_templates(owner_id: int, owner_type: str) -> list[dict]:
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_templates WHERE owner_id = $1 AND owner_type = $2 ORDER BY sort_order, id",
                owner_id, owner_type,
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_template(template_id: int) -> dict | None:
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM task_templates WHERE id = $1", template_id)
            return dict(row) if row else None
    return await _with_retry(_run)


async def delete_template(template_id: int, owner_id: int, owner_type: str) -> bool:
    """owner_id/owner_type в WHERE — та же страховка от удаления чужого шаблона
    по угаданному id, что и у delete_task_photo."""
    async def _run():
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM task_templates WHERE id = $1 AND owner_id = $2 AND owner_type = $3",
                template_id, owner_id, owner_type,
            )
            return result.endswith(" 1")
    return await _with_retry(_run)


async def get_morning_summary_settings(user_id: int) -> dict | None:
    """None, если у пользователя вообще нет записи в user_settings (ни разу
    не писал боту) — вызывающая сторона трактует это как 'выключено'."""
    async def _run():
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT morning_summary_enabled, morning_summary_hour, morning_summary_minute "
                "FROM user_settings WHERE user_id = $1", user_id,
            )
            return dict(row) if row else None
    return await _with_retry(_run)


async def set_morning_summary(user_id: int, enabled: bool, hour: int | None = None, minute: int | None = None):
    """Апсерт — та же логика, что у остальных user_settings-полей (запись
    может ещё не существовать, если это первое касание пользователя к боту
    через именно эту команду, хотя на практике /start уже должен был её
    завести; ON CONFLICT подстраховывает от гонки/порядка вызовов)."""
    async def _run():
        async with _pool.acquire() as conn:
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
    await _with_retry(_run)


async def get_morning_summary_recipients() -> list[dict]:
    """user_id + час/минута для всех, у кого утренняя сводка включена —
    используется при restore_jobs() для восстановления джоб после рестарта."""
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, morning_summary_hour, morning_summary_minute "
                "FROM user_settings WHERE morning_summary_enabled = TRUE"
            )
            return [dict(r) for r in rows]
    return await _with_retry(_run)


async def get_all_summary_recipients() -> list[int]:
    """Все user_id, которым нужна вечерняя сводка: личные пользователи бота
    (user_settings/owner задач) ПЛЮС участники групповых чатов (chat_members) —
    второе нужно, потому что человек мог ни разу не писать боту в личку
    (его просто добавили в группу), и тогда get_all_known_owners его бы не
    нашёл вовсе, джоба сводки для него не создалась бы при restore_jobs."""
    async def _run():
        async with _pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id FROM user_settings
                UNION
                SELECT DISTINCT owner_id FROM tasks WHERE owner_type = 'user'
                UNION
                SELECT DISTINCT user_id FROM chat_members
            """)
            return [r["user_id"] for r in rows]
    return await _with_retry(_run)


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
        async with _pool.acquire() as conn:
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
    return await _with_retry(_run)