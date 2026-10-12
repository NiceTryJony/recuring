"""Настройки владельца (user_settings / chat_settings) и токены дашборда."""

import secrets
from datetime import time

from . import core

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
        async with core._pool.acquire() as conn:
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
    return await core._with_retry(_run)


async def set_owner_timezone(owner_id: int, owner_type: str, tz_name: str):
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, timezone) VALUES ($1, $2)
                    ON CONFLICT ({id_col}) DO UPDATE SET timezone = $2""",
                owner_id, tz_name
            )
    await core._with_retry(_run)


async def set_owner_language(owner_id: int, owner_type: str, lang: str):
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, language) VALUES ($1, $2)
                    ON CONFLICT ({id_col}) DO UPDATE SET language = $2""",
                owner_id, lang
            )
    await core._with_retry(_run)


async def set_owner_quiet_hours(owner_id: int, owner_type: str, start: time | None, end: time | None):
    """start/end = None выключают тихий час."""
    table, id_col = _owner_table(owner_type)

    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, quiet_hours_start, quiet_hours_end) VALUES ($1, $2, $3)
                    ON CONFLICT ({id_col}) DO UPDATE SET quiet_hours_start = $2, quiet_hours_end = $3""",
                owner_id, start, end
            )
    await core._with_retry(_run)


async def set_owner_dashboard_credentials(owner_id: int, owner_type: str, password_hash: str) -> str:
    """Генерирует уникальный токен (часть URL) и сохраняет хеш пароля. Возвращает токен.

    dashboard_password_version инкрементится при КАЖДОЙ смене пароля (включая
    первое создание — там рост с дефолтного 0 до 1 безвреден, сессий ещё нет).
    Это то, на чём держится инвалидация старых cookie при смене пароля — см.
    миграцию в init_db и dashboard._sign_session/_verify_session."""
    table, id_col = _owner_table(owner_type)
    token = secrets.token_urlsafe(16)

    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {table} ({id_col}, dashboard_token, dashboard_password_hash, dashboard_password_version)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT ({id_col}) DO UPDATE
                    SET dashboard_token = $2, dashboard_password_hash = $3,
                        dashboard_password_version = {table}.dashboard_password_version + 1""",
                owner_id, token, password_hash
            )
    await core._with_retry(_run)
    return token


async def get_or_create_user_dashboard_token(user_id: int) -> tuple[str, int]:
    """Токен и dashboard_password_version личного дашборда; если токена ещё нет
    (пароль через /dashboard не задавали) — создаёт его БЕЗ пароля. Нужен для
    входа через Telegram Mini App: личность там подтверждает подпись initData.
    Существующий токен и версия не меняются, поэтому ссылка с паролем живёт."""
    token = secrets.token_urlsafe(16)

    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO user_settings (user_id, dashboard_token) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE
                   SET dashboard_token = COALESCE(user_settings.dashboard_token, $2)
                   RETURNING dashboard_token, dashboard_password_version""",
                user_id, token
            )
            return row["dashboard_token"], row["dashboard_password_version"]
    return await core._with_retry(_run)


async def get_owner_by_dashboard_token(token: str) -> dict | None:
    """Ищет токен сначала среди личных дашбордов, потом среди групповых."""
    async def _run():
        async with core._pool.acquire() as conn:
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
    return await core._with_retry(_run)


async def get_all_known_owners() -> list[dict]:
    """Все владельцы (личные пользователи и групповые чаты), которые когда-либо
    пользовались ботом — используется для рассылки ежедневной сводки (self-check)."""
    async def _run():
        async with core._pool.acquire() as conn:
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
    return await core._with_retry(_run)
