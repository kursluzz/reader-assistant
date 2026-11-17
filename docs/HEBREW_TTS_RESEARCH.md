# Hebrew TTS research (M2A)

Status: research complete, **no model files downloaded**. Metadata (catalogue entries, model cards, configuration files, repository licence files) was read directly from official sources and stored outside the repository.

Date of research: 2026-11-17.

## Summary

1. **An official Piper Hebrew voice exists**: `he_IL-saspeech-medium` in the `rhasspy/piper-voices` catalogue. The earlier M0 uncertainty is resolved. It is one voice, one speaker, medium quality, 22,050 Hz, about 63 MB.
2. **Its licence is the main problem.** The voice was trained on the SASPEECH gold-standard dataset (OpenSLR SLR134), which is listed as **"Custom non-commercial (See README)"**. The voice therefore inherits a non-commercial restriction.
3. **Naturalness is not yet known.** The repository holds sample audio for this voice, but judging naturalness needs a human listening test, which requires the model or samples to be downloaded. That is a decision for the Product Owner.
4. **Other Hebrew options are weaker on licence or on maturity.** MMS-TTS Hebrew is CC-BY-NC-4.0 as well. No other open, maintained Hebrew neural voice with a clean licence was found. Phonikud is a useful Hebrew front-end (text to phonemes) but is not a voice.

## 1. Official Piper voice availability

Source: `https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json` (183 voices in total; language counts: `he_IL` 1, `ru_RU` 4, `en_US` 29, `en_GB` 11).

| Key | Quality | Speakers | Size (all files) | Language |
|---|---|---|---|---|
| `he_IL-saspeech-medium` | medium | 1 | 63.2 MB | Hebrew (he_IL) |

The only Hebrew voice in the official catalogue is `he_IL-saspeech-medium`. Its files in `he/he_IL/saspeech/medium/`:

| File | Size |
|---|---|
| `he_IL-saspeech-medium.onnx` | 63,221,984 bytes |
| `he_IL-saspeech-medium.onnx.json` | 5,269 bytes |
| `MODEL_CARD` | 269 bytes |
| `samples/` | directory of sample audio |

### Model card (verbatim fields)

- Language: he_IL (Hebrew, Israel). Speakers: 1. Quality: medium. Sample rate: 22,050 Hz.
- Dataset URL: `https://www.openslr.org/134` (gold standard). **License: "See dataset URL".**
- Training: fine-tuned from the LibriTTS-R base model.

### Configuration file (`.onnx.json`, read without loading the model)

- `phoneme_type`: `hebrew`
- `espeak.voice`: `he`
- `num_symbols`: 166 phoneme IDs
- `audio.sample_rate`: 22050
- `inference`: noise_scale 0.667, length_scale 1.0, noise_w 0.8
- `dataset`: saspeech

The `phoneme_type` of `hebrew` and the espeak voice `he` show that the voice is designed to take Hebrew text with phonemisation handled by the Piper pipeline. Whether that phonemisation handles unvocalised text well (Hebrew is usually written without vowel marks) has **not** been tested.

## 2. Licence of the Hebrew voice

Source: `https://www.openslr.org/134/` (SASPEECH, SLR134).

- Summary: "Hebrew speech and transcripts by a single speaker (30 hours)".
- **License: "Custom non-commercial (See README)".**
- The page states the dataset was originally published as part of the robo-shaul competition, with a Hebrew-only licence agreement shipped with the archives (`robo_shaul_terms.pdf`).
- The gold-standard subset used for the Piper voice is about 4 hours, 967 MB download.

**Consequence:** `he_IL-saspeech-medium` should be treated as **non-commercial only**, in the same way as MMS-TTS Hebrew. The project itself is GPL-3.0-or-later, which does not change the restriction on the voice. Whether the trained weights count as a derivative of the dataset is a legal question. This report does not give legal advice. The conservative reading, and the one recommended here, is to document it as non-commercial and never to bundle it silently.

## 3. Naturalness and quality

Not assessed yet. What we can say from metadata only:

- Medium quality, one speaker, fine-tuned from a LibriTTS-R base. Medium VITS-style Piper voices are generally intelligible but less natural than high-quality or large neural voices. This is a general expectation, not a measurement.
- Hebrew-specific risks: text without vowel marks (nikud); the phoneme set is Hebrew-specific, so correct reading of homographs depends on the front-end.
- The sample directory in the official repository can be listed and its files read; their content (listening) is a Product Owner task. A listening test requires a short, reversible step: fetching the sample WAV files, which are not model weights. **Requested below.**

## 4. MMS-TTS Hebrew (`facebook/mms-tts-heb`)

Source: `https://huggingface.co/api/models/facebook/mms-tts-heb` (metadata) and the Hugging Face tree listing.

- Licence tag: **`license:cc-by-nc-4.0`** (non-commercial).
- Library: transformers. Pipeline: text-to-speech. Not gated.
- Files: `model.safetensors` **145.2 MB**, `pytorch_model.bin` 145.4 MB (both present; only one is needed).
- Downloads on the Hub: 2,765 (signal only).
- Quality: VITS-based single-speaker model from the MMS project. Usually intelligible but robotic compared with modern neural voices. Not measured here.
- Runtime: needs PyTorch or a conversion to ONNX. PyTorch is heavy, which conflicts with the "avoid heavyweight ML dependencies" constraint for M1–M2.

## 5. Other Hebrew projects

### Phonikud (G2P, front-end)

- Repository `phonikud/phonikud`: "Hebrew grapheme to phoneme (INTERSPEECH 2026)". Last push 2026-06-17.
- Function: converts unvocalised Hebrew to fully specified IPA with stress, and can add nikud.
- **Licence:** the code is **CC BY 4.0** (per the repository LICENSE and README). The datasets it references have **separate licences** that the README tells users to read individually. Not yet checked in detail.
- Relevance: could improve Hebrew input for any phoneme-driven voice. It is not a voice on its own and does not solve the licence problem of the voice.

### `phonikud/phonikud-tts`

- Repository described as "text to speech in Hebrew", last push 2026-11-02. Licence reported as "Other" by the GitHub API. **Not yet inspected** (model architecture, licence file and weights are unknown). Worth a short follow-up before M2B closes.

### `thewh1teagle/phonikud-chatterbox`

- Hebrew TTS using ChatterBox and Phonikud. No licence reported by the GitHub API. Likely a PyTorch-heavy stack. **Not inspected.**

## 6. Hebrew option matrix

| Option | Naturalness | Licence | Size | Runtime | Status |
|---|---|---|---|---|---|
| Piper `he_IL-saspeech-medium` | Unknown (medium) | Non-commercial (SASPEECH) | 63 MB | Piper / onnxruntime (no PyTorch) | Official, usable after licence decision |
| MMS-TTS `mms-tts-heb` | Unknown (VITS, probably lower) | CC-BY-NC-4.0 | 145 MB | PyTorch / transformers | Non-commercial fallback |
| Phonikud + an IPA-capable voice | Depends on voice | G2P code CC BY 4.0; data licences vary | small | Python | Front-end; needs a voice that accepts IPA |
| `phonikud-tts` | Unknown | Unknown | Unknown | Unknown | To inspect |
| `phonikud-chatterbox` | Unknown | Unknown | Unknown | PyTorch | To inspect |

## 7. Conclusions for M2B

1. Hebrew has **one official, runnable-with-Piper candidate**: `he_IL-saspeech-medium`. Its only blocker is the non-commercial licence, not technical feasibility.
2. A **naturalness check is essential** before any choice. A listening test on the official samples, then a test on our own sentences, is the cheapest next step.
3. The Piper `phonikud` route and the `phonikud-tts` project should be inspected before deciding whether Hebrew needs a front-end at all.
4. The licence decision is a **Product Owner decision**. Recommended: accept non-commercial Hebrew voices only as clearly labelled, optional voices that the user must choose explicitly, and document it in `docs/ENGINES.md`.

## Sources

- Piper voice catalogue: https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json
- Hebrew voice model card and file tree: https://huggingface.co/rhasspy/piper-voices/tree/main/he/he_IL/saspeech/medium
- SASPEECH dataset (OpenSLR SLR134): https://www.openslr.org/134/
- MMS-TTS Hebrew metadata: https://huggingface.co/api/models/facebook/mms-tts-heb
- Phonikud repository: https://github.com/phonikud/phonikud (README and LICENSE)
- Phonikud paper: https://arxiv.org/abs/2506.12311
