"""Защита от спама командами (rate-limit по пользователю и чату)."""

import asyncio

from i18n import t

from .app import bot
from .owner import owner_lang, resolve_owner

# ---------- throttling: защита от спама командами ----------
# Простой rate-limit по (user_id, chat_id): не чаще 1 сообщения в
# THROTTLE_RATE_SECONDS. При превышении — молча дропаем апдейт (без ответа,
# чтобы не плодить ещё больше трафика в спам-цикле) и один раз предупреждаем,
# пока троттлинг активен, чтобы не быть totally silent для обычного юзера,
# просто торопыги.
THROTTLE_RATE_SECONDS = 0.7

class ThrottlingMiddleware:
    def __init__(self, rate: float = THROTTLE_RATE_SECONDS):
        self.rate = rate
        self._last_seen: dict[tuple[int, int], float] = {}
        self._warned: set[tuple[int, int]] = set()

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None:
            return await handler(event, data)

        key = (chat.id, user.id)
        now = asyncio.get_event_loop().time()
        last = self._last_seen.get(key, 0.0)
        self._last_seen[key] = now

        if now - last < self.rate:
            if key not in self._warned:
                self._warned.add(key)
                try:
                    lang = await owner_lang(*(await resolve_owner(chat, user.id)))
                    await bot.send_message(chat.id, t(lang, "throttled"))
                except Exception:
                    pass
            return  # дропаем апдейт, хендлер не вызываем
        self._warned.discard(key)
        return await handler(event, data)
