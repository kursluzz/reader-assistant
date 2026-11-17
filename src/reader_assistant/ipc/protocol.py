"""The IPC message format: newline-delimited JSON, one request and one response per connection.

Request::

    {"version": 1, "command": "speak", "text": "..."}

Response, accepted::

    {"version": 1, "status": "accepted", "language": "en", "voice": "af_heart"}

Response, rejected::

    {"version": 1, "status": "rejected", "code": "empty_text", "message": "..."}

Only the ``speak`` command exists. Unknown commands, versions, fields and types are
rejected, never guessed at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from reader_assistant.limits import MAX_TEXT_BYTES

PROTOCOL_VERSION: Final = 1
COMMAND_SPEAK: Final = "speak"
# Upper bound for one message line. JSON escaping can grow text several times, so this
# is well above MAX_TEXT_BYTES while still bounding memory use.
MAX_MESSAGE_BYTES: Final = 512 * 1024

CODE_BAD_REQUEST: Final = "bad_request"
CODE_TOO_LARGE: Final = "too_large"
CODE_EMPTY_TEXT: Final = "empty_text"
CODE_UNSUPPORTED: Final = "unsupported"
CODE_ENGINE_FAILED: Final = "engine_failed"
CODE_AUDIO_FAILED: Final = "audio_failed"
CODE_INTERNAL: Final = "internal"


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


def encode_request(request: SpeakRequest) -> bytes:
    payload = {"version": PROTOCOL_VERSION, "command": COMMAND_SPEAK, "text": request.text}
    return _encode(payload)


def decode_request(line: bytes) -> SpeakRequest:
    """Parse and validate one request line. Raises ProtocolError on any problem."""
    data = _decode_object(line)
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
    """Parse one response line. Raises ProtocolError if it is not a valid response."""
    data = _decode_object(line)
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


def _encode(payload: dict[str, Any]) -> bytes:
    # ensure_ascii=False keeps Hebrew and Cyrillic readable on the wire; the line
    # is still UTF-8 and still a single line, because JSON escapes newlines.
    return json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"


def _decode_object(line: bytes) -> dict[str, Any]:
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
