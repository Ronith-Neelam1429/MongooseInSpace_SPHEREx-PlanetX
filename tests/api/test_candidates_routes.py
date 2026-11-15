"""Offline tests for GET /api/candidates against temp survey databases."""
import dataclasses
import io
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from planetx import pipeline
from planetx.api.app import app
from planetx.api.routes import candidates as candidates_route
from planetx.api.routes.survey import get_db_path
from planetx.api.schemas import Visit
from planetx.spherex import archive, cutouts
from planetx.survey.candidates import CandidateStore
from planetx.survey.motion import FIELD_SIZE_DEG
from planetx.survey.queue import PatchQueue
from synthetic import make_mef

sys.path.insert(0, str(Path(__file__).parents[1] / "survey"))
from survey_fakes import candidate  # noqa: E402

client = TestClient(app)
KEYS = {"id", "ra", "dec", "name", "classification", "is_variable", "pm_total", "pm_ra_cosdec", "pm_dec",
        "significance", "position_angle_deg", "score", "nside", "ipix", "found", "kind", "field_ra", "field_dec", "ref_mjd"}


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
        "kind": "mover", "field_ra": 10.0, "field_dec": 20.0, "ref_mjd": 61080.0,
    }


def test_filter_and_pages(filled):
    body = client.get("/api/candidates?classification=known_star").json()
    assert body["total"] == 1 and body["candidates"][0]["name"] == "Barnard's Star"
    page = client.get("/api/candidates?limit=1&offset=1").json()
    assert page["total"] == 3 and [c["score"] for c in page["candidates"]] == [0.5]
    assert client.get("/api/candidates?offset=3").json() == {"total": 3, "candidates": []}


@pytest.mark.parametrize("query", ["classification=planet", "limit=0", "limit=501", "offset=-1", "sort=name"])
def test_bad_parameters(filled, query):
    assert client.get(f"/api/candidates?{query}").status_code == 422


def test_sort_by_date_puts_the_newest_run_first(filled):
    with CandidateStore(filled) as store:
        store.replace_patch(1, 9, [candidate(score=0.05, name="Late")], found_at=1_794_700_800.0)  # 2026-11-15
    by_date = client.get("/api/candidates?sort=date").json()["candidates"]
    assert [(c["found"], c["score"]) for c in by_date] == [
        ("2026-11-15", 0.05), ("2026-11-14", 0.95), ("2026-11-14", 0.5), ("2026-11-14", 0.15),
    ]
    by_score = client.get("/api/candidates?sort=score").json()["candidates"]
    assert [c["score"] for c in by_score] == [0.95, 0.5, 0.15, 0.05]


# --- thumbnails: IRSA replaced by fakes --------------------------------------------

URL = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/2/level2_x_{n:04d}_1D2_spx.fits"


def _visit(n, mjd, detector=2):
    return Visit(obs_id=f"o{n}", collection="spherex_dr1", detector=detector, mjd=mjd,
                 date=archive.mjd_to_date(mjd), wavelength_min_um=1.1, wavelength_max_um=1.65,
                 access_url=URL.format(n=n))


@pytest.fixture
def thumb(db_path, cache_dir, monkeypatch):
    """A candidate 1' east of its field centre, exposures of the field, fake IRSA."""
    field_ra, field_dec = 10.0, 20.0
    ra = field_ra + (1 / 60) / math.cos(math.radians(field_dec))
    with PatchQueue(db_path, create=True) as q:
        q.init(1)
    c = dataclasses.replace(candidate(ra=ra, dec=field_dec), field_ra=field_ra, field_dec=field_dec)
    with CandidateStore(db_path) as store:
        store.replace_patch(1, 2, [c], found_at=1_794_614_400.0)
        (row,) = store.patch_candidates(1, 2)
    calls = {"visits": [], "fetch": []}

    def visits(ra_, dec_, radius):
        calls["visits"].append((ra_, dec_, radius))
        # Detector 6 is newest but the survey never uses it.
        return [_visit(1, 60900.0), _visit(2, 61080.0), _visit(3, 61085.0, detector=6)]

    def fetch(url, ra_, dec_, size):
        calls["fetch"].append((url, ra_, dec_, size))
        return make_mef(ra_, dec_, sources=[(ra, field_dec, 100.0)])

    monkeypatch.setattr(archive, "query_visits", visits)
    monkeypatch.setattr(archive, "fetch_cutout", fetch)
    return row.id, calls


def test_thumbnail_reuses_the_survey_field_cutout_of_the_newest_exposure(thumb):
    cid, calls = thumb
    resp = client.get(f"/api/candidates/{cid}/thumbnail.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert "max-age=3600" in resp.headers["cache-control"]
    img = np.array(Image.open(io.BytesIO(resp.content)))
    n = img.shape[0]
    assert img.shape[:2] == (n, n) and 15 <= n <= 20  # 0.03 deg at 6.15"/px
    # Centred on the candidate: its source is the brightest pixel, in the middle.
    row, col = np.unravel_index(np.argmax(img[..., 0]), img.shape[:2])
    assert abs(row - (n - 1) / 2) <= 1 and abs(col - (n - 1) / 2) <= 1
    # Visits listed at the field centre, like the survey; the newest detector 1-3 exposure,
    # cut out with the field's request (the cutout the survey already cached).
    assert calls["visits"] == [(10.0, 20.0, pipeline.VISIT_RADIUS_ARCSEC)]
    assert calls["fetch"] == [(URL.format(n=2), *cutouts.cutout_request(10.0, 20.0, FIELD_SIZE_DEG))]
    # A second request is served from the cache.
    assert client.get(f"/api/candidates/{cid}/thumbnail.png").status_code == 200
    assert len(calls["fetch"]) == 1


def test_thumbnail_of_an_unknown_candidate_is_404(thumb):
    cid, calls = thumb
    assert client.get(f"/api/candidates/{cid + 100}/thumbnail.png").status_code == 404
    assert calls["visits"] == []


def test_thumbnail_without_database_is_404(db_path):
    assert client.get("/api/candidates/1/thumbnail.png").status_code == 404


@pytest.mark.parametrize("cid", ["0", "-1", str(2**63), "abc"])
def test_thumbnail_rejects_bad_ids(db_path, cid):
    assert client.get(f"/api/candidates/{cid}/thumbnail.png").status_code == 422


def test_thumbnail_skips_an_exposure_without_cutout_here(thumb, monkeypatch):
    cid, calls = thumb
    real_fetch = archive.fetch_cutout

    def fetch(url, *args):
        if url == URL.format(n=2):  # the newest has no cutout at this position
            calls["fetch"].append((url, *args))
            raise archive.UpstreamError("no overlap", status=404)
        return real_fetch(url, *args)

    monkeypatch.setattr(archive, "fetch_cutout", fetch)
    resp = client.get(f"/api/candidates/{cid}/thumbnail.png")
    assert resp.status_code == 200
    assert [c[0] for c in calls["fetch"]] == [URL.format(n=2), URL.format(n=1)]


def test_thumbnails_beyond_the_slots_are_503_and_free_their_slot(thumb, monkeypatch):
    cid, _ = thumb
    held = [candidates_route._thumbnail_slots.acquire(blocking=False) for _ in range(candidates_route.THUMBNAIL_SLOTS)]
    assert all(held)
    try:
        resp = client.get(f"/api/candidates/{cid}/thumbnail.png")
        assert resp.status_code == 503 and resp.headers["retry-after"] == "3"
        # The health check is async: it never waits for the thread pool the thumbnails use.
        assert client.get("/api/health").status_code == 200
    finally:
        for _ in held:
            candidates_route._thumbnail_slots.release()
    # Slots come back after both successes and errors.
    assert client.get(f"/api/candidates/{cid}/thumbnail.png").status_code == 200
    assert client.get(f"/api/candidates/{cid + 100}/thumbnail.png").status_code == 404
    assert all(candidates_route._thumbnail_slots.acquire(blocking=False) for _ in range(candidates_route.THUMBNAIL_SLOTS))
    for _ in range(candidates_route.THUMBNAIL_SLOTS):
        candidates_route._thumbnail_slots.release()


def test_thumbnail_without_exposures_is_404(thumb, monkeypatch):
    cid, _ = thumb
    monkeypatch.setattr(archive, "query_visits", lambda *a: [])
    assert client.get(f"/api/candidates/{cid}/thumbnail.png").status_code == 404


@pytest.mark.parametrize(("error", "status"), [
    (archive.IrsaBusy("busy"), 503),
    (archive.UpstreamError("IRSA 502", status=502, transient=True), 502),
    (archive.UpstreamError("no overlap", status=404), 404),
])
def test_thumbnail_upstream_errors(thumb, monkeypatch, error, status):
    cid, _ = thumb

    def fail(*args):
        raise error

    monkeypatch.setattr(archive, "fetch_cutout", fail)
    resp = client.get(f"/api/candidates/{cid}/thumbnail.png")
    assert resp.status_code == status
    if status == 503:
        assert resp.headers["retry-after"] == "5"
