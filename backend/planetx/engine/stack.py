"""Co-add each pass's frames, per detector, into deeper images.

Faint objects (e.g. the brown dwarf WISE 0855) sit below the single-frame
detection limit but appear once a pass's frames are added. Frames are first
shifted by the star-matched alignment offsets, then resampled onto one shared
tangent-plane grid and combined with inverse-variance weights.

Each detector covers its own wavelength range (0.75-5 um over the six), so
frames are co-added per detector: adding a red object's 4-5 um frames to
1-2 um frames where it is invisible would only add noise.
"""
from __future__ import annotations

import warnings

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
from reproject import reproject_interp

from .detect import frame_background
from .frames import Frame


def shifted_wcs(wcs: WCS, east_arcsec: float, north_arcsec: float) -> WCS:
    """WCS whose sky positions are moved by (east, north) arcsec."""
    out = wcs.deepcopy()
    ra, dec = out.wcs.crval
    c = SkyCoord(ra * u.deg, dec * u.deg).spherical_offsets_by(east_arcsec * u.arcsec, north_arcsec * u.arcsec)
    out.wcs.crval = [c.ra.deg, c.dec.deg]
    return out


def stack_grid(center: SkyCoord, n: int, scale_arcsec: float) -> WCS:
    """North-up TAN grid of n x n pixels centred on `center`."""
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [center.ra.deg, center.dec.deg]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    w.wcs.cdelt = [-scale_arcsec / 3600, scale_arcsec / 3600]
    return w


def stack_pass(
    frames: list[Frame],
    offsets: list[tuple[float, float]],
    grid: WCS,
    shape: tuple[int, int],
) -> Frame:
    """Inverse-variance co-add of one pass's frames, as a Frame the detector can use.

    Each frame is background-subtracted first. The frames should come from one
    detector; they still span that detector's wavelengths, so the stack's
    `wavelength_um` is their weighted mean and its flux is that of a band
    average (comparable with stacks of the same detector and mean wavelength).
    """
    num = np.zeros(shape)
    den = np.zeros(shape)
    sat_votes = np.zeros(shape)
    cover = np.zeros(shape)
    for f, (dx, dy) in zip(frames, offsets):
        wcs = shifted_wcs(f.wcs, dx, dy)
        bkg, std = frame_background(f)
        var = np.where(np.isfinite(f.variance), f.variance, std**2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d, fp = reproject_interp((f.data - bkg, wcs), grid, shape_out=shape)
            v, _ = reproject_interp((var, wcs), grid, shape_out=shape)
            if f.saturated is not None:
                s, _ = reproject_interp((f.saturated.astype(float), wcs), grid, shape_out=shape)
            else:
                s = np.zeros(shape)
        observed = fp > 0
        good = observed & np.isfinite(d) & np.isfinite(v) & (v > 0)
        num[good] += d[good] / v[good]
        den[good] += 1 / v[good]
        cover += observed
        sat_votes += np.where(observed, np.nan_to_num(s) > 0.5, 0)

    with np.errstate(invalid="ignore", divide="ignore"):
        data = np.where(den > 0, num / den, np.nan)
        variance = np.where(den > 0, 1 / den, np.nan)
    # A pixel is saturated in the stack if most frames saw it saturated.
    saturated = (cover > 0) & (sat_votes >= 0.5 * cover)
    data[saturated] = np.nan
    variance[saturated] = np.nan
    weights = np.array([1 / np.nanmedian(np.where(np.isfinite(f.variance), f.variance, np.nan)) for f in frames])
    weights = np.where(np.isfinite(weights), weights, 0) + 1e-30
    mjd = float(np.average([f.mjd for f in frames], weights=weights))
    waves = np.array([f.wavelength_um for f in frames])
    known = np.isfinite(waves)
    wavelength = float(np.average(waves[known], weights=weights[known])) if known.any() else float("nan")
    detectors = sorted({f.detector for f in frames})
    return Frame(
        data=data,
        variance=variance,
        wcs=grid,
        mjd=mjd,
        # 0 = mixed detectors; per-detector flux comparisons don't apply.
        detector=detectors[0] if len(detectors) == 1 else 0,
        obs_id=f"stack:{len(frames)} frames",
        wavelength_um=wavelength,
        psf_fwhm_arcsec=float(np.nanmedian([f.psf_fwhm_arcsec for f in frames])),
        saturated=saturated,
        meta={
            "stacked": True,
            "n_frames": len(frames),
            "detectors": detectors,
            "mjd_min": min(f.mjd for f in frames),
            "mjd_max": max(f.mjd for f in frames),
            "wavelength_min_um": float(waves[known].min()) if known.any() else float("nan"),
            "wavelength_max_um": float(waves[known].max()) if known.any() else float("nan"),
        },
    )
