"""A SPHEREx spectrum of one sky position from single exposures (pure functions, no network).

SPHEREx has no slit or grating: each detector sits behind a linear variable filter,
so every pixel sees its own narrow band, and an object gets a different wavelength
in every exposure that covers it. A spectrum is built by measuring the object in
many exposures and sorting the measurements by wavelength:

1. `measure(hdul, ra, dec)`: forced aperture photometry at (ra, dec) in one cutout.
   * Wavelength and bandwidth at the object's pixel from the WCS-WAVE lookup
     (alternate WCS "W", SIP cards dropped: the table is indexed by raw pixels).
   * Aperture radius APERTURE_PX, local background = sigma-clipped median of the
     annulus ANNULUS_PX (bad pixels left out).
   * IMAGE is MJy/sr: the background-subtracted sum times the pixel solid angle
     (from the celestial WCS) is MJy, times 1e9 mJy.
   * Aperture correction from the file's own EPSF (5x oversampled effective PSF
     per detector zone; nearest zone in detector pixels), evaluated at the object's
     sub-pixel position. Without an EPSF, a Gaussian of PSF_FWHM from the header.
   * Errors from VARIANCE plus the uncertainty of the background level, plus
     SYSTEMATIC_FRAC of the flux in quadrature.
   * Any pixel in the aperture flagged with PHOTOMETRY_BAD_FLAGS (or OVERFLOW)
     rejects the exposure: "saturated" when the flags are the saturation ones
     (OVERFLOW, BLOOM, NONLINEAR), else "masked". Nothing is interpolated.
2. `combine(measurements)`: the 102-band grid (6 detectors x 17 bands, log-spaced
   between the detector edges, i.e. constant resolving power per detector, as
   SPHEREx defines its bands). Per band: outliers beyond 3 sigma of the median
   (MAD scatter or the point's own error, whichever is larger) are dropped, the
   rest averaged with inverse-variance weights; the error is scaled up by
   sqrt(reduced chi^2) when the points scatter more than their errors.
3. `absorption_features(bands)` and `interpret(...)`: see FEATURES below.

Fluxes are flux densities F_nu in mJy. Exposures from all survey passes are
combined, so a variable object's spectrum mixes its states.
"""
from __future__ import annotations

import functools
import math
import warnings
from dataclasses import dataclass

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_area
from scipy.ndimage import map_coordinates
from scipy.special import erf

from planetx.spherex.cutouts import PHOTOMETRY_BAD_FLAGS, flag_mask, without_sip

APERTURE_PX = 1.5  # radius; SPHEREx PSF FWHM is ~1.2-1.6 px, so this holds ~85-90% of the flux
ANNULUS_PX = (4.0, 8.0)  # background annulus radii
MIN_ANNULUS_PIXELS = 12
SATURATION_FLAGS = ("OVERFLOW", "BLOOM", "NONLINEAR")
REJECT_FLAGS = (*PHOTOMETRY_BAD_FLAGS, "OVERFLOW")
MJY_PER_MJY = 1e9  # MJy -> mJy
# Added in quadrature to each exposure's error: aperture correction, flat field and
# calibration. Without it neighbouring bands of a smooth star differ by several of
# their formal (photon-noise) errors.
SYSTEMATIC_FRAC = 0.02

# Detector band edges (um). 17 bands per detector, log-spaced: resolving power
# ~41 (D1-D3), ~35 (D4), ~110 (D5), ~130 (D6), as published for SPHEREx.
DETECTOR_EDGES_UM = {
    1: (0.75, 1.10), 2: (1.10, 1.63), 3: (1.63, 2.42),
    4: (2.42, 3.82), 5: (3.82, 4.42), 6: (4.42, 5.00),
}
BANDS_PER_DETECTOR = 17
CLIP_SIGMA = 3.0


@dataclass(frozen=True)
class Band:
    index: int  # 0-101
    detector: int
    lo_um: float
    hi_um: float

    @property
    def center_um(self) -> float:
        return math.sqrt(self.lo_um * self.hi_um)


def band_grid() -> list[Band]:
    """The 102 SPHEREx bands, shortest wavelength first."""
    bands = []
    for det, (lo, hi) in DETECTOR_EDGES_UM.items():
        edges = np.geomspace(lo, hi, BANDS_PER_DETECTOR + 1)
        for k in range(BANDS_PER_DETECTOR):
            bands.append(Band((det - 1) * BANDS_PER_DETECTOR + k, det, float(edges[k]), float(edges[k + 1])))
    return bands


BANDS = band_grid()


def band_index(detector: int, wavelength_um: float) -> int | None:
    """The band of a measurement: within its own detector (bands of neighbouring detectors overlap
    slightly at the edges), None when the wavelength is far outside that detector's range."""
    if detector not in DETECTOR_EDGES_UM or not np.isfinite(wavelength_um):
        return None
    lo, hi = DETECTOR_EDGES_UM[detector]
    if not lo * 0.97 <= wavelength_um <= hi * 1.03:
        return None
    k = math.floor(BANDS_PER_DETECTOR * math.log(wavelength_um / lo) / math.log(hi / lo))
    return (detector - 1) * BANDS_PER_DETECTOR + min(BANDS_PER_DETECTOR - 1, max(0, k))


def position_at(ra: float, dec: float, pmra_mas: float, pmdec_mas: float, epoch_yr: float, mjd: float) -> tuple[float, float]:
    """Where an object with this proper motion (mas/yr, pmra includes cos dec) is at `mjd`.

    Linear in RA/Dec (no parallax): fine over the few years and few arcmin involved.
    """
    years = 2000.0 + (mjd - 51544.5) / 365.25 - epoch_yr  # Julian years, as Time(mjd).jyear
    cosd = math.cos(math.radians(dec))
    d_ra = pmra_mas * years / 3.6e6 / cosd if cosd > 1e-9 else 0.0
    return (ra + d_ra) % 360.0, max(-90.0, min(90.0, dec + pmdec_mas * years / 3.6e6))


@dataclass(frozen=True)
class Measurement:
    """Forced photometry of one exposure at the object's position."""

    status: str  # ok | saturated | masked | off_image | no_wavelength
    detector: int | None
    mjd: float
    wavelength_um: float | None = None
    bandwidth_um: float | None = None
    flux_mjy: float | None = None  # total flux (aperture corrected)
    flux_err_mjy: float | None = None
    aperture_correction: float | None = None  # total / aperture flux


class FrameMeasurer:
    """One cutout prepared once for forced photometry at any number of positions.

    Parsing the WCSs (celestial, wavelength, detector) costs far more than the photometry,
    so a survey-scale run measures every object of a field in one pass per cutout.
    """

    def __init__(self, hdul: fits.HDUList):
        self.hdul = hdul
        header = self.header = hdul["IMAGE"].header
        det = header.get("DETECTOR")
        self.detector = int(det) if det is not None else None
        self.mjd = float(header["MJD-AVG"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            self.wcs = WCS(header)
            self.wave_wcs = WCS(without_sip(header), fobj=hdul, key="W")
        self.data = np.asarray(hdul["IMAGE"].data, dtype=np.float64)
        self.flags = np.asarray(hdul["FLAGS"].data)
        self.variance = np.asarray(hdul["VARIANCE"].data, dtype=np.float64) if "VARIANCE" in hdul else None
        bad = ~np.isfinite(self.data) | ((self.flags & flag_mask(REJECT_FLAGS)) != 0)
        if self.variance is not None:
            bad |= ~np.isfinite(self.variance)
        self.bad = bad
        self.yy, self.xx = np.indices(self.data.shape)
        area = proj_plane_pixel_area(self.wcs.celestial)
        self.to_mjy = area * (math.pi / 180) ** 2 * MJY_PER_MJY
        self.scale_arcsec = math.sqrt(area) * 3600
        self._a_wcs: WCS | None | bool = False  # False: not built yet; None: the file has none

    def a_wcs(self) -> WCS | None:
        if self._a_wcs is False:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FITSFixedWarning)
                    self._a_wcs = WCS(without_sip(self.header), key="A")
            except (KeyError, ValueError):
                self._a_wcs = None
        return self._a_wcs

    def measure(self, ra: float, dec: float) -> Measurement:
        """Forced aperture photometry at (ra, dec) (see the module doc)."""
        detector, mjd, data = self.detector, self.mjd, self.data
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            x, y = (float(v) for v in self.wcs.world_to_pixel_values(ra, dec))
            wave, width = (float(v) for v in self.wave_wcs.all_pix2world(x, y, 0))
        ny, nx = data.shape
        r_out = ANNULUS_PX[1]
        if not (np.isfinite(x) and np.isfinite(y)) or x < r_out or y < r_out or x > nx - 1 - r_out or y > ny - 1 - r_out:
            return Measurement("off_image", detector, mjd)
        if not np.isfinite(wave) or wave <= 0:
            return Measurement("no_wavelength", detector, mjd)
        common = {"detector": detector, "mjd": mjd, "wavelength_um": wave, "bandwidth_um": width if np.isfinite(width) else None}

        flags, variance, bad, xx, yy = self.flags, self.variance, self.bad, self.xx, self.yy
        r = np.hypot(xx - x, yy - y)
        aperture = r <= APERTURE_PX
        if ((flags[aperture] & flag_mask(SATURATION_FLAGS)) != 0).any():
            return Measurement("saturated", **common)
        if bad[aperture].any():
            return Measurement("masked", **common)
        annulus = (r >= ANNULUS_PX[0]) & (r <= ANNULUS_PX[1]) & ~bad
        if annulus.sum() < MIN_ANNULUS_PIXELS:
            return Measurement("masked", **common)
        _, bkg, bkg_std = sigma_clipped_stats(data[annulus], sigma=3.0, maxiters=5)
        n_ap, n_bkg = int(aperture.sum()), int(annulus.sum())

        to_mjy = self.to_mjy
        flux_ap = float((data[aperture] - bkg).sum()) * to_mjy
        pixel_var = float(variance[aperture].sum()) if variance is not None else n_ap * bkg_std**2
        # Error of the background level (median: pi/2 times the variance of a mean).
        var_ap = (pixel_var + n_ap**2 * (math.pi / 2) * bkg_std**2 / n_bkg) * to_mjy**2

        fraction = aperture_fraction(self.hdul, self.header, x, y, aperture, xx, yy,
                                     a_wcs=self.a_wcs(), scale_arcsec=self.scale_arcsec)
        flux = flux_ap / fraction
        return Measurement(
            "ok", **common, flux_mjy=flux, flux_err_mjy=math.hypot(math.sqrt(var_ap) / fraction, SYSTEMATIC_FRAC * flux),
            aperture_correction=1.0 / fraction,
        )


def measure(hdul: fits.HDUList, ra: float, dec: float) -> Measurement:
    """Forced aperture photometry at (ra, dec) in one SPHEREx cutout (see the module doc)."""
    return FrameMeasurer(hdul).measure(ra, dec)


def aperture_fraction(
    hdul: fits.HDUList, header: fits.Header, x: float, y: float,
    aperture: np.ndarray, xx: np.ndarray, yy: np.ndarray,
    a_wcs: WCS | None = None, scale_arcsec: float | None = None,
) -> float:
    """Fraction of a point source's flux at (x, y) that falls in the `aperture` pixels.

    From the file's EPSF if it has one, else a Gaussian of the header's PSF_FWHM,
    else 1 (no correction). The EPSF is an effective PSF (pixel response included)
    sampled OVSMPX times finer than the pixels; its value at a pixel's offset from the
    source is proportional to that pixel's share of the flux. Shares are normalised
    over the EPSF box (about 6.6 pixels), so flux beyond the box (~1-2%) is ignored.
    `a_wcs` (detector pixels) and `scale_arcsec` may be passed in when already known.
    """
    dx, dy = xx[aperture] - x, yy[aperture] - y
    epsf = _epsf_near(hdul, header, x, y, a_wcs)
    if epsf is not None:
        psf, ov = epsf
        c = (np.array(psf.shape) - 1) / 2
        half = c[1] / ov  # box half-width in native pixels
        bx, by = np.meshgrid(np.arange(-math.ceil(half) - 1, math.ceil(half) + 2), np.arange(-math.ceil(half) - 1, math.ceil(half) + 2))
        # Pixels around the source at its sub-pixel phase: offsets of their centres from it.
        fx, fy = round(x) - x, round(y) - y
        all_dx, all_dy = (bx + fx).ravel(), (by + fy).ravel()
        total = map_coordinates(psf, [c[0] + all_dy * ov, c[1] + all_dx * ov], order=1, cval=0.0).sum()
        inside = map_coordinates(psf, [c[0] + dy * ov, c[1] + dx * ov], order=1, cval=0.0).sum()
        if total > 0 and inside > 0:
            return float(inside / total)
    fwhm_arcsec = header.get("PSF_FWHM")
    if fwhm_arcsec:
        if scale_arcsec is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                scale_arcsec = math.sqrt(proj_plane_pixel_area(WCS(header).celestial)) * 3600
        sigma = float(fwhm_arcsec) / scale_arcsec / (2 * math.sqrt(2 * math.log(2)))
        share = _gauss_1d(dx, sigma) * _gauss_1d(dy, sigma)
        return float(share.sum())
    return 1.0


def _gauss_1d(offset: np.ndarray, sigma: float) -> np.ndarray:
    """Share of a unit Gaussian falling in pixels [offset - 0.5, offset + 0.5]."""
    s = sigma * math.sqrt(2)
    return 0.5 * (erf((offset + 0.5) / s) - erf((offset - 0.5) / s))


def _epsf_near(
    hdul: fits.HDUList, header: fits.Header, x: float, y: float, a_wcs: WCS | None = None,
) -> tuple[np.ndarray, int] | None:
    """The EPSF of the detector zone nearest the object, and its oversampling factor."""
    if "EPSF" not in hdul:
        return None
    table = hdul["EPSF"]
    ov = int(table.header.get("OVSMPX", 0))
    names = set(table.columns.names) if table.columns is not None else set()
    if ov <= 0 or not {"XCENTER", "YCENTER", "EPSF"} <= names or len(table.data) == 0:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            ax, ay = (a_wcs or WCS(without_sip(header), key="A")).all_pix2world(x, y, 0)
    except (KeyError, ValueError):
        return None
    zone = int(np.argmin(np.hypot(table.data["XCENTER"] - ax, table.data["YCENTER"] - ay)))
    psf = np.asarray(table.data["EPSF"][zone], dtype=np.float64)
    if psf.ndim != 2 or not np.isfinite(psf).all():
        return None
    return psf, ov


# --- the 102-band spectrum -------------------------------------------------------------


@dataclass(frozen=True)
class BandFlux:
    band: Band
    flux_mjy: float
    flux_err_mjy: float
    n_used: int
    n_rejected: int  # outliers dropped by the robust combination


def combine(measurements: list[Measurement]) -> tuple[list[BandFlux], set[int]]:
    """Robustly combine the "ok" measurements per band.

    Returns the bands that have data (by wavelength) and the indices into
    `measurements` that were dropped as outliers.
    """
    by_band: dict[int, list[int]] = {}
    for i, m in enumerate(measurements):
        if m.status != "ok" or m.flux_mjy is None or not m.flux_err_mjy or m.detector is None:
            continue
        k = band_index(m.detector, m.wavelength_um)
        if k is not None:
            by_band.setdefault(k, []).append(i)
    out, dropped = [], set()
    for k in sorted(by_band):
        ids = by_band[k]
        f = np.array([measurements[i].flux_mjy for i in ids])
        e = np.array([measurements[i].flux_err_mjy for i in ids])
        keep = np.ones(len(f), bool)
        if len(f) >= 3:
            med = np.median(f)
            scatter = 1.4826 * np.median(np.abs(f - med))
            keep = np.abs(f - med) <= CLIP_SIGMA * np.maximum(e, scatter)
            if keep.sum() < 2:  # never let clipping leave one point standing against the others
                keep[:] = True
        w = 1 / e[keep] ** 2
        mean = float((w * f[keep]).sum() / w.sum())
        err = float(1 / math.sqrt(w.sum()))
        if keep.sum() >= 2:
            chi2 = float((w * (f[keep] - mean) ** 2).sum() / (keep.sum() - 1))
            err *= math.sqrt(max(1.0, chi2))
        dropped.update(i for i, kept in zip(ids, keep) if not kept)
        out.append(BandFlux(BANDS[k], mean, err, int(keep.sum()), int((~keep).sum())))
    return out, dropped


# --- absorption features --------------------------------------------------------------


@dataclass(frozen=True)
class FeatureDef:
    key: str
    molecule: str  # CH4 | H2O
    label: str
    window_um: tuple[float, float]
    continuum_um: tuple[tuple[float, float], tuple[float, float]]


# Windows are a few SPHEREx bands wide (R ~ 40 at 1-3 um). Continuum windows sit on
# the flux peaks between molecular bands of cool objects (J ~1.26, H ~1.58, K ~2.1,
# and the ~4.05 um window of T/Y dwarfs).
FEATURES = (
    FeatureDef("h2o_1.4", "H2O", "Water 1.4 µm", (1.34, 1.46), ((1.22, 1.31), (1.54, 1.62))),
    FeatureDef("ch4_1.6", "CH4", "Methane 1.6-1.7 µm (H band)", (1.64, 1.76), ((1.54, 1.62), (2.05, 2.16))),
    FeatureDef("h2o_1.9", "H2O", "Water 1.9 µm", (1.80, 2.00), ((1.54, 1.62), (2.05, 2.16))),
    FeatureDef("ch4_2.2", "CH4", "Methane 2.2-2.4 µm (K band)", (2.20, 2.40), ((1.54, 1.62), (2.05, 2.16))),
    FeatureDef("h2o_2.7", "H2O", "Water 2.7 µm", (2.55, 2.85), ((2.05, 2.16), (3.95, 4.15))),
    FeatureDef("ch4_3.3", "CH4", "Methane 3.3 µm", (3.20, 3.45), ((2.05, 2.16), (3.95, 4.15))),
)
DETECT_SIGMA = 3.0  # depth / error
DETECT_DEPTH = 0.15  # fraction of the continuum missing
RJ_SLOPE = -2.0  # F_nu ~ lambda^-2: a star's Rayleigh-Jeans tail


@dataclass(frozen=True)
class Feature:
    definition: FeatureDef
    depth: float | None  # 1 - F(window) / F(continuum); 0 = none, 1 = all absorbed
    depth_err: float | None
    continuum: str | None  # "two windows" | "Rayleigh-Jeans from one window" | None
    detected: bool | None  # None: the bands needed were not measured (or not bright enough)

    @property
    def significance(self) -> float | None:
        if self.depth is None or not self.depth_err:
            return None
        return self.depth / self.depth_err


def _window(bands: list[BandFlux], lo: float, hi: float) -> tuple[float, float, float] | None:
    """Inverse-variance mean flux of the bands centred in [lo, hi]: (flux, err, wavelength)."""
    sel = [b for b in bands if lo <= b.band.center_um <= hi]
    if not sel:
        return None
    w = np.array([1 / b.flux_err_mjy**2 for b in sel])
    f = np.array([b.flux_mjy for b in sel])
    lam = np.array([b.band.center_um for b in sel])
    return float((w * f).sum() / w.sum()), float(1 / math.sqrt(w.sum())), float(math.exp((w * np.log(lam)).sum() / w.sum()))


def window_flux(bands: list[BandFlux], lo_um: float, hi_um: float) -> tuple[float, float, float] | None:
    """Inverse-variance mean flux of the bands centred in [lo_um, hi_um]: (flux mJy, error, wavelength um)."""
    return _window(bands, lo_um, hi_um)


def measure_feature(bands: list[BandFlux], fd: FeatureDef) -> Feature:
    """Depth of one absorption feature against a simple continuum.

    Continuum: a power law (straight line in log flux vs log wavelength) through the
    two continuum windows. When only one of them has a measured, significant (> 3 sigma)
    positive flux (e.g. a Y dwarf, invisible below 3 um but bright at 4 um), a
    Rayleigh-Jeans slope from that window instead, which is what a star would show.
    """
    feat = _window(bands, *fd.window_um)
    sides = [_window(bands, *w) for w in fd.continuum_um]
    good = [s for s in sides if s is not None and s[0] > DETECT_SIGMA * s[1]]
    if feat is None or not good:
        return Feature(fd, None, None, None, None)
    f, f_err, lam = feat
    if len(good) == 2:
        (f1, e1, l1), (f2, e2, l2) = good
        t = math.log(lam / l1) / math.log(l2 / l1)
        cont = math.exp((1 - t) * math.log(f1) + t * math.log(f2))
        cont_rel = math.hypot((1 - t) * e1 / f1, t * e2 / f2)
        how = "two windows"
    else:
        f1, e1, l1 = good[0]
        cont = f1 * (lam / l1) ** RJ_SLOPE
        cont_rel = e1 / f1
        how = "Rayleigh-Jeans from one window"
    depth = 1 - f / cont
    depth_err = math.hypot(f_err / cont, (f / cont) * cont_rel)
    detected = depth >= DETECT_DEPTH and depth >= DETECT_SIGMA * depth_err
    return Feature(fd, depth, depth_err, how, detected)


def absorption_features(bands: list[BandFlux]) -> list[Feature]:
    return [measure_feature(bands, fd) for fd in FEATURES]


def interpret(features: list[Feature], n_bands: int, saturated: bool) -> tuple[str, str]:
    """(code, sentence) of what the features suggest. Never a classification: a suggestion.

    code: brown_dwarf | cool_object | methane_only | none | unknown | saturated
    """
    if saturated:
        return "saturated", (
            "The object saturates SPHEREx in most exposures (flagged core pixels), so its spectrum "
            "here is incomplete and its features cannot be judged."
        )
    known = [f for f in features if f.detected is not None]
    methane = [f for f in known if f.detected and f.definition.molecule == "CH4"]
    water = [f for f in known if f.detected and f.definition.molecule == "H2O"]
    if not known:
        return "unknown", (
            f"Only {n_bands} of 102 bands were measured, not the ones needed to look for methane or water absorption."
            if n_bands else "No band could be measured at this position."
        )
    if methane and water:
        return "brown_dwarf", (
            "Methane and water absorption: this suggests a cool brown dwarf (T or Y type). "
            "A suggestion from a low-resolution spectrum, not a classification."
        )
    if water:
        return "cool_object", (
            "Water absorption without clear methane: this suggests a cool star or an L-type brown dwarf."
        )
    if methane:
        return "methane_only", (
            "A dip where methane absorbs but no clear water band: possibly a cool brown dwarf, but "
            "water normally goes with methane, so treat it with caution."
        )
    return "none", (
        f"No clear methane or water absorption in the {len(known)} of {len(features)} features that "
        "could be measured: no sign of a cool brown dwarf."
    )


# --- neighbours -------------------------------------------------------------------------
#
# A SPHEREx pixel is 6.2", so a catalogued star a few arcsec away puts much of its light
# into the aperture. WISE 0855 is the case in point: a Gaia G 16.3 star ~4" away supplies
# all the light below 2.5 um, where the Y dwarf itself is invisible. The check predicts,
# per band, how much flux the known neighbours (Gaia DR3 / 2MASS) add to the measured
# total flux and calls a band dominated when that is at least half of what was measured.

# PSF_FWHM header value / 6.15" per detector; the same in every cached file checked
# (528 cutouts, 2026-11-14).
PSF_FWHM_PX = {1: 1.22, 2: 1.21, 3: 1.21, 4: 1.41, 5: 1.40, 6: 1.47}
# Neighbours further away than this put < 0.1% of their flux in the aperture (FWHM <= 1.5 px).
NEIGHBOUR_SEARCH_PX = 3.5
DOMINATED_FRAC = 0.5  # a band is dominated when the neighbours' flux >= this x what was measured

# Zero points of catalogue magnitudes: (effective wavelength um, flux density in Jy at mag 0).
# 2MASS: Cohen et al. 2003. Gaia G: Vega zero point 25.6874 vs AB 25.8010 (Riello et al. 2021),
# i.e. G_AB = G + 0.114, so mag 0 is 3631 Jy * 10**(-0.4 * 0.114).
MAG_ZERO_POINTS = {
    "G": (0.622, 3631.0 * 10 ** (-0.4 * 0.114)),
    "J": (1.235, 1594.0),
    "H": (1.662, 1024.0),
    "Ks": (2.159, 666.7),
}


@dataclass(frozen=True)
class Neighbour:
    """A catalogued source near the object, as seen in the SPHEREx epochs."""

    name: str
    separation_px: float
    sed: tuple[tuple[float, float], ...]  # (wavelength um, flux density mJy), shortest first

    def flux_mjy(self, wavelength_um: float) -> float:
        """F_nu at `wavelength_um` from the catalogue points: a power law between neighbouring
        points, flat bluewards of the first, a Rayleigh-Jeans tail (lambda^-2) redwards of the
        last. A stellar model: a cool neighbour's 3-5 um flux comes out somewhat low, and a
        neighbour known only at one wavelength needs a second (estimated) point to be useful."""
        if not self.sed:
            return 0.0
        lam = np.array([p[0] for p in self.sed])
        f = np.array([p[1] for p in self.sed])
        if wavelength_um <= lam[0]:
            return float(f[0])
        if wavelength_um >= lam[-1]:
            return float(f[-1] * (wavelength_um / lam[-1]) ** RJ_SLOPE)
        return float(np.exp(np.interp(math.log(wavelength_um), np.log(lam), np.log(f))))


def sed_from_magnitudes(**mags: float | None) -> tuple[tuple[float, float], ...]:
    """Catalogue magnitudes (keywords G, J, H, Ks; Vega) as (wavelength um, mJy) points, shortest first."""
    points = []
    for band, mag in mags.items():
        if band not in MAG_ZERO_POINTS:
            raise ValueError(f"unknown band {band!r}")
        if mag is not None and math.isfinite(mag):
            lam, zp_jy = MAG_ZERO_POINTS[band]
            points.append((lam, zp_jy * 1e3 * 10 ** (-0.4 * mag)))
    return tuple(sorted(points))


_PHASES = (np.arange(8) + 0.5) / 8 - 0.5  # centres of 8 equal steps across a pixel


def _aperture_shares(dx: float, dy: float, fwhm_px: float) -> np.ndarray:
    """Share of a point source's flux (pixel-integrated Gaussian) inside the APERTURE_PX aperture
    when the source sits (dx, dy) px from the aperture centre, for a grid of sub-pixel phases
    of the aperture centre (different in every exposure)."""
    sigma = fwhm_px / (2 * math.sqrt(2 * math.log(2)))
    px, py = np.meshgrid(np.arange(-5, 6), np.arange(-5, 6))  # pixel centres around the nearest pixel
    shares = []
    for fx in _PHASES:
        for fy in _PHASES:
            inside = np.hypot(px - fx, py - fy) <= APERTURE_PX
            share = _gauss_1d(px[inside] - fx - dx, sigma) * _gauss_1d(py[inside] - fy - dy, sigma)
            shares.append(share.sum())
    return np.array(shares)


@functools.lru_cache(maxsize=4096)
def neighbour_share(separation_px: float, fwhm_px: float) -> float:
    """Flux a neighbour adds to the object's aperture-corrected total flux, per unit neighbour flux.

    Each exposure's aperture correction divides by the share of the object's own (centred) PSF
    in the aperture at that exposure's sub-pixel phase, so a neighbour adds
    share(at its offset) / share(centred), averaged over phases and directions (the field
    rotates between exposures)."""
    if not separation_px >= 0:
        raise ValueError(f"separation must be >= 0, got {separation_px}")
    centred = _aperture_shares(0.0, 0.0, fwhm_px)
    angles = np.linspace(0, math.pi / 2, 9)  # the pixel grid repeats every 90 deg
    ratios = [_aperture_shares(separation_px * math.cos(t), separation_px * math.sin(t), fwhm_px) / centred
              for t in angles]
    return float(np.mean(ratios))


@dataclass(frozen=True)
class BandContamination:
    band: Band
    neighbour_mjy: float  # predicted flux the neighbours add to this band's measured flux
    fraction: float  # neighbour_mjy / max(measured flux, 2 x its error)
    dominated: bool


def contamination(bands: list[BandFlux], neighbours: list[Neighbour]) -> list[BandContamination]:
    """Per measured band: how much of its flux the catalogued neighbours could supply.

    The reference is the measured flux, or twice its error when that is larger, so a
    neighbour below the noise never dominates, and an object invisible in a band (a Y dwarf
    below 2.5 um) is dominated there by any neighbour that is detected.
    """
    near = [n for n in neighbours if n.separation_px <= NEIGHBOUR_SEARCH_PX]
    shares: dict[tuple[str, float], float] = {}
    out = []
    for b in bands:
        fwhm = PSF_FWHM_PX.get(b.band.detector, 1.4)
        add = 0.0
        for n in near:
            key = (n.name, fwhm)
            if key not in shares:
                shares[key] = neighbour_share(n.separation_px, fwhm)
            add += n.flux_mjy(b.band.center_um) * shares[key]
        ref = max(b.flux_mjy, 2 * b.flux_err_mjy)
        frac = add / ref if ref > 0 else (math.inf if add > 0 else 0.0)
        out.append(BandContamination(b.band, float(add), float(frac), frac >= DOMINATED_FRAC))
    return out


def subtract_neighbours(
    bands: list[BandFlux], contam: list[BandContamination], model_frac_err: float = 0.3,
) -> list[BandFlux]:
    """Bands with the predicted neighbour flux removed. The prediction's own uncertainty
    (`model_frac_err` of it: catalogue colours extrapolated, PSF approximated) is added to the
    error. Only as good as the neighbour model: a cross-check, not a measurement."""
    by_index = {c.band.index: c for c in contam}
    out = []
    for b in bands:
        c = by_index.get(b.band.index)
        if c is None or c.neighbour_mjy == 0:
            out.append(b)
            continue
        out.append(BandFlux(
            b.band, b.flux_mjy - c.neighbour_mjy,
            math.hypot(b.flux_err_mjy, model_frac_err * c.neighbour_mjy), b.n_used, b.n_rejected,
        ))
    return out
