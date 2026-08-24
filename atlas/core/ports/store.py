"""
StorePort — persistence and the lease protocol.

This interface carries the two invariants the legacy text file could not provide:

H3 (NO DOUBLE DELIVERY). `lease()` must be a single atomic compare-and-set --
    BEGIN IMMEDIATE; UPDATE ... WHERE state='READY' ... RETURNING
    -- never read-then-write. proxy.txt had no LEASED state at all, so two
    concurrent consumers were always handed the same line (BUG_LEDGER B-05).

H8 (CRASH RESUME). SQLite in WAL mode, and every derived text export written
    .tmp then os.replace(). The legacy save() used open(...,'w') in 8 places:
    SIGKILL mid-write left a truncated or empty working set (B-04).
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from atlas.core.domain.proxy import Proxy, ProxyState
from atlas.core.domain.verdict import Grade


@runtime_checkable
class StorePort(Protocol):
    """Implemented in adapters/ over SQLite (WAL). core/ never imports sqlite3."""

    # ── pool membership ───────────────────────────────────────────────────────
    def upsert(self, proxy: Proxy) -> None:
        """Insert or update by fingerprint. Must be idempotent."""
        ...

    def upsert_many(self, proxies: tuple[Proxy, ...]) -> int:
        """Bulk upsert in ONE transaction; returns the number newly inserted."""
        ...

    def get(self, fingerprint: str) -> Proxy | None:
        ...

    def count_by_state(self) -> dict[ProxyState, int]:
        ...

    # ── the H3 guarantee ──────────────────────────────────────────────────────
    def lease(self, *, count: int, min_grade: Grade, lease_ms: int,
              now: datetime) -> tuple[Proxy, ...]:
        """
        Atomically move up to `count` proxies from READY to LEASED and return them.

        MUST be a single compare-and-set statement. A read-then-write
        implementation violates H3 under concurrency and will be rejected by the
        integration test that runs parallel leases and asserts zero overlap.
        """
        ...

    def release(self, fingerprint: str, *, now: datetime) -> None:
        """Return a leased proxy to READY."""
        ...

    def expire_leases(self, *, now: datetime) -> int:
        """
        Reclaim leases past their deadline; returns how many were reclaimed.

        Without this, a consumer that crashes while holding a lease removes the
        proxy from the pool permanently -- a slow leak that the legacy design
        could not even represent.
        """
        ...

    # ── durability ────────────────────────────────────────────────────────────
    def export_text(self, path: str, *, min_grade: Grade) -> int:
        """
        Write a derived text file ATOMICALLY (.tmp then os.replace) and return the
        number of lines. The text file is an export, never the source of truth
        (ADR-004).
        """
        ...

    def checkpoint(self) -> None:
        """Flush the WAL so a SIGKILL cannot lose acknowledged writes (H8)."""
        ...
