"""Сохранение фото из Telegram к задаче: общая часть кнопки «📷 Фото»
(handlers/photos.py) и быстрого добавления задачи из фото (handlers/add_task.py)."""

import io

import dashboard
import db

from .app import bot


async def attach_photo(task_id: int, file_id: str, user_id: int, title: str) -> bool:
    """Скачивает фото, сжимает как дашборд и пишет в БД. False — файл не картинка."""
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    compressed = dashboard.compress_task_photo(buf.getvalue())
    if compressed is None:
        return False
    data_bytes, mime = compressed
    await db.add_task_photo(task_id, data_bytes, mime, uploaded_by=user_id)
    await db.log_history(task_id, user_id, title, "photo_added")
    return True
