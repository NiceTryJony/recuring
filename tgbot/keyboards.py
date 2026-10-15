"""Inline-клавиатуры."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from i18n import t

from .constants import _NUMBER_EMOJI, QUIET_PRESETS, REMIND_OPTIONS, REPEAT_OPTIONS, TZ_CHOICES
from .templates import builtin_templates_for_lang


def templates_kb(lang: str, custom_templates: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for tpl in builtin_templates_for_lang(lang):
        rows.append([InlineKeyboardButton(text=tpl["title"], callback_data=f"tplapply_builtin_{tpl['key']}")])
    for tpl in custom_templates:
        rows.append([
            InlineKeyboardButton(text=tpl["title"], callback_data=f"tplapply_custom_{tpl['id']}"),
            InlineKeyboardButton(text=t(lang, "btn_delete_template"), callback_data=f"tpldel_{tpl['id']}"),
        ])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_my_templates"), callback_data="tplnew")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ---------- клавиатуры ----------

def repeat_kb(lang: str, prefix="rep"):
    kb = [[InlineKeyboardButton(text=v[lang], callback_data=f"{prefix}_{k}")] for k, v in REPEAT_OPTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def remind_kb(lang: str, prefix="rem", selected: set[str] | None = None):
    selected = selected or set()
    kb = []
    for k, v in REMIND_OPTIONS.items():
        label = v[lang]
        if k in selected:
            label = f"✅ {label}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}_{k}")])
    kb.append([InlineKeyboardButton(text="✅ " + t(lang, "btn_done"), callback_data=f"{prefix}_done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def nag_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_nag_yes"), callback_data="nag_1"),
        InlineKeyboardButton(text=t(lang, "btn_nag_no"), callback_data="nag_0"),
    ]])

def quiet_kb(lang: str):
    kb = [[InlineKeyboardButton(text=p.replace("-", "–"), callback_data=f"quiet_set_{p}")] for p in QUIET_PRESETS]
    kb.append([InlineKeyboardButton(text=t(lang, "btn_quiet_off"), callback_data="quiet_off")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def task_kb(task_id: int, lang: str, done: bool = False, open_url: str | None = None, assign: bool = False):
    """open_url — кнопка Mini App сразу на эту задачу (только в личке:
    web_app-кнопки Telegram в группах запрещает). assign — кнопка «Поручить»
    (только у задач группы)."""
    if done:
        status_label = t(lang, "btn_task_undone")
        status_cb = f"undone_{task_id}"
    else:
        status_label = t(lang, "btn_task_done")
        status_cb = f"done_{task_id}"
    edit_label = t(lang, "btn_edit")
    del_label = t(lang, "btn_delete")
    sub_label = t(lang, "btn_subtasks")
    photo_label = t(lang, "btn_photo")
    kb = [
        [InlineKeyboardButton(text=status_label, callback_data=status_cb)],
        [
            InlineKeyboardButton(text=edit_label, callback_data=f"edit_{task_id}"),
            InlineKeyboardButton(text=del_label, callback_data=f"del_{task_id}"),
        ],
        # Перенос одним касанием; старые сообщения с кнопкой snooze_ (только +1ч) по-прежнему работают
        [
            InlineKeyboardButton(text=t(lang, "btn_snz_1h"), callback_data=f"snz_{task_id}_1h"),
            InlineKeyboardButton(text=t(lang, "btn_snz_tom"), callback_data=f"snz_{task_id}_tom"),
            InlineKeyboardButton(text=t(lang, "btn_snz_week"), callback_data=f"snz_{task_id}_1w"),
        ],
        [
            InlineKeyboardButton(text=sub_label, callback_data=f"subs_{task_id}"),
            InlineKeyboardButton(text=photo_label, callback_data=f"photo_{task_id}"),
        ],
    ]
    if assign:
        kb[-1].append(InlineKeyboardButton(text=t(lang, "btn_assign"), callback_data=f"assign_{task_id}"))
    if open_url:
        kb.append([InlineKeyboardButton(text=t(lang, "btn_open_app"), web_app=WebAppInfo(url=open_url))])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def assign_kb(task_id: int, members: list[tuple[int, str]], current: int | None, lang: str) -> InlineKeyboardMarkup:
    """members — [(user_id, имя)]. Текущий исполнитель помечен ✓."""
    rows = [[InlineKeyboardButton(text=("✓ " if uid == current else "") + name, callback_data=f"asg_{task_id}_{uid}")]
            for uid, name in members]
    rows.append([InlineKeyboardButton(text=("✓ " if current is None else "") + t(lang, "assign_nobody"),
                                      callback_data=f"asg_{task_id}_0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm_delete_kb(task_id: int, lang: str):
    yes = t(lang, "btn_confirm_delete")
    no = t(lang, "btn_cancel")
    kb = [[
        InlineKeyboardButton(text=yes, callback_data=f"delok_{task_id}"),
        InlineKeyboardButton(text=no, callback_data=f"delno_{task_id}"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def edit_field_kb(task_id: int, lang: str):
    labels = {
        "title": t(lang, "field_title"),
        "date": t(lang, "field_date"),
        "repeat": t(lang, "field_repeat"),
        "tag": t(lang, "field_tag"),
    }
    kb = [[InlineKeyboardButton(text=v, callback_data=f"ef_{k}_{task_id}")] for k, v in labels.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def list_open_kb(task_ids: list[int], prefix: str = "listopen"):
    """Ряд кнопок-номеров под компактным списком — тап по номеру открывает
    полную карточку конкретной задачи отдельным сообщением. prefix различает
    источник ("listopen" -> list_task_open, "findopen" -> find_task_open),
    у каждого свой кэш открытых карточек (view["open_cards"])."""
    row = [
        InlineKeyboardButton(text=_NUMBER_EMOJI[i], callback_data=f"{prefix}_{task_id}")
        for i, task_id in enumerate(task_ids)
    ]
    return row

def list_nav_kb(lang: str, filter_mode: str, sort_by: str, page: int, total_pages: int, task_ids: list[int]):
    """Клавиатура под навигационным сообщением /list: строки фильтра/сортировки,
    ряд кнопок-номеров (открыть карточку конкретной задачи), и, если страниц
    больше одной, зацикленная пагинация — ⬅️ с последней страницы уводит на
    первую и наоборот, а не упирается в край."""
    kb = list_filter_kb(lang, filter_mode, sort_by)
    rows = list(kb.inline_keyboard)
    if task_ids:
        rows.append(list_open_kb(task_ids))
    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data=f"listpage_{prev_page}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"listpage_{next_page}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def list_filter_kb(lang: str, filter_mode: str, sort_by: str):
    def label(key: str, active: bool) -> str:
        text = t(lang, key)
        return f"✅ {text}" if active else text

    row_filter = [
        InlineKeyboardButton(text=label("btn_filter_all", filter_mode == "all"), callback_data="listf_all"),
        InlineKeyboardButton(text=label("btn_filter_overdue", filter_mode == "overdue"), callback_data="listf_overdue"),
        InlineKeyboardButton(text=label("btn_filter_week", filter_mode == "this_week"), callback_data="listf_this_week"),
    ]
    row_sort = [
        InlineKeyboardButton(text=label("btn_sort_date", sort_by == "date"), callback_data="lists_date"),
        InlineKeyboardButton(text=label("btn_sort_tag", sort_by == "tag"), callback_data="lists_tag"),
        InlineKeyboardButton(text=label("btn_sort_title", sort_by == "title"), callback_data="lists_title"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row_filter, row_sort])

def tags_kb(tags: list[str]):
    kb = [[InlineKeyboardButton(text=tag, callback_data=f"tagf_{tag}")] for tag in tags]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def timezone_kb():
    kb = [[InlineKeyboardButton(text=tz, callback_data=f"tz_{tz}")] for tz in TZ_CHOICES]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def lang_kb(owner_id: int, owner_type: str):
    # owner зашит прямо в callback_data — выбор языка не завязан на отдельное
    # FSM-состояние, а это самый простой способ протащить owner через колбэк.
    def cb(code: str) -> str:
        return f"lang_{owner_type}_{owner_id}_{code}"

    kb = [
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data=cb("ru"))],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data=cb("en"))],
        [InlineKeyboardButton(text="🇵🇱 Polski", callback_data=cb("pl"))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def subtasks_kb(task_id: int, subtasks: list[dict], lang: str, confirm_id: int | None = None):
    kb = []
    for st in subtasks:
        if st["id"] == confirm_id:
            yes = t(lang, "btn_confirm_delete")
            no = t(lang, "btn_cancel")
            kb.append([
                InlineKeyboardButton(text=yes, callback_data=f"subdelok_{st['id']}"),
                InlineKeyboardButton(text=no, callback_data=f"subdelno_{st['id']}"),
            ])
            continue
        mark = "☑️" if st["done"] else "⬜"
        kb.append([
            InlineKeyboardButton(text=f"{mark} {st['title']}", callback_data=f"subtoggle_{st['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"subdel_{st['id']}"),
        ])
    add_label = t(lang, "btn_add_subtask")
    back_label = t(lang, "btn_back")
    kb.append([InlineKeyboardButton(text=add_label, callback_data=f"subadd_{task_id}")])
    kb.append([InlineKeyboardButton(text=back_label, callback_data=f"subback_{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def history_kb(lang: str, active: str = "recent"):
    def label(key: str, mode: str) -> str:
        text = t(lang, key)
        return f"✅ {text}" if mode == active else text

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label("btn_hist_recent", "recent"), callback_data="hist_recent")],
        [InlineKeyboardButton(text=label("btn_hist_week", "7"), callback_data="hist_7")],
        [InlineKeyboardButton(text=label("btn_hist_month", "30"), callback_data="hist_30")],
    ])
