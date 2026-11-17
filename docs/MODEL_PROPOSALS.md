# TTS model proposals and download approval (M2B)

Status: **proposal only. Nothing has been downloaded.** Approval is needed before M2C starts any download. Metadata was read from official sources on 2026-11-17. Figures marked *assumed* are estimates, not measurements.

## 1. Proposed candidate per language

| | English | Russian | Hebrew |
|---|---|---|---|
| Model and voice | **Kokoro-82M** (`hexgrad/Kokoro-82M`, v1.0), English voices | **Piper `ru_RU-irina-medium`** | **Piper `he_IL-saspeech-medium`** |
| Supported languages | English (the v1.0 release covers 8 languages, English is one of them) | Russian | Hebrew |
| Download size | **327.2 MB** (`kokoro-v1_0.pth`, PyTorch weights) plus the ONNX export used by `kokoro-onnx` (*size not yet verified*) | **63.2 MB** (`.onnx` plus `.onnx.json`) | **63.2 MB** (`.onnx` 63,221,984 B plus `.onnx.json` 5,269 B) |
| Runtime dependencies | Option A: `kokoro-onnx` 0.6.1 (onnxruntime, numpy, phonemizer, espeakng-loader). Option B: `kokoro` 0.9.4 (torch, transformers, misaki). See §3. | `piper-tts` 1.8.0 (onnxruntime, pathvalidate) | Same as Russian |
| Expected RAM | ~0.5–1 GB loaded *(assumed)* | < 0.5 GB *(assumed)* | < 0.5 GB *(assumed)* |
| CPU expectation (i7-1165G7) | Faster than real time with 4 threads *(assumed, to benchmark)* | Far faster than real time *(assumed)* | Far faster than real time *(assumed)* |
| Licence | **Apache-2.0** (model card). Training data: "exclusively permissive/non-copyrighted audio data and IPA phoneme labels" (model card). | **Unknown** for the voice data (model card: dataset RHVoice, licence "Unknown"). Code of the dataset project RHVoice: GPL-2.0. | **Non-commercial only**: dataset SASPEECH (SLR134) is "Custom non-commercial". |
| Official download location | `https://huggingface.co/hexgrad/Kokoro-82M` | `https://huggingface.co/rhasspy/piper-voices` (`ru/ru_RU/irina/medium/`) | `https://huggingface.co/rhasspy/piper-voices` (`he/he_IL/saspeech/medium/`) |

Total download for the three proposed candidates: **about 454 MB** (327 + 63 + 63), before the ONNX export of Kokoro, which is additional.

### Comparison and alternatives considered

- **English, alternative: `en_US-lessac-medium` (Piper).** This is the voice you already have locally. Its dataset, the Blizzard 2013 Lessac data, is stated on the dataset page as **"released under a license for non-commercial use only"**. So the default English Piper voice has the same restriction as the Hebrew one. Kokoro is the cleaner licence and should be the default English engine if its quality holds up.
- **Russian, alternative: Silero TTS.** The Silero models are published under **CC BY-NC-SA 4.0** (model repository licence file), with the exception of some base models under MIT. Russian models are not confirmed as MIT. Non-commercial.
- **Russian, alternative: XTTS-v2.** Not re-verified in M2B. Previously recorded as non-commercial (Coqui Public Model Licence), large, and no Hebrew support. Not proposed.
- **Hebrew, alternative: `facebook/mms-tts-heb`.** Licence **CC-BY-NC-4.0** (verified). Weights 145.2 MB in `model.safetensors`. Needs PyTorch, which is heavy. Keep as a fallback only.

## 2. Licence summary of the proposed set

| Component | Licence | Restriction for this project |
|---|---|---|
| Application code (ours) | GPL-3.0-or-later | None (decided) |
| `piper-tts` 1.8.0 (code, PyPI) | GPL-3.0-or-later (verified in PyPI metadata; the GitHub repository reports GPL-3.0) | Compatible |
| `kokoro-onnx` 0.6.1 (code) | **Not stated** in PyPI metadata | To verify before M2C |
| `kokoro` 0.9.4 (code, PyPI) | Apache-2.0 | Compatible; PyTorch-heavy |
| Kokoro-82M weights | Apache-2.0 | Compatible |
| `ru_RU-irina-medium` voice | **Unknown** | Must be resolved before use in a distributed product |
| `he_IL-saspeech-medium` voice | **Non-commercial** | Document; make it opt-in |
| Phonikud (if used) | Code CC BY 4.0; datasets separate | Check dataset licences first |

The application is GPL-3.0-or-later. A GPL licence on the code does not change the licence of the voice weights. Downloaded voices must keep their own licence text in `docs/ENGINES.md`, and the app must show the licence before a voice is used.

## 3. Runtime choice for Kokoro (decision needed)

| | Option A: `kokoro-onnx` 0.6.1 | Option B: `kokoro` 0.9.4 (official) |
|---|---|---|
| Inference | ONNX Runtime (CPU) | PyTorch |
| Dependencies | onnxruntime, numpy, phonemizer, espeakng-loader | torch, transformers, misaki, huggingface-hub, loguru |
| Install size | Light (a few tens of MB, *assumed*) | Heavy (PyTorch is several hundred MB to GB, *assumed*) |
| Licence of the code | Not stated in PyPI metadata; verify | Apache-2.0 |
| Python | 3.10–3.13 (`<3.14`) | 3.10–3.12 (`<3.13`) |

Recommendation: **Option A**, because it matches the "avoid heavyweight ML dependencies" constraint and runs on CPU through ONNX. Its licence must be confirmed before it is added. If it is not clearly permissive, use Option B for benchmarking only.

Python 3.12 (our pinned version) satisfies both options.

## 4. Proposed download set (requires approval)

| # | Item | Size | Destination (outside the repository) |
|---|---|---|---|
| 1 | `hexgrad/Kokoro-82M` weights (`kokoro-v1_0.pth`) plus `config.json`, `VOICES.md` | 327.2 MB (+ ONNX export, size to confirm) | `$XDG_DATA_HOME/reader-assistant/models/kokoro/` |
| 2 | Piper `ru_RU-irina-medium` (`.onnx`, `.onnx.json`, `MODEL_CARD`) | 63.2 MB | `…/models/piper/ru_RU-irina-medium/` |
| 3 | Piper `he_IL-saspeech-medium` (`.onnx`, `.onnx.json`, `MODEL_CARD`) | 63.2 MB | `…/models/piper/he_IL-saspeech-medium/` |
| 4 | Listening samples (`samples/` folder of the Hebrew voice, WAV) | small, to confirm | Scratch directory outside the repository; not committed |

Nothing is committed to Git. Models stay out of the repository (`.gitignore` covers `*.onnx`, `*.onnx.json`, `*.pth`, `*.safetensors`).

Python packages to install in M2C (not models): `piper-tts==1.8.0`, and either `kokoro-onnx==0.6.1` (Option A) or `kokoro==0.9.4` (Option B). Each goes through `uv add` after the licence check.

## 5. Approval requested

Please answer these before M2C starts:

1. **Approve the three downloads in §4** (Kokoro 327 MB, Piper Russian 63 MB, Piper Hebrew 63 MB), or name replacements?
2. **Kokoro runtime:** Option A (`kokoro-onnx`, light, licence to verify) or Option B (official `kokoro`, PyTorch, Apache-2.0)?
3. **Default English voice:** move from `en_US-lessac-medium` (non-commercial data) to Kokoro?
4. **Licence policy for voices:** accept voices with non-commercial or unknown licences only as opt-in, clearly labelled options (`he_IL-saspeech-medium`, `ru_RU-irina-medium` until resolved)? My recommendation: yes, opt-in, with the licence shown in the UI.
5. **Listening test:** approve fetching the Hebrew sample WAV files (not weights) so the Product Owner can judge naturalness before any Hebrew engine is wired in?

Until these are answered, M2C does not start and nothing is downloaded.

## Sources

- Piper voice catalogue: https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json
- Piper Russian model card: https://huggingface.co/rhasspy/piper-voices/raw/main/ru/ru_RU/irina/medium/MODEL_CARD
- Piper English (Lessac) model card: https://huggingface.co/rhasspy/piper-voices/raw/main/en/en_US/lessac/medium/MODEL_CARD
- Blizzard 2013 Lessac data licence: https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/
- SASPEECH (OpenSLR SLR134): https://www.openslr.org/134/
- Kokoro-82M model card: https://huggingface.co/hexgrad/Kokoro-82M
- MMS-TTS Hebrew: https://huggingface.co/facebook/mms-tts-heb
- Silero models licence: https://github.com/snakers4/silero-models/blob/master/LICENSE
- piper-tts on PyPI: https://pypi.org/project/piper-tts/
- kokoro-onnx on PyPI: https://pypi.org/project/kokoro-onnx/
- kokoro on PyPI: https://pypi.org/project/kokoro/
- Piper repository (GPL-3.0): https://github.com/OHF-Voice/piper1-gpl
