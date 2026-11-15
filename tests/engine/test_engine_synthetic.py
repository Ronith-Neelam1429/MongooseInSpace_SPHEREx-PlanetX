"""Engine behaviour on synthetic SPHEREx-like frames with known truth.

Fast and offline: every scenario plants stars with exactly known positions,
motions and fluxes, so the engine's answers can be checked precisely.
"""
from __future__ import annotations

import dataclasses
import io

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from scipy.special import erf
from synthetic import SHAPE, SIP_A00, make_mef, wavelength_at_raw_pixel

from planetx.engine import (
    Frame,
    PassSource,
    detect_sources,
    find_motion,
    fit_motion,
    frame_from_hdul,
    group_into_passes,
)
from planetx.engine.motion import SkyPlane

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


def continuous_survey(stars, rng, days=400.0, step=5.0):
    """Coverage as at the ecliptic poles: a frame every few days, no gaps."""
    return [
        make_frame(
            stars, PASS_MJDS[0] + t, WAVES[k % len(WAVES)], rng,
            rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)),
        )
        for k, t in enumerate(np.arange(0.0, days + step / 2, step))
    ]


def clear_spot(stars, drift=(0.0, 0.0)):
    """(east, north) with no star within 40" of the middle of a track that
    drifts by `drift` arcsec from there (blending isn't what these tests are about)."""
    grid = [(float(e), float(n)) for e in range(-100, 101, 10) for n in range(-100, 81, 10)]

    def room(p):
        return min(np.hypot(s[0] - p[0] - drift[0] / 2, s[1] - p[1] - drift[1] / 2) for s in stars)

    spot = max(grid, key=room)
    assert room(spot) > 40
    return spot


def test_group_into_passes_splits_on_gaps_only():
    mjds = [10, 11, 50, 200, 230, 400]
    assert group_into_passes(mjds, gap_days=60) == [[0, 1, 2], [3, 4], [5]]
    assert group_into_passes([], gap_days=60) == []
    # Input order doesn't matter: passes come out time-ordered.
    assert group_into_passes([400, 10, 200], gap_days=60) == [[1], [2], [0]]


def test_ordinary_passes_of_up_to_eight_weeks_stay_whole():
    # First and last visit of the three real passes at Barnard's Star (37-55 days long).
    mjds = [60902.9, 60923.1, 60949.3, 61110.1, 61140.0, 61165.1, 61268.6, 61285.3, 61305.6]
    assert group_into_passes(mjds, 60.0) == [[0, 1, 2], [3, 4, 5], [6, 7, 8]]
    # A run of exactly the longest allowed pass is not cut.
    assert group_into_passes([0.0, 30.0, 60.0], 60.0) == [[0, 1, 2]]


def test_continuous_coverage_is_cut_into_time_windows():
    mjds = [60000.0 + 2.5 * k for k in range(201)]  # 500 days without a gap
    passes = group_into_passes(mjds, 60.0)
    assert sorted(i for p in passes for i in p) == list(range(len(mjds)))  # every frame, once
    assert len(passes) == 9  # the fewest windows of at most 60 days
    spans = [mjds[p[-1]] - mjds[p[0]] for p in passes]
    assert max(spans) <= 60 and min(spans) > 45  # equal windows, no sliver at the end
    assert all(mjds[a[-1]] < mjds[b[0]] for a, b in zip(passes, passes[1:]))  # time-ordered
    # The window length follows `max_days`.
    assert len(group_into_passes(mjds, 60.0, max_days=100.0)) == 5
    with pytest.raises(ValueError):
        group_into_passes(mjds, 60.0, max_days=0.0)


def test_a_sample_of_each_window_is_grouped_like_the_full_list():
    # The website downloads a few frames per pass and hands only those to the
    # engine, which must then see the same passes.
    rng = np.random.default_rng(12)
    mjds = sorted(60000.0 + rng.uniform(0, 430, 300))
    passes = group_into_passes(mjds, 60.0)
    sample = [p[i] for p in passes for i in sorted({0, len(p) // 3, 2 * len(p) // 3, len(p) - 1})]
    regrouped = group_into_passes([mjds[i] for i in sample], 60.0)
    assert [[sample[k] for k in p] for p in regrouped] == [[i for i in p if i in sample] for p in passes]


def test_window_without_frames_is_not_a_pass():
    # 135 days with no gap over 60: three 45-day windows, the middle one empty.
    assert group_into_passes([0.0, 5.0, 40.0, 95.0, 130.0, 135.0], 60.0) == [[0, 1, 2], [3, 4, 5]]


def test_sky_plane_agrees_with_astropy_offsets():
    # The engine's own offset maths replaced astropy's (for speed); it must
    # give the same answers, also across RA 0/360 and close to a pole.
    rng = np.random.default_rng(13)
    for centre in [(150.0, 20.0), (359.9999, -30.0), (0.0001, 45.0), (270.0, 89.5)]:
        sky, plane = SkyCoord(*centre, unit="deg"), SkyPlane(*centre)
        east, north = rng.uniform(-300, 300, (2, 50))
        there = sky.spherical_offsets_by(east * u.arcsec, north * u.arcsec)
        ra, dec = plane.sky(east, north)
        assert ((ra >= 0) & (ra < 360)).all()
        assert SkyCoord(ra, dec, unit="deg").separation(there).arcsec.max() < 1e-6
        back_east, back_north = plane.offsets(there.ra.deg, there.dec.deg)
        np.testing.assert_allclose(back_east, east, atol=1e-6)
        np.testing.assert_allclose(back_north, north, atol=1e-6)


def test_fit_motion_takes_a_skycoord_centre():
    # 10"/yr due north, one position a year: callers outside the engine pass a SkyCoord.
    sources = []
    for k in range(3):
        c = CENTER.spherical_offsets_by(5 * u.arcsec, (-20 + 10 * k) * u.arcsec)
        sources.append(PassSource(
            pass_index=k, ra=c.ra.deg, dec=c.dec.deg, sigma_arcsec=0.3, mjd=61000 + 365.25 * k,
            n_det=5, n_cover=5, snr=50.0, flux_by_detector={},
        ))
    ra, dec, ref_mjd, pm_east, pm_north, pm_err, chi2_dof = fit_motion(sources, CENTER)
    assert pm_east == pytest.approx(0.0, abs=1e-6) and pm_north == pytest.approx(10.0, abs=1e-6)
    assert ref_mjd == pytest.approx(61000 + 365.25) and chi2_dof == pytest.approx(0.0, abs=1e-9)
    middle = SkyCoord(sources[1].ra, sources[1].dec, unit="deg")
    assert SkyCoord(ra, dec, unit="deg").separation(middle).arcsec < 1e-6


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


def test_continuous_coverage_recovers_mover_and_variable():
    # Near the ecliptic poles SPHEREx never leaves a field for long, so there
    # are no gaps to split passes at. Motion and brightness changes must still
    # be measured there, and static stars must stay static.
    rng = np.random.default_rng(21)
    stars = field_stars(rng)
    e0, n0 = clear_spot(stars, drift=(-1.1, 11.0))
    ve, vn = clear_spot(stars + [(e0, n0 + 5.5)])

    def brightening(mjd):
        return 600.0 if mjd < PASS_MJDS[0] + 200 else 1200.0

    special = [(e0, n0, -1.0, 10.0, 1500.0), (ve, vn, 0.0, 0.0, brightening)]
    frames = continuous_survey(stars + special, rng)  # 400 days, a frame every 5
    r = find_motion(frames)
    assert len(r.passes) == 7
    assert all(frames[p[-1]].mjd - frames[p[0]].mjd <= 60 for p in r.passes)
    assert not any("only one pass" in n for n in r.notes)
    (m,) = r.movers
    assert m.pm_ra_cosdec == pytest.approx(-1.0, abs=0.6)
    assert m.pm_dec == pytest.approx(10.0, abs=0.6)
    assert len(m.sources) == 7  # followed through every window
    (v,) = r.variables
    assert v is nearest(r, ve, vn)
    assert v.variability["ratio"] == pytest.approx(2.0, rel=0.15)
    assert len(r.objects) >= 35
    # Treated as one long pass (the old behaviour) the same frames say nothing.
    blind = find_motion(frames, pass_max_days=float("inf"))
    assert blind.objects == [] and any("only one pass" in n for n in blind.notes)


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


def test_frame_wavelength_ignores_celestial_sip_terms():
    # The WCS-WAVE table is indexed by raw pixels; the synthetic header's SIP
    # A_0_0 = 3 px would shift the lookup by 0.003 um if it were applied.
    hdul = fits.open(io.BytesIO(make_mef(150.0, 20.0)))
    frame = frame_from_hdul(hdul)
    centre_x = (SHAPE[1] - 1) / 2
    assert frame.wavelength_um == pytest.approx(wavelength_at_raw_pixel(centre_x), abs=1e-5)
    assert frame.wavelength_um != pytest.approx(wavelength_at_raw_pixel(centre_x + SIP_A00), abs=1e-3)


def _outcome(r):
    """Everything a caller reads from a result, frame order aside."""
    objects = [
        (o.link, o.ra, o.dec, o.pm_ra_cosdec, o.pm_dec, o.pm_err, o.is_mover, o.is_variable,
         o.missing_passes, [(s.pass_index, s.ra, s.dec, s.mjd) for s in o.sources])
        for o in r.objects
    ]
    passes = [[r.frames[i].mjd for i in ids] for ids in r.passes]
    unlinked = [(s.pass_index, s.ra, s.dec) for s in r.unlinked]
    return objects, passes, unlinked, r.pm_floor_arcsec_yr, r.notes


@pytest.mark.parametrize("stack", [False, True])
def test_result_does_not_depend_on_frame_order(stack):
    rng = np.random.default_rng(14)

    def brightening(mjd):
        return 600.0 if mjd < PASS_MJDS[1] - 30 else 1200.0

    stars = field_stars(rng) + [(0.0, -10.0, -1.0, 10.0, 1500.0), (-40.0, 25.0, 0.0, 0.0, brightening)]
    frames = survey(stars, rng)
    # Same time and detector, no obs_id: only their content tells them apart.
    frames.append(make_frame(stars, frames[0].mjd, WAVES[1], rng, rot_deg=77.0))
    expected = _outcome(find_motion(frames, stack=stack))
    assert expected[0], "the scenario must produce tracked objects"
    for seed in range(3):
        shuffled = list(frames)
        np.random.default_rng(seed).shuffle(shuffled)
        assert _outcome(find_motion(shuffled, stack=stack)) == expected


def test_variable_seen_across_visits_cut_into_time_windows():
    # Visits of ~80 days (as at R Dra, Dec +67) are cut into 40-day windows,
    # each holding only half of a visit's wavelengths. Brightness must still
    # be compared visit against visit, with all their same-wavelength pairs.
    rng = np.random.default_rng(15)
    starts = [60900.0, 61080.0, 61265.0]

    def brightening(mjd):
        return 600.0 if mjd < starts[2] - 30 else 1200.0

    stars = field_stars(rng)
    spot = clear_spot(stars)
    stars.append((*spot, 0.0, 0.0, brightening))
    frames = [
        make_frame(
            stars, m0 + 11 * k, WAVES[k], rng,
            rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)),
        )
        for m0 in starts
        for k in range(8)
    ]
    r = find_motion(frames)
    assert len(r.passes) == 6  # each visit is two windows
    v = nearest(r, *spot)
    assert v.is_variable and not v.is_mover
    assert v.variability["ratio"] == pytest.approx(2.0, rel=0.15)
    assert v.variability["n_pairs"] >= 8
    assert [o for o in r.variables] == [v]


def test_mover_hidden_by_a_field_star_in_one_pass_is_still_a_mover():
    # Half way through, the mover passes over a field star: in that pass the two
    # make one source (the star's), so the track has nothing there. That is a
    # blend, not evidence against the track.
    # Passes are ~6 months apart and two sources merge within ~1.5 PSF FWHM
    # (~13"), so a mover blends in one pass only if it moves > ~25"/yr: slower,
    # it would blend with the star in every pass.
    rng = np.random.default_rng(16)
    stars = field_stars(rng)
    pm_n = 28.0
    mid_yr = (PASS_MJDS[1] - PASS_MJDS[0]) / 365.25
    e0, n0 = clear_spot(stars, drift=(0.0, pm_n))
    blocker = (e0, n0 + pm_n * mid_yr, 0.0, 0.0, 1500.0)
    frames = survey(stars + [(e0, n0, 0.0, pm_n, 1500.0), blocker], rng)
    r = find_motion(frames, max_pm_arcsec_yr=30.0)
    (m,) = r.movers
    assert [s.pass_index for s in m.sources] == [0, 2]
    assert m.blended_passes == [1] and m.missing_passes == []
    # The star's wings pull the mover's centroids towards it in the other passes.
    assert m.pm_dec == pytest.approx(pm_n, abs=4.0)
    # The star itself (the mover's mean position is the same spot) is static.
    star = nearest(dataclasses.replace(r, objects=[o for o in r.objects if o is not m]), *blocker[:2])
    assert len(star.sources) == 3 and not star.is_mover


def test_variability_pairs_use_each_detection_once():
    # One pass saw the star at 5 wavelengths, the other 5 times at each of them.
    # Pairing every detection with every same-wavelength partner would reuse the
    # first pass's 5 measurements 5 times each and overstate the significance:
    # a constant star would then often look variable.
    from planetx.engine.detect import Detection
    from planetx.engine.motion import _variability

    def source(pass_index, waves, fluxes):
        dets = [
            Detection(0, 0.0, 0.0, 150.0, 20.0, 0.1, f, 5.0, f / 5.0, 60900.0 + 180 * pass_index, 1, wavelength_um=w)
            for w, f in zip(waves, fluxes)
        ]
        return PassSource(pass_index, 150.0, 20.0, 0.1, dets[0].mjd, len(dets), len(dets), 20.0, {}, dets)

    rng = np.random.default_rng(17)
    waves = [1.0, 1.1, 1.2, 1.3, 1.4]
    signif = []
    for _ in range(300):
        a = source(0, waves, rng.normal(100.0, 5.0, 5))
        b = source(1, waves * 5, rng.normal(100.0, 5.0, 25))
        v = _variability([a, b], calib_frac=0.0)
        assert v["n_pairs"] == 5
        signif.append(v["significance"])
    # Gaussian noise: |z| > 3 in ~0.3% of cases; allow for the median's heavier tails.
    assert np.mean(np.array(signif) > 3) < 0.03


def test_stacks_per_detector_keep_a_faint_red_object():
    # A faint Y-dwarf-like mover, visible only to detector 3 (~2.3 sigma per
    # frame). Co-adding all three detectors' frames would add twice as many
    # frames of pure noise and bury it; per-detector stacks find it.
    rng = np.random.default_rng(18)
    stars = field_stars(rng, flux=(400, 2000))
    e0, n0 = clear_spot(stars, drift=(-8.0, 0.7))
    red = (e0, n0, -8.0, 0.7, 13.0)
    waves = {1: 1.0, 2: 1.6, 3: 4.5}
    frames = []
    for m0 in PASS_MJDS:
        for k in range(36):
            det = 1 + k % 3
            frames.append(
                make_frame(
                    stars + ([red] if det == 3 else []), m0 + 0.5 * k, waves[det] + 0.01 * (k // 3), rng,
                    rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)), detector=det,
                )
            )
    r = find_motion(frames, stack=True)
    assert [len(p) for p in r.passes] == [3, 3, 3]
    assert [[r.frames[i].detector for i in p] for p in r.passes] == [[1, 2, 3]] * 3
    # Each stack carries its frames' mean wavelength.
    assert [r.frames[i].wavelength_um for i in r.passes[0]] == pytest.approx([1.055, 1.655, 4.555])
    (m,) = r.movers
    assert m is nearest(r, e0 - 4.0, n0)
    assert m.pm_ra_cosdec == pytest.approx(-8.0, abs=3.0)
    assert all({d.detector for d in s.detections} == {3} for s in m.sources)


def test_position_errors_are_calibrated_on_the_fields_static_stars():
    # 200 static stars whose pass positions scatter by 0.5" while their errors
    # say 0.25": the field's own stars say errors are twice too small. One
    # star sits far off its line (a blend) and must not drag the factor.
    from planetx.engine.motion import _error_scale

    rng = np.random.default_rng(19)
    plane = SkyPlane(150.0, 20.0)
    groups = []
    for k in range(200):
        e0, n0 = rng.uniform(-150, 150, 2)
        g = []
        for p, mjd in enumerate(PASS_MJDS):
            de, dn = rng.normal(0, 0.5, 2) + (30.0 if k == 0 and p == 1 else 0.0)
            ra, dec = plane.sky(e0 + de, n0 + dn)
            g.append(PassSource(p, float(ra), float(dec), 0.25, mjd, 8, 8, 50.0, {}, []))
        groups.append(g)
    scale, used = _error_scale(groups, plane)
    assert used == 200
    assert scale == pytest.approx(2.0, rel=0.1)
    # Too few static stars: keep the global error model.
    assert _error_scale(groups[1:4], plane) == (1.0, 0)



def test_errors_stated_far_too_small_are_scaled_up_not_ignored():
    # Errors 15x too small make every track an outlier at the global model's
    # word; the robust start must still find the factor.
    from planetx.engine.motion import _error_scale

    rng = np.random.default_rng(20)
    plane = SkyPlane(150.0, 20.0)
    groups = []
    for _ in range(50):
        e0, n0 = rng.uniform(-150, 150, 2)
        g = []
        for p, mjd in enumerate(PASS_MJDS):
            ra, dec = plane.sky(e0 + rng.normal(0, 1.5), n0 + rng.normal(0, 1.5))
            g.append(PassSource(p, float(ra), float(dec), 0.1, mjd, 8, 8, 50.0, {}, []))
        groups.append(g)
    scale, used = _error_scale(groups, plane)
    assert used == 50
    assert scale == pytest.approx(15.0, rel=0.25)


def test_star_next_to_two_unrelated_sources_does_not_make_a_mover():
    # One source only in pass 0, another only in pass 2, 14" apart, and a field
    # star of ordinary, constant brightness near the midpoint in every pass.
    # Lined up, they look like a 2-pass mover hidden by the star in pass 1, but
    # the star isn't brighter in pass 1, so there is no blend.
    for seed in range(3):
        rng = np.random.default_rng(seed)
        stars = field_stars(rng)
        mx, my = clear_spot(stars)
        star = (mx, my + 10.0, 0.0, 0.0, 2000.0)
        t0, t2 = (mx - 7.0, my, 0.0, 0.0, 1500.0), (mx + 7.0, my, 0.0, 0.0, 1500.0)
        frames = []
        for p, m0 in enumerate(PASS_MJDS):
            extra = [star] + ([t0] if p == 0 else []) + ([t2] if p == 2 else [])
            for k in range(8):
                frames.append(make_frame(
                    stars + extra, m0 + 3 * k, WAVES[k % len(WAVES)], rng,
                    rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)),
                ))
        r = find_motion(frames)
        assert r.movers == [], [(m.pm_total, m.pm_significance, m.blended_passes) for m in r.movers]


def test_marginal_detections_cannot_make_a_two_pass_mover():
    from planetx.engine.motion import _weak

    def src(n_det, n_cover, snr):
        return PassSource(0, 150.0, 20.0, 0.3, 60900.0, n_det, n_cover, snr, {}, [])

    # A star at the detection limit: caught in 5 of 17 frames at SNR 5-9.
    assert _weak(src(5, 17, 9.0))
    assert not _weak(src(5, 17, 30.0))  # bright, just partly covered by detectors
    assert not _weak(src(12, 17, 9.0))  # faint but solidly detected


def _flipping_blend_frames(rng, layout):
    """Frames of a static field plus one object whose light sits at spot A or at
    spot B, 3.5" further north, depending on pass and detector (`layout[pass][det - 1]`).

    That is how a blend of two stars looks to SPHEREx: which of the pair the
    peak falls on depends on the frame's wavelength and PSF orientation.
    """
    stars = field_stars(rng, flux=(400, 2000))
    e0, n0 = clear_spot(stars)
    spots = {"A": (e0, n0, 0.0, 0.0, 1500.0), "B": (e0, n0 + 3.5, 0.0, 0.0, 1500.0)}
    frames = []
    for p, m0 in enumerate(PASS_MJDS):
        for k in range(6 * len(layout[p])):
            det = 1 + k % len(layout[p])
            frames.append(make_frame(
                stars + [spots[layout[p][det - 1]]], m0 + 0.5 * k, 0.6 + 0.7 * det, rng,
                rot_deg=rng.uniform(0, 360), wcs_error=tuple(rng.normal(0, 0.7, 2)), detector=det,
            ))
    return frames, (e0, n0)


def test_pass_whose_detections_disagree_gets_an_error_that_covers_them(monkeypatch):
    # Per-detector stacks of a blend, as seen for a static G 15.9 Gaia star with
    # a G 17.5 neighbour 3.1" away near Barnard's Star (stacks: 1.53"/yr at 9.5
    # sigma): pass 1 finds spot A in all five detectors, pass 2 finds B in one,
    # pass 3 in two. The pass means walk north without either star moving, and
    # each pass's error was that of an isolated star. The passes' own
    # detections disagree far beyond their errors, so they are not that precise.
    from planetx.engine import motion

    layout = ["AAAAA", "AAAAB", "AAABB"]
    frames, (e0, n0) = _flipping_blend_frames(np.random.default_rng(23), layout)
    o = nearest(find_motion(frames, stack=True), e0, n0 + 1.0)
    assert len(o.sources) == 3
    assert o.pm_significance < motion_sigma(), (o.pm_total, o.pm_err)
    # The disagreeing passes carry it; the consistent one keeps its small error.
    sig = [s.sigma_arcsec for s in o.sources]
    assert sig[0] < 0.15 < min(sig[1:])
    # Without the factor, the same frames gave a significant motion.
    monkeypatch.setattr(motion, "_disagreement", lambda *args: 1.0)
    frames, (e0, n0) = _flipping_blend_frames(np.random.default_rng(23), layout)
    old = nearest(find_motion(frames, stack=True), e0, n0 + 1.0)
    assert old.pm_significance >= motion_sigma()


def _seven_window_frames(rng, stars, start, shown, flux):
    """Seven passes 60 days apart, 4 frames each; a faint -8"/yr object in passes `shown`."""
    frames = []
    for p in range(7):
        for k in range(4):
            mjd = PASS_MJDS[0] + 60 * p + 3 * k
            dt = (mjd - PASS_MJDS[0]) / 365.25
            extra = [(start[0] - 8.0 * dt, start[1], 0.0, 0.0, flux)] if p in shown else []
            frames.append(make_frame(stars + extra, mjd, WAVES[k], rng, rot_deg=rng.uniform(0, 360),
                                     wcs_error=tuple(rng.normal(0, 0.7, 2))))
    return frames


def test_faint_track_seen_in_few_of_the_passes_covering_it_is_not_a_mover():
    # Near the ecliptic poles a field has many passes. Faint sources (SNR < 10,
    # where a pass without them is no evidence on its own) lined up in 3 of 7
    # passes and made stack "movers" with no Gaia star under any of their
    # positions. A real source at that brightness is seen in most passes.
    rng = np.random.default_rng(7)
    stars = field_stars(rng, flux=(400, 2000))
    e0, n0 = clear_spot(stars, drift=(-8.0, 0.0))
    r = find_motion(_seven_window_frames(rng, stars, (e0 + 4, n0), {0, 3, 6}, 24.5))
    o = nearest(r, e0, n0)
    span = (o.sources[-1].mjd - o.sources[0].mjd) / 365.25
    # Everything else says "mover": significant, > 1 px, faint enough that no pass counts as missing.
    assert len(o.sources) == 3 and o.pm_significance >= motion_sigma() and o.pm_total * span >= PIX
    assert o.missing_passes == [] and o.unseen_passes == [1, 2, 4, 5]
    assert not o.is_mover
    # The same object in every pass is a mover, though at this brightness it
    # may drop out of a pass or so.
    rng = np.random.default_rng(7)
    stars = field_stars(rng, flux=(400, 2000))
    r = find_motion(_seven_window_frames(rng, stars, (e0 + 4, n0), set(range(7)), 24.5))
    o = nearest(r, e0, n0)
    assert o.is_mover and len(o.sources) >= 5 and len(o.unseen_passes) <= 1


def motion_sigma() -> float:
    """find_motion's default mover significance."""
    import inspect

    return inspect.signature(find_motion).parameters["mover_sigma"].default


def test_disagreement_factor_leaves_ordinary_scatter_alone():
    from planetx.engine.motion import _disagreement

    rng = np.random.default_rng(22)
    w = np.full(6, 1 / 0.4**2)
    ordinary = [_disagreement(*rng.normal(0, 0.4, (2, 6)), w, np.zeros(2)) for _ in range(200)]
    assert sum(f > 1 for f in ordinary) <= 2  # the gate opens at p < 1e-3
    assert _disagreement(np.array([0.0]), np.array([0.0]), w[:1], np.zeros(2)) == 1.0
    x = np.array([0.0, 0.0, 0.0, 3.5, 3.5, 3.5])
    f = _disagreement(x, np.zeros(6), w, np.array([1.75, 0.0]))
    assert f == pytest.approx(np.sqrt((6 * 1.75**2 / 0.4**2) / 10))


def _pass_source(pass_index, plane, east, north, mjd, fluxes):
    ra, dec = plane.sky(east, north)
    return PassSource(pass_index, float(ra), float(dec), 0.12, mjd, 6, 6, 190.0, fluxes, [])


def test_flux_at_the_noise_level_does_not_veto_a_link():
    from planetx.engine.motion import _flux_compatible

    plane = SkyPlane(331.1, -56.8)

    def src(fluxes):
        return _pass_source(0, plane, 0.0, 0.0, 60800.0, fluxes)

    # Eps Indi B in detector 1 (website sample, 2026-11-14): 4.8 +- 6.8 and 17.4 +- 4.5.
    assert _flux_compatible(src({"D1": (4.8, 6.8)}), src({"D1": (17.4, 4.5)}), 3.0)
    # Non-positive, but consistent with the other pass's flux.
    assert _flux_compatible(src({"D1": (-2.0, 6.0)}), src({"D1": (9.0, 4.0)}), 3.0)
    # Well-measured fluxes 3.6x apart are still different objects.
    assert not _flux_compatible(src({"D2": (5.0, 0.5)}), src({"D2": (18.0, 0.6)}), 3.0)
    assert not _flux_compatible(src({"D2": (-1.0, 0.5)}), src({"D2": (18.0, 0.6)}), 3.0)


def test_mover_split_into_two_static_halves_is_joined_again():
    # Eps Indi B on the website's sample (detectors 1-3, 4 passes): passes 1-2
    # and 3-4 are each < 2" apart and linked as "static", while passes 1 and 4
    # are 7" apart. The halves must merge into one moving track although
    # detector 1, where the T dwarf is at the noise level, read 4.8 in one pass
    # and 17 in another (it came out as two 10 sigma static tracks under 1 px).
    from planetx.engine.motion import _merge_tracks

    plane = SkyPlane(331.1, -56.8)
    mjds = [60806.0, 60958.0, 61157.0, 61311.0]
    pm = (3.98, -2.47)
    fluxes = [
        {"D2": (31.5, 5.6), "D3": (32.5, 4.5)},
        {"D2": (19.6, 5.6), "D3": (20.2, 4.0), "D1": (4.8, 6.8)},
        {"D2": (25.4, 5.8), "D3": (19.7, 3.0), "D1": (17.4, 4.5)},
        {"D2": (14.7, 6.1), "D3": (21.5, 3.9)},
    ]
    track = [
        _pass_source(p, plane, pm[0] * (m - mjds[0]) / 365.25, pm[1] * (m - mjds[0]) / 365.25, m, f)
        for p, (m, f) in enumerate(zip(mjds, fluxes))
    ]
    static, movers = _merge_tracks([track[:2], track[2:]], [], plane, 15.0, 3.0, 1e-3)
    assert static == [] and len(movers) == 1 and len(movers[0]) == 4
