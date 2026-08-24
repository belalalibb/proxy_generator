"""Source registry loader — reads `data/sources/sources.json` (ADR-002).

This adapter is the ONLY code permitted to know where source URLs come from, and
it reads them from data. No URL literal may appear in any `.py` under `atlas/`
(enforced by `test_registry.py::test_no_hardcoded_source_urls_in_python`).

Loading is strict by design: a malformed registry raises rather than silently
yielding fewer sources. A quietly-shrinking source list is exactly the failure
mode that hid the truncated-fetch bug in ADR-013.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from atlas.core.domain.source import ParserKind, Source, SourceState

# Resolved relative to this file so the loader works from any cwd.
_DEFAULT_REGISTRY = (
    pathlib.Path(__file__).resolve().parents[1] / "data" / "sources" / "sources.json"
)

VALID_STATES = frozenset({"ENABLED", "DISABLED"})

# ADR-017: DERIVED from the domain enum, never re-typed. This literal previously
# read {"regex_adjacent", "json_path", "html_table"} while ParserKind offered
# {line_ipport, json_path, csv_columns, html_table, regex} -- two vocabularies for
# one concept, and no code compared them. Deriving makes divergence impossible.
VALID_PARSERS = frozenset(k.value for k in ParserKind)
# `unknown` and `ambiguous` are first-class honest outcomes, not error states.
VALID_HINTS = frozenset({"http", "https", "socks", "socks4", "socks5", "unknown", "ambiguous"})


class RegistryError(ValueError):
    """Raised when the registry file violates its own contract."""


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One immutable registry row.

    `labelled_protocol` is a HINT (ADR-005). `label_is_verified` is False until a
    real probe measures the proxy, which cannot happen before P06.
    """

    id: str
    url: str
    state: str
    parser: str | None
    labelled_protocol: str
    label_derivation: str
    label_is_verified: bool
    disabled_reason: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.state == "ENABLED"

    @property
    def protocol_is_certain(self) -> bool:
        """False for unknown/ambiguous hints AND for any unverified label."""
        return self.label_is_verified and self.labelled_protocol not in {"unknown", "ambiguous"}


@dataclass(frozen=True, slots=True)
class Registry:
    rows: tuple[SourceRow, ...]
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[SourceRow]:
        return iter(self.rows)

    def enabled(self) -> tuple[SourceRow, ...]:
        return tuple(r for r in self.rows if r.enabled)

    def disabled(self) -> tuple[SourceRow, ...]:
        return tuple(r for r in self.rows if not r.enabled)

    def by_parser(self, parser: str) -> tuple[SourceRow, ...]:
        return tuple(r for r in self.enabled() if r.parser == parser)

    def urls(self) -> tuple[str, ...]:
        return tuple(r.url for r in self.enabled())


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegistryError(msg)


def load_registry(path: str | pathlib.Path | None = None) -> Registry:
    """Load and validate the source registry. Raises `RegistryError` on any breach."""
    p = pathlib.Path(path) if path is not None else _DEFAULT_REGISTRY
    _require(p.exists(), f"registry not found: {p}")

    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as exc:  # pragma: no cover - message clarity only
        raise RegistryError(f"registry is not valid JSON: {exc}") from exc

    _require(isinstance(doc, dict), "registry root must be an object")
    _require(doc.get("schema_version") == 1, f"unsupported schema_version: {doc.get('schema_version')}")
    raw_rows = doc.get("sources")
    _require(isinstance(raw_rows, list) and raw_rows, "registry has no `sources` array")

    rows: list[SourceRow] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()

    for i, r in enumerate(raw_rows):
        where = f"sources[{i}]"
        _require(isinstance(r, dict), f"{where} is not an object")
        for key in ("id", "url", "state", "labelled_protocol", "label_derivation"):
            _require(bool(r.get(key)), f"{where} missing required field `{key}`")

        rid, url, state = r["id"], r["url"], r["state"]
        _require(rid not in seen_ids, f"{where} duplicate id: {rid}")
        _require(url not in seen_urls, f"{where} duplicate url: {url}")
        seen_ids.add(rid)
        seen_urls.add(url)

        _require(state in VALID_STATES, f"{where} invalid state: {state}")
        _require(
            r["labelled_protocol"] in VALID_HINTS,
            f"{where} invalid labelled_protocol: {r['labelled_protocol']}",
        )
        _require(
            "label_is_verified" in r and isinstance(r["label_is_verified"], bool),
            f"{where} label_is_verified must be an explicit bool",
        )

        parser = r.get("parser")
        reason = r.get("disabled_reason")
        if state == "ENABLED":
            _require(parser in VALID_PARSERS, f"{where} ENABLED needs a valid parser, got {parser!r}")
        else:
            # The core P02 contract: a disabled source must say WHY.
            _require(
                isinstance(reason, str) and reason.strip() != "",
                f"{where} DISABLED must name a disabled_reason",
            )

        rows.append(
            SourceRow(
                id=rid,
                url=url,
                state=state,
                parser=parser,
                labelled_protocol=r["labelled_protocol"],
                label_derivation=r["label_derivation"],
                label_is_verified=r["label_is_verified"],
                disabled_reason=reason,
                evidence=r.get("evidence", {}) or {},
            )
        )

    meta = {k: v for k, v in doc.items() if k != "sources"}
    return Registry(rows=tuple(rows), meta=meta)


def row_to_source(row: SourceRow) -> Source:
    """
    Convert a registry row into a domain `Source`.

    ADR-017 -- THIS FUNCTION IS WHY THE VOCABULARY BUG WAS FOUND. Until P03,
    nothing crossed the seam between the registry (built from measured probe
    data) and the domain (hand-written first). The registry said
    `regex_adjacent`; `ParserKind` had no such member; and because no code ever
    turned a row into a Source, `ParserKind(row.parser)` was never evaluated and
    the contradiction sat undetected through two green phase gates.

    A DISABLED row is converted with its reason preserved rather than dropped:
    ADR-002 requires that a dead source stay auditable instead of vanishing.
    """
    if row.parser is None:
        # 37 of the 53 DISABLED rows have no parser: they were never successfully
        # parsed by anything, which is WHY they are disabled. A `Source` requires
        # a parser -- an object that cannot say how to read its own payload is not
        # a source -- so these rows are deliberately NOT representable here.
        # Their auditability lives at the SourceRow/JSON level, which keeps the
        # reason (ADR-002), so nothing is lost by refusing the conversion.
        raise RegistryError(
            f"cannot build a Source from {row.id!r}: no parser declared. "
            "Rows with no parser are disabled precisely because nothing parsed "
            "them; they stay auditable as registry rows, not as Sources"
        )
    try:
        kind = ParserKind(row.parser)
    except ValueError as exc:
        raise RegistryError(
            f"{row.id!r} declares parser {row.parser!r}, which is not a "
            f"ParserKind ({sorted(k.value for k in ParserKind)})"
        ) from exc

    src = Source(
        id=row.id,
        url=row.url,
        parser=kind,
        labelled_protocol=row.labelled_protocol,
        state=SourceState.ACTIVE if row.enabled else SourceState.DISABLED,
    )
    if not row.enabled:
        # `disabled()` refuses an empty reason, which is exactly the invariant.
        src = src.disabled(row.disabled_reason or "disabled in registry")
    return src


def fetchable_sources(registry: Registry) -> tuple[Source, ...]:
    """The ENABLED rows as domain objects, ready for a SourcePort."""
    return tuple(row_to_source(r) for r in registry.enabled())


__all__: Sequence[str] = [
    "Registry", "RegistryError", "SourceRow", "load_registry",
    "row_to_source", "fetchable_sources",
]
