"""Check the tracked objects of one field against Gaia, SIMBAD and SkyBoT.

`check_objects` is the only function here that reaches the network, through
the fetchers it is given. Gaia and SIMBAD are fetched once per field and give
every object a name; SkyBoT (slow) is asked only about movers and candidates
that no star explains. A catalogue that cannot answer never fails the run: the
objects it was needed for come back with `classification=None` and a note.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from planetx.engine import TrackedObject
from planetx.observatories import FusedMotion, Verdict

from . import catalogs
from .catalogs import DAYS_PER_YEAR, CatalogError, CatalogObject, SolarSystemBody
from .rules import (
    MATCH_RADIUS_ARCSEC,
    MAX_MATCHES,
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
# Wide enough for the geocentric parallax of a body 0.1 au away.
SKYBOT_RADIUS_ARCSEC = 120.0
# SkyBoT takes 2-10 s per cone; a run asks at most this many.
MAX_BODY_QUERIES = 8

FetchStars = Callable[[float, float, float], list[CatalogObject]]
FetchBodies = Callable[[float, float, float, float], list[SolarSystemBody]]


@dataclass(frozen=True)
class Known:
    """What is known about one tracked object after the catalogue check."""

    kind: str  # mover | candidate | static
    classification: str | None = None  # None: the check could not run for this object
    name: str | None = None
    matches: list[Match] = field(default_factory=list)


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
) -> CheckResult:
    """Name and classify the tracked objects of one field, and settle which of them move.

    `fused[i]` is the archival refit of `objects[i]`. Gaia is required: without
    it nothing is classified. SIMBAD only adds names and object types.
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
    void_archive = on_slow_stars = 0
    for obj, fm in zip(objects, fused, strict=True):
        archive_says = fm.is_mover
        kind = motion_kind(obj, archive_says, pixel_arcsec)
        lead = None
        false_track = fm.verdict is Verdict.REFUTED
        if archive_says and archive_is_other_stars(catalog, obj.ra, obj.dec, obj.ref_mjd, fm.matched):
            archive_says = None
            kind = motion_kind(obj, None, pixel_arcsec)
            void_archive += 1
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
            f"{void_archive} archival confirmation(s) dropped: the archival positions belong to other Gaia stars"
        )
    if on_slow_stars:
        notes.append(f"{on_slow_stars} apparent mover(s) set to static: slow Gaia stars lie at every position")

    asteroids, unchecked_bodies, body_notes = _check_bodies(objects, kinds, leads, fetch_skybot)
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
        ))
    return CheckResult(known=known, notes=notes)


def _check_bodies(
    objects: Sequence[TrackedObject],
    kinds: Sequence[str],
    leads: Sequence[int | None],
    fetch_skybot: FetchBodies,
) -> tuple[dict[int, Match], set[int], list[str]]:
    """Ask SkyBoT about the movers and candidates no star explains.

    Returns (object index -> matched body, indices whose check could not run, notes).
    Most significant objects first, so a query limit or an outage costs the least.
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
            match = asteroid_match(source.ra, source.dec, bodies)
            if match is not None:
                found[i] = match
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
    return found, unchecked_bodies, notes
