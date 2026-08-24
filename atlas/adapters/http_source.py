"""
HttpSourceAdapter — the SourcePort implementation. The ONLY place aiohttp appears
for source fetching.

THE DISCIPLINE THIS FILE EXISTS TO ENFORCE (ADR-013)

The GeoNode API was filed as an empty/dead source THREE times, from three
different causes, while it was serving 500 usable proxies:

  1. an adjacency regex found 0 candidates in valid JSON (ip/port in separate keys)
  2. a 659-byte throttled body, produced by our own per-host hammering, read as "empty"
  3. `resp.content.read(n)` returned only the BUFFERED prefix -- 74 241 of
     230 067 octets -- so the JSON would not parse

Cause 3 is the one this adapter is built around. `read(n)` is not "read n bytes";
it is "give me what is buffered, up to n". The fix is to read to EOF in chunks and
then PROVE completeness against Content-Length before parsing.

Three facts that the legacy code collapsed into one, kept separate here:

  FETCH_INCOMPLETE  the bytes did not all arrive. OUR fault. Says nothing about
                    the source. Never let this be recorded as "empty".
  SOURCE_THROTTLED  200 OK, but a tiny body and nothing parsed -- the signature
                    of rate-limiting, not of an empty list.
  PARSE_EMPTY       the body arrived INTACT and genuinely contained no candidates.
                    Only this one is evidence about the source itself.

Distinguishing them is the entire difference between "GeoNode is dead" (wrong,
three times) and "our fetch was truncated" (correct, and actionable).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

from atlas.core.domain.source import Source
from atlas.core.domain.verdict import ReasonCode
from atlas.core.parsing import parse_body
from atlas.core.ports.source import SourceFetch

# A body cannot be trusted past this size; we record hitting the cap as
# FETCH_INCOMPLETE rather than parsing a prefix and calling it the whole list.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_CHUNK = 64 * 1024

# Below this, a 200 OK with zero candidates is far more likely throttling than a
# genuinely empty list. 659 octets -- the observed GeoNode throttle body -- sits
# well inside this bound.
THROTTLE_BODY_CEILING = 2000

USER_AGENT = "atlas-proxy-fabric/4.0 (source fetch; contact: operator)"


class ReadIncompleteError(RuntimeError):
    """Raised when a body did not arrive intact. Never silently swallowed."""

    def __init__(self, got: int, expected: int | None, capped: bool) -> None:
        self.got, self.expected, self.capped = got, expected, capped
        if capped:
            msg = f"body hit the {got}-octet cap; refusing to parse a prefix"
        else:
            msg = f"short read: got {got} octets, Content-Length said {expected}"
        super().__init__(msg)


async def read_to_eof(
    content: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    chunk_size: int = DEFAULT_CHUNK,
) -> tuple[bytes, bool]:
    """
    Read a stream to EOF in chunks. Returns (body, hit_cap).

    ADR-013: this exists because `content.read(n)` returns only the buffered
    prefix. `iter_chunked` keeps pulling until the stream actually ends, which is
    the difference between 74 241 and 230 067 octets on the body that started all
    of this.
    """
    buf = bytearray()
    async for chunk in content.iter_chunked(chunk_size):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return bytes(buf[:max_bytes]), True
    return bytes(buf), False


def verify_complete(
    body: bytes, headers: Mapping[str, str], hit_cap: bool
) -> None:
    """
    Prove the body arrived intact, or raise.

    Content-Length is only comparable when the payload was NOT
    content-encoded: with gzip, Content-Length describes the compressed
    stream while `body` is decompressed, so comparing them would raise a
    false FETCH_INCOMPLETE on every compressed response. When it is not
    comparable we say so rather than pretending to have verified anything.
    """
    if hit_cap:
        raise ReadIncompleteError(len(body), None, capped=True)

    if (headers.get("Content-Encoding") or "").strip():
        return  # not comparable -- honestly unverifiable, not "verified"

    raw_len = headers.get("Content-Length")
    if not raw_len:
        return  # server declared nothing; absence of proof is not proof of loss
    try:
        expected = int(raw_len)
    except (TypeError, ValueError):
        return
    if len(body) < expected:
        raise ReadIncompleteError(len(body), expected, capped=False)


def classify_fetch(
    source: Source,
    *,
    status: int,
    body: bytes,
    headers: Mapping[str, str],
    elapsed_ms: float,
) -> SourceFetch:
    """
    Turn a COMPLETE response into a SourceFetch. Pure; no I/O.

    Separated from the network call so every branch below is testable offline
    against stored bodies -- the ADR-013(e) requirement that a parser be
    validated on real bytes before any live verdict is trusted.
    """
    common = dict(
        source_id=source.id,
        http_status=status,
        body_bytes=len(body),          # ADR-015: OCTETS, matching the field name
        elapsed_ms=elapsed_ms,
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
    )

    if status == 304:
        # ADR-006: an unchanged list must not be re-parsed or re-counted.
        return SourceFetch(ok=True, reason=ReasonCode.OK,
                           parser_used=None,
                           detail="not modified since last fetch", **common)

    if status != 200:
        return SourceFetch(ok=False, reason=ReasonCode.SOURCE_DEAD,
                           detail=f"HTTP {status}", **common)

    text = body.decode("utf-8", errors="replace")
    result = parse_body(source.parser.value, text)

    if result.candidates:
        return SourceFetch(ok=True, reason=ReasonCode.OK,
                           candidates=result.candidates,
                           parser_used=result.parser, **common)

    # 200 OK and nothing parsed. Two very different facts live here.
    if len(body) < THROTTLE_BODY_CEILING:
        return SourceFetch(
            ok=False, reason=ReasonCode.SOURCE_THROTTLED,
            parser_used=result.parser,
            detail=(f"200 OK but only {len(body)} octets and no candidates: "
                    "throttling is far likelier than an empty list (ADR-006)"),
            **common)

    return SourceFetch(
        ok=False, reason=ReasonCode.PARSE_EMPTY,
        parser_used=result.parser,
        detail=(f"body arrived intact ({len(body)} octets) and parser "
                f"{result.parser!r} found no candidates"),
        **common)


class HttpSourceAdapter:
    """
    SourcePort over aiohttp.

    Conditional requests (ADR-006) are sent whenever the source has an ETag or
    Last-Modified recorded: re-downloading an unchanged 649k-candidate list is
    how the legacy code earned its own 429/403 responses.
    """

    def __init__(
        self,
        session: Any,
        *,
        timeout_ms: int = 20_000,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._session = session
        self._timeout_ms = timeout_ms
        self._max_bytes = max_bytes

    def _headers(self, source: Source) -> dict[str, str]:
        h = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        if source.stats.last_etag:
            h["If-None-Match"] = source.stats.last_etag
        if source.stats.last_modified:
            h["If-Modified-Since"] = source.stats.last_modified
        return h

    async def fetch(self, source: Source) -> SourceFetch:
        started = time.monotonic()

        def ms() -> float:
            return (time.monotonic() - started) * 1000.0

        try:
            async with self._session.get(
                source.url, headers=self._headers(source),
                timeout=self._timeout_ms / 1000.0,
            ) as resp:
                headers = resp.headers
                status = resp.status

                if status == 304:
                    return classify_fetch(source, status=304, body=b"",
                                          headers=headers, elapsed_ms=ms())

                body, hit_cap = await read_to_eof(
                    resp.content, max_bytes=self._max_bytes)
                verify_complete(body, headers, hit_cap)

        except ReadIncompleteError as exc:
            # OUR failure. Explicitly NOT evidence that the source is empty.
            return SourceFetch(
                source_id=source.id, ok=False,
                reason=ReasonCode.FETCH_INCOMPLETE,
                http_status=200, body_bytes=exc.got, elapsed_ms=ms(),
                detail=f"{exc} -- this is a FETCH failure, not an empty source")
        except asyncio.TimeoutError:
            return SourceFetch(
                source_id=source.id, ok=False, reason=ReasonCode.TCP_TIMEOUT,
                elapsed_ms=ms(),
                detail=f"no response within {self._timeout_ms}ms")
        except (OSError, ValueError) as exc:
            # Named, never bare (BUG_LEDGER B-02: 23 silent handlers in legacy).
            return SourceFetch(
                source_id=source.id, ok=False, reason=ReasonCode.SOURCE_DEAD,
                elapsed_ms=ms(),
                detail=f"{type(exc).__name__}: {exc}")

        return classify_fetch(source, status=status, body=body,
                              headers=headers, elapsed_ms=ms())


__all__ = [
    "HttpSourceAdapter", "ReadIncompleteError", "classify_fetch",
    "read_to_eof", "verify_complete", "THROTTLE_BODY_CEILING",
]
