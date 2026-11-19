# Changelog

All notable changes to Reader Assistant are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Releases are made with `bump-my-version`, which moves
the `[Unreleased]` entries below into a dated version section.

## [Unreleased]

### Added

- Background speech service (`reader-assistant serve`) that owns the voices and the sound output.
- English speech with Kokoro (`af_heart`), and Russian and Hebrew speech with Piper.
- Automatic language detection from the letters of the text, by script.
- `read-clipboard` for a GNOME keyboard shortcut; it reads the clipboard text through the service.
- Window (`serve --gui`) as a client of the service: text box, Read, Paste, Pause, Stop, Volume,
  status line and Retry.
- `/read-last` for Claude Code: reads the last assistant message of a session aloud.
- `info` command showing the effective configuration, paths and version.
- Installable package with a `desktop` extra (Kokoro, Piper, sound output and window), for
  `uv tool install`.

[Unreleased]: https://github.com/kursluzz/reader-assistant/commits/main
