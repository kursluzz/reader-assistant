"""The application window (PySide6). It runs on the Qt main (GUI) thread and only draws what
the controller returns.

The window is a client of the background service. It never starts a speech engine or the audio
player itself. Service messages reach it through a timer, which Qt fires on this thread, so
worker threads never update widgets. Closing the window disconnects; the service keeps running.
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
ERROR_STYLE: Final = "color: #b00020;"


class MainWindow(QWidget):
    """The Reader Assistant window."""

    def __init__(
        self,
        controller: GuiController,
        *,
        stop_requested: threading.Event | None = None,
        probe: LatencyProbe | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._stop_requested = stop_requested
        self._probe = probe
        self._clock = clock
        self._closing = False

        self.setWindowTitle("Reader Assistant")
        self.resize(680, 440)
        self._build()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

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
        self._retry = QPushButton("Retry")
        self._read.clicked.connect(self._on_read)
        self._paste.clicked.connect(self._controller.start_paste)
        self._pause.clicked.connect(self._controller.toggle_pause)
        self._stop.clicked.connect(self._controller.stop)
        self._retry.clicked.connect(self._controller.retry)
        self._retry.setVisible(False)  # shown only when the service could not be reached

        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(100)
        self._volume.setMinimumWidth(180)
        self._volume.valueChanged.connect(self._on_volume)

        buttons = QHBoxLayout()
        for button in (self._read, self._paste, self._pause, self._stop, self._retry):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("Volume"))
        buttons.addWidget(self._volume)

        self._status = QLabel("Connecting to the speech service…")
        self._status.setWordWrap(True)
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
        """Apply service messages and paste results, then redraw. Every POLL_MS."""
        if self._closing:
            return
        if self._stop_requested is not None and self._stop_requested.is_set():
            self.close()
            return
        self._controller.pump()
        volume = self._controller.take_volume_sync()
        if volume is not None:
            self._volume.blockSignals(True)
            self._volume.setValue(round(volume))
            self._volume.blockSignals(False)
        pasted = self._controller.poll_paste()
        if pasted:
            self._text.setPlainText(pasted)
        state = self._controller.display_state()
        self._status.setText(state.status)
        self._error.setText(state.error)
        self._pause.setText(state.pause_label)
        self._pause.setEnabled(state.can_pause)
        self._stop.setEnabled(state.can_stop)
        self._retry.setVisible(state.can_retry)
        self._paste.setEnabled(not state.busy_pasting)

    def _probe_tick(self) -> None:
        if self._probe is None:
            return
        self._probe.record(self._probe_scheduled)
        self._probe_scheduled = self._clock() + DEFAULT_INTERVAL_MS / 1000.0

    # --- closing -------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt's own name
        """Disconnect from the service and close. The service keeps running."""
        if not self._closing:
            self._closing = True
            self._controller.shutdown()
        event.accept()
