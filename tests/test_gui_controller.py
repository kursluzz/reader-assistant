"""M5-4: the GUI controller and latency probe. No display needed; no tkinter import."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

import pytest

from fakes import FakeEngine, FakeSink, registry_with, wait_for
from reader_assistant.app.reader_service import (
    Accepted,
    Event,
    EventKind,
    ReaderService,
    ServiceState,
    ServiceStatus,
)
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.clipboard.reader import ClipboardError
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE
from reader_assistant.ui.controller import AUTO, DEFAULT_VOICE_LABEL, GuiController
from reader_assistant.ui.latency import LatencyProbe

EN_TEXT = "Good morning, this is English."


def _real_service(sink: FakeSink | None = None) -> ReaderService:
    engines = [
        FakeEngine("kokoro", "en", ENGLISH_VOICE, extra_voices=("af_sarah",), samples=24_000),
        FakeEngine("piper", "he", HEBREW_VOICE),
        FakeEngine("piper", "ru", RUSSIAN_VOICE),
    ]
    service = ReaderService(
        registry_with(*engines), AudioPlayer(lambda: sink or FakeSink(write_delay=0.01))
    )
    service.start()
    return service


@pytest.fixture
def service() -> Iterator[ReaderService]:
    svc = _real_service()
    yield svc
    svc.close()


# --- a stub service, for exact event order ----------------------------------------


@dataclass
class StubService:
    """Just enough of ReaderService for the controller, with scripted events."""

    events: list[Event] = field(default_factory=list)
    state: ServiceState = ServiceState.IDLE
    last_error: str | None = None
    submitted: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    volume: float = 1.0

    def submit(self, text: str, language: str | None = None, voice: str | None = None) -> Accepted:
        self.submitted.append((text, language, voice))
        return Accepted(
            request_id=len(self.submitted),
            language=language or "en",
            voice_id=voice or ENGLISH_VOICE,
        )

    def drain_events(self) -> list[Event]:
        events, self.events = self.events, []
        return events

    def status(self) -> ServiceStatus:
        return ServiceStatus(
            state=self.state,
            request_id=None,
            language=None,
            voice_id=None,
            engines=(),
            last_error=self.last_error,
            volume=self.volume,
        )

    def pause(self) -> bool:
        self.paused.append("pause")
        return True

    def resume(self) -> bool:
        self.paused.append("resume")
        return True

    def stop(self) -> bool:
        self.paused.append("stop")
        return True

    def set_volume(self, volume: float) -> None:
        self.volume = volume

    def voices(self) -> tuple[tuple[str, str], ...]:
        return (("en", ENGLISH_VOICE), ("he", HEBREW_VOICE), ("ru", RUSSIAN_VOICE))


def _stub_controller(stub: StubService, clipboard: object = None) -> GuiController:
    reader = clipboard if callable(clipboard) else (lambda: "unused")
    return GuiController(cast(ReaderService, stub), clipboard_reader=reader)


# --- reading -----------------------------------------------------------------------


def test_read_with_auto_language_passes_no_language_to_the_service(service: ReaderService) -> None:
    stub = StubService()
    controller = _stub_controller(stub)

    assert controller.read(EN_TEXT, AUTO, DEFAULT_VOICE_LABEL) == ""
    assert stub.submitted == [(EN_TEXT, None, None)]


def test_read_with_explicit_language_and_voice_passes_both(service: ReaderService) -> None:
    stub = StubService()
    controller = _stub_controller(stub)

    controller.read("Привет", "ru", RUSSIAN_VOICE)

    assert stub.submitted == [("Привет", "ru", RUSSIAN_VOICE)]


def test_invalid_selection_returns_the_message_to_show() -> None:
    controller = GuiController(_real_service())
    try:
        message = controller.read(EN_TEXT, "fr", DEFAULT_VOICE_LABEL)

        assert "unsupported language 'fr'" in message
        assert controller.display_state().error == message
    finally:
        controller._service.close()


def test_empty_text_is_rejected_with_a_message() -> None:
    controller = GuiController(_real_service())
    try:
        assert controller.read("   ", AUTO, DEFAULT_VOICE_LABEL) == "The text is empty."
    finally:
        controller._service.close()


def test_real_service_reads_english_and_reports_idle_after_speech() -> None:
    sink = FakeSink(write_delay=0.0)
    svc = _real_service(sink)
    controller = GuiController(svc)
    try:
        assert controller.read(EN_TEXT, "en", DEFAULT_VOICE_LABEL) == ""
        wait_for(lambda: bool(sink.closed_with_drain))
        controller.drain()

        assert controller.display_state().error == ""
    finally:
        svc.close()


# --- controls ---------------------------------------------------------------------


def test_pause_toggles_between_pause_and_resume_by_state() -> None:
    stub = StubService(state=ServiceState.PLAYING)
    controller = _stub_controller(stub)

    controller.toggle_pause()
    stub.state = ServiceState.PAUSED
    controller.toggle_pause()

    assert stub.paused == ["pause", "resume"]


def test_controls_are_disabled_when_idle() -> None:
    controller = _stub_controller(StubService(state=ServiceState.IDLE))

    state = controller.display_state()

    assert state.can_pause is False
    assert state.can_stop is False
    assert state.status == "Idle."


def test_volume_is_clamped_and_passed_to_the_service() -> None:
    stub = StubService()
    controller = _stub_controller(stub)

    controller.set_volume_percent(40)
    assert stub.volume == pytest.approx(0.4)
    controller.set_volume_percent(250)
    assert stub.volume == 1.0
    controller.set_volume_percent(-5)
    assert stub.volume == 0.0


def test_stop_is_forwarded() -> None:
    stub = StubService()
    _stub_controller(stub).stop()

    assert stub.paused == ["stop"]


# --- voice menu -------------------------------------------------------------------


def test_voice_menu_is_default_only_for_auto_and_lists_voices_per_language() -> None:
    controller = _stub_controller(StubService())

    assert controller.voices_for(AUTO) == (DEFAULT_VOICE_LABEL,)
    assert controller.voices_for("en") == (DEFAULT_VOICE_LABEL, ENGLISH_VOICE)
    assert controller.voices_for("he") == (DEFAULT_VOICE_LABEL, HEBREW_VOICE)
    assert controller.voices_for("fr") == (DEFAULT_VOICE_LABEL,)


# --- events and stale errors ---------------------------------------------------------


def test_error_for_the_latest_request_is_shown() -> None:
    stub = StubService()
    controller = _stub_controller(stub)
    controller.read(EN_TEXT, AUTO, DEFAULT_VOICE_LABEL)  # request 1
    stub.events = [Event(EventKind.ERROR, request_id=1, code="engine_failed", message="boom")]

    controller.drain()

    assert controller.display_state().error == "boom"


def test_stale_error_from_an_older_request_is_dropped() -> None:
    stub = StubService()
    controller = _stub_controller(stub)
    controller.read("first", AUTO, DEFAULT_VOICE_LABEL)  # request 1
    controller.read("second", AUTO, DEFAULT_VOICE_LABEL)  # request 2 is now the latest
    stub.events = [Event(EventKind.ERROR, request_id=1, code="engine_failed", message="old boom")]

    controller.drain()

    assert controller.display_state().error == ""


def test_accepted_event_from_another_client_clears_the_old_error() -> None:
    stub = StubService()
    controller = _stub_controller(stub)
    controller.read(EN_TEXT, AUTO, DEFAULT_VOICE_LABEL)
    stub.events = [Event(EventKind.ERROR, request_id=1, code="engine_failed", message="boom")]
    controller.drain()
    stub.events = [Event(EventKind.ACCEPTED, request_id=7, language="en", voice_id=ENGLISH_VOICE)]

    controller.drain()

    assert controller.display_state().error == ""


def test_display_state_reports_playing_language_and_voice() -> None:
    stub = StubService(state=ServiceState.PLAYING)
    stub.status = lambda: ServiceStatus(  # type: ignore[method-assign]
        state=ServiceState.PLAYING,
        request_id=3,
        language="he",
        voice_id=HEBREW_VOICE,
        engines=(),
        last_error=None,
        volume=1.0,
    )
    state = _stub_controller(stub).display_state()

    assert state.status == f"Playing (he, {HEBREW_VOICE})"
    assert state.pause_label == "Pause"
    assert state.can_pause and state.can_stop


def test_display_state_shows_resume_while_paused() -> None:
    state = _stub_controller(StubService(state=ServiceState.PAUSED)).display_state()

    assert state.pause_label == "Resume"


# --- paste ----------------------------------------------------------------------------


def test_paste_runs_in_the_background_so_the_window_is_not_blocked() -> None:
    release = threading.Event()

    def slow_clipboard() -> str:
        release.wait(timeout=5)
        return "pasted text"

    controller = _stub_controller(StubService(), clipboard=slow_clipboard)

    controller.start_paste()

    assert controller.display_state().busy_pasting is True
    assert controller.poll_paste() is None  # still waiting, and the call returned at once
    release.set()


def test_paste_returns_the_clipboard_text() -> None:
    controller = _stub_controller(StubService(), clipboard=lambda: "from the clipboard")
    controller.start_paste()

    result: str | None = None
    deadline = time.monotonic() + 5
    while result is None and time.monotonic() < deadline:
        result = controller.poll_paste()
        time.sleep(0.005)

    assert result == "from the clipboard"
    assert controller.display_state().busy_pasting is False


def test_paste_error_is_shown_and_returns_an_empty_string() -> None:
    def empty() -> str:
        raise ClipboardError("The clipboard is empty.")

    controller = _stub_controller(StubService(), clipboard=empty)
    controller.start_paste()

    result: str | None = None
    deadline = time.monotonic() + 5
    while result is None and time.monotonic() < deadline:
        result = controller.poll_paste()
        time.sleep(0.005)

    assert result == ""
    assert controller.display_state().error == "The clipboard is empty."


def test_second_paste_while_one_is_running_is_ignored() -> None:
    calls: list[int] = []
    release = threading.Event()

    def clipboard() -> str:
        calls.append(1)
        release.wait(timeout=5)
        return "x"

    controller = _stub_controller(StubService(), clipboard=clipboard)
    controller.start_paste()
    controller.start_paste()
    release.set()
    deadline = time.monotonic() + 5
    while controller.poll_paste() is None and time.monotonic() < deadline:
        time.sleep(0.005)

    assert len(calls) == 1


# --- latency probe ---------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_probe_files_lateness_under_the_current_phase() -> None:
    clock = FakeClock()
    probe = LatencyProbe(clock)
    probe.mark("synthesis")
    clock.now = 100.02
    probe.record(scheduled_at=100.01)  # fired 10 ms late

    [summary] = probe.summary()

    assert summary.phase == "synthesis"
    assert summary.samples == 1
    assert summary.max_ms == pytest.approx(10.0)
    assert summary.typical_ms == pytest.approx(10.0)


def test_probe_summaries_give_median_and_maximum_per_phase() -> None:
    clock = FakeClock()
    probe = LatencyProbe(clock)
    probe.mark("playback")
    for lateness in (0.001, 0.002, 0.050):
        clock.now = 100.0 + lateness
        probe.record(scheduled_at=100.0)

    [summary] = probe.summary()

    assert summary.samples == 3
    assert summary.typical_ms == pytest.approx(2.0)
    assert summary.max_ms == pytest.approx(50.0)


def test_probe_never_reports_negative_lateness() -> None:
    clock = FakeClock()
    probe = LatencyProbe(clock)
    probe.record(scheduled_at=200.0)  # scheduled in the future

    assert probe.summary()[0].max_ms == 0.0


def test_probe_phases_appear_in_the_order_they_started() -> None:
    clock = FakeClock()
    probe = LatencyProbe(clock)
    for phase in ("loading", "synthesis", "playback"):
        probe.mark(phase)
        probe.record(scheduled_at=clock.now)

    assert [item.phase for item in probe.summary()] == ["loading", "synthesis", "playback"]
