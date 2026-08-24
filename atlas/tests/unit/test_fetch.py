"""
P03 — source fetch + parse, tested ENTIRELY OFFLINE.

No test here touches the network. Every body is either a literal or the stored
`engineering/raw/geonode_body.txt`, which is the actual 230 067-octet response
that was misfiled as empty three times.

Why offline matters beyond speed: ADR-013(e) requires a parser to be validated
against stored bytes BEFORE any live verdict is trusted. If these tests pass and
a live fetch returns zero candidates, the fault is provably in the FETCH, not the
source. A network-dependent test cannot support that inference.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from atlas.adapters.http_source import (
    THROTTLE_BODY_CEILING, HttpSourceAdapter, ReadIncompleteError,
    classify_fetch, read_to_eof, verify_complete,
)
from atlas.adapters.registry import (
    RegistryError, SourceRow, fetchable_sources, load_registry, row_to_source,
)
from atlas.core.domain.source import ParserKind, Source, SourceState, SourceStats
from atlas.core.domain.verdict import ReasonCode
from atlas.core.parsing import PARSER_NAMES, parse_body
from atlas.core.parsing.candidates import _STRATEGIES

ROOT = pathlib.Path(__file__).resolve().parents[3]
GEONODE = ROOT / "engineering" / "raw" / "geonode_body.txt"

# Pinned in P00.T5 and re-proved by verify_geonode_parser.py.
GEONODE_UNIQUE = 500
GEONODE_OCTETS = 230067
GEONODE_CHARS = 230019


def run(coro):
    """Tiny runner: pytest-asyncio is not installed, and adding a dependency to
    run three coroutines would be a worse trade than four lines here."""
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════
# The vocabulary defect this phase exposed (ADR-017)
# ══════════════════════════════════════════════════════════════════════════════
def test_every_parser_kind_has_an_implementation() -> None:
    """
    ADR-017. `ParserKind` once offered five members while only three parsers
    existed, and omitted `regex_adjacent` -- the value 59 of 67 ENABLED registry
    rows actually carry. Nothing converted a row into a Source, so nothing ever
    evaluated `ParserKind(row.parser)` and the contradiction survived two green
    phase gates. This test makes the enum and the implementations one fact.
    """
    kinds = {k.value for k in ParserKind}
    assert kinds == set(_STRATEGIES), (
        f"ParserKind={sorted(kinds)} but implemented={sorted(_STRATEGIES)}; "
        "a declared parser with no implementation yields silent zeros"
    )
    assert kinds == set(PARSER_NAMES)


def test_registry_parser_values_are_all_representable_in_the_domain() -> None:
    """The end-to-end version of the same bug: every ENABLED row must convert."""
    reg = load_registry()
    srcs = fetchable_sources(reg)
    assert len(srcs) == len(reg.enabled()) == 67
    assert all(s.is_fetchable for s in srcs)


def test_row_to_source_rejects_an_unrepresentable_parser() -> None:
    """Negative control: the conversion must FAIL on a bad parser, not coerce it."""
    row = SourceRow(
        id="x", url="https://example.com/l.txt", state="ENABLED",
        parser="line_ipport",            # plausible, but no implementation
        labelled_protocol="http", label_derivation="test",
        label_is_verified=False,
    )
    with pytest.raises(RegistryError, match="not a ParserKind"):
        row_to_source(row)


def test_row_to_source_preserves_the_disabled_reason() -> None:
    """A dead source stays auditable (ADR-002); it is never silently dropped."""
    row = SourceRow(
        id="dead", url="https://example.com/d.txt", state="DISABLED",
        parser="regex_adjacent",   # 16 of the 53 DISABLED rows do have a parser
        labelled_protocol="unknown", label_derivation="test",
        label_is_verified=False, disabled_reason="probe_verdict_TRULY_EMPTY",
    )
    src = row_to_source(row)
    assert src.state is SourceState.DISABLED
    assert src.disabled_reason == "probe_verdict_TRULY_EMPTY"
    assert not src.is_fetchable


def test_a_row_with_no_parser_is_not_representable_as_a_source() -> None:
    """
    37 of the 53 DISABLED rows carry no parser -- nothing ever parsed them, which
    is exactly why they are disabled. A `Source` that cannot say how to read its
    own payload is not a source, so the conversion REFUSES rather than inventing
    a default parser. My first version of this test asserted the opposite; the
    registry data proved the design, not the assumption.

    Auditability is not lost: the reason survives on the registry row.
    """
    row = SourceRow(
        id="never-parsed", url="https://example.com/x.txt", state="DISABLED",
        parser=None, labelled_protocol="unknown", label_derivation="test",
        label_is_verified=False, disabled_reason="probe_verdict_TRULY_EMPTY",
    )
    with pytest.raises(RegistryError, match="no parser declared"):
        row_to_source(row)
    assert row.disabled_reason == "probe_verdict_TRULY_EMPTY"   # still auditable


def test_disabled_rows_without_a_parser_are_the_majority_case() -> None:
    """Pins the fact that drove the decision above, so it cannot silently change."""
    reg = load_registry()
    dis = reg.disabled()
    assert len(dis) == 53
    assert sum(1 for r in dis if r.parser is None) == 37
    assert all(r.disabled_reason for r in dis)   # every one names a reason


def test_parse_body_refuses_an_undeclared_parser() -> None:
    with pytest.raises(ValueError, match="unknown parser"):
        parse_body("bespoke_scraper", "1.2.3.4:8080")


# ══════════════════════════════════════════════════════════════════════════════
# ADR-013 — the truncated read, reproduced
# ══════════════════════════════════════════════════════════════════════════════
class BufferedPrefixStream:
    """
    Reproduces the aiohttp behaviour that caused the third misclassification:
    `read(n)` hands back only what is currently BUFFERED, while `iter_chunked`
    keeps pulling until the stream truly ends.
    """

    def __init__(self, body: bytes, buffered: int) -> None:
        self._body, self._buffered = body, buffered

    async def read(self, n: int = -1) -> bytes:
        return self._body[: self._buffered]        # the bug, faithfully

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), size):
            await asyncio.sleep(0)
            yield self._body[i : i + size]


def test_the_legacy_read_pattern_really_does_truncate() -> None:
    """
    Establishes that the fixture models a REAL defect. Without this, the test
    below proves only that my own fake is self-consistent.
    """
    body = GEONODE.read_bytes()
    stream = BufferedPrefixStream(body, buffered=74241)
    got = run(stream.read(len(body)))
    assert len(got) == 74241 < len(body), "fixture must actually truncate"
    # And the truncated JSON is genuinely unparseable -> 0 candidates, which is
    # exactly how a live 500-proxy source got filed empty.
    assert parse_body("json_path", got.decode("utf-8", "replace")).count == 0


def test_read_to_eof_recovers_the_whole_body() -> None:
    body = GEONODE.read_bytes()
    assert len(body) == GEONODE_OCTETS
    got, capped = run(read_to_eof(BufferedPrefixStream(body, buffered=74241)))
    assert not capped
    assert len(got) == GEONODE_OCTETS
    assert got == body


def test_full_body_yields_the_pinned_500() -> None:
    """The measured P00.T5 figure, reproduced by PRODUCTION code this time."""
    text = GEONODE.read_bytes().decode("utf-8")
    assert len(text) == GEONODE_CHARS            # ADR-015: chars != octets
    assert parse_body("json_path", text).count == GEONODE_UNIQUE
    assert parse_body("regex_adjacent", text).count == 0   # legacy blind spot


def test_hitting_the_cap_is_reported_not_parsed() -> None:
    body = b"1.2.3.4:8080\n" * 5000
    got, capped = run(read_to_eof(BufferedPrefixStream(body, len(body)),
                                  max_bytes=1024))
    assert capped and len(got) == 1024
    with pytest.raises(ReadIncompleteError, match="refusing to parse a prefix"):
        verify_complete(got, {}, capped)


def test_short_read_against_content_length_raises() -> None:
    with pytest.raises(ReadIncompleteError, match="short read"):
        verify_complete(b"x" * 100, {"Content-Length": "230067"}, False)


def test_content_encoding_makes_length_incomparable() -> None:
    """
    A gzip Content-Length describes the COMPRESSED stream. Comparing it with the
    decompressed body would raise FETCH_INCOMPLETE on every compressed response
    -- a false alarm that would be worse than the bug it guards.
    """
    verify_complete(b"x" * 100, {"Content-Length": "230067",
                                 "Content-Encoding": "gzip"}, False)


def test_absent_content_length_is_not_treated_as_loss() -> None:
    """Absence of proof is not proof of loss."""
    verify_complete(b"x" * 100, {}, False)


# ══════════════════════════════════════════════════════════════════════════════
# The three facts the legacy code collapsed into one
# ══════════════════════════════════════════════════════════════════════════════
def _src(parser: ParserKind = ParserKind.JSON_PATH, **kw) -> Source:
    return Source(id="s1", url="https://example.com/api", parser=parser, **kw)


def test_throttled_short_body_is_not_an_empty_source() -> None:
    """
    The 659-octet GeoNode throttle body. Recording this as PARSE_EMPTY is the
    second of the three misclassifications.
    """
    body = b'{"error":"rate limited"}' + b" " * 600
    assert len(body) < THROTTLE_BODY_CEILING
    r = classify_fetch(_src(), status=200, body=body, headers={}, elapsed_ms=5.0)
    assert r.reason is ReasonCode.SOURCE_THROTTLED
    assert not r.ok
    assert r.reason is not ReasonCode.PARSE_EMPTY


def test_intact_body_with_no_candidates_is_parse_empty() -> None:
    """Only an INTACT body is evidence about the source itself."""
    body = b"<html><body>no proxies here</body></html>" + b"x" * 3000
    assert len(body) > THROTTLE_BODY_CEILING
    r = classify_fetch(_src(ParserKind.HTML_TABLE), status=200, body=body,
                       headers={}, elapsed_ms=5.0)
    assert r.reason is ReasonCode.PARSE_EMPTY
    assert "arrived intact" in (r.detail or "")


def test_the_three_failures_are_distinct_reason_codes() -> None:
    """
    The whole point of P03: these must never collapse together again. Each of the
    three GeoNode misclassifications maps to a DIFFERENT code.
    """
    assert len({ReasonCode.FETCH_INCOMPLETE, ReasonCode.SOURCE_THROTTLED,
                ReasonCode.PARSE_EMPTY}) == 3


def test_geonode_body_classifies_as_a_success() -> None:
    body = GEONODE.read_bytes()
    r = classify_fetch(_src(), status=200, body=body,
                       headers={"Content-Length": str(len(body))},
                       elapsed_ms=12.0)
    assert r.ok and r.reason is ReasonCode.OK
    assert r.unique_candidates == GEONODE_UNIQUE
    assert r.body_bytes == GEONODE_OCTETS      # ADR-015: octets, as named
    assert r.parser_used == "json_path"


def test_declared_parser_is_used_even_when_another_would_win() -> None:
    """
    Refusing parser fallback is deliberate. Silently succeeding with a parser the
    registry did not declare would hide a wrong declaration, and per-source
    attribution is what makes a bad source diagnosable (ADR-002).
    """
    body = GEONODE.read_bytes()
    r = classify_fetch(_src(ParserKind.REGEX_ADJACENT), status=200, body=body,
                       headers={}, elapsed_ms=1.0)
    assert r.reason is ReasonCode.PARSE_EMPTY   # NOT silently rescued by json_path
    assert r.parser_used == "regex_adjacent"


def test_non_200_is_source_dead_not_empty() -> None:
    r = classify_fetch(_src(), status=403, body=b"", headers={}, elapsed_ms=1.0)
    assert r.reason is ReasonCode.SOURCE_DEAD and "403" in (r.detail or "")


def test_304_is_unchanged_and_does_not_re_parse() -> None:
    """ADR-006: an unchanged list must not be re-counted as new yield."""
    r = classify_fetch(_src(), status=304, body=b"", headers={}, elapsed_ms=1.0)
    assert r.ok and r.candidates == () and r.parser_used is None


# ══════════════════════════════════════════════════════════════════════════════
# Adapter wiring, still offline
# ══════════════════════════════════════════════════════════════════════════════
class FakeResponse:
    def __init__(self, status, body, headers=None, buffered=None):
        self.status, self.headers = status, headers or {}
        self.content = BufferedPrefixStream(body, buffered or len(body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, resp):
        self._resp, self.seen_headers = resp, None

    def get(self, url, headers=None, timeout=None):
        self.seen_headers = headers or {}
        return self._resp


def test_adapter_sends_conditional_headers_when_known() -> None:
    """ADR-006: re-downloading an unchanged list is how we earned our own 429s."""
    src = _src(stats=SourceStats(last_etag='W/"abc"',
                                 last_modified="Wed, 21 Oct 2015 07:28:00 GMT"))
    sess = FakeSession(FakeResponse(304, b""))
    r = run(HttpSourceAdapter(sess).fetch(src))
    assert sess.seen_headers["If-None-Match"] == 'W/"abc"'
    assert "If-Modified-Since" in sess.seen_headers
    assert r.ok


def test_adapter_reads_full_body_despite_small_buffer() -> None:
    """End-to-end proof that the ADR-013 defect cannot recur through the adapter."""
    body = GEONODE.read_bytes()
    sess = FakeSession(FakeResponse(200, body,
                                    {"Content-Length": str(len(body))},
                                    buffered=74241))
    r = run(HttpSourceAdapter(sess).fetch(_src()))
    assert r.ok
    assert r.body_bytes == GEONODE_OCTETS
    assert r.unique_candidates == GEONODE_UNIQUE


def test_adapter_reports_fetch_incomplete_not_empty() -> None:
    """
    A truncated stream must implicate the FETCH. This is the exact inference that
    stopped "GeoNode is dead" being written into the record a third time.
    """

    class Truncating(BufferedPrefixStream):
        async def iter_chunked(self, size):
            yield self._body[:1000]      # stops early, mid-stream

    body = GEONODE.read_bytes()
    resp = FakeResponse(200, body, {"Content-Length": str(len(body))})
    resp.content = Truncating(body, 1000)
    r = run(HttpSourceAdapter(FakeSession(resp)).fetch(_src()))
    assert r.reason is ReasonCode.FETCH_INCOMPLETE
    assert not r.ok
    assert "not an empty source" in (r.detail or "")


def test_adapter_timeout_is_named() -> None:
    class Boom:
        def get(self, *a, **k):
            raise asyncio.TimeoutError()

    r = run(HttpSourceAdapter(Boom()).fetch(_src()))
    assert r.reason is ReasonCode.TCP_TIMEOUT


def test_no_test_in_this_file_uses_the_network() -> None:
    """
    Self-check: the offline guarantee is the basis for trusting a live zero
    (ADR-013(e)), so it is asserted rather than assumed.

    Implemented with AST imports, not substring matching. My first version
    searched the file text for banned names and therefore matched ITS OWN list of
    banned names -- a guard that fails on itself and reports nothing about the
    other 20 tests. Exactly the class of self-referential check ADR-010 warns of.
    """
    import ast
    tree = ast.parse(pathlib.Path(__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("aiohttp", "requests", "urllib", "httpx", "socket"):
        assert banned not in imported, (
            f"this suite imports {banned!r}; it must stay offline so that a live "
            "zero implicates the fetch, not the source (ADR-013(e))"
        )
