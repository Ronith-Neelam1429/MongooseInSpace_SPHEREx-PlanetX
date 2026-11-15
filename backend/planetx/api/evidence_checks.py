"""Independent tests that an engine object's motion is real and belongs to one object.

The engine's verdict rests on one number (fitted speed over its error). These tests
look at other pieces of the same data, so they fail for different reasons:

  * beyond_noise        the move against how far the field's fixed stars appear to shift
  * track_line          every pass on one straight line at a steady speed
  * images_show_motion  the difference image has a dark-to-bright pair along the track,
                        compared with the same pattern at random places in the patch
  * many_exposures      found in many single exposures, not one (cosmic rays, glitches)
  * detectors_agree     each detector array measures the move on its own
  * same_look           the same brightness and colour in every pass (one object, not two)
  * no_stand_in         no other source sat at its new spot before, or stays at its old one

All positions are offsets in arcsec on the evidence patch's grid (east and north
positive), so the numbers match what the images show. Pure functions, no network.
"""
from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise

import numpy as np
from astropy.wcs import WCS
from scipy import ndimage

from planetx.api.schemas import EvidenceCheck, FieldScatter
from planetx.engine import EngineResult, TrackedObject
from planetx.engine.motion import PASS_FLOOR_ARCSEC, PassSource
from planetx.spherex import archive

DAYS_PER_YEAR = 365.25
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]

STRONG_SIGMA = 5.0  # the engine's own threshold for a mover
WEAK_SIGMA = 3.0
TRACK_MAX_SIGMA = 3.0  # largest miss off the straight line, in its errors
DETECTOR_MAX_SIGMA = 3.0
# Brightness per detector band is a median over a pass's exposures, and each pass samples
# other wavelengths within the band, so passes of a steady source differ by up to ~20%.
LOOK_TOLERANCE = 0.3
LOOK_MAX_SIGMA = 3.0
MIN_EXPOSURES = 3  # per pass: more than a glitch, a cosmic ray or a pair of them
# Places in the patch that set the difference image's noise; fewer give no usable spread.
MIN_NULL_SAMPLES = 30
# Below this the track is under a grid pixel: the difference image cannot show it.
MIN_IMAGE_SHIFT_PX = 0.5
# A pair across the track this strong, relative to the one along it, is not a clean move.
MAX_CROSS_RATIO = 0.5
# The shift the pixels show has to be within this factor of the engine's.
SEEN_RANGE = (0.5, 2.0)
# Comparison places need room: patches for this test are at least this many stencil radii
# wide (more than the shown patch, around a saturated star's wide core), up to a limit.
ROOM_RADII = 12
MAX_ROOM_PX = 255


def sky_offsets(grid: WCS, n: int, ra, dec) -> np.ndarray:
    """(east, north) arcsec from the centre of an n x n patch grid; shape (..., 2)."""
    x, y = grid.wcs_world2pix(np.atleast_1d(ra), np.atleast_1d(dec), 0)
    centre = (n - 1) / 2
    scale = abs(grid.wcs.cdelt[0]) * 3600
    return np.stack([(centre - x) * scale, (centre - y) * scale], axis=-1)


def _month(mjd: float) -> str:
    date = archive.mjd_to_date(mjd)
    return f"{MONTHS[int(date[5:7]) - 1]} {date[:4]}"


def _compass(east: float, north: float) -> str:
    pa = (np.degrees(np.arctan2(east, north)) + 360) % 360
    return COMPASS[round(float(pa) / 45) % 8]


def _graded(value: float, strong: float = STRONG_SIGMA, weak: float = WEAK_SIGMA) -> str:
    return "pass" if value >= strong else "weak" if value >= weak else "fail"


def _list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def shift_noise(first: PassSource, last: PassSource, field: FieldScatter | None) -> float:
    """Per-axis error of the move from `first` to `last`, arcsec. Fixed stars' apparent shifts
    carry alignment errors the object's own position errors may miss."""
    return float(np.hypot(np.hypot(first.sigma_arcsec, last.sigma_arcsec), field.sigma_arcsec if field else 0.0))


def beyond_noise(
    first: PassSource, last: PassSource, pos: dict[int, np.ndarray], scatter: dict[int, FieldScatter]
) -> EvidenceCheck:
    title = "Did it move more than the fixed stars seem to?"
    shift_vec = pos[last.pass_index] - pos[first.pass_index]
    shift = float(np.hypot(*shift_vec))
    field = scatter.get(last.pass_index)
    noise = shift_noise(first, last, field)
    ratio = shift / noise
    span = f"between {_month(first.mjd)} and {_month(last.mjd)}"
    if field:
        against = (f"Over the same time the field's {field.n_stars} other objects, nearly all fixed stars, "
                   f"appear to shift by {field.sigma_arcsec:.2f}″ (1σ per axis)")
    else:
        against = "Too few other objects were seen in both passes to measure the field's noise; against its own position errors"
    status = _graded(ratio)
    if status == "fail":
        summary = (f"Its position changed by {shift:.1f}″ {span}. {against}, so the change is {ratio:.1f} times the "
                   f"noise: no sign of motion.")
    else:
        summary = (f"It moved {shift:.1f}″ to the {_compass(*shift_vec)} {span}. {against}, "
                   f"so the move is {ratio:.1f} times the noise.")
    return EvidenceCheck(
        key="beyond_noise", title=title, status=status, value=ratio, summary=summary,
        details=[
            f"Position error: {first.sigma_arcsec:.2f}″ in {_month(first.mjd)}, {last.sigma_arcsec:.2f}″ in {_month(last.mjd)}",
            f"Combined noise per axis: {noise:.2f}″",
        ],
    )


def track_line(
    sources: list[PassSource], pos: dict[int, np.ndarray], scatter: dict[int, FieldScatter], obj: TrackedObject
) -> EvidenceCheck:
    title = "Do all passes lie on one straight track?"
    if len(sources) < 3:
        return EvidenceCheck(
            key="track_line", title=title, status="unknown",
            summary=f"Seen in only {len(sources)} passes. Two points always lie on a line, so this needs a third pass.",
        )
    t = np.array([(s.mjd - sources[0].mjd) / DAYS_PER_YEAR for s in sources])
    xy = np.array([pos[s.pass_index] for s in sources])
    field = [scatter[s.pass_index].sigma_arcsec for s in sources if s.pass_index in scatter]
    # The field scatter is of differences between two passes; one pass carries 1/sqrt(2) of it.
    floor = float(np.median(field)) / np.sqrt(2) if field else 0.0
    sig = np.hypot([s.sigma_arcsec for s in sources], floor)
    design = np.vstack([np.ones_like(t), t]).T
    a = design / sig[:, None]
    resid = np.empty_like(xy)
    for axis in (0, 1):
        coef, *_ = np.linalg.lstsq(a, xy[:, axis] / sig, rcond=None)
        resid[:, axis] = xy[:, axis] - design @ coef
    # The fit pulls the line toward every point, most toward the end points: with 3 passes a
    # point 4 errors off leaves a residual of under 3. Standardise by sqrt(1 - leverage).
    leverage = np.einsum("ij,jk,ik->i", a, np.linalg.inv(a.T @ a), a)
    norm = np.abs(resid) / (sig * np.sqrt(np.clip(1 - leverage, 1e-6, None)))[:, None]
    k = int(norm.max(axis=1).argmax())
    worst, miss = float(norm[k].max()), float(np.hypot(*resid[k]))
    details = [
        f"{_month(s.mjd)}: {np.hypot(*resid[i]):.2f}″ off the line (error {sig[i]:.2f}″)" for i, s in enumerate(sources)
    ]
    if worst <= TRACK_MAX_SIGMA:
        return EvidenceCheck(
            key="track_line", title=title, status="pass", value=worst, details=details,
            summary=f"All {len(sources)} positions lie on one straight line at a steady speed, as a real object's do. "
                    f"The largest miss is {miss:.2f}″, {worst:.1f} times its error.",
        )
    # With 3 passes the line misses each of them by the same number of errors: no one pass is to blame.
    which = f"the {_month(sources[k].mjd)} one" if len(sources) > 3 else "one of them"
    if obj.parallax is not None:
        return EvidenceCheck(
            key="track_line", title=title, status="weak", value=worst, details=details,
            summary=f"The positions bend away from a straight line ({which} by {miss:.2f}″, {worst:.1f} times its "
                    f"error). The engine explains this with parallax, the yearly wobble of a nearby star.",
        )
    return EvidenceCheck(
        key="track_line", title=title, status="fail", value=worst, details=details,
        summary=f"The positions do not lie on a straight line: {which} misses it by {miss:.2f}″, "
                f"{worst:.1f} times its error. The passes may have caught different objects.",
    )


def _aperture(image: np.ndarray, row: float, col: float, radius: float) -> float:
    """Background-subtracted sum within `radius` pixels; NaN when the spot has no data."""
    rows, cols = np.mgrid[0:image.shape[0], 0:image.shape[1]]
    r = np.hypot(rows - row, cols - col)
    inner, ring = r <= radius, (r > 2 * radius) & (r <= 3 * radius)
    if not np.isfinite(image[inner]).all() or np.isfinite(image[ring]).sum() < 8:
        return float("nan")
    return float(np.sum(image[inner] - np.nanmedian(image[ring])))


def saturated_core_px(image: np.ndarray, row: float, col: float, hi: float) -> float:
    """Radius (pixels, as a disc of equal area) of the white core at (row, col); 0 if there is none."""
    r, c = round(float(row)), round(float(col))
    if not (0 <= r < image.shape[0] and 0 <= c < image.shape[1]):
        return 0.0
    with np.errstate(invalid="ignore"):
        white = image >= hi
    if not white[r, c]:
        return 0.0
    labels, _ = ndimage.label(white)
    return float(np.sqrt((labels == labels[r, c]).sum() / np.pi))


def stencil_radius_px(shift_px: float, core_px: float, fwhm_px: float) -> float:
    """Disc around the track's midpoint that holds both ends of the move: the shift, the white
    core of a saturated star (its edges carry the signal) and the blur."""
    return shift_px / 2 + core_px + fwhm_px


def _shift_seen(
    first_image: np.ndarray, lo: float, hi: float, u: np.ndarray, dist: float, stat: Callable[[np.ndarray], float],
    measured: float,
) -> float:
    """The shift (pixels) along `u` whose difference pattern matches `measured`; inf when it is beyond
    twice `dist` (the calibration does not reach that far, so no size can be read off)."""
    base = np.where(np.isfinite(first_image), first_image, lo)
    steps = np.linspace(0.0, 2 * dist, 41)
    curve = []
    for step in steps:
        moved = ndimage.shift(base, step * u, order=1, mode="nearest")
        with np.errstate(invalid="ignore"):
            curve.append(stat(np.clip(moved, lo, hi) - np.clip(first_image, lo, hi)))
    curve = np.maximum.accumulate(np.nan_to_num(np.asarray(curve), nan=0.0))  # rising until the pair separates
    if measured >= curve[-1]:
        return float("inf")
    return float(np.interp(measured, curve, steps))


def _seen_text(arcsec: float, cap: float) -> str:
    return f"more than {cap:.1f}″" if not np.isfinite(arcsec) else f"about {arcsec:.1f}″"


def images_show_motion(
    first_image: np.ndarray, last_image: np.ndarray, lo: float, hi: float,
    p_first: np.ndarray, p_last: np.ndarray, pixel_arcsec: float, fwhm_arcsec: float,
    months: tuple[str, str], core_px: float = 0.0, noise_arcsec: float | None = None, saturated: bool = False,
) -> EvidenceCheck:
    """Dark-to-bright pair along the track in last - first, against random places in the patch.

    The statistic: the mean of the difference image over a disc around the track's midpoint,
    counted positive ahead of the midpoint (along the motion) and negative behind it. Each half
    is judged on its own and the weaker one counts, so a source that only brightened (or only
    appeared) ahead of the object does not pass: a move needs the dark half too. The same
    stencil at non-overlapping places in the patch gives the noise, fixed stars' residues included. The same
    stencil turned across the track is a control: a misaligned image or a change of shape
    also leaves a pair, but not one that points along the measured motion only.

    Calibration: the first image moved by known amounts along the track gives the pair each
    shift leaves, so the measured pair converts to a shift seen in the pixels alone (no
    star-finding). It has to match the engine's shift and exceed the alignment noise
    (`noise_arcsec`, per axis: the move's measurement noise): a bright star leaves a strong pair for any small misalignment.
    """
    title = "Do the images themselves show it moving?"
    n = first_image.shape[0]
    centre = (n - 1) / 2
    # (row, col) on the image: row 0 north, column 0 east.
    p0 = np.array([centre - p_first[1] / pixel_arcsec, centre - p_first[0] / pixel_arcsec])
    p1 = np.array([centre - p_last[1] / pixel_arcsec, centre - p_last[0] / pixel_arcsec])
    dist = float(np.hypot(*(p1 - p0)))
    if dist < MIN_IMAGE_SHIFT_PX:
        return EvidenceCheck(
            key="images_show_motion", title=title, status="unknown",
            summary=f"It shifted {dist * pixel_arcsec:.2f}″, under half an image pixel: too little to see in the images.",
        )
    with np.errstate(invalid="ignore"):
        diff = np.clip(last_image, lo, hi) - np.clip(first_image, lo, hi)
    u = (p1 - p0) / dist
    fwhm_px = fwhm_arcsec / pixel_arcsec
    radius = stencil_radius_px(dist, core_px, fwhm_px)
    mid = (p0 + p1) / 2
    base = np.round(mid).astype(int)
    span = int(np.ceil(radius)) + 1
    dr, dc = np.mgrid[-span:span + 1, -span:span + 1]
    rel_r, rel_c = base[0] + dr - mid[0], base[1] + dc - mid[1]
    inside_disc = np.hypot(rel_r, rel_c) <= radius

    def stencil(direction: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        along = rel_r * direction[0] + rel_c * direction[1]
        keep = inside_disc & (np.abs(along) > 0.5)
        return dr[keep], dc[keep], np.sign(along[keep])

    def stat(r0: int, c0: int, st, image: np.ndarray = diff, lobe: int = 0) -> float:
        """Mean of image * sign over the stencil at (r0, c0); only the half with sign == lobe if lobe != 0."""
        rr, cc, sign = r0 + st[0], c0 + st[1], st[2]
        if lobe:
            rr, cc, sign = rr[sign == lobe], cc[sign == lobe], sign[sign == lobe]
        if not ((rr >= 0) & (rr < n) & (cc >= 0) & (cc < n)).all():
            return float("nan")
        vals = image[rr, cc] * sign
        finite = np.isfinite(vals)
        return float(vals[finite].mean()) if finite.mean() >= 0.8 else float("nan")

    track = stencil(u)
    measured = stat(*base, track)
    across = stat(*base, stencil(np.array([-u[1], u[0]])))
    # Comparison places on a grid one radius apart (stencils overlap by at most half), away
    # from the object's own pair: each one is close to an independent sample.
    step = max(1, int(np.floor(radius)))
    places = [
        (r0, c0) for r0 in range(span, n - span, step) for c0 in range(span, n - span, step)
        if np.hypot(r0 - mid[0], c0 - mid[1]) >= 2 * radius
    ]
    null = np.array([[stat(r0, c0, track, lobe=lobe) for lobe in (0, -1, 1)] for r0, c0 in places]).reshape(-1, 3)
    null = null[np.isfinite(null).all(axis=1)]
    if not np.isfinite(measured) or len(null) < MIN_NULL_SAMPLES:
        return EvidenceCheck(
            key="images_show_motion", title=title, status="unknown",
            summary="The patch has too little data around the track, or too little elsewhere to compare with.",
        )
    centres = np.median(null, axis=0)
    spreads = 1.4826 * np.median(np.abs(null - centres), axis=0)
    if not (spreads > 0).all():
        return EvidenceCheck(
            key="images_show_motion", title=title, status="unknown", summary="The difference image is flat: nothing to compare.",
        )
    centre_null = float(centres[0])
    behind = (stat(*base, track, lobe=-1) - centres[1]) / spreads[1]  # darker where it was
    ahead = (stat(*base, track, lobe=1) - centres[2]) / spreads[2]  # brighter where it is
    sig = float(min(behind, ahead))
    beaten = int((null[:, 0] >= measured).sum())
    seen_arcsec = _shift_seen(first_image, lo, hi, u, dist, lambda img: stat(*base, track, img), measured) * pixel_arcsec
    shift_arcsec = dist * pixel_arcsec
    cross = abs(across - centre_null) / abs(measured - centre_null) if measured != centre_null else float("inf")
    details = [
        f"Shift the pixels alone show: {_seen_text(seen_arcsec, 2 * shift_arcsec)} (the engine measured {shift_arcsec:.1f}″)",
        f"Darker where it was: {behind:.1f} times the noise; brighter where it is: {ahead:.1f} times",
        f"Places in the patch compared: {len(null)}; {beaten} of them show as strong a pair",
        f"The same pattern across the track (a control): {cross:.0%} of the one along it",
    ]
    if dist * pixel_arcsec >= 2 * fwhm_arcsec and core_px == 0:
        r = 0.6 * fwhm_px
        old_now, old_then = _aperture(last_image, p0[0], p0[1], r), _aperture(first_image, p0[0], p0[1], r)
        new_then, new_now = _aperture(first_image, p1[0], p1[1], r), _aperture(last_image, p1[0], p1[1], r)
        if old_then > 0 and new_now > 0 and np.isfinite(old_now) and np.isfinite(new_then):
            details.append(f"Its old spot in {months[1]}: {max(old_now, 0) / old_then:.0%} of the light it had in {months[0]}")
            details.append(f"Its new spot in {months[0]}: {max(new_then, 0) / new_now:.0%} of the light it has in {months[1]}")
    if core_px > 0:
        what = "saturated (white" if saturated else "brighter than the grey scale shows (white"
        details.append(f"Its core is {what}, {2 * core_px * pixel_arcsec:.0f}″ across) in both passes, so the "
                       f"move shows on the core's edges")
    if noise_arcsec:
        details.append(f"Measurement noise of the move: {noise_arcsec:.2f}″ per axis")
    status = _graded(sig)
    pair = f"a dark edge or spot where it was in {months[0]} and a bright one where it is in {months[1]}"
    seen = f"Read from the pixels alone, without finding the star, that is a shift of {_seen_text(seen_arcsec, 2 * shift_arcsec)}"
    if sig >= WEAK_SIGMA and cross > MAX_CROSS_RATIO:
        status = "weak"
        summary = (f"The {months[1]} minus {months[0]} image changes around it ({sig:.1f} times the noise), but about as much "
                   f"across its track as along it: that looks more like a change of shape or alignment than a move.")
    elif sig >= WEAK_SIGMA and noise_arcsec and seen_arcsec < WEAK_SIGMA * noise_arcsec:
        status = "weak"
        summary = (f"The {months[1]} minus {months[0]} image shows {pair}. {seen}, no more than "
                   f"{WEAK_SIGMA:.0f} times its {noise_arcsec:.2f}″ measurement noise: a small misalignment leaves such a pair too.")
    elif sig >= WEAK_SIGMA and not SEEN_RANGE[0] <= seen_arcsec / shift_arcsec <= SEEN_RANGE[1]:
        status = "weak"
        summary = (f"The {months[1]} minus {months[0]} image shows {pair}. {seen}, which does not match the "
                   f"{shift_arcsec:.1f}″ the engine measured.")
    elif sig >= WEAK_SIGMA:
        summary = (f"The {months[1]} minus {months[0]} image shows {pair}, pointing along its measured motion and "
                   f"both halves at least {sig:.1f} times stronger than at {len(null)} other places in this patch. {seen}, matching the "
                   f"engine's {shift_arcsec:.1f}″.")
    else:
        summary = (f"The difference image shows no clear dark-to-bright pair along its track: its weaker half is "
                   f"{sig:.1f} times the noise at other places in this patch.")
    return EvidenceCheck(
        key="images_show_motion", title=title, status=status, value=float(sig), summary=summary, details=details,
    )


def many_exposures(sources: list[PassSource], stacked: bool = False) -> EvidenceCheck:
    title = "Was it found in many separate exposures?"
    if stacked:
        return EvidenceCheck(
            key="many_exposures", title=title, status="unknown",
            summary="The engine found it in one combined image per pass (too faint for single exposures), so it was "
                    "not counted in single exposures.",
        )
    least = min(s.n_det for s in sources)
    total = sum(s.n_det for s in sources)
    spans = [
        (max(d.mjd for d in s.detections) - min(d.mjd for d in s.detections)) for s in sources if len(s.detections) > 1
    ]
    gap = min(spans) if spans else 0.0
    spread = f"{gap * 24:.0f} hours" if gap < 2 else f"{gap:.0f} days"
    status = "pass" if least >= MIN_EXPOSURES else "weak" if least >= 2 else "fail"
    return EvidenceCheck(
        key="many_exposures", title=title, status=status, value=float(least),
        summary=f"Found in {total} single exposures over {len(sources)} passes, at least {least} per pass, spread over "
                f"{spread} or more within each pass. A cosmic ray, hot pixel or satellite trail is in one exposure only.",
        details=[f"{_month(s.mjd)}: in {s.n_det} of the {s.n_cover} exposures that cover it" for s in sources],
    )


def detectors_agree(
    result: EngineResult, first: PassSource, last: PassSource, grid: WCS, n: int, total: np.ndarray,
    field: FieldScatter | None, moved: bool,
) -> EvidenceCheck:
    title = "Does each detector see the same move on its own?"
    if result.stacked:
        return EvidenceCheck(
            key="detectors_agree", title=title, status="unknown",
            summary="The engine combined all detectors of a pass into one image here, so they cannot be compared.",
        )

    def by_detector(source: PassSource) -> dict[int, tuple[np.ndarray, float, int]]:
        out = {}
        for det in sorted({d.detector for d in source.detections}):
            ds = [d for d in source.detections if d.detector == det]
            xy = sky_offsets(grid, n, [d.ra for d in ds], [d.dec for d in ds])
            w = 1 / np.array([d.sigma_arcsec for d in ds]) ** 2
            out[det] = ((w[:, None] * xy).sum(axis=0) / w.sum(), float(np.hypot(1 / np.sqrt(w.sum()), PASS_FLOOR_ARCSEC)), len(ds))
        return out

    a, b = by_detector(first), by_detector(last)
    common = sorted(a.keys() & b.keys())
    if len(common) < 2:
        return EvidenceCheck(
            key="detectors_agree", title=title, status="unknown",
            summary="Fewer than two detectors saw it in both its first and last pass, so there is nothing to compare.",
        )
    rows, devs = [], []
    # Every exposure is aligned on its own, so a detector's shift also carries the alignment
    # noise that the field's fixed stars show.
    align = field.sigma_arcsec if field else 0.0
    for det in common:
        shift = b[det][0] - a[det][0]
        err = float(np.hypot(np.hypot(a[det][1], b[det][1]), align))
        devs.append(float(np.hypot(*(shift - total))) / err)
        rows.append((det, shift, err, a[det][2] + b[det][2]))
    agree = sum(d <= DETECTOR_MAX_SIGMA for d in devs)
    status = "pass" if agree == len(common) else "weak" if agree >= 2 else "fail"
    shifts = _list([f"{np.hypot(*s):.1f}″" for _, s, _, _ in rows])
    names = _list([f"D{d}" for d in common])
    if status == "pass" and moved:
        summary = (f"Detectors {names} each measured the move on their own, from their own exposures at their own "
                   f"wavelengths: {shifts} to the {_compass(*total)}. They agree within {max(devs):.1f} times their errors.")
    elif status == "pass":
        summary = (f"Detectors {names} each measured its shift on their own, from their own exposures at their own "
                   f"wavelengths: {shifts}. They agree within {max(devs):.1f} times their errors.")
    else:
        summary = (f"Detectors {names} measured {shifts}; {len(common) - agree} of them differ from the combined move by "
                   f"more than {DETECTOR_MAX_SIGMA:.0f} times their errors.")
    return EvidenceCheck(
        key="detectors_agree", title=title, status=status, value=float(max(devs)), summary=summary,
        details=[
            f"D{d}: {np.hypot(*s):.1f}″ ± {e:.1f}″ to the {_compass(*s)}, from {k} exposures" for d, s, e, k in rows
        ],
    )


def same_look(sources: list[PassSource], is_variable: bool) -> EvidenceCheck:
    title = "Does it look the same in every pass?"
    first = sources[0]
    rows, colours = [], []
    for s in sources[1:]:
        ratios = {}
        for band, (f, e) in sorted(s.flux_by_detector.items()):
            if band not in first.flux_by_detector:
                continue
            f0, e0 = first.flux_by_detector[band]
            if not (f > 0 and f0 > 0):
                continue
            ratio = f / f0
            err = ratio * float(np.hypot(e / f, e0 / f0))
            ratios[band] = (ratio, err)
            rows.append((s, band, ratio, err, abs(ratio - 1) <= max(LOOK_MAX_SIGMA * err, LOOK_TOLERANCE)))
        bands = sorted(ratios)
        for x, y in pairwise(bands):
            c = ratios[x][0] / ratios[y][0]
            ce = c * float(np.hypot(ratios[x][1] / ratios[x][0], ratios[y][1] / ratios[y][0]))
            colours.append(abs(c - 1) <= max(LOOK_MAX_SIGMA * ce, LOOK_TOLERANCE))
    if not rows:
        return EvidenceCheck(
            key="same_look", title=title, status="unknown",
            summary="Its brightness was not measured in the same detector in two passes, so there is nothing to compare.",
        )
    details = [
        f"{_month(s.mjd)} vs {_month(first.mjd)}, {band}: ×{ratio:.2f} ± {err:.2f}" for s, band, ratio, err, _ in rows
    ]
    lo, hi = min(r[2] for r in rows), max(r[2] for r in rows)
    if all(r[4] for r in rows):
        return EvidenceCheck(
            key="same_look", title=title, status="pass", details=details,
            summary=f"It is equally bright in every pass and detector band (×{lo:.2f} to ×{hi:.2f} of its first pass), "
                    f"as one and the same object would be.",
        )
    if colours and all(colours):
        return EvidenceCheck(
            key="same_look", title=title, status="weak", details=details,
            summary=f"Its brightness changed (×{lo:.2f} to ×{hi:.2f}) but its colour, the ratio between detector bands, "
                    f"stayed the same: one object that varies, rather than two different ones.",
        )
    if is_variable:
        return EvidenceCheck(
            key="same_look", title=title, status="unknown", details=details,
            summary="Its brightness changes (the engine flags it as variable), so brightness cannot tie its passes together.",
        )
    return EvidenceCheck(
        key="same_look", title=title, status="fail", details=details,
        summary=f"Its brightness differs between passes (×{lo:.2f} to ×{hi:.2f}) by more than measurement errors and "
                f"wavelength sampling explain. The passes may have caught different objects.",
    )


def no_stand_in(
    result: EngineResult, obj: TrackedObject, sources: list[PassSource], grid: WCS, n: int, radius_arcsec: float
) -> EvidenceCheck:
    """Was any other source at one of its positions in another pass?

    If a source already sat at the object's later position in an earlier pass (or stays at
    its earlier position later), the engine may have joined two fixed objects into one track.
    """
    title = "Is its old spot empty later and its new spot empty before?"
    here = {s.pass_index: sky_offsets(grid, n, s.ra, s.dec)[0] for s in sources}
    others: dict[int, np.ndarray] = {}
    for k in here:
        ra = [s.ra for o in result.objects if o is not obj for s in o.sources if s.pass_index == k]
        ra += [s.ra for s in result.unlinked if s.pass_index == k]
        dec = [s.dec for o in result.objects if o is not obj for s in o.sources if s.pass_index == k]
        dec += [s.dec for s in result.unlinked if s.pass_index == k]
        others[k] = sky_offsets(grid, n, ra, dec) if ra else np.empty((0, 2))
    month = {s.pass_index: _month(s.mjd) for s in sources}
    separable, found = 0, []
    for j, at_j in here.items():
        for k, at_k in here.items():
            if j == k or np.hypot(*(at_j - at_k)) < radius_arcsec:
                continue  # its own blur covers both spots
            separable += 1
            if len(others[j]):
                d = np.hypot(*(others[j] - at_k).T)
                # A source the engine also saw beside it in pass k is a resolved neighbour, not a stand-in.
                beside = len(others[k]) and np.hypot(*(others[k] - at_k).T).min() < radius_arcsec
                if d.min() < radius_arcsec and not beside:
                    found.append(f"In {month[j]} another source sat {d.min():.1f}″ from where it is in {month[k]}")
    if not separable:
        return EvidenceCheck(
            key="no_stand_in", title=title, status="unknown",
            summary=f"It moved less than a star image is wide ({radius_arcsec:.0f}″), so its old and new spots overlap and "
                    f"cannot be checked apart. The difference image has to show the move.",
        )
    if found:
        return EvidenceCheck(
            key="no_stand_in", title=title, status="fail", details=found,
            summary="Another source sat at one of its positions in a different pass, so the engine may have joined two "
                    "objects into one track.",
        )
    return EvidenceCheck(
        key="no_stand_in", title=title, status="pass",
        summary=f"Nothing else was detected within {radius_arcsec:.0f}″ of any of its positions in the other passes: its "
                f"new spots were empty before it arrived and its old spots are empty after it left: one object, not "
                f"two neighbours.",
    )


def run_checks(
    result: EngineResult, obj: TrackedObject, grid: WCS, n: int, images: list[np.ndarray], lo: float, hi: float,
    scatter: dict[int, FieldScatter], pixel_arcsec: float, fwhm_arcsec: float, spherex_pixel_arcsec: float,
    render: Callable[[int, list[int]], list[np.ndarray]],
) -> list[EvidenceCheck]:
    """All tests for `obj` on the evidence patch `grid` (n x n, `pixel_arcsec` per pixel).

    `render(m, passes)` gives those passes' images on an m x m grid of the same centre and pixel
    size, for when the difference-image test needs more room than the shown patch has.
    """
    sources = sorted(obj.sources, key=lambda s: s.mjd)
    first, last = sources[0], sources[-1]
    pos = {s.pass_index: sky_offsets(grid, n, s.ra, s.dec)[0] for s in sources}
    # Two sources closer than this blend into one in SPHEREx images.
    radius = max(1.5 * fwhm_arcsec, 2 * spherex_pixel_arcsec)

    a, b = images[first.pass_index], images[last.pass_index]
    centre = (n - 1) / 2
    rc = {k: (centre - xy[1] / pixel_arcsec, centre - xy[0] / pixel_arcsec) for k, xy in pos.items()}
    core = max(saturated_core_px(a, *rc[first.pass_index], hi), saturated_core_px(b, *rc[last.pass_index], hi))
    shift_px = float(np.hypot(*(pos[last.pass_index] - pos[first.pass_index]))) / pixel_arcsec
    room = min(MAX_ROOM_PX, int(np.ceil(ROOM_RADII * stencil_radius_px(shift_px, core, fwhm_arcsec / pixel_arcsec))) | 1)
    if room > n:
        a, b = render(room, [first.pass_index, last.pass_index])

    noise = beyond_noise(first, last, pos, scatter)
    return [
        noise,
        track_line(sources, pos, scatter, obj),
        images_show_motion(
            a, b, lo, hi, pos[first.pass_index], pos[last.pass_index],
            pixel_arcsec, fwhm_arcsec, (_month(first.mjd), _month(last.mjd)), core_px=core,
            noise_arcsec=shift_noise(first, last, scatter.get(last.pass_index)),
            saturated=any(d.saturated for s in sources for d in s.detections),
        ),
        many_exposures(sources, result.stacked),
        detectors_agree(
            result, first, last, grid, n, pos[last.pass_index] - pos[first.pass_index],
            scatter.get(last.pass_index), noise.status != "fail",
        ),
        same_look(sources, obj.is_variable),
        no_stand_in(result, obj, sources, grid, n, radius),
    ]
