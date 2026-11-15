"""Offline tests for /api/motion: the engine run on synthetic frames (IRSA replaced by fakes)."""
import io
import zlib

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from fastapi.testclient import TestClient
from planetx import pipeline
from planetx.api import motion
from planetx.api.app import app
from planetx.api.schemas import Visit
from planetx.classify import CatalogError, CatalogObject, check_objects
from planetx.config import Settings
from planetx.observatories import refine_objects, sources
from planetx.spherex import archive, cutouts
from synthetic import make_mef

client = TestClient(app)

RA, DEC = 150.0, 20.0
CENTER = SkyCoord(RA * u.deg, DEC * u.deg)
PASS_MJDS = (60900.0, 61080.0, 61265.0)
MOVER_PM = (0.0, 12.0)  # arcsec/yr east, north
PARAMS = {"ra": RA, "dec": DEC}


def _url(tag: str) -> str:
    return f"https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/{tag}_spx.fits"


def _visits(per_pass_detector: int = 3, detectors=(1, 2, 3)) -> list[Visit]:
    out = []
    for p, mjd0 in enumerate(PASS_MJDS):
        for k in range(per_pass_detector):
            for det in detectors:
                mjd = mjd0 + 2.0 * k + 0.01 * det
                out.append(Visit(
                    obs_id=f"p{p}k{k}", collection="spherex_dr1", detector=det, mjd=mjd,
                    date=archive.mjd_to_date(mjd), access_url=_url(f"p{p}k{k}D{det}"),
                ))
    return out


def _offset(east: float, north: float) -> tuple[float, float]:
    c = CENTER.spherical_offsets_by(east * u.arcsec, north * u.arcsec)
    return float(c.ra.deg), float(c.dec.deg)


def _cutout(visit: Visit, static: list[tuple[float, float, float]], seed: int) -> bytes:
    """A noisy synthetic cutout: fixed stars plus one star moving at MOVER_PM from (-60", -40")."""
    years = (visit.mjd - PASS_MJDS[0]) / 365.25
    sources = [(*_offset(e, n), amp) for e, n, amp in static]
    sources.append((*_offset(-60 + MOVER_PM[0] * years, -40 + MOVER_PM[1] * years), 400.0))
    rng = np.random.default_rng(seed)
    with fits.open(io.BytesIO(make_mef(RA, DEC, sources, rotation_deg=float(rng.uniform(0, 360))))) as hdul:
        image = hdul["IMAGE"]
        image.data = (image.data + rng.normal(5.0, 1.0, image.data.shape)).astype(np.float32)
        image.header["MJD-AVG"] = visit.mjd
        image.header["DETECTOR"] = visit.detector
        image.header["PSF_FWHM"] = 1.5 * 2.3548 * 6.15  # make_mef draws sigma = 1.5 px
        buf = io.BytesIO()
        hdul.writeto(buf)
    return buf.getvalue()


class _Inline:
    """Stands in for the job executor: runs the job at once, in the calling thread."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Fake archive + cutout cache; jobs run inline; SPHEREx only, so no cross-check service is called."""
    rng = np.random.default_rng(7)
    # Fixed stars on a jittered grid, none within 40" of the mover's path (the synthetic
    # PSF is 3.5 px wide, so a closer star would blend with it).
    static = [
        (e + rng.uniform(-5, 5), n + rng.uniform(-5, 5), float(rng.uniform(150, 1500)))
        for e in range(-150, 151, 50) for n in range(-150, 151, 50)
        if np.hypot(e + 60, n + 34) > 40
    ]
    state = {"visits": _visits(), "sia_calls": 0, "missing": set()}

    def query_visits(ra, dec, radius_arcsec):
        state["sia_calls"] += 1
        if isinstance(state["visits"], Exception):
            raise state["visits"]
        return list(state["visits"])

    def cached_cutout(url, ra, dec, size_deg):
        visit = next(v for v in state["visits"] if v.access_url == url)
        if url in state["missing"]:
            raise archive.UpstreamError("cutout download failed after 1 attempt(s): HTTP 422", 422)
        key = zlib.crc32(url.encode())  # stable across runs, unlike hash()
        path = tmp_path / f"{key}.fits"
        if not path.exists():
            path.write_bytes(_cutout(visit, static, seed=key))
        return path

    monkeypatch.setattr(archive, "query_visits", query_visits)
    monkeypatch.setattr(cutouts, "cached_cutout", cached_cutout)
    monkeypatch.setattr(motion, "_runner", _Inline())
    monkeypatch.setattr(pipeline, "LIST_RETRY_WAITS_S", (0.0, 0.0))
    monkeypatch.setattr(sources, "settings", Settings(observatories="spherex"))
    motion._jobs.clear()
    yield state
    motion._jobs.clear()


def test_run_finds_the_planted_mover_and_no_other(fakes):
    resp = client.get("/api/motion", params=PARAMS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done" and body["error"] is None
    assert (body["frames_done"], body["frames_total"]) == (27, 27)
    result = body["result"]
    assert result["n_frames"] == 27 and result["detectors"] == [1, 2, 3]
    assert [p["n_frames"] for p in result["passes"]] == [9, 9, 9]

    movers = [o for o in result["objects"] if o["kind"] == "mover"]
    assert len(movers) == 1 and movers[0] is result["objects"][0]  # movers come first
    mover = movers[0]
    assert mover["pm_dec"] == pytest.approx(MOVER_PM[1], abs=1.5)
    assert mover["pm_ra_cosdec"] == pytest.approx(MOVER_PM[0], abs=1.5)
    assert min(mover["position_angle_deg"], 360 - mover["position_angle_deg"]) < 10  # due north
    assert [t["date"] for t in mover["track"]] == sorted(t["date"] for t in mover["track"])
    assert len(mover["track"]) == 3
    assert mover["archive"]["verdict"] == "spherex_only"
    # SPHEREx-only mode consults no catalogue, so nothing is named or classified.
    assert all((o["name"], o["classification"], o["matches"]) == (None, None, []) for o in result["objects"])
    assert [o["id"] for o in result["objects"]] == list(range(len(result["objects"])))
    assert sum(o["kind"] == "static" for o in result["objects"]) >= 30


def _with_catalogues(monkeypatch, gaia, simbad=()):
    """Turn the catalogue check on with fake Gaia/SIMBAD answers (the archival refit stays SPHEREx-only)."""

    def fetch(rows):
        def answer(ra, dec, radius_arcsec):
            if isinstance(rows, Exception):
                raise rows
            return list(rows)
        return answer

    monkeypatch.setattr(sources, "settings", Settings(observatories="multi"))
    monkeypatch.setattr(pipeline, "refine_objects", lambda objs, observatories: refine_objects(objs, observatories="spherex"))
    monkeypatch.setattr(pipeline, "check_objects", lambda objs, fused, pixel: check_objects(
        objs, fused, pixel, fetch_gaia=fetch(gaia), fetch_simbad=fetch(simbad),
        fetch_skybot=lambda ra, dec, radius, mjd: [],
    ))


def test_catalogue_check_names_and_classifies_the_objects(fakes, monkeypatch):
    # A Gaia star with the planted mover's motion, at its place in 2016.0 (Gaia's date).
    years = (PASS_MJDS[0] - 57388.5) / 365.25
    start = _offset(-60 - MOVER_PM[0] * years, -40 - MOVER_PM[1] * years)
    fast = CatalogObject("gaia", "Gaia DR3 42", *start, 57388.5, MOVER_PM[0], MOVER_PM[1], 9.0, "High proper-motion star")
    _with_catalogues(monkeypatch, gaia=[fast])
    result = client.get("/api/motion", params=PARAMS).json()["result"]
    mover = result["objects"][0]
    assert (mover["kind"], mover["classification"], mover["name"]) == ("mover", "known_star", "Gaia DR3 42")
    (match,) = mover["matches"]
    assert match["catalog"] == "gaia" and match["kind"] == "High proper-motion star" and match["sep_arcsec"] < 3
    assert (match["pm_ra_cosdec"], match["pm_dec"], match["magnitude"]) == (MOVER_PM[0], MOVER_PM[1], 9.0)
    rest = result["objects"][1:]
    assert all(o["kind"] == "static" and o["classification"] == "static" and o["name"] is None for o in rest)


def test_mover_no_catalogue_knows_is_an_unknown_candidate(fakes, monkeypatch):
    _with_catalogues(monkeypatch, gaia=[])
    mover = client.get("/api/motion", params=PARAMS).json()["result"]["objects"][0]
    assert (mover["kind"], mover["classification"], mover["name"]) == ("mover", "unknown_candidate", None)


def test_catalogue_outage_does_not_fail_the_run(fakes, monkeypatch):
    _with_catalogues(monkeypatch, gaia=CatalogError("Gaia returned HTTP 503"))
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "done"
    assert body["result"]["objects"][0]["kind"] == "mover"
    assert all(o["classification"] is None for o in body["result"]["objects"])
    assert any(n.startswith("Gaia unavailable (Gaia returned HTTP 503)") for n in body["result"]["notes"])


def test_engine_data_is_kept_for_the_newest_runs_only(fakes, monkeypatch):
    monkeypatch.setattr(motion, "MAX_ENGINE_KEPT", 1)
    assert motion.finished_run(RA, DEC, 0.1) is None  # no run yet
    first = client.get("/api/motion", params=PARAMS).json()["result"]
    run = motion.finished_run(RA + 0.0002, DEC, 0.1)  # snaps to the same run
    assert len(run.tracked) == len(first["objects"]) == len(run.result.objects)
    mover = run.tracked[first["objects"][0]["id"]]
    assert mover.is_mover and mover.ra == first["objects"][0]["ra"]
    client.get("/api/motion", params={"ra": RA, "dec": DEC, "size_deg": 0.12})
    assert motion.finished_run(RA, DEC, 0.1) is None  # dropped: a newer run finished
    assert motion.finished_run(RA, DEC, 0.12) is not None
    assert client.get("/api/motion", params=PARAMS).json()["result"] == first  # the summary stays


def test_polling_reuses_the_run(fakes):
    first = client.get("/api/motion", params=PARAMS).json()
    # A nearby request snaps to the same 0.001 deg centre and so to the same run.
    again = client.get("/api/motion", params={"ra": RA + 0.0002, "dec": DEC}).json()
    assert fakes["sia_calls"] == 1
    assert again == first


def test_failed_run_stays_visible_briefly_then_is_retried(fakes, monkeypatch):
    fakes["visits"] = archive.UpstreamError("SIA query spherex_dr1 failed after 3 attempt(s): HTTP 503", 503, True)
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "failed" and body["result"] is None
    assert "temporarily unavailable (HTTP 503)" in body["error"]
    assert fakes["sia_calls"] == 3  # the listing was retried through the pauses first
    fakes["visits"] = _visits()
    # A second viewer polling the same run sees the failure too, without a new run.
    assert client.get("/api/motion", params=PARAMS).json() == body
    assert fakes["sia_calls"] == 3
    monkeypatch.setattr(motion, "FAILED_TTL_S", -1.0)
    assert client.get("/api/motion", params=PARAMS).json()["status"] == "done"
    assert fakes["sia_calls"] == 4


def test_listing_recovers_from_a_short_irsa_outage(fakes, monkeypatch):
    real, calls = archive.query_visits, []

    def flaky(ra, dec, radius_arcsec):
        calls.append(1)
        if len(calls) < 3:
            raise archive.UpstreamError("SIA query spherex_dr1 failed after 3 attempt(s): HTTP 502: <html>", 502, True)
        return real(ra, dec, radius_arcsec)

    monkeypatch.setattr(archive, "query_visits", flaky)
    assert client.get("/api/motion", params=PARAMS).json()["status"] == "done"
    assert len(calls) == 3


def test_permanent_archive_error_is_not_retried(fakes):
    fakes["visits"] = archive.UpstreamError("SIA query hit the 10000-row limit")
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "failed" and "refused the request" in body["error"] and "<" not in body["error"]
    assert fakes["sia_calls"] == 1


def test_run_nobody_polls_is_dropped_before_any_download(fakes, monkeypatch):
    monkeypatch.setattr(motion, "ABANDON_S", -1.0)
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "failed" and "nobody was waiting" in body["error"]
    assert fakes["sia_calls"] == 0


def test_exposures_without_data_are_left_out_with_a_note(fakes):
    fakes["missing"] = {v.access_url for v in fakes["visits"][:3]}
    result = client.get("/api/motion", params=PARAMS).json()["result"]
    assert result["n_frames"] == 24
    assert "3 exposures had no data at this position" in result["notes"]


def test_run_fails_when_no_frame_loads(fakes):
    fakes["missing"] = {v.access_url for v in fakes["visits"]}
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "failed"
    assert "only 0 of 27 frames" in body["error"]


def test_too_few_exposures_fails(fakes):
    fakes["visits"] = _visits(detectors=(5, 6))  # the engine samples detectors 1-3 only
    body = client.get("/api/motion", params=PARAMS).json()
    assert body["status"] == "failed" and "too few exposures" in body["error"]


def test_busy_when_too_many_runs_wait(fakes, monkeypatch):
    class Never:
        def submit(self, fn, *args):
            pass

    monkeypatch.setattr(motion, "_runner", Never())
    for i in range(motion.MAX_WAITING_JOBS + 1):
        assert client.get("/api/motion", params={"ra": RA + i, "dec": DEC}).json()["status"] == "running"
    resp = client.get("/api/motion", params={"ra": RA + 50, "dec": DEC})
    assert resp.status_code == 503 and resp.headers["retry-after"] == "30"
    # Polling a run that already exists still works while the queue is full.
    assert client.get("/api/motion", params=PARAMS).status_code == 200


@pytest.mark.parametrize("params", [
    {"ra": RA, "dec": DEC, "size_deg": 0.5},
    {"ra": RA, "dec": DEC, "size_deg": 0.01},
    {"ra": 360, "dec": DEC},
    {"dec": DEC},
])
def test_rejects_bad_input(fakes, params):
    assert client.get("/api/motion", params=params).status_code == 422
    assert fakes["sia_calls"] == 0


def test_pick_visits_samples_each_pass_and_detector_evenly():
    visits = _visits(per_pass_detector=20, detectors=(1, 2, 3, 4, 5, 6))
    picked = pipeline.pick_visits(visits)
    assert len(picked) == 3 * 3 * pipeline.FRAMES_PER_PASS_DETECTOR
    assert {v.detector for v in picked} == {1, 2, 3}
    first_d1 = [v for v in picked if v.detector == 1 and v.mjd < PASS_MJDS[1]]
    assert first_d1[0].obs_id == "p0k0" and first_d1[-1].obs_id == "p0k19"  # spans the whole pass
    assert pipeline.pick_visits(list(reversed(visits))) == picked  # independent of input order

