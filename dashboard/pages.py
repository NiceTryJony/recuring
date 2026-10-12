"""HTML-скелеты страниц дашборда (подставляются через str.format).

Стили и скрипты — не здесь: они лежат файлами в dashboard/static/ и
подключаются ссылками с хешем в имени (см. assets.py). Поэтому фигурные скобки
в этих шаблонах — только поля .format(), никаких задвоенных {{ }} из CSS/JS."""

from aiohttp import web as aioweb

from .assets import DASHBOARD_JS_URL, stylesheets
from .texts import _dt_flat

# Шрифты: было 11 отдельных начертаний (Fraunces 6 + Inter 5) — 11 файлов и
# render-blocking CSS-запрос на сторонний домен перед первой отрисовкой. Стало
# три вариативных файла по реально используемым диапазонам (Fraunces 600-700
# прямой + 400-600 курсив, Inter 400-600), а сама таблица стилей грузится
# неблокирующе: media="print" + onload переключает её на all, когда она
# приехала. Текст виден сразу системным шрифтом и подменяется по display=swap,
# не задерживая первый кадр.
_FONT_CSS_URL = ("https://fonts.googleapis.com/css2?"
                 "family=Fraunces:ital,wght@0,600..700;1,400..600&family=Inter:wght@400..600&display=swap")

# Детектор мощного устройства — синхронно, до таблиц стилей, чтобы не было
# «мигания» дешёвого вида перед дорогим. По умолчанию страница рисуется в
# облегчённом режиме (без backdrop-filter, зерна, каскадных анимаций — см.
# base.css); класс fx на <html> включает эти эффекты, только если это похоже
# на настольный компьютер с запасом мощности: точный указатель с hover,
# широкий экран, >= 8 ГБ памяти и >= 6 потоков, и человек не просил систему
# экономить трафик или движение. Телефон уровня Snapdragon 645 (8 ядер, но
# 4 ГБ и сенсорный экран) сюда не попадает.
_FX_DETECT = (
    "<script>(function(){try{var n=navigator,m=window.matchMedia;"
    "if(m('(hover:hover) and (pointer:fine) and (min-width:900px)').matches"
    "&&!m('(prefers-reduced-motion: reduce)').matches"
    "&&!(n.connection&&n.connection.saveData)"
    "&&(n.deviceMemory||8)>=8&&(n.hardwareConcurrency||8)>=6)"
    "document.documentElement.classList.add('fx')}catch(e){}})()</script>"
)


def _head(title_field: str, css_file: str, with_js: bool = False) -> str:
    """Общий <head>. Результат ещё пройдёт через .format() вместе со всей
    страницей, поэтому скобки детектора экранируются здесь."""
    js = f'\n<script defer src="{DASHBOARD_JS_URL}"></script>' if with_js else ""
    fx_detect = _FX_DETECT.replace("{", "{{").replace("}", "}}")
    return f"""<!DOCTYPE html>
<html lang="{{html_lang}}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{{title_field}}}</title>
{fx_detect}
<meta name="color-scheme" content="dark">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{_FONT_CSS_URL}" media="print" onload="this.media='all'">
<noscript><link rel="stylesheet" href="{_FONT_CSS_URL}"></noscript>
{stylesheets(css_file)}{js}
</head>"""


LOGIN_PAGE = _head("login_title", "login.css") + """
<body>
<form method="post" action="{login_action}" id="login-form">
<h2>{login_heading}</h2>
<input type="password" name="password" placeholder="{password_placeholder}" autofocus>
<button type="submit">{btn_login}</button>
{error}
</form>
</body></html>"""


def _login_response(token: str, lang: str, error_html: str = "", status: int = 200) -> aioweb.Response:
    """Единая точка отдачи формы входа. Раньше четыре места собирали её
    вручную, и у формы не было action — POST уходил на текущий URL, а для
    /history и /templates POST-маршрута нет: пользователь, попавший сразу на
    эти страницы, вводил пароль и получал 405 вместо входа."""
    page = LOGIN_PAGE.format(
        error=error_html, login_action=f"/dashboard/{token}", **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", status=status,
                           headers={"Cache-Control": "no-store"})


# Баннер с Telegram Login Widget: не блокирует доступ (все действия и так уже
# разрешены по паролю), только предлагает уточнить identity для истории.
# Колбэк onTelegramAuth живёт в static/dashboard.js и берёт token/csrf из
# data-атрибутов баннера.
TELEGRAM_WIDGET_BANNER = """<div class="tg-widget-banner" data-token="{token}" data-csrf="{csrf}">
<span>{note}</span>
<script async src="https://telegram.org/js/telegram-widget.js?22"
    data-telegram-login="{bot_username}" data-size="medium" data-radius="8"
    data-onauth="onTelegramAuth(user)" data-request-access="write"></script>
</div>"""


TASKS_PAGE = _head("page_title", "tasks.css", with_js=True) + """
<body>
<h1>{heading}</h1>
{nav_html}
{tg_widget_html}
{new_task_error}
<form class="new-task-form" method="post" action="/dashboard/{token}/tasks/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{new_title_placeholder}" maxlength="200" required>
<textarea name="description" placeholder="{new_description_placeholder}" maxlength="4000" rows="2"></textarea>
<input type="datetime-local" name="due_at" required>
<button type="submit">{btn_add}</button>
</form>
{day_picker_html}
{tasks_html}
{more_done_html}
{chart_html}
{nojs_html}
<!-- Один оверлей на всю страницу вместо отдельного на каждую задачу с фото;
     открывается делегированным обработчиком из dashboard.js. -->
<div class="photo-lightbox" id="ph-lb">
<button type="button" class="photo-lightbox-close" aria-label="close">✕</button>
<!-- без src: пустой src="" некоторые браузеры трактуют как ссылку на саму
     страницу и уходят за ней лишним запросом -->
<img alt="">
</div>
</body></html>"""


HISTORY_PAGE = _head("history_page_title", "history.css") + """
<body>
<a class="nav-link" href="/dashboard/{token}">{nav_back}</a>
<h1>{history_heading}</h1>
{events_html}
{load_more_html}
</body></html>"""


TEMPLATES_PAGE = _head("templates_page_title", "templates.css") + """
<body>
<a class="nav-link" href="/dashboard/{token}">{nav_back}</a>
<h1>{templates_heading}</h1>
{new_template_error}
{templates_html}
<form class="new-task-form" method="post" action="/dashboard/{token}/templates/new">
<input type="hidden" name="csrf" value="{csrf}">
<input type="text" name="title" placeholder="{new_template_title_placeholder}" maxlength="200" required>
<label>{new_template_offset_label}</label>
<input type="time" name="time_of_day" required>
<input type="number" name="day_offset" min="0" max="365" value="0" required>
<button type="submit">{btn_save_template}</button>
</form>
</body></html>"""
