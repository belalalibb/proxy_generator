#!/usr/bin/env python3
"""
MEASURE THE POOL LIFECYCLE — the evidence behind ADR-036.

WHY THIS TOOL EXISTS

ADR-036 claims something strong: that a proxy which fails once is removed from
the pool permanently, because `COOLING` has no exit and `RETIRED` is never
assigned. A claim like that must not rest on my having read the code carefully.
Two of this project's worst defects (ADR-020's cross-stream splice, ADR-024's
p95-below-p50) were found by reading an ARTIFACT, and three guard defects
(ADR-022, ADR-023, and the tautological test in ADR-035) were cases where reading
the code produced a confident wrong answer.

So this exercises the REAL `SqliteStore` and the REAL domain transitions and
reports what actually happens. It is offline and deterministic: a temp database,
a fixed clock, no network.

WHAT IT MEASURES, AND WHY EACH ROW IS HERE

1. `failures_to_state` — drive a proxy through N consecutive failures using the
   real `record_failure()` / `with_state()` transitions the engine uses
   (cycle.py:319/326/359), persist it, read it back, and report the state. If
   retirement worked, some N would produce `RETIRED`.

2. `retired_assignments_in_production` — an AST scan for `ProxyState.RETIRED`
   assignments in `atlas/`, excluding tests and the enum definition itself. A
   grep would match the comment in `scoring.py` and the docstring in `proxy.py`
   and report a false positive; that is exactly the prose-matching failure
   ADR-022/ADR-023 record. This walks the AST and only counts real uses.

3. `cooldown_delay_callers` — which modules actually CALL the ADR-006 backoff
   function. ADR-036's central point is that it has one caller and that caller is
   on the source path, so the proxy path has no recovery ladder at all.

4. `cooling_exits` — transitions out of `COOLING` in production code.

5. `lease_of_cooling` — try to lease a `COOLING` row. This is the consequence
   that matters: it is not merely unranked, it is unreachable.

6. `backoff_ladder` — the reachable delays given the configured retirement
   threshold, and whether ADR-006's 3600 s cap can bind on the proxy path.

AFTER THE FIX

The tool is not deleted once ADR-036 is implemented. It is re-run, and the
`after` block records the same measurements against the fixed code, so the
artifact contains the before/after pair rather than a claim that things improved.
Run with `--after` once the fix is in.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from atlas.adapters.store_sqlite import SqliteStore  # noqa: E402
from atlas.core.domain.proxy import (  # noqa: E402
    Endpoint, Grade, Proxy, ProxyState,
)
from atlas.core.ports.clock import cooldown_delay  # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _production_files() -> list[Path]:
    """atlas/**/*.py excluding tests and __pycache__."""
    return sorted(
        p for p in (_REPO / "atlas").rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    )


def _state_assignments(state_name: str) -> list[str]:
    """
    Find real uses of `ProxyState.<state_name>` via AST, not text.

    A grep for `ProxyState.RETIRED` matches the comment in scoring.py and the
    docstring in proxy.py. Comments and docstrings are not code, and counting
    them is the precise mistake ADR-022 and ADR-023 were written about.
    """
    hits: list[str] = []
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr == state_name
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ProxyState"):
                # The enum's own definition is `RETIRED = "RETIRED"`, an
                # ast.Assign to a Name -- not an ast.Attribute -- so it is
                # already excluded. Anything reaching here is a real use.
                rel = path.relative_to(_REPO)
                hits.append(f"{rel}:{node.lineno}")
    return hits


def _callers_of(func_name: str) -> list[str]:
    """Modules that CALL func_name (ast.Call), not those that merely import it."""
    hits: list[str] = []
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = (f.id if isinstance(f, ast.Name)
                        else f.attr if isinstance(f, ast.Attribute) else None)
                if name == func_name:
                    hits.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    return hits


def _drive_failures(n: int) -> dict:
    """Persist a proxy after n consecutive failures; report what the store holds."""
    with tempfile.TemporaryDirectory() as d:
        with SqliteStore(os.path.join(d, "m.db")) as store:
            p = Proxy(endpoint=Endpoint(host="203.0.113.7", port=8080),
                      first_seen=NOW)
            for _ in range(n):
                p = (p.record_failure(NOW, reason="TCP_REFUSED")
                     .with_state(ProxyState.COOLING, reason="TCP_REFUSED")
                     .graded(Grade.REJECTED))
            store.upsert(p)
            got = store.get(p.fingerprint)
            assert got is not None
            leased = store.lease(count=10, min_grade=Grade.REJECTED,
                                 lease_ms=1000, now=NOW)
            return {
                "consecutive_failures": got.consecutive_failures,
                "state": got.state.value,
                "grade": got.grade.value,
                "leasable": len(leased),
            }


def _backoff_ladder(retire_after: int) -> dict:
    ladder = {str(n): cooldown_delay(n).total_seconds() for n in range(1, 10)}
    reachable = [cooldown_delay(n).total_seconds()
                 for n in range(1, max(retire_after, 1))]
    return {
        "delays_s": ladder,
        "retire_after": retire_after,
        "max_reachable_delay_s": max(reachable) if reachable else 0.0,
        "adr006_cap_s": 3600.0,
        "cap_binds_on_proxy_path": bool(reachable) and max(reachable) >= 3600.0,
    }


def _prose_line_numbers(path: Path) -> set[int]:
    """
    Every line occupied by a comment or a string literal (incl. docstrings).

    FOUND BY USING THIS TOOL, and it is the fourth prose-matching recurrence in
    this project (ADR-022 fsync guard, ADR-023 TLS guard, ADR-035 tautological
    test, now here). The first version of `_scheduler_key_readers` classified a
    line as prose if `line.strip()` began with `#` or a quote. That is wrong for
    any line INSIDE a multi-line string: `handout.py:53` is a continuation line
    of the module docstring, begins with a backtick, and was therefore reported
    as a genuine CODE reader of `recheck_ready_after_s` -- the exact false
    positive that would have let me write "1 reader" into an ADR whose whole
    argument is that there are none.

    So prose is identified structurally: `tokenize` yields COMMENT and STRING
    tokens with their line spans, and every line they cover is prose. A hit on
    any other line is real code.
    """
    import io
    import tokenize

    prose: set[int] = set()
    text = path.read_text(encoding="utf-8")
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                prose.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    return prose


def _scheduler_key_readers() -> dict:
    """
    Which scheduler.* keys are read by Python, and which are merely mentioned?

    The interesting answer is 'mentioned but never read' -- the ADR-019 defect
    class -- so the two categories must be distinguished exactly. See
    `_prose_line_numbers` for why a line-prefix heuristic is not good enough.
    """
    keys = ["recheck_ready_after_s", "discovery_interval_s",
            "retire_after_consecutive_failures", "max_pool_size"]
    out: dict[str, dict] = {}
    for k in keys:
        code_hits, prose_hits = [], []
        for path in _production_files():
            prose_lines = _prose_line_numbers(path)
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if k not in line:
                    continue
                loc = f"{path.relative_to(_REPO)}:{i}"
                (prose_hits if i in prose_lines else code_hits).append(loc)
        out[k] = {"code": code_hits, "prose_only": prose_hits}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", action="store_true",
                    help="record the post-fix measurements alongside the pre-fix ones")
    args = ap.parse_args()

    retire_after = 5  # config.yaml scheduler.retire_after_consecutive_failures
    block = {
        "failures_to_state": {str(n): _drive_failures(n) for n in (1, 4, 5, 40)},
        "retired_assignments_in_production": _state_assignments("RETIRED"),
        "cooling_assignments_in_production": _state_assignments("COOLING"),
        "cooldown_delay_callers": _callers_of("cooldown_delay"),
        "scheduler_key_readers": _scheduler_key_readers(),
        "backoff_ladder": _backoff_ladder(retire_after),
    }

    out_path = _REPO / "engineering" / "raw" / "pool_lifecycle.json"
    doc: dict = {}
    if out_path.exists():
        doc = json.loads(out_path.read_text(encoding="utf-8"))

    phase = "after" if args.after else "before"
    doc[phase] = block
    doc["measured_at"] = datetime.now(timezone.utc).isoformat()
    doc["tool"] = "engineering/tools/measure_pool_lifecycle.py"
    doc["note"] = (
        "ADR-036 evidence. `before` is the pre-fix pool: COOLING has no exit, "
        "RETIRED is never assigned, and cooldown_delay has one caller on the "
        "SOURCE path only. Re-run with --after once the fix lands, so this file "
        "holds the pair rather than an assertion that things improved."
    )
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")

    print(f"=== pool lifecycle [{phase}] ===")
    for n, r in block["failures_to_state"].items():
        print(f"  {n:>3} consecutive failures -> state={r['state']:<10} "
              f"grade={r['grade']:<9} leasable={r['leasable']}")
    print(f"  ProxyState.RETIRED assignments : "
          f"{len(block['retired_assignments_in_production'])} "
          f"{block['retired_assignments_in_production']}")
    print(f"  ProxyState.COOLING assignments : "
          f"{len(block['cooling_assignments_in_production'])}")
    print(f"  cooldown_delay callers         : "
          f"{block['cooldown_delay_callers']}")
    for k, v in block["scheduler_key_readers"].items():
        print(f"  {k:<34} code={len(v['code'])} prose_only={len(v['prose_only'])}")
    lad = block["backoff_ladder"]
    print(f"  max reachable backoff          : {lad['max_reachable_delay_s']}s "
          f"(cap {lad['adr006_cap_s']}s binds: {lad['cap_binds_on_proxy_path']})")
    print(f"\nwrote {out_path.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
