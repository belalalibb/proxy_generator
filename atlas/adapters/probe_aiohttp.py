"""
AiohttpProbe — the staged probe pipeline (§7), the real ProbePort implementation.

WHAT THE LEGACY SYSTEM DID, AND WHY THIS IS SHAPED DIFFERENTLY

  * It went straight to a full HTTPS GET of instagram.com with 100-150 threads.
    No triage. Every dead endpoint cost a full TLS handshake and an 8-15s timeout,
    which is most of where the 1418.98s runtime went (BASELINE.json).

  * It took ONE sample and called the result a latency. One sample cannot
    distinguish a 200ms proxy from a 200ms-then-9000ms proxy, so `TOO_JITTERY`
    and `UNRELIABLE` were not merely unenforced -- they were unmeasurable
    (ADR-003).

  * It set verify=False in 9 places (B-09), which makes a MITM proxy
    indistinguishable from an honest one. So "working" included "silently
    rewriting your traffic".

  * It never checked whether the proxy forwarded the client IP, so a transparent
    proxy that leaks the caller's address counted as a success. For most stated
    uses of a proxy that is the one failure that matters.

Staging is an economic decision, not a stylistic one. k=5 sampling (ADR-003) costs
~5x the legacy single sample, so the cheap stages must run first and the expensive
one is only paid by candidates that have already proved they exist:

  S1 SYNTAX    pure, free     -- in core/policy/normalize.py, not here
  S2 TCP       one handshake  -- ~3ms to reject, vs ~8000ms for a GET timeout
  S3 PROTOCOL  discovery      -- ADR-005: the source's label is a HINT
  S4 LATENCY   k samples      -- the gate the legacy system never had
  S5 INTEGRITY body + IP      -- interception and transparent-leak detection

TLS VERIFICATION IS ON. If a proxy cannot carry a verified TLS session it fails;
that is a real property of the proxy, not an inconvenience to be switched off.
"""
from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, replace

import aiohttp

from atlas.core.domain.proxy import Anonymity, Protocol, Proxy
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import ReasonCode
from atlas.core.ports.probe import PROTOCOL_LADDER, ProbePlan, ProbeResult

# Ordered cheapest-first. http is tried before the CONNECT tunnel because a plain
# forward proxy answers it in one round trip.
#
# IMPORTED, NOT DECLARED (ADR-039). `claim_bound()` prices this ladder to size a
# PROBING claim; if the adapter kept its own copy, adding a rung here would leave
# the bound quietly short by one target timeout per probe -- and a short claim is
# the double-probe defect P10 closed, returning through the timeout. The alias
# keeps this module's existing references readable.
_PROTOCOL_LADDER: tuple[Protocol, ...] = PROTOCOL_LADDER

_AIOHTTP_SCHEME = {
    Protocol.HTTP: "http",
    Protocol.HTTPS: "http",      # CONNECT tunnel is still dialled over http://
    Protocol.SOCKS4: "socks4",
    Protocol.SOCKS5: "socks5",
}


def _classify_exception(exc: BaseException) -> tuple[ReasonCode, str]:
    """
    Map a transport exception onto a ReasonCode.

    Every branch names a cause. The legacy code had 23 silent handlers and 9 bare
    `except:` clauses (B-02), so a failure's reason was routinely destroyed at the
    point it was discovered -- which is why its logs could say only "not working".
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ReasonCode.TCP_TIMEOUT, "timeout"
    if isinstance(exc, socket.gaierror):
        return ReasonCode.DNS_FAILED, f"dns: {exc}"
    if isinstance(exc, aiohttp.ClientProxyConnectionError):
        return ReasonCode.TCP_REFUSED, f"proxy connect: {exc}"
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return ReasonCode.TLS_FAILED, f"tls cert: {exc}"
    if isinstance(exc, aiohttp.ClientConnectorSSLError):
        return ReasonCode.TLS_FAILED, f"tls: {exc}"
    if isinstance(exc, aiohttp.ClientSSLError):
        return ReasonCode.TLS_FAILED, f"tls: {exc}"
    if isinstance(exc, aiohttp.ClientHttpProxyError):
        # 407 => the proxy exists and speaks HTTP, but demands credentials.
        status = getattr(exc, "status", None)
        if status == 407:
            return ReasonCode.PROXY_AUTH_REQUIRED, "407 from proxy"
        return ReasonCode.BAD_STATUS, f"proxy http error: {status}"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return ReasonCode.TCP_REFUSED, f"connect: {exc}"
    if isinstance(exc, (aiohttp.ServerDisconnectedError,
                        aiohttp.ClientPayloadError)):
        return ReasonCode.TCP_REFUSED, f"disconnected: {exc}"
    if isinstance(exc, UnicodeDecodeError):
        # A SOCKS proxy handed binary to an HTTP parser: real, and diagnostic.
        return ReasonCode.PROTO_MISMATCH, "binary where http expected"
    if isinstance(exc, aiohttp.ClientError):
        return ReasonCode.TCP_REFUSED, f"client error: {exc}"
    return ReasonCode.TCP_REFUSED, f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True, slots=True)
class IntegrityBaseline:
    """
    What an HONEST answer looks like, measured WITHOUT a proxy first.

    Integrity cannot be judged in the abstract: to say "this body was rewritten"
    or "this proxy leaked my IP" you must first know the true body and the true
    egress IP. Establishing the baseline directly is what makes CONTENT_MISMATCH
    and TRANSPARENT_LEAK decidable rather than guessed.
    """
    direct_ip: str
    body_marker: str
    status: int


class AiohttpProbe:
    """
    ProbePort over aiohttp. All network I/O for probing lives here, so core/ stays
    pure and the architecture fitness test (P01.T2) keeps passing.
    """

    def __init__(self, *, verify_tls: bool = True,
                 user_agent: str = "atlas-probe/4.0") -> None:
        # No verify=False switch is exposed as a convenience. B-09 was 9 sites
        # disabling verification; making that easy is how it happened.
        self._verify_tls = verify_tls
        self._ua = user_agent

    # ── S2: TCP ───────────────────────────────────────────────────────────────
    async def tcp_handshake(self, proxy: Proxy, timeout_ms: int) -> ProbeResult:
        """
        One TCP connect. Rejects the majority of candidates for ~the cost of a
        round trip, before any TLS or HTTP work is paid for.
        """
        started = time.perf_counter()
        try:
            fut = asyncio.open_connection(proxy.endpoint.host, proxy.endpoint.port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
        except Exception as exc:                       # noqa: BLE001 - classified
            reason, detail = _classify_exception(exc)
            return ProbeResult(ok=False, reason=reason, detail=detail,
                               elapsed_ms=(time.perf_counter() - started) * 1000)
        writer.close()
        teardown: str | None = None
        try:
            await writer.wait_closed()
        except Exception as exc:                       # noqa: BLE001 - reported
            # The connection SUCCEEDED; a noisy teardown does not undo that. But
            # it is not discarded either: B-02 was 23 handlers that swallowed the
            # cause, so the detail rides along on the successful result.
            teardown = f"teardown: {type(exc).__name__}: {exc}"
        return ProbeResult(ok=True, reason=ReasonCode.OK,
                           elapsed_ms=(time.perf_counter() - started) * 1000,
                           detail=teardown)

    # ── one request through the proxy ──────────────────────────────────────────
    async def _request(self, proxy: Proxy, target: Target, protocol: Protocol,
                       timeout_ms: int) -> tuple[ProbeResult, str | None]:
        scheme = _AIOHTTP_SCHEME[protocol]
        proxy_url = f"{scheme}://{proxy.endpoint.host}:{proxy.endpoint.port}"
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        started = time.perf_counter()
        try:
            connector = aiohttp.TCPConnector(ssl=self._verify_tls, limit=0)
            async with aiohttp.ClientSession(connector=connector,
                                             timeout=timeout) as session:
                async with session.get(target.url, proxy=proxy_url,
                                       headers={"User-Agent": self._ua},
                                       allow_redirects=False) as resp:
                    body = await resp.read()
                    elapsed = (time.perf_counter() - started) * 1000
                    if resp.status == 407:
                        # A 407 must be named BEFORE the generic status branch.
                        # aiohttp only RAISES ClientHttpProxyError for the CONNECT
                        # tunnel; a plain forward proxy returns 407 as an ordinary
                        # response, so without this the most common case collapsed
                        # into BAD_STATUS and the cause was destroyed (B-02).
                        # "exists, speaks HTTP, wants credentials" is a different
                        # and actionable finding from "returned the wrong page".
                        return ProbeResult(
                            ok=False, reason=ReasonCode.PROXY_AUTH_REQUIRED,
                            status_code=407, elapsed_ms=elapsed,
                            body_bytes=len(body),
                            detail="407 from proxy (credentials required)",
                        ), None
                    if resp.status != target.expect_status:
                        return ProbeResult(
                            ok=False, reason=ReasonCode.BAD_STATUS,
                            status_code=resp.status, elapsed_ms=elapsed,
                            body_bytes=len(body),
                            detail=f"expected {target.expect_status}",
                        ), None
                    if len(body) < target.min_bytes:
                        # ADR-013/ADR-015: a short body is its own fault, in OCTETS.
                        return ProbeResult(
                            ok=False, reason=ReasonCode.BODY_TOO_SMALL,
                            status_code=resp.status, elapsed_ms=elapsed,
                            body_bytes=len(body),
                            detail=f"{len(body)} < min {target.min_bytes}",
                        ), None
                    return ProbeResult(
                        ok=True, reason=ReasonCode.OK, status_code=resp.status,
                        elapsed_ms=elapsed, body_bytes=len(body),
                        discovered_protocol=protocol,
                    ), body.decode("utf-8", errors="replace")
        except Exception as exc:                       # noqa: BLE001 - classified
            reason, detail = _classify_exception(exc)
            return ProbeResult(ok=False, reason=reason, detail=detail,
                               elapsed_ms=(time.perf_counter() - started) * 1000), None

    # ── S3: PROTOCOL DISCOVERY ────────────────────────────────────────────────
    async def discover_protocol(self, proxy: Proxy, target: Target) -> ProbeResult:
        """
        ADR-005: the source's protocol label is a HINT, so it is TESTED.

        TheSpeedX/SOCKS-List/master/http.txt is a SOCKS list named http.txt. It
        measured ALIVE with 2 853 unique candidates, every one of which the legacy
        code tested as HTTP and therefore threw away (B-12). Trusting the filename
        cost the entire source.

        The declared hint is tried FIRST (it is usually right, and being right
        cheaply matters at scale), then the remaining protocols in cost order.
        """
        ladder = list(_PROTOCOL_LADDER)
        if proxy.protocol in ladder and proxy.protocol is not Protocol.UNKNOWN:
            ladder.remove(proxy.protocol)
            ladder.insert(0, proxy.protocol)

        # Two separate records, because they are two different kinds of fact:
        #   last_tested   -- something we actually MEASURED failing
        #   untested      -- a rung we could not try at all (no SOCKS transport)
        #
        # Keeping one `last` variable let the untestable placeholder overwrite a
        # real measurement, since SOCKS sits at the END of the ladder. A refused
        # connection was then reported as "socks4 not testable" -- the true,
        # measured cause destroyed by a note about something never attempted.
        # That is BUG_LEDGER B-02 (losing the cause at the point of discovery)
        # reappearing inside the code written to avoid it.
        last_tested: ProbeResult | None = None
        untested: ProbeResult | None = None
        for protocol in ladder:
            if _AIOHTTP_SCHEME[protocol] in ("socks4", "socks5"):
                # aiohttp has no native SOCKS support; claiming to have tested it
                # would be a fabricated negative (H2). Report honestly instead.
                if untested is None:
                    untested = ProbeResult(
                        ok=False, reason=ReasonCode.PROTO_MISMATCH,
                        detail=f"{protocol.value} not testable: aiohttp lacks "
                               "SOCKS (install aiohttp-socks to enable)",
                    )
                continue
            result, _ = await self._request(proxy, target, protocol,
                                            target.timeout_ms)
            if result.ok:
                return result
            last_tested = result
        # A measured failure always outranks an untested rung.
        return last_tested or untested or ProbeResult(
            ok=False, reason=ReasonCode.PROTO_MISMATCH,
            detail="no protocol succeeded")

    # ── S4: LATENCY, k SAMPLES ────────────────────────────────────────────────
    async def sample_latency(self, proxy: Proxy, target: Target,
                             plan: ProbePlan) -> list[ProbeResult]:
        """
        k INDEPENDENT samples (ADR-003), sequential and separately connected.

        Sequential on purpose: firing k concurrent requests at one proxy measures
        how it behaves under self-inflicted load, not its latency, and would bias
        p95 upward for exactly the good proxies worth keeping.

        Every attempt is returned, failures included. The caller needs the
        DENOMINATOR -- build_profile() derives success_ratio from attempted, and
        without the failures success_ratio would always be 1.0 and UNRELIABLE
        would be undetectable.

        Early stop after `stop_after_consecutive_failures` avoids paying 5x for a
        corpse; the samples already taken are still returned.
        """
        protocol = (proxy.protocol if proxy.protocol is not Protocol.UNKNOWN
                    else Protocol.HTTP)
        out: list[ProbeResult] = []
        consecutive = 0
        for _ in range(plan.samples):
            result, _ = await self._request(proxy, target, protocol,
                                            plan.per_sample_timeout_ms)
            out.append(result)
            consecutive = 0 if result.ok else consecutive + 1
            if consecutive >= plan.stop_after_consecutive_failures:
                break
        return out

    # ── S5: INTEGRITY ─────────────────────────────────────────────────────────
    async def establish_baseline(self, target: Target,
                                 ip_echo_url: str) -> IntegrityBaseline:
        """
        Fetch target and egress IP WITHOUT a proxy.

        Integrity is comparative. Without knowing the true body and the true
        egress IP, "the body was rewritten" and "the proxy leaked my IP" are not
        decidable claims -- and the legacy system, which checked neither, could
        not tell an honest proxy from a MITM.
        """
        timeout = aiohttp.ClientTimeout(total=target.timeout_ms / 1000)
        connector = aiohttp.TCPConnector(ssl=self._verify_tls)
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=timeout) as session:
            async with session.get(ip_echo_url,
                                   headers={"User-Agent": self._ua}) as r:
                direct_ip = (await r.text()).strip()
            async with session.get(target.url,
                                   headers={"User-Agent": self._ua}) as r:
                body = await r.text()
                status = r.status
        return IntegrityBaseline(direct_ip=direct_ip,
                                 body_marker=_marker(body),
                                 status=status)

    async def check_integrity(self, proxy: Proxy, target: Target,
                              baseline: IntegrityBaseline | None = None,
                              ip_echo_url: str | None = None) -> ProbeResult:
        """
        Two questions the legacy system never asked:

          1. Did the body arrive INTACT?  Different marker => CONTENT_MISMATCH.
          2. Does the proxy forward MY IP? Our IP visible => TRANSPARENT_LEAK.

        A proxy that leaks the client IP has failed at the only job that was
        asked of it, no matter how fast it is -- which is why admission.decide()
        ranks integrity ABOVE latency.
        """
        protocol = (proxy.protocol if proxy.protocol is not Protocol.UNKNOWN
                    else Protocol.HTTP)
        result, body = await self._request(proxy, target, protocol,
                                           target.timeout_ms)
        if not result.ok:
            return result

        if baseline is not None and body is not None:
            if _marker(body) != baseline.body_marker:
                return replace(result, ok=False,
                               reason=ReasonCode.CONTENT_MISMATCH,
                               detail="body marker differs from direct fetch")

        if ip_echo_url and baseline is not None:
            echo = Target(url=ip_echo_url, expect_status=200,
                          timeout_ms=target.timeout_ms)
            echo_result, echo_body = await self._request(
                proxy, echo, protocol, target.timeout_ms)
            if echo_result.ok and echo_body is not None:
                seen = echo_body.strip()
                if baseline.direct_ip and baseline.direct_ip in seen:
                    return replace(
                        result, ok=False, reason=ReasonCode.TRANSPARENT_LEAK,
                        observed_anonymity=Anonymity.TRANSPARENT,
                        observed_client_ip=baseline.direct_ip,
                        detail="our own egress IP was visible through the proxy",
                    )
                return replace(result, observed_anonymity=Anonymity.ANONYMOUS,
                               observed_client_ip=seen[:64])
        return result


def _marker(body: str) -> str:
    """
    A stable fingerprint of a body, tolerant of benign variation.

    An echo endpoint legitimately differs between two fetches (it reports the
    caller's IP), so comparing whole bodies would flag every honest proxy as
    CONTENT_MISMATCH. Length-bucket + structural shape changes when content is
    REWRITTEN (an injected ad, a captive-portal login page) but not when a field
    inside it varies.
    """
    stripped = body.strip()
    shape = "json" if stripped[:1] in "{[" else (
        "html" if stripped[:1] == "<" else "text")
    return f"{shape}:{len(stripped) // 64}"
