"""
SCHEDULER / POOL-LIFECYCLE tests (ADR-036, ADR-037).

WHY THIS FILE EXISTS, AND WHY IT WAS WRITTEN TWICE

ADR-036 fixed a real defect: `COOLING` was an absorbing state, so a proxy that
failed once left the pool forever. It shipped with a `**Verify:**` line naming
*this file* -- and this file did not exist. The gate check that is supposed to
catch exactly that (`adr_claims_are_verifiable`) passed 36 ADRs, because it
tested `"**Verify:**" in body`: the presence of the STRING, never the existence
of what the string names. ADR-037 records that hole and the check that closes it.

So these tests carry a second obligation beyond covering the scheduler: they are
the artifact ADR-036 cites. Four groups:

  1. `decide()` -- the pure rule, including the branch ORDER, which is
     load-bearing and was the actual defect (a `RETIRED` row must never be
     resurrected; a `LEASED` row belongs to H3, not to the scheduler).
  2. the ABSORBING-STATE negative control: `RETIRED` is terminal AND `COOLING`
     is NOT. A test that only asserted the first would have passed against the
     broken code, which is the whole point of ADR-036's decision 3.
  3. `PoolScheduler` against a fake store -- plan/apply, and the `max_pool_size`
     rule that must never evict a `LEASED` row.
  4. `load_scheduler_policy()` -- ADR-036 decision 4 claimed this function
     existed. It did not. It does now, and these tests are why that claim is
     checkable: a missing key must RAISE, never default, or `config.yaml` is
     decorative again (ADR-029).

The store is faked for the unit tests; `select_evictable`'s SQL ordering is
exercised against real SQLite in `test_store.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.config import ConfigError, load_scheduler_policy
from atlas.core.domain.proxy import (
    Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade
from atlas.core.policy.lifecycle import (
    PoolAction, SchedulerPolicy, age_s, cooldown_elapsed, decide, is_terminal,
)
from atlas.engine.scheduler import PoolScheduler, SchedulerPlan

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

# Mirrors config.yaml. `test_the_mirrored_defaults_match_the_file` is what stops
# this drifting into a fifth private copy of the same four numbers (ADR-031).
POLICY = SchedulerPolicy()


def mk(host: str = "1.2.3.4", *, port: int = 8080,
       state: ProxyState = ProxyState.READY,
       consecutive_failures: int = 0,
       last_checked: datetime | None = NOW,
       grade: Grade = Grade.GOOD,
       lease_id: str | None = None,
       p95_ms: float | None = 400.0) -> Proxy:
    """A proxy with only the lifecycle-relevant fields set."""
    return Proxy(
        endpoint=Endpoint(host=host, port=port),
        protocol=Protocol.HTTP,
        state=state,
        latency=LatencyProfile(p95_ms=p95_ms),
        consecutive_failures=consecutive_failures,
        last_checked=last_checked,
        lease_id=lease_id,
        grade=grade,
    )


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> float:  # pragma: no cover - unused by scheduler
        return 0.0

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeStore:
    """
    Records writes so tests can assert what the scheduler DID, not just returned.

    `select_evictable` mimics the SQL's worst-first contract by refusing to
    return LEASED/PROBING rows -- if the real query ever regresses on that, the
    integration test in test_store.py catches it; here it keeps the fake honest
    rather than more permissive than the thing it stands in for.
    """

    def __init__(self, rows: tuple[Proxy, ...] = ()) -> None:
        self.rows = list(rows)
        self.upserted: list[Proxy] = []
        self.deleted: list[str] = []

    def select_schedulable(self, *, limit: int = 1000) -> tuple[Proxy, ...]:
        ok = [p for p in self.rows
              if p.state in (ProxyState.DISCOVERED, ProxyState.COOLING,
                             ProxyState.READY, ProxyState.PROBING)]
        return tuple(ok[:limit])

    def select_evictable(self, *, limit: int) -> tuple[Proxy, ...]:
        order = {ProxyState.RETIRED: 0, ProxyState.COOLING: 1,
                 ProxyState.DISCOVERED: 2, ProxyState.READY: 3}
        ok = [p for p in self.rows if p.state in order]
        ok.sort(key=lambda p: (order[p.state], p.fingerprint))
        return tuple(ok[:limit])

    def pool_size(self) -> int:
        return len(self.rows)

    def upsert(self, proxy: Proxy) -> None:
        self.upserted.append(proxy)

    def upsert_many(self, proxies: tuple[Proxy, ...]) -> int:
        self.upserted.extend(proxies)
        # Reflect the write back into `rows`, so a re-plan in the same pass sees
        # the new states. Without this the fake would be more forgiving than
        # SQLite and `run_once`'s re-plan step would be untestable.
        by_fp = {p.fingerprint: p for p in proxies}
        self.rows = [by_fp.get(r.fingerprint, r) for r in self.rows]
        return len(proxies)

    def delete_many(self, fingerprints: tuple[str, ...]) -> int:
        """
        Mimics the real statement's refusal to delete a LEASED row: it deletes
        by fingerprint AND state, so a row leased between plan and delete
        survives and the returned count is LOWER than requested.
        """
        wanted = set(fingerprints)
        keep, gone = [], []
        for r in self.rows:
            if r.fingerprint in wanted and r.state is not ProxyState.LEASED:
                gone.append(r.fingerprint)
            else:
                keep.append(r)
        self.rows = keep
        self.deleted.extend(gone)
        return len(gone)


# ── decide(): the pure rule ────────────────────────────────────────────────────

class TestDecide:
    def test_healthy_ready_inside_horizon_is_kept(self) -> None:
        p = mk(state=ProxyState.READY, last_checked=NOW - timedelta(seconds=100))
        assert decide(p, POLICY, now=NOW) is PoolAction.KEEP_READY

    def test_ready_past_recheck_horizon_is_rechecked(self) -> None:
        p = mk(state=ProxyState.READY, last_checked=NOW - timedelta(seconds=901))
        assert decide(p, POLICY, now=NOW) is PoolAction.RECHECK_READY

    @pytest.mark.parametrize("age,expected", [
        (899.9, PoolAction.KEEP_READY),
        (900.0, PoolAction.KEEP_READY),    # `>` not `>=`: the boundary is inclusive
        (900.1, PoolAction.RECHECK_READY),
    ])
    def test_the_horizon_boundary_is_pinned_on_both_sides(
            self, age: float, expected: PoolAction) -> None:
        """
        A one-sided assertion would pass against `>=` and against `>` alike.
        ADR-035 made 900 s load-bearing on the serving path, so which side of the
        comparison the boundary falls on is a real behavioural commitment.
        """
        p = mk(state=ProxyState.READY, last_checked=NOW - timedelta(seconds=age))
        assert decide(p, POLICY, now=NOW) is expected

    def test_ready_but_never_checked_is_rechecked_not_assumed_fresh(self) -> None:
        """H7/ADR-003: absence of evidence is never evidence of health."""
        p = mk(state=ProxyState.READY, last_checked=None)
        assert decide(p, POLICY, now=NOW) is PoolAction.RECHECK_READY

    def test_at_threshold_retires(self) -> None:
        p = mk(state=ProxyState.COOLING, consecutive_failures=5)
        assert decide(p, POLICY, now=NOW) is PoolAction.RETIRE

    def test_below_threshold_with_elapsed_cooldown_is_rechecked(self) -> None:
        # 2 failures -> 60 s ladder step; 61 s later it is eligible again.
        p = mk(state=ProxyState.COOLING, consecutive_failures=2,
               last_checked=NOW - timedelta(seconds=61))
        assert decide(p, POLICY, now=NOW) is PoolAction.RECHECK

    def test_below_threshold_still_cooling_waits(self) -> None:
        p = mk(state=ProxyState.COOLING, consecutive_failures=2,
               last_checked=NOW - timedelta(seconds=5))
        assert decide(p, POLICY, now=NOW) is PoolAction.COOLING_NOT_ELAPSED

    def test_the_defect_itself_one_failure_does_not_remove_a_proxy_forever(self) -> None:
        """
        THE ADR-036 REGRESSION TEST.

        Before ADR-036 a single failure meant `COOLING` forever: no transition
        out existed, `lease()` filters on `state='READY'`, and discovery skips
        known fingerprints. This asserts the recovery path the docstring always
        claimed -- one failure, cooldown elapses, the proxy is re-probed.
        """
        p = mk(state=ProxyState.COOLING, consecutive_failures=1,
               last_checked=NOW - timedelta(seconds=31))
        assert decide(p, POLICY, now=NOW) is PoolAction.RECHECK

    def test_retired_is_never_reconsidered(self) -> None:
        p = mk(state=ProxyState.RETIRED, consecutive_failures=99)
        assert decide(p, POLICY, now=NOW) is PoolAction.TERMINAL

    @pytest.mark.parametrize("state", [ProxyState.LEASED, ProxyState.PROBING])
    def test_in_flight_rows_are_not_touched(self, state: ProxyState) -> None:
        p = mk(state=state, lease_id="L1" if state is ProxyState.LEASED else None)
        assert decide(p, POLICY, now=NOW) is PoolAction.IN_FLIGHT

    def test_terminal_is_checked_before_retirement(self) -> None:
        """
        Branch ORDER, not just outcome: a RETIRED row over the failure threshold
        must report TERMINAL, never RETIRE. If retirement were checked first the
        scheduler would rewrite already-retired rows on every pass forever.
        """
        p = mk(state=ProxyState.RETIRED, consecutive_failures=50)
        assert decide(p, POLICY, now=NOW) is PoolAction.TERMINAL

    def test_leased_is_checked_before_retirement(self) -> None:
        """
        A LEASED row over the threshold must stay IN_FLIGHT. Retiring it would
        put a lifecycle rule in charge of the H3 guarantee -- the row would leave
        READY while a consumer still held it.
        """
        p = mk(state=ProxyState.LEASED, consecutive_failures=50, lease_id="L1")
        assert decide(p, POLICY, now=NOW) is PoolAction.IN_FLIGHT

    def test_a_recovered_ready_row_past_the_threshold_still_retires(self) -> None:
        """Retirement is not confined to COOLING: the docstring says so."""
        p = mk(state=ProxyState.READY, consecutive_failures=5,
               last_checked=NOW)
        assert decide(p, POLICY, now=NOW) is PoolAction.RETIRE

    def test_discovered_never_checked_is_a_fresh_candidate(self) -> None:
        p = mk(state=ProxyState.DISCOVERED, last_checked=None)
        assert decide(p, POLICY, now=NOW) is PoolAction.RECHECK

    def test_every_action_is_reachable(self) -> None:
        """
        A rule with an unreachable outcome is a rule with dead code in it. Every
        `PoolAction` must be produced by some input, or the enum is lying about
        what the scheduler can do.
        """
        produced = {
            decide(mk(state=ProxyState.RETIRED), POLICY, now=NOW),
            decide(mk(state=ProxyState.LEASED, lease_id="L"), POLICY, now=NOW),
            decide(mk(state=ProxyState.COOLING, consecutive_failures=5),
                   POLICY, now=NOW),
            decide(mk(state=ProxyState.READY,
                      last_checked=NOW - timedelta(seconds=10)), POLICY, now=NOW),
            decide(mk(state=ProxyState.READY, last_checked=None), POLICY, now=NOW),
            decide(mk(state=ProxyState.COOLING, consecutive_failures=1,
                      last_checked=NOW - timedelta(seconds=31)), POLICY, now=NOW),
            decide(mk(state=ProxyState.COOLING, consecutive_failures=2,
                      last_checked=NOW), POLICY, now=NOW),
        }
        assert produced == set(PoolAction)


# ── the absorbing-state negative control (ADR-036 decision 3) ──────────────────

class TestAbsorbingState:
    """
    ADR-036's defect was that `COOLING` behaved as an absorbing state while its
    own docstring said "eligible again after a cooldown". A test asserting only
    that `RETIRED` is terminal would have passed against the broken code, so the
    control that matters is the NEGATIVE one: COOLING must NOT be terminal.
    """

    def test_retired_is_absorbing(self) -> None:
        assert is_terminal(ProxyState.RETIRED) is True

    def test_cooling_is_not_absorbing_the_negative_control(self) -> None:
        """
        THE ASSERTION THAT WOULD HAVE CAUGHT THE DEFECT.

        If someone reintroduces "COOLING is terminal" -- by treating it as
        terminal in `is_terminal`, or by removing the recovery branch -- this
        fails. It is stated as its own test, not as a second assert inside the
        RETIRED test, so the failure output names the actual defect.
        """
        assert is_terminal(ProxyState.COOLING) is False

    @pytest.mark.parametrize("state", [
        ProxyState.DISCOVERED, ProxyState.COOLING, ProxyState.READY,
        ProxyState.PROBING, ProxyState.LEASED,
    ])
    def test_only_retired_is_absorbing(self, state: ProxyState) -> None:
        assert is_terminal(state) is False

    def test_exactly_one_state_is_absorbing(self) -> None:
        """
        Enumerated over the whole enum, so a NEW state added later is forced
        through this decision rather than defaulting into terminality unnoticed.
        """
        terminal = [s for s in ProxyState if is_terminal(s)]
        assert terminal == [ProxyState.RETIRED]

    def test_cooling_has_a_reachable_exit(self) -> None:
        """
        The structural claim behind ADR-036: for a COOLING row below the
        threshold there EXISTS a time at which the scheduler acts on it. Proven
        by advancing a clock rather than by reading the code -- if the exit is
        ever removed again, every one of these decisions stays
        COOLING_NOT_ELAPSED and the assertion fails.
        """
        p = mk(state=ProxyState.COOLING, consecutive_failures=1, last_checked=NOW)
        seen = {decide(p, POLICY, now=NOW + timedelta(seconds=t))
                for t in (0, 1, 10, 29, 31, 60, 3600)}
        assert PoolAction.RECHECK in seen, (
            "no reachable exit from COOLING: this is exactly the ADR-036 defect"
        )

    def test_retirement_produces_a_row_that_is_terminal_under_decide(self) -> None:
        """
        End-to-end on the domain object: `Proxy.retired()` must yield a row that
        `decide()` then classifies TERMINAL. Two halves of ADR-036 (the
        transition and the rule) agreeing is what makes retirement real.
        """
        p = mk(state=ProxyState.COOLING, consecutive_failures=5)
        r = p.retired(reason="threshold")
        assert r.state is ProxyState.RETIRED
        assert is_terminal(r.state) is True
        assert decide(r, POLICY, now=NOW) is PoolAction.TERMINAL

    def test_retirement_clears_the_lease_id(self) -> None:
        """A terminal row holding a lease id would be a lease nobody can release."""
        p = mk(state=ProxyState.COOLING, consecutive_failures=5, lease_id="L9")
        assert p.retired(reason="threshold").lease_id is None


# ── PoolScheduler: plan and apply ──────────────────────────────────────────────

class TestPoolScheduler:
    def test_plan_writes_nothing(self) -> None:
        """
        `plan()` is the inspect-before-mutate half of ADR-036 decision 4. If it
        ever writes, a caller that logs a plan and refuses it has already
        changed the pool.
        """
        store = FakeStore((mk(state=ProxyState.COOLING, consecutive_failures=5),))
        sched = PoolScheduler(store, FakeClock(), policy=POLICY)
        sched.plan()
        assert store.upserted == []
        assert store.deleted == []

    def test_every_row_lands_in_exactly_one_bucket(self) -> None:
        """
        B-02's accounting rule, enforced by `SchedulerPlan.__post_init__`. Built
        from a mixed pool so the sum is a real cross-check, not a tautology on
        an empty plan.
        """
        rows = (
            mk("1.1.1.1", state=ProxyState.COOLING, consecutive_failures=5),
            mk("2.2.2.2", state=ProxyState.COOLING, consecutive_failures=1,
               last_checked=NOW - timedelta(seconds=31)),
            mk("3.3.3.3", state=ProxyState.COOLING, consecutive_failures=2,
               last_checked=NOW),
            mk("4.4.4.4", state=ProxyState.READY,
               last_checked=NOW - timedelta(seconds=10)),
            mk("5.5.5.5", state=ProxyState.READY,
               last_checked=NOW - timedelta(seconds=1000)),
            mk("6.6.6.6", state=ProxyState.PROBING),
        )
        plan = PoolScheduler(FakeStore(rows), FakeClock(), policy=POLICY).plan()
        assert plan.examined == 6
        assert len(plan.retire) == 1
        assert len(plan.recheck) == 1
        assert len(plan.cooling) == 1
        assert len(plan.keep_ready) == 1
        assert len(plan.recheck_ready) == 1
        assert len(plan.in_flight) == 1

    def test_apply_retirements_transitions_and_records_the_reason(self) -> None:
        store = FakeStore((mk(state=ProxyState.COOLING, consecutive_failures=7),))
        sched = PoolScheduler(store, FakeClock(), policy=POLICY)
        n = sched.apply_retirements(sched.plan())
        assert n == 1
        assert store.upserted[0].state is ProxyState.RETIRED
        # The reason names the count, so an operator can tell WHY without
        # re-deriving it from config -- B-02's undiagnosable-row lesson.
        assert "7" in (store.upserted[0].reason_code or "")

    def test_a_retired_row_is_not_retired_again_on_the_next_pass(self) -> None:
        """
        Idempotence. Retirement is terminal, so a second pass over the same pool
        must be a no-op; if it were not, the scheduler would rewrite every dead
        row forever and `last_checked` would keep moving on rows nobody probes.
        """
        store = FakeStore((mk(state=ProxyState.COOLING, consecutive_failures=5),))
        sched = PoolScheduler(store, FakeClock(), policy=POLICY)
        assert sched.apply_retirements(sched.plan()) == 1
        store.upserted.clear()
        assert sched.apply_retirements(sched.plan()) == 0
        assert store.upserted == []

    def test_no_eviction_when_inside_the_cap(self) -> None:
        store = FakeStore(tuple(mk(f"10.0.0.{i}") for i in range(5)))
        sched = PoolScheduler(store, FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=10))
        plan = sched.plan()
        assert plan.over_capacity_by == 0
        assert plan.evict == ()
        assert sched.apply_evictions(plan) == 0

    def test_eviction_removes_only_the_overflow(self) -> None:
        store = FakeStore(tuple(mk(f"10.0.0.{i}") for i in range(8)))
        sched = PoolScheduler(store, FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=5))
        plan = sched.plan()
        assert plan.over_capacity_by == 3
        assert len(plan.evict) == 3
        assert sched.apply_evictions(plan) == 3
        assert store.pool_size() == 5

    def test_eviction_never_deletes_a_leased_row(self) -> None:
        """
        ADR-036 decision 5. If the cap could evict a LEASED row, a size limit
        would be silently in charge of the H3 no-double-delivery guarantee: the
        row would vanish while a consumer still held it.
        """
        rows = (
            mk("9.9.9.9", state=ProxyState.LEASED, lease_id="L1"),
            mk("8.8.8.8", state=ProxyState.LEASED, lease_id="L2"),
        ) + tuple(mk(f"10.0.0.{i}") for i in range(4))
        store = FakeStore(rows)
        sched = PoolScheduler(store, FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=1))
        plan = sched.plan()
        assert all(p.state is not ProxyState.LEASED for p in plan.evict), (
            "a LEASED row was selected for eviction: H3 would be decided by "
            "max_pool_size"
        )
        sched.apply_evictions(plan)
        survivors = {p.fingerprint for p in store.rows}
        for leased in rows[:2]:
            assert leased.fingerprint in survivors

    def test_eviction_reports_the_shortfall_when_it_loses_the_race(self) -> None:
        """
        A row leased between plan and delete survives, and the returned count
        must be the number ACTUALLY deleted. Reporting the requested count would
        make the pool look inside its cap when it is not.
        """
        rows = tuple(mk(f"10.0.0.{i}") for i in range(4))
        store = FakeStore(rows)
        sched = PoolScheduler(store, FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=1))
        plan = sched.plan()
        assert len(plan.evict) == 3
        # Simulate the race: one planned victim is LEASED after planning but
        # before deletion. `FakeStore.delete_many` mirrors the real statement's
        # state predicate, so the leased row survives.
        victim = plan.evict[0].fingerprint
        store.rows = [
            mk(r.endpoint.host, port=r.endpoint.port, state=ProxyState.LEASED,
               lease_id="L1", last_checked=r.last_checked, grade=r.grade)
            if r.fingerprint == victim else r
            for r in store.rows
        ]
        assert sched.apply_evictions(plan) == 2
        assert victim not in store.deleted

    def test_run_once_retires_before_it_evicts(self) -> None:
        """
        ADR-036: retirement first, so the cap sheds dead weight before it
        touches anything live. Asserted by outcome -- the READY row survives
        while the newly-retired one is the row that goes.
        """
        rows = (
            mk("1.1.1.1", state=ProxyState.COOLING, consecutive_failures=5),
            mk("2.2.2.2", state=ProxyState.READY,
               last_checked=NOW - timedelta(seconds=10)),
        )
        store = FakeStore(rows)
        sched = PoolScheduler(store, FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=1))
        plan, retired, evicted = sched.run_once()
        assert retired == 1
        assert evicted == 1
        survivors = {p.fingerprint for p in store.rows}
        assert rows[1].fingerprint in survivors, (
            "eviction removed the healthy READY row and kept the retired one"
        )

    def test_batch_bounds_the_rows_loaded(self) -> None:
        store = FakeStore(tuple(mk(f"10.0.{i // 250}.{i % 250}")
                                for i in range(300)))
        plan = PoolScheduler(store, FakeClock(), policy=POLICY).plan(batch=50)
        assert plan.examined == 50

    def test_the_clock_is_injected_not_read_from_the_wall(self) -> None:
        """
        Advancing a fake clock must change the decision. If the scheduler read
        `datetime.now()` internally, this would be untestable without sleeping
        -- the reason ADR-006's `cooldown_delay` was made pure.
        """
        clock = FakeClock()
        store = FakeStore((mk(state=ProxyState.COOLING, consecutive_failures=1,
                              last_checked=NOW),))
        sched = PoolScheduler(store, clock, policy=POLICY)
        assert len(sched.plan().cooling) == 1
        clock.advance(31)
        assert len(sched.plan().recheck) == 1

    def test_actionable_counts_only_what_changes(self) -> None:
        rows = (
            mk("1.1.1.1", state=ProxyState.COOLING, consecutive_failures=5),
            mk("2.2.2.2", state=ProxyState.READY,
               last_checked=NOW - timedelta(seconds=10)),
        )
        sched = PoolScheduler(FakeStore(rows), FakeClock(),
                              policy=SchedulerPolicy(max_pool_size=1))
        plan = sched.plan()
        assert plan.actionable == len(plan.retire) + len(plan.evict)

    def test_an_empty_pool_plans_nothing_and_raises_nothing(self) -> None:
        sched = PoolScheduler(FakeStore(()), FakeClock(), policy=POLICY)
        plan, retired, evicted = sched.run_once()
        assert (plan.examined, plan.actionable, retired, evicted) == (0, 0, 0, 0)

    def test_a_plan_that_loses_a_row_is_refused(self) -> None:
        """
        The accounting guard itself must bite. Constructed directly with a bad
        sum, because no scheduler input can produce one -- which is the point:
        the invariant is enforced at the boundary, not assumed.
        """
        with pytest.raises(ValueError, match="lost rows"):
            SchedulerPlan(examined=3, retire=(mk(),), recheck=(), cooling=(),
                          keep_ready=(), recheck_ready=(), in_flight=(),
                          terminal=(), evict=(), pool_size=3,
                          over_capacity_by=0)


# ── load_scheduler_policy(): ADR-036 decision 4, ADR-037 ──────────────────────

class TestLoader:
    """
    ADR-036 decision 4 asserted these four keys were "read by
    `load_scheduler_policy()` in `adapters/config.py`". No such function
    existed -- the ADR-019 defect INSIDE the ADR that was written to fix it.
    These tests are what make the claim checkable.
    """

    def test_the_real_config_file_loads(self) -> None:
        p = load_scheduler_policy()
        assert p.recheck_ready_after_s == 900.0
        assert p.discovery_interval_s == 600.0
        assert p.retire_after_consecutive_failures == 5
        assert p.max_pool_size == 50_000

    def test_the_mirrored_defaults_match_the_file(self) -> None:
        """
        THE ANTI-DRIFT TEST (ADR-031 pattern).

        `SchedulerPolicy`'s defaults duplicate config.yaml because `core/` may
        not read files. A mirrored default is only safe if something FAILS when
        it stops mirroring -- otherwise it is a fifth copy of the same numbers,
        free to drift, which is what made the keys decorative to begin with.
        """
        assert load_scheduler_policy() == SchedulerPolicy()

    def test_max_reachable_backoff_is_derived_from_the_file(self) -> None:
        """
        ADR-036 recorded 240 s, and that ADR-006's 3600 s cap therefore cannot
        bind on the proxy path. Re-derived from the loaded config rather than
        restated, so if `retire_after_consecutive_failures` changes the ADR's
        claim is re-checked instead of quietly becoming false.
        """
        p = load_scheduler_policy()
        assert p.max_reachable_backoff_s == 240.0
        assert p.max_reachable_backoff_s < p.cooldown_cap_s

    @pytest.mark.parametrize("key", [
        "recheck_ready_after_s", "discovery_interval_s",
        "retire_after_consecutive_failures", "max_pool_size",
    ])
    def test_a_missing_key_raises_and_does_not_default(
            self, key: str, tmp_path: Path) -> None:
        """
        THE POINT OF THE LOADER (ADR-029).

        A loader that substituted its own fallback would leave config.yaml as
        decorative as ADR-036 found it: an operator could delete `max_pool_size`
        and the system would carry on with a number from the source code.
        """
        block = {"recheck_ready_after_s": 900, "discovery_interval_s": 600,
                 "retire_after_consecutive_failures": 5, "max_pool_size": 50000}
        del block[key]
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler(block), encoding="utf-8")
        with pytest.raises(ConfigError, match=key):
            load_scheduler_policy(cfg)

    def test_a_missing_scheduler_block_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("targets:\n  default_target: null\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="scheduler"):
            load_scheduler_policy(cfg)

    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_scheduler_policy(tmp_path / "nope.yaml")

    def test_a_string_value_is_refused(self, tmp_path: Path) -> None:
        """
        `max_pool_size: "50000"` would otherwise compare as a string and make
        every capacity comparison a type error at the worst moment.
        """
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": 900, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": 5,
            "max_pool_size": '"50000"'}), encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a number"):
            load_scheduler_policy(cfg)

    def test_a_boolean_is_refused_because_bool_is_an_int(self, tmp_path: Path) -> None:
        """
        `retire_after_consecutive_failures: yes` is YAML-true, and `bool` is a
        subclass of `int`, so without an explicit check it would load as 1 and
        retire every proxy on its FIRST failure -- emptying the pool. Guarded by
        construction; never observed in a real config file, and recorded as such
        in ADR-037 rather than claimed as a bug found in the wild.
        """
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": 900, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": "yes",
            "max_pool_size": 50000}), encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a number"):
            load_scheduler_policy(cfg)

    def test_zero_retire_threshold_is_refused_by_the_pure_policy(
            self, tmp_path: Path) -> None:
        """
        A threshold of 0 retires a proxy that has never failed: the pool empties
        itself on the first pass. The range rule lives in core; the loader must
        surface it as a ConfigError rather than letting a raw ValueError escape.
        """
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": 900, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": 0,
            "max_pool_size": 50000}), encoding="utf-8")
        with pytest.raises(ConfigError, match="retire_after_consecutive_failures"):
            load_scheduler_policy(cfg)

    def test_a_negative_horizon_is_refused(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": -1, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": 5,
            "max_pool_size": 50000}), encoding="utf-8")
        with pytest.raises(ConfigError, match="recheck_ready_after_s"):
            load_scheduler_policy(cfg)

    def test_a_fractional_pool_size_is_refused(self, tmp_path: Path) -> None:
        """`max_pool_size: 1.5` is not a number of rows."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": 900, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": 5,
            "max_pool_size": 1.5}), encoding="utf-8")
        with pytest.raises(ConfigError, match="whole number"):
            load_scheduler_policy(cfg)

    def test_the_loaded_policy_actually_drives_a_decision(self, tmp_path: Path) -> None:
        """
        END TO END, and the reason this class is not just input validation: a
        value from the FILE must change what the scheduler does. A loader whose
        output nothing consults is the decorative-config defect with extra steps.
        """
        cfg = tmp_path / "config.yaml"
        cfg.write_text(_yaml_scheduler({
            "recheck_ready_after_s": 60, "discovery_interval_s": 600,
            "retire_after_consecutive_failures": 2,
            "max_pool_size": 50000}), encoding="utf-8")
        policy = load_scheduler_policy(cfg)
        # 100 s old READY: kept under the file's 900, rechecked under this 60.
        p = mk(state=ProxyState.READY, last_checked=NOW - timedelta(seconds=100))
        assert decide(p, POLICY, now=NOW) is PoolAction.KEEP_READY
        assert decide(p, policy, now=NOW) is PoolAction.RECHECK_READY
        # 2 failures retires under the file's threshold of 2, not under 5.
        q = mk(state=ProxyState.COOLING, consecutive_failures=2)
        assert decide(q, POLICY, now=NOW) is not PoolAction.RETIRE
        assert decide(q, policy, now=NOW) is PoolAction.RETIRE


def _yaml_scheduler(block: dict) -> str:
    lines = "\n".join(f"  {k}: {v}" for k, v in block.items())
    return f"scheduler:\n{lines}\n"


# ── helpers on the pure module ────────────────────────────────────────────────

class TestPureHelpers:
    def test_age_is_none_for_a_never_checked_row(self) -> None:
        assert age_s(mk(last_checked=None), now=NOW) is None

    def test_age_is_measured_in_seconds(self) -> None:
        assert age_s(mk(last_checked=NOW - timedelta(seconds=90)),
                     now=NOW) == pytest.approx(90.0)

    def test_a_never_checked_row_counts_as_cooldown_elapsed(self) -> None:
        """
        Treating "unknown" as "still cooling" would strand every DISCOVERED row
        forever -- the ADR-035 hole inverted.
        """
        assert cooldown_elapsed(mk(last_checked=None), POLICY, now=NOW) is True

    @pytest.mark.parametrize("failures,delay_s", [
        (1, 30.0), (2, 60.0), (3, 120.0), (4, 240.0),
    ])
    def test_the_backoff_ladder_is_exponential(
            self, failures: int, delay_s: float) -> None:
        """
        ADR-006's ladder, at the steps reachable before retirement fires. Pinned
        as VALUES: if the base or the doubling changes, ADR-036's 240 s
        max-reachable claim changes with it and must be re-recorded.
        """
        p = mk(consecutive_failures=failures,
               last_checked=NOW - timedelta(seconds=delay_s - 0.1))
        assert cooldown_elapsed(p, POLICY, now=NOW) is False
        p2 = mk(consecutive_failures=failures,
                last_checked=NOW - timedelta(seconds=delay_s + 0.1))
        assert cooldown_elapsed(p2, POLICY, now=NOW) is True
