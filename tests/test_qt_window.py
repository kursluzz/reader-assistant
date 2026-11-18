"""The PySide6 window as a client of a real service on a real socket (offscreen).

Qt widgets are touched only on the main thread, as in the application. The service runs in this
process only so the tests can see its sound output and state; the window never owns either.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from fakes import short_dir  # noqa: E402
from reader_assistant.ipc.client import request  # noqa: E402
from reader_assistant.ui.controller import GuiController  # noqa: E402
from reader_assistant.ui.service_link import ServiceLink  # noqa: E402
from reader_assistant.ui.window import MainWindow  # noqa: E402
from service_harness import LiveService, live_service  # noqa: E402


@pytest.fixture(scope="module")
def app() -> object:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _pump(app: object, until: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        app.processEvents()  # type: ignore[attr-defined]
        if time.monotonic() > deadline:
            raise AssertionError("the window did not reach the expected state in time")
        time.sleep(0.005)


def _window(
    path: Path, *, starter: Callable[[], object] | None = None
) -> tuple[MainWindow, ServiceLink]:
    link = ServiceLink(path, starter=starter)  # type: ignore[arg-type]
    link.start()
    window = MainWindow(GuiController(link, clipboard_reader=lambda: "clip"))
    window.show()
    return window, link


@pytest.fixture
def running(workdir: Path) -> Iterator[LiveService]:
    with live_service(workdir / "s.sock") as live:
        yield live


def _close(window: MainWindow, app: object) -> None:
    window.close()
    app.processEvents()  # type: ignore[attr-defined]


def test_window_connects_to_the_running_service_and_starts_idle(
    app: object, workdir: Path, running: LiveService
) -> None:
    window, link = _window(running.path)
    try:
        _pump(app, lambda: window._status.text() == "Idle.")
        assert not window._pause.isEnabled()
        assert not window._stop.isEnabled()
        assert not window._retry.isVisible()
    finally:
        _close(window, app)
        link.close()


def test_read_speaks_through_the_service_and_the_window_follows_it(
    app: object, workdir: Path, running: LiveService
) -> None:
    window, link = _window(running.path)
    try:
        _pump(app, lambda: window._status.text() == "Idle.")
        window._text.setPlainText("Good morning, this is English.")
        window._read.click()

        _pump(app, lambda: len(running.sink.written) > 0)
        _pump(app, lambda: running.service.status().state.value == "idle")
        _pump(app, lambda: window._status.text() == "Idle.")
        assert window._error.text() == ""
    finally:
        _close(window, app)
        link.close()


def test_invalid_text_shows_the_service_error_in_the_window(
    app: object, workdir: Path, running: LiveService
) -> None:
    window, link = _window(running.path)
    try:
        _pump(app, lambda: window._status.text() == "Idle.")
        window._text.setPlainText("1234 ---")
        window._read.click()

        _pump(app, lambda: "letters" in window._error.text())
    finally:
        _close(window, app)
        link.close()


def test_english_hebrew_and_russian_are_detected_automatically_through_the_window(
    app: object, workdir: Path, running: LiveService
) -> None:
    window, link = _window(running.path)
    try:
        _pump(app, lambda: window._status.text() == "Idle.")
        for text in ("Hello there.", "שלום עולם.", "Привет, мир."):
            window._text.setPlainText(text)
            window._read.click()
            _pump(app, lambda: running.service.status().state.value == "idle", timeout=10)
    finally:
        _close(window, app)
        link.close()

    assert running.service.status().last_error is None


def test_closing_the_window_disconnects_and_the_service_keeps_running(
    app: object, workdir: Path, running: LiveService
) -> None:
    window, link = _window(running.path)
    _pump(app, lambda: window._status.text() == "Idle.")

    _close(window, app)
    link.close()

    assert running.path.exists()
    assert request(running.path, "status")["state"] == "idle"


def test_unavailable_service_shows_retry_and_retry_brings_it_up(app: object, workdir: Path) -> None:
    path = workdir / "s.sock"
    attempts: list[int] = []
    servers: list[object] = []

    def starter() -> object:
        attempts.append(1)
        if len(attempts) <= 3:  # the link gives up after three starts; Retry starts it again
            raise OSError("no such executable")
        ctx = live_service(path)
        servers.append(ctx)
        ctx.__enter__()

        class Running:
            returncode = None

            def poll(self) -> None:
                return None

        return Running()

    window, link = _window(path, starter=starter)
    try:
        _pump(app, lambda: window._retry.isVisible(), timeout=15)
        assert "did not start" in window._status.text()

        window._retry.click()
        _pump(app, lambda: window._status.text() == "Idle.", timeout=15)
        assert not window._retry.isVisible()
    finally:
        _close(window, app)
        link.close()
        for ctx in servers:
            ctx.__exit__(None, None, None)  # type: ignore[attr-defined]


def test_stop_request_from_outside_closes_the_window_and_disconnects(
    app: object, workdir: Path, running: LiveService
) -> None:
    import threading

    stop = threading.Event()
    link = ServiceLink(running.path)
    link.start()
    window = MainWindow(GuiController(link, clipboard_reader=lambda: "clip"), stop_requested=stop)
    window.show()
    try:
        _pump(app, lambda: window._status.text() == "Idle.")
        stop.set()
        _pump(app, lambda: not window.isVisible())
    finally:
        link.close()

    assert request(running.path, "status")["state"] == "idle"
