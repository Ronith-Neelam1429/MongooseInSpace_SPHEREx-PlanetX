"""Check the tracked objects of one field against Gaia, SIMBAD and SkyBoT.

`check_objects` is the only function here that reaches the network, through
the fetchers it is given. Gaia and SIMBAD are fetched once per field and give
every object a name; SkyBoT (slow) is asked only about movers and candidates
that no star explains, and its geocentric positions are moved to SPHEREx's
orbit position from JPL Horizons. A catalogue that cannot answer never fails
the run: the objects it was needed for come back with `classification=None` and
a note. Without Horizons the asteroid check still runs, on geocentric positions
with a wider match radius, and says so in a note.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from planetx.engine import TrackedObject
from planetx.observatories import FusedMotion, Verdict

from . import catalogs, observer
from .catalogs import DAYS_PER_YEAR, CatalogError, CatalogObject, SolarSystemBody
from .observer import SPHEREX_MAX_GEOCENTRIC_KM, mean_position, parallax_arcsec
from .rules import (
    BODY_MATCH_ARCSEC,
    BODY_MIN_AU,
    MATCH_RADIUS_ARCSEC,
    MAX_MATCHES,
    NEAREST_BODY_AU,
    FieldCatalog,
    Match,
    archive_is_other_stars,
    asteroid_match,
    body_epochs,
    check_track,
    classify,
    identify,
)

log = logging.getLogger(__name__)

# A track short of the engine's mover test but still worth a look: this
# significant, seen in this many passes (as in tools/devview.py).
CANDIDATE_SIGMA = 3.0
CANDIDATE_MIN_PASSES = 3
# The engine links nothing faster (find_motion's max_pm_arcsec_yr), so a
# catalogued star further than this many arcsec per year of catalogue age from
# the field cannot be one of its objects.
MAX_PM_ARCSEC_YR = 15.0
# The SkyBoT cone (geocentric) must hold a body SPHEREx sees at the cone's centre
# down to NEAREST_BODY_AU: 8' of parallax plus the match radius and the 120" of
# position error SkyBoT is told to allow (`-filter`), about 620" in all.
SKYBOT_MAX_POSITION_ERR_ARCSEC = 120.0
SKYBOT_RADIUS_ARCSEC = (
    parallax_arcsec(SPHEREX_MAX_GEOCENTRIC_KM, NEAREST_BODY_AU) + BODY_MATCH_ARCSEC + SKYBOT_MAX_POSITION_ERR_ARCSEC
)
# SkyBoT takes 2-10 s per cone; a run asks at most this many.
MAX_BODY_QUERIES = 8
# After Horizons fails, runs skip it for this long (asteroid checks use the
# geocentric fallback meanwhile), so an outage does not cost every run a timeout.
HORIZONS_BACKOFF_S = 600.0

FetchStars = Callable[[float, float, float], list[CatalogObject]]
FetchBodies = Callable[[float, float, float, float], list[SolarSystemBody]]
# UTC MJDs -> SPHEREx's geocentric ICRF position (km) at each.
FetchObserver = Callable[[Sequence[float]], Sequence[Sequence[float]]]


@dataclass(frozen=True)
class Known:
    """What is known about one tracked object after the catalogue check."""

    kind: str  # mover | candidate | static
    classification: str | None = None  # None: the check could not run for this object
    name: str | None = None
    matches: list[Match] = field(default_factory=list)
    # The archival refit "confirmed" a motion from positions that belong to other
    # Gaia stars: its verdict and motion say nothing about this object.
    archive_void: bool = False


@dataclass
class CheckResult:
    known: list[Known]  # one per object, same order
    notes: list[str] = field(default_factory=list)


def motion_kind(obj: TrackedObject, archive_says: bool | None, pixel_arcsec: float) -> str:
    """mover | candidate | static from SPHEREx and the archival refit.

    `archive_says` is the refit's verdict (`FusedMotion.is_mover`); when it has
    one it overrides SPHEREx alone. A candidate is 3-5 sigma in SPHEREx with a
    shift of at least one pixel, and the archive cannot settle it.
    """
    if archive_says if archive_says is not None else obj.is_mover:
        return "mover"
    span_yr = (obj.sources[-1].mjd - obj.sources[0].mjd) / DAYS_PER_YEAR
    if (
        archive_says is None
        and len(obj.sources) >= CANDIDATE_MIN_PASSES
        and obj.pm_significance >= CANDIDATE_SIGMA
        and obj.pm_total * span_yr >= pixel_arcsec
    ):
        return "candidate"
    return "static"


def unchecked(objects: Sequence[TrackedObject], fused: Sequence[FusedMotion], pixel_arcsec: float) -> list[Known]:
    """The motion class of every object with no catalogue consulted (classification None)."""
    return [Known(kind=motion_kind(o, f.is_mover, pixel_arcsec)) for o, f in zip(objects, fused, strict=True)]


def field_cone(objects: Sequence[TrackedObject]) -> tuple[float, float, float]:
    """(ra, dec, radius in arcsec) of the smallest cone around the mean position that holds every pass position."""
    ra = np.radians([s.ra for o in objects for s in o.sources])
    dec = np.radians([s.dec for o in objects for s in o.sources])
    xyz = np.array([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
    mean = xyz.mean(axis=1)
    mean /= np.linalg.norm(mean)
    radius = float(np.degrees(np.arccos(np.clip(xyz.T @ mean, -1.0, 1.0)).max()) * 3600.0)
    return float(np.degrees(np.arctan2(mean[1], mean[0])) % 360.0), float(np.degrees(np.arcsin(mean[2]))), radius


def _reach(objects: Sequence[TrackedObject], catalogue_epoch_mjd: float) -> float:
    """How far (arcsec) a star the engine can track has moved since the catalogue's date."""
    latest = max(s.mjd for o in objects for s in o.sources)
    return MAX_PM_ARCSEC_YR * abs(latest - catalogue_epoch_mjd) / DAYS_PER_YEAR + MATCH_RADIUS_ARCSEC


def check_objects(
    objects: Sequence[TrackedObject],
    fused: Sequence[FusedMotion],
    pixel_arcsec: float,
    *,
    fetch_gaia: FetchStars = catalogs.fetch_gaia,
    fetch_simbad: FetchStars = catalogs.fetch_simbad,
    fetch_skybot: FetchBodies = catalogs.fetch_skybot,
    fetch_observer: FetchObserver = observer.spherex_geocentric_km,
    stacked: bool = False,
) -> CheckResult:
    """Name and classify the tracked objects of one field, and settle which of them move.

    `fused[i]` is the archival refit of `objects[i]`. Gaia is required: without
    it nothing is classified. SIMBAD only adds names and object types. Horizons
    (`fetch_observer`) only sharpens the known-asteroid check. `stacked`
    (`EngineResult.stacked`): positions come from per-pass co-adds whose time is
    a multi-day mean, which no solar-system body fits, so SkyBoT is not asked.
    """
    if not objects:
        return CheckResult(known=[])
    ra, dec, radius = field_cone(objects)
    gaia_epoch = catalogs.MJD_J2000 + (catalogs.GAIA_DR3_EPOCH_YR - 2000.0) * DAYS_PER_YEAR
    try:
        stars = fetch_gaia(ra, dec, min(radius + _reach(objects, gaia_epoch), catalogs.MAX_RADIUS_ARCSEC))
    except CatalogError as exc:
        log.warning("catalogue check skipped: Gaia unavailable: %s", exc)
        return CheckResult(
            known=unchecked(objects, fused, pixel_arcsec),
            notes=[f"Gaia unavailable ({exc}): objects were not checked against catalogues"],
        )
    notes: list[str] = []
    if len(stars) >= catalogs.GAIA_MAX_ROWS:
        notes.append(f"crowded field: only the {catalogs.GAIA_MAX_ROWS} brightest Gaia stars were used")
    try:
        stars = stars + fetch_simbad(
            ra, dec, min(radius + _reach(objects, catalogs.SIMBAD_EPOCH_MJD), catalogs.MAX_RADIUS_ARCSEC)
        )
    except CatalogError as exc:
        log.warning("SIMBAD unavailable: %s", exc)
        notes.append(f"SIMBAD unavailable ({exc}): names and variable-star types come from Gaia only")
    catalog = FieldCatalog(stars)

    # Per object: motion class, the star that explains it, whether the track is false.
    kinds: list[str] = []
    leads: list[int | None] = []
    false_tracks: list[bool] = []
    void_archive: set[int] = set()
    on_slow_stars = 0
    for i, (obj, fm) in enumerate(zip(objects, fused, strict=True)):
        archive_says = fm.is_mover
        kind = motion_kind(obj, archive_says, pixel_arcsec)
        lead = None
        false_track = fm.verdict is Verdict.REFUTED
        if archive_says and archive_is_other_stars(catalog, obj.ra, obj.dec, obj.ref_mjd, fm.matched):
            archive_says = None
            kind = motion_kind(obj, None, pixel_arcsec)
            void_archive.add(i)
        if kind != "static":
            # The motion the class rests on: the archival refit's when it confirmed, else SPHEREx's own.
            claim, claim_err = (
                ((fm.pm_ra_cosdec, fm.pm_dec), fm.pm_err) if archive_says
                else ((obj.pm_ra_cosdec, obj.pm_dec), obj.pm_err)
            )
            track = check_track(catalog, obj.sources, claim, claim_err)
            if track.explained_by is not None:
                kind, lead = "mover", track.explained_by
            elif track.slow_stars is not None:
                kind = "static"
                false_track = false_track or track.slow_stars == "different"
                on_slow_stars += 1
        kinds.append(kind)
        leads.append(lead)
        false_tracks.append(false_track)
    if void_archive:
        notes.append(
            f"{len(void_archive)} archival confirmation(s) dropped: the archival positions belong to other Gaia stars"
        )
    if on_slow_stars:
        notes.append(f"{on_slow_stars} apparent mover(s) set to static: slow Gaia stars lie at every position")

    if stacked:
        asteroids, unchecked_bodies = {}, set()
        if any(kind != "static" and lead is None for kind, lead in zip(kinds, leads, strict=True)):
            notes.append("stacked passes: movers were not checked for known asteroids "
                         "(a co-add's time spans days, no solar-system body fits it)")
    else:
        asteroids, unchecked_bodies, body_notes = _check_bodies(objects, kinds, leads, fetch_skybot, fetch_observer)
        notes += body_notes

    known = []
    for i, (obj, kind) in enumerate(zip(objects, kinds, strict=True)):
        matches = identify(catalog, obj.ra, obj.dec, obj.ref_mjd, lead=leads[i])
        if i in asteroids:
            matches = [asteroids[i], *matches][:MAX_MATCHES]
        classification = None if i in unchecked_bodies else classify(
            kind,
            is_variable=obj.is_variable,
            variability_caution=bool(obj.variability and obj.variability.get("caution")),
            explained=leads[i] is not None,
            asteroid=i in asteroids,
            catalogued_variable=any(m.variable for m in matches),
            false_track=false_tracks[i],
        )
        # A false track joins different objects, so it has no name of its own; the
        # matches still say what lies at its mean position.
        named = bool(matches) and not false_tracks[i]
        known.append(Known(
            kind=kind, classification=classification, name=matches[0].name if named else None, matches=matches,
            archive_void=i in void_archive,
        ))
    return CheckResult(known=known, notes=notes)


class _HorizonsBackoff:
    """Process-wide memory of a Horizons failure, shared by all runs (and threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0
        self._reason = ""

    def reason(self) -> str | None:
        """Why Horizons is being skipped right now, or None to try it."""
        with self._lock:
            return self._reason if time.monotonic() < self._until else None

    def failed(self, reason: str) -> None:
        with self._lock:
            self._until = time.monotonic() + HORIZONS_BACKOFF_S
            self._reason = reason

    def reset(self) -> None:
        with self._lock:
            self._until, self._reason = 0.0, ""


horizons_backoff = _HorizonsBackoff()


def _spherex_at(source, fetch_observer: FetchObserver) -> tuple[object, float, str | None]:
    """(SPHEREx's mean position over the pass's frames or None, its spread km, failure reason).

    Weighted like the pass position (1/sigma^2 per detection), so the shift
    applies to the averaged position the engine reports.
    """
    reason = horizons_backoff.reason()
    if reason is not None:
        return None, 0.0, f"{reason}; not retried for {HORIZONS_BACKOFF_S / 60:.0f} min"
    dets = source.detections
    try:
        positions = fetch_observer([d.mjd for d in dets])
    except CatalogError as exc:
        log.warning("Horizons unavailable, SkyBoT positions stay geocentric: %s", exc)
        horizons_backoff.failed(str(exc))
        return None, 0.0, str(exc)
    weights = [1.0 / d.sigma_arcsec ** 2 if d.sigma_arcsec > 0 else 0.0 for d in dets]
    if not sum(weights) > 0:
        weights = None
    mean, spread = mean_position(positions, weights)
    return mean, spread, None


def _check_bodies(
    objects: Sequence[TrackedObject],
    kinds: Sequence[str],
    leads: Sequence[int | None],
    fetch_skybot: FetchBodies,
    fetch_observer: FetchObserver,
) -> tuple[dict[int, Match], set[int], list[str]]:
    """Ask SkyBoT about the movers and candidates no star explains.

    Returns (object index -> matched body, indices whose check could not run, notes).
    Most significant objects first, so a query limit or an outage costs the least.
    SPHEREx's position is looked up (one Horizons request per pass, for the times
    of its frames) only when SkyBoT lists a body to compare with.
    """
    wanted = sorted(
        (i for i, kind in enumerate(kinds) if kind != "static" and leads[i] is None),
        key=lambda i: -objects[i].pm_significance,
    )
    found: dict[int, Match] = {}
    unchecked_bodies: set[int] = set()
    notes: list[str] = []
    queries = 0
    failure: str | None = None
    horizons_failure: str | None = None
    geocentric_passes = 0
    geocentric_matches = 0
    assumed_distance_matches = 0
    for i in wanted:
        for source in body_epochs(objects[i].sources):
            if failure is not None or queries >= MAX_BODY_QUERIES:
                unchecked_bodies.add(i)
                break
            queries += 1
            try:
                bodies = fetch_skybot(source.ra, source.dec, SKYBOT_RADIUS_ARCSEC, source.mjd)
            except CatalogError as exc:
                log.warning("SkyBoT unavailable: %s", exc)
                failure = str(exc)
                unchecked_bodies.add(i)
                break
            if not bodies:
                continue
            spherex_km, spread_km = None, 0.0
            if horizons_failure is None:
                spherex_km, spread_km, horizons_failure = _spherex_at(source, fetch_observer)
            if spherex_km is None:
                geocentric_passes += 1
            hit = asteroid_match(source.ra, source.dec, bodies, spherex_km, spread_km)
            if hit is not None:
                found[i] = hit.match
                geocentric_matches += spherex_km is None
                assumed_distance_matches += hit.assumed_distance
                break
    if failure is not None:
        notes.append(
            f"SkyBoT unavailable ({failure}): {len(unchecked_bodies)} object(s) not checked for known asteroids"
        )
    elif unchecked_bodies:
        notes.append(
            f"{len(unchecked_bodies)} object(s) not checked for known asteroids "
            f"(limit of {MAX_BODY_QUERIES} SkyBoT queries per run)"
        )
    if horizons_failure is not None:
        notes.append(
            f"JPL Horizons unavailable ({horizons_failure}): {geocentric_passes} position(s) compared with "
            "geocentric SkyBoT predictions, the match radius widened by SPHEREx's largest parallax "
            f"({parallax_arcsec(SPHEREX_MAX_GEOCENTRIC_KM, 1.0):.1f}\" divided by the distance in au, "
            f"at most {parallax_arcsec(SPHEREX_MAX_GEOCENTRIC_KM, NEAREST_BODY_AU):.0f}\"); "
            f"{geocentric_matches} known-asteroid match(es) rest on that wider radius"
        )
    if assumed_distance_matches:
        notes.append(
            f"{assumed_distance_matches} known-asteroid match(es) on bodies SkyBoT gives no distance for: "
            f"compared at the geocentric position within the parallax of a body {BODY_MIN_AU} au away "
            f"({parallax_arcsec(SPHEREX_MAX_GEOCENTRIC_KM, BODY_MIN_AU):.0f}\")"
        )
    return found, unchecked_bodies, notes
