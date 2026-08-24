# DECISIONS — Architecture Decision Records

Format: context → decision → alternatives considered → consequence.
An ADR is added whenever the architecture changes; the resume prompt requires it.

---

## ADR-001 — Build `atlas/` as a clean parallel package, do not refactor the legacy scripts in place

**Phase:** P00 · **Status:** ACCEPTED

**Context.** 2 454 LOC across 6 files, of which 3 (`v1.py`, `v3.py`, `proxy_generator_v2.py`) are
near-duplicates sharing ~70 % of their source lists. 23 silent exception handlers, 0 tests,
0 type hints (`engineering/raw/bug_scan.json`). The legacy control flow is built around blocking
`requests` inside `ThreadPoolExecutor`, and its state is a mutable text file.

**Decision.** Build `atlas/` fresh, and *mine* the legacy tree for logic that measurement proves
valuable (see `MIGRATION_LEDGER.md`, 80 features mapped).

**Alternatives.**
1. *Incremental refactor of `proxy_generator_v2.py`.* Rejected: the file's core loop is
   synchronous-threaded and its state model is `proxy.txt`. Both must go; nothing of the
   skeleton would survive.
2. *Keep `v3.py` and bolt on an API.* Rejected: no atomic consumption is achievable on top of a
   text file, so H3 (no double delivery) could not be satisfied at all.

**Consequence.** Legacy files remain untouched and runnable, so the baseline stays reproducible.
Cost: the source lists must be re-expressed as data (done — that is `sources.json`).

---

## ADR-002 — Sources are data (`data/sources/sources.json`), never Python literals

**Phase:** P00 · **Status:** ACCEPTED (mandated by §4)

**Context.** 257 hardcoded URL literals, 123 unique. Adding a source needed a code edit; a dead
source could not be cooled down; and yield could not be attributed — the legacy log credits
`raw.githubusercontent.com` with 649 404 proxies across roughly 50 *different* repositories
(`ANALYSIS.md` §5).

**Decision.** A source is a record with a stable `id`, a declarative `parser` + `parser_args`,
and its own mutable `stats` block. Hot-reload; adding one at runtime joins the next cycle.

**Alternatives.**
1. *Python list of dicts with callable parsers* (what `v2.py:21-72` does). Rejected: parsers as
   bound methods cannot be serialised, so per-source state can't round-trip to disk.
2. *One file per source.* Rejected: 60+ tiny files, no atomic multi-source update.

**Consequence.** Parsers must be a closed, declarative set (`line_ipport`, `json_path`,
`csv_columns`, `html_table`, `regex`). A genuinely bespoke site needs a new parser *type* — an
acceptable, explicit cost.

---

## ADR-003 — Admission decided on **p95 of k=5 samples**, never on a single measurement or `min`

**Phase:** P00 · **Status:** ACCEPTED (mandated by §8; justified by measurement)

**Context.** The legacy gate was `status == 200 and len(text) > 1000` with one sample
(`proxy_generator_v2.py:380`). Its own recorded output shows what that admitted:
p50 **6 359.5 ms**, p95 **15 903 ms**, max **19 035 ms**, with **95.8 %** of accepted proxies over
1 500 ms and **56.8 %** over 5 000 ms (`BASELINE.json` §A).

**Decision.** k=5 samples per candidate → p50, p95, mean, stdev, `jitter = stdev/p50`,
`success_ratio`, throughput. The gate uses **p95**.

**Alternatives.**
1. *Keep 1 sample (cheap).* Rejected: this is precisely the defect that makes the legacy pool
   unusable, and it is the `LIVE ≠ GOOD` violation H7 forbids.
2. *Use `min` (flattering).* Rejected: `min` measures the best moment, not the experience; §8
   explicitly calls it "تجميل" (cosmetic).
3. *k=10.* Rejected for now: 2× the probe budget for a modest confidence gain. k is configurable
   and will be revisited during P06 calibration against real data.

**Consequence.** ~5× the probe cost per candidate. Mitigated by the cheap `S2 TCP` triage first
(v3's idea, `v3.py:393`), so k=5 is only paid by candidates that already passed a handshake.

---

## ADR-004 — SQLite (WAL) is the source of truth; text files are derived, atomically-replaced exports

**Phase:** P00 · **Status:** ACCEPTED

**Context.** H3 requires that a proxy is never handed to two concurrent requests, and H8 requires
survival of `SIGKILL`. `proxy.txt` cannot express a `LEASED` state, and `save()` truncates with
`open(...,'w')` (`proxy_generator_v2.py:467`) — a kill mid-write destroys the working set (B-04).

**Decision.** SQLite in WAL mode with `busy_timeout`. Consumption is a `BEGIN IMMEDIATE`
compare-and-set (`UPDATE ... WHERE state='READY' ... RETURNING`), never read-then-write. Exports
are written `.tmp` then `os.replace()` (atomic rename).

**Alternatives.**
1. *Postgres/Redis.* Rejected: an external service for a single-node tool; Redis alone is not
   durable enough for lease bookkeeping without extra work.
2. *File locks over JSON.* Rejected: no transactions, and B-05 shows the legacy read-modify-write
   race this reproduces.

**Consequence.** Single-writer-ish throughput. Acceptable: the workload is dominated by network
probes, not DB writes. WAL permits concurrent readers, so `/pool` and `/stats` never block leasing.

---

## ADR-005 — Protocol is discovered empirically; the source's label is a hint only

**Phase:** P00 · **Status:** ACCEPTED (mandated by §7 S3)

**Context.** Proof from the legacy list itself: `TheSpeedX/SOCKS-List/master/http.txt`
(`proxy_generator_v2.py:69`) is a **SOCKS** repository whose file is named `http.txt`. It measured
ALIVE with **2 853 unique** candidates — every one of which the legacy code tested as HTTP
(`proxy_dict = {'http': ..., 'https': ...}`) and therefore discarded (B-12).

**Decision.** `S3 PROTOCOL` probes http / https-CONNECT / socks4 / socks5, writes the discovered
protocol back to the record, and emits `PROTO_MISMATCH` when the label was wrong.

**Alternatives.** *Trust the label* — rejected, demonstrably loses thousands of working SOCKS
proxies. *Try all protocols on every candidate* — rejected as the default (4× cost); the discovered
protocol is cached per fingerprint and only re-probed on failure.

---

## ADR-006 — One failed fetch must not kill a source; cooldown requires *consecutive* failures

**Phase:** P00 · **Status:** ACCEPTED

**Context.** Discovered by accident during Phase 0, and it changed the design. The GeoNode API
returned **230 067 bytes of valid JSON**, then **659 bytes of non-JSON ~2 s later**, which the
re-probe filed as `TRULY_EMPTY`. A third, direct fetch returned **230 019 bytes → 500 unique
proxies** (`engineering/raw/geonode_body.txt`). The middle reading was **our own throttling**, not
a dead source. Corroborating evidence in the same sweep: 2 × HTTP 429 and 3 × 403 from hosts the
legacy code hammers with 100-150 threads (B-08).

**Decision.** (a) per-host rate limiting in addition to the global semaphore; (b) `ETag` /
`If-Modified-Since` so unchanged lists are not refetched; (c) `consecutive_failures` drives an
exponential cooldown `base * 2^n` capped at 1 h — a single failure never disables a source;
(d) a throttled/short body is a distinct reason-code, not "empty".

**Consequence.** Slower per-cycle source coverage, materially better source longevity — and we
stop being the cause of our own 429s.

---

## ADR-007 — No CAPTCHA/WAF/auth circumvention will be ported, and no default target will exist

**Phase:** P00 · **Status:** ACCEPTED (mandated by H5/§20)

**Context.** `bebo.py:11-28` contains a working 2captcha client (10 mechanical matches for
`captcha`). `v1.py:29`/`v3.py:30` default to probing `instagram.com` — a login-walled,
bot-hostile third party — thousands of times per run.

**Decision.** Both refused. Three `RETIRED_PROHIBITED` rows in `MIGRATION_LEDGER.md` with **no
replacement**. The target becomes a required, per-request, allow-policy-checked parameter.

**Alternatives.** *Port the CAPTCHA code but leave it disabled* — rejected; shipping the capability
is the violation. *Keep a "safe" default target* — rejected; any default means the operator
probes a third party they never named. `example.com` was used **only** for the Phase-0 baseline
measurement, where a fixed target is required for comparability.

**Consequence.** `GET /api/proxies` without `url` is an error, by design. This also removes the
legacy conflation of *target difficulty* with *proxy quality* — the 0.68 % legacy success rate
largely measured Instagram's defences.

---

## ADR-008 — `example.com` for the Phase-0 baseline, not a "realistic" site

**Phase:** P00 · **Status:** ACCEPTED

**Context.** The baseline must re-run the legacy algorithm *verbatim* to be a fair comparison, but
the legacy default target is prohibited (ADR-007).

**Decision.** `https://example.com` — the IANA-designated test domain. Legacy timing parameters
kept byte-for-byte (timeout 10 s, 2 retries, 100 workers, `status==200 and len(body)>1000`),
sample seeded (`random.seed(1337)`) for reproducibility.

**Consequence.** The measured 3.0 % live rate is a *floor* — the legacy accept-rule required
`len(body) > 1000` and `example.com` returns ~1 256 bytes, so the rule still applies, but a
heavier page would fail more proxies on bandwidth. Recorded honestly here rather than presented as
a universal figure. Comparison against v4 will use the *same* target for symmetry.

---

## ADR-009 — Survivorship bias in the "measured now" baseline is disclosed, not hidden

**Phase:** P00 · **Status:** ACCEPTED

**Context.** Re-testing `proxy.txt` today yields p95 **1 464 ms** — *better* than what v4 must
admit at first glance. But that list is ~9 months old: the slow proxies died and were never
recorded as deaths, so the survivors look artificially fast (n = 9).

**Decision.** Report **both** numbers and name the bias explicitly: the honest measure of what the
legacy gate *admitted* is the historical distribution (p50 6 359.5 ms, p95 15 903 ms, n = 102),
not the survivor distribution (n = 9).

**Consequence.** `FINAL_AUDIT.md` must compare v4 against the *historical admitted* distribution
and state n for every figure. A comparison against n=9 survivors would be a fabricated victory (H2).

---

## ADR-010 — Evidence must be *verified*, not *declared*; and a test that cannot fail is not evidence

**Phase:** P00 (retrofit) · **Status:** ACCEPTED

**Context.** A platform sync dropped `engineering/tools/` and 3 `engineering/raw/` files.
The loss was **silent**, and two independent process defects let the project keep looking complete:

1. **Evidence was declared, never verified.** `TASK_STATE.json` listed 6 tasks at
   `status: DONE` whose `evidence` paths no longer existed. Nothing ever checked that a
   declared artifact was on disk, so H1 ("evidence for every claim") was structurally
   unenforceable — it depended on the honesty of a JSON field.
2. **A green test suite proved nothing.** `pytest -q` reported **10 passed** while
   `atlas/core/` did not exist. The isolation tests glob `core/**/*.py`, so they were
   asserting over an *empty list* and passing **vacuously**. This is worse than a red
   suite: it is a false clearance signal.

Root cause of the loss itself: `engineering/tools/` was **never git-tracked**, and there was
no `.gitignore` documenting what may and may not be ignored.

**Decision.**
(a) `engineering/tools/gate_check.py` is the authority on phase-gate readiness and **fails the
build** if: any `DONE` task names a missing evidence path; any `files_changed` entry is absent;
any tool on disk is untracked by git; or `atlas/core/` has no modules for the architecture
tests to scan (the vacuity check).
(b) `make doctor` runs `gate_check.py` **before** `pytest`, so a vacuous pass can never be
banked as a gate.
(c) A `.gitignore` exists that explicitly never ignores `engineering/**`.
(d) Regenerated figures are **reconciled field-by-field** against the documented ones; a tool
exits non-zero on unexplained drift. Where a delta is a *definition* difference the definition
is pinned in the tool and both numbers are kept; where it is a *measurement* difference the new
run is filed as a **new dated snapshot** and the old one retained.

**Alternatives.**
1. *Re-declare the tasks DONE and move on* — rejected: that is precisely the H1/H2 violation
   this project exists to eliminate, and it would silently invalidate the baseline.
2. *Recreate the tools from memory and assume the old numbers* — rejected: fabrication (H2).
   Every figure was re-derived and reconciled instead.
3. *Trust CI to catch it* — rejected: there is no CI, and the check must run locally in the
   same command a developer already types.

**Consequence.** Phase Gate 0 was **revoked and re-earned**, not re-asserted. `pytest` alone is
no longer accepted as gate evidence. Cost: `make doctor` is slower and stricter, and it will
refuse to go green until `atlas/core/` contains real modules — which is the correct behaviour.

**Verification.** All deterministic figures reproduced *exactly* after the rebuild:
`257` URL lines · `123` unique URLs (122 real + 1 malformed) · `silent_handlers 23` ·
`bare_except 9` · `except_broad 33` · baseline **9/9 fields** incl. p50 6 359.5 ms and
p95 15 903 ms (n=102). Two counts were **superseded with cause**, not overwritten:
`except_pass` 10→9 and `max_workers_literal` 4→**9**.

---

## ADR-011 — The documented percentile method is preserved, even though it is unorthodox

**Phase:** P00 (retrofit) · **Status:** ACCEPTED

**Context.** Recomputing the baseline reproduced 8 of 9 fields but gave p95 **16 327.6 ms**
against the documented **15 903 ms**. The gap is not a data difference — it is a *method*
difference. Recovered empirically, the original tool used a **mixed** methodology:

| statistic | method | details (n=102) | log (n=118) |
|---|---|---|---|
| p50 | linear interpolation (`(n-1)·p`) | **6 359.5** ✅ | **6 092.5** ✅ |
| p95 | lower/floor rank `sorted[int((n-1)·0.95)]` | **15 903** ✅ | **15 903** ✅ |

It reproduces **both** documented streams exactly, which is what proves it is the original
behaviour rather than a coincidence — an interpolated p95 gives 16 327.6 / 15 970.0, close but
wrong, and a floor p50 gives 6 257.0 / 5 963.0.

**Decision.** Pin both methods in `measure_baseline.py`, and additionally report
`p95_ms_interpolated` and `p50_ms_floor` on every distribution so no headline figure is ever
silently method-dependent.

**Alternatives.**
1. *Switch to a single interpolated percentile and restate the baseline as 16 327.6 ms* —
   rejected. Raising the bar v4 must clear by ~424 ms would make v4's win look *larger*;
   quietly restating the target is exactly the H2 violation this project forbids.
2. *Report only the floor method* — rejected: floor is pessimistic at small n and would
   under-report v4's own p95 later. Both are emitted, so comparisons stay symmetric.

**Consequence.** The number v4 must beat remains **p50 6 359.5 ms / p95 15 903 ms at n=102**.
`FINAL_AUDIT.md` must state the method *and* n beside every percentile, and must compute v4's
own p95 with the **same** function for the comparison to be honest.

---

## ADR-012 — Fitness guards are scoped to *executable* code and must carry negative controls

**Phase:** P01 · **Status:** ACCEPTED

**Context.** Two architecture fitness tests failed on legitimate code, and the
naive fix for either one would have quietly destroyed the guard's value:

1. `test_core_declares_no_module_level_mutable_state` flagged
   `core/ports/__init__.py:19 '__all__'`. `__all__` is a declaration to the import
   machinery and must be a list of `str` by language convention — it cannot hold
   accumulated state.
2. `test_no_default_target_url_constant` flagged
   `core/domain/source.py:131`, which is a **docstring** explaining that the legacy
   code defaulted to a login-walled target and that v4 refuses to. The guard was a
   line-level regex, so it could not tell a *prohibition* from a *violation* —
   and `SECURITY.md` **requires** that refusal to be documented at the code it
   governs.

The dangerous part is the temptation: deleting the sentence, or loosening the
regex, both turn a red test green while removing real protection. That is the
ADR-010 failure mode (a guard that cannot fail) reappearing in a new form.

**Decision.**
(a) Scope the target guard to **executable string values** via AST, ignoring
    docstrings only. Any prohibited host reaching a variable, default argument,
    collection, dict value or f-string — the forms that actually cause traffic —
    still fails.
(b) Exempt exactly three import-machinery dunders (`__all__`, `__slots__`,
    `__match_args__`) from the mutable-state guard; nothing else.
(c) **Every guard that is relaxed must gain a negative control.** The scanner is
    extracted as a callable and fed known-bad source in a parametrised test
    (`module_const`, `default_arg`, `list_item`, `fstring`, `dict_value`), each
    asserting the guard still fires. A companion test asserts a docstring citation
    is *not* flagged, pinning the exemption. A third test asserts the scan set is
    non-empty, so the suite itself detects vacuity rather than relying only on
    `gate_check.py`.

**Alternatives.**
1. *Delete the explanatory docstring.* Rejected: it destroys the forensic record
   `SECURITY.md` mandates, to satisfy a regex.
2. *Add a `# noqa` to the docstring line.* Rejected: precedent for silencing
   guards by annotation, which is how the legacy `except: pass` culture began.
3. *Drop the host regex to code-only by stripping comments textually.* Rejected as
   fragile; AST knows exactly what a docstring is.
4. *Relax the guards and rely on review.* Rejected: unenforceable, and it is the
   exact assumption ADR-010 disproved.

**Consequence.** Guard count rises from 11 to 19 tests. The suite now proves its
guards *can* fail, so a future edit that reduces one to a no-op turns the suite
red instead of green.

---

## ADR-013 — A short or truncated read is a FETCH fault and may never be recorded as an empty source

**Phase:** P01 (retrofit of P00.T4/T5 tooling) · **Status:** ACCEPTED

**Context.** Rebuilding the lost `reprobe_empty.py`, the tool reported the GeoNode
JSON API as `TRULY_EMPTY` from a **74 241-byte** body — the third time this one
source has been misclassified, and the second distinct root cause.

The bug was mine, in the new tool: `aiohttp`'s `resp.content.read(n)` returns
whatever is **currently buffered**, up to `n` — *not* `n` bytes. It returned 74 241
of 230 067 bytes, truncating the JSON mid-structure, so `json.loads()` failed and a
live 500-proxy source looked empty.

It was caught only because the parser was **validated against the stored evidence
first**: `engineering/raw/geonode_body.txt` (230 019 bytes) yields exactly the
documented **500** unique proxies. A parser proven correct on stored bytes means a
live zero must be the *fetch*, not the parser. Without that ordering I would have
written "GeoNode is now dead" into the record — a fabricated finding (H2).

**Decision.**
(a) Read bodies to EOF with `iter_chunked()`; never `read(n)` as a body read.
(b) Record `Content-Length` and flag `short_read` when the body is shorter than
    declared, plus `body_truncated_at_cap` when the size cap is hit.
(c) Introduce the reason code **`FETCH_INCOMPLETE`**, distinct from `TRULY_EMPTY`
    and from `SOURCE_THROTTLED`. An incomplete read is **our** fault and must be
    re-fetched before any verdict.
(d) Cross-check content type: a `application/json` response that fails to parse is
    `FETCH_INCOMPLETE`, not empty.
(e) **Validate a parser against stored evidence before trusting it live.**

**Consequence.** The re-audit recovered **6** sources previously filed
`TRULY_EMPTY` (12 remain), and the ACTIVE registry is **68** sources rather than
the 63 documented in Phase 0 — an increase caused by fixing a defect in our own
tooling, recorded as a new dated snapshot alongside the original, not over it.
ADR-006 is reinforced: **one bad fetch must never kill a source**, and now we can
also tell *whose fault* the bad fetch was.
