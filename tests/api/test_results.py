"""Offline tests for /api/results: the file allow-list, missing files, and live survey numbers."""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from planetx.api.app import app
from planetx.api.routes import results
from planetx.api.routes.results import RESULT_NAMES, get_results_dir
from planetx.api.routes.survey import get_db_path
from planetx.config import Settings
from planetx.survey.candidates import Candidate, CandidateStore, TrackPoint
from planetx.survey.queue import Patch, PatchQueue

FIXTURES = Path(__file__).parents[1] / "fixtures" / "results"
client = TestClient(app)


@pytest.fixture
def results_dir(tmp_path):
    path = tmp_path / "results"
    path.mkdir()
    app.dependency_overrides[get_results_dir] = lambda: path
    yield path
    app.dependency_overrides.pop(get_results_dir, None)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "survey.sqlite"
    app.dependency_overrides[get_db_path] = lambda: path
    yield path
    app.dependency_overrides.pop(get_db_path, None)


def test_fixtures_cover_every_result_and_follow_the_header_contract():
    for name in RESULT_NAMES:
        doc = json.loads((FIXTURES / f"{name}.json").read_text())
        assert doc["name"] == name and doc["version"] == 1
        assert doc["generated_at"].endswith("Z") and doc["command"].startswith("uv run")
        assert isinstance(doc["notes"], list)


def test_index_says_not_run_yet_for_missing_files(results_dir):
    body = client.get("/api/results").json()
    assert [r["name"] for r in body["results"]] == list(RESULT_NAMES)
    assert all(r["available"] is False and r["generated_at"] is None and r["error"] is None for r in body["results"])


def test_index_and_file_once_a_result_exists(results_dir):
    shutil.copy(FIXTURES / "false_movers.json", results_dir)
    index = {r["name"]: r for r in client.get("/api/results").json()["results"]}
    assert index["false_movers"]["available"] is True
    assert index["false_movers"]["generated_at"] == "2026-11-14T22:00:00Z"
    assert index["false_movers"]["version"] == 1
    assert index["gaia_validation"]["available"] is False

    resp = client.get("/api/results/false_movers")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == json.loads((FIXTURES / "false_movers.json").read_text())


def test_missing_file_is_404_not_run_yet(results_dir):
    resp = client.get("/api/results/gaia_validation")
    assert resp.status_code == 404
    assert "not been run yet" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["secrets", "gaia_validation.json", "..", "survey", "GAIA_VALIDATION", ".gitkeep"])
def test_names_outside_the_allow_list_are_404(results_dir, name):
    (results_dir / f"{name}.json").write_text("{}")  # present on disk, still not served
    resp = client.get(f"/api/results/{name}")
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/api/results/..%2Fpyproject", "/api/results/%2e%2e%2fsurvey", "/api/results/../app"])
def test_path_traversal_is_refused(results_dir, path):
    (results_dir.parent / "pyproject.json").write_text('{"leak": true}')
    resp = client.get(path)
    assert resp.status_code == 404
    assert "leak" not in resp.text


def test_a_half_written_file_is_reported_not_served(results_dir):
    (results_dir / "brown_dwarfs.json").write_text('{"name": "brown_dwarfs", "objects": [')
    index = {r["name"]: r for r in client.get("/api/results").json()["results"]}
    assert index["brown_dwarfs"]["available"] is False
    assert "not valid JSON" in index["brown_dwarfs"]["error"]
    resp = client.get("/api/results/brown_dwarfs")
    assert resp.status_code == 503

    # Once the tool finishes writing, the new contents are served (the cache follows the file).
    shutil.copy(FIXTURES / "brown_dwarfs.json", results_dir)
    os.utime(results_dir / "brown_dwarfs.json", ns=(time.time_ns(), time.time_ns() + 10**9))
    assert client.get("/api/results/brown_dwarfs").status_code == 200


def test_a_json_array_is_not_a_result(results_dir):
    (results_dir / "injection_recovery.json").write_text("[1, 2]")
    assert client.get("/api/results/injection_recovery").status_code == 503


def test_oversized_file_is_refused(results_dir, monkeypatch):
    monkeypatch.setattr(results, "MAX_RESULT_BYTES", 10)
    (results_dir / "light_curve_r_dra.json").write_text('{"name": "light_curve_r_dra", "points": []}')
    assert client.get("/api/results/light_curve_r_dra").status_code == 503


def test_results_dir_comes_from_the_setting_when_set(monkeypatch, tmp_path):
    # The Docker image installs the package non-editable and sets PLANETX_RESULTS_DIR=/app/results.
    monkeypatch.setenv("PLANETX_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(results, "settings", Settings())
    assert get_results_dir() == tmp_path

    shutil.copy(FIXTURES / "false_movers.json", tmp_path)
    assert client.get("/api/results/false_movers").json()["name"] == "false_movers"


def test_results_dir_defaults_to_the_repository_folder(monkeypatch):
    monkeypatch.delenv("PLANETX_RESULTS_DIR", raising=False)
    monkeypatch.setattr(results, "settings", Settings())
    assert Settings().results_dir is None
    assert get_results_dir() == Path(__file__).parents[2].resolve() / "results"


# -- survey_stats ------------------------------------------------------------------------------


def test_survey_stats_without_database(db_path):
    body = client.get("/api/results/survey_stats").json()
    assert body["available"] is False
    assert body["patches"] == {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
    assert body["objects_measured"] == 0 and body["candidates_total"] == 0
    assert len(body["last_day"]) == 24
    assert not db_path.exists()  # never created by the API


def _candidate(kind, classification, *, pm=2.0):
    return Candidate(
        ra=270.0, dec=66.5, ref_mjd=61000.0, kind=kind, classification=classification, name=None,
        is_variable=False, pm_ra_cosdec=0.0, pm_dec=pm, pm_total=pm, pm_err=0.2, significance=pm / 0.2,
        position_angle_deg=0.0, archive_verdict=None,
        track=(TrackPoint(ra=270.0, dec=66.5, mjd=61000.0, sigma_arcsec=0.3, n_frames=10),), score=0.5,
        field_ra=270.0, field_dec=66.5,
    )


def _claim(q: PatchQueue) -> Patch:
    patch = q.claim_next()
    assert patch is not None
    return patch


def test_survey_stats_counts_patches_objects_and_candidates(db_path):
    now = time.time()
    times = iter([now - 30 * 3600, now - 2 * 3600, now - 2 * 3600 + 60, now - 1800, now - 60])
    with PatchQueue(db_path, create=True, max_attempts=1) as q:
        q.init(1)  # 12 patches
        old, a, b, bad, last = (_claim(q) for _ in range(5))
        # `last` comes from a processor whose summary has no object count.
        for patch, summary in [(old, {"objects": 100}), (a, {"objects": 2902}), (b, {"objects": 40}), (bad, None),
                               (last, {"n_visits": 3})]:
            finished = next(times)
            q.clock = lambda finished=finished: finished
            if summary is None:
                q.fail(patch, "IRSA 503")  # max_attempts=1: failed for good
            else:
                q.complete(patch, summary)
        q.clock = time.time
        q.claim_next()  # one in progress
    with CandidateStore(db_path) as store:
        store.replace_patch(1, a.ipix, [_candidate("mover", None), _candidate("mover", None),
                                        _candidate("candidate", "unknown_candidate")], found_at=now)
        store.replace_patch(1, b.ipix, [_candidate("mover", "known_star", pm=10.4)], found_at=now)

    body = client.get("/api/results/survey_stats").json()
    assert body["available"] is True
    assert body["patches"] == {"pending": 6, "in_progress": 1, "done": 4, "failed": 1}
    assert body["total_patches"] == 12
    assert body["objects_measured"] == 100 + 2902 + 40
    assert body["candidates_total"] == 4
    assert body["by_classification"] == {"unchecked": 2, "unknown_candidate": 1, "known_star": 1}
    assert body["by_kind"] == {"mover": 3, "candidate": 1}
    assert {"classification": None, "kind": "mover", "count": 2} in body["candidates"]

    hours = body["last_day"]
    assert len(hours) == 24 and hours == sorted(hours, key=lambda h: h["start"])
    # The patch finished 30 h ago is outside the last day; the other three done and one failed are in it.
    assert sum(h["done"] for h in hours) == 3
    assert sum(h["failed"] for h in hours) == 1
    assert hours[-1]["done"] + hours[-2]["done"] >= 1
    assert body["first_finished_at"] < body["last_finished_at"]


def test_survey_stats_reads_without_writing(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    before = db_path.read_bytes()
    assert client.get("/api/results/survey_stats").status_code == 200
    assert db_path.read_bytes() == before


def test_survey_stats_of_a_database_without_tables(db_path):
    sqlite3.connect(db_path).close()  # an empty file
    body = client.get("/api/results/survey_stats").json()
    assert body["available"] is True and body["total_patches"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_database_reports_unavailable_not_500(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    os.chmod(db_path, 0o000)
    try:
        resp = client.get("/api/results/survey_stats")
    finally:
        os.chmod(db_path, 0o644)
    assert resp.status_code == 200
    assert resp.json()["available"] is False
