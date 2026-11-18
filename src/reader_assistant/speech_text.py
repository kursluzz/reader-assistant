"""Turn a Markdown assistant message into plain text for speech, and split it into parts.

The service accepts at most 5,000 bytes per request, counted in UTF-8. Long messages are split
here, at paragraph and sentence boundaries, so no part is cut through a word or a character.
"""

from __future__ import annotations

import re
from typing import Final

MAX_PART_BYTES: Final = 4_800  # below the service limit, with room for the join separator
_SENTENCE_END: Final = re.compile(r"(?<=[.!?…])\s+")
_TABLE_SEPARATOR: Final = re.compile(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?")
_URL: Final = re.compile(r"https?://\S+")
_IMAGE: Final = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK: Final = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_ANGLE_LINK: Final = re.compile(r"<https?://[^>]+>")
_CODE: Final = re.compile(r"`([^`]+)`")
_BOLD: Final = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_STAR: Final = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER: Final = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")


def to_speech_text(markdown: str) -> str:
    """Plain text a voice can read: no code blocks, markers, images or raw URLs.

    Fenced code blocks are left out, because reading source code aloud is rarely useful.
    Ordinary prose is never removed just because it looks like Markdown.
    """
    lines: list[str] = []
    in_code = False
    for raw in markdown.splitlines():
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            if _TABLE_SEPARATOR.fullmatch(stripped):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            lines.append(_inline(", ".join(cell for cell in cells if cell)))
            continue
        line = re.sub(r"^#{1,6}\s*", "", stripped)
        line = re.sub(r"^>\s?", "", line)
        line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line)
        lines.append(_inline(line))
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _inline(text: str) -> str:
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _ANGLE_LINK.sub("", text)
    text = _URL.sub("", text)
    text = _CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def split_for_speech(text: str, limit: int = MAX_PART_BYTES) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` UTF-8 bytes, keeping every word.

    Paragraphs stay together where they fit, and are joined with a blank line, which a voice
    reads as a pause. Sentences inside a paragraph are joined with a space. A sentence longer
    than the limit is cut at whitespace, or between characters only if it has no whitespace at
    all. Pieces without any letter are dropped, because the service would refuse them.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    parts: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        for index, unit in enumerate(_units(paragraph, limit)):
            if not _has_letter(unit):
                continue
            separator = "\n\n" if index == 0 else " "
            if current and _fits(current + separator + unit, limit):
                current = current + separator + unit
                continue
            if current:
                parts.append(current)
            current = unit
    if current:
        parts.append(current)
    return parts


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _units(paragraph: str, limit: int) -> list[str]:
    """Pieces that each fit within ``limit``: whole sentences, or cut pieces of long ones."""
    units: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if len(sentence.encode("utf-8")) <= limit:
            units.append(sentence)
        else:
            units.extend(_cut(sentence, limit))
    return units


def _cut(text: str, limit: int) -> list[str]:
    """Cut an over-long sentence at spaces, and cut any over-long word between characters."""
    pieces: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if _fits(candidate, limit):
            current = candidate
            continue
        if current:
            pieces.append(current)
        if _fits(word, limit):
            current = word
        else:
            *head, tail = _by_bytes(word, limit)
            pieces.extend(head)
            current = tail
    if current:
        pieces.append(current)
    return pieces


def _fits(text: str, limit: int) -> bool:
    return len(text.encode("utf-8")) <= limit


def _by_bytes(word: str, limit: int) -> list[str]:
    """Split one word, which has no spaces, into pieces of at most ``limit`` bytes."""
    if _fits(word, limit):
        return [word]
    pieces: list[str] = []
    current = ""
    for ch in word:
        if len((current + ch).encode("utf-8")) > limit:
            pieces.append(current)
            current = ""
        current += ch
    if current:
        pieces.append(current)
    return pieces
