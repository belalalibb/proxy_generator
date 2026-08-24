"""
HAND-OUT — the layer above the lease, and the place P07 scoring becomes
load-bearing instead of decorative (ADR-033).

WHAT ALREADY EXISTED, AND IS NOT REBUILT HERE

`SqliteStore.lease()` is already a single `BEGIN IMMEDIATE` compare-and-set
(P05.T1), proven under real process concurrency against a committed naive
negative control (P05.T3, 0 vs 30 duplicates), with an independent append-only
`lease_log` audit (P05.T5). H3 -- NO DOUBLE DELIVERY -- is settled at the store.
This module does NOT re-implement leasing, and must never be read as a second
attempt at it.

WHAT WAS MISSING

Four things sat between a proven lease and a usable hand-out:

  1. the caller's target was never validated at lease time (ADR-029 built the
     policy; nothing on the serving path called it),
  2. `lease()` orders by p95 in SQL, so the four-term P07 score had no
     influence on WHICH proxies a caller received,
  3. nothing released the proxies a caller did not end up using, so an
     over-fetch leaked capacity until the lease expired,
  4. a failed proxy had no path back to the pool short of lease expiry.

THE ORDERING PROBLEM (ADR-033)

The store's `ORDER BY (p95_ms IS NULL), p95_ms ASC` is a ONE-TERM proxy for
quality. P07 scores on four: latency, reliability, freshness, anonymity. If this
module leased exactly `count` rows and then ranked them, the ranking would only
permute an already-claimed set -- it could not change which proxies the caller
got. Scoring would be a computation nobody acted on, which is the ADR-019 /
ADR-029 defect class (a captured fact, or a policy, that nothing reads) for the
third and fourth time.

So the hand-out OVER-SELECTS: it leases up to `count * overselect` candidates,
ranks that pool with the full P07 score, grants the best `count`, and RELEASES
the remainder immediately. Over-selection is bounded and the surplus is returned
in a `finally`, because holding rows nobody will use is exactly the capacity leak
this module exists to close.

WHY THE SURPLUS RELEASE IS IN A `finally`

An exception between lease and grant would otherwise strand every leased row
until `lease_expires_at`. With a thin pool and a 30 s lease that is a
self-inflicted outage, and it would be invisible: the rows are not lost, just
unavailable, so no error names the cause. B-02 (23 silent handlers) is what
un-named unavailability costs.

FRESHNESS, AND A CONFIG TENSION THIS MODULE REFUSES TO HIDE

B-16 specifies `target_ttl` = 90 s for per-target validity. `config.yaml` sets
`scheduler.recheck_ready_after_s: 900`. Those two numbers cannot both be
satisfied: a pool re-checked every 900 s cannot offer proxies validated within
90 s, so a hand-out that REFUSED everything older than 90 s would serve almost
nothing, and one that ignored the TTL would silently present 900-second-old
evidence as current.

This module does neither. It hands the proxy out and reports
`revalidation_required` per proxy plus a count on the result, so the caller
learns that the evidence predates the TTL instead of inferring currency from
silence. Reconciling the two config values is a scheduler decision (P09/P11),
recorded as such rather than settled here by picking whichever number made this
file simpler.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

* No per-TARGET validation history. The schema records ONE `last_checked` per
  proxy, against whatever target discovery used -- not per (proxy, target). So
  this module may say "this proxy was verified 40 s ago"; it may NOT say
  "verified against YOUR target 40 s ago", and it does not. Making that claim
  truthfully needs a per-target table, which is a schema change and is named as
  future scope instead of being implied by an optimistic field name.
* No rate limiting. `max_requests_per_host_per_min` needs shared state and a
  clock; ADR-029 already recorded it as P09 API scope, and a second fake
  enforcement point would be worse than none.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol as TypingProtocol

from atlas.core.domain.proxy import Proxy
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade, Score
from atlas.core.policy.scoring import ScoringPolicy, rank
from atlas.core.policy.target_policy import (
    TargetNotAllowed, TargetPolicy, check_target,
)
from atlas.core.ports.clock import ClockPort


class HandoutRefusal(str):
    """
    Named refusal reasons, in the style of `TargetRefusal` and `DropReason`.

    A hand-out that returned an empty tuple for every failure would be
    indistinguishable from an empty pool, and the caller could not tell "your
    target is denied" from "we have nothing right now" -- two problems with
    completely different fixes.
    """

    POOL_EMPTY = "POOL_EMPTY"            # nothing leasable at that grade
    ALL_STALE = "ALL_STALE"              # leased rows existed but all were stale
    TARGET_REFUSED = "TARGET_REFUSED"    # ADR-029; carries the TargetRefusal


@dataclass(frozen=True, slots=True)
class HandoutPolicy:
    """
    Bounds on one hand-out. Every value is an argument, never a literal at a
    call site.

    `overselect` is what makes P07 scoring matter (ADR-033): the store picks a
    candidate pool by p95, and the score picks the winners out of it. At 1 it
    would be off, and the score could no longer influence selection at all --
    so 1 is permitted (a caller may want the cheapest possible query) but it is
    NOT the default, and the docstring says what turning it off costs.

    It is bounded above because every over-selected row is briefly unavailable
    to other consumers. An unbounded factor would let one caller lease the pool
    to rank it, which trades a ranking improvement for a starvation risk.
    """

    max_count: int = 50
    default_lease_ms: int = 30_000        # config.yaml lease.default_ms
    max_lease_ms: int = 300_000           # config.yaml lease.max_ms
    overselect: int = 3
    max_overselect_rows: int = 200
    target_ttl_s: float = 90.0            # B-16
    reclaim_expired_first: bool = True

    def __post_init__(self) -> None:
        if self.max_count < 1:
            raise ValueError(f"max_count must be >= 1, got {self.max_count}")
        if self.default_lease_ms < 1:
            raise ValueError(
                f"default_lease_ms must be >= 1, got {self.default_lease_ms}"
            )
        if self.max_lease_ms < self.default_lease_ms:
            raise ValueError(
                f"max_lease_ms ({self.max_lease_ms}) < default_lease_ms "
                f"({self.default_lease_ms}): the default would be unrequestable"
            )
        if self.overselect < 1:
            raise ValueError(
                f"overselect must be >= 1, got {self.overselect}; 1 disables "
                "score-based selection, below 1 is meaningless"
            )
        if self.max_overselect_rows < self.max_count:
            raise ValueError(
                f"max_overselect_rows ({self.max_overselect_rows}) < max_count "
                f"({self.max_count}): the row cap would truncate a legal request "
                "below the count it asked for"
            )
        if self.target_ttl_s <= 0:
            raise ValueError(f"target_ttl_s must be > 0, got {self.target_ttl_s}")


@dataclass(frozen=True, slots=True)
class Granted:
    """
    One handed-out proxy, with the evidence behind the decision.

    The `Score` is carried, not just the ordering it produced, for the same
    reason `score_proxy` retains its components: a caller who receives a
    mediocre proxy must be able to see WHY it still ranked best, and "trust the
    sort" is not an answer anyone can act on.
    """

    proxy: Proxy
    score: Score
    revalidation_required: bool
    age_s: float | None

    @property
    def fingerprint(self) -> str:
        return self.proxy.fingerprint


@dataclass(frozen=True, slots=True)
class HandoutResult:
    """
    One hand-out, fully accounted -- in the style of `CycleReport`.

    The accounting identity is CHECKED, not trusted: every leased row must end
    up in exactly one bucket. A row that is neither granted nor released is a
    capacity leak, and the whole reason this class exists is that such a leak is
    otherwise invisible (the rows come back on lease expiry, so the pool just
    looks smaller for a while).
    """

    granted: tuple[Granted, ...] = ()
    refusal: str | None = None
    target_refusal: str | None = None
    leased: int = 0
    released_surplus: int = 0
    released_unusable: int = 0
    reclaimed_expired: int = 0
    revalidation_required: int = 0
    lease_ms: int = 0

    def __post_init__(self) -> None:
        accounted = (
            len(self.granted) + self.released_surplus + self.released_unusable
        )
        if self.leased != accounted:
            raise ValueError(
                f"lease accounting lost rows: leased={self.leased} but "
                f"granted+released_surplus+released_unusable={accounted}. Every "
                "leased proxy must be granted or released (H3 capacity leak)."
            )
        if self.revalidation_required > len(self.granted):
            raise ValueError(
                f"revalidation_required ({self.revalidation_required}) exceeds "
                f"granted ({len(self.granted)})"
            )

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.granted)

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(g.fingerprint for g in self.granted)


class _LeaseStore(TypingProtocol):
    """
    The subset of `StorePort` the hand-out needs. Structural, so an in-memory
    fake fits without inheritance -- the same choice `_StoreLike` makes in
    cycle.py, and the reason every branch below is testable without SQLite.
    """

    def lease(self, *, count: int, min_grade: Grade, lease_ms: int,
              now: datetime) -> tuple[Proxy, ...]: ...

    def release(self, fingerprint: str, *, now: datetime) -> None: ...

    def expire_leases(self, *, now: datetime) -> int: ...


class HandoutService:
    """
    Lease -> validate target -> rank -> grant, over PORTS only.

    Holds no state between calls: the pool is the store's, the time is the
    clock's. A service that cached "the best proxies" would be serving a stale
    ranking, which is B-16 rebuilt one layer up.
    """

    def __init__(
        self,
        *,
        store: _LeaseStore,
        clock: ClockPort,
        target_policy: TargetPolicy,
        policy: HandoutPolicy | None = None,
        scoring: ScoringPolicy | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._target_policy = target_policy
        self._policy = policy or HandoutPolicy()
        self._scoring = scoring or ScoringPolicy()

    # ── the hand-out ──────────────────────────────────────────────────────────
    def handout(
        self,
        *,
        target: Target | None,
        count: int = 1,
        min_grade: Grade = Grade.USABLE,
        lease_ms: int | None = None,
    ) -> HandoutResult:
        """
        Grant up to `count` proxies for `target`, or refuse with a named reason.

        `target` is REQUIRED and has no default -- passing None is a refusal
        (NO_TARGET), not an invitation to substitute one (ADR-007). The legacy
        code's default target is the single most expensive defect in this
        project's history, so the parameter is positional-by-keyword and
        un-defaulted at every layer.

        ORDER OF OPERATIONS IS PART OF THE CONTRACT. The target is validated
        BEFORE anything is leased. Leasing first would take rows out of the pool
        on behalf of a request that was never allowed to have them -- a denied
        caller could degrade service for permitted ones.
        """
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        if count > self._policy.max_count:
            raise ValueError(
                f"count {count} exceeds max_count {self._policy.max_count}"
            )

        effective_lease_ms = (
            self._policy.default_lease_ms if lease_ms is None else lease_ms
        )
        if effective_lease_ms < 1:
            raise ValueError(f"lease_ms must be >= 1, got {effective_lease_ms}")
        if effective_lease_ms > self._policy.max_lease_ms:
            raise ValueError(
                f"lease_ms {effective_lease_ms} exceeds max_lease_ms "
                f"{self._policy.max_lease_ms}"
            )

        # ── ADR-029, on the serving path at last ─────────────────────────────
        refusal = check_target(target, self._target_policy)
        if refusal is not None:
            return HandoutResult(
                refusal=HandoutRefusal.TARGET_REFUSED,
                target_refusal=str(refusal),
                lease_ms=effective_lease_ms,
            )

        now = self._clock.now()

        # Reclaim first: a consumer that was SIGKILLed while holding a lease has
        # removed those proxies from the pool until their deadline. Sweeping here
        # means a thin pool recovers on demand rather than only on the scheduler's
        # 10 s tick -- see the config-tension note in the module docstring.
        reclaimed = 0
        if self._policy.reclaim_expired_first:
            reclaimed = self._store.expire_leases(now=now)

        want = min(
            count * self._policy.overselect,
            self._policy.max_overselect_rows,
        )
        leased = self._store.lease(
            count=want, min_grade=min_grade,
            lease_ms=effective_lease_ms, now=now,
        )

        if not leased:
            return HandoutResult(
                refusal=HandoutRefusal.POOL_EMPTY,
                reclaimed_expired=reclaimed,
                lease_ms=effective_lease_ms,
            )

        granted: list[Granted] = []
        surplus: list[Proxy] = []
        unusable: list[Proxy] = []
        # Which rows have been GRANTED, tracked separately from the two release
        # buckets. The first version of this method released `surplus + unusable`
        # in the `finally`, which leaked every leased row whenever `rank` raised:
        # the exception happened BEFORE those lists were populated, so the
        # `finally` dutifully released nothing. The invariant that actually holds
        # is "release everything not granted", so that is what is now computed --
        # from `leased`, which is known before the risky work begins.
        # `test_surplus_is_released_even_if_ranking_raises` failed against the
        # earlier version and is what found this.
        granted_fps: set[str] = set()
        try:
            # Full four-term P07 ranking over the over-selected pool. This is the
            # step that makes the score decide WHO gets served (ADR-033), rather
            # than merely ordering rows the store already chose by p95.
            ranked = rank(
                leased, self._scoring, now=now,
                min_grade=min_grade, include_stale=False,
            )
            keep = ranked[:count]
            kept = {p.fingerprint for p, _ in keep}

            for p, score in keep:
                age_s = self._age_s(p, now)
                stale_for_target = (
                    age_s is None or age_s > self._policy.target_ttl_s
                )
                granted.append(Granted(
                    proxy=p, score=score,
                    revalidation_required=stale_for_target,
                    age_s=age_s,
                ))
                granted_fps.add(p.fingerprint)

            ranked_fps = {p.fingerprint for p, _ in ranked}
            for p in leased:
                if p.fingerprint in kept:
                    continue
                # `rank` drops stale and below-grade rows, so a leased row absent
                # from `ranked` was refused on quality, while one present but
                # beyond `count` merely lost the comparison. Different facts,
                # counted separately -- collapsing them would hide a pool that is
                # large but entirely stale behind "we had plenty".
                (surplus if p.fingerprint in ranked_fps else unusable).append(p)
        finally:
            # Return everything NOT GRANTED, computed from `leased` rather than
            # from the two buckets: if the ranking above raised, those buckets
            # are still empty while the rows are very much leased. See the
            # `finally` note in the module docstring.
            for p in leased:
                if p.fingerprint not in granted_fps:
                    self._store.release(p.fingerprint, now=now)

        if not granted:
            # Rows existed but none survived ranking: report ALL_STALE rather
            # than POOL_EMPTY. "The pool is empty" and "the pool is full of
            # expired evidence" demand opposite responses -- add sources, or run
            # discovery.
            return HandoutResult(
                refusal=HandoutRefusal.ALL_STALE,
                leased=len(leased),
                released_surplus=len(surplus),
                released_unusable=len(unusable),
                reclaimed_expired=reclaimed,
                lease_ms=effective_lease_ms,
            )

        return HandoutResult(
            granted=tuple(granted),
            leased=len(leased),
            released_surplus=len(surplus),
            released_unusable=len(unusable),
            reclaimed_expired=reclaimed,
            revalidation_required=sum(
                1 for g in granted if g.revalidation_required
            ),
            lease_ms=effective_lease_ms,
        )

    # ── returning a proxy ─────────────────────────────────────────────────────
    def report_failure(self, fingerprint: str) -> None:
        """
        A granted proxy failed in use: return it to the pool now.

        Separate from `release_all` because the CALLER's intent differs -- this
        one says "this proxy misbehaved", and the P09 scheduler will want that
        signal to drive `consecutive_failures`. It is deliberately NOT recording
        the failure count here: `record_failure` is a Proxy transition that needs
        an upsert, and quietly performing one inside a release would make a
        return-to-pool call mutate quality history as a side effect.
        """
        self._store.release(fingerprint, now=self._clock.now())

    def release_all(self, result: HandoutResult) -> int:
        """
        Return every proxy from a hand-out. Returns how many were released.

        Takes the RESULT rather than a list of strings so a caller cannot
        accidentally release a set it never held.
        """
        now = self._clock.now()
        for fp in result.fingerprints:
            self._store.release(fp, now=now)
        return len(result.granted)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _age_s(proxy: Proxy, now: datetime) -> float | None:
        """
        Seconds since the proxy was last checked, or None if it never was.

        None is NOT 0.0. An unverified proxy is the least fresh state there is,
        and reporting 0 would make it look just-checked -- the exact inversion
        proxy.txt got wrong to the tune of 97% dead entries.
        """
        if proxy.last_checked is None:
            return None
        return (now - proxy.last_checked).total_seconds()


def require_handout(result: HandoutResult) -> tuple[Granted, ...]:
    """
    Return the granted proxies, or raise naming the refusal.

    Mirrors `require_target`: `handout()` returns a value that is easy to drop on
    the floor, and this codebase already has one instance of a computed fact
    nobody read (ADR-019).
    """
    if result.refusal is not None:
        if result.target_refusal is not None:
            raise TargetNotAllowed(result.target_refusal)
        raise HandoutUnavailable(result.refusal)
    if not result.granted:
        raise HandoutUnavailable(HandoutRefusal.POOL_EMPTY)
    return result.granted


class HandoutUnavailable(RuntimeError):
    """Raised by `require_handout` when no proxy could be granted."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"no proxy handed out: {reason}")


__all__ = [
    "HandoutPolicy", "HandoutRefusal", "HandoutResult", "HandoutService",
    "HandoutUnavailable", "Granted", "require_handout",
]
