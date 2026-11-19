# Installing, upgrading and uninstalling

This document covers the installed `reader-assistant` command: how to install it, upgrade it, remove it, and what happens to your data. For the first setup of voices and the keyboard shortcut, see the [README](../README.md) and [CLIPBOARD.md](CLIPBOARD.md).

## Prerequisites

- Linux with a **Wayland** session and GNOME (or another desktop where you can set a custom shortcut).
- [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 when it is missing (`--python 3.12` asks for it explicitly).
- System packages:
  ```bash
  sudo apt install wl-clipboard libportaudio2
  ```
  `libnotify-bin` (`notify-send`) is optional and only used for the "service is not running" notification.
- Voice model files. They are not part of the package and are downloaded once; see [Voice model files](../README.md#voice-model-files).

## Install

Once the release is on PyPI:

```bash
uv tool install --python 3.12 'reader-assistant[desktop]'
```

Until then, from a clone of the repository:

```bash
git clone https://github.com/kursluzz/reader-assistant.git
cd reader-assistant
uv tool install --python 3.12 '.[desktop]'
```

The `desktop` extra installs both voice engines (`kokoro`, `piper`), sound output (`audio`) and the window (`gui`). You can leave out extras you do not need, for example `reader-assistant` alone for a headless service without voices or sound. Extras are listed in the [README](../README.md#from-a-clone-development).

uv places the `reader-assistant` command in `~/.local/bin` (`uv tool dir --bin` shows the directory). If it is not on your `PATH`, run `uv tool update-shell` and open a new terminal.

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
- Stop the running service before the upgrade and start it again afterwards. A service that was already running keeps the old code until it restarts.
- Upgrading never changes the data listed in [Where your data lives](#where-your-data-lives).

To add the desktop parts to an existing install that has none (for example one installed as `reader-assistant` alone), replace it with the full install:

```bash
uv tool install --force --python 3.12 'reader-assistant[desktop]'
```

## Uninstall

```bash
uv tool uninstall reader-assistant
```

This removes the program: its tool environment and the `reader-assistant` command. It does not remove your data. Stop the service before you uninstall it.

To remove your data as well, delete the directories below. This cannot be undone, and the voice models must then be downloaded again.

```bash
rm -r ~/.config/reader-assistant ~/.local/share/reader-assistant ~/.local/state/reader-assistant
```

## Where your data lives

Paths follow the XDG base directories. The defaults are shown; the environment variables `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` change them. `reader-assistant info` prints the paths in use.

| What | Default location | Set by | Kept by upgrade | Kept by uninstall |
|---|---|---|---|---|
| Voice model files (Kokoro, Piper) | `~/.local/share/reader-assistant/models` | `RA_MODELS_DIR`, or `models_dir` in the configuration file | Yes | Yes |
| Configuration | `~/.config/reader-assistant/config.toml` | `--config PATH` | Yes | Yes |
| Logs | `~/.local/state/reader-assistant/logs` | `log_dir` in the configuration file | Yes | Yes |
| Last-message capture (for `/read-last`), including skip markers | `$XDG_RUNTIME_DIR/reader-assistant/handoff/` | Not configurable | Yes, while the session lasts | Yes, while the session lasts |
| Service socket | `$XDG_RUNTIME_DIR/reader-assistant/service.sock` | Not configurable | Recreated when the service starts | Removed when the service stops |
| Program (tool environment and command) | uv tools directory (`uv tool dir`) and `~/.local/bin/reader-assistant` | uv | Replaced | Removed |

Notes:

- The runtime directory is cleared at logout. If `XDG_RUNTIME_DIR` is not set, the socket and the capture files go under `~/.local/state/reader-assistant/` instead. Nothing needs to be kept there by hand.
- Only the program lives in uv's tool environment. The application never writes into it, so an upgrade or uninstall cannot remove models, configuration or logs.
- The development scripts (`scripts/`) also write benchmark results and sample audio under `~/.local/share/reader-assistant/`. Uninstalling the program does not remove them, and they are not needed to run the application.

## Shortcut, autostart and the Claude Code hook

Three things refer to the program by its absolute path, not by its name:

- the GNOME keyboard shortcut (`read-clipboard`),
- the autostart entry (`serve`), and
- the Claude Code Stop hook and the `/read-last` command (`capture-last` and `read-last`).

The setup in [CLIPBOARD.md](CLIPBOARD.md) and [CLAUDE_CODE.md](CLAUDE_CODE.md) points at `<repo>/.venv/bin/reader-assistant`. A `uv tool install` does not change those paths, so they keep working for as long as that virtual environment exists. To use the installed command instead, change the paths in those three places to the output of `which reader-assistant` (normally `~/.local/bin/reader-assistant`). Use the absolute path, not `~/...`: an autostart `Exec=` line does not expand `~`.

Only one service can run at a time. If you use both a clone and an installed command, start only one of them.
