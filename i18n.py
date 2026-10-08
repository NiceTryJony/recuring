"""Простой словарь переводов. Использование: t(lang, "key", **kwargs)"""

TEXTS = {
    "ru": {
        "welcome": "Привет! Я слежу за твоими задачами и сроками.\n\n"
                   "/add — добавить задачу\n"
                   "/list — список задач\n"
                   "/quiet — тихий час (без уведомлений)\n"
                   "/help — все команды",
        "welcome_group": "Привет! Я слежу за задачами этого чата — они общие для всех участников.\n\n"
                         "/add — добавить задачу\n"
                         "/list — список задач\n"
                         "/help — все команды",
        "help_full": "📖 Все команды:\n\n"
                     "/add — добавить новую задачу (название, дата, повтор, напоминание)\n"
                     "/list — список твоих задач с фильтром и сортировкой\n"
                     "/find — найти задачу по части названия\n"
                     "/tags — показать задачи, сгруппированные по тегу\n"
                     "/history — история выполненных/удалённых задач и статистика\n"
                     "/export — выгрузить все задачи в CSV-файл\n"
                     "/timezone — сменить часовой пояс\n"
                     "/quiet — тихий час: в это время уведомления не приходят\n"
                     "/lang — сменить язык бота\n"
                     "/dashboard — получить ссылку на веб-страницу со списком задач\n"
                     "/cancel — отменить текущее действие (например, добавление задачи)\n"
                     "/help — показать это сообщение",
        "cancelled": "Отменено.",
        "not_authorized": "⛔ Это не ваша задача.",
        "unrecognized": "Не понял команду 🤔\nПосмотри /help — там список всех команд.",
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
        "btn_photo": "📷 Фото",
        "photo_prompt": "Пришли одно или несколько фото. Когда закончишь — /done",
        "photo_saved": "✅ Фото сохранено",
        "photo_limit_reached": "Достигнут лимит фото на задачу ({n})",
        "photo_bad_file": "Не удалось обработать файл как изображение, попробуй другое фото",
        "photo_wrong_type": "Пришли именно фото (не файл и не текст), или /done чтобы закончить",
        "snooze_1h": "Отложено на 1 час",
        "reminder_text": "⏰ Напоминание: «{title}» скоро наступит!",
        "due_text": "🔔 Срок наступил: «{title}»",
        "daily_summary": "🌙 Вечерняя сводка\n\n"
                          "Активных задач: {active}\n"
                          "Выполнено сегодня: {done_today}\n"
                          "{overdue_line}{stuck_line}{streak_line}"
                          "\n<i>Бот работает нормально.</i>",
        "daily_summary_overdue": "⚠️ Просрочено: {overdue}\n",
        "daily_summary_stuck": "🔧 Похоже, сбилось напоминание у повторяющихся задач: {stuck}. Загляните в /list.\n",
        # Объединённая сводка (личка + все группы одним сообщением): заголовок
        # общий, дальше для каждого контекста идёт секция section_personal/
        # section_chat — порядок: сначала личные задачи, потом группы.
        "daily_summary_combined_header": "🌙 Вечерняя сводка\n",
        "daily_summary_section_personal": "\n<b>Личные задачи</b>\n"
                          "Активных: {active} · выполнено сегодня: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_section_chat": "\n<b>{chat_title}</b>\n"
                          "Активных: {active} · выполнено сегодня: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_combined_footer": "{streak_line}\n<i>Бот работает нормально.</i>",
        "daily_summary_chat_fallback": "Группа",
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
        "ask_nag": "🔁 Повторять напоминание, пока не выполню?\n(каждые {interval} мин, максимум {hours} ч после срока)",
        "btn_nag_yes": "🔁 Да, повторять",
        "btn_nag_no": "Нет, один раз",
        "nag_line": "🔁 Напоминать, пока не выполнено",
        "nag_text": "🔔 Всё ещё не выполнено: «{title}»",
        "quiet_current": "🌙 Тихий час: {start}–{end}\nВ это время уведомления откладываются до его окончания.",
        "quiet_none": "🌙 Тихий час выключен.",
        "quiet_prompt": "Выбери вариант или введи свой в формате ЧЧ:ММ-ЧЧ:ММ, например 23:00-08:00 (/cancel — отменить):",
        "quiet_set": "✅ Тихий час: {start}–{end}",
        "quiet_off_done": "✅ Тихий час выключен",
        "quiet_invalid": "Не понял. Формат: 23:00-08:00 (начало и конец должны отличаться). Или напиши «выкл».",
        "btn_quiet_off": "Выключить",
        "btn_hist_recent": "📜 Последние события",
        "btn_hist_week": "📊 Статистика за неделю",
        "btn_hist_month": "📊 Статистика за месяц",
        "history_mode_unknown": "Эта кнопка устарела, обновите список командой /history",
        "stats_title": "📊 Статистика за {days} дн.",
        "stats_totals": "➕ Создано: {created}\n✅ Выполнено: {done}\n🗑 Удалено: {deleted}",
        "streak_line": "🔥 Серия: {n} дн.\n",
        "weekdays_short": "Пн,Вт,Ср,Чт,Пт,Сб,Вс",
        "throttled": "Полегче 🙂 Подожди секунду между командами.",
    },
    "en": {
        "welcome": "Hi! I track your tasks and deadlines.\n\n"
                   "/add — add a task\n"
                   "/list — list tasks\n"
                   "/quiet — quiet hours (no notifications)\n"
                   "/help — all commands",
        "welcome_group": "Hi! I track this chat's tasks — shared by everyone here.\n\n"
                         "/add — add a task\n"
                         "/list — list tasks\n"
                         "/help — all commands",
        "help_full": "📖 All commands:\n\n"
                     "/add — add a new task (title, date, repeat, reminder)\n"
                     "/list — list your tasks with filter and sorting\n"
                     "/find — search for a task by part of its title\n"
                     "/tags — show tasks grouped by tag\n"
                     "/history — completed/deleted task history and stats\n"
                     "/export — export all tasks as a CSV file\n"
                     "/timezone — change your timezone\n"
                     "/quiet — quiet hours: no notifications during this time\n"
                     "/lang — change the bot's language\n"
                     "/dashboard — get a link to the web page with your tasks\n"
                     "/cancel — cancel the current action (e.g. adding a task)\n"
                     "/help — show this message",
        "cancelled": "Cancelled.",
        "not_authorized": "⛔ This isn't your task.",
        "unrecognized": "I didn't understand that 🤔\nCheck /help for the full list of commands.",
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
        "btn_photo": "📷 Photo",
        "photo_prompt": "Send one or more photos. Type /done when finished",
        "photo_saved": "✅ Photo saved",
        "photo_limit_reached": "Photo limit reached for this task ({n})",
        "photo_bad_file": "Couldn't process the file as an image, try another photo",
        "photo_wrong_type": "Please send a photo (not a file or text), or /done to finish",
        "snooze_1h": "Snoozed for 1 hour",
        "reminder_text": "⏰ Reminder: «{title}» is coming up soon!",
        "due_text": "🔔 Due now: «{title}»",
        "daily_summary": "🌙 Evening summary\n\n"
                          "Active tasks: {active}\n"
                          "Completed today: {done_today}\n"
                          "{overdue_line}{stuck_line}{streak_line}"
                          "\n<i>Bot is running normally.</i>",
        "daily_summary_overdue": "⚠️ Overdue: {overdue}\n",
        "daily_summary_stuck": "🔧 Reminders for {stuck} repeating task(s) look stuck. Check /list.\n",
        "daily_summary_combined_header": "🌙 Evening summary\n",
        "daily_summary_section_personal": "\n<b>Personal tasks</b>\n"
                          "Active: {active} · done today: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_section_chat": "\n<b>{chat_title}</b>\n"
                          "Active: {active} · done today: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_combined_footer": "{streak_line}\n<i>Bot is running fine.</i>",
        "daily_summary_chat_fallback": "Group",
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
        "ask_nag": "🔁 Keep reminding me until it's done?\n(every {interval} min, up to {hours} h after the due time)",
        "btn_nag_yes": "🔁 Yes, keep reminding",
        "btn_nag_no": "No, just once",
        "nag_line": "🔁 Reminds until done",
        "nag_text": "🔔 Still not done: «{title}»",
        "quiet_current": "🌙 Quiet hours: {start}–{end}\nNotifications are postponed until they end.",
        "quiet_none": "🌙 Quiet hours are off.",
        "quiet_prompt": "Pick an option or type your own as HH:MM-HH:MM, e.g. 23:00-08:00 (/cancel — cancel):",
        "quiet_set": "✅ Quiet hours: {start}–{end}",
        "quiet_off_done": "✅ Quiet hours turned off",
        "quiet_invalid": "Didn't get that. Format: 23:00-08:00 (start and end must differ). Or type “off”.",
        "btn_quiet_off": "Turn off",
        "btn_hist_recent": "📜 Recent events",
        "btn_hist_week": "📊 Last 7 days",
        "btn_hist_month": "📊 Last 30 days",
        "history_mode_unknown": "This button is outdated, refresh with /history",
        "stats_title": "📊 Stats for {days} days",
        "stats_totals": "➕ Created: {created}\n✅ Done: {done}\n🗑 Deleted: {deleted}",
        "streak_line": "🔥 Streak: {n} days\n",
        "weekdays_short": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "throttled": "Slow down 🙂 Wait a second between commands.",
    },
    "pl": {
        "welcome": "Cześć! Pilnuję Twoich zadań i terminów.\n\n"
                   "/add — dodaj zadanie\n"
                   "/list — lista zadań\n"
                   "/quiet — cisza nocna (bez powiadomień)\n"
                   "/help — wszystkie komendy",
        "welcome_group": "Cześć! Pilnuję zadań tego czatu — są wspólne dla wszystkich uczestników.\n\n"
                         "/add — dodaj zadanie\n"
                         "/list — lista zadań\n"
                         "/help — wszystkie komendy",
        "help_full": "📖 Wszystkie komendy:\n\n"
                     "/add — dodaj nowe zadanie (nazwa, data, powtarzanie, przypomnienie)\n"
                     "/list — lista Twoich zadań z filtrem i sortowaniem\n"
                     "/find — znajdź zadanie po części nazwy\n"
                     "/tags — pokaż zadania pogrupowane wg tagu\n"
                     "/history — historia wykonanych/usuniętych zadań i statystyki\n"
                     "/export — wyeksportuj wszystkie zadania do pliku CSV\n"
                     "/timezone — zmień strefę czasową\n"
                     "/quiet — cisza nocna: w tym czasie nie przychodzą powiadomienia\n"
                     "/lang — zmień język bota\n"
                     "/dashboard — pobierz link do strony z listą Twoich zadań\n"
                     "/cancel — anuluj bieżącą czynność (np. dodawanie zadania)\n"
                     "/help — pokaż tę wiadomość",
        "cancelled": "Anulowano.",
        "not_authorized": "⛔ To nie jest Twoje zadanie.",
        "unrecognized": "Nie zrozumiałem 🤔\nSprawdź /help — tam lista wszystkich komend.",
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
        "btn_photo": "📷 Zdjęcie",
        "photo_prompt": "Wyślij jedno lub kilka zdjęć. Gdy skończysz — /done",
        "photo_saved": "✅ Zdjęcie zapisane",
        "photo_limit_reached": "Osiągnięto limit zdjęć dla zadania ({n})",
        "photo_bad_file": "Nie udało się przetworzyć pliku jako obrazu, spróbuj inne zdjęcie",
        "photo_wrong_type": "Wyślij zdjęcie (nie plik ani tekst), albo /done aby zakończyć",
        "snooze_1h": "Odłożono o 1 godzinę",
        "reminder_text": "⏰ Przypomnienie: «{title}» już wkrótce!",
        "due_text": "🔔 Termin minął: «{title}»",
        "daily_summary": "🌙 Wieczorne podsumowanie\n\n"
                          "Aktywne zadania: {active}\n"
                          "Wykonane dzisiaj: {done_today}\n"
                          "{overdue_line}{stuck_line}{streak_line}"
                          "\n<i>Bot działa poprawnie.</i>",
        "daily_summary_overdue": "⚠️ Zaległe: {overdue}\n",
        "daily_summary_stuck": "🔧 Przypomnienia dla {stuck} powtarzających się zadań wyglądają na zablokowane. Sprawdź /list.\n",
        "daily_summary_combined_header": "🌙 Wieczorne podsumowanie\n",
        "daily_summary_section_personal": "\n<b>Zadania osobiste</b>\n"
                          "Aktywne: {active} · wykonane dziś: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_section_chat": "\n<b>{chat_title}</b>\n"
                          "Aktywne: {active} · wykonane dziś: {done_today}\n"
                          "{overdue_line}{stuck_line}",
        "daily_summary_combined_footer": "{streak_line}\n<i>Bot działa prawidłowo.</i>",
        "daily_summary_chat_fallback": "Grupa",
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
        "ask_nag": "🔁 Przypominać, dopóki nie wykonam?\n(co {interval} min, maksymalnie {hours} h po terminie)",
        "btn_nag_yes": "🔁 Tak, powtarzaj",
        "btn_nag_no": "Nie, jednorazowo",
        "nag_line": "🔁 Przypomina do skutku",
        "nag_text": "🔔 Wciąż niewykonane: «{title}»",
        "quiet_current": "🌙 Cisza nocna: {start}–{end}\nPowiadomienia są odkładane do jej zakończenia.",
        "quiet_none": "🌙 Cisza nocna jest wyłączona.",
        "quiet_prompt": "Wybierz opcję lub wpisz własną w formacie GG:MM-GG:MM, np. 23:00-08:00 (/cancel — anuluj):",
        "quiet_set": "✅ Cisza nocna: {start}–{end}",
        "quiet_off_done": "✅ Cisza nocna wyłączona",
        "quiet_invalid": "Nie rozumiem. Format: 23:00-08:00 (początek i koniec muszą się różnić). Lub napisz „wyłącz”.",
        "btn_quiet_off": "Wyłącz",
        "btn_hist_recent": "📜 Ostatnie zdarzenia",
        "btn_hist_week": "📊 Statystyki z tygodnia",
        "btn_hist_month": "📊 Statystyki z miesiąca",
        "history_mode_unknown": "Ten przycisk jest nieaktualny, odśwież poleceniem /history",
        "stats_title": "📊 Statystyki z {days} dni",
        "stats_totals": "➕ Utworzone: {created}\n✅ Wykonane: {done}\n🗑 Usunięte: {deleted}",
        "streak_line": "🔥 Seria: {n} dni\n",
        "weekdays_short": "Pn,Wt,Śr,Cz,Pt,Sb,Nd",
        "throttled": "Spokojnie 🙂 Poczekaj chwilę między komendami.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "ru"
    text = TEXTS[lang].get(key) or TEXTS["ru"].get(key, key)
    return text.format(**kwargs) if kwargs else text