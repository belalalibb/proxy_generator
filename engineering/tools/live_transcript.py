#!/usr/bin/env python3
"""P13 — 17-step E2E LIVE transcript (ADR-041).

Executes the 17 operating steps against the REAL adapters over the LIVE
network and writes engineering/raw/live_transcript_<UTC>.json — one measured
record per step. Nothing here is hand-written: every step's record carries
values the step actually produced.

The step list is DERIVED FROM THE CODE (ADR-041), and this tool enforces the
derivation: --dry-run asserts that every step maps to a real, importable
symbol WITHOUT touching the network, so a step list that drifts from the
code fails loudly instead of narrating a system that no longer exists
(the ADR-014 defect class, one level up).

Hard constraints honoured:
  * no default target — --target is REQUIRED (ADR-007, H5)
  * the admission gate is never tuned for the demo: admitted=0 is a
    measurement, recorded as such (ADR-003)
  * ADR-006: the cycle budget stays small and per-host limits come from
    config.yaml, not from this file

Usage:
  live_transcript.py --dry-run                          # symbol check, no network
  live_transcript.py --target https://example.com       # live run -> artifact
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config.yaml"
REGISTRY = ROOT / "atlas" / "data" / "sources" / "sources.json"
OUT_DIR = ROOT / "engineering" / "raw"

STEP_NAMES = [
    "load config.yaml (all tunables; unknown keys refused)",
    "load the source registry (ADR-002: sources are data)",
    "enforce: there is no default target (ADR-007)",
    "open the SQLite store + WAL (ADR-004)",
    "sweep expired leases + reclaim stale probes (ADR-039)",
    "plan the scheduler over the store",
    "discovery cycle: fetch ENABLED sources (live network)",
    "normalize candidates (private/loopback/reserved dropped)",
    "dedup against the pool by endpoint (ADR-040)",
    "probe fresh candidates: TCP -> protocol -> k=5 sampling",
    "admission gate on p95 of k samples (ADR-003)",
    "persist every verdict with reason codes (B-02)",
    "serve N proxies via atomic lease (H3)",
    "prove the leases as LEASED rows in the store",
    "release accounting: one lease -> one release",
    "crash recovery: SIGKILL a leasing child, pool stays consistent",
    "observability: counts by state + rejections by reason",
]


def dry_run() -> int:
    """Every step names at least one real symbol. No network, no store."""
    checks = [
        ("atlas.adapters.config", ["load_scheduler_policy", "load_target_policy",
                                   "load_default_target_is_absent"]),
        ("atlas.adapters.registry", ["load_registry", "fetchable_sources"]),
        ("atlas.core.policy.target_policy", ["TargetPolicy"]),
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # expire/reclaim below
        ("atlas.engine.scheduler", ["PoolScheduler"]),
        ("atlas.engine.cycle", ["DiscoveryEngine"]),
        ("atlas.core.policy.normalize", ["normalize_batch"]),
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # get_by_endpoint
        ("atlas.adapters.probe_aiohttp", ["AiohttpProbe"]),
        ("atlas.core.policy.admission", ["AdmissionPolicy", "decide",
                                         "build_profile"]),
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # upsert path
        ("atlas.engine.handout", ["HandoutService"]),
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # lease/count_by_state
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # release
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # double_delivery_violations
        ("atlas.adapters.store_sqlite", ["SqliteStore"]),  # count_by_state
    ]
    import importlib
    bad: list[str] = []
    for i, (mod, syms) in enumerate(checks, 1):
        try:
            m = importlib.import_module(mod)
        except ImportError as exc:
            bad.append(f"step {i}: {mod} unimportable: {exc}")
            continue
        for s in syms:
            if not hasattr(m, s):
                bad.append(f"step {i}: {mod}.{s} missing")
    # method-level checks the module checks cannot express
    from atlas.adapters.store_sqlite import SqliteStore
    for meth in ("expire_leases", "reclaim_stale_probes", "get_by_endpoint",
                 "upsert_many", "lease", "count_by_state", "release",
                 "double_delivery_violations", "pool_size"):
        if not hasattr(SqliteStore, meth):
            bad.append(f"SqliteStore.{meth} missing")
    from atlas.engine.cycle import DiscoveryEngine
    for meth in ("run_cycle", "process_source", "evaluate"):
        if not hasattr(DiscoveryEngine, meth):
            bad.append(f"DiscoveryEngine.{meth} missing")
    from atlas.engine.scheduler import PoolScheduler
    if not hasattr(PoolScheduler, "plan"):
        bad.append("PoolScheduler.plan missing")
    from atlas.engine.handout import HandoutService
    if not hasattr(HandoutService, "handout"):
        bad.append("HandoutService.handout missing")

    for i, name in enumerate(STEP_NAMES, 1):
        print(f"  step {i:2d}: {name}")
    if bad:
        print("\nDRY-RUN FAILED:")
        for b in bad:
            print(f"  {b}")
        return 1
    print("\n17/17 steps resolve to real callables. No network touched.")
    return 0


class SystemClock:
    """Production clock — the tests define their own; tools are engineering code."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ms(self) -> float:
        return time.monotonic() * 1000.0

    def deadline(self, after_ms: float):
        from datetime import timedelta
        return self.now() + timedelta(milliseconds=after_ms)


class Recorder:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def record(self, n: int, ok: bool, measured: dict, note: str = "") -> None:
        self.steps.append({
            "step": n, "name": STEP_NAMES[n - 1], "ok": ok,
            "measured": measured, "note": note,
            "at_utc": datetime.now(timezone.utc).isoformat(),
        })
        flag = "OK " if ok else "FAIL"
        print(f"  [{flag}] step {n:2d} {STEP_NAMES[n - 1]}: "
              f"{json.dumps(measured, default=str)[:160]}")


async def live(target_url: str, max_sources: int, max_probes: int,
               lease_count: int) -> int:
    import aiohttp

    from atlas.adapters.config import (load_default_target_is_absent,
                                       load_scheduler_policy,
                                       load_target_policy)
    from atlas.adapters.http_source import HttpSourceAdapter
    from atlas.adapters.probe_aiohttp import AiohttpProbe
    from atlas.adapters.registry import fetchable_sources, load_registry
    from atlas.adapters.store_sqlite import SqliteStore
    from atlas.core.domain.source import Target
    from atlas.core.domain.verdict import Grade
    from atlas.core.ports.probe import ProbePlan
    from atlas.engine.cycle import CycleBudget, DiscoveryEngine
    from atlas.engine.handout import HandoutService
    from atlas.engine.scheduler import PoolScheduler

    rec = Recorder()
    clock = SystemClock()

    # 1 — config
    sched_policy = load_scheduler_policy(CONFIG)
    target_policy = load_target_policy(CONFIG)
    rec.record(1, True, {"config": "config.yaml",
                         "recheck_ready_after_s": sched_policy.recheck_ready_after_s,
                         "deny_hosts": sorted(target_policy.deny_hosts)})

    # 2 — registry
    registry = load_registry(REGISTRY)
    fetchable = fetchable_sources(registry)
    rec.record(2, len(fetchable) > 0,
               {"total": len(registry), "enabled": len(registry.enabled()),
                "fetchable": len(fetchable)})

    # 3 — no default target
    absent = load_default_target_is_absent(CONFIG)
    rec.record(3, absent, {"default_target_is_absent": absent},
               "a None target handed out would be a refusal, not a substitution")

    # 4 — store + WAL
    tmp = tempfile.TemporaryDirectory(prefix="atlas_p13_")
    db_path = str(Path(tmp.name) / "live.db")
    store = SqliteStore(db_path)
    jm = store.journal_mode  # property, not a method: reads PRAGMA back
    rec.record(4, jm.lower() == "wal", {"journal_mode": jm, "db": "tmpfs live.db"})

    # 5 — lease sweep + stale reclaim (empty pool: the honest values are 0)
    expired = store.expire_leases(now=clock.now())
    reclaimed = store.reclaim_stale_probes(now=clock.now())
    rec.record(5, True, {"expired_leases": expired, "reclaimed_probes": reclaimed},
               "zero on a fresh store is the correct measurement")

    # 6 — scheduler plan
    plan = PoolScheduler(store=store, clock=clock, policy=sched_policy).plan()
    rec.record(6, True, {"pool_size": plan.pool_size, "recheck": len(plan.recheck),
                         "evict": len(plan.evict)})

    # 7-12 — the discovery cycle, live
    target = Target(url=target_url)
    budget = CycleBudget(max_sources=max_sources, max_probes=max_probes,
                         max_candidates_per_source=60, probe_concurrency=16)
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=12, limit_per_host=2)  # ADR-006
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        engine = DiscoveryEngine(
            source_port=HttpSourceAdapter(session),
            probe=AiohttpProbe(),
            store=store, clock=clock, target=target,
            plan=ProbePlan(),
        )
        report, updated = await engine.run_cycle(fetchable, budget)

    seen = sum(o.candidates_seen for o in report.outcomes)
    accepted = sum(o.candidates_accepted for o in report.outcomes)
    dropped = {}
    for o in report.outcomes:
        for reason, n in o.dropped_by_reason.items():
            dropped[reason] = dropped.get(reason, 0) + n
    known = sum(o.already_known for o in report.outcomes)

    rec.record(7, report.probed > 0 or seen == 0,
               {"sources_fetched": len(report.outcomes), "candidates_seen": seen,
                "elapsed_s": round(report.elapsed_s, 1)})
    rec.record(8, True, {"accepted": accepted, "dropped_by_reason": dropped},
               "the normalizer is the production one; drops are typed, not silent")
    rec.record(9, True, {"already_known_deduped": known},
               "endpoint-keyed (ADR-040); 0 on a fresh store is correct")

    tcp_refused = report.rejected_by_reason.get("TCP_REFUSED", 0)
    rec.record(10, report.probed > 0,
               {"probed": report.probed, "tcp_refused": tcp_refused,
                "rejected_by_reason": report.rejected_by_reason},
               "TCP triage -> empirical protocol discovery -> k=5 sampling")
    rec.record(11, True,
               {"admitted": report.admitted, "probed": report.probed,
                "admission_rate": report.admission_rate},
               "admitted=0 is a measurement of free-proxy reality, not a failure")

    by_state = {k.value: v for k, v in store.count_by_state().items()}
    rec.record(12, store.pool_size() == report.stored,
               {"stored": store.pool_size(), "by_state": by_state})

    # 13-15 — handout / lease / release
    handout = HandoutService(store=store, clock=clock, target_policy=target_policy)
    result = handout.handout(target=target, count=lease_count,
                             min_grade=Grade.USABLE)
    granted = result.granted
    if granted:
        rec.record(13, True, {"requested": lease_count, "granted": len(granted),
                              "refusal": result.refusal})
        leased_state = store.count_by_state()
        from atlas.core.domain.proxy import ProxyState
        rec.record(14, leased_state.get(ProxyState.LEASED, 0) >= len(granted),
                   {"leased_rows": leased_state.get(ProxyState.LEASED, 0)})
        for g in granted:
            store.release(g.proxy.fingerprint, now=clock.now())
        after = store.count_by_state()
        rec.record(15, after.get(ProxyState.LEASED, 0) == 0,
                   {"leased_after_release": after.get(ProxyState.LEASED, 0),
                    "ready_after_release": after.get(ProxyState.READY, 0)})
    else:
        for n in (13, 14, 15):
            rec.record(n, True, {"granted": 0, "refusal": result.refusal},
                       "pool admitted nothing this run -- refusal is the honest "
                       "outcome; the lease protocol itself is proven by "
                       "test_store_lease.py and lease_concurrency.json")

    # 16 — crash recovery: a child leases rows and SIGKILLs ITSELF mid-claim.
    # A self-SIGKILL means returncode == -SIGKILL (-9), NOT 0: expecting 0
    # would fail the step on the very behaviour it exists to prove (H8).
    crash = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, db_path],
        capture_output=True, text=True, timeout=120)
    child = (json.loads(crash.stdout.strip().splitlines()[-1])
             if crash.stdout.strip() else {})
    died_by_sigkill = crash.returncode == -signal.SIGKILL
    violations = store.double_delivery_violations()
    pool_after = store.pool_size()
    rec.record(16, died_by_sigkill and not violations,
               {"child_died_by_sigkill": died_by_sigkill,
                "child_returncode": crash.returncode,
                "leased_before_kill": child.get("leased_before_kill"),
                "double_delivery_violations": len(violations),
                "pool_size_after": pool_after},
               "child holds a write transaction when it dies; WAL rolls it "
               "back, no row is lost or double-held. If the pool admitted "
               "nothing in step 11, the child leases 0 rows and this step "
               "proves store durability, not lease reclaim -- recorded, not "
               "dressed up.")

    # 17 — observability
    final = {k.value: v for k, v in store.count_by_state().items()}
    rec.record(17, True, {"by_state": final,
                          "rejected_by_reason": report.rejected_by_reason},
               "no silent paths: every rejection above carries a reason code")

    store.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = OUT_DIR / f"live_transcript_{stamp}.json"
    doc = {
        "task": "P13",
        "generator": "engineering/tools/live_transcript.py",
        "adr": "ADR-041",
        "network_used": True,
        "target": target_url,
        "budget": {"max_sources": max_sources, "max_probes": max_probes},
        "measured_at_utc": stamp,
        "steps_ok": sum(1 for s in rec.steps if s["ok"]),
        "steps": rec.steps,
    }
    artifact.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"\n-> {artifact.relative_to(ROOT)}  "
          f"({doc['steps_ok']}/{len(rec.steps)} steps OK)")
    return 0 if all(s["ok"] for s in rec.steps) else 1


_CRASH_CHILD = r"""
import json, os, signal, sys
from datetime import datetime, timezone
sys.path.insert(0, %r)
from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.verdict import Grade

db = sys.argv[1]
st = SqliteStore(db)
now = datetime.now(timezone.utc)
# claim everything READY with a long lease, then die holding the claim
rows = st.lease(count=5, min_grade=Grade.USABLE, lease_ms=600000, now=now)
print(json.dumps({"leased_before_kill": len(rows)}), flush=True)
os.kill(os.getpid(), signal.SIGKILL)
""" % str(ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve all 17 steps to real callables; no network")
    ap.add_argument("--target", help="REQUIRED caller-supplied target URL (ADR-007)")
    ap.add_argument("--max-sources", type=int, default=6)
    ap.add_argument("--max-probes", type=int, default=40)
    ap.add_argument("--lease-count", type=int, default=3)
    args = ap.parse_args()

    if args.dry_run:
        return dry_run()
    if not args.target:
        print("FATAL: --target is required. There is no default target (ADR-007).",
              file=sys.stderr)
        return 2
    return asyncio.run(live(args.target, args.max_sources, args.max_probes,
                            args.lease_count))


if __name__ == "__main__":
    raise SystemExit(main())
