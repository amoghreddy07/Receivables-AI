"""Swappable clock.

All timestamps in the system flow through `now()` so tests and the evaluation
harness can run on a deterministic VIRTUAL clock while the web app uses the
real wall clock. Times are naive local (Asia/Kolkata for the demo) on purpose —
policy window checks compare hour-of-day only.
"""
from __future__ import annotations

import threading
from datetime import datetime


class _RealClock:
    def now(self) -> datetime:
        return datetime.now()

    def advance(self, **kwargs) -> datetime:  # pragma: no cover - not used on real clock
        raise RuntimeError("advance() is only valid on a virtual clock")


class VirtualClock:
    """Deterministic clock used by tests and the evaluation harness."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 5, 10, 0, 0)  # a Monday 10:00 IST
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, *, hours: float = 0, minutes: float = 0, seconds: float = 0, to: datetime | None = None) -> datetime:
        from datetime import timedelta

        with self._lock:
            if to is not None:
                if to < self._now:
                    raise ValueError("cannot advance clock backwards")
                self._now = to
            else:
                self._now += timedelta(hours=hours, minutes=minutes, seconds=seconds)
            return self._now

    def set(self, dt: datetime) -> None:
        with self._lock:
            self._now = dt


_clock: _RealClock | VirtualClock = _RealClock()


def now() -> datetime:
    return _clock.now()


def set_clock(clock: _RealClock | VirtualClock) -> None:
    global _clock
    _clock = clock


def reset_clock() -> None:
    global _clock
    _clock = _RealClock()
