from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from reader_assistant.audio.sink import AudioDeviceError, SoundDeviceSink


class FakeStream:
    def __init__(self, *, fail_start: bool = False) -> None:
        self._fail_start = fail_start
        self.calls: list[str] = []
        self.written: list[bytes] = []

    def start(self) -> None:
        if self._fail_start:
            raise OSError("PortAudioError: no device")
        self.calls.append("start")

    def write(self, data: bytes) -> None:
        self.written.append(data)
        self.calls.append("write")

    def stop(self) -> None:
        self.calls.append("stop")

    def abort(self) -> None:
        self.calls.append("abort")

    def close(self) -> None:
        self.calls.append("close")


def _install_fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch, stream: FakeStream
) -> dict[str, Any]:
    """Provide a fake ``sounddevice`` module; no audio hardware is touched."""
    recorded: dict[str, Any] = {}
    module = types.ModuleType("sounddevice")

    def raw_output_stream(**kwargs: Any) -> FakeStream:
        recorded.update(kwargs)
        return stream

    module.RawOutputStream = raw_output_stream  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return recorded


def test_missing_sounddevice_package_is_reported_as_device_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # makes the import fail

    with pytest.raises(AudioDeviceError, match="audio backend unavailable"):
        SoundDeviceSink().open(24000)


def test_open_configures_a_mono_int16_stream_and_starts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream()
    recorded = _install_fake_sounddevice(monkeypatch, stream)
    sink = SoundDeviceSink()

    sink.open(24000)
    sink.write(b"\x01\x00\x02\x00")
    sink.close(drain=True)

    assert recorded == {"samplerate": 24000, "channels": 1, "dtype": "int16"}
    assert stream.written == [b"\x01\x00\x02\x00"]
    assert stream.calls == ["start", "write", "stop", "close"]


def test_close_without_drain_aborts_queued_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream()
    _install_fake_sounddevice(monkeypatch, stream)
    sink = SoundDeviceSink()
    sink.open(24000)

    sink.close(drain=False)

    assert stream.calls == ["start", "abort", "close"]


def test_stream_start_failure_is_reported_as_device_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sounddevice(monkeypatch, FakeStream(fail_start=True))

    with pytest.raises(AudioDeviceError, match="cannot open audio output"):
        SoundDeviceSink().open(24000)


def test_write_before_open_is_an_error() -> None:
    with pytest.raises(AudioDeviceError, match="not open"):
        SoundDeviceSink().write(b"\x00\x00")


def test_opening_twice_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sounddevice(monkeypatch, FakeStream())
    sink = SoundDeviceSink()
    sink.open(24000)

    with pytest.raises(AudioDeviceError, match="already open"):
        sink.open(24000)


def test_close_is_idempotent_and_safe_when_never_opened() -> None:
    sink = SoundDeviceSink()

    sink.close(drain=True)
    sink.close(drain=False)
