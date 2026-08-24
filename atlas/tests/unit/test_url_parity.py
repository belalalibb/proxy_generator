"""
DIFFERENTIAL TEST — the pure splitter vs CPython's urlsplit (ADR-030).

WHY THIS FILE MAY IMPORT urllib WHEN core/ MAY NOT

`test_architecture.py` bans `urllib` inside `atlas/core/`, and `_all_atlas_py()`
excludes `tests/` from that scan. That asymmetry is the point, not a loophole:
core/ must never acquire an import that can open a socket, but the ORACLE that
proves core/'s replacement is correct has to be the real thing. Here urllib is
the thing under comparison, never the thing shipped.

WHAT THIS CAUGHT

Four separate defects in my own splitter, each found by disagreement with the
oracle rather than by reading the regex:

  1. DENY-LIST BYPASS. Userinfo matched non-greedily (`[^/?#@]*@`), so
     `https://x@evil.com@<denied>/` produced host 'evil.com@<denied>' -- no
     deny match -- while every real client dials the denied host. Fixed by
     matching userinfo up to the LAST '@'.
  2. Stray brackets: `http://9001]9:0_@-a` parsed to host '-a'; CPython refuses.
  3. Bracketed junk: `http://[#12]` parsed to host '#12'; CPython refuses.
  4. Non-IPv6 brackets: `http://[9a]` and `[b.c:9-01]` parsed; CPython refuses.

Defects 2-4 were all the same shape -- MORE PERMISSIVE THAN THE ORACLE -- which
for a security check is the dangerous direction. A hand-written test suite would
have had to imagine each of those strings; the fuzz found them in seconds.
"""
from __future__ import annotations

import random
from urllib.parse import urlsplit          # THE ORACLE. Legal here, banned in core/.

import pytest

from atlas.core.parsing.url import UrlError, split_url

# Curated: every branch that can flip a security verdict.
CURATED = [
    "https://example.com", "http://example.com:8080/", "https://example.com/a/b?c=d#e",
    "https://EXAMPLE.COM/", "https://example.com./", "http://user@example.com/",
    "http://user:pw@example.com:99/", "http://[::1]:80/", "http://[2001:db8::1]/",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.1.1/",
    "http://example.com:99999/", "http://example.com:abc/", "https://8.8.8.8/",
    "http://metadata.google.internal/", "https://example.com?q=1",
    "https://example.com#f", "http://example.com:/", "https://sub.a.b.example.co.uk/x",
    "HTTP://Example.COM/", "http://[::ffff:127.0.0.1]/", "http://x.com:65535/",
    "http://x.com:65536/", "gopher://example.com/", "ftp://example.com/",
    "https://a@b@example.com/", "https://x@evil.com@example.com/",
    "https://u:p@example.com/", "https://example.com@evil.net/",
    "http://a@b@c@169.254.169.254/", "not-a-url", "", "//example.com/",
    "http:///nohost", "http://\u4f8b\u3048.jp/",
]

ALPHA = "abc.-@:[]/?#0129%_"


def _oracle(raw: str) -> tuple[str | None, str | None, int | None, str | None]:
    try:
        ss = urlsplit(raw)
    except ValueError:
        return None, None, None, "MALFORMED"
    scheme = ss.scheme.lower() or None
    try:
        host = ss.hostname
    except ValueError:
        return scheme, None, None, "MALFORMED"
    try:
        return scheme, host, ss.port, None
    except ValueError:
        return scheme, host, None, "BAD_PORT"


def _agrees(raw: str) -> tuple[bool, str]:
    """
    Do the two implementations reach the same SECURITY VERDICT for `raw`?

    Two differences are deliberate and documented, so they are not failures:
      * empty host reported as '' rather than None -- both become NO_HOST;
      * port 0 refused as BAD_PORT rather than returned as 0 -- port 0 is not
        dialable, so refusing is stricter.
    Refusing to parse at all is always acceptable UNLESS the oracle found a
    usable host, which would mean we are more permissive.
    """
    p = split_url(raw)
    o_s, o_h, o_p, o_e = _oracle(raw)

    if p.error == UrlError.MALFORMED:
        # Refusing is safe. It is only worth flagging if the oracle found a
        # usable host AND a scheme -- i.e. a fully-formed absolute URL we failed
        # to parse. A scheme-relative URL like '//example.com/' has no scheme,
        # so `Target.__post_init__` (absolute http(s) only) rejects it before
        # this function is ever reached; refusing it here is not permissiveness
        # in either direction.
        if o_h and o_s:
            return False, f"we refused but oracle parsed {o_s}://{o_h}"
        return True, ""
    if p.error == UrlError.BAD_PORT:
        if o_e == "BAD_PORT" or o_p == 0 or o_p is None:
            return True, ""
        return False, f"we said BAD_PORT, oracle said port={o_p!r}"

    if not ((p.host == o_h) or (not p.host and not o_h)):
        return False, f"host {p.host!r} != oracle {o_h!r}"
    if p.scheme != o_s:
        return False, f"scheme {p.scheme!r} != oracle {o_s!r}"
    if p.port != o_p and not (p.port is None and o_p == 0):
        return False, f"port {p.port!r} != oracle {o_p!r}"
    return True, ""


@pytest.mark.parametrize("raw", CURATED)
def test_curated_urls_match_cpython(raw: str) -> None:
    ok, why = _agrees(raw)
    assert ok, f"{raw!r}: {why}"


def test_fuzz_never_diverges_from_cpython() -> None:
    """
    50 000 random authorities. Seeded, so a divergence is reproducible rather
    than a flaky failure someone reruns until it passes.
    """
    rnd = random.Random(20260824)
    bad: list[tuple[str, str]] = []
    for _ in range(50_000):
        u = "http://" + "".join(
            rnd.choice(ALPHA) for _ in range(rnd.randint(1, 16))
        )
        ok, why = _agrees(u)
        if not ok:
            bad.append((u, why))
    assert not bad, f"{len(bad)} divergence(s), first 5: {bad[:5]}"


# ── the specific defects, pinned as regression tests ─────────────────────────
@pytest.mark.parametrize("raw,expect_host", [
    ("https://a@b@example.com/", "example.com"),
    ("https://x@evil.com@example.com/", "example.com"),
    ("http://a@b@c@169.254.169.254/", "169.254.169.254"),
    ("https://example.com@evil.net/", "evil.net"),
])
def test_userinfo_is_greedy_so_the_dialed_host_is_the_one_checked(
        raw: str, expect_host: str) -> None:
    """
    THE DENY-LIST BYPASS (ADR-030). A non-greedy userinfo made the host the
    deny-list sees differ from the host a client connects to -- the exact split
    an attacker needs. Also asserted against the oracle so this cannot drift.
    """
    p = split_url(raw)
    assert p.host == expect_host
    assert p.host == urlsplit(raw).hostname


@pytest.mark.parametrize("raw", [
    "http://9001]9:0_@-a",      # stray bracket
    "http://[#12]",             # bracketed junk
    "http://[9a]",              # no colon: not an IPv6 literal
    "http://[b.c:9-01]",        # colon present, still not IPv6
    "http://[:]",               # degenerate
    "http://[2_%]/x",           # bracketed non-address
])
def test_malformed_authorities_are_refused_not_guessed(raw: str) -> None:
    """
    Each of these was ACCEPTED by an earlier version of split_url while CPython
    refused it -- i.e. this parser was the permissive one. For a function whose
    output feeds an SSRF and deny-list decision, "refuse" is the only safe
    answer to an ambiguous authority.
    """
    assert split_url(raw).error is not None


@pytest.mark.parametrize("raw", [
    "http://c12.ca:c[/:.-]", "http://c@bbc90:]2?", "http://-:-[",
    "http://c219a:[", "http://1.:%%%9[/a222b:#b0",
])
def test_a_bracket_outside_the_host_position_is_refused_with_a_reason(
        raw: str) -> None:
    """
    Mutation-derived (ADR-030). Deleting the bracket-balance check left all 47
    parity tests GREEN, which looked like dead code -- but a 400 000-case
    differential run showed the branch IS reachable and changes the reason from
    MALFORMED to BAD_PORT on 13 403 inputs.

    No security verdict moves (both are refusals, and the oracle refuses these
    too), which is exactly why the parity test could not see it. The reason code
    is still a contract: ADR-029 exists because a refusal that misreports WHY is
    not diagnosable. So the distinction is pinned here rather than left to a
    branch no test observes.
    """
    p = split_url(raw)
    assert p.error == UrlError.MALFORMED, (
        f"{raw!r}: a stray bracket is a malformed authority, not a bad port; "
        f"got {p.error}"
    )


def test_a_bare_ipv6_literal_still_works() -> None:
    """
    The bracket tightening must not have broken the legitimate case -- the
    ADR-027 lesson: assert the work, not just the ceiling.
    """
    for raw, host, port in [
        ("http://[::1]:80/", "::1", 80),
        ("http://[2001:db8::1]/", "2001:db8::1", None),
        ("http://[::ffff:127.0.0.1]/", "::ffff:127.0.0.1", None),
    ]:
        p = split_url(raw)
        assert p.ok, f"{raw} should parse, got {p.error}"
        assert p.host == host and p.port == port
        assert p.host == urlsplit(raw).hostname
