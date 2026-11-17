"""Background playback of synthesised speech.

Design:

- :meth:`AudioPlayer.play` opens the output device synchronously, so a missing device
  is reported to the caller at once, then starts one worker thread that writes the
  audio in small blocks.
- Pause and stop are checked between blocks. Stopping therefore takes at most one
  block's duration (about 0.2 s at 24 kHz with the default block size).
- All shared state is guarded by one condition variable. The worker never calls
  back into the caller and never touches a UI toolkit.
- Every playback ends with the sink closed and the worker thread finished, whether
  it completed, was stopped, or failed.
"""

from __future__ import annotations

import enum
import logging
import sys
import threading
from array import array
from collections.abc import Callable
from typing import Final

from reader_assistant.audio.sink import AudioDeviceError, AudioSink, SinkFactory
from reader_assistant.engines.base import AudioBuffer

_log = logging.getLogger("reader_assistant.audio")

# 4096 samples: about 170 ms at 24 kHz. Smaller blocks stop faster; larger ones
# reduce per-call overhead. Must be an even number of bytes (16-bit samples).
DEFAULT_BLOCK_BYTES: Final = 8192
_STOP_JOIN_TIMEOUT_SECONDS: Final = 5.0
OnEnd = Callable[[int, str | None], None]


class PlaybackState(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class InvalidAudioError(ValueError):
    """The audio to play is empty, malformed, or has an invalid sample rate."""


def validate_audio(audio: AudioBuffer) -> None:
    if audio.sample_rate <= 0:
        raise InvalidAudioError(f"invalid sample rate: {audio.sample_rate}")
    if not audio.pcm16:
        raise InvalidAudioError("audio is empty")
    if len(audio.pcm16) % 2:
        raise InvalidAudioError("audio has an odd number of bytes; expected 16-bit samples")


def scale_pcm16(pcm16: bytes, volume: float) -> bytes:
    """Scale little-endian signed 16-bit PCM by ``volume`` in [0, 1].

    Scaling by a factor of at most 1 cannot overflow, so no clipping is needed.
    """
    if volume >= 1.0:
        return pcm16
    samples = array("h")
    samples.frombytes(pcm16)
    if sys.byteorder == "big":
        samples.byteswap()
    scaled = array("h", (int(sample * volume) for sample in samples))
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes()


class AudioPlayer:
    """Plays one :class:`AudioBuffer` at a time on a background thread.

    The player is safe to call from the GUI thread: every method returns quickly.
    Only :meth:`play` can block, and only while the output device is opened.
    """

    def __init__(
        self,
        sink_factory: SinkFactory,
        *,
        volume: float = 1.0,
        block_bytes: int = DEFAULT_BLOCK_BYTES,
    ) -> None:
        if block_bytes <= 0 or block_bytes % 2:
            raise ValueError("block_bytes must be a positive even number")
        self._sink_factory = sink_factory
        self._block_bytes = block_bytes
        self._condition = threading.Condition()
        self._state = PlaybackState.IDLE
        self._volume = _checked_volume(volume)
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._closed = False

    # --- queries -------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        with self._condition:
            return self._state

    @property
    def volume(self) -> float:
        with self._condition:
            return self._volume

    @property
    def last_error(self) -> str | None:
        """Description of the most recent playback failure, if any."""
        with self._condition:
            return self._last_error

    # --- controls ------------------------------------------------------------

    def play(
        self,
        audio: AudioBuffer,
        *,
        token: int = 0,
        on_end: OnEnd | None = None,
    ) -> None:
        """Start playing ``audio``, replacing any playback in progress.

        ``on_end(token, error)`` is called from the playback thread when this playback
        finishes by itself: ``error`` is None after a normal end, or the failure
        message. It is not called when playback is stopped or replaced, because the
        caller asked for that. Keep the callback short and thread-safe.

        Raises:
            RuntimeError: if the player is closed.
            InvalidAudioError: if the audio is empty or malformed.
            AudioDeviceError: if the output device cannot be opened.
        """
        if self._closed:
            raise RuntimeError("audio player is closed")
        validate_audio(audio)
        self.stop()

        sink = self._sink_factory()
        try:
            sink.open(audio.sample_rate)
        except AudioDeviceError:
            _close_quietly(sink, drain=False)
            with self._condition:
                self._last_error = "audio output could not be opened"
            raise

        with self._condition:
            self._state = PlaybackState.PLAYING
            self._stop_requested = False
            self._last_error = None
        thread = threading.Thread(
            target=self._run,
            args=(sink, audio.pcm16, token, on_end),
            name="reader-assistant-playback",
            daemon=True,
        )
        with self._condition:
            self._thread = thread
        thread.start()

    def pause(self) -> None:
        """Pause at the next block boundary. No effect unless playing."""
        with self._condition:
            if self._state is PlaybackState.PLAYING:
                self._state = PlaybackState.PAUSED

    def resume(self) -> None:
        """Continue after :meth:`pause`. No effect unless paused."""
        with self._condition:
            if self._state is PlaybackState.PAUSED:
                self._state = PlaybackState.PLAYING
                self._condition.notify_all()

    def stop(self) -> None:
        """Stop playback and wait for the worker thread to finish. Idempotent."""
        with self._condition:
            thread = self._thread
            if thread is None:
                self._state = PlaybackState.IDLE
                return
            self._stop_requested = True
            self._condition.notify_all()
        thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
        with self._condition:
            if thread.is_alive():
                self._last_error = "playback did not stop in time"
                _log.error("playback thread did not stop within %s s", _STOP_JOIN_TIMEOUT_SECONDS)
                return
            self._thread = None
            self._state = PlaybackState.IDLE

    def set_volume(self, volume: float) -> None:
        """Set the volume in [0, 1]. Takes effect from the next block."""
        checked = _checked_volume(volume)
        with self._condition:
            self._volume = checked

    def wait_until_finished(self, timeout: float | None = None) -> bool:
        """Block until playback ends. Returns False if the timeout expired first."""
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self) -> None:
        """Stop playback and refuse further use. Idempotent."""
        self.stop()
        self._closed = True

    # --- worker --------------------------------------------------------------

    def _run(
        self,
        sink: AudioSink,
        pcm16: bytes,
        token: int,
        on_end: OnEnd | None,
    ) -> None:
        drain = True
        failure: str | None = None
        try:
            offset = 0
            while offset < len(pcm16):
                with self._condition:
                    while self._state is PlaybackState.PAUSED and not self._stop_requested:
                        self._condition.wait()
                    if self._stop_requested:
                        drain = False
                        break
                    volume = self._volume
                block = pcm16[offset : offset + self._block_bytes]
                sink.write(scale_pcm16(block, volume))
                offset += self._block_bytes
        except AudioDeviceError as exc:
            drain = False
            failure = f"audio output failed: {exc}"
            self._record_failure(failure)
        except Exception as exc:
            drain = False
            failure = f"unexpected playback error: {exc}"
            self._record_failure(failure)
            _log.exception("playback worker failed")
        finally:
            try:
                sink.close(drain=drain)
            except AudioDeviceError as exc:
                failure = failure or f"audio output did not close cleanly: {exc}"
                self._record_failure(f"audio output did not close cleanly: {exc}")
            with self._condition:
                stopped_by_caller = self._stop_requested
                self._state = PlaybackState.IDLE
                self._condition.notify_all()
            if on_end is not None and not stopped_by_caller:
                try:
                    on_end(token, failure)
                except Exception:
                    _log.exception("playback end callback failed")

    def _record_failure(self, message: str) -> None:
        _log.warning(message)
        with self._condition:
            self._last_error = message


def _checked_volume(volume: float) -> float:
    if not 0.0 <= volume <= 1.0:
        raise ValueError(f"volume must be between 0.0 and 1.0, got {volume}")
    return float(volume)


def _close_quietly(sink: AudioSink, *, drain: bool) -> None:
    try:
        sink.close(drain=drain)
    except AudioDeviceError:
        _log.debug("sink close after failed open raised", exc_info=True)
