"""Переводы дашборда. Язык страницы берётся из настроек владельца
(user_settings.language для личного дашборда или chat_settings.language для
группового — то, что задано в боте через /lang). Переводы не завязаны на
i18n.py бота, чтобы не тащить aiogram-зависимости в веб-слой — здесь свой
маленький словарь DASHBOARD_TEXTS."""

DASHBOARD_TEXTS = {
    "ru": {
        "html_lang": "ru",
        "login_title": "Вход — Task Reminder",
        "login_heading": "Вход",
        "password_placeholder": "Пароль",
        "btn_login": "Войти",
        "error_wrong_password": "Неверный пароль",
        "error_too_many_attempts": "Слишком много попыток. Подождите немного и попробуйте снова.",
        "page_title": "Мои задачи",
        "heading": "Мои задачи",
        "heading_chat": "Общие задачи чата",
        "empty": "Задач пока нет.",
        "not_found": "Дашборд не найден. Проверь ссылку.",
        "chart_title": "Выполнено за {days} дн.",
        "streak": "Серия: {n} дн.",
        "repeat": {
            "none": "",
            "daily": "Каждый день",
            "weekly": "Каждую неделю",
            "monthly": "Каждый месяц",
            "yearly": "Каждый год",
            "weekdays": "По будням",
            "monthly_nth_weekday": "N-й день недели месяца",
        },
        "btn_postpone": "Отложить на день",
        "btn_delete": "Удалить",
        "confirm_delete": "Удалить задачу?",
        "new_title_placeholder": "Новая задача…",
        "btn_add": "Добавить",
        "error_empty_title": "Введите название задачи",
        "error_bad_due_at": "Не удалось разобрать дату и время",
        "tg_widget_note": "Войдите через Telegram, чтобы действия записывались под вашим именем",
        "nav_history": "История",
        "nav_back": "К задачам",
        "history_page_title": "История — Task Reminder",
        "history_heading": "История событий",
        "history_empty": "Событий пока нет.",
        "event_created": "создал(а) задачу",
        "event_done": "выполнил(а)",
        "event_undone": "отменил(а) выполнение",
        "event_deleted": "удалил(а)",
        "event_rescheduled": "перенёс(ла)",
        "btn_load_more": "Показать ещё",
        "btn_more_done": "Показать ещё выполненные ({n})",
        "nojs_full_version": "Полная версия страницы",
        "btn_add_photo": "Добавить фото",
        "confirm_delete_photo": "Удалить фото?",
        "event_photo_added": "добавил(а) фото к",
        "event_photo_removed": "удалил(а) фото у",
        "error_bad_photo": "Не удалось обработать файл как изображение",
        "nav_templates": "Шаблоны",
        "templates_page_title": "Шаблоны — Task Reminder",
        "templates_heading": "Шаблоны задач",
        "templates_empty": "Шаблонов пока нет.",
        "btn_apply_template": "Создать задачу",
        "btn_delete_template": "Удалить",
        "confirm_delete_template": "Удалить шаблон?",
        "template_offset_today": "сегодня",
        "template_offset_tomorrow": "завтра",
        "template_offset_days": "через {n} дн.",
        "new_template_title_placeholder": "Название шаблона…",
        "new_template_offset_label": "Смещение в днях (0 = сегодня)",
        "btn_save_template": "Сохранить шаблон",
        "error_empty_template_title": "Введите название шаблона",
        "template_limit_reached": "Достигнут лимит шаблонов ({n}).",
        "new_description_placeholder": "Описание (необязательно)…",
        "description_placeholder": "Добавить описание…",
        "btn_save_description": "Сохранить",
        "edit_description_label": "Изменить описание",
        "add_description_label": "Добавить описание",
        "repeats_label": "Повторяется: {label}",
        "no_repeat_label": "Не повторяется",
        "new_subtask_placeholder": "Новый пункт…",
        "btn_add_subtask": "Добавить",
        "confirm_delete_subtask": "Удалить пункт?",
    },
    "en": {
        "html_lang": "en",
        "login_title": "Login — Task Reminder",
        "login_heading": "Login",
        "password_placeholder": "Password",
        "btn_login": "Log in",
        "error_wrong_password": "Wrong password",
        "error_too_many_attempts": "Too many attempts. Please wait a bit and try again.",
        "page_title": "My Tasks",
        "heading": "My Tasks",
        "heading_chat": "Shared Chat Tasks",
        "empty": "No tasks yet.",
        "not_found": "Dashboard not found. Check the link.",
        "chart_title": "Completed in the last {days} days",
        "streak": "Streak: {n} days",
        "repeat": {
            "none": "",
            "daily": "Every day",
            "weekly": "Every week",
            "monthly": "Every month",
            "yearly": "Every year",
            "weekdays": "Weekdays",
            "monthly_nth_weekday": "Nth weekday of month",
        },
        "btn_postpone": "Postpone 1 day",
        "btn_delete": "Delete",
        "confirm_delete": "Delete this task?",
        "new_title_placeholder": "New task…",
        "btn_add": "Add",
        "error_empty_title": "Enter a task title",
        "error_bad_due_at": "Couldn't parse the date and time",
        "tg_widget_note": "Sign in with Telegram so actions are recorded under your name",
        "nav_history": "History",
        "nav_back": "Back to tasks",
        "history_page_title": "History — Task Reminder",
        "history_heading": "Event history",
        "history_empty": "No events yet.",
        "event_created": "created the task",
        "event_done": "completed",
        "event_undone": "unmarked",
        "event_deleted": "deleted",
        "event_rescheduled": "rescheduled",
        "btn_load_more": "Load more",
        "btn_more_done": "Show more completed ({n})",
        "nojs_full_version": "Full version of the page",
        "btn_add_photo": "Add photo",
        "confirm_delete_photo": "Delete this photo?",
        "event_photo_added": "added a photo to",
        "event_photo_removed": "removed a photo from",
        "error_bad_photo": "Couldn't process the file as an image",
        "nav_templates": "Templates",
        "templates_page_title": "Templates — Task Reminder",
        "templates_heading": "Task templates",
        "templates_empty": "No templates yet.",
        "btn_apply_template": "Create task",
        "btn_delete_template": "Delete",
        "confirm_delete_template": "Delete this template?",
        "template_offset_today": "today",
        "template_offset_tomorrow": "tomorrow",
        "template_offset_days": "in {n} days",
        "new_template_title_placeholder": "Template title…",
        "new_template_offset_label": "Days offset (0 = today)",
        "btn_save_template": "Save template",
        "error_empty_template_title": "Enter a template title",
        "template_limit_reached": "Template limit reached ({n}).",
        "new_description_placeholder": "Description (optional)…",
        "description_placeholder": "Add a description…",
        "btn_save_description": "Save",
        "edit_description_label": "Edit description",
        "add_description_label": "Add description",
        "repeats_label": "Repeats: {label}",
        "no_repeat_label": "Doesn't repeat",
        "new_subtask_placeholder": "New item…",
        "btn_add_subtask": "Add",
        "confirm_delete_subtask": "Delete this item?",
    },
    "pl": {
        "html_lang": "pl",
        "login_title": "Logowanie — Task Reminder",
        "login_heading": "Logowanie",
        "password_placeholder": "Hasło",
        "btn_login": "Zaloguj się",
        "error_wrong_password": "Nieprawidłowe hasło",
        "error_too_many_attempts": "Zbyt wiele prób. Odczekaj chwilę i spróbuj ponownie.",
        "page_title": "Moje zadania",
        "heading": "Moje zadania",
        "heading_chat": "Wspólne zadania czatu",
        "empty": "Brak zadań.",
        "not_found": "Nie znaleziono panelu. Sprawdź link.",
        "chart_title": "Wykonane w ostatnich {days} dniach",
        "streak": "Seria: {n} dni",
        "repeat": {
            "none": "",
            "daily": "Codziennie",
            "weekly": "Co tydzień",
            "monthly": "Co miesiąc",
            "yearly": "Co rok",
            "weekdays": "W dni robocze",
            "monthly_nth_weekday": "N-ty dzień tygodnia miesiąca",
        },
        "btn_postpone": "Przełóż o dzień",
        "btn_delete": "Usuń",
        "confirm_delete": "Usunąć zadanie?",
        "new_title_placeholder": "Nowe zadanie…",
        "btn_add": "Dodaj",
        "error_empty_title": "Wpisz nazwę zadania",
        "error_bad_due_at": "Nie udało się odczytać daty i godziny",
        "tg_widget_note": "Zaloguj się przez Telegram, aby działania zapisywały się pod twoim imieniem",
        "nav_history": "Historia",
        "nav_back": "Do zadań",
        "history_page_title": "Historia — Task Reminder",
        "history_heading": "Historia zdarzeń",
        "history_empty": "Brak zdarzeń.",
        "event_created": "utworzył(a) zadanie",
        "event_done": "wykonał(a)",
        "event_undone": "cofnął(ęła) wykonanie",
        "event_deleted": "usunął(ęła)",
        "event_rescheduled": "przełożył(a)",
        "btn_load_more": "Pokaż więcej",
        "btn_more_done": "Pokaż więcej wykonanych ({n})",
        "nojs_full_version": "Pełna wersja strony",
        "btn_add_photo": "Dodaj zdjęcie",
        "confirm_delete_photo": "Usunąć zdjęcie?",
        "event_photo_added": "dodał(a) zdjęcie do",
        "event_photo_removed": "usunął(ęła) zdjęcie z",
        "error_bad_photo": "Nie udało się przetworzyć pliku jako obrazu",
        "nav_templates": "Szablony",
        "templates_page_title": "Szablony — Task Reminder",
        "templates_heading": "Szablony zadań",
        "templates_empty": "Brak szablonów.",
        "btn_apply_template": "Utwórz zadanie",
        "btn_delete_template": "Usuń",
        "confirm_delete_template": "Usunąć szablon?",
        "template_offset_today": "dzisiaj",
        "template_offset_tomorrow": "jutro",
        "template_offset_days": "za {n} dni",
        "new_template_title_placeholder": "Nazwa szablonu…",
        "new_template_offset_label": "Przesunięcie w dniach (0 = dzisiaj)",
        "btn_save_template": "Zapisz szablon",
        "error_empty_template_title": "Wpisz nazwę szablonu",
        "template_limit_reached": "Osiągnięto limit szablonów ({n}).",
        "new_description_placeholder": "Opis (opcjonalnie)…",
        "description_placeholder": "Dodaj opis…",
        "btn_save_description": "Zapisz",
        "edit_description_label": "Edytuj opis",
        "add_description_label": "Dodaj opis",
        "repeats_label": "Powtarza się: {label}",
        "no_repeat_label": "Nie powtarza się",
        "new_subtask_placeholder": "Nowy punkt…",
        "btn_add_subtask": "Dodaj",
        "confirm_delete_subtask": "Usunąć punkt?",
    },
}


def _dt(lang: str) -> dict:
    return DASHBOARD_TEXTS.get(lang, DASHBOARD_TEXTS["ru"])


# Наборы текстов, пригодные для прямой передачи в str.format(**...): без
# вложенного словаря "repeat" (он нужен только рендеру задач) и, во втором
# варианте, без заголовков, которые handle_dashboard передаёт отдельным
# аргументом. Раньше эти словари пересобирались на каждый рендер страницы —
# теперь считаются один раз при импорте модуля.
_TEXTS_FLAT = {
    lang: {k: v for k, v in texts.items() if k != "repeat"}
    for lang, texts in DASHBOARD_TEXTS.items()
}
_TEXTS_TASKS_PAGE = {
    lang: {k: v for k, v in flat.items() if k not in ("heading", "heading_chat")}
    for lang, flat in _TEXTS_FLAT.items()
}


def _dt_flat(lang: str) -> dict:
    return _TEXTS_FLAT.get(lang, _TEXTS_FLAT["ru"])


def _dt_tasks_page(lang: str) -> dict:
    return _TEXTS_TASKS_PAGE.get(lang, _TEXTS_TASKS_PAGE["ru"])


_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
_PL_MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
              "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]
_MONTH_NAMES = {"ru": _RU_MONTHS, "en": _EN_MONTHS, "pl": _PL_MONTHS}

_RU_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_PL_DOW = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]
_DOW_NAMES = {"ru": _RU_DOW, "en": _EN_DOW, "pl": _PL_DOW}


def _months(lang: str) -> list[str]:
    """Фолбэк на ru, как в _dt(): раньше незнакомый язык в БД давал KeyError
    и 500 на главной странице вместо просто русских названий месяцев."""
    return _MONTH_NAMES.get(lang, _RU_MONTHS)


def _dows(lang: str) -> list[str]:
    return _DOW_NAMES.get(lang, _RU_DOW)
