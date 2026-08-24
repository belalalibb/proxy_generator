"""
THE ADMISSION GATE — the single most important file in this rebuild (H7, ADR-003).

WHAT IT REPLACES

The legacy gate, `proxy_generator_v2.py:380`, was:

    if response.status_code == 200 and len(response.text) > 1000:

One sample. No latency comparison anywhere. A mechanical scan found latency
measured **59 times** in the legacy tree and compared against a rejecting
threshold **zero times**. From the legacy system's own recorded run (n=102,
engineering/BASELINE.json):

    p50 6 359.5 ms | mean 7 145.1 ms | p95 15 903 ms | max 19 035 ms
    95.1% of ACCEPTED proxies were slower than 1 500 ms
    58.8% were slower than 5 000 ms
    (n=102, proxy_details.json. The n=118 log stream reads 95.8% / 56.8% --
     ADR-020: real numbers, wrong sentence when spliced together.)

A 19 035 ms proxy was recorded as a success indistinguishable from a 756 ms one.
That is the H7 violation -- LIVE != GOOD -- stated as data rather than opinion.

THE FOUR RULES, AND WHY EACH ONE EXISTS

  1. p95 of k=5 samples, never one sample, never `min`.
     `min` measures the best moment a proxy ever had; a consumer experiences the
     distribution. §8 calls `min` cosmetic and it is excluded structurally: this
     module never calls it.

  2. Zero evidence is a REJECTION, not a default-admit (NOT_MEASURED).
     This is the inversion that matters most. The legacy default was to admit
     anything that answered once; here, absence of measurement is refusal. A gate
     whose failure mode is "let it through" is not a gate.

  3. Reliability is separate from speed (min_success_ratio).
     A proxy that answers in 200 ms four times out of ten is worse than a steady
     900 ms one. The legacy pool could not express that distinction at all,
     because it stored one number per proxy.

  4. Jitter is separate from both (stdev/p50).
     Fast-but-erratic reads as fast under any single-sample or mean-based rule.

ORDER IS PART OF THE CONTRACT

Checks run cheapest-and-most-fundamental first, and the FIRST failure is the
reason reported. So an unmeasured proxy is NOT_MEASURED (not TOO_SLOW_P95), and
an unreliable one is UNRELIABLE (not TOO_JITTERY). One rejection, one cause,
because a reason code that depends on evaluation order is not diagnostic --
and B-02 (23 silent handlers) is what happens when nobody can name a cause.
"""
from __future__ import annotations

from dataclasses import dataclass

from atlas.core.domain.proxy import Anonymity, LatencyProfile, Proxy
from atlas.core.domain.verdict import Grade, ReasonCode, Verdict
from atlas.core.policy.percentile import mean_ms, pct_linear, pct_tail, sample_stdev


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """
    Every threshold, with the measurement that justifies it.

    Defaults mirror config.yaml. They are arguments, not constants, because P06
    calibrates them against real candidates -- and a threshold that cannot be
    changed without editing code is the legacy defect (257 hardcoded literals).
    """

    samples_k: int = 5                 # ADR-003
    max_p95_ms: float = 1500.0         # rejects 95.1% of what legacy admitted (n=102)
    max_jitter: float = 0.5            # stdev/p50
    min_success_ratio: float = 0.6     # of the k attempted samples
    elite_p95_ms: float = 500.0
    good_p95_ms: float = 1000.0
    usable_p95_ms: float = 1500.0
    require_anonymity_for_elite: bool = True

    def __post_init__(self) -> None:
        # A policy that cannot admit anything, or that grades incoherently, is a
        # configuration error and must fail loudly at construction rather than
        # silently reject every proxy at 3 a.m.
        if self.samples_k < 1:
            raise ValueError(f"samples_k must be >= 1, got {self.samples_k}")
        if not (self.elite_p95_ms <= self.good_p95_ms <= self.usable_p95_ms):
            raise ValueError(
                "grade thresholds must be non-decreasing: "
                f"elite={self.elite_p95_ms} good={self.good_p95_ms} "
                f"usable={self.usable_p95_ms}"
            )
        if self.usable_p95_ms > self.max_p95_ms:
            raise ValueError(
                f"usable_p95_ms ({self.usable_p95_ms}) exceeds max_p95_ms "
                f"({self.max_p95_ms}): every 'usable' proxy would be rejected"
            )
        if not (0.0 < self.min_success_ratio <= 1.0):
            raise ValueError(
                f"min_success_ratio must be in (0,1], got {self.min_success_ratio}"
            )


def build_profile(
    samples_ms: tuple[float, ...], *, attempted: int
) -> LatencyProfile:
    """
    Build a LatencyProfile from the SUCCESSFUL samples plus how many were tried.

    `attempted` is a separate argument on purpose. If success_ratio were derived
    from len(samples_ms) alone it would always be 1.0 -- the failures are exactly
    the samples that are missing from the list. Passing the denominator in is what
    makes UNRELIABLE detectable at all.

    p95 uses pct_tail: the ADR-011 floor rank for every k >= 3 (so v4's p95 stays
    directly comparable to the legacy 15 903 ms figure), corrected at k == 2 where
    the floor rank returns the MINIMUM and would admit a proxy measured over
    budget. See pct_tail and ADR-024.
    """
    if attempted < len(samples_ms):
        raise ValueError(
            f"attempted ({attempted}) < successful samples ({len(samples_ms)}): "
            "the denominator cannot be smaller than the numerator"
        )
    if not samples_ms:
        # No successful sample: the profile is honestly EMPTY. p95 stays None so
        # the gate reports NOT_MEASURED rather than comparing against a fake 0.0,
        # which would read as "infinitely fast" and admit it.
        return LatencyProfile(
            samples_ms=(),
            success_ratio=0.0 if attempted else None,
        )
    return LatencyProfile(
        samples_ms=tuple(samples_ms),
        p50_ms=round(pct_linear(samples_ms, 50), 1),
        p95_ms=round(pct_tail(samples_ms, 95), 1),
        mean_ms=round(mean_ms(samples_ms) or 0.0, 1),
        stdev_ms=(round(s, 1) if (s := sample_stdev(samples_ms)) is not None else None),
        success_ratio=(len(samples_ms) / attempted) if attempted else None,
    )


def grade_for(p95_ms: float, policy: AdmissionPolicy,
              anonymity: Anonymity = Anonymity.UNKNOWN) -> Grade:
    """
    Map a p95 to a grade.

    ELITE additionally requires proven anonymity when the policy says so: a
    transparent proxy forwards the client IP, so being fast is irrelevant --
    it fails at the one job the caller wanted. UNKNOWN anonymity is not
    promoted to ELITE either, because unmeasured is not the same as good
    (the same inversion as rule 2 above).
    """
    if p95_ms <= policy.elite_p95_ms:
        if not policy.require_anonymity_for_elite:
            return Grade.ELITE
        if anonymity in (Anonymity.ELITE, Anonymity.ANONYMOUS):
            return Grade.ELITE
        return Grade.GOOD          # fast, but anonymity unproven
    if p95_ms <= policy.good_p95_ms:
        return Grade.GOOD
    if p95_ms <= policy.usable_p95_ms:
        return Grade.USABLE
    return Grade.REJECTED


def decide(
    latency: LatencyProfile,
    policy: AdmissionPolicy,
    *,
    anonymity: Anonymity = Anonymity.UNKNOWN,
    transparent_leak: bool = False,
    content_mismatch: bool = False,
    protocol_mismatch: bool = False,
) -> Verdict:
    """
    The gate. Pure: no clock, no I/O, no randomness -- so it is fully testable.

    Integrity failures are checked BEFORE speed, because a fast proxy that leaks
    the client IP or rewrites the body is not a usable proxy at any latency.
    Ordering them after speed would let a 200 ms MITM proxy be graded ELITE.

    PROTO_MISMATCH is deliberately NOT a rejection: ADR-005 established that the
    source's label is a hint, and a SOCKS list named http.txt (2 853 candidates
    the legacy code discarded) is a mislabelled SOURCE, not a bad proxy. It is
    reported in `detail` so the registry label can be corrected.
    """
    # ── integrity first ───────────────────────────────────────────────────────
    if transparent_leak:
        return Verdict.reject(
            ReasonCode.TRANSPARENT_LEAK,
            "proxy forwarded the client IP: anonymity is the point, so speed is moot",
        )
    if content_mismatch:
        return Verdict.reject(
            ReasonCode.CONTENT_MISMATCH,
            "body differed from the direct fetch: interception suspected",
        )

    # ── rule 2: zero evidence is a refusal, never a default admit ─────────────
    if not latency.measured:
        return Verdict.reject(
            ReasonCode.NOT_MEASURED,
            f"no successful sample of {policy.samples_k}; admission requires "
            "evidence, and absence of evidence is not permission (H7)",
        )

    # ── rule 3: reliability, before speed ─────────────────────────────────────
    if latency.success_ratio is not None and latency.success_ratio < policy.min_success_ratio:
        return Verdict.reject(
            ReasonCode.UNRELIABLE,
            f"success_ratio {latency.success_ratio:.2f} < "
            f"{policy.min_success_ratio:.2f} across k={policy.samples_k}",
        )

    # ── rule 1: p95, the gate the legacy system never had ─────────────────────
    p95 = latency.p95_ms
    assert p95 is not None      # guaranteed by latency.measured above
    if p95 > policy.max_p95_ms:
        return Verdict.reject(
            ReasonCode.TOO_SLOW_P95,
            f"p95 {p95:.0f}ms > {policy.max_p95_ms:.0f}ms "
            f"(legacy admitted a p95 of 15903ms)",
        )

    # ── rule 4: jitter, independent of both ───────────────────────────────────
    jitter = latency.jitter
    if jitter is not None and jitter > policy.max_jitter:
        return Verdict.reject(
            ReasonCode.TOO_JITTERY,
            f"jitter {jitter:.2f} > {policy.max_jitter:.2f}: fast on average "
            "but unpredictable per request",
        )

    grade = grade_for(p95, policy, anonymity)
    if grade is Grade.REJECTED:
        # Reachable only if usable_p95_ms < max_p95_ms: a proxy inside the hard
        # ceiling but outside every grade band. Named explicitly rather than
        # crashing Verdict.accept() with a contradiction.
        return Verdict.reject(
            ReasonCode.TOO_SLOW_P95,
            f"p95 {p95:.0f}ms is within max_p95_ms but outside every grade band "
            f"(usable<={policy.usable_p95_ms:.0f}ms)",
        )

    detail = f"p95 {p95:.0f}ms over {len(latency.samples_ms)} sample(s)"
    if protocol_mismatch:
        # Not a rejection (ADR-005) -- a correction the registry should absorb.
        detail += "; NOTE protocol label mismatch: the SOURCE label is wrong"
    return Verdict.accept(grade, detail)


def decide_for(proxy: Proxy, policy: AdmissionPolicy, **kw) -> Verdict:
    """Convenience wrapper: reads anonymity and label mismatch off the Proxy."""
    return decide(
        proxy.latency, policy,
        anonymity=proxy.anonymity,
        protocol_mismatch=proxy.protocol_mismatch,
        **kw,
    )


__all__ = ["AdmissionPolicy", "build_profile", "decide", "decide_for", "grade_for"]
