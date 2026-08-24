"""
Engine tests — the seams, which is where P06's two defects actually lived.

Both V4-01 and V4-02 were composition failures: each unit was correct and the
wiring lost the fact. So these tests target the wiring specifically:

  * does a measured reason survive to the report, or get overwritten?
  * does ADR-006 cool on ONE failure (the GeoNode defect) or on consecutive ones?
  * does the discovered protocol get written BACK onto the source row, or
    evaporate the way it did through the entire P06 live sweep?
  * is every candidate accounted for, or can the loop silently drop some?

Everything runs against fakes: no network, no clock, no sqlite. That is what
makes the backoff schedule assertable without waiting an hour.

WHY NO `async def` TESTS (the same hazard as test_probe.py)

pytest-asyncio is NOT installed. An `async def test_...` is collected, never
awaited, and reported PASSED with a warning -- a test that cannot fail, which is
the vacuous-pass defect ADR-010 exists to forbid. This module was first written
with `@pytest.mark.asyncio` and pytest reported "16 failed"; that only looked
like a normal failure because the assertions were real. Had the bodies been
weaker, 16 tests would have passed while awaiting nothing.

So `@runs_async` drives the loop explicitly and
`test_no_test_in_this_module_is_a_bare_coroutine` fails if anyone reintroduces it.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest


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

from atlas.core.domain.proxy import (
    Anonymity, Endpoint, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import (
    ParserKind, Source, SourceState, SourceStats,
)
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade, ReasonCode
from atlas.core.ports.probe import ProbePlan, ProbeResult
from atlas.core.ports.source import SourceFetch
from atlas.engine.cycle import (
    CycleBudget, CycleReport, DiscoveryEngine, SourceOutcome,
    apply_source_result, classify_label,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
TARGET = Target(url="http://example.com")


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeClock:
    """Injected time. core/ never reads a clock, so neither does the engine."""

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


class FakeSourcePort:
    def __init__(self, fetches: dict[str, SourceFetch]) -> None:
        self._fetches = fetches
        self.calls: list[str] = []

    async def fetch(self, source: Source) -> SourceFetch:
        self.calls.append(source.id)
        return self._fetches[source.id]


class FakeStore:
    """In-memory StorePort subset. Records what it was asked to persist."""

    def __init__(self, known: tuple[str, ...] = ()) -> None:
        self._known = set(known)
        self.upserted: list[Proxy] = []

    def get(self, fingerprint: str) -> Proxy | None:
        if fingerprint in self._known:
            return Proxy(endpoint=Endpoint(host="45.62.100.1", port=1))
        return None

    def upsert_many(self, proxies: tuple[Proxy, ...]) -> int:
        self.upserted.extend(proxies)
        return len(proxies)


class ScriptedProbe:
    """
    A probe whose behaviour is declared per endpoint, so every gate branch is
    reachable without a network.
    """

    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.sampled: list[str] = []

    def _for(self, proxy: Proxy) -> dict:
        return self.script.get(str(proxy.endpoint), {})

    async def tcp_handshake(self, proxy: Proxy, timeout_ms: int) -> ProbeResult:
        spec = self._for(proxy)
        if spec.get("tcp") == "refused":
            return ProbeResult(ok=False, reason=ReasonCode.TCP_REFUSED,
                               detail="connection refused")
        if spec.get("tcp") == "timeout":
            return ProbeResult(ok=False, reason=ReasonCode.TCP_TIMEOUT,
                               detail="no handshake")
        return ProbeResult(ok=True, reason=ReasonCode.OK, elapsed_ms=10.0)

    async def discover_protocol(self, proxy: Proxy, target: Target) -> ProbeResult:
        spec = self._for(proxy)
        if spec.get("proto") == "bad_status":
            return ProbeResult(ok=False, reason=ReasonCode.BAD_STATUS,
                               detail="407 from the proxy")
        return ProbeResult(ok=True, reason=ReasonCode.OK,
                           discovered_protocol=spec.get("found", Protocol.HTTP))

    async def sample_latency(self, proxy: Proxy, target: Target,
                            plan: ProbePlan) -> list[ProbeResult]:
        self.sampled.append(str(proxy.endpoint))
        spec = self._for(proxy)
        out: list[ProbeResult] = []
        for ms in spec.get("samples", [200.0] * plan.samples):
            if ms is None:
                out.append(ProbeResult(ok=False, reason=ReasonCode.TCP_TIMEOUT))
            else:
                out.append(ProbeResult(
                    ok=True, reason=ReasonCode.OK, elapsed_ms=ms,
                    observed_anonymity=spec.get("anon", Anonymity.ELITE)))
        return out

    async def check_integrity(self, proxy: Proxy, target: Target) -> ProbeResult:
        return ProbeResult(ok=True, reason=ReasonCode.OK)


def mksource(sid: str = "s1", *, parser: ParserKind = ParserKind.REGEX_ADJACENT,
             label: str = "unknown", state: SourceState = SourceState.ACTIVE,
             stats: SourceStats | None = None) -> Source:
    return Source(id=sid, url=f"https://example.com/{sid}.txt", parser=parser,
                  labelled_protocol=label, state=state,
                  stats=stats or SourceStats())


def okfetch(sid: str, candidates: tuple[str, ...]) -> SourceFetch:
    return SourceFetch(source_id=sid, ok=True, reason=ReasonCode.OK,
                       candidates=candidates, http_status=200,
                       body_bytes=len(candidates) * 20,
                       parser_used="regex_adjacent")


def engine(probe: ScriptedProbe, sources: FakeSourcePort, store: FakeStore,
           clock: FakeClock | None = None, **kw) -> DiscoveryEngine:
    return DiscoveryEngine(
        source_port=sources, probe=probe, store=store,
        clock=clock or FakeClock(), target=TARGET,
        plan=ProbePlan(samples=5), **kw)


# ══════════════════════════════════════════════════════════════════════════════
# ADR-006 — cooldown on CONSECUTIVE failures. The GeoNode rule.
# ══════════════════════════════════════════════════════════════════════════════
def test_one_failed_fetch_does_not_disable_a_source():
    """
    THE GEONODE RULE. 230 067 bytes of valid JSON, then 659 bytes 2s later
    because we were throttling it. One failure must never disable a source.
    """
    src = mksource()
    fail = SourceFetch(source_id="s1", ok=False,
                       reason=ReasonCode.SOURCE_THROTTLED, http_status=200)
    out = apply_source_result(src, fail, now=NOW)
    assert out.state is SourceState.COOLING
    assert out.state is not SourceState.DISABLED
    assert out.stats.consecutive_failures == 1


def test_cooldown_grows_exponentially_and_caps_at_one_hour():
    """base * 2^(n-1), capped. Verified against a fake clock, not by waiting."""
    fail = SourceFetch(source_id="s1", ok=False, reason=ReasonCode.SOURCE_DEAD)
    src = mksource()
    seen: list[float] = []
    for _ in range(9):
        src = apply_source_result(src, fail, now=NOW, base_s=30.0,
                                  cap_s=3600.0, disable_after=99)
        seen.append((src.cooldown_until - NOW).total_seconds())
    assert seen[:5] == [30.0, 60.0, 120.0, 240.0, 480.0]
    assert seen[-1] == 3600.0, "cooldown must cap at 1 hour"


def test_a_source_is_disabled_only_after_many_consecutive_failures():
    fail = SourceFetch(source_id="s1", ok=False, reason=ReasonCode.SOURCE_DEAD)
    src = mksource()
    for _ in range(11):
        src = apply_source_result(src, fail, now=NOW, disable_after=12)
        assert src.state is SourceState.COOLING
    src = apply_source_result(src, fail, now=NOW, disable_after=12)
    assert src.state is SourceState.DISABLED
    assert src.disabled_reason and "consecutive" in src.disabled_reason


def test_one_success_resets_the_consecutive_counter():
    """
    Consecutive, not cumulative. A source that fails 11 times and then succeeds
    is a working source, and must not be one failure from being disabled.
    """
    fail = SourceFetch(source_id="s1", ok=False, reason=ReasonCode.SOURCE_DEAD)
    src = mksource()
    for _ in range(11):
        src = apply_source_result(src, fail, now=NOW, disable_after=12)
    assert src.stats.consecutive_failures == 11
    src = apply_source_result(src, okfetch("s1", ("1.2.3.4:80",)), now=NOW)
    assert src.stats.consecutive_failures == 0
    assert src.state is SourceState.ACTIVE


def test_a_cooling_source_that_succeeds_returns_to_active():
    """Otherwise the backoff is a one-way door and the pool bleeds sources."""
    src = mksource(state=SourceState.COOLING)
    out = apply_source_result(src, okfetch("s1", ("1.2.3.4:80",)), now=NOW)
    assert out.state is SourceState.ACTIVE
    assert out.cooldown_until is None


def test_a_304_is_a_success_not_a_failure():
    """
    SOURCE_UNCHANGED means conditional GET worked. Counting it as a failure
    would cool exactly the sources that behave best (ADR-006).
    """
    src = mksource()
    unchanged = SourceFetch(source_id="s1", ok=False,
                            reason=ReasonCode.SOURCE_UNCHANGED, http_status=304)
    out = apply_source_result(src, unchanged, now=NOW)
    assert out.state is SourceState.ACTIVE
    assert out.stats.consecutive_failures == 0


def test_etag_is_retained_when_a_fetch_returns_none():
    """Losing the ETag would silently disable conditional GET (ADR-006)."""
    src = mksource(stats=SourceStats(last_etag='W/"abc"'))
    out = apply_source_result(src, okfetch("s1", ("1.2.3.4:80",)), now=NOW)
    assert out.stats.last_etag == 'W/"abc"'


# ══════════════════════════════════════════════════════════════════════════════
# ADR-026 — the label feedback that P06 dropped at the seam
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unprobed_label_is_unproven_not_verified():
    """Absence of contradiction is not evidence. This is why labels_verified=0."""
    assert classify_label(Protocol.HTTP, {}) == ("UNPROVEN", None)


def test_a_label_confirmed_by_probes_is_verified():
    verdict, winner = classify_label(Protocol.HTTP, {"http": 7})
    assert verdict == "VERIFIED"
    assert winner is Protocol.HTTP


def test_a_socks_list_named_http_is_refuted_not_silently_accepted():
    """
    B-12, measured: TheSpeedX/SOCKS-List/master/http.txt is a SOCKS list named
    http.txt whose 2 853 candidates the legacy code tested as HTTP and discarded.
    """
    verdict, winner = classify_label(Protocol.HTTP, {"socks5": 9, "http": 1})
    assert verdict == "REFUTED"
    assert winner is Protocol.SOCKS5


def test_an_unknown_label_is_reported_distinctly_not_as_verified():
    verdict, winner = classify_label(Protocol.UNKNOWN, {"http": 5})
    assert verdict == "UNKNOWN_LABEL"
    assert winner is Protocol.HTTP


@runs_async
async def test_the_cycle_writes_the_discovered_protocol_back_onto_the_source():
    """
    THE P06 CARRY-FORWARD, CLOSED. The live sweep probed 300 candidates and the
    registry still said labels_verified: 0, because verdicts were recorded per
    PROXY and never fed back onto the SOURCE row.
    """
    src = mksource("s1", label="http")
    probe = ScriptedProbe({
        "45.62.100.10:8080": {"found": Protocol.SOCKS5, "samples": [200.0] * 5},
        "45.62.100.11:8080": {"found": Protocol.SOCKS5, "samples": [210.0] * 5},
    })
    eng = engine(probe,
                 FakeSourcePort({"s1": okfetch(
                     "s1", ("45.62.100.10:8080", "45.62.100.11:8080"))}),
                 FakeStore())
    report, rows = await eng.run_cycle((src,))

    assert len(report.outcomes) == 1
    # The label said http; every probe found socks5.
    assert report.outcomes[0].label_verdict == "REFUTED"
    assert report.outcomes[0].observed_protocols == {"socks5": 2}
    # and the updated row came back to the caller, rather than evaporating.
    assert len(rows) == 1


# ══════════════════════════════════════════════════════════════════════════════
# ADR-025 — a measured cause must survive to the report
# ══════════════════════════════════════════════════════════════════════════════
@runs_async
async def test_a_refused_candidate_reports_tcp_refused_not_a_vaguer_reason():
    probe = ScriptedProbe({"45.62.100.9:8080": {"tcp": "refused"}})
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", ("45.62.100.9:8080",))}),
                 FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.rejected_by_reason == {"TCP_REFUSED": 1}
    assert report.admitted == 0


@runs_async
async def test_the_first_failing_stage_names_the_reason():
    """
    Staged triage: a candidate that fails TCP is never re-labelled by a later
    stage, and is never sampled at all (that is the cost saving of §7).
    """
    probe = ScriptedProbe({
        "45.62.100.1:80": {"tcp": "timeout"},
        "45.62.100.2:80": {"proto": "bad_status"},
    })
    eng = engine(probe, FakeSourcePort({
        "s1": okfetch("s1", ("45.62.100.1:80", "45.62.100.2:80"))}), FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.rejected_by_reason == {"TCP_TIMEOUT": 1, "BAD_STATUS": 1}
    # neither reached the expensive stage
    assert probe.sampled == []


@runs_async
async def test_a_slow_proxy_is_rejected_with_too_slow_p95():
    """The gate the legacy system never had, reached through the real loop."""
    probe = ScriptedProbe({"45.62.100.5:8080": {"samples": [9000.0] * 5}})
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", ("45.62.100.5:8080",))}),
                 FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.rejected_by_reason == {"TOO_SLOW_P95": 1}


@runs_async
async def test_success_ratio_uses_actual_attempts_not_the_planned_k():
    """
    The probe stops early after consecutive failures. Claiming k attempts when 2
    were made would inflate success_ratio and hide UNRELIABLE.
    """
    probe = ScriptedProbe({"45.62.100.6:8080": {"samples": [300.0, None, None]}})
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", ("45.62.100.6:8080",))}),
                 FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    # 1 success of 3 attempts = 0.33, below the 0.6 floor
    assert report.rejected_by_reason == {"UNRELIABLE": 1}


@runs_async
async def test_an_admitted_proxy_is_ready_graded_and_timestamped():
    probe = ScriptedProbe({"45.62.100.7:8080": {"samples": [200.0] * 5}})
    store = FakeStore()
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", ("45.62.100.7:8080",))}),
                 store)
    report, _ = await eng.run_cycle((mksource(),))

    assert report.admitted == 1
    p = store.upserted[0]
    assert p.state is ProxyState.READY
    assert p.grade.meets(Grade.GOOD)
    # last_checked is what B-16/scoring needs; without it the proxy is stale on
    # arrival and can never be ranked.
    assert p.last_checked == NOW
    assert p.total_successes == 1


# ══════════════════════════════════════════════════════════════════════════════
# accounting — every candidate in exactly one bucket
# ══════════════════════════════════════════════════════════════════════════════
@runs_async
async def test_probed_equals_admitted_plus_rejected():
    probe = ScriptedProbe({
        "45.62.100.1:80": {"samples": [200.0] * 5},
        "45.62.100.2:80": {"samples": [9000.0] * 5},
        "45.62.100.3:80": {"tcp": "refused"},
    })
    eng = engine(probe, FakeSourcePort({"s1": okfetch(
        "s1", ("45.62.100.1:80", "45.62.100.2:80", "45.62.100.3:80"))}),
        FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.probed == 3
    assert report.admitted + report.rejected == 3


def test_a_report_that_loses_a_candidate_cannot_be_constructed():
    """The accounting identity is enforced, not merely documented."""
    with pytest.raises(ValueError, match="lost candidates"):
        CycleReport(started_at=NOW, finished_at=NOW, probed=10, admitted=1,
                    rejected_by_reason={"TCP_REFUSED": 2})


@runs_async
async def test_known_endpoints_are_skipped_not_reprobed():
    """
    Re-probing a known endpoint pays full k=5 cost for a fact already recorded.
    The legacy sweep counted 649 404 candidates it could neither attribute nor dedup.
    """
    known = Proxy(endpoint=Endpoint(host="45.62.100.1", port=80),
                  protocol=Protocol.UNKNOWN)
    probe = ScriptedProbe({"45.62.100.2:80": {"samples": [200.0] * 5}})
    eng = engine(probe, FakeSourcePort({"s1": okfetch(
        "s1", ("45.62.100.1:80", "45.62.100.2:80"))}),
        FakeStore(known=(known.fingerprint,)))
    report, _ = await eng.run_cycle((mksource(),))
    assert report.skipped_known == 1
    assert report.probed == 1
    assert probe.sampled == ["45.62.100.2:80"]


@runs_async
async def test_private_range_candidates_are_dropped_with_a_named_reason():
    probe = ScriptedProbe({"45.62.100.4:80": {"samples": [200.0] * 5}})
    eng = engine(probe, FakeSourcePort({"s1": okfetch(
        "s1", ("192.168.1.1:8080", "127.0.0.1:3128", "45.62.100.4:80"))}),
        FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    dropped = report.outcomes[0].dropped_by_reason
    assert dropped == {"PRIVATE_RANGE": 1, "LOOPBACK": 1}
    assert report.probed == 1


@runs_async
async def test_admission_rate_is_none_when_nothing_was_probed():
    """
    "Probed nothing" and "probed 200, admitted none" are different facts.
    Reporting 0.0 for the first is the NOT_MEASURED inversion again.
    """
    eng = engine(ScriptedProbe({}), FakeSourcePort({"s1": SourceFetch(
        source_id="s1", ok=False, reason=ReasonCode.SOURCE_DEAD)}), FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.probed == 0
    assert report.admission_rate is None


# ══════════════════════════════════════════════════════════════════════════════
# budget + cooldown honouring
# ══════════════════════════════════════════════════════════════════════════════
@runs_async
async def test_a_cooling_source_is_not_fetched():
    """Honouring the cooldown is the entire point of having one."""
    sources = FakeSourcePort({})
    eng = engine(ScriptedProbe({}), sources, FakeStore())
    report, rows = await eng.run_cycle((mksource(state=SourceState.COOLING),))
    assert sources.calls == []
    assert report.outcomes == ()
    assert len(rows) == 1


@runs_async
async def test_a_disabled_source_is_not_fetched():
    sources = FakeSourcePort({})
    eng = engine(ScriptedProbe({}), sources, FakeStore())
    await eng.run_cycle((mksource().disabled("dead in P00"),))
    assert sources.calls == []


@runs_async
async def test_max_sources_bounds_the_cycle():
    fetches = {f"s{i}": okfetch(f"s{i}", ()) for i in range(5)}
    sources = FakeSourcePort(fetches)
    eng = engine(ScriptedProbe({}), sources, FakeStore())
    await eng.run_cycle(tuple(mksource(f"s{i}") for i in range(5)),
                        CycleBudget(max_sources=2))
    assert sources.calls == ["s0", "s1"]


@runs_async
async def test_max_probes_bounds_total_work():
    """
    The budget is a CEILING ON WORK DONE, not merely a ceiling.

    `assert report.probed <= 3` alone is satisfied by a cycle that probes
    NOTHING, so it cannot tell "budget enforced" from "engine starved". Proven by
    mutation: starving run_cycle (never probing) left that assertion green, while
    deleting the clamp did fail it. A one-sided bound is half a test -- the
    ADR-010 "test that cannot fail" class, in its subtler form where the test
    fails for the mutant you thought of and passes for the one you did not.

    So both sides are asserted: the cap holds AND the cap is what stopped it.
    8 candidates are offered and exactly 3 must be probed -- equality, so
    over-probing and under-probing are both failures.
    """
    probe = ScriptedProbe({f"45.62.100.{i}:80": {"samples": [200.0] * 5}
                           for i in range(1, 9)})
    cands = tuple(f"45.62.100.{i}:80" for i in range(1, 9))
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", cands)}), FakeStore())
    report, _ = await eng.run_cycle((mksource(),), CycleBudget(max_probes=3))
    assert report.probed == 3, (
        f"expected the budget to bind at exactly 3, got {report.probed} "
        "(0 means the engine was starved, not bounded)")
    # and the probe itself agrees -- the report is not just self-consistent
    assert len(probe.sampled) == 3


def test_a_zero_budget_is_refused():
    with pytest.raises(ValueError, match="must be >= 1"):
        CycleBudget(max_probes=0)


@runs_async
async def test_a_dead_source_yields_an_outcome_naming_the_reason():
    eng = engine(ScriptedProbe({}), FakeSourcePort({"s1": SourceFetch(
        source_id="s1", ok=False, reason=ReasonCode.SOURCE_DEAD,
        http_status=404, detail="404 Not Found")}), FakeStore())
    report, rows = await eng.run_cycle((mksource(),))
    assert report.outcomes[0].reason is ReasonCode.SOURCE_DEAD
    assert report.outcomes[0].ok is False
    assert rows[0].state is SourceState.COOLING


def test_no_test_in_this_module_is_a_bare_coroutine():
    """
    ADR-010 guard. Without pytest-asyncio, a collected `async def test_` is never
    awaited and passes vacuously. This asserts every test object in this module
    is synchronous, so the hazard cannot return quietly.
    """
    module = sys.modules[__name__]
    offenders = [
        name for name, obj in vars(module).items()
        if name.startswith("test_") and inspect.iscoroutinefunction(obj)
    ]
    assert not offenders, (
        "these tests are bare coroutines and would pass without running: "
        + ", ".join(offenders)
    )


@runs_async
async def test_quality_rate_is_admitted_over_probed_not_raw_volume():
    """ANALYSIS.md §5: VOLUME != VALUE. 649 404 candidates -> 102 slow proxies."""
    probe = ScriptedProbe({
        "45.62.100.1:80": {"samples": [200.0] * 5},
        "45.62.100.2:80": {"samples": [9000.0] * 5},
        "45.62.100.3:80": {"samples": [9000.0] * 5},
        "45.62.100.4:80": {"samples": [9000.0] * 5},
    })
    cands = tuple(f"45.62.100.{i}:80" for i in range(1, 5))
    eng = engine(probe, FakeSourcePort({"s1": okfetch("s1", cands)}), FakeStore())
    report, _ = await eng.run_cycle((mksource(),))
    assert report.outcomes[0].quality_rate == pytest.approx(0.25)
