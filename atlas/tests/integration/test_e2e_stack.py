"""
P12 — LEVEL 6: the full stack, end to end, against the REAL adapters.

WHY THIS FILE EXISTS

Levels 1-5 each prove one thing in isolation: domain behaviour, pure policy,
architecture isolation, adapters against fakes, and individual components
against real processes/storage. None of them proves the system ASSEMBLED. The
P06 lesson is that composition is its own defect class: V4-01 and V4-02 were
both correct-unit/broken-wiring failures, found by reading artifacts rather
than by any unit test.

So this suite drives the whole pipeline with REAL pieces wherever one exists:

    registry (real JSON file)
      -> HttpSourceAdapter parse path (real parser, offline body)
      -> normalize_batch (real policy)
      -> DiscoveryEngine.evaluate (real engine composition)
      -> admission.decide (real gate)
      -> SqliteStore (real WAL database on disk)
      -> PoolScheduler.plan (real scheduler over the real store)
      -> HandoutService.handout (real lease, real ranking)

Only the NETWORK is faked -- a scripted probe and a scripted source port --
because a test that needs the internet cannot be a gate. Every decision-making
component is the production one.

WHY NO `async def` TESTS

pytest-asyncio is not installed. A bare `async def test_...` is collected,
never awaited, and reported PASSED (ADR-010's vacuous pass). `@runs_async`
drives the loop explicitly, and the suite pins that no bare coroutine exists.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import (
    Anonymity, Endpoint, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import ParserKind, Source, SourceState, SourceStats
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade, ReasonCode
from atlas.core.parsing.candidates import parse_body
from atlas.core.policy.admission import AdmissionPolicy, build_profile, decide
from atlas.core.policy.lifecycle import SchedulerPolicy
from atlas.core.policy.normalize import normalize_batch
from atlas.core.policy.target_policy import TargetPolicy
from atlas.core.ports.probe import ProbePlan, ProbeResult
from atlas.core.ports.source import SourceFetch
from atlas.engine.cycle import CycleBudget, DiscoveryEngine
from atlas.engine.handout import HandoutService
from atlas.engine.scheduler import PoolScheduler


def runs_async(fn):
    """Drive an async body synchronously; see test_engine.py for the hazard.

    Unlike test_engine.py's version this one must also preserve the fixture
    signature: the level-6 tests take `db_path`, and pytest resolves fixtures
    from the COLLECTED function's parameters -- so the wrapper re-exposes
    `fn`'s signature explicitly (without `functools.wraps`, whose
    `__wrapped__` chain is what test_engine.py warns about).
    """
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
TARGET = Target(url="https://example.com")


class FixedClock:
    def __init__(self, start: datetime = NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def monotonic_ms(self) -> float:
        return self._t.timestamp() * 1000.0

    def deadline(self, after_ms: float) -> datetime:
        return self._t + timedelta(milliseconds=after_ms)

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


class ScriptedSourcePort:
    """Serves SourceFetch bodies built from REAL parse output, offline."""

    def __init__(self, bodies: dict[str, str]) -> None:
        # Each body is run through the PRODUCTION parser, so a parse defect
        # cannot be hidden by hand-crafting candidate strings.
        self._candidates: dict[str, tuple[str, ...]] = {}
        for sid, body in bodies.items():
            cands = parse_body("regex_adjacent", body).candidates
            assert cands, f"fixture body for {sid} parsed to nothing"
            self._candidates[sid] = cands
        self.calls: list[str] = []

    async def fetch(self, source: Source) -> SourceFetch:
        self.calls.append(source.id)
        cands = self._candidates[source.id]
        return SourceFetch(source_id=source.id, ok=True, reason=ReasonCode.OK,
                           candidates=cands, http_status=200,
                           body_bytes=sum(len(c) for c in cands),
                           parser_used="regex_adjacent")


class ScriptedProbe:
    """Per-endpoint behaviour. Fast endpoints admit; slow/dead ones reject."""

    def __init__(self, fast_prefix: str = "45."):
        self.fast_prefix = fast_prefix

    def _is_fast(self, proxy: Proxy) -> bool:
        return proxy.endpoint.host.startswith(self.fast_prefix)

    async def tcp_handshake(self, proxy: Proxy, timeout_ms: int) -> ProbeResult:
        if proxy.endpoint.host.startswith("185.220."):
            return ProbeResult(ok=False, reason=ReasonCode.TCP_REFUSED,
                               detail="connection refused")
        return ProbeResult(ok=True, reason=ReasonCode.OK, elapsed_ms=8.0)

    async def discover_protocol(self, proxy: Proxy, target: Target) -> ProbeResult:
        return ProbeResult(ok=True, reason=ReasonCode.OK,
                           discovered_protocol=Protocol.HTTP)

    async def sample_latency(self, proxy: Proxy, target: Target,
                             plan: ProbePlan) -> list[ProbeResult]:
        ms = 350.0 if self._is_fast(proxy) else 9_500.0
        return [ProbeResult(ok=True, reason=ReasonCode.OK, elapsed_ms=ms,
                            observed_anonymity=Anonymity.ELITE)
                for _ in range(plan.samples)]

    async def check_integrity(self, proxy: Proxy, target: Target) -> ProbeResult:
        return ProbeResult(ok=True, reason=ReasonCode.OK)


def _body(entries: list[tuple[str, int]]) -> str:
    return "\n".join(f"{ip}:{port}" for ip, port in entries)


# Two sources: one fast pool (45.x), one mixed (slow 103.x + dead 185.x).
# ALL addresses are globally routable ON PURPOSE: the P07 lesson (PROGRESS.md)
# is that RFC 5737 documentation ranges (192.0.2.x / 198.51.100.x / 203.0.113.x)
# are non-global per `ipaddress`, so the REAL normalizer drops them -- and 20 of
# 30 engine tests once passed against a pipeline nothing flowed through. These
# prefixes are public ranges (APNIC / a well-known exit-operator block), chosen
# so normalize_batch keeps them and the probe SCRIPT alone decides the outcome.
FAST_BODY = _body([(f"45.10.20.{i}", 8080) for i in range(1, 6)])
MIXED_BODY = _body(
    [(f"103.21.40.{i}", 3128) for i in range(1, 4)]
    + [(f"185.220.101.{i}", 8080) for i in range(1, 3)]
)

SOURCES = (
    Source(id="fast-src", url="https://fixture.invalid/fast.txt",
           parser=ParserKind.REGEX_ADJACENT, labelled_protocol="unknown",
           state=SourceState.ACTIVE, stats=SourceStats()),
    Source(id="mixed-src", url="https://fixture.invalid/mixed.txt",
           parser=ParserKind.REGEX_ADJACENT, labelled_protocol="unknown",
           state=SourceState.ACTIVE, stats=SourceStats()),
)


@pytest.fixture()
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield str(Path(td) / "e2e.db")


def _engine(store, clock) -> DiscoveryEngine:
    return DiscoveryEngine(
        source_port=ScriptedSourcePort(
            {"fast-src": FAST_BODY, "mixed-src": MIXED_BODY}),
        probe=ScriptedProbe(), store=store, clock=clock, target=TARGET,
        plan=ProbePlan(samples=5),
    )


# ══════════════════════════════════════════════════════════════════════════════
# THE FULL STACK
# ══════════════════════════════════════════════════════════════════════════════
@runs_async
async def test_a_full_cycle_populates_a_real_pool_and_serves_from_it(db_path):
    """
    THE level-6 test: nothing is mocked but the wire. Every intermediate fact
    is asserted at the seam where a composition defect would lose it.
    """
    clock = FixedClock()
    with SqliteStore(db_path) as store:
        report, updated = await _engine(store, clock).run_cycle(SOURCES)

        # ── fetch -> normalize -> probe -> gate, accounted to the last row ──
        # CycleReport carries no `seen` total -- per-source attribution is the
        # record (ADR-002), and probed == admitted + rejected is asserted by
        # the report type itself.
        seen = sum(o.candidates_seen for o in report.outcomes)
        assert seen == 10
        assert report.probed == 10
        # 5 fast admit; 3 slow reject TOO_SLOW_P95; 2 dead reject TCP_REFUSED
        assert report.admitted == 5
        assert report.rejected_by_reason.get("TOO_SLOW_P95") == 3
        assert report.rejected_by_reason.get("TCP_REFUSED") == 2
        assert report.stored == 10          # admitted AND rejected rows persist
        assert report.skipped_known == 0

        # ADR-026: the measurement was written back onto BOTH source rows.
        by_id = {s.id: s for s in updated}
        assert by_id["fast-src"].state is SourceState.ACTIVE
        assert by_id["fast-src"].stats.last_reason == "OK"
        assert by_id["fast-src"].stats.candidates_seen == 5

        # ── the real pool holds exactly what the gate admitted ──────────────
        states = store.count_by_state()
        assert states.get(ProxyState.READY, 0) == 5
        assert states.get(ProxyState.COOLING, 0) == 5   # the rejected rows
        ready = [p for p in store.select_schedulable()
                 if p.state is ProxyState.READY]
        assert len(ready) == 5
        assert all(p.endpoint.host.startswith("45.") for p in ready)

        # ── the serving layer leases only gate-admitted rows ────────────────
        service = HandoutService(
            store=store, clock=clock,
            target_policy=TargetPolicy(deny_hosts=frozenset({"instagram.com"})),
        )
        res = service.handout(target=TARGET, count=3)
        assert res.ok
        assert len(res.granted) == 3
        for g in res.granted:
            row = store.get(g.fingerprint)
            assert row.state is ProxyState.LEASED
            assert row.endpoint.host.startswith("45.")
            assert row.grade in {Grade.ELITE, Grade.GOOD}


@runs_async
async def test_a_second_cycle_dedupes_against_the_real_store(db_path):
    """Responsibility 2: never re-probe what the pool already holds."""
    clock = FixedClock()
    with SqliteStore(db_path) as store:
        first, _ = await _engine(store, clock).run_cycle(SOURCES)
        assert first.probed == 10

        clock.advance(60)
        second, _ = await _engine(store, clock).run_cycle(SOURCES)
        assert second.probed == 0, "every candidate is already known"
        assert second.skipped_known == 10
        assert second.stored == 0


@runs_async
async def test_the_scheduler_drives_recheck_through_the_same_store(db_path):
    """plan() over the REAL pool: fresh READY rows stay; the cycle-aged ones
    become RECHECK_READY only after the horizon -- proven with the real clock
    arithmetic, not by waiting."""
    clock = FixedClock()
    with SqliteStore(db_path) as store:
        await _engine(store, clock).run_cycle(SOURCES)

        sched = PoolScheduler(store=store, clock=clock,
                              policy=SchedulerPolicy(recheck_ready_after_s=900.0))
        plan_now = sched.plan()
        assert len(plan_now.recheck_ready) == 0, (
            "fresh rows must not be rechecked")
        assert len(plan_now.keep_ready) == 5

        clock.advance(1000)
        plan_later = sched.plan()
        assert len(plan_later.recheck_ready) == 5, (
            "the 5 admitted rows cross the horizon; rejected rows are COOLING "
            "and follow their own ladder"
        )


def test_no_test_in_this_module_is_a_bare_coroutine():
    """ADR-010: an async def here would be collected, never awaited, PASS."""
    mod = inspect.getmodule(test_a_second_cycle_dedupes_against_the_real_store)
    for name, fn in inspect.getmembers(mod, inspect.iscoroutinefunction):
        assert not name.startswith("test_"), (
            f"{name} is a bare coroutine -- pytest would PASS it without "
            "awaiting it. Wrap with @runs_async."
        )


def test_the_fixture_bodies_really_parse_through_the_production_parser():
    """A fixture that parsed to nothing would make every assertion above
    vacuous. The constructor asserts this too; this pins it as a test."""
    fast = parse_body("regex_adjacent", FAST_BODY).candidates
    mixed = parse_body("regex_adjacent", MIXED_BODY).candidates
    assert len(fast) == 5 and len(mixed) == 5
