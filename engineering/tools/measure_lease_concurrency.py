#!/usr/bin/env python3
"""
Measure H3 (no double delivery) under real process concurrency, and record the
result as an artifact.

WHY THIS TOOL EXISTS SEPARATELY FROM THE TEST SUITE

The pytest integration test ASSERTS that concurrent leases never overlap. That
makes it a gate: it goes green or red. But "green" carries no number, and a claim
in README.md needs a figure that can be re-derived (ADR-014c). So this tool runs
the same contention and writes what actually happened to
engineering/raw/lease_concurrency.json.

It runs BOTH implementations:

  real  -- SqliteStore.lease():        one BEGIN IMMEDIATE + UPDATE...RETURNING CAS
  naive -- NaiveStore.lease_naive():   SELECT, then UPDATE (the legacy shape, B-05)

Reporting only the real store would be the weaker claim. "0 duplicates" is
meaningless on its own -- an idle machine produces 0 duplicates from broken code
too. The naive arm is the control that gives the 0 its meaning (ADR-022).

Offline: no network, no external services. Uses a temp directory and deletes it.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.adapters.store_sqlite import SqliteStore          # noqa: E402
from atlas.core.domain.proxy import (                        # noqa: E402
    Endpoint, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade                  # noqa: E402
from atlas.tests.integration.naive_store import NaiveStore   # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _seed(db: Path, n: int) -> None:
    with SqliteStore(db) as store:
        store.upsert_many(tuple(
            Proxy(endpoint=Endpoint.parse(f"10.{i // 250}.0.{i % 250 + 1}:8080"),
                  protocol=Protocol.HTTP, state=ProxyState.READY, grade=Grade.GOOD)
            for i in range(n)
        ))


def _real_worker(db: str, count: int, out: mp.Queue) -> None:
    store = SqliteStore(db)
    try:
        out.put([p.fingerprint for p in store.lease(
            count=count, min_grade=Grade.USABLE, lease_ms=60_000, now=NOW)])
    finally:
        store.close()


def _naive_worker(db: str, count: int, out: mp.Queue) -> None:
    store = NaiveStore(db)
    try:
        out.put(list(store.lease_naive(count=count, now=NOW, gap_s=0.25)))
    finally:
        store.close()


def _run(kind: str, worker, tmp: Path, *, procs: int, per_proc: int,
         pool: int) -> dict:
    db = tmp / f"{kind}.db"
    _seed(db, pool)
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    workers = [ctx.Process(target=worker, args=(str(db), per_proc, q))
               for _ in range(procs)]
    for w in workers:
        w.start()
    results = [q.get(timeout=120) for _ in range(procs)]
    for w in workers:
        w.join(timeout=120)

    handed = [fp for r in results for fp in r]
    unique = len(set(handed))
    with SqliteStore(db) as store:
        audit = store.double_delivery_violations()
        leased = store.count_by_state().get(ProxyState.LEASED, 0)
    return {
        "implementation": kind,
        "processes": procs,
        "requested_per_process": per_proc,
        "pool_size": pool,
        "total_requested": procs * per_proc,
        "handed_out": len(handed),
        "unique_fingerprints": unique,
        "duplicates": len(handed) - unique,
        "rows_left_in_leased_state": leased,
        "audit_log_violations": len(audit),
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="atlas-h3-"))
    try:
        # Two arms at DIFFERENT configs cannot be tabulated side by side as if
        # they were one experiment -- that is exactly the splice ADR-020 exists
        # to forbid. So the head-to-head comparison runs the real store at the
        # naive arm's own config (pool 12, 6 procs x 6), and the oversubscribed
        # run is reported separately as its own, harder, case.
        matched_real = _run("real_matched", _real_worker, tmp,
                            procs=6, per_proc=6, pool=12)
        naive = _run("naive", _naive_worker, tmp, procs=6, per_proc=6, pool=12)
        real = _run("real", _real_worker, tmp, procs=12, per_proc=4, pool=24)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": "under real process concurrency, is any proxy delivered twice?",
        "invariant": "H3 NO DOUBLE DELIVERY",
        "method": {
            "concurrency": "multiprocessing, spawn context, one sqlite connection "
                           "per process (NOT threads: the GIL can hide a race that "
                           "separate processes expose)",
            "oversubscribed": "the real arm requests 48 from a pool of 24, because "
                              "contention is where read-then-write breaks",
            "naive_gap_s": 0.25,
            "why_naive": "a control. 0 duplicates from an idle machine proves "
                         "nothing; the naive arm shows the same test body DOES "
                         "detect double delivery when it is present (ADR-022)",
        },
        "head_to_head": {
            "note": "same pool (12), same processes (6), same request size (6). "
                    "The ONLY difference is the lease implementation, which is "
                    "what makes this a controlled comparison.",
            "config": {"processes": 6, "requested_per_process": 6,
                       "pool_size": 12},
            "real_duplicates": matched_real["duplicates"],
            "naive_duplicates": naive["duplicates"],
        },
        "real_matched": matched_real,
        "real": real,
        "naive": naive,
        "verdict": (
            "H3 HOLDS for SqliteStore.lease"
            if real["duplicates"] == 0 and real["audit_log_violations"] == 0
            else "H3 VIOLATED"
        ),
        "control_is_effective": naive["duplicates"] > 0,
    }

    out = ROOT / "engineering" / "raw" / "lease_concurrency.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("  head-to-head (pool 12, 6 procs x 6 -- identical config):")
    print(f"    real  : handed={matched_real['handed_out']:3d} "
          f"unique={matched_real['unique_fingerprints']:3d} "
          f"duplicates={matched_real['duplicates']}")
    print(f"    naive : handed={naive['handed_out']:3d} "
          f"unique={naive['unique_fingerprints']:3d} "
          f"duplicates={naive['duplicates']}")
    print("  oversubscribed (pool 24, 12 procs x 4 = 48 requested):")
    print(f"  real  : handed={real['handed_out']:3d} "
          f"unique={real['unique_fingerprints']:3d} "
          f"duplicates={real['duplicates']}")
    print(f"  naive : handed={naive['handed_out']:3d} "
          f"unique={naive['unique_fingerprints']:3d} "
          f"duplicates={naive['duplicates']}")
    print(f"  {report['verdict']}")
    print(f"  control effective: {report['control_is_effective']}")
    print(f"  -> {out.relative_to(ROOT)}")

    if real["duplicates"] or real["audit_log_violations"]:
        return 1
    if matched_real["duplicates"] or matched_real["audit_log_violations"]:
        return 1
    if not report["control_is_effective"]:
        print("  WARNING: the control found no duplicates, so the real arm's 0 "
              "is not meaningful")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
