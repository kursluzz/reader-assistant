"""The ``capture-last`` hook and the ``read-last`` command, as Claude Code runs them."""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import short_dir
from reader_assistant.cli import EXIT_OK, EXIT_OPERATION_FAILED, EXIT_SERVICE_NOT_RUNNING, main
from reader_assistant.last_message import handoff_dir, load_message

SESSION = "1bc70500-b920-4eca-8200-0c0aa9154aa1"


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _env(workdir: Path, session: str | None = SESSION) -> dict[str, str]:
    runtime = workdir / "rt"
    runtime.mkdir(mode=0o700, exist_ok=True)
    env = {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(workdir / "state")}
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def test_capture_last_stores_the_message_and_prints_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"hook_event_name": "Stop", "session_id": SESSION, "last_assistant_message": "Hi."}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    code = main(["capture-last"], env=_env(workdir), home=workdir / "home")

    assert code == EXIT_OK
    assert capsys.readouterr() == ("", "")
    assert load_message(handoff_dir(_env(workdir), workdir / "home"), SESSION) == "Hi."


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"hook_event_name": "Stop"}'])
def test_capture_last_never_fails_the_hook(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert main(["capture-last"], env=_env(workdir), home=workdir / "home") == EXIT_OK


def test_read_last_outside_a_session_fails_with_a_clear_message(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["read-last"], env=_env(workdir, session=None), home=workdir / "home")

    assert code == EXIT_OPERATION_FAILED
    assert "inside a Claude Code session" in capsys.readouterr().err


def test_read_last_without_the_service_exits_with_the_not_running_code(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(workdir)
    handoff_dir(env, workdir / "home").joinpath(f"{SESSION}.json").write_text(
        json.dumps({"text": "Hello", "saved_at": 1.0}), encoding="utf-8"
    )

    code = main(["read-last"], env=env, home=workdir / "home")

    assert code == EXIT_SERVICE_NOT_RUNNING
    assert "reader-assistant serve" in capsys.readouterr().err
