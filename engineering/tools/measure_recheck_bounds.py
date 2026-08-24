#!/usr/bin/env python3
"""
MEASURE the two safety obligations P10 deferred, before either is fixed.

P10 shipped the claim/write-back protocol and closed the lease-clobber, the
double-probe and the absorbing-PROBING defects. It deferred two guarantees, and
recorded them in `next_action` rather than implying they were done:

  1. `RecheckBudget.probe_ms` is validated only as `>= 1`. Nothing relates the
     claim's LIFETIME to how long a probe can legitimately RUN. If the claim is
     shorter, `reclaim_stale_probes` hands a row that is still being probed to a
     second worker -- the double-probe defect returning through the TIMEOUT
     instead of through the missing SELECT that P10 fixed.

  2. There is no recheck-lifecycle counter. `total_attempts` counts PROBE
     SAMPLES, which is a different quantity. The question is whether a proxy
     that never produces a usable result can cycle PROBING -> COOLING ->
     PROBING forever.

This tool answers both with the real code, and writes
`engineering/raw/recheck_bounds.json`. Nothing here asserts a fix; it measures
the CURRENT state so the fix has a before to be compared against.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from atlas.adapters.probe_aiohttp import (  # noqa: E402
    _AIOHTTP_SCHEME, _PROTOCOL_LADDER,
)
from atlas.adapters.store_sqlite import SqliteStore  # noqa: E402
from atlas.core.domain.proxy import (  # noqa: E402
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import Target  # noqa: E402
from atlas.core.domain.verdict import Grade  # noqa: E402
from atlas.core.policy.lifecycle import PoolAction, SchedulerPolicy, decide  # noqa: E402
from atlas.core.ports.probe import ProbePlan  # noqa: E402
from atlas.engine.recheck import RecheckBudget  # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
OUT = Path(__file__).resolve().parents[1] / "raw" / "recheck_bounds.json"


def mk(host: str = "9.9.9.9", *, state: ProxyState = ProxyState.COOLING,
       consecutive_failures: int = 0,
       last_checked: datetime | None = NOW) -> Proxy:
    return Proxy(
        endpoint=Endpoint(host=host, port=8080),
        protocol=Protocol.HTTP,
        state=state,
        grade=Grade.REJECTED,
        latency=LatencyProfile(),
        anonymity=Anonymity.UNKNOWN,
        consecutive_failures=consecutive_failures,
        last_checked=last_checked,
        first_seen=NOW - timedelta(days=1),
        source_id="measure",
    )


def measure_worst_case_probe() -> dict:
    """
    Derive the worst-case duration of ONE evaluate() from the real probe config,
    and compare it against what RecheckBudget currently accepts.

    Read off `DiscoveryEngine.evaluate` (cycle.py:302) rather than the docstring
    pipeline: evaluate calls tcp_handshake -> discover_protocol -> sample_latency.
    S5 check_integrity is NOT called by it, so counting S5 would OVERSTATE the
    bound -- a bound has to describe the code that runs, not the design.
    """
    plan = ProbePlan()
    target = Target(url="https://example.invalid/probe")

    # S3 tries every rung aiohttp can actually dial. SOCKS rungs are skipped
    # (`continue`) with no request, so they cost ~0 and must NOT be counted.
    testable = [p for p in _PROTOCOL_LADDER
                if _AIOHTTP_SCHEME[p] not in ("socks4", "socks5")]

    s2 = plan.tcp_timeout_ms
    s3 = len(testable) * target.timeout_ms
    # Worst case is NOT the early-stop path: an alternating ok/fail pattern never
    # reaches stop_after_consecutive_failures, so all k samples run serially.
    s4 = plan.samples * plan.per_sample_timeout_ms
    per_probe = s2 + s3 + s4

    default = RecheckBudget()
    waves = math.ceil(default.max_rechecks / default.concurrency)

    # Every candidate is claimed by ONE statement at time T, but the semaphore
    # only lets `concurrency` of them run at a time. A row in the last wave sits
    # inside its own claim for the whole queue ahead of it.
    required = per_probe * waves

    accepted_short = None
    try:
        RecheckBudget(probe_ms=1)
        accepted_short = 1
    except ValueError:
        pass

    return {
        "question": "is probe_ms >= the real worst-case time inside a claim?",
        "evaluate_stages_actually_called": ["tcp_handshake", "discover_protocol",
                                            "sample_latency"],
        "check_integrity_called_by_evaluate": False,
        "testable_protocol_rungs": [p.value for p in testable],
        "s2_tcp_ms": s2,
        "s3_protocol_ms": s3,
        "s4_latency_ms": s4,
        "per_probe_worst_ms": per_probe,
        "concurrency": default.concurrency,
        "max_rechecks": default.max_rechecks,
        "waves": waves,
        "required_claim_ms": required,
        "current_default_probe_ms": default.probe_ms,
        "default_is_sufficient": default.probe_ms >= required,
        "shortfall_ms": max(0, required - default.probe_ms),
        "probe_ms_1_accepted": accepted_short == 1,
        "verdict": (
            "UNDERSIZED: the default claim expires while a probe is still "
            f"legitimately running ({default.probe_ms}ms < {required}ms), and "
            "probe_ms=1 is accepted outright"
            if default.probe_ms < required else "sufficient"
        ),
    }


def measure_unbounded_recheck() -> dict:
    """
    Drive a crashing probe through the real store and see whether the row ever
    leaves the PROBING -> COOLING -> PROBING cycle.

    A crash is modelled the way H8 says it happens: the claim is taken and the
    worker never reports. No `finally` runs, so recovery is `reclaim_stale_probes`
    -- exactly the path a SIGKILL leaves behind.
    """
    tmp = tempfile.TemporaryDirectory()
    store = SqliteStore(Path(tmp.name) / "pool.db")
    policy = SchedulerPolicy()
    row = mk()
    store.upsert_many((row,))
    fp = row.fingerprint

    clock = NOW
    cycles = []
    for i in range(12):
        # The scheduler would classify it; record what it decides.
        before = store.get(fp)
        action = decide(before, policy, now=clock)
        if action is not PoolAction.RECHECK:
            cycles.append({"cycle": i, "action": action.value,
                           "note": "not selected for recheck"})
            if action is PoolAction.TERMINAL:
                break
            clock = clock + timedelta(seconds=60)
            continue

        claimed = store.claim_for_probe((fp,), now=clock, probe_ms=60_000)
        # ... and the worker dies here. Nothing reports.
        clock = clock + timedelta(seconds=120)
        reclaimed = store.reclaim_stale_probes(now=clock)
        after = store.get(fp)
        cycles.append({
            "cycle": i,
            "action": action.value,
            "claimed": len(claimed),
            "reclaimed": reclaimed,
            "state_after": after.state.value,
            "consecutive_failures": after.consecutive_failures,
            "total_attempts": after.total_attempts,
            "reason_code": after.reason_code,
        })
        clock = clock + timedelta(seconds=60)

    final = store.get(fp)
    store.close()
    tmp.cleanup()

    probing_cycles = [c for c in cycles if c.get("claimed") == 1]
    return {
        "question": "can a permanently crashing proxy cycle forever?",
        "cycles_driven": len(cycles),
        "claim_reclaim_cycles": len(probing_cycles),
        "consecutive_failures_after": final.consecutive_failures,
        "total_attempts_after": final.total_attempts,
        "final_state": final.state.value,
        "ever_retired": final.state is ProxyState.RETIRED,
        "retire_threshold": policy.retire_after_consecutive_failures,
        "reclaim_touches_failure_counter": final.consecutive_failures > 0,
        "trace": cycles,
        "verdict": (
            "UNBOUNDED: the row was claimed and reclaimed "
            f"{len(probing_cycles)} times, consecutive_failures stayed at "
            f"{final.consecutive_failures}, and it never retired -- the abandon "
            "path records nothing, so the retirement ladder never advances"
            if final.state is not ProxyState.RETIRED else "bounded"
        ),
    }


def main() -> int:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "measure the two safety obligations P10 deferred, BEFORE fixing",
        "probe_lifetime": measure_worst_case_probe(),
        "recheck_attempts": measure_unbounded_recheck(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
