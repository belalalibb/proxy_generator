#!/usr/bin/env python3
"""ADR-015 verifier: a field named for octets must not hold decoded characters.

This proves the unit defect from STORED evidence only (no network), and asserts
the source tree no longer reintroduces it. Exit non-zero on any violation.

Run: python3 engineering/tools/verify_units.py
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = ROOT / "engineering" / "raw"
TOOLS = ROOT / "engineering" / "tools"
SNAPSHOT = RAW / "source_probe_20260824T010038Z.json"
GEONODE = RAW / "geonode_body.txt"

failures: list[str] = []
notes: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    (notes if ok else failures).append(f"[{'PASS' if ok else 'FAIL'}] {label}\n       {detail}")


# ---------------------------------------------------------------- 1. the cause
# len(str) < len(bytes) exactly when the body contains multi-byte characters.
if GEONODE.exists():
    raw = GEONODE.read_bytes()
    chars = raw.decode("utf-8", errors="replace")
    non_ascii = sum(1 for c in chars if ord(c) > 127)
    check(
        "octets_and_chars_differ_on_real_evidence",
        len(raw) != len(chars) and non_ascii > 0,
        f"geonode_body.txt: {len(raw)} octets vs {len(chars)} chars "
        f"(diff {len(raw) - len(chars)}), {non_ascii} non-ASCII chars",
    )
    # The historical "230 019 B" claim is a CHARACTER count, stated plainly.
    check(
        "documented_230019_is_the_char_count_not_bytes",
        len(chars) == 230019 and len(raw) == 230067,
        f"chars={len(chars)} (what old docs called 'B'), octets={len(raw)}",
    )
else:
    check("octets_and_chars_differ_on_real_evidence", False, f"missing {GEONODE}")

# --------------------------------------------------- 2. no counterexamples
# If multi-byte decoding is the cause, chars > bytes must NEVER happen.
if SNAPSHOT.exists():
    results = json.loads(SNAPSHOT.read_text())["results"]
    both = [
        (r["bytes"], r["body_bytes"])
        for r in results
        if "bytes" in r and r.get("body_bytes") is not None
    ]
    greater = [(c, b) for c, b in both if c > b]
    check(
        "zero_counterexamples_chars_gt_octets",
        both and not greater,
        f"{len(both)} rows carry both fields; chars<octets={sum(1 for c, b in both if c < b)}, "
        f"equal={sum(1 for c, b in both if c == b)}, chars>octets={len(greater)} (must be 0)",
    )
else:
    check("zero_counterexamples_chars_gt_octets", False, f"missing {SNAPSHOT}")

# ------------------------------------------- 3. the tree does not reintroduce it
# AST scan: a dict key named "bytes"/"*_bytes" must not be assigned len(<decoded str>).
DECODED = {"body", "text"}  # names known to hold decoded str in these tools


def offending_pairs(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            key = k.value
            if not (key == "bytes" or key.endswith("_bytes")):
                continue
            if not (isinstance(v, ast.Call) and getattr(v.func, "id", None) == "len"):
                continue
            if not v.args:
                continue
            arg = v.args[0]
            name = getattr(arg, "id", None)
            attr = arg.attr if isinstance(arg, ast.Attribute) else None
            if name in DECODED or attr == "text":
                bad.append(f"line {k.lineno}: {key!r} = len({name or '.' + str(attr)})")
    return bad


scanned = 0
for py in sorted(TOOLS.glob("*.py")):
    if py.name == pathlib.Path(__file__).name:
        continue
    scanned += 1
    found = offending_pairs(ast.parse(py.read_text()))
    if found:
        failures.append(f"[FAIL] octet_field_holds_decoded_len\n       {py.name}: " + "; ".join(found))
if scanned and not any("octet_field_holds_decoded_len" in f for f in failures):
    check("no_octet_field_assigned_decoded_len", True, f"{scanned} tool module(s) clean (AST)")

# --------------------------------------------------- 4. negative control
# The guard must actually fire on known-bad source, or it proves nothing.
BAD = 'meta = {"bytes": len(body), "ok": 1}'
GOOD = 'meta = {"body_chars": len(body), "body_bytes": len(raw)}'
check(
    "guard_negative_control",
    len(offending_pairs(ast.parse(BAD))) == 1 and not offending_pairs(ast.parse(GOOD)),
    "injected `\"bytes\": len(body)` is caught; `body_chars`/`len(raw)` is not flagged",
)

print("=" * 74)
print(" ADR-015 unit verification — octets vs characters")
print("=" * 74)
for line in notes + failures:
    print(line)
print("-" * 74)
if failures:
    print(f" {len(failures)} FAILURE(S)")
    sys.exit(1)
print(f" all {len(notes)} check(s) passed")
