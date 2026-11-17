"""Read plain text from the Wayland clipboard by running ``wl-paste``.

The reader never touches the clipboard directly. It runs ``wl-paste`` as a subprocess,
which keeps the application free of X11 and Wayland client libraries.

Size is enforced while the output is being read: :func:`run_command` reads at most
one byte beyond the limit, then stops the process. A huge clipboard therefore never
fills memory. Every failure is raised as :class:`ClipboardError` with a message for a
desktop user. Tests replace the command runner with a fake, so no clipboard is needed.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from reader_assistant.limits import MAX_TEXT_BYTES

WL_PASTE: Final = "wl-paste"
_TIMEOUT_SECONDS: Final = 3.0
_LISTING_MAX_BYTES: Final = 4096
_STDERR_MAX_BYTES: Final = 4096
_PREFERRED_TYPES: Final = ("text/plain;charset=utf-8", "text/plain")
_EMPTY_MARKERS: Final = ("no selection", "nothing is copied")


class ClipboardError(Exception):
    """The clipboard cannot provide speakable text. The message is for the user."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of a command. ``truncated`` means output exceeded the byte limit."""

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False


CommandRunner = Callable[[Sequence[str], float, int], CommandResult]


def run_command(args: Sequence[str], timeout: float, max_bytes: int) -> CommandResult:
    """Run ``args`` and read at most ``max_bytes`` of stdout.

    If the output is longer, the process is killed and the result is marked
    ``truncated`` with only the first ``max_bytes + 1`` bytes kept. On timeout the
    process is killed and ``subprocess.TimeoutExpired`` is raised.

    Raises:
        FileNotFoundError: if the executable does not exist.
        subprocess.TimeoutExpired: if the command runs longer than ``timeout`` seconds.
    """
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=stderr_file)
        expired = threading.Event()

        def _expire() -> None:
            expired.set()
            process.kill()

        timer = threading.Timer(timeout, _expire)
        timer.daemon = True
        timer.start()
        truncated = False
        stdout = b""
        try:
            assert process.stdout is not None
            stdout = process.stdout.read(max_bytes + 1)
            if len(stdout) > max_bytes:
                truncated = True
                process.kill()
            process.wait()
        finally:
            timer.cancel()
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()

        if expired.is_set() and not truncated:
            raise subprocess.TimeoutExpired(list(args), timeout)
        stderr_file.seek(0)
        stderr = stderr_file.read(_STDERR_MAX_BYTES)

    return CommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
    )


def read_clipboard_text(
    *,
    runner: CommandRunner | None = None,
    executable: str = WL_PASTE,
) -> str:
    """Return the clipboard's plain text, unchanged.

    Raises:
        ClipboardError: if ``wl-paste`` is missing or fails, the clipboard is empty,
            it holds no plain text, the text is not valid UTF-8, or it is longer than
            ``MAX_TEXT_BYTES``.
    """
    run = runner or run_command

    listing = _invoke(
        run,
        [executable, "--list-types"],
        max_bytes=_LISTING_MAX_BYTES,
        action="read the clipboard",
    )
    if listing.truncated:
        raise ClipboardError("The clipboard lists too many formats to read as text.")
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
        max_bytes=MAX_TEXT_BYTES,
        action="read the clipboard text",
    )
    if content.truncated or len(content.stdout) > MAX_TEXT_BYTES:
        raise ClipboardError(
            f"The clipboard text is longer than {MAX_TEXT_BYTES} bytes. "
            "Copy a shorter passage and try again."
        )
    try:
        text = content.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClipboardError("The clipboard text is not valid UTF-8.") from exc
    if not text.strip():
        raise ClipboardError("The clipboard is empty.")
    return text


def _invoke(
    run: CommandRunner,
    args: list[str],
    *,
    max_bytes: int,
    action: str,
) -> CommandResult:
    try:
        completed = run(args, _TIMEOUT_SECONDS, max_bytes)
    except FileNotFoundError as exc:
        raise ClipboardError(
            "The wl-paste program is not installed. Install the wl-clipboard package."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"Could not {action}: the clipboard did not respond in time.") from exc

    if completed.truncated:
        return completed  # the caller reports the size problem with its own message
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
