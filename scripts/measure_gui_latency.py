"""Measure GUI event-loop latency with the real window, service and Kokoro voice (M5-4).

The window's timer fires every 10 ms. How late it fires is the event-loop latency: a long
operation on the Tk thread shows up as large lateness. Each phase is named, and its
samples are reported separately.

Phases, in order:

- ``model_load``: the first English request, which loads Kokoro. Silent output.
- ``synthesis_5000_bytes``: a text of exactly 5,000 UTF-8 bytes. Silent output, so about
  five minutes of speech are not played; synthesis still runs in full.
- ``device_init``: a short English request on the real sound output. Audible.
- ``interruption``: a longer request on the real sound output, and a second request sent
  while the first is still in progress. Audible.

Each phase ends when its own request reaches a terminal event (finished, error or
cancelled), matched by request ID. Run it on the desktop, with the models installed:

    uv run --extra kokoro --extra audio python scripts/measure_gui_latency.py

The report is printed. Nothing is written to the repository.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Final

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from reader_assistant.app.reader_service import Event, EventKind, ReaderService
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.audio.sink import AudioSink, default_sink
from reader_assistant.config import load_config, resolve_paths
from reader_assistant.engines.catalog import default_registry
from reader_assistant.ui.controller import GuiController
from reader_assistant.ui.latency import LatencyProbe
from reader_assistant.ui.window import MainWindow

TARGET_BYTES: Final = 5_000
SENTENCE: Final = "The quick brown fox jumps over the lazy dog, and then it sleeps. "
POLL_MS: Final = 200
PHASE_TIMEOUT_S: Final = 900.0
# STOPPED counts as terminal too: a request that is interrupted never reports FINISHED.
TERMINAL: Final = frozenset(
    {EventKind.FINISHED, EventKind.ERROR, EventKind.CANCELLED, EventKind.STOPPED}
)


class SilentSink:
    """Takes no time and makes no sound."""

    def open(self, sample_rate: int) -> None:
        return None

    def write(self, pcm16: bytes) -> None:
        return None

    def close(self, *, drain: bool) -> None:
        return None


class PhaseSink:
    """Silent or real output, chosen by the current phase."""

    def __init__(self) -> None:
        self.silent = True

    def __call__(self) -> AudioSink:
        return SilentSink() if self.silent else default_sink()


def text_of_bytes(size: int) -> str:
    """A string of exactly ``size`` UTF-8 bytes, made of ASCII sentences."""
    text = (SENTENCE * (size // len(SENTENCE) + 1))[:size]
    assert len(text.encode("utf-8")) == size
    return text


class EventRecorder:
    """Keeps a copy of every event the controller drains, so phases can match request IDs."""

    def __init__(self, service: ReaderService) -> None:
        self._service = service
        self.events: list[Event] = []
        self._drain = service.drain_events
        service.drain_events = self._record  # type: ignore[method-assign]

    def _record(self) -> list[Event]:
        drained = self._drain()
        self.events.extend(drained)
        return drained

    def last_accepted_after(self, index: int) -> int | None:
        """The newest request accepted since ``index``: the one the phase is waiting for."""
        accepted = [e.request_id for e in self.events[index:] if e.kind is EventKind.ACCEPTED]
        return accepted[-1] if accepted else None

    def terminal_for(self, request_id: int) -> bool:
        return any(e.request_id == request_id and e.kind in TERMINAL for e in self.events)


def main() -> int:
    env = dict(os.environ)
    config = load_config(paths=resolve_paths(env, Path.home()), env=env)
    sink = PhaseSink()
    service = ReaderService(
        default_registry(config.models_dir),
        AudioPlayer(sink, volume=config.volume),
        default_language="en",
    )
    service.start()
    recorder = EventRecorder(service)
    clock = time.perf_counter
    probe = LatencyProbe(clock)
    controller = GuiController(service)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        controller,
        on_close=lambda: _shutdown(service),
        probe=probe,
        clock=clock,
    )

    phases: list[tuple[str, bool, str, bool]] = [
        ("model_load", True, "Hello.", False),
        ("synthesis_5000_bytes", True, text_of_bytes(TARGET_BYTES), False),
        ("device_init", False, "A short sentence on the real output.", False),
        ("interruption", False, "A longer sentence that will be interrupted. " * 4, True),
    ]
    outcomes: dict[str, str] = {}

    def start_phase(index: int) -> None:
        if index >= len(phases):
            window.request_close()
            return
        name, silent, text, interrupt = phases[index]
        probe.mark(name)
        sink.silent = silent
        mark = len(recorder.events)
        started = clock()
        message = controller.read(text)
        if message:
            outcomes[name] = f"rejected: {message}"
            QTimer.singleShot(POLL_MS, lambda: start_phase(index + 1))
            return
        if interrupt:
            QTimer.singleShot(1000, lambda: _second_request(controller, probe))
        wait_for_phase(index, mark, started)

    def wait_for_phase(index: int, mark: int, started: float) -> None:
        name = phases[index][0]
        request_id = recorder.last_accepted_after(mark)
        finished = request_id is not None and recorder.terminal_for(request_id)
        if finished or clock() - started > PHASE_TIMEOUT_S:
            outcomes.setdefault(name, f"done in {clock() - started:.1f} s")
            QTimer.singleShot(POLL_MS, lambda: start_phase(index + 1))
            return
        QTimer.singleShot(POLL_MS, lambda: wait_for_phase(index, mark, started))

    QTimer.singleShot(500, lambda: start_phase(0))
    window.show()
    app.exec()
    _print_report(probe, outcomes)
    return 0


def _second_request(controller: GuiController, probe: LatencyProbe) -> None:
    probe.mark("interruption_replacement")
    controller.read("This second request interrupts the first one.")


def _shutdown(service: ReaderService) -> threading.Event:
    done = threading.Event()

    def run() -> None:
        service.close()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def _print_report(probe: LatencyProbe, outcomes: dict[str, str]) -> None:
    print("GUI event-loop latency (timer lateness, ms; probe interval 10 ms)")
    print(f"{'phase':<26}{'samples':>9}{'typical':>10}{'maximum':>10}  outcome")
    for summary in probe.summary():
        outcome = outcomes.get(summary.phase, "")
        print(
            f"{summary.phase:<26}{summary.samples:>9}"
            f"{summary.typical_ms:>10.1f}{summary.max_ms:>10.1f}  {outcome}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
