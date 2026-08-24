#!/usr/bin/env python3
"""
Mutation check for the P09 per-host rate limiter (ADR-034).

WHY THIS EXISTS

`test_rate_limit.py` passes. That fact alone says nothing: a test that cannot
fail is not evidence (ADR-010), and this project has already shipped three guards
that matched their own documentation instead of the code (ADR-014, ADR-022,
ADR-023). A rate limiter is a security control, so "the tests are green" is an
especially cheap claim -- the interesting question is whether the suite can tell
a working limiter from a bypassable one.

Each mutation below is a REAL bypass or lockout, not a syntactic tweak:

  * a fixed window instead of a sliding one       -> 2x the configured rate
  * wall-clock instead of monotonic               -> clock adjustment bypass
  * `check()` that records                        -> budget spent on refusals
  * evicting a live host at the cap               -> spray-to-reset bypass
  * raw host as the bucket key                    -> case/dot doubles the rate
  * off-by-one on the limit                       -> limit+1 admitted
  * port in the key                               -> per-port budgets

PROCESS CONSTRAINT, inherited from mutate_handout.py (PROGRESS.md P08 pre-work)

An earlier mutation run left a broken file on disk between steps; an auto-sync
committed the mutant. So this tool NEVER writes a mutant to the real module path.
It copies the package into a temp tree, patches the COPY, and verifies via
git-status that the working tree was untouched.
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
MODULE_REL = Path("atlas/engine/rate_limit.py")
TEST_REL = Path("atlas/tests/unit/test_rate_limit.py")


class Mutation:
    def __init__(self, name: str, old: str, new: str, rationale: str) -> None:
        self.name = name
        self.old = old
        self.new = new
        self.rationale = rationale


MUTATIONS = [
    Mutation(
        "fixed_window_instead_of_sliding",
        old="""        cutoff = now - window_ms
        while hits and hits[0] <= cutoff:
            hits.popleft()""",
        new="""        cutoff = now - window_ms
        if hits and hits[0] <= cutoff:
            hits.clear()""",
        rationale=(
            "Turn the sliding window into a fixed one: the whole bucket resets "
            "as soon as its oldest hit expires, so a caller gets `limit` at the "
            "end of one window and `limit` more immediately after -- 2x the "
            "configured rate across the boundary."
        ),
    ),
    Mutation(
        "wall_clock_instead_of_monotonic",
        old="        now = self._clock.monotonic_ms()      # monotonic; see the module docstring",
        new="        now = self._clock.now().timestamp() * 1000.0",
        rationale=(
            "Measure the window with wall-clock time. A clock adjustment then "
            "either bypasses the limit (jump forward) or locks the caller out "
            "with no error naming why (jump back)."
        ),
    ),
    Mutation(
        "check_records_a_hit",
        old="""    def check(self, target: Target) -> RateDecision:
        \"\"\"Would this be admitted? Records NOTHING. See the check-then-commit note.\"\"\"
        with self._lock:
            return self._decide(target, commit=False)""",
        new="""    def check(self, target: Target) -> RateDecision:
        \"\"\"Would this be admitted? Records NOTHING. See the check-then-commit note.\"\"\"
        with self._lock:
            return self._decide(target, commit=True)""",
        rationale=(
            "Make the read-only probe spend budget. A request refused for some "
            "OTHER reason then costs the host a slot, and the operator sees a "
            "rate-limit refusal whose cause was elsewhere entirely (B-02)."
        ),
    ),
    Mutation(
        "evict_a_live_host_at_the_cap",
        old="""                if not self._evict_drained(now, window_ms):""",
        new="""                self._hits.pop(next(iter(self._hits)), None)
                if False:""",
        rationale=(
            "Evict an arbitrary (possibly ACTIVE) host to make room at the cap. "
            "Spraying distinct hostnames then resets every real host's counter: "
            "the memory bound becomes a rate-limit bypass."
        ),
    ),
    Mutation(
        "raw_host_as_bucket_key",
        old="        host = canonical_host(split_url(target.url).host)",
        new="        host = split_url(target.url).host",
        rationale=(
            "Key on the un-canonicalised host. `split_url` leaves the root's "
            "trailing dot on, so `a.com.` and `a.com` become two buckets for "
            "one host -- silently doubling the rate for anyone who types it."
        ),
    ),
    Mutation(
        "off_by_one_on_the_limit",
        old="        if observed >= limit:",
        new="        if observed > limit:",
        rationale=(
            "Admit limit+1 requests per window. Small enough to look like a "
            "rounding detail, and it makes the configured number wrong."
        ),
    ),
    Mutation(
        "port_included_in_the_key",
        old="        host = canonical_host(split_url(target.url).host)\n        return host or None",
        new="""        _p = split_url(target.url)
        host = canonical_host(_p.host)
        if host and _p.port:
            host = f"{host}:{_p.port}"
        return host or None""",
        rationale=(
            "Give each port its own budget, so :80 and :443 each spend the full "
            "allowance against one origin -- the limit no longer protects the "
            "host it names (ADR-006)."
        ),
    ),
    Mutation(
        "unkeyable_target_waved_through",
        old="""            return RateDecision(
                allowed=False, host=None, refusal=RateRefusal.NO_HOST,
                remaining=0, retry_after_s=0.0, observed=0,
            )""",
        new="""            return RateDecision(
                allowed=True, host=None, refusal=None,
                remaining=limit, retry_after_s=0.0, observed=0,
            )""",
        rationale=(
            "Fail OPEN for a target with no host: an unkeyable target becomes "
            "unlimited, and the limiter disagrees with check_target's NO_HOST."
        ),
    ),
    Mutation(
        "retry_after_is_a_guess",
        old="            retry_after_s = max(0.0, (hits[0] + window_ms - now) / 1000.0)",
        new="            retry_after_s = self._policy.window_s",
        rationale=(
            "Report a whole window as the retry delay instead of the computed "
            "moment the window opens. A client that honours it sleeps far "
            "longer than required, which is how 'retry later' becomes useless."
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
    """Run the limiter suite inside `tree`. Returns (passed, failed, tail)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tree / TEST_REL), "-q",
         "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    passed = failed = 0
    for line in out.splitlines():
        s = line.strip()
        m = re.search(r"(\d+) failed", s)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) passed", s)
        if m:
            passed = int(m.group(1))
    # An error (collection failure, crash) is also a kill, but it must be
    # DISTINGUISHABLE from a test failure or a broken mutant would look like a
    # working guard.
    errors = 0
    m = re.search(r"(\d+) error", out)
    if m:
        errors = int(m.group(1))
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    return passed, failed + errors, tail


def main() -> int:
    before = git_dirty()

    with tempfile.TemporaryDirectory(prefix="atlas-mut-rl-") as td:
        root = Path(td)
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
    out = REPO / "engineering/raw/rate_limit_mutation.json"
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
