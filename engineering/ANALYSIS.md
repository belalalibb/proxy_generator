# PHASE 0 — FORENSIC ARCHAEOLOGY

> **Status:** COMPLETE
> **Rule obeyed:** no line of code was written into `atlas/` before this document and its four sibling
> evidence files existed (PHASE GATE 0).
> **Every number below is machine-extracted.** The tool that produced it is named next to it.
> Nothing here is estimated, remembered, or rounded by hand (H2).

---

## 0. Evidence index

| Artifact | Produced by | What it proves |
|---|---|---|
| `engineering/raw/legacy_urls.json` | `tools/extract_legacy_sources.py` | every URL literal in the legacy code (AST-based) |
| `engineering/SOURCE_INVENTORY.json` | `tools/probe_legacy_sources.py` → `tools/reprobe_empty.py` → `tools/verify_geonode_parser.py` | the **real, current** state of all 123 legacy URLs |
| `engineering/raw/reprobe_empty.json` | `tools/reprobe_empty.py` | recovery of sources a naive regex would have discarded |
| `engineering/raw/geonode_body.txt` | `curl` | raw proof GeoNode is alive (230 019 bytes of valid JSON) |
| `engineering/BASELINE.json` | `tools/measure_baseline.py` | the legacy performance numbers v4 must beat |
| `engineering/BUG_LEDGER.md` | manual audit + code reading | defect inventory with file:line |
| `engineering/MIGRATION_LEDGER.md` | manual audit | 100 % of legacy features mapped, no `unknown` |

Reproduce everything:

```bash
python3 engineering/tools/extract_legacy_sources.py
python3 engineering/tools/probe_legacy_sources.py
python3 engineering/tools/reprobe_empty.py
python3 engineering/tools/verify_geonode_parser.py
python3 engineering/tools/measure_baseline.py 300
python3 engineering/tools/inspect_inventory.py
python3 engineering/tools/show_baseline.py
```

---

## 1. File-by-file verdict

LOC via `wc -l`. Quality is a judgement; the *reason* column is factual.

| file | LOC | purpose | quality (0-5) | verdict | reason | reusable symbols |
|---|---|---|---|---|---|---|
| `bebo.py` | 69 | misc helpers: Telegram notify, **2captcha solver**, file read/append/remove | 1 | **RETIRE (partial ADAPT)** | `get_cap_id`/`get_cap_sol` are CAPTCHA-bypass → **prohibited by H5/§20, no replacement will be built**. File I/O helpers are unsafe (read-modify-write whole file, no locking, no encoding). `send_to_telegram` swallows every exception. | concept only: "notify on event" → v4 `obs/` structured events |
| `proxychecker.py` | 30 | serial proxy checker vs `httpbin.org/ip` | 1 | **REWRITE** | Serial (one proxy at a time), bare `except:`, re-reads `clend_proxy.txt` **inside** the loop → O(n²) file reads, appends duplicates on re-run, crashes if `clend_proxy.txt` missing (never created). Marks a proxy good on a single 200 from one judge. | idea: judge endpoint → v4 `S4 ANONYMITY` probe |
| `proxy_generator_v2.py` | 569 | **the best legacy artifact**: 80 raw + 14 web sources + GeoNode, threaded collect, threaded target test, incremental save, infinite loop, argparse CLI | 3 | **ADAPT** | Genuinely the richest logic: retry loop, per-source stats, `os.path` handling for PyInstaller, atomic-ish append under a lock, `--collect-only`. But: sources hardcoded in `.py` (violates §4), `verify=False` everywhere, single-sample latency, `except Exception: pass` ×7, unbounded `MAX_PROXIES=15000`, no protocol detection, no pool, no lease, file-append as the only state. | `validate_proxy` (harden), `extract_proxies` multi-regex (extend), source URL list (**move to JSON**), `rand_headers`, per-source `source_stats`, retry semantics, `test_one` accept-rule (as baseline reference) |
| `v1.py` | 728 | "500+ source" scraper, tests against `instagram.com`, logging to `proxy_scraper.log`, site-specific parsers | 2 | **REWRITE (mine for parsers)** | Largest but most brittle: target is a **hostile, ToS-protected** site (Instagram) → v4 must not ship that default (H5). `MAX_WORKERS=150` unbounded, CRLF line endings, per-site parser methods hardwired to markup that has since changed, logging config at import time (side effect). | `parse_free_proxy_list`, `parse_spys_one`, `parse_hide_mn`, `parse_advanced_name` → become **declarative** `html_table` parser_args in v4; the logging *format* is a good human-log model |
| `v2.py` | 406 | 10 named sources with a `parser` callable per source | 3 | **ADAPT (best structure)** | The only legacy file with the right *shape*: `{name, url, parser}` records — one step from v4's declarative source registry. But parsers are bound methods (not data), `input()` in `main()` blocks automation, `except:` bare ×5, `test_proxy_connection` is TCP-only and called "valid". | **the source-record concept** → v4 `Source` domain object; `parse_geonode_api`, `parse_proxy11_api`, `parse_proxy_list_download`, `parse_proxydb`, `parse_proxies24` → declarative parsers |
| `v3.py` | 652 | "ultimate" merge of v1+v2: 67 sources, TCP pre-check **then** target test, JSON+TXT output, progress thread | 3 | **ADAPT** | Introduces the single most valuable idea in the whole legacy tree: **cheap TCP triage before expensive target test** (`test_proxy_connection` → `test_proxy_instagram`). Also writes a structured `proxy_details.json`. Still: Instagram target, `MAX_WORKERS=100`, progress thread races on shared counters, no dedupe across runs. | **two-stage validation** → v4 `S2 TCP` → `S5 TARGET`; `save_results` JSON schema → v4 exports; progress reporting → v4 `obs/` |
| `proxy.txt` | 616 | output: 616 unique `ip:port`, no metadata | — | **DATA: ADOPT as seed** | 0 duplicate lines (good), but no timestamp, no protocol, no latency, no source → cannot be aged, scored, or attributed. Re-tested today: **3.0 % still live**. | 616 seed candidates for v4 cold-start |
| `proxy_details.json` | 725 | structured result of a 1418.98 s run: 15 000 collected → 102 working | — | **DATA: ADOPT as baseline** | The single richest evidence file in the repo. Gives the real historical yield **and** the real accepted-latency distribution — which is where the legacy design fails (see §3). | baseline numbers, latency distribution |
| `proxy_scraper.log` | 248 | run log of the v1/v3 lineage | — | **DATA: ADOPT as source-yield evidence** | Reveals per-host yields and the real failure mix (11×404, 6×HTTP error, 1 timeout). | per-host yield ranking, failure taxonomy |

**Totals:** 2 454 LOC of Python across 6 files, 0 tests, 0 type hints, 0 config files, 3 near-duplicate
scrapers (`v1`/`v3`/`proxy_generator_v2` share ~70 % of their source list).

---

## 2. Source Inventory — all 123 legacy URLs, actually fetched

`tools/probe_legacy_sources.py`, 123 URLs, concurrency 12, timeout 20 s, wall clock **2.1 s**.

### 2.1 First pass (naive `ip:port` regex — the legacy method)

| verdict | count | meaning |
|---|---|---|
| ALIVE | 56 | HTTP 200 **and** ≥1 parseable `ip:port` |
| DEAD | 35 | non-200 |
| EMPTY | 25 | HTTP 200 but regex found nothing |
| ERROR | 1 | `http://` — a malformed literal in the legacy code |
| NOT_A_SOURCE | 6 | telegram / 2captcha / httpbin / instagram / media.io — utility endpoints, excluded |

**DEAD breakdown:** `404 × 23`, `502 × 4`, `403 × 3`, `429 × 2`, `521 × 2`, `526 × 1`.
→ **28 % of the legacy source list is simply gone.** Every run wasted requests on them because
no source had health state or a cooldown.

### 2.2 Second pass — the naive regex was itself a bug

25 URLs returned **HTTP 200 with real content** but scored zero, purely because the legacy
regex only matches `ip` and `port` when *adjacent*. JSON APIs put them in separate keys; HTML
tables put them in separate `<td>`. Re-probed with structured parsers (`tools/reprobe_empty.py`):

| refined verdict | count |
|---|---|
| ALIVE_HTML_TABLE | 6 |
| ALIVE_JSON | 1 |
| TRULY_EMPTY | 18 |

Recovered sources (`refined_unique` = distinct public proxies parsed in one fetch):

| unique | parser needed | URL |
|---|---|---|
| 500 | `json_path` | `proxylist.geonode.com/api/proxy-list?...` |
| 198 | `html_table` | `free-proxy-list.net/anonymous-proxy.html` |
| 100 | `html_table` | `www.sslproxies.org/` |
| 64 | `html_table` | `hide.mn/en/proxy-list/` |
| 55 | `html_table` | `geonode.com/free-proxy-list` |
| 50 | `html_table` | `list.proxylistplus.com/SSL-proxy` |
| 28 | `html_table` | `proxybros.com/free-proxy-list/` |

**Correction recorded (H2 honesty):** GeoNode's API first measured 230 067 bytes of JSON, then
**659 bytes of non-JSON** on the re-probe ~2 s later. I did not accept either reading blindly — I
re-fetched with `curl` and got **230 019 bytes of valid JSON yielding 500 unique proxies**
(`engineering/raw/geonode_body.txt`, verified by `tools/verify_geonode_parser.py`, which patched the
inventory and appended a `corrections` entry).
The 659-byte body was **per-host throttling**, not a dead source.
→ **Design consequence for v4 (non-optional): per-host rate limiting + `ETag`/`If-Modified-Since`,
and "one bad fetch ≠ dead source" (require consecutive failures before cooldown).**

### 2.3 Final usable inventory

**63 of 123 legacy URLs are usable today** (56 regex-parseable + 6 HTML-table + 1 JSON),
collectively yielding **93 581 unique candidates** from the ALIVE set in a single 2.1 s sweep.

Top yielders (unique candidates, fetch ms):

| unique | ms | URL |
|---|---|---|
| 28 244 | 18 | `ErcinDedeoglu/proxies/main/proxies/http.txt` |
| 5 909 | 186 | `Tsprnay/Proxy-lists/master/proxies/https.txt` |
| 4 780 | 14 | `zevtyardt/proxy-list/main/http.txt` |
| 3 575 | 132 | `api.openproxylist.xyz/https.txt` |
| 3 185 | 3 | `B4RC0DE-TM/proxy-list/main/HTTP.txt` |
| 3 133 | 4 | `Anonym0usWork1221/Free-Proxies/.../http_proxies.txt` |
| 3 032 | 390 | `Tsprnay/Proxy-lists/master/proxies/http.txt` |
| 2 994 | 4 | `ErcinDedeoglu/proxies/main/proxies/https.txt` |
| 2 871 | 36 | `tuanminpay/live-proxy/master/http.txt` |
| 2 853 | 115 | `TheSpeedX/SOCKS-List/master/http.txt` |

Full per-URL records in `engineering/SOURCE_INVENTORY.json`.

---

## 3. Behavioural Baseline — the numbers v4 must beat

`engineering/BASELINE.json`. Two independent streams.

### 3.1 Historical (the user's own recorded run, `proxy_details.json`)

| metric | value |
|---|---|
| scan date | 2025-09-01T18:11:48 |
| sources | 67 |
| collected | 15 000 |
| working | 102 |
| success rate | **0.68 %** |
| duration | 1 418.98 s (23.6 min) |
| collected / min | 634.3 |
| working / min | 4.31 |
| minutes to produce 10 working | 2.32 |

Accepted-proxy latency distribution (n = 102):

| min | p50 | mean | p95 | max |
|---|---|---|---|---|
| 756 ms | **6 359.5 ms** | 7 145.1 ms | **15 903 ms** | 19 035 ms |

From `proxy_scraper.log` (n = 118 "Working" lines): **95.8 % exceeded 1 500 ms**, **56.8 % exceeded 5 000 ms**.

> **This is the central finding of Phase 0.** The legacy system had *no speed gate whatsoever*:
> a 19-second proxy was recorded as a success identical to a 756 ms one. "Working" meant
> "one request eventually returned 200". That is precisely the `LIVE ≠ GOOD` failure H7 forbids.

### 3.2 Measured now (legacy algorithm re-run verbatim, today)

`tools/measure_baseline.py` re-implements `proxy_generator_v2.ProxyScraper.test_one`
byte-for-byte (same timeout 10 s, 2 retries, 100 workers, accept rule `status==200 and len(body)>1000`)
against `https://example.com` — the IANA-designated test domain, chosen so the baseline itself
complies with H5 (the legacy default of `instagram.com` does not).

| metric | value |
|---|---|
| pool available (`proxy.txt`) | 616 |
| sample tested | 300 (seed 1337, reproducible) |
| live | **9** |
| live rate | **3.0 %** |
| wall clock | 68.5 s |
| throughput | 4.4 tested/s |
| latency min / p50 / p95 / max | 297 / 1 106 / **1 464** / 2 157 ms |
| ≤ 900 ms | 33.3 % |
| ≤ 1 500 ms | 88.9 % |

Note the honest asymmetry: the *survivors* of a 9-month-old list look fast (p95 1 464 ms) because
slow ones died first — survivorship bias. The historical p95 of 15 903 ms is the true measure of
what the legacy gate *admitted*.

### 3.3 Explicit targets for v4

| dimension | legacy (measured) | v4 requirement |
|---|---|---|
| admitted-proxy p95 latency | 15 903 ms (historical) | **≤ 900 ms** (§19) |
| live rate of delivered proxies | 3.0 % of a stale list | 100 % of delivered (validated at hand-out) |
| target validation | none (one global target) | per-request `url` + allow-policy; freshness reported against the 900 s recheck horizon, never claimed per-target (ADR-035) |
| latency samples per proxy | 1 | 5 (p50/p95/jitter) |
| ready-pool guarantee | none | 10 ready, ≥ 99 % availability |
| double delivery | unbounded (plain text file) | **0**, atomic lease |
| crash recovery | none | 10/10 |
| dead sources in list | 35/123 retried forever | health + exponential cooldown |
| tests | 0 | 6 levels, `core/` ≥ 90 % |

---

## 4. Bug Ledger

Full detail with file:line in `engineering/BUG_LEDGER.md`. Summary of what recurs:

| class | count | worst instance |
|---|---|---|
| silent exception swallowing | 21 | `proxy_generator_v2.py:237` `except Exception: pass` inside the fetch path — a source can 500 forever and never be marked unhealthy |
| blocking I/O in thread pools | 6 | `v1.py:414` 150 threads × `requests` with 8 s timeout = 150 OS threads parked on sockets |
| unbounded concurrency | 4 | `v1.py:27` `MAX_WORKERS=150`, no semaphore, no per-host cap → self-inflicted 429s (2 observed in the inventory) |
| file-write tearing | 3 | `proxy_generator_v2.py:467` `save()` truncates `proxy.txt` with `'w'` then writes; SIGKILL mid-write = list destroyed |
| read-modify-write races | 2 | `bebo.py:43` `remove()` reads all lines then rewrites — two callers lose data |
| O(n²) file reads | 1 | `proxychecker.py:24` re-reads `clend_proxy.txt` **inside** the per-proxy loop |
| prohibited functionality | 2 | `bebo.py:11,19` 2captcha solver → **H5 violation, retired without replacement** |
| ToS-hostile default target | 3 | `v1.py:29`, `v3.py:30` `TEST_URL = instagram.com` → v4 requires an explicit caller-supplied target |
| TLS verification disabled globally | 3 | `proxy_generator_v2.py:17` `disable_warnings` + `verify=False` on every call — cannot distinguish a MITM proxy from a working one |
| duplicate-append on re-run | 1 | `proxychecker.py:28` appends to `clend_proxy.txt` without a durable seen-set |
| crash on missing file | 1 | `proxychecker.py:24` `files_as_li('clend_proxy.txt')` — file is never created |

---

## 5. Data Archaeology

**Which sources actually produced?** From `proxy_scraper.log` (`✅ <host>: <n> proxies`, summed per host):

| host | proxies contributed | reality check |
|---|---|---|
| `raw.githubusercontent.com` | 649 404 | dominant, but this aggregates ~50 distinct repos under one hostname — the legacy log **could not attribute yield to a specific source**. v4 fixes this with `source_id`. |
| `proxyspace.pro` | 244 623 | high volume |
| `api.openproxylist.xyz` | 154 290 | high volume, confirmed ALIVE today (3 575 unique) |
| `api.proxyscrape.com` | 109 346 | ALIVE |
| `cdn.jsdelivr.net` | 9 807 | ALIVE (2 474 unique) — useful GitHub mirror when raw is throttled |
| `www.proxy-list.download` | 464 | low yield |
| `www.sslproxies.org` | 200 | low yield, needs `html_table` (100 unique today) |
| `free-proxy-list.net` | 169 | low yield, needs `html_table` (198 unique today) |

**Dead today** (were in the list, now non-200): 35 URLs, dominated by `404 × 23`. The legacy code
re-requested all of them on **every single cycle** for months.

**The decisive lesson:** volume ≠ value. `raw.githubusercontent.com` supplied 649 k candidates and
the whole run still produced only 102 working proxies at p50 6.4 s. v4 therefore ranks sources by
`quality_rate` and `elite_rate` (validated ÷ candidates), **never** by raw candidate count —
exactly the `health_score` weighting in §5.

---

## 6. Architectural conclusions carried into v4

1. **Sources are data, not code.** 63 live URLs + their required parser (`line_ipport` / `json_path` /
   `html_table`) move into `data/sources/sources.json`. Hardcoding a URL in `.py` is a contract breach (§4).
2. **Speed is a gate, not a label.** The legacy p50 of 6.4 s is the failure this project exists to fix.
   Multi-sample p95 + jitter, admission threshold, and re-verification (§8).
3. **Cheap-before-expensive triage** (v3's one great idea) becomes the formal `S2 TCP → S5 TARGET` ladder.
4. **Never trust the source's protocol label** — `TheSpeedX/SOCKS-List/master/http.txt` is a SOCKS repo
   serving a file named `http.txt`. Hence mandatory `S3 PROTOCOL` discovery.
5. **One bad fetch ≠ dead source** (the GeoNode throttle lesson). Health needs consecutive-failure
   counting, exponential cooldown, and per-host rate limiting.
6. **A text file is not state.** Atomic lease in SQLite/WAL, because `proxy.txt` cannot express
   `LEASED`, cannot prevent double delivery, and is destroyed by a SIGKILL mid-`save()`.
7. **Targets come from the caller, never from a constant.** Removes both the ToS problem and the
   "validated against the wrong URL" problem.

---

## 7. PHASE GATE 0 — self-check

| requirement | status | evidence |
|---|---|---|
| every file examined and given a verdict | ✅ | §1, 9/9 files |
| Source Inventory built by **real** requests | ✅ | `SOURCE_INVENTORY.json`, 123 URLs, 2.1 s sweep |
| Behavioural Baseline measured, not guessed | ✅ | `BASELINE.json`, both streams, 300-proxy re-test |
| Bug Ledger with file:line | ✅ | `BUG_LEDGER.md`, 47 findings |
| Data Archaeology answers "who actually yielded?" | ✅ | §5 |
| Migration Ledger covers 100 %, no `unknown` | ✅ | `MIGRATION_LEDGER.md` |
| no code written into `atlas/` yet | ✅ | `atlas/` does not exist at this commit |

**GATE 0: PASSED** → proceed to P01 (architecture skeleton + isolation test).
