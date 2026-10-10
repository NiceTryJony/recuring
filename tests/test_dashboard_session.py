import hashlib
import hmac
import time as time_module
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import dashboard
from dashboard import _sign_session, _verify_session, hash_password, verify_password


def _forge(payload: str) -> str:
    """Валидно подписанная cookie для произвольного payload (как умеет только сервер)."""
    sig = hmac.new(dashboard.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


@pytest.mark.parametrize("owner_id, owner_type", [(123456, "user"), (-1001234567890, "chat")])
def test_session_roundtrip(owner_id, owner_type):
    # tg_user_id не передан -> дефолт 0 ("личность неизвестна", вход по паролю без виджета)
    cookie = _sign_session(owner_id, owner_type)
    assert _verify_session(cookie) == (owner_id, owner_type, 0)


def test_session_roundtrip_with_telegram_identity():
    cookie = _sign_session(-1001234567890, "chat", tg_user_id=555)
    assert _verify_session(cookie) == (-1001234567890, "chat", 555)


def test_session_tampered_owner_rejected():
    cookie = _sign_session(1, "user")
    assert _verify_session(cookie.replace("user:1:", "user:2:", 1)) is None


def test_session_tampered_owner_type_rejected():
    cookie = _sign_session(1, "user")
    assert _verify_session(cookie.replace("user:", "chat:", 1)) is None


def test_session_tampered_tg_user_id_rejected():
    cookie = _sign_session(1, "chat", tg_user_id=555)
    assert _verify_session(cookie.replace(":555:", ":999:", 1)) is None


def test_session_tampered_signature_rejected():
    cookie = _sign_session(1, "user")
    flipped = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
    assert _verify_session(flipped) is None


def test_session_expired_rejected():
    assert _verify_session(_forge(f"user:1:0:0:{int(time_module.time()) - 10}")) is None


def test_session_not_yet_expired_accepted():
    assert _verify_session(_forge(f"user:1:0:0:{int(time_module.time()) + 60}")) == (1, "user", 0)


def test_session_signed_with_other_secret_rejected():
    payload = f"user:1:0:{int(time_module.time()) + 60}"
    bad = hmac.new(b"other-secret", payload.encode(), hashlib.sha256).hexdigest()
    assert _verify_session(f"{payload}:{bad}") is None


@pytest.mark.parametrize("garbage", ["", "abc", "a:b:c", "a:b:c:d:e:f", "user:x:0:y:z", "user:1:0:notint:sig", ":::::"])
def test_session_garbage_rejected_without_exception(garbage):
    assert _verify_session(garbage) is None


# ---------- пароли ----------

def test_password_roundtrip_and_format():
    h = hash_password("secret1")
    assert h.startswith("pbkdf2$")
    assert verify_password("secret1", h)
    assert not verify_password("secret2", h)


def test_password_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_legacy_sha256_hash_still_verifies():
    legacy = hashlib.sha256("oldpass".encode()).hexdigest()
    assert verify_password("oldpass", legacy)
    assert not verify_password("wrong", legacy)


@pytest.mark.parametrize("stored", ["", None, "pbkdf2$broken", "pbkdf2$1$zz$zz"])
def test_verify_password_bad_stored_values(stored):
    assert verify_password("x", stored) is False


# ---------- график ----------

TEXTS = dashboard._dt("ru")


def test_render_chart_heights_and_streak():
    today = date(2026, 10, 4)
    series = [(today - timedelta(days=i), c) for i, c in zip(range(3, -1, -1), [0, 1, 2, 4])]
    html = dashboard._render_chart(series, streak=3, texts=TEXTS)
    assert html.count('class="col"') == 4
    assert "height:90px" in html            # максимум = 90px
    assert "height:2px" in html             # нулевой день — минимальная «полоска»
    assert "Серия: 3" in html
    assert "bar zero" in html


def test_render_chart_all_zero_and_no_streak():
    today = date(2026, 10, 4)
    html = dashboard._render_chart([(today, 0)], streak=0, texts=TEXTS)
    assert "streak" not in html.split('class="bars"')[0]
    assert "height:2px" in html


def test_render_task_escapes_html_and_repeating_not_overdue():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    utc = ZoneInfo("UTC")
    task = {"id": 1, "title": "<script>x</script>", "tag": "a&b", "done": False, "repeat": "daily",
            "due_at": datetime(2020, 1, 1, tzinfo=utc)}
    html = dashboard._render_task(task, utc, "ru", "tok", "csrf-value")
    assert "<script>" not in html and "&lt;script&gt;" in html and "a&amp;b" in html
    assert "overdue" not in html            # регресс: повторяющиеся не «просрочены» вечно
    task["repeat"] = "none"
    assert "overdue" in dashboard._render_task(task, utc, "ru", "tok", "csrf-value")


# ---------- определение IP клиента ----------
# Регресс: раньше брался ПЕРВЫЙ элемент X-Forwarded-For, т.е. значение,
# которое клиент может прислать сам — rate-limit на вход обходился подстановкой
# случайного IP в каждом запросе.

def _req(xff=None, remote="10.0.0.1"):
    return SimpleNamespace(headers={"X-Forwarded-For": xff} if xff else {}, remote=remote)


def test_client_ip_without_forwarded_header():
    assert dashboard._client_ip(_req()) == "10.0.0.1"


def test_client_ip_ignores_client_supplied_prefix():
    # "1.1.1.1" прислал сам клиент, "203.0.113.9" дописал наш прокси
    assert dashboard._client_ip(_req("1.1.1.1, 203.0.113.9")) == "203.0.113.9"


def test_client_ip_single_entry_is_real_client():
    assert dashboard._client_ip(_req("203.0.113.9")) == "203.0.113.9"


def test_client_ip_forged_chain_does_not_change_key():
    """Главное свойство: что бы клиент ни подставил слева, ключ rate-limit один."""
    ips = {
        dashboard._client_ip(_req(f"{i}.{i}.{i}.{i}, 203.0.113.9"))
        for i in range(1, 6)
    }
    assert ips == {"203.0.113.9"}


def test_client_ip_falls_back_when_chain_shorter_than_expected():
    assert dashboard._client_ip(_req(remote=None)) == "unknown"


# ---------- rate-limit входа ----------

@pytest.fixture(autouse=True)
def _clean_login_attempts():
    dashboard._LOGIN_ATTEMPTS.clear()
    dashboard._last_login_prune = 0.0
    yield
    dashboard._LOGIN_ATTEMPTS.clear()


def test_login_rate_limit_triggers_after_max_attempts():
    for _ in range(dashboard._LOGIN_MAX_ATTEMPTS):
        assert dashboard._login_rate_limited("tok", "ip") is False
        dashboard._record_failed_login("tok", "ip")
    assert dashboard._login_rate_limited("tok", "ip") is True


def test_login_rate_limit_check_does_not_leak_keys():
    """Регресс: проверка лимита сама создавала вечную запись в словаре."""
    for i in range(50):
        dashboard._login_rate_limited("tok", f"ip{i}")
    assert dashboard._LOGIN_ATTEMPTS == {}


def test_login_attempts_pruned_after_window():
    old = time_module.time() - max(dashboard._LOGIN_WINDOW_SECONDS,
                                   dashboard._LOGIN_LOCKOUT_SECONDS) - 1
    dashboard._LOGIN_ATTEMPTS[("tok", "ip")] = [old]
    dashboard._last_login_prune = 0.0
    dashboard._login_rate_limited("other", "ip")  # любой вызов запускает уборку
    assert ("tok", "ip") not in dashboard._LOGIN_ATTEMPTS


def test_successful_login_clears_attempts():
    dashboard._record_failed_login("tok", "ip")
    dashboard._clear_login_attempts("tok", "ip")
    assert dashboard._LOGIN_ATTEMPTS == {}


# ---------- страница входа ----------

def test_login_form_posts_to_dashboard_root():
    """Регресс: у формы не было action, и POST с /history или /templates уходил
    на URL без POST-маршрута — вместо входа пользователь получал 405."""
    resp = dashboard._login_response("tok123", "ru")
    assert 'action="/dashboard/tok123"' in resp.text
    assert resp.headers["Cache-Control"] == "no-store"


def test_login_response_carries_error_and_status():
    resp = dashboard._login_response("tok", "en", "<div>boom</div>", status=401)
    assert resp.status == 401
    assert "<div>boom</div>" in resp.text
    assert "Password" in resp.text  # язык взят из аргумента


def test_login_response_unknown_language_falls_back_to_ru():
    assert "Пароль" in dashboard._login_response("tok", "xx").text


# ---------- разбор due_at ----------

def test_parse_due_at_naive_is_owner_local():
    tz = ZoneInfo("Europe/Warsaw")
    parsed = dashboard._parse_due_at("2026-10-05T14:30", tz)
    assert parsed.hour == 14 and parsed.minute == 30
    assert parsed.tzinfo is tz


def test_parse_due_at_respects_explicit_offset():
    tz = ZoneInfo("Europe/Warsaw")  # UTC+2 в октябре
    parsed = dashboard._parse_due_at("2026-10-05T12:30+00:00", tz)
    assert parsed.hour == 14  # переведено в зону владельца, а не затёрто


def test_parse_due_at_empty_means_now():
    tz = ZoneInfo("UTC")
    parsed = dashboard._parse_due_at("", tz)
    assert abs((parsed - datetime.now(tz)).total_seconds()) < 5


@pytest.mark.parametrize("garbage", ["не дата", "2026-13-45T99:99", "T12:00", "05.10.2026 14:30"])
def test_parse_due_at_garbage_returns_none(garbage):
    """Регресс: раньше битое значение молча превращалось в "сейчас"."""
    assert dashboard._parse_due_at(garbage, ZoneInfo("UTC")) is None


# ---------- языковые фолбэки ----------

@pytest.mark.parametrize("lang", ["ru", "en", "pl", "xx", ""])
def test_month_and_dow_names_never_raise(lang):
    assert len(dashboard._months(lang)) == 12
    assert len(dashboard._dows(lang)) == 7


def test_day_picker_renders_for_unknown_language():
    """Регресс: _MONTH_NAMES[lang] давал KeyError и 500 на главной странице."""
    today = date(2026, 10, 11)
    html = dashboard._render_day_picker(today, today, "xx", "tok", {today})
    assert "day-picker" in html and "2026" in html


# ---------- хуки планировщика ----------

def test_scheduler_hooks_are_optional():
    """Без set_scheduler_hooks (тесты, отдельный веб-процесс) — no-op, не падаем."""
    dashboard.set_scheduler_hooks(None, None)
    dashboard._schedule_task(1, 2, "user", datetime.now(ZoneInfo("UTC")), "none", "on_time", ZoneInfo("UTC"))
    dashboard._unschedule_task(1)


def test_scheduler_hooks_receive_utc_and_survive_errors():
    calls = []
    dashboard.set_scheduler_hooks(
        schedule=lambda *a, **kw: calls.append((a, kw)),
        unschedule=lambda task_id: calls.append(("unschedule", task_id)),
    )
    try:
        tz = ZoneInfo("Europe/Warsaw")
        due_local = datetime(2026, 10, 5, 14, 30, tzinfo=tz)
        dashboard._schedule_task(7, 42, "user", due_local, "none", "on_time", tz, remind_until_done=True)
        dashboard._unschedule_task(7)
        (args, kwargs), unschedule_call = calls
        assert args[0] == 7 and args[3].tzinfo is dashboard.UTC
        assert args[3] == due_local  # тот же момент времени, просто в UTC
        assert kwargs == {"remind_until_done": True}
        assert unschedule_call == ("unschedule", 7)

        # Исключение в хуке не должно ронять уже выполненное действие в БД
        dashboard.set_scheduler_hooks(
            schedule=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
            unschedule=lambda task_id: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        dashboard._schedule_task(7, 42, "user", due_local, "none", "on_time", tz)
        dashboard._unschedule_task(7)
    finally:
        dashboard.set_scheduler_hooks(None, None)
