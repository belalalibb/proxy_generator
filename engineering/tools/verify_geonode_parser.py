#!/usr/bin/env python3
"""
P00.T5 EVIDENCE TOOL — rebuilt 2026-08-24 after a second sync loss.

WHAT THIS PROVES, AND WHY IT MATTERS

The GeoNode JSON API has now been misclassified as an empty/dead source THREE
times, from THREE DIFFERENT CAUSES:

  1. 2026-08-23  a naive adjacency regex found 0 candidates in valid JSON
                 (ip and port live in separate keys) -> "EMPTY"
  2. 2026-08-23  a 659-byte throttled body from our own per-host hammering
                 -> "TRULY_EMPTY"                                    (ADR-006)
  3. 2026-08-24  `resp.content.read(n)` returned only the BUFFERED prefix
                 (74 241 of 230 067 bytes), so the JSON would not parse
                 -> "TRULY_EMPTY"                                    (ADR-013)

Each time, the source was actually serving 500 usable proxies.

This tool exists to make that impossible to repeat silently. It runs the SAME
parser used by the live probe against the STORED body
(`engineering/raw/geonode_body.txt`, 230 019 bytes) and asserts it yields exactly
500 unique proxies.

ADR-013(e): a parser must be validated against stored evidence BEFORE it is
trusted live. If this passes and a live fetch returns zero, the fault is in the
FETCH, not the source -- which is precisely the inference that stopped me writing
"GeoNode is dead" into the record.

Network: NONE. Deterministic and offline by design, so it can run in every gate.

Exit codes: 0 = parser verified · 1 = parser regressed or evidence missing
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "engineering" / "raw" / "geonode_body.txt"
PROBE_TOOL = ROOT / "engineering" / "tools" / "probe_legacy_sources.py"
OUT_DIR = ROOT / "engineering" / "raw"

# The documented P00.T5 figure. Pinned deliberately: if a parser change alters
# this, the change must be justified rather than silently accepted.
EXPECTED_UNIQUE = 500
EXPECTED_BYTES = 230019


def load_probe_module():
    """Import the live probe tool so the SAME parser is exercised (no copy)."""
    spec = importlib.util.spec_from_file_location("probe_legacy_sources", PROBE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PROBE_TOOL}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not EVIDENCE.exists():
        print(f"FAIL: stored evidence missing: {EVIDENCE}")
        return 1
    if not PROBE_TOOL.exists():
        print(f"FAIL: probe tool missing: {PROBE_TOOL}")
        return 1

    body = EVIDENCE.read_text(encoding="utf-8", errors="replace")
    mod = load_probe_module()

    by_parser = {
        "json_path": len(mod.parse_json_walk(body)),
        "regex_adjacent": len(mod.parse_adjacent(body)),
        "html_table": len(mod.parse_html_table(body)),
    }

    # classify with honest metadata: this is a COMPLETE read of stored bytes
    verdict, detail = mod.classify(200, body, None, {
        "body_bytes": len(body),
        "content_length": len(body),
        "content_type": "application/json",
        "short_read": False,
        "body_truncated_at_cap": False,
    })

    checks = {
        "evidence_bytes_match": len(body) == EXPECTED_BYTES,
        "json_parser_yields_expected": by_parser["json_path"] == EXPECTED_UNIQUE,
        "regex_would_have_missed_it": by_parser["regex_adjacent"] == 0,
        "verdict_is_alive_json": verdict == "ALIVE_JSON",
    }
    ok = all(checks.values())

    report = {
        "task": "P00.T5",
        "generator": "engineering/tools/verify_geonode_parser.py",
        "rebuilt_after_sync_loss": True,
        "network_used": False,
        "evidence": str(EVIDENCE.relative_to(ROOT)),
        "evidence_bytes": len(body),
        "expected_bytes": EXPECTED_BYTES,
        "expected_unique": EXPECTED_UNIQUE,
        "by_parser": by_parser,
        "verdict": verdict,
        "unique_candidates": detail.get("unique_candidates"),
        "checks": checks,
        "passed": ok,
        "why_this_exists": (
            "The same source was filed empty three times from three different "
            "causes (regex adjacency, throttled body, truncated read). Validating "
            "the parser on stored bytes means a live zero implicates the FETCH, "
            "not the source (ADR-013(e))."
        ),
    }

    dest = OUT_DIR / "geonode_parser_verify.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if ok else 1

    print("=" * 74)
    print("P00.T5 — GeoNode parser verification against STORED evidence (offline)")
    print("=" * 74)
    print(f"  evidence            : {EVIDENCE.relative_to(ROOT)}")
    print(f"  bytes               : {len(body)} (expected {EXPECTED_BYTES})")
    for name, n in by_parser.items():
        print(f"  {name:<20}: {n}")
    print(f"  verdict             : {verdict}")
    print("-" * 74)
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("-" * 74)
    print(f"-> {dest.relative_to(ROOT)}")
    if not ok:
        print("  PARSER REGRESSED — do not trust any live source verdict.")
        return 1
    print("  parser verified: a live zero from this source implicates the FETCH.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
