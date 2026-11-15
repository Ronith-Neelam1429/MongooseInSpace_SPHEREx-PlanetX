"""Find sources that move or change brightness between SPHEREx survey passes.

Pipeline (all in sky coordinates, so frames never need resampling):

1. Group frames into passes: SPHEREx revisits a field every ~6 months and
   each visit spans up to ~8 weeks, so a gap > `pass_gap_days` starts a new pass.
   Near the ecliptic poles coverage has no such gaps; there it is cut into
   time windows of at most `pass_max_days`, each treated as a pass.
2. Detect sources and measure centroids in every frame.
3. Align frames: shift each frame so its stars line up with a reference frame
   (median offset of matched stars). Removes small pointing/WCS errors.
4. Merge detections within a pass into one averaged source per object.
5. Link each pass's sources across passes:
   - "static" links within a few arcsec (ordinary stars and galaxies),
   - "mover" links for sources left unmatched, allowing up to
     `max_pm_arcsec_yr` of motion and requiring similar brightness.
6. Fit linear motion to every linked object; flag it as a mover when the
   motion is significant against both its own errors and the field's
   systematic floor (measured from the static stars).
7. Compare per-pass fluxes in the same detector to flag variables.

Intra-pass motion (fast asteroids moving within one visit) is not handled yet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from scipy.stats import chi2 as chi2_dist

from .detect import Detection, detect_sources, frame_background
from .frames import Frame
from .stack import stack_grid, stack_pass

DAYS_PER_YEAR = 365.25
# Longest stretch of time averaged as one pass. A pass's detections are merged
# as if the object stood still, so the fastest motion the engine links (15"/yr)
# may only smear it by a fraction of the 0.7 px merge radius: 60 days is 0.4 px.
# Ordinary passes are shorter (longest seen: 55 days, Barnard's Star).
MAX_PASS_DAYS = 60.0
# Per-pass position floor for saturated stars. Measured on Barnard's Star
# against Gaia (detectors 1-3 and 4-6, 3 passes): per-axis scatter ~0.8"
# (0.13 px). 0.2 px keeps ~50% margin; fits came out chi2/dof ~0.6-0.7 at
# 0.25 px, i.e. errors overstated. Revisit with more saturated stars.
SATURATED_PASS_FLOOR_PX = 0.2
PASS_FLOOR_ARCSEC = 0.3
# Saturated patch this large marks a very bright star (R Dra: ~90-100 px).
BRIGHT_SAT_AREA_PX = 50
BRIGHT_CAUTION_ARCSEC = 150.0


@dataclass
class PassSource:
    """One object as seen in one pass: detections averaged over that visit."""

    pass_index: int
    ra: float
    dec: float
    sigma_arcsec: float
    mjd: float
    n_det: int
    n_cover: int  # frames in this pass that cover the position
    snr: float  # best single-frame SNR
    # "D3" (core flux) or "D3-wing" (saturated star, annulus flux) -> (median, error)
    flux_by_detector: dict[str, tuple[float, float]]
    detections: list[Detection] = field(default_factory=list)


@dataclass
class TrackedObject:
    """An object linked across passes, with its fitted motion."""

    sources: list[PassSource]
    link: str  # "static" | "mover"
    ra: float  # position at `ref_mjd`
    dec: float
    ref_mjd: float
    pm_ra_cosdec: float  # arcsec/yr, east positive
    pm_dec: float  # arcsec/yr, north positive
    pm_err: float  # arcsec/yr, includes the systematic floor
    chi2_dof: float  # nan when the fit has no spare degrees of freedom
    is_mover: bool = False
    is_variable: bool = False
    variability: dict | None = None
    # Passes whose frames covered the predicted position but saw nothing.
    missing_passes: list[int] = field(default_factory=list)
    # Fitted only when it explains a poor straight-line fit (arcsec).
    parallax: float | None = None
    parallax_err: float | None = None

    @property
    def pm_total(self) -> float:
        return float(np.hypot(self.pm_ra_cosdec, self.pm_dec))

    @property
    def pm_significance(self) -> float:
        return self.pm_total / self.pm_err if self.pm_err > 0 else 0.0

    @property
    def position_angle_deg(self) -> float:
        """Direction of motion, degrees east of north."""
        return float(np.degrees(np.arctan2(self.pm_ra_cosdec, self.pm_dec)) % 360)


@dataclass
class EngineResult:
    frames: list[Frame]  # frames analysed: the inputs, or one stack per pass
    passes: list[list[int]]  # indices into `frames` per pass, time-ordered
    detections: list[list[Detection]]  # per frame, after alignment
    frame_offsets_arcsec: list[tuple[float, float]]  # (east, north) shift applied
    objects: list[TrackedObject]
    unlinked: list[PassSource]  # seen in only one pass
    pm_floor_arcsec_yr: float
    notes: list[str] = field(default_factory=list)
    input_frames: list[Frame] = field(default_factory=list)
    input_passes: list[list[int]] = field(default_factory=list)  # into input_frames
    stacked: bool = False

    @property
    def movers(self) -> list[TrackedObject]:
        return sorted(
            (o for o in self.objects if o.is_mover), key=lambda o: -o.pm_significance
        )

    @property
    def variables(self) -> list[TrackedObject]:
        return [o for o in self.objects if o.is_variable]


def group_into_passes(
    mjds: list[float], gap_days: float = 60.0, max_days: float = MAX_PASS_DAYS
) -> list[list[int]]:
    """Split frame indices into time-ordered passes.

    A gap longer than `gap_days` starts a new pass. Where SPHEREx revisits a
    field without such gaps (near the ecliptic poles), a run of frames longer
    than `max_days` is cut into the fewest equal time windows that are each at
    most `max_days` long, and every window with frames in it is a pass.

    The cuts depend only on a run's first and last frame, so a subset of the
    frames that keeps those two (e.g. a few frames sampled per pass) is
    grouped the same way as the full list.
    """
    if max_days <= 0:
        raise ValueError(f"max_days must be positive, got {max_days}")
    if not mjds:
        return []
    order = np.argsort(mjds)
    runs: list[list[int]] = [[int(order[0])]]
    for prev, cur in zip(order[:-1], order[1:]):
        if mjds[cur] - mjds[prev] > gap_days:
            runs.append([])
        runs[-1].append(int(cur))
    passes: list[list[int]] = []
    for run in runs:
        start = mjds[run[0]]
        span = mjds[run[-1]] - start
        n = max(1, math.ceil(span / max_days))
        windows: list[list[int]] = [[] for _ in range(n)]
        for i in run:
            # The run's last frame sits exactly on the end of the last window.
            windows[min(int((mjds[i] - start) / span * n), n - 1) if n > 1 else 0].append(i)
        passes += [w for w in windows if w]
    return passes


def _offsets(center: SkyCoord, ra, dec) -> tuple[np.ndarray, np.ndarray]:
    """Tangent-plane offsets (east, north) in arcsec from `center`."""
    dra, ddec = center.spherical_offsets_to(SkyCoord(ra * u.deg, dec * u.deg))
    return np.atleast_1d(dra.to_value(u.arcsec)), np.atleast_1d(ddec.to_value(u.arcsec))


def _from_offsets(center: SkyCoord, east, north) -> tuple[float, float]:
    c = center.spherical_offsets_by(east * u.arcsec, north * u.arcsec)
    return float(c.ra.deg), float(c.dec.deg)


def _nearest(xs, ys, x, y) -> tuple[int, float]:
    if len(xs) == 0:
        return -1, np.inf
    d = np.hypot(np.asarray(xs) - x, np.asarray(ys) - y)
    i = int(np.argmin(d))
    return i, float(d[i])


def _covers(frame: Frame, ra: float, dec: float, margin_px: float = 2.0) -> bool:
    x, y = frame.wcs.all_world2pix(ra, dec, 0)
    ny, nx = frame.data.shape
    if not (margin_px <= x <= nx - 1 - margin_px and margin_px <= y <= ny - 1 - margin_px):
        return False
    iy, ix = int(round(float(y))), int(round(float(x)))
    # A saturated star's own pixels are NaN but the spot was observed.
    return bool(np.isfinite(frame.data[iy, ix]) or (frame.saturated is not None and frame.saturated[iy, ix]))


def align_frames(
    detections: list[list[Detection]],
    center: SkyCoord,
    match_arcsec: float = 3.0,
    min_stars: int = 3,
) -> tuple[list[tuple[float, float]], list[str]]:
    """Shift every frame's detections onto the reference frame's stars, in place.

    The reference is the frame with the most detections. The shift is the
    median offset of matched stars, so a few movers can't drag it.
    """
    notes: list[str] = []
    unaligned = 0
    ref_i = int(np.argmax([len(d) for d in detections]))
    ref = detections[ref_i]
    if not ref:
        return [(0.0, 0.0)] * len(detections), ["no detections; frames not aligned"]
    rx, ry = _offsets(center, np.array([d.ra for d in ref]), np.array([d.dec for d in ref]))
    shifts: list[tuple[float, float]] = []
    for i, dets in enumerate(detections):
        if i == ref_i or not dets:
            shifts.append((0.0, 0.0))
            continue
        fx, fy = _offsets(center, np.array([d.ra for d in dets]), np.array([d.dec for d in dets]))
        dx, dy = [], []
        for x, y in zip(fx, fy):
            j, dist = _nearest(rx, ry, x, y)
            if dist <= match_arcsec:
                dx.append(rx[j] - x)
                dy.append(ry[j] - y)
        if len(dx) < min_stars:
            unaligned += 1
            shifts.append((0.0, 0.0))
            continue
        sx, sy = float(np.median(dx)), float(np.median(dy))
        shifts.append((sx, sy))
        for d, x, y in zip(dets, fx, fy):
            d.ra, d.dec = _from_offsets(center, x + sx, y + sy)
    if unaligned:
        notes.append(
            f"{unaligned} of {len(detections)} frames had fewer than {min_stars} stars "
            "matching the reference frame and were left unaligned (sparse field)"
        )
    return shifts, notes


def _merge_pass(
    pass_index: int,
    frame_ids: list[int],
    frames: list[Frame],
    detections: list[list[Detection]],
    center: SkyCoord,
    link_arcsec: float,
    min_detect_frac: float,
) -> list[PassSource]:
    """Average each object's detections within one pass."""
    dets = [d for fi in frame_ids for d in detections[fi]]
    dets.sort(key=lambda d: -d.snr)  # brightest seed clusters first
    if not dets:
        return []
    xs, ys = _offsets(center, np.array([d.ra for d in dets]), np.array([d.dec for d in dets]))
    clusters: list[list[int]] = []
    cx: list[float] = []
    cy: list[float] = []
    for k, d in enumerate(dets):
        j, dist = _nearest(cx, cy, xs[k], ys[k])
        if dist <= link_arcsec and all(dets[m].frame_index != d.frame_index for m in clusters[j]):
            clusters[j].append(k)
        else:
            clusters.append([k])
            cx.append(xs[k])
            cy.append(ys[k])

    out: list[PassSource] = []
    for members in clusters:
        mx, my = xs[members], ys[members]
        sig = np.array([dets[m].sigma_arcsec for m in members])
        w = 1 / sig**2
        ex, ey = float((w * mx).sum() / w.sum()), float((w * my).sum() / w.sum())
        formal = float(1 / np.sqrt(w.sum()))
        n = len(members)
        scatter = float(np.hypot(mx.std(), my.std()) / np.sqrt(2 * n)) if n > 2 else 0.0
        # Alignment leaves ~0.1-0.3" residuals per pass (measured from static
        # stars), which averaging many frames of a bright star can't beat.
        sigma = float(np.hypot(max(formal, scatter), PASS_FLOOR_ARCSEC))
        if any(dets[m].saturated for m in members):
            # Saturated-patch centres share a bias that averaging can't remove.
            sigma = max(sigma, SATURATED_PASS_FLOOR_PX * frames[frame_ids[0]].pixel_scale_arcsec)
        ra, dec = _from_offsets(center, ex, ey)
        covering = [fi for fi in frame_ids if _covers(frames[fi], ra, dec)]
        n_cover = len(covering)
        # A stacked pass is a single frame, so one detection is all there is.
        if n < (2 if len(frame_ids) > 1 else 1):
            continue
        # Judge the detection rate per detector: red objects (Y dwarfs) are only
        # visible at 4-5 um, i.e. in a third of the frames or fewer.
        seen_by_det: dict[int, int] = {}
        for m in members:
            seen_by_det[dets[m].detector] = seen_by_det.get(dets[m].detector, 0) + 1
        cover_by_det: dict[int, int] = {}
        for fi in covering:
            cover_by_det[frames[fi].detector] = cover_by_det.get(frames[fi].detector, 0) + 1
        if not any(
            k >= min_detect_frac * cover_by_det.get(det, k) for det, k in seen_by_det.items()
        ):
            continue  # flickering at the noise limit: not a solid detection
        # Core fluxes and saturated stars' wing fluxes measure different things,
        # so they're kept under separate keys and only compared like with like.
        by_det: dict[str, list[float]] = {}
        for m in members:
            d = dets[m]
            if d.saturated:
                if np.isfinite(d.wing_flux):
                    by_det.setdefault(f"D{d.detector}-wing", []).append(d.wing_flux)
            elif np.isfinite(d.flux):
                by_det.setdefault(f"D{d.detector}", []).append(d.flux)
        flux_by_detector = {}
        for det, fl in by_det.items():
            fl = np.asarray(fl)
            if len(fl) < 2:
                continue  # one frame gives no error estimate; don't compare it
            err = fl.std(ddof=1) / np.sqrt(len(fl))
            flux_by_detector[det] = (float(np.median(fl)), float(err))
        out.append(
            PassSource(
                pass_index=pass_index,
                ra=ra,
                dec=dec,
                sigma_arcsec=sigma,
                # Same weights as the position, so position and epoch agree.
                mjd=float((w * np.array([dets[m].mjd for m in members])).sum() / w.sum()),
                n_det=n,
                n_cover=n_cover,
                snr=float(max(dets[m].snr for m in members)),
                flux_by_detector=flux_by_detector,
                detections=[dets[m] for m in members],
            )
        )
    return out


def fit_motion(
    sources: list[PassSource], center: SkyCoord, floor_arcsec: float = 0.0
) -> tuple[float, float, float, float, float, float, float]:
    """Weighted linear fit of position vs time.

    Returns (ra, dec, ref_mjd, pm_east, pm_north, pm_err, chi2_dof); pm in arcsec/yr.
    `floor_arcsec` is added in quadrature to every position error.
    """
    ra = np.array([s.ra for s in sources])
    dec = np.array([s.dec for s in sources])
    t = np.array([s.mjd for s in sources]) / DAYS_PER_YEAR
    sig = np.sqrt(np.array([s.sigma_arcsec for s in sources]) ** 2 + floor_arcsec**2)
    x, y = _offsets(center, ra, dec)
    w = 1 / sig**2
    tbar = float((w * t).sum() / w.sum())
    dt = t - tbar
    sxx = float((w * dt**2).sum())
    if sxx <= 0:
        raise ValueError("motion fit needs observations at two or more epochs")
    fits_ = []
    chi2 = 0.0
    for v in (x, y):
        vbar = float((w * v).sum() / w.sum())
        slope = float((w * dt * (v - vbar)).sum() / sxx)
        chi2 += float((w * (v - vbar - slope * dt) ** 2).sum())
        fits_.append((vbar, slope))
    dof = 2 * (len(sources) - 2)
    (x0, pmx), (y0, pmy) = fits_
    ra0, dec0 = _from_offsets(center, x0, y0)
    return ra0, dec0, tbar * DAYS_PER_YEAR, pmx, pmy, 1 / np.sqrt(sxx), chi2 / dof if dof > 0 else float("nan")


def parallax_factors(ra_deg: float, dec_deg: float, mjds) -> tuple[np.ndarray, np.ndarray]:
    """(east, north) displacement in arcsec per arcsec of parallax at each epoch.

    The observer is taken at the Earth's barycentric position (SPHEREx's low
    orbit differs by < 1e-4 AU). Displacement = -(observer offset projected on
    the sky) x parallax, i.e. the textbook X sin(a) - Y cos(a), etc.
    """
    from astropy.coordinates import get_body_barycentric
    from astropy.time import Time

    pos = get_body_barycentric("earth", Time(np.asarray(mjds, float), format="mjd")).xyz.to_value(u.AU)
    a, d = np.radians(ra_deg), np.radians(dec_deg)
    east = np.array([-np.sin(a), np.cos(a), 0.0])
    north = np.array([-np.sin(d) * np.cos(a), -np.sin(d) * np.sin(a), np.cos(d)])
    return -(east @ pos), -(north @ pos)


def fit_astrometry(sources: list[PassSource], center: SkyCoord, parallax: bool) -> dict:
    """Weighted least-squares fit of position, proper motion and optionally parallax.

    Parameters x0, y0 (arcsec offsets at the weighted mean epoch), pm_east,
    pm_north (arcsec/yr) and, with `parallax`, plx (arcsec, shared by both axes).
    """
    ra = np.array([s.ra for s in sources])
    dec = np.array([s.dec for s in sources])
    mjd = np.array([s.mjd for s in sources])
    sig = np.array([s.sigma_arcsec for s in sources])
    x, y = _offsets(center, ra, dec)
    w = 1 / sig**2
    t = mjd / DAYS_PER_YEAR
    tbar = float((w * t).sum() / w.sum())
    dt = t - tbar
    n = len(sources)
    k = 5 if parallax else 4
    A = np.zeros((2 * n, k))
    A[:n, 0] = 1
    A[n:, 1] = 1
    A[:n, 2] = dt
    A[n:, 3] = dt
    if parallax:
        fe, fn = parallax_factors(float(np.mean(ra)), float(np.mean(dec)), mjd)
        A[:n, 4] = fe
        A[n:, 4] = fn
    b = np.concatenate([x, y])
    W = np.concatenate([w, w])
    Aw = A * np.sqrt(W)[:, None]
    bw = b * np.sqrt(W)
    normal = Aw.T @ Aw
    if np.linalg.matrix_rank(normal) < k:
        raise ValueError("astrometric fit is degenerate for these epochs")
    cov = np.linalg.inv(normal)
    p = cov @ (Aw.T @ bw)
    chi2 = float(((bw - Aw @ p) ** 2).sum())
    dof = 2 * n - k
    ra0, dec0 = _from_offsets(center, p[0], p[1])
    return {
        "ra": ra0,
        "dec": dec0,
        "ref_mjd": tbar * DAYS_PER_YEAR,
        "pm_east": float(p[2]),
        "pm_north": float(p[3]),
        "pm_err": float(np.sqrt((cov[2, 2] + cov[3, 3]) / 2)),
        "plx": float(p[4]) if parallax else None,
        "plx_err": float(np.sqrt(cov[4, 4])) if parallax else None,
        "chi2": chi2,
        "dof": dof,
    }


def _line_pvalue(sources: list[PassSource], center: SkyCoord) -> float:
    """Probability that positions this far off a straight line arise by chance.

    Used to decide whether a pass's source continues a track. Parallax isn't
    modelled, so nearby objects (WISE 0855: 0.44") fit a line less well than
    their errors suggest; a p-value cut tolerates that better than a fixed
    reduced-chi2 cut, and adapts to how many passes there are.
    """
    chi2_dof = fit_motion(sources, center)[6]
    dof = 2 * (len(sources) - 2)
    if dof <= 0:
        return 1.0
    return float(chi2_dist.sf(chi2_dof * dof, dof))


def _flux_compatible(a: PassSource, b: PassSource, max_ratio: float) -> bool:
    common = set(a.flux_by_detector) & set(b.flux_by_detector)
    if not common:
        return True  # different detectors: brightness can't be compared
    for det in common:
        fa, fb = a.flux_by_detector[det][0], b.flux_by_detector[det][0]
        if fa <= 0 or fb <= 0 or max(fa, fb) / min(fa, fb) > max_ratio:
            return False
    return True


def _flux_kinds(d: Detection) -> list[tuple[str, float, float]]:
    """Brightness measurements usable for comparison: (kind, value, error).

    Kinds are only ever compared with themselves:
    - "D3": core flux of an unsaturated star.
    - "D3-wing" / "D3-wing2": saturated star, flux in an inner / outer
      annulus around the masked core.
    - "D3-satarea": saturated star, area of the saturated patch in pixels.
      Grows only ~logarithmically with flux, so its ratio understates the
      flux change, but it's available even for the brightest stars, whose
      patch swallows the annulus.
    """
    out = []
    if d.contaminated:
        return out
    if d.saturated:
        if np.isfinite(d.wing_flux) and d.wing_flux > 0:
            out.append((f"D{d.detector}-wing", d.wing_flux, d.wing_flux_err))
        if np.isfinite(d.wing2_flux) and d.wing2_flux > 0:
            out.append((f"D{d.detector}-wing2", d.wing2_flux, d.wing2_flux_err))
        if d.sat_area > 0:
            # Area of a patch is uncertain by ~ its perimeter in pixels.
            out.append((f"D{d.detector}-satarea", float(d.sat_area), 2 * np.sqrt(np.pi * d.sat_area)))
    elif np.isfinite(d.flux) and d.flux > 0:
        out.append((f"D{d.detector}", d.flux, d.flux_err))
    return out


def _variability(
    obj_sources: list[PassSource], calib_frac: float, wave_tol: float = 0.01, min_pairs: int = 5
) -> dict | None:
    """Brightness change between passes, from frames at matching wavelengths.

    SPHEREx samples each spot at many wavelengths per pass, and stars' spectra
    (especially Miras') vary strongly across a detector's band. So detections
    are paired across passes only at the same detector, measurement kind and
    wavelength (within `wave_tol`, fractional); the median log flux ratio of
    those pairs, with a robust error, is the measured change.
    """
    best = None
    for a_i, a in enumerate(obj_sources):
        for b in obj_sources[a_i + 1:]:
            logs: dict[str, list[tuple[float, float]]] = {}
            for da in a.detections:
                if not np.isfinite(da.wavelength_um):
                    continue
                for db in b.detections:
                    if not np.isfinite(db.wavelength_um):
                        continue
                    if abs(db.wavelength_um - da.wavelength_um) > wave_tol * da.wavelength_um:
                        continue
                    kb = {k: (v, e) for k, v, e in _flux_kinds(db)}
                    for kind, va, ea in _flux_kinds(da):
                        if kind in kb:
                            vb, eb = kb[kind]
                            logs.setdefault(kind, []).append(
                                (float(np.log(vb / va)), float(np.hypot(ea / va, eb / vb)))
                            )
            for kind, pairs in logs.items():
                if len(pairs) < min_pairs:
                    continue
                lr = np.array([p[0] for p in pairs])
                med = float(np.median(lr))
                # Error of the median: the larger of the observed scatter and
                # the measurements' own noise (a few pairs can scatter little
                # by chance), plus the calibration floor.
                robust = 1.4826 * float(np.median(np.abs(lr - med)))
                formal = float(np.median([p[1] for p in pairs]))
                err = float(np.hypot(1.2533 * max(robust, formal) / np.sqrt(len(lr)), calib_frac))
                signif = abs(med) / err if err > 0 else 0.0
                if best is None or signif > best["significance"]:
                    best = {
                        "detector": kind,
                        "ratio": float(np.exp(abs(med))),
                        "significance": float(signif),
                        "n_pairs": len(lr),
                        # brightness of the later pass relative to the earlier
                        "change": f"P{b.pass_index + 1}/P{a.pass_index + 1} = {np.exp(med):.2f}",
                        "flux_by_pass": {a.pass_index: 1.0, b.pass_index: float(np.exp(med))},
                    }
    return best


def _grow(
    groups: list[list[PassSource]],
    leftovers: list[PassSource],
    center: SkyCoord,
    max_pm_arcsec_yr: float,
    max_flux_ratio: float,
    chain_pvalue: float,
) -> list[PassSource]:
    """Attach single-pass sources to objects they continue on a straight line.

    A fast star can be linked across two passes and missed in a third (e.g.
    its shift between two passes fell inside the static radius but not the
    third). Mutates `groups`; returns the sources still unattached.
    """
    remaining = list(leftovers)
    changed = True
    while changed and remaining:
        changed = False
        for g in groups:
            have = {s.pass_index for s in g}
            best, best_p = None, chain_pvalue
            ox, oy = _offsets(center, np.array([s.ra for s in g]), np.array([s.dec for s in g]))
            gx, gy = np.array([ox.mean()]), np.array([oy.mean()])  # tangent plane: safe at RA 0/360
            gmjd = float(np.mean([s.mjd for s in g]))
            for s in remaining:
                if s.pass_index in have:
                    continue
                sx, sy = _offsets(center, s.ra, s.dec)
                reach = max_pm_arcsec_yr * abs(s.mjd - gmjd) / DAYS_PER_YEAR + 3 * s.sigma_arcsec
                if np.hypot(sx[0] - gx[0], sy[0] - gy[0]) > reach:
                    continue
                if not all(_flux_compatible(s, o, max_flux_ratio) for o in g):
                    continue
                p = _line_pvalue(g + [s], center)
                if p >= best_p:
                    best, best_p = s, p
            if best is not None:
                g.append(best)
                remaining.remove(best)
                changed = True
    return remaining


def _merge_tracks(
    static: list[list[PassSource]],
    movers: list[list[PassSource]],
    center: SkyCoord,
    max_pm_arcsec_yr: float,
    max_flux_ratio: float,
    pvalue: float,
) -> tuple[list[list[PassSource]], list[list[PassSource]]]:
    """Merge pairs of tracks with no pass in common that one line fits.

    Returns (static groups, mover groups); merged tracks count as movers.
    """
    tracks = [(g, False) for g in static] + [(g, True) for g in movers]
    while True:
        best = None
        for i in range(len(tracks)):
            gi = tracks[i][0]
            pi = {s.pass_index for s in gi}
            for j in range(i + 1, len(tracks)):
                gj = tracks[j][0]
                if pi & {s.pass_index for s in gj}:
                    continue
                a = min(gi + gj, key=lambda s: s.mjd)
                b = max(gi + gj, key=lambda s: s.mjd)
                ax, ay = _offsets(center, a.ra, a.dec)
                bx, by = _offsets(center, b.ra, b.dec)
                reach = max_pm_arcsec_yr * (b.mjd - a.mjd) / DAYS_PER_YEAR + 3 * np.hypot(a.sigma_arcsec, b.sigma_arcsec)
                if np.hypot(bx[0] - ax[0], by[0] - ay[0]) > reach:
                    continue
                if not all(_flux_compatible(x, y, max_flux_ratio) for x in gi for y in gj):
                    continue
                p = _line_pvalue(gi + gj, center)
                if p >= pvalue and (best is None or p > best[0]):
                    best = (p, i, j)
        if best is None:
            break
        _, i, j = best
        merged = (tracks[i][0] + tracks[j][0], True)
        tracks = [t for k, t in enumerate(tracks) if k not in (i, j)] + [merged]
    return [g for g, m in tracks if not m], [g for g, m in tracks if m]


def _missing_passes(
    obj: TrackedObject,
    frames: list[Frame],
    passes: list[list[int]],
    center: SkyCoord,
    nsigma: float,
) -> list[int]:
    """Passes that covered the object's predicted position but didn't see it.

    Only counts when the object is bright enough that it should have been
    detected there (best single-detection SNR >= 2 * nsigma).
    """
    if max(s.snr for s in obj.sources) < 2 * nsigma:
        return []
    have = {s.pass_index for s in obj.sources}
    missing = []
    x0, y0 = _offsets(center, obj.ra, obj.dec)
    for pi, ids in enumerate(passes):
        if pi in have:
            continue
        mjd = float(np.mean([frames[i].mjd for i in ids]))
        dt = (mjd - obj.ref_mjd) / DAYS_PER_YEAR
        ra, dec = _from_offsets(center, x0[0] + obj.pm_ra_cosdec * dt, y0[0] + obj.pm_dec * dt)
        covering = sum(_covers(frames[i], ra, dec) for i in ids)
        if covering >= max(1, 0.5 * len(ids)):
            missing.append(pi)
    return missing


def _caution_near_bright(objects: list[TrackedObject], center: SkyCoord) -> None:
    """Annotate variables close to very bright (heavily saturated) stars.

    Such stars' scattered light and ghosts change with telescope orientation
    and with the star's own brightness, which can mimic variability nearby
    (seen around R Draconis). The flag stays; the reader gets a warning.
    """
    bright = [
        o for o in objects
        if max((d.sat_area for s in o.sources for d in s.detections), default=0) >= BRIGHT_SAT_AREA_PX
    ]
    if not bright:
        return
    for o in objects:
        if not o.is_variable or o in bright:
            continue
        ox, oy = _offsets(center, o.ra, o.dec)
        for b in bright:
            bx, by = _offsets(center, b.ra, b.dec)
            sep = float(np.hypot(ox[0] - bx[0], oy[0] - by[0]))
            if sep <= BRIGHT_CAUTION_ARCSEC:
                o.variability["caution"] = (
                    f"{sep:.0f}\" from a very bright star; its scattered light may cause this"
                )
                break


def find_motion(
    frames: list[Frame],
    *,
    nsigma: float = 5.0,
    pass_gap_days: float = 60.0,
    pass_max_days: float = MAX_PASS_DAYS,
    max_pm_arcsec_yr: float = 15.0,
    static_arcsec: float = 2.0,
    min_detect_frac: float = 0.5,
    mover_sigma: float = 5.0,
    max_flux_ratio: float = 3.0,
    variable_ratio: float = 1.25,
    variable_sigma: float = 5.0,
    calib_frac: float = 0.05,
    chain_pvalue: float = 1e-3,
    stack: bool = False,
) -> EngineResult:
    """Run the full pipeline on frames of one patch of sky (any order, any detectors).

    With `stack=True`, each pass's aligned frames are co-added and sources are
    found on the stacks: deeper (faint brown dwarfs), but saturated-star and
    per-detector flux information is coarser.
    """
    if not frames:
        raise ValueError("no frames")
    notes: list[str] = []
    ny, nx = frames[0].data.shape
    c_ra, c_dec = frames[0].wcs.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)
    center = SkyCoord(float(c_ra) * u.deg, float(c_dec) * u.deg)
    pix = float(np.median([f.pixel_scale_arcsec for f in frames]))

    passes = group_into_passes([f.mjd for f in frames], pass_gap_days, pass_max_days)
    usable = []
    for f in frames:
        try:
            frame_background(f)
            usable.append(f)
        except ValueError:
            pass
    if len(usable) < len(frames):
        notes.append(f"skipped {len(frames) - len(usable)} frames with too few usable pixels")
        frames = usable
        if not frames:
            raise ValueError("no frame has enough usable pixels")
        passes = group_into_passes([f.mjd for f in frames], pass_gap_days, pass_max_days)
    detections = [detect_sources(f, i, nsigma) for i, f in enumerate(frames)]
    offsets, align_notes = align_frames(detections, center)
    notes += align_notes
    input_frames, input_passes = frames, passes
    if stack:
        n = int(round(max(frames[0].data.shape)))
        grid = stack_grid(center, n, pix)
        frames = [
            stack_pass([input_frames[i] for i in ids], [offsets[i] for i in ids], grid, (n, n))
            for ids in input_passes
        ]
        passes = [[k] for k in range(len(frames))]
        detections = [detect_sources(f, k, nsigma) for k, f in enumerate(frames)]
        offsets = [(0.0, 0.0)] * len(frames)
    if len(passes) < 2:
        notes.append("only one pass: motion between passes cannot be measured")

    link = 0.7 * pix  # within one pass a star sits still to well under a pixel
    per_pass = [
        _merge_pass(pi, ids, frames, detections, center, link, min_detect_frac)
        for pi, ids in enumerate(passes)
    ]

    # --- static linking -------------------------------------------------------
    groups: list[list[PassSource]] = []
    gx: list[float] = []
    gy: list[float] = []
    for srcs in per_pass:
        claimed: set[int] = set()
        for s in sorted(srcs, key=lambda s: -s.snr):
            x, y = _offsets(center, s.ra, s.dec)
            radius = max(static_arcsec, 3 * s.sigma_arcsec)
            best, best_d = -1, np.inf
            for gi, (px, py) in enumerate(zip(gx, gy)):
                if gi in claimed:
                    continue
                d = float(np.hypot(px - x[0], py - y[0]))
                if d < best_d:
                    best, best_d = gi, d
            if best >= 0 and best_d <= radius:
                groups[best].append(s)
                claimed.add(best)
            else:
                groups.append([s])
                gx.append(float(x[0]))
                gy.append(float(y[0]))
                claimed.add(len(groups) - 1)

    multi = [g for g in groups if len(g) >= 2]
    singles = [g[0] for g in groups if len(g) == 1]

    # --- mover linking among sources seen in only one pass ---------------------
    # A single-pass source only counts as "missing" elsewhere if the other pass
    # actually covered that spot; otherwise it's just off the edge.
    def missing_elsewhere(s: PassSource) -> bool:
        return any(
            any(_covers(frames[fi], s.ra, s.dec) for fi in passes[pj])
            for pj in range(len(passes))
            if pj != s.pass_index
        )

    pool = [s for s in singles if missing_elsewhere(s)]
    used: set[int] = set()
    mover_groups: list[list[PassSource]] = []
    candidates: list[tuple[int, float, list[int]]] = []
    for i, a in enumerate(pool):
        for j, b in enumerate(pool):
            if b.pass_index <= a.pass_index:
                continue
            dt_yr = (b.mjd - a.mjd) / DAYS_PER_YEAR
            ax, ay = _offsets(center, a.ra, a.dec)
            bx, by = _offsets(center, b.ra, b.dec)
            sep = float(np.hypot(bx[0] - ax[0], by[0] - ay[0]))
            if sep > max_pm_arcsec_yr * dt_yr + 3 * np.hypot(a.sigma_arcsec, b.sigma_arcsec):
                continue
            if not _flux_compatible(a, b, max_flux_ratio):
                continue
            chain = [i, j]
            # Extend pass by pass with the best-fitting source in each later
            # pass (not the first acceptable one, which could be a decoy).
            for pk in range(b.pass_index + 1, len(passes)):
                best_k, best_p = None, chain_pvalue
                last = pool[chain[-1]]
                for k, c in enumerate(pool):
                    if c.pass_index != pk or not _flux_compatible(last, c, max_flux_ratio):
                        continue
                    p = _line_pvalue([pool[m] for m in chain] + [c], center)
                    if p >= best_p:
                        best_k, best_p = k, p
                if best_k is not None:
                    chain.append(best_k)
            candidates.append((len(chain), sep, chain))
    # Prefer longer chains, then shorter jumps (less chance of a chance pairing).
    for _, _, chain in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if any(k in used for k in chain):
            continue
        used.update(chain)
        mover_groups.append([pool[k] for k in chain])
    leftovers = [s for k, s in enumerate(pool) if k not in used]
    unlinked = _grow(multi + mover_groups, leftovers, center, max_pm_arcsec_yr, max_flux_ratio, chain_pvalue)
    unlinked += [s for s in singles if s not in pool]
    # A slow mover's early passes can link to each other as "static" and its
    # late passes likewise; join such pieces when one line fits them all.
    multi, merged = _merge_tracks(multi, mover_groups, center, max_pm_arcsec_yr, max_flux_ratio, chain_pvalue)
    mover_groups = merged

    # --- systematic floor from static objects --------------------------------
    raw = []
    for g in multi:
        try:
            raw.append(fit_motion(g, center))
        except ValueError:
            continue
    floor_pm = 0.0
    if len(raw) >= 5:
        pms = np.hypot([r[3] for r in raw], [r[4] for r in raw])
        # Static stars' apparent motions are what alignment and centroid
        # systematics alone produce. Their 2-D magnitude is Rayleigh-distributed,
        # whose median is sigma*sqrt(ln 4); invert for the per-axis sigma.
        floor_pm = float(np.median(pms) / np.sqrt(np.log(4)))
    else:
        notes.append("too few static stars to measure the systematic floor")

    objects: list[TrackedObject] = []
    for g, kind in [(g, "static") for g in multi] + [(g, "mover") for g in mover_groups]:
        try:
            fit = fit_astrometry(g, center, parallax=False)
        except ValueError:
            continue
        # Nearby objects wobble with the Earth's orbit (WISE 0855: 0.44"), which
        # a straight line can't follow. Add parallax when it explains a poor
        # fit: one extra parameter must cut chi2 by > 4 (~2 sigma).
        if fit["dof"] >= 2 and fit["chi2"] > fit["dof"]:
            try:
                plx_fit = fit_astrometry(g, center, parallax=True)
                # Only a physical (positive, significant) parallax counts as an
                # explanation; otherwise the misfit stays and inflates errors.
                if (
                    fit["chi2"] - plx_fit["chi2"] > 4
                    and plx_fit["plx"] > 2 * plx_fit["plx_err"]
                ):
                    fit = plx_fit
            except ValueError:
                pass
        ra0, dec0, ref = fit["ra"], fit["dec"], fit["ref_mjd"]
        pmx, pmy, pm_err = fit["pm_east"], fit["pm_north"], fit["pm_err"]
        chi2 = fit["chi2"] / fit["dof"] if fit["dof"] > 0 else float("nan")
        if np.isfinite(chi2):
            pm_err *= np.sqrt(max(1.0, chi2))  # a poor fit means larger errors
        pm_err = float(np.hypot(pm_err, floor_pm))
        obj = TrackedObject(
            sources=sorted(g, key=lambda s: s.mjd),
            link=kind,
            ra=ra0,
            dec=dec0,
            ref_mjd=ref,
            pm_ra_cosdec=pmx,
            pm_dec=pmy,
            pm_err=pm_err,
            chi2_dof=chi2,
            parallax=fit["plx"],
            parallax_err=fit["plx_err"],
        )
        obj.missing_passes = _missing_passes(obj, frames, passes, center, nsigma)
        obj_span_yr = (obj.sources[-1].mjd - obj.sources[0].mjd) / DAYS_PER_YEAR
        # A mover must beat its errors, shift by > ~1 px over its own baseline,
        # and not be absent from a pass that covered its predicted position
        # (that pattern is a chance pairing of unrelated sources, not a track).
        # Two points always fit a line, so with 3+ passes a mover needs 3+;
        # with only 2 passes, demand twice the significance instead.
        needed_sigma = mover_sigma if len(obj.sources) >= 3 else 2 * mover_sigma
        obj.is_mover = (
            obj.pm_significance >= needed_sigma
            and obj.pm_total * obj_span_yr >= pix
            and not obj.missing_passes
            and len(obj.sources) >= min(3, len(passes))
        )
        var = _variability(obj.sources, calib_frac)
        obj.variability = var
        obj.is_variable = bool(
            var and var["ratio"] >= variable_ratio and var["significance"] >= variable_sigma
        )
        objects.append(obj)

    _caution_near_bright(objects, center)
    return EngineResult(
        frames=frames,
        passes=passes,
        input_frames=input_frames,
        input_passes=input_passes,
        stacked=stack,
        detections=detections,
        frame_offsets_arcsec=offsets,
        objects=objects,
        unlinked=unlinked,
        pm_floor_arcsec_yr=floor_pm,
        notes=notes,
    )
