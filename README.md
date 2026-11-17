# Reader Assistant

A lightweight, **local-first** desktop tool that turns clipboard text into natural speech.

1. Copy text from any application.
2. Press a keyboard shortcut or click a button.
3. The app reads the clipboard, identifies the language, picks a local voice and plays the audio.

No cloud speech APIs, no accounts, no text leaves your computer.

**Languages (target):** English, Russian, Hebrew.
**Platform (initial):** Linux desktop (X11 and Wayland), Python, CPU inference.
**Licence:** GPL-3.0-or-later (see [LICENSE](LICENSE)). Individual voice models keep their own licences; see the voice table once it exists in `docs/ENGINES.md`.

## Development status

**Milestone M1 — project foundation (in progress).** The package installs, has a CLI entry point, reads layered configuration, writes structured logs and is covered by tests and static checks.

Not implemented yet: text-to-speech engines (M2), audio playback (M3), clipboard reading and language detection (M4), the GUI (M5) and global shortcuts (M6). Running `reader-assistant` today does not read anything aloud.

Planning documents: [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) (milestones and decisions) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (module responsibilities).

## Developer installation

Requirements:

- Linux with Python 3.12 or newer. [uv](https://docs.astral.sh/uv/) manages the interpreter and dependencies.
- Tkinter is a system-provided package, not a pip dependency. The uv-managed CPython builds include it. If you use a distribution Python, install its Tk package (for example `sudo apt install python3-tk` on Debian/Ubuntu). This is needed from M5 onwards.
- Audio playback (from M3) uses PortAudio: `sudo apt install libportaudio2`.

```bash
git clone git@github.com:kursluzz/reader-assistant.git
cd reader-assistant
uv sync                       # creates .venv and installs dev tools
uv run reader-assistant --version
uv run reader-assistant info  # shows effective configuration and file locations
```

### Checks

Run these before every commit:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Configuration

Settings come from, in increasing precedence: built-in defaults, a TOML file, environment variables, and command-line options.

- File: `$XDG_CONFIG_HOME/reader-assistant/config.toml` (usually `~/.config/reader-assistant/config.toml`), or any file given with `--config PATH`.
- Environment: `RA_LOG_LEVEL`, `RA_MODELS_DIR`.
- Command line: `--log-level LEVEL`.

Example file:

```toml
log_level = "INFO"          # DEBUG, INFO, WARNING, ERROR
log_to_file = true          # logs go to $XDG_STATE_HOME/reader-assistant/logs
default_language = "auto"   # auto, en, ru, he
speech_rate = 1.0           # 0.25 to 4.0
volume = 1.0                # 0.0 to 1.0
```

Unknown keys and invalid values are rejected with a clear message and exit code 2, so typos do not silently do nothing.

Downloaded voice models are stored under `$XDG_DATA_HOME/reader-assistant/models` and are never part of the repository.

## Contributing

Contribution guidelines are scheduled for milestone M7. Until then: keep changes small, add tests for behaviour, and run the checks above before committing.
