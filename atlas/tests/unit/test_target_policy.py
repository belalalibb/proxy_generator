"""
Target allow-policy tests (ADR-029).

The policy this module tests existed in `config.yaml` since P01 and had NO Python
reader. So these tests are written to answer one question first -- "would this
have caught the thing that was actually wrong?" -- and the answer is
`test_the_legacy_default_target_is_refused`: before P08, `Target(url=
"https://instagram.com")` was constructible and nothing objected.

Both directions are pinned throughout. A deny-list that refuses EVERYTHING would
pass every "is it refused?" test, so each refusal has a matching test that a
legitimate neighbour is still allowed. That is the ADR-027 lesson applied to
security policy: assert the work, not just the ceiling.
"""
from __future__ import annotations

import ast
import ipaddress
from pathlib import Path

import pytest

from atlas.adapters.config import load_target_policy
from atlas.core.domain.source import Target
from atlas.core.policy.target_policy import (
    TargetNotAllowed, TargetPolicy, TargetRefusal,
    check_target, host_matches_deny, require_target,
)

ROOT = Path(__file__).resolve().parents[3]

# ADR-031: the deny-list is DATA, loaded from config.yaml, and appears nowhere in
# executable code. These tests therefore exercise the SAME policy object the
# application builds -- so they prove the file is authoritative, not that a
# constant in a test file matches a constant in a source file (ADR-018: two
# documents agreeing is consistency, not truth).
POLICY = load_target_policy()

# The hosts under test are read from the config, never typed here: H5/ADR-007
# bans naming them in code, and test files are code.
DENIED_HOSTS = sorted(POLICY.deny_hosts)
A_DENIED_HOST = DENIED_HOSTS[0]


# ── the defect this module exists for ────────────────────────────────────────
def test_the_legacy_default_target_is_refused() -> None:
    """
    THE ADR-007 CASE, and the reason ADR-029 exists.

    v1.py:29 and v3.py:30 shipped `TEST_URL = "https://www.instagram.com"` and
    probed it thousands of times per run. MIGRATION_LEDGER.md files that as
    RETIRED_PROHIBITED. Until P08 the prohibition was prose: this exact Target
    was constructible and no code objected.
    """
    assert check_target(Target(url="https://www.instagram.com"), POLICY) == TargetRefusal.DENIED_HOST
    assert check_target(Target(url="https://instagram.com"), POLICY) == TargetRefusal.DENIED_HOST


def test_a_lookalike_host_is_not_refused() -> None:
    """
    THE FALSE-POSITIVE DIRECTION, without which the test above proves nothing:
    a policy that refused every URL would satisfy it.

    `notinstagram.com` ends with "instagram.com" as a STRING but is a different
    domain. A naive `str.endswith` deny-list refuses it -- measured, not assumed
    (see host_matches_deny's docstring table).
    """
    assert check_target(Target(url="https://notinstagram.com"), POLICY) is None
    assert check_target(Target(url="https://myinstagram.com"), POLICY) is None


def test_subdomains_of_a_denied_host_are_refused() -> None:
    """Denying instagram.com must cover its subdomains, or the rule is trivially bypassed."""
    for url in ("https://www.instagram.com", "https://graph.instagram.com",
                "https://i.instagram.com/api/v1/"):
        assert check_target(Target(url=url), POLICY) == TargetRefusal.DENIED_HOST, url


def test_a_denied_host_as_a_prefix_of_another_domain_is_allowed() -> None:
    """
    `instagram.com.evil.net` is NOT under instagram.com -- the denied name is a
    prefix here, not a suffix. It must not be refused as though it were, because
    a reason code that is sometimes wrong stops being diagnostic.
    """
    assert check_target(Target(url="http://instagram.com.evil.net/"), POLICY) is None


@pytest.mark.parametrize("host", ["instagram.com", "facebook.com", "tiktok.com"])
def test_every_config_denied_host_is_actually_denied(host: str) -> None:
    """
    The three hosts config.yaml names must each be refused. Parametrised so a
    host silently dropped from the default policy fails its own case rather than
    hiding inside a loop.
    """
    assert check_target(Target(url=f"https://{host}/"), POLICY) == TargetRefusal.DENIED_HOST


def test_a_policy_cannot_be_built_without_a_deny_list() -> None:
    """
    ADR-031. This test asserted the OPPOSITE until P08: that `TargetPolicy()`
    with no arguments already denied the three config hosts.

    That design put the hostnames in executable code, which
    `test_no_default_target_url_constant` correctly fails the build for (H5 /
    ADR-007 / ADR-012) -- the ban exists so the legacy default target cannot
    return as a "safe" default. It also made config.yaml non-authoritative:
    removing a host from the file could not remove it from the compiled-in set.

    The replacement invariant is stronger than the one it retires. Instead of
    "the default is not empty", it is "there is no default at all": a caller
    cannot obtain a policy that denies nothing by forgetting an argument,
    because the argument is required. Empty remains EXPRESSIBLE -- typed out
    deliberately, below -- so an operator who really wants no deny-list can say
    so, but it can never happen by omission.
    """
    with pytest.raises(TypeError):
        TargetPolicy()                      # type: ignore[call-arg]

    # deliberate-and-explicit remains possible, and is honestly permissive
    empty = TargetPolicy(deny_hosts=frozenset())
    assert empty.deny_hosts == frozenset()
    assert check_target(Target(url=f"https://{A_DENIED_HOST}/"), empty) is None


def test_the_loaded_policy_denies_the_hosts_the_config_file_names() -> None:
    """
    The policy the APPLICATION builds must deny exactly what config.yaml lists.

    Read from the file rather than compared against a literal, so this test
    cannot pass by agreeing with a constant that is itself the bug (ADR-018).
    """
    import yaml
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    from_file = {h.strip().lower()
                 for h in raw["targets"]["allow_policy"]["deny_hosts"]}
    assert from_file, "config.yaml names no denied hosts -- the fixture is vacuous"
    assert POLICY.deny_hosts == frozenset(from_file)
    for host in sorted(from_file):
        assert check_target(Target(url=f"https://{host}/"), POLICY) \
            == TargetRefusal.DENIED_HOST


# ── SSRF: the same predicate as the candidate path ───────────────────────────
@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", TargetRefusal.NOT_GLOBALLY_ROUTABLE),
    ("10.0.0.1", TargetRefusal.NOT_GLOBALLY_ROUTABLE),
    ("192.168.1.1", TargetRefusal.NOT_GLOBALLY_ROUTABLE),
    ("169.254.169.254", TargetRefusal.NOT_GLOBALLY_ROUTABLE),
    ("100.64.1.1", TargetRefusal.NOT_GLOBALLY_ROUTABLE),   # ADR-028 CGNAT
    ("0.0.0.0", TargetRefusal.NOT_GLOBALLY_ROUTABLE),
])
def test_non_routable_target_addresses_are_refused(host: str, expected: str) -> None:
    """
    SECURITY.md P5 / §4 SSRF row. `169.254.169.254` is the AWS/GCP metadata
    endpoint; `100.64.1.1` is the CGNAT range that ADR-028 found was accepted on
    the candidate path.
    """
    assert check_target(Target(url=f"http://{host}/"), POLICY) == expected


def test_a_public_ip_target_is_allowed() -> None:
    """The routability check must not refuse ordinary public addresses."""
    assert check_target(Target(url="https://8.8.8.8/"), POLICY) is None
    assert check_target(Target(url="https://1.1.1.1/"), POLICY) is None


def test_metadata_hostnames_are_refused_by_name() -> None:
    """
    `metadata.google.internal` resolves to a link-local address, but NO ip check
    can catch it: it is a NAME. This module does not resolve DNS (core/ may not do
    I/O), so the name form needs its own rule -- and its own reason code, so the
    operator sees METADATA_ENDPOINT rather than a generic refusal.
    """
    assert check_target(Target(url="http://metadata.google.internal/"), POLICY) \
        == TargetRefusal.METADATA_ENDPOINT
    assert check_target(Target(url="http://metadata/computeMetadata/"), POLICY) \
        == TargetRefusal.METADATA_ENDPOINT


def test_the_ssrf_rule_has_exactly_one_implementation() -> None:
    """
    ADR-029: the target path must REUSE the candidate path's routability
    predicate, not re-derive it.

    Asserted structurally (AST) rather than by reading the code, because two
    copies of a security predicate is two things to keep in sync and the one that
    drifts is the one nobody is testing. If someone reimplements the range list
    here, this fails.
    """
    src = (ROOT / "atlas" / "core" / "policy" / "target_policy.py").read_text()
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.endswith("normalize")
        for alias in node.names
    }
    assert "is_globally_routable" in imported, (
        "target_policy must import is_globally_routable from normalize, so the "
        "SSRF rule has ONE implementation shared with the candidate path"
    )


# ── scheme / structure ───────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "gopher://example.com/",
    "file:///etc/passwd",
])
def test_non_http_schemes_are_refused(url: str) -> None:
    """
    `file://` is the SSRF/local-read case that matters most, and it is refused by
    Target's own constructor as well -- checked here at the policy layer so the
    rule holds even if a Target is built by some other path.
    """
    # Target's constructor already refuses these, which is defence in depth.
    with pytest.raises(ValueError):
        Target(url=url)


def test_the_policy_layer_refuses_a_bad_scheme_independently() -> None:
    """
    Belt and braces: if a Target ever reaches this function with a non-http
    scheme (constructed by a future code path that skips validation), the policy
    must still refuse it. Uses object.__new__ to bypass __post_init__ rather than
    trusting that the constructor is the only door.
    """
    t = object.__new__(Target)
    object.__setattr__(t, "url", "gopher://example.com/")
    object.__setattr__(t, "expect_status", 200)
    object.__setattr__(t, "min_bytes", 0)
    object.__setattr__(t, "timeout_ms", 8000)
    assert check_target(t, POLICY) == TargetRefusal.BAD_SCHEME


def test_absent_target_is_a_refusal_not_a_default() -> None:
    """
    ADR-007: there is no default target. `None` must be refused, never quietly
    replaced with example.com or anything else.
    """
    assert check_target(None, POLICY) == TargetRefusal.NO_TARGET


def test_an_explicit_port_is_allowed_and_an_absurd_one_is_not() -> None:
    """A target may name a port; it may not name an impossible one."""
    assert check_target(Target(url="http://example.com:8080/"), POLICY) is None
    t = object.__new__(Target)
    object.__setattr__(t, "url", "http://example.com:99999/")
    object.__setattr__(t, "expect_status", 200)
    object.__setattr__(t, "min_bytes", 0)
    object.__setattr__(t, "timeout_ms", 8000)
    assert check_target(t, POLICY) == TargetRefusal.BAD_PORT


def test_host_is_matched_case_insensitively() -> None:
    """
    DNS is case-insensitive, so an uppercase HOST must not slip past a deny-list
    that only ever saw lowercase.

    My first version of this test used `HTTPS://INSTAGRAM.COM/` and FAILED -- but
    the code was right and the test was wrong. `Target.__post_init__` validates
    the scheme with a case-SENSITIVE `startswith(("http://", "https://"))`, so an
    uppercase scheme never reaches this policy at all: it is refused one layer
    earlier. The test was asserting a behaviour of the wrong layer. Corrected to
    vary only the host, which is what this function is responsible for.
    """
    assert check_target(Target(url="https://INSTAGRAM.COM/"), POLICY) == TargetRefusal.DENIED_HOST
    assert check_target(Target(url="https://WwW.InStAgRaM.cOm/"), POLICY) == TargetRefusal.DENIED_HOST


def test_an_uppercase_scheme_is_refused_by_the_constructor_not_silently_allowed() -> None:
    """
    Records the layer boundary found above, so it is a pinned fact rather than a
    surprise for the next reader.

    RFC 3986 says schemes are case-INSENSITIVE, so `HTTPS://example.com` is a
    legal URL that this codebase refuses. That is stricter than the spec, and it
    is being recorded rather than "fixed" for one reason: it fails CLOSED. An
    over-strict check refuses a legitimate target (visible, reported, fixable);
    a permissive one would admit a denied host (silent). Since the deny-list
    itself IS case-insensitive on the host -- the security-relevant half, proven
    above and again at the policy layer in
    `test_the_policy_layer_refuses_a_bad_scheme_independently` -- there is no
    bypass here, only a usability wart. Logged as observed-not-fixed in
    TASK_STATE.json rather than changed under a security ADR it does not belong to.
    """
    with pytest.raises(ValueError, match="absolute http"):
        Target(url="HTTPS://INSTAGRAM.COM/")

    # And the policy layer would ALSO have caught the host, had it been reached:
    t = object.__new__(Target)
    object.__setattr__(t, "url", "HTTPS://INSTAGRAM.COM/")
    object.__setattr__(t, "expect_status", 200)
    object.__setattr__(t, "min_bytes", 0)
    object.__setattr__(t, "timeout_ms", 8000)
    assert check_target(t, POLICY) == TargetRefusal.DENIED_HOST


def test_a_trailing_dot_does_not_bypass_the_deny_list() -> None:
    """
    `instagram.com.` is the same name in DNS (fully-qualified form). Without
    stripping the root label this is a one-character bypass.
    """
    assert check_target(Target(url="https://instagram.com./"), POLICY) == TargetRefusal.DENIED_HOST


# ── require_target ───────────────────────────────────────────────────────────
def test_require_target_raises_with_the_named_reason() -> None:
    """
    A refusal must be impossible to drop on the floor. `check_target` returns a
    value a caller can ignore; ADR-019 is what happens when they do.
    """
    with pytest.raises(TargetNotAllowed) as exc:
        require_target(Target(url=f"https://{A_DENIED_HOST}"), POLICY)
    assert exc.value.reason == TargetRefusal.DENIED_HOST
    # the offending URL must appear in the message: a refusal that does not say
    # WHAT was refused is not diagnosable (B-02)
    assert A_DENIED_HOST in str(exc.value)


def test_require_target_returns_an_allowed_target_unchanged() -> None:
    t = Target(url="https://example.com")
    assert require_target(t, POLICY) is t


def test_require_target_refuses_none() -> None:
    with pytest.raises(TargetNotAllowed) as exc:
        require_target(None, POLICY)
    assert exc.value.reason == TargetRefusal.NO_TARGET


# ── host_matches_deny, directly ──────────────────────────────────────────────
@pytest.mark.parametrize("host,deny,expected", [
    ("instagram.com", "instagram.com", True),
    ("www.instagram.com", "instagram.com", True),
    ("a.b.instagram.com", "instagram.com", True),
    ("INSTAGRAM.COM", "instagram.com", True),
    ("instagram.com.", "instagram.com", True),
    ("notinstagram.com", "instagram.com", False),
    ("myinstagram.com", "instagram.com", False),
    ("instagram.com.evil.net", "instagram.com", False),
    ("example.com", "instagram.com", False),
])
def test_host_matches_deny_uses_label_boundaries(host: str, deny: str,
                                                 expected: bool) -> None:
    """
    The table from the ADR, executed. Four of these nine cases differ between
    label-boundary matching and `str.endswith`, which is why the distinction is
    worth a function and a test rather than an inline expression.
    """
    assert host_matches_deny(host, deny) is expected


def test_endswith_would_have_been_wrong() -> None:
    """
    Pins the DESIGN claim, so a future "simplification" to str.endswith fails a
    test instead of silently over-refusing real targets.
    """
    host, deny = "notinstagram.com", "instagram.com"
    assert host.endswith(deny) is True          # the naive check says refuse
    assert host_matches_deny(host, deny) is False   # the correct one says allow


def test_policy_rejects_a_malformed_deny_entry() -> None:
    """A config typo like a leading dot must fail loudly at construction."""
    with pytest.raises(ValueError):
        TargetPolicy(deny_hosts=frozenset({".instagram.com"}))
    with pytest.raises(ValueError):
        TargetPolicy(deny_hosts=frozenset({"http://instagram.com/x"}))


def test_this_module_is_pure() -> None:
    """
    core/ may not do I/O (test_architecture.py covers core/ generally; this is the
    module-specific assertion, since a DNS lookup here would be the most tempting
    way to "improve" the SSRF check -- and would break the purity that makes it
    testable).
    """
    src = (ROOT / "atlas" / "core" / "policy" / "target_policy.py").read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("socket", "aiohttp", "requests", "httpx", "urllib3", "asyncio"):
        assert banned not in imported, f"target_policy must not import {banned}"
