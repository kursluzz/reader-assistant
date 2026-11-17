"""Piper adapter (``piper-tts``), one instance per downloaded voice model.

Licence (verified 2026-11-17): the ``piper-tts`` package and the
``OHF-Voice/piper1-gpl`` repository are GPL-3.0-or-later. Each voice *model* has
its own licence, recorded in ``docs/MODEL_MANIFEST.toml``. The Hebrew voice
``he_IL-saspeech-medium`` is non-commercial; the Russian voice is on hold.

The ``piper-tts`` dependency is optional (``uv sync --extra piper``) and is imported
only when a model is loaded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final, Protocol

from reader_assistant.engines.base import AudioBuffer, EngineError, Voice

ENGINE_ID: Final = "piper"
MIN_SPEED: Final = 0.25
MAX_SPEED: Final = 4.0


class PiperModel(Protocol):
    """The subset of ``piper.PiperVoice`` this adapter uses.

    The sample rate is read from ``config.sample_rate``; audio chunks carry PCM only.
    """

    @property
    def config(self) -> Any: ...

    def synthesize(self, text: str, syn_config: Any | None = None) -> Iterable[Any]: ...


ModelFactory = Callable[[Path], PiperModel]
SynthesisConfigFactory = Callable[[float], Any]


def _default_factory(model_path: Path) -> PiperModel:
    from piper import PiperVoice  # optional dependency, imported lazily

    model: PiperModel = PiperVoice.load(model_path)
    return model


def _default_config_factory(length_scale: float) -> Any:
    from piper.config import SynthesisConfig  # optional dependency, imported lazily

    return SynthesisConfig(length_scale=length_scale)


class PiperEngine:
    """Load-once, synthesise-many engine for a single Piper voice model.

    Piper's ``length_scale`` is the inverse of speaking rate, so ``speed`` is mapped
    as ``length_scale = 1 / speed``.

    Synthesis is serialised with a lock for the same reason as in the Kokoro adapter.
    """

    def __init__(
        self,
        model_path: Path,
        *,
        language: str,
        factory: ModelFactory | None = None,
        config_factory: SynthesisConfigFactory | None = None,
    ) -> None:
        self._model_path = model_path
        self._language = language
        self._factory: ModelFactory = factory or _default_factory
        self._config_factory: SynthesisConfigFactory = config_factory or _default_config_factory
        self._model: PiperModel | None = None
        self._lock = threading.Lock()

    @property
    def voice_id(self) -> str:
        """Piper voice identifier, which is the model file name without extension."""
        return self._model_path.stem

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    @property
    def languages(self) -> frozenset[str]:
        return frozenset({self._language})

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def voices(self) -> tuple[Voice, ...]:
        return (Voice(self.voice_id, self._language, f"Piper voice {self.voice_id}"),)

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not self._model_path.is_file():
                raise EngineError(f"Piper model not found: {self._model_path}")
            config_path = self._model_path.with_name(self._model_path.name + ".json")
            if not config_path.is_file():
                raise EngineError(f"Piper model configuration not found: {config_path}")
            try:
                self._model = self._factory(self._model_path)
            except Exception as exc:
                raise EngineError(f"failed to load Piper model: {exc}") from exc

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        if not text.strip():
            raise ValueError("text must not be empty")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}")
        if voice_id != self.voice_id:
            raise EngineError(f"unknown Piper voice: {voice_id!r}")

        with self._lock:
            model = self._model
            if model is None:
                raise EngineError("Piper model is not loaded; call load() first")
            try:
                sample_rate = int(model.config.sample_rate)
                config = self._config_factory(1.0 / speed)
                chunks = list(model.synthesize(text, syn_config=config))
            except Exception as exc:
                raise EngineError(f"Piper synthesis failed: {exc}") from exc

        if not chunks:
            raise EngineError("Piper produced no audio")
        pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        return AudioBuffer(pcm16=pcm, sample_rate=sample_rate)
