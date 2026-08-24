# SECURITY & RESPONSIBLE USE (H5 / §20)

> Recorded in Phase 0, **before** any v4 code was written, because two legacy behaviours had to be
> refused rather than migrated. This document is the commitment the rest of the build is held to.

## 1. What ATLAS v4 is for

Aggregating and quality-testing **publicly published** proxy lists, for **authorised** network
testing and development work. It answers one question: *"of these public proxies, which are
actually fast and actually work against the target I explicitly asked about, right now?"*

## 2. Hard prohibitions (non-negotiable)

| # | Prohibited | Enforcement in v4 |
|---|---|---|
| P1 | CAPTCHA solving / bypass | No solver, no solver API client, no browser automation. **Legacy `bebo.py:11-28` (2captcha) was deleted, not ported** — see `MIGRATION_LEDGER.md` §1.2/1.3, status `RETIRED_PROHIBITED`. |
| P2 | WAF / bot-defence evasion | No TLS/JA3 fingerprint spoofing, no header-order mimicry, no challenge solvers. UA rotation exists only as ordinary politeness toward public list endpoints. |
| P3 | Rate-limit circumvention | The opposite is implemented: global + **per-host** concurrency caps, exponential cooldown on consecutive failures, `ETag`/`If-Modified-Since` to avoid refetching unchanged lists. A `429` moves a source **toward** cooldown, never toward a retry storm. |
| P4 | Authentication bypass | No credential stuffing, no session replay, no auth probing. Proxy credentials are used only when the public source itself published them. |
| P5 | Private / internal network targeting | `S1 SYNTAX` (`core/policy/normalize.py`) rejects unspecified, loopback, multicast, link-local, **CGNAT 100.64/10**, private (RFC1918) and reserved ranges, each with its own named `DropReason`, and closes the class with a `not is_global` catch-all. The API target allow-policy (`core/policy/target_policy.py`) applies the **same** predicate to the caller's target, plus cloud-metadata hosts (SSRF defence). **Corrected in P08 — ADR-028:** this row previously claimed CGNAT was rejected "via `ipaddress`", but `ipaddress` reports `is_private=False` *and* `is_global=False` for RFC 6598, so CGNAT matched no check and was **accepted**. The claim preceded the control; the control now exists and `test_no_non_global_address_is_ever_accepted` enforces the whole class. |
| P6 | Scraping protected / paywalled sites | Public raw TXT, JSON APIs, CSV feeds and openly published HTML list pages only. A source that requires login, or is behind a challenge, is registered `enabled:false` with a reason — never worked around. |
| P7 | Concealing malicious activity | This project has no traffic-laundering, no chaining-for-anonymity feature, and no logging suppression. |

## 3. Targets come from the caller, never from a constant

The legacy code shipped `TEST_URL = "https://www.instagram.com"`
(`v1.py:29`, `v3.py:30`) and probed it thousands of times per run. That is
**`RETIRED_PROHIBITED`** (`MIGRATION_LEDGER.md` §4.2).

In v4:
- there is **no default target**;
- the target arrives per request (`?url=`), i.e. it is the caller's declared, authorised target;
- it must pass the allow-policy: scheme ∈ {`http`, `https`}, public address, size/timeout caps;
- Phase-0 baseline re-measurement deliberately used `https://example.com`, the IANA-designated
  test domain, so that even the *benchmark* is ToS-clean.

Responsibility for having authorisation to test a target rests with the caller; the system's job is
to refuse the categories above and to keep an auditable record (`request_id` in every log line).

## 4. Defensive posture of the service itself

| surface | control |
|---|---|
| API auth | API key required (header); 401 on absent/invalid |
| abuse | token-bucket rate limit per key/IP → 429 |
| input | `max_count` cap → 422; body size caps; URL scheme + address allow-policy |
| SSRF | private/loopback/link-local/CGNAT/metadata addresses rejected before any request is made |
| TLS | verification **on** (legacy ran `verify=False` in 9 places — `BUG_LEDGER.md` B-09); `TARGET_TLS_FAIL` is a first-class reason-code, so a MITM proxy is rejected instead of scored |
| secrets | `.env` only, never logged; error responses carry a `request_id`, never internals |
| logs | structured, with `request_id`; no proxy credentials written |

## 5. Data handling

Only publicly published proxy endpoints and their measured performance are stored. No traffic
content is captured, no user payloads are proxied by this service — it *hands out addresses*, it
does not relay traffic.

## 6. Honest limitations

- Public proxies are operated by unknown third parties. Some are honeypots or MITM. v4 mitigates
  (TLS verification, content checks, anonymity classification) but **cannot eliminate** this risk.
  Never send sensitive data through a free public proxy.
- Availability of public lists fluctuates; **35 of the 123 legacy URLs are already dead** (measured,
  `SOURCE_INVENTORY.json`). Source health/cooldown handles this, but yield will vary.
- Anonymity classification reflects what a judge endpoint reports at that moment; it is not a
  guarantee of untraceability.
