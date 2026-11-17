# Architecture

This document describes the module layout and the responsibility of each module. The rationale for the broader design is in [PROJECT_PLAN.md](PROJECT_PLAN.md). This file is kept current as modules move from placeholder to implementation.

## Layers

```
 UI and entry points   cli.py (M1)   ui/ (M5)   hotkeys/ (M6)
          │
 Application core      app/ (M4)            config.py, logging_setup.py (M1)
          │
 Services              clipboard/ (M4)   language/ (M4)   engines/ (M2)   audio/ (M3)
```

Dependencies point downwards. Engines, audio and clipboard are injected into the application core rather than imported by it, so each can be replaced or faked in tests.

## Modules

| Module | Status | Responsibility |
|---|---|---|
| `reader_assistant/__init__.py` | Implemented (M1) | Exposes `__version__`, read from the installed package metadata. |
| `reader_assistant/__main__.py` | Implemented (M1) | Makes `python -m reader_assistant` run the CLI. |
| `reader_assistant/cli.py` | Implemented (M1) | Argument parsing, exit codes, `info` command. Wires configuration and logging together. Contains no business logic. |
| `reader_assistant/config.py` | Implemented (M1) | XDG path resolution, the `AppConfig` dataclass, loading and validation from TOML, environment and defaults. Pure functions; no global state. |
| `reader_assistant/logging_setup.py` | Implemented (M1) | Configures the `reader_assistant` logger: stderr plus a rotating file, with `key=value` extras. Idempotent. |
| `reader_assistant/app/reader_service.py` | Implemented (M4) | `ReaderService`: validates text, detects language, chooses the engine and voice, and speaks on one worker thread. Owns the registry and the single `AudioPlayer`. |
| `reader_assistant/engines/base.py` | Implemented (M2) | `TTSEngine` protocol, `Voice`, `AudioBuffer` (mono 16-bit PCM), `EngineError`. No heavy imports. |
| `reader_assistant/engines/registry.py` | Implemented (M2) | `EngineRegistry`: default engine per language, with fallbacks in registration order. Never loads a model. |
| `reader_assistant/engines/kokoro.py` | Implemented (M2) | Kokoro-82M English adapter via `kokoro-onnx` (CPU). Optional extra `kokoro`. Model factory is injectable for tests. |
| `reader_assistant/engines/piper.py` | Implemented (M2) | Piper adapter, one instance per voice model. Speed maps to `length_scale = 1 / speed`. Optional extra `piper`. |
| `reader_assistant/audio/player.py` | Implemented (M3) | `AudioPlayer`: background playback thread with play, pause, resume, stop, volume, and clean shutdown. Engine-independent. |
| `reader_assistant/audio/sink.py` | Implemented (M3) | `AudioSink` protocol (the hardware boundary) and `SoundDeviceSink` (PortAudio via the optional `audio` extra). |
| `reader_assistant/engines/catalog.py` | Implemented (M3) | Default language-to-voice mapping: English, Hebrew and Russian. Builds the registry without loading models. |
| `reader_assistant/clipboard/reader.py` | Implemented (M4) | `read_clipboard_text()`: runs `wl-paste`, checks for plain text, size and UTF-8, and raises `ClipboardError` with a user-facing message. The runner is injectable. |
| `reader_assistant/language/detect.py` | Implemented (M4) | Script-based language detection for English, Russian and Hebrew, with deterministic tie-breaking. No external dependency. |
| `reader_assistant/ui/` | Placeholder (M5) | Tkinter window. Runs on the main thread and never blocks on synthesis or playback. |
| `reader_assistant/ipc/` | Implemented (M4) | Unix socket IPC: `protocol.py` (JSON lines, validation, limits), `paths.py` (socket location), `server.py` (service side, stale-socket handling), `client.py` (CLI side). Knows nothing of the clipboard or engines. |
| `reader_assistant/limits.py` | Implemented (M4) | Shared size limit for speech text (5,000 bytes; temporary V1 limit until chunked synthesis in M6). |
| `reader_assistant/hotkeys/` | Placeholder (M6) | Reserved for shortcut helpers. The shortcut itself is configured in GNOME (see `docs/CLIPBOARD.md`). |

## Configuration and paths

- Config file: `$XDG_CONFIG_HOME/reader-assistant/config.toml`, overridable with `--config`.
- Logs: `$XDG_STATE_HOME/reader-assistant/logs/reader-assistant.log` (rotating, 1 MB × 3).
- Models: `$XDG_DATA_HOME/reader-assistant/models`. Never inside the repository.

Precedence: defaults < file < environment (`RA_LOG_LEVEL`, `RA_MODELS_DIR`) < command-line options.

## Error handling

- Configuration errors are raised as `ConfigError`. The CLI reports them on stderr and exits with code 2.
- Unknown configuration keys are errors, not warnings, so that typos are caught.
- The application logger never propagates to the root logger, which avoids duplicate lines in host applications.

## Testing approach

- `tests/test_config.py`: path resolution, precedence, validation and rejection of invalid input.
- `tests/test_cli.py`: exit codes, `--version`, `info` output, log file creation, and error reporting, run against temporary directories.
- `tests/test_logging_setup.py`: structured output, level filtering, idempotent configuration.
- `tests/conftest.py`: resets the process-global application logger around each test.

Tests never read or write the real home directory; the CLI accepts explicit `env` and `home` for this.

## Tooling

- Packaging and environments: uv (`uv_build` backend).
- Lint and format: Ruff (`E, W, F, I, B, UP, SIM, N, RUF`, line length 100).
- Type checking: mypy in strict mode. Chosen over pyright because it is pure Python and runs from the uv environment without a Node.js dependency; `mypy` is configured in `pyproject.toml`.
- Tests: pytest, with `filterwarnings = error` so resource leaks and deprecations fail the run.
