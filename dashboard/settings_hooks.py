"""Связь со страницей настроек: применение настроек живёт в боте
(tgbot/settings_apply.py — сброс кэша, перепланирование джоб), а импортировать
tgbot отсюда нельзя (цикл). Бот при старте отдаёт сюда свои функции."""

from types import SimpleNamespace

_hooks: SimpleNamespace | None = None


def set_settings_hooks(timezone=None, language=None, quiet=None, morning=None):
    """timezone(owner_id, owner_type, tz) -> bool, language(owner_id, owner_type, lang) -> bool,
    quiet(owner_id, owner_type, (start, end) | None), morning(user_id, (h, m) | None) — все async."""
    global _hooks
    _hooks = SimpleNamespace(timezone=timezone, language=language, quiet=quiet, morning=morning) \
        if timezone else None


def settings_hooks() -> SimpleNamespace | None:
    return _hooks
