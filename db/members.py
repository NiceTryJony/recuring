"""Участники групповых чатов и имена пользователей Telegram."""



from . import core

# ---------- участники групповых чатов ----------

async def add_chat_member(chat_id: int, user_id: int):
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_members (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                chat_id, user_id
            )
    await core._with_retry(_run)


async def get_chat_members(chat_id: int) -> list[int]:
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM chat_members WHERE chat_id = $1", chat_id)
            return [r["user_id"] for r in rows]
    return await core._with_retry(_run)


async def get_chats_for_user(user_id: int) -> list[int]:
    """Обратный запрос к get_chat_members — в каких групповых чатах состоит
    этот пользователь. Нужен для объединённой вечерней сводки: один человек
    может быть участником нескольких групп + иметь личные задачи, и раньше
    каждый owner (личка и каждая группа отдельно) слал СВОЮ сводку этому же
    user_id — см. send_daily_summary в bot.py, где теперь джоба идёт по
    user_id, а не по owner, и сама собирает все его контексты через эту
    функцию."""
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT chat_id FROM chat_members WHERE user_id = $1", user_id)
            return [r["chat_id"] for r in rows]
    return await core._with_retry(_run)


async def is_chat_member(chat_id: int, user_id: int) -> bool:
    """Используется для авторизации действий с задачами чата: нажатие кнопки
    приходит личным сообщением (бот рассылает уведомления в личку), поэтому
    нельзя просто сравнить chat.id нажавшего — нужно явно спросить БД, состоит
    ли этот user_id в участниках chat_id."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM chat_members WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
            )
            return row is not None
    return await core._with_retry(_run)


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
        async with core._pool.acquire() as conn:
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
    await core._with_retry(_run)


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
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name, (photo_data IS NOT NULL) AS has_photo "
                "FROM telegram_users WHERE user_id = ANY($1::bigint[])",
                user_ids
            )
            return {
                r["user_id"]: {"username": r["username"], "first_name": r["first_name"], "has_photo": r["has_photo"]}
                for r in rows
            }
    return await core._with_retry(_run)


async def get_telegram_user_photo(user_id: int) -> tuple[bytes, str] | None:
    """Сами байты аватарки — отдельный лёгкий запрос, только для роута показа
    картинки (/dashboard/{token}/avatar/{user_id}), не для батч-списков."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT photo_data, photo_mime FROM telegram_users WHERE user_id = $1", user_id
            )
            if not row or row["photo_data"] is None:
                return None
            return row["photo_data"], row["photo_mime"] or "image/jpeg"
    return await core._with_retry(_run)
