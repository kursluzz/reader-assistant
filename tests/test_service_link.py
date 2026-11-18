"""The GUI's link to the service: connect, start when needed, reconnect, and never own it."""

from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fakes import short_dir
from reader_assistant.ipc.client import request
from reader_assistant.ui.service_link import (
    LinkMessage,
    LinkState,
    ServiceLink,
    backoff_seconds,
)
from service_harness import live_service


class FakeProcess:
    """Looks enough like Popen for the link: still running until the test says otherwise."""

    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def cleanup() -> Iterator[list[Callable[[], None]]]:
    actions: list[Callable[[], None]] = []
    yield actions
    for action in reversed(actions):
        with contextlib.suppress(Exception):
            action()


def _collect(
    link: ServiceLink, until: Callable[[list[LinkMessage]], bool], timeout: float = 10.0
) -> list[LinkMessage]:
    seen: list[LinkMessage] = []
    deadline = time.monotonic() + timeout
    while not until(seen):
        seen.extend(link.poll())
        if until(seen):
            break
        if time.monotonic() > deadline:
            kinds = [(m.kind, m.state.value if m.state else m.text) for m in seen]
            raise AssertionError(f"condition not met; seen: {kinds}")
        time.sleep(0.02)
    return seen


def _connected(seen: list[LinkMessage]) -> bool:
    return any(m.kind == "connection" and m.state is LinkState.CONNECTED for m in seen)


def _starter_for(
    path: Path,
    cleanup: list[Callable[[], None]],
    calls: list[int],
    *,
    fail_times: int = 0,
) -> Callable[[], Any]:
    """A starter that brings up a real service on ``path`` in this process, like a spawned one."""
    running: list[Any] = []

    def start() -> FakeProcess:
        calls.append(1)
        if len(calls) <= fail_times:
            raise OSError("no such executable")
        ctx = live_service(path)
        running.append(ctx.__enter__())

        def stop() -> None:
            ctx.__exit__(None, None, None)

        cleanup.append(stop)
        return FakeProcess()

    return start


# --- connecting -----------------------------------------------------------------


def test_connects_to_a_running_service_and_never_starts_another(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    starts: list[int] = []
    with live_service(path) as live:
        link = ServiceLink(path, starter=_starter_for(path, cleanup, starts))
        link.start()
        cleanup.append(link.close)
        seen = _collect(link, _connected)
        assert any(m.kind == "status" and m.data["state"] == "idle" for m in seen)

        link.send("speak", {"text": "Good morning, this is English."})
        seen += _collect(
            link,
            lambda items: any(m.kind == "event" and m.data["kind"] == "finished" for m in items),
        )
        link.close()

        assert starts == []
        assert live.service.status().state.value == "idle"


def test_starts_the_background_service_when_none_is_running(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    starts: list[int] = []
    link = ServiceLink(path, starter=_starter_for(path, cleanup, starts))
    link.start()
    cleanup.append(link.close)

    seen = _collect(link, _connected)

    assert len(starts) == 1
    assert any(m.kind == "connection" and m.state is LinkState.STARTING for m in seen)


def test_closing_the_window_disconnects_and_leaves_the_service_running(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    starts: list[int] = []
    link = ServiceLink(path, starter=_starter_for(path, cleanup, starts))
    link.start()
    _collect(link, _connected)

    link.close()

    assert request(path, "status")["state"] == "idle"


def test_gives_up_after_failed_starts_and_offers_a_retry(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    starts: list[int] = []
    link = ServiceLink(path, starter=_starter_for(path, cleanup, starts, fail_times=99))
    link.start()
    cleanup.append(link.close)

    seen = _collect(
        link,
        lambda items: any(
            m.kind == "connection" and m.state is LinkState.UNAVAILABLE for m in items
        ),
    )
    unavailable = [m for m in seen if m.kind == "connection" and m.state is LinkState.UNAVAILABLE]
    attempts_before = len(starts)
    time.sleep(0.8)

    assert "did not start" in unavailable[-1].text
    assert len(starts) == attempts_before  # no retry loop: it waits for the user
    assert attempts_before <= 3


def test_retry_after_giving_up_starts_the_service_again(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    starts: list[int] = []
    link = ServiceLink(path, starter=_starter_for(path, cleanup, starts, fail_times=3))
    link.start()
    cleanup.append(link.close)
    _collect(
        link,
        lambda items: any(
            m.kind == "connection" and m.state is LinkState.UNAVAILABLE for m in items
        ),
    )

    link.retry()

    _collect(link, _connected)
    assert len(starts) == 4


# --- reconnecting -----------------------------------------------------------------


def test_reconnects_after_a_service_restart_and_takes_a_fresh_status(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    path = workdir / "s.sock"
    first = live_service(path)
    first.__enter__()
    link = ServiceLink(path)  # no starter: this test only checks reconnection
    link.start()
    cleanup.append(link.close)
    _collect(link, _connected)
    first.__exit__(None, None, None)

    _collect(
        link,
        lambda items: any(
            m.kind == "connection" and m.state is LinkState.RECONNECTING for m in items
        ),
    )
    with live_service(path):
        reconnected = _collect(link, _connected)
        link.send("speak", {"text": "Good morning, this is English."})
        spoken = _collect(
            link,
            lambda items: any(m.kind == "event" and m.data["kind"] == "finished" for m in items),
        )
        link.close()
    seen = reconnected + spoken

    assert any(m.kind == "status" for m in reconnected), "no fresh status after reconnecting"
    assert any(m.kind == "event" and m.data["kind"] == "finished" for m in seen)


def test_duplicate_and_stale_events_are_not_shown_twice(workdir: Path) -> None:
    link = ServiceLink(workdir / "unused.sock")

    class Stream:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self._items = list(items)

        def read(self, timeout: float) -> dict[str, Any] | None:
            if self._items:
                return self._items.pop(0)
            return {"version": 2, "type": "closed", "reason": "shutdown"}

    items = [
        {"version": 2, "type": "event", "event": {"seq": 2, "kind": "x"}},
        {"version": 2, "type": "event", "event": {"seq": 2, "kind": "x"}},
        {"version": 2, "type": "event", "event": {"seq": 1, "kind": "x"}},
        {"version": 2, "type": "event", "event": {"seq": 3, "kind": "y"}},
    ]
    link._pump(Stream(items), last_seq=1)  # type: ignore[arg-type]

    shown = [m.data["seq"] for m in link.poll() if m.kind == "event"]
    assert shown == [2, 3]


# --- commands and helpers -------------------------------------------------------------


def test_queued_volume_changes_are_coalesced_to_the_latest(workdir: Path) -> None:
    link = ServiceLink(workdir / "unused.sock")  # not started: the queue is inspected directly

    for value in (0.1, 0.5, 0.9):
        link.send("set_volume", {"volume": value}, coalesce=True)
    link.send("stop")

    assert list(link._commands) == [("set_volume", {"volume": 0.9}), ("stop", None)]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (5, 5.0), (9, 5.0)],
)
def test_backoff_grows_and_is_bounded(attempt: int, expected: float) -> None:
    assert backoff_seconds(attempt) == expected


def test_a_given_up_link_connects_by_itself_when_a_service_appears(
    workdir: Path, cleanup: list[Callable[[], None]]
) -> None:
    """Started by hand or at login after the window gave up: no Retry needed."""
    path = workdir / "s.sock"
    starts: list[int] = []
    link = ServiceLink(path, starter=_starter_for(path, cleanup, starts, fail_times=99))
    link.start()
    cleanup.append(link.close)
    _collect(
        link,
        lambda items: any(
            m.kind == "connection" and m.state is LinkState.UNAVAILABLE for m in items
        ),
    )

    with live_service(path):  # someone else starts the service
        seen = _collect(link, _connected, timeout=15)

    assert any(m.kind == "status" for m in seen)
