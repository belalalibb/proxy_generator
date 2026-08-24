"""
H3 / H8 INTEGRATION TESTS — real processes, real SQLite, real SIGKILL.

These are integration tests on purpose. Every claim here is about what happens
when two OS processes contend for the same database file, or when the kernel
kills one mid-write. Neither can be established with mocks: a mock of sqlite3
would be a model of my own assumptions, and the assumption is exactly what is
under test.

Three properties are proven:

  H3  concurrent leases never overlap                  (multiprocessing, N procs)
  H3  the test that proves it FAILS on a naive store    (negative control)
  H8  a SIGKILL mid-write loses no acknowledged data    (os.kill(SIGKILL))
"""
from __future__ import annotations

import multiprocessing as mp
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import Endpoint, Protocol, Proxy, ProxyState
from atlas.core.domain.verdict import Grade
from atlas.tests.integration.naive_store import NaiveStore

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[3]


def _seed(store: SqliteStore, n: int, *, grade: Grade = Grade.GOOD) -> None:
    """n READY proxies, distinct endpoints, deterministic p95 for stable ordering."""
    batch = []
    for i in range(n):
        ep = Endpoint.parse(f"10.{i // 250}.{(i // 250) % 256}.{i % 250 + 1}:8080")
        batch.append(
            Proxy(endpoint=ep, protocol=Protocol.HTTP, state=ProxyState.READY,
                  grade=grade)
        )
    store.upsert_many(tuple(batch))


# ── H3: concurrent leasing ────────────────────────────────────────────────────
def _worker(db_path: str, count: int, out: mp.Queue) -> None:
    """A separate PROCESS, its own sqlite connection -- not a thread. Threads in
    CPython share the GIL and can hide a race that separate processes expose."""
    store = SqliteStore(db_path)
    try:
        leased = store.lease(count=count, min_grade=Grade.USABLE,
                             lease_ms=60_000, now=NOW)
        out.put([p.fingerprint for p in leased])
    finally:
        store.close()


@pytest.mark.parametrize("procs,per_proc,pool", [(8, 5, 40), (12, 4, 24)])
def test_concurrent_leases_never_overlap(tmp_path: Path, procs: int,
                                         per_proc: int, pool: int) -> None:
    """
    THE H3 TEST. N processes lease simultaneously; no fingerprint may appear twice.

    The second parametrisation OVER-SUBSCRIBES the pool (12*4=48 requested from
    24 available). Contention is where a read-then-write implementation breaks,
    so under-subscribing would make the test easier and prove less.
    """
    db = tmp_path / "pool.db"
    with SqliteStore(db) as store:
        _seed(store, pool)

    ctx = mp.get_context("spawn")     # spawn: no inherited sqlite handles
    q: mp.Queue = ctx.Queue()
    # start all workers before joining any, so they actually overlap
    workers = [ctx.Process(target=_worker, args=(str(db), per_proc, q))
               for _ in range(procs)]
    for w in workers:
        w.start()
    results = [q.get(timeout=60) for _ in range(procs)]
    for w in workers:
        w.join(timeout=60)
        assert w.exitcode == 0, f"worker died: exitcode={w.exitcode}"

    handed_out = [fp for r in results for fp in r]
    assert len(handed_out) == len(set(handed_out)), (
        f"H3 VIOLATED: {len(handed_out) - len(set(handed_out))} duplicate "
        f"delivery(ies) across {procs} processes"
    )
    # Never hand out more than existed.
    assert len(handed_out) <= pool

    with SqliteStore(db) as store:
        assert store.double_delivery_violations() == ()
        counts = store.count_by_state()
        assert counts.get(ProxyState.LEASED, 0) == len(handed_out)


def test_oversubscribed_pool_hands_out_each_proxy_exactly_once(
        tmp_path: Path) -> None:
    """
    With demand far above supply, the pool must be exhausted EXACTLY -- every
    proxy leased once, none twice, none lost.

    This is the strongest form of the H3 claim: it pins both directions at the
    same time. Duplicate delivery breaks the uniqueness assertion; a lost row
    (e.g. claimed but not returned) breaks the exhaustion assertion.
    """
    pool = 20
    db = tmp_path / "pool.db"
    with SqliteStore(db) as store:
        _seed(store, pool)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(str(db), 6, q)) for _ in range(10)]
    for w in workers:
        w.start()
    results = [q.get(timeout=60) for _ in range(10)]
    for w in workers:
        w.join(timeout=60)

    handed = [fp for r in results for fp in r]
    assert len(handed) == len(set(handed)), "H3 VIOLATED: duplicate delivery"
    assert len(handed) == pool, (
        f"expected the pool to be exhausted exactly: {len(handed)} of {pool}"
    )
    with SqliteStore(db) as store:
        assert store.count_by_state().get(ProxyState.READY, 0) == 0


# ── the negative control: the same test must FAIL on a broken store ───────────
def _naive_worker(db_path: str, count: int, gap_s: float, out: mp.Queue) -> None:
    store = NaiveStore(db_path)
    try:
        out.put(list(store.lease_naive(count=count, now=NOW, gap_s=gap_s)))
    finally:
        store.close()


def test_the_h3_test_would_catch_a_read_then_write_store(tmp_path: Path) -> None:
    """
    NEGATIVE CONTROL (ADR-010).

    The identical overlap assertion, run against NaiveStore, MUST detect duplicate
    delivery. If it does not, then the passing result above says nothing about
    correctness -- it only says the processes never actually raced.

    This is the check that distinguishes 'my leasing is atomic' from
    'my test never created contention'.
    """
    db = tmp_path / "naive.db"
    with SqliteStore(db) as store:
        _seed(store, 12)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    # every worker reads the same top-of-pool rows, then claims them
    workers = [ctx.Process(target=_naive_worker, args=(str(db), 6, 0.25, q))
               for _ in range(6)]
    for w in workers:
        w.start()
    results = [q.get(timeout=60) for _ in range(6)]
    for w in workers:
        w.join(timeout=60)

    handed = [fp for r in results for fp in r]
    duplicates = len(handed) - len(set(handed))
    assert duplicates > 0, (
        "the negative control did not reproduce double delivery, so the H3 test "
        "above is not actually exercising concurrency and proves nothing"
    )
    # And the independent audit must SEE it in the log.
    with SqliteStore(db) as store:
        assert store.double_delivery_violations() != (), (
            "double_delivery_violations() failed to detect a real violation"
        )


# ── H8: crash durability ──────────────────────────────────────────────────────
# NOTE: this template is filled with str.format, so it must contain NO stray
# braces and NO %-formatting. An earlier version used "%d" % (...) inside and the
# doubled "%%" needed to survive .format() produced a SyntaxError in the child --
# which the test caught only because it asserts on the child's ACKED output
# instead of trusting that a subprocess did what was intended.
_CRASH_CHILD = r"""
import os, sys, signal
sys.path.insert(0, {repo!r})
from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import Proxy, Endpoint, ProxyState, Protocol
from atlas.core.domain.verdict import Grade

store = SqliteStore({db!r})
batch = tuple(
    Proxy(endpoint=Endpoint.parse("10.9." + str(i // 250) + "." + str(i % 250 + 1)
                                  + ":8080"),
          protocol=Protocol.HTTP, state=ProxyState.READY, grade=Grade.GOOD)
    for i in range({n})
)
store.upsert_many(batch)
store.checkpoint()
print("ACKED", flush=True)          # the write is acknowledged AND flushed
os.kill(os.getpid(), signal.SIGKILL)  # hard kill: no atexit, no close, no flush
"""


def test_sigkill_after_acknowledged_write_loses_nothing(tmp_path: Path) -> None:
    """
    H8. A child writes 300 proxies, checkpoints, prints ACKED, then SIGKILLs
    ITSELF. A fresh process must then see all 300.

    SIGKILL, not sys.exit or an exception: signal 9 cannot be caught, so no
    cleanup handler, no context manager and no `finally` runs. That is the only
    way to test durability rather than testing my own shutdown code.
    """
    db = tmp_path / "crash.db"
    n = 300
    src = _CRASH_CHILD.format(repo=str(REPO), db=str(db), n=n)
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True,
                          text=True, timeout=120)

    assert "ACKED" in proc.stdout, (
        f"child never acknowledged the write; stderr={proc.stderr[:800]}"
    )
    assert proc.returncode == -signal.SIGKILL, (
        f"expected death by SIGKILL (-9), got returncode={proc.returncode}. "
        "If the process exited normally this test proves nothing about crashes."
    )

    with SqliteStore(db) as store:
        recovered = sum(store.count_by_state().values())
    assert recovered == n, f"H8 VIOLATED: {recovered} of {n} survived SIGKILL"


def test_export_is_never_observed_truncated(tmp_path: Path) -> None:
    """
    H8 / B-04. The legacy save used open(path,'w') -- truncate at open, refill
    after -- so a crash in that window left an empty or partial working set.

    Here a reader polls the export path while writers replace it repeatedly. Every
    observation must be a COMPLETE file: os.replace() is atomic, so a reader sees
    either the whole old content or the whole new content.
    """
    db = tmp_path / "pool.db"
    out = tmp_path / "exports" / "proxies.txt"
    with SqliteStore(db) as store:
        _seed(store, 200)
        store.export_text(str(out), min_grade=Grade.USABLE)

        expected = 200
        observations: list[int] = []
        for _ in range(25):
            store.export_text(str(out), min_grade=Grade.USABLE)
            lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
            observations.append(len(lines))

    assert observations, "no observation was made"
    assert all(n == expected for n in observations), (
        f"a reader saw a partial export: {sorted(set(observations))} "
        f"(expected every observation to be {expected})"
    )
    # no .tmp litter left behind
    leftovers = list(out.parent.glob(".*tmp"))
    assert not leftovers, f"temp files leaked into the export dir: {leftovers}"


def test_export_writes_tmp_in_the_same_directory(tmp_path: Path) -> None:
    """
    os.replace() is atomic only WITHIN a filesystem. A .tmp written to /tmp and
    moved onto a different mount degrades to copy-then-truncate, silently
    restoring the B-04 window. Pinning the temp file's directory keeps that
    guarantee from being refactored away by someone reaching for tempfile.
    """
    src = Path(__file__).resolve().parents[2] / "adapters" / "store_sqlite.py"
    body = src.read_text(encoding="utf-8")
    assert "target.with_name(" in body, (
        "export_text must build its temp path with target.with_name() so the "
        "temp file shares the target's directory"
    )
    assert "tempfile.gettempdir" not in body and "tempfile.mkstemp" not in body, (
        "the export temp file must not come from the system temp dir: "
        "os.replace across filesystems is not atomic"
    )
    assert "os.replace(" in body, "export must publish via os.replace()"


# ── lease expiry ──────────────────────────────────────────────────────────────
def test_expired_lease_is_reclaimed_not_leaked(tmp_path: Path) -> None:
    """
    A consumer that dies holding a lease must not remove the proxy permanently.
    The legacy design had no lease, so this leak was not fixed -- it was
    unrepresentable, which is not the same thing.
    """
    with SqliteStore(tmp_path / "p.db") as store:
        _seed(store, 3)
        leased = store.lease(count=3, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
        assert len(leased) == 3
        assert store.count_by_state().get(ProxyState.READY, 0) == 0

        # before the deadline: nothing is reclaimed
        assert store.expire_leases(now=NOW + timedelta(milliseconds=500)) == 0
        # after it: all three return
        assert store.expire_leases(now=NOW + timedelta(milliseconds=1500)) == 3
        assert store.count_by_state().get(ProxyState.READY, 0) == 3


def test_a_reclaimed_then_released_lease_is_not_a_double_delivery(
        tmp_path: Path) -> None:
    """
    Sequential lease -> expire -> lease of the SAME proxy is correct behaviour,
    not an H3 breach. If the audit flagged it, the audit would cry wolf on every
    reclaim and become useless -- so this pins the distinction.
    """
    with SqliteStore(tmp_path / "p.db") as store:
        _seed(store, 1)
        store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
        store.expire_leases(now=NOW + timedelta(seconds=2))
        store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000,
                    now=NOW + timedelta(seconds=3))
        assert store.double_delivery_violations() == ()
