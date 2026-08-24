#!/usr/bin/env python3
"""
P00.T3/T4/T5 (rebuilt) — Actually fetch the legacy source URLs and record real state.

REBUILD NOTE (ADR-010): the originals were lost in a platform sync. A past network
read CANNOT be reproduced, so this writes a NEW DATED SNAPSHOT and never overwrites
engineering/SOURCE_INVENTORY.json (which survived and stays as the 2026-08-23 record).

Implements the three lessons Phase 0 paid for:

  T3  fetch every URL, never trust a comment in the old code.
  T4  the naive legacy regex (`ip` and `port` ADJACENT) under-detects: JSON APIs put
      them in separate keys and HTML tables in separate <td>. So each body is parsed
      by THREE strategies and the best result wins -- this is what recovered 6 live
      sources a regex-only audit would have discarded.
  T5  ADR-006: per-host rate limiting + a THROTTLED reason-code distinct from EMPTY.
      One short body must never mark a source dead -- that misclassification is
      exactly how the GeoNode API (230KB of valid JSON) got filed as TRULY_EMPTY.

Usage:
  probe_legacy_sources.py                  # all URLs from raw/legacy_urls.json
  probe_legacy_sources.py --only geonode   # substring filter
  probe_legacy_sources.py --limit 20
Output: engineering/raw/source_probe_<UTC>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "engineering" / "raw"
URLS = OUT_DIR / "legacy_urls.json"
INVENTORY = ROOT / "engineering" / "SOURCE_INVENTORY.json"

CONCURRENCY = 12
PER_HOST_CONCURRENCY = 2          # ADR-006: we stop causing our own 429s
PER_HOST_DELAY_S = 1.0            # ADR-006
TIMEOUT_S = 20
BODY_CAP = 4 * 1024 * 1024        # 4 MB
THROTTLE_BODY_BYTES = 2000        # below this + 0 parsed => THROTTLED, not EMPTY

UA = "atlas-proxy-fabric/4.0 (source inventory audit; contact: operator)"

# The legacy regex: ip and port ADJACENT. Kept verbatim to demonstrate its blind spot.
RX_ADJACENT = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\s*[:\s]\s*(\d{2,5})\b")
RX_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def valid_port(port: str) -> bool:
    return port.isdigit() and 1 <= int(port) <= 65535


# ── three parsing strategies (T4) ─────────────────────────────────────────────
def parse_adjacent(body: str) -> set[str]:
    """Strategy 1 -- what the legacy code did."""
    out = set()
    for ip, port in RX_ADJACENT.findall(body):
        if valid_ip(ip) and valid_port(port):
            out.add(f"{ip}:{port}")
    return out


def parse_json_walk(body: str) -> set[str]:
    """Strategy 2 -- recursive walk pairing ip-ish and port-ish keys."""
    out: set[str] = set()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return out

    ip_keys = ("ip", "host", "address", "addr", "proxy", "ipaddress", "ip_address")
    port_keys = ("port", "portnumber", "port_number")

    def walk(node) -> None:
        if isinstance(node, dict):
            low = {str(k).lower().replace("-", "").replace("_", ""): v
                   for k, v in node.items()}
            ip = next((low[k.replace("_", "")] for k in ip_keys
                       if k.replace("_", "") in low), None)
            port = next((low[k.replace("_", "")] for k in port_keys
                         if k.replace("_", "") in low), None)
            if ip is not None and port is not None:
                s_ip, s_port = str(ip).strip(), str(port).strip()
                if valid_ip(s_ip) and valid_port(s_port):
                    out.add(f"{s_ip}:{s_port}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def parse_html_table(body: str) -> set[str]:
    """Strategy 3 -- cell-pair scan; ip and port in separate <td>."""
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


def classify(status: int | None, body: str, err: str | None,
             fetch_meta: dict | None = None) -> tuple[str, dict]:
    """Returns (verdict, parse detail). THROTTLED is distinct from EMPTY (ADR-006)."""
    fetch_meta = fetch_meta or {}
    if err:
        return "ERROR", {"error": err, **fetch_meta}
    if status is None:
        return "ERROR", {"error": "no status", **fetch_meta}
    if status != 200:
        return "DEAD", {"http_status": status, **fetch_meta}

    adj = parse_adjacent(body)
    js = parse_json_walk(body)
    tbl = parse_html_table(body)
    best_name, best = max(
        (("regex_adjacent", adj), ("json_path", js), ("html_table", tbl)),
        key=lambda kv: len(kv[1]),
    )
    detail = {
        "http_status": status,
        "bytes": len(body),
        "parsed": {"regex_adjacent": len(adj), "json_path": len(js),
                   "html_table": len(tbl)},
        "best_parser": best_name,
        "unique_candidates": len(best),
        "recovered_by_structured_parser": bool(best) and not adj,
        **fetch_meta,
    }
    if best:
        verdict = {"regex_adjacent": "ALIVE", "json_path": "ALIVE_JSON",
                   "html_table": "ALIVE_HTML_TABLE"}[best_name]
        return verdict, detail

    # ── ADR-013(c)/(d): nothing parsed. Before blaming the SOURCE, rule out OUR
    # OWN fetch. An incomplete body is our fault and must be re-fetched, never
    # recorded as an empty source -- that error has already misclassified the
    # GeoNode API twice, from two different causes.
    if fetch_meta.get("short_read") or fetch_meta.get("body_truncated_at_cap"):
        return "FETCH_INCOMPLETE", detail
    ctype = (fetch_meta.get("content_type") or "").lower()
    if "json" in ctype:
        # served as JSON but nothing walked out of it => the bytes are suspect,
        # not the source.
        try:
            json.loads(body)
        except (json.JSONDecodeError, ValueError):
            detail["json_parse_failed"] = True
            return "FETCH_INCOMPLETE", detail

    # genuinely empty vs throttled
    if len(body) < THROTTLE_BODY_BYTES:
        return "THROTTLED_OR_SHORT", detail
    return "TRULY_EMPTY", detail


async def fetch(session, url: str, host_locks, host_last) -> dict:
    import aiohttp

    lock = host_locks[urlparse(url).netloc]
    async with lock:                                    # ADR-006 per-host limit
        host = urlparse(url).netloc
        wait = PER_HOST_DELAY_S - (time.monotonic() - host_last.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        t0 = time.monotonic()
        status = None
        body = ""
        err = None
        fetch_meta: dict = {}
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                                   headers={"User-Agent": UA},
                                   allow_redirects=True) as resp:
                status = resp.status
                # ADR-013(a): read to EOF. `resp.content.read(n)` returns only what
                # is CURRENTLY BUFFERED, up to n -- not n bytes -- so it silently
                # truncates large bodies and a live source then looks empty.
                chunks: list[bytes] = []
                total = 0
                for_truncated = False
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= BODY_CAP:
                        for_truncated = True
                        break
                raw = b"".join(chunks)
                body = raw.decode("utf-8", errors="replace")
                # ADR-013(b): record declared length so a short read is PROVABLE
                # from the artifact rather than inferred later.
                declared = resp.headers.get("Content-Length")
                content_length = int(declared) if (declared or "").isdigit() else None
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
                body_bytes = len(raw)
                # HONESTY NOTE: aiohttp transparently decompresses, so for a gzip/br
                # response `Content-Length` is the COMPRESSED size and is NOT
                # comparable to len(raw). Measured example: TheSpeedX/http.txt
                # reported Content-Length 20360 while the decoded body was 54 284
                # bytes. Comparing them would be meaningless, so the check is only
                # applied to identity-encoded responses and the reason is recorded.
                comparable = content_length is not None and encoding in ("", "identity")
                short_read = bool(
                    comparable and not for_truncated and body_bytes < content_length
                )
                fetch_meta = {
                    "body_bytes": body_bytes,
                    "content_length": content_length,
                    "content_encoding": encoding or None,
                    "length_comparable": comparable,
                    "content_type": content_type,
                    "short_read": short_read,
                    "body_truncated_at_cap": for_truncated,
                }
        except asyncio.TimeoutError:
            err = "timeout"
        except (aiohttp.ClientError, OSError, UnicodeError) as exc:
            # NOT silent (BUG_LEDGER B-02): the reason is recorded on the record.
            err = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            host_last[host] = time.monotonic()

        verdict, detail = classify(status, body, err, fetch_meta)
        return {"url": url, "host": host, "verdict": verdict,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                **detail}


async def run(urls: list[str]) -> list[dict]:
    import aiohttp

    host_locks: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(PER_HOST_CONCURRENCY))
    host_last: dict[str, float] = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY, ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def one(u: str) -> dict:
            async with sem:
                return await fetch(session, u, host_locks, host_last)
        return await asyncio.gather(*(one(u) for u in urls))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="substring filter on the URL")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not URLS.exists():
        print("run extract_legacy_sources.py first (raw/legacy_urls.json missing)")
        return 1
    doc = json.loads(URLS.read_text(encoding="utf-8"))
    urls = [u for u in doc["unique_urls"]
            if u not in ("http://", "https://")            # skip the malformed literal
            and not u.startswith("http://2captcha.com")]   # H5/ADR-007: never probe
    if args.only:
        urls = [u for u in urls if args.only in u]
    if args.limit:
        urls = urls[:args.limit]

    print(f"probing {len(urls)} URLs "
          f"(concurrency {CONCURRENCY}, per-host {PER_HOST_CONCURRENCY} "
          f"+ {PER_HOST_DELAY_S}s delay, timeout {TIMEOUT_S}s)")
    t0 = time.time()
    results = asyncio.run(run(urls))
    wall = time.time() - t0

    by_verdict: dict[str, int] = defaultdict(int)
    for r in results:
        by_verdict[r["verdict"]] += 1
    total_candidates = sum(r.get("unique_candidates", 0) for r in results)
    recovered = [r for r in results if r.get("recovered_by_structured_parser")]
    dead_status: dict[str, int] = defaultdict(int)
    for r in results:
        if r["verdict"] == "DEAD":
            dead_status[f"http_{r.get('http_status')}"] += 1
        elif r["verdict"] == "ERROR":
            dead_status[str(r.get("error", "error"))[:40]] += 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {
        "task": "P00.T3/T4/T5",
        "generator": "engineering/tools/probe_legacy_sources.py",
        "rebuilt_after_sync_loss": True,
        "snapshot_not_reproduction": True,
        "network_used": True,
        "measured_at_utc": stamp,
        "note": ("A past network read cannot be reproduced. This is a NEW snapshot; "
                 "engineering/SOURCE_INVENTORY.json is retained unchanged as the "
                 "2026-08-23 record. Comparison is point-in-time, not a diff of truth."),
        "params": {"concurrency": CONCURRENCY,
                   "per_host_concurrency": PER_HOST_CONCURRENCY,
                   "per_host_delay_s": PER_HOST_DELAY_S,
                   "timeout_s": TIMEOUT_S,
                   "excluded": "2captcha.com (H5/ADR-007), malformed bare scheme"},
        "wall_clock_s": round(wall, 1),
        "urls_probed": len(urls),
        "verdicts": dict(sorted(by_verdict.items(), key=lambda kv: -kv[1])),
        "total_unique_candidates": total_candidates,
        "recovered_by_structured_parser": len(recovered),
        "recovered_detail": [
            {"url": r["url"], "parser": r["best_parser"],
             "candidates": r["unique_candidates"]}
            for r in sorted(recovered, key=lambda r: -r["unique_candidates"])
        ],
        "failure_breakdown": dict(sorted(dead_status.items(), key=lambda kv: -kv[1])),
        "results": sorted(results, key=lambda r: (r["verdict"], r["url"])),
    }
    dest = OUT_DIR / f"source_probe_{stamp}.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("=" * 74)
    print(f"SOURCE PROBE SNAPSHOT — {len(urls)} URLs in {wall:.1f}s")
    print("=" * 74)
    for v, c in out["verdicts"].items():
        print(f"  {v:22} {c}")
    print("-" * 74)
    print(f"  total unique candidates      : {total_candidates}")
    print(f"  recovered by structured parse: {len(recovered)}  "
          "(a regex-only audit would have discarded these)")
    for r in out["recovered_detail"][:8]:
        print(f"      {r['candidates']:5}  {r['parser']:12}  {r['url'][:58]}")
    if out["failure_breakdown"]:
        print("  failure breakdown:")
        for k, c in list(out["failure_breakdown"].items())[:8]:
            print(f"      {c:4}  {k}")
    if INVENTORY.exists():
        inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
        print("-" * 74)
        print("  documented 2026-08-23 snapshot (retained, NOT overwritten):")
        print(f"      ALIVE 56 · DEAD 35 · EMPTY 25 · ERROR 1 · NOT_A_SOURCE 6")
        print(f"      today's snapshot is a separate point-in-time reading")
    print(f"-> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
