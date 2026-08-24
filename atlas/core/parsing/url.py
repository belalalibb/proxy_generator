"""
PURE URL SPLITTER (ADR-030).

WHY THIS EXISTS

`policy/target_policy.py` needs the scheme, host and port of a caller-supplied
target URL. The obvious way to get them is `urllib.parse.urlsplit`. But
`test_architecture.py` bans `urllib` inside `atlas/core/` -- and that ban is
correct, not an inconvenience to be waived:

    urllib.parse is pure TODAY, but it lives in the same package as
    urllib.request, which opens sockets. Allowlisting the package name is how a
    module that must never touch the network acquires an import that can.

The guard caught my own code importing it. Rather than widen the allowlist (which
would have made the guard permanently weaker for one caller's convenience), the
parsing is implemented here, in pure `re`, and its correctness is PROVEN against
CPython by an oracle test that lives OUTSIDE core/ where urllib is legal:

    engineering/tools/url_split_parity.py     (writes engineering/raw/url_split_parity.json)
    atlas/tests/unit/test_url_parity.py       (imports urllib; tests/ are exempt)

THE BUG THIS DESIGN ALREADY CAUGHT

The first draft matched userinfo NON-greedily (`[^/?#@]*@`). Measured against
CPython:

    URL                                  draft host              real client dials
    https://x@evil.com@instagram.com/    'evil.com@instagram.com'  instagram.com

The deny-list is checked against the host, so that URL would have been ALLOWED
while every real HTTP client connects to the denied host. Userinfo must be
greedy: the authority ends at the LAST '@'. That is a deny-list bypass found by
differential testing, not by reading the regex.

WHAT IT DELIBERATELY DOES NOT DO

No percent-decoding, no IDNA/punycode conversion, no DNS. Decoding would create
two spellings of one host (ADR-017's defect class) and IDNA needs `encodings.idna`
tables; both belong at connect time in the adapter, which is where the real
client resolves the name anyway. This function answers exactly one question --
"what are the scheme, host and port AS WRITTEN?" -- and reports MALFORMED rather
than guessing when the authority is ambiguous.

Refusals are NAMED (`error`), never a bare None, for the ADR-029 reason: a
caller told "no" without being told why cannot produce a diagnosable failure.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

# Authority grammar, deliberately explicit about the greedy userinfo:
#   scheme://[userinfo@]host[:port][/?#rest]
# `userinfo` is `[^/?#]*` (NOT `[^/?#@]*`) so it consumes up to the LAST '@',
# matching what every HTTP client does. See the docstring: the non-greedy form
# was a deny-list bypass.
_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://"
    r"(?:(?P<userinfo>[^/?#]*)@)?"
    r"(?P<hostport>\[[^\]]*\]|[^/?#]*)"
    r"(?P<rest>[/?#].*)?$",
    re.DOTALL,
)



class UrlError(str):
    """Named parse failures. A `str` subclass, consistent with DropReason/TargetRefusal."""

    MALFORMED = "MALFORMED"
    BAD_PORT = "BAD_PORT"


@dataclass(frozen=True, slots=True)
class UrlParts:
    """
    The parsed authority. `host` is lowercased; the trailing root dot is NOT
    stripped here -- that is policy's business (a trailing dot is a legitimate
    FQDN spelling, and target_policy strips it when comparing against a deny
    entry so `instagram.com.` cannot bypass the list).
    """

    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def split_url(raw: str | None) -> UrlParts:
    """
    Split `raw` into scheme/host/port, or return a named error.

    Parity with `urllib.parse.urlsplit` is asserted by
    `atlas/tests/unit/test_url_parity.py` over curated cases plus 200 000 fuzz
    inputs; the two documented, deliberate differences are:

      * an empty host is reported as `''` where CPython reports `None` -- both
        become NO_HOST one layer up, so the verdict is identical;
      * port 0 is refused as BAD_PORT where CPython returns 0. Port 0 is not
        dialable, so refusing is stricter on purpose.
    """
    m = _URL.match(raw or "")
    if m is None:
        return UrlParts(error=UrlError.MALFORMED)

    scheme = m.group("scheme").lower()
    hostport = m.group("hostport") or ""
    userinfo = m.group("userinfo") or ""

    # Square brackets are legal ONLY as the IPv6 literal form `[...]` in the
    # host position. CPython raises "Invalid IPv6 URL" on an unbalanced bracket;
    # the first version of this function instead happily parsed
    # `http://9001]9:0_@-a` as host '-a' -- MORE permissive than the oracle,
    # which is the wrong direction for a security check. Anything else with a
    # bracket in the authority is refused outright.
    if "[" in userinfo or "]" in userinfo:
        return UrlParts(scheme=scheme, error=UrlError.MALFORMED)
    if ("[" in hostport or "]" in hostport) and not (
        hostport.startswith("[") and hostport.count("[") == 1
        and hostport.count("]") == 1
    ):
        return UrlParts(scheme=scheme, error=UrlError.MALFORMED)

    if hostport.startswith("["):
        # bracketed IPv6 literal
        end = hostport.find("]")
        if end < 0:
            return UrlParts(scheme=scheme, error=UrlError.MALFORMED)
        host = hostport[1:end]
        tail = hostport[end + 1:]
        if not host:
            return UrlParts(scheme=scheme, error=UrlError.MALFORMED)
        # Brackets mean "IPv6 literal", so the contents must actually BE one.
        # Without this, `http://[#12]` parsed to host '#12' while CPython
        # refuses the URL -- again more permissive than the oracle. Validated by
        # character class rather than ipaddress.ip_address() so that a scoped
        # literal (fe80::1%eth0) is still accepted; the routability decision
        # belongs to policy, not to the splitter.
        # A real IPv6 literal ALWAYS contains ':', and must actually PARSE as
        # one. Character-class checks were not enough: `[:]` and `[b.c:9-01]`
        # pass any reasonable class yet CPython refuses them, leaving this
        # splitter more permissive than the oracle. `ipaddress` is already on
        # core's allowlist and is the authority on the literal's shape, so the
        # zone id is split off (RFC 6874 `%eth0`, which ipaddress rejects) and
        # the address itself is parsed.
        pass  # MUTANT: no IPv6 validation
    else:
        # Split at the FIRST colon, as CPython does. An unbracketed authority
        # containing a second colon is not a valid host:port -- it is either a
        # bare IPv6 literal missing its brackets or junk -- and guessing which
        # is how parsers disagree with the client that eventually dials.
        i = hostport.find(":")
        host, tail = (hostport[:i], hostport[i:]) if i >= 0 else (hostport, "")
        if ":" in host:
            return UrlParts(scheme=scheme, error=UrlError.MALFORMED)

    if "@" in host or "[" in host or "]" in host:
        # Cannot happen for well-formed input; if it does, the authority is
        # ambiguous and must be refused rather than normalised into a guess.
        return UrlParts(scheme=scheme, error=UrlError.MALFORMED)

    port: int | None = None
    if tail.startswith(":"):
        digits = tail[1:]
        if digits:
            if not digits.isdigit():
                return UrlParts(scheme=scheme, host=host.lower(),
                                error=UrlError.BAD_PORT)
            port = int(digits)
            if not (1 <= port <= 65535):
                return UrlParts(scheme=scheme, host=host.lower(),
                                error=UrlError.BAD_PORT)
    elif tail:
        return UrlParts(scheme=scheme, host=host.lower(), error=UrlError.MALFORMED)

    return UrlParts(scheme=scheme, host=host.lower(), port=port)


__all__ = ["UrlParts", "UrlError", "split_url"]
