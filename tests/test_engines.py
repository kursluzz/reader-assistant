from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from reader_assistant.engines.base import AudioBuffer, EngineError, Voice
from reader_assistant.engines.kokoro import KokoroEngine, float_to_pcm16
from reader_assistant.engines.piper import PiperEngine
from reader_assistant.engines.registry import EngineRegistry

# --- helpers -----------------------------------------------------------------


class FakeKokoroModel:
    def __init__(self, samples: list[float], sample_rate: int = 24000) -> None:
        self._samples = np.asarray(samples, dtype=np.float32)
        self._sample_rate = sample_rate
        self.calls: list[dict[str, Any]] = []

    def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[Any, int]:
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return self._samples, self._sample_rate


class FailingKokoroModel:
    def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[Any, int]:
        raise RuntimeError("onnx session exploded")


class FakePiperConfig:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate


class FakePiperModel:
    def __init__(self, chunks: list[bytes], sample_rate: int = 22050) -> None:
        self._chunks = chunks
        self.config = FakePiperConfig(sample_rate)
        self.calls: list[tuple[str, Any]] = []

    def synthesize(self, text: str, syn_config: Any | None = None) -> list[Any]:
        self.calls.append((text, syn_config))
        return [SimpleNamespace(audio_int16_bytes=chunk) for chunk in self._chunks]


class FakeEngine:
    """Minimal engine used to test the registry without any real model."""

    def __init__(self, engine_id: str, languages: set[str]) -> None:
        self._engine_id = engine_id
        self._languages = frozenset(languages)

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def languages(self) -> frozenset[str]:
        return self._languages

    @property
    def is_loaded(self) -> bool:
        return False

    def voices(self) -> tuple[Voice, ...]:
        return ()

    def load(self) -> None:
        return None

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        raise NotImplementedError


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real model; the factory is faked")
    return path


# --- AudioBuffer --------------------------------------------------------------


def test_audio_buffer_duration_follows_sample_count_and_rate() -> None:
    buffer = AudioBuffer(pcm16=b"\x00\x00" * 24000, sample_rate=24000)

    assert buffer.duration_seconds == pytest.approx(1.0)


# --- float_to_pcm16 -----------------------------------------------------------


def test_float_samples_are_scaled_to_signed_16_bit_and_clipped() -> None:
    pcm = float_to_pcm16(np.array([0.0, 1.0, -1.0, 2.0, -2.0, 0.5], dtype=np.float32))

    values = np.frombuffer(pcm, dtype="<i2")
    assert values.tolist() == [0, 32767, -32767, 32767, -32767, 16383]


# --- KokoroEngine -------------------------------------------------------------


@pytest.fixture
def kokoro_files(tmp_path: Path) -> tuple[Path, Path]:
    return _touch(tmp_path / "kokoro-v1.0.onnx"), _touch(tmp_path / "voices-v1.0.bin")


def test_kokoro_exposes_only_english_voices(kokoro_files: tuple[Path, Path]) -> None:
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: FakeKokoroModel([0.0]))

    voices = engine.voices()

    assert engine.languages == frozenset({"en"})
    assert voices
    assert all(voice.language == "en" for voice in voices)
    assert all(voice.voice_id[:1] in {"a", "b"} for voice in voices)


def test_kokoro_loads_model_once_and_reuses_it(kokoro_files: tuple[Path, Path]) -> None:
    created: list[FakeKokoroModel] = []

    def factory(model: Path, voices: Path) -> FakeKokoroModel:
        assert model == kokoro_files[0]
        assert voices == kokoro_files[1]
        instance = FakeKokoroModel([0.0, 0.5])
        created.append(instance)
        return instance

    engine = KokoroEngine(*kokoro_files, factory=factory)
    engine.load()
    engine.load()
    engine.synthesize("hello", voice_id="af_heart")
    engine.synthesize("again", voice_id="af_heart")

    assert len(created) == 1
    assert engine.is_loaded
    assert len(created[0].calls) == 2


def test_kokoro_synthesis_before_load_is_an_error(kokoro_files: tuple[Path, Path]) -> None:
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: FakeKokoroModel([0.0]))

    with pytest.raises(EngineError, match="not loaded"):
        engine.synthesize("hello", voice_id="af_heart")


def test_kokoro_missing_file_is_reported_without_calling_factory(tmp_path: Path) -> None:
    called: list[bool] = []

    def factory(_m: Path, _v: Path) -> FakeKokoroModel:
        called.append(True)
        return FakeKokoroModel([0.0])

    engine = KokoroEngine(tmp_path / "missing.onnx", tmp_path / "missing.bin", factory=factory)

    with pytest.raises(EngineError, match="Kokoro file not found"):
        engine.load()
    assert called == []


def test_kokoro_factory_failure_is_wrapped(kokoro_files: tuple[Path, Path]) -> None:
    def factory(_m: Path, _v: Path) -> FakeKokoroModel:
        raise OSError("corrupt file")

    engine = KokoroEngine(*kokoro_files, factory=factory)

    with pytest.raises(EngineError, match="failed to load Kokoro model: corrupt file"):
        engine.load()
    assert not engine.is_loaded


def test_kokoro_synthesis_passes_voice_speed_and_locale(kokoro_files: tuple[Path, Path]) -> None:
    model = FakeKokoroModel([0.0, 0.25, -0.25], sample_rate=24000)
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: model)
    engine.load()

    audio = engine.synthesize("Good morning.", voice_id="bf_emma", speed=1.25)

    assert model.calls == [
        {"text": "Good morning.", "voice": "bf_emma", "speed": 1.25, "lang": "en-gb"}
    ]
    assert audio.sample_rate == 24000
    assert np.frombuffer(audio.pcm16, dtype="<i2").tolist() == [0, 8191, -8191]


def test_kokoro_american_voice_uses_us_locale(kokoro_files: tuple[Path, Path]) -> None:
    model = FakeKokoroModel([0.0])
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: model)
    engine.load()

    engine.synthesize("Hi.", voice_id="am_adam")

    assert model.calls[0]["lang"] == "en-us"


@pytest.mark.parametrize(
    ("text", "voice_id", "speed", "error"),
    [
        ("   ", "af_heart", 1.0, ValueError),
        ("hello", "af_heart", 0.0, ValueError),
        ("hello", "af_heart", 9.0, ValueError),
        ("hello", "ff_siwis", 1.0, EngineError),  # French voice, not exposed
    ],
)
def test_kokoro_rejects_invalid_requests(
    kokoro_files: tuple[Path, Path],
    text: str,
    voice_id: str,
    speed: float,
    error: type[Exception],
) -> None:
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: FakeKokoroModel([0.0]))
    engine.load()

    with pytest.raises(error):
        engine.synthesize(text, voice_id=voice_id, speed=speed)


def test_kokoro_runtime_failure_is_wrapped(kokoro_files: tuple[Path, Path]) -> None:
    engine = KokoroEngine(*kokoro_files, factory=lambda _m, _v: FailingKokoroModel())
    engine.load()

    with pytest.raises(EngineError, match="Kokoro synthesis failed: onnx session exploded"):
        engine.synthesize("hello", voice_id="af_heart")


# --- PiperEngine --------------------------------------------------------------


@pytest.fixture
def piper_model_path(tmp_path: Path) -> Path:
    model = _touch(tmp_path / "he_IL-saspeech-medium.onnx")
    _touch(model.with_name(model.name + ".json"))
    return model


def _piper_engine(
    model_path: Path,
    model: FakePiperModel,
    configs: list[float],
) -> PiperEngine:
    def config_factory(length_scale: float) -> SimpleNamespace:
        configs.append(length_scale)
        return SimpleNamespace(length_scale=length_scale)

    return PiperEngine(
        model_path,
        language="he",
        factory=lambda _path: model,
        config_factory=config_factory,
    )


def test_piper_voice_id_is_the_model_file_stem(piper_model_path: Path) -> None:
    engine = PiperEngine(piper_model_path, language="he")

    assert engine.voice_id == "he_IL-saspeech-medium"
    assert engine.languages == frozenset({"he"})
    assert engine.voices() == (
        Voice("he_IL-saspeech-medium", "he", "Piper voice he_IL-saspeech-medium"),
    )


def test_piper_load_requires_model_configuration_file(tmp_path: Path) -> None:
    model = _touch(tmp_path / "voice.onnx")  # no voice.onnx.json next to it
    engine = PiperEngine(model, language="ru", factory=lambda _p: FakePiperModel([]))

    with pytest.raises(EngineError, match="configuration not found"):
        engine.load()


def test_piper_concatenates_chunks_and_uses_model_sample_rate(piper_model_path: Path) -> None:
    model = FakePiperModel([b"\x01\x00", b"\x02\x00"], sample_rate=22050)
    configs: list[float] = []
    engine = _piper_engine(piper_model_path, model, configs)
    engine.load()

    audio = engine.synthesize("שלום", voice_id="he_IL-saspeech-medium", speed=1.0)

    assert audio.pcm16 == b"\x01\x00\x02\x00"
    assert audio.sample_rate == 22050
    assert configs == [1.0]


def test_piper_speed_maps_to_inverse_length_scale(piper_model_path: Path) -> None:
    configs: list[float] = []
    engine = _piper_engine(piper_model_path, FakePiperModel([b"\x00\x00"]), configs)
    engine.load()

    engine.synthesize("שלום", voice_id="he_IL-saspeech-medium", speed=2.0)

    assert configs == [0.5]


def test_piper_loads_model_once(piper_model_path: Path) -> None:
    created: list[FakePiperModel] = []

    def factory(_path: Path) -> FakePiperModel:
        model = FakePiperModel([b"\x00\x00"])
        created.append(model)
        return model

    engine = PiperEngine(
        piper_model_path,
        language="he",
        factory=factory,
        config_factory=lambda length_scale: length_scale,
    )
    engine.load()
    engine.load()
    engine.synthesize("שלום", voice_id="he_IL-saspeech-medium")

    assert len(created) == 1


def test_piper_synthesis_before_load_is_an_error(piper_model_path: Path) -> None:
    engine = PiperEngine(piper_model_path, language="he")

    with pytest.raises(EngineError, match="not loaded"):
        engine.synthesize("שלום", voice_id="he_IL-saspeech-medium")


def test_piper_rejects_other_voice_identifiers(piper_model_path: Path) -> None:
    configs: list[float] = []
    engine = _piper_engine(piper_model_path, FakePiperModel([b"\x00\x00"]), configs)
    engine.load()

    with pytest.raises(EngineError, match="unknown Piper voice"):
        engine.synthesize("שלום", voice_id="ru_RU-irina-medium")


def test_piper_empty_output_is_an_error(piper_model_path: Path) -> None:
    configs: list[float] = []
    engine = _piper_engine(piper_model_path, FakePiperModel([]), configs)
    engine.load()

    with pytest.raises(EngineError, match="produced no audio"):
        engine.synthesize("שלום", voice_id="he_IL-saspeech-medium")


# --- EngineRegistry -----------------------------------------------------------


def test_registry_returns_first_registered_engine_as_default() -> None:
    registry = EngineRegistry()
    kokoro = FakeEngine("kokoro", {"en"})
    piper = FakeEngine("piper", {"en", "he"})

    registry.register(kokoro)
    registry.register(piper)

    assert registry.default_for("en") is kokoro
    assert registry.engines_for("en") == (kokoro, piper)
    assert registry.default_for("he") is piper
    assert registry.languages() == frozenset({"en", "he"})


def test_registry_reports_languages_without_an_engine() -> None:
    registry = EngineRegistry()
    registry.register(FakeEngine("kokoro", {"en"}))

    with pytest.raises(EngineError, match="no TTS engine available for language 'ru'"):
        registry.default_for("ru")
    assert registry.engines_for("ru") == ()


def test_registry_rejects_the_same_engine_twice() -> None:
    registry = EngineRegistry()
    engine = FakeEngine("kokoro", {"en"})
    registry.register(engine)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(engine)
