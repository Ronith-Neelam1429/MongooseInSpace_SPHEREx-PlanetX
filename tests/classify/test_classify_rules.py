"""Offline tests for the matching and classification rules, on hand-built catalogue rows.

The Barnard's Star rows and the two false movers are the real numbers from the
cached Barnard field (engine run of 2026-11-14 and Gaia DR3).
"""
import math
from dataclasses import dataclass, field

import pytest

from planetx.classify.catalogs import CatalogObject, SolarSystemBody
from planetx.classify.observer import from_observer
from planetx.classify.rules import (
    MATCH_RADIUS_ARCSEC,
    FieldCatalog,
    TrackCheck,
    archive_is_other_stars,
    asteroid_match,
    body_epochs,
    check_track,
    classify,
    identify,
)

GAIA_EPOCH = 57388.5  # J2016.0
J2000 = 51544.5


@dataclass
class Epoch:
    ra: float
    dec: float
    mjd: float
    detections: list = field(default_factory=list)


@dataclass
class Detection:
    mjd: float


def offset(ra: float, dec: float, east: float, north: float) -> tuple[float, float]:
    """The position `east`, `north` arcsec away."""
    return ra + east / 3600.0 / math.cos(math.radians(dec)), dec + north / 3600.0


def gaia(name, ra, dec, pm=(None, None), g=15.0, variable=False) -> CatalogObject:
    return CatalogObject("gaia", name, ra, dec, GAIA_EPOCH, pm[0], pm[1], g, "Star", variable)


def simbad(name, ra, dec, pm=(None, None), mag=None, kind="Star", variable=False) -> CatalogObject:
    return CatalogObject("simbad", name, ra, dec, J2000, pm[0], pm[1], mag, kind, variable)


# Barnard's Star: engine object 0 of the field (SPHEREx alone: 9.9"/yr due north, 5.1 sigma).
BARNARD_GAIA = gaia("Gaia DR3 4472832130942575872", 269.44850252543836, 4.739420051112412,
                    pm=(-0.8015509783684709, 10.362394206546573), g=8.19)
BARNARD_SIMBAD = simbad("Barnard's Star", 269.4520769586187, 4.693364966576667, pm=(-0.801551, 10.362394),
                        mag=8.19, kind="BY Dra Variable", variable=True)
BARNARD_PASSES = [Epoch(269.44591, 4.76729, 60914.9), Epoch(269.44658, 4.76896, 61115.3),
                  Epoch(269.44598, 4.77000, 61276.1)]
BARNARD_REF = (269.44615, 4.76875, 61102.1)
BARNARD_CLAIM = ((0.422, 9.904), 1.951)

# Engine object 1: SPHEREx sees no motion (1.4 sigma), yet AllWISE + CatWISE "confirmed" 2.0"/yr.
STILL_PASSES = [Epoch(269.39075, 4.81558, 60910.1), Epoch(269.39104, 4.81507, 61129.1),
                Epoch(269.39090, 4.81518, 61271.6)]
STILL_REF = (269.39092, 4.81523, 61121.6)
STILL_STAR = gaia("Gaia DR3 4472837151752184576", 269.39151937956177, 4.814732633349319, pm=(0.0006, -0.0073), g=17.87)
ALLWISE = Epoch(269.39669, 4.82209, 55356.0)  # 32" from the SPHEREx position
CATWISE = Epoch(269.39464, 4.81948, 57170.0)  # 20" from it, on the same line
ALLWISE_STAR = gaia("Gaia DR3 4472837147453515264", 269.3967271610244, 4.8218689878793075, pm=(-0.0009, -0.0041), g=19.24)
CATWISE_STAR = gaia("Gaia DR3 4472837147460317824", 269.39462663147793, 4.819527786745808, g=20.75)

# Engine object 2: a 3.5 sigma "candidate" at 7.8"/yr whose positions fall on two different faint stars.
HOP_PASSES = [Epoch(269.43913, 4.81072, 60918.7), Epoch(269.44030, 4.81223, 61122.7),
              Epoch(269.44057, 4.81243, 61271.9)]
HOP_CLAIM = ((5.046, 5.919), 2.248)
HOP_STAR_A = gaia("Gaia DR3 4472837834648226688", 269.438656, 4.809998, pm=(0.0043, -0.0015), g=17.87)
HOP_STAR_B = gaia("Gaia DR3 4472837834648241024", 269.440463, 4.812663, pm=(0.0039, -0.0049), g=18.71)


def test_catalogue_positions_are_moved_to_the_spherex_date():
    catalog = FieldCatalog([BARNARD_GAIA])
    ra, dec, mjd = BARNARD_REF
    # At Gaia's own date the star is 106" south of where SPHEREx sees it.
    assert catalog.near(ra, dec, GAIA_EPOCH, 100.0) == []
    (index, sep), = catalog.near(ra, dec, mjd, MATCH_RADIUS_ARCSEC)
    assert index == 0 and sep < 1.0


def test_search_works_across_ra_zero_and_near_the_pole():
    catalog = FieldCatalog([gaia("a", 359.9999, 10.0), gaia("b", 200.0, 89.9995)])
    assert [i for i, _ in catalog.near(0.0001, 10.0, GAIA_EPOCH, 2.0)] == [0]
    # 0.0005 deg from the pole on opposite sides of it: 3.6" apart although RA differs by 180 deg.
    assert [round(s, 1) for _, s in catalog.near(20.0, 89.9995, GAIA_EPOCH, 5.0)] == [3.6]


def test_barnard_is_named_after_its_simbad_entry_then_gaia():
    faint = gaia("faint neighbour", *offset(*BARNARD_REF[:2], 2.0, 0.5), g=20.9)
    catalog = FieldCatalog([faint, BARNARD_GAIA, BARNARD_SIMBAD])
    matches = identify(catalog, *BARNARD_REF)
    assert [(m.catalog, m.name) for m in matches] == [
        ("simbad", "Barnard's Star"), ("gaia", "Gaia DR3 4472832130942575872"), ("gaia", "faint neighbour"),
    ]
    assert matches[0].kind == "BY Dra Variable" and matches[0].variable
    assert matches[0].sep_arcsec < 1.0 and matches[1].sep_arcsec < 1.0
    assert (matches[1].pm_ra_cosdec, matches[1].pm_dec) == pytest.approx((-0.8016, 10.3624), abs=1e-4)


def test_the_brightest_star_in_range_leads_not_the_nearest():
    ra, dec, mjd = 150.0, 20.0, 61100.0
    catalog = FieldCatalog([
        gaia("faint, nearest", *offset(ra, dec, 0.5, 0.0), g=20.0),
        gaia("bright", *offset(ra, dec, 3.0, 0.0), g=14.0),
        simbad("a galaxy nearby", *offset(ra, dec, -2.0, 0.0), kind="Galaxy"),
        gaia("out of range", *offset(ra, dec, 6.0, 0.0), g=10.0),
    ])
    assert [m.name for m in identify(catalog, ra, dec, mjd)] == ["bright", "faint, nearest", "a galaxy nearby"]


def test_an_object_only_simbad_knows_takes_its_simbad_name():
    # A brown dwarf Gaia cannot see: 8"/yr since J2000, 211" from its SIMBAD position by now.
    ra, dec, mjd = 133.79, -7.24, 61100.0
    start = offset(ra, dec, 8.0 * 26.16, 0.0)
    catalog = FieldCatalog([simbad("WISE J085510.83-071442.5", *start, pm=(-8.0, 0.0), kind="Brown Dwarf")])
    (match,) = identify(catalog, ra, dec, mjd)
    assert match.name == "WISE J085510.83-071442.5" and match.sep_arcsec < 1.0
    assert identify(catalog, ra, dec, J2000) == []


def test_barnards_track_is_explained_by_gaia_even_with_a_faint_star_beside_it():
    faint = gaia("faint neighbour", *offset(BARNARD_PASSES[0].ra, BARNARD_PASSES[0].dec, 3.0, 0.5), g=20.9)
    catalog = FieldCatalog([faint, BARNARD_GAIA])
    assert check_track(catalog, BARNARD_PASSES, *BARNARD_CLAIM) == TrackCheck(explained_by=1)


def test_a_track_hopping_between_two_slow_stars_is_not_motion():
    catalog = FieldCatalog([HOP_STAR_A, HOP_STAR_B])
    assert check_track(catalog, HOP_PASSES, *HOP_CLAIM) == TrackCheck(slow_stars="different")


def test_a_track_sitting_on_one_slow_star_is_that_star():
    catalog = FieldCatalog([STILL_STAR, ALLWISE_STAR, CATWISE_STAR])
    # The archive's claim (2.0"/yr, tiny error) cannot be the 7 mas/yr star's motion.
    assert check_track(catalog, STILL_PASSES, (-1.292, -1.530), 0.071) == TrackCheck(slow_stars="same")


def test_a_real_mover_is_kept_unless_slow_stars_lie_under_every_position():
    passes = [Epoch(*offset(150.0, 20.0, 0.0, 9.0 * k), 60900.0 + 180.0 * k) for k in range(3)]
    claim = ((0.0, 18.0), 2.0)
    under_one = gaia("under the first position only", *offset(150.0, 20.0, 1.0, 1.0), g=17.0)
    assert check_track(FieldCatalog([under_one]), passes, *claim) == TrackCheck()
    # Stars too faint for SPHEREx to have detected do not count, wherever they are.
    faint = [gaia(f"faint {k}", *offset(p.ra, p.dec, 1.0, 0.0), g=20.5) for k, p in enumerate(passes)]
    assert check_track(FieldCatalog(faint), passes, *claim) == TrackCheck()
    assert check_track(FieldCatalog([]), passes, *claim) == TrackCheck()


def test_a_fast_star_moving_another_way_does_not_explain_a_track():
    passes = [Epoch(*offset(150.0, 20.0, 0.0, 4.0 * k), 60900.0 + 365.25 * k) for k in range(2)]
    # In range at both dates, but moving east at 4"/yr while the track goes north at 4"/yr.
    start = offset(150.0, 20.0, -4.0 * (60900.0 - GAIA_EPOCH) / 365.25 - 2.0, 2.0)
    sideways = gaia("sideways", *start, pm=(4.0, 0.0), g=12.0)
    assert check_track(FieldCatalog([sideways]), passes, (0.0, 4.0), 0.5) == TrackCheck()
    # No claimed motion, nothing to test.
    assert check_track(FieldCatalog([sideways]), passes, (0.0, 0.0), 0.5) == TrackCheck()


def test_archival_counterparts_that_are_other_gaia_stars_void_the_confirmation():
    catalog = FieldCatalog([STILL_STAR, ALLWISE_STAR, CATWISE_STAR])
    assert archive_is_other_stars(catalog, *STILL_REF, [ALLWISE, CATWISE]) is True
    # One archival position with no Gaia star: it may be the object itself, years ago.
    assert archive_is_other_stars(FieldCatalog([STILL_STAR, ALLWISE_STAR]), *STILL_REF, [ALLWISE, CATWISE]) is False
    assert archive_is_other_stars(catalog, *STILL_REF, []) is False


def test_archival_positions_of_the_same_star_do_not_void_anything():
    catalog = FieldCatalog([BARNARD_GAIA])
    years = (51692.0 - GAIA_EPOCH) / 365.25  # 2MASS saw Barnard's Star 166" further south
    twomass = Epoch(*offset(BARNARD_GAIA.ra, BARNARD_GAIA.dec, -0.8016 * years, 10.3624 * years), 51692.0)
    assert archive_is_other_stars(catalog, *BARNARD_REF, [twomass]) is False
    # A stationary star found at its own position in the archive is no evidence against it either.
    still = FieldCatalog([STILL_STAR])
    home = Epoch(STILL_STAR.ra, STILL_STAR.dec, 55400.0)
    assert archive_is_other_stars(still, *STILL_REF, [home]) is False


def test_only_passes_measured_within_hours_can_be_a_solar_system_body():
    quick = Epoch(10.0, 5.0, 61000.0, [Detection(61000.000), Detection(61000.002)])
    slow = Epoch(10.0, 5.0, 61200.0, [Detection(61190.0), Detection(61210.0)])
    assert body_epochs([quick, slow]) == [quick]
    assert body_epochs([Epoch(10.0, 5.0, 61000.0)]) == []


def test_asteroid_tolerance_allows_for_spherex_not_being_at_the_earths_centre():
    ra, dec = 269.4485, -23.0

    def body(east, au, err=0.0, name="(213446) 2002 AV66"):
        return SolarSystemBody(name, "Main-belt asteroid", *offset(ra, dec, east, 0.0), 61000.0, 21.6, au, err)

    match = asteroid_match(ra, dec, [body(30.0, 3.0), body(11.0, 3.0, name="(1) Ceres")])
    assert match.name == "(1) Ceres" and match.catalog == "skybot" and match.sep_arcsec == pytest.approx(11.0, abs=0.01)
    assert match.kind == "Main-belt asteroid" and match.magnitude == 21.6 and match.pm_ra_cosdec is None
    assert asteroid_match(ra, dec, [body(30.0, 3.0)]) is None  # 10" + 3" of parallax is not enough
    assert asteroid_match(ra, dec, [body(30.0, 0.4)]) is not None  # a near-Earth body shifts by 24"
    assert asteroid_match(ra, dec, [body(30.0, 3.0, err=20.0)]) is not None  # its own orbit is uncertain
    assert asteroid_match(ra, dec, []) is None


def test_a_near_earth_object_is_matched_where_spherex_saw_it():
    """A body 0.02 au away, seen from SPHEREx 7,000 km off the Earth's centre, is 8' from its geocentric position."""
    geo_ra, geo_dec, au = 269.4485, -23.0, 0.02
    spherex_km = (0.0, 0.0, 7000.0)
    seen_ra, seen_dec = from_observer(geo_ra, geo_dec, au, spherex_km)
    shift = math.hypot((seen_ra - geo_ra) * math.cos(math.radians(geo_dec)), seen_dec - geo_dec) * 3600.0
    assert 400.0 < shift < 490.0
    neo = SolarSystemBody("2026 AB", "Near-Earth asteroid", geo_ra, geo_dec, 61000.0, 18.0, au, 1.0)

    match = asteroid_match(seen_ra, seen_dec, [neo], spherex_km)
    assert match.name == "2026 AB" and match.sep_arcsec < 0.01  # corrected: exact
    # Corrected positions keep the tolerance tight: 20" from where SPHEREx saw it is someone else.
    assert asteroid_match(seen_ra, seen_dec + 20.0 / 3600.0, [neo], spherex_km) is None
    # Frames averaged over SPHEREx's motion leave that motion's parallax as the allowance.
    assert asteroid_match(seen_ra, seen_dec + 20.0 / 3600.0, [neo], spherex_km, observer_spread_km=300.0) is not None
    # Without SPHEREx's position (Horizons down) the geocentric one is matched within the largest parallax.
    fallback = asteroid_match(seen_ra, seen_dec, [neo])
    assert fallback.name == "2026 AB" and fallback.sep_arcsec == pytest.approx(shift, abs=0.01)
    # ...but not beyond it: a body at 1 au gets under 10" of allowance.
    far = SolarSystemBody("(5) Astraea", "Main-belt asteroid", geo_ra, geo_dec, 61000.0, 12.0, 1.0, 0.0)
    assert asteroid_match(seen_ra, seen_dec, [far]) is None
    # A body without a distance is not moved, and gets the allowance of one 0.05 au away.
    unknown = SolarSystemBody("C/2026 X1", "Comet", geo_ra, geo_dec, 61000.0, None, None, None)
    assert asteroid_match(seen_ra, seen_dec, [unknown], spherex_km) is None
    assert asteroid_match(geo_ra, geo_dec + 150.0 / 3600.0, [unknown], spherex_km) is not None


@pytest.mark.parametrize("kind, flags, expected", [
    ("mover", {"explained": True}, "known_star"),
    ("candidate", {"explained": True, "asteroid": True}, "known_star"),
    ("mover", {"asteroid": True}, "known_asteroid"),
    ("mover", {}, "unknown_candidate"),
    ("candidate", {"is_variable": True, "catalogued_variable": True}, "unknown_candidate"),
    ("static", {}, "static"),
    ("static", {"is_variable": True, "catalogued_variable": True}, "variable_star"),
    ("static", {"is_variable": True}, "unknown_candidate"),
    ("static", {"is_variable": True, "variability_caution": True}, "artifact"),
    ("static", {"is_variable": True, "variability_caution": True, "catalogued_variable": True}, "variable_star"),
    ("static", {"false_track": True}, "artifact"),
    ("static", {"false_track": True, "is_variable": True, "catalogued_variable": True}, "artifact"),
    ("static", {"catalogued_variable": True}, "static"),  # catalogued as variable, but we saw no change
])
def test_classification(kind, flags, expected):
    assert classify(kind, **{"is_variable": False, **flags}) == expected
