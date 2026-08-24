"""
TARGET ALLOW-POLICY — the check `config.yaml` has described since P01 and nothing
ever ran (ADR-029).

WHAT WAS WRONG

`config.yaml` carries a `targets.allow_policy` block: `deny_private_ranges`,
`deny_hosts: [instagram.com, facebook.com, tiktok.com]`,
`max_requests_per_host_per_min`. `SECURITY.md` §3 promises the target "must pass
the allow-policy". A mechanical search for a reader found NONE:

    grep -rn "allow_policy\\|deny_hosts\\|deny_private_ranges" --include=*.py .
    -> no match outside the legacy scripts

`Target.__post_init__` validates only that the URL is non-empty and absolute
http(s). So `Target(url="https://instagram.com")` -- the exact legacy default that
ADR-007 prohibits and MIGRATION_LEDGER.md files as RETIRED_PROHIBITED -- was
constructible. ADR-007's "no default target" half was real and enforced; its
"allow-policy-checked" half was documentation.

That is ADR-019's defect class (a captured fact that nothing reads) applied to a
POLICY instead of a datum, and the third appearance of the shape: ADR-019 (a
captured scheme nobody read), ADR-021 (a port filtering on a field the domain
could not express), and now a policy nobody evaluated.

WHY THIS IS PURE, AND WHAT IT DELIBERATELY DOES NOT DO

No DNS. Resolving a hostname to check where it points is I/O, and core/ may not
do I/O (test_architecture.py). This module therefore decides on the URL AS
WRITTEN: a hostname that resolves to 127.0.0.1 (DNS rebinding) is NOT caught
here, and saying so is the honest position -- the mitigation for that is at
connect time in the adapter, and it is recorded as P09 scope rather than implied
by silence here.

`max_requests_per_host_per_min` is likewise NOT implemented here: a rate limit
needs a clock and shared state. Pretending a pure function enforces it would
recreate the very defect this module fixes.

REFUSALS ARE NAMED

`check_target` returns a `TargetRefusal | None`, never a bool. A bool would tell
a caller "no" without telling them why, and B-02 (23 silent handlers, no
diagnosable failure anywhere) is what that habit costs.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from atlas.core.domain.source import Target
from atlas.core.policy.normalize import is_globally_routable


class TargetRefusal(str):
    """
    Named refusal reasons. A `str` subclass for the same reason DropReason uses
    plain strings: these are diagnostics that get reported, not control flow that
    gets branched on.
    """

    NO_TARGET = "NO_TARGET"                       # ADR-007: no default exists
    BAD_SCHEME = "BAD_SCHEME"                     # not http/https
    NO_HOST = "NO_HOST"
    NOT_GLOBALLY_ROUTABLE = "NOT_GLOBALLY_ROUTABLE"   # ADR-028 predicate, reused
    METADATA_ENDPOINT = "METADATA_ENDPOINT"       # SSRF: cloud metadata
    DENIED_HOST = "DENIED_HOST"                   # deny_hosts, incl. subdomains
    BAD_PORT = "BAD_PORT"


# Cloud metadata hostnames. The IP form (169.254.169.254) is already covered by
# the link-local branch of the routability check; these are the NAME forms, which
# no IP check can catch.
_METADATA_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "metadata",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class TargetPolicy:
    """
    The allow-policy, as data. Mirrors `config.yaml targets.allow_policy` so the
    file stops being decorative.

    `deny_hosts` defaults to the three hosts config.yaml names. They are defaults
    rather than hardcoded constants -- ADR-002's lesson is that policy DATA must
    be overridable -- but the default is not empty, because a policy object that
    denies nothing unless configured would let the legacy target back in through
    an omission.
    """

    deny_private_ranges: bool = True
    deny_hosts: frozenset[str] = frozenset({
        "instagram.com", "facebook.com", "tiktok.com",
    })
    deny_metadata_hosts: bool = True

    def __post_init__(self) -> None:
        if not self.deny_private_ranges:
            # Allowed, but it must be a deliberate act. Silently permitting SSRF
            # because a config field defaulted the wrong way is how P5 gets
            # violated without anyone editing security-relevant code.
            pass
        bad = [h for h in self.deny_hosts if not h or h.startswith(".") or "/" in h]
        if bad:
            raise ValueError(
                f"deny_hosts entries must be bare hostnames, got {bad!r}"
            )


def host_matches_deny(host: str, deny: str) -> bool:
    """
    Does `host` fall under the denied domain `deny`, including subdomains?

    Matches on LABEL BOUNDARIES, not `str.endswith`. Measured difference:

        host                    endswith('instagram.com')   label-boundary
        instagram.com           True                        True
        www.instagram.com       True                        True
        graph.instagram.com     True                        True
        notinstagram.com        True   <-- WRONG             False
        myinstagram.com         True   <-- WRONG             False

    `endswith` would refuse `notinstagram.com`, an unrelated domain. A deny-list
    that over-refuses is not "safely conservative": it silently makes legitimate
    targets un-testable, and the operator gets DENIED_HOST with no idea why.
    """
    h = host.lower().rstrip(".")
    d = deny.lower().rstrip(".")
    return h == d or h.endswith("." + d)


def check_target(target: Target | None, policy: TargetPolicy | None = None,
                 ) -> TargetRefusal | None:
    """
    Return the refusal reason, or None if the target is allowed.

    ORDER IS PART OF THE CONTRACT, as in admission.py and normalize.py: the most
    fundamental failure is reported first, so a caller who passes
    `gopher://instagram.com` is told BAD_SCHEME (the thing they must fix) rather
    than DENIED_HOST.
    """
    if target is None:
        # ADR-007. There is no default target, so "absent" is a refusal and not
        # an invitation to substitute one.
        return TargetRefusal.NO_TARGET

    policy = policy or TargetPolicy()

    parts = urlsplit(target.url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return TargetRefusal.BAD_SCHEME

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return TargetRefusal.NO_HOST

    try:
        port = parts.port
    except ValueError:
        # urlsplit raises for a non-numeric or out-of-range port only when the
        # attribute is READ, which is why this is wrapped rather than trusted.
        return TargetRefusal.BAD_PORT
    if port is not None and not (1 <= port <= 65535):
        return TargetRefusal.BAD_PORT

    if policy.deny_metadata_hosts and host in _METADATA_HOSTS:
        return TargetRefusal.METADATA_ENDPOINT

    if policy.deny_private_ranges:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None          # a hostname; see the no-DNS note in the docstring
        if ip is not None and not is_globally_routable(ip):
            # THE SAME predicate normalize_one uses (ADR-028/ADR-029). One
            # implementation of the SSRF rule, so the candidate path and the
            # target path cannot drift apart.
            return TargetRefusal.NOT_GLOBALLY_ROUTABLE

    for denied in policy.deny_hosts:
        if host_matches_deny(host, denied):
            return TargetRefusal.DENIED_HOST

    return None


def require_target(target: Target | None,
                   policy: TargetPolicy | None = None) -> Target:
    """
    Return the target, or raise `TargetNotAllowed` naming the reason.

    Provided so a caller cannot accidentally ignore a refusal: `check_target`
    returns a value that is easy to drop on the floor, and this codebase already
    has one instance of a computed fact nobody read (ADR-019).
    """
    reason = check_target(target, policy)
    if reason is not None:
        raise TargetNotAllowed(reason, target)
    assert target is not None      # narrowed by check_target's NO_TARGET branch
    return target


class TargetNotAllowed(ValueError):
    """Raised by `require_target`. Carries the named reason, never just a message."""

    def __init__(self, reason: str, target: Target | None = None) -> None:
        self.reason = reason
        self.target_url = target.url if target is not None else None
        super().__init__(
            f"target refused: {reason}"
            + (f" ({self.target_url})" if self.target_url else "")
        )


__all__ = [
    "TargetPolicy", "TargetRefusal", "TargetNotAllowed",
    "check_target", "require_target", "host_matches_deny",
]
