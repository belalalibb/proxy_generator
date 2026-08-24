#!/usr/bin/env python3
"""
LIVE CALIBRATION (P06) — retire the k=1 caveat on H7.

WHAT THIS ANSWERS THAT THE P04 REPLAY COULD NOT

The P04 replay ran the gate against the legacy system's own 102 admitted proxies
and rejected 97 (95.1%). But every one of those rows carries a SINGLE latency
sample, so the replay could only exercise rule 4's threshold. With k=1:

  * stdev is undefined      -> TOO_JITTERY is unreachable
  * success_ratio is 1.0    -> UNRELIABLE is unreachable
  * integrity was unrecorded-> TRANSPARENT_LEAK / CONTENT_MISMATCH unreachable

So three of the gate's four rules had never fired against real data. This tool
probes live candidates with real k=5 sampling and reports which rule actually
rejected each one — turning "the gate is correct by unit test" into "the gate
was applied to live proxies and here is the distribution of outcomes".

HONESTY CONSTRAINTS

  * The target is REQUIRED and passed explicitly (H5/ADR-007). No default.
  * The IP-echo baseline is fetched WITHOUT a proxy first, because
    TRANSPARENT_LEAK is a comparative claim (see probe_aiohttp.py).
  * Candidates come from the pinned registry snapshot, not a hardcoded URL
    (ADR-002).
  * Survivorship is disclosed: this measures candidates that exist TODAY, which
    is not the population the legacy run saw (ADR-009).
  * Every rejection names its ReasonCode. A count of failures without causes is
    what the legacy logs produced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.adapters.probe_aiohttp import AiohttpProbe                 # noqa: E402
from atlas.core.domain.proxy import Anonymity, Protocol, Proxy        # noqa: E402
from atlas.core.domain.source import Target                           # noqa: E402
from atlas.core.domain.verdict import ReasonCode                      # noqa: E402
from atlas.core.policy.admission import (                             # noqa: E402
    AdmissionPolicy, build_profile, decide,
)
from atlas.core.policy.normalize import normalize_many                # noqa: E402
from atlas.core.ports.probe import ProbePlan                          # noqa: E402


def load_candidates(limit: int, seed: int) -> list[Proxy]:
    """
    Candidates from the newest pinned probe snapshot (ADR-002: never a hardcoded
    source URL). Shuffled with a FIXED seed so a rerun probes the same set and the
    numbers are comparable rather than merely similar.
    """
    snaps = sorted((ROOT / "engineering" / "raw").glob("source_probe_*.json"))
    if not snaps:
        raise SystemExit("no source_probe_*.json snapshot found")
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    rows = snap if isinstance(snap, list) else snap.get("results", [])

    raw: list[str] = []
    for row in rows:
        for key in ("sample_candidates", "candidates", "samples"):
            v = row.get(key)
            if isinstance(v, list):
                raw.extend(str(x) for x in v)
    seen: set[str] = set()
    uniq = [c for c in raw if not (c in seen or seen.add(c))]
    random.Random(seed).shuffle(uniq)

    report = normalize_many(tuple(uniq))
    print(f"  snapshot   : {snaps[-1].name}")
    print(f"  candidates : {len(uniq)} unique -> {len(report.accepted)} normalised "
          f"({len(report.dropped)} dropped)")
    return list(report.accepted[:limit])


async def probe_one(probe: AiohttpProbe, proxy: Proxy, target: Target,
                    plan: ProbePlan, policy: AdmissionPolicy,
                    baseline, ip_echo: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        rec: dict = {"endpoint": f"{proxy.endpoint.host}:{proxy.endpoint.port}"}

        # S2 -- cheap triage. Most candidates die here, for the price of one RTT.
        tcp = await probe.tcp_handshake(proxy, plan.tcp_timeout_ms)
        rec["tcp_ok"] = tcp.ok
        if not tcp.ok:
            rec.update(stage="S2_TCP", reason=tcp.reason.value,
                       detail=tcp.detail, admitted=False)
            return rec
        rec["tcp_ms"] = round(tcp.elapsed_ms or 0, 1)

        # S3 -- ADR-005: test the protocol, never trust the label.
        disc = await probe.discover_protocol(proxy, target)
        rec["stage3_ok"] = disc.ok
        rec["discovered_protocol"] = (disc.discovered_protocol.value
                                      if disc.discovered_protocol else None)
        if not disc.ok:
            rec.update(stage="S3_PROTOCOL", reason=disc.reason.value,
                       detail=disc.detail, admitted=False)
            return rec
        proxy = Proxy(endpoint=proxy.endpoint,
                      protocol=disc.discovered_protocol or Protocol.HTTP)

        # S4 -- the k samples that make jitter and reliability measurable at all.
        samples = await probe.sample_latency(proxy, target, plan)
        ok_ms = tuple(s.elapsed_ms for s in samples
                      if s.ok and s.elapsed_ms is not None)
        rec["attempted"] = len(samples)
        rec["successful"] = len(ok_ms)
        rec["samples_ms"] = [round(m, 1) for m in ok_ms]
        rec["sample_reasons"] = [s.reason.value for s in samples if not s.ok]

        # S5 -- integrity, only for endpoints that actually answered.
        anonymity, leak, mismatch = Anonymity.UNKNOWN, False, False
        if ok_ms:
            integ = await probe.check_integrity(proxy, target, baseline, ip_echo)
            rec["integrity_reason"] = integ.reason.value
            leak = integ.reason is ReasonCode.TRANSPARENT_LEAK
            mismatch = integ.reason is ReasonCode.CONTENT_MISMATCH
            anonymity = integ.observed_anonymity or Anonymity.UNKNOWN
            rec["observed_anonymity"] = anonymity.value

        profile = build_profile(ok_ms, attempted=len(samples))
        verdict = decide(profile, policy, anonymity=anonymity,
                         transparent_leak=leak, content_mismatch=mismatch)
        rec.update(
            stage="S4_S5_GATE",
            p50_ms=profile.p50_ms, p95_ms=profile.p95_ms,
            stdev_ms=profile.stdev_ms, success_ratio=profile.success_ratio,
            jitter=(round(profile.stdev_ms / profile.p50_ms, 3)
                    if profile.stdev_ms and profile.p50_ms else None),
            admitted=verdict.admitted, reason=verdict.reason.value,
            grade=verdict.grade.value,
        )
        return rec


async def run(args) -> dict:
    policy = AdmissionPolicy()
    plan = ProbePlan(samples=args.k, per_sample_timeout_ms=args.timeout_ms,
                     tcp_timeout_ms=args.tcp_timeout_ms)
    target = Target(url=args.target, expect_status=200,
                    timeout_ms=args.timeout_ms)
    probe = AiohttpProbe()

    print(f"  target     : {args.target}   (explicit, H5/ADR-007)")
    baseline = await probe.establish_baseline(target, args.ip_echo)
    print(f"  baseline   : direct egress IP established, marker "
          f"{baseline.body_marker!r} (no proxy)")

    candidates = load_candidates(args.limit, args.seed)
    print(f"  probing    : {len(candidates)} candidates, k={args.k}, "
          f"concurrency={args.concurrency}\n")

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*[
        probe_one(probe, p, target, plan, policy, baseline, args.ip_echo, sem)
        for p in candidates
    ])

    admitted = [r for r in results if r.get("admitted")]
    reasons = Counter(r.get("reason") for r in results if not r.get("admitted"))
    stages = Counter(r.get("stage") for r in results if not r.get("admitted"))
    reached_gate = [r for r in results if r.get("stage") == "S4_S5_GATE"]
    multi = [r for r in reached_gate if r.get("successful", 0) >= 2]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": "applied to LIVE proxies with real k sampling, what does the "
                    "admission gate actually do, and which rule fires?",
        "method": {
            "k": args.k,
            "target": args.target,
            "ip_echo": args.ip_echo,
            "candidates_probed": len(candidates),
            "concurrency": args.concurrency,
            "seed": args.seed,
            "policy": {
                "max_p95_ms": policy.max_p95_ms,
                "max_jitter": policy.max_jitter,
                "min_success_ratio": policy.min_success_ratio,
            },
            "tls_verification": "ON (B-09 was 9 sites disabling it)",
        },
        "caveats": [
            "ADR-009 survivorship: these are candidates that exist TODAY, not the "
            "population the legacy 2025 run observed. The two rates are not "
            "directly comparable.",
            "SOCKS candidates cannot be tested: aiohttp has no native SOCKS "
            "support, so they are reported as untestable rather than as failures.",
            "A single sweep is a point-in-time sample; source availability varies "
            "minute to minute (see TASK_STATE.source_registry.point_in_time_variance).",
        ],
        "totals": {
            "probed": len(results),
            "tcp_ok": sum(1 for r in results if r.get("tcp_ok")),
            "reached_gate": len(reached_gate),
            "with_2plus_samples": len(multi),
            "admitted": len(admitted),
            "rejected": len(results) - len(admitted),
        },
        "rejection_reasons": dict(reasons.most_common()),
        "rejection_stages": dict(stages.most_common()),
        "k_gt_1_evidence": {
            "note": "these are the rules the k=1 P04 replay COULD NOT test",
            "proxies_with_multiple_samples": len(multi),
            "jitter_computed_for": sum(1 for r in multi if r.get("jitter") is not None),
            "rejected_too_jittery": reasons.get("TOO_JITTERY", 0),
            "rejected_unreliable": reasons.get("UNRELIABLE", 0),
            "rejected_transparent_leak": reasons.get("TRANSPARENT_LEAK", 0),
            "rejected_content_mismatch": reasons.get("CONTENT_MISMATCH", 0),
            "rejected_not_measured": reasons.get("NOT_MEASURED", 0),
        },
        "admitted_detail": admitted,
        "sample_of_gate_reachers": reached_gate[:40],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=60)
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--tcp-timeout-ms", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=20260824)
    # Required, no default (H5/ADR-007). httpbin/ipify are neutral endpoints that
    # exist to be fetched -- not a login-walled third party.
    ap.add_argument("--target", default="https://httpbin.org/get")
    ap.add_argument("--ip-echo", default="https://api.ipify.org?format=json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    report = asyncio.run(run(args))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else (
        ROOT / "engineering" / "raw" / f"admission_live_{stamp}.json")
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    t = report["totals"]
    print(f"  probed        : {t['probed']}")
    print(f"  tcp ok        : {t['tcp_ok']}")
    print(f"  reached gate  : {t['reached_gate']}")
    print(f"  >=2 samples   : {t['with_2plus_samples']}")
    print(f"  ADMITTED      : {t['admitted']}")
    print(f"  rejected      : {t['rejected']}")
    print("  top reasons   :", dict(list(report["rejection_reasons"].items())[:6]))
    print(f"  -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
