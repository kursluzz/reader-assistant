from __future__ import annotations

import json

import pytest

from reader_assistant.ipc.protocol import (
    CODE_BAD_REQUEST,
    CODE_EMPTY_TEXT,
    CODE_TOO_LARGE,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Response,
    SpeakRequest,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from reader_assistant.limits import MAX_TEXT_BYTES


def _request_line(**overrides: object) -> bytes:
    payload: dict[str, object] = {"version": 1, "command": "speak", "text": "hello"}
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8") + b"\n"


def test_request_round_trips_unicode_text() -> None:
    request = SpeakRequest(text="שלום, Привет, hello 2024")

    assert decode_request(encode_request(request)) == request


def test_encoded_request_is_a_single_line_even_with_newlines_in_text() -> None:
    line = encode_request(SpeakRequest(text="first\nsecond\r\nthird"))

    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")
    assert decode_request(line).text == "first\nsecond\r\nthird"


def test_valid_request_is_decoded() -> None:
    assert decode_request(_request_line()) == SpeakRequest(text="hello")


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b'{"version": 1, "command": "speak", "text": "hi"}', CODE_BAD_REQUEST),  # no newline
        (b"not json\n", CODE_BAD_REQUEST),
        (b"[1, 2, 3]\n", CODE_BAD_REQUEST),
        (b"\xff\xfe\n", CODE_BAD_REQUEST),  # not UTF-8
        (b"\n", CODE_BAD_REQUEST),
    ],
)
def test_malformed_lines_are_rejected(line: bytes, code: str) -> None:
    with pytest.raises(ProtocolError) as excinfo:
        decode_request(line)

    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"version": None},
        {"command": "stop"},  # only speak exists
        {"command": None},
        {"text": 42},
        {"text": None},
    ],
)
def test_invalid_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProtocolError) as excinfo:
        decode_request(_request_line(**overrides))

    assert excinfo.value.code == CODE_BAD_REQUEST


def test_unexpected_extra_fields_do_not_change_the_meaning() -> None:
    # Extra keys are ignored, so future clients can send more without breaking this one.
    assert decode_request(_request_line(extra="x")) == SpeakRequest(text="hello")


def test_empty_and_whitespace_text_is_rejected() -> None:
    for text in ("", "   \n\t"):
        with pytest.raises(ProtocolError) as excinfo:
            decode_request(_request_line(text=text))
        assert excinfo.value.code == CODE_EMPTY_TEXT


def test_text_at_the_limit_is_accepted() -> None:
    text = "a" * MAX_TEXT_BYTES

    assert decode_request(_request_line(text=text)).text == text


def test_text_over_the_limit_is_rejected_as_too_large() -> None:
    with pytest.raises(ProtocolError) as excinfo:
        decode_request(_request_line(text="a" * (MAX_TEXT_BYTES + 1)))

    assert excinfo.value.code == CODE_TOO_LARGE


def test_text_limit_is_measured_in_utf8_bytes_not_characters() -> None:
    # Each Hebrew letter is two bytes, so this is over the byte limit while having fewer
    # characters than the limit.
    text = "ש" * (MAX_TEXT_BYTES // 2 + 1)
    assert len(text) < MAX_TEXT_BYTES

    with pytest.raises(ProtocolError) as excinfo:
        decode_request(_request_line(text=text))

    assert excinfo.value.code == CODE_TOO_LARGE


def test_message_over_the_line_limit_is_rejected() -> None:
    line = b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n"

    with pytest.raises(ProtocolError) as excinfo:
        decode_request(line)

    assert excinfo.value.code == CODE_TOO_LARGE


def test_accepted_response_round_trips() -> None:
    response = Response(accepted=True, language="he", voice="he_IL-saspeech-medium")

    assert decode_response(encode_response(response)) == response


def test_rejected_response_round_trips() -> None:
    response = Response(accepted=False, code=CODE_EMPTY_TEXT, message="The text is empty.")

    assert decode_response(encode_response(response)) == response


@pytest.mark.parametrize(
    "line",
    [
        b'{"version": 1, "status": "maybe"}\n',
        b'{"version": 1, "status": "accepted"}\n',  # missing language and voice
        b'{"version": 9, "status": "accepted", "language": "en", "voice": "x"}\n',
        b"garbage\n",
    ],
)
def test_invalid_responses_are_rejected(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_response(line)
