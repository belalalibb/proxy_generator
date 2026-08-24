#!/usr/bin/env python3
"""
Re-derive BOTH legacy baseline streams and emit the correct per-stream figures.

WHY THIS TOOL EXISTS (ADR-020)

The legacy tree left two independent records of the same run:

    STREAM A  proxy_details.json   n=102   structured JSON, one record per proxy
    STREAM B  proxy_scraper.log    n=118   text log lines

They disagree on n because the log recorded 16 "Working" lines that never made it
into the JSON. BASELINE.json stores both correctly and separately.

The defect was in the PROSE. `over_1500ms_pct: 95.8` and `over_5000ms_pct: 56.8`
exist ONLY under the n=118 log stream, yet six files quoted them in the same
sentence as the n=102 p50/p95 -- producing composite claims like:

    "p50 6 359.5 ms and p95 15 903 ms (n=102), where 95.8% exceeded 1 500 ms"

Every individual number there is real. The sentence is still false: no single
distribution has those properties. The n=102 stream's true figures are 95.1% and
58.8%.

Nothing caught it because the gate verified that each number EXISTS in an
artifact, never that the numbers quoted together came from the SAME artifact.
That is the ADR-018 lesson recurring at a finer grain: an anchored claim can
still be a spliced one.

Offline, deterministic, asserted. Writes engineering/raw/baseline_streams_<UTC>.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.core.policy.percentile import pct_floor, pct_linear  # noqa: E402


def describe(ms: list[float], label: str, provenance: str) -> dict:
    n = len(ms)
    return {
        "stream": label,
        "provenance": provenance,
        "n": n,
        "min_ms": round(min(ms), 1),
        "p50_ms": round(pct_linear(ms, 50), 1),
        "p95_ms": round(pct_floor(ms, 95), 1),
        "mean_ms": round(sum(ms) / n, 1),
        "max_ms": round(max(ms), 1),
        "over_1500ms_pct": round(100 * sum(1 for x in ms if x > 1500) / n, 1),
        "over_5000ms_pct": round(100 * sum(1 for x in ms if x > 5000) / n, 1),
        "percentile_method": "ADR-011: p50 interpolated, p95 floor rank int((n-1)*0.95)",
    }


def stream_a() -> dict:
    """proxy_details.json — the structured record, n=102."""
    p = ROOT / "proxy_details.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    ms = [float(r["response_time"]) for r in doc.get("working_proxies", [])
          if r.get("working") and isinstance(r.get("response_time"), (int, float))]
    if not ms:
        raise SystemExit("stream A yielded no latencies")
    return describe(ms, "A_proxy_details_json", "proxy_details.json::working_proxies")


def stream_b() -> dict:
    """
    proxy_scraper.log — the text log, n=118.

    The log carries 16 more "Working" lines than the JSON. Which record is
    'right' is not decidable from here, so BOTH are kept and each figure is
    reported against its own n. That is the whole point of this tool.
    """
    p = ROOT / "proxy_scraper.log"
    txt = p.read_text(errors="replace")
    ms = [float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*ms", txt)]
    if not ms:
        raise SystemExit("stream B yielded no latencies")
    return describe(ms, "B_proxy_scraper_log", "proxy_scraper.log (regex '<n> ms')")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    a, b = stream_a(), stream_b()

    # The assertions that make this evidence rather than a report.
    assert a["n"] == 102, f"stream A n changed: {a['n']}"
    assert b["n"] == 118, f"stream B n changed: {b['n']}"
    assert b["over_1500ms_pct"] == 95.8, b["over_1500ms_pct"]
    assert b["over_5000ms_pct"] == 56.8, b["over_5000ms_pct"]
    assert a["over_1500ms_pct"] == 95.1, a["over_1500ms_pct"]
    assert a["over_5000ms_pct"] == 58.8, a["over_5000ms_pct"]
    assert a["p50_ms"] == 6359.5 and a["p95_ms"] == 15903.0, a
    assert b["p50_ms"] == 6092.5 and b["p95_ms"] == 15903.0, b

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "prove which figure belongs to which stream (ADR-020)",
        "finding": (
            "over_1500ms_pct=95.8 and over_5000ms_pct=56.8 belong to STREAM B "
            "(n=118). Six files quoted them beside STREAM A's p50/p95 (n=102). "
            "Stream A's true figures are 95.1% and 58.8%."
        ),
        "streams": [a, b],
        "shared": {
            "p95_ms": a["p95_ms"],
            "max_ms": a["max_ms"],
            "min_ms": a["min_ms"],
            "note": "p95, max and min coincide across streams; p50, mean and "
                    "both over_* percentages do NOT. Coincidence on some fields "
                    "is exactly why the splice was easy to miss.",
        },
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = ROOT / "engineering" / "raw" / f"baseline_streams_{stamp}.json"
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    canonical = ROOT / "engineering" / "raw" / "baseline_streams.json"
    canonical.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    if not args.quiet:
        print("=" * 78)
        print("BASELINE STREAMS — which figure belongs to which n")
        print("=" * 78)
        hdr = f"{'field':>18} | {'A (n=102) json':>16} | {'B (n=118) log':>16} | same?"
        print(hdr)
        print("-" * len(hdr))
        for k in ("n", "min_ms", "p50_ms", "mean_ms", "p95_ms", "max_ms",
                  "over_1500ms_pct", "over_5000ms_pct"):
            same = "YES" if a[k] == b[k] else "NO"
            print(f"{k:>18} | {a[k]:>16} | {b[k]:>16} | {same}")
        print("-" * len(hdr))
        print("  95.8 / 56.8 are STREAM B. Stream A is 95.1 / 58.8.")
        print(f"  written: {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
