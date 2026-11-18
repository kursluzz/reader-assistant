"""Record the Reader Assistant window as an animated GIF for the README.

The script runs the real window against the real service and the real voices (Kokoro for
English, Piper for Russian and Hebrew), in-process, with the window rendered offscreen. It
drives the window the way a person would: it types text, presses Read, Pause, Resume and Stop,
and captures a frame whenever the window changes. The sound output is replaced by a silent
device that takes as long as the audio would, so the timing matches real speech.

Nothing is played through the speakers. The GIF is written to docs/images/.

Run it from the repository (needs the voices installed, see the README; Pillow is only needed
here and is not a project dependency):

    uv run --extra gui --extra kokoro --extra piper --with pillow python scripts/make_demo_gif.py
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Final

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

from reader_assistant.app.control import ServiceControl
from reader_assistant.app.reader_service import ReaderService, ServiceState, SpeechError
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.config import load_config, resolve_paths
from reader_assistant.engines.catalog import default_registry
from reader_assistant.ipc.paths import socket_path
from reader_assistant.ipc.protocol import ProtocolError, Response, SpeakRequest
from reader_assistant.ipc.server import SpeechServer
from reader_assistant.ui.controller import GuiController
from reader_assistant.ui.service_link import ServiceLink
from reader_assistant.ui.window import MainWindow

OUTPUT: Final = (
    Path(__file__).resolve().parent.parent / "docs" / "images" / "reader-assistant-demo.gif"
)
TICK_MS: Final = 100
TEXT_EN: Final = "Good morning. This is Reader Assistant reading English text aloud."
TEXT_HE: Final = "שלום. זוהי קריאה של טקסט בעברית."
TEXT_RU: Final = "Привет. Это чтение русского текста вслух."


class TimedSilence:
    """A sound output that plays nothing, but takes as long as the audio would."""

    def __init__(self) -> None:
        self._rate = 24_000

    def open(self, sample_rate: int) -> None:
        self._rate = sample_rate

    def write(self, pcm16: bytes) -> None:
        time.sleep(len(pcm16) / (2 * self._rate))

    def close(self, *, drain: bool) -> None:
        return None


class FrameRecorder:
    """Keeps one frame per visible change. Identical frames extend the previous one's duration."""

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self.frames: list[tuple[bytes, int]] = []
        self._last_digest: bytes | None = None

    def capture(self) -> None:
        image = self._window.grab().toImage()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        digest = hashlib.sha1(data).digest()
        if digest == self._last_digest and self.frames:
            frame, duration = self.frames[-1]
            self.frames[-1] = (frame, duration + TICK_MS)
        else:
            self.frames.append((data, TICK_MS))
            self._last_digest = digest


def main() -> int:
    app = QApplication.instance() or QApplication([])
    runtime = Path(tempfile.mkdtemp(prefix="ra-demo-"))
    runtime.chmod(0o700)
    try:
        return _record(app, runtime)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def _record(app: QApplication, runtime: Path) -> int:
    environment = dict(os.environ)
    config = load_config(paths=resolve_paths(environment, Path.home()), env=environment)
    service = ReaderService(
        default_registry(config.models_dir),
        AudioPlayer(TimedSilence, volume=1.0),
        default_language="en",
    )
    service.start()
    socket = socket_path({"XDG_RUNTIME_DIR": str(runtime)}, Path.home())
    server = SpeechServer(socket, _speak(service), control=ServiceControl(service))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    link = ServiceLink(socket)
    link.start()
    window = MainWindow(GuiController(link, clipboard_reader=lambda: ""))
    window.resize(680, 440)
    window.show()
    recorder = FrameRecorder(window)

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            recorder.capture()
            time.sleep(TICK_MS / 1000)

    def wait_for(state: ServiceState, timeout: float = 90.0) -> None:
        end = time.monotonic() + timeout
        while service.status().state is not state and time.monotonic() < end:
            app.processEvents()
            recorder.capture()
            time.sleep(TICK_MS / 1000)

    def type_text(text: str) -> None:
        for count in range(1, len(text) + 1):
            window._text.setPlainText(text[:count])
            pump(0.04)

    try:
        pump(1.0)
        type_text(TEXT_EN)
        pump(0.5)
        window._read.click()
        wait_for(ServiceState.PLAYING)
        pump(1.5)
        window._pause.click()
        pump(1.5)
        window._pause.click()
        wait_for(ServiceState.IDLE)
        pump(0.8)

        type_text(TEXT_HE)
        pump(0.4)
        window._read.click()
        wait_for(ServiceState.PLAYING)
        pump(1.5)
        window._stop.click()
        wait_for(ServiceState.IDLE)
        pump(0.6)

        type_text(TEXT_RU)
        pump(0.4)
        window._read.click()
        wait_for(ServiceState.PLAYING)
        pump(1.2)
        wait_for(ServiceState.IDLE)
        pump(1.5)
    finally:
        window.close()
        link.close()
        server.shutdown()
        server.close()
        service.close()

    _write_gif(recorder.frames)
    print(f"wrote {OUTPUT} ({len(recorder.frames)} frames)")
    return 0


def _speak(service: ReaderService) -> object:
    def handle(request: SpeakRequest) -> Response:
        try:
            accepted = service.submit(request.text)
        except SpeechError as exc:
            raise ProtocolError(exc.code, exc.message) from exc
        return Response(accepted=True, language=accepted.language, voice=accepted.voice_id)

    return handle


def _write_gif(frames: list[tuple[bytes, int]]) -> None:
    images = [
        Image.open(io.BytesIO(data)).convert("RGB").quantize(colors=128) for data, _ in frames
    ]
    durations = [duration for _, duration in frames]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        OUTPUT,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )


if __name__ == "__main__":
    raise SystemExit(main())
