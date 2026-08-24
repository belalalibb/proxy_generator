"""
NaiveStore — a deliberately WRONG lease(), used as the negative control for H3.

This is not dead code and it is not a mistake. It is the read-then-write pattern
the legacy design implied, and it exists so the concurrency test can be shown to
have teeth: a test that passes for the real store must FAIL for this one, or it
is not testing concurrency at all -- it is testing that the machine was idle.

ADR-010: a test that cannot fail is not evidence. The trap with concurrency tests
specifically is that they are timing-dependent, so an ineffective test looks
identical to a passing one. Running the SAME test body against a known-broken
implementation is the only way to tell those apart.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path


class NaiveStore:
    """SELECT, then UPDATE, in two statements -- as legacy consumers of proxy.txt
    effectively did (BUG_LEDGER B-05)."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None,
                                   timeout=busy_timeout_ms / 1000.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

    def lease_naive(self, *, count: int, now: datetime,
                    gap_s: float = 0.0) -> tuple[str, ...]:
        """
        The bug, in its natural form.

        `gap_s` widens the window between reading and claiming. It does not create
        the defect -- it makes an inherently timing-dependent defect reliably
        observable, so the negative control is not itself flaky. The race exists
        at gap_s=0; it is simply rarer.
        """
        rows = self._db.execute(
            "SELECT fingerprint FROM proxies WHERE state='READY' "
            "ORDER BY fingerprint LIMIT ?", (count,)
        ).fetchall()
        picked = [r["fingerprint"] for r in rows]
        if gap_s:
            time.sleep(gap_s)
        lease_id = uuid.uuid4().hex
        got: list[str] = []
        for fp in picked:
            # No state re-check in the WHERE clause: this claims the row whether
            # or not another process already took it.
            self._db.execute(
                "UPDATE proxies SET state='LEASED', lease_id=? WHERE fingerprint=?",
                (lease_id, fp),
            )
            self._db.execute(
                "INSERT INTO lease_log (lease_id, fingerprint, event, at) "
                "VALUES (?,?, 'LEASE', ?)",
                (lease_id, fp, now.isoformat()),
            )
            got.append(fp)
        return tuple(got)

    def close(self) -> None:
        self._db.close()
