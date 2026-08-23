# PROGRESS LOG — ATLAS PROXY FABRIC v4

> Append-only. Newest entry at the bottom. Every entry names the artifact it produced.
> سجل زمني — يُضاف إليه فقط، ولا يُعاد كتابته.

---

## 2026-08-23 — P00 FORENSIC ARCHAEOLOGY · STARTED

**AR:** بدأت بقراءة المشروع بالكامل قبل كتابة أي كود، التزامًا بـPHASE GATE 0.
**EN:** Read the whole project before writing any code, per PHASE GATE 0.

Environment established: Python 3.13.13, `aiohttp` 3.13.4 / `requests` / `bs4` / `cryptography`
present. Outbound network confirmed (`example.com` 200 in 62 ms, `raw.githubusercontent.com` 200
in 59 ms). Repo is a single-commit GitHub project (`belalalibb/proxy_generator`, branch `main`).

Inventory read: 9 files, 2 454 LOC of Python + 3 data artifacts (`proxy.txt` 616 lines,
`proxy_details.json` 725 lines, `proxy_scraper.log` 248 lines).

---

## 2026-08-23 — P00.T2 · legacy source extraction

Built `tools/extract_legacy_sources.py` (AST-based, so CRLF and encoding oddities can't break it;
regex fallback on `SyntaxError`).

**Result:** **123 unique URLs** — `proxy_generator_v2.py` 93, `v1.py` 72, `v3.py` 69, `v2.py` 10,
`bebo.py` 3, `proxychecker.py` 2.
→ `engineering/raw/legacy_urls.json`

**Immediate finding:** `bebo.py` contains a **2captcha** client. Flagged as an H5/§20 prohibition
before doing anything else — it will not be ported. (→ ADR-007)

---

## 2026-08-23 — P00.T3 · all 123 URLs actually fetched

Built `tools/probe_legacy_sources.py` (async, concurrency 12, 20 s timeout, 4 MB body cap).
No verdict in this project is based on reading the old code's comments — every URL was requested.

**Result (2.1 s wall clock):** ALIVE 56 · DEAD 35 · EMPTY 25 · ERROR 1 · NOT_A_SOURCE 6.
DEAD breakdown: `404 × 23`, `502 × 4`, `403 × 3`, `429 × 2`, `521 × 2`, `526 × 1`.
Total unique candidates from ALIVE sources in that single sweep: **93 581**.
→ `engineering/SOURCE_INVENTORY.json`

**Finding:** 28 % of the legacy source list is dead, and the legacy code re-requested every dead
URL on every cycle because failures were swallowed (`except Exception: pass`).

---

## 2026-08-23 — P00.T4 · the naive regex was itself a bug

25 URLs returned HTTP 200 with real content but scored zero candidates. Cause: the legacy regex
only matches `ip` and `port` when **adjacent**. JSON APIs put them in separate keys; HTML tables
put them in separate `<td>`. Marking these "empty" would have discarded live sources.

Built `tools/reprobe_empty.py` with a recursive JSON walker and an HTML-table cell-pair parser.

**Recovered 6 sources** a regex-only audit would have thrown away:
`free-proxy-list.net/anonymous-proxy.html` 198 · `sslproxies.org` 100 · `hide.mn` 64 ·
`geonode.com/free-proxy-list` 55 · `list.proxylistplus.com/SSL-proxy` 50 · `proxybros.com` 28.
18 remain `TRULY_EMPTY` (JS-rendered / paywalled) → registered disabled **with a reason**, not
silently deleted.
→ `engineering/raw/reprobe_empty.json`

---

## 2026-08-23 — P00.T5 · a misclassification I caught and corrected

The GeoNode JSON API measured **230 067 bytes** in the first pass, then **659 bytes of non-JSON**
in the re-probe ~2 s later, which my own tool filed as `TRULY_EMPTY`.

I did not accept either reading. A third, direct `curl` returned **230 019 bytes of valid JSON →
500 unique proxies** (`tools/verify_geonode_parser.py` re-used the *same* parser to prove it).

The 659-byte body was **our own per-host throttling**. The inventory record was patched to
`ALIVE_JSON` and a `corrections[]` entry was appended, with `engineering/raw/geonode_body.txt` as
the raw evidence.

**This changed the architecture** → **ADR-006**: per-host rate limiting, `ETag`/`If-Modified-Since`,
and cooldown driven by *consecutive* failures — one bad fetch must never kill a source.
Corroboration in the same sweep: 2 × 429 and 3 × 403 from hosts the legacy code hits with 100-150
threads.

**Final usable inventory: 63 of 123 URLs** (56 regex + 6 HTML-table + 1 JSON).

---

## 2026-08-23 — P00.T6 · baseline measured, not guessed

Built `tools/measure_baseline.py` with two independent streams.

**A) Historical** (the user's own recorded run, `proxy_details.json` + `proxy_scraper.log`):
15 000 collected → **102 working (0.68 %)** in 1 418.98 s. Accepted-proxy latency
**p50 6 359.5 ms · mean 7 145.1 ms · p95 15 903 ms · max 19 035 ms**;
from the log (n = 118): **95.8 % over 1 500 ms**, **56.8 % over 5 000 ms**.

**B) Measured now** — re-implemented `proxy_generator_v2.ProxyScraper.test_one` **verbatim**
(timeout 10 s, 2 retries, 100 workers, accept rule `status==200 and len(body)>1000`) against
`https://example.com` (IANA test domain — chosen so the benchmark itself is ToS-clean, ADR-008),
300 proxies sampled from `proxy.txt` with `random.seed(1337)`:
**9 live / 300 = 3.0 %** in 68.5 s; latency min 297 / p50 1 106 / **p95 1 464** / max 2 157 ms.
→ `engineering/BASELINE.json`

**The central finding of Phase 0.** The legacy system had **no speed gate at all** — a 19-second
proxy was recorded as a success identical to a 756 ms one. That is exactly the `LIVE ≠ GOOD`
failure H7 forbids, and it is the reason this rebuild exists.

Disclosed honestly (**ADR-009**): the "measured now" p95 of 1 464 ms looks good only because of
**survivorship bias** — n = 9 survivors of a 9-month-old list. The honest measure of what the legacy
gate *admitted* is the historical n = 102 distribution.

---

## 2026-08-23 — P00.T7 · defects counted mechanically

Built `tools/verify_bug_lines.py` so no defect count in `BUG_LEDGER.md` is hand-written.

**Totals:** `hardcoded_http_url` **257** · `except_broad` 33 · **silent_handlers 23** ·
`time_sleep` 13 · `captcha` 10 · `except_pass` 10 · **`bare_except` 9** · `verify_false` 9 ·
`open_write_truncate` 8 · `max_workers_literal` 4 · `disable_warnings` 3 · `input_call` 3 ·
`open_append` 2 · `instagram_target` 2.
→ `engineering/raw/bug_scan.json`, `engineering/BUG_LEDGER.md` (16 defect classes, file:line each)

Worst single line: `proxy_generator_v2.py:237` — `except Exception: pass` **in the fetch path**,
which is why 35 dead URLs were retried forever.

---

## 2026-08-23 — P00.T8/T9/T10 · archaeology, migration, security

**Data archaeology** (`ANALYSIS.md` §5): `raw.githubusercontent.com` 649 404 candidates,
`proxyspace.pro` 244 623, `api.openproxylist.xyz` 154 290. But the legacy log keyed yield by
**hostname**, so ~50 distinct GitHub repos were merged into one number — yield was unattributable.
**Lesson: volume ≠ value.** 649 k candidates still produced only 102 slow proxies. v4 ranks sources
by `quality_rate`/`elite_rate`, never by raw count.

**Migration ledger:** **80 legacy features** enumerated, **0 rows `unknown`**.
ADOPTED 30 · GENERALISED 19 · REPLACED 14 · RETIRED_HARMFUL 14 · RETIRED_PROHIBITED 3.
→ `engineering/MIGRATION_LEDGER.md`

**Security:** the two refusals (2captcha client; `instagram.com` default target) recorded with their
legacy line numbers and the reason no replacement will be built.
→ `engineering/SECURITY.md`

---

## 2026-08-23 — P00 · PHASE GATE 0 · **PASSED**

Five required evidence files exist, plus supporting raw data and the resume system:

| file | state |
|---|---|
| `engineering/ANALYSIS.md` | 9/9 files verdicted, source inventory, baseline, archaeology |
| `engineering/SOURCE_INVENTORY.json` | 123 URLs really fetched + refinement + correction |
| `engineering/BASELINE.json` | historical + re-measured, `measurable` flags present |
| `engineering/BUG_LEDGER.md` | 16 classes, counts mechanically verified |
| `engineering/MIGRATION_LEDGER.md` | 80 features, 0 unknown |
| `engineering/SECURITY.md` | H5 commitment recorded |
| `engineering/DECISIONS.md` | ADR-001 … ADR-009 |
| `engineering/TASK_STATE.json` | schema-shaped, 11 P00 tasks DONE with evidence paths |
| `engineering/RESUME_PROMPT.md` | resume contract |

`atlas/` **does not exist** at this point — deliberately, per PHASE GATE 0.

**Honest invariant status:** H1 ✅ · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ · H3 ❌ not yet verifiable (no pool
until P08) · H7 ❌ not yet verifiable (no admission gate until P06) · H8 ❌ not yet verifiable (no
storage until P07). These three are recorded `false` rather than optimistically assumed.

**NEXT:** P01.T1 create the `atlas/` skeleton, then immediately P01.T2 — the architecture isolation
test — so the `core/` boundary is enforced by a failing test from the first commit rather than
retrofitted later.
