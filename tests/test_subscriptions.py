"""Bounded subscriptions: ordering, overflow, shutdown and several subscribers."""

from __future__ import annotations

import threading
import time

import pytest

from fakes import FakeEngine, FakeSink, registry_with, wait_for
from reader_assistant.app.reader_service import EventKind, ReaderService
from reader_assistant.app.subscriptions import END_OVERFLOW, END_SHUTDOWN, Subscription
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE

EN = "Good morning, this is English."


def test_items_come_out_in_the_order_they_went_in() -> None:
    sub: Subscription[int] = Subscription(capacity=10)

    for value in (1, 2, 3):
        assert sub.put(value)

    assert sub.drain() == [1, 2, 3]
    assert sub.drain() == []


def test_a_full_queue_ends_the_subscription_and_drops_what_it_held() -> None:
    sub: Subscription[int] = Subscription(capacity=2)

    assert sub.put(1) and sub.put(2)
    assert sub.put(3) is False

    assert sub.end_reason == END_OVERFLOW
    assert sub.get(0.01) is None
    assert sub.put(4) is False


def test_ending_keeps_queued_items_readable_first() -> None:
    sub: Subscription[str] = Subscription(capacity=5)
    sub.put("last words")

    sub.end(END_SHUTDOWN)

    assert sub.get(0.01) == "last words"
    assert sub.get(0.01) is None
    assert sub.end_reason == END_SHUTDOWN


def test_get_waits_for_an_item_and_returns_none_on_timeout() -> None:
    sub: Subscription[int] = Subscription()
    start = time.monotonic()

    assert sub.get(0.05) is None
    assert time.monotonic() - start >= 0.04

    threading.Timer(0.02, lambda: sub.put(9)).start()
    assert sub.get(2.0) == 9


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Subscription(capacity=0)


def _service() -> ReaderService:
    sink = FakeSink()
    engines = [
        FakeEngine("kokoro", "en", ENGLISH_VOICE),
        FakeEngine("piper", "he", HEBREW_VOICE),
        FakeEngine("piper", "ru", RUSSIAN_VOICE),
    ]
    service = ReaderService(registry_with(*engines), AudioPlayer(lambda: sink))
    service.start()
    return service


def test_every_subscriber_sees_the_same_events_in_order() -> None:
    service = _service()
    try:
        _, first = service.subscribe()
        _, second = service.subscribe()
        service.submit(EN)
        wait_for(lambda: service.status().state.value == "idle")
        time.sleep(0.05)
        one = [event.seq for event in first.drain()]
        two = [event.seq for event in second.drain()]
    finally:
        service.close()

    assert one == two
    assert one == sorted(one)
    assert one[0] == 1


def test_unsubscribed_subscriber_gets_nothing_more() -> None:
    service = _service()
    try:
        _, sub = service.subscribe()
        service.unsubscribe(sub)
        service.submit(EN)
        wait_for(lambda: service.status().state.value == "idle")
        time.sleep(0.05)
        leftovers = sub.drain()
    finally:
        service.close()

    assert leftovers == []


def test_status_and_subscription_start_at_the_same_point() -> None:
    service = _service()
    try:
        service.submit(EN)
        wait_for(lambda: service.status().state.value == "idle")
        snapshot, sub = service.subscribe()
        service.submit(EN)
        wait_for(lambda: service.status().state.value == "idle")
        time.sleep(0.05)
        newer = sub.drain()
    finally:
        service.close()

    assert snapshot.event_seq > 0
    assert newer and newer[0].seq > snapshot.event_seq
    assert all(event.seq > snapshot.event_seq for event in newer)


def test_shutdown_is_the_last_event_and_ends_every_subscriber() -> None:
    service = _service()
    _, sub = service.subscribe()
    service.close()

    events = sub.drain()

    assert events[-1].kind is EventKind.SHUTDOWN
    assert sub.end_reason == END_SHUTDOWN
    _, late = service.subscribe()
    assert late.end_reason == END_SHUTDOWN
