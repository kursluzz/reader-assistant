"""M5-1: the ReaderService API, status snapshots, controls and events.

Every test uses fake engines and a fake sink, so no model is loaded and no device is
opened.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import weakref
from collections.abc import Callable, Iterator

import pytest

from fakes import FakeEngine, FakeSink, registry_with, wait_for
from reader_assistant.app.reader_service import (
    Event,
    EventKind,
    ReaderService,
    ServiceState,
    SpeechError,
)
from reader_assistant.app.subscriptions import Subscription
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.base import AudioBuffer
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE

EN_TEXT = "Good morning. This is the English voice."
RU_TEXT = "Доброе утро. Это русский голос."
HE_TEXT = "בוקר טוב. זהו הקול העברי."
EN_EXTRA_VOICE = "af_sarah"


def _engines(**overrides: FakeEngine) -> dict[str, FakeEngine]:
    # 120,000 samples is 5 s of audio, long enough to pause or stop during playback.
    engines = {
        "en": FakeEngine(
            "kokoro", "en", ENGLISH_VOICE, extra_voices=(EN_EXTRA_VOICE,), samples=120_000
        ),
        "he": FakeEngine("piper", "he", HEBREW_VOICE),
        "ru": FakeEngine("piper", "ru", RUSSIAN_VOICE),
    }
    engines.update(overrides)
    return engines


_LOGS: weakref.WeakKeyDictionary[ReaderService, Subscription[Event]] = weakref.WeakKeyDictionary()


def _service(engines: dict[str, FakeEngine], sink_factory: Callable[[], FakeSink]) -> ReaderService:
    """A service whose events are recorded from the start. Read them with :func:`drain_events`."""
    svc = ReaderService(registry_with(*engines.values()), AudioPlayer(sink_factory))
    _, subscription = svc.subscribe(capacity=100_000)
    _LOGS[svc] = subscription
    return svc


def drain_events(svc: ReaderService) -> list[Event]:
    """Every event since the last call, oldest first."""
    return _LOGS[svc].drain()


def _collect(service: ReaderService, until: Callable[[list[Event]], bool]) -> list[Event]:
    """Drain events until ``until`` is satisfied, then return everything seen."""
    seen: list[Event] = []
    deadline = time.monotonic() + 5.0
    while not until(seen):
        seen.extend(drain_events(service))
        if until(seen):
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"events did not arrive; seen: {[e.kind.value for e in seen]}")
        time.sleep(0.005)
    return seen


def _kinds(events: list[Event]) -> list[EventKind]:
    return [event.kind for event in events]


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink(write_delay=0.01)


@pytest.fixture
def service(sink: FakeSink) -> Iterator[ReaderService]:
    svc = _service(_engines(), lambda: sink)
    svc.start()
    try:
        yield svc
    finally:
        svc.close()


# --- explicit language and voice ------------------------------------------------


def test_detection_is_still_used_when_language_is_omitted(service: ReaderService) -> None:
    assert service.submit(RU_TEXT).language == "ru"
    assert service.submit(HE_TEXT).language == "he"
    assert service.submit(EN_TEXT).language == "en"


# --- status snapshot --------------------------------------------------------------


def test_status_snapshot_is_immutable() -> None:
    svc = _service(_engines(), FakeSink)
    try:
        status = svc.status()
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.state = ServiceState.PLAYING  # type: ignore[misc]
        assert isinstance(status.engines, tuple)
    finally:
        svc.close()


def test_status_reports_engines_and_loading_state(sink: FakeSink) -> None:
    engines = _engines()
    svc = _service(engines, lambda: sink)
    svc.start()
    try:
        before = {item.language: item.loaded for item in svc.status().engines}
        assert before == {"en": False, "he": False, "ru": False}

        svc.submit(EN_TEXT)
        wait_for(lambda: svc.status().engines[0].loaded or svc.status().state is ServiceState.IDLE)
        _collect(svc, lambda seen: any(e.kind is EventKind.FINISHED for e in seen))

        after = {item.language: item.loaded for item in svc.status().engines}
        assert after == {"en": True, "he": False, "ru": False}
        assert {item.engine_id for item in svc.status().engines} == {"kokoro", "piper"}
    finally:
        svc.close()


def test_status_tracks_the_active_request_and_returns_to_idle(
    service: ReaderService, sink: FakeSink
) -> None:
    accepted = service.submit(EN_TEXT)

    events = _collect(service, lambda seen: any(e.kind is EventKind.FINISHED for e in seen))

    assert accepted.request_id in {e.request_id for e in events}
    status = service.status()
    assert status.state is ServiceState.IDLE
    assert status.request_id is None
    assert status.language is None


def test_status_volume_reflects_set_volume(service: ReaderService) -> None:
    service.set_volume(0.25)

    assert service.status().volume == 0.25
    with pytest.raises(ValueError):
        service.set_volume(1.5)


def test_status_reads_are_thread_safe_during_a_burst_of_requests(service: ReaderService) -> None:
    errors: list[BaseException] = []
    stop_flag = threading.Event()

    def reader() -> None:
        try:
            while not stop_flag.is_set():
                status = service.status()
                assert isinstance(status.state, ServiceState)
                assert all(isinstance(item.loaded, bool) for item in status.engines)
        except BaseException as exc:  # reported to the test thread
            errors.append(exc)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for index in range(30):
            service.submit(f"Burst number {index} in English.")
        time.sleep(0.2)
    finally:
        stop_flag.set()
        for thread in threads:
            thread.join(timeout=5)

    assert errors == []


# --- controls ---------------------------------------------------------------------


def test_pause_and_resume_during_playback_emit_events_and_update_status(
    service: ReaderService, sink: FakeSink
) -> None:
    accepted = service.submit("A passage long enough to pause in the middle of playback.")
    _collect(service, lambda seen: any(e.kind is EventKind.PLAYBACK_STARTED for e in seen))
    wait_for(lambda: len(sink.written) > 0)

    assert service.pause() is True
    seen = _collect(service, lambda events: any(e.kind is EventKind.PAUSED for e in events))
    assert service.status().state is ServiceState.PAUSED
    assert all(e.request_id == accepted.request_id for e in seen if e.request_id is not None)

    assert service.resume() is True
    _collect(service, lambda events: any(e.kind is EventKind.RESUMED for e in events))
    assert service.status().state is ServiceState.PLAYING

    service.stop()


def test_pause_and_resume_do_nothing_when_idle(service: ReaderService) -> None:
    assert service.pause() is False
    assert service.resume() is False
    assert service.status().state is ServiceState.IDLE


def test_stop_during_playback_stops_it_without_a_finished_event(
    service: ReaderService, sink: FakeSink
) -> None:
    accepted = service.submit("A long passage that will be stopped half way through.")
    _collect(service, lambda seen: any(e.kind is EventKind.PLAYBACK_STARTED for e in seen))
    wait_for(lambda: len(sink.written) > 0)

    assert service.stop() is True

    seen = _collect(service, lambda events: any(e.kind is EventKind.STOPPED for e in events))
    stopped = next(e for e in seen if e.kind is EventKind.STOPPED)
    assert stopped.request_id == accepted.request_id
    assert stopped.reason == "user"
    wait_for(lambda: sink.closed_with_drain == [False])
    assert all(e.kind is not EventKind.FINISHED for e in seen)
    assert service.status().state is ServiceState.IDLE


def test_stop_while_synthesising_cancels_and_plays_nothing() -> None:
    gate = threading.Event()
    sink = FakeSink()
    engines = _engines(en=FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate))
    svc = _service(engines, lambda: sink)
    svc.start()
    try:
        accepted = svc.submit(EN_TEXT)
        wait_for(lambda: svc.status().state is ServiceState.SYNTHESIZING)

        assert svc.stop() is True
        assert svc.status().state is ServiceState.IDLE
        gate.set()

        seen = _collect(svc, lambda events: any(e.kind is EventKind.CANCELLED for e in events))
        cancelled = [e for e in seen if e.kind is EventKind.CANCELLED]
        assert cancelled[-1].request_id == accepted.request_id
        assert cancelled[-1].reason == "user"
        time.sleep(0.1)
        assert sink.opened == 0
        assert all(e.kind is not EventKind.PLAYBACK_STARTED for e in drain_events(svc))
    finally:
        gate.set()
        svc.close()


def test_stop_when_idle_returns_false(service: ReaderService) -> None:
    assert service.stop() is False


def test_stop_drops_a_queued_request_too(sink: FakeSink) -> None:
    gate = threading.Event()
    engines = _engines(en=FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate))
    svc = _service(engines, lambda: sink)
    svc.start()
    try:
        svc.submit("First request, which blocks.")
        wait_for(lambda: svc.status().state is ServiceState.SYNTHESIZING)
        queued = svc.submit("Second request, queued.")

        svc.stop()
        gate.set()
        seen = _collect(svc, lambda events: any(e.kind is EventKind.CANCELLED for e in events))

        assert queued.request_id in {e.request_id for e in seen if e.kind is EventKind.CANCELLED}
        time.sleep(0.1)
        assert [text for text, _ in engines["en"].synthesized] == ["First request, which blocks."]
    finally:
        gate.set()
        svc.close()


# --- event order and request IDs --------------------------------------------------


def test_events_for_one_request_arrive_in_lifecycle_order(sink: FakeSink) -> None:
    svc = _service(_engines(), lambda: sink)
    svc.start()
    try:
        accepted = svc.submit(RU_TEXT)
        events = _collect(svc, lambda seen: any(e.kind is EventKind.FINISHED for e in seen))

        own = [e for e in events if e.request_id == accepted.request_id]
        assert _kinds(own) == [
            EventKind.ACCEPTED,
            EventKind.LOADING,
            EventKind.SYNTHESIS_STARTED,
            EventKind.SYNTHESIS_COMPLETED,
            EventKind.PLAYBACK_STARTED,
            EventKind.FINISHED,
        ]
        assert own[0].language == "ru"
        assert own[0].voice_id == RUSSIAN_VOICE
        assert own[3].duration_seconds > 0
    finally:
        svc.close()


def test_request_ids_increase_and_are_unique(service: ReaderService) -> None:
    ids = [service.submit(EN_TEXT).request_id for _ in range(3)]

    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_a_replaced_pending_request_is_cancelled_with_its_own_id(
    service: ReaderService,
) -> None:
    gate = threading.Event()
    engines = _engines(en=FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate))
    svc = _service(engines, lambda: FakeSink())
    svc.start()
    try:
        first = svc.submit("Starts and blocks in the engine.")
        wait_for(lambda: svc.status().request_id == first.request_id)
        dropped = svc.submit("Dropped because a newer request arrives.")
        newest = svc.submit("The newest request.")
        gate.set()

        seen = _collect(svc, lambda events: any(e.kind is EventKind.FINISHED for e in events))
        cancelled = [e for e in seen if e.kind is EventKind.CANCELLED]
        assert [(e.request_id, e.reason) for e in cancelled] == [(dropped.request_id, "replaced")]
        # The first request was audible when the newest one's audio was ready, so the newest
        # interrupted it. Only the newest finishes.
        stopped = [(e.request_id, e.reason) for e in seen if e.kind is EventKind.STOPPED]
        assert (first.request_id, "replaced") in stopped
        finished = [e.request_id for e in seen if e.kind is EventKind.FINISHED]
        assert finished == [newest.request_id]
    finally:
        gate.set()
        svc.close()


def test_a_new_request_interrupts_playback_and_the_old_one_reports_stopped(
    sink: FakeSink,
) -> None:
    sinks: list[FakeSink] = []

    def factory() -> FakeSink:
        created = FakeSink(write_delay=0.01)
        sinks.append(created)
        return created

    engines = _engines(en=FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=120_000))
    svc = _service(engines, factory)
    svc.start()
    try:
        first = svc.submit("First long English passage.")
        wait_for(lambda: len(sinks) == 1 and len(sinks[0].written) > 0)
        second = svc.submit("Second English passage.")

        seen = _collect(svc, lambda events: sum(e.kind is EventKind.FINISHED for e in events) >= 1)
        stopped = [e for e in seen if e.kind is EventKind.STOPPED]
        assert [(e.request_id, e.reason) for e in stopped] == [(first.request_id, "replaced")]
        assert all(e.request_id != first.request_id for e in seen if e.kind is EventKind.FINISHED)
        assert [e.request_id for e in seen if e.kind is EventKind.FINISHED] == [second.request_id]
    finally:
        svc.close()


def test_no_two_speech_workers_or_playback_threads_run_at_once(sink: FakeSink) -> None:
    gate = threading.Event()
    engine = FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate)
    svc = _service(_engines(en=engine), lambda: sink)
    svc.start()
    try:
        svc.submit("Burst one.")
        wait_for(lambda: engine.active == 1)
        for index in range(2, 8):
            svc.submit(f"Burst {index}.")
        gate.set()
        wait_for(lambda: len(engine.synthesized) == 2)

        assert engine.max_active == 1
        speech = [t for t in threading.enumerate() if t.name == "reader-assistant-speech"]
        assert len(speech) == 1
    finally:
        gate.set()
        svc.close()


# --- errors -----------------------------------------------------------------------


def test_engine_failure_is_reported_as_an_error_event_with_the_request_id(
    sink: FakeSink,
) -> None:
    engines = _engines(
        en=FakeEngine("kokoro", "en", ENGLISH_VOICE, fail_synthesize=True),
    )
    svc = _service(engines, lambda: sink)
    svc.start()
    try:
        accepted = svc.submit(EN_TEXT)
        seen = _collect(svc, lambda events: any(e.kind is EventKind.ERROR for e in events))

        error = next(e for e in seen if e.kind is EventKind.ERROR)
        assert error.request_id == accepted.request_id
        assert error.code == "engine_failed"
        assert "fake synthesis failure" in error.message
        assert svc.status().state is ServiceState.IDLE
        assert svc.status().request_id is None
        assert svc.last_error is not None and svc.last_error.startswith("engine_failed")
        assert sink.opened == 0
    finally:
        svc.close()


def test_audio_device_failure_is_reported_as_an_error_event(sink: FakeSink) -> None:
    svc = _service(_engines(), lambda: FakeSink(fail_open=True))
    svc.start()
    try:
        accepted = svc.submit(EN_TEXT)
        seen = _collect(svc, lambda events: any(e.kind is EventKind.ERROR for e in events))

        error = next(e for e in seen if e.kind is EventKind.ERROR)
        assert error.request_id == accepted.request_id
        assert error.code == "audio_failed"
        assert svc.status().state is ServiceState.IDLE
    finally:
        svc.close()


def test_device_failure_mid_playback_is_an_error_not_a_finish(sink: FakeSink) -> None:
    from reader_assistant.audio.sink import AudioDeviceError

    class BreakingSink(FakeSink):
        def write(self, pcm16: bytes) -> None:
            raise AudioDeviceError("device unplugged")

    svc = _service(_engines(), BreakingSink)
    svc.start()
    try:
        accepted = svc.submit(EN_TEXT)
        seen = _collect(svc, lambda events: any(e.kind is EventKind.ERROR for e in events))

        error = next(e for e in seen if e.kind is EventKind.ERROR)
        assert error.request_id == accepted.request_id
        assert "device unplugged" in error.message
        assert all(e.kind is not EventKind.FINISHED for e in seen)
        assert svc.status().state is ServiceState.IDLE
    finally:
        svc.close()


def test_status_reports_loading_while_an_engine_loads() -> None:
    gate = threading.Event()

    class SlowLoadEngine(FakeEngine):
        def load(self) -> None:
            gate.wait(timeout=5.0)
            super().load()

    sink = FakeSink()
    svc = _service(_engines(en=SlowLoadEngine("kokoro", "en", ENGLISH_VOICE)), lambda: sink)
    svc.start()
    try:
        accepted = svc.submit(EN_TEXT)
        wait_for(lambda: svc.status().state is ServiceState.LOADING)
        status = svc.status()
        assert status.request_id == accepted.request_id
        assert status.language == "en"
        assert status.voice_id == ENGLISH_VOICE

        gate.set()
        _collect(svc, lambda events: any(e.kind is EventKind.FINISHED for e in events))
    finally:
        gate.set()
        svc.close()


# --- existing behaviour kept ---------------------------------------------------


def test_latest_request_wins_still_holds_for_a_burst(sink: FakeSink) -> None:
    gate = threading.Event()
    engine = FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate)
    svc = _service(_engines(en=engine), lambda: sink)
    svc.start()
    try:
        svc.submit("Request one, starts and blocks.")
        wait_for(lambda: engine.active == 1)
        for text in ("Request two.", "Request three.", "Request four.", "Request five."):
            svc.submit(text)
        gate.set()
        wait_for(lambda: len(engine.synthesized) == 2)

        assert [text for text, _ in engine.synthesized] == [
            "Request one, starts and blocks.",
            "Request five.",
        ]
    finally:
        gate.set()
        svc.close()


def test_close_emits_shutdown_last_and_is_idempotent(sink: FakeSink) -> None:
    svc = _service(_engines(), lambda: sink)
    svc.start()
    svc.submit(EN_TEXT)
    svc.close()
    svc.close()

    events = drain_events(svc)
    assert events[-1].kind is EventKind.SHUTDOWN
    assert sum(e.kind is EventKind.SHUTDOWN for e in events) == 1
    with pytest.raises(SpeechError) as excinfo:
        svc.submit(EN_TEXT)
    assert excinfo.value.code == "shutting_down"


def test_status_keeps_the_audible_request_while_a_newer_one_is_prepared() -> None:
    gate = threading.Event()

    class GatedSecond(FakeEngine):
        def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
            # Only the second request waits; the fake's own counters still apply.
            self._gate = gate if text.startswith("Second") else None
            return super().synthesize(text, voice_id=voice_id, speed=speed)

    engine = GatedSecond("kokoro", "en", ENGLISH_VOICE, samples=120_000)
    sinks: list[FakeSink] = []

    def factory() -> FakeSink:
        created = FakeSink(write_delay=0.01)
        sinks.append(created)
        return created

    svc = _service(_engines(en=engine), factory)
    svc.start()
    try:
        first = svc.submit("First long English passage.")
        wait_for(lambda: svc.status().state is ServiceState.PLAYING)
        second = svc.submit("Second English passage, being prepared.")
        wait_for(lambda: engine.active == 1)

        # The user still hears the first request, so the status describes it.
        status = svc.status()
        assert status.state is ServiceState.PLAYING
        assert status.request_id == first.request_id

        assert svc.stop() is True
        seen = _collect(svc, lambda events: any(e.kind is EventKind.CANCELLED for e in events))
        gate.set()
        cancelled = {(e.request_id, e.reason) for e in seen if e.kind is EventKind.CANCELLED}
        stopped = {(e.request_id, e.reason) for e in seen if e.kind is EventKind.STOPPED}
        assert (second.request_id, "user") in cancelled
        assert (first.request_id, "user") in stopped
        time.sleep(0.1)
        assert all(
            e.request_id != second.request_id
            for e in drain_events(svc)
            if e.kind is EventKind.PLAYBACK_STARTED
        )
        assert svc.status().state is ServiceState.IDLE
    finally:
        gate.set()
        svc.close()


# --- automatic language and voice only (M5-4B) ----------------------------------------


@pytest.mark.parametrize(
    ("text", "language", "voice"),
    [
        (EN_TEXT, "en", ENGLISH_VOICE),
        (RU_TEXT, "ru", RUSSIAN_VOICE),
        (HE_TEXT, "he", HEBREW_VOICE),
    ],
)
def test_submit_routes_each_language_to_its_engine_and_default_voice(
    text: str, language: str, voice: str
) -> None:
    sink = FakeSink(write_delay=0.0)
    engines = _engines()
    svc = _service(engines, lambda: sink)
    svc.start()
    try:
        accepted = svc.submit(text)

        assert (accepted.language, accepted.voice_id) == (language, voice)
        wait_for(lambda: len(engines[language].synthesized) == 1)
        assert engines[language].synthesized == [(text, voice)]
    finally:
        svc.close()


@pytest.mark.parametrize("keyword", ["language", "voice"])
def test_submit_has_no_language_or_voice_override(service: ReaderService, keyword: str) -> None:
    with pytest.raises(TypeError):
        service.submit(EN_TEXT, **{keyword: "en"})
