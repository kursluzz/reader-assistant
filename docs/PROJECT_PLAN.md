> **Historical document.** This is the M0 plan, kept as a decision record. Several decisions
> changed later: the window is PySide6, not Tkinter; the window is a client of a headless
> service; the protocol gained version 2. For the current design see
> [ARCHITECTURE.md](ARCHITECTURE.md) and [README.md](../README.md).

# Reader Assistant — Project Plan (M0)

Status: **M0 deliverable, awaiting Technical Lead / Product Owner review.**
No application code has been written. No models have been downloaded.

Legend for claims in this document:
- **[verified]** — confirmed by a primary source (repo/model card/official page) during M0.
- **[reported]** — found in secondary sources or search snippets only; must be re-checked in M2.
- **[assumed]** — engineering assumption to be confirmed by benchmark or by the Product Owner.

---

## 1. Repository inspection report

| Item | Finding |
|---|---|
| Local repo | `/home/oleg/projects/reader-assistant`, branch `main`, **no commits**, working tree empty except `.git/`. |
| Remote | `origin = git@github.com:kursluzz/reader-assistant.git`. GitHub API reports the repository is **empty** (`409 Git Repository is empty`). |
| Issues | None open. |
| Existing code / docs / config | None. No README, LICENSE, pyproject, .gitignore. |
| Pre-existing decisions | ChatGPT session (Technical Lead) has already proposed: Python + Tkinter, Piper as first engine, optional Kokoro for English, `sounddevice` for playback, `pyperclip` or native clipboard tools, pytest, uv. See §8 for the open questions it left. |

Conclusion: greenfield project. M1 can start from a clean slate.

## 2. Local environment

| Component | Finding | Impact |
|---|---|---|
| OS / session | Linux, **Wayland** session (`XDG_SESSION_TYPE=wayland`). | Global shortcuts and clipboard handling need Wayland-aware design (§5, M6). |
| CPU | Intel Core i7-1165G7 (Tiger Lake), 4 cores / 8 threads, 2.8 GHz base, turbo up to ~4.7 GHz. | Sufficient for small VITS/ONNX models in real time (see §7). No GPU. |
| RAM | 30 GiB total, ~22 GiB available at check time. | Far above what the candidate engines need. |
| Disk | 333 GB free on `/`. | No disk constraint for model downloads. |
| Python (system) | 3.14.4 at `/usr/bin/python3`. **No `tkinter`**. | Do not use system Python for the app. |
| Python (uv-managed) | 3.12.14 and 3.13.15 available, **both include `tkinter` 9.0**. | Use uv-managed 3.12 (or 3.13) for the project. |
| uv | 0.12.13 at `~/.local/bin/uv`. | Ready. |
| Audio libraries | `libasound.so.2` present (ALSA). `aplay`, `pw-play` present. **PortAudio not installed**. `libsndfile` and `libsoxr` present. `libespeak-ng` present, `espeak-ng` binary not on PATH. | `sounddevice` needs `libportaudio2` (`sudo apt install libportaudio2`). Document as a system prerequisite. |
| Clipboard tools | `wl-clipboard` 2.2.1 (`wl-paste`) installed. `xclip` not installed. | `wl-paste` is the primary Wayland clipboard source. |
| `pactl` | Not installed. PipeWire/PulseAudio status not yet confirmed. | Verify in M3. |
| ffmpeg | Not installed. | Not required for the planned engines (WAV output). |

Conclusion: the machine is suitable. The only system package M1–M3 is likely to need is `libportaudio2`. Tkinter is available through uv's Python.

## 3. Proposed architecture

```
 ┌──────────────────────────── UI layer ────────────────────────────┐
 │ ui/  Tkinter main window (M5)        cli/  entry point (M1)       │
 │ hotkeys/ global shortcut adapter (M6): desktop shortcut → CLI     │
 └──────────────┬───────────────────────────────┬────────────────────┘
                │ commands / events (no blocking)│
 ┌──────────────▼─────────── Application core ───▼────────────────────┐
 │ app/  ReaderService: read clipboard → normalize → detect language │
 │       → choose engine+voice → synthesize (worker) → play (worker) │
 │ config/  settings + logging                                       │
 └──┬──────────────────┬───────────────────────┬─────────────────────┘
    │                  │                       │
 ┌──▼──────────┐  ┌────▼──────────────┐  ┌─────▼────────────────────┐
 │ clipboard/  │  │ language/         │  │ engines/                  │
 │ ClipboardSource│ │ detector, normalizer│ │ TTSEngine (Protocol)     │
 │ WlPasteSource│ │ (RTL, mixed text)  │  │ PiperEngine (M2)         │
 │ TkClipboard  │ └───────────────────┘  │ KokoroEngine (M2, opt.)  │
 │ (fallback)   │                        │ registry: lang → engines │
 └─────────────┘                         └──────────────────────────┘
 ┌──────────────────────────────────────────────────────────────────┐
 │ audio/  AudioPlayer: background thread, queue of PCM chunks,      │
 │         play/pause/resume/stop, volume, error reporting          │
 │         backend: sounddevice (PortAudio) — ALSA/PipeWire via it   │
 └──────────────────────────────────────────────────────────────────┘
```

Key decisions (proposed):

1. **Engine interface first.** A `TTSEngine` `Protocol` with `load()`, `voices()`, `synthesize(text, voice, speed) -> AudioChunk` (or an iterator of chunks for streaming). Engines are injected into `ReaderService`; the core never imports an engine module directly. This is how engines can be swapped (PO requirement).
2. **Load models once.** Engines are loaded lazily on first use and cached by the service (not per request). No module-level mutable globals: state lives in the service object.
3. **Language routing is data, not code.** A routing table maps language code → ordered list of `(engine, voice)`. Changing the default engine for Hebrew is a config change.
4. **Threading model.** Tkinter stays on the main thread. Synthesis and playback run on a single worker thread with a bounded queue; the UI receives state updates through `after()` polling of a thread-safe state object (no Tk calls from workers). Stop/pause flags are checked between audio chunks, so stop latency ≈ one chunk.
5. **Clipboard abstraction.** `ClipboardSource` protocol. Default on Wayland: `wl-paste --no-newline`. Fallback: Tk clipboard (`tkinter`) when run in a window context. Tests use an in-memory fake.
6. **Global shortcuts are an adapter, not a dependency of the core.** The app exposes a CLI command (e.g. `reader-assistant read`, `reader-assistant stop`, `reader-assistant toggle-pause`) that talks to a running instance over a local Unix socket. Desktop shortcuts (GNOME/KDE custom shortcuts, or any DE) invoke these commands. This works on Wayland, where applications cannot grab global keys directly. An optional X11-only in-process hotkey backend can be added later. (Decision requested — see §10.)
7. **Configuration.** TOML file in `$XDG_CONFIG_HOME/reader-assistant/config.toml`, typed with `pydantic-settings` or a small dataclass parser (decide in M1; avoid the dependency if a dataclass suffices). Model directory in `$XDG_DATA_HOME/reader-assistant/models`, never in the repo.
8. **Logging.** Standard `logging` with a rotating file handler under `$XDG_STATE_HOME`; console level from config.

### Proposed directory structure

```
reader-assistant/
├── pyproject.toml            # uv-managed; ruff, mypy, pytest config
├── uv.lock
├── README.md
├── LICENSE                   # decision pending (§6)
├── CONTRIBUTING.md
├── .gitignore                # models/, .venv/, caches, build artefacts
├── .python-version           # 3.12
├── src/
│   └── reader_assistant/
│       ├── __init__.py
│       ├── __main__.py       # python -m reader_assistant
│       ├── cli.py            # argparse entry point (M1)
│       ├── config.py         # settings + paths (M1)
│       ├── logging_setup.py  # (M1)
│       ├── app/              # ReaderService, state, routing (M3–M4)
│       ├── clipboard/        # ClipboardSource + wl-paste / Tk impls (M4)
│       ├── language/         # normalizer, detector, RTL helpers (M4)
│       ├── engines/          # base.py (Protocol), piper.py, kokoro.py (M2)
│       ├── audio/            # AudioPlayer worker (M3)
│       ├── ui/               # Tkinter window (M5)
│       └── hotkeys/          # shortcut IPC socket + desktop integration docs (M6)
├── tests/                    # pytest; fakes for engines, clipboard, audio
├── docs/
│   ├── PROJECT_PLAN.md       # this file
│   ├── BENCHMARK.md          # M2 benchmark results and reproduction steps
│   ├── ENGINES.md            # engine & voice licences, sizes (M2/M7)
│   └── HOTKEYS.md            # Wayland / X11 setup (M6)
└── scripts/
    └── benchmark_tts.py      # reproducible benchmark (M2)
```

Deviation from the suggested layout: `engines/xtts.py` is dropped from the initial plan (see §4 — XTTS-v2 does not cover Hebrew and is non-commercial). Modules `app/`, `hotkeys/`, `cli.py` are added to keep UI and core separate. The package name `reader_assistant` matches the repository name; ChatGPT's earlier proposal used `clipboard-reader` — the Product Owner should confirm the name (§10).

## 4. TTS technology comparison

Scores are qualitative for planning; M2 replaces them with measurements.

| Engine | EN | RU | HE | Licence (code / weights) | Size (indicative) | CPU inference | Fit |
|---|---|---|---|---|---|---|---|
| **Piper** (`OHF-Voice/piper1-gpl`) | Yes, many voices **[verified]** | Yes — `ru_RU` voices reported **[reported]**, to verify quality | A `he_IL` voice reported on third-party catalogues **[reported]**, **not confirmed** on official voices repo | Engine **GPL-3.0 [verified]**; uses espeak-ng (GPL) for phonemes. Voice licences vary per voice **[verified in general; per-voice to check]** | ~20–80 MB per voice (typical VITS medium) **[assumed]** | Very fast, real-time many times over **[assumed; to benchmark]** | **Primary candidate** for all three languages, pending Hebrew check |
| **Kokoro-82M** | Yes, strong naturalness **[verified: 82M, multi-voice]** | **Not supported** (languages: en, ja, zh, fr, es, it, pt, hi) **[reported]** | **Not supported** **[reported]** | Apache-2.0 **[reported]** (weights; check training-data notes) | ~330 MB weights **[assumed]** | Good on CPU via ONNX **[assumed]** | **Optional English engine** only |
| **Coqui XTTS-v2** | Yes | Yes (ru in its 17 languages) **[verified from model card, to re-check]** | **No** Hebrew in its language list **[verified from model card, to re-check]** | Coqui Public Model Licence — **non-commercial** **[assumed; verify]**; Coqui company wound down, maintenance risk | ~1.8 GB **[assumed]** | Heavy; likely slower than real time on this i7 without GPU **[assumed]** | Not recommended for the clipboard reader; possible later for voice cloning |
| **Meta MMS-TTS** (`facebook/mms-tts-heb`, `…-rus`, `…-eng`) | Yes (eng) | Yes (rus) **[reported]** | Yes (heb) **[reported]** | **CC-BY-NC-4.0 [verified via HF tag]** — **non-commercial** | ~100 MB per language **[assumed]** | Fast (VITS) | **Only confirmed Hebrew neural option**, but non-commercial licence and typically lower naturalness. Use as fallback for HE only if Product Owner accepts the licence |
| **Silero TTS** | Yes | Yes (strong Russian) **[assumed]** | No **[assumed]** | Model licence needs checking (non-commercial in some releases) **[to verify]** | Small **[assumed]** | Very fast **[assumed]** | Worth a benchmark for RU only if Piper RU quality is weak; licence must be verified first |
| **Hebrew G2P: Phonikud** (2025, Interspeech 2026 accepted) | — | — | Front-end, not a voice: unvocalised Hebrew → IPA with stress **[verified: arXiv 2506.12311]** | Check repository licence at `phonikud.github.io` **[to verify]** | Small **[assumed]** | Negligible cost | **Candidate to improve Hebrew** by feeding IPA to a voice that accepts phonemes; integration effort is non-trivial — decide after M2 Piper-HE test |
| **System speech (pyttsx3 → espeak-ng / speech-dispatcher)** | Yes | Yes (robotic) | Limited (robotic) | GPL (espeak-ng) | Tiny | Trivial | **Not** for natural voice; only as an emergency fallback |

### Recommendation for M2 (to be confirmed after review)

1. **Piper as the common engine** for EN and RU, and for HE if the `he_IL` voice passes listening tests. Measure naturalness on the sample sentences before committing.
2. **Kokoro as an optional EN-only engine** (Product Owner may prefer it if English naturalness outweighs the extra ~330 MB and second dependency).
3. **Hebrew is the highest-risk language.** Decision gate: if Piper `he_IL` quality is poor, the options are (a) MMS-TTS-heb with a non-commercial licence, (b) Phonikud + an IPA-capable voice, (c) defer Hebrew. Do **not** choose (a) without Product Owner approval.

We do not assume one engine is optimal for every language: the registry in §3 selects the engine per language.

## 5. Language support assessment (EN / RU / HE)

| Language | Best current candidate | Risk | Notes |
|---|---|---|---|
| English | Piper (en_US/en_GB) — Kokoro as alternative | Low | Kokoro is the naturalness reference **[reported]**; compare in M2. |
| Russian | Piper `ru_RU` voices — Silero if weak | Medium | Russian needs stress marks for natural output; Piper handles stress via espeak — verify on tricky words. |
| Hebrew | Piper `he_IL` (unconfirmed) — MMS-TTS-heb (CC-BY-NC) — Phonikud front-end | **High** | Only area where no clearly good, permissively licensed option is confirmed. **Needs early M2 spike.** |

Text handling requirements (M4):
- **Hebrew RTL:** keep logical order in the text; never reverse strings. Niqqud (diacritics) are typically absent; the TTS front-end may need diacritisation (Phonikud/DictaBERT address this) — this is a known quality limit to document.
- **Mixed-language text:** segment by Unicode script runs (Latin / Cyrillic / Hebrew) at sentence or clause boundaries; route each segment to its language's engine; keep punctuation and sentence terminators with their segment.
- **Sentence splitting:** split on terminal punctuation while keeping it; avoid splitting on abbreviations and decimals; never drop punctuation (PO requirement).
- **Language detection:** script-based heuristic first (Hebrew/Cyrillic/Latin); only fall back to a statistical detector if script is ambiguous (e.g. Latin text in English vs. other Latin languages). Adding a detector library needs a justification in M4.

## 6. Licensing assessment

| Component | Licence | Implication |
|---|---|---|
| Piper1-gpl (engine) | GPL-3.0 **[verified]** | Linking/distributing it makes the **application GPL-3.0** (strong copyleft). Consistent with an open-source project; incompatible with proprietary distribution. |
| espeak-ng (phonemiser used by Piper) | GPL-3.0 **[reported]** | Same effect as above. |
| Legacy `rhasspy/piper` | MIT, archived Oct 2025 **[reported]** | Permissive, but unmaintained. Not recommended. |
| Piper voice models | Vary per voice **[verified in general]** | **Each voice must be checked** (some are CC-BY-NC or research-only). The app must only ship/recommend voices whose licences permit the project's use; the voice catalogue in `docs/ENGINES.md` must list licence per voice. |
| Kokoro-82M | Apache-2.0 **[reported]** | Permissive; compatible with GPL-3.0 app. |
| XTTS-v2 | Coqui Public Model Licence (non-commercial) **[assumed]** | Not used. |
| MMS-TTS | CC-BY-NC-4.0 **[verified]** | Non-commercial. Not usable for commercial distribution. |
| Python deps (sounddevice, numpy, onnxruntime, pytest, ruff) | MIT / BSD / Apache | Compatible. `libportaudio` is MIT-style. |

Proposal: **GPL-3.0-or-later** for the project, because the default engine is GPL-3.0. Models are not stored in Git and are not redistributed by the project; the app downloads voices from their official sources and shows each voice's licence. **This is a Product Owner decision** (§10). Legal review is recommended before a public release (M7).

## 7. Hardware feasibility (Intel i7-1165G7, 32 GB, CPU only)

| Requirement | Estimate | Status |
|---|---|---|
| Piper (VITS, medium) real-time factor | Expected ≪ 1× (faster than real time) **[assumed]** | Benchmark in M2 (`scripts/benchmark_tts.py`). |
| Kokoro-82M via ONNX on CPU | Expected faster than real time with 4 threads **[assumed]** | Benchmark in M2. |
| XTTS-v2 on CPU | Possibly near or below real time **[assumed]** | Not planned; benchmark only if reconsidered. |
| RAM for one loaded voice | < 0.5 GB (Piper); ~1 GB (Kokoro) **[assumed]** | Far below 30 GB. |
| Disk for a full EN+RU+HE voice set | Low single-digit GB at most **[assumed]** | Acceptable; report exact figures in `docs/ENGINES.md`. |
| Time-to-first-audio | Dominated by model load on first use. Preload at startup in background thread. | Design requirement (§3.2). |

Conclusion: **feasible**. No GPU needed. The laptop is a realistic dev and target machine. Note that laptop thermal throttling may reduce sustained throughput; benchmark on battery and on mains.

## 8. Open questions from the ChatGPT session (still unanswered)

The Technical Lead's last message asked three questions. They are **not yet answered** in the session, so M1 is not blocked, but M2 defaults depend on them:

1. Which languages should it read? (EN / RU / HE — assumed: all three, per the product brief.)
2. What matters most — natural voice, low CPU use, or balanced? (Assumed: **natural voice is the primary priority**, per the product brief.)
3. How to operate it — shortcut only, window only, or both? (Assumed: **both**, per the product brief: M5 window and M6 shortcut.)

## 9. Implementation plan M1–M7

| Milestone | Scope | Exit criteria | Key risk |
|---|---|---|---|
| **M1 Foundation** | `uv init` (Python 3.12), `src/` layout, `pyproject.toml` with ruff (lint+format), mypy (strict on `src/`), pytest; `config.py` (XDG paths, TOML); logging; `reader-assistant` CLI with `--version`, `--info`; README, LICENSE (pending decision), CONTRIBUTING, `.gitignore`. | `uv run pytest`, `ruff check`, `ruff format --check`, `mypy` all pass; `uv run reader-assistant --version` works. | Licence decision should be made before M1 commit. |
| **M2 TTS research & engines** | `TTSEngine` protocol; `PiperEngine` (EN, RU, HE voices); optional `KokoroEngine` (EN); engine registry by language; `scripts/benchmark_tts.py`; `docs/BENCHMARK.md`, `docs/ENGINES.md`. **Spike first: Hebrew Piper voice quality.** Do not download large models without reporting size first (we report §7 estimates; PO approves the downloads). | Benchmark run on the i7 with recorded RTF, RAM, load time for each voice; listening samples in all three languages; licence table per voice. | Hebrew quality; piper-tts/onnxruntime wheel availability on Python 3.12. |
| **M3 Audio subsystem** | `AudioPlayer` on a worker thread: play / pause / resume / stop; chunked playback so stop is responsive; volume (software scaling); error handling for missing PortAudio or device failure (clear message, no crash). | Unit tests with fake backend; manual test: play/pause/stop on the laptop without UI freeze. | `libportaudio2` system dependency; PipeWire routing. |
| **M4 Clipboard & language** | `ClipboardSource` with `wl-paste` and Tk fallback; empty clipboard → user message; normalisation (whitespace, NBSP, control chars, Unicode NFC); script-based language detection; RTL-safe handling; mixed-language segmentation; punctuation-preserving sentence split. | Table-driven tests for EN/RU/HE/mixed text, empty input, very long input. | Hebrew niqqud absence; over-splitting. |
| **M5 Desktop GUI (Tkinter)** | Window: Read clipboard, Play, Pause/Resume, Stop, language (auto / EN / RU / HE), voice, speed, status label. All work off the UI thread. | Manual acceptance: UI remains responsive during synthesis of a 5000-character text. | Tk and threads; keep Tk calls on main thread. |
| **M6 Global shortcuts** | Local IPC socket + CLI subcommands (`read`, `stop`, `toggle-pause`); documented GNOME/KDE custom-shortcut setup for Ctrl+Alt+R; optional X11 in-process backend behind an interface; `docs/HOTKEYS.md` with Wayland/X11 limitations. | Ctrl+Alt+R reads clipboard with main window closed (on the target DE); stop/pause command works. | **Wayland does not allow apps to grab global keys directly**: the project must rely on DE shortcuts. Target DE is unknown — **PO input needed**. |
| **M7 Release preparation** | Full test pass; clean-environment install check (fresh `uv sync`, fresh user); licence review; README and installation guide (incl. `libportaudio2`, voice download); engine/language support doc; CONTRIBUTING; GitHub release instructions. | All checks green; PO manual acceptance; **no release without PO approval**. | Voice licences; GPL compliance. |

## 10. Risks and decisions requiring Product Owner input

### Decisions (blocking or near-blocking)

| # | Decision | Recommended option | Blocks |
|---|---|---|---|
| D1 | **Project licence.** Piper1-gpl is GPL-3.0. | GPL-3.0-or-later. | M1 (LICENSE file, pyproject classifier). |
| D2 | **Package and project name** (`reader_assistant` per the brief vs. `clipboard-reader` per ChatGPT). | `reader_assistant` (matches repo). | M1. |
| D3 | **Hebrew voice policy.** Accept MMS-TTS-heb (CC-BY-NC, non-commercial) as an interim option, or only use permissively licensed voices? | Only permissive/clearly-licensed voices; MMS only if Piper HE fails and PO accepts non-commercial use. | M2 Hebrew engine. |
| D4 | **Kokoro as an additional English engine** (~330 MB, extra dependency). | Yes, as optional EN engine; decide after M2 listening test. | M2. |
| D5 | **Hotkey strategy on Wayland.** Use desktop custom shortcuts calling a CLI (works on all DEs) vs. an X11-only in-app hotkey. Which desktop environment is used? | Desktop shortcut + CLI/IPC; PO to name DE (GNOME/KDE/other). | M6. |
| D6 | **Answers to ChatGPT's three open questions** (§8). | Assumed defaults in §8. | M2 defaults, M5 layout. |
| D7 | **Model download approval.** M2 will need to download voices (sizes in §7 are estimates). | Download only after PO approves a listed set. | M2. |

### Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| No high-quality, permissively licensed Hebrew neural voice | High | High | Early Hebrew spike in M2; Phonikud evaluation; PO decision D3. |
| Piper voice licences are restrictive | Medium | High | Per-voice licence table; only recommend licences compatible with the project. |
| GPL-3.0 engine constrains the project's licence | Certain (already true) | Medium | Decide D1 now; document it in README. |
| Wayland blocks global key grabs | Certain | Medium | Desktop-shortcut + IPC design (§3.6). |
| `libportaudio2` / PipeWire audio problems on a clean system | Medium | Medium | Clear error message; installation docs; test in M7 on a clean VM. |
| Python wheel availability for `piper-tts` / `onnxruntime` on 3.12/3.13 | Low–medium | Medium | Check in M1/M2 before adding dependency; pin `requires-python`. |
| Coqui XTTS maintenance risk | — | — | Not planned. |
| Piper Python API changes (project moved repos in 2025) | Medium | Medium | Pin versions in `uv.lock`; isolate behind `PiperEngine`. |
| Thermal throttling reduces sustained speed on the laptop | Medium | Low | Benchmark on mains and battery; chunk-based synthesis. |

## 11. Git and workflow notes

- Local repository has no commits; the first commit will be the M0 plan (`docs/PROJECT_PLAN.md`) after review, or M1 foundation after authorization.
- Nothing has been pushed. Push requires explicit Product Owner approval.
- No pull requests created.

## 12. Next step

Stop here. **Authorization needed before M1** (from ChatGPT or the Product Owner), and answers to **D1, D2, D5** at minimum so M1 can be built without rework.
