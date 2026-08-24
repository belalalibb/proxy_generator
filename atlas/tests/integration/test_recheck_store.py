"""
RECHECK against the REAL store, under REAL process concurrency (ADR-038).

WHY THIS FILE EXISTS SEPARATELY FROM test_recheck.py

The unit suite already drives a real `SqliteStore`, so this is not "the same
tests with a database". What it adds is CONTENTION BETWEEN PROCESSES, and that
is the only setting in which a claim can be shown to work at all:

  * `claim_for_probe` is a compare-and-set. A read-then-write claim passes every
    single-threaded test -- P05.T3 established exactly that for `lease()`, and
    the reasoning transfers unchanged.
  * Threads would not do. CPython's GIL serialises enough of the Python-level
    work to hide the interleaving that matters, so these are `spawn`ed processes
    with independent sqlite connections.

THREE PROPERTIES, EACH TIED TO A MEASURED DEFECT IN recheck_gap.json

  1. N workers claiming one pool never claim a row twice. (`double_probe`
     measured two consecutive passes both selecting `3fd692f1f03a4fe8`.)
  2. A claim and a lease cannot both hold the same row. This is the clobber
     (`lease_clobber` -> CLOBBERED), and it is checked from BOTH directions,
     because a one-directional guard would pass while the opposite order
     silently overlapped.
  3. A worker SIGKILLed mid-probe strands nothing. `PROBING` must not become the
     new absorbing state ADR-036 removed, and SIGKILL is the only honest way to
     prove it: signal 9 is uncatchable, so no `finally`, `atexit` or `__exit__`
     can run, and the test therefore measures the STORE's recovery rather than
     my shutdown code. `assert returncode == -9` is what pins that.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.verdict import Grade

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
STALE = NOW - timedelta(hours=2)
REPO_ROOT = Path(__file__).resolve().parents[3]


def _seed(store: SqliteStore, n: int, *,
          state: ProxyState = ProxyState.READY) -> tuple[str, ...]:
    """n rows, all long overdue for a recheck, deterministic p95 for ordering."""
    batch = []
    for i in range(n):
        ep = Endpoint(host=f"10.{i // 250}.{(i // 250) % 256}.{i % 250 + 1}",
                      port=8080)
        lat = 100.0 + i
        batch.append(Proxy(
            endpoint=ep, protocol=Protocol.HTTP, state=state, grade=Grade.GOOD,
            latency=LatencyProfile(samples_ms=(lat,), p50_ms=lat, p95_ms=lat,
                                   mean_ms=lat, success_ratio=1.0),
            anonymity=Anonymity.ANONYMOUS,
            last_checked=STALE, first_seen=STALE, source_id="itest",
        ))
    store.upsert_many(tuple(batch))
    return tuple(p.fingerprint for p in batch)


# ── 1. concurrent claims never overlap ───────────────────────────────────────
def _claim_worker(db_path: str, fps: list[str], out: mp.Queue) -> None:
    """A separate PROCESS with its own sqlite connection -- not a thread."""
    store = SqliteStore(db_path)
    try:
        claimed = store.claim_for_probe(tuple(fps), now=NOW, probe_ms=60_000)
        out.put([p.fingerprint for p in claimed])
    finally:
        store.close()


@pytest.mark.parametrize("procs,pool", [(8, 20), (12, 6)])
def test_concurrent_claims_never_overlap(tmp_path: Path, procs: int,
                                         pool: int) -> None:
    """
    N processes all try to claim the SAME rows; no fingerprint may be claimed twice.

    The second parametrisation OVER-SUBSCRIBES hard (12 processes contending for
    6 rows). Contention is where a read-then-write claim breaks, so an
    under-subscribed run would be easier and prove less -- the same reasoning as
    the P05 lease test's 48-requested-from-24.
    """
    db = tmp_path / "pool.db"
    with SqliteStore(db) as store:
        fps = _seed(store, pool)

    ctx = mp.get_context("spawn")          # spawn: no inherited sqlite handles
    q: mp.Queue = ctx.Queue()
    workers = [ctx.Process(target=_claim_worker, args=(str(db), list(fps), q))
               for _ in range(procs)]
    for w in workers:                      # start all before joining any,
        w.start()                          # so they actually overlap
    results = [q.get(timeout=60) for _ in range(procs)]
    for w in workers:
        w.join(timeout=60)
        assert w.exitcode == 0, f"worker died: exitcode={w.exitcode}"

    claimed = [fp for r in results for fp in r]
    assert len(claimed) == len(set(claimed)), (
        f"DOUBLE PROBE: {len(claimed) - len(set(claimed))} row(s) claimed twice "
        f"across {procs} processes -- each duplicate is a k=5 probe paid for "
        "twice to answer one question"
    )
    assert len(claimed) == pool, (
        "every row should end up claimed exactly once by exactly one worker"
    )
    with SqliteStore(db) as store:
        assert store.count_by_state().get(ProxyState.PROBING) == pool


# ── 2. a claim and a lease cannot both hold one row ──────────────────────────
def test_a_claimed_row_cannot_be_leased(tmp_path: Path) -> None:
    """Claim first, then try to lease: the consumer must NOT get the row."""
    db = tmp_path / "claim_then_lease.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)
        assert len(store.claim_for_probe(fps, now=NOW, probe_ms=60_000)) == 1
        assert store.lease(count=5, min_grade=Grade.USABLE, lease_ms=60_000,
                           now=NOW) == (), (
            "a row being probed is not READY, so lease() must not see it"
        )


def test_a_leased_row_cannot_be_claimed(tmp_path: Path) -> None:
    """
    The other direction -- the one that actually produced the measured clobber.

    It began exactly here: a READY row selected for recheck was leased in the
    meantime. A guard covering only claim-then-lease would pass while this order
    silently overlapped.
    """
    db = tmp_path / "lease_then_claim.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)
        leased = store.lease(count=1, min_grade=Grade.USABLE, lease_ms=60_000,
                             now=NOW)
        assert len(leased) == 1
        assert store.claim_for_probe(fps, now=NOW, probe_ms=60_000) == ()
        row = store.get(fps[0])
        assert row.state is ProxyState.LEASED
        assert row.lease_id == leased[0].lease_id


def _racer(db_path: str, mode: str, fps: list[str], out: mp.Queue) -> None:
    """Two kinds of process race: one claims for probe, one leases for a consumer."""
    store = SqliteStore(db_path)
    try:
        if mode == "claim":
            got = store.claim_for_probe(tuple(fps), now=NOW, probe_ms=60_000)
        else:
            got = store.lease(count=len(fps), min_grade=Grade.USABLE,
                              lease_ms=60_000, now=NOW)
        out.put((mode, [p.fingerprint for p in got]))
    finally:
        store.close()


def test_a_claim_and_a_lease_never_hold_the_same_row(tmp_path: Path) -> None:
    """
    Real race, real processes: no row may appear in both outcomes.

    This is the property H3's own audit could not detect. If a row were both
    claimed and leased, `double_delivery_violations()` would STILL report
    nothing -- only one LEASE was ever appended -- so the assertion has to
    compare the two result sets directly. That asymmetry is the ADR-038 finding:
    an audit watching one mechanism is blind to a violation produced by another.
    """
    db = tmp_path / "race.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 12)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    modes = ["claim", "lease"] * 5
    workers = [ctx.Process(target=_racer, args=(str(db), m, list(fps), q))
               for m in modes]
    for w in workers:
        w.start()
    results = [q.get(timeout=60) for _ in modes]
    for w in workers:
        w.join(timeout=60)
        assert w.exitcode == 0, f"worker died: exitcode={w.exitcode}"

    claimed = {fp for kind, r in results if kind == "claim" for fp in r}
    leased = {fp for kind, r in results if kind == "lease" for fp in r}
    overlap = claimed & leased
    assert not overlap, (
        f"{len(overlap)} row(s) were BOTH claimed for probe and leased to a "
        "consumer. That is the ADR-038 clobber: the probe's write-back would "
        "erase the consumer's lease, and lease_log would show nothing."
    )
    with SqliteStore(db) as store:
        assert store.double_delivery_violations() == ()


# ── 3. SIGKILL mid-probe strands nothing ─────────────────────────────────────
_KILL_CHILD = r"""
import os, signal, sys
from datetime import datetime, timezone
from atlas.adapters.store_sqlite import SqliteStore

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
store = SqliteStore(sys.argv[1])
claimed = store.claim_for_probe((sys.argv[2],), now=NOW, probe_ms=60_000)
assert len(claimed) == 1, "child failed to claim"
print("CLAIMED", flush=True)
# SIGKILL: uncatchable, so no finally / atexit / __exit__ can release the claim.
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_a_probe_killed_mid_flight_is_reclaimed_not_stranded(
        tmp_path: Path) -> None:
    """
    H8 on the probe path.

    A child claims a row and is SIGKILLed while "probing". Asserting
    `returncode == -9` is what makes this a test of the STORE's recovery rather
    than of my cleanup code: signal 9 cannot be caught, so nothing in the child
    ran after the claim.

    Without `reclaim_stale_probes` this row sits in `PROBING` forever --
    `decide()` calls it IN_FLIGHT and `lease()` only sees READY -- which is
    ADR-036's absorbing state rebuilt under a new name.
    """
    db = tmp_path / "kill.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_CHILD, str(db), fps[0]],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert "CLAIMED" in proc.stdout, f"child never claimed: {proc.stderr}"
    assert proc.returncode == -9, (
        f"expected SIGKILL (-9), got {proc.returncode}: if the child exited "
        "normally it could have cleaned up, and this test would be measuring "
        "shutdown code instead of durability"
    )

    with SqliteStore(db) as store:
        # The claim SURVIVED the crash, exactly as intended: recovery must not
        # depend on the dead process having run anything.
        assert store.get(fps[0]).state is ProxyState.PROBING
        assert store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000,
                           now=NOW) == ()

        # ...and the deadline recorded IN THE ROW is what brings it back.
        assert store.reclaim_stale_probes(now=NOW + timedelta(hours=1)) == 1
        row = store.get(fps[0])
        assert row.state is ProxyState.COOLING, (
            "a probe that never reported is not evidence of health; promoting "
            "it to READY would hand out a proxy on an unfinished measurement"
        )
        assert row.reason_code == "PROBE_ABANDONED"


def test_a_reclaimed_row_is_probeable_again(tmp_path: Path) -> None:
    """Recovery must be COMPLETE: the row re-enters the normal cycle."""
    db = tmp_path / "recover.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)
        store.claim_for_probe(fps, now=NOW, probe_ms=1_000)
        assert store.reclaim_stale_probes(now=NOW + timedelta(minutes=5)) == 1
        again = store.claim_for_probe(fps, now=NOW + timedelta(minutes=5),
                                      probe_ms=60_000)
        assert len(again) == 1, "a reclaimed row must be claimable again"


# ── 4. the write-back under contention ───────────────────────────────────────
def test_the_writeback_refuses_after_the_claim_was_reclaimed(
        tmp_path: Path) -> None:
    """
    A slow probe whose claim expired must not overwrite whoever took the row.

    This is the residual race ADR-038 explicitly does NOT claim to abolish: the
    claim narrows the window, `complete_probe`'s condition closes it. The stale
    measurement is discarded and reported, never applied.
    """
    db = tmp_path / "slow.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)
        claimed = store.claim_for_probe(fps, now=NOW, probe_ms=1_000)
        assert len(claimed) == 1

        # The claim lapses, the row is reclaimed, and a consumer takes it.
        store.reclaim_stale_probes(now=NOW + timedelta(minutes=5))
        store.upsert_many((store.get(fps[0])
                           .with_state(ProxyState.READY, reason="OK")
                           .graded(Grade.GOOD),))
        leased = store.lease(count=1, min_grade=Grade.USABLE, lease_ms=60_000,
                             now=NOW + timedelta(minutes=5))
        assert len(leased) == 1

        # The slow probe finally reports, holding a claim it no longer owns.
        probed = (claimed[0].record_success(NOW)
                  .with_state(ProxyState.READY, reason="OK")
                  .graded(Grade.GOOD))
        assert store.complete_probe(probed, now=NOW) is False

        row = store.get(fps[0])
        assert row.state is ProxyState.LEASED
        assert row.lease_id == leased[0].lease_id, (
            "the consumer keeps the row; the stale measurement is dropped"
        )


def test_a_successful_writeback_does_not_touch_lease_columns(
        tmp_path: Path) -> None:
    """
    `complete_probe` must be UNABLE to assert anything about leases.

    The clobber was possible because the write path carried columns its writer
    had no evidence about, so the SET list deliberately omits `lease_id` and
    `lease_expires_at`. Verified against the RAW row rather than the domain
    object, because the domain object cannot show a column that was never
    written -- it would report whatever the in-memory copy says.
    """
    db = tmp_path / "narrow.db"
    with SqliteStore(db) as store:
        fps = _seed(store, 1)
        store._db.execute(
            "UPDATE proxies SET lease_id='sentinel', "
            "lease_expires_at='2099-01-01T00:00:00+00:00' WHERE fingerprint=?",
            (fps[0],))
        claimed = store.claim_for_probe(fps, now=NOW, probe_ms=60_000)
        assert len(claimed) == 1

        probed = (claimed[0].record_success(NOW)
                  .with_state(ProxyState.READY, reason="OK")
                  .graded(Grade.GOOD))
        assert store.complete_probe(probed, now=NOW) is True

        raw = store._db.execute(
            "SELECT lease_id, lease_expires_at, probe_expires_at, state "
            "FROM proxies WHERE fingerprint=?", (fps[0],)).fetchone()
        assert raw["lease_id"] == "sentinel", (
            "the probe rewrote a lease column it had no evidence about -- "
            "this is the clobber, reintroduced through the write-back"
        )
        assert raw["lease_expires_at"] == "2099-01-01T00:00:00+00:00"
        assert raw["probe_expires_at"] is None, "the claim was not released"
        assert raw["state"] == "READY"
