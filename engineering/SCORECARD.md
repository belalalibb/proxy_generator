# SCORECARD — ATLAS PROXY FABRIC v4

**Date:** 2026-08-27 · **Final score: 91 / 100** (threshold: ≥ 90 — **PASS**)

Scoring rubric. Every row names its evidence; deductions name the gap they
charge. Full reasoning and method statements (n, percentile functions) live in
`engineering/FINAL_AUDIT.md`; this file does not repeat numbers without their
n.

## Awarded

| # | Dimension | Max | Awarded | Evidence |
|---|---|---|---|---|
| 1 | Latency vs historical admitted baseline | 20 | 20 | Legacy p50 6 359.5 / p95 15 903 ms (n=102, `pct_linear`/`pct_floor` per `measure_baseline.py`); v4 live admits 846.5 / 863.7 / 940.2 ms per-proxy p95 (n=3, k=5, `admission_live_adr024.json`). ~7.4× median, ~16.9× p95, n-asymmetry stated. |
| 2 | Admission gate honesty | 20 | 20 | Replay: v4 rejects 97/102 (95.1 %) of legacy admits (n=102, k=1 method stated, `admission_replay_20260824T020209Z.json`). Live: 3/300 admitted, k=5, TLS ON; every rejection reason-coded. |
| 3 | Concurrency: no double delivery (H3) | 15 | 15 | `lease_concurrency.json`: real 0 duplicates vs naive control 30 at identical config; oversubscription 48→24 with 0 duplicates. Control proves the test detects the defect (ADR-022). |
| 4 | Crash durability | 5 | 5 | Live transcript step 16: SIGKILLed child (returncode −9) holding a lease → 0 double-delivery violations, pool intact (`live_transcript_20260827T225147Z.json`); H8 integration tests. |
| 5 | Evidence discipline | 15 | 15 | 19/19 gate checks; README numbers re-derived from artifacts via `<!--verify:-->` anchors; ADR verify-targets resolve; cross-stream splice and percentile-ordering checks read artifacts, not prose; ADR-010 dated snapshots never overwritten. |
| 6 | Tests | 10 | 10 | 661 passed (630 unit + 31 integration), six test levels; declared count reconciled against `pytest --collect-only`, not against any document (ADR-018). |
| 7 | Source registry | 5 | 5 | 122 probed / 69 ACTIVE / 502 211 unique candidates (`source_probe_20260827T222532Z.json`); extended with pubproxy.com + proxyhub.me under snapshot discipline. |
| 8 | Security & hard constraints | 5 | 5 | No default target (caller-supplied `url` + allow-policy, exit 2 without `--target`); deny-private/metadata ranges on; no CAPTCHA/WAF circumvention; no source-URL literals in `.py` (ADR-002); `core/` pure, AST-enforced. |
| | **Subtotal** | **95** | **95** | |

## Deducted (from the remaining completeness dimension)

| # | Gap charged | Points | Where recorded |
|---|---|---|---|
| D1 | `atlas/api/` and `atlas/obs/` are empty stubs — the P09-era API/OpenAPI surface is not on disk; no HTTP API, no observability module ships | −6 | FINAL_AUDIT.md §6.1 |
| D2 | Stretch-goal miss: one live admit at p95 940.2 ms > 900 ms goal (within the configured 1500 ms policy; policy never re-tuned) | −1 | FINAL_AUDIT.md §2 |
| D3 | Carried-forward debts: `discovery_interval_s` drives nothing; `check_integrity` S5 has no caller; SOCKS rungs ~0 without `aiohttp-socks`; TOO_JITTERY never fired live (unit-tested only) | −2 | FINAL_AUDIT.md §6.3–6.4 |
| | **Total deductions** | **−9** | |

## Result

95 − 9 = **91 / 100 ≥ 90 → P14 SCORECARD PASS.**

The score is not higher because the shipped surface is engine-only (D1) — the
single largest gap — and because the audit refuses to round the 940.2 ms admit
down to the goal (D2) or to treat unfired rules as proven live behavior (D3).
The score is not lower because every headline claim — latency, rejection rate,
zero double delivery, crash durability — is backed by an artifact that names
its method and its n, and the gate re-derives the numbers from disk on every
`make doctor`.

**Verify:** `make doctor` # gate checks + full suite; score inputs: `engineering/raw/admission_live_adr024.json`, `engineering/raw/lease_concurrency.json`, `engineering/raw/live_transcript_20260827T225147Z.json`, `engineering/BASELINE.json`
