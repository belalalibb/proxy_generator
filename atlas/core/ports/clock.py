"""
ClockPort — time as an injected dependency.

WHY: the legacy tree calls time.sleep() 13 times inside its control flow
(engineering/raw/bug_scan.json). Any cooldown or scheduling rule written that way
can only be tested by actually waiting, so in practice it never gets tested.

ADR-006 requires an exponential cooldown of `base * 2^consecutive_failures` capped
at one hour. With this port, that rule is verified in microseconds against a fake
clock, and core/ never imports `time` or `asyncio` at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    """Read the current time and schedule relative deadlines. No sleeping in core/."""

    def now(self) -> datetime:
        """Timezone-aware current time. Implementations MUST return UTC-aware values."""
        ...

    def monotonic_ms(self) -> float:
        """
        Monotonic milliseconds, for measuring durations.

        Latency MUST be measured with a monotonic source: the legacy code used
        wall-clock time.time() deltas, which a clock adjustment can distort into a
        negative or absurd latency -- and latency is the value the entire admission
        gate depends on (ADR-003).
        """
        ...

    def deadline(self, after_ms: float) -> datetime:
        """Absolute time `after_ms` from now -- used for cooldowns and lease expiry."""
        ...


def cooldown_delay(consecutive_failures: int, *, base_s: float = 30.0,
                   cap_s: float = 3600.0) -> timedelta:
    """
    ADR-006: exponential backoff on CONSECUTIVE failures, capped at 1 hour.

    Pure function, deliberately here rather than in an adapter, so the rule is
    unit-testable and identical everywhere.

    A single failure must NEVER disable a source: that is exactly how the GeoNode
    API -- 230 019 bytes of valid JSON, 500 proxies -- got filed as TRULY_EMPTY
    from one throttled 659-byte read.
    """
    if consecutive_failures <= 0:
        return timedelta(0)
    delay = base_s * (2 ** (consecutive_failures - 1))
    return timedelta(seconds=min(delay, cap_s))
