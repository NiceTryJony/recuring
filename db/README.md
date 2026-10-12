# db/ — слой БД

Весь SQL проекта (PostgreSQL, `asyncpg`). Снаружи — как один модуль: `import db; db.get_task(...)`.
`__init__.py` реэкспортирует все публичные имена, поэтому `monkeypatch.setattr(db, "get_task", ...)` работает.

| Файл | Зачем | Главное |
|---|---|---|
| `core.py` | Подключение и устойчивость | `DATABASE_URL`, пул `_pool` (читается и как `db._pool`), `_with_retry` (3 попытки при сбое сети), `db_timing` (счётчик мс/запросов для `Server-Timing`), `close_db` |
| `schema.py` | Создание таблиц и миграции | `init_db()` — идемпотентна, вызывается при каждом старте; новые колонки/индексы добавлять сюда через `IF NOT EXISTS` |
| `tasks.py` | Задачи | CRUD, `get_tasks` (для бота), `get_dashboard_tasks` + `get_dashboard_stats` (лёгкие выборки для сайта), поиск, `update_task` (белый список полей) |
| `subtasks.py` | Подзадачи | add / get (+ `_bulk` одним запросом) / toggle / delete |
| `photos.py` | Фото задач (BYTEA) | лимит `MAX_PHOTOS_PER_TASK`, превью `thumb` дорисовывается лениво |
| `shares.py` | «Поделиться задачей» | `get_or_create_task_share` (один код `s_…` на задачу), `get_task_by_share_code` |
| `history.py` | История и статистика | `log_history`, статистика по дням, стрик (`get_done_days`), лента событий группы |
| `members.py` | Люди | участники групповых чатов; имена и аватары Telegram-пользователей |
| `settings.py` | Настройки владельца | пояс, язык, тихий час, токен/пароль дашборда; **кэш на 30 с** `get_owner_by_dashboard_token` — любая запись в этом файле его сбрасывает (`invalidate_dashboard_cache`) |
| `templates.py` | Шаблоны задач | лимит `MAX_TEMPLATES_PER_OWNER` |
| `summaries.py` | Сводки | настройки и получатели утренней/вечерней сводки, счётчики для неё |

## Правила
- Владелец везде — пара `(owner_id, owner_type)`: `'user'` (личный) или `'chat'` (группа).
- SQL только здесь; только параметры `$1…`, никаких f-строк с пользовательским вводом.
- Модуль обращается к пулу через `core._pool`, к ретраю — через `core._with_retry`.
- Между модулями нет зависимостей, кроме `core` (так нет циклов).
- Новая функция → добавить имя в импорт `db/__init__.py`.
- Проверка SQL: `TEST_DATABASE_URL=… pytest -m integration` (имя базы должно содержать `test`).
