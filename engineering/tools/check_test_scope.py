#!/usr/bin/env python3
"""
Prove that the DEFAULT test command reaches the integration tests.

ADR-022 states that `make doctor` runs the real-concurrency and SIGKILL tests.
That claim rests entirely on root `pytest -q` discovering atlas/tests/integration
-- which is a property of pytest configuration, not of anything asserted anywhere.
A stray testpaths/norecursedirs entry in pytest.ini or pyproject.toml would
silently shrink the suite while every gate stayed green: precisely the vacuous-pass
failure mode of ADR-010.

So the scope is measured, not assumed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only"],
        cwd=ROOT, capture_output=True, text=True,
    )
    out = proc.stdout
    integration = len(re.findall(r"atlas/tests/integration/\S+::", out))
    unit = len(re.findall(r"atlas/tests/unit/\S+::", out))
    m = re.search(r"(\d+)\s+tests? collected", out)
    total = int(m.group(1)) if m else -1

    print(f"  default `pytest -q` collects : {total}")
    print(f"    unit                       : {unit}")
    print(f"    integration                : {integration}")

    if integration == 0:
        print("  FAIL: the default test run does not reach atlas/tests/integration,\n"
              "        so ADR-022's H3/H8 evidence is NOT part of `make doctor`.")
        return 1
    if unit == 0:
        print("  FAIL: the default test run collected no unit tests.")
        return 1
    if total != unit + integration:
        print(f"  WARNING: {total} collected but unit+integration = "
              f"{unit + integration}; some tests live elsewhere.")
    print("  OK: `make test` covers unit AND integration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
