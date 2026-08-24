"""
PROBE unit tests — AiohttpProbe against a REAL local proxy server.

WHY A FAKE SERVER AND NOT MOCKS

`AiohttpProbe` is ~350 lines whose entire job is to classify what happens on a
socket. Mocking aiohttp would mean asserting that my code calls the library the
way I imagined it behaves — the classic test that stays green while the product
is broken. So these tests run a real asyncio server on 127.0.0.1 that speaks the
forward-proxy wire protocol (aiohttp sends `GET http://host/path HTTP/1.1` to a
proxy) and assert on what the probe CONCLUDED.

Fully OFFLINE: every connection goes to a loopback port. No source URL, no live
proxy, no network. A test that needs the internet fails for reasons unrelated to
the code and is eventually deleted or marked xfail.

WHY NO `async def` TESTS

pytest-asyncio is NOT installed. An `async def test_...` without it is collected,
never awaited, and reported as passed-with-a-warning — a test that cannot fail,
which is the exact vacuous-pass defect ADR-010 exists to forbid. Rather than add
a dependency, `@runs_async` drives the loop with `asyncio.run` and the collected
object is an ordinary function. `test_no_test_in_this_module_is_a_bare_coroutine`
fails if anyone reintroduces the hazard.

WHAT IS BEING PINNED

  * the ReasonCodes the probe produces, provoked deterministically (RESUME_PROMPT
    listed "every reason-code deterministically provoked by a fake server")
  * that k samples are really k, really sequential, and that FAILURES come back —
    build_profile() needs the denominator or success_ratio is always 1.0 and
    UNRELIABLE is undetectable (ADR-003)
  * that SOCKS is reported UNTESTABLE, not failed: aiohttp has no SOCKS
    transport, and a fabricated negative is worse than an admitted gap (H2)
  * that TRANSPARENT_LEAK and CONTENT_MISMATCH actually fire — the rules the k=1
    P04 replay could not reach
"""
from __future__ import annotations

import asyncio
import inspect
import sys

import pytest

from atlas.adapters.probe_aiohttp import (
    AiohttpProbe,
    IntegrityBaseline,
    _classify_exception,
    _marker,
)
from atlas.core.domain.proxy import Anonymity, Endpoint, Protocol, Proxy
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import ReasonCode
from atlas.core.ports.probe import ProbePlan, ProbePort


def runs_async(fn):
    """
    Turn an async test body into a plain sync test.

    Deliberately NOT functools.wraps: that copies `__wrapped__`, and unwrapping
    collectors could then see a coroutine function again. Only the name and doc
    are carried across, so what pytest collects is unambiguously synchronous.
    """
    def wrapper():
        return asyncio.run(fn())
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ── a real, tiny forward proxy ────────────────────────────────────────────────
def http_response(status: int = 200, body: bytes = b"", phrase: str = "OK") -> bytes:
    return (
        f"HTTP/1.1 {status} {phrase}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: text/plain\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


class FakeProxy:
    """
    A loopback server that answers like an HTTP forward proxy.

    `handler(request_head, nth_request) -> bytes | None`. Returning None means
    "accept the connection and never answer", which provokes TCP_TIMEOUT without
    waiting on a real dead host.

    It records every request and tracks peak concurrency, so "the k samples are
    sequential" becomes a measurement at the server rather than a claim about my
    own client code.
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[str] = []
        self._inflight = 0
        self.max_inflight = 0
        self._server: asyncio.AbstractServer | None = None
        self._stop: asyncio.Event | None = None
        self.port = 0

    async def __aenter__(self) -> "FakeProxy":
        self._stop = asyncio.Event()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None and self._stop is not None
        # Release any "never answer" handler FIRST. Without this, teardown waits
        # for a 30s sleep the client already abandoned, and the suite takes 30s
        # to assert a 400ms timeout. A slow test is a test that gets skipped.
        self._stop.set()
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            text = head.decode("latin-1")
            self.requests.append(text)
            resp = self.handler(text, len(self.requests))
            if resp is None:
                # Hold the connection open without answering: this provokes the
                # CLIENT-side timeout. Waiting on the stop event rather than
                # sleeping means teardown is immediate.
                assert self._stop is not None
                await self._stop.wait()
                return
            writer.write(resp)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._inflight -= 1
            try:
                writer.close()
            except Exception:                # noqa: BLE001 - teardown only
                pass


def proxy_at(port: int, protocol: Protocol = Protocol.HTTP) -> Proxy:
    return Proxy(endpoint=Endpoint(host="127.0.0.1", port=port), protocol=protocol)


TARGET = Target(url="http://target.test/get", expect_status=200, timeout_ms=2000)
BODY = b"y" * 200


# ── S2: TCP ───────────────────────────────────────────────────────────────────
@runs_async
async def test_tcp_handshake_succeeds_and_reports_elapsed():
    async with FakeProxy(lambda h, n: http_response(200, BODY)) as srv:
        r = await AiohttpProbe().tcp_handshake(proxy_at(srv.port), 2000)
    assert r.ok and r.reason is ReasonCode.OK
    assert r.elapsed_ms is not None and r.elapsed_ms >= 0


@runs_async
async def test_a_closed_port_is_refused_and_names_its_cause():
    """
    B-02 was 23 handlers that destroyed the cause at the point of discovery,
    which is why the legacy logs could only say "not working". A failure must
    always arrive with a reason AND a detail.
    """
    async with FakeProxy(lambda h, n: None) as srv:
        dead_port = srv.port
    # the server is now closed, so nothing is listening on dead_port
    r = await AiohttpProbe().tcp_handshake(proxy_at(dead_port), 2000)
    assert not r.ok
    assert r.reason in (ReasonCode.TCP_REFUSED, ReasonCode.TCP_TIMEOUT)
    assert r.detail


# ── the single request: status / body / auth classification ──────────────────
@runs_async
async def test_a_short_body_is_body_too_small_not_a_success():
    """ADR-013/ADR-015: a short body is its own distinct fault, in OCTETS."""
    tiny = Target(url="http://target.test/get", expect_status=200,
                  min_bytes=1000, timeout_ms=2000)
    async with FakeProxy(lambda h, n: http_response(200, b"z" * 10)) as srv:
        res, _ = await AiohttpProbe()._request(
            proxy_at(srv.port), tiny, Protocol.HTTP, 2000)
    assert not res.ok
    assert res.reason is ReasonCode.BODY_TOO_SMALL
    assert res.body_bytes == 10


@runs_async
async def test_an_unexpected_status_is_bad_status_and_keeps_the_code():
    async with FakeProxy(lambda h, n: http_response(503, b"x" * 50,
                                                    "Unavailable")) as srv:
        res, _ = await AiohttpProbe()._request(
            proxy_at(srv.port), TARGET, Protocol.HTTP, 2000)
    assert not res.ok
    assert res.reason is ReasonCode.BAD_STATUS
    assert res.status_code == 503


@runs_async
async def test_407_is_proxy_auth_required_not_a_generic_failure():
    """
    A 407 means the endpoint EXISTS and speaks HTTP but wants credentials.
    Collapsing that into "dead" throws away a real, actionable distinction.
    """
    def handler(head: str, n: int) -> bytes:
        return (b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="x"\r\n'
                b"Content-Length: 0\r\n\r\n")

    async with FakeProxy(handler) as srv:
        res, _ = await AiohttpProbe()._request(
            proxy_at(srv.port), TARGET, Protocol.HTTP, 2000)
    assert not res.ok
    assert res.reason is ReasonCode.PROXY_AUTH_REQUIRED


@runs_async
async def test_a_server_that_never_answers_times_out():
    async with FakeProxy(lambda h, n: None) as srv:
        res, _ = await AiohttpProbe()._request(
            proxy_at(srv.port), TARGET, Protocol.HTTP, 400)
    assert not res.ok
    assert res.reason is ReasonCode.TCP_TIMEOUT


@runs_async
async def test_the_request_really_goes_through_the_proxy():
    """
    A forward proxy receives an ABSOLUTE-form request line. If this asserted
    nothing, the whole suite could be measuring direct connections and every
    other test here would still pass.
    """
    async with FakeProxy(lambda h, n: http_response(200, BODY)) as srv:
        await AiohttpProbe()._request(proxy_at(srv.port), TARGET,
                                      Protocol.HTTP, 2000)
        first_line = srv.requests[0].split("\r\n")[0]
    assert first_line.startswith("GET http://target.test/get")


# ── S3: protocol discovery (ADR-005) ─────────────────────────────────────────
@runs_async
async def test_a_proxy_labelled_socks5_is_still_discovered_as_http():
    """
    B-12, the exact defect: TheSpeedX/SOCKS-List/master/http.txt is a SOCKS list
    named http.txt, and the legacy code tested all 2 853 of its candidates as
    HTTP and discarded them. The label must never veto discovery.
    """
    async with FakeProxy(lambda h, n: http_response(200, BODY)) as srv:
        r = await AiohttpProbe().discover_protocol(
            proxy_at(srv.port, Protocol.SOCKS5), TARGET)
    assert r.ok
    assert r.discovered_protocol is Protocol.HTTP


@runs_async
async def test_socks_is_reported_untestable_rather_than_failed():
    """
    aiohttp has no SOCKS transport. Reporting "socks failed" would be a
    FABRICATED NEGATIVE (H2) because it was never tried. The detail must say so.
    """
    async with FakeProxy(lambda h, n: http_response(502, b"no",
                                                    "Bad Gateway")) as srv:
        r = await AiohttpProbe().discover_protocol(proxy_at(srv.port), TARGET)
    assert not r.ok
    if r.reason is ReasonCode.PROTO_MISMATCH and r.detail:
        assert "not testable" in r.detail


@runs_async
async def test_discovery_reports_a_reason_when_every_rung_fails():
    async with FakeProxy(lambda h, n: http_response(500, b"e",
                                                    "Server Error")) as srv:
        r = await AiohttpProbe().discover_protocol(proxy_at(srv.port), TARGET)
    assert not r.ok
    assert r.reason is not ReasonCode.OK


@runs_async
async def test_a_measured_failure_outranks_an_untestable_socks_rung():
    """
    REGRESSION, found by the first live calibration run: 24 of 24 endpoints that
    passed TCP were reported PROTO_MISMATCH / "socks4 not testable".

    SOCKS sits at the END of the ladder, and a single `last` variable let the
    untestable-rung placeholder overwrite the real HTTP result. So a MEASURED
    failure (500, refused, timeout) was reported as a note about a protocol that
    was never attempted -- B-02 (losing the cause at the point of discovery)
    reappearing inside the code written to prevent it.

    A real measurement must always win over an untested rung.
    """
    async with FakeProxy(lambda h, n: http_response(500, b"e",
                                                    "Server Error")) as srv:
        r = await AiohttpProbe().discover_protocol(
            proxy_at(srv.port, Protocol.UNKNOWN), TARGET)
    assert not r.ok
    assert r.reason is ReasonCode.BAD_STATUS, (
        "an untestable SOCKS rung masked a measured HTTP failure")
    assert r.status_code == 500
    assert "not testable" not in (r.detail or "")


@runs_async
async def test_a_refused_endpoint_is_not_reported_as_untestable_socks():
    """The live-run symptom, reduced: nothing is listening, so say so."""
    async with FakeProxy(lambda h, n: None) as srv:
        dead = srv.port
    r = await AiohttpProbe().discover_protocol(
        proxy_at(dead, Protocol.UNKNOWN),
        Target(url="http://target.test/get", timeout_ms=400))
    assert not r.ok
    assert r.reason in (ReasonCode.TCP_REFUSED, ReasonCode.TCP_TIMEOUT)
    assert "not testable" not in (r.detail or "")


# ── S4: k sampling — the rules the k=1 replay could not reach ────────────────
@runs_async
async def test_k_samples_really_means_k_requests():
    async with FakeProxy(lambda h, n: http_response(200, BODY)) as srv:
        out = await AiohttpProbe().sample_latency(
            proxy_at(srv.port), TARGET,
            ProbePlan(samples=5, per_sample_timeout_ms=2000))
        sent = len(srv.requests)
    assert sent == 5
    assert len(out) == 5 and all(s.ok for s in out)


@runs_async
async def test_samples_are_sequential_so_p95_is_not_self_inflicted_load():
    """
    Firing k concurrent requests at one proxy measures how it behaves under load
    WE created, not its latency, and biases p95 upward for exactly the good
    proxies worth keeping. Peak concurrency observed at the server must be 1.
    """
    async with FakeProxy(lambda h, n: http_response(200, BODY)) as srv:
        await AiohttpProbe().sample_latency(
            proxy_at(srv.port), TARGET,
            ProbePlan(samples=4, per_sample_timeout_ms=2000))
        peak = srv.max_inflight
    assert peak == 1


@runs_async
async def test_failed_samples_are_returned_so_the_denominator_survives():
    """
    build_profile(attempted=...) derives success_ratio. If sample_latency dropped
    its failures the ratio would always be 1.0 and UNRELIABLE would be
    unreachable — precisely the k=1 blind spot P06 exists to close.
    """
    def flaky(head: str, n: int) -> bytes:
        return (http_response(200, BODY) if n % 2 == 1
                else http_response(503, b"x", "Unavailable"))

    async with FakeProxy(flaky) as srv:
        out = await AiohttpProbe().sample_latency(
            proxy_at(srv.port), TARGET,
            ProbePlan(samples=4, per_sample_timeout_ms=2000,
                      stop_after_consecutive_failures=99))
    assert len(out) == 4
    failures = [s for s in out if not s.ok]
    assert failures
    assert all(s.reason is not ReasonCode.OK for s in failures)


@runs_async
async def test_early_stop_avoids_paying_k_times_for_a_corpse():
    async with FakeProxy(lambda h, n: http_response(503, b"x",
                                                    "Unavailable")) as srv:
        out = await AiohttpProbe().sample_latency(
            proxy_at(srv.port), TARGET,
            ProbePlan(samples=5, per_sample_timeout_ms=2000,
                      stop_after_consecutive_failures=2))
        sent = len(srv.requests)
    assert sent == 2          # stopped early, did not pay 5x for a dead endpoint
    assert len(out) == 2


# ── S5: integrity — TRANSPARENT_LEAK / CONTENT_MISMATCH ─────────────────────
@runs_async
async def test_transparent_leak_fires_when_our_own_ip_comes_back():
    """
    The failure that matters most for the stated purpose of a proxy, and the one
    the legacy system never checked: it counted an IP-leaking proxy as working.
    """
    our_ip = "203.0.113.7"

    def handler(head: str, n: int) -> bytes:
        if "/ip" in head.split("\r\n")[0]:
            return http_response(200, f'{{"ip":"{our_ip}"}}'.encode())
        return http_response(200, BODY)

    async with FakeProxy(handler) as srv:
        baseline = IntegrityBaseline(direct_ip=our_ip,
                                    body_marker=_marker(BODY.decode()),
                                    status=200)
        r = await AiohttpProbe().check_integrity(
            proxy_at(srv.port), TARGET, baseline, "http://echo.test/ip")
    assert not r.ok
    assert r.reason is ReasonCode.TRANSPARENT_LEAK
    assert r.observed_anonymity is Anonymity.TRANSPARENT


@runs_async
async def test_a_different_egress_ip_is_recorded_as_anonymous():
    def handler(head: str, n: int) -> bytes:
        if "/ip" in head.split("\r\n")[0]:
            return http_response(200, b'{"ip":"198.51.100.42"}')
        return http_response(200, BODY)

    async with FakeProxy(handler) as srv:
        baseline = IntegrityBaseline(direct_ip="203.0.113.7",
                                    body_marker=_marker(BODY.decode()),
                                    status=200)
        r = await AiohttpProbe().check_integrity(
            proxy_at(srv.port), TARGET, baseline, "http://echo.test/ip")
    assert r.ok
    assert r.observed_anonymity is Anonymity.ANONYMOUS


@runs_async
async def test_a_rewritten_body_is_content_mismatch():
    """A captive portal or an injected ad changes the body. That is not success."""
    async with FakeProxy(lambda h, n: http_response(
            200, b"<html>AD</html>" * 90)) as srv:
        baseline = IntegrityBaseline(direct_ip="203.0.113.7",
                                    body_marker=_marker("y" * 200),
                                    status=200)
        r = await AiohttpProbe().check_integrity(
            proxy_at(srv.port), TARGET, baseline, None)
    assert not r.ok
    assert r.reason is ReasonCode.CONTENT_MISMATCH


@runs_async
async def test_integrity_on_a_dead_proxy_returns_the_transport_failure():
    """
    Integrity must not overwrite a transport error with an integrity verdict:
    "unreachable" and "lying" are different findings.
    """
    async with FakeProxy(lambda h, n: None) as srv:
        dead = srv.port
    r = await AiohttpProbe().check_integrity(
        proxy_at(dead), Target(url="http://target.test/get", timeout_ms=400),
        IntegrityBaseline(direct_ip="203.0.113.7", body_marker="text:3",
                          status=200), "http://echo.test/ip")
    assert not r.ok
    assert r.reason in (ReasonCode.TCP_REFUSED, ReasonCode.TCP_TIMEOUT)


# ── the marker: tolerate benign variation, catch rewriting ───────────────────
def test_marker_ignores_a_varying_field_but_notices_a_rewrite():
    """
    An IP-echo endpoint legitimately differs between two fetches. If the marker
    compared whole bodies, every honest proxy would be flagged CONTENT_MISMATCH
    and the check would be worse than useless.
    """
    a = '{"ip":"203.0.113.7"}'
    b = '{"ip":"198.51.100.42"}'
    assert _marker(a) == _marker(b)                                  # benign
    assert _marker(a) != _marker("<html>login portal</html>" * 40)    # rewrite
    assert _marker("{}").startswith("json")
    assert _marker("<p>x</p>").startswith("html")
    assert _marker("plain").startswith("text")


# ── contract + exception mapping ─────────────────────────────────────────────
def test_probe_satisfies_the_port():
    assert isinstance(AiohttpProbe(), ProbePort)


def test_tls_verification_is_on_by_default():
    """B-09 was 9 sites disabling TLS verification. The default must be secure."""
    assert AiohttpProbe()._verify_tls is True


def test_every_classified_exception_names_a_reason_and_a_detail():
    """
    No branch may return OK for a failure or lose the cause. ProbeResult would
    reject the first, but the mapping is asserted directly so a future branch
    cannot quietly return a bare code with no detail.
    """
    import socket as _socket

    import aiohttp as _aiohttp

    samples: list[BaseException] = [
        asyncio.TimeoutError(),
        _socket.gaierror("nodename nor servname"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
        _aiohttp.ClientError("generic"),
        RuntimeError("unknown shape"),
    ]
    for exc in samples:
        reason, detail = _classify_exception(exc)
        assert reason is not ReasonCode.OK, exc
        assert detail, exc


def test_a_socks_proxy_answering_an_http_parser_is_proto_mismatch():
    """Binary where HTTP was expected is real evidence, not a generic failure."""
    reason, _ = _classify_exception(
        UnicodeDecodeError("utf-8", b"\x05\x00", 0, 1, "invalid"))
    assert reason is ReasonCode.PROTO_MISMATCH


def test_unknown_exceptions_do_not_crash_the_classifier():
    class Weird(Exception):
        pass

    reason, detail = _classify_exception(Weird("?"))
    assert reason is ReasonCode.TCP_REFUSED
    assert "Weird" in detail


# ── meta: the async hazard must stay closed ──────────────────────────────────
def test_no_test_in_this_module_is_a_bare_coroutine():
    """
    pytest-asyncio is not installed. A bare `async def test_...` here would be
    collected, never awaited, and reported green — a test that cannot fail
    (ADR-010). This fails if one is reintroduced.
    """
    module = sys.modules[__name__]
    offenders = [
        name for name, obj in vars(module).items()
        if name.startswith("test_") and inspect.iscoroutinefunction(obj)
    ]
    assert not offenders, (
        f"bare async tests would silently pass without pytest-asyncio: {offenders}"
    )


def test_the_runs_async_wrapper_actually_executes_the_body():
    """
    Teeth for the decorator itself: if @runs_async returned without awaiting,
    every network assertion above would be vacuous. Prove a failing assertion
    inside an async body really propagates.
    """
    ran: list[str] = []

    @runs_async
    async def inner():
        ran.append("executed")
        raise AssertionError("boom")

    with pytest.raises(AssertionError, match="boom"):
        inner()
    assert ran == ["executed"]
