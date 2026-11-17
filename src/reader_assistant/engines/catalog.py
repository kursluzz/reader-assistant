"""The application's default language-to-engine mapping.

Only the voices listed here are used by the application. Adding or changing one is
a product decision, recorded in docs/ENGINES.md and docs/MODEL_MANIFEST.toml.

Engines are constructed without loading models. Models load on first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from reader_assistant.engines.kokoro import KokoroEngine
from reader_assistant.engines.piper import PiperEngine
from reader_assistant.engines.registry import EngineRegistry

ENGLISH_VOICE: Final = "af_heart"
HEBREW_VOICE: Final = "he_IL-saspeech-medium"
RUSSIAN_VOICE: Final = "ru_RU-denis-medium"

# Relative to the models directory ($XDG_DATA_HOME/reader-assistant/models).
KOKORO_MODEL: Final = Path("kokoro/kokoro-v1.0.onnx")
KOKORO_VOICES: Final = Path("kokoro/voices-v1.0.bin")
HEBREW_MODEL: Final = Path(f"piper/{HEBREW_VOICE}/{HEBREW_VOICE}.onnx")
RUSSIAN_MODEL: Final = Path(f"piper/{RUSSIAN_VOICE}/{RUSSIAN_VOICE}.onnx")

DEFAULT_VOICE_BY_LANGUAGE: Final = {
    "en": ENGLISH_VOICE,
    "he": HEBREW_VOICE,
    "ru": RUSSIAN_VOICE,
}


def default_registry(models_dir: Path) -> EngineRegistry:
    """Build the registry for English, Hebrew and Russian. Loads no models."""
    registry = EngineRegistry()
    registry.register(KokoroEngine(models_dir / KOKORO_MODEL, models_dir / KOKORO_VOICES))
    registry.register(PiperEngine(models_dir / HEBREW_MODEL, language="he"))
    registry.register(PiperEngine(models_dir / RUSSIAN_MODEL, language="ru"))
    return registry
