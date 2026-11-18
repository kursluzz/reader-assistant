"""Cleaning Markdown for speech, and splitting long text without losing words."""

from __future__ import annotations

import pytest

from reader_assistant.speech_text import MAX_PART_BYTES, split_for_speech, to_speech_text


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Title\n\nPlain text.", "Title\n\nPlain text."),
        (
            "Some **bold** and *italic* and __strong__ and _emphasis_.",
            "Some bold and italic and strong and emphasis.",
        ),
        ("Use `pytest` now.", "Use pytest now."),
        ("See [the docs](https://example.org/docs) for more.", "See the docs for more."),
        ("Raw https://example.org/x link here.", "Raw link here."),
        ("![diagram](img.png) after the image.", "after the image."),
        ("- first\n- second\n1. third", "first\nsecond\nthird"),
        ("> quoted line", "quoted line"),
    ],
)
def test_inline_markdown_becomes_plain_words(markdown: str, expected: str) -> None:
    assert to_speech_text(markdown) == expected


def test_fenced_code_blocks_are_left_out_entirely() -> None:
    markdown = "Before.\n\n```python\nprint('secret')\n```\n\nAfter."

    assert to_speech_text(markdown) == "Before.\n\nAfter."


def test_tables_are_read_row_by_row_without_the_separator_line() -> None:
    markdown = "| Name | Size |\n|------|------|\n| alpha | 1 |"

    assert to_speech_text(markdown) == "Name, Size\nalpha, 1"


def test_ordinary_prose_with_asterisks_is_not_mangled() -> None:
    assert to_speech_text("Price is 3 * 4 = 12, see 2*3.") == "Price is 3 * 4 = 12, see 2*3."


def test_short_text_is_one_part() -> None:
    assert split_for_speech("Hello there.") == ["Hello there."]


@pytest.mark.parametrize(
    "text",
    [
        "Привет, мир. " * 2000,
        "שלום עולם. " * 1500,
        "A" * 12_000,
        ("Sentence number %d is here. " * 800) % tuple(range(800)),
    ],
)
def test_every_part_fits_the_service_limit_and_no_word_is_lost(text: str) -> None:
    parts = split_for_speech(text)

    assert parts
    assert all(len(part.encode("utf-8")) <= MAX_PART_BYTES for part in parts)
    assert "".join("".join(parts).split()) == "".join(text.split())


def test_sentences_in_one_paragraph_are_joined_by_a_space_not_a_pause() -> None:
    assert split_for_speech("First sentence. Second sentence.") == [
        "First sentence. Second sentence."
    ]


def test_paragraphs_are_separated_by_a_blank_line() -> None:
    assert split_for_speech("One.\n\nTwo.") == ["One.\n\nTwo."]


def test_parts_without_letters_are_dropped() -> None:
    assert split_for_speech("---\n\n***\n\nReal words here.") == ["Real words here."]


def test_long_multibyte_word_is_cut_between_characters_and_stays_valid_utf8() -> None:
    word = "ж" * 3000  # 6000 bytes, no spaces

    parts = split_for_speech(word)

    assert all(len(part.encode("utf-8")) <= MAX_PART_BYTES for part in parts)
    assert "".join(parts) == word


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        split_for_speech("text", limit=0)
