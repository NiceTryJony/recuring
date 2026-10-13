"""HTML-скелеты страниц дашборда (подставляются через str.format).

Стили и скрипты — не здесь: они лежат файлами в dashboard/static/ и
подключаются ссылками с хешем в имени (см. assets.py). Поэтому фигурные скобки
в этих шаблонах — только поля .format(), никаких задвоенных {{ }} из CSS/JS."""

from aiohttp import web as aioweb

from .assets import BASE_CSS_URL, DASHBOARD_JS_URL, FX_CSS_URL, FX_JS_URL, publish, stylesheets
from .texts import _dt_flat

# Шрифт — системный (SF Pro на Apple, Roboto на Android, Segoe UI на Windows):
# ни одного стороннего запроса перед первой отрисовкой, а оптический размер и
# трекинг система подбирает сама. Раньше грузили Fraunces + Inter с Google Fonts.

# Детектор уровня устройства — синхронно, до таблиц стилей, чтобы не было
# «мигания» дешёвого вида перед дорогим. Три уровня (см. base.css):
#   слабое — без класса: сплошные поверхности, только короткие кроссфейды;
#   среднее — .mid: >= 6 ГБ памяти и >= 6 потоков (deviceMemory браузеры
#     округляют: 4 ГБ — это слабый телефон уровня Snapdragon 645, 8 ГБ —
#     нормальный современный); блюр только у панели навигации, анимация
#     раскрытия, лайтбокс из миниатюры со свайпом;
#   мощное — .mid.fx: вдобавок настольный указатель, широкий экран и >= 8 ГБ;
#     блюр у панелей и лайтбокса, подсветка при наведении, а главное — именно тут
#     подгружаются fx.css и fx.js со всеми анимациями для ПК (текст, блоки, hover).
# Просьба системы экономить трафик или движение оставляет слабый уровень.
# Ручной выбор в шторке «Производительность» (localStorage 'perf': auto / full / lite / min)
# главнее автоматики: lite — слабый уровень, min — вдобавок без единой анимации (класс .min),
# full — как «мощное» устройство, но fx-эффекты только при мыши и широком экране.
# Без deviceMemory (Safari, Firefox) считаем 8 — это в основном iPhone/Mac.
_FX_DETECT = (
    "<script>(function(){try{var n=navigator,m=window.matchMedia,c=document.documentElement.classList,p='';"
    "try{p=localStorage.getItem('perf')||''}catch(e){}"
    "if(p==='min'){c.add('min');return}if(p==='lite')return;"
    "if(m('(prefers-reduced-motion: reduce)').matches||(n.connection&&n.connection.saveData))return;"
    "var mem=n.deviceMemory||8,cpu=n.hardwareConcurrency||8,full=p==='full';"
    "if(full||(mem>=6&&cpu>=6)){c.add('mid');"
    "if((full||mem>=8)&&m('(hover:hover) and (pointer:fine) and (min-width:900px)').matches){c.add('fx');"
    "var h=document.head,l=document.createElement('link'),s=document.createElement('script');"
    "l.rel='stylesheet';l.href='__FX_CSS__';s.src='__FX_JS__';h.appendChild(l);h.appendChild(s)}}"
    "}catch(e){}})()</script>"
).replace("__FX_CSS__", FX_CSS_URL).replace("__FX_JS__", FX_JS_URL)

# Иконки в духе SF Symbols — один спрайт на страницу, ссылки <use href="#i-…">
# только в раскрытой (ленивой) части карточки: в свёрнутой иконок нет вовсе,
# чтобы не умножать DOM на длину списка.
_ICON_SPRITE = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
    '<symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></symbol>'
    '<symbol id="i-fwd" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></symbol>'
    '<symbol id="i-cam" viewBox="0 0 24 24"><path d="M3 8a2 2 0 0 1 2-2h2l2-2.5h6L17 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="13" r="3.5"/></symbol>'
    '<symbol id="i-pen" viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16zM13 7l4 4"/></symbol>'
    '<symbol id="i-rep" viewBox="0 0 24 24"><path d="M17 2l3 3-3 3M4 11V9a4 4 0 0 1 4-4h12M7 22l-3-3 3-3M20 13v2a4 4 0 0 1-4 4H4"/></symbol>'
    '<symbol id="i-share" viewBox="0 0 24 24"><path d="M12 15V3M8 7l4-4 4 4M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></symbol>'
    '<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></symbol>'
    '<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>'
    '<symbol id="i-list" viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></symbol>'
    '<symbol id="i-tpl" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M4 10h16M10 10v10"/></symbol>'
    '<symbol id="i-hist" viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 3-6.2M4 4v4h4M12 8v4l3 2"/></symbol>'
    '</svg>'
)

# Шеврон «назад» для панели навигации вторичных страниц.
_BACK_CHEVRON = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true">'
                 '<path d="M15 4l-8 8 8 8"/></svg>')


def _head(title_field: str, css_file: str, with_js: bool = False) -> str:
    """Общий <head>. Результат ещё пройдёт через .format() вместе со всей
    страницей, поэтому скобки детектора экранируются здесь."""
    js = f'\n<script defer src="{DASHBOARD_JS_URL}"></script>' if with_js else ""
    fx_detect = _FX_DETECT.replace("{", "{{").replace("}", "}}")
    return f"""<!DOCTYPE html>
<html lang="{{html_lang}}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{{title_field}}}</title>
{fx_detect}
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f3efe6" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#151716" media="(prefers-color-scheme: dark)">
<meta name="x-err" content="{{action_failed}}">
<script defer src="https://telegram.org/js/telegram-web-app.js"></script>
{stylesheets(css_file)}{js}
</head>"""


# Точка входа Mini App (меню бота → /dashboard/app): отдаёт initData на
# /dashboard/webapp-auth и переходит на личный дашборд. Вне Telegram initData
# пуст — показываем подсказку. Строка без .format(), скобки не экранируются.
WEBAPP_ENTRY_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Dashboard</title>
<!--PRELOAD-->
<script src="https://telegram.org/js/telegram-web-app.js"></script>
</head><body>
<p id="msg"></p>
<script>
(function () {
  var tg = window.Telegram && Telegram.WebApp, msg = document.getElementById('msg');
  if (!tg || !tg.initData) { msg.textContent = 'Open this page from Telegram.'; return; }
  tg.ready();
  fetch('/dashboard/webapp-auth', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({initData: tg.initData}), credentials: 'same-origin'
  }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (d) { location.replace(d.url); })
    .catch(function () { msg.textContent = 'Login failed. Reopen the app.'; });
})();
</script></body></html>"""

# Пока entry-страница ждёт webapp-auth, браузер уже качает стили и скрипт страницы
# задач (файлы с хешем в имени, кеш вечный): переход на дашборд не ждёт сети.
WEBAPP_ENTRY_PAGE = WEBAPP_ENTRY_PAGE.replace(
    "<!--PRELOAD-->",
    f'<link rel="preload" as="style" href="{BASE_CSS_URL}">'
    f'<link rel="preload" as="style" href="{publish("tasks.css")}">'
    f'<link rel="preload" as="script" href="{DASHBOARD_JS_URL}">',
)


LOGIN_PAGE = _head("login_title", "login.css") + """
<body>
<form method="post" action="{login_action}" id="login-form">
<div class="login-badge" aria-hidden="true"><svg class="i" viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></svg></div>
<h2>{login_heading}</h2>
<div class="form-control"><input type="password" name="password" aria-label="{password_placeholder}" required autofocus><label>{password_placeholder_fl}</label></div>
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


def _navbar(title_field: str, left: str = "", right: str = "", tools: str = "") -> str:
    """Панель навигации iOS: слева «назад», по центру компактный заголовок,
    справа ссылки. Компактный заголовок и материал под панелью проявляются,
    когда большой заголовок уезжает под неё, — это CSS scroll-driven
    animation (base.css), без единой строчки JS."""
    return (f'<nav class="navbar"><div class="navbar-side">{left}</div>'
            f'<div class="navbar-title">{{{title_field}}}</div>'
            f'<div class="navbar-side navbar-right"><span class="nav-links">{right}</span>{tools}</div></nav>\n'
            f'<h1 class="large-title">{{{title_field}}}</h1>')


def _back_link() -> str:
    return f'<a class="nav-back" href="/dashboard/{{token}}">{_BACK_CHEVRON}<span>{{nav_back}}</span></a>'


TASKS_PAGE = _head("page_title", "tasks.css", with_js=True) + """
<body>
""" + _ICON_SPRITE + "\n" + _navbar("heading", right="{nav_html}", tools='<button type="button" class="perf-btn" id="perf-open" aria-label="{perf_open}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-sliders"/></svg></button>') + """
{tg_widget_html}
<div class="layout">
<aside class="side">
{summary_html}
{view_controls_html}
{day_picker_html}
{chart_html}
</aside>
<main class="list">
{tasks_html}
{more_done_html}
</main>
</div>
{nojs_html}
<details class="add-sheet"{add_open}>
<summary class="fab" aria-label="{btn_new_task}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-plus"/></svg></summary>
<form class="new-task-form" method="post" action="/dashboard/{token}/tasks/new" data-ajax="create">
<h2>{btn_new_task}</h2>
{new_task_error}
<input type="hidden" name="csrf" value="{csrf}">
<div class="form-control"><input type="text" name="title" aria-label="{new_title_placeholder}" maxlength="200" required><label>{new_title_placeholder_fl}</label></div>
<textarea name="description" placeholder="{new_description_placeholder}" maxlength="4000" rows="2"></textarea>
<input type="datetime-local" name="due_at" required>
<button type="submit" class="btn-filled">{btn_add}</button>
</form>
</details>
<div class="perf-back" id="perf-back" aria-hidden="true">
<div class="perf-sheet" role="dialog" aria-modal="true" aria-labelledby="perf-t">
<h2 id="perf-t">{perf_title}</h2>
<p>{perf_desc}</p>
<div class="perf-lv" role="group" aria-labelledby="perf-t">
<button type="button" data-l="auto">{perf_auto}</button><button type="button" data-l="full">{perf_full}</button><button type="button" data-l="lite">{perf_lite}</button><button type="button" data-l="min">{perf_min}</button>
</div>
<button type="button" class="perf-sw" role="switch" data-k="haptic"><b>{perf_haptic}</b><i></i></button>
<button type="button" class="perf-sw" role="switch" data-k="swipe"><b>{perf_swipe}</b><i></i></button>
</div>
</div>
{tabbar_html}
<!-- Один оверлей на всю страницу вместо отдельного на каждую задачу с фото;
     открывается делегированным обработчиком из dashboard.js. -->
<div class="photo-lightbox" id="ph-lb" role="dialog" aria-modal="true">
<button type="button" class="photo-lightbox-close" aria-label="close"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
<!-- без src: пустой src="" некоторые браузеры трактуют как ссылку на саму
     страницу и уходят за ней лишним запросом -->
<img alt="">
</div>
</body></html>"""


SHARE_PAGE = _head("share_page_title", "share.css", with_js=True) + """
<body>
""" + _navbar("share_page_title", left=_back_link()) + """
{content_html}
</body></html>"""


HISTORY_PAGE = _head("history_page_title", "history.css") + """
<body>
""" + _navbar("history_heading", left=_back_link()) + """
{events_html}
{load_more_html}
</body></html>"""


TEMPLATES_PAGE = _head("templates_page_title", "templates.css") + """
<body>
""" + _navbar("templates_heading", left=_back_link()) + """
{new_template_error}
{templates_html}
<form class="new-task-form" method="post" action="/dashboard/{token}/templates/new">
<input type="hidden" name="csrf" value="{csrf}">
<div class="form-control"><input type="text" name="title" aria-label="{new_template_title_placeholder}" maxlength="200" required><label>{new_template_title_placeholder_fl}</label></div>
<input type="time" name="time_of_day" required>
<label class="field-row"><span>{new_template_offset_label}</span>
<input type="number" name="day_offset" min="0" max="365" value="0" required></label>
<button type="submit" class="btn-filled">{btn_save_template}</button>
</form>
</body></html>"""
