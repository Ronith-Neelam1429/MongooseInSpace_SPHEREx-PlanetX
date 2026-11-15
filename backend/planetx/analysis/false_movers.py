"""Which of the engine's movers are not really moving, and why.

For every object the engine flags as a mover (`TrackedObject.is_mover`) this
decides two things, written to results/false_movers.json (docs/RESULTS.md):

* `truth`, from Gaia DR3 (`gaia_truth`): the Gaia star under every pass
  position, moved to that pass's date. "static" when it barely moves (or when
  each pass sits on a different slow Gaia star: the track hops between
  neighbours), "real_mover" when Gaia's motion is large and well above the
  SPHEREx error, else "ambiguous" (no Gaia counterpart, or a slow one).
* `cause`, from the pixels and the track (`diagnose`): what made a motion that
  isn't there. Checked from the most specific to the least, the first that
  holds wins. A mover Gaia calls static always gets a cause ("other" when no
  check holds); an ambiguous one only when a pixel check shows a problem; a
  real mover never.

Pure functions of an `EngineResult` and a list of Gaia stars: no network.
`tools/false_mover_audit.py` fetches the frames and the stars.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import numpy as np

from planetx.classify.catalogs import CatalogObject
from planetx.classify.rules import VISIBLE_G_MAG, FieldCatalog, pass_radius
from planetx.engine import EngineResult, TrackedObject
from planetx.engine.motion import CALIB_SNR_EDGES, DAYS_PER_YEAR, SkyPlane

CAUSES = (
    "blend", "saturated_halo", "edge", "flagged_pixels", "low_snr",
    "wavelength_centroid", "parallax_misfit", "chance_link", "other",
)
TRUTHS = ("real_mover", "static", "ambiguous")

# Gaia truth (docs/RESULTS.md, as gaia_validation.json): static under 0.05"/yr;
# a real mover above 0.5"/yr and 3x the SPHEREx error.
STATIC_PM_ARCSEC_YR = 0.05
REAL_PM_ARCSEC_YR = 0.5
REAL_PM_FACTOR = 3.0

# Pixel checks.
EDGE_PX = 3.0  # a centroid this close to the frame edge has a truncated PSF wing
NEAR_BAD_PX = 2  # masked or contaminated pixels within this box (half-width) bias a centroid
BAD_SHARE = 0.5  # ... in at least this share of one pass's detections, more than in the others
HALO_MIN_ARCSEC = 60.0  # a saturated star's halo reaches at least this far ...
HALO_SAT_RADII = 6.0  # ... or this many radii of its saturated patch
BLEND_MAX_DMAG = 2.5  # a neighbour within 10x the flux can pull a photocentre by > 0.1 px
LOW_SNR = CALIB_SNR_EDGES[0]  # the engine's faintest calibration band
WAVE_TERM_SIGMA = 3.0  # a centroid-vs-wavelength slope must be this significant ...
MOVER_SIGMA = 5.0  # ... and leave the motion below the engine's mover bar
PARALLAX_MAX_ARCSEC = 0.8  # larger than any known star's (Proxima: 0.77")
MISFIT_CHI2_DOF = 3.0


@dataclass
class MoverAudit:
    """One audited mover; the first ten fields are docs/RESULTS.md's."""

    ra: float
    dec: float
    field_ra: float
    field_dec: float
    pm_total: float
    significance: float
    snr: float
    truth: str
    cause: str | None
    evidence: str
    # Extra fields (the contract allows additions).
    field_name: str = ""
    mode: str = "single"  # "single" | "stack"
    n_passes: int = 0
    pm_err: float = 0.0
    position_angle_deg: float = 0.0
    gaia_name: str | None = None
    gaia_g_mag: float | None = None
    gaia_pm_total: float | None = None
    checks: dict = field(default_factory=dict)  # every check's numbers, for a human to re-judge

    def to_json(self) -> dict:
        return clean_json(asdict(self))


def clean_json(value):
    """`value` with floats rounded to 6 digits, NaN/inf as None and numpy scalars as Python ones."""
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return round(float(value), 6) if math.isfinite(value) else None
    return value


@dataclass(frozen=True)
class GaiaVerdict:
    truth: str
    star: CatalogObject | None  # the Gaia star the track measured, if one star lies under every pass
    hops: bool  # every pass has a slow Gaia star under it, but no one star under all
    evidence: str


GATOR_GAIA_TABLE = "gaia_dr3_source"
GATOR_GAIA_COLUMNS = "source_id,ra,dec,ref_epoch,pmra,pmdec,phot_g_mean_mag"


def parse_gator_gaia(text: str) -> list[CatalogObject]:
    """Gaia DR3 stars from an IRSA Gator answer (IPAC ASCII) for GATOR_GAIA_COLUMNS.

    IRSA mirrors Gaia DR3 (`gaia_dr3_source`); ESA's own TAP service, which
    `planetx.classify.catalogs.fetch_gaia` uses, was unreachable on 2026-11-14.
    Proper motions come in mas/yr and are returned in arcsec/yr, as there.
    Raises ValueError for an error answer or an unreadable table.
    """
    from astropy.io import ascii as astropy_ascii

    if text.lstrip().startswith("[struct"):
        raise ValueError(f"Gator error: {text.strip()[:300]}")
    table = astropy_ascii.read(text, format="ipac")
    def num(row, name: str) -> float | None:
        v = row[name]
        return None if np.ma.is_masked(v) or not np.isfinite(float(v)) else float(v)

    out = []
    for row in table:
        ra, dec = num(row, "ra"), num(row, "dec")
        if ra is None or dec is None:
            continue
        pmra, pmdec = num(row, "pmra"), num(row, "pmdec")
        has_pm = pmra is not None and pmdec is not None
        epoch = num(row, "ref_epoch") or 2016.0
        out.append(CatalogObject(
            catalog="gaia", name=f"Gaia DR3 {int(row['source_id'])}", ra=ra, dec=dec,
            epoch_mjd=51544.5 + (epoch - 2000.0) * DAYS_PER_YEAR,
            pm_ra_cosdec=pmra / 1000.0 if has_pm else None, pm_dec=pmdec / 1000.0 if has_pm else None,
            magnitude=num(row, "phot_g_mean_mag"), kind="Star",
        ))
    return out


def visible_gaia(stars: Sequence[CatalogObject]) -> FieldCatalog:
    """The Gaia stars bright enough for SPHEREx to have detected (rules.VISIBLE_G_MAG)."""
    return FieldCatalog([
        s for s in stars if s.catalog == "gaia" and (s.magnitude is None or s.magnitude <= VISIBLE_G_MAG)
    ])


def _g(star: CatalogObject) -> float:
    return star.magnitude if star.magnitude is not None else math.inf


def gaia_truth(obj: TrackedObject, catalog: FieldCatalog) -> GaiaVerdict:
    """What Gaia DR3 says about a track (see the module docstring)."""
    per_pass = [
        {i for i, _ in catalog.near(s.ra, s.dec, s.mjd, pass_radius(s))} for s in obj.sources
    ]
    common = set.intersection(*per_pass) if per_pass else set()
    if common:
        # Prefer the star whose own motion agrees best with the track, then the brightest.
        def badness(i: int) -> tuple[float, float]:
            st = catalog.objects[i]
            miss = math.hypot((st.pm_ra_cosdec or 0.0) - obj.pm_ra_cosdec, (st.pm_dec or 0.0) - obj.pm_dec)
            return (miss, _g(st))
        star = catalog.objects[min(common, key=badness)]
        name = f"{star.name} (G {_g(star):.1f})"
        if star.pm_ra_cosdec is None or star.pm_dec is None:
            return GaiaVerdict("ambiguous", star, False, f"{name} lies under every pass but has no Gaia proper motion")
        pm = star.pm_total
        if pm < STATIC_PM_ARCSEC_YR:
            return GaiaVerdict("static", star, False, f"{name} lies under every pass and moves only {pm:.3f}\"/yr")
        if pm > REAL_PM_ARCSEC_YR and pm > REAL_PM_FACTOR * obj.pm_err:
            return GaiaVerdict("real_mover", star, False, f"{name} lies under every pass and moves {pm:.2f}\"/yr")
        return GaiaVerdict(
            "ambiguous", star, False,
            f"{name} lies under every pass; its {pm:.2f}\"/yr is too slow to tell against SPHEREx's {obj.pm_err:.2f}\"/yr error",
        )
    slow = [
        {i for i in found if catalog.objects[i].pm_total < STATIC_PM_ARCSEC_YR * 10} for found in per_pass
    ]
    if per_pass and all(slow):
        return GaiaVerdict(
            "static", None, True,
            "each pass lies on a slow Gaia star, but no one star lies under all of them",
        )
    n_empty = sum(not found for found in per_pass)
    return GaiaVerdict("ambiguous", None, False, f"no Gaia star (G <= {VISIBLE_G_MAG:g}) under {n_empty} of {len(per_pass)} passes")


# --- pixel and track checks ------------------------------------------------------

def _pass_frames(result: EngineResult) -> dict[int, list[int]]:
    return dict(enumerate(result.passes))


def check_edge(obj: TrackedObject, result: EngineResult) -> tuple[bool, str, dict]:
    """A pass whose detections sit at the frame edge (truncated PSF), or whose frames mostly miss the spot."""
    worst = (0.0, None)
    thin = None
    for s in obj.sources:
        near = 0
        for d in s.detections:
            ny, nx = result.frames[d.frame_index].data.shape
            if min(d.x, d.y, nx - 1 - d.x, ny - 1 - d.y) < EDGE_PX:
                near += 1
        share = near / len(s.detections) if s.detections else 0.0
        if share > worst[0]:
            worst = (share, s.pass_index)
        n_frames = len(result.passes[s.pass_index])
        if n_frames >= 4 and s.n_cover < 0.5 * n_frames and thin is None:
            thin = (s.pass_index, s.n_cover, n_frames)
    numbers = {"edge_share": worst[0], "edge_pass": worst[1], "thin_cover": thin}
    if worst[0] >= BAD_SHARE:
        return True, (
            f"in pass {worst[1] + 1}, {worst[0]:.0%} of its detections lie within {EDGE_PX:g} px of the frame edge, "
            "where the PSF is cut off and the centroid pulled inwards"
        ), numbers
    if thin is not None:
        p, n, total = thin
        return True, f"pass {p + 1} covered the spot in only {n} of its {total} frames: it sits at the edge of the field", numbers
    return False, "", numbers


def _bad_near(frame, d) -> bool:
    ny, nx = frame.data.shape
    x, y = round(d.x), round(d.y)
    y0, y1 = max(y - NEAR_BAD_PX, 0), min(y + NEAR_BAD_PX + 1, ny)
    x0, x1 = max(x - NEAR_BAD_PX, 0), min(x + NEAR_BAD_PX + 1, nx)
    box = frame.data[y0:y1, x0:x1]
    if not np.all(np.isfinite(box)):
        return True
    return bool(frame.contaminated is not None and frame.contaminated[y0:y1, x0:x1].any())


def check_flagged(obj: TrackedObject, result: EngineResult) -> tuple[bool, str, dict]:
    """A pass whose detections have masked (NaN) or contaminated pixels next to the core.

    The engine skips sources with a bad pixel in the 3x3 core; one just outside
    still clips a PSF wing in the 3x3 centroid's local background and in the
    neighbouring peak search, and contamination flags (ghosts, halos, persistence)
    add light on one side.
    """
    if any(d.saturated for s in obj.sources for d in s.detections):
        return False, "", {}
    shares = []
    for s in obj.sources:
        bad = sum(_bad_near(result.frames[d.frame_index], d) for d in s.detections)
        shares.append(bad / len(s.detections) if s.detections else 0.0)
    numbers = {"flagged_share_by_pass": shares}
    if not shares:
        return False, "", numbers
    k = int(np.argmax(shares))
    others = [v for i, v in enumerate(shares) if i != k]
    if shares[k] >= BAD_SHARE and (not others or shares[k] > max(others) + 0.25):
        return True, (
            f"in pass {obj.sources[k].pass_index + 1}, {shares[k]:.0%} of its detections have masked or "
            f"contaminated pixels within {NEAR_BAD_PX} px of the centroid (other passes at most {max(others, default=0):.0%})"
        ), numbers
    return False, "", numbers


def check_saturated_halo(obj: TrackedObject, result: EngineResult) -> tuple[bool, str, dict]:
    """Inside a saturated star's halo (or a saturated star itself, whose patch centre is crude)."""
    own = [d for s in obj.sources for d in s.detections if d.saturated]
    if own:
        return True, (
            f"the object is itself saturated in {len(own)} detections: its position comes from the shape of the "
            "saturated patch, which bleeding changes between passes"
        ), {"self_saturated": len(own)}
    plane = SkyPlane(obj.ra, obj.dec)
    best = None
    for s in obj.sources:
        for d in s.detections:
            sats = [x for x in result.detections[d.frame_index] if x.saturated]
            if not sats:
                continue
            e, n = plane.offsets(np.array([x.ra for x in sats]), np.array([x.dec for x in sats]))
            pix = result.frames[d.frame_index].pixel_scale_arcsec
            for x, sep in zip(sats, np.hypot(e, n)):
                reach = max(HALO_MIN_ARCSEC, HALO_SAT_RADII * math.sqrt(x.sat_area / math.pi) * pix)
                if sep <= reach and (best is None or sep < best[0]):
                    best = (float(sep), x.sat_area)
    if best is None:
        return False, "", {}
    return True, (
        f"it lies {best[0]:.0f}\" from a saturated star ({best[1]} saturated px), inside its halo of scattered light"
    ), {"halo_sep_arcsec": best[0], "halo_sat_area": best[1]}


def check_blend(obj: TrackedObject, catalog: FieldCatalog, star: CatalogObject | None, blend_arcsec: float
                ) -> tuple[bool, str, dict]:
    """A Gaia neighbour inside one PSF width, bright enough to pull the photocentre.

    The pair's flux ratio changes with wavelength (different colours) and each
    pass samples different wavelengths, so the blended centroid moves between
    passes even though neither star does.
    """
    mjd = float(np.mean([s.mjd for s in obj.sources]))
    near = catalog.near(obj.ra, obj.dec, mjd, blend_arcsec + (star is None) * 0.0)
    ref_mag = _g(star) if star is not None else min((_g(catalog.objects[i]) for i, _ in near), default=math.inf)
    pals = [
        (catalog.objects[i], sep) for i, sep in near
        if (star is None or catalog.objects[i] is not star) and _g(catalog.objects[i]) <= ref_mag + BLEND_MAX_DMAG
    ]
    if star is None and len(pals) < 2:
        return False, "", {"neighbours": len(pals)}
    if not pals:
        return False, "", {"neighbours": 0}
    if star is None:
        # No one star under every pass: two or more Gaia stars inside a PSF width make the source.
        desc = ", ".join(f"G {_g(p):.1f} at {sep:.1f}\"" for p, sep in pals[:3])
        return True, f"{len(pals)} Gaia stars lie within one PSF width ({blend_arcsec:.0f}\") of it ({desc})", {
            "neighbours": len(pals)}
    p, sep = min(pals, key=lambda x: x[1])
    # Gaia stars (G, at Gaia's own epoch) seen through the SPHEREx PSF: pair separation in pixels is what matters.
    return True, (
        f"a Gaia neighbour (G {_g(p):.1f}, {_g(p) - _g(star):+.1f} mag) lies {sep:.1f}\" away, inside one PSF "
        f"width ({blend_arcsec:.0f}\"): the pair's photocentre shifts as each pass sees it at other wavelengths"
    ), {"neighbour_sep_arcsec": sep, "neighbour_dmag": _g(p) - _g(star)}


def wavelength_fit(obj: TrackedObject, plane: SkyPlane | None = None) -> dict | None:
    """Motion from the track's single detections, with and without a centroid-vs-wavelength term.

    Each detector gets its own linear term in wavelength (a blend's or a red
    star's photocentre drifts across a detector's band). Returns None when the
    detections can't separate wavelength from time (stacks, too few detections).
    Errors are scaled by the fit's own reduced chi2 when it exceeds 1.
    """
    dets = [d for s in obj.sources for d in s.detections]
    if len(dets) < 8 or any(d.saturated for d in dets) or not all(np.isfinite(d.wavelength_um) for d in dets):
        return None
    plane = plane or SkyPlane(obj.ra, obj.dec)
    x, y = plane.offsets(np.array([d.ra for d in dets]), np.array([d.dec for d in dets]))
    t = np.array([d.mjd for d in dets]) / DAYS_PER_YEAR
    lam = np.array([d.wavelength_um for d in dets])
    w = 1 / np.array([d.sigma_arcsec for d in dets]) ** 2
    detector = np.array([d.detector for d in dets])
    t = t - np.average(t, weights=w)
    columns = []
    for det in np.unique(detector):
        m = detector == det
        if m.sum() >= 3 and np.ptp(lam[m]) > 0.02 * lam[m].mean():
            col = np.where(m, lam - np.average(lam[m], weights=w[m]), 0.0)
            columns.append(col)
    if not columns:
        return None
    out = {}
    for label, extra in (("plain", []), ("wave", columns)):
        A = np.column_stack([np.ones_like(t), t, *extra])
        k = A.shape[1]
        if len(dets) <= k:
            return None
        fits = []
        chi2 = 0.0
        for v in (x, y):
            Aw = A * np.sqrt(w)[:, None]
            normal = Aw.T @ Aw
            if np.linalg.matrix_rank(normal) < k:
                return None
            cov = np.linalg.inv(normal)
            p = cov @ (Aw.T @ (v * np.sqrt(w)))
            chi2 += float((((v - A @ p) ** 2) * w).sum())
            fits.append((p, cov))
        dof = 2 * (len(dets) - k)
        scale = math.sqrt(max(1.0, chi2 / dof))
        pm = (fits[0][0][1], fits[1][0][1])
        pm_err = scale * math.sqrt((fits[0][1][1, 1] + fits[1][1][1, 1]) / 2)
        out[label] = {"pm": float(math.hypot(*pm)), "pm_err": float(pm_err), "chi2_dof": chi2 / dof}
        if label == "wave":
            # Largest wavelength slope (arcsec per um) and its significance, over detectors and axes.
            best = max(
                ((abs(p[2 + j]) / (scale * math.sqrt(cov[2 + j, 2 + j])), abs(p[2 + j]))
                 for p, cov in fits for j in range(len(columns))),
            )
            out[label]["slope_sigma"], out[label]["slope_arcsec_um"] = float(best[0]), float(best[1])
    return out


def check_wavelength(obj: TrackedObject, pix_arcsec: float, span_yr: float) -> tuple[bool, str, dict]:
    fit = wavelength_fit(obj)
    if fit is None:
        return False, "", {}
    wave, plain = fit["wave"], fit["plain"]
    sig = wave["pm"] / wave["pm_err"] if wave["pm_err"] > 0 else 0.0
    numbers = {"wavelength_fit": fit}
    if wave["slope_sigma"] >= WAVE_TERM_SIGMA and (sig < MOVER_SIGMA or wave["pm"] * span_yr < pix_arcsec):
        return True, (
            f"its centroid shifts {wave['slope_arcsec_um']:.1f}\"/um with wavelength ({wave['slope_sigma']:.0f} sigma); "
            f"with that term the motion drops from {plain['pm']:.1f} to {wave['pm']:.1f} +- {wave['pm_err']:.1f}\"/yr"
        ), numbers
    return False, "", numbers


def check_parallax(obj: TrackedObject) -> tuple[bool, str, dict]:
    numbers = {"parallax": obj.parallax, "chi2_dof": obj.chi2_dof}
    if obj.parallax is not None and obj.parallax > PARALLAX_MAX_ARCSEC:
        return True, (
            f"the fit took a parallax of {obj.parallax:.2f}\", larger than any star's: it absorbed a misfit, "
            "not a distance"
        ), numbers
    if np.isfinite(obj.chi2_dof) and obj.chi2_dof > MISFIT_CHI2_DOF:
        return True, f"the positions fit a straight line badly (chi2/dof {obj.chi2_dof:.1f})", numbers
    return False, "", numbers


def check_low_snr(obj: TrackedObject) -> tuple[bool, str, dict]:
    snr = float(np.median([s.snr for s in obj.sources]))
    seen = [s.n_det / s.n_cover for s in obj.sources if s.n_cover]
    numbers = {"median_snr": snr, "min_detect_share": min(seen, default=1.0)}
    if snr < LOW_SNR:
        share = min(seen, default=1.0)
        caught = f", caught in as few as {share:.0%} of the frames covering it" if share < 1 else ""
        return True, (
            f"it is faint (median pass SNR {snr:.0f}{caught}): noise moves each pass's centroid "
            "by more than its stated error"
        ), numbers
    return False, "", numbers


def diagnose(
    obj: TrackedObject, result: EngineResult, catalog: FieldCatalog, verdict: GaiaVerdict
) -> tuple[str | None, str, dict]:
    """(cause, evidence, every check's numbers) for one mover (see the module docstring)."""
    pix = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    fwhms = [f.psf_fwhm_arcsec for f in result.frames if np.isfinite(f.psf_fwhm_arcsec)]
    blend_arcsec = float(np.median(fwhms)) if fwhms else 1.5 * pix
    span = (obj.sources[-1].mjd - obj.sources[0].mjd) / DAYS_PER_YEAR
    checks: list[tuple[str, tuple[bool, str, dict]]] = []
    if verdict.hops:
        checks.append(("chance_link", (True, (
            f"{verdict.evidence}: the track links different stars"), {})))
    checks += [
        ("saturated_halo", check_saturated_halo(obj, result)),
        ("edge", check_edge(obj, result)),
        ("flagged_pixels", check_flagged(obj, result)),
        ("blend", check_blend(obj, catalog, verdict.star, blend_arcsec)),
        ("wavelength_centroid", check_wavelength(obj, pix, span)),
        ("parallax_misfit", check_parallax(obj)),
        ("low_snr", check_low_snr(obj)),
    ]
    numbers = {name: nums for name, (_, _, nums) in checks}
    if verdict.truth == "real_mover":
        return None, f"Gaia confirms the motion: {verdict.evidence}.", numbers
    # A slow catalogued star is weak evidence of a blend on its own; for an
    # ambiguous track only pixel problems count, never a mere neighbour.
    strong = {"chance_link", "saturated_halo", "edge", "flagged_pixels", "wavelength_centroid"}
    for name, (hit, text, _) in checks:
        if hit and (verdict.truth == "static" or name in strong):
            lead = verdict.evidence[0].upper() + verdict.evidence[1:]
            return name, f"{lead}; {text}.", numbers
    if verdict.truth == "static":
        return "other", f"{verdict.evidence[0].upper() + verdict.evidence[1:]}; no pixel check explains the motion.", numbers
    return None, f"Not settled: {verdict.evidence}; no pixel problem found.", numbers


def audit_result(
    result: EngineResult,
    stars: Sequence[CatalogObject],
    *,
    field_ra: float,
    field_dec: float,
    field_name: str = "",
) -> list[MoverAudit]:
    """Audit every mover of one engine run against `stars` (Gaia DR3, any epoch)."""
    return audit_objects(result.movers, result, stars, field_ra=field_ra, field_dec=field_dec, field_name=field_name)


def audit_objects(
    objects: Sequence[TrackedObject],
    result: EngineResult,
    stars: Sequence[CatalogObject],
    *,
    field_ra: float,
    field_dec: float,
    field_name: str = "",
) -> list[MoverAudit]:
    """`audit_result` for any of the run's objects (e.g. near-movers held back by the 1 px bar)."""
    catalog = visible_gaia(stars)
    out = []
    for obj in objects:
        verdict = gaia_truth(obj, catalog)
        cause, evidence, numbers = diagnose(obj, result, catalog, verdict)
        st = verdict.star
        out.append(MoverAudit(
            ra=obj.ra, dec=obj.dec, field_ra=field_ra, field_dec=field_dec,
            pm_total=obj.pm_total, significance=obj.pm_significance,
            snr=float(np.median([s.snr for s in obj.sources])),
            truth=verdict.truth, cause=cause, evidence=evidence,
            field_name=field_name, mode="stack" if result.stacked else "single",
            n_passes=len(obj.sources), pm_err=obj.pm_err, position_angle_deg=obj.position_angle_deg,
            gaia_name=st.name if st else None, gaia_g_mag=st.magnitude if st else None,
            gaia_pm_total=st.pm_total if st and st.pm_ra_cosdec is not None else None,
            checks=numbers,
        ))
    return out


def _inside(frame, ra: float, dec: float, margin_px: float = EDGE_PX) -> bool:
    ny, nx = frame.data.shape
    x, y = frame.wcs.all_world2pix(ra, dec, 0)
    return bool(margin_px <= x <= nx - 1 - margin_px and margin_px <= y <= ny - 1 - margin_px)


def fast_star_recovery(result: EngineResult, stars: Sequence[CatalogObject], *, field_name: str = "") -> list[dict]:
    """How the engine tracked each Gaia star that should visibly move in this field.

    A star counts when Gaia gives it more than REAL_PM_ARCSEC_YR, it is visible
    (G <= 19.5) and its path shifts by at least a pixel over the run's passes.
    Every engine pass source within the match radius of the star's position on
    that pass's date is looked up; `status` is "mover" (one track, flagged),
    "split" (its passes ended up in two or more tracks), "not_flagged" (one
    track, not a mover), "unlinked" (only single-pass sources) or "missed".
    """
    catalog = visible_gaia(stars)
    if not len(catalog) or not result.passes:
        return []
    pix = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    mjds = [float(np.mean([result.frames[i].mjd for i in ids])) for ids in result.passes]
    span = (max(mjds) - min(mjds)) / DAYS_PER_YEAR
    sources = [(s, o) for o in result.objects for s in o.sources] + [(s, None) for s in result.unlinked]
    by_pass: dict[int, list] = {}
    for s, o in sources:
        by_pass.setdefault(s.pass_index, []).append((s, o))
    out = []
    for star in catalog.objects:
        if star.pm_total <= REAL_PM_ARCSEC_YR or star.pm_total * span < pix:
            continue
        mid_ra, mid_dec = (float(v[0]) for v in FieldCatalog([star]).positions(float(np.mean(mjds))))
        if not any(_inside(f, mid_ra, mid_dec) for f in result.frames):
            continue  # outside the field
        one = FieldCatalog([star])
        tracks: list[TrackedObject] = []
        n_single = 0
        for p, mjd in enumerate(mjds):
            for s, o in by_pass.get(p, []):
                if one.near(s.ra, s.dec, s.mjd, pass_radius(s)):
                    if o is None:
                        n_single += 1
                    elif not any(o is t for t in tracks):
                        tracks.append(o)
        if len(tracks) >= 2:
            status = "split"
        elif tracks:
            status = "mover" if tracks[0].is_mover else "not_flagged"
        else:
            status = "unlinked" if n_single else "missed"
        out.append({
            "field_name": field_name, "mode": "stack" if result.stacked else "single",
            "gaia_name": star.name, "g_mag": star.magnitude, "ra": mid_ra, "dec": mid_dec,
            "gaia_pm_total": star.pm_total, "status": status, "n_tracks": len(tracks),
            "track_passes": [[s.pass_index for s in t.sources] for t in tracks],
            "track_pm": [t.pm_total for t in tracks], "track_significance": [t.pm_significance for t in tracks],
        })
    return out


def summarize(audits: Sequence[MoverAudit]) -> dict:
    by_cause: dict[str, int] = {}
    for a in audits:
        if a.cause is not None:
            by_cause[a.cause] = by_cause.get(a.cause, 0) + 1
    return {
        "n_movers": len(audits),
        "n_fake": sum(a.cause is not None for a in audits),
        "by_cause": dict(sorted(by_cause.items())),
        "by_truth": {t: sum(a.truth == t for a in audits) for t in TRUTHS},
    }
