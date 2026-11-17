from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from fakes import FakeEngine, FakeSink, registry_with, tone, wait_for
from reader_assistant.app.reader_service import ReaderService, SpeechError
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.catalog import DEFAULT_VOICE_BY_LANGUAGE
from reader_assistant.ipc.protocol import CODE_EMPTY_TEXT, CODE_TOO_LARGE, CODE_UNSUPPORTED
from reader_assistant.limits import MAX_TEXT_BYTES

TEXTS = {
    "en": "Hello, this is an English sentence.",
    "ru": "Привет, это русская фраза для проверки.",
    "he": "שלום, זהו משפט עברי לבדיקה.",
}


def _engines(**overrides: FakeEngine) -> dict[str, FakeEngine]:
    engines = {
        "en": FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"]),
        "he": FakeEngine("piper", "he", DEFAULT_VOICE_BY_LANGUAGE["he"]),
        "ru": FakeEngine("piper", "ru", DEFAULT_VOICE_BY_LANGUAGE["ru"]),
    }
    engines.update(overrides)
    return engines


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def service_and_engines(sink: FakeSink) -> Iterator[tuple[ReaderService, dict[str, FakeEngine]]]:
    engines = _engines()
    service = ReaderService(
        registry_with(*engines.values()),
        AudioPlayer(lambda: sink),
    )
    service.start()
    try:
        yield service, engines
    finally:
        service.close()


@pytest.mark.parametrize("language", ["en", "he", "ru"])
def test_each_language_reaches_its_engine_and_voice_and_is_played(
    service_and_engines: tuple[ReaderService, dict[str, FakeEngine]],
    sink: FakeSink,
    language: str,
) -> None:
    service, engines = service_and_engines
    text = TEXTS[language]

    accepted = service.submit(text)

    assert accepted.language == language
    assert accepted.voice_id == DEFAULT_VOICE_BY_LANGUAGE[language]
    wait_for(lambda: len(engines[language].synthesized) == 1)
    assert engines[language].synthesized == [(text, DEFAULT_VOICE_BY_LANGUAGE[language])]
    wait_for(lambda: bool(sink.closed_with_drain))
    assert sink.written  # the generated audio reached the sink
    for other, engine in engines.items():
        if other != language:
            assert engine.synthesized == []


def test_engines_load_lazily_on_first_speech(
    service_and_engines: tuple[ReaderService, dict[str, FakeEngine]],
) -> None:
    service, engines = service_and_engines
    assert engines["he"].load_calls == 0

    service.submit(TEXTS["he"])
    wait_for(lambda: engines["he"].load_calls == 1)

    assert engines["he"].is_loaded
    assert engines["en"].load_calls == 0  # other engines stay unloaded


def test_a_loaded_engine_is_reused_not_reloaded(
    service_and_engines: tuple[ReaderService, dict[str, FakeEngine]],
    sink: FakeSink,
) -> None:
    service, engines = service_and_engines

    service.submit(TEXTS["en"])
    wait_for(lambda: len(engines["en"].synthesized) == 1)
    service.submit("Second English sentence.")
    wait_for(lambda: len(engines["en"].synthesized) == 2)

    assert engines["en"].load_calls == 1


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", CODE_EMPTY_TEXT),
        ("   \n", CODE_EMPTY_TEXT),
        ("12345 !!!", CODE_UNSUPPORTED),
        ("你好世界", CODE_UNSUPPORTED),
        ("a" * (MAX_TEXT_BYTES + 1), CODE_TOO_LARGE),
    ],
)
def test_invalid_text_is_rejected_before_any_engine_work(
    service_and_engines: tuple[ReaderService, dict[str, FakeEngine]],
    text: str,
    code: str,
) -> None:
    service, engines = service_and_engines

    with pytest.raises(SpeechError) as excinfo:
        service.submit(text)

    assert excinfo.value.code == code
    assert all(engine.load_calls == 0 for engine in engines.values())


def test_engine_load_failure_is_recorded_and_not_played(sink: FakeSink) -> None:
    engines = _engines(
        he=FakeEngine("piper", "he", DEFAULT_VOICE_BY_LANGUAGE["he"], fail_load=True)
    )
    service = ReaderService(registry_with(*engines.values()), AudioPlayer(lambda: sink))
    service.start()
    try:
        service.submit(TEXTS["he"])
        wait_for(lambda: service.last_error is not None)

        assert service.last_error is not None
        assert service.last_error.startswith("engine_failed")
        assert "fake model is missing" in service.last_error
        assert sink.opened == 0
    finally:
        service.close()


def test_synthesis_failure_is_recorded_and_not_played(sink: FakeSink) -> None:
    engines = _engines(
        en=FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"], fail_synthesize=True)
    )
    service = ReaderService(registry_with(*engines.values()), AudioPlayer(lambda: sink))
    service.start()
    try:
        service.submit(TEXTS["en"])
        wait_for(lambda: service.last_error is not None)

        assert service.last_error is not None
        assert service.last_error.startswith("engine_failed")
        assert sink.opened == 0
    finally:
        service.close()


def test_audio_device_failure_is_recorded(sink: FakeSink) -> None:
    failing_sink = FakeSink(fail_open=True)
    service = ReaderService(registry_with(*_engines().values()), AudioPlayer(lambda: failing_sink))
    service.start()
    try:
        service.submit(TEXTS["en"])
        wait_for(lambda: service.last_error is not None)

        assert service.last_error is not None
        assert service.last_error.startswith("audio_failed")
        assert "no output device" in service.last_error
    finally:
        service.close()


def test_newer_request_replaces_one_that_has_not_started(sink: FakeSink) -> None:
    gate = threading.Event()
    engines = _engines(en=FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"], gate=gate))
    service = ReaderService(registry_with(*engines.values()), AudioPlayer(lambda: sink))
    service.start()
    try:
        service.submit("First request, which starts and blocks.")
        wait_for(lambda: engines["en"].is_loaded)
        service.submit("Second request, dropped.")
        service.submit("Third request, kept.")
        gate.set()

        wait_for(lambda: len(engines["en"].synthesized) == 2)
        assert [text for text, _ in engines["en"].synthesized] == [
            "First request, which starts and blocks.",
            "Third request, kept.",
        ]
    finally:
        gate.set()
        service.close()


def test_default_language_is_used_for_ties(sink: FakeSink) -> None:
    engines = _engines()
    service = ReaderService(
        registry_with(*engines.values()),
        AudioPlayer(lambda: sink),
        default_language="ru",
    )
    service.start()
    try:
        accepted = service.submit("ab вв")  # two English and two Russian letters

        assert accepted.language == "ru"
    finally:
        service.close()


def test_submit_after_close_is_rejected(sink: FakeSink) -> None:
    service = ReaderService(registry_with(*_engines().values()), AudioPlayer(lambda: sink))
    service.start()
    service.close()
    service.close()  # idempotent

    with pytest.raises(SpeechError) as excinfo:
        service.submit(TEXTS["en"])

    assert excinfo.value.code == "shutting_down"


def test_close_stops_the_worker_thread(sink: FakeSink) -> None:
    service = ReaderService(registry_with(*_engines().values()), AudioPlayer(lambda: sink))
    service.start()

    service.close()

    assert not any(
        t.name == "reader-assistant-speech" and t.is_alive() for t in threading.enumerate()
    )


def test_start_is_idempotent(sink: FakeSink) -> None:
    service = ReaderService(registry_with(*_engines().values()), AudioPlayer(lambda: sink))
    service.start()
    service.start()  # must not raise or start a second thread
    service.close()

    assert not any(
        t.name == "reader-assistant-speech" and t.is_alive() for t in threading.enumerate()
    )


# --- latest request wins: deterministic burst, interruption and concurrency ----


def _threads(name: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name == name and t.is_alive())


def test_a_burst_of_requests_keeps_only_the_first_running_and_the_newest_pending(
    sink: FakeSink,
) -> None:
    gate = threading.Event()
    engine = FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"], gate=gate)
    service = ReaderService(registry_with(engine), AudioPlayer(lambda: sink))
    service.start()
    try:
        service.submit("Request one, starts and blocks.")
        wait_for(lambda: engine.active == 1)
        for text in ("Request two.", "Request three.", "Request four.", "Request five."):
            service.submit(text)
        gate.set()

        wait_for(lambda: len(engine.synthesized) == 2)
        assert [text for text, _ in engine.synthesized] == [
            "Request one, starts and blocks.",
            "Request five.",
        ]
    finally:
        gate.set()
        service.close()


def test_only_one_synthesis_runs_at_a_time_even_under_a_burst(sink: FakeSink) -> None:
    gate = threading.Event()
    engine = FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"], gate=gate)
    service = ReaderService(registry_with(engine), AudioPlayer(lambda: sink))
    service.start()
    try:
        service.submit("Burst request 0.")
        wait_for(lambda: engine.active == 1)  # the first request is now synthesising
        for index in range(1, 6):
            service.submit(f"Burst request {index}.")
        assert _threads("reader-assistant-speech") == 1
        gate.set()
        wait_for(lambda: len(engine.synthesized) == 2)
        assert engine.max_active == 1
    finally:
        gate.set()
        service.close()


def test_a_new_request_interrupts_playback_already_in_progress() -> None:
    sinks: list[FakeSink] = []

    def factory() -> FakeSink:
        sink = FakeSink(write_delay=0.01)
        sinks.append(sink)
        return sink

    # 120,000 samples = 5 s of audio, which takes about 0.3 s to play in this fake.
    engine = FakeEngine("kokoro", "en", DEFAULT_VOICE_BY_LANGUAGE["en"], samples=120_000)
    player = AudioPlayer(factory)
    service = ReaderService(registry_with(engine), player)
    service.start()
    try:
        service.submit("First long passage.")
        wait_for(lambda: len(sinks) == 1 and len(sinks[0].written) > 0)
        first_full = len(tone(120_000).pcm16)

        service.submit("Second passage replaces it.")
        wait_for(lambda: len(sinks) == 2 and bool(sinks[1].closed_with_drain))

        assert sinks[0].closed_with_drain == [False], "the first playback was interrupted"
        assert len(sinks[0].written) < first_full
        assert sinks[1].written == bytearray(tone(120_000).pcm16)
        assert _threads("reader-assistant-playback") == 0
    finally:
        service.close()
