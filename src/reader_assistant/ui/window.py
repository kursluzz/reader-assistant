"""The application window (PySide6). It runs on the Qt main (GUI) thread and only draws what
the controller returns.

Everything that touches widgets happens on the GUI thread. Service events, paste results and
stop requests reach the window through timers, which Qt fires on that thread. Worker threads
never update widgets directly. Actions return at once: the service does the work. Closing the
window starts the shutdown on a helper thread and closes the window when it has finished.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from reader_assistant.ui.controller import GuiController
from reader_assistant.ui.latency import DEFAULT_INTERVAL_MS, LatencyProbe

POLL_MS: Final = 100
SHUTDOWN_CHECK_MS: Final = 50
SHUTDOWN_GIVE_UP_S: Final = 10.0
ERROR_STYLE: Final = "color: #b00020;"


class MainWindow(QWidget):
    """The Reader Assistant window."""

    def __init__(
        self,
        controller: GuiController,
        *,
        on_close: Callable[[], threading.Event],
        stop_requested: threading.Event | None = None,
        probe: LatencyProbe | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._on_close = on_close
        self._stop_requested = stop_requested
        self._probe = probe
        self._clock = clock
        self._closing = False
        self._may_close = False
        self._shutdown_done: threading.Event | None = None
        self._shutdown_started_at = 0.0

        self.setWindowTitle("Reader Assistant")
        self.resize(680, 440)
        self._build()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.timeout.connect(self._check_shutdown)

        if self._probe is not None:
            self._probe_timer = QTimer(self)
            self._probe_timer.timeout.connect(self._probe_tick)
            self._probe_timer.start(DEFAULT_INTERVAL_MS)
            self._probe_scheduled = self._clock() + DEFAULT_INTERVAL_MS / 1000.0

    # --- layout ---------------------------------------------------------------

    def _build(self) -> None:
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("Type, paste or select text. Ctrl+A selects all.")
        self._text.setPlainText("Paste or type text here, then press Read.")

        self._read = QPushButton("Read")
        self._paste = QPushButton("Paste")
        self._pause = QPushButton("Pause")
        self._stop = QPushButton("Stop")
        self._read.clicked.connect(self._on_read)
        self._paste.clicked.connect(self._controller.start_paste)
        self._pause.clicked.connect(self._controller.toggle_pause)
        self._stop.clicked.connect(self._controller.stop)

        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(100)
        self._volume.setMinimumWidth(180)
        self._volume.valueChanged.connect(self._on_volume)

        buttons = QHBoxLayout()
        for button in (self._read, self._paste, self._pause, self._stop):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Volume"))
        buttons.addWidget(self._volume)

        self._status = QLabel("Idle.")
        self._error = QLabel("")
        self._error.setStyleSheet(ERROR_STYLE)
        self._error.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Text to read"))
        layout.addWidget(self._text, stretch=1)
        layout.addLayout(buttons)
        layout.addWidget(self._status)
        layout.addWidget(self._error)

    # --- user actions: forward to the controller and return ----------------------

    def _on_read(self) -> None:
        message = self._controller.read(self._text.toPlainText().strip())
        self._error.setText(message)

    def _on_volume(self, value: int) -> None:
        self._controller.set_volume_percent(float(value))

    # --- timers (GUI thread) -------------------------------------------------------

    def _tick(self) -> None:
        """Apply service events and paste results, then redraw. Every POLL_MS."""
        if self._closing:
            return
        if self._stop_requested is not None and self._stop_requested.is_set():
            self.request_close()
            return
        self._controller.drain()
        pasted = self._controller.poll_paste()
        if pasted:
            self._text.setPlainText(pasted)
        state = self._controller.display_state()
        self._status.setText(state.status)
        self._error.setText(state.error)
        self._pause.setText(state.pause_label)
        self._pause.setEnabled(state.can_pause)
        self._stop.setEnabled(state.can_stop)
        self._paste.setEnabled(not state.busy_pasting)

    def _probe_tick(self) -> None:
        if self._probe is None:
            return
        self._probe.record(self._probe_scheduled)
        self._probe_scheduled = self._clock() + DEFAULT_INTERVAL_MS / 1000.0

    # --- closing -------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt's own name
        if self._may_close:
            event.accept()
            return
        event.ignore()
        self.request_close()

    def request_close(self) -> None:
        """Start the shutdown without blocking. The window closes once it has finished."""
        if self._closing:
            return
        self._closing = True
        self._status.setText("Shutting down…")
        self._shutdown_done = self._on_close()
        self._shutdown_started_at = self._clock()
        self._shutdown_timer.start(SHUTDOWN_CHECK_MS)

    def _check_shutdown(self) -> None:
        done = self._shutdown_done
        finished = done is not None and done.is_set()
        timed_out = self._clock() - self._shutdown_started_at >= SHUTDOWN_GIVE_UP_S
        if finished or timed_out:
            self._shutdown_timer.stop()
            self._may_close = True
            self.close()
