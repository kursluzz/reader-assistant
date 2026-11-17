# TTS benchmarks (M2C)

Date: 2026-11-17. Hardware: Intel Core i7-1165G7 (4 cores, 8 threads), 30 GiB RAM, CPU only, Ubuntu GNOME on Wayland. Python 3.12.14.

These numbers measure speed and memory only. They say nothing about voice quality. Quality needs listening; see [Listening test](#listening-test-product-owner).

## Engines measured

| Engine | Library (version) | Model | Language | Voice |
|---|---|---|---|---|
| Kokoro (ONNX, CPU) | `kokoro-onnx` 0.6.1 (MIT), onnxruntime CPU | `kokoro-v1.0.onnx` (325.5 MB) + `voices-v1.0.bin` (28.2 MB) | English | `af_heart` |
| Piper | `piper-tts` 1.8.0 (GPL-3.0-or-later) | `he_IL-saspeech-medium.onnx` (63.2 MB) | Hebrew | the model's voice |

Checksums and licences are in [MODEL_MANIFEST.toml](MODEL_MANIFEST.toml).

## Method

Each engine runs in its own process, so memory numbers are not mixed.

1. Load the model once and measure the time (cold start).
2. Synthesise one warm-up sentence and discard it.
3. Synthesise each sample sentence, measure the wall time, and compute the real-time factor (RTF): wall time divided by audio duration. RTF below 1.0 is faster than real time.
4. Write each result as a 16-bit mono WAV file.

Reproduce:

```bash
uv sync --all-extras
uv run python scripts/benchmark_tts.py --engine kokoro --out-dir ~/.local/share/reader-assistant/samples
uv run python scripts/benchmark_tts.py --engine piper  --out-dir ~/.local/share/reader-assistant/samples
```

## Results

### Kokoro, English (`af_heart`)

- Cold start (model load): **0.72 s**
- Peak RSS: 36 MB before load, 440 MB after load, 621 MB after synthesis

| # | Text | Audio (s) | Synthesis wall (s) | RTF |
|---|---|---:|---:|---:|
| 1 | Hello. This is a short test of the English voice, read at a normal pace. | 4.46 | 1.54 | 0.344 |
| 2 | The meeting is on Thursday at 3:30 pm in room 12. Please bring the report. | 5.38 | 1.91 | 0.356 |
| 3 | Dr. Smith asked whether the 2024 figures were correct? Thank you! | 5.00 | 1.77 | 0.353 |

### Piper, Hebrew (`he_IL-saspeech-medium`)

- Cold start (model load): **0.34 s**
- Peak RSS: 29 MB before load, 152 MB after load, 309 MB after synthesis

| # | Text | Audio (s) | Synthesis wall (s) | RTF |
|---|---|---:|---:|---:|
| 1 | שלום, זהו מבחן להקראת טקסט מהלוח. | 2.31 | 0.11 | 0.049 |
| 2 | הפגישה תתקיים ביום חמישי בשעה 15:30 בחדר 12. | 3.39 | 0.15 | 0.045 |
| 3 | האם תוכל לשלוח לי את המסמך? תודה רבה! | 2.15 | 0.10 | 0.048 |
| 4 | ד"ר כהן ביקש לבדוק את הנתונים מ-2024. | 2.18 | 0.11 | 0.049 |

### Interpretation

- **Speed is not the constraint.** Both engines are faster than real time on this laptop with CPU only. Kokoro runs at about 0.35 RTF, so a 10-second sentence takes about 3.5 s. Piper runs at about 0.05 RTF.
- **No annoying start-up delay is expected** if the model is loaded in the background when the app starts. Cold start is 0.3 s to 0.7 s. A single probe run after loading also showed the first real synthesis taking about the same time as later ones (Kokoro 1.5 s for 4.5 s of audio; Piper 0.16 s for 2.3 s of audio).
- **Memory is modest.** Kokoro needs about 620 MB at peak and Piper about 310 MB. Both fit easily in 30 GiB.
- **Kokoro's peak is higher than its load-time memory** (440 MB after load, 621 MB after synthesis). The cause has not been profiled; it is likely ONNX Runtime buffers allocated during the first synthesis. Keeping one loaded instance avoids repeating that cost.

## Hebrew findings

1. **Nikud is ignored.** Piper's phonemiser produces identical phonemes for vocalised and unvocalised Hebrew (checked: `שָׁלוֹם עוֹלָם` and `שלום עולם` give the same output; `מִבְחָן` and `מבחן` also). This means users cannot control pronunciation by adding vowel marks, and the model reads plain text as it is.
2. **No diacritiser is installed.** `PiperVoice.use_tashkeel` is `True`, but the diacritiser is `None`. Piper will not add vowels itself, so pronunciation of homographs may be wrong. This is a known quality limit to judge by ear.
3. **Numbers and abbreviations** (`15:30`, `2024`, `ד"ר`) are passed through unchanged. Whether they are spoken correctly is a listening question (see below).
4. **Phonikud** (Hebrew grapheme-to-phoneme with diacritics) could address points 1 and 2. It is not integrated yet; it would need a separate decision.

## Engine interface

Both adapters implement `reader_assistant.engines.base.TTSEngine`:

- `load()` is explicit and idempotent. It is the only expensive call.
- `synthesize(text, voice_id=..., speed=...)` returns mono 16-bit PCM and its sample rate.
- Speed is validated between 0.25 and 4.0. Piper's `length_scale` is `1 / speed`.
- Synthesis is serialised with a lock, because the ONNX session is not documented as thread-safe.
- Optional libraries are imported lazily. The core package works without either extra.
- A registry (`EngineRegistry`) selects the default engine per language, so callers do not name engines.

Tests use fake models and need no downloads: `uv run pytest`.

## Listening test (Product Owner)

Samples are in `~/.local/share/reader-assistant/samples/`:

- English (Kokoro): `kokoro/en_01.wav`, `kokoro/en_02.wav`, `kokoro/en_03.wav`
- Hebrew (Piper): `piper/he_01.wav`, `piper/he_02.wav`, `piper/he_03.wav`, `piper/he_04.wav`

Play them with, for example:

```bash
pw-play ~/.local/share/reader-assistant/samples/kokoro/en_01.wav
```

Please listen and judge:

1. **Hebrew:** does it sound like a natural Hebrew speaker? Note any wrong word stress, wrong vowels, or misread numbers and abbreviations. Compare `he_04` (abbreviation and year) and `he_02` (time and room number).
2. **English:** does Kokoro sound human enough for everyday reading?
3. **Speed:** is there an annoying delay before the first word? (Measured: not expected, because loading is about 0.3 s to 0.7 s and is done once.)

Quality judgements are yours. The numbers above do not replace them.

## Licensing status

- Kokoro weights: Apache-2.0. Training data described as permissive. Usable, with the licence notice kept.
- `kokoro-onnx`: MIT.
- Piper Hebrew voice `he_IL-saspeech-medium`: **non-commercial only**, because its training data (SASPEECH, OpenSLR SLR134) is custom non-commercial. Opt-in only in the application, with the licence shown.
- `piper-tts`: GPL-3.0-or-later.
- Piper Russian `ru_RU-irina-medium`: **on hold**. Dataset licence unknown. Not downloaded.

## Outstanding limitations

- Hebrew is not measured for naturalness yet; a listening test is required before any Hebrew engine is wired into the application.
- No Russian measurements (voice on hold).
- Kokoro int8 and fp16 variants (114 MB and 164 MB) were not measured; fp32 was used.
- Only one warm measurement run per engine; no statistical spread.
- The official SASPEECH dataset archive (967 MB) was not downloaded. The official Piper Hebrew sample (`speaker_0.mp3`) was not used either; our own samples are generated from the model instead.
- Benchmarks are from a single laptop; desktop and battery-mode results may differ.
