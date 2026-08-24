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


@dataclass(frozen=True, slots=True)
class ProbePlan:
    """How many samples and how patient to be. All tunables come from config.yaml."""
    samples: int = 5                 # k, ADR-003
    per_sample_timeout_ms: int = 8000
    tcp_timeout_ms: int = 3000
    stop_after_consecutive_failures: int = 2   # don't pay 5x for a dead endpoint


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
