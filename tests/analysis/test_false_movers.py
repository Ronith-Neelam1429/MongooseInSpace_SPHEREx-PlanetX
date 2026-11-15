"""The false-mover audit's verdicts, on hand-built tracks with known truth (offline)."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from astropy.wcs import WCS

from planetx.analysis import false_movers as fm
from planetx.classify.catalogs import CatalogObject
from planetx.engine import EngineResult, Frame, PassSource, TrackedObject
from planetx.engine.detect import Detection
from planetx.engine.motion import DAYS_PER_YEAR, SkyPlane

CENTER = (150.0, 20.0)
PIX = 6.15
N = 60
PASS_MJDS = [60900.0, 61080.0, 61265.0]
PLANE = SkyPlane(*CENTER)


def _wcs() -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = list(CENTER)
    w.wcs.crpix = [(N + 1) / 2, (N + 1) / 2]
    w.wcs.cdelt = [-PIX / 3600, PIX / 3600]
    return w


def _frame(mjd: float) -> Frame:
    return Frame(data=np.zeros((N, N)), variance=np.ones((N, N)), wcs=_wcs(), mjd=mjd, detector=1,
                 psf_fwhm_arcsec=9.0)


def _detection(frame_index: int, frame: Frame, east: float, north: float, **kw) -> Detection:
    ra, dec = PLANE.sky(east, north)
    x, y = frame.wcs.all_world2pix(float(ra), float(dec), 0)
    return Detection(frame_index=frame_index, x=float(x), y=float(y), ra=float(ra), dec=float(dec),
                     sigma_arcsec=0.6, flux=100.0, flux_err=5.0, snr=kw.pop("snr", 30.0), mjd=frame.mjd,
                     detector=1, wavelength_um=kw.pop("wavelength_um", 1.0), **kw)


def _run(tracks, extra_detections=(), frames_per_pass=2):
    """An EngineResult holding `tracks`: each a list of (east, north) per pass, in arcsec.

    Every pass has `frames_per_pass` frames, each detecting the object at that
    pass's position. `extra_detections` are (pass, frame-in-pass, Detection kwargs
    with east/north) added to those frames (e.g. a saturated neighbour).
    """
    frames, passes, detections = [], [], []
    for p, mjd in enumerate(PASS_MJDS):
        passes.append([])
        for k in range(frames_per_pass):
            frames.append(_frame(mjd + 3 * k))
            passes[-1].append(len(frames) - 1)
            detections.append([])
    objects = []
    for track in tracks:
        sources = []
        for p, (e, n) in enumerate(track):
            if e is None:
                continue
            dets = []
            for fi in passes[p]:
                d = _detection(fi, frames[fi], e, n)
                detections[fi].append(d)
                dets.append(d)
            ra, dec = PLANE.sky(e, n)
            sources.append(PassSource(p, float(ra), float(dec), 0.2, float(np.mean([d.mjd for d in dets])),
                                      len(dets), len(dets), 30.0, {"D1": (100.0, 5.0)}, dets))
        t = np.array([s.mjd for s in sources]) / DAYS_PER_YEAR
        x, y = PLANE.offsets(np.array([s.ra for s in sources]), np.array([s.dec for s in sources]))
        span = t[-1] - t[0]
        pme, pmn = ((x[-1] - x[0]) / span, (y[-1] - y[0]) / span) if span > 0 else (0.0, 0.0)
        ra0, dec0 = PLANE.sky(float(x.mean()), float(y.mean()))
        objects.append(TrackedObject(sources=sources, link="static", ra=float(ra0), dec=float(dec0),
                                     ref_mjd=float(t.mean() * DAYS_PER_YEAR), pm_ra_cosdec=float(pme),
                                     pm_dec=float(pmn), pm_err=0.3, chi2_dof=1.0, is_mover=True))
    for p, k, kw in extra_detections:
        fi = passes[p][k]
        kw = dict(kw)
        detections[fi].append(_detection(fi, frames[fi], kw.pop("east"), kw.pop("north"), **kw))
    return EngineResult(frames=frames, passes=passes, detections=detections,
                        frame_offsets_arcsec=[(0.0, 0.0)] * len(frames), objects=objects, unlinked=[],
                        pm_floor_arcsec_yr=0.0)


def _star(east, north, pm=(0.0, 0.0), g=16.0, name="Gaia DR3 1", epoch_mjd=PASS_MJDS[0]) -> CatalogObject:
    ra, dec = PLANE.sky(east, north)
    return CatalogObject(catalog="gaia", name=name, ra=float(ra), dec=float(dec), epoch_mjd=epoch_mjd,
                         pm_ra_cosdec=pm[0], pm_dec=pm[1], magnitude=g)


def _walk(e0, n0, pm_east, pm_north=0.0):
    return [(e0 + pm_east * (m - PASS_MJDS[0]) / DAYS_PER_YEAR, n0 + pm_north * (m - PASS_MJDS[0]) / DAYS_PER_YEAR)
            for m in PASS_MJDS]


def test_track_on_a_fast_gaia_star_is_a_real_mover():
    r = _run([_walk(0, 0, -8.0)])
    (a,) = fm.audit_result(r, [_star(0, 0, pm=(-8.0, 0.0))], field_ra=150, field_dec=20)
    assert (a.truth, a.cause) == ("real_mover", None)
    assert a.gaia_pm_total == pytest.approx(8.0)
    assert "Gaia confirms" in a.evidence


def test_track_on_a_static_gaia_star_is_fake_and_explained():
    # The track wanders 2.5"/yr over a Gaia star that does not move, with a
    # neighbour of similar brightness 3" away: a blend.
    r = _run([_walk(0, 0, 2.5)])
    stars = [_star(1.2, 0, g=16.0, name="Gaia DR3 1"), _star(1.2, 3.0, g=16.8, name="Gaia DR3 2")]
    (a,) = fm.audit_result(r, stars, field_ra=150, field_dec=20)
    assert (a.truth, a.cause) == ("static", "blend")
    assert "Gaia DR3 1" in a.evidence and "3.0\"" in a.evidence


def test_static_star_without_a_pixel_problem_is_fake_for_another_reason():
    r = _run([_walk(0, 0, 2.5)])
    (a,) = fm.audit_result(r, [_star(1.2, 0)], field_ra=150, field_dec=20)
    assert (a.truth, a.cause) == ("static", "other")


def test_star_too_faint_for_single_frames_still_settles_the_truth():
    # Stacks reach below G 19.5 (and red stars are faint in G): one faint static
    # Gaia star under every pass still shows the motion is not real ...
    r = _run([_walk(0, 0, 2.5)])
    (a,) = fm.audit_result(r, [_star(1.2, 0, g=20.6)], field_ra=150, field_dec=20)
    assert a.truth == "static" and a.gaia_g_mag == pytest.approx(20.6)
    # ... but faint stars under different passes are not a "hop": they are everywhere.
    track = _walk(-10, 0, 10.0)
    hop = [_star(e, n, g=20.6, name=f"Gaia DR3 {k}") for k, (e, n) in enumerate(track)]
    (b,) = fm.audit_result(_run([track]), hop, field_ra=150, field_dec=20)
    assert (b.truth, b.cause) == ("ambiguous", None)


def test_track_hopping_between_static_stars_is_a_chance_link():
    # Each pass sits on a different slow Gaia star 10" apart.
    track = _walk(-10, 0, 10.0)
    r = _run([track])
    stars = [_star(e, n, name=f"Gaia DR3 {k}") for k, (e, n) in enumerate(track)]
    (a,) = fm.audit_result(r, stars, field_ra=150, field_dec=20)
    assert (a.truth, a.cause) == ("static", "chance_link")


def test_unmatched_track_is_unsettled_unless_its_pixels_show_a_problem():
    clean = _run([_walk(0, 0, 8.0)])
    (a,) = fm.audit_result(clean, [], field_ra=150, field_dec=20)
    assert (a.truth, a.cause) == ("ambiguous", None)
    # The same track next to a saturated star (one frame of pass 2).
    halo = _run([_walk(0, 0, 8.0)], extra_detections=[(1, 0, {"east": 40.0, "north": 0.0, "saturated": True,
                                                              "sat_area": 20, "snr": 1e4})])
    (b,) = fm.audit_result(halo, [], field_ra=150, field_dec=20)
    assert (b.truth, b.cause) == ("ambiguous", "saturated_halo")
    assert "saturated star" in b.evidence


def test_detections_at_the_frame_edge_are_an_edge_problem():
    edge = (N / 2 - 1.5) * PIX  # 1.5 px from the frame's east edge
    r = _run([[(0, 0), (0, 0), (edge, 0)]])
    (a,) = fm.audit_result(r, [], field_ra=150, field_dec=20)
    assert a.cause == "edge"
    assert "pass 3" in a.evidence


def test_wavelength_term_that_explains_the_motion_is_found():
    # A colour-dependent centroid: 3"/um, and pass 3 sampled redder frames than pass 1.
    r = _run([[(0, 0), (0, 0), (0, 0)]], frames_per_pass=6)
    obj = r.objects[0]
    rng = np.random.default_rng(1)
    for s in obj.sources:
        waves = 1.0 + 0.1 * np.arange(6) + 0.25 * s.pass_index
        for d, lam in zip(s.detections, waves, strict=True):
            d.wavelength_um = float(lam)
            ra, dec = PLANE.sky(3.0 * (lam - 1.25) + rng.normal(0, 0.1), rng.normal(0, 0.1))
            d.ra, d.dec, d.sigma_arcsec = float(ra), float(dec), 0.15
    fit = fm.wavelength_fit(obj)
    assert fit["plain"]["pm"] > 1.0
    assert fit["wave"]["pm"] < fit["wave"]["pm_err"] * 3
    assert fit["wave"]["slope_arcsec_um"] == pytest.approx(3.0, rel=0.1)
    hit, text, _ = fm.check_wavelength(obj, PIX, 1.0)
    assert hit and "with wavelength" in text


def test_fast_star_split_into_two_tracks_is_reported():
    first, last = _walk(0, 0, 8.0)[:2], _walk(0, 0, 8.0)[2:]
    r = _run([first + [(None, None)], [(None, None), (None, None)] + last])
    (row,) = fm.fast_star_recovery(r, [_star(0, 0, pm=(8.0, 0.0))], field_name="f")
    assert row["status"] == "split" and row["n_tracks"] == 2
    whole = _run([_walk(0, 0, 8.0)])
    (row,) = fm.fast_star_recovery(whole, [_star(0, 0, pm=(8.0, 0.0))])
    assert row["status"] == "mover"
    # Too slow to shift a pixel: not counted.
    assert fm.fast_star_recovery(whole, [_star(0, 0, pm=(0.6, 0.0))]) == []


def test_gator_gaia_table_is_read_in_arcsec_per_year():
    import io

    from astropy.table import MaskedColumn, Table

    # IRSA's answer is an IPAC table (fixed width, "null" for missing values).
    table = Table({
        "source_id": [4472832130942575872, 1],
        "ra": [269.448502, 269.4], "dec": [4.73942, 4.7], "ref_epoch": [2016.0, 2016.0],
        "pmra": MaskedColumn([-801.551, 0.0], mask=[False, True]),
        "pmdec": MaskedColumn([10362.394, 0.0], mask=[False, True]),
        "phot_g_mean_mag": [8.19, 19.2],
    })
    buf = io.StringIO()
    table.write(buf, format="ipac")
    text = buf.getvalue()
    assert "null" in text
    stars = fm.parse_gator_gaia(text)
    assert [s.name for s in stars] == ["Gaia DR3 4472832130942575872", "Gaia DR3 1"]
    assert stars[0].pm_dec == pytest.approx(10.362394)
    assert stars[0].epoch_mjd == pytest.approx(57388.5)
    assert stars[1].pm_ra_cosdec is None
    with pytest.raises(ValueError):
        fm.parse_gator_gaia('[struct stat="ERROR", msg="bad"]')


def test_summary_and_json_follow_the_results_contract():
    r = _run([_walk(0, 0, 2.5), _walk(50, 50, -8.0)])
    stars = [_star(1.2, 0), _star(50, 50, pm=(-8.0, 0.0), name="Gaia DR3 9")]
    audits = fm.audit_result(r, stars, field_ra=150, field_dec=20, field_name="test")
    assert fm.summarize(audits) == {
        "n_movers": 2, "n_fake": 1, "by_cause": {"other": 1},
        "by_truth": {"real_mover": 1, "static": 1, "ambiguous": 0},
    }
    for a in audits:
        row = a.to_json()
        for key in ("ra", "dec", "field_ra", "field_dec", "pm_total", "significance", "snr", "truth", "cause",
                    "evidence"):
            assert key in row
        assert row["truth"] in fm.TRUTHS and (row["cause"] is None or row["cause"] in fm.CAUSES)
        json.dumps(row, allow_nan=False)  # NaN checks are written as null
    assert fm.clean_json({"a": math.nan, "b": np.float64(1.23456789), "c": [np.int64(3)]}) == {
        "a": None, "b": 1.234568, "c": [3]}
