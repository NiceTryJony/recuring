# tgbot/handlers/ — обработчики команд и кнопок

Один файл = одна фича = один `Router`. `__init__.py::register(dp)` подключает их **в порядке ниже**:
первый подошедший хендлер выигрывает, поэтому `fallback` обязан быть последним.

| # | Файл | Команды / кнопки | Заметки |
|---|---|---|---|
| 1 | `templates.py` | `/templates`, `tplapply_*`, `tpldel_*`, создание шаблона | здесь же `apply_template` (создаёт задачу из шаблона) |
| 2 | `basic.py` | `/start /help /cancel /timezone /lang /quiet /morning` | `_apply_timezone`, `_apply_quiet` — общая логика команд и кнопок |
| 3 | `add_task.py` | `/add` (шаги: название → дата → повтор → напоминание → «мучить» → тег), `tplfromtask_*` | FSM `AddTask`; `_finalize_add_task` пишет в БД и ставит джоб |
| 4 | `listing.py` | `/list`, фильтры `listf_`, сортировка `lists_`, страницы `listpage_`, открытие карточки `listopen_` | кэши вида списка на владельца; `_open_task_card` используют `find` и `tags` |
| 5 | `find.py` | `/find`, `findpage_*`, `findopen_*` | текстовый поиск |
| 6 | `tags.py` | `/tags`, `tagf_*`, `tagfpage_*`, `tagfopen_*` | фильтр по тегу |
| 7 | `history.py` | `/history`, `hist_*`, `/export` (CSV) | |
| 8 | `dashboard_cmd.py` | `/dashboard` | задаёт пароль и выдаёт ссылку |
| 9 | `task_actions.py` | `done_ undone_ del_ delok_ delno_ snooze_ edit_ ef_ editrep_` | действия над карточкой; пересчитывает джобы планировщика |
| 10 | `subtasks.py` | `subs_ subtoggle_ subdel_* subadd_ subback_` | |
| 11 | `photos.py` | `photo_*`, приём фото | FSM `AddTaskPhoto`, лимит из `db.MAX_PHOTOS_PER_TASK` |
| 12 | `fallback.py` | любое сообщение без состояния | отвечает подсказкой |

## Как добавить хендлер
1. Фильтр писать на `router` своего файла: `@router.message(Command("x"))`, `@router.callback_query(F.data.startswith("x_"))`.
2. Первым делом `resolve_owner` → `owner_lang`; доступ к чужой задаче — через `_authorize_task`.
3. Новый префикс `callback_data` не должен быть началом существующего (иначе сработает не тот хендлер).
4. Новый файл — добавить в кортеж в `__init__.py` перед `fallback`.
5. Хендлеры не импортируют друг друга, кроме `find`/`tags` → `listing` и `task_actions` → `listing`.
