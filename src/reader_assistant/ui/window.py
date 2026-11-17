"""The Tkinter window (M5-4). It runs on the main thread and only draws what the controller returns.

Every action returns at once. Work happens in the service's worker thread, and results
come back through the controller, which the window polls on a timer. Closing the window
starts the shutdown on a helper thread, so the window never waits for it.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Final

from reader_assistant.ui.controller import (
    AUTO,
    DEFAULT_VOICE_LABEL,
    LANGUAGE_CHOICES,
    GuiController,
)
from reader_assistant.ui.latency import DEFAULT_INTERVAL_MS, LatencyProbe

POLL_MS: Final = 100
SHUTDOWN_CHECK_MS: Final = 50
SHUTDOWN_GIVE_UP_MS: Final = 10_000

ERROR_COLOUR: Final = "#b00020"


class GuiWindow:
    """The application window. Create and run it on the main thread."""

    def __init__(
        self,
        controller: GuiController,
        *,
        on_close: Callable[[], threading.Event],
        stop_requested: threading.Event | None = None,
        probe: LatencyProbe | None = None,
        clock: Callable[[], float] = time.perf_counter,
        root: tk.Tk | None = None,
    ) -> None:
        self._controller = controller
        self._on_close = on_close
        self._stop_requested = stop_requested
        self._probe = probe
        self._clock = clock
        self._closing = False

        self.root = root if root is not None else tk.Tk()
        self.root.title("Reader Assistant")
        self.root.geometry("640x420")
        self.root.minsize(480, 340)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.after(POLL_MS, self._tick)
        if self._probe is not None:
            self._schedule_probe()

    # --- layout -------------------------------------------------------------------

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Text to read").grid(row=0, column=0, sticky="w")
        self._text = tk.Text(frame, height=8, wrap="word")
        self._text.grid(row=1, column=0, sticky="nsew", pady=(4, 8))

        choices = ttk.Frame(frame)
        choices.grid(row=2, column=0, sticky="ew")
        self._language = tk.StringVar(value=AUTO)
        ttk.Label(choices, text="Language").pack(side="left")
        language_box = ttk.Combobox(
            choices,
            textvariable=self._language,
            values=LANGUAGE_CHOICES,
            width=7,
            state="readonly",
        )
        language_box.pack(side="left", padx=(4, 12))
        language_box.bind("<<ComboboxSelected>>", self._on_language_changed)

        ttk.Label(choices, text="Voice").pack(side="left")
        self._voice = tk.StringVar(value=DEFAULT_VOICE_LABEL)
        self._voice_box = ttk.Combobox(
            choices,
            textvariable=self._voice,
            values=self._controller.voices_for(AUTO),
            width=24,
            state="disabled",
        )
        self._voice_box.pack(side="left", padx=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._read_button = ttk.Button(buttons, text="Read", command=self._on_read)
        self._read_button.pack(side="left")
        self._paste_button = ttk.Button(buttons, text="Paste", command=self._on_paste)
        self._paste_button.pack(side="left", padx=6)
        self._pause_button = ttk.Button(buttons, text="Pause", command=self._on_pause)
        self._pause_button.pack(side="left")
        self._stop_button = ttk.Button(buttons, text="Stop", command=self._on_stop)
        self._stop_button.pack(side="left", padx=6)

        volume_row = ttk.Frame(frame)
        volume_row.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(volume_row, text="Volume").pack(side="left")
        self._volume = tk.Scale(
            volume_row,
            from_=0,
            to=100,
            orient="horizontal",
            command=self._on_volume,
            length=260,
            showvalue=True,
        )
        self._volume.set(100)
        self._volume.pack(side="left", padx=8)

        self._status = ttk.Label(frame, text="Idle.", anchor="w")
        self._status.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self._error = ttk.Label(frame, text="", foreground=ERROR_COLOUR, anchor="w")
        self._error.grid(row=6, column=0, sticky="ew")

    # --- user actions: forward to the controller and return --------------------------

    def _on_read(self) -> None:
        text = self._text.get("1.0", "end").strip()
        message = self._controller.read(text, self._language.get(), self._voice.get())
        self._error.config(text=message)  # "" clears a previous error

    def _on_paste(self) -> None:
        self._controller.start_paste()

    def _on_pause(self) -> None:
        self._controller.toggle_pause()

    def _on_stop(self) -> None:
        self._controller.stop()

    def _on_volume(self, value: str) -> None:
        self._controller.set_volume_percent(float(value))

    def _on_language_changed(self, _event: object) -> None:
        language = self._language.get()
        self._voice_box.config(values=self._controller.voices_for(language))
        self._voice.set(DEFAULT_VOICE_LABEL)
        self._voice_box.config(state="readonly" if language != AUTO else "disabled")

    # --- timers -------------------------------------------------------------------

    def _tick(self) -> None:
        """Apply service events and paste results, then redraw. Runs every POLL_MS."""
        if self._closing:
            return
        if self._stop_requested is not None and self._stop_requested.is_set():
            self.request_close()
            return
        self._controller.drain()
        pasted = self._controller.poll_paste()
        if pasted:
            self._text.delete("1.0", "end")
            self._text.insert("1.0", pasted)
        state = self._controller.display_state()
        self._status.config(text=state.status)
        self._error.config(text=state.error)
        self._pause_button.config(
            text=state.pause_label, state="normal" if state.can_pause else "disabled"
        )
        self._stop_button.config(state="normal" if state.can_stop else "disabled")
        self._paste_button.config(state="disabled" if state.busy_pasting else "normal")
        self.root.after(POLL_MS, self._tick)

    def _schedule_probe(self) -> None:
        if self._probe is None or self._closing:
            return
        probe = self._probe
        scheduled_at = self._clock() + DEFAULT_INTERVAL_MS / 1000.0

        def fire() -> None:
            probe.record(scheduled_at)
            self._schedule_probe()

        self.root.after(DEFAULT_INTERVAL_MS, fire)

    # --- closing -----------------------------------------------------------------

    def request_close(self) -> None:
        """Start the shutdown without blocking. The window closes once it has finished."""
        if self._closing:
            return
        self._closing = True
        self._status.config(text="Shutting down…")
        done = self._on_close()
        self._wait_for_shutdown(done, waited_ms=0)

    def _wait_for_shutdown(self, done: threading.Event, waited_ms: int) -> None:
        if done.is_set() or waited_ms >= SHUTDOWN_GIVE_UP_MS:
            self.root.destroy()
            return
        self.root.after(
            SHUTDOWN_CHECK_MS,
            lambda: self._wait_for_shutdown(done, waited_ms + SHUTDOWN_CHECK_MS),
        )

    def run(self) -> None:
        """Run the Tk main loop until the window is destroyed."""
        self.root.mainloop()
