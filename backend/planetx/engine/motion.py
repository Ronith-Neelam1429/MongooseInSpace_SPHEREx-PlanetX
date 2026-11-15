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

import dataclasses
import hashlib
import math
from dataclasses import dataclass, field

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from scipy.spatial import cKDTree
from scipy.special import chdtrc

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
_ARCSEC_PER_RAD = float(u.rad.to(u.arcsec))
# Neighbour searches ask the k-d tree for slightly more than the radius and then
# apply the exact test, so the tree's own rounding never decides a borderline case.
_TREE_MARGIN = 1 + 1e-9


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
    frames: list[Frame]  # frames analysed: the usable inputs in time order, or one stack per pass
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


class SkyPlane:
    """Offsets (east, north) in arcsec from a centre on the sky, and back.

    The rotation behind astropy's `SkyCoord.spherical_offsets_to` and
    `spherical_offsets_by`, in plain numpy (equal to 1e-9 arcsec): building
    coordinate objects for every call used to take most of the engine's time.
    """

    def __init__(self, ra_deg: float, dec_deg: float):
        a, d = np.radians(ra_deg), np.radians(dec_deg)
        # Rows: unit vectors towards the centre, east of it and north of it.
        self._rotation = np.array([
            [np.cos(d) * np.cos(a), np.cos(d) * np.sin(a), np.sin(d)],
            [-np.sin(a), np.cos(a), 0.0],
            [-np.sin(d) * np.cos(a), -np.sin(d) * np.sin(a), np.cos(d)],
        ])

    def offsets(self, ra, dec) -> tuple[np.ndarray, np.ndarray]:
        """(east, north) in arcsec of positions given in degrees."""
        x, y, z = self._rotation @ _unit_vectors(np.radians(ra), np.radians(dec))
        return np.arctan2(y, x) * _ARCSEC_PER_RAD, np.arctan2(z, np.hypot(x, y)) * _ARCSEC_PER_RAD

    def sky(self, east, north) -> tuple[np.ndarray, np.ndarray]:
        """(ra, dec) in degrees of offsets given in arcsec."""
        x, y, z = self._rotation.T @ _unit_vectors(
            np.asarray(east) / _ARCSEC_PER_RAD, np.asarray(north) / _ARCSEC_PER_RAD
        )
        return np.degrees(np.arctan2(y, x)) % 360.0, np.degrees(np.arctan2(z, np.hypot(x, y)))


def _unit_vectors(lon, lat) -> np.ndarray:
    return np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def _plane(center: SkyCoord | SkyPlane) -> SkyPlane:
    """The engine passes its `SkyPlane` around; other callers may give a SkyCoord."""
    return center if isinstance(center, SkyPlane) else SkyPlane(center.ra.deg, center.dec.deg)


def _source_offsets(plane: SkyPlane, sources: list[PassSource]) -> tuple[np.ndarray, np.ndarray]:
    return plane.offsets(np.array([s.ra for s in sources]), np.array([s.dec for s in sources]))


def _covers(frame: Frame, ra, dec, margin_px: float = 2.0) -> np.ndarray:
    """Whether the frame observed each position (degrees), as a boolean array."""
    x, y = frame.wcs.all_world2pix(np.atleast_1d(ra), np.atleast_1d(dec), 0)
    ny, nx = frame.data.shape
    inside = (margin_px <= x) & (x <= nx - 1 - margin_px) & (margin_px <= y) & (y <= ny - 1 - margin_px)
    iy, ix = np.rint(y[inside]).astype(int), np.rint(x[inside]).astype(int)
    seen = np.isfinite(frame.data[iy, ix])
    if frame.saturated is not None:
        seen |= frame.saturated[iy, ix]  # a saturated star's own pixels are NaN but the spot was observed
    covered = np.zeros(x.shape, bool)
    covered[inside] = seen
    return covered


def align_frames(
    detections: list[list[Detection]],
    center: SkyCoord | SkyPlane,
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
    plane = _plane(center)
    rx, ry = plane.offsets(np.array([d.ra for d in ref]), np.array([d.dec for d in ref]))
    ref_tree = cKDTree(np.column_stack([rx, ry]))
    shifts: list[tuple[float, float]] = []
    for i, dets in enumerate(detections):
        if i == ref_i or not dets:
            shifts.append((0.0, 0.0))
            continue
        fx, fy = plane.offsets(np.array([d.ra for d in dets]), np.array([d.dec for d in dets]))
        _, j = ref_tree.query(np.column_stack([fx, fy]))  # nearest reference star
        dx, dy = rx[j] - fx, ry[j] - fy
        matched = np.hypot(dx, dy) <= match_arcsec
        if matched.sum() < min_stars:
            unaligned += 1
            shifts.append((0.0, 0.0))
            continue
        sx, sy = float(np.median(dx[matched])), float(np.median(dy[matched]))
        shifts.append((sx, sy))
        ra, dec = plane.sky(fx + sx, fy + sy)
        for d, a, b in zip(dets, ra, dec):
            d.ra, d.dec = float(a), float(b)
    if unaligned:
        notes.append(
            f"{unaligned} of {len(detections)} frames had fewer than {min_stars} stars "
            "matching the reference frame and were left unaligned (sparse field)"
        )
    return shifts, notes


def _cluster(xs: np.ndarray, ys: np.ndarray, frame_of: list[int], link_arcsec: float) -> list[list[int]]:
    """Group one pass's detections (given brightest first) into objects.

    Each detection joins the nearest cluster seed within `link_arcsec`, unless
    that cluster already holds a detection from the same frame; otherwise it
    seeds a new cluster.
    """
    xy = np.column_stack([xs, ys])
    nearby = cKDTree(xy).query_ball_point(xy, link_arcsec * _TREE_MARGIN)
    cluster_of_seed: dict[int, int] = {}
    clusters: list[list[int]] = []
    frames_in: list[set[int]] = []
    for k in range(len(xs)):
        seeds = sorted(j for j in nearby[k] if j in cluster_of_seed)  # oldest cluster first, as ties go to it
        if seeds:
            dist = np.hypot(xs[seeds] - xs[k], ys[seeds] - ys[k])
            nearest = int(np.argmin(dist))
            c = cluster_of_seed[seeds[nearest]]
            if dist[nearest] <= link_arcsec and frame_of[k] not in frames_in[c]:
                clusters[c].append(k)
                frames_in[c].add(frame_of[k])
                continue
        cluster_of_seed[k] = len(clusters)
        clusters.append([k])
        frames_in.append({frame_of[k]})
    return clusters


def _merge_pass(
    pass_index: int,
    frame_ids: list[int],
    frames: list[Frame],
    detections: list[list[Detection]],
    plane: SkyPlane,
    link_arcsec: float,
    min_detect_frac: float,
) -> list[PassSource]:
    """Average each object's detections within one pass."""
    dets = [d for fi in frame_ids for d in detections[fi]]
    dets.sort(key=lambda d: -d.snr)  # brightest seed clusters first
    if not dets:
        return []
    xs, ys = plane.offsets(np.array([d.ra for d in dets]), np.array([d.dec for d in dets]))
    clusters = _cluster(xs, ys, [d.frame_index for d in dets], link_arcsec)

    centres = np.empty((len(clusters), 2))
    weights = []
    for c, members in enumerate(clusters):
        w = 1 / np.array([dets[m].sigma_arcsec for m in members]) ** 2
        centres[c] = (w * xs[members]).sum() / w.sum(), (w * ys[members]).sum() / w.sum()
        weights.append(w)
    ras, decs = plane.sky(centres[:, 0], centres[:, 1])
    covered = np.array([_covers(frames[fi], ras, decs) for fi in frame_ids])  # [frame, cluster]

    out: list[PassSource] = []
    for c, members in enumerate(clusters):
        mx, my = xs[members], ys[members]
        w = weights[c]
        formal = float(1 / np.sqrt(w.sum()))
        n = len(members)
        scatter = float(np.hypot(mx.std(), my.std()) / np.sqrt(2 * n)) if n > 2 else 0.0
        # Alignment leaves ~0.1-0.3" residuals per pass (measured from static
        # stars), which averaging many frames of a bright star can't beat.
        sigma = float(np.hypot(max(formal, scatter), PASS_FLOOR_ARCSEC))
        if any(dets[m].saturated for m in members):
            # Saturated-patch centres share a bias that averaging can't remove.
            sigma = max(sigma, SATURATED_PASS_FLOOR_PX * frames[frame_ids[0]].pixel_scale_arcsec)
        ra, dec = float(ras[c]), float(decs[c])
        covering = [fi for fi, seen in zip(frame_ids, covered[:, c]) if seen]
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


def _fit_line(x, y, t, sig):
    """Weighted straight-line fit of offsets (arcsec) against time (years).

    Works along the last axis, so the rows of 2-D arrays are separate tracks.
    Returns (tbar, (x0, pm_x), (y0, pm_y), sxx, chi2), with x0/y0 at time tbar.
    """
    w = 1 / sig**2
    wsum = w.sum(-1)
    tbar = (w * t).sum(-1) / wsum
    dt = t - tbar[..., None]
    sxx = (w * dt**2).sum(-1)
    if np.any(sxx <= 0):
        raise ValueError("motion fit needs observations at two or more epochs")
    axes = []
    chi2 = 0.0
    for v in (x, y):
        vbar = (w * v).sum(-1) / wsum
        slope = (w * dt * (v - vbar[..., None])).sum(-1) / sxx
        chi2 = chi2 + (w * (v - vbar[..., None] - slope[..., None] * dt) ** 2).sum(-1)
        axes.append((vbar, slope))
    return tbar, axes[0], axes[1], sxx, chi2


def fit_motion(
    sources: list[PassSource], center: SkyCoord | SkyPlane, floor_arcsec: float = 0.0
) -> tuple[float, float, float, float, float, float, float]:
    """Weighted linear fit of position vs time.

    Returns (ra, dec, ref_mjd, pm_east, pm_north, pm_err, chi2_dof); pm in arcsec/yr.
    `floor_arcsec` is added in quadrature to every position error.
    """
    plane = _plane(center)
    t = np.array([s.mjd for s in sources]) / DAYS_PER_YEAR
    sig = np.sqrt(np.array([s.sigma_arcsec for s in sources]) ** 2 + floor_arcsec**2)
    x, y = _source_offsets(plane, sources)
    tbar, (x0, pmx), (y0, pmy), sxx, chi2 = _fit_line(x, y, t, sig)
    dof = 2 * (len(sources) - 2)
    ra0, dec0 = plane.sky(x0, y0)
    return (
        float(ra0), float(dec0), float(tbar) * DAYS_PER_YEAR, float(pmx), float(pmy),
        float(1 / np.sqrt(sxx)), float(chi2) / dof if dof > 0 else float("nan"),
    )


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


def fit_astrometry(sources: list[PassSource], center: SkyCoord | SkyPlane, parallax: bool) -> dict:
    """Weighted least-squares fit of position, proper motion and optionally parallax.

    Parameters x0, y0 (arcsec offsets at the weighted mean epoch), pm_east,
    pm_north (arcsec/yr) and, with `parallax`, plx (arcsec, shared by both axes).
    """
    ra = np.array([s.ra for s in sources])
    dec = np.array([s.dec for s in sources])
    mjd = np.array([s.mjd for s in sources])
    sig = np.array([s.sigma_arcsec for s in sources])
    plane = _plane(center)
    x, y = plane.offsets(ra, dec)
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
    ra0, dec0 = plane.sky(p[0], p[1])
    return {
        "ra": float(ra0),
        "dec": float(dec0),
        "ref_mjd": tbar * DAYS_PER_YEAR,
        "pm_east": float(p[2]),
        "pm_north": float(p[3]),
        "pm_err": float(np.sqrt((cov[2, 2] + cov[3, 3]) / 2)),
        "plx": float(p[4]) if parallax else None,
        "plx_err": float(np.sqrt(cov[4, 4])) if parallax else None,
        "chi2": chi2,
        "dof": dof,
    }


def _line_pvalues(x, y, t, sig) -> np.ndarray:
    """Probability that positions this far off a straight line arise by chance.

    Arguments as for `_fit_line`; one p-value per track.
    """
    chi2 = _fit_line(x, y, t, sig)[4]
    dof = 2 * (np.shape(x)[-1] - 2)
    return chdtrc(dof, chi2) if dof > 0 else np.ones(np.shape(chi2))


def _line_pvalue(sources: list[PassSource], plane: SkyPlane) -> float:
    """`_line_pvalues` for one track of pass sources.

    Used to decide whether a pass's source continues a track. Parallax isn't
    modelled, so nearby objects (WISE 0855: 0.44") fit a line less well than
    their errors suggest; a p-value cut tolerates that better than a fixed
    reduced-chi2 cut, and adapts to how many passes there are.
    """
    x, y = _source_offsets(plane, sources)
    t = np.array([s.mjd for s in sources]) / DAYS_PER_YEAR
    return float(_line_pvalues(x, y, t, np.array([s.sigma_arcsec for s in sources])))


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


def _brightness_epochs(sources: list[PassSource], epoch_of_pass: list[int]) -> list[PassSource]:
    """A track's sources merged per brightness epoch (time-ordered input).

    Each merged source keeps the first pass's fields (so its `pass_index`
    names the epoch's first pass) and holds all the epoch's detections.
    """
    merged: dict[int, PassSource] = {}
    for s in sources:
        e = epoch_of_pass[s.pass_index]
        if e in merged:
            merged[e] = dataclasses.replace(merged[e], detections=merged[e].detections + s.detections)
        else:
            merged[e] = s
    return list(merged.values())


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
    plane: SkyPlane,
    max_pm_arcsec_yr: float,
    max_flux_ratio: float,
    chain_pvalue: float,
) -> list[PassSource]:
    """Attach single-pass sources to objects they continue on a straight line.

    A fast star can be linked across two passes and missed in a third (e.g.
    its shift between two passes fell inside the static radius but not the
    third). Mutates `groups`; returns the sources still unattached.
    """
    if not leftovers:
        return []
    lx, ly = _source_offsets(plane, leftovers)
    tree = cKDTree(np.column_stack([lx, ly]))
    mjds = [s.mjd for s in leftovers]
    first, last = min(mjds), max(mjds)
    widest = 3 * max(s.sigma_arcsec for s in leftovers)
    free = [True] * len(leftovers)
    changed = True
    while changed and any(free):
        changed = False
        for g in groups:
            have = {s.pass_index for s in g}
            best, best_p = None, chain_pvalue
            ox, oy = _source_offsets(plane, g)
            gx, gy = ox.mean(), oy.mean()  # offsets, not RA/Dec: safe at RA 0/360
            gmjd = float(np.mean([s.mjd for s in g]))
            # No leftover can be reached from farther away than this.
            bound = max_pm_arcsec_yr * max(last - gmjd, gmjd - first) / DAYS_PER_YEAR + widest
            for k in sorted(tree.query_ball_point([gx, gy], bound * _TREE_MARGIN)):
                s = leftovers[k]
                if not free[k] or s.pass_index in have:
                    continue
                reach = max_pm_arcsec_yr * abs(s.mjd - gmjd) / DAYS_PER_YEAR + 3 * s.sigma_arcsec
                if np.hypot(lx[k] - gx, ly[k] - gy) > reach:
                    continue
                if not all(_flux_compatible(s, o, max_flux_ratio) for o in g):
                    continue
                p = _line_pvalue(g + [s], plane)
                if p >= best_p:
                    best, best_p = k, p
            if best is not None:
                g.append(leftovers[best])
                free[best] = False
                changed = True
    return [s for s, is_free in zip(leftovers, free) if is_free]


def _merge_tracks(
    static: list[list[PassSource]],
    movers: list[list[PassSource]],
    plane: SkyPlane,
    max_pm_arcsec_yr: float,
    max_flux_ratio: float,
    pvalue: float,
) -> tuple[list[list[PassSource]], list[list[PassSource]]]:
    """Merge pairs of tracks with no pass in common that one line fits.

    Returns (static groups, mover groups); merged tracks count as movers.
    """
    tracks = [(g, False) for g in static] + [(g, True) for g in movers]
    sources = [s for g, _ in tracks for s in g]
    if not sources:
        return [], []
    xy = dict(zip(map(id, sources), zip(*_source_offsets(plane, sources))))
    every_pass = 0
    for s in sources:
        every_pass |= 1 << s.pass_index
    while True:
        # A track seen in every pass shares a pass with any other track, so only
        # the rest can merge: in an ordinary field that leaves a handful.
        partial = []
        for i, (g, _) in enumerate(tracks):
            seen = 0
            for s in g:
                seen |= 1 << s.pass_index
            if seen != every_pass:
                ends = min(g, key=lambda s: s.mjd), max(g, key=lambda s: s.mjd)
                partial.append((i, seen, ends))
        best = None
        for n, (i, seen_i, (first_i, last_i)) in enumerate(partial):
            for j, seen_j, (first_j, last_j) in partial[n + 1:]:
                if seen_i & seen_j:
                    continue
                a = first_j if first_j.mjd < first_i.mjd else first_i
                b = last_j if last_j.mjd > last_i.mjd else last_i
                (ax, ay), (bx, by) = xy[id(a)], xy[id(b)]
                reach = max_pm_arcsec_yr * (b.mjd - a.mjd) / DAYS_PER_YEAR + 3 * np.hypot(a.sigma_arcsec, b.sigma_arcsec)
                if np.hypot(bx - ax, by - ay) > reach:
                    continue
                gi, gj = tracks[i][0], tracks[j][0]
                if not all(_flux_compatible(x, y, max_flux_ratio) for x in gi for y in gj):
                    continue
                p = _line_pvalue(gi + gj, plane)
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
    plane: SkyPlane,
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
    x0, y0 = plane.offsets(obj.ra, obj.dec)
    for pi, ids in enumerate(passes):
        if pi in have:
            continue
        mjd = float(np.mean([frames[i].mjd for i in ids]))
        dt = (mjd - obj.ref_mjd) / DAYS_PER_YEAR
        ra, dec = plane.sky(x0 + obj.pm_ra_cosdec * dt, y0 + obj.pm_dec * dt)
        covering = sum(bool(_covers(frames[i], ra, dec)[0]) for i in ids)
        if covering >= max(1, 0.5 * len(ids)):
            missing.append(pi)
    return missing


def _caution_near_bright(objects: list[TrackedObject], plane: SkyPlane) -> None:
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
        ox, oy = plane.offsets(o.ra, o.dec)
        for b in bright:
            bx, by = plane.offsets(b.ra, b.dec)
            sep = float(np.hypot(ox - bx, oy - by))
            if sep <= BRIGHT_CAUTION_ARCSEC:
                o.variability["caution"] = (
                    f"{sep:.0f}\" from a very bright star; its scattered light may cause this"
                )
                break


def _link_static(
    per_pass: list[list[PassSource]], plane: SkyPlane, static_arcsec: float
) -> list[list[PassSource]]:
    """Link each pass's sources to the objects earlier passes found at the same spot.

    Brightest first, a source joins the nearest object no other source of its
    pass has taken, if that lies within its radius; otherwise it starts a new
    object. An object's position stays that of its first source.
    """
    groups: list[list[PassSource]] = []
    anchors: list[tuple[float, float]] = []
    for srcs in per_pass:
        if not srcs:
            continue
        srcs = sorted(srcs, key=lambda s: -s.snr)
        x, y = _source_offsets(plane, srcs)
        radius = np.array([max(static_arcsec, 3 * s.sigma_arcsec) for s in srcs])
        known = np.array(anchors).reshape(-1, 2)  # objects of earlier passes
        if len(known):
            nearby = cKDTree(known).query_ball_point(np.column_stack([x, y]), radius * _TREE_MARGIN)
        else:
            nearby = [[] for _ in srcs]
        claimed: set[int] = set()
        for k, s in enumerate(srcs):
            free = sorted(g for g in nearby[k] if g not in claimed)  # oldest object first, as ties go to it
            if free:
                dist = np.hypot(known[free, 0] - x[k], known[free, 1] - y[k])
                nearest = int(np.argmin(dist))
                if dist[nearest] <= radius[k]:
                    groups[free[nearest]].append(s)
                    claimed.add(free[nearest])
                    continue
            groups.append([s])
            anchors.append((float(x[k]), float(y[k])))
    return groups


def _covered_elsewhere(
    sources: list[PassSource], frames: list[Frame], passes: list[list[int]]
) -> np.ndarray:
    """Whether a pass other than its own observed each source's position."""
    if not sources:
        return np.zeros(0, bool)
    ra, dec = np.array([s.ra for s in sources]), np.array([s.dec for s in sources])
    covered = np.array(
        [np.any([_covers(frames[fi], ra, dec) for fi in ids], axis=0) for ids in passes]
    )  # [pass, source]
    covered[[s.pass_index for s in sources], np.arange(len(sources))] = False
    return covered.any(axis=0)


def _link_movers(
    pool: list[PassSource],
    n_passes: int,
    plane: SkyPlane,
    max_pm_arcsec_yr: float,
    max_flux_ratio: float,
    chain_pvalue: float,
) -> tuple[list[list[PassSource]], list[PassSource]]:
    """Chain single-pass sources that lie on one straight track.

    Returns (chains, the sources left over).
    """
    if not pool:
        return [], []
    px, py = _source_offsets(plane, pool)
    pt = np.array([s.mjd for s in pool]) / DAYS_PER_YEAR
    psig = np.array([s.sigma_arcsec for s in pool])
    ppass = np.array([s.pass_index for s in pool])
    in_pass = [np.nonzero(ppass == pk)[0] for pk in range(n_passes)]
    # A pair is never linked across more than the fastest motion over the
    # whole time span plus both position errors: look no farther than that.
    reach = max_pm_arcsec_yr * (pt.max() - pt.min()) + 3 * np.hypot(psig, psig.max())
    xy = np.column_stack([px, py])
    nearby = cKDTree(xy).query_ball_point(xy, reach * _TREE_MARGIN)
    candidates: list[tuple[int, float, list[int]]] = []
    for i, a in enumerate(pool):
        for j in sorted(nearby[i]):
            b = pool[j]
            if b.pass_index <= a.pass_index:
                continue
            dt_yr = (b.mjd - a.mjd) / DAYS_PER_YEAR
            sep = float(np.hypot(px[j] - px[i], py[j] - py[i]))
            if sep > max_pm_arcsec_yr * dt_yr + 3 * np.hypot(a.sigma_arcsec, b.sigma_arcsec):
                continue
            if not _flux_compatible(a, b, max_flux_ratio):
                continue
            chain = [i, j]
            # Extend pass by pass with the best-fitting source in each later
            # pass (not the first acceptable one, which could be a decoy).
            for pk in range(b.pass_index + 1, n_passes):
                cand = in_pass[pk]
                if not len(cand):
                    continue
                # One row per candidate: the chain so far, then the candidate.
                rows = np.column_stack([np.tile(chain, (len(cand), 1)), cand])
                pvalues = _line_pvalues(px[rows], py[rows], pt[rows], psig[rows])
                best_k, best_p = None, chain_pvalue
                last = pool[chain[-1]]
                for n in np.nonzero(pvalues >= chain_pvalue)[0]:
                    k = int(cand[n])
                    if pvalues[n] >= best_p and _flux_compatible(last, pool[k], max_flux_ratio):
                        best_k, best_p = k, pvalues[n]
                if best_k is not None:
                    chain.append(best_k)
            candidates.append((len(chain), sep, chain))
    # Prefer longer chains, then shorter jumps (less chance of a chance pairing).
    used: set[int] = set()
    chains: list[list[PassSource]] = []
    for _, _, chain in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if any(k in used for k in chain):
            continue
        used.update(chain)
        chains.append([pool[k] for k in chain])
    return chains, [s for k, s in enumerate(pool) if k not in used]


def _frame_sort_key(f: Frame) -> tuple:
    """A total order on frames: time, detector, id, then content.

    The content digest only separates frames that agree on everything else
    (e.g. no obs_id); identical frames may come in any order.
    """
    digest = hashlib.blake2b(np.ascontiguousarray(f.data).tobytes(), digest_size=16)
    digest.update(f.wcs.to_header_string(relax=True).encode())
    return (f.mjd, f.detector, f.obs_id, digest.digest())


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
    # The tangent-plane centre, the alignment reference and every tie-break
    # below follow frame order, so fix one that doesn't depend on the caller.
    frames = sorted(frames, key=_frame_sort_key)
    notes: list[str] = []
    ny, nx = frames[0].data.shape
    c_ra, c_dec = frames[0].wcs.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)
    plane = SkyPlane(float(c_ra), float(c_dec))
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
    offsets, align_notes = align_frames(detections, plane)
    notes += align_notes
    input_frames, input_passes = frames, passes
    # Brightness is compared between survey visits (runs split at gaps), not
    # between the time windows a long visit is cut into: halving a visit
    # halves the same-wavelength pairs each comparison gets (R Dra's ~85-day
    # visits fell below `min_pairs`). Where there are no gaps (ecliptic
    # poles), the windows are the only epochs there are.
    visits = group_into_passes([f.mjd for f in frames], pass_gap_days, math.inf)
    if len(visits) >= 2:
        visit_of_frame = {i: v for v, ids in enumerate(visits) for i in ids}
        epoch_of_pass = [visit_of_frame[ids[0]] for ids in input_passes]
    else:
        epoch_of_pass = list(range(len(input_passes)))
    if stack:
        n = int(round(max(frames[0].data.shape)))
        grid = stack_grid(SkyCoord(float(c_ra) * u.deg, float(c_dec) * u.deg), n, pix)
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
        _merge_pass(pi, ids, frames, detections, plane, link, min_detect_frac)
        for pi, ids in enumerate(passes)
    ]

    groups = _link_static(per_pass, plane, static_arcsec)
    multi = [g for g in groups if len(g) >= 2]
    singles = [g[0] for g in groups if len(g) == 1]

    # --- mover linking among sources seen in only one pass ---------------------
    # A single-pass source only counts as "missing" elsewhere if another pass
    # actually covered that spot; otherwise it's just off the edge.
    in_pool = _covered_elsewhere(singles, frames, passes)
    pool = [s for s, keep in zip(singles, in_pool) if keep]
    mover_groups, leftovers = _link_movers(
        pool, len(passes), plane, max_pm_arcsec_yr, max_flux_ratio, chain_pvalue
    )
    unlinked = _grow(multi + mover_groups, leftovers, plane, max_pm_arcsec_yr, max_flux_ratio, chain_pvalue)
    unlinked += [s for s, keep in zip(singles, in_pool) if not keep]
    # A slow mover's early passes can link to each other as "static" and its
    # late passes likewise; join such pieces when one line fits them all.
    multi, merged = _merge_tracks(multi, mover_groups, plane, max_pm_arcsec_yr, max_flux_ratio, chain_pvalue)
    mover_groups = merged

    # --- systematic floor from static objects --------------------------------
    raw = []
    for g in multi:
        try:
            raw.append(fit_motion(g, plane))
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
            fit = fit_astrometry(g, plane, parallax=False)
        except ValueError:
            continue
        # Nearby objects wobble with the Earth's orbit (WISE 0855: 0.44"), which
        # a straight line can't follow. Add parallax when it explains a poor
        # fit: one extra parameter must cut chi2 by > 4 (~2 sigma).
        if fit["dof"] >= 2 and fit["chi2"] > fit["dof"]:
            try:
                plx_fit = fit_astrometry(g, plane, parallax=True)
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
        obj.missing_passes = _missing_passes(obj, frames, passes, plane, nsigma)
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
        var = _variability(_brightness_epochs(obj.sources, epoch_of_pass), calib_frac)
        obj.variability = var
        obj.is_variable = bool(
            var and var["ratio"] >= variable_ratio and var["significance"] >= variable_sigma
        )
        objects.append(obj)

    _caution_near_bright(objects, plane)
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
