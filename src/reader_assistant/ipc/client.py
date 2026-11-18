"""Client side of the IPC, used by the ``read-clipboard`` command."""

from __future__ import annotations

import socket
from pathlib import Path

from reader_assistant.ipc.protocol import (
    MAX_MESSAGE_BYTES,
    Response,
    SpeakRequest,
    decode_response,
    encode_request,
)
from reader_assistant.ipc.server import CLIENT_TIMEOUT_SECONDS


class ServiceUnavailableError(Exception):
    """The service cannot be reached. ``not_running`` is True only if nothing listens."""

    def __init__(self, message: str, *, not_running: bool = False) -> None:
        super().__init__(message)
        self.not_running = not_running


def send_speak(
    path: Path,
    request: SpeakRequest,
    *,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> Response:
    """Send one request and return the service's response.

    Raises:
        ServiceUnavailableError: if nothing listens on ``path``, the connection breaks, or
            the service does not answer within ``timeout``.
        ProtocolError: if the service answers with something invalid.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ServiceUnavailableError("the service is not running", not_running=True) from exc
        except OSError as exc:
            raise ServiceUnavailableError(f"cannot connect to the service: {exc}") from exc

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
