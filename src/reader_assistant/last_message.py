"""Keep the last assistant message of each Claude Code session, and read it on request.

Two commands use this module:

- ``capture-last`` runs as a Claude Code ``Stop`` hook after every response. It only stores the
  text in a private file for that session. It never speaks, and it never fails the hook.
- ``read-last`` runs from the ``/read-last`` slash command. It reads the stored message aloud,
  part by part, through the running service.

The stored text lives in the service's private runtime directory (mode 0600, inside 0700), is
removed after a day, and is never logged.

Hooks also run after the ``/read-last`` command itself, and its reply would replace the message
to read. ``read-last`` therefore leaves a short-lived marker that makes the next capture for the
same session skip one response.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from reader_assistant.ipc.client import (
    ServiceError,
    ServiceUnavailableError,
    request,
    subscribe,
)
from reader_assistant.ipc.paths import socket_path
from reader_assistant.speech_text import split_for_speech, to_speech_text

_log = logging.getLogger("reader_assistant.last_message")
SESSION_ENV: Final = "CLAUDE_CODE_SESSION_ID"
STORED_MAX_AGE_SECONDS: Final = 24 * 60 * 60
SKIP_MARKER_TTL_SECONDS: Final = 300
_SESSION_ID: Final = re.compile(r"[A-Za-z0-9-]{1,64}")
_PART_TIMEOUT_SECONDS: Final = 3_600.0

Clock = Callable[[], float]


class MessageError(Exception):
    """A user-facing problem with reading the last message."""


def handoff_dir(environment: Mapping[str, str], home: Path) -> Path:
    """The private directory for stored messages (created with mode 0700)."""
    directory = socket_path(environment, home).parent / "handoff"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _file(directory: Path, session_id: str, suffix: str) -> Path:
    if not _SESSION_ID.fullmatch(session_id):
        raise MessageError("the session ID is not valid")
    return directory / f"{session_id}{suffix}"


def _write_private(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temporary, path)


def store_message(directory: Path, session_id: str, text: str, now: float) -> None:
    payload = json.dumps({"text": text, "saved_at": now}, ensure_ascii=False)
    _write_private(_file(directory, session_id, ".json"), payload)


def load_message(directory: Path, session_id: str) -> str | None:
    path = _file(directory, session_id, ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else None


def mark_skip_next_capture(directory: Path, session_id: str, now: float) -> None:
    _write_private(_file(directory, session_id, ".skip"), str(now))


def _take_skip_marker(directory: Path, session_id: str, now: float) -> bool:
    """True if a fresh marker says the next response is the reply to ``/read-last``."""
    path = _file(directory, session_id, ".skip")
    try:
        written_at = float(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return False
    path.unlink(missing_ok=True)
    return now - written_at <= SKIP_MARKER_TTL_SECONDS


def capture_from_hook(payload: Mapping[str, Any], directory: Path, now: float) -> bool:
    """Store the message from a ``Stop`` hook payload. Returns True if a message was stored."""
    if payload.get("hook_event_name") != "Stop":
        return False
    session_id = payload.get("session_id")
    text = payload.get("last_assistant_message")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return False
    if _take_skip_marker(directory, session_id, now):
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    store_message(directory, session_id, text, now)
    _prune(directory, now)
    return True


def _prune(directory: Path, now: float) -> None:
    for path in directory.glob("*.json"):
        try:
            if now - path.stat().st_mtime > STORED_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue


@dataclass(frozen=True, slots=True)
class ReadResult:
    parts: int
    stopped_early: bool = False


def read_last(
    environment: Mapping[str, str],
    home: Path,
    *,
    clock: Clock = time.time,
) -> ReadResult:
    """Read the last message of the current session aloud, part by part.

    Raises:
        MessageError: no session, no stored message, or nothing to read.
        ServiceUnavailableError: the speech service is not reachable.
        ServiceError: the service refused a part, or speech failed.
    """
    session_id = environment.get(SESSION_ENV, "")
    if not session_id:
        raise MessageError("this command must run inside a Claude Code session")
    directory = handoff_dir(environment, home)
    # The reply to this command must not replace the message it reads.
    mark_skip_next_capture(directory, session_id, clock())
    text = load_message(directory, session_id)
    if text is None:
        raise MessageError("there is no assistant message saved for this session yet")
    parts = split_for_speech(to_speech_text(text))
    if not parts:
        raise MessageError("the last message has nothing to read aloud")
    return _speak_parts(socket_path(environment, home), parts)


def _speak_parts(path: Path, parts: list[str]) -> ReadResult:
    """Send the parts one at a time: the next part starts when the previous one has finished."""
    events = subscribe(path)
    try:
        for index, part in enumerate(parts):
            reply = request(path, "speak", {"text": part})
            outcome = _wait_for_outcome(events, int(reply["request_id"]))
            if outcome == "finished":
                continue
            return ReadResult(parts=index, stopped_early=True)
    finally:
        events.close()
    return ReadResult(parts=len(parts))


def _wait_for_outcome(events: Any, request_id: int) -> str:
    """Return how one part ended: ``finished``, or ``stopped`` when another request took over."""
    while True:
        message = events.read(_PART_TIMEOUT_SECONDS)
        if message is None:
            continue
        if message.get("type") == "closed":
            raise ServiceUnavailableError("the speech service stopped while reading")
        event = message["event"]
        if event.get("request_id") != request_id:
            continue
        kind = event.get("kind")
        if kind == "finished":
            return "finished"
        if kind == "error":
            raise ServiceError(str(event.get("code", "")), str(event.get("message", "")))
        if kind in ("cancelled", "stopped"):
            return "stopped"


def capture_payload(text: str, environment: Mapping[str, str], home: Path) -> bool:
    """Parse a hook payload from stdin and store it. Never raises: a hook must not block Claude."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    try:
        return capture_from_hook(payload, handoff_dir(environment, home), time.time())
    except (OSError, MessageError, ValueError) as exc:
        _log.debug("could not store the last message", extra={"error": str(exc)})
        return False
