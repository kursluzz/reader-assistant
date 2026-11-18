"""The application service: accepts text, plays it, and reports what it is doing.

Used by the CLI and the IPC socket today, and by the GUI in M5. There is one
instance per process, and it owns the engine registry and the single
:class:`AudioPlayer`. Nothing outside this module calls the player.

Concurrency model:

- One worker thread (``reader-assistant-speech``) does all engine and player work:
  loading, synthesis, starting playback, and the playback controls. Because only that
  thread touches the player's lifecycle, a stop can never race a play.
- Callers (IPC handlers, the GUI) only change small pieces of state under one lock and
  post commands. They never wait for the worker. ``stop`` returns at once.
- Playback ends on its own thread. It reports back through a callback that updates
  the state under the same lock.
- Every request has an ID. Events carry it, and the worker drops results for a request
  that was stopped or replaced, so a stale result cannot be taken for the current one.
- Events are placed on a thread-safe queue in the order they happen, and consumers
  drain it. No callback runs on the worker thread.

Latest request wins: a new request replaces one not yet started. It also interrupts
playback once its own audio is ready. Only one request waits at a time.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Final

from reader_assistant.audio.player import AudioPlayer, InvalidAudioError
from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.engines.base import AudioBuffer, EngineError, TTSEngine
from reader_assistant.engines.catalog import DEFAULT_VOICE_BY_LANGUAGE
from reader_assistant.engines.registry import EngineRegistry
from reader_assistant.ipc.protocol import (
    CODE_AUDIO_FAILED,
    CODE_EMPTY_TEXT,
    CODE_ENGINE_FAILED,
    CODE_INTERNAL,
    CODE_TOO_LARGE,
    CODE_UNSUPPORTED,
)
from reader_assistant.language.detect import UnsupportedTextError, detect_language
from reader_assistant.limits import MAX_TEXT_BYTES

_log = logging.getLogger("reader_assistant.service")
_WORKER_JOIN_TIMEOUT_SECONDS: Final = 5.0
CODE_SHUTTING_DOWN: Final = "shutting_down"


class SpeechError(Exception):
    """A request was rejected, or speech failed. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ServiceState(enum.Enum):
    """What the service is doing right now. Only one request is active at a time."""

    IDLE = "idle"
    LOADING = "loading"  # the engine's model is being loaded
    SYNTHESIZING = "synthesizing"
    PLAYING = "playing"
    PAUSED = "paused"


class EventKind(enum.Enum):
    ACCEPTED = "accepted"  # a request was accepted and queued
    CANCELLED = "cancelled"  # a request was dropped before it started (reason says why)
    LOADING = "loading"  # the engine started loading its model
    SYNTHESIS_STARTED = "synthesis_started"
    SYNTHESIS_COMPLETED = "synthesis_completed"  # duration_seconds is set
    PLAYBACK_STARTED = "playback_started"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"  # playback stopped (reason: "user" or "replaced")
    FINISHED = "finished"  # playback ended normally
    ERROR = "error"  # the request failed (code and message are set)
    SHUTDOWN = "shutdown"  # the service has closed


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened. ``request_id`` is None only for SHUTDOWN."""

    kind: EventKind
    request_id: int | None = None
    language: str | None = None
    voice_id: str | None = None
    reason: str = ""
    code: str = ""
    message: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class EngineStatus:
    language: str
    engine_id: str
    loaded: bool


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """An immutable snapshot of the service. Safe to read from any thread.

    ``request_id``, ``language`` and ``voice_id`` describe the active request, the one
    that is loading, synthesising, playing or paused. They are None when idle, even if
    another request is queued.
    """

    state: ServiceState
    request_id: int | None
    language: str | None
    voice_id: str | None
    engines: tuple[EngineStatus, ...]
    last_error: str | None
    volume: float


@dataclass(frozen=True, slots=True)
class Accepted:
    request_id: int
    language: str
    voice_id: str


@dataclass(frozen=True, slots=True)
class _Job:
    request_id: int
    generation: int
    text: str
    language: str
    voice_id: str


class ReaderService:
    """Speaks text on a worker thread and reports every step as an event."""

    def __init__(
        self,
        registry: EngineRegistry,
        player: AudioPlayer,
        *,
        default_language: str = "en",
    ) -> None:
        self._registry = registry
        self._player = player
        self._default_language = default_language
        self._cond = threading.Condition()
        self._events: queue.Queue[Event] = queue.Queue()
        self._pending: _Job | None = None
        self._commands: deque[str] = deque()
        self._state = ServiceState.IDLE
        self._request_id: int | None = None
        self._language: str | None = None
        self._voice_id: str | None = None
        # The request whose job is running on the worker right now (loading or
        # synthesising). It can differ from the audible request while one plays.
        self._working_id: int | None = None
        self._next_request_id = 1
        # Bumped by every stop, so work for an earlier request can recognise it is stale.
        self._generation = 0
        self._closed = False
        self._last_error: str | None = None
        self._worker = threading.Thread(
            target=self._work, name="reader-assistant-speech", daemon=True
        )

    # --- requests ------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if not self._worker.is_alive() and not self._closed:
            self._worker.start()

    def submit(self, text: str) -> Accepted:
        """Queue ``text`` for speech.

        The service alone chooses the language (by detection from the text) and the voice
        (the language's default). Callers cannot force a language or voice.

        Raises:
            SpeechError: if the text is too large, empty or has no English, Russian or Hebrew
                letters, or the service is shutting down.
        """
        with self._cond:
            if self._closed:
                raise SpeechError(CODE_SHUTTING_DOWN, "The service is shutting down.")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise SpeechError(CODE_TOO_LARGE, f"The text is larger than {MAX_TEXT_BYTES} bytes.")
        if not text.strip():
            raise SpeechError(CODE_EMPTY_TEXT, "The text is empty.")
        chosen_language, chosen_voice = self._detect(text)

        with self._cond:
            if self._closed:
                raise SpeechError(CODE_SHUTTING_DOWN, "The service is shutting down.")
            request_id = self._next_request_id
            self._next_request_id += 1
            if self._pending is not None:
                self._emit(
                    Event(
                        EventKind.CANCELLED,
                        request_id=self._pending.request_id,
                        reason="replaced",
                    )
                )
            self._pending = _Job(
                request_id=request_id,
                generation=self._generation,
                text=text,
                language=chosen_language,
                voice_id=chosen_voice,
            )
            self._emit(
                Event(
                    EventKind.ACCEPTED,
                    request_id=request_id,
                    language=chosen_language,
                    voice_id=chosen_voice,
                )
            )
            self._cond.notify_all()
        _log.info(
            "speech request queued",
            extra={"request_id": request_id, "language": chosen_language, "voice": chosen_voice},
        )
        return Accepted(request_id=request_id, language=chosen_language, voice_id=chosen_voice)

    # --- playback controls (non-blocking; the worker performs the player calls) ----

    def stop(self) -> bool:
        """Stop the active request and drop any queued one. Returns True if anything was active.

        Returns at once. If playback is running it stops on the worker thread. If the
        engine is still synthesising, its result is discarded when it arrives.
        """
        with self._cond:
            if self._closed:
                return False
            was_active = self._state is not ServiceState.IDLE or self._pending is not None
            self._generation += 1
            if self._pending is not None:
                self._emit(
                    Event(EventKind.CANCELLED, request_id=self._pending.request_id, reason="user")
                )
                self._pending = None
            if self._request_id is not None:
                if self._state in (ServiceState.PLAYING, ServiceState.PAUSED):
                    self._emit(Event(EventKind.STOPPED, request_id=self._request_id, reason="user"))
                    self._commands.append("stop")
                else:
                    self._emit(
                        Event(EventKind.CANCELLED, request_id=self._request_id, reason="user")
                    )
            if self._working_id is not None and self._working_id != self._request_id:
                # A newer request is being prepared while an older one is still audible.
                self._emit(Event(EventKind.CANCELLED, request_id=self._working_id, reason="user"))
            self._clear_active()
            self._cond.notify_all()
            return was_active

    def pause(self) -> bool:
        """Pause playback. Returns False unless something is playing."""
        with self._cond:
            if self._closed or self._state is not ServiceState.PLAYING:
                return False
            self._state = ServiceState.PAUSED
            self._emit(Event(EventKind.PAUSED, request_id=self._request_id))
            self._commands.append("pause")
            self._cond.notify_all()
            return True

    def resume(self) -> bool:
        """Resume paused playback. Returns False unless paused."""
        with self._cond:
            if self._closed or self._state is not ServiceState.PAUSED:
                return False
            self._state = ServiceState.PLAYING
            self._emit(Event(EventKind.RESUMED, request_id=self._request_id))
            self._commands.append("resume")
            self._cond.notify_all()
            return True

    def set_volume(self, volume: float) -> None:
        """Set the output volume in [0, 1]. Takes effect from the next block.

        Raises:
            ValueError: if ``volume`` is outside [0, 1].
        """
        self._player.set_volume(volume)

    # --- reading state -------------------------------------------------------

    def status(self) -> ServiceStatus:
        """A snapshot of the current state. Thread-safe and immutable."""
        with self._cond:
            engines = tuple(
                EngineStatus(language=language, engine_id=engine.engine_id, loaded=engine.is_loaded)
                for language in sorted(self._registry.languages())
                for engine in self._registry.engines_for(language)
            )
            return ServiceStatus(
                state=self._state,
                request_id=self._request_id,
                language=self._language,
                voice_id=self._voice_id,
                engines=engines,
                last_error=self._last_error,
                volume=self._player.volume,
            )

    def drain_events(self) -> list[Event]:
        """Return and remove every event so far, oldest first. Thread-safe."""
        events: list[Event] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    @property
    def last_error(self) -> str | None:
        """Description of the most recent failure, if any."""
        with self._cond:
            return self._last_error

    def close(self) -> None:
        """Stop everything and release the worker and the player. Idempotent."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            # Invalidate any synthesis still running, so it cannot start playback after shutdown.
            self._generation += 1
            self._pending = None
            self._commands.clear()
            self._clear_active()
            self._cond.notify_all()
        if self._worker.is_alive() and threading.current_thread() is not self._worker:
            self._worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        self._player.close()
        self._events.put(Event(EventKind.SHUTDOWN))

    # --- validation ----------------------------------------------------------

    def _detect(self, text: str) -> tuple[str, str]:
        """The language detected from ``text``, and that language's default voice."""
        try:
            language = detect_language(text, default=self._default_language)
        except UnsupportedTextError as exc:
            raise SpeechError(CODE_UNSUPPORTED, str(exc)) from exc
        return language, DEFAULT_VOICE_BY_LANGUAGE[language]

    def _engine_for(self, language: str, voice_id: str) -> TTSEngine:
        for engine in self._registry.engines_for(language):
            if any(item.voice_id == voice_id for item in engine.voices()):
                return engine
        raise SpeechError(CODE_ENGINE_FAILED, f"no engine serves voice {voice_id!r}")

    # --- worker --------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._closed and self._pending is None and not self._commands:
                    self._cond.wait()
                if self._closed:
                    return
                commands = list(self._commands)
                self._commands.clear()
                job, self._pending = self._pending, None
            for command in commands:
                self._run_command(command)
            if job is not None:
                self._run_job(job)

    def _run_command(self, command: str) -> None:
        if command == "stop":
            self._player.request_stop()  # non-blocking; the player's worker finishes the stop
        elif command == "pause":
            self._player.pause()
        elif command == "resume":
            self._player.resume()

    def _run_job(self, job: _Job) -> None:
        try:
            self._run_job_steps(job)
        finally:
            with self._cond:
                if self._working_id == job.request_id:
                    self._working_id = None

    def _run_job_steps(self, job: _Job) -> None:
        try:
            engine = self._engine_for(job.language, job.voice_id)
            if not engine.is_loaded:
                if not self._enter(job, ServiceState.LOADING, EventKind.LOADING):
                    return
                try:
                    engine.load()
                except EngineError as exc:
                    raise SpeechError(CODE_ENGINE_FAILED, str(exc)) from exc
            if not self._enter(job, ServiceState.SYNTHESIZING, EventKind.SYNTHESIS_STARTED):
                return
            try:
                audio = engine.synthesize(job.text, voice_id=job.voice_id)
            except (EngineError, ValueError) as exc:
                raise SpeechError(CODE_ENGINE_FAILED, f"speech generation failed: {exc}") from exc
            self._synthesis_done(job, audio)
            self._play(job, audio)
        except SpeechError as exc:
            self._fail(job, exc)
        except Exception as exc:
            _log.exception("unexpected error while speaking")
            self._fail(job, SpeechError(CODE_INTERNAL, f"unexpected error: {exc}"))

    def _enter(self, job: _Job, state: ServiceState, kind: EventKind) -> bool:
        """Start work on ``job``. False if it was stopped meanwhile.

        While an older request is still audible, its state is kept until the new audio is
        ready. The status keeps describing what the user hears, and the events still show
        the new request's progress.
        """
        with self._cond:
            if job.generation != self._generation:
                return False
            self._working_id = job.request_id
            if self._state is ServiceState.IDLE or self._request_id == job.request_id:
                self._state = state
                self._request_id = job.request_id
                self._language = job.language
                self._voice_id = job.voice_id
            self._emit(
                Event(
                    kind,
                    request_id=job.request_id,
                    language=job.language,
                    voice_id=job.voice_id,
                )
            )
            return True

    def _synthesis_done(self, job: _Job, audio: AudioBuffer) -> None:
        with self._cond:
            if job.generation != self._generation:
                return
            self._emit(
                Event(
                    EventKind.SYNTHESIS_COMPLETED,
                    request_id=job.request_id,
                    duration_seconds=audio.duration_seconds,
                )
            )

    def _play(self, job: _Job, audio: AudioBuffer) -> None:
        with self._cond:
            if job.generation != self._generation:
                return
            if self._request_id is not None and self._request_id != job.request_id:
                # The player stops the previous playback when this one starts.
                self._emit(Event(EventKind.STOPPED, request_id=self._request_id, reason="replaced"))
            self._state = ServiceState.PLAYING
            self._request_id = job.request_id
            self._language = job.language
            self._voice_id = job.voice_id
            self._emit(
                Event(
                    EventKind.PLAYBACK_STARTED,
                    request_id=job.request_id,
                    language=job.language,
                    voice_id=job.voice_id,
                )
            )
        try:
            self._player.play(audio, token=job.request_id, on_end=self._on_playback_end)
        except AudioDeviceError as exc:
            raise SpeechError(CODE_AUDIO_FAILED, str(exc)) from exc
        except InvalidAudioError as exc:
            raise SpeechError(
                CODE_ENGINE_FAILED, f"the engine produced invalid audio: {exc}"
            ) from exc
        _log.info("speaking", extra={"request_id": job.request_id, "language": job.language})

    def _on_playback_end(self, token: int, error: str | None) -> None:
        """Called on the playback thread when audio ends by itself."""
        with self._cond:
            if token != self._request_id or self._state not in (
                ServiceState.PLAYING,
                ServiceState.PAUSED,
            ):
                return  # stale: that request was already stopped or replaced
            if error is None:
                self._emit(Event(EventKind.FINISHED, request_id=token))
            else:
                self._last_error = f"{CODE_AUDIO_FAILED}: {error}"
                self._emit(
                    Event(EventKind.ERROR, request_id=token, code=CODE_AUDIO_FAILED, message=error)
                )
            self._clear_active()
            self._cond.notify_all()

    def _fail(self, job: _Job, exc: SpeechError) -> None:
        with self._cond:
            if job.generation != self._generation:
                return  # the request was already stopped; its failure no longer matters
            _log.warning("speech failed", extra={"request_id": job.request_id, "code": exc.code})
            self._last_error = f"{exc.code}: {exc.message}"
            if self._request_id == job.request_id:
                self._clear_active()
            self._emit(
                Event(
                    EventKind.ERROR, request_id=job.request_id, code=exc.code, message=exc.message
                )
            )
            self._cond.notify_all()

    # --- helpers (call with the lock held) -----------------------------------

    def _clear_active(self) -> None:
        self._state = ServiceState.IDLE
        self._request_id = None
        self._language = None
        self._voice_id = None

    def _emit(self, event: Event) -> None:
        self._events.put(event)
