import hashlib
import hmac
import time as time_module
from datetime import date, timedelta

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
    cookie = _sign_session(owner_id, owner_type, "token")
    assert _verify_session(cookie) == (owner_id, owner_type, 0)


def test_session_roundtrip_with_telegram_identity():
    cookie = _sign_session(-1001234567890, "chat", "token", tg_user_id=555)
    assert _verify_session(cookie) == (-1001234567890, "chat", 555)


def test_session_tampered_owner_rejected():
    cookie = _sign_session(1, "user", "t")
    assert _verify_session(cookie.replace("user:1:", "user:2:", 1)) is None


def test_session_tampered_owner_type_rejected():
    cookie = _sign_session(1, "user", "t")
    assert _verify_session(cookie.replace("user:", "chat:", 1)) is None


def test_session_tampered_tg_user_id_rejected():
    cookie = _sign_session(1, "chat", "t", tg_user_id=555)
    assert _verify_session(cookie.replace(":555:", ":999:", 1)) is None


def test_session_tampered_signature_rejected():
    cookie = _sign_session(1, "user", "t")
    flipped = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
    assert _verify_session(flipped) is None


def test_session_expired_rejected():
    assert _verify_session(_forge(f"user:1:0:{int(time_module.time()) - 10}")) is None


def test_session_not_yet_expired_accepted():
    assert _verify_session(_forge(f"user:1:0:{int(time_module.time()) + 60}")) == (1, "user", 0)


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
    task = {"title": "<script>x</script>", "tag": "a&b", "done": False, "repeat": "daily",
            "due_at": datetime(2020, 1, 1, tzinfo=utc)}
    html = dashboard._render_task(task, utc, "ru", "tok", "csrf-value")
    assert "<script>" not in html and "&lt;script&gt;" in html and "a&amp;b" in html
    assert "overdue" not in html            # регресс: повторяющиеся не «просрочены» вечно
    task["repeat"] = "none"
    assert "overdue" in dashboard._render_task(task, utc, "ru", "tok", "csrf-value")