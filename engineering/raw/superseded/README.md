# Superseded artifacts — preserved BECAUSE they are defective

These files are the primary evidence for the defects in
`engineering/BUG_LEDGER.md` § "Defects introduced by v4 itself". They are kept
verbatim and never regenerated.

| file | records | defect |
|---|---|---|
| `calib_smoke_PREFIX_V4-01.json` | `tcp_ok 24, reached_gate 0, PROTO_MISMATCH 24` | V4-01 / ADR-025 — a measured cause overwritten by "socks4 not testable" |
| `admission_live_PREFIX_V4-02.json` | `samples [7659.2, 4100.7], p50 5880.0, p95 4100.7` | V4-02 / ADR-024 — floor-rank p95 returned the minimum at k=2 |

They live in `superseded/` rather than in `raw/` because
`gate_check.check_no_percentile_ordering_violation` scans `raw/*.json` for
`p95 < p50`, and `admission_live_PREFIX_V4-02.json` contains exactly that by
design. The separation is STRUCTURAL — a directory boundary the glob does not
cross — not an exclusion list naming this file, because an exclusion list is a
thing someone later adds a second entry to (ADR-023).

Current, post-fix artifacts stay in `engineering/raw/` and must pass the gate.
