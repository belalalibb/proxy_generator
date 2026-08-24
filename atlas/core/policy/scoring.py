"""
SCORING — ranking READY proxies, and the answer to B-16 (ageing).

WHAT IT REPLACES

`proxy.txt` has no timestamps. A proxy written into that file stays "working"
forever: nothing re-verifies it, nothing evicts it, nothing records when it was
last seen alive. The measured consequence, re-testing that exact file today
(engineering/BASELINE.json, seed 1337, reproducible):

    3.0% live (9 of 300)

97% of the legacy "working" list was stale. That is not a proxy-quality problem;
it is a *bookkeeping* problem. The list could not distinguish "verified 8 seconds
ago" from "verified last year", so both ranked identically and both were served.

WHY THE ADMISSION GATE IS NOT ENOUGH

admission.py answers "is this proxy good ENOUGH to enter the pool?" — a yes/no
decision made at ONE instant, against the evidence available at that instant. It
has no opinion about which of two admitted proxies to hand out first, and no
opinion at all about the passage of time. A gate that runs once cannot express
decay: the moment it says OK, its verdict is frozen.

Scoring is the complementary question: "of the proxies that passed, which is
worth handing out NOW?" — and `now` is the load-bearing word. That is why this
module takes the current time as an ARGUMENT rather than reading a clock: core/
does not read the clock (ClockPort), so freshness stays a pure function of two
timestamps and every decay rule below is verifiable without waiting.

FOUR TERMS, AND WHY EACH IS SEPARATE

  latency      p95, the same statistic the gate uses. Never `min` (cosmetic),
               never a single sample (the legacy defect).
  reliability  success_rate over the proxy's whole history, which is a DIFFERENT
               fact from the success_ratio of one probe burst: a proxy can pass
               k=5 samples cleanly today and still have failed 40% of the time
               over its life.
  freshness    decays with age. The B-16 term. Without it the other three are
               statements about the past presented as claims about the present.
  anonymity    a transparent proxy forwards the client IP, so it fails the one
               job it was chosen for regardless of speed.

Collapsing any pair into one number loses the distinction that makes the pool
rankable — the legacy pool stored ONE number per proxy and therefore could not
rank at all.

THE INVERSION, REPEATED FROM admission.py

Absence of evidence scores ZERO, never a neutral or flattering default. An
unmeasured proxy must not outrank a measured mediocre one, and a never-checked
proxy is not "fresh" — it is unverified. A scoring function whose failure mode is
"unknown looks good" would reintroduce H7 through the ranking instead of through
the gate, which is exactly how ADR-024 got in (a defect in the estimator rather
than in the threshold).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from atlas.core.domain.proxy import Anonymity, Proxy
from atlas.core.domain.verdict import Grade, Score


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """
    Weights and decay constants. Every value is an argument, not a literal.

    The weights must sum to 1.0 so the result is interpretable as a 0..1 quality
    figure AND so `Score.value` cannot silently leave its own validated range.
    That is asserted at construction: a mis-weighted policy is a configuration
    error, and it must fail loudly here rather than by raising deep inside
    Score.__post_init__ during a live lease.
    """

    w_latency: float = 0.35
    w_reliability: float = 0.30
    w_freshness: float = 0.25
    w_anonymity: float = 0.10

    # latency reference: p95 at or above this scores 0. Defaults to the gate's
    # own ceiling so the two modules cannot disagree about what "slow" means.
    latency_budget_ms: float = 1500.0

    # freshness half-life: after this many seconds the freshness term halves.
    # 900s mirrors config.yaml scheduler.recheck_ready_after_s -- a proxy admitted
    # an hour ago is not still known-good.
    freshness_half_life_s: float = 900.0

    # beyond this age a proxy is STALE: freshness is pinned to 0 and the proxy
    # must be re-verified before it is handed out again (B-16).
    max_age_s: float = 3600.0

    def __post_init__(self) -> None:
        weights = (self.w_latency, self.w_reliability,
                   self.w_freshness, self.w_anonymity)
        if any(w < 0.0 for w in weights):
            raise ValueError(f"weights must be non-negative, got {weights}")
        total = math.fsum(weights)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"weights must sum to 1.0, got {total!r} from {weights}: "
                "an unnormalised policy makes Score.value uninterpretable"
            )
        if self.latency_budget_ms <= 0:
            raise ValueError(
                f"latency_budget_ms must be > 0, got {self.latency_budget_ms}"
            )
        if self.freshness_half_life_s <= 0:
            raise ValueError(
                f"freshness_half_life_s must be > 0, got {self.freshness_half_life_s}"
            )
        if self.max_age_s <= 0:
            raise ValueError(f"max_age_s must be > 0, got {self.max_age_s}")
        if self.max_age_s < self.freshness_half_life_s:
            raise ValueError(
                f"max_age_s ({self.max_age_s}) < freshness_half_life_s "
                f"({self.freshness_half_life_s}): the proxy would be declared "
                "stale before its freshness term had even halved"
            )

    # B-16 named `evict_after_failures`; the pool expresses that as
    # ProxyState.RETIRED driven by consecutive_failures, so it is a scheduler
    # decision rather than a scoring one and deliberately does not appear here.


def _clamp01(x: float) -> float:
    """
    Force a component into 0..1.

    Not defensive noise. `Score.__post_init__` rejects any value outside 0..1, so
    an un-clamped component turns a data anomaly -- a negative age from clock
    skew, a success_rate above 1 from a corrupted counter -- into a ValueError
    raised in the middle of a lease. Clamping keeps the ranking usable and lets
    the anomaly be reported by whoever owns the data, instead of taking the
    serving path down with it.
    """
    if x != x:                     # NaN: propagating it would make every
        return 0.0                 # comparison false and silently unrank the proxy
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def latency_term(proxy: Proxy, policy: ScoringPolicy) -> float:
    """
    1.0 at 0 ms, falling linearly to 0.0 at `latency_budget_ms`.

    Uses p95 -- the same statistic as the gate (ADR-011/ADR-024), so a proxy
    cannot be admitted on its tail and then ranked on its best case. An
    UNMEASURED proxy scores 0.0, not a neutral 0.5: see the inversion note above.
    """
    p95 = proxy.latency.p95_ms
    if p95 is None:
        return 0.0
    return _clamp01(1.0 - (p95 / policy.latency_budget_ms))


def reliability_term(proxy: Proxy) -> float:
    """
    Lifetime success_rate = total_successes / total_attempts.

    Distinct from the per-burst success_ratio the gate reads. `None` (no attempt
    ever recorded) scores 0.0 -- an unproven proxy does not get to look reliable.
    """
    rate = proxy.success_rate
    if rate is None:
        return 0.0
    return _clamp01(rate)


def freshness_term(proxy: Proxy, policy: ScoringPolicy, *, now: datetime) -> float:
    """
    THE B-16 TERM. Exponential decay: 0.5 ** (age / half_life), pinned to 0.0
    once age exceeds `max_age_s`.

    A proxy that has never been checked scores 0.0. `last_checked is None` means
    unverified, and unverified is the *least* fresh state there is -- treating a
    missing timestamp as "just checked" is precisely how proxy.txt kept 97% dead
    entries at full rank.

    Exponential rather than linear because staleness risk compounds: the chance a
    proxy is still alive after 2 half-lives is not 0 just because it is not 1.

    Negative age (last_checked in the FUTURE) is clamped to a freshness of 1.0
    rather than allowed to exceed it. Clock skew between a writer and a reader is
    a real operational condition, and a proxy must never be able to score ABOVE
    a genuinely just-checked one by carrying a wrong timestamp.
    """
    if proxy.last_checked is None:
        return 0.0
    age_s = (now - proxy.last_checked).total_seconds()
    if age_s < 0.0:
        return 1.0
    if age_s >= policy.max_age_s:
        return 0.0
    return _clamp01(0.5 ** (age_s / policy.freshness_half_life_s))


def anonymity_term(proxy: Proxy) -> float:
    """
    ELITE 1.0 / ANONYMOUS 0.7 / UNKNOWN 0.0 / TRANSPARENT 0.0.

    TRANSPARENT and UNKNOWN share a score deliberately. A transparent proxy is
    *known* to leak the client IP; an unknown one is *unproven*, and this module
    does not reward unproven. They differ in what the gate does about them
    (TRANSPARENT_LEAK is a rejection), not in how they rank.
    """
    return {
        Anonymity.ELITE: 1.0,
        Anonymity.ANONYMOUS: 0.7,
        Anonymity.TRANSPARENT: 0.0,
        Anonymity.UNKNOWN: 0.0,
    }[proxy.anonymity]


def score_proxy(proxy: Proxy, policy: ScoringPolicy, *, now: datetime) -> Score:
    """
    The weighted score, with every component retained on the result.

    Components are kept, not just the total, because a single float cannot be
    argued with. When a proxy ranks low, the caller must be able to say WHY it
    ranked low -- the same reason every rejection in this codebase carries a
    ReasonCode (B-02: 23 silent handlers, no diagnosable failure anywhere).

    `now` is injected. core/ never reads the clock (ClockPort), so this whole
    decay rule is testable in microseconds against fixed timestamps instead of
    by waiting an hour.
    """
    lat = latency_term(proxy, policy)
    rel = reliability_term(proxy)
    fresh = freshness_term(proxy, policy, now=now)
    anon = anonymity_term(proxy)

    value = math.fsum((
        policy.w_latency * lat,
        policy.w_reliability * rel,
        policy.w_freshness * fresh,
        policy.w_anonymity * anon,
    ))
    return Score(
        value=_clamp01(value),
        speed_component=lat,
        reliability_component=rel,
        freshness_component=fresh,
        anonymity_component=anon,
    )


def is_stale(proxy: Proxy, policy: ScoringPolicy, *, now: datetime) -> bool:
    """
    Does this proxy need re-verification before it may be handed out again?

    True when it has NEVER been checked, or when its age has passed `max_age_s`.
    This is the predicate proxy.txt could not express, and the direct answer to
    "a proxy validated once must not stay 'working' forever".

    Note the asymmetry with freshness_term: a future timestamp yields freshness
    1.0 and stale False. Both readings treat skew as "recently checked" rather
    than inventing an age, and they agree with each other -- an unstale proxy is
    never scored as though it were ancient, and vice versa.
    """
    if proxy.last_checked is None:
        return True
    age_s = (now - proxy.last_checked).total_seconds()
    if age_s < 0.0:
        return False
    return age_s >= policy.max_age_s


def rank(proxies: tuple[Proxy, ...], policy: ScoringPolicy, *,
         now: datetime, min_grade: Grade = Grade.USABLE,
         include_stale: bool = False) -> tuple[tuple[Proxy, Score], ...]:
    """
    Order proxies best-first, dropping anything below `min_grade`.

    Stale proxies are EXCLUDED by default. Their score already decays to the
    freshness floor, but a low score still ranks -- and "ranks last" is not the
    same as "must not be served". With 97% of the legacy list stale, decay alone
    would still have handed out dead proxies whenever the pool was thin, so
    staleness is a filter and not merely a penalty.

    Ties break on fingerprint, so the order is TOTAL and deterministic. Without
    that, two equally-scored proxies would come back in whatever order the store
    happened to yield, and the 500-cycle rotation-fairness simulation could not
    distinguish real bias from arbitrary ordering.
    """
    out: list[tuple[Proxy, Score]] = []
    for p in proxies:
        if not p.grade.meets(min_grade):
            continue
        if not include_stale and is_stale(p, policy, now=now):
            continue
        out.append((p, score_proxy(p, policy, now=now)))
    out.sort(key=lambda pair: (-pair[1].value, pair[0].fingerprint))
    return tuple(out)


__all__ = [
    "ScoringPolicy", "score_proxy", "is_stale", "rank",
    "latency_term", "reliability_term", "freshness_term", "anonymity_term",
]
