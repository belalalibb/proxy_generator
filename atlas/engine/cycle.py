"""
THE ENGINE LOOP — registry -> fetch -> normalize -> probe -> gate -> store.

Every component this composes was built and tested in isolation across P02-P06.
This module is where they finally meet, and composition is its own risk: each
piece can be correct while the wiring silently drops the very facts the pieces
were built to produce. P06 proved that twice over -- V4-01 lost a measured cause
to an untested-rung placeholder, V4-02 inverted a percentile -- and both defects
lived in the seams, not in the units. So this file states what it is responsible
for, and asserts it.

WHAT THE LEGACY LOOP DID (proxy_generator_v2.py, v1-v3.py)

  * fetched hardcoded URLs with 100-150 threads, causing its own 429/403
  * accepted on `status == 200 and len(text) > 1000`, one sample
  * appended survivors to proxy.txt with no timestamp and no state
  * on any exception: `pass` / `continue` (23 silent handlers, B-02)

Consequently one bad fetch permanently poisoned a source's reputation (nothing
recorded WHY), and 15 000 candidates became 102 proxies at p95 15 903 ms.

FOUR RESPONSIBILITIES, EACH TIED TO A MEASURED DEFECT

  1. ADR-006 cooldown on CONSECUTIVE failures, never on one.
     GeoNode returned 230 067 bytes of valid JSON, then 659 bytes ~2s later
     because WE were throttling it. A single failure must not disable a source.

  2. Never re-probe what the pool already holds.
     The legacy sweep counted 649 404 candidates from GitHub across ~50 repos and
     could not attribute or dedup them, so it paid full probe cost for endpoints
     it already had.

  3. Feed the measurement BACK onto the source row (ADR-026).
     P06 probed 300 real candidates and the registry still reported
     labels_verified: 0 -- verdicts were recorded per PROXY and never written
     back onto the SOURCE. A discovered protocol is the only evidence that can
     verify or refute a label, and it was being thrown away at the seam. This is
     ADR-019's defect class (a captured fact that nothing reads), one layer up.

  4. Account for every candidate.
     seen == admitted + rejected + skipped + dropped, asserted on the report.
     "We ingested 500" and "we ingested 500 and silently discarded 900" are
     different facts, and the legacy system could only ever report the first.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not read the clock (ClockPort is injected), does not construct its own
adapters, and does not decide admission -- `admission.decide` owns that. It is
orchestration, so it can be tested against fakes with zero network, which is the
only way the ADR-006 backoff schedule is verifiable without waiting an hour.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol as TypingProtocol

from atlas.core.domain.proxy import (
    Anonymity, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import Source, SourceState
from atlas.core.domain.verdict import Grade, ReasonCode, Verdict
from atlas.core.parsing.candidates import parse_body
from atlas.core.policy.admission import AdmissionPolicy, build_profile, decide
from atlas.core.policy.normalize import normalize_batch
from atlas.core.ports.clock import ClockPort, cooldown_delay
from atlas.core.ports.probe import ProbePlan, ProbeResult
from atlas.core.ports.source import SourceFetch


@dataclass(frozen=True, slots=True)
class CycleBudget:
    """
    Bounds on one cycle. The legacy code had `MAX_PROXIES = 15000` and no time or
    cost bound at all, so a run's duration was whatever the internet decided.

    Every field is a bound on WORK, not a target for OUTPUT: a cycle that probes
    fewer candidates because the sources were thin is a smaller cycle, not a
    failed one.
    """
    max_sources: int = 10
    max_candidates_per_source: int = 500
    max_probes: int = 200
    probe_concurrency: int = 20

    def __post_init__(self) -> None:
        for name in ("max_sources", "max_candidates_per_source",
                     "max_probes", "probe_concurrency"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1, got {getattr(self, name)}")


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """
    What one source produced, and what it cost. The unit of ATTRIBUTION (ADR-002).

    `label_verdict` is the ADR-026 feedback: VERIFIED when probes agreed with the
    registry's hint, REFUTED when they contradicted it, UNPROVEN when no probe
    succeeded. Never silently absent -- that is how labels_verified stayed 0.
    """
    source_id: str
    ok: bool
    reason: ReasonCode
    candidates_seen: int = 0
    candidates_accepted: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)
    already_known: int = 0
    probed: int = 0
    admitted: int = 0
    elite: int = 0
    label_verdict: str = "UNPROVEN"
    observed_protocols: dict[str, int] = field(default_factory=dict)
    cooldown_until: datetime | None = None
    detail: str | None = None

    @property
    def quality_rate(self) -> float | None:
        """admitted / probed -- the ANALYSIS.md §5 lesson: volume != value."""
        return self.admitted / self.probed if self.probed else None


@dataclass(frozen=True, slots=True)
class CycleReport:
    """
    One cycle, fully accounted. Constructed through `finish()` so the accounting
    identity is checked on every instance rather than trusted.
    """
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[SourceOutcome, ...] = ()
    probed: int = 0
    admitted: int = 0
    stored: int = 0
    skipped_known: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def rejected(self) -> int:
        return sum(self.rejected_by_reason.values())

    @property
    def admission_rate(self) -> float | None:
        """
        admitted / probed. The figure P06 calibrated at 3/12 on gate-reachers.

        None when nothing was probed -- NOT 0.0. "We probed nothing" and "we
        probed 200 and admitted none" are different facts, and reporting 0.0 for
        the first would make an idle cycle indistinguishable from a total
        failure. Same inversion as NOT_MEASURED.
        """
        return self.admitted / self.probed if self.probed else None

    def __post_init__(self) -> None:
        if self.probed != self.admitted + self.rejected:
            raise ValueError(
                f"probe accounting lost candidates: probed={self.probed} but "
                f"admitted+rejected={self.admitted + self.rejected}. Every probed "
                "candidate must appear in exactly one bucket (B-02)."
            )


class _StoreLike(TypingProtocol):
    """The subset of StorePort the engine needs. Structural, so fakes fit."""
    def get(self, fingerprint: str) -> Proxy | None: ...
    def get_by_endpoint(self, host: str, port: int) -> tuple[Proxy, ...]: ...
    def upsert_many(self, proxies: tuple[Proxy, ...]) -> int: ...


def _protocol_of(row_label: str) -> Protocol:
    """A registry label string -> Protocol, without guessing."""
    try:
        return Protocol(row_label)
    except ValueError:
        return Protocol.UNKNOWN


def classify_label(labelled: Protocol,
                   observed: dict[str, int]) -> tuple[str, Protocol | None]:
    """
    ADR-026. Compare a source's LABEL against what probes actually discovered.

    Pure, and separated from the engine so it is testable without any I/O.

    Returns (verdict, majority_protocol):

      UNPROVEN  no protocol was discovered -- the honest default. An unverified
                label must never be promoted by absence of contradiction.
      VERIFIED  the majority discovered protocol equals the label.
      REFUTED   the majority contradicts the label. This is B-12, measured:
                TheSpeedX/SOCKS-List/master/http.txt is a SOCKS list named
                http.txt whose 2 853 candidates the legacy code tested as HTTP
                and discarded.
      UNKNOWN_LABEL the row never claimed a protocol, so there is nothing to
                verify -- reported distinctly rather than as a false VERIFIED.
    """
    if not observed:
        return "UNPROVEN", None
    majority = max(observed.items(), key=lambda kv: (kv[1], kv[0]))[0]
    winner = _protocol_of(majority)
    if labelled is Protocol.UNKNOWN:
        return "UNKNOWN_LABEL", winner
    return ("VERIFIED" if winner is labelled else "REFUTED"), winner


def apply_source_result(source: Source, fetch: SourceFetch, *,
                        now: datetime, base_s: float = 30.0,
                        cap_s: float = 3600.0,
                        disable_after: int = 12) -> Source:
    """
    ADR-006, as a pure function: update a Source from one fetch outcome.

    A SUCCESS resets consecutive_failures to 0. A FAILURE increments it and
    cools the source for `base * 2^(n-1)`, capped at 1h. Only after
    `disable_after` CONSECUTIVE failures is the source disabled, with a reason.

    Pure and separate from the loop on purpose: this is the rule the GeoNode
    incident bought, and it must be verifiable against a fake clock rather than
    by waiting out a real backoff.
    """
    st = source.stats
    if fetch.ok or fetch.reason is ReasonCode.SOURCE_UNCHANGED:
        # SOURCE_UNCHANGED is a 304: the source answered correctly and cheaply.
        # Counting it as a failure would cool the very sources that behave best
        # under conditional GET, which is the opposite of ADR-006's intent.
        healed = source.with_stats(replace(
            st,
            fetches=st.fetches + 1,
            consecutive_failures=0,
            candidates_seen=st.candidates_seen + len(fetch.candidates),
            candidates_unique=st.candidates_unique + fetch.unique_candidates,
            last_fetch=now,
            last_success=now,
            last_reason=fetch.reason.value,
            last_etag=fetch.etag or st.last_etag,
            last_modified=fetch.last_modified or st.last_modified,
        ))
        # A cooling source that succeeds returns to ACTIVE. Without this the
        # backoff would be a one-way door: the source would serve its cooldown,
        # prove itself, and stay COOLING forever.
        return healed.reactivated() if source.state is SourceState.COOLING else healed

    n = st.consecutive_failures + 1
    updated = source.with_stats(replace(
        st,
        fetches=st.fetches + 1,
        fetch_failures=st.fetch_failures + 1,
        consecutive_failures=n,
        last_fetch=now,
        last_reason=fetch.reason.value,
    ))
    if n >= disable_after:
        return updated.disabled(
            f"{n} consecutive failures, last {fetch.reason.value} "
            f"(ADR-006: disabled after {disable_after}, never after one)"
        )
    return updated.cooling(
        now + cooldown_delay(n, base_s=base_s, cap_s=cap_s),
        f"consecutive_failures={n}, last {fetch.reason.value}",
    )


class DiscoveryEngine:
    """
    Composes the pipeline over PORTS only, so every branch is testable offline.

    The engine never constructs an adapter. That is not ceremony: an engine that
    builds its own aiohttp session cannot be tested without a network, and an
    untestable loop is where the legacy system kept its 23 silent handlers.
    """

    def __init__(
        self,
        *,
        source_port,
        probe,
        store: _StoreLike,
        clock: ClockPort,
        target,
        policy: AdmissionPolicy | None = None,
        plan: ProbePlan | None = None,
        cooldown_base_s: float = 30.0,
        cooldown_cap_s: float = 3600.0,
        disable_after_failures: int = 12,
    ) -> None:
        self._sources = source_port
        self._probe = probe
        self._store = store
        self._clock = clock
        self._target = target
        self._policy = policy or AdmissionPolicy()
        self._plan = plan or ProbePlan()
        self._base_s = cooldown_base_s
        self._cap_s = cooldown_cap_s
        self._disable_after = disable_after_failures

    # ── one candidate: probe -> gate ─────────────────────────────────────────
    async def evaluate(self, proxy: Proxy) -> tuple[Proxy, Verdict]:
        """
        TCP triage -> protocol discovery -> k samples -> the gate.

        The staging is a cost decision (§7): k=5 sampling is ~5x the legacy
        single sample, so the cheap stages run first and only survivors pay.

        A failure at ANY stage returns the measured reason from that stage. It is
        never replaced by a later, vaguer one -- that is ADR-025, where a
        placeholder about an untested SOCKS rung overwrote every real HTTP
        measurement and drove gate-reachers to 0.
        """
        now = self._clock.now()

        tcp = await self._probe.tcp_handshake(proxy, self._plan.tcp_timeout_ms)
        if not tcp.ok:
            return (proxy.record_failure(now, reason=tcp.reason.value)
                    .with_state(ProxyState.COOLING, reason=tcp.reason.value)
                    .graded(Grade.REJECTED),
                    Verdict.reject(tcp.reason, tcp.detail))

        disco = await self._probe.discover_protocol(proxy, self._target)
        if not disco.ok:
            return (proxy.record_failure(now, reason=disco.reason.value)
                    .with_state(ProxyState.COOLING, reason=disco.reason.value)
                    .graded(Grade.REJECTED),
                    Verdict.reject(disco.reason, disco.detail))

        found = disco.discovered_protocol or proxy.protocol
        probed = replace(proxy, protocol=found)

        results: list[ProbeResult] = await self._probe.sample_latency(
            probed, self._target, self._plan)
        oks = [r for r in results if r.ok]
        samples = tuple(r.elapsed_ms for r in oks if r.elapsed_ms is not None)

        # `attempted` is len(results), NOT the plan's k: the probe stops early
        # after consecutive failures, and claiming k attempts when 2 were made
        # would understate success_ratio and hide UNRELIABLE.
        profile = build_profile(samples, attempted=len(results))
        anonymity = next(
            (r.observed_anonymity for r in oks if r.observed_anonymity), None
        ) or Anonymity.UNKNOWN

        graded = replace(probed, latency=profile, anonymity=anonymity)
        verdict = decide(
            profile, self._policy,
            anonymity=anonymity,
            protocol_mismatch=graded.protocol_mismatch,
        )

        if verdict.admitted:
            out = (graded.record_success(now)
                   .with_state(ProxyState.READY, reason=ReasonCode.OK.value)
                   .graded(verdict.grade))
        else:
            out = (graded.record_failure(now, reason=verdict.reason.value)
                   .with_state(ProxyState.COOLING, reason=verdict.reason.value)
                   .graded(Grade.REJECTED))
        return out, verdict

    # ── one source: fetch -> normalize -> evaluate -> store ──────────────────
    async def process_source(
        self, source: Source, budget: CycleBudget
    ) -> tuple[Source, SourceOutcome, tuple[Proxy, ...]]:
        now = self._clock.now()
        fetch = await self._sources.fetch(source)
        updated = apply_source_result(
            source, fetch, now=now, base_s=self._base_s,
            cap_s=self._cap_s, disable_after=self._disable_after)

        if not fetch.ok:
            return updated, SourceOutcome(
                source_id=source.id, ok=False, reason=fetch.reason,
                cooldown_until=updated.cooldown_until,
                detail=fetch.detail), ()

        # Candidates may arrive pre-parsed from the adapter, or as a raw body the
        # engine must parse with the source's DECLARED parser (never a guessed
        # one -- parse_body refuses to try alternatives, ADR-002).
        raws = fetch.candidates
        if not raws and fetch.detail and fetch.parser_used is None:
            raws = parse_body(source.parser.value, fetch.detail).candidates

        report = normalize_batch(raws[: budget.max_candidates_per_source])

        fresh: list[Proxy] = []
        known = 0
        for cand in report.accepted:
            candidate = Proxy(
                endpoint=cand.endpoint,
                labelled_protocol=cand.labelled_protocol,
                source_id=source.id,
                first_seen=now,
            )
            # Skip what the pool already holds: re-probing a known endpoint pays
            # full k=5 cost for a fact already recorded (responsibility 2).
            # ADR-040: dedup keys on the ENDPOINT, not the fingerprint. The
            # fingerprint includes the DISCOVERED protocol while a freshly
            # parsed candidate is protocol=UNKNOWN, so get(fingerprint) never
            # matched a stored probed row and every admitted proxy was re-probed
            # on every cycle (V4-03, caught by the level-6 E2E test).
            if self._store.get_by_endpoint(candidate.endpoint.host,
                                           candidate.endpoint.port):
                known += 1
                continue
            fresh.append(candidate)

        sem = asyncio.Semaphore(budget.probe_concurrency)

        async def guarded(p: Proxy) -> tuple[Proxy, Verdict]:
            async with sem:
                return await self.evaluate(p)

        evaluated = await asyncio.gather(*(guarded(p) for p in fresh))

        admitted = tuple(p for p, v in evaluated if v.admitted)
        elite = sum(1 for p, v in evaluated if v.admitted and v.grade is Grade.ELITE)
        votes: dict[str, int] = {}
        for p, v in evaluated:
            if v.admitted and p.protocol is not Protocol.UNKNOWN:
                votes[p.protocol.value] = votes.get(p.protocol.value, 0) + 1

        label_verdict, _ = classify_label(
            _protocol_of(source.labelled_protocol), votes)

        stats = updated.stats
        updated = updated.with_stats(replace(
            stats,
            admitted=stats.admitted + len(admitted),
            elite=stats.elite + elite,
        ))
        outcome = SourceOutcome(
            source_id=source.id, ok=True, reason=ReasonCode.OK,
            candidates_seen=report.seen,
            candidates_accepted=len(report.accepted),
            dropped_by_reason=report.dropped_by_reason,
            already_known=known,
            probed=len(evaluated),
            admitted=len(admitted),
            elite=elite,
            label_verdict=label_verdict,
            observed_protocols=votes,
            detail=fetch.detail,
        )
        return updated, outcome, tuple(p for p, _ in evaluated)

    # ── the cycle ────────────────────────────────────────────────────────────
    async def run_cycle(
        self, sources: tuple[Source, ...], budget: CycleBudget | None = None
    ) -> tuple[CycleReport, tuple[Source, ...]]:
        """
        Run one cycle over `sources`, returning the report AND the updated rows.

        The updated sources are RETURNED rather than persisted here: the engine
        does not own the registry file. Handing them back makes the ADR-026 label
        feedback and the ADR-006 cooldown observable to the caller and to tests,
        instead of being a side effect nothing can see -- which is how
        labels_verified stayed 0 through an entire live sweep.

        A source that is not fetchable (COOLING/DISABLED) is skipped without a
        fetch: honouring the cooldown is the entire point of having one.
        """
        budget = budget or CycleBudget()
        started = self._clock.now()

        outcomes: list[SourceOutcome] = []
        updated_rows: list[Source] = []
        probed = admitted = stored = skipped = 0
        rejected: dict[str, int] = {}
        remaining = budget.max_probes

        for source in sources[: budget.max_sources]:
            if not source.is_fetchable:
                updated_rows.append(source)
                continue
            if remaining <= 0:
                updated_rows.append(source)
                continue

            per_source = replace(
                budget,
                max_candidates_per_source=min(
                    budget.max_candidates_per_source, remaining),
            )
            new_source, outcome, evaluated = await self.process_source(
                source, per_source)

            updated_rows.append(new_source)
            outcomes.append(outcome)
            probed += outcome.probed
            admitted += outcome.admitted
            skipped += outcome.already_known
            remaining -= outcome.probed

            for p in evaluated:
                if p.grade is Grade.REJECTED and p.reason_code:
                    rejected[p.reason_code] = rejected.get(p.reason_code, 0) + 1
            if evaluated:
                stored += self._store.upsert_many(evaluated)

        return CycleReport(
            started_at=started,
            finished_at=self._clock.now(),
            outcomes=tuple(outcomes),
            probed=probed,
            admitted=admitted,
            stored=stored,
            skipped_known=skipped,
            rejected_by_reason=rejected,
        ), tuple(updated_rows)


__all__ = [
    "CycleBudget", "CycleReport", "SourceOutcome", "DiscoveryEngine",
    "apply_source_result", "classify_label",
]
