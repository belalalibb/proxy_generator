"""
Candidate extraction — PURE functions, ported from the P00 probe tools.

WHY THESE LIVE IN core/
Parsing a body is pure computation: str in, candidates out. Keeping it in core
means every parser is testable offline against stored evidence, which is the
discipline ADR-013(e) demands: *validate the parser on stored bytes, so that a
live zero implicates the FETCH rather than the source.*

WHY THE COUNTS ARE PINNED ELSEWHERE
These three strategies are not a design guess; they are what was measured in
P00.T4/T5. The adjacency regex — the legacy approach — returns **0** candidates
from the GeoNode JSON body, while `json_path` returns **500** from the same
bytes. That single comparison is the whole justification for declaring a parser
per source instead of running one regex over everything.

DELIBERATE DUPLICATION, NAMED HONESTLY
`engineering/tools/probe_legacy_sources.py` keeps its own copies because it is a
frozen record of the 2026-08-24 audit and must not change when production code
evolves. `test_parsing.py` asserts the two implementations agree on the stored
GeoNode body, so this duplication cannot drift silently.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ip and port ADJACENT — what the legacy code did. Its blind spot is the point.
RX_ADJACENT = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\s*[:\s]\s*(\d{2,5})\b")
RX_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

_IP_KEYS = ("ip", "host", "address", "addr", "proxy", "ipaddress", "ip_address")
_PORT_KEYS = ("port", "portnumber", "port_number")

# Recursion bound: a hostile/looping JSON document must not exhaust the stack
# inside a pure function that the gate runs on every commit.
_MAX_DEPTH = 64


def valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def valid_port(port: str) -> bool:
    return port.isdigit() and 1 <= int(port) <= 65535


def parse_adjacent(body: str) -> set[str]:
    """Strategy 1 — the legacy regex. Blind to JSON and HTML tables by design."""
    out: set[str] = set()
    for ip, port in RX_ADJACENT.findall(body):
        if valid_ip(ip) and valid_port(port):
            out.add(f"{ip}:{port}")
    return out


def parse_json_path(body: str) -> set[str]:
    """Strategy 2 — recursive walk pairing ip-ish and port-ish keys (GeoNode: 500)."""
    out: set[str] = set()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        # A body that is not JSON yields nothing. This is PARSE_EMPTY, and the
        # caller must not confuse it with a truncated read (ADR-013).
        return out

    def walk(node: object, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, dict):
            low = {str(k).lower().replace("-", "").replace("_", ""): v
                   for k, v in node.items()}
            ip = next((low[k.replace("_", "")] for k in _IP_KEYS
                       if k.replace("_", "") in low), None)
            port = next((low[k.replace("_", "")] for k in _PORT_KEYS
                         if k.replace("_", "") in low), None)
            if ip is not None and port is not None:
                s_ip, s_port = str(ip).strip(), str(port).strip()
                if valid_ip(s_ip) and valid_port(s_port):
                    out.add(f"{s_ip}:{s_port}")
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(data, 0)
    return out


def parse_html_table(body: str) -> set[str]:
    """Strategy 3 — cell-pair scan; ip and port in separate <td> (recovered 6 sources)."""
    out: set[str] = set()
    if "<t" not in body.lower():
        return out
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        ip = next((c for c in cells if RX_IP.fullmatch(c) and valid_ip(c)), None)
        if not ip:
            continue
        port = next((c for c in cells if c.isdigit() and valid_port(c)), None)
        if port:
            out.add(f"{ip}:{port}")
    return out


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Candidates plus the parser that produced them, so yield is attributable."""
    parser: str
    candidates: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.candidates)


_STRATEGIES = {
    "regex_adjacent": parse_adjacent,
    "json_path": parse_json_path,
    "html_table": parse_html_table,
}

PARSER_NAMES = frozenset(_STRATEGIES)


def parse_body(parser: str, body: str) -> ParseResult:
    """
    Parse with the DECLARED parser only.

    It would be convenient to try all three and keep the best. That is refused:
    silently succeeding with a different parser than the registry declares would
    hide the fact that the declaration is wrong, and per-source attribution
    (ADR-002) is the thing that makes a dead source diagnosable. A registry error
    should be visible as zero candidates, not papered over at runtime.
    """
    fn = _STRATEGIES.get(parser)
    if fn is None:
        raise ValueError(
            f"unknown parser {parser!r}; declared parsers are "
            f"{sorted(_STRATEGIES)} (ADR-002: the set is closed)"
        )
    return ParseResult(parser=parser, candidates=tuple(sorted(fn(body))))
