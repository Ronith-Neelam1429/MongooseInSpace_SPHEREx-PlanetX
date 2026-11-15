"""Offline tests for `check_objects`: engine objects in, names, classes and settled motion out.

Catalogues are fakes returning hand-built rows. The Barnard field case repeats the
real engine output and Gaia rows behind the rules (see test_rules.py).
"""
import math
from dataclasses import replace

import numpy as np
import pytest

from planetx.classify import catalogs, check
from planetx.classify.catalogs import CatalogError, CatalogObject, SolarSystemBody
from planetx.classify.check import check_objects, field_cone, motion_kind, unchecked
from planetx.classify.observer import AU_KM, from_observer, unit_vector
from planetx.engine import Detection, PassSource, TrackedObject
from planetx.observatories import ArchivalSource, FusedMotion, Observatory, Verdict

PIXEL = 6.15
GAIA_EPOCH = 57388.5
J2000 = 51544.5


def offset(ra, dec, east, north):
    return ra + east / 3600.0 / math.cos(math.radians(dec)), dec + north / 3600.0


def tracked(passes, pm, pm_err, *, is_mover=False, is_variable=False, variability=None, frame_gap_days=12.0):
    """An engine track from (ra, dec, mjd) per pass; each pass has two detections `frame_gap_days` apart."""
    sources = []
    for k, (ra, dec, mjd) in enumerate(passes):
        detections = [
            Detection(frame_index=2 * k + j, x=0.0, y=0.0, ra=ra, dec=dec, sigma_arcsec=0.5, flux=1.0,
                      flux_err=0.1, snr=10.0, mjd=mjd + (j - 0.5) * frame_gap_days, detector=1)
            for j in range(2)
        ]
        sources.append(PassSource(pass_index=k, ra=ra, dec=dec, sigma_arcsec=0.5, mjd=mjd, n_det=2, n_cover=2,
                                  snr=10.0, flux_by_detector={}, detections=detections))
    n = len(passes)
    return TrackedObject(
        sources=sources, link="mover" if is_mover else "static",
        ra=sum(p[0] for p in passes) / n, dec=sum(p[1] for p in passes) / n, ref_mjd=sum(p[2] for p in passes) / n,
        pm_ra_cosdec=pm[0], pm_dec=pm[1], pm_err=pm_err, chi2_dof=1.0,
        is_mover=is_mover, is_variable=is_variable, variability=variability,
    )


def fused(obj, verdict=Verdict.UNCONFIRMED, pm=None, pm_err=None, matched=()):
    pm = pm or (obj.pm_ra_cosdec, obj.pm_dec)
    return FusedMotion(
        verdict=verdict, ra=obj.ra, dec=obj.dec, ref_mjd=obj.ref_mjd, pm_ra_cosdec=pm[0], pm_dec=pm[1],
        pm_err=pm_err or obj.pm_err, chi2_dof=1.0, baseline_yr=1.0, n_epochs=len(obj.sources), matched=list(matched),
    )


def gaia(name, ra, dec, pm=(None, None), g=15.0, variable=False):
    return CatalogObject("gaia", name, ra, dec, GAIA_EPOCH, pm[0], pm[1], g, "Star", variable)


class Catalogues:
    """Fake fetchers that record what was asked."""

    def __init__(self, gaia=(), simbad=(), bodies=(), spherex_km=(0.0, 0.0, 0.0)):
        """`spherex_km`: SPHEREx's geocentric position for every time asked (the geocentre by
        default, so SkyBoT positions stand as given), or an exception Horizons raises."""
        self.gaia_rows, self.simbad_rows, self.bodies, self.spherex_km = gaia, simbad, bodies, spherex_km
        self.gaia_calls, self.simbad_calls, self.skybot_calls, self.observer_calls = [], [], [], []

    def _answer(self, rows):
        if isinstance(rows, Exception):
            raise rows
        return list(rows)

    def fetch_gaia(self, ra, dec, radius):
        self.gaia_calls.append((ra, dec, radius))
        return self._answer(self.gaia_rows)

    def fetch_simbad(self, ra, dec, radius):
        self.simbad_calls.append((ra, dec, radius))
        return self._answer(self.simbad_rows)

    def fetch_skybot(self, ra, dec, radius, mjd):
        self.skybot_calls.append((ra, dec, radius, mjd))
        return self._answer(self.bodies)

    def fetch_observer(self, mjds):
        self.observer_calls.append(list(mjds))
        if isinstance(self.spherex_km, Exception):
            raise self.spherex_km
        return [self.spherex_km for _ in mjds]

    def check(self, objects, fusion):
        return check_objects(objects, fusion, PIXEL, fetch_gaia=self.fetch_gaia, fetch_simbad=self.fetch_simbad,
                             fetch_skybot=self.fetch_skybot, fetch_observer=self.fetch_observer)


# --- the cached Barnard field: one real mover and two false ones ------------------

BARNARD = tracked([(269.44591, 4.76729, 60914.9), (269.44658, 4.76896, 61115.3), (269.44598, 4.77000, 61276.1)],
                  pm=(0.422, 9.904), pm_err=1.951, is_mover=True)
STILL = tracked([(269.39075, 4.81558, 60910.1), (269.39104, 4.81507, 61129.1), (269.39090, 4.81518, 61271.6)],
                pm=(0.609, -1.511), pm_err=1.137)
HOP = tracked([(269.43913, 4.81072, 60918.7), (269.44030, 4.81223, 61122.7), (269.44057, 4.81243, 61271.9)],
              pm=(5.046, 5.919), pm_err=2.248)
STILL_FUSED = fused(STILL, Verdict.CONFIRMED, pm=(-1.292, -1.530), pm_err=0.071, matched=[
    ArchivalSource(Observatory.ALLWISE, 269.39669, 4.82209, 55356.0, 0.31, 16.62, "W1", "J175735.20+044919.5"),
    ArchivalSource(Observatory.CATWISE, 269.39464, 4.81948, 57170.0, 0.22, 17.27, "W1", "J175734.71+044909.9"),
])
BARNARD_FIELD_GAIA = [
    gaia("Gaia DR3 4472832130942575872", 269.44850252543836, 4.739420051112412,
         pm=(-0.8015509783684709, 10.362394206546573), g=8.19),
    gaia("Gaia DR3 4472837151752184576", 269.39151937956177, 4.814732633349319, pm=(0.0006, -0.0073), g=17.87),
    gaia("Gaia DR3 4472837147453515264", 269.3967271610244, 4.8218689878793075, pm=(-0.0009, -0.0041), g=19.24),
    gaia("Gaia DR3 4472837147460317824", 269.39462663147793, 4.819527786745808, g=20.75),
    gaia("Gaia DR3 4472837834648226688", 269.438656, 4.809998, pm=(0.0043, -0.0015), g=17.87),
    gaia("Gaia DR3 4472837834648241024", 269.440463, 4.812663, pm=(0.0039, -0.0049), g=18.71),
]
BARNARD_FIELD_SIMBAD = [
    CatalogObject("simbad", "Barnard's Star", 269.4520769586187, 4.693364966576667, J2000, -0.801551, 10.362394,
                  8.19, "BY Dra Variable", True),
]


def barnard_field():
    objects = [BARNARD, STILL, HOP]
    return objects, [fused(BARNARD), STILL_FUSED, fused(HOP)]


def test_without_catalogues_the_field_shows_two_movers_and_a_candidate():
    objects, fusion = barnard_field()
    assert [k.kind for k in unchecked(objects, fusion, PIXEL)] == ["mover", "mover", "candidate"]
    assert all(k.classification is None and k.name is None and k.matches == [] for k in unchecked(objects, fusion, PIXEL))


def test_barnard_field_keeps_barnard_and_drops_both_false_movers():
    objects, fusion = barnard_field()
    catalogues = Catalogues(gaia=BARNARD_FIELD_GAIA, simbad=BARNARD_FIELD_SIMBAD)
    result = catalogues.check(objects, fusion)
    barnard, still, hop = result.known

    assert (barnard.kind, barnard.classification, barnard.name) == ("mover", "known_star", "Barnard's Star")
    assert [(m.catalog, m.name) for m in barnard.matches] == [
        ("simbad", "Barnard's Star"), ("gaia", "Gaia DR3 4472832130942575872"),
    ]
    assert barnard.matches[1].pm_dec == pytest.approx(10.3624, abs=1e-4) and barnard.matches[1].sep_arcsec < 1.0

    # Gaia has a 7 mas/yr star here, and the archival "counterparts" are two other Gaia stars.
    assert (still.kind, still.classification, still.name) == ("static", "static", "Gaia DR3 4472837151752184576")
    assert still.archive_void and not barnard.archive_void and not hop.archive_void
    # Positions on two different slow stars: a false track, so it carries no name of its own.
    assert (hop.kind, hop.classification, hop.name) == ("static", "artifact", None)
    assert hop.matches[0].name == "Gaia DR3 4472837834648241024"

    assert any("archival confirmation" in n for n in result.notes)
    assert any("set to static" in n for n in result.notes)
    assert catalogues.skybot_calls == []  # nothing unexplained is left to ask about
    # One Gaia cone for the field, wide enough for a 15"/yr star to have come in since 2016.
    radius = catalogues.gaia_calls[0][2]
    assert len(catalogues.gaia_calls) == 1
    assert radius > field_cone(objects)[2] + 150 and len(catalogues.simbad_calls) == 1
    assert catalogues.simbad_calls[0][2] > radius  # SIMBAD positions are older (J2000)


def test_a_gaia_outage_leaves_everything_unclassified_with_a_note():
    objects, fusion = barnard_field()
    catalogues = Catalogues(gaia=CatalogError("Gaia returned HTTP 503"), simbad=BARNARD_FIELD_SIMBAD)
    result = catalogues.check(objects, fusion)
    assert [k.kind for k in result.known] == ["mover", "mover", "candidate"]  # nothing to veto with
    assert all(k.classification is None and k.name is None for k in result.known)
    assert result.notes == ["Gaia unavailable (Gaia returned HTTP 503): objects were not checked against catalogues"]
    assert catalogues.simbad_calls == [] and catalogues.skybot_calls == []


def test_a_simbad_outage_only_costs_the_names():
    objects, fusion = barnard_field()
    catalogues = Catalogues(gaia=BARNARD_FIELD_GAIA, simbad=CatalogError("SIMBAD (simbad.harvard.edu) returned HTTP 404"))
    result = catalogues.check(objects, fusion)
    barnard = result.known[0]
    assert (barnard.kind, barnard.classification) == ("mover", "known_star")
    assert barnard.name == "Gaia DR3 4472832130942575872"
    assert [k.kind for k in result.known] == ["mover", "static", "static"]
    assert any(n.startswith("SIMBAD unavailable") for n in result.notes)


def test_a_candidate_that_a_fast_gaia_star_follows_becomes_a_known_mover():
    passes = [(*offset(150.0, 20.0, 0.0, 4.0 * k), 60900.0 + 182.6 * k) for k in range(3)]
    candidate = tracked(passes, pm=(0.0, 8.0), pm_err=2.2)  # 3.6 sigma
    assert motion_kind(candidate, None, PIXEL) == "candidate"
    years = (60900.0 - GAIA_EPOCH) / 365.25
    star = gaia("Gaia DR3 1", *offset(150.0, 20.0, 0.0, -7.5 * years), pm=(0.0, 7.5), g=13.0)
    (known,) = Catalogues(gaia=[star]).check([candidate], [fused(candidate)]).known
    assert (known.kind, known.classification, known.name) == ("mover", "known_star", "Gaia DR3 1")


# --- movers no star explains: SkyBoT ---------------------------------------------

def lone_mover(frame_gap_days, ra=150.0, significance=6.0):
    passes = [(*offset(ra, 20.0, 0.0, 9.0 * k), 60900.0 + 180.0 * k) for k in range(3)]
    return tracked(passes, pm=(0.0, 18.0), pm_err=18.0 / significance, is_mover=True, frame_gap_days=frame_gap_days)


def test_an_unexplained_mover_is_an_unknown_candidate_and_skybot_is_asked_per_quick_pass():
    mover = lone_mover(frame_gap_days=0.002)  # frames three minutes apart: a body could be there
    catalogues = Catalogues()
    (known,) = catalogues.check([mover], [fused(mover)]).known
    assert (known.kind, known.classification, known.name, known.matches) == ("mover", "unknown_candidate", None, [])
    assert [(c[0], c[1], c[3]) for c in catalogues.skybot_calls] == [(s.ra, s.dec, s.mjd) for s in mover.sources]


def test_a_known_asteroid_at_one_pass_position_explains_the_mover():
    mover = lone_mover(frame_gap_days=0.002)
    second = mover.sources[1]
    ceres = SolarSystemBody("(1) Ceres", "Main-belt asteroid", *offset(second.ra, second.dec, 4.0, 0.0), second.mjd,
                            8.8, 2.5, 0.01)
    far = SolarSystemBody("(2) Pallas", "Main-belt asteroid", *offset(second.ra, second.dec, 90.0, 0.0), second.mjd,
                          9.0, 2.5, 0.01)

    class OnlyAtSecondPass(Catalogues):
        def fetch_skybot(self, ra, dec, radius, mjd):
            self.skybot_calls.append((ra, dec, radius, mjd))
            return [far, ceres] if mjd == second.mjd else [far]

    catalogues = OnlyAtSecondPass()
    (known,) = catalogues.check([mover], [fused(mover)]).known
    assert (known.kind, known.classification, known.name) == ("mover", "known_asteroid", "(1) Ceres")
    assert known.matches[0].catalog == "skybot" and known.matches[0].sep_arcsec == pytest.approx(4.0, abs=0.01)
    assert len(catalogues.skybot_calls) == 2  # stops at the pass that matched


def test_skybot_cone_holds_a_near_earth_object_seen_from_spherex():
    # 8' of parallax at 0.02 au plus the match radius and SkyBoT's 120" position-error filter.
    assert check.SKYBOT_RADIUS_ARCSEC == pytest.approx(619.5, abs=0.1)
    assert check.SKYBOT_RADIUS_ARCSEC <= catalogs.MAX_RADIUS_ARCSEC


def test_a_near_earth_object_is_moved_to_spherex_and_horizons_is_asked_once_per_pass():
    mover = lone_mover(frame_gap_days=0.002)
    first = mover.sources[0]
    spherex_km = (0.0, 7000.0, 0.0)
    # Geocentric position chosen so that, seen from SPHEREx, the body sits on the first pass.
    geo_ra, geo_dec = from_observer(first.ra, first.dec, 0.05, (0.0, -7000.0, 0.0))
    neo = SolarSystemBody("2026 CD", "Near-Earth asteroid", geo_ra, geo_dec, first.mjd, 19.0, 0.05, 2.0)
    catalogues = Catalogues(bodies=[neo], spherex_km=spherex_km)
    result = catalogues.check([mover], [fused(mover)])
    (known,) = result.known
    assert (known.classification, known.name) == ("known_asteroid", "2026 CD")
    assert known.matches[0].sep_arcsec < 1.0  # where SPHEREx saw it, not 6' away (beyond the 3.3' fallback allowance)
    assert catalogues.observer_calls == [[d.mjd for d in first.detections]]  # one request, this pass's frames
    assert result.notes == []


def test_no_horizons_request_when_skybot_lists_nothing():
    mover = lone_mover(frame_gap_days=0.002)
    catalogues = Catalogues(spherex_km=CatalogError("must not be called"))
    assert catalogues.check([mover], [fused(mover)]).notes == []
    assert len(catalogues.skybot_calls) == 3 and catalogues.observer_calls == []


def test_a_horizons_outage_falls_back_to_geocentric_positions_with_a_note():
    mover = lone_mover(frame_gap_days=0.002)
    # A main-belt body 6" off and an unrelated one 3' off, at every pass.
    near = [SolarSystemBody("(9) Metis", "Main-belt asteroid", *offset(s.ra, s.dec, 6.0, 0.0), s.mjd, 10.0, 2.0, 0.1)
            for s in mover.sources[1:]]

    class Bodies(Catalogues):
        def fetch_skybot(self, ra, dec, radius, mjd):
            self.skybot_calls.append((ra, dec, radius, mjd))
            far = SolarSystemBody("(3) Juno", "Main-belt asteroid", *offset(ra, dec, 180.0, 0.0), mjd, 9.0, 2.0, 0.1)
            return [far] + [b for b in near if b.mjd == mjd]

    catalogues = Bodies(spherex_km=CatalogError("Horizons request failed: ConnectTimeout"))
    result = catalogues.check([mover], [fused(mover)])
    (known,) = result.known
    assert (known.classification, known.name) == ("known_asteroid", "(9) Metis")
    assert len(catalogues.observer_calls) == 1  # not asked again after it failed
    (note,) = result.notes
    assert note.startswith("JPL Horizons unavailable (Horizons request failed: ConnectTimeout): 2 position(s)")
    assert 'largest parallax (9.8" divided by the distance in au, at most 489")' in note
    assert note.endswith("1 known-asteroid match(es) rest on that wider radius")


def geocentric_of(seen_ra, seen_dec, seen_au, observer_km):
    """(ra, dec, distance au) from the Earth's centre of a body `seen_au` from the observer in that direction."""
    g = np.asarray(observer_km) + unit_vector(seen_ra, seen_dec) * seen_au * AU_KM
    d = float(np.linalg.norm(g))
    return math.degrees(math.atan2(g[1], g[0])) % 360.0, math.degrees(math.asin(g[2] / d)), d / AU_KM


class Cone(Catalogues):
    """SkyBoT that, like the real one, lists only the bodies inside the cone asked for."""

    def fetch_skybot(self, ra, dec, radius, mjd):
        self.skybot_calls.append((ra, dec, radius, mjd))
        return [b for b in self.bodies if b.mjd == mjd and separation(ra, dec, b.ra, b.dec) <= radius]


def separation(ra1, dec1, ra2, dec2):
    return math.degrees(math.acos(min(1.0, float(unit_vector(ra1, dec1) @ unit_vector(ra2, dec2))))) * 3600.0


def test_a_body_0_02_au_away_at_the_edge_of_the_skybot_cone_is_found():
    mover = lone_mover(frame_gap_days=0.002)
    first = mover.sources[0]
    # SPHEREx 7,050 km out, at right angles to the line of sight: the largest shift there is.
    perpendicular = np.cross(unit_vector(first.ra, first.dec), (0.0, 0.0, 1.0))
    spherex_km = tuple(7050.0 * perpendicular / np.linalg.norm(perpendicular))
    geo_ra, geo_dec, geo_au = geocentric_of(first.ra, first.dec, 0.02, spherex_km)
    offset_arcsec = separation(first.ra, first.dec, geo_ra, geo_dec)
    assert 480.0 < offset_arcsec < check.SKYBOT_RADIUS_ARCSEC  # 8' out, still inside the cone
    neo = SolarSystemBody("2026 GH", "Near-Earth asteroid", geo_ra, geo_dec, first.mjd, 17.0, geo_au, 5.0)
    catalogues = Cone(bodies=[neo], spherex_km=spherex_km)
    result = catalogues.check([mover], [fused(mover)])
    (known,) = result.known
    assert (known.classification, known.name) == ("known_asteroid", "2026 GH")
    assert known.matches[0].sep_arcsec < 0.5 and result.notes == []


def test_a_horizons_outage_is_remembered_across_runs_for_a_while(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    mover = lone_mover(frame_gap_days=0.002)
    body = SolarSystemBody("(3) Juno", "Main-belt asteroid", *offset(mover.sources[0].ra, mover.sources[0].dec,
                                                                     180.0, 0.0), mover.sources[0].mjd, 9.0, 2.0, 0.1)
    down = Catalogues(bodies=[body], spherex_km=CatalogError("Horizons request failed: ConnectTimeout"))
    down.check([mover], [fused(mover)])
    assert len(down.observer_calls) == 1

    clock[0] += 60.0  # a minute later Horizons is not asked, and the note still says why
    later = Catalogues(bodies=[body], spherex_km=(0.0, 0.0, 7000.0))
    (note,) = later.check([mover], [fused(mover)]).notes
    assert later.observer_calls == []
    assert note.startswith("JPL Horizons unavailable (Horizons request failed: ConnectTimeout; not retried for 10 min)")

    clock[0] += check.HORIZONS_BACKOFF_S  # after the back-off it is tried again
    again = Catalogues(bodies=[body], spherex_km=(0.0, 0.0, 7000.0))
    assert again.check([mover], [fused(mover)]).notes == []
    assert len(again.observer_calls) == 3  # one per pass that SkyBoT listed a body for


def test_spherex_position_is_weighted_like_the_pass_position(monkeypatch):
    mover = lone_mover(frame_gap_days=0.002)
    first = mover.sources[0]
    sharp, blurred = first.detections
    first.detections[:] = [replace(sharp, sigma_arcsec=0.5), replace(blurred, sigma_arcsec=1.0)]  # weights 4:1
    positions = {sharp.mjd: (7000.0, 0.0, 0.0), blurred.mjd: (-7000.0, 0.0, 0.0)}
    seen = []

    class Weighted(Catalogues):
        def fetch_observer(self, mjds):
            return [positions.get(m, (0.0, 0.0, 7000.0)) for m in mjds]

    def spy(ra, dec, bodies, observer_km=None, observer_spread_km=0.0):
        seen.append((observer_km, observer_spread_km))

    monkeypatch.setattr(check, "asteroid_match", spy)
    body = SolarSystemBody("(3) Juno", "Main-belt asteroid", first.ra, first.dec, first.mjd, 9.0, 2.0, 0.1)
    Weighted(bodies=[body]).check([mover], [fused(mover)])
    observer_km, spread = seen[0]  # the first pass
    assert observer_km == pytest.approx((4200.0, 0.0, 0.0)) and spread == pytest.approx(11200.0)


def test_stacked_passes_are_not_checked_for_asteroids():
    mover = lone_mover(frame_gap_days=0.002)
    catalogues = Catalogues(bodies=CatalogError("must not be called"), spherex_km=CatalogError("must not be called"))
    result = check_objects([mover], [fused(mover)], PIXEL, fetch_gaia=catalogues.fetch_gaia,
                           fetch_simbad=catalogues.fetch_simbad, fetch_skybot=catalogues.fetch_skybot,
                           fetch_observer=catalogues.fetch_observer, stacked=True)
    assert result.known[0].classification == "unknown_candidate"
    assert catalogues.skybot_calls == [] and catalogues.observer_calls == []
    assert result.notes == [(
        "stacked passes: movers were not checked for known asteroids "
        "(a co-add's time spans days, no solar-system body fits it)"
    )]


def test_a_match_on_a_body_without_a_distance_is_noted():
    mover = lone_mover(frame_gap_days=0.002)
    first = mover.sources[0]
    comet = SolarSystemBody("C/2026 X1", "Comet", *offset(first.ra, first.dec, 100.0, 0.0), first.mjd, 15.0, None, None)
    result = Catalogues(bodies=[comet], spherex_km=(0.0, 0.0, 7000.0)).check([mover], [fused(mover)])
    assert result.known[0].classification == "known_asteroid"
    assert result.notes == [(
        "1 known-asteroid match(es) on bodies SkyBoT gives no distance for: compared at the "
        'geocentric position within the parallax of a body 0.05 au away (196")'
    )]


def test_skybot_is_not_asked_about_passes_averaged_over_days():
    mover = lone_mover(frame_gap_days=12.0)  # no body stays in one pixel for 12 days
    catalogues = Catalogues(bodies=CatalogError("must not be called"))
    result = catalogues.check([mover], [fused(mover)])
    assert result.known[0].classification == "unknown_candidate"
    assert catalogues.skybot_calls == [] and result.notes == []


def test_a_skybot_outage_leaves_only_the_affected_objects_unclassified():
    mover = lone_mover(frame_gap_days=0.002)
    static = tracked([(150.1, 20.0, 60900.0), (150.1, 20.0, 61260.0)], pm=(0.1, 0.1), pm_err=0.5)
    star = gaia("Gaia DR3 2", 150.1, 20.0, pm=(0.001, 0.001))
    catalogues = Catalogues(gaia=[star], bodies=CatalogError("SkyBoT request failed: ReadTimeout"))
    result = catalogues.check([mover, static], [fused(mover), fused(static)])
    assert (result.known[0].kind, result.known[0].classification) == ("mover", None)
    assert (result.known[1].classification, result.known[1].name) == ("static", "Gaia DR3 2")
    assert len(catalogues.skybot_calls) == 1  # no hammering a service that is down
    assert result.notes == [
        "SkyBoT unavailable (SkyBoT request failed: ReadTimeout): 1 object(s) not checked for known asteroids"
    ]


def test_the_skybot_query_limit_is_spent_on_the_most_significant_movers(monkeypatch):
    monkeypatch.setattr(check, "MAX_BODY_QUERIES", 3)
    weak = lone_mover(frame_gap_days=0.002, ra=150.0, significance=6.0)
    strong = lone_mover(frame_gap_days=0.002, ra=150.2, significance=12.0)
    catalogues = Catalogues()
    result = catalogues.check([weak, strong], [fused(weak), fused(strong)])
    assert [k.classification for k in result.known] == [None, "unknown_candidate"]
    assert {round(c[0], 1) for c in catalogues.skybot_calls} == {150.2}
    assert any("limit of 3 SkyBoT queries" in n for n in result.notes)


# --- brightness changes ----------------------------------------------------------

@pytest.mark.parametrize("simbad_variable, gaia_variable, caution, expected", [
    (True, False, None, "variable_star"),
    (False, True, None, "variable_star"),
    (False, False, None, "unknown_candidate"),
    (False, False, "within 150\" of a very bright star", "artifact"),
    (True, False, "within 150\" of a very bright star", "variable_star"),
])
def test_variables(simbad_variable, gaia_variable, caution, expected):
    variability = {"detector": "D3", "ratio": 1.8, "significance": 9.0, "change": "P3/P1 = 0.56"}
    if caution:
        variability["caution"] = caution
    obj = tracked([(248.0, 66.0, 60900.0), (248.0, 66.0, 61260.0)], pm=(0.1, 0.0), pm_err=0.5,
                  is_variable=True, variability=variability)
    star = gaia("Gaia DR3 3", 248.0, 66.0, pm=(0.001, 0.0), g=9.0, variable=gaia_variable)
    mira = CatalogObject("simbad", "R Dra", 248.0, 66.0, J2000, 0.001, 0.0, 9.0, "Mira Variable", simbad_variable)
    (known,) = Catalogues(gaia=[star], simbad=[mira]).check([obj], [fused(obj)]).known
    assert (known.kind, known.classification, known.name) == ("static", expected, "R Dra")


def test_an_archive_refuted_track_is_an_artifact():
    obj = tracked([(150.0, 20.0, 60900.0), (*offset(150.0, 20.0, 9.0, 9.0), 61260.0)], pm=(9.0, 9.0), pm_err=1.0)
    (known,) = Catalogues().check([obj], [fused(obj, Verdict.REFUTED)]).known
    assert (known.kind, known.classification, known.name) == ("static", "artifact", None)


def test_no_objects_means_no_requests():
    catalogues = Catalogues()
    result = catalogues.check([], [])
    assert result.known == [] and result.notes == [] and catalogues.gaia_calls == []


def test_field_cone_holds_every_position_across_ra_zero():
    a = tracked([(359.95, 10.0, 60900.0), (359.95, 10.0, 61260.0)], pm=(0.0, 0.0), pm_err=0.5)
    b = tracked([(0.05, 10.0, 60900.0), (0.05, 10.0, 61260.0)], pm=(0.0, 0.0), pm_err=0.5)
    ra, dec, radius = field_cone([a, b])
    assert min(ra, 360.0 - ra) < 1e-6 and dec == pytest.approx(10.0, abs=1e-3)
    assert radius == pytest.approx(0.05 * 3600 * math.cos(math.radians(10.0)), rel=1e-3)


# --- tracks on a faint, slow Gaia star (real case: Barnard field, Gaia DR3 4472833711479372160) ---

FAINT = gaia("Gaia DR3 4472833711479372160", 269.40, 4.70, pm=(-0.002, -0.009), g=19.35)
YEAR = 365.25


def along(star, offsets_arcsec, pa_deg, *, sigma, pm_err, start=60900.0, step_yr=1.1):
    """A mover track whose passes lie `offsets_arcsec` from `star` along position angle `pa_deg`."""
    east, north = math.sin(math.radians(pa_deg)), math.cos(math.radians(pa_deg))
    passes = [(*offset(star.ra, star.dec, o * east, o * north), start + k * step_yr * YEAR)
              for k, o in enumerate(offsets_arcsec)]
    speed = (offsets_arcsec[-1] - offsets_arcsec[0]) / (step_yr * (len(offsets_arcsec) - 1))
    obj = tracked(passes, pm=(speed * east, speed * north), pm_err=pm_err, is_mover=True)
    return replace(obj, sources=[replace(s, sigma_arcsec=sigma) for s in obj.sources])


def test_a_noisy_track_on_a_faint_slow_gaia_star_is_static():
    # Pass means scatter by ~2" around the star: the last one is 4.8" off, outside the 4.5" match radius.
    obj = along(FAINT, [-3.5, 0.5, 4.8], 315.0, sigma=2.0, pm_err=0.6)
    result = Catalogues(gaia=[FAINT]).check([obj], [fused(obj)])
    (known,) = result.known
    assert (known.kind, known.classification, known.name) == ("static", "static", FAINT.name)
    assert any("slow Gaia stars lie at every position" in n for n in result.notes)


def test_a_motion_consistent_with_the_slow_lead_star_is_static_using_the_larger_error():
    # Precise passes, one 5" off: no slow star under every pass, but the claim (3.2"/yr)
    # is within 3 sigma of the star's motion with the refit's error (1.2), not with the engine's (0.8).
    obj = along(FAINT, [-2.0, 0.0, 5.0], 315.0, sigma=0.5, pm_err=0.8)
    result = Catalogues(gaia=[FAINT]).check([obj], [fused(obj, pm_err=1.2)])
    assert (result.known[0].kind, result.known[0].classification) == ("static", "static")
    assert any("consistent with the slow Gaia star at the mean position" in n for n in result.notes)


def test_a_motion_somewhat_off_the_slow_lead_star_is_at_most_a_candidate_named_near_it():
    obj = along(FAINT, [-2.0, 0.0, 5.0], 315.0, sigma=0.5, pm_err=0.8)  # 4 sigma off the star
    (known,) = Catalogues(gaia=[FAINT]).check([obj], [fused(obj, pm_err=0.8)]).known
    assert (known.kind, known.classification) == ("candidate", "unknown_candidate")
    assert known.name == f"near {FAINT.name}" and known.matches[0].name == FAINT.name


def test_a_fast_mover_passing_a_faint_static_star_stays_a_mover_named_near_it():
    # 10"/yr north, its middle pass right on the star.
    obj = along(FAINT, [-11.0, 0.0, 11.0], 0.0, sigma=0.5, pm_err=0.5)
    result = Catalogues(gaia=[FAINT]).check([obj], [fused(obj)])
    (known,) = result.known
    assert (known.kind, known.classification) == ("mover", "unknown_candidate")
    assert known.name == f"near {FAINT.name}"
    assert any('named "near" that star' in n for n in result.notes)


def test_a_fast_mover_crossing_a_static_star_off_its_mean_position_keeps_no_star_name():
    # The star lies under the first pass only; the mean position is 11" away: nothing to name it after.
    obj = along(FAINT, [0.0, 11.0, 22.0], 0.0, sigma=0.5, pm_err=0.5)
    (known,) = Catalogues(gaia=[FAINT]).check([obj], [fused(obj)]).known
    assert (known.kind, known.classification, known.name) == ("mover", "unknown_candidate", None)
