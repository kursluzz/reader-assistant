from __future__ import annotations

from pathlib import Path

import pytest

from reader_assistant.engines.base import EngineError
from reader_assistant.engines.catalog import (
    DEFAULT_VOICE_BY_LANGUAGE,
    ENGLISH_VOICE,
    HEBREW_VOICE,
    RUSSIAN_VOICE,
    default_registry,
)


def test_registry_covers_exactly_english_hebrew_and_russian(tmp_path: Path) -> None:
    registry = default_registry(tmp_path)

    assert registry.languages() == frozenset({"en", "he", "ru"})


def test_english_is_served_by_kokoro_with_the_default_voice(tmp_path: Path) -> None:
    engine = default_registry(tmp_path).default_for("en")

    assert engine.engine_id == "kokoro"
    assert ENGLISH_VOICE in {voice.voice_id for voice in engine.voices()}


def test_hebrew_is_served_by_piper_with_the_saspeech_voice(tmp_path: Path) -> None:
    engine = default_registry(tmp_path).default_for("he")

    assert engine.engine_id == "piper"
    assert engine.voices()[0].voice_id == HEBREW_VOICE == "he_IL-saspeech-medium"


def test_russian_is_served_by_piper_with_the_denis_voice(tmp_path: Path) -> None:
    engine = default_registry(tmp_path).default_for("ru")

    assert engine.engine_id == "piper"
    assert engine.voices()[0].voice_id == RUSSIAN_VOICE == "ru_RU-denis-medium"


def test_no_engine_is_loaded_by_building_the_registry(tmp_path: Path) -> None:
    registry = default_registry(tmp_path)

    assert all(not registry.default_for(lang).is_loaded for lang in ("en", "he", "ru"))


def test_default_voice_table_matches_the_engines_voices(tmp_path: Path) -> None:
    registry = default_registry(tmp_path)

    for language, voice_id in DEFAULT_VOICE_BY_LANGUAGE.items():
        voices = {voice.voice_id for voice in registry.default_for(language).voices()}
        assert voice_id in voices


def test_irina_is_not_part_of_the_catalogue(tmp_path: Path) -> None:
    registry = default_registry(tmp_path)

    russian_voices = {voice.voice_id for voice in registry.default_for("ru").voices()}
    assert "ru_RU-irina-medium" not in russian_voices


def test_missing_model_files_fail_only_when_loading(tmp_path: Path) -> None:
    engine = default_registry(tmp_path).default_for("he")

    with pytest.raises(EngineError, match="not found"):
        engine.load()
