#!/usr/bin/env python3
"""
P14.T1 — Crash durability, measured as the plan demanded: SIGKILL x10.

Why this tool exists (the honest reason). RESUME_PROMPT.md's gap table promised
"SIGKILL x10 durability" at P07. The session narrative later repeated a claim of
"crash recovery 10/10" -- but no such artifact existed on disk (ADR-010: never
trust memory). Rather than restate an unverifiable claim inside FINAL_AUDIT.md,
this tool PRODUCES the missing evidence now and the audit cites the artifact it
writes, dated, with the production of the number sitting in git next to it.

The unit-level guarantee was already covered from P08 by
atlas/tests/integration/test_store_lease.py::test_sigkill_after_acknowledged_write_loses_nothing
(a single SIGKILL after an acknowledged write). What that test cannot show is
that durability survives REPETITION: a WAL/checkpoint interaction that loses one
acknowledged write in ten runs is invisible to a single-shot test. This tool
runs the experiment ten times with varying batch sizes and reports every round.

Design (same discipline as the H8 integration test):
  * a spawn'd child process opens a REAL SqliteStore on a REAL file,
    upserts N proxies, calls store.checkpoint(), prints ACKED, then kills
    itself with SIGKILL (signal 9 cannot be caught: no atexit, no close,
    no flush runs -- the only honest way to test a crash),
  * the parent asserts the child died BY SIGKILL (-9) and that a fresh
    process recovers exactly N rows,
  * any round that fails any of these assertions is recorded as a failure;
    the artifact is written either way (evidence, not theatre).

Usage:
  python3 engineering/tools/crash_durability.py            # 10 rounds
  python3 engineering/tools/crash_durability.py --rounds 3 # smoke

Output: engineering/raw/crash_durability_<UTC>.json (never overwritten --
a new dated file per run, same convention as measure_baseline.py).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "engineering" / "raw"

# The parent process recovers rows through the same production code path the
# child used, so it needs the repo on sys.path too (the child inserts it
# itself; the parent is launched from anywhere). First run without this line
# reported ModuleNotFoundError as recovery_error on all 10 rounds -- a TOOL
# defect, recorded in superseded/, caught only because the round record
# carries the error string instead of silently marking the round failed.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Filled with str.format -- so it must contain NO stray braces and no
# %-formatting (same trap the H8 test documents: a doubled %% escaping bug
# once produced a SyntaxError in the child, caught only because the parent
# asserts on the ACKED line instead of trusting the subprocess).
_CRASH_CHILD = r"""
import os, sys, signal
sys.path.insert(0, {repo!r})
from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import Proxy, Endpoint, ProxyState, Protocol
from atlas.core.domain.verdict import Grade

store = SqliteStore({db!r})
batch = tuple(
    Proxy(endpoint=Endpoint.parse("10.42." + str(i // 250) + "." + str(i % 250 + 1)
                                  + ":8080"),
          protocol=Protocol.HTTP, state=ProxyState.READY, grade=Grade.GOOD)
    for i in range({n})
)
store.upsert_many(batch)
store.checkpoint()
print("ACKED", flush=True)             # the write is acknowledged AND flushed
os.kill(os.getpid(), signal.SIGKILL)   # hard kill: no atexit/close/flush runs
"""


def one_round(round_no: int, n: int, workdir: Path) -> dict:
    """One full crash experiment. Returns the measured round record."""
    db = workdir / f"crash_r{round_no}.db"
    src = _CRASH_CHILD.format(repo=str(ROOT), db=str(db), n=n)
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True,
                          text=True, timeout=120)
    child_s = time.monotonic() - t0

    acked = "ACKED" in proc.stdout
    died_by_sigkill = proc.returncode == -signal.SIGKILL

    # A fresh process must see exactly n rows -- nothing lost, nothing extra.
    recovered: int | None = None
    recovery_error: str | None = None
    if acked and died_by_sigkill:
        t1 = time.monotonic()
        try:
            from atlas.adapters.store_sqlite import SqliteStore
            with SqliteStore(db) as store:
                recovered = int(sum(store.count_by_state().values()))
        except Exception as exc:  # noqa: BLE001 - the failure IS the finding
            recovery_error = f"{type(exc).__name__}: {exc}"
        recovery_s = time.monotonic() - t1
    else:
        recovery_s = 0.0

    ok = (acked and died_by_sigkill and recovered == n
          and recovery_error is None)
    return {
        "round": round_no,
        "batch_size": n,
        "child_acked_write": acked,
        "child_died_by_sigkill": died_by_sigkill,
        "child_returncode": proc.returncode,
        "recovered_rows": recovered,
        "recovery_error": recovery_error,
        "child_wall_s": round(child_s, 3),
        "recovery_wall_s": round(recovery_s, 3),
        "ok": ok,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10,
                    help="number of independent SIGKILL rounds (default 10)")
    ap.add_argument("--rows", type=int, default=300,
                    help="base batch size per round (varied per round)")
    args = ap.parse_args()

    if args.rounds < 1:
        print("--rounds must be >= 1", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"crash durability: {args.rounds} SIGKILL round(s), "
          f"real SQLite on disk, fresh-process recovery check")

    rounds: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="atlas_crash_") as tmp:
        workdir = Path(tmp)
        for r in range(1, args.rounds + 1):
            # Vary the batch so a size-specific WAL boundary bug cannot hide:
            # 300, 350, 400, ... rows.
            n = args.rows + (r - 1) * 50
            rec = one_round(r, n, workdir)
            rounds.append(rec)
            flag = "ok " if rec["ok"] else "FAIL"
            print(f"  [{flag}] round {r:2d}: n={n} acked={rec['child_acked_write']} "
                  f"rc={rec['child_returncode']} recovered={rec['recovered_rows']} "
                  f"({rec['child_wall_s']}s + {rec['recovery_wall_s']}s)")

    passed = sum(1 for r in rounds if r["ok"])
    doc = {
        "task": "P14.T1",
        "generator": "engineering/tools/crash_durability.py",
        "invariant": "H8: a SIGKILL after an acknowledged+checkpointed write loses nothing",
        "why_this_exists": (
            "the phase plan promised SIGKILL x10 durability and no artifact on "
            "disk carried that evidence; produced now rather than restated from "
            "memory (ADR-010)"
        ),
        "method": {
            "rounds": args.rounds,
            "child": "spawn'd python: SqliteStore.upsert_many(N) -> checkpoint() "
                     "-> print ACKED -> os.kill(self, SIGKILL)",
            "parent_assertions": [
                "child printed ACKED (write was acknowledged before the kill)",
                "child returncode == -9 (died BY SIGKILL, not a clean exit)",
                "a FRESH process recovers exactly N rows (none lost, none extra)",
            ],
            "batch_sizes": "base 300, +50 per round, so a size-specific WAL "
                           "boundary defect cannot hide behind one fixed size",
            "complements": [
                "atlas/tests/integration/test_store_lease.py (single-shot H8 + "
                "concurrency proofs, in the suite since P08)",
                "engineering/raw/live_transcript_20260827T225147Z.json step 16 "
                "(SIGKILL of a LEASING child on the live pipeline)",
            ],
        },
        "measured_at_utc": stamp,
        "rounds_passed": passed,
        "rounds_total": len(rounds),
        "result": f"{passed}/{len(rounds)}",
        "rounds_detail": rounds,
    }
    dest = OUT_DIR / f"crash_durability_{stamp}.json"
    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"-> {dest.relative_to(ROOT)}")
    print(f"RESULT: {passed}/{len(rounds)} rounds passed")
    return 0 if passed == len(rounds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
