"""Bounded event subscriptions.

Every subscriber has its own bounded queue. The service only ever appends to it, and never
waits for a subscriber. A subscriber that falls behind is ended with reason ``overflow``
(its queue is dropped), so memory stays bounded and the service never blocks on a slow reader.
A subscriber that ends must subscribe again to get a fresh status snapshot.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Final

DEFAULT_CAPACITY: Final = 256
END_SHUTDOWN: Final = "shutdown"
END_OVERFLOW: Final = "overflow"


class Subscription[T]:
    """A bounded, thread-safe queue of events for one subscriber."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._items: deque[T] = deque()
        self._cond = threading.Condition()
        self._end_reason: str | None = None

    @property
    def end_reason(self) -> str | None:
        """Why the subscription ended, or None while it is live."""
        with self._cond:
            return self._end_reason

    def put(self, item: T) -> bool:
        """Queue ``item``. Never blocks. Returns False if the subscription has ended.

        If the queue is full the subscription ends with reason ``overflow`` and its pending
        items are dropped, so the subscriber learns it must resubscribe.
        """
        with self._cond:
            if self._end_reason is not None:
                return False
            if len(self._items) >= self._capacity:
                self._end_reason = END_OVERFLOW
                self._items.clear()
                self._cond.notify_all()
                return False
            self._items.append(item)
            self._cond.notify_all()
            return True

    def end(self, reason: str) -> None:
        """End the subscription. Items already queued can still be read."""
        with self._cond:
            if self._end_reason is None:
                self._end_reason = reason
            self._cond.notify_all()

    def get(self, timeout: float) -> T | None:
        """Return the next item, waiting up to ``timeout`` seconds. None on timeout or end."""
        with self._cond:
            if not self._items and self._end_reason is None:
                self._cond.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def drain(self) -> list[T]:
        """Return and remove every queued item. Does not wait."""
        with self._cond:
            items = list(self._items)
            self._items.clear()
            return items
