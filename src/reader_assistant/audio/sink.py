"""Audio output sinks: the only module that talks to the audio hardware.

The player depends on the :class:`AudioSink` protocol, so tests use an in-memory
fake and never open a device. :class:`SoundDeviceSink` is the real implementation
on top of PortAudio through the optional ``audio`` extra.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Protocol

SAMPLE_WIDTH_BYTES: Final = 2  # signed 16-bit PCM
CHANNELS: Final = 1  # mono


class AudioDeviceError(Exception):
    """No usable audio output is available, or the output failed."""


class AudioSink(Protocol):
    """Receives mono 16-bit PCM and plays it on an output device."""

    def open(self, sample_rate: int) -> None:
        """Open the output device. Raises AudioDeviceError if unavailable."""
        ...

    def write(self, pcm16: bytes) -> None:
        """Play one block of PCM. Blocks until the device accepts it."""
        ...

    def close(self, *, drain: bool) -> None:
        """Release the device. With ``drain`` the queued audio is played first,
        otherwise it is discarded immediately."""
        ...


SinkFactory = Callable[[], AudioSink]


class SoundDeviceSink:
    """Output through PortAudio, using the ``sounddevice`` package.

    Requires the system library ``libportaudio2`` and the ``audio`` extra. The
    package is imported only when :meth:`open` runs, so the rest of the application
    works without it.
    """

    def __init__(self) -> None:
        self._stream: Any = None

    def open(self, sample_rate: int) -> None:
        if self._stream is not None:
            raise AudioDeviceError("audio output is already open")
        try:
            import sounddevice  # optional dependency, imported lazily
        except (ImportError, OSError) as exc:
            # OSError is raised when the PortAudio system library is missing.
            raise AudioDeviceError(f"audio backend unavailable: {exc}") from exc
        try:
            stream = sounddevice.RawOutputStream(
                samplerate=sample_rate, channels=CHANNELS, dtype="int16"
            )
            stream.start()
        except Exception as exc:
            raise AudioDeviceError(f"cannot open audio output: {exc}") from exc
        self._stream = stream

    def write(self, pcm16: bytes) -> None:
        if self._stream is None:
            raise AudioDeviceError("audio output is not open")
        try:
            self._stream.write(pcm16)
        except Exception as exc:
            raise AudioDeviceError(f"audio output failed: {exc}") from exc

    def close(self, *, drain: bool) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if drain:
                stream.stop()  # waits for queued audio to play
            else:
                stream.abort()  # discards queued audio immediately
        finally:
            stream.close()


def default_sink() -> AudioSink:
    """Factory used by the application."""
    return SoundDeviceSink()
