"""Read plain text from the Wayland clipboard by running ``wl-paste``.

The reader never touches the clipboard directly. It runs ``wl-paste`` as a
subprocess, which keeps the application free of X11 and Wayland client libraries.
Every failure is raised as :class:`ClipboardError` with a message written for a
desktop user. Tests replace the subprocess runner with a fake, so no clipboard is
needed to run them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Final

from reader_assistant.limits import MAX_TEXT_BYTES

WL_PASTE: Final = "wl-paste"
_TIMEOUT_SECONDS: Final = 3.0
_PREFERRED_TYPES: Final = ("text/plain;charset=utf-8", "text/plain")
_EMPTY_MARKERS: Final = ("no selection", "nothing is copied")


class ClipboardError(Exception):
    """The clipboard cannot provide speakable text. The message is for the user."""


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[bytes]]


def _run_subprocess(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(args), capture_output=True, timeout=timeout, check=False)


def read_clipboard_text(
    *,
    runner: CommandRunner | None = None,
    executable: str = WL_PASTE,
) -> str:
    """Return the clipboard's plain text, unchanged.

    Raises:
        ClipboardError: if ``wl-paste`` is missing or fails, the clipboard is empty,
            it holds no plain text, the text is not valid UTF-8, or it is too large.
    """
    run = runner or _run_subprocess

    listing = _invoke(run, [executable, "--list-types"], action="read the clipboard")
    offered = listing.stdout.decode("utf-8", errors="replace").split()
    if not offered:
        raise ClipboardError("The clipboard is empty.")

    mime_type = next((t for t in _PREFERRED_TYPES if t in offered), None)
    if mime_type is None:
        raise ClipboardError(
            "The clipboard does not contain plain text. Copy some text and try again."
        )

    content = _invoke(
        run,
        [executable, "--no-newline", "--type", mime_type],
        action="read the clipboard text",
    )
    if len(content.stdout) > MAX_TEXT_BYTES:
        raise ClipboardError(
            f"The clipboard text is too long ({len(content.stdout)} bytes; "
            f"the limit is {MAX_TEXT_BYTES})."
        )
    try:
        text = content.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClipboardError("The clipboard text is not valid UTF-8.") from exc
    if not text.strip():
        raise ClipboardError("The clipboard is empty.")
    return text


def _invoke(
    run: CommandRunner, args: list[str], *, action: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = run(args, _TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise ClipboardError(
            "The wl-paste program is not installed. Install the wl-clipboard package."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"Could not {action}: the clipboard did not respond in time.") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        first_line = stderr.splitlines()[0] if stderr else ""
        lowered = stderr.lower()
        no_output = not completed.stdout.strip()
        if no_output and any(marker in lowered for marker in _EMPTY_MARKERS):
            raise ClipboardError("The clipboard is empty.")
        if no_output and not stderr and completed.returncode == 1:
            # wl-paste exits with status 1 and prints nothing when nothing is copied.
            raise ClipboardError("The clipboard is empty.")
        detail = first_line or f"exit code {completed.returncode}"
        raise ClipboardError(f"Could not {action}: {detail}")
    return completed
