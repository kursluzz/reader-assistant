"""Event-loop latency probe: how late the window's timer fires.

A timer is scheduled every few milliseconds. If the Tk main thread is blocked, the timer
fires late, and the lateness is the latency. The probe records the lateness per phase,
so that "GUI stayed responsive during X" becomes a number.

The probe has no tkinter dependency. It only stores samples and summarises them.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

DEFAULT_INTERVAL_MS: Final = 10


@dataclass(frozen=True, slots=True)
class PhaseSummary:
    phase: str
    samples: int
    typical_ms: float  # median
    max_ms: float


class LatencyProbe:
    """Collects timer lateness, grouped by the phase that was active when it fired."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._phase = "idle"
        self._samples: dict[str, list[float]] = {"idle": []}

    @property
    def phase(self) -> str:
        return self._phase

    def mark(self, phase: str) -> None:
        """Start a new phase. Samples from now on are filed under it."""
        self._phase = phase
        self._samples.setdefault(phase, [])

    def record(self, scheduled_at: float) -> None:
        """Record how late a timer fired, given the time it was scheduled for."""
        lateness_ms = max(0.0, (self._clock() - scheduled_at) * 1000.0)
        self._samples.setdefault(self._phase, []).append(lateness_ms)

    def summary(self) -> tuple[PhaseSummary, ...]:
        """Per-phase median and maximum lateness, in the order the phases first appeared."""
        result: list[PhaseSummary] = []
        for phase, values in self._samples.items():
            if not values:
                continue
            result.append(
                PhaseSummary(
                    phase=phase,
                    samples=len(values),
                    typical_ms=statistics.median(values),
                    max_ms=max(values),
                )
            )
        return tuple(result)
