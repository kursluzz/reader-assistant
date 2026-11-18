"""M5-4B: the PySide6 window, driven with the real service and fake engines (offscreen).

Qt widgets are used only on the main thread here, the same as in the application.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from fakes import FakeEngine, FakeSink, registry_with  # noqa: E402
from reader_assistant.app.reader_service import ReaderService, ServiceState  # noqa: E402
from reader_assistant.audio.player import AudioPlayer  # noqa: E402
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE  # noqa: E402
from reader_assistant.ui.controller import GuiController  # noqa: E402
from reader_assistant.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app() -> object:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app: object, until: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        app.processEvents()  # type: ignore[attr-defined]
        if time.monotonic() > deadline:
            raise AssertionError("the window did not reach the expected state in time")
        time.sleep(0.005)


def _more_written(sink: FakeSink, before: int) -> Callable[[], bool]:
    return lambda: len(sink.written) > before


def _service(sink: FakeSink) -> ReaderService:
    engines = [
        FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=4800),
        FakeEngine("piper", "he", HEBREW_VOICE, samples=4800),
        FakeEngine("piper", "ru", RUSSIAN_VOICE, samples=4800),
    ]
    service = ReaderService(registry_with(*engines), AudioPlayer(lambda: sink))
    service.start()
    return service


@pytest.fixture
def service() -> Iterator[tuple[ReaderService, FakeSink]]:
    sink = FakeSink(write_delay=0.0)
    svc = _service(sink)
    yield svc, sink
    svc.close()


def _window(service: ReaderService, on_close: Callable[[], threading.Event]) -> MainWindow:
    return MainWindow(GuiController(service), on_close=on_close)


def _finished() -> threading.Event:
    done = threading.Event()
    done.set()
    return done


def test_window_starts_idle_with_pause_and_stop_disabled(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, _ = service
    window = _window(svc, _finished)
    try:
        _pump(app, lambda: not window._pause.isEnabled() and not window._stop.isEnabled())

        assert window._status.text() == "Idle."
    finally:
        window.deleteLater()


def test_read_speaks_the_text_through_the_real_service(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, sink = service
    window = _window(svc, _finished)
    try:
        window._text.setPlainText("Good morning, this is English.")
        window._read.click()

        assert window._error.text() == ""
        _pump(app, lambda: bool(sink.closed_with_drain))
        _pump(app, lambda: svc.status().state is ServiceState.IDLE)
    finally:
        window.deleteLater()


def test_invalid_text_shows_the_error_in_the_window(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, _ = service
    window = _window(svc, _finished)
    try:
        window._text.setPlainText("12345 !!!")
        window._read.click()

        assert "no English, Russian or Hebrew letters" in window._error.text()
    finally:
        window.deleteLater()


def test_english_hebrew_and_russian_text_are_each_detected_and_spoken(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, sink = service
    window = _window(svc, _finished)
    try:
        for text in ("Good morning, English.", "שלום, זהו משפט בעברית.", "Доброе утро, русский."):
            written_before = len(sink.written)
            window._text.setPlainText(text)
            window._read.click()
            _pump(app, _more_written(sink, written_before))
            _pump(app, lambda: svc.status().state is ServiceState.IDLE)
    finally:
        window.deleteLater()


def test_text_editor_supports_select_all_and_replace(app: object) -> None:
    text = QtWidgets.QPlainTextEdit()
    text.setPlainText("first")
    text.selectAll()

    assert text.textCursor().hasSelection()
    text.insertPlainText("second")

    assert text.toPlainText() == "second"


def test_closing_the_window_runs_shutdown_and_closes_it(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, _ = service
    shut_down: list[bool] = []

    def on_close() -> threading.Event:
        done = threading.Event()

        def run() -> None:
            svc.close()
            shut_down.append(True)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        return done

    window = _window(svc, on_close)
    window.show()
    window.request_close()

    _pump(app, lambda: bool(shut_down) and not window.isVisible())


def test_stop_request_from_outside_closes_the_window(
    app: object, service: tuple[ReaderService, FakeSink]
) -> None:
    svc, _ = service
    stop_requested = threading.Event()
    window = MainWindow(GuiController(svc), on_close=_finished, stop_requested=stop_requested)
    window.show()

    stop_requested.set()  # as a SIGTERM would

    _pump(app, lambda: window._closing)
    _pump(app, lambda: not window.isVisible())
    svc.close()
