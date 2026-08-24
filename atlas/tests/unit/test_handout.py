"""
HAND-OUT tests — the layer above the proven lease (ADR-033).

These tests exist to pin four things the lease itself cannot express:

  1. the caller's target is validated BEFORE any row leaves the pool (ADR-029),
  2. the four-term P07 score decides WHO is served, not just the order
     (the store's SQL orders by p95 alone),
  3. every leased row is granted or released -- no capacity leak,
  4. evidence older than the 900 s recheck horizon is REPORTED, not silently
     presented as current (ADR-035; the 90 s per-target TTL P08 flagged against
     is WITHDRAWN, because the schema cannot express per-target validity).

The store is a fake, so all of this runs without SQLite. H3 itself is NOT
retested here: it belongs to the store and is proven under real process
concurrency in atlas/tests/integration/test_store_lease.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade
from atlas.core.policy.scoring import ScoringPolicy
from atlas.core.policy.target_policy import TargetNotAllowed, TargetPolicy
from atlas.engine.handout import (
    Granted, HandoutPolicy, HandoutRefusal, HandoutResult, HandoutService,
    HandoutUnavailable, require_handout,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

# The deny-list is supplied as DATA, never imported from code: ADR-031 makes the
# field required precisely so a test cannot get an empty policy by omission.
POLICY = TargetPolicy(deny_hosts=frozenset({"instagram.com", "facebook.com"}))
GOOD_TARGET = Target(url="https://example.com")


class FakeClock:
    """A clock that does not move unless a test moves it (ClockPort)."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> float:
        return self._now.timestamp() * 1000.0

    def deadline(self, after_ms: float) -> datetime:
        return self._now + timedelta(milliseconds=after_ms)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeStore:
    """
    An in-memory stand-in for the lease subset of StorePort.

    Records every call so the tests can assert on the INTERACTION -- "was the
    target checked before the lease?" is a question about ordering, and only a
    recording fake can answer it.
    """

    def __init__(self, pool: list[Proxy] | None = None,
                 *, expired_to_reclaim: int = 0) -> None:
        self.pool = list(pool or [])
        self.calls: list[str] = []
        self.leased_out: dict[str, Proxy] = {}
        self.released: list[str] = []
        self._expired = expired_to_reclaim

    def lease(self, *, count: int, min_grade: Grade, lease_ms: int,
              now: datetime) -> tuple[Proxy, ...]:
        self.calls.append(f"lease(count={count})")
        allowed = Grade.at_least(min_grade)
        eligible = [p for p in self.pool if p.grade in allowed]
        # Mirror the real store's ordering: p95 ASC, NULLs last. The whole point
        # of ADR-033 is that this order is NOT the score order.
        eligible.sort(key=lambda p: (p.latency.p95_ms is None,
                                     p.latency.p95_ms or 0.0, p.fingerprint))
        taken = eligible[:count]
        for p in taken:
            self.pool.remove(p)
            self.leased_out[p.fingerprint] = p
        return tuple(taken)

    def release(self, fingerprint: str, *, now: datetime) -> None:
        self.calls.append(f"release({fingerprint})")
        self.released.append(fingerprint)
        p = self.leased_out.pop(fingerprint, None)
        if p is not None:
            self.pool.append(p)

    def expire_leases(self, *, now: datetime) -> int:
        self.calls.append("expire_leases")
        n, self._expired = self._expired, 0
        return n


def mkproxy(
    *,
    host: str = "203.0.113.7",
    port: int = 8080,
    p95: float | None = 500.0,
    successes: int = 9,
    attempts: int = 10,
    age_s: float | None = 10.0,
    anonymity: Anonymity = Anonymity.ELITE,
    grade: Grade = Grade.GOOD,
) -> Proxy:
    """A READY proxy built from explicit facts; every default is a good one."""
    return Proxy(
        endpoint=Endpoint(host=host, port=port),
        protocol=Protocol.HTTP,
        anonymity=anonymity,
        state=ProxyState.READY,
        latency=LatencyProfile(
            samples_ms=(p95,) * 5, p50_ms=p95, p95_ms=p95, success_ratio=1.0,
        ) if p95 is not None else LatencyProfile(),
        total_successes=successes,
        total_attempts=attempts,
        last_checked=None if age_s is None else NOW - timedelta(seconds=age_s),
        grade=grade,
    )


def service(store: FakeStore, *, clock: FakeClock | None = None,
            policy: HandoutPolicy | None = None,
            scoring: ScoringPolicy | None = None) -> HandoutService:
    return HandoutService(
        store=store, clock=clock or FakeClock(),
        target_policy=POLICY, policy=policy, scoring=scoring,
    )


# ── ADR-007 / ADR-029: the target is checked, on the serving path ─────────────
class TestTargetIsValidatedAtLeaseTime:
    """
    ADR-029 built the policy; until P08 nothing on the serving path called it.
    These tests are the difference between a policy and a comment.
    """

    def test_no_target_is_refused(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(target=None, count=1)
        assert res.refusal == HandoutRefusal.TARGET_REFUSED
        assert res.target_refusal == "NO_TARGET"
        assert res.granted == ()

    def test_the_legacy_default_target_is_refused(self):
        """The exact host the legacy code defaulted to (v1.py:29, v3.py:30)."""
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="https://instagram.com/explore"), count=1,
        )
        assert res.refusal == HandoutRefusal.TARGET_REFUSED
        assert res.target_refusal == "DENIED_HOST"

    def test_a_denied_subdomain_is_refused(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="https://graph.instagram.com/v1"), count=1,
        )
        assert res.target_refusal == "DENIED_HOST"

    def test_a_lookalike_host_is_not_refused(self):
        """
        The false-positive direction. A deny-list that refuses everything would
        pass every test above; this one fails unless matching is on label
        boundaries.
        """
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="https://notinstagram.com"), count=1,
        )
        assert res.refusal is None
        assert len(res.granted) == 1

    def test_an_ssrf_target_is_refused(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="http://169.254.169.254/latest/meta-data"),
            count=1,
        )
        assert res.refusal == HandoutRefusal.TARGET_REFUSED
        assert res.target_refusal == "NOT_GLOBALLY_ROUTABLE"

    def test_nothing_is_leased_when_the_target_is_refused(self):
        """
        THE ORDERING PROPERTY. A denied caller must not be able to pull rows out
        of the pool -- otherwise a refused request degrades service for
        permitted ones.
        """
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="https://facebook.com"), count=1,
        )
        assert res.refusal == HandoutRefusal.TARGET_REFUSED
        assert store.calls == []          # not even expire_leases
        assert res.leased == 0
        assert len(store.pool) == 1


# ── ADR-033: the score, not the SQL order, decides who is served ─────────────
class TestScoreDecidesSelection:
    def test_the_fastest_proxy_can_lose_to_a_better_one(self):
        """
        THE ADR-033 TEST.

        `fast` has the best p95, so the store hands it out first and a one-term
        selection would grant it. But it is TRANSPARENT (leaks the client IP)
        and unreliable (1/10). `slower` is ELITE and 10/10. The four-term score
        must prefer `slower`.

        If over-selection were removed, `fast` would be the only leased row and
        this test would fail -- which is exactly what it is for.
        """
        fast = mkproxy(host="198.51.100.1", p95=50.0, successes=1, attempts=10,
                       anonymity=Anonymity.TRANSPARENT)
        slower = mkproxy(host="203.0.113.9", p95=400.0, successes=10,
                         attempts=10, anonymity=Anonymity.ELITE)
        store = FakeStore([fast, slower])

        res = service(store).handout(target=GOOD_TARGET, count=1)

        assert len(res.granted) == 1
        assert res.granted[0].proxy.endpoint.host == "203.0.113.9"
        # and the loser went straight back to the pool
        assert res.released_surplus == 1
        assert fast.fingerprint in store.released

    def test_granted_order_is_best_first(self):
        best = mkproxy(host="203.0.113.1", p95=100.0, successes=10, attempts=10)
        mid = mkproxy(host="203.0.113.2", p95=600.0, successes=8, attempts=10)
        worst = mkproxy(host="203.0.113.3", p95=1400.0, successes=5,
                        attempts=10, anonymity=Anonymity.ANONYMOUS)
        store = FakeStore([worst, best, mid])

        res = service(store).handout(target=GOOD_TARGET, count=3)

        values = [g.score.value for g in res.granted]
        assert values == sorted(values, reverse=True)
        assert res.granted[0].proxy.endpoint.host == "203.0.113.1"

    def test_the_score_is_returned_not_just_the_ordering(self):
        """A caller must be able to see WHY a proxy ranked where it did."""
        store = FakeStore([mkproxy()])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        g = res.granted[0]
        assert 0.0 <= g.score.value <= 1.0
        assert g.score.anonymity_component == 1.0      # ELITE
        assert g.score.reliability_component == pytest.approx(0.9)

    def test_overselect_is_bounded_by_the_row_cap(self):
        pool = [mkproxy(host=f"203.0.113.{i}") for i in range(1, 30)]
        store = FakeStore(pool)
        # max_count must not exceed the row cap, or the policy is self-defeating
        # -- HandoutPolicy refuses that combination, which is asserted separately
        # in test_a_row_cap_below_max_count_is_rejected.
        pol = HandoutPolicy(max_count=10, overselect=10, max_overselect_rows=12)
        service(store, policy=pol).handout(target=GOOD_TARGET, count=2)
        # 2 * 10 = 20 would be requested, but the cap holds it at 12
        assert "lease(count=12)" in store.calls

    def test_overselect_one_disables_score_selection(self):
        """
        The honest negative control for ADR-033: with over-selection OFF, the
        store's p95 order wins and the transparent-but-fast proxy IS served.
        Documented because a caller may choose the cheaper query, and the cost
        of that choice should be visible in the test suite rather than implied.
        """
        fast = mkproxy(host="198.51.100.1", p95=50.0, successes=1, attempts=10,
                       anonymity=Anonymity.TRANSPARENT)
        slower = mkproxy(host="203.0.113.9", p95=400.0, successes=10,
                         attempts=10)
        store = FakeStore([fast, slower])
        pol = HandoutPolicy(overselect=1)
        res = service(store, policy=pol).handout(target=GOOD_TARGET, count=1)
        assert res.granted[0].proxy.endpoint.host == "198.51.100.1"


# ── the capacity leak this module exists to close ────────────────────────────
class TestNoCapacityLeak:
    def test_every_leased_row_is_granted_or_released(self):
        pool = [mkproxy(host=f"203.0.113.{i}") for i in range(1, 10)]
        store = FakeStore(pool)
        res = service(store).handout(target=GOOD_TARGET, count=2)
        # HandoutResult.__post_init__ enforces the identity; assert the numbers
        # too, so a future change to the check cannot make this vacuous.
        assert res.leased == 6                       # count 2 * overselect 3
        assert len(res.granted) == 2
        assert res.released_surplus + res.released_unusable == 4

    def test_the_accounting_identity_is_enforced(self):
        """A result that loses a row must be unconstructable."""
        with pytest.raises(ValueError, match="lease accounting lost rows"):
            HandoutResult(granted=(), leased=3, released_surplus=1)

    def test_surplus_is_released_even_if_ranking_raises(self, monkeypatch):
        """
        The `finally` property. Without it, an exception between lease and grant
        strands every leased row until its deadline -- a self-inflicted outage
        that no error message would name.
        """
        pool = [mkproxy(host=f"203.0.113.{i}") for i in range(1, 7)]
        store = FakeStore(pool)
        svc = service(store)

        import atlas.engine.handout as handout_mod

        def boom(*a, **k):
            raise RuntimeError("ranking blew up")

        monkeypatch.setattr(handout_mod, "rank", boom)

        with pytest.raises(RuntimeError, match="ranking blew up"):
            svc.handout(target=GOOD_TARGET, count=2)

        # all 6 leased rows were handed back
        assert len(store.released) == 6
        assert len(store.leased_out) == 0

    def test_release_all_returns_every_granted_proxy(self):
        pool = [mkproxy(host=f"203.0.113.{i}") for i in range(1, 10)]
        store = FakeStore(pool)
        svc = service(store)
        res = svc.handout(target=GOOD_TARGET, count=3)
        n = svc.release_all(res)
        assert n == 3
        for fp in res.fingerprints:
            assert fp in store.released

    def test_report_failure_returns_the_proxy_to_the_pool(self):
        store = FakeStore([mkproxy()])
        svc = service(store)
        res = svc.handout(target=GOOD_TARGET, count=1)
        fp = res.granted[0].fingerprint
        svc.report_failure(fp)
        assert fp in store.released


# ── B-16: the 90 s target TTL is reported, never silently ignored ────────────
class TestFreshnessIsReported:
    def test_fresh_evidence_is_not_flagged(self):
        store = FakeStore([mkproxy(age_s=10.0)])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        assert res.granted[0].past_recheck_horizon is False
        assert res.past_recheck_horizon == 0
        assert res.granted[0].age_s == pytest.approx(10.0)

    def test_evidence_past_the_recheck_horizon_is_flagged(self):
        """
        The flag means THE SCHEDULER IS BEHIND ON THIS ROW (ADR-035), keyed to
        `recheck_horizon_s` = 900 s. The proxy is still handed out -- refusing
        would serve almost nothing -- but the caller learns the evidence is
        older than the interval this system drives, instead of inferring
        currency from silence.
        """
        store = FakeStore([mkproxy(age_s=1200.0)])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        assert len(res.granted) == 1
        assert res.granted[0].past_recheck_horizon is True
        assert res.past_recheck_horizon == 1

    def test_the_deleted_90s_ttl_no_longer_flags_anything(self):
        """
        THE WITHDRAWN CLAIM, pinned so it cannot creep back (ADR-035).

        A proxy checked 300 s ago was `revalidation_required` under P08's 90 s
        per-target TTL. That TTL is withdrawn -- the schema holds ONE
        `last_checked` per proxy, so "validated against YOUR target 90 s ago" is
        unsayable at any interval -- so 300 s must now report CLEAN.

        The `hasattr` assertions are the actual regression guard: they fail if
        anyone reintroduces the old field name alongside the new one, which is
        how a withdrawn guarantee would quietly return.
        """
        store = FakeStore([mkproxy(age_s=300.0)])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        assert res.granted[0].past_recheck_horizon is False
        assert res.past_recheck_horizon == 0
        assert not hasattr(res, "revalidation_required")
        assert not hasattr(res.granted[0], "revalidation_required")

    def test_the_horizon_boundary_is_not_off_by_one(self):
        under = FakeStore([mkproxy(age_s=899.9)])
        over = FakeStore([mkproxy(age_s=900.1)])
        assert service(under).handout(
            target=GOOD_TARGET, count=1).past_recheck_horizon == 0
        assert service(over).handout(
            target=GOOD_TARGET, count=1).past_recheck_horizon == 1

    def test_never_checked_counts_as_past_the_horizon_not_inside_it(self):
        """
        Tested through the REAL predicate, which is the whole point.

        The first version of this test restated `age_s is None or age_s > ...`
        inline and asserted on its own copy. It passed while the mutation run
        still reported `never_checked_treated_as_fresh_via_comparison` as a
        SURVIVOR -- a test that re-implements the code under test measures the
        test. `_past_horizon` was extracted so this can call the real branch.

        Unreachable via `handout()` today (`rank(include_stale=False)` drops
        never-checked rows first), so it is pinned here rather than left
        unproven or deleted.
        """
        svc = service(FakeStore([]))
        assert svc._past_horizon(None) is True
        assert svc._past_horizon(10.0) is False
        assert svc._past_horizon(1200.0) is True

    def test_a_horizon_at_or_above_max_age_is_refused_at_construction(self):
        """
        A hole neither policy could see (ADR-035).

        `rank(include_stale=False)` drops rows at or past `max_age_s`, so the
        flag is only observable in (recheck_horizon_s, max_age_s). Configure the
        horizon at or above `max_age_s` and that band is EMPTY: the flag can
        never fire and every served row reports fresh at any age -- staleness
        reported as freshness, by CONFIGURATION alone.

        Both dataclasses validate happily in isolation, so the guard can only
        live where both are known: `HandoutService.__init__`.
        """
        with pytest.raises(ValueError, match="could never|never fire"):
            service(
                FakeStore([]),
                policy=HandoutPolicy(recheck_horizon_s=3600.0),
                scoring=ScoringPolicy(max_age_s=3600.0),
            )

    def test_just_below_max_age_is_accepted_and_the_flag_is_observable(self):
        """
        The other side of the same boundary: 3599 is legal, and the flag must
        actually FIRE there. Asserting only that construction succeeds would
        pass even if the observable band were empty.
        """
        svc = service(
            FakeStore([mkproxy(age_s=3599.5)]),
            policy=HandoutPolicy(recheck_horizon_s=3599.0),
            scoring=ScoringPolicy(max_age_s=3600.0),
        )
        res = svc.handout(target=GOOD_TARGET, count=1)
        assert len(res.granted) == 1
        assert res.past_recheck_horizon == 1

    def test_a_never_checked_proxy_is_never_granted(self):
        """
        Unverified is the LEAST fresh state, not a neutral one. A never-checked
        proxy is dropped by `rank(include_stale=False)`, so it is leased,
        counted, and released -- never handed out.
        """
        store = FakeStore([mkproxy(age_s=None)])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        # the pool had a row, none usable
        assert res.refusal == HandoutRefusal.ALL_STALE
        assert res.leased == 1
        assert res.released_unusable == 1

    def test_age_of_a_never_checked_proxy_is_none_not_zero(self):
        """
        Tested DIRECTLY, because the branch is unreachable through `handout()`.

        `is_stale` returns True for `last_checked is None`, and `handout` ranks
        with `include_stale=False`, so a never-checked proxy is filtered out
        before `_age_s` is ever consulted for a granted row. The mutation run
        (`engineering/tools/mutate_handout.py`) proved that: changing the `None`
        return to `0.0` killed ZERO tests, because nothing reached it.

        The branch is kept as defence-in-depth -- `include_stale` is a parameter
        of `rank`, and a future caller that flips it must not find a hand-out
        layer that reports unverified proxies as "checked 0 s ago". Since the
        code cannot be exercised end-to-end today, it is pinned here instead of
        being deleted or left unproven.
        """
        assert HandoutService._age_s(mkproxy(age_s=None), NOW) is None
        assert HandoutService._age_s(mkproxy(age_s=42.0), NOW) == pytest.approx(42.0)


# ── refusals are named and distinguishable ───────────────────────────────────
class TestRefusalsAreNamed:
    def test_an_empty_pool_says_pool_empty(self):
        res = service(FakeStore([])).handout(target=GOOD_TARGET, count=1)
        assert res.refusal == HandoutRefusal.POOL_EMPTY
        assert res.leased == 0

    def test_a_stale_pool_says_all_stale_not_pool_empty(self):
        """
        Two facts that demand opposite responses: add sources, or run
        discovery. The legacy code could report neither.
        """
        store = FakeStore([mkproxy(age_s=99_999.0)])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        assert res.refusal == HandoutRefusal.ALL_STALE
        assert res.leased == 1

    def test_below_grade_rows_are_not_leased_at_all(self):
        store = FakeStore([mkproxy(grade=Grade.REJECTED)])
        res = service(store).handout(
            target=GOOD_TARGET, count=1, min_grade=Grade.USABLE)
        assert res.refusal == HandoutRefusal.POOL_EMPTY

    def test_require_handout_raises_with_the_reason(self):
        res = service(FakeStore([])).handout(target=GOOD_TARGET, count=1)
        with pytest.raises(HandoutUnavailable, match="POOL_EMPTY"):
            require_handout(res)

    def test_require_handout_raises_target_not_allowed_for_a_denied_target(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=Target(url="https://tiktok.com"), count=1)
        # tiktok.com is NOT in this test's deny-list, so it must be allowed --
        # proving the refusal below comes from data, not from a hardcoded list.
        assert res.refusal is None

        res2 = service(store).handout(
            target=Target(url="https://instagram.com"), count=1)
        with pytest.raises(TargetNotAllowed, match="DENIED_HOST"):
            require_handout(res2)

    def test_require_handout_returns_the_granted_tuple_on_success(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(target=GOOD_TARGET, count=1)
        got = require_handout(res)
        assert isinstance(got, tuple) and isinstance(got[0], Granted)


# ── expired leases are reclaimed so a thin pool recovers ─────────────────────
class TestExpiredLeaseReclaim:
    def test_expired_leases_are_swept_before_leasing(self):
        store = FakeStore([mkproxy()], expired_to_reclaim=4)
        res = service(store).handout(target=GOOD_TARGET, count=1)
        assert res.reclaimed_expired == 4
        assert store.calls[0] == "expire_leases"

    def test_the_sweep_can_be_disabled(self):
        store = FakeStore([mkproxy()], expired_to_reclaim=4)
        pol = HandoutPolicy(reclaim_expired_first=False)
        res = service(store, policy=pol).handout(target=GOOD_TARGET, count=1)
        assert res.reclaimed_expired == 0
        assert "expire_leases" not in store.calls


# ── bounds are validated, loudly ─────────────────────────────────────────────
class TestPolicyBounds:
    def test_count_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="count must be >= 1"):
            service(FakeStore([])).handout(target=GOOD_TARGET, count=0)

    def test_count_above_max_is_rejected(self):
        pol = HandoutPolicy(max_count=5)
        with pytest.raises(ValueError, match="exceeds max_count"):
            service(FakeStore([]), policy=pol).handout(
                target=GOOD_TARGET, count=6)

    def test_lease_ms_above_max_is_rejected(self):
        with pytest.raises(ValueError, match="exceeds max_lease_ms"):
            service(FakeStore([])).handout(
                target=GOOD_TARGET, count=1, lease_ms=999_999)

    def test_the_requested_lease_ms_is_passed_through_and_reported(self):
        store = FakeStore([mkproxy()])
        res = service(store).handout(
            target=GOOD_TARGET, count=1, lease_ms=60_000)
        assert res.lease_ms == 60_000

    def test_overselect_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="overselect must be >= 1"):
            HandoutPolicy(overselect=0)

    def test_a_row_cap_below_max_count_is_rejected(self):
        with pytest.raises(ValueError, match="max_overselect_rows"):
            HandoutPolicy(max_count=10, max_overselect_rows=5)

    def test_max_lease_below_default_is_rejected(self):
        with pytest.raises(ValueError, match="max_lease_ms"):
            HandoutPolicy(default_lease_ms=30_000, max_lease_ms=1_000)

    def test_recheck_horizon_must_be_positive(self):
        with pytest.raises(ValueError, match="recheck_horizon_s must be > 0"):
            HandoutPolicy(recheck_horizon_s=0.0)


# ── the clock is injected, so freshness is testable without waiting ──────────
class TestClockIsInjected:
    def test_advancing_the_clock_makes_evidence_stale(self):
        clock = FakeClock()
        store = FakeStore([mkproxy(age_s=10.0)])
        svc = service(store, clock=clock)

        first = svc.handout(target=GOOD_TARGET, count=1)
        assert first.past_recheck_horizon == 0
        svc.release_all(first)

        # Past the 900 s horizon (ADR-035) but still under ScoringPolicy's
        # max_age_s of 3600, so the row is flagged rather than dropped by
        # `rank`. The old version advanced 200 s, which crossed the withdrawn
        # 90 s TTL and would now assert nothing.
        clock.advance(1_000.0)
        second = svc.handout(target=GOOD_TARGET, count=1)
        assert second.past_recheck_horizon == 1
