#!/usr/bin/env python3
"""
MEASURE THE P10 RECHECK GAP — and the hazard the obvious wiring would create.

ADR-036 stopped at a seam: `PoolScheduler.plan()` returns a `recheck` tuple and
`apply_retirements()` performs only state transitions, so nothing re-probes. P10
is meant to connect that seam to `DiscoveryEngine.evaluate()`.

This tool measures the situation BEFORE writing any of it, because the obvious
one-line wiring -- `for p in plan.recheck: evaluate(p)` then `upsert_many(...)` --
is not safe, and the reason is worth having as a number rather than an argument.

FOUR MEASUREMENTS

  1. consumers_of_recheck: AST search for any production code that reads
     `.recheck` / `.recheck_ready` off a plan. Confirms the seam is dead.

  2. probing_assignments: every production site that WRITES ProxyState.PROBING.
     `decide()` classifies LEASED/PROBING as IN_FLIGHT and both store queries
     filter on it, so PROBING is load-bearing on the READ side while nothing
     ever puts a row into it. A guard against a state that cannot occur is a
     guard that has never once fired.

  3. lease_clobber: the hazard. A READY row past `recheck_ready_after_s` is
     selected by `plan()` as `recheck_ready`. It is also, being READY, leasable
     RIGHT NOW. If a recheck probes it and writes the result back with
     `upsert_many`, the UPSERT sets `state` and `lease_id` from the in-memory
     copy -- which was loaded BEFORE the lease existed. The consumer's lease is
     overwritten by a probe that started earlier. This script performs that
     sequence against a real SqliteStore and reports what the pool holds
     afterwards.

  4. double_probe: two scheduler passes with no claim step both select the same
     row, because `select_schedulable` is a plain SELECT with no compare-and-set.
     Reported as the count of passes that returned the same fingerprint.

Run:  python3 engineering/tools/measure_recheck_gap.py [--after]
Out:  engineering/raw/recheck_gap.json  (before/after pair, like pool_lifecycle.json)
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.adapters.store_sqlite import SqliteStore                # noqa: E402
from atlas.core.domain.proxy import (                              # noqa: E402
    Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade                        # noqa: E402
from atlas.core.policy.lifecycle import SchedulerPolicy            # noqa: E402
from atlas.engine.scheduler import PoolScheduler                   # noqa: E402

OUT = ROOT / "engineering" / "raw" / "recheck_gap.json"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t

    def monotonic_ms(self) -> float:
        return 0.0

    def deadline(self, after_ms: float) -> datetime:
        return self._t + timedelta(milliseconds=after_ms)


def _production_files() -> list[Path]:
    return sorted(
        p for p in (ROOT / "atlas").rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    )


def _consumers_of(attrs: tuple[str, ...]) -> dict[str, list[str]]:
    """
    Production sites that READ any of `attrs` off some object.

    Attribute access, not a text grep: a docstring mentioning `.recheck` is not
    a consumer, and counting one would reproduce the ADR-019 false-positive that
    `measure_pool_lifecycle.py` had to filter prose out of.
    """
    found: dict[str, list[str]] = {a: [] for a in attrs}
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in found:
                rel = path.relative_to(ROOT)
                found[node.attr].append(f"{rel}:{node.lineno}")
    return found


def _state_writes(state: str) -> list[str]:
    """
    Sites where `ProxyState.<state>` is used as a VALUE being written.

    Distinguishes a write from the `in (...)` membership test in `decide()`: a
    read inside a comparison is a guard, not an assignment, and conflating them
    is what would let "PROBING is mentioned" pass for "PROBING is reachable".
    """
    writes: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ProxyState"
                    and node.attr == state):
                continue
            writes.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return writes


def _membership_reads(state: str) -> list[str]:
    """Sites where ProxyState.<state> appears inside a comparison/containment."""
    reads: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "ProxyState"
                        and sub.attr == state):
                    reads.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                    break
    return sorted(set(reads))


def _ready_row(host: str, *, last_checked: datetime | None) -> Proxy:
    return Proxy(
        endpoint=Endpoint(host=host, port=8080),
        protocol=Protocol.HTTP,
        state=ProxyState.READY,
        grade=Grade.GOOD,
        latency=LatencyProfile(samples_ms=(100.0,), p50_ms=100.0, p95_ms=120.0,
                               mean_ms=100.0, success_ratio=1.0),
        source_id="measure",
        first_seen=NOW - timedelta(days=1),
        last_checked=last_checked,
    )


def measure_lease_clobber(after: bool = False) -> dict:
    """
    Does a recheck write-back destroy a live lease?

    Sequence, all against a real store:
      1. a READY row, last checked long ago -> `plan()` calls it recheck_ready
      2. a consumer leases it (state LEASED, lease_id set)
      3. the recheck finishes and writes back the copy it loaded at step 1
      4. inspect what the pool holds

    `after=False` writes back the way `process_source` does today
    (`upsert_many`, unconditional). `after=True` uses the ADR-038 path
    (`claim_for_probe` then `complete_probe`). SAME SEQUENCE, SAME HAZARD, one
    variable changed -- otherwise the two arms would not be comparable and the
    "after" column would be measuring a different experiment (the ADR-020/022
    splice defect, which this project has now committed twice).
    """
    policy = SchedulerPolicy(recheck_ready_after_s=900.0)
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "clobber.db")
        try:
            stale = _ready_row("10.0.0.1", last_checked=NOW - timedelta(hours=2))
            store.upsert_many((stale,))

            sched = PoolScheduler(store, FixedClock(NOW), policy=policy)
            plan = sched.plan()
            selected = [p.endpoint.host for p in plan.recheck_ready]

            # ADR-038 inserts the claim HERE, between planning and probing.
            claimed = ()
            if after:
                claimed = store.claim_for_probe(
                    tuple(p.fingerprint for p in plan.recheck_ready),
                    now=NOW, probe_ms=60_000)

            # (2) a consumer takes it while the recheck is "in flight"
            leased = store.lease(count=1, min_grade=Grade.USABLE,
                                 lease_ms=60_000, now=NOW)
            lease_ids = [p.lease_id for p in leased]

            # (3) the recheck writes back the copy it loaded at step 1.
            probed = (plan.recheck_ready[0]
                      .record_success(NOW)
                      .with_state(ProxyState.READY, reason="OK")
                      .graded(Grade.GOOD)) if plan.recheck_ready else None
            writeback_applied = None
            if probed is not None:
                if after:
                    writeback_applied = store.complete_probe(probed, now=NOW)
                else:
                    store.upsert_many((probed,))     # unconditional: the defect

            row = store.get(stale.fingerprint)
            clobbered = row is not None and row.lease_id is None and len(leased) == 1
            # Be precise about WHICH way the hazard was closed. In the `after`
            # arm the claim runs first, so `lease()` never sees a READY row and
            # `lease_granted` is 0 -- the consumer was refused, the probe won.
            # Reporting that as "the claim excluded the leased row" would invert
            # the causality and describe an experiment that did not happen.
            if clobbered:
                verdict = "CLOBBERED: the recheck write-back erased a live lease"
            elif after and not leased:
                verdict = (
                    "NO OVERLAP: the claim took the row to PROBING first, so "
                    "the consumer's lease was REFUSED (lease_granted=0) and "
                    "the write-back had no live lease to erase. The two never "
                    "hold the row at once -- which side wins is a race, and "
                    "both orders are asserted in test_recheck_store.py."
                )
            elif leased and row is not None and row.lease_id:
                verdict = (
                    "LEASE INTACT: the consumer won the row and the probe's "
                    "write-back was refused by complete_probe"
                )
            else:
                verdict = "lease survived"
            return {
                "path": "claim_for_probe + complete_probe" if after
                        else "upsert_many (unconditional)",
                "selected_for_recheck_while_leasable": selected,
                "claimed_for_probe": [p.endpoint.host for p in claimed],
                "lease_granted": len(leased),
                "lease_ids": lease_ids,
                "writeback_applied": writeback_applied,
                "state_after_writeback": row.state.value if row else None,
                "lease_id_after_writeback": row.lease_id if row else None,
                "lease_survived_the_recheck": bool(row and row.lease_id),
                "audit_double_delivery": [
                    list(v) for v in store.double_delivery_violations()
                ],
                "verdict": verdict,
            }
        finally:
            store.close()


def measure_double_probe(after: bool = False) -> dict:
    """
    Two consecutive passes: do both select the same row?

    `after=True` performs the ADR-038 claim between the passes, which is the
    whole point of a claim: it makes a double pass IDEMPOTENT, because `PROBING`
    is excluded from the claimable states.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "double.db")
        try:
            row = _ready_row("10.0.0.2", last_checked=NOW - timedelta(hours=2))
            store.upsert_many((row,))
            sched = PoolScheduler(store, FixedClock(NOW))
            first = sched.plan()
            a = {p.fingerprint for p in first.recheck_ready}
            if after:
                store.claim_for_probe(tuple(a), now=NOW, probe_ms=60_000)
            second = sched.plan()
            b = {p.fingerprint for p in second.recheck_ready}
            return {
                "claim_between_passes": bool(after),
                "pass1_selected": len(a),
                "pass2_selected": len(b),
                "same_row_selected_twice": sorted(a & b),
                "verdict": ("DUPLICATE WORK: no claim step, both passes own it"
                            if a & b else
                            "CLAIMED: the second pass no longer selects it"),
            }
        finally:
            store.close()


def measure_probing_absorbing(after: bool = False) -> dict:
    """
    If a claim marks rows PROBING, is there any exit?

    Asked BEFORE building the claim, because adding PROBING without a reclaim
    would recreate ADR-036's absorbing state under a new name -- and this time
    with the crash window H8 already taught us to expect.

    The `after` arm does not merely check that a method EXISTS: hasattr is
    satisfied by a method that does nothing (the ADR-019 decorative defect). It
    calls `reclaim_stale_probes` and then re-checks whether the row became
    leasable again, which is the property that actually matters.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "probing.db")
        try:
            row = _ready_row("10.0.0.3", last_checked=NOW - timedelta(hours=2))
            probing = row.with_state(ProxyState.PROBING, reason="claimed")
            store.upsert_many((probing,))
            sched = PoolScheduler(store, FixedClock(NOW + timedelta(days=7)))
            plan = sched.plan()
            buckets = {
                "in_flight": [p.endpoint.host for p in plan.in_flight],
                "recheck": [p.endpoint.host for p in plan.recheck],
                "recheck_ready": [p.endpoint.host for p in plan.recheck_ready],
            }
            week = NOW + timedelta(days=7)
            leasable = store.lease(count=1, min_grade=Grade.USABLE,
                                   lease_ms=1000, now=week)
            has_reclaim = hasattr(store, "reclaim_stale_probes")

            # Does the exit actually WORK, not merely exist?
            reclaimed = None
            leasable_after_reclaim = None
            if after and has_reclaim:
                reclaimed = store.reclaim_stale_probes(now=week)
                recovered = store.get(row.fingerprint)
                # COOLING re-enters the ADR-006 ladder; a cooled row becomes
                # RECHECK once its backoff elapses, so ask the scheduler.
                plan2 = PoolScheduler(store, FixedClock(week)).plan()
                leasable_after_reclaim = {
                    "state": recovered.state.value if recovered else None,
                    "reason_code": recovered.reason_code if recovered else None,
                    "recheck_bucket": [p.endpoint.host for p in plan2.recheck],
                }
            return {
                "a_week_later_buckets": buckets,
                "leasable_after_a_week": len(leasable),
                "store_has_reclaim_method": bool(has_reclaim),
                "rows_reclaimed": reclaimed,
                "after_reclaim": leasable_after_reclaim,
                "verdict": (
                    "PROBING WOULD BE ABSORBING: classified IN_FLIGHT forever, "
                    "never leasable, and no reclaim exists"
                    if not has_reclaim and not leasable else
                    "NOT ABSORBING: reclaim_stale_probes returns the row to "
                    "COOLING, which re-enters the ADR-006 ladder"
                    if reclaimed else "a reclaim path exists"
                ),
            }
        finally:
            store.close()


def measure_interval_readers() -> dict:
    """Does anything consult `discovery_interval_s` to decide WHEN to run?"""
    hits: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "discovery_interval_s":
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return {
        "sites": sorted(hits),
        "verdict": ("declared/validated but nothing schedules on it"
                    if len(hits) <= 2 else "consulted"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", action="store_true",
                    help="record the post-fix arm into the same artifact")
    args = ap.parse_args()

    block = {
        "consumers_of_recheck": _consumers_of(("recheck", "recheck_ready")),
        "probing_writes_in_production": _state_writes("PROBING"),
        "probing_membership_reads": _membership_reads("PROBING"),
        "lease_clobber": measure_lease_clobber(args.after),
        "double_probe": measure_double_probe(args.after),
        "probing_absorbing": measure_probing_absorbing(args.after),
        "discovery_interval_readers": measure_interval_readers(),
    }

    payload = {}
    if OUT.exists():
        payload = json.loads(OUT.read_text(encoding="utf-8"))

    # A `before` arm is a HISTORICAL measurement of code that no longer exists.
    # Re-running it after the fix does not reproduce it -- it silently rewrites
    # it with hybrid numbers (measured this session: `store_has_reclaim_method`
    # flipped to true under the key that documents the pre-fix world, which
    # would have been a fabricated baseline). Once `before` is recorded, it is
    # evidence, and evidence is append-only.
    key = "after" if args.after else "before"
    if key == "before" and "before" in payload:
        print("REFUSED: `before` is already recorded and describes pre-fix "
              "code. Re-running it now would overwrite a historical "
              "measurement with numbers from the CURRENT tree.\n"
              "         Use --after to record the post-fix arm, or delete the "
              "artifact deliberately if you really mean to re-baseline.")
        return 4
    payload[key] = block
    payload["measured_at"] = datetime.now(timezone.utc).isoformat()
    payload["tool"] = "engineering/tools/measure_recheck_gap.py"
    payload["note"] = (
        "P10 evidence. `before` is the state ADR-036 shipped: the recheck seam "
        "has no consumer, PROBING is read by decide() and both store queries but "
        "written nowhere, and the obvious wiring (probe then upsert_many) erases "
        "a live lease because the write-back carries a pre-lease snapshot. "
        "`after` runs the SAME three experiments against the ADR-038 path "
        "(claim_for_probe + complete_probe + reclaim_stale_probes): one variable "
        "changed, so the two arms are comparable rather than spliced."
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")

    print(json.dumps(block, indent=1, sort_keys=True))
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
