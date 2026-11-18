"""Service side of the IPC: a Unix socket server for speech requests and GUI control.

Lifecycle:

1. :class:`SpeechServer` checks for an existing socket. A live socket means another
   instance is running, so it refuses to start. A dead socket is removed.
2. It binds the socket and runs in the foreground with :meth:`serve_forever`.
3. :meth:`shutdown` may be called from another thread (for example a signal handler)
   and makes ``serve_forever`` return.
4. :meth:`close` removes the socket file. Use it as a context manager to be sure.

Each connection is handled on its own thread. Version 1 connections carry one request and one
response. Version 2 connections carry one operation and one response, except ``subscribe``,
which keeps the connection open and streams events until either side ends it. A malformed
request gets a rejected response; it never stops the server. A slow or stuck client only
affects its own connection.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import socketserver
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, cast

from reader_assistant.ipc.paths import SocketPathError
from reader_assistant.ipc.protocol import (
    CODE_INTERNAL,
    CODE_UNSUPPORTED,
    CODE_UNSUPPORTED_VERSION,
    MAX_MESSAGE_BYTES,
    OP_SUBSCRIBE,
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_2,
    SUPPORTED_VERSIONS,
    ProtocolError,
    Response,
    SpeakRequest,
    decode_object,
    decode_operation,
    decode_request,
    encode_error,
    encode_rejection_v1,
    encode_response,
    encode_result,
    encode_stream_line,
)

_log = logging.getLogger("reader_assistant.ipc")
CLIENT_TIMEOUT_SECONDS: Final = 10.0
_PROBE_TIMEOUT_SECONDS: Final = 0.5
_STREAM_POLL_SECONDS: Final = 0.5

# The handler turns a version 1 request into a response. It raises ProtocolError to reject.
Handler = Callable[[SpeakRequest], Response]


class EventStream(Protocol):
    """One subscriber's view of the service events."""

    @property
    def status(self) -> dict[str, Any]: ...

    def next(self, timeout: float) -> dict[str, Any] | None:
        """The next event as a dict, or None on timeout or once the stream has ended."""
        ...

    @property
    def end_reason(self) -> str | None: ...

    def close(self) -> None: ...


class ControlHandler(Protocol):
    """Runs version 2 operations. Implemented by :mod:`reader_assistant.app.control`."""

    def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one operation. Raises ProtocolError to reject it."""
        ...

    def open_stream(self) -> EventStream: ...


class ServiceAlreadyRunningError(Exception):
    """Another service instance is listening on the socket."""


def _remove_stale_socket(path: Path) -> None:
    """Remove ``path`` if it is a socket nobody listens on. Refuse anything else."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise SocketPathError(f"{path} exists and is not a socket; remove it and try again")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_PROBE_TIMEOUT_SECONDS)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        _log.info("removing stale socket", extra={"path": str(path)})
        path.unlink(missing_ok=True)
        return
    except OSError as exc:
        raise ServiceAlreadyRunningError(f"could not confirm that {path} is free: {exc}") from exc
    else:
        raise ServiceAlreadyRunningError(f"another instance is already running ({path})")
    finally:
        probe.close()


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(CLIENT_TIMEOUT_SECONDS)
        server = cast(_UnixServer, self.server)
        try:
            line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            self._dispatch(line, server)
        except (TimeoutError, OSError):
            # The client went away or was too slow. Nothing to answer.
            _log.debug("client connection ended early")

    def _dispatch(self, line: bytes, server: _UnixServer) -> None:
        try:
            data = decode_object(line)
        except ProtocolError as exc:
            self._write(encode_rejection_v1(exc.code, exc.message))
            return
        version = data.get("version")
        if version == PROTOCOL_VERSION:
            self._write(encode_response(_respond_v1(line, server.handler)))
        elif version == PROTOCOL_VERSION_2:
            self._dispatch_v2(data, server.control)
        else:
            supported = ", ".join(str(item) for item in SUPPORTED_VERSIONS)
            self._write(
                encode_rejection_v1(
                    CODE_UNSUPPORTED_VERSION,
                    f"protocol version {version!r} is not supported; use one of: {supported}",
                )
            )

    def _dispatch_v2(self, data: dict[str, Any], control: ControlHandler | None) -> None:
        request_id = data.get("id") if isinstance(data.get("id"), str) else None
        try:
            request = decode_operation(data)
        except ProtocolError as exc:
            self._write(encode_error(request_id, exc.code, exc.message))
            return
        if control is None:
            self._write(
                encode_error(request_id, CODE_UNSUPPORTED, "this service has no control interface")
            )
            return
        if request.op == OP_SUBSCRIBE:
            self._stream_events(request.request_id, control)
            return
        try:
            result = control.call(request.op, request.payload)
        except ProtocolError as exc:
            self._write(encode_error(request.request_id, exc.code, exc.message))
            return
        except Exception:
            _log.exception("operation failed", extra={"op": request.op})
            self._write(
                encode_error(
                    request.request_id,
                    CODE_INTERNAL,
                    "The service hit an internal error. See the log for details.",
                )
            )
            return
        self._write(encode_result(request.request_id, result))

    def _stream_events(self, request_id: str | None, control: ControlHandler) -> None:
        """Send the status, then every event, until the service or the client ends it."""
        stream = control.open_stream()
        try:
            self._write(encode_result(request_id, {"status": stream.status}))
            while True:
                event = stream.next(_STREAM_POLL_SECONDS)
                if event is not None:
                    self._write(encode_stream_line({"type": "event", "event": event}))
                elif stream.end_reason is not None:
                    self._write(encode_stream_line({"type": "closed", "reason": stream.end_reason}))
                    return
                if _peer_closed(self.connection):
                    _log.debug("event subscriber disconnected")
                    return
        finally:
            stream.close()

    def _write(self, data: bytes) -> None:
        self.wfile.write(data)


def _peer_closed(connection: socket.socket) -> bool:
    """True if the client has closed its end. Data it sends is ignored."""
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return False
    try:
        return connection.recv(4096) == b""
    except OSError:
        return True


def _respond_v1(line: bytes, handler: Handler) -> Response:
    try:
        request = decode_request(line)
        return handler(request)
    except ProtocolError as exc:
        return Response(accepted=False, code=exc.code, message=exc.message)
    except Exception:
        _log.exception("request handling failed")
        return Response(
            accepted=False,
            code=CODE_INTERNAL,
            message="The service hit an internal error. See the log for details.",
        )


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    # The default of 5 is too small: a burst of clients is refused with EAGAIN.
    request_queue_size = 64

    def __init__(self, address: str, handler: Handler, control: ControlHandler | None) -> None:
        self.handler = handler
        self.control = control
        super().__init__(address, _RequestHandler)


class SpeechServer:
    """Owns the service socket for the lifetime of the service."""

    def __init__(
        self,
        path: Path,
        handler: Handler,
        control: ControlHandler | None = None,
    ) -> None:
        _remove_stale_socket(path)
        self._path = path
        self._server = _UnixServer(str(path), handler, control)
        try:
            os.chmod(path, 0o600)
        except OSError:
            self._server.server_close()
            path.unlink(missing_ok=True)
            raise

    @property
    def path(self) -> Path:
        return self._path

    def serve_forever(self) -> None:
        """Serve requests until :meth:`shutdown` is called. Blocks the calling thread."""
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        """Ask :meth:`serve_forever` to return. Safe to call from any other thread."""
        self._server.shutdown()

    def close(self) -> None:
        """Release the socket and remove its file. Idempotent."""
        self._server.server_close()
        self._path.unlink(missing_ok=True)

    def __enter__(self) -> SpeechServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
