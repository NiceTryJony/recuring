"""Слой БД (PostgreSQL через asyncpg). Весь SQL живёт здесь.

Пакет реэкспортирует публичные имена, поэтому `db.get_task(...)` и
`monkeypatch.setattr(db, "get_task", ...)` работают так же, как до разбиения
модуля. Пул соединений хранится в `db.core._pool`."""

from . import core, history, members, photos, schema, settings, shares, subtasks, summaries, tasks, templates
from .core import (
    _MAX_RETRIES,
    _RETRY_DELAY_SEC,
    DATABASE_URL,
    _normalize_db_url,
    _with_retry,
    close_db,
)
from .history import (
    _history_scope,
    get_completion_rate,
    purge_old_history,
    get_done_days,
    get_event_feed,
    get_history,
    get_history_stats,
    log_history,
)
from .members import (
    add_chat_member,
    get_chat_members,
    get_chats_for_user,
    get_telegram_user_photo,
    get_telegram_users,
    is_chat_member,
    upsert_telegram_user,
)
from .photos import (
    MAX_PHOTOS_PER_TASK,
    add_task_photo,
    count_task_photos,
    delete_task_photo,
    get_task_photo,
    get_task_photo_thumb,
    get_task_photos_meta,
    get_task_photos_meta_bulk,
    set_task_photo_thumb,
)
from .schema import (
    init_db,
)
from .settings import (
    _owner_table,
    get_all_known_owners,
    get_or_create_user_dashboard_token,
    get_owner_by_dashboard_token,
    get_owner_settings,
    invalidate_dashboard_cache,
    set_owner_dashboard_credentials,
    set_owner_language,
    set_owner_quiet_hours,
    set_owner_timezone,
)
from .shares import (
    get_or_create_task_share,
    get_task_by_share_code,
)
from .subtasks import (
    add_subtask,
    delete_subtask,
    get_subtask,
    get_subtasks,
    get_subtasks_bulk,
    toggle_subtask,
)
from .summaries import (
    get_all_summary_recipients,
    get_daily_summary_counts,
    get_morning_summary_recipients,
    get_morning_summary_settings,
    set_morning_summary,
)
from .tasks import (
    _UPDATABLE_TASK_FIELDS,
    add_task,
    delete_task,
    get_all_active_tasks,
    get_dashboard_stats,
    get_dashboard_tasks,
    purge_done_tasks,
    get_owner_tags,
    get_task,
    get_tasks,
    mark_done,
    mark_undone,
    search_tasks,
    set_last_notified,
    update_task,
)
from .templates import (
    MAX_TEMPLATES_PER_OWNER,
    add_template,
    count_templates,
    delete_template,
    get_template,
    get_templates,
)


def __getattr__(name: str):
    # `db._pool` читают тесты и внешний код; пул пересоздаётся в init_db(),
    # поэтому отдаём актуальное значение из core, а не снимок при импорте.
    if name == "_pool":
        return core._pool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
