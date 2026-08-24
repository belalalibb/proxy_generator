"""
HAND-OUT against the REAL store — and the no-double-delivery property at the
hand-out layer (P08).

WHY THIS FILE EXISTS SEPARATELY FROM test_handout.py

The unit suite drives a fake store. A fake encodes MY BELIEFS about the store's
contract, and those beliefs are exactly what can be wrong: `lease()` returns rows
whose `state` and `lease_id` were rewritten by SQL, `release()` is itself a
compare-and-set, and `expire_leases()` depends on stored ISO timestamps. If any
of that differs from the fake, the unit tests would still pass while the real
hand-out was broken.

WHAT IS AND IS NOT RE-PROVEN HERE

H3 at the STORE is already proven (P05.T3: real processes, naive negative
control, 0 vs 30 duplicates) and is NOT repeated. What is new is that the
hand-out layer -- which leases MORE rows than it grants and releases the surplus
-- cannot hand the same proxy to two concurrent callers, and cannot leak the rows
it over-selected. Over-selection is new machinery on the serving path, so it gets
its own concurrency evidence rather than inheriting the store's.
"""
from __future__ import annotations

import multiprocessing as mp
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import (
    Anonymity, Endpoint, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import Target
from atlas.core.domain.verdict import Grade
from atlas.core.policy.target_policy import TargetPolicy
from atlas.engine.handout import HandoutPolicy, HandoutRefusal, HandoutService

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
POLICY = TargetPolicy(deny_hosts=frozenset({"instagram.com"}))
TARGET = Target(url="https://example.com")


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> float:
        return self._now.timestamp() * 1000.0

    def deadline(self, after_ms: float) -> datetime:
        return self._now + timedelta(milliseconds=after_ms)


def _seed(store: SqliteStore, n: int, *, age_s: float = 10.0) -> None:
    """
    n READY, GOOD, recently-checked proxies with distinct p95 values.

    `last_checked` is set: `rank(include_stale=False)` drops never-checked
    proxies, so a pool seeded without timestamps would yield ALL_STALE and the
    concurrency assertions below would be vacuously true (0 == 0).
    """
    batch = []
    for i in range(n):
        ep = Endpoint.parse(f"10.{i // 250}.{(i // 250) % 256}.{i % 250 + 1}:8080")
        p95 = 100.0 + i
        batch.append(Proxy(
            endpoint=ep, protocol=Protocol.HTTP, state=ProxyState.READY,
            grade=Grade.GOOD, anonymity=Anonymity.ELITE,
            latency=LatencyProfile(samples_ms=(p95,) * 5, p50_ms=p95,
                                   p95_ms=p95, success_ratio=1.0),
            total_successes=10, total_attempts=10,
            last_checked=NOW - timedelta(seconds=age_s),
        ))
    store.upsert_many(tuple(batch))


def _service(store: SqliteStore, **kw) -> HandoutService:
    return HandoutService(store=store, clock=FixedClock(),
                          target_policy=POLICY, **kw)


@pytest.fixture()
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield str(Path(td) / "handout.db")


# ── the real store honours the contract the fake assumes ─────────────────────
def test_handout_grants_from_a_real_sqlite_pool(db_path):
    with SqliteStore(db_path) as store:
        _seed(store, 20)
        res = _service(store).handout(target=TARGET, count=3)

        assert len(res.granted) == 3
        assert res.leased == 9                      # 3 * overselect 3
        assert res.released_surplus == 6
        # the granted rows are really LEASED in the database
        for g in res.granted:
            row = store.get(g.fingerprint)
            assert row.state is ProxyState.LEASED
            assert row.lease_id is not None


def test_the_surplus_is_really_back_in_the_pool(db_path):
    """
    The leak test against real SQL. `release()` is a compare-and-set on
    state='LEASED', so this also proves the surplus was genuinely LEASED first
    (a no-op release would leave the rows LEASED and this would fail).
    """
    with SqliteStore(db_path) as store:
        _seed(store, 20)
        res = _service(store).handout(target=TARGET, count=2)

        counts = store.count_by_state()
        assert counts.get(ProxyState.LEASED, 0) == 2          # only the granted
        assert counts.get(ProxyState.READY, 0) == 18          # 20 - 2
        assert res.released_surplus == 4


def test_release_all_returns_rows_to_ready(db_path):
    with SqliteStore(db_path) as store:
        _seed(store, 10)
        svc = _service(store)
        res = svc.handout(target=TARGET, count=3)
        assert store.count_by_state().get(ProxyState.LEASED, 0) == 3

        svc.release_all(res)
        assert store.count_by_state().get(ProxyState.LEASED, 0) == 0
        assert store.count_by_state().get(ProxyState.READY, 0) == 10


def test_a_refused_target_leases_nothing_from_the_real_store(db_path):
    with SqliteStore(db_path) as store:
        _seed(store, 10)
        res = _service(store).handout(
            target=Target(url="https://instagram.com"), count=2)
        assert res.refusal == HandoutRefusal.TARGET_REFUSED
        assert store.count_by_state().get(ProxyState.READY, 0) == 10
        assert store.count_by_state().get(ProxyState.LEASED, 0) == 0


def test_an_empty_real_pool_says_pool_empty(db_path):
    with SqliteStore(db_path) as store:
        res = _service(store).handout(target=TARGET, count=1)
        assert res.refusal == HandoutRefusal.POOL_EMPTY


def test_a_stale_real_pool_says_all_stale(db_path):
    """
    Distinguishing ALL_STALE from POOL_EMPTY against real rows. The pool is
    seeded far beyond ScoringPolicy.max_age_s, so every row is leased, found
    stale, and released.
    """
    with SqliteStore(db_path) as store:
        _seed(store, 5, age_s=99_999.0)
        res = _service(store).handout(target=TARGET, count=2)
        assert res.refusal == HandoutRefusal.ALL_STALE
        assert res.leased == 5           # fewer than requested: pool exhausted
        assert res.released_unusable == 5
        # and nothing was left stranded
        assert store.count_by_state().get(ProxyState.LEASED, 0) == 0


def test_expired_leases_are_reclaimed_by_the_handout(db_path):
    """
    A consumer that died holding a lease must not remove capacity permanently.
    The lease is taken with a short TTL, then the hand-out runs with a clock
    PAST the deadline, so its opening sweep is what returns the rows.
    """
    with SqliteStore(db_path) as store:
        _seed(store, 4)
        store.lease(count=4, min_grade=Grade.USABLE, lease_ms=1_000, now=NOW)
        assert store.count_by_state().get(ProxyState.LEASED, 0) == 4

        later = NOW + timedelta(seconds=60)
        svc = HandoutService(store=store, clock=FixedClock(later),
                             target_policy=POLICY)
        res = svc.handout(target=TARGET, count=1)

        assert res.reclaimed_expired == 4
        assert len(res.granted) == 1


# ── no double delivery THROUGH the hand-out, under real process concurrency ──
def _worker(db_path: str, count: int, out: mp.Queue) -> None:
    """
    A separate PROCESS with its own sqlite connection -- not a thread. Threads
    share the GIL and can hide a race that real processes expose.

    Each worker performs a FULL hand-out: sweep, over-select, rank, grant,
    release surplus. That is the whole point -- the store's lease is already
    proven, the composed operation is not.
    """
    store = SqliteStore(db_path)
    try:
        svc = HandoutService(store=store, clock=FixedClock(),
                             target_policy=POLICY,
                             policy=HandoutPolicy(reclaim_expired_first=False))
        res = svc.handout(target=TARGET, count=count)
        out.put(list(res.fingerprints))
    finally:
        store.close()


@pytest.mark.parametrize("procs,per_proc,pool", [(8, 3, 12)])
def test_concurrent_handouts_never_deliver_the_same_proxy_twice(
        db_path, procs, per_proc, pool):
    """
    8 processes each ask for 3 from a pool of 12, so demand (24) exceeds supply
    and the over-selection (up to 9 rows each) contends hard.

    Asserted:
      * no fingerprint is granted to two callers  (H3 at the hand-out layer)
      * total granted <= pool size               (no invention)
      * the store's own lease_log audit reports zero violations
      * no row is left LEASED-but-ungranted      (no leak under contention)
    """
    with SqliteStore(db_path) as store:
        _seed(store, pool)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(db_path, per_proc, q))
               for _ in range(procs)]
    for w in workers:
        w.start()
    batches = [q.get(timeout=60) for _ in range(procs)]
    for w in workers:
        w.join(timeout=60)

    handed = [fp for batch in batches for fp in batch]

    # NON-VACUITY FIRST. "no duplicates" is trivially true of an empty list, and
    # an idle machine yields 0 duplicates from broken code too (ADR-022). If the
    # pool were mis-seeded -- say every row stale, so every hand-out returned
    # ALL_STALE -- every assertion below would pass while proving nothing.
    # Measured on this machine: 12 granted from a pool of 12 across 8 processes.
    assert handed, (
        "VACUOUS: no proxy was granted at all, so the no-overlap assertion "
        "below would be trivially true"
    )

    assert len(handed) == len(set(handed)), (
        f"DOUBLE DELIVERY through the hand-out: {len(handed)} granted but "
        f"{len(set(handed))} unique"
    )
    assert len(handed) <= pool

    with SqliteStore(db_path) as store:
        assert store.double_delivery_violations() == ()
        # every granted row is LEASED; every other row is back to READY.
        leased_now = store.count_by_state().get(ProxyState.LEASED, 0)
        assert leased_now == len(handed), (
            f"{leased_now} rows LEASED but only {len(handed)} granted: the "
            "surplus was not released under contention"
        )
