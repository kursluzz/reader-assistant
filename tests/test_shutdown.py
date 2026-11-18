"""Clean shutdown of ReaderService while idle, synthesising, and playing. No threads left."""

from __future__ import annotations

import threading
import time

from fakes import FakeEngine, FakeSink, registry_with, wait_for
from reader_assistant.app.reader_service import ReaderService, ServiceState
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE


def _service(engine_en: FakeEngine, sink: FakeSink) -> ReaderService:
    engines = registry_with(
        engine_en,
        FakeEngine("piper", "he", HEBREW_VOICE),
        FakeEngine("piper", "ru", RUSSIAN_VOICE),
    )
    svc = ReaderService(engines, AudioPlayer(lambda: sink, block_bytes=8192))
    svc.start()
    return svc


def _service_threads() -> list[threading.Thread]:
    names = {"reader-assistant-speech", "reader-assistant-playback"}
    return [t for t in threading.enumerate() if t.name in names and t.is_alive()]


def test_close_while_idle_leaves_no_threads() -> None:
    svc = _service(FakeEngine("kokoro", "en", ENGLISH_VOICE), FakeSink())

    started = time.monotonic()
    svc.close()

    assert time.monotonic() - started < 2.0
    assert _service_threads() == []


def test_close_during_synthesis_returns_and_discards_the_result() -> None:
    gate = threading.Event()
    sink = FakeSink()
    engine = FakeEngine("kokoro", "en", ENGLISH_VOICE, gate=gate)
    svc = _service(engine, sink)
    svc.submit("A sentence that is being synthesised.")
    wait_for(lambda: svc.status().state is ServiceState.SYNTHESIZING)

    release = threading.Timer(0.1, gate.set)  # let the engine call return after close starts
    release.start()
    svc.close()
    release.join()

    assert _service_threads() == []
    assert sink.opened == 0, "nothing is played after shutdown started"


def test_close_during_playback_stops_the_sink_and_leaves_no_threads() -> None:
    sink = FakeSink(write_delay=0.01)
    engine = FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=120_000)
    svc = _service(engine, sink)
    svc.submit("A long passage that is still playing when the service closes.")
    wait_for(lambda: svc.status().state is ServiceState.PLAYING)
    wait_for(lambda: len(sink.written) > 0)

    svc.close()

    assert _service_threads() == []
    assert sink.closed_with_drain == [False], "the sink is released exactly once, without draining"
