"""Простой словарь переводов. Использование: t(lang, "key", **kwargs)"""

TEXTS = {
    "ru": {
        "welcome": "Привет! Я слежу за твоими задачами и сроками.\n\n"
                   "/add — добавить задачу\n"
                   "/list — список задач\n"
                   "/tags — задачи по тегу\n"
                   "/history — история выполненных\n"
                   "/timezone — сменить часовой пояс\n"
                   "/lang — сменить язык\n"
                   "/dashboard — веб-страница со списком задач\n"
                   "/help — помощь",
        "cancelled": "Отменено.",
        "ask_title": "Название задачи/события:\n(/cancel — отменить)",
        "ask_date": "Дата и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nнапример: 15.10.2026 18:00\n\n"
                    "Также понимаю: «завтра 18:00», «через 3 дня 10:00», «через час»",
        "bad_date": "Не понял дату. Примеры: 15.10.2026 18:00, завтра 18:00, через 3 дня 10:00",
        "ask_repeat": "Повторять?",
        "ask_remind": "Напомнить заранее?",
        "ask_tag": "Тег для задачи? (или /skip чтобы пропустить)",
        "task_added": "✅ Задача «{title}» добавлена на {date}",
        "no_tasks": "Задач нет. Добавь через /add",
        "task_done": "✅ Отмечено выполненным",
        "task_undone": "↩️ Возвращено в работу",
        "task_deleted": "🗑 Задача удалена.",
        "confirm_delete_q": "Удалить эту задачу?",
        "new_title_prompt": "Новое название:",
        "new_date_prompt": "Новая дата ДД.ММ.ГГГГ ЧЧ:ММ:",
        "task_updated": "✅ Обновлено",
        "repeat_updated": "✅ Повтор обновлён",
        "choose_timezone": "Выбери часовой пояс (или напиши свой в формате Europe/Warsaw):",
        "timezone_set": "✅ Часовой пояс установлен: {tz}",
        "timezone_invalid": "Не распознал часовой пояс. Пример: Europe/Warsaw, Europe/Moscow, Asia/Almaty",
        "choose_lang": "Выбери язык:",
        "lang_set": "✅ Язык переключён",
        "no_tags": "У тебя пока нет тегов. Добавь тег при создании задачи.",
        "choose_tag": "Выбери тег:",
        "no_history": "История пуста.",
        "history_title": "📜 Последние события:",
        "dashboard_ask_password": "Придумай пароль для веб-дашборда (минимум 4 символа):",
        "dashboard_password_short": "Пароль слишком короткий, минимум 4 символа.",
        "dashboard_ready": "✅ Дашборд готов:\n{url}\n\nПароль для входа тот, что ты только что задал. Никому не передавай ссылку — она даёт доступ ко всем твоим задачам.",
        "subtask_added": "✅ Подпункт добавлен",
        "subtask_prompt": "Название подпункта (или /done чтобы закончить добавление):",
        "no_subtasks": "Подпунктов нет.",
        "snooze_1h": "Отложено на 1 час",
        "reminder_text": "⏰ Напоминание: «{title}» скоро наступит!",
        "due_text": "🔔 Срок наступил: «{title}»",
    },
    "en": {
        "welcome": "Hi! I track your tasks and deadlines.\n\n"
                   "/add — add a task\n"
                   "/list — list tasks\n"
                   "/tags — tasks by tag\n"
                   "/history — completed history\n"
                   "/timezone — change timezone\n"
                   "/lang — change language\n"
                   "/dashboard — web page with your tasks\n"
                   "/help — help",
        "cancelled": "Cancelled.",
        "ask_title": "Task/event title:\n(/cancel — cancel)",
        "ask_date": "Date and time as DD.MM.YYYY HH:MM\ne.g. 15.10.2026 18:00\n\n"
                    "Also understands: 'tomorrow 18:00', 'in 3 days 10:00', 'in an hour'",
        "bad_date": "Couldn't parse the date. Examples: 15.10.2026 18:00, tomorrow 18:00, in 3 days 10:00",
        "ask_repeat": "Repeat?",
        "ask_remind": "Remind in advance?",
        "ask_tag": "Tag for this task? (or /skip to skip)",
        "task_added": "✅ Task «{title}» added for {date}",
        "no_tasks": "No tasks yet. Add one with /add",
        "task_done": "✅ Marked done",
        "task_undone": "↩️ Marked not done",
        "task_deleted": "🗑 Task deleted.",
        "confirm_delete_q": "Delete this task?",
        "new_title_prompt": "New title:",
        "new_date_prompt": "New date DD.MM.YYYY HH:MM:",
        "task_updated": "✅ Updated",
        "repeat_updated": "✅ Repeat updated",
        "choose_timezone": "Choose a timezone (or type your own, e.g. Europe/Warsaw):",
        "timezone_set": "✅ Timezone set: {tz}",
        "timezone_invalid": "Didn't recognize that timezone. Example: Europe/Warsaw, America/New_York, Asia/Tokyo",
        "choose_lang": "Choose language:",
        "lang_set": "✅ Language switched",
        "no_tags": "No tags yet. Add a tag when creating a task.",
        "choose_tag": "Choose a tag:",
        "no_history": "History is empty.",
        "history_title": "📜 Recent events:",
        "dashboard_ask_password": "Set a password for the web dashboard (min 4 characters):",
        "dashboard_password_short": "Password too short, minimum 4 characters.",
        "dashboard_ready": "✅ Dashboard ready:\n{url}\n\nUse the password you just set to log in. Don't share this link — it gives access to all your tasks.",
        "subtask_added": "✅ Subtask added",
        "subtask_prompt": "Subtask title (or /done to finish adding):",
        "no_subtasks": "No subtasks.",
        "snooze_1h": "Snoozed for 1 hour",
        "reminder_text": "⏰ Reminder: «{title}» is coming up soon!",
        "due_text": "🔔 Due now: «{title}»",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "ru"
    text = TEXTS[lang].get(key) or TEXTS["ru"].get(key, key)
    return text.format(**kwargs) if kwargs else text
