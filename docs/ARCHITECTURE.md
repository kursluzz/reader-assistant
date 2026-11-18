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
| `reader_assistant/app/reader_service.py` | Implemented (M4, events M5-4C) | `ReaderService`: validates text, detects language, chooses the engine and voice, and speaks on one worker thread. Owns the registry and the single `AudioPlayer`. Publishes numbered events to bounded subscriptions. |
| `reader_assistant/app/subscriptions.py` | Implemented (M5-4C) | `Subscription`: a bounded, thread-safe event queue per subscriber. A full queue ends the subscription (`overflow`); publishing never blocks. |
| `reader_assistant/app/control.py` | Implemented (M5-4C) | `ServiceControl`: runs the protocol version 2 operations on the one `ReaderService`, and converts status and events to plain dictionaries. |
| `reader_assistant/engines/base.py` | Implemented (M2) | `TTSEngine` protocol, `Voice`, `AudioBuffer` (mono 16-bit PCM), `EngineError`. No heavy imports. |
| `reader_assistant/engines/registry.py` | Implemented (M2) | `EngineRegistry`: default engine per language, with fallbacks in registration order. Never loads a model. |
| `reader_assistant/engines/kokoro.py` | Implemented (M2) | Kokoro-82M English adapter via `kokoro-onnx` (CPU). Optional extra `kokoro`. Model factory is injectable for tests. |
| `reader_assistant/engines/piper.py` | Implemented (M2) | Piper adapter, one instance per voice model. Speed maps to `length_scale = 1 / speed`. Optional extra `piper`. |
| `reader_assistant/audio/player.py` | Implemented (M3) | `AudioPlayer`: background playback thread with play, pause, resume, stop, volume, and clean shutdown. Engine-independent. |
| `reader_assistant/audio/sink.py` | Implemented (M3) | `AudioSink` protocol (the hardware boundary) and `SoundDeviceSink` (PortAudio via the optional `audio` extra). |
| `reader_assistant/engines/catalog.py` | Implemented (M3) | Default language-to-voice mapping: English, Hebrew and Russian. Builds the registry without loading models. |
| `reader_assistant/clipboard/reader.py` | Implemented (M4) | `read_clipboard_text()`: runs `wl-paste`, checks for plain text, size and UTF-8, and raises `ClipboardError` with a user-facing message. The runner is injectable. |
| `reader_assistant/language/detect.py` | Implemented (M4) | Script-based language detection for English, Russian and Hebrew, with deterministic tie-breaking. No external dependency. |
| `reader_assistant/ui/window.py` | Implemented (M5-4B, client M5-4C) | PySide6 window (optional `gui` extra). A client of the service. Runs on the Qt main thread and reads link messages on a timer there. |
| `reader_assistant/ui/service_link.py` | Implemented (M5-4C) | Framework-free connection to the service: subscribes to events, reconnects with bounded backoff, starts the background service when none runs, runs commands one at a time. Never owns a `ReaderService`. |
| `reader_assistant/ui/controller.py` | Implemented (M5-4, client M5-4C) | Framework-free GUI logic: actions become service operations, link messages become display state, latest-request error filtering. No Qt import. |
| `reader_assistant/ui/latency.py` | Implemented (M5-4) | Event-loop latency probe. Framework-free. |
| `reader_assistant/notify.py` | Implemented (M5-4B) | Service-down desktop notification through `notify-send`. Used only by the shortcut's CLI path. |
| `reader_assistant/ipc/` | Implemented (M4, version 2 M5-4C) | Unix socket IPC: `protocol.py` (JSON lines, versions 1 and 2, validation, limits), `paths.py` (socket location), `server.py` (service side, version routing, event streams), `client.py` (CLI and GUI side). Knows nothing of the clipboard or engines. |
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

## Service API and events (M5-1)

`ReaderService` is the single owner of engines and the audio player. The CLI, the IPC socket and, from M5, the GUI all talk to it.

- **Requests:** `submit(text)` returns `Accepted(request_id, language, voice_id)`. The language is detected from the text and the voice is that language's default. There are no language or voice arguments (removed in M5-4B). Unsupported text raises `SpeechError` with code `unsupported`.
- **Controls:** `stop()`, `pause()`, `resume()` and `set_volume()` return at once. The worker thread performs every player call, so a stop never races a play.
- **Status:** `status()` returns an immutable `ServiceStatus` (state, active request, language, voice, per-engine loaded flags, last error, volume, `event_seq`).
- **Events:** `subscribe()` returns the status and a bounded `Subscription` of `Event` objects, in order (see "Events (M5-4C)" below). Each event carries its `request_id` and `seq`. Kinds: accepted, cancelled, loading, synthesis started and completed, playback started, paused, resumed, stopped, finished, error and shutdown.
- **Latest request wins:** a newer request replaces one not yet started. It interrupts playback only once its own audio is ready. The status keeps the audible request until then. A stop cancels a request that is still being prepared, and its result is discarded.
- **Threading:** one worker thread runs loading, synthesis and all player calls. Playback ends on its own thread and reports back through a callback that checks the request ID. No callback runs on the worker.
- **Protocol:** version 1 (`speak` only) is unchanged. Version 2 adds the GUI operations (see "IPC protocol version 2" below).

## Events (M5-4C)

- Every event carries `seq`, a number that increases by one for each event the service publishes, starting at 1. `ServiceStatus.event_seq` is the last number published.
- `ReaderService.subscribe()` returns the current status and a subscription together, under one lock. Nothing is missed or seen twice. Events after the status have a higher `seq`. Nothing is replayed; the status is the recovery point.
- Each subscription has a bounded queue (256 events). The service never waits for a subscriber. If a subscriber falls behind, its queue is dropped and it is ended with reason `overflow`. Its client must subscribe again. This keeps memory bounded.
- With no subscriber (the headless service used only by the shortcut), publishing costs nothing and nothing accumulates. Before M5-4C an unread event queue grew for as long as the service ran.
- Shutdown publishes `shutdown` last, then ends every subscription with reason `shutdown`.

## IPC protocol version 2 (M5-4C)

Version 1 is unchanged and remains what the shortcut sends. Version 2 is used by the window.

Request (one per connection, except `subscribe`):

```json
{"version": 2, "op": "speak", "id": "r1", "payload": {"text": "Hello"}}
```

Reply:

```json
{"version": 2, "id": "r1", "ok": true, "result": {"request_id": 4, "language": "en", "voice": "af_heart"}}
{"version": 2, "id": "r1", "ok": false, "error": {"code": "invalid_payload", "message": "..."}}
```

- `id` is optional, at most 64 characters, and echoed back. Unknown top-level fields are rejected.
- Operations and payloads:

| `op` | Payload | Result |
|---|---|---|
| `speak` | `{"text": str}` only. `language` and `voice` are refused: the service detects them. | `request_id`, `language`, `voice` |
| `status` | none | the status snapshot (state, request, language, voice, engines, last error, volume, `event_seq`) |
| `pause` | none | `changed` (bool), `state`. Does nothing unless playing; `changed` is false otherwise. |
| `resume` | none | `changed`, `state`. Does nothing unless paused. |
| `stop` | none | `changed` (true if something was active), `state`. Idempotent. |
| `set_volume` | `{"volume": number}` from 0.0 to 1.0 | `volume` |
| `subscribe` | none | see below |

- Errors: `unsupported_version`, `unknown_operation`, `invalid_payload`, `bad_request`, and the speech codes (`empty_text`, `too_large`, `unsupported`, `shutting_down`, `engine_failed`, `audio_failed`, `internal`).
- Requests that cannot be read far enough to know the version (invalid JSON, unknown version) get a version 1 rejection. A version 2 client can read it, and it is never an unexplained disconnect.

Subscription:

1. The client sends `{"version": 2, "op": "subscribe"}`.
2. The service replies `ok` with `result.status`, the snapshot.
3. The service sends one `{"version": 2, "type": "event", "event": {...}}` line per event, in order.
4. The service ends with `{"version": 2, "type": "closed", "reason": "shutdown" | "overflow"}` and closes the connection. If the client disconnects, the service stops sending and forgets the subscriber.

A slow subscriber cannot block speech, playback or other clients. Its socket writes run on its own connection thread, and the service only appends to its bounded queue.

Listen backlog: 64 (it was the default 5, which refused bursts of clients with `EAGAIN`).

## GUI as a client (M5-4C)

- The window never creates a `ReaderService`, an engine or an `AudioPlayer`. Only the background service owns those. There is exactly one service owner and one audio owner.
- `serve --gui` checks the socket. If a service answers, the window connects. If nothing listens, the window starts `reader-assistant serve` as a separate process (new session, its output discarded; its log is in the usual log directory), then connects. The starter is attempted at most three times in a row. If the service still does not answer, the window shows the reason, the log location and a Retry button. Retry starts the attempts again.
- The window holds one subscription and reconnects with bounded backoff (0.5 s, 1 s, 2 s, 4 s, then 5 s). After a reconnect it takes a fresh status. It never waits for old events. Duplicate and older events are dropped by `seq`.
- Commands (speak, pause, resume, stop, volume) run one at a time on their own thread, in the order sent. Volume changes queued while a previous one is waiting are replaced by the newest.
- Two threads do all socket work: events and commands. They put messages in a queue. The window reads that queue on its 100 ms timer, on the Qt thread. No widget is touched from a socket thread.
- Closing the window disconnects. The service keeps running, and the shortcut keeps working.
- A service stop shows "The speech service stopped. Reconnecting." and the window reconnects when it is back.
- Dependencies: `gui` extra = `pyside6-essentials` and `shiboken6` only. No WebEngine or PDF modules. Nothing new is needed for version 2 or the link.

## Launch and autostart

- `reader-assistant serve`: the headless service, in the foreground. Login autostart runs this command, and only this command.
- `reader-assistant serve --gui`: the window, as a client. It starts the service only when none is running.
- The shortcut runs `read-clipboard`, which uses version 1.
