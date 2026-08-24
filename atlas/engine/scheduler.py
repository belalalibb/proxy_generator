"""
POOL SCHEDULER — the four `scheduler.*` keys made load-bearing, and the exit from
the absorbing `COOLING` state (ADR-036).

WHAT WAS WRONG

`config.yaml scheduler.*` has existed since P01 with four keys. Measured this
session (`engineering/raw/pool_lifecycle.json`), all four had **zero** code
readers -- only a comment in `scoring.py` and two docstring lines in
`handout.py`. That is the ADR-019 defect class for the sixth time, after ADR-019,
ADR-021, ADR-029, ADR-033 and ADR-034.

But the unread keys were the symptom. The defect they were hiding is that the
pool had no lifecycle at all:

  * 40 consecutive failures still left a proxy `COOLING`;
  * `ProxyState.RETIRED` was assigned nowhere in production code;
  * nothing transitioned out of `COOLING`, and `lease()` only sees `READY`;
  * discovery skipped any fingerprint already stored, so a failed proxy was
    never re-probed either.

So a proxy that failed **once** left the pool permanently -- ADR-006's GeoNode
lesson ("a single failure must NEVER disable a source") recurring on the proxy
path, where `cooldown_delay` had exactly one caller and it was the source path.

WHY THE DECISION IS PURE AND ONLY THE APPLICATION LIVES HERE

`core.policy.lifecycle.decide()` answers "what should happen to this row?" with
no clock, no store and no I/O, so every rule -- the retire threshold, the
exponential recovery ladder, the READY recheck horizon -- is testable in
microseconds against a fake clock. This module does the parts `core/` may not:
read the clock through `ClockPort`, ask the store for candidates, write the
transitions back.

That split is why `plan()` returns a report instead of mutating. A scheduler that
decided and wrote in one pass could only be tested by inspecting the database
afterwards, which means every rule test would need a real store and would measure
persistence and policy together. Here the plan is a value: assert on it directly.

WHAT THIS DELIBERATELY DOES NOT DO

It does not probe. `plan()` identifies rows needing a re-probe and
`apply_retirements()` performs only the retirement transitions; handing the
`RECHECK` list to `DiscoveryEngine.evaluate()` is P10 work and is left visibly
undone rather than half-wired. `SchedulerPlan.recheck` is the seam, and the fact
that nothing consumes it yet is recorded in `next_action` -- not implied by a
half-finished call site.

The distinction matters because the failure mode this project keeps hitting is
*documentation ahead of code*. A scheduler that claimed to drive recheck while
only listing candidates would be exactly that, so the limit is stated in the type:
`plan()` returns work, it does not perform it.

EVICTION IS THE ONE PLACE A SIZE LIMIT COULD BREAK H3

`max_pool_size` deletes rows. If it ever deleted a `LEASED` row, a consumer would
still be holding a proxy whose record had vanished, and `release()` -- an
`UPDATE ... WHERE state='LEASED'` -- would match nothing and silently no-op, so
the release would be *lost* rather than rejected. The H3 guarantee would then be
broken by a capacity setting, with no error anywhere. Three things prevent it:
`select_evictable()` never returns a `LEASED` row, `delete_many()` carries
`AND state != 'LEASED'` in the statement itself (not a check-then-delete, which
has a window), and `Proxy.retired()` raises on a `LEASED` proxy.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from atlas.core.domain.proxy import Proxy, ProxyState
from atlas.core.policy.lifecycle import PoolAction, SchedulerPolicy, decide
from atlas.core.ports.clock import ClockPort


@dataclass(frozen=True, slots=True)
class SchedulerPlan:
    """
    What one scheduler pass decided, before anything was written.

    Every examined row lands in exactly one bucket, and `__post_init__` asserts
    it. That is the ADR-021/B-02 pattern: `CycleReport` makes a lost candidate
    structurally impossible rather than merely tested for, and the same reasoning
    applies here -- a row that silently belonged to no bucket would be a row the
    scheduler forgot, which is the defect this whole module exists to fix.
    """
    examined: int
    retire: tuple[Proxy, ...] = ()
    recheck: tuple[Proxy, ...] = ()
    cooling: tuple[Proxy, ...] = ()
    keep_ready: tuple[Proxy, ...] = ()
    recheck_ready: tuple[Proxy, ...] = ()
    in_flight: tuple[Proxy, ...] = ()
    terminal: tuple[Proxy, ...] = ()
    evict: tuple[Proxy, ...] = ()
    pool_size: int = 0
    over_capacity_by: int = 0

    def __post_init__(self) -> None:
        counted = (len(self.retire) + len(self.recheck) + len(self.cooling)
                   + len(self.keep_ready) + len(self.recheck_ready)
                   + len(self.in_flight) + len(self.terminal))
        if counted != self.examined:
            raise ValueError(
                f"scheduler plan lost rows: examined {self.examined} but "
                f"bucketed {counted}. Every row must land in exactly one bucket "
                "(B-02: an unaccounted row is an undiagnosable one)."
            )
        # Eviction is NOT part of the bucket sum: it is a second, orthogonal pass
        # over the whole table, so a row can be both `cooling` and `evict`.
        if self.over_capacity_by < 0:
            raise ValueError(
                f"over_capacity_by must be >= 0, got {self.over_capacity_by}")

    @property
    def actionable(self) -> int:
        """Rows this pass would change: retirements plus evictions."""
        return len(self.retire) + len(self.evict)


@dataclass
class _Counters:
    """Mutable accumulator, kept private so `SchedulerPlan` can stay frozen."""
    buckets: dict[PoolAction, list[Proxy]] = field(default_factory=dict)

    def add(self, action: PoolAction, proxy: Proxy) -> None:
        self.buckets.setdefault(action, []).append(proxy)

    def get(self, action: PoolAction) -> tuple[Proxy, ...]:
        return tuple(self.buckets.get(action, ()))


class PoolScheduler:
    """
    Applies `SchedulerPolicy` to the stored pool.

    The store is accepted as a duck-typed object rather than `StorePort` because
    the scheduler needs `select_schedulable` / `select_evictable` / `delete_many`
    / `pool_size`, which are ADR-036 additions and not part of the port every
    consumer must implement. Widening `StorePort` would force every fake in the
    test suite to grow four methods it does not use, and a port that grows to
    match one caller stops describing a contract.
    """

    def __init__(self, store, clock: ClockPort, *,
                 policy: SchedulerPolicy | None = None) -> None:
        self._store = store
        self._clock = clock
        self._policy = policy or SchedulerPolicy()

    @property
    def policy(self) -> SchedulerPolicy:
        return self._policy

    def plan(self, *, batch: int = 1000) -> SchedulerPlan:
        """
        Decide what this pass would do. Writes nothing.

        `batch` bounds the rows loaded, so a 50 000-row pool does not become a
        50 000-row allocation -- the same bounded-memory reasoning as the rate
        limiter's host cap (ADR-034).
        """
        now = self._clock.now()
        rows = self._store.select_schedulable(limit=batch)

        counters = _Counters()
        for p in rows:
            counters.add(decide(p, self._policy, now=now), p)

        size = self._store.pool_size()
        over = max(0, size - self._policy.max_pool_size)
        evict: tuple[Proxy, ...] = ()
        if over:
            evict = self._store.select_evictable(limit=over)

        return SchedulerPlan(
            examined=len(rows),
            retire=counters.get(PoolAction.RETIRE),
            recheck=counters.get(PoolAction.RECHECK),
            cooling=counters.get(PoolAction.COOLING_NOT_ELAPSED),
            keep_ready=counters.get(PoolAction.KEEP_READY),
            recheck_ready=counters.get(PoolAction.RECHECK_READY),
            in_flight=counters.get(PoolAction.IN_FLIGHT),
            terminal=counters.get(PoolAction.TERMINAL),
            evict=evict,
            pool_size=size,
            over_capacity_by=over,
        )

    def apply_retirements(self, plan: SchedulerPlan) -> int:
        """
        Perform the `COOLING -> RETIRED` transitions the plan identified.

        Returns the number retired. Separate from `plan()` so a caller can
        inspect, log or refuse a pass before it mutates anything -- and separate
        from `apply_evictions` because retirement preserves the record while
        eviction destroys it, and conflating "stop using this" with "forget this
        existed" would delete the failure history that made the retirement
        decision explainable.
        """
        if not plan.retire:
            return 0
        retired = tuple(
            p.retired(reason=f"retired after {p.consecutive_failures} "
                             "consecutive failures (ADR-036)")
            for p in plan.retire
        )
        self._store.upsert_many(retired)
        return len(retired)

    def apply_evictions(self, plan: SchedulerPlan) -> int:
        """
        Delete the rows `max_pool_size` requires removing.

        Returns the number actually deleted, which may be LOWER than
        `len(plan.evict)` if a row was leased between planning and deletion --
        `delete_many` refuses leased rows inside the statement. The shortfall is
        returned rather than raised: losing the race is normal operation, and the
        next pass will reconsider. Silently reporting the requested count as the
        deleted count is what would be wrong, because the pool would then appear
        to be within its cap when it was not.
        """
        if not plan.evict:
            return 0
        return self._store.delete_many(tuple(p.fingerprint for p in plan.evict))

    def run_once(self, *, batch: int = 1000) -> tuple[SchedulerPlan, int, int]:
        """
        One full pass: plan, retire, evict. Returns (plan, retired, evicted).

        Retirement runs BEFORE eviction so a row that has just earned retirement
        is a candidate for eviction in the same pass -- `select_evictable` orders
        `RETIRED` first, so the cap sheds dead weight before it touches anything
        live. The reverse order would evict `COOLING` and `READY` rows while
        newly-retired ones stayed, which is a size limit preferring to delete
        working proxies.
        """
        plan = self.plan(batch=batch)
        retired = self.apply_retirements(plan)
        if retired:
            # Re-plan so eviction sees the rows just retired; otherwise the cap
            # would act on a snapshot that predates its own retirements.
            plan = self.plan(batch=batch)
        evicted = self.apply_evictions(plan)
        return plan, retired, evicted


__all__ = ["PoolScheduler", "SchedulerPlan"]
