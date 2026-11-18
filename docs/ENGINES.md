# Engines, voices and licences

The application uses one voice per language. The mapping is defined in `src/reader_assistant/engines/catalog.py`.

## Supported language-to-engine mapping

| Language | Engine | Voice | Voice licence | Status |
|---|---|---|---|---|
| English (`en`) | Kokoro (`kokoro-onnx`, CPU) | `af_heart` | Apache-2.0 (weights), permissive training data | Default. Listening: very good. |
| Hebrew (`he`) | Piper | `he_IL-saspeech-medium` | Non-commercial (SASPEECH dataset, OpenSLR SLR134) | Acceptable under the project's non-commercial policy. Listening: medium. |
| Russian (`ru`) | Piper | `ru_RU-denis-medium` | CC0 (Nabu Casa voice-datasets) | Acceptable. Listening: acceptable, not very good. |

Kokoro-82M has no Russian or Hebrew voices. Piper is used for both, because it has voices trained on each language.

Not used: `ru_RU-irina-medium` (dataset licence unknown), `ru_RU-ruslan-medium` (CC BY-NC-SA 4.0), `ru_RU-dmitri-medium` (CC0, not yet evaluated), `facebook/mms-tts-heb` (CC BY-NC 4.0, fallback only).

## Licensing

The application is GPL-3.0-or-later (see `LICENSE`). Each model has its own licence, and this file, together with `docs/MODEL_MANIFEST.toml`, records it separately.

**Project policy:** Reader Assistant is a non-commercial, open-source project. Non-commercial voice licences (such as CC BY-NC) are therefore acceptable, provided their attribution and notice requirements are met. A voice with an unknown licence is not used.

| Component | Licence | Required notice |
|---|---|---|
| Application code | GPL-3.0-or-later | `LICENSE` |
| `kokoro-onnx` | MIT | Kept in the library's distribution |
| Kokoro-82M weights and voice pack | Apache-2.0 | Model card and `LICENSE` of the source repository |
| `piper-tts` | GPL-3.0-or-later | `LICENSE` of the library |
| Piper `he_IL-saspeech-medium` | Non-commercial (dataset: SASPEECH, OpenSLR SLR134, "Custom non-commercial") | Attribution to the SASPEECH dataset and the Piper voice project. Licence shown in the application. |
| Piper `ru_RU-denis-medium` | CC0 (dataset: Nabu Casa voice-datasets) | Attribution requested by the dataset (not required by CC0) |

Attribution is not yet shown in the application. The notices are in this file, in `docs/MODEL_MANIFEST.toml` and in the README, and the application does not show the Hebrew voice's licence on screen yet.

## Voice selection

The application selects the voice from `DEFAULT_VOICE_BY_LANGUAGE`. A voice is chosen by language; the user does not need to know which engine is used. Building the registry constructs the engines but loads no model. Loading is an explicit step the application takes, so a missing model file shows up when it is first needed, not at startup.
