"""
RECHECK tests (ADR-038) — the seam ADR-036 left open, and the three defects the
obvious wiring would have introduced.

WHAT THESE TESTS ARE FOR

P10's job was "connect `plan().recheck` to the probe path". The one-line version
of that is wrong in three measured ways (`engineering/raw/recheck_gap.json`), so
the tests that matter here are the NEGATIVE CONTROLS: each one fails against the
naive implementation and passes against the claim-based one.

  1. `TestLeaseClobber` -- the write-back must not erase a live lease. The naive
     `upsert_many` path does exactly that, and H3's `lease_log` audit cannot see
     it, because no second `LEASE` row is ever written. `naive_recheck.py` is the
     committed negative control, in the P05.T3 tradition: a green result means
     nothing unless the same assertion is shown to catch the broken version.
  2. `TestDoubleProbe` -- two passes must not both own the same row.
  3. `TestProbingIsNotAbsorbing` -- `PROBING` must have an exit, or ADR-036's
     absorbing state returns under a new name.

The accounting tests exist because `RecheckReport` asserts its own identities;
`TestAccounting` proves those assertions have teeth rather than trusting a
dataclass to police itself.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade, ReasonCode, Verdict
from atlas.core.policy.lifecycle import SchedulerPolicy
from atlas.engine.recheck import RecheckBudget, RecheckReport, RecheckService
from atlas.engine.scheduler import PoolScheduler

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> float:  # pragma: no cover - unused here
        return 0.0

    def deadline(self, after_ms: float) -> datetime:
        return self._now + timedelta(milliseconds=after_ms)

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def mk(host: str = "1.2.3.4", *, port: int = 8080,
       state: ProxyState = ProxyState.READY,
       consecutive_failures: int = 0,
       last_checked: datetime | None = NOW,
       grade: Grade = Grade.GOOD,
       abandoned_rechecks: int = 0,
       p95_ms: float | None = 400.0) -> Proxy:
    return Proxy(
        endpoint=Endpoint(host=host, port=port),
        protocol=Protocol.HTTP,
        state=state,
        grade=grade,
        latency=LatencyProfile(samples_ms=(p95_ms,) if p95_ms else (),
                               p50_ms=p95_ms, p95_ms=p95_ms, mean_ms=p95_ms,
                               success_ratio=1.0),
        anonymity=Anonymity.ANONYMOUS,
        consecutive_failures=consecutive_failures,
        abandoned_rechecks=abandoned_rechecks,
        last_checked=last_checked,
        first_seen=NOW - timedelta(days=1),
        source_id="test",
    )


class StubEngine:
    """
    Stands in for `DiscoveryEngine.evaluate`, recording what it was asked to probe.

    Deliberately NOT a mock of the probe: this module's contract is the sequencing
    (claim -> probe -> conditional write-back), and the gate itself is already
    proven in test_policy.py / test_probe.py. What must be observable here is
    WHICH rows were probed and what was written back.
    """

    def __init__(self, *, admit: bool = True,
                 reason: ReasonCode = ReasonCode.TOO_SLOW_P95) -> None:
        self.admit = admit
        self.reason = reason
        self.seen: list[str] = []

    async def evaluate(self, proxy: Proxy):
        self.seen.append(proxy.fingerprint)
        if self.admit:
            out = (proxy.record_success(NOW)
                   .with_state(ProxyState.READY, reason=ReasonCode.OK.value)
                   .graded(Grade.GOOD))
            return out, Verdict.accept(Grade.GOOD)
        out = (proxy.record_failure(NOW, reason=self.reason.value)
               .with_state(ProxyState.COOLING, reason=self.reason.value)
               .graded(Grade.REJECTED))
        return out, Verdict.reject(self.reason, "stub")


def store_with(*rows: Proxy) -> tuple[SqliteStore, Path, object]:
    tmp = tempfile.TemporaryDirectory()
    store = SqliteStore(Path(tmp.name) / "pool.db")
    if rows:
        store.upsert_many(rows)
    return store, Path(tmp.name), tmp


def service(store, clock, *, policy: SchedulerPolicy | None = None,
            engine: StubEngine | None = None) -> tuple[RecheckService, StubEngine]:
    eng = engine or StubEngine()
    sched = PoolScheduler(store, clock, policy=policy or SchedulerPolicy())
    return RecheckService(scheduler=sched, engine=eng, store=store,
                          clock=clock), eng


# ── 1. the measured clobber ───────────────────────────────────────────────────
class TestLeaseClobber:
    """
    The defect worth the whole module: a recheck write-back erasing a live lease.

    This is not hypothetical. `recheck_gap.json` records it against a real store:
    verdict "CLOBBERED: the recheck write-back erased a live lease", with
    `audit_double_delivery: []` -- H3's own audit blind to it.
    """

    def test_a_writeback_does_not_erase_a_lease_taken_mid_probe(self):
        stale = mk("10.0.0.1", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(stale)
        try:
            clock = FakeClock()
            svc, eng = service(store, clock)

            # The row is claimed for probe...
            claimed = store.claim_for_probe((stale.fingerprint,), now=NOW,
                                            probe_ms=60_000)
            assert len(claimed) == 1

            # ...so a consumer can no longer lease it. That is the fix: the claim
            # removes it from READY, which is the only state lease() can see.
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            assert leased == (), (
                "a row claimed for probe must not be leasable: that overlap is "
                "exactly how the write-back came to erase a live lease"
            )

            probed = (claimed[0].record_success(NOW)
                      .with_state(ProxyState.READY, reason="OK")
                      .graded(Grade.GOOD))
            assert store.complete_probe(probed, now=NOW) is True
            after = store.get(stale.fingerprint)
            assert after.state is ProxyState.READY
            assert after.lease_id is None
        finally:
            store.close()
            tmp.cleanup()

    def test_a_writeback_is_refused_when_the_claim_was_lost(self):
        """
        The other half: if we lose the row anyway, the write-back must REFUSE.

        The claim narrows the window; it does not abolish every possible race
        (a reclaim could fire against a slow probe). So `complete_probe` is
        conditional, and a lost claim must return False rather than overwrite.
        """
        row = mk("10.0.0.2", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            claimed = store.claim_for_probe((row.fingerprint,), now=NOW,
                                            probe_ms=60_000)
            assert len(claimed) == 1

            # Simulate losing the claim: a reclaim fires, then a consumer leases.
            store.reclaim_stale_probes(now=NOW + timedelta(hours=1))
            store.upsert_many((store.get(row.fingerprint)
                               .with_state(ProxyState.READY, reason="OK")
                               .graded(Grade.GOOD),))
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            assert len(leased) == 1
            live_lease = leased[0].lease_id

            probed = (claimed[0].record_success(NOW)
                      .with_state(ProxyState.READY, reason="OK")
                      .graded(Grade.GOOD))
            assert store.complete_probe(probed, now=NOW) is False, (
                "the write-back must refuse once the claim is gone"
            )
            after = store.get(row.fingerprint)
            assert after.state is ProxyState.LEASED
            assert after.lease_id == live_lease, (
                "the consumer's lease survived: this is the assertion that "
                "fails against the unconditional upsert_many path"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_the_naive_writeback_really_does_clobber(self):
        """
        NEGATIVE CONTROL, in the P05.T3 tradition.

        Without this, the two tests above prove only that MY code passes MY
        assertions. This runs the naive path -- probe, then `upsert_many` the
        pre-lease snapshot -- and asserts the lease IS destroyed. If a future
        change made the naive path safe, this test fails and tells us the
        negative control has stopped controlling for anything.
        """
        from atlas.tests.unit.naive_recheck import naive_writeback

        row = mk("10.0.0.3", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            snapshot = store.get(row.fingerprint)          # pre-lease copy
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            assert len(leased) == 1 and leased[0].lease_id

            naive_writeback(store, snapshot, now=NOW)

            after = store.get(row.fingerprint)
            assert after.lease_id is None and after.state is ProxyState.READY, (
                "the naive path is expected to clobber; if it no longer does, "
                "this negative control is no longer proving anything"
            )
            assert store.double_delivery_violations() == (), (
                "and the audit log is blind to it -- which is why the fix had to "
                "be in the write statement, not in a post-hoc check"
            )
        finally:
            store.close()
            tmp.cleanup()


# ── 2. the double probe ───────────────────────────────────────────────────────
class TestDoubleProbe:
    def test_two_passes_do_not_both_claim_the_same_row(self):
        row = mk("10.0.1.1", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            first = store.claim_for_probe((row.fingerprint,), now=NOW,
                                          probe_ms=60_000)
            second = store.claim_for_probe((row.fingerprint,), now=NOW,
                                           probe_ms=60_000)
            assert len(first) == 1
            assert second == (), (
                "measured before the fix: two consecutive passes both selected "
                "the same fingerprint and would each pay k=5 for one answer"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_a_claimed_row_is_not_offered_to_the_next_recheck(self):
        row = mk("10.0.1.2", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            clock = FakeClock()
            svc, eng = service(store, clock)
            first = asyncio.run(svc.run_once())
            assert first.claimed == 1 and first.applied == 1
            assert eng.seen == [row.fingerprint]

            # Second pass immediately after: the row was just checked, so it is
            # inside its freshness horizon and must not be probed again.
            second = asyncio.run(svc.run_once())
            assert second.selected == 0, (
                "a row rechecked one second ago is not due again"
            )
            assert eng.seen == [row.fingerprint], "probed exactly once"
        finally:
            store.close()
            tmp.cleanup()

    def test_a_leased_row_is_never_claimed_for_recheck(self):
        """H3: a leased row belongs to its consumer, not to the scheduler."""
        row = mk("10.0.1.3", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            assert len(leased) == 1
            claimed = store.claim_for_probe((row.fingerprint,), now=NOW,
                                            probe_ms=60_000)
            assert claimed == ()
            assert store.get(row.fingerprint).state is ProxyState.LEASED
        finally:
            store.close()
            tmp.cleanup()

    def test_a_retired_row_is_never_resurrected_by_a_claim(self):
        row = mk("10.0.1.4", state=ProxyState.COOLING,
                 last_checked=NOW - timedelta(hours=2)).retired(reason="done")
        store, _, tmp = store_with(row)
        try:
            assert store.claim_for_probe((row.fingerprint,), now=NOW,
                                         probe_ms=60_000) == ()
            assert store.get(row.fingerprint).state is ProxyState.RETIRED
        finally:
            store.close()
            tmp.cleanup()


# ── 3. PROBING must not become the new absorbing state ───────────────────────
class TestProbingIsNotAbsorbing:
    """
    ADR-036's defect was an absorbing `COOLING`. Adding `PROBING` without an exit
    would repeat it, so the reclaim was built in the same change and measured
    first: `probing_absorbing` recorded a row still IN_FLIGHT and unleasable a
    week later, with no reclaim method on the store.
    """

    def test_an_abandoned_probe_is_reclaimed(self):
        row = mk("10.0.2.1", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            assert store.get(row.fingerprint).state is ProxyState.PROBING
            # The worker is SIGKILLed: no finally, no release. Only the stored
            # deadline can recover this row (H8).
            n = store.reclaim_stale_probes(now=NOW + timedelta(minutes=5))
            assert n == 1
            after = store.get(row.fingerprint)
            assert after.state is ProxyState.COOLING
            assert after.reason_code == "PROBE_ABANDONED"
        finally:
            store.close()
            tmp.cleanup()

    def test_a_live_probe_is_not_reclaimed_from_under_itself(self):
        """The reclaim must not steal a claim that is still within its deadline."""
        row = mk("10.0.2.2", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=120_000)
            assert store.reclaim_stale_probes(now=NOW + timedelta(seconds=30)) == 0
            assert store.get(row.fingerprint).state is ProxyState.PROBING
        finally:
            store.close()
            tmp.cleanup()

    def test_reclaim_goes_to_cooling_not_ready(self):
        """
        An unfinished probe is not evidence of health.

        Reclaiming to READY would make the row leasable on the strength of a
        measurement that never completed -- H7's "live is not good" inverted.
        """
        row = mk("10.0.2.3", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            store.reclaim_stale_probes(now=NOW + timedelta(minutes=5))
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=1_000, now=NOW + timedelta(minutes=5))
            assert leased == (), "a reclaimed row must not be immediately leasable"
        finally:
            store.close()
            tmp.cleanup()

    def test_a_null_deadline_counts_as_expired(self):
        """
        A row left PROBING by a version that recorded no deadline must be
        recoverable. "No deadline" reads as "reclaim it", never "wait forever".
        """
        row = mk("10.0.2.4", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=120_000)
            store._db.execute(
                "UPDATE proxies SET probe_expires_at=NULL WHERE fingerprint=?",
                (row.fingerprint,))
            assert store.reclaim_stale_probes(now=NOW) == 1
        finally:
            store.close()
            tmp.cleanup()

    def test_the_recheck_pass_reclaims_before_it_plans(self):
        """
        A crashed worker's row must be a candidate for THIS pass, not the next.

        Otherwise every crash costs a full interval of that proxy's availability.
        """
        row = mk("10.0.2.5", last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            clock = FakeClock(NOW + timedelta(minutes=5))
            svc, eng = service(store, clock)
            report = asyncio.run(svc.run_once())
            assert report.reclaimed == 1
            assert report.claimed == 1, (
                "the reclaimed row was probed in the same pass that recovered it"
            )
            assert eng.seen == [row.fingerprint]
        finally:
            store.close()
            tmp.cleanup()


# ── 4. the pass itself ────────────────────────────────────────────────────────
class TestRunOnce:
    def test_a_cooling_row_past_its_cooldown_is_rechecked_and_can_return(self):
        """
        ADR-036's promise, now actually delivered end to end.

        Before P10 this row was selected by `decide()` as RECHECK and consumed by
        nobody, so "eligible again after a cooldown" was still not true in
        practice -- the exit existed but nothing walked through it.
        """
        failed = mk("10.0.3.1", state=ProxyState.COOLING,
                    consecutive_failures=1, grade=Grade.REJECTED,
                    last_checked=NOW - timedelta(minutes=10))
        store, _, tmp = store_with(failed)
        try:
            clock = FakeClock()
            svc, eng = service(store, clock, engine=StubEngine(admit=True))
            report = asyncio.run(svc.run_once())
            assert report.selected == 1 and report.applied == 1
            assert report.promoted == 1
            after = store.get(failed.fingerprint)
            assert after.state is ProxyState.READY, (
                "a recovered proxy is back in the pool: the whole point of "
                "ADR-036 having an exit from COOLING"
            )
            assert after.consecutive_failures == 0
        finally:
            store.close()
            tmp.cleanup()

    def test_a_still_failing_row_is_demoted_with_its_reason(self):
        failed = mk("10.0.3.2", state=ProxyState.COOLING,
                    consecutive_failures=1, grade=Grade.REJECTED,
                    last_checked=NOW - timedelta(minutes=10))
        store, _, tmp = store_with(failed)
        try:
            clock = FakeClock()
            svc, eng = service(store, clock,
                               engine=StubEngine(admit=False,
                                                 reason=ReasonCode.TCP_TIMEOUT))
            report = asyncio.run(svc.run_once())
            assert report.demoted == 1 and report.promoted == 0
            assert report.by_reason == {"TCP_TIMEOUT": 1}
            after = store.get(failed.fingerprint)
            assert after.state is ProxyState.COOLING
            assert after.consecutive_failures == 2, (
                "the failure counter advances, so the ADR-006 ladder and the "
                "retirement threshold both keep moving"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_recovered_rows_are_recheckable_repeatedly_until_retirement(self):
        """
        The absorbing-state property, from the pass's point of view: repeated
        failures must eventually RETIRE rather than loop forever.
        """
        failed = mk("10.0.3.3", state=ProxyState.COOLING,
                    consecutive_failures=1, grade=Grade.REJECTED,
                    last_checked=NOW - timedelta(hours=1))
        store, _, tmp = store_with(failed)
        try:
            clock = FakeClock()
            policy = SchedulerPolicy(retire_after_consecutive_failures=3)
            svc, eng = service(store, clock, policy=policy,
                               engine=StubEngine(admit=False))
            sched = PoolScheduler(store, clock, policy=policy)
            for _ in range(4):
                asyncio.run(svc.run_once())
                clock.advance(3600)
                plan = sched.plan()
                if plan.retire:
                    sched.apply_retirements(plan)
                    break
            after = store.get(failed.fingerprint)
            assert after.state is ProxyState.RETIRED, (
                "repeated failure terminates: RETIRED is the one absorbing state"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_the_budget_bounds_the_work(self):
        rows = tuple(mk(f"10.0.4.{i}", state=ProxyState.COOLING,
                        consecutive_failures=1, grade=Grade.REJECTED,
                        last_checked=NOW - timedelta(hours=1))
                     for i in range(1, 8))
        store, _, tmp = store_with(*rows)
        try:
            clock = FakeClock()
            svc, eng = service(store, clock)
            report = asyncio.run(svc.run_once(RecheckBudget(max_rechecks=3)))
            assert report.selected == 3
            assert report.claimed == 3
            assert len(eng.seen) == 3, (
                "ADR-027: assert the WORK, not just the ceiling -- a pass that "
                "probed nothing would satisfy `<= 3`"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_an_empty_pool_is_not_an_error(self):
        store, _, tmp = store_with()
        try:
            svc, eng = service(store, FakeClock())
            report = asyncio.run(svc.run_once())
            assert report.selected == 0 and report.applied == 0
            assert eng.seen == []
        finally:
            store.close()
            tmp.cleanup()

    def test_failed_rows_are_recovered_before_healthy_ones_are_refreshed(self):
        """
        Under a budget too small for both, recovering lost capacity beats
        refreshing capacity that still works.
        """
        cooling = mk("10.0.5.1", state=ProxyState.COOLING,
                     consecutive_failures=1, grade=Grade.REJECTED,
                     last_checked=NOW - timedelta(hours=1))
        stale_ready = mk("10.0.5.2", last_checked=NOW - timedelta(hours=1))
        store, _, tmp = store_with(stale_ready, cooling)
        try:
            svc, eng = service(store, FakeClock())
            asyncio.run(svc.run_once(RecheckBudget(max_rechecks=1)))
            assert eng.seen == [cooling.fingerprint], (
                "the COOLING row is outside the pool; the READY one is serving"
            )
        finally:
            store.close()
            tmp.cleanup()


# ── 5. the report polices itself ─────────────────────────────────────────────
class TestAccounting:
    def test_a_real_lost_writeback_is_counted_by_run_once(self):
        """
        MUTATION-DRIVEN (P10.T1). The counter must be fed by the STORE's answer.

        Every other test in this class CONSTRUCTS a report and checks
        `__post_init__`. That is why `mutate_recheck.py` found a survivor:
        forcing the write-back branch to be taken unconditionally
        (`complete_probe(...) or True`) keeps `applied + lost_writeback ==
        claimed` perfectly self-consistent -- it just moves a row from the
        second counter to the first. A hand-built report cannot see that,
        because the mutation is in the code that DECIDES which counter to
        increment, not in the identity that checks them.

        This is the ADR-035 lesson recurring: a test that restates the
        arithmetic instead of driving the real path measures the test. So here a
        probe genuinely loses its claim mid-pass and the report must say so.
        """
        row = mk("10.0.0.9", state=ProxyState.COOLING,
                 consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            clock = FakeClock()

            class StealingEngine(StubEngine):
                """Probes, and while 'probing' the row is stolen by a consumer."""

                def __init__(self, store) -> None:
                    super().__init__(admit=True)
                    self._store = store

                async def evaluate(self, proxy: Proxy):
                    out = await super().evaluate(proxy)
                    # The claim lapses and someone else takes the row: exactly
                    # the residual race ADR-038 does not claim to abolish.
                    self._store.reclaim_stale_probes(
                        now=NOW + timedelta(hours=1))
                    self._store.upsert_many(
                        (self._store.get(proxy.fingerprint)
                         .with_state(ProxyState.READY, reason="OK")
                         .graded(Grade.GOOD),))
                    self._store.lease(count=1, min_grade=Grade.USABLE,
                                      lease_ms=60_000, now=NOW)
                    return out

            svc, _ = service(store, clock, engine=StealingEngine(store))
            report = asyncio.run(svc.run_once())

            assert report.claimed == 1
            assert report.lost_writeback == 1, (
                "the store refused the write-back, so the report must count it "
                "as lost -- reporting it as applied claims a refresh that "
                "never landed"
            )
            assert report.applied == 0
            assert report.promoted == 0, (
                "a row whose write-back was refused was not promoted; counting "
                "it would overstate the pool"
            )
            # And the winner keeps the row.
            assert store.get(row.fingerprint).state is ProxyState.LEASED
        finally:
            store.close()
            tmp.cleanup()

    def test_a_row_lost_at_the_claim_is_refused(self):
        with pytest.raises(ValueError, match="lost rows at the claim"):
            RecheckReport(selected=5, claimed=2, lost_claim=1, applied=2)

    def test_a_row_lost_at_the_writeback_is_refused(self):
        with pytest.raises(ValueError, match="lost rows at the write-back"):
            RecheckReport(selected=3, claimed=3, applied=1, lost_writeback=1)

    def test_a_consistent_report_is_accepted(self):
        r = RecheckReport(selected=4, claimed=3, lost_claim=1, applied=2,
                          lost_writeback=1)
        assert r.contention == 2

    def test_contention_separates_cheap_from_expensive_losses(self):
        """
        Losing a row BEFORE probing costs nothing; losing it after costs k=5.
        One combined counter would hide which is happening.
        """
        cheap = RecheckReport(selected=2, claimed=0, lost_claim=2)
        dear = RecheckReport(selected=2, claimed=2, applied=0, lost_writeback=2)
        assert cheap.contention == dear.contention == 2
        assert cheap.lost_writeback == 0 and dear.lost_claim == 0

    @pytest.mark.parametrize("field,value", [
        ("max_rechecks", 0), ("concurrency", 0), ("probe_ms", 0),
        ("max_rechecks", -1),
    ])
    def test_a_nonsense_budget_is_refused(self, field, value):
        with pytest.raises(ValueError, match=field):
            RecheckBudget(**{field: value})

    def test_a_nonpositive_probe_deadline_is_refused_by_the_store(self):
        """A zero-length claim would expire the instant it was taken."""
        store, _, tmp = store_with(mk("10.0.6.1"))
        try:
            with pytest.raises(ValueError, match="probe_ms"):
                store.claim_for_probe(("x",), now=NOW, probe_ms=0)
        finally:
            store.close()
            tmp.cleanup()

    def test_claiming_nothing_is_a_noop_not_a_full_table_claim(self):
        """
        An empty fingerprint tuple must claim NOTHING.

        `delete_many` guards the same shape, because `IN ()` is a syntax error in
        SQLite and a naive f-string build would otherwise produce a statement
        matching every row.
        """
        rows = tuple(mk(f"10.0.7.{i}") for i in range(1, 4))
        store, _, tmp = store_with(*rows)
        try:
            assert store.claim_for_probe((), now=NOW, probe_ms=1000) == ()
            states = {p.state for p in
                      store.select_schedulable(limit=10)}
            assert states == {ProxyState.READY}
        finally:
            store.close()
            tmp.cleanup()


# ── 6. the migration ─────────────────────────────────────────────────────────
class TestMigration:
    def test_a_pool_created_before_adr038_gains_the_new_column(self):
        """
        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
        column is silently absent from every database already on disk and the
        first query naming it fails at runtime. H8 is about not losing a pool to
        a crash; losing one to an upgrade is the same outcome by a slower route.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            import sqlite3
            db = sqlite3.connect(str(path))
            db.executescript("""
                CREATE TABLE proxies (
                    fingerprint TEXT PRIMARY KEY, host TEXT NOT NULL,
                    port INTEGER NOT NULL, protocol TEXT NOT NULL,
                    labelled_protocol TEXT NOT NULL, anonymity TEXT NOT NULL,
                    state TEXT NOT NULL, grade TEXT NOT NULL,
                    samples_ms TEXT NOT NULL DEFAULT '', p50_ms REAL,
                    p95_ms REAL, mean_ms REAL, stdev_ms REAL,
                    success_ratio REAL, source_id TEXT, country TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    total_successes INTEGER NOT NULL DEFAULT 0,
                    total_attempts INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT, last_checked TEXT, lease_id TEXT,
                    lease_expires_at TEXT, reason_code TEXT
                );
            """)
            db.execute(
                "INSERT INTO proxies (fingerprint, host, port, protocol, "
                "labelled_protocol, anonymity, state, grade) VALUES "
                "('fp1','1.1.1.1',80,'http','unknown','unknown','READY','GOOD')")
            db.commit()
            db.close()

            store = SqliteStore(path)
            try:
                cols = {r["name"] for r in store._db.execute(
                    "PRAGMA table_info(proxies)").fetchall()}
                assert "probe_expires_at" in cols
                # and the pre-existing row is intact and still usable
                assert store.pool_size() == 1
                claimed = store.claim_for_probe(("fp1",), now=NOW,
                                                probe_ms=1000)
                assert len(claimed) == 1
            finally:
                store.close()

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "twice.db"
            SqliteStore(path).close()
            store = SqliteStore(path)          # opened again: must not raise
            try:
                assert store.pool_size() == 0
            finally:
                store.close()


# ══════════════════════════════════════════════════════════════════════════════
# P11 / ADR-039 — the two safety bounds P10 deferred.
#
# Both were MEASURED before either was fixed (`engineering/raw/recheck_bounds.json`)
# and both were worse than P10's prose implied:
#
#   * probe_ms: worst case 590 000 ms vs a 120 000 ms default -- the guard was
#     absent AND the value it failed to check was already wrong by ~5x.
#   * abandoned rechecks: 12 claim->reclaim cycles, `consecutive_failures` never
#     left 0, never retired. The abandon path recorded NOTHING.
#
# Every guard below has a negative control, because this project has twice shipped
# a guard that could not fail (ADR-012's empty glob, ADR-023's self-verifying
# docstring). A green assertion is not evidence until it is shown to catch the
# broken version.
# ══════════════════════════════════════════════════════════════════════════════
from atlas.core.ports.probe import (  # noqa: E402
    PROTOCOL_LADDER, ProbeBound, ProbePlan, claim_bound,
    default_target_timeout_ms,
)

# The artifact measured only the rungs the aiohttp adapter can currently TEST
# (http, https -- aiohttp-socks is not installed), while `claim_bound` prices
# every rung in `PROTOCOL_LADDER` on purpose. Named here so the two numbers in
# the tests below are traceable to that difference rather than looking like one
# of them is a typo.
_MEASURED_TESTABLE_RUNGS = 2


class TestProbeLifetimeBound:
    """
    A live probe must not be able to outlive its own PROBING claim.
    """

    def test_the_bound_is_derived_from_the_real_probe_plan(self):
        """
        The bound must be computed from the plan, not restated as a literal.

        Recomputed here from ProbePlan's OWN fields rather than asserted against
        a hardcoded 59 000. Pinning the literal would make this test a copy of
        the answer -- ADR-023 exactly: a guard that verifies its own restatement
        of the code and therefore cannot detect drift.
        """
        plan = ProbePlan()
        t = default_target_timeout_ms()
        bound = claim_bound(plan, target_timeout_ms=t, batch=1, concurrency=1)
        expected = (plan.tcp_timeout_ms
                    + len(PROTOCOL_LADDER) * t
                    + plan.samples * plan.per_sample_timeout_ms)
        assert bound.per_probe_ms == expected
        assert bound.required_ms == expected

    def test_the_bound_grows_with_k(self):
        """
        k is ADR-003's whole subject; a bound that ignored it would be decorative.
        """
        t = default_target_timeout_ms()
        k5 = claim_bound(ProbePlan(samples=5), target_timeout_ms=t,
                         batch=1, concurrency=1)
        k10 = claim_bound(ProbePlan(samples=10), target_timeout_ms=t,
                          batch=1, concurrency=1)
        assert k10.per_probe_ms > k5.per_probe_ms
        assert (k10.per_probe_ms - k5.per_probe_ms
                == 5 * ProbePlan().per_sample_timeout_ms)

    def test_the_bound_accounts_for_queueing_behind_the_semaphore(self):
        """
        THE FACTOR THE OLD DEFAULT MISSED.

        One claim statement covers the whole batch, but only `concurrency` probes
        run at once, so the last wave sits inside a claim taken at T. Missing this
        is what made 120 000 ms wrong by ~5x rather than by a little.
        """
        t = default_target_timeout_ms()
        one = claim_bound(ProbePlan(), target_timeout_ms=t, batch=10,
                          concurrency=10)
        ten = claim_bound(ProbePlan(), target_timeout_ms=t, batch=100,
                          concurrency=10)
        assert one.waves == 1
        assert ten.waves == 10
        assert ten.required_ms == 10 * one.required_ms

    def test_partial_waves_round_up(self):
        """
        A batch of 11 at concurrency 10 needs TWO waves. Truncating would
        under-size the claim for the eleventh row -- the exact off-by-one that
        makes a bound almost right.
        """
        t = default_target_timeout_ms()
        assert claim_bound(ProbePlan(), target_timeout_ms=t, batch=11,
                           concurrency=10).waves == 2

    def test_an_undersized_claim_is_refused(self):
        """
        THE REGRESSION TEST P11 ASKS FOR.

        The old default is the case that matters: it was ACCEPTED by P10's
        `>= 1` check while being 470 000 ms short of the real worst case.
        """
        with pytest.raises(ValueError, match="shorter than the worst-case probe"):
            RecheckBudget(probe_ms=120_000)

    def test_the_bound_reproduces_the_measured_number_rung_for_rung(self):
        """
        Ties the bound to `recheck_bounds.json` so the number is not folklore.

        THE ARTIFACT AND THE CODE DISAGREE ON PURPOSE, and this test pins the
        disagreement instead of averaging over it. The measurement priced the two
        rungs the adapter can currently TEST (http, https -- aiohttp-socks is not
        installed, so the SOCKS rungs cost ~0), giving 59 000 ms per probe and
        590 000 ms required. `claim_bound` prices all four rungs -- 75 000 and
        750 000 -- because the adapter's own error message invites aiohttp-socks
        to be installed, and on that day two free rungs silently become two real
        requests.

        My first version of this test asserted 590 000 against the live code and
        failed. That failure was the test being wrong, not the code: pinning the
        measured number would have quietly converted a deliberate safety margin
        into a regression the suite demanded. So the assertion is now that the
        code EQUALS the measurement when restricted to the measured rungs, and
        EXCEEDS it otherwise -- which catches drift in the arithmetic while
        leaving the conservatism free to be conservative.
        """
        b = RecheckBudget()
        as_measured = claim_bound(
            b.plan, target_timeout_ms=b.target_timeout_ms,
            batch=b.max_rechecks, concurrency=b.concurrency,
            protocol_rungs=_MEASURED_TESTABLE_RUNGS)
        assert as_measured.per_probe_ms == 59_000, (
            "recheck_bounds.json measured 59 000 ms per probe over 2 testable "
            "rungs; if the plan changed, re-measure rather than editing this"
        )
        assert as_measured.required_ms == 590_000
        assert b.claim_bound().required_ms > as_measured.required_ms, (
            "pricing all 4 ladder rungs must be strictly more conservative than "
            "pricing the 2 currently-testable ones"
        )
        assert b.probe_ms == b.claim_bound().required_ms, (
            "the default must BE the bound, not a number near it"
        )

    def test_the_old_default_is_undersized_on_either_reading(self):
        """
        The 120 000 ms default was wrong even by the MOST generous accounting.

        Stated separately so the refusal does not rest on the conservative rung
        count: if someone later installs aiohttp-socks or trims the ladder, the
        old default is still short, and this test keeps saying so.
        """
        b = RecheckBudget()
        generous = claim_bound(
            b.plan, target_timeout_ms=b.target_timeout_ms,
            batch=b.max_rechecks, concurrency=b.concurrency,
            protocol_rungs=1)
        assert generous.required_ms > 120_000, (
            "even pricing a single protocol rung, the old default is too short"
        )

    def test_a_sufficient_claim_is_accepted(self):
        """
        The negative-control half of the guard: it must not reject everything.

        A validator that refuses all input would pass the test above while making
        the recheck unusable -- "the guard fires" and "the guard discriminates"
        are different claims and both need evidence.
        """
        exact = RecheckBudget().claim_bound().required_ms
        assert RecheckBudget(probe_ms=exact).probe_ms == exact
        assert RecheckBudget(probe_ms=exact + 1).probe_ms == exact + 1

    def test_the_default_is_derived_not_hardcoded(self):
        """
        Change the plan and the default must FOLLOW it, with no edit here.

        This is what makes "no duplicated magic number" testable rather than
        asserted: a hand-picked default would stay at its old value and fail.
        """
        slow = RecheckBudget(plan=ProbePlan(samples=20))
        assert slow.probe_ms == slow.claim_bound().required_ms
        assert slow.probe_ms > RecheckBudget().probe_ms

    def test_probe_ms_below_one_is_still_refused(self):
        """
        P10's floor is KEPT. A claim of 0 ms has already expired when taken, and
        that is a distinct kind of nonsense worth its own message.
        """
        with pytest.raises(ValueError, match="probe_ms must be >= 1"):
            RecheckBudget(probe_ms=0, plan=ProbePlan(samples=1, tcp_timeout_ms=1,
                                                     per_sample_timeout_ms=1),
                          target_timeout_ms=1, max_rechecks=1, concurrency=1)

    def test_the_adapter_and_the_bound_share_one_ladder(self):
        """
        NEGATIVE CONTROL for the duplicated-magic-number requirement.

        If the adapter kept its own ladder, adding a rung would leave the bound
        short by one target timeout per probe and nothing would say so. This
        asserts they are the SAME OBJECT, not merely equal.
        """
        from atlas.adapters import probe_aiohttp
        assert probe_aiohttp._PROTOCOL_LADDER is PROTOCOL_LADDER

    def test_an_inconsistent_bound_cannot_be_constructed(self):
        with pytest.raises(ValueError, match="inconsistent bound"):
            ProbeBound(per_probe_ms=100, waves=2, required_ms=999)

    def test_the_claim_outlives_a_probe_that_takes_the_worst_case(self):
        """
        THE PROPERTY ITSELF, end to end: a probe that runs for the full worst case
        must still hold its claim when it writes back.

        Not a unit test of arithmetic -- it drives the real store with a clock
        advanced by exactly the worst case and asserts the write-back still lands.
        """
        row = mk("10.0.9.9", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            budget = RecheckBudget()
            worst = budget.claim_bound().required_ms
            store.claim_for_probe((row.fingerprint,), now=NOW,
                                  probe_ms=budget.probe_ms)
            # One millisecond inside the deadline. NOT `at_limit == deadline`:
            # the store reclaims on `probe_expires_at <= now`, so the deadline
            # instant itself is expired -- see the convention test below, which
            # exists because my first draft of this test assumed the opposite and
            # would have had me "fix" the store to match the test.
            at_limit = NOW + timedelta(milliseconds=worst - 1)
            assert store.reclaim_stale_probes(now=at_limit) == 0, (
                "the claim expired while a probe was still legitimately running"
            )
            probed = store.get(row.fingerprint)
            assert store.complete_probe(
                probed.record_success(at_limit)
                     .with_state(ProxyState.READY, reason="OK")
                     .graded(Grade.GOOD),
                now=at_limit) is True
        finally:
            store.close()
            tmp.cleanup()

    def test_the_deadline_instant_itself_counts_as_expired(self):
        """
        THE CONVENTION, PINNED — and the reason the test above uses `worst - 1`.

        `reclaim_stale_probes` matches `probe_expires_at <= now`, so a claim is
        dead AT its deadline, not one tick after. This is worth its own test
        because I got it wrong first: my draft asserted that a probe running for
        exactly the worst case still held its claim, it failed, and the tempting
        response was to widen the store's comparison to `<`. That would have
        edited proven code to satisfy a newer test's assumption.

        Both readings are defensible in isolation; what settles it is that
        `expire_leases` -- covered by the H3 concurrency work -- already uses
        `lease_expires_at <= now`. Two deadline mechanisms in one store
        disagreeing about whether a boundary is inclusive is precisely the kind
        of near-invisible inconsistency this project keeps finding after the
        fact, so the probe path follows the lease path and this test says so out
        loud.
        """
        row = mk("10.0.9.11", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            deadline = NOW + timedelta(milliseconds=1_000)
            assert store.reclaim_stale_probes(
                now=deadline - timedelta(milliseconds=1)) == 0
            assert store.reclaim_stale_probes(now=deadline) == 1, (
                "the deadline instant must be treated as expired, matching "
                "expire_leases' `lease_expires_at <= now`"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_probe_and_lease_expiry_share_one_boundary_convention(self):
        """
        NEGATIVE CONTROL for the reasoning above, asserted against the SQL.

        If someone changes one comparison and not the other, the two deadline
        mechanisms drift apart and nothing else in the suite notices -- each path
        keeps passing its own tests. Reads the source of both methods and
        requires the same operator.
        """
        import inspect
        probe_sql = inspect.getsource(SqliteStore.reclaim_stale_probes)
        lease_sql = inspect.getsource(SqliteStore.expire_leases)
        assert "probe_expires_at <= ?" in probe_sql
        assert "lease_expires_at <= ?" in lease_sql, (
            "the lease path's boundary changed; the probe path above was "
            "justified by matching it, so they must be reconciled together"
        )

    def test_negative_control_a_short_claim_really_does_get_reclaimed(self):
        """
        NEGATIVE CONTROL for the test above: with an undersized claim (the one
        `RecheckBudget` now refuses), the same probe IS reclaimed mid-flight and
        the write-back is LOST. This is the defect, demonstrated.
        """
        row = mk("10.0.9.10", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            worst = RecheckBudget().claim_bound().required_ms
            store.claim_for_probe((row.fingerprint,), now=NOW,
                                  probe_ms=120_000)      # the old default
            probed = store.get(row.fingerprint)
            at_limit = NOW + timedelta(milliseconds=worst)
            assert store.reclaim_stale_probes(now=at_limit) == 1, (
                "the undersized claim should have been reclaimed mid-probe"
            )
            assert store.complete_probe(
                probed.record_success(at_limit)
                     .with_state(ProxyState.READY, reason="OK")
                     .graded(Grade.GOOD),
                now=at_limit) is False, (
                "the probe's measurement was silently lost -- and the row is now "
                "claimable by a second worker, which is the double probe"
            )
        finally:
            store.close()
            tmp.cleanup()


class TestAbandonedRechecksAreCounted:
    """
    The second half of ADR-039: an abandoned probe must LEAVE A TRACE.

    Measured before the counter existed (`recheck_bounds.json`): 12
    claim->reclaim cycles, `consecutive_failures` and `total_attempts` both still
    0, `decide()` returning RECHECK every time, `ever_retired: false`. The abandon
    path recorded nothing, so no threshold expressed in terms of the existing
    counters could ever bound the cycle. A permanently crashing proxy was an
    infinite claim loop that looked, from every counter in the row, like a proxy
    nothing had happened to.
    """

    def test_reclaiming_an_abandoned_probe_increments_the_counter(self):
        row = mk("10.0.10.1", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            assert store.get(row.fingerprint).abandoned_rechecks == 0
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            store.reclaim_stale_probes(now=NOW + timedelta(seconds=2))
            assert store.get(row.fingerprint).abandoned_rechecks == 1, (
                "the abandon path must record the abandonment, or no threshold "
                "can ever see it (recheck_bounds.json: 12 cycles, counter 0)"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_the_counter_survives_the_roundtrip_through_the_row(self):
        """
        Persistence, not just an in-memory field.

        A counter that lived only in Python would reset on restart, and the whole
        point is to bound a loop that spans crashes.
        """
        row = mk("10.0.10.2", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=2, last_checked=NOW - timedelta(hours=2))
        store, path, tmp = store_with(row)
        try:
            store.close()
            reopened = SqliteStore(path / "pool.db")
            try:
                assert reopened.get(row.fingerprint).abandoned_rechecks == 2
            finally:
                reopened.close()
        finally:
            tmp.cleanup()

    def test_repeated_abandonment_accumulates(self):
        """
        Three crash cycles must read as three, not as one repeated.
        """
        row = mk("10.0.10.3", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            for i in range(3):
                store.claim_for_probe((row.fingerprint,), now=NOW,
                                      probe_ms=1_000)
                store.reclaim_stale_probes(now=NOW + timedelta(seconds=2))
                assert store.get(row.fingerprint).abandoned_rechecks == i + 1
        finally:
            store.close()
            tmp.cleanup()

    def test_a_second_reclaim_does_not_double_count_one_abandonment(self):
        """
        Idempotence, which the `state='PROBING'` predicate is what provides.

        Reclaim is called once per pass and a pass can overlap another; a counter
        that advanced twice for one claim would retire healthy proxies early.
        """
        row = mk("10.0.10.4", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=1_000)
            later = NOW + timedelta(seconds=2)
            assert store.reclaim_stale_probes(now=later) == 1
            assert store.reclaim_stale_probes(now=later) == 0
            assert store.get(row.fingerprint).abandoned_rechecks == 1, (
                "the row left PROBING on the first reclaim, so the second must "
                "match nothing rather than advance the counter again"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_a_completed_probe_clears_the_counter(self):
        """
        CONSECUTIVE, as the field's docstring claims. A cumulative counter would
        retire a healthy long-lived proxy for unrelated crashes spread over weeks
        -- a restart policy masquerading as a proxy-quality decision.
        """
        row = mk("10.0.10.5", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=2, last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=600_000)
            probed = store.get(row.fingerprint)
            assert store.complete_probe(
                probed.record_success(NOW)
                     .with_state(ProxyState.READY, reason="OK")
                     .graded(Grade.GOOD), now=NOW) is True
            assert store.get(row.fingerprint).abandoned_rechecks == 0
        finally:
            store.close()
            tmp.cleanup()

    def test_a_completed_FAILURE_also_clears_the_counter(self):
        """
        A probe that REPORTED is not abandoning its claim, even if it reported bad
        news. `record_failure` clears the abandon counter and advances the failure
        one, so the row retires on the ladder that describes what actually
        happened.
        """
        row = mk("10.0.10.6", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=2, last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=600_000)
            probed = store.get(row.fingerprint)
            assert store.complete_probe(
                probed.record_failure(NOW, reason="TOO_SLOW_P95")
                     .with_state(ProxyState.COOLING, reason="TOO_SLOW_P95")
                     .graded(Grade.REJECTED), now=NOW) is True
            after = store.get(row.fingerprint)
            assert after.abandoned_rechecks == 0
            assert after.consecutive_failures == 2
        finally:
            store.close()
            tmp.cleanup()


class TestAbandonedRetirement:
    """
    The threshold that actually bounds the loop.
    """

    def test_a_row_at_the_threshold_is_retired(self):
        policy = SchedulerPolicy()
        row = mk("10.0.11.1", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            assert store.retire_abandoned(
                threshold=policy.retire_after_abandoned_rechecks, now=NOW) == 1
            after = store.get(row.fingerprint)
            assert after.state is ProxyState.RETIRED
            assert after.reason_code == "RETIRED_ABANDONED_RECHECKS", (
                "the retirement must name the abandon ladder, not be filed "
                "under a generic failure -- otherwise the two causes are "
                "indistinguishable in the pool's own records"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_a_row_below_the_threshold_is_left_alone(self):
        """
        NEGATIVE CONTROL: a retirement that fired on everything would pass the
        test above while emptying the pool.
        """
        policy = SchedulerPolicy()
        row = mk("10.0.11.2", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks - 1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            assert store.retire_abandoned(
                threshold=policy.retire_after_abandoned_rechecks, now=NOW) == 0
            assert store.get(row.fingerprint).state is ProxyState.COOLING
        finally:
            store.close()
            tmp.cleanup()

    def test_a_leased_row_is_never_retired_by_the_abandon_ladder(self):
        """
        H3. The row belongs to a consumer that is using it right now; retiring it
        would be the clobber defect arriving from a new direction.
        """
        row = mk("10.0.11.3", abandoned_rechecks=99)
        store, _, tmp = store_with(row)
        try:
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            assert len(leased) == 1
            assert store.retire_abandoned(threshold=1, now=NOW) == 0
            assert store.get(row.fingerprint).state is ProxyState.LEASED
        finally:
            store.close()
            tmp.cleanup()

    def test_a_probing_row_is_never_retired_from_under_a_live_probe(self):
        """
        Retiring mid-probe would make `complete_probe` silently no-op -- its
        `WHERE state='PROBING'` would stop matching -- turning a measured result
        into a lost one. A counter-driven cleanup must not be able to discard
        work that is still in flight.
        """
        row = mk("10.0.11.4", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=99, last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            store.claim_for_probe((row.fingerprint,), now=NOW, probe_ms=600_000)
            assert store.retire_abandoned(threshold=1, now=NOW) == 0
            probed = store.get(row.fingerprint)
            assert store.complete_probe(
                probed.record_success(NOW)
                     .with_state(ProxyState.READY, reason="OK")
                     .graded(Grade.GOOD), now=NOW) is True, (
                "the in-flight probe's write-back must still land"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_an_already_retired_row_is_not_retired_twice(self):
        """
        Otherwise the count inflates with rows retired passes ago and the report
        stops meaning "retired this pass".
        """
        row = mk("10.0.11.5", state=ProxyState.RETIRED, abandoned_rechecks=99,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            assert store.retire_abandoned(threshold=1, now=NOW) == 0
        finally:
            store.close()
            tmp.cleanup()

    def test_a_row_PAST_the_threshold_still_retires(self):
        """
        `>=`, NOT `==` -- and this test exists because an injection proved nothing
        was checking it.

        `retire_abandoned`'s docstring already argued the point ("a row that
        somehow passed the threshold ... must still retire. An equality test is
        how a guard silently stops firing"), but rewriting the SQL to
        `abandoned_rechecks = ?` left all 66 tests green. The reasoning was
        documented and unverified, which is the ADR-023 pattern: prose that
        describes a guarantee the suite does not actually hold anyone to.

        The overshoot is reachable in practice: lowering
        `retire_after_abandoned_rechecks` in config leaves existing rows above
        the new threshold, and with `==` every one of them becomes permanently
        unretirable -- the exact unbounded loop ADR-039 closed, re-entered
        through a config edit.
        """
        row = mk("10.0.11.9", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=7, last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            assert store.retire_abandoned(threshold=3, now=NOW) == 1, (
                "a row at 7 abandonments against a threshold of 3 must retire; "
                "an equality test would skip it forever"
            )
            assert store.get(row.fingerprint).state is ProxyState.RETIRED
        finally:
            store.close()
            tmp.cleanup()

    def test_decide_also_retires_a_row_past_the_threshold(self):
        """
        The same overshoot on the POLICY side, for the same reason.
        """
        from atlas.core.policy.lifecycle import PoolAction, decide
        policy = SchedulerPolicy(retire_after_abandoned_rechecks=3)
        row = mk("10.0.11.10", state=ProxyState.COOLING,
                 consecutive_failures=0, abandoned_rechecks=9,
                 last_checked=NOW - timedelta(hours=2))
        assert decide(row, policy, now=NOW) is PoolAction.RETIRE

    def test_a_nonsense_threshold_is_refused(self):
        store, _, tmp = store_with()
        try:
            with pytest.raises(ValueError, match="threshold must be >= 1"):
                store.retire_abandoned(threshold=0, now=NOW)
        finally:
            store.close()
            tmp.cleanup()

    def test_decide_retires_a_row_that_has_abandoned_enough_rechecks(self):
        """
        The POLICY branch, not just the SQL: `decide()` must stop returning
        RECHECK for a row that has earned retirement, or the scheduler keeps
        spending a full claim to re-learn a recorded fact.
        """
        from atlas.core.policy.lifecycle import PoolAction, decide
        policy = SchedulerPolicy()
        row = mk("10.0.11.6", state=ProxyState.COOLING, consecutive_failures=0,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks,
                 last_checked=NOW - timedelta(hours=2))
        assert decide(row, policy, now=NOW) is PoolAction.RETIRE

    def test_decide_still_rechecks_a_row_below_the_threshold(self):
        """
        NEGATIVE CONTROL for the branch above.
        """
        from atlas.core.policy.lifecycle import PoolAction, decide
        policy = SchedulerPolicy()
        row = mk("10.0.11.7", state=ProxyState.COOLING, consecutive_failures=0,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks - 1,
                 last_checked=NOW - timedelta(hours=2))
        assert decide(row, policy, now=NOW) is PoolAction.RECHECK

    def test_the_two_thresholds_are_independent_knobs(self):
        """
        `retire_after_abandoned_rechecks` must not be derived from
        `retire_after_consecutive_failures`. Tying them would mean one config
        change silently retuned two different policies -- and they count
        genuinely different events (a probe that RAN vs one that never reported).
        """
        from atlas.core.policy.lifecycle import PoolAction, decide
        policy = SchedulerPolicy(retire_after_consecutive_failures=99)
        row = mk("10.0.11.8", state=ProxyState.COOLING, consecutive_failures=0,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks,
                 last_checked=NOW - timedelta(hours=2))
        assert decide(row, policy, now=NOW) is PoolAction.RETIRE, (
            "raising the FAILURE threshold must not disable the ABANDON ladder"
        )


class TestAbandonLoopIsBounded:
    """
    THE PROPERTY THE ARTIFACT SHOWED WAS FALSE: the loop must terminate.
    """

    def test_a_permanently_crashing_proxy_eventually_retires(self):
        """
        Drives the SAME 12 claim->reclaim cycles the artifact recorded and asserts
        the row is retired by the end. Before ADR-039 this ran forever with every
        counter reading zero.
        """
        policy = SchedulerPolicy()
        row = mk("10.0.12.1", state=ProxyState.COOLING, consecutive_failures=1,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            retired_at = None
            for cycle in range(12):
                now = NOW + timedelta(minutes=30 * cycle)
                store.reclaim_stale_probes(now=now)
                if store.retire_abandoned(
                        threshold=policy.retire_after_abandoned_rechecks,
                        now=now):
                    retired_at = cycle
                    break
                # the worker claims and then dies without reporting
                store.claim_for_probe((row.fingerprint,), now=now,
                                      probe_ms=1_000)
            assert retired_at is not None, (
                "12 crash cycles and the row never retired -- exactly the "
                "unbounded loop recorded in recheck_bounds.json"
            )
            assert store.get(row.fingerprint).state is ProxyState.RETIRED
            assert retired_at <= policy.retire_after_abandoned_rechecks + 1, (
                f"retired only after {retired_at} cycles, which is more claims "
                "than the threshold should ever allow"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_alternating_abandon_and_failure_still_retires(self):
        """
        THE TEST `record_failure`'s DOCSTRING ALREADY PROMISED BY NAME.

        Found by reading that docstring during this pass: it cited this test as
        pinning the alternating case, and the test did not exist -- the ADR-026
        defect class (a citation pointing at nothing), which the gate cannot see
        because `check_adr_claims_are_verifiable` only walks ADR -> code.

        The case is the one where each guard alone is insufficient. Abandon, then
        fail, then abandon: every completed failure clears `abandoned_rechecks`,
        and every abandonment leaves `consecutive_failures` untouched. So a proxy
        alternating between the two advances NEITHER counter monotonically, and a
        reader could reasonably conclude it cycles forever. It does not, because
        `consecutive_failures` is cleared only by SUCCESS -- so the failures
        accumulate across the abandonments and retire it on the failure ladder.
        That reasoning is exactly the kind this project has been wrong about
        before, so it is measured here rather than argued.
        """
        policy = SchedulerPolicy()
        row = mk("10.0.12.2", state=ProxyState.COOLING, consecutive_failures=0,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            from atlas.core.policy.lifecycle import PoolAction, decide
            retired = False
            for cycle in range(40):
                now = NOW + timedelta(minutes=30 * cycle)
                store.reclaim_stale_probes(now=now)
                if store.retire_abandoned(
                        threshold=policy.retire_after_abandoned_rechecks,
                        now=now):
                    retired = True
                    break
                current = store.get(row.fingerprint)
                if decide(current, policy, now=now) is PoolAction.RETIRE:
                    retired = True
                    break
                if cycle % 2 == 0:
                    # abandon: claim and die
                    store.claim_for_probe((row.fingerprint,), now=now,
                                          probe_ms=1_000)
                else:
                    # a probe that REPORTS a failure (clears abandoned_rechecks)
                    store.claim_for_probe((row.fingerprint,), now=now,
                                          probe_ms=600_000)
                    probed = store.get(row.fingerprint)
                    store.complete_probe(
                        probed.record_failure(now, reason="TOO_SLOW_P95")
                             .with_state(ProxyState.COOLING,
                                         reason="TOO_SLOW_P95")
                             .graded(Grade.REJECTED), now=now)
            assert retired, (
                "alternating abandon/failure never retired: each ladder was "
                "reset by the other, which is the seam both thresholds exist "
                "to close"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_run_once_reports_what_it_retired(self):
        """
        The pass must ACCOUNT for the retirement, not perform it silently.
        """
        policy = SchedulerPolicy()
        row = mk("10.0.12.3", state=ProxyState.COOLING, consecutive_failures=1,
                 abandoned_rechecks=policy.retire_after_abandoned_rechecks,
                 last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(row)
        try:
            svc, eng = service(store, FakeClock(), policy=policy)
            report = asyncio.run(svc.run_once())
            assert report.retired_abandoned == 1
            assert eng.seen == [], (
                "a row retired for abandonment must not also be re-probed in "
                "the same pass -- that is the claim the threshold exists to "
                "stop spending"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_retirement_runs_before_planning_so_it_cannot_be_bypassed(self):
        """
        ORDERING, asserted through observable behaviour rather than by reading
        `run_once`. If retirement ran after `plan()`, the row would already be a
        candidate and would take one more claim before dying.
        """
        policy = SchedulerPolicy()
        doomed = mk("10.0.12.4", state=ProxyState.COOLING,
                    consecutive_failures=1,
                    abandoned_rechecks=policy.retire_after_abandoned_rechecks,
                    last_checked=NOW - timedelta(hours=2))
        healthy = mk("10.0.12.5", state=ProxyState.COOLING,
                     consecutive_failures=1,
                     last_checked=NOW - timedelta(hours=2))
        store, _, tmp = store_with(doomed, healthy)
        try:
            svc, eng = service(store, FakeClock(), policy=policy)
            report = asyncio.run(svc.run_once())
            assert report.retired_abandoned == 1
            assert eng.seen == [healthy.fingerprint], (
                "the doomed row must never reach the probe path; only the "
                "healthy one should have been evaluated"
            )
        finally:
            store.close()
            tmp.cleanup()
