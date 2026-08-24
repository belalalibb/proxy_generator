#!/usr/bin/env python3
"""
Mutation check for the ADR-038 recheck path (P10.T1).

WHY THIS EXISTS

`test_recheck.py` and `test_recheck_store.py` both pass. That fact alone says
nothing: a test that cannot fail is not evidence (ADR-010), and the concurrency
claims here are the ones most likely to be vacuous, because the correct and the
broken implementations are BEHAVIOURALLY IDENTICAL in a single-threaded test --
exactly the argument P05.T3 made for `lease()`. So each defect the suites claim
to prevent is injected, and the run fails unless a suite catches it.

WHY BOTH SUITES RUN FOR EVERY MUTANT

The clobber claim spans two files by design: the unit suite pins the sequencing
(claim -> probe -> conditional write-back) and the integration suite pins the
behaviour under real process contention. A mutant killed by neither is a hole; a
mutant killed by only one is still recorded, because WHICH suite caught it is
the interesting part. `read_then_write_claim` in particular is expected to
survive the unit suite and die in the integration suite -- if it ever dies in
the unit suite too, that is a finding, not a bonus.

PROCESS CONSTRAINT (inherited from mutate_handout.py, learned the hard way)

An earlier mutation run left a broken file on disk between steps and an
auto-sync COMMITTED the mutant. So this tool never writes a mutant to the real
module path: it copies the package into a temp tree, patches the copy, and runs
pytest there. A git-status comparison before/after makes a violation loud.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STORE_REL = Path("atlas/adapters/store_sqlite.py")
SERVICE_REL = Path("atlas/engine/recheck.py")
UNIT_REL = Path("atlas/tests/unit/test_recheck.py")
ITEST_REL = Path("atlas/tests/integration/test_recheck_store.py")


class Mutation:
    def __init__(self, name: str, module: Path, old: str, new: str,
                 rationale: str) -> None:
        self.name = name
        self.module = module
        self.old = old
        self.new = new
        self.rationale = rationale


MUTATIONS = [
    Mutation(
        "read_then_write_claim",
        STORE_REL,
        old="""                UPDATE proxies
                   SET state = 'PROBING',
                       probe_expires_at = ?
                 WHERE fingerprint IN ({marks})
                   AND state IN ('DISCOVERED', 'COOLING', 'READY')
                RETURNING *""",
        new="""                UPDATE proxies
                   SET state = 'PROBING',
                       probe_expires_at = ?
                 WHERE fingerprint IN ({marks})
                RETURNING *""",
        rationale=(
            "Drop the state predicate from the claiming UPDATE -- the "
            "check-then-write claim. It steals rows that are LEASED (breaking "
            "H3 from the other side) and RETIRED (resurrecting a deliberately "
            "removed row). Single-threaded tests cannot see this, which is why "
            "the integration suite exists."
        ),
    ),
    Mutation(
        "unconditional_writeback",
        STORE_REL,
        old="                 WHERE fingerprint = :fingerprint\n                   AND state = 'PROBING'",
        new="                 WHERE fingerprint = :fingerprint",
        rationale=(
            "Make the probe write-back unconditional: the measured clobber. A "
            "consumer that leased the row mid-probe keeps using it while the "
            "row reads READY, and double_delivery_violations() stays silent "
            "because no second LEASE was ever appended."
        ),
    ),
    Mutation(
        "writeback_carries_lease_columns",
        STORE_REL,
        old="                       probe_expires_at = NULL\n",
        new="                       probe_expires_at = NULL,\n"
            "                       lease_id = NULL\n",
        rationale=(
            "Let complete_probe assert something it has no evidence about. A "
            "probe measures latency and protocol, not ownership; carrying "
            "lease_id is precisely what made the clobber possible."
        ),
    ),
    Mutation(
        "reclaim_promotes_to_ready",
        STORE_REL,
        old="                   SET state = 'COOLING',",
        new="                   SET state = 'READY',",
        rationale=(
            "Treat an abandoned probe as proof of health. A measurement that "
            "never completed would hand out a proxy on no evidence -- H7's "
            "'live is not good' inverted into 'unfinished is good'."
        ),
    ),
    Mutation(
        "null_deadline_never_reclaimed",
        STORE_REL,
        old="                   AND (probe_expires_at IS NULL OR probe_expires_at <= ?)",
        new="                   AND probe_expires_at <= ?",
        rationale=(
            "Stop treating a NULL deadline as expired, so a row left PROBING "
            "by a version that recorded no deadline is stranded forever -- the "
            "ADR-036 absorbing state rebuilt under a new name."
        ),
    ),
    Mutation(
        "failed_rows_not_prioritised",
        SERVICE_REL,
        old="        return (tuple(plan.recheck) + tuple(plan.recheck_ready))[:limit]",
        new="        return (tuple(plan.recheck_ready) + tuple(plan.recheck))[:limit]",
        rationale=(
            "Refresh healthy rows before recovering failed ones. Under a "
            "budget too small for both, capacity that is already serving wins "
            "and capacity that is lost stays lost."
        ),
    ),
    Mutation(
        "lost_writeback_reported_as_applied",
        SERVICE_REL,
        old="            if self._store.complete_probe(probed, now=done_at):",
        new="            if self._store.complete_probe(probed, now=done_at) or True:",
        rationale=(
            "Count a refused write-back as applied, so the report claims to "
            "have refreshed rows it did not touch. A false success is worse "
            "than a failure: it removes the reason to look."
        ),
    ),
]


def git_dirty() -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return [ln for ln in out.splitlines() if ln.strip()]


def run_suite(tree: Path, rel: Path) -> tuple[int, int, str]:
    """Run one suite inside `tree`. Returns (passed, failed, last line)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tree / rel), "-q",
         "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    passed = failed = 0
    for line in out.splitlines():
        m = re.search(r"(\d+) failed", line)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) passed", line)
        if m:
            passed = int(m.group(1))
    # A collection error or a crash is not a kill by default: report it.
    if passed == 0 and failed == 0 and "error" in out.lower():
        failed = -1
    return passed, failed, out.splitlines()[-1] if out else ""


def main() -> int:
    before = git_dirty()

    with tempfile.TemporaryDirectory(prefix="atlas-recheck-mut-") as td:
        root = Path(td)
        shutil.copytree(REPO / "atlas", root / "atlas",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO / "config.yaml", root / "config.yaml")

        base = {}
        for label, rel in (("unit", UNIT_REL), ("integration", ITEST_REL)):
            p, f, tail = run_suite(root, rel)
            base[label] = {"passed": p, "failed": f}
            print(f"baseline {label}: {p} passed, {f} failed")
            if f or p == 0:
                print(f"ABORT: unmutated {label} copy is not green; mutation "
                      "results would be meaningless.")
                print(tail)
                return 2

        originals = {
            STORE_REL: (REPO / STORE_REL).read_text(),
            SERVICE_REL: (REPO / SERVICE_REL).read_text(),
        }
        results = []
        for m in MUTATIONS:
            src = originals[m.module]
            if m.old not in src:
                # Loud, never a silent skip: a stale anchor tests nothing while
                # inflating the kill rate.
                print(f"  [ERROR] {m.name}: anchor not found in "
                      f"{m.module} -- the module changed and this mutation no "
                      "longer applies")
                results.append({"mutation": m.name, "module": str(m.module),
                                "applied": False, "survived": True,
                                "killed_by": {}, "rationale": m.rationale})
                continue

            (root / m.module).write_text(src.replace(m.old, m.new, 1))
            killed_by = {}
            for label, rel in (("unit", UNIT_REL), ("integration", ITEST_REL)):
                _, f, _ = run_suite(root, rel)
                killed_by[label] = f
            (root / m.module).write_text(src)          # restore before next

            killed = any(v > 0 for v in killed_by.values())
            detail = ", ".join(f"{k} {v}" for k, v in killed_by.items())
            print(f"  [{'KILLED' if killed else 'SURVIVED'}] {m.name}: "
                  f"failures -> {detail}")
            results.append({"mutation": m.name, "module": str(m.module),
                            "applied": True, "survived": not killed,
                            "killed_by": killed_by, "rationale": m.rationale})

    after = git_dirty()
    if before != after:
        print("\nFATAL: the working tree changed during the mutation run.")
        print(f"  before: {before}\n  after:  {after}")
        return 3

    survivors = [r for r in results if r["survived"]]
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules": [str(STORE_REL), str(SERVICE_REL)],
        "suites": [str(UNIT_REL), str(ITEST_REL)],
        "baseline": base,
        "mutations": results,
        "survivors": len(survivors),
        "killed": len(results) - len(survivors),
        "working_tree_unchanged": True,
        "method": (
            "The package is copied to a temp tree and the COPY is patched; the "
            "real modules are never written. Both suites run for every mutant, "
            "because which suite catches a concurrency defect is the point: "
            "read_then_write_claim is expected to survive the unit suite and "
            "die under real process contention."
        ),
    }
    out = REPO / "engineering/raw/recheck_mutation.json"
    out.write_text(json.dumps(artifact, indent=1) + "\n")
    print(f"\n-> {out.relative_to(REPO)}")
    print(f"{len(results) - len(survivors)}/{len(results)} mutations killed")
    if survivors:
        print("SURVIVORS (the suites do not actually pin these):")
        for s in survivors:
            print(f"  - {s['mutation']}: {s['rationale']}")
        return 1
    print("every injected defect was caught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
