"""
THE NAIVE RECHECK WRITE-BACK — committed on purpose, as a negative control.

This is the one-line P10 that ADR-038 rejected: probe the row, then write the
result back with `upsert_many`. It is kept in the tree, and exercised by
`test_recheck.py::TestLeaseClobber::test_the_naive_writeback_really_does_clobber`,
for the same reason `naive_store.py` is kept (P05.T3):

    a green test proves nothing unless the same assertion is shown to CATCH the
    broken implementation.

Without this file, the claim "the write-back cannot erase a live lease" would be
supported only by tests passing against my own code. With it, the difference
between safe and unsafe is observable in the suite, and if a future refactor
makes the naive path safe, the control fails and says so.

WHY THE NAIVE VERSION IS WRONG, PRECISELY

`upsert_many` sets EVERY column from the in-memory `Proxy`, including `state` and
`lease_id`. For a recheck, that object was loaded before the probe ran. So if a
consumer leases the row mid-probe, the write-back restores the pre-lease values:
`state='READY'`, `lease_id=NULL`. The consumer still holds the proxy, the pool
believes it is free, and `double_delivery_violations()` sees nothing, because no
second `LEASE` row was ever appended -- the violation was created by an
unconditional UPDATE, not by a faulty claim.

The fix (`SqliteStore.complete_probe`) makes the write conditional
(`WHERE ... AND state='PROBING'`) and narrows the SET list so a probe cannot
assert anything about leases at all.

DO NOT USE THIS OUTSIDE THE TEST SUITE.
"""
from __future__ import annotations

from datetime import datetime

from atlas.core.domain.proxy import Proxy, ProxyState
from atlas.core.domain.verdict import Grade


def naive_writeback(store, snapshot: Proxy, *, now: datetime) -> None:
    """
    What P10 would have been without ADR-038: probe, then unconditional upsert.

    `snapshot` is the row as it was loaded BEFORE the probe -- which is exactly
    what `plan()` hands a caller, and exactly what makes this unsafe.
    """
    probed = (snapshot.record_success(now)
              .with_state(ProxyState.READY, reason="OK")
              .graded(Grade.GOOD))
    store.upsert_many((probed,))


__all__ = ["naive_writeback"]
