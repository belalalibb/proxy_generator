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
p50 **6 359.5 ms**, p95 **15 903 ms**, max **19 035 ms**, with **95.1 %** of accepted proxies over
1 500 ms and **58.8 %** over 5 000 ms — all five figures from the **n=102**
`proxy_details.json` stream (`BASELINE.json` §A). The 95.8 % / 56.8 % pair quoted here
until 2026-08-24 came from the n=118 log stream; see ADR-020.

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

**Verify:** `python3 engineering/tools/gate_check.py --json` # runs before pytest in `make doctor`

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

**Verify:** `grep -n 'def pct_floor\|def pct_linear' engineering/tools/measure_baseline.py` # both methods pinned

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

**Consequence (MEASURED).** The suite went from **12 collected / 2 failing** to
**22 passing** at the time of implementation, and 58 passing once P01.T3 landed.
The suite now proves its guards *can* fail, so a future edit that reduces one to a
no-op turns the suite red instead of green.

**Verify:** `python3 -m pytest atlas/tests/unit/test_architecture.py -q` # all pass, incl. 7 negative controls

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

**Verify:** `grep -c 'iter_chunked' engineering/tools/probe_legacy_sources.py` # >= 1, and no `content.read(BODY_CAP)`

**Consequence (MEASURED 2026-08-24, snapshot `source_probe_20260824T005530Z.json`).**
The re-audit recovered **6** sources previously filed `TRULY_EMPTY` (**14** remain),
and the ACTIVE registry is **69** sources (ALIVE 60 + HTML_TABLE 8 + JSON 1).

**The dominant effect was not the source count but the harvest**: unique candidates
from the same 120 URLs went from **74 895 to 504 193 (×6.73)**. Large lists were
being cut off mid-body while a regex still matched proxies in the surviving prefix,
so nothing looked wrong. GeoNode's API went from 0 to **500** candidates.

> **Correction (H2).** This ADR was first written stating "12 remain" and "68
> ACTIVE" *before the fix was implemented*. Both were wrong: the measured values
> are **14** and **69**. The figures above come from the artifact; see
> RECONCILIATION.md §5-§6, which also records that ADR-012 and ADR-013 were
> initially documented without being implemented at all.
ADR-006 is reinforced: **one bad fetch must never kill a source**, and now we can
also tell *whose fault* the bad fetch was.

---

## ADR-014 — An ADR that claims an implementation must name a verifiable marker, checked by the gate

**Phase:** P01 · **Status:** ACCEPTED

**Context.** ADR-012 and ADR-013 were written, committed, and cited in `README.md`
as though they were done. Neither was implemented. The README simultaneously
claimed "19 passed" while the suite was **2 failed, 10 passed**, and "68 ACTIVE"
while the only artifact said 61.

Nothing caught it. `gate_check.py` verifies that a **task's** evidence file exists,
but an ADR is prose, and prose was trusted. This is the same failure mode as
ADR-010 (a test that could not fail) one level up: **a decision record that cannot
be falsified is not a decision record, it is an intention.**

Sync loss made it easier to miss — files vanished twice — but the sync did not
write those sentences. I did, before the code existed.

**Decision.**
(a) An ADR that asserts an implementation MUST carry a machine-checkable marker
    line: `**Verify:** <shell command> # <expected>`.
(b) `gate_check.py` gains `adr_claims_are_verifiable`: every ADR containing an
    implementation verb in its Decision section must have a `**Verify:**` line.
(c) `gate_check.py` gains `readme_numbers_have_artifacts`: numeric claims in
    `README.md` tagged `<!--verify:...-->` are re-derived from the named artifact
    and must match.
(d) No phase gate may be declared PASSED while either check fails.
(e) **Numbers in prose are copied FROM artifacts, never typed from memory.** Where
    an ADR is written before the work, it must say `Status: PROPOSED`, not
    `ACCEPTED`, and must not state measured consequences.

**Alternatives.**
1. *Be more careful.* Rejected — precisely the assumption that failed. ADR-010
   already established that unenforced discipline decays.
2. *Forbid writing an ADR before implementing.* Rejected: designing in prose first
   is valuable. The fix is an honest `PROPOSED` status, not a ban.
3. *Require every ADR to link a test.* Rejected as too rigid — ADR-008 (choice of
   `example.com`) and ADR-009 (bias disclosure) are judgements with no code.

**Consequence.** ADR-012 and ADR-013 now carry `**Verify:**` lines. A future ADR
describing work that does not exist fails `make doctor` instead of becoming a
false claim in the README.

**Verify:** `python3 engineering/tools/gate_check.py` # adr_claims_are_verifiable PASS

---

## ADR-015 — A field named `bytes` must contain octets, not decoded characters

**Status:** ACCEPTED · 2026-08-24 · supersedes the `bytes` key in probe artifacts

### Context

`probe_legacy_sources.py:146` recorded `"bytes": len(body)` where `body` is the
result of `raw.decode("utf-8", errors="replace")`. `len()` of a `str` is a
**character** count. For any non-ASCII response the two differ, so the field was
silently wrong under exactly the conditions that matter (HTML pages with `—`, `©`,
non-Latin text).

Proven from stored evidence, no network needed:

| `engineering/raw/geonode_body.txt` | value |
|---|---|
| `len(raw)` — octets | **230 067** |
| `len(decoded str)` — characters | **230 019** |
| non-ASCII characters | 45 |

And across the pinned snapshot's 83 rows carrying both fields:

| relation | rows |
|---|---|
| chars < bytes | 28 |
| chars > bytes | **0** ← required if the cause is multi-byte decoding |
| equal (pure ASCII) | 55 |

Zero counterexamples in 83 rows. The cause is established, not guessed.

### Consequences

- The key is renamed **`body_chars`**; the octet count remains `body_bytes`.
- `measure_baseline.py` had the same bug (`len(r.text)`) and now records both.
- **My own earlier documentation repeated the error**: RECONCILIATION.md and
  PROGRESS.md described `geonode_body.txt` as "230 019 B". That is the character
  count. Corrected in place with a footnote rather than silently edited.
- No verdict changes. The field was descriptive metadata, never an input to
  ALIVE/DEAD/EMPTY classification — scope of impact stated so the reader need not
  guess whether conclusions moved. (They did not.)

### Why this is recorded rather than quietly renamed

A wrong unit is how a "230 019" becomes an authoritative number in a later report.
The measurement was never re-run to fix it: the old artifacts keep the old key, and
the ADR states which key means what, so historical files stay readable.

**Verify:** `python3 engineering/tools/verify_units.py`

---

## ADR-016 — Evidence of the form `path::symbol` must verify the *symbol*, not just the file

**Status:** ACCEPTED · 2026-08-24 · strengthens ADR-010 and ADR-014

### Context

`gate_check.py`'s `done_tasks_have_evidence` resolved each evidence string with
`(ROOT / ev).exists()`. For `path::test_name` evidence that asks the filesystem for
a file literally named `atlas/tests/unit/test_registry.py::test_x`, which never
exists — so the check reported *missing* for real tests, and would equally have
reported *present* for none of them. The failure mode is asymmetric and worse than
it looks: had the path check been written as a `startswith`, or had the evidence
been listed file-first, **a task could cite a test that does not exist and pass.**

It stayed latent through P00 and P01 because every earlier task cited plain file
paths. **P02 was the first phase to cite individual test functions**, which is when
the hole surfaced — as a false *failure*, luckily, rather than a false pass.

### Decision

Split evidence on `::`. Verify the file exists, then verify the file actually
defines `def <symbol>(`. Report *which* half failed (`no such file` vs
`symbol not defined in file`) so a reader is never left guessing.

### Negative control (required by ADR-010 — a check that cannot fail is not a check)

Injecting `atlas/tests/unit/test_registry.py::test_this_does_not_exist` into
P02.T2's evidence:

```
[FAIL] done_tasks_have_evidence
       missing: P02.T2 -> …::test_this_does_not_exist (symbol not defined in file)
exit=1
```

Removing it restores `all checks passed`. The guard demonstrably bites.

### Consequences

- Task evidence may now name a specific test, and that citation is *enforced*.
- This is the third self-inflicted verification defect found in two sessions
  (ADR-013 truncated fetch, ADR-015 wrong units, ADR-016 unresolved symbols).
  All three were in the machinery that *checks* the work, not in the work. Logged
  plainly because the pattern matters more than any single bug: **tooling that
  reports success is the least-tested code in the repository.**

**Verify:** `python3 engineering/tools/gate_check.py` (and the negative control above)

---

## ADR-017 — One concept must not have two vocabularies

**Status:** ACCEPTED · 2026-08-24 · found while implementing P03

### Context

`ParserKind` (domain, written by hand in P01) declared five members:
`line_ipport`, `json_path`, `csv_columns`, `html_table`, `regex`.

`sources.json` (registry, **generated from measured probe results** in P02) uses
three: `regex_adjacent`, `json_path`, `html_table`.

These describe the same concept and did not agree:

| | |
|---|---|
| enum members with no implementation | `csv_columns`, `regex` |
| enum members no source ever used | `line_ipport` |
| registry value the enum could not represent | **`regex_adjacent`** |
| ENABLED rows carrying that value | **59 of 67 (88%)** |

`adapters/registry.py` independently re-typed the correct three as a literal
`VALID_PARSERS` frozenset, so the loader validated rows happily while the domain
could not represent 88% of them.

**Why two green phase gates missed it:** nothing had ever converted a `SourceRow`
into a `Source`. `ParserKind(row.parser)` was never evaluated, so the
contradiction had no execution path on which to fail. P03 is the first phase that
must cross that seam — which is exactly when it surfaced.

The pinning test made it worse by looking adequate:

```python
assert {"line_ipport", "json_path", "html_table"} <= kinds   # passes
```

A **subset** assertion cannot detect a vocabulary that is simultaneously *too
large* (three unused members) and *missing the one member that matters*.

### Decision

1. `ParserKind` names **exactly** the three implemented, measured parsers.
2. The domain test asserts `==`, not `<=`.
3. `VALID_PARSERS` is **derived** — `frozenset(k.value for k in ParserKind)` —
   never re-typed. Divergence becomes unrepresentable rather than merely tested.
4. `row_to_source()` / `fetchable_sources()` cross the seam explicitly and raise
   `RegistryError` on any parser the domain cannot represent.
5. `test_fetch.py::test_every_parser_kind_has_an_implementation` ties the enum to
   `_STRATEGIES`, so a declared parser with no implementation cannot exist.

### A design question the data settled

My first version of `row_to_source` was written to convert every row, and my
first test asserted that a `parser=None` row becomes a DISABLED `Source`. The
registry disagreed: **37 of the 53 DISABLED rows have no parser** — nothing ever
parsed them, which is *why* they are disabled. A `Source` that cannot say how to
read its own payload is not a source. The conversion now refuses, and
auditability is unaffected because the reason lives on the registry row (ADR-002).
I corrected the test rather than weakening the model to match my assumption.

### Negative controls (ADR-010)

```
inject CSV_COLUMNS into ParserKind  -> test_every_parser_kind_has_an_implementation FAILS
inject `import aiohttp` into tests  -> test_no_test_in_this_file_uses_the_network FAILS
```

Both restore green on removal.

### Consequences

- Adding a parser now means: implement it, add the enum member, add a source.
  Any two without the third fails the gate.
- **Generalised lesson:** ADR-013/015/016 were defects in *verification tooling*.
  This one is different and worth naming separately — a defect at a **seam that
  no code had crossed yet**. Two independently-correct components, never
  introduced to each other. The fix is not more tests on each side but *deriving*
  one side from the other so disagreement cannot be expressed.

**Verify:** `python3 -m pytest atlas/tests/unit/test_fetch.py atlas/tests/unit/test_domain.py -q`

---

## ADR-018 — Two documents agreeing is consistency, not truth

**Status:** ACCEPTED · 2026-08-24 · strengthens ADR-014(c)

### Context

ADR-014(c) added `<!--verify:file:jsonpath:value-->` tags so numeric README
claims are re-derived from an artifact instead of trusted. That caught a real
`58 passed` drift in P02, so the mechanism works.

It did not catch this one. After P03 the README said **87 passed** while the
suite had grown to **113**. The tag pointed at
`engineering/TASK_STATE.json:tests.passed`, and `TASK_STATE` *also* still said
87. Both documents were stale **by the same amount**, so they agreed, and
`readme_numbers_have_artifacts` reported PASS.

The flaw is structural, not arithmetic: **the check compared two documents to
each other, when neither is the source of truth.** The tests are. A pair of
mutually-consistent stale numbers is indistinguishable from a pair of correct
ones if you never look past them.

Worth stating plainly: this is the same failure shape as ADR-017, one layer up.
There, two *code* vocabularies described one concept and were never compared.
Here, two *documents* describe one fact and were compared only with each other.

### Decision

Add `check_declared_test_count_matches_collection`: run
`pytest --collect-only -q` and require `TASK_STATE.tests.passed` to equal the
number of tests actually collected. The gate now reaches past every document to
the code.

Collection, not execution — `make doctor` runs the suite separately, because
*existing* and *passing* are different facts and conflating them is precisely
what ADR-010 forbids.

### Negative control (ADR-010)

```
set tests.passed = 87 (the real historical staleness)
  [FAIL] declared_test_count_matches_collection
         declared 87, pytest collects 113 -- update TASK_STATE and README together
exit=1
```

Restoring 113 returns the gate to green.

### Consequences

- README and `TASK_STATE` must now be updated **together**, and the truth is
  arbitrated by pytest rather than by whichever document was edited last.
- Gate checks: 10 → **11**.
- **The rule this generalises:** a verification chain is only as strong as its
  furthest anchor. If every link is a document, the chain is circular. At least
  one link must terminate in executable reality — the code, the bytes on disk,
  or a measurement.

**Verify:** `python3 engineering/tools/gate_check.py` (and the negative control above)

---

## ADR-019 — A captured fact that nothing reads is a lost fact

**Status:** ACCEPTED · 2026-08-24 · P04 · relates to ADR-005

### Context

`Endpoint.parse` matches candidates with this regex:

```python
_HOSTPORT = re.compile(r"^\s*(?:(?P<scheme>\w+)://)?(?P<host>[^:/@\s]+):(?P<port>\d{1,5})\s*$")
```

It has a **named capture group for the scheme**, and the function body never
mentions `scheme` again. Verified directly:

```
socks5://1.2.3.4:1080  ->  1.2.3.4:1080
http://1.2.3.4:1080    ->  1.2.3.4:1080
1.2.3.4:1080           ->  1.2.3.4:1080     # indistinguishable
```

A `socks5://` prefix is the source declaring the protocol **in the candidate
itself** — strictly better evidence than the filename hint that left
`TheSpeedX/SOCKS-List/master/http.txt` labelled `ambiguous`. ADR-005 exists
because that kind of evidence is scarce, and here it was being captured and
dropped on the floor. Candidates would then be probed as HTTP, fail, and be
discarded: the exact shape of B-12, which cost 2 853 candidates.

Nothing caught it because `Endpoint` is a *host:port value object* and its own
tests correctly assert host and port. The defect is not in what `Endpoint` does;
it is that **no layer above it captured what `Endpoint` deliberately discards**.
Until P04 there was no layer above it.

### Decision

`normalize_one` splits the scheme off first and returns it as
`labelled_protocol` + `scheme_seen`, and `to_proxies` carries it into
`Proxy.labelled_protocol` while leaving `protocol` UNKNOWN.

Leaving `protocol` unset is deliberate: writing the label there would make
`Proxy.protocol_mismatch` permanently false and destroy the ability to detect a
lying source — trading a real capability for a cosmetic one.

`socks://` maps to UNKNOWN, not socks5. It is genuinely ambiguous between v4 and
v5, and ADR-005 forbids guessing.

### Negative control (ADR-010)

`test_endpoint_parse_discards_the_scheme_it_captures` pins the old behaviour, so
if someone "fixes" `Endpoint` to smuggle the scheme in, the contradiction
surfaces instead of silently changing identity semantics.
`test_labelled_protocol_is_not_written_into_protocol` fails if the label is ever
promoted to a measurement.

### Consequences

- The 616 seed candidates in `proxy.txt` arrive **label-free** — asserted, and
  itself a finding: the legacy export dropped the scheme, so that evidence is
  already gone for those.
- **The rule this generalises:** a regex group, a return value or a column that
  nothing reads is not "available for later" — it is deleted, silently, with a
  comment implying otherwise.

**Verify:** `python3 -m pytest atlas/tests/unit/test_policy.py -k scheme -q`

---

## ADR-020 — Two true numbers can make a false sentence

**Status:** ACCEPTED · 2026-08-24 · P04 · strengthens ADR-018

### Context

The legacy run left two independent records of itself:

| stream | source | n | p50 | p95 | >1500ms | >5000ms |
|---|---|---|---|---|---|---|
| A | `proxy_details.json` | **102** | 6 359.5 | 15 903 | **95.1 %** | **58.8 %** |
| B | `proxy_scraper.log` | **118** | 6 092.5 | 15 903 | **95.8 %** | **56.8 %** |

`BASELINE.json` stores both correctly, each under its own key. The defect was in
the **prose**. Six files wrote sentences of this shape:

> "p50 6 359.5 ms and p95 15 903 ms (n=102), where 95.8 % exceeded 1 500 ms"

Every number there is real and traceable. The sentence is still false: **no
single distribution has those properties.** The n=102 stream's true figure is
95.1 %.

It reached `config.yaml`, `admission.py` and `verdict.py` as the stated
justification for `max_p95_ms: 1500` — so the gate's own rationale was a splice.

Why three separate guards missed it:

- ADR-014(c) `<!--verify-->` tags check that a number **exists** in an artifact.
  95.8 does exist — under stream B.
- ADR-018 anchors counts in **executable reality**. Also satisfied: the figure is
  real, just measured over a different population.
- Both verify claims **one at a time**. The falsehood lives in the *conjunction*.

The streams share p95, max and min exactly, and differ only on p50, mean and the
two percentages. That partial agreement is what made the splice invisible.

### Decision

1. `engineering/tools/verify_baseline_streams.py` re-derives both streams and
   **asserts** all eight fields per stream, so which figure belongs to which n is
   executable rather than remembered.
2. Every citation corrected to its own stream: the n=102 figures are **95.1 %**
   and **58.8 %**.
3. `gate_check.check_no_cross_stream_splice` fails the build if `95.8` or `56.8`
   appears without naming its stream on the same line.

### Negative control (ADR-010)

```
appended to README.md: "p50 6359.5 ms (n=102) where 95.8 % exceeded 1500 ms."
  [FAIL] no_cross_stream_splice
         n=118 figures cited without naming the stream: README.md:171
```

### Consequences

- Gate checks: 11 → **12**.
- The corrected 95.1 % is *weaker* for the project's argument than the 95.8 % it
  replaces. It is also what the data says. Restating a target to flatter the
  rebuild is the H2 violation ADR-011 already refused once.
- **The rule this generalises:** verifying claims individually does not verify
  them jointly. When several figures appear in one sentence, the sentence needs a
  single provenance — otherwise each number defends itself while the claim they
  form together is unowned.

**Verify:** `python3 engineering/tools/verify_baseline_streams.py` (asserts all 16 fields)

## ADR-021 — A port may not filter on a field the domain cannot express

**Status:** ACCEPTED · 2026-08-24 · P05 · relates to ADR-017, ADR-003, ADR-004

### Context

`StorePort`, written in P01, declared:

```python
def lease(self, *, count: int, min_grade: Grade, lease_ms: int, now: datetime) -> ...
def export_text(self, path: str, *, min_grade: Grade) -> int
```

Both filter the pool by `Grade`. `Proxy` had **no `grade` field**. Verified:

```
>>> [f.name for f in dataclasses.fields(Proxy)]   # P04 state
[... 'consecutive_failures', 'total_successes', 'total_attempts',
 'first_seen', 'last_checked', 'lease_id', 'reason_code']       # no 'grade'
```

So the admission gate computed a `Grade` (P04), returned it inside a `Verdict`,
and the pool **had nowhere to put it**. `lease(min_grade=...)` was unimplementable
exactly as declared: any implementation had to either invent a grade at read time
or ignore the parameter.

This is the **ADR-017 shape a second time**, and it survived four green phase
gates for the same reason: nothing had ever *crossed* the seam. P01 declared the
port, P04 produced Grades, and no code in between ever had to persist one. A
signature that is never called is a comment with parentheses.

Three ways it could have been resolved, and why only one is honest:

| option | consequence |
|---|---|
| drop `min_grade` from the port | the pool could not distinguish an ELITE proxy from a barely-USABLE one — throws away the gate's entire output |
| recompute the grade on read | re-runs policy inside the storage layer, so a threshold change silently reinterprets history; ADR-004 says the DB is the source of truth |
| **persist the verdict** | the gate's judgement becomes a durable fact about the proxy |

### Decision

`Proxy.grade: Grade = Grade.REJECTED`, persisted in the `proxies` table under a
`CHECK` constraint, plus `Grade.rank` / `.meets()` / `.at_least()` so the SQL
filter and the policy cannot disagree about what "USABLE or better" means.

Two details that are load-bearing:

1. **The default is `REJECTED`, not a neutral value.** An unjudged proxy must
   never satisfy `lease(min_grade=USABLE)`. This is the same inversion as
   `NOT_MEASURED` in the admission gate: absence of evidence is refusal, not
   permission. `test_ungraded_proxies_are_never_leased` pins it against real SQL.

2. **`Grade.rank` is an explicit mapping, not `list(Grade).index()`.** Definition
   order is an accident of how the class was typed; deriving priority from it
   means inserting an enum member silently reorders which proxies get leased
   first. A cosmetic edit must not become a behaviour change.

`graded()` deliberately does **not** change `state`. Grading is a judgement,
admitting is a pool transition; collapsing them is how a REJECTED proxy ends up
READY.

### Negative control (ADR-010)

`test_grade_rank_does_not_depend_on_declaration_order` reads `verdict.py` and
fails if `rank` is ever re-derived from enum order.
`test_rejected_never_satisfies_any_useful_minimum` fails if `REJECTED` leaks into
any `at_least()` set.

### Consequences

- The 53 DISABLED registry rows and every unprobed candidate are `REJECTED` by
  default — correct, and it makes "how much of the pool is actually judged" a
  queryable number rather than an assumption.
- **The rule this generalises:** an interface may only name a filter the domain
  can represent. A port signature referencing a non-existent field is not a
  design intention, it is an error that the type system cannot see because nobody
  has called it yet. The remaining unimplemented ports (`ProbePort`) must be
  checked against the domain *before* they are implemented, not after.

**Verify:** `python3 -m pytest atlas/tests/unit/test_store.py -k "grade or leas" -q`

---

## ADR-022 — Atomicity is a structural property, so it must be asserted structurally

**Status:** ACCEPTED · 2026-08-24 · P05 · relates to ADR-010, H3, H8

### Context

A correct `lease()` and a broken read-then-write `lease()` are **behaviourally
identical** in every single-threaded test. Both return N distinct proxies, both
mark them LEASED, both pass any assertion about their return value. The
difference appears only under concurrency, and concurrency tests have a specific
pathology: **an ineffective one is indistinguishable from a passing one.** If the
processes never actually overlap, the test reports success and proves nothing.

Measured, on this machine — same test body, same 12-proxy pool, 6 processes each
requesting 6. Both rows come from **one controlled comparison**: identical pool,
identical process count, identical request size; the *only* variable is the lease
implementation. (Re-derive with `engineering/tools/measure_lease_concurrency.py`,
which writes `engineering/raw/lease_concurrency.json`; these two rows are its
`head_to_head` block.)

| implementation | handed out | unique | duplicates |
|---|---|---|---|
| `SqliteStore.lease` (CAS) | 12 | 12 | **0** |
| `NaiveStore.lease_naive` (read-then-write) | 36 | 6 | **30** |

Separately, and *not* to be tabulated with the above, the real store is also run
**oversubscribed** — 48 requested by 12 processes from a pool of 24 — where it
hands out 24 unique, 0 duplicates. That is a different experiment with a
different config; the first draft of this ADR put the oversubscribed real arm in
the table beside the naive arm's smaller config, which is precisely the
conjunction error ADR-020 was written about. Caught here by trying to re-derive
the table from the artifact and finding the numbers came from two runs.

### Decision

H3/H8 are established by **three independent mechanisms**, because no single one
is sufficient:

1. **Real concurrency** — `multiprocessing` with `spawn`, separate processes and
   separate sqlite connections. Not threads: the GIL can hide a race that
   separate processes expose. One parametrisation deliberately **over-subscribes**
   the pool (48 requested, 24 available), because contention is where
   read-then-write breaks.

2. **A negative control that must fail** — `NaiveStore` is committed, deliberately
   wrong code whose only purpose is to be caught. `test_the_h3_test_would_catch_a_
   read_then_write_store` asserts duplicates > 0 against it. If that assertion
   ever stops holding, the H3 test above is not exercising concurrency and its
   green result is meaningless.

3. **An independent audit** — the append-only `lease_log` table plus
   `double_delivery_violations()`, which reconstructs the LEASE/RELEASE/EXPIRE
   sequence per fingerprint. This does not ask the leasing code whether it
   behaved; it examines what happened. A test that only inspects `lease()`'s
   return value is asking the accused to testify.

Plus **AST guards** on structure, since behaviour cannot distinguish the two
implementations: the claiming `UPDATE` must re-check `state='READY'` in its own
`WHERE`, every mutating method must use `BEGIN IMMEDIATE` (a `DEFERRED`
transaction upgrades to a write lock only on first write — the very window being
closed), and `export_text` must `fsync` *before* `os.replace`.

For H8, the child process ends with `os.kill(os.getpid(), signal.SIGKILL)` and
the test asserts `returncode == -9`. Signal 9 cannot be caught, so no `finally`,
no `atexit` and no context manager runs — otherwise the test would be measuring
my own shutdown code rather than durability.

### The defect this decision immediately caught

The structural guard for `export_text` **failed on its first run** — and was
right to. It searched the method for `os.fsync` and `os.replace(`, and matched
the **docstring**, which names both while explaining them. The code was correct;
the guard was reading prose. This is the third instance of the same class in this
project (P03: an offline-guard that matched its own list of banned strings). The
helper now strips the docstring via AST before searching, and
`test_the_structural_guards_read_code_not_comments` fails if it ever leaks back.

A guard satisfied by a comment describing the behaviour is worse than no guard:
it reports that the mechanism exists.

### Consequences

- `atlas/tests/integration/naive_store.py` is intentionally-wrong code in the
  repository. It is documented as such at the top of the file so no one "fixes"
  it, which would silently disarm the control.
- `make test-integration` becomes meaningful, and `make doctor` now runs it.
- **The rule this generalises:** when correctness is a property of *structure*
  rather than *output*, tests over output cannot establish it. Assert the
  structure, and prove the assertion has teeth by running it against something
  known to be broken.

**Verify:** `python3 -m pytest atlas/tests/integration -q`

---

## ADR-023 — A guard must read code; the third instance of one defect class

**Status:** ACCEPTED · 2026-08-24 · P06 · relates to ADR-010, ADR-012, ADR-022, H1

### Context

`test_no_tls_verification_disabled` (B-09; legacy disabled TLS in 9 places) was a
line-level regex over every `atlas/**/*.py`:

```python
banned = re.compile(r"verify\s*=\s*False|disable_warnings|CERT_NONE|...")
```

The first honest ProbePort implementation failed it — on two lines of its own
documentation:

| line | text | verdict |
|---|---|---|
| 15 | `* It set verify=False in 9 places (B-09), which makes a MITM proxy` | module docstring explaining the defect being avoided |
| 129 | `# No verify=False switch is exposed as a convenience.` | comment stating the prohibition |

Both were prose **forbidding** the thing the guard exists to forbid. Meanwhile the
actual security-relevant code — `aiohttp.TCPConnector(ssl=self._verify_tls)` —
was invisible to the pattern, since a variable named `ssl` set to a variable does
not match a literal `False`.

**This is the third occurrence of one defect class in this project:**

| phase | guard | matched |
|---|---|---|
| P03 | offline-guard | its own list of banned strings |
| P05 | `export_text` fsync guard | the docstring naming `fsync`/`os.replace` |
| P06 | TLS guard | prose forbidding `verify=False` |

ADR-012 fixed the *target-host* guard this way, and ADR-022 stated the principle,
but neither swept the remaining line-based guards. Recurrence three times means
the earlier fixes were point repairs, not the general lesson.

### Decision

`scan_tls_disabled()` parses the AST and reports only constructs that can cause an
insecure connection: keyword `verify=`/`check_hostname=`/`ssl=` bound to the
constant `False`, an `ssl.CERT_NONE` attribute, or a `disable_warnings()` call.
Comments and docstrings are invisible **by construction** — not by an exclusion
list, which is itself a thing that can be forgotten.

The guard is proven in both directions, because a guard that cannot fail is not
evidence (ADR-010):

| snippet | caught |
|---|---|
| `requests.get(u, verify=False)` | **yes** |
| `aiohttp.TCPConnector(ssl=False)` | **yes** |
| `ssl_ctx.verify_mode = ssl.CERT_NONE` | **yes** |
| `urllib3.disable_warnings()` | **yes** |
| `requests.get(u, verify=True)` | no |
| `aiohttp.TCPConnector(ssl=self._verify_tls)` | no |

`test_tls_guard_ignores_prose_that_forbids_the_thing` pins the exact text that
broke the old version. And the guard was verified against a **real** injection:
changing the live adapter to `ssl=False` fails the build at
`adapters/probe_aiohttp.py:169`, then passes again when reverted. Snippet tests
alone would not have shown that it works on the real file.

### Consequences

- The guard is now strictly stronger: it catches `ssl=False`, which the regex
  never could, and stops flagging documentation.
- **The rule this generalises:** a guard over source code must operate on the
  *executable* AST. If a guard can be triggered by a sentence describing the
  defect, it will eventually be triggered by the sentence that documents the fix
  — and the cheapest way to make the build green is to delete the documentation.
- Remaining line-based guards are grandfathered only where the pattern cannot
  appear in prose; each new one must carry teeth tests in both directions.

**Verify:** `python3 -m pytest atlas/tests/unit/test_architecture.py -q` (29 tests)

---

## ADR-024 — A "95th percentile" that returns the minimum

**Status:** ACCEPTED · 2026-08-24 · P06 · relates to ADR-011, ADR-003, H2, H7

### Context

The first honest live calibration run (after the SOCKS-masking fix below) printed
a record that cannot be true:

```json
{ "endpoint": "103.130.61.61:8081",
  "samples_ms": [7659.2, 4100.7],
  "p50_ms": 5880.0,
  "p95_ms": 4100.7,          <-- tail BELOW the median
  "reason": "UNRELIABLE" }
```

A 95th percentile below the 50th is arithmetically impossible. The cause is the
ADR-011 floor rank, `sorted[int((n-1)*p/100)]`, at `p=95`:

| k | index | which sample |
|---|---|---|
| 1 | 0 | the only sample — honest |
| **2** | **0** | **the MINIMUM — the faster of the two** |
| 3 | 1 | in the tail |
| 102 | 95 | in the tail |
| 118 | 111 | in the tail |

At `k=2` the estimator reports the **best** case under the name of the worst.
A randomised check violated the `p95 >= p50` ordering **4000/4000 times at n=2
and 0/4000 at every other n** — the pathology is exactly `k=2`.

`k=2` is not hypothetical: it is the normal outcome of ADR-003's own early-stop
rule, which abandons sampling after consecutive failures. The live sweep produced
6 proxies with 2+ samples, and `with_2plus_samples` is where every k>1 rule lives.

**This is not a cosmetic reporting error — it is a false ADMIT.** Samples
`(1400ms, 1600ms)` against the 1500 ms ceiling:

| statistic | value | rule |
|---|---|---|
| p95 (floor rank) | 1400.0 | passes `max_p95_ms=1500` |
| jitter | 0.094 | passes `max_jitter=0.5` |
| success_ratio | 1.0 | passes `min_success_ratio=0.6` |
| **verdict** | **OK / USABLE** | **admitted** |

One request was *measured* over budget and the gate admitted it anyway. The gate
whose entire purpose is rejecting the legacy system's slow proxies would admit a
proxy it had itself measured too slow — H7's failure mode, reintroduced through
the *estimator* rather than the threshold. Every unit test passed throughout,
because they all use k=5.

### Decision

Split the two jobs the one function was doing:

- **`pct_floor`** — FROZEN, unchanged, for **baseline parity only**. ADR-011
  requires v4's p95 to be computed by the same function as the legacy figure, and
  that requirement is about comparability, not correctness.
- **`pct_tail`** — the estimator the **admission gate** uses. Identical to
  `pct_floor` for all `k >= 3`; at `k == 2` it returns the **upper** sample,
  which is the only defensible tail estimate from two observations.

`build_profile` now calls `pct_tail`. Parity is untouched: the anchors are n=102
and n=118, where the floor index is 95 and 111, nowhere near the k=2 case —
asserted directly by `test_pct_tail_preserves_legacy_parity_for_every_k_above_two`.

### Consequences

- Re-running the sweep with the fix: **3 admitted** (was 1), `TOO_SLOW_P95` now
  fires on live data (2), and **zero `p95 < p50` records** remain in the artifact.
  Artifact: `engineering/raw/admission_live_adr024.json`.
- Four regression tests, teeth proven by injection: deleting the k=2 branch fails
  exactly 3 of them, including the false-admit case. A fifth
  (`test_a_genuinely_fast_k2_proxy_is_still_admitted`) fails if the fix
  over-corrects into rejecting everything at k=2 — returning `+inf` would satisfy
  the others.
- **The rule this generalises:** a statistic borrowed for *comparability* must not
  be reused for *decisions* without re-deriving its behaviour at the sample sizes
  the decision path actually produces. ADR-011 pinned the legacy method for
  honest comparison and was right to; the error was letting the gate inherit it.
  Small-k degenerate behaviour is invisible to tests written at the happy-path k.
- The defect was found by an **artifact**, not by a test — the same way ADR-020's
  splice was. Unit tests at k=5 could not see it; a printed number that contradicted
  arithmetic could.

**Verify:** `python3 -m pytest atlas/tests/unit/test_policy.py -q` (54 tests)

---

## ADR-025 — A measured cause outranks a note about something never attempted

**Status:** ACCEPTED · 2026-08-24 · P06 · relates to ADR-005, B-02, H2

### Context

The first live calibration sweep reported this, and it is nonsense:

```
tcp ok        : 24
reached gate  : 0
top reasons   : {'PROTO_MISMATCH': 24, 'TCP_TIMEOUT': 16}
```

All 24 endpoints that *completed a TCP handshake* were then filed as
`PROTO_MISMATCH` with the detail `socks4 not testable: aiohttp lacks SOCKS`.
Zero reached the gate, so the entire point of the run — exercising the k>1 rules —
produced no data at all.

`discover_protocol` walks a ladder of candidate protocols and keeps the last
failure in a single variable:

```python
for protocol in ladder:
    if scheme in ("socks4", "socks5"):
        last = ProbeResult(ok=False, reason=PROTO_MISMATCH,
                           detail=f"{protocol.value} not testable: ...")
        continue                     # <-- overwrote the real result
    result, _ = await self._request(...)
    last = result
return last
```

SOCKS sits at the **end** of the ladder. So for every proxy, the honest
"I could not test this rung" placeholder was written *after* the real HTTP
measurement and returned in its place. A connection that was **refused**, or a
server that answered **500**, was reported as a note about a protocol that was
never attempted.

Both facts were individually true. The bug was in which one *survived*.

That is **BUG_LEDGER B-02** — losing the cause at the point of discovery — and it
appeared *inside the code written to avoid B-02*. The placeholder itself was added
for an honest reason (refusing to fabricate a negative for an untested protocol,
H2); it destroyed the evidence it was meant to sit beside.

### Decision

Two facts of different kinds get two variables:

| variable | meaning |
|---|---|
| `last_tested` | something actually **measured** failing |
| `untested` | a rung that could not be attempted at all |

`return last_tested or untested or <no protocol succeeded>` — **a measurement
always outranks an untested rung**, and the untestable note survives only when
nothing could be measured. It is also recorded once (`if untested is None`) rather
than overwritten per rung.

### Consequences

- The same sweep, rerun: `PROTO_MISMATCH: 24` became
  `BAD_STATUS: 31, PROXY_AUTH_REQUIRED: 19, TCP_REFUSED: 44, TLS_FAILED: 1` and
  **14 proxies reached the gate** (previously 0). The reasons were always there;
  they were being discarded on the way out.
- Directly unblocked P06: with real causes surfacing, `UNRELIABLE` and
  `NOT_MEASURED` fired on live data for the first time, and the `p95 < p50`
  record that exposed ADR-024 became visible. **One masking bug was hiding
  another defect entirely.**
- Two regression tests: `test_a_measured_failure_outranks_an_untestable_socks_rung`
  (500 through the full ladder must report `BAD_STATUS`, not "not testable") and
  `test_a_refused_endpoint_is_not_reported_as_untestable_socks` (the live symptom,
  reduced). `test_socks_is_reported_untestable_rather_than_failed` still passes,
  so the H2 honesty property is preserved rather than traded away.
- **The rule this generalises:** when one variable accumulates results of
  different epistemic status — measured vs. not-attempted vs. not-applicable —
  loop order silently decides which one the caller sees. Rank them explicitly, or
  the last iteration wins by accident. "No evidence" must never overwrite
  "evidence".

**Verify:** `python3 -m pytest atlas/tests/unit/test_probe.py -q` (28 tests)
