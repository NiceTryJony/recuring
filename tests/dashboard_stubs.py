"""Заглушка БД для тестов веб-слоя дашборда.

Одна точка подмены db.* — ею пользуются и сквозные тесты
(tests/test_dashboard_web.py), и тест производительности
(tests/perf/test_dashboard_perf.py), чтобы набор подменяемых функций не
расходился между ними."""

from datetime import datetime
from zoneinfo import ZoneInfo

import db

# Код «Поделиться задачей», который отдаёт заглушка; указывает на первую задачу.
SHARE_CODE = "s_AAAAAAAAAAAA"


def make_spy() -> dict:
    """Счётчики обращений к БД и планировщику."""
    return {"photos_bulk": 0, "subtasks_bulk": 0, "photos_single": 0, "subtasks_single": 0,
            "scheduled": [], "unscheduled": [], "added": [], "subtasks_added": []}


def install_db_stubs(monkeypatch, token: str, settings: dict, tasks: list[dict], spy: dict | None = None,
                     photos: dict[int, list[dict]] | None = None,
                     subtasks: dict[int, list[dict]] | None = None,
                     users: dict[int, dict] | None = None,
                     thumb: bytes | None = None,
                     avatar: bytes | None = None,
                     history: list[dict] | None = None,
                     done_days: list | None = None,
                     groups: list[dict] | None = None) -> dict:
    """Подменяет db.* на in-memory реализацию. `tasks` — список, который
    возвращает get_tasks (порядок как у настоящего ORDER BY done, due_at — за
    это отвечает вызывающий). photos/subtasks — {task_id: [...]}. Возвращает spy."""
    spy = spy if spy is not None else make_spy()
    spy["tasks"] = tasks   # живой список: тесты подкладывают в него задачи
    photos = photos or {}
    subtasks = subtasks or {}
    users = users or {}

    async def _owner(tok):
        if tok == token:
            return settings
        g = next((g for g in groups or [] if g["token"] == tok), None)
        return g and {"owner_id": g["chat_id"], "owner_type": "chat", "language": "ru",
                      "timezone": "Europe/Warsaw", "dashboard_password_hash": None,
                      "dashboard_password_version": g["password_version"]}

    async def _tasks(*a, **kw):
        return tasks

    async def _task(task_id):
        return next((t for t in tasks if t["id"] == task_id), None)

    async def _dash_tasks(owner_id, owner_type, done_limit, tag=None, day_start=None, day_end=None):
        """Python-зеркало SQL db.get_dashboard_tasks (его честность проверяют интеграционные тесты)."""
        active = [t for t in tasks if not t["done"]]
        done = sorted((t for t in tasks if t["done"] and (not tag or t.get("tag") == tag)),
                      key=lambda t: t["due_at"], reverse=True)[:done_limit]
        day = [t for t in tasks if day_start is not None and day_start <= t["due_at"] < day_end]
        seen, out = set(), []
        for t in active + done + day:
            if t["id"] not in seen:
                seen.add(t["id"])
                out.append(t)
        return sorted(out, key=lambda t: (t["done"], t["due_at"]))

    async def _dash_stats(owner_id, owner_type, tz_name, tag, days_from, days_to):
        """Python-зеркало SQL db.get_dashboard_stats."""
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        local = {t["id"]: t["due_at"].astimezone(tz) for t in tasks}
        today = [t for t in tasks if local[t["id"]].date() == now.date()]
        tags: dict = {}
        for t in tasks:
            if t.get("tag") and not t["done"]:
                tags[t["tag"]] = tags.get(t["tag"], 0) + 1
        return {
            "overdue": sum(1 for t in tasks if not t["done"] and local[t["id"]] < now),
            "today": sum(1 for t in today if not t["done"] and local[t["id"]] >= now),
            "today_total": len(today), "today_done": sum(1 for t in today if t["done"]),
            "done_total": sum(1 for t in tasks if t["done"] and (not tag or t.get("tag") == tag)),
            "days": {d.date() for d in local.values() if days_from <= d.date() <= days_to},
            "tags": list(tags.items()),
        }

    async def _photos_bulk(ids):
        spy["photos_bulk"] += 1
        return {i: photos[i] for i in ids if i in photos}

    async def _subtasks_bulk(ids):
        spy["subtasks_bulk"] += 1
        return {i: subtasks[i] for i in ids if i in subtasks}

    async def _photos_single(task_id):
        spy["photos_single"] += 1
        return photos.get(task_id, [])

    async def _subtasks_single(task_id):
        spy["subtasks_single"] += 1
        return subtasks.get(task_id, [])

    async def _add_task(owner_id, owner_type, title, due_at, repeat, remind, **kw):
        spy["added"].append({"title": title, "due_at": due_at, "repeat": repeat, "owner_id": owner_id,
                             "owner_type": owner_type, "tag": kw.get("tag"), "description": kw.get("description")})
        return 42

    async def _share_code(task_id, created_by):
        return SHARE_CODE

    async def _task_by_share_code(code):
        return tasks[0] if code == SHARE_CODE and tasks else None

    async def _add_subtask(task_id, title):
        spy["subtasks_added"].append((task_id, title))
        return 1

    async def _users(ids):
        return {i: users[i] for i in ids if i in users}

    async def _user_photo(user_id):
        return (avatar, "image/jpeg") if avatar and user_id in users else None

    async def _thumb(photo_id):
        for task_id, items in photos.items():
            if any(p["id"] == photo_id for p in items):
                return thumb, task_id
        return None

    async def _photo(photo_id):
        for task_id, items in photos.items():
            if any(p["id"] == photo_id for p in items):
                return {"id": photo_id, "task_id": task_id, "data": thumb, "mime": "image/jpeg"}
        return None

    async def _history(*a, **kw):
        return list(history or [])

    async def _done_days(*a, **kw):
        return list(done_days or [])

    async def _noop(*a, **kw):
        return None

    async def _empty_list(*a, **kw):
        return []

    async def _user_token(user_id):
        return token, settings.get("dashboard_password_version", 0)

    async def _is_member(chat_id, user_id):
        return user_id in settings.get("_members", [])

    async def _morning(user_id):
        return settings.get("_morning")      # тесты кладут сюда {morning_summary_enabled, _hour, _minute}

    async def _groups(user_id, create_missing=False):
        """groups — [{chat_id, title, token, password_version, members: [user_id]}]."""
        return [g for g in (groups or []) if user_id in g.get("members", [])]

    for name, impl in {
        "get_owner_by_dashboard_token": _owner,
        "get_or_create_user_dashboard_token": _user_token,
        "get_user_group_dashboards": _groups,
        "get_morning_summary_settings": _morning,
        "is_chat_member": _is_member,
        "get_tasks": _tasks,
        "get_dashboard_tasks": _dash_tasks,
        "get_dashboard_stats": _dash_stats,
        "get_task": _task,
        "get_task_photos_meta_bulk": _photos_bulk,
        "get_subtasks_bulk": _subtasks_bulk,
        "get_task_photos_meta": _photos_single,
        "get_subtasks": _subtasks_single,
        "add_task": _add_task,
        "get_or_create_task_share": _share_code,
        "get_task_by_share_code": _task_by_share_code,
        "add_subtask": _add_subtask,
        "get_telegram_users": _users,
        "get_telegram_user_photo": _user_photo,
        "get_task_photo_thumb": _thumb,
        "get_task_photo": _photo,
        "set_task_photo_thumb": _noop,
        "get_history_stats": _history,
        "get_done_days": _done_days,
        "get_event_feed": _empty_list,
        "get_templates": _empty_list,
        "log_history": _noop,
        "mark_done": _noop,
        "mark_undone": _noop,
        "update_task": _noop,
        "delete_task": _noop,
    }.items():
        monkeypatch.setattr(db, name, impl)
    return spy
