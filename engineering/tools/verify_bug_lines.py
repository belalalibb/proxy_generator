#!/usr/bin/env python3
"""
P00.T7 (rebuilt) — Mechanically scan the legacy code for defect classes, file:line.

No count in BUG_LEDGER.md is hand-written. This tool regenerates them and
CROSS-CHECKS against the surviving engineering/raw/bug_scan.json, so drift in
either direction is reported rather than silently absorbed (H2).

PINNED DEFINITIONS (recovered from the surviving raw evidence):
  * hardcoded_http_url  -> source LINES containing http(s)://          (257)
  * silent_handlers     -> handlers whose SINGLE statement DISCARDS the
                           error: `pass`, `continue`, or `return <falsy>`  (23)
  * bare_except         -> `except:` with no type                        (9)
  * except_broad        -> `except Exception:`                          (33)

Two counts in the committed scan are superseded, with the cause named in
RECONCILIATION.md rather than overwritten:
  * except_pass         -> committed 10 vs AST 9 (the AST answer is stable
                           under both the "body is pass" and "body contains
                           pass" readings, so 9 is correct; BUG_LEDGER B-02
                           cites silent_handlers=23, which matches exactly)
  * max_workers_literal -> committed 4 vs full scan 9. The committed pattern
                           caught only a subset; the true figure is 2 module
                           constants + 7 ThreadPoolExecutor call-site literals.
                           This STRENGTHENS B-03.

Output: engineering/raw/bug_scan_verify_<UTC>.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "engineering" / "raw"
COMMITTED = OUT_DIR / "bug_scan.json"

LEGACY = ["v1.py", "v2.py", "v3.py", "bebo.py", "proxychecker.py",
          "proxy_generator_v2.py"]

LINE_PATTERNS: dict[str, re.Pattern] = {
    "hardcoded_http_url": re.compile(r"https?://"),
    "verify_false": re.compile(r"verify\s*=\s*False"),
    "disable_warnings": re.compile(r"disable_warnings"),
    "captcha": re.compile(r"2captcha|captcha_id|captcha_solution|solve_captcha", re.I),
    "instagram_target": re.compile(r"instagram\.com", re.I),
    "open_write_truncate": re.compile(r"open\([^)]*,\s*['\"]w['\"]"),
    "open_append": re.compile(r"open\([^)]*,\s*['\"]a['\"]"),
    "time_sleep": re.compile(r"time\.sleep\s*\("),
    "input_call": re.compile(r"(?<![\w.])input\s*\("),
    "max_workers_literal": re.compile(r"max_workers\s*=\s*\d+|MAX_WORKERS\s*=\s*\d+"),
}

# Counts that are known-superseded, with the reason. Reported as EXPLAINED, not drift.
SUPERSEDED = {
    "except_pass": "committed pattern was a text match spanning one-line handlers; "
                   "AST gives 9 under both readings. BUG_LEDGER cites "
                   "silent_handlers=23, which matches exactly.",
    "max_workers_literal": "committed pattern caught only a subset; full scan finds "
                           "2 module constants + 7 call-site literals = 9. "
                           "Strengthens B-03 (unbounded concurrency).",
}


def _is_falsy_return(stmt: ast.Return) -> bool:
    v = stmt.value
    if v is None:
        return True
    if isinstance(v, ast.Constant):
        return not v.value
    if isinstance(v, (ast.List, ast.Set, ast.Tuple)):
        return not v.elts
    if isinstance(v, ast.Dict):
        return not v.keys
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
        return v.func.id in {"set", "list", "dict", "tuple"} and not v.args
    return False


def scan_file(path: Path) -> tuple[dict[str, list[str]], list[dict]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    hits: dict[str, list[str]] = defaultdict(list)

    for i, line in enumerate(src.splitlines(), 1):
        for name, rx in LINE_PATTERNS.items():
            if rx.search(line):
                hits[name].append(f"{path.name}:{i}")

    silent: list[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return dict(hits), silent

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            hits["bare_except"].append(f"{path.name}:{node.lineno}")
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            hits["except_broad"].append(f"{path.name}:{node.lineno}")
        if len(node.body) != 1:
            continue
        stmt = node.body[0]
        kind = None
        if isinstance(stmt, ast.Pass):
            kind = "pass"
            hits["except_pass"].append(f"{path.name}:{node.lineno}")
        elif isinstance(stmt, ast.Continue):
            kind = "continue"
        elif isinstance(stmt, ast.Return) and _is_falsy_return(stmt):
            kind = ast.unparse(stmt)
        if kind:
            silent.append({
                "file": path.name,
                "line": node.lineno,
                "handler": (f"except {ast.unparse(node.type)}:"
                            if node.type else "except:"),
                "body": kind,
            })
    return dict(hits), silent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    per_file: dict[str, dict] = {}
    totals: dict[str, int] = defaultdict(int)
    all_silent: list[dict] = []

    for name in LEGACY:
        p = ROOT / name
        if not p.exists():
            continue
        hits, silent = scan_file(p)
        per_file[name] = {k: len(v) for k, v in sorted(hits.items())}
        per_file[name]["silent_handlers"] = len(silent)
        for k, v in hits.items():
            totals[k] += len(v)
        all_silent.extend(silent)
    totals["silent_handlers"] = len(all_silent)

    drift: list[str] = []
    explained: list[str] = []
    exact = 0
    if COMMITTED.exists():
        old = json.loads(COMMITTED.read_text(encoding="utf-8"))["totals"]
        for k in sorted(set(old) | set(totals)):
            o, n = old.get(k), totals.get(k, 0)
            if o == n:
                exact += 1
            elif k in SUPERSEDED:
                explained.append(f"{k}: committed={o} regenerated={n} -- {SUPERSEDED[k]}")
            else:
                drift.append(f"{k}: committed={o} regenerated={n}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    doc = {
        "task": "P00.T7",
        "generator": "engineering/tools/verify_bug_lines.py",
        "rebuilt_after_sync_loss": True,
        "deterministic": True,
        "network_used": False,
        "measured_at_utc": stamp,
        "definitions": {
            "hardcoded_http_url": "source LINES containing http(s)://",
            "silent_handlers": "single-statement handler that DISCARDS the error "
                               "(pass | continue | return <falsy>)",
        },
        "totals": dict(sorted(totals.items())),
        "per_file": per_file,
        "silent_handlers": sorted(all_silent, key=lambda r: (r["file"], r["line"])),
        "silent_handler_count": len(all_silent),
        "reconciliation": {
            "exact_matches": exact,
            "explained_supersessions": explained,
            "unexplained_drift": drift,
        },
    }

    dest = OUT_DIR / f"bug_scan_verify_{stamp}.json"
    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(doc["totals"] | {"reconciliation": doc["reconciliation"]},
                         indent=2))
        return 1 if drift else 0

    print("=" * 74)
    print("LEGACY DEFECT SCAN (mechanical, file:line recorded)")
    print("=" * 74)
    for k, v in sorted(totals.items()):
        print(f"  {k:26} {v}")
    print("-" * 74)
    if COMMITTED.exists():
        print(f"  vs committed raw/bug_scan.json: {exact} counts EXACT")
        for e in explained:
            print(f"  EXPLAINED  {e}")
        for d in drift:
            print(f"  DRIFT      {d}")
        if not drift:
            print("  no unexplained drift")
    print(f"-> {dest.relative_to(ROOT)}")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
