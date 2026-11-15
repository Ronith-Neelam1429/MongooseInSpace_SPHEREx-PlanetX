"""Offline tests for /api/motion/evidence: the engine run on synthetic frames with sources
planted at known sky offsets, so image orientation and mark positions are checked for real."""
import base64
import dataclasses
import gc
import io
import weakref

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from fastapi.testclient import TestClient
from PIL import Image
from planetx.api import evidence as proof
from planetx.api import motion
from planetx.api.app import app
from planetx.engine import find_motion, frame_from_hdul
from planetx.spherex import cutouts
from synthetic import make_mef

client = TestClient(app)

RA, DEC, SIZE_DEG = 150.0, 20.0, 0.1
CENTER = SkyCoord(RA * u.deg, DEC * u.deg)
PASS_MJDS = (60900.0, 61080.0, 61265.0)
PARAMS = {"ra": RA, "dec": DEC, "size_deg": SIZE_DEG}
# Offsets below are (east, north) in arcsec from the field centre.
MOVER_START = (-60.0, -40.0)
MOVER_PM = (6.0, 9.0)  # arcsec/yr: north-east, so both axes are exercised
# A fixed star at a lopsided offset from the mover's first position: 36" east, 27" south.
MARKER_FROM_MOVER = (36.0, -27.0)
VARIABLE = (95.0, 60.0)  # fixed; half as bright in the last pass
BLINKER = (-100.0, 80.0)  # fixed; absent in the middle pass
# The middle pass's frames claim a pointing that is off by this much, which the
# engine's alignment has to undo (and the images have to follow).
MISPOINTING = (1.9, -1.5)
PX = proof.PATCH_PIXEL_ARCSEC


def _sky(east: float, north: float) -> tuple[float, float]:
    c = CENTER.spherical_offsets_by(east * u.arcsec, north * u.arcsec)
    return float(c.ra.deg), float(c.dec.deg)


def _frame(pass_index: int, k: int, detector: int, static, rng):
    mjd = PASS_MJDS[pass_index] + 2.0 * k + 0.01 * detector
    years = (mjd - PASS_MJDS[0]) / 365.25
    mover = (MOVER_START[0] + MOVER_PM[0] * years, MOVER_START[1] + MOVER_PM[1] * years)
    sources = [(*_sky(e, n), amp) for e, n, amp in static]
    sources.append((*_sky(*mover), 400.0))
    sources.append((*_sky(*VARIABLE), 600.0 if pass_index < 2 else 300.0))
    if pass_index != 1:
        sources.append((*_sky(*BLINKER), 500.0))
    with fits.open(io.BytesIO(make_mef(RA, DEC, sources, rotation_deg=float(rng.uniform(0, 360))))) as hdul:
        image = hdul["IMAGE"]
        image.data = (image.data + rng.normal(5.0, 1.0, image.data.shape)).astype(np.float32)
        image.header["MJD-AVG"] = mjd
        image.header["DETECTOR"] = detector
        image.header["PSF_FWHM"] = 1.5 * 2.3548 * 6.15  # make_mef draws sigma = 1.5 px
        if pass_index == 1:
            wrong = CENTER.spherical_offsets_by(MISPOINTING[0] * u.arcsec, MISPOINTING[1] * u.arcsec)
            image.header["CRVAL1"], image.header["CRVAL2"] = float(wrong.ra.deg), float(wrong.dec.deg)
        return frame_from_hdul(hdul, obs_id=f"p{pass_index}k{k}")


@pytest.fixture(scope="module")
def run() -> motion.EngineRun:
    """The engine's data for a synthetic field, as `motion.finished_run` hands it out."""
    rng = np.random.default_rng(11)
    marker = (MOVER_START[0] + MARKER_FROM_MOVER[0], MOVER_START[1] + MARKER_FROM_MOVER[1])
    planted = [(*marker, 900.0)]
    # Fixed stars on a jittered grid, none within 40" of the planted sources or the mover's
    # path (the synthetic PSF is 3.5 px wide, so closer stars would blend with them).
    keep_clear = [marker, VARIABLE, BLINKER, MOVER_START, (MOVER_START[0] + 6, MOVER_START[1] + 9)]
    for e in range(-150, 151, 50):
        for n in range(-150, 151, 50):
            if all(np.hypot(e - x, n - y) > 40 for x, y in keep_clear):
                planted.append((e + rng.uniform(-5, 5), n + rng.uniform(-5, 5), float(rng.uniform(150, 1500))))
    frames = [
        _frame(p, k, det, planted, rng)
        for p in range(len(PASS_MJDS)) for k in range(3) for det in (1, 2, 3)
    ]
    result = find_motion(frames)
    return motion.EngineRun(result=result, tracked=list(result.objects))


@pytest.fixture
def served(run, monkeypatch):
    """Serve `run` as the finished run of PARAMS' field, and start from an empty cache."""
    def finished_run(ra, dec, size_deg):
        return run if (round(ra, 3), round(dec, 3), size_deg) == (RA, DEC, SIZE_DEG) else None

    monkeypatch.setattr(motion, "finished_run", finished_run)
    proof._cache.clear()
    yield run
    proof._cache.clear()


def _object_at(run, east: float, north: float) -> int:
    """Index in `run.tracked` of the object whose first position is nearest the given offset."""
    target = SkyCoord(*(v * u.deg for v in _sky(east, north)))
    seps = [
        target.separation(SkyCoord(o.sources[0].ra * u.deg, o.sources[0].dec * u.deg)).arcsec
        for o in run.tracked
    ]
    assert min(seps) < 3.0
    return int(np.argmin(seps))


def _grey(b64: str) -> tuple[np.ndarray, np.ndarray]:
    """(grey, alpha) arrays of a base64 PNG, row 0 at the top."""
    image = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert image.mode == "LA"
    pixels = np.asarray(image)
    return pixels[..., 0].astype(float), pixels[..., 1]


def _centroid(image: np.ndarray, row: float, col: float, radius: float = 4.0) -> tuple[float, float]:
    """Brightness-weighted (row, col) of the blob within `radius` pixels of an expected position."""
    rows, cols = np.mgrid[0:image.shape[0], 0:image.shape[1]]
    near = np.hypot(rows - row, cols - col) <= radius
    weight = np.where(near, image - image[near].min(), 0.0)
    return float((weight * rows).sum() / weight.sum()), float((weight * cols).sum() / weight.sum())


def _pixel(evidence: dict, east: float, north: float) -> tuple[float, float]:
    """(row, col) of an offset from the patch centre, by the documented convention."""
    centre = (evidence["patch_px"] - 1) / 2
    return centre - north / evidence["patch_pixel_arcsec"], centre - east / evidence["patch_pixel_arcsec"]


def _at(image: np.ndarray, position: tuple[float, float]) -> float:
    return float(image[int(round(position[0])), int(round(position[1]))])


def _get(object_id: int) -> dict:
    resp = client.get("/api/motion/evidence", params={**PARAMS, "object": object_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_pass_images_put_known_sky_offsets_on_the_right_pixels(run):
    """Geometry against the truth: a grid centred on a known sky position, exact alignment shifts."""
    n = 49
    centre = (n - 1) / 2
    grid = proof.patch_wcs(*_sky(*MOVER_START), n)
    mispointed = [PASS_MJDS[1] <= f.mjd < PASS_MJDS[1] + 30 for f in run.result.frames]
    assert sum(mispointed) == 9
    # 36" east and 27" south of the centre: left of it and below it (row 0 is north, column 0 east).
    want = (centre + 27 / PX, centre - 36 / PX)

    exact = [(-MISPOINTING[0], -MISPOINTING[1]) if wrong else (0.0, 0.0) for wrong in mispointed]
    images, counts = proof.pass_images(dataclasses.replace(run.result, frame_offsets_arcsec=exact), grid, n)
    assert counts == [9, 9, 9] and all(image.shape == (n, n) for image in images)
    for image in images:
        assert _centroid(image, *want) == pytest.approx(want, abs=0.2)  # 0.2 px = 0.6"

    # Without the shifts the mis-pointed pass shows the star where its header claims it is:
    # 1.9" further east (left) and 1.5" further south (down).
    unaligned, _ = proof.pass_images(
        dataclasses.replace(run.result, frame_offsets_arcsec=[(0.0, 0.0)] * len(exact)), grid, n
    )
    row, col = _centroid(unaligned[1], *want)
    assert row - want[0] == pytest.approx(-MISPOINTING[1] / PX, abs=0.2)
    assert col - want[1] == pytest.approx(-MISPOINTING[0] / PX, abs=0.2)

    assert proof.patch_offsets(grid, n, *_sky(MOVER_START[0] + 36, MOVER_START[1] - 27)) == pytest.approx(
        (36.0, -27.0), abs=0.01
    )


def test_images_are_north_up_east_left_and_marks_sit_on_the_object(served):
    mover_id = _object_at(served, *MOVER_START)
    assert served.tracked[mover_id].is_mover
    ev = _get(mover_id)
    n = ev["patch_px"]
    assert n % 2 == 1 and ev["patch_arcsec"] == pytest.approx(n * PX)
    assert proof.MIN_PATCH_ARCSEC <= ev["patch_arcsec"] <= proof.MIN_PATCH_ARCSEC + 2 * PX
    assert [p["index"] for p in ev["passes"]] == [0, 1, 2]
    assert [p["n_frames"] for p in ev["passes"]] == [9, 9, 9]
    assert ev["passes"][0]["start"] < ev["passes"][1]["start"] < ev["passes"][2]["start"]

    # The measured track: starts at the patch centre and ends where the star was planted.
    points = [p["point"] for p in ev["passes"]]
    assert (points[0]["east_arcsec"], points[0]["north_arcsec"]) == (0.0, 0.0)
    years = (points[2]["mjd"] - points[0]["mjd"]) / 365.25
    assert points[2]["east_arcsec"] == pytest.approx(MOVER_PM[0] * years, abs=1.5)
    assert points[2]["north_arcsec"] == pytest.approx(MOVER_PM[1] * years, abs=1.5)

    # Tolerances are 0.7 image pixels (2"): the engine's centroids and alignment are only
    # that good on the wide synthetic PSF, and the patch is centred on a measured position.
    for p in ev["passes"]:
        grey, alpha = _grey(p["image_png_b64"])
        assert grey.shape == (n, n) and (alpha == 255).all()
        # The fixed star east and south of the object is left of and below the centre in every pass.
        want = _pixel(ev, *MARKER_FROM_MOVER)
        assert want[0] > (n - 1) / 2 + 5 and want[1] < (n - 1) / 2 - 5
        assert _centroid(grey, *want) == pytest.approx(want, abs=0.7)
        # The mark of this pass's measured position sits on the object in this pass's image.
        mark = _pixel(ev, p["point"]["east_arcsec"], p["point"]["north_arcsec"])
        assert _centroid(grey, *mark) == pytest.approx(mark, abs=0.7)
    # North-east motion: the object ends up above and to the left of where it started.
    last = _pixel(ev, points[2]["east_arcsec"], points[2]["north_arcsec"])
    assert last[0] < (n - 1) / 2 - 2 and last[1] < (n - 1) / 2 - 1


def test_passes_share_one_grey_scale(served, monkeypatch):
    limits = []
    real = cutouts.render_png
    monkeypatch.setattr(
        cutouts, "render_png", lambda data, vmin=None, vmax=None: limits.append((vmin, vmax)) or real(data, vmin, vmax)
    )
    ev = _get(_object_at(served, *VARIABLE))
    assert len(limits) == 3 and len(set(limits)) == 1 and None not in limits[0]
    images = [_grey(p["image_png_b64"])[0] for p in ev["passes"]]
    centre = ((ev["patch_px"] - 1) / 2,) * 2
    backgrounds = [float(np.median(image)) for image in images]
    assert max(backgrounds) - min(backgrounds) <= 6
    # The star is equally bright in the first two passes and half as bright in the last:
    # on a shared scale that shows; scaled per image it would not.
    peaks = [_at(image, centre) for image in images]
    assert abs(peaks[1] - peaks[0]) <= 12
    assert peaks[2] < peaks[0] - 25
    assert peaks[2] > backgrounds[2] + 25  # still plainly there


def test_difference_shows_a_mover_as_a_dark_bright_pair(served):
    ev = _get(_object_at(served, *MOVER_START))
    assert ev["difference_passes"] == [0, 2]
    diff, alpha = _grey(ev["difference_png_b64"])
    assert (alpha == 255).all()
    assert abs(np.median(diff) - 127.5) <= 2  # nothing changed over most of the patch
    assert abs(_at(diff, _pixel(ev, *MARKER_FROM_MOVER)) - 127.5) <= 12  # a fixed star cancels
    # The synthetic star is wide and moved by half its width, so the two lobes peak about
    # one PSF sigma (3 image pixels) either side of the midpoint of its path.
    end = ev["passes"][2]["point"]
    step = 3.0 * PX / np.hypot(end["east_arcsec"], end["north_arcsec"])
    behind = _pixel(ev, (0.5 - step) * end["east_arcsec"], (0.5 - step) * end["north_arcsec"])
    ahead = _pixel(ev, (0.5 + step) * end["east_arcsec"], (0.5 + step) * end["north_arcsec"])
    assert _at(diff, behind) < 115  # fainter where it was
    assert _at(diff, ahead) > 140  # brighter where it is now


def test_fit_and_field_scatter_put_the_motion_against_the_noise(served):
    mover_id = _object_at(served, *MOVER_START)
    obj = served.tracked[mover_id]
    ev = _get(mover_id)
    assert (ev["object"], ev["ra"], ev["dec"]) == (mover_id, obj.ra, obj.dec)
    fit = ev["fit"]
    assert (fit["pm_east"], fit["pm_north"], fit["ref_mjd"]) == (obj.pm_ra_cosdec, obj.pm_dec, obj.ref_mjd)
    # The fitted line passes close to every measured point.
    for p in ev["passes"]:
        years = (p["point"]["mjd"] - fit["ref_mjd"]) / 365.25
        assert fit["east_arcsec"] + fit["pm_east"] * years == pytest.approx(p["point"]["east_arcsec"], abs=1.0)
        assert fit["north_arcsec"] + fit["pm_north"] * years == pytest.approx(p["point"]["north_arcsec"], abs=1.0)
    assert ev["pm_floor_arcsec_yr"] == served.result.pm_floor_arcsec_yr
    assert ev["spherex_pixel_arcsec"] == pytest.approx(6.15, abs=0.01)

    # The other objects' apparent shifts since the first pass, checked against great-circle
    # separations. They are fixed stars: the mover went several times further.
    assert ev["passes"][0]["field_scatter"] is None  # the reference pass
    moved = np.hypot(ev["passes"][2]["point"]["east_arcsec"], ev["passes"][2]["point"]["north_arcsec"])
    for p in ev["passes"][1:]:
        shifts = []
        for other in served.tracked:
            by_pass = {s.pass_index: SkyCoord(s.ra * u.deg, s.dec * u.deg) for s in other.sources}
            if other is not obj and 0 in by_pass and p["index"] in by_pass:
                shifts.append(by_pass[0].separation(by_pass[p["index"]]).arcsec)
        scatter = p["field_scatter"]
        assert scatter["n_stars"] == len(shifts) >= 20
        assert scatter["median_shift_arcsec"] == pytest.approx(np.median(shifts), rel=1e-3)
        # Per axis is less than the total shift; for round scatter it is 0.85 of the median.
        assert 0.5 * np.median(shifts) < scatter["sigma_arcsec"] < 1.2 * np.median(shifts)
        assert scatter["median_shift_arcsec"] < moved / 4


def test_fluxes_per_pass_follow_a_planted_brightness_change(served):
    variable_id = _object_at(served, *VARIABLE)
    assert served.tracked[variable_id].is_variable
    ev = _get(variable_id)
    by_pass = [{f["band"]: f for f in p["point"]["fluxes"]} for p in ev["passes"]]
    assert all(set(fluxes) == {"D1", "D2", "D3"} for fluxes in by_pass)
    for band in ("D1", "D2", "D3"):
        assert by_pass[1][band]["flux"] / by_pass[0][band]["flux"] == pytest.approx(1.0, abs=0.1)
        assert by_pass[2][band]["flux"] / by_pass[0][band]["flux"] == pytest.approx(0.5, abs=0.1)
        assert 0 < by_pass[2][band]["flux_err"] < 0.1 * by_pass[2][band]["flux"]
    # It did not move: every mark stays within a fraction of a SPHEREx pixel of the centre.
    for p in ev["passes"]:
        assert np.hypot(p["point"]["east_arcsec"], p["point"]["north_arcsec"]) < 2.0


def test_pass_without_a_measurement_keeps_its_image_but_has_no_point(served):
    ev = _get(_object_at(served, *BLINKER))
    assert [p["point"] is not None for p in ev["passes"]] == [True, False, True]
    assert ev["difference_passes"] == [0, 2]
    centre = (ev["patch_px"] - 1) // 2
    images = [_grey(p["image_png_b64"])[0] for p in ev["passes"]]
    sky = float(np.median(images[1]))
    assert images[0][centre, centre] > sky + 60 and images[2][centre, centre] > sky + 60
    assert abs(images[1][centre, centre] - sky) < 20  # really gone in the middle pass


def test_patch_grows_with_the_track_up_to_a_limit(served):
    obj = served.tracked[_object_at(served, *MOVER_START)]

    def stretched(arcsec: float) -> motion.EngineRun:
        """The same run with one object whose last position lies `arcsec` north of its first."""
        first, last = obj.sources[0], obj.sources[-1]
        moved = dataclasses.replace(last, ra=first.ra, dec=first.dec + arcsec / 3600)
        fake = dataclasses.replace(obj, sources=[*obj.sources[:-1], moved])
        return motion.EngineRun(result=served.result, tracked=[fake])

    ev = proof.build_evidence(stretched(100.0), 0)
    assert ev.patch_arcsec == pytest.approx(2 * (100 + proof.TRACK_MARGIN_ARCSEC), abs=2 * PX)
    assert ev.passes[-1].point.north_arcsec == pytest.approx(100.0, abs=0.01)
    assert ev.passes[-1].point.east_arcsec == pytest.approx(0.0, abs=0.01)
    ev = proof.build_evidence(stretched(900.0), 0)
    assert proof.MAX_PATCH_ARCSEC <= ev.patch_arcsec <= proof.MAX_PATCH_ARCSEC + 2 * PX
    assert ev.patch_px <= 99


def test_pixels_no_frame_covers_are_transparent(served):
    # An object at the field's corner: part of its patch lies outside every frame.
    corner = max(
        range(len(served.tracked)),
        key=lambda i: CENTER.separation(
            SkyCoord(served.tracked[i].sources[0].ra * u.deg, served.tracked[i].sources[0].dec * u.deg)
        ).arcsec,
    )
    ev = _get(corner)
    for p in ev["passes"]:
        grey, alpha = _grey(p["image_png_b64"])
        assert 0 < (alpha == 0).sum() < alpha.size
        assert (grey[alpha == 0] == 0).all()
        assert p["coverage"] == pytest.approx((alpha == 255).mean())
    assert (_grey(ev["difference_png_b64"])[1] == 0).any()
    # An object well inside the field has data everywhere.
    assert [p["coverage"] for p in _get(_object_at(served, *MOVER_START))["passes"]] == [1.0, 1.0, 1.0]


def test_repeat_requests_are_served_from_the_cache(served, monkeypatch):
    built = []
    real = proof.build_evidence
    monkeypatch.setattr(proof, "build_evidence", lambda run, i: built.append(i) or real(run, i))
    first = client.get("/api/motion/evidence", params={**PARAMS, "object": 0})
    again = client.get("/api/motion/evidence", params={**PARAMS, "object": 0})
    assert first.status_code == 200 and again.json() == first.json()
    assert first.headers["cache-control"] == "public, max-age=300"
    assert built == [0]
    _get(1)
    assert built == [0, 1]

    # A re-run of the field is new data, even for the same object number...
    rerun = motion.EngineRun(result=served.result, tracked=list(served.tracked))
    monkeypatch.setattr(motion, "finished_run", lambda ra, dec, size_deg: rerun)
    _get(0)
    assert built == [0, 1, 0]
    # ...and even when the new run reuses the memory address (id) of one that was freed.
    freed = motion.EngineRun(result=served.result, tracked=[])
    proof._cache[(id(rerun), 5)] = (weakref.ref(freed), "evidence of the freed run")
    del freed
    gc.collect()
    assert proof.evidence_for(rerun, 5).object == 5

    monkeypatch.setattr(proof, "MAX_CACHED", 2)
    for i in (2, 3, 4):
        _get(i)
    assert len(proof._cache) == 2


def test_no_finished_run_is_a_404_that_says_what_to_do(served, monkeypatch):
    started = []
    monkeypatch.setattr(motion, "request_job", lambda *args, **kw: started.append((args, kw)))
    resp = client.get("/api/motion/evidence", params={"ra": RA + 1, "dec": DEC, "object": 0})
    assert resp.status_code == 404
    assert "/api/motion" in resp.json()["detail"] and "retry" in resp.json()["detail"]
    assert "cache-control" not in resp.headers
    # The field is run (again), so the client's next /api/motion poll follows a run that keeps its engine data.
    assert started == [((RA + 1, DEC, SIZE_DEG), {"need_engine": True})]


def test_unknown_object_is_a_404(served):
    resp = client.get("/api/motion/evidence", params={**PARAMS, "object": len(served.tracked)})
    assert resp.status_code == 404
    assert f"no object {len(served.tracked)}" in resp.json()["detail"]


@pytest.mark.parametrize("params", [
    {**PARAMS, "object": -1},
    {**PARAMS, "object": 100_001},
    {**PARAMS, "object": "first"},
    {**PARAMS},  # object missing
    {"ra": RA, "dec": DEC, "size_deg": 0.5, "object": 0},
    {"ra": 360, "dec": DEC, "object": 0},
    {"ra": RA, "dec": 91, "object": 0},
])
def test_rejects_bad_input(served, monkeypatch, params):
    monkeypatch.setattr(proof, "build_evidence", lambda run, i: pytest.fail("must not build"))
    assert client.get("/api/motion/evidence", params=params).status_code == 422
