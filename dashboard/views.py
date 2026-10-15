"""GET-страницы дашборда: задачи, история событий, шаблоны.

Доступ — по уникальному токену в URL (/dashboard/<token>) + пароль в форме
логина. Токен может принадлежать как личному пользователю, так и групповому
чату (общие задачи чата) — db.get_owner_by_dashboard_token() ищет среди обоих
и возвращает owner_id/owner_type, дальше вся логика страницы работает с ними
одинаково."""

import asyncio
import datetime as dt
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from aiohttp import web as aioweb

import db
from stats import compute_streak, daily_series
from task_groups import GROUPS, VIEWS, group_tasks, select_tasks

from .config import (
    BOT_USERNAME, CHART_DAYS, DONE_TASKS_PAGE_SIZE, SESSION_MAX_AGE, SHARE_CODE_RE, UTC, _tz_of, logger,
)
from .pages import (
    HISTORY_PAGE, SHARE_PAGE, TASKS_PAGE, TELEGRAM_WIDGET_BANNER, TEMPLATES_PAGE, WEBAPP_ENTRY_PAGE,
    _login_response,
)
from .render import _render_chart, _render_day_picker, _render_event, _render_task, _render_template_row
from .security import (
    _clear_login_attempts, _client_ip, _csrf_token, _login_rate_limited, _record_failed_login,
    _sign_session, _verify_session, verify_password,
)
from .texts import _dt, _dt_flat, _dt_tasks_page

HISTORY_PAGE_SIZE = 30


async def handle_webapp_entry(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/app — стартовая страница Mini App (см. WEBAPP_ENTRY_PAGE)."""
    return aioweb.Response(text=WEBAPP_ENTRY_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_share_preview(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/share/{code} — экран «Добавить к своим задачам».
    Только для личного дашборда получателя; язык и часовой пояс — его."""
    token = request.match_info["token"]
    code = request.match_info["code"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings or settings["owner_type"] != "user":
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, "user"):
        return _login_response(token, lang)

    src = await db.get_task_by_share_code(code) if SHARE_CODE_RE.fullmatch(code) else None
    if not src:
        content = f'<div class="empty">{escape(texts["share_unavailable"])}</div>'
    else:
        tz = _tz_of(settings)
        now = datetime.now(tz)
        due = src["due_at"].astimezone(tz)
        if due <= now:
            due = now + dt.timedelta(hours=1)  # прошедшую дату не предлагаем — напоминание сработало бы сразу
        subtasks = await db.get_subtasks(src["id"])
        tag_html = f'<div class="tag">#{escape(src["tag"])}</div>' if src.get("tag") else ""
        desc_html = f'<p class="description-text">{escape(src["description"])}</p>' if src.get("description") else ""
        subs_html = ""
        if subtasks:
            items = "".join(f"<li>{escape(s['title'])}</li>" for s in subtasks)
            subs_html = (f'<div class="share-label">{escape(texts["share_subtasks_label"])}</div>'
                         f'<ul class="share-subtasks">{items}</ul>')
        error_html = (f'<div class="new-task-error">{escape(texts["error_bad_due_at"])}</div>'
                      if request.query.get("error") == "bad_due_at" else "")
        content = f"""<form class="share-card" id="share-form" method="post" action="/dashboard/{token}/share/{code}/accept">
<input type="hidden" name="csrf" value="{_csrf_token(cookie)}">
<h2 class="share-title">{escape(src["title"])}</h2>
{tag_html}
{desc_html}
{subs_html}
<label class="field-row"><span>{escape(texts["share_due_label"])}</span>
<input type="datetime-local" name="due_at" value="{due.strftime('%Y-%m-%dT%H:%M')}" required></label>
{error_html}
<button type="submit" class="btn-filled" id="share-add">{escape(texts["share_add_btn"])}</button>
</form>"""

    page = SHARE_PAGE.format(token=token, content_html=content, **_dt_flat(lang))
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_history(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/history — пока только для группового дашборда:
    для личного автор события всегда один и тот же человек, лента не нужна."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    if owner_type != "chat":
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}")
    # current_password_version обязателен: без него смена пароля дашборда
    # инвалидировала сессию на всех страницах, кроме этой, и утёкшая cookie
    # продолжала читать ленту событий чата.
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        # Как и на главной странице — незалогиненный видит форму пароля.
        return _login_response(token, lang)

    tz = _tz_of(settings)

    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0

    # Берём на одну запись больше лимита — если она есть, значит дальше ещё
    # что-то осталось и нужно показать "Показать ещё".
    events = await db.get_event_feed(None, owner_id, limit=HISTORY_PAGE_SIZE + 1, offset=offset)
    has_more = len(events) > HISTORY_PAGE_SIZE
    events = events[:HISTORY_PAGE_SIZE]

    if not events:
        events_html = f'<div class="empty">{escape(texts["history_empty"])}</div>'
    else:
        actor_ids = {e["user_id"] for e in events}
        creators = await db.get_telegram_users(list(actor_ids))
        events_html = "\n".join(_render_event(e, tz, lang, token, creators.get(e["user_id"])) for e in events)

    load_more_html = ""
    if has_more:
        next_offset = offset + HISTORY_PAGE_SIZE
        load_more_html = f'<a class="load-more" href="/dashboard/{token}/history?offset={next_offset}">{escape(texts["btn_load_more"])}</a>'

    page = HISTORY_PAGE.format(
        token=token, events_html=events_html, load_more_html=load_more_html, **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_templates_page(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/templates — список своих шаблонов + форма
    создания нового. Доступно и личному, и групповому владельцу (в отличие
    от /history) — см. комментарий у nav_links в handle_dashboard."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        return _login_response(token, lang)

    csrf = _csrf_token(cookie)
    templates = await db.get_templates(owner_id, owner_type)
    if not templates:
        templates_html = f'<div class="empty">{escape(texts["templates_empty"])}</div>'
    else:
        templates_html = "\n".join(_render_template_row(tpl, lang, token, csrf) for tpl in templates)

    new_template_error = ""
    err = request.query.get("error")
    if err == "empty_title":
        new_template_error = f'<div class="new-task-error">{escape(texts["error_empty_template_title"])}</div>'
    elif err == "limit":
        new_template_error = (
            f'<div class="new-task-error">'
            f'{escape(texts["template_limit_reached"].format(n=db.MAX_TEMPLATES_PER_OWNER))}</div>'
        )

    page = TEMPLATES_PAGE.format(
        token=token, csrf=csrf, templates_html=templates_html,
        new_template_error=new_template_error, **_dt_flat(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})




async def _build_chart(owner_id: int, owner_type: str, tz, texts: dict) -> tuple[str, int]:
    # личный дашборд — история самого пользователя; групповой — события по общим задачам чата
    user_id, chat_id = (owner_id, None) if owner_type == "user" else (None, owner_id)
    rows, done_days = await asyncio.gather(
        db.get_history_stats(user_id, CHART_DAYS, tz.key, chat_id=chat_id),
        db.get_done_days(user_id, tz.key, chat_id=chat_id),
    )
    today = datetime.now(tz).date()
    series = daily_series(rows, "done", CHART_DAYS, today)
    streak = compute_streak(done_days, today)
    return _render_chart(series, streak, texts), streak


async def _safe_chart(owner_id: int, owner_type: str, tz, texts: dict) -> tuple[str, int | None]:
    try:
        return await _build_chart(owner_id, owner_type, tz, texts)
    except Exception:
        # график — второстепенный блок: сбой БД/статистики не должен ронять весь дашборд
        logger.exception("Не удалось построить график активности owner=%s/%s", owner_type, owner_id)
        return "", None


async def _safe_streak(owner_id: int, owner_type: str, tz) -> int | None:
    """Стрик нужен сводке над сгибом, а график (второй SQL) — нет: он грузится
    отдельным фрагментом. Сбой статистики не должен ронять страницу."""
    user_id, chat_id = (owner_id, None) if owner_type == "user" else (None, owner_id)
    try:
        done_days = await db.get_done_days(user_id, tz.key, chat_id=chat_id)
        return compute_streak(done_days, datetime.now(tz).date())
    except Exception:
        logger.exception("Не удалось посчитать стрик owner=%s/%s", owner_type, owner_id)
        return None


def _month_window(anchor: dt.date) -> tuple[dt.date, dt.date]:
    """Даты, для которых панель дней рисует точки: неделя и месяц вокруг anchor
    (неделя может заходить в соседние месяцы)."""
    first = anchor.replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return first - dt.timedelta(days=7), last + dt.timedelta(days=7)


async def _page_state(owner_id: int, owner_type: str, tz, now, *, view: str, tag: str | None, done_limit: int,
                      selected_date: dt.date | None, filter_active: bool, group: str = "date") -> dict:
    """Всё, что нужно странице и fetch-ответам от БД, двумя лёгкими запросами
    (get_dashboard_tasks + get_dashboard_stats) вместо загрузки всех задач за
    всё время. Возвращает задачи для показа, их локальные сроки и агрегаты."""
    today = now.date()
    anchor = selected_date if (filter_active and selected_date) else today
    days_from, days_to = _month_window(anchor)
    day_start = day_end = None
    if filter_active and selected_date:
        day_start = dt.datetime.combine(selected_date, dt.time.min, tzinfo=tz).astimezone(UTC)
        day_end = dt.datetime.combine(selected_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz).astimezone(UTC)
    raw, stats = await asyncio.gather(
        db.get_dashboard_tasks(owner_id, owner_type, done_limit, tag=tag, day_start=day_start, day_end=day_end),
        db.get_dashboard_stats(owner_id, owner_type, tz.key, tag, days_from, days_to),
    )
    local_due = {t["id"]: t["due_at"].astimezone(tz) for t in raw}
    tasks, _ = select_tasks(
        raw, local_due, view=view, today=today, selected_date=anchor,
        filter_active=filter_active, done_limit=done_limit, tag=tag, hide_done=group == "none",
    )
    if filter_active or view in ("today", "upcoming") or (group == "none" and view == "all"):
        hidden_done = 0
    else:
        hidden_done = max(0, stats["done_total"] - sum(1 for t in tasks if t["done"]))
    progress = round(100 * stats["today_done"] / stats["today_total"]) if stats["today_total"] else None
    return {
        "tasks": tasks, "local_due": local_due, "stats": stats, "hidden_done": hidden_done, "progress": progress,
        "tags": sorted(stats["tags"], key=lambda kv: kv[0].casefold()),
    }


def _more_done_html(token: str, texts: dict, hidden_done: int, done_limit: int, nojs: bool,
                    view: str = "all") -> str:
    if not hidden_done:
        return ""
    nojs_q = "&nojs=1" if nojs else ""
    # view в ссылке обязателен: без него страница открылась бы на вкладке по умолчанию (upcoming)
    return (
        f'<a class="load-more" href="/dashboard/{token}?view={view}&done_limit={done_limit + DONE_TASKS_PAGE_SIZE}{nojs_q}">'
        f'{escape(texts["btn_more_done"].format(n=hidden_done))}</a>'
    )


async def handle_chart(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/chart — блок графика отдельным фрагментом: на
    телефоне он ниже сгиба, и его два SQL-запроса не должны задерживать
    первую отрисовку списка (страница подгружает его, когда блок подъезжает)."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(status=404)
    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (settings["owner_id"], settings["owner_type"]):
        return aioweb.Response(status=403)
    chart_html, _streak = await _safe_chart(settings["owner_id"], settings["owner_type"], _tz_of(settings),
                                            _dt(settings.get("language", "ru")))
    return aioweb.Response(text=chart_html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def _handle_login_post(request: aioweb.Request, token: str, settings: dict, lang: str) -> aioweb.Response:
    texts = _dt(lang)
    ip = _client_ip(request)
    if _login_rate_limited(token, ip):
        error_html = f'<div class="error">{escape(texts["error_too_many_attempts"])}</div>'
        return _login_response(token, lang, error_html, status=429)

    data = await request.post()
    password = data.get("password", "")
    if await asyncio.to_thread(verify_password, password, settings["dashboard_password_hash"] or ""):
        _clear_login_attempts(token, ip)
        # tg_user_id=0: кто именно ввёл общий пароль, нам неизвестно — для
        # owner_type='chat' это уточнится позже через виджет на самой
        # странице (см. handle_telegram_auth), без этого шага деградируем
        # на owner_id (chat_id) в истории, как было раньше.
        session_value = _sign_session(settings["owner_id"], settings["owner_type"],
                                      password_version=settings.get("dashboard_password_version", 0))
        resp = aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})
        # Render терминирует TLS на прокси — схему берём из X-Forwarded-Proto
        is_https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
        resp.set_cookie(f"session_{token}", session_value, max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="Strict", secure=is_https)
        return resp

    _record_failed_login(token, ip)
    error_html = f'<div class="error">{escape(texts["error_wrong_password"])}</div>'
    return _login_response(token, lang, error_html, status=401)


def _view_controls(token: str, texts: dict, view: str, group: str, tag: str | None,
                   tags: list[tuple[str, int]], nojs: bool) -> str:
    """Вкладки (Все/Сегодня/…), переключатель группировки и чипы тегов — обычные
    ссылки с query, работают без JS. Параметры, не относящиеся к ссылке,
    сохраняются (кроме ?date=: смена вкладки снимает фильтр по дню)."""
    def href(**over) -> str:
        q = {"view": view, "group": group, "tag": tag or "", "nojs": "1" if nojs else ""}
        q.update(over)
        q = {k: v for k, v in q.items() if v and not (k == "view" and v == "upcoming") and not (k == "group" and v == "date")}
        return f"/dashboard/{token}" + (f"?{urlencode(q)}" if q else "")

    def link(label: str, url: str, active: bool, cls: str) -> str:
        return f'<a class="{cls}{" active" if active else ""}" href="{escape(url)}">{escape(label)}</a>'

    tabs = "".join(link(texts[f"view_{v}"], href(view=v), v == view, "view-tab") for v in VIEWS)
    groups = "".join(link(texts[f"group_{g}"], href(group=g), g == group, "group-opt") for g in GROUPS)
    chips = ""
    if tags:
        chips = "".join(link(f"{name} {n}", href(tag="" if name == tag else name), name == tag, "tag-chip")
                        for name, n in tags)
        chips = f'<div class="tag-chips">{chips}</div>'
    return f'<div class="view-controls"><nav class="view-tabs">{tabs}</nav><div class="group-switch">{groups}</div>{chips}</div>'


def _summary(texts: dict, today_n: int, overdue_n: int, streak: int | None, progress: int | None = None) -> str:
    stats = [("today", today_n, texts["stat_today"]), ("overdue", overdue_n, texts["stat_overdue"])]
    if streak is not None:
        stats.append(("streak", streak, texts["stat_streak"]))
    cells = []
    for cls, n, label in stats:
        ring = f' style="--p:{progress}"' if cls == "today" and progress is not None else ""
        cells.append(
            f'<div class="stat stat-{cls}"{ring}><span class="stat-num">{n}</span>'
            f'<span class="stat-label">{escape(label)}</span></div>'
        )
    cells = "".join(cells)
    return f'<section class="summary">{cells}</section>'


def _tabbar(token: str, texts: dict, owner_type: str) -> str:
    """Нижняя панель разделов (телефон / Mini App); на десктопе скрывается CSS."""
    items = [(f"/dashboard/{token}", "i-list", texts["nav_tasks"], True),
             (f"/dashboard/{token}/templates", "i-tpl", texts["nav_templates"], False)]
    if owner_type == "chat":
        items.append((f"/dashboard/{token}/history", "i-hist", texts["nav_history"], False))
    links = "".join(
        f'<a class="tab{" active" if cur else ""}" href="{href}"><svg class="i" viewBox="0 0 24 24" aria-hidden="true">'
        f'<use href="#{icon}"/></svg><span>{escape(label)}</span></a>'
        for href, icon, label, cur in items
    )
    return f'<nav class="tabbar">{links}</nav>'


def _group_heading(key, mode: str, texts: dict, count: int) -> str:
    if mode == "tag":
        label = key or texts["group_untagged"]
    else:
        label = key.strftime("%d.%m")
    return f'<h2 class="group-title">{escape(label)} <span class="group-count">{count}</span></h2>'


async def handle_dashboard(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        # До того, как мы знаем настройки конкретного владельца, язык
        # страницы с ошибкой определить нечем — используем дефолт (ru).
        return aioweb.Response(text=_dt("ru")["not_found"], status=404)

    owner_id = settings["owner_id"]
    owner_type = settings["owner_type"]
    lang = settings.get("language", "ru")
    texts = _dt(lang)

    if request.method == "POST":
        return await _handle_login_post(request, token, settings, lang)

    cookie = request.cookies.get(f"session_{token}")
    pwd_version = settings.get("dashboard_password_version", 0)
    session = _verify_session(cookie, current_password_version=pwd_version) if cookie else None
    if session is None or (session[0], session[1]) != (owner_id, owner_type):
        return _login_response(token, lang)
    _, _, tg_user_id = session

    tz = _tz_of(settings)
    csrf = _csrf_token(cookie)
    now = dt.datetime.now(tz)
    today = now.date()

    raw_date = request.query.get("date", "")
    selected_date = today
    filter_active = bool(raw_date)
    if raw_date:
        try:
            selected_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            selected_date, filter_active = today, False  # битый параметр — тихо откатываемся на "все задачи"
    try:
        done_limit = max(DONE_TASKS_PAGE_SIZE, int(request.query.get("done_limit", DONE_TASKS_PAGE_SIZE)))
    except ValueError:
        done_limit = DONE_TASKS_PAGE_SIZE
    # ?nojs=1 — полная версия без <template>: ссылка на неё стоит в <noscript>,
    # без JS ленивые карточки было бы не раскрыть.
    nojs = request.query.get("nojs") == "1"

    view = request.query.get("view", "upcoming")
    if view not in VIEWS:
        view = "upcoming"
    group_mode = request.query.get("group", "date")
    if group_mode not in GROUPS:
        group_mode = "date"
    tag = request.query.get("tag") or None

    state = await _page_state(
        owner_id, owner_type, tz, now, view=view, tag=tag, done_limit=done_limit,
        selected_date=selected_date, filter_active=filter_active, group=group_mode,
    )
    tasks, local_due, stats = state["tasks"], state["local_due"], state["stats"]
    today_n, overdue_n, hidden_done = stats["today"], stats["overdue"], state["hidden_done"]
    view_controls_html = _view_controls(token, texts, view, group_mode, tag, state["tags"], nojs)

    # task_days — для точек-индикаторов под числами панели: на какие дни есть задачи.
    day_picker_html = _render_day_picker(
        selected_date if filter_active else today, today, lang, token, stats["days"], filter_active=filter_active
    )

    # График — отдельным фрагментом (handle_chart, подгружает dashboard.js); без JS
    # (?nojs=1) рисуем сразу, как раньше.
    if nojs:
        chart_html, streak = await _safe_chart(owner_id, owner_type, tz, texts)
    else:
        chart_html = f'<section class="chart-block chart-lazy" data-src="/dashboard/{token}/chart" aria-busy="true"></section>'
        streak = None

    if not tasks:
        tasks_html = f'<div class="empty">{escape(texts["empty"])}</div>'
        if not nojs:
            streak = await _safe_streak(owner_id, owner_type, tz)
    else:
        async def _creators() -> dict[int, dict]:
            if owner_type != "chat":
                return {}
            return await db.get_telegram_users(list({t["created_by"] for t in tasks if t.get("created_by")}))

        async def _streak() -> int | None:
            return streak if nojs else await _safe_streak(owner_id, owner_type, tz)

        # Фото и подзадачи — ОДИН запрос на всю страницу каждый (WHERE task_id
        # = ANY(...)), а не по запросу на задачу. И все независимые выборки
        # (авторы, фото, подзадачи, стрик) идут параллельно, а не друг за
        # другом — время ответа = самый медленный запрос, а не их сумма.
        task_ids = [t["id"] for t in tasks]
        creators, photos_by_task, subtasks_by_task, streak = await asyncio.gather(
            _creators(), db.get_task_photos_meta_bulk(task_ids), db.get_subtasks_bulk(task_ids), _streak(),
        )
        def _card(t: dict) -> str:
            return _render_task(
                t, tz, lang, token, csrf, creator=creators.get(t.get("created_by")), owner_type=owner_type,
                photos=photos_by_task.get(t["id"]), subtasks=subtasks_by_task.get(t["id"]),
                now=now, local_due=local_due[t["id"]], lazy=not nojs,
            )

        if group_mode == "none":
            tasks_html = "\n".join(_card(t) for t in tasks)
        else:
            tasks_html = "\n".join(
                f'<section class="task-group">{_group_heading(key, group_mode, texts, len(items))}\n'
                + "\n".join(_card(t) for t in items) + "</section>"
                for key, items in group_tasks(tasks, local_due, group_mode)
            )

    more_done_html = _more_done_html(token, texts, hidden_done, done_limit, nojs, view)
    nojs_html = "" if nojs else (
        f'<noscript><a class="load-more" href="/dashboard/{token}?nojs=1">'
        f'{escape(texts["nojs_full_version"])}</a></noscript>'
    )

    heading = texts["heading_chat"] if owner_type == "chat" else texts["heading"]
    # В отличие от истории (nav_history) — лента событий с авторами нужна
    # только групповому дашборду (см. комментарий у handle_history) — шаблоны
    # полезны и личному владельцу, поэтому ссылка показывается всегда.
    nav_links = [f'<a class="nav-link" href="/dashboard/{token}/templates">{escape(texts["nav_templates"])}</a>']
    if owner_type == "chat":
        nav_links.append(f'<a class="nav-link" href="/dashboard/{token}/history">{escape(texts["nav_history"])}</a>')
    nav_html = " ".join(nav_links)
    tabbar_html = _tabbar(token, texts, owner_type)

    error_key = {"empty_title": "error_empty_title", "bad_photo": "error_bad_photo",
                 "bad_due_at": "error_bad_due_at"}.get(request.query.get("error", ""))
    new_task_error_html = f'<div class="new-task-error">{escape(texts[error_key])}</div>' if error_key else ""

    # Виджет показываем только на групповом дашборде и только пока не знаем,
    # кто именно из участников сейчас смотрит страницу (вошли по общему паролю,
    # через виджет ещё не проходили). На личном дашборде identity уже известна
    # (owner_id сам и есть tg_user_id), виджет там не нужен.
    tg_widget_html = ""
    if owner_type == "chat" and tg_user_id == 0 and BOT_USERNAME:
        tg_widget_html = TELEGRAM_WIDGET_BANNER.format(
            bot_username=BOT_USERNAME, token=token, note=texts["tg_widget_note"], csrf=csrf,
        )

    page = TASKS_PAGE.format(
        tasks_html=tasks_html, chart_html=chart_html, heading=heading, nav_html=nav_html,
        token=token, csrf=csrf, new_task_error=new_task_error_html, tg_widget_html=tg_widget_html,
        day_picker_html=day_picker_html, view_controls_html=view_controls_html, tabbar_html=tabbar_html,
        summary_html=_summary(texts, today_n, overdue_n, streak, state["progress"]), add_open=" open" if error_key else "", more_done_html=more_done_html, nojs_html=nojs_html,
        **_dt_tasks_page(lang),
    )
    return aioweb.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})
