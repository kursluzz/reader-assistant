"""A real service on a real Unix socket, with fake engines and a fake sound output.

Used by the IPC, GUI and integration tests. It runs the same ReaderService, ServiceControl and
SpeechServer as ``reader-assistant serve``. Only the engines and the sound device are fakes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fakes import FakeEngine, FakeSink, registry_with
from reader_assistant.app.control import ServiceControl
from reader_assistant.app.reader_service import ReaderService
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE
from reader_assistant.ipc.protocol import Response, SpeakRequest
from reader_assistant.ipc.server import SpeechServer


@dataclass
class LiveService:
    path: Path
    service: ReaderService
    sink: FakeSink
    server: SpeechServer


def default_engines(samples: int = 2400) -> list[FakeEngine]:
    return [
        FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=samples),
        FakeEngine("piper", "he", HEBREW_VOICE, samples=samples),
        FakeEngine("piper", "ru", RUSSIAN_VOICE, samples=samples),
    ]


@contextmanager
def live_service(
    path: Path,
    *,
    engines: list[FakeEngine] | None = None,
    sink: FakeSink | None = None,
) -> Iterator[LiveService]:
    """Start a service on ``path`` in a background thread, and stop it on exit."""
    sink = sink or FakeSink(write_delay=0.0)
    service = ReaderService(
        registry_with(*(engines or default_engines())), AudioPlayer(lambda: sink)
    )
    service.start()

    def handle(request: SpeakRequest) -> Response:
        from reader_assistant.app.reader_service import SpeechError
        from reader_assistant.ipc.protocol import ProtocolError

        try:
            accepted = service.submit(request.text)
        except SpeechError as exc:
            raise ProtocolError(exc.code, exc.message) from exc
        return Response(accepted=True, language=accepted.language, voice=accepted.voice_id)

    server = SpeechServer(path, handle, control=ServiceControl(service))
    thread = threading.Thread(target=server.serve_forever, name="test-ipc", daemon=True)
    thread.start()
    try:
        yield LiveService(path=path, service=service, sink=sink, server=server)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close()
        service.close()
