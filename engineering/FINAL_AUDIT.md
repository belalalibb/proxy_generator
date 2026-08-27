# FINAL AUDIT — ATLAS PROXY FABRIC v4

**Date:** 2026-08-27 · **Verdict:** **SHIP** (with the named gaps in §6) · **Score:** see `SCORECARD.md` (**91 / 100**)

This document is the P14 deliverable and the project's STOP CONDITION artifact
(RESUME_PROMPT: "STOP CONDITION: all Phase Gates PASSED and FINAL_AUDIT.md verified").
Every quantitative claim below names its evidence file, its sample size n, and the
estimation method where a percentile is involved — per the honesty rules in
DECISIONS.md (~lines 196 / 286): compare v4 against the **historical admitted
distribution (n=102)**, never the n=9 survivorship-biased re-test (ADR-009); state
method *and* n beside every percentile; never mix the n=118 log-stream pair
(95.8/56.8) with n=102 figures (ADR-020).

---

## 1. Method

Three kinds of evidence are used, and they are never blended:

| Kind | Artifact(s) | What it can prove |
|---|---|---|
| **Historical baseline** | `engineering/BASELINE.json` (stream A: `proxy_details.json`, scan 2025-09-01) | What the legacy system admitted and how fast the admits were. |
| **Deterministic replay** | `engineering/raw/admission_replay_20260824T020209Z.json` | What the v4 gate *would have done* to the same 102 legacy admits (k=1 — all the legacy file carries; tests the p95 threshold only). |
| **Live measurement** | `engineering/raw/admission_live_adr024.json`, `engineering/raw/live_transcript_20260827T225147Z.json`, `engineering/raw/lease_concurrency.json` | What the assembled v4 does on the live network today. |

Percentile functions are pinned in `engineering/tools/measure_baseline.py`:
`pct_floor` (nearest-rank floor, used for the baseline p95 so the comparison is
parity-honest) and `pct_linear` (interpolated, used for p50 medians). Where v4's
own distribution is a list of per-proxy p95s (n=3), both interpretations are
stated rather than pretending n=3 supports a percentile hierarchy.

---

## 2. Latency: v4 vs the historical admitted distribution (n=102)

Baseline, stream A (`proxy_details.json`, n=102 working of 15 000 collected,
success 0.68 %, scan 1418.98 s):

| Metric | Legacy admitted (n=102) | v4 live admitted (n=3) |
|---|---|---|
| p50 | 6 359.5 ms (method: `pct_linear`, n=102) | 863.7 ms (median of the three per-proxy p95s, n=3) |
| p95 | 15 903 ms (method: `pct_floor`, n=102) | 940.2 ms (`pct_floor` over n=3 = max = 940.2; same value under `pct_linear`) |
| over 1500 ms | 95.1 % (n=102) | 0 % (n=3) |
| min / max | 756 / 19 035 ms | 846.5 / 940.2 ms (per-proxy p95 range, n=3) |

**Honest caveat — n asymmetry.** v4's n=3 is not comparable in statistical power
to n=102; the claim is therefore phrased as the *gate property*, not as a
population victory: v4's admission policy (`max_p95_ms = 1500`, config.yaml)
**structurally excludes** the tail that dominates the legacy distribution
(95.1 % of legacy admits exceed 1500 ms, n=102). No v4-admitted proxy can sit
in that tail without the gate having failed — and the gate's firing is itself
evidenced (rejection `TOO_SLOW_P95` fired twice in the live calibration,
`admission_live_adr024.json`).

**Goal-metric honesty.** The informal goal was p95 ≤ 900 ms. One of the three
live admits has p95 = **940.2 ms** (k=5 samples [899.9, 940.2, 917.7, 944.4,
949.2] — measured, not estimated), which **exceeds 900 ms while passing the
configured 1500 ms policy**. The audit records this as a miss against the
stretch goal, not a gate defect; the policy number was never re-tuned to make
the result look better (that would be the H2 violation).

Speedup on medians: 6 359.5 / 863.7 ≈ **7.4×**; on p95: 15 903 / 940.2 ≈ **16.9×**
(both with the n asymmetry stated above).

---

## 3. Admission honesty: what v4 does to what legacy admitted

Deterministic replay of the v4 gate over the 102 legacy admits
(`admission_replay_20260824T020209Z.json`, method: k=1 because the legacy file
carries one sample per proxy; the replay therefore tests the p95 threshold only):

- v4 **would reject 97 of 102** (95.1 %) of everything the legacy system handed out.
- The 5 survivors: p50 1 199.0 ms / p95 1 329.0 ms (method: `pct_floor`, n=5).
- The legacy system delivered that rejected 95.1 % to callers *as working
  proxies*; v4's gate makes that delivery impossible.

Live calibration (`admission_live_adr024.json`, k=5, TLS verification ON,
target https://httpbin.org/get, 300 probed): 86 TCP-OK → 12 reached the gate →
6 with ≥2 samples → **3 admitted (1.0 % of probed)**. Rejection stages:
S2 TCP 214, S3 protocol 74, S4/S5 gate 9. Reason histogram: TCP_TIMEOUT 194,
TCP_REFUSED 43, BAD_STATUS 28, PROXY_AUTH_REQUIRED 23, NOT_MEASURED 5,
UNRELIABLE 2, TOO_SLOW_P95 2. TOO_JITTERY fired 0 times (unit-tested only —
recorded, not hidden).

Live 17-step transcript (P13, `live_transcript_20260827T225147Z.json`,
target https://example.com, budget 6 sources / 40 probes): 17/17 steps OK;
41 candidates seen, 40 probed, 1 admitted (2.5 %), 40 persisted
(READY 1 / COOLING 39), lease granted → LEASED-row proof → release accounting →
SIGKILL child (returncode −9) left the pool consistent with 0 double-delivery
violations. The 2.5 % is free-proxy reality at that hour; the README phase line
for P13 should not have repeated the "inside the 3–12 % band" framing for it,
and this audit does not.

---

## 4. Concurrency & durability

- **H3 — no double delivery** (`lease_concurrency.json`): head-to-head, same
  pool (12), same processes (6), same request size (6) — the only difference is
  the lease implementation. Real (BEGIN IMMEDIATE compare-and-set): **0
  duplicates**, 12 unique fingerprints of 12 handed out. Naive control: **30
  duplicates** — proving the test body detects double delivery when present
  (ADR-022). Oversubscribed arm: 48 requested from a pool of 24 across 12
  processes → 24 handed out, 0 duplicates.
- **Crash durability**: live transcript step 16 — a child leases 5 rows and is
  SIGKILLed (asserted returncode == −9, i.e. death by signal, not a clean
  exit); WAL rollback leaves 0 double-delivery violations and the pool intact
  (40 rows). Test-level: "H8: SIGKILL after acknowledged write loses nothing"
  (integration tests).
- Baseline contrast: legacy double delivery was **unbounded** (no lease
  protocol at all) and crash recovery was **none**; v4's are 0 and durable.

---

## 5. Throughput / time-to-working

Legacy: 4.31 working proxies/minute → **2.32 min to produce 10 working
proxies** (derived from stream A, n=102, BASELINE.json). v4 live transcript:
1 admit in a 40-probe cycle bounded to 6 sources (budget-limited by
construction; the cycle is not a throughput benchmark). The honest statement:
v4's *admitted* rate on the live sweep was 3/300 (1.0 %, n=300) with k=5 and
TLS ON — slower per admit than legacy's 0.68 % headline in raw collection
terms, because v4 pays for five samples and refuses to call unmeasurable
proxies "working". The throughput claim v4 actually makes is about the
*consumer*: a caller receives only gate-passed, leased, non-duplicated proxies,
which the legacy system never provided at any speed.

---

## 6. Gaps, debts and unfinished business (recorded, not hidden)

1. **`atlas/api/` and `atlas/obs/` are empty stubs** (`__init__.py` only). The
   P09-era API/OpenAPI surface referenced in RESUME_PROMPT's numbers table is
   **not on disk**. There is no HTTP API and no observability module in v4 as
   shipped; the gate passes because no task row claims otherwise. Any consumer
   uses the engine/services directly (see USAGE.md).
2. **Stretch-goal miss**: one live admit at p95 940.2 ms > 900 ms goal
   (within the 1500 ms policy). §2.
3. **Carried forward from earlier audits**: `discovery_interval_s` in config
   drives nothing; `check_integrity` S5 has no caller; SOCKS rungs are ~0
   without `aiohttp-socks` (SOCKS candidates are reported as untestable, never
   as failures); `check_adr_claims_are_verifiable` walks ADR→code only.
4. **TOO_JITTERY never fired live** (0 of 6 multi-sample proxies): the rule is
   unit-tested only.
5. **ADR-009 survivorship** applies to every live figure: today's candidates
   are not the 2025 population; point-in-time source variance (69 vs 67 ACTIVE
   five minutes apart) is upstream availability, not code.
6. **Defects found and fixed in v4's own process**: V4-01, V4-02 (percentile
   estimator reading the wrong end of the distribution — now gate-checked
   against artifacts), V4-03 (intake dedup keyed on fingerprint instead of
   endpoint — fixed per ADR-040). All three in `engineering/BUG_LEDGER.md`.

---

## 7. Tests, gate and registry state at audit time

- `make doctor`: **19/19 gate checks PASS**, **661 tests passed**
  (630 unit + 31 integration; `declared_test_count_matches_collection`
  reconciles the number against `pytest --collect-only`, not against a
  document).
- Source registry: **122 URLs probed, 69 ACTIVE** (60 regex-adjacent, 8
  HTML-table, 1 JSON), **502 211 unique candidates** —
  `engineering/raw/source_probe_20260827T222532Z.json` (extended this session
  with pubproxy.com + proxyhub.me; new dated snapshot, pinned ones untouched,
  ADR-010).
- Phase gates P00–P13: PASSED; P14: this document + `SCORECARD.md`.

---

## 8. Verdict

ATLAS PROXY FABRIC v4 **ships**. Against the historical admitted distribution
it replaces (p50 6 359.5 ms / p95 15 903 ms, n=102, `pct_floor`/`pct_linear`
as pinned), v4's live admits are an order of magnitude faster (n=3, same
functions), the gate rejects 95.1 % of what legacy called "working" (n=102
replay, k=1 method stated), double delivery is 0 under proven-detecting
concurrency (control: 30), and crash durability is demonstrated at test and
live level. The gaps in §6 — above all the absent API/observability surface —
are recorded here, in `TASK_STATE.json`, and in `SCORECARD.md`, which is why
the score is **91** and not higher.

**Verify:** `make doctor` # 19 checks + 661 tests; artifacts cited above under `engineering/raw/`
