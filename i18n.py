"""Простой словарь переводов. Использование: t(lang, "key", **kwargs)"""

TEXTS = {
    "ru": {
        "welcome": "Привет! Я слежу за твоими задачами и сроками.\n\n"
                   "/add — добавить задачу\n"
                   "/list — список задач\n"
                   "/find — поиск задачи по названию\n"
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
        "daily_summary": "🌙 Вечерняя сводка\n\n"
                          "Активных задач: {active}\n"
                          "Выполнено сегодня: {done_today}\n"
                          "{overdue_line}"
                          "\n_Бот работает нормально._",
        "daily_summary_overdue": "⚠️ Просрочено: {overdue}\n",
        "btn_done": "Готово",
        "btn_task_done": "✅ Выполнено",
        "btn_task_undone": "↩️ Отменить выполнение",
        "btn_edit": "✏️ Изменить",
        "btn_delete": "🗑 Удалить",
        "btn_snooze": "⏰ +1ч",
        "btn_subtasks": "📋 Подпункты",
        "btn_confirm_delete": "Да, удалить",
        "btn_cancel": "Отмена",
        "field_title": "Название",
        "field_date": "Дату",
        "field_repeat": "Повтор",
        "field_tag": "Тег",
        "btn_add_subtask": "➕ Добавить подпункт",
        "btn_back": "⬅️ Назад",
        "showing_first": "Показаны первые {page_size} из {total}.",
        "subtasks_title": "📋 Подпункты:",
        "history_created": "➕",
        "ask_find_query": "Что ищем? Введи часть названия задачи:\n(/cancel — отменить)",
        "no_results": "Ничего не найдено.",
        "find_results": "🔎 Найдено задач: {total}",
        "list_controls": "📋 Твои задачи — выбери фильтр и сортировку:",
        "btn_filter_all": "Все",
        "btn_filter_overdue": "Просроченные",
        "btn_filter_week": "На этой неделе",
        "btn_sort_date": "По дате",
        "btn_sort_tag": "По тегу",
        "btn_sort_title": "По алфавиту",
    },
    "en": {
        "welcome": "Hi! I track your tasks and deadlines.\n\n"
                   "/add — add a task\n"
                   "/list — list tasks\n"
                   "/find — search tasks by title\n"
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
        "daily_summary": "🌙 Evening summary\n\n"
                          "Active tasks: {active}\n"
                          "Completed today: {done_today}\n"
                          "{overdue_line}"
                          "\n_Bot is running normally._",
        "daily_summary_overdue": "⚠️ Overdue: {overdue}\n",
        "btn_done": "Done",
        "btn_task_done": "✅ Done",
        "btn_task_undone": "↩️ Mark not done",
        "btn_edit": "✏️ Edit",
        "btn_delete": "🗑 Delete",
        "btn_snooze": "⏰ +1h",
        "btn_subtasks": "📋 Subtasks",
        "btn_confirm_delete": "Yes, delete",
        "btn_cancel": "Cancel",
        "field_title": "Title",
        "field_date": "Date",
        "field_repeat": "Repeat",
        "field_tag": "Tag",
        "btn_add_subtask": "➕ Add subtask",
        "btn_back": "⬅️ Back",
        "showing_first": "Showing first {page_size} of {total}.",
        "subtasks_title": "📋 Subtasks:",
        "history_created": "➕",
        "ask_find_query": "What are we looking for? Type part of the task title:\n(/cancel — cancel)",
        "no_results": "Nothing found.",
        "find_results": "🔎 Found tasks: {total}",
        "list_controls": "📋 Your tasks — pick a filter and sorting:",
        "btn_filter_all": "All",
        "btn_filter_overdue": "Overdue",
        "btn_filter_week": "This week",
        "btn_sort_date": "By date",
        "btn_sort_tag": "By tag",
        "btn_sort_title": "Alphabetically",
    },
    "pl": {
        "welcome": "Cześć! Pilnuję Twoich zadań i terminów.\n\n"
                   "/add — dodaj zadanie\n"
                   "/list — lista zadań\n"
                   "/find — szukaj zadania po nazwie\n"
                   "/tags — zadania wg tagu\n"
                   "/history — historia wykonanych\n"
                   "/timezone — zmień strefę czasową\n"
                   "/lang — zmień język\n"
                   "/dashboard — strona internetowa z listą zadań\n"
                   "/help — pomoc",
        "cancelled": "Anulowano.",
        "ask_title": "Nazwa zadania/wydarzenia:\n(/cancel — anuluj)",
        "ask_date": "Data i godzina w formacie DD.MM.RRRR GG:MM\nnp. 15.10.2026 18:00\n\n"
                    "Rozumiem też: „jutro 18:00”, „za 3 dni 10:00”, „za godzinę”",
        "bad_date": "Nie rozpoznałem daty. Przykłady: 15.10.2026 18:00, jutro 18:00, za 3 dni 10:00",
        "ask_repeat": "Powtarzać?",
        "ask_remind": "Przypomnieć wcześniej?",
        "ask_tag": "Tag dla zadania? (lub /skip, aby pominąć)",
        "task_added": "✅ Zadanie «{title}» dodane na {date}",
        "no_tasks": "Brak zadań. Dodaj przez /add",
        "task_done": "✅ Oznaczono jako wykonane",
        "task_undone": "↩️ Przywrócono do realizacji",
        "task_deleted": "🗑 Zadanie usunięte.",
        "confirm_delete_q": "Usunąć to zadanie?",
        "new_title_prompt": "Nowa nazwa:",
        "new_date_prompt": "Nowa data DD.MM.RRRR GG:MM:",
        "task_updated": "✅ Zaktualizowano",
        "repeat_updated": "✅ Powtarzanie zaktualizowane",
        "choose_timezone": "Wybierz strefę czasową (lub wpisz własną, np. Europe/Warsaw):",
        "timezone_set": "✅ Ustawiono strefę czasową: {tz}",
        "timezone_invalid": "Nie rozpoznałem strefy czasowej. Przykład: Europe/Warsaw, Europe/Moscow, Asia/Almaty",
        "choose_lang": "Wybierz język:",
        "lang_set": "✅ Język zmieniony",
        "no_tags": "Nie masz jeszcze żadnych tagów. Dodaj tag przy tworzeniu zadania.",
        "choose_tag": "Wybierz tag:",
        "no_history": "Historia jest pusta.",
        "history_title": "📜 Ostatnie zdarzenia:",
        "dashboard_ask_password": "Ustaw hasło do panelu internetowego (minimum 4 znaki):",
        "dashboard_password_short": "Hasło za krótkie, minimum 4 znaki.",
        "dashboard_ready": "✅ Panel gotowy:\n{url}\n\nUżyj hasła, które właśnie ustawiłeś/aś, aby się zalogować. Nie udostępniaj tego linku nikomu — daje dostęp do wszystkich Twoich zadań.",
        "subtask_added": "✅ Dodano podpunkt",
        "subtask_prompt": "Nazwa podpunktu (lub /done, aby zakończyć dodawanie):",
        "no_subtasks": "Brak podpunktów.",
        "snooze_1h": "Odłożono o 1 godzinę",
        "reminder_text": "⏰ Przypomnienie: «{title}» już wkrótce!",
        "due_text": "🔔 Termin minął: «{title}»",
        "daily_summary": "🌙 Wieczorne podsumowanie\n\n"
                          "Aktywne zadania: {active}\n"
                          "Wykonane dzisiaj: {done_today}\n"
                          "{overdue_line}"
                          "\n_Bot działa poprawnie._",
        "daily_summary_overdue": "⚠️ Zaległe: {overdue}\n",
        "btn_done": "Gotowe",
        "btn_task_done": "✅ Wykonane",
        "btn_task_undone": "↩️ Cofnij wykonanie",
        "btn_edit": "✏️ Edytuj",
        "btn_delete": "🗑 Usuń",
        "btn_snooze": "⏰ +1h",
        "btn_subtasks": "📋 Podpunkty",
        "btn_confirm_delete": "Tak, usuń",
        "btn_cancel": "Anuluj",
        "field_title": "Nazwa",
        "field_date": "Data",
        "field_repeat": "Powtarzanie",
        "field_tag": "Tag",
        "btn_add_subtask": "➕ Dodaj podpunkt",
        "btn_back": "⬅️ Wstecz",
        "showing_first": "Pokazano pierwsze {page_size} z {total}.",
        "subtasks_title": "📋 Podpunkty:",
        "history_created": "➕",
        "ask_find_query": "Czego szukamy? Wpisz część nazwy zadania:\n(/cancel — anuluj)",
        "no_results": "Nic nie znaleziono.",
        "find_results": "🔎 Znaleziono zadań: {total}",
        "list_controls": "📋 Twoje zadania — wybierz filtr i sortowanie:",
        "btn_filter_all": "Wszystkie",
        "btn_filter_overdue": "Zaległe",
        "btn_filter_week": "W tym tygodniu",
        "btn_sort_date": "Wg daty",
        "btn_sort_tag": "Wg tagu",
        "btn_sort_title": "Alfabetycznie",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "ru"
    text = TEXTS[lang].get(key) or TEXTS["ru"].get(key, key)
    return text.format(**kwargs) if kwargs else text