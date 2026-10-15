"""Выбор, фильтрация и группировка задач для дашборда. Чистая логика без БД и
HTTP: дашборд передаёт список задач и их due_at в часовом поясе владельца."""

from datetime import date

VIEWS = ("all", "today", "upcoming", "done")
GROUPS = ("none", "tag", "date")


def select_tasks(all_tasks: list[dict], local_due: dict, *, view: str, today: date,
                 selected_date: date, filter_active: bool, done_limit: int,
                 tag: str | None = None, hide_done: bool = False) -> tuple[list[dict], int]:
    """Какие задачи рисовать и сколько выполненных осталось за кадром.

    Панель дней (?date=) — ФИЛЬТР и главнее вкладки: показывает весь выбранный
    день целиком, без лимитов. Иначе действует вкладка view: all — все активные
    + последние выполненные (как было до вкладок, ссылка без параметров не
    меняет поведения), today — активные на сегодня и просроченные, upcoming —
    активные позже сегодня, done — только выполненные. Выполненные режутся до
    done_limit самых свежих: они копятся годами, и каждая карточка — цена
    первой отрисовки на слабом телефоне. tag сужает любой вариант. hide_done —
    во вкладке all не показывать выполненные вовсе («без группировки»)."""
    if tag:
        all_tasks = [t for t in all_tasks if t.get("tag") == tag]
    if filter_active:
        return [t for t in all_tasks if local_due[t["id"]].date() == selected_date], 0
    active = [t for t in all_tasks if not t["done"]]
    done = sorted((t for t in all_tasks if t["done"]), key=lambda t: t["due_at"], reverse=True)
    if view == "today":
        return [t for t in active if local_due[t["id"]].date() <= today], 0
    if view == "upcoming":
        return [t for t in active if local_due[t["id"]].date() > today], 0
    if view == "done":
        return done[:done_limit], max(0, len(done) - done_limit)
    if hide_done:
        return active, 0
    return active + done[:done_limit], max(0, len(done) - done_limit)


def group_tasks(tasks: list[dict], local_due: dict, mode: str) -> list[tuple[object, list[dict]]]:
    """[(ключ группы, задачи)]. none — одна группа с ключом None; tag — по
    алфавиту, «без тега» (ключ "") последней; date — в порядке появления
    (вход уже отсортирован по due_at), ключ — date."""
    if mode == "tag":
        groups: dict[str, list[dict]] = {}
        for t in tasks:
            groups.setdefault(t.get("tag") or "", []).append(t)
        return sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0].casefold()))
    if mode == "date":
        by_day: dict[date, list[dict]] = {}
        for t in tasks:
            by_day.setdefault(local_due[t["id"]].date(), []).append(t)
        return list(by_day.items())
    return [(None, tasks)] if tasks else []


def tag_counts(all_tasks: list[dict]) -> list[tuple[str, int]]:
    """Теги активных задач со счётчиками, по алфавиту — для чипов-фильтров."""
    counts: dict[str, int] = {}
    for t in all_tasks:
        if t.get("tag") and not t["done"]:
            counts[t["tag"]] = counts.get(t["tag"], 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].casefold())


def day_counts(all_tasks: list[dict], local_due: dict, now) -> tuple[int, int]:
    """(активных на сегодня, просроченных) — для сводки. now — aware datetime
    в зоне владельца, той же, что у local_due."""
    today = overdue = 0
    for t in all_tasks:
        if t["done"]:
            continue
        due = local_due[t["id"]]
        if due < now:
            overdue += 1
        elif due.date() == now.date():
            today += 1
    return today, overdue


def today_progress(all_tasks: list[dict], local_due: dict, today: date) -> int | None:
    """Доля выполненных среди задач на сегодня, 0..100; None, если задач на
    сегодня нет вовсе (кольцо прогресса в сводке тогда не рисуется)."""
    todays = [t for t in all_tasks if local_due[t["id"]].date() == today]
    if not todays:
        return None
    return round(100 * sum(1 for t in todays if t["done"]) / len(todays))
