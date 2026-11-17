from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable

import pytest

from reader_assistant.audio.player import (
    AudioPlayer,
    InvalidAudioError,
    PlaybackState,
    scale_pcm16,
)
from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.engines.base import AudioBuffer

PLAYBACK_THREAD_NAME = "reader-assistant-playback"
RATE = 24000


class FakeSink:
    """In-memory AudioSink. Records what would have been played."""

    def __init__(
        self,
        *,
        fail_open: bool = False,
        fail_write_at: int | None = None,
        write_error: Exception | None = None,
        write_delay: float = 0.0,
    ) -> None:
        self._fail_open = fail_open
        self._fail_write_at = fail_write_at
        self._write_error = write_error
        self._write_delay = write_delay
        self._lock = threading.Lock()
        self.opened_rate: int | None = None
        self.blocks: list[bytes] = []
        self.closed_with_drain: list[bool] = []

    def open(self, sample_rate: int) -> None:
        if self._fail_open:
            raise AudioDeviceError("no output device")
        self.opened_rate = sample_rate

    def write(self, pcm16: bytes) -> None:
        with self._lock:
            index = len(self.blocks)
        if self._fail_write_at is not None and index >= self._fail_write_at:
            raise self._write_error or AudioDeviceError("device lost")
        if self._write_delay:
            time.sleep(self._write_delay)
        with self._lock:
            self.blocks.append(pcm16)

    def close(self, *, drain: bool) -> None:
        with self._lock:
            self.closed_with_drain.append(drain)

    @property
    def written(self) -> bytes:
        with self._lock:
            return b"".join(self.blocks)


def make_audio(samples: int, value: int = 1000, rate: int = RATE) -> AudioBuffer:
    return AudioBuffer(pcm16=array("h", [value] * samples).tobytes(), sample_rate=rate)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.002)


def state_of(player: AudioPlayer) -> PlaybackState:
    """Read the state through a function call, so mypy does not narrow the type."""
    return player.state


def playback_threads_alive() -> bool:
    return any(t.name == PLAYBACK_THREAD_NAME and t.is_alive() for t in threading.enumerate())


# --- pure helpers ------------------------------------------------------------


def test_scale_pcm16_halves_samples_and_truncates_towards_zero() -> None:
    pcm = array("h", [1000, -1000, 0, 32767, -32768]).tobytes()

    scaled = array("h")
    scaled.frombytes(scale_pcm16(pcm, 0.5))

    assert scaled.tolist() == [500, -500, 0, 16383, -16384]


def test_scale_pcm16_at_full_volume_returns_input_unchanged() -> None:
    pcm = array("h", [1, 2, 3]).tobytes()

    assert scale_pcm16(pcm, 1.0) is pcm


# --- playback ----------------------------------------------------------------


def test_play_writes_all_audio_and_returns_to_idle() -> None:
    sink = FakeSink()
    player = AudioPlayer(lambda: sink, block_bytes=200)
    audio = make_audio(5000)

    player.play(audio)

    assert player.wait_until_finished(5.0)
    assert sink.opened_rate == RATE
    assert sink.written == audio.pcm16
    assert sink.closed_with_drain == [True]
    assert player.state is PlaybackState.IDLE
    assert player.last_error is None


def test_pause_halts_writes_and_resume_continues_to_the_end() -> None:
    sink = FakeSink(write_delay=0.005)
    player = AudioPlayer(lambda: sink, block_bytes=200)
    audio = make_audio(5000)  # 50 blocks

    player.play(audio)
    wait_for(lambda: len(sink.blocks) >= 3)
    player.pause()
    assert player.state is PlaybackState.PAUSED

    time.sleep(0.05)  # allow the one block that was already being written
    frozen = len(sink.blocks)
    time.sleep(0.1)
    assert len(sink.blocks) == frozen
    assert player.state is PlaybackState.PAUSED

    player.resume()
    assert state_of(player) is PlaybackState.PLAYING
    assert player.wait_until_finished(5.0)
    assert sink.written == audio.pcm16
    assert state_of(player) is PlaybackState.IDLE


def test_pause_and_resume_do_nothing_when_idle() -> None:
    player = AudioPlayer(lambda: FakeSink())

    player.pause()
    assert player.state is PlaybackState.IDLE
    player.resume()
    assert player.state is PlaybackState.IDLE


def test_resume_does_nothing_while_playing() -> None:
    sink = FakeSink(write_delay=0.005)
    player = AudioPlayer(lambda: sink, block_bytes=200)
    player.play(make_audio(5000))

    player.resume()

    assert player.state is PlaybackState.PLAYING
    player.stop()


def test_stop_halts_playback_closes_sink_without_draining_and_joins_thread() -> None:
    sink = FakeSink(write_delay=0.005)
    player = AudioPlayer(lambda: sink, block_bytes=200)
    player.play(make_audio(5000))
    wait_for(lambda: len(sink.blocks) >= 2)

    player.stop()

    assert player.state is PlaybackState.IDLE
    assert len(sink.blocks) < 50
    assert sink.closed_with_drain == [False]
    assert not playback_threads_alive()


def test_stop_is_idempotent_and_safe_when_idle() -> None:
    player = AudioPlayer(lambda: FakeSink())

    player.stop()
    player.stop()

    assert player.state is PlaybackState.IDLE


def test_play_replaces_playback_in_progress() -> None:
    sinks: list[FakeSink] = []

    def factory() -> FakeSink:
        sink = FakeSink(write_delay=0.005)
        sinks.append(sink)
        return sink

    player = AudioPlayer(factory, block_bytes=200)
    player.play(make_audio(5000))
    wait_for(lambda: len(sinks) == 1 and len(sinks[0].blocks) >= 2)
    second = make_audio(600, value=-300)

    player.play(second)

    assert player.wait_until_finished(5.0)
    assert len(sinks) == 2
    assert sinks[0].closed_with_drain == [False]
    assert sinks[1].written == second.pcm16
    assert sinks[1].closed_with_drain == [True]


def test_worker_thread_is_cleaned_up_after_completion() -> None:
    player = AudioPlayer(lambda: FakeSink(), block_bytes=200)

    player.play(make_audio(1000))
    assert player.wait_until_finished(5.0)

    assert not playback_threads_alive()


def test_playback_runs_on_a_background_thread() -> None:
    main_thread = threading.get_ident()
    seen: list[int] = []

    class ThreadRecordingSink(FakeSink):
        def write(self, pcm16: bytes) -> None:
            seen.append(threading.get_ident())
            super().write(pcm16)

    player = AudioPlayer(lambda: ThreadRecordingSink(), block_bytes=200)
    player.play(make_audio(1000))
    assert player.wait_until_finished(5.0)

    assert seen
    assert all(ident != main_thread for ident in seen)


# --- volume ------------------------------------------------------------------


def test_volume_is_applied_to_played_audio() -> None:
    sink = FakeSink()
    player = AudioPlayer(lambda: sink, volume=0.5)
    audio = AudioBuffer(pcm16=array("h", [1000, -1000, 32767]).tobytes(), sample_rate=RATE)

    player.play(audio)
    assert player.wait_until_finished(5.0)

    played = array("h")
    played.frombytes(sink.written)
    assert played.tolist() == [500, -500, 16383]


def test_zero_volume_plays_silence() -> None:
    sink = FakeSink()
    player = AudioPlayer(lambda: sink, volume=0.0)

    player.play(make_audio(100, value=5000))
    assert player.wait_until_finished(5.0)

    assert set(array("h", sink.written)) == {0}


def test_set_volume_takes_effect_for_the_next_playback() -> None:
    sink = FakeSink()
    player = AudioPlayer(lambda: sink)
    player.set_volume(0.25)
    assert player.volume == 0.25

    player.play(make_audio(4, value=400))
    assert player.wait_until_finished(5.0)

    assert array("h", sink.written).tolist() == [100, 100, 100, 100]


@pytest.mark.parametrize("bad", [-0.1, 1.01, float("nan")])
def test_invalid_volume_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="volume must be between"):
        AudioPlayer(lambda: FakeSink(), volume=bad)

    player = AudioPlayer(lambda: FakeSink())
    with pytest.raises(ValueError, match="volume must be between"):
        player.set_volume(bad)


# --- invalid audio -----------------------------------------------------------


@pytest.mark.parametrize(
    ("audio", "message"),
    [
        (AudioBuffer(pcm16=b"", sample_rate=RATE), "audio is empty"),
        (AudioBuffer(pcm16=b"\x00", sample_rate=RATE), "odd number of bytes"),
        (AudioBuffer(pcm16=b"\x00\x00", sample_rate=0), "invalid sample rate"),
    ],
)
def test_invalid_audio_is_rejected_before_any_device_is_opened(
    audio: AudioBuffer, message: str
) -> None:
    calls: list[int] = []

    def factory() -> FakeSink:
        calls.append(1)
        return FakeSink()

    player = AudioPlayer(factory)

    with pytest.raises(InvalidAudioError, match=message):
        player.play(audio)

    assert calls == []
    assert player.state is PlaybackState.IDLE


def test_block_size_must_be_positive_and_even() -> None:
    for bad in (0, -2, 3):
        with pytest.raises(ValueError, match="block_bytes"):
            AudioPlayer(lambda: FakeSink(), block_bytes=bad)


# --- device and runtime failures ---------------------------------------------


def test_missing_device_is_reported_to_the_caller_and_state_stays_idle() -> None:
    sink = FakeSink(fail_open=True)
    player = AudioPlayer(lambda: sink)

    with pytest.raises(AudioDeviceError, match="no output device"):
        player.play(make_audio(100))

    assert player.state is PlaybackState.IDLE
    assert player.last_error == "audio output could not be opened"
    assert sink.closed_with_drain == [False]
    assert not playback_threads_alive()


def test_sink_factory_failure_propagates_without_a_thread() -> None:
    def factory() -> FakeSink:
        raise AudioDeviceError("backend unavailable")

    player = AudioPlayer(factory)

    with pytest.raises(AudioDeviceError, match="backend unavailable"):
        player.play(make_audio(100))

    assert player.state is PlaybackState.IDLE
    assert not playback_threads_alive()


def test_device_failure_during_playback_is_recorded_and_sink_closed() -> None:
    sink = FakeSink(fail_write_at=2, write_error=AudioDeviceError("device lost"))
    player = AudioPlayer(lambda: sink, block_bytes=200)

    player.play(make_audio(5000))
    assert player.wait_until_finished(5.0)

    assert player.state is PlaybackState.IDLE
    assert player.last_error is not None
    assert "device lost" in player.last_error
    assert sink.closed_with_drain == [False]
    assert not playback_threads_alive()


def test_unexpected_error_during_playback_does_not_escape_the_thread() -> None:
    sink = FakeSink(fail_write_at=1, write_error=RuntimeError("boom"))
    player = AudioPlayer(lambda: sink, block_bytes=200)

    player.play(make_audio(1000))
    assert player.wait_until_finished(5.0)

    assert player.state is PlaybackState.IDLE
    assert player.last_error == "unexpected playback error: boom"
    assert sink.closed_with_drain == [False]


# --- lifecycle ---------------------------------------------------------------


def test_construction_does_not_open_any_device() -> None:
    calls: list[int] = []

    def factory() -> FakeSink:
        calls.append(1)
        return FakeSink()

    player = AudioPlayer(factory)

    assert calls == []
    assert player.state is PlaybackState.IDLE


def test_close_stops_playback_and_refuses_further_use() -> None:
    sink = FakeSink(write_delay=0.005)
    player = AudioPlayer(lambda: sink, block_bytes=200)
    player.play(make_audio(5000))
    wait_for(lambda: len(sink.blocks) >= 1)

    player.close()
    player.close()

    assert player.state is PlaybackState.IDLE
    assert not playback_threads_alive()
    with pytest.raises(RuntimeError, match="closed"):
        player.play(make_audio(100))
