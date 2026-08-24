"""
PER-HOST RATE LIMITING — the third decorative control in `config.yaml`, made
load-bearing (ADR-034).

WHAT WAS WRONG

`config.yaml targets.allow_policy.max_requests_per_host_per_min: 60` has existed
since P01. `SECURITY.md` §3 promises the target "must pass the allow-policy".
ADR-029 built the allow-policy and implemented `deny_hosts`,
`deny_private_ranges` and `deny_metadata_hosts` -- but explicitly NOT this key,
because a rate limit needs a clock and mutable shared state and `core/` may have
neither (test_architecture.py). ADR-029 said so out loud and deferred it to P09
rather than faking it:

    `max_requests_per_host_per_min` is likewise NOT implemented here: a rate
    limit needs a clock and shared state. Pretending a pure function enforces
    it would recreate the very defect this module fixes.

`atlas/engine/handout.py` repeated the deferral. So for two phases the number in
the config file was read by nobody, which is the ADR-019 defect class (a captured
fact that nothing reads) -- the fifth occurrence, after ADR-019, ADR-021,
ADR-029 and ADR-033. This module is where it stops being deferred.

WHY IT LIVES IN `engine/` AND NOT IN THE POLICY

The obvious place is `core/policy/target_policy.py`, next to the rest of the
allow-policy. It cannot go there. A limiter must know what time it is and must
remember what it has already seen; `core/` is forbidden both a clock and mutable
cross-call state, and smuggling either in would trade a real architectural
guarantee for the appearance of tidiness. So the pure part stays pure, and the
stateful part lives here, one layer out, with the clock INJECTED (`ClockPort`)
so every rule below is tested in microseconds against a fake clock rather than
by actually waiting -- the legacy tree's 13 in-line `time.sleep()` calls are the
reason that matters.

THREE DECISIONS THAT ARE NOT INCIDENTAL

1. MONOTONIC, NOT WALL-CLOCK.
   The window is measured with `clock.monotonic_ms()`, never `clock.now()`.
   A wall-clock limiter is defeated by a clock adjustment in BOTH directions: set
   the clock back and the window never expires (the caller is locked out, with no
   error naming why); set it forward and the whole window expires at once (the
   limit is bypassed). `ClockPort.monotonic_ms` exists precisely because the
   legacy code measured durations with `time.time()` deltas, and ADR-003 records
   what that cost the latency figures the admission gate depends on.

2. SLIDING, NOT FIXED WINDOW.
   A fixed 60 s bucket that resets on the minute admits `limit` requests at
   59.9 s and `limit` more at 60.1 s -- **2x the configured rate** across a
   200 ms span, which is exactly the burst a per-host limit exists to prevent
   (ADR-006: the GeoNode incident was caused by hammering one host). This keeps
   the request timestamps and counts the ones inside the trailing window, so the
   rate holds across every window position, not just the aligned ones.
   `test_fixed_window_boundary_burst_is_refused` is the negative control.

3. BOUNDED MEMORY THAT FAILS CLOSED.
   One dict entry per host, keyed by caller-supplied input, is an unbounded
   allocation driven by an untrusted value. It is capped. The subtlety is what
   to do when the cap is reached: evicting an ACTIVE host to make room would
   reset its budget and turn the cap into a rate-limit BYPASS (spray enough
   distinct hosts and every real host's counter is dropped). So eviction only
   ever removes hosts whose window has fully drained, and if none has, the
   request is REFUSED with `LIMITER_SATURATED` rather than allowed. A limiter
   that fails open under pressure is not a limiter.

CHECK-THEN-COMMIT, BECAUSE THE ORDER IS OBSERVABLE

`check()` reports whether a request WOULD be admitted without recording it;
`consume()` records. They are separate because a request refused for some other
reason -- a denied target (ADR-029), an empty pool -- must not spend the host's
budget. Charging for a refusal would let a caller exhaust their own limit on
requests that never happened, and the operator would see a rate-limit refusal
whose cause was elsewhere entirely: B-02 (23 silent handlers, no diagnosable
failure) is what that habit costs.

The intended call order at the serving layer is therefore: validate the target
(pure, free, ADR-029) -> `consume()` -> serve. Never `consume()` first.

REFUSALS ARE NAMED, AND CARRY THE WAIT

`consume()` returns a `RateDecision`, never a bool, in the style of
`TargetRefusal` / `DropReason` / `HandoutRefusal`. A bool would tell the caller
"no" without telling them for how long, and "retry later" with no number is how
a client ends up either hammering or sleeping far longer than required. The
decision carries `retry_after_s`, computed from the oldest timestamp in the
window -- the moment the window actually opens, not a guess.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from atlas.core.domain.source import Target
from atlas.core.parsing.url import split_url
from atlas.core.policy.target_policy import canonical_host
from atlas.core.ports.clock import ClockPort


class RateRefusal(str):
    """
    Named refusal reasons. A `str` subclass for the same reason `TargetRefusal`
    and `DropReason` are: these are diagnostics that get reported, not control
    flow that gets branched on.
    """

    OVER_LIMIT = "OVER_LIMIT"                  # this host's window is full
    LIMITER_SATURATED = "LIMITER_SATURATED"    # host-table cap hit, nothing evictable
    NO_HOST = "NO_HOST"                        # unkeyable target; see _key_of


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """
    Bounds on the limiter. Values come from `config.yaml`
    (`targets.allow_policy.max_requests_per_host_per_min`), never from a literal
    at a call site -- the config file is authoritative, as ADR-031 established
    for `deny_hosts`.
    """

    max_requests_per_host_per_min: int = 60
    window_s: float = 60.0
    max_tracked_hosts: int = 4096

    def __post_init__(self) -> None:
        if self.max_requests_per_host_per_min < 1:
            # 0 would mean "refuse everything", which is a configuration
            # mistake that looks like an outage. Refused loudly at construction
            # instead of at 3am.
            raise ValueError(
                "max_requests_per_host_per_min must be >= 1, got "
                f"{self.max_requests_per_host_per_min}"
            )
        if self.window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {self.window_s}")
        if self.max_tracked_hosts < 1:
            raise ValueError(
                f"max_tracked_hosts must be >= 1, got {self.max_tracked_hosts}"
            )


@dataclass(frozen=True, slots=True)
class RateDecision:
    """
    The outcome of one admission question.

    `remaining` and `retry_after_s` are both carried because they answer
    different questions: how much budget is left, and -- when there is none --
    when the next slot actually frees. A caller given only "no" can do nothing
    but guess.
    """

    allowed: bool
    host: str | None
    refusal: str | None = None
    remaining: int = 0
    retry_after_s: float = 0.0
    observed: int = 0

    def __post_init__(self) -> None:
        # The accounting identity, checked rather than trusted -- the same
        # discipline HandoutResult applies to leased rows.
        if self.allowed and self.refusal is not None:
            raise ValueError(
                f"allowed decision carries refusal {self.refusal!r}: a caller "
                "branching on either field would get a different answer"
            )
        if not self.allowed and self.refusal is None:
            raise ValueError(
                "refused decision has no reason: an unnamed refusal is B-02"
            )
        if self.retry_after_s < 0:
            raise ValueError(f"retry_after_s must be >= 0, got {self.retry_after_s}")


class HostRateLimiter:
    """
    Sliding-window, per-host request limiter over an injected clock.

    Safe for concurrent use from multiple threads in one process. It is NOT a
    cross-process limiter and does not pretend to be: the counters live in this
    object's memory, so two API workers each get their own. Saying so matters --
    an operator who believes this caps the whole deployment would be wrong by a
    factor of the worker count. A shared limiter needs shared storage (the store
    or an external counter) and is named as future scope (P11), not implied here
    by a hopeful class name.
    """

    def __init__(self, policy: RateLimitPolicy, clock: ClockPort) -> None:
        self._policy = policy
        self._clock = clock
        # host -> monotonic-ms timestamps of admitted requests, oldest first.
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    # ── keying ────────────────────────────────────────────────────────────────
    @staticmethod
    def _key_of(target: Target) -> str | None:
        """
        The bucket key: the target's canonical host.

        Canonicalisation is NOT re-implemented here -- it is the same
        `canonical_host` the allow-policy uses (ADR-034). If this layer had its
        own idea of host identity, `example.com` and `Example.COM.` could be
        denied as one host by the policy and counted as two by the limiter, and
        two buckets for one host is a silent doubling of the configured rate.
        Port is deliberately NOT part of the key: the limit protects the HOST
        (ADR-006 -- one origin, one budget), and per-port buckets would let
        :80 and :443 each spend the full allowance.
        """
        host = canonical_host(split_url(target.url).host)
        return host or None

    # ── read-only probe ───────────────────────────────────────────────────────
    def check(self, target: Target) -> RateDecision:
        """Would this be admitted? Records NOTHING. See the check-then-commit note."""
        with self._lock:
            return self._decide(target, commit=False)

    def consume(self, target: Target) -> RateDecision:
        """Admit and record, or refuse. The only method that mutates state."""
        with self._lock:
            return self._decide(target, commit=True)

    # ── the rule ──────────────────────────────────────────────────────────────
    def _decide(self, target: Target, *, commit: bool) -> RateDecision:
        limit = self._policy.max_requests_per_host_per_min
        window_ms = self._policy.window_s * 1000.0
        now = self._clock.monotonic_ms()      # monotonic; see the module docstring

        host = self._key_of(target)
        if host is None:
            # An unkeyable target cannot be rate limited, so it is refused
            # rather than waved through. check_target() also refuses it
            # (NO_HOST); the limiter must not be the layer that disagrees.
            return RateDecision(
                allowed=False, host=None, refusal=RateRefusal.NO_HOST,
                remaining=0, retry_after_s=0.0, observed=0,
            )

        hits = self._hits.get(host)
        if hits is None:
            if len(self._hits) >= self._policy.max_tracked_hosts:
                if not self._evict_drained(now, window_ms):
                    # Fails CLOSED. Evicting a live host here would reset its
                    # budget and make the cap a bypass. See decision 3.
                    return RateDecision(
                        allowed=False, host=host,
                        refusal=RateRefusal.LIMITER_SATURATED,
                        remaining=0, retry_after_s=self._policy.window_s,
                        observed=0,
                    )
            hits = deque()
            if commit:
                self._hits[host] = hits

        # Drop everything that has fallen out of the trailing window. This is
        # what makes the window SLIDE; a fixed bucket would reset here instead
        # and admit 2*limit across the boundary.
        self._drain(hits, now, window_ms)

        observed = len(hits)
        if observed >= limit:
            # The window opens when the OLDEST hit in it expires -- computed,
            # not guessed, so a caller that honours it succeeds on first retry.
            retry_after_s = max(0.0, (hits[0] + window_ms - now) / 1000.0)
            return RateDecision(
                allowed=False, host=host, refusal=RateRefusal.OVER_LIMIT,
                remaining=0, retry_after_s=retry_after_s, observed=observed,
            )

        if commit:
            hits.append(now)
            # A host seen once but never again must not be tracked forever; the
            # entry is only created on commit, and drained entries are collected
            # by _evict_drained under pressure.
            self._hits[host] = hits
            observed += 1

        return RateDecision(
            allowed=True, host=host, refusal=None,
            remaining=max(0, limit - observed), retry_after_s=0.0,
            observed=observed,
        )

    @staticmethod
    def _drain(hits: deque[float], now: float, window_ms: float) -> None:
        """Discard hits older than the window. Oldest-first order makes this O(expired)."""
        cutoff = now - window_ms
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _evict_drained(self, now: float, window_ms: float) -> bool:
        """
        Remove hosts whose window is now empty. Returns True if room was freed.

        Only ever drops hosts with NO live hits, so eviction can never hand back
        budget to a host that is actively spending it.
        """
        dead = []
        for h, d in self._hits.items():
            self._drain(d, now, window_ms)
            if not d:
                dead.append(h)
        for h in dead:
            del self._hits[h]
        return bool(dead)

    # ── introspection, for the operator and for tests ─────────────────────────
    def tracked_hosts(self) -> int:
        with self._lock:
            return len(self._hits)

    def observed(self, target: Target) -> int:
        """Live hit count for a target's host, after draining. Read-only."""
        with self._lock:
            host = self._key_of(target)
            if host is None:
                return 0
            hits = self._hits.get(host)
            if hits is None:
                return 0
            self._drain(hits, self._clock.monotonic_ms(),
                        self._policy.window_s * 1000.0)
            return len(hits)
