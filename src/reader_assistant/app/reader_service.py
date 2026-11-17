"""The application service: accepts text, chooses language and voice, and speaks it.

``submit`` checks the request and returns at once, so the caller (the CLI behind a
keyboard shortcut) does not wait for synthesis. One worker thread then loads the
engine, synthesises the text, and plays it. Only the most recent request is kept
while the worker is busy: a newer copy replaces an older one that has not started.

The service owns the engine registry and the single :class:`AudioPlayer`. Nothing
here creates engines or players per request.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Final

from reader_assistant.audio.player import AudioPlayer, InvalidAudioError
from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.engines.base import EngineError
from reader_assistant.engines.catalog import DEFAULT_VOICE_BY_LANGUAGE
from reader_assistant.engines.registry import EngineRegistry
from reader_assistant.ipc.protocol import (
    CODE_AUDIO_FAILED,
    CODE_EMPTY_TEXT,
    CODE_ENGINE_FAILED,
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


@dataclass(frozen=True, slots=True)
class Accepted:
    language: str
    voice_id: str


@dataclass(frozen=True, slots=True)
class _Pending:
    text: str
    language: str
    voice_id: str


class ReaderService:
    """Validates speech requests and runs synthesis and playback on a worker thread."""

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
        self._condition = threading.Condition()
        self._pending: _Pending | None = None
        self._closed = False
        self._last_error: str | None = None
        self._worker = threading.Thread(
            target=self._work, name="reader-assistant-speech", daemon=True
        )

    # --- public API ----------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if not self._worker.is_alive() and not self._closed:
            self._worker.start()

    def submit(self, text: str) -> Accepted:
        """Validate ``text`` and queue it for speech.

        Raises:
            SpeechError: if the text is too large, empty, has no supported letters, or
                the service is shutting down.
        """
        with self._condition:
            if self._closed:
                raise SpeechError(CODE_SHUTTING_DOWN, "The service is shutting down.")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise SpeechError(CODE_TOO_LARGE, f"The text is larger than {MAX_TEXT_BYTES} bytes.")
        if not text.strip():
            raise SpeechError(CODE_EMPTY_TEXT, "The text is empty.")
        try:
            language = detect_language(text, default=self._default_language)
        except UnsupportedTextError as exc:
            raise SpeechError(CODE_UNSUPPORTED, str(exc)) from exc

        voice_id = DEFAULT_VOICE_BY_LANGUAGE[language]
        with self._condition:
            if self._closed:
                raise SpeechError(CODE_SHUTTING_DOWN, "The service is shutting down.")
            self._pending = _Pending(text=text, language=language, voice_id=voice_id)
            self._condition.notify_all()
        _log.info("speech request queued", extra={"language": language, "voice": voice_id})
        return Accepted(language=language, voice_id=voice_id)

    @property
    def last_error(self) -> str | None:
        """Description of the most recent speech failure, if any."""
        with self._condition:
            return self._last_error

    def close(self) -> None:
        """Stop the worker and the player. Idempotent."""
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()
        if self._worker.is_alive():
            self._worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        self._player.close()

    # --- worker --------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job = self._pending
                self._pending = None
            if job is None:  # pragma: no cover - guarded by the loop above
                continue
            try:
                self._speak(job)
            except SpeechError as exc:
                self._record_failure(f"{exc.code}: {exc.message}")
            except Exception:
                _log.exception("unexpected error while speaking")
                self._record_failure("internal: unexpected error while speaking")

    def _speak(self, job: _Pending) -> None:
        try:
            engine = self._registry.default_for(job.language)
        except EngineError as exc:
            raise SpeechError(CODE_ENGINE_FAILED, str(exc)) from exc
        if not engine.is_loaded:
            try:
                engine.load()
            except EngineError as exc:
                raise SpeechError(CODE_ENGINE_FAILED, str(exc)) from exc
        try:
            audio = engine.synthesize(job.text, voice_id=job.voice_id)
        except (EngineError, ValueError) as exc:
            raise SpeechError(CODE_ENGINE_FAILED, f"speech generation failed: {exc}") from exc
        try:
            self._player.play(audio)
        except AudioDeviceError as exc:
            raise SpeechError(CODE_AUDIO_FAILED, str(exc)) from exc
        except InvalidAudioError as exc:
            raise SpeechError(
                CODE_ENGINE_FAILED, f"the engine produced invalid audio: {exc}"
            ) from exc
        _log.info("speaking", extra={"language": job.language, "engine": engine.engine_id})

    def _record_failure(self, message: str) -> None:
        _log.warning("speech failed", extra={"reason": message})
        with self._condition:
            self._last_error = message
