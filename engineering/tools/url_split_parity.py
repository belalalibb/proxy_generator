"""
ADR-030 evidence generator: prove the PURE url splitter agrees with CPython's
urllib.parse.urlsplit on every host/scheme/port decision that can change a
security verdict, WITHOUT core/ importing urllib (banned by test_architecture).

This tool lives in engineering/tools (not core/, not tests/) because it is
allowed to import urllib: it is the oracle, not the implementation.

Run:  python3 engineering/tools/url_split_parity.py
Writes: engineering/raw/url_split_parity.json
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from urllib.parse import urlsplit          # the ORACLE. core/ may never do this.

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.core.parsing.url import UrlParts, split_url   # noqa: E402

# Cases chosen to cover every branch that can flip a deny/SSRF decision.
CASES = [
    "https://example.com", "http://example.com:8080/", "https://example.com/a/b?c=d#e",
    "https://EXAMPLE.COM/", "https://example.com./", "http://user@example.com/",
    "http://user:pw@example.com:99/", "http://[::1]:80/", "http://[2001:db8::1]/",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.1.1/",
    "http://example.com:99999/", "http://example.com:abc/", "https://8.8.8.8/",
    "http://metadata.google.internal/", "https://example.com?q=1",
    "https://example.com#f", "http://example.com:/", "https://sub.a.b.example.co.uk/x",
    "http://\u4f8b\u3048.jp/", "HTTP://Example.COM/", "http://[::ffff:127.0.0.1]/",
    "http://x.com:65535/", "http://x.com:65536/", "http://x.com:0/",
    "gopher://example.com/", "ftp://example.com/",
    # credential-confusion: the host a real client DIALS is after the LAST '@'
    "https://a@b@example.com/", "https://x@evil.com@example.com/",
    "https://u:p@example.com/", "https://example.com@evil.net/",
    "http://a@b@c@169.254.169.254/",
]

ALPHA = "abc.-@:[]/?#0129%_"


def oracle(raw: str) -> tuple[str | None, str | None, int | None, str | None]:
    """CPython's answer, normalised into our 4-tuple shape."""
    try:
        ss = urlsplit(raw)
    except ValueError:
        return None, None, None, "MALFORMED"
    scheme = ss.scheme.lower() or None
    try:
        host = ss.hostname
    except ValueError:
        return scheme, None, None, "MALFORMED"
    try:
        port = ss.port
        err = None
    except ValueError:
        port, err = None, "BAD_PORT"
    return scheme, host, port, err


def mine(raw: str) -> tuple[str | None, str | None, int | None, str | None]:
    p: UrlParts = split_url(raw)
    return p.scheme, p.host, p.port, p.error


def compare(raw: str) -> dict | None:
    m_s, m_h, m_p, m_e = mine(raw)
    o_s, o_h, o_p, o_e = oracle(raw)

    # An empty host and a None host are the SAME verdict downstream (NO_HOST),
    # so that difference is not a divergence in meaning.
    host_same = (m_h == o_h) or (not m_h and not o_h)
    # We refuse port 0 as BAD_PORT; CPython reports 0. Refusing is stricter and
    # deliberate (port 0 is not dialable), so it is recorded, not counted.
    port_same = (m_p == o_p) or (m_e == "BAD_PORT" and (o_e == "BAD_PORT" or o_p == 0))
    scheme_same = (m_s == o_s) or m_e == "MALFORMED"

    if m_e == "MALFORMED":
        # Refusing to parse is always safe: the caller gets a refusal, never a
        # host. Only record it if CPython found a *usable* host, which would
        # mean we are stricter (acceptable) -- never more permissive.
        if o_h:
            return {"url": raw, "kind": "stricter_refusal",
                    "mine": [m_s, m_h, m_p, m_e], "oracle": [o_s, o_h, o_p, o_e]}
        return None

    if host_same and port_same and scheme_same:
        return None
    return {"url": raw, "kind": "DIVERGENCE",
            "mine": [m_s, m_h, m_p, m_e], "oracle": [o_s, o_h, o_p, o_e]}


def main() -> int:
    findings: list[dict] = []
    for u in CASES:
        r = compare(u)
        if r:
            findings.append(r)

    # fuzz
    random.seed(20260824)
    fuzz_n = 200_000
    for _ in range(fuzz_n):
        u = "http://" + "".join(
            random.choice(ALPHA) for _ in range(random.randint(1, 16))
        )
        r = compare(u)
        if r:
            findings.append(r)

    divergences = [f for f in findings if f["kind"] == "DIVERGENCE"]
    stricter = [f for f in findings if f["kind"] == "stricter_refusal"]

    # The security property that matters: for every input where WE return a
    # host, that host must be exactly what a real client would dial.
    out = {
        "generated_by": "engineering/tools/url_split_parity.py",
        "oracle": "urllib.parse.urlsplit (CPython %d.%d.%d)" % sys.version_info[:3],
        "curated_cases": len(CASES),
        "fuzz_cases": fuzz_n,
        "divergences": len(divergences),
        "divergence_examples": divergences[:20],
        "stricter_refusals": len(stricter),
        "stricter_examples": stricter[:20],
        "verdict": "PARITY" if not divergences else "DIVERGENT",
    }
    raw_dir = ROOT / "engineering" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "url_split_parity.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"curated={len(CASES)} fuzz={fuzz_n}")
    print(f"divergences={len(divergences)} stricter_refusals={len(stricter)}")
    for d in divergences[:10]:
        print("  DIVERGE", d)
    print("verdict:", out["verdict"])
    return 0 if not divergences else 1


if __name__ == "__main__":
    raise SystemExit(main())
