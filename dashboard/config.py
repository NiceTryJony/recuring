"""Константы и окружение дашборда — то, что нужно почти всем модулям пакета."""

import logging
import os
import re
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

logger = logging.getLogger("dashboard")

CHART_DAYS = 14
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    # Раньше тут был тихий фолбэк на захардкоженную строку "fallback-secret" —
    # если переменная окружения вдруг не задана, подписи cookie/CSRF/Telegram
    # Login Widget стали бы подделываемы кем угодно, кто прочитал исходники.
    # bot.py и так требует BOT_TOKEN жёстко (os.environ["BOT_TOKEN"]) и
    # импортирует этот модуль раньше той строки, так что в текущей сборке
    # процесс и без этого не доедет до старта веб-сервера — но если
    # dashboard запустят отдельно (тесты, отдельный процесс), пусть падает
    # здесь же, а не обслуживает запросы с публично известным секретом.
    raise RuntimeError("BOT_TOKEN не задан — без него SESSION_SECRET небезопасен")
# Секрет подписи cookie-сессий и CSRF. Отдельная переменная, чтобы утечка или
# перевыпуск BOT_TOKEN не затрагивали сессии (и наоборот). Без неё — старое
# поведение (токен бота), чтобы деплой без новой переменной никого не разлогинил.
# Проверка Telegram Login / initData всегда идёт на BOT_TOKEN — так требует Telegram.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or BOT_TOKEN
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # неделя

# Заголовок Server-Timing (время БД и приложения) отдаём только по явной просьбе:
# DASHBOARD_SERVER_TIMING=1. Медленные запросы логируются всегда.
SERVER_TIMING = os.environ.get("DASHBOARD_SERVER_TIMING") == "1"
SLOW_REQUEST_MS = 300

AVATAR_CACHE_SECONDS = 3600  # getFile-ссылка живёт ~1ч, чтобы не дёргать Bot API на каждый показ
# Имя бота без @ — нужно виджету Telegram Login (data-telegram-login=...).
# Домен, на котором крутится дашборд, должен быть прописан боту через
# /setdomain в BotFather, иначе виджет откажется логинить.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

# Код из db.get_or_create_task_share: 's_' + token_urlsafe(9) (12 символов).
SHARE_CODE_RE = re.compile(r"s_[A-Za-z0-9_-]{12}")

# Сколько выполненных задач показывать на главной странице за раз. Активные
# выводятся все; выполненные копятся годами, и раньше страница рендерила их
# целиком — у давнего пользователя это сотни карточек и тысячи DOM-узлов,
# которые на слабом телефоне превращали первую отрисовку в секунды.
DONE_TASKS_PAGE_SIZE = 20


def _tz_of(settings: dict | None):
    """Часовой пояс владельца с фолбэком на UTC — одна точка вместо четырёх
    одинаковых try/except по пакету. ZoneInfo кеширует объекты по имени зоны,
    так что повторные вызовы ничего не пересчитывают."""
    try:
        return ZoneInfo(settings["timezone"])
    except Exception:
        return UTC


def _acting_user_id(owner_id: int, owner_type: str, tg_user_id: int) -> int:
    """Кого писать в log_history как исполнителя действия. На личном дашборде
    (owner_type='user') это всегда владелец — там только он и может залогиниться.
    На групповом — реальный Telegram id, если известен (вход через виджет),
    иначе деградируем на owner_id (chat_id) как и раньше: это семантически
    неверно (в истории окажется id чата, а не человека), но не ломает запись."""
    if owner_type == "user":
        return owner_id
    return tg_user_id or owner_id
