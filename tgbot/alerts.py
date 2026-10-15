"""Алерты о сбоях админу в Telegram: каждая запись лога уровня ERROR (в том числе
logger.exception, которых в коде много и которые иначе видны только в логах Render)
уходит в чат ADMIN_CHAT_ID. Без переменной — ничего не делает."""

import asyncio
import logging
import time

ALERT_REPEAT_SECONDS = 30 * 60   # одинаковый сбой — не чаще раза в 30 минут
ALERT_BURST = 5                   # и не больше 5 алертов за 10 минут вообще
ALERT_BURST_WINDOW = 10 * 60
MAX_TEXT = 3500                   # лимит сообщения Telegram — 4096


class AdminAlertHandler(logging.Handler):
    def __init__(self, send, loop_getter=asyncio.get_running_loop):
        super().__init__(level=logging.ERROR)
        self._send = send                     # async (text) -> None
        self._loop_getter = loop_getter
        self._seen: dict[str, float] = {}
        self._sent: list[float] = []

    def _allowed(self, key: str, now: float) -> bool:
        self._sent = [t for t in self._sent if now - t < ALERT_BURST_WINDOW]
        # Не .get(key, 0): monotonic() считает от старта системы, и на свежей машине
        # (CI, только что перезапущенный Render) первый же сбой выглядел бы «повтором»
        if len(self._sent) >= ALERT_BURST or (key in self._seen and now - self._seen[key] < ALERT_REPEAT_SECONDS):
            return False
        self._seen[key] = now
        self._sent.append(now)
        if len(self._seen) > 200:             # старые ключи не копим
            self._seen = {k: t for k, t in self._seen.items() if now - t < ALERT_REPEAT_SECONDS}
        return True

    def emit(self, record: logging.LogRecord):
        # Сбой самой отправки алерта не должен порождать новый алерт (петля)
        if record.name == "alerts":
            return
        try:
            key = f"{record.name}:{record.getMessage()[:120]}"
            if not self._allowed(key, time.monotonic()):
                return
            text = f"⚠️ {record.name}: {record.getMessage()}"
            if record.exc_info:
                text += "\n\n" + self.format(record)
            loop = self._loop_getter()
        except RuntimeError:
            return                            # нет работающего event loop — слать нечем
        except Exception:
            self.handleError(record)
            return
        loop.create_task(self._safe_send(text[:MAX_TEXT]))

    async def _safe_send(self, text: str):
        try:
            await self._send(text)
        except Exception:
            logging.getLogger("alerts").warning("Не удалось отправить алерт админу", exc_info=True)


def install(bot, admin_chat_id: int | None) -> AdminAlertHandler | None:
    """Вешает обработчик на корневой логгер (ловит и db, и dashboard, и aiohttp)."""
    if not admin_chat_id:
        return None

    async def send(text: str):
        await bot.send_message(admin_chat_id, text, disable_web_page_preview=True)

    handler = AdminAlertHandler(send)
    logging.getLogger().addHandler(handler)
    return handler
