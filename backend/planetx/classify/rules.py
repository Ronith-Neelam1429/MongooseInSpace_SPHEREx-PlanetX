"""What an engine object is, decided from catalogue rows. Pure functions: no network.

The catalogues answer three questions about a SPHEREx track:

1. Which catalogued object sits there (`identify`)? Catalogue positions are
   moved to the SPHEREx date with the catalogue's own proper motions first: a
   10"/yr star is 100" from its Gaia (2016.0) position by now.
2. Is its motion real (`check_track`, `archive_is_other_stars`)? A catalogued
   star that follows the track explains it. Slow catalogued stars under every
   position of the track mean nothing moved.
3. What to call it (`classify`).

The numbers below come from the cached Barnard's Star field (281 tracked objects,
6,884 Gaia stars within 10', galactic latitude +14 deg, so a crowded field):
  * The brightest Gaia star within 6" of a tracked object lies within 1.0" for half
    of them and within 3.6" for 95%; in a blend, single-pass positions sit up to
    4.2" from the star. Random positions have a Gaia star brighter than G 18.5
    within 4" 11% of the time.
  * 99% of those stars are brighter than G 19.3: fainter ones are not what
    SPHEREx detected.
  * The archival positions behind the field's one false "confirmed" mover lie
    0.2" and 0.8" from slow Gaia stars.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .catalogs import DAYS_PER_YEAR, CatalogObject, SolarSystemBody
from .observer import SPHEREX_MAX_GEOCENTRIC_KM, from_observer, parallax_arcsec

ARCSEC_PER_RAD = 180.0 / math.pi * 3600.0
# A catalogue object matches a SPHEREx position within this distance (about 0.7 px).
MATCH_RADIUS_ARCSEC = 4.5
# SIMBAD and Gaia entries this close at the same date are the same star.
SAME_STAR_ARCSEC = 1.5
# An archival (2MASS/WISE) position this close to a Gaia star is that star.
ARCHIVAL_RADIUS_ARCSEC = 2.0
# Gaia stars fainter than this are not what SPHEREx detected in a single exposure.
VISIBLE_G_MAG = 19.5
# A star is "slow" for a track when its catalogued motion is under this fraction of
# the track's, and "fast" otherwise.
SLOW_FRACTION = 0.5
# A fast star's catalogued motion agrees with the track's within this many sigma
# (or within SLOW_FRACTION of the motion, whichever is looser).
AGREE_SIGMA = 3.0
MAX_MATCHES = 4

# A solar-system body cannot stay inside one SPHEREx pixel for longer than this, so
# only pass positions measured within such a window can be a body (a main-belt
# asteroid crosses a pixel in minutes, a Kuiper-belt object in about two hours).
MAX_BODY_SPAN_DAYS = 0.1
# SkyBoT positions are geocentric; SPHEREx orbits about 7,000 km from the Earth's
# centre (see planetx.classify.observer). A body is matched within this distance of
# where SPHEREx saw it, on top of its own position error and whatever parallax is
# left unaccounted for.
BODY_MATCH_ARCSEC = 10.0
# Distance assumed for the parallax allowance when SkyBoT gives none.
BODY_MIN_AU = 0.05
# The nearest body the asteroid check allows for. Nearer bodies move several pixels
# during one SPHEREx exposure, so the engine does not see them as point sources;
# without this floor a 0.005 au body would be matched anywhere within 33'.
NEAREST_BODY_AU = 0.02

CLASSIFICATIONS = ("known_star", "known_asteroid", "variable_star", "artifact", "unknown_candidate", "static")


class PositionLike(Protocol):
    """One pass of a track (the engine's `PassSource` fits)."""

    ra: float
    dec: float
    mjd: float


@dataclass(frozen=True)
class Match:
    """A catalogued object matched to an engine object (the API's `KnownMatch`)."""

    catalog: str  # gaia | simbad | skybot
    name: str
    kind: str | None
    sep_arcsec: float
    pm_ra_cosdec: float | None = None  # arcsec/yr
    pm_dec: float | None = None
    magnitude: float | None = None
    variable: bool = False  # the catalogue lists it as a variable star


@dataclass(frozen=True)
class TrackCheck:
    """What the catalogues say about a track that claims to move."""

    # Index (in the FieldCatalog) of the catalogued star that follows the track
    # with a matching proper motion, if any.
    explained_by: int | None = None
    # Every pass position lies on a slow Gaia star: "same" star each time (the
    # object is that star) or "different" stars (the track hops between them).
    slow_stars: str | None = None


def _separation_arcsec(ra1, dec1, ra2: float, dec2: float) -> np.ndarray:
    """Angular distance (haversine: exact at the poles and across RA 0)."""
    a1, d1 = np.radians(ra1), np.radians(dec1)
    a2, d2 = math.radians(ra2), math.radians(dec2)
    h = np.sin((d1 - d2) / 2) ** 2 + np.cos(d1) * math.cos(d2) * np.sin((a1 - a2) / 2) ** 2
    return 2 * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0))) * ARCSEC_PER_RAD


class FieldCatalog:
    """The Gaia and SIMBAD objects of one field, searchable by position at any date."""

    def __init__(self, objects: Sequence[CatalogObject]) -> None:
        self.objects = list(objects)
        self._ra = np.array([o.ra for o in self.objects], dtype=float)
        self._dec = np.array([o.dec for o in self.objects], dtype=float)
        self._epoch = np.array([o.epoch_mjd for o in self.objects], dtype=float)
        self._pm_ra = np.array([o.pm_ra_cosdec or 0.0 for o in self.objects], dtype=float)
        self._pm_dec = np.array([o.pm_dec or 0.0 for o in self.objects], dtype=float)
        # Proper motion in RA is an angle on the sky; dividing by cos(dec) makes it a coordinate step.
        self._cos_dec = np.maximum(np.cos(np.radians(self._dec)), 1e-9)

    def __len__(self) -> int:
        return len(self.objects)

    def positions(self, mjd: float) -> tuple[np.ndarray, np.ndarray]:
        """(ra, dec) of every object at `mjd`, moved linearly with its proper motion.

        Parallax is left out: at most 0.55" (Barnard's Star), far inside the match radius.
        """
        years = (mjd - self._epoch) / DAYS_PER_YEAR
        return (
            self._ra + self._pm_ra * years / 3600.0 / self._cos_dec,
            self._dec + self._pm_dec * years / 3600.0,
        )

    def near(self, ra: float, dec: float, mjd: float, radius_arcsec: float) -> list[tuple[int, float]]:
        """(index, separation in arcsec) of the objects within the radius at that date, nearest first."""
        if not self.objects:
            return []
        sep = _separation_arcsec(*self.positions(mjd), ra, dec)
        found = np.flatnonzero(sep <= radius_arcsec)
        return [(int(i), float(sep[i])) for i in found[np.argsort(sep[found])]]

    def same_star(self, i: int, j: int, mjd: float) -> bool:
        ra, dec = self.positions(mjd)
        return float(_separation_arcsec(ra[i], dec[i], float(ra[j]), float(dec[j]))) <= SAME_STAR_ARCSEC


def _match(obj: CatalogObject, sep: float) -> Match:
    return Match(
        catalog=obj.catalog, name=obj.name, kind=obj.kind, sep_arcsec=sep,
        pm_ra_cosdec=obj.pm_ra_cosdec, pm_dec=obj.pm_dec, magnitude=obj.magnitude, variable=obj.variable,
    )


def identify(field: FieldCatalog, ra: float, dec: float, mjd: float, lead: int | None = None) -> list[Match]:
    """Catalogue objects at a SPHEREx position on that date, best first.

    The object is taken to be `lead` (the star that explains its track) when
    given, else the brightest Gaia star in range: the brightest star dominates a
    6" SPHEREx pixel, and in crowded fields a fainter one is often nearer by
    chance. Its SIMBAD entry, when there is one, comes first because it carries
    the name people know ("Barnard's Star"); other objects in range follow.
    """
    near = dict(field.near(ra, dec, mjd, MATCH_RADIUS_ARCSEC))
    if lead is not None and lead not in near:
        ra_all, dec_all = field.positions(mjd)
        near[lead] = float(_separation_arcsec(ra_all[lead], dec_all[lead], ra, dec))

    def brightness(i: int) -> tuple[bool, float, float]:
        mag = field.objects[i].magnitude
        return (i != lead, mag if mag is not None else math.inf, near[i])

    gaia = sorted((i for i in near if field.objects[i].catalog == "gaia"), key=brightness)
    simbad = sorted((i for i in near if field.objects[i].catalog == "simbad"), key=lambda i: (i != lead, near[i]))
    anchor = lead if lead is not None else (gaia[0] if gaia else None)
    alias = None
    if anchor is not None:
        alias = next((i for i in simbad if i == anchor or field.same_star(i, anchor, mjd)), None)
    order = ([alias] if alias is not None else []) + gaia[:2] + [i for i in simbad if i != alias][:2]
    return [_match(field.objects[i], near[i]) for i in order[:MAX_MATCHES]]


def check_track(
    field: FieldCatalog,
    passes: Sequence[PositionLike],
    claim: tuple[float, float],
    claim_err: float,
) -> TrackCheck:
    """Test a track's claimed motion (`claim`, arcsec/yr east and north) against the catalogues.

    A catalogued star explains the track when it lies within the match radius of
    every pass position on that pass's date and its own proper motion is fast
    enough and agrees with the claim. Failing that, a slow Gaia star bright enough
    for SPHEREx under every pass position means the track shows catalogued stars
    that did not move: one star (the object is that star) or different ones (a
    blend, or a chance link between neighbours). A real mover would need such a
    star by chance at each of its positions.
    """
    speed = math.hypot(*claim)
    if not passes or not speed > 0:
        return TrackCheck()
    per_pass = [dict(field.near(p.ra, p.dec, p.mjd, MATCH_RADIUS_ARCSEC)) for p in passes]

    def magnitude(i: int) -> float:
        mag = field.objects[i].magnitude
        return mag if mag is not None else math.inf

    explained_by = None
    for i in sorted(set.intersection(*(set(found) for found in per_pass)), key=magnitude):
        star = field.objects[i]
        miss = math.hypot((star.pm_ra_cosdec or 0.0) - claim[0], (star.pm_dec or 0.0) - claim[1])
        if star.pm_total >= SLOW_FRACTION * speed and miss <= max(AGREE_SIGMA * claim_err, SLOW_FRACTION * speed):
            explained_by = i
            break
    if explained_by is not None:
        return TrackCheck(explained_by=explained_by)

    slow = [
        {
            i for i in found
            if field.objects[i].catalog == "gaia"
            and magnitude(i) <= VISIBLE_G_MAG
            and field.objects[i].pm_total < SLOW_FRACTION * speed
        }
        for found in per_pass
    ]
    if not all(slow):
        return TrackCheck()
    return TrackCheck(slow_stars="same" if set.intersection(*slow) else "different")


def archive_is_other_stars(
    field: FieldCatalog, ra: float, dec: float, mjd: float, archival: Sequence[PositionLike]
) -> bool:
    """True when every archival position used for a track is a Gaia star that is somewhere else today.

    The archival refit walks back in time and accepts catalogue sources near the
    predicted path. In a crowded field those can be unrelated stars that happen to
    line up. Gaia tells them apart: a star that was at the archival position on
    the archival date and is not at the SPHEREx position (`ra`, `dec` at `mjd`)
    now is another object, so it says nothing about this one's motion.
    """
    if not archival:
        return False
    here = {i for i, _ in field.near(ra, dec, mjd, MATCH_RADIUS_ARCSEC)}
    for source in archival:
        there = [
            i for i, _ in field.near(source.ra, source.dec, source.mjd, ARCHIVAL_RADIUS_ARCSEC)
            if field.objects[i].catalog == "gaia"
        ]
        if not there or there[0] in here:
            return False
    return True


def body_epochs(passes: Sequence) -> list[PositionLike]:
    """The pass positions of a track that a moving solar-system body could have produced.

    A pass position averages detections from several frames. A body moves on
    between frames taken hours apart, so only a pass whose detections all fall
    within MAX_BODY_SPAN_DAYS can be one.
    """
    out = []
    for p in passes:
        times = [d.mjd for d in p.detections]
        if times and max(times) - min(times) <= MAX_BODY_SPAN_DAYS:
            out.append(p)
    return out


@dataclass(frozen=True)
class AsteroidMatch:
    """A known body matched to a SPHEREx position, and how its position was compared."""

    match: Match
    # True: moved to where SPHEREx saw it. False: compared at its geocentric
    # position within the largest parallax (no SPHEREx position, or no distance).
    corrected: bool
    # SkyBoT gave no distance, so the allowance assumed a body BODY_MIN_AU away.
    assumed_distance: bool


def _largest_parallax(distance_au: float) -> float:
    return parallax_arcsec(SPHEREX_MAX_GEOCENTRIC_KM, max(distance_au, NEAREST_BODY_AU))


def asteroid_match(
    ra: float,
    dec: float,
    bodies: Sequence[SolarSystemBody],
    observer_km: Sequence[float] | None = None,
    observer_spread_km: float = 0.0,
) -> AsteroidMatch | None:
    """The known body that a SPHEREx position can be, if any: the nearest one within its tolerance.

    `bodies` are geocentric (SkyBoT). With `observer_km`, SPHEREx's geocentric
    position at the time, each body is first moved to where SPHEREx saw it, and
    only `observer_spread_km` (how far SPHEREx moved between the frames averaged
    into this position) is left as parallax allowance. Without it the allowance is
    the largest parallax SPHEREx's orbit can give at the body's distance: 24" at
    0.4 au, 8' at NEAREST_BODY_AU and below. The spread allowance never exceeds that.
    """
    best: AsteroidMatch | None = None
    for body in bodies:
        distance = body.geocentric_au if body.geocentric_au and body.geocentric_au > 0 else None
        if observer_km is not None and distance is not None:
            body_ra, body_dec = from_observer(body.ra, body.dec, distance, observer_km)
            allowance = min(parallax_arcsec(observer_spread_km, distance), _largest_parallax(distance))
        else:
            body_ra, body_dec = body.ra, body.dec
            allowance = _largest_parallax(distance or BODY_MIN_AU)
        sep = float(_separation_arcsec(body_ra, body_dec, ra, dec))
        if sep > BODY_MATCH_ARCSEC + allowance + (body.position_err_arcsec or 0.0):
            continue
        if best is None or sep < best.match.sep_arcsec:
            best = AsteroidMatch(
                match=Match(catalog="skybot", name=body.name, kind=body.kind, sep_arcsec=sep, magnitude=body.v_mag),
                corrected=observer_km is not None and distance is not None,
                assumed_distance=distance is None,
            )
    return best


def classify(
    kind: str,
    *,
    is_variable: bool,
    variability_caution: bool = False,
    explained: bool = False,
    asteroid: bool = False,
    catalogued_variable: bool = False,
    false_track: bool = False,
) -> str:
    """One of CLASSIFICATIONS for an object whose catalogue check ran.

    `kind` is the motion class after the catalogues had their say (mover |
    candidate | static). `explained`: a catalogued star's proper motion accounts
    for the motion. `asteroid`: a known solar-system body was at one of its
    positions. `false_track`: the track joins different objects (the archive
    refuted it, or its positions fall on different slow Gaia stars).
    """
    if kind in ("mover", "candidate"):
        if explained:
            return "known_star"
        if asteroid:
            return "known_asteroid"
        return "unknown_candidate"
    if false_track:
        return "artifact"
    if is_variable:
        if catalogued_variable:
            return "variable_star"
        # The engine itself warns that a very bright neighbour may fake this change.
        return "artifact" if variability_caution else "unknown_candidate"
    return "static"
