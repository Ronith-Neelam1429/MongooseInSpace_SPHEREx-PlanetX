"""Offline tests for the survey worker loop and CLI (temp databases only)."""
import dataclasses
import multiprocessing
import os
import signal
import time

import pytest

from planetx.survey.__main__ import main
from planetx.survey.queue import Patch, PatchQueue
from planetx.survey.worker import dry_run, run

NSIDE = 1  # 12 patches


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path) as q:
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


def test_uninitialised_queue_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="init"):
        run(dry_run, tmp_path / "none.sqlite")


def test_processor_exception_fails_the_patch_and_the_loop_continues(db):
    def flaky(patch: Patch) -> dict:
        if patch.rank == 0:
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


def test_invalid_summary_fails_the_patch(db):
    result = run(lambda p: {"bad": float("nan")}, db, max_patches=1, max_attempts=1)
    assert result.failed == 1
    failed = [r for r in rows(db).values() if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["last_error"].startswith("invalid summary")


def test_sigint_finishes_the_current_patch_then_stops(db):
    previous = signal.getsignal(signal.SIGINT)

    def interrupted(patch: Patch) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {"n_visits": 1}

    result = run(interrupted, db)
    assert (result.done, result.aborted, result.reason) == (1, False, "stopped by signal")
    statuses = [r["status"] for r in rows(db).values()]
    assert statuses.count("done") == 1 and statuses.count("pending") == 11
    assert signal.getsignal(signal.SIGINT) is previous


def test_sigterm_aborts_and_releases_the_current_patch(db):
    def terminated(patch: Patch) -> dict:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # interrupted by the handler
        raise AssertionError("not reached")

    started = time.monotonic()
    result = run(terminated, db)
    assert time.monotonic() - started < 4
    assert (result.done, result.failed, result.aborted) == (0, 0, True)
    for row in rows(db).values():
        assert (row["status"], row["attempts"], row["claimed_by"]) == ("pending", 0, None)


def test_heartbeat_keeps_a_slow_patch_leased(db):
    seen_by_other: list[int] = []

    def slow(patch: Patch) -> dict:
        with PatchQueue(db, worker_id="other", lease_s=60) as other:
            for _ in range(4):
                time.sleep(0.25)  # well past the 0.3 s lease without renewal
                p = other.claim_next()
                seen_by_other.append(p.ipix)
        return {"n_visits": 1}

    result = run(slow, db, max_patches=1, lease_s=0.3)
    assert result.done == 1 and result.lease_lost == 0
    done = [ipix for ipix, r in rows(db).items() if r["status"] == "done"]
    assert len(done) == 1
    assert done[0] not in seen_by_other
    assert rows(db)[done[0]]["attempts"] == 0


def _killed_mid_patch(path) -> None:
    def die(patch: Patch) -> dict:
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


def test_cli_rejects_bad_nside(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.sqlite"), "init", "--nside", "3"]) == 2
    assert "power of two" in capsys.readouterr().err


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
