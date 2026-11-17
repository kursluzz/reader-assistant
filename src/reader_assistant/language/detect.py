"""Deterministic, script-based language identification for English, Russian and Hebrew.

Each letter is assigned to the script it belongs to: Hebrew, Cyrillic or Latin.
The language with the most letters wins. Ties go to the configured default
language if it is among the tied languages, then to English, then to the first
in alphabetical order.

Limits (see docs/CLIPBOARD.md): the method sees only the script, not the words.
English and any other Latin-script language are both read as English. Text in an
unsupported script is ignored when counting, so mostly unsupported text may be read
in the language of its few supported letters.
"""

from __future__ import annotations

from collections import Counter
from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "he", "ru")
FALLBACK_LANGUAGE: Final = "en"


class UnsupportedTextError(ValueError):
    """The text contains no letters in a supported script."""


def script_language(char: str) -> str | None:
    """Return the language whose script ``char`` belongs to, or None."""
    if not char.isalpha():
        return None
    code = ord(char)
    if 0x0590 <= code <= 0x05FF or 0xFB1D <= code <= 0xFB4F:
        return "he"
    if 0x0400 <= code <= 0x052F:
        return "ru"
    if char.isascii() or 0x00C0 <= code <= 0x024F:
        return "en"
    return None


def count_scripts(text: str) -> Counter[str]:
    """Count letters per supported language."""
    counts: Counter[str] = Counter()
    for char in text:
        language = script_language(char)
        if language is not None:
            counts[language] += 1
    return counts


def detect_language(text: str, *, default: str = FALLBACK_LANGUAGE) -> str:
    """Return ``en``, ``ru`` or ``he`` for ``text``.

    Raises:
        UnsupportedTextError: if the text has no letters in a supported script.
    """
    counts = count_scripts(text)
    if not counts:
        raise UnsupportedTextError(
            "The text has no English, Russian or Hebrew letters, so it cannot be read."
        )
    best = max(counts.values())
    tied = sorted(language for language, count in counts.items() if count == best)
    preferred = default if default in SUPPORTED_LANGUAGES else FALLBACK_LANGUAGE
    if preferred in tied:
        return preferred
    if FALLBACK_LANGUAGE in tied:
        return FALLBACK_LANGUAGE
    return tied[0]
