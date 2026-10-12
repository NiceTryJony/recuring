"""Запуск: меню команд, health-сервер с дашбордом, graceful shutdown, main()."""

import asyncio
import os
import signal

from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiohttp import web as aioweb

import dashboard
import db

from . import app
from .app import bot, dp, logger
from .constants import DASHBOARD_BASE_URL
from .handlers import register as register_handlers
from .scheduling import _remove_task_jobs, restore_jobs, schedule_task

# ---------- меню команд Telegram (кнопка "/") ----------
# Telegram сам выбирает нужный набор по language_code КЛИЕНТА пользователя
# (не по языку, выбранному внутри бота через /lang) — так что человек видит
# понятные описания команд ещё до того, как вообще начал ими пользоваться,
# даже если потом выберет в боте другой язык через /lang. Набор без
# language_code (BotCommandScopeDefault без указания языка) — фолбэк для
# всех остальных языков клиента Telegram, которых нет в списке ниже.
_COMMAND_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "add": {"ru": "Добавить задачу", "en": "Add a task", "pl": "Dodaj zadanie"},
    "list": {"ru": "Список задач", "en": "List tasks", "pl": "Lista zadań"},
    "find": {"ru": "Найти задачу по названию", "en": "Find a task by title", "pl": "Znajdź zadanie po nazwie"},
    "tags": {"ru": "Задачи по тегам", "en": "Tasks by tag", "pl": "Zadania według tagów"},
    "history": {"ru": "История и статистика", "en": "History and stats", "pl": "Historia i statystyki"},
    "export": {"ru": "Выгрузить задачи в CSV", "en": "Export tasks to CSV", "pl": "Eksportuj zadania do CSV"},
    "timezone": {"ru": "Сменить часовой пояс", "en": "Change timezone", "pl": "Zmień strefę czasową"},
    "quiet": {"ru": "Тихий час (без уведомлений)", "en": "Quiet hours (no notifications)", "pl": "Cisza nocna (bez powiadomień)"},
    "morning": {"ru": "Утренняя сводка задач на день", "en": "Morning summary of today's tasks", "pl": "Poranne podsumowanie zadań na dziś"},
    "templates": {"ru": "Шаблоны задач", "en": "Task templates", "pl": "Szablony zadań"},
    "lang": {"ru": "Сменить язык бота", "en": "Change bot language", "pl": "Zmień język bota"},
    "dashboard": {"ru": "Веб-страница со списком задач", "en": "Web page with your tasks", "pl": "Strona z listą zadań"},
    "cancel": {"ru": "Отменить текущее действие", "en": "Cancel current action", "pl": "Anuluj bieżącą czynność"},
    "help": {"ru": "Все команды и что они делают", "en": "All commands explained", "pl": "Wszystkie komendy — opis"},
}

# Порядок в меню — как в /help, а не алфавитный (Telegram сохраняет порядок списка)
_COMMAND_ORDER = ["add", "templates", "list", "find", "tags", "history", "export", "timezone", "quiet", "morning", "lang", "dashboard", "cancel", "help"]

async def setup_bot_commands():
    for lang in ("ru", "en", "pl"):
        commands = [
            BotCommand(command=cmd, description=_COMMAND_DESCRIPTIONS[cmd][lang])
            for cmd in _COMMAND_ORDER
        ]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code=lang)
    # Фолбэк без language_code — для клиентов Telegram на языках вне ru/en/pl
    # (например, de, fr): используем английские описания как наиболее
    # универсальный вариант.
    fallback_commands = [
        BotCommand(command=cmd, description=_COMMAND_DESCRIPTIONS[cmd]["en"])
        for cmd in _COMMAND_ORDER
    ]
    await bot.set_my_commands(fallback_commands, scope=BotCommandScopeDefault())
    logger.info("Меню команд Telegram настроено (ru/en/pl + фолбэк)")
    # Mini App: кнопка меню в личке открывает дашборд без пароля (вход по
    # initData). Без DASHBOARD_BASE_URL (Telegram требует https) — не трогаем.
    if DASHBOARD_BASE_URL.startswith("https://"):
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="📋 Tasks", web_app=WebAppInfo(url=f"{DASHBOARD_BASE_URL.rstrip('/')}/dashboard/app")))

async def prepare_share_message(user_id: int, title: str, text: str, button_text: str, link: str) -> str:
    """Готовое сообщение «Поделиться задачей» для WebApp.shareMessage: текст и
    кнопка, которая открывает Mini App с кодом задачи (Bot API 8.0+)."""
    result = InlineQueryResultArticle(
        id="share", title=title,
        input_message_content=InputTextMessageContent(message_text=text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=link)]]),
    )
    prepared = await bot.save_prepared_inline_message(
        user_id=user_id, result=result,
        allow_user_chats=True, allow_group_chats=True, allow_bot_chats=False, allow_channel_chats=False,
    )
    return prepared.id

# ---------- health-check веб-сервер (нужен Render Web Service, чтобы видеть открытый порт) ----------

async def health(request):
    return aioweb.Response(text="ok")

async def run_health_server():
    app = aioweb.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    dashboard.register_dashboard_routes(app)
    # Дашборд живёт в этом же процессе, но импортировать bot.py у себя не может
    # (цикл импорта), поэтому отдаём ему наши планировщик-функции: задача,
    # созданная с веб-страницы, попадает в APScheduler сразу, а выполненная или
    # удалённая — снимает свои джобы. Без этого напоминание по задаче с
    # дашборда не приходило до рестарта процесса (restore_jobs).
    dashboard.set_scheduler_hooks(schedule=schedule_task, unschedule=_remove_task_jobs)
    dashboard.set_share_hooks(prepare=prepare_share_message)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = aioweb.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health server listening on port %s", port)
    return runner

# ---------- graceful shutdown ----------

_shutdown_event = asyncio.Event()

def _handle_signal(sig_name: str):
    logger.info("Получен сигнал %s, начинаю остановку...", sig_name)
    _shutdown_event.set()

# ---------- запуск ----------

async def main():
    await db.init_db()
    try:
        await setup_bot_commands()
    except Exception:
        # Не критично для работы бота — если Telegram API моргнул на старте,
        # просто остаёмся со старым меню (или без него), не блокируем запуск.
        logger.exception("Не удалось настроить меню команд Telegram")
    app.scheduler.start()
    await restore_jobs()
    health_runner = await run_health_server()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s.name))
        except NotImplementedError:
            pass  # Windows — сигналы не поддерживаются так, но на Render это Linux

    register_handlers(dp)
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    done, pending = await asyncio.wait(
        [polling_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
    )

    if shutdown_task in done:
        logger.info("Останавливаю polling...")
        await dp.stop_polling()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    logger.info("Останавливаю scheduler...")
    app.scheduler.shutdown(wait=False)
    logger.info("Закрываю health-сервер...")
    await health_runner.cleanup()
    logger.info("Закрываю пул БД...")
    await db.close_db()
    logger.info("Остановка завершена.")

if __name__ == "__main__":
    asyncio.run(main())
