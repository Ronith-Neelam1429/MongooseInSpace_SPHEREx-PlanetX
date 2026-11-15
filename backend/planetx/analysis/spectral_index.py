"""Spectral indices from SPHEREx spectra: is this object methane- and water-dark like a brown dwarf?

SPHEREx is the only all-sky survey with a spectrum (102 bands, 0.75-5 um) of every
source. A cool brown dwarf (T or Y type) has deep methane (1.6, 2.2, 3.3 um) and water
(1.4, 1.9, 2.7 um) absorption that ordinary stars do not have, so two numbers per object,
a methane depth and a water depth, separate the two. This module turns a spectrum
(`planetx.spherex.spectrum.combine`) into those numbers, with errors, and a flag.

* Depths: the per-feature depths of `spectrum.FEATURES` (1 - flux in the band / power-law
  continuum), averaged per molecule with inverse-variance weights. The features share
  continuum windows, so their errors are correlated: the error of the average comes from
  Monte Carlo draws of the band fluxes (each band perturbed by its own error, every
  feature remeasured, the same weights), the 16-84 % half-width of the draws.
* Colour: m_AB(1.22-1.31 um) - m_AB(3.95-4.15 um). A Rayleigh-Jeans star (F_nu ~ lambda^-2)
  is -2.5; T and Y dwarfs are much redder (> 0). A lower limit when the object is not
  detected near 1.3 um (from a 2 sigma upper limit there).
* Neighbours: `spectrum.contamination` predicts, per band, the flux that catalogued
  neighbours (Gaia DR3, 2MASS) add to the aperture. When it reaches half of what was
  measured in any band the indices use, the object is flagged "contaminated": its depths
  may describe the neighbour, not the object (WISE 0855's case below 2.5 um).
* Flag (docs/RESULTS.md): contaminated | suggests_brown_dwarf (methane AND water each at
  least DETECT_DEPTH deep and DETECT_SIGMA significant) | star_like (methane is measured
  and shallower than METHANE_BROWN_DWARF at 2 sigma) | too_faint (neither can be said).
  Always "suggests": a low-resolution spectrum, never a classification.

Catalogue queries (network) are at the bottom; everything above them is pure.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_area

from planetx.spherex import spectrum as sp
from planetx.spherex.cutouts import PIXEL_SCALE_ARCSEC, flag_mask, without_sip

DETECT_DEPTH = sp.DETECT_DEPTH  # 0.15: a feature must remove at least this fraction of the continuum
DETECT_SIGMA = sp.DETECT_SIGMA  # 3
# T dwarfs remove a third or more of the continuum in the methane bands (in these
# SPHEREx windows; deeper at 3.3 um). A methane depth below this at 2 sigma rules one out.
METHANE_BROWN_DWARF = 0.3
N_DRAWS = 300
J_WINDOW_UM = (1.22, 1.31)
M_WINDOW_UM = (3.95, 4.15)
# Every wavelength range the indices read: feature windows, continuum windows, colour windows.
INDEX_WINDOWS_UM = tuple(sorted(
    {fd.window_um for fd in sp.FEATURES} | {w for fd in sp.FEATURES for w in fd.continuum_um}
    | {J_WINDOW_UM, M_WINDOW_UM}
))
CONTINUUM_WINDOWS_UM = ((1.22, 1.31), (1.54, 1.62), (2.05, 2.16), (3.95, 4.15))


@dataclass(frozen=True)
class SpectralIndex:
    methane_depth: float | None
    methane_err: float | None
    water_depth: float | None
    water_err: float | None
    color_1_4um: float | None  # AB mag, m(1.26 um) - m(4.05 um)
    color_is_lower_limit: bool
    snr: float | None  # best continuum window's flux / error
    n_bands: int
    features: dict[str, tuple[float | None, float | None]]  # key -> (depth, error)
    contamination_max: float  # largest neighbour fraction over the bands the indices read
    contamination_at_um: float | None  # where it is largest
    website_suggestion: str  # spectrum.interpret's code for the same bands
    neighbours: list[sp.Neighbour] = field(default_factory=list)


def _in_windows(lam: float, windows=INDEX_WINDOWS_UM) -> bool:
    return any(lo <= lam <= hi for lo, hi in windows)


def _combine_depths(features: list[sp.Feature], molecule: str, weights: dict[str, float]) -> float | None:
    num = den = 0.0
    for f in features:
        w = weights.get(f.definition.key)
        if w is not None and f.depth is not None and f.definition.molecule == molecule:
            num += w * f.depth
            den += w
    return num / den if den > 0 else None


def molecule_depths(
    bands: list[sp.BandFlux], n_draws: int = N_DRAWS, seed: int = 0,
) -> dict[str, tuple[float | None, float | None]]:
    """{"CH4": (depth, error), "H2O": (depth, error)} (see the module doc)."""
    nominal = sp.absorption_features(bands)
    weights = {f.definition.key: 1 / f.depth_err**2 for f in nominal if f.depth is not None and f.depth_err}
    out: dict[str, tuple[float | None, float | None]] = {}
    rng = np.random.default_rng(seed)
    flux = np.array([b.flux_mjy for b in bands])
    err = np.array([b.flux_err_mjy for b in bands])
    draws: dict[str, list[float]] = {"CH4": [], "H2O": []}
    if weights:
        for _ in range(n_draws):
            perturbed = flux + err * rng.standard_normal(len(bands))
            fake = [sp.BandFlux(b.band, float(f), b.flux_err_mjy, b.n_used, b.n_rejected) for b, f in zip(bands, perturbed)]
            feats = sp.absorption_features(fake)
            for mol in draws:
                d = _combine_depths(feats, mol, weights)
                if d is not None:
                    draws[mol].append(d)
    for mol in ("CH4", "H2O"):
        depth = _combine_depths(nominal, mol, weights)
        d = np.array(draws[mol])
        # A draw can lose a feature (its continuum falls below 3 sigma); too few draws = no error.
        if depth is None or len(d) < 0.5 * n_draws:
            out[mol] = (depth, None)
            continue
        lo, hi = np.percentile(d, [15.87, 84.13])
        out[mol] = (depth, float((hi - lo) / 2))
    return out


def colour_1_4um(bands: list[sp.BandFlux]) -> tuple[float | None, bool]:
    """(m_AB(1.26 um) - m_AB(4.05 um), is a lower limit). None without a 4 um detection."""
    j = sp.window_flux(bands, *J_WINDOW_UM)
    m = sp.window_flux(bands, *M_WINDOW_UM)
    if m is None or j is None or m[0] <= DETECT_SIGMA * m[1]:
        return None, False
    if j[0] > 2 * j[1]:
        return float(-2.5 * math.log10(j[0] / m[0])), False
    return float(-2.5 * math.log10(2 * j[1] / m[0])), True


def best_snr(bands: list[sp.BandFlux]) -> float | None:
    snrs = [w[0] / w[1] for w in (sp.window_flux(bands, *win) for win in CONTINUUM_WINDOWS_UM) if w and w[1] > 0]
    return float(max(snrs)) if snrs else None


def measure_index(
    bands: list[sp.BandFlux], neighbours: list[sp.Neighbour] = (), n_draws: int = N_DRAWS, seed: int = 0,
) -> SpectralIndex:
    """All indices of one spectrum; `neighbours` from `neighbours_of` (or none)."""
    depths = molecule_depths(bands, n_draws, seed)
    features = sp.absorption_features(bands)
    colour, is_limit = colour_1_4um(bands)
    contam = [c for c in sp.contamination(bands, list(neighbours)) if _in_windows(c.band.center_um)]
    worst = max(contam, key=lambda c: c.fraction, default=None)
    code, _ = sp.interpret(features, len(bands), saturated=False)
    return SpectralIndex(
        methane_depth=depths["CH4"][0], methane_err=depths["CH4"][1],
        water_depth=depths["H2O"][0], water_err=depths["H2O"][1],
        color_1_4um=colour, color_is_lower_limit=is_limit,
        snr=best_snr(bands), n_bands=len(bands),
        features={f.definition.key: (f.depth, f.depth_err) for f in features},
        contamination_max=float(worst.fraction) if worst else 0.0,
        contamination_at_um=float(worst.band.center_um) if worst and worst.fraction > 0 else None,
        website_suggestion=code,
        neighbours=sorted(neighbours, key=lambda n: n.separation_px),
    )


def _detected(depth: float | None, err: float | None) -> bool:
    return depth is not None and err is not None and depth >= DETECT_DEPTH and depth >= DETECT_SIGMA * err


def flag(ix: SpectralIndex, error_scale: float = 1.0) -> tuple[str, str]:
    """(flag, reason in plain words). `error_scale` (>= 1) widens the depth errors, e.g. by the
    scatter that reference stars show beyond their formal errors."""
    if ix.contamination_max >= sp.DOMINATED_FRAC:
        near = ix.neighbours[0] if ix.neighbours else None
        who = f" ({near.name}, {near.separation_px * PIXEL_SCALE_ARCSEC:.1f}\" away)" if near else ""
        pct = "all" if not math.isfinite(ix.contamination_max) or ix.contamination_max >= 1 else f"{ix.contamination_max:.0%}"
        return "contaminated", (
            f"A catalogued neighbour{who} could supply {pct} of the measured flux near "
            f"{ix.contamination_at_um:.2f} um, so the depths may describe the neighbour, not this object."
        )
    m, w = ix.methane_depth, ix.water_depth
    me = ix.methane_err * error_scale if ix.methane_err is not None else None
    we = ix.water_err * error_scale if ix.water_err is not None else None
    if _detected(m, me) and _detected(w, we):
        return "suggests_brown_dwarf", (
            f"Methane depth {m:.2f} +- {me:.2f} and water depth {w:.2f} +- {we:.2f}: "
            "this suggests a cool brown dwarf (a suggestion from a low-resolution spectrum)."
        )
    if m is not None and me is not None and m + 2 * me < METHANE_BROWN_DWARF:
        return "star_like", f"Methane depth {m:.2f} +- {me:.2f}: shallower than a T dwarf's ({METHANE_BROWN_DWARF})."
    if m is None or me is None:
        return "too_faint", "The bands needed to measure methane were not measured with enough signal."
    if _detected(m, me):
        return "too_faint", (
            f"A methane-like dip ({m:.2f} +- {me:.2f}) without clear water absorption: not enough to suggest anything."
        )
    return "too_faint", f"Methane depth {m:.2f} +- {me:.2f}: too uncertain to tell a star from a brown dwarf."


# --- neighbours from catalogues (pure part) -----------------------------------------------

MJD_J2000 = 51544.5
GAIA_EPOCH_MJD = MJD_J2000 + 16.0 * 365.25


def _jyear(mjd: float) -> float:
    """Julian year of an MJD (what astropy's Time(mjd).jyear gives, ~1000x faster)."""
    return 2000.0 + (mjd - MJD_J2000) / 365.25
MATCH_ARCSEC = 2.0  # same star in Gaia and 2MASS (after Gaia's motion is applied)
SELF_ARCSEC = 2.0  # a catalogue entry this close to the object's own predicted position is the object
SELF_MAX_ARCSEC = 6.0  # cap on that radius when the object's motion is uncertain


def ks_from_gaia(g: float, bp_rp: float | None) -> float:
    """Approximate 2MASS Ks of a star from Gaia G and BP-RP (main-sequence colour relation,
    quadratic in BP-RP, clipped to 0.25 < BP-RP < 5; good to ~0.2 mag). Without a colour,
    a flat F_nu between G and Ks (a solar-type star's)."""
    if bp_rp is None or not math.isfinite(bp_rp):
        return g - 2.5 * math.log10(sp.MAG_ZERO_POINTS["G"][1] / sp.MAG_ZERO_POINTS["Ks"][1])
    x = min(5.0, max(0.25, bp_rp))
    return g - (-0.0981 + 2.089 * x - 0.1579 * x * x)


@dataclass(frozen=True)
class CatalogStar:
    """A star from Gaia DR3 and/or 2MASS, with what the neighbour check needs."""

    name: str
    ra: float  # deg at epoch_mjd
    dec: float
    epoch_mjd: float
    pm_ra: float | None = None  # arcsec/yr, includes cos(dec)
    pm_dec: float | None = None
    g: float | None = None
    bp_rp: float | None = None
    j: float | None = None
    h: float | None = None
    ks: float | None = None
    tmass_mjd: float | None = None  # 2MASS epoch, when the star is in 2MASS

    def position_at(self, mjd: float) -> tuple[float, float]:
        if self.pm_ra is None or self.pm_dec is None:
            return self.ra, self.dec
        return sp.position_at(self.ra, self.dec, self.pm_ra * 1e3, self.pm_dec * 1e3,
                              _jyear(self.epoch_mjd), mjd)

    def sed(self) -> tuple[tuple[float, float], ...]:
        if self.j is None and self.h is None and self.ks is None and self.g is not None:
            return sp.sed_from_magnitudes(G=self.g, Ks=ks_from_gaia(self.g, self.bp_rp))
        return sp.sed_from_magnitudes(G=self.g, J=self.j, H=self.h, Ks=self.ks)


def separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Small-angle separation (fine below a degree)."""
    dra = ((ra1 - ra2 + 180.0) % 360.0 - 180.0) * math.cos(math.radians(0.5 * (dec1 + dec2)))
    return math.hypot(dra, dec1 - dec2) * 3600.0


def merge_catalogs(gaia: list[CatalogStar], tmass: list[CatalogStar]) -> list[CatalogStar]:
    """One entry per star: a 2MASS source within MATCH_ARCSEC of a Gaia star (moved to the
    2MASS epoch with Gaia's proper motion) lends the Gaia star its J, H, Ks."""
    used: set[int] = set()
    out = []
    for gs in gaia:
        best, best_sep = None, MATCH_ARCSEC
        for i, ts in enumerate(tmass):
            if i in used or ts.tmass_mjd is None or abs(ts.dec - gs.dec) > 0.1:  # 0.1 deg: > 20"/yr over 16 yr
                continue
            ra, dec = gs.position_at(ts.tmass_mjd)
            s = separation_arcsec(ra, dec, ts.ra, ts.dec)
            if s < best_sep:
                best, best_sep = i, s
        if best is None:
            out.append(gs)
        else:
            used.add(best)
            ts = tmass[best]
            out.append(CatalogStar(gs.name, gs.ra, gs.dec, gs.epoch_mjd, gs.pm_ra, gs.pm_dec, gs.g, gs.bp_rp,
                                   ts.j, ts.h, ts.ks, ts.tmass_mjd))
    out.extend(ts for i, ts in enumerate(tmass) if i not in used)
    return out


def effective_separation(separations_px: list[float], fwhm_px: float = 1.3) -> float:
    """One separation standing for a neighbour whose distance changes between exposures (a fast
    mover passing a star): the separation at which a neighbour adds the mean of its shares."""
    seps = [float(s) for s in separations_px]
    if len(seps) == 1:
        return seps[0]
    target = float(np.mean([sp.neighbour_share(s, fwhm_px) for s in seps]))
    lo, hi = min(seps), max(seps)
    for _ in range(30):  # the share falls monotonically with separation
        mid = 0.5 * (lo + hi)
        if sp.neighbour_share(mid, fwhm_px) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def neighbours_of(
    ra: float, dec: float, epoch_mjd: float, stars: list[CatalogStar], mjd,
    pm_ra: float = 0.0, pm_dec: float = 0.0, pm_err: float = 0.0,
) -> list[sp.Neighbour]:
    """Catalogued stars near an object during its SPHEREx exposures at `mjd` (one MJD or several).

    The object is at (ra, dec) at `epoch_mjd`, moving at (pm_ra, pm_dec) +- pm_err arcsec/yr.
    Its own catalogue entry, if any, is left out: the nearest entry within SELF_ARCSEC (grown
    by 3 x pm_err x the years between the epochs, at most SELF_MAX_ARCSEC) of where the
    object was at that entry's epoch. With several MJDs, each neighbour gets its
    `effective_separation` over them.
    """
    mjds = [float(m) for m in np.atleast_1d(mjd)]
    # A few epochs are plenty to follow a straight path.
    if len(mjds) > 9:
        mjds = [float(m) for m in np.quantile(mjds, np.linspace(0, 1, 9))]
    def obj_at(t: float) -> tuple[float, float]:
        return sp.position_at(ra, dec, pm_ra * 1e3, pm_dec * 1e3, _jyear(epoch_mjd), t)

    own, own_sep = None, math.inf
    for i, s in enumerate(stars):
        epoch = s.epoch_mjd
        radius = min(SELF_MAX_ARCSEC, SELF_ARCSEC + 3 * pm_err * abs(epoch - epoch_mjd) / 365.25)
        sep = separation_arcsec(*obj_at(epoch), s.ra, s.dec)
        if sep <= radius and sep < own_sep:
            own, own_sep = i, sep
    obj = [obj_at(m) for m in mjds]
    out = []
    for i, s in enumerate(stars):
        if i == own:
            continue
        seps = [separation_arcsec(*o, *s.position_at(m)) / PIXEL_SCALE_ARCSEC for o, m in zip(obj, mjds)]
        if min(seps) > sp.NEIGHBOUR_SEARCH_PX:
            continue
        sed = s.sed()
        if sed:
            out.append(sp.Neighbour(s.name, effective_separation(seps), sed))
    return out


# --- brightness of saturated stars (light curves) ----------------------------------------

WING_ANNULUS_PX = (7.0, 11.0)  # outside R Draconis's saturated core (~5.5 px radius at its brightest)
WING_BACKGROUND_PX = 3.0  # background ring width outside the annulus
WING_MIN_USABLE = 0.6
# The motion engine's choice for the same job (engine/frames.py BAD_FLAG_NAMES): the star's own
# HALO / SUR_ERROR / NONLINEAR flags in its wings mark its own light and must not be thrown away.
WING_REJECT_FLAGS = ("OVERFLOW", "BLOOM", "NONFUNC", "MISSING_DATA", "HOT", "COLD")


@dataclass(frozen=True)
class WingFlux:
    detector: int | None
    mjd: float
    wavelength_um: float
    flux: float  # mJy in the annulus (a fixed fraction of the total at one detector position/wavelength)
    flux_err: float


def wing_flux(hdul: fits.HDUList, ra: float, dec: float, annulus_px=WING_ANNULUS_PX) -> WingFlux | None:
    """A saturated star's brightness from its PSF wings: the background-subtracted flux in an
    annulus outside the saturated core (flagged pixels left out and the sum scaled up to the
    whole ring, as the motion engine does). Only comparable between exposures at the same
    wavelength and detector. None when the ring is cut by the edge or mostly flagged."""
    header = hdul["IMAGE"].header
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        wcs = WCS(header)
        x, y = (float(v) for v in wcs.world_to_pixel_values(ra, dec))
        wave = float(WCS(without_sip(header), fobj=hdul, key="W").all_pix2world(x, y, 0)[0])
    data = np.asarray(hdul["IMAGE"].data, dtype=np.float64)
    ny, nx = data.shape
    r_in, r_out = annulus_px
    edge = r_out + WING_BACKGROUND_PX
    if not (np.isfinite(x) and np.isfinite(y) and np.isfinite(wave)) or min(x, y) < edge or x > nx - 1 - edge or y > ny - 1 - edge:
        return None
    flags = np.asarray(hdul["FLAGS"].data)
    variance = np.asarray(hdul["VARIANCE"].data, dtype=np.float64)
    bad = ~np.isfinite(data) | ~np.isfinite(variance) | ((flags & flag_mask(WING_REJECT_FLAGS)) != 0)
    yy, xx = np.indices(data.shape)
    r = np.hypot(xx - x, yy - y)
    ring = (r >= r_in) & (r < r_out)
    usable = ring & ~bad
    outer = (r >= r_out) & (r < edge) & ~bad
    if usable.sum() < WING_MIN_USABLE * ring.sum() or outer.sum() < 8:
        return None
    scale = ring.sum() / usable.sum()
    bkg = float(np.median(data[outer]))
    to_mjy = proj_plane_pixel_area(wcs.celestial) * (math.pi / 180) ** 2 * sp.MJY_PER_MJY
    flux = float((data[usable] - bkg).sum()) * scale * to_mjy
    err = math.sqrt(float(variance[usable].sum())) * scale * to_mjy
    det = header.get("DETECTOR")
    return WingFlux(int(det) if det is not None else None, float(header["MJD-AVG"]), wave, flux, err)


# --- periods -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodFit:
    period_days: float | None
    period_err_days: float | None  # 1 sigma from the chi^2 curve (rescaled by the reduced chi^2)
    amplitude_mag: dict[int, float]  # per amplitude group: peak-to-peak, magnitudes
    chi2_constant: float  # no variation, offsets only
    chi2_best: float
    dof: int
    periods: np.ndarray  # the trial grid
    chi2: np.ndarray  # chi^2 at each trial period
    offsets: dict[int, float]  # per offset group: fitted mean ln flux
    phase_ref_mjd: float
    constrained: bool  # the 1 sigma interval is closed inside the grid
    notes: list[str]


def _design(t: np.ndarray, groups: np.ndarray, amp_groups: np.ndarray, period: float | None):
    g_ids = np.unique(groups)
    a_ids = np.unique(amp_groups)
    cols = [(groups == g).astype(float) for g in g_ids]
    if period is not None:
        phase = 2 * np.pi * t / period
        for a in a_ids:
            m = (amp_groups == a).astype(float)
            cols += [m * np.cos(phase), m * np.sin(phase)]
    return np.column_stack(cols), g_ids, a_ids


def _solve(A: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, float]:
    sw = np.sqrt(w)
    coef, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    return coef, float((w * (y - A @ coef) ** 2).sum())


def fit_period(
    mjd, log_flux, log_err, groups, amp_groups, periods=None,
) -> PeriodFit:
    """Sinusoidal period search on ln(flux) measured in many bands.

    Model: ln F_i = c[group_i] + a[amp_i] cos(2 pi t / P) + b[amp_i] sin(2 pi t / P): one free
    level per band (a Mira's flux differs hugely between bands), one amplitude per amplitude
    group (e.g. detector), a common period and phase. A band seen at one epoch only fixes its
    own level and says nothing about the period, as it should. Linear at each trial P.
    """
    t = np.asarray(mjd, float)
    y = np.asarray(log_flux, float)
    w = 1 / np.asarray(log_err, float) ** 2
    groups = np.asarray(groups)
    amp_groups = np.asarray(amp_groups)
    t0 = float(np.round(t.min()))
    t = t - t0
    if periods is None:
        periods = np.geomspace(50.0, 2000.0, 4000)
    periods = np.asarray(periods, float)
    A0, g_ids, _ = _design(t, groups, amp_groups, None)
    _, chi2_const = _solve(A0, y, w)
    chi2 = np.array([_solve(_design(t, groups, amp_groups, p)[0], y, w)[1] for p in periods])
    k = int(np.argmin(chi2))
    A, g_ids, a_ids = _design(t, groups, amp_groups, periods[k])
    coef, chi2_best = _solve(A, y, w)
    dof = len(y) - A.shape[1]
    notes = []
    s2 = max(1.0, chi2_best / dof) if dof > 0 else 1.0
    inside = chi2 <= chi2_best + s2
    # The connected stretch of the grid around the minimum.
    lo = k
    while lo > 0 and inside[lo - 1]:
        lo -= 1
    hi = k
    while hi < len(periods) - 1 and inside[hi + 1]:
        hi += 1
    constrained = lo > 0 and hi < len(periods) - 1
    err = float((periods[hi] - periods[lo]) / 2) if constrained else None
    n_g = len(g_ids)
    amps = {}
    for j, a in enumerate(a_ids):
        ca, sa = coef[n_g + 2 * j], coef[n_g + 2 * j + 1]
        amps[int(a)] = float(2 * math.hypot(ca, sa) * 2.5 / math.log(10))  # ln -> mag, peak to peak
    baseline = float(t.max() - t.min())
    if baseline < 2 * periods[k]:
        notes.append(
            f"The data span {baseline:.0f} days, {baseline / periods[k]:.1f} cycles of the best period: "
            "a period from fewer than two cycles is only loosely constrained."
        )
    return PeriodFit(
        period_days=float(periods[k]), period_err_days=err, amplitude_mag=amps,
        chi2_constant=float(chi2_const), chi2_best=float(chi2_best), dof=int(dof),
        periods=periods, chi2=chi2, offsets={g.item(): float(c) for g, c in zip(g_ids, coef[:n_g])},
        phase_ref_mjd=t0, constrained=constrained, notes=notes,
    )


def fit_period_robust(
    mjd, log_flux, log_err, groups, amp_groups, periods=None, clip: float = 4.0, iterations: int = 5,
) -> tuple[PeriodFit, np.ndarray]:
    """`fit_period` with outliers removed: points whose residual exceeds `clip` x the robust
    (MAD) scatter of the residuals are dropped and the period searched again, until nothing
    changes. Returns the fit and the mask of points kept."""
    mjd, y, e = (np.asarray(v, float) for v in (mjd, log_flux, log_err))
    groups, amp_groups = np.asarray(groups), np.asarray(amp_groups)
    keep = np.ones(len(y), bool)
    fit = None
    for _ in range(iterations):
        fit = fit_period(mjd[keep], y[keep], e[keep], groups[keep], amp_groups[keep], periods)
        t = mjd - fit.phase_ref_mjd
        A, g_ids, _ = _design(t[keep], groups[keep], amp_groups[keep], fit.period_days)
        coef, _ = _solve(A, y[keep], 1 / e[keep] ** 2)
        resid = np.full(len(y), np.nan)
        resid[keep] = y[keep] - A @ coef
        # Points outside the kept set: residual against their band's level, where it is known.
        level = dict(zip(g_ids.tolist(), coef[: len(g_ids)]))
        n_g = len(g_ids)
        a_ids = np.unique(amp_groups[keep]).tolist()
        phase = 2 * np.pi * t / fit.period_days
        for i in np.flatnonzero(~keep):
            if groups[i].item() in level and amp_groups[i].item() in a_ids:
                j = a_ids.index(amp_groups[i].item())
                model = level[groups[i].item()] + coef[n_g + 2 * j] * np.cos(phase[i]) + coef[n_g + 2 * j + 1] * np.sin(phase[i])
                resid[i] = y[i] - model
        scatter = 1.4826 * np.nanmedian(np.abs(resid[keep] - np.nanmedian(resid[keep])))
        new = np.isfinite(resid) & (np.abs(resid) <= clip * max(scatter, 1e-6))
        if np.array_equal(new, keep):
            break
        keep = new
    return fit, keep


# --- catalogue queries (network) -----------------------------------------------------------
#
# Both catalogues come from IRSA Gator (one cone each, ~1-2 s). ESA's Gaia TAP did not
# answer at all on 2026-11-14 evening; IRSA's copy (gaia_dr3_source) has the same columns.

TMASS_COLUMNS = "designation,ra,dec,j_m,h_m,k_m,ph_qual,cc_flg,jdate"
GAIA_COLUMNS = "source_id,ra,dec,ref_epoch,pmra,pmdec,phot_g_mean_mag,bp_rp"
MAX_CONE_ARCSEC = 1200.0
MAX_ROWS = 50_000


def _check_cone(ra: float, dec: float, radius_arcsec: float) -> None:
    if not (math.isfinite(ra) and math.isfinite(dec) and 0 <= ra < 360 and -90 <= dec <= 90):
        raise ValueError(f"invalid position ra={ra}, dec={dec}")
    if not (math.isfinite(radius_arcsec) and 0 < radius_arcsec <= MAX_CONE_ARCSEC):
        raise ValueError(f"radius must be in (0, {MAX_CONE_ARCSEC:g}] arcsec, got {radius_arcsec}")


def _gator(catalog: str, columns: str, ra: float, dec: float, radius_arcsec: float, constraints: str = ""):
    """One Gator cone as an astropy Table. Raises ArchiveError (network, 5xx, Gator error, cut-off list)."""
    from astropy.io import ascii as astropy_ascii

    from planetx.observatories import catalogs as gator

    _check_cone(ra, dec, radius_arcsec)
    params = {
        "catalog": catalog, "spatial": "cone", "objstr": f"{ra:.7f} {dec:+.7f}",
        "radius": f"{radius_arcsec:.2f}", "radunits": "arcsec", "outfmt": "1",
        "selcols": columns, "outrows": str(MAX_ROWS),
    }
    if constraints:
        params["constraints"] = constraints
    text = gator._get(params, timeout=90.0, retries=2)
    if text.lstrip().startswith("[struct"):
        raise gator.ArchiveError(f"Gator error for {catalog}: {text.strip()[:300]}")
    try:
        table = astropy_ascii.read(text, format="ipac")
    except Exception as exc:
        raise gator.ArchiveError(f"unreadable Gator response for {catalog}: {exc}") from exc
    if len(table) >= MAX_ROWS:
        raise gator.ArchiveError(f"{catalog} cone of {radius_arcsec:.0f}\" is cut off at {MAX_ROWS} rows")
    return table


def _value(v) -> float | None:
    if np.ma.is_masked(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_2mass(table) -> list[CatalogStar]:
    """2MASS PSC rows -> stars. A band's magnitude is kept when its photometric quality is
    A-D and it is not flagged as an artifact (cc_flg 0 or c), as planetx.observatories does."""
    out = []
    for row in table:
        qual, cc = str(row["ph_qual"]), str(row["cc_flg"])
        mags = []
        for k, col in enumerate(("j_m", "h_m", "k_m")):
            ok = k < len(qual) and qual[k] in "ABCD" and k < len(cc) and cc[k] in "0c"
            mags.append(_value(row[col]) if ok else None)
        ra, dec, jd = _value(row["ra"]), _value(row["dec"]), _value(row["jdate"])
        if all(m is None for m in mags) or ra is None or dec is None or jd is None:
            continue
        mjd = jd - 2400000.5
        out.append(CatalogStar(f"2MASS J{row['designation']}", ra, dec, mjd,
                               j=mags[0], h=mags[1], ks=mags[2], tmass_mjd=mjd))
    return out


def parse_gaia(table) -> list[CatalogStar]:
    """Gaia DR3 rows -> stars (proper motions mas/yr -> arcsec/yr)."""
    out = []
    for row in table:
        ra, dec = _value(row["ra"]), _value(row["dec"])
        if ra is None or dec is None:
            continue
        epoch = _value(row["ref_epoch"]) or 2016.0
        pmra, pmdec = _value(row["pmra"]), _value(row["pmdec"])
        has_pm = pmra is not None and pmdec is not None
        out.append(CatalogStar(
            f"Gaia DR3 {row['source_id']}", ra, dec, MJD_J2000 + (epoch - 2000.0) * 365.25,
            pmra / 1e3 if has_pm else None, pmdec / 1e3 if has_pm else None,
            g=_value(row["phot_g_mean_mag"]), bp_rp=_value(row["bp_rp"]),
        ))
    return out


def fetch_2mass(ra: float, dec: float, radius_arcsec: float) -> list[CatalogStar]:
    """2MASS Point Source Catalog stars in a cone (known minor planets left out)."""
    table = _gator("fp_psc", TMASS_COLUMNS, ra, dec, radius_arcsec, constraints="mp_flg=0")
    return parse_2mass(table) if len(table) else []


def fetch_gaia(ra: float, dec: float, radius_arcsec: float) -> list[CatalogStar]:
    """Gaia DR3 stars in a cone, with G and BP-RP."""
    table = _gator("gaia_dr3_source", GAIA_COLUMNS, ra, dec, radius_arcsec)
    return parse_gaia(table) if len(table) else []


def fetch_field_stars(ra: float, dec: float, radius_arcsec: float) -> list[CatalogStar]:
    """Gaia DR3 and 2MASS stars in a cone, merged (`merge_catalogs`)."""
    return merge_catalogs(fetch_gaia(ra, dec, radius_arcsec), fetch_2mass(ra, dec, radius_arcsec))
