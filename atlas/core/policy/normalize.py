"""
Candidate normalisation — PURE. The seam between "text a source emitted" and
"an object the pool can hold".

WHY THIS IS A SEPARATE, TESTED LAYER

`atlas.core.parsing` produces raw `"host:port"` strings. The pool needs `Proxy`
objects with a validated `Endpoint`. Between those two lies every dirty-data
problem the legacy code handled by accident or not at all:

  * duplicates across sources (the legacy sweep counted 649 404 candidates from
    GitHub alone, unattributable across ~50 repos -- ANALYSIS.md §5)
  * scheme prefixes: `socks5://1.2.3.4:1080`
  * private/loopback/reserved ranges, which are parse artifacts, never proxies
  * whitespace, CR from CRLF files, credentials, IPv6 forms

THE DEFECT THIS MODULE WAS BUILT AROUND (ADR-019)

`Endpoint.parse` uses a regex with a NAMED CAPTURE GROUP for the scheme:

    _HOSTPORT = re.compile(r"^\\s*(?:(?P<scheme>\\w+)://)?(?P<host>[^:/@\\s]+):(?P<port>\\d{1,5})\\s*$")

...and then never reads `m.group("scheme")`. Verified directly: parsing
`socks5://1.2.3.4:1080` returns an Endpoint indistinguishable from parsing
`1.2.3.4:1080`. The protocol declaration is captured and thrown away.

That is precisely the fact ADR-005 says is scarce. A `socks5://` prefix is the
source stating its protocol IN THE CANDIDATE ITSELF -- stronger evidence than the
filename hint that made `TheSpeedX/SOCKS-List/master/http.txt` ambiguous. Losing
it means those candidates get probed as HTTP, fail, and are discarded: the exact
shape of B-12, which cost 2 853 candidates.

So normalisation returns the scheme as a `labelled_protocol` beside the endpoint,
and never lets it silently vanish.

WHAT THIS MODULE REFUSES TO DO

It does not guess a protocol when none is stated (that stays UNKNOWN, for S3 to
discover), and it does not "repair" a malformed candidate. A candidate that
cannot be read is DROPPED WITH A NAMED REASON and counted, because the legacy
code's silent `continue` is why 35 dead URLs were retried forever (B-02).
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass, field

from atlas.core.domain.proxy import Endpoint, InvalidProxy, Protocol, Proxy, ProxyState

# Scheme -> the protocol it declares. `socks` alone is genuinely ambiguous
# between v4 and v5, so it maps to UNKNOWN rather than being guessed as socks5.
_SCHEMES: dict[str, Protocol] = {
    "http": Protocol.HTTP,
    "https": Protocol.HTTPS,
    "socks4": Protocol.SOCKS4,
    "socks4a": Protocol.SOCKS4,
    "socks5": Protocol.SOCKS5,
    "socks5h": Protocol.SOCKS5,
    "socks": Protocol.UNKNOWN,
}

_SCHEME_RE = re.compile(r"^\s*(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<rest>.*)$")

# Ports that are never a proxy in the wild; seeing them means the parser paired
# an IP with an unrelated number from the same line. Kept SMALL and justified:
# guessing too aggressively here would discard real proxies on odd ports, and
# proxy.txt shows real-world ports as diverse as 83, 999 and 3125.
_IMPOSSIBLE_PORTS = frozenset({0})

# RFC 6598 carrier-grade NAT. Named explicitly because `ipaddress` reports
# is_private=False AND is_global=False for it, so it fell through every specific
# check in normalize_one and was ACCEPTED -- while SECURITY.md P5 claimed it was
# rejected "via ipaddress" (ADR-028).
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_globally_routable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Is this address routable on the public internet?

    Exported so the SSRF rule has ONE implementation: the candidate path
    (normalize_one, below) and the target path (policy/target_policy.py, ADR-029)
    both call this. Two copies of a security predicate is two things to keep in
    sync, and the one that drifts is the one nobody is testing.

    This is deliberately NOT a replacement for the specific is_loopback /
    is_multicast / is_private checks. Measured on CPython 3.13.13:

        224.0.0.1        is_global=True   (multicast!)
        239.255.255.250  is_global=True   (multicast!)
        100.64.1.1       is_global=False  is_private=False  (CGNAT)

    So `is_global` alone would newly ADMIT multicast, and the specific checks
    alone miss CGNAT. Neither subsumes the other; normalize_one runs both, with
    this as the closing catch-all.
    """
    return bool(ip.is_global)


class DropReason:
    """
    Named drop reasons. Strings rather than an enum because these are diagnostic
    counters, not control flow -- nothing branches on them, they are reported.
    """
    UNPARSEABLE = "UNPARSEABLE"
    PRIVATE_RANGE = "PRIVATE_RANGE"
    LOOPBACK = "LOOPBACK"
    RESERVED_RANGE = "RESERVED_RANGE"
    MULTICAST = "MULTICAST"
    UNSPECIFIED = "UNSPECIFIED"
    LINK_LOCAL = "LINK_LOCAL"
    CGNAT_RANGE = "CGNAT_RANGE"
    NOT_GLOBALLY_ROUTABLE = "NOT_GLOBALLY_ROUTABLE"
    BAD_PORT = "BAD_PORT"
    HAS_CREDENTIALS = "HAS_CREDENTIALS"
    NOT_AN_IP = "NOT_AN_IP"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class NormalizedCandidate:
    """One accepted candidate, with the protocol its own text declared."""
    endpoint: Endpoint
    labelled_protocol: Protocol = Protocol.UNKNOWN
    scheme_seen: str | None = None      # verbatim, for auditing the source

    @property
    def key(self) -> str:
        """Dedup key: host:port. Deliberately protocol-INDEPENDENT.

        A source listing the same endpoint twice under two schemes is making a
        claim about protocol, not offering two proxies. S3 discovers the truth,
        so deduping on host:port avoids probing one box twice on a guess.
        """
        return f"{self.endpoint.host}:{self.endpoint.port}"


@dataclass(frozen=True, slots=True)
class NormalizeReport:
    """
    The outcome of normalising a batch. Accepted AND dropped, with reasons.

    Both halves are returned because "we ingested 500 candidates" and "we
    ingested 500 and silently discarded 900" are different facts, and the legacy
    system could only ever report the first.
    """
    accepted: tuple[NormalizedCandidate, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()      # (raw, reason)
    seen: int = 0

    @property
    def dropped_by_reason(self) -> dict[str, int]:
        return dict(Counter(reason for _, reason in self.dropped))

    @property
    def accept_rate(self) -> float | None:
        return len(self.accepted) / self.seen if self.seen else None

    def __post_init__(self) -> None:
        total = len(self.accepted) + len(self.dropped)
        if self.seen and total != self.seen:
            raise ValueError(
                f"normalisation lost candidates: seen={self.seen} but "
                f"accepted+dropped={total}. Every input must be accounted for."
            )


def split_scheme(raw: str) -> tuple[str | None, str]:
    """
    Split `socks5://1.2.3.4:1080` into ('socks5', '1.2.3.4:1080').

    ADR-019: this is the information `Endpoint.parse` captures and discards.
    Returned separately so the caller must decide what to do with it, rather
    than it evaporating inside a regex group nobody reads.
    """
    m = _SCHEME_RE.match(raw or "")
    if not m:
        return None, (raw or "").strip()
    return m.group("scheme").lower(), m.group("rest").strip()


def normalize_one(raw: str) -> tuple[NormalizedCandidate | None, str | None]:
    """
    Normalise ONE raw candidate. Returns (candidate, None) or (None, reason).

    Never raises for bad input: a source's junk line is expected, not
    exceptional. It IS always accounted for -- the reason is returned, never
    swallowed.
    """
    if not raw or not raw.strip():
        return None, DropReason.UNPARSEABLE

    scheme, rest = split_scheme(raw)

    # Credentials are refused rather than stripped. `user:pass@host:port` from a
    # public list is either a leaked credential or a parse artifact; using it
    # would mean sending someone else's secret to a third party.
    if "@" in rest:
        return None, DropReason.HAS_CREDENTIALS

    try:
        endpoint = Endpoint.parse(rest)
    except InvalidProxy:
        return None, DropReason.UNPARSEABLE

    if endpoint.port in _IMPOSSIBLE_PORTS:
        return None, DropReason.BAD_PORT

    # Only literal IPs are accepted from public lists. A hostname would have to
    # be resolved to be probed, and resolution is I/O -- which core/ may not do
    # (test_architecture.py). Deferring it silently would mean a candidate that
    # can never be probed sitting in the pool forever.
    try:
        ip = ipaddress.ip_address(endpoint.host)
    except ValueError:
        return None, DropReason.NOT_AN_IP

    # ORDER IS PART OF THE CONTRACT (same principle as admission.py): the most
    # specific true statement wins, and the catch-all closes the class last.
    if ip.is_unspecified:
        return None, DropReason.UNSPECIFIED
    if ip.is_loopback:
        return None, DropReason.LOOPBACK
    if ip.is_multicast:
        return None, DropReason.MULTICAST
    if ip.is_link_local:
        # Before is_private: 169.254.169.254 (the cloud metadata endpoint named
        # in SECURITY.md P5) is BOTH, and LINK_LOCAL is the actionable reading.
        return None, DropReason.LINK_LOCAL
    if ip.version == 4 and ip in _CGNAT:
        # ADR-028. This branch is why the bug existed: CGNAT satisfies NONE of
        # the properties above and is_global is False, so before this check the
        # candidate was accepted.
        return None, DropReason.CGNAT_RANGE
    if ip.is_private:
        # Checked AFTER loopback and multicast so the reason is the most
        # specific true statement: 127.0.0.1 is private too, but LOOPBACK is
        # what a human needs to read.
        return None, DropReason.PRIVATE_RANGE
    if ip.is_reserved:
        return None, DropReason.RESERVED_RANGE
    if not is_globally_routable(ip):
        # THE CATCH-ALL. Without this the check is an enumeration with no
        # backstop, and the next reserved-but-unflagged allocation walks
        # straight through -- which is exactly how CGNAT got in.
        return None, DropReason.NOT_GLOBALLY_ROUTABLE

    return NormalizedCandidate(
        endpoint=endpoint,
        labelled_protocol=_SCHEMES.get(scheme or "", Protocol.UNKNOWN),
        scheme_seen=scheme,
    ), None


def normalize_batch(
    raws: tuple[str, ...] | list[str], *, dedup: bool = True
) -> NormalizeReport:
    """
    Normalise a batch, optionally deduping. ORDER-STABLE: the first occurrence
    of an endpoint wins, so a run over the same input is byte-reproducible.
    """
    accepted: list[NormalizedCandidate] = []
    dropped: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for raw in raws:
        cand, reason = normalize_one(raw)
        if cand is None:
            dropped.append((raw, reason or DropReason.UNPARSEABLE))
            continue
        if dedup and cand.key in seen_keys:
            dropped.append((raw, DropReason.DUPLICATE))
            continue
        seen_keys.add(cand.key)
        accepted.append(cand)

    return NormalizeReport(
        accepted=tuple(accepted), dropped=tuple(dropped), seen=len(raws),
    )


def to_proxies(
    report: NormalizeReport, *, source_id: str | None = None
) -> tuple[Proxy, ...]:
    """
    Turn accepted candidates into DISCOVERED proxies.

    `labelled_protocol` is carried through and `protocol` stays UNKNOWN: the
    label is a hint until S3 measures it (ADR-005), and `Proxy.protocol_mismatch`
    exists precisely to compare the two afterwards. Writing the label into
    `protocol` here would destroy the ability to detect that a source lied.
    """
    return tuple(
        Proxy(
            endpoint=c.endpoint,
            protocol=Protocol.UNKNOWN,
            labelled_protocol=c.labelled_protocol,
            state=ProxyState.DISCOVERED,
            source_id=source_id,
        )
        for c in report.accepted
    )


__all__ = [
    "DropReason", "NormalizeReport", "NormalizedCandidate",
    "is_globally_routable",
    "normalize_batch", "normalize_one", "split_scheme", "to_proxies",
]
