"""The GUI's connection to the background service. Framework-free and thread-based.

The window is a client, never a second service. Two worker threads do all socket work:

- the event thread keeps one subscription open. It reconnects with bounded backoff, and if
  nothing listens it starts the background service in a separate process (``starter``), then
  retries. Starting is tried a few times in a row; after that the link waits for :meth:`retry`.
- the command thread runs one operation at a time, in the order they were sent.

Both threads publish :class:`LinkMessage` objects to an outbox. The window drains it with
:meth:`poll` from its own timer, so no widget is ever touched from these threads.

Closing the link disconnects. It never stops the service.
"""

from __future__ import annotations

import enum
import logging
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from reader_assistant.ipc.client import (
    EventSubscription,
    ServiceError,
    ServiceUnavailableError,
    request,
    subscribe,
)
from reader_assistant.ipc.protocol import ProtocolError

_log = logging.getLogger("reader_assistant.gui")
CONNECT_TIMEOUT_SECONDS: Final = 2.0
COMMAND_TIMEOUT_SECONDS: Final = 5.0
_READ_POLL_SECONDS: Final = 0.25
_START_WAIT_SECONDS: Final = 6.0
_START_POLL_SECONDS: Final = 0.1
MAX_START_ATTEMPTS: Final = 3
MAX_CONNECT_FAILURES: Final = 6
_BACKOFF_MIN_SECONDS: Final = 0.5
_BACKOFF_MAX_SECONDS: Final = 5.0
_GIVE_UP_PROBE_SECONDS: Final = 2.0

Starter = Callable[[], "subprocess.Popen[bytes]"]


class LinkState(enum.Enum):
    CONNECTING = "connecting"
    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LinkMessage:
    """One thing the window needs to know. ``kind`` is connection, status, event or result."""

    kind: str
    state: LinkState | None = None
    text: str = ""
    op: str = ""
    ok: bool = True
    code: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff: 0.5 s, 1 s, 2 s, then 5 s at most."""
    return float(min(_BACKOFF_MAX_SECONDS, _BACKOFF_MIN_SECONDS * (2 ** max(0, attempt - 1))))


class ServiceLink:
    """Keeps the GUI connected to the one background service."""

    def __init__(
        self,
        socket_path: Path,
        *,
        starter: Starter | None = None,
        log_hint: str = "",
    ) -> None:
        self._path = socket_path
        self._starter = starter
        self._log_hint = log_hint
        self._outbox: queue.Queue[LinkMessage] = queue.Queue()
        self._stop = threading.Event()
        self._retry = threading.Event()
        self._commands: deque[tuple[str, dict[str, Any] | None]] = deque()
        self._command_cond = threading.Condition()
        self._last_connection: tuple[LinkState, str] | None = None
        self._had_connection = False
        self._threads: list[threading.Thread] = []

    # --- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        """Start the two worker threads. Call once."""
        self._threads = [
            threading.Thread(target=self._run_events, name="reader-assistant-events", daemon=True),
            threading.Thread(
                target=self._run_commands, name="reader-assistant-commands", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def close(self, timeout: float = 1.0) -> None:
        """Disconnect and stop the worker threads. The service keeps running."""
        self._stop.set()
        with self._command_cond:
            self._command_cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def retry(self) -> None:
        """Try to connect (and start the service) again after giving up."""
        self._retry.set()

    # --- the window's side ----------------------------------------------------------

    def send(
        self, op: str, payload: dict[str, Any] | None = None, *, coalesce: bool = False
    ) -> None:
        """Queue an operation. With ``coalesce``, an older queued copy of ``op`` is dropped."""
        with self._command_cond:
            if coalesce:
                self._commands = deque(item for item in self._commands if item[0] != op)
            self._commands.append((op, payload))
            self._command_cond.notify_all()

    def poll(self) -> list[LinkMessage]:
        """Everything that happened since the last call. Call from the GUI thread."""
        messages: list[LinkMessage] = []
        while True:
            try:
                messages.append(self._outbox.get_nowait())
            except queue.Empty:
                return messages

    # --- event thread -----------------------------------------------------------------

    def _run_events(self) -> None:
        starts = 0
        failures = 0
        process: subprocess.Popen[bytes] | None = None
        while not self._stop.is_set():
            try:
                subscription = subscribe(self._path, timeout=CONNECT_TIMEOUT_SECONDS)
            except ServiceUnavailableError as exc:
                failures += 1
                if process is not None:
                    process.poll()  # reap a started service that has exited, so it does not linger
                if exc.not_running and self._starter is not None and starts < MAX_START_ATTEMPTS:
                    starts += 1
                    process = self._start_service(starts)
                    if process is not None:
                        self._wait_for_start(process)
                    self._sleep(backoff_seconds(starts))
                    continue
                if exc.not_running and self._starter is not None:
                    self._give_up(self._unavailable_text(process))
                    starts = 0
                    failures = 0
                    continue
                if failures >= MAX_CONNECT_FAILURES:
                    self._give_up(f"The speech service is not reachable. {exc}")
                    failures = 0
                    continue
                state = LinkState.RECONNECTING if self._had_connection else LinkState.CONNECTING
                self._publish_connection(state, str(exc))
                self._sleep(backoff_seconds(failures))
                continue
            starts = 0
            failures = 0
            with subscription:
                self._publish_connection(LinkState.CONNECTED, "Connected to the speech service.")
                self._had_connection = True
                self._publish(LinkMessage(kind="status", data=subscription.status))
                reason = self._pump(subscription, int(subscription.status.get("event_seq", 0)))
            if not self._stop.is_set():
                self._publish_connection(LinkState.RECONNECTING, reason)
                self._sleep(_BACKOFF_MIN_SECONDS)

    def _pump(self, subscription: EventSubscription, last_seq: int) -> str:
        """Forward events in order, dropping any the status already covers. Returns why it ended."""
        while not self._stop.is_set():
            try:
                message = subscription.read(_READ_POLL_SECONDS)
            except (ServiceUnavailableError, ProtocolError) as exc:
                return f"The connection to the service was lost ({exc})."
            if message is None:
                continue
            if message["type"] == "closed":
                if message.get("reason") == "shutdown":
                    return "The speech service stopped. Reconnecting."
                return "The connection was too slow and was closed. Reconnecting."
            event = message["event"]
            seq = int(event.get("seq", 0))
            if seq <= last_seq:
                continue  # already covered by the status, or a duplicate
            last_seq = seq
            self._publish(LinkMessage(kind="event", data=event))
        return "Disconnected."

    def _start_service(self, attempt: int) -> subprocess.Popen[bytes] | None:
        assert self._starter is not None
        self._publish_connection(LinkState.STARTING, "Starting the speech service…")
        try:
            process = self._starter()
        except OSError as exc:
            _log.warning("could not start the service", extra={"error": str(exc)})
            self._publish_connection(
                LinkState.STARTING, f"Could not start the service ({exc}). Trying again…"
            )
            self._sleep(backoff_seconds(attempt))
            return None
        _log.info("started the background service", extra={"attempt": attempt})
        return process

    def _wait_for_start(self, process: subprocess.Popen[bytes]) -> None:
        """Wait until the new service answers, or until its process has exited."""
        deadline = time.monotonic() + _START_WAIT_SECONDS
        while not self._stop.is_set() and time.monotonic() < deadline:
            if process.poll() is not None:
                return
            try:
                request(self._path, "status", timeout=CONNECT_TIMEOUT_SECONDS)
            except (ServiceUnavailableError, ServiceError, ProtocolError):
                self._stop.wait(_START_POLL_SECONDS)
                continue
            return

    def _give_up(self, text: str) -> None:
        """Wait for Retry, or for a service started by hand or at login, then connect."""
        self._publish_connection(LinkState.UNAVAILABLE, text)
        next_probe = time.monotonic() + _GIVE_UP_PROBE_SECONDS
        while not self._stop.is_set() and not self._retry.is_set():
            self._stop.wait(0.5)
            if time.monotonic() >= next_probe:
                next_probe = time.monotonic() + _GIVE_UP_PROBE_SECONDS
                if self._service_answers():
                    break
        self._retry.clear()

    def _service_answers(self) -> bool:
        try:
            request(self._path, "status", timeout=CONNECT_TIMEOUT_SECONDS)
        except (ServiceUnavailableError, ServiceError, ProtocolError):
            return False
        return True

    def _unavailable_text(self, process: subprocess.Popen[bytes] | None) -> str:
        hint = f" {self._log_hint}" if self._log_hint else ""
        if process is not None and process.poll() is not None:
            return f"The speech service did not start (exit code {process.returncode}).{hint}"
        return f"The speech service did not start.{hint}"

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _publish_connection(self, state: LinkState, text: str) -> None:
        if self._last_connection == (state, text):
            return  # the same message again is not shown again
        self._last_connection = (state, text)
        self._publish(LinkMessage(kind="connection", state=state, text=text))

    def _publish(self, message: LinkMessage) -> None:
        self._outbox.put(message)

    # --- command thread ---------------------------------------------------------------

    def _run_commands(self) -> None:
        while True:
            with self._command_cond:
                while not self._commands and not self._stop.is_set():
                    self._command_cond.wait(0.5)
                if self._stop.is_set():
                    return
                op, payload = self._commands.popleft()
            self._run_command(op, payload)

    def _run_command(self, op: str, payload: dict[str, Any] | None) -> None:
        try:
            result = request(self._path, op, payload, timeout=COMMAND_TIMEOUT_SECONDS)
        except ServiceUnavailableError as exc:
            code = "not_running" if exc.not_running else "unavailable"
            self._publish(LinkMessage(kind="result", op=op, ok=False, code=code, text=str(exc)))
        except ServiceError as exc:
            self._publish(
                LinkMessage(kind="result", op=op, ok=False, code=exc.code, text=exc.message)
            )
        except ProtocolError as exc:
            self._publish(
                LinkMessage(kind="result", op=op, ok=False, code=exc.code, text=exc.message)
            )
        else:
            if op == "status":
                self._publish(LinkMessage(kind="status", data=result))
            else:
                self._publish(LinkMessage(kind="result", op=op, data=result))


def start_background_service(extra_args: tuple[str, ...] = ()) -> subprocess.Popen[bytes]:
    """Start ``reader-assistant serve`` as a separate process that outlives the window."""
    argv = [sys.executable, "-m", "reader_assistant", *extra_args, "serve"]
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
