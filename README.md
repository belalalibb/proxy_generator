# ATLAS PROXY FABRIC v4

A measured rebuild of a legacy proxy scraper.

> **The one number that justifies this project.** The legacy system measured
> latency **59 times** and compared it against a rejecting threshold **zero
> times**. From its own recorded run it therefore admitted proxies with a
> **p50 of 6 359.5 ms and a p95 of 15 903 ms** (n=102), where **95.8 %** of
> *accepted* proxies were slower than 1 500 ms. A 19-second proxy was recorded as
> a success identical to a 756 ms one.
>
> **`LIVE ≠ GOOD`.** That is the defect this rebuild exists to fix.
> Evidence: [`engineering/BASELINE.json`](engineering/BASELINE.json).

---

## Status

| | |
|---|---|
| Phase | **P01 — ARCHITECTURE** |
| Gate 0 | ✅ PASSED (re-earned after a sync loss — ADR-010) |
| Tests | `19 passed` (`make test`) |
| Sources | **68 ACTIVE** of 123 audited, every record traceable to a real fetch |

Live state: `make state` · Full log: [`engineering/PROGRESS.md`](engineering/PROGRESS.md)

---

## Quick start

```bash
make install      # dependencies
make doctor       # THE GATE: evidence integrity, then tests
make state        # where the project is, and the exact next action
```

### `make doctor` is the gate, not `pytest`

`pytest` alone is **not** accepted as evidence here, and there is a concrete
reason. During a platform sync that dropped `engineering/tools/`, `pytest -q`
reported **“10 passed”** while `atlas/core/` did not exist — the isolation tests
were globbing an empty directory and passing **vacuously**. A test that cannot
fail is not evidence.

So `make doctor` runs [`gate_check.py`](engineering/tools/gate_check.py) **first**,
which fails the build if:

- any task marked `DONE` names an evidence path that is not on disk,
- any tool on disk is untracked by git (that is how the loss happened),
- `atlas/core/` has no modules for the fitness tests to scan (the vacuity check).

The test suite additionally contains **negative controls** that feed known-bad
source to the real guards and assert they still fire. See ADR-010 / ADR-012.

---

## Architecture

Hexagonal, and the boundary is enforced by a **failing test**, not a convention
([`atlas/tests/unit/test_architecture.py`](atlas/tests/unit/test_architecture.py)).

```
atlas/
├── core/                  PURE. no I/O, no network, no clock, no framework
│   ├── domain/            immutable data: Proxy, Source, Target, Verdict, Score
│   ├── policy/            pure decisions: admission gate, scoring, cooldown
│   └── ports/             Protocol interfaces the outer layers implement
├── adapters/              aiohttp, sqlite3, filesystem, clock
├── engine/                staged probe pipeline (S1..S5) + scheduler
├── api/                   HTTP surface
├── obs/                   structured logs + metrics
├── cli/                   operator entry points
└── data/sources/          sources.json — sources are DATA (ADR-002)
```

`core/` is AST-scanned against an **allowlist** of pure-computation stdlib. It may
not import `socket`, `asyncio`, `sqlite3`, `os`, `pathlib`, `aiohttp`, `fastapi`,
or any of `atlas.adapters/api/engine`. Why it matters: the legacy tree fused
fetching, parsing, testing and persistence into single functions, so none of its
logic could be tested without a live network.

**`ClockPort` is part of that rule.** The legacy tree called `time.sleep()` 13
times inside its control flow, making cooldown logic untestable without actually
waiting. Time is injected, so ADR-006's exponential backoff is verified in
microseconds.

---

## What changed, and the measurement behind it

| Legacy behaviour | v4 | Evidence |
|---|---|---|
| Accept on `status==200 and len>1000`, **1 sample** | **k=5 samples, admit on p95** ≤ 1500 ms | p95 15 903 ms admitted (n=102) |
| Latency never gated | Graded `Verdict` + `ReasonCode`, never a bool | 59 measurements, 0 comparisons |
| 257 hardcoded URL literals in 6 files | `sources.json`, per-source stats | `raw/bug_scan.json` |
| Trusts the source's protocol label | **Discovers** protocol (ADR-005) | a SOCKS list named `http.txt`, 2 853 candidates discarded |
| `proxy.txt`, no `LEASED` state | SQLite WAL + atomic compare-and-set lease | H3 was unachievable on a text file |
| `save()` truncates with `open(...,'w')` | `.tmp` + `os.replace()` | 8 truncating writes (B-04) |
| 23 silent `except: pass` handlers | Every failure carries a `ReasonCode` | 35 dead URLs retried forever |
| TLS verification disabled in 9 places | TLS always on | MITM indistinguishable (B-09) |
| One bad fetch ⇒ source treated as dead | Cooldown on **consecutive** failures | GeoNode: 230 067 B → 659 B → 230 019 B |
| Ships a 2captcha client | **Refused, not ported** | ADR-007, `SECURITY.md` |
| Defaults to probing a login-walled site | **No default target, ever** | ADR-007 |

---

## Things this project will not do

Recorded in [`engineering/SECURITY.md`](engineering/SECURITY.md) and ADR-007:

1. **No CAPTCHA / WAF / auth circumvention.** The legacy tree shipped a working
   2captcha client. It was deleted, not ported, and no replacement exists —
   shipping the capability *is* the violation.
2. **No default target.** `GET /api/proxies` without an explicit `url` is an
   error by design, so the operator always names what they are probing. This also
   removes the legacy conflation of *target difficulty* with *proxy quality*: the
   0.68 % legacy success rate substantially measured one site's bot defences.

---

## Honesty rules

This project treats its own numbers as claims that must be verifiable:

- **H1 — evidence for every claim.** A figure with no on-disk artifact may not be
  cited. Enforced mechanically by `gate_check.py`.
- **H2 — no fabricated numbers.** Where a re-run disagrees with a documented
  figure, both are kept and the delta is explained by a *named cause*
  ([`RECONCILIATION.md`](engineering/RECONCILIATION.md)) — never silently
  overwritten to look consistent.
- **Bias is disclosed.** Re-testing the old `proxy.txt` today gives a *flattering*
  p95 of 1 464 ms, but that is **survivorship bias** on 9 survivors of a 9-month-old
  list. The honest comparison target is the historical admitted distribution
  (n=102). See ADR-009.

---

## Documentation map

| File | Contents |
|---|---|
| [`engineering/ANALYSIS.md`](engineering/ANALYSIS.md) | Per-file verdicts, source inventory, data archaeology |
| [`engineering/BASELINE.json`](engineering/BASELINE.json) | The measured legacy baseline |
| [`engineering/BUG_LEDGER.md`](engineering/BUG_LEDGER.md) | 16 defect classes, `file:line` each |
| [`engineering/MIGRATION_LEDGER.md`](engineering/MIGRATION_LEDGER.md) | 80 legacy features, 0 unknown |
| [`engineering/DECISIONS.md`](engineering/DECISIONS.md) | ADRs with alternatives + consequences |
| [`engineering/TASK_STATE.json`](engineering/TASK_STATE.json) | Machine-readable resume state |
| [`config.yaml`](config.yaml) | Every tunable, each citing its justification |

The legacy scripts (`v1.py`, `v2.py`, `v3.py`, `proxy_generator_v2.py`, `bebo.py`,
`proxychecker.py`) are left **untouched and runnable** on purpose, so the baseline
stays reproducible (ADR-001).
