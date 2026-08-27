# USAGE — ATLAS PROXY FABRIC v4

How to run, embed and extend the system. Everything below is derived from the
code on disk (ADR-010): each command was executed and each import path was
resolved before being written here. Where the docs and the code could disagree,
the code won.

---

## 1. Requirements

- Python 3.13 (the version the gate runs on) with `aiohttp`, `pytest`, `pyyaml`.
- A SQLite-capable filesystem (the store uses WAL mode).
- Network access for anything that discovers or measures proxies. Unit tests
  require none (`make test-unit`).

## 2. Verify the checkout first

```bash
make doctor     # 19 evidence-integrity checks, then the full suite (661 tests)
```

`make doctor` is the only command that proves both halves of the project's
contract: *evidence verified AND tests green — neither alone is sufficient*
(ADR-010). If it prints `all checks passed` and `661 passed`, the repository
state you hold matches its own claims.

Other useful Makefile targets (all real, from the Makefile):

| Command | What it does |
|---|---|
| `make test` | whole suite (unit + integration) |
| `make test-unit` | unit tests only — no network |
| `make test-integration` | integration tests (sqlite / network) |
| `make verify-evidence` | re-derive the measured figures from artifacts |
| `make state` | print phase / gates / next action at a glance |
| `make sources-audit` | re-probe the source list (NETWORK; writes a new dated snapshot) |
| `make legacy-baseline` | re-measure the legacy baseline (NETWORK, slow) |

## 3. The one command-line tool that ships

There is **no installed CLI** (`atlas/cli/` is an empty stub — see §7). The
runnable entry point is the P13 engineering tool, which executes the full
17-step operating pipeline (ADR-041) against the live network:

```bash
# structural check — resolves all 17 steps to real callables, NO network:
python3 engineering/tools/live_transcript.py --dry-run

# live run — --target is REQUIRED (ADR-007: there is no default target).
# Without it the tool prints FATAL and exits 2. It never substitutes one.
python3 engineering/tools/live_transcript.py \
    --target https://example.com \
    --max-sources 6 --max-probes 40 --lease-count 3
```

It writes `engineering/raw/live_transcript_<UTC>.json`: one measured record
per step (config → registry → no-default-target proof → SQLite+WAL → lease
sweep → scheduler plan → live discovery cycle → normalize → endpoint dedup →
TCP/protocol/k=5 probing → p95 admission → persistence → atomic lease →
LEASED-row proof → release → SIGKILL-crash consistency → observability).
A passing run ends `17/17 steps OK`. `admitted=0` is a measurement of
free-proxy reality, not a tool failure.

## 4. Embedding as a library (the supported way)

The engine is the product. The exact wiring below is the one the live
transcript runs (engineering/tools/live_transcript.py, `live()`), so it is
guaranteed current by the `--dry-run` symbol guard:

```python
import asyncio
from pathlib import Path
import aiohttp

from atlas.adapters.config import load_scheduler_policy, load_target_policy
from atlas.adapters.http_source import HttpSourceAdapter
from atlas.adapters.probe_aiohttp import AiohttpProbe
from atlas.adapters.registry import fetchable_sources, load_registry
from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade
from atlas.core.ports.probe import ProbePlan
from atlas.engine.cycle import CycleBudget, DiscoveryEngine
from atlas.engine.handout import HandoutService

CONFIG   = Path("config.yaml")
REGISTRY = Path("atlas/data/sources/sources.json")   # 122 rows, 69 enabled

class SystemClock:                       # satisfies atlas.core.ports.clock.ClockPort
    def now(self): ...
    def monotonic_ms(self): ...
    def deadline(self, after_ms): ...

async def discover(target_url: str) -> None:
    clock  = SystemClock()
    store  = SqliteStore("atlas/data/atlas.db")      # WAL; safe for concurrent readers
    target = Target(url=target_url)                  # REQUIRED — never defaulted
    budget = CycleBudget(max_sources=6, max_probes=40,
                         max_candidates_per_source=60, probe_concurrency=16)
    timeout   = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=12, limit_per_host=2)   # ADR-006
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        engine = DiscoveryEngine(
            source_port=HttpSourceAdapter(session),
            probe=AiohttpProbe(),                    # verify_tls=True by default
            store=store, clock=clock, target=target,
            plan=ProbePlan(),                        # k=5 samples, p95 gate
        )
        report, updated_sources = await engine.run_cycle(
            fetchable_sources(load_registry(REGISTRY)), budget)

asyncio.run(discover("https://example.com"))
```

Handing proxies out to a consumer (lease → validate → rank → grant; the target
is validated **before** anything is leased):

```python
from atlas.core.domain.verdict import Grade

handout = HandoutService(store=store, clock=clock,
                         target_policy=load_target_policy(CONFIG))
result = handout.handout(target=target, count=3, min_grade=Grade.USABLE)
if result.ok:
    for g in result.granted:
        use(g.proxy)                                   # do the work
        store.release(g.proxy.fingerprint, now=clock.now())
else:
    handle(result.refusal)          # e.g. TARGET_REFUSED, POOL_EMPTY
# crashed consumer? leases expire on their own; expire_leases() reclaims them.
```

Rules of the contract you get for free:

- **No double delivery (H3).** `lease()` is a single BEGIN IMMEDIATE
  compare-and-set. Proven: 0 duplicates vs 30 for a naive read-then-write
  store at identical load (`engineering/raw/lease_concurrency.json`).
- **No target, no proxies.** `handout(target=None, ...)` returns refusal
  `NO_TARGET`; denied hosts (config `targets.allow_policy.deny_hosts` —
  login-walled third parties) return `TARGET_REFUSED`. Private ranges and
  metadata hosts are denied by default.
- **Every rejection carries a reason code** (TCP_TIMEOUT, TOO_SLOW_P95,
  UNRELIABLE, …). A silent rejection is considered a bug
  (config `obs.require_reason_code: true`).

## 5. Configuration

Every tunable lives in `config.yaml`, never in code (the legacy tree hardcoded
257 URL literals; this rebuild exists to remove that). Keys you will touch
most:

| Key | Default | Meaning |
|---|---|---|
| `admission.samples_k` | 5 | samples per candidate before the gate decides |
| `admission.max_p95_ms` | 1500 | the central rule; rejects 95.1 % of what the legacy gate admitted |
| `admission.grades.*` | 500 / 1000 / 1500 | elite / good / usable p95 bands |
| `probe.per_host_concurrency` | 8 | legacy used 100–150 threads and caused its own 429s |
| `sources.per_host_concurrency` | 2 | deliberately low (ADR-006) |
| `lease.default_ms` / `max_ms` | 30000 / 300000 | lease duration bounds |
| `scheduler.recheck_ready_after_s` | 900 | an admit an hour old is not still good |
| `targets.default_target` | **null** | MUST stay null (engineering/SECURITY.md) |
| `targets.allow_policy.deny_hosts` | instagram/facebook/tiktok | login-walled, bot-hostile third parties |

Note honestly carried forward: `scheduler.discovery_interval_s` is present in
config but **drives nothing** yet (FINAL_AUDIT.md §6.3).

## 6. Extending the source registry

```bash
# 1. probe candidate provider URLs live and merge into a NEW dated snapshot
#    (pinned snapshots are never overwritten — ADR-010):
python3 engineering/tools/extend_source_registry.py   # edit NEW_ENDPOINTS first

# 2. point the builder at the new snapshot, rebuild the registry:
#    engineering/tools/build_source_registry.py  (SNAPSHOT constant), then:
python3 engineering/tools/build_source_registry.py    # rewrites atlas/data/sources/sources.json

# 3. update the pinned counts the tests assert (grep atlas/tests for the old
#    enabled count), then prove nothing drifted:
make doctor
```

Current registry: 122 URLs probed, **69 ACTIVE** (60 regex-adjacent, 8
HTML-table, 1 JSON), 502 211 unique candidates — snapshot
`engineering/raw/source_probe_20260827T222532Z.json`. Source URLs live in the
registry JSON, never as literals in `.py` files (ADR-002).

## 7. What does NOT ship (read this before designing around it)

Recorded in `engineering/FINAL_AUDIT.md` §6 and charged in the scorecard:

- **No HTTP API.** `atlas/api/` is an empty `__init__.py` stub; the `api:`
  block in config.yaml has no code behind it. Embed the engine (§4).
- **No CLI.** `atlas/cli/` is an empty stub; the live transcript tool (§3) is
  the only runnable entry point.
- **No observability module.** `atlas/obs/` is an empty stub; diagnostics are
  the reason codes on every verdict and the state counts in the store.
- **SOCKS candidates are untestable** without `aiohttp-socks` — they are
  reported as untestable, never as failures.
- **No CAPTCHA/WAF circumvention, by design and permanently.** A target that
  fights automation is a target this system refuses to measure (H5/ADR-007).

## 8. If something looks wrong

1. `make doctor` — if the gate is red, trust the gate, not the prose.
2. `make state` — the resume state (phase, gates, next action).
3. `engineering/BUG_LEDGER.md` — every defect found so far, including the
   three v4 introduced into itself (V4-01…V4-03).
4. `engineering/FINAL_AUDIT.md` — what was measured, with method and n beside
   every percentile; `engineering/SCORECARD.md` — the 91/100 accounting.
