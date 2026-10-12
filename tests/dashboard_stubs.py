"""Заглушка БД для тестов веб-слоя дашборда.

Одна точка подмены db.* — ею пользуются и сквозные тесты
(tests/test_dashboard_web.py), и тест производительности
(tests/perf/test_dashboard_perf.py), чтобы набор подменяемых функций не
расходился между ними."""

import db


def make_spy() -> dict:
    """Счётчики обращений к БД и планировщику."""
    return {"photos_bulk": 0, "subtasks_bulk": 0, "photos_single": 0, "subtasks_single": 0,
            "scheduled": [], "unscheduled": [], "added": []}


def install_db_stubs(monkeypatch, token: str, settings: dict, tasks: list[dict], spy: dict | None = None,
                     photos: dict[int, list[dict]] | None = None,
                     subtasks: dict[int, list[dict]] | None = None,
                     users: dict[int, dict] | None = None,
                     thumb: bytes | None = None,
                     avatar: bytes | None = None,
                     history: list[dict] | None = None,
                     done_days: list | None = None) -> dict:
    """Подменяет db.* на in-memory реализацию. `tasks` — список, который
    возвращает get_tasks (порядок как у настоящего ORDER BY done, due_at — за
    это отвечает вызывающий). photos/subtasks — {task_id: [...]}. Возвращает spy."""
    spy = spy if spy is not None else make_spy()
    photos = photos or {}
    subtasks = subtasks or {}
    users = users or {}

    async def _owner(tok):
        return settings if tok == token else None

    async def _tasks(*a, **kw):
        return tasks

    async def _task(task_id):
        return next((t for t in tasks if t["id"] == task_id), None)

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
        spy["added"].append({"title": title, "due_at": due_at, "repeat": repeat})
        return 42

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

    for name, impl in {
        "get_owner_by_dashboard_token": _owner,
        "get_tasks": _tasks,
        "get_task": _task,
        "get_task_photos_meta_bulk": _photos_bulk,
        "get_subtasks_bulk": _subtasks_bulk,
        "get_task_photos_meta": _photos_single,
        "get_subtasks": _subtasks_single,
        "add_task": _add_task,
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
