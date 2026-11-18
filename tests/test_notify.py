"""Service-down notification: shown only when nothing listens, never for other errors."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from fakes import short_dir
from reader_assistant import cli
from reader_assistant.clipboard.reader import ClipboardError
from reader_assistant.ipc.client import ServiceUnavailableError
from reader_assistant.ipc.protocol import ProtocolError
from reader_assistant.notify import NOTIFY_SEND, TITLE, notify_service_not_running


@pytest.fixture
def home() -> Iterator[Path]:
    base = short_dir()
    yield base
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def _env(home: Path) -> dict[str, str]:
    runtime = home / "rt"
    runtime.mkdir(mode=0o700, exist_ok=True)
    return {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(home / "state")}


def test_notification_runs_notify_send_with_the_title_and_the_start_instructions() -> None:
    calls: list[list[str]] = []

    assert notify_service_not_running(run=lambda args: calls.append(list(args))) is True

    assert calls[0][0] == NOTIFY_SEND
    assert calls[0][2] == TITLE
    assert "reader-assistant serve" in calls[0][3]


def test_missing_notify_send_is_logged_and_does_not_raise() -> None:
    def missing(args: Sequence[str]) -> object:
        raise FileNotFoundError(NOTIFY_SEND)

    assert notify_service_not_running(run=missing) is False


def test_a_slow_notify_send_is_given_up_on() -> None:
    def slow(args: Sequence[str]) -> object:
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=2)

    assert notify_service_not_running(run=slow) is False


def test_notify_is_shown_when_nothing_listens(
    home: Path, _no_real_desktop_notifications: list[list[str]]
) -> None:
    code = cli.main(
        ["read-clipboard"],
        env=_env(home),
        home=home,
        clipboard_reader=lambda: "Good morning.",
    )

    assert code == cli.EXIT_SERVICE_NOT_RUNNING
    assert len(_no_real_desktop_notifications) == 1


def test_no_notification_for_an_unresponsive_service(
    home: Path, monkeypatch: pytest.MonkeyPatch, _no_real_desktop_notifications: list[list[str]]
) -> None:
    def no_answer(*args: object, **kwargs: object) -> object:
        raise ServiceUnavailableError("the service did not answer in time")

    monkeypatch.setattr(cli, "send_speak", no_answer)
    code = cli.main(["read-clipboard"], env=_env(home), home=home, clipboard_reader=lambda: "x")

    assert code == cli.EXIT_OPERATION_FAILED
    assert _no_real_desktop_notifications == []


def test_no_notification_for_a_protocol_error(
    home: Path, monkeypatch: pytest.MonkeyPatch, _no_real_desktop_notifications: list[list[str]]
) -> None:
    def bad_reply(*args: object, **kwargs: object) -> object:
        raise ProtocolError("bad_request", "garbage")

    monkeypatch.setattr(cli, "send_speak", bad_reply)
    code = cli.main(["read-clipboard"], env=_env(home), home=home, clipboard_reader=lambda: "x")

    assert code == cli.EXIT_OPERATION_FAILED
    assert _no_real_desktop_notifications == []


def test_no_notification_for_a_clipboard_error(
    home: Path, _no_real_desktop_notifications: list[list[str]]
) -> None:
    def empty() -> str:
        raise ClipboardError("The clipboard is empty.")

    code = cli.main(["read-clipboard"], env=_env(home), home=home, clipboard_reader=empty)

    assert code == cli.EXIT_OPERATION_FAILED
    assert _no_real_desktop_notifications == []
