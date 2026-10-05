import re

import pytest

from i18n import TEXTS, t

LANGS = list(TEXTS)
PLACEHOLDER = re.compile(r"{(\w+)}")


def test_all_languages_have_same_keys():
    base = set(TEXTS["ru"])
    for lang in LANGS:
        assert set(TEXTS[lang]) == base, f"{lang}: расхождение ключей {base ^ set(TEXTS[lang])}"


@pytest.mark.parametrize("key", sorted(TEXTS["ru"]))
def test_placeholders_match_across_languages(key):
    expected = set(PLACEHOLDER.findall(TEXTS["ru"][key]))
    for lang in LANGS:
        assert set(PLACEHOLDER.findall(TEXTS[lang][key])) == expected, f"{lang}.{key}"


def test_t_falls_back_to_russian_and_to_key():
    assert t("xx", "cancelled") == TEXTS["ru"]["cancelled"]
    assert t("en", "no_such_key") == "no_such_key"


def test_t_formats_kwargs():
    assert "Tag" in t("en", "task_added", title="Tag", date="01.01.2026")


def test_welcome_mentions_quiet_in_all_languages():
    assert all("/quiet" in TEXTS[lang]["welcome"] for lang in LANGS)


def test_weekdays_short_has_seven_entries():
    assert all(len(TEXTS[lang]["weekdays_short"].split(",")) == 7 for lang in LANGS)
