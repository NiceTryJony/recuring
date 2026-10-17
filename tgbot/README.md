# tgbot/ — Telegram-бот

Точка входа: `python bot.py` → `tgbot.server.main()`. Хендлеры лежат в `handlers/` (см. его README).

Порядок зависимостей (сверху вниз, импортировать «вверх» нельзя — будет цикл):

`constants` → `app` → `owner` → `formatting` / `keyboards` → `scheduling` → `settings_apply` → `handlers/*` → `server`

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
| `scheduling.py` | Всё про время | `schedule_task` (джобы напоминаний/повторов/«мучить до выполнения»; правила повторов — в корневом `repeats.py`), `advance_repeating` («Готово»/«Пропустить» у повторяющейся — `due_at` на следующий раз, задача остаётся активной; cron по уже закрытому разу молчит), `send_due/send_reminder`, тихий час, утренняя (с кнопкой «просроченные → сегодня») и вечерняя сводки (по воскресеньям — итог недели), чистые `snooze_target` / `overdue_before_today` / `move_to_today`, `purge_done_tasks` (ежедневно 04:00 UTC и при старте — чистка выполненных старше 7 дней), `restore_jobs()` при старте (не досылает уведомление, если по этому сроку оно уже ушло: `last_notified_at >= due_at`) |
| `settings_apply.py` | Применение настроек | `apply_timezone/language/quiet/morning` — запись + сброс кэша + перепланирование джоб; общие для команд `/timezone /lang /quiet /morning` и страницы настроек дашборда (через `dashboard.set_settings_hooks`) |
| `photo_store.py` | Фото из Telegram → задача | `attach_photo` — скачать, сжать (как дашборд), сохранить; общая для кнопки «📷 Фото» и задачи из фото |
| `alerts.py` | Алерты админу | `install(bot, ADMIN_CHAT_ID)` — каждая запись лога уровня ERROR уходит в чат админа (дедуп 30 мин, не больше 5 за 10 мин); без `ADMIN_CHAT_ID` — no-op |
| `middleware.py` | Защита от спама, имена | `ThrottlingMiddleware` — не чаще 1 сообщения за 0,7 с; `MemberNamesMiddleware` — запоминает имена участников групп (`telegram_users`) для списка «Поручить» |
| `server.py` | Запуск | меню команд Telegram, health-сервер + дашборд (`set_scheduler_hooks`, `set_share_hooks`), `prepare_share_message`, graceful shutdown, `main()`; `/health` (для Render), `/health/deep` (БД + планировщик, 503 при сбое — для внешнего мониторинга), `keep_alive` (раз в 10 мин пингует свой публичный адрес на Render, чтобы free-инстанс не засыпал) |

## Правила
- Без SQL: только `db.*`.
- Тексты — через `i18n.t(lang, key)`, не строками в хендлере.
- Задача может принадлежать группе: всегда передавать `(owner_id, owner_type)`.
- Тесты патчат `tgbot.app.scheduler`, `tgbot.owner._settings_cache`, `tgbot.app.bot.send_message`.
