"""Engine-neutral types and the ``TTSEngine`` protocol.

Application code depends only on this module. Concrete engines (Kokoro, Piper)
live in sibling modules and import their heavy dependencies lazily, so the core
package stays importable when no engine extra is installed.

Lifecycle: an engine is constructed cheaply, :meth:`TTSEngine.load` loads the
model once (seconds, not milliseconds), and :meth:`TTSEngine.synthesize` can then
be called many times. Callers must not call ``load`` for every request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EngineError(Exception):
    """A TTS engine could not load, or failed to synthesise speech."""


@dataclass(frozen=True, slots=True)
class Voice:
    """A voice an engine can speak with.

    Attributes:
        voice_id: Identifier understood by the engine (for example ``af_heart``).
        language: Product language code: ``en``, ``ru`` or ``he``.
        description: Short human-readable label for the UI and logs.
    """

    voice_id: str
    language: str
    description: str


@dataclass(frozen=True, slots=True)
class AudioBuffer:
    """Mono, signed 16-bit little-endian PCM audio."""

    pcm16: bytes
    sample_rate: int

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm16) / 2 / self.sample_rate


class TTSEngine(Protocol):
    """Common interface for all text-to-speech engines."""

    @property
    def engine_id(self) -> str:
        """Stable identifier such as ``kokoro`` or ``piper``."""
        ...

    @property
    def languages(self) -> frozenset[str]:
        """Product language codes this engine can speak."""
        ...

    @property
    def is_loaded(self) -> bool:
        """Whether :meth:`load` has completed successfully."""
        ...

    def voices(self) -> tuple[Voice, ...]:
        """Voices available without loading the model."""
        ...

    def load(self) -> None:
        """Load the model. Idempotent: a second call does nothing.

        Raises:
            EngineError: if the model or its runtime dependency is unavailable.
        """
        ...

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        """Render ``text`` to audio.

        Raises:
            EngineError: if the engine is not loaded, the voice is unknown, or
                synthesis fails.
        """
        ...
