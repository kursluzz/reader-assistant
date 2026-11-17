"""M5-4: the Tkinter window, driven with the real service and fake engines.

These tests need a display. They skip cleanly when Tk cannot open one.
"""

from __future__ import annotations

import contextlib
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator

import pytest

from fakes import FakeEngine, FakeSink, registry_with
from reader_assistant.app.reader_service import ReaderService
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE
from reader_assistant.ui.controller import GuiController
from reader_assistant.ui.window import GuiWindow


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        tk_root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")
    tk_root.withdraw()  # the test does not need the window on screen
    yield tk_root
    with contextlib.suppress(tk.TclError):
        tk_root.destroy()


def _pump(root: tk.Tk, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run Tk's event loop by hand until ``until`` holds. Stands in for mainloop in tests."""
    deadline = time.monotonic() + timeout
    while not until():
        root.update()
        if time.monotonic() > deadline:
            raise AssertionError("the window did not reach the expected state in time")
        time.sleep(0.005)


def _exists(root: tk.Tk) -> bool:
    try:
        return bool(root.winfo_exists())
    except tk.TclError:  # the application was destroyed
        return False


def _service(sink: FakeSink) -> ReaderService:
    engines = [
        FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=4800),
        FakeEngine("piper", "he", HEBREW_VOICE, samples=4800),
        FakeEngine("piper", "ru", RUSSIAN_VOICE, samples=4800),
    ]
    service = ReaderService(registry_with(*engines), AudioPlayer(lambda: sink))
    service.start()
    return service


def _window(
    root: tk.Tk,
    service: ReaderService,
    on_close: Callable[[], threading.Event],
    stop_requested: threading.Event | None = None,
) -> GuiWindow:
    return GuiWindow(
        GuiController(service),
        on_close=on_close,
        stop_requested=stop_requested,
        root=root,
    )


def test_window_builds_and_shows_idle_with_controls_disabled(root: tk.Tk) -> None:
    service = _service(FakeSink())
    done = threading.Event()
    done.set()
    try:
        window = _window(root, service, on_close=lambda: done)
        _pump(root, lambda: str(window._pause_button.cget("state")) == "disabled")

        assert window._status.cget("text") == "Idle."
        assert str(window._stop_button.cget("state")) == "disabled"
        assert str(window._stop_button.cget("state")) == "disabled"
    finally:
        service.close()


def test_read_button_speaks_the_text_through_the_real_service(root: tk.Tk) -> None:
    sink = FakeSink(write_delay=0.0)
    service = _service(sink)
    done = threading.Event()
    done.set()
    try:
        window = _window(root, service, on_close=lambda: done)
        window._text.delete("1.0", "end")
        window._text.insert("1.0", "Good morning, this is English.")

        window._on_read()

        assert window._error.cget("text") == ""
        _pump(root, lambda: bool(sink.closed_with_drain))
        _pump(root, lambda: window._status.cget("text") == "Idle.")
    finally:
        service.close()


def test_invalid_text_shows_the_error_on_the_window(root: tk.Tk) -> None:
    service = _service(FakeSink())
    done = threading.Event()
    done.set()
    try:
        window = _window(root, service, on_close=lambda: done)
        window._text.delete("1.0", "end")
        window._text.insert("1.0", "12345 !!!")

        window._on_read()

        assert "no English, Russian or Hebrew letters" in window._error.cget("text")
    finally:
        service.close()


def test_language_choice_refreshes_the_voice_menu(root: tk.Tk) -> None:
    service = _service(FakeSink())
    done = threading.Event()
    done.set()
    try:
        window = _window(root, service, on_close=lambda: done)
        window._language.set("he")
        window._on_language_changed(None)

        assert str(window._voice_box.cget("state")) == "readonly"
        assert HEBREW_VOICE in window._voice_box.cget("values")
        window._language.set("auto")
        window._on_language_changed(None)
        assert str(window._voice_box.cget("state")) == "disabled"
    finally:
        service.close()


def test_closing_the_window_runs_shutdown_and_destroys_it(root: tk.Tk) -> None:
    service = _service(FakeSink())
    shut_down: list[bool] = []

    def on_close() -> threading.Event:
        done = threading.Event()

        def run() -> None:
            service.close()
            shut_down.append(True)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        return done

    window = _window(root, service, on_close=on_close)
    window.request_close()

    _pump(root, lambda: bool(shut_down) and window._closing)
    _pump(root, lambda: not _exists(root))


def test_stop_request_from_outside_closes_the_window(root: tk.Tk) -> None:
    service = _service(FakeSink())
    done = threading.Event()
    done.set()
    stop_requested = threading.Event()
    window = _window(root, service, on_close=lambda: done, stop_requested=stop_requested)

    stop_requested.set()  # as a SIGTERM would

    _pump(root, lambda: window._closing)
    service.close()
