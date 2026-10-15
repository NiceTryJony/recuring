# dashboard/ — веб-страница и Mini App

Живёт в том же процессе, что и бот. Вход: `/dashboard/<токен>` + пароль, либо из Telegram Mini App без пароля (`/dashboard/app`).
`__init__.py` реэкспортирует имена для `tgbot/` и тестов — не удалять.

| Файл | Зачем | Главное |
|---|---|---|
| `routes.py` | Таблица маршрутов | **порядок важен** (aiohttp берёт первый подошедший): статика → `/dashboard/app` → `{token}` → специфичные → общий `{action}` |
| `views.py` | GET-страницы | `handle_dashboard` (список), `handle_chart` (график отдельно), `handle_history`, `handle_templates_page`, `handle_share_preview`; `_page_state` — единый загрузчик данных страницы; вкладка по умолчанию — «Nadchodzące» (`upcoming`, «Все» — только явно `?view=all`); группировка по умолчанию — по дате (`?group=date`), «без группировки» (`?group=none`) выполненные не показывает |
| `actions.py` | POST-действия | задачи, подзадачи, шаблоны, вход (Telegram Login / Mini App), «Поделиться». У каждого: сессия + CSRF + проверка владения |
| `fragments.py` | JSON-ответы для действий без перезагрузки | `wants_json` (`Accept: application/json`), `state_payload` (карточка + сводка + вкладки), `subtasks_payload`, `description_payload` |
| `pages.py` | HTML-каркасы страниц | `TASKS_PAGE`, `LOGIN_PAGE`, `SHARE_PAGE`, `WEBAPP_ENTRY_PAGE`, спрайт иконок, детектор мощности устройства (`.mid` / `.fx` / `.min`; ручной выбор из шторки «Производительность» в `localStorage 'perf'` главнее автоматики), разметка этой шторки |
| `render.py` | HTML кусков | `_render_task` (карточка), подзадачи, описание, фото, график, панель дней |
| `texts.py` | Переводы (ru/en/pl) | `DASHBOARD_TEXTS`, `_dt(lang)`; `*_fl` — подписи полей по буквам |
| `security.py` | Защита | пароли (PBKDF2), подписанные cookie-сессии, CSRF, проверка Telegram Login и `initData`, ограничение попыток входа |
| `photos.py` | Фото и аватарки | сжатие, превью, отдача с ETag |
| `assets.py` | Статика | отдаёт `static/` с хешем в имени и вечным кэшем, минификация, gzip |
| `scheduler_hooks.py` | Мост к планировщику бота | `set_scheduler_hooks` — дашборд **не импортирует** бота |
| `share_hooks.py` | Мост к Bot API | `set_share_hooks` → `savePreparedInlineMessage` для кнопки «Поделиться» |
| `timing.py` | Замер | лог запросов >300 мс; `Server-Timing` при `DASHBOARD_SERVER_TIMING=1` |
| `config.py` | Окружение и константы | `BOT_TOKEN`, `BOT_USERNAME`, `SESSION_*`, `SHARE_CODE_RE`, `_tz_of` |
| `static/` | CSS и JS | см. `static/README.md` |

## Как устроены действия
Форма с `data-ajax` работает и без JS (POST → 302). С JS тот же POST идёт `fetch`-ем с `Accept: application/json`,
сервер отвечает готовыми HTML-фрагментами, `static/dashboard.js` подменяет их на месте. Новое действие = обе ветки.

## Правила
- Без SQL здесь: только `db.*`.
- Доступ проверять на сервере (`_require_session_full` + `_verify_csrf` + владелец задачи), токен в URL — не авторизация.
- Не импортировать `bot`/`tgbot`; связь — через `*_hooks.py`.
- Тексты — в `texts.py` на всех трёх языках.
