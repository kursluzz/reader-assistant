"""Protocol version 2 over a real socket, and version 1 compatibility next to it."""

from __future__ import annotations

import json
import shutil
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fakes import short_dir, wait_for
from reader_assistant.ipc.client import (
    ServiceError,
    request,
    send_speak,
    subscribe,
)
from reader_assistant.ipc.protocol import (
    CODE_BAD_REQUEST,
    CODE_EMPTY_TEXT,
    CODE_INVALID_PAYLOAD,
    CODE_UNKNOWN_OPERATION,
    CODE_UNSUPPORTED,
    CODE_UNSUPPORTED_VERSION,
    OP_STATUS,
    Response,
    SpeakRequest,
    encode_request,
)
from reader_assistant.ipc.server import SpeechServer
from service_harness import live_service

EN = "Good morning, this is English."


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _raw(path: Path, line: bytes) -> dict[str, object]:
    """Send one raw line and return the parsed reply, for checking the exact wire format."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(line)
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    parsed: dict[str, object] = json.loads(reply.decode("utf-8"))
    return parsed


# --- version 1 is unchanged -----------------------------------------------------


def test_version_1_speak_request_still_works_unchanged(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        response = send_speak(live.path, SpeakRequest(text=EN))

        assert response.accepted is True
        assert (response.language, response.voice) == ("en", "af_heart")


def test_version_1_wire_format_is_byte_for_byte_the_same() -> None:
    line = encode_request(SpeakRequest(text="Hi"))

    assert json.loads(line) == {"version": 1, "command": "speak", "text": "Hi"}


def test_version_1_and_version_2_clients_coexist(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        send_speak(live.path, SpeakRequest(text=EN))
        status = request(live.path, OP_STATUS)
        reply = _raw(live.path, encode_request(SpeakRequest(text="Привет, мир.")))

        assert status["state"] in {"idle", "playing", "synthesizing", "loading"}
        assert reply == {
            "version": 1,
            "status": "accepted",
            "language": "ru",
            "voice": "ru_RU-denis-medium",
        }


# --- version rejection and malformed input -------------------------------------


def test_unknown_version_is_rejected_clearly_in_the_version_1_format(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        reply = _raw(live.path, b'{"version": 3, "op": "status"}\n')

    assert reply["status"] == "rejected"
    assert reply["code"] == CODE_UNSUPPORTED_VERSION
    assert "1, 2" in str(reply["message"])


def test_invalid_json_gets_a_structured_rejection_not_a_disconnect(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        reply = _raw(live.path, b"{not json\n")

    assert reply["status"] == "rejected"
    assert reply["code"] == CODE_BAD_REQUEST


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b'{"version": 2, "op": "fly"}\n', CODE_UNKNOWN_OPERATION),
        (b'{"version": 2, "op": "status", "extra": 1}\n', CODE_BAD_REQUEST),
        (b'{"version": 2, "op": 7}\n', CODE_BAD_REQUEST),
        (b'{"version": 2, "op": "status", "id": 5}\n', CODE_BAD_REQUEST),
        (b'{"version": 2, "op": "status", "id": "' + b"x" * 65 + b'"}\n', CODE_BAD_REQUEST),
        (b'{"version": 2, "op": "speak", "payload": {"text": 5}}\n', CODE_INVALID_PAYLOAD),
        (
            b'{"version": 2, "op": "speak", "payload": {"text": "hi", "language": "he"}}\n',
            CODE_INVALID_PAYLOAD,
        ),
        (
            b'{"version": 2, "op": "speak", "payload": {"text": "hi", "voice": "x"}}\n',
            CODE_INVALID_PAYLOAD,
        ),
        (b'{"version": 2, "op": "status", "payload": []}\n', CODE_INVALID_PAYLOAD),
        (b'{"version": 2, "op": "set_volume", "payload": {"volume": 3}}\n', CODE_INVALID_PAYLOAD),
        (
            b'{"version": 2, "op": "set_volume", "payload": {"volume": true}}\n',
            CODE_INVALID_PAYLOAD,
        ),
        (
            b'{"version": 2, "op": "set_volume", "payload": {"volume": "0.5"}}\n',
            CODE_INVALID_PAYLOAD,
        ),
        (b'{"version": 2, "op": "set_volume", "payload": {}}\n', CODE_INVALID_PAYLOAD),
    ],
)
def test_malformed_version_2_requests_return_structured_errors(
    workdir: Path, line: bytes, code: str
) -> None:
    with live_service(workdir / "s.sock") as live:
        reply = _raw(live.path, line)

    assert reply["ok"] is False
    assert reply["version"] == 2
    assert reply["error"]["code"] == code  # type: ignore[index]


def test_language_and_voice_overrides_are_refused_with_the_reason(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live, pytest.raises(ServiceError) as excinfo:
        request(live.path, "speak", {"text": EN, "language": "he"})

    assert excinfo.value.code == CODE_INVALID_PAYLOAD
    assert "detects them" in excinfo.value.message


def test_speak_with_empty_text_returns_the_service_error_code(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live, pytest.raises(ServiceError) as excinfo:
        request(live.path, "speak", {"text": "   "})

    assert excinfo.value.code == CODE_EMPTY_TEXT


def test_request_id_is_echoed_in_the_reply(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        reply = _raw(live.path, b'{"version": 2, "op": "status", "id": "abc-1"}\n')

    assert reply["id"] == "abc-1"
    assert reply["ok"] is True


def test_control_operations_are_refused_when_no_control_is_configured(workdir: Path) -> None:
    path = workdir / "bare.sock"
    server = SpeechServer(path, lambda request: Response(accepted=True, language="en", voice="v"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reply = _raw(path, b'{"version": 2, "op": "status"}\n')
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close()

    assert reply["error"]["code"] == CODE_UNSUPPORTED  # type: ignore[index]


# --- control operations --------------------------------------------------------


def test_status_snapshot_has_the_service_fields(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        status = request(live.path, OP_STATUS)

    assert status["state"] == "idle"
    assert status["volume"] == 1.0
    engines = status["engines"]
    assert isinstance(engines, list)
    assert {item["engine_id"] for item in engines} == {"kokoro", "piper"}
    assert isinstance(status["event_seq"], int)


def test_pause_resume_and_stop_are_idempotent_when_idle(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        assert request(live.path, "pause") == {"changed": False, "state": "idle"}
        assert request(live.path, "resume") == {"changed": False, "state": "idle"}
        assert request(live.path, "stop") == {"changed": False, "state": "idle"}


def test_set_volume_applies_and_returns_the_resulting_volume(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        result = request(live.path, "set_volume", {"volume": 0.25})

        assert result == {"volume": 0.25}
        assert live.service.status().volume == 0.25


def test_speak_over_v2_uses_automatic_routing(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live:
        he = request(live.path, "speak", {"text": "שלום עולם"})
        ru = request(live.path, "speak", {"text": "Привет, мир."})

    assert (he["language"], he["voice"]) == ("he", "he_IL-saspeech-medium")
    assert (ru["language"], ru["voice"]) == ("ru", "ru_RU-denis-medium")


# --- event subscription over the socket -----------------------------------------


def test_subscription_starts_with_status_and_then_streams_ordered_events(workdir: Path) -> None:
    with live_service(workdir / "s.sock") as live, subscribe(live.path) as stream:
        start_seq = stream.status["event_seq"]
        request(live.path, "speak", {"text": EN})
        seen: list[dict[str, Any]] = []
        while not any(item.get("kind") == "finished" for item in seen):
            message = stream.read(2.0)
            assert message is not None, "events stopped arriving"
            if message["type"] == "event":
                seen.append(message["event"])

    seqs = [int(str(item["seq"])) for item in seen]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert seqs[0] > start_seq
    assert [item["kind"] for item in seen][:2] == ["accepted", "loading"]


def test_closing_the_service_ends_the_subscription_with_a_reason(workdir: Path) -> None:
    ctx = live_service(workdir / "s.sock")
    live = ctx.__enter__()
    stream = subscribe(live.path)
    ctx.__exit__(None, None, None)
    try:
        message = None
        for _ in range(20):
            message = stream.read(0.2)
            if message is not None and message["type"] == "closed":
                break
    finally:
        stream.close()

    assert message == {"version": 2, "type": "closed", "reason": "shutdown"}


def test_a_subscriber_that_never_reads_cannot_block_the_service(workdir: Path) -> None:
    """Hundreds of events while a subscriber sleeps: the service keeps speaking, and the
    subscriber is ended with reason ``overflow`` instead of growing memory."""
    with live_service(workdir / "s.sock") as live:
        stalled = subscribe(live.path)  # opened, and then never read
        try:
            for _ in range(60):
                request(live.path, "speak", {"text": "Short sentence."})
            wait_for(lambda: live.service.status().state.value == "idle", timeout=10)
            # The service still answers and still speaks.
            assert request(live.path, "speak", {"text": EN})["language"] == "en"
            reasons = []
            for _ in range(400):
                message = stalled.read(0.05)
                if message is not None and message["type"] == "closed":
                    reasons.append(message["reason"])
                    break
        finally:
            stalled.close()

    assert reasons == ["overflow"]
