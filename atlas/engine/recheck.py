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


@dataclass(frozen=True, slots=True)
class RecheckBudget:
    """
    Bounds on one recheck pass.

    `max_rechecks` bounds WORK, not outcome, exactly as `CycleBudget` does: a
    pass that rechecks fewer rows because the pool is healthy is a smaller pass,
    not a failed one.

    `probe_ms` is the claim's lifetime, and it must exceed the worst-case probe
    duration or a live probe's claim expires under it and another worker starts
    the same work. It is validated against nothing here because the probe plan
    lives elsewhere; the default is deliberately generous for that reason.
    """
    max_rechecks: int = 100
    concurrency: int = 10
    probe_ms: int = 120_000

    def __post_init__(self) -> None:
        for name in ("max_rechecks", "concurrency", "probe_ms"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1, got {getattr(self, name)}")


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

        plan = self._scheduler.plan()
        candidates = self._candidates(plan, budget.max_rechecks)
        if not candidates:
            return RecheckReport(reclaimed=reclaimed)

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
            promoted=promoted,
            demoted=demoted,
            by_reason=by_reason,
        )


__all__ = ["RecheckBudget", "RecheckReport", "RecheckService"]
