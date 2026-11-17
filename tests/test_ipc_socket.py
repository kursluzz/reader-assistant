from __future__ import annotations

import contextlib
import os
import shutil
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import short_dir
from reader_assistant.ipc.client import ServiceUnavailableError, send_speak
from reader_assistant.ipc.paths import SocketPathError, socket_path
from reader_assistant.ipc.protocol import (
    CODE_BAD_REQUEST,
    CODE_TOO_LARGE,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Response,
    SpeakRequest,
)
from reader_assistant.ipc.server import ServiceAlreadyRunningError, SpeechServer


@pytest.fixture
def workdir() -> Iterator[Path]:
    directory = short_dir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def received() -> list[str]:
    return []


@pytest.fixture
def running_server(workdir: Path, received: list[str]) -> Iterator[Path]:
    """A real SpeechServer on a Unix socket, serving in a background thread."""

    def handler(request: SpeakRequest) -> Response:
        received.append(request.text)
        if request.text == "reject me":
            raise ProtocolError("empty_text", "rejected by the test handler")
        return Response(accepted=True, language="en", voice="af_heart")

    path = workdir / "service.sock"
    server = SpeechServer(path, handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close()


def test_client_and_server_exchange_a_request(running_server: Path, received: list[str]) -> None:
    response = send_speak(running_server, SpeakRequest(text="Hello there"))

    assert response.accepted is True
    assert response.language == "en"
    assert response.voice == "af_heart"
    assert received == ["Hello there"]


def test_unicode_payloads_survive_the_round_trip(running_server: Path, received: list[str]) -> None:
    text = "שלום עולם. Привет, мир! Hello, world."

    send_speak(running_server, SpeakRequest(text=text))

    assert received == [text]


def test_handler_rejection_is_returned_to_the_client(running_server: Path) -> None:
    response = send_speak(running_server, SpeakRequest(text="reject me"))

    assert response.accepted is False
    assert response.code == "empty_text"
    assert response.message == "rejected by the test handler"


def test_multiple_sequential_requests_are_all_served(
    running_server: Path, received: list[str]
) -> None:
    for index in range(5):
        assert send_speak(running_server, SpeakRequest(text=f"request {index}")).accepted

    assert received == [f"request {index}" for index in range(5)]


def test_invalid_message_gets_a_rejection_and_the_server_keeps_running(
    running_server: Path, received: list[str]
) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
        raw.connect(str(running_server))
        raw.sendall(b"this is not json\n")
        reply = raw.makefile("rb").readline()

    assert f'"{CODE_BAD_REQUEST}"'.encode() in reply
    assert received == []
    assert send_speak(running_server, SpeakRequest(text="still alive")).accepted


def test_oversized_message_gets_a_too_large_rejection(running_server: Path) -> None:
    payload = b"x" * (MAX_MESSAGE_BYTES + 100) + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
        raw.connect(str(running_server))
        receiver = threading.Thread(target=lambda: _send_ignoring_reset(raw, payload), daemon=True)
        receiver.start()
        reply = raw.makefile("rb").readline()
        receiver.join(timeout=5)

    assert f'"{CODE_TOO_LARGE}"'.encode() in reply
    assert send_speak(running_server, SpeakRequest(text="after oversize")).accepted


def _send_ignoring_reset(sock: socket.socket, payload: bytes) -> None:
    # The server stops reading once it has enough to reject the message, so the
    # sender may see a reset. That is the expected outcome, not a failure.
    with contextlib.suppress(OSError):
        sock.sendall(payload)


def test_client_gets_service_unavailable_when_no_server_is_listening(workdir: Path) -> None:
    with pytest.raises(ServiceUnavailableError, match="not running"):
        send_speak(workdir / "missing.sock", SpeakRequest(text="hello"))


def test_connection_closed_without_an_answer_is_reported(workdir: Path) -> None:
    path = workdir / "silent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def accept_and_hang_up() -> None:
        conn, _ = listener.accept()
        conn.recv(1024)
        conn.close()

    thread = threading.Thread(target=accept_and_hang_up, daemon=True)
    thread.start()
    try:
        with pytest.raises(ServiceUnavailableError, match="without answering"):
            send_speak(path, SpeakRequest(text="hello"), timeout=3.0)
    finally:
        thread.join(timeout=5)
        listener.close()


def test_unresponsive_service_times_out(workdir: Path) -> None:
    path = workdir / "stuck.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)  # accepts into the backlog but never answers
    try:
        with pytest.raises(ServiceUnavailableError, match="did not answer in time"):
            send_speak(path, SpeakRequest(text="hello"), timeout=0.3)
    finally:
        listener.close()


def test_socket_file_has_owner_only_permissions(running_server: Path) -> None:
    mode = stat.S_IMODE(os.stat(running_server).st_mode)

    assert mode == 0o600


def test_closing_the_server_removes_the_socket(workdir: Path) -> None:
    path = workdir / "cleanup.sock"
    server = SpeechServer(path, lambda request: Response(accepted=True))
    assert path.exists()

    server.close()
    server.close()  # idempotent

    assert not path.exists()


def test_context_manager_removes_the_socket(workdir: Path) -> None:
    path = workdir / "context.sock"

    with SpeechServer(path, lambda request: Response(accepted=True)):
        assert path.exists()

    assert not path.exists()


def test_stale_socket_file_is_replaced(workdir: Path) -> None:
    path = workdir / "stale.sock"
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(str(path))
    orphan.close()  # file remains, nobody listens
    assert path.exists()

    server = SpeechServer(path, lambda request: Response(accepted=True))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert send_speak(path, SpeakRequest(text="hello")).accepted
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close()


def test_second_instance_refuses_to_start_over_a_live_service(
    running_server: Path,
) -> None:
    with pytest.raises(ServiceAlreadyRunningError, match="already running"):
        SpeechServer(running_server, lambda request: Response(accepted=True))

    # The first instance is untouched.
    assert send_speak(running_server, SpeakRequest(text="still mine")).accepted


def test_regular_file_at_the_socket_path_is_not_deleted(workdir: Path) -> None:
    path = workdir / "not-a-socket"
    path.write_text("important", encoding="utf-8")

    with pytest.raises(SocketPathError, match="not a socket"):
        SpeechServer(path, lambda request: Response(accepted=True))

    assert path.read_text(encoding="utf-8") == "important"


def test_shutdown_from_another_thread_returns_serve_forever(workdir: Path) -> None:
    server = SpeechServer(workdir / "shutdown.sock", lambda request: Response(accepted=True))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    server.shutdown()
    thread.join(timeout=5)
    server.close()

    assert not thread.is_alive()


# --- socket location --------------------------------------------------------


def test_socket_lives_under_xdg_runtime_dir_with_private_permissions(workdir: Path) -> None:
    path = socket_path({"XDG_RUNTIME_DIR": str(workdir)}, home=workdir)

    assert path == workdir / "reader-assistant" / "service.sock"
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_socket_falls_back_to_state_dir_without_runtime_dir(workdir: Path) -> None:
    path = socket_path({}, home=workdir)

    assert path == workdir / ".local" / "state" / "reader-assistant" / "service.sock"
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_relative_runtime_dir_is_ignored(workdir: Path) -> None:
    path = socket_path({"XDG_RUNTIME_DIR": "relative/dir"}, home=workdir)

    assert path.is_relative_to(workdir)


def test_overlong_socket_path_is_reported(workdir: Path) -> None:
    deep = workdir / ("x" * 100)

    with pytest.raises(SocketPathError, match="too long"):
        socket_path({"XDG_RUNTIME_DIR": str(deep)}, home=workdir)


def test_directory_owned_by_someone_else_is_refused(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    with pytest.raises(SocketPathError, match="not owned by the current user"):
        socket_path({"XDG_RUNTIME_DIR": str(workdir)}, home=workdir)
