"""
Scoring tests — the B-16 ageing rule, and the "unknown is not good" inversion.

These tests are written against the measured failure they exist to prevent:
re-testing the legacy proxy.txt today gives 3.0% live (9/300), because that file
cannot tell a proxy verified 8 seconds ago from one verified last year. So the
decay behaviour is asserted directly, with fixed timestamps, rather than assumed
from the presence of a `freshness` field.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade, Score
from atlas.core.policy.scoring import (
    ScoringPolicy, anonymity_term, freshness_term, is_stale, latency_term,
    rank, reliability_term, score_proxy,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def mkproxy(
    *,
    host: str = "203.0.113.7",
    port: int = 8080,
    p95: float | None = 500.0,
    successes: int = 9,
    attempts: int = 10,
    age_s: float | None = 0.0,
    anonymity: Anonymity = Anonymity.ELITE,
    grade: Grade = Grade.GOOD,
) -> Proxy:
    """A proxy built from explicit facts; every default is a GOOD proxy."""
    samples = (p95,) if p95 is not None else ()
    return Proxy(
        endpoint=Endpoint(host=host, port=port),
        protocol=Protocol.HTTP,
        anonymity=anonymity,
        state=ProxyState.READY,
        latency=LatencyProfile(
            samples_ms=samples,
            p50_ms=p95,
            p95_ms=p95,
            success_ratio=(successes / attempts) if attempts else None,
        ),
        total_successes=successes,
        total_attempts=attempts,
        last_checked=(NOW - timedelta(seconds=age_s)) if age_s is not None else None,
        grade=grade,
    )


# ── policy validation ─────────────────────────────────────────────────────────
def test_weights_must_sum_to_one():
    """An unnormalised policy makes Score.value uninterpretable."""
    with pytest.raises(ValueError, match="sum to 1.0"):
        ScoringPolicy(w_latency=0.9, w_reliability=0.9,
                      w_freshness=0.9, w_anonymity=0.9)


def test_default_policy_weights_are_normalised():
    p = ScoringPolicy()
    assert math.isclose(
        p.w_latency + p.w_reliability + p.w_freshness + p.w_anonymity, 1.0
    )


def test_negative_weight_is_refused():
    with pytest.raises(ValueError, match="non-negative"):
        ScoringPolicy(w_latency=-0.1, w_reliability=0.4,
                      w_freshness=0.4, w_anonymity=0.3)


def test_max_age_below_half_life_is_refused():
    """
    A proxy would be declared stale before its freshness had even halved --
    the two constants would be describing incompatible lifetimes.
    """
    with pytest.raises(ValueError, match="stale before"):
        ScoringPolicy(freshness_half_life_s=900.0, max_age_s=300.0)


# ── B-16: THE AGEING RULE ─────────────────────────────────────────────────────
def test_freshness_halves_after_one_half_life():
    """The decay is the actual measured curve, not merely a field that exists."""
    pol = ScoringPolicy(freshness_half_life_s=900.0, max_age_s=3600.0)
    assert freshness_term(mkproxy(age_s=0), pol, now=NOW) == pytest.approx(1.0)
    assert freshness_term(mkproxy(age_s=900), pol, now=NOW) == pytest.approx(0.5)
    assert freshness_term(mkproxy(age_s=1800), pol, now=NOW) == pytest.approx(0.25)


def test_freshness_is_monotonically_decreasing_in_age():
    """Older is never fresher. Asserted across the whole supported range."""
    pol = ScoringPolicy()
    ages = [0, 1, 10, 60, 300, 900, 1799, 1800, 3000, 3599, 3600, 7200]
    vals = [freshness_term(mkproxy(age_s=a), pol, now=NOW) for a in ages]
    for younger, older in zip(vals, vals[1:]):
        assert older <= younger, f"freshness increased with age: {vals}"


def test_a_proxy_older_than_max_age_has_zero_freshness():
    pol = ScoringPolicy(max_age_s=3600.0)
    assert freshness_term(mkproxy(age_s=3601), pol, now=NOW) == 0.0


def test_a_never_checked_proxy_is_the_least_fresh_not_the_most():
    """
    THE B-16 INVERSION. A missing timestamp means UNVERIFIED, and proxy.txt's
    97%-dead list is what happens when that reads as "just checked".
    """
    pol = ScoringPolicy()
    never = mkproxy(age_s=None)
    assert never.last_checked is None
    assert freshness_term(never, pol, now=NOW) == 0.0
    assert is_stale(never, pol, now=NOW) is True


def test_stale_proxy_is_flagged_for_reverification():
    pol = ScoringPolicy(max_age_s=3600.0)
    assert is_stale(mkproxy(age_s=3599), pol, now=NOW) is False
    assert is_stale(mkproxy(age_s=3600), pol, now=NOW) is True


def test_an_identical_proxy_scores_strictly_lower_as_it_ages():
    """
    The whole point of B-16, stated as a comparison: two proxies identical in
    every other respect must NOT rank equally when one was checked an hour ago.
    """
    pol = ScoringPolicy()
    fresh = score_proxy(mkproxy(age_s=0), pol, now=NOW)
    old = score_proxy(mkproxy(age_s=1800), pol, now=NOW)
    assert fresh.value > old.value
    assert fresh.freshness_component > old.freshness_component
    # and the OTHER components are untouched: freshness alone moved.
    assert fresh.speed_component == old.speed_component
    assert fresh.reliability_component == old.reliability_component


def test_clock_skew_cannot_score_above_a_just_checked_proxy():
    """A future timestamp must not buy a score above the 1.0 ceiling."""
    pol = ScoringPolicy()
    future = mkproxy(age_s=-600)          # last_checked 10 min in the FUTURE
    assert freshness_term(future, pol, now=NOW) == 1.0
    assert is_stale(future, pol, now=NOW) is False


# ── unknown scores zero, never a flattering default ───────────────────────────
def test_an_unmeasured_proxy_scores_zero_latency_not_neutral():
    """
    Same inversion as admission.NOT_MEASURED: absence of evidence is not
    permission, and must not be a middling 0.5 that outranks a measured proxy.
    """
    pol = ScoringPolicy()
    assert latency_term(mkproxy(p95=None), pol) == 0.0


def test_an_unmeasured_proxy_never_outranks_a_slow_measured_one():
    pol = ScoringPolicy()
    unmeasured = score_proxy(mkproxy(p95=None), pol, now=NOW)
    slow = score_proxy(mkproxy(p95=1400.0), pol, now=NOW)
    assert slow.value > unmeasured.value


def test_a_proxy_with_no_attempts_has_zero_reliability():
    assert reliability_term(mkproxy(successes=0, attempts=0)) == 0.0


def test_transparent_and_unknown_anonymity_both_score_zero():
    """Known-leaking and unproven rank the same: neither is rewarded."""
    assert anonymity_term(mkproxy(anonymity=Anonymity.TRANSPARENT)) == 0.0
    assert anonymity_term(mkproxy(anonymity=Anonymity.UNKNOWN)) == 0.0
    assert anonymity_term(mkproxy(anonymity=Anonymity.ELITE)) == 1.0


# ── latency term ──────────────────────────────────────────────────────────────
def test_latency_term_is_zero_at_and_beyond_the_budget():
    pol = ScoringPolicy(latency_budget_ms=1500.0)
    assert latency_term(mkproxy(p95=1500.0), pol) == 0.0
    assert latency_term(mkproxy(p95=9000.0), pol) == 0.0


def test_latency_term_uses_p95_not_the_best_sample():
    """
    A proxy admitted on its TAIL must not be ranked on its best case -- that is
    the `min` defect (§8) reappearing in the ranking instead of the gate.
    """
    pol = ScoringPolicy(latency_budget_ms=1500.0)
    p = Proxy(
        endpoint=Endpoint(host="203.0.113.9", port=3128),
        latency=LatencyProfile(
            samples_ms=(100.0, 200.0, 1400.0),
            p50_ms=200.0, p95_ms=1400.0, success_ratio=1.0,
        ),
        last_checked=NOW, grade=Grade.USABLE,
    )
    # 1 - 1400/1500 == 0.0667 (from p95), NOT 1 - 100/1500 == 0.933 (from min)
    assert latency_term(p, pol) == pytest.approx(1.0 - 1400.0 / 1500.0)


# ── the composite ─────────────────────────────────────────────────────────────
def test_score_is_within_zero_and_one_and_components_are_retained():
    pol = ScoringPolicy()
    s = score_proxy(mkproxy(), pol, now=NOW)
    assert isinstance(s, Score)
    assert 0.0 <= s.value <= 1.0
    # every component present: a bare float cannot be argued with (B-02).
    assert s.speed_component > 0.0
    assert s.reliability_component > 0.0
    assert s.freshness_component > 0.0
    assert s.anonymity_component > 0.0


def test_the_worst_possible_proxy_scores_zero_and_does_not_raise():
    """
    All-unknown must be representable. If it raised, an anomalous record would
    take down the serving path instead of simply ranking last.
    """
    pol = ScoringPolicy()
    worst = mkproxy(p95=None, successes=0, attempts=0, age_s=None,
                    anonymity=Anonymity.UNKNOWN)
    assert score_proxy(worst, pol, now=NOW).value == 0.0


def test_the_best_possible_proxy_scores_one():
    pol = ScoringPolicy()
    best = mkproxy(p95=0.0, successes=10, attempts=10, age_s=0,
                   anonymity=Anonymity.ELITE)
    assert score_proxy(best, pol, now=NOW).value == pytest.approx(1.0)


def test_score_equals_the_declared_weighted_sum():
    """
    Recompute the arithmetic independently: the docstring's formula and the code
    must be the same thing, not merely adjacent.
    """
    pol = ScoringPolicy()
    p = mkproxy(p95=750.0, successes=8, attempts=10, age_s=900,
                anonymity=Anonymity.ANONYMOUS)
    s = score_proxy(p, pol, now=NOW)
    expected = (pol.w_latency * (1.0 - 750.0 / pol.latency_budget_ms)
                + pol.w_reliability * 0.8
                + pol.w_freshness * 0.5
                + pol.w_anonymity * 0.7)
    assert s.value == pytest.approx(expected)


def test_a_fast_unreliable_proxy_ranks_below_a_slower_reliable_one():
    """
    The distinction the legacy pool could not express, because it stored ONE
    number per proxy.
    """
    pol = ScoringPolicy()
    fast_flaky = score_proxy(
        mkproxy(p95=200.0, successes=4, attempts=10), pol, now=NOW)
    steady = score_proxy(
        mkproxy(p95=900.0, successes=10, attempts=10), pol, now=NOW)
    assert steady.value > fast_flaky.value


# ── rank() ────────────────────────────────────────────────────────────────────
def test_rank_orders_best_first_and_filters_by_grade():
    pol = ScoringPolicy()
    good = mkproxy(host="203.0.113.1", p95=300.0, grade=Grade.ELITE)
    mediocre = mkproxy(host="203.0.113.2", p95=1400.0, grade=Grade.USABLE)
    rejected = mkproxy(host="203.0.113.3", p95=300.0, grade=Grade.REJECTED)

    out = rank((mediocre, rejected, good), pol, now=NOW, min_grade=Grade.USABLE)
    assert [p.endpoint.host for p, _ in out] == ["203.0.113.1", "203.0.113.2"]
    # REJECTED is excluded regardless of how fast it is.
    assert all(p.grade is not Grade.REJECTED for p, _ in out)


def test_rank_excludes_stale_proxies_by_default():
    """
    Decay alone is not enough: "ranks last" still gets served when the pool is
    thin, and 97% of the legacy list was stale.
    """
    pol = ScoringPolicy(max_age_s=3600.0)
    fresh = mkproxy(host="203.0.113.1", age_s=0)
    stale = mkproxy(host="203.0.113.2", age_s=7200)

    assert len(rank((fresh, stale), pol, now=NOW)) == 1
    assert len(rank((fresh, stale), pol, now=NOW, include_stale=True)) == 2


def test_rank_is_deterministic_for_tied_scores():
    """
    A total order. Otherwise the rotation-fairness simulation cannot tell real
    bias from arbitrary store ordering.
    """
    pol = ScoringPolicy()
    a = mkproxy(host="203.0.113.1")
    b = mkproxy(host="203.0.113.2")
    c = mkproxy(host="203.0.113.3")
    first = [p.fingerprint for p, _ in rank((a, b, c), pol, now=NOW)]
    second = [p.fingerprint for p, _ in rank((c, a, b), pol, now=NOW)]
    third = [p.fingerprint for p, _ in rank((b, c, a), pol, now=NOW)]
    assert first == second == third


def test_rank_of_an_empty_pool_is_empty_not_an_error():
    assert rank((), ScoringPolicy(), now=NOW) == ()
