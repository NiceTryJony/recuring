"""Связь с ботом для «Поделиться задачей» (сам Bot API живёт в bot.py)."""

from .config import logger

# Как и планировщик (scheduler_hooks.py): bot.py отдаёт сюда свою корутину при
# старте, импортировать bot.py отсюда нельзя — получился бы цикл.
_prepare_share_hook = None


def set_share_hooks(prepare=None):
    """prepare(user_id, title, text, button_text, link) -> id готового сообщения
    для WebApp.shareMessage (Bot API savePreparedInlineMessage)."""
    global _prepare_share_hook
    _prepare_share_hook = prepare


async def _prepare_share_message(user_id: int, title: str, text: str, button_text: str, link: str) -> str | None:
    """None — нативный диалог недоступен (нет хука, неизвестен Telegram-пользователь
    или Bot API отказал): клиент тогда делится обычной ссылкой."""
    if _prepare_share_hook is None or user_id <= 0:
        return None
    try:
        return await _prepare_share_hook(user_id, title, text, button_text, link)
    except Exception:
        logger.exception("Не удалось подготовить сообщение для шеринга")
        return None
