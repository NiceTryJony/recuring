"""Таблица маршрутов дашборда."""

from aiohttp import web as aioweb

from .actions import (
    handle_share_accept, handle_subtask_create, handle_subtask_delete, handle_subtask_toggle,
    handle_task_create, handle_task_edit, handle_task_share, handle_task_toggle, handle_telegram_auth,
    handle_template_apply, handle_template_create, handle_template_delete, handle_webapp_auth,
)
from .assets import compression_middleware, handle_static
from .timing import timing_middleware
from .photos import handle_avatar, handle_task_photo_delete, handle_task_photo_get, handle_task_photo_upload
from .views import (
    handle_chart, handle_dashboard, handle_history, handle_share_preview, handle_templates_page, handle_webapp_entry,
)


def register_dashboard_routes(app: aioweb.Application):
    # Сжатие — на все ответы приложения (для не-текстовых и мелких это no-op).
    if compression_middleware not in app.middlewares:
        app.middlewares.append(compression_middleware)
    if timing_middleware not in app.middlewares:
        app.middlewares.append(timing_middleware)
    # Статика регистрируется первой: её путь /dashboard/static/... по форме
    # совпадает с /dashboard/{token}/..., а aiohttp матчит в порядке регистрации.
    app.router.add_get("/dashboard/static/{filename}", handle_static)
    # Mini App: /dashboard/app и /dashboard/webapp-auth — до {token}, иначе
    # их проглотил бы маршрут токена (токены token_urlsafe(16) так не выглядят).
    app.router.add_get("/dashboard/app", handle_webapp_entry)
    app.router.add_post("/dashboard/webapp-auth", handle_webapp_auth)
    app.router.add_get("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}", handle_dashboard)
    app.router.add_post("/dashboard/{token}/telegram-auth", handle_telegram_auth)
    app.router.add_post("/dashboard/{token}/tasks/new", handle_task_create)
    # Важно: более специфичные /photo-маршруты регистрируются ДО общего
    # /{action} — иначе POST .../tasks/{id}/photo перехватывался бы
    # handle_task_toggle как action="photo" (не входит в его allow-list
    # done/undone/postpone/delete) и отдавал 404 вместо загрузки фото.
    # aiohttp матчит маршруты в порядке регистрации, не по специфичности.
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/photo", handle_task_photo_upload)
    app.router.add_get("/dashboard/{token}/tasks/{task_id}/photo/{photo_id}", handle_task_photo_get)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/photo/{photo_id}/delete", handle_task_photo_delete)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/edit", handle_task_edit)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/subtasks/new", handle_subtask_create)
    app.router.add_post("/dashboard/{token}/subtasks/{subtask_id}/toggle", handle_subtask_toggle)
    app.router.add_post("/dashboard/{token}/subtasks/{subtask_id}/delete", handle_subtask_delete)
    # /share — тоже до общего /{action} (по той же причине, что и /photo).
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/share", handle_task_share)
    app.router.add_post("/dashboard/{token}/tasks/{task_id}/{action}", handle_task_toggle)
    app.router.add_get("/dashboard/{token}/share/{code}", handle_share_preview)
    app.router.add_post("/dashboard/{token}/share/{code}/accept", handle_share_accept)
    app.router.add_get("/dashboard/{token}/avatar/{user_id}", handle_avatar)
    app.router.add_get("/dashboard/{token}/chart", handle_chart)
    app.router.add_get("/dashboard/{token}/history", handle_history)
    app.router.add_get("/dashboard/{token}/templates", handle_templates_page)
    app.router.add_post("/dashboard/{token}/templates/new", handle_template_create)
    app.router.add_post("/dashboard/{token}/templates/{template_id}/apply", handle_template_apply)
    app.router.add_post("/dashboard/{token}/templates/{template_id}/delete", handle_template_delete)
