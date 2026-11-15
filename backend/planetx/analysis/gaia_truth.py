"""Engine motions against Gaia DR3: which tracked objects really move, and how well SPHEREx measures it.

Pure functions, no network: the caller fetches Gaia (tools/gaia_validation.py) and
passes the rows in. The steps:

1. `match_track`: the Gaia star an engine track measured. Gaia positions are at
   2016.0, so they are moved to each pass's date with Gaia's own proper motion
   first (a 1"/yr star is 10" away by 2026). In every pass the visible Gaia stars
   within the match radius are candidates; the star found in the most passes wins,
   then the brightest. The brightest star dominates a 6.2" SPHEREx pixel, and in a
   crowded field a fainter star is often nearer by chance.
2. `assign_truth`: real_mover / static / ambiguous from the Gaia proper motions of
   every star that could have made the track. That is the matched star, the stars
   blended with it in one pixel, and, when the passes land on different stars,
   each pass's star. A brightness-ranked chance probability flags matches that
   random stars would explain just as well.
3. `summarize`: residuals (SPHEREx minus Gaia), pulls (residual over the combined
   error: std ~1 when the errors are honest), the confusion matrix of the engine's
   mover flag against the truth, the false-mover rate and the recovered fraction.
   Each is also split by SNR band and by the number of measured passes.

The engine aligns every frame on the field's stars, so its motions are relative to
the field's mean motion. The mean motion is a few mas/yr, far below SPHEREx's errors.
`summary.frame_pm_offset` measures it from the static stars. The comparison
leaves parallax out: the engine fits parallax only when it explains a bad straight
line, and Gaia's linear motion is what the line fit estimates.
"""
from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from astropy.io import ascii
from scipy.stats import beta

from planetx.classify.rules import MATCH_RADIUS_ARCSEC, VISIBLE_G_MAG
from planetx.engine.motion import CALIB_SNR_EDGES

MJD_J2000 = 51544.5
DAYS_PER_YEAR = 365.25
ARCSEC_PER_RAD = 180.0 / math.pi * 3600.0
PIXEL_ARCSEC = 6.2

# Truth thresholds (docs/RESULTS.md). "Static at > 5 sigma precision": the total
# motion is under 0.05"/yr and each Gaia component is known to 0.01"/yr or better.
STATIC_PM = 0.05
STATIC_PM_ERR = STATIC_PM / 5
MOVER_PM = 0.5
MOVER_SIGMA = 3.0  # Gaia motion over the SPHEREx error

# A pass's match radius: MATCH_RADIUS_ARCSEC (0.7 px, as the catalogue check uses),
# or 3 of the pass's own position errors when that is wider, at most 1.5 px.
PASS_SIGMA = 3.0
MAX_MATCH_RADIUS_ARCSEC = 9.0
# Another visible Gaia star this close to the matched one, and at most this much
# fainter in G, shares its SPHEREx pixel and can shift the measured centroid.
BLEND_RADIUS_ARCSEC = PIXEL_ARCSEC
BLEND_DMAG = 1.5
# A match is "by chance" when a random position would find a visible Gaia star at
# least as bright within the match radius this often.
CHANCE_MAX = 0.05

SNR_EDGES = CALIB_SNR_EDGES  # the engine's error-calibration bands (<15, 15-40, >=40)
# Gaia movers brighter than this that no track matched are listed as missed.
UNTRACKED_MAX_G = 18.0
TRUTHS = ("real_mover", "static", "ambiguous")

GAIA_COLUMNS = (
    "source_id", "ra", "dec", "ref_epoch", "pmra", "pmdec", "pmra_error", "pmdec_error",
    "parallax", "phot_g_mean_mag",
)


class PassLike(Protocol):
    """One pass of a track (the engine's `PassSource` fits)."""

    ra: float
    dec: float
    mjd: float
    sigma_arcsec: float
    snr: float


class TrackLike(Protocol):
    """An engine track (`TrackedObject` fits)."""

    sources: Sequence[PassLike]
    ra: float
    dec: float
    ref_mjd: float
    pm_ra_cosdec: float
    pm_dec: float
    pm_err: float
    chi2_dof: float
    is_mover: bool


@dataclass(frozen=True)
class GaiaStar:
    source_id: str
    ra: float  # degrees at epoch_mjd
    dec: float
    epoch_mjd: float
    g_mag: float | None
    pm_ra: float | None = None  # arcsec/yr, east positive (pmra includes cos dec)
    pm_dec: float | None = None
    pm_ra_err: float | None = None
    pm_dec_err: float | None = None
    parallax: float | None = None  # arcsec

    @property
    def has_pm(self) -> bool:
        return self.pm_ra is not None and self.pm_dec is not None

    @property
    def pm_total(self) -> float | None:
        return math.hypot(self.pm_ra, self.pm_dec) if self.has_pm else None

    @property
    def pm_err(self) -> float | None:
        """Per-component error (RMS of the two), comparable to the engine's `pm_err`."""
        if self.pm_ra_err is None or self.pm_dec_err is None:
            return None
        return math.sqrt((self.pm_ra_err**2 + self.pm_dec_err**2) / 2)


def _float(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_gaia_csv(text: str) -> list[GaiaStar]:
    """Stars from a Gaia TAP CSV answer with GAIA_COLUMNS; mas and mas/yr become arcsec and arcsec/yr."""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in GAIA_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"Gaia answer has no column {missing[0]!r}: {text.strip()[:120]!r}")
    stars = []
    for row in reader:
        ra, dec = _float(row["ra"]), _float(row["dec"])
        if ra is None or dec is None or not row["source_id"].strip():
            continue
        epoch_yr = _float(row["ref_epoch"]) or 2016.0
        pmra, pmdec = _float(row["pmra"]), _float(row["pmdec"])
        has_pm = pmra is not None and pmdec is not None

        def arcsec(v: float | None) -> float | None:
            return v / 1000.0 if v is not None else None

        stars.append(GaiaStar(
            source_id=row["source_id"].strip(), ra=ra, dec=dec,
            epoch_mjd=MJD_J2000 + (epoch_yr - 2000.0) * DAYS_PER_YEAR,
            g_mag=_float(row["phot_g_mean_mag"]),
            pm_ra=arcsec(pmra) if has_pm else None,
            pm_dec=arcsec(pmdec) if has_pm else None,
            pm_ra_err=arcsec(_float(row["pmra_error"])) if has_pm else None,
            pm_dec_err=arcsec(_float(row["pmdec_error"])) if has_pm else None,
            parallax=arcsec(_float(row["parallax"])),
        ))
    return stars


def gaia_ipac_to_csv(text: str) -> str:
    """An IRSA Gator answer for gaia_dr3_source (IPAC table) as the CSV `parse_gaia_csv` reads.

    Gator's copy has the same column names and units as ESA's (mas, mas/yr); nulls become empty fields.
    """
    table = ascii.read(text, format="ipac")
    missing = [c for c in GAIA_COLUMNS if c not in table.colnames]
    if missing:
        raise ValueError(f"Gator answer has no column {missing[0]!r}")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(GAIA_COLUMNS)
    for row in table:
        writer.writerow(["" if np.ma.is_masked(row[c]) else str(row[c]) for c in GAIA_COLUMNS])
    return out.getvalue()


def _separation_arcsec(ra1, dec1, ra2: float, dec2: float) -> np.ndarray:
    """Angular distance (haversine: exact at the poles and across RA 0)."""
    a1, d1 = np.radians(ra1), np.radians(dec1)
    a2, d2 = math.radians(ra2), math.radians(dec2)
    h = np.sin((d1 - d2) / 2) ** 2 + np.cos(d1) * math.cos(d2) * np.sin((a1 - a2) / 2) ** 2
    return 2 * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0))) * ARCSEC_PER_RAD


class GaiaField:
    """The visible Gaia stars of one field, searchable by position at any date.

    `radius_arcsec` is the radius of the cone the stars were fetched from; it sets
    the star density behind the chance-match probability.
    """

    def __init__(self, stars: Iterable[GaiaStar], radius_arcsec: float) -> None:
        if not radius_arcsec > 0:
            raise ValueError(f"radius must be positive, got {radius_arcsec}")
        self.stars = [s for s in stars if s.g_mag is not None and s.g_mag <= VISIBLE_G_MAG]
        self.radius_arcsec = float(radius_arcsec)
        self._ra = np.array([s.ra for s in self.stars], dtype=float)
        self._dec = np.array([s.dec for s in self.stars], dtype=float)
        self._epoch = np.array([s.epoch_mjd for s in self.stars], dtype=float)
        self._pm_ra = np.array([s.pm_ra or 0.0 for s in self.stars], dtype=float)
        self._pm_dec = np.array([s.pm_dec or 0.0 for s in self.stars], dtype=float)
        self._cos_dec = np.maximum(np.cos(np.radians(self._dec)), 1e-9)
        self._mags = np.sort([s.g_mag for s in self.stars])

    def __len__(self) -> int:
        return len(self.stars)

    def positions(self, mjd: float) -> tuple[np.ndarray, np.ndarray]:
        """(ra, dec) of every star at `mjd`, moved linearly with its proper motion."""
        years = (mjd - self._epoch) / DAYS_PER_YEAR
        return (
            self._ra + self._pm_ra * years / 3600.0 / self._cos_dec,
            self._dec + self._pm_dec * years / 3600.0,
        )

    def near(self, ra: float, dec: float, mjd: float, radius_arcsec: float) -> list[tuple[int, float]]:
        """(index, separation) of the stars within the radius on that date, nearest first."""
        if not self.stars:
            return []
        sep = _separation_arcsec(*self.positions(mjd), ra, dec)
        found = np.flatnonzero(sep <= radius_arcsec)
        return [(int(i), float(sep[i])) for i in found[np.argsort(sep[found])]]

    def chance_probability(self, g_mag: float, radius_arcsec: float) -> float:
        """How often a random position has a visible star at least this bright within the radius."""
        n_brighter = int(np.searchsorted(self._mags, g_mag, side="right"))
        density = n_brighter / (math.pi * self.radius_arcsec**2)
        return 1.0 - math.exp(-density * math.pi * radius_arcsec**2)


def pass_radius(p: PassLike) -> float:
    sigma = p.sigma_arcsec
    if sigma is None or not math.isfinite(sigma):
        return MATCH_RADIUS_ARCSEC
    return min(max(MATCH_RADIUS_ARCSEC, PASS_SIGMA * sigma), MAX_MATCH_RADIUS_ARCSEC)


@dataclass(frozen=True)
class TrackMatch:
    primary: int  # index into GaiaField.stars
    kind: str  # "full": the star is in every pass; "hop": every pass is on a star, not
    # always the same one; "partial": some passes have no Gaia star at all
    n_passes_matched: int
    max_sep_arcsec: float  # of the primary, over the passes it matched
    pass_stars: tuple[int | None, ...]  # brightest candidate per pass
    blend: tuple[int, ...]  # other stars sharing the primary's pixel
    chance_p: float


def match_track(track: TrackLike, gaia: GaiaField) -> TrackMatch | None:
    """The Gaia star behind an engine track, or None when no pass has a visible Gaia star nearby."""
    found: dict[int, list[float]] = {}
    pass_stars: list[int | None] = []
    radii = []
    for p in track.sources:
        radius = pass_radius(p)
        radii.append(radius)
        near = gaia.near(p.ra, p.dec, p.mjd, radius)
        for i, sep in near:
            found.setdefault(i, []).append(sep)
        pass_stars.append(min((i for i, _ in near), key=lambda i: gaia.stars[i].g_mag) if near else None)
    if not found:
        return None
    primary = max(found, key=lambda i: (len(found[i]), -gaia.stars[i].g_mag, -float(np.mean(found[i]))))
    n = len(track.sources)
    matched = len(found[primary])
    if matched == n:
        kind = "full"
    elif all(s is not None for s in pass_stars):
        kind = "hop"
    else:
        kind = "partial"

    star = gaia.stars[primary]
    # Blends at every pass's date: a fast neighbour can share the pixel in one pass only.
    blend_set: set[int] = set()
    for p in track.sources:
        ra_then, dec_then = gaia.positions(p.mjd)
        sep = _separation_arcsec(ra_then, dec_then, float(ra_then[primary]), float(dec_then[primary]))
        for i in np.flatnonzero(sep <= BLEND_RADIUS_ARCSEC):
            other = gaia.stars[i]
            # A moving neighbour counts however faint in G: high proper-motion stars are
            # mostly red dwarfs, several magnitudes brighter at SPHEREx's 1-5 um than in G.
            moving = other.has_pm and other.pm_total >= STATIC_PM
            if i != primary and (other.g_mag <= star.g_mag + BLEND_DMAG or moving):
                blend_set.add(int(i))
    blend = tuple(sorted(blend_set))

    if kind == "hop":
        # Each pass is identified with its own star: the weakest identification sets the chance.
        chance = max(
            gaia.chance_probability(gaia.stars[s].g_mag, r) for s, r in zip(pass_stars, radii, strict=True)
        )
    else:
        radius = float(np.median(radii))
        chance = gaia.chance_probability(star.g_mag, radius)
        span_yr = (max(p.mjd for p in track.sources) - min(p.mjd for p in track.sources)) / DAYS_PER_YEAR
        if star.has_pm and star.pm_total * span_yr > radius:
            # A star that moved out of its own match circle and was found in every pass
            # anyway follows the track: a random star does that with probability ~p per pass.
            chance = chance**matched
    return TrackMatch(
        primary=primary, kind=kind, n_passes_matched=matched, max_sep_arcsec=max(found[primary]),
        pass_stars=tuple(pass_stars), blend=blend, chance_p=chance,
    )


def star_status(star: GaiaStar, spherex_pm_err: float) -> str:
    """"static", "mover", "between" (neither) or "unknown" (no Gaia motion)."""
    if not star.has_pm:
        return "unknown"
    pm = star.pm_total
    err = max(star.pm_ra_err or math.inf, star.pm_dec_err or math.inf)
    if pm < STATIC_PM and err <= STATIC_PM_ERR:
        return "static"
    if pm > MOVER_PM and pm > MOVER_SIGMA * spherex_pm_err:
        return "mover"
    return "between"


def assign_truth(match: TrackMatch, gaia: GaiaField, spherex_pm_err: float) -> tuple[str, str]:
    """(truth, reason) for a matched track; truth is one of TRUTHS."""
    primary = star_status(gaia.stars[match.primary], spherex_pm_err)
    blend = [star_status(gaia.stars[i], spherex_pm_err) for i in match.blend]
    if match.kind == "partial":
        return "ambiguous", "some passes have no Gaia star"
    if match.chance_p > CHANCE_MAX:
        return "ambiguous", f"chance match (p={match.chance_p:.2f})"
    if match.kind == "hop":
        others = [star_status(gaia.stars[i], spherex_pm_err) for i in set(match.pass_stars) if i is not None]
        if all(s == "static" for s in [primary, *blend, *others]):
            return "static", "passes fall on different static Gaia stars"
        return "ambiguous", "passes fall on different Gaia stars, not all static"
    if primary == "unknown":
        return "ambiguous", "Gaia has no proper motion for this star"
    if primary == "static":
        if all(s == "static" for s in blend):
            return "static", "blend of static Gaia stars" if blend else "static Gaia star"
        return "ambiguous", "static star blended with a moving or unmeasured one"
    if primary == "mover":
        return "real_mover", "blended moving Gaia star" if blend else "moving Gaia star"
    return "ambiguous", "Gaia motion between the static and mover thresholds"


def snr_of(track: TrackLike) -> float:
    """The track's median pass SNR (what the engine bands its error calibration by)."""
    return float(np.median([p.snr for p in track.sources])) if track.sources else math.nan


def snr_band(snr: float) -> str:
    if not math.isfinite(snr):
        return "unknown"
    e = SNR_EDGES
    labels = [f"<{e[0]:g}"] + [f"{a:g}-{b:g}" for a, b in zip(e[:-1], e[1:])] + [f">={e[-1]:g}"]
    return labels[int(np.searchsorted(e, snr, side="right"))]


def passes_bin(n: int) -> str:
    return str(n) if n < 4 else "4+"


def _r(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _saturated(track: TrackLike) -> bool:
    return any(getattr(d, "saturated", False) for p in track.sources for d in getattr(p, "detections", ()))


def _track_row(track: TrackLike, field_name: str) -> dict:
    return {
        "field": field_name,
        "ra": _r(track.ra, 7),
        "dec": _r(track.dec, 7),
        "ref_mjd": _r(track.ref_mjd, 3),
        "snr": _r(snr_of(track), 2),
        "snr_band": snr_band(snr_of(track)),
        "n_passes": len(track.sources),
        "spherex_pm_ra": _r(track.pm_ra_cosdec, 4),
        "spherex_pm_dec": _r(track.pm_dec, 4),
        "spherex_pm_err": _r(track.pm_err, 4),
        "spherex_pm_total": _r(math.hypot(track.pm_ra_cosdec, track.pm_dec), 4),
        "pm_significance": _r(math.hypot(track.pm_ra_cosdec, track.pm_dec) / track.pm_err
                              if track.pm_err > 0 else 0.0, 2),
        "chi2_dof": _r(track.chi2_dof, 3),
        "saturated": _saturated(track),
        "is_variable": bool(getattr(track, "is_variable", False)),
        "engine_is_mover": bool(track.is_mover),
        "engine_link": getattr(track, "link", None),
        "passes": [
            {"ra": _r(p.ra, 7), "dec": _r(p.dec, 7), "mjd": _r(p.mjd, 3),
             "sigma_arcsec": _r(p.sigma_arcsec, 3), "snr": _r(p.snr, 1)}
            for p in track.sources
        ],
    }


def evaluate_field(tracks: Sequence[TrackLike], gaia: GaiaField, field_name: str) -> tuple[list[dict], list[dict]]:
    """(matched rows, unmatched rows) for one field's engine objects.

    Matched rows carry the docs/RESULTS.md object fields plus the match details;
    `clean` marks the ones fit for residual statistics (Gaia star in every pass,
    no blend, not a chance match, Gaia motion measured).
    """
    matched, unmatched = [], []
    for track in tracks:
        row = _track_row(track, field_name)
        m = match_track(track, gaia)
        if m is None:
            unmatched.append(row | {"truth": None, "truth_reason": "no visible Gaia star within the match radius"})
            continue
        star = gaia.stars[m.primary]
        truth, reason = assign_truth(m, gaia, track.pm_err)
        row |= {
            "g_mag": _r(star.g_mag, 3),
            "gaia_source_id": star.source_id,
            "gaia_pm_ra": _r(star.pm_ra, 5),
            "gaia_pm_dec": _r(star.pm_dec, 5),
            "gaia_pm_err": _r(star.pm_err, 5),
            "gaia_pm_total": _r(star.pm_total, 5),
            "gaia_parallax": _r(star.parallax, 5),
            "match": m.kind,
            "n_passes_matched": m.n_passes_matched,
            "max_sep_arcsec": _r(m.max_sep_arcsec, 2),
            "n_blend": len(m.blend),
            "blend_g_mags": [_r(gaia.stars[i].g_mag, 2) for i in m.blend],
            # Every Gaia star this track saw: the primary, its blends and each pass's star.
            "gaia_related_ids": sorted({gaia.stars[i].source_id for i in (m.primary, *m.blend, *m.pass_stars)
                                        if i is not None}),
            "chance_p": _r(m.chance_p, 4),
            "truth": truth,
            "truth_reason": reason,
        }
        measured = all(row[k] is not None for k in ("spherex_pm_ra", "spherex_pm_dec", "spherex_pm_err")) \
            and row["spherex_pm_err"] > 0
        row["clean"] = bool(
            m.kind == "full" and not m.blend and m.chance_p <= CHANCE_MAX and star.has_pm and measured
        )
        matched.append((row, frozenset(getattr(p, "pass_index", k) for k, p in enumerate(track.sources))))
    # A star split into several tracks, e.g. a fast mover linked as two static pairs of
    # passes. Tracks that share a pass are different sources next to one star, not a split.
    per_star: dict[str, list[frozenset]] = {}
    for row, passes in matched:
        per_star.setdefault(row["gaia_source_id"], []).append(passes)
    for row, passes in matched:
        others = per_star[row["gaia_source_id"]]
        row["n_tracks_same_star"] = len(others)
        row["split_track"] = sum(1 for o in others if not (o & passes)) >= 1
    return [row for row, _ in matched], unmatched


@dataclass
class _Stats:
    rows: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        rows = self.rows
        confusion = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
        for r in rows:
            if r["truth"] == "real_mover":
                confusion["tp" if r["engine_is_mover"] else "fn"] += 1
            elif r["truth"] == "static":
                confusion["fp" if r["engine_is_mover"] else "tn"] += 1
        clean = [r for r in rows if r.get("clean")]
        dx = np.array([r["spherex_pm_ra"] - r["gaia_pm_ra"] for r in clean], dtype=float)
        dy = np.array([r["spherex_pm_dec"] - r["gaia_pm_dec"] for r in clean], dtype=float)
        err = np.array([math.hypot(r["spherex_pm_err"], r["gaia_pm_err"] or 0.0) for r in clean], dtype=float)
        comps = np.concatenate([dx, dy])
        pulls = np.concatenate([dx / err, dy / err]) if len(clean) else np.array([])
        n_static = confusion["fp"] + confusion["tn"]
        n_real = confusion["tp"] + confusion["fn"]
        return {
            "n_matched": len(rows),
            "n_by_truth": {t: sum(r["truth"] == t for r in rows) for t in TRUTHS},
            "n_residual": len(clean),
            "residual_median": _r(float(np.median(np.hypot(dx, dy))), 4) if len(clean) else None,
            "residual_mad": _robust_std(comps),
            "pull_std": _r(float(np.std(pulls)), 3) if len(pulls) > 1 else None,
            "pull_robust_std": _robust_std(pulls),
            "pull_frac_within_3": _r(float(np.mean(np.abs(pulls) <= 3)), 4) if len(pulls) else None,
            "spherex_pm_err_median": _r(float(np.median([r["spherex_pm_err"] for r in clean])), 4)
            if clean else None,
            "confusion": confusion,
            "false_mover_rate": _r(confusion["fp"] / n_static, 5) if n_static else None,
            # Clopper-Pearson: the rate the data rule out at 95% (0 of N gives about 3/N).
            "false_mover_rate_upper95": _upper95(confusion["fp"], n_static),
            "recovered_fraction": _r(confusion["tp"] / n_real, 4) if n_real else None,
        }


def _upper95(k: int, n: int) -> float | None:
    """One-sided 95% Clopper-Pearson upper limit on a rate seen k times in n (0 of n gives about 3/n)."""
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    return _r(float(beta.ppf(0.95, k + 1, n - k)), 6)


def _robust_std(values: np.ndarray) -> float | None:
    """1.4826 x the median absolute deviation: the std of a normal core, deaf to outliers."""
    if len(values) < 2:
        return None
    return _r(1.4826 * float(np.median(np.abs(values - np.median(values)))), 4)


def summarize(matched: Sequence[dict], n_objects: int) -> dict:
    """The docs/RESULTS.md `summary`, plus the same numbers split by SNR band and by passes.

    `residual_median` is the median |SPHEREx - Gaia| proper motion (arcsec/yr) and
    `residual_mad` the robust std (1.4826 x MAD) of one residual component, both over
    the clean rows. `pull_std` is the plain std of residual / combined error over both
    components; `pull_robust_std` is the same with outliers ignored.
    """
    out = {"n_objects": int(n_objects)} | _Stats(list(matched)).summary()
    split = {(r["field"], r["gaia_source_id"], r["truth"]) for r in matched if r.get("split_track")}
    out["n_stars_split_into_tracks"] = len(split)
    out["n_real_movers_split_into_tracks"] = sum(t == "real_mover" for *_, t in split)
    static_clean = [r for r in matched if r.get("clean") and r["truth"] == "static"]
    if static_clean:
        out["frame_pm_offset"] = {
            "pm_ra": _r(float(np.median([r["spherex_pm_ra"] - r["gaia_pm_ra"] for r in static_clean])), 4),
            "pm_dec": _r(float(np.median([r["spherex_pm_dec"] - r["gaia_pm_dec"] for r in static_clean])), 4),
            "n": len(static_clean),
        }
    for key, label in (("snr_band", "by_snr"), ("n_passes", "by_passes")):
        groups: dict[str, list[dict]] = {}
        for r in matched:
            name = r["snr_band"] if key == "snr_band" else passes_bin(r["n_passes"])
            groups.setdefault(name, []).append(r)
        out[label] = {name: _Stats(rows).summary() for name, rows in sorted(groups.items())}
    return out


def untracked_movers(
    gaia: GaiaField, matched: Sequence[dict], mjd: float, inside, max_g_mag: float = UNTRACKED_MAX_G,
) -> list[dict]:
    """Gaia movers (> MOVER_PM) in the field and bright enough for SPHEREx that no engine track matched.

    `inside(ra, dec)` says whether a position on `mjd` is inside the analysed frames.
    The confusion matrix counts only tracked objects; these are the movers it cannot see.
    """
    taken = {sid for r in matched for sid in r.get("gaia_related_ids", [r["gaia_source_id"]])}
    ra_now, dec_now = gaia.positions(mjd)
    ra_now = ra_now % 360.0
    out = []
    for i, star in enumerate(gaia.stars):
        if not star.has_pm or star.pm_total <= MOVER_PM or star.g_mag > max_g_mag or star.source_id in taken:
            continue
        if not inside(float(ra_now[i]), float(dec_now[i])):
            continue
        out.append({
            "gaia_source_id": star.source_id, "ra": _r(float(ra_now[i]), 7), "dec": _r(float(dec_now[i]), 7),
            "mjd": _r(mjd, 3), "g_mag": _r(star.g_mag, 3), "gaia_pm_ra": _r(star.pm_ra, 5),
            "gaia_pm_dec": _r(star.pm_dec, 5), "gaia_pm_total": _r(star.pm_total, 5),
        })
    return out


def false_movers(matched: Sequence[dict]) -> list[dict]:
    """Engine movers that Gaia says are static, most significant first."""
    rows = [r for r in matched if r["engine_is_mover"] and r["truth"] == "static"]
    return sorted(rows, key=lambda r: -(r["pm_significance"] or 0.0))
