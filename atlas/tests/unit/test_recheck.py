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
