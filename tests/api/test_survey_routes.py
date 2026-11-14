"""Offline tests for the /api/survey endpoints against temp databases."""
import os

import pytest
from fastapi.testclient import TestClient

from planetx.api.app import app
from planetx.api.routes.survey import get_db_path
from planetx.survey.patches import NEP, crawl_order
from planetx.survey.queue import PatchQueue

client = TestClient(app)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "survey.sqlite"
    app.dependency_overrides[get_db_path] = lambda: path
    yield path
    app.dependency_overrides.pop(get_db_path, None)


def test_status_without_database(db_path):
    resp = client.get("/api/survey/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["nside"] is None and body["current_rank"] is None
    assert body["counts"] == {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
    assert body["readable"] is True
    # The API never creates the database file. (Reading an existing WAL
    # database may leave -wal/-shm files beside it; that is SQLite's doing.)
    assert not db_path.exists()


def test_coverage_without_database(db_path):
    resp = client.get("/api/survey/coverage")
    assert resp.status_code == 200
    assert resp.json() == {"6": []}
    assert not db_path.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unopenable_database_reports_zeros_not_500(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    # After the last writer closes there are no -wal/-shm files; in a
    # read-only directory SQLite cannot create them, so the open fails.
    os.chmod(db_path.parent, 0o555)
    try:
        status = client.get("/api/survey/status")
        coverage = client.get("/api/survey/coverage")
    finally:
        os.chmod(db_path.parent, 0o755)
    assert status.status_code == 200
    assert status.json()["readable"] is False and status.json()["total"] == 0
    assert coverage.status_code == 200 and coverage.json() == {"6": []}


def test_status_of_an_empty_schema(db_path):
    PatchQueue(db_path, create=True).close()  # schema, no patches
    assert client.get("/api/survey/status").json()["total"] == 0
    assert client.get("/api/survey/coverage").json() == {"6": []}


def test_status_and_coverage_after_progress(db_path):
    order = crawl_order(2)
    with PatchQueue(db_path, create=True) as q:
        q.init(2)
        first, second, third = q.claim_next(), q.claim_next(), q.claim_next()
        q.complete(first, {"n_visits": 4})
        q.complete(third, {"n_visits": 2})
        q.fail(second, "IRSA 503")  # back to pending: the frontier stays at rank 1

    status = client.get("/api/survey/status").json()
    assert status["nside"] == 2 and status["moc_order"] == 1
    assert status["total"] == 48
    assert status["counts"] == {"pending": 46, "in_progress": 0, "done": 2, "failed": 0}
    assert status["current_rank"] == 1
    assert 0 < status["current_radius_deg"] < 90
    assert -90 <= status["current_dec"] <= 90

    coverage = client.get("/api/survey/coverage").json()
    assert coverage == {"1": sorted([int(order[0]), int(order[2])])}


def test_status_when_finished(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
        while (p := q.claim_next()) is not None:
            q.complete(p, {})
    status = client.get("/api/survey/status").json()
    assert status["counts"]["done"] == 12
    assert status["current_rank"] is None and status["current_radius_deg"] is None
    assert client.get("/api/survey/coverage").json() == {"0": list(range(12))}


def test_frontier_starts_at_the_nep(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(64)
    status = client.get("/api/survey/status").json()
    assert status["current_rank"] == 0
    assert status["current_radius_deg"] < 1
    assert abs(status["current_dec"] - NEP.dec.deg) < 1
