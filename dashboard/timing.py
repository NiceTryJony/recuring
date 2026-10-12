"""Замер времени запросов дашборда: Server-Timing (по запросу) и лог медленных.

Время БД набегает в db.core.db_timing (см. _with_retry), поэтому видно, сколько
запросов и миллисекунд ушло на страницу — это и есть главная метрика для
оптимизации на телефоне."""

import time

from aiohttp import web as aioweb

import db

from .config import SERVER_TIMING, SLOW_REQUEST_MS, logger


@aioweb.middleware
async def timing_middleware(request: aioweb.Request, handler):
    box = [0.0, 0]          # [мс в БД, число запросов]
    ctx_token = db.core.db_timing.set(box)
    started = time.perf_counter()
    try:
        resp = await handler(request)
    finally:
        db.core.db_timing.reset(ctx_token)
    total = (time.perf_counter() - started) * 1000
    if SERVER_TIMING:
        try:
            resp.headers["Server-Timing"] = f'db;dur={box[0]:.1f};desc="{box[1]} queries", app;dur={total:.1f}'
        except (AttributeError, TypeError):
            pass            # потоковый/нестандартный ответ — просто без заголовка
    if total > SLOW_REQUEST_MS:
        # canonical — шаблон маршрута (/dashboard/{token}/…), а не реальный путь: токен в логи не пишем.
        route = getattr(request.match_info.route.resource, "canonical", "?")
        logger.warning("Медленный запрос %s %s: %.0f мс (БД %.0f мс, %d запросов)",
                       request.method, route, total, box[0], box[1])
    return resp
