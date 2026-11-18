"""GUI controller: turns user actions into service requests and link messages into display state.

This module does not import Qt, so it is tested without a display. The window draws what
:meth:`GuiController.display_state` returns and forwards clicks here. The controller never
loads a model, synthesises, or touches audio. It only sends operations over the
:class:`~reader_assistant.ui.service_link.ServiceLink`, and it only reads what the link has
delivered.

Stale errors: an error event is shown only for the latest request, so an error from an older
request cannot be mistaken for the current one.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from reader_assistant.clipboard.reader import ClipboardError, read_clipboard_text
from reader_assistant.ui.service_link import LinkMessage, LinkState, ServiceLink

_STATE_TEXT: Final = {
    "idle": "Idle.",
    "loading": "Loading voice…",
    "synthesizing": "Preparing speech…",
    "playing": "Playing",
    "paused": "Paused",
}
# Events that can change what the service is doing. Each one asks for a fresh status.
_STATE_EVENTS: Final = frozenset(
    {
        "loading",
        "synthesis_started",
        "synthesis_completed",
        "playback_started",
        "paused",
        "resumed",
        "stopped",
        "finished",
        "cancelled",
        "error",
        "shutdown",
    }
)


@dataclass(frozen=True, slots=True)
class DisplayState:
    """Everything the window needs to draw."""

    status: str
    error: str
    connection: str
    pause_label: str
    can_pause: bool
    can_stop: bool
    can_retry: bool
    busy_pasting: bool


class GuiController:
    """Mediates between the window and the background service, through the link."""

    def __init__(
        self,
        link: ServiceLink,
        *,
        clipboard_reader: Callable[[], str] = read_clipboard_text,
    ) -> None:
        self._link = link
        self._clipboard_reader = clipboard_reader
        self._state = "unknown"
        self._language: str | None = None
        self._voice_id: str | None = None
        self._connection = LinkState.CONNECTING
        self._connection_text = "Connecting to the speech service…"
        self._volume_sync_pending = False
        self._volume_percent: float | None = None
        self._latest_request_id: int | None = None
        self._error = ""
        self._paste_results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._paste_busy = False

    # --- user actions (all return at once) ----------------------------------

    def read(self, text: str) -> str:
        """Queue ``text`` for speech. Returns a message to show now, or "" if it was sent.

        The service detects the language and picks the voice. The GUI cannot override it. A
        rejection from the service arrives later and is shown as the error.
        """
        if not text.strip():
            self._error = "The text is empty."
            return self._error
        self._error = ""
        self._link.send("speak", {"text": text})
        return ""

    def toggle_pause(self) -> None:
        self._link.send("resume" if self._state == "paused" else "pause")

    def stop(self) -> None:
        self._link.send("stop")

    def set_volume_percent(self, percent: float) -> None:
        clamped = max(0.0, min(100.0, percent))
        self._link.send("set_volume", {"volume": clamped / 100.0}, coalesce=True)

    def retry(self) -> None:
        self._link.retry()

    def start_paste(self) -> None:
        """Read the clipboard on a helper thread, so a slow clipboard never blocks the window."""
        if self._paste_busy:
            return
        self._paste_busy = True

        def read_in_background() -> None:
            try:
                self._paste_results.put(("text", self._clipboard_reader()))
            except ClipboardError as exc:
                self._paste_results.put(("error", str(exc)))

        threading.Thread(
            target=read_in_background, name="reader-assistant-paste", daemon=True
        ).start()

    def poll_paste(self) -> str | None:
        """Return pasted text if a paste finished, "" after an error, or None if still waiting."""
        try:
            kind, value = self._paste_results.get_nowait()
        except queue.Empty:
            return None
        self._paste_busy = False
        if kind == "error":
            self._error = value
            return ""
        self._error = ""
        return value

    # --- link messages --------------------------------------------------------

    def pump(self) -> None:
        """Apply everything the link delivered since the last call. Call from the GUI thread."""
        for message in self._link.poll():
            self.apply(message)

    def shutdown(self) -> None:
        """Disconnect from the service. The service keeps running."""
        self._link.close(timeout=0.3)

    def apply(self, message: LinkMessage) -> None:
        """Update the model from one message delivered by the link."""
        if message.kind == "connection":
            self._apply_connection(message)
        elif message.kind == "status":
            self._apply_status(message.data)
        elif message.kind == "event":
            self._apply_event(message.data)
        elif message.kind == "result":
            self._apply_result(message)

    def take_volume_sync(self) -> float | None:
        """The service volume in percent, once after each connection, for the slider."""
        if self._volume_sync_pending and self._volume_percent is not None:
            self._volume_sync_pending = False
            return self._volume_percent
        return None

    def _apply_connection(self, message: LinkMessage) -> None:
        assert message.state is not None
        self._connection = message.state
        self._connection_text = message.text
        if message.state is LinkState.CONNECTED:
            self._volume_sync_pending = True

    def _apply_status(self, data: dict[str, Any]) -> None:
        self._state = str(data.get("state", "unknown"))
        language = data.get("language")
        voice = data.get("voice_id")
        self._language = language if isinstance(language, str) else None
        self._voice_id = voice if isinstance(voice, str) else None
        volume = data.get("volume")
        if isinstance(volume, int | float):
            self._volume_percent = float(volume) * 100.0

    def _apply_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        request_id = event.get("request_id")
        if kind == "accepted" and isinstance(request_id, int):
            if self._latest_request_id is None or request_id > self._latest_request_id:
                self._latest_request_id = request_id
                self._error = ""
        elif kind == "error" and self._is_current(request_id):
            self._error = str(event.get("message", ""))
        if kind in _STATE_EVENTS:
            self._link.send("status", coalesce=True)

    def _is_current(self, request_id: object) -> bool:
        return (
            request_id is None
            or self._latest_request_id is None
            or request_id == self._latest_request_id
        )

    def _apply_result(self, message: LinkMessage) -> None:
        if not message.ok:
            self._error = message.text
            return
        if message.op == "speak":
            request_id = message.data.get("request_id")
            if isinstance(request_id, int):
                self._latest_request_id = request_id
            self._error = ""

    # --- state ----------------------------------------------------------------

    def display_state(self) -> DisplayState:
        connected = self._connection is LinkState.CONNECTED
        playing_label = ""
        if connected and self._state in ("playing", "paused") and self._language:
            playing_label = f" ({self._language}, {self._voice_id})"
        if connected:
            status = _STATE_TEXT.get(self._state, "Connected.") + playing_label
        else:
            status = self._connection_text
        return DisplayState(
            status=status,
            error=self._error,
            connection="" if connected else self._connection_text,
            pause_label="Resume" if self._state == "paused" else "Pause",
            can_pause=connected and self._state in ("playing", "paused"),
            can_stop=connected and self._state not in ("idle", "unknown"),
            can_retry=self._connection is LinkState.UNAVAILABLE,
            busy_pasting=self._paste_busy,
        )
