#!/usr/bin/env python3
"""
GATE CHECK — the guard that should have existed before the sync loss (ADR-010).

Two process defects allowed Phase 0 to look complete while it was not:

  1. EVIDENCE WAS DECLARED, NEVER VERIFIED.
     TASK_STATE.json kept 6 tasks at status=DONE whose `evidence` paths no longer
     existed on disk. Nothing checked.

  2. A GREEN TEST SUITE PROVED NOTHING.
     `pytest -q` reported "10 passed" while atlas/core/ did not exist -- the
     architecture isolation tests were globbing an empty directory and passing
     VACUOUSLY. A test that cannot fail is not evidence.

This tool fails the build on either condition. Run it in `make doctor` and before
every phase-gate claim.

Exit codes: 0 = all checks pass · 1 = at least one FAIL
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK_STATE = ROOT / "engineering" / "TASK_STATE.json"
ATLAS = ROOT / "atlas"
CORE = ATLAS / "core"

# Phase-gate 0 required evidence (section 21).
GATE0_FILES = [
    "engineering/ANALYSIS.md",
    "engineering/SOURCE_INVENTORY.json",
    "engineering/BASELINE.json",
    "engineering/BUG_LEDGER.md",
    "engineering/MIGRATION_LEDGER.md",
]


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]


def check_declared_evidence(res: Result) -> None:
    """Every task marked DONE must have all of its evidence on disk."""
    if not TASK_STATE.exists():
        res.add("task_state_exists", False, "engineering/TASK_STATE.json missing")
        return
    state = json.loads(TASK_STATE.read_text(encoding="utf-8"))

    bad: list[str] = []
    for t in state.get("tasks", []):
        if t.get("status") != "DONE":
            continue
        for ev in t.get("evidence", []):
            # Evidence may be "path" or "path::symbol" (e.g. a specific test).
            # Verifying only the path would let a task cite a test that does not
            # exist, so the symbol is checked too. Found 2026-08-24: this check
            # had been stringly-comparing the whole "path::name" as a filename,
            # so every ::-form silently "existed". Latent until P02 first used it.
            path_part, _, symbol = ev.partition("::")
            target = ROOT / path_part
            if not target.exists():
                bad.append(f"{t['id']} -> {ev} (no such file)")
                continue
            if symbol:
                try:
                    text = target.read_text(encoding="utf-8")
                except OSError as exc:
                    bad.append(f"{t['id']} -> {ev} (unreadable: {exc})")
                    continue
                if f"def {symbol}(" not in text:
                    bad.append(f"{t['id']} -> {ev} (symbol not defined in file)")
    res.add("done_tasks_have_evidence", not bad,
            "missing: " + "; ".join(bad) if bad else
            "every DONE task's evidence exists on disk")

    missing_fc = [f for f in state.get("files_changed", []) if not (ROOT / f).exists()]
    res.add("files_changed_all_exist", not missing_fc,
            f"{len(missing_fc)} declared file(s) absent: "
            + "; ".join(missing_fc[:6]) + ("..." if len(missing_fc) > 6 else "")
            if missing_fc else "all declared files present")


def check_gate0(res: Result) -> None:
    missing = [f for f in GATE0_FILES if not (ROOT / f).exists()]
    res.add("phase_gate_0_evidence", not missing,
            "missing: " + ", ".join(missing) if missing
            else f"all {len(GATE0_FILES)} required files present")


def check_tools_are_tracked(res: Result) -> None:
    """The sync loss happened because engineering/tools/ was untracked."""
    import subprocess
    tools = ROOT / "engineering" / "tools"
    if not tools.exists():
        res.add("tools_dir_exists", False, "engineering/tools/ is absent")
        return
    py = sorted(p for p in tools.glob("*.py"))
    res.add("tools_dir_exists", bool(py), f"{len(py)} tool(s) on disk")
    try:
        out = subprocess.run(["git", "ls-files", "engineering/tools"],
                             cwd=ROOT, capture_output=True, text=True, timeout=20)
        tracked = {Path(l).name for l in out.stdout.split() if l.strip()}
        untracked = [p.name for p in py if p.name not in tracked]
        res.add("tools_tracked_by_git", not untracked,
                "UNTRACKED (a sync will drop these): " + ", ".join(untracked)
                if untracked else f"all {len(py)} tools tracked")
    except (OSError, subprocess.SubprocessError) as exc:
        res.add("tools_tracked_by_git", False, f"git query failed: {exc!r}")


def _py_modules(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in str(p) and p.name != "__init__.py")


def check_tests_not_vacuous(res: Result) -> None:
    """
    The architecture tests scan atlas/core/. If there are no modules there, they
    pass without asserting anything. Fail loudly instead of banking a green run.
    """
    core_mods = _py_modules(CORE)
    res.add("core_has_modules_to_scan", bool(core_mods),
            f"{len(core_mods)} module(s) under atlas/core/" if core_mods
            else "atlas/core/ has no modules -- architecture tests would pass "
                 "VACUOUSLY (this is what hid the sync loss)")

    test_file = ATLAS / "tests" / "unit" / "test_architecture.py"
    if test_file.exists() and core_mods:
        # confirm the isolation test actually reaches those modules
        res.add("architecture_test_present", True,
                f"{test_file.relative_to(ROOT)} will scan {len(core_mods)} module(s)")
    elif test_file.exists():
        res.add("architecture_test_present", True,
                f"{test_file.relative_to(ROOT)} exists but has nothing to scan")
    else:
        res.add("architecture_test_present", False, "test_architecture.py missing")


def check_state_consistency(res: Result) -> None:
    """A phase cannot be PASSED while any of its tasks is not DONE."""
    if not TASK_STATE.exists():
        return
    state = json.loads(TASK_STATE.read_text(encoding="utf-8"))
    gates = state.get("phase_gate_status", {})
    bad: list[str] = []
    for t in state.get("tasks", []):
        phase = t["id"].split(".")[0]
        if gates.get(phase) == "PASSED" and t.get("status") != "DONE":
            bad.append(f"{t['id']} is {t.get('status')} but {phase} is PASSED")
    res.add("phase_gates_consistent", not bad,
            "; ".join(bad) if bad else "no phase claims PASSED with unfinished tasks")


def check_adr_claims_are_verifiable(res: Result) -> None:
    """
    ADR-014. An ADR that claims an implementation must name a way to check it.

    Why: ADR-012 and ADR-013 were written, committed and cited in README.md while
    the code they described did not exist. Task evidence was verified; prose was
    not. A decision record that cannot be falsified is an intention.
    """
    decisions = ROOT / "engineering" / "DECISIONS.md"
    if not decisions.exists():
        res.add("adr_claims_are_verifiable", False, "DECISIONS.md missing")
        return

    text = decisions.read_text(encoding="utf-8")
    # split into ADR sections
    blocks: list[tuple[str, str]] = []
    current_id, buf = None, []
    for line in text.splitlines():
        if line.startswith("## ADR-"):
            if current_id:
                blocks.append((current_id, "\n".join(buf)))
            current_id, buf = line.split("—")[0].replace("##", "").strip(), []
        else:
            buf.append(line)
    if current_id:
        blocks.append((current_id, "\n".join(buf)))

    # An ADR asserts an implementation if its Decision names a concrete artifact.
    impl_markers = ("gate_check.py", "iter_chunked", "negative control",
                    "reason code", "measure_baseline.py", "test_architecture.py",
                    "make doctor")
    offenders: list[str] = []
    for adr_id, body in blocks:
        status_proposed = "Status: PROPOSED" in body or "**Status:** PROPOSED" in body
        # only the Decision section states what WE build; Context/Alternatives
        # legitimately name legacy files as subject matter (e.g. ADR-001 cites v1.py).
        decision = body.split("**Decision")[1] if "**Decision" in body else ""
        decision = decision.split("**Alternatives")[0]
        claims_impl = any(m in decision for m in impl_markers)
        has_verify = "**Verify:**" in body
        if claims_impl and not has_verify and not status_proposed:
            offenders.append(adr_id)

    res.add("adr_claims_are_verifiable", not offenders,
            f"{len(blocks)} ADR(s); missing **Verify:** -> " + ", ".join(offenders)
            if offenders else f"{len(blocks)} ADR(s) checked")


def check_readme_claims(res: Result) -> None:
    """
    ADR-014(c). Numeric README claims tagged <!--verify:file:jsonpath--> are
    re-derived from the artifact. README once claimed '19 passed' and '68 ACTIVE'
    when the suite was failing and the artifact said 61.
    """
    readme = ROOT / "README.md"
    if not readme.exists():
        res.add("readme_numbers_have_artifacts", False, "README.md missing")
        return
    import re as _re

    bad: list[str] = []
    checked = 0
    pattern = _re.compile(r"<!--verify:([^:]+):([^:]+):([^-]+)-->")
    for line in readme.read_text(encoding="utf-8").splitlines():
        m = pattern.search(line)
        if not m:
            continue
        checked += 1
        rel, dotted, expected = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        target = ROOT / rel
        if not target.exists():
            bad.append(f"{rel} absent")
            continue
        try:
            node = json.loads(target.read_text(encoding="utf-8"))
            for key in dotted.split("."):
                node = node[key] if not key.isdigit() else node[int(key)]
            if str(node) != expected:
                bad.append(f"{rel}:{dotted} = {node!r}, README says {expected!r}")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            bad.append(f"{rel}:{dotted} unreadable ({type(exc).__name__})")

    res.add("readme_numbers_have_artifacts", not bad,
            "; ".join(bad) if bad else f"{checked} tagged claim(s) re-derived from artifacts")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = Result()
    check_declared_evidence(res)
    check_gate0(res)
    check_tools_are_tracked(res)
    check_tests_not_vacuous(res)
    check_state_consistency(res)
    check_adr_claims_are_verifiable(res)
    check_readme_claims(res)

    if args.json:
        print(json.dumps(
            {"checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in res.rows],
             "failed": len(res.failed)}, indent=2))
        return 1 if res.failed else 0

    print("=" * 74)
    print("GATE CHECK — evidence integrity + non-vacuous tests (ADR-010)")
    print("=" * 74)
    for name, ok, detail in res.rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if detail:
            print(f"         {detail}")
    print("-" * 74)
    if res.failed:
        print(f"  {len(res.failed)} CHECK(S) FAILED — a phase gate may not be claimed.")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
