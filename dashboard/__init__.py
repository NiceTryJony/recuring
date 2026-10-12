"""Веб-дашборд: HTML-страницы со списком задач, историей и шаблонами.

Пакет разбит по ответственности:

* config.py          — окружение и константы (BOT_TOKEN, секрет сессий, лимиты);
* security.py        — пароли, cookie-сессии, CSRF, Telegram Login, rate-limit входа;
* scheduler_hooks.py — связь с APScheduler бота;
* texts.py           — переводы страниц;
* assets.py          — статика (static/*.css, static/*.js) и gzip-сжатие;
* pages.py           — HTML-скелеты страниц;
* render.py          — рендер карточек, панели дней, графика;
* photos.py          — аватарки и фото задач;
* views.py           — GET-страницы;
* actions.py         — POST-действия;
* routes.py          — таблица маршрутов.

Здесь — только реэкспорт имён, которыми пользуются bot.py и тесты, чтобы
`import dashboard` / `dashboard.<имя>` работали как до разбиения модуля."""

from .actions import _parse_due_at, _resolve_template_due_at
from .config import BOT_TOKEN, BOT_USERNAME, CHART_DAYS, SESSION_MAX_AGE, SESSION_SECRET, UTC
from .pages import _login_response
from .photos import compress_task_photo, make_task_photo_thumb
from .render import _render_chart, _render_day_picker, _render_task
from .routes import register_dashboard_routes
from .scheduler_hooks import _schedule_task, _unschedule_task, set_scheduler_hooks
from .share_hooks import set_share_hooks
from .security import (
    _LOGIN_ATTEMPTS, _LOGIN_LOCKOUT_SECONDS, _LOGIN_MAX_ATTEMPTS, _LOGIN_WINDOW_SECONDS,
    _clear_login_attempts, _client_ip, _csrf_token, _login_rate_limited, _record_failed_login,
    _reset_login_limiter, _sign_session, _verify_csrf, _verify_session, hash_password,
    verify_password, verify_telegram_login, verify_webapp_init_data,
)
from .texts import DASHBOARD_TEXTS, _dows, _dt, _months
