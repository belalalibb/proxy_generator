"""
Percentile methods — PURE, and deliberately duplicated from the baseline tool.

ADR-011 pinned an unusual pair of methods because that is what the legacy tool
provably used: p50 by linear interpolation, p95 by lower/floor rank. Those two
functions reproduce BOTH documented legacy streams exactly (n=102 and n=118),
which is what proves they are the original behaviour rather than a coincidence.

WHY THIS FILE EXISTS AT ALL

ADR-011 closes with a requirement that is easy to read past:

    "`FINAL_AUDIT.md` [...] must compute v4's own p95 with the **same** function
     for the comparison to be honest."

If the admission gate had computed p95 by the ordinary interpolated method while
the baseline used floor rank, then every "v4 p95 vs legacy p95" sentence in the
final audit would be comparing two different statistics that share a name. On
the legacy data that gap is 424.6 ms (16 327.6 interpolated vs 15 903 floor) --
larger than half of v4's entire latency budget.

So the gate uses the floor method for p95, and `test_policy.py` asserts these
implementations agree value-for-value with `engineering/tools/measure_baseline.py`
on shared inputs. The duplication is intentional (the tool is a frozen record of
the 2026-08-24 audit and must not move when production code moves), and the
cross-check is what stops it drifting silently -- the ADR-017 failure mode.
"""
from __future__ import annotations

from statistics import fmean, stdev


def pct_floor(values: list[float] | tuple[float, ...], p: float) -> float:
    """Lower-rank percentile: sorted[int((n-1)*p)]. The documented p95 method."""
    if not values:
        return 0.0
    s = sorted(values)
    return float(s[int((len(s) - 1) * (p / 100.0))])


def pct_linear(values: list[float] | tuple[float, ...], p: float) -> float:
    """Linearly interpolated percentile. The documented p50 method (== median)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def sample_stdev(values: list[float] | tuple[float, ...]) -> float | None:
    """
    Population-consistent stdev, or None when it is undefined.

    Returns None for n<2 instead of 0.0. A single sample has NO measurable
    spread, and reporting 0.0 would state that the proxy is perfectly stable --
    the most flattering possible reading of the least possible evidence. That is
    the same error, in miniature, as the legacy single-sample gate.
    """
    if len(values) < 2:
        return None
    return float(stdev(values))


def mean_ms(values: list[float] | tuple[float, ...]) -> float | None:
    return float(fmean(values)) if values else None


__all__ = ["pct_floor", "pct_linear", "sample_stdev", "mean_ms"]
