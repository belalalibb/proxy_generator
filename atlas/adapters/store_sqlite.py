"""
SqliteStore — the pool, and the two guarantees proxy.txt could not give (ADR-004).

WHAT IT REPLACES

The legacy pool was a text file. Two consequences, both measured in the audit:

  * H3 (NO DOUBLE DELIVERY) was unachievable. proxy.txt has no LEASED state, so
    two concurrent consumers reading it are ALWAYS handed the same lines
    (BUG_LEDGER B-05). There is no way to express "this one is taken" in a format
    whose only structure is newlines.

  * H8 (CRASH RESUME) was violated by construction. The legacy save path used
    `open(path, 'w')` in 8 places (engineering/raw/bug_scan.json), which truncates
    the file at open() and refills it afterwards. A SIGKILL in that window leaves
    a truncated or EMPTY working set -- the whole pool destroyed by an interrupted
    write of a file that was only ever a cache (B-04).

THE TWO MECHANISMS

  lease()  is ONE statement: BEGIN IMMEDIATE, then a single UPDATE ... RETURNING
           whose WHERE clause re-checks state='READY'. There is no moment at which
           this process has read a row and not yet claimed it, so there is no
           window for a second process to read the same row. A read-then-write
           implementation would pass every single-threaded test and fail in
           production; the concurrency test in tests/integration exists to make
           that difference observable.

  export_text() writes to a .tmp in the SAME directory and then os.replace(),
           which is atomic on POSIX. A reader either sees the whole old file or
           the whole new one -- never a half-written one, and never an empty one.

core/ never imports sqlite3: this is the adapter, and StorePort is the seam
(enforced by test_architecture.py, which forbids sqlite3 anywhere under core/).
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from atlas.core.domain.proxy import (
    Anonymity,
    Endpoint,
    LatencyProfile,
    Protocol,
    Proxy,
    ProxyState,
)
from atlas.core.domain.verdict import Grade

# ── schema ────────────────────────────────────────────────────────────────────
# `state` and `grade` are CHECK-constrained to the domain enums. If application
# code ever writes a state the domain cannot represent, the INSERT fails here
# rather than producing a row that Proxy.__init__ will later refuse to load --
# a corruption that would only surface on read, far from its cause.
_STATES = ",".join(f"'{s.value}'" for s in ProxyState)
_GRADES = ",".join(f"'{g.value}'" for g in Grade)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS proxies (
    fingerprint         TEXT PRIMARY KEY,
    host                TEXT NOT NULL,
    port                INTEGER NOT NULL,
    protocol            TEXT NOT NULL,
    labelled_protocol   TEXT NOT NULL,
    anonymity           TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ({_STATES})),
    grade               TEXT NOT NULL CHECK (grade IN ({_GRADES})),
    samples_ms          TEXT NOT NULL DEFAULT '',
    p50_ms              REAL,
    p95_ms              REAL,
    mean_ms             REAL,
    stdev_ms            REAL,
    success_ratio       REAL,
    source_id           TEXT,
    country             TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_successes     INTEGER NOT NULL DEFAULT 0,
    total_attempts      INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT,
    last_checked        TEXT,
    lease_id            TEXT,
    lease_expires_at    TEXT,
    reason_code         TEXT
);

-- Leasing selects on (state, grade) and orders by p95: without this index the
-- CAS statement degrades to a full scan while holding a write lock, which turns
-- a correctness mechanism into a throughput ceiling.
CREATE INDEX IF NOT EXISTS idx_leasable ON proxies (state, grade, p95_ms);
CREATE INDEX IF NOT EXISTS idx_lease_expiry ON proxies (state, lease_expires_at);

-- Every lease and release, append-only. This is what makes an H3 violation
-- PROVABLE after the fact rather than merely unlikely: if the same fingerprint
-- were ever handed out under two live lease_ids, it is recorded here.
CREATE TABLE IF NOT EXISTS lease_log (
    lease_id    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    event       TEXT NOT NULL CHECK (event IN ('LEASE','RELEASE','EXPIRE')),
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lease_log_fp ON lease_log (fingerprint, at);
"""


# A lease row whose id could not be recovered. A NAMED sentinel rather than NULL:
# the lease_log column is NOT NULL on purpose, so an unattributable termination is
# recorded as explicitly unknown instead of being silently dropped by an
# IntegrityError -- losing the audit record of a real event.
_UNKNOWN_LEASE = "UNKNOWN"


class StoreError(RuntimeError):
    """Raised instead of swallowing a persistence failure (B-02: 23 of those)."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class SqliteStore:
    """
    A StorePort implementation over SQLite in WAL mode.

    WAL is not a performance tweak here, it is what allows /stats to read the pool
    while a lease is being written. In the default rollback journal a reader
    blocks a writer, so an observability endpoint could stall the fabric -- and
    the operator would then be told the system is fine, because the thing that
    reports health was the thing that hung.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000,
                 synchronous: str = "NORMAL") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None => explicit transaction control. The default
        # implicit-BEGIN behaviour would start a DEFERRED transaction, and a
        # DEFERRED transaction upgrades to a write lock only on first write --
        # exactly the read-then-write window that BEGIN IMMEDIATE closes.
        self._db = sqlite3.connect(str(self.path), isolation_level=None,
                                   timeout=busy_timeout_ms / 1000.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._db.execute(f"PRAGMA synchronous={synchronous}")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._writes = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._db.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        """Read back the ACTUAL mode, so a test can assert WAL rather than trust
        that the PRAGMA above took effect."""
        return str(self._db.execute("PRAGMA journal_mode").fetchone()[0]).upper()

    # ── row <-> domain ────────────────────────────────────────────────────────
    @staticmethod
    def _to_row(p: Proxy) -> dict:
        lat = p.latency
        return {
            "fingerprint": p.fingerprint,
            "host": p.endpoint.host,
            "port": p.endpoint.port,
            "protocol": p.protocol.value,
            "labelled_protocol": p.labelled_protocol.value,
            "anonymity": p.anonymity.value,
            "state": p.state.value,
            "grade": p.grade.value,
            "samples_ms": ",".join(repr(s) for s in lat.samples_ms),
            "p50_ms": lat.p50_ms,
            "p95_ms": lat.p95_ms,
            "mean_ms": lat.mean_ms,
            "stdev_ms": lat.stdev_ms,
            "success_ratio": lat.success_ratio,
            "source_id": p.source_id,
            "country": p.country,
            "consecutive_failures": p.consecutive_failures,
            "total_successes": p.total_successes,
            "total_attempts": p.total_attempts,
            "first_seen": _iso(p.first_seen),
            "last_checked": _iso(p.last_checked),
            "lease_id": p.lease_id,
            "reason_code": p.reason_code,
        }

    @staticmethod
    def _from_row(r: sqlite3.Row) -> Proxy:
        raw = r["samples_ms"]
        samples = tuple(float(x) for x in raw.split(",")) if raw else ()
        return Proxy(
            endpoint=Endpoint(host=r["host"], port=r["port"]),
            protocol=Protocol(r["protocol"]),
            labelled_protocol=Protocol(r["labelled_protocol"]),
            anonymity=Anonymity(r["anonymity"]),
            state=ProxyState(r["state"]),
            grade=Grade(r["grade"]),
            latency=LatencyProfile(
                samples_ms=samples,
                p50_ms=r["p50_ms"], p95_ms=r["p95_ms"], mean_ms=r["mean_ms"],
                stdev_ms=r["stdev_ms"], success_ratio=r["success_ratio"],
            ),
            source_id=r["source_id"],
            country=r["country"],
            consecutive_failures=r["consecutive_failures"],
            total_successes=r["total_successes"],
            total_attempts=r["total_attempts"],
            first_seen=_parse_dt(r["first_seen"]),
            last_checked=_parse_dt(r["last_checked"]),
            lease_id=r["lease_id"],
            reason_code=r["reason_code"],
        )

    # ── pool membership ───────────────────────────────────────────────────────
    _UPSERT = """
        INSERT INTO proxies (
            fingerprint, host, port, protocol, labelled_protocol, anonymity,
            state, grade, samples_ms, p50_ms, p95_ms, mean_ms, stdev_ms,
            success_ratio, source_id, country, consecutive_failures,
            total_successes, total_attempts, first_seen, last_checked,
            lease_id, reason_code
        ) VALUES (
            :fingerprint, :host, :port, :protocol, :labelled_protocol, :anonymity,
            :state, :grade, :samples_ms, :p50_ms, :p95_ms, :mean_ms, :stdev_ms,
            :success_ratio, :source_id, :country, :consecutive_failures,
            :total_successes, :total_attempts, :first_seen, :last_checked,
            :lease_id, :reason_code
        )
        ON CONFLICT(fingerprint) DO UPDATE SET
            protocol=excluded.protocol,
            labelled_protocol=excluded.labelled_protocol,
            anonymity=excluded.anonymity,
            state=excluded.state,
            grade=excluded.grade,
            samples_ms=excluded.samples_ms,
            p50_ms=excluded.p50_ms,
            p95_ms=excluded.p95_ms,
            mean_ms=excluded.mean_ms,
            stdev_ms=excluded.stdev_ms,
            success_ratio=excluded.success_ratio,
            source_id=excluded.source_id,
            country=excluded.country,
            consecutive_failures=excluded.consecutive_failures,
            total_successes=excluded.total_successes,
            total_attempts=excluded.total_attempts,
            last_checked=excluded.last_checked,
            lease_id=excluded.lease_id,
            reason_code=excluded.reason_code
        -- first_seen is deliberately NOT updated: re-discovering a proxy must not
        -- reset the record of when we first saw it, or age-based decisions would
        -- silently reset every time a source re-lists it.
    """

    def upsert(self, proxy: Proxy) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(self._UPSERT, self._to_row(proxy))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._after_write(1)

    def upsert_many(self, proxies: tuple[Proxy, ...]) -> int:
        """
        Bulk upsert in ONE transaction; returns how many rows were NEWLY inserted.

        One transaction is a correctness property, not just speed: a partial
        discovery batch would leave the pool in a state no single source explains,
        and the count of "new" proxies -- which is how discovery yield is measured
        -- would be wrong in a way nothing could reconstruct.
        """
        if not proxies:
            return 0
        before = self._count_all()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.executemany(self._UPSERT, [self._to_row(p) for p in proxies])
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._after_write(len(proxies))
        return self._count_all() - before

    def get(self, fingerprint: str) -> Proxy | None:
        r = self._db.execute(
            "SELECT * FROM proxies WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return self._from_row(r) if r else None

    def _count_all(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM proxies").fetchone()[0])

    def count_by_state(self) -> dict[ProxyState, int]:
        rows = self._db.execute(
            "SELECT state, COUNT(*) AS n FROM proxies GROUP BY state"
        ).fetchall()
        return {ProxyState(r["state"]): r["n"] for r in rows}

    # ── ADR-036: the rows a scheduler pass must consider ─────────────────────
    def select_schedulable(self, *, limit: int = 1000) -> tuple[Proxy, ...]:
        """
        Candidate rows for a scheduler pass, oldest-checked first.

        THIS IS A CANDIDATE FILTER, NOT THE DECISION. It selects on `state`
        alone -- an indexable predicate -- and leaves every retire/recheck/keep
        call to `core.policy.lifecycle.decide`. The eligibility rule itself
        (`last_checked + cooldown_delay(consecutive_failures) <= now`) is
        deliberately NOT expressed here: `cooldown_delay` is an exponential
        ladder, and reimplementing it in SQL would put the same rule in two
        languages with nothing to keep them equal. ADR-023 is what that costs --
        a guard that had drifted into verifying its own documentation.

        So SQL narrows 50 000 rows to a bounded batch; Python decides. The
        `LEASED` state is excluded because a leased row belongs to its consumer
        and to H3, and `RETIRED` because it is terminal -- selecting either would
        mean the scheduler repeatedly examines rows it must never act on, and at
        the configured `max_pool_size` those are the two categories most likely
        to dominate the table.

        Ordered by `last_checked` ASC with NULLs first, so the least-recently
        verified rows are considered before fresher ones and a never-checked row
        is never starved by a large pool of recently-checked ones. `limit` bounds
        the batch: an unbounded scheduler pass would load the whole pool into
        memory, which is the same unbounded-allocation defect the rate limiter's
        host cap exists to prevent (ADR-034).
        """
        rows = self._db.execute(
            """
            SELECT * FROM proxies
             WHERE state IN ('DISCOVERED', 'COOLING', 'READY', 'PROBING')
             ORDER BY (last_checked IS NULL) DESC, last_checked ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return tuple(self._from_row(r) for r in rows)

    def select_evictable(self, *, limit: int) -> tuple[Proxy, ...]:
        """
        Rows eligible for `max_pool_size` eviction, worst first (ADR-036).

        NEVER returns a `LEASED` row. Evicting a leased proxy would hand the H3
        guarantee to a size limit: the row would vanish while a consumer still
        held it, and `release()` -- an `UPDATE ... WHERE state='LEASED'` -- would
        silently no-op, so the consumer's release would be lost rather than
        rejected. `PROBING` is excluded for the same reason at a smaller scale: a
        probe is in flight and its result would be written back to a row that no
        longer exists.

        Order is worst-first: RETIRED before COOLING before DISCOVERED before
        READY, and within a state the worst grade and slowest p95 go first. A
        cap that evicted arbitrary rows would silently delete the pool's best
        proxies whenever it triggered, which is a size limit behaving as a
        quality regression.
        """
        rows = self._db.execute(
            """
            SELECT * FROM proxies
             WHERE state IN ('RETIRED', 'COOLING', 'DISCOVERED', 'READY')
             ORDER BY CASE state
                        WHEN 'RETIRED'    THEN 0
                        WHEN 'COOLING'    THEN 1
                        WHEN 'DISCOVERED' THEN 2
                        WHEN 'READY'      THEN 3
                      END ASC,
                      CASE grade
                        WHEN 'REJECTED' THEN 0
                        WHEN 'USABLE'   THEN 1
                        WHEN 'GOOD'     THEN 2
                        WHEN 'PRIME'    THEN 3
                        ELSE 0
                      END ASC,
                      p95_ms DESC NULLS FIRST,
                      fingerprint ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return tuple(self._from_row(r) for r in rows)

    def delete_many(self, fingerprints: tuple[str, ...]) -> int:
        """
        Remove rows by fingerprint. Refuses to delete a LEASED row.

        The refusal is in SQL (`AND state != 'LEASED'`) rather than checked in
        Python beforehand, because a check-then-delete has a window: the row
        could be leased between the check and the DELETE. Same reasoning as
        `lease()`'s compare-and-set -- the condition belongs in the statement
        that acts on it. The return value is the number actually deleted, so a
        caller that tried to remove a leased row sees a shortfall rather than a
        silent success.
        """
        if not fingerprints:
            return 0
        self._db.execute("BEGIN IMMEDIATE")
        try:
            marks = ",".join("?" * len(fingerprints))
            cur = self._db.execute(
                f"DELETE FROM proxies WHERE fingerprint IN ({marks}) "
                "AND state != 'LEASED'",
                fingerprints,
            )
            n = cur.rowcount
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        self._after_write(n)
        return n

    def pool_size(self) -> int:
        """Total rows. Named separately from the private `_count_all` so the
        scheduler's `max_pool_size` check reads against a public contract."""
        return self._count_all()

    # ── THE H3 GUARANTEE ──────────────────────────────────────────────────────
    def lease(self, *, count: int, min_grade: Grade, lease_ms: int,
              now: datetime) -> tuple[Proxy, ...]:
        """
        Atomically move up to `count` READY proxies to LEASED and return them.

        THE WHOLE POINT IS THAT THIS IS ONE STATEMENT.

            BEGIN IMMEDIATE            -- write lock taken NOW, not on first write
            UPDATE proxies SET state='LEASED' ...
             WHERE fingerprint IN (SELECT ... WHERE state='READY' ... LIMIT n)
               AND state='READY'       -- re-checked at write time
            RETURNING ...

        The inner SELECT and the UPDATE are evaluated inside a single statement
        holding a write lock, so no other connection can observe or claim these
        rows in between. The redundant-looking outer `AND state='READY'` is the
        compare-and-set: it makes the claim conditional on the state the SELECT
        assumed.

        Ordering by p95 (NULLs last) hands out the best proxies first. Ordering is
        inside the same statement, so "best" is evaluated against the same
        snapshot that is claimed -- not a stale one read earlier.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        if lease_ms <= 0:
            raise ValueError(f"lease_ms must be positive, got {lease_ms}")

        allowed = Grade.at_least(min_grade)
        if not allowed:
            raise ValueError(f"no grade satisfies min_grade={min_grade}")
        placeholders = ",".join("?" for _ in allowed)
        lease_id = uuid.uuid4().hex
        expires = _iso(_add_ms(now, lease_ms))

        sql = f"""
            UPDATE proxies
               SET state = 'LEASED',
                   lease_id = ?,
                   lease_expires_at = ?
             WHERE fingerprint IN (
                       SELECT fingerprint FROM proxies
                        WHERE state = 'READY'
                          AND grade IN ({placeholders})
                        ORDER BY (p95_ms IS NULL), p95_ms ASC, fingerprint ASC
                        LIMIT ?
                   )
               AND state = 'READY'
            RETURNING *
        """
        params = [lease_id, expires, *[g.value for g in allowed], count]

        self._db.execute("BEGIN IMMEDIATE")
        try:
            rows = self._db.execute(sql, params).fetchall()
            if rows:
                self._db.executemany(
                    "INSERT INTO lease_log (lease_id, fingerprint, event, at) "
                    "VALUES (?,?, 'LEASE', ?)",
                    [(lease_id, r["fingerprint"], _iso(now)) for r in rows],
                )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._after_write(len(rows))
        return tuple(self._from_row(r) for r in rows)

    def release(self, fingerprint: str, *, now: datetime) -> None:
        """
        Return a leased proxy to READY.

        Also a compare-and-set (`WHERE state='LEASED'`): releasing something that
        is not leased is a no-op rather than a way to resurrect a RETIRED proxy
        into the leasable pool.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # Read the outgoing lease_id FIRST. RETURNING reports POST-update
            # values, so `SET lease_id=NULL ... RETURNING lease_id` yields NULL --
            # it destroys the identifier it is being asked to report. Reading here
            # is safe precisely because BEGIN IMMEDIATE already holds the write
            # lock: no other connection can change this row before the UPDATE.
            prior = self._db.execute(
                "SELECT lease_id FROM proxies WHERE fingerprint=? AND state='LEASED'",
                (fingerprint,),
            ).fetchone()
            rows = self._db.execute(
                "UPDATE proxies SET state='READY', lease_id=NULL, "
                "lease_expires_at=NULL "
                "WHERE fingerprint=? AND state='LEASED' RETURNING fingerprint",
                (fingerprint,),
            ).fetchall()
            if rows:
                self._db.execute(
                    "INSERT INTO lease_log (lease_id, fingerprint, event, at) "
                    "VALUES (?,?, 'RELEASE', ?)",
                    (prior["lease_id"] if prior else _UNKNOWN_LEASE,
                     fingerprint, _iso(now)),
                )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._after_write(1)

    def expire_leases(self, *, now: datetime) -> int:
        """
        Reclaim leases past their deadline; returns how many were reclaimed.

        Without this, a consumer that is SIGKILLed while holding a lease removes
        that proxy from the pool forever. The legacy design could not even
        represent the problem -- it had no lease -- so the failure mode was
        invisible rather than absent.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # Same RETURNING-reports-post-update trap as release(): capture the
            # (fingerprint, lease_id) pairs BEFORE the UPDATE nulls them, or every
            # EXPIRE row in the audit log records a NULL lease and the log can no
            # longer pair a lease with its termination.
            doomed = self._db.execute(
                "SELECT fingerprint, lease_id FROM proxies "
                " WHERE state='LEASED' AND lease_expires_at IS NOT NULL "
                "   AND lease_expires_at <= ?",
                (_iso(now),),
            ).fetchall()
            rows = self._db.execute(
                "UPDATE proxies SET state='READY', lease_id=NULL, "
                "lease_expires_at=NULL "
                " WHERE state='LEASED' AND lease_expires_at IS NOT NULL "
                "   AND lease_expires_at <= ? "
                "RETURNING fingerprint",
                (_iso(now),),
            ).fetchall()
            if doomed:
                self._db.executemany(
                    "INSERT INTO lease_log (lease_id, fingerprint, event, at) "
                    "VALUES (?,?, 'EXPIRE', ?)",
                    [(r["lease_id"] or _UNKNOWN_LEASE, r["fingerprint"], _iso(now))
                     for r in doomed],
                )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._after_write(len(rows))
        return len(rows)

    # ── H3 audit ──────────────────────────────────────────────────────────────
    def double_delivery_violations(self) -> tuple[tuple[str, int], ...]:
        """
        Fingerprints that were LEASED again before being released or expired.

        This reads the append-only log and reconstructs the sequence per
        fingerprint, so it is an INDEPENDENT check of H3: it does not ask the
        leasing code whether it behaved, it examines what actually happened.
        A test that only called lease() and inspected its return value would be
        asking the accused to testify.
        """
        rows = self._db.execute(
            "SELECT fingerprint, event, at, rowid FROM lease_log "
            "ORDER BY fingerprint, rowid"
        ).fetchall()
        out: list[tuple[str, int]] = []
        current: str | None = None
        held = False
        breaches = 0
        for r in rows:
            if r["fingerprint"] != current:
                if current is not None and breaches:
                    out.append((current, breaches))
                current, held, breaches = r["fingerprint"], False, 0
            if r["event"] == "LEASE":
                if held:
                    breaches += 1
                held = True
            else:
                held = False
        if current is not None and breaches:
            out.append((current, breaches))
        return tuple(out)

    # ── durability ────────────────────────────────────────────────────────────
    def export_text(self, path: str, *, min_grade: Grade) -> int:
        """
        Write a derived text export ATOMICALLY and return the line count.

        `.tmp` in the SAME directory then os.replace(): os.replace is only atomic
        within a filesystem, so writing the temp file to /tmp and moving it here
        would silently degrade to copy-then-truncate across a mount boundary --
        reintroducing exactly the B-04 window this method exists to close.

        fsync before replace, so the content is durable BEFORE it becomes visible.
        Replacing first and syncing after can expose a name whose content is not
        yet on disk -- a crash then leaves an intact filename with a truncated
        body, which is worse than no file at all because it looks valid.
        """
        allowed = Grade.at_least(min_grade)
        placeholders = ",".join("?" for _ in allowed)
        rows = self._db.execute(
            f"SELECT host, port FROM proxies "
            f" WHERE grade IN ({placeholders}) AND state IN ('READY','LEASED') "
            f" ORDER BY (p95_ms IS NULL), p95_ms ASC, host ASC",
            [g.value for g in allowed],
        ).fetchall()

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        body = "".join(f"{r['host']}:{r['port']}\n" for r in rows)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        # Also fsync the DIRECTORY: on POSIX the rename itself is only guaranteed
        # durable once the containing directory's entry is flushed.
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return len(rows)

    def checkpoint(self) -> None:
        """Flush the WAL so an acknowledged write cannot be lost to SIGKILL (H8)."""
        self._db.execute("PRAGMA wal_checkpoint(FULL)")

    def _after_write(self, n: int) -> None:
        self._writes += n
        if self._writes >= 500:          # config.yaml store.checkpoint_every_writes
            self.checkpoint()
            self._writes = 0


def _add_ms(dt: datetime, ms: int) -> datetime:
    from datetime import timedelta
    if dt.tzinfo is None:
        # A naive deadline compared against an aware one raises at runtime, and
        # lease expiry is exactly where that would surface -- under load, in the
        # reclaim path, long after the bad value was stored.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt + timedelta(milliseconds=ms)
