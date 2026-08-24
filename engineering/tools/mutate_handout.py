#!/usr/bin/env python3
"""
Mutation check for the hand-out layer (P08, extended in P09.T2 by ADR-035).

ANCHOR DRIFT IS A FAILURE, NOT A SKIP

The ADR-035 rename (`target_ttl_s` -> `recheck_horizon_s`) left this file's
`stale_reported_as_fresh` anchor matching no source text. The run reported
`[ERROR] anchor text not found` and counted the mutant as a SURVIVOR rather than
skipping it quietly, so a stale harness degraded loudly instead of inflating its
own kill rate. That behaviour is load-bearing: a mutation tool that silently
skips unmatched anchors reports a perfect score for testing nothing.

WHY THIS EXISTS

`test_handout.py` passes. That fact alone says nothing: a test that cannot fail
is not evidence (ADR-010), and this project has already shipped three guards that
matched their own documentation instead of the code (ADR-014, ADR-022, ADR-023).
So each defect the suite claims to prevent is INJECTED here, and the run is a
failure unless the suite catches it.

PROCESS CONSTRAINT, LEARNED THE HARD WAY (see PROGRESS.md P08 pre-work)

An earlier mutation run left a deliberately broken file on disk between steps; an
auto-sync committed the mutant, and /tmp did not survive to hold the backup. So
this tool NEVER writes a mutant to the real module path. It copies the whole
package into a fresh temp tree, patches the copy, and runs pytest there with that
tree first on sys.path. The working tree is never modified -- verified by a
git-status check before and after.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_REL = Path("atlas/engine/handout.py")
TEST_REL = Path("atlas/tests/unit/test_handout.py")


class Mutation:
    def __init__(self, name: str, old: str, new: str, rationale: str) -> None:
        self.name = name
        self.old = old
        self.new = new
        self.rationale = rationale


MUTATIONS = [
    Mutation(
        "target_checked_after_lease",
        old="""        refusal = check_target(target, self._target_policy)
        if refusal is not None:""",
        new="""        refusal = None
        if refusal is not None:""",
        rationale=(
            "Skip target validation entirely: the ADR-029 policy stops being "
            "consulted on the serving path, which is the exact state P08 set out "
            "to fix."
        ),
    ),
    Mutation(
        "no_overselect",
        old="        want = min(\n            count * self._policy.overselect,",
        new="        want = min(\n            count * 1,",
        rationale=(
            "Disable over-selection, so the store's one-term p95 order decides "
            "who is served and the four-term P07 score becomes decorative "
            "(ADR-033)."
        ),
    ),
    Mutation(
        "leak_surplus",
        old="""            for p in leased:
                if p.fingerprint not in granted_fps:
                    self._store.release(p.fingerprint, now=now)""",
        new="""            for p in surplus:
                self._store.release(p.fingerprint, now=now)
            for p in unusable:
                self._store.release(p.fingerprint, now=now)""",
        rationale=(
            "Restore the ORIGINAL defect this suite found: release from the two "
            "buckets instead of from `leased`. Correct on the happy path, and it "
            "leaks every leased row when ranking raises."
        ),
    ),
    Mutation(
        "stale_reported_as_fresh",
        old="        return age_s is None or age_s > self._policy.recheck_horizon_s",
        new="        return False",
        rationale=(
            "Present evidence older than the recheck horizon as current, "
            "silently -- the B-16 defect that made 97% of proxy.txt look alive."
        ),
    ),
    Mutation(
        "never_checked_treated_as_fresh_via_comparison",
        old="        return age_s is None or age_s > self._policy.recheck_horizon_s",
        new="        return age_s is not None and age_s > self._policy.recheck_horizon_s",
        rationale=(
            "Drop the never-checked arm so an unverified proxy reports INSIDE "
            "the horizon. This is the mutant that SURVIVED while the test "
            "restated the boolean inline instead of calling `_past_horizon` -- "
            "a test that re-implements the code under test measures the test "
            "(ADR-035). It dies only because the predicate is now callable."
        ),
    ),
    Mutation(
        "unfireable_horizon_accepted",
        old="        if self._policy.recheck_horizon_s >= self._scoring.max_age_s:",
        new="        if False:",
        rationale=(
            "Drop the ADR-035 construction guard, permitting a horizon at or "
            "above max_age_s. `rank(include_stale=False)` then drops every row "
            "the flag could describe, so the flag can never fire and all served "
            "proxies report fresh at any age -- staleness reported as freshness "
            "by CONFIGURATION rather than by code."
        ),
    ),
    Mutation(
        "never_checked_looks_just_checked",
        old="""        if proxy.last_checked is None:
            return None
        return (now - proxy.last_checked).total_seconds()""",
        new="""        if proxy.last_checked is None:
            return 0.0
        return (now - proxy.last_checked).total_seconds()""",
        rationale=(
            "Report an unverified proxy's age as 0.0, making 'never checked' "
            "indistinguishable from 'just checked'."
        ),
    ),
    Mutation(
        "all_stale_collapsed_into_pool_empty",
        old="                refusal=HandoutRefusal.ALL_STALE,",
        new="                refusal=HandoutRefusal.POOL_EMPTY,",
        rationale=(
            "Collapse two facts that demand opposite operator responses: an "
            "empty pool (add sources) and a fully stale one (run discovery)."
        ),
    ),
    Mutation(
        "accounting_identity_unchecked",
        old="""        if self.leased != accounted:
            raise ValueError(""",
        new="""        if False:
            raise ValueError(""",
        rationale=(
            "Stop enforcing the lease accounting identity, so a capacity leak "
            "can be constructed and reported as a normal result."
        ),
    ),
]


def git_dirty() -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return [ln for ln in out.splitlines() if ln.strip()]


def run_suite(tree: Path) -> tuple[int, int, str]:
    """Run the hand-out suite inside `tree`. Returns (passed, failed, tail)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tree / TEST_REL), "-q",
         "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    passed = failed = 0
    for line in out.splitlines():
        s = line.strip()
        if " passed" in s or " failed" in s:
            for part in s.replace(",", " ").split():
                if part.isdigit():
                    continue
            import re
            m = re.search(r"(\d+) failed", s)
            if m:
                failed = int(m.group(1))
            m = re.search(r"(\d+) passed", s)
            if m:
                passed = int(m.group(1))
    return passed, failed, out.strip().splitlines()[-1] if out.strip() else ""


def main() -> int:
    before = git_dirty()

    with tempfile.TemporaryDirectory(prefix="atlas-mut-") as td:
        root = Path(td)
        # Copy the package and the config the tests read. Nothing is ever written
        # back into REPO.
        shutil.copytree(REPO / "atlas", root / "atlas",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO / "config.yaml", root / "config.yaml")

        baseline_passed, baseline_failed, baseline_tail = run_suite(root)
        print(f"baseline (unmutated copy): {baseline_passed} passed, "
              f"{baseline_failed} failed")
        if baseline_failed or baseline_passed == 0:
            print("ABORT: the unmutated copy is not green; mutation results "
                  "would be meaningless.")
            print(baseline_tail)
            return 2

        original = (REPO / MODULE_REL).read_text()
        results = []
        for m in MUTATIONS:
            if m.old not in original:
                print(f"  [ERROR] {m.name}: anchor text not found -- the module "
                      "changed and this mutation no longer applies")
                results.append({"mutation": m.name, "applied": False,
                                "killed_by": 0, "survived": True,
                                "rationale": m.rationale})
                continue
            mutated = original.replace(m.old, m.new, 1)
            (root / MODULE_REL).write_text(mutated)
            passed, failed, _ = run_suite(root)
            killed = failed > 0
            print(f"  [{'KILLED' if killed else 'SURVIVED'}] {m.name}: "
                  f"{failed} test(s) failed")
            results.append({
                "mutation": m.name, "applied": True, "killed_by": failed,
                "survived": not killed, "rationale": m.rationale,
            })
            # restore the copy before the next mutation
            (root / MODULE_REL).write_text(original)

    after = git_dirty()
    if before != after:
        print("\nFATAL: the working tree changed during the mutation run.")
        print(f"  before: {before}\n  after:  {after}")
        return 3

    survivors = [r for r in results if r["survived"]]
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "module": str(MODULE_REL),
        "suite": str(TEST_REL),
        "baseline": {"passed": baseline_passed, "failed": baseline_failed},
        "mutations": results,
        "survivors": len(survivors),
        "working_tree_unchanged": True,
        "method": (
            "The package is copied to a temp tree and the COPY is patched; the "
            "real module is never written. A survivor means the suite cannot "
            "detect that defect and the claim it protects is unproven."
        ),
    }
    out = REPO / "engineering/raw/handout_mutation.json"
    out.write_text(json.dumps(artifact, indent=1) + "\n")
    print(f"\n-> {out.relative_to(REPO)}")
    print(f"{len(results) - len(survivors)}/{len(results)} mutations killed")

    if survivors:
        print("SURVIVORS (the suite does not actually pin these):")
        for s in survivors:
            print(f"  - {s['mutation']}")
        return 1
    print("every injected defect was caught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
