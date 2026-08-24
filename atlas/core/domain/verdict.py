"""
Verdict + Score + ReasonCode — PURE. The heart of the LIVE != GOOD rule (H7).

THE CENTRAL PHASE-0 FINDING (engineering/BASELINE.json, engineering/ANALYSIS.md):
the legacy gate was `status == 200 and len(text) > 1000` on a single sample. A
mechanical scan found latency measured **59 times** in the legacy tree and compared
against a rejecting threshold **zero times**. What that admitted, from its own
recorded run (n=102):

    p50 6 359.5 ms | mean 7 145.1 ms | p95 15 903 ms | max 19 035 ms
    95.8% of ACCEPTED proxies were slower than 1 500 ms; 56.8% slower than 5 000 ms.

A 19-second proxy was recorded as a success identical to a 756 ms one. Admission
here is therefore a graded Verdict with an explicit reason, never a bool.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReasonCode(str, Enum):
    """
    Every rejection names itself. The legacy code had 23 silent handlers
    (`except ...: pass|continue|return <falsy>`), which is why 35 dead URLs were
    retried forever and no failure was ever diagnosable (BUG_LEDGER B-02).
    """
    # accepted
    OK = "OK"
    # liveness
    TCP_REFUSED = "TCP_REFUSED"
    TCP_TIMEOUT = "TCP_TIMEOUT"
    DNS_FAILED = "DNS_FAILED"
    TLS_FAILED = "TLS_FAILED"
    PROXY_AUTH_REQUIRED = "PROXY_AUTH_REQUIRED"
    BAD_STATUS = "BAD_STATUS"
    BODY_TOO_SMALL = "BODY_TOO_SMALL"
    # quality -- the gate the legacy system did not have
    TOO_SLOW_P95 = "TOO_SLOW_P95"
    TOO_JITTERY = "TOO_JITTERY"
    UNRELIABLE = "UNRELIABLE"          # success_ratio below floor across k samples
    NOT_MEASURED = "NOT_MEASURED"      # refuse to admit on zero evidence
    # integrity
    CONTENT_MISMATCH = "CONTENT_MISMATCH"   # body differs => interception suspected
    TRANSPARENT_LEAK = "TRANSPARENT_LEAK"   # forwards client IP
    PROTO_MISMATCH = "PROTO_MISMATCH"       # ADR-005; source label was wrong
    # source-level (ADR-006)
    SOURCE_THROTTLED = "SOURCE_THROTTLED"   # short body != empty source
    SOURCE_UNCHANGED = "SOURCE_UNCHANGED"   # ETag/If-Modified-Since hit
    SOURCE_DEAD = "SOURCE_DEAD"
    PARSE_EMPTY = "PARSE_EMPTY"
    FETCH_INCOMPLETE = "FETCH_INCOMPLETE"
    """
    ADR-013: the body did not arrive intact (short read vs Content-Length, or the
    size cap was hit). This is OUR fault, not the source's, and it is a DIFFERENT
    fact from PARSE_EMPTY.

    Earned the hard way: a rebuilt audit tool used aiohttp's
    `resp.content.read(n)`, which returns only what is currently BUFFERED, and so
    read 74 241 of 230 067 bytes. The truncated JSON would not parse, and a live
    source yielding 500 proxies was filed empty -- the third misclassification of
    the same source, from a second distinct cause.

    A candidate carrying this code must be RE-FETCHED before any verdict is
    recorded against it.
    """


class Grade(str, Enum):
    """Graded, not binary -- so 'live but useless' is representable."""
    ELITE = "ELITE"
    GOOD = "GOOD"
    USABLE = "USABLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of the admission gate. `admitted` NEVER stands alone."""
    admitted: bool
    grade: Grade
    reason: ReasonCode
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.admitted and self.reason is not ReasonCode.OK:
            raise ValueError(
                f"an admitted proxy cannot carry rejection reason {self.reason.value}"
            )
        if not self.admitted and self.reason is ReasonCode.OK:
            raise ValueError("a rejected proxy must name a reason (BUG_LEDGER B-02)")
        if self.admitted and self.grade is Grade.REJECTED:
            raise ValueError("contradiction: admitted with grade REJECTED")

    @classmethod
    def accept(cls, grade: Grade, detail: str | None = None) -> Verdict:
        if grade is Grade.REJECTED:
            raise ValueError("use Verdict.reject() for a rejection")
        return cls(admitted=True, grade=grade, reason=ReasonCode.OK, detail=detail)

    @classmethod
    def reject(cls, reason: ReasonCode, detail: str | None = None) -> Verdict:
        if reason is ReasonCode.OK:
            raise ValueError("a rejection needs a real reason code")
        return cls(admitted=False, grade=Grade.REJECTED, reason=reason, detail=detail)

    def __str__(self) -> str:
        return (f"{'ADMIT' if self.admitted else 'REJECT'} "
                f"{self.grade.value} ({self.reason.value})")


@dataclass(frozen=True, slots=True)
class Score:
    """
    Ranking signal for READY proxies. Deliberately NOT just speed: a fast proxy
    that fails 40% of the time is worse than a slightly slower reliable one, and
    the legacy pool could not express that distinction at all.
    """
    value: float
    speed_component: float = 0.0
    reliability_component: float = 0.0
    anonymity_component: float = 0.0
    freshness_component: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.value <= 1.0):
            raise ValueError(f"score must be within 0..1, got {self.value}")

    def __lt__(self, other: Score) -> bool:
        return self.value < other.value
