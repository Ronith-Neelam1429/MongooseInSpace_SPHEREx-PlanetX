"""Offline tests for the survey worker loop and CLI (temp databases only)."""
import dataclasses
import multiprocessing
import os
import signal
import sqlite3
import threading
import time

import pytest

from planetx.survey import worker
from planetx.survey.__main__ import main
from planetx.survey.queue import LeaseLostError, PatchQueue
from planetx.survey.worker import PatchAbandoned, PatchContext, _StopFlag, dry_run, run

NSIDE = 1  # 12 patches


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path, create=True) as q:
        q.init(NSIDE)
    return path


def rows(path):
    with PatchQueue(path) as q:
        return {ipix: q.patch_row(NSIDE, ipix) for ipix in range(12)}


def test_dry_run_drains_the_queue(db):
    result = run(dry_run, db)
    assert (result.done, result.failed, result.aborted) == (12, 0, False)
    assert result.reason == "queue drained"
    for row in rows(db).values():
        assert row["status"] == "done"
        assert row["summary"]["dry_run"] is True


def test_max_patches(db):
    result = run(dry_run, db, max_patches=2)
    assert result.done == 2
    with PatchQueue(db) as q:
        assert q.stats().counts["pending"] == 10


@pytest.mark.parametrize("kw", [{"max_patches": 0}, {"max_patches": True}, {"idle_poll_s": 0}])
def test_invalid_run_settings_rejected(db, kw):
    with pytest.raises(ValueError):
        run(dry_run, db, **kw)


def test_missing_database_is_an_error_and_not_created(tmp_path):
    path = tmp_path / "nested" / "none.sqlite"
    with pytest.raises(FileNotFoundError):
        run(dry_run, path)
    assert not path.parent.exists()


def test_uninitialised_queue_is_an_error(tmp_path):
    path = tmp_path / "empty.sqlite"
    PatchQueue(path, create=True).close()
    with pytest.raises(RuntimeError, match="init"):
        run(dry_run, path)


def test_processor_exception_fails_the_patch_and_the_loop_continues(db):
    def flaky(ctx: PatchContext) -> dict:
        if ctx.patch.rank == 0:
            raise ConnectionError("IRSA 503")
        return {"n_visits": 3}

    result = run(flaky, db, max_attempts=3, retry_delay_s=0.01, idle_poll_s=0.01)
    assert result.done == 11
    assert result.failed == 3  # rank 0 tried three times
    by_rank = {r["rank"]: r for r in rows(db).values()}
    assert by_rank[0]["status"] == "failed"
    assert by_rank[0]["attempts"] == 3
    assert by_rank[0]["last_error"] == "ConnectionError: IRSA 503"
    assert all(by_rank[k]["status"] == "done" for k in range(1, 12))


class _Transient(Exception):
    """Shaped like archive.UpstreamError for an IRSA 5xx."""

    transient = True


def test_upstream_outage_releases_the_patch_without_an_attempt_and_waits(db, monkeypatch):
    waits = []
    monkeypatch.setattr(_StopFlag, "__init__", _recording_wait_init(waits))
    calls = {"n": 0}

    def outage_then_up(ctx: PatchContext) -> dict:
        calls["n"] += 1
        if calls["n"] <= 3:  # IRSA down for the first three claims
            raise _Transient("SIA query spherex_dr1 failed after 3 attempt(s): HTTP 502")
        return {"ok": True}

    result = run(outage_then_up, db, max_attempts=1, outage_waits_s=(1.0, 2.0), idle_poll_s=0.01)
    assert (result.done, result.failed, result.outages) == (12, 0, 3)
    assert waits == [1.0, 2.0, 2.0]  # doubling, capped at the last wait
    by_rank = {r["rank"]: r for r in rows(db).values()}
    assert by_rank[0]["status"] == "done"
    assert by_rank[0]["attempts"] == 0  # max_attempts=1 would have failed it on the first 502


def test_a_patch_that_always_hits_transient_errors_still_fails_eventually(db, monkeypatch):
    monkeypatch.setattr(_StopFlag, "__init__", _recording_wait_init([]))

    def rank0_stuck(ctx: PatchContext) -> dict:
        if ctx.patch.rank == 0:
            raise _Transient("truncated FITS")
        return {"ok": True}

    result = run(rank0_stuck, db, max_attempts=2, retry_delay_s=0.01, outage_waits_s=(0.01,), idle_poll_s=0.01)
    by_rank = {r["rank"]: r for r in rows(db).values()}
    assert by_rank[0]["status"] == "failed"
    assert by_rank[0]["attempts"] == 2
    assert result.done == 11
    assert result.outages == 2 * worker.OUTAGE_RELEASES_PER_ATTEMPT


def test_a_non_transient_upstream_error_counts_an_attempt(db):
    class _Permanent(Exception):
        transient = False

    def bad_request(ctx: PatchContext) -> dict:
        if ctx.patch.rank == 0:
            raise _Permanent("HTTP 400")
        return {"ok": True}

    result = run(bad_request, db, max_attempts=1, idle_poll_s=0.01)
    assert (result.failed, result.outages) == (1, 0)
    assert {r["rank"]: r for r in rows(db).values()}[0]["status"] == "failed"


@pytest.mark.parametrize("waits", [(), (0.0,), (1.0, -1.0)])
def test_invalid_outage_waits_rejected(db, waits):
    with pytest.raises(ValueError):
        run(dry_run, db, outage_waits_s=waits)


def _recording_wait_init(waits):
    """_StopFlag.__init__ whose stop.wait records the timeout instead of sleeping on it."""
    original = _StopFlag.__init__

    def init(self):
        original(self)
        event = self.stop

        class _Recording(threading.Event):
            def wait(self, timeout=None):
                if timeout is not None and timeout >= 0.5:
                    waits.append(timeout)
                    return event.is_set()
                return event.wait(timeout)

            def set(self):
                event.set()

            def is_set(self):
                return event.is_set()

        self.stop = _Recording()

    return init


def test_invalid_summary_fails_the_patch(db):
    result = run(lambda ctx: {"bad": float("nan")}, db, max_patches=1, max_attempts=1)
    assert result.failed == 1
    failed = [r for r in rows(db).values() if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["last_error"].startswith("invalid summary")


def test_abandoned_patch_is_released_without_an_attempt(db):
    def gives_up(ctx: PatchContext) -> dict:
        raise PatchAbandoned("upstream says come back later")

    result = run(gives_up, db, max_patches=1)
    assert (result.released, result.failed, result.done) == (1, 0, 0)
    for row in rows(db).values():
        assert (row["status"], row["attempts"]) == ("pending", 0)


def test_sigint_finishes_the_current_patch_then_stops(db):
    previous = signal.getsignal(signal.SIGINT)
    seen: list[bool] = []

    def interrupted(ctx: PatchContext) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        seen.append(ctx.should_stop())  # the first Ctrl-C means "finish this one"
        return {"n_visits": 1}

    result = run(interrupted, db)
    assert (result.done, result.aborted, result.reason) == (1, False, "stopped by signal")
    assert seen == [False]
    statuses = [r["status"] for r in rows(db).values()]
    assert statuses.count("done") == 1 and statuses.count("pending") == 11
    assert signal.getsignal(signal.SIGINT) is previous


def test_sigterm_aborts_and_releases_the_current_patch(db):
    def terminated(ctx: PatchContext) -> dict:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # interrupted by the handler
        raise AssertionError("not reached")

    started = time.monotonic()
    result = run(terminated, db)
    assert time.monotonic() - started < 4
    assert (result.done, result.failed, result.aborted) == (0, 0, True)
    for row in rows(db).values():
        assert (row["status"], row["attempts"], row["claimed_by"]) == ("pending", 0, None)


def test_sigterm_during_complete_keeps_the_finished_result(db, monkeypatch):
    original = PatchQueue.complete

    def complete_then_signal(self, patch, summary):
        os.kill(os.getpid(), signal.SIGTERM)  # arrives while recording the result
        original(self, patch, summary)

    monkeypatch.setattr(PatchQueue, "complete", complete_then_signal)
    result = run(dry_run, db)
    assert (result.done, result.aborted, result.reason) == (1, False, "stopped by signal")
    statuses = [r["status"] for r in rows(db).values()]
    assert statuses.count("done") == 1 and statuses.count("pending") == 11


def test_sigterm_while_idle_stops_promptly(db):
    with PatchQueue(db, worker_id="other", lease_s=600) as other:
        while other.claim_next() is not None:  # everything is in progress elsewhere
            pass
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        result = run(dry_run, db, idle_poll_s=30)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
    assert (result.done, result.aborted, result.reason) == (0, False, "stopped by signal")


def test_restore_falls_back_to_default_for_unknown_handlers():
    original = signal.getsignal(signal.SIGUSR1)
    try:
        flag = _StopFlag()
        flag._previous = {signal.SIGUSR1: None}  # what signal.signal() returns for C-level handlers
        flag.restore()
        assert signal.getsignal(signal.SIGUSR1) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGUSR1, original)


def test_heartbeat_keeps_a_slow_patch_leased(db, monkeypatch):
    renewals = 0
    renewed_past_lease = threading.Event()
    original = PatchQueue.renew

    def counting_renew(self, patch):
        nonlocal renewals
        original(self, patch)
        renewals += 1
        if renewals >= 8:  # 8 renewals at lease/5 intervals: well past one full lease
            renewed_past_lease.set()

    monkeypatch.setattr(PatchQueue, "renew", counting_renew)
    stolen: list[int] = []
    leased: list[bool] = []

    def slow(ctx: PatchContext) -> dict:
        assert renewed_past_lease.wait(20)
        with PatchQueue(db, worker_id="other", lease_s=60) as other:
            while (p := other.claim_next()) is not None:
                stolen.append(p.ipix)
            row = other.patch_row(NSIDE, ctx.patch.ipix)
            leased.append(row["claimed_by"] != "other" and row["lease_expires"] > time.time())
        return {"n_visits": 1}

    result = run(slow, db, max_patches=1, lease_s=1.0)
    assert result.done == 1 and result.lease_lost == 0
    done = [ipix for ipix, r in rows(db).items() if r["status"] == "done"]
    assert len(done) == 1 and done[0] not in stolen
    assert len(stolen) == 11 and leased == [True]
    assert rows(db)[done[0]]["attempts"] == 0


def test_heartbeat_connection_failure_is_reported_to_the_processor(db, monkeypatch):
    def broken_open(queue):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_open_heartbeat_queue", broken_open)
    seen: list[bool] = []

    def waits_for_trouble(ctx: PatchContext) -> dict:
        deadline = time.monotonic() + 10
        while not ctx.should_stop() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.append(ctx.lease_lost())
        raise PatchAbandoned("lease lost")

    result = run(waits_for_trouble, db, max_patches=1, lease_s=0.5)
    assert seen == [True]
    assert result.released == 1  # nobody reclaimed it yet, so it is handed back cleanly
    for row in rows(db).values():
        assert (row["status"], row["attempts"]) == ("pending", 0)


def test_lease_taken_by_another_worker_is_reported(db, monkeypatch):
    def lost(self, patch):
        raise LeaseLostError("taken")

    monkeypatch.setattr(PatchQueue, "renew", lost)
    seen: list[bool] = []

    def waits_for_lease_loss(ctx: PatchContext) -> dict:
        deadline = time.monotonic() + 10
        while not ctx.lease_lost() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.append(ctx.lease_lost())
        return {"n_visits": 1}

    result = run(waits_for_lease_loss, db, max_patches=1, lease_s=0.5)
    assert seen == [True]
    assert result.done == 1  # renew was faked; the DB still says we hold it


def _killed_mid_patch(path) -> None:
    def die(ctx: PatchContext) -> dict:
        os.kill(os.getpid(), signal.SIGKILL)
        return {}

    run(die, path, lease_s=30)


def test_patch_survives_a_hard_kill(db):
    proc = multiprocessing.get_context("spawn").Process(target=_killed_mid_patch, args=(db,))
    proc.start()
    proc.join(60)
    assert proc.exitcode == -signal.SIGKILL

    stuck = [r for r in rows(db).values() if r["status"] == "in_progress"]
    assert len(stuck) == 1 and stuck[0]["rank"] == 0

    later = time.time() + 31  # after the dead worker's lease
    with PatchQueue(db, clock=lambda: later) as q:
        p = q.claim_next()
        assert (p.rank, p.attempts) == (0, 1)
        q.complete(p, {"n_visits": 2})


def test_cli_init_status_run(tmp_path, capsys):
    path = tmp_path / "cli.sqlite"
    assert main(["--db", str(path), "status"]) == 1
    assert main(["--db", str(path), "init", "--nside", "1"]) == 0
    assert main(["--db", str(path), "init", "--nside", "1"]) == 0
    assert "added 0 patches" in capsys.readouterr().out
    assert main(["--db", str(path), "run", "--processor", "dry-run", "--max-patches", "3"]) == 0
    assert main(["--db", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "done               3" in out and "frontier: rank 3" in out
    assert main(["--db", str(path), "requeue-failed"]) == 0


def test_cli_rejects_bad_nside(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "x.sqlite"), "init", "--nside", "3"])
    assert exc.value.code == 2
    assert "power of two" in capsys.readouterr().err
    assert not (tmp_path / "x.sqlite").exists()


def test_cli_init_refuses_a_second_nside(db, capsys):
    assert main(["--db", str(db), "init", "--nside", "2"]) == 2
    assert "nside=1" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["run", "--processor", "dry-run"], ["requeue-failed"]])
def test_cli_commands_do_not_create_a_missing_database(tmp_path, capsys, command):
    path = tmp_path / "nested" / "none.sqlite"
    assert main(["--db", str(path), *command]) == 1
    assert "not found" in capsys.readouterr().err
    assert not path.parent.exists()


def test_cli_run_requires_a_processor(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.sqlite"), "run"])


def test_cli_dry_run_refuses_the_real_survey_db(monkeypatch, tmp_path, capsys):
    from planetx.survey import __main__ as cli

    real = tmp_path / "real.sqlite"
    monkeypatch.setattr(cli, "settings", dataclasses.replace(cli.settings, db_path=real))
    assert cli.main(["run", "--processor", "dry-run"]) == 2
    assert "scratch database" in capsys.readouterr().err
    assert not real.exists()
