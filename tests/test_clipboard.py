from __future__ import annotations

import subprocess

import pytest

from fakes import FakeClipboardRunner, completed
from reader_assistant.clipboard.reader import ClipboardError, read_clipboard_text
from reader_assistant.limits import MAX_TEXT_BYTES


def _text_runner(
    text: bytes, types: bytes = b"text/plain;charset=utf-8\ntext/plain\n"
) -> FakeClipboardRunner:
    return FakeClipboardRunner(listing=completed(types), content=completed(text))


@pytest.mark.parametrize(
    "text",
    [
        "Hello, this is an English sentence.",
        "Привет, это русский текст для проверки.",
        "שלום, זהו משפט בעברית לבדיקה.",
        "Mixed: Hello мир שלום 2024!",
    ],
)
def test_reads_plain_text_unchanged_in_each_supported_script(text: str) -> None:
    runner = _text_runner(text.encode("utf-8"))

    assert read_clipboard_text(runner=runner) == text


def test_uses_wl_paste_with_no_newline_and_utf8_type() -> None:
    runner = _text_runner(b"hello")

    read_clipboard_text(runner=runner)

    assert runner.calls == [
        ["wl-paste", "--list-types"],
        ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
    ]


def test_falls_back_to_plain_text_type_when_utf8_is_not_offered() -> None:
    runner = _text_runner(b"hello", types=b"text/plain\n")

    read_clipboard_text(runner=runner)

    assert runner.calls[1][-1] == "text/plain"


def test_text_is_not_stripped_or_altered() -> None:
    text = "  first line\nsecond line\t \n"
    runner = _text_runner(text.encode("utf-8"))

    assert read_clipboard_text(runner=runner) == text


def test_empty_clipboard_when_nothing_is_offered() -> None:
    runner = FakeClipboardRunner(listing=completed(b""))

    with pytest.raises(ClipboardError, match="empty"):
        read_clipboard_text(runner=runner)


def test_empty_clipboard_when_wl_paste_reports_no_selection() -> None:
    runner = FakeClipboardRunner(listing=completed(b"", stderr=b"No selection\n", returncode=1))

    with pytest.raises(ClipboardError, match="empty"):
        read_clipboard_text(runner=runner)


def test_silent_failure_with_status_one_is_reported_as_empty() -> None:
    runner = FakeClipboardRunner(listing=completed(b"", returncode=1))

    with pytest.raises(ClipboardError, match="empty"):
        read_clipboard_text(runner=runner)


def test_whitespace_only_text_is_treated_as_empty() -> None:
    runner = _text_runner(b"  \n\t ")

    with pytest.raises(ClipboardError, match="empty"):
        read_clipboard_text(runner=runner)


def test_clipboard_without_plain_text_is_unsupported() -> None:
    runner = FakeClipboardRunner(listing=completed(b"image/png\nTARGETS\n"))

    with pytest.raises(ClipboardError, match="does not contain plain text"):
        read_clipboard_text(runner=runner)


def test_missing_wl_paste_gives_an_install_hint() -> None:
    runner = FakeClipboardRunner(listing=FileNotFoundError("wl-paste"))

    with pytest.raises(ClipboardError, match="wl-clipboard"):
        read_clipboard_text(runner=runner)


def test_nonzero_exit_reports_the_utility_message() -> None:
    runner = FakeClipboardRunner(
        listing=completed(b"", stderr=b"Could not connect to a Wayland display\n", returncode=2)
    )

    with pytest.raises(ClipboardError, match="Could not connect to a Wayland display"):
        read_clipboard_text(runner=runner)


def test_nonzero_exit_without_message_reports_the_exit_code() -> None:
    runner = FakeClipboardRunner(listing=completed(b"x", returncode=5))

    with pytest.raises(ClipboardError, match="exit code 5"):
        read_clipboard_text(runner=runner)


def test_timeout_is_reported_instead_of_waiting_forever() -> None:
    runner = FakeClipboardRunner(listing=subprocess.TimeoutExpired(cmd="wl-paste", timeout=3))

    with pytest.raises(ClipboardError, match="did not respond in time"):
        read_clipboard_text(runner=runner)


def test_invalid_utf8_is_rejected_with_a_clear_message() -> None:
    runner = _text_runner(b"\xff\xfe bad bytes")

    with pytest.raises(ClipboardError, match="not valid UTF-8"):
        read_clipboard_text(runner=runner)


def test_text_at_the_size_limit_is_accepted() -> None:
    text = "a" * MAX_TEXT_BYTES
    runner = _text_runner(text.encode("utf-8"))

    assert read_clipboard_text(runner=runner) == text


def test_oversized_clipboard_is_rejected() -> None:
    runner = _text_runner(b"a" * (MAX_TEXT_BYTES + 1))

    with pytest.raises(ClipboardError, match="too long"):
        read_clipboard_text(runner=runner)


def test_custom_executable_is_used() -> None:
    runner = _text_runner(b"hello")

    read_clipboard_text(runner=runner, executable="/opt/wl/wl-paste")

    assert runner.calls[0][0] == "/opt/wl/wl-paste"
