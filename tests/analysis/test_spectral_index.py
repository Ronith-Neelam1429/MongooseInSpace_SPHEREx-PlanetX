"""Offline tests for planetx.analysis.spectral_index: depths, errors, flags, neighbours, light curves."""
import io
import math

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
from planetx.analysis import spectral_index as si
from planetx.spherex import spectrum as sp
from planetx.spherex.cutouts import FLAG_BITS
from synthetic import SHAPE, make_mef


def _spectrum(flux_at, err_frac=0.02, floor=0.0):
    return [sp.BandFlux(b, flux_at(b.center_um), max(abs(flux_at(b.center_um)) * err_frac, floor, 1e-6), 3, 0)
            for b in sp.BANDS]


def _star(lam):
    return 100.0 * lam**-2.0  # Rayleigh-Jeans F_nu, mJy


def _absorbed(lam, molecules=("CH4", "H2O")):
    return any(fd.window_um[0] <= lam <= fd.window_um[1] for fd in sp.FEATURES if fd.molecule in molecules)


def _dwarf(lam, depth=0.5):
    return 100.0 * lam**-1.0 * (1 - depth if _absorbed(lam) else 1.0)


# --- depths, errors, flags -------------------------------------------------------------------


def test_a_star_is_star_like_with_no_absorption():
    ix = si.measure_index(_spectrum(_star))
    assert abs(ix.methane_depth) < 0.03 and abs(ix.water_depth) < 0.03
    assert 0 < ix.methane_err < 0.05 and 0 < ix.water_err < 0.05
    assert ix.color_1_4um == pytest.approx(-2.5 * math.log10((4.05 / 1.26) ** 2), abs=0.1)
    flag, reason = si.flag(ix)
    assert flag == "star_like" and "Methane" in reason


def test_a_t_dwarf_suggests_a_brown_dwarf_and_says_suggests():
    ix = si.measure_index(_spectrum(_dwarf))
    assert ix.methane_depth == pytest.approx(0.5, abs=0.03) and ix.water_depth == pytest.approx(0.5, abs=0.03)
    flag, reason = si.flag(ix)
    assert flag == "suggests_brown_dwarf"
    assert "suggests" in reason and " is a " not in reason


def test_methane_needs_water_to_suggest_a_brown_dwarf():
    ix = si.measure_index(_spectrum(lambda lam: _star(lam) * (0.5 if _absorbed(lam, ("CH4",)) else 1.0)))
    assert ix.methane_depth > 0.4 and abs(ix.water_depth) < 0.05
    assert si.flag(ix)[0] == "too_faint"  # not a suggestion, not star-like either


def test_noisy_spectrum_is_too_faint():
    ix = si.measure_index(_spectrum(_dwarf, err_frac=1.0))
    assert ix.methane_err is None or ix.methane_err > 0.15
    assert si.flag(ix)[0] == "too_faint"


def test_missing_bands_leave_methane_unmeasured():
    bands = [b for b in _spectrum(_star) if b.band.center_um < 1.5]
    ix = si.measure_index(bands)
    assert ix.methane_depth is None and ix.methane_err is None
    assert len(ix.methane_windows_missing) == 3
    flag, reason = si.flag(ix)
    assert flag == "too_faint" and "No exposure covers this position at the methane wavelengths" in reason
    # Methane bands measured but their continuum too faint: a different reason.
    faint = si.measure_index(_spectrum(lambda lam: 0.0 if lam > 1.5 else _star(lam), floor=1.0))
    assert faint.methane_depth is None and faint.methane_windows_missing == []
    assert si.flag(faint)[1].startswith("The bands needed to measure methane")


def test_monte_carlo_error_follows_the_band_errors_and_is_reproducible():
    small = si.molecule_depths(_spectrum(_dwarf, err_frac=0.02), seed=3)
    big = si.molecule_depths(_spectrum(_dwarf, err_frac=0.08), seed=3)
    assert big["CH4"][1] / small["CH4"][1] == pytest.approx(4.0, rel=0.25)
    assert si.molecule_depths(_spectrum(_dwarf, err_frac=0.02), seed=3) == small


def test_monte_carlo_error_matches_the_scatter_of_noisy_realisations():
    """The quoted error is what repeated noisy spectra of the same dwarf actually scatter by."""
    rng = np.random.default_rng(7)
    truth = _spectrum(_dwarf, err_frac=0.05)
    depths = []
    for _ in range(60):
        noisy = [sp.BandFlux(b.band, b.flux_mjy + b.flux_err_mjy * rng.standard_normal(), b.flux_err_mjy, 3, 0) for b in truth]
        depths.append(si.molecule_depths(noisy, n_draws=50)["CH4"][0])
    quoted = si.molecule_depths(truth)["CH4"][1]
    assert np.std(depths) == pytest.approx(quoted, rel=0.3)


def test_error_scale_widens_errors_before_flagging():
    ix = si.measure_index(_spectrum(lambda lam: _dwarf(lam, depth=0.2), err_frac=0.12))
    assert si.flag(ix)[0] == "suggests_brown_dwarf"
    assert si.flag(ix, error_scale=3.0)[0] != "suggests_brown_dwarf"


def test_y_dwarf_colour_is_a_lower_limit():
    bands = _spectrum(lambda lam: 20.0 if 3.9 <= lam <= 4.2 else 0.0, floor=0.05)
    colour, is_limit = si.colour_1_4um(bands)
    assert is_limit and colour > 5
    assert si.colour_1_4um([b for b in bands if b.band.center_um < 3])[0] is None  # no 4 um


def test_contaminated_object_is_flagged_with_the_neighbour_named():
    """Like WISE 0855: a star 4" away supplies the short-wavelength light the object lacks."""
    nb = sp.Neighbour("Gaia DR3 42", 4.0 / 6.15, sp.sed_from_magnitudes(G=16.3, J=15.4, H=15.0, Ks=15.0))
    def seen(lam):
        own = 2.0 if lam > 3.8 else 0.0
        return own + nb.flux_mjy(lam) * sp.neighbour_share(nb.separation_px, sp.PSF_FWHM_PX[3])
    ix = si.measure_index(_spectrum(seen, floor=0.005), [nb])
    assert ix.contamination_max >= 0.9 and ix.contamination_at_um < 2.5
    flag, reason = si.flag(ix)
    assert flag == "contaminated" and "Gaia DR3 42" in reason and "4.0\"" in reason
    # Without the neighbour list the same spectrum is not called contaminated.
    assert si.flag(si.measure_index(_spectrum(seen, floor=0.005)))[0] != "contaminated"


# --- catalogue neighbours ---------------------------------------------------------------------


def _star_at(name, ra, dec, epoch_mjd=si.GAIA_EPOCH_MJD, **kw):
    return si.CatalogStar(name, ra, dec, epoch_mjd, **kw)


def test_neighbours_leave_out_the_object_itself_and_keep_a_close_star():
    ra, dec = 150.0, 10.0
    me = _star_at("me", ra, dec, j=14.0, ks=13.5)
    close = _star_at("close", ra + 4.0 / 3600 / math.cos(math.radians(dec)), dec, j=15.0, ks=14.5)
    far = _star_at("far", ra, dec + 60.0 / 3600, j=10.0, ks=9.5)
    nbs = si.neighbours_of(ra, dec, si.GAIA_EPOCH_MJD, [me, close, far], si.GAIA_EPOCH_MJD + 3000)
    assert [n.name for n in nbs] == ["close"]
    assert nbs[0].separation_px == pytest.approx(4.0 / 6.15, rel=1e-3)


def test_a_fast_mover_is_matched_at_the_catalogue_epoch_not_today():
    """Gaia saw the mover 20" from where it is now; that entry is the mover, not a neighbour,
    and a static star at today's position is a neighbour."""
    ra, dec, pm = 150.0, 10.0, 2.0  # arcsec/yr north
    gaia_self = _star_at("self", ra, dec, pm_ra=0.0, pm_dec=pm, g=18.0)
    years = 10.0
    now = si.GAIA_EPOCH_MJD + years * 365.25
    static = _star_at("static", ra, dec + (pm * years + 3.0) / 3600, pm_ra=0.0, pm_dec=0.0, g=17.0)
    nbs = si.neighbours_of(ra, dec, si.GAIA_EPOCH_MJD, [gaia_self, static], now, 0.0, pm, 0.01)
    assert [n.name for n in nbs] == ["static"]
    assert nbs[0].separation_px == pytest.approx(3.0 / 6.15, rel=1e-3)


def test_effective_separation_of_a_passing_star_lies_between_its_extremes():
    seps = [0.3, 1.0, 2.0]
    eff = si.effective_separation(seps)
    assert 0.3 < eff < 2.0
    mean_share = np.mean([sp.neighbour_share(s, 1.3) for s in seps])
    assert sp.neighbour_share(eff, 1.3) == pytest.approx(mean_share, rel=1e-3)
    assert si.effective_separation([1.2]) == 1.2


def test_merge_catalogs_matches_gaia_to_2mass_with_proper_motion():
    """A Gaia star moving 1"/yr is 16" from its 2MASS (2000.0) position: still one star."""
    tm_mjd = si.MJD_J2000
    gaia = [_star_at("Gaia DR3 1", 150.0, 10.0 + 16.0 / 3600, pm_ra=0.0, pm_dec=1.0, g=15.0, bp_rp=1.5)]
    tmass = [_star_at("2MASS J1", 150.0, 10.0, tm_mjd, j=13.0, h=12.5, ks=12.3, tmass_mjd=tm_mjd),
             _star_at("2MASS J2", 150.1, 10.0, tm_mjd, j=14.0, ks=13.0, tmass_mjd=tm_mjd)]
    merged = si.merge_catalogs(gaia, tmass)
    assert [m.name for m in merged] == ["Gaia DR3 1", "2MASS J2"]
    assert merged[0].ks == 12.3 and merged[0].g == 15.0 and merged[0].pm_dec == 1.0
    assert [p[0] for p in merged[0].sed()] == [0.622, 1.235, 1.662, 2.159]


def test_gaia_only_star_gets_an_estimated_ks():
    s = _star_at("g", 0.0, 0.0, g=16.0, bp_rp=1.0)
    assert len(s.sed()) == 2 and s.sed()[1][0] == 2.159
    assert si.ks_from_gaia(16.0, 1.0) == pytest.approx(16.0 - (-0.0981 + 2.089 - 0.1579), abs=1e-9)
    # No colour: a flat F_nu between G and Ks.
    flat = si.ks_from_gaia(16.0, None)
    sed = sp.sed_from_magnitudes(G=16.0, Ks=flat)
    assert sed[0][1] == pytest.approx(sed[1][1])


def test_parse_2mass_keeps_only_good_bands():
    t = Table(rows=[("05591914-1404488", 89.83, -14.08, 13.8, 13.68, 13.58, "AAU", "000", 2451200.5),
                    ("1", 1.0, 1.0, 10.0, 10.0, 10.0, "AAA", "0p0", 2451200.5),
                    ("2", 1.0, 1.0, 10.0, 10.0, 10.0, "UUU", "000", 2451200.5)],
              names=("designation", "ra", "dec", "j_m", "h_m", "k_m", "ph_qual", "cc_flg", "jdate"))
    stars = si.parse_2mass(t)
    assert len(stars) == 2
    assert (stars[0].j, stars[0].h, stars[0].ks) == (13.8, 13.68, None)  # U: an upper limit
    assert stars[0].tmass_mjd == pytest.approx(51200.0) and stars[0].name == "2MASS J05591914-1404488"
    assert (stars[1].j, stars[1].h, stars[1].ks) == (10.0, None, 10.0)  # p: persistence artifact


def test_parse_gaia_converts_motions():
    t = Table(rows=[(123, 10.0, 20.0, 2016.0, 1000.0, -500.0, 15.2, 1.1)],
              names=("source_id", "ra", "dec", "ref_epoch", "pmra", "pmdec", "phot_g_mean_mag", "bp_rp"))
    (s,) = si.parse_gaia(t)
    assert s.name == "Gaia DR3 123" and (s.pm_ra, s.pm_dec) == (1.0, -0.5)
    assert s.epoch_mjd == pytest.approx(si.GAIA_EPOCH_MJD)


def test_cone_checks_reject_bad_input():
    with pytest.raises(ValueError):
        si._check_cone(400.0, 0.0, 10.0)
    with pytest.raises(ValueError):
        si._check_cone(10.0, 0.0, 5000.0)


# --- wing photometry of saturated stars --------------------------------------------------------


def _saturated_frame(amp, core_px=4.0, extra_flag_box=None):
    hdul = fits.open(io.BytesIO(make_mef(120.0, 40.0)))
    yy, xx = np.indices(SHAPE)
    x0, y0 = WCS(hdul["IMAGE"].header).world_to_pixel_values(120.0, 40.0)
    r = np.hypot(xx - x0, yy - y0)
    hdul["IMAGE"].data = (1.0 + amp * (1 + r**2) ** -1.5).astype(np.float32)  # a Moffat-like PSF with wings
    flags = np.zeros(SHAPE, np.int32)
    flags[r <= core_px] |= 1 << FLAG_BITS["OVERFLOW"]
    if extra_flag_box is not None:
        ys, xs = extra_flag_box
        flags[ys, xs] |= 1 << FLAG_BITS["BLOOM"]
    flags[r <= core_px + 1] |= 1 << FLAG_BITS["HALO"]  # the star's own halo flag must not be thrown away
    hdul["FLAGS"].data = flags
    return hdul


def test_wing_flux_scales_with_the_star_and_ignores_the_saturated_core():
    a = si.wing_flux(_saturated_frame(1000.0), 120.0, 40.0)
    b = si.wing_flux(_saturated_frame(2500.0, core_px=6.0), 120.0, 40.0)
    assert a is not None and b is not None and a.flux > 0
    assert b.flux / a.flux == pytest.approx(2.5, rel=0.01)
    assert a.detector == 3 and a.wavelength_um > 1.0 and a.flux_err > 0


def test_flagged_ring_pixels_are_replaced_by_scaling():
    """A bleed column through the ring: the rest of the ring, scaled up, gives the same flux."""
    clean = si.wing_flux(_saturated_frame(1000.0), 120.0, 40.0)
    hdul = _saturated_frame(1000.0)
    x0, _ = WCS(hdul["IMAGE"].header).world_to_pixel_values(120.0, 40.0)
    col = int(round(float(x0)))
    bled = si.wing_flux(_saturated_frame(1000.0, extra_flag_box=(slice(0, SHAPE[0]), slice(col - 1, col + 2))), 120.0, 40.0)
    assert bled.flux == pytest.approx(clean.flux, rel=0.05)  # scaling assumes a uniform ring
    # Most of the ring flagged: no measurement rather than a wild extrapolation.
    assert si.wing_flux(_saturated_frame(1000.0, extra_flag_box=(slice(0, SHAPE[0]), slice(0, col + 3))), 120.0, 40.0) is None


def test_wing_flux_needs_the_whole_ring_on_the_image():
    assert si.wing_flux(_saturated_frame(1000.0), 120.0 + 0.08, 40.0) is None


# --- periods ----------------------------------------------------------------------------------


def _mira(period=245.0, n=150, seed=0, baseline=700.0):
    rng = np.random.default_rng(seed)
    t = np.sort(rng.uniform(60800, 60800 + baseline, n))
    band = rng.integers(0, 12, n)
    det = 1 + band % 3
    level = np.linspace(-2, 3, 12)[band]
    y = level + 0.6 * np.sin(2 * np.pi * (t - 60800) / period + 0.7) + 0.03 * rng.standard_normal(n)
    return t, y, np.full(n, 0.03), band, det


def test_fit_period_recovers_a_sinusoid_through_band_levels():
    t, y, e, g, a = _mira()
    fit = si.fit_period(t, y, e, g, a)
    assert fit.period_days == pytest.approx(245.0, rel=0.02)
    assert fit.constrained and fit.period_err_days < 5
    assert fit.chi2_best / fit.dof == pytest.approx(1.0, abs=0.3)
    assert fit.chi2_constant > 10 * fit.chi2_best
    assert all(v == pytest.approx(2 * 0.6 * 2.5 / math.log(10), rel=0.05) for v in fit.amplitude_mag.values())
    assert fit.notes == []


def test_fit_period_says_when_the_baseline_is_short():
    t, y, e, g, a = _mira(baseline=400.0)
    fit = si.fit_period(t, y, e, g, a)
    assert any("fewer than two cycles" in n for n in fit.notes)


def test_robust_fit_drops_outliers():
    t, y, e, g, a = _mira(seed=2)
    y = y.copy()
    y[[5, 50, 90]] += 3.0
    fit, keep = si.fit_period_robust(t, y, e, g, a)
    assert not keep[[5, 50, 90]].any() and keep.sum() == len(y) - 3
    assert fit.period_days == pytest.approx(245.0, rel=0.02)
