"""Хендлеры бота — по одному Router на фичу."""

from aiogram import Dispatcher

from ..middleware import ThrottlingMiddleware
from . import (
    add_task,
    basic,
    dashboard_cmd,
    fallback,
    find,
    history,
    listing,
    photos,
    subtasks,
    tags,
    task_actions,
    templates,
)


def register(dp: Dispatcher):
    """Порядок include_router = порядок срабатывания хендлеров, как было в
    одном файле: fallback (ловит всё остальное) обязан быть последним."""
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    for module in (templates, basic, add_task, listing, find, tags, history, dashboard_cmd,
                   task_actions, subtasks, photos, fallback):
        dp.include_router(module.router)
