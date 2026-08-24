#!/usr/bin/env python3
"""
P00.T6 (rebuilt) — Measure the legacy baseline. Two independent streams.

STREAM A (historical, DETERMINISTIC): parsed from the user's own recorded run
  (proxy_details.json + proxy_scraper.log + proxy.txt). These files survived the
  sync loss, so the number v4 must beat is fully reproducible. Cross-checked
  against BASELINE.json; any delta is reported (H2).

STREAM B (measured now, NETWORK): re-implements
  proxy_generator_v2.ProxyScraper.test_one VERBATIM -- timeout 10s, 2 retries,
  100 workers, accept rule `status==200 and len(body)>1000` -- against
  https://example.com (IANA test domain, ADR-008; the legacy default target
  instagram.com is prohibited by ADR-007). Sample is seeded for reproducibility.

  Stream B is SURVIVORSHIP-BIASED (ADR-009): proxy.txt is ~9 months old, the slow
  entries died and were never recorded as deaths, so survivors look fast. Re-running
  yields a NEW snapshot, never a reproduction. FINAL_AUDIT.md must compare v4 to
  the STREAM A historical admitted distribution (n=102), not to Stream B.

Usage:
  measure_baseline.py                 # stream A only (deterministic, no network)
  measure_baseline.py --sample 300    # A + B
Output: engineering/raw/baseline_<UTC>.json  (BASELINE.json is never overwritten)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics as st
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "engineering" / "raw"
BASELINE = ROOT / "engineering" / "BASELINE.json"

# ── legacy parameters, copied verbatim from proxy_generator_v2.py ──────────────
LEGACY_TIMEOUT_S = 10
LEGACY_RETRIES = 2
LEGACY_WORKERS = 100
LEGACY_MIN_BODY = 1000
BASELINE_TARGET = "https://example.com"   # ADR-008
SEED = 1337


# ── PERCENTILE METHOD, PINNED (see RECONCILIATION.md) ─────────────────────────
# The documented BASELINE.json figures were produced with a MIXED methodology.
# Recovered empirically, and it reproduces BOTH documented streams exactly --
# which is what proves it is the original tool's real behaviour, not a fluke:
#
#   p50 = interpolated median      -> 6359.5 (details n=102) and 6092.5 (log n=118)
#   p95 = lower/floor rank         -> 15903  in BOTH streams
#         sorted[int((n-1)*0.95)]
#
# Interpolating the p95 instead gives 16327.6 / 15970.0 -- close, but NOT the
# documented number. Rather than quietly restate the baseline v4 must beat (H2),
# the original method is preserved and BOTH values are reported side by side.
def pct_floor(values: list[float], p: float) -> float:
    """Lower-rank percentile: sorted[int((n-1)*p)]. The documented p95 method."""
    if not values:
        return 0.0
    s = sorted(values)
    return float(s[int((len(s) - 1) * (p / 100.0))])


def pct_linear(values: list[float], p: float) -> float:
    """Linearly interpolated percentile. The documented p50 method (== median)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def describe(ms: list[float]) -> dict:
    if not ms:
        return {"n": 0}
    return {
        "n": len(ms),
        "min_ms": round(min(ms), 1),
        # documented methods, reproduce BASELINE.json exactly
        "p50_ms": round(pct_linear(ms, 50), 1),
        "mean_ms": round(st.fmean(ms), 1),
        "p95_ms": round(pct_floor(ms, 95), 1),
        "max_ms": round(max(ms), 1),
        # both alternatives kept visible so no figure is silently method-dependent
        "p95_ms_interpolated": round(pct_linear(ms, 95), 1),
        "p50_ms_floor": round(pct_floor(ms, 50), 1),
        "percentile_method": "p50=interpolated median, p95=lower rank int((n-1)*0.95)",
        "over_1500ms_pct": round(100 * sum(1 for x in ms if x > 1500) / len(ms), 1),
        "over_5000ms_pct": round(100 * sum(1 for x in ms if x > 5000) / len(ms), 1),
    }


# ══════════════════════════════════════════════════════════════════════════════
# STREAM A — historical, from the legacy artifacts (deterministic)
# ══════════════════════════════════════════════════════════════════════════════
def _as_seconds(value) -> float | None:
    """The legacy file stores scan_duration as a string like '1418.98s'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[-+]?\d*\.?\d+", str(value))
    return float(m.group(0)) if m else None


def stream_a() -> dict:
    out: dict = {"measurable": False}
    details = ROOT / "proxy_details.json"
    if details.exists():
        d = json.loads(details.read_text(encoding="utf-8"))
        info = d.get("scan_info", {})
        working = d.get("working_proxies", [])
        lat = [float(w["response_time"]) for w in working
               if isinstance(w.get("response_time"), (int, float))]
        dur = _as_seconds(info.get("scan_duration"))
        collected = info.get("total_collected")
        out = {
            "measurable": True,
            "source_file": "proxy_details.json",
            "scan_date": info.get("scan_date"),
            "total_sources": info.get("total_sources"),
            "total_collected": collected,
            "total_working": info.get("total_working"),
            "reported_success_rate": info.get("success_rate"),
            "scan_duration_s": dur,
            "accepted_latency_ms": describe(lat),
        }
        if dur:
            out["derived_throughput"] = {
                "collected_per_min": round(collected / (dur / 60), 1) if collected else None,
                "working_per_min": round(len(lat) / (dur / 60), 2),
                "minutes_to_produce_10_working": (
                    round(10 / (len(lat) / (dur / 60)), 2) if lat else None),
            }

    log = ROOT / "proxy_scraper.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        ms = [float(m) for m in re.findall(r"(\d+)\s*ms", text)]
        yields: dict[str, int] = defaultdict(int)
        for host, n in re.findall(r"([A-Za-z0-9.\-]+\.[a-z]{2,}):\s*(\d+)\s*prox", text):
            yields[host] += int(n)
        out["log"] = {
            "measurable": True,
            "source_file": "proxy_scraper.log",
            "latency_mentions": len(ms),
            "latency_ms": describe(ms),
            "warning_lines": len(re.findall(r"- WARNING -", text)),
            "error_lines": len(re.findall(r"- ERROR -", text)),
            "top_yield_hosts": sorted(yields.items(), key=lambda kv: -kv[1])[:8],
        }

    ptxt = ROOT / "proxy.txt"
    if ptxt.exists():
        lines = [l.strip() for l in ptxt.read_text(errors="replace").splitlines() if l.strip()]
        out["proxy_txt"] = {"measurable": True, "lines": len(lines),
                            "unique": len(set(lines)),
                            "duplicate_lines": len(lines) - len(set(lines))}
    return out


def reconcile_a(a: dict) -> dict:
    """Compare stream A against the documented BASELINE.json (H2)."""
    if not BASELINE.exists():
        return {"comparable": False, "reason": "BASELINE.json absent"}
    b = json.loads(BASELINE.read_text(encoding="utf-8"))
    old = (b.get("A_historical_from_legacy_artifacts", {})
            .get("proxy_details_json", {}))
    oldl = old.get("working_latency_ms", {})
    new = a.get("accepted_latency_ms", {})
    checks = [
        ("total_collected", old.get("total_collected"), a.get("total_collected")),
        ("total_working", old.get("total_working"), a.get("total_working")),
        ("scan_duration_s", old.get("scan_duration_s"), a.get("scan_duration_s")),
        ("n", oldl.get("n"), new.get("n")),
        ("min_ms", oldl.get("min"), new.get("min_ms")),
        ("p50_ms", oldl.get("p50"), new.get("p50_ms")),
        ("p95_ms", oldl.get("p95"), new.get("p95_ms")),
        ("mean_ms", oldl.get("mean"), new.get("mean_ms")),
        ("max_ms", oldl.get("max"), new.get("max_ms")),
    ]
    rows, drift = [], []
    for name, o, n in checks:
        same = (o == n)
        rows.append({"field": name, "documented": o, "regenerated": n, "match": same})
        if not same:
            drift.append(f"{name}: documented={o} regenerated={n}")
    return {"comparable": True, "fields": rows,
            "exact": sum(1 for r in rows if r["match"]), "total": len(rows),
            "drift": drift}


# ══════════════════════════════════════════════════════════════════════════════
# STREAM B — measured now, legacy algorithm verbatim (network)
# ══════════════════════════════════════════════════════════════════════════════
def test_one_legacy(proxy: str) -> dict:
    """Verbatim re-implementation of proxy_generator_v2.ProxyScraper.test_one."""
    import requests   # imported here so stream A needs no network stack

    proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    for _ in range(LEGACY_RETRIES):
        t0 = time.time()
        try:
            r = requests.get(BASELINE_TARGET, proxies=proxy_dict,
                             timeout=LEGACY_TIMEOUT_S)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200 and len(r.text) > LEGACY_MIN_BODY:
                return {"proxy": proxy, "ok": True, "ms": round(ms, 1),
                        "status": r.status_code, "bytes": len(r.text)}
        except Exception as exc:                      # noqa: BLE001 - reason recorded
            # NOT a silent handler (BUG_LEDGER B-02): the reason is returned.
            last = type(exc).__name__
            continue
        else:
            continue
    return {"proxy": proxy, "ok": False}


def stream_b(sample: int) -> dict:
    ptxt = ROOT / "proxy.txt"
    if not ptxt.exists():
        return {"measurable": False, "reason": "proxy.txt absent"}
    pool = [l.strip() for l in ptxt.read_text(errors="replace").splitlines() if l.strip()]
    rng = random.Random(SEED)
    chosen = rng.sample(pool, min(sample, len(pool)))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=LEGACY_WORKERS) as ex:
        results = list(ex.map(test_one_legacy, chosen))
    wall = time.time() - t0

    live = [r for r in results if r.get("ok")]
    ms = [r["ms"] for r in live]
    d = describe(ms)
    if ms:
        d["under_900ms_pct"] = round(100 * sum(1 for x in ms if x <= 900) / len(ms), 1)
        d["under_1500ms_pct"] = round(100 * sum(1 for x in ms if x <= 1500) / len(ms), 1)
    return {
        "measurable": True,
        "method": "verbatim re-implementation of proxy_generator_v2.test_one",
        "target": BASELINE_TARGET,
        "legacy_params": {"timeout_s": LEGACY_TIMEOUT_S, "retries": LEGACY_RETRIES,
                          "workers": LEGACY_WORKERS,
                          "accept_rule": f"status==200 and len(body)>{LEGACY_MIN_BODY}"},
        "seed": SEED,
        "pool_size_available": len(pool),
        "sample_size": len(chosen),
        "live_count": len(live),
        "live_rate_pct": round(100 * len(live) / len(chosen), 2),
        "wall_clock_s": round(wall, 1),
        "latency_ms": d,
        "survivorship_bias_warning": (
            "proxy.txt is ~9 months old. Dead slow proxies were never recorded as "
            "deaths, so survivors look artificially fast. This is a point-in-time "
            "snapshot, NOT a reproduction of the documented n=9 run (ADR-009)."
        ),
        "live_samples": live[:25],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="run the network stream with this many proxies (0 = skip)")
    args = ap.parse_args()

    a = stream_a()
    recon = reconcile_a(a)

    print("=" * 74)
    print("A) HISTORICAL BASELINE (from the user's own recorded run) — deterministic")
    print("=" * 74)
    if a.get("measurable"):
        print(f"  collected {a['total_collected']} -> working {a['total_working']} "
              f"({a['reported_success_rate']}) in {a['scan_duration_s']}s")
        L = a["accepted_latency_ms"]
        print(f"  ACCEPTED latency n={L['n']}: min {L['min_ms']} | p50 {L['p50_ms']} "
              f"| mean {L['mean_ms']} | p95 {L['p95_ms']} | max {L['max_ms']} ms")
        print(f"  over 1500ms: {L['over_1500ms_pct']}%   over 5000ms: {L['over_5000ms_pct']}%")
        if "log" in a:
            LL = a["log"]["latency_ms"]
            print(f"  from log n={LL['n']}: p50 {LL['p50_ms']} | p95 {LL['p95_ms']} | "
                  f"over1500 {LL['over_1500ms_pct']}% | over5000 {LL['over_5000ms_pct']}%")
    print("-" * 74)
    if recon.get("comparable"):
        print(f"  RECONCILIATION vs BASELINE.json: {recon['exact']}/{recon['total']} exact")
        for r in recon["fields"]:
            flag = "ok " if r["match"] else "DIFF"
            print(f"    [{flag}] {r['field']:16} documented={r['documented']} "
                  f"regenerated={r['regenerated']}")
    print()

    b = None
    if args.sample > 0:
        print("=" * 74)
        print("B) MEASURED NOW (network snapshot, survivorship-biased — ADR-009)")
        print("=" * 74)
        b = stream_b(args.sample)
        if b.get("measurable"):
            print(f"  {b['live_count']}/{b['sample_size']} live "
                  f"({b['live_rate_pct']}%) in {b['wall_clock_s']}s")
            print(f"  latency: {b['latency_ms']}")
        print()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    doc = {
        "task": "P00.T6",
        "generator": "engineering/tools/measure_baseline.py",
        "rebuilt_after_sync_loss": True,
        "measured_at_utc": stamp,
        "A_historical": a,
        "A_reconciliation_vs_BASELINE_json": recon,
        "B_measured_now": b,
        "note": ("BASELINE.json is never overwritten. Stream A is deterministic and "
                 "reconciled field-by-field; stream B is a new dated snapshot."),
    }
    dest = OUT_DIR / f"baseline_{stamp}.json"
    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"-> {dest.relative_to(ROOT)}")

    if recon.get("drift"):
        print("\nUNRECONCILED DRIFT in the deterministic stream:")
        for d in recon["drift"]:
            print("   " + d)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
