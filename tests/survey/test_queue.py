"""Offline tests for the SQLite patch queue (temp databases only)."""
import multiprocessing
import sqlite3
import threading
import time

import pytest

from planetx.survey import queue as queue_module
from planetx.survey.patches import crawl_order
from planetx.survey.queue import LeaseLostError, NsideMismatchError, PatchQueue

NSIDE = 2  # 48 patches: fast, but still a real crawl order


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sub" / "survey.sqlite"  # parent dir does not exist yet
    with PatchQueue(path, create=True) as q:
        q.init(NSIDE)
    return path


@pytest.fixture
def clock():
    return FakeClock()


def open_queue(path, clock, **kw):
    return PatchQueue(path, clock=clock, **kw)


def test_init_creates_parent_dir_and_all_patches(db):
    with PatchQueue(db) as q:
        stats = q.stats()
        assert q.nside() == NSIDE
        assert q.patch_row(NSIDE, int(crawl_order(NSIDE)[0]))["rank"] == 0
    assert stats.total == 48
    assert stats.counts == {"pending": 48, "in_progress": 0, "done": 0, "failed": 0}
    assert stats.current_rank == 0
    assert stats.current_ipix == crawl_order(NSIDE)[0]


def test_missing_database_is_not_created_without_create(tmp_path):
    path = tmp_path / "nested" / "survey.sqlite"
    with pytest.raises(FileNotFoundError):
        PatchQueue(path)
    assert not path.parent.exists()


def test_opening_an_existing_database_takes_no_write_lock(db, monkeypatch):
    # The heartbeat opens a connection per patch; that must not wait on writers.
    monkeypatch.setattr(queue_module, "BUSY_TIMEOUT_S", 1.0)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with PatchQueue(db) as q:
            assert q.stats().total == 48
        assert time.monotonic() - started < 0.9
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_database_uses_wal(db):
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_init_is_idempotent_and_keeps_progress(db, clock):
    with open_queue(db, clock) as q:
        p = q.claim_next()
        q.complete(p, {"n_visits": 4})
        assert q.init(NSIDE) == 0
        stats = q.stats()
    assert stats.total == 48
    assert stats.counts["done"] == 1
    with PatchQueue(db) as q:
        assert q.patch_row(NSIDE, p.ipix)["summary"] == {"n_visits": 4}


def test_init_default_nside_twice(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path, create=True) as q:
        assert q.init() == 49152
        assert q.init() == 0
        assert q.stats().total == 49152


def test_init_refuses_a_second_nside(db):
    with PatchQueue(db) as q, pytest.raises(NsideMismatchError, match="nside=2"):
        q.init(4)


def test_claims_follow_rank_order(db, clock):
    order = crawl_order(NSIDE)
    with open_queue(db, clock) as q:
        claimed = [q.claim_next() for _ in range(3)]
        assert [p.rank for p in claimed] == [0, 1, 2]
        assert [p.ipix for p in claimed] == list(order[:3])
        assert all(p.nside == NSIDE and p.attempts == 0 for p in claimed)
        assert q.stats().counts["in_progress"] == 3


def test_claim_on_empty_queue_returns_none(tmp_path):
    with PatchQueue(tmp_path / "empty.sqlite", create=True) as q:
        assert q.claim_next() is None
        assert q.stats().total == 0


def test_complete(db, clock):
    with open_queue(db, clock) as q:
        p = q.claim_next()
        q.complete(p, {"n_visits": 3, "bands": [1, 2]})
        row = q.patch_row(p.nside, p.ipix)
        assert row["status"] == "done"
        assert row["summary"] == {"n_visits": 3, "bands": [1, 2]}
        assert row["claimed_by"] is None and row["lease_expires"] is None
        assert row["finished_at"] == clock.t
        assert q.done_patches() == [p.ipix]
        assert q.stats().current_rank == 1
        with pytest.raises(LeaseLostError):
            q.complete(p, {})  # already done


def test_patches_with_status(db, clock):
    order = [int(i) for i in crawl_order(NSIDE)]
    with open_queue(db, clock) as q:
        q.complete(q.claim_next(), {})
        running = q.claim_next()
        assert q.patches_with_status(("done",)) == q.done_patches() == [order[0]]
        assert q.patches_with_status(("in_progress",)) == [running.ipix] == [order[1]]
        assert q.patches_with_status(("pending", "in_progress")) == sorted(order[1:])
        # A limit keeps the lowest crawl ranks, returned in ipix order.
        assert q.patches_with_status(("pending",), limit=4) == sorted(order[2:6])
        assert q.patches_with_status(("failed",)) == []
        for bad in [(), ("queued",)]:
            with pytest.raises(ValueError):
                q.patches_with_status(bad)
        with pytest.raises(ValueError):
            q.patches_with_status(("done",), limit=0)


@pytest.mark.parametrize("summary, exc", [
    ([1, 2], TypeError),
    ({"x": float("nan")}, ValueError),
    ({"x": object()}, TypeError),
    ({"x": "a" * 70_000}, ValueError),
])
def test_complete_rejects_bad_summary_without_writing(db, clock, summary, exc):
    with open_queue(db, clock) as q:
        p = q.claim_next()
        with pytest.raises(exc):
            q.complete(p, summary)
        assert q.patch_row(p.nside, p.ipix)["status"] == "in_progress"


def test_fail_retries_after_delay_then_marks_failed(db, clock):
    with open_queue(db, clock, max_attempts=3, retry_delay_s=60) as q:
        p = q.claim_next()
        assert q.fail(p, "IRSA 503") == "pending"
        row = q.patch_row(p.nside, p.ipix)
        assert (row["attempts"], row["last_error"]) == (1, "IRSA 503")

        # Not claimable during its retry delay; the next patch is handed out instead.
        other = q.claim_next()
        assert other.ipix != p.ipix
        q.release(other)

        clock.t += 60
        again = q.claim_next()
        assert (again.ipix, again.attempts) == (p.ipix, 1)
        assert q.fail(again, "IRSA 503") == "pending"

        clock.t += 120  # delay grows with attempts
        third = q.claim_next()
        assert (third.ipix, third.attempts) == (p.ipix, 2)
        assert q.fail(third, "boom") == "failed"
        row = q.patch_row(p.nside, p.ipix)
        assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 3, "boom")

        clock.t += 10_000
        assert q.claim_next().ipix != p.ipix  # failed patches are never claimed
        assert q.stats().counts["failed"] == 1


def test_fail_requires_an_error_message(db, clock):
    with open_queue(db, clock) as q:
        p = q.claim_next()
        with pytest.raises(ValueError):
            q.fail(p, "  ")


def test_requeue_failed(db, clock):
    with open_queue(db, clock, max_attempts=1) as q:
        p = q.claim_next()
        q.fail(p, "boom")
        assert q.requeue_failed() == 1
        row = q.patch_row(p.nside, p.ipix)
        assert (row["status"], row["attempts"]) == ("pending", 0)
        assert q.claim_next().ipix == p.ipix


def test_release_does_not_count_an_attempt(db, clock):
    with open_queue(db, clock) as q:
        p = q.claim_next()
        q.release(p)
        again = q.claim_next()
        assert (again.ipix, again.attempts) == (p.ipix, 0)


def test_expired_lease_is_reclaimed_as_a_lost_attempt(db, clock):
    dead = open_queue(db, clock, worker_id="dead", lease_s=300)
    p = dead.claim_next()
    dead.close()  # the worker "crashes" mid-patch

    with open_queue(db, clock, worker_id="alive", lease_s=300) as q:
        clock.t += 299
        assert q.claim_next().ipix != p.ipix  # lease still valid
        clock.t += 1
        again = q.claim_next()
        assert (again.ipix, again.attempts) == (p.ipix, 1)
        row = q.patch_row(p.nside, p.ipix)
        assert row["claimed_by"] == "alive"
        assert "lease expired" in row["last_error"]
        q.complete(again, {"n_visits": 1})


def test_repeatedly_expiring_patch_ends_failed(db, clock):
    for attempt in range(2):
        with open_queue(db, clock, worker_id=f"w{attempt}", max_attempts=2, lease_s=10) as q:
            assert q.claim_next().rank == 0
        clock.t += 10
    with open_queue(db, clock, worker_id="last", max_attempts=2, lease_s=10) as q:
        assert q.claim_next().rank == 1  # sweep marked rank 0 failed
        assert q.stats().counts["failed"] == 1


def test_stats_count_expired_leases_as_pending(db, clock):
    with open_queue(db, clock, worker_id="dead", lease_s=10) as q:
        q.claim_next()
        assert q.stats().counts["in_progress"] == 1
        clock.t += 10
        stats = q.stats()
    assert stats.counts["in_progress"] == 0 and stats.counts["pending"] == 48
    assert stats.current_rank == 0


def test_complete_accepts_a_swept_but_unclaimed_patch(db, clock):
    a = open_queue(db, clock, worker_id="a", lease_s=10)
    b = open_queue(db, clock, worker_id="b", lease_s=10)
    p0, p1 = a.claim_next(), a.claim_next()
    clock.t += 11
    assert b.claim_next().ipix == p0.ipix  # sweeps both, reclaims rank 0 only
    assert a.patch_row(NSIDE, p1.ipix)["status"] == "pending"
    a.complete(p1, {"n_visits": 2})  # nobody else holds it: keep the result
    assert a.patch_row(NSIDE, p1.ipix)["status"] == "done"
    with pytest.raises(LeaseLostError):
        a.complete(p0, {})  # b holds it now
    a.close()
    b.close()


def test_complete_accepts_a_patch_swept_to_failed(db, clock):
    with open_queue(db, clock, worker_id="a", lease_s=10, max_attempts=1) as a:
        p0, p1 = a.claim_next(), a.claim_next()
        clock.t += 11
        assert a.claim_next().rank == 2  # the sweep marked ranks 0 and 1 failed
        a.complete(p0, {"n_visits": 1})
        assert a.patch_row(NSIDE, p0.ipix)["status"] == "done"
        assert a.patch_row(NSIDE, p1.ipix)["status"] == "failed"


def test_lost_lease_cannot_be_completed(db, clock):
    a = open_queue(db, clock, worker_id="a", lease_s=10)
    b = open_queue(db, clock, worker_id="b", lease_s=10)
    p = a.claim_next()
    clock.t += 11
    assert b.claim_next().ipix == p.ipix
    with pytest.raises(LeaseLostError):
        a.complete(p, {})
    with pytest.raises(LeaseLostError):
        a.renew(p)
    with pytest.raises(LeaseLostError):
        a.fail(p, "x")
    b.complete(p, {"by": "b"})
    a.close()
    b.close()


def test_renew_extends_the_lease(db, clock):
    with open_queue(db, clock, worker_id="a", lease_s=10) as a, open_queue(db, clock, worker_id="b") as b:
        p = a.claim_next()
        clock.t += 9
        a.renew(p)
        clock.t += 9  # 18 s after the claim, 9 s after the renewal
        assert b.claim_next().ipix != p.ipix
        a.complete(p, {})


def test_readonly_open(db, tmp_path):
    with PatchQueue(db, readonly=True) as q:
        assert q.stats().total == 48
        with pytest.raises(PermissionError):
            q.claim_next()
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        PatchQueue(missing, readonly=True)
    assert not missing.exists()


@pytest.mark.parametrize("kw", [{"lease_s": 0}, {"lease_s": float("nan")}, {"max_attempts": 0},
                                {"retry_delay_s": -1}, {"max_attempts": True}])
def test_invalid_settings_rejected(tmp_path, kw):
    with pytest.raises(ValueError):
        PatchQueue(tmp_path / "x.sqlite", **kw)


def test_readonly_and_create_are_exclusive(tmp_path):
    with pytest.raises(ValueError):
        PatchQueue(tmp_path / "x.sqlite", readonly=True, create=True)


def _claim_all(path, worker_id: str) -> list[int]:
    claimed = []
    with PatchQueue(path, worker_id=worker_id) as q:
        while (p := q.claim_next()) is not None:
            claimed.append(p.ipix)
    return claimed


def _assert_disjoint_and_complete(results: list[list[int]]) -> None:
    flat = [ipix for r in results for ipix in r]
    assert len(flat) == len(set(flat)), "a patch was claimed twice"
    assert sorted(flat) == list(range(48))


def test_concurrent_threads_never_claim_the_same_patch(db):
    results: list[list[int]] = [[] for _ in range(8)]
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = _claim_all(db, f"t{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _assert_disjoint_and_complete(results)


def test_concurrent_processes_never_claim_the_same_patch(db):
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        results = pool.starmap(_claim_all, [(db, f"p{i}") for i in range(4)])
    _assert_disjoint_and_complete(results)
