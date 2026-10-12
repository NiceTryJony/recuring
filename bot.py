"""Точка входа: `python bot.py`. Вся логика бота — в пакете tgbot/."""

import asyncio

from tgbot.server import main

if __name__ == "__main__":
    asyncio.run(main())
