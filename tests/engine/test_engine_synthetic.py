"""Engine behaviour on synthetic SPHEREx-like frames with known truth.

Fast and offline: every scenario plants stars with exactly known positions,
motions and fluxes, so the engine's answers can be checked precisely.
"""
from __future__ import annotations

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
from scipy.special import erf

from planetx.engine import Frame, detect_sources, find_motion, group_into_passes

PIX = 6.15  # arcsec / px, as SPHEREx
FWHM = 9.0  # arcsec, as SPHEREx PSF_FWHM
N = 60
CENTER = SkyCoord(150.0 * u.deg, 20.0 * u.deg)
# Three passes ~6 months apart, 8 frames each spread over ~3 weeks.
PASS_MJDS = [60900.0, 61080.0, 61265.0]
WAVES = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15]


def make_wcs(rot_deg: float = 0.0, shift_arcsec=(0.0, 0.0)) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    c = CENTER.spherical_offsets_by(shift_arcsec[0] * u.arcsec, shift_arcsec[1] * u.arcsec)
    w.wcs.crval = [c.ra.deg, c.dec.deg]
    w.wcs.crpix = [(N + 1) / 2, (N + 1) / 2]
    th = np.radians(rot_deg)
    s = PIX / 3600
    w.wcs.cd = [[-s * np.cos(th), s * np.sin(th)], [s * np.sin(th), s * np.cos(th)]]
    return w


def pixel_integrated_psf(x0: float, y0: float, flux: float) -> np.ndarray:
    """Gaussian PSF integrated over each pixel (SPHEREx is undersampled)."""
    sig = FWHM / PIX / 2.3548
    edges = np.arange(N + 1) - 0.5
    fx = 0.5 * np.diff(erf((edges - x0) / (np.sqrt(2) * sig)))
    fy = 0.5 * np.diff(erf((edges - y0) / (np.sqrt(2) * sig)))
    return flux * np.outer(fy, fx)


def make_frame(stars, mjd, wave, rng, rot_deg=0.0, wcs_error=(0.0, 0.0), saturate_above=None, noise=1.0, detector=1):
    """stars: list of (east0, north0, pm_e, pm_n, flux_fn(mjd)) in arcsec, arcsec/yr."""
    true_wcs = make_wcs(rot_deg)
    data = rng.normal(0.0, noise, (N, N)) + 5.0
    t_yr = (mjd - PASS_MJDS[0]) / 365.25
    for e0, n0, pme, pmn, flux in stars:
        c = CENTER.spherical_offsets_by((e0 + pme * t_yr) * u.arcsec, (n0 + pmn * t_yr) * u.arcsec)
        x, y = true_wcs.world_to_pixel(c)
        data += pixel_integrated_psf(float(x), float(y), flux(mjd) if callable(flux) else flux)
    saturated = None
    if saturate_above is not None:
        saturated = data > saturate_above
        data = np.where(saturated, np.nan, data)
    # The frame's recorded WCS can be slightly wrong (pointing error).
    return Frame(
        data=data,
        variance=np.full((N, N), noise**2),
        wcs=make_wcs(rot_deg, wcs_error),
        mjd=mjd,
        detector=detector,
        wavelength_um=wave,
        psf_fwhm_arcsec=FWHM,
        saturated=saturated,
    )


def field_stars(rng, n=40, flux=(150, 2000)):
    pos = rng.uniform(-150, 150, (n, 2))
    return [(e, nn, 0.0, 0.0, f) for (e, nn), f in zip(pos, rng.uniform(*flux, n))]


def survey(stars, rng, frames_per_pass=8, **kw):
    frames = []
    for p, m0 in enumerate(PASS_MJDS):
        for k in range(frames_per_pass):
            frames.append(
                make_frame(
                    stars, m0 + 3 * k, WAVES[k % len(WAVES)], rng,
                    rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)), **kw,
                )
            )
    return frames


def nearest(result, east, north):
    def sep(o):
        e, n = CENTER.spherical_offsets_to(SkyCoord(o.ra * u.deg, o.dec * u.deg))
        return np.hypot(e.arcsec - east, n.arcsec - north)

    return min(result.objects, key=sep)


def test_group_into_passes_splits_on_gaps_only():
    mjds = [10, 11, 50, 200, 230, 400]
    assert group_into_passes(mjds, gap_days=60) == [[0, 1, 2], [3, 4], [5]]
    assert group_into_passes([], gap_days=60) == []
    # Input order doesn't matter: passes come out time-ordered.
    assert group_into_passes([400, 10, 200], gap_days=60) == [[1], [2], [0]]


def test_detects_star_at_subpixel_position():
    rng = np.random.default_rng(1)
    f = make_frame([(10.0, -20.0, 0, 0, 3000.0)], PASS_MJDS[0], 1.0, rng)
    (d,) = [d for d in detect_sources(f) if d.snr > 20]
    e, n = CENTER.spherical_offsets_to(SkyCoord(d.ra * u.deg, d.dec * u.deg))
    assert abs(e.arcsec - 10.0) < 0.5 and abs(n.arcsec + 20.0) < 0.5
    assert d.sigma_arcsec < 1.0


def test_recovers_known_proper_motion_and_nothing_else_moves():
    rng = np.random.default_rng(2)
    stars = field_stars(rng) + [(0.0, -10.0, -1.0, 10.0, 1500.0)]  # Barnard-like
    r = find_motion(survey(stars, rng))
    assert [len(p) for p in r.passes] == [8, 8, 8]
    movers = r.movers
    assert len(movers) == 1, [(m.pm_total, m.ra, m.dec) for m in movers]
    m = movers[0]
    assert m.pm_ra_cosdec == pytest.approx(-1.0, abs=0.6)
    assert m.pm_dec == pytest.approx(10.0, abs=0.6)
    assert m.pm_significance > 5
    # Frame pointing errors (0.7" rms) were corrected by alignment.
    assert r.pm_floor_arcsec_yr < 0.6


def test_static_field_produces_no_movers():
    rng = np.random.default_rng(3)
    r = find_motion(survey(field_stars(rng, n=60), rng))
    assert r.movers == []
    assert len(r.objects) >= 40


def test_slow_motion_below_one_pixel_is_not_a_mover():
    # 2"/yr over ~1 yr = 0.33 px: real but below what SPHEREx can claim.
    rng = np.random.default_rng(4)
    stars = field_stars(rng) + [(30.0, 30.0, 2.0, 0.0, 2500.0)]
    r = find_motion(survey(stars, rng))
    assert r.movers == []
    # ...but it was tracked and measured, not simply missed.
    slow = nearest(r, 31.0, 30.0)
    assert slow.pm_ra_cosdec == pytest.approx(2.0, abs=0.7)


def test_flags_star_that_doubles_in_brightness():
    rng = np.random.default_rng(5)

    def brightening(mjd):
        return 600.0 if mjd < PASS_MJDS[1] - 30 else 1200.0

    stars = field_stars(rng) + [(-40.0, 25.0, 0.0, 0.0, brightening)]
    r = find_motion(survey(stars, rng))
    var = r.variables
    assert len(var) == 1
    v = nearest(r, -40.0, 25.0)
    assert v.is_variable
    assert v.variability["ratio"] == pytest.approx(2.0, rel=0.15)
    assert not v.is_mover


def test_saturated_mover_located_from_masked_patch():
    rng = np.random.default_rng(6)
    stars = field_stars(rng) + [(0.0, 0.0, 0.0, 10.0, 60000.0)]
    frames = survey(stars, rng, saturate_above=2000.0)
    assert all(f.saturated.any() for f in frames)
    r = find_motion(frames)
    (m,) = r.movers
    assert m.pm_dec == pytest.approx(10.0, abs=1.5)
    assert all(d.saturated for s in m.sources for d in s.detections)


def test_stacking_finds_a_mover_too_faint_for_single_frames():
    rng = np.random.default_rng(7)
    faint = (20.0, -30.0, -8.0, 0.7, 14.0)  # ~2.5 sigma per frame
    stars = field_stars(rng, flux=(400, 2000)) + [faint]
    frames = survey(stars, rng, frames_per_pass=12)
    plain = find_motion(frames)
    assert all(abs(m.pm_ra_cosdec + 8) > 2 for m in plain.movers)
    stacked = find_motion(frames, stack=True)
    assert stacked.stacked and [len(p) for p in stacked.passes] == [1, 1, 1]
    (m,) = stacked.movers
    assert m.pm_ra_cosdec == pytest.approx(-8.0, abs=2.0)


def test_single_pass_reports_it_cannot_measure_motion():
    rng = np.random.default_rng(8)
    frames = [make_frame(field_stars(rng), PASS_MJDS[0] + k, 1.0, rng) for k in range(4)]
    r = find_motion(frames)
    assert r.movers == []
    assert any("only one pass" in n for n in r.notes)


def test_rejects_empty_input():
    with pytest.raises(ValueError):
        find_motion([])


def test_red_object_seen_by_one_detector_is_kept():
    # A Y dwarf is visible only at 4-5 um: here, only in detector 3 frames,
    # i.e. a third of each pass. It must still be tracked.
    rng = np.random.default_rng(9)
    stars = field_stars(rng)
    # Put it where no field star is within 40" of its track (blending isn't
    # what this test is about).
    grid = [(e, n) for e in range(-100, 101, 10) for n in range(-100, 81, 10)]
    e0, n0 = max(grid, key=lambda p: min(np.hypot(s[0] - p[0], s[1] - p[1] - 5) for s in stars))
    assert min(np.hypot(s[0] - e0, s[1] - n0 - 5) for s in stars) > 40
    red = (float(e0), float(n0), 0.0, 10.0, 1500.0)
    frames = []
    for m0 in PASS_MJDS:
        for k in range(9):
            det = 1 + k % 3
            frames.append(
                make_frame(
                    stars + ([red] if det == 3 else []), m0 + 2 * k, WAVES[k % len(WAVES)], rng,
                    rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)), detector=det,
                )
            )
    (m,) = find_motion(frames).movers
    assert m.pm_dec == pytest.approx(10.0, abs=1.0)


def test_two_pass_survey_needs_double_significance():
    rng = np.random.default_rng(10)
    stars = field_stars(rng) + [(0.0, 0.0, 0.0, 10.0, 1500.0)]
    frames = [f for f in survey(stars, rng) if f.mjd < PASS_MJDS[2] - 30]
    r = find_motion(frames)
    for m in r.movers:
        assert m.pm_significance >= 10


def test_frame_without_usable_pixels_is_skipped_not_fatal():
    rng = np.random.default_rng(11)
    frames = survey(field_stars(rng) + [(0.0, -10.0, -1.0, 10.0, 1500.0)], rng)
    frames[3].data[:] = np.nan
    r = find_motion(frames)
    assert any("skipped 1 frames" in n for n in r.notes)
    assert len(r.movers) == 1
