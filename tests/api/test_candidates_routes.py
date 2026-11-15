"""Offline tests for GET /api/candidates against temp survey databases."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from planetx.api.app import app
from planetx.api.routes.survey import get_db_path
from planetx.survey.candidates import CandidateStore
from planetx.survey.queue import PatchQueue

sys.path.insert(0, str(Path(__file__).parents[1] / "survey"))
from survey_fakes import candidate  # noqa: E402

client = TestClient(app)
KEYS = {"id", "ra", "dec", "name", "classification", "is_variable", "pm_total", "pm_ra_cosdec", "pm_dec",
        "significance", "position_angle_deg", "score", "nside", "ipix", "found"}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "survey.sqlite"
    app.dependency_overrides[get_db_path] = lambda: path
    yield path
    app.dependency_overrides.pop(get_db_path, None)


@pytest.fixture
def filled(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    with CandidateStore(db_path) as store:
        store.replace_patch(1, 2, [candidate(score=0.15, name="Barnard's Star"),
                                   candidate(score=0.95, classification="unknown_candidate", name=None)],
                            found_at=1_794_614_400.0)  # 2026-11-14
        store.replace_patch(1, 7, [candidate(score=0.5, classification=None, name=None)], found_at=1_794_614_400.0)
    return db_path


def test_empty_without_database(db_path):
    resp = client.get("/api/candidates")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "candidates": []}
    assert not db_path.exists()  # read-only: never created


def test_empty_before_the_crawler_stored_anything(db_path):
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    assert client.get("/api/candidates?classification=").json() == {"total": 0, "candidates": []}


def test_shape_and_order(filled):
    body = client.get("/api/candidates?limit=100&offset=0&classification=").json()
    assert set(body) == {"total", "candidates"} and body["total"] == 3
    assert all(set(c) == KEYS for c in body["candidates"])
    assert [c["score"] for c in body["candidates"]] == [0.95, 0.5, 0.15]
    top, unchecked, barnard = body["candidates"]
    assert top["classification"] == "unknown_candidate" and top["name"] is None
    assert unchecked["classification"] is None and unchecked["ipix"] == 7
    assert barnard == {
        "id": barnard["id"], "ra": 10.0, "dec": 20.0, "name": "Barnard's Star", "classification": "known_star",
        "is_variable": False, "pm_total": 10.39, "pm_ra_cosdec": -0.8, "pm_dec": 10.36, "significance": 20.8,
        "position_angle_deg": 355.6, "score": 0.15, "nside": 1, "ipix": 2, "found": "2026-11-14",
    }


def test_filter_and_pages(filled):
    body = client.get("/api/candidates?classification=known_star").json()
    assert body["total"] == 1 and body["candidates"][0]["name"] == "Barnard's Star"
    page = client.get("/api/candidates?limit=1&offset=1").json()
    assert page["total"] == 3 and [c["score"] for c in page["candidates"]] == [0.5]
    assert client.get("/api/candidates?offset=3").json() == {"total": 3, "candidates": []}


@pytest.mark.parametrize("query", ["classification=planet", "limit=0", "limit=501", "offset=-1"])
def test_bad_parameters(filled, query):
    assert client.get(f"/api/candidates?{query}").status_code == 422
