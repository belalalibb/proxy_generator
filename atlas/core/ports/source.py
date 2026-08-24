"""
SourcePort — fetch a source list and parse it into candidates.

Two measured requirements are encoded in this interface:

1. ADR-006 conditional fetching. `SourceFetch` carries etag/last_modified and can
   report SOURCE_UNCHANGED, because re-downloading an unchanged 649k-candidate list
   is how the legacy code caused its own 429/403 responses.

2. A short or throttled body is NOT an empty source. `SourceFetch` distinguishes
   `throttled` from `parsed_zero`, because conflating them is precisely how the
   GeoNode API (230 019 bytes, 500 proxies) was filed TRULY_EMPTY off one
   659-byte read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from atlas.core.domain.source import Source
from atlas.core.domain.verdict import ReasonCode


@dataclass(frozen=True, slots=True)
class SourceFetch:
    """The result of one source fetch + parse."""
    source_id: str
    ok: bool
    reason: ReasonCode
    candidates: tuple[str, ...] = ()      # raw "host:port" strings, unvalidated
    http_status: int | None = None
    body_bytes: int = 0
    elapsed_ms: float | None = None
    etag: str | None = None
    last_modified: str | None = None
    parser_used: str | None = None
    detail: str | None = None

    @property
    def unique_candidates(self) -> int:
        return len(set(self.candidates))

    @property
    def duplicate_count(self) -> int:
        return len(self.candidates) - self.unique_candidates

    @property
    def throttled(self) -> bool:
        """HTTP 200 + suspiciously small body + nothing parsed => throttled."""
        return (self.http_status == 200 and not self.candidates
                and self.body_bytes < 2000)

    def __post_init__(self) -> None:
        if self.ok and self.reason is not ReasonCode.OK:
            raise ValueError("a successful fetch cannot carry a failure reason")
        if not self.ok and self.reason is ReasonCode.OK:
            raise ValueError("a failed fetch must name its reason")


@runtime_checkable
class SourcePort(Protocol):
    """Implemented in adapters/. core/ never imports aiohttp."""

    async def fetch(self, source: Source) -> SourceFetch:
        """
        Fetch + parse one source.

        MUST send If-None-Match / If-Modified-Since when the source has them
        recorded, and MUST return reason=SOURCE_UNCHANGED on a 304 rather than
        re-parsing (ADR-006).
        """
        ...

    def load_registry(self) -> tuple[Source, ...]:
        """
        Load sources from data/sources/sources.json.

        A source is never a Python literal (ADR-002): the legacy tree had 257 URL
        lines across 6 files, so a dead source could not be cooled down and yield
        could not be attributed past the hostname.
        """
        ...

    def save_registry(self, sources: tuple[Source, ...]) -> None:
        """
        Persist updated per-source stats atomically.

        MUST write .tmp then os.replace(): the legacy save() truncated with
        open(...,'w') in 8 places, so a kill mid-write destroyed the file (B-04).
        """
        ...
