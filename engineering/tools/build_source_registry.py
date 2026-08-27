#!/usr/bin/env python3
"""P02 — build `atlas/data/sources/sources.json` from the PINNED probe snapshot.

ADR-002: sources are data, never Python literals.
ADR-005: a source's protocol label is a HINT, never a fact. Nothing here claims
         to know a proxy's real protocol; that is decided empirically in P06.

Deterministic and OFFLINE: reads the snapshot artifact, writes the registry.
No network. Re-running on the same snapshot yields a byte-identical file.

Run: python3 engineering/tools/build_source_registry.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "engineering" / "raw" / "source_probe_20260827T222532Z.json"
OUT = ROOT / "atlas" / "data" / "sources" / "sources.json"

# Verdicts that mean "this URL yielded parseable proxy candidates".
ACTIVE_VERDICTS = {"ALIVE", "ALIVE_JSON", "ALIVE_HTML_TABLE"}

# Verdict -> (state, disabled_reason). Every non-ENABLED row MUST name a reason.
STATE_MAP = {
    "ALIVE": ("ENABLED", None),
    "ALIVE_JSON": ("ENABLED", None),
    "ALIVE_HTML_TABLE": ("ENABLED", None),
    "DEAD": ("DISABLED", "probe_verdict_DEAD: transport or HTTP failure at snapshot time"),
    "TRULY_EMPTY": ("DISABLED", "probe_verdict_TRULY_EMPTY: 200 OK but no parser found candidates"),
    "THROTTLED_OR_SHORT": ("DISABLED", "probe_verdict_THROTTLED_OR_SHORT: short/identity-encoded read, unreliable"),
    "FETCH_INCOMPLETE": ("DISABLED", "probe_verdict_FETCH_INCOMPLETE: body did not reach EOF (ADR-013)"),
}

# Parser name in the snapshot -> parser id used by the registry.
PARSER_MAP = {
    "regex_adjacent": "regex_adjacent",
    "json_path": "json_path",
    "html_table": "html_table",
}

# ADR-005: protocol hints derived from URL tokens ONLY, and NEVER collapsed to a
# single guess when the URL disagrees with itself.
#
# Two real ambiguities found in this corpus (both would have been mislabelled by a
# naive first-match-wins scan):
#   1. .../TheSpeedX/SOCKS-List/master/http.txt
#      repo name says SOCKS, filename says http -> genuinely ambiguous
#   2. ...?request=getproxies&proxytype=http&...&ssl=yes&...
#      the PARAM says http; `ssl=yes` is a capability FILTER, not the protocol.
#      A naive `ssl` match labelled this `https`, contradicting proxytype=http.
#
# Therefore: explicit query parameters outrank free path text, and any residual
# conflict is reported as `ambiguous` rather than resolved by regex ordering.

# Query params that genuinely DECLARE the protocol being requested.
_PARAM_RX = re.compile(r"(?:^|[?&])(?:protocols?|proxytype|type)=([a-z0-9]+)", re.I)
# `ssl=yes` and friends are FILTERS on top of a protocol, not a protocol.
_FILTER_ONLY = re.compile(r"(?:^|[?&])ssl=", re.I)

_PATH_TOKENS: list[tuple[str, re.Pattern[str]]] = [
    ("socks5", re.compile(r"socks[_-]?5", re.I)),
    ("socks4", re.compile(r"socks[_-]?4", re.I)),
    ("socks", re.compile(r"socks", re.I)),
    ("https", re.compile(r"\bssl\b|\bhttps\b", re.I)),
    ("http", re.compile(r"\bhttp\b", re.I)),
]

# socks5/socks4 are refinements of socks, not conflicts with it.
_FAMILY = {"socks5": "socks", "socks4": "socks", "socks": "socks",
           "https": "web", "http": "web"}


def protocol_hint(url: str) -> tuple[str, str]:
    """Return (hint, how_derived).

    `unknown` and `ambiguous` are honest outcomes; a coin-flip would not be.
    """
    # The scheme (https://) describes the LIST's transport, not the proxies.
    body = re.sub(r"^[a-z]+://", "", url, flags=re.I)

    declared = [m.group(1).lower() for m in _PARAM_RX.finditer(body)]
    declared = [d for d in declared if d in {"socks5", "socks4", "socks", "https", "http"}]
    if declared:
        uniq = sorted(set(declared))
        if len(uniq) == 1:
            note = "+ssl_filter_ignored" if _FILTER_ONLY.search(body) else ""
            return uniq[0], f"query_param:{uniq[0]}{note}"
        return "ambiguous", "conflicting_query_params:" + ",".join(uniq)

    hits = [name for name, rx in _PATH_TOKENS if rx.search(body)]
    if not hits:
        return "unknown", "no_token_in_url"
    families = {_FAMILY[h] for h in hits}
    if len(families) > 1:
        return "ambiguous", "conflicting_path_tokens:" + ",".join(sorted(set(hits)))
    return hits[0], f"path_token:{hits[0]}"


def slugify(url: str, taken: set[str]) -> str:
    host = re.sub(r"^[a-z]+://", "", url, flags=re.I).split("/")[0].lower()
    host = re.sub(r"^www\.", "", host)
    tail = re.sub(r"^[a-z]+://[^/]+", "", url, flags=re.I)
    bits = [b for b in re.split(r"[^a-z0-9]+", tail.lower()) if b][:3]
    base = re.sub(r"[^a-z0-9]+", "-", host) + ("-" + "-".join(bits) if bits else "")
    base = base.strip("-")[:72] or "source"
    cand, n = base, 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    taken.add(cand)
    return cand


def main() -> int:
    if not SNAPSHOT.exists():
        print(f"FATAL: pinned snapshot missing: {SNAPSHOT}", file=sys.stderr)
        return 1

    snap = json.loads(SNAPSHOT.read_text())
    results = snap["results"]

    rows: list[dict] = []
    taken: set[str] = set()
    for r in sorted(results, key=lambda x: x["url"]):
        verdict = r["verdict"]
        if verdict not in STATE_MAP:
            print(f"FATAL: unmapped verdict {verdict!r} — refusing to guess", file=sys.stderr)
            return 1
        state, reason = STATE_MAP[verdict]
        hint, derivation = protocol_hint(r["url"])
        best = r.get("best_parser")
        parser = PARSER_MAP.get(best) if best else None
        if state == "ENABLED" and parser is None:
            print(f"FATAL: ENABLED row without a parser: {r['url']}", file=sys.stderr)
            return 1
        rows.append(
            {
                "id": slugify(r["url"], taken),
                "url": r["url"],
                "state": state,
                "disabled_reason": reason,
                "parser": parser,
                # ADR-005: a HINT. Never treated as the proxy's actual protocol.
                "labelled_protocol": hint,
                "label_derivation": derivation,
                "label_is_verified": False,
                "evidence": {
                    "snapshot": str(SNAPSHOT.relative_to(ROOT)),
                    "verdict": verdict,
                    "http_status": r.get("http_status"),
                    "unique_candidates": r.get("unique_candidates", 0),
                    "body_bytes": r.get("body_bytes"),
                    "fetched_at_utc": r.get("fetched_at_utc"),
                },
            }
        )

    enabled = [x for x in rows if x["state"] == "ENABLED"]
    active_in_snap = sum(1 for r in results if r["verdict"] in ACTIVE_VERDICTS)
    if len(enabled) != active_in_snap:
        print(
            f"FATAL: ENABLED={len(enabled)} != snapshot ACTIVE={active_in_snap}",
            file=sys.stderr,
        )
        return 1

    doc = {
        "schema_version": 1,
        "generator": "engineering/tools/build_source_registry.py",
        "generated_from_snapshot": str(SNAPSHOT.relative_to(ROOT)),
        "snapshot_measured_at_utc": snap.get("measured_at_utc"),
        "network_used": False,
        "deterministic": True,
        "honesty_notes": [
            "ADR-002: this file is the ONLY place source URLs live; no .py may hardcode one.",
            "ADR-005: `labelled_protocol` is a HINT derived from URL tokens, never a measurement. "
            "`label_is_verified` is false for every row because no proxy has been probed yet.",
            "State reflects ONE point in time (see snapshot). A DISABLED row is not proof a "
            "source is permanently dead: ACTIVE varied 67-69 across two sweeps 5 minutes apart.",
        ],
        "counts": {
            "total": len(rows),
            "enabled": len(enabled),
            "disabled": len(rows) - len(enabled),
            "by_parser": dict(Counter(x["parser"] for x in enabled)),
            "by_protocol_hint": dict(Counter(x["labelled_protocol"] for x in rows)),
            "protocol_hint_unknown": sum(1 for x in rows if x["labelled_protocol"] == "unknown"),
        },
        "sources": rows,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    print("=" * 74)
    print(" P02 — source registry built from PINNED snapshot (offline, deterministic)")
    print("=" * 74)
    print(f"  snapshot : {SNAPSHOT.name}")
    print(f"  total    : {len(rows)}")
    print(f"  ENABLED  : {len(enabled)}  (== snapshot ACTIVE {active_in_snap})")
    print(f"  DISABLED : {len(rows) - len(enabled)}  (every row names a reason)")
    print(f"  parsers  : {doc['counts']['by_parser']}")
    print(f"  hints    : {doc['counts']['by_protocol_hint']}")
    print(f"-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
