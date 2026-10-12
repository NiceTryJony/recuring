"""Telegram-бот: хендлеры, планировщик, клавиатуры и запуск. Точка входа — bot.py."""

from dotenv import load_dotenv

# До импорта подмодулей: constants.py читает BOT_TOKEN из окружения при импорте.
load_dotenv()
