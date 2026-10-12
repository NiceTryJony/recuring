"""Фото задач."""



from . import core

# ---------- фото задач ----------
# Лимит числа фото на задачу — защита от раздувания БД через форму на
# дашборде/бота; проверяется на вызывающей стороне (add_task_photo сам
# лимит не знает, чтобы не плодить гонки — см. komментарий в dashboard.py/bot.py).
MAX_PHOTOS_PER_TASK = 5


async def add_task_photo(task_id: int, data: bytes, mime: str, uploaded_by: int | None = None,
                         thumb: bytes | None = None) -> int:
    """thumb — необязательное превью (см. колонку task_photos.thumb). Если не
    передать, оно будет сделано лениво при первом запросе превью, поэтому
    старые вызывающие стороны (бот) ломать не нужно."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO task_photos (task_id, data, mime, uploaded_by, thumb)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                task_id, data, mime, uploaded_by, thumb,
            )
            return row["id"]
    return await core._with_retry(_run)


async def set_task_photo_thumb(photo_id: int, thumb: bytes):
    """Досоздание превью для фото, загруженного до появления колонки thumb
    (или ботом). Делается один раз на фото, при первом показе."""
    async def _run():
        async with core._pool.acquire() as conn:
            await conn.execute("UPDATE task_photos SET thumb = $1 WHERE id = $2", thumb, photo_id)
    await core._with_retry(_run)


async def get_task_photo(photo_id: int) -> dict | None:
    """Возвращает одно фото с байтами (для отдачи по GET) вместе с task_id —
    чтобы вызывающая сторона могла проверить принадлежность задаче/владельцу
    перед отдачей, не делая второй запрос."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM task_photos WHERE id = $1", photo_id)
            return dict(row) if row else None
    return await core._with_retry(_run)


async def get_task_photos_meta(task_id: int) -> list[dict]:
    """Список фото задачи БЕЗ байтов (id, mime, created_at) — для рендера
    превьюшек/галереи, где сами байты отдаются отдельным запросом на каждую
    картинку через <img src=".../photo/{id}">, а не инлайнятся все разом."""
    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, mime, uploaded_by, created_at FROM task_photos WHERE task_id = $1 ORDER BY created_at",
                task_id,
            )
            return [dict(r) for r in rows]
    return await core._with_retry(_run)


async def get_task_photo_thumb(photo_id: int) -> tuple[bytes | None, int] | None:
    """Только превью и task_id, без полного кадра. Нужна отдельно от
    get_task_photo, потому что та тащит из БД колонку data (сотни килобайт на
    фото), а для показа ленты нужны лишь байты превью. Возвращает
    (thumb_or_None, task_id) или None, если фото нет."""
    async def _run():
        async with core._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT thumb, task_id FROM task_photos WHERE id = $1", photo_id)
            return (row["thumb"], row["task_id"]) if row else None
    return await core._with_retry(_run)


async def get_task_photos_meta_bulk(task_ids: list[int]) -> dict[int, list[dict]]:
    """То же, что get_task_photos_meta, но сразу для списка задач — ОДНИМ
    запросом вместо запроса на задачу (дашборд рендерит десятки карточек за
    раз, и N+1 там был самой дорогой частью страницы). Возвращает
    {task_id: [фото...]}; задачи без фото в словаре отсутствуют."""
    if not task_ids:
        return {}

    async def _run():
        async with core._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, task_id, mime, uploaded_by, created_at FROM task_photos
                   WHERE task_id = ANY($1::int[]) ORDER BY task_id, created_at""",
                list(task_ids),
            )
            grouped: dict[int, list[dict]] = {}
            for r in rows:
                grouped.setdefault(r["task_id"], []).append(dict(r))
            return grouped
    return await core._with_retry(_run)


async def count_task_photos(task_id: int) -> int:
    async def _run():
        async with core._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM task_photos WHERE task_id = $1", task_id)
    return await core._with_retry(_run)


async def delete_task_photo(photo_id: int, task_id: int) -> bool:
    """task_id передаётся явно и входит в WHERE — страховка от удаления чужого
    фото по угаданному/перебранному id (вызывающая сторона и так должна
    проверить владельца задачи, но лишняя защита в самом запросе не мешает)."""
    async def _run():
        async with core._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM task_photos WHERE id = $1 AND task_id = $2", photo_id, task_id
            )
            return result.endswith(" 1")
    return await core._with_retry(_run)
