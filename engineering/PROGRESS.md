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
