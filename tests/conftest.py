"""Общие настройки тестов.

bot.py и db.py читают BOT_TOKEN / DATABASE_URL при импорте, поэтому переменные
выставляем ДО любых импортов проекта. DATABASE_URL принудительно перезаписывается
(даже если в окружении разработчика лежит боевая строка) — юнит-тесты никогда не
должны случайно подключиться к рабочей базе."""

import os

os.environ["BOT_TOKEN"] = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "postgresql://test:test@localhost:5432/test"
