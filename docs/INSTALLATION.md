# Installing, upgrading and uninstalling

This document covers the installed `reader-assistant` command: how to install it, upgrade it, remove it, and where your data is kept. For the voice model files, see the [README](../README.md#voice-model-files). For the keyboard shortcut and autostart, see [CLIPBOARD.md](CLIPBOARD.md).

## Prerequisites

- Linux with a **Wayland** session and GNOME (or another desktop where you can set a custom shortcut). X11 is not supported.
- [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 when it is missing; `--python 3.12` asks for it explicitly.
- System packages:
  ```bash
  sudo apt install wl-clipboard libportaudio2
  ```
  `libnotify-bin` (`notify-send`) is optional. It only shows the "service is not running" notification.

## Install

From PyPI:

```bash
uv tool install --python 3.12 'reader-assistant[desktop]'
```

From a clone of the repository (use this until the release is on PyPI):

```bash
git clone https://github.com/kursluzz/reader-assistant.git
cd reader-assistant
uv tool install --python 3.12 '.[desktop]'
```

The `desktop` extra installs both voice engines (`kokoro`, `piper`), sound output (`audio`) and the window (`gui`). To leave some of them out, install an extra selection instead, for example `reader-assistant[kokoro,piper]`. Without `kokoro` or `piper`, that language cannot be spoken. Without `audio`, nothing is played.

uv places the `reader-assistant` command in `~/.local/bin`. Run `uv tool dir --bin` to see the directory. If it is not on your `PATH`, run `uv tool update-shell` and open a new terminal.

Check the installation:

```bash
reader-assistant --version
reader-assistant info      # configuration file, models directory, log directory
```

## Upgrade

```bash
uv tool upgrade reader-assistant
```

- uv keeps the extras recorded at install time, so a `desktop` install stays a `desktop` install.
- Stop the running service before the upgrade and start it again afterwards. A service that was already running keeps the old code until it restarts. Stop it with Ctrl+C in its terminal, or with `pkill -TERM -f 'reader.assistant serve'`, which also matches a service the window started.
- An upgrade does not change the data listed under [Where your data lives](#where-your-data-lives).

To add the desktop parts to an install that has only some of them, reinstall with the full set:

```bash
uv tool install --force --python 3.12 'reader-assistant[desktop]'
```

## Uninstall

Stop the service first, then:

```bash
uv tool uninstall reader-assistant
```

This removes the program: its tool environment and the `reader-assistant` command. It does not remove your data.

To remove your data as well, delete the directories below. This cannot be undone, and the voice models must then be downloaded again.

```bash
rm -r ~/.config/reader-assistant ~/.local/share/reader-assistant ~/.local/state/reader-assistant
```

## Where your data lives

Paths follow the XDG base directories. The defaults are shown. The environment variables `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` change them, and `reader-assistant info` prints the paths in use.

| What | Default location | Set by | Kept by upgrade | Kept by uninstall |
|---|---|---|---|---|
| Voice model files (Kokoro, Piper) | `~/.local/share/reader-assistant/models` | `RA_MODELS_DIR`, or `models_dir` in the configuration file | Yes | Yes |
| Configuration | `~/.config/reader-assistant/config.toml` | `--config PATH` | Yes | Yes |
| Logs | `~/.local/state/reader-assistant/logs` | `log_dir` in the configuration file | Yes | Yes |
| Last-message capture for `/read-last`, and its skip markers | `$XDG_RUNTIME_DIR/reader-assistant/handoff/` | Not configurable | Yes | Yes (cleared at logout) |
| Service socket | `$XDG_RUNTIME_DIR/reader-assistant/service.sock` | Not configurable | Recreated when the service starts | Removed when the service stops |
| Program (tool environment and command) | The uv tools directory (`uv tool dir`) and `~/.local/bin/reader-assistant` | uv | Replaced | Removed |

Notes:

- If `XDG_RUNTIME_DIR` is not set, the socket and the capture files go under `~/.local/state/reader-assistant/` instead.
- The program lives in uv's tool environment, and the application never writes into it. An upgrade or an uninstall therefore cannot remove models, configuration or logs.

## Keyboard shortcut, autostart and the Claude Code hook

These three integrations run the command by its absolute path. Find the path with `which reader-assistant` (normally `~/.local/bin/reader-assistant`) and use it in:

- the GNOME keyboard shortcut (`read-clipboard`), see [CLIPBOARD.md](CLIPBOARD.md#3-set-up-the-gnome-keyboard-shortcut),
- the autostart entry (`serve`), see [CLIPBOARD.md](CLIPBOARD.md#2-start-the-service), and
- the Claude Code Stop hook and the `/read-last` command, see [CLAUDE_CODE.md](CLAUDE_CODE.md).

Use the absolute path, not `~/...`: an autostart `Exec=` line does not expand `~`.

Only one service can run at a time. Start one service, and stop any other before you start it.
