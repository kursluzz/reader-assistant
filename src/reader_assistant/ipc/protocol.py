"""The IPC message formats: newline-delimited JSON over a Unix socket.

Protocol version 1 (speak only, one request and one response per connection) is unchanged::

    {"version": 1, "command": "speak", "text": "..."}
    {"version": 1, "status": "accepted", "language": "en", "voice": "af_heart"}
    {"version": 1, "status": "rejected", "code": "empty_text", "message": "..."}

Protocol version 2 adds operations for the GUI. Each request is an operation envelope and each
response is an explicit ok/error envelope::

    {"version": 2, "op": "speak", "id": "r1", "payload": {"text": "..."}}
    {"version": 2, "id": "r1", "ok": true, "result": {"request_id": 4, "language": "en", ...}}
    {"version": 2, "id": "r1", "ok": false,
     "error": {"code": "invalid_payload", "message": "..."}}

``subscribe`` keeps the connection open. After the ``ok`` response with the current status, the
service sends one ``{"version": 2, "type": "event", "event": {...}}`` line per event, and finally
``{"version": 2, "type": "closed", "reason": "..."}`` before it closes the connection.

Requests that cannot be parsed far enough to know their version (invalid JSON, a missing or
unknown version) are answered in the version 1 rejection format, so every client can read why.

Unknown commands, operations, versions and fields are rejected, never guessed at.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Final

from reader_assistant.limits import MAX_TEXT_BYTES

PROTOCOL_VERSION: Final = 1
PROTOCOL_VERSION_2: Final = 2
SUPPORTED_VERSIONS: Final = (PROTOCOL_VERSION, PROTOCOL_VERSION_2)
COMMAND_SPEAK: Final = "speak"
# Upper bound for one message line. JSON escaping can grow text several times, so this
# is well above MAX_TEXT_BYTES while still bounding memory use.
MAX_MESSAGE_BYTES: Final = 512 * 1024
MAX_REQUEST_ID_CHARS: Final = 64

OP_SPEAK: Final = "speak"
OP_STATUS: Final = "status"
OP_PAUSE: Final = "pause"
OP_RESUME: Final = "resume"
OP_STOP: Final = "stop"
OP_SET_VOLUME: Final = "set_volume"
OP_SUBSCRIBE: Final = "subscribe"
OPERATIONS: Final = frozenset(
    {OP_SPEAK, OP_STATUS, OP_PAUSE, OP_RESUME, OP_STOP, OP_SET_VOLUME, OP_SUBSCRIBE}
)
_OPERATION_FIELDS: Final = frozenset({"version", "op", "id", "payload"})

CODE_BAD_REQUEST: Final = "bad_request"
CODE_TOO_LARGE: Final = "too_large"
CODE_EMPTY_TEXT: Final = "empty_text"
CODE_UNSUPPORTED: Final = "unsupported"
CODE_ENGINE_FAILED: Final = "engine_failed"
CODE_AUDIO_FAILED: Final = "audio_failed"
CODE_INTERNAL: Final = "internal"
CODE_UNSUPPORTED_VERSION: Final = "unsupported_version"
CODE_UNKNOWN_OPERATION: Final = "unknown_operation"
CODE_INVALID_PAYLOAD: Final = "invalid_payload"


class ProtocolError(Exception):
    """A message is malformed or violates the protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SpeakRequest:
    text: str


@dataclass(frozen=True, slots=True)
class Response:
    accepted: bool
    message: str = ""
    code: str = ""
    language: str = ""
    voice: str = ""


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """A validated version 2 request."""

    op: str
    request_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperationReply:
    """A version 2 reply: either ``result`` (ok) or ``code`` and ``message`` (error)."""

    request_id: str | None
    ok: bool
    result: dict[str, Any]
    code: str = ""
    message: str = ""


# --- version 1 -----------------------------------------------------------------


def encode_request(request: SpeakRequest) -> bytes:
    payload = {"version": PROTOCOL_VERSION, "command": COMMAND_SPEAK, "text": request.text}
    return _encode(payload)


def decode_request(line: bytes) -> SpeakRequest:
    """Parse and validate one version 1 request line. Raises ProtocolError on any problem."""
    data = decode_object(line)
    if data.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(CODE_BAD_REQUEST, "unsupported protocol version")
    if data.get("command") != COMMAND_SPEAK:
        raise ProtocolError(CODE_BAD_REQUEST, "unknown command")
    text = data.get("text")
    if not isinstance(text, str):
        raise ProtocolError(CODE_BAD_REQUEST, "text must be a string")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ProtocolError(CODE_TOO_LARGE, f"the text is larger than {MAX_TEXT_BYTES} bytes")
    if not text.strip():
        raise ProtocolError(CODE_EMPTY_TEXT, "the text is empty")
    return SpeakRequest(text=text)


def encode_response(response: Response) -> bytes:
    if response.accepted:
        payload: dict[str, Any] = {
            "version": PROTOCOL_VERSION,
            "status": "accepted",
            "language": response.language,
            "voice": response.voice,
        }
    else:
        payload = {
            "version": PROTOCOL_VERSION,
            "status": "rejected",
            "code": response.code,
            "message": response.message,
        }
    return _encode(payload)


def decode_response(line: bytes) -> Response:
    """Parse one version 1 response line. Raises ProtocolError if it is not a valid response."""
    data = decode_object(line)
    if data.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(CODE_BAD_REQUEST, "unsupported protocol version in response")
    status = data.get("status")
    if status == "accepted":
        language = data.get("language")
        voice = data.get("voice")
        if not isinstance(language, str) or not isinstance(voice, str):
            raise ProtocolError(CODE_BAD_REQUEST, "accepted response is missing fields")
        return Response(accepted=True, language=language, voice=voice)
    if status == "rejected":
        code = data.get("code")
        message = data.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            raise ProtocolError(CODE_BAD_REQUEST, "rejected response is missing fields")
        return Response(accepted=False, code=code, message=message)
    raise ProtocolError(CODE_BAD_REQUEST, "unknown response status")


def encode_rejection_v1(code: str, message: str) -> bytes:
    """A version 1 rejection, used when the version is unknown or the line is not JSON."""
    return encode_response(Response(accepted=False, code=code, message=message))


# --- version 2 -----------------------------------------------------------------


def encode_operation(
    op: str, *, request_id: str | None = None, payload: dict[str, Any] | None = None
) -> bytes:
    """A version 2 request line."""
    return _encode(
        {
            "version": PROTOCOL_VERSION_2,
            "op": op,
            "id": request_id,
            "payload": payload if payload is not None else {},
        }
    )


def decode_operation(data: dict[str, Any]) -> OperationRequest:
    """Validate a parsed version 2 request. Raises ProtocolError with a precise code."""
    unknown = sorted(set(data) - _OPERATION_FIELDS)
    if unknown:
        raise ProtocolError(CODE_BAD_REQUEST, f"unknown fields: {', '.join(unknown)}")
    request_id = data.get("id")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id) > MAX_REQUEST_ID_CHARS
    ):
        raise ProtocolError(
            CODE_BAD_REQUEST, f"id must be a string of at most {MAX_REQUEST_ID_CHARS} characters"
        )
    op = data.get("op")
    if not isinstance(op, str):
        raise ProtocolError(CODE_BAD_REQUEST, "op must be a string")
    if op not in OPERATIONS:
        raise ProtocolError(CODE_UNKNOWN_OPERATION, f"unknown operation {op!r}")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError(CODE_INVALID_PAYLOAD, "payload must be a JSON object")
    _check_payload(op, payload)
    return OperationRequest(op=op, request_id=request_id, payload=payload)


def _check_payload(op: str, payload: dict[str, Any]) -> None:
    allowed: frozenset[str] = frozenset()
    if op == OP_SPEAK:
        allowed = frozenset({"text"})
    elif op == OP_SET_VOLUME:
        allowed = frozenset({"volume"})
    extra = sorted(set(payload) - allowed)
    if extra:
        if op == OP_SPEAK and {"language", "voice"} & set(extra):
            raise ProtocolError(
                CODE_INVALID_PAYLOAD,
                "the language and voice cannot be chosen; the service detects them",
            )
        raise ProtocolError(CODE_INVALID_PAYLOAD, f"unexpected payload fields: {', '.join(extra)}")
    if op == OP_SPEAK and not isinstance(payload.get("text"), str):
        raise ProtocolError(CODE_INVALID_PAYLOAD, "text must be a string")
    if op == OP_SET_VOLUME:
        volume = payload.get("volume")
        if (
            isinstance(volume, bool)
            or not isinstance(volume, int | float)
            or not math.isfinite(volume)
            or not 0.0 <= volume <= 1.0
        ):
            raise ProtocolError(CODE_INVALID_PAYLOAD, "volume must be a number from 0.0 to 1.0")


def encode_result(request_id: str | None, result: dict[str, Any]) -> bytes:
    return _encode({"version": PROTOCOL_VERSION_2, "id": request_id, "ok": True, "result": result})


def encode_error(request_id: str | None, code: str, message: str) -> bytes:
    return _encode(
        {
            "version": PROTOCOL_VERSION_2,
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }
    )


def encode_stream_line(body: dict[str, Any]) -> bytes:
    """One subscription line: ``{"type": "event", ...}`` or ``{"type": "closed", ...}``."""
    return _encode({"version": PROTOCOL_VERSION_2, **body})


def decode_operation_reply(line: bytes) -> OperationReply:
    """Parse a reply to a version 2 request. Raises ProtocolError if it is not a valid reply."""
    data = decode_object(line)
    version = data.get("version")
    if version == PROTOCOL_VERSION:
        # A version 1 rejection: the service refused before it could read our version.
        rejection = decode_response(line)
        if rejection.accepted:
            raise ProtocolError(CODE_BAD_REQUEST, "unexpected version 1 acceptance")
        return OperationReply(
            request_id=None, ok=False, result={}, code=rejection.code, message=rejection.message
        )
    if version != PROTOCOL_VERSION_2:
        raise ProtocolError(CODE_UNSUPPORTED_VERSION, f"unexpected protocol version {version!r}")
    request_id = data.get("id")
    if data.get("ok") is True:
        result = data.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(CODE_BAD_REQUEST, "ok reply is missing its result")
        return OperationReply(request_id=request_id, ok=True, result=result)
    error = data.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise ProtocolError(CODE_BAD_REQUEST, "error reply is missing its code")
    message = error.get("message")
    return OperationReply(
        request_id=request_id,
        ok=False,
        result={},
        code=error["code"],
        message=message if isinstance(message, str) else "",
    )


def decode_stream_line(line: bytes) -> dict[str, Any]:
    """Parse one line of an event subscription and check that it is a stream message."""
    data = decode_object(line)
    if data.get("version") != PROTOCOL_VERSION_2 or data.get("type") not in ("event", "closed"):
        raise ProtocolError(CODE_BAD_REQUEST, "unexpected message on the event subscription")
    return data


# --- shared --------------------------------------------------------------------


def decode_object(line: bytes) -> dict[str, Any]:
    """Parse one line into a JSON object. Raises ProtocolError if it is not one."""
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError(CODE_TOO_LARGE, "the message is too large")
    if not line.endswith(b"\n"):
        raise ProtocolError(CODE_BAD_REQUEST, "message is not terminated by a newline")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(CODE_BAD_REQUEST, "message is not valid JSON in UTF-8") from exc
    if not isinstance(data, dict):
        raise ProtocolError(CODE_BAD_REQUEST, "message must be a JSON object")
    return data


def _encode(payload: dict[str, Any]) -> bytes:
    # ensure_ascii=False keeps Hebrew and Cyrillic readable on the wire; the line
    # is still UTF-8 and still a single line, because JSON escapes newlines.
    return json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
