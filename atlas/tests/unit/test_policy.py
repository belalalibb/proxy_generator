"""
P04 — the admission gate and the normalizer. Entirely offline and deterministic.

These tests are the ones that matter most in the project, because the gate is the
whole thesis: LIVE != GOOD (H7). A bug here does not crash anything -- it quietly
admits slow proxies, which is exactly what the legacy system did for 15 000
candidates.

So the suite is built around three obligations:

  1. REAL DATA, NOT EXAMPLES. The gate is run against the actual 102 latencies the
     legacy system admitted (`proxy_details.json`), not against invented numbers.
  2. NEGATIVE CONTROLS (ADR-010/012). Every guard is shown to FAIL on input that
     should break it. A gate that admits everything passes any test that only
     feeds it good proxies.
  3. METHOD PARITY (ADR-011). v4's p95 is asserted to be computed by the same
     function as the baseline's, so the headline comparison is not two different
     statistics sharing a name.
"""
from __future__ import annotations

import ast
import json
import pathlib
import random

import pytest

from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade, ReasonCode
from atlas.core.policy.admission import (
    AdmissionPolicy, build_profile, decide, decide_for, grade_for,
)
from atlas.core.policy.normalize import (
    DropReason, NormalizeReport, normalize_batch, normalize_one, split_scheme,
    to_proxies,
)
from atlas.core.policy.percentile import (
    mean_ms, pct_floor, pct_linear, pct_tail, sample_stdev,
)

ROOT = pathlib.Path(__file__).resolve().parents[3]
LEGACY_DETAILS = ROOT / "proxy_details.json"

# Pinned in P00.T6 / BASELINE.json, re-derived by verify_baseline_streams.py.
LEGACY_N = 102
LEGACY_P50 = 6359.5
LEGACY_P95 = 15903.0
LEGACY_OVER_1500_PCT = 95.1     # n=102 stream. NOT 95.8 (that is n=118) -- ADR-020


def legacy_latencies() -> list[float]:
    doc = json.loads(LEGACY_DETAILS.read_text(encoding="utf-8"))
    return [float(r["response_time"]) for r in doc["working_proxies"]
            if r.get("working") and isinstance(r.get("response_time"), (int, float))]


# ══════════════════════════════════════════════════════════════════════════════
# METHOD PARITY — ADR-011 requires v4's p95 to use the baseline's function
# ══════════════════════════════════════════════════════════════════════════════
def test_percentile_methods_match_the_baseline_tool() -> None:
    """
    ADR-011 closes with: FINAL_AUDIT "must compute v4's own p95 with the same
    function for the comparison to be honest."

    If the gate used the ordinary interpolated p95 while the baseline used floor
    rank, every "v4 p95 vs legacy p95" claim would compare two different
    statistics. On the legacy data that gap is 424.6 ms -- more than a quarter of
    the entire 1500 ms budget. So parity is asserted, not assumed.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_baseline_tool", ROOT / "engineering" / "tools" / "measure_baseline.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    data = legacy_latencies()
    assert pct_floor(data, 95) == tool.pct_floor(data, 95)
    assert pct_linear(data, 50) == tool.pct_linear(data, 50)
    # and on adversarial small inputs, where methods diverge most
    for xs in ([1.0], [1.0, 2.0], [1.0, 2.0, 3.0], [5.0, 1.0, 3.0, 2.0]):
        assert pct_floor(xs, 95) == tool.pct_floor(xs, 95), xs
        assert pct_linear(xs, 50) == tool.pct_linear(xs, 50), xs


def test_the_two_percentile_methods_really_do_differ() -> None:
    """
    Negative control for the test above. If floor and interpolated agreed on this
    data, parity would be trivially true and would prove nothing.
    """
    data = legacy_latencies()
    assert pct_floor(data, 95) == LEGACY_P95
    assert pct_linear(data, 95) != pct_floor(data, 95), (
        "the methods coincide here, so test_percentile_methods_match_the_"
        "baseline_tool would pass vacuously"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADR-024 — a "95th percentile" that returns the MINIMUM
# ══════════════════════════════════════════════════════════════════════════════
def test_floor_rank_returns_the_minimum_at_k2_which_is_why_pct_tail_exists() -> None:
    """
    Documents the DEFECT in the frozen legacy estimator, so the reason pct_tail
    exists cannot be deleted as redundant.

    int((n-1)*0.95) == 0 for n == 2: the "95th percentile" of two samples is the
    FASTER one. Found via engineering/raw/admission_live_fixed.json, which
    recorded p95=4100.7 BELOW p50=5880.0 -- impossible for a real percentile.
    """
    assert pct_floor([900.0, 9000.0], 95) == 900.0, "the defect is gone; drop pct_tail"
    assert pct_tail([900.0, 9000.0], 95) == 9000.0


def test_the_tail_estimator_never_falls_below_the_median() -> None:
    """p95 < p50 is arithmetically impossible. The floor rank violates it at k=2."""
    rng = random.Random(20260824)
    saw_floor_violation = False
    for n in range(1, 9):
        for _ in range(400):
            xs = [rng.uniform(10.0, 20000.0) for _ in range(n)]
            assert pct_tail(xs, 95) >= pct_linear(xs, 50) - 1e-9, (n, xs)
            if pct_floor(xs, 95) < pct_linear(xs, 50) - 1e-9:
                saw_floor_violation = True
    assert saw_floor_violation, (
        "the frozen floor rank never violated the ordering in this sample, so "
        "this test proves nothing -- check the k=2 case is still generated"
    )


def test_pct_tail_preserves_legacy_parity_for_every_k_above_two() -> None:
    """
    The fix must not silently re-open ADR-011. Baseline comparability lives at
    n=102 and n=118, where the floor index is 95 and 111 -- nowhere near the k=2
    pathology -- so pct_tail must agree with pct_floor there exactly.
    """
    data = legacy_latencies()
    assert len(data) == LEGACY_N
    assert pct_tail(data, 95) == pct_floor(data, 95) == LEGACY_P95

    rng = random.Random(11)
    for n in list(range(3, 40)) + [102, 118]:
        xs = [rng.uniform(10.0, 20000.0) for _ in range(n)]
        assert pct_tail(xs, 95) == pct_floor(xs, 95), n


def test_a_proxy_measured_over_budget_at_k2_is_not_admitted() -> None:
    """
    THE REGRESSION THAT MATTERS. This is a false ADMIT, not a mis-stated number.

    Samples (1400ms, 1600ms) against a 1500ms ceiling: one request was measured
    OVER budget, but floor-rank p95 reported 1400 and the gate returned
    OK/USABLE. Jitter is 0.09, far below the 0.5 ceiling, so no other rule
    catches it -- the gate built to reject the legacy system's slow proxies would
    have admitted a proxy it had itself measured too slow (H7's failure mode,
    reintroduced through the estimator instead of the threshold).
    """
    policy = AdmissionPolicy()
    profile = build_profile((1400.0, 1600.0), attempted=2)

    assert profile.p95_ms == 1600.0, "p95 must reflect the slower observation"
    assert profile.jitter is not None and profile.jitter < policy.max_jitter, (
        "if jitter caught this, the p95 rule would not be the load-bearing one")

    verdict = decide(profile, policy)
    assert verdict.reason is ReasonCode.TOO_SLOW_P95
    assert verdict.grade is Grade.REJECTED


def test_a_genuinely_fast_k2_proxy_is_still_admitted() -> None:
    """
    Teeth in the other direction: pct_tail must not reject everything at k=2.
    Without this, returning +inf would pass the test above.
    """
    verdict = decide(build_profile((300.0, 420.0), attempted=2), AdmissionPolicy())
    assert verdict.reason is ReasonCode.OK
    assert verdict.grade is not Grade.REJECTED


def test_stdev_of_one_sample_is_none_not_zero() -> None:
    """
    A single sample has no measurable spread. Returning 0.0 would claim perfect
    stability from the least possible evidence -- the same flattering-default
    error as the legacy single-sample gate, one level down.
    """
    assert sample_stdev([5.0]) is None
    assert sample_stdev([]) is None
    assert sample_stdev([1.0, 2.0]) is not None
    assert mean_ms([]) is None


# ══════════════════════════════════════════════════════════════════════════════
# THE GATE vs THE REAL LEGACY DATA
# ══════════════════════════════════════════════════════════════════════════════
def test_gate_rejects_the_overwhelming_majority_of_legacy_admitted() -> None:
    """
    The central claim of the rebuild, checked against the legacy system's own
    output rather than an argument.

    k=1 here because the legacy file records ONE sample per proxy. That makes the
    replay GENEROUS: jitter and reliability are unmeasurable at n=1, so every
    rejection below is on latency alone.
    """
    policy = AdmissionPolicy()
    lat = legacy_latencies()
    assert len(lat) == LEGACY_N

    verdicts = [decide(build_profile((ms,), attempted=1), policy) for ms in lat]
    rejected = [v for v in verdicts if not v.admitted]

    assert len(rejected) == 97, f"expected 97 rejections, got {len(rejected)}"
    pct = round(100 * len(rejected) / len(lat), 1)
    assert pct == LEGACY_OVER_1500_PCT, (
        f"rejection rate {pct}% should equal the over-1500ms rate "
        f"{LEGACY_OVER_1500_PCT}% for this stream"
    )
    # every rejection names latency, not some incidental cause
    assert {v.reason for v in rejected} == {ReasonCode.TOO_SLOW_P95}


def test_the_worst_legacy_proxy_is_rejected_and_the_best_survives() -> None:
    """Both ends of the real distribution, so the gate is not one-sided."""
    policy = AdmissionPolicy()
    lat = legacy_latencies()

    worst = decide(build_profile((max(lat),), attempted=1), policy)
    assert not worst.admitted and worst.reason is ReasonCode.TOO_SLOW_P95
    assert max(lat) == 19035.0

    best = decide(build_profile((min(lat),), attempted=1), policy)
    assert best.admitted, f"the 756ms proxy must survive, got {best}"
    assert min(lat) == 756.0


def test_gate_would_have_admitted_only_five_of_the_legacy_hundred_and_two() -> None:
    policy = AdmissionPolicy()
    admitted = [ms for ms in legacy_latencies()
                if decide(build_profile((ms,), attempted=1), policy).admitted]
    assert len(admitted) == 5
    # and those survivors are genuinely fast, not marginal luck
    assert max(admitted) <= policy.max_p95_ms


# ══════════════════════════════════════════════════════════════════════════════
# RULE 2 — zero evidence is a REFUSAL. The inversion that matters most.
# ══════════════════════════════════════════════════════════════════════════════
def test_unmeasured_is_rejected_not_admitted() -> None:
    """
    H7. A gate whose failure mode is "let it through" is not a gate. The legacy
    default was to admit anything that answered once.
    """
    v = decide(build_profile((), attempted=5), AdmissionPolicy())
    assert not v.admitted
    assert v.reason is ReasonCode.NOT_MEASURED
    assert v.grade is Grade.REJECTED


def test_empty_profile_does_not_read_as_infinitely_fast() -> None:
    """
    Negative control for the above. If build_profile defaulted p95 to 0.0 instead
    of None, an unmeasured proxy would compare as faster than everything and be
    admitted ELITE. This asserts the absence is preserved as absence.
    """
    prof = build_profile((), attempted=3)
    assert prof.p95_ms is None
    assert prof.measured is False
    assert prof.success_ratio == 0.0


def test_attempted_cannot_be_smaller_than_successes() -> None:
    """The denominator must not silently shrink; that would fake reliability."""
    with pytest.raises(ValueError, match="denominator"):
        build_profile((100.0, 200.0, 300.0), attempted=2)


# ══════════════════════════════════════════════════════════════════════════════
# RULES 3 & 4 — reliability and jitter are SEPARATE facts from speed
# ══════════════════════════════════════════════════════════════════════════════
def test_fast_but_unreliable_is_rejected() -> None:
    """
    Two successes out of five, both quick. Speed alone would admit it. The legacy
    pool literally could not represent this case: one number per proxy.
    """
    prof = build_profile((120.0, 140.0), attempted=5)
    assert prof.success_ratio == pytest.approx(0.4)
    v = decide(prof, AdmissionPolicy())
    assert not v.admitted
    assert v.reason is ReasonCode.UNRELIABLE


def test_fast_but_erratic_is_rejected_for_jitter() -> None:
    prof = build_profile((80.0, 90.0, 100.0, 110.0, 1400.0), attempted=5)
    v = decide(prof, AdmissionPolicy())
    assert not v.admitted
    assert v.reason is ReasonCode.TOO_JITTERY, v


def test_reliability_is_checked_before_speed() -> None:
    """
    Order is part of the contract: a proxy that is BOTH unreliable and slow must
    report UNRELIABLE, because that is the more fundamental fact. A reason code
    that depends on evaluation order is not diagnostic (B-02).
    """
    prof = build_profile((9000.0, 9500.0), attempted=5)
    v = decide(prof, AdmissionPolicy())
    assert v.reason is ReasonCode.UNRELIABLE, (
        f"expected the reliability failure to be named first, got {v.reason}"
    )


def test_unmeasured_is_checked_before_everything_else() -> None:
    v = decide(build_profile((), attempted=5), AdmissionPolicy())
    assert v.reason is ReasonCode.NOT_MEASURED


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRITY OUTRANKS SPEED
# ══════════════════════════════════════════════════════════════════════════════
def test_a_fast_transparent_proxy_is_still_rejected() -> None:
    """
    A proxy that forwards the client IP has failed at the only job that matters,
    so latency is irrelevant. If this check ran after the speed check, a 200 ms
    leaking proxy would be graded ELITE.
    """
    prof = build_profile((200.0,) * 5, attempted=5)
    v = decide(prof, AdmissionPolicy(), transparent_leak=True)
    assert not v.admitted and v.reason is ReasonCode.TRANSPARENT_LEAK


def test_a_fast_intercepting_proxy_is_still_rejected() -> None:
    prof = build_profile((150.0,) * 5, attempted=5)
    v = decide(prof, AdmissionPolicy(), content_mismatch=True)
    assert not v.admitted and v.reason is ReasonCode.CONTENT_MISMATCH


def test_protocol_mismatch_is_reported_but_does_not_reject() -> None:
    """
    ADR-005: a mislabelled SOURCE is not a bad PROXY. TheSpeedX/SOCKS-List's
    http.txt held 2 853 candidates the legacy code discarded on the label alone
    (B-12). The mismatch belongs in `detail`, for the registry to absorb.
    """
    prof = build_profile((300.0,) * 5, attempted=5)
    v = decide(prof, AdmissionPolicy(), protocol_mismatch=True)
    assert v.admitted
    assert "label mismatch" in (v.detail or "")


# ══════════════════════════════════════════════════════════════════════════════
# GRADING
# ══════════════════════════════════════════════════════════════════════════════
def test_grades_follow_the_thresholds() -> None:
    p = AdmissionPolicy()
    assert grade_for(400.0, p, Anonymity.ELITE) is Grade.ELITE
    assert grade_for(900.0, p, Anonymity.ELITE) is Grade.GOOD
    assert grade_for(1400.0, p, Anonymity.ELITE) is Grade.USABLE
    assert grade_for(1600.0, p, Anonymity.ELITE) is Grade.REJECTED


def test_elite_requires_proven_anonymity() -> None:
    """
    Unmeasured anonymity is not good anonymity. A fast proxy of unknown
    transparency is GOOD, never ELITE -- the same 'absence is not permission'
    rule as NOT_MEASURED.
    """
    p = AdmissionPolicy()
    assert grade_for(200.0, p, Anonymity.UNKNOWN) is Grade.GOOD
    assert grade_for(200.0, p, Anonymity.TRANSPARENT) is Grade.GOOD
    assert grade_for(200.0, p, Anonymity.ELITE) is Grade.ELITE
    assert grade_for(200.0, AdmissionPolicy(require_anonymity_for_elite=False),
                     Anonymity.UNKNOWN) is Grade.ELITE


def test_incoherent_policy_is_rejected_at_construction() -> None:
    """A policy that can never admit anything is a config bug, not a strict gate."""
    with pytest.raises(ValueError, match="non-decreasing"):
        AdmissionPolicy(elite_p95_ms=900, good_p95_ms=500)
    with pytest.raises(ValueError, match="every 'usable' proxy would be rejected"):
        AdmissionPolicy(max_p95_ms=800, usable_p95_ms=1500)
    with pytest.raises(ValueError, match="samples_k"):
        AdmissionPolicy(samples_k=0)
    with pytest.raises(ValueError, match="min_success_ratio"):
        AdmissionPolicy(min_success_ratio=0.0)


def test_decide_for_reads_the_proxy() -> None:
    prox = Proxy(
        endpoint=Endpoint.parse("1.2.3.4:8080"),
        protocol=Protocol.SOCKS5,
        labelled_protocol=Protocol.HTTP,      # the source lied
        anonymity=Anonymity.ELITE,
        latency=build_profile((250.0,) * 5, attempted=5),
    )
    v = decide_for(prox, AdmissionPolicy())
    assert v.admitted and v.grade is Grade.ELITE
    assert "label mismatch" in (v.detail or "")


# ══════════════════════════════════════════════════════════════════════════════
# NORMALIZER — ADR-019, the scheme that was captured and thrown away
# ══════════════════════════════════════════════════════════════════════════════
def test_endpoint_parse_discards_the_scheme_it_captures() -> None:
    """
    Documents the ADR-019 defect precisely, so it cannot silently return.

    `_HOSTPORT` has a named group for the scheme and `Endpoint.parse` never reads
    it. Endpoint is a host:port value object, so dropping it there is defensible
    -- but only if something else CAPTURES it. Before P04, nothing did.
    """
    from atlas.core.domain.proxy import _HOSTPORT
    m = _HOSTPORT.match("socks5://1.2.3.4:1080")
    assert m is not None and m.group("scheme") == "socks5"
    assert Endpoint.parse("socks5://1.2.3.4:1080") == Endpoint.parse("1.2.3.4:1080")


def test_normalizer_preserves_the_scheme_as_a_protocol_label() -> None:
    """The fix: the declaration survives normalisation instead of evaporating."""
    cand, reason = normalize_one("socks5://1.2.3.4:1080")
    assert reason is None
    assert cand.labelled_protocol is Protocol.SOCKS5
    assert cand.scheme_seen == "socks5"
    assert str(cand.endpoint) == "1.2.3.4:1080"


def test_bare_socks_is_not_guessed_as_socks5() -> None:
    """
    `socks://` is genuinely ambiguous between v4 and v5. ADR-005 forbids guessing;
    UNKNOWN leaves it for S3 to discover.
    """
    cand, _ = normalize_one("socks://1.2.3.4:1080")
    assert cand.labelled_protocol is Protocol.UNKNOWN
    assert cand.scheme_seen == "socks"


def test_split_scheme_handles_the_shapes_sources_actually_emit() -> None:
    assert split_scheme("http://1.2.3.4:80") == ("http", "1.2.3.4:80")
    assert split_scheme("SOCKS5://1.2.3.4:80") == ("socks5", "1.2.3.4:80")
    assert split_scheme("1.2.3.4:80") == (None, "1.2.3.4:80")
    assert split_scheme("  1.2.3.4:80  ") == (None, "1.2.3.4:80")


def test_labelled_protocol_is_not_written_into_protocol() -> None:
    """
    ADR-005. If normalisation set `protocol` from the label, `protocol_mismatch`
    could never fire and the ability to detect a lying source would be destroyed.
    """
    report = normalize_batch(["socks5://1.2.3.4:1080"])
    prox = to_proxies(report, source_id="s1")[0]
    assert prox.labelled_protocol is Protocol.SOCKS5
    assert prox.protocol is Protocol.UNKNOWN
    assert prox.state is ProxyState.DISCOVERED
    assert prox.source_id == "s1"


# ── drops, each with a named reason ───────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.1:8080", DropReason.PRIVATE_RANGE),
    ("10.0.0.1:3128", DropReason.PRIVATE_RANGE),
    ("127.0.0.1:8080", DropReason.LOOPBACK),
    ("0.0.0.0:80", DropReason.UNSPECIFIED),
    ("224.0.0.1:80", DropReason.MULTICAST),
    ("user:pass@1.2.3.4:8080", DropReason.HAS_CREDENTIALS),
    ("1.2.3.4.5:80", DropReason.UNPARSEABLE),
    # 999.1.1.1 never reaches the ipaddress check: Endpoint.parse rejects any
    # host whose rightmost label is all digits (the P01.T3 fix). So the honest
    # reason is UNPARSEABLE. My first version of this table asserted NOT_AN_IP
    # and was simply wrong about the order of the checks -- corrected here rather
    # than "fixed" by loosening the assertion.
    ("999.1.1.1:80", DropReason.UNPARSEABLE),
    ("1.2.3.4:99999", DropReason.UNPARSEABLE),
    # NOT_AN_IP is reachable only via a genuine hostname, which parses fine as an
    # endpoint but cannot be probed without DNS -- and core/ may not do I/O.
    ("proxy.example.com:8080", DropReason.NOT_AN_IP),
    ("", DropReason.UNPARSEABLE),
    ("   ", DropReason.UNPARSEABLE),
    ("garbage", DropReason.UNPARSEABLE),
])
def test_every_drop_has_a_named_reason(raw: str, expected: str) -> None:
    """
    B-02: the legacy code's silent `continue` is why 35 dead URLs were retried
    forever. Nothing is dropped anonymously here.
    """
    cand, reason = normalize_one(raw)
    assert cand is None, f"{raw!r} should not have been accepted"
    assert reason == expected, f"{raw!r}: expected {expected}, got {reason}"


def test_loopback_is_reported_as_loopback_not_private() -> None:
    """
    127.0.0.1 is private too. The reason must be the most SPECIFIC true statement,
    or the counter stops being diagnostic.
    """
    _, reason = normalize_one("127.0.0.1:8080")
    assert reason == DropReason.LOOPBACK


def test_credentials_are_refused_not_stripped() -> None:
    """
    Stripping would mean forwarding someone else's leaked secret to a third
    party. Refusal is the only responsible option (H5).
    """
    _, reason = normalize_one("bob:hunter2@1.2.3.4:8080")
    assert reason == DropReason.HAS_CREDENTIALS


# ── accounting ───────────────────────────────────────────────────────────────
def test_normalisation_accounts_for_every_input() -> None:
    """
    accepted + dropped == seen, enforced by NormalizeReport itself. "We ingested
    500" and "we ingested 500 and silently discarded 900" are different facts.
    """
    raws = ["1.2.3.4:8080", "192.168.0.1:80", "garbage", "5.6.7.8:3128"]
    r = normalize_batch(raws)
    assert r.seen == 4
    assert len(r.accepted) + len(r.dropped) == 4
    assert r.dropped_by_reason == {DropReason.PRIVATE_RANGE: 1,
                                   DropReason.UNPARSEABLE: 1}
    assert r.accept_rate == pytest.approx(0.5)


def test_report_refuses_to_lose_candidates() -> None:
    """Negative control for the accounting invariant."""
    with pytest.raises(ValueError, match="lost candidates"):
        NormalizeReport(accepted=(), dropped=(), seen=7)


def test_dedup_keeps_first_occurrence_and_is_order_stable() -> None:
    raws = ["1.2.3.4:8080", "5.6.7.8:80", "1.2.3.4:8080", "9.9.9.9:80"]
    r = normalize_batch(raws)
    assert [c.key for c in r.accepted] == ["1.2.3.4:8080", "5.6.7.8:80", "9.9.9.9:80"]
    assert r.dropped_by_reason == {DropReason.DUPLICATE: 1}
    assert normalize_batch(raws).accepted == r.accepted     # reproducible


def test_dedup_is_protocol_independent() -> None:
    """
    The same endpoint under two schemes is one machine and a protocol CLAIM, not
    two proxies. Probing it twice on a guess wastes budget; S3 settles it.
    """
    r = normalize_batch(["socks5://1.2.3.4:1080", "http://1.2.3.4:1080"])
    assert len(r.accepted) == 1
    assert r.dropped_by_reason == {DropReason.DUPLICATE: 1}


def test_dedup_can_be_disabled_without_losing_accounting() -> None:
    r = normalize_batch(["1.2.3.4:8080"] * 3, dedup=False)
    assert len(r.accepted) == 3 and not r.dropped


# ── against the real seed file ────────────────────────────────────────────────
def test_normalizer_accepts_the_real_seed_list_intact() -> None:
    """
    proxy.txt is the legacy system's own 616-line output. Measured directly: all
    616 lines are syntactically valid public IPv4 host:port pairs with no
    duplicates, so a correct normalizer must accept exactly 616.

    This is a REGRESSION FLOOR, not a compliment to the data. If a future change
    starts silently discarding real candidates, this fails.
    """
    seed = ROOT / "proxy.txt"
    raws = [l.strip() for l in seed.read_text().splitlines() if l.strip()]
    assert len(raws) == 616
    r = normalize_batch(raws)
    assert r.seen == 616
    assert len(r.accepted) == 616, (
        f"dropped {len(r.dropped)}: {r.dropped_by_reason} -- "
        "these were all valid public endpoints when measured in P04"
    )
    # and none of them carries a protocol claim, which is itself the finding:
    # the legacy export lost the scheme, so 616 candidates arrive label-free.
    assert all(c.labelled_protocol is Protocol.UNKNOWN for c in r.accepted)


def test_seed_candidates_are_unverified_not_ready() -> None:
    """
    RESUME_PROMPT: the 616 seeds are to be treated as UNVERIFIED. They enter as
    DISCOVERED and the gate refuses them until measured.
    """
    r = normalize_batch(["46.8.236.122:9999"])
    prox = to_proxies(r)[0]
    assert prox.state is ProxyState.DISCOVERED
    assert not prox.is_leasable
    v = decide_for(prox, AdmissionPolicy())
    assert not v.admitted and v.reason is ReasonCode.NOT_MEASURED


# ══════════════════════════════════════════════════════════════════════════════
# SELF-CHECKS
# ══════════════════════════════════════════════════════════════════════════════
def test_this_suite_is_offline() -> None:
    """AST-based, not substring matching (which would match its own banned list)."""
    tree = ast.parse(pathlib.Path(__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("aiohttp", "requests", "urllib", "httpx", "socket"):
        assert banned not in imported


def test_the_gate_module_never_uses_min_as_a_statistic() -> None:
    """
    §8 calls `min` cosmetic: it reports the best moment a proxy ever had, not the
    experience a consumer gets. Excluded STRUCTURALLY rather than by review --
    admission.py must contain no call to min() at all.
    """
    src = (ROOT / "atlas" / "core" / "policy" / "admission.py").read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "min"]
    assert not calls, (
        f"admission.py calls min() {len(calls)}x; §8 forbids deciding on min"
    )


# ── ADR-028: the SSRF range check was an enumeration with no backstop ─────────
CGNAT_CASES = [
    "100.64.0.0", "100.64.1.1", "100.100.100.100", "100.127.255.255",
]


@pytest.mark.parametrize("addr", CGNAT_CASES)
def test_cgnat_is_refused(addr: str) -> None:
    """
    ADR-028. SECURITY.md P5 claimed CGNAT 100.64/10 was rejected "via
    ipaddress"; it was ACCEPTED, because `ipaddress` reports is_private=False
    AND is_global=False for RFC 6598 space, so it satisfied none of the
    specific checks.

    This asserts the named reason, not merely "dropped": a generic refusal
    would not tell an operator that the range the security policy calls out by
    name is the one that matched.
    """
    cand, reason = normalize_one(f"{addr}:8080")
    assert cand is None, f"{addr} is CGNAT (RFC 6598) and must not be accepted"
    assert reason == DropReason.CGNAT_RANGE


def test_cgnat_boundaries_are_not_over_refused() -> None:
    """
    The addresses either side of 100.64.0.0/10 are ordinary public space.

    Without this, `test_cgnat_is_refused` would still pass if the check refused
    all of 100.0.0.0/8 -- or everything. A deny rule needs its false-positive
    direction pinned, which is the ADR-027 lesson (assert the work, not just
    the ceiling).
    """
    for ok in ("100.63.255.255", "100.128.0.0"):
        cand, reason = normalize_one(f"{ok}:8080")
        assert cand is not None, f"{ok} is outside 100.64/10 and must be accepted, got {reason}"


def test_metadata_endpoint_is_reported_as_link_local() -> None:
    """
    169.254.169.254 (cloud metadata, SECURITY.md P5) is link-local AND private.
    LINK_LOCAL is the specific true statement, so that is what must be reported.
    """
    cand, reason = normalize_one("169.254.169.254:80")
    assert cand is None
    assert reason == DropReason.LINK_LOCAL


def test_no_non_global_address_is_ever_accepted() -> None:
    """
    THE CLASS-LEVEL GUARD, and the actual point of ADR-028.

    The defect was not "one range was missing" -- it was that the check
    enumerated ranges with no backstop, so the next reserved-but-unflagged
    allocation would walk through too. This enumerates every special-purpose
    range `ipaddress` can recognise and asserts each is refused, so a future
    gap fails a test instead of shipping.
    """
    import ipaddress
    specials = [
        "0.0.0.0", "10.0.0.1", "100.64.1.1", "127.0.0.1", "169.254.1.1",
        "172.16.0.1", "192.0.0.1", "192.0.2.1", "192.168.1.1", "198.18.0.1",
        "198.51.100.1", "203.0.113.1", "224.0.0.1", "239.255.255.250",
        "240.0.0.1", "255.255.255.255",
    ]
    accepted = []
    for addr in specials:
        cand, _ = normalize_one(f"{addr}:8080")
        if cand is not None:
            accepted.append(addr)
    assert not accepted, (
        f"non-routable addresses were ACCEPTED as proxy candidates: {accepted}. "
        "SECURITY.md P5 promises these are refused."
    )
    # Vacuity guard: the loop must actually have exercised the address that
    # motivated the ADR. Without this the test would still pass if `specials`
    # were emptied by a bad merge.
    assert "100.64.1.1" in specials and len(specials) >= 16


def test_is_global_alone_would_not_be_a_correct_check() -> None:
    """
    Proves the DESIGN claim in ADR-028 rather than asserting it in prose: neither
    `is_global` nor the specific properties subsume the other, so both are needed.

    If someone "simplifies" normalize_one down to `if not ip.is_global`, multicast
    starts being admitted -- and this test records why that refactor is wrong,
    measured on the interpreter rather than argued.
    """
    import ipaddress
    # multicast that is_global calls global -> is_global alone would ADMIT these
    for mcast in ("224.0.0.1", "239.255.255.250"):
        assert ipaddress.ip_address(mcast).is_global is True
        assert ipaddress.ip_address(mcast).is_multicast is True
    # CGNAT that no specific property flags -> specific checks alone MISS it
    cgnat = ipaddress.ip_address("100.64.1.1")
    assert cgnat.is_global is False
    assert not (cgnat.is_private or cgnat.is_loopback or cgnat.is_multicast
                or cgnat.is_reserved or cgnat.is_link_local
                or cgnat.is_unspecified)
    # and normalize_one refuses BOTH kinds
    for addr in ("224.0.0.1", "239.255.255.250", "100.64.1.1"):
        cand, _ = normalize_one(f"{addr}:8080")
        assert cand is None, f"{addr} must be refused"
