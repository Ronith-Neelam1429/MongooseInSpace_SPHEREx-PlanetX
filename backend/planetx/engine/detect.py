"""Point-source detection and sub-pixel centroids in a single frame."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy.stats import sigma_clipped_stats
from scipy import ndimage

from .frames import Frame

# SPHEREx PSFs are ~1.5 px FWHM, so a 3x3 box holds most of a point source's
# flux and keeps neighbours out in crowded fields.
_HALF = 1
# Positional error can't beat this however bright the star (undersampled PSF,
# residual distortion); keeps the motion fit from trusting one frame too much.
CENTROID_FLOOR_PX = 0.05
# Saturated stars are located from the shape of their masked patch, which
# bleeding/blooming can skew.
SATURATED_SIGMA_PX = 0.5
SATURATED_MIN_PIXELS = 3
# Annuli (inner, outer radius in px) for saturated stars' brightness proxy.
# The outer one serves the brightest stars, whose saturated patch swallows
# the inner one (R Draconis: ~90-100 saturated px, radius ~5.5 px).
WING_ANNULI_PX = ((3.0, 6.0), (7.0, 11.0))


@dataclass
class Detection:
    frame_index: int
    x: float  # pixel coords (0-based) in the frame
    y: float
    ra: float  # degrees
    dec: float
    sigma_arcsec: float  # 1-sigma positional error
    flux: float  # background-subtracted 3x3 sum (MJy/sr x px)
    flux_err: float
    snr: float
    mjd: float
    detector: int
    saturated: bool = False  # position from the saturated patch; flux is NaN
    wavelength_um: float = float("nan")  # frame's central wavelength
    wing_flux: float = float("nan")  # saturated stars only: annulus flux proxy
    wing_flux_err: float = float("nan")
    wing2_flux: float = float("nan")  # same, wider annulus
    wing2_flux_err: float = float("nan")
    sat_area: int = 0  # saturated stars only: pixels in the saturated patch
    contaminated: bool = False  # ghost/halo/crosstalk light: skip for photometry


def frame_background(frame: Frame) -> tuple[float, float]:
    """Sigma-clipped (median, std) of the frame's usable pixels."""
    good = frame.data[np.isfinite(frame.data)]
    if good.size < 25:
        raise ValueError("frame has too few usable pixels to estimate a background")
    _, median, std = sigma_clipped_stats(good, sigma=3.0, maxiters=10)
    return float(median), float(std)


def detect_sources(frame: Frame, frame_index: int = 0, nsigma: float = 5.0) -> list[Detection]:
    """Find point sources above `nsigma` and measure flux-weighted centroids.

    Noise comes from the VARIANCE plane where available (falls back to the
    clipped scatter), so bright, noisy regions don't produce false peaks.
    """
    data = frame.data
    bkg, std = frame_background(frame)
    noise = np.sqrt(frame.variance)
    noise = np.where(np.isfinite(noise), noise, std)
    noise = np.maximum(noise, std * 0.5)  # variance can be optimistic in faint pixels

    resid = data - bkg
    signif = np.where(np.isfinite(resid), resid / noise, 0.0)
    above = signif > nsigma
    if not above.any():
        return _saturated_sources(frame, frame_index, frame.pixel_scale_arcsec, resid, noise)

    pix_scale = frame.pixel_scale_arcsec
    ny, nx = data.shape
    fwhm_px = frame.psf_fwhm_arcsec / pix_scale if np.isfinite(frame.psf_fwhm_arcsec) else 1.5
    out = _saturated_sources(frame, frame_index, pix_scale, resid, noise)
    # Unsaturated detections in the halo of a saturated star are fragments of it.
    near_sat = (
        ndimage.binary_dilation(frame.saturated, iterations=2)
        if frame.saturated is not None and frame.saturated.any()
        else np.zeros(data.shape, bool)
    )

    # Every local maximum above threshold is a candidate source. (One per
    # connected blob would merge neighbours whose wings touch, which bright
    # stars' wings do over tens of pixels.)
    filled = np.where(np.isfinite(resid), resid, -np.inf)
    is_peak = above & (filled == ndimage.maximum_filter(filled, size=3, mode="constant", cval=-np.inf))
    # A lone pixel above threshold is a hot pixel or cosmic ray, not a PSF.
    neighbours = ndimage.convolve(above.astype(int), np.ones((3, 3), int), mode="constant") - above
    for py, px in zip(*np.nonzero(is_peak & (neighbours >= 1))):
        if near_sat[py, px]:
            continue
        y0, y1, x0, x1 = py - _HALF, py + _HALF + 1, px - _HALF, px + _HALF + 1
        if y0 < 0 or x0 < 0 or y1 > ny or x1 > nx:
            continue  # centroid box would be truncated by the cutout edge
        local = _local_background(resid, px, py)
        box = resid[y0:y1, x0:x1] - local
        var = noise[y0:y1, x0:x1] ** 2
        if not np.all(np.isfinite(box)):
            continue  # bad pixel in the core: centroid would be biased
        w = np.clip(box, 0, None)
        total = w.sum()
        if total <= 0:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        cx = float((w * xx).sum() / total)
        cy = float((w * yy).sum() / total)
        flux = float(box.sum())
        flux_err = float(np.sqrt(var.sum()))
        snr = flux / flux_err if flux_err > 0 else 0.0
        if snr < nsigma:
            continue
        # FWHM/SNR rather than the textbook FWHM/(2.355 SNR): SPHEREx PSFs are
        # undersampled and a 3x3 centroid is noisier than the ideal. Checked on
        # WISE 0855 stacks (SNR ~10), where pass-to-pass scatter is ~1".
        sigma_px = max(fwhm_px / snr, CENTROID_FLOOR_PX)
        contaminated = bool(
            frame.contaminated is not None and frame.contaminated[y0:y1, x0:x1].any()
        )
        ra, dec = frame.wcs.all_pix2world(cx, cy, 0)
        out.append(
            Detection(
                frame_index=frame_index,
                x=cx,
                y=cy,
                ra=float(ra),
                dec=float(dec),
                sigma_arcsec=sigma_px * pix_scale,
                flux=flux,
                flux_err=flux_err,
                snr=snr,
                mjd=frame.mjd,
                detector=frame.detector,
                wavelength_um=frame.wavelength_um,
                contaminated=contaminated,
            )
        )
    return out


def _local_background(resid: np.ndarray, px: int, py: int) -> float:
    """Clipped median of an annulus (3-6 px) around a source.

    The global background is subtracted already; this removes what's left
    locally, e.g. the extended glow of a nearby bright star, which would
    otherwise change apparent brightness whenever that star varies.
    """
    ny, nx = resid.shape
    y0, y1, x0, x1 = max(py - 6, 0), min(py + 7, ny), max(px - 6, 0), min(px + 7, nx)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    r = np.hypot(xx - px, yy - py)
    vals = resid[y0:y1, x0:x1][(r >= 3) & (r < 6)]
    vals = vals[np.isfinite(vals)]
    if vals.size < 8:
        return 0.0
    _, median, _ = sigma_clipped_stats(vals, sigma=2.5, maxiters=5)
    return float(median)


def _saturated_sources(
    frame: Frame, frame_index: int, pix_scale: float, resid: np.ndarray, noise: np.ndarray
) -> list[Detection]:
    """One detection per patch of saturated pixels, at the patch's centre.

    The core pixels are unusable, but the overflow/bloom patch is centred on
    the star, so its geometric centre is a usable (cruder) position. Brightness
    comes from the PSF wings in a fixed annulus (`wing_flux`): proportional to
    the total flux for a given detector/wavelength, so it can be compared
    between passes (with other wing fluxes only, never with core fluxes).
    """
    if frame.saturated is None or not frame.saturated.any():
        return []
    labels, n = ndimage.label(frame.saturated, structure=np.ones((3, 3)))
    ny, nx = frame.saturated.shape
    out: list[Detection] = []
    for k, sl in enumerate(ndimage.find_objects(labels), start=1):
        ys, xs = np.nonzero(labels[sl] == k)
        if len(ys) < SATURATED_MIN_PIXELS:
            continue
        ys = ys + sl[0].start
        xs = xs + sl[1].start
        if ys.min() == 0 or xs.min() == 0 or ys.max() == ny - 1 or xs.max() == nx - 1:
            continue  # patch cut by the cutout edge: centre would be biased
        cx, cy = float(xs.mean()), float(ys.mean())
        ra, dec = frame.wcs.all_pix2world(cx, cy, 0)
        yy, xx = np.mgrid[0:ny, 0:nx]
        r = np.hypot(xx - cx, yy - cy)
        wings = []
        for r_in, r_out in WING_ANNULI_PX:
            ring = (r >= r_in) & (r < r_out)
            usable = ring & np.isfinite(resid)
            # Need most of the ring: the saturated patch (brighter star) or
            # the cutout edge eating it would bias the scaled sum.
            if ring.sum() == 0 or usable.sum() < 0.6 * ring.sum():
                wings.append((float("nan"), float("nan")))
                continue
            scale = ring.sum() / usable.sum()
            # Background from just outside the ring, so another bright star's
            # glow under this one doesn't masquerade as a brightness change.
            outer = (r >= r_out) & (r < r_out + 3) & np.isfinite(resid)
            local = float(np.median(resid[outer])) if outer.sum() >= 8 else 0.0
            wings.append((
                float((resid[usable] - local).sum() * scale),
                float(np.sqrt((noise[usable] ** 2).sum()) * scale),
            ))
        out.append(
            Detection(
                frame_index=frame_index,
                x=cx,
                y=cy,
                ra=float(ra),
                dec=float(dec),
                sigma_arcsec=SATURATED_SIGMA_PX * pix_scale,
                flux=float("nan"),
                flux_err=float("nan"),
                # Rank above every unsaturated source when seeding clusters.
                snr=1e4 + len(ys),
                mjd=frame.mjd,
                detector=frame.detector,
                wavelength_um=frame.wavelength_um,
                saturated=True,
                wing_flux=wings[0][0],
                wing_flux_err=wings[0][1],
                wing2_flux=wings[1][0],
                wing2_flux_err=wings[1][1],
                sat_area=int(len(ys)),
                # A saturated star's own halo/ghost flags surround it, so
                # contamination flags can't be told apart from its own light.
                contaminated=False,
            )
        )
    return out
