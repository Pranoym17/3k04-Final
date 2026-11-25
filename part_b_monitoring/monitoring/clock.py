"""Clock module.

Responsibility: tell every other module the current time.
Secret: where time comes from (operating system clock, hospital time service,
or a manual clock in tests). Callers see only `now()` in seconds since the
epoch, so a change of time source touches this file only.
"""
from __future__ import annotations

import time


class Clock:
    """Interface for the injected source of epoch seconds."""
    def now(self) -> float:  # pragma: no cover - interface
        """Return the current time in seconds since the epoch."""
        raise NotImplementedError


class SystemClock(Clock):
    """Read epoch seconds from the operating system clock."""
    def now(self) -> float:
        """Return the current time in seconds since the epoch."""
        return time.time()


class ManualClock(Clock):
    """Deterministic clock for tests and for the change-drill demo."""

    def __init__(self, start: float = 1_800_000_000.0):
        """Initialise the manual clock at the supplied epoch time."""
        self._t = float(start)

    def now(self) -> float:
        """Return the current time in seconds since the epoch."""
        return self._t

    def advance(self, seconds: float) -> float:
        """Advance manual time by nonnegative seconds and return the new time."""
        if seconds < 0:
            raise ValueError("time cannot move backwards")
        self._t += seconds
        return self._t
