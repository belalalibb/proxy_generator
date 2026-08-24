"""
ProbePort — the staged probe pipeline (§7).

Staging exists because k=5 sampling (ADR-003) is ~5x the cost of the legacy single
sample. The cheap stages run first so the expensive one is only paid by candidates
that already proved they exist:

  S1 SYNTAX    pure, free      -- reject malformed/private endpoints
  S2 TCP       one handshake   -- v3.py:393 had this idea and it was worth keeping
  S3 PROTOCOL  discovery       -- ADR-005; the source's label is only a hint
  S4 LATENCY   k samples       -- the gate the legacy system never had
  S5 INTEGRITY body/IP checks  -- detect interception and transparent leaks

The legacy code went straight to a full HTTPS GET with 100-150 threads, which is
why it produced 2 x HTTP 429 and 3 x 403 against its own sources in a single sweep.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from atlas.core.domain.proxy import Anonymity, Protocol as ProxyProtocol, Proxy
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import ReasonCode


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """
    One probe outcome. `reason` is mandatory on failure -- there is no code path
    that loses the cause of an error (BUG_LEDGER B-02, 23 silent handlers).
    """
    ok: bool
    reason: ReasonCode
    elapsed_ms: float | None = None
    status_code: int | None = None
    body_bytes: int | None = None
    discovered_protocol: ProxyProtocol | None = None
    observed_anonymity: Anonymity | None = None
    observed_client_ip: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.ok and self.reason is not ReasonCode.OK:
            raise ValueError("a successful probe cannot carry a failure reason")
        if not self.ok and self.reason is ReasonCode.OK:
            raise ValueError("a failed probe must name its reason")


# The protocol rungs S3 will try, cheapest-first: a plain forward proxy answers
# http in one round trip, so it precedes the CONNECT tunnel.
#
# THIS LIVES IN CORE BECAUSE TWO THINGS NEED IT AND THEY MUST NOT DISAGREE.
# `AiohttpProbe` walks this ladder; `claim_bound()` prices it. Defined in the
# adapter and re-stated in the bound, the two would be a duplicated magic number
# of exactly the kind ADR-039 exists to forbid -- and the failure would be
# silent, because a bound computed from a stale rung count still returns a
# plausible-looking number.
PROTOCOL_LADDER: tuple[ProxyProtocol, ...] = (
    ProxyProtocol.HTTP,
    ProxyProtocol.HTTPS,
    ProxyProtocol.SOCKS5,
    ProxyProtocol.SOCKS4,
)


@dataclass(frozen=True, slots=True)
class ProbePlan:
    """How many samples and how patient to be. All tunables come from config.yaml."""
    samples: int = 5                 # k, ADR-003
    per_sample_timeout_ms: int = 8000
    tcp_timeout_ms: int = 3000
    stop_after_consecutive_failures: int = 2   # don't pay 5x for a dead endpoint

    def worst_case_ms(self, *, target_timeout_ms: int,
                      protocol_rungs: int = len(PROTOCOL_LADDER)) -> int:
        """
        The longest ONE `DiscoveryEngine.evaluate()` can legitimately take.

        DERIVED FROM THE STAGES THAT ACTUALLY RUN, NOT FROM THE DOCUMENTED
        PIPELINE. `evaluate()` (cycle.py) calls `tcp_handshake` ->
        `discover_protocol` -> `sample_latency`. It does NOT call
        `check_integrity`: measured in `engineering/raw/recheck_bounds.json`
        (`check_integrity_called_by_evaluate: false`), and confirmed by AST --
        S5 has no production caller at all. Pricing S5 here would inflate the
        bound by a stage no probe pays for, which is the mirror image of the
        defect this method exists to prevent: a bound that describes the design
        instead of the code is not a bound.

        `samples * per_sample_timeout_ms` is NOT reduced by
        `stop_after_consecutive_failures`. Early stop needs `n` CONSECUTIVE
        failures, and an alternating ok/fail proxy never produces them, so the
        worst case really is all k samples run serially -- and `sample_latency`
        is sequential on purpose (firing k at once measures self-inflicted load,
        not latency).

        Every rung of the ladder is priced, including the SOCKS rungs the aiohttp
        adapter currently SKIPS for ~0 cost. Deliberately conservative: the
        adapter's own failure message invites `aiohttp-socks` to be installed,
        and on that day two skipped rungs silently become two real requests. A
        bound that has to be revised by hand when a documented switch is flipped
        is a bound that goes stale without anyone noticing. Overstating a claim's
        lifetime is safe -- the claim merely outlives the work; understating it
        is the defect (a live probe's claim expires and a second worker starts
        the same k=5).
        """
        if target_timeout_ms < 1:
            raise ValueError(
                f"target_timeout_ms must be >= 1, got {target_timeout_ms}")
        if protocol_rungs < 1:
            raise ValueError(
                f"protocol_rungs must be >= 1, got {protocol_rungs}")
        return (self.tcp_timeout_ms                                  # S2
                + protocol_rungs * target_timeout_ms                 # S3
                + self.samples * self.per_sample_timeout_ms)         # S4


@dataclass(frozen=True, slots=True)
class ProbeBound:
    """
    How long a worker can legitimately stay inside one PROBING claim.

    Reported as its parts, not just a total, because a claim that turns out to be
    too short must be diagnosable: `waves` says the cause was queueing behind a
    semaphore, `per_probe_ms` says it was the probe itself. A bare number would
    force the next person to re-derive it -- which is how the 120 000 ms default
    came to be wrong in the first place.
    """
    per_probe_ms: int
    waves: int
    required_ms: int

    def __post_init__(self) -> None:
        if self.required_ms != self.per_probe_ms * self.waves:
            raise ValueError(
                f"inconsistent bound: {self.per_probe_ms} * {self.waves} != "
                f"{self.required_ms}"
            )


def claim_bound(plan: ProbePlan, *, target_timeout_ms: int,
                batch: int, concurrency: int,
                protocol_rungs: int = len(PROTOCOL_LADDER)) -> ProbeBound:
    """
    The authoritative minimum lifetime of a PROBING claim (ADR-039).

    ONE claim statement covers the whole batch, but a semaphore admits only
    `concurrency` probes at a time. So a row in the LAST wave sits inside a claim
    that was taken at T while it waits for every wave ahead of it:

        required = worst_case_per_probe * ceil(batch / concurrency)

    Missing the wave factor is what made the old default wrong by ~5x rather than
    by a little: measured 59 000 ms per probe, but 590 000 ms required at the
    default batch of 100 and concurrency of 10 (`recheck_bounds.json`).

    This is the single definition. `RecheckBudget` validates against it and
    derives its default from it, so there is no second number to drift.
    """
    if batch < 1:
        raise ValueError(f"batch must be >= 1, got {batch}")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    per_probe = plan.worst_case_ms(target_timeout_ms=target_timeout_ms,
                                   protocol_rungs=protocol_rungs)
    waves = -(-batch // concurrency)          # ceil without importing math
    return ProbeBound(per_probe_ms=per_probe, waves=waves,
                      required_ms=per_probe * waves)


def default_target_timeout_ms() -> int:
    """
    `Target.timeout_ms`'s default, READ from the dataclass rather than restated.

    Restating `8000` here would be the duplicated magic number ADR-039 forbids,
    and it would break quietly: the bound would keep computing a number that no
    longer matched the target the probe actually uses.
    """
    return int(Target.__dataclass_fields__["timeout_ms"].default)


@runtime_checkable
class ProbePort(Protocol):
    """Implemented in adapters/ with aiohttp; core/ only knows this shape."""

    async def tcp_handshake(self, proxy: Proxy, timeout_ms: int) -> ProbeResult:
        """S2 -- cheap triage before any protocol work."""
        ...

    async def discover_protocol(self, proxy: Proxy, target: Target) -> ProbeResult:
        """
        S3 -- try http / https-CONNECT / socks4 / socks5 and report what worked.

        Justified by evidence: TheSpeedX/SOCKS-List/master/http.txt is a SOCKS list
        named http.txt. It measured ALIVE with 2 853 unique candidates, every one of
        which the legacy code tested as HTTP and therefore discarded (B-12).
        """
        ...

    async def sample_latency(self, proxy: Proxy, target: Target,
                             plan: ProbePlan) -> list[ProbeResult]:
        """S4 -- k independent samples; the caller builds the LatencyProfile."""
        ...

    async def check_integrity(self, proxy: Proxy, target: Target) -> ProbeResult:
        """
        S5 -- did the body arrive intact, and does the proxy leak the client IP?

        The legacy code disabled TLS verification in 9 places, which makes a MITM
        proxy indistinguishable from an honest one (BUG_LEDGER B-09).
        """
        ...
