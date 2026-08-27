#!/usr/bin/env python3
"""P12 follow-up — probe NEW provider URLs and merge them into a NEW dated snapshot.

ADR-002: source URLs are data; this tool never writes one into a .py module.
ADR-006: per-host rate limiting, THROTTLED distinct from EMPTY.
ADR-010: never overwrites the pinned snapshot — writes source_probe_<UTC>.json.

Two providers were selected from live probing (2026-08-27): pubproxy.com
(JSON/txt API) and proxyhub.me (HTML table). Both hosts are absent from
engineering/raw/legacy_urls.json, so they extend the registry rather than
duplicate it.

Run: python3 engineering/tools/extend_source_registry.py
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
PINNED = ROOT / "engineering" / "raw" / "source_probe_20260824T010038Z.json"
OUT_DIR = ROOT / "engineering" / "raw"
LEGACY_URLS = OUT_DIR / "legacy_urls.json"

UA = "atlas-proxy-fabric/4.0 (source inventory audit; contact: operator)"
TIMEOUT_S = 25
BODY_CAP = 4 * 1024 * 1024
THROTTLE_BODY_BYTES = 2000

# The two new provider endpoints, as DATA (fetched, never hardcoded downstream).
NEW_ENDPOINTS = [
    "http://pubproxy.com/api/proxy?limit=20&format=txt&type=http",
    "https://proxyhub.me/en/all-http-proxy-list.html",
]

RX_ADJACENT = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\s*[:\s]\s*(\d{2,5})\b")


def valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def valid_port(port: str) -> bool:
    return port.isdigit() and 1 <= int(port) <= 65535


def parse_adjacent(body: str) -> set[str]:
    out = set()
    for ip, port in RX_ADJACENT.findall(body):
        if valid_ip(ip) and valid_port(port):
            out.add(f"{ip}:{port}")
    return out


def parse_json_walk(body: str) -> set[str]:
    out: set[str] = set()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return out

    def walk(node):
        if isinstance(node, dict):
            ips = [v for v in node.values() if isinstance(v, str) and valid_ip(v)]
            ports = [v for v in node.values()
                     if isinstance(v, (int, str)) and valid_port(str(v))]
            for ip in ips:
                for pt in ports:
                    out.add(f"{ip}:{pt}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def parse_html_table(body: str) -> set[str]:
    """Pair ip-ish and port-ish <td> cells row by row."""
    out: set[str] = set()
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S | re.I)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        ips = [c for c in cells if valid_ip(c)]
        ports = [c for c in cells if valid_port(c)]
        for ip in ips:
            for pt in ports:
                out.add(f"{ip}:{pt}")
    return out


async def probe_one(session: aiohttp.ClientSession, url: str) -> dict:
    fetched = datetime.now(timezone.utc).isoformat()
    host = urlparse(url).netloc
    loop = asyncio.get_event_loop()
    started = loop.time()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as resp:
            raw = await resp.read()
            truncated = len(raw) >= BODY_CAP
            body = raw[:BODY_CAP].decode("utf-8", errors="replace")
            elapsed = round((loop.time() - started) * 1000, 1)
            counts = {
                "regex_adjacent": len(parse_adjacent(body)),
                "json_path": len(parse_json_walk(body)),
                "html_table": len(parse_html_table(body)),
            }
            best = max(counts, key=lambda k: counts[k])
            unique = counts[best]
            if unique == 0 and len(body) < THROTTLE_BODY_BYTES:
                verdict = "THROTTLED_OR_SHORT"
                best_name = None
            elif unique == 0:
                verdict = "TRULY_EMPTY"
                best_name = None
            else:
                verdict = {"regex_adjacent": "ALIVE",
                           "json_path": "ALIVE_JSON",
                           "html_table": "ALIVE_HTML_TABLE"}[best]
                best_name = best
            return {
                "url": url,
                "host": host,
                "verdict": verdict,
                "elapsed_ms": elapsed,
                "fetched_at_utc": fetched,
                "http_status": resp.status,
                "bytes": len(raw),
                "parsed": counts,
                "best_parser": best_name,
                "unique_candidates": unique,
                "recovered_by_structured_parser": best_name in {"json_path", "html_table"},
                "body_bytes": len(raw),
                "content_length": resp.content_length,
                "content_encoding": resp.headers.get("Content-Encoding"),
                "length_comparable": True,
                "content_type": resp.headers.get("Content-Type"),
                "short_read": len(body) < THROTTLE_BODY_BYTES,
                "body_truncated_at_cap": truncated,
            }
    except Exception as exc:  # transport failure -> DEAD, never silent
        return {
            "url": url,
            "host": host,
            "verdict": "DEAD",
            "elapsed_ms": round((loop.time() - started) * 1000, 1),
            "fetched_at_utc": fetched,
            "http_status": None,
            "bytes": 0,
            "parsed": {"regex_adjacent": 0, "json_path": 0, "html_table": 0},
            "best_parser": None,
            "unique_candidates": 0,
            "recovered_by_structured_parser": False,
            "body_bytes": 0,
            "content_length": None,
            "content_encoding": None,
            "length_comparable": False,
            "content_type": None,
            "short_read": True,
            "body_truncated_at_cap": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def main() -> int:
    legacy = json.loads(LEGACY_URLS.read_text())
    known = set(legacy["unique_urls"])
    for u in NEW_ENDPOINTS:
        if u in known:
            print(f"FATAL: {u} is already in legacy_urls.json — refusing to duplicate")
            return 1

    connector = aiohttp.TCPConnector(limit=4, limit_per_host=2)  # ADR-006
    async with aiohttp.ClientSession(
            headers={"User-Agent": UA}, connector=connector) as session:
        new_results = []
        for url in NEW_ENDPOINTS:
            r = await probe_one(session, url)
            print(f"  {r['verdict']:20s} {r['unique_candidates']:>5d} cand  {url}")
            new_results.append(r)
            await asyncio.sleep(1.0)  # ADR-006: be polite even across hosts

    pinned = json.loads(PINNED.read_text())
    results = pinned["results"] + new_results
    verdicts: dict[str, int] = {}
    for r in results:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = {
        "task": "P12-provider-extension",
        "generator": "engineering/tools/extend_source_registry.py",
        "extends_snapshot": PINNED.name,
        "network_used": True,
        "measured_at_utc": stamp,
        "note": ("120 rows carried verbatim from the pinned snapshot (state at "
                 "2026-08-24T01:00:38Z); 2 rows freshly probed. Each row keeps its "
                 "own fetched_at_utc — the registry already documents that state "
                 "is per-row, per-moment."),
        "urls_probed": len(results),
        "verdicts": verdicts,
        "total_unique_candidates": sum(r.get("unique_candidates", 0) for r in results),
        "results": results,
    }
    out = OUT_DIR / f"source_probe_{stamp}.json"
    out.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n")
    print(f"-> {out.relative_to(ROOT)}  ({len(results)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
