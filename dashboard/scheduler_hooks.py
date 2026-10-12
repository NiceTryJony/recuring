"""Связь с планировщиком бота (APScheduler живёт в bot.py)."""

from datetime import datetime

from .config import UTC, logger

# ---------- связь с планировщиком бота ----------
# Дашборд работает в том же процессе, что и бот (bot.py вызывает
# register_dashboard_routes на своём aiohttp-приложении), но импортировать
# bot.py отсюда нельзя — он сам импортирует этот модуль, получился бы цикл.
# Поэтому bot.py при старте отдаёт сюда две свои функции (schedule_task и
# _remove_task_jobs) через set_scheduler_hooks. Без этого задача, созданная с
# дашборда, вообще не попадала в APScheduler — напоминание по ней не
# приходило до перезапуска процесса (restore_jobs), а удалённая с дашборда
# задача наоборот оставляла после себя живые джобы.
_schedule_task_hook = None
_unschedule_task_hook = None


def set_scheduler_hooks(schedule=None, unschedule=None):
    global _schedule_task_hook, _unschedule_task_hook
    _schedule_task_hook = schedule
    _unschedule_task_hook = unschedule


def _schedule_task(task_id: int, owner_id: int, owner_type: str, due_at: datetime,
                   repeat: str, remind: str, tz, remind_until_done: bool = False):
    """Хуки намеренно необязательны: тесты и запуск дашборда отдельным
    процессом работают и без планировщика — тогда это no-op, как было раньше.
    Сбой планирования не должен ронять уже выполненное действие в БД, поэтому
    исключение только логируется."""
    if _schedule_task_hook is None:
        return
    try:
        _schedule_task_hook(task_id, owner_id, owner_type, due_at.astimezone(UTC),
                            repeat, remind, tz, remind_until_done=remind_until_done)
    except Exception:
        logger.exception("Не удалось запланировать задачу %s, созданную с дашборда", task_id)


def _unschedule_task(task_id: int):
    if _unschedule_task_hook is None:
        return
    try:
        _unschedule_task_hook(task_id)
    except Exception:
        logger.exception("Не удалось снять джобы задачи %s с дашборда", task_id)
