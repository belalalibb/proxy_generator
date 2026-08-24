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
import re
import sys
import subprocess
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
                # P07: this previously tested only `def {symbol}(`, so a CLASS
                # (`class ScoringPolicy`) or an ADR heading (`## ADR-026`) could
                # not be cited as evidence at all -- it reported "not defined"
                # for symbols plainly present. It failed LOUDLY rather than
                # silently, so it was an under-powered guard and not a hole; but
                # a guard that cannot express the evidence people actually have
                # invites them to cite something vaguer instead.
                #
                # Python is now parsed with AST rather than string-matched, which
                # is also STRICTER than the old substring test: `def foo(` in a
                # comment or a docstring no longer counts as a definition.
                if path_part.endswith(".py"):
                    try:
                        tree = ast.parse(text)
                    except SyntaxError as exc:
                        bad.append(f"{t['id']} -> {ev} (unparseable: {exc})")
                        continue
                    defined = {
                        n.name for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))
                    }
                    if symbol not in defined:
                        bad.append(
                            f"{t['id']} -> {ev} (no def/class named {symbol})")
                elif path_part.endswith(".md"):
                    # A markdown symbol must be a HEADING, not a passing mention,
                    # or "documented in DECISIONS.md" would be satisfied by the
                    # ADR merely being named in another ADR's prose.
                    if not re.search(rf"^#+\s.*{re.escape(symbol)}\b", text,
                                     flags=re.MULTILINE):
                        bad.append(f"{t['id']} -> {ev} (no heading for {symbol})")
                elif symbol not in text:
                    bad.append(f"{t['id']} -> {ev} (symbol absent)")
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


def check_declared_test_count_matches_collection(res: Result) -> None:
    """
    ADR-018. TASK_STATE.tests.passed must equal the number of tests pytest can
    actually COLLECT.

    Earned in P03: README claimed '87 passed' while the suite had grown to 113.
    The ADR-014(c) tag checked README *against TASK_STATE*, and TASK_STATE was
    stale by the identical amount -- so two wrong numbers agreed with each other
    and the gate passed. Cross-checking two documents proves consistency, not
    truth. This check reaches past both to the code itself.

    Collection only (--collect-only): no test is executed here, so this stays
    cheap and cannot mask a failing suite -- `make doctor` runs the suite
    separately, because passing and existing are different facts (ADR-010).
    """
    state = ROOT / "engineering" / "TASK_STATE.json"
    if not state.exists():
        res.add("declared_test_count_matches_collection", False, "TASK_STATE.json missing")
        return
    try:
        declared = json.loads(state.read_text(encoding="utf-8"))["tests"]["passed"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        res.add("declared_test_count_matches_collection", False,
                f"cannot read tests.passed ({type(exc).__name__})")
        return

    # Collect the WHOLE tests tree, not just unit/. This check previously named
    # `atlas/tests/unit/` explicitly, which meant an entire test directory could
    # be added -- or LOST to a sync, the ADR-010 failure -- without the declared
    # count ever disagreeing. A guard that only looks where it already expects
    # tests cannot detect tests going missing somewhere else.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "atlas/tests/", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    m = re.search(r"(\d+)\s+tests? collected", proc.stdout)
    if not m:
        res.add("declared_test_count_matches_collection", False,
                "could not parse pytest collection output")
        return
    collected = int(m.group(1))
    ok = collected == declared
    res.add("declared_test_count_matches_collection", ok,
            f"declared {declared}, pytest collects {collected}"
            + ("" if ok else " -- update TASK_STATE and README together"))


def check_h3_negative_control_present(res: Result) -> None:
    """
    ADR-022. The H3 concurrency proof depends on a NEGATIVE CONTROL: a deliberately
    read-then-write store that the same test body must catch.

    Without it, a green concurrency test is ambiguous -- it may mean leasing is
    atomic, or it may mean the processes never actually raced. Since the control is
    the thing that distinguishes those two readings, its disappearance would
    silently downgrade the H3 evidence from 'proven' to 'not contradicted', and
    every test would still pass.

    Checked here rather than only in pytest because a sync loss removes the FILE,
    and a test that no longer exists cannot report that it is missing.
    """
    control = ROOT / "atlas" / "tests" / "integration" / "naive_store.py"
    suite = ROOT / "atlas" / "tests" / "integration" / "test_store_lease.py"
    missing = [str(p.relative_to(ROOT)) for p in (control, suite) if not p.exists()]
    if missing:
        res.add("h3_negative_control_present", False,
                f"MISSING (H3 proof is void): {', '.join(missing)}")
        return

    body = suite.read_text(encoding="utf-8")
    # the control must be USED, not merely present
    uses = "NaiveStore" in body and "lease_naive" in body
    asserts_it_fails = "duplicates > 0" in body
    kills = "SIGKILL" in body and "returncode == -signal.SIGKILL" in body
    problems = []
    if not uses:
        problems.append("the naive store is never exercised")
    if not asserts_it_fails:
        problems.append("nothing asserts the naive store IS caught")
    if not kills:
        problems.append("H8 does not assert death by SIGKILL")
    res.add("h3_negative_control_present", not problems,
            "; ".join(problems) if problems
            else "negative control present, exercised, and asserted to fail")


def check_tests_tracked_by_git(res: Result) -> None:
    """
    ADR-010 again. Three separate syncs have deleted files in this project. Tools
    are already checked; test FILES were not, and an untracked test directory is
    the most dangerous thing to lose -- the suite simply gets smaller and stays
    green.
    """
    test_root = ROOT / "atlas" / "tests"
    on_disk = {
        str(p.relative_to(ROOT))
        for p in test_root.rglob("*.py")
        if "__pycache__" not in str(p)
    }
    proc = subprocess.run(["git", "ls-files", "atlas/tests"], cwd=ROOT,
                          capture_output=True, text=True)
    tracked = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    untracked = sorted(on_disk - tracked)
    res.add("tests_tracked_by_git", not untracked,
            f"UNTRACKED (a sync will drop these): {', '.join(untracked)}"
            if untracked else f"all {len(on_disk)} test file(s) tracked")


def check_makefile_tools_exist(res: Result) -> None:
    """
    Every $(TOOLS)/x.py referenced by the Makefile must exist on disk.

    `make sources-audit` invoked reprobe_empty.py for multiple phases after a sync
    deleted it. Nothing caught this because no gate ever RAN that target, and a
    Makefile recipe is invisible to the import graph and to pytest. A documented
    command that cannot execute is a false claim about the project's own tooling.
    """
    mk = ROOT / "Makefile"
    if not mk.exists():
        res.add("makefile_tools_exist", False, "Makefile missing")
        return
    text = mk.read_text(encoding="utf-8")
    referenced = sorted(set(re.findall(r"\$\(TOOLS\)/([A-Za-z0-9_]+\.py)", text)))
    missing = [t for t in referenced if not (ROOT / "engineering" / "tools" / t).exists()]
    res.add("makefile_tools_exist", not missing,
            f"Makefile references missing tool(s): {', '.join(missing)}"
            if missing else f"all {len(referenced)} Makefile-referenced tool(s) exist")


def check_no_cross_stream_splice(res: Result) -> None:
    """
    ADR-020. The legacy run left TWO records: proxy_details.json (n=102) and
    proxy_scraper.log (n=118). They share p95/max/min but differ on p50, mean and
    both over_* percentages.

    Six files quoted the n=118 pair (95.8% / 56.8%) in the same sentence as the
    n=102 p50/p95, producing composite claims that no single distribution
    supports. Every number was individually real and traceable to an artifact --
    which is exactly why ADR-014(c) and ADR-018 both passed it. An anchored claim
    can still be a SPLICED one.

    This check requires any prose citing 95.8 or 56.8 to also name its stream, so
    the n=118 figures can never again sit unqualified beside n=102 ones.
    """
    stream_b_only = ("95.8", "56.8")
    qualifiers = ("n=118", "n = 118", "log stream", "ADR-020", "STREAM B",
                  "proxy_scraper.log", "n118")
    targets = ["README.md", "config.yaml",
               "engineering/BUG_LEDGER.md", "engineering/RESUME_PROMPT.md",
               "atlas/core/policy/admission.py", "atlas/core/domain/verdict.py"]
    offenders: list[str] = []
    for rel in targets:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if any(v in line for v in stream_b_only):
                # the qualifier may be on the line or in the surrounding block;
                # require it within the line itself to keep the rule mechanical
                if not any(q in line for q in qualifiers):
                    offenders.append(f"{rel}:{i}")
    res.add("no_cross_stream_splice", not offenders,
            "n=118 figures cited without naming the stream: " + ", ".join(offenders)
            if offenders else
            "every 95.8/56.8 citation names its n=118 stream")


def check_no_percentile_ordering_violation(res: Result) -> None:
    """
    ADR-024 / V4-02. A p95 below the p50 is arithmetically impossible, so any
    artifact containing one is proof that the estimator read the wrong end of the
    distribution.

    This is checked against the ARTIFACTS rather than by unit test on purpose:
    the defect was invisible to every unit test (all written at k=5) and visible
    on the first line of real output. The gate now reads what the tools actually
    produced -- the same reason ADR-020's splice check greps prose instead of
    trusting that the numbers came from one run.

    Scans every calibration report for records where p95 < p50.
    """
    raw = ROOT / "engineering" / "raw"
    offenders: list[str] = []
    scanned = 0
    for f in sorted(raw.glob("admission_live*.json")) + sorted(raw.glob("calib*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            offenders.append(f"{f.name}: unreadable ({type(exc).__name__})")
            continue
        records = []
        for key in ("admitted_detail", "sample_of_gate_reachers"):
            val = doc.get(key)
            if isinstance(val, list):
                records.extend(val)
        for rec in records:
            if not isinstance(rec, dict):
                continue
            p50, p95 = rec.get("p50_ms"), rec.get("p95_ms")
            if isinstance(p50, (int, float)) and isinstance(p95, (int, float)):
                scanned += 1
                if p95 < p50:
                    offenders.append(
                        f"{f.name}:{rec.get('endpoint', '?')} p95={p95} < p50={p50}")
    res.add("no_percentile_ordering_violation", not offenders,
            "p95 below p50 (ADR-024 estimator defect): " + "; ".join(offenders[:5])
            if offenders else
            f"{scanned} measured record(s) satisfy p95 >= p50")


def check_cited_adrs_exist(res: Result) -> None:
    """
    ADR-027's sibling, earned in P07: every ADR-NNN cited by executable code must
    actually EXIST in DECISIONS.md.

    Why this direction is new. `check_adr_claims_are_verifiable` walks
    DECISIONS.md and asks "does this ADR name a way to check it?" -- ADR -> code.
    Nothing walked the reverse edge. So `atlas/engine/cycle.py` shipped, tested
    and green, citing **ADR-026 five times** while DECISIONS.md stopped at
    ADR-025: the engine's central design decision (feeding probe results back
    onto the source row) existed only as a docstring reference to a document that
    did not describe it. A reader following the citation found nothing.

    That is the ADR-014 defect with its arrows reversed. ADR-014 was earned when
    an ADR described code that did not exist; this is code citing an ADR that
    does not exist. Both are dangling references, and a guard covering only one
    direction leaves the other free.

    Scans executable code only (atlas/), never the engineering prose, because a
    ledger legitimately discusses ADR numbers in the past tense.
    """
    decisions = ROOT / "engineering" / "DECISIONS.md"
    if not decisions.exists():
        res.add("cited_adrs_exist", False, "DECISIONS.md missing")
        return

    defined = set(re.findall(r"^## (ADR-\d+)", decisions.read_text(encoding="utf-8"),
                             flags=re.MULTILINE))
    if not defined:
        res.add("cited_adrs_exist", False, "no ADR headings parsed from DECISIONS.md")
        return

    dangling: list[str] = []
    scanned = 0
    for py in sorted((ROOT / "atlas").rglob("*.py")):
        scanned += 1
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for cited in re.findall(r"ADR-\d+", line):
                if cited not in defined:
                    rel = py.relative_to(ROOT)
                    dangling.append(f"{rel}:{lineno} cites {cited}")

    res.add("cited_adrs_exist", not dangling,
            f"code cites undocumented ADR(s): {'; '.join(dangling[:5])}"
            if dangling else
            f"{scanned} module(s) scanned; every cited ADR is defined "
            f"({len(defined)} ADRs)")


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
    check_declared_test_count_matches_collection(res)
    check_no_cross_stream_splice(res)
    check_h3_negative_control_present(res)
    check_tests_tracked_by_git(res)
    check_makefile_tools_exist(res)
    check_no_percentile_ordering_violation(res)
    check_cited_adrs_exist(res)

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
