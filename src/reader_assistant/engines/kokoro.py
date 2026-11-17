"""Kokoro-82M adapter using ONNX Runtime on CPU (``kokoro-onnx``).

Licences (verified 2026-11-17):

- Inference library ``kokoro-onnx``: MIT.
- Kokoro-82M model weights: Apache-2.0. Training data described as permissive.

This adapter covers English only. Kokoro has no Russian or Hebrew voices.

The ``kokoro-onnx`` dependency is optional (install with ``uv sync --extra kokoro``)
and is imported only when a model is loaded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np

from reader_assistant.engines.base import AudioBuffer, EngineError, Voice

ENGINE_ID: Final = "kokoro"
LANGUAGE: Final = "en"
MIN_SPEED: Final = 0.25
MAX_SPEED: Final = 4.0

# Kokoro voice identifiers start with a language/gender prefix. Only English voices
# are exposed; the prefix selects the espeak locale used for phonemisation.
_LOCALE_BY_PREFIX: Final = {"a": "en-us", "b": "en-gb"}

_VOICES: Final = (
    Voice("af_heart", LANGUAGE, "American English, female (Heart)"),
    Voice("af_sarah", LANGUAGE, "American English, female (Sarah)"),
    Voice("am_adam", LANGUAGE, "American English, male (Adam)"),
    Voice("am_michael", LANGUAGE, "American English, male (Michael)"),
    Voice("bf_emma", LANGUAGE, "British English, female (Emma)"),
    Voice("bm_george", LANGUAGE, "British English, male (George)"),
)
_VOICE_IDS: Final = frozenset(voice.voice_id for voice in _VOICES)


class KokoroModel(Protocol):
    """The subset of ``kokoro_onnx.Kokoro`` this adapter uses."""

    def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[Any, int]: ...


ModelFactory = Callable[[Path, Path], KokoroModel]


def _default_factory(model_path: Path, voices_path: Path) -> KokoroModel:
    from kokoro_onnx import Kokoro  # optional dependency, imported lazily

    model: KokoroModel = Kokoro(str(model_path), str(voices_path))
    return model


class KokoroEngine:
    """Load-once, synthesise-many Kokoro engine.

    Synthesis is serialised with a lock, because the underlying ONNX session is
    not documented as thread-safe. Callers may run it from a worker thread.
    """

    def __init__(
        self,
        model_path: Path,
        voices_path: Path,
        *,
        factory: ModelFactory | None = None,
    ) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._factory: ModelFactory = factory or _default_factory
        self._model: KokoroModel | None = None
        self._lock = threading.Lock()

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    @property
    def languages(self) -> frozenset[str]:
        return frozenset({LANGUAGE})

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def voices(self) -> tuple[Voice, ...]:
        return _VOICES

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            for path in (self._model_path, self._voices_path):
                if not path.is_file():
                    raise EngineError(f"Kokoro file not found: {path}")
            try:
                self._model = self._factory(self._model_path, self._voices_path)
            except Exception as exc:
                raise EngineError(f"failed to load Kokoro model: {exc}") from exc

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        if not text.strip():
            raise ValueError("text must not be empty")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}")
        if voice_id not in _VOICE_IDS:
            raise EngineError(f"unknown Kokoro voice: {voice_id!r}")

        locale = _LOCALE_BY_PREFIX[voice_id[0]]
        with self._lock:
            if self._model is None:
                raise EngineError("Kokoro model is not loaded; call load() first")
            try:
                samples, sample_rate = self._model.create(
                    text, voice=voice_id, speed=speed, lang=locale
                )
            except Exception as exc:
                raise EngineError(f"Kokoro synthesis failed: {exc}") from exc

        return AudioBuffer(pcm16=float_to_pcm16(samples), sample_rate=int(sample_rate))


def float_to_pcm16(samples: Any) -> bytes:
    """Convert float samples in [-1, 1] to signed 16-bit little-endian PCM.

    Out-of-range values are clipped rather than wrapped.
    """
    array = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (array * 32767.0).astype("<i2").tobytes()
