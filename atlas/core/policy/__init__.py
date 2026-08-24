"""
POLICY — pure decision logic. No I/O, no clock, no randomness.

`admission.py` is the gate the legacy system did not have (H7/ADR-003).
`normalize.py` is the seam from raw source text to pool objects (ADR-019).
`percentile.py` pins the ADR-011 methods so v4's p95 is comparable to legacy's.
"""
from atlas.core.policy.admission import (
    AdmissionPolicy, build_profile, decide, decide_for, grade_for,
)
from atlas.core.policy.normalize import (
    DropReason, NormalizeReport, NormalizedCandidate,
    normalize_batch, normalize_one, split_scheme, to_proxies,
)
from atlas.core.policy.percentile import mean_ms, pct_floor, pct_linear, sample_stdev

__all__ = [
    "AdmissionPolicy", "build_profile", "decide", "decide_for", "grade_for",
    "DropReason", "NormalizeReport", "NormalizedCandidate",
    "normalize_batch", "normalize_one", "split_scheme", "to_proxies",
    "mean_ms", "pct_floor", "pct_linear", "sample_stdev",
]
