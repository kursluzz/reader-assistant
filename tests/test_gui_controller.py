"""The GUI controller, against a recording link. No display, no sockets, no threads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reader_assistant.clipboard.reader import ClipboardError
from reader_assistant.ui.controller import GuiController
from reader_assistant.ui.service_link import LinkMessage, LinkState

EN_TEXT = "Good morning, this is English."


@dataclass
class RecordingLink:
    """Records what the controller asks for. ``retry`` and ``close`` are counted."""

    sent: list[tuple[str, dict[str, Any] | None, bool]] = field(default_factory=list)
    retries: int = 0
    closed: int = 0

    def send(
        self, op: str, payload: dict[str, Any] | None = None, *, coalesce: bool = False
    ) -> None:
        self.sent.append((op, payload, coalesce))

    def poll(self) -> list[LinkMessage]:
        return []

    def retry(self) -> None:
        self.retries += 1

    def close(self, timeout: float = 1.0) -> None:
        self.closed += 1


def _controller(
    link: RecordingLink | None = None, clipboard: Any = None
) -> tuple[GuiController, RecordingLink]:
    recording = link or RecordingLink()
    controller = GuiController(recording, clipboard_reader=clipboard or (lambda: "clip"))  # type: ignore[arg-type]
    return controller, recording


def _connected(controller: GuiController, **status: Any) -> None:
    controller.apply(LinkMessage(kind="connection", state=LinkState.CONNECTED, text="ok"))
    body = {"state": "idle", "language": None, "voice_id": None, "volume": 1.0, "event_seq": 0}
    body.update(status)
    controller.apply(LinkMessage(kind="status", data=body))


def _event(controller: GuiController, kind: str, **fields: Any) -> None:
    controller.apply(LinkMessage(kind="event", data={"kind": kind, **fields}))


# --- actions go to the service as plain operations --------------------------------


def test_read_sends_only_the_text_so_the_service_decides_the_language() -> None:
    controller, link = _controller()

    assert controller.read(EN_TEXT) == ""

    assert link.sent == [("speak", {"text": EN_TEXT}, False)]


def test_empty_text_is_rejected_locally_and_nothing_is_sent() -> None:
    controller, link = _controller()

    message = controller.read("   ")

    assert message == "The text is empty."
    assert link.sent == []


def test_stop_and_retry_are_forwarded() -> None:
    controller, link = _controller()

    controller.stop()
    controller.retry()

    assert link.sent == [("stop", None, False)]
    assert link.retries == 1


def test_volume_is_clamped_and_sent_as_a_fraction_coalesced() -> None:
    controller, link = _controller()

    controller.set_volume_percent(150)
    controller.set_volume_percent(-4)
    controller.set_volume_percent(40)

    assert [item[1] for item in link.sent] == [{"volume": 1.0}, {"volume": 0.0}, {"volume": 0.4}]
    assert all(item[0] == "set_volume" and item[2] is True for item in link.sent)


def test_pause_toggles_by_the_last_known_state() -> None:
    controller, link = _controller()
    _connected(controller, state="playing")
    controller.toggle_pause()
    _connected(controller, state="paused")
    controller.toggle_pause()

    assert [op for op, _, _ in link.sent] == ["pause", "resume"]


def test_paste_failure_is_shown_as_an_error() -> None:
    def fail() -> str:
        raise ClipboardError("The clipboard is empty.")

    controller, _ = _controller(clipboard=fail)
    controller.start_paste()
    result = None
    for _ in range(200):
        result = controller.poll_paste()
        if result is not None:
            break
        import time

        time.sleep(0.005)

    assert result == ""
    assert controller.display_state().error == "The clipboard is empty."


# --- display ---------------------------------------------------------------------


def test_controls_are_disabled_while_idle_or_disconnected() -> None:
    controller, _ = _controller()
    before = controller.display_state()
    _connected(controller, state="idle")
    idle = controller.display_state()

    assert before.can_pause is False and before.can_stop is False
    assert idle.can_pause is False and idle.can_stop is False
    assert idle.status == "Idle."


def test_playing_shows_language_and_voice() -> None:
    controller, _ = _controller()
    _connected(controller, state="playing", language="he", voice_id="he_IL-saspeech-medium")

    state = controller.display_state()

    assert state.status == "Playing (he, he_IL-saspeech-medium)"
    assert state.can_pause and state.can_stop
    assert state.pause_label == "Pause"


def test_paused_offers_resume() -> None:
    controller, _ = _controller()
    _connected(controller, state="paused", language="en", voice_id="af_heart")

    state = controller.display_state()

    assert state.pause_label == "Resume"


def test_disconnected_shows_the_connection_message_and_retry_when_unavailable() -> None:
    controller, _ = _controller()
    controller.apply(
        LinkMessage(
            kind="connection", state=LinkState.UNAVAILABLE, text="The service did not start."
        )
    )

    state = controller.display_state()

    assert state.status == "The service did not start."
    assert state.connection == "The service did not start."
    assert state.can_retry is True
    assert state.can_pause is False and state.can_stop is False


def test_connected_clears_the_connection_text_and_hides_retry() -> None:
    controller, _ = _controller()
    _connected(controller)

    state = controller.display_state()

    assert state.connection == ""
    assert state.can_retry is False


def test_volume_follows_the_service_once_after_each_connection() -> None:
    controller, _ = _controller()
    _connected(controller, volume=0.25)

    assert controller.take_volume_sync() == 25.0
    assert controller.take_volume_sync() is None


# --- errors: only for the latest request ----------------------------------------------


def test_error_for_the_latest_request_is_shown() -> None:
    controller, _ = _controller()
    _connected(controller)
    _event(controller, "accepted", request_id=5)

    _event(controller, "error", request_id=5, message="engine failed")

    assert controller.display_state().error == "engine failed"


def test_stale_error_from_an_older_request_is_dropped() -> None:
    controller, _ = _controller()
    _connected(controller)
    _event(controller, "accepted", request_id=5)
    _event(controller, "accepted", request_id=6)

    _event(controller, "error", request_id=5, message="old failure")

    assert controller.display_state().error == ""


def test_an_accepted_request_from_another_client_clears_the_old_error() -> None:
    controller, _ = _controller()
    _connected(controller)
    _event(controller, "accepted", request_id=1)
    _event(controller, "error", request_id=1, message="first failed")

    _event(controller, "accepted", request_id=2)

    assert controller.display_state().error == ""


def test_a_rejected_operation_result_is_shown() -> None:
    controller, _ = _controller()
    controller.apply(
        LinkMessage(kind="result", op="speak", ok=False, code="unsupported", text="no letters")
    )

    assert controller.display_state().error == "no letters"


def test_state_events_ask_the_service_for_a_fresh_status() -> None:
    controller, link = _controller()

    _event(controller, "playback_started", request_id=1)
    _event(controller, "accepted", request_id=2)

    assert [op for op, _, _ in link.sent] == ["status"]
