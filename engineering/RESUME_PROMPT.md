# RESUME PROMPT — paste this to continue the build

```
Resume ATLAS PROXY FABRIC v4.

READ FIRST (in order):
1. engineering/TASK_STATE.json
2. engineering/PROGRESS.md
3. engineering/ANALYSIS.md
4. engineering/DECISIONS.md
5. engineering/FINAL_AUDIT.md (if exists)

THEN: run `make doctor && pytest -q` and compare reality to TASK_STATE.

RULES:
- Do NOT restart completed work. Do NOT overwrite working functionality.
- Continue from the first task whose status != DONE.
- Any DONE task lacking on-disk evidence → downgrade to TODO and redo.
- Architecture changes require a new ADR in DECISIONS.md with justification.
- After every milestone: update PROGRESS.md + TASK_STATE.json, run tests,
  record failures and the exact next_action.
- Never claim completion without artifacts. Never fabricate numbers.
STOP CONDITION: all Phase Gates PASSED and FINAL_AUDIT.md verified.
```

---

## Current position (2026-08-23)

**Phase:** P00 PASSED → P01 IN_PROGRESS
**Next action:** `P01.T1` — create the `atlas/` package skeleton, then **immediately** `P01.T2`
(the architecture isolation test), so the `core/` boundary is enforced from the first commit.

`atlas/` does not exist yet. That is correct, not an omission: PHASE GATE 0 forbids writing code
before the Phase-0 evidence files exist.

## Hard constraints that must not be re-litigated

| # | Constraint | Where it came from |
|---|---|---|
| 1 | No CAPTCHA/WAF/rate-limit/auth circumvention. The legacy 2captcha client (`bebo.py:11-28`) is **not** to be ported. | H5/§20, ADR-007, `SECURITY.md` |
| 2 | No default target. `url` is a required per-request parameter with an allow-policy. The legacy `instagram.com` default is retired. | H5, ADR-007 |
| 3 | No source URL may appear as a literal in a `.py` file. Sources live in `data/sources/sources.json`. | §4, ADR-002 |
| 4 | `core/` may not import `adapters/`, `api/`, or any network library. Enforced by `tests/unit/test_architecture.py`. | §3, P01.T2 |
| 5 | Admission is gated on **p95 of k=5 samples**, never one sample and never `min`. | §8, ADR-003, B-03 |
| 6 | Consumption is a single `BEGIN IMMEDIATE` compare-and-set. Never read-then-write. | §9, ADR-004 |
| 7 | One failed fetch must not disable a source; cooldown needs *consecutive* failures. Per-host rate limiting + ETag are required. | ADR-006 (learned from the GeoNode incident) |
| 8 | Protocol is discovered empirically; the source label is a hint. | §7 S3, ADR-005, B-12 |

## Numbers to beat (all from `engineering/BASELINE.json`)

| metric | legacy | v4 target |
|---|---|---|
| admitted-proxy p95 latency | **15 903 ms** (historical, n=102) | ≤ 900 ms |
| admitted-proxy p50 latency | **6 359.5 ms** | — |
| % admitted over 1 500 ms | **95.8 %** (n=118) | ~0 % |
| success rate | **0.68 %** (15 000 → 102) | admission rate 3–12 % of probed |
| live rate of stored list, re-tested | **3.0 %** (9/300, seed 1337) | 100 % of *delivered* proxies |
| minutes to 10 working | 2.32 | TTR ≤ 120 s after consuming 5 |
| double delivery | unbounded | **0** |
| crash recovery | none | 10/10 |
| tests | 0 | 6 levels, `core/` ≥ 90 % |

**Do not compare v4 against the n=9 survivor p95 of 1 464 ms** — that figure is survivorship-biased
(ADR-009). The honest legacy comparator is the historical n=102 distribution.

## Assets already measured and ready to consume

- **63 usable legacy sources** (56 `line_ipport` + 6 `html_table` + 1 `json_path`), with per-URL
  unique-candidate counts and fetch latencies → `engineering/SOURCE_INVENTORY.json`.
  P02 needs ≥ 25 *admitted* sources, so this is a strong starting set, but each still has to pass
  the §4 Source Admission Test (reachable → parseable → ≥50 candidates → ≥10 % unique → sample-validate 40 → live_rate ≥ 3 %).
- **35 dead URLs** with their exact status codes — register them disabled with reasons rather than
  re-discovering them.
- **18 `TRULY_EMPTY`** (JS-rendered) — same treatment.
- **616 seed candidates** from `proxy.txt`, to be treated as *unverified*.
- **≥ 5 SOCKS-capable sources** are required by P02; `TheSpeedX/SOCKS-List` (2 853 unique) and
  `hookzof/socks5_list` are known starting points, and `S3` discovery will find more mislabelled ones.

## Tools already built (reusable, all under `engineering/tools/`)

| tool | reuse for |
|---|---|
| `extract_legacy_sources.py` | AST URL extraction |
| `probe_legacy_sources.py` | the async fetch + candidate-count pattern for `adapters/fetchers/` |
| `reprobe_empty.py` | **`walk_json()` and `parse_html_table()` are directly portable** to the `json_path` and `html_table` parsers, and `ok_pair()` already implements the private/reserved-range rejection required by `S1` |
| `measure_baseline.py` | the comparison harness for `FINAL_AUDIT.md` — re-run it against v4 with the same target and seed |
| `verify_bug_lines.py` | regression check that v4 introduces no bare `except:` |

## Known gaps to fix when the relevant phase arrives

| gap | phase |
|---|---|
| `atlas/` skeleton + isolation test | P01 |
| Source admission test + ≥25 admitted + ≥5 SOCKS | P02 |
| Rotation fairness (500-cycle sim, chi-square, starvation-free) | P03 |
| Normalizer ≥60 cases + Hypothesis idempotence | P04 |
| Every reason-code deterministically provoked by a fake server | P05 |
| Calibration on ≥800 real candidates → admission rate 3–12 % | P06 |
| SIGKILL ×10 durability | P07 |
| 200-concurrent × 20-run double-delivery proof | P08 |
| OpenAPI contract snapshot + SSRF/ratelimit/401/422 tests | P09 |
| TTR ≤ 120 s measured ×3 | P10 |
| `/health` `/stats` `/metrics` + `atlas doctor` | P11 |
| Full 6-level suite green | P12 |
| 17-step E2E live transcript | P13 |
| `FINAL_AUDIT.md` + `SCORECARD.md` ≥ 90 | P14 |
