"""Evidence for one tracked object: the same patch of sky in every survey pass, and
the object's measured positions on it.

Built from the engine data of a finished run (`planetx.api.motion.EngineRun`), which
is in memory: no network, a fraction of a second per object. The images are meant to
let a reader check the engine's verdict by eye, so they are rendered plainly:

  * One image per pass: the median of that pass's exposures. SPHEREx takes each
    exposure at a different wavelength, so every exposure is first put in units of
    its own noise (as in tools/devview.py) and moved by the engine's alignment shift.
  * Nearest-neighbour resampling onto a grid of half SPHEREx pixels: every image
    pixel is a median of measured pixel values, never an interpolated one. The
    exposures are rotated and offset against each other, which is what gives the
    median its half-pixel detail.
  * One grey scale for all passes of an object.
"""
from __future__ import annotations

import base64
import dataclasses
import io
import threading
import warnings
import weakref
from collections import OrderedDict

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from PIL import Image
from reproject import reproject_interp

from planetx.api.evidence_checks import run_checks, saturated_core_px
from planetx.api.motion import EngineRun
from planetx.api.schemas import (
    EvidenceExposure,
    EvidenceFit,
    EvidenceFlux,
    EvidencePass,
    EvidencePoint,
    FieldScatter,
    MotionEvidence,
)
from planetx.engine import EngineResult, Frame, TrackedObject
from planetx.spherex import archive, cutouts

PATCH_PIXEL_ARCSEC = cutouts.PIXEL_SCALE_ARCSEC / 2
# Wide enough to show neighbouring stars standing still next to a saturated star's
# ~50" core (Barnard's Star), small enough that a one-pixel hop is easy to see.
MIN_PATCH_ARCSEC = 150.0
MAX_PATCH_ARCSEC = 300.0
# Sky kept beyond the farthest measured position, so a long track stays inside the patch.
TRACK_MARGIN_ARCSEC = 45.0
MIN_USABLE_PIXELS = 25  # fewer cannot give a background (the engine's own limit)
# Black and white levels, in units of the patch's noise around its background. With the
# asinh stretch of `cutouts.render_png` the noise stays visible and a 10-sigma star is mid-grey.
BLACK_SIGMA = 2.0
WHITE_SIGMA = 80.0
# The difference image reaches black or white at this fraction of the black-to-white
# range: a 20-sigma star that moved gives a clear pair, while the rings fixed stars
# leave behind (each pass samples other wavelengths) stay faint.
DIFFERENCE_FULL_SCALE = 0.5
MIN_FIELD_STARS = 5  # fewer shared objects give no usable scatter
# Single exposures shown per pass, spread over its time: enough to see the object in each
# one, few enough to keep the answer small (each image is about 2 kB).
EXPOSURES_PER_PASS = 6
# Side of the single-exposure images: the track plus a margin, at least this.
MIN_EXPOSURE_ARCSEC = 60.0
EXPOSURE_MARGIN_ARCSEC = 20.0
# Rendered evidence kept for repeat requests (about 20 kB each).
MAX_CACHED = 64

_cache: OrderedDict[tuple[int, int], tuple[weakref.ref[EngineRun], MotionEvidence]] = OrderedDict()
# One build at a time: they are CPU-bound, and a second request for the same object
# (two viewers, React's double effect in development) then finds the first one's result.
_lock = threading.Lock()


def evidence_for(run: EngineRun, object_id: int) -> MotionEvidence:
    """Evidence for `run.tracked[object_id]`, built once per run and object."""
    key = (id(run), object_id)
    with _lock:
        hit = _cache.get(key)
        # id() values are reused after a run is freed; the weak reference tells them apart
        # without keeping the run's ~15 MB of frames alive.
        if hit is not None and hit[0]() is run:
            _cache.move_to_end(key)
            return hit[1]
        evidence = build_evidence(run, object_id)
        _cache[key] = (weakref.ref(run), evidence)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)
        return evidence


def patch_wcs(ra: float, dec: float, n: int) -> WCS:
    """North-up TAN grid of n x n pixels centred on (ra, dec).

    Row 0 is the north edge and column 0 the east edge (as `cutouts.grid_wcs`), so
    arrays on this grid are already in image orientation.
    """
    scale = PATCH_PIXEL_ARCSEC / 3600
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra, dec]
    wcs.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    wcs.wcs.cdelt = [-scale, -scale]
    return wcs


def patch_offsets(grid: WCS, n: int, ra: float, dec: float) -> tuple[float, float]:
    """(east, north) of a sky position from the patch centre, arcsec.

    Measured on the patch's own pixel grid, so a mark drawn from these offsets
    lands on the image exactly.
    """
    x, y = grid.wcs_world2pix(ra, dec, 0)
    centre = (n - 1) / 2
    # 0.1 mas is far below any error here; "+ 0.0" turns -0.0 into 0.0.
    return (
        round(float(centre - x) * PATCH_PIXEL_ARCSEC, 4) + 0.0,
        round(float(centre - y) * PATCH_PIXEL_ARCSEC, 4) + 0.0,
    )


def _shifted(wcs: WCS, east_arcsec: float, north_arcsec: float) -> WCS:
    """A copy of `wcs` whose sky positions are moved by (east, north)."""
    out = wcs.deepcopy()
    ra, dec = out.wcs.crval
    moved = SkyCoord(ra * u.deg, dec * u.deg).spherical_offsets_by(east_arcsec * u.arcsec, north_arcsec * u.arcsec)
    out.wcs.crval = [moved.ra.deg, moved.dec.deg]
    return out


def _noise_units(frame: Frame) -> np.ndarray | None:
    """The frame with its background removed, in units of its own noise; None if unusable.

    Saturated pixels (NaN in the engine's frames) are set to the frame's brightest
    measured value: all that is known is that they are brighter than that.
    """
    finite = np.isfinite(frame.data)
    if finite.sum() < MIN_USABLE_PIXELS:
        return None
    _, median, std = sigma_clipped_stats(frame.data[finite], sigma=3.0, maxiters=10)
    if not std > 0:
        return None
    out = (frame.data - median) / std
    if frame.saturated is not None and frame.saturated.any():
        out[frame.saturated] = out[finite].max()
    return out


def pass_images(result: EngineResult, grid: WCS, n: int) -> tuple[list[np.ndarray], list[int]]:
    """One image per pass on `grid`, and how many frames went into each.

    A pixel no frame of the pass covers (field edge, masked detector pixels) is NaN.
    """
    images, counts = [], []
    for ids in result.passes:
        stack = []
        for i in ids:
            frame = result.frames[i]
            data = _noise_units(frame)
            if data is None:
                continue
            aligned = _shifted(frame.wcs, *result.frame_offsets_arcsec[i])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                stack.append(reproject_interp(
                    (data, aligned), grid, shape_out=(n, n), order="nearest-neighbor", return_footprint=False
                ))
        if stack:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # pixels that are NaN in every frame
                images.append(np.nanmedian(np.array(stack), axis=0))
        else:
            images.append(np.full((n, n), np.nan))
        counts.append(len(stack))
    return images, counts


def _fit_position(obj: TrackedObject, mjd: float) -> tuple[float, float]:
    """Where the engine's straight-line fit puts the object at `mjd`."""
    years = (mjd - obj.ref_mjd) / 365.25
    at = SkyCoord(obj.ra * u.deg, obj.dec * u.deg).spherical_offsets_by(
        obj.pm_ra_cosdec * years * u.arcsec, obj.pm_dec * years * u.arcsec
    )
    return float(at.ra.deg), float(at.dec.deg)


def _covers(frame: Frame, wcs: WCS, ra: float, dec: float, margin_px: float = 2.0) -> bool:
    """Whether the frame has data (or a saturated star) at a sky position."""
    x, y = wcs.all_world2pix(ra, dec, 0)
    ny, nx = frame.data.shape
    if not (margin_px <= x <= nx - 1 - margin_px and margin_px <= y <= ny - 1 - margin_px):
        return False
    iy, ix = round(float(y)), round(float(x))
    return bool(np.isfinite(frame.data[iy, ix]) or (frame.saturated is not None and frame.saturated[iy, ix]))


def _spread(ids: list[int], detectors: list[int], k: int = EXPOSURES_PER_PASS) -> list[int]:
    """Up to k of the time-ordered `ids`: the slots shared out over detectors in turn, each
    detector's picks spread evenly over its own exposures. Time-ordered."""
    by_det: dict[int, list[int]] = {}
    for i, det in zip(ids, detectors, strict=True):
        by_det.setdefault(det, []).append(i)
    order = sorted(by_det)
    quota = dict.fromkeys(order, 0)
    turn = 0
    while sum(quota.values()) < min(k, len(ids)):
        det = order[turn % len(order)]
        turn += 1
        if quota[det] < len(by_det[det]):
            quota[det] += 1
    picked = set()
    for det, own in by_det.items():
        if quota[det]:
            picked.update(own[j] for j in np.linspace(0, len(own) - 1, quota[det]).round().astype(int))
    return [i for i in ids if i in picked]


def pass_exposures(
    result: EngineResult, obj: TrackedObject, grid: WCS, m: int, lo: float, hi: float
) -> tuple[list[list[EvidenceExposure]], list[int]]:
    """Per pass: a few single exposures of the object's spot, and how many exposures cover it.

    The exposures are spread evenly in time over those that cover the object's position in
    that pass (measured, or where the fit puts it), whether or not they detected it, so the
    selection shows what the data holds rather than its best frames.
    """
    by_pass = {s.pass_index: s for s in obj.sources}
    exposures, covers = [], []
    for k, ids in enumerate(result.passes):
        source = by_pass.get(k)
        if source is not None:
            ra, dec = source.ra, source.dec
        else:
            ra, dec = _fit_position(obj, float(np.mean([result.frames[i].mjd for i in ids])))
        aligned = {i: _shifted(result.frames[i].wcs, *result.frame_offsets_arcsec[i]) for i in ids}
        covering = sorted((i for i in ids if _covers(result.frames[i], aligned[i], ra, dec)), key=lambda i: result.frames[i].mjd)
        covers.append(len(covering))
        if result.stacked or not covering:
            exposures.append([])
            continue
        detected = {d.frame_index: d for d in source.detections} if source is not None else {}
        row = []
        for i in _spread(covering, [result.frames[i].detector for i in covering]):
            frame = result.frames[i]
            data = _noise_units(frame)
            if data is None:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                image = reproject_interp(
                    (data, aligned[i]), grid, shape_out=(m, m), order="nearest-neighbor", return_footprint=False
                )
            point = None
            d = detected.get(i)
            if d is not None:
                east, north = patch_offsets(grid, m, d.ra, d.dec)
                point = EvidencePoint(
                    east_arcsec=east, north_arcsec=north, sigma_arcsec=d.sigma_arcsec,
                    mjd=d.mjd, date=archive.mjd_to_date(d.mjd), n_frames=1, fluxes=[],
                )
            row.append(EvidenceExposure(
                mjd=frame.mjd, time=Time(frame.mjd, format="mjd", scale="utc").iso[:16],
                detector=frame.detector,
                wavelength_um=float(frame.wavelength_um) if np.isfinite(frame.wavelength_um) else None,
                image_png_b64=_b64(cutouts.render_png(image, vmin=lo, vmax=hi)), point=point,
            ))
        exposures.append(row)
    return exposures, covers


def grey_limits(images: list[np.ndarray]) -> tuple[float, float]:
    """(black, white) levels shared by all passes, from the patch's background and noise."""
    pixels = np.concatenate([image[np.isfinite(image)] for image in images])
    if pixels.size == 0:
        return 0.0, 1.0
    _, median, std = sigma_clipped_stats(pixels, sigma=3.0, maxiters=10)
    if not std > 0:
        std = 1.0
    return float(median - BLACK_SIGMA * std), float(median + WHITE_SIGMA * std)


def difference_png(first: np.ndarray, last: np.ndarray, lo: float, hi: float) -> bytes:
    """`last - first` as a grey + alpha PNG, linear about mid-grey.

    No change is mid-grey; brighter in the last pass is lighter, fainter is darker,
    so a mover shows as a dark spot (where it was) next to a bright one (where it is).
    Both images are first cut off at the passes' black and white levels: whatever is
    white in both (the core of a bright or saturated star, where the values mean
    little) counts as unchanged. Pixels without data in either pass are transparent.
    """
    with np.errstate(invalid="ignore"):
        diff = np.clip(last, lo, hi) - np.clip(first, lo, hi)
        scaled = np.clip(diff / (DIFFERENCE_FULL_SCALE * (hi - lo)), -1, 1)
    finite = np.isfinite(diff)
    gray = np.where(finite, np.round(127.5 * (1 + scaled)), 0).astype(np.uint8)
    alpha = np.where(finite, 255, 0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(np.dstack([gray, alpha])).save(buf, format="PNG")
    return buf.getvalue()


def field_scatter(result: EngineResult, obj: TrackedObject, ref_pass: int) -> dict[int, FieldScatter]:
    """Per pass: how far the field's other objects appear to shift since `ref_pass`.

    Robust statistics, so the few real movers among them do not matter.
    """
    shifts: dict[int, list[tuple[float, float]]] = {}
    for other in result.objects:
        if other is obj:
            continue
        by_pass = {s.pass_index: s for s in other.sources}
        ref = by_pass.get(ref_pass)
        if ref is None:
            continue
        cos_dec = np.cos(np.radians(ref.dec))
        for k, s in by_pass.items():
            if k != ref_pass:
                d_ra = (s.ra - ref.ra + 180.0) % 360.0 - 180.0  # fields across RA 0
                shifts.setdefault(k, []).append((d_ra * cos_dec * 3600, (s.dec - ref.dec) * 3600))
    out = {}
    for k, pairs in shifts.items():
        if len(pairs) < MIN_FIELD_STARS:
            continue
        d = np.array(pairs)
        out[k] = FieldScatter(
            sigma_arcsec=float(1.4826 * np.median(np.abs(d))),
            median_shift_arcsec=float(np.median(np.hypot(d[:, 0], d[:, 1]))),
            n_stars=len(pairs),
        )
    return out


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


def build_evidence(run: EngineRun, object_id: int) -> MotionEvidence:
    result, obj = run.result, run.tracked[object_id]
    sources = sorted(obj.sources, key=lambda s: s.mjd)
    first, last = sources[0], sources[-1]

    start = SkyCoord(first.ra * u.deg, first.dec * u.deg)
    reach = max(float(start.separation(SkyCoord(s.ra * u.deg, s.dec * u.deg)).arcsec) for s in sources)
    side = min(MAX_PATCH_ARCSEC, max(MIN_PATCH_ARCSEC, 2 * (reach + TRACK_MARGIN_ARCSEC)))
    n = int(np.ceil(side / PATCH_PIXEL_ARCSEC)) | 1  # odd: the first position is the centre of a pixel
    grid = patch_wcs(first.ra, first.dec, n)

    images, counts = pass_images(result, grid, n)
    lo, hi = grey_limits(images)
    scatter = field_scatter(result, obj, first.pass_index)
    by_pass = {s.pass_index: s for s in sources}
    # Dates come from the exposures themselves, also when the engine analysed one stack per pass.
    if len(result.input_passes) == len(result.passes):
        exposures = [[result.input_frames[i].mjd for i in ids] for ids in result.input_passes]
    else:
        exposures = [[result.frames[i].mjd for i in ids] for ids in result.passes]

    passes = []
    for k, (image, mjds) in enumerate(zip(images, exposures, strict=True)):
        source = by_pass.get(k)
        point = None
        if source is not None:
            east, north = patch_offsets(grid, n, source.ra, source.dec)
            point = EvidencePoint(
                east_arcsec=east, north_arcsec=north, sigma_arcsec=source.sigma_arcsec,
                mjd=source.mjd, date=archive.mjd_to_date(source.mjd), n_frames=source.n_det,
                fluxes=[
                    EvidenceFlux(band=band, flux=flux, flux_err=err)
                    for band, (flux, err) in sorted(source.flux_by_detector.items())
                ],
            )
        passes.append(EvidencePass(
            index=k, start=archive.mjd_to_date(min(mjds)), end=archive.mjd_to_date(max(mjds)),
            n_frames=counts[k], image_png_b64=_b64(cutouts.render_png(image, vmin=lo, vmax=hi)),
            coverage=float(np.isfinite(image).mean()), point=point, field_scatter=scatter.get(k),
        ))

    # The single exposures: same centre and grey scale, cut down to the track and its surroundings.
    # A saturated star is white well beyond its position, and its move shows only on the edge of
    # that core, so the window then holds the whole core with sky around it.
    centre = (n - 1) / 2
    core_arcsec = PATCH_PIXEL_ARCSEC * saturated_core_px(images[first.pass_index], centre, centre, hi)
    side = max(MIN_EXPOSURE_ARCSEC, 2 * (core_arcsec + reach + EXPOSURE_MARGIN_ARCSEC))
    m = min(n, int(np.ceil(side / PATCH_PIXEL_ARCSEC)) | 1)
    small = patch_wcs(first.ra, first.dec, m)
    exposures, covers = pass_exposures(result, obj, small, m, lo, hi)
    for p, row, cover in zip(passes, exposures, covers, strict=True):
        p.exposures, p.n_cover = row, cover

    spherex_px = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    fwhms = [f.psf_fwhm_arcsec for f in result.frames if np.isfinite(f.psf_fwhm_arcsec)]
    fwhm = float(np.median(fwhms)) if fwhms else 1.5 * spherex_px  # SPHEREx PSFs are ~1.5 px wide
    checks = run_checks(
        result, obj, grid, n, images, lo, hi, scatter, PATCH_PIXEL_ARCSEC, fwhm, spherex_px,
        render=lambda size, keep: pass_images(
            dataclasses.replace(result, passes=[result.passes[k] for k in keep]), patch_wcs(first.ra, first.dec, size), size
        )[0],
    )

    fit_east, fit_north = patch_offsets(grid, n, obj.ra, obj.dec)
    return MotionEvidence(
        object=object_id, ra=obj.ra, dec=obj.dec,
        patch_ra=first.ra, patch_dec=first.dec,
        patch_arcsec=n * PATCH_PIXEL_ARCSEC, patch_px=n, patch_pixel_arcsec=PATCH_PIXEL_ARCSEC,
        spherex_pixel_arcsec=spherex_px,
        passes=passes,
        difference_png_b64=_b64(difference_png(images[first.pass_index], images[last.pass_index], lo, hi)),
        difference_passes=(first.pass_index, last.pass_index),
        fit=EvidenceFit(
            ref_mjd=obj.ref_mjd, east_arcsec=fit_east, north_arcsec=fit_north,
            pm_east=obj.pm_ra_cosdec, pm_north=obj.pm_dec,
        ),
        pm_floor_arcsec_yr=result.pm_floor_arcsec_yr,
        exposure_arcsec=m * PATCH_PIXEL_ARCSEC, exposure_px=m, checks=checks,
        missing_passes=sorted(obj.missing_passes),
    )
