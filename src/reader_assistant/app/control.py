"""Runs the version 2 operations on the one :class:`ReaderService` in this process.

The IPC layer only moves messages. This module turns them into service calls and back into
plain dictionaries, so that nothing outside the service ever touches the engines or the player.
"""

from __future__ import annotations

from typing import Any

from reader_assistant.app.reader_service import Event, ReaderService, ServiceStatus, SpeechError
from reader_assistant.app.subscriptions import Subscription
from reader_assistant.ipc.protocol import (
    CODE_INVALID_PAYLOAD,
    CODE_UNKNOWN_OPERATION,
    OP_PAUSE,
    OP_RESUME,
    OP_SET_VOLUME,
    OP_SPEAK,
    OP_STATUS,
    OP_STOP,
    ProtocolError,
)


def status_to_dict(status: ServiceStatus) -> dict[str, Any]:
    return {
        "state": status.state.value,
        "request_id": status.request_id,
        "language": status.language,
        "voice_id": status.voice_id,
        "engines": [
            {"language": item.language, "engine_id": item.engine_id, "loaded": item.loaded}
            for item in status.engines
        ],
        "last_error": status.last_error,
        "volume": status.volume,
        "event_seq": status.event_seq,
    }


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind.value,
        "request_id": event.request_id,
        "language": event.language,
        "voice_id": event.voice_id,
        "reason": event.reason,
        "code": event.code,
        "message": event.message,
        "duration_seconds": event.duration_seconds,
    }


class EventStream:
    """One subscriber: the status it starts from, then its events in order."""

    def __init__(
        self,
        service: ReaderService,
        status: dict[str, Any],
        subscription: Subscription[Event],
    ) -> None:
        self._service = service
        self._subscription = subscription
        self._status = status

    @property
    def status(self) -> dict[str, Any]:
        return self._status

    def next(self, timeout: float) -> dict[str, Any] | None:
        event = self._subscription.get(timeout)
        return None if event is None else event_to_dict(event)

    @property
    def end_reason(self) -> str | None:
        return self._subscription.end_reason

    def close(self) -> None:
        self._service.unsubscribe(self._subscription)


class ServiceControl:
    """The version 2 operations, run against ``service``."""

    def __init__(self, service: ReaderService) -> None:
        self._service = service

    def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op == OP_SPEAK:
            return self._speak(payload)
        if op == OP_STATUS:
            return status_to_dict(self._service.status())
        if op == OP_PAUSE:
            changed = self._service.pause()
            return {"changed": changed, "state": self._state()}
        if op == OP_RESUME:
            changed = self._service.resume()
            return {"changed": changed, "state": self._state()}
        if op == OP_STOP:
            changed = self._service.stop()
            return {"changed": changed, "state": self._state()}
        if op == OP_SET_VOLUME:
            return self._set_volume(payload)
        raise ProtocolError(CODE_UNKNOWN_OPERATION, f"unknown operation {op!r}")

    def open_stream(self) -> EventStream:
        status, subscription = self._service.subscribe()
        return EventStream(self._service, status_to_dict(status), subscription)

    def _speak(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            accepted = self._service.submit(str(payload["text"]))
        except SpeechError as exc:
            raise ProtocolError(exc.code, exc.message) from exc
        return {
            "request_id": accepted.request_id,
            "language": accepted.language,
            "voice": accepted.voice_id,
        }

    def _set_volume(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self._service.set_volume(float(payload["volume"]))
        except ValueError as exc:
            raise ProtocolError(CODE_INVALID_PAYLOAD, str(exc)) from exc
        return {"volume": self._service.status().volume}

    def _state(self) -> str:
        return self._service.status().state.value
