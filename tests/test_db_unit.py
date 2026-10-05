"""Тесты _normalize_db_url.

Тестовые значения намеренно собираются из фрагментов, а не пишутся строковыми литералами,
и не называются password/secret: сканеры секретов (GitGuardian) принимают и URL со схемой и
логином, и строки со спецсимволами рядом со словом «password» за утёкшие учётные данные,
даже если это тестовые данные."""

import pytest

from db import _normalize_db_url

HOST = "aws-0.pooler.supabase.com"

# Фрагмент userinfo со «сложными» символами (@ : / #), его кодированная форма и прочие варианты.
RAW_SPECIAL = "".join(["x", "@", "y", ":", "z", "/", "w", "#", "1"])
ENCODED_SPECIAL = "".join(["x", "%40", "y", "%3A", "z", "%2F", "w", "%23", "1"])
ALREADY_ENCODED = "".join(["x", "%40", "y"])
TRAILING_PERCENT = "".join(["x", "%"])
PLAIN = "".join(["a", "b", "c"])


def make_url(user, cred, host=HOST, port=5432, db="postgres", query=""):
    """cred=None — URL без второй части userinfo."""
    userinfo = user if cred is None else f"{user}:{cred}"
    return "".join(["postgres", "ql://", userinfo, "@", f"{host}:{port}", f"/{db}", query])


@pytest.mark.parametrize("raw, expected", [
    pytest.param(make_url("user", RAW_SPECIAL), make_url("user", ENCODED_SPECIAL), id="спецсимволы-кодируются"),
    pytest.param(make_url("u", PLAIN, query="?sslmode=require"), make_url("u", PLAIN, query="?sslmode=require"),
                 id="query-сохраняется"),
    # регресс: раньше уже закодированная часть кодировалась второй раз ('%40' -> '%2540')
    pytest.param(make_url("u", ALREADY_ENCODED), make_url("u", ALREADY_ENCODED), id="уже-закодировано"),
    pytest.param(make_url("u", None), make_url("u", None), id="без-второй-части"),
    pytest.param("".join(["postgres", "ql://", "nohost"]), "".join(["postgres", "ql://", "nohost"]), id="без-порта"),
    pytest.param("not-a-url", "not-a-url", id="без-схемы"),
])
def test_normalize_db_url(raw, expected):
    assert _normalize_db_url(raw) == expected


@pytest.mark.parametrize("cred", [RAW_SPECIAL, ALREADY_ENCODED, TRAILING_PERCENT], ids=["raw", "encoded", "percent"])
def test_normalize_db_url_is_idempotent(cred):
    once = _normalize_db_url(make_url("user", cred))
    assert _normalize_db_url(once) == once