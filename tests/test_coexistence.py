"""One service, many clients: a GUI-like subscriber, shortcut-style v1 speech, and v2 control at
the same time. Also repeated connect/disconnect cycles and shutdown during playback.

Everything runs on a real socket with fake engines and a fake sound output.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import FakeEngine, FakeSink, short_dir, wait_for
from reader_assistant.ipc.client import request, send_speak, subscribe
from reader_assistant.ipc.protocol import SpeakRequest
from service_harness import default_engines, live_service


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _service_threads() -> int:
    names = {"reader-assistant-speech", "reader-assistant-playback", "test-ipc"}
    return sum(1 for t in threading.enumerate() if t.name in names and t.is_alive())


def test_gui_subscriber_sees_shortcut_and_control_requests_in_order(workdir: Path) -> None:
    path = workdir / "s.sock"
    with live_service(path) as live, subscribe(path) as gui:
        # The shortcut (version 1) and the window (version 2) use the service at once.
        send_speak(path, SpeakRequest(text="Good morning, this is English."))
        request(path, "set_volume", {"volume": 0.5})
        request(path, "speak", {"text": "Привет, мир."})
        seen: list[dict[str, object]] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = gui.read(0.2)
            if message is not None and message["type"] == "event":
                seen.append(message["event"])
            if live.service.status().state.value == "idle" and any(
                e.get("kind") == "finished" and e.get("request_id") == 2 for e in seen
            ):
                break

    seqs = [int(str(item["seq"])) for item in seen]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert {item["request_id"] for item in seen if item["kind"] == "accepted"} == {1, 2}


def test_many_clients_speaking_at_once_leave_the_service_consistent(workdir: Path) -> None:
    path = workdir / "s.sock"
    errors: list[BaseException] = []
    with live_service(path, engines=default_engines(samples=2400)) as live:

        def client(index: int) -> None:
            try:
                for turn in range(5):
                    if (index + turn) % 2:
                        send_speak(path, SpeakRequest(text=f"Client {index} turn {turn}."))
                    else:
                        request(path, "speak", {"text": f"Клиент {index}, ход {turn}."})
                    request(path, "status")
            except BaseException as exc:  # reported to the test thread
                errors.append(exc)

        threads = [threading.Thread(target=client, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        wait_for(lambda: live.service.status().state.value == "idle", timeout=20)
        status = live.service.status()

    assert errors == []
    assert status.state.value == "idle"
    assert status.last_error is None


def test_repeated_connect_and_disconnect_cycles_leave_no_threads_behind(workdir: Path) -> None:
    path = workdir / "s.sock"
    with live_service(path):
        request(path, "status")  # warm up
        before = _service_threads()
        for _ in range(60):
            with subscribe(path) as stream:
                stream.read(0.01)
            request(path, "status")
        wait_for(lambda: _service_threads() <= before + 1, timeout=5)
        after = _service_threads()

    assert after <= before + 1


def test_shutdown_during_playback_ends_subscribers_and_releases_the_sound_output(
    workdir: Path,
) -> None:
    path = workdir / "s.sock"
    sink = FakeSink(write_delay=0.01)
    engines = [FakeEngine("kokoro", "en", "af_heart", samples=240_000)]
    ctx = live_service(path, engines=engines, sink=sink)
    live = ctx.__enter__()
    stream = subscribe(path)
    try:
        request(
            path, "speak", {"text": "A long passage that is still playing when the service stops."}
        )
        wait_for(lambda: live.service.status().state.value == "playing", timeout=10)
        wait_for(lambda: len(sink.written) > 0, timeout=10)

        ctx.__exit__(None, None, None)

        ended = None
        for _ in range(40):
            message = stream.read(0.1)
            if message is not None and message["type"] == "closed":
                ended = message["reason"]
                break
    finally:
        stream.close()
    assert ended == "shutdown"
    assert sink.closed_with_drain == [False]
    assert not path.exists()
    assert _service_threads() == 0
