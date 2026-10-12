"""Рендер HTML-фрагментов страниц дашборда: карточки задач, панель дней,
график, события истории, строки шаблонов. Чистые функции без обращения к БД —
все данные приходят аргументами (их собирают обработчики в views.py)."""

import calendar as cal
import datetime as dt
from html import escape

import db

from .config import BOT_USERNAME
from .texts import _dows, _dt, _float_label, _months

# Чекбокс WhiteNervosa (uiverse): контур-«капля» стирается, галочка дорисовывается.
CHECK_SVG = ('<svg viewBox="0 0 18 18" aria-hidden="true"><path d="M 1 9 L 1 9 c 0 -5 3 -8 8 -8 L 9 1 '
             'C 14 1 17 5 17 9 L 17 9 c 0 4 -4 8 -8 8 L 9 17 C 5 17 1 14 1 9 L 1 9 Z"/>'
             '<polyline points="1 9 7 14 15 4"/></svg>')


def _icon(name: str) -> str:
    """Иконка из спрайта TASKS_PAGE (pages._ICON_SPRITE). Только для раскрытой
    части карточки — в свёрнутой иконки рисует CSS, без лишних узлов DOM."""
    return f'<svg class="i" aria-hidden="true"><use href="#i-{name}"/></svg>'


def _monogram(name: str) -> str:
    """Заглушка аватара как в «Контактах»: первая буква имени в кружке."""
    return f'<span class="avatar avatar-placeholder">{escape(name.lstrip("@")[:1].upper())}</span>'


def _render_event(ev: dict, tz, lang: str, token: str, creator: dict | None) -> str:
    texts = _dt(lang)
    local_dt = ev["event_at"].astimezone(tz)
    label = texts.get(f"event_{ev['event']}", ev["event"])
    actor_id = ev["user_id"]
    if creator and (creator.get("first_name") or creator.get("username")):
        name = creator.get("first_name") or f"@{creator['username']}"
    else:
        name = f"id{actor_id}"
    if creator and creator.get("has_photo"):
        avatar_html = (f'<img class="avatar" src="/dashboard/{token}/avatar/{actor_id}" '
                       f'alt="" loading="lazy" decoding="async">')
    else:
        avatar_html = _monogram(name)
    return f"""<div class="event">
{avatar_html}
<div class="event-body">
<div class="event-line">{escape(name)} {escape(label)} «<span class="ev-title">{escape(ev["title"])}</span>»</div>
<div class="event-time">{local_dt.strftime('%d.%m.%Y %H:%M')}</div>
</div>
</div>"""


def _fmt_template_offset(day_offset: int, lang: str) -> str:
    texts = _dt(lang)
    if day_offset == 0:
        return texts["template_offset_today"]
    if day_offset == 1:
        return texts["template_offset_tomorrow"]
    return texts["template_offset_days"].format(n=day_offset)


def _render_template_row(tpl: dict, lang: str, token: str, csrf: str) -> str:
    texts = _dt(lang)
    meta = f'{escape(tpl["time_of_day"])} · {escape(_fmt_template_offset(tpl["day_offset"], lang))}'
    return f"""<div class="task">
<div class="task-body">
<div class="title">{escape(tpl["title"])}</div>
<div class="meta">{meta}</div>
<div class="task-actions">
<form method="post" action="/dashboard/{token}/templates/{tpl['id']}/apply">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="btn-tinted">{escape(texts["btn_apply_template"])}</button>
</form>
<form method="post" action="/dashboard/{token}/templates/{tpl['id']}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_template"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="del-btn">{escape(texts["btn_delete_template"])}</button>
</form>
</div>
</div>
</div>"""


def _render_day_picker(selected: "dt.date", today: "dt.date", lang: str, token: str, task_days: set,
                        filter_active: bool = True) -> str:
    """Панель выбора дня дашборда (фильтр по due_at), как неделя в «Календаре»
    iOS: выбранный день — закрашенный круг, сегодняшний — число цветом
    акцента; стрелка вниз раскрывает полный месяц. Раскрытие — чистый HTML
    <details>/<summary> (без JS): браузер сам переключает видимость и
    поворот стрелки по CSS от атрибута [open], сервер ничего не считает
    по клику — вся навигация это обычные ссылки с ?date=YYYY-MM-DD.
    task_days — set дат (date), на которые есть хотя бы одна задача, для
    точек под числами."""
    month_name = _months(lang)[selected.month - 1]
    dow_names = _dows(lang)

    # Неделя (Пн-Вс), в которую попадает selected — тот же принцип, что и в
    # оригинальном компоненте (ряд дней вокруг активного).
    week_start = selected - dt.timedelta(days=selected.weekday())
    week_days = [week_start + dt.timedelta(days=i) for i in range(7)]

    def _day_link(d: "dt.date", extra_class: str = "") -> str:
        cls = "day-item"
        if filter_active and d == selected:
            cls += " day-active"
        if d == today and not (filter_active and d == selected):
            cls += " day-today"
        dot = '<span class="day-dot"></span>' if d in task_days else ""
        return f"""<a class="{cls} {extra_class}" href="/dashboard/{token}?date={d.isoformat()}">
<div class="day-number">{d.day}</div>
<div class="day-name">{dow_names[d.weekday()]}</div>
{dot}
</a>"""

    week_html = "".join(_day_link(d) for d in week_days)

    # Полный месяц selected — те же ссылки-дни, сеткой 7 колонок, с пустыми
    # ячейками в начале/конце для выравнивания по дню недели.
    _, days_in_month = cal.monthrange(selected.year, selected.month)
    first_of_month = selected.replace(day=1)
    lead_empty = first_of_month.weekday()  # 0=Пн
    month_cells = ['<div class="day-empty"></div>'] * lead_empty
    for day_num in range(1, days_in_month + 1):
        month_cells.append(_day_link(first_of_month.replace(day=day_num), "day-item-month"))
    month_html = "".join(month_cells)

    # Ссылка сброса видна всегда, когда фильтр активен (вернуться к полному
    # списку задач) — а не только когда выбранный день не сегодня.
    reset_html = (f'<a class="day-today-link" href="/dashboard/{token}" aria-label="reset">'
                  f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v5h5M5.5 15a7 7 0 1 0 0-6"/></svg></a>'
                  if filter_active else "")

    return f"""<div class="day-picker">
<div class="day-picker-header">
<span class="day-picker-month">{month_name} {selected.year}</span>
{reset_html}
</div>
<details class="day-picker-expand">
<summary class="day-picker-summary">
<div class="date-nav-container">{week_html}</div>
</summary>
<div class="day-picker-month-grid">
<div class="day-picker-dow-row">{"".join(f'<div class="day-dow-label">{n}</div>' for n in dow_names)}</div>
<div class="day-picker-month-cells">{month_html}</div>
</div>
</details>
</div>"""


def _render_author(created_by: int | None, creator: dict | None, token: str) -> str:
    """Бейдж автора (аватар + имя) для группового дашборда. creator — запись из
    db.get_telegram_users (есть только у тех, кто хоть раз логинился через
    Telegram Login Widget); для остальных просто показываем raw id."""
    if not created_by:
        return ""
    if creator and (creator.get("first_name") or creator.get("username")):
        name = creator.get("first_name") or f"@{creator['username']}"
    else:
        name = f"id{created_by}"
    if creator and creator.get("has_photo"):
        avatar_html = (f'<img class="avatar" src="/dashboard/{token}/avatar/{created_by}" '
                       f'alt="" loading="lazy" decoding="async">')
    else:
        avatar_html = _monogram(name)
    return f'<div class="task-author">{avatar_html}<span>{escape(name)}</span></div>'


PHOTO_SLOT_WIDTH = 118  # px — ширина одного кадра карусели, используется и в CSS, и в JS-прокрутке стрелками (было 96 — фото было мелковато различимо, укрупнили)


def _render_photos(photos: list[dict], token: str, task_id: int, csrf: str, texts: dict) -> str:
    """Карусель фото под задачей. Сначала самый частый случай — ОДНО фото —
    рендерится крупно на всю ширину карточки (.task-photo-hero), а не
    мелким квадратиком 118×118 из общей сетки: именно одно фото теряло
    больше всего в читаемости при сжатии в квадрат. 2+ фото — прежняя
    карусель (до 3 кадров разом, scroll-snap, стрелки только если фото
    больше 3 — незачем рисовать управление, которым нечего листать).

    В ленту идут ПРЕВЬЮ (?size=thumb) — полный кадр подтягивается только в
    лайтбоксе по тапу. Лайтбокс на страницу один (см. TASKS_PAGE в
    pages.py) и открывается делегированным обработчиком по data-full:
    раньше на каждую задачу с фото рендерился свой оверлей плюс по
    inline-onclick на каждом кадре."""
    if not photos:
        return ""
    track_id = f"ph-track-{task_id}"
    is_hero = len(photos) == 1
    items = []
    for p in photos:
        src = f"/dashboard/{token}/tasks/{task_id}/photo/{p['id']}"
        photo_class = "task-photo task-photo-hero" if is_hero else "task-photo"
        # loading=lazy здесь особенно уместно: карточка свёрнута (<details>
        # закрыт), и браузер вообще не тронет эти картинки, пока её не
        # раскроют. decoding=async — чтобы декодирование JPEG не занимало
        # главный поток в момент раскрытия.
        items.append(f"""<div class="{photo_class}" data-full="{src}">
<img src="{src}?size=thumb" alt="" loading="lazy" decoding="async" fetchpriority="low">
<form method="post" action="{src}/delete" onsubmit="return confirm('{escape(texts["confirm_delete_photo"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="photo-del-btn" aria-label="delete photo"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg></button>
</form>
</div>""")

    arrows_html = ""
    if len(photos) > 3:
        # scrollBy на фиксированный шаг (один кадр) — явное, предсказуемое
        # перемещение по клику, никакого авто-таймера/зацикливания.
        # Сама прокрутка — в dashboard.js по data-track/data-step.
        arrows_html = (
            f'<button type="button" class="photo-nav photo-nav-prev" aria-label="prev" '
            f'data-track="{track_id}" data-step="-{PHOTO_SLOT_WIDTH}">‹</button>\n'
            f'<button type="button" class="photo-nav photo-nav-next" aria-label="next" '
            f'data-track="{track_id}" data-step="{PHOTO_SLOT_WIDTH}">›</button>'
        )

    wrap_class = "task-photos-wrap hero" if is_hero else "task-photos-wrap"
    return f"""<div class="{wrap_class}">
<div class="task-photos" id="{track_id}">{"".join(items)}</div>
{arrows_html}
</div>"""


def _render_upload_form(token: str, task_id: int, csrf: str, texts: dict, photo_count: int) -> str:
    """Форма прикрепления фото — скрыта, когда лимит на задачу уже достигнут
    (MAX_PHOTOS_PER_TASK), чтобы не провоцировать запрос, который сервер
    всё равно отклонит."""
    if photo_count >= db.MAX_PHOTOS_PER_TASK:
        return ""
    return f"""<form method="post" action="/dashboard/{token}/tasks/{task_id}/photo" enctype="multipart/form-data" class="photo-upload-form">
<input type="hidden" name="csrf" value="{csrf}">
<label class="photo-upload-btn">{_icon("cam")}{escape(texts["btn_add_photo"])}
<input type="file" name="photo" accept="image/*" onchange="this.form.submit()">
</label>
</form>"""


def _render_subtasks(subtasks: list[dict], token: str, task_id: int, csrf: str, texts: dict) -> str:
    """Чек-лист подзадач внутри раскрытой карточки — раньше заводился только
    из чата (FSM-диалог /subtasks в bot.py), теперь то же самое доступно и
    с дашборда: свой toggle/delete/add на отдельных маршрутах, та же модель
    данных (subtasks таблица, которую /subtasks в Telegram читает и пишет)."""
    rows = []
    for st in subtasks:
        row_class = "subtask-row done" if st["done"] else "subtask-row"
        rows.append(f"""<div class="{row_class}">
<form method="post" action="/dashboard/{token}/subtasks/{st['id']}/toggle" data-ajax="sub">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="subtask-check" aria-label="toggle">{CHECK_SVG}</button>
</form>
<span class="subtask-title">{escape(st["title"])}</span>
<form method="post" action="/dashboard/{token}/subtasks/{st['id']}/delete" data-ajax="sub" onsubmit="return confirm('{escape(texts["confirm_delete_subtask"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="subtask-del-btn" aria-label="delete">✕</button>
</form>
</div>""")
    list_html = f'<div class="subtasks">{"".join(rows)}</div>' if rows else ""
    add_form = f"""<form class="subtask-add-form" method="post" action="/dashboard/{token}/tasks/{task_id}/subtasks/new" data-ajax="sub">
<input type="hidden" name="csrf" value="{csrf}">
<div class="form-control"><input type="text" name="title" aria-label="{escape(texts["new_subtask_placeholder"])}" maxlength="200" required><label>{_float_label(texts["new_subtask_placeholder"])}</label></div>
<button type="submit" class="btn-tinted">{escape(texts["btn_add_subtask"])}</button>
</form>"""
    # Обёртка нужна fetch-действиям: сервер присылает новый блок целиком, клиент
    # подменяет его, не трогая остальную (раскрытую) карточку.
    return f'<div class="subtasks-block">{list_html}{add_form}</div>'


def _render_description(task: dict, token: str, csrf: str, texts: dict) -> str:
    """Текст описания + вложенная форма правки (см. комментарий в _render_task).
    Отдельной функцией, чтобы fetch-ответ мог прислать только этот блок."""
    description = task.get("description") or ""
    description_text_html = f'<p class="description-text">{escape(description)}</p>' if description else ""
    edit_label = texts["edit_description_label"] if description else texts["add_description_label"]
    return f"""<div class="desc-block">
{description_text_html}
<details class="edit-toggle">
<summary>{_icon("pen")}{escape(edit_label)}</summary>
<form class="desc-form" method="post" action="/dashboard/{token}/tasks/{task['id']}/edit" data-ajax="desc">
<input type="hidden" name="csrf" value="{csrf}">
<textarea name="description" placeholder="{escape(texts["description_placeholder"])}" maxlength="4000" rows="2">{escape(description)}</textarea>
<button type="submit" class="btn-tinted">{escape(texts["btn_save_description"])}</button>
</form>
</details>
</div>"""


def _render_task(
    task: dict, tz, lang: str, token: str, csrf: str,
    creator: dict | None = None, owner_type: str = "user",
    photos: list[dict] | None = None,
    subtasks: list[dict] | None = None,
    *, now: "dt.datetime | None" = None, local_due: "dt.datetime | None" = None,
    lazy: bool = False,
) -> str:
    """now/local_due — посчитанные один раз на страницу (handle_dashboard и
    так переводит каждый due_at в зону владельца для фильтра по дню); без них
    функция считает сама, как раньше.

    lazy=True кладёт раскрытую часть карточки в <template>: браузер не
    создаёт из неё живой DOM, пока карточку не раскроют (это делает
    static/dashboard.js). Свёрнутая карточка — это две строки текста, а
    раскрытая часть — 5-8 форм, textarea с копией описания, подзадачи и
    карусель; на длинном списке разница в числе узлов многократная."""
    texts = _dt(lang)
    if local_due is None:
        local_due = task["due_at"].astimezone(tz)
    if now is None:
        now = dt.datetime.now(tz)
    css_class = "task"
    if task["done"]:
        css_class += " done"
    elif local_due < now and task["repeat"] == "none":
        css_class += " overdue"

    # Тег — как в «Напоминаниях»: «#тег» цветом акцента, без плашки-эмодзи.
    tag_html = f'<div class="tag">#{escape(task["tag"])}</div>' if task.get("tag") else ""
    repeat_label = texts["repeat"].get(task["repeat"], task["repeat"])
    # Короткий значок повтора — виден уже в свёрнутой шапке карточки (summary):
    # класс .rep, саму иконку рисует CSS (::after), чтобы не плодить узлы SVG
    # на каждую карточку длинного списка. Полная формулировка («Повторяется:
    # раз в месяц») — только внутри, в .repeat-chip.
    meta_class = "meta rep" if task["repeat"] != "none" and repeat_label else "meta"
    repeat_info_html = (
        f'<div class="repeat-chip">{_icon("rep")}{escape(texts["repeats_label"].format(label=repeat_label))}</div>'
        if task["repeat"] != "none" and repeat_label
        else f'<div class="repeat-chip">{escape(texts["no_repeat_label"])}</div>'
    )
    toggle_action = "undone" if task["done"] else "done"
    # Автора показываем только в групповом дашборде — в личном он и так всегда
    # один и тот же человек, бейдж был бы бесполезным шумом.
    author_html = _render_author(task.get("created_by"), creator, token) if owner_type == "chat" else ""

    photos = photos or []
    photos_html = _render_photos(photos, token, task["id"], csrf, texts)
    upload_html = _render_upload_form(token, task["id"], csrf, texts, len(photos))

    # Раньше текст описания дублировался: один раз как read-only абзац, рядом —
    # та же строка в textarea. Теперь textarea спрятана во вложенный <details>
    # (см. _render_description): по умолчанию виден только чистый текст.
    description_html = _render_description(task, token, csrf, texts)
    subtasks_html = _render_subtasks(subtasks or [], token, task["id"], csrf, texts)

    extra_actions = ""
    if not task["done"]:
        # «Отложить на день» двигает due_at в БД, но у повторяющихся задач due_at
        # не источник расписания (см. bot.py: schedule_task строит CronTrigger
        # один раз, а restore_jobs() при рестарте пересоберёт его уже по
        # сдвинутому due_at) — кнопка либо ничего не даёт сейчас, либо незаметно
        # съедет день/час повтора после следующего рестарта процесса. Поэтому
        # показываем её только для разовых задач (repeat == "none"); тот же
        # repeat != "none" уже отдельно обрабатывается в bot.py/snooze_task.
        postpone_html = (
            f"""<form method="post" action="/dashboard/{token}/tasks/{task['id']}/postpone" data-ajax="task">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">{_icon("fwd")}{escape(texts["btn_postpone"])}</button>
</form>"""
            if task["repeat"] == "none" else ""
        )
        share_html = (
            f"""<button type="button" class="share-btn" data-url="/dashboard/{token}/tasks/{task['id']}/share" data-csrf="{csrf}" data-copied="{escape(texts["share_link_copied"])}">{_icon("share")}{escape(texts["btn_share"])}</button>"""
            if BOT_USERNAME else ""
        )
        extra_actions = f"""<div class="task-actions">
{share_html}
{postpone_html}
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/delete" data-ajax="task" onsubmit="return confirm('{escape(texts["confirm_delete"])}')">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="del-btn">{_icon("trash")}{escape(texts["btn_delete"])}</button>
</form>
</div>"""

    expanded_html = f"""<div class="task-expanded">
{repeat_info_html}
{author_html}
{description_html}
{subtasks_html}
{photos_html}
{upload_html}
{extra_actions}
</div>"""
    if lazy:
        expanded_html = f'<template class="task-x">{expanded_html}</template>'

    return f"""<div class="{css_class}" id="task-{task['id']}" data-due="{int(local_due.timestamp())}">
<form method="post" action="/dashboard/{token}/tasks/{task['id']}/{toggle_action}" data-ajax="task">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="check-btn" aria-label="toggle">{CHECK_SVG}</button>
</form>
<details class="task-details">
<summary class="task-summary">
<div class="title">{escape(task["title"])}</div>
<div class="{meta_class}">{local_due.strftime('%d.%m.%Y %H:%M')}</div>
{tag_html}
</summary>
{expanded_html}
</details>
</div>"""


def _render_chart(series: list, streak: int, texts: dict) -> str:
    """Столбчатый график на чистом HTML/CSS: высота столбца в px, без JS."""
    max_px = 90
    peak = max((c for _, c in series), default=0)
    cols, labels = [], []
    for d, c in series:
        px = round(c / peak * max_px) if peak else 0
        zero = " zero" if c == 0 else ""
        cols.append(
            f'<div class="col" title="{d.strftime("%d.%m")}: {c}">'
            f'<div class="num">{c if c else ""}</div><div class="bar{zero}" style="height:{max(px, 2)}px"></div></div>'
        )
        labels.append(f"<span>{d.day}</span>")
    streak_html = f'<div class="streak">{escape(texts["streak"].format(n=streak))}</div>' if streak > 0 else ""
    title = escape(texts["chart_title"].format(days=len(series)))
    return (
        f'<div class="chart-block"><div class="chart-head"><div class="chart-title">{title}</div>{streak_html}</div>'
        f'<div class="bars">{"".join(cols)}</div><div class="labels">{"".join(labels)}</div></div>'
    )
