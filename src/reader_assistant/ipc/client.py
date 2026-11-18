"""Client side of the IPC: the ``read-clipboard`` command (v1) and the GUI (v2)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Final

from reader_assistant.ipc.protocol import (
    CODE_BAD_REQUEST,
    CODE_TOO_LARGE,
    MAX_MESSAGE_BYTES,
    OP_SUBSCRIBE,
    ProtocolError,
    Response,
    SpeakRequest,
    decode_operation_reply,
    decode_response,
    decode_stream_line,
    encode_operation,
    encode_request,
)
from reader_assistant.ipc.server import CLIENT_TIMEOUT_SECONDS

_RECV_CHUNK: Final = 65536


class ServiceUnavailableError(Exception):
    """The service cannot be reached. ``not_running`` is True only if nothing listens."""

    def __init__(self, message: str, *, not_running: bool = False) -> None:
        super().__init__(message)
        self.not_running = not_running


class ServiceError(Exception):
    """The service answered with an error to a version 2 operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def send_speak(
    path: Path,
    request: SpeakRequest,
    *,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> Response:
    """Send one version 1 request and return the service's response.

    Raises:
        ServiceUnavailableError: if nothing listens on ``path``, the connection breaks, or
            the service does not answer within ``timeout``.
        ProtocolError: if the service answers with something invalid.
    """
    sock = _connect(path, timeout)
    try:
        sock.sendall(encode_request(request))
        with sock.makefile("rb") as reader:
            line = reader.readline(MAX_MESSAGE_BYTES + 1)
    except TimeoutError as exc:
        raise ServiceUnavailableError("the service did not answer in time") from exc
    except OSError as exc:
        raise ServiceUnavailableError(
            f"the connection to the service was interrupted: {exc}"
        ) from exc
    finally:
        sock.close()

    if not line:
        raise ServiceUnavailableError("the service closed the connection without answering")
    return decode_response(line)


def request(
    path: Path,
    op: str,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one version 2 operation and return its ``result``.

    Raises:
        ServiceUnavailableError: the service cannot be reached or does not answer in time.
        ServiceError: the service rejected the operation (with its code and message).
        ProtocolError: the reply is invalid.
    """
    sock = _connect(path, timeout)
    try:
        sock.sendall(encode_operation(op, request_id=request_id, payload=payload))
        line = _LineBuffer(sock).read_line(timeout)
    except OSError as exc:
        raise ServiceUnavailableError(f"the connection to the service failed: {exc}") from exc
    finally:
        sock.close()
    if line is None:
        raise ServiceUnavailableError("the service did not answer in time")
    reply = decode_operation_reply(line)
    if not reply.ok:
        raise ServiceError(reply.code, reply.message)
    return reply.result


class EventSubscription:
    """An open ``subscribe`` connection. Read events with :meth:`read`; close it when done."""

    def __init__(self, sock: socket.socket, status: dict[str, Any]) -> None:
        self._sock = sock
        self._lines = _LineBuffer(sock)
        self._status = status

    @property
    def status(self) -> dict[str, Any]:
        """The service status when the subscription started."""
        return self._status

    def read(self, timeout: float) -> dict[str, Any] | None:
        """The next subscription message, or None if none arrived within ``timeout``.

        A message is ``{"type": "event", "event": {...}}`` or ``{"type": "closed", "reason": ...}``.

        Raises:
            ServiceUnavailableError: the connection ended without a ``closed`` message.
        """
        line = self._lines.read_line(timeout)
        if line is None:
            return None
        return decode_stream_line(line)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> EventSubscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def subscribe(
    path: Path,
    *,
    request_id: str | None = None,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> EventSubscription:
    """Open a subscription and wait for the status it starts from."""
    sock = _connect(path, timeout)
    try:
        sock.sendall(encode_operation(OP_SUBSCRIBE, request_id=request_id))
        line = _LineBuffer(sock).read_line(timeout)
    except OSError as exc:
        sock.close()
        raise ServiceUnavailableError(f"the connection to the service failed: {exc}") from exc
    if line is None:
        sock.close()
        raise ServiceUnavailableError("the service did not answer the subscription in time")
    try:
        reply = decode_operation_reply(line)
    except ProtocolError:
        sock.close()
        raise
    if not reply.ok:
        sock.close()
        raise ServiceError(reply.code, reply.message)
    status = reply.result.get("status")
    if not isinstance(status, dict):
        sock.close()
        raise ProtocolError(CODE_BAD_REQUEST, "the subscription did not start with a status")
    return EventSubscription(sock, status)


def _connect(path: Path, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        sock.close()
        raise ServiceUnavailableError("the service is not running", not_running=True) from exc
    except OSError as exc:
        sock.close()
        raise ServiceUnavailableError(f"cannot connect to the service: {exc}") from exc
    return sock


class _LineBuffer:
    """Reads newline-terminated lines from a socket. A timeout keeps any partial line."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = bytearray()

    def read_line(self, timeout: float) -> bytes | None:
        """The next full line (with its newline), or None if the timeout passes first."""
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise ProtocolError(CODE_TOO_LARGE, "the message is too large")
            self._sock.settimeout(timeout)
            try:
                chunk = self._sock.recv(_RECV_CHUNK)
            except TimeoutError:
                return None
            if not chunk:
                raise ServiceUnavailableError("the service closed the connection")
            self._buffer.extend(chunk)
