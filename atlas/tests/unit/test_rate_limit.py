"""
PER-HOST RATE LIMITING (ADR-034).

Every test drives an INJECTED clock. Nothing here sleeps: a limiter whose tests
wait for real time is a limiter whose edge cases never get tested, which is what
the legacy tree's 13 in-line `time.sleep()` calls produced.

Two negative controls carry their weight here:

  * `test_fixed_window_boundary_burst_is_refused` — the classic fixed-window bug
    admits 2*limit across a window boundary. It is reproduced against a
    deliberately-wrong local implementation, which is asserted to FAIL, so the
    test proves the sliding window does something a fixed one would not.
  * `test_saturated_limiter_refuses_rather_than_evicting_a_live_host` — a cap
    that evicts live hosts is a rate-limit BYPASS. Asserted directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.core.domain.source import Target
from atlas.core.parsing.url import split_url
from atlas.core.policy.target_policy import canonical_host
from atlas.engine.rate_limit import (
    HostRateLimiter, RateLimitPolicy, RateRefusal,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A clock that moves only when a test moves it (ClockPort)."""

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


def limiter(*, limit: int = 3, window_s: float = 60.0,
            max_hosts: int = 4096) -> tuple[HostRateLimiter, FakeClock]:
    clk = FakeClock()
    pol = RateLimitPolicy(
        max_requests_per_host_per_min=limit,
        window_s=window_s,
        max_tracked_hosts=max_hosts,
    )
    return HostRateLimiter(pol, clk), clk


T = lambda url: Target(url=url)          # noqa: E731 - terse on purpose here


# ── the basic rule ────────────────────────────────────────────────────────────

class TestLimit:

    def test_admits_up_to_the_limit_then_refuses(self):
        rl, _ = limiter(limit=3)
        assert [rl.consume(T("https://example.com")).allowed
                for _ in range(3)] == [True, True, True]
        d = rl.consume(T("https://example.com"))
        assert d.allowed is False
        assert d.refusal == RateRefusal.OVER_LIMIT

    def test_remaining_counts_down_and_floors_at_zero(self):
        rl, _ = limiter(limit=3)
        assert [rl.consume(T("https://a.com")).remaining for _ in range(3)] == [2, 1, 0]
        assert rl.consume(T("https://a.com")).remaining == 0

    def test_hosts_have_independent_budgets(self):
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com")).allowed is True
        # b.com must be unaffected by a.com exhausting its budget.
        assert rl.consume(T("https://b.com")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is False

    def test_limit_of_one_admits_exactly_one(self):
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is False


# ── the window slides ─────────────────────────────────────────────────────────

class TestSlidingWindow:

    def test_budget_returns_after_the_window_passes(self):
        rl, clk = limiter(limit=2, window_s=60.0)
        rl.consume(T("https://a.com"))
        rl.consume(T("https://a.com"))
        assert rl.consume(T("https://a.com")).allowed is False
        clk.advance(60.1)
        assert rl.consume(T("https://a.com")).allowed is True

    def test_budget_returns_incrementally_not_all_at_once(self):
        """
        The distinguishing property of a sliding window: at t=30s only the hit
        from t=0 has aged out, so exactly ONE slot is free -- a fixed window
        would have freed the whole budget.
        """
        rl, clk = limiter(limit=2, window_s=60.0)
        rl.consume(T("https://a.com"))       # t=0
        clk.advance(30.0)
        rl.consume(T("https://a.com"))       # t=30
        clk.advance(30.1)                    # t=60.1: the t=0 hit has expired
        assert rl.consume(T("https://a.com")).allowed is True   # one slot
        assert rl.consume(T("https://a.com")).allowed is False  # not two

    def test_fixed_window_boundary_burst_is_refused(self):
        """
        NEGATIVE CONTROL. A fixed-window limiter admits `limit` at the end of one
        bucket and `limit` more at the start of the next -- 2*limit inside a
        200ms span. The wrong implementation is written out here and asserted to
        exceed the rate, so this test cannot pass for both designs.
        """
        limit, window_s = 3, 60.0

        # -- the WRONG design, reproduced locally --
        fixed_bucket, fixed_count, fixed_admitted = 0, 0, 0

        def fixed_window(t_s: float) -> bool:
            nonlocal fixed_bucket, fixed_count, fixed_admitted
            b = int(t_s // window_s)
            if b != fixed_bucket:
                fixed_bucket, fixed_count = b, 0      # the reset that is the bug
            if fixed_count < limit:
                fixed_count += 1
                fixed_admitted += 1
                return True
            return False

        for t in (59.8, 59.85, 59.9, 60.05, 60.1, 60.15):
            fixed_window(t)
        assert fixed_admitted == 6, "control must reproduce the 2x burst"

        # -- the design under test, same request pattern --
        rl, clk = limiter(limit=limit, window_s=window_s)
        clk.advance(59.8)
        admitted = 0
        for step in (0.0, 0.05, 0.05, 0.15, 0.05, 0.05):
            clk.advance(step)
            if rl.consume(T("https://a.com")).allowed:
                admitted += 1

        assert admitted == limit, (
            f"sliding window admitted {admitted} across the boundary; the rate "
            f"is {limit} per {window_s}s at EVERY window position"
        )
        assert admitted < fixed_admitted

    def test_retry_after_points_at_the_moment_the_window_opens(self):
        rl, clk = limiter(limit=1, window_s=60.0)
        rl.consume(T("https://a.com"))          # t=0
        clk.advance(20.0)
        d = rl.consume(T("https://a.com"))
        assert d.allowed is False
        # oldest hit at t=0 expires at t=60, i.e. 40s from now.
        assert d.retry_after_s == pytest.approx(40.0, abs=0.01)

    def test_honouring_retry_after_succeeds_on_the_first_attempt(self):
        """The number must be actionable, not decorative."""
        rl, clk = limiter(limit=2, window_s=60.0)
        rl.consume(T("https://a.com"))
        clk.advance(5.0)
        rl.consume(T("https://a.com"))
        d = rl.consume(T("https://a.com"))
        assert d.allowed is False
        clk.advance(d.retry_after_s + 0.001)
        assert rl.consume(T("https://a.com")).allowed is True

    def test_allowed_decision_reports_no_retry_delay(self):
        rl, _ = limiter(limit=2)
        assert rl.consume(T("https://a.com")).retry_after_s == 0.0


# ── monotonic, not wall-clock ─────────────────────────────────────────────────

class TestMonotonic:

    def test_window_is_measured_monotonically(self):
        """
        The limiter must never read `now()`. A wall-clock limiter breaks in both
        directions: set the clock back and the window never expires; set it
        forward and the limit is bypassed. Enforced by a clock whose `now()`
        raises -- if anything calls it, this test fails.
        """
        class MonotonicOnlyClock(FakeClock):
            def now(self) -> datetime:               # pragma: no cover - must not run
                raise AssertionError(
                    "rate limiter read wall-clock now(); ADR-034 requires "
                    "monotonic_ms so a clock adjustment cannot bypass the limit"
                )

        clk = MonotonicOnlyClock()
        rl = HostRateLimiter(RateLimitPolicy(max_requests_per_host_per_min=2), clk)
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is False
        clk.advance(61.0)
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.observed(T("https://a.com")) == 1
        assert rl.tracked_hosts() == 1


# ── check() vs consume() ──────────────────────────────────────────────────────

class TestCheckThenCommit:

    def test_check_does_not_spend_budget(self):
        rl, _ = limiter(limit=1)
        for _ in range(5):
            assert rl.check(T("https://a.com")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is True

    def test_check_reports_the_refusal_without_recording(self):
        rl, _ = limiter(limit=1)
        rl.consume(T("https://a.com"))
        assert rl.check(T("https://a.com")).allowed is False
        assert rl.observed(T("https://a.com")) == 1     # still 1, not 2

    def test_check_does_not_create_a_tracking_entry(self):
        """
        A read-only probe that allocates is a memory-growth path driven by
        untrusted input, and it would also let `check` alone fill the host table.
        """
        rl, _ = limiter(limit=1, max_hosts=2)
        rl.check(T("https://a.com"))
        rl.check(T("https://b.com"))
        rl.check(T("https://c.com"))
        assert rl.tracked_hosts() == 0

    def test_a_refusal_elsewhere_costs_nothing(self):
        """
        The reason these are two methods: a request rejected for a DIFFERENT
        reason (denied target, empty pool) must not spend the host's budget, or
        the operator sees a rate-limit refusal whose cause was elsewhere.
        """
        rl, _ = limiter(limit=2)
        assert rl.check(T("https://a.com")).allowed is True   # would have served
        # ... caller then refuses for its own reasons and never consumes.
        assert rl.observed(T("https://a.com")) == 0


# ── host identity is shared with the allow-policy ─────────────────────────────

class TestHostKeying:

    def test_case_is_not_a_second_bucket(self):
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://Example.COM")).allowed is True
        assert rl.consume(T("https://example.com")).allowed is False

    def test_trailing_dot_is_not_a_second_bucket(self):
        """
        `split_url` case-folds but leaves the root dot on, so this is the
        spelling that would silently double the rate if the limiter keyed on the
        raw host. Verified at the parser first, so the test names its own premise.
        """
        assert split_url("https://a.com.").host == "a.com."     # premise
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com.")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is False

    def test_port_does_not_split_the_budget(self):
        """
        The limit protects the HOST (ADR-006). Per-port buckets would let :80
        and :443 each spend the full allowance against one origin.
        """
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com:8443")).allowed is True
        assert rl.consume(T("https://a.com")).allowed is False

    def test_path_does_not_split_the_budget(self):
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com/one")).allowed is True
        assert rl.consume(T("https://a.com/two")).allowed is False

    def test_subdomain_is_a_separate_host(self):
        """
        Not folded to the registrable domain: `canonical_host` is exact-host
        identity, and claiming otherwise would need a public-suffix list this
        codebase does not have.
        """
        rl, _ = limiter(limit=1)
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.consume(T("https://www.a.com")).allowed is True

    def test_canonical_host_is_the_shared_definition(self):
        """
        Pins the ADR-034 seam: the limiter keys on the same function the
        allow-policy uses. If `canonical_host` changes, both move together.
        """
        assert canonical_host("Example.COM.") == "example.com"
        assert canonical_host(split_url("https://Example.COM.").host) == "example.com"
        assert canonical_host(None) == ""

    def test_canonical_host_folds_case_via_split_url(self):
        """
        WHERE THE CASE FOLDING ACTUALLY LIVES.

        `canonical_host` calls `.lower()` itself, so it is correct for raw
        config strings too -- but on the target path the host arrives already
        folded by `split_url`. Both halves are pinned so that if `split_url`
        ever stops folding, a test names the reason instead of the limiter
        silently doubling every host's effective rate.
        """
        assert split_url("https://Example.COM").host == "example.com"
        assert split_url("https://EXAMPLE.com:8443").host == "example.com"


# ── bounded memory, failing closed ────────────────────────────────────────────

class TestBounded:

    def test_host_table_is_capped(self):
        rl, _ = limiter(limit=5, max_hosts=3)
        for i in range(10):
            rl.consume(T(f"https://h{i}.com"))
        assert rl.tracked_hosts() <= 3

    def test_saturated_limiter_refuses_rather_than_evicting_a_live_host(self):
        """
        NEGATIVE CONTROL for the bypass. If the cap evicted an ACTIVE host to
        admit a new one, spraying distinct hosts would reset every real host's
        counter. So at saturation with nothing drained, the NEW host is refused
        and the existing budgets are left intact.
        """
        rl, _ = limiter(limit=1, max_hosts=2)
        assert rl.consume(T("https://a.com")).allowed is True
        assert rl.consume(T("https://b.com")).allowed is True

        d = rl.consume(T("https://c.com"))
        assert d.allowed is False
        assert d.refusal == RateRefusal.LIMITER_SATURATED

        # the bypass that must NOT have happened: a.com's budget still spent
        assert rl.consume(T("https://a.com")).allowed is False
        assert rl.observed(T("https://a.com")) == 1

    def test_drained_hosts_are_reclaimed_to_make_room(self):
        rl, clk = limiter(limit=1, window_s=60.0, max_hosts=2)
        rl.consume(T("https://a.com"))
        rl.consume(T("https://b.com"))
        clk.advance(61.0)                       # both windows drain
        assert rl.consume(T("https://c.com")).allowed is True
        assert rl.tracked_hosts() <= 2

    def test_saturation_refusal_carries_a_usable_retry_delay(self):
        rl, _ = limiter(limit=1, max_hosts=1)
        rl.consume(T("https://a.com"))
        d = rl.consume(T("https://b.com"))
        assert d.refusal == RateRefusal.LIMITER_SATURATED
        assert d.retry_after_s > 0


# ── unkeyable targets ─────────────────────────────────────────────────────────

class TestNoHost:

    def test_unkeyable_target_is_refused_not_waved_through(self):
        """
        A target with no host cannot be rate limited. `check_target` already
        refuses it (NO_HOST); the limiter must not be the layer that disagrees
        and admits it unlimited.
        """
        rl, _ = limiter(limit=1)
        d = rl.consume(Target(url="https:///path"))
        assert d.allowed is False
        assert d.refusal == RateRefusal.NO_HOST
        assert d.host is None

    def test_unkeyable_target_allocates_nothing(self):
        rl, _ = limiter(limit=1)
        rl.consume(Target(url="https:///path"))
        assert rl.tracked_hosts() == 0


# ── the decision object refuses to lie ────────────────────────────────────────

class TestDecisionInvariants:

    def test_allowed_with_a_refusal_is_rejected(self):
        from atlas.engine.rate_limit import RateDecision
        with pytest.raises(ValueError, match="carries refusal"):
            RateDecision(allowed=True, host="a.com", refusal=RateRefusal.OVER_LIMIT)

    def test_refused_without_a_reason_is_rejected(self):
        from atlas.engine.rate_limit import RateDecision
        with pytest.raises(ValueError, match="unnamed refusal"):
            RateDecision(allowed=False, host="a.com", refusal=None)

    def test_negative_retry_after_is_rejected(self):
        from atlas.engine.rate_limit import RateDecision
        with pytest.raises(ValueError, match="retry_after_s"):
            RateDecision(allowed=True, host="a.com", retry_after_s=-1.0)


class TestPolicyValidation:

    def test_zero_limit_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="max_requests_per_host_per_min"):
            RateLimitPolicy(max_requests_per_host_per_min=0)

    def test_negative_window_is_refused(self):
        with pytest.raises(ValueError, match="window_s"):
            RateLimitPolicy(window_s=0)

    def test_zero_tracked_hosts_is_refused(self):
        with pytest.raises(ValueError, match="max_tracked_hosts"):
            RateLimitPolicy(max_tracked_hosts=0)

    def test_config_default_is_sixty_per_minute(self):
        """The number in config.yaml, now actually read (ADR-034)."""
        p = RateLimitPolicy()
        assert p.max_requests_per_host_per_min == 60
        assert p.window_s == 60.0


# ── concurrency ───────────────────────────────────────────────────────────────

class TestThreadSafety:

    def test_concurrent_consumers_never_exceed_the_limit(self):
        """
        The limit is the whole point, so it is asserted under real threads
        rather than assumed from a lock's presence. Non-vacuity is checked
        first: if nothing was admitted, "no overshoot" would be trivially true.
        """
        import threading

        rl, _ = limiter(limit=50, window_s=60.0)
        admitted = []
        lock = threading.Lock()

        def worker() -> None:
            local = 0
            for _ in range(40):
                if rl.consume(T("https://a.com")).allowed:
                    local += 1
            with lock:
                admitted.append(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = sum(admitted)
        assert total > 0, "vacuous: nothing was admitted at all"
        assert total == 50, f"8 threads x 40 requests admitted {total}, limit was 50"
