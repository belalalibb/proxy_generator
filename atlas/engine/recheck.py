"""
RECHECK — the seam ADR-036 left open, closed with a claim (ADR-038).

WHAT ADR-036 DELIBERATELY STOPPED SHORT OF

`PoolScheduler.plan()` returns `recheck` and `recheck_ready` tuples;
`apply_retirements()` performs only state transitions. Nothing re-probed. The
seam was left visibly undone rather than half-wired, and `next_action` said so.

Measured before writing any of this (`engineering/raw/recheck_gap.json`):
`.recheck` and `.recheck_ready` had exactly ONE reader each, both inside
`scheduler.py`'s own `__post_init__` bucket sum. A row could be selected for
recheck by every pass forever and consumed by nobody.

WHY THE OBVIOUS WIRING IS WRONG

The one-line version -- `for p in plan.recheck: evaluate(p)` then
`upsert_many(...)` -- is what this module exists NOT to be. Three measured
reasons, all in the artifact:

  1. LEASE CLOBBER. A `READY` row past `recheck_ready_after_s` is selected for
     recheck AND is leasable right now. Lease it mid-probe, then let the probe
     finish: `upsert_many` writes `state` and `lease_id` from the snapshot loaded
     BEFORE the lease existed, so the row returns to `READY` with
     `lease_id=NULL` while the consumer still holds it. Two callers now believe
     they own one proxy. Worse, `double_delivery_violations()` reports NOTHING,
     because no second `LEASE` was ever recorded -- H3's audit is blind to a
     violation created by an unconditional write rather than by a bad claim.
     Measured verdict: "CLOBBERED: the recheck write-back erased a live lease".

  2. DOUBLE PROBE. `select_schedulable` is a plain SELECT with no claim, so two
     consecutive passes return the SAME fingerprint. Measured: pass 1 and pass 2
     both selected `3fd692f1f03a4fe8`. Two workers would pay k=5 twice for one
     answer, which is responsibility 2 of the engine ("never re-probe what the
     pool already holds") violated by the component meant to enforce it.

  3. PROBING WAS UNREACHABLE. `decide()` classifies `PROBING` as `IN_FLIGHT` and
     both store queries filter on it, but AST search found `ProxyState.PROBING`
     written NOWHERE in production -- its single production site was the
     membership test that reads it. A guard against a state nothing can enter has
     never once fired. That is ADR-019's decorative-config defect in the state
     machine itself.

THE FIX IS THE MECHANISM THAT ALREADY WORKS

`claim_for_probe()` is `lease()`'s compare-and-set with a different target
state, and `complete_probe()` is a conditional write-back
(`WHERE ... AND state='PROBING'`) that reports a lost race instead of resolving
it by overwriting. So a recheck now: claims (or loses the row and moves on),
probes, and writes back only while it still holds the claim.

WHY A CLAIM NEEDS A RECLAIM, DECIDED BEFORE THE CLAIM WAS BUILT

Introducing `PROBING` without an exit would recreate ADR-036's absorbing state
under a new name -- and this time with a crash window H8 already taught us to
expect: SIGKILL is uncatchable, so no `finally` can release a claim. This was
measured first (`probing_absorbing`): a row left PROBING was still `IN_FLIGHT`
and still unleasable a week later, with no reclaim method on the store.

`reclaim_stale_probes()` is therefore part of the same change, not a follow-up,
and it reclaims to `COOLING` rather than `READY`: a probe that never reported is
not evidence of health, and promoting it would hand out a proxy on a measurement
that never finished.

THE TWO BOUNDS P10 DEFERRED, AND WHY THEY WERE NOT OPTIONAL (ADR-039)

P10 shipped the claim and the reclaim and then stopped, recording two gaps in
`next_action` instead of implying they were closed. Both were measured before
being fixed (`engineering/raw/recheck_bounds.json`), and both were worse than the
prose suggested:

  1. THE CLAIM WAS SHORTER THAN THE WORK IT COVERED. `probe_ms` was validated as
     `>= 1`. The real worst case is 59 000 ms per probe (S2 + every protocol rung
     + k=5 serial samples) and 590 000 ms for the last wave of a 100-row batch at
     concurrency 10 -- against a 120 000 ms default. So `reclaim_stale_probes`
     would reclaim rows whose probes were still legitimately running and hand
     them to a second worker: the DOUBLE PROBE this module exists to prevent,
     re-entering through the timeout instead of through the missing claim.
     `RecheckBudget` now validates against `claim_bound()` and derives its
     default from it, so the number cannot be hand-picked wrong again.

  2. THE ABANDON PATH RECORDED NOTHING. Driving a crashing worker through the
     real store produced 12 claim -> reclaim cycles with `consecutive_failures`
     and `total_attempts` both still 0, `decide()` returning RECHECK every time,
     and no retirement -- forever. `total_attempts` counts probe SAMPLES and
     `consecutive_failures` counts probes that RETURNED, so neither could ever
     see an abandonment, and no threshold expressed in them could bound the
     cycle. `abandoned_rechecks` is the missing counter, incremented inside the
     reclaim statement itself.

WHAT THIS MODULE DOES NOT DO

It does not decide. `decide()` owns which rows need a recheck and
`admission.decide` owns whether a measurement passes; this module only sequences
claim -> probe -> write-back and accounts for every row. It does not re-implement
probing: `DiscoveryEngine.evaluate()` is used exactly as discovery uses it, so a
recheck and a first check apply the identical gate -- a second evaluation path
would be a second set of rules to drift (ADR-023).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from atlas.core.domain.proxy import Proxy
from atlas.core.domain.verdict import Grade
from atlas.core.ports.clock import ClockPort
from atlas.core.ports.probe import (
    ProbeBound, ProbePlan, claim_bound, default_target_timeout_ms,
)


@dataclass(frozen=True, slots=True)
class RecheckBudget:
    """
    Bounds on one recheck pass.

    `max_rechecks` bounds WORK, not outcome, exactly as `CycleBudget` does: a
    pass that rechecks fewer rows because the pool is healthy is a smaller pass,
    not a failed one.

    `probe_ms` is the claim's LIFETIME, and P10 left it validated only as `>= 1`
    -- a bound on the sign of a number, not on the thing that matters. ADR-039
    closes that: it is now checked against `claim_bound()`, which prices the real
    probe plan.

    THE DEFAULT IS COMPUTED, NOT CHOSEN. `probe_ms=None` means "derive it", so
    the ordinary caller cannot get it wrong and there is no literal to drift.
    Measured (`engineering/raw/recheck_bounds.json`): the old hand-picked
    120 000 ms default was short by 470 000 ms at this batch and concurrency --
    the guard was not merely weak, the value it was failing to check was already
    wrong by ~5x. Raising the literal was rejected as the fix for exactly that
    reason: the next change to k, to a timeout, or to the protocol ladder would
    have made a new hand-picked number wrong again, silently.
    """
    max_rechecks: int = 100
    concurrency: int = 10
    probe_ms: int | None = None
    plan: ProbePlan = field(default_factory=ProbePlan)
    target_timeout_ms: int = field(default_factory=default_target_timeout_ms)

    def __post_init__(self) -> None:
        for name in ("max_rechecks", "concurrency"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1, got {getattr(self, name)}")

        bound = self.claim_bound()
        if self.probe_ms is None:
            # frozen dataclass: object.__setattr__ is the sanctioned way to
            # finish initialising a derived field.
            object.__setattr__(self, "probe_ms", bound.required_ms)
            return

        # KEPT from P10, deliberately. It is subsumed by the bound check below,
        # but it is the guard that states the floor in its own terms, and a
        # `probe_ms=0` claim is a distinct kind of nonsense (a claim that has
        # already expired when it is taken) worth naming separately.
        if self.probe_ms < 1:
            raise ValueError(f"probe_ms must be >= 1, got {self.probe_ms}")

        if self.probe_ms < bound.required_ms:
            raise ValueError(
                f"probe_ms={self.probe_ms} is shorter than the worst-case probe "
                f"({bound.required_ms}ms = {bound.per_probe_ms}ms per probe x "
                f"{bound.waves} wave(s) of {self.concurrency} from a batch of "
                f"{self.max_rechecks}). A claim shorter than the work it covers "
                "expires while the probe is still running, so reclaim hands the "
                "row to a second worker -- the double-probe defect ADR-038 "
                "closed, returning through the timeout (ADR-039)."
            )

    def claim_bound(self) -> ProbeBound:
        """
        This budget's authoritative claim bound, from the real probe plan.

        Exposed rather than kept private so the number is inspectable at runtime
        and in tests. A bound that can only be re-derived by rereading the source
        is one that gets re-derived WRONG, which is the 120 000 ms literal's
        entire history.
        """
        return claim_bound(self.plan, target_timeout_ms=self.target_timeout_ms,
                           batch=self.max_rechecks,
                           concurrency=self.concurrency)


@dataclass(frozen=True, slots=True)
class RecheckReport:
    """
    One recheck pass, fully accounted.

    `selected == claimed + lost_claim` and `claimed == applied + lost_writeback`
    are asserted, not trusted. This is the `CycleReport` / `SchedulerPlan`
    pattern (B-02): a row that belonged to no bucket would be a row the recheck
    forgot, and the whole point of this module is that rows stop being forgotten.

    `lost_claim` and `lost_writeback` are SEPARATE counters because they mean
    different things operationally: the first is contention before any work was
    done (cheap), the second is contention after a k=5 probe was already paid for
    (expensive). Collapsing them into one "races" number would hide which of the
    two is actually costing anything.
    """
    selected: int = 0
    claimed: int = 0
    lost_claim: int = 0
    applied: int = 0
    lost_writeback: int = 0
    reclaimed: int = 0
    # ADR-039: rows retired this pass for repeatedly abandoning their claims.
    # Reported separately from `reclaimed` because they answer different
    # questions: `reclaimed` is "how much work was recovered", `retired_abandoned`
    # is "how much of the pool we have stopped trying to recover". A pass that
    # reclaims 40 and retires 0 forever is the unbounded cycle this counter makes
    # visible -- and an operator who cannot see it cannot know the loop is stuck.
    retired_abandoned: int = 0
    promoted: int = 0
    demoted: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.claimed + self.lost_claim != self.selected:
            raise ValueError(
                f"recheck lost rows at the claim: selected={self.selected} but "
                f"claimed+lost_claim={self.claimed + self.lost_claim} (B-02)"
            )
        if self.applied + self.lost_writeback != self.claimed:
            raise ValueError(
                f"recheck lost rows at the write-back: claimed={self.claimed} "
                f"but applied+lost_writeback={self.applied + self.lost_writeback}"
                " (B-02)"
            )

    @property
    def contention(self) -> int:
        """Rows another actor took from us, at either stage."""
        return self.lost_claim + self.lost_writeback


class RecheckService:
    """
    Runs one recheck pass: claim -> probe -> conditional write-back.

    The store is duck-typed for the same reason `PoolScheduler` duck-types it:
    `claim_for_probe` / `complete_probe` / `reclaim_stale_probes` are ADR-038
    additions, and widening `StorePort` would force every fake in the suite to
    grow three methods it does not use.
    """

    def __init__(self, *, scheduler, engine, store, clock: ClockPort) -> None:
        self._scheduler = scheduler
        self._engine = engine
        self._store = store
        self._clock = clock

    def _candidates(self, plan, limit: int) -> tuple[Proxy, ...]:
        """
        Which rows this pass will attempt, worst-first.

        `recheck` (a failed row whose ADR-006 cooldown elapsed) is ordered BEFORE
        `recheck_ready` (a healthy row past its freshness horizon), because the
        first is a proxy currently outside the pool and the second is one already
        serving. Under a budget that cannot cover both, recovering lost capacity
        beats refreshing capacity that still works.
        """
        return (tuple(plan.recheck) + tuple(plan.recheck_ready))[:limit]

    async def run_once(self, budget: RecheckBudget | None = None) -> RecheckReport:
        """
        One pass. Reclaims abandoned probes FIRST, then claims, probes, writes back.

        Reclaim runs first so a crashed worker's rows are candidates for THIS
        pass rather than waiting for the next one -- otherwise every crash costs
        a full interval of that proxy's availability, and with a thin pool that
        is the self-inflicted outage `handout`'s `finally` exists to avoid.
        """
        budget = budget or RecheckBudget()
        now = self._clock.now()

        reclaimed = self._store.reclaim_stale_probes(now=now)

        # ADR-039. AFTER reclaim, BEFORE plan -- both halves are load-bearing.
        #
        # After reclaim: the reclaim above is what INCREMENTS the counter, so a
        # row that just crossed the threshold is retired in the same pass that
        # noticed. Retiring first would always act on stale counts and let the
        # row take one more claim.
        #
        # Before plan: `plan()` is what SELECTS rows to re-probe. Retiring first
        # means a row at the threshold is already `RETIRED` when the candidate
        # query runs, and `select_schedulable` excludes RETIRED -- so it cannot be
        # picked up by this pass. That is the "cannot bypass the limit through
        # scheduler ordering" property, and it holds because of this ordering
        # rather than because of a check inside the loop.
        threshold = self._scheduler.policy.retire_after_abandoned_rechecks
        retired_abandoned = self._store.retire_abandoned(
            threshold=threshold, now=now)

        plan = self._scheduler.plan()
        candidates = self._candidates(plan, budget.max_rechecks)
        if not candidates:
            return RecheckReport(reclaimed=reclaimed,
                                 retired_abandoned=retired_abandoned)

        # ONE claim statement for the whole batch: a per-row claim would take the
        # write lock N times and widen the window in which a consumer can lease a
        # row this pass has already decided to probe.
        claimed = self._store.claim_for_probe(
            tuple(p.fingerprint for p in candidates),
            now=now, probe_ms=budget.probe_ms)

        sem = asyncio.Semaphore(budget.concurrency)

        async def guarded(p: Proxy):
            async with sem:
                return await self._engine.evaluate(p)

        evaluated = await asyncio.gather(*(guarded(p) for p in claimed))

        applied = lost_writeback = promoted = demoted = 0
        by_reason: dict[str, int] = {}
        done_at = self._clock.now()
        for probed, verdict in evaluated:
            # The claim is released by the write-back itself (probe_expires_at is
            # cleared in the same statement), so a lost race needs no cleanup:
            # the row already belongs to whoever won it.
            if self._store.complete_probe(probed, now=done_at):
                applied += 1
                if verdict.admitted:
                    promoted += 1
                else:
                    demoted += 1
                    code = verdict.reason.value
                    by_reason[code] = by_reason.get(code, 0) + 1
            else:
                lost_writeback += 1

        return RecheckReport(
            selected=len(candidates),
            claimed=len(claimed),
            lost_claim=len(candidates) - len(claimed),
            applied=applied,
            lost_writeback=lost_writeback,
            reclaimed=reclaimed,
            retired_abandoned=retired_abandoned,
            promoted=promoted,
            demoted=demoted,
            by_reason=by_reason,
        )


__all__ = ["RecheckBudget", "RecheckReport", "RecheckService"]
