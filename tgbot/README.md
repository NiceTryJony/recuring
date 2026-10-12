# tgbot/ — Telegram-бот

Точка входа: `python bot.py` → `tgbot.server.main()`. Хендлеры лежат в `handlers/` (см. его README).

Порядок зависимостей (сверху вниз, импортировать «вверх» нельзя — будет цикл):

`constants` → `app` → `owner` → `formatting` / `keyboards` → `scheduling` → `handlers/*` → `server`

| Файл | Зачем | Главное |
|---|---|---|
| `__init__.py` | Загружает `.env` до остальных модулей | `load_dotenv()` |
| `constants.py` | Константы и словари вариантов | `API_TOKEN`, `PAGE_SIZE`, `NAG_*`, `REPEAT_OPTIONS`, `REMIND_OPTIONS`, `TZ_CHOICES` |
| `app.py` | Общие объекты процесса | `bot`, `dp`, `scheduler`, `logger`. **Планировщик брать только как `app.scheduler`** (тесты подменяют его) |
| `states.py` | Состояния диалогов (FSM) | `AddTask`, `EditTask`, `SetTimezone`, `FindTask`, … |
| `owner.py` | Кто владелец и его настройки | `resolve_owner` (личный/группа), `_authorize_task/_subtask` (проверка прав), кэш `_settings_cache` + `invalidate_settings`, `owner_tz/lang/quiet`, `to_utc/to_local` |
| `formatting.py` | Текст задач для сообщений | `fmt_task`, `fmt_task_line`, `fmt_repeat/remind`, `_current_streak` |
| `keyboards.py` | Все inline-клавиатуры | `task_kb`, `list_nav_kb`, `subtasks_kb`, `repeat_kb`, … Префиксы `callback_data` (`done_`, `del_`, `listpage_`) должны совпадать с фильтрами в `handlers/` |
| `templates.py` | Встроенные шаблоны (данные) | `BUILTIN_TEMPLATES`, `builtin_templates_for_lang` |
| `scheduling.py` | Всё про время | `schedule_task` (джобы напоминаний/повторов/«мучить до выполнения»), `send_due/send_reminder`, тихий час, утренняя и вечерняя сводки, `restore_jobs()` при старте |
| `middleware.py` | Защита от спама | `ThrottlingMiddleware` — не чаще 1 сообщения за 0,7 с |
| `server.py` | Запуск | меню команд Telegram, health-сервер + дашборд (`set_scheduler_hooks`, `set_share_hooks`), `prepare_share_message`, graceful shutdown, `main()` |

## Правила
- Без SQL: только `db.*`.
- Тексты — через `i18n.t(lang, key)`, не строками в хендлере.
- Задача может принадлежать группе: всегда передавать `(owner_id, owner_type)`.
- Тесты патчат `tgbot.app.scheduler`, `tgbot.owner._settings_cache`, `tgbot.app.bot.send_message`.
