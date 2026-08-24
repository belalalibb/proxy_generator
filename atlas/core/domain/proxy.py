"""
Proxy — the central domain object. PURE: no I/O, no network, no clock.

Design forced by measurement (engineering/BASELINE.json, engineering/BUG_LEDGER.md):

  * The legacy pool was a line of text in proxy.txt. It could not express a LEASED
    state, so H3 (no double delivery) was unachievable, and a truncating rewrite
    (proxy_generator_v2.py:467) could destroy the working set (B-04).
  * The legacy accept rule was `status == 200 and len(body) > 1000` on ONE sample,
    which admitted a p50 of 6 359.5 ms and a p95 of 15 903 ms (n=102). Latency was
    measured 59 times in the legacy tree and never once compared to a threshold.
    So a Proxy carries a full LatencyProfile, never a single number (ADR-003).
  * The label on a source is a hint, not a fact: TheSpeedX/SOCKS-List/master/http.txt
    is a SOCKS list named http.txt whose 2 853 candidates the legacy code tested as
    HTTP and discarded (B-12). So `protocol` is DISCOVERED and `labelled_protocol`
    is kept beside it to detect the mismatch (ADR-005).
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Self

_HOSTPORT = re.compile(r"^\s*(?:(?P<scheme>\w+)://)?(?P<host>[^:/@\s]+):(?P<port>\d{1,5})\s*$")


class Protocol(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    UNKNOWN = "unknown"


class Anonymity(str, Enum):
    TRANSPARENT = "transparent"   # forwards your IP -- worthless for most uses
    ANONYMOUS = "anonymous"
    ELITE = "elite"
    UNKNOWN = "unknown"


class ProxyState(str, Enum):
    """
    The state machine proxy.txt could not express (ADR-004).

    DISCOVERED -> a candidate, never probed.
    PROBING    -> a probe is in flight (prevents duplicate work).
    READY      -> passed the admission gate; leasable.
    LEASED     -> handed to exactly one consumer. THE H3 GUARANTEE.
    COOLING    -> failed recently; eligible again after a cooldown (ADR-006).
    RETIRED    -> repeatedly failed; kept as a record, never leased.
    """
    DISCOVERED = "DISCOVERED"
    PROBING = "PROBING"
    READY = "READY"
    LEASED = "LEASED"
    COOLING = "COOLING"
    RETIRED = "RETIRED"


class InvalidProxy(ValueError):
    """Raised instead of returning None, so a bad parse can never be swallowed."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A validated host:port. Frozen so it can be a dict key and a fingerprint."""
    host: str
    port: int

    def __post_init__(self) -> None:
        if not self.host:
            raise InvalidProxy("empty host")
        if not (1 <= self.port <= 65535):
            raise InvalidProxy(f"port out of range: {self.port}")

    @classmethod
    def parse(cls, raw: str) -> Self:
        m = _HOSTPORT.match(raw or "")
        if not m:
            raise InvalidProxy(f"unparseable endpoint: {raw!r}")
        host, port = m.group("host"), int(m.group("port"))
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            # Hostnames are allowed, but must look like hostnames -- the legacy
            # regex accepted things like '1.2.3.4.5:80' as valid.
            if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?", host):
                raise InvalidProxy(f"not an IP or hostname: {host!r}") from exc
            labels = host.split(".")
            if any(not lab for lab in labels):
                raise InvalidProxy(f"empty label in host: {host!r}") from exc
            # A REAL hostname's rightmost label is never all digits. Without this,
            # the character-class above happily accepts a malformed dotted quad
            # such as '1.2.3.4.5' -- exactly the legacy defect this branch claims
            # to reject, and one that P01.T3 caught by actually asserting it.
            if len(labels) > 1 and labels[-1].isdigit():
                raise InvalidProxy(
                    f"malformed numeric address (not a valid IP or hostname): {host!r}"
                ) from exc
        return cls(host=host, port=port)

    @property
    def is_private(self) -> bool:
        """A 'proxy' on a private range is almost always a parse artifact."""
        try:
            return ipaddress.ip_address(self.host).is_private
        except ValueError:
            return False

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    """
    The distribution from k samples (ADR-003, k=5 by default).

    The legacy system stored ONE number and called it response_time. That single
    sample is what admitted a 19 035 ms proxy as a success identical to a 756 ms one.
    """
    samples_ms: tuple[float, ...] = ()
    p50_ms: float | None = None
    p95_ms: float | None = None
    mean_ms: float | None = None
    stdev_ms: float | None = None
    success_ratio: float | None = None   # successful samples / attempted

    @property
    def jitter(self) -> float | None:
        """stdev/p50 -- a proxy that is fast but erratic is not a good proxy."""
        if self.stdev_ms is None or not self.p50_ms:
            return None
        return self.stdev_ms / self.p50_ms

    @property
    def measured(self) -> bool:
        return self.p95_ms is not None


@dataclass(frozen=True, slots=True)
class Proxy:
    """
    A candidate or pool member. Immutable: transitions return a NEW Proxy, so a
    concurrent reader can never observe a half-updated record (B-05 was a
    read-modify-write race on shared mutable state).
    """
    endpoint: Endpoint
    protocol: Protocol = Protocol.UNKNOWN
    labelled_protocol: Protocol = Protocol.UNKNOWN   # what the source CLAIMED
    anonymity: Anonymity = Anonymity.UNKNOWN
    state: ProxyState = ProxyState.DISCOVERED
    latency: LatencyProfile = field(default_factory=LatencyProfile)
    source_id: str | None = None
    country: str | None = None
    consecutive_failures: int = 0
    total_successes: int = 0
    total_attempts: int = 0
    first_seen: datetime | None = None
    last_checked: datetime | None = None
    lease_id: str | None = None
    reason_code: str | None = None      # why it is in its current state

    # ── identity ──────────────────────────────────────────────────────────────
    @property
    def fingerprint(self) -> str:
        """
        Stable identity = endpoint + discovered protocol. The same host:port
        reachable over socks5 and http are genuinely different proxies.
        """
        raw = f"{self.endpoint}|{self.protocol.value}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def protocol_mismatch(self) -> bool:
        """ADR-005: emitted as PROTO_MISMATCH when the source's label was wrong."""
        return (
            self.labelled_protocol is not Protocol.UNKNOWN
            and self.protocol is not Protocol.UNKNOWN
            and self.labelled_protocol != self.protocol
        )

    @property
    def is_leasable(self) -> bool:
        return self.state is ProxyState.READY

    @property
    def success_rate(self) -> float | None:
        if not self.total_attempts:
            return None
        return self.total_successes / self.total_attempts

    # ── transitions (pure; each returns a new object) ──────────────────────────
    def with_state(self, state: ProxyState, *, reason: str | None = None) -> Proxy:
        return replace(self, state=state, reason_code=reason)

    def with_latency(self, latency: LatencyProfile) -> Proxy:
        return replace(self, latency=latency)

    def record_success(self, at: datetime) -> Proxy:
        return replace(self, consecutive_failures=0,
                       total_successes=self.total_successes + 1,
                       total_attempts=self.total_attempts + 1,
                       last_checked=at)

    def record_failure(self, at: datetime, *, reason: str) -> Proxy:
        """
        ADR-006: cooldown is driven by CONSECUTIVE failures, and the reason is
        always recorded. The legacy code swallowed the reason entirely (B-02).
        """
        return replace(self, consecutive_failures=self.consecutive_failures + 1,
                       total_attempts=self.total_attempts + 1,
                       last_checked=at, reason_code=reason)

    def leased_as(self, lease_id: str) -> Proxy:
        if self.state is not ProxyState.READY:
            raise InvalidProxy(
                f"cannot lease a proxy in state {self.state.value} (H3): {self.endpoint}"
            )
        return replace(self, state=ProxyState.LEASED, lease_id=lease_id)

    def released(self) -> Proxy:
        return replace(self, state=ProxyState.READY, lease_id=None)

    def __str__(self) -> str:
        return f"{self.protocol.value}://{self.endpoint} [{self.state.value}]"


def utc_now_placeholder() -> None:
    """
    Deliberately absent: core/ must not read the clock (that is ClockPort).
    datetime.now() here would make every policy test time-dependent.
    """
    raise NotImplementedError("use ClockPort; core/ does not read the clock")
