# Clipboard reading and keyboard shortcut (M4)

Copy text in any application, press a shortcut, and Reader Assistant reads it aloud.

```
You copy text ──► GNOME custom shortcut ──► reader-assistant read-clipboard
                                                │  wl-paste (clipboard)
                                                │  Unix socket (JSON lines)
                                                ▼
                 reader-assistant serve ──► language ──► TTS engine ──► AudioPlayer ──► speakers
                 (one process, started once)
```

The application never grabs keys. GNOME owns the shortcut and runs our command.

## 1. Install the requirements

```bash
sudo apt install wl-clipboard libportaudio2   # wl-paste, and PortAudio for playback
uv sync --all-extras                          # from the repository directory
```

`wl-clipboard` provides `wl-paste`. It works on Wayland, which is the target session. X11 is not supported.

## 2. Start the service

```bash
uv run reader-assistant serve
```

The service runs in the foreground, loads voices on first use, and stops on Ctrl+C or SIGTERM. It removes its socket when it stops. Logs go to `~/.local/state/reader-assistant/logs/reader-assistant.log`.

Only one instance can run at a time. A second `serve` reports that another instance is already running and exits with status 1.

Start it at login (optional): create `~/.config/autostart/reader-assistant.desktop`, replacing `<repo>` with the absolute path to your clone:

```ini
[Desktop Entry]
Type=Application
Name=Reader Assistant service
Exec=<repo>/.venv/bin/reader-assistant serve
X-GNOME-Autostart-enabled=true
```

## 3. Set up the GNOME keyboard shortcut

GNOME's shortcut runs with its own environment, so give it the **absolute path** to the command, not just `reader-assistant`:

1. Open **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**.
2. Click **+** (Add Custom Shortcut).
3. Name: `Read clipboard aloud`
4. Command: `<repo>/.venv/bin/reader-assistant read-clipboard`
5. Click **Set Shortcut**, press **Super+Shift+R** (or another combination you prefer), and confirm.

To change the shortcut later, return to the same list and edit it. The suggested key is a convention; any free combination works.

The procedure above follows the GNOME Settings layout on Ubuntu. It has not been checked against every GNOME version.

## 4. Commands

| Command | Purpose | Output on success | Exit codes |
|---|---|---|---|
| `reader-assistant serve` | Run the service in the foreground. | Log lines on stderr. | `0` after a clean stop; `1` if another instance runs or the socket cannot be created. |
| `reader-assistant read-clipboard` | Read the clipboard and send it to the service. | Nothing. | `0` accepted; `1` clipboard or request failed; `3` service not running. |
| `reader-assistant info` | Show configuration and paths. | Settings. | `0`. |

Every failure prints one line on stderr starting with `reader-assistant:`, with no traceback. Exit code 2 means a configuration error, as before.

## 5. Language selection

The service picks the language from the letters in the text, by script:

- Hebrew letters count for Hebrew, Cyrillic for Russian, and Latin letters for English.
- The language with the most letters wins. Digits, punctuation, emoji and other scripts are ignored.
- On a tie, the configured `default_language` wins if it is one of the tied languages. Otherwise English wins if tied, and otherwise the first alphabetically.
- Text with no English, Russian or Hebrew letters is rejected with a message.

The voice follows the language: English uses Kokoro (`af_heart`), Hebrew uses Piper (`he_IL-saspeech-medium`) and Russian uses Piper (`ru_RU-denis-medium`). See [ENGINES.md](ENGINES.md).

**Limits:** the method looks only at scripts, not words. English and other Latin-script languages are read as English. Text in an unsupported language that contains a few supported letters may be read in the language of those letters.

## 6. Clipboard handling

- Only plain text is read (`text/plain`, preferring UTF-8). Images, files and rich text are rejected with a message.
- The text is passed on unchanged. The application does not trim or rewrite it.
- Invalid UTF-8 is rejected with a message rather than read with replacement characters.
- Text larger than 50,000 bytes is rejected.
- `wl-paste` is given 3 seconds. A timeout is reported, never waited on indefinitely.
- A missing `wl-paste`, an error from it, or an empty clipboard each produce their own message.

## 7. Service protocol

The socket is `$XDG_RUNTIME_DIR/reader-assistant/service.sock`. The directory is created with mode 0700 and must belong to the current user. The socket itself is 0600. If `XDG_RUNTIME_DIR` is not set, the socket goes under `~/.local/state/reader-assistant/`.

Each connection carries one request and one response, each a single line of JSON in UTF-8:

```json
{"version": 1, "command": "speak", "text": "Hello"}
{"version": 1, "status": "accepted", "language": "en", "voice": "af_heart"}
{"version": 1, "status": "rejected", "code": "empty_text", "message": "The text is empty."}
```

Rejection codes: `bad_request`, `too_large`, `empty_text`, `unsupported`, `engine_failed`, `audio_failed`, `shutting_down`, `internal`.

Limits: a message can be at most 512 KiB. Text can be at most 50,000 bytes.

Only `speak` exists. The service does not expose any other operation over the socket.

A stale socket file (left after a crash, with nothing listening) is removed on start. A live socket means another instance runs, so the new one refuses to start. A regular file at the socket path is never deleted.

## 8. Behaviour while speaking

- `read-clipboard` returns as soon as the service has accepted the text. It does not wait for speech.
- Synthesis happens after acceptance. The engine loads on first use, and playback starts when the whole text is synthesised.
- Only the newest request is kept while one is being prepared. A request that arrives while another waits to start replaces it.
- A new request stops any playback in progress.
- Engine and audio failures after acceptance are written to the log. They are not shown to you yet; the GUI in M5 will show them.

## 9. Limitations

- Wayland only. Under X11 the clipboard command will not find a Wayland clipboard.
- Plain text only. The primary selection (middle-click paste) is not read.
- Language is chosen by script, not meaning (see section 5).
- Long text takes time before any sound: Kokoro needs about 0.35 seconds to make one second of audio, and speech starts only after the whole text is ready.
- The service must be running. Nothing starts it automatically unless you set up the autostart entry.
- The text is read in full from the clipboard before the size check. The 3-second timeout limits how long this can take.
- Real-device playback is covered by [AUDIO.md](AUDIO.md); the clipboard path has been run on the development machine with a real Wayland clipboard, but the GNOME shortcut itself was not triggered from the keyboard during M4.
