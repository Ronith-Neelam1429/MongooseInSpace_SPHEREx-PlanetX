"""Offline tests for the proof that a motion is real (`planetx.api.evidence_checks`) and for the
single exposures shown with it: the synthetic field of test_evidence_routes for the whole
path, and hand-made inputs for the ways a fake mover gives itself away."""
import numpy as np
import pytest
import test_evidence_routes as routes
from planetx.api import evidence as proof
from planetx.api import evidence_checks as checks
from planetx.engine import EngineResult, TrackedObject
from planetx.engine.motion import PassSource
from test_evidence_routes import (
    MARKER_FROM_MOVER,
    MOVER_START,
    VARIABLE,
    _get,
    _grey,
    _object_at,
)

# The synthetic field's engine run and its serving, shared with test_evidence_routes.
run = routes.run
served = routes.served

MARKER = (MOVER_START[0] + MARKER_FROM_MOVER[0], MOVER_START[1] + MARKER_FROM_MOVER[1])
KEYS = ["beyond_noise", "track_line", "images_show_motion", "many_exposures", "detectors_agree", "same_look", "no_stand_in"]


def _by_key(evidence: dict) -> dict[str, dict]:
    return {c["key"]: c for c in evidence["checks"]}


# --- the whole path, on the synthetic field ---------------------------------------------


def test_planted_mover_passes_the_independent_tests(served):
    ev = _get(_object_at(served, *MOVER_START))
    assert [c["key"] for c in ev["checks"]] == KEYS
    got = _by_key(ev)
    for key in ("beyond_noise", "track_line", "images_show_motion", "many_exposures", "detectors_agree", "same_look"):
        assert got[key]["status"] == "pass", got[key]
    assert got["beyond_noise"]["value"] > 5
    assert "north-east" in got["beyond_noise"]["summary"]  # planted (6, 9)"/yr
    assert got["images_show_motion"]["value"] > 5
    assert "D1, D2 and D3" in got["detectors_agree"]["summary"]
    # The synthetic PSF is 22" wide and the mover went 11": its spots overlap, and the test says so.
    assert got["no_stand_in"]["status"] == "unknown"
    assert all(c["summary"] for c in ev["checks"])


def test_fixed_star_is_not_passed_off_as_a_mover(served):
    got = _by_key(_get(_object_at(served, *MARKER)))
    assert got["beyond_noise"]["status"] == "fail" and got["beyond_noise"]["value"] < 3
    assert got["images_show_motion"]["status"] in ("fail", "unknown")
    assert "move" not in got["detectors_agree"]["summary"]  # nothing moved, so no talk of "the move"
    assert got["same_look"]["status"] == "pass"


def test_variable_star_keeps_its_colour(served):
    got = _by_key(_get(_object_at(served, *VARIABLE)))
    # Half as bright in the last pass in every band: the colour holds, so it is one object that varies.
    assert got["same_look"]["status"] == "weak"
    assert "colour" in got["same_look"]["summary"]


def test_single_exposures_are_real_frames_spread_over_each_pass(served):
    ev = _get(_object_at(served, *MOVER_START))
    m = ev["exposure_px"]
    assert m % 2 == 1 and m * proof.PATCH_PIXEL_ARCSEC == pytest.approx(ev["exposure_arcsec"])
    assert ev["exposure_arcsec"] >= proof.MIN_EXPOSURE_ARCSEC
    for p in ev["passes"]:
        assert p["n_cover"] == 9  # 3 exposures x 3 detectors cover the mover in every pass
        shown = p["exposures"]
        assert len(shown) == proof.EXPOSURES_PER_PASS
        assert [e["mjd"] for e in shown] == sorted(e["mjd"] for e in shown)
        assert {e["detector"] for e in shown} == {1, 2, 3}
        for e in shown:
            grey, alpha = _grey(e["image_png_b64"])
            assert grey.shape == (m, m) and (alpha > 0).all()
            assert e["time"].startswith(e["point"]["date"])
            # Each exposure's own detection sits at the pass's combined position, within its own
            # error and the ~1" alignment noise of a single exposure.
            d = np.hypot(e["point"]["east_arcsec"] - p["point"]["east_arcsec"],
                         e["point"]["north_arcsec"] - p["point"]["north_arcsec"])
            assert d < 4 * np.hypot(e["point"]["sigma_arcsec"], 1.0)
            # And the object is visibly there: its pixel is far above the image's background.
            centre = (m - 1) / 2
            row = round(centre - e["point"]["north_arcsec"] / proof.PATCH_PIXEL_ARCSEC)
            col = round(centre - e["point"]["east_arcsec"] / proof.PATCH_PIXEL_ARCSEC)
            assert grey[row, col] > np.median(grey) + 40


# --- hand-made inputs: how fake movers fail ---------------------------------------------

RA, DEC = 150.0, 20.0
N = 201  # room for the difference-image test's comparison places around a saturated core
GRID = proof.patch_wcs(RA, DEC, N)
MJDS = (60900.0, 61080.0, 61265.0)


def _source(k: int, east: float, north: float, fluxes=None, sigma: float = 0.3) -> PassSource:
    ra, dec = _sky_at(east, north)
    return PassSource(
        pass_index=k, ra=ra, dec=dec, sigma_arcsec=sigma, mjd=MJDS[k], n_det=9, n_cover=9, snr=50.0,
        flux_by_detector=fluxes or {"D1": (100.0, 1.0), "D2": (80.0, 1.0)},
    )


def _sky_at(east: float, north: float) -> tuple[float, float]:
    x, y = (N - 1) / 2 - east / proof.PATCH_PIXEL_ARCSEC, (N - 1) / 2 - north / proof.PATCH_PIXEL_ARCSEC
    ra, dec = GRID.wcs_pix2world(x, y, 0)
    return float(ra), float(dec)


def _object(sources, parallax=None) -> TrackedObject:
    return TrackedObject(
        sources=sources, link="mover", ra=sources[0].ra, dec=sources[0].dec, ref_mjd=MJDS[0],
        pm_ra_cosdec=0.0, pm_dec=0.0, pm_err=1.0, chi2_dof=float("nan"), parallax=parallax,
    )


def _pos(sources):
    return {s.pass_index: checks.sky_offsets(GRID, N, s.ra, s.dec)[0] for s in sources}


def test_sky_offsets_round_trip():
    assert checks.sky_offsets(GRID, N, *_sky_at(12.0, -7.5))[0] == pytest.approx([12.0, -7.5], abs=1e-6)


def test_straight_track_passes_and_bent_track_fails():
    straight = [_source(0, 0, 0), _source(1, 5, 0), _source(2, 10, 0)]
    assert checks.track_line(straight, _pos(straight), {}, _object(straight)).status == "pass"
    bent = [_source(0, 0, 0), _source(1, 5, 6), _source(2, 10, 0)]
    result = checks.track_line(bent, _pos(bent), {}, _object(bent))
    assert result.status == "fail" and "one of them" in result.summary
    # With 3 passes the fit leans toward every point: a middle point 4.3 errors off leaves a raw
    # residual under 3 errors, and has to fail all the same.
    sigma = 0.3
    near = [_source(0, 0, 0, sigma=sigma), _source(1, 5, 4.3 * sigma, sigma=sigma), _source(2, 10, 0, sigma=sigma)]
    assert checks.track_line(near, _pos(near), {}, _object(near)).status == "fail"
    # A nearby star's parallax is a known reason for a bend.
    assert checks.track_line(bent, _pos(bent), {}, _object(bent, parallax=0.5)).status == "weak"
    two = straight[:2]
    assert checks.track_line(two, _pos(two), {}, _object(two)).status == "unknown"


def test_brightness_jump_without_variability_fails():
    jump = [_source(0, 0, 0), _source(1, 5, 0, {"D1": (300.0, 1.0), "D2": (90.0, 1.0)})]
    assert checks.same_look(jump, is_variable=False).status == "fail"
    assert checks.same_look(jump, is_variable=True).status == "unknown"
    none = [_source(0, 0, 0, {"D1": (100.0, 1.0)}), _source(1, 5, 0, {"D2": (100.0, 1.0)})]
    assert checks.same_look(none, is_variable=False).status == "unknown"


def _result(objects, unlinked=()) -> EngineResult:
    return EngineResult(
        frames=[], passes=[[0], [1], [2]], detections=[], frame_offsets_arcsec=[], objects=list(objects),
        unlinked=list(unlinked), pm_floor_arcsec_yr=0.0,
    )


def test_stand_in_at_the_new_spot_is_caught():
    mover = _object([_source(0, 0, 0), _source(1, 30, 0), _source(2, 60, 0)])
    fixed = _object([_source(k, 200, 200) for k in range(3)])
    clear = checks.no_stand_in(_result([mover, fixed]), mover, mover.sources, GRID, N, radius_arcsec=12.0)
    assert clear.status == "pass"
    # A star already sat where the "mover" ends up, in the first pass: two objects, not one.
    already = _source(0, 60, 1)
    caught = checks.no_stand_in(_result([mover, fixed], [already]), mover, mover.sources, GRID, N, radius_arcsec=12.0)
    assert caught.status == "fail" and "Aug 2025" in caught.details[0]
    short = _object([_source(0, 0, 0), _source(1, 4, 0), _source(2, 8, 0)])
    assert checks.no_stand_in(_result([short]), short, short.sources, GRID, N, radius_arcsec=12.0).status == "unknown"


def _blob(east: float, north: float, amp: float = 50.0, sigma_px: float = 2.0) -> np.ndarray:
    rows, cols = np.mgrid[0:N, 0:N]
    row = (N - 1) / 2 - north / proof.PATCH_PIXEL_ARCSEC
    col = (N - 1) / 2 - east / proof.PATCH_PIXEL_ARCSEC
    return amp * np.exp(-((rows - row) ** 2 + (cols - col) ** 2) / (2 * sigma_px**2))


def test_difference_image_test_tells_a_move_from_noise():
    rng = np.random.default_rng(3)
    stars = sum(_blob(e, n, 30.0) for e, n in [(-100, 90), (80, -60), (110, 100), (-60, -110)])
    first = stars + _blob(0, 0) + rng.normal(0, 1, (N, N))
    moved = stars + _blob(15, 0) + rng.normal(0, 1, (N, N))
    still = stars + _blob(0, 0) + rng.normal(0, 1, (N, N))
    args = {"lo": -2.0, "hi": 80.0, "pixel_arcsec": proof.PATCH_PIXEL_ARCSEC, "fwhm_arcsec": 12.0,
            "months": ("Aug 2025", "Aug 2026")}
    hit = checks.images_show_motion(first, moved, p_first=np.array([0.0, 0.0]), p_last=np.array([15.0, 0.0]), **args)
    assert hit.status == "pass" and hit.value > 5
    assert "where it was in Aug 2025 and a bright one where it is in Aug 2026" in hit.summary
    miss = checks.images_show_motion(first, still, p_first=np.array([0.0, 0.0]), p_last=np.array([15.0, 0.0]), **args)
    assert miss.status == "fail"
    tiny = checks.images_show_motion(first, moved, p_first=np.array([0.0, 0.0]), p_last=np.array([0.5, 0.0]), **args)
    assert tiny.status == "unknown"


def test_exposure_picks_share_slots_over_detectors_and_time():
    # Time-ordered ids, detectors interleaved as SPHEREx takes them; D2 has only one exposure.
    ids = list(range(10))
    dets = [1, 3, 1, 3, 2, 1, 3, 1, 3, 1]
    picked = proof._spread(ids, dets, k=5)
    assert picked == sorted(picked) and len(picked) == 5
    assert [dets[i] for i in picked].count(2) == 1
    assert {dets[i] for i in picked} == {1, 2, 3}
    # D1's two picks are its first and last exposure: spread over the pass.
    assert [i for i in picked if dets[i] == 1] == [0, 9]
    assert proof._spread([4, 7], [2, 2], k=6) == [4, 7]


def _saturated(east: float, north: float, core_px: float = 7.0) -> np.ndarray:
    """A star whose core is far above the white level, with a halo around it."""
    rows, cols = np.mgrid[0:N, 0:N]
    row = (N - 1) / 2 - north / proof.PATCH_PIXEL_ARCSEC
    col = (N - 1) / 2 - east / proof.PATCH_PIXEL_ARCSEC
    r = np.hypot(rows - row, cols - col)
    return np.where(r <= core_px, 1000.0, 200.0 * np.exp(-((r - core_px) ** 2) / 8.0))


def test_saturated_star_moves_on_the_edges_of_its_core():
    rng = np.random.default_rng(5)
    first = _saturated(0, 0) + rng.normal(0, 1, (N, N))
    last = _saturated(0, 9.8) + rng.normal(0, 1, (N, N))  # Barnard's Star: 9.8" north in a year
    centre = (N - 1) / 2
    core = checks.saturated_core_px(first, centre, centre, hi=80.0)
    # White out to where the halo drops below the white level: 7 + sqrt(8 ln 2.5) = 9.7 px.
    assert core == pytest.approx(9.7, abs=0.5)
    assert checks.saturated_core_px(first, 5, 5, hi=80.0) == 0.0  # empty sky
    args = {"lo": -2.0, "hi": 80.0, "pixel_arcsec": proof.PATCH_PIXEL_ARCSEC, "fwhm_arcsec": 9.0,
            "months": ("Aug 2025", "Aug 2026")}
    ends = {"p_first": np.array([0.0, 0.0]), "p_last": np.array([0.0, 9.8])}
    # Inside the white core both passes are equal: a stencil that ignores the core sees nothing.
    blind = checks.images_show_motion(first, last, **ends, **args)
    assert blind.status == "fail"
    seen = checks.images_show_motion(first, last, **ends, **args, core_px=core, saturated=True)
    assert seen.status == "pass" and seen.value > 5
    assert any("saturated" in d for d in seen.details)


def test_pair_across_the_track_is_not_a_move():
    rng = np.random.default_rng(6)
    first = _blob(0, 0) + rng.normal(0, 1, (N, N))
    # The image shifted east, but the measured "motion" points north: misalignment, not a move.
    sideways = _blob(10, 2) + rng.normal(0, 1, (N, N))
    args = {"lo": -2.0, "hi": 80.0, "pixel_arcsec": proof.PATCH_PIXEL_ARCSEC, "fwhm_arcsec": 12.0,
            "months": ("Aug 2025", "Aug 2026")}
    got = checks.images_show_motion(first, sideways, p_first=np.array([0.0, 0.0]), p_last=np.array([0.0, 3.0]), **args)
    assert got.status != "pass"


def _seen(check) -> float:
    """The shift the pixels alone show, from the check's numbers."""
    line = next(d for d in check.details if d.startswith("Shift the pixels alone show"))
    return float(line.split("about ")[1].split("″")[0])


def test_pixels_alone_measure_the_shift():
    rng = np.random.default_rng(7)
    args = {"lo": -2.0, "hi": 80.0, "pixel_arcsec": proof.PATCH_PIXEL_ARCSEC, "fwhm_arcsec": 12.0,
            "months": ("Aug 2025", "Aug 2026"), "noise_arcsec": 0.4}
    first = _blob(0, 0) + rng.normal(0, 1, (N, N))
    moved = _blob(15, 0) + rng.normal(0, 1, (N, N))
    got = checks.images_show_motion(first, moved, p_first=np.array([0.0, 0.0]), p_last=np.array([15.0, 0.0]), **args)
    assert got.status == "pass" and _seen(got) == pytest.approx(15.0, abs=2.0)
    assert "matching the engine's 15.0″" in got.summary
    # Saturated: Barnard's Star's 9.8" north, read off the edges of its white core.
    first = _saturated(0, 0) + rng.normal(0, 1, (N, N))
    last = _saturated(0, 9.8) + rng.normal(0, 1, (N, N))
    centre = (N - 1) / 2
    core = checks.saturated_core_px(first, centre, centre, hi=80.0)
    sat = checks.images_show_motion(
        first, last, p_first=np.array([0.0, 0.0]), p_last=np.array([0.0, 9.8]), **{**args, "fwhm_arcsec": 9.0}, core_px=core,
    )
    assert sat.status == "pass" and _seen(sat) == pytest.approx(9.8, abs=1.5)


def test_misaligned_bright_star_is_not_a_mover():
    """A fixed saturated star 0.6" off between passes leaves a strong edge pair, far above the
    sky's noise, but no bigger than the 0.4" alignment noise explains."""
    rng = np.random.default_rng(8)
    first = _saturated(0, 0) + rng.normal(0, 1, (N, N))
    last = _saturated(0, 0.6) + rng.normal(0, 1, (N, N))
    centre = (N - 1) / 2
    got = checks.images_show_motion(
        first, last, lo=-2.0, hi=80.0, p_first=np.array([0.0, 0.0]), p_last=np.array([0.0, 4.0]),
        pixel_arcsec=proof.PATCH_PIXEL_ARCSEC, fwhm_arcsec=9.0, months=("Aug 2025", "Aug 2026"),
        core_px=checks.saturated_core_px(first, centre, centre, hi=80.0), noise_arcsec=0.4,
    )
    assert got.status != "pass", got.summary


def test_exposure_window_holds_a_saturated_core(served, monkeypatch):
    """With a white core in the pass images, the exposure window grows to show its edges."""
    monkeypatch.setattr(proof, "saturated_core_px", lambda *a: 20.0 / proof.PATCH_PIXEL_ARCSEC)
    proof._cache.clear()
    ev = _get(_object_at(served, *MOVER_START))
    assert ev["exposure_arcsec"] >= 2 * 20.0 + 2 * proof.EXPOSURE_MARGIN_ARCSEC
    assert ev["exposure_px"] <= ev["patch_px"]


def _image_args(**over):
    return {"lo": -2.0, "hi": 80.0, "pixel_arcsec": proof.PATCH_PIXEL_ARCSEC, "fwhm_arcsec": 12.0,
            "months": ("Aug 2025", "Aug 2026"), "noise_arcsec": 0.4, **over}


def test_a_source_that_only_appears_is_not_a_move():
    """A new bright source ahead of a fixed star (a flare, a passing asteroid) leaves the bright
    half of the pair but no dark half: the weaker half decides."""
    rng = np.random.default_rng(9)
    first = _blob(0, 0, 10.0) + rng.normal(0, 1, (N, N))
    last = _blob(0, 0, 10.0) + _blob(15, 0, 60.0) + rng.normal(0, 1, (N, N))
    got = checks.images_show_motion(first, last, p_first=np.array([0.0, 0.0]), p_last=np.array([15.0, 0.0]), **_image_args())
    assert got.status == "fail", got.summary
    assert any(d.startswith("Darker where it was") for d in got.details)


def test_a_shift_beyond_the_calibration_is_not_called_a_match():
    rng = np.random.default_rng(10)
    first = _blob(0, 0) + rng.normal(0, 1, (N, N))
    brighter = _blob(15, 0, 150.0) + rng.normal(0, 1, (N, N))  # moved and got 3x brighter
    got = checks.images_show_motion(first, brighter, p_first=np.array([0.0, 0.0]), p_last=np.array([15.0, 0.0]), **_image_args())
    assert "more than 30.0″" in got.details[0]
    assert got.status != "pass" and "matching" not in got.summary


def test_stacked_runs_do_not_count_single_exposures():
    sources = [_source(0, 0, 0), _source(1, 5, 0)]
    assert checks.many_exposures(sources, stacked=True).status == "unknown"


def test_resolved_neighbour_is_not_a_stand_in():
    mover = _object([_source(0, 0, 0), _source(1, 30, 0), _source(2, 60, 0)])
    # A fixed star 8" from the mover's middle spot, seen apart from it in every pass.
    neighbour = _object([_source(k, 38, 0) for k in range(3)])
    got = checks.no_stand_in(_result([mover, neighbour]), mover, mover.sources, GRID, N, radius_arcsec=12.0)
    assert got.status == "pass", got.details


def test_fixed_star_wording_does_not_claim_motion():
    still = [_source(0, 0, 0), _source(2, 0.2, 0.1)]
    got = checks.beyond_noise(still[0], still[1], _pos(still), {})
    assert got.status == "fail" and "moved" not in got.summary and "position changed" in got.summary
