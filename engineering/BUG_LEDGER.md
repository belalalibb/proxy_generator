# BUG LEDGER — legacy defects (PHASE 0)

> Every `file:line` below was verified mechanically by
> `engineering/tools/verify_bug_lines.py` → `engineering/raw/bug_scan.json`.
> Mechanical totals are reproduced verbatim; no count is hand-written (H2).

## Mechanical totals across `bebo.py`, `proxychecker.py`, `proxy_generator_v2.py`, `v1.py`, `v2.py`, `v3.py`

| defect class | occurrences |
|---|---|
| `hardcoded_http_url` | **257** |
| `except_broad` (`except Exception`) | 33 |
| `silent_handlers` (handler body is only `pass`/`continue`/`return <empty>`) | **23** |
| `time_sleep` (blocking sleep) | 13 |
| `captcha` references | **10** |
| `except_pass` (`except Exception:` bare body) | 10 |
| `bare_except` (`except:`) | **9** |
| `verify_false` (TLS verification off) | 9 |
| `open_write_truncate` (`open(..., 'w')`) | 8 |
| `max_workers_literal` | 4 |
| `disable_warnings` | 3 |
| `input_call` (blocks automation) | 3 |
| `open_append` | 2 |
| `instagram_target` | 2 |

---

## B-01 — CAPTCHA-solving code (BLOCKER, H5)

**`bebo.py:11-17`** `get_cap_id()`, **`bebo.py:19-28`** `get_cap_sol()` — 2captcha integration
(10 mechanical matches for `captcha`).

```python
def get_cap_id(api_key, site_key, page_url):
    captcha_id_response = requests.post(
        f"http://2captcha.com/in.php?key={api_key}&method=userrecaptcha&googlekey={site_key}&pageurl={page_url}")
```

**Impact:** CAPTCHA bypass is explicitly prohibited by H5 / §20.
**Resolution:** **RETIRED with no replacement.** Recorded in `MIGRATION_LEDGER.md` as
`RETIRED_PROHIBITED`. v4 contains no CAPTCHA/WAF/rate-limit/auth circumvention logic of any kind.
Secondary defect: `get_cap_sol` is an **unbounded `while` loop** with `time.sleep(5)` and no
attempt ceiling — it can hang forever.

---

## B-02 — Silent exception swallowing (23 sites)

Worst instance, **`proxy_generator_v2.py:237`**, in the *fetch* path:

```python
        except Exception:
            pass
        return set()
```

**Impact:** a source can 404/502/throttle forever and be indistinguishable from a source that
legitimately returned zero proxies. This is the direct cause of the legacy system re-requesting
**35 dead URLs on every cycle for months** (§2.1 of `ANALYSIS.md`: 23×404, 4×502, 3×403, 2×429, 2×521, 1×526).

Full list (from `bug_scan.json`): `bebo.py:8`, `proxychecker.py:18`,
`proxy_generator_v2.py:191,237,281,301,318,329,349,416,529`, `v1.py:220,318,343`,
`v2.py:96,193,211,228,252,304,327`, `v3.py:186,402`.

**v4 resolution:** every failure produces a typed **reason-code** (§7) persisted to
`data/quarantine/` and aggregated in `/stats`. No handler may end in a bare `pass`;
`tests/unit/test_reason_codes.py` deterministically provokes each code.

---

## B-03 — `LIVE ≠ GOOD`: no speed gate (root architectural failure, H7)

**`proxy_generator_v2.py:380`**

```python
                if r.status_code == 200 and len(r.text) > 1000:
                    return {'proxy': proxy, 'ok': True, 'ms': ms, ...}
```

Single sample, and `ms` is **recorded but never used as an admission criterion**.

**Measured impact** (`BASELINE.json`, from the user's own 1 418.98 s run):
accepted proxies had **p50 = 6 359.5 ms**, **p95 = 15 903 ms**, **max 19 035 ms**;
**95.1 %** exceeded 1 500 ms and **58.8 %** exceeded 5 000 ms (all four figures from
the **n=102** `proxy_details.json` stream).

> The n=118 `proxy_scraper.log` stream of the same run gives **95.8 %** / **56.8 %**.
> Quoting those two beside this stream's p50 was a real defect in this document,
> corrected under ADR-020: every number was true, the sentence was not.

**v4 resolution:** k=5 samples → p50/p95/jitter/throughput; admission gate on **p95** (never `min`);
calibrated thresholds (§8); target metric `pool_p95_proxy_latency ≤ 900 ms`.

---

## B-04 — File-write tearing / state destruction

**`proxy_generator_v2.py:467`**

```python
        with open(filename, 'w', encoding='utf-8') as f:
            for p in sorted_proxies:
                f.write(f"{p['proxy']}\n")
```

8 `open(...,'w')` truncations across the tree. `SIGKILL` between truncate and final write
leaves `proxy.txt` empty or half-written — the entire working set is lost, with no journal.

**v4 resolution:** SQLite + WAL as the source of truth; all exports written `.tmp` → `os.replace()`
(atomic rename); `tests/integration/test_crash_durability.py` runs SIGKILL ×10.

---

## B-05 — Read-modify-write race on shared files

**`bebo.py:43-51`** `remove()` reads all lines then rewrites the file:

```python
    with open(file_path, 'r') as file:
        lines = file.readlines()
    with open(file_path, 'w') as file:
        for line in lines: ...
```

Two concurrent callers → lost updates. Combined with **`proxy_generator_v2.py:446`** appending
under a `threading.Lock` that is **local to one process**, any second process corrupts the file.

**v4 resolution:** no file is ever the mutable source of truth. State transitions are
`BEGIN IMMEDIATE` transactions (§9 lease protocol).

---

## B-06 — No atomic consumption → double delivery is guaranteed (H3)

There is **no consumption concept anywhere in the legacy tree**. `proxy.txt` is a flat list;
every reader gets every line. `proxychecker.py` re-reads and re-appends without a durable seen-set.

**v4 resolution:** `pool` table with `READY → LEASED → CONSUMED`, `lease_id` + `lease_expires`,
compare-and-set inside one transaction, expiry sweep. Proven by
`tests/chaos/test_no_double_delivery.py` (200 concurrent × 20 runs, zero intersection).

---

## B-07 — O(n²) file reads inside the hot loop

**`proxychecker.py:23-24`**

```python
for pr in proxy_li:
    proxy_li_clend = bebo.files_as_li('clend_proxy.txt')   # re-read EVERY iteration
```

**Impact:** 616 proxies → 616 full file reads. Also **`proxychecker.py:24` crashes** on first run:
`clend_proxy.txt` is never created (`FileNotFoundError`, uncaught).
Also fully **serial** — one proxy at a time, 5 s timeout each.

**v4 resolution:** in-memory Bloom filter + SQLite `UNIQUE` index (§6), O(1) membership.

---

## B-08 — Unbounded concurrency, self-inflicted throttling

`max_workers_literal` ×4: **`v1.py:27`** `MAX_WORKERS = 150`, **`v3.py:28`** `MAX_WORKERS = 100`,
**`proxy_generator_v2.py:44`** `MAX_WORKERS_TEST = 100`, **`:43`** `MAX_WORKERS_COLLECT = 20`.

No semaphore, no **per-host** cap. `ThreadPoolExecutor(150)` × blocking `requests` = 150 OS threads
parked on sockets.

**Measured evidence of self-harm:** the inventory sweep observed **2× HTTP 429** and
**1× 403** from the very hosts the legacy code hammers, and GeoNode returned a **659-byte throttled
body** when hit twice within ~2 s (§2.2 of `ANALYSIS.md`).

**v4 resolution:** async I/O with a global semaphore **and** a per-host limiter; `ETag`/
`If-Modified-Since` to avoid refetching unchanged lists; exponential cooldown on consecutive failures.

---

## B-09 — TLS verification globally disabled

`verify_false` ×9, `disable_warnings` ×3 (e.g. **`proxy_generator_v2.py:17`**, `:231`, `:245`, `:373`).

```python
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
...
r = requests.get(url, ..., verify=False)
```

**Impact:** a proxy that MITMs or injects a captive-portal page is scored **identically** to an
honest one, because the certificate is never checked and only `len(r.text) > 1000` is asserted.
This silently pollutes the "working" set.

**v4 resolution:** TLS verified by default; `TARGET_TLS_FAIL` is a first-class reason-code (§7 S5);
content sanity via `CONTENT_MISMATCH` rather than a length heuristic.

---

## B-10 — ToS-hostile default target (H5 risk)

`instagram_target` ×2: **`v1.py:29`**, **`v3.py:30`** `TEST_URL = "https://www.instagram.com"`.
`v3.py:586` even prints `"Instagram may be blocking free proxies"` — the authors knew.

**Impact:** the legacy default probes a login-walled, bot-hostile third party thousands of times per
run. Also makes the 0.68 % success rate meaningless (it measures Instagram's defences, not proxy quality).

**v4 resolution:** **no default target exists.** The target arrives per-request from the caller and
passes an allow-policy (scheme ∈ {http, https}; private/loopback/link-local/metadata addresses
rejected). Baseline re-measurement deliberately used `example.com` (IANA test domain).

---

## B-11 — Sources hardcoded in source files

`hardcoded_http_url` = **257** literal URLs across the tree; 123 unique
(`engineering/raw/legacy_urls.json`). Three near-duplicate lists in `v1.py`, `v3.py`,
`proxy_generator_v2.py` sharing ~70 % of entries, each drifting independently.

**Impact:** adding or disabling a source requires a code edit and restart. A dead source cannot be
cooled down. Yield cannot be attributed (the log shows `raw.githubusercontent.com: 649 404` for
~50 *different* repos — see §5 of `ANALYSIS.md`).

**v4 resolution:** §4 — sources live in `data/sources/sources.json` with a stable `id`, per-source
stats and hot-reload. Hardcoding a source URL in a `.py` file is a contract breach; enforced by test.

---

## B-12 — Protocol label trusted blindly

**`proxy_generator_v2.py:368`** `proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}`
— every candidate is assumed HTTP.

**Concrete proof it is wrong:** the legacy list contains
`raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt` (**`proxy_generator_v2.py:69`**) —
a **SOCKS** repository whose file is named `http.txt`, measured ALIVE with **2 853 unique** candidates.
Every SOCKS proxy there was tested as HTTP and discarded.

**v4 resolution:** mandatory `S3 PROTOCOL` empirical discovery (http / https-connect / socks4 /
socks5), record corrected, `PROTO_MISMATCH` reason-code.

---

## B-13 — `input()` blocks automation

`input_call` ×3, e.g. **`v2.py:396`** `choice = input("\nEnter choice (1 or 2): ").strip()`.
Cannot run headless, under cron, or in CI.

**v4 resolution:** `config.yaml` + env + CLI flags only; `atlas` CLI is fully non-interactive.

---

## B-14 — Import-time side effects

**`v1.py:45-53`** configures `logging.basicConfig` with a `FileHandler('proxy_scraper.log')` at
**module import**, and **`v1.py:24`/`v3.py:26`** call `urllib3.disable_warnings()` at import.
Importing the module for a unit test mutates global logging state and writes a file.

**v4 resolution:** `core/` is import-pure (no I/O, no network — enforced by
`tests/unit/test_architecture.py`); logging configured explicitly in `obs/` at process start.

---

## B-15 — Progress thread races on shared counters

**`v3.py:481-495`** / **`v1.py:500`** / **`proxy_generator_v2.py:419-431`**: a daemon thread reads
`self.tested` and a `working` closure variable mutated by pool threads without synchronisation, in a
`while` loop with `time.sleep(1)`. Reported progress can go backwards or wedge at 99 %.
`proxy_generator_v2.py:420` `while self.tested < total` never terminates if a future raises
outside the counted path.

**v4 resolution:** counters live in the store / metrics registry; progress derived from queried
state, not from mutable shared ints.

---

## B-16 — No re-verification, no eviction, no ageing

`proxy.txt` has no timestamps. A proxy validated once stays in the file forever.

**Measured impact:** re-testing that file today gives **3.0 % live (9/300)** — 97 % of the
"working" list is stale (`BASELINE.json`, seed 1337, reproducible).

**v4 resolution:** `reverify_interval_seconds`, `max_age_seconds`, `evict_after_failures`,
a `freshness` term in the score, and a per-row staleness flag on the hand-out.

**Correction (P09, ADR-035).** This entry originally promised `target_ttl` (90 s) "for
per-target validity". That part was **withdrawn, not implemented**: the schema holds ONE
`last_checked` per proxy, recorded against whatever target discovery probed, so no interval
however tight can support the sentence "validated against YOUR target 90 s ago". Re-probing
every 90 s would have made it *worse* -- refreshing `last_checked` against the discovery
target would CLEAR the staleness flag, converting an honest "revalidate this yourself" into
a per-target guarantee still unbacked by any stored fact. The flag is therefore keyed to
`scheduler.recheck_ready_after_s` (900 s), the one interval this system drives, and named
`past_recheck_horizon` for what it actually proves. Per-target validity needs a
`(proxy, target)` table and remains unbuilt.

---

## Defects intentionally NOT carried into v4

| legacy behaviour | why dropped |
|---|---|
| 2captcha solving (`bebo.py:11,19`) | prohibited (H5/§20) — retired, no replacement |
| `instagram.com` default target | ToS-hostile; targets now caller-supplied + allow-policy |
| `verify=False` everywhere | hides MITM/captive-portal proxies |
| `input()` menus | blocks headless operation |
| `proxy.txt` as mutable state | cannot express LEASED; destroyed by mid-write SIGKILL |
| `MAX_PROXIES = 15000` truncation | arbitrary cap; v4 bounds work by time/budget, not by a magic constant |

---

# Defects introduced by **v4 itself**, found by measurement

The ledger above catalogues the legacy system's defects. This section records
defects in the *rewrite* — kept in the same file deliberately, because a ledger
that only indicts the predecessor is a marketing document.

Both entries below were found by **reading an artifact**, not by a failing test.
Both were sitting behind a fully green suite.

## V4-01 — A measured cause overwritten by a note about something never tested

**Where:** `atlas/adapters/probe_aiohttp.py`, `discover_protocol`
**Class:** B-02 (cause lost at the point of discovery), *recurring inside its own fix*

One variable held both "measured failure" and "this rung was untestable". SOCKS
is last in the ladder, so the untestable placeholder overwrote every real HTTP
measurement. 24 of 24 endpoints that passed TCP were reported
`PROTO_MISMATCH: socks4 not testable`; **0 reached the gate.**

**Symptom in artifact:** `engineering/raw/calib_smoke.json` —
`tcp_ok: 24, reached_gate: 0, PROTO_MISMATCH: 24`.

**Fix:** rank the two facts (`last_tested or untested`). A measurement always
outranks an untested rung. ADR-025.

**Effect:** real causes appeared — `TCP_REFUSED: 44, BAD_STATUS: 31,
PROXY_AUTH_REQUIRED: 19, TLS_FAILED: 1` — and 14 proxies reached the gate.

## V4-02 — A "95th percentile" that returned the minimum

**Where:** `atlas/core/policy/percentile.py` / `admission.py`, `build_profile`
**Class:** new — a statistic borrowed for comparability, reused for decisions

`int((n-1)*0.95) == 0` at `n=2`, so the p95 of two samples was the **faster**
one. Violated `p95 >= p50` in 4000/4000 randomised trials at n=2, 0/4000 elsewhere.

**Symptom in artifact:** `engineering/raw/admission_live_fixed.json` —
`samples [7659.2, 4100.7]`, `p50 5880.0`, `p95 4100.7`. A tail below the median.

**Severity: false ADMIT, not a display bug.** Samples `(1400ms, 1600ms)` against
the 1500 ms ceiling → `OK / USABLE`. Jitter 0.09 and success_ratio 1.0, so no
other rule intervened. The gate admitted a proxy it had itself measured over
budget. Invisible to every unit test, all of which use k=5.

**Fix:** `pct_floor` frozen for ADR-011 baseline parity; new `pct_tail` used by
the gate, identical for k>=3, returns the upper sample at k=2. ADR-024.

**Effect:** `TOO_SLOW_P95` began firing on live data; zero `p95 < p50` records
remain in `engineering/raw/admission_live_adr024.json`.

**Note on discovery order:** V4-01 was *masking* V4-02. Until real reasons
surfaced, no proxy ever reached the gate with 2 samples, so the k=2 pathology
could not be observed. Fixing one defect is what made the next one visible.
