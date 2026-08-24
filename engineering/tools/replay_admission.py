#!/usr/bin/env python3
"""
REPLAY the legacy-admitted proxies through the v4 admission gate.

This answers one question with real data rather than argument:

    Of the 102 proxies the legacy system ACCEPTED and wrote to disk,
    how many would the v4 gate have admitted?

Input is `proxy_details.json` -- the legacy system's own output, 102 records it
declared `working: true`, each with the single `response_time` it measured.

────────────────────────────────────────────────────────────────────────────────
WHAT THIS DOES **NOT** PROVE  (read before quoting any number below)

The legacy file contains ONE sample per proxy. So this replay runs the gate at
k=1, which means it tests THE THRESHOLD ONLY. It cannot test:

  * the k=5 sampling rule (ADR-003) -- there is one sample, not five
  * `min_success_ratio`  -- with one attempt, the ratio is 1.0 or the record
                            would not be in the file
  * `max_jitter`         -- stdev is undefined at n=1, so jitter is None and
                            the check is skipped by design

A k=1 replay is therefore GENEROUS to the legacy data: it is the most flattering
reading available, since a single sample cannot expose unreliability or jitter.
Every proxy this still rejects is rejected on latency alone.

That generosity is the point. If the gate rejected most of them only because the
replay penalised missing samples, the comparison would be rigged.
────────────────────────────────────────────────────────────────────────────────

Writes engineering/raw/admission_replay_<UTC>.json and prints a summary.
Deterministic and offline: same input, same output, no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.core.policy.admission import (  # noqa: E402
    AdmissionPolicy, build_profile, decide,
)
from atlas.core.policy.percentile import pct_floor, pct_linear  # noqa: E402


def load_legacy_admitted() -> list[dict]:
    p = ROOT / "proxy_details.json"
    if not p.exists():
        raise SystemExit(f"legacy artifact missing: {p}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    rows = doc.get("working_proxies", [])
    # Only rows the legacy system itself called working, with a usable latency.
    return [r for r in rows
            if r.get("working") and isinstance(r.get("response_time"), (int, float))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-p95-ms", type=float, default=1500.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows = load_legacy_admitted()
    if not rows:
        raise SystemExit("no legacy admitted rows found")

    policy = AdmissionPolicy(max_p95_ms=args.max_p95_ms)
    latencies = [float(r["response_time"]) for r in rows]

    reasons: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    admitted_ms: list[float] = []
    per_proxy: list[dict] = []

    for r in rows:
        ms = float(r["response_time"])
        # k=1: the only evidence the legacy file carries. See the caveat above.
        profile = build_profile((ms,), attempted=1)
        v = decide(profile, policy)
        reasons[v.reason.value] += 1
        grades[v.grade.value] += 1
        if v.admitted:
            admitted_ms.append(ms)
        per_proxy.append({
            "proxy": r.get("proxy"),
            "legacy_response_time_ms": ms,
            "v4_admitted": v.admitted,
            "v4_grade": v.grade.value,
            "v4_reason": v.reason.value,
        })

    n = len(rows)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": "proxy_details.json::working_proxies",
        "question": "how many legacy-ADMITTED proxies would the v4 gate admit?",
        "method": {
            "k_used": 1,
            "why": "the legacy file records ONE sample per proxy",
            "tests_only": "the p95 latency THRESHOLD",
            "cannot_test": ["k=5 sampling (ADR-003)",
                            "min_success_ratio (one attempt)",
                            "max_jitter (stdev undefined at n=1)"],
            "bias": "GENEROUS to the legacy data: a single sample cannot expose "
                    "unreliability or jitter, so this is the most flattering "
                    "reading available",
            "percentile": "p95 by ADR-011 floor rank, identical to "
                          "measure_baseline.py, so the figures are comparable",
        },
        "policy": {
            "max_p95_ms": policy.max_p95_ms,
            "elite_p95_ms": policy.elite_p95_ms,
            "good_p95_ms": policy.good_p95_ms,
            "usable_p95_ms": policy.usable_p95_ms,
        },
        "legacy_admitted_n": n,
        "legacy_distribution": {
            "p50_ms": round(pct_linear(latencies, 50), 1),
            "p95_ms": round(pct_floor(latencies, 95), 1),
            "max_ms": round(max(latencies), 1),
            "min_ms": round(min(latencies), 1),
        },
        "v4_would_admit": len(admitted_ms),
        "v4_would_reject": n - len(admitted_ms),
        "v4_reject_pct": round(100 * (n - len(admitted_ms)) / n, 1),
        "v4_admitted_distribution": (
            {
                "n": len(admitted_ms),
                "p50_ms": round(pct_linear(admitted_ms, 50), 1),
                "p95_ms": round(pct_floor(admitted_ms, 95), 1),
                "max_ms": round(max(admitted_ms), 1),
            } if admitted_ms else {"n": 0}
        ),
        "by_reason": dict(reasons),
        "by_grade": dict(grades),
        "per_proxy": per_proxy,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = ROOT / "engineering" / "raw" / f"admission_replay_{stamp}.json"
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    if not args.quiet:
        print("=" * 74)
        print("ADMISSION REPLAY — legacy-admitted proxies vs the v4 gate")
        print("=" * 74)
        print(f"  legacy admitted        : {n}")
        print(f"  legacy p50 / p95       : "
              f"{out['legacy_distribution']['p50_ms']} / "
              f"{out['legacy_distribution']['p95_ms']} ms")
        print(f"  v4 would ADMIT         : {out['v4_would_admit']}")
        print(f"  v4 would REJECT        : {out['v4_would_reject']} "
              f"({out['v4_reject_pct']}%)")
        if admitted_ms:
            print(f"  survivor p50 / p95     : "
                  f"{out['v4_admitted_distribution']['p50_ms']} / "
                  f"{out['v4_admitted_distribution']['p95_ms']} ms")
        print(f"  reasons                : {dict(reasons)}")
        print(f"  grades                 : {dict(grades)}")
        print("-" * 74)
        print("  CAVEAT: k=1 replay. Tests the THRESHOLD only, and is GENEROUS")
        print("  to the legacy data (jitter/reliability unmeasurable at n=1).")
        print(f"  written: {dest.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
