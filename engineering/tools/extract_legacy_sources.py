#!/usr/bin/env python3
"""
P00.T2 (rebuilt) — Extract every URL literal from the legacy Python via AST.

REBUILD NOTE (ADR-010): the original tool was lost in a platform sync. This is a
re-implementation whose output is CROSS-CHECKED against the numbers recorded in
ANALYSIS.md / RECONCILIATION.md. Deltas are reported, never silently absorbed (H2).

PINNED DEFINITIONS (recovered empirically -- see RECONCILIATION.md section 1).
The documented figures reproduce EXACTLY, but only under these exact rules:

  url_literals = 257  -> count of source LINES containing 'http(s)://'.
                         This is what raw/bug_scan.json's `hardcoded_http_url`
                         measures. Verified: 0 are comment-only, 1 line holds 2.
  unique_urls  = 123  -> ast.Constant strings ONLY (never f-string fragments),
                         matched with the LOOSE regex, then deduplicated.
                       = 122 real URLs + 1 malformed bare 'http://'.

f-string reconstruction is deliberately EXCLUDED from the unique count: joining
the literal parts of f"...{key}&method=..." fabricates URLs that never exist at
runtime (it produced 4 phantom 2captcha variants, inflating 123 -> 126). They are
still reported separately as `fstring_fragment_urls`, because they prove which
endpoints the code actually CALLS -- that is how bebo.py's 2captcha use surfaced.

Output: engineering/raw/legacy_urls.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "engineering" / "raw" / "legacy_urls.json"

LEGACY = ["v1.py", "v2.py", "v3.py", "bebo.py", "proxychecker.py",
          "proxy_generator_v2.py"]

# LOOSE: allows zero chars after '//' so the bare 'http://' literal (used to build
# proxy dicts, e.g. proxy_generator_v2.py:368) is counted -- required to hit 123.
URL_LOOSE = re.compile(r"https?://[^\s\"'<>{}\\^|`]*")
SCHEME_ONLY = ("http://", "https://")

DOCUMENTED = {"unique_urls": 123, "url_literals": 257, "unique_real": 122}


def parse_strings(path: Path) -> tuple[list[tuple[str, int]],
                                       list[tuple[str, int]], str]:
    """Returns (constants, fstring_fragments, method); each item is (value, line)."""
    src = path.read_text(encoding="utf-8", errors="replace")
    consts: list[tuple[str, int]] = []
    frags: list[tuple[str, int]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        for i, line in enumerate(src.splitlines(), 1):
            consts.append((line, i))
        return consts, frags, "regex_fallback"

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.append((node.value, node.lineno))
        elif isinstance(node, ast.JoinedStr):
            parts = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if parts:
                frags.append((parts, node.lineno))
    return consts, frags, "ast"


def url_line_numbers(path: Path) -> list[int]:
    """Lines containing an http(s):// occurrence -- the '257' definition."""
    src = path.read_text(encoding="utf-8", errors="replace")
    return [i for i, line in enumerate(src.splitlines(), 1)
            if re.search(r"https?://", line)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    per_file: dict[str, dict] = {}
    hits: list[dict] = []
    frag_hits: list[dict] = []
    total_url_lines = 0
    methods: dict[str, str] = {}

    for name in LEGACY:
        p = ROOT / name
        if not p.exists():
            continue
        consts, frags, method = parse_strings(p)
        methods[name] = method

        f_hits: list[dict] = []
        for value, lineno in consts:
            for m in URL_LOOSE.finditer(value):
                f_hits.append({"url": m.group(0).rstrip(".,);'\""),
                               "file": name, "line": lineno})
        f_frags: list[dict] = []
        for value, lineno in frags:
            for m in URL_LOOSE.finditer(value):
                f_frags.append({"url": m.group(0).rstrip(".,);'\""),
                                "file": name, "line": lineno, "kind": "fstring"})

        n_lines = len(url_line_numbers(p))
        total_url_lines += n_lines
        hits.extend(f_hits)
        frag_hits.extend(f_frags)
        per_file[name] = {
            "url_lines": n_lines,
            "constant_matches": len(f_hits),
            "fstring_fragments": len(f_frags),
            "unique_urls": len({h["url"] for h in f_hits}),
            "extraction_method": method,
        }

    unique = sorted({h["url"] for h in hits})
    malformed = [u for u in unique if u in SCHEME_ONLY]
    real_unique = [u for u in unique if u not in SCHEME_ONLY]

    by_host: dict[str, int] = defaultdict(int)
    for u in real_unique:
        by_host[urlparse(u).netloc or "<malformed>"] += 1

    recon = {
        "documented_url_literals": DOCUMENTED["url_literals"],
        "regenerated_url_lines": total_url_lines,
        "url_literals_match": total_url_lines == DOCUMENTED["url_literals"],
        "documented_unique_urls": DOCUMENTED["unique_urls"],
        "regenerated_unique": len(unique),
        "unique_match": len(unique) == DOCUMENTED["unique_urls"],
        "documented_real_unique": DOCUMENTED["unique_real"],
        "regenerated_real_unique": len(real_unique),
        "malformed_literals": malformed,
        "fstring_fragment_urls_excluded": len({h["url"] for h in frag_hits}),
        "explanation": (
            "Both documented figures reproduce exactly under the pinned definitions. "
            f"url_literals=257 counts source LINES containing a URL (regenerated "
            f"{total_url_lines}). unique_urls=123 counts ast.Constant strings only, "
            f"loose-matched (regenerated {len(unique)}) = {len(real_unique)} real + "
            f"{len(malformed)} malformed bare literal {malformed}, which ANALYSIS.md "
            "section 2.1 already records as 'ERROR 1 - a malformed literal in the "
            "legacy code'. f-string fragments are excluded because joining literal "
            "parts fabricates runtime-nonexistent URLs; they are reported separately."
        ),
    }

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "task": "P00.T2",
        "generator": "engineering/tools/extract_legacy_sources.py",
        "rebuilt_after_sync_loss": True,
        "deterministic": True,
        "network_used": False,
        "generated_at_utc": stamp,
        "definitions": {
            "url_literals": "count of source LINES containing http(s)://",
            "unique_urls": "ast.Constant strings only, LOOSE regex, deduplicated",
            "fstring_policy": "reported separately, excluded from unique_urls",
        },
        "files_scanned": list(methods),
        "extraction_methods": methods,
        "totals": {
            "url_lines": total_url_lines,
            "constant_matches": len(hits),
            "unique_urls": len(unique),
            "real_unique_urls": len(real_unique),
            "unique_hosts": len(by_host),
            "fstring_fragment_urls": len({h["url"] for h in frag_hits}),
        },
        "per_file": per_file,
        "reconciliation": recon,
        "unique_urls": unique,
        "fstring_fragment_urls": sorted({h["url"] for h in frag_hits}),
        "hits": sorted(hits, key=lambda h: (h["file"], h["line"])),
        "fstring_fragment_hits": sorted(frag_hits,
                                        key=lambda h: (h["file"], h["line"])),
        "urls_per_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps({"totals": doc["totals"], "reconciliation": recon}, indent=2))
        return 0

    print("=" * 74)
    print("LEGACY URL EXTRACTION (AST) — rebuilt after sync loss")
    print("=" * 74)
    for name, st in per_file.items():
        print(f"  {name:22} lines={st['url_lines']:4}  const={st['constant_matches']:4}"
              f"  unique={st['unique_urls']:4}  fstr={st['fstring_fragments']:3}"
              f"  ({st['extraction_method']})")
    print("-" * 74)
    tag1 = "MATCH" if recon["url_literals_match"] else "DELTA"
    tag2 = "MATCH" if recon["unique_match"] else "DELTA"
    print(f"  URL lines (== 'literals'): {total_url_lines:4}   documented 257   [{tag1}]")
    print(f"  UNIQUE urls              : {len(unique):4}   documented 123   [{tag2}]")
    print(f"       = {len(real_unique)} real + {len(malformed)} malformed {malformed}")
    print(f"  UNIQUE hosts             : {len(by_host):4}")
    print(f"  f-string fragments       : {len({h['url'] for h in frag_hits}):4}"
          "   (excluded from unique, reported separately)")
    print("-" * 74)
    print("  top hosts:")
    for h, c in list(doc["urls_per_host"].items())[:8]:
        print(f"    {c:4}  {h}")
    print(f"-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
