"""Tkinter visual spike for M5-3. Not part of the package; nothing here is shipped.

It runs the real ReaderService with a fake engine and a silent sink, so no model is loaded
and no sound is made. The point is to judge the window: layout, feel and responsiveness.

Run it on the desktop:

    uv run python scripts/gui_spike.py

``--self-test`` opens the window, drives it briefly and closes it, for automated checks.
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from array import array
from tkinter import ttk
from typing import Final

from reader_assistant.app.reader_service import ReaderService, ServiceState, SpeechError
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.audio.sink import AudioDeviceError
from reader_assistant.engines.base import AudioBuffer, EngineError, Voice
from reader_assistant.engines.registry import EngineRegistry

RATE: Final = 24000
POLL_MS: Final = 100
LANGUAGES: Final = ("auto", "en", "he", "ru")


class FakeEngine:
    """Engine that returns a short silent buffer after a realistic delay."""

    def __init__(self, engine_id: str, language: str, voice_id: str) -> None:
        self._engine_id = engine_id
        self._language = language
        self._voice_id = voice_id
        self._loaded = False

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def languages(self) -> frozenset[str]:
        return frozenset({self._language})

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def voices(self) -> tuple[Voice, ...]:
        return (Voice(self._voice_id, self._language, "fake"),)

    def load(self) -> None:
        time.sleep(0.4)  # pretend a model takes a moment to load
        self._loaded = True

    def synthesize(self, text: str, *, voice_id: str, speed: float = 1.0) -> AudioBuffer:
        if not self._loaded:
            raise EngineError("not loaded")
        seconds = max(1.0, len(text) / 15)  # roughly one second per 15 characters
        return AudioBuffer(pcm16=array("h", [0] * int(seconds * RATE)).tobytes(), sample_rate=RATE)


class SilentSink:
    """Output that takes real time per block but makes no sound."""

    def open(self, sample_rate: int) -> None:
        return None

    def write(self, pcm16: bytes) -> None:
        time.sleep(len(pcm16) / 2 / RATE)

    def close(self, *, drain: bool) -> None:
        return None


def build_service() -> ReaderService:
    registry = EngineRegistry()
    registry.register(FakeEngine("kokoro", "en", "af_heart"))
    registry.register(FakeEngine("piper", "he", "he_IL-saspeech-medium"))
    registry.register(FakeEngine("piper", "ru", "ru_RU-denis-medium"))
    service = ReaderService(registry, AudioPlayer(SilentSink, block_bytes=8192))
    service.start()
    return service


class SpikeWindow:
    def __init__(self, root: tk.Tk, service: ReaderService) -> None:
        self.root = root
        self.service = service
        root.title("Reader Assistant — GUI spike")
        root.geometry("640x420")
        root.minsize(480, 340)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Text to read").grid(row=0, column=0, sticky="w")
        self.text = tk.Text(frame, height=8, wrap="word")
        self.text.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        self.text.insert("1.0", "Paste or type text here, then press Read.")

        controls = ttk.Frame(frame)
        controls.grid(row=2, column=0, sticky="ew")
        self.language = tk.StringVar(value="auto")
        ttk.Label(controls, text="Language").pack(side="left")
        ttk.Combobox(
            controls, textvariable=self.language, values=LANGUAGES, width=7, state="readonly"
        ).pack(side="left", padx=(4, 12))
        ttk.Button(controls, text="Read", command=self.read).pack(side="left")
        self.pause_button = ttk.Button(controls, text="Pause", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=6)
        ttk.Button(controls, text="Stop", command=self.stop).pack(side="left")

        volume_row = ttk.Frame(frame)
        volume_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(volume_row, text="Volume").pack(side="left")
        self.volume = tk.Scale(
            volume_row,
            from_=0,
            to=100,
            orient="horizontal",
            command=self.set_volume,
            length=260,
            showvalue=True,
        )
        self.volume.set(int(service.status().volume * 100))
        self.volume.pack(side="left", padx=8)

        self.status_label = ttk.Label(frame, text="Ready.", anchor="w")
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.error_label = ttk.Label(frame, text="", foreground="#b00020", anchor="w")
        self.error_label.grid(row=5, column=0, sticky="ew")

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(POLL_MS, self.poll)

    # --- actions: each returns at once, the service does the work ------------

    def read(self) -> None:
        text = self.text.get("1.0", "end").strip()
        language = None if self.language.get() == "auto" else self.language.get()
        try:
            accepted = self.service.submit(text, language=language)
        except SpeechError as exc:
            self.error_label.config(text=exc.message)
            return
        self.error_label.config(text="")
        self.status_label.config(
            text=f"Accepted request {accepted.request_id} ({accepted.language})."
        )

    def toggle_pause(self) -> None:
        if self.service.status().state is ServiceState.PAUSED:
            self.service.resume()
        else:
            self.service.pause()

    def stop(self) -> None:
        self.service.stop()

    def set_volume(self, value: str) -> None:
        self.service.set_volume(int(float(value)) / 100)

    def close(self) -> None:
        self.service.close()
        self.root.destroy()

    # --- display: poll the service on the Tk thread, never block it -----------

    def poll(self) -> None:
        status = self.service.status()
        for event in self.service.drain_events():
            if event.kind.value == "error":
                self.error_label.config(text=event.message)
        labels = {
            ServiceState.IDLE: "Idle.",
            ServiceState.LOADING: "Loading voice…",
            ServiceState.SYNTHESIZING: "Preparing speech…",
            ServiceState.PLAYING: "Playing.",
            ServiceState.PAUSED: "Paused.",
        }
        detail = f" [{status.language}, {status.voice_id}]" if status.request_id else ""
        self.status_label.config(text=labels[status.state] + detail)
        self.pause_button.config(text="Resume" if status.state is ServiceState.PAUSED else "Pause")
        self.root.after(POLL_MS, self.poll)


def main() -> int:
    service = build_service()
    root = tk.Tk()
    window = SpikeWindow(root, service)
    if "--self-test" in sys.argv:
        window.text.delete("1.0", "end")
        window.text.insert("1.0", "Self test: a short sentence in English.")
        root.after(300, window.read)
        root.after(2500, window.toggle_pause)
        root.after(3000, window.toggle_pause)
        root.after(3500, window.stop)
        root.after(4000, lambda: print("self-test: window ran, closing"))
        root.after(4200, window.close)
    try:
        root.mainloop()
    except (AudioDeviceError, KeyboardInterrupt):
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
