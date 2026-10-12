"""Создание таблиц и миграции (init_db)."""



import asyncpg

from . import core


async def init_db():
    core._pool = await asyncpg.create_pool(
        core.DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    async with core._pool.acquire() as conn:
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
        # Развёрнутое описание задачи — опционально, заполняется и правится
        # только с дашборда (раскрывающаяся карточка); бот пока создаёт задачи
        # без него, но полноценно читает/показывает через get_task (SELECT *).
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS description TEXT")

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
        # Превью (уменьшенная копия того же кадра). Дашборд показывает фото в
        # ленте размером 118px, а в data лежит кадр до 1280px — отдавать его в
        # превью значило гнать в телефон в десятки раз больше байтов, чем он
        # способен показать. NULL означает "превью ещё не сделано" — такие
        # записи (все, загруженные до этой миграции, и всё, что приходит из
        # бота) дорендериваются на первом же запросе превью.
        await conn.execute("ALTER TABLE task_photos ADD COLUMN IF NOT EXISTS thumb BYTEA")

        # Коды «Поделиться задачей»: один код на задачу (UNIQUE), живёт, пока
        # жива задача. Код случайный — id задачи наружу не светится.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_shares (
                code TEXT PRIMARY KEY,
                task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
