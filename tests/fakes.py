"""Fakes shared by the M4 tests. None of them touch hardware, models or a desktop."""

from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path

from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.clipboard.reader import CommandResult
from reader_assistant.engines.base import AudioBuffer, EngineError, Voice
from reader_assistant.engines.registry import EngineRegistry

RATE = 24000


def tone(samples: int = 2400, value: int = 1000) -> AudioBuffer:
    return AudioBuffer(pcm16=array("h", [value] * samples).tobytes(), sample_rate=RATE)


class FakeSink:
    """An AudioSink that records what it was asked to play.

    ``write_delay`` makes each block take real time, so playback can be interrupted.
    """

    def __init__(self, *, fail_open: bool = False, write_delay: float = 0.0) -> None:
        self._fail_open = fail_open
        self._write_delay = write_delay
        self._lock = threading.Lock()
        self.opened = 0
        self.written = bytearray()
        self.closed_with_drain: list[bool] = []

    def open(self, sample_rate: int) -> None:
        if self._fail_open:
            raise AudioDeviceError("no output device")
        self.opened += 1

    def write(self, pcm16: bytes) -> None:
        if self._write_delay:
            time.sleep(self._write_delay)
        with self._lock:
            self.written.extend(pcm16)

    def close(self, *, drain: bool) -> None:
        with self._lock:
            self.closed_with_drain.append(drain)


class FakeEngine:
    """A TTS engine that records calls. Behaves like the real adapters' contract."""

    def __init__(
        self,
        engine_id: str,
        language: str,
        voice_id: str,
        *,
        fail_load: bool = False,
        fail_synthesize: bool = False,
        gate: threading.Event | None = None,
        samples: int = 2400,
    ) -> None:
        self._samples = samples
        self._engine_id = engine_id
        self._language = language
        self._voice_id = voice_id
        self._fail_load = fail_load
        self._fail_synthesize = fail_synthesize
        self._gate = gate
        self._loaded = False
        self.load_calls = 0
        self.synthesized: list[tuple[str, str]] = []
        self.active = 0  # synthesis calls running right now
        self.max_active = 0  # highest concurrency ever observed
        self._count_lock = threading.Lock()

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def languages(self) -> frozenset[str]:
        return frozenset({self._language})

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def voices(self) -> tuple[Voice, ...]:
        return (Voice(self._voice_id, self._language, "fake"),)

    def load(self) -> None:
        if self._loaded:
            return
        self.load_calls += 1
        if self._fail_load:
            raise EngineError("fake model is missing")
        self._loaded = True

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        if not self._loaded:
            raise EngineError("not loaded")
        with self._count_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self._gate is not None:
                self._gate.wait(timeout=5.0)
            self.synthesized.append((text, voice_id))
            if self._fail_synthesize:
                raise EngineError("fake synthesis failure")
            return tone(self._samples)
        finally:
            with self._count_lock:
                self.active -= 1


def registry_with(*engines: FakeEngine) -> EngineRegistry:
    registry = EngineRegistry()
    for engine in engines:
        registry.register(engine)
    return registry


class FakeClipboardRunner:
    """Stands in for the bounded command runner when calling ``wl-paste``.

    ``listing`` answers ``--list-types``; ``content`` answers the paste call. Either may
    be a CommandResult or an exception to raise. Like the real runner, a result longer
    than ``max_bytes`` is returned as truncated.
    """

    def __init__(
        self,
        *,
        listing: CommandResult | BaseException,
        content: CommandResult | BaseException | None = None,
    ) -> None:
        self._listing = listing
        self._content = content
        self.calls: list[list[str]] = []
        self.max_bytes_requested: list[int] = []

    def __call__(self, args: Sequence[str], timeout: float, max_bytes: int) -> CommandResult:
        self.calls.append(list(args))
        self.max_bytes_requested.append(max_bytes)
        result = self._listing if "--list-types" in args else self._content
        if result is None:
            raise AssertionError("unexpected paste call")
        if isinstance(result, BaseException):
            raise result
        if len(result.stdout) > max_bytes:
            return CommandResult(
                returncode=result.returncode,
                stdout=result.stdout[: max_bytes + 1],
                stderr=result.stderr,
                truncated=True,
            )
        return result


def completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it returns true, or fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def short_dir(base: Path | None = None) -> Path:
    """A short temporary directory. Unix socket paths are limited to 107 bytes."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="ra-", dir=str(base or "/tmp")))
