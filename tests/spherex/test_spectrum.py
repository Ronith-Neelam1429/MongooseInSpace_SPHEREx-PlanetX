"""Offline tests for planetx.spherex.spectrum: photometry, wavelengths, masking, bands, features."""
import io
import itertools
import math

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_area
from planetx.spherex import spectrum as sp
from planetx.spherex.cutouts import FLAG_BITS, without_sip
from scipy.special import erf
from synthetic import SHAPE, SIP_A00, make_mef, wavelength_at_raw_pixel

RA, DEC = 120.0, 40.0
SIGMA_PX = 0.6  # about SPHEREx's PSF (FWHM ~1.4 px)
TOTAL = 400.0  # source flux, MJy/sr summed over pixels
BACKGROUND = 5.0


def _frame(sources=(), flags=None, background=BACKGROUND, psf_fwhm=True, epsf=False, detector=1):
    """A synthetic SPHEREx cutout with pixel-integrated Gaussian sources (what a detector records)."""
    hdul = fits.open(io.BytesIO(make_mef(RA, DEC, flags=flags)))
    wcs = WCS(hdul["IMAGE"].header)
    yy, xx = np.indices(SHAPE)
    image = np.full(SHAPE, background, dtype=np.float64)
    for ra, dec in sources:
        x0, y0 = wcs.world_to_pixel_values(ra, dec)
        image += TOTAL * _gauss_share(xx - x0, SIGMA_PX) * _gauss_share(yy - y0, SIGMA_PX)
    hdul["IMAGE"].data = image.astype(np.float32)
    hdul["IMAGE"].header["DETECTOR"] = detector
    if psf_fwhm:
        hdul["IMAGE"].header["PSF_FWHM"] = SIGMA_PX * 2.3548 * 6.15
    if epsf:
        _add_epsf(hdul)
    return hdul


def _gauss_share(offset, sigma):
    s = sigma * math.sqrt(2)
    return 0.5 * (erf((offset + 0.5) / s) - erf((offset - 0.5) / s))


def _add_epsf(hdul, ov=5, n=33):
    """An EPSF table like SPHEREx's: 5x oversampled effective PSF (pixel-integrated Gaussian)
    on a 3 x 3 grid of detector zones, plus the 'A' WCS to detector pixels."""
    off = (np.arange(n) - (n - 1) / 2) / ov
    psf = np.outer(_gauss_share(off, SIGMA_PX), _gauss_share(off, SIGMA_PX))
    psf /= psf.sum()
    centres = np.array([(x, y) for y in (100.0, 1000.0, 1900.0) for x in (100.0, 1000.0, 1900.0)])
    stack = np.repeat(psf[np.newaxis], len(centres), axis=0)
    # A broad decoy PSF everywhere except the zone the cutout sits in: picking the wrong zone shows.
    broad = np.outer(_gauss_share(off, 1.5), _gauss_share(off, 1.5))
    stack[np.arange(len(centres)) != 4] = broad / broad.sum()
    table = fits.BinTableHDU.from_columns([
        fits.Column(name="XCENTER", format="E", array=centres[:, 0]),
        fits.Column(name="YCENTER", format="E", array=centres[:, 1]),
        fits.Column(name="EPSF", format=f"{n * n}D", dim=f"({n},{n})", array=stack),
    ], name="EPSF")
    table.header["OVSMPX"] = ov
    hdul.append(table)
    h = hdul["IMAGE"].header
    # Cutout pixel 0 is detector pixel 970: the cutout lies in the middle zone (1000, 1000).
    for i in (1, 2):
        h[f"CTYPE{i}A"], h[f"CRPIX{i}A"], h[f"CRVAL{i}A"], h[f"CDELT{i}A"] = "LINEAR", -969.0, 0.0, 1.0
    h["WCSAXESA"] = 2


def _true_flux_mjy(hdul):
    omega = proj_plane_pixel_area(WCS(hdul["IMAGE"].header).celestial) * (math.pi / 180) ** 2
    return TOTAL * omega * 1e9


def _offset(east_arcsec, north_arcsec):
    return RA + east_arcsec / 3600 / math.cos(math.radians(DEC)), DEC + north_arcsec / 3600


# --- wavelength ------------------------------------------------------------------------


def test_wavelength_comes_from_the_raw_pixel_without_sip():
    ra, dec = _offset(20, -10)
    with _frame([(ra, dec)]) as hdul:
        m = sp.measure(hdul, ra, dec)
        x, _ = WCS(hdul["IMAGE"].header).world_to_pixel_values(ra, dec)  # celestial: SIP applies
        sip_dropped = WCS(without_sip(hdul["IMAGE"].header)).world_to_pixel_values(ra, dec)[0]
    assert m.status == "ok"
    assert m.wavelength_um == pytest.approx(wavelength_at_raw_pixel(float(x)), abs=2e-6)
    assert m.bandwidth_um == pytest.approx(0.04, abs=1e-6)
    # Locating the object without SIP would put it SIP_A00 pixels off: a different wavelength.
    assert abs(float(sip_dropped) - float(x)) == pytest.approx(SIP_A00, abs=0.1)


# --- flux ------------------------------------------------------------------------------


@pytest.mark.parametrize("dx, dy", [(0.0, 0.0), (0.37, -0.21), (-0.5, 0.5)])
def test_flux_is_mjy_with_gaussian_aperture_correction(dx, dy):
    with _frame() as hdul:
        wcs = WCS(hdul["IMAGE"].header)
        ra, dec = (float(v) for v in wcs.pixel_to_world_values(30 + dx, 30 + dy))
    with _frame([(ra, dec)]) as hdul:
        m = sp.measure(hdul, ra, dec)
        truth = _true_flux_mjy(hdul)
    assert m.status == "ok"
    # The aperture holds ~90% (less off-centre); the correction restores the total.
    assert 1.01 < m.aperture_correction < 1.35
    assert m.flux_mjy == pytest.approx(truth, rel=0.015)


def test_flux_uses_the_epsf_of_the_nearest_zone():
    ra, dec = _offset(-6, 9)
    with _frame([(ra, dec)], psf_fwhm=False, epsf=True) as hdul:
        m = sp.measure(hdul, ra, dec)
        truth = _true_flux_mjy(hdul)
    assert m.status == "ok"
    assert m.flux_mjy == pytest.approx(truth, rel=0.02)  # a decoy (wider) zone would give about twice the flux


def test_no_psf_information_means_no_correction():
    ra, dec = _offset(0, 0)
    with _frame([(ra, dec)], psf_fwhm=False) as hdul:
        m = sp.measure(hdul, ra, dec)
    assert m.aperture_correction == 1.0


def test_error_comes_from_variance_and_the_background():
    ra, dec = _offset(0, 0)
    with _frame([(ra, dec)]) as hdul:
        m = sp.measure(hdul, ra, dec)
        wcs = WCS(hdul["IMAGE"].header)
        x, y = wcs.world_to_pixel_values(ra, dec)
        yy, xx = np.indices(SHAPE)
        n_ap = int((np.hypot(xx - x, yy - y) <= sp.APERTURE_PX).sum())
        to_mjy = proj_plane_pixel_area(wcs.celestial) * (math.pi / 180) ** 2 * 1e9
    # VARIANCE is 1 per pixel and the noiseless background adds nothing; plus the systematic floor.
    photon = math.sqrt(n_ap) * to_mjy * m.aperture_correction
    assert m.flux_err_mjy == pytest.approx(math.hypot(photon, sp.SYSTEMATIC_FRAC * m.flux_mjy), rel=1e-6)
    assert photon > 0.3 * sp.SYSTEMATIC_FRAC * m.flux_mjy  # both terms matter here


def test_background_is_subtracted():
    ra, dec = _offset(0, 0)
    with _frame([(ra, dec)], background=0.0) as a, _frame([(ra, dec)], background=40.0) as b:
        assert sp.measure(a, ra, dec).flux_mjy == pytest.approx(sp.measure(b, ra, dec).flux_mjy, rel=1e-6)


# --- masking ---------------------------------------------------------------------------


def _flags_at(ra, dec, name, radius=0.0, offset_px=(0, 0)):
    with _frame() as hdul:
        x, y = WCS(hdul["IMAGE"].header).world_to_pixel_values(ra, dec)
    flags = np.zeros(SHAPE, np.int32)
    yy, xx = np.indices(SHAPE)
    near = np.hypot(xx - round(float(x)) - offset_px[0], yy - round(float(y)) - offset_px[1]) <= radius
    flags[near] = 1 << FLAG_BITS[name]
    return flags


@pytest.mark.parametrize("name, status", [
    ("BLOOM", "saturated"), ("OVERFLOW", "saturated"), ("NONLINEAR", "saturated"),
    ("PERSIST", "masked"), ("SUR_ERROR", "masked"), ("HOT", "masked"),
])
def test_flagged_aperture_rejects_the_exposure(name, status):
    ra, dec = _offset(0, 0)
    with _frame([(ra, dec)], flags=_flags_at(ra, dec, name)) as hdul:
        m = sp.measure(hdul, ra, dec)
    assert m.status == status and m.flux_mjy is None
    assert m.wavelength_um is not None  # still says where it would have been


def test_harmless_flags_and_flagged_background_pixels_are_fine():
    ra, dec = _offset(0, 0)
    flags = _flags_at(ra, dec, "SOURCE", radius=1.5) | _flags_at(ra, dec, "HOT", radius=1.0, offset_px=(5, 0))
    with _frame([(ra, dec)], flags=flags) as hdul:
        m = sp.measure(hdul, ra, dec)
        truth = _true_flux_mjy(hdul)
    assert m.status == "ok" and m.flux_mjy == pytest.approx(truth, rel=0.015)


def test_object_near_the_edge_is_off_image():
    with _frame() as hdul:
        ra, dec = (float(v) for v in WCS(hdul["IMAGE"].header).pixel_to_world_values(3, 30))
        assert sp.measure(hdul, ra, dec).status == "off_image"


# --- bands -----------------------------------------------------------------------------


def test_band_grid_has_102_contiguous_bands_per_detector():
    assert len(sp.BANDS) == 102 and [b.index for b in sp.BANDS] == list(range(102))
    for det, (lo, hi) in sp.DETECTOR_EDGES_UM.items():
        mine = [b for b in sp.BANDS if b.detector == det]
        assert len(mine) == 17 and mine[0].lo_um == pytest.approx(lo) and mine[-1].hi_um == pytest.approx(hi)
        assert all(a.hi_um == pytest.approx(b.lo_um) for a, b in itertools.pairwise(mine))
        ratios = [b.hi_um / b.lo_um for b in mine]
        assert max(ratios) == pytest.approx(min(ratios))  # constant resolving power


def test_band_index_uses_the_measurement_detector():
    assert sp.band_index(1, 0.7501) == 0
    assert sp.band_index(1, 1.0999) == 16
    assert sp.band_index(2, 1.0999) == 17  # just below D2's edge: still D2's first band
    assert sp.band_index(6, 4.999) == 101
    assert sp.band_index(3, 1.0) is None  # far outside D3
    assert sp.band_index(7, 1.0) is None
    assert sp.band_index(1, float("nan")) is None


def _m(wave, flux, err=1.0, det=1, status="ok"):
    return sp.Measurement(status, det, 61000.0, wave, 0.02, flux, err, 1.1)


def test_combine_drops_outliers_and_weights_by_error():
    w = sp.BANDS[3].center_um
    ms = [_m(w, 10.0), _m(w, 10.2), _m(w, 9.8), _m(w, 10.1, 0.5), _m(w, 50.0)]
    bands, dropped = sp.combine(ms)
    assert len(bands) == 1 and dropped == {4}
    b = bands[0]
    assert b.band.index == 3 and (b.n_used, b.n_rejected) == (4, 1)
    expected = (10.0 + 10.2 + 9.8 + 4 * 10.1) / 7
    assert b.flux_mjy == pytest.approx(expected)
    assert b.flux_err_mjy == pytest.approx(1 / math.sqrt(7))  # chi2 < 1: no inflation


def test_combine_inflates_errors_when_points_disagree():
    w = sp.BANDS[40].center_um
    bands, _ = sp.combine([_m(w, 10.0, det=3), _m(w, 20.0, det=3)])
    assert bands[0].flux_mjy == pytest.approx(15.0)
    assert bands[0].flux_err_mjy == pytest.approx(5.0)  # scatter, not the formal 0.71


def test_combine_ignores_rejected_and_unbinned_measurements():
    w = sp.BANDS[0].center_um
    ms = [_m(w, 5.0), _m(w, None, None, status="saturated"), _m(2.0, 9.0, det=1)]
    bands, _ = sp.combine(ms)
    assert [(b.band.index, b.n_used) for b in bands] == [(0, 1)]


def test_bands_come_out_sorted_by_wavelength():
    ms = [_m(b.center_um, 1.0, det=b.detector) for b in reversed(sp.BANDS[::7])]
    bands, _ = sp.combine(ms)
    assert [b.band.index for b in bands] == sorted(b.band.index for b in bands)


# --- features --------------------------------------------------------------------------


def _spectrum(flux_at, err_frac=0.02):
    bands = []
    for b in sp.BANDS:
        f = flux_at(b.center_um)
        bands.append(sp.BandFlux(b, f, max(abs(f), 1.0) * err_frac, 2, 0))
    return bands


def _star(lam):
    return 100.0 * lam**-2.0  # Rayleigh-Jeans F_nu


def _dwarf(lam):
    """A star-like continuum with the T-dwarf water and methane bands carved out (half the flux)."""
    absorbed = any(fd.window_um[0] <= lam <= fd.window_um[1] for fd in sp.FEATURES)
    return 100.0 * lam**-1.0 * (0.5 if absorbed else 1.0)


def test_a_smooth_star_shows_no_absorption():
    feats = sp.absorption_features(_spectrum(_star))
    assert all(f.detected is False for f in feats)
    assert all(abs(f.depth) < 0.03 for f in feats)  # the power-law continuum follows the slope
    code, text = sp.interpret(feats, 102, saturated=False)
    assert code == "none" and "no sign" in text


def test_methane_and_water_suggest_a_brown_dwarf():
    feats = {f.definition.key: f for f in sp.absorption_features(_spectrum(_dwarf))}
    for key in ("h2o_1.4", "h2o_1.9", "ch4_1.6", "h2o_2.7", "ch4_3.3"):
        assert feats[key].detected and feats[key].depth == pytest.approx(0.5, abs=0.03), key
        assert feats[key].continuum == "two windows"
    code, text = sp.interpret(list(feats.values()), 102, saturated=False)
    assert code == "brown_dwarf" and "suggests" in text and " is a " not in text


def test_water_alone_suggests_a_cool_object():
    def cool(lam):
        water = any(fd.window_um[0] <= lam <= fd.window_um[1] for fd in sp.FEATURES if fd.molecule == "H2O")
        return _star(lam) * (0.6 if water else 1.0)

    code, text = sp.interpret(sp.absorption_features(_spectrum(cool)), 102, saturated=False)
    assert code == "cool_object" and "suggests" in text


def test_red_object_invisible_below_3um_uses_a_rayleigh_jeans_continuum():
    """Like WISE 0855: nothing below ~3 um, bright at 4 um, the 3.3 um methane band dark."""
    def y_dwarf(lam):
        return 20.0 if 3.9 <= lam <= 4.2 else 0.0

    bands = _spectrum(y_dwarf, err_frac=0.05)
    f = sp.measure_feature(bands, next(fd for fd in sp.FEATURES if fd.key == "ch4_3.3"))
    assert f.continuum == "Rayleigh-Jeans from one window"
    assert f.depth == pytest.approx(1.0) and f.detected
    # Below 2.4 um both sides are noise: those features cannot be judged.
    assert sp.measure_feature(bands, sp.FEATURES[0]).detected is None


def test_shallow_or_noisy_dips_are_not_claimed():
    def dip(lam, depth):
        return _star(lam) * (1 - depth if 3.20 <= lam <= 3.45 else 1.0)

    fd = next(fd for fd in sp.FEATURES if fd.key == "ch4_3.3")
    assert sp.measure_feature(_spectrum(lambda lam: dip(lam, 0.08)), fd).detected is False  # too shallow
    assert sp.measure_feature(_spectrum(lambda lam: dip(lam, 0.3), err_frac=0.3), fd).detected is False  # too noisy


def test_missing_bands_leave_a_feature_unmeasured():
    bands = [b for b in _spectrum(_dwarf) if not 3.2 <= b.band.center_um <= 3.45]
    f = sp.measure_feature(bands, next(fd for fd in sp.FEATURES if fd.key == "ch4_3.3"))
    assert (f.depth, f.detected, f.significance) == (None, None, None)


def test_interpret_without_data_or_when_saturated():
    assert sp.interpret([], 0, saturated=False)[0] == "unknown"
    assert sp.interpret(sp.absorption_features([]), 0, saturated=False)[0] == "unknown"
    code, text = sp.interpret(sp.absorption_features(_spectrum(_dwarf)), 102, saturated=True)
    assert code == "saturated" and "saturates" in text


# --- moving objects --------------------------------------------------------------------


def test_position_at_applies_proper_motion():
    from astropy.time import Time

    mjd = Time(2017.0, format="jyear").mjd
    ra, dec = sp.position_at(RA, DEC, pmra_mas=1000.0, pmdec_mas=-2000.0, epoch_yr=2016.0, mjd=mjd)
    assert (dec - DEC) * 3600 == pytest.approx(-2.0, abs=1e-6)
    assert (ra - RA) * 3600 * math.cos(math.radians(DEC)) == pytest.approx(1.0, abs=1e-6)
    assert sp.position_at(359.9999, 0.0, 3.6e6, 0.0, 2016.0, mjd)[0] < 1.0  # wraps at 360
