"""Desktop notification through ``notify-send`` (libnotify). No Python dependency.

Used only when the GNOME shortcut cannot reach a running service, so the user is told why
nothing was spoken. The command is short and bounded: it is waited for briefly, so no
process is left behind, and a missing or slow ``notify-send`` is logged and otherwise ignored.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from typing import Final

_log = logging.getLogger("reader_assistant.notify")
NOTIFY_SEND: Final = "notify-send"
TITLE: Final = "Reader Assistant is not running"
BODY: Final = (
    "Start it with: reader-assistant serve\nor open the window with: reader-assistant serve --gui"
)
_TIMEOUT_SECONDS: Final = 2.0

Runner = Callable[[Sequence[str]], object]


def _run(args: Sequence[str]) -> object:
    return subprocess.run(
        list(args),
        check=False,
        timeout=_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def notify_service_not_running(run: Runner | None = None) -> bool:
    """Show the "not running" notification. Returns False if it could not be shown."""
    runner = run if run is not None else _run
    try:
        runner([NOTIFY_SEND, "--app-name=Reader Assistant", TITLE, BODY])
    except FileNotFoundError:
        _log.warning("notify-send is not installed; the service-down notice was not shown")
        return False
    except subprocess.TimeoutExpired:
        _log.warning("notify-send did not finish in time; the service-down notice may be late")
        return False
    except OSError as exc:
        _log.warning("could not run notify-send: %s", exc)
        return False
    return True
