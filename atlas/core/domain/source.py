"""
Source + Target — PURE domain objects.

A Source is DATA, never a Python literal (ADR-002). The legacy tree hardcoded 257
URL lines / 123 unique URLs across 6 files, so adding a source meant editing code,
a dead source could not be cooled down, and yield could not be attributed:
proxy_scraper.log credits `raw.githubusercontent.com` with 649 404 candidates
across ~50 DIFFERENT repositories (ANALYSIS.md §5).

Hence `id` is stable and per-source, and `stats` is the unit of attribution.

Measured basis for ParserKind: the legacy regex required ip and port to be
ADJACENT, which silently produced zero candidates from live JSON APIs and HTML
tables. Re-probing with structured parsers recovered 6 sources a regex-only audit
would have thrown away (P00.T4), and the GeoNode JSON API alone yields 500
(P00.T5). So the parser is declared per source, from a closed set.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class ParserKind(str, Enum):
    """
    Closed, declarative set (ADR-002) -- and deliberately limited to the parsers
    that ACTUALLY EXIST in atlas.core.parsing.

    ADR-017. This enum previously read LINE_IPPORT / JSON_PATH / CSV_COLUMNS /
    HTML_TABLE / REGEX. Three of those five were speculation: no parser
    implemented csv_columns or regex, and nothing in the registry ever used
    line_ipport. Meanwhile the registry that P02 generated from measured probe
    results labels 59 of its 67 ENABLED rows `regex_adjacent` -- a value this
    enum could not represent at all.

    Nothing had converted a registry row into a Source yet, so the mismatch was
    invisible: two vocabularies for one concept, neither validated against the
    other. Now the enum names exactly what was measured and implemented, and
    `test_parsing.py::test_every_parser_kind_has_an_implementation` fails if a
    member is ever added without a parser behind it.
    """
    REGEX_ADJACENT = "regex_adjacent"   # ip:port adjacent -- the legacy strategy, 59 rows
    JSON_PATH = "json_path"             # ip/port under separate keys (GeoNode: 500)
    HTML_TABLE = "html_table"           # ip/port in separate <td> (recovered 6 sources)


class SourceState(str, Enum):
    ACTIVE = "ACTIVE"
    COOLING = "COOLING"       # transient failure; returns automatically (ADR-006)
    DISABLED = "DISABLED"     # disabled WITH A REASON, never silently dropped
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class SourceStats:
    """
    Per-source attribution. `quality_rate` is the field that matters.

    ANALYSIS.md §5 lesson -- VOLUME != VALUE: 649 404 candidates from GitHub still
    produced only 102 slow proxies. v4 ranks sources by quality, never raw count.
    """
    fetches: int = 0
    fetch_failures: int = 0
    consecutive_failures: int = 0          # drives cooldown (ADR-006)
    candidates_seen: int = 0
    candidates_unique: int = 0
    admitted: int = 0                      # passed the speed gate
    elite: int = 0
    last_fetch: datetime | None = None
    last_success: datetime | None = None
    last_reason: str | None = None
    last_etag: str | None = None           # ADR-006: don't refetch unchanged lists
    last_modified: str | None = None

    @property
    def quality_rate(self) -> float | None:
        """admitted / unique -- the only ranking signal that predicts usefulness."""
        if not self.candidates_unique:
            return None
        return self.admitted / self.candidates_unique

    @property
    def elite_rate(self) -> float | None:
        if not self.candidates_unique:
            return None
        return self.elite / self.candidates_unique

    @property
    def duplicate_ratio(self) -> float | None:
        """High duplication => the source mirrors others and adds little."""
        if not self.candidates_seen:
            return None
        return 1.0 - (self.candidates_unique / self.candidates_seen)


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    url: str
    parser: ParserKind
    parser_args: dict[str, str] = field(default_factory=dict)
    state: SourceState = SourceState.ACTIVE
    labelled_protocol: str = "unknown"      # a HINT only (ADR-005)
    stats: SourceStats = field(default_factory=SourceStats)
    disabled_reason: str | None = None
    cooldown_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("source id is required (yield must be attributable)")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"source url must be absolute http(s): {self.url!r}")

    @property
    def is_fetchable(self) -> bool:
        return self.state is SourceState.ACTIVE

    def with_stats(self, stats: SourceStats) -> Source:
        return replace(self, stats=stats)

    def disabled(self, reason: str) -> Source:
        """
        A source is never silently deleted -- 18 TRULY_EMPTY sources were registered
        disabled WITH a reason in P00.T4 precisely so the decision stays auditable.
        """
        if not reason:
            raise ValueError("disabling a source requires a reason")
        return replace(self, state=SourceState.DISABLED, disabled_reason=reason)

    def cooling(self, until: datetime, reason: str) -> Source:
        return replace(self, state=SourceState.COOLING, cooldown_until=until,
                       disabled_reason=reason)

    def reactivated(self) -> Source:
        return replace(self, state=SourceState.ACTIVE, cooldown_until=None,
                       disabled_reason=None)


@dataclass(frozen=True, slots=True)
class Target:
    """
    What a proxy is measured AGAINST. Required, never defaulted (H5 / ADR-007).

    The legacy code defaulted to instagram.com (v1.py:29, v3.py:30) and probed a
    login-walled third party thousands of times per run. That also conflated target
    difficulty with proxy quality: the 0.68% legacy success rate substantially
    measured Instagram's defences, not the proxies.
    """
    url: str
    expect_status: int = 200
    min_bytes: int = 0
    timeout_ms: int = 8000

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError(
                "a target URL is required: there is no default target (H5/ADR-007)"
            )
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"target must be absolute http(s): {self.url!r}")
