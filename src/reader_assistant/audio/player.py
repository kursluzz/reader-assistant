"""Background playback of synthesised speech.

Design:

- Each playback is a :class:`_Session` with its own sink, audio, flags and worker
  thread. Every decision (is this still the current playback? has the sink been closed?
  should the end callback run?) looks at the session it concerns, never at a flag shared
  with other playbacks. A stale worker therefore cannot affect a newer one.
- Two locks, with fixed roles:

  * ``_lifecycle`` serialises :meth:`play` and :meth:`close`. It is held while the
    previous playback is joined and while the output device is opened. Nothing else
    takes it, so stop, pause, resume and volume never wait on it.
  * ``_cond`` guards the current session pointer, the flags, the stop generation, the
    volume and the last error. It is held only for short reads and writes, never while
    waiting for a thread, opening a device or calling back into the caller.

- :meth:`request_stop` is the non-blocking stop: it marks the current playback as
  stopped and returns. :meth:`stop` does that and then waits for the worker to finish.
- The worker owns its cleanup. It closes its sink exactly once, and it runs the end
  callback only when it finished by itself, not when it was stopped or replaced.
- A stop also bumps a generation counter, so a play that is still opening its device
  notices it was cancelled and does not start.
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
_JOIN_TIMEOUT_SECONDS: Final = 5.0
_THREAD_NAME: Final = "reader-assistant-playback"
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


class _Session:
    """One playback. Its flags are read and written only under the player's ``_cond``."""

    def __init__(
        self,
        token: int,
        sink: AudioSink,
        pcm16: bytes,
        on_end: OnEnd | None,
    ) -> None:
        self.token = token
        self.sink = sink
        self.pcm16 = pcm16
        self.on_end = on_end
        self.stop_event = threading.Event()
        self.paused = False
        self.sink_closed = False
        self.thread: threading.Thread | None = None
        self.finished = threading.Event()


class AudioPlayer:
    """Plays one :class:`AudioBuffer` at a time on a background thread.

    Safe to call from any thread. Only :meth:`play` and :meth:`close` can block, and only
    while the previous playback ends and the device opens. The stop, pause, resume and
    volume methods return at once.
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
        self._lifecycle = threading.Lock()
        self._cond = threading.Condition()
        self._session: _Session | None = None
        # Sessions asked to stop whose worker has not finished its cleanup yet. A stop
        # waits for these too, even if request_stop() already detached them.
        self._retiring: list[_Session] = []
        self._generation = 0
        self._volume = _checked_volume(volume)
        self._last_error: str | None = None
        self._closed = False

    # --- queries -------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        with self._cond:
            session = self._session
            if session is None:
                return PlaybackState.IDLE
            return PlaybackState.PAUSED if session.paused else PlaybackState.PLAYING

    @property
    def volume(self) -> float:
        with self._cond:
            return self._volume

    @property
    def last_error(self) -> str | None:
        """Description of the most recent playback failure, if any."""
        with self._cond:
            return self._last_error

    # --- playback ------------------------------------------------------------

    def play(
        self,
        audio: AudioBuffer,
        *,
        token: int = 0,
        on_end: OnEnd | None = None,
    ) -> None:
        """Start playing ``audio``, replacing any playback in progress.

        ``on_end(token, error)`` runs on the playback thread when this playback finishes by
        itself: ``error`` is None after a normal end, or the failure message. It does not run
        when the playback is stopped or replaced, because the caller asked for that. It runs
        outside every lock, so it may call back into the player.

        Raises:
            RuntimeError: if the player is closed.
            InvalidAudioError: if the audio is empty or malformed.
            AudioDeviceError: if the output device cannot be opened.
        """
        validate_audio(audio)
        with self._lifecycle:
            if self._closed:
                raise RuntimeError("audio player is closed")
            with self._cond:
                generation = self._generation
            self._detach()
            self._join_retiring()

            sink = self._sink_factory()
            try:
                sink.open(audio.sample_rate)
            except AudioDeviceError:
                _close_quietly(sink, drain=False)
                with self._cond:
                    self._last_error = "audio output could not be opened"
                raise

            session = _Session(token, sink, audio.pcm16, on_end)
            thread = threading.Thread(
                target=self._run,
                args=(session,),
                name=_THREAD_NAME,
                daemon=True,
            )
            session.thread = thread
            with self._cond:
                if self._generation != generation:
                    cancelled = True  # a stop arrived while the device was opening
                else:
                    cancelled = False
                    self._session = session
                    self._last_error = None
                    thread.start()
            if cancelled:
                self._close_sink(session, drain=False)

    def pause(self) -> None:
        """Pause at the next block boundary. No effect unless playing."""
        with self._cond:
            session = self._session
            if session is not None and not session.paused and not session.stop_event.is_set():
                session.paused = True
                self._cond.notify_all()

    def resume(self) -> None:
        """Continue after :meth:`pause`. No effect unless paused."""
        with self._cond:
            session = self._session
            if session is not None and session.paused:
                session.paused = False
                self._cond.notify_all()

    def request_stop(self) -> bool:
        """Ask the current playback to stop, without waiting. Returns True if one was running.

        The worker stops at the next block boundary and closes its own sink. A stop request
        during device opening cancels that playback as well.
        """
        with self._cond:
            self._generation += 1
            session = self._detach_locked()
        return session is not None

    def stop(self) -> None:
        """Stop playback and wait for the worker to finish. Idempotent.

        Waits at most about one block, plus a short timeout. Use :meth:`request_stop` where
        waiting is not wanted.
        """
        with self._cond:
            self._generation += 1
            self._detach_locked()
        self._join_retiring()

    def set_volume(self, volume: float) -> None:
        """Set the volume in [0, 1]. Takes effect from the next block."""
        checked = _checked_volume(volume)
        with self._cond:
            self._volume = checked

    def wait_until_finished(self, timeout: float | None = None) -> bool:
        """Block until the current playback ends. Returns False if the timeout expired first."""
        with self._cond:
            session = self._session
        if session is None:
            return True
        return session.finished.wait(timeout)

    def close(self) -> None:
        """Stop playback and refuse further use. Idempotent."""
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            self.stop()

    # --- internals -----------------------------------------------------------

    def _detach(self) -> None:
        """Mark the current playback stopped and forget it. Does not wait."""
        with self._cond:
            self._detach_locked()

    def _detach_locked(self) -> _Session | None:
        session = self._session
        self._session = None
        if session is not None:
            session.stop_event.set()
            self._retiring.append(session)
            self._cond.notify_all()
        return session

    def _join_retiring(self) -> None:
        """Wait for every stopped playback to finish its cleanup. Holds no lock while waiting."""
        with self._cond:
            sessions = list(self._retiring)
        for session in sessions:
            thread = session.thread
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                _log.error("playback did not stop within %s s", _JOIN_TIMEOUT_SECONDS)
                self._record_failure("playback did not stop in time")

    def _run(self, session: _Session) -> None:
        drain = True
        failure: str | None = None
        try:
            offset = 0
            pcm16 = session.pcm16
            while offset < len(pcm16):
                with self._cond:
                    while session.paused and not session.stop_event.is_set():
                        self._cond.wait()
                    if session.stop_event.is_set():
                        drain = False
                        break
                    volume = self._volume
                block = pcm16[offset : offset + self._block_bytes]
                session.sink.write(scale_pcm16(block, volume))
                offset += self._block_bytes
        except AudioDeviceError as exc:
            drain = False
            failure = f"audio output failed: {exc}"
        except Exception as exc:
            drain = False
            failure = f"unexpected playback error: {exc}"
            _log.exception("playback worker failed")
        finally:
            if failure is not None:
                self._record_failure(failure)
            close_error = self._close_sink(session, drain=drain)
            if close_error is not None:
                failure = failure or f"audio output did not close cleanly: {close_error}"
                self._record_failure(f"audio output did not close cleanly: {close_error}")
            with self._cond:
                owner = self._session is session
                if owner:
                    self._session = None
                    self._cond.notify_all()
                natural_end = owner and not session.stop_event.is_set()
            if natural_end and session.on_end is not None:
                try:
                    session.on_end(session.token, failure)
                except Exception:
                    _log.exception("playback end callback failed")
            with self._cond:
                if session in self._retiring:
                    self._retiring.remove(session)
            session.finished.set()

    def _close_sink(self, session: _Session, *, drain: bool) -> str | None:
        """Close the session's sink exactly once. Returns an error message, if any."""
        with self._cond:
            if session.sink_closed:
                return None
            session.sink_closed = True
        try:
            session.sink.close(drain=drain)
        except AudioDeviceError as exc:
            return str(exc)
        return None

    def _record_failure(self, message: str) -> None:
        _log.warning(message)
        with self._cond:
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
