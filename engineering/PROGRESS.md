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

I did not accept either reading. A third, direct `curl` returned **230 067 octets of valid JSON →
500 unique proxies** (`tools/verify_geonode_parser.py` re-used the *same* parser to prove it).

> **Unit correction (ADR-015, 2026-08-24):** this line originally read "230 019 bytes". That
> figure was `len()` of the *decoded string* — a character count. The stored evidence is
> **230 067 octets** / **230 019 characters** (45 non-ASCII). `wc -c` confirms 230 067. The
> proxy count (500) and the verdict (`ALIVE_JSON`) are unaffected.

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

---

## 2026-08-24 — P01 · resumed, and found my own documentation running ahead of the code

**AR:** عند الاستئناف، وجدت أن التوثيق يسبق التنفيذ. صححت الأرقام من الأدلة، لا العكس.
**EN:** On resume, the documents claimed work that did not exist. Corrected the
numbers from the artifacts — never the artifacts to match the numbers.

`make doctor` and `pytest -q` contradicted the repository's own README:

| claimed | actual |
|---|---|
| "19 passed" | **2 failed, 10 passed** |
| "68 ACTIVE of 123" | artifact said **61** |
| ADR-012 implemented | guards still line-regex |
| ADR-013 implemented | `resp.content.read(BODY_CAP)` still at line 179 |

A second sync loss had also removed `reprobe_empty.py` and `verify_geonode_parser.py`
while `TASK_STATE.json` still marked P00.T4/T5 `DONE`. Recorded in
**RECONCILIATION.md §5** rather than quietly fixed: an ADR describing a fix that
does not exist is a fabricated claim.

### ADR-012 implemented — guards that can prove they still bite

Two fitness tests were failing on *legitimate* code: `__all__` (import machinery,
must be a list) and a **docstring** in `source.py:131` documenting the refusal to
default to a login-walled target — which `SECURITY.md` *requires*. The tempting
fixes (delete the sentence, loosen the regex) would each have turned the test green
by removing real protection.

Instead the scanners were extracted as callables, scoped to **executable** string
values via AST, and given **7 negative controls** (`module_const`, `default_arg`,
`list_item`, `dict_value`, `fstring`, `nested_call`, `lowercase_var`) that feed
known-bad source and assert the guard still fires. → 12 → **22 passing**

### ADR-013 implemented — the defect was far larger than the ADR described

Per ADR-013(e) the parser was validated against **stored** evidence before any live
read: `geonode_body.txt` (230 067 octets / 230 019 chars — see ADR-015; earlier
drafts of this file called the char count "B") → exactly **500** unique proxies,
`regex_adjacent` → 0. A parser proven correct on stored bytes means a live zero
implicates the *fetch*.

It did. `resp.content.read(n)` returns only what is **currently buffered**. Reading
to EOF with `iter_chunked()` changed the harvest from the same 120 URLs:

| | before | after |
|---|---|---|
| unique candidates | 74 895 | **502 189** (**×6.7**) |
| ACTIVE sources | 61 | **67** |
| `TRULY_EMPTY` | 20 | **14** |
| GeoNode API | 0 | **500** |

Large lists were cut off mid-body while a regex still matched proxies in the
surviving prefix — so every source looked alive and nothing signalled the loss.
**The worst defect found so far is in our own tooling, not the legacy code.**

Then `short_read` itself proved unsound: aiohttp auto-decompresses, so
`Content-Length` is the *compressed* size for **83 of 120** URLs
(`TheSpeedX/http.txt`: declared 20 360, decoded 54 284). Now applied only to
identity-encoded responses, with `length_comparable` recorded. → **§7**

### P01.T3 — domain tests found a real bug

36 behaviour tests. One failed immediately: `Endpoint.parse` **accepted**
`1.2.3.4.5:80` even though the code comment claimed that exact string was rejected.
The hostname fallback allowed all-numeric labels. Fixed by refusing a numeric
rightmost label. **A comment claimed the guarantee; only the test checked it.**

### ADR-014 — the process fix

`gate_check.py` verified task *evidence* but not ADR *prose*. It now enforces
`adr_claims_are_verifiable` (every implementation-claiming ADR carries a
`**Verify:**` command) and `readme_numbers_have_artifacts` (tagged README numbers
re-derived from JSON). Both negative-controlled: injecting `active:99` makes the
gate fail as designed.

### PHASE GATE 1 · **PASSED**

`make doctor` → **10/10 gate checks PASS**, **58 tests pass**
(22 architecture/fitness + 36 domain behaviour). Every `**Verify:**` command in
`DECISIONS.md` executes successfully.

Honest invariant status: H1 ✅ (re-earned) · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ ·
H3 / H7 / H8 still `false` — no pool, no admission gate, no storage yet.

**NEXT:** P02 — generate `atlas/data/sources/sources.json` from the **pinned**
snapshot `source_probe_20260824T010038Z.json` (67 ACTIVE rows, each with
url/parser/labelled_protocol/state), plus a loader test asserting zero hardcoded
URLs in `.py` and that every DISABLED row names a reason.

---

## 2026-08-24 — P02 · sources become data, and three defects in my own verifiers

**AR:** الغموض يُعلن، لا يُخمَّن. والأدوات التي تتحقق من العمل هي أقل ما تم اختباره.
**EN:** Ambiguity is declared, not guessed. And the tools that *check* the work are
the least-tested code in the repository.

### ADR-015 — a field named `bytes` held characters

`probe_legacy_sources.py` recorded `"bytes": len(body)` where `body` is a **decoded
`str`**. That is a character count. Proven offline from stored evidence:

| `geonode_body.txt` | value |
|---|---|
| octets (`wc -c`) | **230 067** |
| characters | **230 019** |
| non-ASCII chars | 45 |

Across the 83 snapshot rows carrying both fields: `chars < octets` 28 times, equal
55, and `chars > octets` **0 times** — zero counterexamples, so multi-byte decoding
is *established* as the cause, not guessed.

My first `grep '"bytes"'` found two sites. The new **AST-based** `verify_units.py`
found a third that grep missed: `EXPECTED_BYTES = 230019` in
`verify_geonode_parser.py` — **the origin of the "230 019 B" figure I had repeated
in PROGRESS.md and README.md**. Corrected in place *with a visible footnote stating
the old number and why it was wrong*, rather than silently rewriting history.
GeoNode still parses to 500 / `ALIVE_JSON`: units fixed, conclusions unmoved.

### P02 — the registry

`atlas/data/sources/sources.json`, generated offline and **byte-identical on
rebuild** (verified with `diff`):

| | |
|---|---|
| rows | **120** |
| ENABLED | **67** — asserted `==` snapshot ACTIVE by test |
| DISABLED | **53** — every one names a reason |
| parsers | regex_adjacent 59 · html_table 7 · json_path 1 |
| labels verified | **0** (nothing probed yet, ADR-005) |

Protocol labelling exposed two genuine ambiguities that a first-match-wins scan
gets wrong, both now regression-tested:

1. `TheSpeedX/SOCKS-List/master/http.txt` — repo says SOCKS, filename says http.
   Reported **`ambiguous`**. Picking either would fabricate a fact.
2. `?proxytype=http&…&ssl=yes` — `ssl` is a capability **filter**, not a protocol.
   My own first version labelled this `https`, *contradicting the explicit
   `proxytype=http`*. Query params now outrank free path text.

The loader is strict: **14 parametrized rejection cases** plus a *positive* control,
so the rejection suite cannot pass a loader that rejects everything.

The ADR-002 "no hardcoded URLs" guard first fired on `Source.url`'s legitimate
`startswith(("http://", "https://"))`. Suppressing it would have removed a real
protection; instead the guard now requires a host after the scheme, with a negative
control pinning both directions.

### ADR-016 — evidence citing a test never checked the test

`done_tasks_have_evidence` did `(ROOT / ev).exists()` on `path::test_name`, asking
the filesystem for a file with `::` in its name. Latent through P00/P01 (plain paths
only); **P02 was the first phase to cite individual test functions.** It surfaced as
a false *failure* — but the same defect would let a task cite a test that does not
exist. Now the symbol is verified, and injecting `::test_this_does_not_exist`
produces `exit=1`.

**Three self-inflicted verification defects in two sessions** (ADR-013 truncated
fetch, ADR-015 wrong units, ADR-016 unresolved symbols) — every one in the machinery
that *checks* the work, none in the work itself.

### PHASE GATE 2 · **PASSED**

`make doctor` → **10/10 gate checks PASS**, **87 tests pass** (22 architecture +
36 domain + 29 registry). The gate caught a stale `58 passed` README claim during
this phase — exactly the drift the ADR-014c tags exist for.

Honest invariant status: H1 ✅ · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ · H3/H7/H8 still
`false` — no pool, no admission gate, no storage.

**NEXT:** P03 — SourcePort fetch adapter using the ADR-013 read-to-EOF discipline
(`iter_chunked` + `FETCH_INCOMPLETE`), the three parsers behind one interface,
tested **offline** against stored bodies in `engineering/raw/` so no test depends
on the live network.

---

## 2026-08-24 — P03 · the fetch that cannot lie about a body, and a seam nobody had crossed

**AR:** أخطر عيب ليس في مكوّن، بل في وصلة لم يعبرها أحد.
**EN:** The most dangerous defect is not inside a component; it is at a seam that no
code has crossed yet.

### ADR-017 — one concept, two vocabularies, never introduced

`ParserKind` (hand-written, P01) said `line_ipport / json_path / csv_columns /
html_table / regex`. The registry (**generated from measured probe data**, P02)
says `regex_adjacent / json_path / html_table`.

| | |
|---|---|
| enum members with no implementation | `csv_columns`, `regex` |
| enum members no source uses | `line_ipport` |
| registry value the enum couldn't represent | **`regex_adjacent`** |
| ENABLED rows affected | **59 of 67 — 88%** |

Two green phase gates missed this because **nothing had ever converted a
`SourceRow` into a `Source`**, so `ParserKind(row.parser)` had no execution path
on which to fail. P03 is the first phase that must cross that seam.

The pinning test looked adequate and was not: `{...} <= kinds` is a **subset**
assertion, and cannot detect a vocabulary that is simultaneously too large *and*
missing the one member that matters. It now asserts `==`, and `VALID_PARSERS` is
**derived** from the enum rather than re-typed, so disagreement is no longer
expressible.

### The fetch discipline (ADR-013), now in production code

`read_to_eof` + `verify_complete`, then parse — never the reverse. Proven against
the stored 230 067-octet GeoNode body:

| | |
|---|---|
| `read(n)` on a 74 241-octet buffer | 74 241 octets → **0 candidates** |
| `iter_chunked` to EOF | **230 067 octets → 500 candidates** |

Both directions are tested: a fixture reproduces the truncating `read(n)` *and*
asserts the truncated JSON genuinely yields zero — otherwise the fix would only
prove my fake is self-consistent.

**Three facts, three codes**, no longer collapsible:
`FETCH_INCOMPLETE` (our fault, says nothing about the source) ·
`SOURCE_THROTTLED` (200 OK, tiny body — the 659-octet signature) ·
`PARSE_EMPTY` (**intact** body, genuinely nothing there — the only one that is
evidence about the source).

One deliberate refusal: the adapter parses with the **declared** parser only. On
the GeoNode body with `regex_adjacent` declared it returns `PARSE_EMPTY`, *not* a
silent rescue by `json_path`. Falling back would hide a wrong declaration, and
per-source attribution is what makes a bad source diagnosable.

One near-miss avoided: comparing `Content-Length` to a decompressed body would
raise a **false** `FETCH_INCOMPLETE` on every gzip response. `Content-Encoding`
is now treated as *incomparable* — honestly unverifiable, not "verified".

### Two errors of mine, corrected rather than hidden

1. A test asserted `parser=None` rows convert to Sources. The registry disagreed:
   **37 of 53** DISABLED rows have no parser — nothing ever parsed them, which is
   *why* they're disabled. I corrected the test, not the model.
2. My offline-guarantee guard scanned the file for banned strings and so matched
   **its own list of banned strings** — failing on itself while proving nothing
   about the other 25 tests. Rewritten over AST imports.

### PHASE GATE 3 · **PASSED**

`make doctor` → **10/10 gate checks PASS**, **113 tests** (up from 87). Negative
controls: injecting `import aiohttp` fails the offline guard; adding
`CSV_COLUMNS` fails the implementation guard; both restore green.

Honest invariant status: H1 ✅ · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ ·
**H3/H7/H8 still `false`** — candidates can now be fetched and parsed, but
nothing measures or admits them.

**NEXT:** P04 — pool + admission gate (H3/H7). Admission must refuse on zero
evidence (`NOT_MEASURED`) rather than defaulting to admit.

---

## 2026-08-24 — P04 · the gate itself, and two defects found in my own evidence

**AR:** رقمان صحيحان قد يصنعان جملة كاذبة.
**EN:** Two true numbers can make a false sentence.

### THE GATE (H7 / ADR-003) — `atlas/core/policy/admission.py`

Pure, no I/O, 260 lines. p95 of *k* samples — never one sample, never `min()`
(its absence is asserted by AST, not by comment). Four rules in a **fixed order**,
so the reason code is diagnostic rather than merely negative:

| order | rule | reason code |
|---|---|---|
| 1 | zero evidence | `NOT_MEASURED` — a **refusal**, not a default-admit |
| 2 | integrity (IP leak / body rewrite) | `TRANSPARENT_LEAK` · `CONTENT_MISMATCH` |
| 3 | success ratio below floor | `UNRELIABLE` |
| 4 | p95 over budget, then jitter | `TOO_SLOW_P95` · `TOO_JITTERY` |

Integrity outranks speed deliberately: a 200 ms proxy that forwards the client IP
is rejected *before* latency is considered. Speed is worthless if the proxy leaks.

### Replay against the legacy system's own output

Not a synthetic benchmark — `proxy_details.json`, the 102 proxies the legacy run
**itself declared working**:

| | |
|---|---|
| legacy-admitted | **102** |
| v4 admits | **5** |
| v4 rejects | **97 (95.1%)** — all `TOO_SLOW_P95` |
| survivors | p50 **1199 ms** · p95 **1329 ms** |
| legacy distribution | p50 6359.5 ms · p95 15903 ms · max 19035 ms |

**Caveat stated in the artifact, not buried:** k=1, because the legacy file
records one sample per proxy. This tests the **threshold only** — jitter and
reliability are unmeasurable at n=1 — so the replay is *generous* to the legacy
data. The most flattering reading available still rejects 95.1%.

### ADR-019 — a captured fact that nothing reads is a lost fact

`_HOSTPORT` has a named capture group for the scheme, and `Endpoint.parse` never
read it: `socks5://1.2.3.4:1080` parsed **identically** to `1.2.3.4:1080`. That
prefix is the source declaring its protocol *in the candidate itself* — stronger
evidence than the filename hint ADR-005 was written for — and it was being
discarded. Same shape as B-12, which cost 2853 candidates. The normalizer now
keeps it as `labelled_protocol` while leaving `protocol` UNKNOWN, so
`protocol_mismatch` can still catch a source that lies.

### ADR-020 — two true numbers can make a false sentence

The legacy run left **two** records: `proxy_details.json` (n=102) and
`proxy_scraper.log` (n=118). "95.8% over 1500ms" and "56.8% over 5000ms" belong
to the **n=118** stream. Six files — including `config.yaml` and `admission.py`,
as the stated justification for `max_p95_ms: 1500` — quoted that pair beside the
**n=102** p50/p95. Every number was real; no single distribution has those
properties.

| stream | n | over 1500ms | over 5000ms |
|---|---|---|---|
| `proxy_details.json` | 102 | **95.1%** | **58.8%** |
| `proxy_scraper.log` | 118 | 95.8% | 56.8% |

ADR-014(c) and ADR-018 both passed it because each verifies claims **one at a
time**, and the falsehood lived in the *conjunction*. Corrected in all six files.
Note the direction: the honest figure (95.1) is **weaker** for this project's
argument than the spliced one it replaces.

New guard `check_no_cross_stream_splice` (negative-controlled) fails the build if
95.8/56.8 ever again appears without naming its n=118 stream.
`verify_baseline_streams.py` re-derives all 16 fields from both raw files.

### PHASE GATE 4 · **PASSED**

`make doctor` → **12/12 gate checks PASS** (was 10), **162 tests** (was 113).
The normalizer accepts all 616 real seed candidates; the accounting invariant
`accepted + dropped == seen` is enforced by the report type itself, with 13 named
drop reasons.

Honest invariant status: H1 ✅ · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ ·
**H7 ✅ (with the k=1 caveat recorded)** · **H3/H8 still `false`** — the pool can
be populated and judged, but nothing *persists* it.

**NEXT:** P05 — STORE + LEASE (H3/H8). SQLite WAL, `lease()` as a single
`BEGIN IMMEDIATE` compare-and-set, atomic `.tmp`+`os.replace` exports. Leasing
must be proven under **real concurrency**, not asserted.

---

## 2026-08-24 — P05 · the pool persists, and a splice I had written myself

**AR:** الصفر لا يعني شيئًا حتى تُثبت أن أداتك تستطيع رؤية غير الصفر.
**EN:** A zero means nothing until you prove your instrument can see non-zero.

### STORE + LEASE (H3 / H8) — `atlas/adapters/store_sqlite.py`

SQLite in WAL, `synchronous=FULL`. `lease()` is **one** `BEGIN IMMEDIATE` plus a
single `UPDATE … RETURNING` whose `WHERE` re-checks `state='READY'`. The write
lock is taken at `BEGIN`, not on first write — a `DEFERRED` transaction upgrades
lazily, and that upgrade window *is* the race.

### Why four mechanisms, not one

A correct `lease()` and a broken read-then-write `lease()` are **behaviourally
identical** in every single-threaded test. Worse, a concurrency test that never
actually creates contention passes and proves nothing — an ineffective test is
indistinguishable from a passing one. So:

1. **Real concurrency** — `multiprocessing`/`spawn`, separate processes and
   connections (threads let the GIL hide the race). One case oversubscribes:
   48 requested from a pool of 24.
2. **A committed negative control** — `NaiveStore`, deliberately wrong code whose
   only job is to be caught.
3. **An independent audit** — append-only `lease_log` +
   `double_delivery_violations()`, which reconstructs what happened instead of
   asking `lease()` to report on itself.
4. **AST guards** — because the difference is structural, not observable.

Head-to-head, **identical config** (pool 12, 6 procs × 6):

| implementation | handed out | unique | duplicates |
|---|---|---|---|
| `SqliteStore.lease` (CAS) | 12 | 12 | **0** |
| `NaiveStore.lease_naive` | 36 | 6 | **30** |

Oversubscribed (pool 24, 12 procs × 4 = 48 requested): **24 unique, 0 duplicates.**

For H8 the child ends with `os.kill(os.getpid(), SIGKILL)` and the parent asserts
`returncode == -9`. Signal 9 is uncatchable, so no `finally`, no `atexit`, no
`__exit__` runs — otherwise the test would be measuring my own shutdown code
instead of durability.

### Three claims that did not survive re-derivation

`make doctor` was **14/14 green with 204 tests passing** when I resumed. All
three defects below sat *behind* that green.

**(a) ADR-022's own table was a splice — ADR-020, recurring.** It read "same
12-proxy pool, 6 processes each requesting 6", but the real arm's `12 / 12 / 0`
came from the *oversubscribed* run (pool 24, 12 procs) while the naive arm's
`36 / 6 / 30` came from pool 12. Both rows were real measurements; no single
experiment produced both. Found by trying to re-derive the table from the
artifact and discovering the numbers came from two runs. The tool now runs a
**matched** arm, so the only variable is the implementation.

**(b) `make sources-audit` could not execute.** It invoked
`engineering/tools/reprobe_empty.py`, deleted by a sync several phases ago. No
gate caught it because no gate ever *ran* that target — a Makefile recipe is
invisible to both the import graph and pytest. New check
`makefile_tools_exist`; teeth proven by injecting a bogus reference (FAIL) then
restoring (PASS).

**(c) ADR-022 claimed `make doctor` runs the integration tests.** True — but only
as an unasserted side effect of pytest discovery. One `testpaths` line would have
silently dropped the H3/H8 evidence while every gate stayed green: the exact
vacuous-pass shape of ADR-010. `check_test_scope.py` now *measures* the default
collection (195 unit + 9 integration = 204) inside `make gate`.

The pattern in all three: **the green was real, and the claims behind it were
not.** A passing suite tells you the assertions you wrote hold — not that they
mean what your prose says.

### PHASE GATE 5 · **PASSED**

`make doctor` → **15/15 gate checks**, **204 tests** (195 unit + 9 integration).

Invariants: H1 ✅ · H2 ✅ · H4 ✅ · H5 ✅ · H6 ✅ · H7 ✅ (k=1 caveat) ·
**H3 ✅ NEW** · **H8 ✅ NEW**. All eight now hold, H7 with its caveat recorded.

**NEXT:** P06 — PROBE + LIVE CALIBRATION. The gate's replay is k=1, so it tests
the *threshold* only; jitter and reliability have never been measured live.
Build the real `ProbePort` (k=5, integrity checks for `TRANSPARENT_LEAK` /
`CONTENT_MISMATCH`) and produce the first artifact in which `label_is_verified`
can become true. Registry currently reports `labels_verified: 0`.

---

## P06 — PROBE + LIVE CALIBRATION · gate **PASSED**

**Goal:** retire the H7 `k=1` caveat. The P04 replay proved the gate rejects 97 of
the legacy system's own 102 admitted proxies, but every legacy row carries one
latency sample, so only the *threshold* was ever exercised. Three of the gate's
rules — `UNRELIABLE`, `TOO_JITTERY`, and the integrity checks — had never run
against real data.

### Result

`make doctor` → **16/16 gate checks**, **244 tests** (235 unit + 9 integration).

Live sweep (`engineering/raw/admission_live_adr024.json`), k=5, TLS verification ON,
300 candidates drawn from 14 of 67 enabled registry sources:

| stage | count |
|---|---|
| probed | 300 |
| TCP ok | 86 |
| reached the gate | 12 |
| ≥2 samples | 6 |
| **ADMITTED** | **3** (p95 846.5 / 863.7 / 940.2 ms, all GOOD) |

Rejections, every one naming its cause: `TCP_TIMEOUT` 194, `TCP_REFUSED` 43,
`BAD_STATUS` 28, `PROXY_AUTH_REQUIRED` 23, `NOT_MEASURED` 5, `UNRELIABLE` 2,
`TOO_SLOW_P95` 2.

**H7 upgraded.** `UNRELIABLE` and `NOT_MEASURED` fired on live data for the first
time. Stated plainly: **`TOO_JITTERY` still has not** — 0 of 6 multi-sample
proxies tripped it, so that rule remains unit-tested only, and I am recording that
rather than letting "the k>1 rules were exercised" imply all of them.

### Two defects v4 introduced, both behind a green suite

Neither was found by a failing test. Both were found by **reading an artifact and
noticing the numbers could not be true.** The suite was 239-green throughout.

**V4-01 / ADR-025 — a measured cause overwritten by a note about something never tested.**
The first sweep reported `tcp_ok: 24, reached_gate: 0, PROTO_MISMATCH: 24`. Every
endpoint that completed a TCP handshake was filed as `socks4 not testable`.
`discover_protocol` kept the last failure in one variable, and since SOCKS sits at
the *end* of the ladder, the honest "cannot test this rung" placeholder overwrote
the real HTTP measurement every time. A **refused connection** was reported as a
note about a protocol never attempted.

Both facts were true. The bug was in which one *survived*. That is **B-02 —
cause lost at the point of discovery — recurring inside the code written to avoid
B-02**, and the placeholder had been added for an honest reason (refusing to
fabricate a negative, H2). Fix: rank the two facts, `last_tested or untested`.
A measurement always outranks an untested rung. Afterwards the real causes
appeared and 14 proxies reached the gate.

**V4-02 / ADR-024 — a "95th percentile" that returned the minimum.**
With real reasons finally surfacing, the next artifact contained:

```json
{"samples_ms": [7659.2, 4100.7], "p50_ms": 5880.0, "p95_ms": 4100.7}
```

A tail *below* the median is arithmetically impossible. Cause: the ADR-011 floor
rank, `int((n-1)*0.95)`, is **0 at n=2** — the p95 of two samples was the faster
one. Randomised check: ordering violated **4000/4000 at n=2, 0/4000 at every other
n**. And k=2 is not exotic; it is the ordinary outcome of ADR-003's own early-stop
rule.

This was **not cosmetic — it was a false ADMIT.** Samples `(1400ms, 1600ms)`
against the 1500 ms ceiling: p95 reads 1400 → passes; jitter 0.09 → passes;
success_ratio 1.0 → passes. Verdict **OK / USABLE**. The gate whose entire purpose
is rejecting slow proxies admitted one it had itself measured over budget — H7's
own failure mode, reintroduced through the *estimator* rather than the threshold.
Invisible to every unit test, because they all use k=5.

Fix: `pct_floor` **frozen** for ADR-011 baseline parity (that requirement is about
comparability, not correctness); new `pct_tail` for the gate — identical for k≥3,
returns the upper sample at k=2. Parity anchors n=102/n=118 use indices 95 and 111,
nowhere near the pathology, and that is asserted directly.

**One defect was masking the other.** Until real reasons surfaced, nothing ever
reached the gate with 2 samples, so the k=2 pathology could not be observed.

### What this phase changed about the method

Both defects were invisible to tests and visible in output, so the gate now
**reads artifacts too**: `check_no_percentile_ordering_violation` scans every
calibration report for `p95 < p50`. Teeth proven by injection — PASS on 10 real
records, FAIL on an injected row, PASS after removal.

The pre-fix artifacts are **preserved as evidence** in `engineering/raw/superseded/`,
not regenerated. They are separated from the gate's glob by a **directory
boundary** rather than a filename exclusion list, because an exclusion list is a
thing someone later adds a second entry to (the ADR-023 lesson).

### PHASE GATE 6 · **PASSED**

Invariants: H1 ✅ · H2 ✅ · H3 ✅ · H4 ✅ · H5 ✅ · H6 ✅ · H8 ✅ ·
**H7 ✅ UPGRADED** — the k=1 caveat is retired, with the `TOO_JITTERY` gap named.

**NEXT:** P07 — SCORING + ENGINE. Scoring per B-16 (freshness / reliability /
latency, so a proxy validated once cannot stay "working" forever), then the engine
loop composing registry → fetch → normalize → probe → gate → store with ADR-006
consecutive-failure cooldown. Carried forward honestly: `TOO_JITTERY` unproven
live, and `labels_verified` is still **0** because the sweep records verdicts per
*proxy* and never writes them back onto the *source* row.

---

## P07 — SCORING + ENGINE · gate **PASSED**

**Goal:** the two pieces the system was still missing — a *ranking* function that
ages (B-16) and the *loop* that composes everything built in P01–P06.

### Result

`make doctor` → **17/17 gate checks**, **300 tests** (291 unit + 9 integration).

**Scoring** (`atlas/core/policy/scoring.py`). Four terms kept deliberately
separate — latency (p95, the gate's own statistic), reliability (lifetime success
rate, a *different* fact from one burst's success_ratio), freshness (the B-16
decay term), anonymity. Collapsing any pair loses the distinction that makes a
pool rankable; the legacy pool stored one number per proxy and therefore could
not rank at all. `now` is an **argument**, not a clock read, so core/ stays pure
and every decay rule is verifiable without waiting. Absence of evidence scores
**zero**, never a flattering default — the ADR-024 lesson applied to ranking
rather than to the threshold.

**Engine** (`atlas/engine/cycle.py`). `registry → fetch → normalize → probe →
gate → store`, with ADR-006's cooldown extracted as a **pure function**
(`base * 2^n`, capped 1 h, disable only after 12 *consecutive* failures) so the
backoff schedule is testable without waiting an hour. `CycleReport.__post_init__`
asserts every candidate lands in **exactly one** bucket — B-02 (a candidate
vanishing without a reason) made structurally impossible instead of merely
tested for.

### Two defects found during the resume audit, both behind a 300-green suite

Consistent with P06: **neither was found by a failing test.** Both are defects in
*verification* rather than in behaviour — the machinery that is supposed to catch
defects had gaps in it.

**ADR-026 — code citing an ADR that did not exist.** `cycle.py` referenced
`ADR-026` **five times**; `DECISIONS.md` stopped at ADR-025. The engine's central
design decision — feeding probe results back onto the *source* row so a lying
source is recorded as lying — existed only as a docstring pointing at nothing.

The gate could not see it: `check_adr_claims_are_verifiable` walks
DECISIONS.md → code ("does this ADR name a way to check it?") and **nothing
walked the reverse edge**. ADR-014 was earned when an ADR described code that did
not exist; this is the same dangling reference *with its arrows reversed*. Fixed
by writing the ADR and by adding `check_cited_adrs_exist`, teeth proven by
injection (PASS → FAIL naming `cycle.py:33 cites ADR-099` → PASS).

Writing that ADR also exposed a smaller honesty bug: its `**Verify:**` line said
`-k "label"` yields four tests. It yields **three** — the REFUTED test is named
after its symptom (`..._socks_list_named_http_is_refuted_...`) and contains no
"label". Corrected to `-k "label or refuted"` (4 selected, 26 deselected). An
unverified Verify: line is the exact thing ADR-014 exists to prevent.

**ADR-027 — a one-sided bound.** `test_max_probes_bounds_total_work` asserted
`report.probed <= 3`, which **a cycle that probes nothing also satisfies**.
Mutation-tested rather than argued:

| mutant | `probed <= 3` | strengthened |
|---|---|---|
| clamp deleted (probes 8) | **FAIL** ✅ | FAIL ✅ |
| `run_cycle` starved (probes 0) | **PASS** ❌ | **FAIL** ✅ |

It caught the mutant its author imagined and passed the one they did not — which
is why it survived review: a test that fails for *some* mutant looks proven. Now
asserts `probed == 3` **and** `len(probe.sampled) == 3`, a second independent
witness, so a merely self-consistent report cannot pass alone.

The same shape had already bitten this phase's fixtures: they used RFC 5737
documentation IPs (`203.0.113.x`), which Python's `ipaddress` reports as
`is_private=True`, so `normalize` correctly dropped **every** candidate — and
**20 of 30 engine tests still passed** against a pipeline nothing flowed through.
The fixtures were wrong, not the code. Moved to a globally-routable range (no
network is touched; the probe is a fake).

**One more guard strengthened.** Citing `ADR-026` and `ScoringPolicy` as evidence
made `done_tasks_have_evidence` fail: it only recognised `def {symbol}(`, so a
**class** or a **markdown heading** could not be cited at all. It failed loudly
rather than silently — an under-powered guard, not a hole — but one that cannot
express the evidence people actually have pushes them to cite something vaguer.
Now parses Python with **AST** (functions, async functions, classes) and requires
markdown symbols to be **headings**, not passing mentions. This is *stricter*
than what it replaced: a `def` inside a comment no longer counts. Proven by
injection — a fake symbol and a commented-out `def` are both rejected, and the
old substring test would have accepted the latter.

### PHASE GATE 7 · **PASSED**

Invariants: H1 ✅ · H2 ✅ · H3 ✅ · H4 ✅ · H5 ✅ · H6 ✅ · H7 ✅ · H8 ✅.

**NEXT:** P08 — LEASE + API. The atomic lease (ADR-004/ADR-022; target **0**
double-delivery under real concurrency) and the hand-out API that validates
against a **caller-supplied** target at lease time.

**Carried forward honestly, unchanged by this phase:**
- `TOO_JITTERY` has **still** never fired on live data — unit-tested only.
- `labels_verified` is **still 0** in the registry artifact. `classify_label` now
  exists and all four branches are tested, but **no live sweep has been re-run**,
  so nothing has written verdicts back onto source rows yet. The capability is
  built; the number on disk has not moved, and I am not reporting it as though
  it had.
