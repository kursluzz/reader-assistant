from __future__ import annotations

import pytest

from reader_assistant.language.detect import (
    UnsupportedTextError,
    count_scripts,
    detect_language,
    script_language,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The quick brown fox jumps over the lazy dog.", "en"),
        ("Это короткое предложение на русском языке.", "ru"),
        ("זהו משפט קצר בעברית.", "he"),
        ("Café résumé naïve façade", "en"),
        ("Ёж и медведь", "ru"),
        ("שָׁלוֹם עוֹלָם", "he"),  # vowel marks are not letters but the letters still count
    ],
)
def test_detects_each_supported_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_dominant_script_wins_in_mixed_text() -> None:
    assert detect_language("שלום לכולם, this") == "he"  # 10 Hebrew letters, 4 Latin
    assert detect_language("שלום לכולם, this is a test") == "en"  # 10 Hebrew, 11 Latin
    assert detect_language("Hello and welcome to the test, всем привет") == "en"
    assert detect_language("Привет, это Hello") == "ru"


def test_tie_goes_to_the_configured_default_when_it_is_tied() -> None:
    # Two English letters and two Russian letters: a tie.
    assert detect_language("ab вв", default="ru") == "ru"
    assert detect_language("ab вв", default="en") == "en"


def test_tie_without_the_default_goes_to_english_when_tied() -> None:
    assert detect_language("ab вв", default="he") == "en"


def test_tie_without_english_or_default_goes_alphabetically() -> None:
    # Hebrew and Russian tied; English is not involved, so "he" sorts before "ru".
    assert detect_language("шш שש", default="en") == "he"
    assert detect_language("шш שש", default="ru") == "ru"


def test_unsupported_default_falls_back_to_english_for_ties() -> None:
    assert detect_language("ab вв", default="fr") == "en"


def test_digits_and_punctuation_have_no_supported_letters() -> None:
    with pytest.raises(UnsupportedTextError, match="no English, Russian or Hebrew letters"):
        detect_language("2024-05-17 !!! (12:30)")


def test_empty_text_is_unsupported() -> None:
    with pytest.raises(UnsupportedTextError):
        detect_language("")


def test_chinese_only_text_is_unsupported() -> None:
    with pytest.raises(UnsupportedTextError):
        detect_language("你好世界")


def test_emoji_is_ignored_when_counting() -> None:
    assert detect_language("🙂🙂🙂 Hello") == "en"


def test_script_language_classifies_representative_characters() -> None:
    assert script_language("a") == "en"
    assert script_language("é") == "en"
    assert script_language("Ж") == "ru"
    assert script_language("ש") == "he"
    assert script_language("1") is None
    assert script_language("\u00d7") is None  # multiplication sign is not a letter
    assert script_language("中") is None


def test_count_scripts_counts_letters_only() -> None:
    counts = count_scripts("Hi, мир! שלום 42")

    assert counts == {"en": 2, "ru": 3, "he": 4}
