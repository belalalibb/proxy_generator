"""
DOMAIN BEHAVIOUR TESTS (P01.T3).

test_architecture.py proves core/ is *shaped* correctly. These prove it *behaves*
correctly. Every test pins an invariant to the specific legacy defect it prevents,
so the reason a rule exists cannot be lost.

All tests here are pure: no network, no clock, no filesystem.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.core.domain.proxy import (
    Endpoint, InvalidProxy, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import ParserKind, Source, SourceState, Target
from atlas.core.domain.verdict import Grade, ReasonCode, Score, Verdict
from atlas.core.ports.clock import cooldown_delay

T0 = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _proxy(**kw) -> Proxy:
    return Proxy(endpoint=Endpoint.parse("1.2.3.4:8080"), **kw)


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint — the legacy regex accepted garbage
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("raw,host,port", [
    ("1.2.3.4:8080", "1.2.3.4", 8080),
    ("  10.0.0.1:3128  ", "10.0.0.1", 3128),
    ("proxy.example.org:1080", "proxy.example.org", 1080),
])
def test_endpoint_parses_valid(raw: str, host: str, port: int) -> None:
    ep = Endpoint.parse(raw)
    assert (ep.host, ep.port) == (host, port)


@pytest.mark.parametrize("raw", [
    "1.2.3.4.5:80",        # over-long dotted quad
    "1.2.3.4:99999",       # port out of range
    "1.2.3.4:0",           # port 0
    "1.2.3.4",             # no port
    "",                    # empty
    ":8080",               # no host
])
def test_endpoint_rejects_malformed(raw: str) -> None:
    """
    RAISES, never returns a falsy value. A parse failure that returns None is how
    the legacy code silently discarded candidates (BUG_LEDGER B-02).
    """
    with pytest.raises(InvalidProxy):
        Endpoint.parse(raw)


def test_endpoint_detects_private_ranges() -> None:
    """A 'proxy' on a private range is almost always a parse artifact."""
    assert Endpoint.parse("192.168.1.1:8080").is_private
    assert Endpoint.parse("127.0.0.1:8080").is_private
    assert not Endpoint.parse("8.8.8.8:8080").is_private


def test_endpoint_is_hashable_and_frozen() -> None:
    ep = Endpoint.parse("1.2.3.4:80")
    assert len({ep, Endpoint.parse("1.2.3.4:80")}) == 1
    with pytest.raises((AttributeError, TypeError)):
        ep.port = 90        # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# LatencyProfile — ADR-003, the gate the legacy system never had
# ══════════════════════════════════════════════════════════════════════════════
def test_unmeasured_latency_is_representable() -> None:
    """
    'No evidence yet' must be expressible. The legacy system had no such state, so
    absence of measurement was indistinguishable from success (H7).
    """
    assert not LatencyProfile().measured
    assert LatencyProfile().jitter is None


def test_jitter_is_stdev_over_p50() -> None:
    p = LatencyProfile(samples_ms=(100, 110, 120), p50_ms=110.0, p95_ms=120.0,
                       stdev_ms=11.0)
    assert p.measured
    assert p.jitter == pytest.approx(0.1)


def test_jitter_guards_division_by_zero() -> None:
    assert LatencyProfile(p50_ms=0.0, p95_ms=1.0, stdev_ms=5.0).jitter is None


# ══════════════════════════════════════════════════════════════════════════════
# Proxy — identity, mismatch, and the H3 lease rule
# ══════════════════════════════════════════════════════════════════════════════
def test_fingerprint_is_stable_and_protocol_scoped() -> None:
    """
    The same host:port over socks5 and http are genuinely DIFFERENT proxies, so the
    fingerprint must include the discovered protocol.
    """
    assert _proxy(protocol=Protocol.HTTP).fingerprint == _proxy(protocol=Protocol.HTTP).fingerprint
    assert _proxy(protocol=Protocol.HTTP).fingerprint != _proxy(protocol=Protocol.SOCKS5).fingerprint


def test_protocol_mismatch_detects_mislabelled_source() -> None:
    """
    ADR-005 / B-12. A SOCKS list served under an http.txt name yielded thousands of
    candidates the legacy code tested as HTTP and discarded. Must be DETECTABLE.
    """
    assert _proxy(protocol=Protocol.SOCKS5, labelled_protocol=Protocol.HTTP).protocol_mismatch
    assert not _proxy(protocol=Protocol.HTTP, labelled_protocol=Protocol.HTTP).protocol_mismatch
    # UNKNOWN on either side is missing information, not a contradiction
    assert not _proxy(protocol=Protocol.SOCKS5,
                      labelled_protocol=Protocol.UNKNOWN).protocol_mismatch


def test_only_ready_proxies_can_be_leased() -> None:
    """
    H3 (no double delivery). proxy.txt had no LEASED state, so two concurrent
    consumers were always handed the same line (B-05).
    """
    leased = _proxy(state=ProxyState.READY).leased_as("lease-1")
    assert leased.state is ProxyState.LEASED
    assert leased.lease_id == "lease-1"

    for bad in (ProxyState.DISCOVERED, ProxyState.PROBING, ProxyState.LEASED,
                ProxyState.COOLING, ProxyState.RETIRED):
        with pytest.raises(InvalidProxy):
            _proxy(state=bad).leased_as("lease-2")


def test_is_leasable_agrees_with_leased_as() -> None:
    """The predicate and the transition must never disagree."""
    for st in ProxyState:
        p = _proxy(state=st)
        if p.is_leasable:
            assert p.leased_as("L").state is ProxyState.LEASED
        else:
            with pytest.raises(InvalidProxy):
                p.leased_as("L")


def test_transitions_are_immutable() -> None:
    """
    A transition returns a NEW object, so a concurrent reader can never observe a
    half-updated record. B-05 was a read-modify-write race on shared state.
    """
    original = _proxy(state=ProxyState.READY)
    moved = original.with_state(ProxyState.COOLING, reason="TOO_SLOW_P95")
    assert original.state is ProxyState.READY      # unchanged
    assert moved.state is ProxyState.COOLING
    assert moved.reason_code == "TOO_SLOW_P95"
    assert moved is not original


def test_record_failure_increments_consecutive_and_keeps_reason() -> None:
    """ADR-006: cooldown is driven by CONSECUTIVE failures, and the cause survives."""
    p = _proxy().record_failure(T0, reason="TCP_TIMEOUT").record_failure(T0, reason="TCP_TIMEOUT")
    assert (p.consecutive_failures, p.total_attempts) == (2, 2)
    assert p.reason_code == "TCP_TIMEOUT"


def test_record_success_resets_the_failure_streak() -> None:
    """One success clears the streak — a single failure must not retire a proxy."""
    p = _proxy().record_failure(T0, reason="TCP_TIMEOUT").record_success(T0)
    assert p.consecutive_failures == 0
    assert (p.total_successes, p.total_attempts) == (1, 2)
    assert p.success_rate == pytest.approx(0.5)


def test_success_rate_is_none_without_attempts() -> None:
    """No attempts means NO DATA — not 0 %, and not 100 %."""
    assert _proxy().success_rate is None


def test_release_returns_to_ready_and_clears_the_lease() -> None:
    p = _proxy(state=ProxyState.READY).leased_as("L1").released()
    assert p.state is ProxyState.READY
    assert p.lease_id is None


# ══════════════════════════════════════════════════════════════════════════════
# Verdict — a rejection must always name its reason
# ══════════════════════════════════════════════════════════════════════════════
def test_accept_carries_ok_reason() -> None:
    v = Verdict.accept(Grade.ELITE)
    assert v.admitted and v.reason is ReasonCode.OK


def test_reject_carries_a_real_reason() -> None:
    v = Verdict.reject(ReasonCode.TOO_SLOW_P95, "p95 6359ms > 1500ms")
    assert not v.admitted
    assert v.grade is Grade.REJECTED
    assert v.reason is ReasonCode.TOO_SLOW_P95


def test_verdict_forbids_contradictions() -> None:
    """
    The type enforces what the legacy bool could not express. B-02: 23 silent
    handlers meant a failure never carried its cause.
    """
    with pytest.raises(ValueError):
        Verdict(admitted=True, grade=Grade.GOOD, reason=ReasonCode.TOO_SLOW_P95)
    with pytest.raises(ValueError):
        Verdict(admitted=False, grade=Grade.GOOD, reason=ReasonCode.OK)
    with pytest.raises(ValueError):
        Verdict(admitted=True, grade=Grade.REJECTED, reason=ReasonCode.OK)


def test_fetch_incomplete_is_distinct_from_empty(  ) -> None:
    """
    ADR-013. An incomplete body is OUR fault and must be distinguishable from an
    empty source — conflating them is what misclassified a live JSON API.
    """
    assert ReasonCode.FETCH_INCOMPLETE is not ReasonCode.PARSE_EMPTY
    assert ReasonCode.FETCH_INCOMPLETE is not ReasonCode.SOURCE_THROTTLED


def test_not_measured_reason_exists() -> None:
    """H7: refuse to admit on zero evidence."""
    assert ReasonCode.NOT_MEASURED in set(ReasonCode)


# ══════════════════════════════════════════════════════════════════════════════
# Score
# ══════════════════════════════════════════════════════════════════════════════
def test_score_must_be_normalised() -> None:
    Score(value=0.0)
    Score(value=1.0)
    for bad in (-0.1, 1.1, 2.0):
        with pytest.raises(ValueError):
            Score(value=bad)


# ══════════════════════════════════════════════════════════════════════════════
# Target — H5 / ADR-007
# ══════════════════════════════════════════════════════════════════════════════
def test_target_requires_an_explicit_url() -> None:
    """
    There is NO default target. The legacy code defaulted to a login-walled site
    and probed it thousands of times per run (v1.py:29, v3.py:30).
    """
    assert Target(url="https://example.com").url == "https://example.com"
    with pytest.raises(TypeError):
        Target()            # type: ignore[call-arg]


# ══════════════════════════════════════════════════════════════════════════════
# cooldown — ADR-006, verified WITHOUT sleeping
# ══════════════════════════════════════════════════════════════════════════════
def test_cooldown_is_zero_before_any_failure() -> None:
    assert cooldown_delay(0) == timedelta(0)
    assert cooldown_delay(-1) == timedelta(0)


def test_cooldown_grows_exponentially() -> None:
    """delay = base * 2^(n-1). A single failure must never disable a source."""
    assert cooldown_delay(1, base_s=30) == timedelta(seconds=30)
    assert cooldown_delay(2, base_s=30) == timedelta(seconds=60)
    assert cooldown_delay(3, base_s=30) == timedelta(seconds=120)
    assert cooldown_delay(4, base_s=30) == timedelta(seconds=240)


def test_cooldown_is_capped() -> None:
    """Unbounded backoff is indistinguishable from deletion."""
    assert cooldown_delay(50, base_s=30, cap_s=3600) == timedelta(seconds=3600)
    assert cooldown_delay(999, base_s=30, cap_s=3600) == timedelta(seconds=3600)


def test_cooldown_needs_no_clock() -> None:
    """
    The point of ClockPort: this rule is verified in microseconds. The legacy tree
    called time.sleep() 13 times inside its control flow, so equivalent logic could
    only be tested by actually waiting.
    """
    import time
    t = time.monotonic()
    for n in range(500):
        cooldown_delay(n)
    assert time.monotonic() - t < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# Source — ADR-002
# ══════════════════════════════════════════════════════════════════════════════
def test_source_carries_declarative_parser_and_stats() -> None:
    s = Source(id="example-http", url="https://example.com/list.txt",
               parser=ParserKind.REGEX_ADJACENT)
    assert s.parser is ParserKind.REGEX_ADJACENT
    assert s.stats.consecutive_failures == 0
    assert s.state is SourceState.ACTIVE


def test_parser_kinds_are_exactly_the_measured_formats() -> None:
    """
    P00.T4: the adjacency regex found zero candidates in JSON APIs and HTML tables.
    Structured parsers recovered 6 sources in the 2026-08-24 snapshot.

    ADR-017 -- this asserts EQUALITY, not a subset. The old version used `<=`, so
    it happily passed while the enum carried three speculative kinds (csv_columns,
    regex, line_ipport) that no parser implemented and no source used, AND while
    the enum lacked `regex_adjacent`, the value 59 of 67 ENABLED rows actually
    carry. A subset assertion cannot detect a vocabulary that is simultaneously
    too large and missing the one member that matters.
    """
    kinds = {k.value for k in ParserKind}
    assert kinds == {"regex_adjacent", "json_path", "html_table"}, (
        "ParserKind must name exactly the parsers that exist in "
        f"atlas.core.parsing; got {sorted(kinds)}"
    )
