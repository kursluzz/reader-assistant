"""Benchmark one TTS engine on fixed sample sentences and write WAV files.

Run each engine in its own process so cold-start time and peak memory are not
mixed with the other engine's:

    uv run --extra kokoro python scripts/benchmark_tts.py --engine kokoro --out-dir DIR
    uv run --extra piper  python scripts/benchmark_tts.py --engine piper  --out-dir DIR

Models are read from the application's models directory
(``$XDG_DATA_HOME/reader-assistant/models`` by default); nothing is downloaded.
The script prints a Markdown table to stdout. Real-time factor (RTF) is the
synthesis wall time divided by the audio duration; below 1.0 is faster than real time.
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from reader_assistant.config import resolve_paths
from reader_assistant.engines.base import AudioBuffer, TTSEngine

ENGLISH_SENTENCES: Final = (
    "Hello. This is a short test of the English voice, read at a normal pace.",
    "The meeting is on Thursday at 3:30 pm in room 12. Please bring the report.",
    "Dr. Smith asked whether the 2024 figures were correct? Thank you!",
)

HEBREW_SENTENCES: Final = (
    "שלום, זהו מבחן להקראת טקסט מהלוח.",
    "הפגישה תתקיים ביום חמישי בשעה 15:30 בחדר 12.",
    "האם תוכל לשלוח לי את המסמך? תודה רבה!",
    'ד"ר כהן ביקש לבדוק את הנתונים מ-2024.',
)

RUSSIAN_SENTENCES: Final = (
    "Здравствуйте. Это короткий тест русского голоса, который читает текст в обычном темпе.",
    "Встреча состоится в четверг в 15:30 в комнате 12. Пожалуйста, принесите отчёт.",
    "Доктор Смирнов спросил, верны ли данные за 2024 год? Спасибо!",
)

PIPER_HEBREW_MODEL: Final = Path("piper/he_IL-saspeech-medium/he_IL-saspeech-medium.onnx")
PIPER_RUSSIAN_MODEL: Final = Path("piper/ru_RU-denis-medium/ru_RU-denis-medium.onnx")
KOKORO_MODEL: Final = Path("kokoro/kokoro-v1.0.onnx")
KOKORO_VOICES: Final = Path("kokoro/voices-v1.0.bin")
KOKORO_VOICE: Final = "af_heart"


@dataclass(frozen=True, slots=True)
class Measurement:
    engine: str
    language: str
    text: str
    audio_seconds: float
    wall_seconds: float

    @property
    def rtf(self) -> float:
        return self.wall_seconds / self.audio_seconds


def _peak_rss_mb() -> float:
    # ru_maxrss is kilobytes on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _write_wav(path: Path, audio: AudioBuffer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(audio.sample_rate)
        handle.writeframes(audio.pcm16)


def _build_engine(name: str, models_dir: Path) -> tuple[TTSEngine, str, str]:
    """Return the engine, its voice identifier, and the product language."""
    if name == "kokoro":
        from reader_assistant.engines.kokoro import KokoroEngine

        kokoro = KokoroEngine(models_dir / KOKORO_MODEL, models_dir / KOKORO_VOICES)
        return kokoro, KOKORO_VOICE, "en"
    if name == "piper":
        from reader_assistant.engines.piper import PiperEngine

        piper = PiperEngine(models_dir / PIPER_HEBREW_MODEL, language="he")
        return piper, piper.voice_id, "he"
    if name == "piper-ru":
        from reader_assistant.engines.piper import PiperEngine

        piper_ru = PiperEngine(models_dir / PIPER_RUSSIAN_MODEL, language="ru")
        return piper_ru, piper_ru.voice_id, "ru"
    raise SystemExit(f"unknown engine {name!r}")


_SENTENCES_BY_ENGINE: Final = {
    "kokoro": ENGLISH_SENTENCES,
    "piper": HEBREW_SENTENCES,
    "piper-ru": RUSSIAN_SENTENCES,
}


def _sentences_for(engine_name: str) -> tuple[str, ...]:
    return _SENTENCES_BY_ENGINE[engine_name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--engine", required=True, choices=("kokoro", "piper", "piper-ru"))
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    models_dir: Path = args.models_dir or resolve_paths(os.environ, Path.home()).models_dir
    engine, voice_id, language = _build_engine(args.engine, models_dir)

    rss_before = _peak_rss_mb()
    start = time.perf_counter()
    engine.load()
    load_seconds = time.perf_counter() - start
    rss_after_load = _peak_rss_mb()

    sentences = _sentences_for(args.engine)
    # Warm-up: the first synthesis includes one-off allocation and is not reported.
    engine.synthesize(sentences[0], voice_id=voice_id)

    measurements: list[Measurement] = []
    for index, text in enumerate(sentences, start=1):
        start = time.perf_counter()
        audio = engine.synthesize(text, voice_id=voice_id)
        wall = time.perf_counter() - start
        wav_path = args.out_dir / args.engine / f"{language}_{index:02d}.wav"
        _write_wav(wav_path, audio)
        measurements.append(Measurement(args.engine, language, text, audio.duration_seconds, wall))
    peak_rss = _peak_rss_mb()

    print(f"## {args.engine} ({language}, voice `{voice_id}`)")
    print()
    print(f"- Cold start (load): **{load_seconds:.2f} s**")
    print(
        f"- Peak RSS before load: {rss_before:.0f} MB; after load: {rss_after_load:.0f} MB; "
        f"after synthesis: {peak_rss:.0f} MB"
    )
    print(f"- Python: {sys.version.split()[0]}; models directory: `{models_dir}`")
    print()
    print("| # | Text | Audio (s) | Synthesis wall (s) | RTF |")
    print("|---|---|---:|---:|---:|")
    for index, m in enumerate(measurements, start=1):
        print(
            f"| {index} | {m.text} | {m.audio_seconds:.2f} | {m.wall_seconds:.2f} | {m.rtf:.3f} |"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
