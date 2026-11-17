"""M5-2: AudioPlayer concurrency and lifecycle.

The sinks here are gated: each block is written only after the test releases a permit.
That makes the order of events deterministic. The only sleeps are for negative checks
("nothing happened"), and they are short.
"""

from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable, Iterator

import pytest

from fakes import FakeSink, tone
from reader_assistant.audio.player import AudioPlayer, PlaybackState
from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.engines.base import AudioBuffer

PLAYER_THREAD = "reader-assistant-playback"
BLOCK = 200  # bytes: 100 samples per block, so short audio spans several blocks


class GatedSink:
    """A sink whose writes wait for permits, and whose open can be held."""

    def __init__(
        self,
        *,
        open_gate: threading.Event | None = None,
        fail_at_block: int | None = None,
        failure: Exception | None = None,
    ) -> None:
        self._open_gate = open_gate
        self._fail_at_block = fail_at_block
        self._failure = failure
        self.permits = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.opened = 0
        self.blocks: list[bytes] = []
        self.closes: list[bool] = []

    def open(self, sample_rate: int) -> None:
        if self._open_gate is not None:
            self._open_gate.wait(timeout=5.0)
        with self._lock:
            self.opened += 1

    def write(self, pcm16: bytes) -> None:
        if not self.permits.acquire(timeout=5.0):
            raise AssertionError("the test never released this block")
        with self._lock:
            index = len(self.blocks)
        if self._fail_at_block is not None and index >= self._fail_at_block:
            raise self._failure or AudioDeviceError("device lost")
        with self._lock:
            self.blocks.append(pcm16)

    def close(self, *, drain: bool) -> None:
        with self._lock:
            self.closes.append(drain)

    def allow(self, count: int) -> None:
        for _ in range(count):
            self.permits.release()

    @property
    def written(self) -> bytes:
        with self._lock:
            return b"".join(self.blocks)


def _audio(samples: int, value: int = 1000) -> AudioBuffer:
    return AudioBuffer(pcm16=array("h", [value] * samples).tobytes(), sample_rate=24000)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.002)


def _state_of(instance: AudioPlayer) -> PlaybackState:
    """Read the state through a function call, so mypy does not narrow the type."""
    return instance.state


def _release_after(sink: GatedSink, delay: float = 0.05) -> threading.Timer:
    """Release one permit shortly, so a worker blocked in write can reach its stop check."""
    timer = threading.Timer(delay, sink.allow, args=(1,))
    timer.start()
    return timer


def _player_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == PLAYER_THREAD and t.is_alive()]


@pytest.fixture
def player() -> Iterator[AudioPlayer]:
    created: list[AudioPlayer] = []

    def make(sink_factory: Callable[[], object]) -> AudioPlayer:
        instance = AudioPlayer(sink_factory, block_bytes=BLOCK)  # type: ignore[arg-type]
        created.append(instance)
        return instance

    yield make  # type: ignore[misc]
    for instance in created:
        instance.close()


# --- concurrency -------------------------------------------------------------------


def test_concurrent_play_and_stop_leave_no_thread_and_close_every_sink_once() -> None:
    sinks: list[FakeSink] = []
    lock = threading.Lock()

    def factory() -> FakeSink:
        created = FakeSink()
        with lock:
            sinks.append(created)
        return created

    instance = AudioPlayer(factory, block_bytes=BLOCK)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for step in range(25):
                if (index + step) % 2:
                    instance.play(_audio(400, value=100 + index))
                else:
                    instance.stop()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    instance.stop()

    assert errors == []
    assert not any(thread.is_alive() for thread in threads), "a caller deadlocked"
    assert _player_threads() == []
    assert len(sinks) > 0
    for sink in sinks:
        assert len(sink.closed_with_drain) == 1, "each sink closes exactly once"
    instance.close()


def test_repeated_stop_is_safe_and_closes_the_sink_once(player: Callable[..., AudioPlayer]) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(400))
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)

    release = _release_after(sink)
    for _ in range(10):
        instance.stop()
    release.join()

    assert sink.closes == [False]
    assert instance.state is PlaybackState.IDLE


# --- stop --------------------------------------------------------------------------


def test_stop_during_playback_closes_without_draining_and_joins(
    player: Callable[..., AudioPlayer],
) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(2000))
    sink.allow(2)
    _wait_until(lambda: len(sink.blocks) == 2)

    release = _release_after(sink)
    instance.stop()
    release.join()

    assert sink.closes == [False]
    assert instance.state is PlaybackState.IDLE
    assert _player_threads() == []


def test_request_stop_returns_at_once_while_the_worker_is_blocked(
    player: Callable[..., AudioPlayer],
) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(2000))
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)  # the worker is now blocked in write

    started = time.monotonic()
    assert instance.request_stop() is True
    assert time.monotonic() - started < 0.5, "request_stop must not wait for the worker"
    assert sink.closes == []  # the worker has not reached cleanup yet

    sink.allow(1)  # let the blocked write return; the worker then sees the stop
    _wait_until(lambda: sink.closes == [False])
    _wait_until(lambda: _player_threads() == [])


def test_stop_during_sink_initialisation_cancels_the_playback(
    player: Callable[..., AudioPlayer],
) -> None:
    open_gate = threading.Event()
    opening = threading.Event()
    sinks: list[GatedSink] = []

    def factory() -> GatedSink:
        created = GatedSink(open_gate=open_gate)
        sinks.append(created)
        opening.set()
        return created

    instance = player(factory)
    caller = threading.Thread(target=instance.play, args=(_audio(400),))
    caller.start()
    assert opening.wait(timeout=5.0)

    # A stop while the device is still opening must not be lost, and must not block.
    assert instance.request_stop() is False  # nothing was running yet
    open_gate.set()
    caller.join(timeout=5.0)

    assert not caller.is_alive()
    assert sinks[0].written == b""
    assert sinks[0].closes == [False]
    assert instance.state is PlaybackState.IDLE


# --- pause, resume, volume ----------------------------------------------------------


def test_pause_halts_writes_and_resume_completes_the_audio(
    player: Callable[..., AudioPlayer],
) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    audio = _audio(1000)  # 2000 bytes = 10 blocks
    instance.play(audio)
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)

    instance.pause()
    assert instance.state is PlaybackState.PAUSED
    sink.allow(20)  # plenty of permits, but a paused worker must not use them
    # A block already in flight may still complete; after that nothing may progress.
    time.sleep(0.05)
    frozen = len(sink.blocks)
    time.sleep(0.05)  # negative check: nothing should happen
    assert len(sink.blocks) == frozen
    assert frozen <= 2

    instance.resume()
    assert _state_of(instance) is PlaybackState.PLAYING
    assert instance.wait_until_finished(5.0)
    assert sink.written == audio.pcm16
    assert sink.closes == [True]


def test_pause_and_resume_are_no_ops_when_idle(player: Callable[..., AudioPlayer]) -> None:
    instance = player(lambda: GatedSink())

    instance.pause()
    instance.resume()

    assert instance.state is PlaybackState.IDLE


def test_volume_change_during_playback_applies_to_later_blocks(
    player: Callable[..., AudioPlayer],
) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(500, value=1000))  # 1000 bytes = 5 blocks
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)

    instance.set_volume(0.5)
    sink.allow(4)
    assert instance.wait_until_finished(5.0)

    played = array("h")
    played.frombytes(sink.written)
    samples_per_block = BLOCK // 2
    assert set(played[:samples_per_block]) == {1000}  # the first block was already read
    # Blocks read after set_volume are scaled. Block 2 may or may not have been read
    # before the change, so only blocks 3 onwards are asserted.
    assert set(played[2 * samples_per_block :]) == {500}


def test_set_volume_validates_and_is_reported(player: Callable[..., AudioPlayer]) -> None:
    instance = player(lambda: GatedSink())

    instance.set_volume(0.25)

    assert instance.volume == 0.25
    with pytest.raises(ValueError):
        instance.set_volume(2.0)
    assert instance.volume == 0.25


# --- callbacks and stale playbacks ------------------------------------------------------


def test_completion_callback_runs_once_with_no_error(player: Callable[..., AudioPlayer]) -> None:
    calls: list[tuple[int, str | None]] = []
    instance = player(lambda: FakeSink())

    instance.play(tone(400), token=7, on_end=lambda t, e: calls.append((t, e)))
    assert instance.wait_until_finished(5.0)
    _wait_until(lambda: calls != [])

    assert calls == [(7, None)]


def test_stopped_playback_never_runs_the_completion_callback(
    player: Callable[..., AudioPlayer],
) -> None:
    calls: list[tuple[int, str | None]] = []
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(2000), token=3, on_end=lambda t, e: calls.append((t, e)))
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)

    release = _release_after(sink)
    instance.stop()
    release.join()

    assert calls == []


def test_stale_callback_is_not_delivered_after_replacement(
    player: Callable[..., AudioPlayer],
) -> None:
    calls: list[tuple[int, str | None]] = []
    first_sink = GatedSink()
    sinks = [first_sink, FakeSink()]
    instance = player(lambda: sinks.pop(0))
    instance.play(_audio(2000), token=1, on_end=lambda t, e: calls.append((t, e)))
    first_sink.allow(1)
    _wait_until(lambda: len(first_sink.blocks) == 1)

    release = _release_after(first_sink)  # lets A's blocked write return so it can stop
    instance.play(_audio(400), token=2, on_end=lambda t, e: calls.append((t, e)))
    release.join()
    assert instance.wait_until_finished(5.0)
    _wait_until(lambda: len(calls) >= 1)
    time.sleep(0.05)

    assert calls == [(2, None)], "the replaced playback must not report an end"


def test_callback_may_call_back_into_the_player_without_deadlock(
    player: Callable[..., AudioPlayer],
) -> None:
    done = threading.Event()
    instance: AudioPlayer | None = None

    def on_end(token: int, error: str | None) -> None:
        assert instance is not None
        instance.request_stop()
        instance.stop()
        done.set()

    instance = player(lambda: FakeSink())
    instance.play(tone(400), token=1, on_end=on_end)

    assert done.wait(timeout=5.0), "a callback calling back into the player deadlocked"


def test_failure_during_playback_is_reported_to_the_callback(
    player: Callable[..., AudioPlayer],
) -> None:
    calls: list[tuple[int, str | None]] = []
    sink = GatedSink(fail_at_block=1, failure=AudioDeviceError("device unplugged"))
    instance = player(lambda: sink)
    instance.play(_audio(2000), token=5, on_end=lambda t, e: calls.append((t, e)))
    sink.allow(3)
    assert instance.wait_until_finished(5.0)
    _wait_until(lambda: len(calls) == 1)

    assert calls[0][0] == 5
    assert calls[0][1] is not None and "device unplugged" in calls[0][1]
    assert instance.last_error is not None
    assert sink.closes == [False]


def test_cleanup_happens_exactly_once_when_stop_races_a_natural_end(
    player: Callable[..., AudioPlayer],
) -> None:
    for _ in range(30):
        sink = FakeSink()
        instance = AudioPlayer(lambda s=sink: s, block_bytes=BLOCK)  # type: ignore[misc]
        instance.play(_audio(200))  # two blocks: it ends almost at once
        instance.request_stop()
        instance.stop()
        instance.close()
        assert len(sink.closed_with_drain) == 1


# --- waiting, closing, termination --------------------------------------------------


def test_wait_until_finished_reports_a_timeout(player: Callable[..., AudioPlayer]) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(400))

    assert instance.wait_until_finished(0.05) is False

    sink.allow(10)
    assert instance.wait_until_finished(5.0) is True


def test_close_terminates_the_worker_and_refuses_further_play(
    player: Callable[..., AudioPlayer],
) -> None:
    sink = GatedSink()
    instance = player(lambda: sink)
    instance.play(_audio(2000))
    sink.allow(1)
    _wait_until(lambda: len(sink.blocks) == 1)

    release = _release_after(sink)
    instance.close()
    release.join()
    instance.close()

    assert _player_threads() == []
    assert sink.closes == [False]
    with pytest.raises(RuntimeError, match="closed"):
        instance.play(_audio(100))


def test_play_rejects_invalid_audio_before_touching_the_device(
    player: Callable[..., AudioPlayer],
) -> None:
    opened: list[int] = []

    def factory() -> FakeSink:
        opened.append(1)
        return FakeSink()

    instance = player(factory)
    with pytest.raises(ValueError):
        instance.play(AudioBuffer(pcm16=b"", sample_rate=24000))

    assert opened == []
