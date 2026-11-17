"""GUI controller: turns user actions into service calls and service state into display state.

This module does not import tkinter, so it is tested without a display. The window
(``ui/window.py``) draws what :meth:`GuiController.display_state` returns and forwards
clicks here. The controller never loads a model, synthesises, or touches the audio player.
Those stay on the service's worker thread.

Stale events: the service tags every event with a request ID. Errors are shown only for
the latest request. An error from an older request is dropped, so it cannot be mistaken
for the current one.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from reader_assistant.app.reader_service import (
    EventKind,
    ReaderService,
    ServiceState,
    SpeechError,
)
from reader_assistant.clipboard.reader import ClipboardError, read_clipboard_text

AUTO: Final = "auto"
LANGUAGE_CHOICES: Final = (AUTO, "en", "he", "ru")
DEFAULT_VOICE_LABEL: Final = "(default)"

_STATE_TEXT: Final = {
    ServiceState.IDLE: "Idle.",
    ServiceState.LOADING: "Loading voice…",
    ServiceState.SYNTHESIZING: "Preparing speech…",
    ServiceState.PLAYING: "Playing",
    ServiceState.PAUSED: "Paused",
}


@dataclass(frozen=True, slots=True)
class DisplayState:
    """Everything the window needs to draw, computed from one snapshot."""

    status: str
    error: str
    pause_label: str
    can_pause: bool
    can_stop: bool
    busy_pasting: bool


class GuiController:
    """Mediates between the window and the shared :class:`ReaderService`."""

    def __init__(
        self,
        service: ReaderService,
        *,
        clipboard_reader: Callable[[], str] = read_clipboard_text,
    ) -> None:
        self._service = service
        self._clipboard_reader = clipboard_reader
        self._latest_request_id: int | None = None
        self._error = ""
        self._paste_results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._paste_busy = False

    # --- user actions (all return at once) ----------------------------------

    def read(self, text: str, language: str, voice: str) -> str:
        """Queue ``text`` for speech. Returns the message to show, or "" if accepted.

        ``language`` is ``auto`` or a language code. ``voice`` is a voice ID, or the
        default label to use the language's default voice.
        """
        chosen_language = None if language == AUTO else language
        chosen_voice = None if voice in ("", DEFAULT_VOICE_LABEL) else voice
        try:
            accepted = self._service.submit(text, language=chosen_language, voice=chosen_voice)
        except SpeechError as exc:
            self._error = exc.message
            return exc.message
        self._latest_request_id = accepted.request_id
        self._error = ""
        return ""

    def toggle_pause(self) -> None:
        if self._service.status().state is ServiceState.PAUSED:
            self._service.resume()
        else:
            self._service.pause()

    def stop(self) -> None:
        self._service.stop()

    def set_volume_percent(self, percent: float) -> None:
        self._service.set_volume(max(0.0, min(100.0, percent)) / 100.0)

    def voices_for(self, language: str) -> tuple[str, ...]:
        """Voice labels for the language menu. Only explicit languages have choices."""
        if language == AUTO:
            return (DEFAULT_VOICE_LABEL,)
        ids = tuple(voice for lang, voice in self._service.voices() if lang == language)
        return (DEFAULT_VOICE_LABEL, *ids)

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

    # --- state ----------------------------------------------------------------

    def drain(self) -> None:
        """Apply every event that arrived since the last call. Call from the window's timer."""
        for event in self._service.drain_events():
            if event.kind is EventKind.ACCEPTED and event.request_id is not None:
                if self._latest_request_id is None or event.request_id > self._latest_request_id:
                    self._latest_request_id = event.request_id
                    self._error = ""
            # An error for an older request is stale and is dropped.
            elif event.kind is EventKind.ERROR and (
                event.request_id is None or event.request_id == self._latest_request_id
            ):
                self._error = event.message

    def display_state(self) -> DisplayState:
        status = self._service.status()
        playing_label = ""
        if status.state in (ServiceState.PLAYING, ServiceState.PAUSED) and status.language:
            playing_label = f" ({status.language}, {status.voice_id})"
        return DisplayState(
            status=_STATE_TEXT[status.state] + playing_label,
            error=self._error,
            pause_label="Resume" if status.state is ServiceState.PAUSED else "Pause",
            can_pause=status.state in (ServiceState.PLAYING, ServiceState.PAUSED),
            can_stop=status.state is not ServiceState.IDLE,
            busy_pasting=self._paste_busy,
        )
