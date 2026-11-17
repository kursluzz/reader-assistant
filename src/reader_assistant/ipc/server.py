"""Service side of the IPC: a Unix socket server that accepts speech requests.

Lifecycle:

1. :class:`SpeechServer` checks for an existing socket. A live socket means another
   instance is running, so it refuses to start. A dead socket is removed.
2. It binds the socket and runs in the foreground with :meth:`serve_forever`.
3. :meth:`shutdown` may be called from another thread (for example a signal handler)
   and makes ``serve_forever`` return.
4. :meth:`close` removes the socket file. Use it as a context manager to be sure.

Each connection carries one request and receives one response. A malformed request
gets a rejected response; it never stops the server.
"""

from __future__ import annotations

import logging
import os
import socket
import socketserver
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Final, cast

from reader_assistant.ipc.paths import SocketPathError
from reader_assistant.ipc.protocol import (
    CODE_INTERNAL,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Response,
    SpeakRequest,
    decode_request,
    encode_response,
)

_log = logging.getLogger("reader_assistant.ipc")
CLIENT_TIMEOUT_SECONDS: Final = 10.0
_PROBE_TIMEOUT_SECONDS: Final = 0.5

# The handler turns a request into a response. It raises ProtocolError to reject.
Handler = Callable[[SpeakRequest], Response]


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
            response = _respond(line, server.handler)
            self.wfile.write(encode_response(response))
        except (TimeoutError, OSError):
            # The client went away or was too slow. Nothing to answer.
            _log.debug("client connection ended early")


def _respond(line: bytes, handler: Handler) -> Response:
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

    def __init__(self, address: str, handler: Handler) -> None:
        self.handler = handler
        super().__init__(address, _RequestHandler)


class SpeechServer:
    """Owns the service socket for the lifetime of the service."""

    def __init__(self, path: Path, handler: Handler) -> None:
        _remove_stale_socket(path)
        self._path = path
        self._server = _UnixServer(str(path), handler)
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
