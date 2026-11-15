"""Injecting fake movers into frames, and scoring what the engine returns.

Offline. Frames are synthetic (noise only, tangent-plane WCS) so every injected flux,
position and motion is known exactly.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from planetx.analysis.injection import (
    EpsfLibrary,
    Injection,
    Match,
    Psf,
    SiteFinder,
    ab_mag_to_flux_mjy,
    flux_mjy_to_ab_mag,
    inject,
    match_injections,
    pixel_solid_angle_sr,
)
from planetx.engine import Frame, find_motion

PIX = 6.15
FWHM = 9.0
N = 60
CENTER = SkyCoord(150.0 * u.deg, 20.0 * u.deg)
PASS_MJDS = [60900.0, 61080.0, 61265.0]
NOISE = 0.01  # MJy/sr


def make_wcs(shift_arcsec=(0.0, 0.0), rot_deg=0.0) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    c = CENTER.spherical_offsets_by(shift_arcsec[0] * u.arcsec, shift_arcsec[1] * u.arcsec)
    w.wcs.crval = [c.ra.deg, c.dec.deg]
    w.wcs.crpix = [(N + 1) / 2, (N + 1) / 2]
    th, s = np.radians(rot_deg), PIX / 3600
    w.wcs.cd = [[-s * np.cos(th), s * np.sin(th)], [s * np.sin(th), s * np.cos(th)]]
    return w


def blank_frame(mjd, rng=None, noise=0.0, wcs=None, detector=1) -> Frame:
    data = rng.normal(0.0, noise, (N, N)) if rng is not None and noise else np.zeros((N, N))
    return Frame(
        data=data, variance=np.full((N, N), max(noise, 1e-3) ** 2), wcs=wcs or make_wcs(), mjd=mjd,
        detector=detector, wavelength_um=1.0, psf_fwhm_arcsec=FWHM,
    )


def at(east, north, mjd=60900.0, pm=(0.0, 0.0), mag=16.0, **kw) -> Injection:
    c = CENTER.spherical_offsets_by(east * u.arcsec, north * u.arcsec)
    return Injection(c.ra.deg, c.dec.deg, mjd, pm[0], pm[1], mag, **kw)


def centroid(frame: Frame) -> tuple[float, float]:
    d = np.nan_to_num(frame.data)
    yy, xx = np.indices(d.shape)
    return float((d * xx).sum() / d.sum()), float((d * yy).sum() / d.sum())


# --- photometry ---------------------------------------------------------------------


def test_ab_zero_point_and_roundtrip():
    # AB 23.9 is exactly 1 microJy; 8.9 is 3631 Jy / 10^(0.4*8.9) = 1 Jy.
    assert ab_mag_to_flux_mjy(23.9) == pytest.approx(1e-3, rel=2e-3)
    assert ab_mag_to_flux_mjy(8.9) == pytest.approx(1e3, rel=2e-3)
    assert flux_mjy_to_ab_mag(ab_mag_to_flux_mjy(17.3)) == pytest.approx(17.3)


def test_injected_flux_matches_magnitude_in_surface_brightness_units():
    f = blank_frame(60900.0)
    (out,) = inject([f], [at(0, 0, mag=15.0)])
    # Summed over pixels, MJy/sr x sr per pixel is the source's flux in MJy.
    total_mjy = np.nansum(out.data) * pixel_solid_angle_sr(f)
    assert total_mjy * 1e9 == pytest.approx(ab_mag_to_flux_mjy(15.0), rel=1e-9)  # MJy -> mJy is 1e9
    # And it does not alter the input frame.
    assert not f.data.any()


def test_brighter_by_five_magnitudes_is_100_times_the_signal():
    f = blank_frame(60900.0)
    a, b = inject([f], [at(0, 0, mag=15.0)])[0], inject([f], [at(0, 0, mag=10.0)])[0]
    assert b.data.sum() / a.data.sum() == pytest.approx(100.0, rel=1e-9)


# --- position and motion -----------------------------------------------------------


@pytest.mark.parametrize("east,north", [(0.0, 0.0), (31.2, -17.4), (-40.0, 22.5)])
def test_static_source_lands_where_the_wcs_says(east, north):
    f = blank_frame(60900.0)
    (out,) = inject([f], [at(east, north)])
    x, y = f.wcs.world_to_pixel(CENTER.spherical_offsets_by(east * u.arcsec, north * u.arcsec))
    cx, cy = centroid(out)
    # The 9x9 box truncates at |d| > 4 px, so the centroid is good to a few 0.01 px.
    assert (cx, cy) == pytest.approx((float(x), float(y)), abs=0.03)


def test_source_follows_proper_motion_to_the_frame_date():
    rot = make_wcs(rot_deg=37.0)  # a rotated frame: motion east/north must not be confused with x/y
    inj = at(5.0, -8.0, mjd := 60900.0, pm=(6.0, -3.0))
    for dt_days in (0.0, 180.0, 365.25):
        f = blank_frame(mjd + dt_days, wcs=rot)
        (out,) = inject([f], [inj])
        yr = dt_days / 365.25
        want = CENTER.spherical_offsets_by((5.0 + 6.0 * yr) * u.arcsec, (-8.0 - 3.0 * yr) * u.arcsec)
        x, y = rot.world_to_pixel(want)
        assert centroid(out) == pytest.approx((float(x), float(y)), abs=0.03)


def test_parallax_adds_an_annual_wobble_zero_at_the_reference_date():
    inj = at(0, 0, 60900.0, parallax_arcsec=0.5)
    ra, dec = inj.position([60900.0, 60900.0 + 91.3, 60900.0 + 182.6, 60900.0 + 365.25])
    sep = SkyCoord(ra * u.deg, dec * u.deg).separation(SkyCoord(inj.ra * u.deg, inj.dec * u.deg)).arcsec
    assert sep[0] == pytest.approx(0.0, abs=1e-9)
    assert sep[1:3].max() <= 2 * 0.5 + 1e-6 and sep[1:3].max() > 0.1  # moved, within the parallax ellipse
    assert sep[3] == pytest.approx(0.0, abs=0.02)  # a year later it is back


def test_masked_pixels_stay_masked_and_off_frame_sources_are_skipped():
    f = blank_frame(60900.0)
    f.data[30, 30] = np.nan
    (out,) = inject([f], [at(0, 0), at(5000.0, 0.0)])  # the second is far outside
    assert np.isnan(out.data[30, 30])
    assert np.nansum(out.data) > 0
    assert len(out.meta["injected"]) == 2


# --- PSF ---------------------------------------------------------------------------------


def test_psf_shares_sum_to_one_and_peak_at_the_source():
    psf = Psf.gaussian(FWHM, PIX)
    rows, cols, share = psf.shares(10.3, 20.7)
    assert share.sum() == pytest.approx(1.0)
    assert (rows.ravel()[share.argmax()], cols.ravel()[share.argmax()]) == (21, 10)
    assert float((share * cols).sum()) == pytest.approx(10.3, abs=0.01)


def _epsf_hdul(detector: int, ov: int = 5, size: int = 33, sigma_px: float = 0.7) -> fits.HDUList:
    r = (np.arange(size) - (size - 1) / 2) / ov
    k = np.exp(-(r[:, None] ** 2 + r[None, :] ** 2) / (2 * sigma_px**2)).astype(np.float32)
    k /= k.sum()
    xc, yc = np.array([100.0, 900.0], np.float32), np.array([100.0, 100.0], np.float32)
    wide = np.exp(-(r[:, None] ** 2 + r[None, :] ** 2) / (2 * (2 * sigma_px) ** 2)).astype(np.float32)
    wide /= wide.sum()
    table = fits.BinTableHDU.from_columns([
        fits.Column(name="XCENTER", format="E", array=xc),
        fits.Column(name="YCENTER", format="E", array=yc),
        fits.Column(name="EPSF", format=f"{size * size}E", dim=f"({size},{size})", array=np.stack([k, wide])),
    ], name="EPSF")
    table.header["OVSMPX"] = ov
    image = fits.ImageHDU(np.zeros((4, 4)), name="IMAGE")
    image.header["DETECTOR"] = detector
    return fits.HDUList([fits.PrimaryHDU(), image, table])


def test_epsf_library_picks_the_nearest_zone_and_survives_save_and_load(tmp_path):
    lib = EpsfLibrary({})
    assert lib.add_hdul(_epsf_hdul(3))
    assert not lib.add_hdul(_epsf_hdul(3))  # one table per detector
    assert not lib.add_hdul(fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2)), name="IMAGE")]))
    near, far = lib.psf_at(3, 120.0, 90.0), lib.psf_at(3, 850.0, 80.0)
    assert far.kernel.max() < near.kernel.max()  # the second zone's PSF is the wide one
    lib.save(tmp_path / "lib.npz")
    again = EpsfLibrary.load(tmp_path / "lib.npz")
    assert again.detectors == [3]
    assert np.allclose(again.psf_at(3, 850.0, 80.0).kernel, far.kernel, atol=1e-6)
    assert again.psf_at(3, 1.0, 1.0).oversample == 5


def test_injection_uses_the_given_epsf_not_the_gaussian():
    f = blank_frame(60900.0)
    lib = EpsfLibrary({})
    lib.add_hdul(_epsf_hdul(1, sigma_px=1.5))  # much broader than the Gaussian default
    (broad,) = inject([f], [at(0, 0)], lambda fr, x, y: lib.psf_at(1, 100.0, 100.0))
    (default,) = inject([f], [at(0, 0)])
    assert broad.data.max() < 0.8 * default.data.max()
    assert broad.data.sum() == pytest.approx(default.data.sum(), rel=1e-9)  # same flux


# --- choosing sites ----------------------------------------------------------------------


def test_sites_avoid_real_sources_edges_and_bad_pixels():
    rng = np.random.default_rng(3)
    frames = [blank_frame(m, rng, NOISE) for m in PASS_MJDS for _ in range(2)]
    star = at(-60.0, 0.0, mag=13.0)  # a bright real star in the usable half (east is -x)
    star_pos = CENTER.spherical_offsets_by(-60.0 * u.arcsec, 0 * u.arcsec)
    frames = inject(frames, [star])
    for f in frames:
        f.data[:, : N // 2] = np.nan  # the whole western half is unusable
    finder = SiteFinder(frames)
    sites = [s for _ in range(30) if (s := finder.sample(rng, 2.0, 1.0, 16.0)) is not None]
    assert sites
    for s in sites:
        ra, dec = s.position([f.mjd for f in frames])
        for f, r, d in zip(frames, ra, dec):
            x, y = f.wcs.all_world2pix(r, d, 0)
            assert 5 <= x <= N - 6 and 5 <= y <= N - 6
            assert round(float(x)) - 1 >= N // 2  # clear stamp: no masked pixel
        # ...and the whole track stays out of the real star's 20" exclusion zone
        assert SkyCoord(ra * u.deg, dec * u.deg).separation(star_pos).arcsec.min() > 20.0 - 1e-6


def test_a_site_on_or_next_to_a_real_star_is_rejected_one_clear_of_it_is_not():
    rng = np.random.default_rng(4)
    frames = inject([blank_frame(m, rng, NOISE) for m in PASS_MJDS for _ in range(2)], [at(0, 0, mag=13.0)])
    finder = SiteFinder(frames)
    assert not finder.clear(at(0, 0))
    assert not finder.clear(at(12, 0))  # 2 px away: inside the 20" exclusion
    assert finder.clear(at(0, 60))
    # a track that starts clear but runs into the star during the baseline is rejected too
    assert not finder.clear(at(60, 0, 61080.0, pm=(-100.0, 0.0)))


def test_sample_returns_none_when_nothing_is_free_and_keeps_its_distance_from_others():
    rng = np.random.default_rng(5)
    frames = [blank_frame(m, rng, NOISE) for m in PASS_MJDS]
    for f in frames:
        f.data[:] = np.nan
    assert SiteFinder(frames).sample(rng, 0.0, 0.0, 16.0, tries=20) is None
    frames = [blank_frame(m, rng, NOISE) for m in PASS_MJDS]
    finder = SiteFinder(frames)
    taken = []
    for _ in range(4):
        s = finder.sample(rng, 1.0, 1.0, 16.0, taken=taken)
        assert s is not None
        taken.append(s)
    for i, a in enumerate(taken):
        for b in taken[i + 1 :]:
            sep = SkyCoord(a.ra * u.deg, a.dec * u.deg).separation(SkyCoord(b.ra * u.deg, b.dec * u.deg)).arcsec
            assert sep > 45.0  # 50" minimum, minus the drift between epochs


# --- matching ----------------------------------------------------------------------------


class FakeObject:
    def __init__(self, inj: Injection, mjd: float, pm_e=0.0, pm_n=0.0, err=0.1, mover=True, dra=0.0):
        ra, dec = inj.position([mjd])
        self.ra, self.dec, self.ref_mjd = ra[0] + dra / 3600, dec[0], mjd
        self.pm_ra_cosdec, self.pm_dec, self.pm_err, self.is_mover = pm_e, pm_n, err, mover


def test_matching_is_one_to_one_by_position_at_the_objects_date():
    a, b = at(0, 0, pm=(5.0, 0.0)), at(60, 0)
    objs = [FakeObject(b, 61000.0), FakeObject(a, 61100.0, 5.0, 0.0)]
    ma, mb = match_injections(objs, [a, b])
    assert ma.obj is objs[1] and mb.obj is objs[0]
    assert ma.flagged_mover and ma.pull(a) == pytest.approx((0.0, 0.0))
    # Out of the 12" radius: missed. One object cannot serve two injections.
    assert not match_injections([FakeObject(a, 61100.0, dra=30.0)], [a])[0].recovered
    c = at(2, 0)
    got = match_injections([FakeObject(a, 61100.0)], [a, c])
    assert sum(m.recovered for m in got) == 1


def test_pull_is_measured_minus_injected_over_error():
    a = at(0, 0, pm=(4.0, 2.0))
    m = match_injections([FakeObject(a, 61000.0, 4.3, 1.8, err=0.1)], [a])[0]
    assert m.pull(a) == pytest.approx((3.0, -2.0))
    assert Match(None).pull(a) is None


# --- end to end with the engine --------------------------------------------------------


def survey_frames(rng):
    """Three passes of 8 frames of a static field (30 stars) with noise and small pointing errors."""
    frames = []
    for m0 in PASS_MJDS:
        for k in range(8):
            shift = tuple(rng.normal(0, 0.5, 2))
            frames.append(blank_frame(m0 + 3 * k, rng, NOISE, wcs=make_wcs(shift, rng.uniform(0, 360))))
    return frames


def planted_field(seed, extra):
    rng = np.random.default_rng(seed)
    frames = survey_frames(rng)
    stars = []
    while len(stars) < 30:
        e, n = rng.uniform(-150, 150, 2)
        if all(math.hypot(e - s[0], n - s[1]) > 40 for s in stars + [(a.east, a.north) for a in extra]):
            stars.append((e, n, rng.uniform(14.5, 15.5)))
    star_inj = [at(e, n, mag=m) for e, n, m in stars]
    return inject(frames, star_inj)


class Spot:
    def __init__(self, east, north, pm, mag):
        self.east, self.north, self.pm, self.mag = east, north, pm, mag

    def injection(self):
        return at(self.east, self.north, 60900.0 + 182.0, pm=self.pm, mag=self.mag)


@pytest.mark.parametrize("stack", [False, True])
def test_bright_fast_injection_is_recovered_as_a_mover_with_the_right_motion(stack):
    spot = Spot(60.0, -70.0, (0.0, 0.0), 14.0)
    spot.pm = (8.0, -4.0)
    inj = spot.injection()
    planted = inject(planted_field(11, [spot]), [inj])
    result = find_motion(planted, stack=stack)
    (m,) = match_injections(result.objects, [inj])
    assert m.recovered and m.flagged_mover
    pe, pn = m.pull(inj)
    assert abs(pe) < 4 and abs(pn) < 4  # the engine's own error bar covers the injected motion
    assert m.obj.pm_total == pytest.approx(math.hypot(8.0, 4.0), abs=1.5)


def test_without_the_injection_there_is_nothing_to_recover():
    """Negative control: the same field with no planted mover returns no object there."""
    spot = Spot(60.0, -70.0, (8.0, -4.0), 14.0)
    result = find_motion(planted_field(11, [spot]))
    (m,) = match_injections(result.objects, [spot.injection()])
    assert not m.recovered


def test_bright_static_injection_is_recovered_but_not_flagged_as_a_mover():
    spot = Spot(-60.0, 60.0, (0.0, 0.0), 14.0)
    inj = spot.injection()
    result = find_motion(inject(planted_field(12, [spot]), [inj]))
    (m,) = match_injections(result.objects, [inj])
    assert m.recovered and not m.flagged_mover


def test_a_source_far_below_the_noise_is_not_recovered():
    spot = Spot(60.0, -70.0, (8.0, -4.0), 24.0)  # ~1 microJy
    inj = spot.injection()
    result = find_motion(inject(planted_field(13, [spot]), [inj]))
    (m,) = match_injections(result.objects, [inj])
    assert not m.recovered


# --- the tool's summaries ----------------------------------------------------------------


def _tool():
    path = Path(__file__).resolve().parents[2] / "tools" / "injection_recovery.py"
    spec = importlib.util.spec_from_file_location("injection_recovery_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wilson_interval_is_sane_at_the_extremes():
    t = _tool()
    lo, hi = t.wilson(0, 40)
    assert lo == 0.0 and 0.05 < hi < 0.12  # "0 of 40" still allows a few percent
    lo, hi = t.wilson(40, 40)
    assert hi == 1.0 and 0.88 < lo < 0.95
    assert t.wilson(5, 10)[0] < 0.5 < t.wilson(5, 10)[1]
    assert math.isnan(t.wilson(0, 0)[0])


def test_crossing_interpolates_and_reports_never():
    t = _tool()
    assert t.crossing([1, 2, 4], [0.1, 0.4, 0.9], 0.5) == pytest.approx(2 + 2 * (0.1 / 0.5))
    assert t.crossing([1, 2, 4], [0.1, 0.4, 0.45], 0.5) is None
    assert t.crossing([1, 2], [0.7, 0.9], 0.5) == 1.0  # already above at the slowest speed


def _rows(spec):
    """spec: {(mode, pm, mag): (n, n_flagged)} -> raw outcome rows."""
    rows = []
    for (mode, pm, mag), (n, k) in spec.items():
        for i in range(n):
            rows.append({"field": "f", "mode": mode, "pm": pm, "mag_centre": mag, "placed": True, "recovered": i < k, "flagged_mover": i < k})
    return rows


def test_summary_finds_pm_50_pm_90_and_mag_50_and_static_false_rate():
    t = _tool()
    spec = {}
    for mode in t.MODES:
        for pm, frac in [(0.0, 0.0), (1.0, 0.2), (2.0, 0.6), (4.0, 1.0)]:
            spec[(mode, pm, 14.0)] = (10, round(10 * frac))
        for mag, frac in [(14.0, 1.0), (16.0, 0.8), (18.0, 0.2)]:
            spec[(mode, 8.0, mag)] = (10, round(10 * frac))
    spec[("single", 0.0, 14.0)] = (10, 1)  # one false mover out of ten static injections
    rows = _rows(spec)
    grid = [g for g in t.grid_rows(rows) if g["field"] == "all"]
    s = t.summarize(grid, ref_mag=15.0, ref_pm=(8.0, 8.0))["single"]
    assert s["pm_50"] == pytest.approx(1.0 + 0.3 / 0.4)  # between 1" (0.2) and 2" (0.6)
    assert s["pm_90"] == pytest.approx(2.0 + 2.0 * 0.3 / 0.4)  # between 2" (0.6) and 4" (1.0)
    assert 16.0 < s["mag_50"] < 18.0
    fp = t.false_positive_summary(rows)
    assert fp["single"]["n_static_injected"] == 10 and fp["single"]["n_flagged_mover"] == 1
    assert fp["stack"]["n_flagged_mover"] == 0


def test_verdicts_call_a_recovered_static_injection_not_moving_and_a_fast_one_moving():
    static, fast = Spot(-60.0, 60.0, (0.0, 0.0), 14.0), Spot(60.0, -70.0, (8.0, -4.0), 14.0)
    injections = [static.injection(), fast.injection()]
    result = find_motion(inject(planted_field(14, [static, fast]), injections))
    ms = match_injections(result.objects, injections)
    verdicts = _tool().object_verdicts(result.objects)
    v_static, v_fast = (verdicts[result.objects.index(m.obj)] for m in ms)
    assert v_static.verdict == "not_moving"
    assert v_fast.verdict == "moving" and v_fast.certainty == "candidate"
