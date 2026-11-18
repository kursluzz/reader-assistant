"""Read the last assistant message of a Claude Code session, through the real service."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeEngine, FakeSink, short_dir, wait_for
from reader_assistant.ipc.client import ServiceError, ServiceUnavailableError
from reader_assistant.last_message import (
    MessageError,
    _wait_for_outcome,
    capture_from_hook,
    capture_payload,
    handoff_dir,
    load_message,
    read_last,
    store_message,
)
from service_harness import live_service

SESSION = "6db4590f-2618-4fb9-aba8-9c91a27187e8"


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


def _stop_payload(text: str, session: str = SESSION) -> dict[str, Any]:
    return {
        "hook_event_name": "Stop",
        "session_id": session,
        "last_assistant_message": text,
    }


# --- capture -----------------------------------------------------------------------


def test_stop_hook_stores_the_last_assistant_message_privately(workdir: Path) -> None:
    env = _env(workdir)
    directory = handoff_dir(env, workdir / "home")

    assert capture_from_hook(_stop_payload("Hello from Claude."), directory, now=1000.0)

    stored = directory / f"{SESSION}.json"
    assert load_message(directory, SESSION) == "Hello from Claude."
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_hook_ignores_other_events_and_empty_or_bad_payloads(workdir: Path) -> None:
    directory = handoff_dir(_env(workdir), workdir / "home")

    assert not capture_from_hook({"hook_event_name": "PreToolUse"}, directory, now=1.0)
    assert not capture_from_hook(_stop_payload("   "), directory, now=1.0)
    assert not capture_from_hook(
        {"hook_event_name": "Stop", "session_id": "../evil"}, directory, now=1.0
    )
    assert list(directory.iterdir()) == []


def test_capture_payload_never_raises_on_garbage(workdir: Path) -> None:
    env = _env(workdir)

    assert capture_payload("not json", env, workdir / "home") is False
    assert capture_payload("[1, 2]", env, workdir / "home") is False


def test_each_session_keeps_its_own_message(workdir: Path) -> None:
    directory = handoff_dir(_env(workdir), workdir / "home")
    capture_from_hook(_stop_payload("first session"), directory, now=1.0)
    capture_from_hook(_stop_payload("second session", session="other-session"), directory, now=1.0)

    assert load_message(directory, SESSION) == "first session"
    assert load_message(directory, "other-session") == "second session"


def test_old_messages_are_removed_after_a_day(workdir: Path) -> None:
    directory = handoff_dir(_env(workdir), workdir / "home")
    store_message(directory, "old-session", "stale", now=0.0)
    old = directory / "old-session.json"
    os.utime(old, (0, 0))

    capture_from_hook(_stop_payload("fresh"), directory, now=200_000.0)

    assert not old.exists()


# --- read-last ---------------------------------------------------------------------


def test_read_last_speaks_the_stored_message_through_the_service(workdir: Path) -> None:
    env = _env(workdir)
    engine = FakeEngine("kokoro", "en", "af_heart", samples=2400)
    sink = FakeSink()
    message = "Good morning. This is the answer Claude gave."
    directory = handoff_dir(env, workdir / "home")
    capture_from_hook(_stop_payload(message), directory, now=1.0)

    with live_service(
        workdir / "rt" / "reader-assistant" / "service.sock", engines=[engine], sink=sink
    ):
        result = read_last(env, workdir / "home", clock=lambda: 2.0)

    assert result.parts == 1
    assert result.stopped_early is False
    assert [text.split() for text, _ in engine.synthesized] == [message.split()]


def test_read_last_sends_long_messages_as_ordered_parts(workdir: Path) -> None:
    env = _env(workdir)
    engine = FakeEngine("kokoro", "en", "af_heart", samples=2400)
    long_text = " ".join(f"Sentence {number} is here." for number in range(900))
    directory = handoff_dir(env, workdir / "home")
    capture_from_hook(_stop_payload(long_text), directory, now=1.0)

    with live_service(workdir / "rt" / "reader-assistant" / "service.sock", engines=[engine]):
        result = read_last(env, workdir / "home", clock=lambda: 2.0)

    spoken = [text for text, _ in engine.synthesized]
    assert result.parts == len(spoken) > 1
    assert " ".join(" ".join(spoken).split()) == " ".join(long_text.split())


def test_the_reply_to_read_last_does_not_replace_the_message(workdir: Path) -> None:
    env = _env(workdir)
    directory = handoff_dir(env, workdir / "home")
    capture_from_hook(_stop_payload("the real answer"), directory, now=1.0)
    with live_service(workdir / "rt" / "reader-assistant" / "service.sock") as live:
        read_last(env, workdir / "home", clock=lambda: 10.0)
        wait_for(lambda: live.service.status().state.value == "idle")

    # Claude's reply to the command itself is the next Stop: it must be skipped once.
    assert not capture_from_hook(_stop_payload("Done."), directory, now=11.0)
    assert load_message(directory, SESSION) == "the real answer"
    # After that, the next real answer is stored as usual.
    assert capture_from_hook(_stop_payload("the next answer"), directory, now=12.0)


def test_a_stale_skip_marker_does_not_hide_a_real_answer(workdir: Path) -> None:
    directory = handoff_dir(_env(workdir), workdir / "home")
    store_message(directory, SESSION, "old", now=1.0)
    (directory / f"{SESSION}.skip").write_text("0", encoding="utf-8")

    assert capture_from_hook(_stop_payload("new answer"), directory, now=10_000.0)


@pytest.mark.parametrize(
    ("session", "stored", "message"),
    [
        (None, None, "must run inside a Claude Code session"),
        (SESSION, None, "no assistant message saved"),
    ],
)
def test_read_last_explains_what_is_missing(
    workdir: Path, session: str | None, stored: str | None, message: str
) -> None:
    env = _env(workdir, session)
    if stored is not None:
        store_message(handoff_dir(env, workdir / "home"), SESSION, stored, now=1.0)

    with pytest.raises(MessageError, match=message):
        read_last(env, workdir / "home")


def test_read_last_with_nothing_to_read_says_so(workdir: Path) -> None:
    env = _env(workdir)
    store_message(handoff_dir(env, workdir / "home"), SESSION, "```\nonly code\n```", now=1.0)

    with pytest.raises(MessageError, match="nothing to read aloud"):
        read_last(env, workdir / "home")


def test_read_last_without_the_service_reports_it_is_not_running(workdir: Path) -> None:
    env = _env(workdir)
    store_message(handoff_dir(env, workdir / "home"), SESSION, "Hello", now=1.0)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        read_last(env, workdir / "home")

    assert excinfo.value.not_running is True


# --- how one part ends -------------------------------------------------------------


class _ScriptedEvents:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)

    def read(self, timeout: float) -> dict[str, Any] | None:
        return self._messages.pop(0) if self._messages else None


def _event(request_id: int, kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": "event", "event": {"request_id": request_id, "kind": kind, **fields}}


def test_a_part_finishes_on_its_own_finished_event() -> None:
    events = _ScriptedEvents([_event(7, "playback_started"), _event(7, "finished")])

    assert _wait_for_outcome(events, 7) == "finished"


def test_events_of_other_requests_are_ignored() -> None:
    events = _ScriptedEvents([_event(3, "finished"), _event(7, "finished")])

    assert _wait_for_outcome(events, 7) == "finished"


def test_another_request_taking_over_stops_the_reading() -> None:
    events = _ScriptedEvents([_event(7, "cancelled", reason="replaced")])

    assert _wait_for_outcome(events, 7) == "stopped"


def test_an_engine_error_is_reported_to_the_caller() -> None:
    events = _ScriptedEvents([_event(7, "error", code="engine_failed", message="no model")])

    with pytest.raises(ServiceError) as excinfo:
        _wait_for_outcome(events, 7)

    assert excinfo.value.code == "engine_failed"


def test_the_service_stopping_during_a_part_is_an_error() -> None:
    events = _ScriptedEvents([{"type": "closed", "reason": "shutdown"}])

    with pytest.raises(ServiceUnavailableError):
        _wait_for_outcome(events, 7)


class _SilentEvents:
    """Never delivers anything, like a service that lost the request."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, timeout: float) -> dict[str, Any] | None:
        self.reads += 1
        return None


def test_a_part_that_never_finishes_times_out_for_the_whole_wait() -> None:
    now = [0.0]
    events = _SilentEvents()

    def fake_clock() -> float:
        now[0] += 0.5  # time passes with each poll
        return now[0]

    with pytest.raises(ServiceUnavailableError, match="did not finish the part in time"):
        _wait_for_outcome(events, 7, timeout=10.0, clock=fake_clock)

    assert 1 <= events.reads <= 30


def test_the_marker_is_refreshed_when_a_long_reading_ends(workdir: Path) -> None:
    """Reading can take longer than the marker's lifetime; its reply must still be skipped."""
    env = _env(workdir)
    directory = handoff_dir(env, workdir / "home")
    capture_from_hook(_stop_payload("the real answer"), directory, now=1.0)
    times = iter([0.0, 10_000.0])  # the command starts at 0 and ends 10,000 seconds later
    with live_service(workdir / "rt" / "reader-assistant" / "service.sock"):
        read_last(env, workdir / "home", clock=lambda: next(times))

    assert not capture_from_hook(_stop_payload("Done."), directory, now=10_001.0)
    assert load_message(directory, SESSION) == "the real answer"
