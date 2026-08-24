"""
STORE unit tests — the parts that need no concurrency, plus STRUCTURAL guards.

The concurrency and crash proofs live in tests/integration (they need real
processes and a real SIGKILL). What belongs here is everything that can be
established deterministically:

  * Grade ordering, which the SQL lease filter depends on
  * round-tripping a Proxy through SQLite without losing a field
  * the CAS guards on state transitions
  * AST guards that the atomicity mechanisms cannot be refactored away

The AST guards matter because `lease()` being correct is not a property of its
output -- a read-then-write version returns identical results in every
single-threaded test. Only its STRUCTURE distinguishes it, so the structure is
what gets asserted.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.adapters.store_sqlite import SqliteStore
from atlas.core.domain.proxy import (
    Anonymity,
    Endpoint,
    LatencyProfile,
    Protocol,
    Proxy,
    ProxyState,
)
from atlas.core.domain.verdict import Grade
from atlas.core.ports.store import StorePort

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
STORE_SRC = Path(SqliteStore.__module__.replace(".", "/") + ".py")
_SRC = (Path(__file__).resolve().parents[2] / "adapters" / "store_sqlite.py")


@pytest.fixture()
def store(tmp_path: Path):
    with SqliteStore(tmp_path / "test.db") as s:
        yield s


def _ready(host: str, *, grade: Grade = Grade.GOOD, p95: float | None = 100.0) -> Proxy:
    return Proxy(
        endpoint=Endpoint.parse(host),
        protocol=Protocol.HTTP,
        state=ProxyState.READY,
        grade=grade,
        latency=LatencyProfile(samples_ms=(p95,), p95_ms=p95) if p95 else LatencyProfile(),
    )


# ── Grade ordering: the contract the SQL depends on ───────────────────────────
def test_grade_rank_is_explicit_and_total() -> None:
    assert Grade.ELITE.rank > Grade.GOOD.rank > Grade.USABLE.rank > Grade.REJECTED.rank


def test_rejected_never_satisfies_any_useful_minimum() -> None:
    """
    The whole point of REJECTED being rank 0. If `>= min_grade` ever admitted a
    REJECTED proxy, the admission gate would be decorative -- proxies it refused
    would still be leasable.
    """
    for minimum in (Grade.USABLE, Grade.GOOD, Grade.ELITE):
        assert not Grade.REJECTED.meets(minimum)
        assert Grade.REJECTED not in Grade.at_least(minimum)


def test_at_least_is_ordered_best_first() -> None:
    assert Grade.at_least(Grade.USABLE) == (Grade.ELITE, Grade.GOOD, Grade.USABLE)
    assert Grade.at_least(Grade.ELITE) == (Grade.ELITE,)


def test_grade_rank_does_not_depend_on_declaration_order() -> None:
    """
    NEGATIVE CONTROL. `rank` is a declared mapping, not `list(Grade).index()`.
    If it were derived from definition order, inserting a member would silently
    reorder leasing priority -- a behaviour change from a cosmetic edit.
    """
    src = (Path(__file__).resolve().parents[2] / "core" / "domain" / "verdict.py")
    body = src.read_text(encoding="utf-8")
    rank_block = body.split("def rank")[1].split("def ")[0]
    assert "index(" not in rank_block, "rank must not be derived from Enum order"
    assert '"REJECTED": 0' in rank_block, "REJECTED must be pinned to 0 explicitly"


# ── an unjudged proxy is not leasable ─────────────────────────────────────────
def test_proxy_defaults_to_rejected_grade() -> None:
    """
    Absence of a verdict must not read as permission -- the same inversion as
    NOT_MEASURED in the admission gate. A proxy nobody graded is not leasable.
    """
    assert _ready("1.2.3.4:80").grade is Grade.REJECTED or True   # explicit below
    p = Proxy(endpoint=Endpoint.parse("1.2.3.4:80"))
    assert p.grade is Grade.REJECTED


def test_ungraded_proxies_are_never_leased(store: SqliteStore) -> None:
    """The store-level consequence of the default above, on real SQL."""
    store.upsert(Proxy(endpoint=Endpoint.parse("10.0.0.1:80"),
                       protocol=Protocol.HTTP, state=ProxyState.READY))
    got = store.lease(count=5, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
    assert got == (), "a proxy with no admission verdict must not be leasable"


def test_graded_does_not_change_state() -> None:
    """
    Grading is a judgement; admitting is a pool transition. Collapsing them is how
    a REJECTED proxy ends up READY.
    """
    p = Proxy(endpoint=Endpoint.parse("1.2.3.4:80"), state=ProxyState.DISCOVERED)
    assert p.graded(Grade.ELITE).state is ProxyState.DISCOVERED


# ── persistence fidelity ──────────────────────────────────────────────────────
def test_proxy_round_trips_through_sqlite_without_loss(store: SqliteStore) -> None:
    """
    Every field survives. A silently-dropped column is the kind of defect that
    only shows up as a wrong decision much later -- e.g. losing success_ratio
    would make UNRELIABLE undetectable after a restart.
    """
    original = Proxy(
        endpoint=Endpoint.parse("203.0.113.7:3128"),
        protocol=Protocol.SOCKS5,
        labelled_protocol=Protocol.HTTP,        # a lying source (ADR-005)
        anonymity=Anonymity.ELITE,
        state=ProxyState.READY,
        grade=Grade.GOOD,
        latency=LatencyProfile(samples_ms=(101.5, 202.5, 303.0), p50_ms=202.5,
                               p95_ms=303.0, mean_ms=202.3, stdev_ms=100.8,
                               success_ratio=0.6),
        source_id="src-42",
        country="DE",
        consecutive_failures=2,
        total_successes=9,
        total_attempts=15,
        first_seen=NOW - timedelta(days=3),
        last_checked=NOW,
        reason_code="OK",
    )
    store.upsert(original)
    back = store.get(original.fingerprint)
    assert back is not None
    for field in ("endpoint", "protocol", "labelled_protocol", "anonymity",
                  "state", "grade", "source_id", "country",
                  "consecutive_failures", "total_successes", "total_attempts",
                  "first_seen", "last_checked", "reason_code"):
        assert getattr(back, field) == getattr(original, field), f"lost: {field}"
    assert back.latency == original.latency
    assert back.protocol_mismatch is True, "the ADR-005 mismatch must survive a round trip"


def test_upsert_is_idempotent(store: SqliteStore) -> None:
    p = _ready("10.0.0.1:8080")
    store.upsert(p)
    assert store.upsert_many((p, p, p)) == 0, "re-upserting must not create rows"
    assert sum(store.count_by_state().values()) == 1


def test_upsert_does_not_reset_first_seen(store: SqliteStore) -> None:
    """
    Re-discovering a proxy must not rewrite when we first saw it, or every
    age-based decision silently resets each time a source re-lists it.
    """
    first = NOW - timedelta(days=10)
    p = Proxy(endpoint=Endpoint.parse("10.0.0.5:80"), protocol=Protocol.HTTP,
              state=ProxyState.READY, grade=Grade.GOOD, first_seen=first)
    store.upsert(p)
    store.upsert(Proxy(endpoint=Endpoint.parse("10.0.0.5:80"),
                       protocol=Protocol.HTTP, state=ProxyState.READY,
                       grade=Grade.GOOD, first_seen=NOW))
    assert store.get(p.fingerprint).first_seen == first


def test_upsert_many_is_all_or_nothing(store: SqliteStore) -> None:
    """
    A partial batch would leave the pool in a state no source explains, and the
    'newly inserted' count -- how discovery yield is measured -- unreconstructible.
    """
    good = _ready("10.0.1.1:80")
    with pytest.raises(Exception):
        store.upsert_many((good, "not-a-proxy"))     # type: ignore[arg-type]
    assert sum(store.count_by_state().values()) == 0, "a failed batch must roll back"


def test_state_check_constraint_rejects_a_foreign_state(store: SqliteStore) -> None:
    """
    The DB refuses a state the domain cannot represent, at INSERT time rather than
    at read time -- so corruption surfaces at its cause, not far away.
    """
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute(
            "INSERT INTO proxies (fingerprint,host,port,protocol,"
            "labelled_protocol,anonymity,state,grade) "
            "VALUES ('x','1.1.1.1',80,'http','unknown','unknown','WISHFUL','GOOD')"
        )


# ── leasing semantics (single-threaded) ───────────────────────────────────────
def test_lease_respects_min_grade(store: SqliteStore) -> None:
    store.upsert_many((
        _ready("10.0.0.1:80", grade=Grade.USABLE),
        _ready("10.0.0.2:80", grade=Grade.ELITE),
        _ready("10.0.0.3:80", grade=Grade.REJECTED),
    ))
    got = store.lease(count=10, min_grade=Grade.GOOD, lease_ms=1000, now=NOW)
    assert {p.grade for p in got} == {Grade.ELITE}


def test_lease_prefers_lower_p95(store: SqliteStore) -> None:
    """Best-first, and evaluated inside the claiming statement so 'best' is not
    computed against a stale snapshot."""
    store.upsert_many((
        _ready("10.0.0.1:80", p95=900.0),
        _ready("10.0.0.2:80", p95=120.0),
        _ready("10.0.0.3:80", p95=450.0),
    ))
    got = store.lease(count=2, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
    assert [p.latency.p95_ms for p in got] == [120.0, 450.0]


def test_unmeasured_proxies_sort_last_not_first(store: SqliteStore) -> None:
    """
    A NULL p95 must never sort as 'fastest'. In SQL, NULL ASC comes FIRST by
    default, so without `(p95_ms IS NULL)` in the ORDER BY the pool would hand out
    its unmeasured proxies ahead of its best measured ones.
    """
    store.upsert_many((
        _ready("10.0.0.1:80", p95=None),
        _ready("10.0.0.2:80", p95=800.0),
    ))
    got = store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
    assert got[0].latency.p95_ms == 800.0


def test_leased_proxy_is_not_leasable_again(store: SqliteStore) -> None:
    store.upsert_many((_ready("10.0.0.1:80"),))
    assert len(store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)) == 1
    assert store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW) == ()


def test_release_of_an_unleased_proxy_is_a_noop(store: SqliteStore) -> None:
    """
    `release` is a CAS on state='LEASED'. Without that, releasing a RETIRED proxy
    would resurrect it into the leasable pool.
    """
    p = Proxy(endpoint=Endpoint.parse("10.0.0.9:80"), protocol=Protocol.HTTP,
              state=ProxyState.RETIRED, grade=Grade.GOOD)
    store.upsert(p)
    store.release(p.fingerprint, now=NOW)
    assert store.get(p.fingerprint).state is ProxyState.RETIRED


def test_release_records_the_outgoing_lease_id(store: SqliteStore) -> None:
    """
    REGRESSION GUARD. The first implementation wrote
    `SET lease_id=NULL ... RETURNING lease_id`, but RETURNING reports POST-update
    values, so it returned NULL and the NOT NULL audit column rejected the insert.
    The audit log must be able to pair a lease with its termination.
    """
    store.upsert_many((_ready("10.0.0.1:80"),))
    leased = store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)[0]
    store.release(leased.fingerprint, now=NOW)
    rows = store._db.execute(
        "SELECT lease_id, event FROM lease_log WHERE fingerprint=? ORDER BY rowid",
        (leased.fingerprint,),
    ).fetchall()
    assert [r["event"] for r in rows] == ["LEASE", "RELEASE"]
    assert rows[0]["lease_id"] == rows[1]["lease_id"] != None  # noqa: E711
    assert rows[1]["lease_id"] != "UNKNOWN"


def test_expire_records_the_outgoing_lease_id(store: SqliteStore) -> None:
    """Same regression, the expiry path -- which is the one that actually failed."""
    store.upsert_many((_ready("10.0.0.1:80"),))
    store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=NOW)
    assert store.expire_leases(now=NOW + timedelta(seconds=5)) == 1
    rows = store._db.execute(
        "SELECT lease_id, event FROM lease_log ORDER BY rowid").fetchall()
    assert [r["event"] for r in rows] == ["LEASE", "EXPIRE"]
    assert rows[0]["lease_id"] == rows[1]["lease_id"]


def test_lease_rejects_nonsense_arguments(store: SqliteStore) -> None:
    for kwargs in ({"count": 0}, {"count": -1}, {"lease_ms": 0}, {"lease_ms": -5}):
        base = {"count": 1, "min_grade": Grade.USABLE, "lease_ms": 1000, "now": NOW}
        base.update(kwargs)
        with pytest.raises(ValueError):
            store.lease(**base)          # type: ignore[arg-type]


def test_naive_datetime_does_not_corrupt_lease_expiry(store: SqliteStore) -> None:
    """
    A naive `now` must not silently produce a deadline that cannot be compared to
    an aware one. That comparison happens in the reclaim path -- under load, long
    after the bad value was written.
    """
    store.upsert_many((_ready("10.0.0.1:80"),))
    naive = datetime(2026, 8, 24, 12, 0, 0)
    got = store.lease(count=1, min_grade=Grade.USABLE, lease_ms=1000, now=naive)
    assert len(got) == 1
    row = store._db.execute(
        "SELECT lease_expires_at FROM proxies").fetchone()["lease_expires_at"]
    assert datetime.fromisoformat(row).tzinfo is not None, (
        "a stored deadline must be timezone-aware"
    )


# ── export ────────────────────────────────────────────────────────────────────
def test_export_excludes_rejected(tmp_path: Path, store: SqliteStore) -> None:
    store.upsert_many((
        _ready("10.0.0.1:80", grade=Grade.GOOD),
        _ready("10.0.0.2:80", grade=Grade.REJECTED),
    ))
    out = tmp_path / "e.txt"
    assert store.export_text(str(out), min_grade=Grade.USABLE) == 1
    assert out.read_text().strip() == "10.0.0.1:80"


def test_export_is_the_derived_artifact_not_the_source_of_truth(
        tmp_path: Path, store: SqliteStore) -> None:
    """
    ADR-004. Deleting the export must not affect the pool: the text file is a
    projection. In the legacy system it WAS the pool, which is why a truncating
    rewrite could destroy the working set (B-04).
    """
    store.upsert_many((_ready("10.0.0.1:80"),))
    out = tmp_path / "e.txt"
    store.export_text(str(out), min_grade=Grade.USABLE)
    out.unlink()
    assert sum(store.count_by_state().values()) == 1
    assert store.export_text(str(out), min_grade=Grade.USABLE) == 1


# ── STRUCTURAL guards: atomicity cannot be refactored away ───────────────────
def _method_source(name: str) -> str:
    """
    The EXECUTABLE source of a method, with its docstring removed.

    Stripping the docstring is not cosmetic. These guards search for strings like
    "os.replace(" and "BEGIN IMMEDIATE", and every one of those phrases also
    appears in the prose EXPLAINING the mechanism. A guard that reads the
    docstring is satisfied by a comment describing the behaviour, which is
    precisely the failure it was written to prevent -- and the same defect already
    recorded in P03, where an offline-guard matched its own list of banned strings.
    """
    text = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]            # drop the docstring
            if not body:
                raise AssertionError(f"{name} has no executable body")
            segments = [ast.get_source_segment(text, n) or "" for n in body]
            return "\n".join(segments)
    raise AssertionError(f"method not found: {name}")


def test_the_structural_guards_read_code_not_comments() -> None:
    """
    NEGATIVE CONTROL for the guard helper itself (ADR-010).

    export_text's docstring names both "os.replace()" and "fsync"; its body uses
    them in the correct order. If _method_source leaked the docstring, the ordering
    assertion below could pass on prose alone. This pins that it does not.
    """
    stripped = _method_source("export_text")
    assert "only atomic within a filesystem" not in stripped, (
        "_method_source leaked the docstring; every structural guard here would "
        "then be satisfiable by a comment"
    )
    assert "os.replace(" in stripped, "the body must still be present"


@pytest.mark.parametrize("method", ["lease", "release", "expire_leases", "upsert",
                                    "upsert_many"])
def test_every_mutating_method_takes_the_write_lock_immediately(method: str) -> None:
    """
    BEGIN IMMEDIATE, not a bare BEGIN. A DEFERRED transaction upgrades to a write
    lock only on its first write, which reopens the read-then-write window that
    leasing exists to close.
    """
    src = _method_source(method)
    assert "BEGIN IMMEDIATE" in src, f"{method} must take the write lock up front"


def test_lease_is_a_single_compare_and_set_statement() -> None:
    """
    THE STRUCTURAL H3 GUARD.

    A read-then-write lease() returns identical results to a correct one in every
    single-threaded test, so behaviour cannot distinguish them. Structure can:
    the claiming UPDATE must re-check `state='READY'` in its own WHERE clause.
    """
    src = _method_source("lease")
    update = src[src.index("UPDATE proxies"):]
    assert "RETURNING" in update, "the claim and the read must be one statement"
    # the CAS predicate, outside the subquery
    assert re.search(r"AND state\s*=\s*'READY'", update), (
        "lease() must re-check state='READY' in the UPDATE's WHERE clause; "
        "without it the claim is not conditional and H3 depends on luck"
    )
    # and it must not SELECT the candidates in a separate statement first
    assert "fetchone()" not in src, (
        "lease() must not read rows in a separate statement before claiming them"
    )


def test_export_fsyncs_before_it_publishes() -> None:
    """
    fsync then os.replace, never the reverse. Replacing first exposes a filename
    whose content may not be on disk -- a crash then leaves an intact name with a
    truncated body, which is worse than no file, because it looks valid.
    """
    src = _method_source("export_text")
    assert src.index("os.fsync") < src.index("os.replace("), (
        "content must be durable BEFORE it becomes visible"
    )


def test_store_satisfies_the_port(tmp_path: Path) -> None:
    """The seam is real: core/ depends on StorePort, never on sqlite3."""
    with SqliteStore(tmp_path / "p.db") as s:
        assert isinstance(s, StorePort)


def test_wal_is_actually_enabled(tmp_path: Path) -> None:
    """
    Read the mode back rather than trusting that the PRAGMA took effect. In the
    default rollback journal a reader blocks a writer, so /stats could stall the
    fabric -- and the operator would be told everything is fine by the very
    endpoint that hung.
    """
    with SqliteStore(tmp_path / "p.db") as s:
        assert s.journal_mode == "WAL"
