"""
ATLAS PROXY FABRIC v4.

A rebuild of a legacy proxy scraper, driven by measurement rather than by opinion.
The layering is enforced by tests, not convention (atlas/tests/unit/test_architecture.py):

    core/      pure domain + policy + ports. No I/O, no network, no clock.
    adapters/  implement the ports (aiohttp, sqlite3, filesystem).
    engine/    orchestration: the probe pipeline and the scheduler.
    api/       HTTP surface.
    obs/       metrics and structured logs.
    cli/       operator entry points.

The single sentence that justifies the rewrite: the legacy system measured latency
59 times and never once compared it against a rejecting threshold, so it admitted a
p50 of 6 359.5 ms and a p95 of 15 903 ms (n=102, engineering/BASELINE.json).
LIVE != GOOD.
"""
__version__ = "4.0.0-dev"
