"""
POOL LIFECYCLE — retirement and recovery, and the end of the absorbing `COOLING`
state (ADR-036).

WHAT WAS WRONG

`proxy.py` documents its own state machine:

    COOLING    -> failed recently; eligible again after a cooldown (ADR-006).
    RETIRED    -> repeatedly failed; kept as a record, never leased.

Neither sentence was true. Measured against the real store
(`engineering/raw/pool_lifecycle.json`, tool `measure_pool_lifecycle.py`):

  * a proxy with **40** consecutive failures is still `COOLING`;
  * `ProxyState.RETIRED` is assigned **nowhere** in production code — 0 AST
    hits, the only mentions being a comment in `scoring.py` and the docstring
    above;
  * nothing transitions **out of** `COOLING`;
  * `lease()` filters on `state='READY'`, so a `COOLING` row is unreachable;
  * `DiscoveryEngine.process_source` skips any fingerprint the store already
    holds, so a failed proxy is never re-probed either.

Together: **one transient failure removed a proxy from the pool forever.** Not
ranked last — gone. `COOLING` was an absorbing state wearing the name of a
temporary one, which is worse than an honestly-named terminal state because
every reader of `proxy.py` was told recovery existed.

That is ADR-006's own lesson, one level down. ADR-006 exists because a single
throttled read filed GeoNode (230 019 bytes, 500 proxies) as permanently dead,
and its rule was: *a single failure must NEVER disable a source.* `Source` got
`cooldown_until` and `reactivated()`. `Proxy` got neither, and the pool is where
the value actually lives.

WHY ELIGIBILITY IS DERIVED AND NOT STORED

No `cooldown_until` column is added. A proxy is eligible for re-probe when

    last_checked + cooldown_delay(consecutive_failures) <= now

and the schema already holds both inputs. Storing a third column would create a
second source of truth for a fact the existing two fully determine, and it could
disagree with them — a `cooldown_until` written from a stale
`consecutive_failures`, or the reverse, and nothing to say which was right. This
project has already paid for exactly that: ADR-020's cross-stream splice was two
recorded numbers that could not both be true, and it took a dedicated tool to
untangle. Derived state cannot drift from itself.

The cost is that eligibility is not directly indexable, which is why the store
selects *candidates* by `(state, last_checked)` — a predicate an index CAN
serve — and this module makes the actual decision on each candidate. The rule
therefore exists in exactly one place. Pushing the whole predicate into SQL would
duplicate it, and a duplicated rule is a rule that will drift (ADR-023).

FOUR OUTCOMES, NAMED

`PoolAction` is an enum, not a bool or a tri-state int, for the same reason every
other decision in this codebase is named: B-02 (23 silent handlers) is what
unnamed outcomes cost. An operator inspecting a scheduler run must be able to see
*why* a row was left alone — `KEEP_READY` and `COOLING_NOT_ELAPSED` are both
"nothing happened", and they mean completely different things.

WHY `RETIRE` IS DECIDED BEFORE `RECHECK`

A row at or past the retirement threshold whose cooldown has ALSO elapsed
satisfies both predicates. Order matters, and retirement wins: re-probing a proxy
that has already earned retirement spends k=5 samples to re-learn a fact recorded
five failures ago. The reverse order would make `retire_after_consecutive_failures`
unreachable for any row whose cooldown had elapsed — which is nearly all of them,
since cooldown elapses in minutes — reintroducing the decorative-config defect in
a subtler form. `test_retirement_is_decided_before_recheck` pins it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from atlas.core.domain.proxy import Proxy, ProxyState
from atlas.core.ports.clock import cooldown_delay


class PoolAction(str, Enum):
    """
    What the scheduler should do with one row, right now.

    A `str` subclass so it serialises into artifacts and log lines as the name
    rather than as `PoolAction.RETIRE`, matching `ReasonCode` / `RateRefusal`.
    """
    RETIRE = "RETIRE"                       # at/past the failure threshold
    RECHECK = "RECHECK"                     # cooldown elapsed; re-probe it
    COOLING_NOT_ELAPSED = "COOLING_NOT_ELAPSED"   # failed recently, still waiting
    KEEP_READY = "KEEP_READY"               # healthy and inside the recheck horizon
    RECHECK_READY = "RECHECK_READY"         # READY but past recheck_ready_after_s
    TERMINAL = "TERMINAL"                   # already RETIRED; never reconsidered
    IN_FLIGHT = "IN_FLIGHT"                 # LEASED or PROBING; not ours to touch


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    """
    The four `config.yaml scheduler.*` keys, and the first code that reads them.

    Defaults mirror `config.yaml` exactly. They are duplicated here rather than
    imported because `core/` may not read files — but `test_scheduler.py` asserts
    the defaults EQUAL the file, so the duplication cannot drift silently. That
    is the ADR-031 pattern: a mirrored default is only safe if something fails
    when it stops mirroring.
    """
    recheck_ready_after_s: float = 900.0
    discovery_interval_s: float = 600.0
    retire_after_consecutive_failures: int = 5
    max_pool_size: int = 50_000

    # ADR-039: how many ABANDONED rechecks a row may accumulate before it is
    # retired. A SEPARATE bound from `retire_after_consecutive_failures`, because
    # it counts a different event and neither can substitute for the other:
    #
    #   consecutive_failures  -- a probe RAN and returned a verdict we can name
    #   abandoned_rechecks    -- a probe was claimed and never reported at all
    #
    # Measured (`engineering/raw/recheck_bounds.json`): a crashing worker drove 12
    # claim->reclaim cycles and `consecutive_failures` never left 0, because the
    # abandon path writes no failure. So the existing retirement ladder could
    # never advance and the row cycled PROBING -> COOLING -> PROBING forever.
    #
    # 3 rather than 5: an abandoned probe costs a full claim lifetime of nothing
    # happening, so it is a more expensive signal than a fast rejection and is
    # worth acting on sooner. It is not derived from the failure threshold --
    # tying them together would mean one config change silently retuned two
    # different policies.
    retire_after_abandoned_rechecks: int = 3

    # ADR-006 backoff, passed through so the ladder is configurable in one place.
    cooldown_base_s: float = 30.0
    cooldown_cap_s: float = 3600.0

    def __post_init__(self) -> None:
        if self.recheck_ready_after_s <= 0:
            raise ValueError(
                f"recheck_ready_after_s must be > 0, got {self.recheck_ready_after_s}"
            )
        if self.discovery_interval_s <= 0:
            raise ValueError(
                f"discovery_interval_s must be > 0, got {self.discovery_interval_s}"
            )
        if self.retire_after_consecutive_failures < 1:
            # Zero would retire a proxy that has never failed -- the pool would
            # empty itself on the first scheduler pass. Refused loudly rather
            # than clamped, because a clamp would hide a config typo that
            # destroys the entire pool.
            raise ValueError(
                "retire_after_consecutive_failures must be >= 1, got "
                f"{self.retire_after_consecutive_failures}"
            )
        if self.max_pool_size < 1:
            raise ValueError(f"max_pool_size must be >= 1, got {self.max_pool_size}")
        if self.retire_after_abandoned_rechecks < 1:
            # Zero would retire a row for an abandonment that never happened --
            # the same pool-emptying typo `retire_after_consecutive_failures`
            # refuses, by the same reasoning: refused loudly, never clamped.
            raise ValueError(
                "retire_after_abandoned_rechecks must be >= 1, got "
                f"{self.retire_after_abandoned_rechecks}"
            )
        if self.cooldown_base_s <= 0:
            raise ValueError(
                f"cooldown_base_s must be > 0, got {self.cooldown_base_s}")
        if self.cooldown_cap_s < self.cooldown_base_s:
            raise ValueError(
                f"cooldown_cap_s ({self.cooldown_cap_s}) < cooldown_base_s "
                f"({self.cooldown_base_s}): the cap would shorten the first "
                "backoff instead of bounding the last"
            )

    @property
    def max_reachable_backoff_s(self) -> float:
        """
        The longest cooldown a proxy can actually wait before retiring.

        ADR-036 records that with `retire_after=5` this is 240 s, so ADR-006's
        3600 s cap never binds on the proxy path. Exposed as a property rather
        than left implicit so the fact is inspectable -- a cap that cannot bind
        is precisely the decorative parameter this project keeps finding, and
        naming it is cheaper than rediscovering it.
        """
        n = self.retire_after_consecutive_failures - 1
        if n < 1:
            return 0.0
        return cooldown_delay(n, base_s=self.cooldown_base_s,
                              cap_s=self.cooldown_cap_s).total_seconds()


def cooldown_elapsed(proxy: Proxy, policy: SchedulerPolicy, *,
                     now: datetime) -> bool:
    """
    Has this proxy's ADR-006 backoff expired?

    A proxy that has NEVER been checked (`last_checked is None`) counts as
    elapsed: it is a fresh candidate, not a recovering failure, and treating
    "unknown" as "still cooling" would strand every DISCOVERED row forever --
    the same never-checked-treated-as-fresh hole ADR-035 found on the serving
    path, inverted.
    """
    if proxy.last_checked is None:
        return True
    delay = cooldown_delay(proxy.consecutive_failures,
                           base_s=policy.cooldown_base_s,
                           cap_s=policy.cooldown_cap_s)
    return now >= proxy.last_checked + delay


def age_s(proxy: Proxy, *, now: datetime) -> float | None:
    """Seconds since last check, or None if never checked."""
    if proxy.last_checked is None:
        return None
    return (now - proxy.last_checked).total_seconds()


def decide(proxy: Proxy, policy: SchedulerPolicy, *, now: datetime) -> PoolAction:
    """
    What should happen to this row now? Pure; the scheduler applies the result.

    The branch order is load-bearing and each step is justified in the module
    docstring: terminal first (nothing may resurrect a RETIRED row), then
    in-flight (a LEASED row belongs to a consumer and to H3, not to us), then
    retirement, then recovery.
    """
    if proxy.state is ProxyState.RETIRED:
        return PoolAction.TERMINAL

    if proxy.state in (ProxyState.LEASED, ProxyState.PROBING):
        # Touching a LEASED row would put a size/lifecycle rule in charge of the
        # H3 guarantee. PROBING means a probe is in flight; re-scheduling it is
        # the duplicate-work defect PROBING exists to prevent.
        return PoolAction.IN_FLIGHT

    if proxy.consecutive_failures >= policy.retire_after_consecutive_failures:
        # Checked BEFORE cooldown: see the module docstring. Applies to READY
        # rows too -- a proxy can be READY with a long failure history if it
        # recovered, and once it crosses the threshold it retires regardless of
        # which state it happens to be sitting in.
        return PoolAction.RETIRE

    if proxy.abandoned_rechecks >= policy.retire_after_abandoned_rechecks:
        # ADR-039. Alongside the failure threshold and for the same reason:
        # re-probing a row that has already earned retirement spends a full claim
        # to re-learn a fact already recorded.
        #
        # This branch is what bounds the cycle. Measured without it
        # (`recheck_bounds.json`): 12 claim->reclaim cycles, `consecutive_failures`
        # stuck at 0, `RECHECK` returned every single time. The abandon path
        # records no failure, so NOTHING above this line can ever fire for a
        # proxy that crashes its probe instead of failing it -- which is why this
        # cannot be folded into the failure threshold.
        return PoolAction.RETIRE

    if proxy.state is ProxyState.READY:
        a = age_s(proxy, now=now)
        if a is None or a > policy.recheck_ready_after_s:
            # `a is None` -- READY but never checked -- should be impossible via
            # the engine, which only grants READY after a successful probe. It is
            # handled rather than asserted because a hand-built or migrated row
            # could reach it, and the safe reading of "no evidence" is
            # "re-verify", never "assume fresh" (H7/ADR-003).
            return PoolAction.RECHECK_READY
        return PoolAction.KEEP_READY

    # DISCOVERED or COOLING, below the retirement threshold.
    if cooldown_elapsed(proxy, policy, now=now):
        return PoolAction.RECHECK
    return PoolAction.COOLING_NOT_ELAPSED


def is_terminal(state: ProxyState) -> bool:
    """
    Only `RETIRED` is terminal.

    A named predicate rather than an inline `is ProxyState.RETIRED` so the
    absorbing-state property has one definition and can be tested directly --
    `test_only_retired_is_absorbing` asserts this returns True for RETIRED and
    **False for COOLING**, which is the negative control that would have caught
    ADR-036's defect.
    """
    return state is ProxyState.RETIRED


__all__ = [
    "PoolAction", "SchedulerPolicy", "decide", "cooldown_elapsed", "age_s",
    "is_terminal",
]
