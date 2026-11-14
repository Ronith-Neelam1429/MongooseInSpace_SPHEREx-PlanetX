"""Resumable SQLite queue of survey patches.

One row per HEALPix patch, created by `PatchQueue.init` with its crawl rank.
Workers `claim_next()` the lowest-ranked available patch, process it, then
`complete()` or `fail()` it. All state lives in the database, so a worker can
be killed at any moment and restarted without losing or duplicating work:

  * A claim takes a lease (`lease_s`) that the worker renews while it works.
    If the worker dies, the lease runs out and the next claim by any worker
    sweeps the patch back to pending, counting it as a failed attempt (so a
    patch that crashes the worker every time ends up `failed`, not looping).
  * `fail()` returns a patch to pending with a growing retry delay until
    `max_attempts`, then marks it failed. `release()` (clean shutdown) returns
    it to pending without counting an attempt.
  * Claims run in a `BEGIN IMMEDIATE` transaction on a WAL database, so two
    workers (threads or processes) can never claim the same patch.

Timestamps are Unix seconds (REAL). One database holds one nside.
"""
import json
import math
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from planetx.survey.patches import (
    DEFAULT_NSIDE,
    crawl_order,
    patch_center,
    validate_nside,
)

STATUSES = ("pending", "in_progress", "done", "failed")

DEFAULT_LEASE_S = 300.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_S = 60.0
# How long a connection waits for another writer's lock before raising.
BUSY_TIMEOUT_S = 30.0
MAX_SUMMARY_BYTES = 64 * 1024
MAX_ERROR_CHARS = 2000

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS patches (
        nside         INTEGER NOT NULL,
        ipix          INTEGER NOT NULL,
        rank          INTEGER NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'in_progress', 'done', 'failed')),
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        summary       TEXT,            -- JSON result summary, set when done
        claimed_by    TEXT,            -- worker id holding the lease
        lease_expires REAL,            -- while in_progress
        retry_at      REAL,            -- pending patches wait until this time after a failure
        created_at    REAL    NOT NULL,
        updated_at    REAL    NOT NULL,
        started_at    REAL,            -- last claim
        finished_at   REAL,            -- done or failed
        PRIMARY KEY (nside, ipix)
    )
    """,
    "CREATE INDEX IF NOT EXISTS patches_status_rank ON patches (status, rank)",
)


class LeaseLostError(RuntimeError):
    """The worker no longer holds this patch (its lease expired and it was reclaimed)."""


@dataclass(frozen=True)
class Patch:
    """A claimed patch. `attempts` counts failed attempts before this claim."""

    nside: int
    ipix: int
    rank: int
    attempts: int

    @property
    def center(self) -> tuple[float, float]:
        """ICRS (ra, dec) of the patch centre, degrees."""
        return patch_center(self.ipix, self.nside)


@dataclass(frozen=True)
class QueueStats:
    nside: int | None  # None until the queue is initialised
    counts: dict[str, int]  # every status in STATUSES, zero if absent
    total: int
    current_rank: int | None  # lowest rank not yet done or failed; None when finished
    current_ipix: int | None  # the patch at current_rank


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _positive(name: str, value: float) -> float:
    if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    return value


def _encode_summary(summary: dict[str, Any]) -> str:
    """JSON-encode a result summary, rejecting anything that will not round-trip."""
    if not isinstance(summary, dict):
        raise TypeError(f"summary must be a dict, got {type(summary).__name__}")
    # Strict JSON: NaN/inf and non-JSON types (e.g. numpy scalars) raise here.
    text = json.dumps(summary, allow_nan=False, sort_keys=True)
    if len(text.encode()) > MAX_SUMMARY_BYTES:
        raise ValueError(f"summary is {len(text.encode())} bytes; limit is {MAX_SUMMARY_BYTES}")
    return text


class PatchQueue:
    """Connection to the patch queue database. Not thread-safe: one per thread.

    `readonly=True` opens an existing database without creating or changing
    anything (for the API); it raises FileNotFoundError if the file is missing.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        worker_id: str | None = None,
        lease_s: float = DEFAULT_LEASE_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        clock: Callable[[], float] = time.time,
        readonly: bool = False,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"max_attempts must be an int >= 1, got {max_attempts!r}")
        self.path = Path(path)
        self.worker_id = worker_id or default_worker_id()
        self.lease_s = _positive("lease_s", lease_s)
        self.retry_delay_s = _positive("retry_delay_s", retry_delay_s)
        self.max_attempts = max_attempts
        self.clock = clock
        self.readonly = readonly

        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(f"survey database not found: {self.path}")
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: autocommit; transactions are explicit below.
            self._conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError(f"could not enable WAL mode on {self.path} (got {mode!r})")
            with self._write():
                for statement in _SCHEMA:
                    self._conn.execute(statement)

    # -- connection plumbing -------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self) -> "_ImmediateTransaction":
        if self.readonly:
            raise PermissionError("queue was opened read-only")
        return _ImmediateTransaction(self._conn)

    def _has_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'patches'"
        ).fetchone()
        return row is not None

    # -- setup ---------------------------------------------------------------

    def init(self, nside: int = DEFAULT_NSIDE) -> int:
        """Add every patch at `nside` with its crawl rank. Returns how many were new.

        Idempotent: existing rows (and their progress) are left untouched.
        Raises ValueError if the database already holds a different nside.
        """
        validate_nside(nside)
        order = crawl_order(nside)
        now = self.clock()
        with self._write():
            existing = self.nside()
            if existing is not None and existing != nside:
                raise ValueError(f"{self.path} already holds nside={existing}; refusing to add nside={nside}")
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO patches (nside, ipix, rank, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ((nside, int(ipix), rank, now, now) for rank, ipix in enumerate(order)),
            )
            return self._conn.total_changes - before

    def nside(self) -> int | None:
        """The nside stored in the queue, or None if it has not been initialised."""
        if not self._has_schema():
            return None
        row = self._conn.execute("SELECT nside FROM patches LIMIT 1").fetchone()
        return None if row is None else int(row[0])

    # -- worker operations ---------------------------------------------------

    def claim_next(self) -> Patch | None:
        """Lease the lowest-ranked available patch to this worker, or None if none is.

        Available means pending (and past its retry delay). Expired leases are
        first swept back to pending (or failed) as a lost attempt.
        """
        now = self.clock()
        with self._write():
            self._conn.execute(
                """
                UPDATE patches SET
                    attempts = attempts + 1,
                    last_error = 'lease expired (worker died or stalled)',
                    status = CASE WHEN attempts + 1 >= :max THEN 'failed' ELSE 'pending' END,
                    finished_at = CASE WHEN attempts + 1 >= :max THEN :now ELSE NULL END,
                    retry_at = :now,
                    claimed_by = NULL, lease_expires = NULL, updated_at = :now
                WHERE status = 'in_progress' AND lease_expires <= :now
                """,
                {"max": self.max_attempts, "now": now},
            )
            row = self._conn.execute(
                """
                UPDATE patches SET
                    status = 'in_progress', claimed_by = :worker, lease_expires = :expires,
                    started_at = :now, updated_at = :now
                WHERE rowid = (
                    SELECT rowid FROM patches
                    WHERE status = 'pending' AND (retry_at IS NULL OR retry_at <= :now)
                    ORDER BY rank LIMIT 1
                )
                RETURNING nside, ipix, rank, attempts
                """,
                {"worker": self.worker_id, "expires": now + self.lease_s, "now": now},
            ).fetchone()
        return None if row is None else Patch(*row)

    def renew(self, patch: Patch) -> None:
        """Extend this worker's lease on `patch`. Raises LeaseLostError if it is gone."""
        now = self.clock()
        self._update_owned(
            patch, "lease_expires = :expires, updated_at = :now", {"expires": now + self.lease_s, "now": now}
        )

    def complete(self, patch: Patch, summary: dict[str, Any]) -> None:
        """Mark `patch` done with a small JSON-serialisable result summary.

        Raises TypeError/ValueError for a summary that is not a small JSON
        object (nothing is written), LeaseLostError if this worker lost the patch.
        """
        text = _encode_summary(summary)
        self._update_owned(
            patch,
            "status = 'done', summary = :summary, last_error = NULL, finished_at = :now,"
            " claimed_by = NULL, lease_expires = NULL, retry_at = NULL, updated_at = :now",
            {"summary": text, "now": self.clock()},
        )

    def fail(self, patch: Patch, error: str) -> str:
        """Record a failed attempt. Returns the new status: 'pending' (will retry) or 'failed'.

        A retried patch waits `retry_delay_s * attempts` before it can be claimed
        again, so a short outage upstream does not burn every attempt at once.
        """
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be a non-empty string")
        now = self.clock()
        return self._update_owned(
            patch,
            """
            attempts = attempts + 1,
            last_error = :error,
            status = CASE WHEN attempts + 1 >= :max THEN 'failed' ELSE 'pending' END,
            finished_at = CASE WHEN attempts + 1 >= :max THEN :now ELSE NULL END,
            retry_at = :now + :delay * (attempts + 1),
            claimed_by = NULL, lease_expires = NULL, updated_at = :now
            """,
            {"error": error[:MAX_ERROR_CHARS], "max": self.max_attempts, "now": now, "delay": self.retry_delay_s},
        )

    def release(self, patch: Patch) -> None:
        """Hand `patch` back unprocessed (clean shutdown); not counted as an attempt."""
        self._update_owned(
            patch,
            "status = 'pending', claimed_by = NULL, lease_expires = NULL, updated_at = :now",
            {"now": self.clock()},
        )

    def _update_owned(self, patch: Patch, assignments: str, params: dict[str, Any]) -> str:
        """Apply `assignments` to `patch` if this worker still holds it; return the new status."""
        with self._write():
            row = self._conn.execute(
                f"""
                UPDATE patches SET {assignments}
                WHERE nside = :nside AND ipix = :ipix
                  AND status = 'in_progress' AND claimed_by = :worker
                RETURNING status
                """,
                {**params, "nside": patch.nside, "ipix": patch.ipix, "worker": self.worker_id},
            ).fetchone()
        if row is None:
            raise LeaseLostError(f"patch {patch.ipix} (nside {patch.nside}) is no longer held by {self.worker_id}")
        return row[0]

    def requeue_failed(self) -> int:
        """Return every failed patch to pending with attempts reset. Returns how many."""
        now = self.clock()
        with self._write():
            cursor = self._conn.execute(
                "UPDATE patches SET status = 'pending', attempts = 0, retry_at = NULL,"
                " finished_at = NULL, updated_at = ? WHERE status = 'failed'",
                (now,),
            )
            return cursor.rowcount

    # -- reporting -----------------------------------------------------------

    def stats(self) -> QueueStats:
        """Patch counts per status, plus the crawl frontier."""
        counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
        if not self._has_schema():
            return QueueStats(nside=None, counts=counts, total=0, current_rank=None, current_ipix=None)
        for status, n in self._conn.execute("SELECT status, COUNT(*) FROM patches GROUP BY status"):
            counts[status] = n
        frontier = self._conn.execute(
            "SELECT rank, ipix FROM patches WHERE status IN ('pending', 'in_progress') ORDER BY rank LIMIT 1"
        ).fetchone()
        current_rank, current_ipix = frontier if frontier else (None, None)
        return QueueStats(
            nside=self.nside(), counts=counts, total=sum(counts.values()),
            current_rank=current_rank, current_ipix=current_ipix,
        )

    def done_patches(self) -> list[int]:
        """NESTED indices of all done patches, ascending (a single-order MOC)."""
        if not self._has_schema():
            return []
        return [row[0] for row in self._conn.execute("SELECT ipix FROM patches WHERE status = 'done' ORDER BY ipix")]

    def patch_row(self, nside: int, ipix: int) -> dict[str, Any] | None:
        """Every stored column of one patch (summary decoded), or None if absent."""
        if not self._has_schema():
            return None
        cursor = self._conn.execute("SELECT * FROM patches WHERE nside = ? AND ipix = ?", (nside, ipix))
        row = cursor.fetchone()
        if row is None:
            return None
        record = dict(zip([c[0] for c in cursor.description], row))
        if record["summary"] is not None:
            record["summary"] = json.loads(record["summary"])
        return record


class _ImmediateTransaction:
    """`with` block running inside BEGIN IMMEDIATE (takes the write lock up front).

    Taking the lock at BEGIN, rather than at the first write, means a
    concurrent writer makes us wait (busy timeout) instead of failing with
    SQLITE_BUSY halfway through a read-then-write.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: type[BaseException] | None, *exc: object) -> None:
        self._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
